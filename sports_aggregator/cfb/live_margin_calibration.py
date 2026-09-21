"""Production CFB margin calibration.

Frozen after walk-forward/common-sample validation:
  raw Football Lab margin
  + projected PPD differential
  + projected drive differential
  + Elo differential
  + CORE margin
  + FPI margin
  + recent margin differential
  + red-zone scoring-rate differential

Vegas is intentionally excluded from model fitting and prediction.
If a live external rating is unavailable, fall back through nested
walk-forward-tested feature sets rather than imputing market information.

Every tier requires elo_diff. There used to be a further, elo-less "base"
tier (raw_margin/ppd_diff/drive_diff only) as a last-resort fallback; it was
removed after the team missing pregame Elo turned out to almost always be an
FCS opponent CFBD never rates, and applying a model fit on well-matched
FBS-vs-FBS games to that kind of blowout produced wildly unstable margins
(one 2022 buy game reached a -74pt edge). A team with no Elo is now simply
not assessed by margin-v2 -- predict_with_models() returns no tier, and
callers fall back to their own simpler estimate (e.g. matchup_research.py's
raw projected-points margin) instead of a ridge model extrapolating outside
its training distribution.

red_zone_diff (home minus away, each side's shrunk trips-per-drive x
shrunk red-zone-TD-rate, the same two shrinks game_projection.py serves
live) was added after margin_feature_ablation.py's walk-forward test found
it beat plain plus_recent in every one of 2023, 2024 and 2025 held out in
turn: pooled margin MAE 11.1385 -> 11.0833. A matching turnover_diff
feature was tested alongside it and only shaved off another ~0.007 pooled
MAE combined -- inside noise for the added complexity of a second shrunk
blend -- so it was left out of the live model; see
margin_feature_ablation.py's "plus_turnovers"/"plus_turnovers_redzone"
tiers if that's revisited.

hc_diff/qb_diff (home minus away pregame HC/QB Elo -- the same person-level
ratings Engine A already reads categorically as its 4th convergence signal,
here as continuous margin-v2 regression inputs instead) were added after
the same walk-forward test found a further, smaller improvement on top of
plus_redzone: pooled margin MAE 11.0833 -> 11.0733, and pooled score MAE
improved in every one of 2023-2025 (margin MAE only in 2 of 3 -- 2024 was
flat). Weaker evidence than red_zone_diff, kept anyway since it was
consistently non-negative. A matching weather feature set
(temperature/wind/precipitation) was tested alongside it and made margin
MAE WORSE in 2 of 3 seasons -- not added; see margin_feature_ablation.py's
"plus_weather" tier for the numbers if that's revisited with a different
formulation (e.g. interacted with team pass rate rather than as a main
effect).
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from sports_aggregator.cfb import coach_elo, qb_elo
from sports_aggregator.cfb.projection_backtest import BACKTEST_VERSION
from sports_aggregator.cfb.xpoints import DATASET_VERSION as XPOINTS_VERSION
from sports_aggregator.cfb.xredzone import DATASET_VERSION as XREDZONE_VERSION
from sports_aggregator.cfb.xredzone import SHRINKAGE_PSEUDO_GAMES as REDZONE_PSEUDO_GAMES
from sports_aggregator.cfb.game_projection import _shrunk_blend

MODEL_VERSION = "margin-v2"
L2 = 2.0

FEATURE_SETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plus_hc_qb_elo", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
        "fpi_margin", "recent_margin_diff", "red_zone_diff", "hc_diff", "qb_diff",
    )),
    ("plus_redzone", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
        "core_margin", "fpi_margin", "recent_margin_diff", "red_zone_diff",
    )),
    ("plus_recent", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
        "core_margin", "fpi_margin", "recent_margin_diff",
    )),
    ("plus_fpi", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
        "core_margin", "fpi_margin",
    )),
    ("plus_core", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff", "core_margin",
    )),
    ("plus_elo", (
        "raw_margin", "ppd_diff", "drive_diff", "elo_diff",
    )),
)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    aug = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, n):
            factor = aug[row][col] / aug[col][col]
            for idx in range(col, n + 1):
                aug[row][idx] -= factor * aug[col][idx]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        out[row] = (
            aug[row][n]
            - sum(aug[row][idx] * out[idx] for idx in range(row + 1, n))
        ) / aug[row][row]
    return out


def _fit(rows: list[dict[str, Any]], features: tuple[str, ...]) -> dict[str, Any] | None:
    eligible = [
        row for row in rows
        if row.get("actual_margin") is not None
        and all(row.get(key) is not None for key in features)
    ]
    if len(eligible) < 100:
        return None
    means = {key: sum(float(r[key]) for r in eligible) / len(eligible) for key in features}
    scales = {}
    for key in features:
        scale = math.sqrt(
            sum((float(r[key]) - means[key]) ** 2 for r in eligible) / len(eligible)
        )
        scales[key] = scale or 1.0

    size = len(features) + 1
    xtx = [[0.0] * size for _ in range(size)]
    xty = [0.0] * size
    for row in eligible:
        x = [1.0] + [
            (float(row[key]) - means[key]) / scales[key] for key in features
        ]
        y = float(row["actual_margin"])
        for i in range(size):
            xty[i] += x[i] * y
            for j in range(size):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, size):
        xtx[i][i] += L2
    beta = _solve(xtx, xty)
    if beta is None:
        return None
    return {
        "features": features,
        "means": means,
        "scales": scales,
        "beta": beta,
        "training_rows": len(eligible),
    }


def _side_red_zone_rate(row: dict[str, Any]) -> float | None:
    """Same shrink game_projection.py serves live: trips-per-drive x
    red-zone-TD-rate, each shrunk toward league by its own validated
    pseudo-games constant (see xredzone.SHRINKAGE_PSEUDO_GAMES)."""
    trips_league = row.get("league_prior_trips_per_drive")
    td_league = row.get("league_prior_red_zone_td_rate")
    if trips_league is None or td_league is None:
        return None
    sample = min(row.get("rz_team_games") or 0, row.get("rz_opp_games") or 0)
    trips = _shrunk_blend(
        row.get("team_prior_trips_per_drive"), row.get("opponent_prior_trips_allowed_per_drive"),
        trips_league, sample, pseudo_games=REDZONE_PSEUDO_GAMES["trips_per_drive"],
    )
    td_rate = _shrunk_blend(
        row.get("team_prior_red_zone_td_rate"), row.get("opponent_prior_red_zone_td_rate_allowed"),
        td_league, sample, pseudo_games=REDZONE_PSEUDO_GAMES["red_zone_td_rate"],
    )
    return trips * td_rate if trips is not None and td_rate is not None else None


def _historical_rows(repository, *, target_season: int) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        rows = [dict(r) for r in connection.execute(
            """SELECT p.game_id,p.season,p.side,p.team,
                      p.projected_offensive_points,p.projected_points_per_drive,
                      p.projected_drives,p.actual_score_points,
                      x.elo_difference,x.core_margin,x.fpi_margin,
                      x.team_recent_margin,x.opponent_recent_margin,
                      rz.team_prior_games AS rz_team_games,
                      rz.team_prior_trips_per_drive, rz.team_prior_red_zone_td_rate,
                      rz.opponent_prior_games AS rz_opp_games,
                      rz.opponent_prior_trips_allowed_per_drive,
                      rz.opponent_prior_red_zone_td_rate_allowed,
                      rz.league_prior_trips_per_drive, rz.league_prior_red_zone_td_rate,
                      hc.home_pre_elo AS hc_home_pre_elo, hc.away_pre_elo AS hc_away_pre_elo,
                      (SELECT q.pre_rating FROM cfb_qb_elo_games q
                       WHERE q.game_id=p.game_id AND q.side='home') qb_home_pre_rating,
                      (SELECT q.pre_rating FROM cfb_qb_elo_games q
                       WHERE q.game_id=p.game_id AND q.side='away') qb_away_pre_rating
               FROM cfb_projection_backtest p
               LEFT JOIN cfb_xpoints_dataset x
                 ON x.game_id=p.game_id AND x.team=p.team AND x.dataset_version=?
               LEFT JOIN cfb_xredzone_dataset rz
                 ON rz.game_id=p.game_id AND rz.team=p.team AND rz.dataset_version=?
               LEFT JOIN cfb_coach_elo_games hc
                 ON hc.game_id=p.game_id
               WHERE p.backtest_version=? AND p.season<?
                 AND p.projected_offensive_points IS NOT NULL
                 AND p.actual_score_points IS NOT NULL
               ORDER BY p.season,p.game_id,p.side""",
            (XPOINTS_VERSION, XREDZONE_VERSION, BACKTEST_VERSION, int(target_season)),
        )]

    grouped: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        grouped[int(row["game_id"])][str(row["side"])] = row

    out = []
    for gid, sides in grouped.items():
        home, away = sides.get("home"), sides.get("away")
        if not home or not away:
            continue
        hp = float(home["projected_offensive_points"])
        ap = float(away["projected_offensive_points"])
        ha = float(home["actual_score_points"])
        aa = float(away["actual_score_points"])
        h_red_zone, a_red_zone = _side_red_zone_rate(home), _side_red_zone_rate(away)
        out.append({
            "game_id": gid,
            "season": int(home["season"]),
            "raw_margin": hp - ap,
            "ppd_diff": (
                float(home["projected_points_per_drive"])
                - float(away["projected_points_per_drive"])
                if home.get("projected_points_per_drive") is not None
                and away.get("projected_points_per_drive") is not None
                else None
            ),
            "drive_diff": (
                float(home["projected_drives"]) - float(away["projected_drives"])
                if home.get("projected_drives") is not None
                and away.get("projected_drives") is not None
                else None
            ),
            "elo_diff": (
                float(home["elo_difference"])
                if home.get("elo_difference") is not None else None
            ),
            "core_margin": (
                float(home["core_margin"])
                if home.get("core_margin") is not None else None
            ),
            "fpi_margin": (
                float(home["fpi_margin"])
                if home.get("fpi_margin") is not None else None
            ),
            "hc_diff": (
                float(home["hc_home_pre_elo"]) - float(home["hc_away_pre_elo"])
                if home.get("hc_home_pre_elo") is not None
                and home.get("hc_away_pre_elo") is not None
                else None
            ),
            "qb_diff": (
                float(home["qb_home_pre_rating"]) - float(home["qb_away_pre_rating"])
                if home.get("qb_home_pre_rating") is not None
                and home.get("qb_away_pre_rating") is not None
                else None
            ),
            "recent_margin_diff": (
                float(home["team_recent_margin"]) - float(home["opponent_recent_margin"])
                if home.get("team_recent_margin") is not None
                and home.get("opponent_recent_margin") is not None
                else None
            ),
            "red_zone_diff": (
                h_red_zone - a_red_zone if h_red_zone is not None and a_red_zone is not None else None
            ),
            "actual_margin": ha - aa,
        })
    return out


def _hc_qb_diff(repository, game: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Home-minus-away pregame HC/QB Elo, same source Engine A's live
    signal reads: a completed-game pregame row when one is stored, else
    each side's current rating carried forward (coach_elo/qb_elo's
    current_rating, upcoming-game fallback shared with two_engine_live.py)."""
    if not game or game.get("game_id") is None:
        return None, None
    game_id = int(game["game_id"])
    with repository._reader() as connection:
        hc = connection.execute(
            """SELECT home_pre_elo,away_pre_elo
               FROM cfb_coach_elo_games WHERE game_id=?""",
            (game_id,),
        ).fetchone()
        qb_rows = connection.execute(
            """SELECT side,pre_rating FROM cfb_qb_elo_games WHERE game_id=?""",
            (game_id,),
        ).fetchall()
    qb = {str(row["side"]): float(row["pre_rating"]) for row in qb_rows}
    if hc is not None and hc["home_pre_elo"] is not None and hc["away_pre_elo"] is not None \
            and "home" in qb and "away" in qb:
        return float(hc["home_pre_elo"]) - float(hc["away_pre_elo"]), qb["home"] - qb["away"]

    season = game.get("season")
    if season is None:
        return None, None
    hc_home, _ = coach_elo.current_rating(
        repository, season=int(season), team_id=int(game["home_team_id"]))
    hc_away, _ = coach_elo.current_rating(
        repository, season=int(season), team_id=int(game["away_team_id"]))
    qb_home, _ = qb_elo.current_rating(
        repository, season=int(season), team=str(game["home_team"]))
    qb_away, _ = qb_elo.current_rating(
        repository, season=int(season), team=str(game["away_team"]))
    hc_diff = hc_home - hc_away if hc_home is not None and hc_away is not None else None
    qb_diff = qb_home - qb_away if qb_home is not None and qb_away is not None else None
    return hc_diff, qb_diff


def live_features(
    projection: dict[str, Any], *, repository=None, game: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    home = projection.get("home") or {}
    away = projection.get("away") or {}
    quality = (projection.get("opponent_quality") or {}).get("home") or {}
    components = quality.get("components") or {}
    home_points = projection.get("home_points_snapshot") or {}
    away_points = projection.get("away_points_snapshot") or {}
    hc_diff, qb_diff = _hc_qb_diff(repository, game) if repository is not None else (None, None)

    hp = home.get("expected_points")
    ap = away.get("expected_points")
    hppd = home.get("points_per_drive")
    appd = away.get("points_per_drive")
    hd = home.get("drives")
    ad = away.get("drives")
    elo_points = components.get("elo")

    return {
        "raw_margin": float(hp) - float(ap) if hp is not None and ap is not None else None,
        "ppd_diff": float(hppd) - float(appd) if hppd is not None and appd is not None else None,
        "drive_diff": float(hd) - float(ad) if hd is not None and ad is not None else None,
        # matchup_quality_snapshot converts Elo to point-like scale by /25.
        # Historical margin-v2 was fit on raw Elo difference.
        "elo_diff": float(elo_points) * 25.0 if elo_points is not None else None,
        "core_margin": (
            float(components["core"]) if components.get("core") is not None else None
        ),
        "fpi_margin": (
            float(components["fpi"]) if components.get("fpi") is not None else None
        ),
        "recent_margin_diff": (
            float(home_points["recent_margin"]) - float(away_points["recent_margin"])
            if home_points.get("recent_margin") is not None
            and away_points.get("recent_margin") is not None
            else None
        ),
        "red_zone_diff": (
            float(home["red_zone_scoring_rate"]) - float(away["red_zone_scoring_rate"])
            if home.get("red_zone_scoring_rate") is not None
            and away.get("red_zone_scoring_rate") is not None
            else None
        ),
        "hc_diff": hc_diff,
        "qb_diff": qb_diff,
    }


def fit_models(history: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """One fitted ridge model per FEATURE_SETS tier, trained on `history`
    (the caller decides the walk-forward cutoff -- see _historical_rows).
    Shared by predict_live() and internal_power_lenses._football_lab_margin_lookup()
    so both read the same models rather than each fitting their own."""
    return {label: _fit(history, features) for label, features in FEATURE_SETS}


def predict_with_models(
    models: dict[str, dict[str, Any] | None], features: dict[str, float | None],
) -> tuple[str, float, dict[str, Any]] | tuple[None, None, None]:
    """The richest FEATURE_SETS tier whose features are all present in
    `features` AND has a fitted model in `models`, evaluated -- the same
    richest-available-tier fallback predict_live() and the reconciled
    discovery-time lookup both use."""
    for label, feature_names in FEATURE_SETS:
        if any(features.get(key) is None for key in feature_names):
            continue
        model = models.get(label)
        if model is None:
            continue
        value = float(model["beta"][0])
        for idx, key in enumerate(feature_names, 1):
            value += (
                float(model["beta"][idx])
                * (float(features[key]) - float(model["means"][key]))
                / float(model["scales"][key])
            )
        return label, value, model
    return None, None, None


def predict_live(repository, *, target_season: int,
                 projection: dict[str, Any],
                 game: dict[str, Any] | None = None) -> dict[str, Any]:
    features = live_features(projection, repository=repository, game=game)
    history = _historical_rows(repository, target_season=int(target_season))
    models = fit_models(history)

    label, value, model = predict_with_models(models, features)
    if label is not None:
        feature_names = dict(FEATURE_SETS)[label]
        return {
            "value": value,
            "model_version": MODEL_VERSION,
            "variant": label,
            "features": {key: features[key] for key in feature_names},
            "missing_features": [
                key for key in FEATURE_SETS[0][1] if features.get(key) is None
            ],
            "training_games": int(model["training_rows"]),
            "l2": L2,
            "market_used": False,
        }

    missing = [key for key in FEATURE_SETS[0][1] if features.get(key) is None]
    if features.get("elo_diff") is None:
        # A team with no pregame Elo on record (almost always an FCS
        # opponent CFBD doesn't rate) is not assessed at all, rather than
        # falling back to a raw points-based margin that looks like a real
        # prediction -- see the module docstring for why.
        return {
            "value": None,
            "model_version": MODEL_VERSION,
            "variant": "not_assessed_missing_elo",
            "features": features,
            "missing_features": missing,
            "training_games": 0,
            "l2": L2,
            "market_used": False,
        }

    raw = features.get("raw_margin")
    return {
        "value": raw,
        "model_version": MODEL_VERSION,
        "variant": "raw_fallback",
        "features": features,
        "missing_features": missing,
        "training_games": 0,
        "l2": L2,
        "market_used": False,
    }
