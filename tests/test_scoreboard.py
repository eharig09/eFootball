"""One day's games, and the day being the reader's rather than UTC's.

Kickoffs are stored in UTC and read in the display timezone, and the two
disagree about which calendar day a night game belongs to: 64 of the first 400
games this season fall on different dates under the two readings. Grouping on
the stored string would file a sixth of the schedule under the wrong day.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app import create_app
from sports_aggregator.cfb.web import (
    SLATE_STRIP_GAMES, _current_slate,
)
from sports_aggregator.cfb.external import initialize as external_initialize
from sports_aggregator.cfb.lines import initialize as lines_initialize
from sports_aggregator.cfb.models import Game, Team
from sports_aggregator.cfb.repository import (
    CFBRepository, forget_initialized_schemas,
)
from sports_aggregator.cfb.views import _market_line
from sports_aggregator.social.content import ContentRepository
from sports_aggregator.social.stories import StoryRepository


def _game(game_id, start, home_id, home, away_id, away, *, completed=False,
          home_points=None, away_points=None, conference="Big Ten",
          home_elo=None, away_elo=None):
    return Game.from_cfbd({
        "homePregameElo": home_elo, "awayPregameElo": away_elo,
        "id": game_id, "season": 2026, "week": 1, "seasonType": "regular",
        "startDate": start, "startTimeTBD": False, "completed": completed,
        "neutralSite": False, "conferenceGame": True, "venue": "Stadium",
        "venueId": 1, "homeId": home_id, "homeTeam": home,
        "homeConference": conference, "homePoints": home_points,
        "awayId": away_id, "awayTeam": away, "awayConference": conference,
        "awayPoints": away_points,
    })


class ScoreboardTests(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams((
            Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs",
                 "00274C", "FFCB05", (), ("Michigan",), None, None),
            Team(2, "Ohio State", "Buckeyes", "OSU", "Big Ten", None, "fbs",
                 "BB0000", "666666", (), ("Ohio State",), None, None),
            Team(3, "Alabama", "Tide", "BAMA", "SEC", None, "fbs",
                 "9E1B32", "828A8F", (), ("Alabama",), None, None),
            Team(4, "Georgia", "Dawgs", "UGA", "SEC", None, "fbs",
                 "BA0C2F", "000000", (), ("Georgia",), None, None),
        ))
        self.repository.replace_games(2026, [
            # 8pm Eastern on 4 September is midnight UTC on the 5th.
            _game(1, "2026-09-05T00:00:00.000Z", 1, "Michigan", 2, "Ohio State"),
            _game(2, "2026-09-05T16:00:00.000Z", 3, "Alabama", 4, "Georgia",
                  conference="SEC", completed=True, home_points=31, away_points=17),
        ])
        ContentRepository(self.path).initialize()
        StoryRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "CFB_DATABASE_PATH": self.path,
            "CFB_DISPLAY_TIMEZONE": "America/New_York",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    # -- the day belongs to the reader -------------------------------------

    def test_a_night_game_belongs_to_the_local_day_not_the_utc_one(self):
        """Midnight UTC on the 5th is 8pm Eastern on the 4th."""
        days = {entry["date"]: entry["games"]
                for entry in self.repository.scoreboard_days(2026)}
        self.assertEqual(days.get("2026-09-04"), 1)
        self.assertEqual(days.get("2026-09-05"), 1)

    def test_games_on_a_day_are_selected_in_local_time(self):
        fourth = self.repository.games_on_day("2026-09-04", 2026)
        self.assertEqual([game["game_id"] for game in fourth], [1])
        fifth = self.repository.games_on_day("2026-09-05", 2026)
        self.assertEqual([game["game_id"] for game in fifth], [2])

    def test_an_unparseable_day_returns_nothing_rather_than_raising(self):
        self.assertEqual(self.repository.games_on_day("not-a-date", 2026), [])

    # -- the page ----------------------------------------------------------

    def _body(self, query=""):
        return self.client.get(
            "/college-football/scoreboard/" + query).get_data(as_text=True)

    def test_every_game_links_to_its_matchup_page(self):
        self.assertIn("/college-football/games/2/", self._body("?date=2026-09-05"))

    def test_a_completed_game_shows_its_score(self):
        body = self._body("?date=2026-09-05")
        self.assertIn("Final", body)
        self.assertIn(">31<", body)
        self.assertIn(">17<", body)

    def test_a_scheduled_game_shows_a_kickoff_time_rather_than_a_score(self):
        body = self._body("?date=2026-09-04")
        self.assertIn("8:00 PM", body)
        self.assertNotIn("Final", body)

    def test_the_conference_filter_narrows_the_slate(self):
        body = self._body("?date=2026-09-05&conference=sec")
        self.assertIn("Alabama", body)
        self.assertNotIn("Michigan", body)

    def test_an_unknown_conference_is_ignored_rather_than_emptying_the_page(self):
        self.assertIn("Alabama", self._body("?date=2026-09-05&conference=nope"))

    def test_a_date_with_no_games_falls_back_to_the_nearest_that_has_some(self):
        """An arbitrary date should not be a dead end."""
        self.assertIn("game-card", self._body("?date=1999-01-01"))

    def test_an_unparseable_date_still_renders(self):
        self.assertIn("game-card", self._body("?date=nonsense"))

    def test_navigation_offers_only_days_that_have_games(self):
        body = self._body("?date=2026-09-04")
        self.assertIn("date=2026-09-05", body)
        self.assertIn('aria-disabled="true"', body)   # nothing earlier exists

    def test_conference_pills_use_abbreviations(self):
        body = self._body("?date=2026-09-05")
        self.assertIn("conference-mark", body)
        self.assertIn(">SEC<", body)


class PreviewLinkTests(unittest.TestCase):
    """Only a story that actually previews the game earns the link."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.stories = StoryRepository(self.path)
        self.stories.initialize()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def _seed(self, story_id, kind, game_id):
        with closing(self.stories._connect()) as connection:
            # The content row first: stories.primary_content_id references it.
            connection.execute(
                """INSERT INTO content_items(content_id,platform,platform_content_id,
                   canonical_url,original_url,title,body_text,summary,author_name,
                   publisher_name,published_at,ingested_at,content_type,source_role,
                   raw_json) VALUES(?,'rss',?,?,?,'T','B','S','A','Publisher',
                   '2026-09-01','2026-09-01','ARTICLE','REPORTING','{}')""",
                (story_id, "i%d" % story_id,
                 "https://x.test/%d" % story_id, "https://x.test/%d" % story_id))
            connection.execute(
                """INSERT INTO stories(story_id,cluster_key,headline_canonical,
                   story_type,first_reported_at,last_updated_at,confidence,
                   story_score,primary_content_id,clustering_method,generated_at)
                   VALUES(?,?,?,?,'2026-09-01','2026-09-01',0.9,5.0,?,'X','2026-09-01')""",
                (story_id, "k%d" % story_id, "Headline %d" % story_id, kind, story_id))
            connection.execute(
                "INSERT INTO story_games(story_id,game_id) VALUES(?,?)",
                (story_id, game_id))
            connection.commit()

    def test_only_game_preview_clusters_are_offered(self):
        self._seed(1, "GAME_PREVIEW", 11)
        self._seed(2, "INJURY", 12)
        previews = self.stories.game_previews([11, 12])
        self.assertIn(11, previews)
        self.assertNotIn(12, previews, "an injury note is not a preview")
        self.assertEqual(previews[11]["url"], "https://x.test/1")

    def test_the_link_carries_its_publisher(self):
        self._seed(1, "GAME_PREVIEW", 11)
        self.assertEqual(self.stories.game_previews([11])[11]["publisher"], "Publisher")

    def test_asking_for_nothing_returns_nothing(self):
        self.assertEqual(self.stories.game_previews([]), {})
        self.assertEqual(self.stories.game_previews([None, 0]), {})


class MarketLineTests(unittest.TestCase):
    """A spread is signed against the home side, so it has to be placed."""

    def test_a_negative_spread_belongs_to_the_home_team(self):
        line = _market_line({"home_team": "Georgia", "away_team": "Austin Peay"},
                            {"spread": -47.5, "total": 55.0, "books": 3})
        self.assertEqual(line["favourite"], "home")
        self.assertEqual(line["spread"], "-47.5")
        self.assertEqual(line["total"], "55")
        self.assertEqual(line["title"], "Consensus of 3 books")

    def test_a_positive_spread_belongs_to_the_away_team(self):
        line = _market_line({"home_team": "Purdue", "away_team": "Notre Dame"},
                            {"spread": 21.0, "total": None, "books": 1})
        self.assertEqual(line["favourite"], "away")
        self.assertEqual(line["spread"], "-21")
        self.assertIsNone(line["total"])
        self.assertEqual(line["title"], "Consensus of 1 book")

    def test_a_level_game_has_no_favourite_to_hang_the_number_on(self):
        line = _market_line({"home_team": "A", "away_team": "B"}, {"spread": 0.0})
        self.assertIsNone(line["favourite"])
        self.assertEqual(line["spread"], "PK")

    def test_no_stored_spread_is_no_line(self):
        game = {"home_team": "A", "away_team": "B"}
        self.assertIsNone(_market_line(game, None))
        self.assertIsNone(_market_line(game, {"spread": None, "total": 51.0}))


class ScoreboardExtrasTests(unittest.TestCase):
    """Line, Elo and weather on the card, and the line yielding to the score."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams((
            Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs",
                 "00274C", "FFCB05", (), ("Michigan",), None, None),
            Team(2, "Ohio State", "Buckeyes", "OSU", "Big Ten", None, "fbs",
                 "BB0000", "666666", (), ("Ohio State",), None, None),
            Team(3, "Alabama", "Tide", "BAMA", "SEC", None, "fbs",
                 "9E1B32", "828A8F", (), ("Alabama",), None, None),
            Team(4, "Georgia", "Dawgs", "UGA", "SEC", None, "fbs",
                 "BA0C2F", "000000", (), ("Georgia",), None, None),
        ))
        self.repository.replace_games(2026, [
            _game(1, "2026-09-05T00:00:00.000Z", 1, "Michigan", 2, "Ohio State",
                  home_elo=1804, away_elo=1902),
            _game(2, "2026-09-05T16:00:00.000Z", 3, "Alabama", 4, "Georgia",
                  conference="SEC", completed=True, home_points=31, away_points=17),
        ])
        lines_initialize(self.repository)
        external_initialize(self.repository)
        with closing(self.repository._connect()) as connection:
            for game_id, provider, spread in ((1, "DK", -6.0), (1, "FD", -7.0),
                                              (2, "DK", -3.5)):
                connection.execute(
                    """INSERT INTO game_lines(game_id,season,provider,spread,
                       over_under,formatted_spread,fetched_at)
                       VALUES(?,2026,?,?,48.0,'','2026-09-01')""",
                    (game_id, provider, spread))
            connection.execute(
                """INSERT INTO game_weather(game_id,forecast_generated_at,
                   kickoff_time,forecast_hour,temperature,weather_code,condition,
                   indoor,source,imported_at)
                   VALUES(1,'2026-09-04','2026-09-05','2026-09-05',
                          58.4,61,'Light rain',0,'test','2026-09-04')""")
            connection.commit()
        ContentRepository(self.path).initialize()
        StoryRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "CFB_DATABASE_PATH": self.path,
            "CFB_DISPLAY_TIMEZONE": "America/New_York",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def _body(self, day):
        return self.client.get(
            "/college-football/scoreboard/?date=" + day).get_data(as_text=True)

    def _sides(self, day):
        """The two team rows of the first card, tags stripped."""
        body = self._body(day)
        card = body[body.index('class="game-card"'):]
        return [" ".join(re.sub(r"<[^>]+>", " ", block).split())
                for block in re.findall(r'<div class="game-side.*?</div>', card,
                                        re.S)[:2]]

    def test_the_spread_rides_on_the_favourite_and_the_total_on_the_other(self):
        away, home = self._sides("2026-09-04")
        self.assertIn("Ohio State", away)
        self.assertIn("48", away)
        self.assertNotIn("-6.5", away)
        self.assertIn("Michigan", home)
        self.assertIn("-6.5", home)          # the average of -6 and -7
        self.assertNotIn("48", home)

    def test_the_total_is_unlabelled_and_unsigned(self):
        """The minus sign opposite it is the only label it needs."""
        away, _ = self._sides("2026-09-04")
        self.assertNotIn("O/U", away)
        self.assertNotIn("-48", away)
        self.assertIn('title="Market total"', self._body("2026-09-04"))

    def test_the_spread_says_which_books_agreed(self):
        self.assertIn('title="Consensus of 2 books"', self._body("2026-09-04"))

    def test_a_completed_game_shows_the_score_and_keeps_the_line(self):
        """The line used to come off the card at the final. It now stays: beside the engine's
        projection and the result it is the third number worth comparing."""
        body = self._body("2026-09-05")
        self.assertIn(">31<", body)
        self.assertIn("-3.5", body)
        self.assertIn('class="market', body)

    def test_the_line_never_appears_in_the_card_foot(self):
        """It belongs beside a team, not in the footnote with the venue."""
        body = self._body("2026-09-04")
        foot = re.search(r'<div class="game-card-foot">(.*?)</div>', body, re.S)
        self.assertNotIn("48", foot.group(1))
        self.assertNotIn("-6.5", foot.group(1))

    def test_elo_is_not_shown(self):
        self.assertNotIn('class="elo"', self._body("2026-09-04"))

    def test_the_forecast_becomes_a_glyph_and_a_temperature(self):
        body = self._body("2026-09-04")
        self.assertIn("🌧", body)             # light rain
        self.assertIn("58°", body)
        self.assertIn('title="Light rain, 58°F"', body)

    def test_a_game_with_no_forecast_carries_no_weather_markup(self):
        self.assertNotIn('class="weather"', self._body("2026-09-05"))


class FinishedMatchupTests(unittest.TestCase):
    """A finished game is not a preview of itself.

    The scoreboard sends every game to the same page, so on a Saturday evening
    a reader clicking a game that ended two hours ago was getting kickoff
    weather, a market line and "Full preview", with the result nowhere on it.
    """

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams((
            Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs",
                 "00274C", "FFCB05", (), ("Michigan",), None, None),
            Team(2, "Ohio State", "Buckeyes", "OSU", "Big Ten", None, "fbs",
                 "BB0000", "666666", (), ("Ohio State",), None, None),
        ))
        self.repository.replace_games(2026, [
            _game(1, "2026-09-05T16:00:00.000Z", 1, "Michigan", 2, "Ohio State",
                  completed=True, home_points=17, away_points=31),
            _game(2, "2026-09-12T16:00:00.000Z", 1, "Michigan", 2, "Ohio State"),
        ])
        ContentRepository(self.path).initialize()
        StoryRepository(self.path).initialize()
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "CFB_DATABASE_PATH": self.path,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def _page(self, game_id):
        return self.client.get(
            "/college-football/games/%d/" % game_id).get_data(as_text=True)

    def _scoreboard_block(self, body):
        start = body.index('<div class="scoreboard">')
        return body[start:body.index("<nav", start)]

    def test_a_finished_game_shows_both_scores(self):
        block = self._scoreboard_block(self._page(1))
        self.assertEqual(
            re.findall(r'<div class="matchup-score">(\d+)</div>', block),
            ["31", "17"])

    def test_the_page_says_final_rather_than_full_preview(self):
        body = self._page(1)
        # From the start of the page's own content: the site header now carries
        # a <nav> of its own, so "the first nav" is no longer where the content
        # begins. What is being asserted is that the result is at the top.
        content = body[body.index("<main"):]
        self.assertIn("Final", content[:content.index("<nav")])
        self.assertNotIn("Full preview", body)

    def test_the_winner_is_marked(self):
        """Two scores side by side should not read as equal."""
        block = self._scoreboard_block(self._page(1))
        sides = re.findall(r'<section class="team([^"]*)"', block)
        self.assertEqual(len(sides), 2)
        self.assertIn("is-winner", sides[0], "the away team won 31-17")
        self.assertNotIn("is-winner", sides[1])

    def test_an_upcoming_game_is_untouched(self):
        body = self._page(2)
        self.assertIn("Full preview", body)
        self.assertNotIn('<div class="matchup-score">', body)
        self.assertNotIn('<section class="team is-winner"', body)

    def test_a_game_marked_complete_with_no_points_is_not_called_final(self):
        """The flag arrives before the score sometimes."""
        self.repository.replace_games(2026, [
            _game(3, "2026-09-05T16:00:00.000Z", 1, "Michigan", 2, "Ohio State",
                  completed=True)])
        body = self._page(3)
        self.assertIn("Full preview", body)
        self.assertNotIn('<div class="matchup-score">', body)


class DashboardSlateTests(unittest.TestCase):
    """The strip on the front page, which is the site's only live surface.

    The dashboard showed no score at all once games had been played: a reader
    arriving on a Saturday evening got the preseason page. The strip is capped,
    so which games it keeps matters as much as that it has any.
    """

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.replace_teams(tuple(
            Team(index, "Team %d" % index, "Mascot", "T%d" % index, "Big Ten",
                 None, "fbs", "00274C", "FFCB05", (), ("Team %d" % index,),
                 None, None)
            for index in range(1, 41)))
        self.app = create_app({
            "TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
            "CFB_REPOSITORY": self.repository, "CFB_DEFAULT_SEASON": 2026,
            "CFB_DATABASE_PATH": self.path,
            "CFB_DISPLAY_TIMEZONE": "America/New_York",
        })

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def _slate(self, moment):
        with self.app.test_request_context():
            return _current_slate(self.repository, 2026, now=moment)

    def _saturday(self, count, *, hour=16):
        """`count` games, one every half hour from `hour` UTC."""
        games = []
        for index in range(count):
            minutes = index * 30
            start = datetime(2026, 9, 5, hour, tzinfo=timezone.utc) + timedelta(
                minutes=minutes)
            games.append(_game(index + 1,
                               start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                               1 + (index * 2) % 38, "Team %d" % (1 + (index * 2) % 38),
                               2 + (index * 2) % 38, "Team %d" % (2 + (index * 2) % 38)))
        self.repository.replace_games(2026, games)

    # -- which day ---------------------------------------------------------

    def test_today_is_shown_when_today_has_games(self):
        self._saturday(4)
        day, games = self._slate(datetime(2026, 9, 5, 18, tzinfo=timezone.utc))
        self.assertEqual(day, "2026-09-05")
        self.assertEqual(len(games), 4)

    def test_sunday_morning_shows_saturday_rather_than_next_weekend(self):
        """What a reader wants then is yesterday's results."""
        self._saturday(4)
        day, _ = self._slate(datetime(2026, 9, 6, 13, tzinfo=timezone.utc))
        self.assertEqual(day, "2026-09-05")

    def test_midweek_looks_forward_to_the_next_slate(self):
        self._saturday(4)
        day, _ = self._slate(datetime(2026, 9, 2, 14, tzinfo=timezone.utc))
        self.assertEqual(day, "2026-09-05")

    def test_no_schedule_at_all_yields_no_strip(self):
        self.assertEqual(self._slate(datetime(2026, 9, 5, 18, tzinfo=timezone.utc)),
                         (None, []))

    # -- which games -------------------------------------------------------

    def test_a_full_saturday_is_capped(self):
        self._saturday(40)
        _, games = self._slate(datetime(2026, 9, 5, 18, tzinfo=timezone.utc))
        self.assertEqual(len(games), SLATE_STRIP_GAMES)

    def test_the_cap_keeps_the_games_nearest_to_now(self):
        """Not the head of the day: at nine at night that is over."""
        self._saturday(40)
        _, evening = self._slate(datetime(2026, 9, 6, 0, tzinfo=timezone.utc))
        starts = [str(game["start_date"]) for game in evening]
        self.assertNotIn("2026-09-05T16:00:00.000Z", starts,
                         "the noon window finished hours ago")
        self.assertTrue(any(start >= "2026-09-05T23:00" for start in starts))

    def test_the_games_it_keeps_are_still_in_kickoff_order(self):
        self._saturday(40)
        _, games = self._slate(datetime(2026, 9, 5, 20, tzinfo=timezone.utc))
        starts = [str(game["start_date"]) for game in games]
        self.assertEqual(starts, sorted(starts))

    def test_a_short_slate_is_left_alone(self):
        self._saturday(SLATE_STRIP_GAMES)
        _, games = self._slate(datetime(2026, 9, 5, 18, tzinfo=timezone.utc))
        self.assertEqual(len(games), SLATE_STRIP_GAMES)

    # -- the page ----------------------------------------------------------

    def test_the_dashboard_carries_the_scores(self):
        ContentRepository(self.path).initialize()
        StoryRepository(self.path).initialize()
        self.repository.replace_games(2026, [
            _game(1, "2026-09-05T16:00:00.000Z", 1, "Team 1", 2, "Team 2",
                  completed=True, home_points=17, away_points=31)])
        body = self.app.test_client().get(
            "/college-football/").get_data(as_text=True)
        self.assertIn("slate-chip", body)
        self.assertIn(">31<", body)
        self.assertIn("slate-side is-winner", body)

    def test_the_strip_links_to_the_full_scoreboard(self):
        ContentRepository(self.path).initialize()
        StoryRepository(self.path).initialize()
        self._saturday(3)
        body = self.app.test_client().get(
            "/college-football/").get_data(as_text=True)
        self.assertIn("/college-football/scoreboard/?date=2026-09-05", body)


if __name__ == "__main__":
    unittest.main()
