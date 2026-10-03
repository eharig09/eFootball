"""What a draft prospect is walking into next: opponent, unit, and how much it matters.

A big board says who is good. It does not say whether you can watch them this
weekend, or against whom. These three answers are what turns a ranking into a
viewing plan: the next game, the opposing group the player will spend the
afternoon against, and a score that weighs the player's own grade against how
much the game is worth watching at all.

The opposing group is by draft position rather than by roster position, because
that is the vocabulary the board already speaks. A receiver's afternoon is spent
against the secondary; a tackle's against the edge. Where the answer is genuinely
"the whole front", it says so rather than inventing a precision it does not have.
"""

from __future__ import annotations

from typing import Any, Iterable

from sports_aggregator.cfb.insights import score_game_attention
from sports_aggregator.cfb.repository import CFBRepository


#: Draft position -> what it lines up across from, and the PFF position codes
#: that make up that group on the other roster.
OPPOSING_GROUP: dict[str, tuple[str, tuple[str, ...]]] = {
    "Quarterback": ("Pass rush", ("ED", "DI")),
    "Running Back": ("Front seven", ("DI", "ED", "LB")),
    "Wide Receiver": ("Secondary", ("CB", "S")),
    "Tight End": ("Linebackers & safeties", ("LB", "S")),
    "Offensive Tackle": ("Edge rushers", ("ED",)),
    "Offensive Guard": ("Interior line", ("DI",)),
    "Center": ("Interior line", ("DI",)),
    "Defensive Edge": ("Offensive tackles", ("T",)),
    "Defensive Tackle": ("Interior line", ("G", "C")),
    "Linebacker": ("Run game & tight ends", ("G", "C", "TE")),
    "Cornerback": ("Receivers", ("WR",)),
    "Safety": ("Receivers & tight ends", ("WR", "TE")),
}

#: The board's own scale tops out around here, so a raw interest score is put on
#: a 0-100 footing before it is blended with a game's attention score.
INTEREST_SCALE = 100.0


def _normalized_interest(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, score if score > 1.5 else score * INTEREST_SCALE))


def upcoming_by_team(repository: CFBRepository, season: int) -> dict[int, dict[str, Any]]:
    """The next unplayed game for every team, keyed by team id.

    One query for the whole slate rather than one per prospect: a hundred-row
    board would otherwise ask the same question a hundred times.
    """
    repository.initialize()
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT game_id, season, week, start_date, start_time_tbd,
                      home_team_id, away_team_id, home_team, away_team,
                      neutral_site, conference_game
               FROM games
               WHERE season = ? AND COALESCE(completed, 0) = 0
               ORDER BY start_date""",
            (int(season),)).fetchall()

    # Attention is computed, not stored, so it is derived here through the same
    # scorer the weekly watch list uses rather than a second opinion about what
    # makes a game worth watching.
    ranks: dict[str, int] = {}
    try:
        poll = repository.latest_rankings(int(season)) or {}
        for entry in poll.get("teams") or []:
            school = entry.get("school") or entry.get("team")
            if school and entry.get("rank"):
                ranks.setdefault(str(school), int(entry["rank"]))
    except Exception:
        ranks = {}

    upcoming: dict[int, dict[str, Any]] = {}
    for row in rows:
        game = dict(row)
        game["home_rank"] = ranks.get(str(game.get("home_team")))
        game["away_rank"] = ranks.get(str(game.get("away_team")))
        attention, _factors = score_game_attention(game)
        for side, opposite in (("home", "away"), ("away", "home")):
            team_id = row[f"{side}_team_id"]
            if team_id is None or int(team_id) in upcoming:
                continue
            upcoming[int(team_id)] = {
                "game_id": row["game_id"], "week": row["week"],
                "start_date": row["start_date"],
                "opponent": row[f"{opposite}_team"],
                "opponent_id": row[f"{opposite}_team_id"],
                "at_home": side == "home",
                "neutral": bool(row["neutral_site"]),
                "attention": float(attention or 0),
            }
    return upcoming


_OFFENSIVE_LINE = {"T", "G", "C"}
_DEFENDERS = {"ED", "DI", "LB", "CB", "S"}


def _unit_dataset(codes: tuple[str, ...]) -> str:
    """The one PFF dataset whose grade describes this unit.

    pff_matchup_rows carries a row per player *per dataset*, so a defensive
    lineman appears under defense, pass rush and run defense. Averaging across
    rows counted him three times and mixed grades that mean different things.
    """
    if _OFFENSIVE_LINE & set(codes):
        return "blocking"
    return "defense" if set(codes) <= _DEFENDERS else "receiving"


def _unit_grade(rows: Iterable[dict[str, Any]], codes: tuple[str, ...]) -> tuple[float | None, int]:
    """Snap-weighted grade across a position group, and how many players it averaged.

    Every player counts in proportion to his snaps, the way PFF's own team grades are
    built: against the 2026 team grades, weighting all players by snaps tracked PFF
    (r = 0.79-0.98 across seven college units) better than keeping only the top few
    players by snaps (0.75-0.93), so no starter cutoff is applied.
    """
    dataset = _unit_dataset(codes)
    wanted = {code.upper() for code in codes}
    graded = [(float(row["primary_grade"]), float(row.get("usage_count") or 0))
              for row in rows
              if row.get("dataset") == dataset and row.get("primary_grade") is not None
              and str(row.get("position") or "").upper() in wanted]
    if not graded:
        return None, 0
    snaps = sum(weight for _, weight in graded)
    grade = (sum(g * w for g, w in graded) / snaps if snaps
             else sum(g for g, _ in graded) / len(graded))
    return grade, len(graded)


def board_context(repository: CFBRepository, *, season: int,
                  pff_season: int = 2025) -> dict[str, Any]:
    """The schedule and grades every board on a page shares.

    Neither depends on which prospects are being annotated -- they cover every
    team with a game and every opponent of one -- so a page showing two boards
    was fetching both twice. On the draft page that was the single largest cost
    on the request.
    """
    upcoming = upcoming_by_team(repository, season)
    opponents = {entry["opponent_id"] for entry in upcoming.values() if entry.get("opponent_id")}
    return {"upcoming": upcoming,
            "grades": repository.pff_matchup_rows(opponents, pff_season) if opponents else {}}


def annotate_board(repository: CFBRepository, prospects: list[dict[str, Any]], *,
                   season: int, pff_season: int = 2025,
                   context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Add next game, opposing group and watch score to each prospect in place.

    Prospects whose team has no scheduled game keep the rest of their row and
    simply carry nothing here, which is the honest answer in the offseason and
    for a team whose season is over.

    Pass `context` from `board_context` when annotating more than one board on
    a page; without it each call fetches the same schedule and grades again.
    """
    if context is None:
        context = board_context(repository, season=season, pff_season=pff_season)
    upcoming, grades = context["upcoming"], context["grades"]

    for prospect in prospects:
        # The profile board carries `team_id`; the consensus board carries
        # `cfbd_team_id`. Both are the same school and both reach this function.
        team_id = prospect.get("team_id") or prospect.get("cfbd_team_id")
        game = upcoming.get(int(team_id)) if team_id is not None else None
        if not game:
            continue
        label, codes = OPPOSING_GROUP.get(
            prospect.get("draft_position") or "", ("Opposing front", ()))
        grade, counted = _unit_grade(grades.get(game["opponent_id"]) or [], codes) if codes else (None, 0)
        prospect["next_game_id"] = game["game_id"]
        prospect["next_week"] = game["week"]
        prospect["next_opponent"] = game["opponent"]
        prospect["next_is_home"] = game["at_home"]
        prospect["next_neutral"] = game["neutral"]
        prospect["opposing_group"] = label
        prospect["opposing_grade"] = grade
        prospect["opposing_graded_players"] = counted
        # The same blend the weekly watch list uses: mostly the player, with a
        # thumb on the scale for whether the game itself is worth turning on.
        prospect["watch_score"] = round(
            0.8 * _normalized_interest(prospect.get("interest_score"))
            + 0.2 * game["attention"], 1)
    return prospects
