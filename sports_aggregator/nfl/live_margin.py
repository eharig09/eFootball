"""Quarterback-aware margin model for the live Football Lab chain.

Promotes the stack validated in qb_player_ablation (core margin features + Elo +
recent margin + shrunk QB ratings and QB-change features). Training rows are the
same walk-forward rows the backtest used, restricted to seasons before the
target season. Features for an upcoming game are built from the same code the
backtest uses (QBTracker, the recent-margin decay), with one difference that
cannot be avoided: the backtest knows who actually played quarterback, while a
live game has to use the *expected* starter -- depth-chart QB1, skipping anyone
the current injury report lists as out. The report says which source it used.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import closing
import os
import threading
from typing import Any

import numpy as np

from sports_aggregator.nfl.distribution_calibration import MAX_SIGMA, MIN_SIGMA, _pmf, _tail
from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
from sports_aggregator.nfl.model_cache import history_cached
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, QBTracker, build_rows
from sports_aggregator.nfl.qb_quality_projection import _qb_games
from sports_aggregator.nfl.repository import NFLRepository

MODEL_LABEL = "qb-aware-margin-v1"
UNAVAILABLE = frozenset({"O", "OUT", "IR", "PUP", "SUSP", "D", "DOUBTFUL"})


def residual_sigma(sample: list[dict[str, Any]]) -> float | None:
    """Std of walk-forward (out-of-fold) margin residuals, clipped to a sane range."""
    residuals = []
    for season in sorted({int(r["season"]) for r in sample}):
        train = [r for r in sample if int(r["season"]) < season]
        model = _fit(train, SHRUNK_CHANGE)
        if model is None:
            continue
        residuals.extend(float(r["actual_margin"]) - _predict(model, r)
                         for r in sample if int(r["season"]) == season)
    if len(residuals) < 200:
        return None
    return float(min(MAX_SIGMA, max(MIN_SIGMA, np.std(residuals))))


#: The fit reads these modules' closure; hashing only them keeps this cache independent of the older model caches.
FIT_ROOTS = ("live_margin", "qb_player_ablation", "margin_strength_ablation", "distribution_calibration")
#: build_rows also reads quarterback profiles and Elo, which the shared history fingerprint does not cover.
FIT_FINGERPRINT = (
    "SELECT COUNT(*), ROUND(SUM(total_epa),3) FROM qb_pass_profiles WHERE season<=?",
    "SELECT COUNT(*), ROUND(SUM(home_pre),1) FROM nfl_elo_games WHERE season<=?",
)


@history_cached("live_margin_fit", roots=FIT_ROOTS, extra_queries=FIT_FINGERPRINT)
def fit_model(repository: NFLRepository, start_season: int, end_season: int):
    """(ridge margin model, residual sigma) from seasons start..end; (None, None) if thin.

    Rebuilding the historical feature rows takes seconds and depends only on completed seasons, so it is cached on disk
    and in memory (see model_cache) instead of being redone by every page that shows a forecast.
    """
    rows = build_rows(repository, start_season, end_season)
    sample = [r for r in rows if r.get("actual_margin") is not None
              and all(r.get(k) is not None for k in SHRUNK_CHANGE)]
    return _fit(sample, SHRUNK_CHANGE), residual_sigma(sample)


def fit(repository: NFLRepository, season: int):
    """(ridge margin model, residual sigma) from seasons before `season`; (None, None) if thin."""
    return fit_model(repository, 2010, max(2010, int(season) - 1))


# Pregame state for one (season, week): who the quarterbacks were and how each team has been priced so far. It changes
# only when a game completes, so it is shared between requests and retired by any database write.
_STATE: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
_STATE_LOCK = threading.Lock()
_STATE_LIMIT = 6


def _recent_margins(repository: NFLRepository, season: int, week: int) -> dict[str, float]:
    """Season-decayed mean margin per team over completed games before (season, week)."""
    records: dict[str, list[tuple[int, float]]] = defaultdict(list)
    with closing(repository._connect()) as connection:
        for g in connection.execute(
            """SELECT season,week,home_team,away_team,home_score,away_score FROM games
               WHERE season BETWEEN 2010 AND ? AND completed=1
                 AND home_score IS NOT NULL AND away_score IS NOT NULL""",
            (int(season),),
        ):
            if (int(g["season"]), int(g["week"])) >= (int(season), int(week)):
                continue
            m = float(g["home_score"]) - float(g["away_score"])
            records[str(g["home_team"])].append((int(g["season"]), m))
            records[str(g["away_team"])].append((int(g["season"]), -m))
    out = {}
    for team, recs in records.items():
        weights = [(STATE_SEASON_DECAY ** max(0, season - s), m) for s, m in recs]
        total = sum(w for w, _ in weights)
        if total:
            out[team] = sum(w * m for w, m in weights) / total
    return out


class LiveMargin:
    """Per-report context: fitted model plus pregame state for the target week."""

    def __init__(self, repository: NFLRepository, season: int, week: int) -> None:
        self.repository, self.season, self.week = repository, int(season), int(week)
        self.model, self.sigma = fit(repository, season)
        state = self._pregame_state(repository, self.season, self.week)
        self.recent, self.tracker = state["recent"], state["tracker"]
        self.current_elo, self.game_elo = state["current_elo"], state["game_elo"]

    @staticmethod
    def _pregame_state(repository: NFLRepository, season: int, week: int) -> dict[str, Any]:
        """Read-only after construction (the tracker is only asked for features), so it is shared, not copied."""
        key = (os.path.abspath(str(repository.path)), repository.data_stamp(), season, week)
        with _STATE_LOCK:
            hit = _STATE.get(key)
            if hit is not None:
                _STATE.move_to_end(key)
                return hit
        tracker = QBTracker()
        by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in _qb_games(repository, 2010, season):
            by_week[(int(row["season"]), int(row["week"]))].append(row)
        for week_key in sorted(by_week):
            if week_key < (season, week):
                tracker.update(by_week[week_key])
        with closing(repository._connect()) as connection:
            current_elo = {str(r["team"]): float(r["rating"]) for r in connection.execute(
                "SELECT team,rating FROM nfl_elo_ratings")}
            game_elo = {str(r["game_id"]): float(r["home_pre"]) - float(r["away_pre"])
                        for r in connection.execute(
                "SELECT game_id,home_pre,away_pre FROM nfl_elo_games WHERE season=? AND week=?", (season, week))}
        state = {"recent": _recent_margins(repository, season, week), "tracker": tracker,
                 "current_elo": current_elo, "game_elo": game_elo}
        with _STATE_LOCK:
            _STATE[key] = state
            while len(_STATE) > _STATE_LIMIT:
                _STATE.popitem(last=False)
        return state

    def _elo_diff(self, game: dict[str, Any]) -> float | None:
        if str(game["game_id"]) in self.game_elo:
            return self.game_elo[str(game["game_id"])]
        home = self.current_elo.get(canon_team(game["home_team"]))
        away = self.current_elo.get(canon_team(game["away_team"]))
        return None if home is None or away is None else home - away

    def expected_starter(self, team: str) -> dict[str, Any] | None:
        """Depth-chart QB1 not listed out; falls back to the last game's passer."""
        team = canon_team(team)
        depth = [r for r in self.repository.current_depth_chart(self.season, team)
                 if r.get("position_abbreviation") == "QB" and r.get("gsis_id")]
        out = {str(r["gsis_id"]) for r in self.repository.team_injuries(self.season, team)
               if str(r.get("designation") or r.get("status") or "").upper() in UNAVAILABLE}
        for row in sorted(depth, key=lambda r: int(r.get("position_rank") or 99)):
            if str(row["gsis_id"]) not in out:
                return {"qb_id": str(row["gsis_id"]), "name": row["player_name"],
                        "source": "depth_chart"}
        last = self.tracker.last_qb.get(team)
        return {"qb_id": last, "name": None, "source": "last_game"} if last else None

    def home_win_probability(self, margin: float) -> float | None:
        """Gaussian on integer margins with the out-of-fold residual sigma.

        distribution_calibration found this beat conditional-sigma, empirical and
        key-number variants out of sample (log loss 0.626 vs 0.609 for the market line).
        """
        if self.sigma is None:
            return None
        p = _tail(_pmf(float(margin), self.sigma), 0.0)
        return None if p is None else round(p, 4)

    def margin(self, game: dict[str, Any], game_row: dict[str, Any]) -> dict[str, Any] | None:
        """Prediction plus the inputs that drove it, or None if a feature is unavailable."""
        if self.model is None:
            return None
        home, away = str(game["home_team"]), str(game["away_team"])
        hs, as_ = self.expected_starter(home), self.expected_starter(away)
        elo = self._elo_diff(game)
        hr, ar = self.recent.get(home), self.recent.get(away)
        if not hs or not as_ or elo is None or hr is None or ar is None:
            return None
        hf = self.tracker.features(canon_team(home), hs["qb_id"], self.season)
        af = self.tracker.features(canon_team(away), as_["qb_id"], self.season)
        row = {
            **game_row,
            "elo_diff": elo,
            "recent_margin_diff": hr - ar,
            "sq_epa_diff": hf["epa"] - af["epa"],
            "sq_cpoe_diff": hf["cpoe"] - af["cpoe"],
            "sq_change_diff": hf["changed"] - af["changed"],
            "sq_drop_diff": af["drop"] - hf["drop"],
            "sq_exp_diff": hf["exp"] - af["exp"],
        }
        margin = _predict(self.model, row)
        return {
            "margin": margin,
            "margin_sigma": self.sigma,
            "home_win_probability": self.home_win_probability(margin),
            "model": MODEL_LABEL,
            "quarterbacks": {
                "home": {**hs, "changed": bool(hf["changed"])},
                "away": {**as_, "changed": bool(af["changed"])},
            },
        }
