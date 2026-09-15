# Sports News Aggregator

A Flask sports dashboard evolving from Cincinnati team pages into a reusable,
multi-league aggregation platform. College football is the first league on the
new shared pipeline.

## Current architecture

The active application factory is `app.create_app`. It exposes:

- `/` — league directory plus links to legacy dashboards
- `/college-football/` — College Football Today dashboard
- `/leagues/college-football/` — redirect to the CFB dashboard
- `/college-football/conferences/<slug>/` — conference stories, standings, games,
  current player leaders, and historical PFF context
- `/college-football/teams/<team_id>/` — full team schedule, roster, production,
  news, experience-first depth board, arrivals/departures, and prior-season
  player/position-group context
- `/college-football/teams/<team_id>/history/` - season index and filterable
  completed-game log with results, kickoff windows, opponent conferences, and PPG
- `/college-football/teams/<team_id>/history/stats/` - historical records,
  efficiency, traditional team production, and position-group identity alongside
  transparent PFF context
- `/college-football/players/<player_id>/` — player career path, season statistics,
  confirmed PFF grades, transfer/draft events, and player/team reporting
- `/college-football/games/<game_id>/` — full matchup preview with current CFBD
  metrics, series/coach/time-window/conference history, PFF unit comparisons,
  players to know, and attributed story clusters
- `/api/v1/leagues` — machine-readable league discovery
- `/api/v1/leagues/college-football/articles` — normalized article API
- `/api/v1/cfb/status` — structured-data freshness and row counts
- `/api/v1/cfb/games` — upcoming canonical CFBD games
- `/api/v1/cfb/games-to-watch` — scored upcoming games with explanation factors
- `/api/v1/cfb/matchups-to-watch` — the nearest week's top player/unit and
  unit/unit watches across games
- `/api/v1/cfb/teams` and `/api/v1/cfb/rankings` — canonical discovery data
- `/api/v1/cfb/conferences` and `/api/v1/cfb/conferences/<slug>` — conference discovery and view packets
- `/api/v1/cfb/teams/<team_id>` — team preview packet
- `/api/v1/cfb/teams/<team_id>/history` and `/history/stats` — historical game
  and production packets
- `/api/v1/cfb/players/<player_id>` — player identity, statistics, career, and stories
- `/api/v1/cfb/games/<game_id>/preview` — full game preview packet
- `/api/v1/cfb/teams/resolve?q=...` — exact normalized alias candidates
- `/college-football/admin/sources/` — curated Bluesky identity and coverage review
- `/college-football/admin/source-graph/` — unified entities, endpoints, and candidates
- `/api/v1/cfb/sources` — source metadata, DID status, and explicit coverage gaps
- `/api/v1/cfb/source-entities` — platform-neutral source graph
- `/api/v1/cfb/content` — normalized recent source content without raw payloads
- `/api/v1/cfb/games/<game_id>/content` — reporting layers for a scheduled game
- `/api/v1/cfb/games/<game_id>/matchups` — ranked unit matchups with reasons
- `/api/v1/cfb/developments` — content ranked by relevance rather than recency
- `/college-football/draft/` — 2027 draft watch: consensus board vs production profile
- `/api/v1/cfb/draft/board`, `/draft/consensus`, `/draft/reconcile` — draft packets
- `/college-football/feed.xml` — national reporting as RSS, linking to publishers
- `/college-football/teams/<team_id>/feed.xml` — one team's reporting as RSS
- `/sitemap.xml` and `/robots.txt` — canonical pages only; admin and API excluded
- `/college-football/scoreboard/?date=&conference=` — one day's games, grouped
  by the reader's calendar day rather than UTC, filterable by conference
- `/college-football/search/` — cross-entity search over teams, players, games, reporting
- `/college-football/admin/links/` — entity link audit with matched text and rule
- `/api/v1/cfb/links` — the same audit as JSON
- `/api/v1/cfb/games/<game_id>/player-matchups` — individual matchups in a game
- `/api/v1/cfb/games/<game_id>/situation` — schedule spot, travel, availability, market
- `/api/v1/cfb/games/<game_id>/weather`, `/fpi` — kickoff forecast and FPI packets
- `/api/v1/cfb/sources/status` — row counts, freshness and failures per source
- `/api/v1/cfb/search`, `/api/v1/cfb/transfers` — search and portal-impact packets
- `/api/v1/cfb/pff/summary?season=2025` — historical player and position-group signals
- `/reds/` and `/bengals/` — existing team dashboards, unchanged

The new `sports_aggregator/` package has four boundaries:

1. `models.py` defines stable provider-neutral data contracts.
2. `providers/` converts RSS, APIs, scrapers, or social sources into those contracts.
3. `service.py` runs sources concurrently, isolates failures, deduplicates results,
   sorts them, and caches each league briefly.
4. `catalog.py` declares leagues and their sources; `web.py` delivers the same data
   through HTML and JSON.

Presentation is its own boundary rather than template logic:

- `sports_aggregator/tables.py` defines `Column` and `Table`: a column names its
  key, header, and format once, and one Jinja macro decides markup, alignment,
  and number formatting. `pct` means 0-100 and `rate` means a 0-1 fraction, so a
  percentage cannot render at two different scales on two pages.
- `sports_aggregator/cfb/statlines.py` pivots the long-form
  `player_season_stats` store into conventional box-score lines. Column order,
  headers, and leaderboard qualifying minimums live in one spec per category.
- `sports_aggregator/cfb/views.py` turns repository packets into `Table`
  objects for schedules, standings, leaders, depth boards, roster movement,
  PFF grades, matchup metrics, and ranked developments. Team tables carry the
  school logo and color through the `_logo` / `_color` row conventions.
- `sports_aggregator/cfb/matchups.py` ranks unit-versus-unit comparisons by how
  watchable they are — quality, separation, and mutual strength — and labels
  each one. It consumes a provider-neutral `MatchupSignal`, so compiled season
  statistics and models can feed the same ranking without touching callers.
- `sports_aggregator/providers/sportsdataverse.py` downloads static SportsDataverse
  release assets, resolving URLs through the release API and validating that an
  asset actually carries the expected columns before it is trusted.
- `sports_aggregator/providers/weather.py` fetches Open-Meteo forecasts per venue
  and aligns them to kickoff. No API key is required.
- `sports_aggregator/cfb/external.py` stores secondary sources keyed to canonical
  CFBD entities, with provenance on every row and `import_runs` recording every
  attempt. See [docs/SECONDARY_SOURCES.md](docs/SECONDARY_SOURCES.md).
- `sports_aggregator/bootstrap.py` is the single entry point: `initial`,
  `refresh`, `status` and `plan`. Each step runs isolated so one unavailable
  source cannot stop the rest.
- `sports_aggregator/cfb/unit_continuity.py` answers, for a graded unit, how
  much of the snaps behind last season's PFF grade are still on the roster, and
  blends the prior grade against current-season play by credibility. Continuity
  is decided against the current roster, never against PFF's stored
  `cfbd_player_id`: that link is written against the roster of the season being
  imported, so a departed player is unresolved, and counting only linked players
  reports a unit that lost its five highest-snap players as fully returning.
  Before a snap is played the adjusted grade is exactly last season's; as games
  accumulate it shifts toward current play, retaining a residual proportional to
  how much of the unit carried over.
- `sports_aggregator/cfb/roster_production.py` splits prior-season production
  into returning, arrived and departed, so a preseason page says what is on the
  roster rather than who led the team last year. Arrived production always names
  the school it was earned at.
- `sports_aggregator/cfb/search.py` matches a query against team aliases, person
  names, matchups and headlines, and returns why each result matched.
- `sports_aggregator/cfb/lines.py` stores betting lines per provider with opening
  and current numbers kept apart. Books are never averaged into one number.
- `sports_aggregator/cfb/situations.py` derives schedule spots, travel and
  time-zone burden, and availability reporting for a game.
- `sports_aggregator/cfb/transfers.py` ranks portal entries on prior production
  first, grade second and recruiting opinion last. A transfer with no record is
  reported as unproven, which is not the same as low impact.
- `sports_aggregator/social/team_reddit.py` keeps team subreddits in a registry
  and activates them by the week's schedule rather than sweeping all 138.
- `sports_aggregator/cfb/player_matchups.py` combines direct line assignments with
  player-versus-unit watches. WR/CB is one-on-one only for a substantial heavy-man
  sample; otherwise receivers face the secondary, receiving backs face linebackers,
  and tight ends face linebackers and safeties. Draft standing can raise a credible
  watch but cannot create one.
- `sports_aggregator/social/context.py` gates the widest resolution rule. An
  unscoped player match is blocked by professional-football vocabulary and by a
  coaching title beside the name, because shared names across levels were the
  main source of wrong links.
- `sports_aggregator/social/sport.py` persists an explainable sport decision
  before incoming team, player, or game candidates become accepted links.
  Explicit other-sport and NFL items are rejected; uncertain items remain stored
  for editorial review but cannot enter CFB pages or story clusters.
- `sports_aggregator/cfb/identity.py` derives a contrast-checked accent for each
  team from its CFBD color and holds the editorial conference palette. Team
  colors are chosen for helmets, so a near-white one is darkened only as far as
  it must be to stay visible rather than replaced.
- `sports_aggregator/social/roles.py` decides what an item *is* — original report,
  corroboration, analysis, opinion — from markers in the text plus its position in
  a story cluster, and keeps the evidence for every verdict. It replaces the
  `REPORTING_UNDETERMINED` placeholder that every journalist's post received.
- `sports_aggregator/cfb/draft.py` builds a prospect board calibrated on the
  completed draft: 247 of the 2026 picks are matched to their prior-season PFF
  profile, and returners are placed against that distribution by position.
- `sports_aggregator/cfb/prospects.py` imports an external consensus board with
  provenance and reconciles it against that profile board, reporting agreement,
  disagreement, and missing evidence separately.
- `sports_aggregator/social/media.py` validates YouTube channels and podcast
  feeds before they can enter the trusted registry, and attaches both endpoints
  to the same show entity so one programme distributed twice does not gain
  double authority.
- `sports_aggregator/social/relevance.py` scores content on source expertise for
  the topic at hand, role, topic importance, recency half-life, and how
  specifically the item resolved to a team, player, or game. Every score keeps
  its factors as text.
- `templates/_layout.html`, `templates/_tables.html`, and `static/cfb.css` are
  the shared page shell, table macros, and stylesheet. Pages previously carried
  a private copy of the same CSS.
- `sports_aggregator/cfb/meta.py` builds one sharing packet per page kind, so a
  pasted link renders as a card describing what the page actually holds. A card
  claims only the layers that populated for that page.
- `sports_aggregator/cfb/syndication.py` emits RSS feeds, `sitemap.xml`, and
  `robots.txt`. Feed items link to the original publisher and name them, so
  attribution survives the hop; a story with no resolvable source URL is
  dropped rather than relinked internally.
- The stylesheet carries a light and a dark theme. Every color is a token; a
  literal hex outside the token blocks is a bug, and `tests/test_theme.py`
  fails on one. Team identity arrives as `--team-light`/`--team-dark` pairs
  because a color legible on cream is not legible on charcoal and the
  correction runs in opposite directions. The theme resolves in an inline
  script before the stylesheet loads, so no page paints light and then snaps.
- `sports_aggregator/page_cache.py` caches rendered pages against the database's
  modification time, so a refresh in another process invalidates them without
  any signalling between the two. Pages here are ~100% CPU — a matchup render
  spends 1,312 ms of CPU against 1,368 ms of wall clock — so the GIL has no I/O
  to overlap and threads add nothing: eight concurrent requests took 21.8
  seconds and throughput *fell* to 0.37/s. Cached, the same page serves in
  single-digit milliseconds and throughput scales past 200/s.
- Story lists disclose their own provenance: how the story was grouped, who
  filed it and in what role, and whether it has moved since. Every value was
  already stored by the clustering and role pipelines.
- Team marks follow the theme. CFBD publishes both variants for every school
  (`logos/500/N.png` and `logos-dark/500/N.png`); both reach the page and CSS
  chooses, because a `<picture>` element answers only to the media query and
  never to the manual toggle. Conferences have no logo in the CFBD data at all
  — `/conferences` carries only name, abbreviation and member count — so a
  conference is marked by its own abbreviation set in its palette color, on the
  dashboard strip, conference pages, matchup pages, and conference table
  columns. `sports_aggregator/cfb/identity.py` owns both.
- The `roles` step determines what each item is and records the evidence. It
  runs after clustering, because determination reads an item's position in its
  cluster, and before scoring, because relevance weights the role.

The `sports_aggregator/cfb/` package adds the structured college-football path:

- `cfbd.py` is the authenticated current CFBD REST adapter with retries and a raw
  response cache.
- `models.py` maps CFBD team/game IDs into provider-neutral entities.
- `repository.py` owns the normalized SQLite schema and safe upserts.
- `sync.py` isolates each weekly dataset so one unavailable access tier does not
  discard successful updates.
- `insights.py` contains an intentionally provisional, explainable game-attention
  score. It is not presented as the final importance model.
- `pff.py` imports the user-provided 2025 PFF snapshots with source-file provenance,
  conservative player matching, usage-weighted position-group summaries, and
  regular-season scheme/depth/run-defense detail for player cards and matchups.

The parallel `sports_aggregator/social/` package is the curated reporting boundary.
It models people, publications, shows, organizations, and communities once, then
attaches Bluesky, Reddit, RSS, API, YouTube, and podcast endpoints. It also stores
multidimensional expertise, resolves stable platform identities, persists normalized
content, and conservatively attaches topics, teams, players, and games. The legacy
Reds and Bengals integrations remain unchanged.

National articles currently come from ESPN, Yahoo Sports' college-football RSS
feed, and the official NCAA.com FBS RSS feed. The source graph records each
publisher independently and NCAA.com as an official primary source, so items
retain the right attribution and role. Provider terms still apply: preserve
attribution and links, and do not modify syndicated content.

The nationwide local reporting registry researches every current FBS program,
normalizes publishers, verifies recurring team coverage and machine-readable
fallbacks, rejects weak cross-state opponent mappings, and imports the results into
the unified source graph. See
[docs/LOCAL_SOURCE_REGISTRY.md](docs/LOCAL_SOURCE_REGISTRY.md) and the generated
artifacts in [`data/local_sources/`](data/local_sources/).

## Run locally

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

`requirements.txt` is the web service and the refresh jobs, which is what Render
installs. The legacy Reds/Bengals dashboards are disabled in production but on
by default locally, and they need a second file:

```powershell
python -m pip install -r requirements.txt -r requirements-dashboards.txt
```

Set `REGISTER_LEGACY_DASHBOARDS=0` in `.env` to skip them instead -- the CFB
pages do not use them, and leaving them on runs the baseball scrapers on every
start.

Add `CFBD_API_KEY` to `.env`, then use the orchestrator for a complete build or
routine refresh:

```powershell
python -m sports_aggregator.bootstrap plan --season 2026
python -m sports_aggregator.bootstrap initial --season 2026
python -m sports_aggregator.bootstrap refresh --season 2026
python -m sports_aggregator.bootstrap status --season 2026
python -m sports_aggregator.bootstrap history --season 2026
```

On Windows, register lock-safe refreshes for 6:00 AM, noon, 6:00 PM, and
11:00 PM local time:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_refresh_task.ps1
```

Missed runs start when the computer becomes available; overlapping runs are
skipped. Logs and compact run history live under `instance/`. See
[`docs/SCHEDULED_REFRESH.md`](docs/SCHEDULED_REFRESH.md) for operations and
custom schedules.

For Render, use the included [`render.yaml`](render.yaml) Cron Job trigger rather
than refreshing SQLite inside a separate service. The web service needs a
persistent disk and a shared `CFB_REFRESH_TOKEN`; deployment details are in
[`docs/SCHEDULED_REFRESH.md`](docs/SCHEDULED_REFRESH.md#render).

The Blueprint mounts a 5 GB disk at `/var/data` and keeps SQLite, CFBD responses,
SportsDataverse assets, and weather responses there. Consequently, only a brand-new
empty disk needs `bootstrap initial`; code rebuilds reuse the stored database and
routine `bootstrap refresh` calls only update moving datasets. The CFB Render service
uses one threaded Gunicorn worker and leaves the memory-heavy legacy dashboards off,
which preserves headroom for an in-service refresh subprocess.

`initial` builds canonical teams first, prepares the source registry, and ingests
national RSS before the more memory-intensive model and media work. It then adds
current and prior-season games, coaches and production, models, roster lifecycle,
PFF, transfer identity links, draft data, weather, social/media ingestion,
team-scoped local RSS, retagging, clustering, and relevance scores. `refresh`
similarly prioritizes national RSS immediately after the canonical CFBD sync so a
later degraded optional step cannot leave the Articles stream empty. Local RSS
responses are processed as each feed completes instead of being accumulated in
memory. RSS commands record attempted/succeeded endpoints, item counts, and errors;
these diagnostics appear in `bootstrap status` and `/api/v1/cfb/status`. Week 0 is retained as a real scheduled week. Optional
sources are visibly skipped when their credentials are unavailable; Reddit
requires both its client ID and client secret.

The current-season player-stat step is season-dependent and non-blocking: CFBD can
legitimately publish zero rows before games are played. The prior-season baseline
remains available until current production appears, and a failed/empty refresh does
not erase the last successful snapshot.

Backfill prior seasons so team and matchup history pages have canonical results,
coach attribution, traditional/advanced team stats, and player position production.
The orchestrated `history` phase runs each historical season in its own process;
it is intentionally separate from the live refresh path:

```powershell
python -m sports_aggregator.bootstrap history --season 2026
python -m sports_aggregator.bootstrap history --season 2026 --from-year 2000 --to-year 2025
python -m sports_aggregator.cfb.cli sync-history --from-year 2019 --to-year 2025
```

Historical synchronization is append-only by default. Once a completed-season
dataset is stored in SQLite, later history runs skip its CFBD request. An interrupted
player or box-score backfill resumes its missing conference or dataset; `--force`
is the explicit way to replace a completed snapshot. Expanding `--from-year` simply
adds older seasons, while the normal `refresh` phase updates only moving current-season data.

`sync-history` stores games, records, traditional team stats, advanced team stats,
and CFBD head-coach seasons. The player-history workers store rosters and player
season stats; `sync-box-scores` stores normalized team and player game lines. Team,
game, player, and box-score pages read those SQLite tables and never call CFBD during
a page request. `bootstrap status` reports
both conference player-stat gaps and per-season history coverage. Kickoff-window
splits use US Eastern broadcast time; coach-versus-opponent records are explicitly
season-attributed because intra-season interim changes may not be game-exact.

A team promoted from FCS has no history in any FBS-filtered dataset. `sync-promoted`
finds those teams and fetches their prior seasons from the conference they actually
played in; `coverage` reports which conference-seasons are missing:

```powershell
python -m sports_aggregator.cfb.cli sync-promoted --year 2026 --from-year 2024 --to-year 2025
python -m sports_aggregator.cfb.cli sync-venues --year 2026
python -m sports_aggregator.cfb.cli sync-lines --year 2026
python -m sports_aggregator.cfb.cli sync-recruits --year 2026
python -m sports_aggregator.cfb.cli link-transfer-grades --year 2026
python -m sports_aggregator.cfb.cli coverage
```

Use `--force` only when intentionally bypassing the raw-response cache. Use
`--basic` to omit advanced statistics and CORE ratings. The equivalent Flask CLI
command is `flask --app app sync-cfb --year 2026`.

The player-stat sync resolves CFBD conference display names to its official API
abbreviations. Use `--conference "Big Ten"` to refresh one conference. During
preseason, the UI automatically falls back to the newest available prior-season
player statistics and labels that season explicitly.

`sync-roster-context` loads the prior roster plus current transfer portal, NFL Draft,
and returning-production datasets. Team pages identify sourced transfers/draft picks,
infer possible graduation or eligibility departures only from class/roster comparison,
and label that inference instead of presenting it as confirmed reporting.

Import the local 2025 PFF snapshot after a 2026 roster sync:

```powershell
python -m sports_aggregator.cfb.pff_cli import --season 2025 --roster-season 2026 --directory PFF
```

Seed and verify the curated Bluesky registry:

```powershell
python -m sports_aggregator.social.cli seed
python -m sports_aggregator.social.cli resolve
python -m sports_aggregator.social.cli status
python -m sports_aggregator.social.cli prepare
python -m sports_aggregator.social.cli validate-reddit
python -m sports_aggregator.social.cli unified-status
```

Resolution is intentionally strict: both the handle resolver and actor profile must
agree on the DID/current handle. A transient failure does not erase a previously
verified DID.

Run the public Bluesky author-feed ingestion as a background/scheduled command:

```powershell
python -m sports_aggregator.social.content_cli ingest --season 2026 --limit 10
python -m sports_aggregator.social.content_cli ingest-reddit --season 2026 --limit 25
python -m sports_aggregator.social.content_cli ingest-youtube --season 2026 --limit 20
python -m sports_aggregator.social.content_cli ingest-podcasts --season 2026 --limit 20
python -m sports_aggregator.social.content_cli ingest-reporting --season 2026
python -m sports_aggregator.social.content_cli cluster
python -m sports_aggregator.social.content_cli score
python -m sports_aggregator.social.content_cli review-export --limit 50 --review-mode triage --reviewer editorial
python -m sports_aggregator.social.content_cli review-import --input instance/cfb_content_review.csv --reviewer editorial
python -m sports_aggregator.social.content_cli review-report --reviewer editorial
python -m sports_aggregator.cfb.prospects_cli 2027_nfl_mock_draft_database_top_100.csv     --draft-year 2027 --roster-season 2026 --source mock_draft_database_consensus
python -m sports_aggregator.social.content_cli status --limit 50
```

`ingest-reddit` needs `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and
`REDDIT_USER_AGENT`; the unauthenticated endpoint is blocked. Submissions are
classified (`LINK_DISCOVERY`, `GAME_THREAD`, `ANALYSIS`, `RUMOR`, …) and a
submission that links out credits the external publisher, keeping the subreddit
only as the discovery endpoint.

`retag` re-runs topic and entity resolution over stored content without
re-fetching any source, so a tagging-rule change reaches the whole archive. It
now runs the persisted sport gate first. `ACCEPT` items can receive entity links;
`REJECT` and `REVIEW` items keep their text and decision evidence but have their
team, player, game, and conference links cleared:

```powershell
python -m sports_aggregator.social.content_cli retag --season 2026
```

`content_cli status` reports `cfb_eligible`, `sport_rejected`, and
`sport_review`. Use `review-export --review-mode triage` to inspect uncertain
sport decisions alongside borderline entity links. The older
`prune-local-non-football` command remains available as an explicit cleanup tool,
but scheduled refreshes no longer delete those rows because retained rejections
are needed for classifier evaluation.

Game links are evidence-based. Two resolved opponents uniquely identify their
scheduled meeting; a one-team item must also contain game language, and
preview/recap wording plus publication time chooses the appropriate direction.
An explicit `Week 0` or `Week Zero` is retained and matched as week zero. This
prevents recruiting, transfer, and facilities articles from silently inheriting
a team's next game. Player matching checks the active roster first, then the prior
season at reduced confidence for recent graduates, draftees, and departures;
headline matches are separately recorded and boosted.

The default review export is exception-focused: it selects uncertain roles,
ranking-boundary items, missing scope, borderline entity links, and classifier
disagreements. This makes a 25–50 row triage pass more useful than reviewing every
ordinary item. Periodically use `--review-mode stratified --limit 25` as a blind
quality audit. The three `review-*` commands persist completed labels and report
relevance precision/recall, topic and entity
multilabel precision/recall, role accuracy, and the rank correlation between the
feed score and a human 1–5 priority. Blank label cells are ignored; enter `NONE`
to review a topic/team/player field as explicitly empty. See
[docs/CLASSIFIER_REVIEW.md](docs/CLASSIFIER_REVIEW.md).

Set `YOUTUBE_API_KEY` (or `YOUTUBE_API`, which is also accepted) before validating
or polling any YouTube candidate. Candidate names are not production endpoints:
channel IDs and podcast feeds are discovered through search, scored on name
agreement, audience, publishing history, and topical vocabulary, and only clear
matches are promoted. Search rank never decides identity — a query for "Split Zone
Duo" returns a four-subscriber channel above the real show, and one for "Joel Klatt
Show" returns a channel with no videos.

The versioned [CFB media registry](data/media/cfb_media_registry.json) now preserves
the original researched seed list and the application's earlier national candidates,
adds verified team/conference discoveries, stores access and original-reporting
evidence, and records unresolved team gaps. `media-seed` runs before validation on
both initial builds and refreshes. Ambiguous identities are retried weekly rather
than on every scheduled refresh, and an exact researched channel can qualify without
the search-only 5,000-subscriber corroborator. See
[docs/CFB_MEDIA_REGISTRY.md](docs/CFB_MEDIA_REGISTRY.md).

```powershell
python -m sports_aggregator.social.media_cli seed
python -m sports_aggregator.social.media_cli validate-all
python -m sports_aggregator.social.media_cli validate-all --force
```

Andy Staples (who publishes on the On3 channel) and College Football Enquirer (a
Yahoo Sports programme) must not be attached to a parent-brand channel merely
because it ranks highly in search.

To run only the lightweight shared league platform while legacy pages are being
migrated:

```powershell
$env:REGISTER_LEGACY_DASHBOARDS = "0"
python run.py
```

For production, set `FLASK_DEBUG=0`, keep secrets outside source control, use a
real shared cache when running multiple workers, and run the app behind a WSGI
server.

## Add another league

The NFL is now the second cataloged league. Its dashboard is available at
`/nfl/`, with linked team (`/nfl/teams/<abbr>/`), game
(`/nfl/games/<game_id>/`), and player (`/nfl/players/<gsis_id>/`) pages.
Matching JSON packets live under `/api/v1/nfl`, and headlines remain available
at `/api/v1/nfl/articles`. Ongoing work is tracked in
[`docs/NFL_ARCHITECTURE.md`](docs/NFL_ARCHITECTURE.md).

Build or refresh the isolated NFL database with:

```powershell
flask --app app sync-nfl --year 2026
```

For a lightweight roster-only refresh between full data runs:

```powershell
flask --app app sync-nfl-rosters --year 2026
```

That lightweight command also refreshes the cached league-wide ESPN injury
report and the versioned staff directory. To update only those context layers:

```powershell
flask --app app sync-nfl-context --year 2026
```

Import NFL PFF exports from the sibling `scouting_report` repository, then
rebuild play-by-play analytics from the local nflverse cache with:

```powershell
flask --app app sync-nfl-pff --year 2025
flask --app app sync-nfl-pbp-analytics --year 2025
flask --app app sync-nfl-history --start-year 2010 --end-year 2025
```

The history command is a one-time/seeding operation, not part of every
scheduled refresh. It processes seasons serially and reads only the PBP
columns used by EPA, pace, situational, play-calling, and passing-location
models. The PBP analytics command also rebuilds team run/pass, early-down pass,
shotgun, and no-huddle rates; it does not add another scheduled process.

`NFL_PFF_SOURCE_ROOT` defaults to
`C:\Users\ehari\Desktop\scouting_report`. The importer reads that tree in
place and writes normalized results into this project's NFL SQLite database;
it does not copy or modify the source exports. The current 2025 import covers
nine PFF families, 4,749 family/player rows, and 430,317 numeric metrics.

Refresh league reporting and the curated public Bluesky feeds separately with:

```powershell
flask --app app sync-nfl-content --year 2026 --posts-per-source 4
```

Reporting is organized into Articles, News Wire, Analysis, Personnel,
Players + Usage, and Team Beats using the sections carried by the curated NFL
directory. The same organized streams appear on league, team, player, and
matchup pages. `/nfl/sources/` reconciles every configured account against
stored output and the latest instrumented fetch; filters expose producing,
silent, failed, and not-yet-checked sources by team. Its JSON counterpart is
`/api/v1/nfl/sources`.

The content refresh uses Bluesky's unauthenticated public AppView reads. It
stores NFL content and entity links in `nfl.sqlite3`, separate from CFB's
integer team/game link tables. The first live 2026 refresh stored 23 RSS
articles and 490 original Bluesky posts. After current-roster normalization,
the latest instrumented refresh leaves 691 unique items, 673 team links, 510
player links, and 107 matchup links; stale player links are pruned on roster
replacement. All 158 configured Bluesky sources were attempted: 146 currently
have historical stored output, six returned source errors, three returned empty
feeds, and three returned posts with no eligible original item. The linked stream is available at
`/api/v1/nfl/content`, with
optional `team`, `player`, or `game` filters.
Each refresh also writes per-account checks and an ingestion-run ledger with
attempted, succeeded, seen, stored, and error counts. Historical content can
show that an account has produced material before this ledger existed; running
`sync-nfl-content` once populates the current fetch status for every attempted
account.

The command syncs nflverse teams, schedules, the latest roster row per
player/team, the canonical nflverse player master and DynastyProcess ID map,
long-form weekly statistics, game-level snap counts, and timestamped depth-chart
history, current ESPN injury designations, the versioned coaching-staff
snapshot, weekly team production, and compact play-by-play efficiency summaries
into `instance/nfl.sqlite3` by default. Team pages expose
the current depth snapshot, injury detail, staff structure, and season snap
usage while retaining the underlying
history. Set `NFL_DATABASE_PATH` and `NFLVERSE_RAW_CACHE_PATH` to relocate the
database and raw parquet cache.

The roster releases are normalized to the current organization by retaining
active, reserve, developmental/practice-squad, inactive, and exempt players
while excluding cut, retired, and traded-away rows. After this normalization,
the live 2026 refresh on 2026-09-13 stored 2,524 current roster identities and
272 scheduled games; the normalized 2025 comparison set contains 2,662.
Team pages compare exact GSIS IDs against that 2025 release to
label arrivals, rookies, prior NFL teams, departures, and known destination
teams. A player missing from both adjacent team rosters is not guessed by name.

The NFL dashboard ranks teams by net EPA, team pages summarize offensive and
defensive efficiency, and game pages now build a full matchup briefing. During
the opening slate, the dashboard keeps the current schedule, standings,
rosters, and coverage but uses a labeled prior-season power table and leader
board until at least 24 teams have current play-by-play profiles. Each
game compares both offensive units with the opposing defense across overall,
dropback, rushing, success-rate, and explosive-play efficiency; it also shows
season production, the five games entering the matchup, team production
leaders, market context, and a postgame review. During the opening weeks, the
page uses a clearly labeled prior-season baseline until both teams have a
current pregame sample. A transparent offense-versus-points-allowed scoring
midpoint and ranked matchup watches make that baseline easier to interpret
without presenting it as a predictive model. Player box scores are split into
compact passing, rushing, receiving, and defense tables instead of one wide
matrix. Schedule context adds rest, divisional status, venue conditions, and
stored prior meetings. Player pages likewise replace the universal wide game
log with only the passing, rushing, receiving, defensive, or kicking tables the
player actually needs. Team and matchup pages also show current-roster target,
carry, and combined opportunity shares, plus exact-ID retention across eight
position units. Current player pages fall back to a labeled prior-season role
profile when their new-season production has not started. These summaries use
rushes and dropbacks only; special-teams events are
excluded from the scrimmage efficiency denominator.

Team overview pages pair the 2026 HC/OC/playcaller/DC directory with stored
performance and tactical evidence. Offensive tendencies show run/pass mix,
neutral and early-down pass rate, pace, shotgun, and no-huddle usage. Defensive
tendencies show EPA allowed, PFF man/zone player-snap shares, and the position
groups producing sacks plus quarterback hits. Exact blitz rate remains visibly
unavailable until the configured feeds provide pass-rusher count data.

The dashboard now separates season and selected-week leaders and includes
minimum-sample PFF leaderboards. `/nfl/pff/` provides a filterable explorer;
team and player pages surface linked grades, player pages preserve draft
pedigree, and team/player form is rendered as dark, responsive weekly charts.
The play-by-play layer stores quarterback attempts by behind/short/intermediate/
deep depth and left/middle/right location. QB pages show the resulting passing
map, while game pages invert the same data to show how each opposing defense
has performed by zone. Matchup interaction cards combine current-roster PFF
alignment, route grades, prior target share, slot-coverage samples, and those
defensive PBP zones. They are labeled as likely interactions rather than exact
coverage assignments.

The dashboard also computes all eight division standings directly from completed
regular-season games. Records, point differential, and streaks flow into team
pages and JSON packets. NFL presentation is split between the base dark theme in
`static/nfl.css` and the responsive navigation, standings, identity, and
interaction components in `static/nfl_components.css`.
Current games render as matchup cards with team identity, record, score, and EPA
context, with the sortable schedule available directly below them.

Every NFL page loads `static/nfl.css`, which declares a dark color scheme and
explicit dark backgrounds for the document, shared data tables, alternating
rows, empty states, and form controls. This prevents the shared CFB table layer
or browser defaults from introducing light surfaces into the NFL section.

NFL refreshes now participate in the same locked scheduled plan as CFB. The
light profile refreshes NFL rosters and reporting; the heavy profile also
refreshes schedules, weekly stats, snaps/depth, PBP analytics, and the local
PFF baseline. Each segment runs serially in its own subprocess under the
existing child-memory limit.

Team pages now include significance-filtered personnel movement and full
position rooms. Player and team charts support selectable normalized overlays
plus undistorted absolute-value plots. Receiver pages include target/reception
maps, while matchup pages connect those field sections to opponent results
allowed and add highlighted unit edges, game-shape components, and trench
analysis. Reporting is ordered by editorial grade with a modest freshness
bonus; hover the displayed grade to see its reasons.

The NFL landing desk also maintains a continuous Elo history from the 2010
season onward, with every franchise entering at 1500. Elo quality/closeness,
division and standings context, and PFF player quality feed the Games to Watch
ranking. A provisional 2027 draft panel reads the CFB consensus board and
matches it to current NFL order and roster need; established returning
quarterbacks suppress quarterback selections. These rankings are navigation
and context tools, not betting or draft-outcome models.

The sync also imports the curated
[`data/nfl/NFL_Bluesky_Directory.xlsx`](data/nfl/NFL_Bluesky_Directory.xlsx)
when present. Validate or import that directory independently with:

```powershell
python -m sports_aggregator.nfl.sources_cli validate
flask --app app import-nfl-sources
```

Its 158 accounts retain league, conference, division, team, account-type,
coverage, specialty, priority, and recommended-list metadata. NFL scope lives
beside the existing source graph, so a handle shared with CFB does not lose its
college-football tags.

The candidate data catalog in
[`data/nfl/NFL_Data_Sources_Directory.xlsx`](data/nfl/NFL_Data_Sources_Directory.xlsx)
can be validated and summarized without an Excel dependency:

```powershell
python -m sports_aggregator.nfl.data_sources
```

The current intake policy excludes paid/trial commercial APIs. Sources marked
free for non-commercial use remain visible with their license restriction so
they can be evaluated without being mistaken for unrestricted production data.

For an RSS-backed league, add a `LeagueConfig` and its `FeedConfig` values to
`sports_aggregator/catalog.py`. Discovery, the league page, caching, aggregation,
and JSON endpoints are automatic.

For a non-RSS source:

1. Implement the small `NewsProvider` protocol in `sports_aggregator/providers/`.
2. Normalize every record to `Article`.
3. Register the provider in `build_default_service`.
4. Add parsing and failure-isolation tests.

Avoid putting provider request code in Flask views. That separation is what makes
the same source usable by web requests, scheduled ingestion, and future workers.

## Repository assessment

The prototype contains useful integrations, but they sit at different stages of
refactoring:

- `reds/` already separates scrapers, stats, social, charts, and utilities, but its
  public models and orchestration remain Reds-specific.
- `blueprints/bengals.py` is a large module with repeated aggregation/authentication
  definitions and source-specific parsing mixed into the route layer.
- `blueprints/reds.py` appears to be an older duplicate of the active `reds/`
  package and should be retired after behavior is compared.
- `nfl_prospect_scraper_v5.py` is a capable standalone search CLI. Its source
  functions are candidates for provider adapters, but draft-prospect search should
  remain a separate feature from the general league headline feed.
- Generated charts, a local virtual environment, secrets, and application code
  currently share the repository root. `.gitignore` and `.env.example` now define
  the intended boundary; existing local files were not removed.
- The previous `routes.py` contained an invalid demo callback and a second Flask
  app. It is now only a compatibility entry point to the canonical factory.

## Recommended development sequence

Phase 1 is now implemented: current CFBD teams, games, media, overall/conference records, poll
rankings, basic team statistics, advanced statistics, and CORE ratings have
cache/persistence adapters. Empty preseason datasets are valid and will populate
as CFBD publishes in-season data.

The unified source graph, Reddit discovery normalization, durable Bluesky ingestion,
multilabel topic rules, conservative team/player/game candidates, and game-page
reporting layers are implemented. Cross-source story clustering preserves URL
identity, rejects platform permalinks and repeated source homepages as story keys,
and requires independent-source evidence for similarity merges. Earliest-report
attribution remains a confidence-scored candidate, not a fact. Conference hubs plus
full team and game preview shells consume the same repository packets in HTML and
JSON. The next sequence is:

1. **Classifier measurement:** label exception-focused 25–50 item triage packets,
   retain small stratified audits, and use their precision/recall reports to tune
   topics, roles, entity thresholds, and ranking.
2. **In-season exercise:** replay completed prior-season weeks through advanced
   stats, CORE ratings, recaps, situation flags, and result-driven Elo before Week 1.
3. **Scheduling:** run the refresh outside web requests so weather, odds, injuries,
   and reporting do not decay between manual runs.
4. **Official-source expansion:** validate athletics/conference endpoints and add
   game notes, releases, press conferences, and official video.
5. **Coverage expansion:** add verified beat sources outside the power four after
   clustering and classifier thresholds are measured.
6. **Migrate legacy pages:** adapt Reds and Bengals sources one at a time, then
   remove the duplicate modules after parity tests pass.
7. **Production hardening:** add database migrations, structured logging, request
   timeouts/retries, source rate limits, health checks, and CI.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The test suite covers normalization, RSS/Reddit/YouTube/podcast adapters, external
publisher credit, provider failure isolation, source-graph identity, content topics,
team/player matching, story clusters and source roles, CFBD authentication and caching,
conference standings/player leaders, roster lifecycle classification, PFF import,
SQLite persistence, conference/team/player/game HTML routes, story destinations,
JSON view packets, limits, and 404 behavior. `tests/test_tables.py` additionally
covers cell formatting and scale, stat-line pivoting and column order, leaderboard
qualifying minimums, schedule win/loss derivation, and the rendered player page
emitting a real pivoted table. `tests/test_relevance.py` covers topic half-lives,
expertise selection, beat-versus-pundit ordering, Reddit crosspost detection and
publisher credit, matchup archetypes and sample discounting, and score
persistence. `tests/test_media.py` covers YouTube key naming, channel and feed
validation including impersonator and unrelated-brand rejection, video
classification, the team resolver (three-letter programmes, case sensitivity,
lead prominence, and roundup demotion), and the per-source streams.
`tests/test_draft.py` covers role determination and its precedence rules, board
parsing, name-suffix and school-alias matching, percentile calibration, and the
separation of missing evidence from genuine disagreement.
`tests/test_presentation.py` covers headline extraction from promotional video
descriptions, color contrast and conference palette readability, position
abbreviations, the separation of team reporting from conference context, and the
mobile-first stylesheet rules, individual matchup pairing and its draft
weighting, the link-audit shape, the unscoped-match guard, initial-collapsing name
normalization, and statistical coverage-gap reporting. `tests/test_features.py` covers search
scoring and abbreviated school names, per-provider line storage and movement,
travel and time-zone derivation, transfer impact evidence, and team-subreddit
activation.
`tests/test_review.py` covers classifier metric math and the export/import/report
round trip; `tests/test_stories.py` protects URL identity, platform-link rejection,
boilerplate-link handling, cross-source merging, and same-source separation.

See [docs/CFB_ARCHITECTURE.md](docs/CFB_ARCHITECTURE.md) for schema ownership,
cache policy, verified CFBD endpoints, and the next entity-linking boundary.
See [docs/CFB_NEWS_AGGREGATION.md](docs/CFB_NEWS_AGGREGATION.md) for the Bluesky
registry assessment, PFF identity policy, and incremental news roadmap.
