import json

import pytest

from sports_aggregator.cfb.expected_points_event import (
    export_model_artifact,
    initialize,
    install_model_artifact,
)
from sports_aggregator.cfb.ep_v2_rebuild_cli import _pbp_coverage
from sports_aggregator.cfb.repository import CFBRepository


def _repository(tmp_path):
    repository = CFBRepository(tmp_path / "cfb.sqlite3")
    initialize(repository)
    with repository._connect() as connection:
        connection.executemany("""
          INSERT INTO cfb_expected_points_state
          (model_version,down_bucket,distance_bucket,field_bucket,time_bucket,
           samples,expected_points,fitted_at)
          VALUES('ep-v2',?,?,?,?,?,?,?)
        """, [
            (1, 3, 4, 5, 120, 1.25, "2026-01-01T00:00:00+00:00"),
            (3, 2, 1, 2, 80, -0.75, "2026-01-01T00:00:00+00:00"),
        ])
        connection.execute("CREATE TABLE artifact_sentinel(value TEXT)")
        connection.execute("INSERT INTO artifact_sentinel VALUES('preserve me')")
        connection.execute("""
          INSERT INTO games
          (game_id,season,week,season_type,start_date,start_time_tbd,completed,
           neutral_site,conference_game,home_team_id,home_team,away_team_id,
           away_team,updated_at)
          VALUES(1,2025,1,'regular','2025-08-30',0,1,0,0,1,'Home',2,'Away','now')
        """)
        connection.execute("""
          INSERT INTO cfb_plays
          (play_id,game_id,offense,defense,scoring,season,week,raw_json,imported_at)
          VALUES('play-1',1,'Home','Away',0,2025,1,'{}','now')
        """)
        connection.commit()
    return repository


def test_model_artifact_round_trip_replaces_only_model_state(tmp_path):
    repository = _repository(tmp_path)
    artifact = tmp_path / "ep-v2.json"

    exported = export_model_artifact(
        repository, artifact, from_season=2015, to_season=2025, min_cell=10)
    pbp_before = _pbp_coverage(repository)
    with repository._connect() as connection:
        connection.execute(
            "UPDATE cfb_expected_points_state SET expected_points=99 WHERE model_version='ep-v2'")
        connection.commit()

    installed = install_model_artifact(repository, artifact)
    pbp_after = _pbp_coverage(repository)

    with repository._connect() as connection:
        values = [row[0] for row in connection.execute("""
          SELECT expected_points FROM cfb_expected_points_state
          WHERE model_version='ep-v2' ORDER BY down_bucket
        """)]
        sentinel = connection.execute("SELECT value FROM artifact_sentinel").fetchone()[0]
        plays = connection.execute("SELECT COUNT(*) FROM cfb_plays").fetchone()[0]
    assert values == [1.25, -0.75]
    assert sentinel == "preserve me"
    assert plays == 1
    assert pbp_before == pbp_after == {
        "plays": 1, "games": 1, "from_season": 2025, "to_season": 2025,
    }
    assert installed["rows_sha256"] == exported["rows_sha256"]
    assert installed["training"] == {
        "from_season": 2015, "to_season": 2025, "min_cell": 10, "samples": 200,
    }


def test_model_artifact_rejects_checksum_mismatch_without_replacing_model(tmp_path):
    repository = _repository(tmp_path)
    artifact = tmp_path / "ep-v2.json"
    export_model_artifact(repository, artifact)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["rows"][0]["expected_points"] = 999
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum"):
        install_model_artifact(repository, artifact)

    with repository._connect() as connection:
        value = connection.execute("""
          SELECT expected_points FROM cfb_expected_points_state
          WHERE model_version='ep-v2' AND down_bucket=1
        """).fetchone()[0]
    assert value == 1.25
