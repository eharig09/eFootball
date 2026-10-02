"""Drive efficiency components and special teams, as pregame features for NFL margins and totals.

Two groups, both built from the cached nflverse play-by-play and both strictly pregame (earlier week batches only,
season-decayed, shrunk toward the league mean):

EFFICIENCY (points-per-drive components). Each side's expectation is the average of its OWN OFFENSE and the
OPPONENT'S DEFENSE -- the same matchup blend as the lean model:
  explosive rate, touchdown rate per drive, red-zone touchdown rate, three-and-out rate, starting field position.

SPECIAL TEAMS (unit features, not blended):
  net punting      net yards per punt (gross minus return, touchbacks charged 20) and yards per punt returned
  kicker accuracy  field goals made over what the distance predicts, on kicks under 45 yards, per attempt, shrunk
  kicker range     the same on kicks of 45+ yards, plus kickoff touchback rate
  The kicker is the team's most recent FG kicker (known before the game); the "expected" make rate at each distance
  comes only from earlier seasons.

The module also validates the special-teams ratings directly: do pregame kicker/punter ratings predict the next kicks?
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository

PBP_COLUMNS = (
    "game_id", "week", "season_type", "posteam", "defteam", "play_type", "fixed_drive", "fixed_drive_result",
    "drive_play_count", "yardline_100", "kick_distance", "return_yards", "touchback", "punt_blocked",
    "kicker_player_id", "field_goal_result",
)
PRIOR_GAMES = 3.0
KICKER_SHORT_PRIOR, KICKER_LONG_PRIOR = 30.0, 15.0     # attempts of league-average kicking mixed into each kicker
LONG_FG = 45                                           # kicks of this many yards or more count as "range"
FG_BINS = (0, 30, 35, 40, 45, 50, 55, 99)              # make-rate bins by distance
DEFAULT_MAKE_RATE = {0: .97, 30: .92, 35: .87, 40: .83, 45: .75, 50: .67, 55: .55}

#: blended metrics: (numerator, denominator); own offense averaged with the opponent's defense
BLENDED = {
    "td": ("tds", "drives"), "three_out": ("three_outs", "drives"), "rz_td": ("rz_tds", "rz_trips"),
    "start": ("start_sum", "drives"), "explosive": ("explosive", "plays"),
}
#: unit metrics: the team's own numbers only
UNIT = {"punt_net": ("punt_net_sum", "punts"), "punt_ret": ("recv_ret_sum", "recv_n"), "ko_tb": ("ko_tb", "kos")}
ALL_KEYS = sorted({k for pair in (*BLENDED.values(), *UNIT.values(), ("fg_att", "games")) for k in pair})


# ------------------------------------------------------------------------------------------ extraction
def _bin(distance: float) -> int:
    return max(b for b in FG_BINS[:-1] if distance >= b)


def extract_season(frame, season: int) -> dict[str, list[dict[str, Any]]]:
    """Team-game aggregates and field-goal events for one season's regular-season play-by-play."""
    frame = frame[frame["season_type"] == "REG"]
    teams: dict[tuple[str, str], dict[str, float]] = {}

    def slot(game_id: str, team: str, week: int) -> dict[str, float]:
        key = (game_id, canon_team(team))
        if key not in teams:
            teams[key] = {k: 0.0 for k in ALL_KEYS}
            teams[key].update(game_id=game_id, team=canon_team(team), week=int(week), season=season)
        return teams[key]

    scrimmage = frame[frame["play_type"].isin(("pass", "run")) & frame["posteam"].notna() & frame["fixed_drive"].notna()]
    drives = scrimmage.groupby(["game_id", "fixed_drive", "posteam"], sort=False).agg(
        week=("week", "first"), result=("fixed_drive_result", "first"), plays=("drive_play_count", "first"),
        start=("yardline_100", "first"), rz=("yardline_100", lambda s: bool((s <= 20).any()))).reset_index()
    for r in drives.itertuples(index=False):
        if r.result == "End of half":
            continue
        t = slot(r.game_id, r.posteam, r.week)
        t["drives"] += 1
        t["tds"] += r.result == "Touchdown"
        t["three_outs"] += (r.result == "Punt") and (r.plays or 99) <= 3
        t["start_sum"] += float(r.start) if r.start == r.start else 50.0
        if r.rz:
            t["rz_trips"] += 1
            t["rz_tds"] += r.result == "Touchdown"

    punts = frame[(frame["play_type"] == "punt") & (frame["punt_blocked"] != 1) & frame["kick_distance"].notna()]
    for r in punts.itertuples(index=False):
        returned = 0.0 if r.touchback == 1 else (r.return_yards if r.return_yards == r.return_yards else 0.0)
        net = float(r.kick_distance) - returned - (20.0 if r.touchback == 1 else 0.0)
        kicker, receiver = slot(r.game_id, r.posteam, r.week), slot(r.game_id, r.defteam, r.week)
        kicker["punts"] += 1
        kicker["punt_net_sum"] += net
        receiver["recv_n"] += 1
        receiver["recv_ret_sum"] += returned

    for r in frame[frame["play_type"] == "kickoff"].itertuples(index=False):
        t = slot(r.game_id, r.posteam, r.week)
        t["kos"] += 1
        t["ko_tb"] += r.touchback == 1

    events = []
    for r in frame[(frame["play_type"] == "field_goal") & frame["kick_distance"].notna()
                   & frame["kicker_player_id"].notna()].itertuples(index=False):
        team = slot(r.game_id, r.posteam, r.week)
        team["fg_att"] += 1
        events.append({"season": season, "week": int(r.week), "game_id": r.game_id, "team": canon_team(r.posteam),
                       "kicker": str(r.kicker_player_id), "distance": float(r.kick_distance),
                       "made": 1.0 if r.field_goal_result == "made" else 0.0})
    for t in teams.values():
        t["games"] = 1.0
    return {"teams": list(teams.values()), "fg_events": events}


def load(start: int, end: int, cache: str | Path = "instance/nflverse_raw") -> dict[str, list[dict[str, Any]]]:
    import pandas as pd
    out: dict[str, list[dict[str, Any]]] = {"teams": [], "fg_events": []}
    for season in range(int(start), int(end) + 1):
        path = Path(cache) / f"pbp_{season}.parquet"
        if not path.exists():
            continue
        part = extract_season(pd.read_parquet(path, columns=list(PBP_COLUMNS)), season)
        out["teams"] += part["teams"]
        out["fg_events"] += part["fg_events"]
    return out


def _explosive(repository: NFLRepository, start: int, end: int) -> dict[tuple[str, str], tuple[float, float]]:
    with closing(repository._connect()) as connection:
        return {(str(r["game_id"]), canon_team(r["team"])): (float(r["explosive_plays"]), float(r["plays"]))
                for r in connection.execute(
                    "SELECT game_id,team,explosive_plays,plays FROM game_team_efficiency WHERE season BETWEEN ? AND ? AND plays>0",
                    (int(start), int(end)))}


# ------------------------------------------------------------------------------------------ kicker model
def make_rates(events: list[dict[str, Any]]) -> dict[int, dict[int, float]]:
    """season -> {distance bin: make rate} from STRICTLY EARLIER seasons (a bin with under 30 kicks uses the default)."""
    counts: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_season[e["season"]].append(e)
    rates: dict[int, dict[int, float]] = {}
    for season in sorted(by_season):
        rates[season] = {b: (counts[b][1] / counts[b][0] if counts[b][0] >= 30 else DEFAULT_MAKE_RATE[b])
                         for b in FG_BINS[:-1]}
        for e in by_season[season]:
            c = counts[_bin(e["distance"])]
            c[0] += 1
            c[1] += e["made"]
    return rates


def kicker_ratings(state: dict[str, list[float]], kicker: str | None) -> tuple[float, float]:
    """(accuracy, range) as shrunk make-over-expected per attempt; (0, 0) for an unknown kicker."""
    s = state.get(kicker or "")
    if not s:
        return 0.0, 0.0
    return s[1] / (s[0] + KICKER_SHORT_PRIOR), s[3] / (s[2] + KICKER_LONG_PRIOR)


# ------------------------------------------------------------------------------------------ pregame features
def _ratio(numerator: float, denominator: float) -> float:
    """A rate that is 0.0, not a crash, when nothing has been observed or seeded for its denominator."""
    return numerator / denominator if denominator > 0 else 0.0


def _snapshot(records: list[dict[str, Any]], season: int, league: dict[str, float]) -> dict[str, float]:
    sums = dict.fromkeys(ALL_KEYS, 0.0)
    opp = dict.fromkeys(ALL_KEYS, 0.0)
    for r in records:
        w = STATE_SEASON_DECAY ** max(0, season - r["season"])
        for k in ALL_KEYS:
            sums[k] += w * r[k]
            opp[k] += w * r["opp_" + k]
    prior = {k: PRIOR_GAMES * league[k] for k in ALL_KEYS}      # three league-average games
    out = {"games": sums["games"] + PRIOR_GAMES}
    for name, (num, den) in {**BLENDED, **UNIT}.items():
        out[f"off_{name}"] = _ratio(sums[num] + prior[num], sums[den] + prior[den])
    for name, (num, den) in BLENDED.items():
        out[f"def_{name}"] = _ratio(opp[num] + prior[num], opp[den] + prior[den])
    out["fg_att_pg"] = _ratio(sums["fg_att"] + prior["fg_att"], sums["games"] + prior["games"])
    return out


def pregame_features(repository: NFLRepository, start: int, end: int,
                     cache: str | Path = "instance/nflverse_raw", with_extras: bool = False):
    """game_id -> {'sides': {team: snapshot}, kicker ratings, 'teams': (a, b)} for every game from `start`.

    With `with_extras`, also returns the evidence needed to validate the special-teams ratings: every field goal with the
    kicker's PREGAME ratings next to what happened, and every team-game's punting with the team's pregame net average.
    """
    data = load(max(2009, start - 2), end, cache)
    explosive = _explosive(repository, max(2009, start - 2), end)
    table = {(t["game_id"], t["team"]): t for t in data["teams"]}
    for (gid, team), t in table.items():
        t["explosive"], t["plays"] = explosive.get((gid, team), (0.0, 0.0))
    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in table.values():
        by_game[t["game_id"]].append(t)
    # attach each team-game's opponent numbers so defence-allowed rates are available
    records: list[dict[str, Any]] = []
    for gid, pair in by_game.items():
        if len(pair) != 2:
            continue
        a, b = pair
        for me, other in ((a, b), (b, a)):
            rec = dict(me)
            rec.update({"opp_" + k: other[k] for k in ALL_KEYS})
            rec["opponent"] = other["team"]
            records.append(rec)
    rates = make_rates(data["fg_events"])
    events_by_batch: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for e in data["fg_events"]:
        events_by_batch[(e["season"], e["week"])].append(e)
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_week[(r["season"], r["week"])].append(r)

    kick_checks: list[dict[str, Any]] = []
    punt_checks: list[dict[str, Any]] = []
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    kicker_state: dict[str, list[float]] = {}
    last_kicker: dict[str, str] = {}
    totals = dict.fromkeys(ALL_KEYS, 0.0)
    count = 0
    out: dict[str, dict[str, Any]] = {}
    for season, week in sorted(by_week):
        batch = by_week[(season, week)]
        league = {k: totals[k] / count if count else 1.0 for k in ALL_KEYS}
        if not count:                     # first batch ever: neutral placeholders (never used: no history exists yet)
            league.update({"plays": 60.0, "drives": 11.0, "start": 30.0})
        snaps: dict[str, dict[str, float]] = {}
        for r in batch:
            for team in (r["team"], r["opponent"]):
                if team not in snaps and history[team]:
                    snaps[team] = _snapshot(history[team], season, league)
        if season >= start:
            seen: set[str] = set()
            for r in batch:
                gid = r["game_id"]
                if gid in seen or r["team"] not in snaps or r["opponent"] not in snaps:
                    continue
                seen.add(gid)
                pair = (r["team"], r["opponent"])
                out[gid] = {"teams": pair, "sides": {t: snaps[t] for t in pair},
                            "kicker": {t: kicker_ratings(kicker_state, last_kicker.get(t)) for t in pair},
                            "kicker_id": {t: last_kicker.get(t) for t in pair}}
        if with_extras and season >= start:
            for r in batch:
                snap = snaps.get(r["team"])
                if snap and r["punts"]:
                    punt_checks.append({"pre_net": snap["off_punt_net"], "punts": r["punts"],
                                        "net": r["punt_net_sum"] / r["punts"], "season": season})
            for e in events_by_batch.get((season, week), []):
                expected = rates.get(season, DEFAULT_MAKE_RATE)[_bin(e["distance"])]
                acc, rng = kicker_ratings(kicker_state, e["kicker"])
                kick_checks.append({"season": season, "long": e["distance"] >= LONG_FG, "pre": rng if e["distance"] >= LONG_FG else acc,
                                    "actual_over_expected": e["made"] - expected, "known": e["kicker"] in kicker_state})
        # --- update after the whole week batch
        for e in sorted(events_by_batch.get((season, week), []), key=lambda x: x["game_id"]):
            expected = rates.get(season, DEFAULT_MAKE_RATE)[_bin(e["distance"])]
            s = kicker_state.setdefault(e["kicker"], [0.0, 0.0, 0.0, 0.0])
            slot = 2 if e["distance"] >= LONG_FG else 0
            s[slot] += 1
            s[slot + 1] += e["made"] - expected
            last_kicker[e["team"]] = e["kicker"]
        for r in batch:
            history[r["team"]].append(r)
            for k in ALL_KEYS:
                totals[k] += r[k]
            count += 1
    return (out, {"kicks": kick_checks, "punts": punt_checks}) if with_extras else out


def blend_side(own: dict[str, float], opp: dict[str, float], name: str) -> float:
    """Expected value for one side: its OWN offense averaged with the OPPONENT's defense."""
    return (own[f"off_{name}"] + opp[f"def_{name}"]) / 2.0


def attach(rows: list[dict[str, Any]], features: dict[str, dict[str, Any]], lean_sides: dict[str, dict[str, float]] | None = None) -> None:
    """Add eff_* (blended efficiency) and st_* (special teams) features, as home-minus-away and home-plus-away."""
    for r in rows:
        f = features.get(str(r["game_id"]))
        if not f:
            continue
        home, away = canon_team(r["home_team"]), canon_team(r["away_team"])
        if home not in f["sides"] or away not in f["sides"]:
            continue
        h, a = f["sides"][home], f["sides"][away]
        for name in BLENDED:
            bh, ba = blend_side(h, a, name), blend_side(a, h, name)
            r[f"eff_{name}_h"], r[f"eff_{name}_a"] = bh, ba
            r[f"eff_{name}_diff"], r[f"eff_{name}_sum"] = bh - ba, bh + ba
        r["st_punt_net_diff"], r["st_punt_net_sum"] = h["off_punt_net"] - a["off_punt_net"], h["off_punt_net"] + a["off_punt_net"]
        r["st_punt_ret_diff"], r["st_punt_ret_sum"] = h["off_punt_ret"] - a["off_punt_ret"], h["off_punt_ret"] + a["off_punt_ret"]
        r["st_ko_tb_diff"], r["st_ko_tb_sum"] = h["off_ko_tb"] - a["off_ko_tb"], h["off_ko_tb"] + a["off_ko_tb"]
        # kicker value in points: attempts per game x (accuracy on short kicks and range on long kicks) x 3 points
        value = {}
        for team, snap in ((home, h), (away, a)):
            acc, rng = f["kicker"][team]
            short_share = 0.7
            value[team] = snap["fg_att_pg"] * 3.0 * (short_share * acc + (1 - short_share) * rng)
            value[team + "_acc"], value[team + "_rng"] = acc, rng
        r["st_kicker_acc_diff"], r["st_kicker_rng_diff"] = value[home + "_acc"] - value[away + "_acc"], value[home + "_rng"] - value[away + "_rng"]
        r["st_fg_pts_h"], r["st_fg_pts_a"] = value[home], value[away]
        r["st_fg_pts_diff"], r["st_fg_pts_sum"] = value[home] - value[away], value[home] + value[away]
        r["st_kicker_acc_sum"], r["st_kicker_rng_sum"] = value[home + "_acc"] + value[away + "_acc"], value[home + "_rng"] + value[away + "_rng"]
