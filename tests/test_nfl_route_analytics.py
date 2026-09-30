import tempfile
import unittest
from pathlib import Path

from sports_aggregator.nfl import route_analytics as ra
from sports_aggregator.nfl.plays import (
    build_play_rows, field_zone, hash_mark, package_snap_rows,
)
from sports_aggregator.nfl.repository import NFLRepository

GAME = "2025_01_DAL_PHI"
OFF_11 = "1 C, 2 G, 1 QB, 1 RB, 2 T, 1 TE, 3 WR"
OFF_12 = "1 C, 2 G, 1 QB, 1 RB, 2 T, 2 TE, 2 WR"
DEF_NICKEL = "4 DL, 2 LB, 5 DB"
ON_FIELD = ["QB1", "R1", "R2", "R3", "RB1", "TE1", "T1", "T2", "G1", "G2", "C1"]
POSITIONS = ["QB", "WR", "WR", "WR", "RB", "TE", "T", "T", "G", "G", "C"]


def scenario():
    """(pbp, participation, ftn) for six DAL plays; see each comment for what it encodes."""
    def play(play_id, yardline, **kw):
        base = {"game_id": GAME, "play_id": play_id, "season": 2025, "week": 1, "qtr": 1, "posteam": "DAL",
                "defteam": "PHI", "home_team": "PHI", "away_team": "DAL", "down": 1, "ydstogo": 10,
                "yardline_100": yardline, "pass": 1, "rush": 0, "epa": 0.5, "yards_gained": 0,
                "passer_player_id": "QB1", "air_yards": 5}
        base.update(kw)
        return base

    pbp = [
        play(1, 70, receiver_player_id="R1", complete_pass=1, yards_gained=8, epa=0.2),            # hitch, boundary
        play(2, 70, receiver_player_id="R1", complete_pass=0, epa=-0.4),                          # hitch, boundary
        play(3, 60, receiver_player_id="R1", complete_pass=1, yards_gained=40, epa=1.5,
             pass_touchdown=1, air_yards=30),                                                     # go, boundary (R hash)
        play(4, 30, receiver_player_id="R2", complete_pass=1, yards_gained=5, epa=0.3),            # slant, FIELD side
        play(5, 15, receiver_player_id="R1", complete_pass=0, epa=-0.6, passer_player_id="QB2"),   # go from the red zone
        play(6, 55, **{"pass": 0, "rush": 1}, rusher_player_id="RB1", run_location="left", epa=0.1,
             yards_gained=4, passer_player_id=None),                                               # run to the boundary
    ]
    pbp[0]["pass_location"] = "left"
    pbp[1]["pass_location"] = "left"
    pbp[2]["pass_location"] = "right"
    pbp[3]["pass_location"] = "right"
    pbp[4]["pass_location"] = "left"
    routes = {1: "HITCH/CURL", 2: "HITCH/CURL", 3: "GO", 4: "SLANT", 5: "GO"}
    hashes = {1: "L", 2: "L", 3: "R", 4: "L", 5: "R", 6: "L"}
    personnel = {1: OFF_11, 2: OFF_11, 3: OFF_11, 4: OFF_12, 5: OFF_11, 6: OFF_11}
    participation = []
    for play_id in range(1, 7):
        on_field = ON_FIELD if play_id != 6 else [p for p in ON_FIELD if p != "R1"]
        positions = POSITIONS if play_id != 6 else [q for p, q in zip(ON_FIELD, POSITIONS) if p != "R1"]
        participation.append({
            "nflverse_game_id": GAME, "play_id": play_id, "offense_personnel": personnel[play_id],
            "defense_personnel": DEF_NICKEL, "route": routes.get(play_id, ""),
            "defense_coverage_type": "COVER_3", "defense_man_zone_type": "ZONE_COVERAGE",
            "offense_players": ";".join(on_field), "offense_positions": ";".join(positions),
            "defense_players": "D1;D2;D3;D4;D5;D6;D7;D8;D9;D10;D11",
            "defense_positions": "DT;DT;DE;DE;LB;LB;CB;CB;CB;FS;SS"})
    ftn = [{"nflverse_game_id": GAME, "nflverse_play_id": play_id, "starting_hash": hashes[play_id]}
           for play_id in range(1, 7)]
    return pbp, participation, ftn


class Base(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.repository = NFLRepository(Path(self._directory.name) / "nfl.sqlite3")
        rows = build_play_rows(*scenario())
        self.repository.replace_plays(2025, rows)
        self.repository.replace_package_snaps(2025, package_snap_rows(rows))

    def tearDown(self):
        self._directory.cleanup()


class ZoneAndHashTests(unittest.TestCase):
    def test_field_zones(self):
        self.assertEqual(field_zone(90), "Backed up")
        self.assertEqual(field_zone(80), "Backed up")
        self.assertEqual(field_zone(65), "Neutral")
        self.assertEqual(field_zone(50), "Plus")
        self.assertEqual(field_zone(21), "Plus")
        self.assertEqual(field_zone(20), "Red zone")
        self.assertEqual(field_zone(15, ydstogo=10), "Red zone")
        self.assertEqual(field_zone(8, goal_to_go=1), "Goal to go")
        self.assertEqual(field_zone(8, ydstogo=8), "Goal to go")
        self.assertIsNone(field_zone(None))

    def test_hash_marks_only_accept_l_m_r(self):
        self.assertEqual((hash_mark("L"), hash_mark("M"), hash_mark("R")), ("L", "M", "R"))
        self.assertIsNone(hash_mark("0"))
        self.assertIsNone(hash_mark(None))

    def test_side_bucket_is_relative_to_the_hash(self):
        self.assertEqual(ra.side_bucket("L", "left"), "Boundary")
        self.assertEqual(ra.side_bucket("L", "right"), "Field")
        self.assertEqual(ra.side_bucket("R", "right"), "Boundary")
        self.assertEqual(ra.side_bucket("R", "left"), "Field")
        self.assertEqual(ra.side_bucket("L", "middle"), "Middle")
        self.assertEqual(ra.side_bucket("M", "left"), "Mid-hash left")
        self.assertIsNone(ra.side_bucket(None, "left"))
        self.assertIsNone(ra.side_bucket("L", None))


class RouteTreeTests(Base):
    def test_receiver_tree_counts_rates_and_depth_families(self):
        tree = ra.route_tree(self.repository, 2025, "receiver", "R1")
        rows = {row["route"]: row for row in tree["rows"]}
        self.assertEqual(set(rows), {"HITCH/CURL", "GO"})
        hitch = rows["HITCH/CURL"]
        self.assertEqual((hitch["targets"], hitch["receptions"], hitch["yards"]), (2, 1, 8))
        self.assertAlmostEqual(hitch["catch_rate"], 0.5)
        self.assertAlmostEqual(hitch["epa_per_target"], -0.1)
        go = rows["GO"]
        self.assertEqual((go["targets"], go["receptions"], go["yards"], go["touchdowns"]), (2, 1, 40, 1))
        self.assertAlmostEqual(go["adot"], 17.5)
        self.assertEqual(tree["total"]["targets"], 4)
        self.assertAlmostEqual(sum(row["share"] for row in tree["rows"]), 1.0)
        families = {family["label"]: family["targets"] for family in tree["families"]}
        self.assertEqual(families, {"Short": 2, "Deep": 2})

    def test_league_comparison_uses_every_target_on_the_route(self):
        tree = ra.route_tree(self.repository, 2025, "receiver", "R1")
        slant_free = {row["route"]: row for row in tree["rows"]}
        go = slant_free["GO"]
        # league GO: plays 3 and 5 are both R1's, so the league rate equals his own
        self.assertAlmostEqual(go["league_catch_rate"], 0.5)
        self.assertAlmostEqual(go["epa_vs_league"], 0.0)

    def test_passer_and_team_scopes(self):
        self.assertEqual(ra.route_tree(self.repository, 2025, "passer", "QB1")["total"]["targets"], 4)
        self.assertEqual(ra.route_tree(self.repository, 2025, "passer", "QB2")["total"]["targets"], 1)
        self.assertEqual(ra.route_tree(self.repository, 2025, "offense", "DAL")["total"]["targets"], 5)
        self.assertEqual(ra.route_tree(self.repository, 2025, "defense", "PHI")["noun"], "Targets allowed")

    def test_unknown_player_has_no_data(self):
        self.assertFalse(ra.route_tree(self.repository, 2025, "receiver", "NOBODY")["has_data"])


class PairTests(Base):
    def test_passer_targets_ordered_by_volume_with_top_routes(self):
        result = ra.pairs(self.repository, None, 2025, "passer", "QB1")
        self.assertEqual([row["player_id"] for row in result["rows"]], ["R1", "R2"])
        first = result["rows"][0]
        self.assertEqual((first["targets"], first["receptions"], first["yards"]), (3, 2, 48))
        self.assertEqual(first["top_routes"][0], ("Hitch / Curl", 2))
        self.assertAlmostEqual(first["share"], 0.75)

    def test_receiver_sees_their_passers(self):
        result = ra.pairs(self.repository, None, 2025, "receiver", "R1")
        self.assertEqual({row["player_id"]: row["targets"] for row in result["rows"]}, {"QB1": 3, "QB2": 1})

    def test_team_pairs(self):
        result = ra.team_pairs(self.repository, 2025, "DAL")
        self.assertEqual((result["rows"][0]["passer_id"], result["rows"][0]["receiver_id"],
                          result["rows"][0]["targets"]), ("QB1", "R1", 3))


class HashProfileTests(Base):
    def test_small_samples_withhold_the_ratio_but_keep_the_counts(self):
        passes = ra.hash_profile(self.repository, 2025, "passer", "QB1")["passes"]
        self.assertEqual((passes["boundary"], passes["field"]), (3, 1))
        self.assertIsNone(passes["field_to_boundary"])       # four plays is not enough to quote a ratio
        self.assertIsNone(passes["field_share"])

    def test_passer_field_boundary_split(self):
        original = ra.MIN_RATIO_SAMPLE
        ra.MIN_RATIO_SAMPLE = 1
        try:
            profile = ra.hash_profile(self.repository, 2025, "passer", "QB1")
        finally:
            ra.MIN_RATIO_SAMPLE = original
        passes = profile["passes"]
        self.assertEqual((passes["boundary"], passes["field"]), (3, 1))
        self.assertAlmostEqual(passes["field_to_boundary"], 1 / 3)
        self.assertAlmostEqual(passes["field_share"], 0.25)
        rows = {row["bucket"]: row for row in passes["rows"]}
        self.assertEqual(rows["Boundary"]["n"], 3)
        self.assertAlmostEqual(rows["Boundary"]["completion_rate"], 2 / 3)
        self.assertAlmostEqual(rows["Field"]["epa_per"], 0.3)

    def test_team_runs_and_hash_share(self):
        profile = ra.hash_profile(self.repository, 2025, "offense", "DAL")
        self.assertEqual(profile["runs"]["boundary"], 1)
        self.assertAlmostEqual(profile["hash_share"]["L"], 4 / 6)
        self.assertAlmostEqual(profile["hash_share"]["R"], 2 / 6)
        self.assertEqual(profile["directions"]["pass"], {"left": 3, "right": 2})

    def test_matrix_counts_each_hash_direction_cell(self):
        matrix = ra.hash_profile(self.repository, 2025, "passer", "QB1")["passes"]["matrix"]
        cells = {(cell["hash"], cell["loc"]): cell["n"] for cell in matrix}
        self.assertEqual(cells, {("L", "left"): 2, ("R", "right"): 1, ("L", "right"): 1})


class PackageTests(Base):
    def test_player_on_field_rate_by_package_and_zone(self):
        usage = ra.player_packages(self.repository, 2025, "R1")
        self.assertEqual((usage["team"], usage["side"], usage["snaps"]), ("DAL", "off", 5))
        self.assertEqual(usage["team_snaps"], 6)
        self.assertAlmostEqual(usage["on_field_rate"], 5 / 6)
        packages = {item["package"]: item for item in usage["packages"]}
        self.assertEqual(set(packages), {"11", "12"})
        eleven = packages["11"]
        self.assertEqual((eleven["snaps"], eleven["team_snaps"]), (4, 5))      # R1 sat out the run
        self.assertAlmostEqual(eleven["on_field_rate"], 0.8)
        self.assertAlmostEqual(eleven["pass_rate"], 1.0)
        self.assertEqual(eleven["zones"]["Neutral"]["snaps"], 3)
        self.assertEqual(eleven["zones"]["Neutral"]["team"], 4)
        self.assertEqual(eleven["zones"]["Red zone"]["snaps"], 1)
        self.assertAlmostEqual(packages["12"]["on_field_rate"], 1.0)
        zones = {row["zone"]: row for row in usage["zones"]}
        self.assertEqual(zones["Plus"]["snaps"], 1)
        self.assertEqual(zones["Backed up"]["snaps"], 0)

    def test_defender_is_reported_on_the_defensive_side(self):
        usage = ra.player_packages(self.repository, 2025, "D5")
        self.assertEqual((usage["side"], usage["team"]), ("def", "PHI"))
        self.assertEqual(usage["packages"][0]["package"], "Nickel")
        self.assertEqual(usage["packages"][0]["on_field_rate"], 1.0)

    def test_team_package_table_has_shares_efficiency_and_who_plays(self):
        table = ra.team_packages(self.repository, 2025, "DAL")
        by_package = {row["package"]: row for row in table["rows"]}
        self.assertEqual(table["plays"], 6)
        self.assertAlmostEqual(by_package["11"]["share"], 5 / 6)
        self.assertAlmostEqual(by_package["11"]["pass_rate"], 4 / 5)
        self.assertEqual(by_package["12"]["plays"], 1)
        wr_names = [item["player_id"] for item in by_package["11"]["who"]["WR"]]
        self.assertEqual(set(wr_names), {"R1", "R2", "R3"})
        rates = {item["player_id"]: item["rate"] for item in by_package["11"]["who"]["WR"]}
        self.assertAlmostEqual(rates["R2"], 1.0)        # on the field for all five 11 personnel plays
        self.assertAlmostEqual(rates["R1"], 0.8)        # sat out the run

    def test_unknown_player_has_no_package_data(self):
        self.assertFalse(ra.player_packages(self.repository, 2025, "NOBODY")["has_data"])


class DataSeasonTests(Base):
    def test_coverage_floor_and_baseline_fallback(self):
        self.assertIsNone(ra.data_season(self.repository, 2025, "route"))        # six plays is not coverage
        original = ra.MIN_COVERAGE
        ra.MIN_COVERAGE = 3
        try:
            current = ra.data_season(self.repository, 2025, "route")
            self.assertEqual((current["season"], current["baseline"]), (2025, False))
            baseline = ra.data_season(self.repository, 2026, "route")
            self.assertEqual((baseline["season"], baseline["baseline"]), (2025, True))
            self.assertIsNone(ra.data_season(self.repository, 2029, "route"))
        finally:
            ra.MIN_COVERAGE = original


if __name__ == "__main__":
    unittest.main()
