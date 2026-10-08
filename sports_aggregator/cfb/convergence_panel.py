"""The track record behind Full Convergence, for the diagnostics page.

Reads the tracked action-policy study (`research_outputs/convergence_action_policy_summary.csv`,
produced by `convergence_action_policy.py`) and presents it without upgrading it: the frozen rule is
the baseline, every policy variant was defined after seeing the games and is labeled exploratory,
and a hit rate whose 95% interval includes 50% is said to be unproven. Nothing here changes which
games qualify; it only makes the evidence readable next to the picks.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sports_aggregator.tables import Column, Table, format_value

SUMMARY_CSV = Path(__file__).resolve().parents[2] / "research_outputs" / "convergence_action_policy_summary.csv"
BASELINE = "baseline_keep_all_full_convergence"
POLICY_LABELS = {
    BASELINE: "Frozen rule: back every Full Convergence game",
    "pass_narrative_missing": "Pass when Narrative is missing",
    "pass_spread_14_plus": "Pass when the spread is 14+",
    "fade_spread_14_plus": "Fade when the spread is 14+",
    "pass_missing_and_fade_14_plus": "Pass missing Narrative, fade 14+",
    "pass_weak_confirmation": "Pass on weak confirmation",
    "pass_missing_and_weak_fade_14_plus": "Pass missing and weak, fade 14+",
}


def _read(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _interval(text: str) -> tuple[float | None, float | None]:
    try:
        low, high = (float(part) for part in str(text).split("|"))
        return low, high
    except (TypeError, ValueError):
        return None, None


def _verdict(row: dict[str, str]) -> str:
    low, _ = _interval(row.get("hit_rate_ci95", ""))
    if not row.get("n") or int(row["n"]) == 0 or low is None:
        return "No games"
    return "Interval clears 50%" if low > 0.5 else "Not distinguishable from 50%"


def _line(row: dict[str, str], *, label: str) -> dict[str, Any]:
    low, high = _interval(row.get("hit_rate_ci95", ""))
    n = int(row["n"]) if row.get("n") else 0
    return {
        "label": label, "n": n if n else None,
        "record": f"{row['wins']}-{n - int(row['wins'])}" if n and row.get("wins") else None,
        "hit_rate": float(row["hit_rate"]) if row.get("hit_rate") else None,
        "interval": (f"{format_value(low * 100, 'f1')}% to {format_value(high * 100, 'f1')}%"
                     if low is not None else None),
        "residual": float(row["mean_aligned_residual"]) if row.get("mean_aligned_residual") else None,
        "verdict": _verdict(row),
    }


def _table(rows: list[dict[str, Any]], *, first: str, caption: str, note: str | None = None) -> Table:
    shown = [{
        "label": row["label"], "record": format_value(row["record"]),
        "n": format_value(row["n"], "int"),
        "hit": format_value(None if row["hit_rate"] is None else row["hit_rate"] * 100, "f1"),
        "interval": format_value(row["interval"]),
        "residual": format_value(row["residual"], "signed"),
        "verdict": row["verdict"],
    } for row in rows]
    return Table(
        columns=[Column("label", first, align="left", emphasis=True),
                 Column("record", "Record", align="right"),
                 Column("n", "Games", align="right"),
                 Column("hit", "Hit %", align="right", emphasis=True),
                 Column("interval", "95% interval", align="right"),
                 Column("residual", "Mean edge vs close", align="right"),
                 Column("verdict", "Read", align="left")],
        rows=shown, caption=caption, note=note,
        empty="The action-policy study is not available here.")


def build(path: Path = SUMMARY_CSV) -> dict[str, Any]:
    rows = _read(path)
    baseline_pooled = next((r for r in rows if r["scope"] == "pooled" and r["policy"] == BASELINE), None)
    by_year = sorted((r for r in rows if r["scope"] == "year" and r["policy"] == BASELINE),
                     key=lambda r: int(r["season"]))
    policies = [r for r in rows if r["scope"] == "pooled" and r["policy"] != BASELINE]
    if baseline_pooled is None:
        return {"available": False}
    summary = _line(baseline_pooled, label=POLICY_LABELS[BASELINE])
    return {
        "available": True,
        "summary": summary,
        "seasons_table": _table(
            [_line(r, label=str(r["season"])) for r in by_year], first="Season",
            caption="Frozen Full Convergence by season",
            note="Hit rate is directional against the closing spread; each season is small."),
        "policies_table": _table(
            [summary] + [_line(r, label=POLICY_LABELS.get(r["policy"], r["policy"])) for r in policies],
            first="Policy", caption="Action-policy variants (exploratory)",
            note="Each variant was defined after the games were seen, so a better-looking row here "
                 "is a hypothesis to test forward, not a result to rely on."),
    }
