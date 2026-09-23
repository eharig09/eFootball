"""CLI for event-aligned ep-v2 fitting, scoring, and game audit."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.expected_points_event import (
    MODEL_VERSION, audit_game, fit_model, score_plays, validate_model,
)
from sports_aggregator.cfb.repository import CFBRepository


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fit/score event-aligned ep-v2")
    p.add_argument("command", choices=("fit", "score", "validate", "audit"))
    p.add_argument("--from-year", type=int, default=None)
    p.add_argument("--to-year", type=int, default=None)
    p.add_argument("--game-id", type=int, default=None)
    p.add_argument("--model-version", default=MODEL_VERSION)
    p.add_argument("--database", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "fit":
        output = fit_model(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
            model_version=args.model_version,
        )
    elif args.command == "score":
        output = score_plays(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
            model_version=args.model_version,
        )
    elif args.command == "validate":
        output = validate_model(
            repository,
            from_season=args.from_year,
            to_season=args.to_year,
            model_version=args.model_version,
        )
    else:
        if args.game_id is None:
            raise SystemExit("audit requires --game-id")
        output = {
            "game_id": args.game_id,
            "model_version": args.model_version,
            "events": audit_game(repository, args.game_id, model_version=args.model_version),
        }

    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
