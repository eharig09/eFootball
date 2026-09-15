"""A transparent, continuous NFL Elo history beginning in 2010."""
from __future__ import annotations
from datetime import datetime, timezone
from contextlib import closing
import math
from typing import Any, Iterable, Mapping
from sports_aggregator.nfl.models import optional_float
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository

BASE = 1500.0
HOME_ADVANTAGE = 55.0
K_FACTOR = 20.0

def build_elo(repository: NFLRepository, games: Iterable[Mapping[str, Any]],
              *, start_season: int = 2010) -> int:
    """Replay completed games chronologically; every franchise enters at 1500."""
    eligible = []
    for row in games:
        away_score = optional_float(row.get("away_score")); home_score = optional_float(row.get("home_score"))
        season = int(row.get("season") or 0)
        if season < start_season or away_score is None or home_score is None:
            continue
        if str(row.get("game_type") or "REG") not in {"REG", "WC", "DIV", "CON", "SB", "POST"}:
            continue
        eligible.append(row)
    eligible.sort(key=lambda row: (int(row.get("season") or 0), int(row.get("week") or 0),
                                   str(row.get("gameday") or ""), str(row.get("game_id") or "")))
    ratings: dict[str, float] = {}; records: dict[str, dict[str, Any]] = {}; history = []
    for row in eligible:
        away, home = canon_team(row.get("away_team")), canon_team(row.get("home_team"))
        if not away or not home:
            continue
        away_pre, home_pre = ratings.get(away, BASE), ratings.get(home, BASE)
        neutral = str(row.get("location") or "").casefold() == "neutral"
        adjusted_home = home_pre + (0 if neutral else HOME_ADVANTAGE)
        home_expected = 1 / (1 + 10 ** ((away_pre - adjusted_home) / 400))
        away_score, home_score = float(row["away_score"]), float(row["home_score"])
        actual = 1.0 if home_score > away_score else 0.0 if home_score < away_score else .5
        margin = abs(home_score - away_score)
        multiplier = max(1.0, math.log(margin + 1) * (2.2 / (abs(home_pre - away_pre) * .001 + 2.2)))
        change = K_FACTOR * multiplier * (actual - home_expected)
        home_post, away_post = home_pre + change, away_pre - change
        ratings[home], ratings[away] = home_post, away_post
        game_id = str(row.get("game_id"))
        for team, won, lost, tied in ((home, actual == 1, actual == 0, actual == .5),
                                      (away, actual == 0, actual == 1, actual == .5)):
            record = records.setdefault(team, {"games": 0, "wins": 0, "losses": 0, "ties": 0})
            record["games"] += 1; record["wins"] += int(won); record["losses"] += int(lost)
            record["ties"] += int(tied); record["last_game_id"] = game_id
        history.append((game_id, int(row["season"]), int(row.get("week") or 0), away, home,
                        int(away_score), int(home_score), away_pre, home_pre, away_post,
                        home_post, home_expected))
    stamp = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM nfl_elo_games"); connection.execute("DELETE FROM nfl_elo_ratings")
        connection.executemany("INSERT INTO nfl_elo_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", history)
        connection.executemany("INSERT INTO nfl_elo_ratings VALUES (?,?,?,?,?,?,?,?)", [
            (team, rating, records[team]["games"], records[team]["wins"], records[team]["losses"],
             records[team]["ties"], records[team]["last_game_id"], stamp)
            for team, rating in ratings.items()
        ])
        connection.commit()
    return len(history)
