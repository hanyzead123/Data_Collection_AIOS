"""Schedule CPU experiments and derive onset labels from the AIOS collector."""

import argparse
import bisect
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid


NS = 1_000_000_000
CLOCK_COLUMN = "monotonic_timestamp_ns"
PSI_COLUMN = "cpu_psi_some_total_us"


def schedule(seed, seconds, cpus):
    rng = random.Random(seed)
    phases = [("baseline", rng.randint(24, 44), ())]
    recovery = rng.randint(22, 36)
    patterns = []
    remaining = seconds - phases[0][1] - recovery
    while remaining:
        if not patterns:
            patterns = ["ramp", "periodic", "random", "mixed"]
            rng.shuffle(patterns)
        pattern = patterns.pop()
        nice = rng.choice((0, 5, 10))
        rest = rng.randint(5, 15) if pattern == "periodic" else rng.randint(6, 13)
        block = [(f"{pattern}_gap", rest, ())]
        if pattern == "ramp":
            for load, workers in [(30, cpus), (60, cpus), (90, cpus), (100, cpus * 2)]:
                block.append(("ramp", rng.randint(5, 9), ((workers, load, nice),)))
        elif pattern == "mixed":
            block.append(("mixed", rng.randint(7, 17),
                          ((cpus, 90, 0), (cpus, rng.choice((60, 90, 100)), 10))))
        else:
            burst = rng.randint(5, 10)
            for index in range(3 if pattern == "periodic" else 2):
                if index:
                    block.append((f"{pattern}_gap", rest if pattern == "periodic"
                                  else rng.randint(6, 17), ()))
                workers = rng.choice((cpus, cpus * 2))
                load = rng.choice((30, 60, 90, 100))
                if pattern == "periodic" and index:
                    workers, load, _ = block[-2][2][0]
                duration = burst if pattern == "periodic" else rng.randint(5, 15)
                block.append((pattern, duration, ((workers, load, nice),)))
        span = sum(phase[1] for phase in block)
        if span > remaining:
            phases.append(("quiet", remaining, ()))
            break
        phases.extend(block)
        remaining -= span
    phases.append(("recovery", recovery, ()))
    return phases


def save_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    if sys.platform == "linux":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def stop_group(process, first_signal, timeout=3):
    try:
        os.killpg(process.pid, first_signal)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        forced = True
    else:
        forced = False
    # Clean up remaining children even if the group leader exited first.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    return forced


def wait_for_collector(path, raw_dir, existing, collector, pressure_column, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if collector.poll() is not None:
            raise RuntimeError("Collector exited during startup. See collector.log.")
        if raw_dir is not None:
            new_files = sorted(set(raw_dir.glob("system_*.csv")) - existing)
            if len(new_files) > 1:
                raise RuntimeError("Multiple new collector CSVs appeared. Run one collector at a "
                                   "time or use its explicit output option with {csv}.")
            path = new_files[0] if new_files else None
        if path is not None and path.exists():
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                row = next(reader, None)
                if row is not None:
                    try:
                        stamp = int(row[CLOCK_COLUMN])
                        int(row[pressure_column])
                    except (KeyError, TypeError, ValueError) as error:
                        raise ValueError(f"Expected integer {CLOCK_COLUMN} and {pressure_column}. "
                                         f"Collector columns: {reader.fieldnames}") from error
                    if not 0 <= time.monotonic_ns() - stamp <= 5 * NS:
                        raise ValueError("Collector timestamps must use the VM's monotonic clock "
                                         "and be flushed within five seconds.")
                    return path, reader.fieldnames
        time.sleep(0.1)
    raise RuntimeError(f"Collector did not write a data row in {raw_dir or path}. "
                       "Check the collector command and collector.log.")


def run(args):
    if sys.platform != "linux":
        raise ValueError("Run the experiment inside the Ubuntu VM.")
    if not args.collector:
        raise ValueError("Provide your existing collector command after --.")
    stress = shutil.which("stress-ng")
    nice_command = shutil.which("nice")
    if not stress or not nice_command:
        raise ValueError("Install stress-ng and GNU coreutils in the VM.")
    cpus = len(os.sched_getaffinity(0))
    phases = schedule(args.seed, args.minutes * 60, cpus)
    run_id = uuid.uuid4().hex
    directory = args.output.resolve() / run_id
    directory.mkdir(parents=True)
    collector_csv = directory / "system.csv"
    explicit_path = any("{csv}" in item for item in args.collector)
    raw_dir = None if explicit_path else args.collector_raw_dir.expanduser().resolve()
    collector_duration = args.minutes * 60 + int(args.startup_timeout) + 45
    command = [item.replace("{csv}", str(collector_csv)).replace("{run_id}", run_id)
               .replace("{duration}", str(collector_duration))
               for item in args.collector]
    metadata = {
        "run_id": run_id, "seed": args.seed, "pilot": args.pilot, "status": "starting",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "kernel": os.uname().release, "available_vcpus": cpus,
        "host_notes": args.host_notes, "pressure_column": args.psi_column,
        "schedule": phases, "collector_command": command,
        "stress_ng_version": subprocess.check_output([stress, "--version"], text=True).strip(),
    }
    save_json(directory / "metadata.json", metadata)
    print(f"Run {run_id}: {directory}", flush=True)
    collector = None
    source_csv = None
    workers = []
    with (directory / "collector.log").open("w", encoding="utf-8") as collector_log, (
        directory / "stress-ng.log"
    ).open("w", encoding="utf-8") as stress_log, (
        directory / "events.csv"
    ).open("w", newline="", encoding="utf-8") as events_file, (
        directory / "probe.csv"
    ).open("w", newline="", encoding="utf-8") as probe_file:
        events = csv.writer(events_file)
        events.writerow([CLOCK_COLUMN, "event", "phase_index", "pattern", "detail"])
        probe = csv.writer(probe_file)
        probe.writerow([CLOCK_COLUMN, "wakeup_late_ns", "compute_wall_ns"])
        payload = bytes(64 * 1024)
        phase_index, pattern = "", ""

        def event(kind, detail="", timestamp=None):
            events.writerow([time.monotonic_ns() if timestamp is None else timestamp,
                             kind, phase_index, pattern, detail])
            events_file.flush()

        def boot_minus_monotonic():
            return time.clock_gettime_ns(time.CLOCK_BOOTTIME) - time.monotonic_ns()

        def interrupted(_signum, _frame):
            raise KeyboardInterrupt

        previous_term = signal.signal(signal.SIGTERM, interrupted)
        try:
            existing = set(raw_dir.glob("system_*.csv")) if raw_dir is not None else set()
            collector = subprocess.Popen(command, start_new_session=True,
                                         stdout=collector_log, stderr=subprocess.STDOUT)
            source_csv, columns = wait_for_collector(collector_csv, raw_dir, existing, collector,
                                                     args.psi_column, args.startup_timeout)
            metadata["collector_source_csv"] = str(source_csv)
            metadata["collector_steal_columns"] = [name for name in columns if "steal" in name.lower()]
            metadata["session_start_unix_ns"] = time.time_ns()
            metadata["session_start_monotonic_ns"] = time.monotonic_ns()
            metadata["status"] = "running"
            save_json(directory / "metadata.json", metadata)
            event("session_start", timestamp=metadata["session_start_monotonic_ns"])
            boot_offset = boot_minus_monotonic()
            next_probe = time.monotonic_ns() + NS
            for phase_index, (pattern, duration, groups) in enumerate(phases):
                phase_start = time.monotonic_ns()
                deadline = phase_start + duration * NS
                event("phase_start", json.dumps({"seconds": duration, "groups": [
                    {"workers": workers_count, "load_pct": load, "nice": nice}
                    for workers_count, load, nice in groups
                ]}), phase_start)
                for workers_count, load, nice in groups:
                    worker = subprocess.Popen(
                        [nice_command, "-n", str(nice), stress, "--cpu", str(workers_count),
                         "--cpu-load", str(load), "--cpu-load-slice", "10", "--cpu-method",
                         "sqrt", "--timeout", f"{duration + 3}s"],
                        stdout=stress_log, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                    workers.append(worker)
                    event("worker_launched", str(worker.pid))
                while time.monotonic_ns() < deadline:
                    now = time.monotonic_ns()
                    new_offset = boot_minus_monotonic()
                    if abs(new_offset - boot_offset) > NS // 2:
                        event("suspend_detected", str(new_offset - boot_offset), now)
                        raise RuntimeError("Guest clock indicated suspend. Run marked incomplete.")
                    if collector.poll() is not None:
                        raise RuntimeError("Collector stopped during the run. See collector.log.")
                    if any(worker.poll() is not None for worker in workers):
                        raise RuntimeError("Workload stopped early. See stress-ng.log.")
                    if now >= next_probe:
                        started = time.monotonic_ns()
                        hashlib.sha256(payload).digest()
                        probe.writerow([started, started - next_probe, time.monotonic_ns() - started])
                        probe_file.flush()
                        next_probe += (1 + (now - next_probe) // NS) * NS
                    time.sleep(max(0, min(deadline, next_probe, now + NS // 4)
                                   - time.monotonic_ns()) / NS)
                for worker in workers:
                    stop_group(worker, signal.SIGTERM)
                    event("worker_stopped", str(worker.pid))
                workers.clear()
                event("phase_end")
            if collector.poll() is not None:
                raise RuntimeError("Collector stopped before session completion. See collector.log.")
            metadata["status"] = "complete"
        except KeyboardInterrupt:
            metadata["status"] = "interrupted"
            raise
        except Exception as error:
            metadata.update(status="failed", error=str(error))
            raise
        finally:
            # Finish cleanup even if the user presses Ctrl+C again.
            previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                for worker in workers:
                    stop_group(worker, signal.SIGTERM)
                    event("worker_stopped", str(worker.pid))
                event("session_end", metadata["status"])
                if collector is not None:
                    metadata["collector_shutdown_timed_out"] = stop_group(
                        collector, signal.SIGINT, timeout=10)
                    metadata["collector_exit_code"] = collector.returncode
                if source_csv is not None and source_csv != collector_csv:
                    try:
                        shutil.copyfile(source_csv, collector_csv)
                    except OSError as error:
                        metadata.update(status="failed", error=str(error))
                        save_json(directory / "metadata.json", metadata)
                        raise
                save_json(directory / "metadata.json", metadata)
            finally:
                signal.signal(signal.SIGINT, previous_int)
                signal.signal(signal.SIGTERM, previous_term)


def read_segments(path, pressure_column, max_gap_ns):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not {CLOCK_COLUMN, pressure_column}.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV needs {CLOCK_COLUMN} and {pressure_column}.")
        segment = []
        previous = None
        for row in reader:
            stamp = int(row[CLOCK_COLUMN])
            counter = int(row[pressure_column])
            fraction = None
            if previous is not None:
                elapsed = stamp - previous[0]
                if elapsed <= 0:
                    raise ValueError("Collector timestamps must be strictly increasing within a run.")
                delta_us = counter - previous[1]
                if 0 <= delta_us and elapsed <= max_gap_ns:
                    fraction = delta_us * 100_000 / elapsed
            if fraction is None and segment:
                yield segment
                segment = []
            segment.append((stamp, fraction))
            previous = stamp, counter
        if segment:
            yield segment


def label_segment(segment, threshold, sustain_ns, calm_ns, horizon_ns, max_gap_ns):
    for stamp, pressure, calm, targets, onset, start in label_segment_horizons(
            segment, threshold, sustain_ns, calm_ns, (horizon_ns,), max_gap_ns):
        yield stamp, pressure, calm, targets[0], onset, start


def label_segment_horizons(segment, threshold, sustain_ns, calm_ns, horizons_ns, max_gap_ns):
    """Share onset detection and searches across horizons; stream output rows."""
    stamps = [stamp for stamp, _ in segment]
    calm_start = None
    high_start = None
    eligible = []
    onset_intervals = {}
    for index, (stamp, stall_pct) in enumerate(segment):
        if stall_pct is None:
            eligible.append(False)
            continue
        if stall_pct >= threshold:
            if high_start is None:
                high_start = index
            start, end = stamps[high_start - 1:high_start + 1]
            if stamp - start >= sustain_ns:
                onset_intervals[end] = start
            calm_start = None
        else:
            if calm_start is None:
                calm_start = stamps[index - 1]
            high_start = None
        eligible.append(calm_start is not None and stamp - calm_start >= calm_ns)

    onset_starts = list(onset_intervals.values())
    onset_ends = list(onset_intervals)
    for (stamp, stall_pct), is_calm in zip(segment, eligible):
        onset_index = bisect.bisect_left(onset_starts, stamp)
        targets = []
        for horizon_ns in horizons_ns:
            target = None
            if is_calm and stamp + horizon_ns + sustain_ns + max_gap_ns <= stamps[-1]:
                if onset_index == len(onset_starts) or onset_starts[onset_index] > stamp + horizon_ns:
                    target = 0
                elif stamp <= onset_starts[onset_index] and onset_ends[onset_index] <= stamp + horizon_ns:
                    target = 1
                # Leave the target blank when an onset interval crosses a window boundary.
            targets.append(target)
        onset_start = onset_intervals.get(stamp)
        yield stamp, stall_pct, int(is_calm), targets, int(onset_start is not None), onset_start


def label(args):
    if args.output.resolve() == args.system_csv.resolve():
        raise ValueError("The labels must be written to a separate file.")
    max_gap = args.max_gap if args.max_gap is not None else 1.5 * args.interval
    max_gap_ns = int(max_gap * NS)
    segments = list(read_segments(args.system_csv, args.psi_column, max_gap_ns))
    if not segments:
        raise ValueError("Collector CSV contains no samples.")
    counts = {"onsets": 0, "calm_samples": 0, "positive": 0, "negative": 0}
    with args.output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([CLOCK_COLUMN, "cpu_psi_some_interval_pct", "calm", "onset_next_5s",
                         "onset", "onset_interval_start_ns"])
        for segment in segments:
            for row in label_segment(segment, args.threshold, int(args.sustain * NS),
                                     int(args.calm * NS), 5 * NS, max_gap_ns):
                writer.writerow(row)
                _, _, calm, target, onset, _ = row
                counts["onsets"] += onset
                counts["calm_samples"] += calm
                if target is not None:
                    counts["positive" if target else "negative"] += 1
    print(json.dumps({"threshold_pct": args.threshold, "sustain_s": args.sustain,
                      "calm_s": args.calm, "max_gap_s": max_gap, **counts}))


def split(args):
    validation = set(args.validation_seed)
    test = set(args.test_seed)
    if validation & test:
        raise ValueError("Validation and test seeds must be disjoint.")
    rows = []
    for path in sorted(args.root.glob("*/metadata.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["status"] != "complete" or record["pilot"]:
            continue
        seed = record["seed"]
        subset = "test" if seed in test else "validation" if seed in validation else "train"
        rows.append((record["run_id"], seed, subset))
    if {row[2] for row in rows} != {"train", "validation", "test"}:
        raise ValueError("Each split needs at least one completed, non-pilot run.")
    with args.output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["run_id", "seed", "split"])
        writer.writerows(rows)


def positive_float(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("Expected a finite positive value.")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    runner = commands.add_parser("run", help="run the scheduled CPU experiment")
    runner.add_argument("--seed", type=int, required=True)
    runner.add_argument("--minutes", type=int, choices=range(2, 31), default=15, metavar="2..30")
    runner.add_argument("--pilot", action="store_true")
    runner.add_argument("--output", type=Path, default=Path("runs"))
    runner.add_argument("--host-notes", default="unrecorded")
    runner.add_argument("--psi-column", default=PSI_COLUMN)
    runner.add_argument("--collector-raw-dir", type=Path, default=Path("~/aios_data/raw"),
                        help="directory to watch when the collector command omits {csv}")
    runner.add_argument("--startup-timeout", type=positive_float, default=30.0)
    runner.set_defaults(action=run)

    labeller = commands.add_parser("label", help="derive calm-only 5-second onset labels")
    labeller.add_argument("system_csv", type=Path)
    labeller.add_argument("output", type=Path)
    labeller.add_argument("--threshold", type=positive_float, required=True, help="stall percentage")
    labeller.add_argument("--psi-column", default=PSI_COLUMN)
    labeller.add_argument("--sustain", type=positive_float, default=2.0)
    labeller.add_argument("--calm", type=positive_float, default=3.0)
    labeller.add_argument("--interval", type=positive_float, default=1.0,
                          help="collector sampling interval in seconds")
    labeller.add_argument("--max-gap", type=positive_float,
                          help="gap cutoff in seconds (default: 1.5 times --interval)")
    labeller.set_defaults(action=label)

    splitter = commands.add_parser("split", help="assign complete runs by seed")
    splitter.add_argument("root", type=Path)
    splitter.add_argument("output", type=Path)
    splitter.add_argument("--validation-seed", type=int, nargs="+", required=True)
    splitter.add_argument("--test-seed", type=int, nargs="+", required=True)
    splitter.set_defaults(action=split)

    options = sys.argv[1:]
    if options[:1] == ["run"] and "--" in options:
        separator = options.index("--")
        args = parser.parse_args(options[:separator])
        args.collector = options[separator + 1:]
    else:
        args = parser.parse_args(options)
        args.collector = []
    if args.action is run and args.minutes < 10 and not args.pilot:
        parser.error("Production runs are 10–30 minutes. Use --pilot for a shorter run.")
    if args.action is label and args.threshold > 100:
        parser.error("--threshold must be at most 100 percent.")
    try:
        args.action(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Experiment interrupted. Partial logs were retained.\n")


if __name__ == "__main__":
    main()
