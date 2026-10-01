import json
import sqlite3
from pathlib import Path

from scripts.build_nfl_render_seed import FOOTBALL_LAB_FILTERS, build
from sports_aggregator.nfl import production_seed
from sports_aggregator.nfl.repository import NFLRepository


def _source(path: Path, seasons) -> Path:
    NFLRepository(path).initialize()
    with sqlite3.connect(path) as c:
        for season in seasons:
            c.execute("INSERT INTO game_team_efficiency (season,week,game_id,team,opponent_team,plays,total_epa,"
                      "successful_plays,pass_plays,pass_epa,rush_plays,rush_epa,early_down_plays,early_down_epa,"
                      "explosive_plays) VALUES (?,?,?,?,?,60,1.0,30,35,1.0,25,0.0,30,0.5,5)",
                      (season, 1, f"{season}_01_A_B", "A", "B"))
    return path


def test_history_topup_detects_restores_and_never_overwrites(tmp_path):
    archive = tmp_path / "history.sqlite3.gz"
    build(_source(tmp_path / "source.sqlite3", range(2012, 2024)), archive, FOOTBALL_LAB_FILTERS)

    target = tmp_path / "prod.sqlite3"
    repo = NFLRepository(target)
    repo.initialize()
    assert production_seed.needs_history(repo, 2026, archive, min_weather_games=0) is True
    assert production_seed.needs_history(repo, 2026, tmp_path / "missing.gz") is False  # nothing to supply it

    # A newer row already on disk must survive the merge.
    with sqlite3.connect(target) as c:
        c.execute("INSERT INTO game_team_efficiency (season,week,game_id,team,opponent_team,plays,total_epa,"
                  "successful_plays,pass_plays,pass_epa,rush_plays,rush_epa,early_down_plays,early_down_epa,"
                  "explosive_plays) VALUES (2023,1,'2023_01_A_B','A','B',999,9.0,1,1,1,1,1,1,1,1)")
    result = production_seed.restore_football_lab_history(target, archive)
    assert result["rows"] == 11  # 12 archive rows minus the pre-existing 2023 one
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT plays FROM game_team_efficiency WHERE season=2023").fetchone()[0] == 999
    assert production_seed.needs_history(repo, 2026, archive, min_weather_games=0) is False


def test_topup_launch_is_throttled(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(production_seed.subprocess, "Popen", lambda *a, **k: launched.append(a[0]))
    db = tmp_path / "prod.sqlite3"
    assert production_seed._launch_history_topup(db, 2026, 3600) is True
    assert production_seed._launch_history_topup(db, 2026, 3600) is False  # inside the retry window
    assert launched and launched[0][-1] == "--history-only"
    assert json.loads((tmp_path / production_seed.HISTORY_STATE_NAME).read_text())["status"] == "launching"


def test_maybe_launch_tops_up_a_seeded_disk_without_a_full_seed(tmp_path, monkeypatch):
    monkeypatch.delenv("NFL_AUTO_SEED_CHILD", raising=False)
    monkeypatch.setattr(production_seed, "needs_seed", lambda *a, **k: False)
    monkeypatch.setattr(production_seed, "needs_history", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(production_seed, "_launch_history_topup", lambda *a: calls.append(a) or True)
    assert production_seed.maybe_launch(database_path=tmp_path / "prod.sqlite3", season=2026) is True
    assert calls and not (tmp_path / production_seed.STATE_NAME).exists()  # no full-seed state written


def test_missing_observed_weather_also_triggers_a_top_up(tmp_path):
    archive = tmp_path / "history.sqlite3.gz"
    build(_source(tmp_path / "source.sqlite3", range(2012, 2024)), archive, FOOTBALL_LAB_FILTERS)
    repo = NFLRepository(tmp_path / "prod.sqlite3")
    repo.initialize()
    production_seed.restore_football_lab_history(tmp_path / "prod.sqlite3", archive)
    assert production_seed.needs_history(repo, 2026, archive, min_weather_games=0) is False
    assert production_seed.needs_history(repo, 2026, archive, min_weather_games=5) is True  # no weather rows
