"""Render Cron trigger. A clock, and nothing more.

This runs in its own container with no disk: the database mounts to the web
service alone, so nothing here can see whether a game is being played. It asks
for ``auto`` and lets the web service choose the smallest profile that moment
needs.

The trigger is deliberately tolerant of short Render deploy/restart windows.
A 502/503/504 or connection failure is retried a few times with a short backoff;
configuration/auth/client errors still fail immediately. Every failed attempt
is printed as JSON so the Render cron log says what actually happened instead
of only reporting exit status 1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import json
import os
import socket
import sys
import time
from typing import Any, Callable


RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 4
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)


def _error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:500]
    except Exception:
        return ""


def _emit_attempt(*, attempt: int, attempts: int, error: str,
                  status: int | None = None, body: str = "") -> None:
    print(json.dumps({
        "status": "retrying" if attempt < attempts else "failed",
        "attempt": attempt,
        "attempts": attempts,
        "http_status": status,
        "error": error[:300],
        "response": body[:500],
    }, sort_keys=True), flush=True)


def trigger_if_due(
    *,
    now: datetime | None = None,
    opener: Callable[..., Any] = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = DEFAULT_ATTEMPTS,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    zone_name = os.getenv("CFB_REFRESH_TIMEZONE", "America/New_York")
    current = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone_name))
    profile = (os.getenv("CFB_REFRESH_PROFILE") or "auto").strip().casefold()
    segment = (os.getenv("CFB_REFRESH_SEGMENT") or "").strip().casefold()
    url = (os.getenv("CFB_REFRESH_URL") or "").strip()
    token = (os.getenv("CFB_REFRESH_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("CFB_REFRESH_URL and CFB_REFRESH_TOKEN are required")

    separator = "&" if "?" in url else "?"
    query = {"profile": profile}
    if segment:
        query["segment"] = segment
    trigger_url = f"{url}{separator}{urlencode(query)}"
    request = Request(
        trigger_url,
        method="POST",
        data=b"",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "cfb-intelligence-render-cron/1.0",
        },
    )

    attempts = max(1, int(attempts))
    last_error = "unknown trigger failure"
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=request_timeout) as response:
                body = response.read().decode("utf-8", "replace")
                status = getattr(response, "status", 200)
            if not 200 <= status < 300:
                last_error = f"refresh endpoint returned HTTP {status}"
                if status not in RETRYABLE_HTTP_STATUSES or attempt >= attempts:
                    raise RuntimeError(f"{last_error}: {body[:500]}")
                _emit_attempt(attempt=attempt, attempts=attempts,
                              status=status, error=last_error, body=body)
            else:
                return {
                    "status": "triggered" if status == 202 else "skipped",
                    "requested": profile,
                    "profile": _resolved_profile(body) or profile,
                    "segment": segment or None,
                    "http_status": status,
                    "local_time": current.isoformat(),
                    "attempt": attempt,
                    "response": body[:500],
                }
        except HTTPError as exc:
            body = _error_body(exc)
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in RETRYABLE_HTTP_STATUSES or attempt >= attempts:
                raise RuntimeError(f"{last_error}: {body}") from exc
            _emit_attempt(attempt=attempt, attempts=attempts,
                          status=exc.code, error=last_error, body=body)
        except (URLError, TimeoutError, socket.timeout, ConnectionError,
                RemoteDisconnected) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= attempts:
                raise RuntimeError(last_error) from exc
            _emit_attempt(attempt=attempt, attempts=attempts, error=last_error)

        if attempt < attempts:
            index = min(attempt - 1, len(DEFAULT_BACKOFF_SECONDS) - 1)
            sleeper(DEFAULT_BACKOFF_SECONDS[index])

    raise RuntimeError(last_error)


def _resolved_profile(body: str) -> str | None:
    try:
        return (json.loads(body) or {}).get("profile")
    except (json.JSONDecodeError, AttributeError):
        return None


def main() -> int:
    try:
        report = trigger_if_due()
    except Exception as exc:
        print(json.dumps({
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, sort_keys=True), flush=True)
        return 1
    print(json.dumps(report, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
