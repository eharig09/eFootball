from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sports_aggregator.tracked_refresh import _segment_for_light


EASTERN = ZoneInfo("America/New_York")


class SegmentedRefreshScheduleTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "CFB_REFRESH_TIMEZONE": "America/New_York",
        "CFB_REFRESH_CORE_HOURS": "6,18",
        "CFB_REFRESH_CONTENT_HOURS": "10,16",
        "CFB_REFRESH_ROSTER_HOURS": "12",
        "CFB_REFRESH_STATS_HOURS": "22",
        "CFB_REFRESH_MODEL_HOURS": "23",
    }, clear=False)
    def test_each_light_hour_routes_to_one_small_segment(self):
        expected = {
            6: "core",
            10: "content",
            12: "rosters",
            16: "content",
            18: "core",
            22: "stats",
            23: "models",
        }
        for hour, segment in expected.items():
            with self.subTest(hour=hour):
                moment = datetime(2026, 8, 27, hour, 0, tzinfo=EASTERN)
                self.assertEqual(_segment_for_light(moment), segment)

    def test_unscheduled_manual_light_defaults_to_core(self):
        moment = datetime(2026, 8, 27, 15, 0, tzinfo=EASTERN)
        with patch.dict(os.environ, {
            "CFB_REFRESH_CORE_HOURS": "6,18",
            "CFB_REFRESH_CONTENT_HOURS": "10,16",
            "CFB_REFRESH_ROSTER_HOURS": "12",
            "CFB_REFRESH_STATS_HOURS": "22",
            "CFB_REFRESH_MODEL_HOURS": "23",
        }, clear=False):
            self.assertEqual(_segment_for_light(moment), "core")


if __name__ == "__main__":
    unittest.main()


# --- catch-up: a skipped slot must not wait a day -----------------------------------------------
from datetime import timedelta

from sports_aggregator.tracked_refresh import SEGMENT_MAX_AGE_HOURS


def _health(now, ages):
    """A refresh roll-up in which each segment last ran `ages[name]` hours ago."""
    return {name: {"last_run_at": (now - timedelta(hours=hours)).isoformat()}
            for name, hours in ages.items()}


def _fresh(now, **overrides):
    ages = {name: limit / 4 for name, limit in SEGMENT_MAX_AGE_HOURS.items()}
    ages.update(overrides)
    return _health(now, ages)


def _tick(hour):
    return datetime(2026, 10, 6, hour, tzinfo=ZoneInfo("America/New_York"))


def test_nothing_overdue_keeps_the_clock_schedule():
    moment = _tick(10)
    assert _segment_for_light(moment, _fresh(moment)) == "content"


def test_without_health_the_clock_alone_decides():
    assert _segment_for_light(_tick(23)) == "models"


def test_a_segment_overdue_is_run_at_a_later_tick_instead_of_waiting_for_its_hour():
    # models owns 23:00. It was skipped last night (lock held), so at 10:00 it is 34h old.
    moment = _tick(10)
    health = _fresh(moment, models=34.0)
    assert _segment_for_light(moment, health) == "models"


def test_the_most_overdue_segment_goes_first():
    moment = _tick(12)
    health = _fresh(moment, models=40.0, analytics=75.0)   # 1.3x and 2.5x their limits
    assert _segment_for_light(moment, health) == "analytics"


def test_the_segment_that_owns_the_hour_wins_when_it_is_itself_overdue():
    moment = _tick(6)      # core's hour
    health = _fresh(moment, core=20.0, models=90.0)
    assert _segment_for_light(moment, health) == "core"


def test_a_segment_that_never_ran_counts_as_overdue():
    moment = _tick(10)
    health = _fresh(moment)
    del health["analytics"]
    assert _segment_for_light(moment, health) == "analytics"


def test_a_degraded_run_counts_as_a_run():
    """An always-degraded segment must not read as perpetually overdue and starve the rest."""
    moment = _tick(10)
    health = _fresh(moment)
    health["analytics"] = {"last_run_at": (moment - timedelta(hours=3)).isoformat(),
                           "last_success_at": (moment - timedelta(days=9)).isoformat(),
                           "last_status": "degraded"}
    assert _segment_for_light(moment, health) == "content"


def test_a_stalled_projections_cron_is_caught_by_the_hourly_tick():
    """Projections have their own cron; if it stops reaching the web service the hourly tick must run them."""
    moment = _tick(10)                       # content's hour, nothing else overdue
    assert _segment_for_light(moment, _fresh(moment, projections=9.0)) == "projections"
    assert _segment_for_light(moment, _fresh(moment, projections=1.5)) == "content"      # the cron is keeping up


def test_projections_that_never_ran_count_as_overdue():
    moment = _tick(10)
    health = _fresh(moment)
    del health["projections"]
    assert _segment_for_light(moment, health) == "projections"


def test_a_scheduled_segment_that_is_itself_overdue_still_goes_before_projections():
    moment = _tick(6)                        # core's hour
    assert _segment_for_light(moment, _fresh(moment, core=20.0, projections=9.0)) == "core"
