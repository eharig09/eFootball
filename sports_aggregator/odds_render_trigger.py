"""Render cron trigger for the odds-line tracker. A clock, and nothing more.

It runs in its own container with no disk, so it cannot see whether a slot is due or how many credits are left. It
posts to the web service, which checks the schedule and the credit ledger and answers "skipped" at once when there is
nothing to do. Failures print as JSON so the Render cron log says what actually happened.
"""

from __future__ import annotations

from http.client import RemoteDisconnected
import json
import os
import socket
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

RETRYABLE = frozenset({429, 500, 502, 503, 504})
BACKOFF = (2, 5, 10)
ATTEMPTS = 4
ENDPOINT = "/internal/odds-lines-refresh"


def endpoint_url() -> str:
    explicit = (os.getenv("ODDS_LINES_REFRESH_URL") or "").strip()
    if explicit:
        return explicit
    base = (os.getenv("CFB_REFRESH_URL") or "").strip()
    if not base:
        raise RuntimeError("ODDS_LINES_REFRESH_URL or CFB_REFRESH_URL is required")
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, ENDPOINT, "", ""))


def trigger(*, opener: Callable[..., Any] = urlopen, sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    token = (os.getenv("CFB_REFRESH_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("CFB_REFRESH_TOKEN is required")
    url = endpoint_url()
    request = Request(url, method="POST", data=b"", headers={
        "Authorization": f"Bearer {token}", "User-Agent": "football-lab-odds-trigger/1.0"})
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with opener(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
                status = getattr(response, "status", 200)
            if 200 <= status < 300:
                return {"status": "triggered", "http_status": status, "attempt": attempt,
                        "response": body[:300], "url_host": urlsplit(url).netloc}
            last = f"HTTP {status}: {body[:300]}"
            if status not in RETRYABLE:
                break
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            last = f"HTTP {exc.code} {exc.reason}: {detail[:300]}"
            if exc.code not in RETRYABLE:
                break
        except (URLError, TimeoutError, socket.timeout, ConnectionError, RemoteDisconnected) as exc:
            last = f"{type(exc).__name__}: {exc}"
        print(json.dumps({"status": "retrying", "attempt": attempt, "error": last}, sort_keys=True), flush=True)
        if attempt < ATTEMPTS:
            sleeper(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
    raise RuntimeError(last or "odds-lines trigger failed")


def main() -> int:
    try:
        result = trigger()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True),
              flush=True)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
