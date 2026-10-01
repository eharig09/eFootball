"""Percentile panels for NFL team pages: value, league percentile, and 1-N rank per stat."""

from __future__ import annotations

from typing import Any, Iterable

from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.ranking import rank_within
from sports_aggregator.nfl.repository import NFLRepository


# (label, key, format, lower_is_better, neutral). Neutral rows describe identity,
# not quality, so the page draws them gray instead of green/red.
OFFENSE_GROUPS = (
    ("Efficiency", "efficiency", (
        ("EPA / play", "epa_per_play", "signed2", False, False),
        ("Dropback EPA", "pass_epa_per_play", "signed2", False, False),
        ("Rush EPA", "rush_epa_per_play", "signed2", False, False),
        ("Success rate", "success_rate", "rate", False, False),
        ("Explosive rate", "explosive_rate", "rate", False, False),
    )),
    ("Production", "traditional", (
        ("Points / game", "points_per_game", "f1", False, False),
        ("Pass yards / game", "passing_yards_per_game", "f1", False, False),
        ("Rush yards / game", "rushing_yards_per_game", "f1", False, False),
        ("Yards / attempt", "pass_yards_per_attempt", "f2", False, False),
        ("Yards / carry", "rush_yards_per_carry", "f2", False, False),
        ("Turnovers / game", "turnovers_per_game", "f1", True, False),
        ("Sacks allowed / game", "sacks_allowed_per_game", "f1", True, False),
    )),
    ("Next Gen", "ngs", (
        ("CPOE", "pass_cpoe", "pct", False, False),
        ("Time to throw", "pass_time_to_throw", "f2", False, True),
        ("Receiver separation", "rec_separation", "f2", False, False),
        ("Rush yards over expected / carry", "rush_yards_over_expected_per_carry", "signed2", False, False),
    )),
    ("PFF unit grades", "pff", (
        ("Pass blocking", "pass_block_grade", "f1", False, False),
        ("Run blocking", "run_block_grade", "f1", False, False),
        ("Receiving routes", "route_grade", "f1", False, False),
        ("Rushing", "rushing_grade", "f1", False, False),
    )),
)

DEFENSE_NEUTRAL = frozenset({"pass_time_to_throw", "rush_stacked_box_pct"})


from sports_aggregator.ranked_panels import panel_row as _row, percentile, rows_for_team, tone  # noqa: F401  (re-exported)


def _rows(pool: list[dict[str, Any]], team: str,
          definitions: Iterable[tuple[str, str, str, bool, bool]]) -> list[dict[str, Any]]:
    return rows_for_team(pool, team, "team", definitions)


def offense_panel(repository: NFLRepository, pff: NFLPFFService, season: int, team: str,
                  *, pff_season: int | None = None) -> dict[str, Any]:
    pools = {
        "efficiency": repository.league_efficiency(season),
        "traditional": repository.league_team_summary(season),
        "ngs": repository.league_ngs_summary(season),
        "pff": pff.league_unit_grades(pff_season) if pff_season is not None else [],
    }
    groups = []
    for label, pool_key, definitions in OFFENSE_GROUPS:
        rows = _rows(pools[pool_key], team, definitions)
        if rows:
            groups.append({"label": label, "rows": rows})
    return {"season": season, "groups": groups, "has_data": bool(groups)}


def defense_panel(profile: dict[str, Any]) -> dict[str, Any]:
    """Re-shape defensive_profile()'s ranked rows into the shared panel structure."""
    groups = []
    for group in profile.get("groups") or ():
        rows = [
            _row(row["label"], row["key"], row["format"], row["value"], row.get("rank"),
                 row.get("of"), row["key"] in DEFENSE_NEUTRAL)
            for row in group["rows"]
        ]
        if rows:
            groups.append({"label": group["label"], "rows": rows})
    return {"season": profile.get("season"), "groups": groups, "has_data": bool(groups)}


def scoring_ranks(repository: NFLRepository, season: int, team: str) -> dict[str, int | None]:
    """League rank of points scored (high = 1) and allowed (low = 1)."""
    pool = repository.league_team_summary(season)
    return {
        "pf": rank_within(pool, id_key="team", value_key="points_per_game").get(team, {}).get("rank"),
        "pa": rank_within(pool, id_key="team", value_key="points_allowed_per_game",
                          lower_is_better=True).get(team, {}).get("rank"),
    }


def ranked_stats_panel(groups: Iterable[tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """Panel from stats that already carry a direction-aware `rank` and `of`
    (player headline / Next Gen stats), so the page shares one bar component."""
    output = []
    for label, stats in groups:
        rows = [_row(stat["label"], stat.get("key", stat["label"]), stat.get("format", "text"),
                     stat.get("value"), stat.get("rank"), stat.get("of"), False)
                for stat in stats if stat.get("value") is not None]
        if rows:
            output.append({"label": label, "rows": rows})
    return {"season": None, "groups": output, "has_data": bool(output)}


def share_rows(players: list[dict[str, Any]], share_key: str, detail) -> list[dict[str, Any]]:
    """Rows for the bar_table macro: label, link, sub-line, bar width (relative to the leader), value."""
    top = max((float(player.get(share_key) or 0) for player in players), default=0) or 1.0
    rows = []
    for player in players:
        share = float(player.get(share_key) or 0)
        rows.append({"label": player.get("player_name"), "url": player.get("player_url"),
                     "sub": detail(player), "bar": share / top, "value": f"{share * 100:.1f}%"})
    return rows
