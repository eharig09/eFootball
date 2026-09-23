"""View models for compact, data-first visuals on CFB entity pages."""

from __future__ import annotations

import json
from typing import Any


def _pff_label(player: dict[str, Any], evidence: dict[str, Any]) -> str | None:
    """The position-relevant PFF grade, without presenting a blended score as one."""
    position = str(player.get("position") or "").upper()
    preferred = {
        "QB": ("passing",), "RB": ("rushing",), "FB": ("rushing",),
        "WR": ("receiving",), "TE": ("receiving",),
        "EDGE": ("pass_rush", "run_defense_detail"),
        "DE": ("pass_rush", "run_defense_detail"),
        "DL": ("run_defense_detail", "pass_rush"), "DT": ("run_defense_detail", "pass_rush"),
        "LB": ("run_defense_detail", "coverage"),
        "CB": ("coverage",), "DB": ("coverage",), "S": ("coverage",),
    }.get(position, ())
    datasets = evidence.get("pff_datasets") or {}
    for name in preferred:
        grade = (datasets.get(name) or {}).get("primary_grade")
        if grade is not None:
            return f"PFF {float(grade):.1f}"
    if position in {"OL", "OT", "OG", "G", "T", "C"}:
        packet = datasets.get("blocking") or {}
        try:
            metrics = json.loads(packet.get("metrics_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {}
        components = []
        for label, key in (("PB", "grades_pass_block"), ("RB", "grades_run_block")):
            try:
                if metrics.get(key) not in (None, ""):
                    components.append(f"{label} {float(metrics[key]):.1f}")
            except (TypeError, ValueError):
                pass
        if components:
            return " · ".join(components)
    return None


def _slot(player: dict[str, Any] | None, backup: dict[str, Any] | None,
          label: str, projection: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def entry(item):
        if not item:
            return None
        evidence = projection.get(str(item.get("player_id"))) or {}
        return {"name": item.get("name"), "player_id": item.get("player_id"),
                "grade": _pff_label(item, evidence),
                "status": "T" if item.get("arrival_type") == "TRANSFER_IN"
                else "R" if item.get("is_returner") else "N"}
    return {"label": label, "starter": entry(player), "backup": entry(backup)}


def depth_formations(depth_chart: dict[str, Any],
                     projection: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Eleven-person 11-offense and nickel 4-2-5 defense formations."""
    units = depth_chart.get("units") or {}
    offense = units.get("Offense") or {}
    defense = units.get("Defense") or {}

    def take(groups, name, count):
        players = list(groups.get(name) or [])
        starters = players[:count]
        backups = players[count:count * 2]
        starters += [None] * (count - len(starters))
        backups += [None] * (count - len(backups))
        return starters, backups

    qb, qb2 = take(offense, "Quarterback", 1)
    rb, rb2 = take(offense, "Backfield", 1)
    wr, wr2 = take(offense, "Wide receiver", 3)
    te, te2 = take(offense, "Tight end", 1)
    ol, ol2 = take(offense, "Offensive line", 5)
    interior, interior2 = take(defense, "Interior defensive line", 2)
    edge, edge2 = take(defense, "Edge", 2)
    lb, lb2 = take(defense, "Linebacker", 2)
    db, db2 = take(defense, "Defensive back", 5)
    make = lambda players, backups, labels: [
        _slot(player, backups[index], labels[index], projection)
        for index, player in enumerate(players)]
    # Rows read top to bottom as depth from the line of scrimmage, the same
    # line shared by both units, so a formation actually looks like one: the
    # line first (wide receivers split at its ends, tackle-to-tackle between
    # them), then anything lined up just off it, then the backfield or
    # secondary furthest back.
    return {
        "offense": {
            "line": make(wr[:1] + ol + te + wr[1:2], wr2[:1] + ol2 + te2 + wr2[1:2],
                        ["X", "LT", "LG", "C", "RG", "RT", "TE", "Z"]),
            "off_line": make(wr[2:], wr2[2:], ["SLOT"]),
            "backfield": [make(qb, qb2, ["QB"]), make(rb, rb2, ["RB"])],
            "count": 11, "label": "11 personnel",
        },
        "defense": {
            "line": make(edge[:1] + interior + edge[1:], edge2[:1] + interior2 + edge2[1:],
                        ["EDGE", "DT", "NT", "EDGE"]),
            "off_line": make(lb, lb2, ["MIKE", "WILL"]),
            "backfield": [make(db, db2, ["CB", "NB", "FS", "SS", "CB"])],
            "count": 11, "label": "4–2–5 nickel",
        },
    }


def upcoming_games_rows(schedule: list[dict[str, Any]], team_id: int, after_date: str,
                        limit: int = 3) -> list[dict[str, Any]]:
    """The next few games on this team's schedule, chronological, not yet played.

    `schedule` must already be sorted by start_date (as `team_schedule` returns
    it) and labeled with `date_label` (as `_label_games` adds it).
    """
    rows = []
    for game in schedule:
        start = game.get("start_date")
        if not start or start <= after_date or game.get("completed"):
            continue
        home = game.get("home_team_id") == team_id
        rows.append({
            "result": "NEXT",
            "opponent": game.get("away_team") if home else game.get("home_team"),
            "site": "Neutral" if game.get("neutral_site") else ("Home" if home else "Away"),
            "date_label": game.get("date_label"),
            "opponent_elo": game.get("away_pregame_elo") if home else game.get("home_pregame_elo"),
            "game_id": game.get("game_id"),
        })
        if len(rows) >= limit:
            break
    return rows


def win_prob_sparkline(values: list[float] | None, *,
                       width: float = 96, height: float = 28) -> dict[str, Any] | None:
    """SVG polyline geometry for a compact win-probability sparkline.

    Fewer than two points has no line to draw (a single-play win-probability
    "series" isn't a shape, it's a dot), so this returns None and the caller
    skips the sparkline entirely rather than rendering a flat or broken one.
    """
    if not values or len(values) < 2:
        return None
    step = width / (len(values) - 1)
    points = " ".join(
        f"{round(index * step, 1)},{round(height - (value / 100) * height, 1)}"
        for index, value in enumerate(values)
    )
    return {"points": points, "width": width, "height": height,
            "midline_y": round(height / 2, 1), "final": values[-1], "start": values[0]}


def recent_form_rows(games: list[dict[str, Any]], *,
                     upcoming: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Completed games leading into a matchup, with what's next after it.

    `upcoming` (see `upcoming_games_rows`) appends a few "NEXT" cards, so the
    recent form leads somewhere instead of just stopping -- the same context
    the old, separate season-journey timeline carried, without duplicating the
    schedule table with a second list of every game in the season.

    A row whose source game already carries `win_prob_values` (this team's
    own play-by-play win probability for that past game, attached by the
    caller since it needs a repository lookup this function deliberately
    doesn't take) gets a `win_prob_spark` sparkline built from it.
    """
    rows = []
    for game in games:
        points_for, points_against = game.get("points_for"), game.get("points_against")
        margin = (points_for - points_against
                  if points_for is not None and points_against is not None else None)
        opponent_elo = (game.get("away_pregame_elo") if game.get("site") == "Home"
                        else game.get("home_pregame_elo"))
        rows.append({**game, "full_score": game.get("score"), "margin": margin,
                     "margin_label": f"{margin:+d}" if margin is not None else None,
                     "shape": ("Blowout" if margin is not None and abs(margin) >= 21
                               else "One score" if margin is not None and abs(margin) <= 8
                               else "Multi-score" if margin is not None else None),
                     "opponent_elo": opponent_elo,
                     "win_prob_spark": win_prob_sparkline(game.get("win_prob_values"))})
    if upcoming:
        rows.extend(upcoming)
    return rows


def _elo_win_prob(home_elo: float | None, away_elo: float | None) -> float | None:
    """Standard Elo win-probability curve, not an invented one."""
    if not home_elo or not away_elo:
        return None
    return 100 / (1 + 10 ** ((away_elo - home_elo) / 400))


def model_probability_track(game: dict[str, Any], fpi: dict[str, Any],
                            elo: dict[int, dict[str, Any]],
                            market: dict[str, Any]) -> dict[str, Any] | None:
    """Independent win-probability reads on the same axis, home team's share.

    Only sources with a real probability are plotted: FPI publishes one
    directly, Elo's comes from the standard logistic curve, and the market's
    comes from de-vigged moneylines (see `lines.game_lines`) -- never a spread
    converted through an uncalibrated guess.
    """
    home_id, away_id = game["home_team_id"], game["away_team_id"]
    home_fpi = (fpi.get("teams") or {}).get(home_id) or {}
    home_elo_value = (elo.get(home_id) or {}).get("elo")
    away_elo_value = (elo.get(away_id) or {}).get("elo")
    models = []
    fpi_prob = home_fpi.get("game_projection")
    if fpi_prob is not None:
        models.append({"key": "fpi", "label": "FPI", "home_prob": round(float(fpi_prob), 1)})
    elo_prob = _elo_win_prob(home_elo_value, away_elo_value)
    if elo_prob is not None:
        models.append({"key": "elo", "label": "Elo", "home_prob": round(elo_prob, 1)})
    market_prob = market.get("consensus_home_win_prob")
    if market_prob is not None:
        models.append({"key": "market", "label": "Market", "home_prob": market_prob})
    if not models:
        return None
    return {
        "home_team": game["home_team"], "away_team": game["away_team"],
        "models": models,
        "spread_open": market.get("consensus_spread_open"),
        "spread_current": market.get("consensus_spread"),
    }


def _week_series(rows: list[dict[str, Any]], key: str, *, pct: bool = False) -> list[float | None]:
    out = []
    for row in rows:
        value = row.get(key)
        if value is None:
            out.append(None)
        else:
            out.append(round(value * 100, 1) if pct else round(value, 3))
    return out


def _weeks_with_ghosts(current_rows: list[dict[str, Any]],
                       previous_rows: list[dict[str, Any]] | None, *,
                       min_count: int = 3) -> tuple[list[dict[str, Any]], int]:
    """Pad a sparse start-of-season trend with last season's closing weeks.

    Week one or two of a new season is too few points to read as a trend, and
    the honest fix isn't averaging them away -- it's borrowing the end of last
    season's line so the chart has a shape from the first week, then quietly
    losing those borrowed points one by one as this season's real sample
    grows past `min_count`.
    """
    missing = max(0, min_count - len(current_rows)) if previous_rows else 0
    ghosts = previous_rows[-missing:] if missing else []
    labeled = [{**row, "ghost": True, "week_label": f"P{row['week']}"} for row in ghosts]
    labeled += [{**row, "ghost": False, "week_label": f"W{row['week']}"} for row in current_rows]
    return labeled, len(ghosts)


def team_rank_trend_chart_data(elo_history: list[dict[str, Any]],
                               rank_history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Weekly Elo and AP rank, shaped for the generic `trend_chart` macro.

    Elo and rank rarely share the same week set -- a team carries an Elo
    rating every week it plays, but only appears in the poll while ranked --
    so both series are reindexed onto the union of weeks either one reports,
    leaving a null rather than guessing a value for a week one source lacks.
    Rank's own metric is marked `reverse` since #1 is best; `trend_scripts()`
    flips that axis so both series read "up is better" on screen.
    """
    weeks = sorted({row["week"] for row in elo_history} | {row["week"] for row in rank_history})
    if not weeks:
        return None
    elo_by_week = {row["week"]: row["elo"] for row in elo_history}
    rank_by_week = {row["week"]: row["rank"] for row in rank_history}
    return {
        "labels": [f"W{week}" for week in weeks],
        "metrics": [
            {"key": "elo", "label": "Elo", "series": [
                {"key": "elo", "label": "Elo", "color": "#6ea8f0",
                 "data": [elo_by_week.get(week) for week in weeks]},
            ]},
            {"key": "rank", "label": "AP rank", "reverse": True, "series": [
                {"key": "rank", "label": "AP rank", "color": "#ef7a7a",
                 "data": [rank_by_week.get(week) for week in weeks]},
            ]},
        ],
    }


def _labeled_weeks(source: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{**row, "week_label": f"W{row['week']}"} for row in (source or [])]


def _team_context_definitions(
    scoring_rows: list[dict[str, Any]] | None, counting_rows: list[dict[str, Any]] | None, *,
    label_prefix: str = "",
) -> tuple[tuple[list[dict[str, Any]], str, str, str, bool], ...]:
    """(source, key, label, format, pct) for Points For/Against/Margin plus
    raw plays/yards/scoring-drive counts -- shared between the team page's
    own trend chart and the player page's team-context overlay so the two
    never drift into describing the same numbers with different labels.
    """
    scoring, counting = _labeled_weeks(scoring_rows), _labeled_weeks(counting_rows)
    return (
        (scoring, "points_for", f"{label_prefix}Points For", "int", False),
        (scoring, "points_against", f"{label_prefix}Points Against", "int", False),
        (scoring, "margin", f"{label_prefix}Margin", "signed", False),
        (counting, "scrimmage_plays", f"{label_prefix}Plays", "int", False),
        (counting, "pass_plays", f"{label_prefix}Pass Plays", "int", False),
        (counting, "rush_plays", f"{label_prefix}Rush Plays", "int", False),
        (counting, "completions", f"{label_prefix}Completions", "int", False),
        (counting, "total_yards", f"{label_prefix}Total Yards", "big", False),
        (counting, "pass_yards", f"{label_prefix}Pass Yards", "big", False),
        (counting, "rush_yards", f"{label_prefix}Rush Yards", "big", False),
        (counting, "touchdowns", f"{label_prefix}Touchdowns", "int", False),
        (counting, "field_goals", f"{label_prefix}Field Goals", "int", False),
        (counting, "turnovers", f"{label_prefix}Turnovers", "int", False),
        (counting, "punts", f"{label_prefix}Punts", "int", False),
    )


def _build_charts(definitions: tuple[tuple[list[dict[str, Any]], str, str, str, bool], ...], *,
                  color_offset: int = 0) -> list[dict[str, Any]]:
    charts = []
    for index, (source, key, label, value_format, pct) in enumerate(definitions):
        chart = _chart_series(source, key, label, value_format=value_format, pct=pct,
                              color=CHART_COLORS[(color_offset + index) % len(CHART_COLORS)])
        if chart:
            charts.append(chart)
    return charts


def team_trend_chart_data(rows: list[dict[str, Any]],
                          scoring_rows: list[dict[str, Any]] | None = None,
                          counting_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Weekly team series, shaped for the shared dual-axis `chart_workbench`.

    `rows` (team_game_advanced.team_weekly_trend) supplies offense/defense
    EPA, success rate, and explosive rate -- stored as 0-1 fractions,
    converted to percentages here so the chart never has to. `scoring_rows`
    and `counting_rows` add Points For/Against/Margin and raw plays/yards/
    scoring-drive counts via `_team_context_definitions`. Checking any two of
    these overlays them on chart_workbench's dual axis -- e.g. Offense EPA
    against Points For -- even though the three sources don't share one
    week-indexed table; each metric carries its own week per point, the same
    way the player page's stats-plus-team-context mix does.
    """
    advanced = _labeled_weeks(rows)
    definitions = (
        (advanced, "epa_per_play", "Offense EPA / play", "signed2", False),
        (advanced, "defense_epa_per_play", "Defense EPA / play", "signed2", False),
        (advanced, "success_rate", "Offense Success Rate", "pct", True),
        (advanced, "defense_success_rate", "Defense Success Rate", "pct", True),
        (advanced, "explosive_rate", "Offense Explosive Rate", "pct", True),
        (advanced, "defense_explosive_rate", "Defense Explosive Rate", "pct", True),
    ) + _team_context_definitions(scoring_rows, counting_rows)
    return _build_charts(definitions)


#: Same palette sports_aggregator.nfl.charts uses for its own dual-axis
#: workbench -- a literal constant, not shared logic, so it is repeated here
#: rather than importing NFL code into a CFB module.
CHART_COLORS = ("#69c5ff", "#ffb45f", "#78d69b", "#d49cff", "#ff7f8a", "#e7dc68")


def _chart_series(rows: list[dict[str, Any]], key: str, label: str, *, value_format: str = "f1",
                  pct: bool = False, color: str | None = None) -> dict[str, Any] | None:
    """One metric's weekly series, shaped for the shared chart_workbench macro
    (see templates/_charts.html) -- the exact same {key,label,format,color,
    values:[{week,week_label,opponent,game_id,value}]} shape
    sports_aggregator.nfl.charts.series() produces, so both sports' player
    pages render with the same dual-axis Chart.js workbench."""
    values = []
    for row in rows:
        raw = row.get(key)
        if raw is None:
            continue
        value = round(raw * 100, 1) if pct else round(raw, 3)
        values.append({"week": row.get("week"), "week_label": row.get("week_label"),
                       "opponent": row.get("opponent"), "game_id": row.get("game_id"),
                       "value": value})
    if not values:
        return None
    return {"key": key, "label": label, "format": value_format, "color": color, "values": values}


def player_trend_chart_data(rows: list[dict[str, Any]],
                            previous_rows: list[dict[str, Any]] | None = None,
                            *, min_count: int = 3) -> list[dict[str, Any]]:
    """One passer's week-by-week series, for the shared dual-axis chart_workbench.

    `previous_rows` (last season's version of the same weekly rows) backfills
    a season that has not yet reached `min_count` real weeks -- see
    `_weeks_with_ghosts`.
    """
    combined, _ghost_count = _weeks_with_ghosts(rows, previous_rows, min_count=min_count)
    definitions = (
        ("epa_per_attempt", "EPA / attempt", "signed2", False),
        ("completion_rate", "Completion rate", "pct", True),
        ("yards_per_attempt", "Yards / attempt", "f1", False),
        ("attempts", "Attempts", "int", False),
        ("completions", "Completions", "int", False),
        ("yards", "Yards", "big", False),
        ("interceptions", "Interceptions", "int", False),
    )
    charts = []
    for index, (key, label, value_format, pct) in enumerate(definitions):
        chart = _chart_series(combined, key, label, value_format=value_format, pct=pct,
                              color=CHART_COLORS[index % len(CHART_COLORS)])
        if chart:
            charts.append(chart)
    return charts


#: Which weekly box-score metrics read as "recent form" for a non-QB skill
#: position. Every field player_weekly_trend computes is offered regardless
#: of position -- a WR occasionally getting jet-sweep carries is real
#: production, not noise to hide.
SKILL_TREND_METRICS = {
    "RB": (("scrimmage_yards", "Scrimmage yds", "big"), ("rush_yards", "Rush yds", "big"),
          ("rush_attempts", "Carries", "int"), ("yards_per_carry", "Yds / carry", "f1"),
          ("rush_td", "Rush TD", "int"), ("receiving_yards", "Receiving yds", "big"),
          ("receptions", "Receptions", "int"), ("receiving_td", "Rec TD", "int")),
    "FB": (("scrimmage_yards", "Scrimmage yds", "big"), ("rush_yards", "Rush yds", "big"),
          ("rush_attempts", "Carries", "int"), ("yards_per_carry", "Yds / carry", "f1"),
          ("rush_td", "Rush TD", "int"), ("receiving_yards", "Receiving yds", "big"),
          ("receptions", "Receptions", "int"), ("receiving_td", "Rec TD", "int")),
    "WR": (("receiving_yards", "Receiving yds", "big"), ("receptions", "Receptions", "int"),
          ("yards_per_reception", "Yds / catch", "f1"), ("receiving_td", "Rec TD", "int"),
          ("rush_yards", "Rush yds", "big"), ("rush_attempts", "Carries", "int"),
          ("rush_td", "Rush TD", "int")),
    "TE": (("receiving_yards", "Receiving yds", "big"), ("receptions", "Receptions", "int"),
          ("yards_per_reception", "Yds / catch", "f1"), ("receiving_td", "Rec TD", "int"),
          ("rush_yards", "Rush yds", "big"), ("rush_attempts", "Carries", "int"),
          ("rush_td", "Rush TD", "int")),
}


def skill_player_trend_chart_data(rows: list[dict[str, Any]], position: str,
                                  previous_rows: list[dict[str, Any]] | None = None,
                                  *, min_count: int = 3) -> list[dict[str, Any]]:
    """A rusher or receiver's week-by-week series, for the shared dual-axis
    chart_workbench.

    Passers get event-level EPA (`player_trend_chart_data`, from charted pass
    plays); nothing tags a rusher or receiver on a play the same way, so this
    reads the same weekly box-score numbers the game log already shows.
    """
    metrics_spec = SKILL_TREND_METRICS.get(str(position or "").upper())
    if not metrics_spec:
        return []
    combined, _ghost_count = _weeks_with_ghosts(rows, previous_rows, min_count=min_count)
    charts = []
    for index, (key, label, value_format) in enumerate(metrics_spec):
        chart = _chart_series(combined, key, label, value_format=value_format,
                              color=CHART_COLORS[index % len(CHART_COLORS)])
        if chart:
            charts.append(chart)
    return charts


def team_scoring_chart_series(scoring_rows: list[dict[str, Any]] | None,
                              counting_rows: list[dict[str, Any]] | None = None, *,
                              label_prefix: str = "Team ", color_offset: int = 0) -> list[dict[str, Any]]:
    """Points for/against/margin plus raw plays/yards/scoring-drive counts, as
    chart_workbench-shaped series (see `_team_context_definitions`).

    Appending these onto a player's own weekly chart_workbench list connects
    "how did this player do" to "how did the team do that week" -- checking
    one of these alongside a player's own metric overlays team outcome on the
    second axis rather than forcing them onto the same scale.
    """
    definitions = _team_context_definitions(scoring_rows, counting_rows, label_prefix=label_prefix)
    return _build_charts(definitions, color_offset=color_offset)


#: (player_field, team_field, label) per position -- only pairs with an
#: honest, unambiguous team-level denominator. Touchdowns are still absent:
#: cfb_team_game_drive_outcomes counts every offensive touchdown without
#: splitting rush vs pass, so a "share of team touchdowns" would silently mix
#: a runner's and a receiver's scores under the same denominator. Receptions
#: now has one (team_weekly_completions, garbage-time-excluded same as every
#: player-side weekly stat here) since every completion is caught by exactly
#: one receiver.
SHARE_SPECS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "QB": (
        ("attempts", "pass_plays", "Share of team pass attempts"),
        ("completions", "completions", "Share of team completions"),
        ("yards", "pass_yards", "Share of team passing yards"),
    ),
    "RB": (
        ("rush_yards", "rush_yards", "Share of team rushing yards"),
        ("rush_attempts", "rush_plays", "Share of team rush attempts"),
        ("receiving_yards", "pass_yards", "Share of team passing yards (receiving)"),
        ("receptions", "completions", "Share of team completions (receiving)"),
    ),
    "WR": (
        ("receiving_yards", "pass_yards", "Share of team passing yards"),
        ("receptions", "completions", "Share of team completions"),
        ("rush_yards", "rush_yards", "Share of team rushing yards"),
    ),
}
SHARE_SPECS["FB"] = SHARE_SPECS["RB"]
SHARE_SPECS["TE"] = SHARE_SPECS["WR"]


def player_share_chart_series(player_rows: list[dict[str, Any]], counting_rows: list[dict[str, Any]],
                              position: str, *, color_offset: int = 0) -> list[dict[str, Any]]:
    """This player's week-by-week share of the team's matching counting
    stat -- e.g. what fraction of the team's rushing yards this back
    carried. Only computed for the (player stat, team stat) pairs in
    SHARE_SPECS; a position with none configured, or a week missing either
    side, simply contributes no data for that point rather than a guess.
    """
    specs = SHARE_SPECS.get(str(position or "").upper())
    if not specs or not player_rows or not counting_rows:
        return []
    team_by_week = {row["week"]: row for row in counting_rows}
    share_rows = []
    for row in player_rows:
        week = row.get("week")
        team_row = team_by_week.get(week)
        entry = {"week": week, "week_label": f"W{week}", "opponent": row.get("opponent"),
                "game_id": row.get("game_id")}
        for player_field, team_field, _label in specs:
            player_value = row.get(player_field)
            team_value = (team_row or {}).get(team_field)
            entry[f"share_{player_field}"] = (
                round(100 * player_value / team_value, 1)
                if player_value is not None and team_value else None
            )
        share_rows.append(entry)
    definitions = tuple(
        (share_rows, f"share_{player_field}", label, "pct", False)
        for player_field, _team_field, label in specs
    )
    return _build_charts(definitions, color_offset=color_offset)


#: PFF college grades cluster in the 60s; a straight 0-100 scale would leave
#: every bar looking short and nearly identical. Matches the floor/ceiling
#: matchups.py already scores interest against, so "looks good here" and
#: "scores well as a matchup" mean the same grade range everywhere.
_UNIT_GRADE_FLOOR = 45.0
_UNIT_GRADE_CEILING = 90.0


def pff_unit_grade_bars(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ranked PFF unit grades for a team, with a display bar width attached."""
    span = _UNIT_GRADE_CEILING - _UNIT_GRADE_FLOOR
    bars = []
    for unit in units:
        grade = unit.get("grade")
        pct = max(4.0, min(100.0, 100 * (float(grade) - _UNIT_GRADE_FLOOR) / span)) if grade is not None else 0.0
        bars.append({**unit, "bar_pct": round(pct, 1)})
    return bars


#: (outcome keys to sum, display label, color). Turnovers-by-interception/
#: fumble and turnovers-on-downs are both "gave the ball away" to a reader,
#: and end-half drives are folded into "other" -- neither is common enough on
#: its own to earn a segment, and a six-slice bar reads as noise.
DRIVE_OUTCOME_SEGMENTS = (
    (("touchdowns",), "TD", "#5fa87a"),
    (("field_goals",), "FG", "#6ea8f0"),
    (("turnovers", "turnovers_on_downs"), "Turnover", "#ef7a7a"),
    (("punts",), "Punt", "#93a1b3"),
    (("end_half_drives", "other_drives"), "Other", "#5d6b7d"),
)


def drive_outcome_bars(away_summary: dict[str, Any] | None, home_summary: dict[str, Any] | None,
                       away_team: str, home_team: str) -> dict[str, Any] | None:
    """Season-to-date drive outcome shares for both teams, for a stacked bar.

    `away_summary`/`home_summary` come from
    `team_game_drive_outcomes.season_summary()`. Segment width is passed as a
    flex-grow proportion rather than a percentage width, so segments still
    read correctly even when their shares don't sum to exactly 100 (a team
    with an unresolved dataset gap here or there).
    """
    if not away_summary and not home_summary:
        return None

    def segments(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not summary:
            return []
        shares = summary["shares"]
        return [{"label": label, "color": color, "pct": round(sum(shares.get(key, 0) for key in keys), 1)}
                for keys, label, color in DRIVE_OUTCOME_SEGMENTS]

    return {
        "away_team": away_team, "home_team": home_team,
        "away_games": away_summary["games"] if away_summary else 0,
        "home_games": home_summary["games"] if home_summary else 0,
        "away_segments": segments(away_summary), "home_segments": segments(home_summary),
        "legend": [{"label": label, "color": color} for _, label, color in DRIVE_OUTCOME_SEGMENTS],
    }


def game_shape(away_team: str, home_team: str, away_pace: dict[str, Any] | None,
               home_pace: dict[str, Any] | None, away_drives: float | None,
               home_drives: float | None, away_advanced: dict[str, Any] | None,
               home_advanced: dict[str, Any] | None) -> dict[str, Any]:
    """How each side tends to play, side by side, plus an expected possession count.

    Each row is each team's own real tendency, not a blended prediction -- the
    same "read two independent views, don't average them" approach the models
    and market panel takes. Expected possessions is the average of both teams'
    own actual drives/game, which is the honest version of that number: no
    league-average plays-per-drive constant stands in for a team this hasn't
    been measured for.
    """
    def clamp_bar(value: float | None, scale: float = 1.0) -> float:
        return max(0.0, min(100.0, value * scale)) if value is not None else 0.0

    def rate(pace, key):
        return round(100 * pace[key], 1) if pace and pace.get(key) is not None else None

    away_plays = (away_pace or {}).get("plays_per_game")
    home_plays = (home_pace or {}).get("plays_per_game")
    away_pass_rate = rate(away_pace, "pass_rate")
    home_pass_rate = rate(home_pace, "pass_rate")
    away_explosive = (away_advanced or {}).get("offense_explosiveness")
    home_explosive = (home_advanced or {}).get("offense_explosiveness")
    rows = [
        {"label": "Pace", "unit": " plays", "away": away_plays, "home": home_plays,
         "fmt": "f1", "bar": True, "bar_away": clamp_bar(away_plays),
         "bar_home": clamp_bar(home_plays)},
        {"label": "Pass rate", "unit": "%", "away": away_pass_rate, "home": home_pass_rate,
         "fmt": "f1", "bar": True, "bar_away": clamp_bar(away_pass_rate),
         "bar_home": clamp_bar(home_pass_rate)},
        # CFBD's "explosiveness" is average EPA-scale value per explosive play,
        # not a rate -- typically 0.7-1.8, so it is shown raw and only scaled
        # (not treated as a percentage) for the comparison bar.
        {"label": "Explosiveness", "unit": "",
         "away": round(away_explosive, 2) if away_explosive is not None else None,
         "home": round(home_explosive, 2) if home_explosive is not None else None,
         "fmt": "f2", "bar": True, "bar_away": clamp_bar(away_explosive, 55),
         "bar_home": clamp_bar(home_explosive, 55)},
        {"label": "Points / opportunity", "unit": "",
         "away": (away_advanced or {}).get("offense_points_per_opportunity"),
         "home": (home_advanced or {}).get("offense_points_per_opportunity"),
         "fmt": "f2", "bar": False, "bar_away": 0.0, "bar_home": 0.0},
    ]
    drive_values = [value for value in (away_drives, home_drives) if value is not None]
    expected_possessions = round(sum(drive_values) / len(drive_values), 1) if drive_values else None
    return {
        "away_team": away_team, "home_team": home_team, "rows": rows,
        "away_drives": round(away_drives, 1) if away_drives is not None else None,
        "home_drives": round(home_drives, 1) if home_drives is not None else None,
        "expected_possessions": expected_possessions,
    }
