"""The style-clash panel: each team's style set against the opponent's, and the opponent-adjusted expectation.

Built from team_styles ratings (offence and defence rated separately, adjusted for the opposition faced, pregame).
For each direction -- team A's offence against team B's defence -- every metric shows what A's offence does against
an average defence, what B's defence allows to an average offence, and what that predicts for this pairing.

This is description plus an opponent-adjusted projection. A backtest of category-by-category adjustments on top of
these ratings (2013-2025) found nothing beyond the ratings themselves, so the categories label the matchup and are
never used to move a number.
"""
from __future__ import annotations

from typing import Any

from sports_aggregator.nfl import team_styles
from sports_aggregator.nfl.repository import NFLRepository

# key, label, format, offence rank direction (True: higher is better for the offence), is a quality metric
ROWS = (
    ("points", "Points", "f1", True, True),
    ("pass_epa", "Pass EPA / play", "signed2", True, True),
    ("rush_epa", "Rush EPA / play", "signed2", True, True),
    ("explosive", "Explosive play rate", "rate", True, True),
    ("pass_rate", "Neutral pass rate", "rate", True, False),
    ("pace", "Seconds / play", "f1", False, False),
)
#: A tempo or pass-rate value is a style, not good or bad; the rank column then reads "most" / "fastest".
STYLE_NOTE = {"pass_rate": "1 = most pass-heavy", "pace": "1 = fastest"}
MIN_GAMES_SETTLED = 4


def _rank(values: dict[str, float], team: str, descending: bool) -> int:
    ordered = sorted(values, key=lambda t: values[t], reverse=descending)
    return ordered.index(team) + 1


def alignment(offence: dict[str, str], defence: dict[str, str]) -> str:
    """Whether the offence's lean is the thing this defence is weakest against."""
    approach, weak = offence["approach"], defence["vulnerability"]
    if (approach, weak) in {("Pass-heavy", "Pass-vulnerable"), ("Run-heavy", "Run-vulnerable")}:
        return "Fits"
    if (approach, weak) in {("Pass-heavy", "Run-vulnerable"), ("Run-heavy", "Pass-vulnerable")}:
        return "Against the grain"
    return "Neutral"


def _direction(snap: dict[str, dict[str, Any]], labels: dict[str, dict[str, str]],
               offence: str, defence: str, home_col: float) -> dict[str, Any]:
    rows = []
    for key, label, fmt, high_is_good, quality in ROWS:
        rating = snap[key]
        mu = rating["mu"]
        off_value, def_value = mu + rating["off"][offence], mu + rating["def"][defence]
        expected = mu + rating["off"][offence] + rating["def"][defence]
        if key == "points":
            expected += rating["home"] * home_col
        # Quality metrics rank "best first" on both sides; style metrics rank by how much of the thing a side does.
        off_rank = _rank(rating["off"], offence, high_is_good)
        # best defence allows the least; for a style metric, rank by how much of it the defence forces
        def_rank = _rank(rating["def"], defence, False if quality else high_is_good)
        edge = expected - mu
        rows.append({
            "key": key, "label": label, "format": fmt, "quality": quality,
            "off_value": off_value, "off_rank": off_rank, "def_value": def_value, "def_rank": def_rank,
            "expected": expected, "league": mu, "edge": edge,
            # for quality rows: does this pairing favour the offence?
            "tone": ("good" if (edge > 0) == high_is_good else "poor") if quality and abs(edge) > 1e-9 else "neutral",
            "note": STYLE_NOTE.get(key),
        })
    return {"offence": offence, "defence": defence, "rows": rows,
            "offence_labels": labels[offence], "defence_labels": labels[defence],
            "alignment": alignment(labels[offence], labels[defence])}


def packet(repository: NFLRepository, game: dict[str, Any]) -> dict[str, Any]:
    """Panel data for one game, or {"available": False, "reason": ...}."""
    away, home = game["away_team"], game["home_team"]
    snap = team_styles.snapshot_for(repository, int(game["season"]), int(game["week"]))
    if not snap or any(t not in snap["points"]["off"] for t in (away, home)):
        return {"available": False, "reason": "Style ratings need play-by-play efficiency rows for both teams."}
    played = min(_games_played(repository, game, t) for t in (away, home))
    labels = team_styles.classify(snap)
    neutral = bool(game.get("neutral_site"))
    points = snap["points"]
    home_base = _expected(points, home, away, 0.0 if neutral else 0.5)
    away_base = _expected(points, away, home, 0.0 if neutral else -0.5)
    return {
        "available": True, "games_played": played, "settled": played >= MIN_GAMES_SETTLED,
        "labels": {away: labels[away], home: labels[home]},
        "directions": [
            _direction(snap, labels, away, home, 0.0 if neutral else -0.5),
            _direction(snap, labels, home, away, 0.0 if neutral else 0.5),
        ],
        "expected": {"away": away_base, "home": home_base, "margin": home_base - away_base,
                     "total": home_base + away_base},
        "neutral": neutral,
    }


def _expected(points: dict[str, Any], team: str, opp: str, home_col: float) -> float:
    return points["mu"] + points["home"] * home_col + points["off"][team] + points["def"][opp]


def _games_played(repository: NFLRepository, game: dict[str, Any], team: str) -> int:
    from contextlib import closing
    with closing(repository._connect()) as connection:
        return int(connection.execute(
            """SELECT COUNT(*) FROM games WHERE season=? AND week<? AND season_type='REG' AND completed=1
               AND (home_team=? OR away_team=?)""",
            (int(game["season"]), int(game["week"]), team, team)).fetchone()[0])
