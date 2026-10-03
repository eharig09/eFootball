from datetime import datetime, timezone
import json
import os
import tempfile
import unittest

from app import create_app
from sports_aggregator.cfb.history import (
    matchup_history, matchup_player_history, team_game_history,
    team_historical_stats, time_slot)
from sports_aggregator.cfb.models import Game, Player, Team
from sports_aggregator.cfb.repository import CFBRepository


def game(game_id, season, start, home_id, home, home_conf, home_points,
         away_id, away, away_conf, away_points):
    return Game(game_id, season, 1, "regular", datetime.fromisoformat(start), False,
                True, False, home_conf == away_conf, None, None,
                home_id, home, home_conf, home_points, None,
                away_id, away, away_conf, away_points, None, None, None)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = CFBRepository(os.path.join(self.temp.name, "cfb.sqlite3"))
        self.repository.initialize()
        self.repository.replace_teams((
            Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs",
                 "00274C", "FFCB05", (), ("Michigan",), None, None),
            Team(2, "Wisconsin", "Badgers", "WIS", "Big Ten", None, "fbs",
                 "C5050C", "FFFFFF", (), ("Wisconsin",), None, None),
            Team(3, "Iowa", "Hawkeyes", "IOWA", "Big Ten", None, "fbs",
                 "000000", "FFCD00", (), ("Iowa",), None, None),
        ))
        self.repository.replace_games(2022, (game(
            90, 2022, "2022-09-03T16:00:00+00:00", 2, "Wisconsin", "Big Ten", 10,
            3, "Iowa", "Big Ten", 17),))
        self.repository.replace_games(2023, (game(
            91, 2023, "2023-10-07T23:30:00+00:00", 1, "Michigan", "Big Ten", 24,
            2, "Wisconsin", "Big Ten", 17),))
        self.repository.replace_games(2024, (game(
            92, 2024, "2024-09-12T23:30:00+00:00", 2, "Wisconsin", "Big Ten", 21,
            1, "Michigan", "Big Ten", 20),))
        for season, team_id, school in ((2022, 3, "Iowa"), (2023, 1, "Michigan"),
                                        (2024, 1, "Michigan"), (2026, 1, "Michigan")):
            self.repository.replace_coach_seasons(season, ({
                "id": 7, "firstName": "Coach", "lastName": "Example",
                "seasons": [{"year": season, "teamId": team_id, "school": school,
                             "conference": "Big Ten", "games": 1, "wins": 1,
                             "losses": 0, "ties": 0}],
            },))

    def tearDown(self):
        self.temp.cleanup()

    def test_matchup_series_slots_ppg_and_coach_follow_prior_jobs(self):
        target = game(100, 2026, "2026-09-05T23:30:00+00:00",
                      1, "Michigan", "Big Ten", None,
                      2, "Wisconsin", "Big Ten", None)
        target = {name: getattr(target, name) for name in target.__dataclass_fields__}
        target["start_date"] = target["start_date"].isoformat()
        packet = matchup_history(self.repository, target)
        self.assertEqual(packet["meetings"], 2)
        self.assertEqual(packet["away_record"]["record"], "1-1")
        self.assertEqual(packet["home_record"]["record"], "1-1")
        self.assertEqual(packet["slot"], "Saturday — primetime")
        self.assertEqual(packet["home_context"]["site"]["record"], "1-0")
        self.assertEqual(packet["home_context"]["coach"]["record"], "2-1")
        self.assertAlmostEqual(packet["away_record"]["ppg_for"], 19.0)
        self.assertEqual(len(packet["away_recent"]), 3)
        self.assertEqual(len(packet["home_recent"]), 2)
        self.assertEqual(packet["away_recent"][0]["game_id"], 92)
        self.assertEqual(packet["away_recent"][-1]["opponent"], "Iowa")
        self.assertTrue(all(row["game_url"].endswith("/box-score/")
                            for row in packet["away_recent"]))

    def test_series_ats_uses_the_closing_line_from_each_teams_side(self):
        from contextlib import closing
        from sports_aggregator.cfb import lines
        lines.initialize(self.repository)
        with closing(self.repository._connect()) as connection:
            for game_id, season, provider, spread in (
                    (91, 2023, "consensus", -3.0),     # Michigan home, -3, won 24-17: covers by 4
                    (91, 2023, "Bovada", -9.0),        # ignored: the consensus line wins
                    (92, 2024, "Bovada", 1.0),         # Wisconsin home +1 (no consensus), lost... 21-20 win, covers
                    (92, 2024, "ESPN Bet", 2.0)):      # mean 1.5 rounds to the half point: Wisconsin +1.5
                connection.execute(
                    "INSERT INTO game_lines(game_id,season,provider,spread,fetched_at) VALUES(?,?,?,?,?)",
                    (game_id, season, provider, spread, "2026-01-01T00:00:00+00:00"))
            connection.commit()
        target = game(100, 2026, "2026-09-05T23:30:00+00:00", 1, "Michigan", "Big Ten", None,
                      2, "Wisconsin", "Big Ten", None)
        target = {name: getattr(target, name) for name in target.__dataclass_fields__}
        target["start_date"] = target["start_date"].isoformat()
        packet = matchup_history(self.repository, target)
        # game 91: Michigan -3, won by 7 -> Michigan covers, Wisconsin does not.
        # game 92: Wisconsin +1.5 at home, won by 1 -> Wisconsin covers, Michigan (-1.5 away) does not.
        self.assertEqual(packet["home_record"]["ats"]["record"], "1-1")      # Michigan
        self.assertEqual(packet["away_record"]["ats"]["record"], "1-1")      # Wisconsin
        self.assertEqual(packet["home_record"]["ats"]["games"], 2)
        by_game = {row["game_id"]: row for row in packet["recent"]}           # Wisconsin's side
        self.assertEqual((by_game[91]["ats"], by_game[91]["line"]), ("L", 3.0))
        self.assertEqual((by_game[92]["ats"], by_game[92]["line"]), ("W", 1.5))
        self.assertEqual(packet["home_context"]["site"]["ats"]["record"], "1-0")   # Michigan at home: game 91 cover

    def test_series_ats_is_blank_when_no_lines_are_stored(self):
        target = game(100, 2026, "2026-09-05T23:30:00+00:00", 1, "Michigan", "Big Ten", None,
                      2, "Wisconsin", "Big Ten", None)
        target = {name: getattr(target, name) for name in target.__dataclass_fields__}
        target["start_date"] = target["start_date"].isoformat()
        packet = matchup_history(self.repository, target)
        self.assertIsNone(packet["home_record"]["ats"]["record"])
        self.assertEqual(packet["home_record"]["ats"]["games"], 0)

    def test_team_game_log_and_position_production(self):
        self.repository.replace_player_stats(2024, (
            {"playerId": "rb", "player": "Runner", "team": "Michigan",
             "conference": "Big Ten", "position": "RB", "category": "rushing",
             "statType": "YDS", "stat": 1200},
            {"playerId": "rb", "player": "Runner", "team": "Michigan",
             "conference": "Big Ten", "position": "RB", "category": "receiving",
             "statType": "YDS", "stat": 350},
        ), "Big Ten")
        log = team_game_history(self.repository, 1)
        self.assertEqual(log["summary"]["record"], "1-1")
        self.assertEqual(log["unique_opponents"], 1)
        self.assertTrue(all(row["game_url"].endswith("/box-score/")
                            for row in log["games"]))
        stats = team_historical_stats(self.repository, 1)
        rb = next(row for row in stats["positions"] if row["position_group"] == "RB")
        self.assertEqual(rb["rush_yards"], 1200)
        self.assertEqual(rb["receiving_yards"], 350)

    def test_time_slot_boundaries(self):
        self.assertEqual(time_slot("2026-09-05T16:00:00+00:00"), "Saturday — day")
        self.assertEqual(time_slot("2026-09-05T23:30:00+00:00"), "Saturday — primetime")
        self.assertEqual(time_slot("2026-09-06T02:30:00+00:00"), "Saturday — late night")

    def test_box_scores_are_stored_and_player_history_is_reused(self):
        self.repository.replace_players(2026, (
            Player("rb", 2026, "Test", "Runner", "Michigan", "RB", 2, 71, 205, 3),
        ))
        self.repository.store_game_team_box_scores(({
            "id": 92, "teams": [{"teamId": 2, "team": "Wisconsin",
                "conference": "Big Ten", "homeAway": "home", "points": 21,
                "stats": [{"category": "totalYards", "stat": "388"}]}],
        },))
        self.repository.store_game_player_box_scores(({
            "id": 92, "teams": [{"team": "Michigan", "conference": "Big Ten",
                "homeAway": "away", "points": 20, "categories": [{
                    "name": "rushing", "types": [{"name": "YDS", "athletes": [
                        {"id": "rb", "name": "Test Runner", "stat": "104"}]}]}]}],
        },))
        packet = self.repository.game_box_score(92)
        self.assertTrue(packet["available"])
        self.assertEqual(packet["team_stats"][0]["numeric_value"], 388)
        target = game(100, 2026, "2026-09-05T23:30:00+00:00",
                      1, "Michigan", "Big Ten", None,
                      2, "Wisconsin", "Big Ten", None)
        target = {name: getattr(target, name) for name in target.__dataclass_fields__}
        target["start_date"] = target["start_date"].isoformat()
        history = matchup_player_history(self.repository, target)
        self.assertEqual(history[0]["player"], "Test Runner")
        self.assertEqual(history[0]["rushing"], "104 yd")
        # This packet is served by /api/v1/cfb/games/<id>/preview.
        # _game_stat_lines builds a lookup index keyed by (category, stat_type)
        # tuples and used to spread it into every emitted row. Templates never
        # noticed; a tuple key cannot be represented in JSON, so the preview
        # endpoint raised instead of responding.
        for row in history:
            self.assertNotIn("stats", row, "internal lookup index leaked into output")
        json.dumps(history)


    def test_partial_box_rows_require_completion_marker(self):
        self.repository.store_game_team_box_scores(({
            "id": 92, "teams": [{"team": "Wisconsin", "stats": [
                {"category": "totalYards", "stat": "388"}]}]},))
        self.assertFalse(self.repository.history_dataset_cached(2024, "team_box_scores"))
        self.repository.mark_history_dataset(2024, "team_box_scores", 1)
        self.assertTrue(self.repository.history_dataset_cached(2024, "team_box_scores"))

    def test_history_pages_render_populated_packets(self):
        target = game(100, 2026, "2026-09-05T23:30:00+00:00",
                      1, "Michigan", "Big Ten", None,
                      2, "Wisconsin", "Big Ten", None)
        self.repository.replace_games(2026, (target,))
        app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                          "CFB_REPOSITORY": self.repository,
                          "CFB_DEFAULT_SEASON": 2026})
        client = app.test_client()
        preview = client.get("/college-football/games/100/")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Coach Example", preview.data)
        self.assertIn(b"Recent meetings", preview.data)
        self.assertIn(b"Recent form", preview.data)
        self.assertIn(b"<h3>Wisconsin</h3>", preview.data)
        self.assertIn(b"<h3>Michigan</h3>", preview.data)
        self.assertIn(b"Box score", preview.data)
        self.assertEqual(client.get("/college-football/games/100/box-score/").status_code, 200)
        self.assertEqual(client.get("/api/v1/cfb/games/100/box-score").status_code, 200)
        self.assertIn(b"Season index", client.get(
            "/college-football/teams/1/history/").data)
        self.assertIn(b"Position production history", client.get(
            "/college-football/teams/1/history/stats/").data)


if __name__ == "__main__":
    unittest.main()
