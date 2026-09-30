"""Display models for the route, hash-side and package analytics: banded tables and view dicts.

Keeps `route_analytics` (queries) free of presentation. Each builder returns a `Table` using the shared
banded-header contract, or a plain dict the `_nfl_ui.html` macros render (side bars, route-depth mix).
"""

from __future__ import annotations

from typing import Any

from sports_aggregator.nfl import route_analytics as ra
from sports_aggregator.nfl.plays import ZONES
from sports_aggregator.tables import Column, Table

NO_ROUTES = ("Routes and targets by route are not published for {season} yet. nflverse releases "
             "participation data after the fact; earlier seasons have it.")
NO_HASH = "Hash-mark charting (FTN) starts in 2022 and has not been stored for this season."
NO_PACKAGES = "Personnel and on-field data are not published for this season yet."


def _signed_class(value: float | None, threshold: float = 0.05) -> str | None:
    if value is None:
        return None
    if value > threshold:
        return "is-good"
    if value < -threshold:
        return "is-poor"
    return None


def heat(rate: float | None, low: float = 0.05) -> str | None:
    """0-5 bucket for an on-field rate, used as a heat class on table cells."""
    if rate is None or rate < low:
        return None
    return f"heat-{min(5, 1 + int(rate * 5))}" if rate < 1 else "heat-5"


def _player_url(player_id: str, season: int) -> str:
    return f"/nfl/players/{player_id}/?season={season}"


# ---------------------------------------------------------------- route tree

def route_table(tree: dict[str, Any], *, role: str) -> Table:
    if not tree.get("has_data"):
        return Table((), [], empty="No targets with a charted route are stored for this scope.")
    passer = role == "passer"
    columns = [
        Column("label", "Route", "text", emphasis=True, group="Route"),
        Column("targets", "Att" if passer else "Tgt", "int", group="Volume"),
        Column("share", "%", "rate", title="Share of all charted targets", group="Volume"),
        Column("receptions", "Cmp" if passer else "Rec", "int", group="Results"),
        Column("catch_rate", "Cmp %" if passer else "Catch %", "rate", group="Results"),
        Column("yards", "Yds", "big", group="Results"),
        Column("yards_per_target", "Y/A" if passer else "Y/T", "f1", group="Results"),
        Column("touchdowns", "TD", "int", group="Results"),
    ]
    if passer:
        columns.append(Column("interceptions", "INT", "int", group="Results"))
    columns += [
        Column("epa_per_target", "EPA/A" if passer else "EPA/T", "signed2", group="Efficiency"),
        Column("epa_vs_league", "vs Lg", "signed2", title="EPA per target minus the league rate on this route",
               group="Efficiency"),
        Column("adot", "aDOT", "f1", title="Average depth of target", group="Efficiency"),
    ]
    if passer:
        columns.append(Column("cpoe", "CPOE", "f1", group="Efficiency"))
    else:
        columns += [Column("yac_per_reception", "YAC/R", "f1", group="Efficiency"),
                    Column("drops", "Drop", "int", group="Efficiency")]
    rows = []
    for row in tree["rows"]:
        item = dict(row)
        item["epa_vs_league_class"] = _signed_class(row.get("epa_vs_league"))
        item["epa_per_target_class"] = _signed_class(row.get("epa_per_target"), 0.0)
        rows.append(item)
    total = dict(tree["total"])
    total["label"] = "All charted routes"
    return Table(columns, rows, total_row=total, dense=True, empty="No charted routes.")


def route_depth_mix(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Targets by depth family for the stacked bar above a route table."""
    if not tree.get("has_data"):
        return []
    return [{"label": family["label"], "share": family["share"], "targets": family["targets"],
             "epa_per_target": family["epa_per_target"], "catch_rate": family["catch_rate"],
             "key": family["label"].split()[0].lower()} for family in tree["families"]]


# ---------------------------------------------------------------- pairs

def pair_table(result: dict[str, Any], *, role: str, season: int) -> Table:
    if not result.get("has_data"):
        return Table((), [], empty="No charted pairings are stored for this player.")
    passer = role == "passer"
    columns = [
        Column("name", "Target" if passer else "Quarterback", "text", emphasis=True, group="Pairing"),
        Column("position", "Pos", "text", group="Pairing"),
        Column("targets", "Tgt", "int", group="Volume"),
        Column("share", "%", "rate", group="Volume"),
        Column("receptions", "Rec", "int", group="Results"),
        Column("catch_rate", "Catch %", "rate", group="Results"),
        Column("yards", "Yds", "big", group="Results"),
        Column("yards_per_target", "Y/T", "f1", group="Results"),
        Column("touchdowns", "TD", "int", group="Results"),
        Column("interceptions", "INT", "int", group="Results"),
        Column("epa_per_target", "EPA/T", "signed2", group="Efficiency"),
        Column("adot", "aDOT", "f1", group="Efficiency"),
        Column("routes", "Top routes", "text", group="Routes"),
    ]
    if passer:
        columns += [
            Column("pff_route_grade", "PFF route", "f1", title="PFF receiving route grade, season", group="Target grades & tracking"),
            Column("pff_yprr", "YPRR", "f2", title="PFF yards per route run", group="Target grades & tracking"),
            Column("ngs_separation", "Sep", "f1", title="Next Gen average separation (yd)", group="Target grades & tracking"),
            Column("ngs_cushion", "Cush", "f1", title="Next Gen average cushion (yd)", group="Target grades & tracking"),
        ]
    rows = []
    for row in result["rows"]:
        item = dict(row)
        item["name_url"] = _player_url(row["player_id"], season)
        item["name_sub"] = row.get("team") or ""
        item["routes"] = " · ".join(f"{label} {count}" for label, count in row.get("top_routes", []))
        item["epa_per_target_class"] = _signed_class(row.get("epa_per_target"), 0.0)
        rows.append(item)
    return Table(columns, rows, dense=True, empty="No charted pairings.")


def team_pair_table(result: dict[str, Any], season: int) -> Table:
    if not result.get("has_data"):
        return Table((), [], empty="No charted pairings are stored for this team.")
    columns = [
        Column("passer", "Passer", "text", emphasis=True, group="Pairing"),
        Column("receiver", "Target", "text", emphasis=True, group="Pairing"),
        Column("position", "Pos", "text", group="Pairing"),
        Column("targets", "Tgt", "int", group="Volume"),
        Column("receptions", "Rec", "int", group="Results"),
        Column("catch_rate", "Catch %", "rate", group="Results"),
        Column("yards", "Yds", "big", group="Results"),
        Column("yards_per_target", "Y/T", "f1", group="Results"),
        Column("touchdowns", "TD", "int", group="Results"),
        Column("epa_per_target", "EPA/T", "signed2", group="Efficiency"),
        Column("adot", "aDOT", "f1", group="Efficiency"),
    ]
    rows = []
    for row in result["rows"]:
        item = dict(row)
        item["passer_url"] = _player_url(row["passer_id"], season)
        item["receiver_url"] = _player_url(row["receiver_id"], season)
        item["epa_per_target_class"] = _signed_class(row.get("epa_per_target"), 0.0)
        rows.append(item)
    return Table(columns, rows, dense=True, empty="No charted pairings.")


# ---------------------------------------------------------------- packages

def _zone_columns(label_suffix: str = "") -> list[Column]:
    return [Column(f"zone_{index}", zone, "rate", group=f"On-field rate by field zone{label_suffix}")
            for index, zone in enumerate(ZONES)]


def package_table(usage: dict[str, Any]) -> Table:
    """A player's on-field rate by personnel package, with the same rate split by field zone."""
    if not usage.get("has_data"):
        return Table((), [], empty=NO_PACKAGES)
    offense = usage["side"] == "off"
    columns = [
        Column("label", "Package", "text", emphasis=True, group="Package"),
        Column("team_snaps", "Team", "int", title="Team snaps in this package", group="Usage"),
        Column("snaps", "Player", "int", title="Snaps this player was on the field in this package", group="Usage"),
        Column("on_field_rate", "On field", "rate", title="Player snaps / team snaps in the package", group="Usage"),
        Column("player_share", "% of his", "rate", title="Share of this player's snaps", group="Usage"),
        Column("pass_rate", "Pass %", "rate", group="Results"),
        Column("epa_per_snap", "EPA/play", "signed2", title="Team EPA per play with him on the field", group="Results"),
        *_zone_columns(),
    ]
    rows = []
    for item in usage["packages"]:
        row = {"label": f"{item['package']}P" if offense and item["package"] != "Other" else item["package"],
               **{key: item[key] for key in ("team_snaps", "snaps", "on_field_rate", "player_share", "pass_rate",
                                             "epa_per_snap")}}
        for index, zone in enumerate(ZONES):
            cell = item["zones"][zone]
            row[f"zone_{index}"] = cell["rate"]
            row[f"zone_{index}_sub"] = f"{cell['snaps']}/{cell['team']}" if cell["team"] else ""
            row[f"zone_{index}_class"] = heat(cell["rate"])
        row["on_field_rate_class"] = heat(item["on_field_rate"])
        rows.append(row)
    total = {"label": "All packages", "team_snaps": usage["team_snaps"], "snaps": usage["snaps"],
             "on_field_rate": usage["on_field_rate"], "player_share": 1.0}
    for index, zone_row in enumerate(usage["zones"]):
        total[f"zone_{index}"] = zone_row["rate"]
    return Table(columns, rows, total_row=total, dense=True, empty=NO_PACKAGES)


def team_package_table(result: dict[str, Any]) -> Table:
    if not result.get("has_data"):
        return Table((), [], empty=NO_PACKAGES)
    offense = result["side"] == "off"
    groups = result["groups"]
    columns = [
        Column("label", "Package", "text", emphasis=True, group="Package"),
        Column("plays", "Plays", "int", group="Usage"),
        Column("share", "%", "rate", group="Usage"),
        Column("pass_rate", "Pass %", "rate", group="Results"),
        Column("epa_per_play", "EPA/P", "signed2", group="Results"),
        Column("success", "Succ %", "rate", group="Results"),
        Column("yards_per_play", "Y/P", "f1", group="Results"),
    ]
    columns += [Column(f"zone_{index}", zone, "int", title=f"Plays from {zone.lower()}", group="Plays by field zone")
                for index, zone in enumerate(ZONES)]
    columns += [Column(f"who_{name}", name, "text", group="Most-used players (share of the package's snaps)")
                for name in groups]
    rows = []
    for item in result["rows"]:
        row = {"label": f"{item['package']}P" if offense and item["package"] != "Other" else item["package"],
               **{key: item[key] for key in ("plays", "share", "pass_rate", "epa_per_play", "success",
                                             "yards_per_play")}}
        for index, zone in enumerate(ZONES):
            row[f"zone_{index}"] = item["zones"][zone]
        for name in groups:
            row[f"who_{name}"] = " · ".join(f"{who['name'].split()[-1]} {who['rate'] * 100:.0f}%"
                                            for who in item["who"].get(name, []))
        row["epa_per_play_class"] = _signed_class(item["epa_per_play"], 0.0)
        rows.append(row)
    return Table(columns, rows, dense=True, empty=NO_PACKAGES)


# ---------------------------------------------------------------- hash and side

def _side_rows(section: dict[str, Any], league: dict[str, Any] | None, *, passing: bool) -> list[dict[str, Any]]:
    league_rows = {row["bucket"]: row for row in (league or {}).get("rows", [])} if league else {}
    rows = []
    for row in section["rows"]:
        comparison = league_rows.get(row["bucket"], {})
        rows.append({**row, "league_share": comparison.get("share"),
                     "epa_class": _signed_class(row["epa_per"], 0.0)})
    return rows


def _ratio_text(section: dict[str, Any]) -> str | None:
    if section["field_to_boundary"] is None:
        return None
    return f"{section['field_to_boundary']:.2f} : 1"


def side_view(profile: dict[str, Any], league: dict[str, Any] | None = None) -> dict[str, Any]:
    """Field / boundary bars and the hash-by-direction grid for a profile, against the league's split."""
    if not profile.get("has_data"):
        return {"has_data": False}
    view: dict[str, Any] = {"has_data": True, "season": profile["season"], "hash_share": profile["hash_share"],
                            "hash_total": profile["hash_total"]}
    for key, passing in (("passes", True), ("runs", False)):
        section = profile[key]
        league_section = league[key] if league and league.get("has_data") else None
        view[key] = {
            "has_data": bool(section["rows"]), "total": section["total"], "rows": _side_rows(section, league_section, passing=passing),
            "ratio": _ratio_text(section), "league_ratio": _ratio_text(league_section) if league_section else None,
            "field_share": section["field_share"], "league_field_share": league_section["field_share"] if league_section else None,
            "matrix": matrix_grid(section["matrix"]),
        }
    view["directions"] = profile["directions"]
    return view


def matrix_grid(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows L/M/R hash, columns left/middle/right, each with n, EPA per play and a heat bucket."""
    by_cell = {(cell["hash"], cell["loc"]): cell for cell in cells}
    total = sum(cell["n"] for cell in cells) or 1
    grid = []
    for hash_mark, label in (("L", "Left hash"), ("M", "Middle"), ("R", "Right hash")):
        row = {"hash": hash_mark, "label": label, "cells": []}
        for loc in ("left", "middle", "right"):
            cell = by_cell.get((hash_mark, loc))
            share = cell["n"] / total if cell else 0
            row["cells"].append({"loc": loc, "n": cell["n"] if cell else 0, "share": share,
                                 "epa_per": cell["epa_per"] if cell else None,
                                 "heat": heat(share * 3, 0.02) if cell else None,
                                 "epa_class": _signed_class(cell["epa_per"], 0.0) if cell else None})
        grid.append(row)
    return grid


# ---------------------------------------------------------------- orchestration

def _baseline_note(meta: dict[str, Any] | None) -> str | None:
    if meta and meta["baseline"]:
        return f"{meta['season']} baseline (no {meta['requested']} data yet)"
    return None


def player_enhanced(repository, pff, season: int, player_id: str, position: str | None) -> dict[str, Any]:
    """Everything the player page needs for routes, pairs, hash sides and packages."""
    position = (position or "").upper()
    receiver = position in {"WR", "TE", "RB", "FB"}
    passer = position == "QB"
    rusher = position in {"RB", "FB", "QB"}
    result: dict[str, Any] = {"receiver": receiver, "passer": passer, "rusher": rusher}

    routes_meta = ra.data_season(repository, season, "route") if receiver or passer else None
    if routes_meta and (receiver or passer):
        role = "passer" if passer else "receiver"
        tree = ra.route_tree(repository, routes_meta["season"], role, player_id)
        own = {}
        if role == "receiver":
            own = ra.receiver_context(repository, pff, routes_meta["season"], [player_id]).get(player_id, {})
        result["routes"] = {"role": role, "meta": routes_meta, "note": _baseline_note(routes_meta), "tree": tree,
                            "table": route_table(tree, role=role), "mix": route_depth_mix(tree), "context": own}
        pair_result = ra.pairs(repository, pff, routes_meta["season"], role, player_id)
        result["pairs"] = {"role": role, "season": routes_meta["season"], "note": _baseline_note(routes_meta),
                           "table": pair_table(pair_result, role=role, season=routes_meta["season"]),
                           "has_data": pair_result["has_data"]}
    hash_meta = ra.data_season(repository, season, "hash")
    if hash_meta:
        league = ra.league_hash_profile(repository, hash_meta["season"])
        sides = []
        for role, label in (("passer", "Throws"), ("receiver", "Targets"), ("rusher", "Runs")):
            if (role == "passer" and passer) or (role == "receiver" and receiver) or (role == "rusher" and rusher):
                view = side_view(ra.hash_profile(repository, hash_meta["season"], role, player_id), league)
                if view.get("has_data"):
                    sides.append({"role": role, "label": label, "view": view})
        if sides:
            result["sides"] = {"meta": hash_meta, "note": _baseline_note(hash_meta), "items": sides}
    package_meta = ra.data_season(repository, season, "package")
    if package_meta:
        usage = ra.player_packages(repository, package_meta["season"], player_id)
        if usage["has_data"]:
            result["packages"] = {"meta": package_meta, "note": _baseline_note(package_meta), "usage": usage,
                                  "table": package_table(usage)}
    result["has_data"] = any(key in result for key in ("routes", "pairs", "sides", "packages"))
    return result


def team_enhanced(repository, pff, season: int, team: str) -> dict[str, Any]:
    """Tendency tables for the team page: packages, hash sides, route tree and pairings."""
    result: dict[str, Any] = {}
    package_meta = ra.data_season(repository, season, "package")
    if package_meta:
        for side, key in (("off", "offense_packages"), ("def", "defense_packages")):
            table = ra.team_packages(repository, package_meta["season"], team, side=side)
            if table["has_data"]:
                result[key] = {"season": package_meta["season"], "note": _baseline_note(package_meta),
                               "table": team_package_table(table), "plays": table["plays"]}
    hash_meta = ra.data_season(repository, season, "hash")
    if hash_meta:
        league = ra.league_hash_profile(repository, hash_meta["season"])
        for scope, key in (("offense", "offense_sides"), ("defense", "defense_sides")):
            view = side_view(ra.hash_profile(repository, hash_meta["season"], scope, team), league)
            if view.get("has_data"):
                result[key] = {"view": view, "note": _baseline_note(hash_meta), "season": hash_meta["season"]}
    routes_meta = ra.data_season(repository, season, "route")
    if routes_meta:
        for scope, key in (("offense", "offense_routes"), ("defense", "defense_routes")):
            tree = ra.route_tree(repository, routes_meta["season"], scope, team)
            if tree["has_data"]:
                result[key] = {"season": routes_meta["season"], "note": _baseline_note(routes_meta), "tree": tree,
                               "table": route_table(tree, role="receiver"), "mix": route_depth_mix(tree)}
        pair_result = ra.team_pairs(repository, routes_meta["season"], team)
        if pair_result["has_data"]:
            result["pairs"] = {"season": routes_meta["season"], "note": _baseline_note(routes_meta),
                               "table": team_pair_table(pair_result, routes_meta["season"])}
    result["has_data"] = any(key != "has_data" for key in result)
    return result


def to_json(value: Any) -> Any:
    """Recursively turn tables into their API form so the enhanced packets can be served as JSON."""
    if isinstance(value, Table):
        return value.as_dict()
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    return value
