import logging

from app import create_app


def _client(tmp_path, **config):
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False, **config})
    return app, app.test_client()


def test_responses_carry_server_timing(tmp_path):
    _, client = _client(tmp_path)
    header = client.get("/").headers["Server-Timing"]
    assert header.startswith("app;dur=")


def test_page_cache_state_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("CFB_PAGE_CACHE_SECONDS", "600")
    app, client = _client(tmp_path, TESTING=False, CACHE_TYPE="SimpleCache")
    first = client.get("/nfl/picks/").headers["Server-Timing"]
    second = client.get("/nfl/picks/").headers["Server-Timing"]
    assert 'cache;desc="miss"' in first and 'cache;desc="hit"' in second


def test_slow_requests_are_logged_with_cpu_and_cache(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SLOW_REQUEST_SECONDS", "0.000001")
    _, client = _client(tmp_path)
    with caplog.at_level(logging.WARNING, logger="sports_aggregator.slow"):
        client.get("/")
    assert any("slow request GET /" in r.message and "cpu=" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setenv("SLOW_REQUEST_SECONDS", "0")
    with caplog.at_level(logging.WARNING, logger="sports_aggregator.slow"):
        client.get("/")
    assert not caplog.records


def test_bytecode_cache_is_opt_in_under_test_and_never_fatal(tmp_path, monkeypatch):
    app, _ = _client(tmp_path)
    assert app.jinja_env.bytecode_cache is None

    monkeypatch.setenv("JINJA_BYTECODE_CACHE_DIR", str(tmp_path / "jc"))
    app, client = _client(tmp_path)
    client.get("/")
    assert app.jinja_env.bytecode_cache is not None
    assert any((tmp_path / "jc").iterdir())

    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("JINJA_BYTECODE_CACHE_DIR", str(blocker / "sub"))
    app, client = _client(tmp_path)
    assert app.jinja_env.bytecode_cache is None and client.get("/").status_code == 200
