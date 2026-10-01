"""Per-team reshaping of the matchup-page data, for the shared UI kit's panels.

The existing packets list every comparison in one flat table (ranked, both directions mixed together). A reader asks a
team-shaped question -- "what does Pittsburgh have against Virginia Tech's defense?" -- so these functions group the same
data by the attacking team and hand the kit plain dicts. Nothing here recomputes a grade or a ranking.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from sports_aggregator.cfb.matchups import DISPLAY_ORDER

#: A margin this large (grade points) is drawn as a full-length edge bar.
FULL_BAR_MARGIN = 20.0
_BADGES = {
    "MISMATCH": ("Clear edge", "edge"),
    "STRENGTH_VS_STRENGTH": ("Strength vs strength", "marquee"),
    "EVEN": ("Evenly matched", "even"),
    "LOW_QUALITY": ("Neither unit graded well", "muted"),
}


def unit_matchup_panels(report: dict[str, Any], away: str, home: str,
                        identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One panel per attacking team: its five unit matchups, in a fixed order, from `game_matchup_report`'s items."""
    order = {label: index for index, label in enumerate(DISPLAY_ORDER)}
    panels = []
    for team, opponent in ((away, home), (home, away)):
        rows = []
        for item in sorted((item for item in report.get("matchups", []) if item["attack_team"] == team),
                           key=lambda item: order.get(item["label"], len(order))):
            attack, defend = item.get("attack_grade"), item.get("defend_grade")
            gap = (attack - defend) if attack is not None and defend is not None else None
            badge, tone = _BADGES.get(item.get("archetype"), ("", "muted"))
            rows.append({
                "unit": item["label"], "attack_label": item.get("attack_label"), "attack_grade": attack,
                "defend_label": item.get("defend_label"), "defend_grade": defend,
                "gap": gap, "bar": min(abs(gap) / FULL_BAR_MARGIN, 1.0) * 100 if gap is not None else 0,
                "side": "none" if gap is None else ("attack" if gap > 0 else "defend" if gap < 0 else "even"),
                "badge": badge, "tone": tone, "watch": item.get("interest"),
            })
        identity = identities.get(team, {})
        panels.append({"team": team, "opponent": opponent, "color": identity.get("color"),
                       "logo_url": identity.get("logo_url"), "rows": rows})
    return panels


def unit_grade_rows(units: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Both teams' grade for each unit, with the better side marked (higher grade wins)."""
    rows = []
    for unit in units:
        away, home = unit.get("away_grade"), unit.get("home_grade")
        lead = ("away" if away > home else "home" if home > away else None) if away is not None and home is not None else None
        rows.append({
            "unit": unit.get("label"), "lead": lead,
            "away": {"grade": away, "returning": _percent(unit.get("away_returning_share")), "usage": unit.get("away_usage")},
            "home": {"grade": home, "returning": _percent(unit.get("home_returning_share")), "usage": unit.get("home_usage")},
        })
    return rows


def _percent(share: float | None) -> float | None:
    return None if share is None else round(share * 100, 1)


def player_matchup_panels(matchups: Iterable[dict[str, Any]], away: str, home: str, season: int,
                          player_url: Callable[[Any, int], str | None],
                          identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The named player pairings grouped by the attacking player's school."""
    matchups = list(matchups)
    panels = []
    for team, opponent in ((away, home), (home, away)):
        rows = []
        for matchup in sorted((m for m in matchups if m["attacker"].get("school") == team),
                              key=lambda m: -(m.get("interest") or 0)):
            attacker, defender = matchup["attacker"], matchup["defender"]
            members = defender.get("members") or []
            rows.append({
                "label": matchup["label"], "why": matchup.get("why"),
                "player": attacker.get("player_name"), "player_url": player_url(attacker.get("cfbd_player_id"), season),
                "player_detail": " · ".join(part for part in (
                    attacker.get("position"), f"board #{attacker['board_rank']}" if attacker.get("board_rank") else None) if part),
                "player_grade": attacker.get("interest_score"),
                "against": defender.get("player_name"), "against_url": player_url(defender.get("cfbd_player_id"), season),
                "against_detail": (", ".join(f"{member['player_name']} {member['grade']:.1f}" for member in members)
                                   if members else " · ".join(part for part in (
                                       defender.get("position"),
                                       f"board #{defender['board_rank']}" if defender.get("board_rank") else None) if part)),
                "against_grade": defender.get("interest_score"), "watch": matchup.get("interest"),
            })
        identity = identities.get(team, {})
        panels.append({"team": team, "opponent": opponent, "color": identity.get("color"),
                       "logo_url": identity.get("logo_url"), "rows": rows})
    return panels


def advanced_metric_rows(game: dict[str, Any], national_context: dict[str, dict[str, dict[str, Any]]] | None,
                         definitions: Iterable[tuple[str, str, str, str]]) -> list[dict[str, Any]]:
    """One row per advanced metric: each team's offense and defense value with its national FBS rank and percentile."""
    metrics = game.get("advanced_metrics") or {}
    rows = []
    for label, offense_key, defense_key, fmt in definitions:
        cells = {}
        for side, team in (("away", game["away_team"]), ("home", game["home_team"])):
            for unit, key in (("offense", offense_key), ("defense", defense_key)):
                value = (metrics.get(team) or {}).get(key)
                context = ((national_context or {}).get(team) or {}).get(key) or {}
                cells[f"{side}_{unit}"] = {"value": value, "rank": context.get("rank"), "of": context.get("of"),
                                           "pctl": context.get("percentile")}
        rows.append({"metric": label, "format": fmt, **cells})
    return rows
