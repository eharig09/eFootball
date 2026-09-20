"""Leak-safe NFL Football Lab drive estimator and historical backtest.

The first NFL projection layer mirrors the CFB build philosophy:
estimate possessions before trying to estimate plays, efficiency, yards, or points.

Every feature is a pregame snapshot. Games are processed in week batches, so
no game in a week can inform another game in that same week.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-drive-v1"
MIN_PRIOR_GAMES = 3
RIDGE_ALPHA = 8.0

FEATURES = (
    "team_drives",
    "opponent_drives_allowed",
    "team_plays_per_drive",
    "opponent_plays_per_drive_allowed",
    "team_neutral_seconds_per_play",
    "opponent_neutral_seconds_per_play",
    "team_neutral_pass_rate",
    "opponent_neutral_pass_rate",
    "team_epa_per_play",
    "opponent_epa_allowed_per_play",
    "team_success_rate",
    "opponent_success_allowed_rate",
    "team_explosive_rate",
    "opponent_explosive_allowed_rate",
    "market_total",
    "abs_spread",
    "rest_diff",
    "home",
    "division_game",
)


@dataclass
class TeamHistory:
    games: int = 0
    drives_for: float = 0.0
    drives_against: float = 0.0
    plays: float = 0.0
    opponent_plays: float = 0.0
    seconds_sum: float = 0.0
    clocked_plays: float = 0.0
    neutral_plays: float = 0.0
    neutral_passes: float = 0.0
    opponent_seconds_sum: float = 0.0
    opponent_clocked_plays: float = 0.0
    opponent_neutral_plays: float = 0.0
    opponent_neutral_passes: float = 0.0
    total_epa: float = 0.0
    pass_plays: float = 0.0
    pass_epa: float = 0.0
    rush_plays: float = 0.0
    rush_epa: float = 0.0
    successful_plays: float = 0.0
    explosive_plays: float = 0.0
    opponent_total_epa: float = 0.0
    opponent_pass_plays: float = 0.0
    opponent_pass_epa: float = 0.0
    opponent_rush_plays: float = 0.0
    opponent_rush_epa: float = 0.0
    opponent_successful_plays: float = 0.0
    opponent_explosive_plays: float = 0.0

    def snapshot(self) -> dict[str, float | None]:
        if not self.games:
            return {}
        return {
            "games": self.games,
            "drives_for": self.drives_for / self.games,
            "drives_allowed": self.drives_against / self.games,
            "plays_per_drive": self.plays / self.drives_for if self.drives_for else None,
            "plays_per_drive_allowed": (
                self.opponent_plays / self.drives_against if self.drives_against else None
            ),
            "neutral_seconds_per_play": (
                self.seconds_sum / self.clocked_plays if self.clocked_plays else None
            ),
            "neutral_pass_rate": (
                self.neutral_passes / self.neutral_plays if self.neutral_plays else None
            ),
            "opponent_neutral_seconds_per_play": (
                self.opponent_seconds_sum / self.opponent_clocked_plays
                if self.opponent_clocked_plays else None
            ),
            "opponent_neutral_pass_rate": (
                self.opponent_neutral_passes / self.opponent_neutral_plays
                if self.opponent_neutral_plays else None
            ),
            "epa_per_play": self.total_epa / self.plays if self.plays else None,
            "pass_epa_per_play": self.pass_epa / self.pass_plays if self.pass_plays else None,
            "rush_epa_per_play": self.rush_epa / self.rush_plays if self.rush_plays else None,
            "success_rate": self.successful_plays / self.plays if self.plays else None,
            "explosive_rate": self.explosive_plays / self.plays if self.plays else None,
            "epa_allowed_per_play": (
                self.opponent_total_epa / self.opponent_plays if self.opponent_plays else None
            ),
            "pass_epa_allowed_per_play": (
                self.opponent_pass_epa / self.opponent_pass_plays
                if self.opponent_pass_plays else None
            ),
            "rush_epa_allowed_per_play": (
                self.opponent_rush_epa / self.opponent_rush_plays
                if self.opponent_rush_plays else None
            ),
            "success_allowed_rate": (
                self.opponent_successful_plays / self.opponent_plays
                if self.opponent_plays else None
            ),
            "explosive_allowed_rate": (
                self.opponent_explosive_plays / self.opponent_plays
                if self.opponent_plays else None
            ),
        }


def _raw_games(repository: NFLRepository, start_season: int, end_season: int) -> list[dict[str, Any]]:
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            """SELECT g.game_id,g.season,g.week,g.game_date,g.away_team,g.home_team,
                      g.completed,g.division_game,g.spread_line,g.total_line,
                      g.away_rest,g.home_rest,
                      hs.drives AS home_drives,hs.plays AS home_plays,
                      hs.neutral_plays AS home_neutral_plays,
                      hs.neutral_passes AS home_neutral_passes,
                      hs.seconds_sum AS home_seconds_sum,
                      hs.clocked_plays AS home_clocked_plays,
                      aw.drives AS away_drives,aw.plays AS away_plays,
                      aw.neutral_plays AS away_neutral_plays,
                      aw.neutral_passes AS away_neutral_passes,
                      aw.seconds_sum AS away_seconds_sum,
                      aw.clocked_plays AS away_clocked_plays,
                      he.total_epa AS home_total_epa,
                      he.pass_plays AS home_pass_plays,
                      he.pass_epa AS home_pass_epa,
                      he.rush_plays AS home_rush_plays,
                      he.rush_epa AS home_rush_epa,
                      he.successful_plays AS home_successful_plays,
                      he.explosive_plays AS home_explosive_plays,
                      ae.total_epa AS away_total_epa,
                      ae.pass_plays AS away_pass_plays,
                      ae.pass_epa AS away_pass_epa,
                      ae.rush_plays AS away_rush_plays,
                      ae.rush_epa AS away_rush_epa,
                      ae.successful_plays AS away_successful_plays,
                      ae.explosive_plays AS away_explosive_plays
               FROM games g
               JOIN game_team_situational hs
                 ON hs.game_id=g.game_id AND hs.team=g.home_team
               JOIN game_team_situational aw
                 ON aw.game_id=g.game_id AND aw.team=g.away_team
               JOIN game_team_efficiency he
                 ON he.game_id=g.game_id AND he.team=g.home_team
               JOIN game_team_efficiency ae
                 ON ae.game_id=g.game_id AND ae.team=g.away_team
               WHERE g.season BETWEEN ? AND ? AND g.completed=1
               ORDER BY g.season,g.week,g.game_date,g.game_id""",
            (int(start_season), int(end_season)),
        )
        return [dict(r) for r in rows]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _league_snapshot(history: dict[str, TeamHistory]) -> dict[str, float]:
    snapshots = [h.snapshot() for h in history.values() if h.games]
    keys = (
        "drives_for", "drives_allowed", "plays_per_drive",
        "plays_per_drive_allowed", "neutral_seconds_per_play",
        "neutral_pass_rate", "opponent_neutral_seconds_per_play",
        "opponent_neutral_pass_rate", "epa_per_play", "success_rate",
        "explosive_rate", "epa_allowed_per_play", "pass_epa_per_play",
        "rush_epa_per_play", "pass_epa_allowed_per_play",
        "rush_epa_allowed_per_play", "success_allowed_rate",
        "explosive_allowed_rate",
    )
    out: dict[str, float] = {}
    for key in keys:
        vals = [float(s[key]) for s in snapshots if s.get(key) is not None]
        value = _mean(vals)
        if value is not None:
            out[key] = value
    return out


def _feature_row(
    game: dict[str, Any], side: str, history: dict[str, TeamHistory],
    league: dict[str, float],
) -> dict[str, Any] | None:
    team = str(game[f"{side}_team"])
    opponent = str(game["away_team" if side == "home" else "home_team"])
    own = history[team].snapshot()
    opp = history[opponent].snapshot()
    if int(own.get("games") or 0) < MIN_PRIOR_GAMES or int(opp.get("games") or 0) < MIN_PRIOR_GAMES:
        return None

    home = side == "home"
    spread = game.get("spread_line")
    rest = game.get("home_rest" if home else "away_rest")
    opp_rest = game.get("away_rest" if home else "home_rest")

    values = {
        "team_drives": own.get("drives_for"),
        "opponent_drives_allowed": opp.get("drives_allowed"),
        "team_plays_per_drive": own.get("plays_per_drive"),
        "opponent_plays_per_drive_allowed": opp.get("plays_per_drive_allowed"),
        "team_neutral_seconds_per_play": own.get("neutral_seconds_per_play"),
        "opponent_neutral_seconds_per_play": opp.get("opponent_neutral_seconds_per_play"),
        "team_neutral_pass_rate": own.get("neutral_pass_rate"),
        "opponent_neutral_pass_rate": opp.get("opponent_neutral_pass_rate"),
        "team_epa_per_play": own.get("epa_per_play"),
        "opponent_epa_allowed_per_play": opp.get("epa_allowed_per_play"),
        "team_pass_epa_per_play": own.get("pass_epa_per_play"),
        "opponent_pass_epa_allowed_per_play": opp.get("pass_epa_allowed_per_play"),
        "team_rush_epa_per_play": own.get("rush_epa_per_play"),
        "opponent_rush_epa_allowed_per_play": opp.get("rush_epa_allowed_per_play"),
        "team_success_rate": own.get("success_rate"),
        "opponent_success_allowed_rate": opp.get("success_allowed_rate"),
        "team_explosive_rate": own.get("explosive_rate"),
        "opponent_explosive_allowed_rate": opp.get("explosive_allowed_rate"),
        "market_total": game.get("total_line"),
        "abs_spread": abs(float(spread)) if spread is not None else None,
        "rest_diff": (
            float(rest) - float(opp_rest)
            if rest is not None and opp_rest is not None else 0.0
        ),
        "home": 1.0 if home else 0.0,
        "division_game": float(game.get("division_game") or 0),
    }
    # Market fields can be absent historically. Use league/sample-neutral imputation,
    # never future information.
    values["market_total"] = (
        values["market_total"] if values["market_total"] is not None else 44.0
    )
    values["abs_spread"] = (
        values["abs_spread"] if values["abs_spread"] is not None else 3.0
    )
    for key in FEATURES:
        if values.get(key) is None:
            fallback_key = {
                "team_drives": "drives_for",
                "opponent_drives_allowed": "drives_allowed",
                "team_plays_per_drive": "plays_per_drive",
                "opponent_plays_per_drive_allowed": "plays_per_drive_allowed",
                "team_neutral_seconds_per_play": "neutral_seconds_per_play",
                "opponent_neutral_seconds_per_play": "opponent_neutral_seconds_per_play",
                "team_neutral_pass_rate": "neutral_pass_rate",
                "opponent_neutral_pass_rate": "opponent_neutral_pass_rate",
                "team_epa_per_play": "epa_per_play",
                "opponent_epa_allowed_per_play": "epa_allowed_per_play",
                "team_pass_epa_per_play": "pass_epa_per_play",
                "opponent_pass_epa_allowed_per_play": "pass_epa_allowed_per_play",
                "team_rush_epa_per_play": "rush_epa_per_play",
                "opponent_rush_epa_allowed_per_play": "rush_epa_allowed_per_play",
                "team_success_rate": "success_rate",
                "opponent_success_allowed_rate": "success_allowed_rate",
                "team_explosive_rate": "explosive_rate",
                "opponent_explosive_allowed_rate": "explosive_allowed_rate",
            }.get(key)
            values[key] = float(league.get(fallback_key, 0.0)) if fallback_key else 0.0

    return {
        "game_id": game["game_id"],
        "season": int(game["season"]),
        "week": int(game["week"]),
        "team": team,
        "opponent": opponent,
        "side": side,
        "actual_drives": float(game[f"{side}_drives"]),
        "actual_plays": float(game[f"{side}_plays"]),
        "actual_plays_per_drive": (
            float(game[f"{side}_plays"]) / float(game[f"{side}_drives"])
            if float(game[f"{side}_drives"]) else None
        ),
        "actual_neutral_pass_rate": (
            float(game[f"{side}_neutral_passes"]) / float(game[f"{side}_neutral_plays"])
            if float(game[f"{side}_neutral_plays"]) else None
        ),
        "actual_pass_epa_per_play": (
            float(game[f"{side}_pass_epa"]) / float(game[f"{side}_pass_plays"])
            if float(game[f"{side}_pass_plays"]) else None
        ),
        "actual_rush_epa_per_play": (
            float(game[f"{side}_rush_epa"]) / float(game[f"{side}_rush_plays"])
            if float(game[f"{side}_rush_plays"]) else None
        ),
        **{key: float(values[key]) for key in FEATURES},
    }


def build_rows(repository: NFLRepository, *, start_season: int = 2016,
               end_season: int = 2025) -> list[dict[str, Any]]:
    games = _raw_games(repository, start_season, end_season)
    history: dict[str, TeamHistory] = defaultdict(TeamHistory)
    rows: list[dict[str, Any]] = []
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for game in games:
        by_week[(int(game["season"]), int(game["week"]))].append(game)

    for season_week in sorted(by_week):
        current = by_week[season_week]
        league = _league_snapshot(history)
        for game in current:
            for side in ("away", "home"):
                row = _feature_row(game, side, history, league)
                if row:
                    rows.append(row)

        # Update only after the entire week is snapshotted.
        for game in current:
            home, away = str(game["home_team"]), str(game["away_team"])
            for side, team, opponent in (("home", home, away), ("away", away, home)):
                other = "away" if side == "home" else "home"
                h = history[team]
                h.games += 1
                h.drives_for += float(game[f"{side}_drives"])
                h.drives_against += float(game[f"{other}_drives"])
                h.plays += float(game[f"{side}_plays"])
                h.opponent_plays += float(game[f"{other}_plays"])
                h.seconds_sum += float(game[f"{side}_seconds_sum"])
                h.clocked_plays += float(game[f"{side}_clocked_plays"])
                h.neutral_plays += float(game[f"{side}_neutral_plays"])
                h.neutral_passes += float(game[f"{side}_neutral_passes"])
                h.opponent_seconds_sum += float(game[f"{other}_seconds_sum"])
                h.opponent_clocked_plays += float(game[f"{other}_clocked_plays"])
                h.opponent_neutral_plays += float(game[f"{other}_neutral_plays"])
                h.opponent_neutral_passes += float(game[f"{other}_neutral_passes"])
                h.total_epa += float(game[f"{side}_total_epa"])
                h.pass_plays += float(game[f"{side}_pass_plays"])
                h.pass_epa += float(game[f"{side}_pass_epa"])
                h.rush_plays += float(game[f"{side}_rush_plays"])
                h.rush_epa += float(game[f"{side}_rush_epa"])
                h.successful_plays += float(game[f"{side}_successful_plays"])
                h.explosive_plays += float(game[f"{side}_explosive_plays"])
                h.opponent_total_epa += float(game[f"{other}_total_epa"])
                h.opponent_pass_plays += float(game[f"{other}_pass_plays"])
                h.opponent_pass_epa += float(game[f"{other}_pass_epa"])
                h.opponent_rush_plays += float(game[f"{other}_rush_plays"])
                h.opponent_rush_epa += float(game[f"{other}_rush_epa"])
                h.opponent_successful_plays += float(game[f"{other}_successful_plays"])
                h.opponent_explosive_plays += float(game[f"{other}_explosive_plays"])
    return rows


def _fit_ridge(train: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(train) < 100:
        return None
    x = np.asarray([[r[k] for k in FEATURES] for r in train], dtype=float)
    y = np.asarray([r["actual_drives"] for r in train], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x - means) / scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {"means": means, "scales": scales, "beta": beta}


def _ridge_predict(model: dict[str, Any], row: dict[str, Any]) -> float:
    x = np.asarray([row[k] for k in FEATURES], dtype=float)
    z = (x - model["means"]) / model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def _summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [
        (float(r[key]), float(r["actual_drives"]))
        for r in rows if r.get(key) is not None
    ]
    if not values:
        return {"n": 0}
    errors = [pred - actual for pred, actual in values]
    abs_errors = [abs(v) for v in errors]
    return {
        "n": len(values),
        "mae": round(sum(abs_errors) / len(abs_errors), 4),
        "rmse": round(math.sqrt(sum(v * v for v in errors) / len(errors)), 4),
        "bias": round(sum(errors) / len(errors), 4),
        "within_1_drive": round(sum(v <= 1.0 for v in abs_errors) / len(abs_errors), 4),
        "within_2_drives": round(sum(v <= 2.0 for v in abs_errors) / len(abs_errors), 4),
    }


def report(repository: NFLRepository, *, start_season: int = 2016,
           end_season: int = 2025) -> dict[str, Any]:
    rows = build_rows(repository, start_season=start_season, end_season=end_season)
    seasons = sorted({int(r["season"]) for r in rows})
    evaluated: list[dict[str, Any]] = []
    folds = []

    for season in seasons:
        train = [r for r in rows if int(r["season"]) < season]
        test = [dict(r) for r in rows if int(r["season"]) == season]
        if len(train) < 100 or not test:
            continue
        league_drives = sum(float(r["actual_drives"]) for r in train) / len(train)
        model = _fit_ridge(train)
        if model is None:
            continue
        for row in test:
            row["pred_league"] = league_drives
            row["pred_team"] = float(row["team_drives"])
            row["pred_blend"] = (
                float(row["team_drives"]) + float(row["opponent_drives_allowed"])
            ) / 2.0
            row["pred_ridge"] = _ridge_predict(model, row)

        # NFL possessions are coupled. Compare independent team projections
        # with a shared game-environment estimate formed from both sides'
        # pregame offense/defense drive blends.
        by_game = defaultdict(list)
        for row in test:
            by_game[row["game_id"]].append(row)
        for game_rows in by_game.values():
            if len(game_rows) != 2:
                continue
            shared = sum(float(r["pred_blend"]) for r in game_rows) / 2.0
            ridge_shared = sum(float(r["pred_ridge"]) for r in game_rows) / 2.0
            for row in game_rows:
                row["pred_shared_blend"] = shared
                row["pred_shared_ridge"] = ridge_shared
        evaluated.extend(test)
        folds.append({
            "season": season,
            "train_rows": len(train),
            "test_rows": len(test),
            "league": _summary(test, "pred_league"),
            "team": _summary(test, "pred_team"),
            "blend": _summary(test, "pred_blend"),
            "ridge": _summary(test, "pred_ridge"),
            "shared_blend": _summary(test, "pred_shared_blend"),
            "shared_ridge": _summary(test, "pred_shared_ridge"),
        })

    return {
        "version": MODEL_VERSION,
        "target": "team offensive drives",
        "leakage_policy": "pregame snapshots; whole-week batch update; test season excluded from fit",
        "minimum_prior_games": MIN_PRIOR_GAMES,
        "ridge_alpha": RIDGE_ALPHA,
        "features": list(FEATURES),
        "dataset_rows": len(rows),
        "seasons_available": seasons,
        "walk_forward": folds,
        "pooled": {
            "league": _summary(evaluated, "pred_league"),
            "team": _summary(evaluated, "pred_team"),
            "blend": _summary(evaluated, "pred_blend"),
            "ridge": _summary(evaluated, "pred_ridge"),
            "shared_blend": _summary(evaluated, "pred_shared_blend"),
            "shared_ridge": _summary(evaluated, "pred_shared_ridge"),
        },
    }
