"""Linux workload commands, schedules and resource safeguards."""

import os
from pathlib import Path
import random
import shutil
import stat

from cpu_runner import schedule

GIB = 1024 ** 3
MIB = 1024 ** 2
RESOURCES = ("cpu", "memory", "io")


def read_pairs(path):
    return {line.split()[0].rstrip(":"): int(line.split()[1])
            for line in Path(path).read_text().splitlines() if len(line.split()) >= 2}


def psi_avg10(resource):
    lines = Path(f"/proc/pressure/{resource}").read_text().splitlines()
    return float(lines[0].split()[1].split("=")[1])


def backing_disk(directory):
    target = directory.resolve()
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields = before.split()
        mountpoint = Path(fields[4].replace(r"\040", " ").replace(r"\134", "\\"))
        if target.is_relative_to(mountpoint):
            fs, source, _ = after.split(maxsplit=2)
            mounts.append((len(mountpoint.parts), fs, source, fields[2]))
    _, fs, source, device = max(mounts, key=lambda mount: mount[0])
    block = Path("/sys/dev/block", device).exists()
    if source.startswith("/dev/") and Path(source).exists():
        block |= stat.S_ISBLK(Path(source).stat().st_mode)
    return fs, source, block


class MemoryWatchdog:
    def __init__(self, page_size):
        self.page_size = page_size
        self.previous = None
        self.swap_streak = 0

    def check(self, available, total, swap_in, now):
        if self.previous is not None:
            old_swap, old_time = self.previous
            elapsed = now - old_time
            if elapsed >= 0.9:
                rate = (swap_in - old_swap) * self.page_size / elapsed
                self.swap_streak = self.swap_streak + 1 if rate >= 64 * MIB else 0
                self.previous = swap_in, now
        else:
            self.previous = swap_in, now
        if available < total * 0.15:
            return "MemAvailable below 15% of MemTotal"
        if self.swap_streak >= 3:
            return "swap-in reached 64 MiB/s for three consecutive intervals"
        return None


def graded_schedule(resource, seed, seconds, cpus, ceiling=3, calibration=False, tuning=3):
    def settings(level, rng):
        if resource == "cpu":
            return [{"workers": (cpus, cpus, cpus * 2)[level - 1],
                     "load": (30, 90, 100)[level - 1], "nice": 0}]
        if resource == "memory":
            high, ratio = ((0.25, 0.8), (0.40, 1.10), (0.60, 1.50))[level - 1]
            return [{"high_fraction": high, "target_ratio": ratio, "workers": 1}]
        # Read/write configs keep all jobs inside one bounded test file.
        seed = rng.randrange(1, 2 ** 32)
        if calibration:
            return [{"pattern": ("randread", "randwrite", "randrw")[level - 1],
                     "bs": ("4k", "64k", "1m")[level - 1], "depth": (1, 4, 32)[level - 1],
                     "jobs": (1, 1, 2)[level - 1], "rate_mib": (8, 40, 0)[level - 1], "seed": seed}]
        options = ((1, 4), (4, 16), (16, 32))[level - 1]
        return [{"pattern": rng.choice(("randread", "randwrite", "randrw")),
                 "bs": rng.choice(("4k", "64k", "1m")), "depth": rng.choice(options),
                 "jobs": (1, 1, 2)[level - 1], "rate_mib": (8, 40, 0)[level - 1], "seed": seed}]

    rng = random.Random(seed)
    if calibration:
        baseline = min(15, seconds // 4)
        length, remainder = divmod(seconds - baseline, 3)
        return [("calibration_baseline", baseline, [])] + [
            (f"calibration_{level}", length + (remainder if level == 3 else 0), settings(level, rng))
            for level in (1, 2, 3)]
    phases = []
    for pattern, duration, groups in schedule(seed, seconds, cpus):
        if not groups:
            params = []
        elif resource == "cpu" and ceiling == 3 and tuning == 3:
            params = [dict(workers=w, load=load, nice=nice) for w, load, nice in groups]
        elif resource == "cpu" and ceiling < 3:
            params = [dict(workers=min(w, cpus), load=min(load, 85), nice=max(nice, 5))
                      for w, load, nice in groups[:1]]
        else:
            requested = 1 + (groups[0][1] >= 60) + (groups[0][1] >= 95)
            if ceiling == 3 and (tuning == 1 or (tuning == 2 and requested == 3)):
                requested = max(1, requested - 1)
            level = min(ceiling, requested)
            params = settings(level, rng)
            # Repeat the configuration throughout a periodic block.
            if pattern == "periodic" and len(phases) >= 2 and phases[-2][0] == pattern:
                params = phases[-2][2]
        phases.append((pattern, duration, params))
    return phases


def cpu_command(stress, params, seconds):
    return ["nice", "-n", str(params["nice"]), stress, "--cpu", str(params["workers"]),
            "--cpu-load", str(params["load"]), "--cpu-load-slice", "10", "--cpu-method",
            "sqrt", "--timeout", f"{seconds}s"]


def memory_command(stress, unit, params, total_bytes, available_bytes, seconds, user):
    high = int(total_bytes * params["high_fraction"])
    headroom = available_bytes - int(total_bytes * 0.15)
    max_bytes = min(int(total_bytes * 0.70), high + max(64 * MIB, high // 5), headroom)
    if max_bytes < min(high, int(high * params["target_ratio"])) + 32 * MIB:
        return None
    target = int(high * params["target_ratio"] / params["workers"])
    return ["sudo", "-n", "systemd-run", "--quiet", "--scope", f"--unit={unit}",
            f"--property=MemoryHigh={high}", f"--property=MemoryMax={max_bytes}",
            "--property=MemorySwapMax=0", f"--property=RuntimeMaxSec={seconds + 8}s",
            "runuser", "-u", user, "--", stress, "--vm", str(params["workers"]),
            "--vm-bytes", str(target), "--vm-keep", "--vm-method", "write64",
            "--timeout", f"{seconds}s"]


def fio_command(fio, path, size, params, seconds):
    command = [fio, "--name=aios", f"--filename={path}", f"--size={size}", "--allow_file_create=0",
               "--direct=1", "--ioengine=libaio", "--time_based=1", f"--runtime={seconds}",
               f"--rw={params['pattern']}", f"--bs={params['bs']}",
               f"--iodepth={params['depth']}", f"--numjobs={params['jobs']}",
               f"--randseed={params['seed']}", "--group_reporting=1", "--eta=never"]
    if params["rate_mib"]:
        rate = f"{params['rate_mib']}m"
        command.append(f"--rate={rate},{rate}" if params["pattern"] == "randrw" else f"--rate={rate}")
    return command


def io_file_size(free, cap_bytes=4 * GIB):
    return min(cap_bytes, 4 * GIB, free // 10) // MIB * MIB


def prepare_io_file(path, cap_bytes=4 * GIB, cancelled=None):
    # A bounded, initialized file gives reads written extents even on thin VM disks.
    fs, _, block = backing_disk(path.parent)
    if not block or fs in {"tmpfs", "ramfs", "devtmpfs"}:
        raise RuntimeError("I/O workspace must be on a block-backed filesystem")
    free = shutil.disk_usage(path.parent).free
    size = io_file_size(free, cap_bytes)
    if size < MIB or free < 5 * GIB:
        raise RuntimeError("I/O workspace needs at least 5 GiB free")
    with path.open("xb") as handle:
        for _ in range(size // MIB):
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("campaign supervisor stopped during I/O preparation")
            handle.write(os.urandom(MIB))
        handle.flush()
        os.fsync(handle.fileno())
    return size
