"""Presentation packets for NFL HTML pages and JSON APIs."""

from __future__ import annotations

from datetime import date
import json
from urllib.parse import quote

from sports_aggregator.tables import Column, Table, format_value


def _score(value):
    return int(value) if value is not None else None


def schedule_table(games: list[dict], *, team: str | None = None,
                   identities: dict[str, dict] | None = None) -> Table:
    identities = identities or {}
    rows = []
    for game in games:
        opponent = None
        result = None
        if team:
            opponent = game["home_team"] if game["away_team"] == team else game["away_team"]
            if game["completed"]:
                own = game["away_score"] if game["away_team"] == team else game["home_score"]
                other = game["home_score"] if game["away_team"] == team else game["away_score"]
                result = "W" if own > other else ("L" if own < other else "T")
        score = (f"{_score(own)}–{_score(other)}" if team and game["completed"] else None)
        rows.append({
            "week": game["week"], "date": game["game_date"], "time": game["game_time"],
            "away": game["away_team"], "away_score": _score(game["away_score"]),
            "home": game["home_team"], "home_score": _score(game["home_score"]),
            "opponent": opponent, "result": result, "score": score,
            "date_url": (f"/nfl/games/{quote(str(game['game_id']), safe='')}/" +
                         ("#review" if game["completed"] else "#overview")),
            "away_url": f"/nfl/teams/{game['away_team']}/",
            "home_url": f"/nfl/teams/{game['home_team']}/",
            "opponent_url": f"/nfl/teams/{opponent}/" if opponent else None,
            "away_logo": identities.get(game["away_team"], {}).get("logo_url"),
            "away_color": identities.get(game["away_team"], {}).get("color"),
            "home_logo": identities.get(game["home_team"], {}).get("logo_url"),
            "home_color": identities.get(game["home_team"], {}).get("color"),
            "opponent_logo": identities.get(opponent, {}).get("logo_url") if opponent else None,
            "opponent_color": identities.get(opponent, {}).get("color") if opponent else None,
            "result_class": result.lower() if result else "pending",
        })
    columns = (
        Column("week", "Week", "int"), Column("date", "Date"),
        Column("opponent", "Opponent"), Column("result", "Result"),
        Column("score", "Score"),
    ) if team else (
        Column("week", "Week", "int"), Column("date", "Date"), Column("time", "Time"),
        Column("away", "Away"), Column("away_score", "Score", "int"),
        Column("home", "Home"), Column("home_score", "Score", "int"),
    )
    return Table(columns, rows, caption="Season schedule",
                 note="Team marks and color rails link directly to each club page.",
                 empty="No NFL schedule has been synced for this season.")


def leaders_table(rows: list[dict], label: str, *, season: int | None = None) -> Table:
    for row in rows:
        suffix = f"?season={season}" if season is not None else ""
        row["player_name_url"] = f"/nfl/players/{quote(str(row['player_id']), safe='')}/{suffix}"
        row["team_url"] = f"/nfl/teams/{row['team']}/"
    return Table(
        (Column("player_name", "Player"), Column("team", "Team"),
         Column("position", "Pos."), Column("games", "Games", "int"),
         Column("value", label, "big", emphasis=True)),
        rows, caption=label, empty="No weekly player statistics have been synced yet.", dense=True,
    )


def usage_table(rows: list[dict], *, caption: str = "Opportunity leaders") -> Table:
    for row in rows:
        row["player_name_url"] = row.get("player_url")
    return Table(
        (Column("player_name", "Player"), Column("position", "Pos."),
         Column("games", "G", "int"), Column("targets", "Tgt", "int"),
         Column("target_share", "Tgt share", "rate"),
         Column("carries", "Car", "int"), Column("carry_share", "Carry share", "rate"),
         Column("opportunity_share", "Opp. share", "rate", emphasis=True),
         Column("scrimmage_yards", "Scrim yds", "big"),
         Column("first_downs", "1D", "int"),
         Column("explosive_touches", "20+", "int")),
        rows, caption=caption,
        note="Targets plus carries define opportunities. Shares are calculated within the listed team-season sample.",
        empty="No target or carry workload is available for this team.", dense=True,
    )


def pff_leaders_table(rows: list[dict], *, caption: str, season: int) -> Table:
    for row in rows:
        if row.get("gsis_id"):
            row["player_name_url"] = f"/nfl/players/{quote(str(row['gsis_id']), safe='')}/?season={season}"
        row["team_url"] = f"/nfl/teams/{row['team']}/?season={season}"
    return Table(
        (Column("player_name", "Player"), Column("team", "Team"),
         Column("position", "Pos."), Column("value", "Value", "f1", emphasis=True),
         Column("key_source", "Match")),
        rows, caption=caption,
        note=f"PFF {season} season aggregate. Identity joins use PFF ID first and name/team only as a labeled fallback.",
        empty="No PFF rows are available for this selection.", dense=True,
    )


def roster_table(rows: list[dict]) -> Table:
    for row in rows:
        row["full_name_url"] = f"/nfl/players/{quote(str(row['player_id']), safe='')}/?season={row['season']}"
    return Table(
        (Column("jersey_number", "No.", "int"), Column("full_name", "Player"),
         Column("position", "Pos."), Column("depth_position", "Role"),
         Column("status", "Status"), Column("years_experience", "Exp.", "int"),
         Column("college", "College")),
        rows, caption="Roster", empty="No roster has been synced for this team and season.", dense=True,
    )


def movement_tables(packet: dict) -> dict[str, Table]:
    tables = {}
    for key, label, empty in (
        ("arrivals", "Arrivals", "No arrivals were found against the prior roster release."),
        ("departures", "Departures", "No departures were found against the prior roster release."),
    ):
        rows = packet.get(key, [])
        for row in rows:
            row["full_name_url"] = (
                f"/nfl/players/{quote(str(row['player_id']), safe='')}/"
                f"?season={packet['season'] if key == 'arrivals' else packet['prior_season']}"
            )
        tables[key] = Table(
            (Column("full_name", "Player", emphasis=True), Column("position", "Pos."),
             Column("movement", "Type"), Column("detail", "Roster path"),
             Column("status", "Status")),
            rows, caption=f"{label}: {packet['prior_season']} → {packet['season']}",
            note="Exact GSIS-ID comparison between adjacent nflverse roster releases.",
            empty=empty, dense=True,
        )
    return tables


def depth_chart_table(rows: list[dict]) -> Table:
    for row in rows:
        if row.get("gsis_id"):
            row["player_name_url"] = f"/nfl/players/{quote(str(row['gsis_id']), safe='')}/"
    snapshot = rows[0]["snapshot_at"] if rows else None
    return Table(
        (Column("position_group", "Unit"), Column("position_abbreviation", "Slot"),
         Column("position_rank", "Rank", "int"), Column("player_name", "Player")),
        rows, caption="Current depth chart",
        note=f"Source: ESPN via nflverse · snapshot {snapshot}" if snapshot else None,
        empty="No depth-chart snapshot has been synced for this team.", dense=True,
    )


def snap_usage_table(rows: list[dict]) -> Table:
    for row in rows:
        if row.get("player_id"):
            row["player_name_url"] = f"/nfl/players/{quote(str(row['player_id']), safe='')}/"
    return Table(
        (Column("player_name", "Player"), Column("position", "Pos."),
         Column("games", "Games", "int"), Column("offense_snaps", "Off. snaps", "big"),
         Column("offense_pct", "Off. share", "rate"),
         Column("defense_snaps", "Def. snaps", "big"), Column("defense_pct", "Def. share", "rate"),
         Column("st_snaps", "ST snaps", "big"), Column("st_pct", "ST share", "rate")),
        rows, caption="Season snap usage",
        note="Source: Pro Football Reference via nflverse. Shares average available game-level percentages.",
        empty="No snap counts have been synced for this team.", dense=True,
    )


def game_stats_table(rows: list[dict]) -> Table:
    useful = [row for row in rows if any(row.get(metric) for metric in (
        "passing_yards", "rushing_yards", "receiving_yards", "def_tackles_solo",
        "fg_made", "punt_return_yards", "kickoff_return_yards",
    ))]
    keys = ("player_name", "team", "position", "passing_yards", "passing_tds",
            "rushing_yards", "rushing_tds", "receptions", "receiving_yards",
            "receiving_tds", "def_tackles_solo", "def_sacks", "def_interceptions")
    projected = []
    for source in useful:
        row = {key: source.get(key) for key in keys}
        row["player_name_url"] = f"/nfl/players/{quote(str(source['player_id']), safe='')}/"
        row["team_url"] = f"/nfl/teams/{source['team']}/"
        projected.append(row)
    return Table(
        (Column("player_name", "Player"), Column("team", "Team"), Column("position", "Pos."),
         Column("passing_yards", "Pass yd", "int"), Column("passing_tds", "Pass TD", "int"),
         Column("rushing_yards", "Rush yd", "int"), Column("rushing_tds", "Rush TD", "int"),
         Column("receptions", "Rec", "int"), Column("receiving_yards", "Rec yd", "int"),
         Column("receiving_tds", "Rec TD", "int"), Column("def_tackles_solo", "Solo", "int"),
         Column("def_sacks", "Sacks", "f1"), Column("def_interceptions", "INT", "int")),
        projected, caption="Player box score", empty="No player statistics are available for this game.", dense=True,
    )


def game_stat_tables(rows: list[dict]) -> list[dict]:
    """Split a box score into readable football units instead of one 13-column sheet."""
    definitions = (
        ("Passing", "passing_yards", (
            Column("player_name", "Player"), Column("team", "Team"),
            Column("completions", "Cmp", "int"), Column("attempts", "Att", "int"),
            Column("passing_yards", "Yds", "int", emphasis=True),
            Column("passing_tds", "TD", "int"), Column("passing_interceptions", "INT", "int"),
        )),
        ("Rushing", "rushing_yards", (
            Column("player_name", "Player"), Column("team", "Team"),
            Column("carries", "Car", "int"), Column("rushing_yards", "Yds", "int", emphasis=True),
            Column("rushing_tds", "TD", "int"), Column("rushing_first_downs", "1D", "int"),
        )),
        ("Receiving", "receiving_yards", (
            Column("player_name", "Player"), Column("team", "Team"),
            Column("targets", "Tgt", "int"), Column("receptions", "Rec", "int"),
            Column("receiving_yards", "Yds", "int", emphasis=True),
            Column("receiving_tds", "TD", "int"), Column("receiving_first_downs", "1D", "int"),
        )),
        ("Defense", "def_tackles_solo", (
            Column("player_name", "Player"), Column("team", "Team"),
            Column("def_tackles_solo", "Solo", "int", emphasis=True),
            Column("def_tackle_assists", "Ast", "int"), Column("def_sacks", "Sack", "f1"),
            Column("def_qb_hits", "QB hit", "int"), Column("def_interceptions", "INT", "int"),
        )),
    )
    groups = []
    for label, primary, columns in definitions:
        keys = tuple(column.key for column in columns)
        projected = []
        for source in rows:
            if not any(source.get(column.key) for column in columns[2:]):
                continue
            row = {key: source.get(key) for key in keys}
            row["player_name_url"] = f"/nfl/players/{quote(str(source['player_id']), safe='')}/"
            row["team_url"] = f"/nfl/teams/{source['team']}/"
            projected.append(row)
        projected.sort(key=lambda row: (-(row.get(primary) or 0), row.get("player_name") or ""))
        if projected:
            groups.append({"label": label, "table": Table(
                columns, projected, caption=f"Player box score — {label}", dense=True,
            )})
    return groups


def player_game_log(rows: list[dict]) -> Table:
    keys = ("week", "team", "opponent_team", "passing_yards", "passing_tds",
            "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards",
            "receiving_tds", "def_tackles_solo", "def_sacks", "def_interceptions")
    projected = []
    for source in rows:
        row = {key: source.get(key) for key in keys}
        row["week_url"] = f"/nfl/games/{quote(str(source['game_id']), safe='')}/"
        row["team_url"] = f"/nfl/teams/{source['team']}/"
        row["opponent_team_url"] = f"/nfl/teams/{source['opponent_team']}/"
        projected.append(row)
    return Table(
        (Column("week", "Week", "int"), Column("team", "Team"), Column("opponent_team", "Opp."),
         Column("passing_yards", "Pass yd", "int"), Column("passing_tds", "Pass TD", "int"),
         Column("rushing_yards", "Rush yd", "int"), Column("rushing_tds", "Rush TD", "int"),
         Column("targets", "Tgt", "int"), Column("receptions", "Rec", "int"),
         Column("receiving_yards", "Rec yd", "int"), Column("receiving_tds", "Rec TD", "int"),
         Column("def_tackles_solo", "Solo", "int"), Column("def_sacks", "Sacks", "f1"),
         Column("def_interceptions", "INT", "int")),
        projected, caption="Game log", empty="No weekly statistics are available for this player.", dense=True,
    )


def player_game_log_tables(rows: list[dict], *, show_season: bool = False) -> list[dict]:
    """Return only the stat-family tables relevant to a player's actual season.

    `show_season=True` (the career view, which spans many seasons) prepends
    a Season column to every family table so rows stay identifiable once
    "Week 1" no longer means one specific game.
    """
    season_column = (Column("season", "Season", "int"),) if show_season else ()
    definitions = (
        ("Passing", ("completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions"), (
            *season_column, Column("week", "Week", "int"), Column("opponent_team", "Opp."),
            Column("completions", "Cmp", "int"), Column("attempts", "Att", "int"),
            Column("passing_yards", "Yds", "int", emphasis=True),
            Column("passing_tds", "TD", "int"), Column("passing_interceptions", "INT", "int"),
            Column("passing_epa", "EPA", "signed2"), Column("passing_cpoe", "CPOE", "signed2"),
            Column("passing_air_yards", "Air yd", "int"),
        )),
        ("Rushing", ("carries", "rushing_yards", "rushing_tds"), (
            *season_column, Column("week", "Week", "int"), Column("opponent_team", "Opp."),
            Column("carries", "Car", "int"), Column("rushing_yards", "Yds", "int", emphasis=True),
            Column("rushing_tds", "TD", "int"), Column("rushing_first_downs", "1D", "int"),
            Column("rushing_epa", "EPA", "signed2"), Column("rushing_fumbles_lost", "Fum", "int"),
        )),
        ("Receiving", ("targets", "receptions", "receiving_yards", "receiving_tds"), (
            *season_column, Column("week", "Week", "int"), Column("opponent_team", "Opp."),
            Column("targets", "Tgt", "int"), Column("receptions", "Rec", "int"),
            Column("receiving_yards", "Yds", "int", emphasis=True),
            Column("receiving_tds", "TD", "int"), Column("receiving_first_downs", "1D", "int"),
            Column("receiving_air_yards", "Air yd", "int"),
            Column("receiving_yards_after_catch", "YAC", "int"),
            Column("target_share", "Tgt share", "rate"), Column("receiving_epa", "EPA", "signed2"),
        )),
        ("Defense", ("def_tackles_solo", "def_sacks", "def_interceptions"), (
            *season_column, Column("week", "Week", "int"), Column("opponent_team", "Opp."),
            Column("def_tackles_solo", "Solo", "int", emphasis=True),
            Column("def_tackle_assists", "Ast", "int"), Column("def_sacks", "Sack", "f1"),
            Column("def_qb_hits", "QB hit", "int"), Column("def_interceptions", "INT", "int"),
            Column("def_tackles_for_loss", "TFL", "int"), Column("def_pass_defended", "PD", "int"),
            Column("def_fumbles_forced", "FF", "int"),
        )),
        ("Kicking", ("fg_att", "fg_made", "pat_att", "pat_made"), (
            *season_column, Column("week", "Week", "int"), Column("opponent_team", "Opp."),
            Column("fg_made", "FGM", "int"), Column("fg_att", "FGA", "int"),
            Column("fg_long", "Long", "int"), Column("pat_made", "XPM", "int"),
            Column("pat_att", "XPA", "int"),
        )),
    )
    groups = []
    for label, signals, columns in definitions:
        if not any(any(row.get(key) for key in signals) for row in rows):
            continue
        keys = tuple(column.key for column in columns)
        projected = []
        for source in rows:
            if not any(source.get(key) for key in signals):
                continue
            row = {key: source.get(key) for key in keys}
            row["week_url"] = f"/nfl/games/{quote(str(source['game_id']), safe='')}/"
            row["opponent_team_url"] = f"/nfl/teams/{source['opponent_team']}/"
            projected.append(row)
        groups.append({"label": label, "table": Table(
            columns, projected, caption=f"{label} game log", dense=True,
            empty=f"No {label.lower()} statistics are available.",
        )})
    return groups


def player_headline_stats(totals: dict[str, float], position: str | None) -> list[dict]:
    position = (position or "").upper()
    if position == "QB":
        definitions = (("Pass yards", "passing_yards", "big"), ("Pass TD", "passing_tds", "int"),
                       ("Cmp", "completions", "big"), ("Att", "attempts", "big"),
                       ("INT", "passing_interceptions", "int"), ("Rush yards", "rushing_yards", "big"))
    elif position in {"RB", "FB"}:
        definitions = (("Rush yards", "rushing_yards", "big"), ("Carries", "carries", "big"),
                       ("Rush TD", "rushing_tds", "int"), ("Receptions", "receptions", "big"),
                       ("Rec yards", "receiving_yards", "big"), ("Rec TD", "receiving_tds", "int"))
    elif position in {"WR", "TE"}:
        definitions = (("Rec yards", "receiving_yards", "big"), ("Receptions", "receptions", "big"),
                       ("Targets", "targets", "big"), ("Rec TD", "receiving_tds", "int"),
                       ("Air yards", "receiving_air_yards", "big"), ("YAC", "receiving_yards_after_catch", "big"))
    elif position in {"K", "PK"}:
        definitions = (("FG made", "fg_made", "int"), ("FG attempts", "fg_att", "int"),
                       ("Long", "fg_long", "int"), ("PAT made", "pat_made", "int"))
    else:
        definitions = (("Solo tackles", "def_tackles_solo", "big"),
                       ("Assists", "def_tackle_assists", "big"), ("Sacks", "def_sacks", "f1"),
                       ("QB hits", "def_qb_hits", "big"), ("INT", "def_interceptions", "int"),
                       ("Pass defended", "def_pass_defended", "big"))
    return [{"label": label, "value": totals[key], "format": value_format}
            for label, key, value_format in definitions if totals.get(key) is not None]


def player_totals_table(totals: dict[str, float]) -> Table:
    labels = {
        "passing_yards": "Passing yards", "passing_tds": "Passing touchdowns",
        "passing_interceptions": "Interceptions thrown", "rushing_yards": "Rushing yards",
        "rushing_tds": "Rushing touchdowns", "receptions": "Receptions",
        "receiving_yards": "Receiving yards", "receiving_tds": "Receiving touchdowns",
        "targets": "Targets", "def_tackles_solo": "Solo tackles", "def_sacks": "Sacks",
        "def_interceptions": "Defensive interceptions", "fg_made": "Field goals made",
    }
    rows = [{"metric": label, "value": totals[key]} for key, label in labels.items()
            if key in totals and totals[key] != 0]
    return Table(
        (Column("metric", "Metric"), Column("value", "Season total", "num", emphasis=True)),
        rows, caption="Season production", empty="No season totals are available for this player.",
        sortable=False,
    )


def efficiency_table(rows: list[dict], *, caption: str = "Play-by-play efficiency") -> Table:
    for index, row in enumerate(rows, 1):
        row.setdefault("rank", index)
        row["team_url"] = f"/nfl/teams/{row['team']}/"
    columns = [Column("rank", "Rank", "rank"), Column("team", "Team"),
         Column("plays", "Plays", "big"), Column("epa_per_play", "EPA/play", "signed2", emphasis=True),
         Column("success_rate", "Success", "rate"),
         Column("pass_epa_per_play", "Dropback EPA", "signed2"),
         Column("rush_epa_per_play", "Rush EPA", "signed2"),
         Column("early_down_epa", "Early-down EPA", "signed2"),
         Column("explosive_rate", "Explosive", "rate")]
    if any("defensive_epa_allowed" in row for row in rows):
        columns.extend((Column("defensive_epa_allowed", "EPA allowed", "signed2"),
                        Column("net_epa", "Net EPA", "signed2", emphasis=True)))
    return Table(
        tuple(columns),
        rows, caption=caption,
        note="Source: nflverse play-by-play. EPA fields are per play; explosive rate uses gains of 10+ yards.",
        empty="No play-by-play efficiency has been synced for this season.", dense=True,
    )


def power_table(rows: list[dict], *, season: int | None = None) -> Table:
    """A dashboard-scale team ranking that fits without hiding its meaning."""
    for index, row in enumerate(rows, 1):
        row.setdefault("rank", index)
        suffix = f"?season={season}" if season is not None else ""
        row["team_url"] = f"/nfl/teams/{row['team']}/{suffix}"
    return Table(
        (Column("rank", "", "rank"), Column("team", "Team"),
         Column("elo_rating", "Elo", "int", title="Continuous rating replayed from 2010"),
         Column("net_epa", "Net", "signed2", emphasis=True,
                title="Offensive EPA per play minus defensive EPA allowed"),
         Column("epa_per_play", "Off EPA", "signed2"),
         Column("defensive_epa_allowed", "Def EPA", "signed2"),
         Column("pass_epa_per_play", "Pass", "signed2"),
         Column("rush_epa_per_play", "Rush", "signed2")),
        rows, caption="EPA power ranking",
        note="Per-play, scrimmage-only nflverse EPA. Lower defensive EPA is better.",
        empty="No play-by-play efficiency has been synced for this season.", dense=True,
    )


def standings_tables(rows: list[dict]) -> list[dict]:
    divisions: dict[str, list[dict]] = {}
    for row in rows:
        row["team"] = row["abbreviation"]
        row["team_url"] = f"/nfl/teams/{row['abbreviation']}/"
        row["team_logo"] = row.get("logo_url")
        row["team_color"] = row.get("color")
        divisions.setdefault(row["division"], []).append(row)
    ordered = ("AFC East", "AFC North", "AFC South", "AFC West",
               "NFC East", "NFC North", "NFC South", "NFC West")
    tables = []
    for division in ordered:
        division_rows = divisions.get(division, [])
        for rank, row in enumerate(division_rows, 1):
            row["rank"] = rank
        tables.append({"division": division, "conference": division[:3], "table": Table(
            (Column("rank", "", "rank"), Column("team", "Team"),
             Column("wins", "W", "int"), Column("losses", "L", "int"),
             Column("ties", "T", "int"), Column("win_pct", "Pct", "f3"),
             Column("point_diff", "+/-", "signed"), Column("streak", "Strk")),
            division_rows, caption=division,
            empty="No completed regular-season games are available.", dense=True,
        )})
    return tables


def current_games(games: list[dict], *, today: date | None = None, limit: int = 16) -> list[dict]:
    if not games:
        return []
    today = today or date.today()
    upcoming = [game for game in games if game["game_date"] >= today.isoformat()]
    if upcoming:
        first_week = min(game["week"] for game in upcoming)
        # A current slate is the entire football week, not merely the games
        # whose kickoff has not passed. Finals remain beside the remaining
        # previews until the schedule advances to the next week.
        return [game for game in games if game["week"] == first_week][:limit]
    last_week = max(game["week"] for game in games)
    return [game for game in games if game["week"] == last_week][:limit]


def matchup_cards(games: list[dict], identities: dict[str, dict], records: dict[str, dict],
                  efficiency: dict[str, dict], elo: dict[str, float]) -> list[dict]:
    """Shape a week's games into the away/home side cards the dashboard and
    scoreboard both render: logo, record, live score, EPA, and Elo."""
    matchups = []
    for game in games:
        sides = []
        for role in ("away", "home"):
            code = game[f"{role}_team"]
            identity = identities.get(code, {})
            sides.append({
                "role": role, "team": code, "name": identity.get("name", code),
                "logo_url": identity.get("logo_url"), "color": identity.get("color", "#0b5aa5"),
                "record": records.get(code, {}).get("record", "0-0"),
                "score": game.get(f"{role}_score"),
                "epa_per_play": efficiency.get(code, {}).get("epa_per_play"),
                "elo": round(elo.get(code, 1500)),
            })
        matchups.append({"game_id": game["game_id"], "week": game["week"],
                         "date": game["game_date"], "time": game["game_time"],
                         "completed": bool(game["completed"]), "sides": sides,
                         "destination": "review" if game["completed"] else "overview"})
    return matchups


def source_coverage_table(rows: list[dict]) -> Table:
    def short_stamp(value):
        return str(value)[:16].replace("T", " ") if value else None

    prepared = []
    for row in rows:
        prepared.append({
            **row,
            "source": row.get("display_name") or row.get("handle"),
            "source_url": row.get("profile_url"),
            "directory_scope": row.get("team_label") or row.get("division") or row.get("scope"),
            "sections_label": ", ".join(row.get("sections") or ()),
            "stored": row.get("item_count", 0),
            "latest": short_stamp(row.get("latest_published") or row.get("last_success")),
            "last_checked": short_stamp(row.get("last_checked")),
            "status_class": row.get("status", "").replace(" ", "-"),
            "error": (row.get("last_error") or "")[:100],
        })
    return Table(
        (Column("source", "Source"), Column("handle", "Handle"),
         Column("directory_scope", "Scope"), Column("sections_label", "Sections"),
         Column("status", "Status", emphasis=True), Column("stored", "Stored", "big"),
         Column("latest", "Latest item"), Column("last_checked", "Last checked"),
         Column("error", "Last error")),
        prepared, caption="Configured source audit",
        note="Stored counts are cumulative. Check status reflects the latest instrumented fetch.",
        empty="No NFL reporting sources are configured.", dense=True,
    )


def zone_matchup_table(item: dict) -> Table:
    """One receiver's usage-zone grid as a real `<table>`.

    This used to be hand-rolled CSS grid rows, each an independent grid
    formatting context sizing its own columns off its own row's content --
    so a long unbroken label like "Intermediate middle" widened that one
    row's first column without widening its neighbors, and the EPA numbers
    landed at different x-positions row to row. A table's column widths are
    computed once across every row, which is the actual fix, not a
    CSS tweak: alignment is the table framework's job everywhere else on
    this site, and this grid is presentation-identical to it.
    """
    columns = (
        Column("zone", "Zone"),
        Column("offense_epa", f"{item.get('offense', 'Offense')} EPA/tgt", "signed2"),
        Column("defense_epa", f"{item.get('defense', 'Defense')} allowed", "signed2"),
    )
    rows = []
    for zone in item.get("zone_matchups") or ():
        rows.append({
            "zone": f"{(zone.get('depth_bucket') or '').title()} {zone.get('pass_location') or ''}".strip(),
            "zone_sub": (f"{format_value(zone.get('receptions'), 'int')}/"
                        f"{format_value(zone.get('targets'), 'int')} · "
                        f"{format_value(zone.get('receiving_yards'), 'int')} yd · "
                        f"{format_value(zone.get('target_share'), 'rate')} targets"),
            "zone_class": "weak-zone" if zone.get("is_weak_zone") else None,
            "offense_epa": zone.get("offense_epa"),
            "defense_epa": zone.get("defense_epa"),
            "defense_epa_sub": f"{format_value(zone.get('defense_attempts'), 'int')} att",
        })
    return Table(
        columns, rows, dense=True, sortable=False,
        empty="Not enough zone-level target and allowed sample yet to compare.",
    )


def ingestion_runs_table(rows: list[dict]) -> Table:
    """Recent `sync-nfl-content` invocations, across every platform it ran."""
    prepared = []
    for row in rows:
        try:
            error_count = len(json.loads(row.get("errors_json") or "[]"))
        except (TypeError, ValueError):
            error_count = 0
        prepared.append({
            "platform": row.get("platform"), "season": row.get("season"),
            "finished_at": row.get("finished_at"), "attempted": row.get("attempted"),
            "succeeded": row.get("succeeded"), "seen": row.get("seen"),
            "stored": row.get("stored"), "errors": error_count,
            "errors_class": "error" if error_count else None,
        })
    return Table(
        (Column("platform", "Platform"), Column("season", "Season", "int"),
         Column("finished_at", "Finished"), Column("attempted", "Attempted", "int"),
         Column("succeeded", "Succeeded", "int"), Column("seen", "Seen", "big"),
         Column("stored", "Stored", "big", emphasis=True), Column("errors", "Errors", "int")),
        rows=prepared, caption="Recent ingestion runs",
        note="One row per sync-nfl-content invocation. Errors counts failed feeds/sources within that run, not stored items.",
        empty="No content ingestion runs are recorded yet.", dense=True, sortable=False,
    )


def team_source_coverage_table(teams: dict[str, dict[str, int]]) -> Table:
    rows = [{"team": team, **counts,
             "coverage_rate": counts["producing"] / counts["configured"] if counts["configured"] else None,
             "team_url": f"/nfl/teams/{team}/"}
            for team, counts in sorted(teams.items())]
    return Table(
        (Column("team", "Team"), Column("configured", "Configured", "int"),
         Column("producing", "With stored items", "int"),
         Column("coverage_rate", "Observed", "rate", emphasis=True),
         Column("errors", "Errors", "int")),
        rows, caption="Team-account coverage",
        note="Observed means at least one stored item from the configured account.",
        empty="No team-scoped sources are configured.", dense=True,
    )
