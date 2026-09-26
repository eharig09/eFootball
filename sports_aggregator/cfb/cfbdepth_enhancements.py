"""Small presentation refinements for imported CFBDepth data.

This layer intentionally sits outside the core CFBD repository. It reshapes the
private roster export into the same fact-card language as the team page and
surfaces current-roster players with imported availability updates on matchup
pages.
"""

from __future__ import annotations

from contextlib import closing
from html import escape
from typing import Any

from flask import url_for
from jinja2 import BaseLoader, TemplateNotFound
from markupsafe import Markup

from sports_aggregator.cfb.cfbdepth_data import initialize, roster_breakdown
from sports_aggregator.cfb.depth_chart_observed_display import install_observed_depth_display
from sports_aggregator.cfb.depth_chart_profiles import install_depth_chart_profiles
from sports_aggregator.cfb.models import normalize_alias
from sports_aggregator.cfb.postgame_display import install_postgame_display
from sports_aggregator.cfb.postgame_analytics_display import install_postgame_analytics_display
from sports_aggregator.cfb.postgame_tendencies_display import install_postgame_tendencies_display
from sports_aggregator.cfb.production_display import install_production_display


ROSTER_CALL = "{{ cfbdepth_roster_strip(team.school) }}"
ROSTER_REPLACEMENT = "{{ cfbdepth_roster_facts(team.school) }}"
PLAYER_MATCHUP_ANCHOR = "    {{ data_table(player_matchup_table) }}\n"
PLAYER_MATCHUP_REPLACEMENT = (
    "    {{ cfbdepth_matchup_player_flags(game.away_team, game.home_team, game.season) }}\n"
    "    {{ data_table(player_matchup_table) }}\n"
)
# The `.cfbdepth-*` rules these fragments rely on live in `static/cfb.css`
# alongside the situation band they are modelled on. They used to be spliced in
# here as a page-local <style> block, which silently stopped applying once the
# CFB templates moved their styles out to external sheets.


class _EnhancementLoader(BaseLoader):
    def __init__(self, wrapped: BaseLoader):
        self.wrapped = wrapped

    def get_source(self, environment, template):
        if self.wrapped is None:
            raise TemplateNotFound(template)
        source, filename, uptodate = self.wrapped.get_source(environment, template)
        if template == "cfb_team.html" and "cfbdepth_roster_facts(" not in source:
            source = source.replace(ROSTER_CALL, ROSTER_REPLACEMENT, 1)
        if template == "cfb_game.html" and "cfbdepth_matchup_player_flags(" not in source:
            source = source.replace(PLAYER_MATCHUP_ANCHOR, PLAYER_MATCHUP_REPLACEMENT, 1)
        return source, filename, uptodate

    def list_templates(self):
        if hasattr(self.wrapped, "list_templates"):
            return self.wrapped.list_templates()
        return []


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return escape(str(value))


def _roster_facts(repository, school: str) -> Markup:
    row = roster_breakdown(repository, school)
    if not row:
        return Markup("")
    facts = [
        (_fmt(row.get("active_players")), "Active players"),
        (f"{_fmt(row.get('transfers'))} · {_fmt(row.get('transfer_pct'))}%", "Transfers"),
        (f"{_fmt(row.get('home_grown'))} · {_fmt(row.get('home_grown_pct'))}%", "Home grown"),
        (f"{_fmt(row.get('blue_chip_pct'), 1)}%", "Blue chip"),
        (f"{_fmt(row.get('five_star'))} / {_fmt(row.get('four_star'))}", "5★ / 4★"),
        (f"{_fmt(row.get('ol_avg_wt'), 1)}", "OL avg weight"),
        (f"{_fmt(row.get('dl_avg_wt'), 1)}", "DL avg weight"),
    ]
    cards = "".join(
        f'<div class="fact"><b>{value}</b><span>{escape(label)}</span></div>'
        for value, label in facts
    )
    return Markup(
        '<div class="cfbdepth-roster-note"><strong>Roster breakdown</strong>'
        '<span>private CFBDepth export</span></div>'
        f'<div class="facts cfbdepth-roster-facts">{cards}</div>'
    )


def _matchup_updates(repository, away: str, home: str, season: int) -> list[dict[str, Any]]:
    """Latest imported availability row for current-roster players only."""
    initialize(repository)
    team_lookup = {
        normalize_alias(away): away,
        normalize_alias(home): home,
    }
    with closing(repository._connect()) as connection:
        source_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfbdepth_player_updates
               WHERE normalized_team IN (?,?)
               ORDER BY update_id DESC""",
            tuple(team_lookup.keys()),
        ).fetchall()]
        roster_rows = [dict(row) for row in connection.execute(
            """SELECT player_id,first_name,last_name,normalized_name,team,position
               FROM players WHERE season=? AND team IN (?,?)""",
            (int(season), away, home),
        ).fetchall()]

    roster = {
        (str(row["normalized_name"]), normalize_alias(str(row["team"]))): row
        for row in roster_rows
    }
    seen: set[tuple[str, str]] = set()
    matched: list[dict[str, Any]] = []
    for update in source_rows:
        key = (str(update["normalized_name"]), str(update["normalized_team"]))
        if key in seen or key not in roster:
            continue
        seen.add(key)
        player = roster[key]
        matched.append({
            **update,
            "player_id": player["player_id"],
            "display_position": player.get("position") or update.get("position"),
            "display_team": player["team"],
        })
    status_order = {"Out for Season": 0, "Out": 1, "Doubtful": 2, "Questionable": 3, "Probable": 4}
    matched.sort(key=lambda row: (
        status_order.get(str(row.get("status") or ""), 9),
        -(float(row.get("impact")) if row.get("impact") is not None else -1.0),
        str(row.get("display_team") or ""),
        str(row.get("player_name") or ""),
    ))
    return matched


def _matchup_flags(repository, away: str, home: str, season: int) -> Markup:
    rows = _matchup_updates(repository, away, home, season)
    if not rows:
        return Markup("")
    grouped: dict[str, list[str]] = {away: [], home: []}
    for row in rows:
        href = url_for("cfb.player_preview", player_id=row["player_id"])
        name = escape(str(row.get("player_name") or ""))
        raw_status = str(row.get("status") or "Update")
        status = escape(raw_status)
        status_class = "".join(
            char if char.isalnum() else "-" for char in raw_status.casefold()
        ).strip("-")
        position = escape(str(row.get("display_position") or "—"))
        team = str(row.get("display_team") or "")
        impact = _fmt(row.get("impact"), 1)
        description = str(row.get("update_text") or "").strip()
        last_update = str(row.get("last_update") or "").strip()
        tooltip_parts = [part for part in (description, f"Updated {last_update}" if last_update else "") if part]
        tooltip = escape(" — ".join(tooltip_parts), quote=True)
        title_attr = f' title="{tooltip}"' if tooltip else ""
        grouped.setdefault(team, []).append(
            f'<article class="cfbdepth-availability-row status-{status_class}"{title_attr}>'
            '<div class="identity">'
            f'<a href="{href}">{name}</a>'
            f'<span>{position}</span>'
            '</div>'
            f'<strong class="status">{status}</strong>'
            f'<span class="impact">Impact {impact}</span>'
            '</article>'
        )

    team_cards = []
    for team in (away, home):
        team_rows = grouped.get(team, [])
        count = len(team_rows)
        team_cards.append(
            '<section class="cfbdepth-availability-team">'
            '<header>'
            f'<h4>{escape(team)}</h4>'
            f'<span>{count} flagged</span>'
            '</header>'
            '<div class="cfbdepth-availability-rows">'
            + ("".join(team_rows) if team_rows else '<p class="cfbdepth-availability-empty">No player designations</p>')
            + '</div></section>'
        )
    return Markup(
        '<div class="cfbdepth-availability">'
        '<header class="cfbdepth-availability-head">'
        '<div><span>Roster availability</span><h3>Players carrying a designation</h3></div>'
        f'<p>{len(rows)} linked update{"s" if len(rows) != 1 else ""} · private CFBDepth export · hover for report detail</p>'
        '</header>'
        '<div class="cfbdepth-availability-grid">'
        + "".join(team_cards)
        + '</div>'
        + '</div>'
    )


def install_cfbdepth_enhancements(app) -> None:
    if app.extensions.get("cfbdepth_enhancements_installed"):
        return
    repository = app.extensions["cfb_repository"]
    install_depth_chart_profiles()
    install_observed_depth_display(repository)
    install_production_display(app)
    install_postgame_display(app)
    install_postgame_analytics_display(app)
    install_postgame_tendencies_display(app)
    app.jinja_env.globals["cfbdepth_roster_facts"] = (
        lambda school: _roster_facts(repository, str(school))
    )
    app.jinja_env.globals["cfbdepth_matchup_player_flags"] = (
        lambda away, home, season: _matchup_flags(
            repository, str(away), str(home), int(season)
        )
    )
    app.jinja_loader = _EnhancementLoader(app.jinja_loader)
    app.jinja_env.cache.clear()
    app.extensions["cfbdepth_enhancements_installed"] = True
