"""Offensive line against defensive front, from free (non-PFF) data.

Per team-game the cached nflverse play-by-play gives, for the OFFENSE (which is the offensive line's work):
    sacks and QB hits allowed per dropback, stuffed runs and tackles for loss per designed rush,
    pass and rush EPA, and (2021+, where the plays were charted) the share of dropbacks that were pressured.
The same numbers, seen from the other bench, are the DEFENSIVE FRONT's havoc: sacks, QB hits, stuffs and tackles for
loss it generated, and the pressure rate it created.

A side's expectation is the average of ITS OWN OFFENSE and THE OPPONENT'S DEFENSE (the matchup blend used throughout):

    expected sack rate on the home offense = (home OL sacks allowed + away front sacks generated) / 2

Availability is added separately: how many of a line's regular starters are listed out or doubtful that week, weighted
by how much they have played (injury reports 2009+, snap counts 2013+).

Everything is a pregame snapshot from earlier week batches, season-decayed, shrunk to the league mean.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl.availability_ablation import STATUS_WEIGHT, _importance, _snap_history
from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository

PBP_COLUMNS = ("game_id", "week", "season_type", "posteam", "defteam", "play_type", "qb_dropback", "sack", "qb_hit",
               "tackled_for_loss", "qb_scramble", "yards_gained", "epa")
PRIOR_GAMES = 3.0
MIN_CHARTED_DROPBACKS = 150      # a pressure-rate snapshot needs this many charted dropbacks behind it

KEYS = ("dropbacks", "sacks", "hits", "pass_epa", "rushes", "stuffs", "tfl", "rush_epa", "pressures", "press_db", "games")
#: name -> (numerator, denominator), each seen from the offense (off_*) and from the defense that faced it (def_*)
RATES = {
    "sack": ("sacks", "dropbacks"), "hit": ("hits", "dropbacks"), "stuff": ("stuffs", "rushes"),
    "tfl": ("tfl", "rushes"), "pass_epa": ("pass_epa", "dropbacks"), "rush_epa": ("rush_epa", "rushes"),
    "press": ("pressures", "press_db"),
}
OL_GROUP = frozenset({"T", "G", "C", "OL", "OT", "OG"})
DL_GROUP = frozenset({"DE", "DT", "NT", "DL", "EDGE"})


# ------------------------------------------------------------------------------------------ extraction
def extract_season(frame, season: int) -> list[dict[str, Any]]:
    """Team-game offensive-line numbers from one season's regular-season play-by-play."""
    frame = frame[(frame["season_type"] == "REG") & frame["posteam"].notna()]
    teams: dict[tuple[str, str], dict[str, float]] = {}

    def slot(game_id: str, team: str, week: int) -> dict[str, float]:
        key = (game_id, canon_team(team))
        if key not in teams:
            teams[key] = dict.fromkeys(KEYS, 0.0)
            teams[key].update(game_id=game_id, team=canon_team(team), week=int(week), season=season, games=1.0)
        return teams[key]

    drops = frame[frame["qb_dropback"] == 1]
    for (gid, week, team), g in drops.groupby(["game_id", "week", "posteam"], sort=False):
        t = slot(gid, team, week)
        t["dropbacks"] += len(g)
        t["sacks"] += float((g["sack"] == 1).sum())
        t["hits"] += float(((g["qb_hit"] == 1) & (g["sack"] != 1)).sum())      # hits on plays that were not sacks
        t["pass_epa"] += float(g["epa"].fillna(0.0).sum())
    rushes = frame[(frame["play_type"] == "run") & (frame["qb_scramble"] != 1)]
    for (gid, week, team), g in rushes.groupby(["game_id", "week", "posteam"], sort=False):
        t = slot(gid, team, week)
        t["rushes"] += len(g)
        t["stuffs"] += float((g["yards_gained"] <= 0).sum())
        t["tfl"] += float((g["tackled_for_loss"] == 1).sum())
        t["rush_epa"] += float(g["epa"].fillna(0.0).sum())
    return list(teams.values())


def load(start: int, end: int, cache: str | Path = "instance/nflverse_raw") -> list[dict[str, Any]]:
    import pandas as pd
    out: list[dict[str, Any]] = []
    for season in range(int(start), int(end) + 1):
        path = Path(cache) / f"pbp_{season}.parquet"
        if path.exists():
            out += extract_season(pd.read_parquet(path, columns=list(PBP_COLUMNS)), season)
    return out


def _pressure(repository: NFLRepository, start: int, end: int) -> dict[tuple[str, str], tuple[float, float]]:
    """(game, team) -> (pressured dropbacks, charted dropbacks); only seasons where the plays were charted (2021+)."""
    with closing(repository._connect()) as connection:
        return {(str(r["game_id"]), canon_team(r["team"])): (float(r["pressured"]), float(r["charted"]))
                for r in connection.execute(
                    """SELECT game_id, posteam team, SUM(was_pressure=1) pressured, SUM(was_pressure IS NOT NULL) charted
                       FROM nfl_plays WHERE is_pass=1 AND season_type='REG' AND season BETWEEN ? AND ?
                       GROUP BY game_id, posteam HAVING charted > 0""", (int(start), int(end)))}


# ------------------------------------------------------------------------------------------ snapshots
def _ratio(num: float, den: float) -> float:
    return num / den if den > 0 else 0.0


def _snapshot(records: list[dict[str, Any]], season: int, league: dict[str, float]) -> dict[str, float]:
    own, opp = dict.fromkeys(KEYS, 0.0), dict.fromkeys(KEYS, 0.0)
    for r in records:
        w = STATE_SEASON_DECAY ** max(0, season - r["season"])
        for k in KEYS:
            own[k] += w * r[k]
            opp[k] += w * r["opp_" + k]
    prior = {k: PRIOR_GAMES * league[k] for k in KEYS}
    out: dict[str, float] = {"charted": own["press_db"]}
    for name, (num, den) in RATES.items():
        out[f"off_{name}"] = _ratio(own[num] + prior[num], own[den] + prior[den])
        out[f"def_{name}"] = _ratio(opp[num] + prior[num], opp[den] + prior[den])
    return out


def pregame_snapshots(repository: NFLRepository, start: int, end: int,
                      cache: str | Path = "instance/nflverse_raw", with_actuals: bool = False):
    """game_id -> {'teams': (a, b), 'sides': {team: snapshot}} for every game from `start`, whole-week batches.

    With `with_actuals`, also returns the realised (game, team) numbers, for the mechanism and PFF checks.
    """
    lo = max(2009, start - 2)
    table = {(t["game_id"], t["team"]): t for t in load(lo, end, cache)}
    for key, (pressured, charted) in _pressure(repository, lo, end).items():
        if key in table:
            table[key]["pressures"], table[key]["press_db"] = pressured, charted
    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in table.values():
        by_game[t["game_id"]].append(t)
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for pair in by_game.values():
        if len(pair) != 2:
            continue
        for me, other in ((pair[0], pair[1]), (pair[1], pair[0])):
            rec = dict(me)
            rec.update({"opp_" + k: other[k] for k in KEYS})
            rec["opponent"] = other["team"]
            by_week[(rec["season"], rec["week"])].append(rec)
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    totals, count = dict.fromkeys(KEYS, 0.0), 0
    out: dict[str, dict[str, Any]] = {}
    for season, week in sorted(by_week):
        batch = by_week[(season, week)]
        league = {k: totals[k] / count if count else 1.0 for k in KEYS}
        snaps: dict[str, dict[str, float]] = {}
        for r in batch:
            for team in (r["team"], r["opponent"]):
                if team not in snaps and history[team]:
                    snaps[team] = _snapshot(history[team], season, league)
        if season >= start:
            for r in batch:
                if r["game_id"] not in out and r["team"] in snaps and r["opponent"] in snaps:
                    out[r["game_id"]] = {"teams": (r["team"], r["opponent"]),
                                         "sides": {t: snaps[t] for t in (r["team"], r["opponent"])}}
        for r in batch:
            history[r["team"]].append(r)
            for k in KEYS:
                totals[k] += r[k]
            count += 1
    return (out, table) if with_actuals else out


# ------------------------------------------------------------------------------------------ availability
def line_availability(repository: NFLRepository, start: int, end: int) -> dict[tuple[int, int, str], dict[str, float]]:
    """(season, week, team) -> {'ol_out', 'dl_out'}: snap-weighted regulars listed out/doubtful/questionable that week."""
    history = _snap_history(repository, start, end)
    out: dict[tuple[int, int, str], dict[str, float]] = {}
    with closing(repository._connect()) as connection:
        for r in connection.execute(
                """SELECT season,week,team,normalized_name,position,report_status FROM nfl_injury_history
                   WHERE season BETWEEN ? AND ?""", (int(start), int(end))):
            group = "ol_out" if r["position"] in OL_GROUP else "dl_out" if r["position"] in DL_GROUP else None
            weight = STATUS_WEIGHT.get(r["report_status"])
            if not group or not weight:
                continue
            key = (int(r["season"]), int(r["week"]), str(r["team"]))
            slot = out.setdefault(key, {"ol_out": 0.0, "dl_out": 0.0})
            slot[group] += weight * _importance(history, str(r["normalized_name"]), key[0], key[1])
    return out


# ------------------------------------------------------------------------------------------ game features
def blend(own: dict[str, float], opp: dict[str, float], name: str) -> float:
    """One side's expectation: its OWN offense averaged with the OPPONENT's defense."""
    return (own[f"off_{name}"] + opp[f"def_{name}"]) / 2.0


def attach(rows: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]],
           availability: dict[tuple[int, int, str], dict[str, float]]) -> None:
    for r in rows:
        f = snapshots.get(str(r["game_id"]))
        if not f:
            continue
        home, away = canon_team(r["home_team"]), canon_team(r["away_team"])
        if home not in f["sides"] or away not in f["sides"]:
            continue
        h, a = f["sides"][home], f["sides"][away]
        # a side that was pressured more than expected is worse off, so differences/sums keep the raw blend:
        # the regression learns the sign.
        for name in RATES:
            if name == "press" and min(h["charted"], a["charted"]) < MIN_CHARTED_DROPBACKS:
                continue
            bh, ba = blend(h, a, name), blend(a, h, name)
            r[f"tr_{name}_h"], r[f"tr_{name}_a"] = bh, ba
            r[f"tr_{name}_diff"], r[f"tr_{name}_sum"] = bh - ba, bh + ba
        # pass-rush havoc a side's offense faces: sacks + non-sack hits per dropback
        for side, own, opp in (("h", h, a), ("a", a, h)):
            r[f"tr_havoc_{side}"] = blend(own, opp, "sack") + blend(own, opp, "hit")
        r["tr_havoc_diff"], r["tr_havoc_sum"] = r["tr_havoc_h"] - r["tr_havoc_a"], r["tr_havoc_h"] + r["tr_havoc_a"]
        season, week = int(r["season"]), int(r["week"])
        ah, aa = availability.get((season, week, home), {}), availability.get((season, week, away), {})
        for key in ("ol_out", "dl_out"):
            r[f"tr_{key}_h"], r[f"tr_{key}_a"] = ah.get(key, 0.0), aa.get(key, 0.0)
        # positive favours the home team: the away line is missing more / the home front is missing less
        r["tr_ol_out_diff"] = r["tr_ol_out_a"] - r["tr_ol_out_h"]
        r["tr_dl_out_diff"] = r["tr_dl_out_a"] - r["tr_dl_out_h"]
        r["tr_ol_out_sum"], r["tr_dl_out_sum"] = r["tr_ol_out_h"] + r["tr_ol_out_a"], r["tr_dl_out_h"] + r["tr_dl_out_a"]
