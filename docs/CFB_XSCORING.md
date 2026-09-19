# Turnovers and Red-Zone Opportunity (Milestone 9)

This increment extends the drives -> plays -> volume -> yardage chain with two
separate, deliberately conservative models. It still does **not** project final
points: red-zone touchdowns are only one drive outcome, and field position,
non-red-zone scores, field goals, safeties, and special teams remain separate
future components.

## Team-game actuals

`team_game_scoring.py` builds `cfb_team_game_scoring`, one row per team-game,
from the same meaningful regulation drives used by `team_game_pace.py`.
Garbage-time plays are excluded from model events.

Turnovers count only:

- interceptions (`Interception`, `Pass Interception Return`, and interception
  return touchdowns); and
- opponent-recovered fumbles (`Fumble Recovery (Opponent)` and fumble return
  touchdowns).

Own-team fumble recoveries do not count. A defensive return touchdown counts as
a giveaway for the offense but never as an offensive touchdown.

A red-zone trip is a meaningful drive with a competitive play at or inside the
opponent 20. Goal-to-go requires a down/distance state where distance is at
least yards to goal. Offensive touchdowns require a scoring rush/pass play and
explicitly exclude defensive and special-teams return scores.

The 2022-2025 build produced 7,317 team-game rows. The comparisons below are
in-sample component diagnostics; a season-held-out evaluation is still required
before promoting a richer model over any winning simple baseline.

## Turnover model

`xturnovers.py` builds a leak-safe, recency-weighted giveaway-rate dataset.
Rates are weighted event/exposure ratios (`giveaways / competitive plays`), not
an unweighted average of game rates. Baselines on 6,961 eligible team-games:

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | 0.014503 | 0.019068 | +0.000492 |
| Team trailing rate | 0.015417 | 0.020368 | -0.000017 |
| Offense/defense blend | 0.014950 | 0.019686 | +0.000003 |
| Blend shrunk by six pseudo-games | 0.014556 | 0.019141 | +0.000097 |

The league prior wins. That is consistent with the specification's warning
that turnover rates contain substantial randomness. The live projection keeps
the matchup rate for diagnosis but serves `league giveaway rate x xPlays`
until a held-out model demonstrates real improvement.

## Red-zone model

`xredzone.py` independently models trips per drive and touchdown conversion.
The rates use recency-weighted counts over their correct exposure (drives for
trips, trips for touchdowns).

### Trips per drive (6,961 eligible team-games)

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | 0.123862 | 0.154485 | -0.010403 |
| Team trailing rate | 0.122550 | 0.154465 | -0.005055 |
| Offense/defense blend | **0.121356** | **0.152111** | -0.006095 |

The opponent-adjusted blend earns its place for trip creation.

### Touchdowns per red-zone trip (6,599 eligible team-games)

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | **0.248250** | **0.305352** | +0.010941 |
| Team trailing rate | 0.257725 | 0.319846 | +0.006557 |
| Offense/defense blend | 0.252442 | 0.312388 | +0.007351 |

Conversion is noisy enough that the league prior wins. The live path therefore
uses opponent-adjusted trips but the pregame league conversion rate. It retains
the matchup conversion estimate for auditability rather than allowing a weaker
estimate to drive the served result.

## Live projection and next step

`game_projection.py` now adds expected giveaways, red-zone trips, and red-zone
touchdowns to each team packet and the matchup page/API. For example, the
2026-09-19 Georgia at Arkansas smoke test produced 1.14 vs 0.99 expected
giveaways and 4.11 vs 3.14 red-zone trips.

Milestone 10 is now documented in
[`CFB_XFIELD_POSITION.md`](CFB_XFIELD_POSITION.md). Drive-outcome expected
points remains next; the current red-zone touchdown output is not a final
scoring projection.

## Files

```
sports_aggregator/cfb/team_game_scoring.py
sports_aggregator/cfb/xturnovers.py
sports_aggregator/cfb/xredzone.py
tests/test_xscoring.py
docs/CFB_XSCORING.md
```

CLI commands:

```
build-team-scoring
build-xturnovers-dataset
evaluate-xturnovers-baselines
build-xredzone-dataset
evaluate-xredzone-baselines
```
