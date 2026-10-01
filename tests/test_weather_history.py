from sports_aggregator.nfl.weather_history import _VENUES, aggregate, is_indoor, venue, window_times
from sports_aggregator.nfl.weather_total_ablation import _wx


def test_window_starts_at_kickoff_hour_and_spans_the_game():
    assert window_times("2023-11-19T18:25:00+00:00") == [
        "2023-11-19T18:00", "2023-11-19T19:00", "2023-11-19T20:00", "2023-11-19T21:00"]
    assert window_times("2023-11-19T23:30:00+00:00")[-1] == "2023-11-20T02:00"  # crosses midnight


def test_aggregate_means_wind_maxes_gust_sums_rain_and_tolerates_gaps():
    hourly = {"time": ["a", "b", "c", "d"],
              "temperature_2m": [40.0, 42.0, None, 44.0], "wind_speed_10m": [10.0, 20.0, 30.0, 40.0],
              "wind_gusts_10m": [15.0, 35.0, 20.0, 25.0], "precipitation": [0.0, 0.1, 0.05, 0.0]}
    out = aggregate(hourly, ["a", "b", "c", "d"])
    assert out["temperature"] == 42.0 and out["wind_speed"] == 25.0
    assert out["wind_gust"] == 35.0 and abs(out["precipitation"] - 0.15) < 1e-9 and out["hours_used"] == 4
    previous = {"time": ["a"], "wind_speed_10m_previous_day1": [12.0]}
    assert aggregate(previous, ["a", "z"], "_previous_day1")["wind_speed"] == 12.0
    empty = aggregate({"time": ["a"], "temperature_2m": [None]}, ["a"])
    assert empty["temperature"] is None and empty["hours_used"] == 0


def test_recorded_roof_beats_venue_default_and_unknown_roof_uses_default():
    assert is_indoor("closed", "AT&T Stadium") and not is_indoor("open", "Lucas Oil Stadium")
    assert is_indoor(None, "Ford Field") and not is_indoor(None, "Lambeau Field")
    assert not is_indoor(None, "Not A Stadium")


def test_every_venue_has_plausible_coordinates():
    assert venue("Wembley Stadium")[0] > 50 and venue("Arena Corinthians")[0] < 0
    for name, (lat, lon, _) in _VENUES.items():
        assert -90 <= lat <= 90 and -180 <= lon <= 180, name
    assert venue("Gillette Stadium") == venue("Gillette Stadium") and venue(None) is None


def test_weather_features_zero_out_indoor_and_reject_missing_outdoor_inputs():
    indoor = {"indoor": 1, "temperature": None, "wind_speed": None, "wind_gust": None, "precipitation": None}
    assert _wx(indoor)["wx_dome"] == 1.0 and _wx(indoor)["wx_wind"] == 0.0
    outdoor = {"indoor": 0, "temperature": 30.0, "wind_speed": 12.0, "wind_gust": 25.0, "precipitation": 0.2}
    assert _wx(outdoor) == {"wx_wind": 12.0, "wx_gust": 25.0, "wx_cold": 20.0, "wx_precip": 0.2, "wx_dome": 0.0}
    assert _wx({**outdoor, "wind_speed": None}) is None and _wx(None) is None
