# NFL playoff projection

`/nfl/playoffs/` (and `/api/v1/nfl/playoffs`) simulates the rest of the regular season and
reports each team's odds to win its division, make the playoffs, earn the bye, and win the
Super Bowl.

```
python -m sports_aggregator.nfl.playoff_cli forecast --sims 10000   # live table
python -m sports_aggregator.nfl.playoff_cli backtest                # calibration on 2015-2025
```

The CFB version (`docs/CFB_PLAYOFF.md`) shares the architecture; the differences are
below.

## Pipeline

| Module | Job |
|---|---|
| `playoff_state.py` | Leak-safe snapshot as of a week (results, Elo, posted lines) and the ratings fit |
| `tiebreakers.py` | Pure NFL tiebreakers and seeding over team-pair matrices |
| `playoff_bracket.py` | Pure bracket: byes, wild card, per-round re-seeding, neutral-site Super Bowl |
| `playoff_sim.py` | Monte Carlo: scores -> pairwise results -> tiebreakers -> seeds -> bracket |
| `playoff_backtest.py` | Scores historical mid-season forecasts against what happened |
| `playoff_service.py` / `playoff_view.py` | Cached forecast and page presentation |

## Game model

- **Ratings** (points vs an average team): weighted ridge over margins (weight 1, capped at 24),
  market lines (weight 2), pulled toward an Elo prior (50 Elo = 1 point, weight 2 "games").
  Tuned on 2015-25 by predicting games two or more weeks past each snapshot: Elo-only MAE 10.68,
  tuned 10.42. Lines are the input that matters; the surface is flat around the chosen values.
- **Spread**: the posted line when there is one (`spread_line`, positive = home favored), else
  rating difference + 1.8 home edge (0 at neutral sites). Only the next week or so of games has
  lines, so most of the schedule is rating-priced.
- **Noise**: 12.7 points around a line (the residual vs closing lines, 2015-25). Rating-priced
  games also get a persistent per-team strength error (sd 4.5, chosen by backtest log loss; the
  smaller value implied by the raw residual left the 90%+ bin overconfident).
- **Scores**: integer margins with totals from the posted total (or the 45.6 league mean, sd 13.2),
  so points for/against are real inputs to the tiebreakers. A game level after regulation is
  decided by a 3-point overtime margin, except 12% stay tied (about the real 0.3% tie rate).
- **Ratings update inside the simulation**: end-of-season ratings are re-solved from the simulated
  results (one matrix inverse), and those drive the playoff games.

## Tiebreakers (`tiebreakers.py`)

Implements the NFL's published procedures:

- Division: head-to-head, division record, common games, conference record, strength of victory,
  strength of schedule, combined points scored/allowed ranking (conference, then league), net points
  in common games, net points overall.
- Wild card and seeding across divisions: head-to-head (sweep only with three or more clubs),
  conference record, common games (minimum four), strength of victory, strength of schedule, combined
  ranking (conference, then league), net points in conference games, net points overall. Clubs from
  the same division are first reduced to that division's best club.
- After any step singles out a club (or smaller group) the rest restart from the first step.
- Not modelled: net touchdowns. A coin toss settles anything still level.

**Validation against reality**: the engine reproduces, for every season 2010-2025 (16 seasons), the
actual playoff teams in both conferences, the bye teams, and exactly which clubs hosted wild-card games
(i.e. seeds 2-4, or 3-4 in six-team years). Tests cover the individual rules; the historical check needs
the full database and is not part of the unit suite.

## Playoff format

Seven teams per conference since 2020 (one bye), six before (two byes); `playoff_service.teams_per_conference`
picks by season. Division winners take seeds 1-4 by record, the best remaining records take the wild cards.
Wild card round: 2v7, 3v6, 4v5. After every round the bracket re-seeds: best remaining seed hosts the worst.
The higher seed hosts through the conference championship (1.8-point home edge); the Super Bowl is neutral.

## Forecast backtest (2015-2025, weeks 4/8/12, 1,056 team-snapshots per target)

Truth = who actually made the playoffs / won the division / got the bye. Brier skill vs base rate:

| Target | Week 4 | Week 8 | Week 12 | All |
|---|---|---|---|---|
| Make playoffs | 0.32 | 0.46 | 0.61 | 0.46 |
| Win division | 0.31 | 0.45 | 0.54 | 0.44 |
| Earn bye | 0.20 | 0.41 | 0.53 | 0.38 |

Calibration for making the playoffs: predicted 0.399 / actual 0.432 (30-50% bin), 0.602 / 0.612
(50-70%), 0.799 / 0.776 (70-90%), 0.964 / 0.956 (90%+). The bye target is the weakest (9-12 observations
in its top bins; treat those probabilities as rough).

## Assumptions and limits

- Injuries and QB changes enter only through the market lines. Rating-priced games past the next week
  do not know about them (a QB-aware rating exists in the app's margin work but is not promoted).
- Ratings are fixed within a simulation apart from the post-season re-solve used for playoff games.
- Production's NFL database holds only the 2026 season: that is
  enough for the live forecast, but the backtest needs the local 2010-2025 archive.
- The forecast computes on first request after data changes (a few seconds, cached by data fingerprint);
  it is not yet part of the scheduled refresh.
- Draft order, strength-of-schedule remaining and "what a win is worth" are natural extensions and are
  not built.
