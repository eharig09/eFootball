"""Production-facing NFL Football Lab forecast diagnostics.

Live output intentionally preserves two separate views:
1) Market Anchor: current spread/total converted to implied team scores.
2) Independent Football Lab: market-free forecast built from the validated
   recency-weighted football core.

Football Lab component deltas and historical residual scale are shown as
diagnostics. They are NOT automatically treated as betting edges.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.drive_projection import (
    TeamHistory, STATE_SEASON_DECAY, MIN_PRIOR_GAMES,
    _raw_games, _league_snapshot,
)
from sports_aggregator.nfl.drive_feature_ablation import (
    FOOTBALL_FEATURES as DRIVE_FEATURES,
    _fit as _fit_drive, _predict as _predict_drive,
)
from sports_aggregator.nfl.plays_projection import (
    BASE_FEATURES as PLAYS_FEATURES,
    _fit as _fit_plays, _predict as _predict_plays,
)
from sports_aggregator.nfl.efficiency_projection import (
    PASS_FEATURES,
    _fit as _fit_efficiency, _predict as _predict_efficiency,
)
from sports_aggregator.nfl.scoring_bridge import (
    _core_oof_rows, _fit_score, _predict_score,
)
from sports_aggregator.nfl.score_calibration import (
    _game_rows, _fit_ridge, _predict as _predict_cal,
    TOTAL_FEATURES, MARGIN_FEATURES,
)
from sports_aggregator.nfl.uncertainty_calibration import (
    _calibrated_oof, _fit_scale, _scale_predict,
    TOTAL_SCALE_FEATURES, MARGIN_SCALE_FEATURES,
)

MODEL_VERSION = "nfl-live-forecast-diagnostics-v1"


def _update_history(history: dict[str, TeamHistory], game: dict[str, Any]) -> None:
    home, away = str(game["home_team"]), str(game["away_team"])
    for side, team in (("home", home), ("away", away)):
        other = "away" if side == "home" else "home"
        h = history[team]
        h.games += 1
        h.weighted_games += 1.0
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


def _history_before_week(repository: NFLRepository, season: int, week: int):
    games = _raw_games(repository, 2010, season)
    history: dict[str, TeamHistory] = defaultdict(TeamHistory)
    previous_season: int | None = None
    for game in games:
        gs, gw = int(game["season"]), int(game["week"])
        if gs > season or (gs == season and gw >= week):
            continue
        if previous_season is not None and gs != previous_season:
            for h in history.values():
                h.decay_offseason(float(STATE_SEASON_DECAY))
        previous_season = gs
        _update_history(history, game)
    return history


def _target_games(repository: NFLRepository, season: int, week: int):
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            """SELECT game_id,season,week,game_date,away_team,home_team,completed,
                      division_game,spread_line,total_line,away_rest,home_rest
               FROM games
               WHERE season=? AND week=?
               ORDER BY game_date,game_id""",
            (int(season), int(week)),
        )
        return [dict(r) for r in rows]


def _market_anchor(game: dict[str, Any]) -> dict[str, float] | None:
    """The stored spread/total converted to implied team scores. Needs no
    fitted model at all, so it's available even when Football Lab's
    football-only core doesn't yet have enough training rows to fit."""
    spread = game.get("spread_line")
    total = game.get("total_line")
    if spread is None or total is None:
        return None
    return {
        "margin": float(spread),
        "total": float(total),
        "home_points": (float(total) + float(spread)) / 2.0,
        "away_points": (float(total) - float(spread)) / 2.0,
    }


def _team_row(game: dict[str, Any], side: str, history, league):
    team = str(game[f"{side}_team"])
    opponent = str(game["away_team" if side == "home" else "home_team"])
    own = history[team].snapshot()
    opp = history[opponent].snapshot()
    # Below MIN_PRIOR_GAMES, still build a row rather than blacking out the
    # whole game: the fallback loop below already leans on league averages
    # for any missing field, which is exactly what a thin sample needs. Just
    # flag it so the caller can show an honest "still warming up" caveat
    # instead of presenting it with full confidence.
    thin_sample = (
        int(own.get("games") or 0) < MIN_PRIOR_GAMES
        or int(opp.get("games") or 0) < MIN_PRIOR_GAMES
    )

    home = side == "home"
    rest = game.get("home_rest" if home else "away_rest")
    opp_rest = game.get("away_rest" if home else "home_rest")
    row = {
        "game_id": game["game_id"], "season": int(game["season"]), "week": int(game["week"]),
        "team": team, "opponent": opponent, "side": side,
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
        "rest_diff": float(rest or 0) - float(opp_rest or 0),
        "home": 1.0 if home else 0.0,
        "division_game": float(game.get("division_game") or 0),
    }
    fallback = {
        "team_drives": "drives_for", "opponent_drives_allowed": "drives_allowed",
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
    }
    for key, league_key in fallback.items():
        if row.get(key) is None:
            row[key] = float(league.get(league_key, 0.0))
    row["thin_sample"] = thin_sample
    return row


def report(repository: NFLRepository, *, season: int, week: int):
    history = _history_before_week(repository, season, week)
    league = _league_snapshot(history)
    games = _target_games(repository, season, week)

    # First-stage models refit on every completed pregame row available before target week.
    all_rows = _core_oof_rows(repository, 2010, max(2010, season - 1))
    component_rows = []
    # Add completed current-season rows only as raw training examples for first-stage refit.
    from sports_aggregator.nfl.drive_projection import build_rows
    current_completed = build_rows(repository, start_season=2010, end_season=season)
    current_completed = [
        r for r in current_completed
        if int(r["season"]) < season or int(r["week"]) < week
    ]

    drive_model = _fit_drive(current_completed, DRIVE_FEATURES)
    plays_model = _fit_plays(current_completed, PLAYS_FEATURES)
    pass_model = _fit_efficiency(current_completed, PASS_FEATURES, "actual_pass_epa_per_play")

    # Score bridge and total/margin calibrators remain trained on validated historical OOF data.
    historical_oof = _core_oof_rows(repository, 2010, max(2010, season - 1))
    score_model = _fit_score(historical_oof)
    historical_games = _game_rows(repository, start_season=2010, end_season=max(2010, season - 1))
    total_model = _fit_ridge(historical_games, TOTAL_FEATURES, "actual_total")
    margin_model = _fit_ridge(historical_games, MARGIN_FEATURES, "actual_margin")

    calibrated = _calibrated_oof(repository, 2010, max(2010, season - 1))
    total_scale_model = _fit_scale(calibrated, TOTAL_SCALE_FEATURES, "total_residual")
    margin_scale_model = _fit_scale(calibrated, MARGIN_SCALE_FEATURES, "margin_residual")

    output = []
    for game in games:
        away = _team_row(game, "away", history, league)
        home = _team_row(game, "home", history, league)
        if not away or not home or not all((drive_model, plays_model, pass_model, score_model, total_model, margin_model)):
            # The football-only core doesn't have enough training rows yet
            # (e.g. early in a season with no prior-season history loaded).
            # The market anchor needs no model at all, so still show it --
            # only the independent Football Lab side is actually unavailable.
            output.append({
                "game_id": game["game_id"],
                "season": int(game["season"]), "week": int(game["week"]),
                "game_date": game.get("game_date"),
                "away_team": game["away_team"], "home_team": game["home_team"],
                "completed": bool(game.get("completed")),
                "status": "insufficient_history",
                "market_anchor": _market_anchor(game),
                "football_lab": None,
                "disagreement": None,
                "historical_uncertainty_scale": None,
                "interpretation": (
                    "Football Lab's independent model needs more completed games "
                    "this season to train on before it can project this matchup "
                    "-- typically ready by week 3-4. The market anchor is shown "
                    "on its own until then."
                ),
            })
            continue

        team_rows = []
        for row in (away, home):
            row = dict(row)
            row["pred_drives"] = _predict_drive(drive_model, row)
            row["pred_plays_per_drive"] = _predict_plays(plays_model, row)
            row["pred_total_plays"] = row["pred_drives"] * row["pred_plays_per_drive"]
            row["pred_pass_rate"] = (
                float(row["team_neutral_pass_rate"]) + float(row["opponent_neutral_pass_rate"])
            ) / 2.0
            row["pred_pass_epa"] = _predict_efficiency(pass_model, row)
            row["pred_rush_epa"] = (
                float(row["team_rush_epa_per_play"]) +
                float(row["opponent_rush_epa_allowed_per_play"])
            ) / 2.0
            row["pred_combined_epa_per_play"] = (
                row["pred_pass_rate"] * row["pred_pass_epa"] +
                (1.0 - row["pred_pass_rate"]) * row["pred_rush_epa"]
            )
            row["pred_combined_epa"] = row["pred_total_plays"] * row["pred_combined_epa_per_play"]
            row["pred_points_per_drive"] = _predict_score(score_model, row)
            row["raw_pred_points"] = row["pred_points_per_drive"] * row["pred_drives"]
            team_rows.append(row)

        away_r = next(r for r in team_rows if r["side"] == "away")
        home_r = next(r for r in team_rows if r["side"] == "home")
        game_row = {
            "week": int(game["week"]),
            "raw_total": home_r["raw_pred_points"] + away_r["raw_pred_points"],
            "raw_margin": home_r["raw_pred_points"] - away_r["raw_pred_points"],
            "sum_pred_drives": home_r["pred_drives"] + away_r["pred_drives"],
            "sum_pred_total_plays": home_r["pred_total_plays"] + away_r["pred_total_plays"],
            "sum_pred_combined_epa": home_r["pred_combined_epa"] + away_r["pred_combined_epa"],
            "diff_pred_drives": home_r["pred_drives"] - away_r["pred_drives"],
            "diff_pred_plays_per_drive": home_r["pred_plays_per_drive"] - away_r["pred_plays_per_drive"],
            "diff_pred_pass_rate": home_r["pred_pass_rate"] - away_r["pred_pass_rate"],
            "diff_pred_pass_epa": home_r["pred_pass_epa"] - away_r["pred_pass_epa"],
            "diff_pred_rush_epa": home_r["pred_rush_epa"] - away_r["pred_rush_epa"],
            "diff_pred_combined_epa": home_r["pred_combined_epa"] - away_r["pred_combined_epa"],
        }
        cal_total = _predict_cal(total_model, game_row)
        cal_margin = _predict_cal(margin_model, game_row)
        fl_home = (cal_total + cal_margin) / 2.0
        fl_away = (cal_total - cal_margin) / 2.0

        spread = game.get("spread_line")
        total = game.get("total_line")
        market = _market_anchor(game)

        scale = None
        if total_scale_model and margin_scale_model:
            scale_row = {
                **game_row,
                "cal_total": cal_total,
                "cal_margin": cal_margin,
                "abs_cal_margin": abs(cal_margin),
                "abs_sum_pred_combined_epa": abs(game_row["sum_pred_combined_epa"]),
                "abs_diff_pred_drives": abs(game_row["diff_pred_drives"]),
                "abs_diff_pred_pass_epa": abs(game_row["diff_pred_pass_epa"]),
                "abs_diff_pred_rush_epa": abs(game_row["diff_pred_rush_epa"]),
                "abs_diff_pred_combined_epa": abs(game_row["diff_pred_combined_epa"]),
            }
            scale = {
                "margin_residual_scale": _scale_predict(margin_scale_model, scale_row),
                "total_residual_scale": _scale_predict(total_scale_model, scale_row),
            }

        output.append({
            "game_id": game["game_id"],
            "season": int(game["season"]), "week": int(game["week"]),
            "game_date": game.get("game_date"),
            "away_team": game["away_team"], "home_team": game["home_team"],
            "completed": bool(game.get("completed")),
            "thin_sample": bool(home_r.get("thin_sample") or away_r.get("thin_sample")),
            "market_anchor": market,
            "football_lab": {
                "margin": cal_margin,
                "total": cal_total,
                "home_points": fl_home,
                "away_points": fl_away,
            },
            "disagreement": {
                "margin": cal_margin - float(spread) if spread is not None else None,
                "total": cal_total - float(total) if total is not None else None,
            },
            "components": {
                "home": {
                    "drives": home_r["pred_drives"],
                    "plays_per_drive": home_r["pred_plays_per_drive"],
                    "pass_rate": home_r["pred_pass_rate"],
                    "pass_epa_per_play": home_r["pred_pass_epa"],
                    "rush_epa_per_play": home_r["pred_rush_epa"],
                    "combined_epa_per_play": home_r["pred_combined_epa_per_play"],
                },
                "away": {
                    "drives": away_r["pred_drives"],
                    "plays_per_drive": away_r["pred_plays_per_drive"],
                    "pass_rate": away_r["pred_pass_rate"],
                    "pass_epa_per_play": away_r["pred_pass_epa"],
                    "rush_epa_per_play": away_r["pred_rush_epa"],
                    "combined_epa_per_play": away_r["pred_combined_epa_per_play"],
                },
                "differentials_home_minus_away": {
                    "drives": game_row["diff_pred_drives"],
                    "plays_per_drive": game_row["diff_pred_plays_per_drive"],
                    "pass_rate": game_row["diff_pred_pass_rate"],
                    "pass_epa": game_row["diff_pred_pass_epa"],
                    "rush_epa": game_row["diff_pred_rush_epa"],
                    "combined_epa": game_row["diff_pred_combined_epa"],
                },
            },
            "historical_uncertainty_scale": scale,
            "interpretation": (
                "Market anchor is the primary accuracy baseline. Football Lab is an "
                "independent diagnostic forecast; disagreement is not automatically an edge."
            ),
        })

    return {
        "version": MODEL_VERSION,
        "season": int(season), "week": int(week),
        "state_season_decay": STATE_SEASON_DECAY,
        "production_policy": {
            "primary_accuracy_view": "market implied score",
            "independent_diagnostic_view": "Football Lab",
            "automatic_market_adjustment": False,
        },
        "games": output,
    }
