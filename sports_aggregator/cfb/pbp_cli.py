"""CLI for play-by-play backfill and in-house model development."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
import sqlite3
import time

from dotenv import load_dotenv

from sports_aggregator.cfb.cfbd import (
    CFBDClient, CFBDConfigurationError, FINISHED_WEEK_TTL, LIVE_WEEK_TTL,
)
from sports_aggregator.cfb.epa_validation import validate_epa
from sports_aggregator.cfb.expected_points import fit_model as fit_edp, score_plays as score_edp
from sports_aggregator.cfb.expected_points_event import fit_model as fit_ep_v2
from sports_aggregator.cfb.expected_points_event import score_plays as score_epa_v2
from sports_aggregator.cfb.expected_points_v2 import fit_model as fit_ep_v1
from sports_aggregator.cfb.expected_points_v2 import score_plays as score_epa_v1
from sports_aggregator.cfb.expected_points_v2 import validate_model as validate_ep
from sports_aggregator.cfb.model_validation import validate_edp, validate_wp
from sports_aggregator.cfb.pace import game_pace_summary
from sports_aggregator.cfb.play_by_play import replace_week_plays, derive_week
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.team_game_advanced import build as build_team_game_advanced
from sports_aggregator.cfb.team_game_pace import build as build_team_pace
from sports_aggregator.cfb.team_game_scoring import build as build_team_scoring
from sports_aggregator.cfb.team_game_special_teams import build as build_team_special_teams
from sports_aggregator.cfb.team_game_drive_outcomes import build as build_team_drive_outcomes
from sports_aggregator.cfb.win_probability import fit_model as fit_wp, score_plays as score_wp
from sports_aggregator.cfb.win_probability_v2 import fit_model as fit_wp_v2, score_plays as score_wp_v2
from sports_aggregator.cfb.wp_calibration import fit_calibration as fit_wp_calibration
from sports_aggregator.cfb.wp_calibration import score_calibrated as score_wp_calibrated
from sports_aggregator.cfb.xdrives import build_dataset as build_xdrives_dataset
from sports_aggregator.cfb.xdrives import evaluate_advanced_model as evaluate_xdrives_advanced
from sports_aggregator.cfb.xdrives import evaluate_baselines as evaluate_xdrives_baselines
from sports_aggregator.cfb.xdrives import fit_advanced_model as fit_xdrives_advanced
from sports_aggregator.cfb.xplays import build_dataset as build_xplays_dataset
from sports_aggregator.cfb.xplays import evaluate_baselines as evaluate_xplays_baselines
from sports_aggregator.cfb.xplays import evaluate_expected_plays
from sports_aggregator.cfb.xvolume import build_dataset as build_xvolume_dataset
from sports_aggregator.cfb.xvolume import evaluate_baselines as evaluate_xvolume_baselines
from sports_aggregator.cfb.xvolume import evaluate_expected_volume
from sports_aggregator.cfb.xyards import build_dataset as build_xyards_dataset
from sports_aggregator.cfb.xyards import evaluate_baselines as evaluate_xyards_baselines
from sports_aggregator.cfb.xyards import evaluate_expected_yardage
from sports_aggregator.cfb.xturnovers import build_dataset as build_xturnovers_dataset
from sports_aggregator.cfb.xturnovers import evaluate_baselines as evaluate_xturnovers_baselines
from sports_aggregator.cfb.xredzone import build_dataset as build_xredzone_dataset
from sports_aggregator.cfb.xredzone import evaluate_baselines as evaluate_xredzone_baselines
from sports_aggregator.cfb.xfieldposition import build_dataset as build_xfieldposition_dataset
from sports_aggregator.cfb.xfieldposition import evaluate_baselines as evaluate_xfieldposition_baselines
from sports_aggregator.cfb.xpoints import build_dataset as build_xpoints_dataset
from sports_aggregator.cfb.xpoints import evaluate as evaluate_xpoints
from sports_aggregator.cfb.xpoints import fit_model as fit_xpoints


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CFB play-by-play analytics")
    p.add_argument("command", choices=(
        "backfill", "derive",
        "fit-edp", "score-edp", "validate-edp",
        "fit-ep", "score-epa", "validate-ep", "validate-epa", "build-team-advanced",
        "fit-wp", "score-wp", "fit-wp-v2", "score-wp-v2", "validate-wp",
        "fit-wp-calibration", "score-wp-calibrated", "rebuild-values", "pace",
        "build-team-pace", "build-xdrives-dataset", "evaluate-xdrives-baselines",
        "fit-xdrives-advanced", "evaluate-xdrives-advanced",
        "build-xplays-dataset", "evaluate-xplays-baselines", "evaluate-expected-plays",
        "build-xvolume-dataset", "evaluate-xvolume-baselines", "evaluate-expected-volume",
        "build-xyards-dataset", "evaluate-xyards-baselines", "evaluate-expected-yardage",
        "build-team-scoring", "build-xturnovers-dataset", "evaluate-xturnovers-baselines",
        "build-xredzone-dataset", "evaluate-xredzone-baselines",
        "build-team-special-teams", "build-xfieldposition-dataset",
        "evaluate-xfieldposition-baselines", "build-team-drive-outcomes",
        "build-xpoints-dataset", "evaluate-xpoints", "fit-xpoints"))
    p.add_argument("--year", type=int, default=datetime.now().year)
    p.add_argument("--from-year", type=int, default=None)
    p.add_argument("--to-year", type=int, default=None)
    p.add_argument("--week", type=int, default=None)
    p.add_argument("--game-id", type=int, default=None)
    p.add_argument("--model-version", default=None,
                   help="Optional output/model version. Defaults by model family.")
    p.add_argument("--source-model-version", default=None,
                   help="Source WP version for calibration commands.")
    p.add_argument("--epochs", type=int, default=8,
                   help="Newton/IRLS iterations for wp-v2 logistic model.")
    p.add_argument("--learning-rate", type=float, default=1.0,
                   help="Damping multiplier for wp-v2 Newton steps (0-1).")
    p.add_argument("--l2", type=float, default=0.0005,
                   help="L2 regularization for wp-v2.")
    p.add_argument("--xdrives-l2", type=float, default=None,
                   help="Ridge penalty for fit-xdrives-advanced/evaluate-xdrives-advanced "
                        "(defaults to xdrives.ADVANCED_MODEL_L2).")
    p.add_argument("--train-from-year", type=int, default=None,
                   help="evaluate-xdrives-advanced: first training season.")
    p.add_argument("--train-to-year", type=int, default=None,
                   help="evaluate-xdrives-advanced: last training season.")
    p.add_argument("--test-from-year", type=int, default=None,
                   help="evaluate-xdrives-advanced: first held-out test season.")
    p.add_argument("--test-to-year", type=int, default=None,
                   help="evaluate-xdrives-advanced: last held-out test season.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--database", default=None)
    return p


def _years(args) -> tuple[int, int]:
    first = args.from_year or args.year
    last = args.to_year or args.year
    if first > last:
        raise ValueError("--from-year must not be after --to-year")
    return first, last


#: `expected_points_v2.py` implements ep-v1 and `expected_points_event.py`
#: implements ep-v2, which supersedes it -- the file names record the order the
#: files were written, not the model versions they produce. Every consumer of
#: EPA reads ep-v2: team_game_advanced, player_game_epa, qb_air_yards,
#: team_game_tendencies, wp_turning_points and the postgame report.
_EP_MODULES = {"ep-v1": (fit_ep_v1, score_epa_v1),
               "ep-v2": (fit_ep_v2, score_epa_v2)}


def _model_version(args, family: str) -> str:
    # "ep" defaults to ep-v2. It defaulted to the superseded ep-v1, so the
    # obvious sequence -- fit-ep, score-epa, build-team-advanced -- wrote rows
    # that were correct, complete, and invisible to every page that reads them.
    defaults = {"edp": "edp-v1", "ep": "ep-v2", "wp": "wp-v1", "wp-v2": "wp-v2"}
    return str(args.model_version or defaults[family])


def _ep_functions(version: str):
    """Fit/score for the requested EPA version, so a tag names its own model."""
    try:
        return _EP_MODULES[version]
    except KeyError:
        raise SystemExit(
            f"unknown EPA model version {version!r}; expected one of "
            f"{', '.join(sorted(_EP_MODULES))}") from None


def _require_scored_plays(repository, version: str) -> bool:
    """Whether there is anything to build from, and whether that is a problem.

    Building against an unscored version produced an empty table and a report
    saying the metrics were "not available for this game yet", which reads as
    missing data rather than as a step nobody ran. So a version that has no
    plays while another version does is an error worth stopping for -- that is
    the wrong version, which is exactly the trap this guard exists for.

    An empty table is different. Nothing scored at all means the pipeline has
    not reached here yet, which is the normal state of a fresh database and of
    a preseason. As a scheduled step that is a no-op, not a failure, and
    reporting it as one put `team-advanced` in the degraded list of a refresh
    that was working.
    """
    with closing(repository._connect()) as connection:
        try:
            scored = int(connection.execute(
                "SELECT COUNT(*) FROM cfb_play_epa WHERE model_version=?",
                (version,)).fetchone()[0])
            other = [row[0] for row in connection.execute(
                "SELECT DISTINCT model_version FROM cfb_play_epa WHERE model_version<>?",
                (version,))]
        except sqlite3.Error:
            scored, other = 0, []
    if scored:
        return True
    if other:
        raise SystemExit(
            f"no plays scored for model version {version!r}, but {', '.join(sorted(other))}"
            f" {'is' if len(other) == 1 else 'are'} scored. Either build that version or run"
            f" `score-epa --model-version {version}` first (ep-v2 is also scored by"
            f" `python -m sports_aggregator.cfb.expected_points_event_cli score`).")
    print(f"no plays scored yet for {version}; nothing to build")
    return False


def _week_ready(repository: CFBRepository, year: int, week: int) -> tuple[bool, int, int]:
    """Whether a week's play-by-play is complete and derived.

    "Has some plays" is not "done": a Thursday game whose PBP lands a day
    after that week's Saturday slate leaves the week looking finished while a
    completed game still has no plays. The third value is that gap, so the
    caller re-fetches instead of trusting a year-long cache.
    """
    try:
        with closing(repository._connect()) as connection:
            raw = int(connection.execute(
                "SELECT COUNT(*) FROM cfb_plays WHERE season=? AND week=?", (int(year), int(week))
            ).fetchone()[0])
            if raw <= 0:
                return False, 0, 0
            derived = int(connection.execute("""
                SELECT COUNT(*) FROM cfb_play_metrics m
                JOIN cfb_plays p ON p.play_id=m.play_id
                WHERE p.season=? AND p.week=? AND m.metric_version='pbp-v1'
            """, (int(year), int(week))).fetchone()[0])
            games_without_plays = int(connection.execute("""
                SELECT COUNT(*) FROM games g
                WHERE g.season=? AND g.week=? AND g.completed=1
                  AND NOT EXISTS (SELECT 1 FROM cfb_plays p WHERE p.game_id=g.game_id)
            """, (int(year), int(week))).fetchone()[0])
        return (derived == raw and games_without_plays == 0), raw, games_without_plays
    except sqlite3.Error:
        return False, 0, 0


def _replace_with_lock_retry(repository: CFBRepository, raw, *, year: int, week: int) -> int:
    delays = (15, 30, 60)
    for attempt in range(len(delays) + 1):
        try:
            return replace_week_plays(repository, raw, season=year, week=week)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).casefold() or attempt >= len(delays):
                raise
            delay = delays[attempt]
            print(f"{year} week {week}: database locked; retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def main(argv: list[str] | None = None, *, client=None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    first, last = _years(args)

    if args.command == "pace":
        if not args.game_id:
            raise SystemExit("pace requires --game-id")
        print(json.dumps(game_pace_summary(repository, args.game_id), indent=2, default=str))
        return 0

    if args.command == "derive":
        total = {"plays": 0, "drives": 0}
        for year in range(first, last + 1):
            weeks = [args.week] if args.week is not None else repository.completed_weeks(year)
            for week in weeks:
                result = derive_week(repository, season=year, week=int(week))
                print(f"{year} week {week}: plays={result['plays']} drives={result['drives']}")
                total["plays"] += result["plays"]
                total["drives"] += result["drives"]
        print(json.dumps(total))
        return 0

    if args.command == "fit-edp":
        version = _model_version(args, "edp")
        print(json.dumps(fit_edp(repository, from_season=first, to_season=last,
                                 model_version=version), indent=2))
        return 0
    if args.command == "score-edp":
        version = _model_version(args, "edp")
        print(json.dumps(score_edp(repository, from_season=first, to_season=last,
                                   model_version=version), indent=2))
        return 0
    if args.command == "validate-edp":
        version = _model_version(args, "edp")
        print(json.dumps(validate_edp(repository, from_season=first, to_season=last,
                                     model_version=version), indent=2))
        return 0

    if args.command == "fit-ep":
        version = _model_version(args, "ep")
        fit, _score = _ep_functions(version)
        print(json.dumps(fit(repository, from_season=first, to_season=last,
                             model_version=version), indent=2))
        return 0
    if args.command == "score-epa":
        version = _model_version(args, "ep")
        _fit, score = _ep_functions(version)
        print(json.dumps(score(repository, from_season=first, to_season=last,
                               model_version=version), indent=2))
        return 0
    if args.command == "validate-ep":
        version = _model_version(args, "ep")
        print(json.dumps(validate_ep(repository, from_season=first, to_season=last,
                                    model_version=version), indent=2))
        return 0
    if args.command == "validate-epa":
        version = _model_version(args, "ep")
        print(json.dumps(validate_epa(repository, from_season=first, to_season=last,
                                     model_version=version), indent=2))
        return 0
    if args.command == "build-team-advanced":
        version = _model_version(args, "ep")
        if not _require_scored_plays(repository, version):
            return 0
        print(json.dumps(build_team_game_advanced(
            repository, from_season=first, to_season=last, model_version=version), indent=2))
        return 0

    if args.command == "build-team-pace":
        print(json.dumps(build_team_pace(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-xdrives-dataset":
        print(json.dumps(build_xdrives_dataset(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xdrives-baselines":
        print(json.dumps(evaluate_xdrives_baselines(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "fit-xdrives-advanced":
        kwargs = {} if args.xdrives_l2 is None else {"l2": args.xdrives_l2}
        print(json.dumps(fit_xdrives_advanced(
            repository, from_season=first, to_season=last, **kwargs), indent=2))
        return 0
    if args.command == "evaluate-xdrives-advanced":
        missing = [name for name in ("train_from_year", "train_to_year", "test_from_year", "test_to_year")
                   if getattr(args, name) is None]
        if missing:
            raise SystemExit(f"evaluate-xdrives-advanced requires --{', --'.join(n.replace('_', '-') for n in missing)}")
        kwargs = {} if args.xdrives_l2 is None else {"l2": args.xdrives_l2}
        print(json.dumps(evaluate_xdrives_advanced(
            repository, train_from=args.train_from_year, train_to=args.train_to_year,
            test_from=args.test_from_year, test_to=args.test_to_year, **kwargs), indent=2))
        return 0

    if args.command == "build-xplays-dataset":
        print(json.dumps(build_xplays_dataset(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xplays-baselines":
        print(json.dumps(evaluate_xplays_baselines(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-expected-plays":
        print(json.dumps(evaluate_expected_plays(repository, from_season=first, to_season=last), indent=2))
        return 0

    if args.command == "build-xvolume-dataset":
        print(json.dumps(build_xvolume_dataset(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xvolume-baselines":
        print(json.dumps(evaluate_xvolume_baselines(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-expected-volume":
        print(json.dumps(evaluate_expected_volume(repository, from_season=first, to_season=last), indent=2))
        return 0

    if args.command == "build-xyards-dataset":
        print(json.dumps(build_xyards_dataset(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xyards-baselines":
        print(json.dumps(evaluate_xyards_baselines(repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-expected-yardage":
        print(json.dumps(evaluate_expected_yardage(repository, from_season=first, to_season=last), indent=2))
        return 0

    if args.command == "build-team-scoring":
        print(json.dumps(build_team_scoring(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-xturnovers-dataset":
        print(json.dumps(build_xturnovers_dataset(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xturnovers-baselines":
        print(json.dumps(evaluate_xturnovers_baselines(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-xredzone-dataset":
        print(json.dumps(build_xredzone_dataset(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xredzone-baselines":
        print(json.dumps(evaluate_xredzone_baselines(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-team-special-teams":
        print(json.dumps(build_team_special_teams(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-xfieldposition-dataset":
        print(json.dumps(build_xfieldposition_dataset(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xfieldposition-baselines":
        print(json.dumps(evaluate_xfieldposition_baselines(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-team-drive-outcomes":
        print(json.dumps(build_team_drive_outcomes(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "build-xpoints-dataset":
        print(json.dumps(build_xpoints_dataset(
            repository, from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "evaluate-xpoints":
        train_from = args.train_from_year or first
        train_to = args.train_to_year or max(train_from, last - 1)
        test_from = args.test_from_year or last
        test_to = args.test_to_year or last
        print(json.dumps(evaluate_xpoints(
            repository, train_from=train_from, train_to=train_to,
            test_from=test_from, test_to=test_to), indent=2))
        return 0
    if args.command == "fit-xpoints":
        print(json.dumps(fit_xpoints(
            repository, from_season=first, to_season=last), indent=2))
        return 0

    if args.command == "fit-wp":
        version = _model_version(args, "wp")
        print(json.dumps(fit_wp(repository, from_season=first, to_season=last,
                                model_version=version), indent=2))
        return 0
    if args.command == "score-wp":
        version = _model_version(args, "wp")
        print(json.dumps(score_wp(repository, from_season=first, to_season=last,
                                  model_version=version), indent=2))
        return 0
    if args.command == "fit-wp-v2":
        version = _model_version(args, "wp-v2")
        print(json.dumps(fit_wp_v2(
            repository, from_season=first, to_season=last, model_version=version,
            epochs=args.epochs, learning_rate=args.learning_rate, l2=args.l2), indent=2))
        return 0
    if args.command == "score-wp-v2":
        version = _model_version(args, "wp-v2")
        print(json.dumps(score_wp_v2(repository, from_season=first, to_season=last,
                                     model_version=version), indent=2))
        return 0
    if args.command == "validate-wp":
        version = _model_version(args, "wp")
        print(json.dumps(validate_wp(repository, from_season=first, to_season=last,
                                    model_version=version), indent=2))
        return 0

    if args.command == "fit-wp-calibration":
        source = str(args.source_model_version or "wp-v1")
        version = str(args.model_version or f"{source}-calibrated")
        print(json.dumps(fit_wp_calibration(
            repository, source_model_version=source, calibration_version=version,
            from_season=first, to_season=last), indent=2))
        return 0
    if args.command == "score-wp-calibrated":
        source = str(args.source_model_version or "wp-v1")
        version = str(args.model_version or f"{source}-calibrated")
        print(json.dumps(score_wp_calibrated(
            repository, source_model_version=source, calibration_version=version,
            output_model_version=version, from_season=first, to_season=last), indent=2))
        return 0

    if args.command == "rebuild-values":
        edp = score_edp(repository, from_season=first, to_season=last,
                        model_version=_model_version(args, "edp"))
        ep_version = _model_version(args, "ep")
        ep = _ep_functions(ep_version)[1](
            repository, from_season=first, to_season=last, model_version=ep_version)
        wp = score_wp(repository, from_season=first, to_season=last,
                      model_version=_model_version(args, "wp"))
        print(json.dumps({"edp": edp, "ep": ep, "wp": wp}, indent=2))
        return 0

    client = client or CFBDClient(raw_cache_path=os.getenv("CFBD_RAW_CACHE_PATH", "instance/cfbd_raw"))
    if not client.configured:
        raise CFBDConfigurationError("CFBD_API_KEY is required for PBP backfill")
    failures = []
    total = 0
    cached_total = 0
    for year in range(first, last + 1):
        weeks = [args.week] if args.week is not None else repository.completed_weeks(year)
        if not weeks:
            print(f"{year}: no completed weeks stored; sync game history first")
            continue
        for week in weeks:
            week = int(week)
            ready, count, missing_games = _week_ready(repository, year, week)
            if ready and not args.force:
                cached_total += count
                print(f"{year} week {week}: cached ({count} plays)")
                continue
            try:
                # A week with completed games still missing plays is still
                # filling in; the year-long cache would freeze that miss for
                # a season, so read it live.
                ttl = LIVE_WEEK_TTL if missing_games else FINISHED_WEEK_TTL
                raw = client.get("/plays", {
                    "year": year, "week": week, "seasonType": "both", "classification": "fbs"
                }, cache_ttl_seconds=ttl, force=args.force)
                count = _replace_with_lock_retry(repository, raw, year=year, week=week)
                total += count
                print(f"{year} week {week}: {count} plays")
            except Exception as exc:
                failures.append(f"{year} week {week}")
                print(f"{year} week {week}: failed ({exc})")
    print(f"PBP backfill complete: {total} new/rebuilt plays, {cached_total} cached plays, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
