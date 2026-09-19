import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing

from app import create_app
from sports_aggregator.cfb import passing_plays
from sports_aggregator.cfb.middle_of_field import (
    MIN_GAME_PLAYS, game_middle, middle_verdict, team_season_middle,
)
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"


class MiddleFixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        passing_plays.initialize(self.repository)
        from sports_aggregator.cfb.play_detail import initialize as initialize_detail
        initialize_detail(self.repository)
        self.play = 0

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def rush(self, offense, defense, direction, epa, *, game_id=1, season=2026, success=1):
        self.play += 1
        pid = f"r{self.play}"
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO cfb_plays(play_id,game_id,season,week,offense,defense,"
                      "home_team,away_team,play_type,play_text,raw_json,imported_at) "
                      "VALUES (?,?,?,1,?,?,?,?,'Rush','run','{}',?)",
                      (pid, game_id, season, offense, defense, defense, offense, NOW))
            c.execute("INSERT INTO cfb_play_detail(play_id,parser_version,rush_direction,"
                      "screen,scramble,sack,play_action,parser_confidence,parsed_at) "
                      "VALUES (?,'play-detail-v3',?,0,0,0,0,'high',?)", (pid, direction, NOW))
            c.execute("INSERT INTO cfb_play_epa(play_id,model_version,epa,possession_changed,"
                      "scored_at) VALUES (?,'ep-v2',?,0,?)", (pid, epa, NOW))
            c.execute("INSERT INTO cfb_play_metrics(play_id,metric_version,rush_pass,success,"
                      "derived_at) VALUES (?,'pbp-v1','rush',?,?)", (pid, success, NOW))
            c.commit()

    def throw(self, offense, defense, direction, epa, *, game_id=1, season=2026, success=1):
        self.play += 1
        pid = f"p{self.play}"
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO cfbd_passing_plays(play_id,game_id,season,week,offense,"
                      "defense,pass_direction,outcome,imported_at) "
                      "VALUES (?,?,?,1,?,?,?,'completion',?)",
                      (pid, game_id, season, offense, defense, direction, NOW))
            c.execute("INSERT INTO cfb_play_epa(play_id,model_version,epa,possession_changed,"
                      "scored_at) VALUES (?,'ep-v2',?,0,?)", (pid, epa, NOW))
            c.execute("INSERT INTO cfb_play_metrics(play_id,metric_version,rush_pass,success,"
                      "derived_at) VALUES (?,'pbp-v1','pass',?,?)", (pid, success, NOW))
            c.commit()


class GameMiddleTests(MiddleFixture):
    """Run and pass together, offence only, because a game mirrors itself."""

    def seed(self):
        for _ in range(5):
            self.rush("Michigan", "Ohio State", "middle", 1.0)
            self.rush("Michigan", "Ohio State", "left", 0.0)
            self.throw("Michigan", "Ohio State", "middle", 0.5)
            self.throw("Ohio State", "Michigan", "middle", -1.0)

    def test_run_and_pass_are_reported_apart_and_together(self):
        self.seed()
        michigan = game_middle(self.repository, 1)["Michigan"]
        self.assertEqual(michigan["run"]["middle"]["plays"], 5)
        self.assertEqual(michigan["pass"]["middle"]["plays"], 5)
        self.assertEqual(michigan["combined"]["middle"]["plays"], 10)
        self.assertAlmostEqual(michigan["combined"]["middle"]["epa_per_play"], 0.75)

    def test_a_game_reports_each_offence_once(self):
        """A team's middle allowed is the opponent's middle attacked, same plays."""
        self.seed()
        teams = game_middle(self.repository, 1)
        self.assertEqual(set(teams), {"Michigan", "Ohio State"})
        for data in teams.values():
            self.assertNotIn("defense", data)

    def test_left_and_right_are_one_outside_bucket(self):
        self.rush("Michigan", "Ohio State", "left", 0.0)
        self.rush("Michigan", "Ohio State", "right", 0.0)
        outside = game_middle(self.repository, 1)["Michigan"]["run"]["outside"]
        self.assertEqual(outside["plays"], 2)

    def test_the_middle_share_counts_runs_and_passes(self):
        self.seed()
        michigan = game_middle(self.repository, 1)["Michigan"]
        self.assertAlmostEqual(michigan["middle_share"], 10 / 15)

    def test_success_rate_comes_back_with_the_epa(self):
        self.rush("Michigan", "Ohio State", "middle", 1.0, success=1)
        self.rush("Michigan", "Ohio State", "middle", -1.0, success=0)
        middle = game_middle(self.repository, 1)["Michigan"]["run"]["middle"]
        self.assertAlmostEqual(middle["success_rate"], 0.5)

    def test_a_game_with_no_direction_is_empty_not_an_error(self):
        self.assertEqual(game_middle(self.repository, 999), {})


class VerdictTests(MiddleFixture):
    def test_the_better_combined_middle_wins_it(self):
        for _ in range(4):
            self.rush("Michigan", "Ohio State", "middle", 1.0)
            self.rush("Ohio State", "Michigan", "middle", -1.0)
        verdict = middle_verdict(game_middle(self.repository, 1))
        self.assertEqual(verdict["winner"], "Michigan")
        self.assertAlmostEqual(verdict["margin"], 2.0)

    def test_the_run_and_the_pass_are_weighed_together(self):
        """Losing the ground middle and winning it through the air is one answer."""
        for _ in range(4):
            self.rush("Michigan", "Ohio State", "middle", -1.0)
            self.throw("Michigan", "Ohio State", "middle", 2.0)
            self.rush("Ohio State", "Michigan", "middle", 0.1)
            self.throw("Ohio State", "Michigan", "middle", 0.1)
        teams = game_middle(self.repository, 1)
        self.assertLess(teams["Michigan"]["run"]["middle"]["epa_per_play"],
                        teams["Ohio State"]["run"]["middle"]["epa_per_play"])
        self.assertEqual(middle_verdict(teams)["winner"], "Michigan")

    def test_too_few_plays_gives_no_verdict(self):
        self.rush("Michigan", "Ohio State", "middle", 1.0)
        self.rush("Ohio State", "Michigan", "middle", -1.0)
        self.assertIsNone(middle_verdict(game_middle(self.repository, 1)))

    def test_a_tie_gives_no_verdict(self):
        for _ in range(MIN_GAME_PLAYS):
            self.rush("Michigan", "Ohio State", "middle", 0.5)
            self.rush("Ohio State", "Michigan", "middle", 0.5)
        self.assertIsNone(middle_verdict(game_middle(self.repository, 1)))


class SeasonMiddleTests(MiddleFixture):
    """Across a season the defence faced other people, so both sides are real."""

    def test_offence_and_defence_differ_once_there_is_more_than_one_opponent(self):
        for _ in range(3):
            self.rush("Michigan", "Ohio State", "middle", 1.0, game_id=1)
            self.rush("Ohio State", "Michigan", "middle", -1.0, game_id=1)
            self.rush("Michigan", "Purdue", "middle", 1.0, game_id=2)
            self.rush("Purdue", "Michigan", "middle", 0.0, game_id=2)
        michigan = team_season_middle(self.repository, "Michigan", 2026)
        self.assertEqual(michigan["offense"]["run"]["middle"]["plays"], 6)
        self.assertEqual(michigan["defense"]["run"]["middle"]["plays"], 6)
        self.assertAlmostEqual(michigan["offense"]["run"]["middle"]["epa_per_play"], 1.0)
        self.assertAlmostEqual(michigan["defense"]["run"]["middle"]["epa_per_play"], -0.5)

    def test_a_season_with_nothing_stored_reports_no_plays(self):
        self.assertEqual(team_season_middle(self.repository, "Nobody", 2026)["offense"]["plays"], 0)


class StyleIntegrationTests(unittest.TestCase):
    def test_matchup_page_defines_the_analysis_type_scale(self):
        css = (Path(__file__).parents[1] / "static" / "cfb_analysis.css").read_text(
            encoding="utf-8")
        block = re.search(r"\.matchup-page\s*\{([^}]+)\}", css)
        self.assertIsNotNone(block)
        for token in ("--fs-h3", "--fs-data", "--fs-data-lg", "--fs-label",
                      "--fs-micro", "--num-font"):
            self.assertIn(token, block.group(1))
        self.assertIn(".matchup-page .pass-thin{font-style:normal}", css)


class RenderTests(MiddleFixture):
    def setUp(self):
        super().setUp()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DATABASE_PATH": self.path})

    def render(self, name, *args):
        with self.app.test_request_context("/"):
            return str(self.app.jinja_env.globals[name](*args))

    def test_the_postgame_block_states_who_won_the_middle(self):
        for _ in range(4):
            self.rush("Michigan", "Ohio State", "middle", 1.0)
            self.rush("Ohio State", "Michigan", "middle", -1.0)
        html = self.render("passing_game_splits",
                           {"game_id": 1, "season": 2026,
                            "away_team": "Michigan", "home_team": "Ohio State"})
        self.assertIn("won the middle", html)
        self.assertIn("Michigan", html)
        self.assertIn("All middle", html)
        self.assertIn("Run — middle", html)

    def test_the_postgame_block_prints_no_second_title(self):
        """The report wraps it in a numbered chapter that already names it."""
        for _ in range(4):
            self.rush("Michigan", "Ohio State", "middle", 1.0)
            self.rush("Ohio State", "Michigan", "middle", -1.0)
        html = self.render("passing_game_splits",
                           {"game_id": 1, "season": 2026,
                            "away_team": "Michigan", "home_team": "Ohio State"})
        self.assertNotIn("<h2>", html)

    def test_the_matchup_block_pairs_an_offence_against_the_defence_it_faces(self):
        for _ in range(30):
            self.rush("Michigan", "Purdue", "middle", 1.0, game_id=2)
            self.rush("Ohio State", "Purdue", "middle", -1.0, game_id=3)
        html = self.render("passing_matchup_splits",
                           {"season": 2026, "away_team": "Michigan", "home_team": "Ohio State"})
        self.assertIn("When Michigan has the ball", html)
        self.assertIn("When Ohio State has the ball", html)
        self.assertIn("Ohio State allowed", html)
        self.assertIn("<h2>", html)

    def test_the_matchup_block_is_a_compact_middle_first_comparison(self):
        for _ in range(30):
            self.rush("Michigan", "Purdue", "middle", 1.0, game_id=2)
            self.rush("Michigan", "Purdue", "left", 0.2, game_id=2)
            self.rush("Ohio State", "Purdue", "middle", -1.0, game_id=3)
        html = self.render("passing_matchup_splits",
                           {"season": 2026, "away_team": "Michigan",
                            "home_team": "Ohio State"})
        self.assertIn('class="mof-matchup-grid"', html)
        self.assertIn('class="mof-grid mof-matchup-matrix"', html)
        self.assertIn("Overall", html)
        self.assertIn("Outside +0.20", html)
        self.assertIn("How to read this", html)
        self.assertNotIn("Run â€” outside", html)

    def test_a_game_without_direction_says_so_rather_than_vanishing(self):
        html = self.render("passing_game_splits",
                           {"game_id": 1, "season": 2026,
                            "away_team": "Michigan", "home_team": "Ohio State"})
        self.assertIn("No classified run or pass direction", html)


if __name__ == "__main__":
    unittest.main()
