# NFL engine research status

## Current decision

Football Lab remains an independent, market-free forecast. The displayed ATS
and total outputs are **experimental model leans**. Model-to-market distance is
labelled **separation**, not confidence: the 2014-2025 walk-forward audit did
not show larger disagreement producing better results.

The preferred football-only challenger is the expanding-window margin model
with recent margin, quarterback EPA, and quarterback CPOE. On its 3,195-game
common sample it reduced margin MAE from 10.2362 (core) to 10.1597. This is a
real but small research improvement and is not yet the production model.

## Public perception and line movement

The repository does not currently contain ticket counts, bet percentages, or
money percentages. Two viable licensed sources are:

- [SportsDataIO NFL Betting Splits](https://sportsdata.io/developers/data-dictionary/nfl)
  exposes bet and money percentages, with coverage listed from 2021.
- [Sportradar Betting Insights](https://developer.sportradar.com/insights/v2/reference/betting-insights-overview)
  describes consensus splits compiled from more than 150 bookmakers. It is a
  separately permissioned product.

Neither API key is configured in the current environment. Until a feed is
licensed, narrative features are explicitly named perception **proxies** and
must not be presented as public betting data.

`market_line_snapshots` now records every changed nflverse spread, total,
moneyline, and price state during schedule refreshes. Identical refreshes are
deduplicated. This creates a forward-looking first-observed/current/closing
movement history; the old canonical schedule rows cannot reconstruct true
historical openers or intermediate moves.

## Narrative challenger v1

The challenger snapshots an entire week before adding any result from that
week. It combines:

- prior-season win percentage and scoring margin as clearly labelled preseason
  proxies;
- current-season points and win percentage entering the game;
- each team's immediately preceding result;
- the listed spread/total and the season's previously observed average total.

Initial 2010-2025 pooled results at the stored nflverse number:

| Contrarian rule | Record | Win rate, pushes removed | -110 units |
|---|---:|---:|---:|
| Over: two cold offenses, ordinary total | 58-34-0 | 63.04% | +20.6 |
| Fade favorite after a 14+ point win | 401-356-20 | 52.97% | +9.4 |
| Fade fast-starting favorite vs preseason proxy | 348-327-16 | 51.56% | -11.7 |
| Under: both teams scored 30+, ordinary total | 35-40-0 | 46.67% | -9.0 |
| Under: two hot offenses, ordinary total | 49-54-1 | 47.57% | -10.4 |

These are discovery results, not promoted rules. The cold-offense result is
promising but sparse (92 games across 15 seasons); threshold sensitivity and a
held-out confirmation period are required. The original hot-offense hypothesis
does not validate in this first specification.

Run the report with:

```powershell
python -m sports_aggregator.nfl.projection_cli --db instance/nfl.sqlite3 perception-challenger --from-year 2010 --to-year 2026
```

## Grading protocol

`nfl_engine_forecasts` is the immutable issuance ledger. Each record freezes
the model version, generated time, projected scores, exact line at issue,
selected side/total, and active narrative tags. Unchanged refreshes are
deduplicated. Grades always use the issued line.

```powershell
python -m sports_aggregator.nfl.projection_cli --db instance/nfl.sqlite3 freeze-live-forecast --season 2026 --week 4
python -m sports_aggregator.nfl.projection_cli --db instance/nfl.sqlite3 forecast-grades --season 2026
```

Before any challenger is promoted, require a frozen definition, walk-forward
season results, minimum sample, threshold sensitivity, a held-out season, and
comparison with both the independent core and the raw market baseline.
