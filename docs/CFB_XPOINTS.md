# CFB xPoints and opponent quality (Milestone 11)

## What ships

The matchup Projection tab now extends the volume chain through drive outcomes:

`xDrives -> touchdown / field-goal probabilities -> opponent-adjusted points per drive -> expected offensive points`

The implementation lives in:

- `team_game_drive_outcomes.py`: mutually exclusive meaningful-drive outcomes and offensive points.
- `xpoints.py`: chronological training rows, quality/form features, temporal evaluation, and the persisted ridge model.
- `game_projection.py`: live pregame snapshots and matchup inference.

“Offensive points” is deliberately narrower than final team score. It credits an
offensive touchdown as 7 and a made field goal as 3, and excludes defensive and
return touchdowns. This makes the target consistent with the offense's drive
opportunities and keeps the future player-allocation layer coherent.

## Leakage controls

Every feature on a game row is calculated before that game's result is added to
history. Both teams are featured before either side updates the rolling state.

- Team and opponent drive-outcome histories use the same recency decay and
  trailing window as the other x-stat layers.
- Recent margin contains only previously completed games.
- Elo is the stored pregame Elo.
- FPI is the per-game pregame projection.
- CORE uses only a snapshot with `through_week < game.week`.
- Vegas uses the stored consensus spread, oriented to the team.

## Stronger opponent adjustment

Two residual histories distinguish schedule strength from raw production:

- Offensive residual = actual points/drive minus what that opponent had
  previously allowed.
- Defensive residual = actual points/drive allowed minus what that opposing
  offense had previously scored.

Positive defensive residual means the defense allowed more than expected. The
raw residual-only estimate is retained as a diagnostic, but is not served by
itself because it lost on the temporal holdout.

## Blended quality

Four independent pregame views are put onto an estimated point-margin scale:

| Source | Team-oriented value |
|---|---:|
| Elo | `(team Elo - opponent Elo) / 25` |
| ESPN FPI | team `pred_point_diff` |
| CFBD CORE | team overall minus opponent overall |
| Vegas | negative of the team-relative spread |

The live quality edge is the equal-weight mean of available sources. Missing
sources are omitted and the remaining weights are renormalized; source count is
stored with every row. This avoids filling a missing pregame opinion with a
later value.

## 2025 temporal holdout

Training used 2022–2024 and testing used 2025. All numbers below are
points-per-drive error; lower is better.

| Model | Games | MAE | RMSE |
|---|---:|---:|---:|
| League prior | 1,854 | 0.8191 | 1.0229 |
| Team trailing | 1,854 | 0.7714 | 0.9731 |
| Team/opponent allowed blend | 1,853 | 0.7798 | 0.9734 |
| Raw opponent residual | 1,854 | 0.8154 | 1.0211 |
| Residuals + recent form + quality blend | 1,850 | **0.6944** | **0.8784** |

On the 1,428 test rows where all four quality sources were present, the quality
ablation was:

| Added quality signal | MAE | RMSE |
|---|---:|---:|
| No rating/market quality | 0.7428 | 0.9343 |
| Elo | 0.7185 | 0.9052 |
| FPI | 0.7061 | 0.8912 |
| CORE | 0.7175 | 0.8978 |
| Vegas | 0.6978 | 0.8815 |
| Equal four-source blend | **0.6938** | **0.8722** |

The blend beat every individual source on the identical complete-case holdout.
The production model was then refit on 2022–2025 and persisted as
`xpoints-ridge-v1` for 2026 inference.

## Commands

```powershell
python -m sports_aggregator.cfb.pbp_cli build-team-drive-outcomes --from-year 2022 --to-year 2025
python -m sports_aggregator.cfb.pbp_cli build-xpoints-dataset --from-year 2022 --to-year 2025
python -m sports_aggregator.cfb.pbp_cli evaluate-xpoints --from-year 2022 --to-year 2025 --train-from-year 2022 --train-to-year 2024 --test-from-year 2025 --test-to-year 2025
python -m sports_aggregator.cfb.pbp_cli fit-xpoints --from-year 2022 --to-year 2025
```
