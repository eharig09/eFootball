# xDrives: Drive & Pace Estimator — design doc (Milestones 1–2)

Scope of this document: repository/data assessment, drive definition, schema, and
backtesting design for the possession-count layer of the eventual game-projection
engine. It does not touch yardage, scoring, or final-score prediction — those are
later milestones and are deliberately out of scope here.

## 1. What already existed

The CFB subsystem (`sports_aggregator/cfb/`) already has a large in-house
play-by-play analytics stack: `cfb_plays`/`cfb_play_metrics`/`cfb_drive_metrics`
(derived by `play_by_play.py`, 2022+ only), fitted EPA (`expected_points_v2.py`,
`expected_points_event.py`) and win probability (`win_probability_v2.py`) models,
a precomputed team-game efficiency table (`team_game_advanced.py` →
`cfb_team_game_advanced`), betting lines (`lines.py` → `game_lines`), and
ad-hoc (non-persisted) tempo functions in `pace.py` and `coordinator_pace.py`.

Relevant, and reused here rather than rebuilt:

- `cfb_play_metrics.garbage_time` — the one shared garbage-time definition
  (`period>=4 and margin>=22`, or `period==3 and margin>=29`). Reused as-is.
- `cfb_play_metrics.success` / `.explosive` — reused as-is for success and
  explosive rate.
- `cfb_drive_metrics` — per-drive plays/scrimmage_plays/points, reused instead
  of recomputing drive-level yardage/points from raw plays.
- `pace.py`'s snap-interval method (gap to the previous snap *on the same
  drive*, discarding gaps over 60 seconds as period/timeout artifacts) — the
  same rule, reimplemented here because `pace.py`'s own buckets are ad-hoc and
  request-scoped rather than persisted per team-game.
- `coordinator_pace.py`'s `team_drives_per_game` — real per-team drive counts
  already existed as a query-time function; this work persists an equivalent,
  more carefully defined count into a table so it can be joined and trended.
- `games.home_pregame_elo` / `away_pregame_elo` — the only Elo in the
  codebase; used as-is, already pregame-safe by construction.
- `game_lines` — consensus spread/total, used as-is.

Gaps this work fills: no table had drive counts *and* pace *and* style
together at team-game grain; no "meaningful drives" definition existed
(kneels and OT were never separated out); and nothing in the codebase builds
a leakage-safe trailing-feature dataset for backtesting — every existing
precomputed table (`cfb_team_game_advanced`, `cfb_drive_metrics`, etc.) is
purely postgame and has no point-in-time contract beyond
`pregame_snapshots.py`, which itself carries no pace/drive data yet.

Historical coverage ceiling: PBP-derived tables only go back to **2022**
(`games`/`team_stats` go back to 2015, but `cfb_plays` does not), so xDrives
is bounded to 2022+ until/unless older PBP is backfilled.

## 2. Drive definitions

A "drive" as stored today (`cfb_drive_metrics`, one row per `(game_id,
drive_id)`) is whatever CFBD's own `driveId` grouped together — it makes no
distinction between a normal possession, a kneel-out, a two-play
end-of-half snap, or an overtime possession. For a model that estimates *how
many meaningful possessions a team will get*, blending all of those in
distorts the target: a team that kneels out three times in blowouts doesn't
truly have "return an offense faster" behavior, and OT possessions don't
follow the same game-clock logic as regulation ones at all.

Two counts are therefore defined per team-game:

- **`raw_drives`** — every distinct drive from `cfb_drive_metrics` for that
  team as offense, in any period, no filtering. Kept for auditability.
- **`meaningful_drives`** — `raw_drives` restricted to:
  - **regulation only** (`period <= 4`; OT drives don't face a play clock
    or field position in the normal sense and are tracked separately as
    `ot_drives`, never blended into pace/count features), and
  - **not a victory-kneel drive**: a drive is a "kneel-out" and excluded when
    *every* play with a `down` on it is a kneel. Detected from
    `play_text LIKE '%kneel%'`, not `play_type`, because CFBD tags some
    kneels as `Rush` and others as `Uncategorized` — `play_type` alone
    under-detects them. A drive that already converted first downs and
    kneels out the final snap to end the game keeps its earlier real plays
    and is **not** excluded — only drives where literally every snap is a
    kneel (the true victory-formation case) are dropped.
  - Drives with **zero snaps at all** (a stray CFBD `drive_id` covering only
    a kickoff/timeout/end-of-period marker, no play with a `down` set) are
    also excluded — they are marker rows, not possessions.

This intentionally leaves untouched: turnover-started drives, drives cut
short by a defensive score on the *next* possession, and short end-of-half
drives that included a real, non-kneel snap attempt (e.g., a failed
last-second heave) — all of those are real possessions and should count.

`three_and_out_drives` is a documented approximation, since CFBD play data
has no explicit drive-ending-event field: a meaningful drive with **3 or
fewer plays that have a `down` set**, that **scored no points**
(`cfb_drive_metrics.points = 0`), and whose **final play was not an
interception or fumble recovery** (a turnover after 1–2 snaps is a turnover,
not a three-and-out, even though both end the drive quickly).

## 3. Team-game pace table (`cfb_team_game_pace`)

New module `sports_aggregator/cfb/team_game_pace.py`, following the exact
precompute pattern `team_game_advanced.py` and `team_game_tendencies.py`
already use: `schema_once`-guarded `initialize()`, a `build(repository, *,
from_season=None, to_season=None)` that deletes-and-reinserts the scoped
season range, `game_summary(repository, game_id)`, and
`team_weekly_trend(repository, team, season)`.

`metric_version = "team-game-pace-v1"`, versioned the same way as every
other derived table here, so redefining "meaningful" later doesn't silently
collide with old rows.

Columns (one row per `(game_id, team)`):

| Column | Meaning | Pregame-safe? |
|---|---|---|
| `raw_drives` | every drive, any period | no (postgame actual) |
| `meaningful_drives` | see §2 | no (postgame actual — the backtest target) |
| `ot_drives` | period ≥ 5 drives | no |
| `three_and_out_drives` | see §2 | no |
| `scrimmage_plays` | rush/pass plays on meaningful drives, non-garbage | no |
| `plays_per_meaningful_drive` | `scrimmage_plays / meaningful_drives` | no |
| `seconds_per_play` | same-drive snap interval, meaningful drives, non-garbage | no |
| `neutral_seconds_per_play` | same, restricted to `\|margin\| <= 8` | no |
| `pass_rate`, `neutral_pass_rate` | of scrimmage plays | no |
| `success_rate`, `explosive_rate` | reuses `cfb_play_metrics` flags | no |
| `first_down_rate` | plays with `down`,`distance`,`yards_gained` present and `yards_gained >= distance` — an approximation; CFBD has no explicit first-down flag | no |
| `three_and_out_rate` | `three_and_out_drives / meaningful_drives` | no |

Every column here is postgame — this table is the **actuals** side (what
happened), not a projection input by itself. It is also the thing a future
live xDrives model would need to trend *up to but excluding* the game being
projected — which is exactly what §4 builds.

## 4. Leak-safe dataset (`cfb_xdrives_dataset`)

New module `sports_aggregator/cfb/xdrives.py`. This is the Milestone 2
deliverable proper: one row per team-game carrying **only pregame-available
information** plus the actual outcome, for backtesting.

Leakage rule (spec §29, the most emphasized constraint in the assignment):
a team's trailing features are computed by walking that team's own game
history **in chronological order by `games.start_date`**, maintaining a
rolling window (last 12 games) of its own prior `cfb_team_game_pace` rows,
and reading the window's mean *before* appending the current game to it.
Nothing about the current game is ever in its own trailing average. This
also naturally lets a team carry its most recent prior-season form into week
1 of a new season rather than starting from nothing — full recency
weighting and Bayesian shrinkage toward a preseason prior are Milestone 4+
work (spec §27–28), not implemented here; the trailing window is an
unweighted mean over up to 12 games, simple by design so Milestone 3's
baselines can be compared against something equally simple.

Two rolling series are tracked per team, not one: the team's own trailing
**offense** figures (`team_prior_*`), and its trailing figures **as an
opponent** (`team_prior_drives_allowed`, i.e., how many meaningful drives
this team's defense has been giving up) — needed for Baseline C in §5, and
useful on its own as a defensive pace signal.

Columns:

```
game_id, team, opponent, season, week, home_away, dataset_version

actual_meaningful_drives          -- this team's actual result (the "y")
actual_game_total_drives          -- team + opponent actual, same game

team_prior_games                  -- trailing sample size (for later shrinkage/filtering)
team_prior_drives                 -- trailing mean of team's own meaningful_drives
team_prior_drives_allowed         -- trailing mean of drives team's defense allowed
team_prior_seconds_per_play
team_prior_neutral_seconds_per_play
team_prior_pass_rate
team_prior_neutral_pass_rate
team_prior_success_rate
team_prior_explosive_rate
team_prior_first_down_rate
team_prior_three_and_out_rate

opponent_prior_games
opponent_prior_drives
opponent_prior_drives_allowed
opponent_prior_seconds_per_play
opponent_prior_neutral_seconds_per_play
opponent_prior_pass_rate
opponent_prior_neutral_pass_rate
opponent_prior_success_rate
opponent_prior_explosive_rate
opponent_prior_first_down_rate
opponent_prior_three_and_out_rate

team_elo, opponent_elo            -- games.home_pregame_elo/away_pregame_elo, already pregame
market_spread, market_total       -- game_lines consensus (home-relative spread; negative = home favored)
team_implied_points, opponent_implied_points  -- total/2 -+ spread/2

built_at
```

`dataset_version = "xdrives-dataset-v1"`.

Leakage risk register (spec §3 Step 3 asks explicitly for this):

| Input | Risk | Mitigation |
|---|---|---|
| Trailing pace/drive features | current game leaking into its own average | rolling window built strictly before the game in chronological order |
| `home_pregame_elo`/`away_pregame_elo` | none — literally pregame by name | used as-is |
| `game_lines` spread/total | none in the predictive sense (a market price, not derived from the game's own result); but `fetched_at` is not staged the way `pregame_snapshots.py` stages market data, so a line refreshed unusually late could reflect very-late injury news the model shouldn't get credit for foreseeing | flagged; not corrected in this milestone (would require the `pregame_snapshots` T-24H/T-3H staging, out of scope until xDrives is served live) |
| First game of a team's history (no trailing window) | `team_prior_*` is `None` | rows with `team_prior_games == 0` are kept but excluded from baseline evaluation by default (reported separately) |

## 5. Baselines (Milestone 3)

Implemented in `xdrives.py` as `evaluate_baselines(repository, *,
from_season=None, to_season=None)`, following `model_validation.py`'s
accumulator/MAE/RMSE/bias pattern exactly (same shape as `validate_edp`).

- **Baseline A — league average.** Predict every team's drives as the
  dataset-wide mean of `actual_meaningful_drives`.
- **Baseline B — team average.** Predict `team_prior_drives` (the team's own
  trailing mean, ignoring the opponent entirely).
- **Baseline C — team & opponent-allowed blend.** Predict
  `(team_prior_drives + opponent_prior_drives_allowed) / 2`, the exact
  formulation in spec §30.
- **Baseline D — simple linear regression.** A 2-feature OLS fit
  (`team_prior_drives`, `opponent_prior_drives_allowed`) solved by closed-form
  normal equations in pure Python (no numpy — it is not a production
  dependency; see `requirements.txt`). Deliberately simple: this is a
  ceiling check on whether Baseline C's fixed 50/50 blend is already close to
  the data-driven weighting, not a first attempt at the "advanced" model.

Each is reported overall and by season, by conference is deferred (needs a
team→conference join not yet wired into this dataset — cheap to add once
the advanced model needs it).

Explicitly **not** built in this milestone: opponent-adjustment beyond the
simple allowed/drives-allowed pairing above, PROE, in-house Elo, game-script
conditioning, or anything from Milestone 5 onward. The assignment is
explicit that the drive-count problem's difficulty should be understood
before reaching for those.

## 6. Advanced model (Milestone 4)

Baseline D already showed a 2-feature regression barely beats Baseline C's
fixed 50/50 blend in-sample — the honest next question is whether a richer,
still-linear model beats the baselines **out of sample**, which is what
Milestone 4 actually asks for ("build an advanced model... compare it to the
baseline").

`ADVANCED_FEATURES` in `xdrives.py` adds pace, opponent quality and market
context to Baseline D's two drive-count features:

```
team_prior_drives, opponent_prior_drives_allowed        -- same as Baseline D
team_prior_seconds_per_play, opponent_prior_seconds_per_play  -- combined pace
team_prior_pass_rate                                     -- style
team_prior_success_rate, opponent_prior_success_rate     -- efficiency both ways
elo_diff (= team_elo - opponent_elo)                      -- opponent quality
market_total                                              -- scoring environment
is_home                                                    -- game context
```

`elo_diff` and `is_home` are derived at fit/predict time from columns already
in `cfb_xdrives_dataset` rather than stored as their own columns.

Two functions, mirroring how `win_probability_v2.py` separates fitting from
scoring:

- **`fit_advanced_model(repository, *, from_season=None, to_season=None, l2=1.0)`**
  fits a ridge-regularized OLS (closed-form normal equations, still no
  numpy) and persists coefficients to a new `cfb_xdrives_model` table, keyed
  by `model_version` the same versioning way every other fitted model here
  is. The L2 penalty exists because several of these ten features move
  together (a team's own tempo and its opponent's, for instance) and a few
  seasons of games is not many rows for a ten-feature fit — `l2=0` recovers
  plain OLS if a caller wants it.
- **`evaluate_advanced_model(repository, *, train_from, train_to, test_from, test_to)`**
  fits only on the training season range and scores strictly on the held-out
  test range — a genuine temporal holdout, unlike `evaluate_baselines`'
  Baseline A/D, which are explicitly disclosed as in-sample diagnostics of
  problem difficulty rather than predictive-performance claims. Baselines B
  and C need no fitting, so they are reported on the same held-out test rows
  for a like-for-like comparison against the advanced model (labeled `E` in
  the output to avoid confusion with the in-sample `D`).

This is still a linear model — per spec §37 ("use the simplest model that
provides competitive predictive accuracy"), a tree/boosting model is only
worth reaching for once a genuine holdout comparison shows the linear model
underfitting the relationship, which `evaluate_advanced_model` is exactly
the tool to check.

## 7. Three calibration levers, honestly evaluated (post-Milestone-4)

After Milestone 4's first out-of-sample check (train 2022–2024, test 2025)
found every baseline under-predicting 2025 drives by a consistent margin,
three fixes were tried, each evaluated the same rigorous way: a genuine
temporal holdout (never the same seasons used to fit), and — critically —
every comparison scored on identical rows (`test_results_matched` /
`overall_matched`) after discovering the first attempt at each of these
comparisons was accidentally scoring different baselines on different-sized
row sets, which flatters whichever baseline has looser eligibility
requirements. Two independent holdouts were checked for each lever (2022–24→
2025 and 2022–23→2024) so a conclusion doesn't rest on one season's quirks.

**Recency weighting (spec §27)** — `_trailing_summary`'s flat mean over the
last `TRAILING_WINDOW_GAMES` became an exponential-decay mean
(`RECENCY_LAMBDA`, half-life `RECENCY_HALF_LIFE_GAMES=8` games). Result: a
real but modest win — cut Baseline C's bias by roughly a quarter to a third
and nudged its MAE down slightly. `dataset_version` bumped to
`xdrives-dataset-v2`; `v1` (flat mean) is left in the table under its own
version for direct comparison.

**League environment term (spec §6)** — `league_prior_drives`: a leaguewide,
leak-safe, date-batched (never same-day) trailing level, independent of any
one team's schedule. Exposed as `ADVANCED_FEATURES` input (feeds the F
regression) and as a hand-picked, unfit `E_environment_blend` baseline
(`ENV_BLEND_TEAM_WEIGHT=0.7`). Result: **did not beat Baseline C on MAE on
either holdout** (2025: 1.506 vs C's 1.500; 2024: 1.629 vs C's 1.623), though
it did consistently shave a little off the bias both times. The theory —
"the whole league sped up and no team-specific window can see it coming" —
turned out not to be the dominant story, or at least not one this
implementation captured well enough to pay for its own complexity.

**Shrinkage (spec §28)** — `_shrink()`: empirical-Bayes blend of a team's own
trailing drives/drives-allowed toward `league_prior_drives`, weight
`games / (games + SHRINKAGE_PSEUDO_GAMES)` with `SHRINKAGE_PSEUDO_GAMES=4`.
Stored as `team_prior_drives_shrunk` / `team_prior_drives_allowed_shrunk` /
opponent equivalents, and a `G_shrunk_blend` baseline (Baseline C's formula
on the shrunk fields). Result: **also did not beat Baseline C on MAE on
either holdout** (2025: 1.504 vs 1.500; 2024: 1.627 vs 1.623) — in-sample
checks made it look like a clear winner (1.557 vs C's 1.572 on the full
2022–2025 span), which is exactly the in-sample-vs-holdout gap this project
brief warns about (spec §29–30) and the reason every one of these levers was
re-checked out of sample before being trusted.

Shrinkage's genuine, unambiguous win is different from accuracy on already-
covered games: because `_shrink` is defined at zero trailing games (it falls
back outright to the league prior), it produces a usable prediction for true
week-1 cold starts that every other baseline had to skip entirely.
`evaluate_baselines`' `cold_start_recovery` reports this directly — across
2022–2025, 236 of 238 cold-start rows get a prediction with MAE 1.64
(worse than the established-team baselines, as expected for zero own
history, but far better than no prediction at all).

**Where this leaves the model choice:** three independent levers, three
honest non-wins on MAE against the simplest opponent-adjusted blend. That is
itself a real finding, not a failure to find the right hyperparameter — the
temptation to keep tuning `RECENCY_HALF_LIFE_GAMES`, `ENV_BLEND_TEAM_WEIGHT`
or `SHRINKAGE_PSEUDO_GAMES` until one of these wins on one of these two
holdouts was deliberately not taken, since that is fitting the holdout
rather than the problem. **Baseline C (or D) remains the recommended xDrives
point estimate for established teams; G is the recommended choice
specifically when a true cold-start prediction is needed** (week 1 of a
season, a team with no stored history at all), since nothing else produces
one. Further accuracy gains likely need genuinely new information (personnel,
returning production, explicit game-script modeling) rather than more
combinations of the same trailing pace signals.

## 8. Files added

```
docs/CFB_XDRIVES.md                       -- this document
sports_aggregator/cfb/team_game_pace.py   -- cfb_team_game_pace (actuals)
sports_aggregator/cfb/xdrives.py          -- cfb_xdrives_dataset + baselines + advanced model
tests/test_team_game_pace.py
tests/test_xdrives.py
```

CLI: `pbp_cli.py` gains `build-team-pace`, `build-xdrives-dataset`,
`evaluate-xdrives-baselines`, `fit-xdrives-advanced`, and
`evaluate-xdrives-advanced`, alongside the existing `build-team-advanced`/
`validate-edp` commands. The first three use the same `--year`/`--from-year`/
`--to-year` flags as the rest of `pbp_cli.py`; `evaluate-xdrives-advanced`
additionally requires `--train-from-year`/`--train-to-year`/
`--test-from-year`/`--test-to-year` since it needs two disjoint season
ranges, and both new fit/evaluate commands accept `--xdrives-l2` to override
the default ridge penalty.

Not touched: `pregame_snapshots.py` (no pace/drives call added yet —
premature until xDrives is served live rather than backtested), and nothing
about yardage, scoring, or final score.
