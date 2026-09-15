"""Repository-backed NFL dashboard, team pages, and structured APIs."""

from __future__ import annotations
import json
from pathlib import Path
from flask import Blueprint, abort, current_app, jsonify, render_template, request

from sports_aggregator.catalog import get_league
from sports_aggregator.nfl.charts import player_charts, team_charts, with_last_season
from sports_aggregator.nfl.alignments import (
    alignment_matchups, player_matchup_watches, rushing_matchups,
)
from sports_aggregator.nfl.availability import availability_packet
from sports_aggregator.nfl.content import NFLContentRepository
from sports_aggregator.nfl.explorer import (
    METRICS, METRIC_CATEGORIES, METRIC_LABELS, SUM_METRICS,
    player_stat_table, scatter_plot, with_rates,
)
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.matchups import matchup_context
from sports_aggregator.nfl.landing import draft_projection, games_to_watch
from sports_aggregator.nfl.nflverse import current_season
from sports_aggregator.nfl.pff import NFLPFFService, PFF_EXPLORER_METRICS, season_scaled_minimum
from sports_aggregator.nfl.personnel import position_rooms, significant_movements
from sports_aggregator.nfl.passing import pass_matchup_packet, pass_zone_packet
from sports_aggregator.nfl.postgame import postgame_packet
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.search import search_entities
from sports_aggregator.nfl.staff import staff_tendencies
from sports_aggregator.nfl.teams import team_context
from sports_aggregator.nfl.trenches import trench_matchups
from sports_aggregator.nfl.usage import returning_player_usage
from sports_aggregator.nfl.views import (
    current_games, depth_chart_table, efficiency_table, game_stat_tables, game_stats_table,
    leaders_table, matchup_cards, movement_tables, player_game_log, player_game_log_tables,
    player_headline_stats, pff_leaders_table, player_totals_table, power_table, roster_table,
    schedule_table, snap_usage_table, source_coverage_table, team_source_coverage_table,
    usage_table, standings_tables,
)


nfl_pages = Blueprint("nfl", __name__)
SOURCE_SECTIONS = (
    ("wire", "NFL wire"), ("analysis", "Film + analytics"),
    ("personnel", "Personnel"), ("players", "Players + usage"),
    ("beats", "Team beats"),
)
REPORTING_STREAMS = (
    ("articles", "Articles", "Published national reporting"),
    ("wire", "News wire", "News and rapid league updates"),
    ("analysis", "Analysis", "Film, data, and tactical context"),
    ("personnel", "Personnel", "Draft, roster, contract, and transaction reporting"),
    ("players", "Players + usage", "Player roles, health, and performance"),
    ("beats", "Team beats", "Reporting from team-focused accounts"),
)


def _production_seed_status() -> dict:
    path = Path(current_app.config["NFL_DATABASE_PATH"]).parent / "nfl_production_seed.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _repository() -> NFLRepository:
    return current_app.extensions["nfl_repository"]


def _content() -> NFLContentRepository:
    return NFLContentRepository(_repository())


def _pff() -> NFLPFFService:
    return NFLPFFService(_repository(), current_app.config["NFL_PFF_SOURCE_ROOT"],
                         current_app.config.get("NFL_PFF_UPLOAD_ROOT"))


def _team_form_rows(season: int, code: str) -> list[dict]:
    """This season's weekly form, backfilled with last season's tail early on."""
    repository = _repository()
    current = repository.team_weekly_performance(season, code)
    previous = repository.team_weekly_performance(season - 1, code)
    return with_last_season(current, previous)


def _pff_season(season: int) -> int | None:
    service = _pff()
    for candidate in (season, season - 1):
        if service.counts(candidate)["metrics"]:
            return candidate
    return None


def _team_pff(team: str, season: int) -> dict:
    pff_season = _pff_season(season)
    if pff_season is None:
        return {"season": None, "cards": [], "counts": {}}
    service = _pff()
    repository = _repository()
    current_ids = {row["player_id"] for row in repository.team_roster(season, team)}
    pff_weeks = repository.latest_stat_week(pff_season) or 0
    cards = []
    for label, family, metric, sample_metric, sample_minimum in (
        ("Route grade", "receiving_summary", "grades_pass_route", "routes", 50),
        ("Run grade", "rushing_summary", "grades_run", "attempts", 50),
        ("Pass protection", "offense_blocking", "grades_pass_block", "snap_counts_pass_block", 100),
        ("Man coverage", "defense_coverage_scheme", "man_grades_coverage_defense", "man_snap_counts_coverage", 50),
        ("Pass rush", "pass_rush_summary", "grades_pass_rush_defense", "total_pressures", 10),
        ("Run defense", "defense_summary", "grades_run_defense", "tackles", 15),
    ):
        rows = service.leaders(pff_season, family, metric, team=team, limit=100,
                               minimum_metric=sample_metric,
                               minimum_value=season_scaled_minimum(sample_minimum, pff_weeks))
        if pff_season != season:
            rows = [row for row in rows if row.get("gsis_id") in current_ids]
        for row in rows[:3]:
            row["player_url"] = (f"/nfl/players/{row['gsis_id']}/?season={season}"
                                 if row.get("gsis_id") else None)
        cards.append({"label": label, "family": family, "metric": metric,
                      "rows": rows[:3]})
    return {"season": pff_season, "cards": cards, "counts": service.counts(pff_season)}


def _player_pff(player_id: str, season: int, position: str | None) -> dict:
    pff_season = _pff_season(season)
    if pff_season is None:
        return {"season": None, "highlights": [], "families": {}}
    packet = _pff().player(pff_season, player_id)
    position = (position or "").upper()
    if position == "QB":
        fields = (("passing_depth", "deep_grades_pass", "Deep grade", "f1"),
                  ("passing_depth", "medium_grades_pass", "Intermediate grade", "f1"),
                  ("passing_depth", "short_grades_pass", "Short grade", "f1"),
                  ("passing_depth", "deep_attempts_percent", "Deep attempt rate", "pct"))
    elif position in {"WR", "TE"}:
        fields = (("receiving_summary", "grades_pass_route", "Route grade", "f1"),
                  ("receiving_summary", "yprr", "Yards / route", "f2"),
                  ("receiving_summary", "avg_depth_of_target", "Target depth", "f1"),
                  ("receiving_summary", "slot_rate", "Slot rate", "pct"))
    elif position in {"RB", "FB"}:
        fields = (("rushing_summary", "grades_run", "Run grade", "f1"),
                  ("rushing_summary", "elusive_rating", "Elusive rating", "f1"),
                  ("rushing_summary", "yco_attempt", "YAC / attempt", "f2"),
                  ("receiving_summary", "grades_pass_route", "Route grade", "f1"))
    elif position in {"OT", "T", "OG", "G", "C", "OL"}:
        fields = (("offense_blocking", "grades_pass_block", "Pass-block grade", "f1"),
                  ("offense_blocking", "grades_run_block", "Run-block grade", "f1"),
                  ("offense_blocking", "pbe", "Pass-block efficiency", "f1"),
                  ("offense_blocking", "pressures_allowed", "Pressures allowed", "int"))
    else:
        fields = (("defense_coverage_scheme", "man_grades_coverage_defense", "Man grade", "f1"),
                  ("defense_coverage_scheme", "zone_grades_coverage_defense", "Zone grade", "f1"),
                  ("slot_coverage", "qb_rating_against", "Slot rating allowed", "f1"),
                  ("slot_coverage", "yards_per_coverage_snap", "Slot yards / snap", "f2"))
    highlights = []
    for family, metric, label, value_format in fields:
        value = packet["by_family"].get(family, {}).get(metric)
        if value is not None:
            highlights.append({"family": family, "metric": metric, "label": label,
                               "value": value, "format": value_format})
    return {"season": pff_season, "highlights": highlights,
            "families": packet["by_family"], "identities": packet["identities"]}


def _season() -> int:
    requested = request.args.get("season", type=int)
    season = requested or _repository().latest_season() or current_season()
    if season < 1920 or season > current_season() + 2:
        abort(400)
    return season


def _reporting():
    return current_app.extensions["league_aggregation_service"].aggregate(get_league("nfl"))


def _sources(*, team: str | None = None) -> dict[str, list[dict]]:
    registry = current_app.extensions["source_registry"]
    return {key: registry.list_league_sources("nfl", section=key, team=team, limit=24)
            for key, _label in SOURCE_SECTIONS}


def _directory_sources() -> list[dict]:
    return current_app.extensions["source_registry"].list_league_sources("nfl")


def _reporting_streams(items: list[dict], *, include_empty: bool = False) -> list[dict]:
    """Organize NFL content using the directory's explicit section tags."""
    directory = {row["handle"].casefold(): row for row in _directory_sources()}
    prepared = []
    for original in items:
        item = dict(original)
        source = directory.get(str(item.get("source_handle") or "").casefold(), {})
        sections = sorted({tag["section"] for tag in source.get("tags", [])})
        item.update({
            "editorial_sections": sections,
            "source_profile_url": source.get("profile_url"),
            "source_scope": source.get("scope"),
            "source_team": source.get("team"),
            "account_type": source.get("account_type"),
        })
        prepared.append(item)
    streams = []
    for key, label, description in REPORTING_STREAMS:
        if key == "articles":
            selected = [item for item in prepared if item["platform"] == "rss"]
        else:
            selected = [item for item in prepared if item["platform"] == "bluesky"
                        and key in item["editorial_sections"]]
        if selected or include_empty:
            streams.append({"key": key, "label": label, "description": description,
                            "count": len(selected), "items": selected[:10]})
    return streams


def _dashboard_packet(season: int, week: int | None = None) -> dict:
    repository = _repository()
    all_games = repository.schedule(season)
    teams = repository.list_teams()
    standings = repository.standings(season)
    records = {row["abbreviation"]: row for row in standings}
    identities = {team["abbreviation"]: team for team in teams}
    elo = {row["team"]: row["rating"] for row in repository.elo_ratings()}
    analysis_season = season
    league_efficiency = repository.league_efficiency(analysis_season)
    if len(league_efficiency) < 24:
        previous = repository.league_efficiency(season - 1)
        if len(previous) > len(league_efficiency):
            analysis_season = season - 1
            league_efficiency = previous
    efficiency_rows = league_efficiency[:12]
    all_efficiency = {row["team"]: row for row in league_efficiency}
    for row in efficiency_rows:
        identity = identities.get(row["team"], {})
        row["team_logo"] = identity.get("logo_url")
        row["team_color"] = identity.get("color")
        row["elo_rating"] = round(elo.get(row["team"], 1500))
    leaders = {
        "passing": leaders_table(repository.player_leaders(analysis_season, "passing_yards"),
                                 "Passing yards", season=analysis_season),
        "rushing": leaders_table(repository.player_leaders(analysis_season, "rushing_yards"),
                                 "Rushing yards", season=analysis_season),
        "receiving": leaders_table(repository.player_leaders(analysis_season, "receiving_yards"),
                                   "Receiving yards", season=analysis_season),
    }
    selected_week = week or repository.latest_stat_week(season)
    weekly_leaders = {
        "passing": leaders_table(
            repository.player_leaders(season, "passing_yards", week=selected_week),
            f"Week {selected_week} passing yards" if selected_week else "Weekly passing yards",
            season=season,
        ),
        "rushing": leaders_table(
            repository.player_leaders(season, "rushing_yards", week=selected_week),
            f"Week {selected_week} rushing yards" if selected_week else "Weekly rushing yards",
            season=season,
        ),
        "receiving": leaders_table(
            repository.player_leaders(season, "receiving_yards", week=selected_week),
            f"Week {selected_week} receiving yards" if selected_week else "Weekly receiving yards",
            season=season,
        ),
    }
    pff_service = _pff()
    # Full-season "qualified" floors (100 routes, 100 snaps, ...) exclude
    # every player early in a season, when a fresh PFF upload has only a
    # game or two of accumulation. Scale each one to how much of the season
    # has actually been played so real, freshly synced data still surfaces.
    pff_weeks = repository.latest_stat_week(analysis_season) or 0
    def _min(value: float) -> float:
        return season_scaled_minimum(value, pff_weeks)
    pff_features = {
        "route": pff_leaders_table(
            pff_service.leaders(analysis_season, "receiving_summary", "grades_pass_route", limit=10,
                                minimum_metric="routes", minimum_value=_min(100)),
            caption="Route grade", season=analysis_season,
        ),
        "rushing": pff_leaders_table(
            pff_service.leaders(analysis_season, "rushing_summary", "grades_run", limit=10,
                                minimum_metric="attempts", minimum_value=_min(100)),
            caption="Rushing grade", season=analysis_season,
        ),
        "blocking": pff_leaders_table(
            pff_service.leaders(analysis_season, "offense_blocking", "grades_pass_block", limit=10,
                                minimum_metric="snap_counts_pass_block", minimum_value=_min(200)),
            caption="Pass-block grade", season=analysis_season,
        ),
        "coverage": pff_leaders_table(
            pff_service.leaders(analysis_season, "defense_coverage_scheme",
                                "man_grades_coverage_defense", limit=10,
                                minimum_metric="man_snap_counts_coverage", minimum_value=_min(100)),
            caption="Man coverage grade", season=analysis_season,
        ),
        "receiving_efficiency": pff_leaders_table(
            pff_service.leaders(analysis_season, "receiving_summary", "yprr", limit=10,
                                minimum_metric="routes", minimum_value=_min(100)),
            caption="Yards per route", season=analysis_season,
        ),
        "elusive": pff_leaders_table(
            pff_service.leaders(analysis_season, "rushing_summary", "elusive_rating", limit=10,
                                minimum_metric="attempts", minimum_value=_min(75)),
            caption="Elusive rating", season=analysis_season,
        ),
        "run_blocking": pff_leaders_table(
            pff_service.leaders(analysis_season, "offense_blocking", "grades_run_block", limit=10,
                                minimum_metric="snap_counts_run_block", minimum_value=_min(150)),
            caption="Run-block grade", season=analysis_season,
        ),
        "zone_coverage": pff_leaders_table(
            pff_service.leaders(analysis_season, "defense_coverage_scheme",
                                "zone_grades_coverage_defense", limit=10,
                                minimum_metric="zone_snap_counts_coverage", minimum_value=_min(100)),
            caption="Zone coverage grade", season=analysis_season,
        ),
        "slot_coverage": pff_leaders_table(
            pff_service.leaders(analysis_season, "slot_coverage",
                                "yards_per_coverage_snap", limit=10, lower=True,
                                minimum_metric="coverage_snaps", minimum_value=_min(75)),
            caption="Slot yards allowed / snap", season=analysis_season,
        ),
        "deep_passing": pff_leaders_table(
            pff_service.leaders(analysis_season, "passing_depth",
                                "deep_grades_pass", limit=10,
                                minimum_metric="deep_attempts", minimum_value=_min(20)),
            caption="Deep passing grade", season=analysis_season,
        ),
        "deep_receiving": pff_leaders_table(
            pff_service.leaders(analysis_season, "receiving_depth",
                                "deep_grades_pass_route", limit=10,
                                minimum_metric="deep_targets", minimum_value=_min(15)),
            caption="Deep receiving grade", season=analysis_season,
        ),
        "pass_rush": pff_leaders_table(
            pff_service.leaders(analysis_season, "pass_rush_summary",
                                "grades_pass_rush_defense", limit=10,
                                minimum_metric="total_pressures", minimum_value=_min(15)),
            caption="Pass-rush grade", season=analysis_season,
        ),
        "run_defense": pff_leaders_table(
            pff_service.leaders(analysis_season, "defense_summary",
                                "grades_run_defense", limit=10,
                                minimum_metric="tackles", minimum_value=_min(30)),
            caption="Run-defense grade", season=analysis_season,
        ),
    }
    slate = current_games(all_games)
    # Games to Watch is deliberately constrained to the same current-week
    # slate shown below it; it must not drift into future weeks to fill space.
    watch_pool = [game for game in slate if not game["completed"]]
    matchups = matchup_cards(slate, identities, records, all_efficiency, elo)
    content_items = _content().latest(60)
    source_coverage = _content().source_coverage(_directory_sources())
    return {
        "season": season, "analysis_season": analysis_season,
        "production_seed": _production_seed_status(),
        "selected_week": selected_week,
        "available_weeks": list(range(1, (repository.latest_stat_week(season) or 0) + 1)),
        "counts": repository.counts(season),
        "teams": teams,
        "standings": standings_tables(standings),
        "matchups": matchups,
        "slate_counts": {"completed": sum(game["completed"] for game in slate),
                         "upcoming": sum(not game["completed"] for game in slate)},
        "games_to_watch": games_to_watch(
            repository, watch_pool, records, identities, analysis_season,
        ),
        "elo_ratings": repository.elo_ratings(),
        "draft_projection": draft_projection(
            repository, current_app.extensions["cfb_repository"], season,
        ),
        "identity_coverage": repository.identity_coverage(),
        "efficiency": power_table(efficiency_rows, season=analysis_season),
        "schedule": schedule_table(slate, identities=identities),
        "leaders": leaders, "weekly_leaders": weekly_leaders,
        "pff_counts": pff_service.counts(analysis_season), "pff_features": pff_features,
        "pff_groups": [{"label": table.caption, "table": table}
                       for table in pff_features.values()],
        "sources": _sources(), "source_sections": SOURCE_SECTIONS,
        "content": content_items,
        "content_streams": _reporting_streams(content_items, include_empty=True),
        "content_counts": _content().counts(), "source_coverage": source_coverage,
    }


@nfl_pages.get("/nfl/")
def dashboard():
    packet = _dashboard_packet(_season(), request.args.get("week", type=int))
    return render_template("nfl.html", league=get_league("nfl"), **packet)


@nfl_pages.get("/nfl/search/")
def search_page():
    season = _season(); query = (request.args.get("q") or "").strip()
    results = (search_entities(_repository(), _content(), query, season=season, limit=10)
               if query else {"query": "", "too_short": False, "teams": [], "players": [],
                              "games": [], "stories": [], "total": 0, "season": season})
    return render_template("nfl_search.html", league=get_league("nfl"), season=season,
                           results=results)


@nfl_pages.get("/api/v1/nfl/search")
def search_api():
    season = _season(); query = (request.args.get("q") or "").strip()
    limit = min(max(request.args.get("limit", 10, type=int) or 10, 1), 40)
    return jsonify(search_entities(_repository(), _content(), query, season=season, limit=limit))


@nfl_pages.get("/nfl/scoreboard/")
def scoreboard():
    season = _season()
    repository = _repository()
    all_games = repository.schedule(season)
    weeks = sorted({game["week"] for game in all_games})
    if not weeks:
        return render_template("nfl_scoreboard.html", league=get_league("nfl"), season=season,
                               week=None, weeks=[], previous_week=None, next_week=None, games=[])
    requested = request.args.get("week", type=int)
    if requested not in weeks:
        # Land on the same "current" week the dashboard slate uses, rather
        # than always defaulting to week 1, so the page opens somewhere
        # relevant to what is actually being played right now.
        current = current_games(all_games)
        requested = current[0]["week"] if current else weeks[-1]
    identities = {team["abbreviation"]: team for team in repository.list_teams()}
    records = {row["abbreviation"]: row for row in repository.standings(season)}
    elo = {row["team"]: row["rating"] for row in repository.elo_ratings()}
    efficiency = {row["team"]: row for row in repository.league_efficiency(season)}
    week_games = [game for game in all_games if game["week"] == requested]
    index = weeks.index(requested)
    return render_template(
        "nfl_scoreboard.html", league=get_league("nfl"), season=season, week=requested,
        weeks=weeks, previous_week=weeks[index - 1] if index > 0 else None,
        next_week=weeks[index + 1] if index < len(weeks) - 1 else None,
        games=matchup_cards(week_games, identities, records, efficiency, elo),
    )


@nfl_pages.get("/api/v1/nfl")
def dashboard_api():
    packet = _dashboard_packet(_season(), request.args.get("week", type=int))
    api_packet = {key: value for key, value in packet.items() if key != "pff_groups"}
    return jsonify({**api_packet,
        "schedule": packet["schedule"].as_dict(),
        "leaders": {key: table.as_dict() for key, table in packet["leaders"].items()},
        "weekly_leaders": {key: table.as_dict() for key, table in packet["weekly_leaders"].items()},
        "pff_features": {key: table.as_dict() for key, table in packet["pff_features"].items()},
        "efficiency": packet["efficiency"].as_dict(),
        "standings": [{**group, "table": group["table"].as_dict()} for group in packet["standings"]],
    })


def _pff_packet() -> dict:
    service = _pff()
    season = request.args.get("season", type=int) or ((_repository().latest_season() or current_season()) - 1)
    available = service.available(season)
    family = request.args.get("family", "receiving_summary")
    if family not in available:
        family = next(iter(available), "receiving_summary")
    choices = available.get(family, [])
    valid_metrics = {item["metric"] for item in choices}
    metric = request.args.get("metric") or (choices[0]["metric"] if choices else "grades_pass_route")
    if metric not in valid_metrics and choices:
        metric = choices[0]["metric"]
    team = canon_team(request.args.get("team")) if request.args.get("team") else None
    lower = metric in {"pressures_allowed", "qb_rating_against", "yards_per_coverage_snap"}
    rows = service.leaders(season, family, metric, team=team, limit=75, lower=lower)
    label = next((item["label"] for item in choices if item["metric"] == metric), metric)
    return {
        "season": season, "family": family, "metric": metric, "metric_label": label,
        "team_filter": team, "available": available, "counts": service.counts(season),
        "catalog": service.catalog_rows(), "rows": rows,
        "table": pff_leaders_table(rows, caption=label, season=season),
        "teams": _repository().list_teams(),
    }


@nfl_pages.get("/nfl/pff/")
def pff_explorer():
    return render_template("nfl_pff.html", league=get_league("nfl"), **_pff_packet())


@nfl_pages.get("/api/v1/nfl/pff")
def pff_api():
    packet = _pff_packet()
    return jsonify({**packet, "table": packet["table"].as_dict()})


def _explorer_packet() -> dict:
    repository = _repository()
    season = request.args.get("season", type=int) or (repository.latest_season() or current_season())
    team = canon_team(request.args.get("team")) if request.args.get("team") else None
    position = (request.args.get("position") or "").strip().upper() or None
    minimum_games = max(1, request.args.get("min_games", 1, type=int) or 1)
    week_from = request.args.get("week_from", type=int)
    week_to = request.args.get("week_to", type=int)
    color_by = request.args.get("color_by") or "position"
    if color_by not in {"position", "team"}:
        color_by = "position"
    teams = repository.list_teams()
    rows = [with_rates(row) for row in repository.player_season_stats(
        season, SUM_METRICS, team=team, position=position, minimum_games=minimum_games,
        week_from=week_from, week_to=week_to,
    )]
    rows.sort(key=lambda row: row["player_name"])
    x_key = request.args.get("x") or "passing_epa"
    y_key = request.args.get("y") or "passing_yards"
    if x_key not in METRIC_LABELS:
        x_key = "passing_epa"
    if y_key not in METRIC_LABELS:
        y_key = "passing_yards"
    # Any single real metric works here -- this only reads the position
    # column across every player synced this season, unfiltered, to build
    # the filter dropdown's option list.
    positions = sorted({row["position"] for row in
                        repository.player_season_stats(season, ("attempts",), minimum_games=1)
                        if row.get("position")})
    team_colors = {row["abbreviation"]: (row.get("color") or "#8296a4",
                                         row.get("alternate_color") or row.get("color") or "#0b1319")
                  for row in teams}
    return {
        "season": season, "team_filter": team, "position_filter": position,
        "minimum_games": minimum_games, "week_from": week_from, "week_to": week_to,
        "latest_week": repository.latest_stat_week(season) or 18,
        "color_by": color_by, "teams": teams,
        "positions": positions, "table": player_stat_table(rows, season=season),
        "metrics": METRICS, "metric_categories": METRIC_CATEGORIES,
        "x_key": x_key, "y_key": y_key,
        "scatter": scatter_plot(rows, x_key, y_key, color_by=color_by, team_colors=team_colors),
        "player_count": len(rows),
    }


@nfl_pages.get("/nfl/explorer/")
def stat_explorer():
    return render_template("nfl_explorer.html", league=get_league("nfl"), **_explorer_packet())


@nfl_pages.get("/api/v1/nfl/explorer")
def stat_explorer_api():
    packet = _explorer_packet()
    return jsonify({**packet, "table": packet["table"].as_dict()})


@nfl_pages.get("/api/v1/nfl/content")
def content_api():
    repository = _content()
    limit = max(1, min(request.args.get("limit", 30, type=int), 100))
    team = request.args.get("team")
    player = request.args.get("player")
    game = request.args.get("game")
    if game:
        items = repository.for_game(game, limit)
    elif player:
        items = repository.for_player(_season(), player, limit)
    elif team:
        items = repository.for_team(canon_team(team), limit)
    else:
        items = repository.latest(limit)
    return jsonify({"count": len(items), "items": items, "links": repository.counts()})


def _source_audit_packet() -> dict:
    season = _season(); content = _content()
    coverage = content.source_coverage(_directory_sources())
    status_filter = (request.args.get("status") or "").strip().casefold()
    team_filter = canon_team(request.args.get("team")) if request.args.get("team") else None
    rows = coverage["rows"]
    if status_filter:
        rows = [row for row in rows if row["status"].casefold() == status_filter]
    if team_filter:
        rows = [row for row in rows if canon_team(row.get("team")) == team_filter]
    return {
        "season": season, "coverage": coverage, "status_filter": status_filter,
        "team_filter": team_filter, "teams": _repository().list_teams(),
        "source_table": source_coverage_table(rows),
        "team_table": team_source_coverage_table(coverage["teams"]),
    }


@nfl_pages.get("/nfl/sources/")
def source_audit():
    return render_template("nfl_sources.html", league=get_league("nfl"), **_source_audit_packet())


@nfl_pages.get("/api/v1/nfl/sources")
def source_audit_api():
    packet = _source_audit_packet()
    return jsonify({**packet, "source_table": packet["source_table"].as_dict(),
                    "team_table": packet["team_table"].as_dict()})


@nfl_pages.get("/nfl/teams/<abbreviation>/")
def team_page(abbreviation: str):
    season = _season(); code = canon_team(abbreviation)
    team = _repository().get_team(code)
    if team is None:
        abort(404)
    pff_season = _pff_season(season) or season - 1
    movements = significant_movements(_repository(), _pff(), season, code)
    context = team_context(_repository(), season, code)
    form_rows = _team_form_rows(season, code)
    content_items = _content().for_team(code, 60)
    availability = availability_packet(_repository(), season, code)
    rooms = position_rooms(
        _repository(), _pff(), season, code, pff_season,
        injuries=availability["rows"],
    )
    staff_packet = staff_tendencies(
        _repository(), _pff(), season, code, pff_season, context["efficiency"],
    )
    return render_template(
        "nfl_team.html", league=get_league("nfl"), season=season, team=team,
        schedule=schedule_table(
            _repository().schedule(season, team=code), team=code,
            identities={row["abbreviation"]: row for row in _repository().list_teams()},
        ),
        roster=roster_table(_repository().team_roster(season, code)),
        depth_chart=depth_chart_table(_repository().current_depth_chart(season, code)),
        snap_usage=snap_usage_table(_repository().team_snap_leaders(season, code)),
        workload=usage_table(
            context["usage"]["rows"][:16],
            caption=f"{context['usage']['season']} opportunity leaders",
        ),
        pff=_team_pff(code, season),
        position_rooms=rooms, availability=availability,
        staff=staff_packet["staff"], staff_packet=staff_packet,
        efficiency=context["efficiency"], team_context=context,
        form_charts=team_charts(form_rows),
        record=_repository().team_record(season, code),
        sources=_sources(team=team["name"]),
        source_sections=SOURCE_SECTIONS,
        movements=movement_tables({**movements, "arrivals": movements["all_arrivals"],
                                   "departures": movements["all_departures"]}),
        movement_packet=movements,
        content=content_items, content_streams=_reporting_streams(content_items),
    )


@nfl_pages.get("/api/v1/nfl/teams/<abbreviation>")
def team_api(abbreviation: str):
    season = _season(); code = canon_team(abbreviation)
    team = _repository().get_team(code)
    if team is None:
        abort(404)
    pff_season = _pff_season(season) or season - 1
    movements = significant_movements(_repository(), _pff(), season, code)
    context = team_context(_repository(), season, code)
    form_rows = _team_form_rows(season, code)
    availability = availability_packet(_repository(), season, code)
    staff_packet = staff_tendencies(
        _repository(), _pff(), season, code, pff_season, context["efficiency"],
    )
    return jsonify({
        "season": season, "team": team,
        "schedule": schedule_table(
            _repository().schedule(season, team=code), team=code,
            identities={row["abbreviation"]: row for row in _repository().list_teams()},
        ).as_dict(),
        "roster": roster_table(_repository().team_roster(season, code)).as_dict(),
        "depth_chart": depth_chart_table(_repository().current_depth_chart(season, code)).as_dict(),
        "snap_usage": snap_usage_table(_repository().team_snap_leaders(season, code)).as_dict(),
        "workload": usage_table(
            context["usage"]["rows"][:16],
            caption=f"{context['usage']['season']} opportunity leaders",
        ).as_dict(),
        "efficiency": context["efficiency"], "team_context": context,
        "form_charts": team_charts(form_rows),
        "record": _repository().team_record(season, code),
        "sources": _sources(team=team["name"]),
        "movements": movements, "content": _content().for_team(code),
        "pff": _team_pff(code, season),
        "position_rooms": position_rooms(
            _repository(), _pff(), season, code, pff_season,
            injuries=availability["rows"],
        ),
        "availability": availability, "staff": staff_packet["staff"],
        "staff_packet": staff_packet,
    })


def _game_packet(game_id: str) -> dict:
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    identities = {team["abbreviation"]: team for team in repository.list_teams()}
    efficiency_rows = repository.game_efficiency(game_id)
    for row in efficiency_rows:
        identity = identities.get(row["team"], {})
        row["team_logo"] = identity.get("logo_url")
        row["team_color"] = identity.get("color")
    player_rows = repository.game_player_stats(game_id)
    context = matchup_context(repository, game, identities, _pff())
    profile_season = game["season"]
    defense_profiles = {code: pass_zone_packet(
        repository.defense_pass_profile(profile_season, code), lower_is_better=True)
                        for code in (game["away_team"], game["home_team"])}
    if not any(profile["has_data"] for profile in defense_profiles.values()):
        profile_season = game["season"] - 1
        defense_profiles = {code: pass_zone_packet(
            repository.defense_pass_profile(profile_season, code), lower_is_better=True)
                            for code in (game["away_team"], game["home_team"])}
    defense_profiles = {code: pass_zone_packet(
        profile, contributors=repository.pass_zone_contributors(
            profile_season, defense_team=code,
        ), lower_is_better=True) for code, profile in defense_profiles.items()}
    passing_comparisons = []
    for offense, defense in ((game["away_team"], game["home_team"]),
                             (game["home_team"], game["away_team"])):
        candidates = repository.player_leaders_for_metrics(
            profile_season, ("attempts",), team=offense,
        )
        if profile_season != game["season"]:
            current_ids = {row["player_id"] for row in
                           repository.team_roster(game["season"], offense)}
            candidates = [row for row in candidates if row["player_id"] in current_ids]
        quarterback = next((row for row in candidates
                            if (row.get("position") or "").upper() == "QB"), None)
        qb_profile = None
        if quarterback:
            qb_profile = pass_zone_packet(
                repository.qb_pass_profile(profile_season, quarterback["player_id"]),
                contributors=repository.pass_zone_contributors(
                    profile_season, passer_player_id=quarterback["player_id"],
                ),
            )
        passing_comparisons.append({
            "offense": offense, "defense": defense, "quarterback": quarterback,
            "quarterback_profile": qb_profile, "defense_profile": defense_profiles[defense],
            "matchup": pass_matchup_packet(qb_profile, defense_profiles[defense]),
            "offense_identity": identities.get(offense, {}),
            "defense_identity": identities.get(defense, {}),
        })
    pff_season = _pff_season(game["season"]) or profile_season
    player_matchups = alignment_matchups(
        repository, _pff(), game, game["season"], pff_season, defense_profiles,
    )
    run_matchups = rushing_matchups(
        repository, _pff(), game, game["season"], pff_season, context["profiles"],
    )
    trenches = trench_matchups(
        repository, _pff(), game, game["season"], pff_season,
        context["baseline_season"], context["profiles"],
    )
    player_watches = player_matchup_watches(player_matchups, trenches)
    content_items = _content().for_game(game_id, 60)
    postgame = (postgame_packet(repository, game, efficiency_rows, player_rows)
                if game["completed"] else None)
    opponent_mode = request.args.get("opponent_mode", "total")
    if opponent_mode not in {"total", "per_game"}:
        opponent_mode = "total"
    availability = ({code: availability_packet(repository, game["season"], code)
                     for code in (game["away_team"], game["home_team"])}
                    if not game["completed"] else {})
    return {"game": game, "away_identity": identities.get(game["away_team"], {}),
            "home_identity": identities.get(game["home_team"], {}),
            "stats": game_stats_table(player_rows),
            "stat_groups": game_stat_tables(player_rows),
            "efficiency": efficiency_table(efficiency_rows, caption="Game efficiency"),
            "efficiency_rows": {row["team"]: row for row in efficiency_rows},
            "pass_profile_season": profile_season, "defense_pass_profiles": defense_profiles,
            "passing_comparisons": passing_comparisons,
            "player_matchups": player_matchups, "player_watches": player_watches,
            "pff_season": pff_season,
            "run_matchups": run_matchups,
            "trenches": trenches,
            "availability": availability,
            "postgame": postgame,
            "opponent_mode": opponent_mode,
            "content": content_items, "content_streams": _reporting_streams(content_items),
            **context}


@nfl_pages.get("/nfl/games/<game_id>/")
def game_page(game_id: str):
    return render_template("nfl_game.html", league=get_league("nfl"), **_game_packet(game_id))


@nfl_pages.get("/api/v1/nfl/games/<game_id>")
def game_api(game_id: str):
    packet = _game_packet(game_id)
    return jsonify({
        "game": packet["game"], "away_identity": packet["away_identity"],
        "home_identity": packet["home_identity"], "stats": packet["stats"].as_dict(),
        "stat_groups": [{"label": group["label"], "table": group["table"].as_dict()}
                        for group in packet["stat_groups"]],
        "efficiency": packet["efficiency"].as_dict(),
        "efficiency_rows": packet["efficiency_rows"], "records": packet["records"],
        "elo": packet["elo"],
        "profiles": packet["profiles"], "unit_cards": packet["unit_cards"],
        "baseline_season": packet["baseline_season"],
        "matchup_watches": packet["matchup_watches"],
        "production": packet["production"], "recent": packet["recent"],
        "game_shape": packet["game_shape"],
        "leaders": packet["leaders"], "history": packet["history"],
        "player_history": packet["player_history"], "market": packet["market"],
        "situation": packet["situation"],
        "personnel": packet["personnel"],
        "usage": packet["usage"],
        "pass_profile_season": packet["pass_profile_season"],
        "defense_pass_profiles": packet["defense_pass_profiles"],
        "passing_comparisons": packet["passing_comparisons"],
        "player_matchups": packet["player_matchups"], "player_watches": packet["player_watches"],
        "pff_season": packet["pff_season"],
        "run_matchups": packet["run_matchups"],
        "trenches": packet["trenches"],
        "availability": packet["availability"],
        "postgame": packet["postgame"],
        "opponent_mode": packet["opponent_mode"],
        "content": packet["content"],
    })


def _player_packet(player_id: str, season: int) -> dict:
    repository = _repository()
    player = repository.get_player(season, player_id)
    if player is None:
        abort(404)
    team_identity = repository.get_team(player["teams"][0]) if player.get("teams") else None
    weekly = repository.player_weekly(season, player_id)
    totals = repository.player_totals(season, player_id)
    chart_rows = with_last_season(weekly, repository.player_weekly(season - 1, player_id))
    passing_profile = pass_zone_packet(repository.qb_pass_profile(season, player_id))
    if player.get("position") == "QB" and not passing_profile["has_data"]:
        passing_profile = pass_zone_packet(repository.qb_pass_profile(season - 1, player_id))
    if player.get("position") == "QB" and passing_profile["has_data"]:
        passing_profile = pass_zone_packet(
            passing_profile,
            contributors=repository.pass_zone_contributors(
                passing_profile["season"], passer_player_id=player_id,
            ),
        )
    receiving_profile = pass_zone_packet(
        repository.receiver_pass_profile(season, player_id), receiver=True,
    )
    if player.get("position") in {"WR", "TE", "RB", "FB"} and not receiving_profile["has_data"]:
        receiving_profile = pass_zone_packet(
            repository.receiver_pass_profile(season - 1, player_id), receiver=True,
        )
    content_items = _content().for_player(season, player_id, 50)
    career_view = request.args.get("career") == "1"
    log_rows = repository.player_career_weekly(player_id) if career_view else weekly
    return {
        "season": season, "player": player, "career_view": career_view,
        "team_identity": team_identity or {},
        "totals": player_totals_table(totals),
        "headline_stats": player_headline_stats(totals, player.get("position")),
        "game_log": player_game_log(weekly),
        "game_log_groups": player_game_log_tables(log_rows, show_season=career_view),
        "performance_charts": player_charts(chart_rows, player.get("position")),
        "passing_profile": passing_profile,
        "receiving_profile": receiving_profile,
        "returning_usage": returning_player_usage(repository, season, player_id),
        "pff": _player_pff(player_id, season, player.get("position")),
        "content": content_items, "content_streams": _reporting_streams(content_items),
    }


@nfl_pages.get("/nfl/players/<player_id>/")
def player_page(player_id: str):
    return render_template(
        "nfl_player.html", league=get_league("nfl"), **_player_packet(player_id, _season())
    )


@nfl_pages.get("/api/v1/nfl/players/<player_id>")
def player_api(player_id: str):
    packet = _player_packet(player_id, _season())
    return jsonify({**packet, "totals": packet["totals"].as_dict(),
                    "game_log": packet["game_log"].as_dict(),
                    "game_log_groups": [
                        {"label": group["label"], "table": group["table"].as_dict()}
                        for group in packet["game_log_groups"]
                    ]})
