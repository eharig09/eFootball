# CFB 12-team playoff projection

`/college-football/playoff/` (and `/api/v1/cfb/playoff`) simulates the rest of the season
and reports each team's odds to win its conference, make the field, earn a bye, and win
the national championship.

```
python -m sports_aggregator.cfb.playoff_cli forecast --sims 10000   # live table
python -m sports_aggregator.cfb.playoff_cli committee               # refit + out-of-sample accuracy
python -m sports_aggregator.cfb.playoff_cli backtest                # calibration on 2023-25
```

## Pipeline

| Module | Job |
|---|---|
| `playoff_state.py` | Leak-safe season snapshot as of a week (results, Elo, posted lines) and the ratings fit |
| `playoff_rules.py` | Pure rules: conference tiebreakers, title games, 5+7 selection, seeding, bracket |
| `playoff_sim.py` | Monte Carlo: games -> standings -> title games -> ratings -> committee ranking -> field -> bracket |
| `playoff_committee.py` | Fits/validates the committee-ranking model on final committee rankings |
| `playoff_backtest.py` | Scores historical mid-season forecasts against what happened |
| `playoff_service.py` / `playoff_view.py` | Cached forecast and page presentation |

## Game model

- **Ratings** (points vs an average FBS team): weighted ridge over actual margins
  (weight 1, capped at 28), market lines (weight 2), pulled toward an Elo prior
  (24.35 Elo = 1 point; weight 2 "games"). Parameters were tuned on 2023-25 by predicting
  games two or more weeks past each snapshot; lines were the only input that moved the
  error materially (MAE 13.25 -> 12.80), and margins added little.
- **Spread**: the posted line when there is one, else rating difference + 3.0 home field.
  Lines exist for roughly the next week, so most of the schedule is rating-priced.
- **Noise**: total residual vs the closing line is 15.2 points; 4.0 of it is a persistent
  per-team strength error drawn once per simulated season (so a team that is secretly
  good wins a lot of games together). The 4.0 was picked by backtest log loss; playoff
  odds are insensitive to it, conference-title odds improve with larger values.
- **Ratings update inside the simulation**: end-of-season ratings are re-solved from the
  simulated results with the same ridge system (exactly, via one matrix inverse), so a
  simulated 12-0 team gets the rating bump a real one would.

## Season structure

- Conference standings: conference win %, then record among the tied teams (head-to-head
  for two), then committee strength. This stands in for common-opponent and ranking
  steps that cannot be resolved without per-conference rule books.
- The top two teams play a title game in each of the ten conferences
  (`TITLE_GAME_CONFERENCES`). The Sun Belt sends its East and West division winners (the one
  with the better conference record hosts). The Pac-12, American, Conference USA and Mountain
  West games are on the higher seed's campus (3-point home edge); ACC, Big Ten, Big 12, SEC
  and MAC games are neutral. Source: the 2026-27 CFP guide.
- Format lives in `CFPFormat`, defaulting to the 2026-27 rules: 12 teams; automatic bids to the
  ACC, Big 12, Big Ten and SEC champions regardless of ranking plus the highest-ranked champion
  of the American, CUSA, MAC, Mountain West, Pac-12 and Sun Belt; Notre Dame gets an automatic
  bid if ranked in the top 12 (then only six at-large); everyone else is at-large by ranking.
  Seeds 1-4 are the four highest-ranked teams (byes, champion or not), 5-12 follow the ranking,
  and an automatic qualifier ranked outside the top 12 is the bottom seed. No rematch
  avoidance, no re-seeding. Bracket: 5v12, 6v11, 7v10, 8v9, then 1 v 8/9, 2 v 7/10, 3 v 6/11,
  4 v 5/12, then (1 side) v (4 side) and (2 side) v (3 side). Round one is at the higher seed;
  later rounds are neutral. The earlier rules remain selectable (`auto_rule="five_best_champions"`,
  `seeding="champion_byes"`).

## Committee model

The committee publishes no formula, so it is learned: a ridge regression of final committee
position (top 25, plus the best-rated unranked teams as negatives) on rating, strength of
record (results vs a 20th-best-team baseline), losses, conference championship, strength of
schedule and win %. Trained on the 2023-25 final rankings.

Leave-one-season-out (fit on two seasons, test on the third), correct playoff teams out of 12:

| Season | Teams right | Missed / extra |
|---|---|---|
| 2023 | 11 | Missouri / LSU |
| 2024 | 11 | SMU / Alabama |
| 2025 | 11 | Tulane / James Madison |

The misses are the judgment calls a regression cannot see. (Under the earlier five-best-champions
rule the 2025 miss was Miami / Notre Dame.) Three seasons is a small training set; treat the weights as a first fit.

## Forecast backtest (2023-25, weeks 4/7/10/12, 1,612 team-snapshots)

Truth = the field the actual final ranking and actual champions give under the 2026-27 rules.
Committee model is always the one not trained on that season.

| As of week | Brier skill vs base rate |
|---|---|
| 4 | 0.50 |
| 7 | 0.62 |
| 10 | 0.67 |
| 12 | 0.72 |

Calibration is close to the diagonal across bins (e.g. predicted 0.39 / actual 0.35 for the
30-50% bin). Known weakness: the 90%+ bin is overconfident (predicted 0.973, actual 0.937,
63 observations), so near-locks early in the season are somewhat too sure.

## Assumptions to revisit

- The guide does not say where the Sun Belt title game is played; the better-record division
  winner is assumed to host (as in 2025).
- Historical truth fields treat the Pac-12 as a Group of 6 conference in every season.
- Ratings are fixed within a simulation except for the post-season re-solve; injuries,
  QB changes and weather are only in the model through the market lines.
- Only 2023-25 final committee rankings are in the database, and 2023 was ranked for a
  4-team playoff; they are used here as ranking behavior, not as 12-team selections.
- First-round home-field uses the same 3-point home edge as a regular game.
- The page computes on first request after data changes (a few seconds, cached by
  data fingerprint). It is not yet part of the scheduled refresh segments.
