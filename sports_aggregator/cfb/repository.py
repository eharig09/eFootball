"""SQLite persistence for canonical CFBD entities and raw-derived metrics."""

from __future__ import annotations

from bisect import bisect_left
from contextlib import closing, contextmanager
from functools import wraps
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import os
from pathlib import Path
import sqlite3
import re
from typing import Any, Iterable, Iterator

from sports_aggregator.cfb.identity import conference_slug as _conference_slug
from sports_aggregator.cfb.models import Game, PollRanking, Team, normalize_alias
from sports_aggregator.cfb.statlines import (
    CATEGORY_ORDER, category_columns, category_label, qualifier, sort_stat)
from sports_aggregator.nfl.ranking import rank_within


SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    school TEXT NOT NULL UNIQUE,
    mascot TEXT,
    abbreviation TEXT,
    conference TEXT,
    division TEXT,
    classification TEXT,
    color TEXT,
    alternate_color TEXT,
    logos_json TEXT NOT NULL DEFAULT '[]',
    venue_id INTEGER,
    venue_name TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_aliases (
    team_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    alias_type TEXT NOT NULL DEFAULT 'cfbd',
    PRIMARY KEY (team_id, normalized_alias),
    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_team_alias_normalized ON team_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS players (
 season INTEGER NOT NULL, player_id TEXT NOT NULL, first_name TEXT NOT NULL,
 last_name TEXT NOT NULL, normalized_name TEXT NOT NULL, team TEXT NOT NULL,
 position TEXT, jersey INTEGER, height REAL, weight INTEGER, class_year INTEGER,
 PRIMARY KEY(season,player_id,team));
CREATE INDEX IF NOT EXISTS idx_players_name ON players(season,normalized_name);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(season,team);

CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    season_type TEXT NOT NULL,
    start_date TEXT NOT NULL,
    start_time_tbd INTEGER NOT NULL,
    completed INTEGER NOT NULL,
    neutral_site INTEGER NOT NULL,
    conference_game INTEGER NOT NULL,
    venue_id INTEGER,
    venue TEXT,
    television TEXT,
    home_team_id INTEGER NOT NULL,
    home_team TEXT NOT NULL,
    home_conference TEXT,
    home_points INTEGER,
    home_pregame_elo INTEGER,
    away_team_id INTEGER NOT NULL,
    away_team TEXT NOT NULL,
    away_conference TEXT,
    away_points INTEGER,
    away_pregame_elo INTEGER,
    excitement_index REAL,
    notes TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_season_start ON games(season, start_date);
CREATE INDEX IF NOT EXISTS idx_games_home_team ON games(home_team_id, season);
CREATE INDEX IF NOT EXISTS idx_games_away_team ON games(away_team_id, season);

CREATE TABLE IF NOT EXISTS coach_seasons (
    season INTEGER NOT NULL,
    coach_id INTEGER NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    team TEXT NOT NULL,
    conference TEXT,
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    ties INTEGER NOT NULL DEFAULT 0,
    win_percentage REAL,
    attribution_complete INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, coach_id, team_id)
);
CREATE INDEX IF NOT EXISTS idx_coach_seasons_team
    ON coach_seasons(team_id, season DESC);
CREATE INDEX IF NOT EXISTS idx_coach_seasons_coach
    ON coach_seasons(coach_id, season DESC);

CREATE TABLE IF NOT EXISTS history_sync_state (
    season INTEGER NOT NULL,
    dataset TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (season, dataset)
);

CREATE TABLE IF NOT EXISTS game_team_box_stats (
    game_id INTEGER NOT NULL,
    team_id INTEGER,
    team TEXT NOT NULL,
    conference TEXT,
    home_away TEXT,
    points INTEGER,
    category TEXT NOT NULL,
    stat_value TEXT,
    numeric_value REAL,
    PRIMARY KEY (game_id, team, category)
);

CREATE TABLE IF NOT EXISTS game_player_box_stats (
    game_id INTEGER NOT NULL,
    team_id INTEGER,
    team TEXT NOT NULL,
    conference TEXT,
    home_away TEXT,
    points INTEGER,
    category TEXT NOT NULL,
    stat_type TEXT NOT NULL,
    player_id TEXT NOT NULL,
    player TEXT NOT NULL,
    stat_value TEXT,
    numeric_value REAL,
    PRIMARY KEY (game_id, team, category, stat_type, player_id)
);
CREATE INDEX IF NOT EXISTS idx_game_player_box_player
    ON game_player_box_stats(player_id, game_id);

CREATE TABLE IF NOT EXISTS game_player_ppa (
    game_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    season_type TEXT NOT NULL,
    player_id TEXT NOT NULL,
    player TEXT NOT NULL,
    position TEXT,
    team TEXT NOT NULL,
    opponent TEXT NOT NULL,
    ppa_all REAL,
    ppa_pass REAL,
    ppa_rush REAL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_game_player_ppa_player
    ON game_player_ppa(player_id, season, week);

CREATE TABLE IF NOT EXISTS team_records (
    season INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    team TEXT NOT NULL,
    conference TEXT,
    division TEXT,
    games INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    ties INTEGER NOT NULL,
    expected_wins REAL,
    conference_games INTEGER NOT NULL DEFAULT 0,
    conference_wins INTEGER NOT NULL DEFAULT 0,
    conference_losses INTEGER NOT NULL DEFAULT 0,
    conference_ties INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, team_id)
);

CREATE TABLE IF NOT EXISTS player_season_stats (
    season INTEGER NOT NULL, player_id TEXT NOT NULL, player TEXT NOT NULL,
    team TEXT NOT NULL, conference TEXT, position TEXT,
    category TEXT NOT NULL, stat_type TEXT NOT NULL,
    stat_value TEXT, numeric_value REAL,
    PRIMARY KEY(season,player_id,category,stat_type)
);
CREATE INDEX IF NOT EXISTS idx_player_stats_conference
    ON player_season_stats(season,conference,category,stat_type,numeric_value DESC);
CREATE INDEX IF NOT EXISTS idx_player_stats_team
    ON player_season_stats(season,team,category,stat_type,numeric_value DESC);

CREATE TABLE IF NOT EXISTS player_transfers (
    season INTEGER NOT NULL, normalized_name TEXT NOT NULL,
    first_name TEXT NOT NULL, last_name TEXT NOT NULL, position TEXT,
    origin TEXT NOT NULL, destination TEXT, transfer_date TEXT,
    rating REAL, stars INTEGER, eligibility TEXT,
    PRIMARY KEY(season,normalized_name,origin)
);
CREATE INDEX IF NOT EXISTS idx_transfers_origin ON player_transfers(season,origin);
CREATE INDEX IF NOT EXISTS idx_transfers_destination ON player_transfers(season,destination);

CREATE TABLE IF NOT EXISTS draft_picks (
    draft_year INTEGER NOT NULL, overall_pick INTEGER NOT NULL,
    round INTEGER NOT NULL, round_pick INTEGER NOT NULL,
    college_athlete_id TEXT, college_team_id INTEGER, college_team TEXT NOT NULL,
    college_conference TEXT, nfl_team_id INTEGER, nfl_team TEXT NOT NULL,
    player_name TEXT NOT NULL, normalized_name TEXT NOT NULL, position TEXT,
    pre_draft_ranking INTEGER, pre_draft_position_ranking INTEGER,
    pre_draft_grade INTEGER,
    PRIMARY KEY(draft_year,overall_pick)
);
CREATE INDEX IF NOT EXISTS idx_draft_school ON draft_picks(draft_year,college_team);
CREATE INDEX IF NOT EXISTS idx_draft_player ON draft_picks(normalized_name,draft_year);

CREATE TABLE IF NOT EXISTS returning_production (
    season INTEGER NOT NULL, team TEXT NOT NULL, conference TEXT,
    total_ppa REAL, passing_ppa REAL, receiving_ppa REAL, rushing_ppa REAL,
    percent_ppa REAL, percent_passing_ppa REAL,
    percent_receiving_ppa REAL, percent_rushing_ppa REAL,
    usage REAL, passing_usage REAL, receiving_usage REAL, rushing_usage REAL,
    PRIMARY KEY(season,team)
);

CREATE TABLE IF NOT EXISTS rankings (
    season INTEGER NOT NULL,
    season_type TEXT NOT NULL,
    week INTEGER NOT NULL,
    poll TEXT NOT NULL,
    is_final INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    team_id INTEGER,
    school TEXT NOT NULL,
    conference TEXT,
    first_place_votes INTEGER,
    points INTEGER,
    PRIMARY KEY (season, season_type, week, poll, school)
);
CREATE INDEX IF NOT EXISTS idx_rankings_team ON rankings(season, school, week);

CREATE TABLE IF NOT EXISTS team_stats (
    season INTEGER NOT NULL,
    team TEXT NOT NULL,
    conference TEXT,
    stat_name TEXT NOT NULL,
    stat_value TEXT,
    PRIMARY KEY (season, team, stat_name)
);

CREATE TABLE IF NOT EXISTS team_advanced_stats (
    season INTEGER NOT NULL,
    team TEXT NOT NULL,
    conference TEXT,
    offense_success_rate REAL,
    defense_success_rate REAL,
    offense_explosiveness REAL,
    defense_explosiveness REAL,
    offense_ppa REAL,
    defense_ppa REAL,
    offense_points_per_opportunity REAL,
    defense_points_per_opportunity REAL,
    offense_havoc REAL,
    defense_havoc REAL,
    offense_json TEXT NOT NULL,
    defense_json TEXT NOT NULL,
    PRIMARY KEY (season, team)
);

CREATE TABLE IF NOT EXISTS core_ratings (
    season INTEGER NOT NULL,
    through_season_type TEXT NOT NULL,
    through_week INTEGER NOT NULL,
    team TEXT NOT NULL,
    conference TEXT,
    overall REAL NOT NULL,
    offense REAL NOT NULL,
    defense REAL NOT NULL,
    offense_plays INTEGER NOT NULL,
    defense_plays INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    PRIMARY KEY (season, through_season_type, through_week, team)
);
CREATE INDEX IF NOT EXISTS idx_core_latest ON core_ratings(season, through_week, overall DESC);

CREATE TABLE IF NOT EXISTS pff_players (
    season INTEGER NOT NULL,
    pff_player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    position TEXT,
    pff_team_name TEXT NOT NULL,
    cfbd_team_id INTEGER,
    cfbd_team TEXT,
    cfbd_player_id TEXT,
    candidate_cfbd_player_id TEXT,
    match_status TEXT NOT NULL,
    match_confidence REAL NOT NULL,
    interest_score REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, pff_player_id)
);
CREATE INDEX IF NOT EXISTS idx_pff_players_team ON pff_players(season, cfbd_team_id);
CREATE INDEX IF NOT EXISTS idx_pff_players_interest ON pff_players(season, interest_score DESC);
CREATE INDEX IF NOT EXISTS idx_pff_players_cfbd_player
    ON pff_players(cfbd_player_id, season DESC);
CREATE INDEX IF NOT EXISTS idx_pff_players_identity
    ON pff_players(normalized_name, cfbd_team, season DESC);

CREATE TABLE IF NOT EXISTS pff_player_metrics (
    season INTEGER NOT NULL,
    pff_player_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    source_file TEXT NOT NULL,
    game_count INTEGER,
    primary_grade REAL,
    usage_count REAL,
    metrics_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (season, pff_player_id, dataset),
    FOREIGN KEY (season, pff_player_id)
        REFERENCES pff_players(season, pff_player_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pff_position_groups (
    season INTEGER NOT NULL,
    cfbd_team_id INTEGER,
    pff_team_name TEXT NOT NULL,
    position_group TEXT NOT NULL,
    dataset TEXT NOT NULL,
    weighted_grade REAL,
    player_count INTEGER NOT NULL,
    usage_count REAL NOT NULL,
    PRIMARY KEY (season, pff_team_name, position_group, dataset)
);
CREATE INDEX IF NOT EXISTS idx_pff_groups_grade
    ON pff_position_groups(season, dataset, weighted_grade DESC);

CREATE TABLE IF NOT EXISTS pff_supplemental_metrics (
    season INTEGER NOT NULL,
    pff_player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    position TEXT,
    event_team TEXT,
    dataset TEXT NOT NULL,
    context TEXT NOT NULL,
    source_file TEXT NOT NULL,
    game_count INTEGER,
    primary_grade REAL,
    usage_count REAL,
    metrics_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (season, pff_player_id, dataset, context)
);
CREATE INDEX IF NOT EXISTS idx_pff_supplemental_player
    ON pff_supplemental_metrics(pff_player_id, season DESC);

CREATE TABLE IF NOT EXISTS pff_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season INTEGER NOT NULL,
    roster_season INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    source_directory TEXT NOT NULL,
    files INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    linked_players INTEGER NOT NULL,
    candidate_transfers INTEGER NOT NULL,
    unresolved_players INTEGER NOT NULL,
    unresolved_teams_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recruits (
 season INTEGER NOT NULL, recruit_id TEXT NOT NULL, athlete_id TEXT,
 name TEXT NOT NULL, normalized_name TEXT NOT NULL, position TEXT,
 committed_to TEXT, recruit_type TEXT, stars INTEGER, rating REAL,
 ranking INTEGER, height INTEGER, weight INTEGER, home_city TEXT, home_state TEXT,
 updated_at TEXT NOT NULL, PRIMARY KEY(season,recruit_id)
);
CREATE INDEX IF NOT EXISTS idx_recruits_team ON recruits(season,committed_to);
CREATE INDEX IF NOT EXISTS idx_recruits_athlete ON recruits(athlete_id);
CREATE TABLE IF NOT EXISTS venues (
 venue_id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT, state TEXT,
 latitude REAL, longitude REAL, timezone TEXT, elevation REAL,
 dome INTEGER NOT NULL DEFAULT 0, grass INTEGER, capacity INTEGER,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
    season INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    succeeded INTEGER NOT NULL,
    details_json TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metric(side: dict[str, Any], name: str) -> float | None:
    value = side.get(name)
    return float(value) if value is not None else None


def _numeric(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


#: Position -> the existing stat category whose qualifying threshold (see
#: statlines.LEADER_QUALIFIERS) stands in for PPA's missing play-count floor.
_PPA_QUALIFYING_CATEGORY = {
    "QB": "passing", "RB": "rushing", "FB": "rushing", "WR": "receiving", "TE": "receiving",
}


#: CFBD's returning-production percentage is returning PPA over last season's
#: team PPA. When an offense's season PPA nets near zero -- true of the three or
#: four worst teams in the country every year -- that denominator is unstable and
#: the ratio stops being a percentage of anything: Massachusetts came through for
#: 2026 at -56.7%, with the rushing split reading 221%. Returning *usage* has a
#: non-negative denominator by construction and sits within a few points of the
#: PPA figure for every healthy offense, so it is the honest stand-in on the rare
#: row where the ratio breaks.
_RETURNING_PPA_MAX = 1.25


def _returning_production_signal(row: dict[str, Any],
                                have_row: bool) -> tuple[float | None, str]:
    """Value and source label for the "Returning production" context card."""
    percent_ppa = row.get("percent_ppa")
    usage = row.get("usage")
    if percent_ppa is not None and 0.0 <= percent_ppa <= _RETURNING_PPA_MAX:
        return percent_ppa, "CFBD returning PPA"
    if usage is not None and 0.0 < usage <= _RETURNING_PPA_MAX:
        return usage, "CFBD returning usage (PPA base near zero)"
    if have_row:
        return None, "CFBD returning production not interpretable this year"
    return None, "Awaiting CFBD returning production"


def _hex_color(value: str | None) -> str | None:
    """Normalize a CFBD color to a single-hash CSS hex value.

    CFBD returns colors already prefixed with "#". Templates that prepended
    another one produced "##0c2340", which browsers discard, so team accent
    colors silently never applied.
    """
    if not value:
        return None
    candidate = str(value).strip().lstrip("#")
    return f"#{candidate}" if re.fullmatch(r"[0-9A-Fa-f]{3,8}", candidate) else None


#: Re-exported: the slug is a presentation rule and lives with the palette.
conference_slug = _conference_slug


def _logo_pair(logos: list[str]) -> tuple[str | None, str | None]:
    """The light mark and its dark-background counterpart, if one is published.

    CFBD ships both variants for every team, interleaved by size:
    ``logos/500/2.png`` then ``logos-dark/500/2.png``. Taking ``logos[0]`` kept
    only the light mark, so a school with a dark wordmark disappeared against a
    dark page. The dark variant is the school's own alternate, not a filter, so
    it is preferred over tinting the light one.
    """
    light = next((url for url in logos if "-dark" not in url), None)
    dark = next((url for url in logos if "-dark" in url), None)
    # A team with only one published mark uses it on both themes rather than
    # rendering nothing on one of them.
    return light or dark, dark or light


#: Databases whose schema has already been created in this process.
#:
#: Every repository method begins with `self.initialize()`, which opened a
#: connection and replayed its whole `CREATE TABLE IF NOT EXISTS` script. The
#: social repositories compound it: ContentRepository.initialize() builds a
#: fresh CFBRepository and runs the full CFB schema, and StoryRepository builds
#: a ContentRepository on top of that, so a single `list_stories()` replayed
#: four schemas. Rendering one matchup page opened 137 connections and issued
#: 4,431 statements, roughly 3,500 of them re-creating tables that already
#: existed.
#:
#: The work is idempotent, so it only has to happen once per database per
#: process. Keyed by resolved path rather than by instance because those inner
#: calls construct new objects each time. A path whose file has since been
#: removed is initialized again, so a caller that deletes a database and reuses
#: the location still gets a schema.
_INITIALIZED_SCHEMAS: set[tuple[str, str]] = set()


def _schema_is_current(kind: str, path: Path) -> bool:
    key = (kind, str(path.resolve()))
    if key not in _INITIALIZED_SCHEMAS:
        return False
    if not path.exists():
        _INITIALIZED_SCHEMAS.discard(key)
        return False
    return True


def _mark_schema_current(kind: str, path: Path) -> None:
    _INITIALIZED_SCHEMAS.add((kind, str(path.resolve())))


def forget_initialized_schemas() -> None:
    """Drop the memo. For tests that rebuild a database in place."""
    _INITIALIZED_SCHEMAS.clear()


#: Prefix for the per-request connections `_reader` parks on Flask's `g`. Keyed
#: by repository identity too, since an app may hold more than one database.
_CONNECTION_KEY = "_cfb_reader_"

#: Same idea for the team table, which a single render reads a dozen times.
_TEAMS_KEY = "_cfb_teams_"

#: And for the season-wide indexes a page rebuilds once per team it shows.
_SEASON_INDEX_KEY = "_cfb_season_index_"


def _request_scope():
    """Flask's per-request scratch space, or None when there is no request.

    Imported here rather than at module scope so the persistence layer still
    loads for a CLI command or a test that never builds an app.
    """
    try:
        from flask import g, has_app_context
    except ImportError:
        return None
    return g if has_app_context() else None


def _request_cached(repository, name: str, build):
    """Hold a season-wide index for the request, the way `_teams_by_id` does.

    Several of these cover a whole season but are read once per team, so a page
    showing two teams built the same dictionary from the same thousands of rows
    two to eight times. Scoped to the request and not to the repository: a
    refresh runs in another process, and a value cached past the end of the
    request would put stale rows into a page the rendered-page cache believed
    it had just rebuilt.
    """
    scope = _request_scope()
    if scope is None:
        return build()
    key = f"{_SEASON_INDEX_KEY}{id(repository)}:{name}"
    cached = getattr(scope, key, None)
    if cached is None:
        cached = build()
        setattr(scope, key, cached)
    return cached


def schema_once(kind: str):
    """Memoize a module's `initialize(repository)` the way the repository's is.

    `CFBRepository.initialize` has been guarded since the matchup-page work
    above, but the per-module table creators were not, and they call each other:
    `team_game_advanced` initializes `expected_points_v2`, which initializes
    `play_by_play`. One box-score render therefore opened the database fifteen
    extra times to replay `CREATE TABLE IF NOT EXISTS` against tables that were
    already there -- half of that page's connections, and a third of its time.

    Sharing `_INITIALIZED_SCHEMAS` rather than keeping a private memo means
    `forget_initialized_schemas()` still resets every schema at once, so a test
    that rebuilds a database in place does not have to know which modules cache.
    """

    def decorate(function):
        @wraps(function)
        def wrapper(repository) -> None:
            path = Path(repository.path)
            if _schema_is_current(kind, path):
                return
            function(repository)
            _mark_schema_current(kind, path)

        return wrapper

    return decorate


def _to_zone(value: str, zone: ZoneInfo) -> datetime | None:
    """A stored UTC timestamp read in the reader's timezone."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone)


class CFBRepository:
    def __init__(self, database_path: str | Path) -> None:
        self._brands: dict[int, dict[str, Any]] | None = None
        self._production_distributions: dict[int, dict[str, list[float]]] = {}
        self._production_strengths: dict[int, dict[str, float]] = {}
        self._brands_by_school: dict[str, dict[str, Any]] | None = None
        self.path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            busy_timeout_ms = max(1_000, int(os.getenv("CFB_SQLITE_BUSY_TIMEOUT_MS", "60000")))
        except ValueError:
            busy_timeout_ms = 60_000
        connection = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        return connection

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """A connection for reading, shared by everything in one request.

        A matchup page opened ninety-three connections to render once, each one
        a fresh open of a 900 MB file plus two pragmas, and reusing a single one
        is worth about a fifth of the page. Reads only: `transaction` keeps its
        own connection, so the writer never waits on a reader and a failed write
        cannot roll back somebody else's work.

        Only a request is scoped, because only a request has an end at which to
        close the thing. A CLI command or a test gets what it always got -- a
        connection of its own, closed on the way out -- so nothing is left
        holding a database file open after the work that needed it is done.
        """
        scope = _request_scope()
        if scope is None:
            with closing(self._connect()) as connection:
                yield connection
            return
        key = f"{_CONNECTION_KEY}{id(self)}"
        connection = getattr(scope, key, None)
        if connection is None:
            connection = self._connect()
            setattr(scope, key, connection)
        yield connection

    @staticmethod
    def close_request_connections(_exception: BaseException | None = None) -> None:
        """Registered as a Flask teardown; see `_reader`."""
        scope = _request_scope()
        if scope is None:
            return
        for key in [name for name in vars(scope) if name.startswith(_CONNECTION_KEY)]:
            try:
                getattr(scope, key).close()
            except sqlite3.Error:
                pass
            delattr(scope, key)

    def initialize(self) -> None:
        if _schema_is_current("cfb", self.path):
            return
        with closing(self._connect()) as connection:
            # Changing journal mode requires an exclusive lock. Reissuing this
            # on every request can itself cause `database is locked` on a live
            # service, so only perform the write when the database is not yet WAL.
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            if mode != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            player_primary_key = [
                row["name"] for row in sorted(
                    connection.execute("PRAGMA table_info(players)").fetchall(),
                    key=lambda row: row["pk"] or 99,
                ) if row["pk"]
            ]
            if player_primary_key == ["season", "player_id"]:
                connection.executescript(
                    """
                    ALTER TABLE players RENAME TO players_single_team_legacy;
                    CREATE TABLE players (
                     season INTEGER NOT NULL, player_id TEXT NOT NULL, first_name TEXT NOT NULL,
                     last_name TEXT NOT NULL, normalized_name TEXT NOT NULL, team TEXT NOT NULL,
                     position TEXT, jersey INTEGER, height REAL, weight INTEGER, class_year INTEGER,
                     PRIMARY KEY(season,player_id,team));
                    INSERT OR IGNORE INTO players SELECT * FROM players_single_team_legacy;
                    DROP TABLE players_single_team_legacy;
                    CREATE INDEX IF NOT EXISTS idx_players_name ON players(season,normalized_name);
                    CREATE INDEX IF NOT EXISTS idx_players_team ON players(season,team);
                    """
                )
            record_columns={row["name"] for row in connection.execute("PRAGMA table_info(team_records)")}
            for name in ("conference_games","conference_wins","conference_losses","conference_ties"):
                if name not in record_columns:
                    connection.execute(f"ALTER TABLE team_records ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            primary_key = [
                row["name"]
                for row in sorted(
                    connection.execute("PRAGMA table_info(rankings)").fetchall(),
                    key=lambda row: row["pk"] or 99,
                )
                if row["pk"]
            ]
            if primary_key and primary_key[-1] == "rank":
                # CFBD polls may contain tied teams with the same numeric rank.
                connection.executescript(
                    """
                    DROP INDEX IF EXISTS idx_rankings_team;
                    ALTER TABLE rankings RENAME TO rankings_rank_key_legacy;
                    CREATE TABLE rankings (
                        season INTEGER NOT NULL, season_type TEXT NOT NULL,
                        week INTEGER NOT NULL, poll TEXT NOT NULL,
                        is_final INTEGER NOT NULL, rank INTEGER NOT NULL,
                        team_id INTEGER, school TEXT NOT NULL, conference TEXT,
                        first_place_votes INTEGER, points INTEGER,
                        PRIMARY KEY (season, season_type, week, poll, school)
                    );
                    INSERT OR REPLACE INTO rankings
                        SELECT * FROM rankings_rank_key_legacy;
                    DROP TABLE rankings_rank_key_legacy;
                    CREATE INDEX idx_rankings_team ON rankings(season, school, week);
                    """
                )
            # `game_id` is already the leftmost column of each table's
            # primary-key autoindex, so these two indexed nothing that was not
            # indexed twice: 42 MB across the two box-score tables, and a second
            # b-tree to write on every one of the millions of refresh inserts.
            # Query plans fall through to the autoindex at the same speed.
            connection.execute("DROP INDEX IF EXISTS idx_game_team_box_game")
            connection.execute("DROP INDEX IF EXISTS idx_game_player_box_game")
            self._ensure_statistics(connection)
        _mark_schema_current("cfb", self.path)

    #: How many rows ANALYZE samples per index. Without a limit it scans every
    #: index in the file; with one the whole database is analyzed in about a
    #: quarter of a second at 1.8 GB, which is what makes it affordable to
    #: check on startup and to redo after every refresh.
    ANALYSIS_LIMIT = 400

    @staticmethod
    def _tables_missing_statistics(connection: sqlite3.Connection) -> list[str]:
        """Indexed tables that hold rows but have no entry in `sqlite_stat1`.

        ANALYZE writes nothing for an empty table, so a table is only counted
        when it actually has a row -- otherwise every empty table would look
        unanalyzed forever and re-trigger the work on every startup.
        """
        analyzed = {row[0] for row in
                    connection.execute("SELECT DISTINCT tbl FROM sqlite_stat1")}
        missing = []
        for (name,) in connection.execute(
                "SELECT DISTINCT tbl_name FROM sqlite_master WHERE type='index'"):
            if name in analyzed or name.startswith("sqlite_"):
                continue
            if connection.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone():
                missing.append(name)
        return missing

    @classmethod
    def _ensure_statistics(cls, connection: sqlite3.Connection) -> None:
        """Give the query planner statistics for every table that has data.

        Without them SQLite guesses join order from defaults, and it guesses
        badly here: the continuity lookup drove from `pff_player_metrics`,
        scanning every row for the season, instead of starting from the handful
        of `pff_players` rows for one team that `idx_pff_players_team` already
        indexes. Five teams took 462ms; with statistics the same work takes
        16ms and the plan uses the index.

        This used to run once and then skip forever if `sqlite_stat1` held any
        row at all, which quietly meant "once, on whatever tables existed the
        first time". The play tables were empty then and were still unanalyzed
        650,000 rows later, so the box score's pace query drove from all of
        `cfb_play_metrics` matching `metric_version` and filtered 650k rows
        down to 166: 8.3 seconds for one game, against 4.6ms once the planner
        could see the shape of the table. So the condition is now the real one
        -- a table that has rows and no statistics -- rather than a proxy for
        it that a growing database outlives.
        """
        try:
            has_stats = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='sqlite_stat1'").fetchone()[0]
            if has_stats and not cls._tables_missing_statistics(connection):
                return
            connection.execute(f"PRAGMA analysis_limit={cls.ANALYSIS_LIMIT}")
            connection.execute("ANALYZE")
        except sqlite3.Error:
            # Statistics are an optimization; a database that refuses them
            # still answers every query.
            pass

    def _statistics_stamp(self) -> str:
        """What the rendered-page cache keys on: the database and its WAL."""
        parts = []
        for candidate in (self.path, self.path.with_name(self.path.name + "-wal")):
            try:
                parts.append(str(candidate.stat().st_mtime_ns))
            except OSError:
                parts.append("0")
        return "-".join(parts)

    def optimize(self) -> None:
        """Refresh planner statistics, but only once per change to the data.

        This called `PRAGMA optimize`, which only considers tables the *current
        connection* has already queried -- and this opens a connection purely
        to run it, so it had queried nothing and did nothing, every night, for
        as long as it has been scheduled. Nor is the pragma usable here: on
        SQLite 3.45 the bit that would make it consider every table is not
        supported, and merely selecting a row from each does not register,
        because a query answered without opening an index does not count as
        having used one.

        So it runs ANALYZE itself, with `analysis_limit` sampling rows per
        index rather than scanning them all -- about a quarter of a second on
        the 1.8 GB file. That writes, and the rendered-page cache is keyed on
        the database's modification time, so doing it unconditionally would
        throw away every cached page after a refresh that never touched this
        database at all -- a news pass, most often. The marker beside the file
        records what the database looked like when statistics were last
        written; if nothing has been written since, there is nothing to do and
        nothing to invalidate.
        """
        self.initialize()
        marker = self.path.with_name(self.path.name + ".analyzed")
        try:
            if marker.read_text(encoding="utf-8").strip() == self._statistics_stamp():
                return
        except OSError:
            pass
        try:
            with closing(self._connect()) as connection:
                connection.execute(f"PRAGMA analysis_limit={self.ANALYSIS_LIMIT}")
                connection.execute("ANALYZE")
                connection.commit()
        except sqlite3.Error:
            return
        try:
            # Written after the connection closes: closing checkpoints the WAL,
            # which moves the very timestamps being recorded.
            marker.write_text(self._statistics_stamp(), encoding="utf-8")
        except OSError:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect()
        try:
            # These are write transactions. Reserving the writer slot before
            # any reads avoids SQLITE_BUSY_SNAPSHOT when a deferred transaction
            # tries to upgrade after another process commits under WAL.
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace_teams(self, teams: Iterable[Team]) -> int:
        items = tuple(teams)
        with self.transaction() as connection:
            for team in items:
                connection.execute(
                    """
                    INSERT INTO teams (
                        team_id, school, mascot, abbreviation, conference, division,
                        classification, color, alternate_color, logos_json, venue_id,
                        venue_name, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(team_id) DO UPDATE SET
                        school=excluded.school, mascot=excluded.mascot,
                        abbreviation=excluded.abbreviation, conference=excluded.conference,
                        division=excluded.division, classification=excluded.classification,
                        color=excluded.color, alternate_color=excluded.alternate_color,
                        logos_json=excluded.logos_json, venue_id=excluded.venue_id,
                        venue_name=excluded.venue_name, updated_at=excluded.updated_at
                    """,
                    (
                        team.team_id, team.school, team.mascot, team.abbreviation,
                        team.conference, team.division, team.classification, team.color,
                        team.alternate_color, json.dumps(team.logos), team.venue_id,
                        team.venue_name, _now_iso(),
                    ),
                )
                connection.execute("DELETE FROM team_aliases WHERE team_id = ?", (team.team_id,))
                for alias in team.aliases:
                    normalized = normalize_alias(alias)
                    if normalized:
                        connection.execute(
                            "INSERT OR IGNORE INTO team_aliases (team_id, alias, normalized_alias) VALUES (?, ?, ?)",
                            (team.team_id, alias, normalized),
                        )
        return len(items)

    def replace_games(self, season: int, games: Iterable[Game]) -> int:
        items = tuple(games)
        with self.transaction() as connection:
            # Media is a separate CFBD endpoint and is synchronized after games.
            # Preserve it when a game/history refresh replaces canonical rows so
            # an isolated historical sync cannot temporarily blank current TV.
            media_by_game = {row["game_id"]: row["television"] for row in
                             connection.execute(
                                 "SELECT game_id,television FROM games WHERE season=?",
                                 (season,))}
            connection.executemany(
                """
                INSERT INTO games VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(game_id) DO UPDATE SET
                    season=excluded.season,week=excluded.week,
                    season_type=excluded.season_type,start_date=excluded.start_date,
                    start_time_tbd=excluded.start_time_tbd,completed=excluded.completed,
                    neutral_site=excluded.neutral_site,
                    conference_game=excluded.conference_game,venue_id=excluded.venue_id,
                    venue=excluded.venue,
                    television=COALESCE(excluded.television,games.television),
                    home_team_id=excluded.home_team_id,home_team=excluded.home_team,
                    home_conference=excluded.home_conference,
                    home_points=excluded.home_points,
                    home_pregame_elo=excluded.home_pregame_elo,
                    away_team_id=excluded.away_team_id,away_team=excluded.away_team,
                    away_conference=excluded.away_conference,
                    away_points=excluded.away_points,
                    away_pregame_elo=excluded.away_pregame_elo,
                    excitement_index=excluded.excitement_index,notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        game.game_id, game.season, game.week, game.season_type,
                        game.start_date.isoformat(), int(game.start_time_tbd),
                        int(game.completed), int(game.neutral_site), int(game.conference_game),
                        game.venue_id, game.venue, media_by_game.get(game.game_id),
                        game.home_team_id, game.home_team,
                        game.home_conference, game.home_points, game.home_pregame_elo,
                        game.away_team_id, game.away_team, game.away_conference,
                        game.away_points, game.away_pregame_elo, game.excitement_index,
                        game.notes, _now_iso(),
                    )
                    for game in items
                ],
            )
            # Remove games genuinely withdrawn upstream only after their
            # replacements are present. Updating in place preserves FPI,
            # weather, box scores and every other game-keyed child row.
            if items:
                identifiers = [game.game_id for game in items]
                placeholders = ",".join("?" for _ in identifiers)
                connection.execute(
                    f"DELETE FROM games WHERE season=? AND game_id NOT IN ({placeholders})",
                    (season, *identifiers))
        return len(items)

    def replace_players(self, season: int, players: Iterable[Any]) -> int:
        items=tuple(players)
        with self.transaction() as connection:
            connection.execute("DELETE FROM players WHERE season=?",(season,))
            connection.executemany("INSERT INTO players VALUES(?,?,?,?,?,?,?,?,?,?,?)",[
                (season,p.player_id,p.first_name,p.last_name,normalize_alias(p.name),p.team,p.position,
                 p.jersey,p.height,p.weight,p.class_year) for p in items])
        return len(items)

    def update_game_media(self, media: Iterable[dict[str, Any]]) -> int:
        outlets: dict[int, list[str]] = {}
        for item in media:
            game_id = int(item["id"])
            outlet = str(item.get("outlet") or "").strip()
            if outlet and outlet not in outlets.setdefault(game_id, []):
                outlets[game_id].append(outlet)
        with self.transaction() as connection:
            for game_id, names in outlets.items():
                connection.execute(
                    "UPDATE games SET television = ? WHERE game_id = ?",
                    (", ".join(names), game_id),
                )
        return len(outlets)

    def replace_coach_seasons(self, season: int,
                              coaches: Iterable[dict[str, Any]]) -> int:
        """Store the head coach attributed to each team-season by CFBD.

        The historical ``/coaches`` response is coach-shaped with nested
        seasons. Keeping the season grain locally makes coach/opponent records
        derivable from the same canonical game log without copying game data.
        """
        rows = []
        now = _now_iso()
        for coach in coaches:
            coach_id = coach.get("id")
            if coach_id is None:
                continue
            for item in coach.get("seasons") or []:
                if int(item.get("year") or 0) != season or item.get("teamId") is None:
                    continue
                rows.append((
                    season, int(coach_id), str(coach.get("firstName") or ""),
                    str(coach.get("lastName") or ""), int(item["teamId"]),
                    str(item.get("school") or ""), item.get("conference"),
                    int(item.get("games") or 0), int(item.get("wins") or 0),
                    int(item.get("losses") or 0), int(item.get("ties") or 0),
                    _numeric(item.get("winPercentage")),
                    (int(bool(item["attributionComplete"]))
                     if item.get("attributionComplete") is not None else None),
                    now,
                ))
        with self.transaction() as connection:
            connection.execute("DELETE FROM coach_seasons WHERE season=?", (season,))
            connection.executemany(
                """INSERT INTO coach_seasons VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        return len(rows)

    HISTORY_TABLES = {
        "games": "games", "records": "team_records", "team_stats": "team_stats",
        "advanced_stats": "team_advanced_stats", "coaches": "coach_seasons",
        "roster": "players", "player_stats": "player_season_stats",
        "team_box_scores": "game_team_box_stats", "player_box_scores": "game_player_box_stats",
    }

    def history_dataset_cached(self, season: int, dataset: str) -> bool:
        """Whether a completed-season dataset is already durable in SQLite."""
        self.initialize()
        table = self.HISTORY_TABLES.get(dataset)
        if table is None:
            raise ValueError(f"Unknown historical dataset: {dataset}")
        with self._reader() as connection:
            marker = connection.execute(
                "SELECT 1 FROM history_sync_state WHERE season=? AND dataset=?",
                (season, dataset)).fetchone()
            if marker:
                return True
            # These arrive a week at a time. Any-row fallback would turn an
            # interrupted first pass into a false whole-season cache hit.
            if dataset in {"team_box_scores", "player_box_scores"}:
                return False
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE season=?", (season,)).fetchone()[0]
        return count > 0

    def mark_history_dataset(self, season: int, dataset: str, row_count: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO history_sync_state VALUES(?,?,?,?)
                   ON CONFLICT(season,dataset) DO UPDATE SET
                   row_count=excluded.row_count,synced_at=excluded.synced_at""",
                (season, dataset, row_count, _now_iso()))

    def history_conference_stats_cached(self, season: int, conference: str) -> bool:
        self.initialize()
        with self._reader() as connection:
            return connection.execute(
                """SELECT 1 FROM player_season_stats
                   WHERE season=? AND conference=? LIMIT 1""",
                (season, conference)).fetchone() is not None

    def clear_box_scores(self, season: int) -> None:
        with self.transaction() as connection:
            game_ids = "SELECT game_id FROM games WHERE season=?"
            connection.execute(
                f"DELETE FROM game_team_box_stats WHERE game_id IN ({game_ids})", (season,))
            connection.execute(
                f"DELETE FROM game_player_box_stats WHERE game_id IN ({game_ids})", (season,))
            connection.execute(
                "DELETE FROM history_sync_state WHERE season=? AND dataset IN (?,?)",
                (season, "team_box_scores", "player_box_scores"))

    def completed_weeks(self, season: int) -> list[int]:
        self.initialize()
        with self._reader() as connection:
            return [row[0] for row in connection.execute(
                """SELECT DISTINCT week FROM games WHERE season=? AND completed=1
                   ORDER BY week""", (season,))]

    def completed_week_season_types(self, season: int) -> list[tuple[str, int]]:
        """(season_type, week) pairs with at least one completed game --
        `completed_weeks` collapses season_type, which loses postseason
        weeks that reuse regular-season week numbers (both commonly use
        week 1)."""
        self.initialize()
        with self._reader() as connection:
            return [(row[0], row[1]) for row in connection.execute(
                """SELECT DISTINCT season_type,week FROM games
                   WHERE season=? AND completed=1
                   ORDER BY CASE season_type WHEN 'regular' THEN 0 ELSE 1 END, week""",
                (season,))]

    def box_score_counts(self, season: int) -> dict[str, int]:
        self.initialize()
        with self._reader() as connection:
            return {
                "team_box_scores": connection.execute(
                    """SELECT COUNT(*) FROM game_team_box_stats b JOIN games g
                       ON g.game_id=b.game_id WHERE g.season=?""", (season,)).fetchone()[0],
                "player_box_scores": connection.execute(
                    """SELECT COUNT(*) FROM game_player_box_stats b JOIN games g
                       ON g.game_id=b.game_id WHERE g.season=?""", (season,)).fetchone()[0],
            }

    def store_game_team_box_scores(self, payload: Iterable[dict[str, Any]]) -> int:
        rows = []
        for game in payload:
            game_id = int(game["id"])
            for team in game.get("teams") or []:
                for stat in team.get("stats") or []:
                    rows.append((game_id, team.get("teamId"), str(team.get("team") or ""),
                                 team.get("conference"), team.get("homeAway"),
                                 team.get("points"), str(stat.get("category") or ""),
                                 str(stat.get("stat") or ""), _numeric(stat.get("stat"))))
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO game_team_box_stats VALUES(?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def store_game_player_box_scores(self, payload: Iterable[dict[str, Any]]) -> int:
        games = tuple(payload)
        game_ids = [int(item["id"]) for item in games]
        with self._reader() as connection:
            placeholders = ",".join("?" for _ in game_ids) or "NULL"
            game_teams = {row["game_id"]: dict(row) for row in connection.execute(
                f"SELECT game_id,home_team_id,home_team,away_team_id,away_team FROM games "
                f"WHERE game_id IN ({placeholders})", game_ids)}
        rows = []
        for game in games:
            game_id = int(game["id"]); canonical = game_teams.get(game_id) or {}
            for team in game.get("teams") or []:
                name = str(team.get("team") or "")
                team_id = (canonical.get("home_team_id") if name == canonical.get("home_team")
                           else canonical.get("away_team_id") if name == canonical.get("away_team")
                           else None)
                for category in team.get("categories") or []:
                    category_name = str(category.get("name") or "")
                    for stat_type in category.get("types") or []:
                        type_name = str(stat_type.get("name") or "")
                        for athlete in stat_type.get("athletes") or []:
                            rows.append((
                                game_id, team_id, name, team.get("conference"),
                                team.get("homeAway"), team.get("points"), category_name,
                                type_name, str(athlete.get("id") or ""),
                                str(athlete.get("name") or ""),
                                str(athlete.get("stat") or ""), _numeric(athlete.get("stat"))))
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO game_player_box_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
        return len(rows)

    def replace_game_player_ppa(self, payload: Iterable[dict[str, Any]]) -> int:
        """Store `/ppa/players/games` rows. The endpoint gives season/week/
        seasonType/team but no game id, so game_id is resolved here from the
        games table -- (season, week, season_type, team) identifies exactly
        one game a team plays. Rows that don't resolve (a team/week CFBD
        reports PPA for but this repository has no matching game row for,
        e.g. an FCS opponent never synced) are dropped rather than guessed at."""
        items = tuple(payload)
        if not items:
            return 0
        seasons = {int(item.get("season") or 0) for item in items}
        with self._reader() as connection:
            placeholders = ",".join("?" for _ in seasons)
            lookup: dict[tuple[int, int, str, str], int] = {}
            for row in connection.execute(
                f"SELECT game_id,season,week,season_type,home_team,away_team FROM games "
                f"WHERE season IN ({placeholders})", tuple(seasons),
            ):
                lookup[(row["season"], row["week"], row["season_type"], row["home_team"])] = row["game_id"]
                lookup[(row["season"], row["week"], row["season_type"], row["away_team"])] = row["game_id"]
        now = _now_iso()
        rows = []
        for item in items:
            player_id = str(item.get("id") or "")
            team = str(item.get("team") or "")
            if not player_id or not team:
                continue
            key = (int(item.get("season") or 0), int(item.get("week") or 0),
                   str(item.get("seasonType") or ""), team)
            game_id = lookup.get(key)
            if game_id is None:
                continue
            average = item.get("averagePPA") or {}
            rows.append((
                game_id, key[0], key[1], key[2], player_id, str(item.get("name") or ""),
                item.get("position"), team, str(item.get("opponent") or ""),
                _numeric(average.get("all")), _numeric(average.get("pass")),
                _numeric(average.get("rush")), now,
            ))
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO game_player_ppa VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
        return len(rows)

    def game_box_score(self, game_id: int) -> dict[str, Any] | None:
        game = self.get_game(game_id)
        if game is None:
            return None
        with self._reader() as connection:
            team_stats = [dict(row) for row in connection.execute(
                """SELECT * FROM game_team_box_stats WHERE game_id=?
                   ORDER BY CASE home_away WHEN 'away' THEN 0 ELSE 1 END,category""",
                (game_id,))]
            player_stats = [dict(row) for row in connection.execute(
                """SELECT * FROM game_player_box_stats WHERE game_id=?
                   ORDER BY CASE home_away WHEN 'away' THEN 0 ELSE 1 END,
                   category,player""", (game_id,))]
        return {"game": game, "team_stats": team_stats, "player_stats": player_stats,
                "available": bool(team_stats or player_stats)}

    def replace_records(self, season: int, records: Iterable[dict[str, Any]]) -> int:
        items = tuple(records)
        with self.transaction() as connection:
            connection.execute("DELETE FROM team_records WHERE season = ?", (season,))
            for item in items:
                total = item.get("total") or {}
                conference = item.get("conferenceGames") or {}
                connection.execute(
                    """
                    INSERT INTO team_records(season,team_id,team,conference,division,games,wins,
                    losses,ties,expected_wins,conference_games,conference_wins,
                    conference_losses,conference_ties) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        season, int(item["teamId"]), item["team"], item.get("conference"),
                        item.get("division"), int(total.get("games") or 0),
                        int(total.get("wins") or 0), int(total.get("losses") or 0),
                        int(total.get("ties") or 0), item.get("expectedWins"),
                        int(conference.get("games") or 0),int(conference.get("wins") or 0),
                        int(conference.get("losses") or 0),int(conference.get("ties") or 0),
                    ),
                )
        return len(items)

    def replace_player_stats(self, season: int, stats: Iterable[dict[str, Any]],
                             conference: str | None = None) -> int:
        items=tuple(stats)
        with self.transaction() as connection:
            if conference:
                connection.execute("DELETE FROM player_season_stats WHERE season=? AND conference=?",
                                   (season,conference))
            else:
                connection.execute("DELETE FROM player_season_stats WHERE season=?",(season,))
            connection.executemany(
                """INSERT OR REPLACE INTO player_season_stats VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [(season,str(item["playerId"]),str(item["player"]),str(item["team"]),
                  conference or item.get("conference"),item.get("position"),str(item["category"]),
                  str(item["statType"]),str(item.get("stat") or ""),
                  _numeric(item.get("stat"))) for item in items],
            )
        return len(items)

    def replace_player_ppa(self, season: int, rows: Iterable[dict[str, Any]]) -> int:
        """Store `/ppa/players/season` under `player_season_stats` as its own
        "ppa" category -- reuses the existing career-table/player-page
        pipeline (statlines.py's CATEGORY_SPECS/player_stat_tables) instead
        of a second storage or rendering path. Deletes only category="ppa"
        rows for the season, unlike replace_player_stats's whole-season
        wipe -- this call isn't conference-scoped (one CFBD call covers all
        of FBS), so a blanket delete here would have to run once and would
        risk racing a concurrent per-conference player-stats sync."""
        items = tuple(rows)
        flattened = []
        for item in items:
            player_id = str(item.get("id") or "")
            if not player_id:
                continue
            average = item.get("averagePPA") or {}
            for stat_type, key in (("PPA_ALL", "all"), ("PPA_PASS", "pass"), ("PPA_RUSH", "rush")):
                value = average.get(key)
                if value is None:
                    continue
                flattened.append((season, player_id, str(item.get("name") or ""),
                                  str(item.get("team") or ""), item.get("conference"),
                                  item.get("position"), "ppa", stat_type,
                                  str(value), _numeric(value)))
        with self.transaction() as connection:
            connection.execute("DELETE FROM player_season_stats WHERE season=? AND category='ppa'",
                               (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO player_season_stats VALUES(?,?,?,?,?,?,?,?,?,?)", flattened
            )
        return len(flattened)

    def player_ppa_rank(self, season: int, position: str) -> dict[str, dict[str, int]]:
        """Leaguewide PPA_ALL rank for every qualifying player at one
        position, keyed by player_id -- `rank_within`'s standard shape,
        reused directly from the NFL vertical (see
        sports_aggregator.nfl.ranking) rather than a second implementation.

        PPA itself carries no play-count field (confirmed live against
        CFBD), so "qualifying" volume is borrowed from whichever existing
        category actually tracks this position's snaps -- passing attempts
        for a QB, carries for a back, receptions for a receiver -- using
        the same box-score thresholds (statlines.qualifier) real leaders
        tables already use, rather than inventing a new floor.
        """
        category = _PPA_QUALIFYING_CATEGORY.get((position or "").upper())
        threshold = qualifier(category) if category else None
        if threshold is None:
            return {}
        qualifying_stat, minimum = threshold
        self.initialize()
        with self._reader() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT s.player_id,s.numeric_value AS value
                   FROM player_season_stats s
                   JOIN player_season_stats q
                     ON q.season=s.season AND q.player_id=s.player_id
                    AND q.category=? AND q.stat_type=? AND q.numeric_value>=?
                   WHERE s.season=? AND s.category='ppa' AND s.stat_type='PPA_ALL'
                     AND s.position=? AND s.numeric_value IS NOT NULL""",
                (category, qualifying_stat, minimum, season, position),
            )]
        return rank_within(rows, id_key="player_id", value_key="value")

    def confirm_transfer_pff_links(self, season: int, pff_season: int | None = None) -> dict[str, Any]:
        """Confirm PFF identities for players the portal proves have moved.

        The PFF importer would only confirm a player whose name matched on the
        *same* team, so anyone who transferred stayed at `possible_transfer` with
        no linked identity -- 1,933 players, including most of the highest-impact
        additions. The portal record closes that gap: CFBD says this exact player
        moved from school X to school Y, and the PFF row says he played at X.

        A link is only written when the origin school, the destination roster and
        a unique name all agree. Anything ambiguous is left alone.
        """
        self.initialize()
        pff_season = pff_season or (season - 1)
        with self.transaction() as connection:
            transfers = [dict(row) for row in connection.execute(
                """SELECT normalized_name,origin,destination FROM player_transfers
                   WHERE season=? AND origin IS NOT NULL AND destination IS NOT NULL""",
                (season,))]
            duplicates = {row["normalized_name"] for row in connection.execute(
                """SELECT normalized_name FROM player_transfers WHERE season=?
                   GROUP BY normalized_name HAVING COUNT(*)>1""", (season,))}
            roster = {}
            for row in connection.execute(
                "SELECT player_id,normalized_name,team FROM players WHERE season=?", (season,)
            ):
                roster.setdefault((row["normalized_name"], row["team"]), []).append(row["player_id"])
            candidates = {}
            for row in connection.execute(
                """SELECT pff_player_id,normalized_name,cfbd_team FROM pff_players
                   WHERE season=? AND match_status='possible_transfer'
                   AND cfbd_player_id IS NULL AND cfbd_team IS NOT NULL""", (pff_season,)
            ):
                candidates.setdefault((row["normalized_name"], row["cfbd_team"]), []).append(
                    row["pff_player_id"])

            confirmed = skipped = 0
            updates = []
            for transfer in transfers:
                name = transfer["normalized_name"]
                if name in duplicates:
                    skipped += 1
                    continue
                pff_ids = candidates.get((name, transfer["origin"]))
                roster_ids = roster.get((name, transfer["destination"]))
                if not pff_ids or not roster_ids:
                    continue
                if len(pff_ids) != 1 or len(roster_ids) != 1:
                    skipped += 1
                    continue
                # Only the identity link is written. The team fields keep naming
                # the school the performance actually happened at.
                updates.append((roster_ids[0], pff_ids[0], pff_season))
                confirmed += 1
            connection.executemany(
                """UPDATE pff_players SET cfbd_player_id=?,
                   match_status='portal_confirmed',match_confidence=0.9
                   WHERE pff_player_id=? AND season=?""", updates)
        return {"season": season, "pff_season": pff_season,
                "confirmed": confirmed, "ambiguous": skipped}

    def replace_recruits(self, season: int, recruits: Iterable[dict[str, Any]]) -> int:
        """Store the signing class so an incoming freshman carries his rating."""
        items = [row for row in recruits if row.get("id") is not None]
        now = _now_iso()
        with self.transaction() as connection:
            connection.execute("DELETE FROM recruits WHERE season=?", (season,))
            connection.executemany(
                """INSERT OR REPLACE INTO recruits VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(season, str(row["id"]),
                  str(row["athleteId"]) if row.get("athleteId") else None,
                  str(row.get("name") or ""), normalize_alias(str(row.get("name") or "")),
                  row.get("position"), row.get("committedTo"), row.get("recruitType"),
                  row.get("stars"), _numeric(row.get("rating")), row.get("ranking"),
                  row.get("height"), row.get("weight"),
                  row.get("city"), row.get("stateProvince"), now) for row in items])
        return len(items)

    def recruit_index(self, season: int) -> dict[str, dict[str, Any]]:
        """Recruits keyed by athlete id and by normalized name.

        CFBD supplies an athlete id for most recruits, which is an exact link to
        the roster. The name key is a fallback for the rest and is only trusted
        when it is unique within the class.

        Read once per request, like `_teams_by_id` and for the same reason: the
        index covers a whole signing class and is keyed only by season, but a
        matchup page asked for it eight times -- once per team per roster
        section -- rebuilding several thousand rows into the same dictionary
        each time, for a fifth of the page. Held for the request rather than
        the life of the repository so a refresh in another process cannot leave
        a stale class in a page the rendered-page cache believes is current.
        """
        return _request_cached(self, f"recruits:{int(season)}",
                               lambda: self._build_recruit_index(season))

    def _pff_interest(self, season: int) -> tuple[dict[str, Any], dict[Any, Any]]:
        """Interest scores by name and by CFBD id, for a whole season.

        The query has no team filter -- it never had one -- so every caller was
        reading all 10,000 rows for the season and building the same two
        dictionaries. A team page calls `roster_movements` six times.
        """
        return _request_cached(self, f"pff_interest:{int(season)}",
                               lambda: self._build_pff_interest(season))

    def _build_pff_interest(self, season: int) -> tuple[dict[str, Any], dict[Any, Any]]:
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT normalized_name,cfbd_player_id,interest_score
                   FROM pff_players
                   WHERE season=? AND interest_score IS NOT NULL""",
                (season,)).fetchall()
        by_name = {row["normalized_name"]: row["interest_score"] for row in rows}
        by_id = {row["cfbd_player_id"]: row["interest_score"]
                 for row in rows if row["cfbd_player_id"]}
        return by_name, by_id

    def _build_recruit_index(self, season: int) -> dict[str, dict[str, Any]]:
        self.initialize()
        with self._reader() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM recruits WHERE season=?", (season,))]
        index: dict[str, dict[str, Any]] = {}
        seen_names: dict[str, int] = {}
        for row in rows:
            seen_names[row["normalized_name"]] = seen_names.get(row["normalized_name"], 0) + 1
        for row in rows:
            if row["athlete_id"]:
                index[row["athlete_id"]] = row
            if seen_names[row["normalized_name"]] == 1:
                index.setdefault(row["normalized_name"], row)
        return index

    def replace_venues(self, venues: Iterable[dict[str, Any]]) -> int:
        """Store venue coordinates so travel and altitude can be described."""
        items = [row for row in venues if row.get("id") is not None]
        now = _now_iso()
        with self.transaction() as connection:
            connection.executemany(
                """INSERT INTO venues VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(venue_id) DO UPDATE SET name=excluded.name,city=excluded.city,
                   state=excluded.state,latitude=excluded.latitude,longitude=excluded.longitude,
                   timezone=excluded.timezone,elevation=excluded.elevation,dome=excluded.dome,
                   grass=excluded.grass,capacity=excluded.capacity,updated_at=excluded.updated_at""",
                [(int(row["id"]), str(row.get("name") or ""), row.get("city"), row.get("state"),
                  _numeric(row.get("latitude")), _numeric(row.get("longitude")),
                  row.get("timezone"), _numeric(row.get("elevation")),
                  int(bool(row.get("dome"))), (int(bool(row["grass"])) if row.get("grass") is not None else None),
                  row.get("capacity"), now) for row in items])
        return len(items)

    def team_venues(self) -> dict[int, dict[str, Any]]:
        """Home venue with coordinates for each team, where CFBD supplies them."""
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT t.team_id,v.venue_id,v.name venue_name,v.city,v.state,
                   v.latitude,v.longitude,v.timezone,v.elevation,v.dome
                   FROM teams t JOIN venues v ON v.venue_id=t.venue_id
                   WHERE v.latitude IS NOT NULL AND v.longitude IS NOT NULL""").fetchall()
        return {row["team_id"]: dict(row) for row in rows}

    def replace_promoted_stats(self, season: int, stats: Iterable[dict[str, Any]],
                               schools: Iterable[str]) -> int:
        """Store prior-classification statistics for specific schools only.

        `replace_player_stats` clears a whole conference-season, which would wipe
        an FBS conference when writing an FCS team's history into it. This
        replaces rows school by school and records the conference the team
        actually played in, so the provenance stays honest.
        """
        names = tuple(schools)
        if not names:
            return 0
        items = [row for row in stats if str(row.get("team")) in set(names)]
        with self.transaction() as connection:
            placeholders = ",".join("?" for _ in names)
            connection.execute(
                f"DELETE FROM player_season_stats WHERE season=? AND team IN ({placeholders})",
                (season, *names))
            connection.executemany(
                """INSERT OR REPLACE INTO player_season_stats
                   (season,player_id,player,team,conference,position,category,stat_type,
                    stat_value,numeric_value)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [(season, str(row.get("playerId") or row.get("player_id") or ""),
                  str(row.get("player") or ""), str(row.get("team") or ""),
                  str(row.get("conference") or ""), row.get("position"),
                  str(row.get("category") or ""), str(row.get("statType") or ""),
                  str(row.get("stat") if row.get("stat") is not None else ""),
                  _numeric(row.get("stat")))
                 for row in items])
        return len(items)

    def replace_transfers(self, season: int, transfers: Iterable[dict[str, Any]]) -> int:
        items = tuple(transfers); deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            name = normalize_alias(f"{item.get('firstName') or ''} {item.get('lastName') or ''}")
            origin = str(item.get("origin") or "")
            if not name or not origin:
                continue
            current = deduplicated.get((name, origin))
            if current is None or (
                bool(item.get("destination")), str(item.get("transferDate") or "")
            ) > (
                bool(current.get("destination")), str(current.get("transferDate") or "")
            ):
                deduplicated[(name, origin)] = item
        with self.transaction() as connection:
            connection.execute("DELETE FROM player_transfers WHERE season=?", (season,))
            connection.executemany(
                """INSERT INTO player_transfers VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        season,
                        normalize_alias(f"{item.get('firstName') or ''} {item.get('lastName') or ''}"),
                        str(item.get("firstName") or ""), str(item.get("lastName") or ""),
                        item.get("position"), str(item.get("origin") or ""),
                        item.get("destination"), item.get("transferDate"),
                        _numeric(item.get("rating")), item.get("stars"), item.get("eligibility"),
                    )
                    for item in deduplicated.values()
                    if item.get("origin") and (item.get("firstName") or item.get("lastName"))
                ],
            )
        return len(deduplicated)

    def replace_draft_picks(self, draft_year: int, picks: Iterable[dict[str, Any]]) -> int:
        items = tuple(picks)
        with self.transaction() as connection:
            connection.execute("DELETE FROM draft_picks WHERE draft_year=?", (draft_year,))
            connection.executemany(
                """INSERT INTO draft_picks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        draft_year, int(item["overall"]), int(item["round"]), int(item["pick"]),
                        str(item["collegeAthleteId"]) if item.get("collegeAthleteId") is not None else None,
                        item.get("collegeId"), str(item.get("collegeTeam") or ""),
                        item.get("collegeConference"), item.get("nflTeamId"),
                        str(item.get("nflTeam") or ""), str(item.get("name") or ""),
                        normalize_alias(str(item.get("name") or "")), item.get("position"),
                        item.get("preDraftRanking"), item.get("preDraftPositionRanking"),
                        item.get("preDraftGrade"),
                    )
                    for item in items
                    if item.get("overall") is not None
                ],
            )
        return len(items)

    def replace_returning_production(self, season: int, rows: Iterable[dict[str, Any]]) -> int:
        items = tuple(rows)
        with self.transaction() as connection:
            connection.execute("DELETE FROM returning_production WHERE season=?", (season,))
            connection.executemany(
                """INSERT INTO returning_production VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        season, item["team"], item.get("conference"),
                        _numeric(item.get("totalPPA")), _numeric(item.get("totalPassingPPA")),
                        _numeric(item.get("totalReceivingPPA")), _numeric(item.get("totalRushingPPA")),
                        _numeric(item.get("percentPPA")), _numeric(item.get("percentPassingPPA")),
                        _numeric(item.get("percentReceivingPPA")), _numeric(item.get("percentRushingPPA")),
                        _numeric(item.get("usage")), _numeric(item.get("passingUsage")),
                        _numeric(item.get("receivingUsage")), _numeric(item.get("rushingUsage")),
                    )
                    for item in items
                    if item.get("team")
                ],
            )
        return len(items)

    def replace_rankings(self, season: int, rankings: Iterable[PollRanking]) -> int:
        items = tuple(rankings)
        with self.transaction() as connection:
            connection.execute("DELETE FROM rankings WHERE season = ?", (season,))
            connection.executemany(
                "INSERT INTO rankings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item.season, item.season_type, item.week, item.poll,
                        int(item.is_final), item.rank, item.team_id, item.school,
                        item.conference, item.first_place_votes, item.points,
                    )
                    for item in items
                ],
            )
        return len(items)

    def replace_team_stats(self, season: int, stats: Iterable[dict[str, Any]]) -> int:
        items = tuple(stats)
        with self.transaction() as connection:
            connection.execute("DELETE FROM team_stats WHERE season = ?", (season,))
            connection.executemany(
                "INSERT INTO team_stats VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        season, item["team"], item.get("conference"), item["statName"],
                        str(item.get("statValue")) if item.get("statValue") is not None else None,
                    )
                    for item in items
                ],
            )
        return len(items)

    def replace_advanced_stats(self, season: int, stats: Iterable[dict[str, Any]]) -> int:
        items = tuple(stats)
        with self.transaction() as connection:
            connection.execute("DELETE FROM team_advanced_stats WHERE season = ?", (season,))
            for item in items:
                offense = item.get("offense") or {}
                defense = item.get("defense") or {}
                connection.execute(
                    "INSERT INTO team_advanced_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        season, item["team"], item.get("conference"),
                        _metric(offense, "successRate"), _metric(defense, "successRate"),
                        _metric(offense, "explosiveness"), _metric(defense, "explosiveness"),
                        _metric(offense, "ppa"), _metric(defense, "ppa"),
                        _metric(offense, "pointsPerOpportunity"),
                        _metric(defense, "pointsPerOpportunity"),
                        _metric(offense.get("havoc") or {}, "total"),
                        _metric(defense.get("havoc") or {}, "total"),
                        json.dumps(offense, separators=(",", ":")),
                        json.dumps(defense, separators=(",", ":")),
                    ),
                )
        return len(items)

    def replace_core_ratings(self, season: int, ratings: Iterable[dict[str, Any]]) -> int:
        items = tuple(ratings)
        with self.transaction() as connection:
            connection.execute("DELETE FROM core_ratings WHERE season = ?", (season,))
            connection.executemany(
                "INSERT INTO core_ratings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        season, item["throughSeasonType"], int(item["throughWeek"]),
                        item["team"], item.get("conference"), float(item["overall"]),
                        float(item["offense"]), float(item["defense"]),
                        int(item["offensePlays"]), int(item["defensePlays"]),
                        item["modelVersion"],
                    )
                    for item in items
                ],
            )
        return len(items)

    def record_sync(self, report: Any) -> None:
        details = [
            {"dataset": item.dataset, "count": item.count, "status": item.status, "message": item.message}
            for item in report.datasets
        ]
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO sync_runs (season, started_at, finished_at, succeeded, details_json) VALUES (?, ?, ?, ?, ?)",
                (
                    report.season, report.started_at.isoformat(), report.finished_at.isoformat(),
                    int(report.succeeded), json.dumps(details, separators=(",", ":")),
                ),
            )

    def resolve_team_alias(self, value: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT t.*, a.alias FROM team_aliases a
                JOIN teams t ON t.team_id = a.team_id
                WHERE a.normalized_alias = ? ORDER BY t.school
                """,
                (normalize_alias(value),),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self, season: int) -> dict[str, Any]:
        self.initialize()
        with self._reader() as connection:
            counts = {
                "teams": connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
                "games": connection.execute("SELECT COUNT(*) FROM games WHERE season = ?", (season,)).fetchone()[0],
                "coach_seasons": connection.execute("SELECT COUNT(*) FROM coach_seasons WHERE season = ?", (season,)).fetchone()[0],
                "players": connection.execute("SELECT COUNT(*) FROM players WHERE season = ?", (season,)).fetchone()[0],
                "rankings": connection.execute("SELECT COUNT(*) FROM rankings WHERE season = ?", (season,)).fetchone()[0],
                "team_stats": connection.execute("SELECT COUNT(*) FROM team_stats WHERE season = ?", (season,)).fetchone()[0],
                "player_stats": connection.execute("SELECT COUNT(*) FROM player_season_stats WHERE season = ?", (season,)).fetchone()[0],
                "transfers": connection.execute("SELECT COUNT(*) FROM player_transfers WHERE season = ?", (season,)).fetchone()[0],
                "draft_picks": connection.execute("SELECT COUNT(*) FROM draft_picks WHERE draft_year = ?", (season,)).fetchone()[0],
                "returning_production": connection.execute("SELECT COUNT(*) FROM returning_production WHERE season = ?", (season,)).fetchone()[0],
                "advanced_stats": connection.execute("SELECT COUNT(*) FROM team_advanced_stats WHERE season = ?", (season,)).fetchone()[0],
                "core_ratings": connection.execute("SELECT COUNT(*) FROM core_ratings WHERE season = ?", (season,)).fetchone()[0],
                "pff_players": connection.execute("SELECT COUNT(*) FROM pff_players WHERE season = ?", (season,)).fetchone()[0],
                "pff_metrics": connection.execute("SELECT COUNT(*) FROM pff_player_metrics WHERE season = ?", (season,)).fetchone()[0],
                "team_box_stats": connection.execute(
                    """SELECT COUNT(*) FROM game_team_box_stats b JOIN games g
                       ON g.game_id=b.game_id WHERE g.season=?""", (season,)).fetchone()[0],
                "player_box_stats": connection.execute(
                    """SELECT COUNT(*) FROM game_player_box_stats b JOIN games g
                       ON g.game_id=b.game_id WHERE g.season=?""", (season,)).fetchone()[0],
            }
            history_state = [dict(row) for row in connection.execute(
                """SELECT dataset,row_count,synced_at FROM history_sync_state
                   WHERE season=? ORDER BY dataset""", (season,))]
            last_sync = connection.execute(
                "SELECT * FROM sync_runs WHERE season = ? ORDER BY sync_id DESC LIMIT 1", (season,)
            ).fetchone()
        sync_payload = dict(last_sync) if last_sync else None
        if sync_payload:
            sync_payload["details"] = json.loads(sync_payload.pop("details_json"))
        return {"season": season, "counts": counts,
                "history_datasets": history_state, "last_sync": sync_payload}

    def teams(self, conference: str | None = None, limit: int = 150) -> list[dict[str, Any]]:
        self.initialize()
        sql = "SELECT * FROM teams"
        params: list[Any] = []
        if conference:
            sql += " WHERE conference = ?"
            params.append(conference)
        sql += " ORDER BY school LIMIT ?"
        params.append(limit)
        with self._reader() as connection:
            rows = connection.execute(sql, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["logos"] = json.loads(item.pop("logos_json"))
            results.append(item)
        return results

    def conferences(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._reader() as connection:
            rows=connection.execute("""SELECT conference,COUNT(*) team_count FROM teams
              WHERE conference IS NOT NULL GROUP BY conference ORDER BY conference""").fetchall()
        return [{**dict(row),"slug":conference_slug(row["conference"])} for row in rows]

    def conference_by_slug(self, slug: str) -> dict[str, Any] | None:
        return next((item for item in self.conferences() if item["slug"]==slug),None)

    def conference_standings(self, conference: str, season: int) -> list[dict[str, Any]]:
        self.initialize(); rankings=self.latest_rankings(season)["teams"]
        rank_by_team={item["school"]:item["rank"] for item in rankings}
        elo_by_team=self.team_elo(season)
        with self._reader() as connection:
            rows=connection.execute("""SELECT t.team_id,t.school,t.mascot,t.color,t.logos_json,
              COALESCE(r.games,0) games,COALESCE(r.wins,0) wins,COALESCE(r.losses,0) losses,
              COALESCE(r.ties,0) ties,COALESCE(r.conference_games,0) conference_games,
              COALESCE(r.conference_wins,0) conference_wins,
              COALESCE(r.conference_losses,0) conference_losses,
              COALESCE(r.conference_ties,0) conference_ties,r.expected_wins
              FROM teams t LEFT JOIN team_records r ON r.team_id=t.team_id AND r.season=?
              WHERE t.conference=?""",(season,conference)).fetchall()
        result=[]
        for row in rows:
            item=dict(row); item["rank"]=rank_by_team.get(item["school"])
            elo=elo_by_team.get(item["team_id"]) or {}
            item["elo"]=elo.get("elo"); item["elo_rank"]=elo.get("elo_rank")
            item["elo_week"]=elo.get("week")
            item["logos"]=json.loads(item.pop("logos_json")); result.append(item)
        result.sort(key=lambda item:(-item["conference_wins"],item["conference_losses"],
                                    -item["wins"],item["losses"],item["rank"] or 999,item["school"]))
        return result

    def stat_coverage(self, seasons: Iterable[int] | None = None) -> dict[str, Any]:
        """Which conference-seasons actually have player statistics stored.

        The player-stat sync runs conference by conference, so one failed request
        leaves a silent hole: Notre Dame had no statistics for five seasons
        because the Independents were never synchronized, and nothing in the
        application said so. This makes the gaps explicit.
        """
        self.initialize()
        with self._reader() as connection:
            stored = {(row["season"], row["conference"]): row["rows"]
                      for row in connection.execute(
                          """SELECT season,conference,COUNT(*) rows
                             FROM player_season_stats GROUP BY 1,2""")}
            conferences = [row["conference"] for row in connection.execute(
                "SELECT DISTINCT conference FROM teams "
                "WHERE conference IS NOT NULL ORDER BY conference")]
            available = sorted({row["season"] for row in connection.execute(
                "SELECT DISTINCT season FROM player_season_stats")})
        wanted = sorted(seasons) if seasons else available
        grid, gaps = [], []
        for season in wanted:
            row = {"season": season, "conferences": {}}
            for conference in conferences:
                count = stored.get((season, conference), 0)
                row["conferences"][conference] = count
                if not count:
                    gaps.append({"season": season, "conference": conference})
            row["total"] = sum(row["conferences"].values())
            grid.append(row)
        return {"seasons": wanted, "conferences": conferences,
                "grid": grid, "gaps": gaps, "gap_count": len(gaps)}

    def history_coverage(self) -> dict[str, Any]:
        """Season-level completeness for every dataset used by history pages."""
        self.initialize()
        tables = {
            "games": "games", "records": "team_records", "team_stats": "team_stats",
            "advanced_stats": "team_advanced_stats", "coaches": "coach_seasons",
            "player_stats": "player_season_stats",
        }
        by_year: dict[int, dict[str, int]] = {}
        with self._reader() as connection:
            for label, table in tables.items():
                for row in connection.execute(
                    f"SELECT season,COUNT(*) rows FROM {table} GROUP BY season"):
                    by_year.setdefault(row["season"], {"season": row["season"]})[label] = row["rows"]
        rows = []
        for year in sorted(by_year, reverse=True):
            row = by_year[year]
            for label in tables:
                row.setdefault(label, 0)
            row["complete"] = all(row[label] > 0 for label in tables)
            rows.append(row)
        return {"seasons": rows, "complete_seasons": sum(row["complete"] for row in rows),
                "season_count": len(rows)}

    def team_elo(self, season: int) -> dict[int, dict[str, Any]]:
        """Current Elo per team, derived from CFBD per-game pregame ratings.

        CFBD publishes Elo on the game record (`homePregameElo` / `awayPregameElo`),
        not as a team-level table, and only for games near enough to have been
        rated. Each team therefore takes the rating attached to its most recent
        rated game, and the week that rating came from travels with it so the page
        can say how current it is.
        """
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT team_id,elo,week FROM (
                     SELECT home_team_id team_id,home_pregame_elo elo,week,start_date
                       FROM games WHERE season=? AND home_pregame_elo IS NOT NULL
                     UNION ALL
                     SELECT away_team_id team_id,away_pregame_elo elo,week,start_date
                       FROM games WHERE season=? AND away_pregame_elo IS NOT NULL
                   ) ORDER BY start_date""",
                (season, season)).fetchall()
            fbs_ids = {row["team_id"] for row in connection.execute(
                "SELECT team_id FROM teams WHERE lower(classification)='fbs'")}
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            if row["team_id"] is None:
                continue
            latest[row["team_id"]] = {"elo": row["elo"], "week": row["week"],
                                      "source": "CFBD pregame Elo"}
        if not latest:
            return {}
        rank_pool = ((team_id, entry) for team_id, entry in latest.items()
                     if not fbs_ids or team_id in fbs_ids)
        ordered = sorted(rank_pool, key=lambda pair: -(pair[1]["elo"] or 0))
        for rank, (team_id, entry) in enumerate(ordered, start=1):
            entry["elo_rank"] = rank
        return latest

    def team_elo_history(self, team_id: int, season: int) -> list[dict[str, Any]]:
        """This team's own week-by-week pregame Elo, in season order."""
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT week,
                          CASE WHEN home_team_id=? THEN home_pregame_elo ELSE away_pregame_elo END elo
                   FROM games WHERE season=? AND (home_team_id=? OR away_team_id=?)
                   ORDER BY week""",
                (team_id, season, team_id, team_id)).fetchall()
        return [{"week": row["week"], "elo": row["elo"]} for row in rows if row["elo"] is not None]

    def team_rank_history(self, team_id: int, season: int, poll: str = "AP Top 25") -> list[dict[str, Any]]:
        """This team's own week-by-week poll rank, in season order.

        Unranked weeks are simply absent -- CFBD's rankings feed only carries a
        row for a team while it holds a spot in the poll.
        """
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT week, rank FROM rankings
                   WHERE season=? AND team_id=? AND poll=? ORDER BY week""",
                (season, team_id, poll)).fetchall()
        return [{"week": row["week"], "rank": row["rank"]} for row in rows]

    def elo_snapshot(self, season: int) -> dict[str, Any]:
        """A page-ready FBS Elo leaderboard with movement and league context.

        Elo is carried on game records rather than a standalone CFBD endpoint.
        Keeping the reconstruction here gives the HTML page and JSON API the
        same definition of "current": the most recent dated pregame rating for
        each team in the requested season.
        """
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT rated.team_id,rated.elo,rated.week,rated.start_date,
                          rated.game_id,rated.completed,t.school,t.conference
                     FROM (
                       SELECT game_id,home_team_id team_id,home_pregame_elo elo,
                              week,start_date,completed
                         FROM games
                        WHERE season=? AND home_pregame_elo IS NOT NULL
                       UNION ALL
                       SELECT game_id,away_team_id team_id,away_pregame_elo elo,
                              week,start_date,completed
                         FROM games
                        WHERE season=? AND away_pregame_elo IS NOT NULL
                     ) rated
                     JOIN teams t ON t.team_id=rated.team_id
                    WHERE lower(t.classification)='fbs'
                    ORDER BY rated.start_date,rated.game_id""",
                (season, season),
            ).fetchall()
            available_seasons = [row["season"] for row in connection.execute(
                """SELECT DISTINCT g.season
                     FROM games g JOIN teams t
                       ON t.team_id IN (g.home_team_id,g.away_team_id)
                    WHERE lower(t.classification)='fbs'
                      AND (g.home_pregame_elo IS NOT NULL
                           OR g.away_pregame_elo IS NOT NULL)
                    ORDER BY g.season DESC""")]

        histories: dict[int, list[dict[str, Any]]] = {}
        for raw in rows:
            histories.setdefault(int(raw["team_id"]), []).append(dict(raw))

        rankings: list[dict[str, Any]] = []
        for team_rows in histories.values():
            # A full schedule can contain Elo values on several future games.
            # The first upcoming observation is the current rating; selecting
            # the last dated row jumped the page months into the future. Once a
            # season has no upcoming rated game, the last available pregame
            # observation is the honest endpoint supplied by CFBD.
            upcoming = [row for row in team_rows if not row["completed"]]
            latest = upcoming[0] if upcoming else team_rows[-1]
            current_index = team_rows.index(latest)
            observed = team_rows[:current_index + 1]
            previous_row = observed[-2] if len(observed) > 1 else None
            first = observed[0]
            previous = previous_row["elo"] if previous_row is not None else None
            values = [int(row["elo"]) for row in observed]
            weekly_change = (int(latest["elo"] - previous)
                             if previous is not None else None)
            season_change = (int(latest["elo"] - first["elo"])
                             if len(observed) > 1 else None)
            rankings.append({
                "team_id": latest["team_id"],
                "team": latest["school"],
                "conference": latest["conference"],
                "elo": int(latest["elo"]),
                "previous_elo": int(previous) if previous is not None else None,
                "change": weekly_change,
                "weekly_change": weekly_change,
                "season_change": season_change,
                "week": latest["week"],
                "rating_context": (f"Entering Week {latest['week']}"
                                   if not latest["completed"] else f"Week {latest['week']} pregame"),
                "rated_games": len(observed),
                "season_high": max(values),
                "season_low": min(values),
            })
        rankings.sort(key=lambda row: (-row["elo"], row["team"]))
        for rank, row in enumerate(rankings, start=1):
            row["rank"] = rank

        conference_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rankings:
            conference_groups.setdefault(row["conference"] or "Independent", []).append(row)
        conferences = []
        for conference, conference_rows in conference_groups.items():
            leader = conference_rows[0]
            conferences.append({
                "conference": conference,
                "average_elo": round(
                    sum(row["elo"] for row in conference_rows) / len(conference_rows), 1),
                "rated_teams": len(conference_rows),
                "leader": leader["team"],
                "leader_team_id": leader["team_id"],
                "leader_elo": leader["elo"],
                "leader_rank": leader["rank"],
            })
        conferences.sort(key=lambda row: (-row["average_elo"], row["conference"]))

        values = sorted(row["elo"] for row in rankings)
        middle = len(values) // 2
        median = (values[middle] if len(values) % 2
                  else ((values[middle - 1] + values[middle]) / 2 if values else None))
        def movers(key: str, rising: bool) -> list[dict[str, Any]]:
            candidates = [row for row in rankings
                          if row.get(key) is not None and (row[key] > 0 if rising else row[key] < 0)]
            candidates.sort(key=lambda row: (-row[key], row["team"])
                            if rising else (row[key], row["team"]))
            return candidates[:5]

        return {
            "season": season,
            "available_seasons": available_seasons,
            "rankings": rankings,
            "conferences": conferences,
            "movement": {
                "weekly": {
                    "risers": movers("weekly_change", True),
                    "fallers": movers("weekly_change", False),
                },
                "season": {
                    "risers": movers("season_change", True),
                    "fallers": movers("season_change", False),
                },
            },
            "summary": {
                "rated_teams": len(rankings),
                "leader": rankings[0]["team"] if rankings else None,
                "leader_elo": rankings[0]["elo"] if rankings else None,
                "median_elo": median,
                "rating_spread": values[-1] - values[0] if values else None,
                "latest_week": max((row["week"] for row in rankings
                                    if row["week"] is not None), default=None),
            },
        }

    def conference_games(self, conference: str, season: int, limit: int=30) -> list[dict[str,Any]]:
        self.initialize(); now=datetime.now(timezone.utc).isoformat()
        with self._reader() as connection:
            rows=connection.execute("""SELECT * FROM games WHERE season=? AND completed=0
              AND start_date>=? AND (home_conference=? OR away_conference=?)
              ORDER BY start_date LIMIT ?""",(season,now,conference,conference,limit)).fetchall()
        return [dict(row) for row in rows]

    #: Leader categories, in reading order, ranked by their conventional stat.
    LEADER_CATEGORIES = ("passing", "rushing", "receiving", "defensive",
                         "interceptions", "kicking")

    #: How many games a team must have played this season before its own
    #: partial numbers, rather than last year's full ones, rank the leaderboard.
    #: One game reshuffles the board around a single carry; by three the sample
    #: is real. Until then the board stays on the prior season and each row
    #: carries its current-season line so this year's production is still shown.
    LEADER_SETTLE_GAMES = 3

    def _season_games_played(self, connection, season: int,
                             scope_column: str, scope_value: str) -> int:
        if scope_column == "team":
            row = connection.execute(
                """SELECT COUNT(*) FROM games
                   WHERE season=? AND completed=1 AND (home_team=? OR away_team=?)""",
                (season, scope_value, scope_value)).fetchone()
            return int(row[0] or 0)
        # A conference has no single game count; the weeks that have finished is
        # the honest "how far into the year are we" signal for its leaderboard.
        row = connection.execute(
            "SELECT COUNT(DISTINCT week) FROM games WHERE season=? AND completed=1",
            (season,)).fetchone()
        return int(row[0] or 0)

    def _arrival_stat_lines(self, connection, season: int, team: str,
                            categories: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        """Prior-season production of players who transferred in.

        Leaders are queried by the team the statistics were recorded under, so an
        arriving starter is invisible on his new team's page even though he is the
        most likely player to produce there. His numbers are pulled from his old
        school and clearly marked as earned elsewhere.
        """
        rows = connection.execute(
            """SELECT s.player_id,s.player,s.position,s.team,s.category,s.stat_type,
               s.stat_value,s.numeric_value,t.origin
               FROM player_transfers t
               JOIN players r ON r.season=? AND r.normalized_name=t.normalized_name
                 AND r.team=t.destination
               JOIN player_season_stats s ON s.season=? AND s.player_id=r.player_id
               WHERE t.season=? AND t.destination=? AND s.numeric_value IS NOT NULL""",
            (season, season - 1, season, team)).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["category"] not in categories:
                continue
            grouped.setdefault(row["category"], []).append(dict(row))
        return grouped

    def _stat_leaders(self, *, season: int, conference: str | None=None,
                      team: str | None=None, limit: int=8) -> dict[str,Any]:
        """Leaders per category, each carrying the full stat line.

        Ranking by a single statistic and displaying only that number hides the
        context that makes it meaningful, so every leader also returns the other
        statistics CFBD published for the same category.
        """
        self.initialize()
        with self._reader() as connection:
            scope_column="conference" if conference else "team"; scope_value=conference or team
            available=connection.execute(
                f"SELECT MAX(season) FROM player_season_stats WHERE season<=? AND {scope_column}=?",
                (season,scope_value)).fetchone()[0]
            if available is None:
                # A team can have no statistics of its own and still have
                # production on the roster, if it arrived through the portal.
                groups: dict[str, Any] = {}
                if team:
                    self._merge_arrivals(connection, groups, season, team, limit)
                return {"season": season - 1 if groups else None,
                        "current_season": season, "settled": False, "groups": groups}
            # Hold the board on the prior season until this one has enough games
            # behind it, so the leaderboard settles once rather than reshuffling
            # every week of September.
            games_played = self._season_games_played(
                connection, season, scope_column, scope_value)
            settled = games_played >= self.LEADER_SETTLE_GAMES
            if available >= season and not settled and season - 1 >= 1:
                prior = connection.execute(
                    f"""SELECT MAX(season) FROM player_season_stats
                        WHERE season<? AND {scope_column}=?""",
                    (season, scope_value)).fetchone()[0]
                if prior is not None:
                    available = prior
            groups={}
            # Per-game rates need the games played by whichever team a line was
            # actually recorded for -- the scope team for most rows, but an
            # arrival's origin school for a line merged in from a transfer.
            # Cached per team since a conference board spans many of them.
            team_games_cache: dict[str, int] = {}
            def team_games(school: str) -> int:
                if school not in team_games_cache:
                    team_games_cache[school] = self._season_games_played(
                        connection, available, "team", school)
                return team_games_cache[school]
            active_ids: list[str] | None = None
            if available < season:
                # A prior-season fallback is useful only for players who are
                # actually on the requested-season roster. Without this guard,
                # drafted and transferred stars were presented as current
                # leaders (for example Ashton Jeanty on 2026 Boise State).
                if team:
                    active_rows = connection.execute(
                        "SELECT player_id FROM players WHERE season=? AND team=?",
                        (season, team)).fetchall()
                else:
                    active_rows = connection.execute(
                        """SELECT p.player_id FROM players p JOIN teams t ON t.school=p.team
                           WHERE p.season=? AND t.conference=?""",
                        (season, conference)).fetchall()
                active_ids = list(dict.fromkeys(str(row[0]) for row in active_rows))
            active_placeholders = ",".join("?" for _ in (active_ids or []))
            for category in self.LEADER_CATEGORIES:
                if active_ids == []:
                    continue
                stat_type=sort_stat(category)
                threshold=qualifier(category)
                active_plain = (f" AND player_id IN ({active_placeholders})"
                                if active_ids is not None else "")
                active_qualified = (f" AND s.player_id IN ({active_placeholders})"
                                    if active_ids is not None else "")
                if threshold is None:
                    rows=connection.execute(f"""SELECT player_id,player,position,team,stat_value,numeric_value
                      FROM player_season_stats WHERE season=? AND {scope_column}=? AND category=? AND stat_type=?
                      AND numeric_value IS NOT NULL AND numeric_value>0{active_plain}
                      ORDER BY numeric_value DESC,player LIMIT ?""",
                      (available,scope_value,category,stat_type,*(active_ids or []),limit)).fetchall()
                else:
                    # Rank on the headline statistic, but only among players who
                    # cleared the usage minimum for the category.
                    qualifying_stat,minimum=threshold
                    rows=connection.execute(f"""SELECT s.player_id,s.player,s.position,s.team,
                      s.stat_value,s.numeric_value
                      FROM player_season_stats s JOIN player_season_stats q
                        ON q.season=s.season AND q.player_id=s.player_id AND q.category=s.category
                        AND q.stat_type=? AND q.numeric_value>=?
                      WHERE s.season=? AND s.{scope_column}=? AND s.category=? AND s.stat_type=?
                      AND s.numeric_value IS NOT NULL{active_qualified}
                      ORDER BY s.numeric_value DESC,s.player LIMIT ?""",
                      (qualifying_stat,minimum,available,scope_value,category,stat_type,
                       *(active_ids or []),limit)).fetchall()
                if not rows: continue
                identifiers=[row["player_id"] for row in rows]
                placeholders=",".join("?" for _ in identifiers)
                detail=connection.execute(f"""SELECT player_id,stat_type,stat_value,numeric_value
                  FROM player_season_stats WHERE season=? AND {scope_column}=? AND category=?
                  AND player_id IN ({placeholders})""",
                  (available,scope_value,category,*identifiers)).fetchall()
                line: dict[str, dict[str, Any]] = {}
                for item in detail:
                    value=item["numeric_value"]
                    line.setdefault(item["player_id"],{})[item["stat_type"]]=(
                        value if value is not None else item["stat_value"])
                groups[category]={
                    "label":category_label(category),
                    "stat_type":stat_type,
                    "qualifier":(f"min {threshold[1]:g} {threshold[0]}" if threshold else None),
                    "players":[{**dict(row),"stats":line.get(row["player_id"],{}),
                                "arrival":False, "games_played":team_games(row["team"])}
                               for row in rows],
                }
            # Arrivals fill a gap only while the board still ranks on a season
            # before this team's own current-season stats exist under its own
            # scope (available < season). Once available reaches season, every
            # current-roster player -- transfers included -- already has a real
            # row from the query above; merging their old school's prior-season
            # line back in would re-inject a stale, often larger number that
            # outranks their actual current production.
            if team and available < season:
                self._merge_arrivals(connection, groups, season, team, limit)

            # A category with no returning or incoming production still
            # deserves a place on the board: it means this team has no proven
            # answer there, which is itself worth knowing, not a reason to
            # drop "Passing" from the tabs entirely. Surface whoever leads
            # the category so far this season -- below the usual qualifying
            # threshold, since the whole point is there is no established
            # line yet -- with a zeroed headline (there is no returning or
            # incoming production to rank) and let the companion attachment
            # below carry his real current-season number as the sub text.
            if team and available < season and active_ids != []:
                for category in self.LEADER_CATEGORIES:
                    if category in groups:
                        continue
                    stat_type = sort_stat(category)
                    if not stat_type:
                        continue
                    current_leader = connection.execute(
                        """SELECT player_id,player,position,team FROM player_season_stats
                           WHERE season=? AND team=? AND category=? AND stat_type=?
                             AND numeric_value IS NOT NULL AND numeric_value>0
                           ORDER BY numeric_value DESC,player LIMIT 1""",
                        (season, team, category, stat_type)).fetchone()
                    if current_leader is None:
                        continue
                    zero_stats = {column.key: 0 for column in category_columns(category)}
                    groups[category] = {
                        "label": category_label(category), "stat_type": stat_type,
                        "qualifier": None,
                        "players": [{
                            "player_id": current_leader["player_id"],
                            "player": current_leader["player"],
                            "position": current_leader["position"],
                            "team": current_leader["team"],
                            "stats": zero_stats, "arrival": False,
                            "games_played": team_games(current_leader["team"]),
                            "no_baseline": True,
                        }],
                    }

            # Attach the other season's line for every listed player, so a board
            # ranked on 2025 still shows what a leader has done in 2026 (and a
            # settled 2026 board still shows the 2025 baseline it grew out of).
            companion_season = season if available < season else available - 1
            self._attach_companion_lines(
                connection, groups, companion_season, scope_column, scope_value)
        return {"season": available, "current_season": season,
                "companion_season": companion_season, "settled": settled,
                "games_played": games_played, "groups": groups}

    def _attach_companion_lines(self, connection, groups: dict[str, Any],
                                companion_season: int, scope_column: str,
                                scope_value: str) -> None:
        ids_by_category: dict[str, list[str]] = {
            category: [str(row["player_id"]) for row in group["players"]]
            for category, group in groups.items()
        }
        every_id = {pid for ids in ids_by_category.values() for pid in ids}
        if not every_id:
            return
        placeholders = ",".join("?" for _ in every_id)
        rows = connection.execute(
            f"""SELECT player_id,category,stat_type,stat_value,numeric_value
                FROM player_season_stats
                WHERE season=? AND {scope_column}=? AND player_id IN ({placeholders})""",
            (companion_season, scope_value, *every_id)).fetchall()
        lines: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            value = row["numeric_value"]
            lines.setdefault((str(row["player_id"]), row["category"]), {})[row["stat_type"]] = (
                value if value is not None else row["stat_value"])
        for category, group in groups.items():
            for entry in group["players"]:
                companion = lines.get((str(entry["player_id"]), category))
                entry["companion"] = companion or {}
                entry["companion_season"] = companion_season

    def _merge_arrivals(self, connection, groups: dict[str, Any], season: int,
                        team: str, limit: int) -> None:
        """Fold transferred-in production into the team's leader groups."""
        arrivals = self._arrival_stat_lines(
            connection, season, team, tuple(self.LEADER_CATEGORIES))
        # Arrival lines are always drawn from season - 1 (see
        # _arrival_stat_lines), so their per-game rate needs that origin
        # school's games in that same prior season, not the board's own season.
        origin_games_cache: dict[str, int] = {}
        def origin_games(school: str) -> int:
            if school not in origin_games_cache:
                origin_games_cache[school] = self._season_games_played(
                    connection, season - 1, "team", school)
            return origin_games_cache[school]
        for category, rows in arrivals.items():
            statistic = sort_stat(category)
            lines: dict[str, dict[str, Any]] = {}
            for row in rows:
                entry = lines.setdefault(row["player_id"], {
                    "player_id": row["player_id"], "player": row["player"],
                    "position": row["position"], "team": row["team"],
                    "origin": row["origin"], "arrival": True, "stats": {},
                    "games_played": origin_games(row["team"]),
                })
                value = row["numeric_value"]
                entry["stats"][row["stat_type"]] = (
                    value if value is not None else row["stat_value"])
            qualifying = [entry for entry in lines.values()
                          if float(entry["stats"].get(statistic) or 0) > 0]
            if not qualifying:
                continue
            group = groups.setdefault(category, {
                "label": category_label(category), "stat_type": statistic,
                "qualifier": None, "players": [],
            })
            merged = group["players"] + qualifying
            merged.sort(key=lambda entry: -float(entry["stats"].get(statistic) or 0))
            group["players"] = merged[:limit]

    def conference_player_leaders(self, conference: str, season: int, limit: int=8) -> dict[str,Any]:
        return self._stat_leaders(season=season,conference=conference,limit=limit)

    def team_player_leaders(self, team: str, season: int, limit: int=8) -> dict[str,Any]:
        return self._stat_leaders(season=season,team=team,limit=limit)

    def team_impact_players(self, team: str, season: int,
                            limit: int = 12) -> list[dict[str, Any]]:
        """Current producers who are materially affecting a team's games.

        Unlike the preseason/PFF lists, this uses only statistics recorded for
        the requested season. Qualifying producers are tiered by strength and
        role leadership, and a dual-role player is merged rather than repeated.
        """
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT player_id,player,position,category,stat_type,numeric_value
                   FROM player_season_stats
                   WHERE season=? AND team=? AND numeric_value IS NOT NULL
                   ORDER BY player,category,stat_type""", (season, team)).fetchall()

        players: dict[str, dict[str, Any]] = {}
        for raw in rows:
            player = players.setdefault(str(raw["player_id"]), {
                "player_id": str(raw["player_id"]), "player": raw["player"],
                "position": raw["position"], "team": team, "stats": {},
            })
            player["stats"].setdefault(raw["category"], {})[raw["stat_type"]] = (
                float(raw["numeric_value"]))

        def stat(player: dict[str, Any], category: str, key: str) -> float:
            return float(player["stats"].get(category, {}).get(key) or 0)

        def number(value: float) -> str:
            return f"{value:,.1f}".rstrip("0").rstrip(".")

        candidates: list[tuple[str, str, Any, Any, Any, Any]] = [
            ("Passing engine", "Current passing production",
             lambda p: stat(p, "passing", "ATT") >= 10 or stat(p, "passing", "YDS") >= 100,
             lambda p: stat(p, "passing", "YDS") + 20 * stat(p, "passing", "TD")
                       - 10 * stat(p, "passing", "INT"),
             lambda p: (f"{number(stat(p, 'passing', 'YDS'))} pass yd · "
                        f"{number(stat(p, 'passing', 'TD'))} TD · "
                        f"{number(stat(p, 'passing', 'INT'))} INT"),
             lambda p: stat(p, "passing", "YDS") >= 500 or stat(p, "passing", "TD") >= 5),
            ("Ground engine", "Current rushing production",
             lambda p: stat(p, "rushing", "CAR") >= 5 and stat(p, "rushing", "YDS") >= 30,
             lambda p: stat(p, "rushing", "YDS") + 20 * stat(p, "rushing", "TD"),
             lambda p: (f"{number(stat(p, 'rushing', 'YDS'))} rush yd · "
                        f"{number(stat(p, 'rushing', 'TD'))} TD · "
                        f"{number(stat(p, 'rushing', 'YPC'))} avg"),
             lambda p: stat(p, "rushing", "YDS") >= 200 or stat(p, "rushing", "TD") >= 3),
            ("Primary target", "Current receiving production",
             lambda p: stat(p, "receiving", "REC") >= 3 or stat(p, "receiving", "YDS") >= 40,
             lambda p: stat(p, "receiving", "YDS") + 20 * stat(p, "receiving", "TD"),
             lambda p: (f"{number(stat(p, 'receiving', 'REC'))} rec · "
                        f"{number(stat(p, 'receiving', 'YDS'))} yd · "
                        f"{number(stat(p, 'receiving', 'TD'))} TD"),
             lambda p: stat(p, "receiving", "YDS") >= 150 or stat(p, "receiving", "TD") >= 2),
            ("Defensive disruptor", "Current disruptive production",
             lambda p: (stat(p, "defensive", "TOT") >= 8
                        or stat(p, "defensive", "TFL") >= 1
                        or stat(p, "defensive", "SACKS") >= 1
                        or stat(p, "interceptions", "INT") >= 1
                        or stat(p, "defensive", "PD") >= 2),
             lambda p: (stat(p, "defensive", "TOT")
                        + 3 * stat(p, "defensive", "TFL")
                        + 5 * stat(p, "defensive", "SACKS")
                        + 7 * stat(p, "interceptions", "INT")
                        + 2 * stat(p, "defensive", "PD")
                        + 10 * stat(p, "defensive", "TD")),
             lambda p: (f"{number(stat(p, 'defensive', 'TOT'))} tackles · "
                        f"{number(stat(p, 'defensive', 'TFL'))} TFL · "
                        f"{number(stat(p, 'defensive', 'SACKS'))} sacks · "
                        f"{number(stat(p, 'interceptions', 'INT'))} INT"),
             lambda p: (stat(p, "defensive", "TOT")
                        + 3 * stat(p, "defensive", "TFL")
                        + 5 * stat(p, "defensive", "SACKS")
                        + 7 * stat(p, "interceptions", "INT")
                        + 2 * stat(p, "defensive", "PD")) >= 35),
        ]

        selected: dict[str, dict[str, Any]] = {}
        for label, why, qualifies, score, detail, primary in candidates:
            eligible = sorted(
                (player for player in players.values() if qualifies(player)),
                key=lambda player: (-score(player), player["player"]),
            )
            if not eligible:
                continue
            for rank, player in enumerate(eligible):
                identifier = player["player_id"]
                if identifier not in selected:
                    selected[identifier] = {
                        **{key: value for key, value in player.items() if key != "stats"},
                        "roles": [], "evidence": [], "role_leader": False,
                        "primary_signal": False, "impact_sort": 0.0,
                    }
                selected[identifier]["roles"].append(label)
                selected[identifier]["evidence"].append({
                    "label": why, "detail": detail(player),
                })
                selected[identifier]["role_leader"] |= rank == 0
                selected[identifier]["primary_signal"] |= bool(primary(player))
                selected[identifier]["impact_sort"] = max(
                    selected[identifier]["impact_sort"], float(score(player)))

        for player in selected.values():
            primary_signal = player.pop("primary_signal")
            role_leader = player.pop("role_leader")
            if primary_signal or len(player["roles"]) > 1:
                player["impact_level"], player["impact_label"] = "primary", "Primary impact"
            elif role_leader:
                player["impact_level"], player["impact_label"] = "notable", "Notable impact"
            else:
                player["impact_level"], player["impact_label"] = "emerging", "Emerging impact"
        ranked = sorted(selected.values(), key=lambda player: (
            {"primary": 0, "notable": 1, "emerging": 2}[player["impact_level"]],
            -player["impact_sort"], player["player"],
        ))
        for player in ranked:
            player.pop("impact_sort", None)
        return ranked[:limit]

    def _teams_by_id(self) -> dict[int, dict[str, Any]]:
        """Every team row, read once per request.

        A matchup page asked for a team twelve times per render -- seven of them
        for the same team -- each its own connection and its own query against a
        900 MB file, for 25ms of the page. There are 138 teams and 130 KB of logo
        JSON in total, so reading the table whole costs less than asking twice.

        Held for the request and not for the life of the repository, which is
        what `team_brands` below does. A refresh runs in another process, and the
        rendered-page cache is built on noticing that: it versions its keys by
        the database's modification time so the next request rebuilds the page.
        A team row cached past the end of the request would put stale content
        into a page the cache believed it had just rebuilt.
        """
        scope = _request_scope()
        key = f"{_TEAMS_KEY}{id(self)}"
        if scope is not None:
            cached = getattr(scope, key, None)
            if cached is not None:
                return cached
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute("SELECT * FROM teams").fetchall()
        teams: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["logos"] = json.loads(item.pop("logos_json") or "[]")
            teams[int(item["team_id"])] = item
        if scope is not None:
            setattr(scope, key, teams)
        return teams

    def get_team(self, team_id: int) -> dict[str,Any] | None:
        item = self._teams_by_id().get(int(team_id))
        if item is None:
            return None
        # Callers treat what they get back as their own, and this one is shared
        # for the life of the repository.
        return {**item, "logos": list(item["logos"])}

    def team_brands(self) -> dict[int, dict[str, Any]]:
        """Team identity for display: school, conference, colors, and primary logo.

        Cached for the life of the repository because team branding changes once
        a season at most, while the pages that need it render it hundreds of
        times per request.
        """
        if self._brands is None:
            self.initialize()
            with self._reader() as connection:
                rows = connection.execute(
                    "SELECT team_id,school,abbreviation,mascot,conference,color,"
                    "alternate_color,logos_json FROM teams").fetchall()
            brands: dict[int, dict[str, Any]] = {}
            for row in rows:
                logos = json.loads(row["logos_json"] or "[]")
                light, dark = _logo_pair(logos)
                brands[row["team_id"]] = {
                    "team_id": row["team_id"], "school": row["school"],
                    "abbreviation": row["abbreviation"], "mascot": row["mascot"],
                    "conference": row["conference"],
                    "color": _hex_color(row["color"]),
                    "alternate_color": _hex_color(row["alternate_color"]),
                    "logo": light,
                    "logo_dark": dark,
                }
            self._brands = brands
        return self._brands

    def brand_for(self, team_id: int | None) -> dict[str, Any]:
        """Branding for one team, or empty values when the team is unknown."""
        if team_id is None:
            return {}
        return self.team_brands().get(team_id, {})

    def brand_by_school(self, school: str | None) -> dict[str, Any]:
        """Branding looked up by school name, for packets keyed by name."""
        if not school:
            return {}
        if self._brands_by_school is None:
            self._brands_by_school = {brand["school"]: brand
                                      for brand in self.team_brands().values()}
        return self._brands_by_school.get(school, {})

    def team_schedule(self, team_id: int, season: int) -> list[dict[str,Any]]:
        self.initialize()
        with self._reader() as connection:
            rows=connection.execute("""SELECT * FROM games WHERE season=?
              AND (home_team_id=? OR away_team_id=?) ORDER BY start_date""",
              (season,team_id,team_id)).fetchall()
        return [dict(row) for row in rows]

    def team_schedule_seasons(self, team_id: int) -> list[dict[str, int]]:
        """Stored schedule years, with remaining games kept separate from history."""
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT season,COUNT(*) games,
                          SUM(CASE WHEN completed=0 THEN 1 ELSE 0 END) upcoming
                   FROM games WHERE home_team_id=? OR away_team_id=?
                   GROUP BY season ORDER BY season DESC""",
                (team_id, team_id)).fetchall()
        return [dict(row) for row in rows]

    def team_roster(self, team: str, season: int) -> list[dict[str,Any]]:
        self.initialize()
        with self._reader() as connection:
            rows=connection.execute("""SELECT * FROM players WHERE season=? AND team=?
              ORDER BY CASE position WHEN 'QB' THEN 1 WHEN 'RB' THEN 2 WHEN 'WR' THEN 3
              WHEN 'TE' THEN 4 WHEN 'OL' THEN 5 WHEN 'DL' THEN 6 WHEN 'LB' THEN 7
              WHEN 'DB' THEN 8 ELSE 9 END,position,jersey,last_name""",(season,team)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _position_group(position: str | None) -> tuple[str, str, int]:
        value = (position or "OTHER").upper()
        groups = {
            "QB": ("Offense", "Quarterback", 1),
            "RB": ("Offense", "Backfield", 2), "FB": ("Offense", "Backfield", 2),
            "HB": ("Offense", "Backfield", 2),
            "WR": ("Offense", "Wide receiver", 3), "TE": ("Offense", "Tight end", 4),
            "OL": ("Offense", "Offensive line", 5), "OT": ("Offense", "Offensive line", 5),
            "G": ("Offense", "Offensive line", 5), "C": ("Offense", "Offensive line", 5),
            "DL": ("Defense", "Interior defensive line", 1), "DT": ("Defense", "Interior defensive line", 1),
            "NT": ("Defense", "Interior defensive line", 1), "DE": ("Defense", "Edge", 2),
            "DI": ("Defense", "Interior defensive line", 1),
            "EDGE": ("Defense", "Edge", 2), "ED": ("Defense", "Edge", 2),
            "LB": ("Defense", "Linebacker", 3),
            "CB": ("Defense", "Cornerback", 4), "S": ("Defense", "Safety", 5),
            "DB": ("Defense", "Defensive back", 6), "PK": ("Special teams", "Kicker", 1),
            "P": ("Special teams", "Punter", 2), "LS": ("Special teams", "Long snapper", 3),
        }
        return groups.get(value, ("Other", value.title(), 99))

    def roster_movements(self, team_id: int, season: int) -> dict[str, Any]:
        """Arrivals and departures for one team/season, held for the request.

        This is one of the more expensive reads here -- four queries plus the
        season-wide recruit, PFF-interest and production indexes -- and a
        single team page calls it several times over (directly, and again
        inside `team_production`, `team_depth_chart` and `pff_team_context`,
        each for the same team and season). Cached the way `recruit_index` is,
        for the same reason: correct within a request, gone at the end of it.
        """
        return _request_cached(self, f"roster_movements:{int(team_id)}:{int(season)}",
                               lambda: self._build_roster_movements(team_id, season))

    def _build_roster_movements(self, team_id: int, season: int) -> dict[str, Any]:
        # Local import: cfb.recruiting depends on this class.
        from sports_aggregator.cfb.recruiting import evidence_score

        team = self.get_team(team_id)
        if team is None:
            return {"season": season, "arrivals": [], "departures": [], "counts": {}}
        self.initialize(); previous_season = season - 1
        with self._reader() as connection:
            current = [dict(row) for row in connection.execute(
                "SELECT * FROM players WHERE season=? AND team=?", (season, team["school"])
            )]
            previous = [dict(row) for row in connection.execute(
                "SELECT * FROM players WHERE season=? AND team=?", (previous_season, team["school"])
            )]
            transfers = [dict(row) for row in connection.execute(
                """SELECT * FROM player_transfers WHERE season=? AND (origin=? OR destination=?)""",
                (season, team["school"], team["school"]),
            )]
            picks = [dict(row) for row in connection.execute(
                "SELECT * FROM draft_picks WHERE draft_year=? AND college_team=?",
                (season, team["school"]),
            )]
        recruits = self.recruit_index(season)
        interest, interest_by_id = self._pff_interest(previous_season)
        current_names = {normalize_alias(f"{row['first_name']} {row['last_name']}") for row in current}
        current_ids = {row["player_id"] for row in current}
        previous_names = {normalize_alias(f"{row['first_name']} {row['last_name']}") for row in previous}
        previous_ids = {row["player_id"] for row in previous}
        transfer_in = {row["normalized_name"]: row for row in transfers if row["destination"] == team["school"]}
        transfer_out = {row["normalized_name"]: row for row in transfers if row["origin"] == team["school"]}
        draft_by_name = {row["normalized_name"]: row for row in picks}
        draft_by_id = {row["college_athlete_id"]: row for row in picks if row["college_athlete_id"]}
        produced = self.player_production_strength(previous_season)
        arrivals = []
        for row in current:
            name = normalize_alias(f"{row['first_name']} {row['last_name']}")
            if row["player_id"] in previous_ids or name in previous_names:
                continue
            portal = transfer_in.get(name)
            # A newcomer who is not a transfer is a signee, and his recruiting
            # rating is the only prior evidence that exists for him.
            recruit = None if portal else (recruits.get(row["player_id"]) or recruits.get(name))
            if portal:
                movement_type, evidence = "TRANSFER_IN", "CFBD transfer portal"
            elif recruit:
                movement_type, evidence = "SIGNEE", "CFBD recruiting class"
            else:
                movement_type, evidence = "NEWCOMER", "Roster comparison"
            arrivals.append({
                **row, "name": f"{row['first_name']} {row['last_name']}",
                "movement_type": movement_type,
                "origin": portal["origin"] if portal else (recruit["home_state"] if recruit else None),
                "rating": portal["rating"] if portal else (recruit["rating"] if recruit else None),
                "stars": portal["stars"] if portal else (recruit["stars"] if recruit else None),
                "recruit_ranking": recruit["ranking"] if recruit else None,
                "production_strength": produced.get(row["player_id"], 0.0),
                "pff_interest": interest_by_id.get(row["player_id"], interest.get(name)),
                "evidence": evidence,
            })
        departures = []
        for row in previous:
            name = normalize_alias(f"{row['first_name']} {row['last_name']}")
            if row["player_id"] in current_ids or name in current_names:
                continue
            drafted = draft_by_id.get(row["player_id"]) or draft_by_name.get(name)
            portal = transfer_out.get(name)
            if drafted:
                movement_type, destination, evidence = "DRAFTED", drafted["nfl_team"], "CFBD NFL Draft"
            elif portal:
                movement_type, destination, evidence = "TRANSFER_OUT", portal["destination"], "CFBD transfer portal"
            elif (row.get("class_year") or 0) >= 4:
                movement_type, destination, evidence = "ELIGIBILITY_DEPARTURE", None, "Inferred from class and roster comparison"
            else:
                movement_type, destination, evidence = "ROSTER_DEPARTURE", None, "Roster comparison; reason unverified"
            departures.append({
                **row, "name": f"{row['first_name']} {row['last_name']}",
                "movement_type": movement_type, "destination": destination,
                "draft_round": drafted["round"] if drafted else None,
                "draft_pick": drafted["overall_pick"] if drafted else None,
                "interest_score": interest.get(name), "evidence": evidence,
                # A departure is measured the same way an arrival is: what he
                # produced, how he was graded, what he was rated. He was on last
                # season's roster, so the production index already holds him.
                "rating": portal["rating"] if portal else None,
                "stars": portal["stars"] if portal else None,
                "production_strength": produced.get(row["player_id"], 0.0),
                "pff_interest": interest.get(name),
            })
        # Rating, and rating alone. Both numbers are the same CFBD recruiting
        # composite on the same scale -- 4-star transfers run 0.90-0.97 against
        # 0.89-0.98 for 4-star signees, 3-stars are 0.80-0.89 in both -- so they
        # compare directly and movement type only breaks a tie.
        #
        # An earlier version ranked these on the depth board's blended evidence
        # instead, on the theory that a transfer's rating was stale and needed
        # college production to correct it. The distributions say otherwise, and
        # the blend simply buried every signee: any transfer who played scores
        # above 0.85 while the best recruit in the country reaches 0.54. When a
        # team's signees outrank its portal additions here, that is a fact about
        # the team, and the page splits the two so both are visible anyway.
        for row in departures:
            row["movement_evidence"] = evidence_score(
                pff_interest=row.get("pff_interest"),
                recruit_rating=row.get("rating"),
                production=row.get("production_strength"))
        for row in arrivals:
            row["arrival_evidence"] = evidence_score(
                pff_interest=row.get("pff_interest"),
                recruit_rating=row.get("rating"),
                production=row.get("production_strength"))
            row["movement_evidence"] = row["arrival_evidence"]
        arrivals.sort(key=lambda row: (
            -(row["rating"] or 0),
            {"TRANSFER_IN": 0, "SIGNEE": 1}.get(row["movement_type"], 2),
            row["name"]))
        departures.sort(key=lambda row: (
            {"DRAFTED": 0, "TRANSFER_OUT": 1, "ELIGIBILITY_DEPARTURE": 2}.get(row["movement_type"], 3),
            -(row["interest_score"] or 0), row["name"],
        ))
        counts: dict[str, int] = {}
        for row in arrivals + departures:
            counts[row["movement_type"]] = counts.get(row["movement_type"], 0) + 1
        return {"season": season, "previous_season": previous_season,
                "arrivals": arrivals, "departures": departures, "counts": counts}

    def production_distribution(self, season: int) -> dict[str, list[float]]:
        """Sorted headline values per category, for ranking within a category.

        The categories are not comparable to one another: median passing yards
        is 185 against a 99th percentile of 3,711, while a defensive tackle
        count runs 8 to 96. Comparing raw values across them is meaningless, so
        a player is placed against others doing the same job.

        Cached per season on the instance; the underlying rows only change when
        a refresh runs.
        """
        cached = self._production_distributions.get(season)
        if cached is not None:
            return cached
        self.initialize()
        wanted = {category: sort_stat(category) for category in CATEGORY_ORDER}
        wanted = {category: stat for category, stat in wanted.items() if stat}
        distribution: dict[str, list[float]] = {category: [] for category in wanted}
        with self._reader() as connection:
            for category, stat in wanted.items():
                distribution[category] = [
                    float(row[0]) for row in connection.execute(
                        """SELECT numeric_value FROM player_season_stats
                           WHERE season=? AND category=? AND stat_type=?
                             AND numeric_value > 0
                           ORDER BY numeric_value""", (season, category, stat))]
        self._production_distributions[season] = distribution
        return distribution

    def production_strength(self, category: str, value: float | None,
                            season: int) -> float:
        """Where a value sits among everyone else doing the same job, 0-1."""
        if not value or value <= 0:
            return 0.0
        values = self.production_distribution(season).get(category) or []
        if not values:
            return 0.0
        return round(bisect_left(values, float(value)) / len(values), 4)

    def player_production_strength(self, stat_season: int) -> dict[str, float]:
        """Every player's best production percentile that season, by player id.

        Deliberately league-wide rather than per team: an incoming transfer
        produced somewhere else, and filtering by the current roster's school
        would find nothing for exactly the players a depth board most needs to
        place. Cached per season for the same reason team brands are — the
        answer is identical for every team.

        Read directly rather than through `projected_depth`, which recomputes
        every team total and is already called separately by the page.
        """
        cached = self._production_strengths.get(stat_season)
        if cached is not None:
            return cached
        self.initialize()
        wanted = {category: sort_stat(category) for category in CATEGORY_ORDER}
        pairs = [(category, stat) for category, stat in wanted.items() if stat]
        if not pairs:
            return {}
        clauses = " OR ".join("(category=? AND stat_type=?)" for _ in pairs)
        params: list[Any] = [stat_season]
        for category, stat in pairs:
            params.extend((category, stat))
        with self._reader() as connection:
            rows = connection.execute(
                f"""SELECT player_id, category, MAX(numeric_value) value
                    FROM player_season_stats
                    WHERE season=? AND ({clauses}) AND numeric_value > 0
                    GROUP BY player_id, category""", params).fetchall()
        best: dict[str, float] = {}
        for row in rows:
            if not row["player_id"]:
                continue
            strength = self.production_strength(row["category"], row["value"], stat_season)
            if strength > best.get(row["player_id"], 0.0):
                best[row["player_id"]] = strength
        self._production_strengths[stat_season] = best
        return best

    def team_depth_chart(self, team_id: int, season: int, *,
                         movements: dict[str, Any] | None = None) -> dict[str, Any]:
        # Imported here rather than at module scope: cfb.recruiting depends on
        # this class, so a top-level import would be circular.
        from sports_aggregator.cfb.recruiting import evidence_score

        team = self.get_team(team_id)
        if team is None:
            return {"season": season, "units": {}, "summary": {}}
        roster = self.team_roster(team["school"], season)
        produced = self.player_production_strength(season - 1)
        if movements is None:
            movements = self.roster_movements(team_id, season)
        arrival_by_id = {row["player_id"]: row for row in movements["arrivals"]}
        self.initialize()
        with self._reader() as connection:
            previous_ids = {row[0] for row in connection.execute(
                "SELECT player_id FROM players WHERE season=? AND team=?", (season - 1, team["school"])
            )}
            # Grades are looked up by player identity, not by last season's team.
            # Filtering on cfbd_team_id gave every incoming transfer a blank grade
            # and sorted proven starters to the bottom of their position group.
            roster_ids = [row["player_id"] for row in roster]
            placeholders = ",".join("?" for _ in roster_ids) or "NULL"
            pff_rows = connection.execute(
                f"""SELECT cfbd_player_id,normalized_name,interest_score,cfbd_team
                    FROM pff_players
                    WHERE season=? AND interest_score IS NOT NULL
                    AND (cfbd_team_id=? OR cfbd_player_id IN ({placeholders}))""",
                (season - 1, team_id, *roster_ids),
            ).fetchall()
        pff_by_id = {row["cfbd_player_id"]: row["interest_score"] for row in pff_rows if row["cfbd_player_id"]}
        pff_by_name = {row["normalized_name"]: row["interest_score"] for row in pff_rows}
        graded_at = {row["cfbd_player_id"]: row["cfbd_team"] for row in pff_rows
                     if row["cfbd_player_id"]}
        unit_rows: dict[str, dict[str, list[dict[str, Any]]]] = {}
        returning = upperclassmen = 0
        for row in roster:
            name = normalize_alias(f"{row['first_name']} {row['last_name']}")
            is_returner = row["player_id"] in previous_ids
            returning += int(is_returner); upperclassmen += int((row.get("class_year") or 0) >= 3)
            arrival = arrival_by_id.get(row["player_id"])
            item = {**row, "name": f"{row['first_name']} {row['last_name']}",
                    "is_returner": is_returner,
                    "arrival_type": arrival["movement_type"] if arrival else None,
                    "origin": arrival.get("origin") if arrival else None,
                    "recruit_rating": arrival.get("rating") if arrival else None,
                    "recruit_stars": arrival.get("stars") if arrival else None,
                    "pff_graded_at": graded_at.get(row["player_id"]),
                    "production_strength": produced.get(row["player_id"], 0.0),
                    "pff_interest": pff_by_id.get(row["player_id"], pff_by_name.get(name))}
            unit, group, order = self._position_group(row.get("position"))
            item["group_order"] = order
            unit_rows.setdefault(unit, {}).setdefault(group, []).append(item)
        for groups in unit_rows.values():
            for players in groups.values():
                # Rank on the strongest prior evidence a player has, whichever
                # kind it is: what he produced, how he was graded, or what he
                # was rated. Grade alone put a five-star fourth on his own depth
                # chart behind three backups in the bottom third of graded
                # players. Grade plus rating, without production, then put a
                # three-star signee above a back who ran for 788 yards, because
                # an ungraded player scored zero however much he had done. See
                # cfb.recruiting for how the three are made comparable.
                players.sort(key=lambda row: (
                    -evidence_score(pff_interest=row.get("pff_interest"),
                                    recruit_rating=row.get("recruit_rating"),
                                    production=row.get("production_strength")),
                    not row["is_returner"],
                    -(row.get("class_year") or 0), row.get("jersey") or 999, row["name"],
                ))
        ordered_units = {
            unit: dict(sorted(groups.items(), key=lambda pair: pair[1][0]["group_order"]))
            for unit, groups in unit_rows.items()
        }
        total = len(roster)
        return {"season": season, "units": ordered_units, "movements": movements,
                "summary": {"players": total, "returners": returning,
                            "continuity_pct": round(100 * returning / total, 1) if total else None,
                            "upperclassmen": upperclassmen,
                            "upperclassmen_pct": round(100 * upperclassmen / total, 1) if total else None,
                            "transfer_arrivals": movements["counts"].get("TRANSFER_IN", 0)}}

    def team_quality_snapshot(self, team_id: int, season: int, *,
                              movements: dict[str, Any] | None = None) -> dict[str, Any]:
        team = self.get_team(team_id)
        if team is None:
            return {"state": "UNAVAILABLE", "cards": []}
        metrics = self.team_metrics(team["school"], season)
        # Everything below builds the PRESEASON context cards, which a team
        # with live in-season metrics never renders -- so the depth chart and
        # PFF/returning-production lookups those cards need are worth running
        # only once it's clear this team is still in that state.
        if metrics["advanced"] or metrics["core"]:
            return {"state": "LIVE", "season": season, "cards": [], "metrics": metrics}
        depth = self.team_depth_chart(team_id, season, movements=movements)
        with self._reader() as connection:
            returning = connection.execute(
                "SELECT * FROM returning_production WHERE season=? AND team=?",
                (season, team["school"]),
            ).fetchone()
            proven = connection.execute(
                """SELECT COUNT(*) FROM pff_players p JOIN players r
                   ON r.season=? AND r.player_id=p.cfbd_player_id
                   WHERE p.season=? AND p.cfbd_team_id=? AND p.interest_score IS NOT NULL""",
                (season, season - 1, team_id),
            ).fetchone()[0]
        row = dict(returning) if returning else {}
        returning_value, returning_source = _returning_production_signal(row, bool(returning))
        # Formats name the scale explicitly: "rate" is a 0-1 fraction, "pct" is
        # already 0-100. Leaving both as "percent" forced callers to guess, and
        # a 45.9% continuity figure rendered as 4590%.
        cards = [
            {"label": "Returning production", "value": returning_value, "format": "rate",
             "source": returning_source},
            {"label": "Roster continuity", "value": depth["summary"].get("continuity_pct"), "format": "pct",
             "source": f"{season - 1} to {season} roster IDs"},
            {"label": "Transfer arrivals", "value": depth["summary"].get("transfer_arrivals"), "format": "int",
             "source": "CFBD transfer portal"},
            {"label": "Proven PFF players", "value": proven, "format": "int",
             "source": f"{season - 1} PFF snapshot on current roster"},
        ]
        return {"state": "PRESEASON", "season": season, "cards": cards,
                "message": "Live team-quality metrics will replace these context signals after games are played."}

    def get_player(self, player_id: str, season: int) -> dict[str, Any] | None:
        self.initialize()
        with self._reader() as connection:
            row = connection.execute(
                """SELECT p.*,t.team_id,t.conference,t.color,t.alternate_color,t.logos_json
                   FROM players p LEFT JOIN teams t ON t.school=p.team
                   WHERE p.player_id=? AND p.season<=? ORDER BY p.season DESC LIMIT 1""",
                (player_id, season),
            ).fetchone()
            if row is None:
                return None
            player = dict(row); player["name"] = f"{player['first_name']} {player['last_name']}"
            player["logos"] = json.loads(player.pop("logos_json") or "[]")
            player["stints"] = [dict(item) for item in connection.execute(
                "SELECT season,team,position,jersey,class_year FROM players WHERE player_id=? ORDER BY season DESC",
                (player_id,),
            )]
            player["stats"] = [dict(item) for item in connection.execute(
                """SELECT * FROM player_season_stats WHERE player_id=?
                   ORDER BY season DESC,category,stat_type""", (player_id,)
            )]
            normalized = normalize_alias(player["name"])
            player["pff"] = [dict(item) for item in connection.execute(
                """SELECT p.*,m.dataset,m.primary_grade,m.usage_count,m.game_count games,
                   m.metrics_json,'REGULAR_SEASON' context,0 supplemental
                   FROM pff_players p LEFT JOIN pff_player_metrics m
                   ON m.season=p.season AND m.pff_player_id=p.pff_player_id
                   WHERE p.cfbd_player_id=? OR (p.normalized_name=? AND p.cfbd_team=?)
                   ORDER BY p.season DESC,m.primary_grade DESC""",
                (player_id, normalized, player["team"]),
            )]
            player["pff_supplemental"] = [dict(item) for item in connection.execute(
                """SELECT s.*,s.game_count games,1 supplemental
                   FROM pff_supplemental_metrics s JOIN pff_players p
                   ON p.season=s.season AND p.pff_player_id=s.pff_player_id
                   WHERE p.cfbd_player_id=? OR (p.normalized_name=? AND p.cfbd_team=?)
                   ORDER BY s.season DESC,s.context,s.dataset""",
                (player_id, normalized, player["team"]),
            )]
            player["transfers"] = [dict(item) for item in connection.execute(
                "SELECT * FROM player_transfers WHERE normalized_name=? ORDER BY season DESC",
                (normalized,),
            )]
            player["draft"] = [dict(item) for item in connection.execute(
                """SELECT * FROM draft_picks WHERE college_athlete_id=? OR normalized_name=?
                   ORDER BY draft_year DESC""", (player_id, normalized)
            )]
        return player

    def recent_movements(self, season: int, limit: int = 24) -> list[dict[str, Any]]:
        self.initialize()
        with self._reader() as connection:
            transfers = [dict(row) for row in connection.execute(
                """SELECT 'TRANSFER' event_type,normalized_name,first_name||' '||last_name player_name,
                   position,origin,destination,transfer_date event_date,rating,stars,NULL nfl_team,
                   NULL round,NULL overall_pick FROM player_transfers WHERE season=?
                   ORDER BY transfer_date DESC,rating DESC LIMIT ?""", (season, limit)
            )]
            picks = [dict(row) for row in connection.execute(
                """SELECT 'DRAFTED' event_type,normalized_name,player_name,position,
                   college_team origin,NULL destination,NULL event_date,NULL rating,NULL stars,
                   nfl_team,round,overall_pick FROM draft_picks WHERE draft_year=?
                   ORDER BY overall_pick LIMIT ?""", (season, limit)
            )]
        transfer_slots = (limit + 1) // 2
        return transfers[:transfer_slots] + picks[: max(0, limit - transfer_slots)]

    def team_metrics(self, team: str, season: int) -> dict[str,Any]:
        self.initialize()
        with self._reader() as connection:
            record=connection.execute("SELECT * FROM team_records WHERE season=? AND team=?",(season,team)).fetchone()
            advanced=connection.execute("SELECT * FROM team_advanced_stats WHERE season=? AND team=?",(season,team)).fetchone()
            core=connection.execute("""SELECT * FROM core_ratings WHERE season=? AND team=?
              ORDER BY through_week DESC LIMIT 1""",(season,team)).fetchone()
            stats=[dict(row) for row in connection.execute("SELECT stat_name,stat_value FROM team_stats WHERE season=? AND team=? ORDER BY stat_name",(season,team))]
            score=connection.execute("""SELECT COUNT(*) games,
              SUM(CASE WHEN home_team=? THEN home_points ELSE away_points END) points_for,
              SUM(CASE WHEN home_team=? THEN away_points ELSE home_points END) points_against
              FROM games WHERE season=? AND completed=1
              AND home_points IS NOT NULL AND away_points IS NOT NULL
              AND (home_team=? OR away_team=?)""",
              (team,team,season,team,team)).fetchone()
        return {"record":dict(record) if record else None,"advanced":dict(advanced) if advanced else None,
                "core":dict(core) if core else None,"stats":stats,
                "score":dict(score) if score else {"games":0,"points_for":None,"points_against":None}}

    def advanced_metric_ranks(self, season: int) -> dict[str, dict[str, dict[str, Any]]]:
        """National FBS rank and performance percentile for advanced metrics.

        The source query is already restricted to FBS. Percentiles are always
        oriented as performance percentiles (100 is best), even for measures
        such as defensive success rate and offensive havoc allowed where a
        lower raw value is favorable.
        """
        directions = {
            "offense_success_rate": True, "defense_success_rate": False,
            "offense_explosiveness": True, "defense_explosiveness": False,
            "offense_ppa": True, "defense_ppa": False,
            "offense_points_per_opportunity": True,
            "defense_points_per_opportunity": False,
            "offense_havoc": False, "defense_havoc": True,
        }
        self.initialize()
        with self._reader() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM team_advanced_stats WHERE season=? ORDER BY team",
                (season,))]

        result: dict[str, dict[str, dict[str, Any]]] = {
            row["team"]: {} for row in rows}
        for metric, higher_is_better in directions.items():
            values = [float(row[metric]) for row in rows if row.get(metric) is not None]
            values.sort(reverse=higher_is_better)
            total = len(values)
            if not total:
                continue
            rank_by_value = {value: 1 + sum(
                other > value if higher_is_better else other < value
                for other in values) for value in set(values)}
            for row in rows:
                if row.get(metric) is None:
                    continue
                rank = rank_by_value[float(row[metric])]
                percentile = (100.0 if total == 1 else
                              100.0 * (total - rank) / (total - 1))
                result[row["team"]][metric] = {
                    "rank": rank, "of": total, "percentile": round(percentile),
                }
        return result

    def opponent_quality(self, team_id: int, season: int) -> dict[str, Any]:
        """Cached schedule quality using contemporaneous and latest model ratings.

        Pregame Elo is the cleanest historical measure because it describes the
        opponent at kickoff. Latest Elo and CORE are retained as separate lenses;
        their scales are intentionally not averaged together.
        """
        self.initialize()
        elo = self.team_elo(season)
        with self._reader() as connection:
            games = [dict(row) for row in connection.execute(
                """SELECT * FROM games WHERE season=? AND completed=1
                   AND home_points IS NOT NULL AND away_points IS NOT NULL
                   AND (home_team_id=? OR away_team_id=?) ORDER BY start_date""",
                (season, team_id, team_id))]
            core_rows = [dict(row) for row in connection.execute(
                """SELECT c.* FROM core_ratings c JOIN (
                     SELECT team,MAX(through_week) through_week FROM core_ratings
                     WHERE season=? GROUP BY team
                   ) latest ON latest.team=c.team AND latest.through_week=c.through_week
                   WHERE c.season=? ORDER BY c.overall DESC""", (season, season))]
            poll_rows = [dict(row) for row in connection.execute(
                """SELECT r.school,r.rank FROM rankings r JOIN (
                     SELECT school,MAX(week) week FROM rankings
                     WHERE season=? AND poll='AP Top 25' GROUP BY school
                   ) latest ON latest.school=r.school AND latest.week=r.week
                   WHERE r.season=? AND r.poll='AP Top 25'""", (season, season))]
        core = {row["team"]: {**row, "rank": rank}
                for rank, row in enumerate(core_rows, start=1)}
        poll = {row["school"]: row["rank"] for row in poll_rows}
        opponents = []
        for game in games:
            home = game["home_team_id"] == team_id
            opponent_id = game["away_team_id"] if home else game["home_team_id"]
            opponent = game["away_team"] if home else game["home_team"]
            pregame_elo = game["away_pregame_elo"] if home else game["home_pregame_elo"]
            opponents.append({
                "opponent_id": opponent_id, "opponent": opponent,
                "pregame_elo": pregame_elo,
                "elo": (elo.get(opponent_id) or {}).get("elo"),
                "elo_rank": (elo.get(opponent_id) or {}).get("elo_rank"),
                "core": (core.get(opponent) or {}).get("overall"),
                "core_rank": (core.get(opponent) or {}).get("rank"),
                "poll_rank": poll.get(opponent),
            })
        def average(key: str) -> float | None:
            values = [float(row[key]) for row in opponents if row.get(key) is not None]
            return sum(values) / len(values) if values else None
        return {
            "season": season, "games": len(opponents), "opponents": opponents,
            "average_pregame_elo": average("pregame_elo"),
            "average_latest_elo": average("elo"),
            "average_elo_rank": average("elo_rank"),
            "average_core": average("core"),
            "average_core_rank": average("core_rank"),
            "elo_top_25": sum(1 for row in opponents if row.get("elo_rank") and row["elo_rank"] <= 25),
            "poll_ranked": sum(1 for row in opponents if row.get("poll_rank")),
        }

    def latest_pff_season(self, default: int = 2025) -> int:
        """Newest season with any imported PFF grade rows.

        PFF grades are a manual, licensed CSV upload (`/college-football/
        data-import/pff`), not a live sync, so there is no "current season"
        to derive from the calendar the way CFBD stats can be. A team page
        should show whichever season was actually uploaded most recently,
        so re-uploading a fresh season's export is enough to move every PFF
        display over -- no code change needed each year.
        """
        self.initialize()
        with self._reader() as connection:
            row = connection.execute(
                "SELECT MAX(season) FROM pff_player_metrics").fetchone()
        return int(row[0]) if row and row[0] is not None else default

    def pff_team_context(self, team_id: int, season: int=2025, player_limit: int=12) -> dict[str,Any]:
        self.initialize()
        with self._reader() as connection:
            players=[dict(row) for row in connection.execute("""SELECT p.*,
              GROUP_CONCAT(m.dataset||':'||ROUND(m.primary_grade,1)) grades
              FROM pff_players p LEFT JOIN pff_player_metrics m
              ON m.season=p.season AND m.pff_player_id=p.pff_player_id
              WHERE p.season=? AND p.cfbd_team_id=? AND p.interest_score IS NOT NULL
              GROUP BY p.season,p.pff_player_id ORDER BY p.interest_score DESC LIMIT ?""",
              (season,team_id,player_limit)).fetchall()]
            groups=[dict(row) for row in connection.execute("""SELECT * FROM pff_position_groups
              WHERE season=? AND cfbd_team_id=? AND weighted_grade IS NOT NULL
              AND player_count>=2 ORDER BY weighted_grade DESC""",(season,team_id)).fetchall()]
        movements = self.roster_movements(team_id, season + 1)
        departed = {normalize_alias(row["name"]): row for row in movements["departures"]}
        for player in players:
            movement = departed.get(player["normalized_name"])
            if movement:
                player["roster_status"] = movement["movement_type"]
                player["roster_destination"] = movement["destination"]
                player["player_page_id"] = movement["player_id"]
            elif player.get("cfbd_player_id"):
                player["roster_status"] = "RETURNING"
                player["roster_destination"] = None
                player["player_page_id"] = player["cfbd_player_id"]
            else:
                player["roster_status"] = "UNRESOLVED"
                player["roster_destination"] = None
                player["player_page_id"] = None
        return {"season":season,"players":players,"position_groups":groups}

    def conference_pff_players(self, conference: str, season: int=2025,
                               roster_season: int | None = None,
                               limit: int=20) -> list[dict[str,Any]]:
        """Prior-season standouts who are actually on a current conference roster.

        Joining through the current roster is deliberate: a conference page is a
        current-season discovery view, not a departures ledger. Drafted,
        transferred-out, graduated, and otherwise absent players belong on their
        former team's departures table instead.
        """
        self.initialize()
        current_season = roster_season or season + 1
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT p.*,r.team current_team,r.position current_position,
                   CASE WHEN r.team<>p.cfbd_team THEN 'TRANSFER_ARRIVAL'
                        ELSE 'RETURNING' END roster_status,
                   CASE WHEN r.team<>p.cfbd_team THEN 'from '||p.cfbd_team
                        ELSE NULL END roster_destination,
                   r.player_id player_page_id
                   FROM pff_players p JOIN players r
                   ON r.player_id=p.cfbd_player_id AND r.season=?
                   JOIN teams t ON t.school=r.team
                   WHERE p.season=? AND t.conference=?
                   AND p.interest_score IS NOT NULL
                   ORDER BY p.interest_score DESC LIMIT ?""",
                (current_season,season,conference,limit),
            ).fetchall()
        players = [dict(row) for row in rows]
        for player in players:
            player["cfbd_team"] = player["current_team"]
            player["position"] = player["current_position"] or player["position"]
        return players

    #: (label, position_group, dataset) -- one representative dataset per unit
    #: label, since a position_group can otherwise appear under more than one
    #: dataset (SECONDARY grades both under "coverage" and "defense").
    PFF_UNIT_SPECS = (("Pass protection","OL","blocking"),("Pass rush","EDGE","pass_rush"),
                      ("Interior rush","INTERIOR_DL","pass_rush"),("Coverage","SECONDARY","coverage"),
                      ("Rushing","RB","rushing"),("Receiving","WR","receiving"))

    def pff_game_units(self, home_team_id: int, away_team_id: int, season: int=2025) -> list[dict[str,Any]]:
        self.initialize()
        with self._reader() as connection:
            rows=[dict(row) for row in connection.execute("""SELECT * FROM pff_position_groups
              WHERE season=? AND cfbd_team_id IN (?,?) AND weighted_grade IS NOT NULL""",
              (season,home_team_id,away_team_id)).fetchall()]
        by_key={(row["cfbd_team_id"],row["position_group"],row["dataset"]):row for row in rows}
        result=[]
        for label,group,dataset in self.PFF_UNIT_SPECS:
            home=by_key.get((home_team_id,group,dataset)); away=by_key.get((away_team_id,group,dataset))
            if home or away: result.append({"label":label,"position_group":group,"dataset":dataset,
                "home_grade":home["weighted_grade"] if home else None,
                "away_grade":away["weighted_grade"] if away else None,
                "home_usage":home["usage_count"] if home else None,"away_usage":away["usage_count"] if away else None})
        return result

    def pff_team_units(self, team_id: int, season: int) -> list[dict[str, Any]]:
        """One representative PFF grade per unit for a single team, ranked best-first.

        Same (label, position_group, dataset) picks as `pff_game_units`, just
        for one side instead of a home/away pair -- a team page's ranked bar
        chart of "which units grade well" rather than a two-team comparison.
        """
        self.initialize()
        with self._reader() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT * FROM pff_position_groups
                   WHERE season=? AND cfbd_team_id=? AND weighted_grade IS NOT NULL""",
                (season, team_id)).fetchall()]
        by_key = {(row["position_group"], row["dataset"]): row for row in rows}
        result = []
        for label, group, dataset in self.PFF_UNIT_SPECS:
            row = by_key.get((group, dataset))
            if row:
                result.append({"label": label, "grade": row["weighted_grade"],
                               "player_count": row["player_count"], "usage": row["usage_count"]})
        result.sort(key=lambda item: -item["grade"])
        return result

    def pff_matchup_rows(self, team_ids: Iterable[int],
                         season: int = 2025) -> dict[int, list[dict[str, Any]]]:
        """Grade rows for many teams in one round trip, keyed by team.

        `pff_matchups` reads exactly two teams, which is fine for a matchup page
        and wasteful for the dashboard: the weekly slate calls it once per game,
        so twenty games meant forty queries fetching two teams each. Fetching
        the whole slate at once turns that into two.
        """
        wanted = sorted({int(team_id) for team_id in team_ids if team_id})
        if not wanted:
            return {}
        self.initialize()
        placeholders = ",".join("?" for _ in wanted)
        grouped: dict[int, list[dict[str, Any]]] = {team_id: [] for team_id in wanted}
        with self._reader() as connection:
            statements = (
                f"""SELECT p.cfbd_team_id,p.position,m.dataset,m.primary_grade,
                    m.usage_count,m.metrics_json FROM pff_players p
                    JOIN pff_player_metrics m ON m.season=p.season
                    AND m.pff_player_id=p.pff_player_id
                    WHERE p.season=? AND p.cfbd_team_id IN ({placeholders})""",
                f"""SELECT p.cfbd_team_id,p.position,m.dataset,m.primary_grade,
                    m.usage_count,m.metrics_json FROM pff_players p
                    JOIN pff_supplemental_metrics m ON m.season=p.season
                    AND m.pff_player_id=p.pff_player_id
                    WHERE p.season=? AND p.cfbd_team_id IN ({placeholders})
                    AND m.dataset='run_defense_detail'""",
            )
            for statement in statements:
                for row in connection.execute(statement, (season, *wanted)):
                    item = dict(row)
                    grouped.setdefault(item["cfbd_team_id"], []).append(item)
        return grouped

    def pff_matchups(self, home_team_id: int, away_team_id: int,
                     season: int = 2025, *,
                     prefetched: dict[int, list[dict[str, Any]]] | None = None,
                     ) -> list[dict[str, Any]]:
        """Build directional offense-vs-defense comparisons from licensed PFF rows.

        `prefetched` lets a caller comparing many games load every team's rows
        once; without it this fetches the two teams it needs.
        """
        if prefetched is not None:
            rows = [*prefetched.get(home_team_id, ()), *prefetched.get(away_team_id, ())]
        else:
            grouped = self.pff_matchup_rows((home_team_id, away_team_id), season)
            rows = [*grouped.get(home_team_id, ()), *grouped.get(away_team_id, ())]

        def grade(team_id: int, dataset: str, groups: set[str],
                  raw_field: str | None = None) -> dict[str, Any] | None:
            values: list[tuple[float, float]] = []
            for row in rows:
                if row["cfbd_team_id"] != team_id or row["dataset"] != dataset:
                    continue
                _, group, _ = self._position_group(row["position"])
                if group not in groups:
                    continue
                value = row["primary_grade"]
                if raw_field:
                    try:
                        value = _numeric(json.loads(row["metrics_json"] or "{}").get(raw_field))
                    except (TypeError, json.JSONDecodeError):
                        value = None
                if value is not None:
                    values.append((float(value), float(row["usage_count"] or 1)))
            if not values:
                return None
            usage = sum(weight for _, weight in values)
            weighted = sum(value * weight for value, weight in values) / usage
            return {"grade": round(weighted, 1), "players": len(values), "usage": round(usage, 1)}

        specs = (
            ("Rushing", "Rush offense", "rushing", {"Backfield"}, None,
             "Run defense", "run_defense_detail", {"Interior defensive line", "Edge", "Linebacker"}, None),
            ("Run blocking", "Run-block grade", "blocking", {"Offensive line", "Tight end"}, "grades_run_block",
             "Run defense", "run_defense_detail", {"Interior defensive line", "Edge", "Linebacker"}, None),
            ("Pass protection", "Pass-block grade", "blocking", {"Offensive line"}, "grades_pass_block",
             "Pass rush", "pass_rush", {"Interior defensive line", "Edge", "Linebacker"}, None),
            ("Passing", "Pass grade", "passing", {"Quarterback"}, None,
             "Coverage", "coverage", {"Defensive back", "Cornerback", "Safety", "Linebacker"}, None),
            ("Receiving", "Receiving grade", "receiving", {"Wide receiver", "Tight end", "Backfield"}, None,
             "Coverage", "coverage", {"Defensive back", "Cornerback", "Safety", "Linebacker"}, None),
        )

        def direction(attack_team: int, defend_team: int, spec: tuple) -> dict[str, Any]:
            attack = grade(attack_team, spec[2], spec[3], spec[4])
            defense = grade(defend_team, spec[6], spec[7], spec[8])
            edge = None
            if attack and defense:
                difference = round(attack["grade"] - defense["grade"], 1)
                if abs(difference) < 2.5:
                    edge = {"side": "EVEN", "margin": abs(difference)}
                else:
                    edge = {"side": "OFFENSE" if difference > 0 else "DEFENSE",
                            "margin": abs(difference)}
            return {"attack_label": spec[1], "attack": attack,
                    "counter_label": spec[5], "counter": defense, "edge": edge}

        result = []
        for spec in specs:
            away_direction = direction(away_team_id, home_team_id, spec)
            home_direction = direction(home_team_id, away_team_id, spec)
            if away_direction["attack"] or away_direction["counter"] or home_direction["attack"] or home_direction["counter"]:
                result.append({"label": spec[0], "away_attacks": away_direction,
                               "home_attacks": home_direction})
        return result

    def latest_rankings(self, season: int) -> dict[str, Any]:
        self.initialize()
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM rankings WHERE season = ? ORDER BY week DESC, poll, rank",
                (season,),
            ).fetchall()
        if not rows:
            return {"week": None, "poll": None, "teams": []}
        latest_week = max(row["week"] for row in rows)
        latest = [row for row in rows if row["week"] == latest_week]
        polls = {row["poll"] for row in latest}

        def poll_priority(name: str) -> tuple[int, str]:
            lower = name.casefold()
            if "playoff" in lower or "cfp" in lower or "committee" in lower:
                return (0, name)
            if lower.startswith("ap") or "associated press" in lower:
                return (1, name)
            if "coach" in lower:
                return (2, name)
            return (3, name)

        selected = sorted(polls, key=poll_priority)[0]
        teams = [dict(row) for row in latest if row["poll"] == selected]
        return {"week": latest_week, "poll": selected, "teams": teams}

    def scoreboard_days(self, season: int, *, timezone_name: str = "America/New_York",
                        ) -> list[dict[str, Any]]:
        """Every day that has a game, in the reader's timezone.

        Grouped locally rather than by the stored UTC date: a 7pm Eastern
        kickoff is stored as the following day in UTC, and 64 of the first 400
        games of this season fall on a different calendar day under the two
        readings. Grouping by the stored string would file a sixth of the
        schedule under the wrong date.
        """
        self.initialize()
        zone = ZoneInfo(timezone_name)
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT start_date, completed FROM games WHERE season=?", (season,)
            ).fetchall()
        days: dict[str, dict[str, Any]] = {}
        for row in rows:
            local = _to_zone(row["start_date"], zone)
            if local is None:
                continue
            key = local.date().isoformat()
            entry = days.setdefault(key, {"date": key, "games": 0, "completed": 0})
            entry["games"] += 1
            entry["completed"] += int(row["completed"] or 0)
        return [days[key] for key in sorted(days)]

    def games_on_day(self, day: str, season: int, *,
                     timezone_name: str = "America/New_York") -> list[dict[str, Any]]:
        """Every game kicking off on one local calendar day, in start order.

        A UTC window either side of the day is read and then filtered locally,
        because the stored timestamps are UTC and the day being asked for is not.
        """
        self.initialize()
        zone = ZoneInfo(timezone_name)
        try:
            wanted = date.fromisoformat(day)
        except ValueError:
            return []
        window_start = (datetime.combine(wanted, dtime.min, tzinfo=zone)
                        - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        window_end = (datetime.combine(wanted, dtime.max, tzinfo=zone)
                      + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        with self._reader() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT * FROM games
                   WHERE season=? AND start_date >= ? AND start_date <= ?
                   ORDER BY start_date""", (season, window_start, window_end))]
        return [row for row in rows
                if (local := _to_zone(row["start_date"], zone)) is not None
                and local.date() == wanted]

    def upcoming_games(self, season: int, limit: int = 16) -> list[dict[str, Any]]:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT * FROM games
                WHERE season = ? AND completed = 0 AND start_date >= ?
                ORDER BY start_date LIMIT ?
                """,
                (season, now, limit),
            ).fetchall()
        rankings = self.latest_rankings(season)["teams"]
        rank_by_school = {row["school"]: row["rank"] for row in rankings}
        results = []
        for row in rows:
            item = dict(row)
            item["home_rank"] = rank_by_school.get(item["home_team"])
            item["away_rank"] = rank_by_school.get(item["away_team"])
            results.append(item)
        return results

    def get_game(self, game_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self._reader() as connection:
            row = connection.execute("SELECT * FROM games WHERE game_id = ?", (game_id,)).fetchone()
            if row is None:
                return None
            game = dict(row)
            season = game["season"]
            record_rows = connection.execute(
                "SELECT * FROM team_records WHERE season = ? AND team_id IN (?, ?)",
                (season, game["home_team_id"], game["away_team_id"]),
            ).fetchall()
            metric_rows = connection.execute(
                "SELECT * FROM team_advanced_stats WHERE season = ? AND team IN (?, ?)",
                (season, game["home_team"], game["away_team"]),
            ).fetchall()
        game["records"] = {row["team"]: dict(row) for row in record_rows}
        game["advanced_metrics"] = {row["team"]: dict(row) for row in metric_rows}
        rankings = self.latest_rankings(game["season"])["teams"]
        game["rankings"] = {row["school"]: row["rank"] for row in rankings}
        game["momentum"] = {
            game["home_team"]: self._team_momentum(game["home_team_id"], season),
            game["away_team"]: self._team_momentum(game["away_team_id"], season),
        }
        return game

    def _team_momentum(self, team_id: int, season: int) -> str | None:
        """"up"/"down" if this team's Elo moved between its last two games this
        season, else None (fewer than two rated games, or no net change)."""
        history = self.team_elo_history(team_id, season)
        if len(history) < 2:
            return None
        last, previous = history[-1]["elo"], history[-2]["elo"]
        if last > previous:
            return "up"
        if last < previous:
            return "down"
        return None
