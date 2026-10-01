"""NFL player-level quarterback state ablation for the calibrated margin.

The team-level QB features in margin_strength_ablation drop any game whose
quarterback has fewer than MIN_QB_ATTEMPTS prior attempts, which silently
removes rookies and backups -- the games where a quarterback change matters
most. This ablation replaces them with empirical-Bayes (shrunk) ratings that
exist for every quarterback, and adds explicit quarterback-change features.

Leak policy: ratings use only games from earlier week batches. The quarterback
for a game is the passer with the most attempts in that game, which can differ
from the announced starter after an in-game injury; that is a small known
leak, flagged in the report. No market/odds inputs are used.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl.margin_strength_ablation import (
    _elo_map,
    _fit,
    _predict,
    _recent_margin_map,
)
from sports_aggregator.nfl.qb_quality_projection import _pregame_qb_states, _qb_games
from sports_aggregator.nfl.repository import NFLRepository
from sports_aggregator.nfl.score_calibration import MARGIN_FEATURES, _game_rows

MODEL_VERSION = "nfl-qb-player-ablation-v1"
QB_SEASON_DECAY = 0.6
EPA_PRIOR_ATTEMPTS = 200.0
CPOE_PRIOR_ATTEMPTS = 300.0

BASE = MARGIN_FEATURES + ("elo_diff", "recent_margin_diff")
OLD_QB = BASE + ("qb_epa_diff", "qb_cpoe_diff")
SHRUNK = BASE + ("sq_epa_diff", "sq_cpoe_diff")
SHRUNK_CHANGE = SHRUNK + ("sq_change_diff", "sq_drop_diff", "sq_exp_diff")

FEATURE_SETS = (
    ("base", BASE),
    ("old_qb", OLD_QB),
    ("shrunk_qb", SHRUNK),
    ("shrunk_qb_change", SHRUNK_CHANGE),
)


def _rating(history: list[dict[str, Any]], season: int, decay: float,
            league_epa: float, league_cpoe: float) -> dict[str, float]:
    """Shrunk per-attempt EPA and CPOE for one quarterback's prior games."""
    att = epa = cpoe_n = cpoe = 0.0
    for r in history:
        w = decay ** max(0, season - int(r["season"]))
        att += w * float(r["attempts"] or 0)
        epa += w * float(r["total_epa"] or 0)
        cpoe_n += w * float(r["cpoe_plays"] or 0)
        cpoe += w * float(r["cpoe_total"] or 0)
    return {
        "epa": (epa + EPA_PRIOR_ATTEMPTS * league_epa) / (att + EPA_PRIOR_ATTEMPTS),
        "cpoe": (cpoe + CPOE_PRIOR_ATTEMPTS * league_cpoe) / (cpoe_n + CPOE_PRIOR_ATTEMPTS),
        "exp": math.log1p(att),
    }


class QBTracker:
    """Chronological quarterback state shared by the backtest and the live chain.

    Feed completed week batches through `update`; ask for a team's pregame
    features with `features` before the batch containing that game is applied.
    """

    def __init__(self, decay: float = QB_SEASON_DECAY) -> None:
        self.decay = decay
        self.history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.last_qb: dict[str, str] = {}
        self.tot = {"att": 0.0, "epa": 0.0, "cpoe_n": 0.0, "cpoe": 0.0}

    def _league(self) -> tuple[float, float]:
        t = self.tot
        return (t["epa"] / t["att"] if t["att"] else 0.0,
                t["cpoe"] / t["cpoe_n"] if t["cpoe_n"] else 0.0)

    def features(self, team: str, qb: str, season: int) -> dict[str, float]:
        league_epa, league_cpoe = self._league()
        now = _rating(self.history[qb], season, self.decay, league_epa, league_cpoe)
        prev = self.last_qb.get(team)
        changed = prev is not None and prev != qb
        drop = 0.0
        if changed:
            prior = _rating(self.history[prev], season, self.decay, league_epa, league_cpoe)
            drop = prior["epa"] - now["epa"]
        return {**now, "changed": 1.0 if changed else 0.0, "drop": drop}

    def update(self, batch: list[dict[str, Any]]) -> None:
        for row in batch:
            self.history[str(row["passer_player_id"])].append(row)
            self.last_qb[str(row["offense_team"])] = str(row["passer_player_id"])
            self.tot["att"] += float(row["attempts"] or 0)
            self.tot["epa"] += float(row["total_epa"] or 0)
            self.tot["cpoe_n"] += float(row["cpoe_plays"] or 0)
            self.tot["cpoe"] += float(row["cpoe_total"] or 0)


def _shrunk_states(repository: NFLRepository, start_season: int, end_season: int,
                   decay: float = QB_SEASON_DECAY):
    """Pregame (game_id, team) -> shrunk rating, change flag, and drop size."""
    games = _qb_games(repository, start_season, end_season)
    by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in games:
        by_week[(int(row["season"]), int(row["week"]))].append(row)

    tracker = QBTracker(decay)
    states: dict[tuple[str, str], dict[str, float]] = {}
    for season, week in sorted(by_week):
        current = by_week[(season, week)]
        for row in current:
            states[(str(row["game_id"]), str(row["offense_team"]))] = tracker.features(
                str(row["offense_team"]), str(row["passer_player_id"]), season)
        tracker.update(current)
    return states


def _errors(rows: list[dict[str, Any]], key: str) -> np.ndarray:
    return np.asarray([float(r[key]) - float(r["actual_margin"]) for r in rows])


def _summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    e = _errors(rows, key)
    return {
        "n": len(rows),
        "mae": round(float(np.abs(e).mean()), 4),
        "rmse": round(float(math.sqrt((e * e).mean())), 4),
        "bias": round(float(e.mean()), 4),
    }


def _paired(rows: list[dict[str, Any]], a: str, b: str) -> dict[str, Any]:
    """Paired t on absolute-error differences; negative mean_diff favours b."""
    if len(rows) < 30:
        return {"n": len(rows)}
    d = np.abs(_errors(rows, b)) - np.abs(_errors(rows, a))
    se = d.std(ddof=1) / math.sqrt(len(d))
    return {
        "n": len(rows),
        "mean_abs_err_diff": round(float(d.mean()), 4),
        "t": round(float(d.mean() / se), 3) if se else None,
    }


def build_rows(repository: NFLRepository, start_season: int, end_season: int,
               decay: float = QB_SEASON_DECAY) -> list[dict[str, Any]]:
    """Game rows carrying the core, Elo, recent-margin and QB feature stack."""
    games = _game_rows(repository, start_season=start_season, end_season=end_season)
    elo = _elo_map(repository, start_season, end_season)
    recent = _recent_margin_map(repository, start_season, end_season)
    old_qb = _pregame_qb_states(repository, start_season, end_season)
    shrunk = _shrunk_states(repository, start_season, end_season, decay)
    with closing(repository._connect()) as connection:
        teams = {
            str(x["game_id"]): (str(x["home_team"]), str(x["away_team"]))
            for x in connection.execute(
                "SELECT game_id,home_team,away_team FROM games WHERE season BETWEEN ? AND ?",
                (int(start_season), int(end_season)),
            )
        }

    for r in games:
        gid = str(r["game_id"])
        if gid not in teams:
            continue
        home, away = teams[gid]
        r["elo_diff"] = elo.get(gid)
        hr, ar = recent.get((gid, home)), recent.get((gid, away))
        r["recent_margin_diff"] = hr - ar if hr is not None and ar is not None else None
        ho, ao = old_qb.get((gid, home)), old_qb.get((gid, away))
        if ho and ao:
            r["qb_epa_diff"] = float(ho["qb_epa_per_attempt"]) - float(ao["qb_epa_per_attempt"])
            r["qb_cpoe_diff"] = float(ho["qb_cpoe"]) - float(ao["qb_cpoe"])
        hs, as_ = shrunk.get((gid, home)), shrunk.get((gid, away))
        if hs and as_:
            r["sq_epa_diff"] = hs["epa"] - as_["epa"]
            r["sq_cpoe_diff"] = hs["cpoe"] - as_["cpoe"]
            r["sq_change_diff"] = hs["changed"] - as_["changed"]
            r["sq_drop_diff"] = as_["drop"] - hs["drop"]  # positive = home lost less
            r["sq_exp_diff"] = hs["exp"] - as_["exp"]
            r["any_qb_change"] = bool(hs["changed"] or as_["changed"])
        r["home_team"], r["away_team"] = home, away
    return games


def report(repository: NFLRepository, *, start_season=2010, end_season=2025,
           decay: float = QB_SEASON_DECAY):
    games = build_rows(repository, start_season, end_season, decay)

    needed = set().union(*(set(f) for _, f in FEATURE_SETS))
    full = [r for r in games if r.get("actual_margin") is not None
            and all(r.get(k) is not None for k in needed)]
    # Wider sample that the old QB features cannot reach.
    new_needed = set(SHRUNK_CHANGE)
    wide = [r for r in games if r.get("actual_margin") is not None
            and all(r.get(k) is not None for k in new_needed)]

    def walk(sample, sets):
        pooled, folds = [], []
        for season in sorted({int(r["season"]) for r in sample}):
            train = [r for r in sample if int(r["season"]) < season]
            test = [dict(r) for r in sample if int(r["season"]) == season]
            if len(train) < 100 or not test:
                continue
            models = {label: _fit(train, f) for label, f in sets}
            for r in test:
                for label, m in models.items():
                    r[f"pred_{label}"] = _predict(m, r)
            pooled.extend(test)
            folds.append({"season": season, "test_games": len(test), "models": {
                label: _summary(test, f"pred_{label}") for label, _ in sets}})
        return pooled, folds

    pooled, folds = walk(full, FEATURE_SETS)
    wide_sets = tuple(s for s in FEATURE_SETS if s[0] != "old_qb")
    wide_pooled, wide_folds = walk(wide, wide_sets)
    changed = [r for r in wide_pooled if r.get("any_qb_change")]

    return {
        "version": MODEL_VERSION,
        "market_used": False,
        "qb_season_decay": decay,
        "prior_attempts": {"epa": EPA_PRIOR_ATTEMPTS, "cpoe": CPOE_PRIOR_ATTEMPTS},
        "leakage_note": "QB = most-attempts passer in the game; in-game injury "
                        "replacements can leak a little. Ratings use prior week batches only.",
        "raw_games": len(games),
        "common_games": len(full),
        "wide_games": len(wide),
        "common_sample": {
            "pooled": {l: _summary(pooled, f"pred_{l}") for l, _ in FEATURE_SETS},
            "paired_vs_base": {l: _paired(pooled, "pred_base", f"pred_{l}")
                               for l, _ in FEATURE_SETS if l != "base"},
            "paired_change_vs_old_qb": _paired(pooled, "pred_old_qb", "pred_shrunk_qb_change"),
            "walk_forward": folds,
        },
        "wide_sample": {
            "pooled": {l: _summary(wide_pooled, f"pred_{l}") for l, _ in wide_sets},
            "paired_vs_base": {l: _paired(wide_pooled, "pred_base", f"pred_{l}")
                               for l, _ in wide_sets if l != "base"},
            "qb_change_games": {
                "pooled": {l: _summary(changed, f"pred_{l}") for l, _ in wide_sets},
                "paired_vs_base": {l: _paired(changed, "pred_base", f"pred_{l}")
                                   for l, _ in wide_sets if l != "base"},
            },
            "walk_forward": wide_folds,
        },
    }
