"""Presentation for the playoff projection page: tables, bracket, seed heat map."""
from __future__ import annotations

from typing import Any

from flask import url_for

from sports_aggregator.cfb.identity import conference_identity, team_identity
from sports_aggregator.cfb.views import brand_cell
from sports_aggregator.tables import Column, Table

MIN_LISTED = 0.005           # a team needs a 0.5% shot at something to be listed
HEATMAP_TEAMS = 16
CONFERENCE_ROWS = 4


def _pct(value: float) -> float:
    return round(100.0 * float(value), 1)


def brand_index(brands: dict[int, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {b["school"]: b for b in brands.values() if b.get("school")}


def _team_url(brand: dict[str, Any] | None) -> str | None:
    return url_for("cfb.team_preview", team_id=brand["team_id"]) if brand and brand.get("team_id") else None


def _entry(row: dict[str, Any], brand: dict[str, Any] | None) -> dict[str, Any]:
    identity = team_identity(brand or {})
    return {"team": row["team"], "url": _team_url(brand),
            "logo": identity.get("logo_dark") or identity.get("logo"),
            "accent": identity["accent"], "accent_dark": identity["accent_dark"],
            "conference": row["conference"], "record": f"{row['wins']}-{row['losses']}",
            "playoff": _pct(row["playoff"]), "bye": _pct(row["bye"]), "auto": _pct(row["auto"]),
            "champion": _pct(row["champion"]), "avg_rank": row["avg_rank"]}


def projected_bracket(forecast: dict[str, Any], by_school: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The most likely field in projected-committee-rank order, laid out as the bracket."""
    rows = {r["team"]: r for r in forecast["rows"]}
    field = [_entry(rows[t], by_school.get(t)) for t in forecast["projected_field"]]
    for i, entry in enumerate(field):
        entry["seed"] = i + 1
    if len(field) < 12:
        return {"field": field, "first_round": [], "byes": field[:4]}
    by_seed = {e["seed"]: e for e in field}
    first_round = [(by_seed[a], by_seed[b]) for a, b in ((5, 12), (6, 11), (7, 10), (8, 9))]
    byes = [(by_seed[1], "8/9"), (by_seed[2], "7/10"), (by_seed[3], "6/11"), (by_seed[4], "5/12")]
    return {"field": field, "first_round": first_round, "byes": byes}


def main_table(forecast: dict[str, Any], by_school: dict[str, dict[str, Any]], season: int) -> Table:
    rows = []
    for r in forecast["rows"]:
        if max(r["playoff"], r["conf_champion"]) < MIN_LISTED:
            continue
        brand = by_school.get(r["team"])
        row = {
            "team": r["team"], "team_url": _team_url(brand),
            "conference": r["conference"], "conference_conference": conference_identity(r["conference"]),
            "record": f"{r['wins']}-{r['losses']}", "rating": r["rating"],
            "playoff": _pct(r["playoff"]), "auto": _pct(r["auto"]), "at_large": _pct(r["at_large"]),
            "bye": _pct(r["bye"]), "quarterfinal": _pct(r["quarterfinal"]),
            "semifinal": _pct(r["semifinal"]), "final": _pct(r["final"]), "champion": _pct(r["champion"]),
            "conf_champion": _pct(r["conf_champion"]), "projected_wins": r["projected_wins"],
            "avg_rank": r["avg_rank"],
        }
        brand_cell(row, "team", brand)
        rows.append(row)
    return Table(
        columns=[
            Column(key="team", label="Team", emphasis=True),
            Column(key="conference", label="Conf"),
            Column(key="record", label="Rec", sort="number"),
            Column(key="rating", label="Rating", format="f1",
                   title="Points better than an average FBS team on a neutral field"),
            Column(key="playoff", label="Make", format="pct", emphasis=True, group="Field",
                   title="Chance to make the 12-team field"),
            Column(key="auto", label="Auto", format="pct", group="Field",
                   title="Chance to get one of the five conference-champion bids"),
            Column(key="at_large", label="At-large", format="pct", group="Field"),
            Column(key="bye", label="Bye", format="pct", group="Field",
                   title="Chance to finish a top-four seed and skip round one"),
            Column(key="quarterfinal", label="Quarters", format="pct", group="Rounds"),
            Column(key="semifinal", label="Semis", format="pct", group="Rounds"),
            Column(key="final", label="Title game", format="pct", group="Rounds"),
            Column(key="champion", label="Champion", format="pct", emphasis=True, group="Rounds"),
            Column(key="conf_champion", label="Conf title", format="pct", group="Season",
                   title="Chance to win the conference championship"),
            Column(key="projected_wins", label="Proj W", format="f1", group="Season"),
            Column(key="avg_rank", label="Avg rank", format="f1", group="Season",
                   title="Average final committee rank across simulations"),
        ],
        rows=rows,
        caption=f"{season} playoff odds",
        note=f"{forecast['n_sims']:,} simulated seasons. Select a heading to reorder.",
        empty="No teams have a projected playoff chance yet.",
        dense=True,
    )


def seed_heatmap(forecast: dict[str, Any], by_school: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in forecast["rows"][:HEATMAP_TEAMS]:
        entry = _entry(r, by_school.get(r["team"]))
        entry["cells"] = [{"seed": i + 1, "pct": _pct(p), "level": round(min(1.0, p / 0.35), 2)}
                          for i, p in enumerate(r["seed_probs"])]
        rows.append(entry)
    return rows


def conference_panels(forecast: dict[str, Any], by_school: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    panels = []
    for conference in forecast["params"]["title_game_conferences"]:
        teams = sorted((r for r in forecast["rows"] if r["conference"] == conference),
                       key=lambda r: -r["conf_champion"])[:CONFERENCE_ROWS]
        panels.append({
            "conference": conference, "identity": conference_identity(conference),
            "playoff_weight": sum(t["playoff"] for t in teams),
            "teams": [{**_entry(r, by_school.get(r["team"])),
                       "title_game": _pct(r["title_game"]), "conf_champion": _pct(r["conf_champion"])}
                      for r in teams],
        })
    panels.sort(key=lambda p: -p["playoff_weight"])
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
