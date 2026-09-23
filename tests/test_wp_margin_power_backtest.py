from sports_aggregator.cfb.wp_margin_power_backtest import _paired_direction_test


def test_paired_direction_test_counts_discordant_pairs_and_exact_p_value():
    rows = [
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 4, "variant": 5},
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 4, "variant": 2},
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 2, "variant": 4},
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 2, "variant": 1},
    ]

    result = _paired_direction_test(rows, "base", "variant")

    assert result == {
        "n": 4,
        "both_correct": 1,
        "baseline_only_correct": 1,
        "variant_only_correct": 1,
        "both_wrong": 1,
        "discordant": 2,
        "exact_two_sided_p": 1.0,
    }


def test_paired_direction_test_excludes_market_and_model_pushes():
    rows = [
        {"actual_home_margin": 3, "market_home_margin": 3, "base": 4, "variant": 4},
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 3, "variant": 4},
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 4, "variant": None},
    ]

    assert _paired_direction_test(rows, "base", "variant")["n"] == 0


def test_paired_direction_test_is_numerically_stable_for_large_samples():
    rows = [
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 4, "variant": 2}
        for _ in range(1200)
    ] + [
        {"actual_home_margin": 7, "market_home_margin": 3, "base": 2, "variant": 4}
        for _ in range(800)
    ]

    result = _paired_direction_test(rows, "base", "variant")

    assert result["discordant"] == 2000
    assert 0.0 <= result["exact_two_sided_p"] < 0.001
