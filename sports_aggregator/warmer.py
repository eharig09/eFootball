"""Renders the heaviest pages before a visitor asks for them.

A cold CFB page is 8-20 s of CPU, and the page cache goes stale more often than it looks: its key carries the
database's modification time (bucketed to an hour in production), so every hour a refresh writes the first
visitor to each page pays the full render. A worker that was recycled has also lost its in-process model caches,
so the first page it builds pays for those too.

The warmer runs inside the web worker, through the app's own test client, so it fills the SAME cache with the SAME
keys a real request would use. It is deliberately polite on a one-CPU instance: it works only while no visitor has
been on for `idle_seconds`, no refresh holds the lock and process memory has room, it stops the moment any of
those stops being true, and it is single-flight with real traffic (a visitor who arrives mid-render waits for it
instead of repeating it, see page_cache.cached_page). It starts after the first completed request so it never
competes with Render's health check.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

WARMER_HEADER = "X-Cache-Warmer"
CFB_PAGES = ("cfb.today", "cfb.scoreboard", "cfb.engine_picks", "cfb.playoff_projection", "cfb.elo_ratings")
NFL_PAGES = ("nfl.dashboard", "nfl.scoreboard", "nfl.playoff_projection")
CFB_GAME_HORIZON_HOURS = 40
CFB_GAME_LIMIT = 20
NFL_GAME_HORIZON_DAYS = 4
NFL_GAME_LIMIT = 8
#: Process memory (not file cache) above which the warmer does not start another render.
MEMORY_CEILING_MB = 1300


def warm_urls(app) -> list[str]:
    """Pages worth having ready, most important first: the landing pages, then games about to be played."""
    from flask import url_for
    urls: list[str] = []
    with app.test_request_context():
        for endpoint in CFB_PAGES + NFL_PAGES:
            try:
                urls.append(url_for(endpoint))
            except Exception:                                # a page that does not exist here is simply not warmed
                LOGGER.debug("warmer: no route %s", endpoint)
        now = datetime.now(timezone.utc)
        try:
            season = app.config.get("CFB_DEFAULT_SEASON") or datetime.now().year
            for game in app.extensions["cfb_repository"].upcoming_games(int(season), limit=80):
                kickoff = datetime.fromisoformat(str(game["start_date"]).replace("Z", "+00:00"))
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=timezone.utc)
                if kickoff - now <= timedelta(hours=CFB_GAME_HORIZON_HOURS):
                    urls.append(url_for("cfb.game_preview", game_id=game["game_id"]))
                if sum(1 for u in urls if "/college-football/games/" in u) >= CFB_GAME_LIMIT:
                    break
        except Exception:
            LOGGER.exception("warmer: could not list upcoming CFB games")
        try:
            from sports_aggregator.nfl.nflverse import current_season
            repository = app.extensions["nfl_repository"]
            season = repository.latest_season() or current_season()
            today = now.date()
            horizon = today + timedelta(days=NFL_GAME_HORIZON_DAYS)
            picked = 0
            for game in repository.schedule(int(season)):
                if game.get("completed") or not game.get("game_date"):
                    continue
                played_on = datetime.fromisoformat(str(game["game_date"])[:10]).date()
                if today <= played_on <= horizon:
                    urls.append(url_for("nfl.game_page", game_id=game["game_id"]))
                    picked += 1
                    if picked >= NFL_GAME_LIMIT:
                        break
        except Exception:
            LOGGER.exception("warmer: could not list upcoming NFL games")
    return urls


class PageWarmer:
    """Keeps the page cache filled; every outside dependency is injected so the policy is testable."""

    def __init__(self, *, urls: Callable[[], list[str]], version: Callable[[], str],
                 fetch: Callable[[str], int], refresh_running: Callable[[], bool],
                 memory_mb: Callable[[], int | None] = lambda: None,
                 idle_seconds: float = 20.0, poll_seconds: float = 60.0, startup_delay: float = 45.0,
                 max_pass_seconds: float = 900.0, max_wait_seconds: float = 120.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._urls, self._version, self._fetch = urls, version, fetch
        self._refresh_running, self._memory_mb = refresh_running, memory_mb
        self.idle_seconds, self.poll_seconds, self.startup_delay = idle_seconds, poll_seconds, startup_delay
        self.max_pass_seconds, self.max_wait_seconds = max_pass_seconds, max_wait_seconds
        self._clock, self._sleep = clock, sleep
        self._last_visitor = clock() - idle_seconds * 10          # nobody has been here yet
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.warmed_version: str | None = None
        self.passes = 0
        self.last_pass: dict[str, Any] | None = None

    # ---- what the request path tells us -------------------------------------------------------------------
    def note_visitor(self) -> None:
        self._last_visitor = self._clock()

    def busy_reason(self) -> str | None:
        if self._clock() - self._last_visitor < self.idle_seconds:
            return "visitor"
        if self._refresh_running():
            return "refresh"
        memory = self._memory_mb()
        if memory is not None and memory > MEMORY_CEILING_MB:
            return "memory"
        return None

    def _wait_until_quiet(self) -> str | None:
        """None once the site is quiet; otherwise the reason it never was within the wait."""
        waited, reason = 0.0, self.busy_reason()
        while reason and waited < self.max_wait_seconds and not self._stop.is_set():
            self._sleep(2.0)
            waited += 2.0
            reason = self.busy_reason()
        return reason

    # ---- one pass -----------------------------------------------------------------------------------------
    def run_pass(self) -> dict[str, Any]:
        """Render every listed page once, stopping early if a visitor, a refresh or memory needs the machine."""
        started = self._clock()
        version = self._version()
        urls = self._urls()
        done, failed = 0, []
        stopped = None
        for url in urls:
            if self._stop.is_set():
                stopped = "shutdown"
                break
            if self._clock() - started > self.max_pass_seconds:
                stopped = "time_budget"
                break
            stopped = self._wait_until_quiet()
            if stopped:
                break
            try:
                status = self._fetch(url)
            except Exception as error:                      # one bad page must not end the pass
                LOGGER.warning("warmer: %s raised %s", url, error)
                failed.append(url)
                continue
            if status >= 500:
                failed.append(url)
            done += 1
        summary = {"version": version, "pages": len(urls), "warmed": done, "failed": failed,
                   "stopped": stopped, "seconds": round(self._clock() - started, 1),
                   "finished_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.passes += 1
            self.last_pass = summary
            if stopped is None:
                self.warmed_version = version              # a complete pass; an interrupted one is retried
        return summary

    def due(self) -> bool:
        return self._version() != self.warmed_version

    # ---- the background loop ------------------------------------------------------------------------------
    def _loop(self) -> None:
        self._sleep(self.startup_delay)
        while not self._stop.is_set():
            try:
                if self.due():
                    self.run_pass()
            except Exception:
                LOGGER.exception("warmer pass failed")
            self._sleep(self.poll_seconds)

    def arm(self) -> bool:
        """Start the background thread once; False if it was already running."""
        with self._lock:
            if self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._loop, name="page-warmer", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"armed": self._thread is not None, "passes": self.passes,
                    "warmed_version": self.warmed_version, "last_pass": self.last_pass}


#: Paths that are not a person using the site: Render's health check requests "/" every few seconds, and static
#: files ride along with a page that was already counted. Treating them as visitors would keep the site "busy" forever.
NON_VISITOR_PATHS = ("/", "/robots.txt", "/favicon.ico", "/sitemap.xml")


def is_visitor_request(path: str, user_agent: str = "") -> bool:
    if path in NON_VISITOR_PATHS or path.startswith("/static/") or path.startswith("/internal/"):
        return False
    return "render/" not in user_agent.casefold() and "cfb-intelligence-render-cron" not in user_agent.casefold()


def install_page_warmer(app, *, enabled: bool) -> PageWarmer | None:
    """Wire a warmer into a Flask app; None (and no hooks) when disabled, so tests and scripts are untouched."""
    if not enabled or app.config.get("TESTING"):
        return None
    from flask import request
    from sports_aggregator import page_cache
    from pathlib import Path
    from sports_aggregator.process_probe import lock_is_held

    def version() -> str:
        with app.app_context():
            return f"{page_cache.code_version()}:{page_cache.data_version()}"

    def fetch(url: str) -> int:
        return app.test_client().get(url, headers={WARMER_HEADER: "1"}).status_code

    def refresh_running() -> bool:
        lock = Path(app.config["CFB_DATABASE_PATH"]).parent / "scheduled_refresh.lock"
        return lock_is_held(lock, stale_seconds=3600)

    def memory_mb() -> int | None:
        from sports_aggregator.cfb.data_status import host_resources
        info = host_resources()
        return None if not info else info.get("memory_processes_mb")

    warmer = PageWarmer(urls=lambda: warm_urls(app), version=version, fetch=fetch,
                        refresh_running=refresh_running, memory_mb=memory_mb)
    app.extensions["page_warmer"] = warmer

    @app.before_request
    def note_visitor() -> None:
        if request.headers.get(WARMER_HEADER) is None and is_visitor_request(request.path, request.headers.get("User-Agent", "")):
            warmer.note_visitor()

    @app.after_request
    def arm_after_first_response(response):
        # after a real response: the platform's health check has been answered, so warming can begin
        if request.headers.get(WARMER_HEADER) is None:
            warmer.arm()
        return response

    return warmer
