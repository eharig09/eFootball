"""Leak-safe red-zone trip and touchdown-rate models (Milestone 9)."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb.team_game_scoring import METRIC_VERSION as SCORING_VERSION
from sports_aggregator.cfb.team_game_scoring import initialize as initialize_scoring
from sports_aggregator.cfb.xdrives import RECENCY_LAMBDA, TRAILING_WINDOW_GAMES

DATASET_VERSION = "xredzone-dataset-v1"

#: Unlike xturnovers.py's giveaway rate (where the league prior alone beat
#: every shrunk/matchup blend, see two_engine_live.py's live formula), a
#: light games-weighted shrink of the matchup blend toward league beats BOTH
#: the raw matchup blend and the league-only rate here. Picked by sweeping
#: pseudo-games against this module's own dataset:
#:   trips_per_drive:  matchup mae=0.12301 -> shrunk(pseudo=4)  mae=0.12262
#:   red_zone_td_rate: league  mae=0.24719 -> shrunk(pseudo=10) mae=0.24544
SHRINKAGE_PSEUDO_GAMES = {"trips_per_drive": 4.0, "red_zone_td_rate": 10.0}


@schema_once("xredzone")
def initialize(repository) -> None:
    initialize_scoring(repository)
    with closing(repository._connect()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS cfb_xredzone_dataset (
          game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
          season INTEGER NOT NULL, week INTEGER, home_away TEXT NOT NULL,
          dataset_version TEXT NOT NULL,
          actual_red_zone_trips INTEGER NOT NULL, actual_trips_per_drive REAL,
          actual_red_zone_touchdowns INTEGER NOT NULL, actual_red_zone_td_rate REAL,
          team_prior_games INTEGER NOT NULL, team_prior_trips_per_drive REAL,
          team_prior_red_zone_td_rate REAL,
          opponent_prior_games INTEGER NOT NULL, opponent_prior_trips_allowed_per_drive REAL,
          opponent_prior_red_zone_td_rate_allowed REAL,
          league_prior_trips_per_drive REAL, league_prior_red_zone_td_rate REAL,
          built_at TEXT NOT NULL,
          PRIMARY KEY(game_id,team,dataset_version)
        );
        """)
        connection.commit()


def _weights(count: int) -> list[float]:
    import math
    return [math.exp(-RECENCY_LAMBDA * (count - 1 - i)) for i in range(count)]


def _mean(rows, key: str) -> float | None:
    pairs = [(weight, row.get(key)) for weight, row in zip(_weights(len(rows)), rows)
             if row.get(key) is not None]
    total = sum(weight for weight, _ in pairs)
    return (sum(weight * float(value) for weight, value in pairs) / total
            if pairs and total else None)


def _ratio(rows, numerator: str, denominator: str) -> float | None:
    weighted_numerator = weighted_denominator = 0.0
    for weight, row in zip(_weights(len(rows)), rows):
        if row.get(numerator) is None or row.get(denominator) is None:
            continue
        weighted_numerator += weight * float(row[numerator])
        weighted_denominator += weight * float(row[denominator])
    return weighted_numerator / weighted_denominator if weighted_denominator else None


def build_dataset(repository, *, from_season: int | None = None,
                  to_season: int | None = None,
                  dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    initialize(repository)
    clauses = ["s.metric_version=?"]
    params: list[Any] = [SCORING_VERSION]
    if from_season is not None:
        clauses.append("g.season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("g.season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        actuals = [dict(row) for row in connection.execute(f"""
          SELECT s.*,g.season,g.week,g.start_date,g.home_team,g.away_team,
                 CAST(s.red_zone_trips AS REAL)/NULLIF(s.meaningful_drives,0) AS trips_per_drive
          FROM cfb_team_game_scoring s JOIN games g USING(game_id)
          WHERE {' AND '.join(clauses)}
          ORDER BY g.start_date,g.game_id,s.team
        """, params)]

    own: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    allowed: dict[str, deque] = defaultdict(lambda: deque(maxlen=TRAILING_WINDOW_GAMES))
    league_trips = league_drives = league_tds = 0
    now = datetime.now(timezone.utc).isoformat()
    output = []
    by_game: dict[int, list[dict[str, Any]]] = defaultdict(list)
    order = []
    for row in actuals:
        if row["game_id"] not in by_game:
            order.append(row["game_id"])
        by_game[row["game_id"]].append(row)
    for game_id in order:
        game_rows = by_game[game_id]
        for row in game_rows:
            team, opponent = row["team"], row["opponent"]
            team_window, defense_window = own[team], allowed[opponent]
            output.append((
                game_id, team, opponent, row["season"], row["week"],
                "home" if team == row["home_team"] else "away", dataset_version,
                row["red_zone_trips"], row["trips_per_drive"],
                row["red_zone_touchdowns"], row["red_zone_td_rate"],
                len(team_window), _ratio(team_window, "red_zone_trips", "meaningful_drives"),
                _ratio(team_window, "red_zone_touchdowns", "red_zone_trips"),
                len(defense_window), _ratio(defense_window, "red_zone_trips", "meaningful_drives"),
                _ratio(defense_window, "red_zone_touchdowns", "red_zone_trips"),
                league_trips / league_drives if league_drives else None,
                league_tds / league_trips if league_trips else None, now,
            ))
        for row in game_rows:
            own[row["team"]].append(row)
            allowed[row["opponent"]].append(row)
            league_trips += int(row["red_zone_trips"])
            league_drives += int(row["meaningful_drives"])
            league_tds += int(row["red_zone_touchdowns"])

    with closing(repository._connect()) as connection:
        if from_season is not None or to_season is not None:
            filters, values = [], []
            if from_season is not None:
                filters.append("season>=?"); values.append(int(from_season))
            if to_season is not None:
                filters.append("season<=?"); values.append(int(to_season))
            connection.execute(f"""DELETE FROM cfb_xredzone_dataset
              WHERE dataset_version=? AND game_id IN
              (SELECT game_id FROM games WHERE {' AND '.join(filters)})""",
              (dataset_version, *values))
        else:
            connection.execute("DELETE FROM cfb_xredzone_dataset WHERE dataset_version=?",
                               (dataset_version,))
        connection.executemany("INSERT INTO cfb_xredzone_dataset VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               output)
        connection.commit()
    return {"dataset_version": dataset_version, "rows": len(output)}


def _finish(errors: list[tuple[float, float]]) -> dict[str, Any]:
    import math
    if not errors:
        return {"games": 0, "mae": None, "rmse": None, "bias": None}
    diffs = [predicted - actual for predicted, actual in errors]
    return {"games": len(errors),
            "mae": round(sum(abs(value) for value in diffs) / len(diffs), 6),
            "rmse": round(math.sqrt(sum(value * value for value in diffs) / len(diffs)), 6),
            "bias": round(sum(diffs) / len(diffs), 6)}


def evaluate_baselines(repository, *, from_season: int | None = None,
                       to_season: int | None = None,
                       dataset_version: str = DATASET_VERSION) -> dict[str, Any]:
    initialize(repository)
    clauses = ["dataset_version=?"]
    params: list[Any] = [dataset_version]
    if from_season is not None:
        clauses.append("season>=?"); params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?"); params.append(int(to_season))
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            f"SELECT * FROM cfb_xredzone_dataset WHERE {' AND '.join(clauses)}", params)]
    reports = {}
    definitions = {
        "trips_per_drive": ("actual_trips_per_drive", "team_prior_trips_per_drive",
                            "opponent_prior_trips_allowed_per_drive", "league_prior_trips_per_drive"),
        "red_zone_td_rate": ("actual_red_zone_td_rate", "team_prior_red_zone_td_rate",
                             "opponent_prior_red_zone_td_rate_allowed", "league_prior_red_zone_td_rate"),
    }
    for label, keys in definitions.items():
        actual_key, team_key, allowed_key, league_key = keys
        errors = {key: [] for key in
                  ("A_league_prior", "B_team_rate", "C_matchup_blend", "D_shrunk_blend")}
        dropped = 0
        pseudo_games = SHRINKAGE_PSEUDO_GAMES[label]
        for row in rows:
            values = [row.get(key) for key in keys]
            if any(value is None for value in values):
                dropped += 1
                continue
            actual, team, allowed, league = values
            blend = (team + allowed) / 2
            sample = min(row["team_prior_games"], row["opponent_prior_games"])
            weight = sample / (sample + pseudo_games)
            shrunk = weight * blend + (1 - weight) * league
            for key, predicted in zip(errors, (league, team, blend, shrunk)):
                errors[key].append((predicted, actual))
        reports[label] = {"rows_dropped": dropped,
                          "overall": {key: _finish(value) for key, value in errors.items()}}
    return {"dataset_version": dataset_version, "rows": len(rows), **reports}
