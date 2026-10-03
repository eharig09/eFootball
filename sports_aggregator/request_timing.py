"""Per-request timing: a Server-Timing header and a slow-request log line.

Profiling this app showed that local timings on a different database say little
about what Render actually does, and nothing in the app reported real render
time. Every non-static response now carries ``Server-Timing: app;dur=<ms>`` (plus
``cache;desc=hit|miss`` for pages behind ``cached_page``), visible in browser dev
tools and to ``curl -i``. Requests slower than SLOW_REQUEST_SECONDS (default 3,
0 disables) also log method, path, status, wall and CPU seconds -- CPU matters
because the instance is shared: wall far above CPU means the request waited.
"""
from __future__ import annotations

import logging
import os
import time

from flask import Flask, Response, g, request

LOGGER = logging.getLogger("sports_aggregator.slow")


def slow_threshold() -> float:
    try:
        return max(0.0, float(os.getenv("SLOW_REQUEST_SECONDS", "3")))
    except ValueError:
        return 3.0


def install_request_timing(app: Flask) -> None:
    @app.before_request
    def start_timer() -> None:
        g._timing_start = (time.perf_counter(), time.process_time())

    @app.after_request
    def report_timing(response: Response) -> Response:
        started = getattr(g, "_timing_start", None)
        if started is None or request.endpoint == "static":
            return response
        wall = time.perf_counter() - started[0]
        parts = [f"app;dur={wall * 1000:.1f}"]
        state = getattr(g, "page_cache", None)
        if state:
            parts.append(f'cache;desc="{state}"')
        response.headers["Server-Timing"] = ", ".join(parts)
        threshold = slow_threshold()
        if threshold and wall >= threshold:
            LOGGER.warning(
                "slow request %s %s -> %s wall=%.2fs cpu=%.2fs cache=%s",
                request.method, request.full_path.rstrip("?"), response.status_code,
                wall, time.process_time() - started[1], state or "-")
        return response
