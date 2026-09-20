"""Walk-forward narrative adjustments for Football Lab totals.

The existing narrative engine was designed for spread context. This module asks
whether those same pregame narrative states contain independent information
about game totals.

Method:
- Use the existing leak-safe cfb_narrative_state tags.
- Collapse team-level tags to one game-level context: a tag is active if either
  team carries it before kickoff.
- For each target season, estimate each tag's total residual vs closing total
  from PRIOR seasons only.
- Require at least MIN_TAG_TRAIN_GAMES historical games.
- Shrink each tag mean toward zero with a fixed empirical-Bayes-style weight:
  n / (n + SHRINKAGE_GAMES).
- Average the active tag adjustments for the game.
- Compare unadjusted Football Lab vs narrative-adjusted Football Lab, and split
  the original Football Lab results by narrative agreement/opposition.

No tag meaning or threshold is selected from target-season outcomes.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import totals_market_movement as tmm
from sports_aggregator.cfb import narrative_shapes as ns
from sports_aggregator.cfb.repository import CFBRepository

MIN_TAG_TRAIN_GAMES = 30
SHRINKAGE_GAMES = 50

TAGS = (
    "statement_win",
    "upset_win",
    "bad_loss",
    "upset_loss",
    "letdown_candidate",
    "bounceback_candidate",
    "won_big_then_underdog",
    "market_darling",
    "market_skepticism",
    "market_chase",
    "market_lag",
    "lookahead_candidate",
    "sandwich_candidate",
    "disputed_team",
)


def _narrative_by_game(repository: CFBRepository) -> dict[int, dict[str, Any]]:
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                f"""SELECT game_id,team,side,season,week,{','.join(TAGS)}
                    FROM cfb_narrative_state
                    WHERE narrative_version=?""",
                (ns.NARRATIVE_VERSION,),
            )
        ]
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["game_id"])].append(row)

    out = {}
    for gid, grows in grouped.items():
        active = []
        counts = {}
        home_tags = []
        away_tags = []
        for tag in TAGS:
            count = sum(int(r.get(tag) or 0) for r in grows)
            counts[tag] = count
            if count:
                active.append(tag)
            if any(str(r.get("side")) == "home" and int(r.get(tag) or 0) for r in grows):
                home_tags.append(tag)
            if any(str(r.get("side")) == "away" and int(r.get(tag) or 0) for r in grows):
                away_tags.append(tag)
        out[gid] = {
            "active_tags": active,
            "tag_counts": counts,
            "home_tags": home_tags,
            "away_tags": away_tags,
        }
    return out


def _base_rows(repository: CFBRepository, *, test_season: int) -> list[dict[str, Any]]:
    rows = tmm.build_rows(repository, test_season=int(test_season))
    narrative = _narrative_by_game(repository)
    out = []
    for row in rows:
        n = narrative.get(int(row["game_id"]), {
            "active_tags": [], "tag_counts": {}, "home_tags": [], "away_tags": []
        })
        cooked = dict(row)
        cooked["market_total_residual"] = (
            float(row["actual_total"]) - float(row["closing_total"])
        )
        cooked["active_narrative_tags"] = list(n["active_tags"])
        cooked["narrative_tag_count"] = len(n["active_tags"])
        cooked["home_narrative_tags"] = list(n["home_tags"])
        cooked["away_narrative_tags"] = list(n["away_tags"])
        out.append(cooked)
    return out


def _tag_priors(train: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    priors = {}
    for tag in TAGS:
        vals = [
            float(r["market_total_residual"])
            for r in train if tag in r.get("active_narrative_tags", [])
        ]
        n = len(vals)
        raw_mean = sum(vals) / n if n else None
        if n >= MIN_TAG_TRAIN_GAMES and raw_mean is not None:
            weight = n / (n + SHRINKAGE_GAMES)
            adjusted = raw_mean * weight
        else:
            weight = None
            adjusted = None
        priors[tag] = {
            "n": n,
            "raw_mean_total_residual": raw_mean,
            "shrinkage_weight": weight,
            "adjustment": adjusted,
        }
    return priors


def _game_adjustment(
    row: dict[str, Any],
    priors: dict[str, dict[str, Any]],
) -> tuple[float | None, list[str]]:
    values = []
    used = []
    for tag in row.get("active_narrative_tags", []):
        p = priors.get(tag)
        if p and p.get("adjustment") is not None:
            values.append(float(p["adjustment"]))
            used.append(tag)
    if not values:
        return None, []
    return sum(values) / len(values), used


def _result(aligned: float) -> str:
    return "win" if aligned > 0 else "loss" if aligned < 0 else "push"


def _classified(repository: CFBRepository, *, test_season: int) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    rows = _base_rows(repository, test_season=int(test_season))
    seasons = sorted({int(r["season"]) for r in rows})
    out = []
    season_priors = {}

    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [r for r in rows if int(r["season"]) == season]
        priors = _tag_priors(train)
        season_priors[season] = priors

        for row in test:
            adjustment, used = _game_adjustment(row, priors)
            model_direction = 1 if row["model_direction"] == "over" else -1
            original_aligned = float(row["close_result_aligned"])

            adjusted_projection = (
                float(row["projected_total"]) + adjustment
                if adjustment is not None else float(row["projected_total"])
            )
            adjusted_edge = adjusted_projection - float(row["closing_total"])
            adjusted_direction = 1 if adjusted_edge > 0 else -1 if adjusted_edge < 0 else 0
            adjusted_aligned = (
                (float(row["actual_total"]) - float(row["closing_total"]))
                * adjusted_direction if adjusted_direction else 0.0
            )

            narrative_direction = (
                1 if adjustment is not None and adjustment > 0
                else -1 if adjustment is not None and adjustment < 0
                else 0
            )
            if adjustment is None or narrative_direction == 0:
                relation = "missing_or_neutral"
            elif narrative_direction == model_direction:
                relation = "agrees"
            else:
                relation = "opposes"

            cooked = dict(row)
            cooked.update({
                "narrative_adjustment": adjustment,
                "narrative_tags_used": used,
                "narrative_relation_to_fl": relation,
                "original_closing_result": _result(original_aligned),
                "adjusted_projected_total": adjusted_projection,
                "adjusted_edge_vs_close": adjusted_edge,
                "adjusted_direction": (
                    "over" if adjusted_direction > 0
                    else "under" if adjusted_direction < 0
                    else "pick"
                ),
                "adjusted_aligned_residual": adjusted_aligned,
                "adjusted_closing_result": _result(adjusted_aligned),
                "direction_changed_by_narrative": bool(
                    adjusted_direction and adjusted_direction != model_direction
                ),
                "absolute_projection_error_original": abs(
                    float(row["projected_total"]) - float(row["actual_total"])
                ),
                "absolute_projection_error_adjusted": abs(
                    adjusted_projection - float(row["actual_total"])
                ),
            })
            out.append(cooked)

    return out, season_priors


def _bet_summary(rows: list[dict[str, Any]], result_key: str, residual_key: str) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r[result_key] == "win")
    losses = sum(1 for r in rows if r[result_key] == "loss")
    pushes = sum(1 for r in rows if r[result_key] == "push")
    decisions = wins + losses
    residuals = [float(r[residual_key]) for r in rows]
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "decisions": decisions,
        "win_rate_ex_pushes": round(wins / decisions, 4) if decisions else None,
        "mean_aligned_residual": (
            round(sum(residuals) / len(residuals), 3) if residuals else None
        ),
    }


def _projection_error(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0, "original_mae": None, "adjusted_mae": None,
            "mae_delta_adjusted_minus_original": None,
        }
    original = [float(r["absolute_projection_error_original"]) for r in rows]
    adjusted = [float(r["absolute_projection_error_adjusted"]) for r in rows]
    return {
        "n": len(rows),
        "original_mae": round(sum(original) / len(original), 3),
        "adjusted_mae": round(sum(adjusted) / len(adjusted), 3),
        "mae_delta_adjusted_minus_original": round(
            sum(adjusted) / len(adjusted) - sum(original) / len(original), 3
        ),
    }


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key, "unknown"))].append(row)
    return [
        {
            "value": value,
            "original": _bet_summary(
                grouped[value], "original_closing_result", "close_result_aligned"),
            "adjusted": _bet_summary(
                grouped[value], "adjusted_closing_result", "adjusted_aligned_residual"),
            "projection_error": _projection_error(grouped[value]),
        }
        for value in sorted(grouped)
    ]


def _tag_diagnostics(
    rows: list[dict[str, Any]],
    season_priors: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for season in sorted(season_priors):
        season_rows = [r for r in rows if int(r["season"]) == int(season)]
        priors = season_priors[season]
        for tag in TAGS:
            tagged = [r for r in season_rows if tag in r.get("active_narrative_tags", [])]
            p = priors[tag]
            actual_resids = [float(r["market_total_residual"]) for r in tagged]
            test_mean = (
                sum(actual_resids) / len(actual_resids) if actual_resids else None
            )
            train_adj = p.get("adjustment")
            output.append({
                "season": season,
                "tag": tag,
                "train_n": p.get("n"),
                "train_raw_mean_total_residual": (
                    round(float(p["raw_mean_total_residual"]), 3)
                    if p.get("raw_mean_total_residual") is not None else None
                ),
                "train_shrunk_adjustment": (
                    round(float(train_adj), 3) if train_adj is not None else None
                ),
                "test_n": len(tagged),
                "test_mean_total_residual": (
                    round(test_mean, 3) if test_mean is not None else None
                ),
                "direction_persisted": (
                    (float(train_adj) > 0) == (float(test_mean) > 0)
                    if train_adj is not None and test_mean is not None and test_mean != 0
                    else None
                ),
            })
    return output


def report(repository: CFBRepository, *, test_season: int = 2025) -> dict[str, Any]:
    rows, priors = _classified(repository, test_season=int(test_season))
    with_adjustment = [r for r in rows if r.get("narrative_adjustment") is not None]

    overall = {
        "all_rows": {
            "original": _bet_summary(
                rows, "original_closing_result", "close_result_aligned"),
            "adjusted": _bet_summary(
                rows, "adjusted_closing_result", "adjusted_aligned_residual"),
            "projection_error": _projection_error(rows),
        },
        "rows_with_narrative_adjustment": {
            "original": _bet_summary(
                with_adjustment, "original_closing_result", "close_result_aligned"),
            "adjusted": _bet_summary(
                with_adjustment, "adjusted_closing_result", "adjusted_aligned_residual"),
            "projection_error": _projection_error(with_adjustment),
        },
        "direction_changes": sum(
            1 for r in with_adjustment if r["direction_changed_by_narrative"]
        ),
    }

    return {
        "version": "totals-narrative-adjustment-v1",
        "test_through_season": int(test_season),
        "method": {
            "min_tag_train_games": MIN_TAG_TRAIN_GAMES,
            "shrinkage_games": SHRINKAGE_GAMES,
            "game_adjustment": "mean of eligible active-tag shrunk prior-season residuals",
            "target": "actual final total minus closing market total",
        },
        "overall": overall,
        "by_season": _group(rows, "season"),
        "by_narrative_relation_to_fl": _group(
            rows, "narrative_relation_to_fl"),
        "by_opening_edge_bucket": _group(rows, "opening_edge_bucket"),
        "by_model_direction": _group(rows, "model_direction"),
        "tag_diagnostics": _tag_diagnostics(rows, priors),
        "game_rows": rows,
        "notes": [
            "Narrative tag effects are estimated from prior seasons only.",
            "No narrative tag is manually assigned an Over or Under meaning.",
            "Tags with fewer than 30 prior games receive no adjustment.",
            "Tag means are shrunk toward zero before game-level aggregation.",
            "Narrative is evaluated as a contextual adjustment layer, not an equal vote.",
            "The existing narrative tags were originally designed for spread context; this report tests, rather than assumes, their totals relevance.",
        ],
    }


def export_report(payload: dict[str, Any], output_dir: str) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "totals_narrative_adjustment.json")
    summary_path = os.path.join(output_dir, "totals_narrative_adjustment_summary.csv")
    tags_path = os.path.join(output_dir, "totals_narrative_tags.csv")
    games_path = os.path.join(output_dir, "totals_narrative_adjustment_games.csv")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    summary_rows = []
    for scope in (
        "by_season", "by_narrative_relation_to_fl",
        "by_opening_edge_bucket", "by_model_direction",
    ):
        for row in payload[scope]:
            summary_rows.append({
                "scope": scope,
                "value": row["value"],
                **{f"original_{k}": v for k, v in row["original"].items()},
                **{f"adjusted_{k}": v for k, v in row["adjusted"].items()},
                **{f"error_{k}": v for k, v in row["projection_error"].items()},
            })
    fields = sorted({key for row in summary_rows for key in row})
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    tags = payload["tag_diagnostics"]
    with open(tags_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(tags[0].keys()) if tags else ["season", "tag"])
        writer.writeheader()
        writer.writerows(tags)

    games = payload["game_rows"]
    if games:
        export_games = []
        for row in games:
            cooked = dict(row)
            for key in ("active_narrative_tags", "home_narrative_tags",
                        "away_narrative_tags", "narrative_tags_used"):
                if isinstance(cooked.get(key), list):
                    cooked[key] = "|".join(cooked[key])
            export_games.append(cooked)
        with open(games_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(export_games[0].keys()))
            writer.writeheader()
            writer.writerows(export_games)
    else:
        with open(games_path, "w", encoding="utf-8") as handle:
            handle.write("")

    return {
        "json": json_path,
        "summary_csv": summary_path,
        "tags_csv": tags_path,
        "games_csv": games_path,
    }


def compact_console_summary(payload: dict[str, Any], paths: dict[str, str]) -> dict[str, Any]:
    return {
        "version": payload["version"],
        "overall": payload["overall"],
        "by_narrative_relation_to_fl": payload["by_narrative_relation_to_fl"],
        "files": paths,
    }
