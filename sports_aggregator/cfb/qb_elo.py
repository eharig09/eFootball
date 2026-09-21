"""QB Elo, keyed by the person rather than the school -- and, unlike the
head-coach Elo, driven by performance rather than team win/loss.

A coach's job is literally to win the game in front of him, so a classic
win/loss Elo update is a fair signal for coach quality. A QB's team can lose
a game the QB himself played well in (or win one he played poorly in) --
attributing the team's game outcome straight to the QB the way coach-Elo
attributes it to the coach would systematically punish good QBs on bad
rosters and flatter mediocre ones on great rosters. Instead, each start
updates the rating toward a performance-implied rating built from that
game's per-play predicted points added (PPA, this repo's PPA-based
NGS-equivalent -- see docs/CFB_ARCHITECTURE.md), z-scored against *that
season's* qualifying-QB distribution before being converted to a rating
delta. That z-scoring is the "controlling for passing environment" this
was asked for: a 0.25 PPA/play game means something different in a
run-first, low-possession 2015 than in the pace-and-space 2024 landscape,
and raw PPA never gets compared across seasons without first being
standardized against its own season.

There's no per-game "starter" designation in this repo's schema, so the
starter is inferred the same way sports_aggregator.cfb.depth_chart_observed
already infers usage from box scores: whichever QB had the most passing
attempts for a team in a given game is treated as that game's starter,
subject to a minimum-attempts floor so three garbage-time snaps don't
count as a start.

The composite rating (asked to be "similar" to the head-coach one) blends
five Z-scores: the PPA-driven Elo itself, an era-adjusted yards-per-attempt
index, an era-adjusted TD-minus-INT-per-game index, average opponent (team)
Elo faced, and team win rate in games this QB started -- the same idea as
the coach composite (record + counting stats + strength of schedule),
just built around an individual performance signal instead of a
categorical outcome.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import math
from typing import Any

from sports_aggregator.cfb.depth_chart_observed import _compound_attempts, observed_depth_roles
from sports_aggregator.cfb.repository import CFBRepository, schema_once

BASE = 1500.0
#: Rating points per one standard deviation of that season's qualifying-QB
#: PPA/play distribution -- roughly the same order of magnitude as a team's
#: typical single-game Elo swing, so one great or awful start moves a QB's
#: rating by a comparable amount to what a comparable single result moves
#: a team's Elo.
RATING_SCALE = 175.0
#: How much one game's performance-implied rating pulls the running rating
#: toward it. An EWMA-style blend rather than a classic K/400 Elo update,
#: because the signal here (season-relative performance) is continuous, not
#: a binary win/loss draw.
ALPHA = 0.18
#: A QB needs this many pass attempts in a game to count as that game's
#: start for rating purposes -- keeps three garbage-time snaps from moving
#: the rating.
MIN_ATTEMPTS_FOR_START = 8
#: And this many counted starts before appearing on the composite leaderboard,
#: same volume-floor idea used for the coach composite, PFF grades, and NGS
#: headline ranks elsewhere in this codebase.
MIN_STARTS_FOR_COMPOSITE = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_qb_elo_games (
    game_id INTEGER NOT NULL, season INTEGER NOT NULL, week INTEGER NOT NULL,
    start_date TEXT NOT NULL, side TEXT NOT NULL,
    player_id TEXT NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
    attempts REAL NOT NULL, pass_yards REAL, pass_td REAL, pass_int REAL,
    ppa_all REAL, opponent_pregame_elo REAL, team_won INTEGER,
    season_ppa_z REAL, season_ypa_z REAL, season_td_int_margin_z REAL,
    pre_rating REAL NOT NULL, post_rating REAL NOT NULL,
    PRIMARY KEY (game_id, side)
);
CREATE INDEX IF NOT EXISTS idx_qb_elo_games_player ON cfb_qb_elo_games(player_id);

CREATE TABLE IF NOT EXISTS cfb_qb_elo_ratings (
    player_id TEXT PRIMARY KEY, name TEXT NOT NULL,
    rating REAL NOT NULL, starts INTEGER NOT NULL, wins INTEGER NOT NULL,
    losses INTEGER NOT NULL, ties INTEGER NOT NULL,
    last_team TEXT NOT NULL, last_game_id INTEGER NOT NULL, updated_at TEXT NOT NULL
);
"""


@schema_once("qb_elo")
def initialize(repository: CFBRepository) -> None:
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)


def _games(repository: CFBRepository, start_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [dict(r) for r in connection.execute(
            """SELECT game_id,season,week,start_date,
                      home_team_id,home_team,home_points,home_pregame_elo,
                      away_team_id,away_team,away_points,away_pregame_elo
               FROM games
               WHERE season >= ? AND completed=1
                 AND home_points IS NOT NULL AND away_points IS NOT NULL
               ORDER BY season,start_date,game_id""",
            (int(start_season),),
        )]


def _starters(repository: CFBRepository, start_season: int) -> dict[int, dict[str, tuple[str, str, float]]]:
    """game_id -> {"home"/"away": (player_id, player_name, attempts)} for
    whichever QB had the most passing attempts for that team that game."""
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT b.game_id,b.team,b.player_id,b.player,b.stat_value,g.home_team,g.away_team
               FROM game_player_box_stats b
               JOIN games g ON g.game_id=b.game_id
               WHERE b.category='passing' AND b.stat_type IN ('C/ATT','CMP/ATT')
                 AND g.season >= ?""",
            (int(start_season),),
        )]
    best: dict[tuple[int, str], tuple[str, str, float]] = {}
    for row in rows:
        attempts = _compound_attempts(row["stat_value"])
        if attempts <= 0:
            continue
        key = (int(row["game_id"]), str(row["team"]))
        current = best.get(key)
        if current is None or attempts > current[2]:
            best[key] = (str(row["player_id"]), str(row["player"] or ""), attempts)

    out: dict[int, dict[str, tuple[str, str, float]]] = defaultdict(dict)
    for (game_id, team), (player_id, name, attempts) in best.items():
        if attempts < MIN_ATTEMPTS_FOR_START:
            continue
        out[game_id][team] = (player_id, name, attempts)
    return out


def _passing_lines(repository: CFBRepository, start_season: int) -> dict[tuple[int, str], dict[str, float]]:
    """(game_id, player_id) -> {yards, td, int} from the box score."""
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT b.game_id,b.player_id,b.stat_type,b.numeric_value
               FROM game_player_box_stats b JOIN games g ON g.game_id=b.game_id
               WHERE b.category='passing' AND b.stat_type IN ('YDS','TD','INT')
                 AND g.season >= ?""",
            (int(start_season),),
        )]
    out: dict[tuple[int, str], dict[str, float]] = defaultdict(dict)
    label = {"YDS": "yards", "TD": "td", "INT": "int"}
    for row in rows:
        key = (int(row["game_id"]), str(row["player_id"]))
        stat = label.get(str(row["stat_type"]))
        if stat and row["numeric_value"] is not None:
            out[key][stat] = float(row["numeric_value"])
    return out


def _ppa_lookup(repository: CFBRepository, start_season: int) -> dict[tuple[int, str], float]:
    with repository._reader() as connection:
        rows = connection.execute(
            "SELECT game_id,player_id,ppa_all FROM game_player_ppa WHERE season >= ? AND ppa_all IS NOT NULL",
            (int(start_season),),
        )
        return {(int(r["game_id"]), str(r["player_id"])): float(r["ppa_all"]) for r in rows}


def _season_environment(rows: list[dict[str, Any]]) -> dict[int, dict[str, tuple[float, float]]]:
    """season -> {"ppa": (mean, stdev), "ypa": (...), "td_int_margin": (...)}
    over qualifying starts that season -- what each individual game gets
    standardized against before it can move a rating or feed the composite."""
    by_season: dict[int, dict[str, list[float]]] = defaultdict(lambda: {"ppa": [], "ypa": [], "margin": []})
    for row in rows:
        bucket = by_season[row["season"]]
        if row.get("ppa_all") is not None:
            bucket["ppa"].append(row["ppa_all"])
        if row.get("pass_yards") is not None and row["attempts"]:
            bucket["ypa"].append(row["pass_yards"] / row["attempts"])
        if row.get("pass_td") is not None and row.get("pass_int") is not None:
            bucket["margin"].append(row["pass_td"] - row["pass_int"])

    def stats(values: list[float]) -> tuple[float, float]:
        if not values:
            return 0.0, 1.0
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        return mean, (math.sqrt(var) or 1.0)

    return {
        season: {
            "ppa": stats(b["ppa"]), "ypa": stats(b["ypa"]), "margin": stats(b["margin"]),
        }
        for season, b in by_season.items()
    }


def build(repository: CFBRepository, *, start_season: int = 2015) -> int:
    initialize(repository)
    games = _games(repository, start_season)
    starters = _starters(repository, start_season)
    passing = _passing_lines(repository, start_season)
    ppa = _ppa_lookup(repository, start_season)

    # First pass: assemble every qualifying start's raw line so the
    # per-season environment (mean/stdev) can be computed before anything
    # is rated -- every game's z-score needs its whole season's population.
    starts: list[dict[str, Any]] = []
    for game in games:
        sides = starters.get(int(game["game_id"]))
        if not sides:
            continue
        for side, team_key, opp_key, team_id_key in (
            ("home", "home_team", "away_team", "home_team_id"),
            ("away", "away_team", "home_team", "away_team_id"),
        ):
            team = str(game[team_key])
            entry = sides.get(team)
            if not entry:
                continue
            player_id, name, attempts = entry
            line = passing.get((int(game["game_id"]), player_id), {})
            starts.append({
                "game_id": int(game["game_id"]), "season": int(game["season"]),
                "week": int(game["week"]), "start_date": str(game["start_date"]),
                "side": side, "player_id": player_id, "name": name,
                "team": team, "opponent": str(game[opp_key]), "attempts": attempts,
                "pass_yards": line.get("yards"), "pass_td": line.get("td"), "pass_int": line.get("int"),
                "ppa_all": ppa.get((int(game["game_id"]), player_id)),
                "opponent_pregame_elo": (
                    float(game["away_pregame_elo"]) if side == "home" and game["away_pregame_elo"] is not None
                    else float(game["home_pregame_elo"]) if side == "away" and game["home_pregame_elo"] is not None
                    else None
                ),
                "team_won": (
                    1 if (game["home_points"] > game["away_points"]) == (side == "home") else 0
                    if game["home_points"] != game["away_points"] else None
                ),
            })

    environment = _season_environment(starts)
    starts.sort(key=lambda r: (r["season"], r["start_date"], r["game_id"], r["side"]))

    ratings: dict[str, float] = {}
    names: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    history = []

    for row in starts:
        env = environment[row["season"]]
        ppa_mean, ppa_sd = env["ppa"]
        ypa_mean, ypa_sd = env["ypa"]
        margin_mean, margin_sd = env["margin"]

        player_id = row["player_id"]
        names[player_id] = row["name"] or names.get(player_id, player_id)
        pre_rating = ratings.get(player_id, BASE)

        ppa_z = ypa_z = margin_z = None
        if row["ppa_all"] is not None:
            ppa_z = (row["ppa_all"] - ppa_mean) / ppa_sd
        if row["pass_yards"] is not None and row["attempts"]:
            ypa_z = (row["pass_yards"] / row["attempts"] - ypa_mean) / ypa_sd
        if row["pass_td"] is not None and row["pass_int"] is not None:
            margin_z = (row["pass_td"] - row["pass_int"] - margin_mean) / margin_sd

        if ppa_z is not None:
            game_rating = BASE + ppa_z * RATING_SCALE
            post_rating = (1 - ALPHA) * pre_rating + ALPHA * game_rating
        else:
            # No PPA for this start (e.g. a game CFBD hasn't scored yet) --
            # rating carries forward unchanged rather than guessing.
            post_rating = pre_rating
        ratings[player_id] = post_rating

        record = records.setdefault(player_id, {"starts": 0, "wins": 0, "losses": 0, "ties": 0})
        record["starts"] += 1
        if row["team_won"] == 1:
            record["wins"] += 1
        elif row["team_won"] == 0:
            record["losses"] += 1
        else:
            record["ties"] += 1
        record["last_team"] = row["team"]
        record["last_game_id"] = row["game_id"]

        history.append((
            row["game_id"], row["season"], row["week"], row["start_date"], row["side"],
            player_id, row["team"], row["opponent"], row["attempts"],
            row["pass_yards"], row["pass_td"], row["pass_int"], row["ppa_all"],
            row["opponent_pregame_elo"], row["team_won"],
            ppa_z, ypa_z, margin_z, pre_rating, post_rating,
        ))

    stamp = datetime.now(timezone.utc).isoformat()
    with closing(repository._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM cfb_qb_elo_games")
        connection.execute("DELETE FROM cfb_qb_elo_ratings")
        connection.executemany(
            """INSERT INTO cfb_qb_elo_games VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            history,
        )
        connection.executemany(
            "INSERT INTO cfb_qb_elo_ratings VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    player_id, names[player_id], rating,
                    records[player_id]["starts"], records[player_id]["wins"],
                    records[player_id]["losses"], records[player_id]["ties"],
                    records[player_id]["last_team"], records[player_id]["last_game_id"], stamp,
                )
                for player_id, rating in ratings.items()
            ],
        )
        connection.commit()
    return len(history)


def current_rating(
    repository: CFBRepository, *, season: int, team: str
) -> tuple[float | None, dict[str, Any]]:
    """Best current QB rating using observed in-season role evidence.

    The QB Elo game table contains completed starts only. For an upcoming
    game, carry the player's current rating forward and identify the likely
    current QB from recent observed usage among players still on the
    current roster. Shared by Engine A's live HC/QB signal and margin-v2's
    qb_diff feature, so both read the same QB for the same game.
    """
    roles = observed_depth_roles(repository, str(team), int(season))
    with repository._reader() as connection:
        rows = [
            dict(row) for row in connection.execute(
                """SELECT p.player_id,p.first_name,p.last_name,r.rating,r.starts
                   FROM players p
                   LEFT JOIN cfb_qb_elo_ratings r
                     ON CAST(r.player_id AS TEXT)=CAST(p.player_id AS TEXT)
                   WHERE p.season=? AND p.team=? AND UPPER(COALESCE(p.position,''))='QB'""",
                (int(season), str(team)),
            )
        ]
    candidates = []
    for row in rows:
        if row.get("rating") is None:
            continue
        role = roles.get(str(row["player_id"])) or {}
        candidates.append((
            float(role.get("observed_score") or 0.0),
            int(role.get("observed_games") or 0),
            int(row.get("starts") or 0),
            row,
            role,
        ))
    if not candidates:
        return None, {"player": None, "source": "current_roster_qb_unrated"}
    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    _, _, _, row, role = candidates[0]
    name = f'{row.get("first_name") or ""} {row.get("last_name") or ""}'.strip()
    return float(row["rating"]), {
        "player_id": str(row["player_id"]),
        "player": name or None,
        "observed_games": int(role.get("observed_games") or 0),
        "observed_confidence": role.get("confidence"),
        "source": "observed_current_qb_rating",
    }


def _zscore(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    n = len(values)
    mean = sum(values.values()) / n
    variance = sum((v - mean) ** 2 for v in values.values()) / n
    stdev = math.sqrt(variance) or 1.0
    return {k: (v - mean) / stdev for k, v in values.items()}


def leaderboard(repository: CFBRepository, *, min_starts: int = MIN_STARTS_FOR_COMPOSITE) -> list[dict[str, Any]]:
    initialize(repository)
    with repository._reader() as connection:
        ratings = [dict(r) for r in connection.execute("SELECT * FROM cfb_qb_elo_ratings")]
        games = [dict(r) for r in connection.execute("SELECT * FROM cfb_qb_elo_games")]

    per_qb: dict[str, dict[str, list[float]]] = defaultdict(lambda: {
        "ppa_z": [], "ypa_z": [], "margin_z": [], "opponent_elo": [],
    })
    for game in games:
        bucket = per_qb[game["player_id"]]
        for key, field in (("ppa_z", "season_ppa_z"), ("ypa_z", "season_ypa_z"), ("margin_z", "season_td_int_margin_z")):
            if game[field] is not None:
                bucket[key].append(game[field])
        if game["opponent_pregame_elo"] is not None:
            bucket["opponent_elo"].append(game["opponent_pregame_elo"])

    qualifying = {r["player_id"]: r for r in ratings if r["starts"] >= min_starts}
    elo_z = _zscore({pid: r["rating"] for pid, r in qualifying.items()})
    ypa_index = {pid: (sum(per_qb[pid]["ypa_z"]) / len(per_qb[pid]["ypa_z"]) if per_qb[pid]["ypa_z"] else 0.0)
                 for pid in qualifying}
    margin_index = {pid: (sum(per_qb[pid]["margin_z"]) / len(per_qb[pid]["margin_z"]) if per_qb[pid]["margin_z"] else 0.0)
                    for pid in qualifying}
    opponent_elo = {pid: (sum(per_qb[pid]["opponent_elo"]) / len(per_qb[pid]["opponent_elo"])
                          if per_qb[pid]["opponent_elo"] else BASE)
                    for pid in qualifying}
    win_pct = {pid: (r["wins"] / max(1, r["wins"] + r["losses"])) for pid, r in qualifying.items()}

    ypa_z = _zscore(ypa_index)
    margin_z = _zscore(margin_index)
    opp_elo_z = _zscore(opponent_elo)
    win_pct_z = _zscore(win_pct)

    rows = []
    for player_id, rating in qualifying.items():
        components = {
            "elo_z": elo_z.get(player_id, 0.0),
            "yards_per_attempt_index_z": ypa_z.get(player_id, 0.0),
            "td_int_margin_index_z": margin_z.get(player_id, 0.0),
            "opponent_elo_z": opp_elo_z.get(player_id, 0.0),
            "team_win_pct_z": win_pct_z.get(player_id, 0.0),
        }
        rows.append({
            "player_id": player_id, "name": rating["name"], "last_team": rating["last_team"],
            "elo": round(rating["rating"], 1), "starts": rating["starts"],
            "record": f"{rating['wins']}-{rating['losses']}" + (f"-{rating['ties']}" if rating["ties"] else ""),
            "yards_per_attempt_index": round(ypa_index[player_id], 4),
            "td_int_margin_index": round(margin_index[player_id], 4),
            "opponent_avg_elo": round(opponent_elo[player_id], 1),
            "team_win_pct": round(win_pct[player_id], 4),
            "components": {k: round(v, 4) for k, v in components.items()},
            "composite_rating": round(sum(components.values()) / len(components), 4),
        })
    rows.sort(key=lambda r: r["composite_rating"], reverse=True)
    return rows
