"""CORE ratings stored for a finished season are the season-FINAL snapshot (`postseason`, week 1); a game in that
season must never read it as a pregame input."""
import sqlite3
from contextlib import closing

from sports_aggregator.cfb import xpoints
from sports_aggregator.cfb.repository import CFBRepository


def _core(repository, rows):
    repository.initialize()
    with closing(repository._connect()) as connection:
        connection.executemany(
            "INSERT INTO core_ratings (season,through_season_type,through_week,team,conference,overall,"
            "offense,defense,offense_plays,defense_plays,model_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(season, kind, week, team, "X", overall, 0, 0, 0, 0, "core-v1")
             for season, kind, week, team, overall in rows])
        connection.commit()


def _prior_core_rows(repository):
    """The same selection xpoints.build_dataset makes."""
    with repository._reader() as connection:
        out = {}
        for row in connection.execute(
                "SELECT season,through_week,team,overall FROM core_ratings WHERE through_season_type='regular'"):
            out.setdefault((row["season"], row["team"]), []).append(dict(row))
    return out


def test_a_postseason_snapshot_is_never_a_prior_rating(tmp_path):
    repository = CFBRepository(tmp_path / "cfb.sqlite3")
    _core(repository, [
        (2024, "postseason", 1, "A", 40.0),     # season-final: knows every 2024 result
        (2026, "regular", 5, "A", 12.0),        # a genuine in-season snapshot
    ])
    prior = _prior_core_rows(repository)
    assert (2024, "A") not in prior
    assert prior[(2026, "A")][0]["overall"] == 12.0


def test_the_training_builder_selects_regular_snapshots_only():
    import inspect
    source = inspect.getsource(xpoints.build_dataset)
    assert "through_season_type='regular'" in source


def test_the_pregame_quality_blend_leaves_core_out():
    import inspect
    from sports_aggregator.cfb import game_projection
    source = inspect.getsource(game_projection.matchup_quality_snapshot)
    assert 'BLENDED = ("elo", "fpi", "vegas")' in source
    assert "through_season_type='regular'" in source
