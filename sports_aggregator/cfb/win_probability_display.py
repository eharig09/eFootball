"""How the game's outcome probability moved, play by play.

The postgame report is a document, not a page with client-side charts -- its
own "Print / Save PDF" feature exists precisely because it's meant to be read
and archived as one static thing. A canvas-based chart wouldn't print or
export to PDF cleanly, and this page carries no other client-JS dependency
to justify adding one just for this section, so the win-probability graph
renders as inline SVG like every other visual on this report.
"""

from __future__ import annotations

from html import escape
from typing import Any

from markupsafe import Markup

from sports_aggregator.cfb.win_probability_v2 import game_win_probability_series

#: A chart under this many charted plays reads as noise, not a shape.
MIN_PLAYS = 10
WIDTH = 720
HEIGHT = 160


def _clock_label(row: dict[str, Any]) -> str:
    period = row.get("period")
    if not period:
        return ""
    minutes, seconds = row.get("clock_minutes"), row.get("clock_seconds")
    if minutes is None or seconds is None:
        return f"Q{period}"
    return f"Q{period} {int(minutes):02d}:{int(seconds):02d}"


def render_win_probability(repository, game: dict[str, Any]) -> Markup:
    """The postgame report's game-flow chapter: home win probability across every charted play."""
    game_id = int(game.get("game_id") or 0)
    if not game_id:
        return Markup("")
    try:
        series = game_win_probability_series(repository, game_id)
    except Exception:
        return Markup("")
    if len(series) < MIN_PLAYS:
        return Markup("")

    home_team = str(game.get("home_team") or "Home")
    away_team = str(game.get("away_team") or "Away")
    step = WIDTH / (len(series) - 1)
    coords = [
        (round(index * step, 1), round(HEIGHT - float(row["home_win_probability"]) * HEIGHT, 1))
        for index, row in enumerate(series)
    ]
    points = " ".join(f"{x},{y}" for x, y in coords)
    fill_points = f"0,{HEIGHT} " + points + f" {WIDTH},{HEIGHT}"
    start_wp = float(series[0]["home_win_probability"]) * 100
    final_wp = float(series[-1]["home_win_probability"]) * 100

    # Scoring plays get a small marker -- the moments a reader would already
    # look for on the scoreboard, tied to what the model thought at that instant.
    markers = []
    prior_offense_score, prior_defense_score = series[0].get("offense_score"), series[0].get("defense_score")
    for (x, y), row in zip(coords, series):
        offense_score = row.get("offense_score")
        defense_score = row.get("defense_score")
        scored = (offense_score is not None and prior_offense_score is not None
                 and offense_score > prior_offense_score) or (
                 defense_score is not None and prior_defense_score is not None
                 and defense_score > prior_defense_score)
        if scored:
            wp = float(row["home_win_probability"]) * 100
            markers.append(
                f'<circle cx="{x}" cy="{y}" r="3.5" class="wp-flow-marker">'
                f'<title>{escape(str(row.get("offense") or ""))} scores &middot; '
                f'{escape(_clock_label(row))} &middot; {escape(home_team)} {wp:.0f}%</title></circle>'
            )
        prior_offense_score, prior_defense_score = offense_score, defense_score

    return Markup(
        '<section class="section wp-flow">'
        '<div class="section-note">'
        f'{escape(home_team)} win probability (in-house wp-v2 model), play by play across '
        f'{len(series):,} charted plays. Dots mark scoring plays.'
        '</div>'
        '<div class="wp-flow-chart">'
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="none" role="img" '
        f'aria-label="{escape(home_team)} win probability opened at {start_wp:.0f} percent '
        f'and finished at {final_wp:.0f} percent">'
        f'<line x1="0" y1="{HEIGHT / 2}" x2="{WIDTH}" y2="{HEIGHT / 2}" class="wp-flow-mid"></line>'
        f'<polygon points="{fill_points}" class="wp-flow-fill"></polygon>'
        f'<polyline points="{points}" class="wp-flow-line" fill="none"></polyline>'
        + "".join(markers) +
        '</svg>'
        '<div class="wp-flow-labels">'
        f'<span class="wp-flow-home">{escape(home_team)} <b>{final_wp:.0f}%</b></span>'
        f'<span class="wp-flow-away">{escape(away_team)} <b>{100 - final_wp:.0f}%</b></span>'
        '</div>'
        '</div>'
        '</section>'
    )


def install_win_probability_display(app) -> None:
    """Registered as a template global, matching passing_display.py's pattern:
    the postgame report is already assembled by several of these chained on
    each other's output strings, so a new chapter doesn't need a new
    Jinja-loader wrapper."""
    if app.extensions.get("win_probability_display_installed"):
        return
    repository = app.extensions["cfb_repository"]
    app.jinja_env.globals["win_probability_flow"] = (
        lambda game: render_win_probability(repository, dict(game)))
    app.extensions["win_probability_display_installed"] = True
