from sports_aggregator.cfb.convergence_four_signal import (
    _agreement_count,
    _old_agreement_count,
    cumulative_ladder_report,
    exact_ladder_report,
    old_vs_new_transition_report,
)


def _row(structural, line, elo, hit=True, residual=3.0):
    return {
        "structural_z_aligned": structural,
        "line_elo_z_aligned": line,
        "hc_qb_elo_aligned": elo,
        "hc_qb_elo_confirms": elo > 0,
        "aligned_residual": residual,
        "hit": hit,
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
