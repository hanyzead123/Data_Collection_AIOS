"""Unit tests plus a short-profile campaign using stand-in subprocesses."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from aios_campaign import build_plan, campaign, derived_seed, setup
from cpu_runner import CLOCK_COLUMN, NS, save_json
from dataset import digest, load_json, machine_split, make_labels, merge, package, verified_archive
from pilot import assess, compare
from workloads import GIB, MemoryWatchdog, MIB, RESOURCES, graded_schedule, io_file_size, memory_command

ENTRY = Path(__file__).with_name("aios_campaign.py").resolve()


class PlanningTests(unittest.TestCase):
    def test_profiles_and_order(self):
        short = build_plan("short", None, "vm")
        self.assertEqual(list(short["counts"].values()), [3, 2, 2, 2])
        self.assertEqual(list(build_plan("long", None, "vm")["counts"].values()), [14, 10, 10, 6])
        self.assertEqual(list(build_plan("short", 1, "vm")["counts"].values()), [1, 1, 1, 1])
        self.assertEqual([item["category"] for item in short["sessions"]],
                         ["cpu"] * 3 + ["memory"] * 2 + ["io"] * 2 + ["mixed"] * 2)
        with self.assertRaises(ValueError):
            build_plan("short", .25, "vm")

    def test_seed_uniqueness_and_schedule_duration(self):
        seeds = [item["seed"] for machine in range(50) for item in
                 build_plan("long", None, f"vm-{machine}")["sessions"]]
        self.assertEqual(len(seeds), len(set(seeds)))
        rests = set()
        for resource in RESOURCES:
            for seed in range(45):
                for length in (600, 900):
                    planned = graded_schedule(resource, seed, length, 4)
                    self.assertEqual(planned, graded_schedule(resource, seed, length, 4))
                    self.assertEqual(sum(part[1] for part in planned), length)
                    i = 0
                    while i < len(planned):
                        if planned[i][0] == "periodic_gap":
                            block = planned[i:i + 6]
                            self.assertEqual([phase[0] for phase in block], ["periodic_gap", "periodic"] * 3)
                            self.assertEqual(block[0][1], block[2][1])
                            self.assertEqual(block[0][1], block[4][1])
                            self.assertEqual(block[1][2], block[3][2])
                            rests.add(block[0][1])
                            i += 6
                        else:
                            i += 1
        self.assertEqual(rests, set(range(5, 16)))
        tracks = [graded_schedule(name, derived_seed(17, name), 600, 4, 2) for name in RESOURCES]
        self.assertEqual(len({tuple(phase[1] for phase in track) for track in tracks}), 3)

    def test_machine_split_override(self):
        self.assertEqual(machine_split("vm", {"vm": "test"}), "test")
        self.assertEqual(machine_split("vm", {}), machine_split("vm", {}))

    def test_io_size_and_optional_legacy_short_profile(self):
        self.assertEqual(io_file_size(10 * GIB), GIB)
        self.assertEqual(io_file_size(60 * GIB), 4 * GIB)
        self.assertEqual(io_file_size(60 * GIB, 192 * MIB), 192 * MIB)
        self.assertEqual(build_plan("short", None, "vm", 1)["counts"]["mixed"], 1)
        with self.assertRaisesRegex(ValueError, "minimum mixed"):
            build_plan("short", 1, "vm", 2)


class PilotTests(unittest.TestCase):
    def test_compare_pilot_thresholds_and_tuning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pilots = []
            for index, quiet in enumerate(([.1, .2], [.1, .2, .5])):
                evidence = {"measurable": True, "baseline": {"samples": 12, "sustained_thresholds": quiet},
                            "intensities": [
                                {"intensity": "2", "watchdog_stop": False, "sustained_thresholds": [.5, 1, 2]},
                                {"intensity": "3", "watchdog_stop": True, "sustained_thresholds": [5]}]}
                path = root / f"vm-{index}.json"
                save_json(path, {"machine_id": f"vm-{index}", "dry_run": False,
                                 "resources": {name: {"some": evidence, "full": evidence}
                                               for name in RESOURCES}})
                pilots.append(path)
            report = compare(pilots)
            self.assertEqual(report["suggested_thresholds"],
                             {"cpu": 1, "memory": 1, "io": 1, "full": {"memory": 1, "io": 1}})
            self.assertEqual(assess({"some": evidence}, 1)["level"], 2)
            self.assertFalse(assess({"some": evidence}, 5)["ok"])
            self.assertFalse(assess({"some": evidence}, .5)["ok"])

    def test_pilot_does_not_freeze_production_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(root=root, command="calibrate", dry_run=True)
            plan = build_plan("short", None, "pilot-vm")
            config = {"cpu": 5, "memory": 1, "io": 1}
            with patch("aios_campaign.host_record",
                       return_value={"machine_id": "pilot-vm", "dry_run": True,
                                     "hostname": __import__("socket").gethostname()}):
                setup(args, plan, {})
                self.assertFalse((root / "thresholds.json").exists())
                args.command = "campaign"
                setup(args, plan, config)
                self.assertEqual(load_json(root / "thresholds.json"), config)
                with self.assertRaisesRegex(ValueError, "Saved thresholds"):
                    setup(args, plan, {**config, "cpu": 2})


class RetryTests(unittest.TestCase):
    def test_retry_once_continues_and_resumes_without_retrying_failed_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(root=root, dry_run=True, require_signal=False)
            plan = build_plan("short", None, "retry-vm")
            plan["sessions"] = plan["sessions"][:3]
            items = [item["run_id"] for item in plan["sessions"]]
            calls = []

            def fake_run(spec, directory):
                directory.mkdir(parents=True)
                key = (spec["run_id"], spec["campaign_attempt"])
                calls.append(key)
                fails = key == (items[0], 1) or key[0] == items[1]
                rec = {**spec, "status": "failed" if fails else "complete",
                       "quality": "aborted" if fails else "ok"}
                save_json(directory / "metadata.json", rec)
                return rec

            def fake_spec(args, item, machine, config, idle, signal_data):
                return {**item, "cooldown": idle}

            with (patch("aios_campaign.calibrate", return_value=dict.fromkeys(RESOURCES, {})),
                  patch("aios_campaign.spec_for", side_effect=fake_spec),
                  patch("aios_campaign.attempt_session", side_effect=fake_run),
                  patch("aios_campaign.package", return_value=root / "fake.zip")):
                campaign(args, plan, {}, dict.fromkeys(RESOURCES, 1))
                campaign(args, plan, {}, dict.fromkeys(RESOURCES, 1))
            self.assertEqual(calls, [(items[0], 1), (items[0], 2),
                                     (items[1], 1), (items[1], 2), (items[2], 1)])
            state = load_json(root / "campaign_state.json")
            self.assertEqual(state["status"], "complete_with_failures")
            self.assertEqual(state["sessions"][items[1]]["failed_attempts"], 2)
            self.assertEqual(state["sessions"][items[2]]["status"], "complete")


class LabelTests(unittest.TestCase):
    def test_boundaries_and_gap_for_all_horizons(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "system.csv"
            target = root / "labels.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([CLOCK_COLUMN, *(f"{name}_psi_some_total_us" for name in RESOURCES)])
                counter = 0
                for second in range(110):
                    if second in (41, 42, 70, 81):
                        counter += 100_000
                    if second != 80:
                        writer.writerow([second * NS, counter, counter, counter])
            counts = make_labels(source, target, "memory", 5)
            self.assertEqual(counts["onsets"], 1)
            with target.open(newline="", encoding="utf-8") as handle:
                rows = {int(row[CLOCK_COLUMN]) // NS: row for row in csv.DictReader(handle)}
            self.assertEqual(rows[41]["onset_interval_start_ns"], str(40 * NS))
            self.assertEqual(rows[41]["memory_psi_some_interval_pct"], "10.0")
            for horizon in (5, 10, 30):
                col = f"onset_next_{horizon}s"
                self.assertEqual(rows[40][col], "1")             # includes lower bound
                self.assertEqual(rows[41 - horizon][col], "1")   # includes horizon end
                self.assertEqual(rows[40 - horizon][col], "")    # crosses horizon end
                self.assertEqual(rows[39 - horizon][col], "0")
                self.assertEqual(rows[41][col], "")               # pressure is ongoing
                self.assertEqual(rows[81][col], "")               # gap starts a segment
            self.assertEqual(rows[70]["onset"], "0")


class WatchdogTests(unittest.TestCase):
    def test_low_memory_and_sustained_swap(self):
        watchdog = MemoryWatchdog(4096)
        self.assertIsNotNone(watchdog.check(149, 1000, 0, 0))
        watchdog = MemoryWatchdog(4096)
        pages = 64 * MIB // 4096
        for index, total in enumerate([0, pages, 2 * pages, 2 * pages, 3 * pages, 4 * pages]):
            self.assertIsNone(watchdog.check(150, 1000, total, index))
        self.assertIn("three", watchdog.check(150, 1000, 5 * pages, 6))

    def test_memory_scope_limits_and_guard(self):
        args = {"high_fraction": .60, "target_ratio": 1.5, "workers": 1}
        total = 5120 * MIB
        self.assertIsNone(memory_command("stress-ng", "unit.scope", args, total, total // 2, 60, "op"))
        command = memory_command("stress-ng", "unit.scope", args, total, total, 60, "op")
        self.assertIn("--scope", command)
        self.assertIn(f"--property=MemoryHigh={int(total * .6)}", command)
        self.assertIn(f"--property=MemoryMax={int(total * .7)}", command)
        self.assertIn("--property=MemorySwapMax=0", command)


class CampaignIntegrationTests(unittest.TestCase):
    def test_two_machines_and_frozen_thresholds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fixture(identity, cpu_threshold):
                directory = root / identity
                run_id = f"{identity}-cpu-001"
                run = directory / "runs" / run_id
                run.mkdir(parents=True)
                (directory / "labels").mkdir()
                save_json(directory / "machine.json", {"machine_id": identity})
                save_json(directory / "campaign_plan.json", {"profile": "short",
                          "sessions": [{"run_id": run_id, "category": "cpu"}]})
                save_json(directory / "campaign_state.json", {"date": "20261123", "sessions": {}})
                save_json(directory / "thresholds.json", {"cpu": cpu_threshold, "memory": 1, "io": 1})
                save_json(run / "metadata.json", {"run_id": run_id, "machine_id": identity,
                          "seed": derived_seed(identity, "cpu", 1), "category": "cpu",
                          "status": "complete", "quality": "ok"})
                (run / "system.csv").write_text("source", encoding="utf-8")
                (directory / "labels" / f"{run_id}_cpu.csv").write_text("labels", encoding="utf-8")
                return package(directory)

            first = fixture("alpha", 5)
            second = fixture("beta", 5)
            different = fixture("gamma", 6)
            output = root / "combined"
            self.assertEqual(merge([first, second], output, {"alpha": "train", "beta": "test"}), 2)
            with (output / "splits.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual({row["machine_id"]: row["split"] for row in csv.DictReader(handle)},
                                 {"alpha": "train", "beta": "test"})
            with self.assertRaisesRegex(ValueError, "different frozen thresholds"):
                merge([first, different], root / "conflict", {})
            self.assertFalse((root / "conflict").exists())

    def test_kill_partial_package_resume_and_merge(self):
        start = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "first.log"
            data = root / "campaign"
            config = root / "frozen.json"
            save_json(config, {"cpu": 5, "memory": 1, "io": 1, "full": {"memory": 2, "io": 2}})
            run_command = [sys.executable, "-B", str(ENTRY), "campaign", "--profile", "short",
                           "--dry-run", "--root", str(data), "--machine-id", "fixture-vm",
                           "--thresholds", str(config)]
            io_id = "fixture-vm-io-001"
            first_cpu = data / "runs" / "fixture-vm-cpu-001" / "metadata.json"
            events = data / "runs" / io_id / "events.csv"
            scratch = data / "io_scratch" / f".aios_{io_id}.bin"
            with log.open("w", encoding="utf-8") as stream:
                process = subprocess.Popen(run_command, stdout=stream, stderr=subprocess.STDOUT)
                try:
                    while time.monotonic() - start < 120:
                        if process.poll() is not None:
                            self.fail(log.read_text(encoding="utf-8"))
                        if events.exists() and "worker_start,io," in events.read_text(encoding="utf-8"):
                            break
                        time.sleep(.03)
                    else:
                        self.fail("I/O worker did not launch before timeout")
                    self.assertTrue(scratch.exists())
                    process.kill()
                    process.wait(timeout=15)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=15)
            baseline_hash = digest(first_cpu)
            partial = root / "partial.zip"
            result = subprocess.run([sys.executable, str(ENTRY), "package", "--root", str(data),
                                     "--output", str(partial)], capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(scratch.exists())
            self.assertFalse((events.parent / "cleanup.json").exists())
            with zipfile.ZipFile(partial) as archive:
                names = verified_archive(archive)
                self.assertIn(f"runs/{io_id}/metadata.json", names)
                self.assertNotIn(f"labels/{io_id}_io.csv", names)
                self.assertIn(f"{io_id} | io | interrupted", archive.read("CONTENTS.md").decode())
            result = subprocess.run(run_command, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(digest(first_cpu), baseline_hash)
            state = load_json(data / "campaign_state.json")
            self.assertEqual(state["status"], "complete")
            self.assertEqual(len(state["sessions"]), 9)
            self.assertTrue(all(record["status"] == "complete" for record in state["sessions"].values()))
            self.assertFalse(list((data / "io_scratch").glob("*.bin")))
            self.assertEqual(len(list((data / "runs").glob(f"{io_id}-aborted-*"))), 1)
            packaged, = data.glob("aios_*.zip")
            with zipfile.ZipFile(packaged) as archive:
                names = verified_archive(archive)
                self.assertEqual(sum(name.startswith("labels/") for name in names), 21)
                self.assertIn("labels/fixture-vm-mixed-002_io_full.csv", names)
                self.assertIn("calibration/calibration.json", names)
                for run_id in state["sessions"]:
                    record = json.loads(archive.read(f"runs/{run_id}/metadata.json"))
                    self.assertEqual(record["collector_metadata"], "copied")
                    self.assertEqual(record["validator"]["status"], "passed")
                    self.assertEqual(record["quality"], "low_signal")
            merged = root / "dataset"
            self.assertEqual(merge([packaged], merged, {"fixture-vm": "validation"}), 9)
            with (merged / "splits.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["split"] for row in rows}, {"validation"})
            with self.assertRaisesRegex(ValueError, "repeated machine ID"):
                merge([packaged, packaged], root / "duplicate", {})
            self.assertLess(time.monotonic() - start, 300)
            print(f"Accelerated short profile, hard kill, partial package, resume and merge: "
                  f"{time.monotonic() - start:.1f} s", flush=True)


if __name__ == "__main__":
    unittest.main()
