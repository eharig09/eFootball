# Secondary structured sources

CFBD owns the canonical teams, games, players and season statistics. Everything
in this document is *secondary*: it adds information CFBD does not carry, stored
in its own tables keyed to CFBD identities and never merged into them.

Three rules apply to every integration here:

1. **No entity creation.** A row is stored only when its game and team already
   resolve to canonical CFBD entities. Unresolved rows are counted and reported.
2. **Models stay separate.** FPI is not blended into CORE, SP+ or Elo. Where
   models disagree is the information; averaging destroys it.
3. **Provenance travels with the data.** Every row records its source, the exact
   asset it came from, and when it was imported.

`import_runs` records every attempt, successful or not, so freshness and
failures are visible without inspecting the data tables.

---

## SportsDataverse — ESPN FPI

**Source.** Static release assets on `sportsdataverse/sportsdataverse-data`,
tag `espn_cfb_power_index`. Static assets are preferred over live ESPN endpoints
because they are versioned, cacheable, and do not break when ESPN changes an
internal route.

**Access.** No credentials. The release listing is resolved through the GitHub
API and cached; assets are cached on disk under `instance/sportsdataverse/`.

**Licensing.** ESPN-derived data redistributed by SportsDataverse under that
project's terms. Used here for non-commercial analysis with attribution retained
on every row.

**Unique information.** A per-team, per-game FPI projection: predicted point
differential, win probability, and matchup quality. CFBD publishes none of these.
Coverage is 2015–2026, refreshed through the current season.

**Schema.** `fpi_game_projections(season, game_id, team_id, pred_point_diff,
game_projection, matchup_quality, team_adj_gamescore, source, source_asset,
imported_at)`.

**Entity resolution.** ESPN game and team identifiers were verified to match
CFBD's exactly — all 888 game ids and all 138 team ids for 2026 — so no mapping
table is needed. Rows for FCS opponents outside the store are skipped, not
inserted; roughly 120 per season.

### Two traps this integration encodes

Both were found by inspecting real assets, and both would have failed silently:

* **Asset URLs must be resolved, not constructed.** Formats are not uniform
  across seasons: 2026 publishes no `.csv.gz`, so a pattern-built URL 404s on
  exactly the season that matters most.
* **Formats within one release are not the same data.** Several seasons ship a
  `.csv.gz` containing unresolved ESPN `$ref` pointers while the `.csv` holds the
  actual values. Preferring the compressed file imported zero usable rows while
  reporting success. `rows()` now takes `required_columns` and selects the asset
  that actually carries them; a genuine mismatch is recorded as
  `schema_mismatch` rather than a zero-row success.

### Injuries are not available

`espn_cfb_injuries` exists as a release tag but **publishes zero assets**. The
upstream schema documentation notes the fixture is empty. The
`player_availability` table is created and the injury shape is defined, but no
importer is wired to a source that does not publish data. Availability reporting
on game pages currently comes from the topic-classified reporting stream instead.

---

## Open-Meteo — kickoff weather

**Source.** `api.open-meteo.com/v1/forecast`, hourly variables at venue
coordinates.

**Access.** No API key and no account for non-commercial use. The free tier is
rate-limited and asks callers to be reasonable, so one request covers a venue's
entire 16-day window rather than one request per game, and responses are cached
for an hour under `instance/weather/`.

**Licensing.** CC-BY 4.0 for non-commercial use. Attribution retained via the
`source` column.

**Unique information.** Forecast conditions at kickoff, which no other source in
the stack provides. Wind in particular changes how a game is played and is not
derivable from anything already stored.

**Schema.** `game_weather(game_id, forecast_generated_at, kickoff_time,
forecast_hour, temperature, precipitation_probability, precipitation_amount,
sustained_wind, wind_gust, humidity, visibility, weather_code, condition,
flags_json, indoor, venue, latitude, longitude, source, imported_at)`.

**Snapshots, not overwrites.** A forecast taken ten days out and one taken on
game morning are different information, and the movement between them is often
what matters. The primary key includes `forecast_generated_at`, and
`weather_for_game` reports the change since the first snapshot.

**Flags** are explainable and carry the number that produced them:
`HIGH_WIND` (≥15 mph sustained), `HEAVY_GUSTS` (≥25 mph), `RAIN_RISK` (≥40%
chance or ≥0.15 in), `EXTREME_HEAT` (≥88°F), `EXTREME_COLD` (≤28°F). Thresholds
are chosen for football, not meteorology.

**Graceful degradation.** The forecast horizon is about 16 days, so most of a
season is outside it at any moment. That is reported as `outside horizon`, not as
a failure. Indoor venues store a row so a page can say "indoor" rather than
showing nothing, and carry no flags.

---

## Betting market

CFBD's `/lines` endpoint already supplies DraftKings and Bovada quotes, stored
per provider in `game_lines` with opening and current values. That covers the
stated goal — comparing market expectations with model ratings — without a paid
plan, and nothing depends on a commercial provider.

**The Odds API (optional, free tier).** `sports_aggregator/odds_api_tracker.py` adds a line *history* from
DraftKings, FanDuel, BetMGM and BetRivers (ESPN BET is kept if the feed offers it; it was not in the feed
when this was written). Rows land in `odds_api_lines` in `cfb.sqlite3` / `nfl.sqlite3`, appended only when a
book's number moves, with the matching game id where one can be found.

Credits are the constraint, so a sweep is guarded three ways: a calendar slot must be due (college: Sunday
night, Tue/Thu/Fri noon, three Saturday waves; NFL: Wed, Thu night, Sat, two Sunday windows, Monday), the free
`/events` endpoint must show a game inside eight days, and a shared ledger plus the API's own
remaining-credit header must leave room under a 450-credit monthly cap. Spreads and totals for the US region
cost 2 credits per sweep whatever the slate size, so the schedule uses roughly 120 of the 500.

    python -m sports_aggregator.odds_api_tracker status
    python -m sports_aggregator.odds_api_tracker sweep --league cfb --force   # spends 2 credits
    python -m sports_aggregator.odds_api_tracker run-due                      # what a scheduler calls

The free plan has no historical data, so history starts the day collection starts. Set `ODDS_API_KEY` in the
environment (never in the repository).

---

## Not yet implemented

Deliberately deferred rather than built thin:

* **Official awards.** Award organizations publish announcements as prose on
  pages with no stable structure. This needs a per-award parser and a review
  step; a brittle scraper would produce a source that silently rots.
* **Draft evaluator and all-star signals.** Senior Bowl and Shrine Bowl watch
  lists are seasonal and mostly published as articles or PDFs. The consensus
  board importer already accepts ranked CSVs, which is the same shape these
  would take.
* **Wikidata.** Stadium capacity and coordinates already come from CFBD's
  `/venues` endpoint, which is authoritative for this purpose. Wikidata would
  duplicate it.
* **NCAA statistics.** Treated as a validation reference. No stable downloadable
  feed was found that would not require brittle scraping.

---

## Commands

```powershell
# What each upstream dataset currently publishes, before importing anything
python -m sports_aggregator.cfb.external_cli sources

python -m sports_aggregator.cfb.external_cli fpi --from-year 2023 --to-year 2026
python -m sports_aggregator.cfb.external_cli weather --season 2026
python -m sports_aggregator.cfb.external_cli status
```

Orchestrated through the bootstrap entry point:

```powershell
python -m sports_aggregator.bootstrap plan     --season 2026
python -m sports_aggregator.bootstrap initial  --season 2026
python -m sports_aggregator.bootstrap refresh  --season 2026
python -m sports_aggregator.bootstrap status   --season 2026
```

`initial` builds current canonical data first, adds the immediately prior-season
player baseline, then runs static sources (venues, promoted-team history, PFF and
draft board) before identity derivation and content ingestion. Full multi-season
career history remains the separate `history` phase. `refresh` updates current
player production, models, markets, roster context, weather, verified reporting
sources, entity tags, story clusters, and relevance scores. Every step runs in its
own process, so one unavailable source cannot stop the rest; the exit code still
reflects non-optional failures.

Current-season player statistics are allowed to be empty before CFBD publishes
in-season production. That step remains visible as an optional failure while the UI
uses the prior-season baseline; it becomes current automatically once rows exist.

`/api/v1/cfb/sources/status` exposes the same counts and freshness over HTTP.
