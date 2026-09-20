"""CLI for narrative-state and Line-Elo research."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb import narrative_shapes
from sports_aggregator.cfb import narrative_shapes_v2
from sports_aggregator.cfb import narrative_composite
from sports_aggregator.cfb import extreme_tail_composite
from sports_aggregator.cfb import composite_input_repair
from sports_aggregator.cfb import internal_power_lenses
from sports_aggregator.cfb import conditional_convergence
from sports_aggregator.cfb import convergence_validation
from sports_aggregator.cfb import convergence_robustness
from sports_aggregator.cfb import convergence_action_policy
from sports_aggregator.cfb import market_ats_totals
from sports_aggregator.cfb import totals_convergence
from sports_aggregator.cfb import totals_market_movement
from sports_aggregator.cfb import totals_divergence_matrix


def main() -> None:
    parser = argparse.ArgumentParser(description="CFB narrative shape / Line Elo research")
    parser.add_argument("--db", default=None, help="Optional SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--from-year", type=int, default=2022)
    build.add_argument("--to-year", type=int, default=2025)
    build.add_argument("--home-field-points", type=float, default=2.5)
    build.add_argument("--line-learning-rate", type=float, default=0.35)

    report = sub.add_parser("report")
    report.add_argument("--year", type=int, default=2025)
    report.add_argument("--min-train-rows", type=int, default=30)
    report.add_argument("--min-test-rows", type=int, default=12)

    report_v2 = sub.add_parser("report-v2")
    report_v2.add_argument("--year", type=int, default=2025)
    report_v2.add_argument("--min-train-rows", type=int, default=30)
    report_v2.add_argument("--min-test-rows", type=int, default=12)

    stability = sub.add_parser("stability")
    stability.add_argument("--year", type=int, default=2025)
    stability.add_argument("--min-year-rows", type=int, default=12)

    composite = sub.add_parser("composite")
    composite.add_argument("--year", type=int, default=2025)

    extreme_tail = sub.add_parser("extreme-tail")
    extreme_tail.add_argument("--year", type=int, default=2025)

    input_audit = sub.add_parser("input-audit")
    input_audit.add_argument("--from-year", type=int, default=2022)
    input_audit.add_argument("--to-year", type=int, default=2025)

    input_repair = sub.add_parser("input-repair")
    input_repair.add_argument("--from-year", type=int, default=2022)
    input_repair.add_argument("--to-year", type=int, default=2025)

    family = sub.add_parser("family-composite")
    family.add_argument("--year", type=int, default=2025)

    internal_power = sub.add_parser("internal-power")
    internal_power.add_argument("--year", type=int, default=2025)

    convergence = sub.add_parser("conditional-convergence")
    convergence.add_argument("--year", type=int, default=2025)

    convergence_validation_parser = sub.add_parser("convergence-validation")
    convergence_validation_parser.add_argument("--year", type=int, default=2025)

    robustness = sub.add_parser("convergence-robustness")
    robustness.add_argument("--year", type=int, default=2025)
    robustness.add_argument(
        "--output-dir",
        default="research_outputs",
        help="Directory for full JSON/CSV artifacts; console output stays compact",
    )

    action_policy = sub.add_parser("convergence-action-policy")
    action_policy.add_argument("--year", type=int, default=2025)
    action_policy.add_argument(
        "--output-dir",
        default="research_outputs",
        help="Directory for full JSON/CSV artifacts; console output stays compact",
    )

    market_totals = sub.add_parser("market-ats-totals")
    market_totals.add_argument("--year", type=int, default=2025)
    market_totals.add_argument(
        "--output-dir",
        default="research_outputs",
        help="Directory for ATS/totals research artifacts",
    )

    totals_conv = sub.add_parser("totals-convergence")
    totals_conv.add_argument("--year", type=int, default=2025)
    totals_conv.add_argument(
        "--output-dir",
        default="research_outputs",
        help="Directory for totals convergence artifacts",
    )

    totals_movement = sub.add_parser("totals-market-movement")
    totals_movement.add_argument("--year", type=int, default=2025)
    totals_movement.add_argument(
        "--output-dir",
        default="research_outputs",
        help="Directory for totals market-movement artifacts",
    )

    totals_divergence = sub.add_parser("totals-divergence-matrix")
    totals_divergence.add_argument("--year", type=int, default=2025)
    totals_divergence.add_argument(
        "--output-dir",
        default="research_outputs",
        help="Directory for totals divergence artifacts",
    )

    load_dotenv()
    args = parser.parse_args()
    repository = CFBRepository(
        args.db or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "build":
        payload = narrative_shapes.build(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
            home_field_points=args.home_field_points,
            line_learning_rate=args.line_learning_rate,
        )
    elif args.command == "report-v2":
        payload = narrative_shapes_v2.report(
            repository,
            test_season=args.year,
            min_train_rows=args.min_train_rows,
            min_test_rows=args.min_test_rows,
        )
    elif args.command == "stability":
        payload = narrative_composite.stability_report(
            repository,
            test_season=args.year,
            min_year_rows=args.min_year_rows,
        )
    elif args.command == "composite":
        payload = narrative_composite.composite_report(
            repository,
            test_season=args.year,
        )
    elif args.command == "extreme-tail":
        payload = extreme_tail_composite.report(
            repository,
            test_season=args.year,
        )
    elif args.command == "input-audit":
        payload = composite_input_repair.source_audit(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
        )
    elif args.command == "input-repair":
        payload = composite_input_repair.rebuild_from_sources(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
        )
    elif args.command == "family-composite":
        payload = composite_input_repair.family_report(
            repository,
            test_season=args.year,
        )
    elif args.command == "internal-power":
        payload = internal_power_lenses.report(
            repository,
            test_season=args.year,
        )
    elif args.command == "conditional-convergence":
        payload = conditional_convergence.report(
            repository,
            test_season=args.year,
        )
    elif args.command == "convergence-validation":
        payload = convergence_validation.report(
            repository,
            test_season=args.year,
        )
    elif args.command == "convergence-robustness":
        payload = convergence_robustness.report(
            repository,
            test_season=args.year,
        )
        paths = convergence_robustness.export_report(
            payload,
            args.output_dir,
        )
        payload = convergence_robustness.compact_console_summary(payload, paths)
    elif args.command == "convergence-action-policy":
        payload = convergence_action_policy.report(
            repository,
            test_season=args.year,
        )
        paths = convergence_action_policy.export_report(
            payload,
            args.output_dir,
        )
        payload = convergence_action_policy.compact_console_summary(payload, paths)
    elif args.command == "market-ats-totals":
        payload = market_ats_totals.report(
            repository,
            test_season=args.year,
        )
        paths = market_ats_totals.export_report(
            payload,
            args.output_dir,
        )
        payload = market_ats_totals.compact_console_summary(payload, paths)
    elif args.command == "totals-convergence":
        payload = totals_convergence.report(
            repository,
            test_season=args.year,
        )
        paths = totals_convergence.export_report(
            payload,
            args.output_dir,
        )
        payload = totals_convergence.compact_console_summary(payload, paths)
    elif args.command == "totals-market-movement":
        payload = totals_market_movement.report(
            repository,
            test_season=args.year,
        )
        paths = totals_market_movement.export_report(
            payload,
            args.output_dir,
        )
        payload = totals_market_movement.compact_console_summary(payload, paths)
    elif args.command == "totals-divergence-matrix":
        payload = totals_divergence_matrix.report(
            repository,
            test_season=args.year,
        )
        paths = totals_divergence_matrix.export_report(
            payload,
            args.output_dir,
        )
        payload = totals_divergence_matrix.compact_console_summary(payload, paths)
    else:
        payload = narrative_shapes.report(
            repository,
            test_season=args.year,
            min_train_rows=args.min_train_rows,
            min_test_rows=args.min_test_rows,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
