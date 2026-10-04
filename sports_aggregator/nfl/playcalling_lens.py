"""A play-calling lens: how each side chooses to attack, set against what the situation usually calls for.

All from nfl_plays (scrimmage plays, 2021 on; play action and motion from 2022). Measures:

  early-down pass rate over expected   the share of 1st/2nd-down plays that were passes, minus what the league does in
                                       the same spot (down, distance, field zone, win-probability state)
  pass rate when trailing / leading    early-down pass rate with win probability under 25% / over 75%, and the swing
  fourth-down attempts / game          plays on 4th down that were runs or passes, and how often they converted
  play action / motion / no huddle     usage rates

These describe a team's choices; they are not a forecast. An earlier test found coaches' results against their
opponent-adjusted ratings carry no information about their next games, and the ratings already include whatever the
coach contributes, so nothing here moves a number. The tendencies belong to the team's play-calling, which is the head
coach's unless the play-caller differs; the staff tables on the team page say who calls the plays.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import time
from typing import Any

from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-playcalling-lens-v1"
MIN_GAMES = 4
CACHE_SECONDS = 600
_CACHE: dict[tuple, tuple[float, dict[str, dict[str, Any]]]] = {}

_EARLY = """season{season_clause} AND down IN (1,2) AND (is_pass=1 OR is_rush=1) AND COALESCE(is_penalty,0)=0
            AND posteam IS NOT NULL AND wp IS NOT NULL AND game_seconds>120 AND NOT (game_seconds BETWEEN 1800 AND 1920)"""
_BUCKET = """down,
  CASE WHEN ydstogo<=2 THEN 'a' WHEN ydstogo<=5 THEN 'b' WHEN ydstogo<=9 THEN 'c' WHEN ydstogo=10 THEN 'd' ELSE 'e' END AS dist,
  CASE WHEN yardline_100<=20 THEN 'rz' WHEN yardline_100<=50 THEN 'opp' WHEN yardline_100<=80 THEN 'mid' ELSE 'own' END AS field,
  CASE WHEN wp<0.25 THEN 'trail' WHEN wp>0.75 THEN 'lead' ELSE 'even' END AS state"""

#: key, label, kind (drives the format), note
ROWS = (
    ("proe", "Early-down pass rate over expected", "signed_rate", "vs league in the same spots"),
    ("pass_trailing", "Early-down pass rate, trailing", "rate", "win probability under 25%"),
    ("pass_leading", "Early-down pass rate, leading", "rate", "win probability over 75%"),
    ("script_swing", "Script swing", "signed_rate", "trailing minus leading"),
    ("fourth_attempts", "4th-down attempts / game", "f2", "runs and passes only"),
    ("fourth_conversion", "4th-down conversion rate", "rate", None),
    ("play_action", "Play-action rate", "rate", "of dropbacks, 2022+"),
    ("motion", "Pre-snap motion rate", "rate", "of dropbacks, 2022+"),
    ("no_huddle", "No-huddle rate", "rate", "of plays"),
)


def _baseline(connection, through_season: int) -> dict[tuple, float]:
    clause = _EARLY.format(season_clause=" BETWEEN 2021 AND %d" % int(through_season))
    rows = connection.execute(
        f"SELECT {_BUCKET}, COUNT(*) n, SUM(is_pass) passes FROM nfl_plays WHERE {clause} GROUP BY 1,2,3,4")
    return {(r[0], r[1], r[2], r[3]): r[5] / r[4] for r in rows if r[4]}


def league_profiles(repository: NFLRepository, season: int) -> dict[str, dict[str, Any]]:
    """team -> measure values for one season, plus each measure's 1..n rank (1 = the highest value).

    Held for a few minutes: a season's plays only change when a refresh lands, and a game page asks twice.
    """
    key = (str(getattr(repository, "path", "")), int(season))
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    profiles = _league_profiles(repository, int(season))
    _CACHE[key] = (time.monotonic(), profiles)
    return profiles


def _league_profiles(repository: NFLRepository, season: int) -> dict[str, dict[str, Any]]:
    season = int(season)
    with closing(repository._connect()) as connection:
        baseline = _baseline(connection, season)
        early = connection.execute(
            f"SELECT posteam, {_BUCKET}, COUNT(*) n, SUM(is_pass) passes FROM nfl_plays "
            f"WHERE {_EARLY.format(season_clause='=%d' % season)} GROUP BY 1,2,3,4,5")
        passes_by_state: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        over: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for team, down, dist, field, state, n, passes in early:
            team = canon_team(team)
            expected = baseline.get((down, dist, field, state))
            if expected is None:
                continue
            over[team][0] += passes - n * expected
            over[team][1] += n
            passes_by_state[(team, state)][0] += passes
            passes_by_state[(team, state)][1] += n
        fourth = {canon_team(r[0]): (r[1], r[2]) for r in connection.execute(
            """SELECT posteam, COUNT(*), SUM(CASE WHEN first_down=1 OR is_touchdown=1 THEN 1 ELSE 0 END) FROM nfl_plays
               WHERE season=? AND down=4 AND (is_pass=1 OR is_rush=1) AND COALESCE(is_penalty,0)=0 AND posteam IS NOT NULL
               GROUP BY 1""", (season,))}
        games = {canon_team(r[0]): r[1] for r in connection.execute(
            "SELECT posteam, COUNT(DISTINCT game_id) FROM nfl_plays WHERE season=? AND posteam IS NOT NULL GROUP BY 1",
            (season,))}
        dropbacks = {canon_team(r[0]): (r[1], r[2], r[3]) for r in connection.execute(
            """SELECT posteam, COUNT(*), SUM(play_action), SUM(motion) FROM nfl_plays
               WHERE season=? AND is_pass=1 AND play_action IS NOT NULL AND posteam IS NOT NULL GROUP BY 1""", (season,))}
        huddle = {canon_team(r[0]): (r[1], r[2]) for r in connection.execute(
            """SELECT posteam, COUNT(*), SUM(no_huddle) FROM nfl_plays
               WHERE season=? AND (is_pass=1 OR is_rush=1) AND no_huddle IS NOT NULL AND posteam IS NOT NULL GROUP BY 1""",
            (season,))}

    profiles: dict[str, dict[str, Any]] = {}
    for team, (excess, n) in over.items():
        trailing, leading = passes_by_state[(team, "trail")], passes_by_state[(team, "lead")]
        attempts, converted = fourth.get(team, (0, 0))
        plays_pa = dropbacks.get(team, (0, 0, 0))
        plays_nh = huddle.get(team, (0, 0))
        g = games.get(team, 0)
        trail_rate = trailing[0] / trailing[1] if trailing[1] >= 30 else None
        lead_rate = leading[0] / leading[1] if leading[1] >= 30 else None
        profiles[team] = {
            "games": g, "early_plays": n,
            "proe": excess / n if n else None,
            "pass_trailing": trail_rate, "pass_leading": lead_rate,
            "script_swing": trail_rate - lead_rate if trail_rate is not None and lead_rate is not None else None,
            "fourth_attempts": attempts / g if g else None,
            "fourth_conversion": converted / attempts if attempts >= 5 else None,
            "play_action": plays_pa[1] / plays_pa[0] if plays_pa[0] and plays_pa[1] is not None else None,
            "motion": plays_pa[2] / plays_pa[0] if plays_pa[0] and plays_pa[2] is not None else None,
            "no_huddle": plays_nh[1] / plays_nh[0] if plays_nh[0] else None,
        }
    for key, *_ in ROWS:
        ordered = sorted((t for t, p in profiles.items() if p[key] is not None), key=lambda t: -profiles[t][key])
        for rank, team in enumerate(ordered, start=1):
            profiles[team][f"{key}_rank"] = rank
            profiles[team][f"{key}_of"] = len(ordered)
    return profiles


def packet(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any]:
    """Panel data for one game: both sides' play-calling measures, from this season or the last if too early."""
    season = int(game["season"])
    sides = {}
    for side in ("away", "home"):
        team = canon_team(game[f"{side}_team"])
        used = season
        profiles = league_profiles(repository, used)
        profile = profiles.get(team)
        if profile is None or profile["games"] < MIN_GAMES:
            prior = league_profiles(repository, season - 1) if season - 1 >= 2021 else {}
            if prior.get(team) and prior[team]["games"] >= MIN_GAMES:
                profile, used = prior[team], season - 1
        if profile is None:
            return {"available": False, "reason": "Play-calling measures need play-by-play for both teams."}
        sides[side] = {"team": team, "season": used, "coach": game.get(f"{side}_coach"), "profile": profile,
                       "carried": used != season}
    return {"available": True, "rows": ROWS, "sides": sides}
