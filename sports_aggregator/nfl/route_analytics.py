"""Route, hash-side and personnel-package analytics over stored plays.

Everything here is a query over `nfl_plays` (and `nfl_player_package_snaps`), scoped to one
player, one team, or the whole league for a season. Three families:

* **Route trees**: targets, catches, yards, EPA and depth by the route the targeted receiver ran,
  for a receiver, a passer, or a team, with the league rate on the same route for comparison.
* **Hash and field side**: where the ball starts (left / middle / right hash) against where it goes
  (left / middle / right). A throw or run to the *same* side as the ball's hash is toward the
  boundary (the short side of the field); the opposite side is the field (wide) side.
  FTN hash charting starts in 2022.
* **Packages by field zone**: how often a player is on the field in each personnel package
  (11, 12, 21, 13 ...) and from each part of the field, against the team's snaps in that package.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import closing
from typing import Any, Callable, Iterable

from sports_aggregator.nfl.plays import ZONES

MIN_COVERAGE = 500
#: A field:boundary ratio from fewer charted plays than this is noise, so it is withheld.
MIN_RATIO_SAMPLE = 20
CACHE_SECONDS = 1800
_CACHE: dict[tuple, tuple[float, Any]] = {}


def clear_cache() -> None:
    """Forget cached league-level aggregates (called after a play ingest)."""
    _CACHE.clear()


def _cached(repository, key: tuple, compute: Callable[[], Any]) -> Any:
    """League-wide aggregates scan a whole season, so compute them once per process per half hour."""
    full = (repository.path, *key)
    now = time.monotonic()
    hit = _CACHE.get(full)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = compute()
    _CACHE[full] = (now, value)
    return value

ROUTE_LABELS = {
    "HITCH/CURL": "Hitch / Curl", "IN/DIG": "In / Dig", "QUICK OUT": "Quick out", "DEEP OUT": "Deep out",
    "SHALLOW CROSS/DRAG": "Shallow cross / Drag", "TEXAS/ANGLE": "Texas / Angle", "GO": "Go", "POST": "Post",
    "CORNER": "Corner", "WHEEL": "Wheel", "SLANT": "Slant", "SCREEN": "Screen", "SWING": "Swing",
    "FLAT": "Flat", "OUT": "Out",
}
ROUTE_FAMILY = {
    "SCREEN": "Behind the line", "SWING": "Behind the line", "FLAT": "Behind the line",
    "SLANT": "Short", "QUICK OUT": "Short", "HITCH/CURL": "Short", "SHALLOW CROSS/DRAG": "Short",
    "TEXAS/ANGLE": "Short", "IN/DIG": "Intermediate", "DEEP OUT": "Intermediate", "OUT": "Intermediate",
    "GO": "Deep", "POST": "Deep", "CORNER": "Deep", "WHEEL": "Deep",
}
FAMILY_ORDER = ("Behind the line", "Short", "Intermediate", "Deep", "Other")
BUCKETS = ("Field", "Boundary", "Middle", "Mid-hash left", "Mid-hash right")


def route_label(route: str) -> str:
    return ROUTE_LABELS.get(route, route.replace("/", " / ").capitalize())


def _query(repository, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    repository.initialize()
    with closing(repository._connect()) as connection:
        return [dict(row) for row in connection.execute(sql, tuple(params))]


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def identities(repository, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Name, position and team for gsis ids, from the player master table."""
    wanted = [player_id for player_id in dict.fromkeys(ids) if player_id]
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    rows = _query(repository, f"SELECT gsis_id,display_name,position,latest_team,headshot_url FROM player_master "
                              f"WHERE gsis_id IN ({marks})", wanted)
    return {row["gsis_id"]: {"name": row["display_name"], "position": row["position"],
                             "team": row["latest_team"], "headshot": row["headshot_url"]} for row in rows}


# ---------------------------------------------------------------- data availability

def data_season(repository, season: int, kind: str) -> dict[str, Any] | None:
    """The season to read `kind` ('route', 'hash' or 'package') from.

    The requested season wins once it has real coverage; otherwise the nearest earlier season
    that does is used and flagged as a baseline. None means no stored season qualifies.
    """
    sql = {
        "route": "SELECT COUNT(*) n FROM (SELECT 1 FROM nfl_plays WHERE season=? AND route IS NOT NULL "
                 "AND route!='' LIMIT ?)",
        "hash": "SELECT COUNT(*) n FROM (SELECT 1 FROM nfl_plays WHERE season=? AND starting_hash IS NOT NULL LIMIT ?)",
        "package": "SELECT COUNT(*) n FROM (SELECT 1 FROM nfl_player_package_snaps WHERE season=? LIMIT ?)",
    }[kind]

    def compute() -> dict[str, Any] | None:
        for candidate in (season, season - 1, season - 2):
            if _query(repository, sql, (candidate, MIN_COVERAGE))[0]["n"] >= MIN_COVERAGE:
                return {"season": candidate, "requested": season, "baseline": candidate != season}
        return None

    return _cached(repository, ("data_season", kind, season, MIN_COVERAGE), compute)


# ---------------------------------------------------------------- route trees

_ROUTE_SQL = """
SELECT route, COUNT(*) targets, SUM(COALESCE(is_complete,0)) receptions,
       SUM(CASE WHEN is_complete=1 THEN COALESCE(yards_gained,0) ELSE 0 END) yards,
       SUM(is_pass_td) touchdowns, SUM(is_interception) interceptions, SUM(COALESCE(epa,0)) epa,
       AVG(air_yards) adot, SUM(CASE WHEN is_complete=1 THEN COALESCE(yac,0) ELSE 0 END) yac,
       SUM(COALESCE(is_drop,0)) drops, SUM(COALESCE(is_contested,0)) contested, SUM(first_down) first_downs,
       AVG(cpoe) cpoe
FROM nfl_plays
WHERE season=? AND is_pass=1 AND is_sack=0 AND route IS NOT NULL AND route!='' {scope}
GROUP BY route"""

SCOPES = {
    "receiver": ("receiver_id=?", "Targets"),
    "passer": ("passer_id=?", "Attempts"),
    "offense": ("posteam=?", "Targets"),
    "defense": ("defteam=?", "Targets allowed"),
}


def _route_row(row: dict[str, Any]) -> dict[str, Any]:
    targets = row["targets"]
    return {
        "route": row.get("route"), "label": route_label(row["route"]) if row.get("route") else "All routes",
        "family": ROUTE_FAMILY.get(row.get("route") or "", "Other"), "targets": targets,
        "receptions": row["receptions"], "yards": row["yards"], "touchdowns": row["touchdowns"],
        "interceptions": row["interceptions"], "epa": row["epa"],
        "catch_rate": _ratio(row["receptions"], targets), "yards_per_target": _ratio(row["yards"], targets),
        "yards_per_reception": _ratio(row["yards"], row["receptions"]), "epa_per_target": _ratio(row["epa"], targets),
        "adot": row["adot"], "yac_per_reception": _ratio(row["yac"], row["receptions"]),
        "drops": row["drops"], "contested": row["contested"], "first_downs": row["first_downs"],
        "cpoe": row["cpoe"],
    }


def _combine(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = {key: 0 for key in ("targets", "receptions", "yards", "touchdowns", "interceptions", "epa",
                                "yac", "drops", "contested", "first_downs")}
    weighted_adot = weighted_cpoe = 0.0
    for row in rows:
        for key in total:
            total[key] += row.get(key) or 0
        weighted_adot += (row.get("adot") or 0) * row["targets"]
        weighted_cpoe += (row.get("cpoe") or 0) * row["targets"]
    total["route"] = None
    total["adot"] = weighted_adot / total["targets"] if total["targets"] else None
    total["cpoe"] = weighted_cpoe / total["targets"] if total["targets"] else None
    return total


def route_tree(repository, season: int, scope: str, scope_id: str) -> dict[str, Any]:
    """Routes for a receiver/passer/team with the league rate on each route.

    `scope` is one of receiver, passer, offense or defense. Rows are ordered by volume; `family`
    groups them by depth (behind the line, short, intermediate, deep) and `families` totals each.
    """
    clause, noun = SCOPES[scope]
    rows = _query(repository, _ROUTE_SQL.format(scope=f"AND {clause}"), (season, scope_id))
    league = _cached(repository, ("league_routes", season), lambda: {
        row["route"]: _route_row(row) for row in _query(repository, _ROUTE_SQL.format(scope=""), (season,))})
    if not rows:
        return {"has_data": False, "noun": noun}
    total_targets = sum(row["targets"] for row in rows)
    tree = []
    for raw in sorted(rows, key=lambda row: row["targets"], reverse=True):
        row = _route_row(raw)
        row["share"] = row["targets"] / total_targets
        comparison = league.get(row["route"], {})
        row["league_catch_rate"] = comparison.get("catch_rate")
        row["league_epa_per_target"] = comparison.get("epa_per_target")
        row["league_yards_per_target"] = comparison.get("yards_per_target")
        row["epa_vs_league"] = (None if row["epa_per_target"] is None or comparison.get("epa_per_target") is None
                                else row["epa_per_target"] - comparison["epa_per_target"])
        tree.append(row)
    total = _route_row(_combine(rows))
    total["share"] = 1.0
    families = []
    for name in FAMILY_ORDER:
        members = [raw for raw in rows if ROUTE_FAMILY.get(raw["route"], "Other") == name]
        if members:
            family = _route_row(_combine(members))
            family["label"], family["share"] = name, family["targets"] / total_targets
            families.append(family)
    return {"has_data": True, "noun": noun, "rows": tree, "total": total, "families": families,
            "season": season}


# ---------------------------------------------------------------- pairs

_PAIR_SQL = """
SELECT {other} other_id, COUNT(*) targets, SUM(COALESCE(is_complete,0)) receptions,
       SUM(CASE WHEN is_complete=1 THEN COALESCE(yards_gained,0) ELSE 0 END) yards,
       SUM(is_pass_td) touchdowns, SUM(is_interception) interceptions, SUM(COALESCE(epa,0)) epa,
       AVG(air_yards) adot, SUM(COALESCE(is_drop,0)) drops, SUM(first_down) first_downs
FROM nfl_plays
WHERE season=? AND is_pass=1 AND is_sack=0 AND {fixed}=? AND {other} IS NOT NULL
GROUP BY {other} ORDER BY targets DESC LIMIT ?"""


def _pair_scope(role: str) -> tuple[str, str]:
    """(fixed column, other column) for a role: receivers pair with passers and vice versa."""
    return ("receiver_id", "passer_id") if role == "receiver" else ("passer_id", "receiver_id")


def _top_routes(repository, season: int, fixed: str, fixed_id: str, other: str, other_ids: list[str],
                per_pair: int = 3) -> dict[str, list[tuple[str, int]]]:
    if not other_ids:
        return {}
    marks = ",".join("?" for _ in other_ids)
    rows = _query(repository, f"SELECT {other} other_id, route, COUNT(*) n FROM nfl_plays "
                              f"WHERE season=? AND is_pass=1 AND {fixed}=? AND {other} IN ({marks}) "
                              f"AND route IS NOT NULL AND route!='' GROUP BY {other}, route",
                  (season, fixed_id, *other_ids))
    by_pair: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in rows:
        by_pair[row["other_id"]].append((row["route"], row["n"]))
    return {key: sorted(routes, key=lambda item: item[1], reverse=True)[:per_pair] for key, routes in by_pair.items()}


def receiver_context(repository, pff, season: int, receiver_ids: list[str]) -> dict[str, dict[str, Any]]:
    """PFF route grade / yards per route run and NGS separation / cushion for receivers."""
    context: dict[str, dict[str, Any]] = {player_id: {} for player_id in receiver_ids}
    if not receiver_ids:
        return context
    if pff is not None:
        for metric, key in (("grades_pass_route", "pff_route_grade"), ("yprr", "pff_yprr"),
                            ("routes", "pff_routes")):
            try:
                rows = pff.leaders(season, "receiving_summary", metric, limit=None)
            except Exception:
                rows = []
            for row in rows:
                if row.get("gsis_id") in context:
                    context[row["gsis_id"]][key] = row["value"]
    marks = ",".join("?" for _ in receiver_ids)
    ngs = _query(repository, f"SELECT player_id, metric, SUM(value) value FROM player_weekly_stats "
                             f"WHERE season=? AND player_id IN ({marks}) AND metric IN "
                             f"('targets','ngs_rec_separation_wtd','ngs_rec_cushion_wtd') "
                             f"GROUP BY player_id, metric", (season, *receiver_ids))
    sums: dict[str, dict[str, float]] = defaultdict(dict)
    for row in ngs:
        sums[row["player_id"]][row["metric"]] = row["value"]
    for player_id, values in sums.items():
        targets = values.get("targets")
        if targets:
            if "ngs_rec_separation_wtd" in values:
                context[player_id]["ngs_separation"] = values["ngs_rec_separation_wtd"] / targets
            if "ngs_rec_cushion_wtd" in values:
                context[player_id]["ngs_cushion"] = values["ngs_rec_cushion_wtd"] / targets
    return context


def pairs(repository, pff, season: int, role: str, player_id: str, *, limit: int = 12) -> dict[str, Any]:
    """Passer-target pairs for a player: a receiver's passers, or a passer's targets."""
    fixed, other = _pair_scope(role)
    rows = _query(repository, _PAIR_SQL.format(other=other, fixed=fixed), (season, player_id, limit))
    if not rows:
        return {"has_data": False}
    ids = [row["other_id"] for row in rows]
    names = identities(repository, ids)
    routes = _top_routes(repository, season, fixed, player_id, other, ids)
    context = receiver_context(repository, pff, season, ids) if role == "passer" else {}
    output = []
    total_targets = sum(row["targets"] for row in rows)
    for row in rows:
        who = names.get(row["other_id"], {})
        item = {
            "player_id": row["other_id"], "name": who.get("name") or row["other_id"],
            "position": who.get("position"), "team": who.get("team"),
            "targets": row["targets"], "share": row["targets"] / total_targets,
            "receptions": row["receptions"], "yards": row["yards"], "touchdowns": row["touchdowns"],
            "interceptions": row["interceptions"], "catch_rate": _ratio(row["receptions"], row["targets"]),
            "yards_per_target": _ratio(row["yards"], row["targets"]),
            "epa": row["epa"], "epa_per_target": _ratio(row["epa"], row["targets"]), "adot": row["adot"],
            "drops": row["drops"], "first_downs": row["first_downs"],
            "top_routes": [(route_label(route), count) for route, count in routes.get(row["other_id"], [])],
        }
        item.update(context.get(row["other_id"], {}))
        output.append(item)
    return {"has_data": True, "role": role, "rows": output, "season": season}


def team_pairs(repository, season: int, team: str, *, limit: int = 12) -> dict[str, Any]:
    """The team's most frequent passer -> target combinations."""
    rows = _query(repository, """
        SELECT passer_id, receiver_id, COUNT(*) targets, SUM(COALESCE(is_complete,0)) receptions,
               SUM(CASE WHEN is_complete=1 THEN COALESCE(yards_gained,0) ELSE 0 END) yards,
               SUM(is_pass_td) touchdowns, SUM(COALESCE(epa,0)) epa, AVG(air_yards) adot
        FROM nfl_plays WHERE season=? AND posteam=? AND is_pass=1 AND is_sack=0
          AND passer_id IS NOT NULL AND receiver_id IS NOT NULL
        GROUP BY passer_id, receiver_id ORDER BY targets DESC LIMIT ?""", (season, team, limit))
    if not rows:
        return {"has_data": False}
    names = identities(repository, [value for row in rows for value in (row["passer_id"], row["receiver_id"])])
    return {"has_data": True, "season": season, "rows": [{
        "passer": names.get(row["passer_id"], {}).get("name") or row["passer_id"], "passer_id": row["passer_id"],
        "receiver": names.get(row["receiver_id"], {}).get("name") or row["receiver_id"],
        "receiver_id": row["receiver_id"], "position": names.get(row["receiver_id"], {}).get("position"),
        "targets": row["targets"], "receptions": row["receptions"], "yards": row["yards"],
        "touchdowns": row["touchdowns"], "catch_rate": _ratio(row["receptions"], row["targets"]),
        "yards_per_target": _ratio(row["yards"], row["targets"]),
        "epa_per_target": _ratio(row["epa"], row["targets"]), "adot": row["adot"],
    } for row in rows]}


# ---------------------------------------------------------------- hash and field side

_HASH_SQL = """
SELECT is_pass, starting_hash hash, CASE WHEN is_pass=1 THEN pass_location ELSE run_location END loc,
       COUNT(*) n, SUM(COALESCE(epa,0)) epa, SUM(COALESCE(yards_gained,0)) yards, SUM(COALESCE(success,0)) success,
       SUM(COALESCE(is_complete,0)) completions, SUM(is_touchdown) touchdowns, AVG(air_yards) adot
FROM nfl_plays
WHERE season=? AND starting_hash IS NOT NULL {scope}
  AND ((is_pass=1 AND is_sack=0 AND pass_location IS NOT NULL)
    OR (is_rush=1 AND run_location IS NOT NULL {scrambles}))
GROUP BY is_pass, starting_hash, loc"""

HASH_SCOPES = {
    "passer": "passer_id=?", "receiver": "receiver_id=?", "rusher": "rusher_id=?",
    "offense": "posteam=?", "defense": "defteam=?", None: "",
}


def side_bucket(hash_mark: str | None, location: str | None) -> str | None:
    """Field / Boundary / Middle for a snap from the left or right hash; mid-hash snaps stay left/right."""
    if not location or hash_mark not in {"L", "M", "R"}:
        return None
    if location == "middle":
        return "Middle"
    if hash_mark == "M":
        return "Mid-hash left" if location == "left" else "Mid-hash right"
    hash_side = "left" if hash_mark == "L" else "right"
    return "Boundary" if location == hash_side else "Field"


def _summarize(rows: list[dict[str, Any]], *, passing: bool) -> dict[str, Any]:
    buckets: dict[str, dict[str, float]] = {name: defaultdict(float) for name in BUCKETS}
    matrix: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        bucket = side_bucket(row["hash"], row["loc"])
        if bucket is None:
            continue
        for target in (buckets[bucket], matrix[(row["hash"], row["loc"])]):
            target["n"] += row["n"]
            target["epa"] += row["epa"]
            target["yards"] += row["yards"]
            target["success"] += row["success"]
            target["completions"] += row["completions"]
            target["touchdowns"] += row["touchdowns"]
            target["adot_sum"] += (row["adot"] or 0) * row["n"]
    classified = sum(bucket["n"] for bucket in buckets.values())
    output = []
    for name in BUCKETS:
        bucket = buckets[name]
        if not bucket["n"]:
            continue
        n = bucket["n"]
        output.append({"bucket": name, "n": int(n), "share": n / classified, "epa_per": bucket["epa"] / n,
                       "yards_per": bucket["yards"] / n, "success": bucket["success"] / n,
                       "completion_rate": bucket["completions"] / n if passing else None,
                       "adot": bucket["adot_sum"] / n if passing else None, "touchdowns": int(bucket["touchdowns"])})
    field, boundary = buckets["Field"]["n"], buckets["Boundary"]["n"]
    reliable = field + boundary >= MIN_RATIO_SAMPLE
    cells = [{"hash": hash_mark, "loc": loc, "n": int(cell["n"]), "epa_per": cell["epa"] / cell["n"]}
             for (hash_mark, loc), cell in sorted(matrix.items()) if cell["n"]]
    return {"rows": output, "total": int(classified), "field": int(field), "boundary": int(boundary),
            "field_to_boundary": field / boundary if boundary and reliable else None,
            "field_share": field / (field + boundary) if reliable else None, "matrix": cells}


def hash_profile(repository, season: int, scope: str | None, scope_id: str | None = None) -> dict[str, Any]:
    """Field/boundary splits of passes and runs, plus where the offense lines up (hash share).

    `scope` is passer, receiver, rusher, offense, defense, or None for the whole league. Team and league
    run splits leave out quarterback scrambles, which are not designed run direction.
    """
    clause = HASH_SCOPES[scope]
    scope_sql = f"AND {clause}" if clause else ""
    params = (season, scope_id) if clause else (season,)
    scrambles = "" if scope in {"passer", "rusher"} else "AND is_scramble=0"
    rows = _query(repository, _HASH_SQL.format(scope=scope_sql, scrambles=scrambles), params)
    if not rows:
        return {"has_data": False}
    passes = _summarize([row for row in rows if row["is_pass"] == 1], passing=True)
    runs = _summarize([row for row in rows if row["is_pass"] == 0], passing=False)
    lineup_scope = {"passer": "passer_id=?", "receiver": "receiver_id=?", "rusher": "rusher_id=?",
                    "offense": "posteam=?", "defense": "defteam=?", None: "1=1"}[scope]
    lineup = _query(repository, f"SELECT starting_hash hash, COUNT(*) n FROM nfl_plays WHERE season=? "
                                f"AND starting_hash IS NOT NULL AND {lineup_scope} GROUP BY starting_hash",
                    (season, scope_id) if clause else (season,))
    total = sum(row["n"] for row in lineup) or 1
    hash_share = {row["hash"]: row["n"] / total for row in lineup}
    directions = _query(repository, f"""SELECT is_pass, CASE WHEN is_pass=1 THEN pass_location ELSE run_location END loc,
                                               COUNT(*) n FROM nfl_plays WHERE season=? AND {lineup_scope}
                                         AND ((is_pass=1 AND is_sack=0 AND pass_location IS NOT NULL)
                                           OR (is_rush=1 AND run_location IS NOT NULL {scrambles}))
                                         GROUP BY is_pass, loc""", (season, scope_id) if clause else (season,))
    return {"has_data": bool(passes["total"] or runs["total"]), "season": season, "passes": passes, "runs": runs,
            "hash_share": hash_share, "hash_total": total,
            "directions": {"pass": {row["loc"]: row["n"] for row in directions if row["is_pass"] == 1},
                           "run": {row["loc"]: row["n"] for row in directions if row["is_pass"] == 0}}}


def league_hash_profile(repository, season: int) -> dict[str, Any]:
    """The league-wide hash/side profile, cached; the baseline every player and team is shown against."""
    return _cached(repository, ("league_hash", season), lambda: hash_profile(repository, season, None))


# ---------------------------------------------------------------- packages by field zone

def _team_package_counts(repository, season: int, team: str, side: str) -> dict[tuple[str, str], int]:
    column, group = ("posteam", "offense_group") if side == "off" else ("defteam", "defense_package")
    rows = _query(repository, f"SELECT {group} package, zone, COUNT(*) n FROM nfl_plays WHERE season=? AND {column}=? "
                              f"AND {group} IS NOT NULL AND zone IS NOT NULL GROUP BY {group}, zone", (season, team))
    return {(row["package"], row["zone"]): row["n"] for row in rows}


PACKAGE_ORDER = {"off": ("11", "12", "21", "13", "22", "10", "01", "02", "20", "23", "Other"),
                 "def": ("Base", "Nickel", "Dime")}


def _package_sort(side: str, package: str) -> int:
    order = PACKAGE_ORDER[side]
    return order.index(package) if package in order else len(order)


def player_packages(repository, season: int, player_id: str) -> dict[str, Any]:
    """On-field rate by package and zone for one player, against the team's snaps in the same spots."""
    rows = _query(repository, """
        SELECT team, position, side, package, zone, SUM(snaps) snaps, SUM(pass_snaps) pass_snaps,
               SUM(rush_snaps) rush_snaps, SUM(epa_sum) epa_sum
        FROM nfl_player_package_snaps WHERE season=? AND player_id=? GROUP BY team, position, side, package, zone""",
                  (season, player_id))
    if not rows:
        return {"has_data": False}
    by_side: dict[str, int] = defaultdict(int)
    by_team: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        by_side[row["side"]] += row["snaps"]
        by_team[(row["side"], row["team"])] += row["snaps"]
    side = max(by_side, key=by_side.get)
    team = max((key for key in by_team if key[0] == side), key=by_team.get)[1]
    mine = [row for row in rows if row["side"] == side and row["team"] == team]
    positions = sorted({row["position"] for row in mine if row["position"]})
    team_counts = _team_package_counts(repository, season, team, side)
    team_total = sum(team_counts.values())
    player_total = sum(row["snaps"] for row in mine)

    packages: dict[str, dict[str, Any]] = {}
    for row in mine:
        entry = packages.setdefault(row["package"], {
            "package": row["package"], "snaps": 0, "pass_snaps": 0, "rush_snaps": 0, "epa_sum": 0.0, "zones": {}})
        entry["snaps"] += row["snaps"]
        entry["pass_snaps"] += row["pass_snaps"]
        entry["rush_snaps"] += row["rush_snaps"]
        entry["epa_sum"] += row["epa_sum"]
        zone = entry["zones"].setdefault(row["zone"], {"snaps": 0})
        zone["snaps"] += row["snaps"]
    output = []
    for package, entry in packages.items():
        team_snaps = sum(n for (name, _zone), n in team_counts.items() if name == package)
        zones = {}
        for zone in ZONES:
            own = entry["zones"].get(zone, {}).get("snaps", 0)
            team_zone = team_counts.get((package, zone), 0)
            zones[zone] = {"snaps": own, "team": team_zone, "rate": _ratio(own, team_zone)}
        output.append({
            "package": package, "snaps": entry["snaps"], "team_snaps": team_snaps,
            "on_field_rate": _ratio(entry["snaps"], team_snaps), "team_share": _ratio(team_snaps, team_total),
            "player_share": _ratio(entry["snaps"], player_total), "pass_rate": _ratio(entry["pass_snaps"], entry["snaps"]),
            "epa_per_snap": _ratio(entry["epa_sum"], entry["snaps"]), "zones": zones,
        })
    output.sort(key=lambda item: (-(item["team_snaps"] or 0), _package_sort(side, item["package"])))
    zone_rows = []
    for zone in ZONES:
        own = sum(item["zones"][zone]["snaps"] for item in output)
        team_zone = sum(n for (_package, name), n in team_counts.items() if name == zone)
        zone_rows.append({"zone": zone, "snaps": own, "team": team_zone, "rate": _ratio(own, team_zone),
                          "share": _ratio(own, player_total)})
    return {"has_data": True, "season": season, "team": team, "side": side, "positions": positions,
            "snaps": player_total, "team_snaps": team_total, "on_field_rate": _ratio(player_total, team_total),
            "packages": output, "zones": zone_rows}


def team_packages(repository, season: int, team: str, *, side: str = "off") -> dict[str, Any]:
    """A team's snaps by package: share, pass rate, efficiency, zone mix, and who plays in each."""
    column, group = ("posteam", "offense_group") if side == "off" else ("defteam", "defense_package")
    rows = _query(repository, f"""SELECT {group} package, zone, COUNT(*) n, SUM(is_pass) passes, SUM(COALESCE(epa,0)) epa,
                                         SUM(COALESCE(success,0)) success, SUM(COALESCE(yards_gained,0)) yards
                                  FROM nfl_plays WHERE season=? AND {column}=? AND {group} IS NOT NULL
                                  GROUP BY {group}, zone""", (season, team))
    if not rows:
        return {"has_data": False}
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = totals.setdefault(row["package"], {"n": 0, "passes": 0, "epa": 0.0, "success": 0.0, "yards": 0.0, "zones": {}})
        entry["n"] += row["n"]
        entry["passes"] += row["passes"]
        entry["epa"] += row["epa"]
        entry["success"] += row["success"]
        entry["yards"] += row["yards"]
        if row["zone"]:
            entry["zones"][row["zone"]] = row["n"]
    grand = sum(entry["n"] for entry in totals.values())
    people = _query(repository, """SELECT package, player_id, position, SUM(snaps) snaps FROM nfl_player_package_snaps
                                   WHERE season=? AND team=? AND side=? GROUP BY package, player_id, position""",
                    (season, team, side))
    names = identities(repository, [row["player_id"] for row in people])
    by_package: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in people:
        by_package[row["package"]].append(row)
    groups = ({"WR": ("WR",), "TE": ("TE",), "RB": ("RB", "FB")} if side == "off"
              else {"DL": ("DT", "DE", "NT", "DL"), "LB": ("ILB", "MLB", "OLB", "LB"), "DB": ("CB", "S", "FS", "SS", "DB")})
    output = []
    for package, entry in totals.items():
        who: dict[str, list[dict[str, Any]]] = {}
        for group_name, positions in groups.items():
            ranked = sorted((row for row in by_package.get(package, []) if row["position"] in positions),
                            key=lambda row: row["snaps"], reverse=True)[:3]
            who[group_name] = [{"name": names.get(row["player_id"], {}).get("name") or row["player_id"],
                                "player_id": row["player_id"], "rate": row["snaps"] / entry["n"]} for row in ranked]
        output.append({
            "package": package, "plays": entry["n"], "share": entry["n"] / grand,
            "pass_rate": entry["passes"] / entry["n"], "epa_per_play": entry["epa"] / entry["n"],
            "success": entry["success"] / entry["n"], "yards_per_play": entry["yards"] / entry["n"],
            "zones": {zone: entry["zones"].get(zone, 0) for zone in ZONES}, "who": who,
        })
    output.sort(key=lambda item: (-item["plays"], _package_sort(side, item["package"])))
    return {"has_data": True, "season": season, "side": side, "plays": grand, "rows": output, "groups": list(groups)}
