"""How the game's outcome probability moved, play by play.

The postgame report is a document, not a page with client-side charts -- its
own "Print / Save PDF" feature exists precisely because it's meant to be read
and archived as one static thing. A canvas-based chart wouldn't print or
export to PDF cleanly, and this page carries no other client-JS dependency to
justify adding one just for the chart itself, so the line/fill render as
inline SVG like every other visual on this report. The hover interaction is
a small, self-contained script scoped to this one chart -- it only changes
what a reader sees on screen; a printed or exported copy is unaffected either
way, since hover has never meant anything on paper.
"""

from __future__ import annotations

from html import escape
import json
from typing import Any

from markupsafe import Markup

from sports_aggregator.cfb.win_probability_v2 import game_win_probability_series

#: A chart under this many charted plays reads as noise, not a shape.
MIN_PLAYS = 10
WIDTH = 720
HEIGHT = 160

_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _clock_label(row: dict[str, Any]) -> str:
    period = row.get("period")
    if not period:
        return ""
    minutes, seconds = row.get("clock_minutes"), row.get("clock_seconds")
    if minutes is None or seconds is None:
        return f"Q{period}"
    return f"Q{period} {int(minutes):02d}:{int(seconds):02d}"


def _down_distance(row: dict[str, Any]) -> str | None:
    down, distance = row.get("down"), row.get("distance")
    if not down or down not in _ORDINALS:
        return None
    if distance is None:
        return _ORDINALS[down]
    return f"{_ORDINALS[down]} & {int(distance)}"


def _score_line(row: dict[str, Any], home_team: str, away_team: str) -> str:
    offense_score, defense_score = row.get("offense_score"), row.get("defense_score")
    if offense_score is None or defense_score is None:
        return ""
    home_score, away_score = (offense_score, defense_score) if row.get("offense") == row.get("home_team") \
        else (defense_score, offense_score)
    return f"{away_team} {int(away_score)} – {int(home_score)} {home_team}"


def _play_point(row: dict[str, Any], home_team: str, away_team: str) -> dict[str, Any]:
    """One play's tooltip content, as plain text -- rendered client-side via
    .textContent, never innerHTML, so nothing here is (or should be) HTML-escaped."""
    wp = round(float(row["home_win_probability"]) * 100, 1)
    parts = [part for part in (_clock_label(row), row.get("offense"), _down_distance(row)) if part]
    heading = " · ".join(str(part) for part in parts) or "Play"
    description = (row.get("play_text") or "").strip()
    if len(description) > 160:
        description = description[:157].rstrip() + "…"
    return {
        "heading": heading,
        "description": description,
        "score": _score_line(row, home_team, away_team),
        "wp": f"{home_team} {wp:.0f}%",
    }


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

    # Scoring plays get a permanent marker -- the moments a reader would
    # already look for on the scoreboard, tied to what the model thought at
    # that instant. Every other play is reachable too, via the hover track
    # below, just without cluttering the line with 150-plus dots at rest.
    markers = []
    prior_offense_score, prior_defense_score = series[0].get("offense_score"), series[0].get("defense_score")
    for (x, y), row in zip(coords, series):
        offense_score, defense_score = row.get("offense_score"), row.get("defense_score")
        scored = (offense_score is not None and prior_offense_score is not None
                 and offense_score > prior_offense_score) or (
                 defense_score is not None and prior_defense_score is not None
                 and defense_score > prior_defense_score)
        if scored:
            markers.append(f'<circle cx="{x}" cy="{y}" r="3.5" class="wp-flow-marker"></circle>')
        prior_offense_score, prior_defense_score = offense_score, defense_score

    # One plain-text point per play, in the same left-to-right order the
    # x-coordinates already use, so the hover script can index straight into
    # it by nearest x -- no per-point DOM node, no client-side re-fetch.
    payload = [_play_point(row, home_team, away_team) for row in series]
    payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    return Markup(
        '<section class="section wp-flow">'
        '<div class="section-note">'
        f'{escape(home_team)} win probability (in-house wp-v2 model), play by play across '
        f'{len(series):,} charted plays. Dots mark scoring plays; hover or drag along the line '
        'for any play.'
        '</div>'
        '<div class="wp-flow-chart" data-wp-flow>'
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" preserveAspectRatio="none" role="img" '
        f'aria-label="{escape(home_team)} win probability opened at {start_wp:.0f} percent '
        f'and finished at {final_wp:.0f} percent" data-wp-flow-svg>'
        f'<line x1="0" y1="{HEIGHT / 2}" x2="{WIDTH}" y2="{HEIGHT / 2}" class="wp-flow-mid"></line>'
        f'<polygon points="{fill_points}" class="wp-flow-fill"></polygon>'
        f'<polyline points="{points}" class="wp-flow-line" fill="none"></polyline>'
        + "".join(markers) +
        f'<line class="wp-flow-guide" x1="0" y1="0" x2="0" y2="{HEIGHT}" hidden></line>'
        '<circle class="wp-flow-cursor" r="4" hidden></circle>'
        f'<rect class="wp-flow-hit" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" '
        'fill="transparent" data-wp-flow-hit></rect>'
        '</svg>'
        '<div class="wp-flow-tooltip" data-wp-flow-tip hidden>'
        '<strong data-wp-flow-tip-heading></strong>'
        '<span data-wp-flow-tip-description></span>'
        '<span data-wp-flow-tip-score></span>'
        '<span data-wp-flow-tip-wp></span>'
        '</div>'
        '<div class="wp-flow-labels">'
        f'<span class="wp-flow-home">{escape(home_team)} <b>{final_wp:.0f}%</b></span>'
        f'<span class="wp-flow-away">{escape(away_team)} <b>{100 - final_wp:.0f}%</b></span>'
        '</div>'
        '</div>'
        f'<script type="application/json" data-wp-flow-json>{payload_json}</script>'
        '</section>'
        '<script>(function(){"use strict";'
        'document.querySelectorAll("[data-wp-flow]").forEach(function(root){'
        'if(root.dataset.wpFlowBound)return;root.dataset.wpFlowBound="1";'
        'var svg=root.querySelector("[data-wp-flow-svg]");'
        'var hit=root.querySelector("[data-wp-flow-hit]");'
        'var guide=root.querySelector(".wp-flow-guide");'
        'var cursor=root.querySelector(".wp-flow-cursor");'
        'var tip=root.querySelector("[data-wp-flow-tip]");'
        'var tipHeading=root.querySelector("[data-wp-flow-tip-heading]");'
        'var tipDescription=root.querySelector("[data-wp-flow-tip-description]");'
        'var tipScore=root.querySelector("[data-wp-flow-tip-score]");'
        'var tipWp=root.querySelector("[data-wp-flow-tip-wp]");'
        'var data;try{data=JSON.parse(root.querySelector("[data-wp-flow-json]").textContent);}catch(e){return;}'
        'if(!data||!data.length)return;'
        f'var width={WIDTH},height={HEIGHT};'
        'var line=svg.querySelector(".wp-flow-line");'
        'var points=line.getAttribute("points").split(" ").map(function(p){'
        'var xy=p.split(",");return {x:parseFloat(xy[0]),y:parseFloat(xy[1])};});'
        'function show(clientX){'
        'var box=svg.getBoundingClientRect();'
        'var ratio=Math.max(0,Math.min(1,(clientX-box.left)/box.width));'
        'var index=Math.round(ratio*(points.length-1));'
        'var point=points[index],play=data[index];'
        'guide.setAttribute("x1",point.x);guide.setAttribute("x2",point.x);guide.hidden=false;'
        'cursor.setAttribute("cx",point.x);cursor.setAttribute("cy",point.y);cursor.hidden=false;'
        'tipHeading.textContent=play.heading;'
        'tipDescription.textContent=play.description;'
        'tipScore.textContent=play.score;'
        'tipWp.textContent=play.wp;'
        'tip.hidden=false;'
        'var leftPct=(point.x/width)*100;'
        'tip.style.left=leftPct+"%";'
        'tip.classList.toggle("wp-flow-tooltip-right",leftPct>62);'
        'tip.classList.toggle("wp-flow-tooltip-left",leftPct<=62);'
        '}'
        'function hide(){guide.hidden=true;cursor.hidden=true;tip.hidden=true;}'
        'hit.addEventListener("pointermove",function(e){show(e.clientX);});'
        'hit.addEventListener("pointerdown",function(e){show(e.clientX);});'
        'hit.addEventListener("pointerleave",function(){hide();});'
        '});'
        '}());</script>'
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
