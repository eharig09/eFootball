"""Compact landing-page intelligence: watch scores and draft projection."""
from __future__ import annotations
from collections import defaultdict
from contextlib import closing
from typing import Any
from sports_aggregator.cfb.prospects import consensus_board
from sports_aggregator.nfl.repository import NFLRepository

GRADE_METRICS = ("grades_pass", "grades_pass_route", "grades_run", "grades_pass_block",
                 "grades_run_block", "man_grades_coverage_defense",
                 "zone_grades_coverage_defense")
POSITION_GROUP = {"OT": "OL", "OG": "OL", "G": "OL", "C": "OL", "T": "OL",
                  "DE": "EDGE", "ED": "EDGE", "DI": "DT", "DL": "DT",
                  "DB": "S", "FS": "S", "SS": "S", "HB": "RB"}

def _top_graded(repository: NFLRepository, pff_season: int) -> dict[str, list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in GRADE_METRICS)
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            f"""SELECT m.team,m.gsis_id,p.player_name,p.position,MAX(m.value) grade
                FROM nfl_pff_player_metrics m JOIN nfl_pff_players p
                  ON p.season=m.season AND p.pff_id=m.pff_id AND p.team=m.team
                WHERE m.season=? AND m.metric IN ({placeholders}) AND m.week=0
                GROUP BY m.team,m.gsis_id,p.player_name,p.position
                ORDER BY m.team,grade DESC""", (pff_season, *GRADE_METRICS))]
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if len(output[row["team"]]) < 4:
            output[row["team"]].append(row)
    return output

def games_to_watch(repository: NFLRepository, games: list[dict[str, Any]],
                   standings: dict[str, dict[str, Any]], identities: dict[str, dict[str, Any]],
                   pff_season: int, *, limit: int = 6) -> list[dict[str, Any]]:
    elo = {row["team"]: row["rating"] for row in repository.elo_ratings()}
    talent = _top_graded(repository, pff_season)
    output = []
    for game in games:
        away, home = game["away_team"], game["home_team"]
        away_elo, home_elo = elo.get(away, 1500), elo.get(home, 1500)
        quality = max(0, min(100, ((away_elo + home_elo) / 2 - 1350) / 3))
        closeness = max(0, 100 - abs(away_elo - home_elo) / 2)
        division = bool(game.get("division_game"))
        away_pct = standings.get(away, {}).get("win_pct")
        home_pct = standings.get(home, {}).get("win_pct")
        away_pct = .5 if away_pct is None else away_pct
        home_pct = .5 if home_pct is None else home_pct
        standing_score = .5 * ((away_pct + home_pct) / 2 * 100) + .5 * (
            100 - abs(away_pct - home_pct) * 100)
        player_pair = []
        for team in (away, home):
            if talent.get(team):
                player_pair.append(talent[team][0])
        player_score = sum(row["grade"] for row in player_pair) / len(player_pair) if player_pair else 60
        score = round(.32 * quality + .25 * closeness + .15 * standing_score
                      + .18 * player_score + (10 if division else 0), 1)
        reasons = []
        if division: reasons.append("division leverage")
        if quality >= 70: reasons.append("high-rated teams")
        if closeness >= 75: reasons.append("tight Elo matchup")
        if standing_score >= 65: reasons.append("standings pressure")
        if player_pair: reasons.append("premium player interaction")
        sides = []
        for team in (away, home):
            identity = identities.get(team, {})
            sides.append({"team": team, "logo_url": identity.get("logo_url"),
                          "color": identity.get("color"), "elo": round(elo.get(team, 1500)),
                          "record": standings.get(team, {}).get("record", "0-0")})
        output.append({**game, "watch_score": score, "reasons": reasons,
                       "standing_score": round(standing_score, 1),
                       "sides": sides, "players": player_pair})
    output.sort(key=lambda row: (-row["watch_score"], row["game_date"], row["game_id"]))
    return output[:limit]

def _established_qbs(repository: NFLRepository, season: int) -> dict[str, dict[str, Any]]:
    established = {}
    depth_rank = {}
    for team in repository.list_teams():
        for row in repository.current_depth_chart(season, team["abbreviation"]):
            if row.get("gsis_id"):
                key = (team["abbreviation"], row["gsis_id"])
                depth_rank[key] = min(depth_rank.get(key, 99), row.get("position_rank") or 99)
    with closing(repository._connect()) as connection:
        rows = connection.execute(
            """SELECT p.team,p.player_id,p.full_name,m.draft_round,m.years_experience,
                      SUM(CASE WHEN s.metric='attempts' THEN s.value ELSE 0 END) attempts,
                      SUM(CASE WHEN s.metric='passing_yards' THEN s.value ELSE 0 END) yards,
                      SUM(CASE WHEN s.metric='passing_epa' THEN s.value ELSE 0 END) passing_epa
               FROM players p LEFT JOIN player_master m ON m.gsis_id=p.player_id
               LEFT JOIN player_weekly_stats s ON s.player_id=p.player_id AND s.season=?
               WHERE p.season=? AND p.position='QB'
               GROUP BY p.team,p.player_id,p.full_name,m.draft_round,m.years_experience""",
            (season - 1, season),
        )
        for qb in rows:
            attempts, yards = qb["attempts"] or 0, qb["yards"] or 0
            efficiency = (qb["passing_epa"] or 0) / attempts if attempts else -99
            young_first_rounder = (qb["draft_round"] == 1
                                   and (qb["years_experience"] or 99) <= 7 and attempts >= 200)
            productive_veteran = yards >= 3500 and efficiency >= .08
            if depth_rank.get((qb["team"], qb["player_id"])) == 1 and (
                    young_first_rounder or productive_veteran):
                current = established.get(qb["team"])
                score = efficiency * 100 + (12 if qb["draft_round"] == 1 else 0)
                if current is None or score > current["score"]:
                    established[qb["team"]] = {
                        "player": qb["full_name"], "attempts": attempts, "yards": yards,
                        "efficiency": efficiency, "score": score,
                        "reason": "established returning QB1",
                    }
    return established

def draft_projection(repository: NFLRepository, cfb_repository, season: int,
                     *, draft_year: int = 2027, limit: int = 32) -> dict[str, Any]:
    board = consensus_board(cfb_repository, draft_year=draft_year, limit=100)
    standings = repository.standings(season)
    elo = {row["team"]: row["rating"] for row in repository.elo_ratings()}
    order = sorted(standings, key=lambda row: (
        row["win_pct"] if row["win_pct"] is not None else .5,
        row["point_diff"], elo.get(row["abbreviation"], 1500),
    ))
    established = _established_qbs(repository, season)
    roster_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with closing(repository._connect()) as connection:
        for player in connection.execute("SELECT team,position FROM players WHERE season=?", (season,)):
            raw = (player["position"] or "").upper()
            roster_counts[player["team"]][POSITION_GROUP.get(raw, raw)] += 1
    minimums = {"QB": 3, "RB": 4, "WR": 6, "TE": 3, "OL": 9, "EDGE": 4,
                "DT": 4, "LB": 5, "CB": 6, "S": 4}
    remaining = list(board); picks = []
    for pick, team in enumerate(order[:limit], 1):
        if not remaining:
            break
        code = team["abbreviation"]; window = remaining[:12]
        def need(entry):
            position = POSITION_GROUP.get((entry.get("position") or "").upper(),
                                          (entry.get("position") or "").upper())
            if position == "QB":
                return 0 if code in established else 100
            minimum = minimums.get(position, 4)
            count = roster_counts[code].get(position, 0)
            return max(20, min(85, 50 + (minimum - count) * 12))
        selected = max(window, key=lambda entry: need(entry) - 7 * window.index(entry))
        remaining.remove(selected)
        position = POSITION_GROUP.get((selected.get("position") or "").upper(),
                                      (selected.get("position") or "").upper())
        picks.append({"pick": pick, "team": code, "team_logo": team.get("logo_url"),
                      "team_color": team.get("color"), "player": selected["player_name"],
                      "school": selected["school"], "position": position,
                      "board_rank": selected["rank"], "need_score": need(selected),
                      "qb_filter": established.get(code),
                      "reason": ("QB suppressed: " + established[code]["player"]
                                 if position != "QB" and code in established
                                 else "best board/need fit in next 12 prospects")})
    return {"draft_year": draft_year, "picks": picks, "established_qbs": established,
            "note": "Order uses current record, point differential, then Elo. This is not a predictive mock draft."}
