"""Presentation for the NFL playoff projection page: bracket, division tables, odds, seed heat map."""
from __future__ import annotations

from typing import Any

from flask import url_for

from sports_aggregator.tables import Column, Table

HEAT_TEAMS_PER_CONF = 10


def _pct(value: float) -> float:
    return round(100.0 * float(value), 1)


def _record(row: dict[str, Any]) -> str:
    base = f"{row['wins']}-{row['losses']}"
    return f"{base}-{row['ties']}" if row["ties"] else base


def _entry(row: dict[str, Any], identities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ident = identities.get(row["team"], {})
    return {
        "team": row["team"], "name": ident.get("nickname") or ident.get("name") or row["team"],
        "url": url_for("nfl.team_page", abbreviation=row["team"]),
        "logo": ident.get("logo_url"), "color": ident.get("color") or "#0b5aa5",
        "record": _record(row), "division": row["division"], "conference": row["conference"],
        "playoff": _pct(row["playoff"]), "bye": _pct(row["bye"]), "division_title": _pct(row["division_title"]),
        "champion": _pct(row["champion"]), "projected_wins": row["projected_wins"],
    }


def _expected_seed(row: dict[str, Any]) -> float:
    probs = row["seed_probs"]
    total = sum(probs)
    return sum((i + 1) * p for i, p in enumerate(probs)) / total if total else 99.0


def projected_bracket(forecast: dict[str, Any], identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Per conference: the seven most likely playoff teams, ordered by expected seed."""
    per = forecast["format"]["teams_per_conference"]
    byes = forecast["format"]["byes_per_conference"]
    panels = []
    for conference in ("AFC", "NFC"):
        rows = [r for r in forecast["rows"] if r["conference"] == conference]
        field = sorted(sorted(rows, key=lambda r: -r["playoff"])[:per], key=_expected_seed)
        seeds = []
        for i, row in enumerate(field):
            entry = _entry(row, identities)
            entry["seed"] = i + 1
            seeds.append(entry)
        by_seed = {e["seed"]: e for e in seeds}
        wild = [(by_seed[a], by_seed[b]) for a, b in zip(range(byes + 1, per + 1), range(per, byes, -1))
                if a < b and a in by_seed and b in by_seed]
        panels.append({"conference": conference, "seeds": seeds, "byes": seeds[:byes], "wild_card": wild})
    return panels


def division_panels(forecast: dict[str, Any], identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for conference in ("AFC", "NFC"):
        for division in ("East", "North", "South", "West"):
            name = f"{conference} {division}"
            rows = sorted((r for r in forecast["rows"] if r["division"] == name),
                          key=lambda r: (-r["division_title"], -r["projected_wins"]))
            out.append({"name": name, "conference": conference,
                        "teams": [_entry(r, identities) for r in rows]})
    return out


def odds_table(forecast: dict[str, Any], identities: dict[str, dict[str, Any]], season: int) -> Table:
    rows = []
    for r in forecast["rows"]:
        ident = identities.get(r["team"], {})
        row = {
            "team": ident.get("name") or r["team"], "team_url": url_for("nfl.team_page", abbreviation=r["team"]),
            "team_logo": ident.get("logo_url"), "team_color": ident.get("color"),
            "division": r["division"], "record": _record(r), "rating": r["rating"],
            "projected_wins": r["projected_wins"],
            "division_title": _pct(r["division_title"]), "playoff": _pct(r["playoff"]),
            "bye": _pct(r["bye"]), "divisional": _pct(r["divisional"]),
            "championship": _pct(r["championship"]), "super_bowl": _pct(r["super_bowl"]),
            "champion": _pct(r["champion"]),
        }
        rows.append(row)
    return Table(
        columns=[
            Column(key="team", label="Team", emphasis=True),
            Column(key="division", label="Division"),
            Column(key="record", label="Rec", sort="number"),
            Column(key="rating", label="Rating", format="f1",
                   title="Points better than an average team on a neutral field"),
            Column(key="projected_wins", label="Proj W", format="f1", group="Season"),
            Column(key="division_title", label="Division", format="pct", group="Odds",
                   title="Chance to win the division"),
            Column(key="playoff", label="Playoffs", format="pct", emphasis=True, group="Odds"),
            Column(key="bye", label="Bye", format="pct", group="Odds",
                   title="Chance to earn the top seed and a first-round bye"),
            Column(key="divisional", label="Divisional", format="pct", group="Rounds"),
            Column(key="championship", label="Conf final", format="pct", group="Rounds"),
            Column(key="super_bowl", label="Super Bowl", format="pct", group="Rounds"),
            Column(key="champion", label="Champion", format="pct", emphasis=True, group="Rounds"),
        ],
        rows=rows, caption=f"{season} NFL playoff odds",
        note=f"{forecast['n_sims']:,} simulated seasons. Select a heading to reorder.",
        empty="No NFL schedule is stored for this season yet.", dense=True,
    )


def seed_heatmap(forecast: dict[str, Any], identities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    per = forecast["format"]["teams_per_conference"]
    panels = []
    for conference in ("AFC", "NFC"):
        rows = sorted((r for r in forecast["rows"] if r["conference"] == conference),
                      key=lambda r: -r["playoff"])[:HEAT_TEAMS_PER_CONF]
        panels.append({"conference": conference, "seeds": list(range(1, per + 1)), "teams": [
            {**_entry(r, identities),
             "cells": [{"seed": i + 1, "pct": _pct(p), "level": round(min(1.0, p / 0.45), 2)}
                       for i, p in enumerate(r["seed_probs"])]}
            for r in rows]})
    return panels


def summary(forecast: dict[str, Any]) -> dict[str, Any]:
    rows = forecast["rows"]
    favourite = max(rows, key=lambda r: r["champion"]) if rows else None
    return {
        "as_of_week": forecast["as_of_week"], "games_remaining": forecast["games_remaining"],
        "n_sims": forecast["n_sims"],
        "favourite": favourite["team"] if favourite else None,
        "favourite_pct": _pct(favourite["champion"]) if favourite else None,
        "locks": sum(1 for r in rows if r["playoff"] >= 0.995),
        "contenders": sum(1 for r in rows if r["playoff"] >= 0.05),
    }
