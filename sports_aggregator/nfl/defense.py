"""Reusable NFL defensive profiles and mirrored matchup comparisons."""

from __future__ import annotations

from typing import Any, Iterable

from sports_aggregator.nfl.pff import NFLPFFService
from sports_aggregator.nfl.ranking import rank_within
from sports_aggregator.nfl.repository import NFLRepository


DEFENSE_GROUPS = (
    ("Traditional", "NFL box scores", (
        ("Points allowed / game", "points_allowed_per_game", "f1", True),
        ("Pass yards allowed / game", "pass_yards_allowed_per_game", "f1", True),
        ("Rush yards allowed / game", "rush_yards_allowed_per_game", "f1", True),
        ("Completion rate allowed", "completion_rate_allowed", "rate", True),
        ("Pass yards / attempt allowed", "pass_yards_per_attempt_allowed", "f2", True),
        ("Rush yards / carry allowed", "rush_yards_per_carry_allowed", "f2", True),
        ("First downs allowed / game", "first_downs_allowed_per_game", "f1", True),
    )),
    ("Disruption", "NFL box scores", (
        ("Takeaways / game", "takeaways_per_game", "f1", False),
        ("Sacks / game", "sacks_per_game", "f1", False),
        ("QB hits / game", "qb_hits_per_game", "f1", False),
        ("Tackles for loss / game", "tackles_for_loss_per_game", "f1", False),
        ("Passes defended / game", "passes_defended_per_game", "f1", False),
    )),
    ("Efficiency allowed", "nflfastR play-by-play", (
        ("EPA / play allowed", "defensive_epa_allowed", "signed2", True),
        ("Dropback EPA allowed", "defensive_pass_epa_allowed", "signed2", True),
        ("Rush EPA allowed", "defensive_rush_epa_allowed", "signed2", True),
        ("Success rate allowed", "defensive_success_allowed", "rate", True),
        ("Explosive rate allowed", "defensive_explosive_allowed", "rate", True),
    )),
    ("Next Gen allowed", "NFL Next Gen Stats", (
        ("CPOE allowed", "pass_cpoe", "pct", True),
        ("Time to throw faced", "pass_time_to_throw", "f2", True),
        ("Receiver separation allowed", "rec_separation", "f2", True),
        ("Rush yards over expected / carry", "rush_yards_over_expected_per_carry", "signed2", True),
        ("YAC over expected / reception", "rec_yac_over_expected_per_reception", "signed2", True),
        ("Stacked box rate faced", "rush_stacked_box_pct", "pct", False),
    )),
    ("PFF unit grades", "PFF licensed local data", (
        ("Coverage grade", "coverage_grade", "f1", False),
        ("Pass-rush grade", "pass_rush_grade", "f1", False),
        ("Run-defense grade", "run_defense_grade", "f1", False),
    )),
)


def _profile_rows(rows: list[dict[str, Any]], team: str,
                  definitions: Iterable[tuple[str, str, str, bool]]) -> list[dict[str, Any]]:
    current = next((row for row in rows if row.get("team") == team), {})
    output = []
    for label, key, value_format, lower in definitions:
        value = current.get(key)
        if value is None:
            continue
        rank = rank_within(rows, id_key="team", value_key=key, lower_is_better=lower).get(team, {})
        output.append({"label": label, "key": key, "value": value, "format": value_format,
                       "rank": rank.get("rank"), "of": rank.get("of"),
                       "lower_is_better": lower})
    return output


def defensive_profile(repository: NFLRepository, pff: NFLPFFService, season: int, team: str,
                      *, before_week: int | None = None,
                      pff_season: int | None = None) -> dict[str, Any]:
    traditional = repository.league_defensive_summary(season, before_week=before_week)
    efficiency = repository.league_efficiency(season, before_week=before_week)
    ngs = repository.league_ngs_summary(season, before_week=before_week, defense=True)
    grades = pff.league_unit_grades(pff_season) if pff_season is not None else []
    pools = (traditional, traditional, efficiency, ngs, grades)
    groups = []
    for (label, source, definitions), rows in zip(DEFENSE_GROUPS, pools):
        metrics = _profile_rows(rows, team, definitions)
        if metrics:
            groups.append({"label": label, "source": source, "rows": metrics})
    games = next((row.get("games") for row in traditional if row.get("team") == team), None)
    return {"team": team, "season": season, "games": games, "pff_season": pff_season,
            "groups": groups, "has_data": bool(groups)}


MATCHUP_SPECS = (
    ("Traditional", "NFL box scores", (
        ("Scoring", "points_per_game", "points_allowed_per_game", "f1", False, True),
        ("Passing yards / game", "passing_yards_per_game", "pass_yards_allowed_per_game", "f1", False, True),
        ("Rushing yards / game", "rushing_yards_per_game", "rush_yards_allowed_per_game", "f1", False, True),
        ("Completion rate", "completion_rate", "completion_rate_allowed", "rate", False, True),
        ("Pass yards / attempt", "pass_yards_per_attempt", "pass_yards_per_attempt_allowed", "f2", False, True),
        ("Rush yards / carry", "rush_yards_per_carry", "rush_yards_per_carry_allowed", "f2", False, True),
        ("First downs / game", "first_downs_per_game", "first_downs_allowed_per_game", "f1", False, True),
        ("Ball security vs takeaways", "turnovers_per_game", "takeaways_per_game", "f1", True, False),
        ("Protection vs sacks", "sacks_allowed_per_game", "sacks_per_game", "f1", True, False),
    )),
    ("Efficiency", "nflfastR play-by-play", (
        ("Overall EPA / play", "epa_per_play", "defensive_epa_allowed", "signed2", False, True),
        ("Dropback EPA", "pass_epa_per_play", "defensive_pass_epa_allowed", "signed2", False, True),
        ("Rush EPA", "rush_epa_per_play", "defensive_rush_epa_allowed", "signed2", False, True),
        ("Success rate", "success_rate", "defensive_success_allowed", "rate", False, True),
        ("Explosive rate", "explosive_rate", "defensive_explosive_allowed", "rate", False, True),
    )),
    ("Next Gen Stats", "NFL Next Gen Stats", (
        ("CPOE", "pass_cpoe", "pass_cpoe", "pct", False, True),
        ("Receiver separation", "rec_separation", "rec_separation", "f2", False, True),
        ("Rush YOE / carry", "rush_yards_over_expected_per_carry", "rush_yards_over_expected_per_carry", "signed2", False, True),
        ("YACOE / reception", "rec_yac_over_expected_per_reception", "rec_yac_over_expected_per_reception", "signed2", False, True),
    )),
    ("PFF", "PFF licensed local data", (
        ("Pass block vs pass rush", "pass_block_grade", "pass_rush_grade", "f1", False, False),
        ("Run block vs run defense", "run_block_grade", "run_defense_grade", "f1", False, False),
        ("Routes vs coverage", "route_grade", "coverage_grade", "f1", False, False),
        ("Rushing vs run defense", "rushing_grade", "run_defense_grade", "f1", False, False),
    )),
)


def matchup_defense_cards(repository: NFLRepository, pff: NFLPFFService, season: int,
                          before_week: int, away: str, home: str, *,
                          baseline_season: int, pff_season: int | None) -> list[dict[str, Any]]:
    cutoff = before_week if baseline_season == season else None
    offense_pools = {
        "Traditional": repository.league_team_summary(baseline_season, before_week=cutoff),
        "Efficiency": repository.league_efficiency(baseline_season, before_week=cutoff),
        "Next Gen Stats": repository.league_ngs_summary(baseline_season, before_week=cutoff),
        "PFF": pff.league_unit_grades(pff_season) if pff_season is not None else [],
    }
    defense_pools = {
        "Traditional": repository.league_defensive_summary(baseline_season, before_week=cutoff),
        "Efficiency": offense_pools["Efficiency"],
        "Next Gen Stats": repository.league_ngs_summary(
            baseline_season, before_week=cutoff, defense=True),
        "PFF": offense_pools["PFF"],
    }
    cards = []
    for offense, defense in ((away, home), (home, away)):
        sections = []
        for label, source, specs in MATCHUP_SPECS:
            offense_pool = offense_pools[label]; defense_pool = defense_pools[label]
            offense_values = next((row for row in offense_pool if row.get("team") == offense), {})
            defense_values = next((row for row in defense_pool if row.get("team") == defense), {})
            rows = []
            for metric_label, offense_key, defense_key, value_format, offense_lower, defense_lower in specs:
                offense_value = offense_values.get(offense_key); defense_value = defense_values.get(defense_key)
                if offense_value is None or defense_value is None:
                    continue
                offense_rank = rank_within(
                    offense_pool, id_key="team", value_key=offense_key,
                    lower_is_better=offense_lower).get(offense, {})
                defense_rank = rank_within(
                    defense_pool, id_key="team", value_key=defense_key,
                    lower_is_better=defense_lower).get(defense, {})
                left = offense_rank.get("rank"); right = defense_rank.get("rank")
                separation = abs(left - right) if left is not None and right is not None else None
                lean = (offense if separation is not None and left + 4 <= right else
                        defense if separation is not None and right + 4 <= left else "Even")
                rows.append({
                    "label": metric_label, "format": value_format,
                    "offense_value": offense_value, "defense_value": defense_value,
                    "offense_rank": left, "offense_of": offense_rank.get("of"),
                    "defense_rank": right, "defense_of": defense_rank.get("of"),
                    "separation": separation, "lean": lean,
                    "strength": ("strong" if separation is not None and separation >= 12 else
                                 "moderate" if separation is not None and separation >= 5 else "even"),
                })
            if rows:
                sections.append({"label": label, "source": source, "rows": rows})
        cards.append({"offense": offense, "defense": defense, "sections": sections,
                      "has_data": bool(sections)})
    return cards
