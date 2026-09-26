"""Precomputed team-game advanced metrics built from in-house EPA and pbp-v1.

The web app should read this compact table instead of repeatedly scanning play-by-play.
Offensive efficiency denominators use qualifying rush/pass scrimmage plays only;
kicks, penalties and other transitions remain available in cfb_play_epa but do not
pollute offensive EPA/play.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once

MODEL_VERSION = "ep-v2"
METRIC_VERSION = "team-game-advanced-v1"


@schema_once("team_game_advanced")
def initialize(repository) -> None:
    from sports_aggregator.cfb.expected_points_v2 import initialize as initialize_ep
    initialize_ep(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_team_game_advanced (
          game_id INTEGER NOT NULL,
          team TEXT NOT NULL,
          opponent TEXT NOT NULL,
          model_version TEXT NOT NULL,
          metric_version TEXT NOT NULL,
          scrimmage_plays INTEGER NOT NULL,
          rush_plays INTEGER NOT NULL,
          pass_plays INTEGER NOT NULL,
          total_epa REAL,
          epa_per_play REAL,
          competitive_epa_per_play REAL,
          rush_epa_per_play REAL,
          pass_epa_per_play REAL,
          early_down_epa_per_play REAL,
          standard_down_epa_per_play REAL,
          passing_down_epa_per_play REAL,
          success_rate REAL,
          explosive_rate REAL,
          havoc_allowed_rate REAL,
          scoring_opportunity_rate REAL,
          defensive_epa_allowed_per_play REAL,
          defensive_competitive_epa_allowed_per_play REAL,
          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,model_version,metric_version),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_advanced_team
          ON cfb_team_game_advanced(team,model_version,game_id);
        """)
        connection.commit()


def _ratio(total: float, count: int) -> float | None:
    return total / count if count else None


def build(repository, *, from_season: int | None = None,
          to_season: int | None = None, model_version: str = MODEL_VERSION,
          metric_version: str = METRIC_VERSION) -> dict[str, Any]:
    """Aggregate scored EPA to one compact row per offense/team/game."""
    initialize(repository)
    clauses = ["e.model_version=?", "m.metric_version='pbp-v1'", "m.rush_pass IN ('rush','pass')"]
    params: list[Any] = [model_version]
    season_clauses: list[str] = []
    season_params: list[Any] = []
    if from_season is not None:
        clauses.append("p.season>=?")
        params.append(int(from_season))
        season_clauses.append("season>=?")
        season_params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?")
        params.append(int(to_season))
        season_clauses.append("season<=?")
        season_params.append(int(to_season))

    sql = f"""
      SELECT p.game_id,p.offense AS team,p.defense AS opponent,
             COUNT(*) AS scrimmage_plays,
             SUM(CASE WHEN m.rush_pass='rush' THEN 1 ELSE 0 END) AS rush_plays,
             SUM(CASE WHEN m.rush_pass='pass' THEN 1 ELSE 0 END) AS pass_plays,
             SUM(e.epa) AS total_epa,
             AVG(e.epa) AS epa_per_play,
             AVG(CASE WHEN m.garbage_time=0 THEN e.epa END) AS competitive_epa_per_play,
             AVG(CASE WHEN m.rush_pass='rush' THEN e.epa END) AS rush_epa_per_play,
             AVG(CASE WHEN m.rush_pass='pass' THEN e.epa END) AS pass_epa_per_play,
             AVG(CASE WHEN p.down IN (1,2) THEN e.epa END) AS early_down_epa_per_play,
             AVG(CASE WHEN m.down_type='standard' THEN e.epa END) AS standard_down_epa_per_play,
             AVG(CASE WHEN m.down_type='passing' THEN e.epa END) AS passing_down_epa_per_play,
             AVG(CASE WHEN m.success IS NOT NULL THEN m.success END) AS success_rate,
             AVG(CASE WHEN m.explosive IS NOT NULL THEN m.explosive END) AS explosive_rate,
             AVG(CASE WHEN m.havoc IS NOT NULL THEN m.havoc END) AS havoc_allowed_rate,
             AVG(CASE WHEN m.scoring_opportunity IS NOT NULL THEN m.scoring_opportunity END) AS scoring_opportunity_rate
      FROM cfb_plays p
      JOIN cfb_play_metrics m ON m.play_id=p.play_id
      JOIN cfb_play_epa e ON e.play_id=p.play_id
      WHERE {' AND '.join(clauses)}
      GROUP BY p.game_id,p.offense,p.defense
    """

    now = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        if season_clauses:
            connection.execute(f"""DELETE FROM cfb_team_game_advanced
              WHERE model_version=? AND metric_version=? AND game_id IN (
                SELECT game_id FROM games WHERE {' AND '.join(season_clauses)}
              )""", (model_version, metric_version, *season_params))
        else:
            connection.execute("DELETE FROM cfb_team_game_advanced WHERE model_version=? AND metric_version=?",
                               (model_version, metric_version))
        rows = connection.execute(sql, params).fetchall()
        output = []
        for r in rows:
            output.append((
                int(r["game_id"]), str(r["team"]), str(r["opponent"]), model_version, metric_version,
                int(r["scrimmage_plays"] or 0), int(r["rush_plays"] or 0), int(r["pass_plays"] or 0),
                r["total_epa"], r["epa_per_play"], r["competitive_epa_per_play"],
                r["rush_epa_per_play"], r["pass_epa_per_play"], r["early_down_epa_per_play"],
                r["standard_down_epa_per_play"], r["passing_down_epa_per_play"],
                r["success_rate"], r["explosive_rate"], r["havoc_allowed_rate"],
                r["scoring_opportunity_rate"], None, None, now,
            ))
        connection.executemany("""INSERT INTO cfb_team_game_advanced(
          game_id,team,opponent,model_version,metric_version,scrimmage_plays,rush_plays,pass_plays,
          total_epa,epa_per_play,competitive_epa_per_play,rush_epa_per_play,pass_epa_per_play,
          early_down_epa_per_play,standard_down_epa_per_play,passing_down_epa_per_play,
          success_rate,explosive_rate,havoc_allowed_rate,scoring_opportunity_rate,
          defensive_epa_allowed_per_play,defensive_competitive_epa_allowed_per_play,built_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output)

        connection.execute("""UPDATE cfb_team_game_advanced AS a
          SET defensive_epa_allowed_per_play=(
                SELECT b.epa_per_play FROM cfb_team_game_advanced b
                WHERE b.game_id=a.game_id AND b.team=a.opponent
                  AND b.model_version=a.model_version AND b.metric_version=a.metric_version
              ),
              defensive_competitive_epa_allowed_per_play=(
                SELECT b.competitive_epa_per_play FROM cfb_team_game_advanced b
                WHERE b.game_id=a.game_id AND b.team=a.opponent
                  AND b.model_version=a.model_version AND b.metric_version=a.metric_version
              )
          WHERE a.model_version=? AND a.metric_version=?""", (model_version, metric_version))
        connection.commit()

    return {
        "model_version": model_version,
        "metric_version": metric_version,
        "team_game_rows": len(rows),
        "from_season": from_season,
        "to_season": to_season,
    }


def game_summary(repository, game_id: int, *, model_version: str = MODEL_VERSION,
                 metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        return [dict(r) for r in connection.execute("""
          SELECT * FROM cfb_team_game_advanced
          WHERE game_id=? AND model_version=? AND metric_version=?
          ORDER BY team
        """, (int(game_id), model_version, metric_version)).fetchall()]


def team_weekly_trend(repository, team: str, season: int, *,
                      model_version: str = MODEL_VERSION,
                      metric_version: str = METRIC_VERSION) -> list[dict[str, Any]]:
    """One team's offense/defense EPA, success and explosive rate, by week.

    Defense here is what the opponent's OWN offense did that game -- the same
    row `defensive_epa_allowed_per_play` is already built from -- read off the
    opponent's own team-game row rather than recomputed, so it stays exactly
    consistent with the rest of this table.
    """
    initialize(repository)
    with repository._reader() as connection:
        rows = connection.execute("""
          SELECT g.week, g.game_id, a.opponent,
                 a.epa_per_play, a.pass_epa_per_play, a.rush_epa_per_play,
                 a.success_rate, a.explosive_rate,
                 d.epa_per_play AS defense_epa_per_play,
                 d.pass_epa_per_play AS defense_pass_epa_per_play,
                 d.rush_epa_per_play AS defense_rush_epa_per_play,
                 d.success_rate AS defense_success_rate,
                 d.explosive_rate AS defense_explosive_rate
          FROM cfb_team_game_advanced a
          JOIN games g ON g.game_id = a.game_id
          LEFT JOIN cfb_team_game_advanced d
            ON d.game_id = a.game_id AND d.team = a.opponent
            AND d.model_version = a.model_version AND d.metric_version = a.metric_version
          WHERE a.team = ? AND g.season = ? AND a.model_version = ? AND a.metric_version = ?
          ORDER BY g.week
        """, (str(team), int(season), model_version, metric_version)).fetchall()
    return [dict(row) for row in rows]
