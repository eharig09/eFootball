"""Does a team's style, against the opponent's style, say anything the opponent-adjusted ratings do not?

Baseline: expected points for each side from the opponent-adjusted points ratings (team_styles), re-solved weekly from
earlier games. That already adjusts for opponent quality the way Margin Power does, so any style effect has to show up
as a residual on top of it.

Style adjustment: each game falls in "cells" built from the two teams' categories, and a cell's adjustment is the
shrunk mean of the points residuals (actual - baseline) of earlier games in that cell. Learned online, one game at a
time in calendar order, so a game is never scored with its own result.

  matchup    offence approach x opposing defence vulnerability      (points for the offence)
  bigplay    offence explosiveness x opposing defence big plays     (points for the offence)
  tempo      the two teams' tempo categories, unordered             (game total, split across both sides)

Scored against the closing line and the Margin Power forecast, on the same games. The market is a benchmark only.
Run: python -m sports_aggregator.nfl.style_matchup
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

import numpy as np

from sports_aggregator.nfl import team_styles
from sports_aggregator.nfl.repository import NFLRepository

MODEL_VERSION = "nfl-style-matchup-v1"
CELL_K = 40.0          # pseudo-observations pulling a cell's mean toward zero


class Cells:
    """Shrunk running means of residuals by cell key."""

    def __init__(self, k: float = CELL_K):
        self.k, self.sum, self.n = k, defaultdict(float), defaultdict(int)

    def adjustment(self, key) -> float:
        return self.sum[key] / (self.n[key] + self.k)

    def add(self, key, residual: float) -> None:
        self.sum[key] += residual
        self.n[key] += 1


def _pair_games(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_game[r["game_id"]].append(r)
    games = []
    for game_id, sides in by_game.items():
        if len(sides) != 2:
            continue
        home = next((s for s in sides if s["is_home"]), None)
        away = next((s for s in sides if not s["is_home"]), None)
        if home and away:
            games.append({"game_id": game_id, "season": home["season"], "week": home["week"], "home": home,
                          "away": away, "neutral": bool(home["neutral_site"])})
    games.sort(key=lambda g: (g["season"], g["week"], g["game_id"]))
    return games


def _expected_points(points: dict[str, Any], team: str, opp: str, home_col: float) -> float:
    return points["mu"] + points["home"] * home_col + points["off"][team] + points["def"][opp]


def run(repository: NFLRepository, start: int = 2013, end: int = 2025) -> list[dict[str, Any]]:
    """One row per scored game with the baseline, each style adjustment, and the outcome and market."""
    rows = team_styles.load_observations(repository, start - 1, end)
    snaps = team_styles.snapshots(rows, start)
    games = _pair_games([r for r in rows if r["season"] >= start])
    matchup, bigplay, tempo = Cells(), Cells(), Cells()
    market = _market(repository)
    out, pending = [], []
    current = None
    for g in games:
        key = (g["season"], g["week"])
        if key != current:                          # a week's games are scored before any of them is learned from
            for item in pending:
                item()
            pending, current = [], key
        snap = snaps.get(key)
        if not snap or not snap["points"]["off"]:
            continue
        labels = team_styles.classify(snap)
        h, a = g["home"]["team"], g["away"]["team"]
        base_h = _expected_points(snap["points"], h, a, 0.0 if g["neutral"] else 0.5)
        base_a = _expected_points(snap["points"], a, h, 0.0 if g["neutral"] else -0.5)
        cell = {}
        for side, own, opp in (("h", h, a), ("a", a, h)):
            cell[side] = {"matchup": (labels[own]["approach"], labels[opp]["vulnerability"]),
                          "bigplay": (labels[own]["explosive"], labels[opp]["big_plays"])}
        tempo_key = tuple(sorted((labels[h]["tempo"], labels[a]["tempo"])))
        adj = {name: {s: cells.adjustment(cell[s][name]) for s in ("h", "a")}
               for name, cells in (("matchup", matchup), ("bigplay", bigplay))}
        tempo_adj = tempo.adjustment(tempo_key)
        hs, as_ = float(g["home"]["points"]), float(g["away"]["points"])
        line = market.get(g["game_id"], (None, None))
        out.append({
            "game_id": g["game_id"], "season": g["season"], "week": g["week"], "home": h, "away": a,
            "base_h": base_h, "base_a": base_a,
            "adj_matchup": adj["matchup"]["h"] - adj["matchup"]["a"],
            "adj_bigplay": adj["bigplay"]["h"] - adj["bigplay"]["a"],
            "adj_tempo_total": tempo_adj,
            "adj_matchup_total": adj["matchup"]["h"] + adj["matchup"]["a"],
            "adj_bigplay_total": adj["bigplay"]["h"] + adj["bigplay"]["a"],
            "actual_margin": hs - as_, "actual_total": hs + as_,
            "spread": line[0], "total_line": line[1],
            "labels_h": labels[h], "labels_a": labels[a],
        })

        def learn(g=g, cell=cell, tempo_key=tempo_key, base_h=base_h, base_a=base_a, hs=hs, as_=as_):
            for side, base, actual in (("h", base_h, hs), ("a", base_a, as_)):
                matchup.add(cell[side]["matchup"], actual - base)
                bigplay.add(cell[side]["bigplay"], actual - base)
            tempo.add(tempo_key, (hs + as_) - (base_h + base_a))
        pending.append(learn)
    return out


def _market(repository: NFLRepository) -> dict[str, tuple[float | None, float | None]]:
    from contextlib import closing
    with closing(repository._connect()) as connection:
        return {str(r["game_id"]): (r["spread_line"], r["total_line"])
                for r in connection.execute("SELECT game_id,spread_line,total_line FROM games")}


def _mae(errors) -> float:
    return float(np.mean(np.abs(errors)))


def _t(values: np.ndarray) -> float:
    return float(values.mean() / (values.std(ddof=1) / math.sqrt(len(values)))) if len(values) > 2 else float("nan")


def report(rows: list[dict[str, Any]], margin_power: dict[str, dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    priced = [r for r in rows if r["spread"] is not None and r["total_line"] is not None]
    lines.append(f"{len(rows)} games scored, {len(priced)} with a closing line. Seasons {rows[0]['season']}-{rows[-1]['season']}.")

    def block(title: str, sample: list[dict[str, Any]]) -> None:
        if not sample:
            return
        am = np.array([r["actual_margin"] for r in sample])
        at = np.array([r["actual_total"] for r in sample])
        base_m = np.array([r["base_h"] - r["base_a"] for r in sample])
        base_t = np.array([r["base_h"] + r["base_a"] for r in sample])
        adj_m = {k: np.array([r[f"adj_{k}"] for r in sample]) for k in ("matchup", "bigplay")}
        lines.append(f"\n{title} (n={len(sample)})")
        lines.append("  margin MAE  baseline %.3f | +matchup %.3f | +bigplay %.3f | +both %.3f" % (
            _mae(am - base_m), _mae(am - base_m - adj_m["matchup"]), _mae(am - base_m - adj_m["bigplay"]),
            _mae(am - base_m - adj_m["matchup"] - adj_m["bigplay"])))
        if any(r["spread"] is not None for r in sample):
            spread = np.array([r["spread"] if r["spread"] is not None else np.nan for r in sample])
            ok = ~np.isnan(spread)
            lines.append("  vs market   spread MAE %.3f  (baseline %.3f on the same games)" % (
                _mae(am[ok] - spread[ok]), _mae(am[ok] - base_m[ok])))
        tot_adj = {"matchup": np.array([r["adj_matchup_total"] for r in sample]),
                   "bigplay": np.array([r["adj_bigplay_total"] for r in sample]),
                   "tempo": np.array([r["adj_tempo_total"] for r in sample])}
        lines.append("  total  MAE  baseline %.3f | +matchup %.3f | +bigplay %.3f | +tempo %.3f | +all %.3f" % (
            _mae(at - base_t), _mae(at - base_t - tot_adj["matchup"]), _mae(at - base_t - tot_adj["bigplay"]),
            _mae(at - base_t - tot_adj["tempo"]), _mae(at - base_t - sum(tot_adj.values()))))
        if any(r["total_line"] is not None for r in sample):
            tl = np.array([r["total_line"] if r["total_line"] is not None else np.nan for r in sample])
            ok = ~np.isnan(tl)
            lines.append("  vs market   total MAE %.3f  (baseline %.3f on the same games)" % (
                _mae(at[ok] - tl[ok]), _mae(at[ok] - base_t[ok])))

    block("All scored games", rows)
    block("Priced games", priced)
    block("2013-2019 (priced)", [r for r in priced if r["season"] < 2020])
    block("2020-2025 (priced)", [r for r in priced if r["season"] >= 2020])

    # does a style adjustment carry information about the margin residual, game by game?
    lines.append("\nDo the adjustments predict what the baseline missed? (slope of residual on adjustment; 1.0 = fully right)")
    for name in ("matchup", "bigplay"):
        x = np.array([r[f"adj_{name}"] for r in rows])
        y = np.array([r["actual_margin"] - (r["base_h"] - r["base_a"]) for r in rows])
        slope = float(np.dot(x - x.mean(), y - y.mean()) / np.dot(x - x.mean(), x - x.mean()))
        resid = y - slope * (x - x.mean())
        se = float(resid.std(ddof=2) / (x.std() * math.sqrt(len(x))))
        lines.append(f"  {name:<8} slope {slope:+.2f} (se {se:.2f}, t {slope / se:+.2f})  adj sd {x.std():.2f} pts")
    for name in ("matchup_total", "bigplay_total", "tempo_total"):
        x = np.array([r[f"adj_{name}"] for r in rows])
        y = np.array([r["actual_total"] - (r["base_h"] + r["base_a"]) for r in rows])
        slope = float(np.dot(x - x.mean(), y - y.mean()) / np.dot(x - x.mean(), x - x.mean()))
        resid = y - slope * (x - x.mean())
        se = float(resid.std(ddof=2) / (x.std() * math.sqrt(len(x))))
        lines.append(f"  {name:<14} slope {slope:+.2f} (se {se:.2f}, t {slope / se:+.2f})  adj sd {x.std():.2f} pts")
    return "\n".join(lines)


if __name__ == "__main__":
    repo = NFLRepository("instance/nfl.sqlite3")
    print(report(run(repo)))
