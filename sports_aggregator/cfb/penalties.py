"""Team discipline for college football: penalties committed and drawn, ranked against the FBS.

College play-by-play has no structured penalty table, so flags are read out of the play text:

    PENALTY TULSA False Start (East,Cam) 5 yards from ACU33 to ACU38. NO PLAY.
    (10:08) Shotgun ... rush middle for 7 yards ... PENALTY TULSA Holding (Lucas,Landen) 3 yards from ... NO PLAY.
    Tulsa Penalty, Intentional Grounding (Kirk Francis) to the TLSA 19

Only enforced regular-season flags count: declined and offsetting penalties are dropped. Which team committed a
flag is resolved from the team token in the text; when neither side matches (an FCS opponent's abbreviation is not in
`teams`) the side that does not match the FBS team is taken, and only when that is also impossible does the sign of
the yardage decide. The ranking and display machinery is the NFL's (`nfl.penalties.profiles_from_rows`), so both
leagues show the same panels.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import re
from typing import Any

from sports_aggregator.cfb.derived_cache import derived
from sports_aggregator.nfl.penalties import panel_rows, profiles_from_rows

#: Flags before the snap (alignment, motion, clock): the ones a team controls entirely.
PRESNAP_TYPES = frozenset({
    "False Start", "Delay of Game", "Offside", "Encroachment", "Neutral Zone Infraction", "Illegal Formation",
    "Illegal Shift", "Illegal Motion", "Illegal Substitution", "Substitution Infraction", "Too Many Men on the Field",
    "Too Many Players on the Field", "Defensive Offside", "Offside on Defense", "Illegal Snap",
})
_QUERY = """
SELECT p.play_id, p.game_id, p.week, p.offense, p.defense, p.yards_gained, p.play_text, e.epa
FROM cfb_plays p
JOIN games g ON g.game_id = p.game_id AND g.season_type = 'regular'
LEFT JOIN cfb_play_epa e ON e.play_id = p.play_id
WHERE p.season = ? AND p.play_type = 'Penalty' {week_filter}
"""
_TYPE_END = re.compile(r"\s+\(|\s+\d+\s+yards?\b|\s+to the\b|\.|,\s*1ST\b|\s+1ST\b", re.IGNORECASE)
_PLAYER = re.compile(r"\(([^)]*)\)")
_YARDS = re.compile(r"(\d+)\s+yards?\b", re.IGNORECASE)
_CODE_PREFIX = re.compile(r"^[A-Z]{2,5}:\s*")
_JERSEY = re.compile(r"^#\d+\s+")
_SMALL_WORDS = frozenset({"of", "the", "on", "to", "for", "a", "in", "and"})
_ALIASES = {"falsestart": "False Start", "offsides": "Offside", "offside": "Offside", "facemask": "Face Mask",
            "delay of game": "Delay of Game", "12 men on the field": "Too Many Men on the Field"}
_TEAM_FIRST = re.compile(r"^(?P<team>.+?)\s+Penalty,\s*(?P<rest>.+)$", re.IGNORECASE | re.DOTALL)


def normalize_type(raw: str) -> str:
    """One spelling per flag: 'false start' / 'FALSESTART' / 'UNS: Unsportsmanlike Conduct' collapse together."""
    text = _CODE_PREFIX.sub("", raw.strip()) if raw[:1].isupper() and ":" in raw[:7] else raw.strip()
    key = re.sub(r"\s+", " ", text).lower()
    if key in _ALIASES:
        return _ALIASES[key]
    words = key.split(" ")
    return " ".join(word if (word in _SMALL_WORDS and index) else "-".join(part.capitalize() for part in word.split("-"))
                    for index, word in enumerate(words))


def _keys(school: str, abbreviation: str | None) -> list[str]:
    """Upper-case spellings a play's text may use for a team, longest first."""
    names = {school.upper(), school.upper().replace(".", ""), school.upper().replace(" ", "")}
    if abbreviation:
        names.add(abbreviation.upper())
    return sorted((name for name in names if name), key=len, reverse=True)


def _starts_with(text: str, keys: list[str]) -> str | None:
    upper = text.upper()
    for key in keys:
        if upper == key or upper.startswith(key + " ") or upper.startswith(key + ","):
            return key
    return None


def parse_penalty(text: str, offense_keys: list[str], defense_keys: list[str],
                  yards_gained: float | None) -> dict[str, Any] | None:
    """Fields of one enforced flag, or None for a declined/offsetting/unreadable one.

    Returns committed_by 'offense' or 'defense', the penalty type, enforced yards, the offender's name, whether it
    was a no-play and whether it carried an automatic first down.
    """
    lowered = text.lower()
    if "declined" in lowered or "offsetting" in lowered or "off-setting" in lowered:
        return None
    marker = re.search(r"PENALTY\s+(.*)$", text, re.DOTALL)
    committed_by = remainder = None
    if marker:
        rest = marker.group(1).strip()
        for side, keys in (("offense", offense_keys), ("defense", defense_keys)):
            key = _starts_with(rest, keys)
            if key:
                committed_by, remainder = side, rest[len(key):].lstrip(" ,")
                break
        if committed_by is None:
            remainder = rest.split(" ", 1)[1] if " " in rest else rest   # drop the unknown team token
    else:
        alt = _TEAM_FIRST.match(text.strip())
        if not alt:
            return None
        token, remainder = alt.group("team").strip(), alt.group("rest").strip()
        for side, keys in (("offense", offense_keys), ("defense", defense_keys)):
            if _starts_with(token, keys) or token.upper() in keys:
                committed_by = side
                break
    penalty_type = normalize_type(_TYPE_END.split(remainder, 1)[0].strip(" .,"))
    if not penalty_type or len(penalty_type) > 60:
        return None
    if committed_by is None:
        # One side is a known FBS team and did not match, so the flag is on the other bench; with neither
        # known, the yardage direction decides (offenses are moved back, defenses forward).
        committed_by = "offense" if (yards_gained or 0) < 0 else "defense"
    player = _PLAYER.search(remainder)
    name = None
    if player:
        raw = _JERSEY.sub("", player.group(1).strip())
        name = " ".join(part.strip() for part in raw.split(",")[::-1]) if "," in raw else raw
    yards = _YARDS.search(remainder)
    return {"committed_by": committed_by, "penalty_type": penalty_type,
            "yards": float(yards.group(1)) if yards else abs(float(yards_gained or 0)),
            "player_name": name or None, "no_play": "no play" in lowered,
            "auto_first_down": 1 if "1st down" in lowered else 0}


def _fbs_keys(connection) -> dict[str, list[str]]:
    return {school: _keys(school, abbreviation) for school, abbreviation in connection.execute(
        "SELECT school, abbreviation FROM teams WHERE classification = 'fbs'")}


def _build(repository, season: int, before_week: int | None = None) -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    week_filter = "AND p.week < ?" if before_week is not None else ""
    parameters: list[Any] = [int(season)] + ([int(before_week)] if before_week is not None else [])
    games: dict[str, set[str]] = defaultdict(set)
    with closing(repository._connect()) as connection:
        fbs = _fbs_keys(connection)
        for offense, game_id in connection.execute(
                """SELECT DISTINCT p.offense, p.game_id FROM cfb_plays p
                   JOIN games g ON g.game_id = p.game_id AND g.season_type = 'regular' WHERE p.season = ? """
                + week_filter, parameters):
            if offense in fbs:
                games[offense].add(game_id)
        for _pid, game_id, week, offense, defense, yards_gained, text, epa in connection.execute(
                _QUERY.format(week_filter=week_filter), parameters):
            if offense not in fbs and defense not in fbs:
                continue
            parsed = parse_penalty(text or "", fbs.get(offense) or _keys(offense, None),
                                   fbs.get(defense) or _keys(defense, None), yards_gained)
            if parsed is None:
                continue
            by_offense = parsed["committed_by"] == "offense"
            team, opponent = (offense, defense) if by_offense else (defense, offense)
            epa_team = None
            if parsed["no_play"] and epa is not None:
                epa_team = epa if by_offense else -epa      # the flagged team's own EPA on the no-play
            rows.append({**parsed, "team": team, "opponent": opponent, "week": week, "game_id": game_id,
                         "epa_team": epa_team, "player_id": None})
    # Only FBS teams are profiled and ranked; a flag drawn from (or committed against) an FCS opponent still
    # counts for the FBS side.
    return profiles_from_rows(rows, games, season, presnap_types=PRESNAP_TYPES, only_teams=set(fbs))


def league_penalties(repository, season: int, before_week: int | None = None) -> dict[str, dict[str, Any]]:
    return derived(repository, "cfb_penalties", lambda: _build(repository, season, before_week), int(season), before_week)


def team_penalties(repository, season: int, school: str, before_week: int | None = None) -> dict[str, Any] | None:
    profile = league_penalties(repository, season, before_week).get(school)
    if not profile or not profile["games"]:
        return None
    return profile


def team_view(repository, season: int, school: str, *, minimum_games: int = 3) -> dict[str, Any] | None:
    """Profile plus both panels' rows; the prior season stands in until a team has `minimum_games`."""
    profile, note = team_penalties(repository, season, school), ""
    if not profile or profile["games"] < minimum_games:
        prior = team_penalties(repository, season - 1, school)
        if prior:
            note = f"{season - 1} baseline" + (f" ({profile['games']} {season} game{'s' if profile['games'] != 1 else ''} so far)"
                                              if profile else "")
            profile = prior
    if not profile:
        return None
    # College play-by-play scores almost no no-play flags (the expected-points model skips them), so the EPA row
    # would read +0.00 for every team; the yardage and flag counts carry the panel instead.
    return {"profile": profile, "note": note,
            "committed": [row for row in panel_rows(profile, "committed") if row["key"] != "epa"],
            "drawn": [row for row in panel_rows(profile, "drawn") if row["key"] != "epa"]}


def matchup_view(repository, game: dict[str, Any], *, minimum_games: int = 3) -> dict[str, Any] | None:
    """Both teams' discipline for a game page, from games played before it (see situational_tendencies.matchup_view)."""
    season, away, home = int(game["season"]), game["away_team"], game["home_team"]
    before = int(game["week"]) if game.get("season_type", "regular") == "regular" else None
    current = {team: team_penalties(repository, season, team, before) for team in (away, home)}
    if all(profile and profile["games"] >= minimum_games for profile in current.values()):
        chosen, note = current, (f"through week {before - 1}" if before else "regular season")
    else:
        chosen = {team: team_penalties(repository, season - 1, team) for team in (away, home)}
        if not all(chosen.values()):
            return None
        note = f"{season - 1} baseline"
    return {"season": next(iter(chosen.values()))["season"], "note": note, "away": chosen[away], "home": chosen[home]}
