"""The one file that says how every refresh segment is currently doing.

Production runs seven bounded segments -- core, rosters, stats, models, content,
analytics, news -- one per hour. Each writes its own row to
``scheduled_refresh_history.jsonl`` and each overwrites the single
``refresh_progress.json``, so "the latest run" is only ever the segment that
happened last, not the health of the pipeline. A content segment that degraded
at 10:00 is invisible by 12:00 once rosters runs clean.

``segment_health.json`` is the roll-up that fixes that. Every segment folds its
finished report into it and the entry survives the next segment's run, so the
status page can answer "is anything wrong right now" from one cheap read.

Stdlib only, so the low-memory refresh entry points can write it directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from sports_aggregator.refresh_remediation import classify, remedy, self_heals

HEALTH_FILENAME = "segment_health.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def health_path(instance: Path) -> Path:
    return Path(instance) / HEALTH_FILENAME


def read_health(instance: Path) -> dict[str, Any]:
    """The whole roll-up, ``{segment: entry}``. Empty dict if not written yet."""
    try:
        data = json.loads(health_path(instance).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_health(instance: Path, health: dict[str, Any]) -> None:
    path = health_path(instance)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(health, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _issue_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every step in this report that did not succeed, with a severity.

    ``degraded_steps`` are optional-step failures; ``required_failures`` are
    the ones that failed the run. Either list may already carry ``category`` /
    ``self_heals`` from the runner; anything missing is derived here so an older
    report still classifies.
    """
    rows: list[dict[str, Any]] = []
    for severity, key in (("failed", "required_failures"), ("degraded", "degraded_steps")):
        for row in report.get(key) or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or ("failed" if severity == "failed" else "degraded"))
            message = str(row.get("message") or "")
            category = str(row.get("category") or classify(status=status, message=message))
            rows.append({
                "step": str(row.get("step") or "unknown"),
                "status": status,
                "message": message[:240],
                "severity": severity,
                "category": category,
                "self_heals": bool(row.get("self_heals", self_heals(category))),
                "detail": str(row.get("detail") or "")[:1500],
            })
    return rows


def classify_step_rows(
    results: list[dict[str, Any]], *, segment: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split refresh step results three ways, each row sanitized for storage.

    Returns ``(required_failures, degraded_steps, skipped_for_env)``:

    * ``required_failures`` -- a non-optional step failed; the run is ``failed``.
    * ``degraded_steps``    -- an optional step failed; the run is ``degraded``.
    * ``skipped_for_env``   -- the step was skipped for a missing key. Not a
      failure and does not change the run status, but worth showing with its
      remedy so a dark source is not silently dark.

    Every row carries a remediation ``category`` and the owning ``segment``.
    """
    from sports_aggregator.refresh_remediation import STEP_SEGMENT

    required: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        step = str(row.get("step", "unknown"))
        status = str(row.get("status", "failed"))
        message = str(row.get("message", ""))[:240]
        owning = segment or STEP_SEGMENT.get(step, "")
        if status == "success":
            continue
        if status == "skipped":
            if message.lower().startswith("needs "):
                skipped.append({"step": step, "message": message,
                                "category": "not_configured", "segment": owning})
            continue
        category = classify(status=status, message=message)
        item = {
            "step": step, "status": status, "message": message,
            "category": category, "self_heals": self_heals(category),
            "segment": owning,
        }
        if row.get("detail"):
            item["detail"] = str(row["detail"])[:1500]
        (degraded if row.get("optional", False) else required).append(item)
    return required, degraded, skipped


def summarize_segment(instance: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Fold one finished segment report into the roll-up and persist it.

    Returns the entry that was written. A ``skipped`` report (the lock was
    already held) changes nothing.
    """
    status = str(report.get("status") or "unknown")
    if status == "skipped":
        return read_health(instance).get(str(report.get("profile") or ""), {})

    segment = str(report.get("profile") or "unknown")
    finished = str(report.get("finished_at") or _now_iso())
    health = read_health(instance)
    previous = health.get(segment) if isinstance(health.get(segment), dict) else {}
    prior_issues = {i.get("step"): i for i in (previous.get("open_issues") or [])
                    if isinstance(i, dict)}

    open_issues = []
    for row in _issue_rows(report):
        first_seen = str((prior_issues.get(row["step"]) or {}).get("since") or finished)
        open_issues.append({**row, "since": first_seen, "segment": segment})

    not_configured = [
        {"step": str(row.get("step") or "unknown"),
         "message": str(row.get("message") or "")[:240], "segment": segment}
        for row in (report.get("skipped_steps") or []) if isinstance(row, dict)
    ]

    entry = {
        "segment": segment,
        "last_run_at": finished,
        "last_status": status,
        "last_success_at": (finished if status == "success"
                            else str(previous.get("last_success_at") or "")),
        "consecutive_degraded": (0 if status == "success"
                                 else int(previous.get("consecutive_degraded") or 0) + 1),
        "seconds": report.get("seconds"),
        "step_count": report.get("step_count"),
        "open_issues": open_issues,
        "not_configured": not_configured,
    }
    health[segment] = entry
    _write_health(instance, health)

    newly_open = [i for i in open_issues
                  if not i["self_heals"] and i["step"] not in prior_issues]
    if newly_open:
        _announce(segment, newly_open)
    return entry


def _announce(segment: str, issues: list[dict[str, Any]]) -> None:
    """POST newly-opened, non-self-healing issues to CFB_ALERT_WEBHOOK.

    Opt-in (no webhook, no call), fire-and-forget, and never allowed to raise
    into the refresh. Only *new* breakage notifies -- an issue already open on
    the previous run is not re-sent every hour.
    """
    webhook = (os.getenv("CFB_ALERT_WEBHOOK") or "").strip()
    if not webhook:
        return
    lines = [f"CFB refresh: {segment} segment has a new problem"]
    for issue in issues:
        fix = remedy(step=issue["step"], category=issue["category"], segment=segment)
        lines.append(f"- {issue['step']} ({issue['category']}): {fix['action']}")
    payload = json.dumps({"text": "\n".join(lines)}).encode("utf-8")
    try:
        request = Request(webhook, data=payload, method="POST",
                          headers={"Content-Type": "application/json"})
        urlopen(request, timeout=10).close()
    except Exception:
        pass


#: Worst first: a hard failure outranks a degrade, and a degrade that will not
#: fix itself outranks one that will.
_SEVERITY_RANK = {"failed": 0, "degraded": 1}


def attention_items(instance: Path) -> list[dict[str, Any]]:
    """Every open issue across every segment, worst first, with its remedy.

    This is what the status page's "Needs attention" list renders.
    """
    items: list[dict[str, Any]] = []
    for entry in read_health(instance).values():
        if not isinstance(entry, dict):
            continue
        segment = str(entry.get("segment") or "")
        for issue in entry.get("open_issues") or []:
            if not isinstance(issue, dict):
                continue
            fix = remedy(step=str(issue.get("step") or ""),
                         category=str(issue.get("category") or "unknown"),
                         segment=segment)
            items.append({
                "step": str(issue.get("step") or "unknown"),
                "segment": segment,
                "severity": str(issue.get("severity") or "degraded"),
                "category": fix["category"],
                "self_heals": bool(issue.get("self_heals", fix["self_heals"])),
                "message": str(issue.get("message") or "")[:240],
                "detail": str(issue.get("detail") or "")[:1500],
                "since": str(issue.get("since") or entry.get("last_run_at") or ""),
                "last_success_at": str(entry.get("last_success_at") or ""),
                "consecutive_degraded": int(entry.get("consecutive_degraded") or 0),
                "cause": fix["cause"],
                "action": fix["action"],
                "rerun_segment": fix["rerun_segment"],
            })

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        return (
            _SEVERITY_RANK.get(item["severity"], 2),
            1 if item["self_heals"] else 0,
            item["step"],
        )

    items.sort(key=sort_key)
    return items


def not_configured_items(instance: Path) -> list[dict[str, Any]]:
    """Steps skipped for a missing key, deduplicated by step, with the remedy."""
    seen: dict[str, dict[str, Any]] = {}
    for entry in read_health(instance).values():
        if not isinstance(entry, dict):
            continue
        segment = str(entry.get("segment") or "")
        for row in entry.get("not_configured") or []:
            if not isinstance(row, dict):
                continue
            step = str(row.get("step") or "unknown")
            if step in seen:
                continue
            fix = remedy(step=step, category="not_configured", segment=segment)
            seen[step] = {
                "step": step, "segment": segment,
                "message": str(row.get("message") or "")[:240],
                "cause": fix["cause"], "action": fix["action"],
                "rerun_segment": fix["rerun_segment"],
            }
    return sorted(seen.values(), key=lambda row: row["step"])


def overall(instance: Path) -> dict[str, Any]:
    """A one-line verdict across all segments for the pill and the banner."""
    health = read_health(instance)
    segments = [e for e in health.values() if isinstance(e, dict)]
    failed = [e for e in segments if e.get("last_status") == "failed"]
    degraded = [e for e in segments if e.get("last_status") == "degraded"]
    healthy = [e for e in segments if e.get("last_status") == "success"]

    open_issues = attention_items(instance)
    actionable = [i for i in open_issues if not i["self_heals"]]

    if failed or actionable:
        state = "failed" if failed else "degraded"
    elif open_issues:
        state = "self_healing"
    elif segments:
        state = "healthy"
    else:
        state = "unknown"

    return {
        "state": state,
        "segment_count": len(segments),
        "failed_count": len(failed),
        "degraded_count": len(degraded),
        "healthy_count": len(healthy),
        "open_issue_count": len(open_issues),
        "actionable_count": len(actionable),
        "self_healing_count": len(open_issues) - len(actionable),
    }
