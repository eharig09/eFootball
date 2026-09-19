# xPlaysPerDrive and Expected Plays (Milestone 5)

Scope: `Expected Plays = xDrives × xPlaysPerDrive` (spec §18-19). Builds on
the xDrives work in [docs/CFB_XDRIVES.md](CFB_XDRIVES.md) rather than
duplicating it — xDrives answers "how many possessions", this answers "how
many plays per possession".

## Design

New module `sports_aggregator/cfb/xplays.py`, matching the project brief's
own suggested architecture split (`pace/drive_model.py` vs
`offense/play_volume.py`, §32) rather than folding this into the
already-large `xdrives.py`.

`cfb_xplays_dataset` — one row per team-game, built the same leak-safe,
chronologically-walked way `xdrives.build_dataset` walks
`cfb_team_game_pace`:

- `actual_plays_per_drive` / `actual_scrimmage_plays` — the target and its
  exact play count (`team_game_pace.plays_per_meaningful_drive` /
  `.scrimmage_plays` for that game).
- `team_prior_plays_per_drive`, `opponent_prior_plays_per_drive` — each
  team's own trailing plays/drive.
- `team_prior_plays_per_drive_allowed`, `opponent_prior_plays_per_drive_allowed`
  — the defensive mirror (what a team's *opponents* have needed per drive
  against it), exactly parallel to xDrives' `drives_allowed`.
- `team_prior_success_rate` / `_explosive_rate` / `_first_down_rate` /
  `_three_and_out_rate` (+ opponent equivalents) — the efficiency drivers
  spec §18 names as inputs to plays/drive. Penalties, sack rate and turnover
  rate are not included: they are not yet columns on `cfb_team_game_pace`
  (turnovers are Milestone 9's own model), and this file only claims what is
  actually measured.

Recency weighting reuses xDrives' already-tuned half-life
(`xdrives.RECENCY_LAMBDA`, `TRAILING_WINDOW_GAMES`) rather than deriving a
new one — there is no evidence a team's plays-per-drive identity decays at a
different rate than its drive count does, and re-deriving one on no evidence
would be exactly the over-tuning spec §37 warns against.

## Deliberate scope decision: no repeat of the environment/shrinkage experiment

xDrives' Milestone 4 follow-up tried three levers (recency weighting,
leaguewide environment term, empirical-Bayes shrinkage) and found only
recency weighting held up out of sample; the other two looked like wins
in-sample and were not, once checked on a genuine train/test holdout (see
`CFB_XDRIVES.md` §7). Re-running that same three-lever experiment for
plays/drive without first establishing the baseline is difficult here would
be process for its own sake. `xplays.py` therefore ships with only the
direct analogs of Baselines A–D:

- **A** — league average plays/drive.
- **B** — team's own trailing plays/drive.
- **C** — `(team_prior_plays_per_drive + opponent_prior_plays_per_drive_allowed) / 2`.
- **D** — ridge regression on B/C's two features plus
  `team_prior_success_rate` and `opponent_prior_three_and_out_rate`.

`evaluate_baselines(repository, ...)` reports these the same way
`xdrives.evaluate_baselines` does, with the same honest disclosure that A
and D are in-sample diagnostics, not held-out performance claims. In-sample
on 2022–2025: league average plays/drive ≈ 5.34; C improves MAE over A/B
(1.15 vs 1.16–1.20); D a little further (1.13). If a future holdout check
shows C underfitting, add recency/environment/shrinkage analogs then — not
before.

## Expected Plays

`evaluate_expected_plays(repository, *, from_season=None, to_season=None)`
joins `cfb_xdrives_dataset` and `cfb_xplays_dataset` on `(game_id, team)` and
compares two projections against actual scrimmage plays:

- **`naive_constant`** — the same league-wide drives × plays-per-drive
  product for every team-game.
- **`combined_matchup_blend`** — each side's own Baseline C (opponent-adjusted
  blend), multiplied: `((team_prior_drives + opponent_prior_drives_allowed)/2)
  × ((team_prior_plays_per_drive + opponent_prior_plays_per_drive_allowed)/2)`.

Deliberately reuses each dataset's own Baseline C rather than inventing a
third, untested combination rule. On 2022–2025 (in-sample; no train/test
split yet — this checks whether the combination is coherent, not final
predictive performance): naive MAE 11.65 plays, combined-blend MAE 11.35 —
a real if modest reduction, and the bias drops from +2.02 to +1.00. A
genuine holdout comparison (mirroring `xdrives.evaluate_advanced_model`)
would be the next step if this combination is promoted toward a served
projection.

## Files added

```
docs/CFB_XPLAYS.md                  -- this document
sports_aggregator/cfb/xplays.py     -- cfb_xplays_dataset + baselines + Expected Plays
tests/test_xplays.py
```

CLI: `pbp_cli.py` gains `build-xplays-dataset`, `evaluate-xplays-baselines`,
and `evaluate-expected-plays`, using the same `--year`/`--from-year`/
`--to-year` flags as the rest of the file.

Not touched: run/rush attempt split (Milestone 19), passing/rushing yardage
(Milestone 20), and everything from turnovers/red-zone onward.
