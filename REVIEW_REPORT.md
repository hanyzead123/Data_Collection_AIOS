# Collection review — 24 November 2026

Reviewed repository revision: `c59b9ef` (downloaded from the supplied GitHub repository).
Changes are local; nothing has been pushed to GitHub.

## What the evidence establishes

The existing 12-test suite passed locally with Python 3.13.5 on Windows. Its
accelerated campaign exercised all nine sessions, hard kill during I/O, partial
packaging, cleanup, resume, and merge in 74.7 seconds. Workloads, collector, and
validator were simulated. This verifies orchestration; it does not establish that
real Linux workloads generate useful PSI or that the separate collector works.

The updated suite adds four regression tests covering rejected validation,
unusable telemetry, output preservation, invalid parameters, and class counts.
All 16 updated tests passed in 77.546 seconds. The full kill/package/resume/merge
scenario passed in 74.6 seconds. `git diff --check` also passed.

## Fixes and efficiency changes

1. A validator returning failure or encountering an execution error previously
   left an otherwise successful session marked complete. Such sessions now fail,
   retain telemetry and diagnostics, and generate no new labels. Existing campaign
   retry handling applies. The optional missing-validator behavior is unchanged.
2. A header-only CSV, single sample, or series containing only gaps previously
   produced a successful label output without usable intervals. These inputs now
   raise an error. Failed labeling removes its temporary output and preserves an
   existing label file. Direct calls also reject overwriting the telemetry input
   and invalid numeric parameters.
3. The 5/10/30-second targets share one onset/calm calculation and one onset search
   per row. The original implementation repeated that work three times. Output
   rows stream into the CSV writer.
4. `metadata.json` label counts now include samples, valid intervals, segment
   count, calm samples, and positive/negative/blank counts per horizon. Existing
   numeric horizon keys retain their positive-count meaning. A session missing
   either class for a horizon is marked `low_signal`, with a reason. This is a
   diagnostic flag; such sessions remain available for inspection and merging.
5. Added a GitHub Actions configuration for simulated tests on Ubuntu and Windows
   with Python 3.11 and 3.14. Those hosted jobs have not been run here.

## Label equivalence and benchmark

Compared the exact original labeler loaded from Git revision `c59b9ef` with the
new shared implementation. All rows matched across 200 seeded randomized
segments, including irregular timestamps, threshold crossings, and short runs.

For 100,000 one-second samples, alternately calm and pressured, both implementations
streamed the same three-horizon rows into a SHA-256 checksum. `tracemalloc` measured
peak allocation after constructing the input. A single local run measured:

| Implementation | Time | Peak working memory |
| --- | ---: | ---: |
| Original | 6.715 s | 4.737 MiB |
| Shared calculations | 6.076 s | 1.579 MiB |

Outputs had identical checksums. This is about 9.5% less time and 66.7% less peak
working memory in this benchmark. It excludes CSV I/O and input storage, and is
not an estimate of total campaign speedup. Normal 10–15 minute sessions have far
fewer rows; collection time is mainly determined by the workload schedule.

## Prove collection on your Ubuntu VM

The repository does not include `system_collector.py` or
`validate_system_data.py`, and no actual collected CSVs were supplied. A Linux VM
was not accessible from this session. Keep the existing `TEST_REPORT.md` Linux
acceptance checks as requirements before unattended production collection.

From the updated folder inside Ubuntu, with your real collector and validator:

```bash
python -B -m unittest discover -s . -v
python aios_campaign.py init-machine --root pilot_review
python aios_campaign.py doctor --root pilot_review \
  --collector /path/to/system_collector.py \
  --validator /path/to/validate_system_data.py
python aios_campaign.py calibrate --root pilot_review \
  --collector /path/to/system_collector.py \
  --validator /path/to/validate_system_data.py
```

Use your documented `--raw-dir` if the collector does not use `~/aios_data/raw`.
Pass any required validator arguments using `--validator-arg`. Review
`pilot_review/calibration/pilot.json` and the per-resource CSVs and logs. A real
pass requires current monotonic timestamps, advancing PSI counters during load,
successful workload launches, a passed validator, no unexpected watchdog stops,
and quiet versus sustained-pressure evidence for each chosen threshold.

Compare pilot reports as documented in `PILOT_WORKFLOW.md`, then save the reviewed
thresholds as `thresholds.json`. Run production with signal checking enabled:

```bash
python aios_campaign.py campaign --root pilot_review --profile short \
  --collector /path/to/system_collector.py \
  --validator /path/to/validate_system_data.py \
  --thresholds thresholds.json --require-signal
```

Inspect each run's `metadata.json`: status, validator result, resource-quality
reasons, onsets, and positive/negative/blank counts at every horizon. A completed
process alone is insufficient evidence of a useful prediction dataset. Use a
fresh root for new production collection when changing code or data contracts;
completed sessions from older runs are skipped on resume and are not revalidated.
