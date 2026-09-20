"""NFL pressure/OL modeling readiness audit.

Reports whether pressure, offensive-line, and PFF inputs have enough historical
coverage for a leak-safe interaction model. This is intentionally diagnostic:
season-level aggregates are not treated as valid pregame features for the same
season unless a prior-season-only design is used later.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository


KEYWORDS = (
    "pressure", "pass_block", "pass blocking", "pblk", "sack", "hurry",
    "allowed_pressure", "pass_rush", "prsh", "grade_pass", "grade_offense",
)


def report(repository: NFLRepository, *, from_season=2010, to_season=2026) -> dict[str, Any]:
    repository.initialize()
    with repository._connect() as connection:
        pressure = [
            dict(r) for r in connection.execute(
                """SELECT season,COUNT(*) team_rows,
                          SUM(CASE WHEN pressure_rate IS NOT NULL THEN 1 ELSE 0 END) pressure_rate_rows,
                          SUM(CASE WHEN hurry_rate IS NOT NULL THEN 1 ELSE 0 END) hurry_rate_rows,
                          SUM(CASE WHEN knockdown_rate IS NOT NULL THEN 1 ELSE 0 END) knockdown_rate_rows
                   FROM team_pressure_rates
                   WHERE season BETWEEN ? AND ?
                   GROUP BY season ORDER BY season""",
                (int(from_season), int(to_season)),
            )
        ]

        metrics = [
            dict(r) for r in connection.execute(
                """SELECT season,week,family,metric,COUNT(*) rows
                   FROM nfl_pff_player_metrics
                   WHERE season BETWEEN ? AND ?
                   GROUP BY season,week,family,metric
                   ORDER BY season,week,family,metric""",
                (int(from_season), int(to_season)),
            )
        ]

        pff_players = [
            dict(r) for r in connection.execute(
                """SELECT season,COUNT(*) player_rows,
                          SUM(CASE WHEN gsis_id IS NOT NULL THEN 1 ELSE 0 END) linked_rows
                   FROM nfl_pff_players
                   WHERE season BETWEEN ? AND ?
                   GROUP BY season ORDER BY season""",
                (int(from_season), int(to_season)),
            )
        ]

    keyword_metrics = []
    for r in metrics:
        hay = f"{r.get('family','')} {r.get('metric','')}".lower()
        if any(k in hay for k in KEYWORDS):
            keyword_metrics.append(r)

    by_season: dict[int, dict[str, Any]] = defaultdict(lambda: {
        "team_pressure_rows": 0,
        "pff_player_rows": 0,
        "pff_linked_rows": 0,
        "pff_pressure_ol_rows": 0,
        "pff_weekly_pressure_ol_rows": 0,
        "families": set(),
        "metrics": set(),
    })
    for r in pressure:
        s = int(r["season"])
        by_season[s]["team_pressure_rows"] = int(r["team_rows"] or 0)
        by_season[s]["pressure_rate_rows"] = int(r["pressure_rate_rows"] or 0)
    for r in pff_players:
        s = int(r["season"])
        by_season[s]["pff_player_rows"] = int(r["player_rows"] or 0)
        by_season[s]["pff_linked_rows"] = int(r["linked_rows"] or 0)
    for r in keyword_metrics:
        s = int(r["season"])
        n = int(r["rows"] or 0)
        by_season[s]["pff_pressure_ol_rows"] += n
        if int(r["week"] or 0) > 0:
            by_season[s]["pff_weekly_pressure_ol_rows"] += n
        by_season[s]["families"].add(str(r["family"]))
        by_season[s]["metrics"].add(str(r["metric"]))

    rows = []
    for season in range(int(from_season), int(to_season)+1):
        item = by_season[season]
        rows.append({
            "season": season,
            "team_pressure_rows": item["team_pressure_rows"],
            "pressure_rate_rows": item.get("pressure_rate_rows", 0),
            "pff_player_rows": item["pff_player_rows"],
            "pff_linked_rows": item["pff_linked_rows"],
            "pff_pressure_ol_rows": item["pff_pressure_ol_rows"],
            "pff_weekly_pressure_ol_rows": item["pff_weekly_pressure_ol_rows"],
            "pff_families": sorted(item["families"]),
            "pff_metrics": sorted(item["metrics"]),
        })

    weekly_ready = [
        r["season"] for r in rows
        if r["pff_weekly_pressure_ol_rows"] > 0
    ]
    return {
        "version": "nfl-pressure-ol-readiness-v1",
        "warning": (
            "team_pressure_rates is season-level; same-season values are not valid "
            "pregame features without a dated/weekly source. PFF week>0 rows are the "
            "preferred basis for a leak-safe pressure/OL interaction model."
        ),
        "by_season": rows,
        "weekly_pff_ready_seasons": weekly_ready,
        "recommended_next_step": (
            "Use week-level PFF pressure/pass-block metrics only if coverage is broad enough; "
            "otherwise build pressure from play-by-play or use prior-season team pressure as a "
            "separate, explicitly lagged experiment."
        ),
    }
