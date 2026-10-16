"""Keep the test run from writing into the developer's real refresh state.

`refresh_cli.main` appends every run to `nfl_refresh_history.jsonl` beside the NFL database. Tests
that call it without a database of their own resolved that to the real `instance/` directory, and
about 1,270 fake rows (including 88 injected "boom" failures) ended up in the file the status
page reads. Pointing the refresh state at a temp directory for the whole session stops that.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _no_live_odds_api(monkeypatch, tmp_path_factory):
    """Keep every test away from the real Odds API key and credit ledger.

    The refresh segments now sweep the tracked books, and a developer's .env supplies a real key. A test that ran a whole
    segment unpatched then made paid requests and wrote real rows. A blank key makes the tracker a no-op (load_dotenv never
    overrides a variable that is already set, even to ""); tests that exercise the tracker set their own key and ledger.
    """
    monkeypatch.setenv("ODDS_API_KEY", "")
    monkeypatch.setenv("ODDS_API_LEDGER_PATH", str(tmp_path_factory.getbasetemp() / "odds_api_usage.sqlite3"))


@pytest.fixture(scope="session", autouse=True)
def _isolated_refresh_state():
    with tempfile.TemporaryDirectory(prefix="refresh-state-") as directory:
        previous = os.environ.get("NFL_REFRESH_STATE_DIR")
        os.environ["NFL_REFRESH_STATE_DIR"] = directory
        try:
            yield directory
        finally:
            if previous is None:
                os.environ.pop("NFL_REFRESH_STATE_DIR", None)
            else:
                os.environ["NFL_REFRESH_STATE_DIR"] = previous
