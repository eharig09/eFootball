from pathlib import Path

from sports_aggregator.cfb.page_visuals import (
    player_trend_chart_data, team_scoring_chart_series, team_trend_chart_data,
)


def test_team_trend_pairs_pass_rush_offense_defense_and_targets():
    advanced = [{
        "week": 1, "game_id": 10, "opponent": "B",
        "epa_per_play": .2, "defense_epa_per_play": -.1,
        "pass_epa_per_play": .3, "defense_pass_epa_per_play": -.2,
        "rush_epa_per_play": .1, "defense_rush_epa_per_play": 0.0,
    }]
    counting = [{
        "week": 1, "game_id": 10, "opponent": "B",
        "targets": 27, "targets_allowed": 21,
        "pass_attempts": 30, "defense_pass_attempts": 24,
        "rush_plays": 35, "defense_rush_plays": 29,
    }]
    charts = team_trend_chart_data(advanced, counting_rows=counting)
    by_key = {chart["key"]: chart for chart in charts}
    assert len(by_key) == len(charts)

    assert by_key["targets"]["values"][0]["value"] == 27
    assert by_key["targets_allowed"]["values"][0]["value"] == 21
    assert by_key["pass_epa_per_play"]["values"][0]["value"] == .3
    assert by_key["defense_pass_epa_per_play"]["values"][0]["value"] == -.2
    assert by_key["rush_plays"]["values"][0]["value"] == 35
    assert by_key["defense_rush_plays"]["values"][0]["value"] == 29


def test_player_and_team_context_series_have_unique_browser_keys():
    player = player_trend_chart_data([{
        "week": 1, "week_label": "W1", "completions": 18, "attempts": 25,
    }])
    team = team_scoring_chart_series([], [{
        "week": 1, "completions": 21, "rush_yards": 140,
    }])
    combined = player + team
    keys = [chart["key"] for chart in combined]

    assert "completions" in keys
    assert "team_completions" in keys
    assert len(keys) == len(set(keys))


def test_player_page_loads_portal_script_for_zone_tooltips():
    template = Path("templates/cfb_player.html").read_text(encoding="utf-8")
    assert "filename='tooltips.js'" in template
