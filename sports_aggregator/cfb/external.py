"""Storage for secondary structured sources, with provenance on every row.

CFBD owns the canonical teams, games, players and season statistics. Everything
here is *secondary*: it adds information CFBD does not carry, and it is stored in
its own tables keyed to CFBD identities rather than merged into them.

Three rules hold for every table in this module:

* **No entity creation.** A row is stored only when its game and team already
  resolve to canonical CFBD entities. ESPN game and team identifiers happen to
  match CFBD's, which was verified before relying on it, but unresolved rows are
  counted and reported rather than inserted as new teams.
* **Models stay separate.** FPI is not blended into CORE, SP+ or any internal
  rating. Comparing models is the point; averaging them destroys it.
* **Provenance travels with the data.** Every row records the source, the exact
  release asset it came from, and when it was imported, so a number on a page can
  always be traced back to a file.

`import_runs` records every attempt, successful or not, so status reporting can
show freshness and failures without inspecting the tables themselves.
"""

from __future__ import annotations

from contextlib import closing
import json
from typing import Any, Iterable

from sports_aggregator.cfb.repository import CFBRepository, schema_once
from sports_aggregator.providers.sportsdataverse import optional_float, utc_now
from sports_aggregator.providers.weather import weather_condition, weather_emoji


EXTERNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_runs (
 import_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source TEXT NOT NULL, dataset TEXT NOT NULL, season INTEGER,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL,
 rows_seen INTEGER NOT NULL DEFAULT 0, rows_stored INTEGER NOT NULL DEFAULT 0,
 rows_skipped INTEGER NOT NULL DEFAULT 0,
 asset TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_import_runs_recent
  ON import_runs(source,dataset,finished_at DESC);

CREATE TABLE IF NOT EXISTS fpi_game_projections (
 season INTEGER NOT NULL, game_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
 pred_point_diff REAL, game_projection REAL, matchup_quality REAL,
 team_adj_gamescore REAL,
 source TEXT NOT NULL, source_asset TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(season,game_id,team_id),
 FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fpi_team ON fpi_game_projections(season,team_id);

CREATE TABLE IF NOT EXISTS player_availability (
 availability_id TEXT NOT NULL, season INTEGER NOT NULL,
 team_id INTEGER, team TEXT NOT NULL DEFAULT '',
 player_id TEXT, player_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 position TEXT, status TEXT NOT NULL, injury_type TEXT,
 reported_at TEXT, game_id INTEGER, week INTEGER,
 short_comment TEXT NOT NULL DEFAULT '', long_comment TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL, source_url TEXT NOT NULL DEFAULT '',
 match_status TEXT NOT NULL DEFAULT 'unresolved', imported_at TEXT NOT NULL,
 PRIMARY KEY(source,availability_id)
);
CREATE INDEX IF NOT EXISTS idx_availability_team
  ON player_availability(season,team_id,reported_at DESC);

CREATE TABLE IF NOT EXISTS game_weather (
 game_id INTEGER NOT NULL, forecast_generated_at TEXT NOT NULL,
 kickoff_time TEXT NOT NULL, forecast_hour TEXT NOT NULL,
 temperature REAL, precipitation_probability REAL, precipitation_amount REAL,
 sustained_wind REAL, wind_gust REAL, humidity REAL, visibility REAL,
 weather_code INTEGER, condition TEXT NOT NULL DEFAULT '',
 flags_json TEXT NOT NULL DEFAULT '[]', indoor INTEGER NOT NULL DEFAULT 0,
 venue TEXT NOT NULL DEFAULT '', latitude REAL, longitude REAL,
 source TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(game_id,forecast_generated_at),
 FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_game_weather_game
  ON game_weather(game_id,forecast_generated_at DESC);
"""


@schema_once("external")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(EXTERNAL_SCHEMA)


def record_run(repository: CFBRepository, *, source: str, dataset: str,
               season: int | None, started_at: str, status: str,
               rows_seen: int = 0, rows_stored: int = 0, rows_skipped: int = 0,
               asset: str = "", message: str = "") -> None:
    """Record an ingestion attempt, whether or not it produced rows."""
    initialize(repository)
    with closing(repository._connect()) as connection:
        connection.execute(
            """INSERT INTO import_runs(source,dataset,season,started_at,finished_at,
               status,rows_seen,rows_stored,rows_skipped,asset,message)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (source, dataset, season, started_at, utc_now(), status,
             rows_seen, rows_stored, rows_skipped, asset, message[:500]))
        connection.commit()


def canonical_ids(repository: CFBRepository) -> tuple[set[int], set[int]]:
    """Game and team identifiers this store already owns."""
    with closing(repository._connect()) as connection:
        games = {row[0] for row in connection.execute("SELECT game_id FROM games")}
        teams = {row[0] for row in connection.execute("SELECT team_id FROM teams")}
    return games, teams


# --------------------------------------------------------------------------
# ESPN FPI
# --------------------------------------------------------------------------

#: Columns as published. The documented long format is not what ships.
FPI_COLUMNS = {
    "teampredptdiff": "pred_point_diff",
    "gameprojection": "game_projection",
    "matchupquality": "matchup_quality",
    "teamadjgamescore": "team_adj_gamescore",
}


def store_fpi(repository: CFBRepository, season: int, rows: Iterable[dict[str, Any]], *,
              asset: str, source: str = "sportsdataverse") -> dict[str, Any]:
    """Store per-game FPI projections keyed to canonical games and teams.

    ESPN publishes a projection per team per game: a predicted point
    differential, a win probability, and a matchup-quality score. None of these
    exist in CFBD, and none of them are merged into an existing rating.
    """
    initialize(repository)
    games, teams = canonical_ids(repository)
    stored = skipped = seen = 0
    now = utc_now()
    payload = []
    for row in rows:
        seen += 1
        try:
            game_id = int(str(row.get("game_id") or "").strip())
            team_id = int(str(row.get("team_id") or "").strip())
        except ValueError:
            skipped += 1
            continue
        # Rows for opponents outside the FBS store are dropped rather than
        # inserted; this importer never creates a team or a game.
        if game_id not in games or team_id not in teams:
            skipped += 1
            continue
        values = {field: optional_float(row.get(column))
                  for column, field in FPI_COLUMNS.items()}
        if all(value is None for value in values.values()):
            skipped += 1
            continue
        payload.append((season, game_id, team_id, values["pred_point_diff"],
                        values["game_projection"], values["matchup_quality"],
                        values["team_adj_gamescore"], source, asset, now))
        stored += 1
    with closing(repository._connect()) as connection:
        connection.executemany(
            """INSERT INTO fpi_game_projections VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(season,game_id,team_id) DO UPDATE SET
               pred_point_diff=excluded.pred_point_diff,
               game_projection=excluded.game_projection,
               matchup_quality=excluded.matchup_quality,
               team_adj_gamescore=excluded.team_adj_gamescore,
               source=excluded.source,source_asset=excluded.source_asset,
               imported_at=excluded.imported_at""", payload)
        connection.commit()
    return {"seen": seen, "stored": stored, "skipped": skipped}


def fpi_for_game(repository: CFBRepository, game_id: int) -> dict[str, Any]:
    """Both teams' FPI projections for one game, kept as a distinct model."""
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT f.*,t.school FROM fpi_game_projections f
               JOIN teams t USING(team_id) WHERE f.game_id=?""", (game_id,))]
    by_team = {row["team_id"]: row for row in rows}
    favored = None
    if rows:
        favored = max(rows, key=lambda row: row.get("pred_point_diff") or -99)
    return {
        "teams": by_team,
        "rows": rows,
        "model": "ESPN FPI",
        "favored": favored["school"] if favored and
                   (favored.get("pred_point_diff") or 0) > 0 else None,
        "matchup_quality": rows[0].get("matchup_quality") if rows else None,
        "source": rows[0]["source"] if rows else None,
        "source_asset": rows[0]["source_asset"] if rows else None,
    }


def fpi_team_season(repository: CFBRepository, season: int,
                    team_id: int) -> dict[str, Any]:
    """A team's FPI profile across its scheduled games."""
    initialize(repository)
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT f.*,g.week,g.start_date,g.home_team,g.away_team,
               g.home_team_id,g.away_team_id
               FROM fpi_game_projections f JOIN games g USING(game_id)
               WHERE f.season=? AND f.team_id=? ORDER BY g.start_date""",
            (season, team_id))]
    projections = [row["game_projection"] for row in rows
                   if row.get("game_projection") is not None]
    differentials = [row["pred_point_diff"] for row in rows
                     if row.get("pred_point_diff") is not None]
    return {
        "games": rows,
        "count": len(rows),
        # A season win total implied by per-game FPI probabilities. Reported as
        # what the model expects, never as a projection this application makes.
        "expected_wins": (round(sum(projections) / 100, 1) if projections else None),
        "average_point_diff": (round(sum(differentials) / len(differentials), 2)
                               if differentials else None),
    }


def store_weather(repository: CFBRepository, game_id: int, forecast, *,
                  flags, venue: str, latitude: float, longitude: float,
                  indoor: bool, generated_at: str,
                  source: str = "open-meteo") -> None:
    """Store one forecast snapshot for a game.

    Snapshots accumulate rather than overwrite: a forecast taken ten days out and
    one taken on game morning are different information, and the movement between
    them is often what matters.
    """
    initialize(repository)
    with closing(repository._connect()) as connection:
        connection.execute(
            """INSERT INTO game_weather VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id,forecast_generated_at) DO UPDATE SET
               temperature=excluded.temperature,
               precipitation_probability=excluded.precipitation_probability,
               precipitation_amount=excluded.precipitation_amount,
               sustained_wind=excluded.sustained_wind,wind_gust=excluded.wind_gust,
               humidity=excluded.humidity,visibility=excluded.visibility,
               weather_code=excluded.weather_code,condition=excluded.condition,
               flags_json=excluded.flags_json""",
            (game_id, generated_at, forecast.kickoff, forecast.forecast_hour,
             forecast.temperature, forecast.precipitation_probability,
             forecast.precipitation, forecast.wind_speed, forecast.wind_gusts,
             forecast.humidity, forecast.visibility, forecast.weather_code,
             forecast.condition, json.dumps(flags), int(bool(indoor)),
             venue, latitude, longitude, source, utc_now()))
        connection.commit()


def weather_for_game(repository: CFBRepository, game_id: int) -> dict[str, Any]:
    """The newest forecast for a game, plus how it has moved since the first."""
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM game_weather WHERE game_id=?
               ORDER BY forecast_generated_at DESC""", (game_id,))]
    if not rows:
        return {"available": False, "snapshots": 0, "flags": []}
    latest, first = rows[0], rows[-1]
    latest["flags"] = json.loads(latest.pop("flags_json") or "[]")
    # Snapshots written before the clear-sky code-zero fix stored "Unknown".
    # Correct them at read time so cached forecasts become accurate immediately
    # without throwing away their historical snapshot timestamps.
    if not latest.get("condition") or latest["condition"] == "Unknown":
        latest["condition"] = weather_condition(latest.get("weather_code"))
    movement = {}
    for field in ("temperature", "precipitation_probability", "sustained_wind"):
        if latest.get(field) is not None and first.get(field) is not None and len(rows) > 1:
            movement[field] = round(latest[field] - first[field], 1)
    return {
        "available": True,
        "snapshots": len(rows),
        "latest": latest,
        "flags": latest["flags"],
        "indoor": bool(latest.get("indoor")),
        "movement": movement,
        "first_forecast_at": first["forecast_generated_at"],
    }


def weather_summary_by_game(repository: CFBRepository, game_ids) -> dict[int, dict]:
    """Newest forecast per game, reduced to what a scoreboard card can show.

    Distinct from `weather_flags_by_game`, which answers "is anything notable
    about this game's weather" for a whole season. This answers "what does it
    look like" for a named set of games, and needs the code and temperature the
    flags summary discards.
    """
    wanted = [int(game_id) for game_id in game_ids if game_id]
    if not wanted:
        return {}
    initialize(repository)
    placeholders = ",".join("?" for _ in wanted)
    with closing(repository._connect()) as connection:
        rows = connection.execute(
            f"""SELECT game_id, weather_code, condition, temperature, indoor,
                       flags_json, forecast_generated_at
                FROM game_weather WHERE game_id IN ({placeholders})
                ORDER BY game_id, forecast_generated_at DESC""", wanted).fetchall()
    summary: dict[int, dict] = {}
    for row in rows:
        if row["game_id"] in summary:
            continue
        indoor = bool(row["indoor"])
        code = row["weather_code"]
        glyph = weather_emoji(code, indoor=indoor)
        if not glyph:
            continue
        condition = row["condition"]
        if not indoor and (not condition or condition == "Unknown"):
            condition = weather_condition(code)
        summary[row["game_id"]] = {
            "emoji": glyph,
            "indoor": indoor,
            "condition": "Indoors" if indoor else condition,
            "temperature": (None if indoor or row["temperature"] is None
                            else round(float(row["temperature"]))),
            # Indoors there is no weather to matter, whatever an old row's flags say.
            "flags": [] if indoor else json.loads(row["flags_json"] or "[]"),
        }
    return summary


def weather_flags_by_game(repository: CFBRepository, season: int) -> dict[int, list[dict]]:
    """Newest weather flags per game, for slate-level views."""
    initialize(repository)
    with closing(repository._connect()) as connection:
        rows = connection.execute(
            """SELECT w.game_id,w.flags_json,w.indoor,w.forecast_generated_at
               FROM game_weather w JOIN games g USING(game_id)
               WHERE g.season=? ORDER BY w.game_id,w.forecast_generated_at DESC""",
            (season,)).fetchall()
    flags: dict[int, list[dict]] = {}
    for row in rows:
        if row["game_id"] in flags or row["indoor"]:
            continue
        flags[row["game_id"]] = json.loads(row["flags_json"] or "[]")
    return {game_id: value for game_id, value in flags.items() if value}


def import_status(repository: CFBRepository, limit: int = 40) -> dict[str, Any]:
    """Row counts and freshness for every secondary source."""
    initialize(repository)
    with closing(repository._connect()) as connection:
        runs = [dict(row) for row in connection.execute(
            """SELECT source,dataset,season,status,rows_seen,rows_stored,rows_skipped,
               finished_at,asset,message FROM import_runs
               ORDER BY finished_at DESC LIMIT ?""", (limit,))]
        latest = [dict(row) for row in connection.execute(
            """SELECT source,dataset,MAX(finished_at) last_run,
               SUM(rows_stored) total_stored,
               SUM(CASE WHEN status<>'success' THEN 1 ELSE 0 END) failures
               FROM import_runs GROUP BY source,dataset ORDER BY source,dataset""")]
        counts = {}
        for table in ("fpi_game_projections", "player_availability",
                      "game_weather", "odds_snapshots", "award_events",
                      "prospect_signals"):
            try:
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                counts[table] = None
    return {"counts": counts, "datasets": latest, "recent_runs": runs}
