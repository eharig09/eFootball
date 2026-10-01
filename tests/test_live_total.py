import numpy as np

from sports_aggregator.nfl.live_total import MAX_LEAD_DAYS, LiveTotal
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES
from sports_aggregator.nfl.weather_total_ablation import WEATHER

FEATURES = TOTAL_FEATURES + WEATHER
ROW = {k: 0.0 for k in TOTAL_FEATURES}


def _live(live=None, with_model=True):
    t = object.__new__(LiveTotal)
    beta = np.zeros(len(FEATURES) + 1)
    beta[0] = 44.0
    beta[1 + FEATURES.index("wx_wind")] = -0.5
    beta[1 + FEATURES.index("wx_dome")] = 2.0
    t.model = {"features": FEATURES, "means": np.zeros(len(FEATURES)), "scales": np.ones(len(FEATURES)),
               "beta": beta} if with_model else None
    t.live = live or {}
    return t


def _fc(wind):
    return {"wx_wind": wind, "wx_gust": 0.0, "wx_cold": 0.0, "wx_precip": 0.0, "wx_dome": 0.0}


def test_outdoor_game_uses_forecast_within_validated_lead():
    t = _live({"g": (1, _fc(10.0))})
    r = t.total({"game_id": "g", "stadium": "Lambeau Field", "roof": "outdoors"}, ROW)
    assert r["status"] == "used" and abs(r["total"] - 39.0) < 1e-9
    assert r["weather"]["source"] == "forecast_1d_ahead"


def test_far_out_forecast_is_not_trusted():
    t = _live({"g": (MAX_LEAD_DAYS + 1, _fc(19.0))})
    r = t.total({"game_id": "g", "stadium": "Lambeau Field", "roof": "outdoors"}, ROW)
    assert r["status"] == "forecast_too_far_out" and r["total"] is None


def test_dome_needs_no_forecast_and_uses_dome_encoding():
    r = _live().total({"game_id": "g", "stadium": "Ford Field", "roof": "dome"}, ROW)
    assert r["status"] == "indoor" and abs(r["total"] - 46.0) < 1e-9


def test_unknown_roof_missing_forecast_and_missing_model_all_fall_back():
    t = _live()
    assert t.total({"game_id": "g", "stadium": "Lucas Oil Stadium", "roof": None}, ROW)["status"] == "roof_unknown"
    assert t.total({"game_id": "g", "stadium": "Lambeau Field", "roof": None}, ROW)["status"] == "no_forecast"
    assert _live(with_model=False).total({"game_id": "g", "stadium": "Ford Field", "roof": "dome"}, ROW)["status"] == "no_model"
    # A recorded roof on a retractable venue is known, so it is not "unknown".
    assert t.total({"game_id": "g", "stadium": "Lucas Oil Stadium", "roof": "closed"}, ROW)["status"] == "indoor"
