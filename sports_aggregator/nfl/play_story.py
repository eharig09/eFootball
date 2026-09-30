"""Game-page views over stored plays: win-probability curve, drive chart, play-by-play table."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository

WP_WIDTH, WP_HEIGHT = 760, 250
WP_LEFT, WP_RIGHT, WP_TOP, WP_BOTTOM = 40, 12, 10, 214

DRIVE_RESULTS = {
    "Touchdown": ("TD", "td"), "Field goal": ("FG", "fg"), "Missed field goal": ("FG miss", "miss"),
    "Punt": ("Punt", "punt"), "Turnover": ("TO", "to"), "Turnover on downs": ("Downs", "to"),
    "Opp touchdown": ("TO", "to"), "Safety": ("Safety", "to"), "End of half": ("End", "punt"),
}
COVERAGE_LABELS = {"COVER_0": "Cover 0", "COVER_1": "Cover 1", "COVER_2": "Cover 2", "COVER_3": "Cover 3",
                   "COVER_4": "Cover 4", "COVER_6": "Cover 6", "COVER_9": "Cover 9", "2_MAN": "2-Man",
                   "COMBO": "Combo", "BLOWN": "Blown"}
FORMATION_LABELS = {"SHOTGUN": "Shotgun", "UNDER CENTER": "Under center", "PISTOL": "Pistol"}


def game_plays(repository: NFLRepository, game_id: str) -> list[dict[str, Any]]:
    repository.initialize()
    with closing(repository._connect()) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM nfl_plays WHERE game_id=? ORDER BY play_id", (game_id,))]


def _segments(points: list[tuple[float, float, float]]) -> list[dict[str, Any]]:
    """Split the win-probability line at 50% so each run is drawn in the leading team's color.

    `points` are (x, y, home_wp). Crossings are interpolated so the color change lands exactly on
    the midline instead of bleeding into the wrong side.
    """
    segments: list[dict[str, Any]] = []
    current: list[tuple[float, float]] = []
    side = None
    mid_y = round((WP_TOP + WP_BOTTOM) / 2, 1)
    for index, (x, y, wp) in enumerate(points):
        point_side = "home" if wp >= 0.5 else "away"
        if side is None:
            side, current = point_side, [(x, y)]
        elif point_side == side:
            current.append((x, y))
        else:
            previous_x, previous_y, previous_wp = points[index - 1]
            span = (wp - previous_wp) or 1e-9
            fraction = (0.5 - previous_wp) / span
            cross_x = round(previous_x + (x - previous_x) * fraction, 1)
            current.append((cross_x, mid_y))
            segments.append({"side": side, "points": " ".join(f"{a},{b}" for a, b in current)})
            side, current = point_side, [(cross_x, mid_y), (x, y)]
    if len(current) > 1 or (current and not segments):
        segments.append({"side": side or "home",
                         "points": " ".join(f"{a},{b}" for a, b in (current * 2 if len(current) == 1 else current))})
    return segments


def _drive_markers(plays: list[dict[str, Any]]) -> dict[tuple[str, int], int]:
    """Index of the last stored play of each drive, keyed by (offense, drive)."""
    last: dict[tuple[str, int], int] = {}
    for index, play in enumerate(plays):
        if play.get("drive") is not None:
            last[(play["posteam"], play["drive"])] = index
    return last


def win_probability(plays: list[dict[str, Any]], home: str, away: str) -> dict[str, Any]:
    usable = [(index, play) for index, play in enumerate(plays) if play.get("home_wp") is not None]
    if len(usable) < 2:
        return {"has_data": False}
    count = len(usable)
    plot_width = WP_WIDTH - WP_LEFT - WP_RIGHT

    def x_of(position: int) -> float:
        return round(WP_LEFT + position / (count - 1) * plot_width, 1)

    def y_of(wp: float) -> float:
        return round(WP_TOP + (1 - wp) * (WP_BOTTOM - WP_TOP), 1)

    position_of = {index: pos for pos, (index, _) in enumerate(usable)}
    points = [(x_of(pos), y_of(play["home_wp"]), play["home_wp"]) for pos, (_, play) in enumerate(usable)]

    quarters = []
    seen = set()
    for pos, (_, play) in enumerate(usable):
        quarter = play.get("qtr")
        if quarter and quarter not in seen:
            seen.add(quarter)
            quarters.append({"x": x_of(pos), "label": "OT" if quarter >= 5 else f"Q{quarter}"})

    markers = []
    for (offense, _drive), index in _drive_markers(plays).items():
        result = DRIVE_RESULTS.get(plays[index].get("drive_result") or "", ("", ""))[1]
        if result in {"td", "fg"} and index in position_of:
            markers.append({"x": x_of(position_of[index]), "kind": result.upper(),
                            "team": offense, "side": "home" if offense == home else "away"})
    return {
        "has_data": True, "width": WP_WIDTH, "height": WP_HEIGHT, "left": WP_LEFT, "right": WP_WIDTH - WP_RIGHT,
        "top": WP_TOP, "bottom": WP_BOTTOM, "mid_y": round((WP_TOP + WP_BOTTOM) / 2, 1),
        "segments": _segments(points), "quarters": quarters, "markers": markers,
        "home": home, "away": away, "position_of": position_of, "x_of": x_of, "y_of": y_of,
        "label_y": WP_HEIGHT - 6,
    }


def _situation(play: dict[str, Any]) -> tuple[str, str]:
    quarter = play.get("qtr")
    clock = play.get("clock") or ""
    when = f"{'OT' if quarter and quarter >= 5 else 'Q' + str(quarter or '?')} · {clock}".strip(" ·")
    down = play.get("down")
    if down:
        suffix = {1: "ST", 2: "ND", 3: "RD"}.get(down, "TH")
        where = f"{down}{suffix} & {play.get('ydstogo') if play.get('ydstogo') is not None else '?'}"
    else:
        where = "No down"
    if play.get("yard_line"):
        where += f" at {play['yard_line']}"
    return when, where


def biggest_swings(plays: list[dict[str, Any]], chart: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    ranked = sorted((play for play in plays if play.get("wpa") is not None),
                    key=lambda play: abs(play["wpa"]), reverse=True)[:limit]
    swings = []
    for rank, play in enumerate(ranked, start=1):
        index = plays.index(play)
        gained = play["posteam"] if play["wpa"] > 0 else play["defteam"]
        when, where = _situation(play)
        swing = {"rank": rank, "team": gained, "pct": round(abs(play["wpa"]) * 100),
                 "description": play.get("description") or "", "when": when, "where": where}
        position = chart.get("position_of", {}).get(index) if chart.get("has_data") else None
        if position is not None:
            swing["x"] = chart["x_of"](position)
            swing["y"] = chart["y_of"](play["home_wp"])
        swings.append(swing)
    return swings


def drive_chart(plays: list[dict[str, Any]], away: str, home: str) -> dict[str, Any]:
    drives: dict[tuple[str, int], dict[str, Any]] = {}
    order: list[tuple[str, int]] = []
    for play in plays:
        if play.get("drive") is None:
            continue
        key = (play["posteam"], play["drive"])
        if key not in drives:
            drives[key] = {"plays": 0, "yards": 0.0, "result": None, "drive": play["drive"]}
            order.append(key)
        item = drives[key]
        item["plays"] += 1
        item["yards"] += play.get("yards_gained") or 0
        item["result"] = play.get("drive_result") or item["result"]
    rows = []
    for team in (away, home):
        items = []
        for key in order:
            if key[0] != team:
                continue
            label, klass = DRIVE_RESULTS.get(drives[key]["result"] or "", ("—", "punt"))
            items.append({"plays": drives[key]["plays"], "yards": int(round(drives[key]["yards"])),
                          "label": label, "klass": klass})
        totals = {"td": sum(i["klass"] == "td" for i in items), "fg": sum(i["klass"] == "fg" for i in items),
                  "to": sum(i["klass"] == "to" for i in items)}
        rows.append({"team": team, "drives": items, "totals": totals})
    return {"has_data": any(row["drives"] for row in rows), "rows": rows}


def _offense_called(play: dict[str, Any]) -> list[str]:
    tags = []
    if play.get("offense_group"):
        tags.append(f"{play['offense_group']}P")
    if play.get("offense_formation"):
        tags.append(FORMATION_LABELS.get(play["offense_formation"], play["offense_formation"].title()))
    for key, label in (("play_action", "Play action"), ("rpo", "RPO"), ("screen", "Screen"),
                       ("motion", "Motion"), ("no_huddle", "No huddle")):
        if play.get(key):
            tags.append(label)
    return tags


def _defense_played(play: dict[str, Any]) -> tuple[list[str], list[str]]:
    base, hot = [], []
    if play.get("defense_package"):
        base.append(play["defense_package"])
    if play.get("box") in {"Light", "Heavy"}:
        base.append(f"{play['box']} box")
    if play.get("coverage"):
        label = COVERAGE_LABELS.get(play["coverage"], play["coverage"].title())
        kind = {"ZONE_COVERAGE": " Zone", "MAN_COVERAGE": " Man"}.get(play.get("man_zone") or "", "")
        base.append(label + kind)
    if (play.get("blitzers") or 0) > 0 or (play.get("pass_rushers") or 0) >= 5:
        hot.append("Blitz")
    if play.get("was_pressure"):
        hot.append("Pressure")
    return base, hot


def play_table(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scoring_ends = {index for index in _drive_markers(plays).values()
                    if DRIVE_RESULTS.get(plays[index].get("drive_result") or "", ("", ""))[1] in {"td", "fg"}}
    rows = []
    for index, play in enumerate(plays):
        yards = play.get("yards_gained") or 0
        kinds = []
        if play.get("is_touchdown") or index in scoring_ends:
            kinds.append("scoring")
        if (play["is_pass"] and yards >= 20) or (play["is_rush"] and yards >= 10):
            kinds.append("explosive")
        if play.get("is_turnover"):
            kinds.append("turnover")
        when, where = _situation(play)
        defense, hot = _defense_played(play)
        rows.append({
            "when": when, "where": where, "offense": play["posteam"], "description": play.get("description") or "",
            "called": _offense_called(play), "defense": defense, "hot": hot,
            "epa": play.get("epa"), "wpa": None if play.get("wpa") is None else play["wpa"] * 100,
            "kinds": " ".join(kinds), "big_wpa": play.get("wpa") is not None and abs(play["wpa"]) >= 0.05,
        })
    return rows


def play_story(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any]:
    plays = game_plays(repository, game["game_id"])
    if not plays:
        return {"has_data": False}
    away, home = game["away_team"], game["home_team"]
    chart = win_probability(plays, home, away)
    table = play_table(plays)
    counts = {name: sum(name in row["kinds"].split() for row in table)
              for name in ("scoring", "explosive", "turnover")}
    chart_public = {key: value for key, value in chart.items() if key not in {"position_of", "x_of", "y_of"}}
    return {
        "has_data": True, "wp": chart_public, "swings": biggest_swings(plays, chart),
        "drives": drive_chart(plays, away, home), "plays": table,
        "filters": [("all", "All plays", len(table)), ("scoring", "Scoring", counts["scoring"]),
                    ("explosive", "Explosive", counts["explosive"]), ("turnover", "Turnovers", counts["turnover"])],
        "enhanced": any(play.get("offense_group") for play in plays),
        "ftn": any(play.get("motion") is not None for play in plays),
    }
