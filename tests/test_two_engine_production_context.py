from sports_aggregator.cfb import two_engine_live as live


class _Repo:
    path = "fake.sqlite3"


def test_historical_context_uses_frozen_research_scales(monkeypatch):
    live._HISTORICAL_CACHE.clear()

    def fail(*args, **kwargs):
        raise AssertionError("research lens builder must not run in production context")

    monkeypatch.setattr(live.ipl, "build_lens_rows", fail)
    monkeypatch.setattr(live.nsv2, "_load_rows", lambda repository: [])
    monkeypatch.setattr(
        live.nsv2, "_choose_line_rate",
        lambda rows, validation_season: (0.35, []),
    )
    monkeypatch.setattr(
        live.nsv2, "_line_elo_pass",
        lambda rows, learning_rate: ({}, {}),
    )

    context = live._historical_context(_Repo())

    assert context["lens_scales"] == live.FROZEN_LENS_SCALES
    assert context["hc_stats"] == live.FROZEN_HC_STATS
    assert context["qb_stats"] == live.FROZEN_QB_STATS
    assert context["historical_calibration_source"] == "frozen_2015_2025_research_artifacts"
