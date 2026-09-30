# CFB event-aligned EPA

The canonical `ep-v2` model is implemented in
`sports_aggregator/cfb/expected_points_event.py`. It estimates the value of a
pre-play state from the next scoring event within the same half, then assigns
EPA to the play that caused the state transition. Overtime and administrative
states are excluded.

## Historical retune

After PBP was expanded to the local game-history floor, the production model
was retuned on 2015-2025 and its state-cell shrinkage changed from 30 to 10
samples. The setting was selected on a 2015-2023 -> 2024 temporal holdout and
confirmed on 2015-2024 -> 2025 before the final fit.

| Holdout | Comparable 2022+ fit MAE / RMSE | Expanded fit MAE / RMSE |
| --- | --- | --- |
| 2024 | 4.15973 / 4.98183 | 4.14412 / 4.97586 |
| 2025 | 4.10261 / 4.93754 | 4.09568 / 4.93412 |

The improvement is small but consistent across both holdouts. Production was
then fitted through completed 2025 only and scored across 2015-2026, keeping
the partial 2026 season out of coefficient estimation.

Run the event-aligned holdout validator with:

```powershell
python -m sports_aggregator.cfb.expected_points_event_cli validate `
  --from-year 2025 --to-year 2025 --model-version <holdout-model-version>
```

Rebuild the canonical model and dependent report tables with:

```powershell
python -m sports_aggregator.cfb.ep_v2_rebuild_cli `
  --fit-from-year 2015 --fit-to-year 2025 `
  --score-from-year 2015 --score-to-year 2026 --min-cell 10
```

## Deploying coefficients without expanding live PBP

`data/cfb/models/ep-v2.json` contains the fitted 583-cell model and a SHA-256
checksum. Installing it replaces only `cfb_expected_points_state`; it does not
write `cfb_plays`, schedules, or any provider cache.

To keep the live site's existing historical coverage and recalculate EPA only,
run this from the disk-backed web-service shell after deploying the artifact:

```bash
python -m sports_aggregator.cfb.ep_v2_rebuild_cli \
  --skip-fit \
  --score-from-year 2015 --score-to-year 2026 \
  --detail-from-year 2025
```

`--skip-fit` atomically installs the bundled artifact before scoring. A lower
year bound earlier than the live PBP floor is harmless: the scorer reads and
writes only play IDs already stored in that database. The command then rebuilds
the EPA-dependent team-game, tendency, and quarterback summaries over that same
available coverage.
