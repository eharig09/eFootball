import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from app import create_app
from sports_aggregator.cfb import passing_plays
from sports_aggregator.cfb.models import Game, Team
from sports_aggregator.cfb.passing_plays import (
    MIN_GAME_ATTEMPTS, coverage, game_splits, store_attempts, sync_season,
    sync_week, team_season_splits, _receiver_position_rows,
)
from sports_aggregator.cfb.rushing_plays import _rusher_position_rows
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas


def attempt(play_id, offense, defense, direction, *, depth="short", epa=None,
            air=8.0, yac=3.0, outcome="completion", week=9, game_id=1):
    return {"playId": play_id, "gameId": game_id, "season": 2025, "week": week,
            "offense": offense, "defense": defense, "passer": "A Passer",
            "passerId": "p1", "target": "A Target", "targetId": "t1",
            "down": 1, "distance": 10, "startYardsToGoal": 60,
            "passDirection": direction, "passDepth": depth,
            "passLocation": None if direction is None else f"{depth} {direction}",
            "airYards": air, "yardsAfterCatch": yac, "totalYards": 11,
            "outcome": outcome, "parseStatus": "complete", "_epa": epa}


def game_row(game_id):
    return Game.from_cfbd({
        "id": game_id, "season": 2025, "week": 9, "seasonType": "regular",
        "startDate": "2025-10-25T19:30:00.000Z", "startTimeTBD": False,
        "completed": True, "neutralSite": False, "conferenceGame": True,
        "venue": "Stadium", "venueId": 1, "homeId": 2, "homeTeam": "Ohio State",
        "homeConference": "Big Ten", "homePoints": 20, "awayId": 1,
        "awayTeam": "Michigan", "awayConference": "Big Ten", "awayPoints": 17})


def score_plays(path, pairs):
    """Give plays an ep-v2 EPA, through the real schema rather than a stand-in."""
    now = "2026-01-01T00:00:00+00:00"
    with closing(sqlite3.connect(path)) as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO cfb_play_epa"
            "(play_id,model_version,epa,possession_changed,scored_at) VALUES (?,?,?,0,?)",
            [(play_id, "ep-v2", epa, now) for play_id, epa in pairs])
        connection.commit()


def mark_garbage_time(path, play_id):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO cfb_play_metrics"
            "(play_id,metric_version,garbage_time,derived_at) VALUES (?,?,1,?)",
            (play_id, "pbp-v1", "2026-01-01T00:00:00+00:00"))
        connection.commit()


class FakeClient:
    def __init__(self, by_week): self.by_week = by_week; self.calls = []

    def get(self, path, params, **kwargs):
        self.calls.append((path, params.get("week")))
        week = params.get("week")
        if week in self.by_week and self.by_week[week] == "boom":
            raise RuntimeError("provider is unwell")
        return self.by_week.get(week, [])


class PassingStoreTests(unittest.TestCase):
    def test_cfb_opponent_production_aggregates_by_position(self):
        passing = _receiver_position_rows({"WR": {
            "players": {"a", "b"}, "targets": 7, "receptions": 5, "yards": 81,
            "touchdowns": 1, "epa": 2.1, "epa_plays": 7,
        }})
        rushing = _rusher_position_rows({"RB": {
            "players": {"c", "d"}, "attempts": 10, "yards": 52,
            "touchdowns": 1, "epa": 1.5, "epa_plays": 10,
        }})
        self.assertEqual((passing[0]["players"], passing[0]["receptions"],
                          passing[0]["targets"], passing[0]["yards"]), (2, 5, 7, 81))
        self.assertAlmostEqual(passing[0]["epa_per_attempt"], .3)
        self.assertEqual((rushing[0]["players"], rushing[0]["attempts"],
                          rushing[0]["yards"]), (2, 10, 52))
        self.assertAlmostEqual(rushing[0]["epa_per_attempt"], .15)

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def test_attempts_are_stored_with_their_direction(self):
        report = store_attempts(self.repository, [
            attempt("1", "A", "B", "middle"), attempt("2", "A", "B", "left")])
        self.assertEqual(report["stored"], 2)
        self.assertEqual(report["classified"], 2)

    def test_an_unclassified_attempt_is_kept_but_not_counted_as_classified(self):
        """Coverage is partial, and a caller has to be able to see how partial."""
        report = store_attempts(self.repository, [
            attempt("1", "A", "B", "middle"), attempt("2", "A", "B", None)])
        self.assertEqual(report["stored"], 2)
        self.assertEqual(report["classified"], 1)
        self.assertEqual(report["coverage"], 0.5)

    def test_resyncing_a_week_corrects_rows_rather_than_duplicating_them(self):
        store_attempts(self.repository, [attempt("1", "A", "B", "left")])
        store_attempts(self.repository, [attempt("1", "A", "B", "middle")])
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT play_id, pass_direction FROM cfbd_passing_plays").fetchall()
        self.assertEqual(rows, [("1", "middle")])

    def test_a_row_without_the_teams_is_skipped_rather_than_stored_half_formed(self):
        broken = attempt("1", "A", "B", "middle"); broken["offense"] = None
        self.assertEqual(store_attempts(self.repository, [broken])["stored"], 0)

    def test_one_bad_week_does_not_lose_the_others(self):
        client = FakeClient({1: [attempt("1", "A", "B", "middle")], 2: "boom",
                             3: [attempt("3", "A", "B", "left")]})
        report = sync_season(self.repository, client, season=2025, weeks=(1, 2, 3))
        self.assertEqual(report["stored"], 2)
        self.assertEqual([f["week"] for f in report["failures"]], [2])

    def test_sync_week_reports_what_it_stored(self):
        client = FakeClient({9: [attempt("1", "A", "B", "middle")]})
        report = sync_week(self.repository, client, season=2025, week=9)
        self.assertEqual((report["season"], report["week"], report["stored"]), (2025, 9, 1))

    def test_coverage_describes_the_season(self):
        store_attempts(self.repository, [
            attempt("1", "A", "B", "middle"), attempt("2", "A", "B", None)])
        report = coverage(self.repository, 2025)
        self.assertEqual((report["attempts"], report["classified"]), (2, 1))
        self.assertEqual(report["games"], 1)


class PassingSplitTests(unittest.TestCase):
    """The splits are the point: middle against outside, thrown and allowed."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        passing_plays.initialize(self.repository)
        self.attempts = []
        # Middle is worth more than outside, which is the shape of the real data.
        for index in range(6):
            self.attempts.append(attempt(f"m{index}", "Michigan", "Ohio State", "middle"))
        for index in range(10):
            self.attempts.append(attempt(f"o{index}", "Michigan", "Ohio State", "left"))
        store_attempts(self.repository, self.attempts)
        score_plays(self.path, [(f"m{i}", 1.0) for i in range(6)]
                    + [(f"o{i}", 0.1) for i in range(10)])

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def test_the_middle_and_the_outside_are_reported_separately(self):
        splits = team_season_splits(self.repository, "Michigan", 2025)["offense"]
        self.assertEqual(splits["middle"]["attempts"], 6)
        self.assertEqual(splits["outside"]["attempts"], 10)
        self.assertAlmostEqual(splits["middle"]["epa_per_attempt"], 1.0)
        self.assertAlmostEqual(splits["outside"]["epa_per_attempt"], 0.1)

    def test_left_and_right_are_one_bucket(self):
        store_attempts(self.repository, [attempt("r0", "Michigan", "Ohio State", "right")])
        score_plays(self.path, [("r0", 0.1)])
        splits = team_season_splits(self.repository, "Michigan", 2025)["offense"]
        self.assertEqual(splits["outside"]["attempts"], 11)

    def test_what_one_team_throws_is_what_the_other_allows(self):
        offense = team_season_splits(self.repository, "Michigan", 2025)["offense"]
        defense = team_season_splits(self.repository, "Ohio State", 2025)["defense"]
        self.assertEqual(offense["middle"]["attempts"], defense["middle"]["attempts"])
        self.assertAlmostEqual(offense["middle"]["epa_per_attempt"],
                               defense["middle"]["epa_per_attempt"])

    def test_the_middle_share_is_reported(self):
        splits = team_season_splits(self.repository, "Michigan", 2025)["offense"]
        self.assertAlmostEqual(splits["middle_share"], 6 / 16)

    def test_garbage_time_is_excluded(self):
        mark_garbage_time(self.path, "m0")
        splits = team_season_splits(self.repository, "Michigan", 2025)["offense"]
        self.assertEqual(splits["middle"]["attempts"], 5)

    def test_an_unscored_attempt_contributes_nothing(self):
        """No EPA row means no EPA, not a zero dragging the average down."""
        store_attempts(self.repository, [attempt("x0", "Michigan", "Ohio State", "middle")])
        splits = team_season_splits(self.repository, "Michigan", 2025)["offense"]
        self.assertEqual(splits["middle"]["attempts"], 6)

    def test_a_game_reports_both_teams_from_both_sides(self):
        splits = game_splits(self.repository, 1)
        self.assertEqual(set(splits), {"Michigan", "Ohio State"})
        self.assertEqual(splits["Michigan"]["offense"]["middle"]["attempts"], 6)
        self.assertEqual(splits["Ohio State"]["defense"]["middle"]["attempts"], 6)

    def test_a_game_with_nothing_stored_is_empty_not_an_error(self):
        self.assertEqual(game_splits(self.repository, 99999), {})


class PassingRenderTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams([Team.from_cfbd({
            "id": i, "school": s, "mascot": s, "abbreviation": s[:3].upper(),
            "alternateNames": [], "conference": "Big Ten", "classification": "fbs",
            "color": "#0033A0", "logos": []}) for i, s in ((1, "Michigan"), (2, "Ohio State"))])
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DATABASE_PATH": self.path})

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def render(self, name, game):
        with self.app.test_request_context("/"):
            return str(self.app.jinja_env.globals[name](game))

    def test_a_game_without_direction_says_so_rather_than_vanishing(self):
        html = self.render("passing_game_splits", {"game_id": 1, "season": 2025})
        self.assertIn("No classified run or pass direction", html)

    def test_a_matchup_without_direction_says_so_too(self):
        html = self.render("passing_matchup_splits", {
            "season": 2025, "away_team": "Michigan", "home_team": "Ohio State"})
        self.assertIn("Middle of the field", html)
        self.assertIn("No classified run or pass direction", html)

    def test_a_matchup_missing_its_teams_renders_nothing_at_all(self):
        self.assertEqual(self.render("passing_matchup_splits", {"season": 2025}), "")

    def test_the_block_discloses_that_it_is_not_a_recommendation(self):
        """The middle EPA gap describes where value was available, not advice.

        Quarterbacks throw over the middle when coverage allows it, so the gap is
        a selection effect as much as a causal one. Losing that sentence would
        turn a description into a play-call instruction.
        """
        passing_plays.initialize(self.repository)
        store_attempts(self.repository, [
            attempt(f"m{i}", "Michigan", "Ohio State", "middle") for i in range(5)])
        score_plays(self.path, [(f"m{i}", 1.0) for i in range(5)])
        html = self.render("passing_matchup_splits", {
            "season": 2025, "away_team": "Michigan", "home_team": "Ohio State"})
        self.assertIn("recommendation", html)
        self.assertIn("Middle", html)


class PasserProfileTests(unittest.TestCase):
    """A quarterback's own page: depth, direction, and what the throws returned."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        passing_plays.initialize(self.repository)
        # Air yards on four of five, so availability differs from attempts.
        rows = [attempt("a0", "Michigan", "Ohio State", "middle", air=-2.0),
                attempt("a1", "Michigan", "Ohio State", "middle", air=5.0),
                attempt("a2", "Michigan", "Ohio State", "left", air=14.0),
                attempt("a3", "Michigan", "Ohio State", "right", air=27.0),
                attempt("a4", "Michigan", "Ohio State", "left", air=None)]
        store_attempts(self.repository, rows)
        score_plays(self.path, [(f"a{i}", 0.5) for i in range(5)])

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def profile(self):
        return passing_plays.passer_profile(self.repository, "p1", 2025)

    def test_the_depth_bands_split_where_a_chart_would(self):
        depth = self.profile()["depth"]
        self.assertEqual(depth, {"behind_line": 1, "short": 1,
                                 "intermediate": 1, "deep": 1})

    def test_a_screen_is_not_counted_as_a_short_pass(self):
        """Behind the line is its own band; averaging it into short hides both."""
        self.assertEqual(self.profile()["depth"]["behind_line"], 1)

    def test_availability_is_reported_separately_from_attempts(self):
        profile = self.profile()
        self.assertEqual(profile["attempts"], 5)
        self.assertEqual(profile["air_yards_available"], 4)

    def test_adot_averages_only_the_measured_attempts(self):
        self.assertAlmostEqual(self.profile()["adot"], (-2.0 + 5.0 + 14.0 + 27.0) / 4)

    def test_the_direction_split_rides_along(self):
        direction = self.profile()["direction"]
        self.assertEqual(direction["middle"]["attempts"], 2)
        self.assertEqual(direction["outside"]["attempts"], 3)

    def test_a_player_who_never_threw_has_no_profile(self):
        self.assertEqual(
            passing_plays.passer_profile(self.repository, "nobody", 2025)["attempts"], 0)

    def test_team_passers_are_ranked_by_attempts(self):
        passers = passing_plays.season_passers(self.repository, "Michigan", 2025, minimum=1)
        self.assertEqual([p["attempts"] for p in passers], [5])


class QbAirYardsSourceTests(unittest.TestCase):
    """The measured source replaces a text parser that reached ~1% of attempts."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        self.repository.replace_games(2025, [game_row(1), game_row(7)])
        passing_plays.initialize(self.repository)
        store_attempts(self.repository, [
            attempt("q0", "Michigan", "Ohio State", "middle", air=12.0),
            attempt("q1", "Michigan", "Ohio State", "left", air=4.0,
                    outcome="incompletion")])
        score_plays(self.path, [("q0", 1.0), ("q1", -0.5)])

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def test_passing_for_a_game_the_schedule_has_not_stored_is_skipped(self):
        """One orphan must not fail the insert and take every other row with it."""
        from sports_aggregator.cfb.qb_air_yards import build_from_cfbd
        store_attempts(self.repository, [
            attempt("z0", "Michigan", "Ohio State", "middle", air=9.0, game_id=99999)])
        score_plays(self.path, [("z0", 0.4)])
        report = build_from_cfbd(self.repository, from_season=2025, to_season=2025)
        self.assertEqual(report["rows"], 1)
        self.assertEqual(report["skipped_unknown_games"], 1)

    def test_building_one_season_leaves_the_others_alone(self):
        """An unscoped delete turned 3,007 rows into 21 when 2026 was built."""
        from sports_aggregator.cfb.qb_air_yards import build_from_cfbd
        build_from_cfbd(self.repository, from_season=2025, to_season=2025)
        before = self.stored_rows()
        self.assertTrue(before)
        build_from_cfbd(self.repository, from_season=2026, to_season=2026)
        self.assertEqual(self.stored_rows(), before, "the 2025 rows were discarded")

    def stored_rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM cfb_qb_air_yards_game WHERE season=2025").fetchone()[0]

    def test_it_builds_a_row_per_passer_per_game(self):
        from sports_aggregator.cfb.qb_air_yards import build_from_cfbd
        report = build_from_cfbd(self.repository, from_season=2025, to_season=2025)
        self.assertEqual((report["rows"], report["games"]), (1, 1))

    def test_air_yards_include_incompletions_so_the_average_is_adot(self):
        from sports_aggregator.cfb.qb_air_yards import build_from_cfbd, game_summary
        build_from_cfbd(self.repository, from_season=2025, to_season=2025)
        row = game_summary(self.repository, 1)[0]
        self.assertEqual(row["attributed_pass_plays"], 2)
        self.assertEqual(row["measured_completions"], 1)
        self.assertAlmostEqual(row["measured_adot"], 8.0)

    def test_coverage_is_recorded_rather_than_assumed(self):
        from sports_aggregator.cfb.qb_air_yards import build_from_cfbd, game_summary
        build_from_cfbd(self.repository, from_season=2025, to_season=2025)
        self.assertAlmostEqual(game_summary(self.repository, 1)[0]["numeric_depth_coverage"], 1.0)

    def test_the_source_with_measurements_wins_not_the_newer_one(self):
        """A week CFBD has not published carries attempts but no air yards."""
        from sports_aggregator.cfb.qb_air_yards import (
            CFBD_PARSER_VERSION, METRIC_VERSION, MODEL_VERSION, build_from_cfbd, game_summary)
        from sports_aggregator.cfb.play_detail import PARSER_VERSION
        store_attempts(self.repository, [
            attempt("u0", "Michigan", "Ohio State", None, air=None, game_id=7)])
        score_plays(self.path, [("u0", 0.2)])
        build_from_cfbd(self.repository, from_season=2025, to_season=2025)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO cfb_qb_air_yards_game VALUES (%s)" % ",".join("?" * 23),
                (7, 2025, "Michigan", "Ohio State", "p1", "A Passer", PARSER_VERSION,
                 MODEL_VERSION, METRIC_VERSION, 1, 1, 9.0, 9.0, 3.0, 3.0, 0.2, 0.2,
                 0, 1, 0, 0, 1.0, "2026-01-01T00:00:00+00:00"))
            connection.commit()
        rows = game_summary(self.repository, 7)
        self.assertEqual(rows[0]["parser_version"], PARSER_VERSION,
                         "the parsed row measured something and the CFBD row did not")
        self.assertNotEqual(rows[0]["parser_version"], CFBD_PARSER_VERSION)


class ScoredPlayGuardTests(unittest.TestCase):
    """Nothing built yet is a no-op; the wrong version is an error."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        from sports_aggregator.cfb.expected_points_v2 import initialize
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        initialize(self.repository)

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def score(self, version):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO cfb_play_epa(play_id,model_version,epa,possession_changed,"
                "scored_at) VALUES (?,?,0.1,0,?)", (version, version, "now"))
            connection.commit()

    def test_an_empty_table_is_nothing_to_do_not_a_failure(self):
        """A scheduled step ran before the pipeline reached it, which is normal.

        Reporting that as a failure put team-advanced in the degraded list of a
        refresh that was working.
        """
        from sports_aggregator.cfb.pbp_cli import _require_scored_plays
        self.assertFalse(_require_scored_plays(self.repository, "ep-v2"))

    def test_a_version_nobody_scored_while_another_is_scored_is_an_error(self):
        """That is the wrong version, which is the trap this guard exists for."""
        from sports_aggregator.cfb.pbp_cli import _require_scored_plays
        self.score("ep-v1")
        with self.assertRaises(SystemExit) as caught:
            _require_scored_plays(self.repository, "ep-v2")
        self.assertIn("ep-v1", str(caught.exception))

    def test_a_scored_version_builds(self):
        from sports_aggregator.cfb.pbp_cli import _require_scored_plays
        self.score("ep-v2")
        self.assertTrue(_require_scored_plays(self.repository, "ep-v2"))


class EndpointToleranceTests(unittest.TestCase):
    """One dead account must not report a working refresh as degraded.

    A handle that has been renamed or deleted stays unresolved and fails on
    every run: skhanjr.bsky.social marked 18 of 23 refreshes degraded.
    """

    class Result:
        def __init__(self, handle, status):
            self.requested_handle, self.status = handle, status

    def resolve(self, verified, failed):
        from sports_aggregator.social.cli import _endpoint_exit
        return _endpoint_exit(
            [self.Result(f"ok{i}", "verified") for i in range(verified)]
            + [self.Result(f"dead{i}", "resolution_failed") for i in range(failed)],
            kind="bluesky")

    def test_one_dead_handle_among_many_is_tolerated(self):
        self.assertEqual(self.resolve(39, 1), 0)

    def test_a_wide_failure_still_fails(self):
        """That is the API being down, which is worth waking up for."""
        self.assertEqual(self.resolve(0, 40), 1)

    def test_the_tolerance_is_the_documented_one(self):
        from sports_aggregator.social.content_cli import ENDPOINT_FAILURE_TOLERANCE
        total = 100
        allowed = int(total * ENDPOINT_FAILURE_TOLERANCE)
        self.assertEqual(self.resolve(total - allowed, allowed), 0)
        self.assertEqual(self.resolve(total - allowed - 1, allowed + 1), 1)

    def test_nothing_to_resolve_is_not_a_failure(self):
        self.assertEqual(self.resolve(0, 0), 0)


class RefreshWiringTests(unittest.TestCase):
    def test_the_analytics_pipeline_has_scheduled_steps(self):
        """It had none, which is why the whole layer was empty."""
        from sports_aggregator.bootstrap import steps
        by_name = {step.name: step for step in steps(2026)}
        for name in ("pbp", "pbp-derive", "epa", "team-advanced",
                     "win-probability", "passing-detail", "passing-qb"):
            self.assertIn(name, by_name, f"{name} has no scheduled step")
            self.assertIn("refresh", by_name[name].phases)

    def test_recruiting_is_backfilled_not_only_synced_for_this_season(self):
        """A player who signed in an earlier class had no pedigree on his page.

        Only the current season was ever synced, so a 2026 roster showed a rating
        for its freshmen and nothing for everyone else.
        """
        from sports_aggregator.bootstrap import steps
        names = [step.name for step in steps(2026)]
        self.assertIn("cfbd-recruits", names)
        history = [name for name in names if name.startswith("recruits-history-")]
        self.assertGreaterEqual(len(history), 5,
                                "a roster spans more classes than this reaches")

    def test_the_epa_step_builds_the_version_the_report_reads(self):
        from sports_aggregator.bootstrap import steps
        from sports_aggregator.cfb.team_game_advanced import MODEL_VERSION
        command = {step.name: step.command for step in steps(2026)}["team-advanced"]
        self.assertIn(MODEL_VERSION, command)


if __name__ == "__main__":
    unittest.main()
