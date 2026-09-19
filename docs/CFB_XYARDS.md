# Expected Passing/Rushing Yards (spec section 20)

Scope: `ExpectedPassYards = ExpectedDropbacks × ExpectedYardsPerDropback`
and `ExpectedRushYards = ExpectedRushAttempts × ExpectedYardsPerRush` — the
fourth layer on `xDrives → xPlaysPerDrive → xPassRate → xYards`. Same
modular split as [docs/CFB_XVOLUME.md](CFB_XVOLUME.md): a new file,
`sports_aggregator/cfb/xyards.py`.

## Prerequisite: raw yardage on `cfb_team_game_pace`

Added `pass_yards`, `rush_yards` (raw totals), `yards_per_dropback`,
`yards_per_rush` (rates) to `cfb_team_game_pace`, computed in the same
`_play_rates` pass that already accumulates success/explosive/first-down
totals per play (it already had `yards_gained` in hand for the first-down
check; this just also sums it by rush/pass). Migrated onto the existing
table the same `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` way as
`pass_plays`/`rush_plays` were in Milestone 6.

## Design

`cfb_xyards_dataset` tracks **two parallel metrics** (passing and rushing),
each with its own offense × defense interaction — spec §20's own examples
treat `f(PassOffense, PassDefense, ...)` and `f(RushOffense, RunDefense,
...)` as different functions, not one shared efficiency number:

- `team_prior_yards_per_dropback` / `team_prior_yards_per_rush` — a team's
  own trailing offensive efficiency, each independently recency-weighted.
- `team_prior_yards_per_dropback_allowed` / `team_prior_yards_per_rush_allowed`
  — the defensive mirror: what a team's *opponents* have gained per
  dropback/rush against it. Tracked as two separate "allowed" windows (not
  one shared one) — a defense can be stout against the run and porous
  against the pass in the same game, and collapsing them would hide that.
- `team_prior_success_rate` / `team_prior_explosive_rate` — efficiency
  drivers, reused as Baseline D regression features.

## Baselines and result

`evaluate_baselines(repository, ...)` returns `passing` and `rushing` as
independent sub-reports, each with the same A–D shape as every other
xdrives/xplays/xvolume evaluator (A/D in-sample diagnostics, B/C leak-safe
by construction). In-sample on 2022–2025:

| | league avg (A) | team avg (B) | opponent-adjusted blend (C) | regression (D) |
|---|---|---|---|---|
| Passing (yds/dropback) MAE | 2.067 | 2.099 | 1.966 | 1.951 |
| Rushing (yds/rush) MAE | 1.337 | 1.372 | 1.279 | 1.268 |

Same pattern as every layer below this one: the opponent-adjusted blend (C)
is where almost all the real gain is (~5–7% MAE reduction over a plain
average), and the regression (D) adds only a little further.

## Expected Yardage

`evaluate_expected_yardage(repository, ...)` chains xDrives' → xPlaysPerDrive's
→ xVolume's → xYards' own Baseline C predictions all the way down (never
substituting a different combination rule partway through), split into
Expected Pass Yards and Expected Rush Yards, backtested against actual
yardage. In-sample on 2022–2025:

| | naive (league constants) | combined matchup blend |
|---|---|---|
| Expected Pass Yards MAE | 77.7 | 72.6 |
| Expected Rush Yards MAE | 58.9 | 54.3 |

A real, coherent ~6–8% reduction, consistent with the compounding
improvement already seen at the Expected Plays and Expected Volume layers
(`CFB_XPLAYS.md`, `CFB_XVOLUME.md`) — four layers of opponent-adjusted
blends chain together sensibly rather than one layer's noise swamping the
others' signal.

As with every prior in-sample check in this chain, a genuine train/test
holdout (mirroring `xdrives.evaluate_advanced_model`) should be added before
any of this feeds a served projection.

> Milestone 9 is now implemented separately in
> [`CFB_XSCORING.md`](CFB_XSCORING.md). The original "not touched" statement
> below records the boundary of the yardage increment, not the current project
> boundary.

## Matchup-page projection integration (2026-09-19)

`sports_aggregator/cfb/game_projection.py` is the live, point-in-time adapter
over the four historical datasets. It reads each team's recency-weighted
trailing `cfb_team_game_pace` rows strictly before the selected game's kickoff
and applies the same Baseline C offense/defense-allowed blend at every layer.
It therefore works for an upcoming game that has no actuals row of its own and
retains the historical backtest's no-current-game-leakage contract.

The matchup page now has a dedicated **Projection** tab showing:

- xDrives and xPlays per drive, then their xPlays product;
- xPass rate, xDropbacks, and xRush attempts;
- xYards per dropback/rush and expected pass, rush, and total yards;
- each team's trailing sample size and a plain-language read.

The same stable packet is exposed at
`/api/v1/cfb/games/<game_id>/projection` and embedded in the existing preview
API. That packet is the boundary for the next increment: allocate team
dropbacks, rush attempts, and yardage to players using point-in-time roster,
availability, and opportunity shares without changing the game-volume model.

This is labeled as an early team-opportunity/yardage model, not a points, win
probability, or betting projection. The full chained model still needs a true
held-out evaluation before those stronger claims are warranted.

## Files added / changed

```
docs/CFB_XYARDS.md                     -- this document
sports_aggregator/cfb/xyards.py        -- cfb_xyards_dataset + baselines + Expected Yardage
sports_aggregator/cfb/team_game_pace.py -- + pass_yards/rush_yards/yards_per_dropback/yards_per_rush
tests/test_xyards.py
tests/test_team_game_pace.py           -- + yardage assertions
```

CLI: `pbp_cli.py` gains `build-xyards-dataset`, `evaluate-xyards-baselines`,
and `evaluate-expected-yardage`.

Not touched: red-zone leverage, turnovers, and everything from Milestone 9
onward (spec's own numbering) — the drives→plays→volume→yardage chain
(spec §18–20) is now complete end to end.
