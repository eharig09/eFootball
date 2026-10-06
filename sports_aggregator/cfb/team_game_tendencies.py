"""Precomputed team-game tendencies from play-detail-v3 joined to in-house EPA."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once

PARSER_VERSION = "play-detail-v3"
MODEL_VERSION = "ep-v2"
METRIC_VERSION = "team-game-tendency-v1"
DEFAULT_MIN_SEASON = 2025

DIMENSIONS = {
    "rush_direction": {"column": "rush_direction", "rush_pass": "rush"},
    "pass_depth": {"column": "pass_depth", "rush_pass": "pass"},
    "pass_location": {"column": "pass_location", "rush_pass": "pass"},
    "formation": {"column": "formation", "rush_pass": None},
    "tempo": {"column": "tempo", "rush_pass": None},
}


@schema_once("team_game_tendencies")
def initialize(repository) -> None:
    from sports_aggregator.cfb.play_detail import initialize as initialize_detail
    from sports_aggregator.cfb.expected_points_v2 import initialize as initialize_ep
    initialize_detail(repository); initialize_ep(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_team_game_tendencies (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          parser_version TEXT NOT NULL, model_version TEXT NOT NULL, metric_version TEXT NOT NULL,
          dimension TEXT NOT NULL, value TEXT NOT NULL, eligible_plays INTEGER NOT NULL,
          classified_plays INTEGER NOT NULL, plays INTEGER NOT NULL, coverage REAL NOT NULL,
          total_epa REAL, epa_per_play REAL, success_rate REAL, built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,parser_version,model_version,metric_version,dimension,value),
          FOREIGN KEY(game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_tendencies_lookup ON cfb_team_game_tendencies(game_id,team,metric_version,dimension);
        CREATE INDEX IF NOT EXISTS idx_cfb_team_game_tendencies_team ON cfb_team_game_tendencies(team,metric_version,dimension,value,game_id);
        """); connection.commit()


def _scope(from_season: int | None, to_season: int | None) -> tuple[list[str], list[Any]]:
    clauses=[]; params=[]
    if from_season is not None: clauses.append("p.season>=?"); params.append(int(from_season))
    if to_season is not None: clauses.append("p.season<=?"); params.append(int(to_season))
    return clauses, params


# The joins below are CROSS JOINs on purpose: that is SQLite's way of fixing the join order, plays first. Left
# to itself the planner starts from cfb_play_metrics via its metric_version index -- every play of every
# season, 1.77M rows -- and only filters to the requested season after looking each one up in cfb_plays:
# 46 s for a one-season build instead of about a second. The results are identical; only the order differs.
def build(repository, *, from_season: int | None = DEFAULT_MIN_SEASON,
          to_season: int | None = None, parser_version: str = PARSER_VERSION,
          model_version: str = MODEL_VERSION, metric_version: str = METRIC_VERSION) -> dict[str, Any]:
    initialize(repository); scope_clauses, scope_params = _scope(from_season, to_season); now = datetime.now(timezone.utc).isoformat()
    delete_game_scope=[]; delete_params=[parser_version,model_version,metric_version]
    if from_season is not None: delete_game_scope.append("season>=?"); delete_params.append(int(from_season))
    if to_season is not None: delete_game_scope.append("season<=?"); delete_params.append(int(to_season))
    output=[]; dimension_counts={}
    with closing(repository._connect()) as connection:
        if delete_game_scope:
            connection.execute(f"DELETE FROM cfb_team_game_tendencies WHERE parser_version=? AND model_version=? AND metric_version=? AND game_id IN (SELECT game_id FROM games WHERE {' AND '.join(delete_game_scope)})", delete_params)
        else:
            connection.execute("DELETE FROM cfb_team_game_tendencies WHERE parser_version=? AND model_version=? AND metric_version=?", delete_params)
        for dimension, spec in DIMENSIONS.items():
            column=str(spec["column"]); base=["e.model_version=?","m.metric_version='pbp-v1'","m.rush_pass IN ('rush','pass')"]; params=[model_version]
            if spec["rush_pass"]: base.append("m.rush_pass=?"); params.append(spec["rush_pass"])
            base.extend(scope_clauses); params.extend(scope_params); where=" AND ".join(base)
            eligible_sql=f"""SELECT p.game_id,p.offense AS team,p.defense AS opponent,COUNT(*) AS eligible_plays,SUM(CASE WHEN d.{column} IS NOT NULL THEN 1 ELSE 0 END) AS classified_plays FROM cfb_plays p CROSS JOIN cfb_play_metrics m ON m.play_id=p.play_id CROSS JOIN cfb_play_epa e ON e.play_id=p.play_id LEFT JOIN cfb_play_detail d ON d.play_id=p.play_id AND d.parser_version=? WHERE {where} GROUP BY p.game_id,p.offense,p.defense"""
            eligible={(int(r["game_id"]),str(r["team"])):dict(r) for r in connection.execute(eligible_sql,[parser_version,*params]).fetchall()}
            grouped_sql=f"""SELECT p.game_id,p.offense AS team,p.defense AS opponent,d.{column} AS value,COUNT(*) AS plays,SUM(e.epa) AS total_epa,AVG(e.epa) AS epa_per_play,AVG(CASE WHEN m.success IS NOT NULL THEN m.success END) AS success_rate FROM cfb_plays p CROSS JOIN cfb_play_metrics m ON m.play_id=p.play_id CROSS JOIN cfb_play_epa e ON e.play_id=p.play_id CROSS JOIN cfb_play_detail d ON d.play_id=p.play_id AND d.parser_version=? WHERE {where} AND d.{column} IS NOT NULL GROUP BY p.game_id,p.offense,p.defense,d.{column}"""
            rows=connection.execute(grouped_sql,[parser_version,*params]).fetchall(); dimension_counts[dimension]=len(rows)
            for row in rows:
                denom=eligible.get((int(row["game_id"]),str(row["team"]))) or {}; eligible_plays=int(denom.get("eligible_plays") or 0); classified_plays=int(denom.get("classified_plays") or 0); coverage=classified_plays/eligible_plays if eligible_plays else 0.0
                output.append((int(row["game_id"]),str(row["team"]),str(row["opponent"]),parser_version,model_version,metric_version,dimension,str(row["value"]),eligible_plays,classified_plays,int(row["plays"] or 0),coverage,row["total_epa"],row["epa_per_play"],row["success_rate"],now))
        connection.executemany("""INSERT INTO cfb_team_game_tendencies(game_id,team,opponent,parser_version,model_version,metric_version,dimension,value,eligible_plays,classified_plays,plays,coverage,total_epa,epa_per_play,success_rate,built_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", output); connection.commit()
    return {"parser_version":parser_version,"model_version":model_version,"metric_version":metric_version,"from_season":from_season,"to_season":to_season,"rows":len(output),"rows_by_dimension":dimension_counts}


def game_summary(repository, game_id: int, *, parser_version: str = PARSER_VERSION,
                 model_version: str = MODEL_VERSION, metric_version: str = METRIC_VERSION,
                 min_plays: int = 3, min_coverage: float = 0.25) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        return [dict(r) for r in connection.execute("""SELECT * FROM cfb_team_game_tendencies WHERE game_id=? AND parser_version=? AND model_version=? AND metric_version=? AND plays>=? AND coverage>=? ORDER BY team,dimension,plays DESC,value""", (int(game_id),parser_version,model_version,metric_version,max(1,int(min_plays)),max(0.0,min(1.0,float(min_coverage))))).fetchall()]
