"""Command-line research entry point for NFL Football Lab."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sports_aggregator.nfl.drive_projection import report
from sports_aggregator.nfl.drive_feature_ablation import report as drive_ablation_report
from sports_aggregator.nfl.plays_projection import report as plays_report
from sports_aggregator.nfl.drive_recency_ablation import report as drive_recency_report
from sports_aggregator.nfl.pass_rate_projection import report as pass_rate_report
from sports_aggregator.nfl.efficiency_projection import report as efficiency_report
from sports_aggregator.nfl.state_recency_ablation import report as state_recency_report
from sports_aggregator.nfl.modeling_readiness import audit as readiness_audit
from sports_aggregator.nfl.qb_quality_projection import report as qb_quality_report
from sports_aggregator.nfl.pressure_ol_readiness import report as pressure_ol_readiness_report
from sports_aggregator.nfl.scoring_bridge import report as scoring_bridge_report
from sports_aggregator.nfl.score_calibration import report as score_calibration_report
from sports_aggregator.nfl.margin_strength_ablation import report as margin_strength_report
from sports_aggregator.nfl.availability_ablation import report as availability_report
from sports_aggregator.nfl.context_ablation import report as context_report
from sports_aggregator.nfl.distribution_calibration import report as distribution_report
from sports_aggregator.nfl.market_gap import report as market_gap_report
from sports_aggregator.nfl.travel_ablation import report as travel_report
from sports_aggregator.nfl.nonlinear_ablation import report as nonlinear_report
from sports_aggregator.nfl.weather_total_ablation import report as weather_total_report
from sports_aggregator.nfl.qb_player_ablation import report as qb_player_report
from sports_aggregator.nfl.uncertainty_calibration import report as uncertainty_report
from sports_aggregator.nfl.market_disagreement import report as market_disagreement_report
from sports_aggregator.nfl.market_anchor_leverage import report as market_anchor_report
from sports_aggregator.nfl.market_leverage_ablation import report as market_leverage_ablation_report
from sports_aggregator.nfl.live_projection import report as live_projection_report
from sports_aggregator.nfl.perception_challenger import report as perception_report
from sports_aggregator.nfl.engine_picks import build_dashboard as picks_dashboard
from sports_aggregator.nfl.forecast_ledger import freeze_dashboard, grading_report
from sports_aggregator.nfl.repository import NFLRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="instance/nfl.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)

    live_projection = sub.add_parser("live-forecast")
    live_projection.add_argument("--season", type=int, required=True)
    live_projection.add_argument("--week", type=int, required=True)

    perception = sub.add_parser("perception-challenger")
    perception.add_argument("--from-year", type=int, default=2010)
    perception.add_argument("--to-year", type=int, default=2026)

    freeze = sub.add_parser("freeze-live-forecast")
    freeze.add_argument("--season", type=int, required=True)
    freeze.add_argument("--week", type=int, required=True)

    grades = sub.add_parser("forecast-grades")
    grades.add_argument("--season", type=int)

    market_leverage = sub.add_parser("market-leverage-ablation")
    market_leverage.add_argument("--from-year", type=int, default=2010)
    market_leverage.add_argument("--to-year", type=int, default=2025)

    market_anchor = sub.add_parser("market-anchor")
    market_anchor.add_argument("--from-year", type=int, default=2010)
    market_anchor.add_argument("--to-year", type=int, default=2025)

    market_disagreement = sub.add_parser("market-disagreement")
    market_disagreement.add_argument("--from-year", type=int, default=2010)
    market_disagreement.add_argument("--to-year", type=int, default=2025)

    uncertainty = sub.add_parser("uncertainty-backtest")
    uncertainty.add_argument("--from-year", type=int, default=2010)
    uncertainty.add_argument("--to-year", type=int, default=2025)

    margin_strength = sub.add_parser("margin-strength")
    margin_strength.add_argument("--from-year", type=int, default=2010)
    margin_strength.add_argument("--to-year", type=int, default=2025)

    backfill = sub.add_parser("backfill-injuries")
    backfill.add_argument("--from-year", type=int, default=2009)
    backfill.add_argument("--to-year", type=int, default=2025)
    backfill.add_argument("--snaps-to-year", type=int, default=2024,
                          help="snap counts are only backfilled through this season (2025+ is live-synced)")
    backfill.add_argument("--force", action="store_true")

    weather_backfill = sub.add_parser("weather-backfill")
    weather_backfill.add_argument("--from-year", type=int, default=2013)
    weather_backfill.add_argument("--to-year", type=int, default=2025)
    weather_backfill.add_argument("--no-forecasts", action="store_true")
    weather_backfill.add_argument("--check-only", action="store_true",
                                  help="report unmapped stadiums and the games to fetch, then stop")

    weather_total = sub.add_parser("weather-total")
    weather_total.add_argument("--from-year", type=int, default=2013)
    weather_total.add_argument("--to-year", type=int, default=2025)

    travel = sub.add_parser("travel")
    travel.add_argument("--from-year", type=int, default=2013)
    travel.add_argument("--to-year", type=int, default=2025)

    market_gap = sub.add_parser("market-gap")
    market_gap.add_argument("--from-year", type=int, default=2013)
    market_gap.add_argument("--to-year", type=int, default=2025)

    nonlinear = sub.add_parser("nonlinear")
    nonlinear.add_argument("--from-year", type=int, default=2013)
    nonlinear.add_argument("--to-year", type=int, default=2025)

    distribution = sub.add_parser("distribution")
    distribution.add_argument("--from-year", type=int, default=2013)
    distribution.add_argument("--to-year", type=int, default=2025)

    context = sub.add_parser("context")
    context.add_argument("--from-year", type=int, default=2010)
    context.add_argument("--to-year", type=int, default=2025)

    availability = sub.add_parser("availability")
    availability.add_argument("--from-year", type=int, default=2013)
    availability.add_argument("--to-year", type=int, default=2025)

    qb_player = sub.add_parser("qb-player")
    qb_player.add_argument("--from-year", type=int, default=2010)
    qb_player.add_argument("--to-year", type=int, default=2025)
    qb_player.add_argument("--decay", type=float, default=0.6)

    score_cal = sub.add_parser("score-calibration")
    score_cal.add_argument("--from-year", type=int, default=2010)
    score_cal.add_argument("--to-year", type=int, default=2025)

    scoring = sub.add_parser("scoring-backtest")
    scoring.add_argument("--from-year", type=int, default=2010)
    scoring.add_argument("--to-year", type=int, default=2025)

    pressure_ready = sub.add_parser("pressure-ol-readiness")
    pressure_ready.add_argument("--from-year", type=int, default=2010)
    pressure_ready.add_argument("--to-year", type=int, default=2026)

    qb_quality = sub.add_parser("qb-quality-backtest")
    qb_quality.add_argument("--from-year", type=int, default=2010)
    qb_quality.add_argument("--to-year", type=int, default=2025)

    readiness = sub.add_parser("readiness")
    readiness.add_argument("--from-year", type=int, default=2010)
    readiness.add_argument("--to-year", type=int, default=2026)

    drive_ablation = sub.add_parser("drive-ablation")
    drive_ablation.add_argument("--from-year", type=int, default=2010)
    drive_ablation.add_argument("--to-year", type=int, default=2025)

    drive_recency = sub.add_parser("drive-recency")
    drive_recency.add_argument("--from-year", type=int, default=2010)
    drive_recency.add_argument("--to-year", type=int, default=2025)

    state_recency = sub.add_parser("state-recency")
    state_recency.add_argument("--from-year", type=int, default=2010)
    state_recency.add_argument("--to-year", type=int, default=2025)

    efficiency = sub.add_parser("efficiency-backtest")
    efficiency.add_argument("--from-year", type=int, default=2010)
    efficiency.add_argument("--to-year", type=int, default=2025)

    pass_rate = sub.add_parser("pass-rate-backtest")
    pass_rate.add_argument("--from-year", type=int, default=2010)
    pass_rate.add_argument("--to-year", type=int, default=2025)

    plays = sub.add_parser("plays-backtest")
    plays.add_argument("--from-year", type=int, default=2010)
    plays.add_argument("--to-year", type=int, default=2025)

    drives = sub.add_parser("drive-backtest")
    drives.add_argument("--from-year", type=int, default=2016)
    drives.add_argument("--to-year", type=int, default=2025)

    args = parser.parse_args(argv)
    repository = NFLRepository(Path(args.db))

    if args.command == "live-forecast":
        payload = live_projection_report(
            repository,
            season=int(args.season),
            week=int(args.week),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "perception-challenger":
        payload = perception_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "freeze-live-forecast":
        dashboard = picks_dashboard(repository, int(args.season), int(args.week))
        stored = freeze_dashboard(repository, dashboard)
        print(json.dumps({
            "season": int(args.season), "week": int(args.week),
            "games": len(dashboard.get("games", [])), "forecasts_stored": stored,
            "model_version": dashboard.get("version"),
        }, indent=2, sort_keys=True))
        return 0

    if args.command == "forecast-grades":
        print(json.dumps(grading_report(repository, season=args.season),
                         indent=2, sort_keys=True))
        return 0

    if args.command == "market-leverage-ablation":
        payload = market_leverage_ablation_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "market-anchor":
        payload = market_anchor_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "market-disagreement":
        payload = market_disagreement_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "uncertainty-backtest":
        payload = uncertainty_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "margin-strength":
        payload = margin_strength_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "backfill-injuries":
        from sports_aggregator.nfl.injury_history import backfill
        from sports_aggregator.nfl.refresh_cli import _nflverse_client
        payload = backfill(repository, _nflverse_client(), args.from_year, args.to_year,
                           snaps_end_season=args.snaps_to_year, force=args.force)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "weather-backfill":
        from sports_aggregator.nfl import weather_history as wh
        missing = wh.unmapped_stadiums(repository, args.from_year, args.to_year)
        if args.check_only:
            games = wh._games(repository, args.from_year, args.to_year)
            print(json.dumps({"unmapped_stadiums": missing, "games_to_fetch": len(games),
                              "venue_seasons": len({(g["lat"], g["lon"], g["season"]) for g in games})},
                             indent=2, sort_keys=True))
            return 0
        payload = wh.backfill(repository, wh.Fetcher(), args.from_year, args.to_year,
                              forecasts=not args.no_forecasts)
        print(json.dumps({**payload, "unmapped_stadiums": missing}, indent=2, sort_keys=True))
        return 0

    if args.command == "weather-total":
        payload = weather_total_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "travel":
        payload = travel_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "market-gap":
        payload = market_gap_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "nonlinear":
        payload = nonlinear_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "distribution":
        payload = distribution_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "context":
        payload = context_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "availability":
        payload = availability_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "qb-player":
        payload = qb_player_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
            decay=float(args.decay),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "score-calibration":
        payload = score_calibration_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "scoring-backtest":
        payload = scoring_bridge_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "pressure-ol-readiness":
        payload = pressure_ol_readiness_report(
            repository,
            from_season=int(args.from_year),
            to_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "qb-quality-backtest":
        payload = qb_quality_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "readiness":
        payload = readiness_audit(
            repository,
            from_season=int(args.from_year),
            to_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "drive-ablation":
        payload = drive_ablation_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "drive-recency":
        payload = drive_recency_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "state-recency":
        payload = state_recency_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "efficiency-backtest":
        payload = efficiency_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "pass-rate-backtest":
        payload = pass_rate_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "plays-backtest":
        payload = plays_report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if args.command == "drive-backtest":
        payload = report(
            repository,
            start_season=int(args.from_year),
            end_season=int(args.to_year),
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
