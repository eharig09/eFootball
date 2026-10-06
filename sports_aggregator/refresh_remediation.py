"""Turn a refresh step that did not succeed into a cause and a next action.

The scheduled refresh already records, for every step that failed, its name, its
status (``failed`` / ``timeout`` / ``skipped``), the segment that ran it, and the
last line it printed. Nothing turned that into "here is what to do about it": the
status page could say *degraded* and stop.

This module has two halves:

* :func:`classify` names the *kind* of failure from the status and the message,
  using markers that already appear in the codebase -- a spent upstream quota,
  a missing API key, a step over its time budget, address-space exhaustion, an
  empty result, a transient upstream error.
* :data:`_CATEGORY_REMEDY` and :data:`_STEP_REMEDY` pair a kind (optionally a
  specific step) with a plain-English cause and the next step, including which
  bounded segment to re-run on demand.

It imports nothing outside the standard library so the low-memory refresh entry
points can use it without pulling in the web or data layers.
"""

from __future__ import annotations

from typing import Any

#: Categories that clear on their own on the next scheduled run, with no
#: intervention. A spent daily allowance is the canonical case.
SELF_HEALING = frozenset({"upstream_quota"})

#: Ordered (category, needles) checks against the lower-cased step message.
#: First match wins, so the more specific conditions come first.
_MESSAGE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("upstream_quota", (
        "daily api request limit", "try again tomorrow", "quota exhausted",
        "daily quota", "daily allowance",
    )),
    ("resource", (
        "can't start new thread", "cannot allocate memory", "memoryerror",
        "out of memory", "killed", "signal 9",
        # numpy's BLAS reports an address-space ceiling this way, which is what
        # production's nfl-core-pbp step failed with while the status page called
        # it "unknown".
        "memory allocation", "openblas error",
    )),
    ("timeout", ("exceeded ", "timed out", "read timed out", "timeout")),
    ("empty_result", (
        "0 rows", "stored 0", "0 stored", "nothing stored", "no rows",
        "stored nothing", "0 forecasts", "0 items",
    )),
    ("upstream_error", (
        "http 5", "http 4", "connectionerror", "connection refused",
        "connection reset", "ssl", "temporarily unavailable", "bad gateway",
        "gateway timeout", "service unavailable", "429", "502", "503", "504",
    )),
)


def classify(*, status: str, message: str) -> str:
    """Name the kind of failure. Returns one of:

    ``not_configured``   the step was skipped for a missing key / credential
    ``upstream_quota``   an upstream daily allowance is spent (self-healing)
    ``timeout``          the step ran past its budget
    ``resource``         the host ran out of memory or thread address space
    ``empty_result``     the step ran but produced nothing
    ``upstream_error``   a transient error from a provider
    ``unknown``          none of the above matched
    """
    state = (status or "").strip().lower()
    text = (message or "").strip().lower()
    if state == "skipped" or text.startswith("needs "):
        return "not_configured"
    if state == "timeout":
        return "timeout"
    for category, needles in _MESSAGE_MARKERS:
        if any(needle in text for needle in needles):
            return category
    return "unknown"


def self_heals(category: str) -> bool:
    return category in SELF_HEALING


# --- the remedy catalog ---------------------------------------------------
#
# A remedy is {cause, action, rerun_segment}. ``rerun_segment`` names the
# bounded segment to re-run on demand, or is None when re-running will not help
# (a missing key, a quota that has to reset). ``{segment}`` in a string is
# filled with the segment the step actually ran in.

_GENERIC = {
    "not_configured": {
        "cause": "The step was skipped because a required API key or credential "
                 "is not set in the environment.",
        "action": "Add the missing value to the Render service environment, then "
                  "re-run the {segment} segment. Until then this source stays dark.",
        "rerun_segment": None,
    },
    "upstream_quota": {
        "cause": "An upstream provider's daily request allowance is spent. "
                 "Nothing will succeed until it resets.",
        "action": "No action. The allowance resets on the provider's daily "
                  "schedule and the next {segment} run picks the work back up.",
        "rerun_segment": None,
    },
    "timeout": {
        "cause": "The step ran past its time budget and was stopped. Whatever it "
                 "had not reached yet is stale.",
        "action": "Re-run the {segment} segment on demand, ideally in a quiet "
                  "hour. If it times out again the work genuinely no longer fits "
                  "one pass and the step needs splitting.",
        "rerun_segment": "{segment}",
    },
    "resource": {
        "cause": "The host ran out of memory or thread address space partway "
                 "through the step.",
        "action": "Re-run the {segment} segment when the instance is otherwise "
                  "idle. If it recurs, the step's worker count or the child "
                  "memory ceiling (CFB_REFRESH_CHILD_MB) needs lowering.",
        "rerun_segment": "{segment}",
    },
    "empty_result": {
        "cause": "The step completed but stored nothing. Either the upstream "
                 "feed returned an empty set or a resolver has gone stale.",
        "action": "Re-run the {segment} segment. If it is still empty, the "
                  "source or endpoint is broken and needs checking in source "
                  "admin, not another retry.",
        "rerun_segment": "{segment}",
    },
    "upstream_error": {
        "cause": "A provider returned a transient error (a 5xx, a reset "
                 "connection, or a rate-limit bounce).",
        "action": "Re-run the {segment} segment once. A second failure means the "
                  "provider is down rather than flaky -- check its status page.",
        "rerun_segment": "{segment}",
    },
    "unknown": {
        "cause": "The step failed for a reason this catalog does not recognise. "
                 "The message and the log tail are the place to look.",
        "action": "Read the log tail for this run, then re-run the {segment} "
                  "segment if it looks transient.",
        "rerun_segment": "{segment}",
    },
}

#: Step-specific overrides, keyed by step then category. "*" catches any
#: category for that step.
_STEP_REMEDY: dict[str, dict[str, dict[str, Any]]] = {
    "weather": {
        "upstream_quota": {
            "cause": "Open-Meteo's free daily request allowance is spent. The "
                     "forecasts already captured are fine; new venues will not "
                     "resolve until the allowance resets at 00:00 UTC.",
            "action": "No action needed. The next models-segment run after "
                      "00:00 UTC recaptures the outstanding venues. Force a "
                      "models run only if a game with no forecast kicks off "
                      "before then.",
            "rerun_segment": None,
        },
    },
    "youtube": {
        "not_configured": {
            "cause": "YOUTUBE_API_KEY (or YOUTUBE_API) is not set, so verified "
                     "channel uploads are not being ingested.",
            "action": "Add YOUTUBE_API_KEY in Render -> Environment, redeploy, "
                      "then re-run the content segment. This step stays skipped "
                      "and the site loses YouTube clips until then.",
            "rerun_segment": "content",
        },
    },
    "reddit": {
        "not_configured": {
            "cause": "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not both set, "
                     "so curated subreddit submissions are not ingested.",
            "action": "Set both Reddit credentials in Render -> Environment, "
                      "then re-run the content segment.",
            "rerun_segment": "content",
        },
    },
    "pbp": {
        "timeout": {
            "cause": "The play-by-play ingest for the weeks just played ran past "
                     "its 1800s budget. EPA, win probability, tendencies and the "
                     "middle-of-field splits downstream are all stale until it "
                     "finishes a clean pass.",
            "action": "Re-run the analytics segment on demand -- it only has "
                      "02:00 on the schedule, so a same-day backfill has to be "
                      "asked for.",
            "rerun_segment": "analytics",
        },
    },
    "bluesky": {
        "empty_result": {
            "cause": "The curated Bluesky author feeds returned nothing. Usually "
                     "a handle changed or a DID needs re-resolving.",
            "action": "Re-run bluesky-resolve, then the content segment. If a "
                      "specific author stays empty, fix the handle in "
                      "/college-football/admin/sources/.",
            "rerun_segment": "content",
        },
    },
}

#: Which segment owns which step, for steps whose remedy defaults to re-running
#: "their" segment. Mirrors the *_STEPS lists in tracked_refresh without
#: importing it (that module imports this one).
STEP_SEGMENT: dict[str, str] = {
    "cfbd-sync": "core", "weather": "models", "pregame-snapshot": "core",
    "cfbd-roster-context": "rosters", "cfbd-recruits": "rosters",
    "transfer-grades": "rosters", "cfbd-current-player-stats": "stats",
    "cfbd-models": "models", "cfbd-box-scores": "models", "cfbd-lines": "models",
    "articles": "content", "bluesky": "content", "reddit": "content",
    "youtube": "content", "podcasts": "content", "retag": "content",
    "cluster": "content", "roles": "content", "score": "content",
    "pbp": "analytics", "pbp-derive": "analytics", "epa": "analytics",
    "play-detail": "analytics", "build-tendencies": "analytics",
    "team-advanced": "analytics", "win-probability": "analytics",
    "passing-detail": "analytics", "passing-qb": "analytics",
    "coordinators": "analytics", "local-news-shard": "news",
}


def remedy(*, step: str, category: str, segment: str = "") -> dict[str, Any]:
    """The cause, next action and re-run segment for one failed step.

    ``segment`` is the segment the step actually ran in; it fills ``{segment}``
    placeholders and is the default re-run target. When it is not supplied the
    owning segment from :data:`STEP_SEGMENT` is used.
    """
    step = (step or "").strip()
    category = category if category in _GENERIC else "unknown"
    owning = (segment or "").strip() or STEP_SEGMENT.get(step, "")

    template = (_STEP_REMEDY.get(step, {}).get(category)
                or _STEP_REMEDY.get(step, {}).get("*")
                or _GENERIC[category])

    def fill(value: str | None) -> str | None:
        if value is None:
            return None
        return value.replace("{segment}", owning or "the owning")

    rerun = fill(template.get("rerun_segment"))
    return {
        "category": category,
        "self_heals": self_heals(category),
        "cause": fill(template["cause"]),
        "action": fill(template["action"]),
        "rerun_segment": rerun or None,
    }
