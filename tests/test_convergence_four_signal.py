from sports_aggregator.cfb.convergence_four_signal import (
    _agreement_count,
    _old_agreement_count,
    cumulative_ladder_report,
    exact_ladder_report,
    old_vs_new_transition_report,
    policy_comparison_report,
    three_of_four_origin_report,
)


def _row(structural, line, elo, hit=True, residual=3.0, spread="<3", season=2024):
    return {
        "structural_z_aligned": structural,
        "line_elo_z_aligned": line,
        "hc_qb_elo_aligned": elo,
        "hc_qb_elo_confirms": elo > 0,
        "aligned_residual": residual,
        "hit": hit,
        "spread_bucket": spread,
        "season": season,
    }


def _decorate(row):
    row = dict(row)
    row["old_agreement_count"] = _old_agreement_count(row)
    row["agreement_count"] = _agreement_count(row)
    return row


def test_agreement_counts_anchor_on_margin_power_vote():
    assert _agreement_count(_row(1.0, 1.0, 1.0)) == 4
    assert _agreement_count(_row(1.0, -1.0, 1.0)) == 3
    assert _agreement_count(_row(-1.0, -1.0, 1.0)) == 2
    assert _agreement_count(_row(-1.0, -1.0, -1.0)) == 1


def test_old_agreement_count_matches_existing_three_signal_frame():
    assert _old_agreement_count(_row(1.0, 1.0, -1.0)) == 3
    assert _old_agreement_count(_row(1.0, -1.0, 1.0)) == 2
    assert _old_agreement_count(_row(-1.0, -1.0, 1.0)) == 1


def test_exact_ladder_keeps_tiers_separate():
    rows = [
        _decorate(_row(1, 1, 1, True, 4)),
        _decorate(_row(1, 1, -1, False, -2)),
        _decorate(_row(1, -1, -1, True, 1)),
        _decorate(_row(-1, -1, -1, False, -5)),
    ]
    result = {r["agreement"]: r for r in exact_ladder_report(rows)}
    assert result["4/4"]["n"] == 1
    assert result["3/4"]["n"] == 1
    assert result["2/4"]["n"] == 1
    assert result["1/4"]["n"] == 1


def test_cumulative_ladder_expands_volume_monotonically():
    rows = [
        _decorate(_row(1, 1, 1)),
        _decorate(_row(1, 1, -1)),
        _decorate(_row(1, -1, -1)),
        _decorate(_row(-1, -1, -1)),
    ]
    result = {r["rule"]: r for r in cumulative_ladder_report(rows)}
    assert result[">=4/4"]["n"] == 1
    assert result[">=3/4"]["n"] == 2
    assert result[">=2/4"]["n"] == 3
    assert result[">=1/4"]["n"] == 4


def test_transition_report_shows_elo_promotion_of_old_state():
    rows = [
        _decorate(_row(1, 1, 1, True, 5)),
        _decorate(_row(1, 1, -1, False, -3)),
        _decorate(_row(1, -1, 1, True, 2)),
    ]
    result = {
        (r["old_state"], r["hc_qb_elo"]): r
        for r in old_vs_new_transition_report(rows)
    }
    assert result[("3/3", "agrees")]["new_state"] == "4/4"
    assert result[("3/3", "agrees")]["n"] == 1
    assert result[("3/3", "disagrees")]["new_state"] == "3/4"
    assert result[("3/3", "disagrees")]["n"] == 1
    assert result[("2/3", "agrees")]["new_state"] == "3/4"
    assert result[("2/3", "agrees")]["n"] == 1



def test_three_of_four_origin_keeps_promotions_and_downgrades_separate():
    rows = [
        _decorate(_row(1, 1, -1, True, 4, spread="3-6.5")),   # old 3/3 -> 3/4
        _decorate(_row(1, -1, 1, True, 5, spread="3-6.5")),   # old 2/3 -> 3/4
        _decorate(_row(-1, 1, 1, False, -2, spread="14+")),   # old 2/3 -> 3/4
    ]
    result = three_of_four_origin_report(rows)
    downgraded = result["old_3_of_3_elo_disagrees"]
    promoted = result["old_2_of_3_elo_agrees"]
    assert downgraded["overall"]["n"] == 1
    assert promoted["overall"]["n"] == 2
    assert promoted["by_spread_bucket"]["3-6.5"]["n"] == 1
    assert promoted["by_spread_bucket"]["14+"]["n"] == 1


def test_predeclared_policy_comparison_applies_spread_and_origin_rules():
    rows = [
        _decorate(_row(1, 1, 1, True, 5, spread="<3")),       # 4/4
        _decorate(_row(1, 1, -1, False, -2, spread="<3")),    # old 3/3 Elo disagrees
        _decorate(_row(1, -1, 1, True, 3, spread="3-6.5")),   # promoted 2/3
        _decorate(_row(1, -1, 1, False, -4, spread="14+")),   # promoted 2/3 but 14+
        _decorate(_row(-1, -1, 1, True, 2, spread="<3")),     # 2/4, excluded
    ]
    result = {r["policy"]: r for r in policy_comparison_report(rows)}
    assert result["old_full_convergence_baseline"]["n"] == 2
    assert result["four_of_four_only"]["n"] == 1
    assert result["at_least_three_of_four"]["n"] == 4
    assert result["at_least_three_of_four_exclude_14_plus"]["n"] == 3
    assert result["four_of_four_plus_promoted_two_of_three_exclude_14_plus"]["n"] == 2
