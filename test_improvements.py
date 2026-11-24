"""Regression checks for telemetry rejection and training-data accounting."""

import csv
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cpu_runner import CLOCK_COLUMN, NS
from dataset import load_json, make_labels
from session import run_session


class LabelQualityTests(unittest.TestCase):
    def write_source(self, path, seconds):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([CLOCK_COLUMN, "cpu_psi_some_total_us"])
            counter = 0
            for second in seconds:
                if 41 <= second <= 45:
                    counter += 100_000
                writer.writerow([second * NS, counter])

    def test_empty_single_row_and_gaps_preserve_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "system.csv", Path(tmp) / "labels.csv"
            for seconds in ([], [0], [0, 10, 20]):
                with self.subTest(seconds=seconds):
                    self.write_source(source, seconds)
                    target.write_text("previous labels", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "no usable"):
                        make_labels(source, target, "cpu", 5)
                    self.assertEqual(target.read_text(), "previous labels")
                    self.assertFalse(target.with_suffix(".tmp").exists())

    def test_class_counts_match_written_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "system.csv", Path(tmp) / "labels.csv"
            self.write_source(source, range(110))
            counts = make_labels(source, target, "cpu", 5)
            with target.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(counts["samples"], 110)
            self.assertEqual(counts["valid_intervals"], 109)
            self.assertEqual(counts["segments"], 1)
            self.assertEqual(counts["onsets"], 1)
            self.assertEqual(counts["calm_samples"], sum(int(row["calm"]) for row in rows))
            for horizon, expected_positives in ((5, 5), (10, 10), (30, 30)):
                targets = [row[f"onset_next_{horizon}s"] for row in rows]
                totals = counts["horizons"][str(horizon)]
                self.assertEqual(totals, {"positive": targets.count("1"),
                                          "negative": targets.count("0"), "blank": targets.count("")})
                self.assertEqual(totals["positive"], expected_positives)
                self.assertGreater(totals["negative"], 0)
                self.assertEqual(counts[str(horizon)], expected_positives)

    def test_invalid_parameters_and_source_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "system.csv", Path(tmp) / "labels.csv"
            self.write_source(source, range(5))
            original = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "separate"):
                make_labels(source, source, "cpu", 5)
            for threshold, interval in ((0, 1), (101, 1), (float("nan"), 1), (5, 0), (5, float("inf"))):
                with self.subTest(threshold=threshold, interval=interval), self.assertRaises(ValueError):
                    make_labels(source, target, "cpu", threshold, interval)
            self.assertEqual(source.read_bytes(), original)


class ValidatorGateTests(unittest.TestCase):
    def test_validator_failure_and_error_prevent_completion_and_labels(self):
        for status in ("failed", "error", "passed", "unavailable"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = root / "runs" / "fixture"
                run.mkdir(parents=True)
                standin = str(Path(__file__).with_name("standins.py"))
                spec = {"run_id": "fixture", "machine_id": "vm", "category": "cpu",
                        "profile": "short", "seed": 1, "pilot": False, "cooldown": {},
                        "raw_dir": str(root / "raw"), "io_dir": str(root / "scratch"),
                        "dry_run": True, "tracks": {"cpu": [("baseline", 20, [])]},
                        "thresholds": {"cpu": 5}, "root": str(root), "seconds": 20,
                        "collector": standin, "resource_signal": {"cpu": True}}
                with patch("session.validate", return_value={"status": status}):
                    run_session(spec, run, threading.Event())
                record = load_json(run / "metadata.json")
                if status in {"failed", "error"}:
                    self.assertEqual(record["status"], "failed")
                    self.assertEqual(record["quality"], "aborted")
                    self.assertFalse((root / "labels").exists())
                    self.assertIn("validator rejected", record["error"])
                else:
                    self.assertEqual(record["status"], "complete")
                    self.assertEqual(record["quality"], "low_signal")
                    self.assertTrue((root / "labels" / "fixture_cpu.csv").exists())
                    self.assertTrue(any("lacks positive or negative" in reason for reason in
                                        record["resource_quality"]["cpu"]["reasons"]))
                self.assertTrue((run / "system.csv").exists())
                self.assertFalse((run / "cleanup.json").exists())


if __name__ == "__main__":
    unittest.main()
