import sqlite3

from sports_aggregator.nfl.nonlinear_ablation import add_context


class _Repo:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE games (game_id TEXT, home_rest INT, away_rest INT, division_game INT,"
                        " roof TEXT, temperature REAL, wind REAL, spread_line REAL, total_line REAL)")
        self.db.executemany("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)", [
            ("dome", 7, 6, 1, "dome", None, None, -3.0, 47.0),
            ("cold", 6, 7, 0, "outdoors", 30.0, 15.0, 2.5, 41.0),
            ("mild", 7, 7, 0, "outdoors", 72.0, 4.0, 0.0, 44.0),
            ("unknown", 7, 7, 0, "outdoors", None, None, 1.0, 45.0),
        ])

    def _connect(self):
        class _Conn:
            def __init__(s, db): s.db = db
            def execute(s, *a): return s.db.execute(*a)
            def close(s): pass
        return _Conn(self.db)


def test_context_encoding_and_unknown_weather_is_excluded():
    rows = [{"game_id": g} for g in ("dome", "cold", "mild", "unknown", "missing")]
    add_context(rows, _Repo())
    by = {r["game_id"]: r for r in rows}
    assert by["dome"]["ctx_dome"] == 1.0 and by["dome"]["ctx_wind"] == 0.0 and by["dome"]["ctx_temp_below_50"] == 0.0
    assert by["dome"]["ctx_rest_diff"] == 1.0 and by["dome"]["ctx_division"] == 1.0
    assert by["cold"]["ctx_wind"] == 15.0 and by["cold"]["ctx_temp_below_50"] == 20.0 and by["cold"]["ctx_rest_diff"] == -1.0
    assert by["mild"]["ctx_temp_below_50"] == 0.0 and by["mild"]["ctx_dome"] == 0.0
    assert "ctx_wind" not in by["unknown"] and "ctx_wind" not in by["missing"]  # left out, not guessed
