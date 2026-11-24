# AIOS Multi-Resource Pressure Campaign

AIOS means **AI-Native Adaptive Operating System**. This project runs repeatable workloads inside an Ubuntu VM, records system telemetry, and creates labels for predicting CPU, memory, and disk I/O pressure.

**You can now run a 30-minute test from the command line.** Use `--profile test --duration-minutes 30`. You do not need to edit Python code to change the duration. The final section of this README explains all duration settings.

## 1. What the project does

The campaign runs CPU-only, memory-only, I/O-only, and mixed-resource sessions. Each session includes a quiet baseline, workload changes, and recovery. A separate telemetry collector records the VM while these workloads run.

| Resource | Workload | What PSI measures |
| --- | --- | --- |
| CPU | `stress-ng` with varying worker counts and load | Time tasks stall waiting for CPU |
| Memory | `stress-ng` inside a bounded systemd memory scope | Time tasks stall because of memory contention |
| Disk I/O | `fio` against an initialized temporary disk file | Time tasks stall waiting for I/O in the guest |
| Mixed | CPU, memory, and I/O together with lighter settings | Pressure during overlapping resource demands |

High utilization does not always mean pressure. Labels use Linux Pressure Stall Information (PSI), which measures stalled tasks. VMware telemetry describes the guest's experience; it does not prove that the host's physical disk is saturated.

## 2. Files and requirements

| File | Role |
| --- | --- |
| `aios_campaign.py` | Main command-line interface, plans, calibration, retries, and campaign state |
| `session.py` | Collector/workload subprocesses, monitoring, validation, and cleanup |
| `workloads.py` | CPU/memory/I/O commands, schedules, and resource limits |
| `cpu_runner.py` | Shared timing and labeling logic; also an older CPU-only entry point |
| `dataset.py` | Multi-horizon labels, archive checksums, packaging, and merge |
| `pilot.py` | Pilot measurements and threshold comparison |
| `thresholds.example.json` | Example thresholds; these are not validated for your VM |
| `standins.py` | Simulated collector, validator, and workloads for software testing |
| `test_*.py` | Automated regression and integration tests |
| `PILOT_WORKFLOW.md` | Additional guidance for choosing shared thresholds |
| `TEST_REPORT.md`, `REVIEW_REPORT.md` | Test evidence and limits of local verification |

**The real `system_collector.py` and `validate_system_data.py` are not included in this folder.** Supply your existing collector before real collection. The validator is optional when omitted; explicitly supplying a missing validator path fails the environment check.

Use Python 3.11 or newer. Python 3.14 is supported by the configured CI matrix. Real workloads require Ubuntu/Linux, PSI, cgroup v2 with the memory controller, systemd, `stress-ng`, `fio`, disk space, and permission to manage workload scopes. Windows can run plans and simulated tests; real campaigns must run inside Ubuntu.

## 3. Set up inside Ubuntu

Copy this project folder into the Ubuntu VM. Run the following in an Ubuntu terminal, changing the project path to your Linux location. The Windows Desktop path is not the path inside the VM.

```bash
cd ~/Data_Collection_AIOS
sudo apt update
sudo apt install -y python3 python3-venv stress-ng fio
python3 -m venv .venv
source .venv/bin/activate
python --version
```

If you already have a suitable venv, activate it. Install any additional Python dependencies required by your separate collector using that collector's own instructions. This campaign's Python code uses the standard library.

Place your scripts beside `aios_campaign.py`, or supply their paths using `--collector` and `--validator`. Verify the collector's interface:

```bash
python system_collector.py --help
python validate_system_data.py --help
```

Skip the validator command if you do not have that script. The campaign expects the collector to accept `--duration SECONDS` and `--experiment-id ID`. It launches the collector with the active Python interpreter and unbuffered output. The collector must flush rows during collection and write a new `system_*.csv` in `~/aios_data/raw`, or the directory supplied through `--raw-dir`.

Expected CSV columns at about one sample per second:

```text
monotonic_timestamp_ns
cpu_psi_some_total_us
memory_psi_some_total_us
io_psi_some_total_us
```

Timestamps must be integer nanoseconds from the same guest monotonic clock. PSI columns must be raw cumulative microsecond counters, not `avg10` percentages. The label reader supports alternate names such as `mem_psi_some_total_us` and `psi_memory_some_total_us`; startup currently requires the exact CPU column `cpu_psi_some_total_us`. Optional full-channel labels require memory/I/O `full` counters too. Collector JSON metadata should contain the matching `experiment_id`.

The default validator call is `python validate_system_data.py /absolute/path/to/system.csv`. If your validator uses a flag, supply its documented arguments, for example `--validator-arg=--input --validator-arg='{csv}'` **only if** its help lists `--input`. The `{csv}` placeholder is replaced by the session CSV path.

## 4. Check the environment

Initialize each new campaign root once, then run `doctor`:

```bash
python aios_campaign.py init-machine --root campaign_data
python aios_campaign.py doctor --root campaign_data \
  --collector ./system_collector.py \
  --validator ./validate_system_data.py
```

Remove `--validator ./validate_system_data.py` from examples if you do not have it. Without a validator, metadata reports validation as `unavailable`, so you must check data quality separately.

`doctor` checks Python, installed workload tools, Linux PSI, cgroup v2, non-interactive sudo, RAM, at least 5 GiB of free disk space, collector CLI flags, and a disk-backed I/O directory. It checks an explicitly supplied validator file exists. It does not prove that the collector's full output schema or workload commands work in your VM; calibration tests those paths.

Memory scopes use non-interactive `sudo`. Have the VM administrator configure the required access and resolve failing checks before a real campaign. The current sudo probe is `sudo -n -k true`; temporary cached credentials alone do not pass it. Keep the VMware host on AC power and record unusual host activity.

To use other paths, pass these same options to `doctor`, `calibrate`, and `campaign`:

```bash
python aios_campaign.py doctor --root campaign_data \
  --collector ~/AIOS/system_collector.py \
  --validator ~/AIOS/validate_system_data.py \
  --raw-dir ~/aios_data/raw --io-dir ~/aios-io
```

Use only one collector writing to the selected raw directory at a time.

## 5. Run a real 30-minute workflow test

This checks the actual collector and real Linux workloads with a small campaign. The test profile plans **one CPU, one memory, one I/O, and one mixed session**, each lasting three minutes. It reserves room for first-time calibration in its 30-minute budget.

Preview first:

```bash
python aios_campaign.py plan --profile test --duration-minutes 30 --root test_30
```

Then initialize a fresh test root and run:

```bash
python aios_campaign.py init-machine --root test_30
python aios_campaign.py campaign --profile test --duration-minutes 30 \
  --root test_30 --collector ./system_collector.py \
  --validator ./validate_system_data.py \
  --thresholds thresholds.example.json --io-max-mib 1024
```

This example deliberately uses the bundled **example thresholds for a smoke test**. It verifies the workflow; it does not establish useful training thresholds. If you have already reviewed thresholds, use `--thresholds thresholds.json` instead. A 1 GiB I/O scratch cap reduces preparation time for this test.

The campaign runs missing calibration automatically. Under normal idle conditions, the plan estimates about 25 minutes including calibration and scheduled overhead. **30 minutes is a scheduling budget, not an exact timer or hard process-kill deadline.** It can finish early. Disk preparation, longer waits for the VM to settle, validation, shutdown, and ZIP creation can change elapsed time. It stops starting sessions when the remaining budget is insufficient; it preserves cleanup and diagnostics.

Inspect `test_30/campaign_state.json`, per-run `metadata.json`, logs, and the resulting ZIP. Short tests may produce few onsets or no usable examples for some horizons; such runs are marked `low_signal`. Use a longer collection campaign after confirming the setup.

## 6. Choose thresholds for training collection

Use a separate production root so smoke-test thresholds and data are kept separate:

```bash
python aios_campaign.py init-machine --root campaign_data
python aios_campaign.py calibrate --root campaign_data \
  --collector ./system_collector.py --validator ./validate_system_data.py
```

Skip `init-machine` if this root was already initialized in section 4. Calibration has a quiet baseline and three intensities per resource, about three minutes per resource plus setup and settling. It writes `campaign_data/calibration/pilot.json`; no thresholds file is required to calibrate.

Compare one pilot or multiple VM reports:

```bash
python aios_campaign.py compare-calibration campaign_data --output comparison.json
# Or, after copying reports from separate VMs:
python aios_campaign.py compare-calibration pilot-vm1.json pilot-vm2.json \
  --output comparison-multiple.json
```

A proposed threshold must produce sustained signal in a loaded phase without a watchdog stop and avoid sustained signal in the quiet baseline on every pilot. Inspect the reports if a resource has no shared suggestion. An existing comparison output is not overwritten; select a new name for a new report.

Create and edit your reviewed settings:

```bash
cp thresholds.example.json thresholds.json
nano thresholds.json
```

Replace the example values using your pilot evidence. Use the same reviewed file on every production VM. Top-level `cpu`, `memory`, and `io` values are PSI `some` percentages in `(0, 100]`. To add full-channel labels, add reviewed values in an optional `"full": {"memory": 1.0, "io": 1.0}` object. Those numbers are examples too.

## 7. Run the full collector and campaign

The campaign command starts and stops the collector for every calibration and production session, generates workload events and probe data, validates available telemetry, labels completed production sessions, and packages the results. **Do not start a second collector separately while the campaign is running.**

| Profile | Default total budget | Session length | Default session counts |
| --- | --- | --- | --- |
| `test` | 30 minutes | 3 minutes | 1 CPU, 1 memory, 1 I/O, 1 mixed |
| `short` | 2 hours | 10 minutes | 3 CPU, 2 memory, 2 I/O, 2 mixed |
| `long` | 12 hours | 15 minutes | 14 CPU, 10 memory, 10 I/O, 6 mixed |

The **14** in the long profile is the number of CPU sessions, not 14 hours. Counts change when you change the budget or session length.

Full two-hour campaign:

```bash
python aios_campaign.py plan --profile short --root campaign_data
python aios_campaign.py campaign --profile short --root campaign_data \
  --collector ./system_collector.py --validator ./validate_system_data.py \
  --thresholds thresholds.json --require-signal \
  --host-notes "Ubuntu VMware guest; host on AC; host idle"
```

Full twelve-hour campaign, using a fresh root if you already collected a different plan:

```bash
python aios_campaign.py init-machine --root campaign_long
python aios_campaign.py plan --profile long --root campaign_long
python aios_campaign.py campaign --profile long --root campaign_long \
  --collector ./system_collector.py --validator ./validate_system_data.py \
  --thresholds thresholds.json --require-signal
```

`--require-signal` stops before production unless calibration demonstrates sustained signal for all three chosen `some` thresholds. Without it, the campaign retains affected runs with `low_signal` annotations. Optional full-channel labels need separate review of their evidence and class counts.

If you only want to record telemetry without generated workloads, the collector contract used by this project supports:

```bash
python -u system_collector.py --duration 1800 --experiment-id manual-30min
```

That command requests 1,800 seconds of collection only. It does not run campaign workloads, label data, or produce the campaign archive. Check the supplied collector's `--help` for any additional requirements. The collector itself is supplied separately, so this exact standalone path has not been executed here.

## 8. Resume, stop, and inspect results

Press Ctrl+C for a controlled stop. The campaign tries to stop workload groups/scopes and the collector, saves state, and preserves diagnostics. On normal Linux cleanup, it first sends the collector SIGINT and allows ten seconds for shutdown. The collector's own metadata may therefore say interrupted even when the campaign deliberately ended its sampling.

To resume, repeat the **same command**, including profile, duration, session length, root, identity, thresholds, and other settings. Completed sessions are skipped. Failed sessions are retried once, then recorded as failed while the campaign moves on. Interrupted attempts are retained in diagnostic directories. Do not change a saved plan or frozen thresholds in place; use a new `--root`.

The budget is measured from the first campaign start stored in `campaign_state.json`, including elapsed time while stopped. Repeating an expired campaign does not grant a fresh budget. Use a fresh root for a new timed experiment. Reusing the same identity across independent roots on the same VM is appropriate; distinguish archives before merging them because merge currently accepts only one archive per machine ID.

| Output under the selected root | Purpose |
| --- | --- |
| `machine_identity.json`, `machine.json` | Persisted identity, host configuration, tool versions, and collection mode |
| `campaign_plan.json` | Frozen session allocation and budget; workload detail is in each session's diagnostics |
| `campaign_state.json` | Overall status, session results, failed attempts, and calibration signal |
| `thresholds.json` | Frozen production labeling thresholds |
| `calibration/pilot.json` | Portable pilot evidence |
| `runs/<run_id>/system.csv` | Copied telemetry from the collector |
| `runs/<run_id>/metadata.json` | Status, validator result, resource quality, and label counts |
| `runs/<run_id>/events.csv`, `probe.csv` | Actual workload timings and responsiveness diagnostics |
| `runs/<run_id>/*.log` | Collector, workload, and validator messages |
| `labels/<run_id>_<resource>.csv` | PSI values, calm eligibility, onsets, and future targets |
| `aios_*.zip` | Packaged results and checksum manifest |

A useful dataset needs more than a completed process. Check the validator status, actual sample cadence, pressure counters, confirmed onsets, and positive/negative/blank counts for every horizon. Validator failure or execution error fails the session. Empty or entirely disconnected telemetry cannot be labeled. Missing positive or negative examples for any horizon marks the resource `low_signal`. Those runs are retained and may still be merged; inspect them before training.

## 9. Labels and model inputs

For each interval, the labeler divides the change in cumulative PSI stall microseconds by elapsed monotonic time and converts it to a percentage. By default, pressure must persist for two seconds. Calm eligibility requires three seconds of below-threshold history. Targets ask whether a confirmed onset falls within the next **5, 10, or 30 seconds**.

`1` means a future onset meets the timing rules. `0` means the eligible window has no onset. Blank means no confident target: pressure is already active, history/future coverage is insufficient, or onset timing crosses a boundary. **Do not turn blank targets into zero.** Gaps and counter resets break the series.

Align labels and telemetry on `monotonic_timestamp_ns` within the same run. Build features from present and historical telemetry. Exclude future targets, onset scoring columns, workload names/schedules, `events.csv`, and `probe.csv` from model inputs. Fit preprocessing only on training data.

To label a CSV separately with reviewed thresholds:

```bash
python aios_campaign.py label /path/to/system.csv /path/to/cpu_labels.csv \
  --resource cpu --thresholds thresholds.json --interval 1
```

Use `--resource memory` or `io` as appropriate; use `--kind full` only with full counters and reviewed full thresholds. Set `--interval` to the actual sampling interval if it differs from one second. Real campaign labeling currently assumes one-second sampling.

## 10. Package and merge

The campaign writes its archive automatically. Rebuild it, or package available results after interruption:

```bash
python aios_campaign.py package --root campaign_data
python aios_campaign.py package --root test_30 --output test_30_results.zip
```

Copy one archive per VM to a common machine and combine them:

```bash
python aios_campaign.py merge vm1.zip vm2.zip --output dataset
```

Merge checks SHA-256 entries and rejects duplicate identities/seeds, conflicting frozen thresholds, and mixing real and simulated archives. It includes completed eligible runs, including `low_signal`, and excludes incomplete attempts. All runs from a machine stay in one split to reduce leakage.

With few VMs, automatic machine hashing may leave a split empty. Supply explicit assignments when needed:

```json
{"vm01": "train", "vm02": "validation", "vm03": "test"}
```

Save that object as `split_overrides.json` with your actual IDs, then use:

```bash
python aios_campaign.py merge vm1.zip vm2.zip vm3.zip \
  --output dataset --split-overrides split_overrides.json
```

## 11. Safety settings and common failures

Memory workloads are confined to scopes with memory-high/max settings and swap disabled. A watchdog stops memory work below 15% available guest memory or after sustained swap-in of 64 MiB/s for three intervals. This reduces risk but cannot guarantee behavior under competing host/guest activity.

I/O scratch files are limited to the smaller of 4 GiB and 10% of free disk; `--io-max-mib 1024` lowers the cap to 1 GiB. The campaign requires at least 5 GiB free and rejects RAM-backed scratch directories. Cooldown waits for PSI to settle, with a five-minute maximum wait. Cleanup/reboot behavior still requires live Linux acceptance checks described in `TEST_REPORT.md`.

| Message or symptom | Action |
| --- | --- |
| Collector missing or unsupported CLI | Supply the separate script and check `--help` and `doctor` |
| Collector startup timeout | Inspect `collector.log`; verify raw directory, live row flushing, timestamp clock, and CPU column name |
| Multiple new collector CSVs | Stop competing collectors or use a dedicated raw directory |
| Validator file missing | Correct `--validator`; omit it only if validation is intentionally unavailable |
| Validator rejected session | Read `validator.log`; fix telemetry before retrying |
| Budget cannot fit sessions | Use the test profile for 30 minutes, shorten sessions to at least 3 minutes, or increase total duration |
| Saved plan or thresholds differ | Restore the original command/settings or use a fresh root |
| `low_signal` | Inspect calibration, watchdog events, and label class counts; do not infer success from utilization alone |
| `budget_exhausted` | Inspect completed sessions; the budget ran out before the next full session could start |
| Campaign complete with failures | Inspect each failed run's metadata and logs; do not treat the overall process exit alone as acceptance |

## 12. Software tests without real load

Plans and simulated campaigns work on Windows or Ubuntu. From this project directory:

```bash
python -B -m unittest discover -s . -v
python aios_campaign.py campaign --profile test --duration-minutes 30 \
  --root simulated_test_30 --dry-run
```

The simulated campaign accelerates time and normally finishes in tens of seconds. It exercises campaign management, not 30 minutes of real telemetry. Its short simulated traces can have blank targets and low-signal flags. Never train on these fixtures. Use separate roots for simulated and real collection.

Tests cover plans, duration validation, three-minute schedules, PSI label boundaries, class accounting, watchdog fixtures, validator rejection, retries, crash cleanup, resume, archive verification, and merge. The GitHub workflow defines Ubuntu/Windows jobs with Python 3.11/3.14; hosted jobs and real VM workloads were not executed by this local review. See `TEST_REPORT.md` for the latest local result.

## 13. How to change the total time and session time

**Use command-line options for ordinary runs. No code editing is required.** Apply the same settings to `plan` and `campaign`.

| Setting | Meaning |
| --- | --- |
| `--profile test` | Defaults to a 30-minute campaign with 3-minute sessions; reserves planning space for nine minutes of first-time calibration |
| `--profile short` | Defaults to a 2-hour campaign with 10-minute sessions |
| `--profile long` | Defaults to a 12-hour campaign with 15-minute sessions |
| `--duration-minutes N` | Overrides the total campaign budget in minutes |
| `--budget-hours N` | Overrides the same total budget in hours; use this or `--duration-minutes`, never both |
| `--session-minutes N` | Overrides each workload session length, from 3 to 60 minutes; fractional values are converted to whole seconds |

Preview a 30-minute real test:

```bash
python aios_campaign.py plan --profile test --duration-minutes 30
```

Run it with actual collection, using a fresh output root:

```bash
python aios_campaign.py campaign --profile test --duration-minutes 30 \
  --root test_30_new --collector ./system_collector.py \
  --validator ./validate_system_data.py --thresholds thresholds.example.json
```

Again, example thresholds are for workflow testing. Use reviewed `thresholds.json` for training data.

Other duration examples:

```bash
# 45-minute test budget; retain three-minute sessions
python aios_campaign.py plan --profile test --duration-minutes 45

# One-hour budget with five-minute sessions
python aios_campaign.py plan --profile test --duration-minutes 60 --session-minutes 5

# Same one-hour budget expressed in hours
python aios_campaign.py plan --profile test --budget-hours 1 --session-minutes 5

# Four-hour collection using the short profile's ten-minute sessions
python aios_campaign.py plan --profile short --duration-minutes 240
```

Replace `plan` with `campaign`, add the collector and thresholds options, and choose a fresh `--root` to collect with that allocation. Budget changes alter the number of sessions. Session changes alter both per-session duration and how many sessions fit. A 30-minute budget with the short profile's default ten-minute sessions cannot fit all four categories; select `--profile test`. Test budgets shorter than approximately 28 minutes cannot fit first-time calibration and one minimum-length session per category with the reserved overhead.

The budget covers campaign time, not a separate full `calibrate` command you ran earlier. `--session-minutes` changes production sessions only; calibration stays at three minutes per resource. Collection ends when scheduled sessions are complete or the next session no longer fits. It does not deliberately idle until the budget expires, and cleanup may extend past it.

If you want to permanently change profile defaults in code, edit `PROFILES` near the top of `aios_campaign.py`:

```python
PROFILES = {"long": (12, 15), "short": (2, 10), "test": (.5, 3)}
```

Each pair is **(total hours, minutes per workload session)**. For example, changing `"short": (2, 10)` to `"short": (4, 10)` changes its default budget to four hours. Keep session lengths in the supported range, run the tests, and inspect `plan`. Editing defaults also changes the expectations in profile tests. Use a fresh campaign root when defaults or plan settings change. For individual experiments, the command-line options above are simpler.
