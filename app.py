import os
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess
import sys

import click
from flask import Flask, abort, jsonify, render_template, request, session
from dotenv import load_dotenv

from sports_aggregator.cfb.refresh_window import profile_for
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.nflverse import current_season as current_nfl_season
from sports_aggregator.nfl.web import nfl_pages
from sports_aggregator.cfb.web import cfb_pages
from sports_aggregator.cfb.data_status import data_status_pages
from sports_aggregator.cfb.data_import import data_import_pages
from sports_aggregator.cfb.conference_features import (
    conference_schedule_elo,
    install_market_freshness_note,
    zap_content_url,
)
from sports_aggregator.cfb.conference_extras import (
    conference_leader_packet,
    team_schedule_elo,
)
from sports_aggregator.cfb.model_market_display import install_model_comparison_display
from sports_aggregator.cfb.wiki_context_enrichment import install_wiki_context_enrichment
from sports_aggregator.cfb.coordinator_display import install_coordinator_display
from sports_aggregator.cfb.cfbdepth_display import install_cfbdepth_display
from sports_aggregator.cfb.cfbdepth_enhancements import install_cfbdepth_enhancements
from sports_aggregator.cfb.passing_display import install_passing_display
from sports_aggregator.cfb.player_game_log import player_game_log_table
from sports_aggregator.cfb.player_career_context import player_career_context
from sports_aggregator.catalog import list_leagues
from sports_aggregator.service import build_default_service
from sports_aggregator.social.registry import SourceRegistry
from sports_aggregator.social.unified import UnifiedSourceRegistry
from sports_aggregator.social.source_admin import add_or_update_source
from sports_aggregator.social.content import ContentRepository
from sports_aggregator.social.stories import StoryRepository
from sports_aggregator.cfb.repository import _logo_pair
from sports_aggregator.cfb.views import height_label
from sports_aggregator.social.roles import role_label
from sports_aggregator.tables import format_value
from sports_aggregator.client_cache import install_client_caching
from sports_aggregator.compression import install_compression
from sports_aggregator.page_cache import cache
from sports_aggregator.scheduled_refresh import REFRESH_PROFILES
from sports_aggregator.tracked_refresh import SEGMENTS
from sports_aggregator.web import league_pages
load_dotenv()


def _env_flag(name: str, default: bool) -> bool:
    fallback = "1" if default else "0"
    return os.getenv(name, fallback).strip().lower() not in {"0", "false", "no", "off"}


def _legacy_dashboards_default() -> bool:
    return not _env_flag("RENDER", False)


def _tail_lines(path: Path, limit: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=limit)]
    except OSError:
        return []


def _cache_dir(app: Flask) -> str:
    configured = (os.getenv("CFB_PAGE_CACHE_DIR") or "").strip()
    if configured:
        return configured
    database = Path(os.getenv("CFB_DATABASE_PATH") or os.path.join(app.instance_path, "cfb.sqlite3"))
    return str(database.parent / "page_cache")


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    refresh_token = os.getenv("CFB_REFRESH_TOKEN", "")

    app.config.from_mapping(
        CACHE_TYPE="FileSystemCache",
        CACHE_DIR=_cache_dir(app),
        CACHE_THRESHOLD=int(os.getenv("CFB_PAGE_CACHE_THRESHOLD", "150")),
        CACHE_DEFAULT_TIMEOUT=int(os.getenv("CFB_PAGE_CACHE_SECONDS", "900")),
        REGISTER_LEGACY_DASHBOARDS=_env_flag(
            "REGISTER_LEGACY_DASHBOARDS", _legacy_dashboards_default()),
        CFB_DEFAULT_SEASON=int(os.getenv("CFB_DEFAULT_SEASON", "0")) or None,
        CFB_DISPLAY_TIMEZONE=os.getenv("CFB_DISPLAY_TIMEZONE", "America/New_York"),
        CFB_REFRESH_TOKEN=refresh_token,
        CFB_ADMIN_PIN=os.getenv("CFB_ADMIN_PIN", "").strip(),
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "").strip() or refresh_token or secrets.token_hex(32),
        CFB_DATABASE_PATH=os.getenv(
            "CFB_DATABASE_PATH", os.path.join(app.instance_path, "cfb.sqlite3")
        ),
        NFL_DATABASE_PATH=os.getenv(
            "NFL_DATABASE_PATH", os.path.join(app.instance_path, "nfl.sqlite3")
        ),
        NFLVERSE_RAW_CACHE_PATH=os.getenv(
            "NFLVERSE_RAW_CACHE_PATH", os.path.join(app.instance_path, "nflverse_raw")
        ),
        NFL_PFF_SOURCE_ROOT=os.getenv(
            "NFL_PFF_SOURCE_ROOT", r"C:\Users\ehari\Desktop\scouting_report"
        ),
        NFL_AUTO_SEED=_env_flag("NFL_AUTO_SEED", False),
        CFBD_RAW_CACHE_PATH=os.getenv(
            "CFBD_RAW_CACHE_PATH", os.path.join(app.instance_path, "cfbd_raw")
        ),
    )
    if test_config:
        app.config.update(test_config)

    # Registered before every other after_request hook so that it runs after
    # them: Flask walks that list in reverse.
    install_compression(app)
    install_client_caching(app)
    cache.init_app(app)
    app.extensions["league_aggregation_service"] = app.config.get(
        "LEAGUE_AGGREGATION_SERVICE"
    ) or build_default_service()
    app.extensions["cfb_repository"] = app.config.get("CFB_REPOSITORY") or CFBRepository(
        app.config["CFB_DATABASE_PATH"]
    )
    app.extensions["nfl_repository"] = app.config.get("NFL_REPOSITORY") or NFLRepository(
        app.config["NFL_DATABASE_PATH"]
    )
    # Reads share one connection for the life of a request; this is the end at
    # which it is closed. See CFBRepository._reader.
    app.teardown_appcontext(CFBRepository.close_request_connections)
    source_database_path = app.extensions["cfb_repository"].path
    app.extensions["source_registry"] = app.config.get("SOURCE_REGISTRY") or SourceRegistry(source_database_path)
    app.extensions["unified_source_registry"] = app.config.get("UNIFIED_SOURCE_REGISTRY") or UnifiedSourceRegistry(source_database_path)
    app.extensions["content_repository"] = app.config.get("CONTENT_REPOSITORY") or ContentRepository(source_database_path)
    app.extensions["story_repository"] = app.config.get("STORY_REPOSITORY") or StoryRepository(source_database_path)

    install_market_freshness_note()
    install_model_comparison_display()
    install_wiki_context_enrichment()
    install_coordinator_display(app)
    install_cfbdepth_display(app)
    install_cfbdepth_enhancements(app)
    install_passing_display(app)

    def require_refresh_auth() -> None:
        if session.get("cfb_admin") is True:
            return
        refresh_expected = str(app.config.get("CFB_REFRESH_TOKEN") or "").strip()
        pin_expected = str(app.config.get("CFB_ADMIN_PIN") or "").strip()
        provided = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        valid_refresh = bool(refresh_expected and provided and secrets.compare_digest(provided, refresh_expected))
        valid_pin = bool(pin_expected and provided and secrets.compare_digest(provided, pin_expected))
        if valid_refresh or valid_pin:
            if valid_pin:
                session["cfb_admin"] = True
                session.permanent = True
            return
        if not refresh_expected and not pin_expected:
            abort(503, description="CFB_REFRESH_TOKEN or CFB_ADMIN_PIN is not configured")
        abort(401)

    @app.route("/")
    def index():
        return render_template("index.html", leagues=list_leagues(), legacy_dashboards=app.config["REGISTER_LEGACY_DASHBOARDS"])

    @app.post("/internal/cfb-admin-login")
    def cfb_admin_login():
        expected = str(app.config.get("CFB_ADMIN_PIN") or "").strip()
        if not expected:
            abort(503, description="CFB_ADMIN_PIN is not configured")
        payload = request.get_json(silent=True) or request.form
        provided = str(payload.get("pin") or "").strip()
        if not provided or not secrets.compare_digest(provided, expected):
            abort(401)
        session["cfb_admin"] = True
        session.permanent = True
        return jsonify({"status": "ok"})

    @app.post("/internal/cfb-admin-logout")
    def cfb_admin_logout():
        session.pop("cfb_admin", None)
        return jsonify({"status": "ok"})

    @app.get("/college-football/source-admin/")
    def cfb_source_admin():
        return render_template("cfb_source_admin.html")

    @app.post("/internal/cfb-source-seed")
    def cfb_source_seed():
        require_refresh_auth()
        payload = request.get_json(silent=True) or request.form
        try:
            result = add_or_update_source(
                app.extensions["unified_source_registry"],
                app.extensions["source_registry"],
                dict(payload),
            )
        except (TypeError, ValueError) as exc:
            abort(400, description=str(exc))
        cache.clear()
        return jsonify({"status": "seeded", **result})

    @app.post("/internal/cfb-refresh")
    def start_cfb_refresh():
        require_refresh_auth()
        profile = (request.args.get("profile") or "light").strip().casefold()
        season = app.config.get("CFB_DEFAULT_SEASON") or datetime.now().year
        decision = None
        if profile == "auto":
            decision = profile_for(app.extensions["cfb_repository"], season=season)
            profile = decision["profile"]
            if profile is None:
                return jsonify({"status": "skipped", "season": season, **decision}), 200
        elif profile not in REFRESH_PROFILES:
            abort(400, description="profile must be auto or one of " + ", ".join(sorted(REFRESH_PROFILES)))

        # A named segment runs now rather than when its hour comes round. The
        # analytics segment in particular exists to be backfilled on demand:
        # its steps are the expensive ones and they only have an hour a day.
        segment = (request.args.get("segment") or "").strip().casefold() or None
        if segment and segment not in SEGMENTS:
            abort(400, description="segment must be one of " + ", ".join(sorted(SEGMENTS)))

        root = Path(__file__).resolve().parent
        command = [sys.executable, "-m", "sports_aggregator.tracked_refresh",
                   "--season", str(season), "--profile", profile]
        if segment:
            command += ["--segment", segment]
        subprocess.Popen(
            command,
            cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )
        return jsonify({"status": "accepted", "season": season, "profile": profile,
                        **({"segment": segment} if segment else {}),
                        **({"reason": decision["reason"], "games": decision["games"]} if decision else {})}), 202

    @app.post("/internal/cfb-content-zap")
    def cfb_content_zap():
        require_refresh_auth()
        payload = request.get_json(silent=True) or request.form
        target = str(payload.get("url") or "").strip()
        try:
            result = zap_content_url(app.extensions["content_repository"], target)
        except ValueError as exc:
            abort(400, description=str(exc))
        cache.clear()
        return jsonify({"status": "zapped", **result})

    @app.get("/internal/cfb-refresh-status")
    def cfb_refresh_status():
        require_refresh_auth()
        database = Path(app.config["CFB_DATABASE_PATH"])
        instance = database.parent
        logs = instance / "refresh_logs"
        candidates = sorted(logs.glob("refresh-*.log"), key=lambda path: path.stat().st_mtime, reverse=True) if logs.exists() else []
        latest_log = candidates[0] if candidates else None
        history_path = instance / "scheduled_refresh_history.jsonl"
        history_lines = _tail_lines(history_path, 1)
        last_refresh = None
        if history_lines:
            try:
                last_refresh = json.loads(history_lines[-1])
            except json.JSONDecodeError:
                last_refresh = {"status": "unreadable_history_record"}
        lock_path = instance / "scheduled_refresh.lock"
        lock = None
        if lock_path.exists():
            try:
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                lock = {"present": True}
        return jsonify({
            "running": lock_path.exists(), "lock": lock, "last_refresh": last_refresh,
            "latest_log": str(latest_log) if latest_log else None,
            "latest_log_lines": _tail_lines(latest_log, 100) if latest_log else [],
        })

    app.jinja_env.filters["cell"] = format_value
    app.jinja_env.filters["height"] = height_label
    app.jinja_env.filters["role"] = role_label
    app.jinja_env.filters["logo_pair"] = _logo_pair
    app.jinja_env.globals["conference_elo_summary"] = lambda conference, season: conference_schedule_elo(
        app.extensions["cfb_repository"], conference, season
    )
    app.jinja_env.globals["conference_leader_packet"] = lambda conference, season: conference_leader_packet(
        app.extensions["cfb_repository"], conference, season
    )
    app.jinja_env.globals["team_elo_summary"] = lambda team_id, season: team_schedule_elo(
        app.extensions["cfb_repository"], int(team_id), int(season)
    )
    app.jinja_env.globals["player_game_log"] = lambda player, season: player_game_log_table(
        app.extensions["cfb_repository"], player, int(season)
    )
    app.jinja_env.globals["player_career_context"] = lambda player: player_career_context(
        app.extensions["cfb_repository"], player
    )

    app.register_blueprint(league_pages)
    app.register_blueprint(nfl_pages)
    app.register_blueprint(cfb_pages)
    app.register_blueprint(data_status_pages)
    app.register_blueprint(data_import_pages)

    @app.cli.command("sync-cfb")
    @click.option("--year", type=int, default=lambda: app.config.get("CFB_DEFAULT_SEASON") or datetime.now().year)
    @click.option("--force", is_flag=True, help="Bypass cached CFBD responses.")
    @click.option("--basic", is_flag=True, help="Skip advanced stats and CORE ratings.")
    def sync_cfb(year: int, force: bool, basic: bool) -> None:
        from sports_aggregator.cfb.cfbd import CFBDClient, CFBDConfigurationError
        from sports_aggregator.cfb.sync import CFBDataSync
        client = CFBDClient(raw_cache_path=app.config["CFBD_RAW_CACHE_PATH"])
        if not client.configured:
            raise click.ClickException(str(CFBDConfigurationError("CFBD_API_KEY is required")))
        report = CFBDataSync(client, app.extensions["cfb_repository"]).sync(year, force=force, include_advanced=not basic)
        for dataset in report.datasets:
            click.echo(f"{dataset.dataset}: {dataset.status} ({dataset.count})")
        if not report.succeeded:
            raise click.ClickException("One or more CFBD datasets failed; inspect application logs.")

    @app.cli.command("sync-nfl")
    @click.option("--year", type=int, default=current_nfl_season)
    @click.option("--force", is_flag=True, help="Bypass cached nflverse release assets.")
    @click.option("--skip-pbp", is_flag=True,
                  help="Seed lighter schedule, roster, and box-score datasets first.")
    def sync_nfl(year: int, force: bool, skip_pbp: bool) -> None:
        """Sync canonical NFL teams, games, rosters, and weekly statistics."""
        from sports_aggregator.nfl.nflverse import NflverseClient
        from sports_aggregator.nfl.sync import NFLDataSync
        client = NflverseClient(app.config["NFLVERSE_RAW_CACHE_PATH"])
        report = NFLDataSync(client, app.extensions["nfl_repository"]).sync(
            year, force=force, include_pbp=not skip_pbp,
        )
        for dataset in report.datasets:
            click.echo(f"{dataset.dataset}: {dataset.status} ({dataset.count})")
        from sports_aggregator.nfl.espn import sync_espn_context
        context = sync_espn_context(
            app.extensions["nfl_repository"], app.config["NFLVERSE_RAW_CACHE_PATH"],
            year, force=force,
        )
        click.echo(f"nfl_context: success (staff={context['staff']}, injuries={context['injuries']})")
        from sports_aggregator.nfl.source_directory import DEFAULT_PATH, import_directory
        if DEFAULT_PATH.exists():
            sources = import_directory(app.extensions["source_registry"], DEFAULT_PATH)
            click.echo(f"nfl_sources: success ({sources})")
        if not report.succeeded:
            raise click.ClickException("One or more nflverse datasets failed; inspect application logs.")

    @app.cli.command("sync-nfl-history")
    @click.option("--start-year", type=click.IntRange(1999, 2100), default=2010, show_default=True)
    @click.option("--end-year", type=click.IntRange(1999, 2100), default=lambda: current_nfl_season() - 1)
    @click.option("--force", is_flag=True, help="Bypass cached nflverse release assets.")
    @click.option("--skip-pbp", is_flag=True, help="Load schedules and weekly stats without PBP analytics.")
    def sync_nfl_history(start_year: int, end_year: int, force: bool, skip_pbp: bool) -> None:
        """Backfill NFL schedule, player, team, and play-by-play history sequentially."""
        if end_year < start_year:
            raise click.ClickException("--end-year must be at least --start-year")
        from sports_aggregator.nfl.nflverse import NflverseClient
        from sports_aggregator.nfl.sync import NFLDataSync
        client = NflverseClient(app.config["NFLVERSE_RAW_CACHE_PATH"])
        result = NFLDataSync(client, app.extensions["nfl_repository"]).sync_history(
            start_year, end_year, force=force, include_pbp=not skip_pbp,
        )
        click.echo("nfl_history: " + json.dumps(result, sort_keys=True))
        cache.clear()

    @app.cli.command("sync-nfl-rosters")
    @click.option("--year", type=int, default=current_nfl_season)
    @click.option("--force", is_flag=True, help="Bypass the cached nflverse roster release.")
    def sync_nfl_rosters(year: int, force: bool) -> None:
        """Refresh only the canonical current NFL roster for a season."""
        from sports_aggregator.nfl.nflverse import NflverseClient
        from sports_aggregator.nfl.sync import NFLDataSync
        client = NflverseClient(app.config["NFLVERSE_RAW_CACHE_PATH"])
        count = NFLDataSync(client, app.extensions["nfl_repository"]).sync_players(
            year, force=force,
        )
        click.echo(f"players: success ({count})")
        from sports_aggregator.nfl.espn import sync_espn_context
        context = sync_espn_context(
            app.extensions["nfl_repository"], app.config["NFLVERSE_RAW_CACHE_PATH"],
            year, force=force,
        )
        click.echo(f"nfl_context: success (staff={context['staff']}, injuries={context['injuries']})")

    @app.cli.command("sync-nfl-context")
    @click.option("--year", type=int, default=current_nfl_season)
    @click.option("--force", is_flag=True, help="Bypass the cached ESPN injury document.")
    def sync_nfl_context(year: int, force: bool) -> None:
        """Refresh current NFL staff and injury context without heavier datasets."""
        from sports_aggregator.nfl.espn import sync_espn_context
        result = sync_espn_context(
            app.extensions["nfl_repository"], app.config["NFLVERSE_RAW_CACHE_PATH"],
            year, force=force,
        )
        click.echo(f"nfl_context: success (staff={result['staff']}, injuries={result['injuries']})")
        cache.clear()

    @app.cli.command("sync-nfl-pff")
    @click.option("--year", type=int, default=lambda: current_nfl_season() - 1)
    @click.option("--force-scan", is_flag=True, help="Re-fingerprint unchanged PFF exports.")
    def sync_nfl_pff(year: int, force_scan: bool) -> None:
        """Scan sibling-repo NFL PFF exports and persist normalized metrics."""
        from sports_aggregator.nfl.pff import NFLPFFService
        result = NFLPFFService(
            app.extensions["nfl_repository"], app.config["NFL_PFF_SOURCE_ROOT"],
        ).sync(year, force_scan=force_scan)
        click.echo("nfl_pff: " + json.dumps(result, sort_keys=True))
        cache.clear()

    @app.cli.command("sync-nfl-pbp-analytics")
    @click.option("--year", type=int, default=lambda: current_nfl_season() - 1)
    @click.option("--force", is_flag=True, help="Bypass the cached nflverse play-by-play asset.")
    def sync_nfl_pbp_analytics(year: int, force: bool) -> None:
        """Rebuild efficiency and QB pass-zone analytics from nflverse PBP."""
        from sports_aggregator.nfl.nflverse import NflverseClient
        client = NflverseClient(app.config["NFLVERSE_RAW_CACHE_PATH"])
        rows = client.load_pbp([year], force=force).to_dict("records")
        efficiency = app.extensions["nfl_repository"].replace_game_efficiency(year, rows)
        situational = app.extensions["nfl_repository"].replace_game_situational(year, rows)
        playcalling = app.extensions["nfl_repository"].replace_game_playcalling(year, rows)
        profiles = app.extensions["nfl_repository"].replace_qb_pass_profiles(year, rows)
        receivers = app.extensions["nfl_repository"].replace_receiver_pass_profiles(year, rows)
        click.echo(f"pbp_analytics: success (efficiency={efficiency}, situational={situational}, playcalling={playcalling}, pass_zones={profiles}, receiver_zones={receivers})")
        cache.clear()

    @app.cli.command("import-nfl-sources")
    @click.option("--input", "input_path", type=click.Path(exists=True, dir_okay=False),
                  default="data/nfl/NFL_Bluesky_Directory.xlsx", show_default=True)
    def import_nfl_sources(input_path: str) -> None:
        """Import the curated NFL Bluesky directory into the shared registry."""
        from sports_aggregator.nfl.source_directory import import_directory
        count = import_directory(app.extensions["source_registry"], input_path)
        click.echo(f"nfl_sources_imported={count}")

    @app.cli.command("sync-nfl-content")
    @click.option("--year", type=int, default=current_nfl_season)
    @click.option("--posts-per-source", type=click.IntRange(1, 20), default=4, show_default=True)
    @click.option("--max-sources", type=click.IntRange(1, 500), default=158, show_default=True)
    @click.option("--no-social", is_flag=True, help="Ingest league RSS without Bluesky feeds.")
    def sync_nfl_content(year: int, posts_per_source: int, max_sources: int,
                         no_social: bool) -> None:
        """Ingest NFL reporting and public Bluesky posts, then link entities."""
        from sports_aggregator.catalog import get_league
        from sports_aggregator.nfl.content import NFLContentRepository
        content = NFLContentRepository(app.extensions["nfl_repository"])
        league = get_league("nfl"); rss_started = datetime.now(timezone.utc)
        result = app.extensions["league_aggregation_service"].aggregate(league)
        article_count = content.ingest_articles(result.articles, year)
        rss_finished = datetime.now(timezone.utc)
        rss_errors = [{"source": error.source, "error": error.message} for error in result.errors]
        failed_sources = {error["source"] for error in rss_errors}
        articles_by_source = {feed.name: sum(article.source == feed.name for article in result.articles)
                              for feed in league.feeds}
        content.record_source_checks(({
            "platform": "rss", "source_key": feed.url, "display_name": feed.name,
            "success": feed.name not in failed_sources,
            "seen": articles_by_source[feed.name], "stored": articles_by_source[feed.name],
            "error": next((error["error"] for error in rss_errors
                           if error["source"] == feed.name), None),
        } for feed in league.feeds), checked_at=rss_finished)
        content.record_ingestion_run(
            "rss", year, rss_started, rss_finished, len(league.feeds),
            len(league.feeds) - len(rss_errors), len(result.articles), article_count, rss_errors,
        )
        click.echo(f"nfl_articles: success ({article_count})")
        if result.errors:
            click.echo(f"nfl_article_errors: {len(result.errors)}")
        if not no_social:
            sources = app.extensions["source_registry"].list_league_sources(
                "nfl", limit=max_sources,
            )
            social = content.ingest_bluesky(
                sources, year, posts_per_source=posts_per_source,
            )
            click.echo(f"nfl_bluesky: stored ({social['stored']}) from {social['sources']} sources")
            if social["errors"]:
                click.echo(f"nfl_bluesky_errors: {len(social['errors'])}")
                for error in social["errors"][:20]:
                    click.echo(f"  {error['handle']}: {error['error']}")
        rescored = content.rescore_all()
        click.echo(f"nfl_content_scored: {rescored}")
        click.echo("nfl_content_links: " + json.dumps(content.counts(), sort_keys=True))
        cache.clear()

    if app.config["REGISTER_LEGACY_DASHBOARDS"]:
        from blueprints.bengals import bengals
        from reds.reds import reds
        app.register_blueprint(reds, url_prefix="/reds")
        app.register_blueprint(bengals, url_prefix="/bengals")
    if app.config["NFL_AUTO_SEED"]:
        from sports_aggregator.nfl.production_seed import maybe_launch
        maybe_launch(database_path=app.config["NFL_DATABASE_PATH"])
    return app


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1").strip().lower() not in {"0", "false", "no"}
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    create_app().run(debug=debug, host=host, port=port)
