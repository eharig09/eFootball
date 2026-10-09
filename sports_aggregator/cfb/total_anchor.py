"""A market-free total anchor that pulls the xPoints total, never replaces it.

Two components, both built only from games that kicked off before the one being projected, against FBS
opponents only (FCS blowouts inflate scoring history and bias totals up):

  season midpoint   each team's recency-weighted points scored averaged with the opponent's points allowed
  opponent-adjusted expected points from pregame EPA/play (own offence, opponent defence, pace, home edge) from a
  totals            ridge fitted on earlier seasons, plus a team "total tendency": how far each team's previous
                    games finished over or under what the same pregame EPA/play said they should, shrunk

anchor = 0.7 * season midpoint + 0.3 * opponent-adjusted totals.

A walk-forward test over 3,170 FBS-vs-FBS games (2022-2025) found the xPoints total (calibrated) misses by 13.18
points, the anchor by 12.93, and that a 50% pull toward the anchor (12.98) keeps about 80% of that gain while the
xPoints model still leads. Last-three scoring form, the NFL's second piece, got zero weight in every fit and is
not used. Pulling the total this way moves the margin only through `adjust`'s quality rule below, which the same
test found neutral for accuracy, so it is deliberately small.

`adjust` moves the total toward the anchor and splits the change by quality: when the total goes UP the better
offence picks up the extra points, when it goes DOWN the better defence gives up fewer of them.

Nothing here reads the betting market.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import numpy as np

from sports_aggregator.cfb import derived_cache
from sports_aggregator.cfb.repository import schema_once

LOGGER = logging.getLogger(__name__)

MODEL_VERSION = "cfb-total-anchor-v1"
WINDOW = 24             # team-games looked back; only those against FBS opponents are used
TENDENCY_WINDOW = 12    # previous games whose over/under residual feeds a team's tendency
HALF_LIFE = 8.0
LAM = math.log(2.0) / HALF_LIFE
MIN_GAMES = 4           # FBS-opponent games a team needs before its snapshot counts
SHRINK_K = 4.0          # pseudo-games pulling a team's tendency toward zero
FIRST_TRAIN_SEASON = 2018
HISTORY_FROM_SEASON = FIRST_TRAIN_SEASON - 1    # the dataset and the live snapshot both start here, so they agree
MIN_TRAIN_GAMES = 400   # below this the expected-points ridge is not trusted and no anchor is served
SEASON_WEIGHT, ADJUSTED_WEIGHT = 0.7, 0.3
PULL_LAMBDA = 0.5       # share of the gap to the anchor the total moves; never 1, the anchor does not conclude
SHIFT_KAPPA = 0.15      # how much of the change is routed to the better offence/defence
O_SCALE, D_SCALE = 0.138, 0.121     # 1 sd of the home-minus-away offence / defence EPA-per-play edges

FBS_CONFERENCES = frozenset({
    "ACC", "American Athletic", "Big 12", "Big Ten", "Conference USA", "FBS Independents",
    "Mid-American", "Mountain West", "Pac-12", "SEC", "Sun Belt",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_total_anchor_dataset (
  game_id INTEGER NOT NULL, team TEXT NOT NULL, opponent TEXT NOT NULL,
  season INTEGER NOT NULL, start_date TEXT NOT NULL,
  is_home INTEGER NOT NULL, neutral INTEGER NOT NULL, fbs_game INTEGER NOT NULL,
  ready INTEGER NOT NULL, n_prior INTEGER,
  pf REAL, pa REAL, off_epa REAL, def_epa REAL, plays REAL,
  points_for REAL NOT NULL, points_against REAL NOT NULL, built_at TEXT NOT NULL,
  PRIMARY KEY(game_id, team)
);
CREATE INDEX IF NOT EXISTS idx_cfb_total_anchor_team ON cfb_total_anchor_dataset(team, start_date);
"""


@schema_once("total_anchor")
def initialize(repository) -> None:
    repository.initialize()
    from sports_aggregator.cfb.team_game_advanced import initialize as initialize_advanced
    initialize_advanced(repository)
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


# ---------------------------------------------------------------------------------------------- snapshots

def _wmean(values: list[float]) -> float | None:
    n = len(values)
    if not n:
        return None
    weights = [math.exp(-LAM * (n - 1 - i)) for i in range(n)]
    return sum(w * v for w, v in zip(weights, values)) / sum(weights)


def snapshot(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A team's pregame state from its previous team-games, oldest first; None until it has enough FBS games.

    Each history item: pf, pa, epa, opp_epa, plays, opp_fbs."""
    games = [item for item in history if item["opp_fbs"]]
    if len(games) < MIN_GAMES:
        return None
    return {"n": len(games),
            "pf": _wmean([g["pf"] for g in games]), "pa": _wmean([g["pa"] for g in games]),
            "off_epa": _wmean([g["epa"] for g in games]), "def_epa": _wmean([g["opp_epa"] for g in games]),
            "plays": _wmean([g["plays"] for g in games])}


def _history_item(row: dict[str, Any], *, team_is_home: bool) -> dict[str, Any] | None:
    if row["epa"] is None or row["opp_epa"] is None:
        return None
    opponent_conference = row["away_conference"] if team_is_home else row["home_conference"]
    return {"pf": float(row["home_points"] if team_is_home else row["away_points"]),
            "pa": float(row["away_points"] if team_is_home else row["home_points"]),
            "epa": float(row["epa"]), "opp_epa": float(row["opp_epa"]), "plays": float(row["plays"] or 0),
            "opp_fbs": opponent_conference in FBS_CONFERENCES}


_LIVE_SQL = """
SELECT g.game_id,g.start_date,g.home_team,g.home_points,g.away_points,g.home_conference,g.away_conference,
       a.epa_per_play AS epa,a.scrimmage_plays AS plays,o.epa_per_play AS opp_epa
FROM cfb_team_game_advanced a
JOIN games g ON g.game_id=a.game_id
JOIN cfb_team_game_advanced o ON o.game_id=a.game_id AND o.team=a.opponent
     AND o.model_version=a.model_version AND o.metric_version=a.metric_version
WHERE a.team=? AND a.model_version=? AND a.metric_version=? AND g.completed=1 AND g.start_date<?
  AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL AND g.season>=?
  AND a.epa_per_play IS NOT NULL AND o.epa_per_play IS NOT NULL
ORDER BY g.start_date DESC,g.game_id DESC LIMIT ?
"""


def live_snapshot(repository, team: str, before: str) -> dict[str, Any] | None:
    """The team's snapshot as of `before`, from the same tables and window the dataset is built with."""
    from sports_aggregator.cfb.team_game_advanced import METRIC_VERSION, MODEL_VERSION as EP
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(_LIVE_SQL, (team, EP, METRIC_VERSION, str(before), HISTORY_FROM_SEASON, WINDOW))]
    items = [_history_item(r, team_is_home=(r["home_team"] == team)) for r in reversed(rows)]
    return snapshot([i for i in items if i])


# ---------------------------------------------------------------------------------------------- dataset

_BULK_SQL = """
SELECT g.game_id,g.season,g.start_date,g.neutral_site,g.home_team,g.away_team,g.home_points,g.away_points,
       g.home_conference,g.away_conference,a.team,a.opponent,a.epa_per_play,a.scrimmage_plays
FROM games g JOIN cfb_team_game_advanced a ON a.game_id=g.game_id AND a.model_version=? AND a.metric_version=?
WHERE g.completed=1 AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL AND g.season>=?
ORDER BY g.start_date,g.game_id
"""


def build_dataset(repository, *, from_season: int = HISTORY_FROM_SEASON) -> dict[str, Any]:
    """Chronological pregame snapshots for every team-game; histories update only after a game is recorded."""
    from sports_aggregator.cfb.team_game_advanced import METRIC_VERSION, MODEL_VERSION as EP
    initialize(repository)
    with repository._reader() as connection:
        records = [dict(r) for r in connection.execute(_BULK_SQL, (EP, METRIC_VERSION, int(from_season)))]
    by_game: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    order: list[int] = []
    for r in records:
        if r["game_id"] not in by_game:
            order.append(r["game_id"])
        by_game[r["game_id"]][r["team"]] = r
    history: dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
    now = datetime.now(timezone.utc).isoformat()
    output = []
    for game_id in order:
        pair = by_game[game_id]
        any_row = next(iter(pair.values()))
        home, away = any_row["home_team"], any_row["away_team"]
        if home not in pair or away not in pair:
            continue
        fbs_game = (any_row["home_conference"] in FBS_CONFERENCES and any_row["away_conference"] in FBS_CONFERENCES)
        snaps = {team: snapshot(list(history[team])) for team in (home, away)}
        for team, opponent in ((home, away), (away, home)):
            snap = snaps[team]
            is_home = team == home
            output.append((
                game_id, team, opponent, int(any_row["season"]), any_row["start_date"], int(is_home),
                int(bool(any_row["neutral_site"])), int(fbs_game), int(snap is not None),
                snap["n"] if snap else None, *( (snap["pf"], snap["pa"], snap["off_epa"], snap["def_epa"], snap["plays"])
                                                 if snap else (None,) * 5),
                float(any_row["home_points"] if is_home else any_row["away_points"]),
                float(any_row["away_points"] if is_home else any_row["home_points"]), now))
        for team, opponent in ((home, away), (away, home)):
            item = _history_item({"epa": pair[team]["epa_per_play"], "opp_epa": pair[opponent]["epa_per_play"],
                                  "plays": pair[team]["scrimmage_plays"], **{k: any_row[k] for k in (
                                      "home_points", "away_points", "home_conference", "away_conference")}},
                                 team_is_home=(team == home))
            if item:
                history[team].append(item)
    with closing(repository._connect()) as connection:
        connection.execute("DELETE FROM cfb_total_anchor_dataset")
        connection.executemany("INSERT INTO cfb_total_anchor_dataset VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", output)
        connection.commit()
    return {"model_version": MODEL_VERSION, "rows": len(output),
            "ready_rows": sum(1 for row in output if row[8])}


# ---------------------------------------------------------------------------------------------- the expected-points ridge

def _side(own: dict[str, Any], opp: dict[str, Any], home_col: float) -> list[float]:
    return [1.0, own["off_epa"], opp["def_epa"], (own["plays"] + opp["plays"]) / 2.0, home_col]


def _home_col(neutral: bool) -> float:
    return 0.0 if neutral else 0.5


def fit_points_model(repository, season: int) -> np.ndarray | None:
    """Ridge of a side's points on its own offence EPA, the opponent's defence EPA, pace and home edge,
    fitted on FBS-vs-FBS games of seasons before `season`."""
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT h.neutral,h.pf h_pf,h.off_epa h_off,h.def_epa h_def,h.plays h_plays,h.points_for h_points,
                      a.pf a_pf,a.off_epa a_off,a.def_epa a_def,a.plays a_plays,a.points_for a_points
               FROM cfb_total_anchor_dataset h JOIN cfb_total_anchor_dataset a
                 ON a.game_id=h.game_id AND a.team=h.opponent
               WHERE h.is_home=1 AND h.ready=1 AND a.ready=1 AND h.fbs_game=1 AND h.season>=? AND h.season<?""",
            (FIRST_TRAIN_SEASON, int(season)))]
    if len(rows) < MIN_TRAIN_GAMES:
        return None
    X, y = [], []
    for r in rows:
        home = {"off_epa": r["h_off"], "def_epa": r["h_def"], "plays": r["h_plays"]}
        away = {"off_epa": r["a_off"], "def_epa": r["a_def"], "plays": r["a_plays"]}
        hc = _home_col(bool(r["neutral"]))
        X.append(_side(home, away, hc)); y.append(r["h_points"])
        X.append(_side(away, home, -hc)); y.append(r["a_points"])
    X, y = np.array(X), np.array(y)
    return np.linalg.solve(X.T @ X + 1e-6 * np.eye(X.shape[1]), X.T @ y)


def expected_points(beta: np.ndarray, home: dict[str, Any], away: dict[str, Any], neutral: bool) -> tuple[float, float]:
    hc = _home_col(neutral)
    return (float(np.array(_side(home, away, hc)) @ beta), float(np.array(_side(away, home, -hc)) @ beta))


def _tendency(residuals: list[float]) -> float:
    """Recency-weighted mean over/under residual, shrunk toward zero; oldest first."""
    n = len(residuals)
    if not n:
        return 0.0
    weights = [math.exp(-LAM * (n - 1 - i)) for i in range(n)]
    effective = sum(weights)
    return _wmean(residuals) * effective / (effective + SHRINK_K)


def team_tendency(repository, team: str, season: int, before: str, beta: np.ndarray) -> tuple[float, int]:
    """How far the team's previous games finished over (+) or under (-) what their own pregame EPA/play predicted."""
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT t.is_home,t.neutral,t.points_for+t.points_against actual,
                      t.off_epa t_off,t.def_epa t_def,t.plays t_plays,
                      o.off_epa o_off,o.def_epa o_def,o.plays o_plays
               FROM cfb_total_anchor_dataset t JOIN cfb_total_anchor_dataset o
                 ON o.game_id=t.game_id AND o.team=t.opponent
               WHERE t.team=? AND t.ready=1 AND o.ready=1 AND t.season IN (?,?) AND t.start_date<?
               ORDER BY t.start_date DESC LIMIT ?""",
            (team, int(season) - 1, int(season), str(before), TENDENCY_WINDOW))]
    residuals = []
    for r in reversed(rows):
        mine = {"off_epa": r["t_off"], "def_epa": r["t_def"], "plays": r["t_plays"]}
        theirs = {"off_epa": r["o_off"], "def_epa": r["o_def"], "plays": r["o_plays"]}
        home, away = (mine, theirs) if r["is_home"] else (theirs, mine)
        eh, ea = expected_points(beta, home, away, bool(r["neutral"]))
        residuals.append(float(r["actual"]) - (eh + ea))
    return _tendency(residuals), len(residuals)


# ---------------------------------------------------------------------------------------------- the anchor for a game

def for_game(repository, game: dict[str, Any]) -> dict[str, Any] | None:
    """The market-free anchor for one game, or None when either team lacks FBS history or the ridge is untrained."""
    home, away, season = game["home_team"], game["away_team"], int(game["season"])
    before = game.get("start_date") or datetime.now(timezone.utc).isoformat()
    h, a = live_snapshot(repository, home, before), live_snapshot(repository, away, before)
    if h is None or a is None:
        return None
    beta = derived_cache.derived(repository, "cfb_total_anchor_beta", lambda: fit_points_model(repository, season), season)
    if beta is None:
        return None
    neutral = bool(game.get("neutral_site"))
    midpoint_home, midpoint_away = (h["pf"] + a["pa"]) / 2.0, (a["pf"] + h["pa"]) / 2.0
    eh, ea = expected_points(beta, h, a, neutral)
    tend_home, n_home = team_tendency(repository, home, season, before, beta)
    tend_away, n_away = team_tendency(repository, away, season, before, beta)
    adjustment = (tend_home + tend_away) / 2.0
    midpoint_total, adjusted_total = midpoint_home + midpoint_away, eh + ea + adjustment
    return {
        "model_version": MODEL_VERSION,
        "total": SEASON_WEIGHT * midpoint_total + ADJUSTED_WEIGHT * adjusted_total,
        "season_midpoint_total": midpoint_total, "epa_expected_total": eh + ea,
        "opponent_adjusted_total": adjusted_total, "tendency_adjustment": adjustment,
        "tendencies": {home: tend_home, away: tend_away},
        "o_edge": h["off_epa"] - a["off_epa"],      # > 0: the home offence is better
        "d_edge": a["def_epa"] - h["def_epa"],      # > 0: the home defence is better (it allows less)
        "weights": {"season_midpoint": SEASON_WEIGHT, "opponent_adjusted_totals": ADJUSTED_WEIGHT},
        "games": {home: h["n"], away: a["n"]},
    }


def adjust(total: float, margin: float, anchor: dict[str, Any], *, pull: float = PULL_LAMBDA,
           kappa: float = SHIFT_KAPPA) -> dict[str, float]:
    """Move the total part-way to the anchor and route the change by quality.

    total' = total + pull * (anchor - total). The change D is split evenly between the sides, then `kappa` of it
    is steered: when D > 0 the home side gets extra in proportion to its offensive edge (and the away side gives
    it up when its offence is better); when D < 0 the side with the better defence gives up fewer points. Each
    edge is scaled to [-1, 1] by one standard deviation, so a larger quality gap moves the spread proportionally
    more. `margin` is home minus away."""
    change = pull * (float(anchor["total"]) - float(total))
    offence = float(np.clip(anchor["o_edge"] / O_SCALE, -1.0, 1.0))
    defence = float(np.clip(anchor["d_edge"] / D_SCALE, -1.0, 1.0))
    shift = kappa * change * offence if change > 0 else kappa * (-change) * defence
    return {"total": float(total) + change, "margin": float(margin) + 2.0 * shift,
            "change": change, "shift": shift, "offence_edge": offence, "defence_edge": defence}


# ---------------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os

    from dotenv import load_dotenv

    from sports_aggregator.cfb.repository import CFBRepository
    load_dotenv()
    parser = argparse.ArgumentParser(description="Rebuild the CFB total-anchor dataset")
    parser.add_argument("command", choices=("refresh",))
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    print(json.dumps(build_dataset(repository), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
