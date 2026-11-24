# AIOS multi-resource campaign test report

## Duration update — 24 November 2026

The Desktop project was reviewed and updated with a `test` profile, mutually
exclusive `--duration-minutes` / `--budget-hours` options, and a
`--session-minutes` override. An explicitly supplied missing validator now fails
the environment check. The README explains setup, external collector requirements,
30-minute tests, complete campaigns, data quality, resume, and duration settings.

Executed with Python 3.13.5 on Windows:

| Check | Result |
| --- | --- |
| Full updated suite | **21 tests passed in 100.478 seconds** with `python -B -m unittest discover -s . -v`. |
| Existing crash/resume scenario | Passed in 74.5 seconds, including hard kill, partial package, scratch cleanup, resume, and merge. |
| New test-profile integration | Four accelerated sessions completed, archive SHA-256 entries verified, resume preserved completed metadata, and changing the saved duration was rejected. |
| Test-profile allocation | 1 CPU, 1 memory, 1 I/O, 1 mixed; each 180 seconds; budget 1,800 seconds. First-time calibration plus scheduled session overhead fits within the test planning allowance. |
| Compact schedules | Checked all three resources across 100 seeds at 180 seconds; durations matched, with baseline, active work, and recovery. |
| Duration input validation | Minutes/hours equivalent; custom session lengths supported; conflicting, zero, negative, nonfinite, too-short, and out-of-range settings rejected. |
| Source and documentation checks | Every Python source parsed; CLI help checked; all 27 README campaign command examples parsed; changed files had no trailing whitespace. |

These are software and simulated subprocess tests. The real `system_collector.py`
and `validate_system_data.py` are still absent from the project, and live Ubuntu
workloads were not available. The 30-minute real command must therefore still be
executed inside the user's VM. The budget controls session scheduling, not a hard
deadline for cleanup or ZIP creation. Historical results below refer to the
earlier suite; this section records the latest run.

## Executed

Tests ran with Python 3.13.5 on Windows. The stress-ng, fio, collector, and validator subprocesses were stand-ins. The collector stand-in accepts the two documented collector flags, writes timestamped CSVs using the shared monotonic clock, and writes source metadata keyed by experiment ID.

| Check | Result |
| --- | --- |
| Profile planning | Passed. Short planned 3 CPU, 2 memory, 2 I/O, and 2 mixed sessions. Long planned 14 CPU, 10 memory, 10 I/O, and 6 mixed sessions. Tested the legacy short allocation and the budget limit. |
| Determinism and duration | Passed. Checked schedules across resources and durations, all periodic rest values from 5–15 seconds, independent mixed tracks, and 2,000 distinct derived seeds across 50 machine IDs. |
| Hand-checked PSI labels | Passed. Checked 5, 10, and 30 second horizons, onset brackets, the last calm sample at the lower onset bound, exact horizon end, gaps, and transient high PSI. |
| Memory watchdog | Passed with numeric fixtures for low RAM, sustained extreme swap-in, reset of the swap streak, and bounded cgroup properties and launch headroom. |
| Accelerated short campaign | Passed in 76.5 seconds, including interruption, resume, packaging, and merge. All nine sessions completed with the stand-in tools marked `low_signal`. The archive contained 21 label files with the optional memory and I/O full channels enabled. |
| Hard kill and partial package | Passed. Killed the campaign during an active I/O workload, observed scratch-file removal, and verified the interrupted run had diagnostics and no labels. Resume preserved the first completed session byte for byte and completed the interrupted slot as a new attempt. |
| Package and merge | Passed. Verified all SHA-256 entries, collector metadata and validator result, machine partition overrides, two-machine combination, duplicate machine rejection, and rejection of inconsistent frozen thresholds. |
| Transient and permanent failures | Passed with an injected session fixture. A transient error recovered on attempt two, a permanent failure was recorded after two attempts, and the next session completed. Resume skipped the exhausted session. |
| Pilot and I/O settings | Passed with fixtures. Compared two pilots, filtered noisy baselines and watchdog stops, derived a workload level, froze thresholds only at campaign start, and verified disk sizing and the override. |
| Pilot CLI and clone identity | Passed in a separate smoke check. Two fresh roots produced distinct persisted IDs. Threshold-free calibration wrote portable evidence and comparison kept simulated evidence separate. |

Run the suite with `python -m unittest discover -s . -v` from the unpacked directory. Simulated archives are marked and cannot be combined with real campaign archives.

## Linux and VMware acceptance checks still required

1. Run `doctor` with the Python 3.14 venv on Ubuntu. Verify the installed collector's and validator's actual CLI, all three PSI counter names, and that metadata contains the experiment ID. The source scripts were unavailable in the build workspace and are supplied by the operator.
2. Run calibration with the installed stress-ng and fio. Confirm command flags, systemd scope startup and shutdown, direct I/O on the VM disk, and system-level PSI before production collection. Check that measured workload grades and per-resource quality agree with live VM behavior. A low-signal finding is retained in the archive, and `--require-signal` makes it a precondition for starting production.
3. Check scope limits and `memory.events` on the VM. The scope keeps its own OOM limit separate from the runner and collector. The guest reserve and swap watchdog reduce host-wide pressure risk. System-wide OOM behavior under concurrent external load was not exercised.
4. Exercise Ctrl+C, SIGTERM, hard kill, guest suspend, and reboot on an actual Linux pilot. The stand-in test covers loss of the campaign parent, supervisor cleanup, and resumption. Linux process-group signaling, privileged scope teardown, suspend detection, and reboot cleanup still need live verification. A power loss prevents immediate file cleanup. The next `campaign` or `package` reconciles its durable cleanup journal.
5. Verify host AC and idle conditions separately if VMware masks the battery or host contention. I/O PSI measures the guest's experience. It cannot certify physical-device pressure behind the hypervisor.

The delay limit is 5 minutes per session with a 1% PSI `some avg10` idle cutoff. The memory watchdog uses a 15% available-memory floor and a swap-in cutoff of 64 MiB/s for three intervals. The I/O scratch limit is configurable up to 4 GiB and never exceeds 10% of free space. These operational settings leave the frozen labeling thresholds unchanged.
