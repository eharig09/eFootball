"""Stored scrimmage plays: nflverse play-by-play joined with participation and FTN charting.

`nfl_plays` keeps one row per pass or rush play with what the offense showed (formation,
personnel, motion, play action, RPO ...) and what the defense showed (package, box count,
coverage, man/zone, rushers, pressure). Everything the game and team pages need
(win-probability curve, drive chart, play-by-play table, EPA-by-look splits) is a query
over this table, so no page recomputes from raw parquet.
"""

from __future__ import annotations

import functools
import math
import re
from typing import Any, Iterable, Mapping

from sports_aggregator.nfl.naming import canon_team as _canon_team

# A few dozen distinct values repeat a million times across a season of plays.
canon_team = functools.lru_cache(maxsize=512)(_canon_team)

_COUNT = re.compile(r"(\d+)\s+([A-Z]+)")
_BACKS = {"RB", "FB", "HB"}
_TIGHT_ENDS = {"TE"}
_LINEMEN = {"C", "G", "T", "OL"}
_DEFENSIVE_BACKS = {"CB", "S", "FS", "SS", "DB"}
_TWO_HIGH = {"COVER_2", "COVER_4", "COVER_6", "2_MAN"}
_ONE_HIGH = {"COVER_1", "COVER_3", "COVER_0", "COVER_9"}


def _number(value: Any) -> float | None:
    kind = type(value)
    if kind is float:
        return None if value != value else value          # NaN is the only float not equal to itself
    if kind is int:
        return float(value)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _text(value: Any) -> str | None:
    if type(value) is str:
        text = value.strip()
        return text or None
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> int | None:
    """nflverse booleans arrive as 0/1 floats, True/False, or blanks."""
    kind = type(value)
    if kind is float:
        return None if value != value else (1 if value else 0)
    if kind is bool or kind is int:
        return 1 if value else 0
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "1.0", "yes"}:
            return 1
        if lowered in {"false", "0", "0.0", "no"}:
            return 0
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return 1 if bool(value) else 0


def _counts(personnel: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for number, position in _COUNT.findall(personnel or ""):
        counts[position] = counts.get(position, 0) + int(number)
    return counts


@functools.lru_cache(maxsize=4096)
def offense_personnel_group(personnel: str | None) -> str | None:
    """'1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR' -> '11' (backs then tight ends).

    Only a standard five-lineman, one-quarterback look gets a number; everything else
    (extra linemen, no quarterback under center swaps, unbalanced trick looks) is 'Other'.
    """
    counts = _counts(personnel)
    if not counts:
        return None
    linemen = sum(counts.get(position, 0) for position in _LINEMEN)
    if counts.get("QB", 0) != 1 or linemen != 5:
        return "Other"
    backs = sum(counts.get(position, 0) for position in _BACKS)
    tight_ends = sum(counts.get(position, 0) for position in _TIGHT_ENDS)
    return f"{backs}{tight_ends}"


@functools.lru_cache(maxsize=4096)
def defense_package(personnel: str | None) -> str | None:
    """Base (<=4 defensive backs), Nickel (5), Dime (6+)."""
    counts = _counts(personnel)
    if not counts:
        return None
    backs = sum(counts.get(position, 0) for position in _DEFENSIVE_BACKS)
    if not backs:
        return None
    return "Base" if backs <= 4 else ("Nickel" if backs == 5 else "Dime")


def coverage_shell(coverage: str | None) -> str | None:
    if coverage in _TWO_HIGH:
        return "2-High"
    if coverage in _ONE_HIGH:
        return "1-High"
    return None


def box_label(defenders_in_box: Any) -> str | None:
    count = _int(defenders_in_box)
    if count is None or count <= 0:
        return None
    return "Light" if count <= 5 else ("Heavy" if count >= 8 else "Base")


ZONES = ("Backed up", "Neutral", "Plus", "Red zone", "Goal to go")


def field_zone(yardline_100: Any, goal_to_go: Any = None, ydstogo: Any = None) -> str | None:
    """Where on the field the offense snaps from, by yards to the opponent's end zone.

    Backed up: inside its own 20. Neutral: own 21 to its own 49. Plus: midfield to the 21.
    Red zone: the 20 to the 11 (not goal to go). Goal to go: the first-down line is the goal line.
    """
    distance = _number(yardline_100)
    if distance is None:
        return None
    needs_goal = _flag(goal_to_go) == 1
    if not needs_goal and _number(ydstogo) is not None:
        needs_goal = _number(ydstogo) >= distance and distance <= 20
    if needs_goal:
        return "Goal to go"
    if distance >= 80:
        return "Backed up"
    if distance > 50:
        return "Neutral"
    if distance > 20:
        return "Plus"
    return "Red zone"


def hash_mark(value: Any) -> str | None:
    """FTN's starting_hash: 'L', 'M' or 'R'. A '0' means the hash was not charted."""
    text = _text(value)
    return text if text in {"L", "M", "R"} else None


def _index(rows: Iterable[Mapping[str, Any]], game_key: str, play_key: str) -> dict[tuple[str, int], Mapping[str, Any]]:
    indexed: dict[tuple[str, int], Mapping[str, Any]] = {}
    for row in rows:
        game_id = _text(row.get(game_key))
        play_id = _int(row.get(play_key))
        if game_id and play_id is not None:
            indexed[(game_id, play_id)] = row
    return indexed


PLAY_COLUMNS = (
    "season", "week", "game_id", "play_id", "season_type", "qtr", "clock", "game_seconds",
    "drive", "drive_result", "posteam", "defteam", "home_team", "away_team", "home_score", "away_score",
    "down", "ydstogo", "yardline_100", "yard_line", "description", "play_type",
    "is_pass", "is_rush", "is_sack", "is_turnover", "is_touchdown", "is_penalty",
    "yards_gained", "epa", "success", "wp", "home_wp", "wpa",
    "air_yards", "pass_location", "run_location", "run_gap", "shotgun", "no_huddle",
    "offense_formation", "offense_personnel", "offense_group", "defense_personnel", "defense_package",
    "defenders_in_box", "box", "pass_rushers", "man_zone", "coverage", "shell", "was_pressure",
    "time_to_throw", "motion", "play_action", "rpo", "screen", "qb_out_of_pocket", "blitzers",
    "is_drop", "is_contested", "is_catchable", "starting_hash",
    "passer_id", "receiver_id", "rusher_id", "route", "is_complete", "yac", "cpoe", "is_scramble",
    "goal_to_go", "zone", "is_pass_td", "is_rush_td", "first_down", "qb_location",
    "is_interception",
    # Who was on the field, semicolon-separated gsis ids with parallel positions. Kept in a side table
    # (nfl_play_participants) so nfl_plays stays narrow enough to scan for league-wide rates.
    "offense_players", "offense_positions", "defense_players", "defense_positions",
)
PARTICIPANT_COLUMNS = ("offense_players", "offense_positions", "defense_players", "defense_positions")
PLAY_TABLE_COLUMNS = PLAY_COLUMNS[:-len(PARTICIPANT_COLUMNS)]


def build_play_rows(pbp: Iterable[Mapping[str, Any]],
                    participation: Iterable[Mapping[str, Any]] = (),
                    ftn: Iterable[Mapping[str, Any]] = ()) -> list[tuple]:
    """One tuple per scrimmage play, ordered as PLAY_COLUMNS.

    Plays without a game id or play id, deleted plays, and non-scrimmage plays are dropped.
    Participation and FTN are optional: a season without them stores the play with those
    fields empty rather than skipping it.
    """
    part = _index(participation, "nflverse_game_id", "play_id")
    charted = _index(ftn, "nflverse_game_id", "nflverse_play_id")
    rows: list[tuple] = []
    for play in pbp:
        game_id = _text(play.get("game_id"))
        play_id = _int(play.get("play_id"))
        if not game_id or play_id is None or _flag(play.get("play_deleted")) == 1:
            continue
        is_pass = _flag(play.get("pass")) == 1
        is_rush = _flag(play.get("rush")) == 1
        if not (is_pass or is_rush):
            continue
        posteam = canon_team(play.get("posteam"))
        defteam = canon_team(play.get("defteam"))
        if not posteam or not defteam:
            continue
        seen = part.get((game_id, play_id), {})
        chart = charted.get((game_id, play_id), {})
        offense = _text(seen.get("offense_personnel"))
        defense = _text(seen.get("defense_personnel"))
        coverage = _text(seen.get("defense_coverage_type")) if is_pass else None
        man_zone = _text(seen.get("defense_man_zone_type")) if is_pass else None
        box = _int(seen.get("defenders_in_box"))
        turnover = 1 if (_flag(play.get("interception")) == 1 or _flag(play.get("fumble_lost")) == 1) else 0
        rows.append((
            _int(play.get("season")), _int(play.get("week")), game_id, play_id,
            _text(play.get("season_type")), _int(play.get("qtr")), _text(play.get("time")),
            _int(play.get("game_seconds_remaining")),
            _int(play.get("fixed_drive")) or _int(play.get("drive")), _text(play.get("fixed_drive_result")),
            posteam, defteam, canon_team(play.get("home_team")), canon_team(play.get("away_team")),
            _int(play.get("total_home_score")), _int(play.get("total_away_score")),
            _int(play.get("down")), _int(play.get("ydstogo")), _number(play.get("yardline_100")),
            _text(play.get("yrdln")), _text(play.get("desc")), _text(play.get("play_type")),
            int(is_pass), int(is_rush), _flag(play.get("sack")) or 0, turnover,
            _flag(play.get("touchdown")) or 0, _flag(play.get("penalty")) or 0,
            _number(play.get("yards_gained")), _number(play.get("epa")), _number(play.get("success")),
            _number(play.get("wp")), _number(play.get("home_wp")), _number(play.get("wpa")),
            _number(play.get("air_yards")), _text(play.get("pass_location")), _text(play.get("run_location")),
            _text(play.get("run_gap")), _flag(play.get("shotgun")), _flag(play.get("no_huddle")),
            _text(seen.get("offense_formation")), offense, offense_personnel_group(offense),
            defense, defense_package(defense), box, box_label(box),
            _int(seen.get("number_of_pass_rushers")), man_zone, coverage, coverage_shell(coverage),
            _flag(seen.get("was_pressure")), _number(seen.get("time_to_throw")),
            _flag(chart.get("is_motion")), _flag(chart.get("is_play_action")), _flag(chart.get("is_rpo")),
            _flag(chart.get("is_screen_pass")), _flag(chart.get("is_qb_out_of_pocket")),
            _int(chart.get("n_blitzers")), _flag(chart.get("is_drop")), _flag(chart.get("is_contested_ball")),
            _flag(chart.get("is_catchable_ball")), hash_mark(chart.get("starting_hash")),
            _text(play.get("passer_player_id")), _text(play.get("receiver_player_id")),
            _text(play.get("rusher_player_id")), _text(seen.get("route")) if is_pass else None,
            _flag(play.get("complete_pass")), _number(play.get("yards_after_catch")), _number(play.get("cpoe")),
            _flag(play.get("qb_scramble")) or 0, _flag(play.get("goal_to_go")),
            field_zone(play.get("yardline_100"), play.get("goal_to_go"), play.get("ydstogo")),
            _flag(play.get("pass_touchdown")) or 0, _flag(play.get("rush_touchdown")) or 0,
            _flag(play.get("first_down")) or 0, _text(chart.get("qb_location")),
            _flag(play.get("interception")) or 0,
            _text(seen.get("offense_players")), _text(seen.get("offense_positions")),
            _text(seen.get("defense_players")), _text(seen.get("defense_positions")),
        ))
    return rows


PENALTY_COLUMNS = ("season", "week", "season_type", "game_id", "play_id", "team", "opponent", "penalty_type",
                   "yards", "player_id", "player_name", "auto_first_down", "no_play", "epa_team", "down",
                   "ydstogo", "qtr")


def build_penalty_rows(pbp: Iterable[Mapping[str, Any]]) -> list[tuple]:
    """One tuple per enforced penalty (ordered as PENALTY_COLUMNS), kickoffs and punts included.

    Declined and offsetting flags carry no yardage and are dropped. `epa_team` is the flagged team's own EPA
    for the play and is only set on no-play flags, where the penalty is the whole play; on a live play the EPA
    belongs to the result of the snap, not the flag.
    """
    rows: list[tuple] = []
    for play in pbp:
        if _flag(play.get("penalty")) != 1 or _flag(play.get("play_deleted")) == 1:
            continue
        yards = _number(play.get("penalty_yards"))
        game_id, play_id = _text(play.get("game_id")), _int(play.get("play_id"))
        team = canon_team(play.get("penalty_team"))
        posteam, defteam = canon_team(play.get("posteam")), canon_team(play.get("defteam"))
        penalty_type = _text(play.get("penalty_type"))
        if not yards or yards <= 0 or not game_id or play_id is None or not team or not penalty_type:
            continue
        if team == posteam:
            opponent = defteam
        elif team == defteam:
            opponent = posteam
        else:
            continue
        if not opponent:
            continue
        no_play = 1 if _text(play.get("play_type")) == "no_play" else 0
        epa = _number(play.get("epa"))
        epa_team = (epa if team == posteam else -epa) if (no_play and epa is not None) else None
        rows.append((
            _int(play.get("season")), _int(play.get("week")), _text(play.get("season_type")), game_id, play_id,
            team, opponent, penalty_type, yards, _text(play.get("penalty_player_id")),
            _text(play.get("penalty_player_name")), _flag(play.get("first_down_penalty")) or 0, no_play,
            epa_team, _int(play.get("down")), _int(play.get("ydstogo")), _int(play.get("qtr")),
        ))
    return rows


PACKAGE_SNAP_COLUMNS = ("season", "team", "player_id", "position", "side", "package", "zone",
                        "snaps", "pass_snaps", "rush_snaps", "epa_sum")


def package_snap_rows(rows: Iterable[tuple]) -> list[tuple]:
    """Aggregate who was on the field into (season, team, player, position, side, package, zone) counts.

    `rows` are tuples from build_play_rows. A play counts once for every player in the participation
    lists, under the offense's personnel group (or the defense's package for defenders) and the field
    zone it was run from. Plays without participation data contribute nothing.

    Identical 11-man lineups repeat, so plays are first grouped by lineup and only each distinct lineup
    is expanded into players.
    """
    index = {column: position for position, column in enumerate(PLAY_COLUMNS)}
    zone_at, pass_at, rush_at, epa_at, season_at = (index[name] for name in ("zone", "is_pass", "is_rush", "epa", "season"))
    sides = tuple(
        (side, index[team_key], index[group_key], index[players_key], index[positions_key])
        for side, team_key, group_key, players_key, positions_key in (
            ("off", "posteam", "offense_group", "offense_players", "offense_positions"),
            ("def", "defteam", "defense_package", "defense_players", "defense_positions"),
        )
    )
    lineups: dict[tuple, list[float]] = {}
    for row in rows:
        zone = row[zone_at]
        if not zone:
            continue
        pass_play = int(row[pass_at] or 0)
        rush_play = int(row[rush_at] or 0)
        epa = row[epa_at] or 0.0
        season = row[season_at]
        for side, team_at, group_at, players_at, positions_at in sides:
            package = row[group_at]
            players = row[players_at]
            if not package or not players:
                continue
            key = (season, row[team_at], side, package, zone, players, row[positions_at] or "")
            cell = lineups.get(key)
            if cell is None:
                lineups[key] = [1, pass_play, rush_play, epa]
            else:
                cell[0] += 1
                cell[1] += pass_play
                cell[2] += rush_play
                cell[3] += epa
    totals: dict[tuple, list[float]] = {}
    for (season, team, side, package, zone, players, positions), (n, passes, rushes, epa) in lineups.items():
        names = players.split(";")
        if len(names) < 2:
            continue
        spots = positions.split(";")
        for position_index, player in enumerate(names):
            if not player:
                continue
            position = spots[position_index] if position_index < len(spots) else ""
            key = (season, team, player, position, side, package, zone)
            cell = totals.get(key)
            if cell is None:
                totals[key] = [n, passes, rushes, epa]
            else:
                cell[0] += n
                cell[1] += passes
                cell[2] += rushes
                cell[3] += epa
    return [(*key, int(cell[0]), int(cell[1]), int(cell[2]), cell[3]) for key, cell in totals.items()]
