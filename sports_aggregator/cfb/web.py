"""College Football Today dashboard, preview pages, and structured APIs."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from zoneinfo import ZoneInfo

from flask import (Blueprint, Response, abort, current_app, jsonify,
                   render_template, request, url_for)

from sports_aggregator.catalog import get_league
from sports_aggregator.social.roles import role_label
from sports_aggregator.cfb.insights import games_to_watch
from sports_aggregator.cfb.ats import matchup_ats
from sports_aggregator.cfb.draft import position_targets, prospect_board
from sports_aggregator.cfb.draft_matchups import annotate_board, board_context
from sports_aggregator.cfb.prospects import (
    board_with_profile, consensus_board, reconcile)
from sports_aggregator.cfb.external import (
    fpi_for_game, fpi_team_season, weather_flags_by_game,
    weather_for_game, weather_summary_by_game)
from sports_aggregator.cfb.identity import (
    conference_color, conference_color_dark, conference_identity, team_identity)
from sports_aggregator.cfb.history import (
    matchup_history, matchup_player_history, team_game_history,
    team_historical_stats, upcoming_player_opponent_history)
from sports_aggregator.cfb.game_projection import narrative as projection_narrative
from sports_aggregator.cfb.game_projection import project_matchup
from sports_aggregator.cfb.matchup_research import (
    matchup_research_packet, TOTALS_RESEARCH_OVERALL, TOTALS_TRACKED_OVERALL,
    TOTALS_TRACKED_MIN_WIN_RATE)
from sports_aggregator.cfb.lines import game_lines, lines_by_game
from sports_aggregator.cfb import meta as page_meta_for
from sports_aggregator.cfb import syndication
from sports_aggregator.page_cache import cached_page
from sports_aggregator.cfb.search import search as search_entities
from sports_aggregator.cfb.situations import game_situation
from sports_aggregator.cfb.recruiting import signing_class
from sports_aggregator.cfb.roster_production import projected_depth, team_production
from sports_aggregator.cfb.schedule_shape import season_week_zero_cutoff
from sports_aggregator.cfb.transfers import notable_transfers, rank_transfers
from sports_aggregator.cfb.unit_continuity import (
    unit_continuity, units_with_continuity)
from sports_aggregator.cfb.matchups import game_matchup_report
from sports_aggregator.cfb.player_matchups import player_matchups
from sports_aggregator.cfb.page_visuals import (
    depth_formations, drive_outcome_bars, game_shape, model_probability_track,
    pff_unit_grade_bars, player_trend_chart_data, recent_form_rows,
    skill_player_trend_chart_data, team_rank_trend_chart_data, team_scoring_chart_series,
    team_trend_chart_data, unit_matchup_bars, upcoming_games_rows)
from sports_aggregator.cfb.team_game_drive_outcomes import season_summary as drive_outcome_summary
from sports_aggregator.cfb.coordinator_pace import team_drives_per_game, team_pace
from sports_aggregator.cfb.player_game_log import player_weekly_trend
from sports_aggregator.cfb.team_game_advanced import team_weekly_trend
from sports_aggregator.cfb.passing_plays import (
    matchup_field, matchup_situational, passer_career_field, passer_profile, passer_weekly_trend)
from sports_aggregator.cfb.rushing_plays import matchup_rushing, matchup_rushing_situational
from sports_aggregator.cfb.pff import pff_summary
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.two_engine_live import (
    ENGINE_A_OVERALL, ENGINE_B_OVERALL,
    _initialize_manifest as _initialize_two_engine_manifest,
    _pooled as _two_engine_pooled,
    display_packet as two_engine_display_packet, manifest_for_games,
    manifest_history_for_game,
    engine_b_rules_plain_language,
    route_plain_language, season_record as two_engine_season_record,
    season_record_first_appearance as two_engine_season_record_first_appearance,
    team_ratings_display,
)
from sports_aggregator.cfb import views
from sports_aggregator.cfb.pff_statlines import player_percentile_radar


cfb_pages = Blueprint("cfb", __name__)


def _repository() -> CFBRepository:
    return current_app.extensions["cfb_repository"]


def _season() -> int:
    configured = current_app.config.get("CFB_DEFAULT_SEASON")
    requested = request.args.get("season", type=int)
    year = requested or configured or datetime.now().year
    if year < 1869 or year > datetime.now().year + 2:
        abort(400)
    return year


def _current_season() -> int:
    """Configured live season, unaffected by historical navigation parameters."""
    return current_app.config.get("CFB_DEFAULT_SEASON") or datetime.now().year


def _game_projection(repository: CFBRepository, game: dict) -> dict:
    """One canonical projection packet for HTML and API consumers."""
    return project_matchup(
        repository, game["home_team"], game["away_team"],
        as_of_date=game.get("start_date") or datetime.now(timezone.utc).isoformat(),
        game_id=game.get("game_id"),
    )


def _reporting():
    return current_app.extensions["league_aggregation_service"].aggregate(
        get_league("college-football")
    )


def _source_registry():
    return current_app.extensions["source_registry"]


def _unified_source_registry():
    return current_app.extensions["unified_source_registry"]


def _content_repository():
    return current_app.extensions["content_repository"]


def _story_repository():
    return current_app.extensions["story_repository"]


def _local_start(value: str) -> datetime:
    timezone_name = current_app.config.get("CFB_DISPLAY_TIMEZONE", "America/New_York")
    return datetime.fromisoformat(value).astimezone(ZoneInfo(timezone_name))


def _start_label(value: str) -> str:
    return _local_start(value).strftime("%a, %b %d · %I:%M %p %Z")


def _label_games(games: list[dict]) -> list[dict]:
    """Attach display labels once; tables split date and time across two lines."""
    for game in games:
        local = _local_start(game["start_date"])
        game["start_label"] = local.strftime("%a, %b %d · %I:%M %p %Z")
        game["date_label"] = local.strftime("%a, %b %-d") if os.name != "nt" else local.strftime("%a, %b %d")
        game["time_label"] = local.strftime("%I:%M %p %Z").lstrip("0")
    return games


def _merge_stories(*groups: tuple[str, list[dict]], limit: int = 20) -> list[dict]:
    merged: list[dict] = []; seen: set[int] = set()
    for relevance, stories in groups:
        for story in stories:
            if story["story_id"] in seen:
                continue
            item = {**story, "coverage_label": relevance}
            merged.append(item); seen.add(story["story_id"])
            if len(merged) >= limit:
                return merged
    return merged


def _team_packet(team_id: int, season: int) -> dict:
    repository = _repository()
    team = repository.get_team(team_id)
    if team is None:
        abort(404)
    rankings = repository.latest_rankings(season)
    rank = next(
        (row["rank"] for row in rankings["teams"] if row["school"] == team["school"]),
        None,
    )
    team_stories = _story_repository().list_stories(team_id=team_id, limit=20)
    # The conference wire is a separate stream, not weaker team reporting. Stories
    # already linked to this team are removed so the wire is genuinely "elsewhere
    # in the conference", and each item keeps the team it is actually about.
    linked_ids = {story["story_id"] for story in team_stories}
    conference_stories = [
        story for story in _story_repository().list_stories(
            conference=team["conference"], limit=24)
        if story["story_id"] not in linked_ids
        and team_id not in {item["team_id"] for item in story.get("teams") or []}
    ][:10] if team.get("conference") else []
    # Computed once and threaded through: roster_movements does several of its
    # own queries (transfers, draft picks, recruit and PFF lookups), and
    # quality/depth-chart/production each independently asked for the exact
    # same team/season answer before they accepted it as an argument.
    movements = repository.roster_movements(team_id, season)
    return {
        "season": season,
        "team": team,
        "rank": rank,
        "ranking_poll": rankings["poll"],
        "metrics": repository.team_metrics(team["school"], season),
        "quality": repository.team_quality_snapshot(team_id, season, movements=movements),
        "schedule": _label_games(repository.team_schedule(team_id, season)),
        "roster": repository.team_roster(team["school"], season),
        "depth_chart": repository.team_depth_chart(team_id, season, movements=movements),
        "movements": movements,
        "leaders": repository.team_player_leaders(team["school"], season),
        "pff": repository.pff_team_context(team_id, repository.latest_pff_season()),
        "production": team_production(repository, team_id, season, movements=movements),
        "stories": [{**story, "coverage_label": "Team linked"} for story in team_stories],
        "conference_stories": conference_stories,
    }


def _with_conference_identity(conferences: list[dict]) -> list[dict]:
    """Attach the mark packet to each conference for the listing strips."""
    return [{**item, "identity": conference_identity(item.get("conference"))}
            for item in conferences]


def _labelled_developments(items: list[dict]) -> list[dict]:
    """Attach the reader-facing role name without losing the stored code."""
    for item in items:
        item["role_label"] = role_label(item.get("source_role"))
    return items


def _with_matchup_edges(repository: CFBRepository, games: list[dict]) -> list[dict]:
    """Attach the best graded unit matchup to each game on the slate.

    A high attention score says a game matters. The matchup edge says what to
    actually watch inside it, which is the question the dashboard could not
    answer before.
    """
    # One load for the whole slate, the way `_weekly_matchup_watches` does it:
    # without this each game fetched its own two teams' grade rows, so the
    # dashboard ran the pair of grade queries once per game.
    pff_season = repository.latest_pff_season()
    prefetched = repository.pff_matchup_rows(
        [team for game in games
         for team in (game.get("home_team_id"), game.get("away_team_id"))],
        pff_season)
    for game in games:
        report = game_matchup_report(
            repository.pff_matchups(game["home_team_id"], game["away_team_id"], pff_season,
                                    prefetched=prefetched),
            game["away_team"], game["home_team"], limit=1,
        )
        top = (report["matchups"] or [None])[0]
        game["matchup_edge"] = top["interest"] if top else None
        # Name the team that holds the advantage: an interest score alone told a
        # reader that a matchup mattered without saying who it favours.
        game["matchup_edge_team"] = (top.get("advantage") or "Even") if top else None
        game["matchup_edge_unit"] = top["label"] if top else None
        game["matchup_edge_label"] = f"{top['label']}: {top['headline']}" if top else None
    return games


def _weekly_matchup_watches(repository: CFBRepository, games: list[dict],
                            limit: int = 12) -> list[dict]:
    """Blend the best player/unit and unit/unit watches across one week."""
    watches = []
    # Every game in the slate needs the same kind of grade rows, so load them
    # for the whole week at once rather than twice per game.
    pff_season = repository.latest_pff_season()
    prefetched = repository.pff_matchup_rows(
        [team for game in games
         for team in (game.get("home_team_id"), game.get("away_team_id"))],
        pff_season)
    for game in games:
        attention = float(game.get("attention_score") or 0)
        for matchup in player_matchups(
            repository, game["home_team_id"], game["away_team_id"], limit=3
        ):
            attacker, defender = matchup["attacker"], matchup["defender"]
            members = defender.get("members") or []
            detail = (", ".join(f"{member['player_name']} {member['grade']:.1f}"
                                for member in members) or matchup["why"])
            watches.append({
                "game_id": game["game_id"], "away_team": game["away_team"],
                "home_team": game["home_team"], "start_label": game.get("start_label"),
                "kind_label": "Player vs unit" if defender.get("is_unit") else "One-on-one",
                "label": matchup["label"], "focus": attacker["player_name"],
                "focus_player_id": attacker.get("cfbd_player_id"),
                "against": defender["player_name"], "detail": detail,
                "weekly_score": round(0.8 * matchup["interest"] + 0.2 * attention, 1),
            })
        report = game_matchup_report(
            repository.pff_matchups(game["home_team_id"], game["away_team_id"], pff_season,
                                    prefetched=prefetched),
            game["away_team"], game["home_team"], limit=1)
        for matchup in report["matchups"]:
            watches.append({
                "game_id": game["game_id"], "away_team": game["away_team"],
                "home_team": game["home_team"], "start_label": game.get("start_label"),
                "kind_label": "Unit vs unit", "label": matchup["label"],
                "focus": f"{matchup['attack_team']} {matchup['attack_label']}",
                "focus_player_id": None,
                "against": f"{matchup['defend_team']} {matchup['defend_label']}",
                "detail": matchup["headline"],
                "weekly_score": round(0.8 * matchup["interest"] + 0.2 * attention, 1),
            })
    watches.sort(key=lambda item: -item["weekly_score"])
    return watches[:limit]


def _nearest_week_games(games: list[dict]) -> tuple[int | None, list[dict]]:
    """Select the next scheduled week, explicitly retaining preseason Week 0."""
    nearest = min((game.get("week") for game in games
                   if game.get("week") is not None), default=None)
    return nearest, [game for game in games if game.get("week") == nearest]


def _weekly_engine_picks(
    games: list[dict],
    signals: dict[int, dict],
    market: dict[int, dict],
) -> list[dict]:
    """Qualified frozen two-engine selections for the current week."""
    priority = {"agreement": 0, "engine_a_only": 1, "engine_b_only": 2}
    picks: list[dict] = []
    for game in games:
        packet = signals.get(int(game["game_id"]))
        if not packet or packet.get("state") not in priority:
            continue
        selected_team = packet.get("selected_team")
        selected_side = packet.get("selected_side")
        if not selected_team or selected_side not in {"home", "away"}:
            continue

        market_row = market.get(int(game["game_id"])) or {}
        home_spread = market_row.get("spread")
        pick_spread = None
        if home_spread is not None:
            pick_spread = float(home_spread) if selected_side == "home" else -float(home_spread)

        engine_a = packet.get("engine_a") or {}
        engine_b = packet.get("engine_b") or {}
        if packet.get("state") == "agreement":
            detail = "Both engines agree"
        elif packet.get("state") == "engine_a_only":
            detail = route_plain_language(engine_a.get("route")) or "Frozen Engine A route"
        else:
            rules = [
                str(rule).replace("_", " ")
                for rule in (engine_b.get("rules") or [])
            ]
            detail = " + ".join(rules) if rules else "Frozen Engine B rule"

        is_final = bool(game.get("completed")) or (
            game.get("home_points") is not None and game.get("away_points") is not None
        )
        picks.append({
            **game,
            "pick_state": packet.get("state"),
            "pick_state_label": packet.get("state_label"),
            "pick_team": selected_team,
            "pick_side": selected_side,
            "pick_spread": pick_spread,
            "pick_detail": detail,
            "pick_route": engine_a.get("route"),
            "pick_rules": engine_b.get("rules") or [],
            "pick_frozen_at": packet.get("frozen_at"),
            "pick_is_final": is_final,
            "market": market_row,
        })

    picks.sort(key=lambda row: (
        priority.get(row["pick_state"], 9),
        str(row.get("start_date") or ""),
        int(row["game_id"]),
    ))
    return picks


def _portfolio_season_record(
    repository: CFBRepository,
    season: int,
) -> dict:
    """Grade the unique non-conflicting frozen portfolio at flat -110."""
    _initialize_two_engine_manifest(repository)
    with repository._reader() as connection:
        rows = [
            dict(row) for row in connection.execute(
                """SELECT m.game_id,m.packet_json,g.home_points,g.away_points
                   FROM cfb_two_engine_manifest m
                   JOIN games g ON g.game_id=m.game_id
                   WHERE m.manifest_version=? AND m.season=?
                     AND g.completed=1
                     AND g.home_points IS NOT NULL
                     AND g.away_points IS NOT NULL""",
                ("two-engine-pregame-v1", int(season)),
            )
        ]

    wins = losses = pushes = 0
    for row in rows:
        packet = json.loads(str(row["packet_json"]))
        if packet.get("state") not in {"engine_a_only", "engine_b_only", "agreement"}:
            continue
        side = packet.get("selected_side")
        if side not in {"home", "away"}:
            continue
        lines = game_lines(repository, int(row["game_id"]))
        spread = lines.get("consensus_spread")
        if spread is None:
            continue

        home_margin = float(row["home_points"]) - float(row["away_points"])
        selected_margin = home_margin if side == "home" else -home_margin
        selected_spread = float(spread) if side == "home" else -float(spread)
        edge = selected_margin + selected_spread
        if abs(edge) < 1e-9:
            pushes += 1
        elif edge > 0:
            wins += 1
        else:
            losses += 1

    n = wins + losses + pushes
    decided = wins + losses
    net_units = wins * (100.0 / 110.0) - losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "n": n,
        "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        "hit_rate": round(wins / decided, 4) if decided else None,
        "net_units": round(net_units, 2),
        "roi": round((net_units / n) * 100.0, 1) if n else None,
        "price": "-110",
    }


def _portfolio_season_record_first_appearance(
    repository: CFBRepository,
    season: int,
) -> dict:
    """Same as _portfolio_season_record(), except each game is graded
    against the line it had the moment the non-conflicting portfolio FIRST
    qualified, not the closing line -- sourced from
    cfb_two_engine_manifest_history rather than the current manifest row."""
    _initialize_two_engine_manifest(repository)
    with repository._reader() as connection:
        rows = [
            dict(row) for row in connection.execute(
                """SELECT h.game_id,h.recorded_at,h.market_spread,h.packet_json,
                          g.home_points,g.away_points
                   FROM cfb_two_engine_manifest_history h
                   JOIN games g ON g.game_id=h.game_id
                   WHERE h.manifest_version=? AND g.season=?
                     AND g.completed=1
                     AND g.home_points IS NOT NULL
                     AND g.away_points IS NOT NULL
                   ORDER BY h.game_id, h.recorded_at""",
                ("two-engine-pregame-v1", int(season)),
            )
        ]

    wins = losses = pushes = 0
    seen_game_ids: set[int] = set()
    for row in rows:
        game_id = int(row["game_id"])
        if game_id in seen_game_ids:
            continue  # already graded this game's first qualifying appearance
        packet = json.loads(str(row["packet_json"]))
        if packet.get("state") not in {"engine_a_only", "engine_b_only", "agreement"}:
            continue
        side = packet.get("selected_side")
        if side not in {"home", "away"}:
            continue
        spread = row["market_spread"]
        if spread is None:
            continue
        seen_game_ids.add(game_id)

        home_margin = float(row["home_points"]) - float(row["away_points"])
        selected_margin = home_margin if side == "home" else -home_margin
        selected_spread = float(spread) if side == "home" else -float(spread)
        edge = selected_margin + selected_spread
        if abs(edge) < 1e-9:
            pushes += 1
        elif edge > 0:
            wins += 1
        else:
            losses += 1

    n = wins + losses + pushes
    decided = wins + losses
    net_units = wins * (100.0 / 110.0) - losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "n": n,
        "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        "hit_rate": round(wins / decided, 4) if decided else None,
        "net_units": round(net_units, 2),
        "roi": round((net_units / n) * 100.0, 1) if n else None,
        "price": "-110",
    }


@cfb_pages.get("/college-football/")
@cached_page
def today():
    season = _current_season()
    repository = _repository()
    upcoming = repository.upcoming_games(season, limit=80)
    nearest_week, week_games = _nearest_week_games(upcoming)
    watch_games = games_to_watch(upcoming)
    rankings = repository.latest_rankings(season)
    movement_stream = repository.recent_movements(season, limit=16)
    # Cards paint the team's letters in its own color, so they need the
    # contrast-checked pair rather than the raw helmet color. team_identity
    # returns a superset of the brand, so existing consumers are unaffected.
    raw_brands = repository.team_brands()
    brands = {team_id: team_identity(brand) for team_id, brand in raw_brands.items()}
    slate = _with_matchup_edges(repository, _label_games(watch_games))
    weekly_slate = _label_games(games_to_watch(week_games, limit=20))
    weekly_games_labelled = _label_games([dict(game) for game in week_games])
    market = lines_by_game(repository, season)
    frozen_ids = {
        int(game["game_id"]) for game in slate
    } | {
        int(game["game_id"]) for game in weekly_games_labelled
    }
    frozen_signals = manifest_for_games(repository, sorted(frozen_ids))
    for game in slate:
        game['market'] = market.get(game['game_id']) or {}
        game['two_engine'] = frozen_signals.get(int(game["game_id"]))
    weekly_engine_picks = _weekly_engine_picks(
        weekly_games_labelled, frozen_signals, market
    )
    portfolio_record = _portfolio_season_record(repository, season)
    national_stories = _story_repository().list_stories(limit=16)
    slate_day, slate_games = _current_slate(repository, season)
    return render_template(
        "cfb_today.html",
        slate_day=slate_day,
        slate_strip=views.scoreboard_games(
            slate_games, {}, raw_brands,
            timezone_name=current_app.config.get(
                "CFB_DISPLAY_TIMEZONE", "America/New_York"),
            lines=market),
        meta=page_meta_for.today_meta(
            season, game_count=len(slate), story_count=len(national_stories)),
        season=season,
        status=repository.status(season),
        rankings=rankings,
        games_table=views.games_to_watch_compact(slate, brands),
        watch_games=slate,
        watch_brands=brands,
        weekly_engine_picks=weekly_engine_picks,
        portfolio_record=portfolio_record,
        weekly_matchups_table=views.weekly_matchups_table(
            _weekly_matchup_watches(repository, weekly_slate), season),
        nearest_week=nearest_week,
        conferences=_with_conference_identity(repository.conferences()),
        national_stories=national_stories,
        streams=_content_repository().source_streams(limit=8),
        content_summary=_content_repository().summary(),
        developments=_labelled_developments(_content_repository().top_developments(limit=16)),
        draft_table=views.draft_panel_table(
            board_with_profile(
                repository, prospect_board(repository, roster_season=season, limit=500),
                limit=500),
            season),
        reference_tables=[
            {"label": "Rankings", "table": views.rankings_table(rankings, season, brands)},
            {"label": "Personnel movement",
             "table": views.movement_stream_table(movement_stream)},
        ],
        reporting=_reporting(),
        cfbd_configured=bool(os.getenv("CFBD_API_KEY", "").strip()),
    )


@cfb_pages.get("/college-football/elo/")
@cached_page
def elo_ratings():
    season = _season()
    repository = _repository()
    snapshot = repository.elo_snapshot(season)
    brands = repository.team_brands()

    leaders = []
    for row in snapshot["rankings"][:5]:
        leaders.append({**row, "identity": team_identity(brands.get(row["team_id"]) or {})})

    movement = {}
    for horizon, groups in snapshot["movement"].items():
        movement[horizon] = {
            direction: [
                {**row, "identity": team_identity(brands.get(row["team_id"]) or {})}
                for row in teams
            ]
            for direction, teams in groups.items()
        }

    conferences = []
    averages = [row["average_elo"] for row in snapshot["conferences"]]
    floor = min(averages, default=0)
    ceiling = max(averages, default=0)
    span = ceiling - floor
    for row in snapshot["conferences"]:
        conferences.append({
            **row,
            "identity": conference_identity(row["conference"]),
            "strength": round(18 + 82 * (row["average_elo"] - floor) / span, 1)
            if span else 100,
        })

    return render_template(
        "cfb_elo.html",
        meta=page_meta_for.elo_meta(
            season, rated_teams=snapshot["summary"]["rated_teams"]),
        season=season,
        seasons=snapshot["available_seasons"],
        summary=snapshot["summary"],
        leaders=leaders,
        movement=movement,
        conferences=conferences,
        rankings_table=views.elo_rankings_table(snapshot, season, brands),
    )


@cfb_pages.get("/college-football/visual-lab/")
def visual_lab():
    """Static, clearly labelled prototypes for proposed page visuals."""
    return render_template("cfb_visual_lab.html")


@cfb_pages.get("/college-football/conferences/<slug>/")
@cached_page
def conference_preview(slug: str):
    season = _season()
    repository = _repository()
    conference = repository.conference_by_slug(slug)
    if conference is None:
        abort(404)
    name = conference["conference"]
    leaders = repository.conference_player_leaders(name, season)
    games = _label_games(repository.conference_games(name, season, limit=24))
    return render_template(
        "cfb_conference.html",
        meta=page_meta_for.conference_meta(conference, season, game_count=len(games)),
        season=season,
        conference={**conference, "identity": conference_identity(name)},
        conference_color=conference_color(name),
        conference_color_dark=conference_color_dark(name),
        standings_table=views.standings_table(
            repository.conference_standings(name, season), season
        ),
        games_table=views.games_table(games, f"Upcoming games ({len(games)})",
                                      repository.team_brands(),
                                      repository.team_elo(season)),
        market=lines_by_game(repository, season),
        leaders=leaders,
        leader_groups=views.leader_groups(leaders, season),
        pff_table=views.pff_players_table(
            repository.conference_pff_players(
                name, repository.latest_pff_season(), roster_season=season, limit=20
            ), season, dense=True
        ),
        stories=_story_repository().list_stories(conference=name, limit=24),
    )


@cfb_pages.get("/college-football/teams/<int:team_id>/")
@cached_page
def team_preview(team_id: int):
    # A team link reached from a historical game used to carry that game's
    # `season` query parameter and silently turn the whole team page into 2025.
    # Team intelligence is always current; only the schedule switch is allowed
    # to select a completed season. Full historical browsing lives under /history/.
    season = _current_season()
    repository = _repository()
    schedule_seasons = repository.team_schedule_seasons(team_id)
    if not schedule_seasons and repository.get_team(team_id) is None:
        abort(404)
    latest_upcoming = next((row["season"] for row in schedule_seasons
                            if row["upcoming"]), None)
    current_schedule_year = max(season, latest_upcoming or season)
    requested_schedule = request.args.get("schedule_year", type=int)
    stored_years = {row["season"] for row in schedule_seasons}
    if requested_schedule is not None and (requested_schedule < 1869 or
                                             requested_schedule > datetime.now().year + 2):
        abort(400)
    schedule_year = requested_schedule if requested_schedule is not None else current_schedule_year
    stats_year = request.args.get("stats_year", season, type=int)
    if stats_year not in {season, season - 1}:
        abort(400)
    stats_mode = (request.args.get("stats_mode") or "per_game").strip().lower()
    if stats_mode not in {"total", "per_game"}:
        abort(400)
    packet = _team_packet(team_id, season)
    selected_schedule = _label_games(repository.team_schedule(team_id, schedule_year))
    schedule_is_current = schedule_year == current_schedule_year
    packet["schedule"] = selected_schedule
    prior_year = max((year for year in stored_years if year < current_schedule_year), default=None)
    schedule_options = [{"year": current_schedule_year,
                         "label": str(current_schedule_year)}]
    if prior_year is not None:
        schedule_options.append({"year": prior_year, "label": str(prior_year)})
    next_game = next((game for game in selected_schedule
                      if not game.get("completed")), None)
    return render_template(
        "cfb_team.html", **packet,
        meta=page_meta_for.team_meta(
            packet["team"], repository.brand_for(team_id), season,
            record=(packet.get("metrics") or {}).get("record"),
            next_game=next_game),
        **_team_tables(packet, season, schedule_year=schedule_year,
                       schedule_is_current=schedule_is_current,
                       stats_year=stats_year, stats_mode=stats_mode),
        schedule_year=schedule_year, schedule_options=schedule_options,
        schedule_is_current=schedule_is_current, stats_year=stats_year,
        stats_mode=stats_mode, stats_year_options=(season, season - 1),
    )


@cfb_pages.get("/college-football/teams/<int:team_id>/history/")
@cached_page
def team_history(team_id: int):
    selected = request.args.get("year", type=int)
    packet = team_game_history(_repository(), team_id, selected)
    if packet["team"] is None:
        abort(404)
    return render_template(
        "cfb_team_history.html", **packet, season=_season(),
        identity=team_identity(_repository().brand_for(team_id)),
        game_log_table=views.historical_games_table(packet["games"]),
        season_table=views.season_history_table(packet["season_summaries"]),
    )


@cfb_pages.get("/college-football/teams/<int:team_id>/history/stats/")
@cached_page
def team_history_stats(team_id: int):
    packet = team_historical_stats(_repository(), team_id)
    if packet["team"] is None:
        abort(404)
    position_identity = packet.pop("identity")
    return render_template(
        "cfb_team_history_stats.html", **packet, season=_season(),
        identity=team_identity(_repository().brand_for(team_id)),
        season_table=views.season_history_table(packet["seasons"]),
        team_stats_table=views.historical_team_stats_table(packet["team_stats"]),
        position_table=views.position_history_table(packet["positions"]),
        identity_table=views.position_history_table(position_identity, latest_only=True),
    )


def _team_tables(packet: dict, season: int, *, schedule_year: int | None = None,
                 schedule_is_current: bool = False, stats_year: int | None = None,
                 stats_mode: str = "per_game") -> dict:
    """Rendered tables for the team page, derived from the JSON packet."""
    movements = packet["movements"]
    history = team_historical_stats(_repository(), packet["team"]["team_id"])
    schedule_year = schedule_year or season
    stats_year = stats_year or season
    selected_metrics = _repository().team_metrics(packet["team"]["school"], stats_year)
    # Opponent quality describes the schedule, so it follows the schedule's
    # season selector rather than the statistics one. Reading it from stats_year
    # meant the two could disagree on screen: a 2025 schedule beside 2026
    # opponent ratings.
    opponent_quality = _repository().opponent_quality(
        packet["team"]["team_id"], schedule_year)
    projection = projected_depth(
        _repository(), packet["team"]["team_id"], season, production=packet["production"])
    team_trend = team_trend_chart_data(
        team_weekly_trend(_repository(), packet["team"]["school"], stats_year),
        _repository().team_weekly_scoring(packet["team"]["team_id"], stats_year))
    rank_trend = team_rank_trend_chart_data(
        _repository().team_elo_history(packet["team"]["team_id"], season),
        _repository().team_rank_history(packet["team"]["team_id"], season))
    pff_unit_bars = pff_unit_grade_bars(_repository().pff_team_units(
        packet["team"]["team_id"], _repository().latest_pff_season()))
    return {
        "team_trend": team_trend,
        "rank_trend": rank_trend,
        "pff_unit_bars": pff_unit_bars,
        "schedule_table": views.schedule_table(
            packet["schedule"], packet["team"]["team_id"], schedule_year,
            _repository().team_brands(), _repository().team_elo(schedule_year),
            lines_by_game(_repository(), schedule_year),
            caption=f"{schedule_year} schedule",
            week_zero_cutoff=season_week_zero_cutoff(_repository(), schedule_year),
            empty=f"No {schedule_year} schedule is stored.",
        ),
        "depth_units": views.depth_chart_tables(
            packet["depth_chart"], season, projection),
        "depth_formations": depth_formations(packet["depth_chart"], projection),
        # The interest score the dropped "Key returning production" table
        # carried, keyed both ways because a PFF row links to a roster by id
        # when it can and by name when it cannot.
        "production_groups": views.production_groups(
            packet["production"], season,
            interest={key: row["interest_score"]
                      for row in packet["pff"]["players"]
                      if row.get("interest_score") is not None
                      for key in (str(row.get("cfbd_player_id") or ""),
                                  row.get("normalized_name") or "")
                      if key}),
        "departures_table": views.movements_table(
            movements["departures"][:20], season, arrivals=False
        ),
        "quality_table": views.quality_cards_table(packet["quality"]),
        "portal_arrivals_table": views.arrivals_table(
            views.arrivals_of_kind(movements["arrivals"], ("TRANSFER_IN", "NEWCOMER"))[:10],
            season, caption="From the portal"),
        "signee_arrivals_table": views.arrivals_table(
            views.arrivals_of_kind(movements["arrivals"], ("SIGNEE",))[:10],
            season, caption="From the signing class"),
        "signing_class_table": views.signing_class_table(
            signing_class(_repository(), packet["team"]["team_id"], season)),
        "team_stats_table": views.team_summary_table(selected_metrics, stats_year, stats_mode),
        "opponent_quality_table": views.team_opponent_quality_table(
            packet["team"]["school"], opponent_quality, schedule_year,
            upcoming=schedule_is_current),
        "fpi_season": fpi_team_season(_repository(), season, packet["team"]["team_id"]),
        # Returning share always compares last season's grades to this
        # year's roster (see the matching comment in game_preview), even
        # once this season's own PFF grades exist.
        "unit_continuity_table": views.unit_continuity_table(
            units_with_continuity(_repository(), packet["team"]["team_id"],
                                  prior_season=season - 1, current_season=season),
            season - 1),
        "position_philosophy_table": views.position_philosophy_table(
            history["identity"], history["latest_production_season"],
            # Empty until the season starts producing, which is what the column
            # is for: whether this year is following last year's distribution.
            current=[row for row in history["positions"]
                     if row.get("season") == season],
            current_season=season),
        "position_philosophy_season": history["latest_production_season"],
        "draft_table": views.prospect_table(
            prospect_board(_repository(), roster_season=season, limit=10,
                           team_id=packet["team"]["team_id"]),
            season, include_team=False, dense=True),
        "identity": team_identity(_repository().brand_for(packet["team"]["team_id"])),
    }


@cfb_pages.get("/college-football/players/<player_id>/")
@cached_page
def player_preview(player_id: str):
    season = _season(); repository = _repository()
    player = repository.get_player(player_id, season)
    if player is None:
        abort(404)
    direct = _story_repository().list_stories(
        player_id=player_id, player_season=player["season"], limit=20
    )
    # Team reporting is context for a player, not reporting about him, so it is
    # shown in its own section rather than padding the player's stream.
    linked_ids = {story["story_id"] for story in direct}
    team_context = [
        story for story in _story_repository().list_stories(
            team_id=player["team_id"], limit=16)
        if story["story_id"] not in linked_ids
    ][:10] if player.get("team_id") else []
    opponent_history = (upcoming_player_opponent_history(
        repository, player_id, player["team_id"], season)
        if player.get("team_id") else {"game": None, "performances": []})
    position = str(player.get("position") or "").upper()
    if position == "QB":
        current_trend = passer_weekly_trend(repository, player_id, season)
        previous_trend = (passer_weekly_trend(repository, player_id, season - 1)
                          if len(current_trend) < 3 else [])
        player_trend = player_trend_chart_data(current_trend, previous_trend)
    elif position in {"RB", "FB", "WR", "TE"}:
        current_trend = player_weekly_trend(repository, player, season)
        previous_trend = (player_weekly_trend(repository, player, season - 1)
                          if len(current_trend) < 3 else [])
        player_trend = skill_player_trend_chart_data(current_trend, position, previous_trend)
    else:
        player_trend = None
    # Connects "how did this player do" to "how did the team do that week" --
    # checking one of these alongside the player's own metric overlays team
    # outcome on chart_workbench's second axis without forcing a shared scale.
    if player_trend and player.get("team_id"):
        player_trend = player_trend + team_scoring_chart_series(
            repository.team_weekly_scoring(player["team_id"], season),
            color_offset=len(player_trend))
    # Only meaningful for the season actually being viewed -- a career stat
    # line's older seasons aren't ranked against this season's peer pool.
    ppa_rank = (repository.player_ppa_rank(season, position).get(player_id)
               if player.get("season") == season else None)
    percentile_radar = player_percentile_radar(repository, player, season)
    return render_template(
        "cfb_player.html", season=season, player=player,
        meta=page_meta_for.player_meta(
            player, repository.brand_for(player.get("team_id"))),
        identity=team_identity(_repository().brand_for(player.get("team_id"))),
        stat_groups=views.player_stat_groups(player),
        ppa_rank=ppa_rank,
        percentile_radar=percentile_radar,
        passer_profile=passer_profile(repository, player_id, season),
        career_passing_field=passer_career_field(repository, player_id),
        player_trend=player_trend,
        pff_table=views.pff_grades_table(
            (player.get("pff") or []) + (player.get("pff_supplemental") or [])),
        pff_dataset_groups=views.pff_dataset_groups(player),
        stories=[{**story, "coverage_label": "Player linked"} for story in direct],
        team_stories=team_context,
        opponent_history=opponent_history,
        opponent_performance_table=views.opponent_performance_table(
            opponent_history["performances"], include_player=False),
    )


@cfb_pages.get("/college-football/games/<int:game_id>/")
@cached_page
def game_preview(game_id: int):
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    game["start_label"] = _start_label(game["start_date"])
    direct_stories = _story_repository().list_stories(game_id=game_id, limit=20)
    away_stories = _story_repository().list_stories(team_id=game["away_team_id"], limit=10)
    home_stories = _story_repository().list_stories(team_id=game["home_team_id"], limit=10)
    away_conference_stories = _story_repository().list_stories(
        conference=game["away_conference"], limit=6
    ) if game.get("away_conference") else []
    home_conference_stories = _story_repository().list_stories(
        conference=game["home_conference"], limit=6
    ) if game.get("home_conference") else []
    season = game["season"]
    stats_year = request.args.get("stats_year", season, type=int)
    if stats_year not in {season, season - 1}:
        abort(400)
    stats_mode = (request.args.get("stats_mode") or "per_game").strip().lower()
    if stats_mode not in {"total", "per_game"}:
        abort(400)
    away_stats = repository.team_metrics(game["away_team"], stats_year)
    home_stats = repository.team_metrics(game["home_team"], stats_year)
    away_pace = team_pace(repository, game["away_team"], [stats_year])
    home_pace = team_pace(repository, game["home_team"], [stats_year])
    away_drives = team_drives_per_game(repository, game["away_team"], [stats_year])
    home_drives = team_drives_per_game(repository, game["home_team"], [stats_year])
    away_opponents = repository.opponent_quality(game["away_team_id"], stats_year)
    home_opponents = repository.opponent_quality(game["home_team_id"], stats_year)
    home_quality = repository.team_quality_snapshot(game["home_team_id"], season)
    away_quality = repository.team_quality_snapshot(game["away_team_id"], season)
    home_leaders = repository.team_player_leaders(game["home_team"], season, 5)
    away_leaders = repository.team_player_leaders(game["away_team"], season, 5)
    home_impact = views.impact_player_index(
        repository.team_impact_players(game["home_team"], season))
    away_impact = views.impact_player_index(
        repository.team_impact_players(game["away_team"], season))
    pff_season = repository.latest_pff_season()
    home_pff = repository.pff_team_context(game["home_team_id"], pff_season, 8)
    away_pff = repository.pff_team_context(game["away_team_id"], pff_season, 8)
    pff_matchups = repository.pff_matchups(game["home_team_id"], game["away_team_id"], pff_season)
    matchup_report = game_matchup_report(pff_matchups, game["away_team"], game["home_team"])
    drive_outcomes = drive_outcome_bars(
        drive_outcome_summary(repository, game["away_team"], season),
        drive_outcome_summary(repository, game["home_team"], season),
        game["away_team"], game["home_team"])
    brands_by_school = {
        game["away_team"]: repository.brand_for(game["away_team_id"]),
        game["home_team"]: repository.brand_for(game["home_team_id"]),
    }
    away_identity = team_identity(brands_by_school[game["away_team"]])
    home_identity = team_identity(brands_by_school[game["home_team"]])
    elo = repository.team_elo(season)
    away_identity["elo"] = elo.get(game["away_team_id"]) or {}
    home_identity["elo"] = elo.get(game["home_team_id"]) or {}
    history = matchup_history(repository, game)
    # Read once: this was three identical queries for the same game.
    market = game_lines(repository, game_id)
    prior_player_games = matchup_player_history(repository, game)
    core_by_team = {
        game["away_team_id"]: repository.team_metrics(
            game["away_team"], season).get("core"),
        game["home_team_id"]: repository.team_metrics(
            game["home_team"], season).get("core"),
    }
    # Unit grades describe the most recently graded season's players. Returning
    # share is a deliberately different question -- how much of *last* season's
    # production carries into this year's roster -- so it always compares
    # season - 1 to season, even once this season's own grades are in and
    # pff_season above has moved on to display them.
    pff_game_units = repository.pff_game_units(
        game["home_team_id"], game["away_team_id"], pff_season)
    away_carry = unit_continuity(repository, game["away_team_id"],
                                 prior_season=season - 1, current_season=season)
    home_carry = unit_continuity(repository, game["home_team_id"],
                                 prior_season=season - 1, current_season=season)
    for unit in pff_game_units:
        key = (unit["dataset"], unit["position_group"])
        unit["away_returning_share"] = (away_carry.get(key) or {}).get("returning_share")
        unit["home_returning_share"] = (home_carry.get(key) or {}).get("returning_share")
    # Each of these answers one question, and the page asked several of them
    # two to four times over while building a single template call.
    fpi = fpi_for_game(repository, game_id)
    weather = weather_for_game(repository, game_id)
    away_arrivals = repository.roster_movements(game["away_team_id"], season)["arrivals"]
    home_arrivals = repository.roster_movements(game["home_team_id"], season)["arrivals"]
    projection = _game_projection(repository, game)
    research_intelligence = matchup_research_packet(
        repository, game, projection, market)
    two_engine_signal = two_engine_display_packet(
        repository, game, projection=projection, lines=market,
        research=research_intelligence)
    engine_a_route_plain = route_plain_language(
        (two_engine_signal.get("engine_a") or {}).get("route"))
    engine_b_packet = two_engine_signal.get("engine_b") or {}
    engine_b_rules_plain = engine_b_rules_plain_language(engine_b_packet.get("rules"))
    engine_b_historical = engine_b_packet.get("historical") or []
    # Sample-weighted pool across whichever Engine B rule(s) fired for this
    # game (there can be one or two) -- "Track record" needs a single figure
    # to show, the same way ENGINE_A_OVERALL/ENGINE_B_OVERALL already pool
    # across a whole history dict rather than listing each entry separately.
    engine_b_route_pooled = (
        _two_engine_pooled({item["rule"]: item for item in engine_b_historical})
        if engine_b_historical else None
    )
    team_ratings = team_ratings_display(repository, game)
    season_record = two_engine_season_record(repository, season)
    season_record_first_appearance = two_engine_season_record_first_appearance(repository, season)
    two_engine_history = manifest_history_for_game(repository, game_id)
    return render_template(
        "cfb_game.html",
        meta=page_meta_for.game_meta(
            game, away_identity, home_identity,
            weather=weather,
            story_count=len(direct_stories)),
        away_brand=away_identity,
        home_brand=home_identity,
        pff_season=pff_season,
        drive_outcomes=drive_outcomes,
        situation=game_situation(repository, game, elo),
        fpi=fpi,
        weather=views.weather_panel(weather),
        model_table=views.model_comparison_table(
            game, fpi, market, elo, core_by_team),
        model_probability=model_probability_track(game, fpi, elo, market),
        projection=projection,
        research_intelligence=research_intelligence,
        two_engine_signal=two_engine_signal,
        engine_a_route_plain=engine_a_route_plain,
        engine_b_rules_plain=engine_b_rules_plain,
        engine_b_route_pooled=engine_b_route_pooled,
        engine_a_overall=ENGINE_A_OVERALL,
        engine_b_overall=ENGINE_B_OVERALL,
        totals_overall=TOTALS_RESEARCH_OVERALL,
        totals_tracked_overall=TOTALS_TRACKED_OVERALL,
        totals_tracked_min_win_rate=TOTALS_TRACKED_MIN_WIN_RATE,
        team_ratings=team_ratings,
        season_record=season_record,
        season_record_first_appearance=season_record_first_appearance,
        two_engine_history=two_engine_history,
        projection_lines=projection_narrative(projection),
        projection_table=views.game_projection_table(projection),
        game_shape=game_shape(
            game["away_team"], game["home_team"], away_pace, home_pace,
            away_drives, home_drives, away_stats.get("advanced"), home_stats.get("advanced")),
        lines=market,
        market_table=views.market_table(market, game),
        # Every arrival ranked together, not portal additions alone: a team's
        # best signee belongs beside the transfers he is competing with.
        away_arrivals_table=views.arrivals_table(
            views.arrivals_of_kind(
                away_arrivals, ("TRANSFER_IN", "NEWCOMER"))[:5],
            season, caption=f"{game['away_team']} portal", impact=away_impact),
        away_signees_table=views.arrivals_table(
            views.arrivals_of_kind(away_arrivals, ("SIGNEE",))[:5],
            season, caption=f"{game['away_team']} signees", impact=away_impact),
        home_arrivals_table=views.arrivals_table(
            views.arrivals_of_kind(
                home_arrivals, ("TRANSFER_IN", "NEWCOMER"))[:5],
            season, caption=f"{game['home_team']} portal", impact=home_impact),
        home_signees_table=views.arrivals_table(
            views.arrivals_of_kind(home_arrivals, ("SIGNEE",))[:5],
            season, caption=f"{game['home_team']} signees", impact=home_impact),
        away_portal_in_table=views.transfer_impact_table(
            rank_transfers(repository, season=season, team_id=game["away_team_id"],
                           direction="in", limit=12), season,
            caption=f"{game['away_team']} portal additions", impact=away_impact),
        away_portal_out_table=views.transfer_impact_table(
            rank_transfers(repository, season=season, team_id=game["away_team_id"],
                           direction="out", limit=12), season,
            caption=f"{game['away_team']} portal departures", departed=True),
        home_portal_in_table=views.transfer_impact_table(
            rank_transfers(repository, season=season, team_id=game["home_team_id"],
                           direction="in", limit=12), season,
            caption=f"{game['home_team']} portal additions", impact=home_impact),
        home_portal_out_table=views.transfer_impact_table(
            rank_transfers(repository, season=season, team_id=game["home_team_id"],
                           direction="out", limit=12), season,
            caption=f"{game['home_team']} portal departures", departed=True),
        matchup_report=matchup_report,
        unit_matchup_bars_data=unit_matchup_bars(matchup_report),
        passing_field_panels=matchup_field(repository, game),
        passing_situational_panels=matchup_situational(repository, game),
        rushing_field_panels=matchup_rushing(repository, game),
        rushing_situational_panels=matchup_rushing_situational(repository, game),
        matchup_table=views.matchup_watch_table(matchup_report, brands_by_school),
        player_matchup_table=views.player_matchup_table(
            player_matchups(repository, game["home_team_id"], game["away_team_id"]),
            season),
        pff_units_table=views.pff_units_table(
            pff_game_units, game["away_team"], game["home_team"],
        ),
        game=game,
        metrics_table=views.matchup_metrics_table(
            game, repository.advanced_metric_ranks(season)),
        totals_table=views.matchup_summary_table(
            game, away_stats, home_stats, stats_year, stats_mode),
        opponent_quality_table=views.opponent_quality_table(
            game["away_team"], away_opponents, game["home_team"], home_opponents,
            stats_year),
        stats_year=stats_year, stats_mode=stats_mode,
        stats_year_options=(season, season - 1),
        preseason_table=views.preseason_context_table(
            game["away_team"], away_quality, game["home_team"], home_quality
        ),
        away_returning_table=views.pff_players_table(
            [row for row in away_pff["players"] if row.get("roster_status") == "RETURNING"],
            season, caption=f"{game['away_team']} returning", dense=True,
            impact=away_impact),
        away_departed_table=views.pff_departures_table(
            [row for row in away_pff["players"] if row.get("roster_status") not in
             (None, "RETURNING")], season, caption=f"{game['away_team']} departed"),
        home_returning_table=views.pff_players_table(
            [row for row in home_pff["players"] if row.get("roster_status") == "RETURNING"],
            season, caption=f"{game['home_team']} returning", dense=True,
            impact=home_impact),
        home_departed_table=views.pff_departures_table(
            [row for row in home_pff["players"] if row.get("roster_status") not in
             (None, "RETURNING")], season, caption=f"{game['home_team']} departed"),
        away_leader_groups=views.leader_groups(away_leaders, season, include_team=False, limit=3),
        home_leader_groups=views.leader_groups(home_leaders, season, include_team=False, limit=3),
        content_layers=_content_repository().for_game(
            game_id, (game["home_team_id"], game["away_team_id"])
        ),
        story_clusters=_merge_stories(
            ("Game linked", direct_stories), ("Away-team context", away_stories),
            ("Home-team context", home_stories),
            ("Away-conference context", away_conference_stories),
            ("Home-conference context", home_conference_stories), limit=20,
        ),
        pff_matchups=pff_matchups,
        home_pff=home_pff, away_pff=away_pff,
        home_leaders=home_leaders, away_leaders=away_leaders,
        home_quality=home_quality, away_quality=away_quality,
        history=history,
        history_games_table=views.historical_games_table(
            history["recent"], caption=f"Recent meetings — {game['away_team']} perspective"),
        away_recent_form=recent_form_rows(history["away_recent"], upcoming=upcoming_games_rows(
            _label_games(repository.team_schedule(game["away_team_id"], season)),
            game["away_team_id"], game["start_date"])),
        home_recent_form=recent_form_rows(history["home_recent"], upcoming=upcoming_games_rows(
            _label_games(repository.team_schedule(game["home_team_id"], season)),
            game["home_team_id"], game["start_date"])),
        ats=matchup_ats(repository, game, total=market.get("consensus_total")),
        prior_player_games=prior_player_games,
        prior_player_games_table=views.opponent_performance_table(prior_player_games),
    )


@cfb_pages.get("/college-football/games/<int:game_id>/box-score/")
@cached_page
def game_box_score(game_id: int):
    packet = _repository().game_box_score(game_id)
    if packet is None:
        abort(404)
    game = packet["game"]
    game["start_label"] = _start_label(game["start_date"])
    return render_template(
        "cfb_box_score.html", **packet,
        away_brand=team_identity(_repository().brand_for(game["away_team_id"])),
        home_brand=team_identity(_repository().brand_for(game["home_team_id"])),
        team_box_table=views.team_box_score_table(packet["team_stats"]),
        player_box_groups=views.player_box_score_groups(packet["player_stats"]),
    )


@cfb_pages.get("/college-football/scoreboard/")
@cached_page
def scoreboard():
    """Every game on one day, with the matchup page one click away."""
    season = _current_season()
    repository = _repository()
    zone = current_app.config.get("CFB_DISPLAY_TIMEZONE", "America/New_York")
    days = repository.scoreboard_days(season, timezone_name=zone)
    if not days:
        return render_template("cfb_scoreboard.html", season=season, day=None,
                               days=[], games=[], conferences=[], selected=None,
                               previous_day=None, next_day=None,
                               meta=page_meta_for.scoreboard_meta(season))

    available = [entry["date"] for entry in days]
    requested = (request.args.get("date") or "").strip()
    if requested and requested not in available:
        # An arbitrary date is not an error: show the nearest day that has
        # games rather than an empty page with no way forward.
        requested = min(available, key=lambda value: abs(
            (date.fromisoformat(value) - date.fromisoformat(requested)).days
        )) if _is_date(requested) else ""
    if not requested:
        today = datetime.now(ZoneInfo(zone)).date().isoformat()
        requested = next((value for value in available if value >= today), available[-1])

    index = available.index(requested)
    games = repository.games_on_day(requested, season, timezone_name=zone)
    selected = (request.args.get("conference") or "").strip() or None
    conferences = views.scoreboard_conferences(games)
    if selected and selected not in {item["slug"] for item in conferences}:
        selected = None
    selected_name = next((item["conference"] for item in conferences
                          if item["slug"] == selected), None)
    previews = _story_repository().game_previews([game["game_id"] for game in games])
    forecasts = weather_summary_by_game(repository, [game["game_id"] for game in games])
    return render_template(
        "cfb_scoreboard.html",
        meta=page_meta_for.scoreboard_meta(season, day=requested, games=len(games)),
        season=season,
        day=requested,
        days=days,
        previous_day=available[index - 1] if index > 0 else None,
        next_day=available[index + 1] if index + 1 < len(available) else None,
        conferences=conferences,
        selected=selected,
        selected_name=selected_name,
        games=views.scoreboard_games(games, previews, repository.team_brands(),
                                     timezone_name=zone, conference=selected_name,
                                     lines=lines_by_game(repository, season),
                                     weather=forecasts),
        total_games=len(games),
    )


#: Games the dashboard strip carries before deferring to the scoreboard.
#:
#: A September Saturday is sixty-eight games. Every chip costs about a
#: kilobyte, so the whole slate would be half again the weight of the page it
#: sits on top of, to show something a reader scrolls past six at a time.
SLATE_STRIP_GAMES = 15


def _current_slate(repository, season: int, *,
                   now: datetime | None = None) -> tuple[str | None, list[dict]]:
    """The day the front page should be showing, and the games nearest to now.

    Today when today has games. Otherwise yesterday, because on a Sunday
    morning the thing a reader wants is Saturday's results and not next
    weekend's schedule. Otherwise the next day that has any.

    The strip is capped, and what it keeps are the games closest to this
    moment rather than the first fifteen of the day: at one o'clock that is the
    noon window, and at nine it is the night games. Taking the head of the day
    would have shown a reader on Saturday night a screen of games that finished
    eight hours earlier.
    """
    zone = current_app.config.get("CFB_DISPLAY_TIMEZONE", "America/New_York")
    days = [entry["date"] for entry in repository.scoreboard_days(
        season, timezone_name=zone)]
    if not days:
        return None, []
    moment = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    today = moment.date()
    chosen = None
    if today.isoformat() in days:
        chosen = today.isoformat()
    else:
        yesterday = (today - timedelta(days=1)).isoformat()
        if yesterday in days:
            chosen = yesterday
        else:
            chosen = next((day for day in days if day >= today.isoformat()),
                          days[-1])
    games = repository.games_on_day(chosen, season, timezone_name=zone)
    if len(games) > SLATE_STRIP_GAMES:
        def distance(game):
            start = str(game.get("start_date") or "").replace("Z", "+00:00")
            try:
                return abs((datetime.fromisoformat(start) - moment).total_seconds())
            except ValueError:
                return float("inf")

        nearest = sorted(games, key=distance)[:SLATE_STRIP_GAMES]
        games = sorted(nearest, key=lambda game: str(game.get("start_date") or ""))
    return chosen, games


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@cfb_pages.get("/college-football/search/")
def search_page():
    season = _season()
    query = (request.args.get("q") or "").strip()
    results = (search_entities(_repository(), query, season=season, limit=10)
               if query else {"query": "", "too_short": False, "teams": [], "players": [],
                              "games": [], "stories": [], "total": 0, "season": season})
    return render_template("cfb_search.html", results=results, season=season)


@cfb_pages.get("/api/v1/cfb/search")
def search_api():
    query = (request.args.get("q") or "").strip()
    limit = min(max(request.args.get("limit", 10, type=int) or 10, 1), 40)
    return jsonify(search_entities(_repository(), query, season=_season(), limit=limit))


@cfb_pages.get("/api/v1/cfb/transfers")
def transfers_api():
    season = _season()
    team_id = request.args.get("team_id", type=int)
    direction = "out" if (request.args.get("direction") or "").strip() == "out" else "in"
    limit = min(max(request.args.get("limit", 40, type=int) or 40, 1), 200)
    rows = rank_transfers(_repository(), season=season, team_id=team_id,
                          direction=direction, limit=limit)
    return jsonify({"season": season, "direction": direction,
                    "count": len(rows), "transfers": rows})


@cfb_pages.get("/api/v1/cfb/sources/status")
def source_status_api():
    """Row counts, freshness and failures for every secondary source."""
    from sports_aggregator.cfb.external import import_status
    return jsonify(import_status(_repository()))


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/weather")
def game_weather_api(game_id: int):
    repository = _repository()
    if repository.get_game(game_id) is None:
        abort(404)
    return jsonify(weather_for_game(repository, game_id))


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/fpi")
def game_fpi_api(game_id: int):
    repository = _repository()
    if repository.get_game(game_id) is None:
        abort(404)
    return jsonify(fpi_for_game(repository, game_id))


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/situation")
def game_situation_api(game_id: int):
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    payload = game_situation(repository, game)
    payload["lines"] = game_lines(repository, game_id)
    payload["weather"] = weather_for_game(repository, game_id)
    payload["fpi"] = fpi_for_game(repository, game_id)
    return jsonify({"game_id": game_id, **payload})


@cfb_pages.get("/college-football/draft/")
@cached_page
def draft_watch():
    season = _season()
    repository = _repository()
    conference = (request.args.get("conference") or "").strip() or None
    # The filter used to reach only the position cards. Everything below is
    # built from `full_board`, so a conference pill changed a corner of the page
    # and left the 250-row board it sits above completely alone -- which reads
    # as a filter that does not work.
    full_board = prospect_board(repository, roster_season=season, limit=500,
                                conference=conference)
    # `prospect_board` applies its limit only after querying, scoring and
    # sorting the whole eligible pool, so asking for 80 and then 500 did the
    # same work twice for a different final slice. The shorter board is that
    # slice.
    board = {**full_board, "prospects": (full_board.get("prospects") or [])[:80]}
    comparison = reconcile(repository, full_board, draft_year=2027)
    # Both boards want the same schedule and the same opponent grades.
    context = board_context(repository, season=season,
                            pff_season=repository.latest_pff_season())
    annotate_board(repository, board.get("prospects") or [], season=season,
                   context=context)
    watch_entries = annotate_board(
        repository, board_with_profile(repository, full_board, limit=100),
        season=season, context=context)
    return render_template(
        "cfb_draft.html",
        season=season,
        board=board,
        comparison=comparison,
        conference=conference,
        conferences=_with_conference_identity(repository.conferences()),
        prospect_table=views.prospect_table(board, season, dense=True),
        draft_watch_table=views.draft_watch_table(
            watch_entries, season,
            caption=("2027 consensus board" if not conference
                     else f"2027 consensus board · {conference}")),
        consensus_table=views.consensus_table(
            consensus_board(repository, draft_year=2027, limit=100), season),
        agree_table=views.divergence_table(
            comparison["agree"], season, caption="Board and production agree",
            note="ranked highly and grades out",
            empty="No consensus prospect also clears the drafted-profile bar."),
        board_ahead_table=views.divergence_table(
            comparison["board_ahead"], season, caption="Board ahead of the profile",
            note="the case rests on traits this system cannot see",
            empty="No divergence of this kind."),
        profile_ahead_table=views.divergence_table(
            comparison["profile_ahead"], season, ranked=False,
            caption="Profile ahead of the board",
            note="matches drafted profiles but is unranked",
            empty="No unranked player clears the drafted-profile bar."),
        position_groups=position_targets(board),
    )


@cfb_pages.get("/api/v1/cfb/draft/consensus")
def draft_consensus_api():
    draft_year = min(max(request.args.get("draft_year", 2027, type=int) or 2027, 2020), 2035)
    limit = min(max(request.args.get("limit", 100, type=int) or 100, 1), 300)
    board = consensus_board(_repository(), draft_year=draft_year, limit=limit)
    return jsonify({"draft_year": draft_year, "count": len(board), "prospects": board})


@cfb_pages.get("/api/v1/cfb/draft/reconcile")
def draft_reconcile_api():
    season = _season()
    repository = _repository()
    board = prospect_board(repository, roster_season=season, limit=500)
    return jsonify(reconcile(repository, board, draft_year=2027))


@cfb_pages.get("/api/v1/cfb/draft/board")
def draft_board_api():
    season = _season()
    limit = min(max(request.args.get("limit", 50, type=int) or 50, 1), 200)
    conference = (request.args.get("conference") or "").strip() or None
    team_id = request.args.get("team_id", type=int)
    return jsonify(prospect_board(_repository(), roster_season=season, limit=limit,
                                  conference=conference, team_id=team_id))


@cfb_pages.get("/college-football/admin/links/")
def link_audit():
    kind = "team" if (request.args.get("kind") or "").strip() == "team" else "player"
    method = (request.args.get("method") or "").strip() or None
    limit = min(max(request.args.get("limit", 120, type=int) or 120, 1), 400)
    return render_template("cfb_links.html", audit=_content_repository().link_audit(
        kind=kind, method=method, limit=limit))


@cfb_pages.get("/api/v1/cfb/links")
def link_audit_api():
    kind = "team" if (request.args.get("kind") or "").strip() == "team" else "player"
    method = (request.args.get("method") or "").strip() or None
    limit = min(max(request.args.get("limit", 100, type=int) or 100, 1), 400)
    return jsonify(_content_repository().link_audit(kind=kind, method=method, limit=limit))


@cfb_pages.get("/college-football/admin/sources/")
def source_admin():
    return render_template("cfb_sources.html", **_source_registry().status())


@cfb_pages.get("/college-football/admin/source-graph/")
def source_graph_admin():
    return render_template("cfb_source_graph.html", **_unified_source_registry().status())


@cfb_pages.get("/api/v1/cfb/status")
def status_api():
    repository = _repository()
    payload = repository.status(_season())
    payload["cfbd_configured"] = bool(os.getenv("CFBD_API_KEY", "").strip())
    payload["stat_coverage"] = repository.stat_coverage()
    payload["content"] = _content_repository().summary()
    return jsonify(payload)


@cfb_pages.get("/api/v1/cfb/sources")
def sources_api():
    return jsonify(_source_registry().status())


@cfb_pages.get("/api/v1/cfb/source-entities")
def source_entities_api():
    return jsonify(_unified_source_registry().status())


@cfb_pages.get("/api/v1/cfb/content")
def content_api():
    limit = min(max(request.args.get("limit", 50, type=int) or 50, 1), 100)
    items = _content_repository().recent(limit)
    return jsonify({"count": len(items), "items": items})


@cfb_pages.get("/api/v1/cfb/stories")
def stories_api():
    limit = min(max(request.args.get("limit", 30, type=int) or 30, 1), 100)
    stories = _story_repository().list_stories(limit=limit)
    return jsonify({"count": len(stories), "stories": stories})


@cfb_pages.get("/api/v1/cfb/developments")
def developments_api():
    limit = min(max(request.args.get("limit", 25, type=int) or 25, 1), 100)
    days = min(max(request.args.get("days", 7, type=int) or 7, 1), 60)
    items = _content_repository().top_developments(limit=limit, days=days)
    return jsonify({"count": len(items), "days": days, "developments": items})


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/player-matchups")
def game_player_matchups_api(game_id: int):
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    matchups = player_matchups(repository, game["home_team_id"], game["away_team_id"])
    return jsonify({"game_id": game_id, "count": len(matchups), "matchups": matchups})


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/matchups")
def game_matchups_api(game_id: int):
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    pff_season = repository.latest_pff_season()
    report = game_matchup_report(
        repository.pff_matchups(game["home_team_id"], game["away_team_id"], pff_season),
        game["away_team"], game["home_team"],
    )
    return jsonify({"game_id": game_id, "pff_season": pff_season, **report})


@cfb_pages.get("/api/v1/cfb/pff/summary")
def pff_summary_api():
    default_season = _repository().latest_pff_season()
    season = request.args.get("season", default_season, type=int) or default_season
    if season < 1869 or season > datetime.now().year:
        abort(400)
    return jsonify(pff_summary(_repository(), season))


@cfb_pages.get("/api/v1/cfb/games")
def games_api():
    season = _season()
    limit = min(max(request.args.get("limit", 25, type=int) or 25, 1), 100)
    games = _repository().upcoming_games(season, limit=limit)
    return jsonify({"season": season, "count": len(games), "games": games})


@cfb_pages.get("/api/v1/cfb/games-to-watch")
def games_to_watch_api():
    season = _season()
    limit = min(max(request.args.get("limit", 10, type=int) or 10, 1), 50)
    games = games_to_watch(_repository().upcoming_games(season, limit=100), limit=limit)
    return jsonify({"season": season, "count": len(games), "games": games})


@cfb_pages.get("/api/v1/cfb/matchups-to-watch")
def matchups_to_watch_api():
    season = _season()
    repository = _repository()
    upcoming = repository.upcoming_games(season, limit=100)
    nearest_week, week_games = _nearest_week_games(upcoming)
    week_games = _label_games(games_to_watch(
        week_games, limit=20))
    matchups = _weekly_matchup_watches(repository, week_games, limit=20)
    return jsonify({"season": season, "week": nearest_week,
                    "count": len(matchups), "matchups": matchups})


@cfb_pages.get("/api/v1/cfb/teams")
def teams_api():
    limit = min(max(request.args.get("limit", 150, type=int) or 150, 1), 200)
    conference = (request.args.get("conference") or "").strip() or None
    teams = _repository().teams(conference=conference, limit=limit)
    return jsonify({"count": len(teams), "conference": conference, "teams": teams})


@cfb_pages.get("/api/v1/cfb/teams/<int:team_id>")
def team_api(team_id: int):
    return jsonify(_team_packet(team_id, _season()))


@cfb_pages.get("/api/v1/cfb/teams/<int:team_id>/history")
def team_history_api(team_id: int):
    packet = team_game_history(_repository(), team_id, request.args.get("year", type=int))
    if packet["team"] is None:
        abort(404)
    return jsonify(packet)


@cfb_pages.get("/api/v1/cfb/teams/<int:team_id>/history/stats")
def team_history_stats_api(team_id: int):
    packet = team_historical_stats(_repository(), team_id)
    if packet["team"] is None:
        abort(404)
    return jsonify(packet)


@cfb_pages.get("/api/v1/cfb/players/<player_id>")
def player_api(player_id: str):
    season = _season(); player = _repository().get_player(player_id, season)
    if player is None:
        abort(404)
    player["stories"] = _story_repository().list_stories(
        player_id=player_id, player_season=player["season"], limit=20
    )
    return jsonify(player)


@cfb_pages.get("/api/v1/cfb/conferences")
def conferences_api():
    conferences = _repository().conferences()
    return jsonify({"count": len(conferences), "conferences": conferences})


@cfb_pages.get("/api/v1/cfb/conferences/<slug>")
def conference_api(slug: str):
    season = _season()
    repository = _repository()
    conference = repository.conference_by_slug(slug)
    if conference is None:
        abort(404)
    name = conference["conference"]
    return jsonify({
        "season": season,
        "conference": conference,
        "standings": repository.conference_standings(name, season),
        "games": repository.conference_games(name, season),
        "player_leaders": repository.conference_player_leaders(name, season),
        "pff_players": repository.conference_pff_players(
            name, repository.latest_pff_season(), roster_season=season, limit=20),
        "stories": _story_repository().list_stories(conference=name, limit=24),
    })


@cfb_pages.get("/api/v1/cfb/rankings")
def rankings_api():
    season = _season()
    payload = _repository().latest_rankings(season)
    payload["season"] = season
    payload["count"] = len(payload["teams"])
    return jsonify(payload)


@cfb_pages.get("/api/v1/cfb/elo")
def elo_api():
    """The same current FBS Elo snapshot used by the display page."""
    return jsonify(_repository().elo_snapshot(_season()))


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>")
def game_api(game_id: int):
    game = _repository().get_game(game_id)
    if game is None:
        abort(404)
    return jsonify(game)


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/box-score")
def game_box_score_api(game_id: int):
    packet = _repository().game_box_score(game_id)
    if packet is None:
        abort(404)
    return jsonify(packet)


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/preview")
def game_preview_api(game_id: int):
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    direct_stories = _story_repository().list_stories(game_id=game_id, limit=20)
    away_stories = _story_repository().list_stories(team_id=game["away_team_id"], limit=10)
    home_stories = _story_repository().list_stories(team_id=game["home_team_id"], limit=10)
    away_conference_stories = _story_repository().list_stories(
        conference=game["away_conference"], limit=6
    ) if game.get("away_conference") else []
    home_conference_stories = _story_repository().list_stories(
        conference=game["home_conference"], limit=6
    ) if game.get("home_conference") else []
    pff_season = repository.latest_pff_season()
    return jsonify({
        "game": game,
        "projection": _game_projection(repository, game),
        "pff_season": pff_season,
        "pff_units": repository.pff_game_units(
            game["home_team_id"], game["away_team_id"], pff_season
        ),
        "pff_matchups": repository.pff_matchups(
            game["home_team_id"], game["away_team_id"], pff_season
        ),
        "player_unit_watches": player_matchups(
            repository, game["home_team_id"], game["away_team_id"]),
        "home_pff": repository.pff_team_context(game["home_team_id"], pff_season, 8),
        "away_pff": repository.pff_team_context(game["away_team_id"], pff_season, 8),
        "home_leaders": repository.team_player_leaders(game["home_team"], game["season"], 5),
        "away_leaders": repository.team_player_leaders(game["away_team"], game["season"], 5),
        "home_quality": repository.team_quality_snapshot(game["home_team_id"], game["season"]),
        "away_quality": repository.team_quality_snapshot(game["away_team_id"], game["season"]),
        "history": matchup_history(repository, game),
        "prior_player_games": matchup_player_history(repository, game),
        "stories": _merge_stories(
            ("Game linked", direct_stories), ("Away-team context", away_stories),
            ("Home-team context", home_stories),
            ("Away-conference context", away_conference_stories),
            ("Home-conference context", home_conference_stories), limit=20,
        ),
    })


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/projection")
def game_projection_api(game_id: int):
    """Team opportunity/yardage contract for the matchup and future allocators."""
    repository = _repository()
    game = repository.get_game(game_id)
    if game is None:
        abort(404)
    projection = _game_projection(repository, game)
    return jsonify({"game_id": game_id, **projection})


@cfb_pages.get("/api/v1/cfb/games/<int:game_id>/content")
def game_content_api(game_id: int):
    game = _repository().get_game(game_id)
    if game is None:
        abort(404)
    return jsonify(_content_repository().for_game(
        game_id, (game["home_team_id"], game["away_team_id"])
    ))


@cfb_pages.get("/api/v1/cfb/teams/resolve")
def resolve_team_api():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"error": "q must contain at least two characters"}), 400
    matches = _repository().resolve_team_alias(query)
    return jsonify({"query": query, "count": len(matches), "matches": matches})


# ---------------------------------------------------------------------------
# Syndication and discovery
#
# These endpoints are the only outbound surfaces. Feeds link to the original
# publisher rather than back into this site, so attribution survives the hop;
# see sports_aggregator.cfb.syndication for the reasoning.
# ---------------------------------------------------------------------------

def _absolute(path: str) -> str:
    return request.url_root.rstrip("/") + path


def _xml(body: str, content_type: str) -> Response:
    response = Response(body, mimetype=content_type)
    response.headers["Cache-Control"] = "public, max-age=900"
    return response


@cfb_pages.get("/college-football/feed.xml")
def national_feed():
    stories = _story_repository().list_stories(limit=syndication.RSS_ITEM_LIMIT)
    return _xml(syndication.rss_feed(
        title="College Football Reporting",
        description="Clustered college-football reporting, attributed to the "
                    "publisher that filed it.",
        link=_absolute("/college-football/"),
        self_url=_absolute("/college-football/feed.xml"),
        items=syndication.story_items(stories),
    ), "application/rss+xml")


@cfb_pages.get("/college-football/teams/<int:team_id>/feed.xml")
def team_feed(team_id: int):
    team = _repository().get_team(team_id)
    if team is None:
        abort(404)
    stories = _story_repository().list_stories(
        team_id=team_id, limit=syndication.RSS_ITEM_LIMIT)
    return _xml(syndication.rss_feed(
        title=f"{team['school']} Reporting",
        description=f"Reporting linked to {team['school']}, attributed to the "
                    "publisher that filed it.",
        link=_absolute(f"/college-football/teams/{team_id}/"),
        self_url=_absolute(f"/college-football/teams/{team_id}/feed.xml"),
        items=syndication.story_items(stories),
    ), "application/rss+xml")


@cfb_pages.get("/sitemap.xml")
def sitemap_xml():
    repository = _repository()
    season = _current_season()
    entries: list[dict] = [
        {"loc": _absolute("/college-football/"), "changefreq": "hourly", "priority": 1.0},
        {"loc": _absolute("/college-football/draft/"), "changefreq": "weekly", "priority": 0.6},
        {"loc": _absolute("/college-football/scoreboard/"),
         "changefreq": "daily", "priority": 0.8},
    ]
    for conference in repository.conferences():
        if conference.get("slug"):
            entries.append({
                "loc": _absolute(f"/college-football/conferences/{conference['slug']}/"),
                "changefreq": "daily", "priority": 0.7})
    for team_id in sorted(repository.team_brands()):
        entries.append({"loc": _absolute(f"/college-football/teams/{team_id}/"),
                        "changefreq": "daily", "priority": 0.8})
    # Only games that exist on the schedule; a preview for an unplayed game is
    # the most valuable page here, so it outranks the static sections.
    for game in repository.upcoming_games(season, limit=400):
        entries.append({"loc": _absolute(f"/college-football/games/{game['game_id']}/"),
                        "lastmod": game.get("start_date"),
                        "changefreq": "daily", "priority": 0.9})
    return _xml(syndication.sitemap(entries), "application/xml")


@cfb_pages.get("/robots.txt")
def robots_txt():
    response = Response(syndication.robots(_absolute("/sitemap.xml")),
                        mimetype="text/plain")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response
