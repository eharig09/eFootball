"""A draft board calibrated against the draft that actually happened.

Ranking prospects by opinion would mean inventing scouting grades this system has
no source for. Instead the completed 2026 draft is used as ground truth: 248 of
its 257 picks are matched by name and school to their prior-season PFF profile,
which gives a real distribution of what a drafted player at each position looked
like the year before.

A returning player is then placed against that distribution. The output is a
watchlist with an explicit basis -- "his profile sits at the 88th percentile of
players drafted at his position" -- not a projection of where he will be picked,
and never a substitute for the scouting sources the source graph is built to
ingest.

Draft eligibility is inferred from class year, which is the only signal the CFBD
roster carries. Redshirts and early declarations are invisible to it, so the
eligibility field is labeled an estimate everywhere it is shown.
"""

from __future__ import annotations

from contextlib import closing
import json
from typing import Any

from sports_aggregator.cfb.repository import CFBRepository, _logo_pair


#: PFF position codes mapped to the draft vocabulary CFBD publishes. Grouping
#: guard and tackle separately matters: they are drafted on different curves.
PFF_TO_DRAFT_POSITION = {
    "QB": "Quarterback", "HB": "Running Back", "FB": "Running Back",
    "WR": "Wide Receiver", "TE": "Tight End",
    "T": "Offensive Tackle", "G": "Offensive Guard", "C": "Center",
    "ED": "Defensive Edge", "DI": "Defensive Tackle", "LB": "Linebacker",
    "CB": "Cornerback", "S": "Safety",
    "K": "Place Kicker", "P": "Punter", "LS": "Long Snapper",
}

#: Display abbreviations for the draft vocabulary. Full position names wrap
#: mid-word in a narrow column ("Lineback / er"), and every reader of a draft
#: board already reads LB. The full name is kept as the column tooltip.
DRAFT_POSITION_ABBREVIATIONS = {
    "Quarterback": "QB", "Running Back": "RB", "Wide Receiver": "WR",
    "Tight End": "TE", "Offensive Tackle": "OT", "Offensive Guard": "OG",
    "Center": "C", "Defensive Edge": "EDGE", "Defensive Tackle": "DT",
    "Linebacker": "LB", "Cornerback": "CB", "Safety": "S",
    "Place Kicker": "K", "Punter": "P", "Long Snapper": "LS",
}


def position_abbreviation(position: str | None) -> str:
    """Short form for a draft position, falling back to the value itself."""
    if not position:
        return "—"
    return DRAFT_POSITION_ABBREVIATIONS.get(position, position)


#: Percentile bands and the language used to describe each. The label describes
#: the comparison, not a predicted selection.
BANDS = (
    (0.90, "ELITE_PROFILE", "Early-round profile"),
    (0.75, "STRONG_PROFILE", "Drafted profile"),
    (0.55, "FRINGE_PROFILE", "Late-round profile"),
    (0.00, "DEVELOPMENTAL", "Below drafted range"),
)

#: Minimum drafted players at a position before its distribution is trusted.
MIN_CALIBRATION_SAMPLE = 8


def _percentile(sorted_values: list[float], value: float) -> float:
    """Share of the calibration set at or below this value."""
    if not sorted_values:
        return 0.0
    below = sum(1 for item in sorted_values if item <= value)
    return below / len(sorted_values)


def calibration(repository: CFBRepository, *, draft_year: int = 2026,
                pff_season: int = 2025) -> dict[str, Any]:
    """Prior-season PFF profiles of players who were actually drafted.

    Matching is by normalized name *and* school, the same conservative rule the
    PFF importer uses, so a name collision cannot inflate a position's baseline.
    """
    repository.initialize()
    with repository._reader() as connection:
        rows = connection.execute(
            """SELECT d.position,d.round,d.overall_pick,d.pre_draft_grade,
               p.interest_score,p.position pff_position
               FROM draft_picks d
               JOIN pff_players p ON p.normalized_name=d.normalized_name
                 AND p.season=? AND p.cfbd_team=d.college_team
               WHERE d.draft_year=? AND p.interest_score IS NOT NULL""",
            (pff_season, draft_year)).fetchall()
    by_position: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_position.setdefault(row["position"], []).append(dict(row))
    positions = {}
    for position, entries in by_position.items():
        scores = sorted(entry["interest_score"] for entry in entries)
        early = [entry["interest_score"] for entry in entries if (entry["round"] or 9) <= 3]
        positions[position] = {
            "position": position,
            "drafted": len(entries),
            "scores": scores,
            "median": scores[len(scores) // 2],
            "minimum": scores[0],
            "early_round_median": (sorted(early)[len(early) // 2] if early else None),
            "reliable": len(entries) >= MIN_CALIBRATION_SAMPLE,
        }
    # A pooled distribution backs positions with too few picks to stand alone.
    pooled = sorted(row["interest_score"] for row in rows)
    return {"draft_year": draft_year, "pff_season": pff_season,
            "matched_picks": len(rows), "positions": positions, "pooled": pooled}


def prospect_board(repository: CFBRepository, *, roster_season: int = 2026,
                   pff_season: int = 2025, draft_year: int = 2027,
                   limit: int = 60, team_id: int | None = None,
                   conference: str | None = None) -> dict[str, Any]:
    """Draft-eligible returners ranked against the completed draft class."""
    reference = calibration(repository, draft_year=roster_season, pff_season=pff_season)
    repository.initialize()
    # Order must follow the placeholders in the query below: roster season,
    # PFF season, then the draft year whose picks are excluded.
    filters, params = [], [roster_season, pff_season, roster_season]
    if team_id is not None:
        # `players` stores the school name, not an id; the join supplies the id.
        filters.append("AND t.team_id=?")
        params.append(team_id)
    if conference:
        filters.append("AND t.conference=?")
        params.append(conference)
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT p.pff_player_id,p.player_name,p.position pff_position,
                p.interest_score,p.cfbd_player_id,p.cfbd_team,
                r.class_year,r.jersey,r.height,r.weight,
                t.team_id,t.school,t.conference,t.color,t.logos_json
                FROM pff_players p
                JOIN players r ON r.player_id=p.cfbd_player_id AND r.season=?
                JOIN teams t ON t.school=r.team
                WHERE p.season=? AND p.interest_score IS NOT NULL
                AND r.class_year>=3
                AND p.normalized_name NOT IN (
                    SELECT normalized_name FROM draft_picks WHERE draft_year=?)
                {' '.join(filters)}
                ORDER BY p.interest_score DESC""",
            params).fetchall()
        prospects = []
        for row in rows:
            item = dict(row)
            draft_position = PFF_TO_DRAFT_POSITION.get(item["pff_position"], item["pff_position"])
            profile = reference["positions"].get(draft_position)
            if profile and profile["reliable"]:
                scores, basis = profile["scores"], f"{profile['drafted']} drafted {draft_position}s"
            else:
                scores, basis = reference["pooled"], "all drafted players (thin positional sample)"
            percentile = _percentile(scores, item["interest_score"] or 0)
            band, headline = next(
                (code, text) for threshold, code, text in BANDS if percentile >= threshold)
            logos = json.loads(item.pop("logos_json") or "[]")
            reasons = [
                f"{pff_season} PFF interest {item['interest_score']:.1f}",
                f"{percentile * 100:.0f}th percentile against {basis}",
                f"class {item['class_year']} in {roster_season}",
            ]
            if profile and profile.get("early_round_median") is not None:
                comparison = "at or above" if (item["interest_score"] or 0) >= profile["early_round_median"] else "below"
                reasons.append(f"{comparison} the top-three-round median at this position")
            prospects.append({
                **item,
                "draft_position": draft_position,
                "position_abbreviation": position_abbreviation(draft_position),
                "percentile": round(percentile, 3),
                "band": band,
                "headline": headline,
                "calibration_basis": basis,
                "logo": _logo_pair(logos)[0],
                "logo_dark": _logo_pair(logos)[1],
                "reasons": reasons,
                "eligibility": "class-year estimate",
            })
    prospects.sort(key=lambda item: (-item["percentile"], -(item["interest_score"] or 0)))
    return {
        "draft_year": draft_year,
        "roster_season": roster_season,
        "pff_season": pff_season,
        "calibration": {"matched_picks": reference["matched_picks"],
                        "draft_year": reference["draft_year"]},
        "eligible_pool": len(prospects),
        "prospects": prospects[:limit],
    }


def position_targets(board: dict[str, Any], limit_per_position: int = 3) -> list[dict[str, Any]]:
    """The strongest returning prospects at each position group."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for prospect in board.get("prospects") or []:
        grouped.setdefault(prospect["draft_position"], []).append(prospect)
    return [{"position": position,
             "prospects": entries[:limit_per_position],
             "count": len(entries)}
            for position, entries in sorted(
                grouped.items(), key=lambda pair: -pair[1][0]["percentile"])]
