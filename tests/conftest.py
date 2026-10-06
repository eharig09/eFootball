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
