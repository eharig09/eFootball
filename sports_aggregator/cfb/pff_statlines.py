"""Pivot licensed PFF per-dataset CSV rows into readable stat tables.

``pff_player_metrics``/``pff_supplemental_metrics`` store the entire raw PFF
CSV row per (season, dataset) in ``metrics_json`` -- ingestion already parsed
it, nothing here re-derives a value PFF did not publish. This module only
picks which of the several dozen published fields per dataset are worth a
column and labels them, mirroring how ``statlines.py`` pivots CFBD's
long-form ``player_season_stats`` into conventional box-score tables.
"""

from __future__ import annotations

import json
from typing import Any

from sports_aggregator.tables import Column, Table

#: Datasets read from ``player["pff"]`` (one row per season, team key
#: "cfbd_team") versus ``player["pff_supplemental"]`` (team key "event_team").
PRIMARY_DATASETS = frozenset(
    {"passing", "rushing", "receiving", "blocking", "pass_rush", "defense", "coverage"}
)

#: (field, header, format, tooltip), in display order. `field` is the raw PFF
#: CSV column name found inside metrics_json.
DATASET_SPECS: dict[str, dict[str, Any]] = {
    "passing": {
        "label": "PFF passing",
        "columns": [
            ("dropbacks", "DB", "int", "Dropbacks (attempts plus sacks and scrambles)"),
            ("completion_percent", "CMP%", "pct", "Completion percentage"),
            ("yards", "YDS", "int", "Passing yards"),
            ("ypa", "YPA", "f1", "Yards per attempt"),
            ("touchdowns", "TD", "int", "Touchdowns"),
            ("interceptions", "INT", "int", "Interceptions"),
            ("big_time_throws", "BTT", "int", "Big-time throws"),
            ("turnover_worthy_plays", "TWP", "int", "Turnover-worthy plays"),
            ("avg_depth_of_target", "ADOT", "f1", "Average depth of target"),
            ("avg_time_to_throw", "TT", "f2", "Average time to throw, seconds"),
            ("pressure_to_sack_rate", "PRSK%", "pct", "Pressure-to-sack rate"),
            ("qb_rating", "RTG", "f1", "PFF quarterback rating"),
        ],
    },
    "rushing": {
        "label": "PFF rushing",
        "columns": [
            ("attempts", "ATT", "int", "Attempts"),
            ("yards", "YDS", "int", "Rushing yards"),
            ("yards_after_contact", "YACO", "int", "Yards after contact"),
            ("yco_attempt", "YACO/A", "f2", "Yards after contact per attempt"),
            ("avoided_tackles", "MTF", "int", "Missed tackles forced"),
            ("elusive_rating", "ELU", "f1", "Elusive rating"),
            ("breakaway_percent", "BRKWY%", "pct", "Share of yards from breakaway runs"),
            ("explosive", "EXPL", "int", "Explosive runs"),
            ("first_downs", "1D", "int", "First downs"),
            ("fumbles", "FUM", "int", "Fumbles"),
        ],
    },
    "receiving": {
        "label": "PFF receiving",
        "columns": [
            ("routes", "RTE", "int", "Routes run"),
            ("targets", "TGT", "int", "Targets"),
            ("receptions", "REC", "int", "Receptions"),
            ("caught_percent", "CATCH%", "pct", "Catch rate"),
            ("yards", "YDS", "int", "Receiving yards"),
            ("yprr", "YPRR", "f2", "Yards per route run"),
            ("yards_after_catch", "YAC", "int", "Yards after catch"),
            ("avg_depth_of_target", "ADOT", "f1", "Average depth of target"),
            ("contested_catch_rate", "CC%", "pct", "Contested catch rate"),
            ("drop_rate", "DROP%", "pct", "Drop rate"),
        ],
    },
    "blocking": {
        "label": "PFF blocking",
        "columns": [
            ("snap_counts_offense", "SNP", "int", "Offensive snaps"),
            ("pass_block_percent", "PB%", "pct", "Share of snaps pass-blocking"),
            ("pressures_allowed", "PRSS", "int", "Pressures allowed"),
            ("hurries_allowed", "HUR", "int", "Hurries allowed"),
            ("hits_allowed", "HIT", "int", "Hits allowed"),
            ("sacks_allowed", "SK", "int", "Sacks allowed"),
            ("pbe", "PBE", "f1", "Pass-block efficiency"),
        ],
    },
    "pass_rush": {
        "label": "PFF pass rush",
        "columns": [
            ("snap_counts_pass_rush", "SNP", "int", "Pass-rush snaps"),
            ("total_pressures", "PRSS", "int", "Total pressures"),
            ("sacks", "SK", "f1", "Sacks"),
            ("hits", "HIT", "int", "QB hits"),
            ("hurries", "HUR", "int", "Hurries"),
            ("pass_rush_win_rate", "WIN%", "pct", "Pass-rush win rate"),
            ("prp", "PRP", "f1", "Pass-rush productivity"),
        ],
    },
    "defense": {
        "label": "PFF run defense & tackling",
        "columns": [
            ("snap_counts_defense", "SNP", "int", "Defensive snaps"),
            ("tackles", "TKL", "int", "Tackles"),
            ("assists", "AST", "int", "Assisted tackles"),
            ("missed_tackles", "MISS", "int", "Missed tackles"),
            ("missed_tackle_rate", "MISS%", "pct", "Missed tackle rate"),
            ("tackles_for_loss", "TFL", "int", "Tackles for loss"),
            ("stops", "STOP", "int", "Stops"),
            ("forced_fumbles", "FF", "int", "Forced fumbles"),
        ],
    },
    "coverage": {
        "label": "PFF coverage",
        "columns": [
            ("snap_counts_coverage", "SNP", "int", "Coverage snaps"),
            ("targets", "TGT", "int", "Targets allowed"),
            ("receptions", "REC", "int", "Receptions allowed"),
            ("catch_rate", "CATCH%", "pct", "Catch rate allowed"),
            ("yards", "YDS", "int", "Yards allowed"),
            ("yards_per_coverage_snap", "Y/SNP", "f2", "Yards allowed per coverage snap"),
            ("forced_incompletion_rate", "FI%", "pct", "Forced incompletion rate"),
            ("pass_break_ups", "PBU", "int", "Pass breakups"),
            ("interceptions", "INT", "int", "Interceptions"),
            ("qb_rating_against", "RTG", "f1", "Opposing quarterback rating"),
        ],
    },
    "run_defense_detail": {
        "label": "PFF run defense detail",
        "columns": [
            ("snap_counts_run", "SNP", "int", "Run-defense snaps"),
            ("stops", "STOP", "int", "Stops"),
            ("stop_percent", "STOP%", "pct", "Stop rate"),
            ("run_stop_opp", "OPP", "int", "Run-stop opportunities"),
            ("tackles", "TKL", "int", "Tackles"),
            ("missed_tackles", "MISS", "int", "Missed tackles"),
            ("avg_depth_of_tackle", "ADOT", "f1", "Average depth of tackle"),
            ("forced_fumbles", "FF", "int", "Forced fumbles"),
        ],
    },
    "returns": {
        "label": "PFF returns",
        "columns": [
            ("kickoff_attempts", "KOR", "int", "Kickoff returns"),
            ("kickoff_yards", "KOR YDS", "int", "Kickoff return yards"),
            ("kickoff_ypa", "KOR AVG", "f1", "Yards per kickoff return"),
            ("kickoff_touchdowns", "KOR TD", "int", "Kickoff return touchdowns"),
            ("punt_attempts", "PR", "int", "Punt returns"),
            ("punt_yards", "PR YDS", "int", "Punt return yards"),
            ("punt_ypa", "PR AVG", "f1", "Yards per punt return"),
            ("punt_touchdowns", "PR TD", "int", "Punt return touchdowns"),
        ],
    },
}

#: Split-view datasets: PFF publishes each split (man/zone, depth of target)
#: as its own field-name prefix rather than a separate row, so these render
#: one row per (season, split) instead of one row per season.
SPLIT_DATASET_SPECS: dict[str, dict[str, Any]] = {
    "coverage_scheme": {
        "label": "PFF coverage by scheme",
        "split_label": "Scheme",
        "splits": (("Man", "man"), ("Zone", "zone")),
        "columns": [
            ("snap_counts_coverage", "SNP", "int", "Coverage snaps"),
            ("targets", "TGT", "int", "Targets allowed"),
            ("catch_rate", "CATCH%", "pct", "Catch rate allowed"),
            ("yards", "YDS", "int", "Yards allowed"),
            ("yards_per_coverage_snap", "Y/SNP", "f2", "Yards allowed per coverage snap"),
            ("forced_incompletion_rate", "FI%", "pct", "Forced incompletion rate"),
            ("pass_break_ups", "PBU", "int", "Pass breakups"),
            ("grades_coverage_defense", "GRD", "f1", "PFF coverage grade in this scheme"),
        ],
    },
    "receiving_scheme": {
        "label": "PFF receiving by coverage",
        "split_label": "Coverage",
        "splits": (("Man", "man"), ("Zone", "zone")),
        "columns": [
            ("routes", "RTE", "int", "Routes run"),
            ("targets", "TGT", "int", "Targets"),
            ("caught_percent", "CATCH%", "pct", "Catch rate"),
            ("yards", "YDS", "int", "Receiving yards"),
            ("yprr", "YPRR", "f2", "Yards per route run"),
            ("grades_pass_route", "GRD", "f1", "PFF receiving grade against this coverage"),
        ],
    },
    "passing_depth": {
        "label": "PFF passing by depth of target",
        "split_label": "Depth",
        "splits": (("Behind LOS", "behind_los"), ("Short", "short"),
                   ("Medium", "medium"), ("Deep", "deep")),
        "columns": [
            ("attempts", "ATT", "int", "Attempts"),
            ("completion_percent", "CMP%", "pct", "Completion percentage"),
            ("yards", "YDS", "int", "Passing yards"),
            ("ypa", "YPA", "f1", "Yards per attempt"),
            ("touchdowns", "TD", "int", "Touchdowns"),
            ("interceptions", "INT", "int", "Interceptions"),
            ("big_time_throws", "BTT", "int", "Big-time throws"),
            ("turnover_worthy_plays", "TWP", "int", "Turnover-worthy plays"),
            ("grades_pass", "GRD", "f1", "PFF passing grade at this depth"),
        ],
    },
}


def _parse_metrics(raw_json: str | None) -> dict[str, Any]:
    if not raw_json:
        return {}
    try:
        return json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return {}


def _dataset_table(rows: list[dict[str, Any]], spec: dict[str, Any], *, team_key: str) -> Table:
    lines = []
    for row in rows:
        metrics = _parse_metrics(row.get("metrics_json"))
        line = {"season": row.get("season"), "team": row.get(team_key),
                "grade": row.get("primary_grade")}
        for field, *_ in spec["columns"]:
            line[field] = metrics.get(field)
        lines.append(line)
    lines.sort(key=lambda line: -(line.get("season") or 0))
    columns = [
        Column(key="season", label="Season", format="rank", align="left"),
        Column(key="team", label="Team", align="left"),
        *(Column(key=field, label=header, format=fmt, title=title)
          for field, header, fmt, title in spec["columns"]),
        Column(key="grade", label="Grade", format="f1", emphasis=True,
               title="PFF grade for this dataset"),
    ]
    return Table(columns=columns, rows=lines, caption=spec["label"],
                 empty=f"No {spec['label']} rows are stored for this player.")


def _split_table(rows: list[dict[str, Any]], spec: dict[str, Any], *, team_key: str) -> Table:
    split_key = "split"
    lines = []
    for row in rows:
        metrics = _parse_metrics(row.get("metrics_json"))
        for split_label, prefix in spec["splits"]:
            line = {"season": row.get("season"), "team": row.get(team_key), split_key: split_label}
            has_value = False
            for field, *_ in spec["columns"]:
                value = metrics.get(f"{prefix}_{field}")
                line[field] = value
                if value not in (None, ""):
                    has_value = True
            if has_value:
                lines.append(line)
    lines.sort(key=lambda line: (-(line.get("season") or 0), line.get(split_key) or ""))
    columns = [
        Column(key="season", label="Season", format="rank", align="left"),
        Column(key="team", label="Team", align="left"),
        Column(key=split_key, label=spec["split_label"], align="left"),
        *(Column(key=field, label=header, format=fmt, title=title)
          for field, header, fmt, title in spec["columns"]),
    ]
    return Table(columns=columns, rows=lines, caption=spec["label"],
                 empty=f"No {spec['label'].lower()} rows are stored for this player.")


def pff_dataset_tables(player: dict[str, Any]) -> list[dict[str, Any]]:
    """One tab per PFF dataset this player has real rows for.

    ``player["pff"]`` and ``player["pff_supplemental"]`` already carry every
    dataset CFBRepository.get_player links for this player; this only decides
    how each one is displayed.
    """
    primary = player.get("pff") or []
    supplemental = player.get("pff_supplemental") or []
    groups: list[dict[str, Any]] = []
    for dataset, spec in DATASET_SPECS.items():
        source = primary if dataset in PRIMARY_DATASETS else supplemental
        team_key = "cfbd_team" if dataset in PRIMARY_DATASETS else "event_team"
        rows = [row for row in source
                if row.get("dataset") == dataset and row.get("metrics_json")]
        if not rows:
            continue
        groups.append({
            "category": f"pff_{dataset}", "label": spec["label"],
            "table": _dataset_table(rows, spec, team_key=team_key),
        })
    for dataset, spec in SPLIT_DATASET_SPECS.items():
        rows = [row for row in supplemental
                if row.get("dataset") == dataset and row.get("metrics_json")]
        if not rows:
            continue
        groups.append({
            "category": f"pff_{dataset}", "label": spec["label"],
            "table": _split_table(rows, spec, team_key="event_team"),
        })
    return groups
