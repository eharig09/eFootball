from datetime import datetime, timedelta, timezone
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from app import create_app
from sports_aggregator.catalog import get_league
from sports_aggregator.cfb.cfbd import CFBDClient, CFBDConfigurationError
from sports_aggregator.cfb.models import Game, Player
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.statlines import player_stat_tables
from sports_aggregator.cfb.sync import CFBDataSync
from sports_aggregator.cfb.web import _nearest_week_games
from sports_aggregator.models import Article
from sports_aggregator.service import AggregationResult


class PlayerStatTablesPpaTests(unittest.TestCase):
    def test_ppa_category_renders_as_its_own_labeled_table(self):
        """The "ppa" category needed no new rendering path -- registering it
        in statlines.CATEGORY_SPECS is enough for player_stat_tables (the
        same pivot used for passing/rushing/receiving) to produce it."""
        tables = player_stat_tables([
            {"season": 2026, "team": "Michigan", "position": "QB",
             "category": "ppa", "stat_type": "PPA_ALL", "numeric_value": .35, "stat_value": ".35"},
            {"season": 2026, "team": "Michigan", "position": "QB",
             "category": "ppa", "stat_type": "PPA_PASS", "numeric_value": .4, "stat_value": ".4"},
        ])
        ppa = next(entry for entry in tables if entry["category"] == "ppa")
        self.assertEqual(ppa["label"], "Predicted points added (PPA)")
        self.assertEqual([column.key for column in ppa["table"].columns][:2], ["season", "team"])
        self.assertIn("PPA_ALL", [column.key for column in ppa["table"].columns])
        self.assertAlmostEqual(ppa["table"].rows[0]["PPA_ALL"], .35)


class WeekSelectionTests(unittest.TestCase):
    def test_week_zero_is_the_first_upcoming_week(self):
        week, games = _nearest_week_games([
            {"game_id": 2, "week": 1}, {"game_id": 1, "week": 0},
        ])
        self.assertEqual(week, 0)
        self.assertEqual([game["game_id"] for game in games], [1])


TEAM_PAYLOAD = [
    {
        "id": 1, "school": "Michigan", "mascot": "Wolverines", "abbreviation": "MICH",
        "alternateNames": ["U-M"], "conference": "Big Ten", "division": None,
        "classification": "fbs", "color": "00274C", "alternateColor": "FFCB05",
        "logos": ["https://example.com/michigan.png"],
        "location": {"id": 10, "name": "Michigan Stadium"},
    },
    {
        "id": 2, "school": "Wisconsin", "mascot": "Badgers", "abbreviation": "WIS",
        "alternateNames": [], "conference": "Big Ten", "division": None,
        "classification": "fbs", "color": "C5050C", "alternateColor": "FFFFFF",
        "logos": [], "location": {"id": 20, "name": "Camp Randall Stadium"},
    },
]

GAME_PAYLOAD = [
    {
        "id": 100, "season": 2026, "week": 2, "seasonType": "regular",
        "startDate": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(), "startTimeTBD": False,
        "completed": False, "neutralSite": False, "conferenceGame": True,
        "venueId": 10, "venue": "Michigan Stadium", "homeId": 1,
        "homeTeam": "Michigan", "homeConference": "Big Ten", "homePoints": None,
        "homePregameElo": 1750, "awayId": 2, "awayTeam": "Wisconsin",
        "awayConference": "Big Ten", "awayPoints": None, "awayPregameElo": 1680,
        "excitementIndex": None, "notes": None,
    }
]

RANKING_PAYLOAD = [
    {
        "season": 2026, "seasonType": "regular", "week": 2,
        "polls": [{
            "poll": "AP Top 25", "isFinal": False,
            "ranks": [
                {"rank": 5, "teamId": 1, "school": "Michigan", "conference": "Big Ten", "firstPlaceVotes": 1, "points": 1200},
                {"rank": 18, "teamId": 2, "school": "Wisconsin", "conference": "Big Ten", "firstPlaceVotes": 0, "points": 500},
                {"rank": 18, "teamId": 3, "school": "Iowa", "conference": "Big Ten", "firstPlaceVotes": 0, "points": 500},
            ],
        }],
    }
]

PLAYER_PAYLOAD = [
    {"id": "p1", "firstName": "Alex", "lastName": "Example", "team": "Michigan",
     "position": "QB", "jersey": 7, "height": 74, "weight": 215, "year": 3},
]


class FakeCFBDClient:
    def teams(self, _year, _force=False): return TEAM_PAYLOAD
    def roster(self, _year, _force=False): return PLAYER_PAYLOAD
    # The sync gained a recruits dataset after this stand-in was written,
    # and a client that cannot answer it fails the whole run. Empty is a
    # legitimate answer -- a class with nobody in it yet -- and keeps this
    # test about the canonical entities it is named for.
    def recruits(self, _year, _force=False): return []
    def games(self, _year, _force=False): return GAME_PAYLOAD
    def betting_lines(self, _year, _force=False): return []
    def game_media(self, _year, _force=False): return [{"id": 100, "outlet": "ABC"}]
    def records(self, _year, _force=False):
        return [
            {"teamId": 1, "team": "Michigan", "classification": "fbs", "conference": "Big Ten", "division": None, "expectedWins": 10.2, "total": {"games": 1, "wins": 1, "losses": 0, "ties": 0}, "conferenceGames": {"games": 1, "wins": 1, "losses": 0, "ties": 0}},
            {"teamId": 2, "team": "Wisconsin", "classification": "fbs", "conference": "Big Ten", "division": None, "expectedWins": 8.1, "total": {"games": 1, "wins": 1, "losses": 0, "ties": 0}, "conferenceGames": {"games": 1, "wins": 1, "losses": 0, "ties": 0}},
        ]
    def coaches(self, year, _force=False):
        return [{"id": 11, "firstName": "Test", "lastName": "Coach",
                 "seasons": [{"teamId": 1, "school": "Michigan",
                              "conference": "Big Ten", "year": year,
                              "games": 1, "wins": 1, "losses": 0, "ties": 0,
                              "winPercentage": 1.0}]}]
    def rankings(self, _year, _force=False): return RANKING_PAYLOAD
    def team_stats(self, _year, _force=False):
        return [{"season": 2026, "team": "Michigan", "conference": "Big Ten", "statName": "yardsPerRush", "statValue": 5.4}]
    def advanced_team_stats(self, _year, _force=False):
        return [
            {"season": 2026, "team": "Michigan", "conference": "Big Ten", "offense": {"successRate": .51, "explosiveness": 1.2, "ppa": .25, "pointsPerOpportunity": 4.8, "havoc": {"total": .12}}, "defense": {"successRate": .32, "explosiveness": .8, "ppa": -.1, "pointsPerOpportunity": 2.1, "havoc": {"total": .21}}},
            {"season": 2026, "team": "Wisconsin", "conference": "Big Ten", "offense": {"successRate": .45, "explosiveness": 1.0, "ppa": .18, "pointsPerOpportunity": 4.1, "havoc": {"total": .15}}, "defense": {"successRate": .37, "explosiveness": .9, "ppa": -.02, "pointsPerOpportunity": 2.8, "havoc": {"total": .18}}},
        ]
    def core_ratings(self, _year, _force=False):
        return [{"year": 2026, "throughSeasonType": "regular", "throughWeek": 1, "team": "Michigan", "conference": "Big Ten", "overall": 18.2, "offense": 10.1, "defense": -8.1, "offensePlays": 70, "defensePlays": 65, "modelVersion": "test"}]
    def ppa_players_season(self, _year, _force=False):
        return [
            {"season": 2026, "id": "500", "name": "Test QB", "position": "QB", "team": "Michigan",
             "conference": "Big Ten", "averagePPA": {"all": .35, "pass": .4, "rush": -.1},
             "totalPPA": {"all": 100.0, "pass": 105.0, "rush": -5.0}},
        ]


class StubReporting:
    def aggregate(self, league):
        return AggregationResult(
            league=league,
            articles=(Article(title="Game story", url="https://example.com/game", source="Example", reliability=4),),
            errors=(), fetched_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        )


class CFBRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = CFBRepository(os.path.join(self.temp_dir.name, "cfb.sqlite3"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_write_transactions_wait_for_the_existing_writer(self):
        self.repository.initialize()
        blocker = sqlite3.connect(self.repository.path)
        blocker.execute("BEGIN IMMEDIATE")
        outcome = []

        def write_after_blocker():
            try:
                with self.repository.transaction() as connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS cfb_lock_probe(value INTEGER)")
                    connection.execute("INSERT INTO cfb_lock_probe VALUES(1)")
                outcome.append("committed")
            except Exception as exc:  # pragma: no cover - asserted below
                outcome.append(f"{type(exc).__name__}: {exc}")

        writer = threading.Thread(target=write_after_blocker)
        writer.start()
        time.sleep(0.1)
        self.assertTrue(writer.is_alive())
        blocker.commit()
        blocker.close()
        writer.join(timeout=3)
        self.assertEqual(outcome, ["committed"])
        connection = self.repository._connect()
        try:
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 60_000)
        finally:
            connection.close()

    def test_opponent_quality_uses_kickoff_elo_and_keeps_models_separate(self):
        completed = {**GAME_PAYLOAD[0], "completed": True, "homePoints": 31,
                     "awayPoints": 17}
        self.repository.replace_games(2026, [Game.from_cfbd(completed)])
        with self.repository.transaction() as connection:
            connection.execute(
                "INSERT INTO core_ratings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (2026, "regular", 2, "Wisconsin", "Big Ten", 8.5, 4.0,
                 -4.5, 120, 118, "test"))
            connection.execute(
                "INSERT INTO rankings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (2026, "regular", 2, "AP Top 25", 0, 18, 2, "Wisconsin",
                 "Big Ten", 0, 500,))
        quality = self.repository.opponent_quality(1, 2026)
        self.assertEqual(quality["games"], 1)
        self.assertEqual(quality["average_pregame_elo"], 1680)
        self.assertEqual(quality["average_core"], 8.5)
        self.assertEqual(quality["poll_ranked"], 1)

    def test_elo_snapshot_ranks_fbs_and_preserves_rating_context(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        next_game = {
            **GAME_PAYLOAD[0], "id": 101, "week": 3,
            "startDate": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat(),
            "homePregameElo": 1768, "awayPregameElo": 1662,
        }
        self.repository.replace_games(
            2026, (Game.from_cfbd(game) for game in (GAME_PAYLOAD[0], next_game)))
        snapshot = self.repository.elo_snapshot(2026)

        self.assertEqual([row["team"] for row in snapshot["rankings"]],
                         ["Michigan", "Wisconsin"])
        self.assertEqual(snapshot["rankings"][0]["rank"], 1)
        # Week 2 is the earliest upcoming observation. A later scheduled game
        # must not make the current board jump forward to Week 3.
        self.assertEqual(snapshot["rankings"][0]["season_high"], 1750)
        self.assertEqual(snapshot["rankings"][0]["season_low"], 1750)
        self.assertIsNone(snapshot["rankings"][0]["weekly_change"])
        self.assertEqual(snapshot["conferences"][0]["average_elo"], 1715.0)
        self.assertEqual(snapshot["summary"]["median_elo"], 1715.0)
        self.assertIn(2026, snapshot["available_seasons"])

    def test_elo_snapshot_builds_weekly_and_season_movers(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        now = datetime.now(timezone.utc)
        games = []
        for game_id, week, days, completed, home_elo, away_elo in (
                (101, 1, -14, True, 1700, 1730),
                (102, 2, -7, True, 1750, 1680),
                (103, 3, 7, False, 1780, 1650)):
            games.append(Game.from_cfbd({
                **GAME_PAYLOAD[0], "id": game_id, "week": week,
                "startDate": (now + timedelta(days=days)).isoformat(),
                "completed": completed, "homePregameElo": home_elo,
                "awayPregameElo": away_elo,
            }))
        self.repository.replace_games(2026, games)

        snapshot = self.repository.elo_snapshot(2026)
        by_team = {row["team"]: row for row in snapshot["rankings"]}
        self.assertEqual(by_team["Michigan"]["weekly_change"], 30)
        self.assertEqual(by_team["Michigan"]["season_change"], 80)
        self.assertEqual(by_team["Wisconsin"]["weekly_change"], -30)
        self.assertEqual(by_team["Wisconsin"]["season_change"], -80)
        self.assertEqual(snapshot["movement"]["weekly"]["risers"][0]["team"],
                         "Michigan")
        self.assertEqual(snapshot["movement"]["weekly"]["fallers"][0]["team"],
                         "Wisconsin")
        self.assertEqual(snapshot["movement"]["season"]["risers"][0]["team"],
                         "Michigan")

    def test_elo_page_and_api_use_the_same_snapshot(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "LEAGUE_AGGREGATION_SERVICE": StubReporting(),
        })
        client = app.test_client()

        page = client.get("/college-football/elo/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"FBS Elo ratings", page.data)
        self.assertIn(b"Risers and fallers", page.data)
        self.assertIn(b"Michigan", page.data)
        payload = client.get("/api/v1/cfb/elo").get_json()
        self.assertEqual(payload["rankings"][0]["team"], "Michigan")
        self.assertIn("weekly", payload["movement"])

    def test_replace_player_ppa_only_touches_the_ppa_category(self):
        """`/ppa/players/season` isn't conference-scoped like the regular
        stats sync -- storing it must not wipe other categories the way a
        conference=None replace_player_stats call would."""
        self.repository.replace_player_stats(2026, [
            {"season": 2026, "playerId": "p1", "player": "Alex Example",
             "team": "Michigan", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": "YDS", "stat": "3120"},
        ], "Big Ten")
        count = self.repository.replace_player_ppa(2026, [
            {"season": 2026, "id": "p1", "name": "Alex Example", "position": "QB",
             "team": "Michigan", "conference": "Big Ten",
             "averagePPA": {"all": .35, "pass": .4, "rush": None},
             "totalPPA": {"all": 100.0, "pass": 105.0, "rush": None}},
        ])
        self.assertEqual(count, 2)  # PPA_ALL and PPA_PASS; PPA_RUSH skipped (None)

        def stored_categories():
            with self.repository._reader() as connection:
                rows = connection.execute(
                    "SELECT category,stat_type,numeric_value FROM player_season_stats WHERE player_id='p1'"
                ).fetchall()
            return {(row["category"], row["stat_type"]): row["numeric_value"] for row in rows}

        stored = stored_categories()
        self.assertEqual(stored, {
            ("passing", "YDS"): 3120.0,
            ("ppa", "PPA_ALL"): .35,
            ("ppa", "PPA_PASS"): .4,
        })

        # Re-syncing PPA for the same season must not disturb "passing".
        self.repository.replace_player_ppa(2026, [])
        self.assertEqual(stored_categories(), {("passing", "YDS"): 3120.0})

    def test_current_impact_players_are_production_led_and_merge_roles(self):
        def rows(player_id, player, position, category, stats):
            return [{
                "season": 2026, "playerId": player_id, "player": player,
                "team": "Michigan", "conference": "Big Ten", "position": position,
                "category": category, "statType": key, "stat": value,
            } for key, value in stats.items()]

        stats = []
        stats += rows("qb", "Impact Passer", "QB", "passing",
                      {"ATT": 42, "YDS": 210, "TD": 1, "INT": 1})
        stats += rows("rb", "Two Way Back", "RB", "rushing",
                      {"CAR": 31, "YDS": 220, "TD": 3, "YPC": 7.1})
        stats += rows("rb", "Two Way Back", "RB", "receiving",
                      {"REC": 9, "YDS": 145, "TD": 2})
        stats += rows("lb", "Impact Defender", "LB", "defensive",
                      {"TOT": 18, "TFL": 4, "SACKS": 2, "PD": 1, "TD": 0})
        stats += rows("wr", "Emerging Target", "WR", "receiving",
                      {"REC": 3, "YDS": 50, "TD": 0})
        stats += rows("reserve", "Small Sample", "WR", "receiving",
                      {"REC": 1, "YDS": 12, "TD": 0})
        self.repository.replace_player_stats(2026, stats)

        impact = self.repository.team_impact_players("Michigan", 2026)
        by_id = {row["player_id"]: row for row in impact}
        self.assertEqual(set(by_id), {"qb", "rb", "lb", "wr"})
        self.assertEqual(by_id["rb"]["roles"], ["Ground engine", "Primary target"])
        self.assertEqual(len(by_id["rb"]["evidence"]), 2)
        self.assertEqual(by_id["rb"]["impact_level"], "primary")
        self.assertEqual(by_id["qb"]["impact_label"], "Notable impact")
        self.assertEqual(by_id["wr"]["impact_level"], "emerging")
        self.assertNotIn("role_leader", by_id["lb"])
        self.assertNotIn("primary_signal", by_id["lb"])
        self.assertNotIn("impact_sort", by_id["lb"])
        self.assertNotIn("reserve", by_id)

    def test_advanced_metric_ranks_always_treat_the_best_result_as_top_percentile(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)

        context = self.repository.advanced_metric_ranks(2026)
        michigan = context["Michigan"]
        wisconsin = context["Wisconsin"]
        # Higher is better for offensive success and defensive havoc.
        self.assertEqual(michigan["offense_success_rate"],
                         {"rank": 1, "of": 2, "percentile": 100})
        self.assertEqual(michigan["defense_havoc"]["rank"], 1)
        # Lower is better for defensive efficiency and offensive havoc allowed.
        self.assertEqual(michigan["defense_success_rate"]["rank"], 1)
        self.assertEqual(michigan["offense_havoc"]["rank"], 1)
        self.assertEqual(wisconsin["offense_success_rate"],
                         {"rank": 2, "of": 2, "percentile": 0})

    def test_player_ppa_rank_applies_the_position_appropriate_volume_floor(self):
        """PPA has no play-count field of its own, so the qualifying floor
        is borrowed from the position's existing box-score threshold (ATT
        for a QB) -- a low-volume outlier must not out-rank a real starter."""
        self.repository.replace_player_stats(2026, [
            {"season": 2026, "playerId": "starter", "player": "Real Starter",
             "team": "Michigan", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": "ATT", "stat": "300"},
            {"season": 2026, "playerId": "mopup", "player": "Mop Up",
             "team": "Wisconsin", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": "ATT", "stat": "2"},
        ], None)
        self.repository.replace_player_ppa(2026, [
            {"season": 2026, "id": "starter", "name": "Real Starter", "position": "QB",
             "team": "Michigan", "conference": "Big Ten",
             "averagePPA": {"all": .3}, "totalPPA": {"all": 90.0}},
            {"season": 2026, "id": "mopup", "name": "Mop Up", "position": "QB",
             "team": "Wisconsin", "conference": "Big Ten",
             "averagePPA": {"all": 6.9}, "totalPPA": {"all": 6.9}},
        ])
        ranks = self.repository.player_ppa_rank(2026, "QB")
        self.assertEqual(ranks, {"starter": {"rank": 1, "of": 1}})
        self.assertNotIn("mopup", ranks)

    def test_syncs_canonical_entities_aliases_and_preview_data(self):
        report = CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        self.assertTrue(report.succeeded)
        self.assertEqual(self.repository.status(2026)["counts"]["games"], 1)
        self.assertEqual(self.repository.status(2026)["counts"]["players"], 1)
        self.assertEqual(self.repository.resolve_team_alias("U-M")[0]["team_id"], 1)
        rankings = self.repository.latest_rankings(2026)
        self.assertEqual(rankings["poll"], "AP Top 25")
        game = self.repository.get_game(100)
        self.assertEqual(game["television"], "ABC")
        self.repository.replace_games(2026, (Game.from_cfbd(item) for item in GAME_PAYLOAD))
        self.assertEqual(self.repository.get_game(100)["television"], "ABC")
        self.assertEqual(game["records"]["Michigan"]["wins"], 1)
        self.assertAlmostEqual(game["advanced_metrics"]["Wisconsin"]["offense_success_rate"], .45)
        standings = self.repository.conference_standings("Big Ten", 2026)
        self.assertEqual(standings[0]["conference_wins"], 1)

    def test_player_stat_leaders_and_preview_routes(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        historical_game = {**GAME_PAYLOAD[0], "id": 99, "season": 2025,
                           "startDate": "2025-09-06T19:30:00Z", "completed": True,
                           "homePoints": 21, "awayPoints": 14}
        self.repository.replace_games(2025, (Game.from_cfbd(historical_game),))
        current_result = {
            **GAME_PAYLOAD[0], "id": 98, "week": 1,
            "startDate": "2026-08-29T19:30:00Z", "completed": True,
            "homePoints": 31, "awayPoints": 17,
        }
        self.repository.replace_games(
            2026, (Game.from_cfbd(item) for item in (current_result, GAME_PAYLOAD[0])))
        self.repository.replace_player_stats(2025, [
            {"season": 2025, "playerId": "p1", "player": "Alex Example",
             "team": "Michigan", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": "YDS", "stat": "3120"},
            {"season": 2025, "playerId": "p1", "player": "Alex Example",
             "team": "Michigan", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": "ATT", "stat": "300"},
            {"season": 2025, "playerId": "p1", "player": "Alex Example",
             "team": "Michigan", "conference": "Big Ten", "position": "QB",
             "category": "passing", "statType": "TD", "stat": "27"},
            {"season": 2025, "playerId": "trick", "player": "Trick Play",
             "team": "Michigan", "conference": "Big Ten", "position": "WR",
             "category": "passing", "statType": "YDS", "stat": "40"},
            {"season": 2025, "playerId": "trick", "player": "Trick Play",
             "team": "Michigan", "conference": "Big Ten", "position": "WR",
             "category": "passing", "statType": "ATT", "stat": "1"},
            {"season": 2025, "playerId": "p2", "player": "Runner Example",
             "team": "Wisconsin", "conference": "Big Ten", "position": "RB",
             "category": "rushing", "statType": "YDS", "stat": "1400"},
            {"season": 2025, "playerId": "p2", "player": "Runner Example",
             "team": "Wisconsin", "conference": "Big Ten", "position": "RB",
             "category": "rushing", "statType": "CAR", "stat": "240"},
        ], "Big Ten")
        leaders = self.repository.conference_player_leaders("Big Ten", 2026)
        self.assertEqual(leaders["season"], 2025)
        passing = leaders["groups"]["passing"]
        self.assertEqual(passing["players"][0]["player"], "Alex Example")
        # The whole category stat line travels with each leader, not just the
        # statistic the leaderboard is ranked by.
        self.assertEqual(passing["players"][0]["stats"]["TD"], 27)
        # A single trick-play attempt does not qualify as a passing leader.
        self.assertNotIn("Trick Play", [row["player"] for row in passing["players"]])
        self.assertEqual(passing["qualifier"], "min 25 ATT")

        app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "LEAGUE_AGGREGATION_SERVICE": StubReporting(),
        })
        client = app.test_client()
        self.assertEqual(client.get("/college-football/conferences/big-ten/").status_code, 200)
        self.assertIn(b"Conference player leaders", client.get("/college-football/conferences/big-ten/").data)
        self.assertEqual(client.get("/college-football/teams/1/").status_code, 200)
        current_team = client.get("/college-football/teams/1/?season=2025")
        self.assertIn(b"2026 schedule", current_team.data)
        self.assertIn(b"W 31-17", current_team.data)
        self.assertIn(b"/college-football/games/98/box-score/", current_team.data)
        self.assertNotIn(b"Upcoming schedule", current_team.data)
        prior_schedule = client.get("/college-football/teams/1/?schedule_year=2025")
        self.assertIn(b"2025 schedule", prior_schedule.data)
        self.assertIn(b"W 21-14", prior_schedule.data)
        self.assertEqual(client.get("/college-football/teams/1/history/").status_code, 200)
        self.assertEqual(client.get("/college-football/teams/1/history/stats/").status_code, 200)
        self.assertEqual(client.get("/college-football/games/100/").status_code, 200)
        self.assertIn(b"Matchups to watch", client.get("/college-football/games/100/").data)
        self.assertIn(b'data-mobile-tab="projection"', client.get("/college-football/games/100/").data)
        self.assertIn(b"Game projection", client.get("/college-football/games/100/").data)
        projection = client.get("/api/v1/cfb/games/100/projection")
        self.assertEqual(projection.status_code, 200)
        self.assertEqual(projection.get_json()["away_team"], "Wisconsin")
        self.assertIn("projection", client.get("/api/v1/cfb/games/100/preview").get_json())
        self.assertEqual(client.get("/api/v1/cfb/conferences/big-ten").status_code, 200)
        self.assertEqual(client.get("/api/v1/cfb/teams/1").get_json()["team"]["school"], "Michigan")
        self.assertEqual(client.get("/api/v1/cfb/games/100/preview").status_code, 200)

    def test_prior_year_leader_fallback_excludes_players_not_on_current_roster(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        self.repository.replace_player_stats(2025, [
            {"playerId": "p1", "player": "Alex Example", "team": "Michigan",
             "conference": "Big Ten", "position": "QB", "category": "passing",
             "statType": "YDS", "stat": 3000},
            {"playerId": "p1", "player": "Alex Example", "team": "Michigan",
             "conference": "Big Ten", "position": "QB", "category": "passing",
             "statType": "ATT", "stat": 350},
            {"playerId": "gone", "player": "Departed Star", "team": "Michigan",
             "conference": "Big Ten", "position": "QB", "category": "passing",
             "statType": "YDS", "stat": 4500},
            {"playerId": "gone", "player": "Departed Star", "team": "Michigan",
             "conference": "Big Ten", "position": "QB", "category": "passing",
             "statType": "ATT", "stat": 500},
        ], "Big Ten")
        leaders = self.repository.team_player_leaders("Michigan", 2026)
        names = [row["player"] for row in leaders["groups"]["passing"]["players"]]
        self.assertIn("Alex Example", names)
        self.assertNotIn("Departed Star", names)

    def _seed_returning_qb(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)

        def line(season, yds, att):
            return [{"playerId": "p1", "player": "Alex Example", "team": "Michigan",
                     "conference": "Big Ten", "position": "QB", "category": "passing",
                     "statType": stat, "stat": value}
                    for stat, value in (("YDS", yds), ("ATT", att))]

        self.repository.replace_player_stats(2025, line(2025, 3400, 420), "Big Ten")
        self.repository.replace_player_stats(2026, line(2026, 280, 34), "Big Ten")

    def _complete_michigan_games(self, count):
        games = []
        for index in range(count):
            games.append(Game.from_cfbd({
                **GAME_PAYLOAD[0], "id": 500 + index, "week": index + 1,
                "completed": True, "homePoints": 30, "awayPoints": 20}))
        self.repository.replace_games(2026, games)

    def test_the_board_holds_on_the_prior_season_until_this_one_settles(self):
        self._seed_returning_qb()
        self._complete_michigan_games(2)

        leaders = self.repository.team_player_leaders("Michigan", 2026)
        self.assertEqual(leaders["season"], 2025, "two games is too few to rank on")
        self.assertFalse(leaders["settled"])
        leader = leaders["groups"]["passing"]["players"][0]
        self.assertEqual(leader["stats"]["YDS"], 3400)
        # 2026 production is still attached, so it is shown rather than hidden.
        self.assertEqual(leader["companion_season"], 2026)
        self.assertEqual(leader["companion"]["YDS"], 280)

    def test_the_board_switches_to_this_season_once_enough_games_are_in(self):
        self._seed_returning_qb()
        self._complete_michigan_games(self.repository.LEADER_SETTLE_GAMES)

        leaders = self.repository.team_player_leaders("Michigan", 2026)
        self.assertEqual(leaders["season"], 2026)
        self.assertTrue(leaders["settled"])
        leader = leaders["groups"]["passing"]["players"][0]
        self.assertEqual(leader["stats"]["YDS"], 280)
        self.assertEqual((leader["companion_season"], leader["companion"]["YDS"]), (2025, 3400))

    def test_dashboard_and_game_api_use_persisted_data(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository,
            "CFB_DEFAULT_SEASON": 2026,
            "LEAGUE_AGGREGATION_SERVICE": StubReporting(),
        })
        client = app.test_client()
        dashboard = client.get("/college-football/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Michigan", dashboard.data)
        self.assertIn(b"Games to Watch", dashboard.data)
        historical_query = client.get("/college-football/?season=2025")
        self.assertIn(b"2026", historical_query.data)
        game = client.get("/api/v1/cfb/games/100")
        self.assertEqual(game.status_code, 200)
        self.assertEqual(game.get_json()["television"], "ABC")
        watch = client.get("/api/v1/cfb/games-to-watch")
        self.assertEqual(watch.get_json()["games"][0]["game_id"], 100)
        teams = client.get("/api/v1/cfb/teams?conference=Big%20Ten")
        self.assertEqual(teams.get_json()["count"], 2)

    def test_roster_lifecycle_depth_board_and_player_page(self):
        CFBDataSync(FakeCFBDClient(), self.repository).sync(2026)
        self.repository.replace_players(2025, (
            Player("p1", 2025, "Alex", "Example", "Michigan", "QB", 7, 74, 215, 3),
            Player("draft1", 2025, "Dan", "Drafted", "Michigan", "DE", 9, 76, 255, 4),
            Player("transfer1", 2025, "Tom", "Transfer", "Michigan", "WR", 2, 72, 190, 2),
            Player("grad1", 2025, "Gary", "Graduate", "Michigan", "OL", 70, 77, 310, 4),
        ))
        self.repository.replace_transfers(2026, ({
            "firstName": "Tom", "lastName": "Transfer", "position": "WR",
            "origin": "Michigan", "destination": "Wisconsin",
            "transferDate": "2026-01-10T00:00:00Z", "rating": 0.91,
            "stars": 4, "eligibility": "Immediate",
        },))
        self.repository.replace_draft_picks(2026, ({
            "overall": 20, "round": 1, "pick": 20, "collegeAthleteId": "draft1",
            "collegeId": 1, "collegeTeam": "Michigan", "collegeConference": "Big Ten",
            "nflTeamId": 10, "nflTeam": "Example Pros", "name": "Dan Drafted",
            "position": "DE", "preDraftRanking": 15,
            "preDraftPositionRanking": 2, "preDraftGrade": 90,
        },))
        movements = self.repository.roster_movements(1, 2026)
        by_name = {row["name"]: row for row in movements["departures"]}
        self.assertEqual(by_name["Dan Drafted"]["movement_type"], "DRAFTED")
        self.assertEqual(by_name["Tom Transfer"]["movement_type"], "TRANSFER_OUT")
        self.assertEqual(by_name["Gary Graduate"]["movement_type"], "ELIGIBILITY_DEPARTURE")
        depth = self.repository.team_depth_chart(1, 2026)
        self.assertEqual(depth["summary"]["returners"], 1)

        connection = sqlite3.connect(self.repository.path)
        connection.executemany(
            """INSERT INTO pff_players(season,pff_player_id,player_name,normalized_name,
               position,pff_team_name,cfbd_team_id,cfbd_team,cfbd_player_id,
               match_status,match_confidence,interest_score,updated_at)
               VALUES(2025,?,?,?,?,?,?,?,?,'CONFIRMED',1.0,?,'now')""", (
                ("pff-returner", "Alex Example", "alex example", "QB", "MICH",
                 1, "Michigan", "p1", 90.0),
                ("pff-drafted", "Dan Drafted", "dan drafted", "DE", "MICH",
                 1, "Michigan", "draft1", 95.0),
            ))
        connection.commit()
        connection.close()
        conference_players = self.repository.conference_pff_players(
            "Big Ten", 2025, roster_season=2026)
        self.assertEqual([row["player_name"] for row in conference_players], ["Alex Example"])
        self.assertEqual(conference_players[0]["roster_status"], "RETURNING")

        app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "LEAGUE_AGGREGATION_SERVICE": StubReporting(),
        })
        client = app.test_client()
        self.assertEqual(client.get("/college-football/players/p1/").status_code, 200)
        self.assertIn(b"Career path", client.get("/college-football/players/p1/").data)
        self.assertEqual(client.get("/api/v1/cfb/players/p1").get_json()["name"], "Alex Example")


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): return None
    def json(self): return self.payload


class FakeSession:
    def __init__(self, payload): self.payload = payload; self.calls = []
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return FakeResponse(self.payload)


class CFBDClientTests(unittest.TestCase):
    def test_requires_key_and_caches_raw_authenticated_response(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CFBDConfigurationError):
                CFBDClient(api_key="", raw_cache_path=directory).teams(2026)

            session = FakeSession(TEAM_PAYLOAD)
            client = CFBDClient(api_key="secret", raw_cache_path=directory, session=session)
            self.assertEqual(len(client.teams(2026)), 2)
            self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer secret")
            self.assertEqual(len(client.teams(2026)), 2)
            self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
