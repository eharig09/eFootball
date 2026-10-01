import pandas as pd

from sports_aggregator.nfl.context_ablation import pregame_snapshots, team_game_context


def _pbp():
    base = dict(season_type="REG", week=1, game_id="2020_01_AAA_BBB", epa=0.0, interception=0,
                fumble=0, fumble_lost=0, penalty_team=None, penalty_yards=None, sack=0,
                qb_dropback=0, touchdown=0, td_team=None, drive=1, yardline_100=75.0)
    rows = [
        dict(base, posteam="AAA", defteam="BBB", play_type="pass", interception=1, qb_dropback=1),
        dict(base, posteam="AAA", defteam="BBB", play_type="run", drive=2, yardline_100=60.0),
        dict(base, posteam="AAA", defteam="BBB", play_type="pass", drive=2, touchdown=1,
             td_team="BBB", qb_dropback=1, interception=1),  # pick-six
        dict(base, posteam="BBB", defteam="AAA", play_type="punt", epa=-1.5),
        dict(base, posteam="BBB", defteam="AAA", play_type="pass", penalty_team="BBB",
             penalty_yards=10.0, qb_dropback=1, sack=1),
    ]
    return pd.DataFrame(rows)


def test_team_game_context_counts():
    ctx = {r["team"]: r for r in team_game_context(_pbp())}
    a, b = ctx["AAA"], ctx["BBB"]
    assert a["giveaways"] == 2 and b["takeaways"] == 2
    assert b["nonoff_tds"] == 1 and a["nonoff_tds"] == 0
    assert a["net_st_epa"] == 1.5 and b["net_st_epa"] == -1.5  # punt by BBB cost BBB
    assert b["penalty_yards"] == 10.0 and b["sacks_taken"] == 1
    assert a["drives"] == 2 and a["start_yardline_sum"] == 75.0 + 60.0


def test_snapshot_is_pregame_and_week_batched():
    r1 = dict(season=2020, week=1, game_id="g1", team="AAA", **{k: 1.0 for k in (
        "giveaways", "off_plays", "takeaways", "def_plays", "fumbles_lost", "fumbles",
        "sacks_taken", "dropbacks", "penalty_yards", "games", "net_st_epa", "nonoff_tds",
        "start_yardline_sum", "drives")})
    r2 = dict(r1, week=2, game_id="g2")
    snaps = pregame_snapshots([r1, r2])
    assert ("g1", "AAA") not in snaps  # nothing before week 1
    assert ("g2", "AAA") in snaps
