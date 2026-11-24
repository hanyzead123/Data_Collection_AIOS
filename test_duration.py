"""Duration options, compact schedules, and the complete test-profile workflow."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aios_campaign import build_plan
from dataset import load_json, verified_archive
from workloads import RESOURCES, graded_schedule
import zipfile

ENTRY = Path(__file__).with_name("aios_campaign.py").resolve()


class DurationTests(unittest.TestCase):
    def test_test_profile_fits_calibration_and_all_resources(self):
        plan = build_plan("test", None, "vm")
        self.assertEqual(plan["budget_seconds"], 1800)
        self.assertEqual(plan["session_seconds"], 180)
        self.assertEqual(plan["counts"], dict.fromkeys((*RESOURCES, "mixed"), 1))
        self.assertLessEqual(9 * 60 + len(plan["sessions"]) * (180 + 60), plan["usable_seconds"])
        for resource in RESOURCES:
            for seed in range(100):
                phases = graded_schedule(resource, seed, 180, 4)
                self.assertEqual(sum(p[1] for p in phases), 180)
                self.assertEqual(phases[0][0], "baseline")
                self.assertEqual(phases[-1][0], "recovery")
                self.assertTrue(any(p[2] for p in phases))
                self.assertTrue(all(p[1] > 0 for p in phases))

    def test_custom_session_and_invalid_durations(self):
        self.assertEqual(build_plan("test", 1, "vm", session_minutes=3.5)["session_seconds"], 210)
        for minutes in (0, -1, 2.9, 60.1, float("nan"), float("inf")):
            with self.subTest(minutes=minutes), self.assertRaisesRegex(ValueError, "Session length"):
                build_plan("test", None, "vm", session_minutes=minutes)
        with self.assertRaisesRegex(ValueError, "30-minute"):
            build_plan("test", 20 / 60, "vm")
        with self.assertRaisesRegex(ValueError, "minimum mixed"):
            build_plan("test", None, "vm", min_mixed=2)

    def test_cli_minutes_hours_and_session_override(self):
        for options in (["--duration-minutes", "30"], ["--budget-hours", "0.5"]):
            result = subprocess.run([sys.executable, "-B", str(ENTRY), "plan", "--profile", "test",
                                     "--machine-id", "vm", *options], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {**build_plan("test", .5, "vm"),
                              **{k: v for k, v in json.loads(result.stdout).items()
                                 if k in {"expected_finish", "budget_deadline"}}})
        result = subprocess.run([sys.executable, "-B", str(ENTRY), "plan", "--profile", "test",
                                 "--duration-minutes", "60", "--session-minutes", "5"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual((plan["budget_seconds"], plan["session_seconds"]), (3600, 300))

    def test_cli_rejects_conflicting_and_nonfinite_budgets(self):
        for options in (["--duration-minutes", "30", "--budget-hours", "1"],
                        ["--duration-minutes", "nan"], ["--duration-minutes", "0"],
                        ["--duration-minutes", "-30"], ["--duration-minutes", "inf"]):
            result = subprocess.run([sys.executable, "-B", str(ENTRY), "plan", "--profile", "test", *options],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)

    def test_test_profile_collects_packages_and_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "campaign"
            command = [sys.executable, "-B", str(ENTRY), "campaign", "--profile", "test",
                       "--duration-minutes", "30", "--root", str(root),
                       "--machine-id", "duration-vm", "--dry-run"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            state = load_json(root / "campaign_state.json")
            self.assertEqual(state["status"], "complete")
            self.assertEqual(len(state["sessions"]), 4)
            self.assertEqual({s["status"] for s in state["sessions"].values()}, {"complete"})
            plan = load_json(root / "campaign_plan.json")
            self.assertEqual(plan["session_seconds"], 180)
            with zipfile.ZipFile(next(root.glob("aios_*.zip"))) as archive:
                names = verified_archive(archive)
                self.assertEqual(sum(n.startswith("labels/") for n in names), 6)
            metadata = {p: p.read_bytes() for p in (root / "runs").glob("*/metadata.json")}
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(metadata, {p: p.read_bytes() for p in metadata})
            changed = command.copy()
            changed[changed.index("30")] = "45"
            result = subprocess.run(changed, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Saved campaign_plan.json differs", result.stderr)


if __name__ == "__main__":
    unittest.main()
