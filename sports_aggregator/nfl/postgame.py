"""Evidence-led NFL postgame packets built only from locally stored game data."""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl.passing import pass_zone_packet
from sports_aggregator.nfl.repository import NFLRepository


TEAM_BOX_METRICS = (
    ("Total yards", "total_yards", "big", False),
    ("Passing yards", "passing_yards", "big", False),
    ("Rushing yards", "rushing_yards", "big", False),
    ("First downs", "first_downs", "int", False),
    ("Third-down conversions", "third_down_conversions", "int", False),
    ("Sacks allowed", "sacks_suffered", "int", True),
    ("Turnovers", "turnovers", "int", True),
)

PLAYER_LEADERS = (
    ("Passing", "passing_yards", "passing yards"),
    ("Rushing", "rushing_yards", "rushing yards"),
    ("Receiving", "receiving_yards", "receiving yards"),
    ("Pressure", "def_sacks", "sacks"),
    ("Tackling", "def_tackles_solo", "solo tackles"),
    ("Takeaways", "def_interceptions", "interceptions"),
)


def _lean(away: float | None, home: float | None, away_team: str, home_team: str,
          *, lower: bool = False) -> str | None:
    if away is None or home is None or away == home:
        return None
    away_better = away < home if lower else away > home
    return away_team if away_better else home_team


def _market_result(game: dict[str, Any]) -> dict[str, Any]:
    away_score = game.get("away_score") or 0; home_score = game.get("home_score") or 0
    margin = away_score - home_score
    spread = game.get("spread_line")
    total = game.get("total_line")
    ats_value = margin + spread if spread is not None else None
    total_value = away_score + home_score - total if total is not None else None
    if ats_value is None:
        ats = "No stored spread"
    elif ats_value > 0:
        ats = f"{game['away_team']} covered"
    elif ats_value < 0:
        ats = f"{game['home_team']} covered"
    else:
        ats = "Push"
    if total_value is None:
        total_result = "No stored total"
    elif total_value > 0:
        total_result = "Over"
    elif total_value < 0:
        total_result = "Under"
    else:
        total_result = "Push"
    return {"ats": ats, "ats_margin": ats_value, "total": total_result,
            "total_margin": total_value, "points": away_score + home_score}


def postgame_packet(repository: NFLRepository, game: dict[str, Any],
                    efficiency_rows: list[dict[str, Any]],
                    player_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize result, decisive statistical edges, market, and game-only QB charts."""
    away, home = game["away_team"], game["home_team"]
    away_score = game.get("away_score") or 0; home_score = game.get("home_score") or 0
    winner = away if away_score > home_score else home if home_score > away_score else None
    team_stats = repository.game_team_stats(game["game_id"])
    situational = {row["team"]: row for row in repository.game_situational(game["game_id"])}
    efficiency = {row["team"]: row for row in efficiency_rows}

    def stat(team: str, key: str) -> float | None:
        value = team_stats.get(team, {}).get(key)
        if value is not None:
            return value
        if key == "total_yards":
            parts = [team_stats.get(team, {}).get(item) for item in
                     ("passing_yards", "rushing_yards")]
            return sum(parts) if any(item is not None for item in parts) else None
        if key == "turnovers":
            parts = [team_stats.get(team, {}).get(item, 0) or 0 for item in
                     ("passing_interceptions", "fumbles_lost_total")]
            return sum(parts)
        if key == "third_down_conversions":
            return situational.get(team, {}).get("third_down_conversions")
        return None

    team_box = []
    for label, key, value_format, lower in TEAM_BOX_METRICS:
        away_value, home_value = stat(away, key), stat(home, key)
        if away_value is None and home_value is None:
            continue
        team_box.append({"label": label, "away": away_value, "home": home_value,
                         "format": value_format,
                         "lean": _lean(away_value, home_value, away, home, lower=lower)})

    edge_specs = (
        ("Overall efficiency", "epa_per_play", "EPA/play", "signed2"),
        ("Passing efficiency", "pass_epa_per_play", "pass EPA/play", "signed2"),
        ("Rushing efficiency", "rush_epa_per_play", "rush EPA/play", "signed2"),
        ("Success rate", "success_rate", "success", "rate"),
        ("Explosive plays", "explosive_plays", "explosive plays", "int"),
    )
    edges = []
    for label, key, unit, value_format in edge_specs:
        away_value = efficiency.get(away, {}).get(key); home_value = efficiency.get(home, {}).get(key)
        leader = _lean(away_value, home_value, away, home)
        if leader:
            edges.append({"label": label, "team": leader,
                          "value": away_value if leader == away else home_value,
                          "unit": unit, "format": value_format,
                          "separation": abs(away_value - home_value)})
    edges.sort(key=lambda row: row["separation"], reverse=True)

    leaders = []
    for label, metric, unit in PLAYER_LEADERS:
        candidates = [row for row in player_rows if row.get(metric) is not None]
        if not candidates:
            continue
        leader = max(candidates, key=lambda row: row.get(metric) or 0)
        if not leader.get(metric):
            continue
        leaders.append({"label": label, "metric": metric, "unit": unit,
                        "player_id": leader["player_id"], "player_name": leader["player_name"],
                        "team": leader["team"], "value": leader[metric]})

    qb_charts = []
    for passer in repository.game_qb_pass_profiles(game["game_id"]):
        if passer["attempts"] < 3:
            continue
        chart = pass_zone_packet(
            passer, contributors=repository.pass_zone_contributors(
                game["season"], passer_player_id=passer["passer_player_id"],
                game_id=game["game_id"],
            ),
        )
        qb_charts.append({"player_id": passer["passer_player_id"],
                          "player_name": passer["passer_name"],
                          "team": passer["offense_team"], "profile": chart})

    result_read = "The stored play-by-play sample is not sufficient for an efficiency read."
    if winner and efficiency.get(winner, {}).get("epa_per_play") is not None:
        loser = home if winner == away else away
        winner_epa = efficiency[winner]["epa_per_play"]
        loser_epa = efficiency.get(loser, {}).get("epa_per_play")
        if loser_epa is not None and winner_epa >= loser_epa:
            result_read = (f"{winner} paired the final-score advantage with the stronger "
                           f"stored per-play efficiency ({winner_epa:+.2f} EPA/play).")
        elif loser_epa is not None:
            result_read = (f"{winner} won despite the lower stored per-play efficiency. "
                           "The aggregate points to scoring sequence or field position that "
                           "is not yet modeled as a separate leverage measure here.")

    return {
        "winner": winner, "margin": abs(away_score - home_score),
        "one_score": abs(away_score - home_score) <= 8,
        "market": _market_result(game), "team_box": team_box,
        "situational": situational, "edges": edges[:4], "leaders": leaders,
        "qb_charts": qb_charts, "result_read": result_read,
    }
