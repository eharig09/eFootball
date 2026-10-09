"""Concurrent requests for the same cold page render it once; the others read the result from the cache."""
import threading
import time

from flask import Flask

from sports_aggregator import page_cache


def _app():
    app = Flask(__name__)
    app.config.update(TESTING=False, DEBUG=False, CACHE_TYPE="SimpleCache", CFB_DATABASE_PATH="")
    page_cache.cache.init_app(app)
    return app


def test_identical_cold_requests_render_once(monkeypatch):
    monkeypatch.setenv("CFB_PAGE_CACHE_SECONDS", "60")
    app = _app()
    renders = []

    @app.get("/slow/")
    @page_cache.cached_page
    def slow():
        renders.append(time.monotonic())
        time.sleep(0.4)                                   # a cold page
        return f"rendered {len(renders)}"

    results = []

    def fetch():
        results.append(app.test_client().get("/slow/").get_data(as_text=True))

    threads = [threading.Thread(target=fetch) for _ in range(5)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(renders) == 1
    assert results == ["rendered 1"] * 5
    assert time.monotonic() - started < 1.5               # waited for one render, not five
    assert not page_cache._FLIGHTS                        # the lock table does not grow with every page built


def test_different_pages_render_in_parallel(monkeypatch):
    monkeypatch.setenv("CFB_PAGE_CACHE_SECONDS", "60")
    app = _app()

    @app.get("/page/<name>/")
    @page_cache.cached_page
    def page(name):
        time.sleep(0.4)
        return name

    threads = [threading.Thread(target=lambda n=n: app.test_client().get(f"/page/{n}/")) for n in ("a", "b", "c")]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert time.monotonic() - started < 1.0               # not serialised behind one another


def test_a_failing_render_lets_the_next_request_try(monkeypatch):
    monkeypatch.setenv("CFB_PAGE_CACHE_SECONDS", "60")
    app = _app()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    calls = []

    @app.get("/flaky/")
    @page_cache.cached_page
    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "ok"

    client = app.test_client()
    assert client.get("/flaky/").status_code == 500
    assert client.get("/flaky/").get_data(as_text=True) == "ok"
    assert not page_cache._FLIGHTS                         # the lock was released by the failure too


def test_a_stuck_render_does_not_hold_everyone_forever(monkeypatch):
    monkeypatch.setenv("CFB_PAGE_CACHE_SECONDS", "60")
    monkeypatch.setattr(page_cache, "SINGLE_FLIGHT_WAIT_SECONDS", 0.2)
    app = _app()
    gate = threading.Event()
    calls = []

    @app.get("/stuck/")
    @page_cache.cached_page
    def stuck():
        calls.append(1)
        if len(calls) == 1:
            gate.wait(2)                                  # only the first render hangs
        return "done"

    first = threading.Thread(target=lambda: app.test_client().get("/stuck/"))
    first.start()
    time.sleep(0.05)
    started = time.monotonic()
    second = app.test_client().get("/stuck/")             # waits 0.2 s, then renders on its own
    assert time.monotonic() - started < 1.5
    gate.set()
    first.join()
    assert second.status_code == 200
