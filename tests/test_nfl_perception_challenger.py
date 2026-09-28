from sports_aggregator.nfl.perception_challenger import narrative_tags


def test_hot_offense_tag_is_explicitly_a_proxy_and_fades_under():
    tags = narrative_tags({
        "home_team": "A", "away_team": "B", "spread_line": 1.0,
        "total_line": 44.0, "pregame_market_total_avg": 44.5,
        "pregame_league_team_ppg": 22.0,
        "home_season_ppg": 27.0, "away_season_ppg": 25.0,
        "home_last_points": 28.0, "away_last_points": 24.0,
    })

    tag = next(item for item in tags if item["key"] == "two_hot_offenses_average_total")
    assert tag["market_narrative"] == "over"
    assert tag["contrarian_fade"] == "under"


def test_blowout_favorite_tag_orients_fade_to_opponent():
    tags = narrative_tags({
        "home_team": "HOME", "away_team": "AWAY", "spread_line": 4.0,
        "total_line": 47.0, "pregame_market_total_avg": 44.0,
        "pregame_league_team_ppg": 22.0,
        "home_season_ppg": 23.0, "away_season_ppg": 21.0,
        "home_last_points": 35.0, "away_last_points": 17.0,
        "home_last_margin": 21.0, "home_season_win_pct": .75,
        "home_preseason_win_pct_proxy": .50,
    })

    tag = next(item for item in tags if item["key"] == "post_blowout_favorite")
    assert tag["market_narrative"] == "HOME"
    assert tag["contrarian_fade"] == "AWAY"
