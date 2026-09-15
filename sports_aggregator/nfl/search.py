"""Small, deterministic NFL entity search matching the CFB search surface."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from sports_aggregator.nfl.content import NFLContentRepository
from sports_aggregator.nfl.naming import normalize_name
from sports_aggregator.nfl.repository import NFLRepository


def search_entities(repository: NFLRepository, content: NFLContentRepository,
                    query: str, *, season: int, limit: int = 10) -> dict[str, Any]:
    raw = str(query or "").strip(); folded = raw.casefold(); normalized = normalize_name(raw)
    empty = {"query": raw, "too_short": len(folded) < 2, "season": season,
             "teams": [], "players": [], "games": [], "stories": [], "total": 0}
    if len(folded) < 2:
        return empty
    identities = {row["abbreviation"]: row for row in repository.list_teams()}
    teams = []
    for team in identities.values():
        haystack = " ".join(str(team.get(key) or "") for key in
                            ("abbreviation", "name", "nickname", "division")).casefold()
        if folded in haystack or normalized in normalize_name(haystack):
            exact = folded in {str(team.get("abbreviation") or "").casefold(),
                               str(team.get("nickname") or "").casefold()}
            teams.append({**team, "reason": "Exact team match" if exact else "Team identity match",
                          "score": 2 if exact else 1})
    teams.sort(key=lambda row: (-row["score"], row["name"]))
    teams = teams[:limit]
    matched_codes = {team["abbreviation"] for team in teams}

    with closing(repository._connect()) as connection:
        players = [dict(row) for row in connection.execute(
            """SELECT p.player_id,MAX(p.full_name) player_name,MAX(p.team) team,
                      MAX(p.position) position,MAX(p.headshot_url) headshot_url
               FROM players p WHERE p.season=? AND p.normalized_name LIKE ?
               GROUP BY p.player_id ORDER BY
                 CASE WHEN p.normalized_name=? THEN 0 ELSE 1 END,player_name LIMIT ?""",
            (season, f"%{normalized}%", normalized, limit),
        )]
        game_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM games
               WHERE LOWER(game_id || ' ' || away_team || ' ' || home_team || ' ' || game_date)
                     LIKE ?
               ORDER BY game_date DESC LIMIT ?""", (f"%{folded}%", limit * 3),
        )]
    if matched_codes:
        known = {game["game_id"] for game in game_rows}
        related = [game for game in repository.schedule(season)
                   if game["away_team"] in matched_codes or game["home_team"] in matched_codes]
        related.sort(key=lambda game: game["game_date"], reverse=True)
        game_rows.extend(game for game in related if game["game_id"] not in known)
    for player in players:
        identity = identities.get(player["team"], {})
        player.update({"logo_url": identity.get("logo_url"), "color": identity.get("color"),
                       "reason": f"{season} roster match"})
    games = []
    for game in game_rows[:limit]:
        game["reason"] = ("Final score" if game["completed"] else "Scheduled matchup")
        game["destination"] = "review" if game["completed"] else "overview"
        games.append(game)
    stories = content.search(raw, limit)
    for story in stories:
        story["reason"] = "Reporting text match"
    result = {**empty, "too_short": False, "teams": teams, "players": players,
              "games": games, "stories": stories}
    result["total"] = sum(len(result[key]) for key in ("teams", "players", "games", "stories"))
    return result
