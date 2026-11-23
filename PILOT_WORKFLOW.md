# Choose the AIOS pressure thresholds

1. On each fresh Ubuntu pilot VM, run `init-machine` once after cloning. Each call below uses the default campaign root, `campaign_data`. For a previously initialized clone, choose a fresh `--root` or pass a distinct `--machine-id` to every command. Keep the same root and ID from pilot through campaign.
2. Run `python aios_campaign.py doctor`, then `python aios_campaign.py calibrate`. The calibration takes a quiet baseline and three workload intensities per resource. Add `--io-max-mib 1024` if you want a 1 GiB scratch limit. The default limit is 4 GiB, further capped at 10% of available disk space. Keep the same I/O limit across calibration and the campaign on that machine.
3. Gather one `campaign_data/calibration/pilot.json` from each pilot machine. Rename the copies by machine so one does not overwrite another. Compare the results:

```bash
python aios_campaign.py compare-calibration pilot-vm1.json pilot-vm2.json --output comparison.json
```

4. Review `comparison.json`. For each PSI channel, the report lists the candidate thresholds that were sustained for at least two seconds during a loaded phase without a watchdog stop and were absent as sustained events in the quiet baseline. The suggestion is the lowest candidate supported by every pilot. A missing suggestion means the available pilots did not agree on a candidate. Re-run the relevant pilot or investigate the weak channel before freezing a threshold. Simulated data is for testing.
5. Copy the reviewed settings into one shared `thresholds.json`. The required top-level keys are `cpu`, `memory`, and `io` for PSI `some`. Add an optional object such as `"full": {"memory": 1.0, "io": 1.0}` to label the corresponding PSI `full` counters separately. Values are percentages. The bundled example contains placeholders.
6. Use `python aios_campaign.py campaign --profile short --thresholds thresholds.json` on each VM. Calibration itself does not freeze a settings file. The campaign freezes the reviewed settings when production starts and reuses the existing pilot data in that root.

## Campaign controls

- Add `--require-signal` to stop before production when a baseline and a loaded phase do not establish sustained signal for every resource. The default keeps sessions and explicitly marks affected resources and runs `low_signal`. A run also receives that flag when its actual PSI `some` data contains no sustained onset. Mixed sessions expose quality for each resource.
- Recorded calibration levels shape single-resource workload intensity, while existing safety bounds and the shared label thresholds still apply. Mixed tracks retain their lighter limits.
- The default two-hour short profile has two mixed sessions. `--min-mixed-sessions 1` restores the original allocation and resumes a previously frozen short plan with one mixed session. An explicit minimum above available budget is rejected.
- An optional `--io-max-mib N` accepts 1–4096. Add paths such as `--collector PATH`, `--raw-dir PATH`, `--io-dir PATH`, and `--root PATH` to the per-machine commands as needed.
- Generate a standalone full-channel label with `python aios_campaign.py label system.csv io_full.csv --resource io --kind full --thresholds thresholds.json`.
