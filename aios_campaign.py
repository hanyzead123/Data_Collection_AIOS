"""Plan, collect and package AIOS multi-resource pressure campaigns."""

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
import zipfile

from cpu_runner import save_json
from dataset import completed, load_json, make_labels, merge, package, safe_component, thresholds
from pilot import GRID, assess, compare, measure
from session import lock, recover, supervise
from workloads import GIB, MIB, RESOURCES, backing_disk, graded_schedule, psi_avg10, read_pairs

PROFILES = {"long": (12, 15), "short": (2, 10)}
SHARES = {"cpu": .35, "memory": .25, "io": .25, "mixed": .15}
HERE = Path(__file__).resolve().parent


def derived_seed(*parts):
    return int.from_bytes(hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).digest(), "big")


def machine_id(override):
    if override:
        if not safe_component(override) or len(override) > 32:
            raise ValueError("Machine IDs must be at most 32 ASCII letters, digits, underscores or hyphens")
        return override
    machine_file = Path("/etc/machine-id")
    identity = machine_file.read_text().strip() if machine_file.exists() else str(uuid.getnode())
    return f"m{derived_seed(socket.gethostname(), identity):064x}"[:25]


def build_plan(profile, budget_hours, identity, min_mixed=None):
    default, minutes = PROFILES[profile]
    hours = default if budget_hours is None else budget_hours
    if not math.isfinite(hours) or not 0 < hours <= 8760:
        raise ValueError("Budget must be a finite number greater than 0 and at most 8760 hours")
    seconds = int(hours * 3600)
    session = minutes * 60
    usable = seconds * .90
    counts = {category: max(1, math.floor(usable * share / (session + 60)))
              for category, share in SHARES.items()}
    if sum(counts.values()) * (session + 60) > usable:
        raise ValueError("Budget cannot fit at least one session per resource plus scheduled cool-downs")
    spare = math.floor(usable / (session + 60)) - sum(counts.values())
    minimum = (2 if profile == "short" else 1) if min_mixed is None else min_mixed
    extra = max(0, minimum - counts["mixed"])
    if min_mixed is not None and extra > spare:
        raise ValueError("Budget cannot fit the requested minimum mixed sessions")
    counts["mixed"] += min(spare, extra)
    sessions = []
    for category, count in counts.items():
        for index in range(1, count + 1):
            seed = derived_seed(identity, category, index)
            sessions.append({"category": category, "index": index, "seconds": session, "seed": seed,
                             "run_id": f"{identity}-{category}-{index:03}"})
    return {"profile": profile, "machine_id": identity, "budget_seconds": seconds,
            "usable_seconds": int(usable), "session_seconds": session,
            "cooldown_seconds": 60, "counts": counts, "sessions": sessions}


def print_plan(plan, start):
    estimate = min(plan["budget_seconds"], 9 * 60 + len(plan["sessions"]) *
                   (plan["session_seconds"] + 60))
    print(json.dumps({**plan, "expected_finish": datetime.fromtimestamp(start + estimate).astimezone().isoformat(),
                      "budget_deadline": datetime.fromtimestamp(start + plan["budget_seconds"]).astimezone().isoformat()},
                     indent=2), flush=True)


def capture(command, timeout=8):
    try:
        probe = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return probe.returncode, (probe.stdout or probe.stderr).strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)


def disk_parent(path):
    return next(parent for parent in (path, *path.parents) if parent.is_dir())


def io_location(path):
    fs, source, block = backing_disk(disk_parent(path))
    return fs, source, block and fs not in {"tmpfs", "ramfs", "devtmpfs"}


def doctor(args):
    outcomes = []

    def check(name, passed, detail, fix):
        outcomes.append(passed)
        print(f"{'PASS' if passed else 'FAIL'} {name} — {detail}" +
              ("" if passed else f" | Fix: {fix}"))

    linux = sys.platform == "linux"
    check("Linux", linux, platform.system(), "Boot your Ubuntu VMware VM and rerun doctor there")
    check("Python", sys.version_info >= (3, 11), platform.python_version(),
          "sudo apt install python3.14 python3.14-venv && python3.14 -m venv .venv")
    for tool, prefix in (("stress-ng", "stress-ng"), ("fio", "fio-")):
        code, result = capture([tool, "--version"])
        check(tool, code == 0 and result.startswith(prefix), result or "version missing", f"sudo apt install {tool}")
    code, result = capture(["sudo", "-n", "-k", "true"])
    check("passwordless sudo", linux and code == 0, result or "non-interactive privilege probe",
          "sudo visudo -f /etc/sudoers.d/aios (VM administrator must configure NOPASSWD for the operator)")
    controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    cgroups = controllers.is_file() and "memory" in controllers.read_text().split()
    check("cgroup v2 and memory controller", cgroups, str(controllers),
          "sudoedit /etc/default/grub && sudo update-grub && sudo reboot (enable unified cgroup v2)")
    try:
        readings = {resource: psi_avg10(resource) for resource in RESOURCES}
    except (OSError, ValueError, IndexError):
        readings = {}
    check("system PSI", len(readings) == 3, str(readings),
          "sudoedit /etc/default/grub && sudo update-grub && sudo reboot (add psi=1)")
    free = shutil.disk_usage(disk_parent(args.io_dir)).free
    root_free = shutil.disk_usage(disk_parent(args.root)).free
    check("free disk", min(free, root_free) >= 5 * GIB,
          f"I/O {free / GIB:.2f} GiB, output {root_free / GIB:.2f} GiB",
          "df -h; sudo apt clean; free or expand the selected VM disk to at least 5 GiB free")
    memory = read_pairs("/proc/meminfo") if Path("/proc/meminfo").is_file() else {}
    available = memory.get("MemAvailable", 0)
    check("free RAM", available >= max(512 * 1024, .15 * memory.get("MemTotal", 0)),
          f"{available / 1024:.0f} MiB available", "free -h; close background apps or increase VM RAM")
    code, help_text = capture([sys.executable, str(args.collector), "--help"])
    check("collector", code == 0 and all(flag in help_text for flag in ("--duration", "--experiment-id")),
          str(args.collector), "python3 aios_campaign.py doctor --collector /path/to/system_collector.py")
    batteries = list(Path("/sys/class/power_supply").glob("BAT*/status"))
    on_battery = any(path.read_text().strip() == "Discharging" for path in batteries)
    check("AC power", not on_battery,
          "Host power is not exposed by this guest. Verify host AC." if not batteries else "Guest power status",
          "Plug the VMware host into AC power and rerun doctor")
    fs, source, valid = io_location(args.io_dir) if linux else ("unavailable", "-", False)
    check("I/O backing disk", valid, f"{source} ({fs})",
          "mkdir -p ~/aios-io && python3 aios_campaign.py doctor --io-dir ~/aios-io")
    return all(outcomes)


def host_record(args, identity):
    dry = args.dry_run
    mem = {} if dry else read_pairs("/proc/meminfo")
    cpus = 4 if dry else len(os.sched_getaffinity(0))
    disk = shutil.disk_usage(disk_parent(args.io_dir))
    filesystem, device, _ = ("stand-in", "stand-in", False) if dry else io_location(args.io_dir)
    _, graph = (0, "") if dry else capture(["lsblk", "--json", "--bytes", "-o",
                                            "NAME,PATH,TYPE,SIZE,ROTA,TRAN,FSTYPE"])
    try:
        block_devices = json.loads(graph)["blockdevices"]
    except (ValueError, KeyError, TypeError):
        block_devices = []
    _, virt = (0, "stand-in") if dry else capture(["systemd-detect-virt"])
    versions = {name: "stand-in" if dry else capture([name, "--version"])[1]
                for name in ("stress-ng", "fio")}
    return {"machine_id": identity, "hostname": socket.gethostname(), "vcpus": cpus,
            "ram_bytes": mem.get("MemTotal", 5 * GIB // 1024) * 1024,
            "swap_bytes": mem.get("SwapTotal", 0) * 1024,
            "disk": {"type": filesystem, "source": device, "filesystem_bytes": disk.total,
                     "block_devices": block_devices},
            "kernel": platform.release(), "virtualization": virt, "python": platform.python_version(),
            "tool_versions": versions, "host_notes": args.host_notes, "dry_run": dry}


def idle_wait(seconds, dry):
    if dry:
        return {"waited_seconds": 0, "near_idle": True, "avg10": dict.fromkeys(RESOURCES, 0)}
    start = time.monotonic()
    while True:
        readings = {resource: psi_avg10(resource) for resource in RESOURCES}
        elapsed = time.monotonic() - start
        idle = max(readings.values()) <= 1
        if (idle and elapsed >= seconds) or elapsed >= 300:
            return {"waited_seconds": round(elapsed, 3), "near_idle": idle, "avg10": readings}
        time.sleep(min(2, 300 - elapsed))


def run_guardian(spec, directory):
    directory.mkdir(parents=True, exist_ok=True)
    spec_path = directory / "session_spec.json"
    save_json(spec_path, spec)
    process = subprocess.Popen([sys.executable, __file__, "_session", str(spec_path)],
                               stdin=subprocess.PIPE, start_new_session=os.name != "nt")
    try:
        code = process.wait()
    finally:
        process.stdin.close()
        if process.poll() is None:
            try:
                process.wait(timeout=100)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                recover(directory)
    if code:
        recover(directory)
        raise RuntimeError(f"Session supervisor exited {code}; inspect {directory}")
    return load_json(directory / "metadata.json")


def existing_record(directory):
    if not directory.exists():
        return None
    # The campaign lock can be released before a killed campaign's session guardian finishes.
    with lock(directory / ".session.lock", timeout=100):
        path = directory / "metadata.json"
        return load_json(path) if path.exists() else None


def reconcile(root):
    for folder in ("runs", "calibration"):
        for directory in (root / folder).glob("*/"):
            if directory.is_dir() and not directory.is_symlink():
                existing_record(directory)
                recover(directory)


def retry_directory(directory, item, record):
    if not directory.exists():
        return
    recover(directory)
    attempt = directory.with_name(f"{directory.name}-aborted-{uuid.uuid4().hex[:8]}")
    record = record or {key: item[key] for key in ("run_id", "category", "seed")}
    if record.get("status") not in {"failed", "interrupted"}:
        record["status"] = "interrupted"
    record.update(run_id=attempt.name, quality="aborted")
    save_json(directory / "metadata.json", record)
    directory.rename(attempt)


def spec_for(args, item, machine, config, idle, signal_data, calibration=False):
    category = item["category"]
    resources = RESOURCES if category == "mixed" else (category,)
    tracks = {name: graded_schedule(name, derived_seed(item["seed"], name), item["seconds"],
                                    machine["vcpus"], 2 if category == "mixed" else 3,
                                    calibration, signal_data.get(name, {}).get("level", 3))
              for name in resources}
    collector = HERE / "standins.py" if args.dry_run else args.collector
    validator = (HERE / "standins.py" if args.dry_run else args.validator or
                 args.collector.with_name("validate_system_data.py"))
    return {**item, "machine_id": machine["machine_id"], "profile": args.profile,
            "pilot": calibration, "tracks": tracks, "thresholds": config, "cooldown": idle,
            "resource_signal": {name: signal_data[name]["ok"] for name in resources if name in signal_data},
            "dry_run": args.dry_run, "root": str(args.root),
            "standins": str(HERE / "standins.py"), "raw_dir": str(args.raw_dir),
            "io_dir": str(args.io_dir), "collector": str(collector),
            "validator": str(validator) if validator.is_file() else None,
            "validator_args": (["validator", "{csv}"] if args.dry_run else
                               (args.validator_arg or ["{csv}"])),
            "stress": shutil.which("stress-ng"), "fio": shutil.which("fio"),
            "ram_bytes": machine["ram_bytes"], "io_cap_bytes": args.io_max_mib * MIB}


def calibrate(args, machine, config, deadline=None):
    folder = args.root / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    result_path = folder / "calibration.json"
    result = {}
    for resource in RESOURCES:
        calibration_item = {"category": resource, "index": 0, "seconds": 180,
                            "seed": derived_seed(machine["machine_id"], "calibration", resource),
                            "run_id": f"{machine['machine_id']}-cal-{resource}"}
        directory = folder / resource
        record = existing_record(directory)
        if not record or record["status"] != "complete":
            if deadline and time.time() + 180 + 60 > deadline and not args.dry_run:
                result[resource] = {"error": "remaining budget cannot fit calibration"}
                continue
            retry_directory(directory, calibration_item, record)
            idle = idle_wait(0, args.dry_run)
            spec = spec_for(args, calibration_item, machine, {}, idle, {}, calibration=True)
            record = attempt_session(spec, directory)
        if record["status"] != "complete":
            result[resource] = {"error": record.get("error", record["status"])}
        else:
            candidates = sorted(set(GRID) | ({config[resource]} if resource in config else set()))
            try:
                result[resource] = measure(directory, resource, .04 if args.dry_run else 1, candidates)
            except (OSError, ValueError, TypeError) as error:
                result[resource] = {"error": str(error)}
        print(f"Calibration {resource}: {json.dumps(result[resource])}", flush=True)
    save_json(result_path, result)
    save_json(folder / "pilot.json", {"machine_id": machine["machine_id"],
                                     "dry_run": args.dry_run, "resources": result})
    return result


def attempt_session(spec, directory):
    try:
        return run_guardian(spec, directory)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        recover(directory)
        record = existing_record(directory) or {key: spec[key] for key in
                ("run_id", "machine_id", "category", "profile", "seed", "pilot", "cooldown")}
        record.update(status="failed", quality="aborted", error=str(error))
        if "campaign_attempt" in spec:
            record["campaign_attempt"] = spec["campaign_attempt"]
        save_json(directory / "metadata.json", record)
        return record


def campaign_session(args, item, machine, config, signal_data, state, state_path, deadline):
    run_id = item["run_id"]
    directory = args.root / "runs" / run_id
    record = existing_record(directory)
    checkpoint = state["sessions"].get(run_id, {})
    failures = checkpoint.get("failed_attempts", 0)
    if record and record["status"] == "failed":
        failures = max(failures, record.get("campaign_attempt", 1))
    if record and (completed(record) or failures >= 2):
        state["sessions"][run_id] = {**checkpoint, "status": record["status"],
                                     "quality": record["quality"], "failed_attempts": failures,
                                     "cooldown": record.get("cooldown", {})}
        save_json(state_path, state)
        return True
    while failures < 2:
        margin = item["seconds"] + 60
        if not args.dry_run and deadline - time.time() < margin:
            return False
        idle = idle_wait(60 if state["sessions"] else 0, args.dry_run)
        if not args.dry_run and deadline - time.time() < margin:
            state["budget_stop_cooldown"] = idle
            return False
        retry_directory(directory, item, record)
        state["sessions"][run_id] = {"status": "starting", "failed_attempts": failures,
                                     "cooldown": idle}
        save_json(state_path, state)
        spec = spec_for(args, {**item, "campaign_attempt": failures + 1}, machine,
                        config, idle, signal_data)
        record = attempt_session(spec, directory)
        if not completed(record):
            failures += 1
            record.update(status="failed", quality="aborted", campaign_attempt=failures)
            save_json(directory / "metadata.json", record)
        state["sessions"][run_id] = {"status": record["status"], "quality": record["quality"],
                                     "failed_attempts": failures, "cooldown": idle}
        save_json(state_path, state)
        print(f"{run_id}: {record['status']} / {record['quality']}" +
              (f" ({record.get('error', 'session failed')}); " +
               ("retrying once" if failures == 1 else "continuing to the next session")
               if record["status"] == "failed" else ""), flush=True)
        if completed(record):
            return True
    return True


def campaign(args, plan, machine, config):
    state_path = args.root / "campaign_state.json"
    state = load_json(state_path) if state_path.exists() else None
    if state is None:
        start = time.time()
        state = {"start_unix": start, "date": datetime.now().astimezone().strftime("%Y%m%d"),
                 "status": "planned", "sessions": {}}
        save_json(state_path, state)
    deadline = state["start_unix"] + plan["budget_seconds"]
    try:
        measurements = calibrate(args, machine, config, deadline)
        signal_data = {name: assess(measurements[name], config[name]) for name in RESOURCES}
        low_signal = [name for name in RESOURCES if not signal_data[name]["ok"]]
        state["calibration_signal"] = signal_data
        if low_signal:
            print("Low calibration signal: " + ", ".join(low_signal), file=sys.stderr, flush=True)
            if args.require_signal:
                state["status"] = "calibration_low_signal"
                raise SystemExit("Calibration signal requirement was not met. See the packaged pilot measurements.")
        state["status"] = "running"
        for item in plan["sessions"]:
            if not campaign_session(args, item, machine, config, signal_data, state, state_path, deadline):
                state["status"] = "budget_exhausted"
                break
        else:
            state["status"] = ("complete_with_failures" if any(
                entry["status"] == "failed" for entry in state["sessions"].values()) else "complete")
    except (KeyboardInterrupt, Exception):
        state["status"] = "interrupted"
        raise
    finally:
        save_json(state_path, state)
        print(package(args.root), flush=True)


def setup(args, plan, config):
    args.root.mkdir(parents=True, exist_ok=True)
    frozen = (("campaign_plan.json", plan), ("thresholds.json", config))
    for name, value in frozen if args.command == "campaign" else ():
        path = args.root / name
        if path.exists() and load_json(path) != value:
            raise ValueError(f"Saved {name} differs; use the original configuration or a new --root")
        if not path.exists():
            save_json(path, value)
    path = args.root / "machine.json"
    if path.exists():
        machine = load_json(path)
        if (machine["dry_run"] != args.dry_run or machine["hostname"] != socket.gethostname()
                or machine["machine_id"] != plan["machine_id"]):
            raise ValueError("Campaign root belongs to a different machine identity, host or collection mode")
    else:
        machine = host_record(args, plan["machine_id"])
        save_json(path, machine)
    return machine


def parser():
    top = argparse.ArgumentParser(description=__doc__)
    commands = top.add_subparsers(dest="command", required=True)
    for name in ("plan", "doctor", "calibrate", "campaign"):
        sub = commands.add_parser(name)
        sub.add_argument("--profile", choices=PROFILES, default="short")
        sub.add_argument("--budget-hours", type=float)
        sub.add_argument("--machine-id")
        sub.add_argument("--root", type=Path, default=Path("campaign_data"))
        sub.add_argument("--min-mixed-sessions", type=int)
        if name == "plan":
            continue
        sub.add_argument("--collector", type=Path, default=Path("system_collector.py"))
        sub.add_argument("--raw-dir", type=Path, default=Path("~/aios_data/raw"))
        sub.add_argument("--io-dir", type=Path)
        sub.add_argument("--io-max-mib", type=int, default=4096, metavar="1..4096",
                         help="cap I/O test file; also limited to 10%% of available disk")
        sub.add_argument("--require-signal", action="store_true",
                         help="stop before production unless all resources have sustained pilot signal")
        sub.add_argument("--thresholds", type=Path)
        sub.add_argument("--validator", type=Path)
        sub.add_argument("--validator-arg", action="append", help="validator argument; use {csv} for the input path")
        sub.add_argument("--host-notes", default="")
        sub.add_argument("--dry-run", action="store_true",
                         help="accelerated run against bundled stand-ins; writes a separate simulated campaign")
    sub = commands.add_parser("label")
    sub.add_argument("system_csv", type=Path)
    sub.add_argument("output", type=Path)
    sub.add_argument("--resource", required=True, choices=RESOURCES)
    sub.add_argument("--kind", choices=("some", "full"), default="some")
    sub.add_argument("--thresholds", type=Path, required=True)
    sub.add_argument("--interval", type=float, default=1)
    sub = commands.add_parser("package")
    sub.add_argument("--root", type=Path, default=Path("campaign_data"))
    sub.add_argument("--output", type=Path)
    sub = commands.add_parser("merge")
    sub.add_argument("archives", nargs="+", type=Path)
    sub.add_argument("--output", type=Path, required=True)
    sub.add_argument("--split-overrides", type=Path)
    sub = commands.add_parser("_session")
    sub.add_argument("spec", type=Path)
    sub = commands.add_parser("compare-calibration")
    sub.add_argument("pilots", nargs="+", type=Path, help="pilot.json files or campaign roots")
    sub.add_argument("--output", type=Path, help="write the comparison report as JSON")
    sub = commands.add_parser("init-machine")
    sub.add_argument("--root", type=Path, default=Path("campaign_data"))
    return top


def main():
    args = parser().parse_args()
    if args.command == "_session":
        supervise(args.spec)
        return
    if args.command == "init-machine":
        with lock(args.root / ".campaign.lock"):
            path = args.root / "machine_identity.json"
            existing = args.root / "machine.json"
            if existing.exists():
                raise ValueError("This root already contains a machine record; use a fresh root on the clone")
            if not path.exists():
                save_json(path, {"machine_id": "m" + uuid.uuid4().hex[:24]})
            print(load_json(path)["machine_id"])
        return
    if args.command == "compare-calibration":
        report = compare(args.pilots)
        if args.output:
            if args.output.exists():
                raise ValueError("Comparison output already exists")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            save_json(args.output, report)
        print(json.dumps(report["suggested_thresholds"], indent=2))
        return
    if args.command == "merge":
        print(f"Merged {merge(args.archives, args.output, load_json(args.split_overrides) if args.split_overrides else {})} runs")
        return
    if args.command == "label":
        if not math.isfinite(args.interval) or args.interval <= 0 or args.system_csv.resolve() == args.output.resolve():
            raise ValueError("Use a positive sampling interval and a separate label output")
        selected = thresholds(args.thresholds)
        values = selected if args.kind == "some" else selected.get("full", {})
        if args.resource not in values:
            raise ValueError(f"No frozen {args.resource} PSI {args.kind} threshold")
        print(json.dumps(make_labels(args.system_csv, args.output, args.resource,
                                     values[args.resource], args.interval, args.kind)))
        return
    if args.command == "package":
        with lock(args.root / ".campaign.lock"):
            # Wait for a supervisor orphaned by a hard kill before capturing a snapshot.
            reconcile(args.root)
            print(package(args.root, args.output))
        return
    args.root = args.root.expanduser().resolve()
    identity = args.root / "machine_identity.json"
    if not args.machine_id and identity.exists():
        args.machine_id = load_json(identity)["machine_id"]
    if args.min_mixed_sessions is not None and args.min_mixed_sessions < 1:
        raise ValueError("--min-mixed-sessions must be at least 1")
    plan = build_plan(args.profile, args.budget_hours, machine_id(args.machine_id),
                      args.min_mixed_sessions)
    if args.command == "plan":
        print_plan(plan, time.time())
        return
    if not 1 <= args.io_max_mib <= 4096:
        raise ValueError("--io-max-mib must be between 1 and 4096")
    args.io_dir = (args.io_dir or args.root / "io_scratch").expanduser().resolve()
    args.raw_dir = (args.root / "_standin_raw" if args.dry_run else args.raw_dir.expanduser()).resolve()
    args.collector = args.collector.expanduser().resolve()
    if args.validator:
        args.validator = args.validator.expanduser().resolve()
    if args.dry_run and not args.io_dir.is_relative_to(args.root):
        raise ValueError("Dry-run I/O path must be inside its campaign root")
    if args.command != "doctor" and not args.dry_run and sys.platform != "linux":
        raise RuntimeError("Run real campaigns inside the Ubuntu VMware guest")
    if args.command == "doctor":
        if not doctor(args):
            raise SystemExit(1)
        return
    cfg = args.thresholds or args.root / "thresholds.json"
    if args.dry_run and not cfg.exists() and args.thresholds is None:
        cfg = HERE / "thresholds.example.json"
    if not cfg.exists() and (args.command == "campaign" or args.thresholds is not None):
        raise ValueError("Pass the team's frozen global --thresholds thresholds.json")
    config = thresholds(cfg) if cfg.exists() else {}
    if args.command == "campaign":
        previous = args.root / "campaign_state.json"
        start = load_json(previous)["start_unix"] if previous.exists() else time.time()
        print_plan(plan, start)
    with lock(args.root / ".campaign.lock"):
        if not args.dry_run and not doctor(args):
            raise SystemExit(1)
        machine = setup(args, plan, config)
        reconcile(args.root)
        if args.command == "calibrate":
            result = calibrate(args, machine, config)
            if any("error" in item for item in result.values()):
                raise SystemExit("One or more resources failed calibration. See calibration/pilot.json.")
        else:
            campaign(args, plan, machine, config)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: signal.raise_signal(signal.SIGINT))
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        sys.exit(str(error))
