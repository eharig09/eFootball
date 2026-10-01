"""Weather-aware total for the live Football Lab chain.

Fits the calibrated-total ridge with game-window weather (weather_total_ablation
found oracle weather improves total MAE 10.748 -> 10.647, t=-2.3, 8/10 seasons)
on observed reanalysis weather, then scores upcoming games with the forecast
snapshot nearest kickoff (nfl_weather_history kind='live').

Every game gets a status. The caller keeps the core total unless it is "used" or
"indoor": no model, an outdoor game with no forecast, a forecast further out than
the backtest validated (day-1/day-2 issued forecasts; a 10-day forecast is far
noisier and would be read as if it were the realised weather), or a retractable
roof whose state for that game is unknown. Domes use the zero-weather encoding
the model was trained on.
"""
from __future__ import annotations

from contextlib import closing
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import TOTAL_FEATURES, _fit_ridge, _predict
from sports_aggregator.nfl.weather_history import is_indoor
from sports_aggregator.nfl.weather_total_ablation import WEATHER, _wx, load_weather

MODEL_LABEL = "weather-aware-total-v1"
#: A recorded roof is known per game historically; for an upcoming game it is not.
RETRACTABLE = frozenset({
    "AT&T Stadium", "Lucas Oil Stadium", "Mercedes-Benz Stadium", "NRG Stadium",
    "Reliant Stadium", "State Farm Stadium", "University of Phoenix Stadium",
})
#: Issued forecasts were validated at 1-2 days; allow a day of slack for Thursday-issued Sunday games.
MAX_LEAD_DAYS = 3
DOME = {"wx_wind": 0.0, "wx_gust": 0.0, "wx_cold": 0.0, "wx_precip": 0.0, "wx_dome": 1.0}


class LiveTotal:
    def __init__(self, repository: NFLRepository, historical_games: list[dict[str, Any]]) -> None:
        observed = load_weather(repository)[0]
        rows = [{**r, **observed[str(r["game_id"])]} for r in historical_games
                if str(r.get("game_id")) in observed]
        self.model = _fit_ridge(rows, TOTAL_FEATURES + WEATHER, "actual_total")
        self.live: dict[str, tuple[int, dict[str, float]]] = {}
        with closing(repository._connect()) as connection:
            for r in connection.execute(
                    "SELECT * FROM nfl_weather_history WHERE kind='live' ORDER BY lead_days DESC"):
                feats = _wx(r)
                if feats is not None:  # smallest lead wins: it is iterated last
                    self.live[str(r["game_id"])] = (int(r["lead_days"]), feats)

    def total(self, game: dict[str, Any], game_row: dict[str, Any]) -> dict[str, Any]:
        """{"status", "total" (None unless usable), "model", "weather"}."""
        def skip(status: str) -> dict[str, Any]:
            return {"status": status, "total": None, "model": None, "weather": None}

        if self.model is None:
            return skip("no_model")
        stadium, roof = game.get("stadium"), game.get("roof")
        if not roof and stadium in RETRACTABLE:
            return skip("roof_unknown")
        if is_indoor(roof, stadium):
            features, source, status = DOME, "indoor", "indoor"
        else:
            hit = self.live.get(str(game["game_id"]))
            if hit is None:
                return skip("no_forecast")
            lead, features = hit
            if lead > MAX_LEAD_DAYS:
                return skip("forecast_too_far_out")
            source, status = f"forecast_{lead}d_ahead", "used"
        return {
            "status": status,
            "total": _predict(self.model, {**game_row, **features}),
            "model": MODEL_LABEL,
            "weather": {"source": source, **{k: round(v, 3) for k, v in features.items()}},
        }
