"""The page warmer: it renders the heaviest pages only while the site is quiet, and never gets in a visitor's way."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import create_app
from sports_aggregator import warmer as warmer_module
from sports_aggregator.warmer import PageWarmer, is_visitor_request, warm_urls


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(urls=("/a/", "/b/", "/c/"), *, version=lambda: "v1", fetch=None, refresh=lambda: False, memory=lambda: None,
         clock=None, **options):
    clock = clock or Clock()
    fetched = []

    def default_fetch(url):
        fetched.append(url)
        clock.now += 10.0                                    # a cold page takes a while
        return 200

    warmer = PageWarmer(urls=lambda: list(urls), version=version, fetch=fetch or default_fetch,
                        refresh_running=refresh, memory_mb=memory, clock=clock, sleep=clock.sleep, **options)
    return warmer, fetched, clock


def test_a_quiet_site_gets_every_page_rendered_once_and_is_then_up_to_date():
    warmer, fetched, _ = make()
    summary = warmer.run_pass()
    assert fetched == ["/a/", "/b/", "/c/"]
    assert (summary["warmed"], summary["stopped"], summary["failed"]) == (3, None, [])
    assert warmer.warmed_version == "v1" and warmer.due() is False


def test_a_new_data_version_makes_it_due_again():
    state = {"v": "v1"}
    warmer, _, _ = make(version=lambda: state["v"])
    warmer.run_pass()
    state["v"] = "v2"                                        # a refresh wrote the database
    assert warmer.due() is True


def test_it_waits_while_a_visitor_is_on_and_gives_up_if_they_never_leave():
    warmer, fetched, clock = make(idle_seconds=20.0, max_wait_seconds=10.0)     # the wait ends before the visitor "leaves"
    warmer.note_visitor()
    summary = warmer.run_pass()
    assert fetched == [] and summary["stopped"] == "visitor"
    assert warmer.warmed_version is None and warmer.due() is True       # an interrupted pass is retried, not recorded


def test_a_visitor_arriving_mid_pass_stops_it_before_the_next_page():
    clock = Clock()
    holder = {}

    def fetch(url):
        holder["fetched"].append(url)
        if url == "/a/":
            holder["warmer"].note_visitor()                  # someone opens the site while /a/ is rendering
        clock.now += 1.0
        return 200

    warmer, _, _ = make(clock=clock, fetch=fetch, idle_seconds=20.0, max_wait_seconds=0.0)
    holder.update(warmer=warmer, fetched=[])
    summary = warmer.run_pass()
    assert holder["fetched"] == ["/a/"] and summary["stopped"] == "visitor"


def test_it_does_not_run_beside_a_refresh_or_when_process_memory_is_high():
    refreshing, fetched, _ = make(refresh=lambda: True, max_wait_seconds=4.0)
    assert refreshing.run_pass()["stopped"] == "refresh" and fetched == []
    heavy, fetched, _ = make(memory=lambda: 1500, max_wait_seconds=4.0)
    assert heavy.run_pass()["stopped"] == "memory" and fetched == []
    fine, fetched, _ = make(memory=lambda: 600)
    assert fine.run_pass()["stopped"] is None and len(fetched) == 3


def test_a_pass_has_a_time_budget():
    warmer, fetched, _ = make(urls=[f"/p{i}/" for i in range(10)], max_pass_seconds=35.0)
    summary = warmer.run_pass()
    assert summary["stopped"] == "time_budget" and 0 < len(fetched) < 10


def test_one_bad_page_does_not_end_the_pass():
    clock = Clock()
    calls = []

    def fetch(url):
        calls.append(url)
        if url == "/b/":
            raise RuntimeError("boom")
        return 500 if url == "/c/" else 200

    warmer, _, _ = make(clock=clock, fetch=fetch, urls=("/a/", "/b/", "/c/", "/d/"))
    summary = warmer.run_pass()
    assert calls == ["/a/", "/b/", "/c/", "/d/"]
    assert summary["failed"] == ["/b/", "/c/"] and summary["stopped"] is None


def test_it_only_counts_real_page_views_as_visitors():
    assert is_visitor_request("/college-football/games/1/", "Mozilla/5.0") is True
    assert is_visitor_request("/", "Mozilla/5.0") is False                     # Render's health check asks for this constantly
    assert is_visitor_request("/college-football/scoreboard/", "Render/1.0") is False
    assert is_visitor_request("/static/cfb.css", "Mozilla/5.0") is False
    assert is_visitor_request("/internal/cfb-refresh", "curl/8") is False
    assert is_visitor_request("/robots.txt", "Googlebot") is False


# ---- which pages -----------------------------------------------------------------------------------------------
class FakeCFB:
    path = "fake-cfb.sqlite3"                     # create_app reads the repository's path; nothing is opened

    def __init__(self, games):
        self.games = games

    def upcoming_games(self, season, limit=16):
        return self.games[:limit]


class FakeNFL:
    path = "fake-nfl.sqlite3"

    def __init__(self, games):
        self.games = games

    def latest_season(self):
        return 2026

    def schedule(self, season, team=None):
        return self.games


def _iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_it_lists_landing_pages_then_games_about_to_be_played():
    soon = [{"game_id": 100 + i, "start_date": _iso(3 + i)} for i in range(30)]
    later = [{"game_id": 900, "start_date": _iso(24 * 6)}]                   # next week: not worth warming yet
    today = datetime.now(timezone.utc).date()
    nfl = [{"game_id": "g1", "game_date": str(today + timedelta(days=1)), "completed": 0},
           {"game_id": "g2", "game_date": str(today - timedelta(days=2)), "completed": 1},
           {"game_id": "g3", "game_date": str(today + timedelta(days=30)), "completed": 0}]
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_REPOSITORY": FakeCFB(soon + later), "NFL_REPOSITORY": FakeNFL(nfl)})
    urls = warm_urls(app)
    assert urls[0] == "/college-football/" and "/nfl/" in urls[:8]
    cfb_games = [u for u in urls if "/college-football/games/" in u]
    assert len(cfb_games) == warmer_module.CFB_GAME_LIMIT
    assert cfb_games[0].endswith("/100/") and not any("/900/" in u for u in cfb_games)    # soonest first, nothing next week
    nfl_games = [u for u in urls if "/nfl/games/" in u]
    assert len(nfl_games) == 1 and "g1" in nfl_games[0]                         # not played, not a month away


def test_a_listing_failure_still_leaves_the_landing_pages():
    class Broken:
        path = "fake-cfb.sqlite3"

        def upcoming_games(self, *a, **k):
            raise RuntimeError("database locked")

    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_REPOSITORY": Broken(), "NFL_REPOSITORY": FakeNFL([])})
    assert "/college-football/" in warm_urls(app)


# ---- wiring ----------------------------------------------------------------------------------------------------
def test_it_is_off_unless_asked_for_and_never_in_tests():
    assert create_app({"TESTING": True}).extensions.get("page_warmer") is None
    assert warmer_module.install_page_warmer(create_app({"TESTING": True}), enabled=True) is None


def test_visitors_are_noted_the_warmer_itself_is_not_and_the_first_response_arms_it(monkeypatch):
    app = create_app({"TESTING": False, "REGISTER_LEGACY_DASHBOARDS": False})
    armed = []
    monkeypatch.setattr(PageWarmer, "arm", lambda self: armed.append(1) or True)
    warmer = warmer_module.install_page_warmer(app, enabled=True)
    assert warmer is not None and warmer.busy_reason() is None
    client = app.test_client()
    client.get("/robots.txt", headers={"User-Agent": "Render/1.0"})              # a health check
    assert warmer.busy_reason() is None
    client.get("/college-football/search/", headers={"User-Agent": "Mozilla/5.0"})
    assert warmer.busy_reason() == "visitor"
    warmer._last_visitor -= 100
    client.get("/college-football/search/", headers={warmer_module.WARMER_HEADER: "1"})
    assert warmer.busy_reason() is None                                           # its own requests are not visitors
    assert armed                                                                   # armed by a completed response
