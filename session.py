"""One monitored session, isolated from the campaign process."""

import csv
from contextlib import contextmanager
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from cpu_runner import CLOCK_COLUMN, NS, save_json, wait_for_collector
from dataset import load_json, make_labels, psi_columns
from workloads import (MemoryWatchdog, cpu_command, fio_command, memory_command,
                       prepare_io_file, read_pairs)


@contextmanager
def lock(path, timeout=0):
    deadline = time.monotonic() + timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Another AIOS process holds {path}") from error
                time.sleep(.1)
        yield


def process_identity(pid):
    if sys.platform != "linux":
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return [boot, fields[19]]
    except (OSError, IndexError):
        return None


def stop_scope(unit):
    present = subprocess.run(["sudo", "-n", "systemctl", "show", "-p", "LoadState",
                              "--value", unit], capture_output=True, text=True, timeout=12)
    if present.stdout.strip() == "not-found":
        return
    subprocess.run(["sudo", "-n", "systemctl", "stop", unit], check=True, timeout=15,
                   capture_output=True, text=True)


def recover(directory):
    """Reconcile leftovers if a prior session supervisor could not clean up."""
    journal = directory / "cleanup.json"
    if not journal.exists():
        return
    with lock(directory / ".session.lock", timeout=35):
        if not journal.exists():
            return
        saved = load_json(journal)
        if sys.platform == "linux":
            # Kill launchers before stopping scopes, preventing a late scope creation.
            for pid, identity in saved["groups"].items():
                pid = int(pid)
                if identity and process_identity(pid) == identity:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (PermissionError, ProcessLookupError):
                        pass
            for unit in saved["scopes"]:
                stop_scope(unit)
        if saved["file"]:
            Path(saved["file"]).unlink(missing_ok=True)
        journal.unlink(missing_ok=True)


class Children:
    def __init__(self, journal, test_file):
        self.journal = journal
        self.file = str(test_file) if test_file else None
        self.groups = {}
        self.scopes = set()
        self.identity = {}
        self.save()

    def save(self):
        save_json(self.journal, {"file": self.file, "groups": self.identity,
                                 "scopes": sorted(self.scopes)})

    def start(self, command, log, scope=None, env=None):
        if scope:
            self.scopes.add(scope)
            self.save()
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=os.name != "nt", env=env)
        self.groups[child.pid] = child, scope
        self.identity[child.pid] = process_identity(child.pid)
        self.save()
        return child

    def stop(self, child, first_signal=signal.SIGTERM, timeout=3):
        _, scope = self.groups[child.pid]
        if os.name == "nt":
            if child.poll() is None:
                child.terminate()
        else:
            try:
                os.killpg(child.pid, first_signal)
            except (PermissionError, ProcessLookupError):
                pass
        if scope:
            stop_scope(scope)
        forced = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            forced = True
        if os.name == "nt":
            if forced:
                child.kill()
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
        child.wait(timeout=5)
        if scope:
            # Stop again after the launcher exits to cover concurrent scope startup.
            stop_scope(scope)
            self.scopes.remove(scope)
        self.groups.pop(child.pid)
        self.identity.pop(child.pid)
        self.save()
        return forced

    def close(self, collector):
        problems = []
        collector_forced = False
        for child, _ in reversed(list(self.groups.values())):
            try:
                forced = self.stop(child, signal.SIGINT if child is collector else signal.SIGTERM,
                                   timeout=10 if child is collector else 3)
                if child is collector:
                    collector_forced = forced
            except (OSError, subprocess.SubprocessError) as error:
                problems.append(str(error))
        for scope in sorted(self.scopes):
            try:
                stop_scope(scope)
            except (OSError, subprocess.SubprocessError) as error:
                problems.append(str(error))
        if self.file:
            try:
                Path(self.file).unlink(missing_ok=True)
            except OSError as error:
                problems.append(str(error))
        if problems:
            raise RuntimeError("Cleanup failed: " + "; ".join(problems))
        self.journal.unlink(missing_ok=True)
        return collector_forced


def collector_metadata(raw, source, run_id):
    nearest = list(source.parent.glob(f"{source.stem}*.json"))
    secondary = list((raw.parent / "metadata").glob("*.json"))
    for path in dict.fromkeys(nearest + list(raw.glob(f"*{run_id}*.json")) + secondary):
        try:
            metadata = load_json(path)
        except (ValueError, OSError):
            continue
        if isinstance(metadata, dict) and metadata.get("experiment_id") == run_id:
            return path
    return None


def validate(spec, source, directory):
    validator = spec.get("validator")
    if not validator:
        return {"status": "unavailable", "reason": "validate_system_data.py was not found"}
    command = [sys.executable, validator] + [
        arg.replace("{csv}", str(source)) for arg in spec["validator_args"]]
    with (directory / "validator.log").open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            return {"status": "error", "reason": str(error)}
    return {"status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode}


def run_session(spec, directory, cancelled):
    collector = None
    source = None
    raw = Path(spec["raw_dir"])
    dry = spec["dry_run"]
    test_file = Path(spec["io_dir"]) / f".aios_{spec['run_id']}.bin" if "io" in spec["tracks"] else None
    metadata = {key: spec[key] for key in
                ("run_id", "machine_id", "category", "profile", "seed", "pilot", "cooldown")}
    if "campaign_attempt" in spec:
        metadata["campaign_attempt"] = spec["campaign_attempt"]
    metadata.update(status="starting", quality="aborted", validator={"status": "not_run"})
    save_json(directory / "metadata.json", metadata)
    children = Children(directory / "cleanup.json", test_file)
    environment = dict(os.environ)
    if dry:
        environment["AIOS_STANDIN_RAW"] = str(raw)
        raw.mkdir(parents=True, exist_ok=True)
    scale = .01 if dry else 1
    interval = .04 if dry else 1
    boot_clock = getattr(time, "CLOCK_BOOTTIME", None)
    boot_offset = time.clock_gettime_ns(boot_clock) - time.monotonic_ns() if boot_clock else 0
    collectors = [sys.executable, "-u", spec["collector"], "--duration",
                  str(math.ceil(spec["seconds"] * scale) + 75), "--experiment-id", spec["run_id"]]
    if cancelled.is_set():
        children.close(collector)
        metadata["status"] = "interrupted"
        save_json(directory / "metadata.json", metadata)
        return
    with (directory / "collector.log").open("w", encoding="utf-8") as log, (
        directory / "workloads.log").open("w", encoding="utf-8") as workload_log, (
        directory / "events.csv").open("w", newline="", encoding="utf-8") as event_file, (
        directory / "probe.csv").open("w", newline="", encoding="utf-8") as probe_file:
        events = csv.writer(event_file)
        events.writerow([CLOCK_COLUMN, "event", "resource", "phase", "detail"])
        probe = csv.writer(probe_file)
        probe.writerow([CLOCK_COLUMN, "wakeup_late_ns", "compute_wall_ns"])

        def note(event, resource="", phase="", detail=None):
            events.writerow([time.monotonic_ns(), event, resource, phase,
                             json.dumps(detail) if detail is not None else ""])
            event_file.flush()

        try:
            if test_file:
                test_file.parent.mkdir(parents=True, exist_ok=True)
                if dry:
                    test_file.write_bytes(b"stand-in")
                    file_size = 8
                else:
                    file_size = prepare_io_file(test_file, spec["io_cap_bytes"], cancelled)
                metadata["io_file_bytes"] = file_size
            previous_files = set(raw.glob("system_*.csv"))
            collector = children.start(collectors, log, env=environment)
            source, _ = wait_for_collector(None, raw, previous_files, collector,
                                           "cpu_psi_some_total_us", 30)
            columns = psi_columns(source)
            full = [name for name in spec["tracks"] if name in spec["thresholds"].get("full", {})]
            full_columns = psi_columns(source, "full", full)
            with source.open(newline="", encoding="utf-8-sig") as handle:
                first = next(csv.DictReader(handle))
                for column in (*columns.values(), *full_columns.values()):
                    int(first[column])
            metadata.update(status="running", collector_source_csv=str(source))
            save_json(directory / "metadata.json", metadata)

            timeline = {}
            for resource, phases in spec["tracks"].items():
                offset = 0
                for phase, duration, params in phases:
                    timeline.setdefault(offset, []).append((resource, phase, duration, params))
                    offset += duration
                if offset != spec["seconds"]:
                    raise ValueError(f"{resource} schedule length mismatch")
            active = {}
            phase_names = {}
            begin = time.monotonic()
            next_probe = begin
            next_watch = begin
            watchdog = MemoryWatchdog(os.sysconf("SC_PAGE_SIZE") if not dry else 4096)
            previous = begin
            payload = bytes(64 * 1024)

            def end_phase(resource):
                for child in active.pop(resource, []):
                    children.stop(child)
                    note("worker_stop", resource, phase_names[resource], child.returncode)
                if resource in phase_names:
                    note("phase_end", resource, phase_names.pop(resource))

            for offset, changes in sorted(timeline.items()) + [(spec["seconds"], [])]:
                deadline = begin + offset * scale
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if cancelled.is_set():
                        raise InterruptedError("campaign supervisor stopped")
                    if collector.poll() is not None:
                        raise RuntimeError("collector exited before session completion")
                    if boot_clock and abs(time.clock_gettime_ns(boot_clock) - time.monotonic_ns()
                                          - boot_offset) > NS // 2:
                        note("suspend_detected")
                        raise RuntimeError("guest suspended during session")
                    if now - previous > (5 if not dry else 1):
                        note("sampling_pause", detail=now - previous)
                        raise RuntimeError("session paused unexpectedly")
                    previous = now
                    if now >= next_watch and not dry:
                        next_watch = now + 1
                        if active.get("memory"):
                            memory = read_pairs("/proc/meminfo")
                            swap = read_pairs("/proc/vmstat").get("pswpin", 0)
                            reason = watchdog.check(memory["MemAvailable"], memory["MemTotal"], swap, now)
                            if reason:
                                note("watchdog_stop", "memory", phase_names["memory"], reason)
                                end_phase("memory")
                        else:
                            watchdog = MemoryWatchdog(os.sysconf("SC_PAGE_SIZE"))
                    for resource, workers in list(active.items()):
                        for child in workers[:]:
                            code = child.poll()
                            if code is not None:
                                note("worker_exit", resource, phase_names[resource], code)
                                # Command/setup failures must not masquerade as successful load.
                                if resource != "memory" or code not in (-9, 137):
                                    raise RuntimeError(f"{resource} worker exited early with code {code}")
                                children.stop(child)
                                workers.remove(child)
                    if now >= next_probe:
                        stamp = time.monotonic_ns()
                        hashlib.sha256(payload).digest()
                        probe.writerow([stamp, max(0, stamp - int(next_probe * NS)),
                                        time.monotonic_ns() - stamp])
                        probe_file.flush()
                        next_probe = now + interval
                    cancelled.wait(max(0, min(deadline - now, .02 if dry else .2)))
                if cancelled.is_set():
                    raise InterruptedError("campaign supervisor stopped")
                for resource, phase, duration, params in changes:
                    end_phase(resource)
                    phase_names[resource] = phase
                    note("phase_start", resource, phase, {"seconds": duration, "parameters": params})
                    for index, entry in enumerate(params):
                        unit = None
                        if dry:
                            command = [sys.executable, spec["standins"], "fio" if resource == "io"
                                       else "stress-ng", resource, str(duration)]
                        elif resource == "cpu":
                            command = cpu_command(spec["stress"], entry, duration + 4)
                        elif resource == "memory":
                            unit = f"aios-{spec['run_id']}-{offset}-{index}.scope"
                            available = read_pairs("/proc/meminfo")["MemAvailable"] * 1024
                            command = memory_command(spec["stress"], unit, entry, spec["ram_bytes"],
                                                     available, duration + 4, getpass.getuser())
                            if command is None:
                                note("watchdog_stop", "memory", phase,
                                     "insufficient headroom for memory scope and guest reserve")
                                break
                        else:
                            command = fio_command(spec["fio"], test_file, file_size, entry, duration + 4)
                        child = children.start(command, workload_log, scope=unit)
                        active.setdefault(resource, []).append(child)
                        note("worker_start", resource, phase, {"pid": child.pid, "argv": command})
            if collector.poll() is not None:
                raise RuntimeError("collector exited before session completion")
            for resource in list(phase_names):
                end_phase(resource)
            metadata.update(status="complete", quality="ok")
        except InterruptedError as error:
            metadata.update(status="interrupted", error=str(error))
        except Exception as error:
            metadata.update(status="failed", error=str(error))
        finally:
            note("session_end", detail=metadata["status"])
            try:
                forced = children.close(collector)
                metadata["collector_shutdown_timed_out"] = forced
                if forced and metadata["status"] == "complete":
                    metadata.update(status="failed", quality="aborted",
                                    error="collector did not finish shutdown within 10 seconds")
            except RuntimeError as error:
                metadata.update(status="failed", quality="aborted", cleanup_error=str(error))
            if collector is not None:
                metadata["collector_exit_code"] = collector.returncode
            if source is not None:
                try:
                    system = directory / "system.csv"
                    shutil.copyfile(source, system)
                    original = collector_metadata(raw, source, spec["run_id"])
                    metadata["collector_metadata"] = "missing" if original is None else "copied"
                    if original:
                        shutil.copyfile(original, directory / "collector_metadata.json")
                    metadata["validator"] = validate(spec, system, directory)
                    if metadata["status"] == "complete" and metadata["validator"]["status"] in {"failed", "error"}:
                        metadata.update(status="failed", quality="aborted",
                                        error="telemetry validator rejected the session; see validator.log")
                    if metadata["status"] == "complete" and not spec["pilot"]:
                        for resource in spec["tracks"]:
                            label = Path(spec["root"]) / "labels" / f"{spec['run_id']}_{resource}.csv"
                            counts = make_labels(system, label, resource, spec["thresholds"][resource], interval)
                            metadata.setdefault("label_counts", {})[resource] = counts
                            if resource in full:
                                target = label.with_name(f"{label.stem}_full.csv")
                                metadata["label_counts"][f"{resource}_full"] = make_labels(
                                    system, target, resource, spec["thresholds"]["full"][resource],
                                    interval, "full")
                            reasons = []
                            if not spec["resource_signal"][resource]:
                                reasons.append("calibration did not establish sustained signal")
                            if counts["onsets"] == 0:
                                reasons.append("no sustained PSI some onsets in the session")
                            for horizon, totals in counts["horizons"].items():
                                if not totals["positive"] or not totals["negative"]:
                                    reasons.append(f"{horizon}s horizon lacks positive or negative examples")
                            metadata.setdefault("resource_quality", {})[resource] = {
                                "quality": "low_signal" if reasons else "ok", "reasons": reasons}
                        if any(item["quality"] == "low_signal"
                               for item in metadata["resource_quality"].values()):
                            metadata["quality"] = "low_signal"
                except (OSError, ValueError, TypeError) as error:
                    metadata.update(status="failed", quality="aborted", error=str(error))
            save_json(directory / "metadata.json", metadata)


def supervise(spec_path):
    directory = spec_path.parent
    cancelled = threading.Event()

    def watch():
        # The writer is held only by the campaign process, including during a crash.
        os.read(sys.stdin.fileno(), 1)
        cancelled.set()

    threading.Thread(target=watch, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    with lock(directory / ".session.lock", timeout=35):
        run_session(load_json(spec_path), directory, cancelled)
