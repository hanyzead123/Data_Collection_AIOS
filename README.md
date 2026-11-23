# AIOS Multi-Resource Pressure Campaign

AIOS stands for **AI-Native Adaptive Operating System**. This toolkit collects labeled Linux telemetry for training and evaluating models that predict **CPU, memory, and disk I/O pressure** before it starts.

## What the toolkit does

The toolkit runs repeatable workloads inside Linux VMs and records how the guest responds. Each campaign includes CPU-only, memory-only, disk I/O-only, and mixed-resource sessions. Single-resource sessions help you study each kind of pressure. Mixed sessions show what happens when workloads compete for several resources at once.

The distinction that matters is **utilization versus pressure**. A CPU can be busy while the tasks that need it still run promptly. Pressure appears when tasks stall waiting for a resource. The toolkit uses Linux **Pressure Stall Information (PSI)** to measure these stalls and build prediction labels.

| Resource | How the campaign generates load | What the pressure signal captures |
| --- | --- | --- |
| CPU | `stress-ng` changes compute intensity across scheduled phases. | Tasks waiting for CPU time. |
| Memory | `stress-ng` creates memory demand inside a bounded cgroup. A watchdog can stop the workload when available memory is low or swap-in is sustained. | Tasks delayed by memory contention. |
| Disk I/O | `fio` generates storage load against a temporary file on the VM’s disk-backed filesystem. | Tasks waiting for I/O in the Linux guest. |
| Mixed | CPU, memory, and I/O workloads run together at lighter settings. | Pressure in each resource when workloads overlap. |

The workflow is **prepare the VM → collect pilot data → choose shared thresholds → run campaigns → merge the results**.

## 1. Set up the Ubuntu VM

Run real campaigns inside the Linux guest. Use Python 3.11 or newer. The project setup uses Python 3.14.

```bash
sudo apt update
sudo apt install -y python3 python3-venv stress-ng fio

cd /path/to/aios-campaign
python3 -m venv .venv
source .venv/bin/activate
```

If you already have a Python 3.14 environment, activate it and skip creating another one.

The AIOS telemetry collector is supplied separately. Place `system_collector.py` next to `aios_campaign.py`, and place `validate_system_data.py` there if you use the dataset validator. The collector must support `--duration` and `--experiment-id`, and its CSV must include the expected PSI counters and `monotonic_timestamp_ns`. The default raw-data directory is `~/aios_data/raw`.

On each **fresh VM after cloning**, initialize a unique machine identity and verify the environment.

```bash
python aios_campaign.py init-machine
python aios_campaign.py doctor
```

`doctor` checks the collector, Linux PSI, cgroup v2, installed tools, available RAM, and disk space. It requires at least 5 GiB of free disk space and checks that the I/O directory is on a disk-backed filesystem. Real workload management uses non-interactive `sudo` for cgroup operations, so have the VM administrator configure the required access. Resolve any failed checks before proceeding.

If your scripts or data live elsewhere, pass the corresponding options to `doctor`, `calibrate`, and `campaign`.

```bash
python aios_campaign.py doctor \
  --collector ~/AIOS/system_collector.py \
  --validator ~/AIOS/validate_system_data.py \
  --raw-dir ~/aios_data/raw \
  --io-dir ~/aios-io
```

## 2. Collect pilot data and choose thresholds

Run calibration on each pilot VM.

```bash
python aios_campaign.py calibrate
```

Calibration records a quiet baseline and three workload intensities per resource. It writes the portable report to `campaign_data/calibration/pilot.json`. Calibration can run before you have a thresholds file. You can reuse the same campaign directory for production.

Copy and rename the pilot reports on one machine, then compare them.

```bash
python aios_campaign.py compare-calibration \
  pilot-vm1.json pilot-vm2.json \
  --output comparison.json
```

Review `comparison.json`. A suggested threshold must be supported by every pilot. In each pilot, it must appear as sustained pressure during a loaded phase without a watchdog stop, and it must be absent as sustained pressure in the quiet baseline. If a resource has no shared suggestion, inspect its pilot results before choosing a threshold.

Save the reviewed thresholds in `thresholds.json` and use **the same file on every production VM**. The numbers below are examples. Replace them with values reviewed against your pilot data.

```json
{
  "cpu": 5.0,
  "memory": 1.0,
  "io": 1.0
}
```

The top-level values are percentages for PSI `some`. To generate additional PSI `full` labels for memory or I/O, add an optional `"full"` object with reviewed values for the applicable resources. These labels require the corresponding `full` counters in the collector data.

## 3. Preview and run a campaign

| Profile | Default budget | Default sessions |
| --- | --- | --- |
| `short` | 2 hours | 3 CPU, 2 memory, 2 I/O, 2 mixed |
| `long` | 12 hours | 14 CPU, 10 memory, 10 I/O, 6 mixed |

The budget includes the campaign overhead. The plan shows which sessions are scheduled, and the runner stops starting new sessions when the remaining budget is insufficient.

```bash
python aios_campaign.py plan --profile short

python aios_campaign.py campaign \
  --profile short \
  --thresholds thresholds.json
```

To run the longer profile, use `--profile long` in both commands. Other useful options are:

| Option | What it does |
| --- | --- |
| `--root PATH` | Selects the campaign directory. The default is `campaign_data`. Use the same directory for planning, calibration, collection, and packaging on that VM. |
| `--machine-id ID` | Sets the VM identity explicitly. Use a distinct ID for every VM and keep it consistent across commands. |
| `--budget-hours N` | Overrides the profile’s budget. Use the same value when previewing and running the plan. |
| `--io-max-mib N` | Sets a lower scratch-file cap. The default file limit is the smaller of 4 GiB and 10% of free disk space. Keep this setting consistent between calibration and production. |
| `--require-signal` | Stops before production if any resource lacks sustained calibration signal. By default, affected runs are retained and marked `low_signal`. |
| `--min-mixed-sessions 1` | Uses one mixed session with the original short-plan allocation. Use this when resuming an older short campaign frozen with that allocation. |

The campaign freezes its production thresholds when it starts. If a session fails, the runner retries it once. After a second failure, it records the failure and moves on. To resume an interrupted campaign, rerun the same command with the same root, identity, plan options, and threshold settings.

## 4. Understand the data and labels

| Output | Purpose |
| --- | --- |
| `campaign_data/campaign_plan.json` | Planned sessions and workload schedule. |
| `campaign_data/campaign_state.json` | Progress, session attempts, and completion status. |
| `campaign_data/calibration/pilot.json` | Evidence used to review threshold candidates. |
| `campaign_data/runs/<run_id>/system.csv` | Telemetry collected during a session. |
| `campaign_data/runs/<run_id>/events.csv` | Workload and phase timings for diagnosing a run. |
| `campaign_data/runs/<run_id>/metadata.json` | Session status, validation results, and resource-quality annotations. |
| `campaign_data/labels/` | Label CSVs for the configured resources and PSI channels. |

The labeler calculates a stall percentage from the change in the cumulative PSI counter divided by the elapsed monotonic time. It identifies sustained pressure onsets and labels eligible calm samples according to whether a new onset falls within the next **5, 10, or 30 seconds**. A blank label means the sample could not be assigned confidently for that horizon. Keep blank labels distinct from zero.

For model training, align telemetry and labels by `monotonic_timestamp_ns` **within the same run**. Build input features from current and historical telemetry. Keep future labels, workload names, schedules, `events.csv`, and `probe.csv` out of model inputs.

## 5. Package and merge results

The campaign produces a ZIP archive in its output directory. To rebuild it from the saved campaign state:

```bash
python aios_campaign.py package
```

Copy one archive from each VM to a common machine and merge them.

```bash
python aios_campaign.py merge vm1.zip vm2.zip --output dataset
```

Merge checks archive integrity and rejects duplicate machine IDs or inconsistent production thresholds. Incomplete attempts are excluded from the merged dataset. The resulting `splits.csv` assigns each machine to train, validation, or test, keeping all runs from one machine together.

## Testing

To check the workflow without generating real resource load:

```bash
python aios_campaign.py campaign --profile short --root simulated --dry-run
```

Dry runs use simulated telemetry and a separate output directory. Use real Ubuntu VM data for training. The automated tests cover planning, labels, failure recovery, resume, packaging, and merging with stand-ins. Live Ubuntu VMware validation remains pending. See [TEST_REPORT.md](TEST_REPORT.md) for coverage and [PILOT_WORKFLOW.md](PILOT_WORKFLOW.md) for threshold selection details.

```bash
python -m unittest discover -s . -v
```
