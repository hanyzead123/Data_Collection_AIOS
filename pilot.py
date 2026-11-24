"""Compare sustained PSI some/full across pilot machines."""

import csv
from cpu_runner import CLOCK_COLUMN, NS, read_segments
from dataset import FULL_RESOURCES, psi_columns, load_json
from workloads import RESOURCES

GRID = (.1, .2, .5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 50)


def phase_intervals(directory, resource):
    phases = []
    start = None
    with (directory / "events.csv").open(newline="", encoding="utf-8") as handle:
        for item in csv.DictReader(handle):
            if item["resource"] != resource:
                continue
            stamp = int(item[CLOCK_COLUMN])
            if item["event"] == "phase_start":
                start = item["phase"], stamp
            if start and item["event"] in {"phase_end", "watchdog_stop"} and item["phase"] == start[0]:
                phases.append((start[0].split("_")[-1], start[1], stamp,
                               item["event"] == "watchdog_stop"))
                start = None
    return phases


def signal_evidence(samples, threshold):
    span = 0
    for duration, pressure in samples:
        span = span + duration if pressure is not None and pressure >= threshold else 0
        if span >= 2 * NS:
            return True
    return False


def measure(directory, resource, interval, candidates=GRID):
    source = directory / "system.csv"
    report = {}
    phases = phase_intervals(directory, resource)
    for kind in ("some", "full") if resource in FULL_RESOURCES else ("some",):
        column = psi_columns(source, kind, (resource,), required=kind == "some")[resource]
        if column is None:
            continue
        segments = list(read_segments(source, column, int(1.5 * interval * NS)))
        observed = []
        for intensity, start, end, watchdog in phases:
            samples = []
            for segment in segments:
                subset = [(t - previous, value)
                          for (previous, _), (t, value) in zip(segment, segment[1:])
                          if start <= previous and t <= end]
                if subset:
                    samples.extend([(0, None), *subset])
            values = [value for _, value in samples if value is not None]
            observed.append({"intensity": intensity, "samples": len(values),
                             "mean_stall_pct": sum(values) / len(values) if values else None,
                             "max_stall_pct": max(values, default=None), "watchdog_stop": watchdog,
                             "sustained_thresholds": [t for t in candidates if signal_evidence(samples, t)]})
        baseline = next((phase for phase in observed if phase["intensity"] == "baseline"), {})
        intensities = [phase for phase in observed if phase["intensity"] != "baseline"]
        mean = baseline.get("mean_stall_pct")
        peak = max((phase["mean_stall_pct"] or 0 for phase in intensities), default=0)
        report[kind] = {"baseline": baseline, "intensities": intensities,
                        "measurable": mean is not None and peak >= mean + .1}
    return report


def assess(report, threshold):
    some = report.get("some", {})
    levels = [int(phase["intensity"]) for phase in some.get("intensities", [])
              if not phase["watchdog_stop"] and threshold in phase["sustained_thresholds"]]
    quiet = threshold in some.get("baseline", {}).get("sustained_thresholds", [])
    ok = some.get("measurable", False) and bool(levels) and not quiet
    return {"ok": ok, "level": min(levels) if ok else 3, "quiet_pressure": quiet}


def compare(roots):
    pilots = {}
    dry_modes = set()
    for root in roots:
        path = root / "calibration" / "pilot.json" if root.is_dir() else root
        record = load_json(path)
        machine = record["machine_id"]
        if machine in pilots:
            raise ValueError(f"Duplicate pilot machine ID: {machine}")
        pilots[machine] = record["resources"]
        dry_modes.add(bool(record["dry_run"]))
    if len(dry_modes) > 1:
        raise ValueError("Compare real and simulated pilots separately")
    proposed = {}
    evidence = {}
    for name in RESOURCES:
        for kind in ("some", "full") if name in FULL_RESOURCES else ("some",):
            key = f"{name}_{kind}"
            evidence[key] = {}
            for machine, resources in pilots.items():
                group = resources.get(name, {}).get(kind, {})
                baseline = group.get("baseline", {})
                supported = set()
                if group.get("measurable", False) and baseline.get("samples"):
                    for phase in group.get("intensities", []):
                        if not phase["watchdog_stop"]:
                            supported.update(phase["sustained_thresholds"])
                    supported.difference_update(baseline["sustained_thresholds"])
                evidence[key][machine] = sorted(supported.intersection(GRID))
            shared = set.intersection(*(set(supported) for supported in evidence[key].values()))
            if shared:
                if kind == "some":
                    proposed[name] = min(shared)
                else:
                    proposed.setdefault("full", {})[name] = min(shared)
    return {"machine_ids": list(pilots), "simulated": True in dry_modes,
            "candidate_thresholds_pct": list(GRID), "supported_by_machine": evidence,
            "suggested_thresholds": proposed, "pilot_measurements": pilots}
