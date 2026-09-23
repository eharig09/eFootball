"""Richer evidence columns for Experience First projected depth charts.

The depth board should answer two separate questions for each player:
1. What did he actually produce on the field last season?
2. What did the advanced grading say about how he played?

Counting production and PFF are deliberately rendered in separate columns. New
players with no college production fall back to recruiting pedigree rather than
an unexplained blank.
"""

from __future__ import annotations

import json
from typing import Any

from sports_aggregator.cfb import views
from sports_aggregator.cfb.recruiting import evidence_score
from sports_aggregator.tables import Column, Table


def _num(value: Any, digits: int = 0) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits:
        return f"{number:.{digits}f}"
    return f"{int(number):,}" if number.is_integer() else f"{number:g}"


def _stat(stats: dict[str, Any], key: str, digits: int = 0) -> str | None:
    return _num(stats.get(key), digits)


def _join(parts: list[str | None], sep: str = " / ") -> str | None:
    clean = [part for part in parts if part]
    return sep.join(clean) if clean else None


def _production_line(position: str | None, categories: dict[str, dict[str, Any]]) -> str | None:
    """Conventional prior-season slash line for a player's position."""
    pos = str(position or "").upper()
    passing = categories.get("passing") or {}
    rushing = categories.get("rushing") or {}
    receiving = categories.get("receiving") or {}
    defense = categories.get("defensive") or {}
    interceptions = categories.get("interceptions") or {}
    kicking = categories.get("kicking") or {}
    punting = categories.get("punting") or {}

    if pos == "QB":
        pass_line = _join([
            (f"{_stat(passing, 'COMPLETIONS')}/{_stat(passing, 'ATT')} CMP"
             if _stat(passing, "ATT") else None),
            f"{_stat(passing, 'YDS')} YDS" if _stat(passing, "YDS") else None,
            (f"{_stat(passing, 'TD')}/{_stat(passing, 'INT')} TD/INT"
             if _stat(passing, "TD") or _stat(passing, "INT") else None),
        ])
        rush_line = _join([
            f"{_stat(rushing, 'CAR')} CAR" if _stat(rushing, "CAR") else None,
            f"{_stat(rushing, 'YDS')} YDS" if _stat(rushing, "YDS") else None,
            f"{_stat(rushing, 'TD')} TD" if _stat(rushing, "TD") else None,
        ])
        return _join([pass_line, f"RUSH {rush_line}" if rush_line else None], " · ")

    if pos in {"RB", "HB", "FB"}:
        rush_line = _join([
            f"{_stat(rushing, 'CAR')} CAR" if _stat(rushing, "CAR") else None,
            f"{_stat(rushing, 'YDS')} YDS" if _stat(rushing, "YDS") else None,
            f"{_stat(rushing, 'YPC', 1)} AVG" if _stat(rushing, "YPC") else None,
            f"{_stat(rushing, 'TD')} TD" if _stat(rushing, "TD") else None,
        ])
        rec_line = _join([
            f"{_stat(receiving, 'REC')} REC" if _stat(receiving, "REC") else None,
            f"{_stat(receiving, 'YDS')} YDS" if _stat(receiving, "YDS") else None,
            f"{_stat(receiving, 'TD')} TD" if _stat(receiving, "TD") else None,
        ])
        return _join([rush_line, f"REC {rec_line}" if rec_line else None], " · ")

    if pos in {"WR", "TE"}:
        rec_line = _join([
            f"{_stat(receiving, 'REC')} REC" if _stat(receiving, "REC") else None,
            f"{_stat(receiving, 'YDS')} YDS" if _stat(receiving, "YDS") else None,
            f"{_stat(receiving, 'YPR', 1)} AVG" if _stat(receiving, "YPR") else None,
            f"{_stat(receiving, 'TD')} TD" if _stat(receiving, "TD") else None,
        ])
        rush_line = _join([
            f"{_stat(rushing, 'CAR')} CAR" if _stat(rushing, "CAR") else None,
            f"{_stat(rushing, 'YDS')} YDS" if _stat(rushing, "YDS") else None,
        ])
        return _join([rec_line, f"RUSH {rush_line}" if rush_line else None], " · ")

    if pos in {"DL", "DT", "NT", "DE", "EDGE", "IDL", "LB", "ILB", "OLB"}:
        line = _join([
            f"{_stat(defense, 'TOT')} TKL" if _stat(defense, "TOT") else None,
            f"{_stat(defense, 'TFL', 1)} TFL" if _stat(defense, "TFL") else None,
            f"{_stat(defense, 'SACKS', 1)} SACK" if _stat(defense, "SACKS") else None,
            f"{_stat(defense, 'QB HUR')} HUR" if _stat(defense, "QB HUR") else None,
            f"{_stat(defense, 'PD')} PD" if _stat(defense, "PD") else None,
            f"{_stat(interceptions, 'INT')} INT" if _stat(interceptions, "INT") else None,
        ])
        return line

    if pos in {"DB", "CB", "S", "FS", "SS"}:
        return _join([
            f"{_stat(defense, 'TOT')} TKL" if _stat(defense, "TOT") else None,
            f"{_stat(defense, 'TFL', 1)} TFL" if _stat(defense, "TFL") else None,
            f"{_stat(defense, 'PD')} PD" if _stat(defense, "PD") else None,
            f"{_stat(interceptions, 'INT')} INT" if _stat(interceptions, "INT") else None,
        ])

    if pos == "K":
        return _join([
            (f"{_stat(kicking, 'FGM')}/{_stat(kicking, 'FGA')} FG"
             if _stat(kicking, "FGA") else None),
            f"{_stat(kicking, 'PCT', 1)}%" if _stat(kicking, "PCT") else None,
            f"LNG {_stat(kicking, 'LONG')}" if _stat(kicking, "LONG") else None,
            (f"{_stat(kicking, 'XPM')}/{_stat(kicking, 'XPA')} XP"
             if _stat(kicking, "XPA") else None),
        ])

    if pos == "P":
        return _join([
            f"{_stat(punting, 'NO')} PUNT" if _stat(punting, "NO") else None,
            f"{_stat(punting, 'YPP', 1)} AVG" if _stat(punting, "YPP") else None,
            f"LNG {_stat(punting, 'LONG')}" if _stat(punting, "LONG") else None,
            f"{_stat(punting, 'In 20')} IN20" if _stat(punting, "In 20") else None,
        ])

    # Unknown/athlete positions still get the most informative stored category.
    for category in ("receiving", "rushing", "defensive", "passing"):
        if categories.get(category):
            return _production_line({"receiving": "WR", "rushing": "RB",
                                     "defensive": "LB", "passing": "QB"}[category], categories)
    return None


_DATASET_LABELS = {
    "offense": "OFF", "passing": "PASS", "passing_depth": "DEPTH",
    "rushing": "RUN", "receiving": "REC", "receiving_scheme": "ROUTE",
    "receiving_concept": "CONCEPT", "receiving_depth": "DEPTH",
    "blocking": "BLK", "blocking_history": "BLK", "defense": "DEF",
    "run_defense": "RUN D", "run_defense_detail": "RUN D",
    "pass_rush": "PRSH", "coverage": "COV", "coverage_scheme": "COV",
    "returns": "RET", "kicking": "KICK", "punting": "PUNT",
}

_POSITION_DATASETS = {
    "QB": ("passing", "offense", "passing_depth", "rushing"),
    "RB": ("rushing", "receiving", "offense"),
    "HB": ("rushing", "receiving", "offense"),
    "FB": ("rushing", "receiving", "blocking", "offense"),
    "WR": ("receiving", "receiving_scheme", "receiving_concept", "receiving_depth", "offense"),
    "TE": ("receiving", "blocking", "receiving_scheme", "receiving_concept", "receiving_depth", "offense"),
    "OL": ("blocking", "blocking_history", "offense"),
    "IOL": ("blocking", "blocking_history", "offense"),
    "OT": ("blocking", "blocking_history", "offense"),
    "G": ("blocking", "blocking_history", "offense"),
    "C": ("blocking", "blocking_history", "offense"),
    "DL": ("defense", "run_defense_detail", "run_defense", "pass_rush"),
    "DT": ("defense", "run_defense_detail", "run_defense", "pass_rush"),
    "NT": ("defense", "run_defense_detail", "run_defense", "pass_rush"),
    "DE": ("defense", "pass_rush", "run_defense_detail", "run_defense"),
    "EDGE": ("defense", "pass_rush", "run_defense_detail", "run_defense"),
    "IDL": ("defense", "run_defense_detail", "pass_rush"),
    "LB": ("defense", "run_defense_detail", "coverage", "pass_rush"),
    "ILB": ("defense", "run_defense_detail", "coverage", "pass_rush"),
    "OLB": ("defense", "pass_rush", "coverage", "run_defense_detail"),
    "DB": ("coverage", "defense", "run_defense_detail"),
    "CB": ("coverage", "defense", "coverage_scheme"),
    "S": ("coverage", "defense", "run_defense_detail"),
    "FS": ("coverage", "defense"), "SS": ("coverage", "defense"),
    "K": ("kicking",), "P": ("punting",),
}


def _raw_grade(dataset: str, packet: dict[str, Any]) -> list[tuple[str, float]]:
    """Expose useful component grades when a PFF dataset stores them."""
    try:
        raw = json.loads(packet.get("metrics_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    candidates: tuple[tuple[str, str], ...] = ()
    if dataset in {"blocking", "blocking_history"}:
        candidates = (("PB", "grades_pass_block"), ("RB", "grades_run_block"))
    elif dataset == "coverage_scheme":
        candidates = (("MAN", "man_grades_coverage_defense"),
                      ("ZONE", "zone_grades_coverage_defense"))
    values = []
    for label, key in candidates:
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError):
            continue
        values.append((label, value))
    return values


def _advanced_line(position: str | None, datasets: dict[str, dict[str, Any]],
                   fallback: Any = None) -> str | None:
    pos = str(position or "").upper()
    wanted = _POSITION_DATASETS.get(pos, tuple(datasets))
    parts: list[str] = []
    used_labels: set[str] = set()
    for dataset in wanted:
        packet = datasets.get(dataset)
        if not packet:
            continue
        components = _raw_grade(dataset, packet)
        if components:
            for label, value in components:
                if label not in used_labels:
                    parts.append(f"{label} {value:.1f}")
                    used_labels.add(label)
            if len(parts) >= 3:
                break
            continue
        grade = packet.get("primary_grade")
        if grade is None:
            continue
        label = _DATASET_LABELS.get(dataset, dataset.replace("_", " ").upper())
        if label in used_labels:
            continue
        try:
            parts.append(f"{label} {float(grade):.1f}")
        except (TypeError, ValueError):
            continue
        used_labels.add(label)
        if len(parts) >= 3:
            break
    if parts:
        return " / ".join(parts)
    try:
        return f"PFF {float(fallback):.1f}" if fallback is not None else None
    except (TypeError, ValueError):
        return None


def _pedigree(player: dict[str, Any]) -> str | None:
    stars = player.get("recruit_stars") or player.get("stars")
    rating = player.get("recruit_rating") or player.get("rating")
    pieces = []
    if stars:
        pieces.append(f"{int(stars)}★")
    if rating is not None:
        try:
            pieces.append(f"{float(rating):.3f}")
        except (TypeError, ValueError):
            pass
    return " · ".join(pieces) or None


def depth_chart_tables(depth_chart: dict[str, Any], season: int,
                       projection: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Depth charts with separate counting-production and advanced evidence."""
    units = []
    for unit, groups in (depth_chart.get("units") or {}).items():
        tables = []
        for group, players in groups.items():
            rows = []
            for player in players:
                if player.get("arrival_type") == "TRANSFER_IN":
                    origin = f"Transfer from {player.get('origin') or 'unknown'}"
                elif player.get("is_returner"):
                    origin = "Returner"
                else:
                    origin = "New"
                evidence = (projection or {}).get(player.get("player_id")) or {}
                production = _production_line(
                    player.get("position"), evidence.get("stats_by_category") or {})
                production_source = None
                if production:
                    stat_year = evidence.get("stat_season") or season - 1
                    earned_at = evidence.get("stats_team") or evidence.get("earned_at")
                    production_source = str(stat_year)
                    if earned_at and earned_at != depth_chart.get("team"):
                        production_source += f" at {earned_at}"
                else:
                    production = _pedigree(player)
                    if production:
                        production_source = "Recruiting composite"
                advanced = _advanced_line(
                    player.get("position"), evidence.get("pff_datasets") or {},
                    evidence.get("grade") or player.get("pff_interest"))
                rows.append({
                    "jersey": player.get("jersey"),
                    "name": player.get("name"),
                    "name_url": views._player_url(player.get("player_id"), season),
                    "name_class": ("state-arrived" if evidence.get("state") == "ARRIVED" else None),
                    "class_year": player.get("class_year"),
                    "height": views.height_label(player.get("height")),
                    "height_sort": player.get("height"),
                    "weight": player.get("weight"),
                    "origin": origin,
                    "production": production,
                    "production_sub": production_source,
                    "production_sort": evidence_score(
                        pff_interest=player.get("pff_interest"),
                        recruit_rating=player.get("recruit_rating"),
                        production=player.get("production_strength")),
                    "advanced": advanced,
                    "advanced_sort": evidence.get("grade") or player.get("pff_interest"),
                })
            tables.append({
                "label": f"{group} ({len(players)})",
                "table": Table(
                    columns=[
                        Column(key="jersey", label="#", format="rank", align="right"),
                        Column(key="name", label="Player", align="left", emphasis=True),
                        Column(key="class_year", label="Cl", format="rank", align="right",
                               title="Current roster class value from CFBD"),
                        Column(key="height", label="Ht", align="right", sort="number"),
                        Column(key="weight", label="Wt", format="int", title="Pounds"),
                        Column(key="origin", label="Origin", align="left"),
                        Column(key="production", label="Prior production", align="left",
                               sort="number",
                               title="Position-specific prior-season counting statistics; recruiting composite when no college production is stored"),
                        Column(key="advanced", label="Advanced", align="left", sort="number",
                               title="Position-relevant PFF dataset grades from the prior season"),
                    ],
                    rows=rows,
                    caption=group,
                    dense=True,
                    empty="No players in this group.",
                ),
            })
        units.append({"unit": unit, "groups": tables})
    return units


def install_depth_chart_profiles() -> None:
    """Replace only the depth-chart table renderer; all other views stay intact."""
    views.depth_chart_tables = depth_chart_tables
