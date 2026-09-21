"""CLI for the CFB drive-count model: python -m sports_aggregator.cfb.xdrives_cli ..."""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.xdrives import (
    ADVANCED_FEATURES_LIVE,
    ADVANCED_MODEL_L2,
    ADVANCED_MODEL_VERSION_LIVE,
    build_dataset,
    fit_advanced_model,
)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="CFB drive-count model")
    parser.add_argument("command", choices=("refresh",))
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))

    if args.command == "refresh":
        dataset_result = build_dataset(repository)
        # No season bounds: production wants the best model trained on every
        # game on record, not a held-out slice -- evaluate_advanced_model()
        # is where the "does this generalize" question already got answered
        # (see xdrives.py's ADVANCED_FEATURES_LIVE comment), not this step.
        fit_result = fit_advanced_model(
            repository, feature_keys=ADVANCED_FEATURES_LIVE,
            model_version=ADVANCED_MODEL_VERSION_LIVE, l2=ADVANCED_MODEL_L2,
        )
        print(json.dumps({"dataset": dataset_result, "model": {
            "model_version": fit_result["model_version"],
            "training_rows": fit_result["training_rows"],
            "coefficients_fit": fit_result["coefficients"] is not None,
        }}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
