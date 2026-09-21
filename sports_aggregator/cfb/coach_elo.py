"""Head-coach Elo, keyed by the person rather than the school.

CFBD publishes a team-level Elo directly on `games` (home_pregame_elo /
away_pregame_elo), but nothing coach-scoped. This builds a parallel Elo
history keyed by `coach_id` -- mirrors sports_aggregator.nfl.elo's engine
(same BASE/K_FACTOR/HOME_ADVANTAGE/margin-of-victory shape), but a coach's
rating carries across a job change instead of resetting with a new team.

Game-to-coach attribution: `coach_seasons` gives CFBD's own attributed
games/wins/losses per (season, coach_id, team_id), not which specific
game_ids belong to which coach. For the ~8% of team-seasons with more than
one coach (a mid-season firing/interim), there's no ground truth for which
games belong to whom, so this approximates: the coach with the *most*
credited games that season is assumed to have coached the first stretch of
the schedule in order, the next coach the following stretch, and so on.
That's a real approximation, not a fact -- every persisted row from a
split season is stamped attribution_method='heuristic_split' so it can be
filtered out or discounted downstream.

The composite rating asked for (Elo + offensive PPG + defensive PPG +
opponent average Elo + home record, blended via Z-scores) is intentionally
NOT persisted -- it's cheap to compute on demand from the built game table
and always reflects live standardization against whichever coach
population currently qualifies, the same "recompute, don't cache a
snapshot" choice the rest of this codebase's research modules make.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import math
from typing import Any

from sports_aggregator.cfb.repository import CFBRepository, schema_once

BASE = 1500.0
HOME_ADVANTAGE = 55.0
K_FACTOR = 20.0

#: A coach needs at least this many attributed games before their composite
#: rating is trusted -- an interim who coached one bowl game shouldn't be
#: able to post a #1 rating off a single result. Mirrors the volume floors
#: already used for PFF grades and NGS headline ranks.
MIN_GAMES_FOR_COMPOSITE = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_coach_elo_games (
    game_id INTEGER PRIMARY KEY, season INTEGER NOT NULL, week INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    home_coach_id INTEGER NOT NULL, away_coach_id INTEGER NOT NULL,
    home_team TEXT NOT NULL, away_team TEXT NOT NULL,
    home_points INTEGER NOT NULL, away_points INTEGER NOT NULL,
    home_opponent_pregame_elo REAL, away_opponent_pregame_elo REAL,
    neutral_site INTEGER NOT NULL,
    home_pre_elo REAL NOT NULL, away_pre_elo REAL NOT NULL,
    home_post_elo REAL NOT NULL, away_post_elo REAL NOT NULL,
    home_expected REAL NOT NULL,
    home_attribution_method TEXT NOT NULL, away_attribution_method TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coach_elo_games_home ON cfb_coach_elo_games(home_coach_id);
CREATE INDEX IF NOT EXISTS idx_coach_elo_games_away ON cfb_coach_elo_games(away_coach_id);

CREATE TABLE IF NOT EXISTS cfb_coach_elo_ratings (
    coach_id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
    rating REAL NOT NULL, games INTEGER NOT NULL, wins INTEGER NOT NULL,
    losses INTEGER NOT NULL, ties INTEGER NOT NULL,
    last_team TEXT NOT NULL, last_game_id INTEGER NOT NULL, updated_at TEXT NOT NULL
);
"""


@schema_once("coach_elo")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)


def _coach_seasons(repository: CFBRepository) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """(season, team_id) -> coach stints, ordered most-games-first."""
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT season,coach_id,team_id,first_name,last_name,games
               FROM coach_seasons WHERE games > 0"""
        )]
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["season"]), int(row["team_id"]))].append(row)
    for key, stints in grouped.items():
        stints.sort(key=lambda r: -int(r["games"]))
    return grouped


def _games(repository: CFBRepository, start_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,start_date,neutral_site,
                      home_team_id,home_team,home_points,home_pregame_elo,
                      away_team_id,away_team,away_points,away_pregame_elo
               FROM games
               WHERE season >= ? AND completed=1
                 AND home_points IS NOT NULL AND away_points IS NOT NULL
               ORDER BY season,start_date,game_id""",
            (int(start_season),),
        )]
    return rows


def _assign_coaches(games: list[dict[str, Any]], coach_seasons: dict[tuple[int, int], list[dict[str, Any]]]):
    """Walk each team's season schedule in order, consuming coach stints by
    their credited game count. Returns {game_id: {"home": (coach_id, name, method), "away": (...)}}."""
    by_team_season: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        by_team_season[(int(game["season"]), int(game["home_team_id"]))].append(game)
        by_team_season[(int(game["season"]), int(game["away_team_id"]))].append(game)

    # Each team-season's games list may have duplicates removed below by
    # only ever walking it once per (season, team_id) via a cursor dict.
    cursor: dict[tuple[int, int], int] = {}
    assignment: dict[int, dict[str, tuple[int, str, str]]] = defaultdict(dict)

    for key, team_games in by_team_season.items():
        stints = coach_seasons.get(key)
        if not stints:
            continue
        method = "direct" if len(stints) == 1 else "heuristic_split"
        pos = 0
        for stint in stints:
            name = f"{stint['first_name']} {stint['last_name']}".strip()
            take = min(int(stint["games"]), len(team_games) - pos)
            for game in team_games[pos:pos + take]:
                side = "home" if int(game["home_team_id"]) == key[1] else "away"
                assignment[int(game["game_id"])][side] = (int(stint["coach_id"]), name, method)
            pos += take
            if pos >= len(team_games):
                break
    return assignment


def build(repository: CFBRepository, *, start_season: int = 2015) -> int:
    """Replay every completed game chronologically; a coach's rating starts
    at BASE the first time they're seen and persists across job changes."""
    initialize(repository)
    games = _games(repository, start_season)
    coach_seasons = _coach_seasons(repository)
    assignment = _assign_coaches(games, coach_seasons)

    ratings: dict[int, float] = {}
    names: dict[int, str] = {}
    records: dict[int, dict[str, Any]] = {}
    history = []

    for game in games:
        sides = assignment.get(int(game["game_id"]))
        if not sides or "home" not in sides or "away" not in sides:
            continue
        home_coach, home_name, home_method = sides["home"]
        away_coach, away_name, away_method = sides["away"]
        names[home_coach] = home_name
        names[away_coach] = away_name

        home_pre = ratings.get(home_coach, BASE)
        away_pre = ratings.get(away_coach, BASE)
        neutral = bool(game["neutral_site"])
        adjusted_home = home_pre + (0.0 if neutral else HOME_ADVANTAGE)
        home_expected = 1.0 / (1.0 + 10 ** ((away_pre - adjusted_home) / 400.0))

        home_points, away_points = float(game["home_points"]), float(game["away_points"])
        actual = 1.0 if home_points > away_points else 0.0 if home_points < away_points else 0.5
        margin = abs(home_points - away_points)
        multiplier = max(1.0, math.log(margin + 1) * (2.2 / (abs(home_pre - away_pre) * .001 + 2.2)))
        change = K_FACTOR * multiplier * (actual - home_expected)
        home_post, away_post = home_pre + change, away_pre - change
        ratings[home_coach], ratings[away_coach] = home_post, away_post

        for coach_id, won, lost, tied, team in (
            (home_coach, actual == 1, actual == 0, actual == .5, game["home_team"]),
            (away_coach, actual == 0, actual == 1, actual == .5, game["away_team"]),
        ):
            record = records.setdefault(coach_id, {"games": 0, "wins": 0, "losses": 0, "ties": 0})
            record["games"] += 1
            record["wins"] += int(won)
            record["losses"] += int(lost)
            record["ties"] += int(tied)
            record["last_team"] = team
            record["last_game_id"] = int(game["game_id"])

        history.append((
            int(game["game_id"]), int(game["season"]), int(game["week"]), str(game["start_date"]),
            home_coach, away_coach, str(game["home_team"]), str(game["away_team"]),
            int(home_points), int(away_points),
            float(game["away_pregame_elo"]) if game["away_pregame_elo"] is not None else None,
            float(game["home_pregame_elo"]) if game["home_pregame_elo"] is not None else None,
            int(neutral), home_pre, away_pre, home_post, away_post, home_expected,
            home_method, away_method,
        ))

    stamp = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM cfb_coach_elo_games")
        connection.execute("DELETE FROM cfb_coach_elo_ratings")
        connection.executemany(
            """INSERT INTO cfb_coach_elo_games VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            history,
        )
        connection.executemany(
            "INSERT INTO cfb_coach_elo_ratings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    coach_id, names[coach_id].split(" ", 1)[0] if " " in names[coach_id] else names[coach_id],
                    names[coach_id].split(" ", 1)[1] if " " in names[coach_id] else "",
                    rating, records[coach_id]["games"], records[coach_id]["wins"],
                    records[coach_id]["losses"], records[coach_id]["ties"],
                    records[coach_id]["last_team"], records[coach_id]["last_game_id"], stamp,
                )
                for coach_id, rating in ratings.items()
            ],
        )
        connection.commit()
    return len(history)


def _zscore(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return {}
    n = len(values)
    mean = sum(values.values()) / n
    variance = sum((v - mean) ** 2 for v in values.values()) / n
    stdev = math.sqrt(variance) or 1.0
    return {k: (v - mean) / stdev for k, v in values.items()}


def leaderboard(repository: CFBRepository, *, min_games: int = MIN_GAMES_FOR_COMPOSITE) -> list[dict[str, Any]]:
    """Current Elo plus a Z-score composite over offensive/defensive PPG,
    average opponent (team) Elo faced, and home win rate -- every component
    standardized against the population of coaches meeting min_games."""
    initialize(repository)
    with repository._reader() as connection:
        ratings = [dict(r) for r in connection.execute("SELECT * FROM cfb_coach_elo_ratings")]
        games = [dict(r) for r in connection.execute("SELECT * FROM cfb_coach_elo_games")]

    per_coach: dict[int, dict[str, Any]] = defaultdict(lambda: {
        "points_for": [], "points_against": [], "opponent_elo": [],
        "home_games": 0, "home_wins": 0, "heuristic_games": 0,
    })
    for game in games:
        for side, other in (("home", "away"), ("away", "home")):
            coach_id = game[f"{side}_coach_id"]
            bucket = per_coach[coach_id]
            bucket["points_for"].append(game[f"{side}_points"])
            bucket["points_against"].append(game[f"{other}_points"])
            opp_elo = game[f"{side}_opponent_pregame_elo"]
            if opp_elo is not None:
                bucket["opponent_elo"].append(opp_elo)
            if game[f"{side}_attribution_method"] == "heuristic_split":
                bucket["heuristic_games"] += 1
        home_won = game["home_points"] > game["away_points"]
        per_coach[game["home_coach_id"]]["home_games"] += 1
        per_coach[game["home_coach_id"]]["home_wins"] += int(home_won)

    qualifying = {r["coach_id"]: r for r in ratings if r["games"] >= min_games}
    elo_z = _zscore({cid: r["rating"] for cid, r in qualifying.items()})
    off_ppg = {cid: sum(per_coach[cid]["points_for"]) / len(per_coach[cid]["points_for"]) for cid in qualifying}
    def_ppg = {cid: sum(per_coach[cid]["points_against"]) / len(per_coach[cid]["points_against"]) for cid in qualifying}
    opp_elo = {
        cid: (sum(per_coach[cid]["opponent_elo"]) / len(per_coach[cid]["opponent_elo"])
              if per_coach[cid]["opponent_elo"] else BASE)
        for cid in qualifying
    }
    home_pct = {
        cid: (per_coach[cid]["home_wins"] / per_coach[cid]["home_games"]
              if per_coach[cid]["home_games"] else None)
        for cid in qualifying
    }
    off_z = _zscore(off_ppg)
    def_z = _zscore({cid: -v for cid, v in def_ppg.items()})  # fewer points allowed is better
    opp_elo_z = _zscore(opp_elo)
    home_pct_z = _zscore({cid: v for cid, v in home_pct.items() if v is not None})

    rows = []
    for coach_id, rating in qualifying.items():
        components = {
            "elo_z": elo_z.get(coach_id, 0.0),
            "offense_ppg_z": off_z.get(coach_id, 0.0),
            "defense_ppg_z": def_z.get(coach_id, 0.0),
            "opponent_elo_z": opp_elo_z.get(coach_id, 0.0),
            "home_win_pct_z": home_pct_z.get(coach_id, 0.0),
        }
        rows.append({
            "coach_id": coach_id,
            "name": f"{rating['first_name']} {rating['last_name']}".strip(),
            "last_team": rating["last_team"],
            "elo": round(rating["rating"], 1),
            "games": rating["games"],
            "record": f"{rating['wins']}-{rating['losses']}" + (f"-{rating['ties']}" if rating["ties"] else ""),
            "offense_ppg": round(off_ppg[coach_id], 2),
            "defense_ppg": round(def_ppg[coach_id], 2),
            "opponent_avg_elo": round(opp_elo[coach_id], 1),
            "home_win_pct": round(home_pct[coach_id], 4) if home_pct[coach_id] is not None else None,
            "heuristic_attribution_games": per_coach[coach_id]["heuristic_games"],
            "components": {k: round(v, 4) for k, v in components.items()},
            "composite_rating": round(sum(components.values()) / len(components), 4),
        })
    rows.sort(key=lambda r: r["composite_rating"], reverse=True)
    return rows
