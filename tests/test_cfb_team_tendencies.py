"""College football tendency, penalty and direction panels (cfb.situational_tendencies / penalties / direction_splits)."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from sports_aggregator.cfb import direction_splits, passing_plays, penalties, rushing_plays, situational_tendencies
from sports_aggregator.cfb.repository import CFBRepository, forget_initialized_schemas

NOW = "2026-01-01T00:00:00+00:00"
A_KEYS, B_KEYS = penalties._keys("Tulsa", "TLSA"), penalties._keys("Abilene Christian", None)


class ParsingTests(unittest.TestCase):
    def parse(self, text, yards=0.0, a=A_KEYS, b=B_KEYS):
        return penalties.parse_penalty(text, a, b, yards)

    def test_offense_flag_with_player_and_no_play(self):
        r = self.parse("PENALTY TULSA False Start (East,Cam) 5 yards from ACU33 to ACU38. NO PLAY.", -5)
        self.assertEqual((r["committed_by"], r["penalty_type"], r["yards"], r["player_name"], r["no_play"]),
                         ("offense", "False Start", 5.0, "Cam East", True))

    def test_token_beats_yardage_sign_when_a_gain_precedes_the_flag(self):
        r = self.parse("(10:08) rush middle for 7 yards PENALTY TULSA Holding (Lucas,Landen) 10 yards from X.", 7)
        self.assertEqual(r["committed_by"], "offense")          # net yardage is positive; the token is right

    def test_defense_flag_and_automatic_first_down(self):
        r = self.parse("PENALTY ACU Pass Interference (Malaska,Jocelyn) 15 yards from ACU29 to ACU14, 1ST DOWN. NO PLAY.", 15)
        self.assertEqual((r["committed_by"], r["auto_first_down"]), ("defense", 1))

    def test_team_first_format_has_no_yards_in_text(self):
        r = self.parse("Tulsa Penalty, Intentional Grounding (Kirk Francis) to the TLSA 19", -8)
        self.assertEqual((r["committed_by"], r["penalty_type"], r["yards"]), ("offense", "Intentional Grounding", 8.0))

    def test_unknown_token_falls_back_to_the_yardage_direction(self):
        self.assertEqual(self.parse("PENALTY ZZZ Holding 10 yards from A to B.", -10)["committed_by"], "offense")
        self.assertEqual(self.parse("PENALTY ZZZ Offside 5 yards from A to B.", 5)["committed_by"], "defense")

    def test_declined_and_offsetting_flags_are_dropped(self):
        self.assertIsNone(self.parse("PENALTY TULSA Holding 10 yards. Penalty declined."))
        self.assertIsNone(self.parse("PENALTY TULSA Holding and PENALTY ACU Holding, offsetting."))

    def test_type_and_player_normalisation(self):
        self.assertEqual(penalties.normalize_type("falsestart"), "False Start")
        self.assertEqual(penalties.normalize_type("Delay Of Game"), "Delay of Game")
        self.assertEqual(penalties.normalize_type("UNS: Unsportsmanlike Conduct"), "Unsportsmanlike Conduct")
        r = self.parse("PENALTY TULSA Holding (#74 S.Nomani) 10 yards from A to B.", -10)
        self.assertEqual(r["player_name"], "S.Nomani")           # jersey number stripped


class Fixture(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.path)
        forget_initialized_schemas()
        self.repository = CFBRepository(self.path)
        self.repository.initialize()
        passing_plays.initialize(self.repository)
        rushing_plays.initialize(self.repository)
        from sports_aggregator.cfb.play_detail import initialize as initialize_detail
        initialize_detail(self.repository)
        self.n = 0
        with closing(sqlite3.connect(self.path)) as c:
            for tid, school, abbr in ((1, "Tulsa", "TLSA"), (2, "Air Force", "AF")):
                c.execute("INSERT INTO teams(team_id,school,abbreviation,classification,logos_json,updated_at) "
                          "VALUES (?,?,?,'fbs','[]',?)", (tid, school, abbr, NOW))
            c.commit()

    def tearDown(self):
        forget_initialized_schemas()
        for path in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(path):
                os.unlink(path)

    def game(self, game_id, season=2026, kind="regular"):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO games(game_id,season,week,season_type,start_date,start_time_tbd,completed,"
                      "neutral_site,conference_game,home_team_id,home_team,away_team_id,away_team,updated_at) "
                      "VALUES (?,?,1,?,'2026-09-01',0,1,0,0,1,'Tulsa',2,'Air Force',?)", (game_id, season, kind, NOW))
            c.commit()

    def play(self, offense, defense, play_type, down=1, distance=10, to_goal=70, text="play", call=None,
             epa=0.1, success=1, game_id=1, season=2026, yards=0):
        self.n += 1
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO cfb_plays(play_id,game_id,season,week,offense,defense,home_team,away_team,"
                      "down,distance,yards_to_goal,yards_gained,scoring,play_type,play_text,raw_json,imported_at) "
                      "VALUES (?,?,?,1,?,?,?,?,?,?,?,?,0,?,?,'{}',?)",
                      (f"p{self.n}", game_id, season, offense, defense, offense, defense, down, distance, to_goal,
                       yards, play_type, text, NOW))
            c.execute("INSERT INTO cfb_play_epa(play_id,model_version,epa,possession_changed,scored_at) "
                      "VALUES (?,'ep-v2',?,0,?)", (f"p{self.n}", epa, NOW))
            c.execute("INSERT INTO cfb_play_metrics(play_id,metric_version,rush_pass,success,derived_at) "
                      "VALUES (?,'pbp-v1',?,?,?)", (f"p{self.n}", call, success, NOW))
            c.commit()
        return f"p{self.n}"


class SituationalTests(Fixture):
    def test_cells_league_rate_and_exclusions(self):
        self.game(1)
        self.game(2, kind="postseason")
        for _ in range(12):
            self.play("Tulsa", "Air Force", "Pass Reception", down=3, distance=8, call="pass")
        for _ in range(3):
            self.play("Tulsa", "Air Force", "Rush", down=3, distance=8, call="rush")
        self.play("Tulsa", "Air Force", "Rush", down=3, distance=8, call="rush", text="QB kneel")       # excluded
        self.play("Tulsa", "Air Force", "Penalty", down=3, distance=8, call="pass")                       # excluded
        self.play("Tulsa", "Air Force", "Pass Reception", down=3, distance=8, call="pass", game_id=2)     # postseason
        self.play("Air Force", "Tulsa", "Rush", down=3, distance=8, call="rush")
        profile = situational_tendencies.team_tendencies(self.repository, 2026, "Tulsa")
        cell = profile["rows"][2]["cells"][2]                       # 3rd & 7+
        self.assertEqual((cell["label"], cell["plays"], cell["status"], cell["call"]), ("3rd & 7+", 15, "ok", "PASS"))
        self.assertAlmostEqual(cell["pass_rate"], 12 / 15)
        self.assertAlmostEqual(cell["league_pass_rate"], 12 / 16)   # Air Force's one run is in the league pool
        self.assertEqual(profile["plays"], 15)

    def test_goal_to_go_first_down_and_small_samples(self):
        self.game(1)
        for _ in range(3):
            self.play("Tulsa", "Air Force", "Rush", down=1, distance=4, to_goal=4, call="rush")
        cell = situational_tendencies.team_tendencies(self.repository, 2026, "Tulsa")["rows"][0]["cells"][0]
        self.assertEqual((cell["label"], cell["plays"], cell["status"]), ("1st & goal", 3, "small"))

    def test_early_season_falls_back_to_last_season_with_a_note(self):
        self.game(1, season=2025)
        self.game(2, season=2026)
        for _ in range(situational_tendencies.MIN_SEASON_PLAYS):
            self.play("Tulsa", "Air Force", "Rush", call="rush", season=2025, game_id=1)
        self.play("Tulsa", "Air Force", "Rush", call="rush", season=2026, game_id=2)
        view = situational_tendencies.team_view(self.repository, 2026, "Tulsa")
        self.assertEqual(view["profile"]["season"], 2025)
        self.assertIn("2025 baseline", view["note"])


class PenaltyTests(Fixture):
    def test_committed_drawn_net_yards_ranking_pool_and_fcs_opponent(self):
        self.game(1)
        self.play("Tulsa", "Air Force", "Penalty", text="PENALTY TLSA False Start (East,Cam) 5 yards from A to B. NO PLAY.", yards=-5, epa=None)
        self.play("Tulsa", "Air Force", "Penalty", text="PENALTY AF Pass Interference (Doe,Jo) 15 yards from A to B, 1ST DOWN. NO PLAY.", yards=15, epa=None)
        self.play("Tulsa", "Air Force", "Penalty", text="PENALTY AF Holding 10 yards. Penalty declined.", yards=0, epa=None)
        self.play("Air Force", "Abilene Christian", "Penalty", text="PENALTY ACU Offside 5 yards from A to B. NO PLAY.", yards=5, epa=None)
        for offense in ("Tulsa", "Air Force"):
            self.play(offense, "x", "Rush", call="rush")
        league = penalties.league_penalties(self.repository, 2026)
        self.assertEqual(sorted(league), ["Air Force", "Tulsa"])            # FCS opponent is never ranked
        tulsa, air_force = league["Tulsa"], league["Air Force"]
        self.assertEqual((tulsa["committed"]["flags"], tulsa["committed"]["yards"], tulsa["committed"]["presnap"]), (1, 5, 1))
        self.assertEqual((tulsa["drawn"]["flags"], tulsa["drawn"]["yards"], tulsa["drawn"]["auto_first_downs"]), (1, 15, 1))
        self.assertEqual(tulsa["net_yards"], 10)
        # Air Force committed the one enforced pass interference (its declined holding is gone) and drew two flags:
        # Tulsa's false start and the FCS opponent's offside, which still counts for the FBS side.
        self.assertEqual((air_force["committed"]["flags"], air_force["drawn"]["flags"]), (1, 2))
        self.assertEqual(air_force["drawn"]["yards"], 10)
        self.assertEqual(tulsa["of"], 2)
        view = penalties.team_view(self.repository, 2026, "Tulsa", minimum_games=1)
        self.assertNotIn("epa", [row["key"] for row in view["committed"] + view["drawn"]])
        self.assertEqual(tulsa["committed"]["offenders"][0]["player_name"], "Cam East")


class DirectionTests(Fixture):
    def throw(self, offense, defense, direction, depth="short", air=5, yards=7, outcome="completion", epa=0.2):
        self.n += 1
        pid = f"t{self.n}"
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO cfbd_passing_plays(play_id,game_id,season,week,offense,defense,pass_direction,"
                      "pass_depth,air_yards,total_yards,outcome,imported_at) VALUES (?,1,2026,1,?,?,?,?,?,?,?,?)",
                      (pid, offense, defense, direction, depth, air, yards, outcome, NOW))
            c.execute("INSERT INTO cfb_play_epa(play_id,model_version,epa,possession_changed,scored_at) VALUES (?,'ep-v2',?,0,?)",
                      (pid, epa, NOW))
            c.commit()

    def carry(self, offense, defense, direction, yards=4, epa=0.0, success=1):
        self.n += 1
        pid = f"r{self.n}"
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO cfbd_rushing_plays(play_id,game_id,season,week,offense,defense,rush_direction,"
                      "rushing_yards,is_touchdown,is_sack,is_kneel,imported_at) VALUES (?,1,2026,1,?,?,?,?,0,0,0,?)",
                      (pid, offense, defense, direction, yards, NOW))
            c.execute("INSERT INTO cfb_play_epa(play_id,model_version,epa,possession_changed,scored_at) VALUES (?,'ep-v2',?,0,?)",
                      (pid, epa, NOW))
            c.execute("INSERT INTO cfb_play_metrics(play_id,metric_version,rush_pass,success,derived_at) VALUES (?,'pbp-v1','rush',?,?)",
                      (pid, success, NOW))
            c.commit()

    def test_offense_and_defense_views_ratio_and_depth_grid(self):
        self.game(1)
        for _ in range(12):
            self.throw("Tulsa", "Air Force", "left")
        for _ in range(6):
            self.throw("Tulsa", "Air Force", "right", depth="deep", air=22, yards=0, outcome="incompletion")
        for _ in range(6):
            self.throw("Tulsa", "Air Force", "middle")
        side = direction_splits.team_side(self.repository, 2026, "Tulsa", "offense")["passes"]
        self.assertTrue(side["has_data"])
        self.assertEqual(side["total"], 24)
        self.assertEqual(side["ratio"], "2.00 : 1")
        left = side["rows"][0]
        self.assertEqual((left["bucket"], left["n"], left["completion_rate"]), ("Left", 12, 1.0))
        self.assertAlmostEqual(left["adot"], 5.0)
        self.assertEqual([c["n"] for c in side["matrix"][1]["cells"]], [0, 0, 6])          # deep row: right only
        faced = direction_splits.team_side(self.repository, 2026, "Air Force", "defense")["passes"]
        self.assertEqual(faced["total"], 24)                                                # same plays, defense view

    def test_thin_samples_are_not_drawn_and_view_falls_back(self):
        self.game(1)
        for _ in range(5):
            self.carry("Tulsa", "Air Force", "middle")
        self.assertFalse(direction_splits.team_side(self.repository, 2026, "Tulsa", "offense")["runs"]["has_data"])
        self.assertIsNone(direction_splits.team_view(self.repository, 2026, "Tulsa"))      # nothing usable anywhere


if __name__ == "__main__":
    unittest.main()
