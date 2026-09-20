"""Render trigger for NFL refresh endpoints.

Unlike the old curl cron this emits structured errors/retries and can derive
the NFL endpoint from the already-configured CFB refresh URL when possible.
"""
from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit
from http.client import RemoteDisconnected
import json
import os
import socket
import time


RETRYABLE = {429, 500, 502, 503, 504}
BACKOFF = (2, 5, 10)


def _derive_url() -> str:
    explicit = (os.getenv("NFL_REFRESH_URL") or "").strip()
    if explicit:
        return explicit
    cfb = (os.getenv("CFB_REFRESH_URL") or "").strip()
    if not cfb:
        raise RuntimeError("NFL_REFRESH_URL or CFB_REFRESH_URL is required")
    parts = urlsplit(cfb)
    path = parts.path
    if path.endswith("/internal/cfb-refresh"):
        path = path[:-len("/internal/cfb-refresh")] + "/internal/nfl-refresh"
    elif path.endswith("/internal/cfb-refresh/"):
        path = path[:-len("/internal/cfb-refresh/")] + "/internal/nfl-refresh"
    else:
        path = "/internal/nfl-refresh"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def trigger() -> dict:
    base = _derive_url()
    token = (os.getenv("CFB_REFRESH_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("CFB_REFRESH_TOKEN is required")
    segment = (os.getenv("NFL_REFRESH_SEGMENT") or "content").strip().casefold()
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}segment={segment}"
    request = Request(
        url, method="POST", data=b"",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "football-lab-nfl-render-trigger/1.0",
        },
    )
    last = ""
    for attempt in range(1, 5):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
                status = getattr(response, "status", 200)
            if 200 <= status < 300:
                return {
                    "status": "triggered",
                    "http_status": status,
                    "segment": segment,
                    "attempt": attempt,
                    "response": body[:500],
                    "url_host": urlsplit(url).netloc,
                }
            last = f"HTTP {status}: {body[:300]}"
            if status not in RETRYABLE:
                break
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            last = f"HTTP {exc.code} {exc.reason}: {body[:300]}"
            if exc.code not in RETRYABLE:
                break
        except (URLError, TimeoutError, socket.timeout, ConnectionError, RemoteDisconnected) as exc:
            last = f"{type(exc).__name__}: {exc}"
        print(json.dumps({
            "status": "retrying",
            "attempt": attempt,
            "segment": segment,
            "error": last,
        }, sort_keys=True), flush=True)
        if attempt < 4:
            time.sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
    raise RuntimeError(last or "NFL refresh trigger failed")


def main() -> int:
    try:
        result = trigger()
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, sort_keys=True), flush=True)
        return 1
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
