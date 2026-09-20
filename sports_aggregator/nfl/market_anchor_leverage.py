"""NFL market-anchored leverage experiment.

Keeps the independent Football Lab forecast untouched, but tests a separate
market-anchored score layer:

    market implied total/margin
      + walk-forward football leverage adjustment
      = anchored total/margin
      -> implied home/away points

The leverage model predicts actual-minus-market residuals using only prior
seasons. This directly tests whether Football Lab components can explain when
and how far the final score should move away from Vegas, rather than assuming
raw model-market disagreement is itself a signal.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.uncertainty_calibration import _calibrated_oof
from sports_aggregator.nfl.score_calibration import RIDGE_ALPHA

MODEL_VERSION = "nfl-market-anchor-leverage-v1"

MARGIN_EDGE_ONLY = ("fl_margin_edge",)
MARGIN_STRUCTURAL = (
    "fl_margin_edge",
    "diff_pred_drives",
    "diff_pred_plays_per_drive",
    "diff_pred_pass_rate",
    "diff_pred_pass_epa",
    "diff_pred_rush_epa",
    "diff_pred_combined_epa",
    "abs_market_margin",
    "market_total",
)

TOTAL_EDGE_ONLY = ("fl_total_edge",)
TOTAL_STRUCTURAL = (
    "fl_total_edge",
    "sum_pred_drives",
    "sum_pred_total_plays",
    "sum_pred_combined_epa",
    "abs_cal_margin",
    "market_total",
    "abs_market_margin",
)


def _market_map(repository: NFLRepository, start_season: int, end_season: int):
    repository.initialize()
    with repository._connect() as connection:
        rows = connection.execute(
            """SELECT game_id,spread_line,total_line
               FROM games
               WHERE season BETWEEN ? AND ? AND completed=1""",
            (int(start_season), int(end_season)),
        )
        return {
            str(r["game_id"]): {
                "market_margin": float(r["spread_line"]) if r["spread_line"] is not None else None,
                "market_total": float(r["total_line"]) if r["total_line"] is not None else None,
            }
            for r in rows
        }


def _fit(rows: list[dict[str, Any]], features: tuple[str, ...], target: str):
    eligible = [
        r for r in rows
        if r.get(target) is not None and all(r.get(k) is not None for k in features)
    ]
    if len(eligible) < 100:
        return None
    x = np.asarray([[r[k] for k in features] for r in eligible], dtype=float)
    y = np.asarray([r[target] for r in eligible], dtype=float)
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x-means)/scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0,0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {
        "features": features,
        "means": means,
        "scales": scales,
        "beta": beta,
        "n": len(eligible),
    }


def _predict(model, row):
    x = np.asarray([row[k] for k in model["features"]], dtype=float)
    z = (x-model["means"])/model["scales"]
    return float(model["beta"][0] + z @ model["beta"][1:])


def _summary(values: list[tuple[float,float]]):
    if not values:
        return {"n": 0}
    e=[p-a for p,a in values]
    ae=[abs(x) for x in e]
    return {
        "n": len(values),
        "mae": round(sum(ae)/len(ae),4),
        "rmse": round(math.sqrt(sum(x*x for x in e)/len(e)),4),
        "bias": round(sum(e)/len(e),4),
    }


def _directional(rows: list[dict[str, Any]], pred_key: str, market_key: str,
                 actual_key: str):
    wins=losses=pushes=no_bet=0
    edge_sizes=[]
    realized=[]
    for r in rows:
        edge=float(r[pred_key])-float(r[market_key])
        act=float(r[actual_key])-float(r[market_key])
        if abs(edge) < 1e-12:
            no_bet += 1
            continue
        edge_sizes.append(abs(edge))
        realized.append((1.0 if edge>0 else -1.0)*act)
        if abs(act) < 1e-12:
            pushes += 1
        elif edge*act > 0:
            wins += 1
        else:
            losses += 1
    decided=wins+losses
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "no_bet": no_bet,
        "decided": decided,
        "win_rate": round(wins/decided,4) if decided else None,
        "mean_abs_adjustment": round(sum(edge_sizes)/len(edge_sizes),4) if edge_sizes else None,
        "mean_directional_market_result": round(sum(realized)/len(realized),4) if realized else None,
    }


def _prepare(repository: NFLRepository, start_season: int, end_season: int):
    rows=_calibrated_oof(repository,start_season,end_season)
    markets=_market_map(repository,start_season,end_season)
    out=[]
    for r0 in rows:
        r=dict(r0)
        market=markets.get(str(r["game_id"]))
        if not market:
            continue
        r.update(market)
        if r["market_margin"] is None or r["market_total"] is None:
            continue
        r["fl_margin_edge"]=float(r["cal_margin"])-float(r["market_margin"])
        r["fl_total_edge"]=float(r["cal_total"])-float(r["market_total"])
        r["margin_market_residual"]=float(r["actual_margin"])-float(r["market_margin"])
        r["total_market_residual"]=float(r["actual_total"])-float(r["market_total"])
        r["abs_market_margin"]=abs(float(r["market_margin"]))
        r["abs_cal_margin"]=abs(float(r["cal_margin"]))
        r["market_home_points"]=(float(r["market_total"])+float(r["market_margin"]))/2.0
        r["market_away_points"]=(float(r["market_total"])-float(r["market_margin"]))/2.0
        out.append(r)
    return out


def report(repository: NFLRepository, *, start_season=2010, end_season=2025):
    rows=_prepare(repository,start_season,end_season)
    seasons=sorted({int(r["season"]) for r in rows})
    pooled=[]
    folds=[]

    for season in seasons:
        train=[r for r in rows if int(r["season"])<season]
        test=[dict(r) for r in rows if int(r["season"])==season]
        if len(train)<100 or not test:
            continue

        margin_edge=_fit(train,MARGIN_EDGE_ONLY,"margin_market_residual")
        margin_struct=_fit(train,MARGIN_STRUCTURAL,"margin_market_residual")
        total_edge=_fit(train,TOTAL_EDGE_ONLY,"total_market_residual")
        total_struct=_fit(train,TOTAL_STRUCTURAL,"total_market_residual")
        if not all((margin_edge,margin_struct,total_edge,total_struct)):
            continue

        for r in test:
            r["anch_margin_edge"]=float(r["market_margin"])+_predict(margin_edge,r)
            r["anch_margin_struct"]=float(r["market_margin"])+_predict(margin_struct,r)
            r["anch_total_edge"]=float(r["market_total"])+_predict(total_edge,r)
            r["anch_total_struct"]=float(r["market_total"])+_predict(total_struct,r)

            r["anch_home_edge"]=(r["anch_total_edge"]+r["anch_margin_edge"])/2.0
            r["anch_away_edge"]=(r["anch_total_edge"]-r["anch_margin_edge"])/2.0
            r["anch_home_struct"]=(r["anch_total_struct"]+r["anch_margin_struct"])/2.0
            r["anch_away_struct"]=(r["anch_total_struct"]-r["anch_margin_struct"])/2.0

            r["actual_home"]=(float(r["actual_total"])+float(r["actual_margin"]))/2.0
            r["actual_away"]=(float(r["actual_total"])-float(r["actual_margin"]))/2.0

        def metrics(group):
            market_margin=[(float(r["market_margin"]),float(r["actual_margin"])) for r in group]
            fl_margin=[(float(r["cal_margin"]),float(r["actual_margin"])) for r in group]
            edge_margin=[(float(r["anch_margin_edge"]),float(r["actual_margin"])) for r in group]
            struct_margin=[(float(r["anch_margin_struct"]),float(r["actual_margin"])) for r in group]

            market_total=[(float(r["market_total"]),float(r["actual_total"])) for r in group]
            fl_total=[(float(r["cal_total"]),float(r["actual_total"])) for r in group]
            edge_total=[(float(r["anch_total_edge"]),float(r["actual_total"])) for r in group]
            struct_total=[(float(r["anch_total_struct"]),float(r["actual_total"])) for r in group]

            market_scores=[]; fl_scores=[]; edge_scores=[]; struct_scores=[]
            for r in group:
                market_scores += [
                    (float(r["market_home_points"]),float(r["actual_home"])),
                    (float(r["market_away_points"]),float(r["actual_away"])),
                ]
                fl_scores += [
                    ((float(r["cal_total"])+float(r["cal_margin"]))/2.0,float(r["actual_home"])),
                    ((float(r["cal_total"])-float(r["cal_margin"]))/2.0,float(r["actual_away"])),
                ]
                edge_scores += [
                    (float(r["anch_home_edge"]),float(r["actual_home"])),
                    (float(r["anch_away_edge"]),float(r["actual_away"])),
                ]
                struct_scores += [
                    (float(r["anch_home_struct"]),float(r["actual_home"])),
                    (float(r["anch_away_struct"]),float(r["actual_away"])),
                ]

            return {
                "margin": {
                    "market": _summary(market_margin),
                    "football_lab": _summary(fl_margin),
                    "anchored_edge_only": _summary(edge_margin),
                    "anchored_structural": _summary(struct_margin),
                },
                "total": {
                    "market": _summary(market_total),
                    "football_lab": _summary(fl_total),
                    "anchored_edge_only": _summary(edge_total),
                    "anchored_structural": _summary(struct_total),
                },
                "score": {
                    "market_implied": _summary(market_scores),
                    "football_lab": _summary(fl_scores),
                    "anchored_edge_only": _summary(edge_scores),
                    "anchored_structural": _summary(struct_scores),
                },
                "directional": {
                    "margin_edge_only": _directional(group,"anch_margin_edge","market_margin","actual_margin"),
                    "margin_structural": _directional(group,"anch_margin_struct","market_margin","actual_margin"),
                    "total_edge_only": _directional(group,"anch_total_edge","market_total","actual_total"),
                    "total_structural": _directional(group,"anch_total_struct","market_total","actual_total"),
                },
            }

        pooled.extend(test)
        folds.append({
            "season": season,
            "train_games": len(train),
            "test_games": len(test),
            **metrics(test),
        })

    def pooled_metrics(group):
        if not group:
            return {}
        market_margin=[(float(r["market_margin"]),float(r["actual_margin"])) for r in group]
        fl_margin=[(float(r["cal_margin"]),float(r["actual_margin"])) for r in group]
        edge_margin=[(float(r["anch_margin_edge"]),float(r["actual_margin"])) for r in group]
        struct_margin=[(float(r["anch_margin_struct"]),float(r["actual_margin"])) for r in group]
        market_total=[(float(r["market_total"]),float(r["actual_total"])) for r in group]
        fl_total=[(float(r["cal_total"]),float(r["actual_total"])) for r in group]
        edge_total=[(float(r["anch_total_edge"]),float(r["actual_total"])) for r in group]
        struct_total=[(float(r["anch_total_struct"]),float(r["actual_total"])) for r in group]
        market_scores=[]; fl_scores=[]; edge_scores=[]; struct_scores=[]
        for r in group:
            market_scores += [
                (float(r["market_home_points"]),float(r["actual_home"])),
                (float(r["market_away_points"]),float(r["actual_away"])),
            ]
            fl_scores += [
                ((float(r["cal_total"])+float(r["cal_margin"]))/2.0,float(r["actual_home"])),
                ((float(r["cal_total"])-float(r["cal_margin"]))/2.0,float(r["actual_away"])),
            ]
            edge_scores += [
                (float(r["anch_home_edge"]),float(r["actual_home"])),
                (float(r["anch_away_edge"]),float(r["actual_away"])),
            ]
            struct_scores += [
                (float(r["anch_home_struct"]),float(r["actual_home"])),
                (float(r["anch_away_struct"]),float(r["actual_away"])),
            ]
        return {
            "margin":{
                "market":_summary(market_margin),
                "football_lab":_summary(fl_margin),
                "anchored_edge_only":_summary(edge_margin),
                "anchored_structural":_summary(struct_margin),
            },
            "total":{
                "market":_summary(market_total),
                "football_lab":_summary(fl_total),
                "anchored_edge_only":_summary(edge_total),
                "anchored_structural":_summary(struct_total),
            },
            "score":{
                "market_implied":_summary(market_scores),
                "football_lab":_summary(fl_scores),
                "anchored_edge_only":_summary(edge_scores),
                "anchored_structural":_summary(struct_scores),
            },
            "directional":{
                "margin_edge_only":_directional(group,"anch_margin_edge","market_margin","actual_margin"),
                "margin_structural":_directional(group,"anch_margin_struct","market_margin","actual_margin"),
                "total_edge_only":_directional(group,"anch_total_edge","market_total","actual_total"),
                "total_structural":_directional(group,"anch_total_struct","market_total","actual_total"),
            },
        }

    return {
        "version": MODEL_VERSION,
        "independent_forecast_preserved": True,
        "market_role": "separate anchor layer; never used in Football Lab base forecast",
        "architecture": "market implied score + walk-forward predicted football residual",
        "margin_edge_features": list(MARGIN_EDGE_ONLY),
        "margin_structural_features": list(MARGIN_STRUCTURAL),
        "total_edge_features": list(TOTAL_EDGE_ONLY),
        "total_structural_features": list(TOTAL_STRUCTURAL),
        "walk_forward": folds,
        "pooled": pooled_metrics(pooled),
    }
