from unittest.mock import Mock, patch
from pathlib import Path

from sports_aggregator.cfb import engine_picks
from sports_aggregator import tracked_refresh


def _record():
    bucket = {"wins": 0, "losses": 0, "pushes": 0, "n": 0,
              "record": "0-0", "hit_rate": None}
    return {"season": 2026, "engine_a": dict(bucket),
            "engine_b": dict(bucket), "totals": dict(bucket)}


def test_dashboard_counts_each_leg_and_keeps_pending_games_visible(monkeypatch):
    games = [
        {"game_id": 1, "away_team": "Alabama", "home_team": "Mississippi State",
         "completed": 0, "start_date": "2026-10-03T16:00:00+00:00"},
        {"game_id": 2, "away_team": "Stanford", "home_team": "Wake Forest",
         "completed": 0, "start_date": "2026-10-03T17:00:00+00:00"},
        {"game_id": 3, "away_team": "Tulane", "home_team": "Rice",
         "completed": 0, "start_date": "2026-10-03T18:00:00+00:00"},
    ]
    packets = {
        1: {"state": "engine_a_only", "state_label": "ENGINE A", "market_spread": 7,
            "engine_a": {"qualified": True, "selected_side": "away",
                         "selected_team": "Alabama", "route": "route"},
            "engine_b": {"qualified": False}, "frozen_at": "now"},
        2: {"state": "engine_b_only", "state_label": "ENGINE B", "market_spread": -2,
            "engine_a": {"qualified": False},
            "engine_b": {"qualified": True, "selected_side": "away",
                         "selected_team": "Stanford", "rules": ["rule"]},
            "totals": {"model_direction": "over", "closing_total": 48.5,
                       "calibrated_projected_total": 53, "closing_edge": 4.5,
                       "regime_benchmark": {"tracked": True, "label": "tracked"}},
            "frozen_at": "now"},
        3: {"state": "pending", "state_label": "INPUTS PENDING",
            "engine_a": {"qualified": False, "reason": "Line unavailable"},
            "engine_b": {"qualified": False}, "frozen_at": "now"},
    }
    monkeypatch.setattr(engine_picks, "games_for_week", lambda *args: games)
    monkeypatch.setattr(engine_picks, "manifest_for_games", lambda *args: packets)
    monkeypatch.setattr(engine_picks, "record_summary", lambda *args, **kwargs: _record())

    result = engine_picks.build_dashboard(object(), 2026, 5)

    assert result["counts"] == {
        "games": 3, "analyzed": 3, "pending": 1, "no_signal": 0,
        "engine_a": 1, "engine_b": 1, "totals": 1, "conflicts": 0,
    }
    assert result["engine_a"][0]["pick"] == "Alabama"
    assert result["engine_b"][0]["pick"] == "Stanford"
    assert result["totals"][0]["pick"] == "Over"
    assert len(result["audit"]) == 3


def test_pending_filter_does_not_hide_pending_from_the_audit():
    dashboard = {"engine_a": [], "engine_b": [], "totals": [], "audit": [
        {"away_team": "A", "home_team": "B", "state": "pending"},
        {"away_team": "C", "home_team": "D", "state": "none"},
    ]}
    result = engine_picks.filter_dashboard(dashboard, status="pending")
    assert [row["state"] for row in result["audit"]] == ["pending"]


def test_core_and_models_refresh_manifest_after_their_inputs():
    completed = {"step": "input", "status": "success"}
    manifest = {"step": "two-engine-manifest", "status": "success"}
    for segment in ("core", "models"):
        with patch.object(tracked_refresh, "_run_cfbd_split", return_value=completed), \
             patch.object(tracked_refresh, "_run_low_memory_phase", return_value=[completed]), \
             patch.object(tracked_refresh, "_refresh_two_engine_manifest",
                          return_value=manifest) as refresh:
            results = tracked_refresh._segment_results(
                segment, 2026, root=Path("."), log=Mock(), heartbeat=Mock())
        refresh.assert_called_once()
        assert results[-1] == manifest
