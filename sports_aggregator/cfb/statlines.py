"""Pivot long-form CFBD player statistics into conventional stat lines.

``player_season_stats`` stores one row per (season, category, stat_type), which
is the right shape for ingestion and the wrong shape for reading: a quarterback
renders as fifteen near-identical rows of ``Season | Team | passing | YDS |
3120``.

This module turns those rows into the box-score layout a football reader already
knows -- one row per season with ordered, labeled columns -- without inventing
any value CFBD did not publish.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sports_aggregator.tables import Column, Table


#: (stat_type, header, format, tooltip). Order defines rendered column order.
CATEGORY_SPECS: dict[str, dict[str, Any]] = {
    "passing": {
        "label": "Passing",
        "sort": "YDS",
        "columns": [
            ("COMPLETIONS", "CMP", "int", "Completions"),
            ("ATT", "ATT", "int", "Attempts"),
            ("PCT", "PCT", "rate", "Completion percentage"),
            ("YDS", "YDS", "int", "Passing yards"),
            ("YPA", "YPA", "f1", "Yards per attempt"),
            ("TD", "TD", "int", "Touchdowns"),
            ("INT", "INT", "int", "Interceptions thrown"),
        ],
    },
    "rushing": {
        "label": "Rushing",
        "sort": "YDS",
        "columns": [
            ("CAR", "CAR", "int", "Carries"),
            ("YDS", "YDS", "int", "Rushing yards"),
            ("YPC", "AVG", "f1", "Yards per carry"),
            ("TD", "TD", "int", "Touchdowns"),
            ("LONG", "LNG", "int", "Longest rush"),
        ],
    },
    "receiving": {
        "label": "Receiving",
        "sort": "YDS",
        "columns": [
            ("REC", "REC", "int", "Receptions"),
            ("YDS", "YDS", "int", "Receiving yards"),
            ("YPR", "AVG", "f1", "Yards per reception"),
            ("TD", "TD", "int", "Touchdowns"),
            ("LONG", "LNG", "int", "Longest reception"),
        ],
    },
    "ppa": {
        "label": "Predicted points added (PPA)",
        "sort": "PPA_ALL",
        "columns": [
            ("PPA_ALL", "PPA", "f2", "Predicted points added per play, all situations"),
            ("PPA_PASS", "PASS", "f2", "Predicted points added per play, passing plays"),
            ("PPA_RUSH", "RUSH", "f2", "Predicted points added per play, rushing plays"),
        ],
    },
    "defensive": {
        "label": "Defense",
        "sort": "TOT",
        "columns": [
            ("SOLO", "SOLO", "int", "Solo tackles"),
            ("TOT", "TOT", "int", "Total tackles"),
            ("TFL", "TFL", "f1", "Tackles for loss"),
            ("SACKS", "SACK", "f1", "Sacks"),
            ("QB HUR", "HUR", "int", "Quarterback hurries"),
            ("PD", "PD", "int", "Passes defended"),
            ("TD", "TD", "int", "Defensive touchdowns"),
        ],
    },
    "interceptions": {
        "label": "Interceptions",
        "sort": "INT",
        "columns": [
            ("INT", "INT", "int", "Interceptions"),
            ("YDS", "YDS", "int", "Return yards"),
            ("AVG", "AVG", "f1", "Yards per return"),
            ("TD", "TD", "int", "Return touchdowns"),
        ],
    },
    "fumbles": {
        "label": "Fumbles",
        "sort": "FUM",
        "columns": [
            ("FUM", "FUM", "int", "Fumbles"),
            ("LOST", "LOST", "int", "Fumbles lost"),
            ("REC", "REC", "int", "Fumbles recovered"),
        ],
    },
    "kicking": {
        "label": "Kicking",
        "sort": "PTS",
        "columns": [
            ("FGM", "FGM", "int", "Field goals made"),
            ("FGA", "FGA", "int", "Field goals attempted"),
            ("PCT", "PCT", "rate", "Field goal percentage"),
            ("LONG", "LNG", "int", "Longest field goal"),
            ("XPM", "XPM", "int", "Extra points made"),
            ("XPA", "XPA", "int", "Extra points attempted"),
            ("PTS", "PTS", "int", "Kicking points"),
        ],
    },
    "punting": {
        "label": "Punting",
        "sort": "YDS",
        "columns": [
            ("NO", "NO", "int", "Punts"),
            ("YDS", "YDS", "int", "Punt yards"),
            ("YPP", "AVG", "f1", "Yards per punt"),
            ("LONG", "LNG", "int", "Longest punt"),
            ("In 20", "IN20", "int", "Punts inside the 20"),
            ("TB", "TB", "int", "Touchbacks"),
        ],
    },
    "kickReturns": {
        "label": "Kick returns",
        "sort": "YDS",
        "columns": [
            ("NO", "NO", "int", "Returns"),
            ("YDS", "YDS", "int", "Return yards"),
            ("AVG", "AVG", "f1", "Yards per return"),
            ("LONG", "LNG", "int", "Longest return"),
            ("TD", "TD", "int", "Return touchdowns"),
        ],
    },
    "puntReturns": {
        "label": "Punt returns",
        "sort": "YDS",
        "columns": [
            ("NO", "NO", "int", "Returns"),
            ("YDS", "YDS", "int", "Return yards"),
            ("AVG", "AVG", "f1", "Yards per return"),
            ("LONG", "LNG", "int", "Longest return"),
            ("TD", "TD", "int", "Return touchdowns"),
        ],
    },
}

#: (stat_type, minimum) a player must clear to appear on a leaderboard. Ranking
#: purely by a volume statistic otherwise lets trick-play and mop-up lines rank
#: above real production.
LEADER_QUALIFIERS: dict[str, tuple[str, float]] = {
    "passing": ("ATT", 25),
    "rushing": ("CAR", 20),
    "receiving": ("REC", 5),
    "kicking": ("FGA", 5),
    "punting": ("NO", 10),
}


def qualifier(category: str) -> tuple[str, float] | None:
    """Minimum usage a player needs before ranking in this category."""
    return LEADER_QUALIFIERS.get(category)


#: Reading order: scrimmage production, then defense, then special teams.
CATEGORY_ORDER = (
    "passing", "rushing", "receiving", "ppa", "defensive", "interceptions",
    "fumbles", "kicking", "punting", "kickReturns", "puntReturns",
)


def category_columns(category: str) -> list[Column]:
    """Statistic columns for one category, in conventional box-score order."""
    spec = CATEGORY_SPECS.get(category)
    if spec is None:
        return []
    return [
        Column(key=stat_type, label=label, format=fmt, title=title)
        for stat_type, label, fmt, title in spec["columns"]
    ]


#: category -> (PFF dataset supplying the usage figure, row key, header, tooltip).
#: CFBD's own box score has no snap/route/dropback counts, so this borrows the
#: one PFF already imports for each dataset's usage_count rather than parsing
#: a new field out of metrics_json. Rushing is deliberately absent: PFF's own
#: usage figure there is "attempts", identical to the CAR column already shown.
PFF_USAGE_COLUMNS: dict[str, tuple[str, str, str, str]] = {
    "passing": ("passing", "pff_dropbacks", "DB",
                "PFF-recorded dropbacks (pass attempts plus sacks and scrambles)"),
    "receiving": ("receiving", "pff_routes", "RTE", "PFF-recorded routes run"),
    "defensive": ("defense", "pff_snaps", "SNP", "PFF-recorded defensive snaps"),
}


def category_label(category: str) -> str:
    spec = CATEGORY_SPECS.get(category)
    return spec["label"] if spec else category.replace("_", " ").title()


def sort_stat(category: str) -> str | None:
    spec = CATEGORY_SPECS.get(category)
    return spec["sort"] if spec else None


def _pivot(rows: Iterable[Any], keys: Sequence[str]) -> list[dict[str, Any]]:
    """Collapse long-form rows into one dict per grouping key."""
    grouped: dict[tuple, dict[str, Any]] = {}
    for record in rows:
        row = record if isinstance(record, dict) else dict(record)
        identity = tuple(row.get(key) for key in keys)
        bucket = grouped.setdefault(identity, {key: row.get(key) for key in keys})
        value = row.get("numeric_value")
        bucket[row["stat_type"]] = value if value is not None else row.get("stat_value")
    return list(grouped.values())


def _unordered_columns(rows: list[dict[str, Any]]) -> list[Column]:
    """Fallback columns for a category CFBD adds before this module knows it."""
    return [
        Column(key=stat_type, label=stat_type, format="num")
        for stat_type in sorted({row["stat_type"] for row in rows})
    ]


def player_stat_tables(stat_rows: Iterable[Any],
                       pff_usage: dict[tuple[str, int], float] | None = None) -> list[dict[str, Any]]:
    """Career stat lines for one player: one table per category, newest first.

    Returns ``{"category", "label", "table"}`` entries so a page renders
    "Passing" and "Rushing" as separate, correctly-headed tables instead of one
    undifferentiated key/value dump. With prior seasons backfilled these become
    career lines, one row per season, newest first.

    ``pff_usage`` is ``{(dataset, season): usage_count}``, built from the
    player's PFF rows. It adds one extra column to the categories in
    ``PFF_USAGE_COLUMNS`` -- CFBD's box score has no snap/route/dropback
    counts of its own, so a category volume number sits with no denominator
    to judge it against until this joins one in.
    """
    rows = [record if isinstance(record, dict) else dict(record) for record in stat_rows]
    categories = {row["category"] for row in rows}
    ordered = [name for name in CATEGORY_ORDER if name in categories]
    ordered.extend(sorted(categories - set(CATEGORY_ORDER)))
    tables: list[dict[str, Any]] = []
    for category in ordered:
        category_rows = [row for row in rows if row["category"] == category]
        lines = sorted(
            _pivot(category_rows, ("season", "team", "position")),
            key=lambda line: -(line.get("season") or 0),
        )
        statistics = category_columns(category) or _unordered_columns(category_rows)
        usage_spec = PFF_USAGE_COLUMNS.get(category)
        if usage_spec and pff_usage:
            dataset, row_key, header, title = usage_spec
            for line in lines:
                line[row_key] = pff_usage.get((dataset, line.get("season")))
            statistics = [*statistics,
                          Column(key=row_key, label=header, format="int", title=title)]
        tables.append({
            "category": category,
            "label": category_label(category),
            "table": Table(
                columns=[
                    Column(key="season", label="Season", format="rank", align="left"),
                    Column(key="team", label="Team", align="left"),
                    *statistics,
                ],
                rows=lines,
                caption=category_label(category),
                empty=f"No {category_label(category).lower()} production is stored.",
            ),
        })
    return tables


def leader_table(category: str, players: Sequence[dict[str, Any]], *,
                 include_team: bool = True, limit: int | None = None) -> Table:
    """A leaderboard that shows the whole stat line, not one anonymous number."""
    selected = list(players[:limit] if limit else players)
    headline_stat = sort_stat(category)

    def per_game(player: dict[str, Any]) -> float | None:
        games = player.get("games_played")
        if not games or not headline_stat:
            return None
        try:
            return round(float(player.get("stats", {}).get(headline_stat) or 0) / games, 1)
        except (TypeError, ValueError):
            return None

    ranked = [
        {**player.get("stats", {}),
         "rank": index,
         "player": player.get("player"),
         "player_id": player.get("player_id"),
         "position": player.get("position"),
         "team": player.get("team"),
         "per_game": per_game(player)}
        for index, player in enumerate(selected, start=1)
    ]
    columns = [
        Column(key="rank", label="#", format="rank", align="right"),
        Column(key="player", label="Player", align="left", emphasis=True),
        Column(key="position", label="Pos", align="left"),
    ]
    if include_team:
        columns.append(Column(key="team", label="Team", align="left"))
    columns.extend(category_columns(category))
    if headline_stat:
        columns.append(Column(key="per_game", label=f"{headline_stat}/GM", format="f1",
                              title=f"{headline_stat} per game, over the games played "
                                    "by whichever team the line was recorded for."))
    return Table(
        columns=columns,
        rows=ranked,
        caption=category_label(category),
        empty=f"No {category_label(category).lower()} leaders are stored.",
    )
