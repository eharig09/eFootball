"""NFL market residual leverage factor ablation.

Tests which individual Football Lab components, if any, add stable predictive
value to market-implied margin/total. The independent Football Lab forecast is
preserved; Vegas is used only in this separate anchor layer.

Each model predicts actual-minus-market residual using prior seasons only.
All variants are compared on the same walk-forward sample.
"""
from __future__ import annotations

import math
from typing import Any
import numpy as np

from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.market_anchor_leverage import _prepare
from sports_aggregator.nfl.score_calibration import RIDGE_ALPHA

MODEL_VERSION = "nfl-market-leverage-ablation-v1"

MARGIN_VARIANTS = {
    "market_only": (),
    "fl_edge": ("fl_margin_edge",),
    "drive_diff": ("diff_pred_drives",),
    "plays_per_drive_diff": ("diff_pred_plays_per_drive",),
    "pass_rate_diff": ("diff_pred_pass_rate",),
    "pass_epa_diff": ("diff_pred_pass_epa",),
    "rush_epa_diff": ("diff_pred_rush_epa",),
    "combined_epa_diff": ("diff_pred_combined_epa",),
    "market_context": ("abs_market_margin","market_total"),
    "edge_plus_pass_epa": ("fl_margin_edge","diff_pred_pass_epa"),
    "edge_plus_drives": ("fl_margin_edge","diff_pred_drives"),
    "edge_plus_combined_epa": ("fl_margin_edge","diff_pred_combined_epa"),
}

TOTAL_VARIANTS = {
    "market_only": (),
    "fl_edge": ("fl_total_edge",),
    "sum_drives": ("sum_pred_drives",),
    "sum_plays": ("sum_pred_total_plays",),
    "sum_combined_epa": ("sum_pred_combined_epa",),
    "market_context": ("market_total","abs_market_margin"),
    "edge_plus_drives": ("fl_total_edge","sum_pred_drives"),
    "edge_plus_plays": ("fl_total_edge","sum_pred_total_plays"),
    "edge_plus_combined_epa": ("fl_total_edge","sum_pred_combined_epa"),
}


def _fit(rows: list[dict[str, Any]], features: tuple[str, ...], target: str):
    if not features:
        vals=[float(r[target]) for r in rows if r.get(target) is not None]
        if len(vals)<100:
            return None
        return {"features": (), "intercept": float(np.mean(vals)), "n": len(vals)}
    eligible=[
        r for r in rows
        if r.get(target) is not None and all(r.get(k) is not None for k in features)
    ]
    if len(eligible)<100:
        return None
    x=np.asarray([[r[k] for k in features] for r in eligible],dtype=float)
    y=np.asarray([r[target] for r in eligible],dtype=float)
    means=x.mean(axis=0)
    scales=x.std(axis=0)
    scales[scales==0]=1.0
    z=(x-means)/scales
    design=np.column_stack([np.ones(len(z)),z])
    penalty=np.eye(design.shape[1])*RIDGE_ALPHA
    penalty[0,0]=0.0
    beta=np.linalg.solve(design.T@design+penalty,design.T@y)
    return {"features":features,"means":means,"scales":scales,"beta":beta,"n":len(eligible)}


def _predict(model,row):
    if not model["features"]:
        return float(model["intercept"])
    x=np.asarray([row[k] for k in model["features"]],dtype=float)
    z=(x-model["means"])/model["scales"]
    return float(model["beta"][0]+z@model["beta"][1:])


def _summary(vals):
    if not vals:
        return {"n":0}
    e=[p-a for p,a in vals]
    ae=[abs(x) for x in e]
    return {
        "n":len(vals),
        "mae":round(sum(ae)/len(ae),4),
        "rmse":round(math.sqrt(sum(x*x for x in e)/len(e)),4),
        "bias":round(sum(e)/len(e),4),
    }


def _directional(rows,pred_key,market_key,actual_key):
    wins=losses=pushes=0
    for r in rows:
        edge=float(r[pred_key])-float(r[market_key])
        realized=float(r[actual_key])-float(r[market_key])
        if abs(realized)<1e-12:
            pushes+=1
        elif edge*realized>0:
            wins+=1
        else:
            losses+=1
    decided=wins+losses
    return {
        "wins":wins,
        "losses":losses,
        "pushes":pushes,
        "decided":decided,
        "win_rate":round(wins/decided,4) if decided else None,
    }


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

        margin_models={k:_fit(train,v,"margin_market_residual") for k,v in MARGIN_VARIANTS.items()}
        total_models={k:_fit(train,v,"total_market_residual") for k,v in TOTAL_VARIANTS.items()}
        if any(v is None for v in margin_models.values()) or any(v is None for v in total_models.values()):
            continue

        for r in test:
            for label,model in margin_models.items():
                r[f"pred_margin_{label}"]=float(r["market_margin"])+_predict(model,r)
            for label,model in total_models.items():
                r[f"pred_total_{label}"]=float(r["market_total"])+_predict(model,r)

        pooled.extend(test)

        margin_results={}
        for label in MARGIN_VARIANTS:
            vals=[(float(r[f"pred_margin_{label}"]),float(r["actual_margin"])) for r in test]
            margin_results[label]={
                **_summary(vals),
                "directional":_directional(test,f"pred_margin_{label}","market_margin","actual_margin"),
            }

        total_results={}
        for label in TOTAL_VARIANTS:
            vals=[(float(r[f"pred_total_{label}"]),float(r["actual_total"])) for r in test]
            total_results[label]={
                **_summary(vals),
                "directional":_directional(test,f"pred_total_{label}","market_total","actual_total"),
            }

        folds.append({
            "season":season,
            "train_games":len(train),
            "test_games":len(test),
            "margin":margin_results,
            "total":total_results,
        })

    pooled_margin={}
    for label in MARGIN_VARIANTS:
        vals=[(float(r[f"pred_margin_{label}"]),float(r["actual_margin"])) for r in pooled]
        pooled_margin[label]={
            **_summary(vals),
            "directional":_directional(pooled,f"pred_margin_{label}","market_margin","actual_margin"),
        }

    pooled_total={}
    for label in TOTAL_VARIANTS:
        vals=[(float(r[f"pred_total_{label}"]),float(r["actual_total"])) for r in pooled]
        pooled_total[label]={
            **_summary(vals),
            "directional":_directional(pooled,f"pred_total_{label}","market_total","actual_total"),
        }

    return {
        "version":MODEL_VERSION,
        "market_role":"anchor baseline only; independent Football Lab preserved separately",
        "margin_variants":{k:list(v) for k,v in MARGIN_VARIANTS.items()},
        "total_variants":{k:list(v) for k,v in TOTAL_VARIANTS.items()},
        "walk_forward":folds,
        "pooled":{
            "margin":pooled_margin,
            "total":pooled_total,
        },
    }
