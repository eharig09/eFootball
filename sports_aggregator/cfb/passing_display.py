"""Where each team throws, and what it is worth: middle of the field vs outside.

A pass over the middle is worth about four times one outside it -- 0.42 EPA per
attempt against 0.11 and 0.08 across the classified 2025 attempts -- and deep
middle is the most valuable throw in the sport. That gap is what these blocks
show, on the matchup page before the game and in the postgame report after it.

Two things are said out loud rather than assumed. The attempt counts are printed
next to every rate, because coverage is partial before 2026 and a middle EPA over
six throws is not a number to read. And the difference is not a recommendation:
quarterbacks throw over the middle when coverage allows it, so the number
describes where value was available, not what would happen if a team forced more
of them.
"""

from __future__ import annotations

from html import escape
from typing import Any

from markupsafe import Markup

from sports_aggregator.cfb.game_phases import (
    HALVES, MIN_PHASE_PLAYS, QUARTERS, game_phases, phase_margin,
)
from sports_aggregator.cfb.middle_of_field import (
    MIN_GAME_PLAYS, MIN_SEASON_PLAYS, game_middle, middle_verdict, team_season_middle,
)


STYLE = ""  # served from static/cfb_analysis.css, not inlined per render


def _f2(value: Any, *, signed: bool = True) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:+.2f}" if signed else f"{number:.2f}"


def _pct(value: Any) -> str:
    try:
        return f"{100 * float(value):.0f}%"
    except (TypeError, ValueError):
        return "—"


def _f1(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


def _side(label: str, buckets: dict[str, Any], *, minimum: int, better_high: bool) -> str:
    """One team, one side of the ball: middle against everything else."""
    middle, outside = buckets.get("middle") or {}, buckets.get("outside") or {}
    rows = [
        '<div class="pass-row head"><span></span><span class="num">EPA/att</span>'
        '<span class="num">Att</span><span class="num">ADOT</span></div>'
    ]
    values = [middle.get("epa_per_attempt"), outside.get("epa_per_attempt")]
    thin = [(middle.get("attempts") or 0) < minimum, (outside.get("attempts") or 0) < minimum]
    # Only mark an edge where both sides carry enough attempts to compare.
    best = None
    if not any(thin) and None not in values and values[0] != values[1]:
        best = 0 if (values[0] > values[1]) == better_high else 1
    for index, (name, bucket) in enumerate((("Middle", middle), ("Outside", outside))):
        attempts = bucket.get("attempts") or 0
        classes = "pass-row" + (" pass-thin" if thin[index] else "")
        epa = f'<strong class="{"best" if best == index else ""}">{_f2(bucket.get("epa_per_attempt"))}</strong>'
        rows.append(
            f'<div class="{classes}"><span>{escape(name)}</span>'
            f'<span class="num">{epa}</span>'
            f'<span class="num">{attempts}</span>'
            f'<span class="num">{_f1(bucket.get("adot"))}</span></div>')
    share = buckets.get("middle_share")
    share_text = f" · {_pct(share)} of attempts to the middle" if share is not None else ""
    return (f'<div class="pass-side"><div class="pass-side-label">{escape(label)}'
            f'{escape(share_text)}</div>{"".join(rows)}</div>')



def _empty(reason: str, *, heading: bool = True) -> Markup:
    # A plain muted line, not a dashed "nothing here" box: the section has a
    # real place in the report and is just waiting on a pipeline step.
    return Markup(
        STYLE + '<section class="section pass-map">'
        + (_TITLE if heading else "")
        + f'<p class="pass-note">{escape(reason)}</p></section>')



#: Run and pass, each split middle against outside, then the two together. The
#: combined row is the one the verdict is drawn from: a team can lose the middle
#: on the ground and win it through the air, and neither half alone says which.
ROWS = (("run", "middle", "Run — middle"), ("run", "outside", "Run — outside"),
        ("pass", "middle", "Pass — middle"), ("pass", "outside", "Pass — outside"),
        ("combined", "middle", "All middle"), ("combined", "outside", "All outside"))

NOTE = ("Where each team attacked and what it returned, split between the middle "
        "of the field and outside it, run and pass together. EPA is our "
        "event-aligned ep-v2 model; pass direction is CFBD per-attempt detail and "
        "rush direction is parsed from play text, so coverage varies by game and "
        "the play count behind every figure is shown. A middle throw has been "
        "worth about four times an outside one, but that reflects where coverage "
        "allowed the ball to go: it describes value that was available, not a "
        "play-call recommendation.")

MATCHUP_NOTE = ("Season-to-date EPA per play through the middle. Each offense is paired "
                "with the defense it faces; the outside value is a baseline, not a "
                "projection.")


def _cell(bucket, *, minimum, best=False):
    """Rate on top, the totals it came from underneath.

    Both, because they answer different questions and disagree often: a team can
    lead the middle in yards by running there forty times and still be worse per
    play than one that went nine times.
    """
    plays = bucket.get("plays") or 0
    thin = plays < minimum
    parts = ["%d plays" % plays]
    yards = bucket.get("yards")
    if plays and yards is not None:
        parts.append("%d yds" % round(yards))
        parts.append("%s/play" % _f1(bucket.get("yards_per_play")))
    total_epa = bucket.get("total_epa")
    if total_epa is not None:
        parts.append("%s EPA" % _f1(total_epa))
    success = bucket.get("success_rate")
    if success is not None:
        parts.append("%s succ" % _pct(success))
    mark = "best" if best and not thin else ""
    return ('<div class="mof-cell%s"><strong class="%s">%s</strong>'
            '<span>%s</span></div>'
            % (" pass-thin" if thin else "", mark, _f2(bucket.get("epa_per_play")),
               escape(" \u00b7 ".join(parts))))


def _matrix(columns, *, minimum, better_high=True):
    """One row per split, one column per thing being compared."""
    width = len(columns)
    head = "".join('<div class="mof-head">%s</div>' % escape(label) for label, _ in columns)
    body = []
    for source, side, label in ROWS:
        values = [data[source][side].get("epa_per_play") for _l, data in columns]
        counts = [data[source][side].get("plays") or 0 for _l, data in columns]
        usable = [v for v, c in zip(values, counts) if v is not None and c >= minimum]
        winner = None
        if len(usable) == width and width > 1 and len(set(usable)) > 1:
            winner = values.index(max(usable) if better_high else min(usable))
        cells = "".join(_cell(data[source][side], minimum=minimum, best=(i == winner))
                        for i, (_l, data) in enumerate(columns))
        emphasis = " mof-total" if source == "combined" else ""
        body.append('<div class="mof-row%s"><div class="mof-label">%s</div>%s</div>'
                    % (emphasis, escape(label), cells))
    return ('<div class="mof-grid" style="--mof-columns:%d">'
            '<div class="mof-row mof-header"><div class="mof-label"></div>%s</div>%s</div>'
            % (width, head, "".join(body)))


def _matchup_cell(data, source, *, minimum, best=False):
    """A matchup-page cell with one primary value and one compact baseline.

    The full report needs totals, yards and success rate. The preview needs a
    quick comparison, so it keeps EPA/play prominent and puts only the sample
    and outside EPA underneath it.
    """
    middle = data[source]["middle"]
    outside = data[source]["outside"]
    plays = middle.get("plays") or 0
    thin = plays < minimum
    mark = "best" if best and not thin else ""
    outside_value = _f2(outside.get("epa_per_play"))
    return (
        '<div class="mof-cell%s"><strong class="%s">%s</strong>'
        '<span><b>%d plays</b><i>Outside %s</i></span></div>'
        % (" pass-thin" if thin else "", mark,
           _f2(middle.get("epa_per_play")), plays, outside_value)
    )


def _matchup_matrix(attacker, defender, offense, defense, *, minimum):
    """Three scannable middle-of-field rows for one possession matchup."""
    columns = (("%s offense" % attacker, offense),
               ("%s allowed" % defender, defense))
    head = "".join('<div class="mof-head">%s</div>' % escape(label)
                   for label, _data in columns)
    body = []
    for source, label in (("combined", "Overall"), ("run", "Run"), ("pass", "Pass")):
        values = [data[source]["middle"].get("epa_per_play") for _label, data in columns]
        counts = [data[source]["middle"].get("plays") or 0 for _label, data in columns]
        usable = all(value is not None and count >= minimum
                     for value, count in zip(values, counts))
        winner = None
        if usable and values[0] != values[1]:
            winner = values.index(max(values))
        cells = "".join(
            _matchup_cell(data, source, minimum=minimum, best=(index == winner))
            for index, (_label, data) in enumerate(columns)
        )
        emphasis = " mof-total" if source == "combined" else ""
        body.append('<div class="mof-row%s"><div class="mof-label">%s</div>%s</div>'
                    % (emphasis, label, cells))
    return (
        '<div class="mof-grid mof-matchup-matrix" style="--mof-columns:2">'
        '<div class="mof-row mof-header"><div class="mof-label">EPA/play</div>%s</div>%s</div>'
        % (head, "".join(body))
    )


#: Printed only where nothing above it already says what this is. The postgame
#: report wraps the block in a numbered chapter whose head is the title.
_TITLE = '<h2>Middle of the field</h2>'


def _shell_middle(inner, note, *, heading=True):
    return Markup(
        STYLE + '<section class="section pass-map">'
        + (_TITLE if heading else "")
        + '<div class="section-note">%s</div>%s<p class="pass-note">%s</p></section>'
        % (escape(NOTE), inner, escape(note)))


def render_game(repository, game):
    """Postgame: both offences compared in the middle, run and pass.

    Offence only. In one game a team's "middle allowed" is the opponent's
    "middle attacked" -- the same plays counted again -- so printing both sides
    for both teams showed two numbers four times.
    """
    game_id = int(game.get("game_id") or 0)
    if not game_id:
        return Markup("")
    try:
        teams = game_middle(repository, game_id)
    except Exception:
        return _empty("Directional detail could not be read for this game.",
                      heading=False)
    order = [str(game.get("away_team") or ""), str(game.get("home_team") or "")]
    columns = [(team, teams[team]) for team in order if team in teams] or list(teams.items())
    if not columns or not any(data["plays"] for _team, data in columns):
        return _empty("No classified run or pass direction is stored for this game. "
                      "Coverage varies by game and is thin before 2025.",
                      heading=False)
    verdict = middle_verdict(dict(columns))
    banner = ""
    if verdict:
        banner = ('<div class="mof-verdict"><strong>%s</strong> won the middle, '
                  '%s EPA per play to %s.</div>'
                  % (escape(verdict["winner"]), _f2(verdict["winner_epa"]),
                     _f2(verdict["loser_epa"])))
    return _shell_middle(
        banner + _matrix(columns, minimum=MIN_GAME_PLAYS),
        "A split under %d plays is greyed and carries no edge mark. Each column is "
        "that team with the ball, so what the opponent gave up is the other column "
        "rather than a repeat of it." % MIN_GAME_PLAYS,
        heading=False)


def render_matchup(repository, game):
    """Pre-game: each offence against the defence it is about to face."""
    season = int(game.get("season") or 0)
    away = str(game.get("away_team") or "")
    home = str(game.get("home_team") or "")
    if not season or not away or not home:
        return Markup("")
    try:
        splits = {team: team_season_middle(repository, team, season) for team in (away, home)}
    except Exception:
        return _empty("Directional detail could not be read for %d." % season)
    if not any(side["plays"] for team in splits.values()
               for side in (team["offense"], team["defense"])):
        return _empty("No classified run or pass direction is stored for %d yet." % season)
    blocks = []
    for attacker, defender in ((away, home), (home, away)):
        offense = splits[attacker]["offense"]
        if not offense.get("plays"):
            # A table of dashes reads as broken; say it plainly instead.
            blocks.append(
                '<div class="mof-block"><h3>When %s has the ball</h3>'
                '<p class="pass-thin">No classified run or pass direction is stored for '
                '%s&rsquo;s offence yet.</p></div>' % (escape(attacker), escape(attacker)))
            continue
        matrix = _matchup_matrix(attacker, defender, offense,
                                 splits[defender]["defense"],
                                 minimum=MIN_SEASON_PLAYS)
        blocks.append('<div class="mof-block"><h3>When %s has the ball</h3>%s</div>'
                      % (escape(attacker), matrix))
    detail = (
        '<details class="mof-method"><summary>How to read this</summary>'
        '<p>Values are EPA per play through the middle; Outside is the same '
        'team&rsquo;s baseline. A row under %d middle plays is muted and carries '
        'no edge mark. Direction coverage varies by game, and the comparison '
        'is descriptive rather than a play-call recommendation.</p></details>'
        % MIN_SEASON_PLAYS
    )
    return Markup(
        STYLE + '<section class="section pass-map mof-matchup">' + _TITLE
        + '<div class="section-note">%s</div><div class="mof-matchup-grid">%s</div>%s</section>'
        % (escape(MATCHUP_NOTE), "".join(blocks), detail)
    )


PHASE_COLUMNS = ((("quarters", 1), "Q1"), (("quarters", 2), "Q2"),
                 (("quarters", 3), "Q3"), (("quarters", 4), "Q4"),
                 (("halves", "first"), "1st half"), (("halves", "second"), "2nd half"),
                 (("game", None), "Game"))


def _phase_cell(bucket, *, best=False):
    plays = bucket.get("plays") or 0
    if bucket.get("garbage_only"):
        # Snaps were run, just all past the garbage-time margin -- a bare
        # dash here is indistinguishable from a phase with no plays at all
        # and reads as missing data rather than a blowout.
        return '<div class="mof-cell"><strong class="pass-thin">—</strong><span class="pass-thin">Garbage time</span></div>'
    thin = plays < MIN_PHASE_PLAYS
    yards = bucket.get("yards")
    detail = "%d plays" % plays
    if plays and yards is not None:
        detail += " · %d yds" % round(yards)
    return ('<div class="mof-cell%s"><strong class="%s">%s</strong><span>%s</span></div>'
            % (" pass-thin" if thin else "", "best" if best and not thin else "",
               _f2(bucket.get("epa_per_play")), escape(detail)))


def render_phases(repository, game):
    """EPA by quarter and by half, for both teams.

    A final margin says who was better, not when. A team level for three quarters
    that pulls away in the fourth and one that led throughout arrive at the same
    number and are not the same team.
    """
    game_id = int(game.get("game_id") or 0)
    if not game_id:
        return Markup("")
    try:
        teams = game_phases(repository, game_id)
    except Exception:
        return Markup("")
    order = [str(game.get("away_team") or ""), str(game.get("home_team") or "")]
    rows = [(team, teams[team]) for team in order if team in teams] or list(teams.items())
    if not rows or not any(data["game"]["plays"] for _team, data in rows):
        return Markup("")

    def bucket(data, scope, key):
        return data["game"] if scope == "game" else data[scope].get(key) or {}

    head = "".join('<div class="mof-head">%s</div>' % escape(label)
                   for _spec, label in PHASE_COLUMNS)
    body = []
    for team, data in rows:
        cells = []
        for (scope, key), _label in PHASE_COLUMNS:
            values = [bucket(other, scope, key).get("epa_per_play") for _t, other in rows]
            counts = [bucket(other, scope, key).get("plays") or 0 for _t, other in rows]
            usable = [v for v, c in zip(values, counts) if v is not None and c >= MIN_PHASE_PLAYS]
            mine = bucket(data, scope, key)
            best = (len(usable) == len(rows) and len(set(usable)) > 1
                    and mine.get("epa_per_play") == max(usable))
            cells.append(_phase_cell(mine, best=best))
        body.append('<div class="mof-row"><div class="mof-label">%s</div>%s</div>'
                    % (escape(team), "".join(cells)))
    grid = ('<div class="mof-grid mof-phases" style="--mof-columns:%d">'
            '<div class="mof-row mof-header"><div class="mof-label"></div>%s</div>%s</div>'
            % (len(PHASE_COLUMNS), head, "".join(body)))

    verdicts = []
    for scope, key, label in (("halves", "second", "second half"), ("quarters", 4, "fourth quarter")):
        margin = phase_margin(dict(rows), scope, key)
        if margin:
            verdicts.append("%s took the %s, %s EPA per play to %s"
                            % (escape(margin["winner"]), label,
                               _f2(margin["winner_epa"]), _f2(margin["loser_epa"])))
    banner = ('<div class="mof-verdict">%s.</div>' % "; ".join(verdicts)) if verdicts else ""
    return Markup(
        STYLE + '<section class="section pass-map">'
        '<div class="section-note">Regulation only: '
        'overtime starts at the twenty-five with no clock, so folding it into the '
        'fourth quarter would compare plays the model was never fitted on. Rates '
        'are EPA per play on rush and pass snaps outside garbage time; a phase '
        'under %d plays is greyed, and a blowout can leave a whole quarter or half '
        'marked garbage time rather than counted here at all.</div>%s%s</section>'
        % (MIN_PHASE_PLAYS, banner, grid))




def install_passing_display(app) -> None:
    """Registered as template globals, so the templates call them where they want.

    Deliberately not another Jinja-loader wrapper: the postgame report is already
    assembled by four of those chained on each other's output strings, and each
    new link makes the section order depend on an import order somewhere else.
    """
    if app.extensions.get("passing_display_installed"):
        return
    repository = app.extensions["cfb_repository"]
    app.jinja_env.globals["passing_game_splits"] = (
        lambda game: render_game(repository, dict(game)))
    app.jinja_env.globals["passing_matchup_splits"] = (
        lambda game: render_matchup(repository, dict(game)))
    app.jinja_env.globals["game_phase_epa"] = (
        lambda game: render_phases(repository, dict(game)))
    app.extensions["passing_display_installed"] = True
