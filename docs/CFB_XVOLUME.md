# xPassRate and Expected Run/Pass Volume (Milestone 6)

Scope: `Expected Dropbacks` and `Expected Rush Attempts` (spec §19), the
third layer of `xDrives → xPlaysPerDrive → xPassRate → Expected Plays →
Expected Volume`. Same modular split as
[docs/CFB_XPLAYS.md](CFB_XPLAYS.md) — a new file,
`sports_aggregator/cfb/xvolume.py`, rather than folding into the two
existing ones.

## A prerequisite fix: raw attempt counts

`cfb_team_game_pace` stored `pass_rate` but not the underlying `pass_plays`/
`rush_plays` counts, so getting exact attempt counts would have meant
reconstructing them by rounding `pass_rate × scrimmage_plays` back out —
fragile for something this downstream work depends on directly. Added
`pass_plays`/`rush_plays` INTEGER columns to `cfb_team_game_pace` instead,
computed from the same `pass_plays` value `team_game_pace.build()` already
derives internally, migrated onto the existing table the same way
`play_detail.py` backfills its own additions (`PRAGMA table_info` +
`ALTER TABLE ADD COLUMN`).

## Design

`cfb_xvolume_dataset` — one row per team-game, same leak-safe chronological
walk as `xdrives.py`/`xplays.py`, reusing xDrives' tuned recency half-life:

- `actual_pass_rate` / `actual_pass_attempts` / `actual_rush_attempts` — the
  target and its exact counts.
- `team_prior_pass_rate`, `team_prior_neutral_pass_rate` — a team's own
  trailing identity, both the raw (game-script-diluted) rate and the
  situation-neutral one spec §3 and §18 both prefer as a cleaner identity
  signal.
- `team_prior_pass_rate_allowed` (+ opponent mirror) — the defensive
  analog of xDrives' `drives_allowed`: what a team's *opponents* have
  passed at against its defense.
- `team_elo`, `opponent_elo`, `market_spread` (home-relative; flipped to
  this team's own signed line, same convention as `xdrives.py`) — inputs
  for testing the game-script hypothesis below.

## The actual question this milestone needed to answer

Spec §19 is explicit: *"Do not simply multiply overall pass percentage by
projected plays... game state should influence the expectation."* That is a
testable claim, not an assumption to build in — Baseline D regresses
`actual_pass_rate` on `team_prior_neutral_pass_rate`,
`opponent_prior_pass_rate_allowed`, and this team's own signed
`market_spread` (a stand-in for expected game script: a big favorite is
expected to protect a lead and run more; a big underdog is expected to
throw more, especially trailing).

**Result, in-sample on 2022–2025:** the spread coefficient came out
`+0.00057` — the *correct* sign (more of an underdog → more passing) but
functionally zero: even a 30-point spread only moves the predicted pass
rate by ~1.7 percentage points. Baseline D's overall MAE (0.086) is barely
distinguishable from Baseline C's (0.087) or even plain Baseline B's
(0.086). **The naive game-script-via-point-spread hypothesis does not hold
up as a material effect in this data**, at least not in this simple linear
form. This is the same pattern xDrives found for its environment term and
shrinkage lever (`CFB_XDRIVES.md` §7): a plausible-sounding adjustment that,
measured honestly, does not earn its complexity. No further per-lever
tuning was attempted here for the same reason spec §37 gives — chasing a
coefficient until it looks good on the same data it was fit on is fitting
the holdout, not the problem.

Baseline C (the plain opponent-adjusted blend) remains the recommended
choice for pass rate, same as it is for drives and plays/drive.

## Expected Volume

`evaluate_expected_volume(repository, ...)` chains all three layers' own
Baseline C — `((team_prior_drives + opponent_prior_drives_allowed)/2) ×
((team_prior_plays_per_drive + opponent_prior_plays_per_drive_allowed)/2) ×
((team_prior_pass_rate + opponent_prior_pass_rate_allowed)/2)` — split into
Expected Dropbacks and Expected Rush Attempts, backtested against actual
attempt counts. In-sample on 2022–2025:

| | naive (league constants) | combined matchup blend |
|---|---|---|
| Expected Dropbacks MAE | 8.74 | 8.20 |
| Expected Rush Attempts MAE | 8.37 | 7.87 |

A real, coherent ~6% reduction at both layers, on top of the ~3% reduction
already found at the Expected Plays level (`CFB_XPLAYS.md`) — the layers
compound sensibly rather than one layer's noise swamping another's signal,
which is itself a useful confirmation that the `xDrives → xPlaysPerDrive →
xPassRate` decomposition is coherent.

As with `xplays.evaluate_expected_plays`, this is in-sample only; a genuine
train/test holdout (mirroring `xdrives.evaluate_advanced_model`) should be
added before any of this feeds a served projection.

## Files added / changed

```
docs/CFB_XVOLUME.md                    -- this document
sports_aggregator/cfb/xvolume.py       -- cfb_xvolume_dataset + baselines + Expected Volume
sports_aggregator/cfb/team_game_pace.py -- + pass_plays/rush_plays columns
tests/test_xvolume.py
tests/test_team_game_pace.py           -- + pass_plays/rush_plays assertion
```

CLI: `pbp_cli.py` gains `build-xvolume-dataset`, `evaluate-xvolume-baselines`,
and `evaluate-expected-volume`.

Not touched: red-zone leverage, turnovers, and everything from Milestone 9
onward.
