from sports_aggregator.tracked_refresh import _nearest_upcoming_week


class _Repo:
    def __init__(self, rows):
        self.rows = rows

    def upcoming_games(self, season, limit=1):
        assert season == 2026
        return self.rows[:limit]


def test_nearest_upcoming_week_uses_first_upcoming_game():
    assert _nearest_upcoming_week(_Repo([{"week": 4}, {"week": 5}]), 2026) == 4


def test_nearest_upcoming_week_handles_empty_schedule():
    assert _nearest_upcoming_week(_Repo([]), 2026) is None
