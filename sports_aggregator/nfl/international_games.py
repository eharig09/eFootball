"""How do NFL games played abroad behave, relative to the market and to ordinary games?

The designated "home" team in an international game plays at a neutral site, so it should not be given the usual
home-field bump. This module measures, from the nflverse schedule (the only table that still carries the Neutral
flag), four things on completed regular-season games since 2010:

1. Home edge. Mean margin, home win rate and cover rate of the designated home team abroad vs. at home.
2. Totals. Actual total minus the closing total, and over rate, abroad vs. at home -- the question is whether the
   market totals are biased for these games.
3. Venues. The same two numbers by city, with the small samples shown as small.
4. Teams. Each team's margin abroad vs. its own margin in its other games that season.

Nothing here is a model feature; the closing line is a benchmark. Run: python -m sports_aggregator.nfl.international_games
"""
from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCHEDULES = Path(__file__).resolve().parents[2] / "instance" / "nflverse_raw" / "schedules.parquet"
START_SEASON = 2010
HOME_FIELD = 2.0       # what margin_power / line_elo add for every game today

CITY = {
    "Wembley Stadium": "London", "Tottenham Stadium": "London", "Tottenham Hotspur Stadium": "London",
    "Twickenham Stadium": "London",
    "Azteca Stadium": "Mexico City", "Estadio Banorte": "Mexico City",
    "Allianz Arena": "Germany", "Deutsche Bank Park": "Germany", "FC Bayern Munich Stadium": "Germany",
    "Arena Corinthians": "Brazil", "Maracana Stadium": "Brazil",
    "Bernabeu": "Spain", "Stade de France": "France", "Melbourne Cricket Ground": "Australia",
    "Rogers Centre": "Toronto",
}


def load(path: Path = SCHEDULES) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df[(df.season >= START_SEASON) & (df.game_type == "REG")].copy()
    df = df[df.home_score.notna() & df.away_score.notna()]
    df["city"] = df.stadium.map(CITY)
    # Toronto games before 2013 are flagged Home (the Bills sold the date); only the Neutral one is a true neutral site.
    df["abroad"] = df.city.notna() & (df.location == "Neutral")
    df["margin"] = df.home_score - df.away_score
    df["ats"] = df.margin - df.spread_line            # > 0: designated home team covered
    df["total_miss"] = (df.home_score + df.away_score) - df.total_line   # > 0: over
    return df


def _t(values: np.ndarray) -> tuple[float, float, float]:
    """mean, standard error, t statistic (against zero)."""
    values = values[~np.isnan(values)]
    if len(values) < 2:
        return float("nan"), float("nan"), float("nan")
    mean, se = float(values.mean()), float(values.std(ddof=1) / math.sqrt(len(values)))
    return mean, se, mean / se if se else float("nan")


def _binom_p(successes: int, n: int) -> float:
    """Two-sided exact binomial p against 0.5."""
    if n == 0:
        return float("nan")
    tail = sum(math.comb(n, k) for k in range(0, min(successes, n - successes) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _cover(series: pd.Series) -> tuple[int, int]:
    decided = series.dropna()
    decided = decided[decided != 0]
    return int((decided > 0).sum()), len(decided)


def summarize(df: pd.DataFrame, label: str) -> dict[str, Any]:
    margin, margin_se, _ = _t(df.margin.to_numpy(float))
    ats_mean, ats_se, ats_t = _t(df.ats.to_numpy(float))
    tot_mean, tot_se, tot_t = _t(df.total_miss.to_numpy(float))
    covers, covers_n = _cover(df.ats)
    overs, overs_n = _cover(df.total_miss)
    return {
        "label": label, "games": len(df),
        "home_margin": margin, "home_margin_se": margin_se,
        "home_win_pct": float(((df.margin > 0) + 0.5 * (df.margin == 0)).mean()) if len(df) else float("nan"),
        "ats_resid": ats_mean, "ats_se": ats_se, "ats_t": ats_t,
        "home_cover": f"{covers}-{covers_n - covers}", "home_cover_pct": covers / covers_n if covers_n else float("nan"),
        "cover_p": _binom_p(covers, covers_n),
        "avg_total": float((df.home_score + df.away_score).mean()) if len(df) else float("nan"),
        "total_miss": tot_mean, "total_se": tot_se, "total_t": tot_t,
        "over": f"{overs}-{overs_n - overs}", "over_pct": overs / overs_n if overs_n else float("nan"),
        "over_p": _binom_p(overs, overs_n),
    }


def team_margins(df: pd.DataFrame) -> pd.DataFrame:
    """One row per team-game, with the team's margin and whether the game was abroad."""
    home = df.assign(team=df.home_team, margin_t=df.margin, side="designated_home")
    away = df.assign(team=df.away_team, margin_t=-df.margin, side="away")
    return pd.concat([home, away], ignore_index=True)[
        ["game_id", "season", "week", "team", "margin_t", "side", "abroad", "city", "location"]]


def team_vs_self(df: pd.DataFrame) -> pd.DataFrame:
    """Each team's margin abroad minus its mean margin in its other regular-season games that year."""
    games = team_margins(df)
    rows = []
    for (season, team), group in games.groupby(["season", "team"]):
        abroad, other = group[group.abroad], group[~group.abroad]
        if abroad.empty or other.empty:
            continue
        for _, game in abroad.iterrows():
            rows.append({"season": season, "team": team, "city": game.city, "side": game.side,
                         "margin": game.margin_t, "other_mean": float(other.margin_t.mean()),
                         "delta": game.margin_t - float(other.margin_t.mean())})
    return pd.DataFrame(rows)


def report(df: pd.DataFrame | None = None) -> str:
    df = load() if df is None else df
    abroad, domestic = df[df.abroad], df[(df.location == "Home") & ~df.abroad]
    lines = [f"International (neutral, non-US) regular-season games {START_SEASON}+: {len(abroad)} "
             f"of {len(df)} completed. Domestic home games: {len(domestic)}.", ""]

    def fmt(rows: list[dict[str, Any]]) -> list[str]:
        out = [f"{'group':<26}{'n':>4} {'home mgn':>9} {'home win':>9} {'ATS resid':>10} {'cover':>8} {'p':>6}"
               f" {'avg tot':>8} {'tot-line':>9} {'t':>6} {'over':>8} {'p':>6}"]
        for r in rows:
            out.append(f"{r['label']:<26}{r['games']:>4} {r['home_margin']:>+9.2f} {r['home_win_pct']:>9.1%} "
                       f"{r['ats_resid']:>+10.2f} {r['home_cover']:>8} {r['cover_p']:>6.2f} {r['avg_total']:>8.1f} "
                       f"{r['total_miss']:>+9.2f} {r['total_t']:>6.2f} {r['over']:>8} {r['over_p']:>6.2f}")
        return out

    lines += ["1/2. Designated home team and totals, abroad vs. ordinary home games"]
    lines += fmt([summarize(domestic, "domestic home games"), summarize(abroad, "abroad (all)")])
    lines += ["", "     By era (abroad)"]
    lines += fmt([summarize(abroad[abroad.season < 2020], "abroad 2010-2019"),
                  summarize(abroad[abroad.season >= 2020], "abroad 2020+"),
                  summarize(domestic[domestic.season < 2020], "domestic 2010-2019"),
                  summarize(domestic[domestic.season >= 2020], "domestic 2020+")])
    lines += ["", "3. By venue city"]
    lines += fmt([summarize(g, city) for city, g in sorted(abroad.groupby("city"), key=lambda kv: -len(kv[1]))])
    early = abroad[abroad.gametime == "09:30"]
    lines += ["", "     By kickoff (local London/Europe 9:30 ET slot vs. the rest)"]
    lines += fmt([summarize(early, "9:30 ET kickoff"), summarize(abroad[abroad.gametime != "09:30"], "other kickoffs")])

    own = team_vs_self(df)
    lines += ["", "4. Each team's margin abroad vs. its own margin in other games that season"]
    for side in ("designated_home", "away"):
        part = own[own.side == side]
        mean, se, t = _t(part.delta.to_numpy(float))
        lines.append(f"   {side:<16} n={len(part):>3}  mean delta {mean:+.2f} (se {se:.2f}, t {t:.2f})")
    mean, se, t = _t(own.delta.to_numpy(float))
    lines.append(f"   {'all abroad':<16} n={len(own):>3}  mean delta {mean:+.2f} (se {se:.2f}, t {t:.2f})")
    by_team: dict[str, list[float]] = defaultdict(list)
    for _, r in own.iterrows():
        by_team[r.team].append(r.delta)
    lines += ["   By team (>=3 games abroad):"]
    for team, deltas in sorted(by_team.items(), key=lambda kv: -len(kv[1])):
        if len(deltas) >= 3:
            lines.append(f"     {team:<4} n={len(deltas):>2}  mean delta {np.mean(deltas):+6.2f}")

    # What the models assume vs. what the data shows.
    lines += ["", f"Models add HOME_FIELD={HOME_FIELD:+.1f} to every designated home team; abroad the observed mean margin is "
              f"{summarize(abroad, '')['home_margin']:+.2f} (domestic {summarize(domestic, '')['home_margin']:+.2f})."]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
