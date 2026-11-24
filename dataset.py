"""PSI labeling, verified machine archives and machine-level dataset splits."""

import csv
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from cpu_runner import CLOCK_COLUMN, NS, label_segment_horizons, read_segments
from workloads import RESOURCES

HORIZONS = (5, 10, 30)
FULL_RESOURCES = ("memory", "io")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def thresholds(path):
    values = load_json(path)
    valid = lambda value: type(value) in (int, float) and 0 < value <= 100
    if (not isinstance(values, dict) or set(values) - {*RESOURCES, "full"} or
            not all(valid(values.get(name)) for name in RESOURCES)):
        raise ValueError("thresholds.json requires percentages in (0, 100] for cpu, memory and io")
    full = values.get("full", {})
    if not isinstance(full, dict) or set(full) - set(FULL_RESOURCES) or not all(map(valid, full.values())):
        raise ValueError("Optional full thresholds must map memory and/or io to percentages in (0, 100]")
    return values


def psi_columns(path, kind="some", resources=RESOURCES, required=True):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        fields = csv.DictReader(handle).fieldnames or []
    if CLOCK_COLUMN not in fields:
        raise ValueError(f"Collector CSV is missing {CLOCK_COLUMN}: {path}")
    found = {}
    for name in resources:
        aliases = (name, "mem") if name == "memory" else (name,)
        candidates = [f"{alias}_psi_{kind}_total_us" for alias in aliases]
        candidates += [f"psi_{alias}_{kind}_total_us" for alias in aliases]
        found[name] = next((candidate for candidate in candidates if candidate in fields), None)
        if found[name] is None and required:
            raise ValueError(f"Collector CSV is missing {name} PSI {kind} total (microseconds): {path}")
    return found


def make_labels(source, output, resource, threshold, interval=1.0, kind="some"):
    if source.resolve() == output.resolve():
        raise ValueError("The labels must be written to a separate file")
    if not math.isfinite(interval) or interval <= 0 or not math.isfinite(threshold) or not 0 < threshold <= 100:
        raise ValueError("Use a positive finite sampling interval and a PSI threshold in (0, 100]")
    column = psi_columns(source, kind, (resource,))[resource]
    gap = int(interval * 1.5 * NS)
    counts = {str(horizon): 0 for horizon in HORIZONS}
    counts["onsets"] = 0
    counts.update(samples=0, valid_intervals=0, segments=0, calm_samples=0,
                  horizons={str(h): {"positive": 0, "negative": 0, "blank": 0} for h in HORIZONS})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    try:
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([CLOCK_COLUMN, f"{resource}_psi_{kind}_interval_pct", "calm",
                             *(f"onset_next_{h}s" for h in HORIZONS),
                             "onset", "onset_interval_start_ns"])
            for segment in read_segments(source, column, gap):
                counts["segments"] += 1
                for stamp, pressure, calm, targets, onset, start in label_segment_horizons(
                        segment, threshold, 2 * NS, 3 * NS, tuple(h * NS for h in HORIZONS), gap):
                    writer.writerow([stamp, pressure, calm, *targets, onset, start])
                    counts["samples"] += 1
                    counts["valid_intervals"] += pressure is not None
                    counts["calm_samples"] += calm
                    counts["onsets"] += onset
                    for horizon, target in zip(HORIZONS, targets):
                        counts[str(horizon)] += target == 1
                        counts["horizons"][str(horizon)][
                            "blank" if target is None else "positive" if target else "negative"] += 1
        if not counts["valid_intervals"]:
            raise ValueError("Collector CSV contains no usable sampling intervals")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return counts


def hash_stream(handle):
    return hashlib.file_digest(handle, "sha256").hexdigest()


def digest(path):
    with path.open("rb") as handle:
        return hash_stream(handle)


def completed(record):
    return (record.get("status") == "complete" and record.get("quality") in {"ok", "low_signal"}
            and not record.get("pilot") and record.get("category") != "calibration")


def run_label_names(run_id, category, config):
    resources = RESOURCES if category == "mixed" else (category,)
    names = {f"{run_id}_{name}.csv" for name in resources}
    names.update(f"{run_id}_{name}_full.csv" for name in resources if name in config.get("full", {}))
    return names


def package(root, destination=None):
    machine = load_json(root / "machine.json")
    plan = load_json(root / "campaign_plan.json")
    state = load_json(root / "campaign_state.json")
    run_dir = root / "runs"
    records = [load_json(path) for path in sorted(run_dir.glob("*/metadata.json"))]
    by_id = {record["run_id"]: record for record in records}
    eligible = {run_id for run_id, record in by_id.items() if completed(record)}
    destination = destination or root / (
        f"aios_{machine['machine_id']}_{plan['profile']}_{state['date']}.zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    names = ["machine.json", "campaign_plan.json", "campaign_state.json", "thresholds.json"]
    files = [root / name for name in names]
    for folder in ("runs", "calibration"):
        files.extend(path for path in sorted((root / folder).rglob("*"))
                     if path.is_file() and not path.is_symlink()
                     and path.name not in {"session_spec.json", "cleanup.json", ".session.lock"}
                     and path.suffix != ".tmp")
    config = thresholds(root / "thresholds.json")
    for run_id in sorted(eligible):
        for name in sorted(run_label_names(run_id, by_id[run_id]["category"], config)):
            path = root / "labels" / name
            if not path.is_file():
                raise ValueError(f"Completed run is missing a label file: {path}")
            files.append(path)
    report = ["# AIOS pressure data", "", f"Completed sessions eligible for merging: {len(eligible)}.",
              "Incomplete sessions retain available diagnostics and are excluded from labeling and merging.",
              "", "| Run | Category | Status | Quality |", "| --- | --- | --- | --- |"]
    planned_ids = {item["run_id"] for item in plan["sessions"]}
    for item in plan["sessions"]:
        rec = by_id.get(item["run_id"], state["sessions"].get(item["run_id"], {}))
        report.append(f"| {item['run_id']} | {item['category']} | "
                      f"{rec.get('status', 'not started')} | {rec.get('quality', '—')} |")
    for run_id, record in by_id.items():
        if run_id not in planned_ids:
            report.append(f"| {run_id} | {record['category']} | {record['status']} | {record['quality']} |")
    contents = ("\n".join(report) + "\n").encode("utf-8")
    temporary = destination.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            manifest = []
            for path in files:
                member = path.relative_to(root).as_posix()
                hasher = hashlib.sha256()
                with path.open("rb") as original, archive.open(member, "w", force_zip64=True) as target:
                    while chunk := original.read(1024 ** 2):
                        hasher.update(chunk)
                        target.write(chunk)
                manifest.append(f"{hasher.hexdigest()}  {member}\n")
            archive.writestr("CONTENTS.md", contents)
            manifest.append(f"{hashlib.sha256(contents).hexdigest()}  CONTENTS.md\n")
            archive.writestr("MANIFEST.sha256", "".join(sorted(manifest)))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def safe_component(value):
    return (isinstance(value, str) and 0 < len(value) <= 100 and value.isascii()
            and all(char.isalnum() or char in "_-" for char in value))


def verified_archive(archive):
    names = set()
    for item in archive.infolist():
        name = item.filename
        path = PurePosixPath(name)
        if (name != path.as_posix() or path.is_absolute() or any(part in {"..", "."} for part in path.parts)
                or "\\" in name or ":" in name or name in names or item.is_dir()
                or stat.S_ISLNK(item.external_attr >> 16)):
            raise ValueError(f"Unsafe or duplicate ZIP member: {name}")
        names.add(name)
    manifest = {}
    for line in archive.read("MANIFEST.sha256").decode("utf-8").splitlines():
        checksum, name = line.split("  ", 1)
        if name in manifest:
            raise ValueError(f"Duplicate manifest member: {name}")
        manifest[name] = checksum
    if names - {"MANIFEST.sha256"} != manifest.keys():
        raise ValueError("ZIP contents and MANIFEST.sha256 disagree")
    for name, checksum in manifest.items():
        with archive.open(name) as handle:
            if hash_stream(handle) != checksum:
                raise ValueError(f"Checksum mismatch: {name}")
    return names


def machine_split(machine_id, overrides):
    if machine_id in overrides:
        return overrides[machine_id]
    value = int.from_bytes(hashlib.sha256(machine_id.encode()).digest()[:8], "big") / 2 ** 64
    return "train" if value < .70 else "validation" if value < .85 else "test"


def merge(archives, output, overrides):
    if output.exists():
        raise ValueError(f"Dataset output already exists: {output}")
    if not isinstance(overrides, dict) or any(not safe_component(key) or value not in
            {"train", "validation", "test"} for key, value in overrides.items()):
        raise ValueError("Split override must map machine IDs to train, validation or test")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".aios-merge-", dir=output.parent))
    machines = set()
    seeds = set()
    ids = set()
    reference = None
    simulated = None
    splits = []
    try:
        for source in archives:
            with zipfile.ZipFile(source) as archive:
                names = verified_archive(archive)
                machine = json.loads(archive.read("machine.json"))
                machine_id = machine["machine_id"]
                if not safe_component(machine_id) or machine_id in machines:
                    raise ValueError(f"Invalid or repeated machine ID: {machine_id}")
                machines.add(machine_id)
                if simulated is None:
                    simulated = bool(machine.get("dry_run"))
                elif simulated != bool(machine.get("dry_run")):
                    raise ValueError("Simulation archives cannot be combined with real machine data")
                config = json.loads(archive.read("thresholds.json"))
                if reference is None:
                    reference = config
                    (temp / "thresholds.json").write_bytes(archive.read("thresholds.json"))
                elif config != reference:
                    raise ValueError(f"Machine {machine_id} uses different frozen thresholds")
                namespace = temp / "machines" / machine_id
                namespace.mkdir(parents=True)
                (namespace / "machine.json").write_bytes(archive.read("machine.json"))
                for name in sorted(names):
                    parts = PurePosixPath(name).parts
                    if len(parts) != 3 or parts[0] != "runs" or parts[2] != "metadata.json":
                        continue
                    record = json.loads(archive.read(name))
                    if not completed(record):
                        continue
                    run_id = parts[1]
                    seed = record["seed"]
                    category = record["category"]
                    if (not safe_component(run_id) or record["run_id"] != run_id or
                            record["machine_id"] != machine_id or run_id in ids or seed in seeds or
                            category not in (*RESOURCES, "mixed")):
                        raise ValueError(f"Conflicting or invalid run/seed: {name}")
                    ids.add(run_id)
                    seeds.add(seed)
                    label_names = {f"labels/{name}" for name in run_label_names(run_id, category, config)}
                    if not label_names | {f"runs/{run_id}/system.csv"} <= names:
                        raise ValueError(f"Completed run is missing telemetry or labels: {run_id}")
                    for member in sorted(label_names | {p for p in names if p.startswith(f"runs/{run_id}/")}):
                        target = namespace.joinpath(*PurePosixPath(member).parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as original, target.open("xb") as copy:
                            shutil.copyfileobj(original, copy)
                    splits.append([machine_id, run_id, seed, category, record["quality"],
                                   machine_split(machine_id, overrides)])
        with (temp / "splits.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["machine_id", "run_id", "seed", "category", "quality", "split"])
            writer.writerows(sorted(splits))
        os.rename(temp, output)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return len(splits)
