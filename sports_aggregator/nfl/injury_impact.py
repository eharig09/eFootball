"""Quantified NFL injury impact: "starter-equivalents" missing and what they cost.

A team's injury index is the sum, over its Out / Doubtful / Questionable players,
of (chance the player is lost) x (how much he normally plays), where how much
he normally plays is his mean snap share over his previous PRIOR_GAMES games.
Read it as the number of full-time starters the team is expected to be without:
2.0 means about two every-down players missing.

Exactly the weights and importance definition the walk-forward backtest in
availability_ablation.py used, so the points-per-starter slope calibrated here
(`calibrate`) describes the same quantity the live panel shows.

Scope decisions that matter:
* Quarterbacks are excluded from the index. qb_player_ablation already models
  the quarterback, and double counting him would overstate every QB injury.
* IR / PUP / suspended players are listed but not counted. Their absence has
  been visible in the team's recent results for weeks, so it is already in the
  forecast's form inputs; only the *newly* unavailable are a fresh adjustment.
* The points conversion is a single pooled slope. The backtest found splitting
  it by position group does worse (the data cannot support it), so groups are
  shown for context only.
* The swing is applied to Football Lab's live margin (live_projection.py), and so to
  its win probability and picks, only for games inside INJURY_WINDOW_DAYS of
  kickoff -- the backtest used each game-week's final report, and an injury
  list six days out is not that. The pre-injury margin is kept alongside.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date
import math
from typing import Any

from sports_aggregator.nfl.naming import normalize_name
from sports_aggregator.nfl.repository import NFLRepository

PRIOR_GAMES = 6
STATUS_WEIGHT = {"Out": 1.0, "Doubtful": 0.85, "Questionable": 0.2}
#: Designations that have been in the team's results for a while already.
LONG_TERM = {"IR", "PUP", "SUSP", "NFI", "RESERVE"}
GROUPS = {
    "Offensive line": {"T", "G", "C", "OL", "OT", "OG"},
    "Skill": {"WR", "TE", "RB", "FB"},
    "Front seven": {"DE", "DT", "NT", "DL", "EDGE", "LB", "OLB", "ILB", "MLB"},
    "Secondary": {"CB", "S", "SS", "FS", "DB"},
}
POSITION_GROUP = {pos: group for group, positions in GROUPS.items() for pos in positions}

#: From `projection_cli injury-calibrate` (2013-2025, residuals of the walk-forward QB-aware
#: stack; 2,632 games). Points of home margin per starter-equivalent out.
CALIBRATION: dict[str, Any] = {
    "points_per_starter": 0.762, "standard_error": 0.184, "t": 4.15, "games": 2632,
    "market_points_per_starter": 0.281, "market_t": 1.56,
    "seasons_same_sign": 8, "seasons": 10, "window": "2013-2025",
}

#: Games further out than this keep the unadjusted margin (the report is not final yet).
INJURY_WINDOW_DAYS = 6

MARGIN_SIGMA = 13.07  # live football_lab margin_sigma (Gaussian out-of-fold)


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def evidence_label(calibration: dict[str, Any] | None = None) -> dict[str, Any]:
    """How loudly the page may talk about the points number."""
    c = calibration or CALIBRATION
    t, slope = c.get("t"), c.get("points_per_starter")
    if slope is None or t is None:
        return {"level": "uncalibrated", "text": "Points are not calibrated yet; showing the index only."}
    if abs(t) >= 2.58:
        level, head = "supported", (
            f"Across {c['games']:,} games ({c['window']}), each starter-equivalent out cost about "
            f"{slope:.2f} points beyond what Football Lab's inputs already say (t = {t:.1f}, same direction "
            f"in {c['seasons_same_sign']} of {c['seasons']} seasons).")
    elif abs(t) >= 1.64:
        level, head = "borderline", (
            f"Borderline in the {c['window']} backtest (t = {t:.1f}); treat the points as a rough guide.")
    else:
        level, head = "weak", (
            f"The {c['window']} backtest could not tell this from zero (t = {t:.1f}); "
            "the index is real, the points are a guess.")
    market = c.get("market_points_per_starter")
    if market is not None and slope:
        priced = max(0.0, min(1.0, 1.0 - market / slope))
        head += (f" Closing lines already reflect roughly {priced:.0%} of it, so this is context for "
                 "reading the number, not a betting edge.")
    return {"level": level, "text": head}


# ------------------------------------------------------------------ live

def _group(position: str | None) -> str | None:
    return POSITION_GROUP.get(str(position or "").upper())


class SnapHistory:
    """Recent snap shares of every currently listed player, loaded in one query.

    snap_counts is indexed by season, not by player name, so a lookup per injured
    player scanned the whole table (12 lookups cost 4.5 s on a game page). One
    query for the league's listed names over this and the prior season is ~50 ms
    and is shared by every game scored in the same request.
    """

    def __init__(self, repository: NFLRepository, season: int, week: int) -> None:
        self.season, self.week = int(season), int(week)
        self._games: dict[str, list[float]] = {}
        with closing(repository._connect()) as connection:
            names = sorted({normalize_name(r[0]) for r in connection.execute(
                "SELECT DISTINCT player_name FROM injury_reports WHERE season=?", (self.season,))})
            for start in range(0, len(names), 500):  # stay under SQLite's variable limit
                chunk = names[start:start + 500]
                rows = connection.execute(
                    f"""SELECT normalized_name,season,week,offense_pct,defense_pct FROM snap_counts
                        WHERE season IN (?,?) AND normalized_name IN ({','.join('?' * len(chunk))})""",
                    (self.season, self.season - 1, *chunk)).fetchall()
                by_name: dict[str, list[tuple[int, int, float]]] = {}
                for r in rows:
                    if (r["season"], r["week"]) < (self.season, self.week):
                        by_name.setdefault(r["normalized_name"], []).append(
                            (r["season"], r["week"], max(r["offense_pct"] or 0.0, r["defense_pct"] or 0.0)))
                for name, games in by_name.items():
                    games.sort(reverse=True)
                    self._games[name] = [share for _, _, share in games[:PRIOR_GAMES]]

    def importance(self, normalized_name: str) -> tuple[float | None, int]:
        """Mean snap share over the last PRIOR_GAMES games before the target week; None = no history."""
        games = self._games.get(normalized_name)
        return (sum(games) / len(games), len(games)) if games else (None, 0)


def team_impact(repository: NFLRepository, season: int, week: int, team: str, *,
                history: SnapHistory | None = None) -> dict[str, Any]:
    """One team's injury index and the players behind it."""
    history = history or SnapHistory(repository, season, week)
    players, unrated = [], []
    for injury in repository.team_injuries(int(season), team):
        designation = str(injury.get("designation") or "").upper()
        status = injury.get("status")
        position = str(injury.get("position") or "").upper()
        base = {"player": injury.get("player_name"), "position": position, "status": status,
                "gsis_id": injury.get("gsis_id"),
                "detail": " · ".join(str(v) for v in (injury.get("injury_type"), injury.get("practice_status")) if v)}
        if designation in LONG_TERM:
            players.append({**base, "counts": False, "reason": "Long-term; already in recent form"})
            continue
        weight = STATUS_WEIGHT.get(status)
        if weight is None:
            continue
        if position == "QB":
            players.append({**base, "counts": False, "reason": "Quarterbacks are modelled separately",
                            "weight": weight})
            continue
        importance, games = history.importance(normalize_name(injury.get("player_name")))
        if importance is None:
            unrated.append({**base, "weight": weight})
            continue
        players.append({**base, "counts": True, "weight": weight, "importance": importance,
                        "games": games, "starters": weight * importance,
                        "group": _group(position)})
    counted = [p for p in players if p["counts"]]
    index = sum(p["starters"] for p in counted)
    groups = {name: round(sum(p["starters"] for p in counted if p["group"] == name), 2) for name in GROUPS}
    slope = CALIBRATION["points_per_starter"]
    for p in counted:
        p["points"] = None if slope is None else round(-p["starters"] * slope, 2)
    counted.sort(key=lambda p: -p["starters"])
    others = sorted((p for p in players if not p["counts"]), key=lambda p: p["player"] or "")
    return {"team": team, "index": round(index, 2), "groups": groups,
            "players": counted, "excluded": others, "unrated": unrated,
            "points": None if slope is None else round(-index * slope, 2)}


def game_impact(repository: NFLRepository, game: dict[str, Any], *,
                model_margin: float | None = None,
                history: SnapHistory | None = None) -> dict[str, Any]:
    """Matchup view: both teams' indexes, the net swing, and the overlay on Football Lab."""
    season, week = int(game["season"]), int(game["week"])
    history = history or SnapHistory(repository, season, week)
    away = team_impact(repository, season, week, game["away_team"], history=history)
    home = team_impact(repository, season, week, game["home_team"], history=history)
    if not (away["players"] or away["unrated"] or away["excluded"]
            or home["players"] or home["unrated"] or home["excluded"]):
        return {"available": False}
    slope = CALIBRATION["points_per_starter"]
    # Positive gap = the away team is missing more, which favours the home side.
    gap = away["index"] - home["index"]
    swing = None if slope is None else round(gap * slope, 2)
    packet: dict[str, Any] = {
        "available": True, "away": away, "home": home, "gap": round(gap, 2),
        "home_swing": swing, "evidence": evidence_label(), "calibration": CALIBRATION,
        "adjusted": None,
    }
    if swing is not None and model_margin is not None:
        base = float(model_margin)
        adjusted = base + swing
        packet["adjusted"] = {
            "margin": round(base, 2), "adjusted_margin": round(adjusted, 2),
            "home_win_probability": round(normal_cdf(base / MARGIN_SIGMA), 4),
            "adjusted_home_win_probability": round(normal_cdf(adjusted / MARGIN_SIGMA), 4),
        }
    return packet


def forecast_swing(repository: NFLRepository, game: dict[str, Any], *,
                   today: "date | None" = None,
                   history: SnapHistory | None = None) -> dict[str, Any]:
    """Margin adjustment for Football Lab: {"swing", "status", ...}; swing is 0.0 when none applies.

    status says why: "applied", "completed" (already played),
    "no_report" (nothing stored for either team),
    "too_early" (kickoff outside the window), or "weak_evidence"/"uncalibrated".
    """
    from datetime import date as _date, datetime

    if game.get("completed"):
        return {"swing": 0.0, "status": "completed"}
    level = evidence_label()["level"]
    if level not in ("supported", "borderline"):
        return {"swing": 0.0, "status": level}
    try:
        kickoff = datetime.fromisoformat(str(game.get("game_date"))).date()
    except ValueError:
        return {"swing": 0.0, "status": "too_early"}
    if (kickoff - (today or _date.today())).days > INJURY_WINDOW_DAYS:
        return {"swing": 0.0, "status": "too_early"}
    packet = game_impact(repository, game, history=history)
    if not packet["available"] or packet["home_swing"] is None:
        return {"swing": 0.0, "status": "no_report"}
    return {"swing": packet["home_swing"], "status": "applied",
            "away_starters_out": packet["away"]["index"],
            "home_starters_out": packet["home"]["index"]}


# ------------------------------------------------------------- backtest

def calibrate(repository: NFLRepository, *, start_season: int = 2013,
              end_season: int = 2025) -> dict[str, Any]:
    """Points of home margin per starter-equivalent, against the model and the market.

    Two slopes on the home-minus-away gap in starter-equivalents out
    (`av_total_diff`, positive = away team missing more):
      * against the walk-forward residual of the QB-aware stack -- what the
        injury information adds to Football Lab;
      * against actual margin minus the closing spread -- whether the market
        already prices it (a slope near zero says yes).
    """
    import numpy as np

    from sports_aggregator.nfl.availability_ablation import add_availability
    from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
    from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, build_rows

    rows = build_rows(repository, start_season, end_season)
    add_availability(rows, repository, start_season, end_season)
    with closing(repository._connect()) as connection:
        spreads = {str(r["game_id"]): r["spread_line"] for r in connection.execute(
            "SELECT game_id,spread_line FROM games WHERE season BETWEEN ? AND ?",
            (int(start_season), int(end_season)))}
    sample = [r for r in rows if r.get("actual_margin") is not None
              and r.get("av_total_diff") is not None
              and all(r.get(k) is not None for k in SHRUNK_CHANGE)]

    def slope(xs, ys):
        x, y = np.asarray(xs, float), np.asarray(ys, float)
        xc = x - x.mean()
        denom = float((xc ** 2).sum())
        if denom == 0 or len(x) < 30:
            return None
        b = float((xc * (y - y.mean())).sum() / denom)
        resid = (y - y.mean()) - b * xc
        se = math.sqrt(float((resid ** 2).sum()) / (len(x) - 2) / denom)
        return {"slope": b, "se": se, "t": b / se if se else None, "n": len(x)}

    stack_x, stack_y, mkt_x, mkt_y, per_season = [], [], [], [], {}
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        test = [r for r in sample if int(r["season"]) == season]
        model = _fit(train, SHRUNK_CHANGE) if len(train) >= 100 else None
        if model is None:
            continue
        xs, ys = [], []
        for r in test:
            xs.append(r["av_total_diff"])
            ys.append(r["actual_margin"] - _predict(model, r))
            line = spreads.get(str(r["game_id"]))
            if line is not None:
                mkt_x.append(r["av_total_diff"])
                mkt_y.append(r["actual_margin"] - float(line))
        stack_x += xs
        stack_y += ys
        fit = slope(xs, ys)
        if fit:
            per_season[season] = round(fit["slope"], 3)
    model_fit, market_fit = slope(stack_x, stack_y), slope(mkt_x, mkt_y)
    deciles = []
    if stack_x:
        order = np.argsort(stack_x)
        for chunk in np.array_split(order, 5):
            deciles.append({"mean_gap": round(float(np.mean([stack_x[i] for i in chunk])), 3),
                            "mean_residual": round(float(np.mean([stack_y[i] for i in chunk])), 3),
                            "n": int(len(chunk))})
    return {
        "window": f"{start_season}-{end_season}", "games": len(stack_x),
        "points_per_starter": round(model_fit["slope"], 3) if model_fit else None,
        "standard_error": round(model_fit["se"], 3) if model_fit else None,
        "t": round(model_fit["t"], 2) if model_fit else None,
        "market_points_per_starter": round(market_fit["slope"], 3) if market_fit else None,
        "market_standard_error": round(market_fit["se"], 3) if market_fit else None,
        "market_t": round(market_fit["t"], 2) if market_fit and market_fit["t"] else None,
        "market_games": len(mkt_x),
        "seasons": len(per_season),
        "seasons_same_sign": (sum(v > 0 for v in per_season.values())
                              if (model_fit and model_fit["slope"] > 0)
                              else sum(v < 0 for v in per_season.values())),
        "per_season_slope": per_season,
        "quintiles_by_gap": deciles,
        "note": ("gap = away starter-equivalents out minus home; slope is points of home margin "
                 "per one starter-equivalent. Residual = actual margin minus the walk-forward "
                 "QB-aware stack (model) or minus the closing spread (market)."),
    }


# ------------------------------------------------------------------ tables

def tables(packet: dict[str, Any], season: int) -> dict[str, Any]:
    """One shared-kit Table per team: who is missing and what each absence is worth."""
    from sports_aggregator.tables import Column, Table

    away, home = packet["away"]["team"], packet["home"]["team"]
    swing, adj = packet["home_swing"], packet["adjusted"]

    def signed(value: float) -> str:
        return f"{value:+.1f}"

    summary_rows = [
        {"measure": "Starters out", "value": f"{packet['away']['index']:.2f} / {packet['home']['index']:.2f}",
         "note": f"{away} / {home}"},
        {"measure": "Estimated margin cost",
         "value": ("—" if swing is None else
                   f"{packet['away']['points']:+.1f} / {packet['home']['points']:+.1f}"),
         "note": f"{away} / {home}, points"},
        {"measure": "Net swing",
         "value": "—" if swing is None else f"{abs(swing):.1f} pts",
         "note": ("not calibrated" if swing is None else
                  f"toward {home if swing > 0 else away if swing < 0 else 'neither side'}")},
    ]
    if adj:
        summary_rows += [
            {"measure": f"Football Lab {home} margin",
             "value": f"{signed(adj['margin'])} → {signed(adj['adjusted_margin'])}",
             "note": "before → after injuries"},
            {"measure": f"{home} win probability",
             "value": (f"{adj['home_win_probability']:.0%} → "
                       f"{adj['adjusted_home_win_probability']:.0%}"),
             "note": "Gaussian on the margin"},
        ]
    summary = Table([
        Column("measure", "Measure", "text", emphasis=True),
        Column("value", "Value", "text", align="right"),
        Column("note", "Reading", "text"),
    ], summary_rows, dense=True, sortable=False)

    out = {"summary": summary}
    for side in ("away", "home"):
        team = packet[side]
        rows = [{
            "player": p["player"], "position": p["position"] or "—", "status": p["status"],
            "snap_share": p["importance"], "starters": p["starters"], "points": p["points"],
            "player_url": (f"/nfl/players/{p['gsis_id']}/?season={season}" if p.get("gsis_id") else None),
            "points_class": "loss" if (p["points"] or 0) < 0 else "",
        } for p in team["players"]]
        total = {"player": "Team total", "starters": team["index"], "points": team["points"]} if rows else None
        out[side] = Table([
            Column("player", "Player", "text", emphasis=True),
            Column("position", "Pos", "text"),
            Column("status", "Status", "text"),
            Column("snap_share", "Typical snaps", "rate",
                   title=f"Mean snap share over his last {PRIOR_GAMES} games"),
            Column("starters", "Starters out", "f2",
                   title="Chance he is lost x typical snap share; 1.0 = one every-down starter"),
            Column("points", "Pts", "signed2", title="Estimated margin cost to this team"),
        ], rows, total_row=total, dense=True, sortable=False,
            empty="No counted absences: nobody non-QB is listed Out, Doubtful or Questionable with snap history.")
    return out
