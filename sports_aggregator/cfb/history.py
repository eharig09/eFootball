"""Derived historical game, matchup, and position-production context."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sports_aggregator.cfb.repository import CFBRepository


EASTERN = ZoneInfo("America/New_York")


def _local_start(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def time_slot(value: str) -> str:
    """Editorial kickoff bucket, based on the US Eastern broadcast window."""
    start = _local_start(value)
    weekday = start.strftime("%A")
    if weekday != "Saturday":
        return weekday
    minutes = start.hour * 60 + start.minute
    if minutes < 15 * 60 + 30:
        return "Saturday — day"
    if minutes < 19 * 60:
        return "Saturday — afternoon"
    if minutes < 22 * 60:
        return "Saturday — primetime"
    return "Saturday — late night"


def _perspective(game: dict[str, Any], team_id: int) -> dict[str, Any]:
    home = game["home_team_id"] == team_id
    points_for = game["home_points"] if home else game["away_points"]
    points_against = game["away_points"] if home else game["home_points"]
    opponent_id = game["away_team_id"] if home else game["home_team_id"]
    opponent = game["away_team"] if home else game["home_team"]
    opponent_conference = game["away_conference"] if home else game["home_conference"]
    if points_for is None or points_against is None:
        result = None
    elif points_for > points_against:
        result = "W"
    elif points_for < points_against:
        result = "L"
    else:
        result = "T"
    return {
        **game, "team_id": team_id, "opponent_id": opponent_id,
        "opponent": opponent, "opponent_conference": opponent_conference,
        "points_for": points_for, "points_against": points_against,
        "result": result, "site": "Neutral" if game["neutral_site"] else
        ("Home" if home else "Away"), "slot": time_slot(game["start_date"]),
    }


def _record(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = [row for row in rows if row.get("result")]
    wins = sum(row["result"] == "W" for row in items)
    losses = sum(row["result"] == "L" for row in items)
    ties = sum(row["result"] == "T" for row in items)
    games = len(items)
    points_for = sum(int(row["points_for"]) for row in items)
    points_against = sum(int(row["points_against"]) for row in items)
    return {
        "games": games, "wins": wins, "losses": losses, "ties": ties,
        "record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
        "ppg_for": round(points_for / games, 1) if games else None,
        "ppg_against": round(points_against / games, 1) if games else None,
        "average_margin": round((points_for - points_against) / games, 1) if games else None,
    }


def _series_spreads(repository: CFBRepository, game_ids: list[int]) -> dict[int, float]:
    """Closing home-signed spread per game (negative = home favoured): the consensus line when the provider has one,
    otherwise the mean across providers rounded to the half point. Games with no stored line are absent."""
    if not game_ids:
        return {}
    from sports_aggregator.cfb import lines
    lines.initialize(repository)          # once per process; the table is created lazily elsewhere
    placeholders = ",".join("?" for _ in game_ids)
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT game_id,
                       AVG(CASE WHEN provider='consensus' THEN spread END) consensus,
                       AVG(spread) average
                FROM game_lines WHERE spread IS NOT NULL AND game_id IN ({placeholders}) GROUP BY game_id""",
            game_ids).fetchall()
    return {int(row["game_id"]): float(row["consensus"]) if row["consensus"] is not None
            else round(float(row["average"]) * 2) / 2 for row in rows}


def _attach_ats(rows: list[dict[str, Any]], spreads: dict[int, float]) -> None:
    """Add `ats` ('W'/'L'/'P') and the team's own closing line (`line`, negative = favourite) to each row, in place."""
    for row in rows:
        spread = spreads.get(row["game_id"])
        if spread is None or row.get("points_for") is None or row.get("points_against") is None:
            row["ats"], row["line"] = None, None
            continue
        team_line = spread if row["home_team_id"] == row["team_id"] else -spread
        margin = float(row["points_for"]) - float(row["points_against"]) + team_line
        row["ats"] = "W" if margin > 0 else "L" if margin < 0 else "P"
        row["line"] = team_line


def _ats_record(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Against-the-spread record over the rows that have a stored line; `games` says how many that was."""
    items = [row for row in rows if row.get("ats")]
    wins = sum(row["ats"] == "W" for row in items)
    losses = sum(row["ats"] == "L" for row in items)
    pushes = sum(row["ats"] == "P" for row in items)
    return {"games": len(items), "wins": wins, "losses": losses, "pushes": pushes,
            "record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else "") if items else None}


def _streak(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    latest = sorted(rows, key=lambda row: row["start_date"], reverse=True)
    result = latest[0].get("result")
    if not result:
        return None
    length = 0
    for row in latest:
        if row.get("result") != result:
            break
        length += 1
    return f"{result}{length}"


def _completed_games(repository: CFBRepository, *, before: str | None = None,
                     team_ids: Iterable[int] | None = None
                     ) -> list[dict[str, Any]]:
    """Completed games, optionally scoped to the teams that actually need them.

    Every caller here only ever wants one or two teams' worth of history, not
    the whole database's -- so ``team_ids`` pushes that filter into SQL instead
    of pulling every completed game ever played and discarding most of it in
    Python.
    """
    repository.initialize()
    sql = "SELECT * FROM games WHERE completed=1 AND home_points IS NOT NULL AND away_points IS NOT NULL"
    params: list[Any] = []
    if before:
        sql += " AND start_date<?"
        params.append(before)
    if team_ids:
        ids = list(team_ids)
        placeholders = ",".join("?" for _ in ids)
        sql += f" AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))"
        params.extend(ids)
        params.extend(ids)
    sql += " ORDER BY start_date DESC"
    with repository._reader() as connection:
        return [dict(row) for row in connection.execute(sql, params)]


def matchup_history(repository: CFBRepository, game: dict[str, Any],
                    recent_limit: int = 10, team_recent_limit: int = 3) -> dict[str, Any]:
    """History packet for one scheduled or completed matchup."""
    away_id, home_id = game["away_team_id"], game["home_team_id"]
    all_games = _completed_games(repository, before=game["start_date"],
                                 team_ids=(away_id, home_id))
    meetings = [row for row in all_games if {row["home_team_id"], row["away_team_id"]}
                == {home_id, away_id}]
    away_meetings = [_perspective(row, away_id) for row in meetings]
    home_meetings = [_perspective(row, home_id) for row in meetings]
    spreads = _series_spreads(repository, [int(row["game_id"]) for row in meetings])
    _attach_ats(away_meetings, spreads)
    _attach_ats(home_meetings, spreads)
    away_all = [_perspective(row, away_id) for row in all_games
                if away_id in (row["home_team_id"], row["away_team_id"])]
    home_all = [_perspective(row, home_id) for row in all_games
                if home_id in (row["home_team_id"], row["away_team_id"])]
    for rows_ in (away_all, home_all):
        _attach_ats([row for row in rows_ if int(row["game_id"]) in spreads], spreads)
    slot = time_slot(game["start_date"])

    def context(team_id: int, rows: list[dict[str, Any]], opponent_conference: str | None,
                opponent_id: int) -> dict[str, Any]:
        slot_rows = [row for row in rows if row["slot"] == slot]
        target_site = "Neutral" if game["neutral_site"] else (
            "Home" if game["home_team_id"] == team_id else "Away")
        site_rows = [row for row in rows if row["opponent_id"] == opponent_id
                     and row["site"] == target_site]
        conference_rows = [row for row in rows
                           if opponent_conference and row["opponent_conference"] == opponent_conference]
        unique_opponents = len({row["opponent_id"] for row in rows})
        return {
            "overall": _record(rows), "slot": {**_record(slot_rows), "label": slot},
            "site": {**_record(site_rows), "label": target_site, "ats": _ats_record(site_rows)},
            "series_ats": _ats_record(row for row in rows if row["opponent_id"] == opponent_id),
            "conference": {**_record(conference_rows), "conference": opponent_conference},
            "unique_opponents": unique_opponents,
            "coach": _coach_record(repository, team_id, opponent_id,
                                    game["season"], game["start_date"]),
        }

    recent = []
    for row in sorted(away_meetings, key=lambda item: item["start_date"], reverse=True)[:recent_limit]:
        local = _local_start(row["start_date"])
        recent.append({
            **row, "date_label": local.strftime("%b %d, %Y"),
            "score": f"{row['points_for']}-{row['points_against']}",
            "game_url": f"/college-football/games/{row['game_id']}/box-score/",
        })

    def recent_team_games(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for row in sorted(rows, key=lambda item: item["start_date"], reverse=True)[
                :team_recent_limit]:
            local = _local_start(row["start_date"])
            result.append({
                **row,
                "date_label": local.strftime("%b %d, %Y"),
                "score": f"{row['points_for']}-{row['points_against']}",
                "game_url": f"/college-football/games/{row['game_id']}/box-score/",
            })
        return result

    coverage = sorted({row["season"] for row in all_games})
    return {
        "meetings": len(meetings), "first_meeting": min((row["season"] for row in meetings), default=None),
        "last_meeting": max((row["season"] for row in meetings), default=None),
        "away_record": {**_record(away_meetings), "streak": _streak(away_meetings), "ats": _ats_record(away_meetings)},
        "home_record": {**_record(home_meetings), "streak": _streak(home_meetings), "ats": _ats_record(home_meetings)},
        "away_context": context(away_id, away_all, game.get("home_conference"), home_id),
        "home_context": context(home_id, home_all, game.get("away_conference"), away_id),
        "slot": slot, "recent": recent,
        "away_recent": recent_team_games(away_all),
        "home_recent": recent_team_games(home_all),
        "coverage": {"from": min(coverage) if coverage else None,
                     "through": max(coverage) if coverage else None},
    }


def _coach_record(repository: CFBRepository, team_id: int, opponent_id: int,
                  season: int, before: str) -> dict[str, Any] | None:
    with repository._reader() as connection:
        current = connection.execute(
            """SELECT * FROM coach_seasons WHERE team_id=? AND season<=?
               ORDER BY season DESC LIMIT 1""", (team_id, season)).fetchone()
        if current is None:
            return None
        attributed = [dict(row) for row in connection.execute(
            "SELECT season,team_id FROM coach_seasons WHERE coach_id=?",
            (current["coach_id"],))]
        game_rows = [dict(row) for row in connection.execute(
            """SELECT * FROM games WHERE completed=1 AND start_date<? AND
               (home_team_id=? OR away_team_id=?)""",
            (before, opponent_id, opponent_id))]
    # A coach's record against an opponent follows him across jobs. CFBD's
    # season attribution is the source boundary, so a mid-season handoff is not
    # represented as exact unless the upstream season attribution is exact.
    coached = {(row["season"], row["team_id"]) for row in attributed}
    relevant = []
    for row in game_rows:
        for coached_season, coached_team in coached:
            if row["season"] != coached_season:
                continue
            if coached_team not in (row["home_team_id"], row["away_team_id"]):
                continue
            perspective = _perspective(row, coached_team)
            if perspective["opponent_id"] == opponent_id:
                relevant.append(perspective)
            break
    record = _record(relevant)
    return {**record, "coach_id": current["coach_id"],
            "name": f"{current['first_name']} {current['last_name']}".strip(),
            "through_season": current["season"], "attribution": "team-season"}


def team_game_history(repository: CFBRepository, team_id: int,
                      season: int | None = None) -> dict[str, Any]:
    team = repository.get_team(team_id)
    if team is None:
        return {"team": None, "games": [], "seasons": [], "summary": _record([])}
    games = _completed_games(repository, team_ids=(team_id,))
    with closing(repository._connect()) as connection:
        stored_seasons = {row["season"]: dict(row) for row in connection.execute(
            """SELECT r.*,a.offense_success_rate,a.defense_success_rate,
                      a.offense_ppa,a.defense_ppa
               FROM team_records r LEFT JOIN team_advanced_stats a
                 ON a.season=r.season AND a.team=r.team
               WHERE r.team_id=?""", (team_id,))}
    rows = [_perspective(row, team_id) for row in games
            if team_id in (row["home_team_id"], row["away_team_id"])]
    seasons = sorted({row["season"] for row in rows}, reverse=True)
    selected = [row for row in rows if season is None or row["season"] == season]
    for row in selected:
        local = _local_start(row["start_date"])
        row["date_label"] = local.strftime("%b %d, %Y")
        row["score"] = f"{row['points_for']}-{row['points_against']}"
        row["game_url"] = f"/college-football/games/{row['game_id']}/box-score/"
    by_season = []
    for year in seasons:
        year_rows = [row for row in rows if row["season"] == year]
        by_season.append({**stored_seasons.get(year, {}),
                          "season": year, **_record(year_rows)})
    return {"team": team, "games": selected, "seasons": seasons,
            "selected_season": season, "summary": _record(selected),
            "season_summaries": by_season,
            "unique_opponents": len({row["opponent_id"] for row in selected})}


POSITION_GROUPS = {
    "QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE",
    "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL", "G": "OL", "T": "OL",
    "DL": "DL", "DT": "DL", "DE": "EDGE", "EDGE": "EDGE",
    "LB": "LB", "ILB": "LB", "OLB": "LB",
    "CB": "SECONDARY", "S": "SECONDARY", "DB": "SECONDARY",
    "K": "SPECIALISTS", "P": "SPECIALISTS", "LS": "SPECIALISTS",
}


def team_historical_stats(repository: CFBRepository, team_id: int) -> dict[str, Any]:
    team = repository.get_team(team_id)
    if team is None:
        return {"team": None, "seasons": [], "positions": [], "identity": []}
    repository.initialize()
    with closing(repository._connect()) as connection:
        records = [dict(row) for row in connection.execute(
            """SELECT r.*,a.offense_success_rate,a.defense_success_rate,
                      a.offense_ppa,a.defense_ppa
               FROM team_records r LEFT JOIN team_advanced_stats a
                 ON a.season=r.season AND a.team=r.team
               WHERE r.team_id=? ORDER BY r.season DESC""", (team_id,))]
        games = [dict(row) for row in connection.execute(
            """SELECT * FROM games WHERE completed=1 AND
               (home_team_id=? OR away_team_id=?)""", (team_id, team_id))]
        stats = [dict(row) for row in connection.execute(
            """SELECT season,position,category,stat_type,SUM(numeric_value) value
               FROM player_season_stats WHERE team=? AND numeric_value IS NOT NULL
               GROUP BY season,position,category,stat_type""", (team["school"],))]
        pff = [dict(row) for row in connection.execute(
            """SELECT season,position_group,dataset,weighted_grade,player_count
               FROM pff_position_groups WHERE cfbd_team_id=? AND weighted_grade IS NOT NULL
               ORDER BY season DESC""", (team_id,))]
        raw_team_stats = [dict(row) for row in connection.execute(
            """SELECT season,stat_name,stat_value FROM team_stats
               WHERE team=? ORDER BY season DESC,stat_name""", (team["school"],))]
    game_rows = [_perspective(row, team_id) for row in games]
    season_games: dict[int, list[dict[str, Any]]] = {}
    for row in game_rows:
        season_games.setdefault(row["season"], []).append(row)
    record_by_year = {row["season"]: row for row in records}
    years = sorted(set(record_by_year) | set(season_games), reverse=True)
    seasons = []
    for year in years:
        derived = _record(season_games.get(year, []))
        stored = record_by_year.get(year, {})
        seasons.append({**stored, **derived, "season": year})

    stat_pivot: dict[int, dict[str, float]] = {}
    for row in raw_team_stats:
        try:
            value = float(row["stat_value"])
        except (TypeError, ValueError):
            continue
        stat_pivot.setdefault(row["season"], {})[row["stat_name"]] = value
    team_stats = []
    for year in sorted(stat_pivot, reverse=True):
        values = stat_pivot[year]
        games_played = len(season_games.get(year, [])) or None
        per_game = lambda name: (round(values[name] / games_played, 1)
                                 if games_played and name in values else None)
        team_stats.append({
            "season": year, "games": games_played,
            "yards_per_game": per_game("totalYards"),
            "pass_yards_per_game": per_game("netPassingYards"),
            "rush_yards_per_game": per_game("rushingYards"),
            "opponent_yards_per_game": per_game("totalYardsOpponent"),
            "sacks": values.get("sacks"), "tackles_for_loss": values.get("tacklesForLoss"),
            "turnover_margin": ((values.get("turnoversOpponent") or 0) -
                                (values.get("turnovers") or 0)
                                if "turnoversOpponent" in values and "turnovers" in values
                                else None),
        })

    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    for row in stats:
        group = POSITION_GROUPS.get(str(row.get("position") or "").upper(), "OTHER")
        entry = grouped.setdefault((row["season"], group), {
            "season": row["season"], "position_group": group,
            "pass_yards": 0.0, "rush_yards": 0.0, "receiving_yards": 0.0,
            "touchdowns": 0.0, "receptions": 0.0, "tackles": 0.0,
            "sacks": 0.0, "interceptions": 0.0,
        })
        category = str(row["category"]).casefold()
        stat_type = str(row["stat_type"]).upper()
        value = float(row["value"] or 0)
        if category == "passing" and stat_type == "YDS": entry["pass_yards"] += value
        if category == "rushing" and stat_type == "YDS": entry["rush_yards"] += value
        if category == "receiving" and stat_type == "YDS": entry["receiving_yards"] += value
        if stat_type in ("TD", "TDS"): entry["touchdowns"] += value
        if category == "receiving" and stat_type in ("REC", "RECEPTIONS"): entry["receptions"] += value
        if stat_type in ("TOT", "TOTAL", "TACKLES"): entry["tackles"] += value
        if stat_type in ("SACKS", "SACK"): entry["sacks"] += value
        if category in ("interceptions", "defensive") and stat_type in ("INT", "INTS"):
            entry["interceptions"] += value
    totals: dict[int, dict[str, float]] = {}
    for (year, _group), row in grouped.items():
        total = totals.setdefault(year, {"rush_yards": 0, "receiving_yards": 0,
                                         "tackles": 0, "sacks": 0})
        for name in total:
            total[name] += row[name]
    pff_by_group: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in pff:
        production_group = {"INTERIOR_DL": "DL"}.get(
            row["position_group"], row["position_group"])
        pff_by_group.setdefault((row["season"], production_group), []).append(row)
    for key in pff_by_group:
        if key not in grouped:
            grouped[key] = {
                "season": key[0], "position_group": key[1],
                "pass_yards": 0.0, "rush_yards": 0.0, "receiving_yards": 0.0,
                "touchdowns": 0.0, "receptions": 0.0, "tackles": 0.0,
                "sacks": 0.0, "interceptions": 0.0,
            }
            totals.setdefault(key[0], {"rush_yards": 0, "receiving_yards": 0,
                                       "tackles": 0, "sacks": 0})
    positions = []
    for key, row in sorted(grouped.items(), key=lambda item: (-item[0][0], item[0][1])):
        grades = pff_by_group.get(key, [])
        row["pff_grade"] = max((item["weighted_grade"] for item in grades), default=None)
        row["pff_detail"] = "; ".join(
            f"{item['dataset'].replace('_', ' ')} {item['weighted_grade']:.1f}"
            for item in sorted(grades, key=lambda item: -item["weighted_grade"])) or None
        row["pff_samples"] = sum(item["player_count"] for item in grades) if grades else None
        for stat in ("rush_yards", "receiving_yards", "tackles", "sacks"):
            denominator = totals[row["season"]][stat]
            row[f"{stat}_share"] = round(100 * row[stat] / denominator, 1) if denominator else None
        positions.append(row)
    latest = max((row["season"] for row in positions), default=None)
    identity = [row for row in positions if row["season"] == latest]
    return {"team": team, "seasons": seasons, "team_stats": team_stats,
            "positions": positions,
            "identity": identity, "latest_production_season": latest,
            "pff_seasons": sorted({row["season"] for row in pff}, reverse=True)}


def _game_stat_lines(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    games: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["game_id"], row["player_id"])
        item = games.setdefault(key, {
            "game_id": row["game_id"], "season": row["season"],
            "start_date": row["start_date"], "player_id": row["player_id"],
            "player": row["player"], "position": row.get("position"),
            "team": row["box_team"], "team_id": row.get("box_team_id"),
            "home_team_id": row["home_team_id"], "home_team": row["home_team"],
            "away_team_id": row["away_team_id"], "away_team": row["away_team"],
            "stats": {},
        })
        item["stats"][(str(row["category"]).casefold(),
                       str(row["stat_type"]).upper())] = row["stat_value"]

    def value(item: dict[str, Any], category: str, *types: str) -> str | None:
        for stat_type in types:
            found = item["stats"].get((category, stat_type))
            if found not in (None, "", "0", "0.0"):
                return str(found)
        return None

    results = []
    for item in games.values():
        team_id = item.get("team_id")
        if team_id == item["home_team_id"]:
            opponent_id, opponent = item["away_team_id"], item["away_team"]
        else:
            opponent_id, opponent = item["home_team_id"], item["home_team"]
        passing = value(item, "passing", "C/ATT", "COMPLETIONS/ATTEMPTS")
        passing_yards = value(item, "passing", "YDS")
        passing_td = value(item, "passing", "TD")
        passing_int = value(item, "passing", "INT")
        rushing_att = value(item, "rushing", "CAR", "ATT")
        rushing_yards = value(item, "rushing", "YDS")
        rushing_td = value(item, "rushing", "TD")
        receptions = value(item, "receiving", "REC")
        receiving_yards = value(item, "receiving", "YDS")
        receiving_td = value(item, "receiving", "TD")
        tackles = value(item, "defensive", "TOT", "TACKLES")
        sacks = value(item, "defensive", "SACKS", "SACK")
        interceptions = (value(item, "interceptions", "INT") or
                         value(item, "defensive", "INT"))
        local = _local_start(item["start_date"])
        # `stats` is the lookup index this function builds for `value()` above,
        # keyed by (category, stat_type) tuples. Spreading it into the result
        # leaked an internal structure into every packet, and a tuple key is not
        # representable in JSON, so /api/v1/cfb/games/<id>/preview raised
        # "keys must be str, int, float, bool or None, not tuple" rather than
        # returning. Nothing downstream reads it; the derived lines below are
        # the output.
        emitted = {key: field for key, field in item.items() if key != "stats"}
        results.append({
            **emitted, "opponent_id": opponent_id, "opponent": opponent,
            "date_label": local.strftime("%b %d, %Y"),
            "passing": " / ".join(filter(None, (
                passing, f"{passing_yards} yd" if passing_yards else None,
                f"{passing_td} TD" if passing_td else None,
                f"{passing_int} INT" if passing_int else None))) or None,
            "rushing": " / ".join(filter(None, (
                f"{rushing_att} car" if rushing_att else None,
                f"{rushing_yards} yd" if rushing_yards else None,
                f"{rushing_td} TD" if rushing_td else None))) or None,
            "receiving": " / ".join(filter(None, (
                f"{receptions} rec" if receptions else None,
                f"{receiving_yards} yd" if receiving_yards else None,
                f"{receiving_td} TD" if receiving_td else None))) or None,
            "defense": " / ".join(filter(None, (
                f"{tackles} tkl" if tackles else None,
                f"{sacks} sacks" if sacks else None,
                f"{interceptions} INT" if interceptions else None))) or None,
            "game_url": f"/college-football/games/{item['game_id']}/box-score/",
        })
    return sorted(results, key=lambda item: item["start_date"], reverse=True)


def player_vs_opponent_history(repository: CFBRepository, player_id: str,
                               opponent_id: int, before: str) -> list[dict[str, Any]]:
    repository.initialize()
    with closing(repository._connect()) as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT b.*,b.team box_team,b.team_id box_team_id,g.season,g.start_date,
                      g.home_team_id,g.home_team,g.away_team_id,g.away_team,
                      p.position
               FROM game_player_box_stats b JOIN games g ON g.game_id=b.game_id
               LEFT JOIN players p ON p.player_id=b.player_id AND p.season=g.season
                    AND p.team=b.team
               WHERE b.player_id=? AND g.start_date<? AND
                     (g.home_team_id=? OR g.away_team_id=?)""",
            (player_id, before, opponent_id, opponent_id))]
    return [row for row in _game_stat_lines(rows) if row["opponent_id"] == opponent_id]


def matchup_player_history(repository: CFBRepository, game: dict[str, Any]) -> list[dict[str, Any]]:
    repository.initialize()
    with repository._reader() as connection:
        roster = {row["player_id"]: dict(row) for row in connection.execute(
            """SELECT p.player_id,p.first_name||' '||p.last_name player,p.position,t.team_id
               FROM players p JOIN teams t ON t.school=p.team
               WHERE p.season=? AND t.team_id IN (?,?)""",
            (game["season"], game["home_team_id"], game["away_team_id"]))}
        identifiers = list(roster)
        placeholders = ",".join("?" for _ in identifiers) or "NULL"
        home_id, away_id = game["home_team_id"], game["away_team_id"]
        # Every row that survives the loop below has opponent_id equal to
        # whichever of these two teams the player *isn't* on -- so the game
        # always involves one of them. Filtering on that in SQL avoids
        # pulling a roster's entire career box-score history just to throw
        # most of it away in Python afterward.
        rows = [dict(row) for row in connection.execute(
            f"""SELECT b.*,b.team box_team,b.team_id box_team_id,g.season,g.start_date,
                       g.home_team_id,g.home_team,g.away_team_id,g.away_team,
                       p.position
                FROM game_player_box_stats b JOIN games g ON g.game_id=b.game_id
                LEFT JOIN players p ON p.player_id=b.player_id AND p.season=?
                     AND p.team=b.team
                WHERE b.player_id IN ({placeholders}) AND g.start_date<?
                     AND (g.home_team_id IN (?,?) OR g.away_team_id IN (?,?))""",
            (game["season"], *identifiers, game["start_date"],
             home_id, away_id, home_id, away_id))]
    lines = _game_stat_lines(rows)
    result = []
    for row in lines:
        current = roster.get(row["player_id"])
        if not current:
            continue
        target_opponent = (game["away_team_id"] if current["team_id"] == game["home_team_id"]
                           else game["home_team_id"])
        if row["opponent_id"] != target_opponent:
            continue
        row["player"] = current["player"]
        row["position"] = current["position"]
        row["current_team_id"] = current["team_id"]
        result.append(row)
    return result


def upcoming_player_opponent_history(repository: CFBRepository, player_id: str,
                                     team_id: int, season: int) -> dict[str, Any]:
    repository.initialize()
    # Game timestamps are stored as UTC ISO strings. Keep the lexical SQLite
    # comparison in the same offset instead of comparing UTC to Eastern text.
    now = datetime.now(tz=ZoneInfo("UTC")).isoformat()
    with closing(repository._connect()) as connection:
        row = connection.execute(
            """SELECT * FROM games WHERE season=? AND completed=0 AND start_date>=?
               AND (home_team_id=? OR away_team_id=?) ORDER BY start_date LIMIT 1""",
            (season, now, team_id, team_id)).fetchone()
    if row is None:
        return {"game": None, "performances": []}
    game = dict(row)
    opponent_id = (game["away_team_id"] if game["home_team_id"] == team_id
                   else game["home_team_id"])
    opponent = game["away_team"] if game["home_team_id"] == team_id else game["home_team"]
    return {"game": game, "opponent_id": opponent_id, "opponent": opponent,
            "performances": player_vs_opponent_history(
                repository, player_id, opponent_id, game["start_date"])}
