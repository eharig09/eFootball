# NFL Extension Architecture

## Implementation status

Updated 2026-09-13.

- **Phase 1 complete:** `nfl` is registered in the shared league catalog with
  ESPN's documented NFL RSS endpoint. The first public surface is available at
  `/nfl/`, with both `/api/v1/nfl/articles` and the generic
  `/api/v1/leagues/nfl/articles` JSON forms. The landing page discovers it
  automatically.
- **Phase 2 complete:**
  `sports_aggregator/nfl/nflverse.py` is now a project-native port of the
  sibling adapter. It preserves immutable completed-season caches, six-hour
  moving-data caches, stale-cache fallback, atomic downloads, and per-season
  loaders. The live CSV variants were audited for teams, schedules, 2025
  rosters, and 2025 weekly stats; required-column guards now make future schema
  drift fail the affected dataset explicitly. `sports_aggregator/nfl/naming.py` contains only the reusable NFL
  identity normalization extracted from the sibling project; no DFS parsing
  crossed the boundary.
- **Phase 3 complete:** `nfl/models.py`, `nfl/repository.py`, and `nfl/sync.py`
  normalize and persist teams, games, players, and long-form weekly metrics in
  the separate `nfl.sqlite3`. Each dataset is isolated, sync runs are audited,
  and `flask --app app sync-nfl --year YEAR` is wired to the application. Since
  roster releases contain the latest transaction row for player/team spells,
  canonical persistence keeps the newest row for each
  `(season, gsis_id, team)` identity and removes terminal cut, retired, and
  traded-away states. The wide weekly
  stat release is stored long-form so new numeric metrics do not force schema
  migrations.
- **Phase 4 complete:** the headline-only shell has been replaced by the NFL
  blueprint. `/nfl/` and `/api/v1/nfl` now expose canonical counts, the current
  schedule window, passing/rushing/receiving leaders, all teams, purpose-routed
  Bluesky sources, and national reporting. Team HTML/API pages expose schedule,
  roster, and team-relevant sources. Dedicated game pages expose matchup context
  and player box scores; player pages expose roster identity, season totals, and
  weekly game logs. Every HTML surface has a matching `/api/v1/nfl/...` packet,
  and schedule, roster, and leader tables link into the canonical pages.
- **NFL source directory complete:**
  `data/nfl/NFL_Bluesky_Directory.xlsx` is parsed with the Python standard
  library, so importing the workbook does not add an Excel runtime dependency.
  All 158 unique handles and all 32 team scopes validate. New shared-registry
  tables preserve league scope and keep coverage, specialty, and recommended
  list tags distinct. Recommended lists route accounts to NFL Wire, Film +
  Analytics, Personnel, Players + Usage, and team-beat sections. Importing NFL
  scope does not overwrite an existing handle's CFB specialties or team scope.
  `sync-nfl` imports the workbook when present; `import-nfl-sources` and
  `sports_aggregator.nfl.sources_cli validate` are available independently.
- **Phase 5 in progress — first live content graph complete:** NFL reporting
  and public Bluesky posts now persist in NFL-owned content/link tables inside
  `nfl.sqlite3`. This avoids forcing string NFL game IDs or GSIS player IDs into
  CFB's integer-keyed entity tables. `sync-nfl-content` ingests the ESPN NFL RSS
  result and public `app.bsky.feed.getAuthorFeed` responses from the curated
  directory, skips replies/reposts, and links exact team names, team-scoped
  sources, exact full player names, and schedule-proximate matchups with stored
  confidence/method values. Team, player, game, and dashboard pages consume the
  linked stream; `/api/v1/nfl/content` exposes the same graph with optional
  entity filters. The first live 2026 run stored 23 RSS articles and 490 Bluesky
  posts from 158 attempted sources (six endpoint failures). After canonical
  roster filtering, the graph retains 494 team, 381 player, and 70 game links;
  roster replacement automatically prunes links to identities no longer in
  that season's current organization set. Remaining Phase 5 work is source-resolution/
  retry auditing, richer NFL-specific topics and story clustering, and folding
  this command into a scheduled NFL refresh profile.
- **Live vertical audit complete:** the 2025 end-to-end sync was exercised on
  2026-09-13 against current nflverse parquet releases and persisted 32 teams,
  285 games, 2,662 current-organization roster identities after terminal-status
  filtering, and 2,440,807 long-form weekly metric rows.
  The same run imported all 158 NFL Bluesky directory entries. This validates
  the observed release schemas and the full download-normalize-persist path,
  not only fixture-shaped unit data.
- **2026 roster refresh and movement layer complete:** the live 2026 nflverse
  refresh on 2026-09-13 persisted 272 scheduled games, 2,524
  current-organization roster identities,
  16,866 weekly player metrics, 187 snap rows, 515,474 depth rows, 516 team
  metrics, and four team-game efficiency profiles (the two completed games
  available at refresh time). Default NFL pages therefore resolve to 2026.
  The canonical roster explicitly retains active, reserve,
  developmental/practice-squad, inactive, and exempt states while excluding
  cut, retired, and traded-away rows. `sync-nfl-rosters` refreshes this layer
  without reprocessing the larger weekly and play-by-play datasets. Team pages
  compare adjacent releases by GSIS ID and mark arrivals from another
  NFL team, rookies identified by master-player draft year, other new roster
  additions, departures with a known 2026 destination, and players absent from
  the new release. No name-only movement inference is used.
- **Free data-source intake in progress:**
  `data/nfl/NFL_Data_Sources_Directory.xlsx` is now parsed and validated without
  an Excel runtime dependency. It contains 28 candidate datasets: 25 free/open
  candidates and three paid/trial commercial entries that are explicitly
  excluded under the current policy. The first expansion adds PFR snap counts
  via nflverse and ESPN depth charts via nflverse. The 2025 live audit persisted
  26,612 game/player snap rows and 553,770 valid timestamped depth rows. Team
  pages select the newest depth snapshot while the database retains history;
  snap shares carry PFR-via-nflverse attribution and depth charts carry
  ESPN-via-nflverse attribution.

  The next free-source tranche should establish player master/ID-crosswalk
  dimensions before play-by-play, team statistics, NGS, and FTN charting are
  added. This preserves stable GSIS/PFR/ESPN joins before larger fact tables
  arrive. Sleeper remains secondary and must retain its non-commercial-use
  restriction. The dead-after-2024 nflverse injury feed is historical-only;
  current-season availability is now supplied by one cached league-wide ESPN
  injury request. Participation is delayed research data, and Big Data Bowl files must never
  become a production-page dependency. Sportradar and SportsDataIO are ignored
  until the user makes a separate licensing decision.
- **Player identity tranche complete:** the global nflverse player master is
  persisted with GSIS as its primary key, including biography, position,
  college, current status, headshot, and draft context. Provider identifiers
  live in a separate long-form table keyed by source, GSIS ID, and provider so
  additional platforms require rows rather than migrations. The live audit
  stored 24,820 master players, linked 7,997 DynastyProcess records to GSIS, and
  produced 236,465 external-ID mappings across the two sources. Roster-backed
  player pages are enriched from the master; historical master-only players also
  render without inventing season production.
- **NFL dark-mode contract complete:** all NFL templates load the dedicated
  stylesheet after the shared table stylesheet. The NFL layer explicitly sets
  dark document/body surfaces, panels, tables, alternating rows, links, empty
  states, and native form controls rather than relying only on the user's OS
  preference. A web regression test verifies that contract.
- **Canonical navigation correction:** the home-page NFL card and league
  discovery API now point directly to `/nfl/`. The earlier generic
  `/leagues/nfl/` URL redirects there, preventing users from landing on the
  headline-only generic league template while the deeper NFL vertical remains
  hidden.
- **First analytics tranche complete:** nflverse weekly team statistics are
  persisted long-form (73,608 numeric 2025 measurements), and the play-by-play
  feed is reduced during ingestion into 570 compact team-game profiles instead
  of copying the 372-column raw release into SQLite. The derived layer includes
  scrimmage EPA/play, success rate, dropback EPA, rush EPA, early-down EPA,
  explosive-play rate, defensive EPA allowed, and net EPA. Kicks, punts, and
  other non-scrimmage plays are excluded. These metrics now drive an EPA power
  table on the league dashboard, a team profile strip, and game-level efficiency
  comparisons. Team cards, team heroes, power-table identities, and game heroes
  use canonical team logos/colors while retaining dark fallback surfaces.
- **Standings and visual hierarchy complete:** regular-season standings are
  computed from canonical completed games, ranked within all eight divisions,
  and expose wins, losses, ties, win percentage, point differential, and current
  streak. Team pages receive the same canonical record. A dedicated component
  stylesheet now supplies the NFL navigation shell, responsive AFC/NFC division
  cards, team record treatment, team-branded player surfaces, table interaction
  states, and mobile/reduced-motion behavior while leaving the base dark token
  layer intact.
  The dashboard's current slate now renders as responsive matchup cards with
  team marks, records, scores, and season EPA context; the sortable schedule is
  retained as an accessible disclosure rather than removed.
- **Matchup depth tranche complete:** `nfl/matchups.py` now assembles the game
  briefing separately from route and template code. Both offensive directions
  are compared with the opposing defense across overall EPA/play, dropback EPA,
  rush EPA, success rate, and explosive-play rate, with league ranks and a
  deliberately descriptive lean. Efficiency, production, recent form, and
  player leaders are cut off before the selected game's week so an archived
  matchup cannot leak later-season knowledge into its pregame context. Matchup
  pages also expose per-game scoring and
  yardage production, sacks and turnovers, the five completed games entering the
  matchup, team leaders, market context, and a compact postgame efficiency
  review. Defensive pass/rush/success/explosive allowance is derived from the
  same stored team-game play-by-play summaries, requiring no new raw fact table.
  The game API carries these same packets. The former 13-column player box score
  remains available in the compatibility `stats` JSON field, while HTML and the
  new `stat_groups` field use separate passing, rushing, receiving, and defense
  tables. This is the first NFL-native step toward the CFB matchup page's depth;
  weather history and player-vs-unit grading remain future layers because
  their canonical sources are not yet synchronized.
- **Early-season comparison tranche complete:** a 2026 team or matchup page no
  longer collapses into empty efficiency cards before both teams have played.
  Current rosters and personnel changes remain 2026-native, while efficiency,
  production, returning leaders, and closing form fall back to a visibly
  labeled 2025 baseline. Prior-year leaders are restricted to GSIS IDs on the
  current roster. Matchup pages also expose a transparent scoring midpoint
  (each offense averaged with the opposing points allowed) and the largest
  offense/defense rank separations as descriptive watches. The HTML and game
  JSON packet both expose the baseline season, scoring shape, and watch list;
  none of these values are labeled or treated as a betting projection.
  The league dashboard follows the same contract: current schedules,
  standings, roster counts, and linked coverage remain in place while the EPA
  power table and leader boards use a labeled prior-season baseline until at
  least 24 clubs have a current play-by-play profile. Dashboard JSON exposes
  that choice as `analysis_season`, and baseline player/team links retain the
  correct season query rather than silently opening the current-year page.
- **Usage and unit-continuity tranche complete:** `nfl/usage.py` converts the
  existing weekly target, carry, reception, air-yard, first-down, explosive-
  gain, yardage, and EPA fields into team-relative workload profiles. Team
  pages expose target concentration, rushing concentration, a compact sortable
  opportunity table, and returning target/carry/combined-opportunity shares.
  When the current season is empty, prior-year rows are limited to exact GSIS
  IDs on the current organization roster; the historical team denominator is
  intentionally retained, so the shares quantify how much workload actually
  returns rather than renormalizing the remaining players to 100 percent.
  Matchup pages show the six largest returning workloads on each side, and
  current player pages add a labeled prior-season role profile. Exact-ID roster
  comparison also now reports retention, additions, and departures for
  quarterbacks, backfield, receivers, offensive line, defensive front,
  linebackers, secondary, and specialists on both team and matchup pages. The
  HTML and JSON packets share the same workload and continuity structures.
- **Situation and player-table tranche complete:** matchup packets now derive
  rest days from the last completed game, surface division status, venue/roof/
  surface context, recorded schedule weather, and summarize all prior meetings
  available in the isolated NFL database. The history query spans synchronized
  seasons and is bounded before the selected game. Player pages no longer force
  every position through the same 14-column weekly table: headline production
  is position-aware and game logs are split into only the passing, rushing,
  receiving, defense, and kicking families the player actually recorded. The
  original wide `game_log` remains in JSON for compatibility and the structured
  `game_log_groups` packet drives HTML.

The initial plan called for an "official NFL.com feed." Verification on
2026-09-13 found that `https://www.nfl.com/news?service=rss` serves the ordinary
HTML news page, not RSS/Atom. It has not been placed behind `RSSNewsProvider`.
An official-site HTML/API provider can be added separately after its access and
normalization contract is tested; Phase 1 therefore ships with the documented
ESPN NFL feed rather than a knowingly invalid second source.

## Organizing principle

The repository currently contains two different things stacked together, and the
NFL extension has to respect that split rather than paper over it:

1. A genuinely generic league platform (`sports_aggregator/models.py`,
   `catalog.py`, `service.py`, `providers/`, `web.py`, `tables.py`) that already
   supports adding a league via a `LeagueConfig` and RSS `FeedConfig` list, per
   the README's "Add another league" section. This layer is reusable today,
   unmodified.
2. A deep, single-league vertical (`sports_aggregator/cfb/`, ~90 modules,
   ~32,000 lines, one SQLite database `cfb.sqlite3`) that is not a "CFB
   implementation of a generic sports framework" -- there is no generic sports
   framework underneath it. Every table is unprefixed and CFBD-shaped
   (`teams`, `games`, `players`, `player_season_stats`, `rankings`,
   `draft_picks`, ...) and every model (win probability, EPA, garbage-time
   detection, matchup grading, draft board, transfer portal) is calibrated on
   college rules and college data.

"Extend into the NFL" means building a second deep vertical, `sports_aggregator/nfl/`,
that mirrors `cfb/`'s module boundaries and quality bar, backed by its own
database (`nfl.sqlite3`). It does not mean generalizing `cfb/` in place -- that
would be a much larger, riskier rewrite for no benefit, since almost nothing
below the RSS-aggregation layer is shaped in a sport-neutral way.

The one exception is the social/reporting pipeline (`sports_aggregator/social/`),
which today writes into the same SQLite file as the CFB repository and, in
`social/sport.py`, explicitly rejects anything classified as NFL content with
0.95 confidence. That rejection has to become an acceptance-and-routing path
before any NFL reporting can enter the system. See "Social pipeline" below.

## The blocking gap: there is no NFL equivalent of CFBD -- except there mostly already is

Everything downstream of ingestion in `cfb/` assumes CollegeFootballData.com:
authenticated REST, endpoint-shaped JSON, retry/cache semantics in `cfbd.py`,
normalization in `cfb/models.py`. CFBD does not cover the NFL, so choosing a
structured data source looked like the biggest open risk in this plan --
except a sibling project on this machine, `C:\Users\ehari\Desktop\scouting_report`,
already built and hardened exactly this layer for its own NFL DFS tooling. That
project is not a news aggregator (it generates DraftKings projections, boards,
and lineup optimization), so its `optimize.py`/`salaries.py`/`ownership.py`/
`upload.py` layer is out of scope here -- but its **data-access layer is
directly relevant** and should be ported rather than rebuilt:

- **`nfl/data.py`** -- reads nflverse release assets
  (`nflverse/nflverse-data` on GitHub: rosters, schedules, weekly stats, snap
  counts, injuries, depth charts) as parquet directly via `requests` +
  `pd.read_parquet`, deliberately avoiding the `nfl_data_py` package to dodge
  its pandas pin. It gets the cache invalidation rule right in a way worth
  preserving exactly: a **completed season is immutable and cached forever**;
  the **current season carries a 6-hour TTL** because nflverse republishes
  within hours of a game finishing. This is the same "raw response cache,
  season/week-aware TTL" discipline `cfbd.py`'s `FINISHED_WEEK_TTL` /
  `LIVE_WEEK_TTL` split already uses on the CFB side -- same principle,
  independently arrived at, already proven across multiple seasons of use
  (`.cache/nflverse/` on that machine holds rosters back to 2014, weekly stats
  back to 2018).
- **`nfl/pffdata.py`** -- solves two silent-failure problems that a naive PFF
  importer would hit on the NFL side: PFF's exported filenames carry no season
  (`receiving_summary (4).csv`, numbered by download order, not chronology) --
  solved by fingerprinting file contents against known rosters rather than
  trusting the filename; and PFF's `player_id` is the **same id space** as
  nflverse's `pff_id` roster column, so player identity joins exactly on
  skill positions (97-99% coverage) with name+team matching kept as an
  explicit fallback for the offensive line (~66% coverage via id, where
  nflverse's column is often unfilled). This directly answers the identity
  problem `cfb/pff.py` and `cfb/repository.py`'s transfer-identity work solved
  for CFB, already solved for NFL.
- **NFL PFF data already exists locally**: `scouting_report/nfl/pff/` and
  `pff_coverage_data/` hold real PFF exports (fantasy-stats passing/receiving,
  receiving/rushing summaries, coverage scheme, slot coverage), and
  `.cache/pff/crosswalk.parquet` is the resolved id crosswalk. This corrects
  what would otherwise be a real gap -- CFB's `PFF/` folder is team-grades-only
  with no NFL data, but NFL PFF data is sitting in the sibling project.
- **`nfl/naming.py`** -- canonical team/position code normalization
  (`canon_team`, `canon_position`, `normalize_name`, imported by `pffdata.py`)
  is the NFL-side equivalent of `cfb/models.py`'s `normalize_alias` /
  `normalize_person_name`, and should be ported for the same reason: name
  matching across sources is where CFB's hardest bugs were.
- Derived signal work that overlaps with what `cfb/matchups.py`,
  `cfb/unit_continuity.py`, and `cfb/situations.py` do for CFB already exists
  in some form for NFL: `nfl/usage.py` and `nfl/redzone.py` (target share,
  red-zone/inside-5 workload), `nfl/oline.py` (offensive-line unit analysis),
  `nfl/sos.py` (strength of schedule), and `nfl/coldstart.py` (rookie/role
  priors when there's no prior-season sample -- the NFL analog of CFB's
  "missing evidence, not low impact" transfer framing). These are shaped for
  DFS projection output today, not for a team/player page, so porting them
  means extracting the underlying signal computation and re-presenting it
  through `sports_aggregator/tables.py`, not copying the modules wholesale.

That sibling project's own documentation (`CHEATSHEET_NFL.md`) explicitly
frames its `nfl/` package as "a deliberate port rather than a shared module"
from its MLB twin, for the same reason this plan forks `cfb/` instead of
generalizing it in place: the live system stays untouched while the new sport
gets code shaped for its own domain. That's independent confirmation, in this
codebase owner's own practice, that "separate but similar" is the right call
here too.

**Revised recommendation**: treat `scouting_report/nfl/data.py` and
`nfl/pffdata.py` as the starting point for `sports_aggregator/nfl/nflverse.py`
and its PFF importer, adapted into this project's raw-cache/normalize/persist
pipeline shape (`cfbd.py` -> `nfl/nflverse.py`, `cfb/pff.py` -> `nfl/pff.py`),
rather than building an nflverse client from nothing or reaching for
`sportsdataverse.py`'s ESPN-release pattern. The `sportsdataverse-data` ESPN-NFL
assets remain a reasonable secondary/cross-check source since the existing
provider already knows how to talk to that repository, but they're no longer
the primary path.

## Domain mapping: where CFB concepts don't transfer literally

| CFB concept | NFL equivalent | Notes |
|---|---|---|
| Conference / division / classification (FBS) | Division (AFC/NFC x 4), no classification tier | 32 teams, 2 conferences, 8 divisions vs. 134+ schools -- `identity.py`'s conference-palette logic simplifies |
| Recruiting + transfer portal | Free agency, trades, waivers | No direct analog to `cfb/recruiting.py` / `cfb/transfers.py`; new modules keyed on cap space and contract year, not stars/rating |
| Bowl season / CFP | Playoffs (wild card through Super Bowl) | `game_phases.py`'s postseason handling needs real rework, not renaming |
| Class year / eligibility | Experience (years in league), contract year | No eligibility clock; `roster_production.py`'s "returning production" reasoning doesn't carry over as-is |
| Draft board (**produces** prospects, CFB side) | Draft **results** (consumes picks) | `cfb.sqlite3`'s `draft_picks` table already records NFL destination team per CFBD athlete. The NFL vertical should consume that table as a join point, not duplicate `cfb/draft.py`. |
| Win probability / EPA / turning points | Same concepts, different calibration | Easier on the NFL side -- nflverse ships EPA/WP pre-computed |
| PFF snapshots (`PFF/*.csv`) | `scouting_report/nfl/pff/`, `pff_coverage_data/` | CFB's own `PFF/` folder is team-grades-only, but real NFL PFF exports exist in the sibling project -- see "PFF ingestion" below for how they become canonical without copying files between repos |

## Proposed structure

Mirror `cfb/` module-for-module under `sports_aggregator/nfl/`, its own
database, its own blueprint, registered the same way `cfb_pages` is registered
in `app.py` today:

```text
sports_aggregator/nfl/
    models.py        # Team, Game, Player -- nflverse-shaped, not CFBD-shaped
    nflverse.py       # adapter: retry/cache/provenance, mirrors cfbd.py's contract
    repository.py     # new SQLite file: nfl.sqlite3, own unprefixed schema
    sync.py           # per-dataset isolation, mirrors CFBDataSync
    views.py          # repository packets -> Table objects (sports_aggregator/tables.py)
    statlines.py       # box-score pivots (fewer stat categories than CFB)
    web.py             # blueprint "nfl", routes hardcoded "/nfl/...", "/api/v1/nfl/..."
    situations.py, matchups.py, ...  # ported once nflverse field coverage is confirmed
```

### Why a separate SQLite file

The CFB schema is unprefixed (`teams`, `games`, `players`, no `cfb_` prefix).
Sharing `cfb.sqlite3` would require either renaming every existing CFB table
(invasive, risks the live production database) or awkwardly namespacing new
NFL tables inside a schema that wasn't designed for it. A second file costs
nothing, mirrors the isolation the legacy `reds`/`bengals` dashboards already
get, and matches "separate but similar" literally. `app.py` already shows the
pattern for wiring a second repository as a Flask extension.

## Social pipeline

`sports_aggregator/social/` (`registry.py`, `unified.py`, `content.py`,
`stories.py`, `relevance.py`) currently persists into the same file as
`cfb.sqlite3`. Some sources genuinely cover both leagues -- `social/seeds.py`
already tags entries like `NFL/CFB` analysts -- so a fully duplicated registry
per league would force registering the same person twice and would split
their expertise/reliability scoring in two.

**Decision: shared registry, scoped links.** The source graph (people,
publications, shows, platform endpoints, expertise scores) stays exactly as it
is today -- one copy, shared across leagues. Content-to-entity links become
league-scoped:

- `social/sport.py`'s NFL branch changes from `SportDecision("NFL", "REJECT", ...)`
  to an `ACCEPT` path that tags the decision as NFL-scoped instead of
  discarding it.
- `nfl.sqlite3` gets its own content-link tables (team/player/game), or the
  existing link tables gain a `league` column if physically keeping them in
  one file proves preferable once the DB-split decision is exercised in
  practice.
- `social/stories.py` clustering and `social/relevance.py` scoring operate on
  content and role/topic evidence, not CFB-specific fields, so they should
  work against NFL-scoped content largely unchanged once the sport gate is
  fixed.
- Team-scoped registries (`social/team_reddit.py`) need NFL entries, but the
  problem is smaller than CFB's: 32 team subreddits instead of 138 school
  ones.

## Reuse inventory

**Reusable untouched:**

- `sports_aggregator/tables.py` (`Column`/`Table` presentation contract)
- `sports_aggregator/providers/{rss,reddit,youtube,podcast,base}.py`
- `sports_aggregator/page_cache.py`, `client_cache.py`, `compression.py`
- `templates/_layout.html`, `templates/_tables.html`, and the `static/cfb.css`
  token system (share the tokens; a thin `nfl.css` can import them rather than
  duplicating the theme)
- The `catalog.py` + `AggregationService` layer for a basic NFL headline feed --
  this works today, unmodified, the moment an `nfl` `LeagueConfig` is added.
  This should ship first: it's a low-risk win that validates the generic layer
  still holds while the deep vertical is built.

**Port from `scouting_report/nfl/`, file by file:**

| Source | Destination | Port whole or extract? |
|---|---|---|
| `nfl/data.py` | `sports_aggregator/nfl/nflverse.py` | Whole file. Zero DFS coupling -- it is already exactly the adapter this project needs (parquet-over-`requests`, completed/current-season TTL split). |
| `nfl/pffdata.py` | `sports_aggregator/nfl/pff_catalog.py` | Whole file (`catalog`, `resolve`, `latest`, `crosswalk`, `identify_season`, `attach_keys`). Zero DFS coupling -- this is a read-only file catalog and identity resolver, not a projection input. See "PFF ingestion" below for the one adaptation it needs: pointing at the sibling repo's folders instead of its own. |
| `nfl/salaries.py` | `sports_aggregator/nfl/naming.py` | Extract only `canon_team`, `canon_position`, `normalize_name` and their `TEAM_ALIASES` / `POSITION_ALIASES` / `SUFFIXES` tables. Leave the rest of the file (DraftKings salary-export parsing, `Status`/`OUT`/`IR` availability columns) behind -- nflverse's own `load_injuries()` is the real injury-status source; DK's derived column is redundant here and DFS-specific besides. |
| `nfl/usage.py` | `sports_aggregator/nfl/usage.py` | Extract `receiver_profile`, `rusher_profile`, `high_value_touches`, `team_target_distribution`, and the recency-weighted `_blend` helper. This is descriptive deployment/usage data (target share, high-value touches) -- exactly what a player or matchup page wants -- with no DFS-scoring step mixed in. |
| `nfl/redzone.py` | `sports_aggregator/nfl/redzone.py` | Extract `red_zone_profile`, `expected_receiving_tds`, `expected_rushing_tds`, `team_red_zone_share`. Drop `pff_path`/`load_fantasy_receiving` -- superseded by the shared `pff_catalog.py` above. |
| `nfl/oline.py` | `sports_aggregator/nfl/oline.py` | Extract `lineman_season`, `lineman_grades`, `depth_chart_line`, `projected_line`, `team_line_strength` -- the NFL equivalent of CFB's o-line/unit-continuity work. |
| `nfl/sos.py` | `sports_aggregator/nfl/situations.py` (merged in) | Extract `load_sos`/`season_sos` only. `sos_multiplier`/`attach_sos` are DK-board-shaped (write onto a `Position`/`TeamAbbrev` frame) and don't carry over; confirm what `SOS/` (repo root) actually computes before trusting it as an input, since that's precomputed data this plan hasn't inspected yet. |
| `nfl/coldstart.py` | `sports_aggregator/nfl/coldstart.py` | Port the *mechanism* (`fit_draft_curves`, `rookie_prior`, `veteran_prior`, `depth_multiplier`) but re-target the output. Today it fits toward DK fantasy points; a content page wants "how a Day 2 pick at this position typically progresses," not a point projection. This is the NFL analog of CFB's "missing evidence, not low impact" framing for rookies/transfers with no NFL sample yet, and is also the natural place to consume `cfb.sqlite3`'s `draft_picks` table. |

**Leave behind entirely** (DFS-specific, no content-site use regardless of code quality): `optimize.py`, `optimizer.py`, `ownership.py`, `upload.py`, `pool.py`, `slate.py`, `backtest.py`, `scoring.py`, `evaluate.py`, `profiling.py`, `report.py`, `report_visuals.py`, `studies.py`, `snapshot.py`, `cli.py`, and the DK-export half of `salaries.py`.

## PFF ingestion: canonical source, scanned for updates

The user-facing requirement: PFF CSVs should live in exactly one place --
`scouting_report`'s own drop folders, which already receive fresh exports as
part of that project's normal workflow -- and `sports_aggregator` should treat
that location as the source of truth, re-scanning it rather than requiring a
manual copy-and-import step into its own `PFF/`-style directory.

**Design**, adapting `nfl/pffdata.py`'s existing catalog mechanism rather than
inventing a new one:

1. **Location, not copy.** `sports_aggregator/nfl/pff_catalog.py` points at
   absolute paths via an env var, e.g. `NFL_PFF_SOURCE_ROOTS`, defaulting to
   this machine's actual paths (`C:\Users\ehari\Desktop\scouting_report\nfl\pff`,
   `...\nfl\Projections`, `...\pff_coverage_data`). No file is ever copied
   between the two repos; `sports_aggregator` reads them in place.
2. **Scanning is the existing `catalog()` call, run on every refresh, not a
   new file-watcher.** `catalog()` already keys its cache by each file's
   `(size, mtime)` stamp, so a full re-scan only re-fingerprints files that
   actually changed -- dropping in one new export re-fingerprints that file
   alone, not the other ~90. Folding a `sync-nfl-pff` step into the normal
   `bootstrap refresh` cadence (the same scheduled-task/cron mechanism CFB
   already uses) satisfies "scan for updates" without adding a live
   filesystem watcher, which would be new infrastructure this project doesn't
   otherwise have. Flag if continuous/real-time watching is actually wanted
   instead -- nothing here needs it given exports arrive in human-driven
   batches, not a stream.
3. **Fingerprint against this project's own synced rosters, not a fresh
   nflverse pull.** The original `_roster_fingerprints()` calls
   `nfl_data.load_rosters()` directly; once `sports_aggregator/nfl/nflverse.py`
   is syncing rosters into `nfl.sqlite3` (step 2 of Sequencing), the ported
   catalog should fingerprint against that stored roster table instead of
   re-fetching from nflverse. Same fingerprinting logic, one fewer redundant
   network call, and it stays consistent with whatever roster season this
   project has actually ingested.
4. **Own catalog location.** The implemented file catalog and import audit live
   in `nfl.sqlite3` (`nfl_pff_catalog` and `nfl_pff_imports`) -- never in
   `scouting_report/.cache/pff/`, which is that project's internal cache format
   and free to change independently.
5. **Join on `gsis_id`, not name+team.** CFB's `pff.py` links PFF rows to
   roster players by exact-name-same-team matching with a `possible_transfer`
   fallback, because CFBD carries no PFF-native id. NFL doesn't have that
   problem: `attach_keys()`'s `pff_id` -> `gsis_id` crosswalk is a real,
   pre-existing 97-99%-coverage join key on skill positions. The new
   `nfl_pff_players` table should key on `gsis_id` as the primary path and
   keep name+team matching only as the offensive-line fallback the crosswalk
   itself already documents (~66% id coverage there).
6. **Schema**, mirroring CFB's PFF tables but simplified for the better join
   key: `nfl_pff_players` (season, pff_player_id, team, gsis_id, key source,
   match confidence, source path, updated time), `nfl_pff_player_metrics`
   (season/week/family/player/team/metric/value/source path; week zero is the
   season aggregate), and
   `nfl_pff_imports` (audit row per catalog run: files scanned, files changed,
   rows written, unresolved count) so a scheduled scan is diagnosable the same
   way `sync_runs` already makes CFBD syncs diagnosable.

**Operational constraint worth stating plainly**: this only works on a machine
that can see the `scouting_report` folder -- i.e., local dev / wherever the
scheduled refresh actually runs, not a Render web dyno with no access to this
machine's filesystem. That's consistent with how CFB's own historical
PFF/backfill work already runs locally and ships its *result* (the populated
SQLite tables), not the live import step, to production. The NFL PFF scan
should run the same way: locally or on whatever machine holds the refresh
cron, writing into `nfl.sqlite3`, which is what production actually serves
from.

**Fork the pattern, write new code:**

- `repository.py`, `sync.py`, `models.py`, `views.py`, `statlines.py`, `web.py`
  -- same responsibilities as their CFB counterparts, NFL schema and rules,
  consuming the ported data layer above
- `identity.py` -- team color/logo handling reusable in shape; division
  palette instead of conference palette
- `bootstrap.py` -- needs an NFL-aware `initial`/`refresh`/`status`/`history`
  orchestrator (or a shared orchestrator parameterized by league)

**Genuinely new, no CFB precedent:**

- Free agency / trade / cap tracking
- Playoff seeding and bracket logic
- `social/sport.py` NFL routing (rejection path becomes an acceptance and
  link-scoping path)

**Skip / defer:**

- Recruiting, transfer portal, eligibility/class-year inference, bowl-season
  handling -- no NFL analog
- Everything DFS-specific in `scouting_report/nfl/` (`optimize.py`, `salaries.py`,
  `ownership.py`, `upload.py`, slate/lineup handling) -- out of scope for a
  news/content site regardless of code quality; do not port these

## Sequencing

1. **Cheap win first — complete.** Add `nfl` to `catalog.py` using ESPN's
   documented NFL RSS feed. This ships a `/nfl/` headline page and JSON API
   with no new database and confirms the generic layer still holds. NFL.com's
   current news URL returns HTML rather than a syndication feed, so it requires
   a future dedicated provider instead of being misconfigured as RSS.
2. **Port, don't spike, the data layer — complete.** Adapt `scouting_report/nfl/data.py`
   into `sports_aggregator/nfl/nflverse.py`, preserving its cache TTL rule, and
   pull one season of teams/schedules/rosters into a throwaway database to
   confirm field coverage against this project's schema needs. This is now a
   port-and-verify step, not a from-scratch spike -- the harder unknown (does
   nflverse actually publish clean, joinable data) was already answered by the
   sibling project's 315 passing tests and multi-season cache.
3. **Canonical core — complete.** `nfl/repository.py` + `nfl/sync.py` for teams, games,
   players, and season stats -- the NFL equivalent of the CFB architecture
   doc's "Phase 1."
4. **Web surface — complete.** `/nfl/` team/game/player pages and
   `/api/v1/nfl/...` packets reuse the `tables.py`/`views.py` patterns.
5. **Social routing fix — complete.** NFL reporting and Bluesky items are stored
   in NFL-owned content/link tables and connected to teams, players, and games.
6. **PFF and signal parity — in progress.** Port `pff_catalog.py` per the design above
   (pointed at `scouting_report`'s folders, fingerprinting against this
   project's own synced rosters, `gsis_id`-keyed), then extract the signal
   computation from `nfl/usage.py`, `nfl/redzone.py`, `nfl/oline.py`,
   `nfl/sos.py`, `nfl/coldstart.py` into this project's matchup/situation
   modules.
7. **Analytics parity — in progress.** EPA, weekly form chart packets, QB pass
   zones, opposing-defense zones, and the first alignment/usage matchup cards
   are live. Win probability and richer matchup grading remain.
8. **NFL-specific domain.** Free agency/trades, playoff bracket, and draft-pick
   linkage back to `cfb.sqlite3`'s `draft_picks` table.

## 2026-09-13 implementation checkpoint

- `NFL_PFF_SOURCE_ROOT` points to the sibling `scouting_report` repository.
  `NFLPFFService` scans `nfl/pff` and `pff_coverage_data` read-only, fingerprints
  seasons against the NFL roster already stored here, and persists its own
  catalog/import audit. The first 2025 run scanned 87 files and imported nine
  selected families: 4,749 family/player rows and 430,317 long-form metrics,
  with 42 unresolved family-level identities. `week=0` denotes a season
  aggregate and leaves the schema ready for weekly PFF exports.
- `/nfl/pff/` and `/api/v1/nfl/pff` expose those metrics. Dashboard and team
  leader cards apply minimum route/attempt/snap samples so a tiny workload
  cannot top the featured grading lists; the explorer retains the complete
  population for research.
- `qb_pass_profiles` stores nflverse targeted throws by season, week, game,
  offense, defense, passer, depth bucket, and horizontal location. It includes
  attempts, completions, yards, air yards, EPA, touchdowns, interceptions, and
  CPOE. Sacks and plays without chartable air yards are excluded. The 2025
  cached backfill produced 5,989 passer/game/zone rows.
- Team and player charts use dependency-free accessible inline SVG packets.
  Player charts adapt to position; every weekly point links to its game.
- Matchup interaction cards combine current-roster PFF receiver alignment and
  route quality, nflverse target share/box-score workload, PFF slot coverage,
  and defensive PBP zones. They intentionally describe likely high-volume
  interactions and never claim exact defender assignments.
- Production serves the populated SQLite tables. A Render process cannot read
  this developer machine's sibling repository, so PFF scanning remains a local
  refresh responsibility; the web request path never accesses source CSVs.
- NFL reporting now follows the CFB reader model while retaining NFL-owned
  storage. Directory tags route items into Articles, News Wire, Analysis,
  Personnel, Players + Usage, and Team Beats on league/team/player/game pages.
  Items expose their publisher/account, timestamp, external destination, and
  stored team/player/game links. `nfl_content_source_checks` records the latest
  outcome for every attempted account and `nfl_content_ingestion_runs` records
  platform-level attempted/succeeded/seen/stored/error totals. `/nfl/sources/`
  and `/api/v1/nfl/sources` reconcile all 158 configured accounts with those
  checks and historical output, including a 32-team coverage table.
  The first instrumented full-directory run attempted all 158 accounts: 146
  have stored output, six failed with unresolved/invalid-source responses,
  three feeds were empty, and three returned no eligible original item. The
  resulting graph contains 691 unique items, 673 team links, 510 player links,
  and 107 game links.
- The NFL presentation now follows a wide, conventional statistics-desk layout:
  a 1,480 px desktop shell, flat section hierarchy, compact rectangular modules,
  four-column division standings, two-column PFF groups, and full-width player
  and box-score logs. The shared table renderer remains unchanged, while the NFL
  component layer supplies sticky headers, aligned numeric columns, quiet row
  striping, hover focus, restrained borders, and responsive horizontal overflow.

## 2026-09-14 intelligence phase

- Personnel headlines are no longer raw roster churn. The personnel service
  scores every exact-ID arrival and departure from prior snap participation
  (38 points), best relevant PFF grade (37), and draft capital (25). Only
  scores of 30 or higher appear as key movement; complete tables remain
  available for audit.
- Team pages expose every current player in a position room ordered by latest
  depth rank and then snap participation, with status, draft context, prior
  participation, and linked PFF grade where available.
- SVG charts now use a 720x260 coordinate space with undistorted aspect ratio,
  axes, labels, tooltips, and a selectable normalized overlay. Team pages
  support scoring, EPA, success, dropback, rush, and explosive options; player
  options adapt by position.
- Receiver pass profiles persist targets, receptions, yards, air yards, YAC,
  EPA, and touchdowns for each depth/location cell. Matchup cards compare the
  featured receiver's highest-volume exact field section with the opposing
  defense's result allowed in that same section.
- Matchup pages add snap-weighted PFF pass/run blocking, pressures allowed,
  returning box-score pressure production, and opponent rush EPA. No PFF
  pass-rush grade is implied where a selected export is unavailable.
- Projected game shape is a disclosed blend: 70% season
  offense/opponent-defense midpoint, 20% last-three scoring when available,
  and 10% listed market midpoint when available. It presents a context range,
  not betting-model precision.
- NFL content stores a transparent editorial score and reasons. Article depth,
  entity specificity, matchup linkage, reporting signals, and supporting links
  establish the base; freshness contributes at most 12 points when ordering.
  The existing 691 items were rescored in place.
- NFL jobs now use the same bootstrap plan and process lock as CFB. Light
  refreshes append isolated roster and content subprocesses. Heavy refreshes
  additionally run core nflverse synchronization and the read-only PFF import.
  One child runs at a time and exits before the next begins.

## 2026-09-14 landing-page and navigation pass

- Recent-form pages now render one SVG workbench rather than repeating every
  metric below it. The first metric is visible on load; each additional
  checkbox intentionally adds or removes a normalized overlay using explicit
  SVG display state.
- The ESPN/nflverse depth feed labels formations as position groups. Position
  rooms now use canonical roster positions, retain the best depth rank when a
  player appears in multiple formations, and display the provider's actual
  alignment slot separately.
- A continuous NFL Elo ledger replays 4,365 completed regular-season and
  playoff games from 2010 forward. Every franchise begins at 1500 on its first
  appearance. The transparent model uses a 55-point home advantage, K=20, and
  a margin-of-victory multiplier; historical aliases normalize to the current
  franchise code.
- Landing-page games-to-watch scores combine average Elo quality, Elo
  closeness, division leverage, current standings, and the strongest available
  PFF player interaction. The score ranks attention; it is not a point-spread
  projection.
- The NFL landing page reads the existing 2027 CFB consensus board directly
  from the CFB repository and pairs it with current NFL draft order and roster
  need. Order uses win percentage, point differential, then Elo. Prospect
  selection considers the next twelve board names; an established returning
  quarterback (350 attempts plus 3,200 yards or first-round capital) suppresses
  quarterback need.
- PFF leaders expanded from four six-player blocks to ten filterable
  ten-player views: route grade, yards per route, rushing grade, elusive
  rating, pass/run blocking, man/zone/slot coverage, and deep passing.
- The landing page now follows the CFB briefing hierarchy: games to watch and
  draft projection lead, while standings, ratings, leaders, reporting, and
  reference sections follow on flatter continuous surfaces.

## 2026-09-14 matchup history, depth, and tabbed-workspace pass

- Games to Watch is restricted to the same current-week slate rendered on the
  landing page. It no longer reaches into later weeks to fill six positions.
- Canonical schedules, closing lines, rest, moneylines, spread/total prices,
  and head-coach attribution are stored from 2010 forward. The one-time
  `sync-nfl-history` backfill processes weekly and play-by-play releases one
  season at a time; routine scheduled refreshes still update only the current
  season and therefore do not add a second large concurrent workload.
  Play-by-play parquet reads are projected to the 32 columns consumed by the
  site before pandas materializes them (about 27 MB for 2021, versus roughly
  1.8 GB for the unbounded wide frame). SQLite uses WAL mode so readers can
  continue while a seasonal transaction commits.
- Matchup history now reports stored coverage, series record, scoring, streak,
  and up to 25 prior meetings. Current-roster player production against the
  opponent is aggregated across every stored season.
- The game-shape packet now includes expected plays and possessions, seconds
  per play, neutral-script pass rate, third-down conversion, and red-zone play
  success. Market context preserves nflverse's away-team spread convention and
  shows career head-coach records against the closing number and total.
- Key movement PFF evidence is selected by football role: passing for QBs,
  rushing/receiving/protection for backs, route grade for receivers, both
  blocking grades for linemen, and man/zone grades for coverage players. A
  player's unrelated highest grade is no longer allowed to drive relevance.
- Unit continuity is participation-weighted. Prior snap share multiplied by
  games produces equivalent starts, and retention is returning equivalent
  starts divided by the prior unit's equivalent starts.
- Depth charts are authoritative for precise slots. LT/LG/C/RG/RT and every
  defensive alignment slot are normalized into readable rooms; roster-only
  players remain appended rather than disappearing.
- Team and matchup pages use persistent, keyboard-accessible desktop/mobile
  tabs patterned after the CFB workspace. Charted pass grids run vertically
  from deep at the top to Behind LOS at the bottom.

## 2026-09-14 matchup interaction and live-results pass

- Coach market history now exposes actual ATS records as favorite, underdog,
  home, and away. A separate `versus` lens applies the current team's exact
  spread to every earlier coaching margin; it is explicitly labeled as a
  replay against this week's number rather than an historical closing-line
  record.
- `pass_zone_receivers` stores the exact passer-to-receiver contribution in
  each depth/location cell. Quarterback and defense pass charts expose a
  keyboard-focusable hover table with receptions, targets, yards, touchdowns,
  and aDOT. This is derived in the same bounded PBP pass as the existing zone
  aggregates and does not require another parquet load.
- Receiver interaction cards compare the player's three highest-volume pass
  zones with the opponent's result allowed in each exact zone. Run interaction
  rows connect returning carry share and role-appropriate PFF rushing measures
  to the opposing defense's rushing EPA allowed.
- Matchup production leaders now retain the top two players across passing
  yards/TDs, rushing yards/carries, receiving yards/targets/receptions,
  sacks/QB hits, and interceptions. Opponent-history tables can switch between
  career totals and per-game rates without changing the underlying historical
  sample.
- The 2026 live refresh completed on 2026-09-14. It stored all 15 completed
  Week 1 games available at refresh time, 16,866 current-season player-weekly
  metric rows, 516 team-weekly metric rows, and 142 team-game PBP efficiency
  rows. The remaining Week 1 game stays scheduled until the next routine sync.

## 2026-09-14 matchup workspace UX pass

- The matchup tab bar now sits below the global NFL header and retains a
  compact, team-colored score/record context while the user moves through the
  page. Tab subtitles describe the information behind each choice and collapse
  cleanly on smaller screens.
- Tabs are addressable as `#tab-<name>`. A hash that points to an actual section
  (for example `#players`) takes priority over session storage, which keeps
  totals/per-game and other deep links from opening inside a hidden panel.
  Arrow-key, Home, and End navigation remain supported.
- Production comparisons mark the stronger value with the applicable team's
  color and explicitly account for lower-is-better measures. Expanded player
  leaders use a compact two-deep ranking matrix instead of another grid of
  cards.
- Matchup leader categories are aggregated in one query per team, and movement
  snap context is loaded for all relevant teams in one batch. On the current
  local dataset, the sampled completed-game render improved from about 11.9 to
  3.8 seconds cold and from 6.5 to 3.3 seconds warm during this pass.

## 2026-09-14 live-slate, search, and postgame pass

- The landing page now carries a current-week game rail containing both final
  and upcoming games. Finals link directly to `#tab-review`; scheduled games
  link to `#tab-overview`. The full current-slate grid preserves the same
  behavior, so a completed game is never presented as though it were still a
  preview.
- Completed game reviews follow the CFB report hierarchy while remaining
  honest about the NFL data currently stored: final/market outcome, decisive
  efficiency edges, game production leaders, team box-score comparison,
  game-only quarterback pass maps, and the categorized player box score. No
  drive-leverage or quarter model is implied until those PBP aggregates are
  persisted explicitly.
- Pregame charted passing is now offense versus defense. Each direction pairs
  the current or returning likely quarterback's depth/location chart with the
  opponent defense's results allowed in those same cells. Receiver hover
  detail remains available on both sides of the comparison.
- Coach market panels show only the role and venue that apply to the selected
  matchup, plus replay against this week's exact spread. The current-season
  ATS and over/under records are separate from the relevant career role/venue
  samples; zero-game rows are suppressed.
- `/nfl/search/` and `/api/v1/nfl/search` search current rosters, team identity,
  games, and stored reporting. Completed game results route to their review;
  upcoming results route to their preview. A compact search box is present in
  the shared NFL header.
- The platform root is now a two-league decision surface rather than a generic
  card directory. CFB and NFL retain distinct visual identity and advertise
  the parts of each workspace a reader can expect before entering.

## 2026-09-14 matchup semantics and roster hierarchy pass

- Passing-map color now encodes EPA outcome rather than attempt volume. Raw
  quarterback and receiver maps treat higher EPA as better; defense maps invert
  the interpretation because lower EPA allowed is better. Every map includes a
  visible legend so tint intensity cannot be mistaken for sample size.
- Pregame passing is reduced to one joined grid per offense. Each cell reports
  quarterback EPA/attempt, opponent EPA/attempt allowed, their signed
  difference, and an interaction share based on both sides' zone usage. Team
  colors identify which side owns the advantage without coloring the entire
  page chrome.
- Movement importance now selects role-specific PFF evidence. In particular,
  quarterbacks use deep, intermediate, short, and behind-LOS passing grades
  from `passing_depth`; the earlier accidental mapping to `rushing_summary` was
  removed. Transfer grades are selected from the prior club named by the
  roster movement (and true multi-club stints are averaged) rather than taking
  an arbitrary same-season row. Draft capital has full weight in Years 1–2, reduced weight in Years
  3–4, and no effect on movement ranking beginning in Year 5. Participation and
  available role grades continue to carry veteran evaluation.
- Depth rooms are grouped as offense, defense, and special teams. Offensive-line
  positions retain LT/LG/C/RG/RT ordering, while nose tackle and nickel roles
  remain distinct when the provider supplies those slots. Team schedules now
  show the team-perspective final score beside the result.
- Coach market context now replays the coach's prior game totals against the
  selected matchup's current total in addition to the existing current-spread,
  role, venue, and season splits.

## 2026-09-14 staff and availability pass

- `team_staff` stores a versioned team/season staff hierarchy. The initial 2026
  snapshot is extracted from ESPN's projection guide dated September 9 and
  includes head coach, offensive coordinator, offensive playcaller, defensive
  coordinator, and general manager. Playcaller is a separate attribute because
  the title holder and caller are not necessarily the same person.
- `injury_reports` stores current ESPN designations with ESPN and linked GSIS
  identities, injury area, practice/return fields when supplied, report copy,
  timestamps, and source attribution. Active transaction-news entries returned
  by the endpoint are discarded rather than mislabeled as injuries.
- The ESPN transport makes one league-wide request, caches it for 30 minutes,
  and falls back to the last valid document on a transport failure. It runs in
  the existing isolated `nfl-rosters` refresh child, preserving the shared
  lock and one-process-at-a-time memory contract. `sync-nfl-context` provides a
  smaller manual refresh path.
- Team overview pages show the staff hierarchy and identify the offensive
  playcaller. Depth-chart injury badges join by GSIS ID, then ESPN ID, then
  normalized name; hover and keyboard focus expose the full stored context.
  Upcoming matchup pages compare both current reports and rank availability by
  designation severity, starting depth, and recent participation. Completed
  historical matchups intentionally omit today's injury state.

## 2026-09-14 coaching tendencies and mobile pass

- `game_team_playcalling` is a compact derived table rebuilt with the existing
  PBP analytics job. It stores pass/rush, early-down pass, shotgun, and
  no-huddle counts by team/game; the browser receives aggregates rather than
  raw play-by-play. Existing situational aggregates remain the source for
  neutral-script pass rate, seconds per play, and plays per game.
- Team overview pages now separate staff identity from coaching evidence. The
  head coach row includes straight-up record, win rate, scoring margin, and
  stored-game sample. The offensive desk identifies the actual playcaller,
  preserves a separate OC title where applicable, and reports run/pass, pace,
  early-down aggression, shotgun, and no-huddle usage with denominators.
- Defensive coordinator context combines team EPA allowed, a PFF man/zone
  player-coverage-snap proxy, and the share of sacks plus quarterback hits
  generated by the defensive front, linebackers, and secondary. The UI marks
  blitz rate as not charted because neither current source identifies the
  number of pass rushers; this should be replaced when a licensed charting
  field is available rather than inferred from play outcome.
- Mobile uses a distinct reading hierarchy at 680px and below: compact hero
  identity, swipeable 50px team tabs, edge-to-edge sections, two-column summary
  rails, horizontally scrollable tables and pass maps, larger disclosure/toggle
  targets, fixed-width readable matchup scores, and bottom-sheet-style injury
  detail. Only the first position room in each unit begins expanded on small
  screens, reducing the initial roster-page scroll without hiding any player.

## Open questions to revisit once real usage exists

- Whether NFL content-link tables live in `nfl.sqlite3` or as a `league`
  column on the existing shared link tables -- deferred until the DB-split
  decision is exercised in step 3.
- Vendored port vs. runtime dependency on `scouting_report/nfl/`: this plan
  recommends porting a copy of `pffdata.py`/`data.py` rather than importing
  the sibling repo as a live dependency (matching `CHEATSHEET_NFL.md`'s own
  "deliberate port rather than a shared module" philosophy) -- two independent
  projects on independent release cadences shouldn't runtime-couple via
  `sys.path` tricks. Revisit only if the duplication actually causes drift
  pain in practice.
- Whether `SOS/` (repo root, precomputed strength-of-schedule data consumed by
  `nfl/sos.py`) is worth porting as-is or needs its own source investigation --
  not yet inspected.
- Whether `nfl_pff_player_metrics`' long-form grade rows need a
  `pff_supplemental_metrics`-equivalent (CFB's man/zone coverage splits,
  depth-of-target splits) in the first cut, or whether the seven core PFF
  families are enough for NFL parity at launch.
- Whether the win-probability/EPA calibration work from the CFB side
  (`win_probability_v2.py`, `expected_points_v2.py`) is worth generalizing into
  a shared module once both leagues have pre-computed and derived variants to
  compare.
## Render production initialization

The deployed NFL UI and the NFL database have separate lifecycles. A code deploy can
create the SQLite schema, but it cannot copy a developer workstation's data onto the
attached Render disk. Render also does not expose a persistent disk to build or
pre-deploy commands. Production therefore uses a runtime initializer:

- Render defaults `NFL_AUTO_SEED` on because an existing service might deploy a
  Blueprint change without importing newly declared environment variables. An
  explicit `NFL_AUTO_SEED=0` still disables it; other environments remain opt-in.
- Missing teams, games, or players launch `sports_aggregator.nfl.production_seed`.
- An atomic disk lock prevents worker recycling or overlapping deploys from starting
  duplicate population processes; failed attempts become eligible for retry after a
  bounded cooldown.
- Essentials run without play-by-play first, so schedules, teams, rosters, weekly
  production, snap counts, depth charts, IDs, staff, injuries, sources, and Elo become
  usable before the largest parquet file is loaded.
- Reporting follows essentials, then a no-PBP 2010-through-prior-season backfill.
  Current PBP and derived matchup analytics remain assigned to the low-traffic
  analytics refresh segment.
- `nfl_production_seed.json` and `nfl_production_seed.log` live beside
  `NFL_DATABASE_PATH`, making first-run progress visible across deploys. The dashboard
  API exposes the state as `production_seed`, and the empty-state page explains that
  population is in progress.

The rendered-page cache now versions against both the CFB and NFL SQLite files. NFL
writes consequently invalidate NFL pages instead of leaving the initial empty page in
the hour-long CFB cache bucket.

PFF remains a read-only authorized-data boundary. No licensed exports are committed
to the public application repository. Production points `NFL_PFF_SOURCE_ROOT` at
`/var/data/nfl_pff`; an operator must transfer an allowed snapshot there before the
PFF seed stage can run.
