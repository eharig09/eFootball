from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import create_app
from sports_aggregator.cfb.models import Game, Team
from sports_aggregator.cfb.refresh_window import GAME_WINDOW_HOURS, games_in_progress, profile_for
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas
from sports_aggregator.social.content import ContentRepository
from sports_aggregator.scheduled_refresh import (
    REFRESH_PROFILES, RESULTS_REFRESH_STEPS, SCORES_DATASETS, SCORES_REFRESH_STEPS,
)

EASTERN = ZoneInfo("America/New_York")


def _game(game_id, start, *, completed=False, points=None):
    return Game.from_cfbd({
        "id": game_id, "season": 2026, "week": 1, "seasonType": "regular",
        "startDate": start, "startTimeTBD": False, "completed": completed,
        "neutralSite": False, "conferenceGame": True, "venue": "Stadium",
        "venueId": 1, "homeId": 1, "homeTeam": "Michigan",
        "homeConference": "Big Ten", "homePoints": points,
        "awayId": 2, "awayTeam": "Ohio State", "awayConference": "Big Ten",
        "awayPoints": points,
    })


class RefreshWindowTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle); os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams((
            Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs", "00274C", "FFCB05", (), ("Michigan",), None, None),
            Team(2, "Ohio State", "Buckeyes", "OSU", "Big Ten", None, "fbs", "BB0000", "666666", (), ("Ohio State",), None, None),
        ))
        self.kickoff = datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path): os.unlink(path)

    def _schedule(self, *games):
        self.repository.replace_games(2026, list(games))

    def _at(self, **offset):
        return self.kickoff + timedelta(**offset)

    def test_a_game_underway_is_in_progress(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        self.assertEqual(games_in_progress(self.repository, now=self._at(hours=2)), 1)

    def test_a_game_about_to_start_is_already_worth_a_pass(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        self.assertEqual(games_in_progress(self.repository, now=self._at(minutes=-30)), 1)

    def test_a_game_still_hours_away_is_not(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        self.assertEqual(games_in_progress(self.repository, now=self._at(hours=-4)), 0)

    def test_a_game_whose_result_has_arrived_leaves_the_window(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        self.assertEqual(games_in_progress(self.repository, now=self._at(hours=3)), 1)
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z", completed=True, points=31))
        self.assertEqual(games_in_progress(self.repository, now=self._at(hours=3)), 0)

    def test_a_result_that_never_arrives_cannot_hold_the_window_open(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        self.assertEqual(games_in_progress(self.repository, now=self._at(hours=GAME_WINDOW_HOURS + 1)), 0)

    def test_live_quarter_hour_is_a_tiny_score_pulse(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        decision = profile_for(self.repository, now=self._at(hours=2, minutes=15))
        self.assertEqual(decision["profile"], "scores")
        self.assertEqual(decision["reason"], "games_in_progress")

    def test_live_top_of_hour_adds_box_scores(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        decision = profile_for(self.repository, now=self._at(hours=2))
        self.assertEqual(decision["profile"], "results")
        self.assertEqual(decision["reason"], "hourly_live_results")

    @patch.dict(os.environ, {"CFB_REFRESH_HOURS": "6,12,18,23", "CFB_REFRESH_HEAVY_HOURS": "6,23", "CFB_REFRESH_NEWS_HOURS": "8,10"}, clear=False)
    def test_scheduled_profiles_fire_in_the_first_minutes_of_the_hour(self):
        self.assertEqual(profile_for(self.repository, now=datetime(2026, 7, 1, 6, 0, tzinfo=EASTERN))["profile"], "heavy")
        self.assertEqual(profile_for(self.repository, now=datetime(2026, 7, 1, 12, 0, tzinfo=EASTERN))["profile"], "light")
        self.assertEqual(profile_for(self.repository, now=datetime(2026, 7, 1, 8, 0, tzinfo=EASTERN))["profile"], "news")
        self.assertIsNone(profile_for(self.repository, now=datetime(2026, 7, 1, 6, 15, tzinfo=EASTERN))["profile"])

    @patch.dict(os.environ, {"CFB_REFRESH_HOURS": "2,6", "CFB_REFRESH_HEAVY_HOURS": "", "CFB_REFRESH_NEWS_HOURS": "8"}, clear=False)
    def test_a_cron_tick_that_starts_a_little_late_still_counts(self):
        """Render's ticks landed at 02:01:37 and 06:01:17 and were refused as 'between ticks', so
        the 2 AM analytics and 6 AM core runs were skipped depending on cron jitter."""
        for hour, minute, second in ((2, 1, 37), (6, 1, 17), (2, 0, 5), (6, 9, 59)):
            decision = profile_for(self.repository, now=datetime(2026, 7, 1, hour, minute, second, tzinfo=EASTERN))
            self.assertEqual(decision["profile"], "light", (hour, minute))
        decision = profile_for(self.repository, now=datetime(2026, 7, 1, 8, 1, 3, tzinfo=EASTERN))
        self.assertEqual(decision["profile"], "news")

    @patch.dict(os.environ, {"CFB_REFRESH_HOURS": "2,6", "CFB_REFRESH_HEAVY_HOURS": "", "CFB_REFRESH_NEWS_HOURS": "8"}, clear=False)
    def test_later_ticks_in_the_hour_do_not_start_a_second_scheduled_run(self):
        for minute in (10, 15, 31, 45, 59):
            decision = profile_for(self.repository, now=datetime(2026, 7, 1, 2, minute, tzinfo=EASTERN))
            self.assertIsNone(decision["profile"], minute)
            self.assertEqual(decision["reason"], "between_scheduled_ticks")

    def test_during_games_the_hourly_results_pass_also_tolerates_a_late_tick(self):
        self._schedule(_game(1, "2026-09-05T16:00:00.000Z"))
        late = profile_for(self.repository, now=self._at(hours=2, minutes=1))
        self.assertEqual((late["profile"], late["reason"]), ("results", "hourly_live_results"))
        quarter = profile_for(self.repository, now=self._at(hours=2, minutes=16))
        self.assertEqual(quarter["profile"], "scores")


class ScoresProfileTests(unittest.TestCase):
    def test_profiles_are_accepted(self):
        for profile in ("scores", "results", "news", "light", "heavy"):
            self.assertIn(profile, REFRESH_PROFILES)

    def test_quarter_hour_score_pass_excludes_box_scores(self):
        self.assertEqual(SCORES_REFRESH_STEPS, ["cfbd-sync", "cfbd-lines"])

    def test_hourly_results_add_box_scores(self):
        self.assertEqual(RESULTS_REFRESH_STEPS, ["cfbd-sync", "cfbd-box-scores", "cfbd-lines"])

    def test_live_profiles_read_only_games_from_cfbd_sync(self):
        self.assertEqual(SCORES_DATASETS, ["games"])

    def test_an_unknown_profile_is_refused(self):
        from sports_aggregator.scheduled_refresh import run_scheduled_refresh
        with self.assertRaises(ValueError):
            run_scheduled_refresh(2026, profile="everything")


class RefreshEndpointTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle); os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams((
            Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs", "00274C", "FFCB05", (), ("Michigan",), None, None),
            Team(2, "Ohio State", "Buckeyes", "OSU", "Big Ten", None, "fbs", "BB0000", "666666", (), ("Ohio State",), None, None),
        ))
        ContentRepository(self.path).initialize()
        self.app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                               "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
                               "CFB_DATABASE_PATH": self.path, "CFB_REFRESH_TOKEN": "test-token"})
        self.client = self.app.test_client(); self.started = []
        patcher = patch("app.subprocess.Popen", side_effect=self._record)
        patcher.start(); self.addCleanup(patcher.stop)

    def _record(self, command, **_kwargs):
        self.started.append(command); return unittest.mock.MagicMock()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path): os.unlink(path)

    def _post(self, profile):
        return self.client.post("/internal/cfb-refresh?profile=" + profile,
                                headers={"Authorization": "Bearer test-token"})

    def test_auto_during_a_slate_starts_a_live_profile(self):
        kickoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.repository.replace_games(2026, [_game(1, kickoff)])
        response = self._post("auto")
        self.assertEqual(response.status_code, 202)
        self.assertIn(response.get_json()["profile"], {"scores", "results"})

    def test_explicit_new_profiles_work(self):
        for profile in ("scores", "results", "news", "heavy"):
            response = self._post(profile)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.get_json()["profile"], profile)

    def test_an_unknown_profile_is_refused(self):
        self.assertEqual(self._post("everything").status_code, 400)

    def test_the_endpoint_is_closed_without_the_token(self):
        response = self.client.post("/internal/cfb-refresh?profile=auto")
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
