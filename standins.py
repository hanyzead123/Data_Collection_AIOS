"""Bounded process and collector stand-ins used by the accelerated campaign."""

import argparse
import csv
import json
import os
from pathlib import Path
import signal
import sys
import time


def collect():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", required=True, type=int)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    directory = Path(os.environ["AIOS_STANDIN_RAW"])
    source = directory / f"system_{time.time_ns()}.csv"
    metadata = {"experiment_id": args.experiment_id, "status": "running", "standin": True}
    meta_path = source.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    resources = ("cpu", "memory", "io")
    started = before = time.monotonic_ns()
    counters = [0, 0, 0]
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["monotonic_timestamp_ns", *(f"{name}_psi_some_total_us" for name in resources),
                         "memory_psi_full_total_us", "io_psi_full_total_us"])
        iteration = 0
        while running and time.monotonic_ns() - started < args.duration * 10 ** 9:
            stamp = time.monotonic_ns()
            for index in range(3):
                if (iteration + index * 2) % 13 >= 4:
                    counters[index] += (stamp - before) * (index + 2) // 20_000
            writer.writerow([stamp, *counters, counters[1] // 2, counters[2] // 2])
            handle.flush()
            before = stamp
            iteration += 1
            time.sleep(.04)
    metadata["status"] = "complete"
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] in ("stress-ng", "fio"):
        time.sleep(min(float(sys.argv[3]) * .01 + 4, 20))
    elif sys.argv[1] == "validator":
        with Path(sys.argv[2]).open(newline="", encoding="utf-8") as stream:
            assert next(csv.DictReader(stream))["monotonic_timestamp_ns"]
        print("stand-in validator passed")
    else:
        collect()
