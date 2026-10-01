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

from collections import defaultdict
from contextlib import closing
from typing import Any

from sports_aggregator.nfl.drive_projection import STATE_SEASON_DECAY
from sports_aggregator.nfl.margin_strength_ablation import _fit, _predict
from sports_aggregator.nfl.naming import canon_team
from sports_aggregator.nfl.qb_player_ablation import SHRUNK_CHANGE, QBTracker, build_rows
from sports_aggregator.nfl.qb_quality_projection import _qb_games
from sports_aggregator.nfl.repository import NFLRepository

MODEL_LABEL = "qb-aware-margin-v1"
UNAVAILABLE = frozenset({"O", "OUT", "IR", "PUP", "SUSP", "D", "DOUBTFUL"})


def fit(repository: NFLRepository, season: int):
    """Ridge margin model on rows from seasons before `season`, or None if too thin."""
    rows = build_rows(repository, 2010, max(2010, int(season) - 1))
    sample = [r for r in rows if r.get("actual_margin") is not None
              and all(r.get(k) is not None for k in SHRUNK_CHANGE)]
    return _fit(sample, SHRUNK_CHANGE)


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
        self.model = fit(repository, season)
        self.recent = _recent_margins(repository, season, week)
        self.tracker = QBTracker()
        by_week: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in _qb_games(repository, 2010, season):
            by_week[(int(row["season"]), int(row["week"]))].append(row)
        for key in sorted(by_week):
            if key < (self.season, self.week):
                self.tracker.update(by_week[key])
        with closing(repository._connect()) as connection:
            self.current_elo = {str(r["team"]): float(r["rating"]) for r in connection.execute(
                "SELECT team,rating FROM nfl_elo_ratings")}
            self.game_elo = {str(r["game_id"]): float(r["home_pre"]) - float(r["away_pre"])
                             for r in connection.execute(
                "SELECT game_id,home_pre,away_pre FROM nfl_elo_games WHERE season=? AND week=?",
                (self.season, self.week))}

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
        return {
            "margin": _predict(self.model, row),
            "model": MODEL_LABEL,
            "quarterbacks": {
                "home": {**hs, "changed": bool(hf["changed"])},
                "away": {**as_, "changed": bool(af["changed"])},
            },
        }
