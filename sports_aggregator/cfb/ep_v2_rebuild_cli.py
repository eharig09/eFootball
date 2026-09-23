"""One-shot migration/rebuild for the canonical event-aligned ep-v2 report stack."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.expected_points_event import MIN_CELL, fit_model, score_plays
from sports_aggregator.cfb.qb_air_yards import build as build_qb_air_yards
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.team_game_advanced import build as build_team_game_advanced
from sports_aggregator.cfb.team_game_tendencies import build as build_tendencies


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fit ep-v2, rescore EPA, and rebuild report aggregates")
    p.add_argument("--fit-from-year", type=int, default=2015)
    p.add_argument("--fit-to-year", type=int, default=2025)
    p.add_argument("--score-from-year", type=int, default=2015)
    p.add_argument("--score-to-year", type=int, default=2026)
    p.add_argument("--detail-from-year", type=int, default=2025)
    p.add_argument("--min-cell", type=int, default=MIN_CELL)
    p.add_argument("--database", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    output: dict[str, object] = {}
    output["fit"] = fit_model(
        repository,
        from_season=args.fit_from_year,
        to_season=args.fit_to_year,
        model_version="ep-v2",
        min_cell=args.min_cell,
    )
    output["score"] = score_plays(
        repository,
        from_season=args.score_from_year,
        to_season=args.score_to_year,
        model_version="ep-v2",
    )
    output["team_game_advanced"] = build_team_game_advanced(
        repository,
        from_season=args.score_from_year,
        to_season=args.score_to_year,
        model_version="ep-v2",
    )
    output["team_game_tendencies"] = build_tendencies(
        repository,
        from_season=args.detail_from_year,
        to_season=args.score_to_year,
        model_version="ep-v2",
    )
    output["qb_air_yards"] = build_qb_air_yards(
        repository,
        from_season=args.detail_from_year,
        to_season=args.score_to_year,
        model_version="ep-v2",
    )

    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
