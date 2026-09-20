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
from sports_aggregator.nfl.repository import NFLRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="instance/nfl.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)

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
