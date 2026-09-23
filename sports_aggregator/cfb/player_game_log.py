"""Player-page career game log with opponent and position-aware stat context."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import Any

from sports_aggregator.tables import Column, Table


PRIMARY_STATS: dict[str, tuple[str, str, str]] = {
    "QB": ("passing", "YDS", "Pass yds"),
    "RB": ("rushing", "YDS", "Rush yds"),
    "FB": ("rushing", "YDS", "Rush yds"),
    "WR": ("receiving", "YDS", "Rec yds"),
    "TE": ("receiving", "YDS", "Rec yds"),
    "K": ("kicking", "PTS", "Kick pts"),
    "P": ("punting", "AVG", "Punt avg"),
}

DEFENSIVE_POSITIONS = {
    "DE", "DT", "DL", "EDGE", "LB", "ILB", "OLB", "CB", "DB", "S", "FS", "SS", "NT",
}

FALLBACK_STATS = (
    ("passing", "YDS", "Pass yds"),
    ("rushing", "YDS", "Rush yds"),
    ("receiving", "YDS", "Rec yds"),
    ("defensive", "TOT", "Tackles"),
    ("interceptions", "INT", "INT"),
    ("kicking", "PTS", "Kick pts"),
    ("punting", "AVG", "Punt avg"),
    ("kickReturns", "YDS", "KR yds"),
    ("puntReturns", "YDS", "PR yds"),
)

YARDAGE_CONTEXT = {"passing", "rushing", "receiving"}

# Each display column accepts several common stat-type spellings. CFBD and
# historical providers have not always used exactly the same abbreviation, so
# the game log resolves the first value present rather than assuming one schema.
STAT_SPECS = {
    "pass_cmp": ("Cmp", "passing", ("CMP", "COM", "COMPLETIONS"), "int"),
    "pass_att": ("Att", "passing", ("ATT", "ATTEMPTS"), "int"),
    "pass_yds": ("Pass yds", "passing", ("YDS", "YARDS"), "num"),
    "pass_td": ("Pass TD", "passing", ("TD", "TDS", "TOUCHDOWNS"), "int"),
    "pass_int": ("INT", "passing", ("INT", "INTERCEPTIONS"), "int"),
    "rush_att": ("Rush", "rushing", ("CAR", "ATT", "ATTEMPTS", "CARRIES"), "int"),
    "rush_yds": ("Rush yds", "rushing", ("YDS", "YARDS"), "num"),
    "rush_td": ("Rush TD", "rushing", ("TD", "TDS", "TOUCHDOWNS"), "int"),
    "rec": ("Rec", "receiving", ("REC", "RECEPTIONS"), "int"),
    "rec_yds": ("Rec yds", "receiving", ("YDS", "YARDS"), "num"),
    "rec_td": ("Rec TD", "receiving", ("TD", "TDS", "TOUCHDOWNS"), "int"),
    "tackles": ("Tkl", "defensive", ("TOT", "TOTAL", "TACKLES"), "num"),
    "solo": ("Solo", "defensive", ("SOLO", "SOLO_TACKLES"), "num"),
    "tfl": ("TFL", "defensive", ("TFL", "TACKLES_FOR_LOSS"), "num"),
    "sacks": ("Sack", "defensive", ("SACKS", "SACK"), "num"),
    "pd": ("PD", "defensive", ("PD", "PBU", "PASSES_DEFENDED"), "num"),
    "def_int": ("INT", "interceptions", ("INT", "INTERCEPTIONS"), "num"),
    "ff": ("FF", "fumbles", ("FF", "FORCED", "FORCED_FUMBLES"), "num"),
    "fr": ("FR", "fumbles", ("FR", "REC", "FUMBLE_RECOVERIES"), "num"),
    "fgm": ("FGM", "kicking", ("FGM", "FG_MADE"), "int"),
    "fga": ("FGA", "kicking", ("FGA", "FG_ATTEMPTS"), "int"),
    "xpm": ("XP", "kicking", ("XPM", "XP", "PAT"), "int"),
    "xpa": ("XPA", "kicking", ("XPA", "PAT_ATTEMPTS"), "int"),
    "kick_pts": ("Pts", "kicking", ("PTS", "POINTS"), "num"),
    "punts": ("Punts", "punting", ("NO", "PUNTS", "ATT"), "int"),
    "punt_yds": ("Punt yds", "punting", ("YDS", "YARDS"), "num"),
    "punt_avg": ("Avg", "punting", ("AVG", "AVERAGE"), "f1"),
    "punt_long": ("Long", "punting", ("LONG", "LNG"), "num"),
    "punt_in20": ("In 20", "punting", ("IN20", "INSIDE20", "I20"), "int"),
}

POSITION_COLUMNS = {
    "QB": ("pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
           "rush_att", "rush_yds", "rush_td"),
    "RB": ("rush_att", "rush_yds", "rush_td", "rec", "rec_yds", "rec_td"),
    "FB": ("rush_att", "rush_yds", "rush_td", "rec", "rec_yds", "rec_td"),
    "WR": ("rec", "rec_yds", "rec_td", "rush_att", "rush_yds", "rush_td"),
    "TE": ("rec", "rec_yds", "rec_td", "rush_att", "rush_yds", "rush_td"),
    "K": ("fgm", "fga", "xpm", "xpa", "kick_pts"),
    "P": ("punts", "punt_yds", "punt_avg", "punt_long", "punt_in20"),
}

DEFENSIVE_COLUMNS = ("tackles", "solo", "tfl", "sacks", "pd", "def_int", "ff", "fr")
GENERIC_COLUMNS = ("pass_yds", "rush_yds", "rec_yds", "tackles", "def_int")


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _career_identity(connection, player: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Resolve a bounded set of IDs for one stored player career."""
    current_id = str(player.get("player_id") or "").strip()
    name = str(player.get("name") or "").strip()
    normalized = str(player.get("normalized_name") or "").strip()
    position = str(player.get("position") or "").strip().upper()

    teams = {
        str(item.get("team") or "").strip()
        for item in (player.get("stints") or [])
        if str(item.get("team") or "").strip()
    }
    if player.get("team"):
        teams.add(str(player["team"]).strip())
    for movement in player.get("transfers") or []:
        for key in ("origin", "destination"):
            if movement.get(key):
                teams.add(str(movement[key]).strip())

    ids = {current_id} if current_id else set()
    if normalized:
        roster_rows = connection.execute(
            """SELECT DISTINCT player_id,team,position FROM players
               WHERE normalized_name=?""",
            (normalized,),
        ).fetchall()
        for row in roster_rows:
            row_team = str(row["team"] or "").strip()
            row_pos = str(row["position"] or "").strip().upper()
            if teams and row_team not in teams:
                continue
            if position and row_pos and row_pos != position:
                continue
            ids.add(str(row["player_id"]))
            if row_team:
                teams.add(row_team)

    if name and teams:
        team_values = sorted(teams)
        placeholders = _placeholders(team_values)
        rows = connection.execute(
            f"""SELECT DISTINCT player_id,team FROM player_season_stats
                WHERE player=? AND team IN ({placeholders})""",
            [name, *team_values],
        ).fetchall()
        for row in rows:
            ids.add(str(row["player_id"]))

    ordered = sorted(item for item in ids if item)
    if current_id and current_id in ordered:
        ordered.remove(current_id)
        ordered.insert(0, current_id)
    return ordered[:12], sorted(teams)


def _primary_stat(connection, player_ids: list[str], position: str | None):
    """Pick the most useful stat that exists for any resolved career id."""
    pos = str(position or "").upper().strip()
    preferred = ("defensive", "TOT", "Tackles") if pos in DEFENSIVE_POSITIONS else PRIMARY_STATS.get(pos)
    candidates = ([preferred] if preferred else []) + [item for item in FALLBACK_STATS if item != preferred]
    if not player_ids:
        return preferred or ("defensive", "TOT", "Tackles")
    placeholders = _placeholders(player_ids)
    for category, stat_type, label in candidates:
        exists = connection.execute(
            f"""SELECT 1 FROM game_player_box_stats
                WHERE player_id IN ({placeholders}) AND category=? AND stat_type=?
                LIMIT 1""",
            [*player_ids, category, stat_type],
        ).fetchone()
        if exists:
            return category, stat_type, label
    return preferred or ("defensive", "TOT", "Tackles")


def _defense_allowed_before(connection, seasons: set[int], category: str,
                            stat_type: str) -> dict[tuple[int, str], float | None]:
    """Opponent defense's average yards allowed entering each game."""
    if category not in YARDAGE_CONTEXT or stat_type != "YDS":
        return {}

    context: dict[tuple[int, str], float | None] = {}
    for season in sorted(seasons):
        rows = connection.execute(
            """SELECT g.game_id,g.start_date,g.home_team,g.away_team,gp.team,
                      SUM(gp.numeric_value) AS yards
               FROM games g
               JOIN game_player_box_stats gp USING(game_id)
               WHERE g.season=? AND gp.category=? AND gp.stat_type=?
                 AND gp.numeric_value IS NOT NULL
               GROUP BY g.game_id,g.start_date,g.home_team,g.away_team,gp.team
               ORDER BY g.start_date,g.game_id""",
            (season, category, stat_type),
        ).fetchall()
        running: dict[str, list[float]] = {}
        for raw in rows:
            item = dict(raw)
            offense = str(item.get("team") or "")
            if offense == item.get("home_team"):
                defense = str(item.get("away_team") or "")
            elif offense == item.get("away_team"):
                defense = str(item.get("home_team") or "")
            else:
                continue
            prior = running.setdefault(defense, [0.0, 0.0])
            context[(int(item["game_id"]), defense)] = prior[0] / prior[1] if prior[1] else None
            yards = item.get("yards")
            if yards is not None:
                prior[0] += float(yards)
                prior[1] += 1.0
    return context


def _date_label(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%b %d")
    except ValueError:
        return str(value)[:10]


def _stat_key(category: str | None, stat_type: str | None) -> tuple[str, str]:
    return str(category or "").casefold(), str(stat_type or "").strip().upper()


def _game_stat(stats: dict[tuple[str, str], Any], key: str) -> Any:
    _label, category, aliases, _format = STAT_SPECS[key]
    normalized_category = category.casefold()
    for alias in aliases:
        value = stats.get((normalized_category, alias.upper()))
        if value is not None:
            return value
    return None


def _stat_columns(position: str | None, available: set[tuple[str, str]]) -> list[str]:
    pos = str(position or "").strip().upper()
    desired = DEFENSIVE_COLUMNS if pos in DEFENSIVE_POSITIONS else POSITION_COLUMNS.get(pos, GENERIC_COLUMNS)
    present = []
    for key in desired:
        _label, category, aliases, _format = STAT_SPECS[key]
        if any((category.casefold(), alias.upper()) in available for alias in aliases):
            present.append(key)
    # Sparse historical data should still display something useful rather than
    # collapsing the game log to metadata only.
    if not present:
        for key in GENERIC_COLUMNS:
            _label, category, aliases, _format = STAT_SPECS[key]
            if any((category.casefold(), alias.upper()) in available for alias in aliases):
                present.append(key)
    return present


def player_weekly_trend(repository, player: dict[str, Any], season: int) -> list[dict[str, Any]]:
    """One rusher or receiver's season broken out by week, for a trend chart.

    Nothing tags a rusher or receiver on an individual play the way passer_id
    does for a QB, so this reads the same per-game box-score rows the career
    game log uses, scoped to one season and grouped by week instead of game.
    """
    repository.initialize()
    with closing(repository._connect()) as connection:
        player_ids, _career_teams = _career_identity(connection, player)
        if not player_ids:
            return []
        placeholders = _placeholders(player_ids)
        rows = connection.execute(
            f"""SELECT gp.game_id,g.week,gp.category,gp.stat_type,gp.numeric_value,
                       gp.team_id,g.home_team_id,g.home_team,g.away_team_id,g.away_team
                FROM game_player_box_stats gp JOIN games g USING(game_id)
                WHERE gp.player_id IN ({placeholders}) AND g.season=?""",
            [*player_ids, season],
        ).fetchall()

    games: dict[int, dict[str, Any]] = {}
    weeks: dict[int, int] = {}
    opponents: dict[int, str] = {}
    for raw in rows:
        game_id = int(raw["game_id"])
        weeks[game_id] = raw["week"]
        opponents[game_id] = (raw["away_team"] if raw["team_id"] == raw["home_team_id"]
                              else raw["home_team"])
        value = raw["numeric_value"]
        if value is None:
            continue
        key = _stat_key(raw["category"], raw["stat_type"])
        games.setdefault(game_id, {})[key] = float(value)

    def stat(values: dict[tuple[str, str], float], category: str, *types: str) -> float | None:
        for stat_type in types:
            value = values.get((category, stat_type))
            if value is not None:
                return value
        return None

    output = []
    for game_id, values in games.items():
        week = weeks.get(game_id)
        if week is None:
            continue
        rush_att = stat(values, "rushing", "CAR", "ATT")
        rush_yards = stat(values, "rushing", "YDS")
        rush_td = stat(values, "rushing", "TD")
        receptions = stat(values, "receiving", "REC")
        receiving_yards = stat(values, "receiving", "YDS")
        receiving_td = stat(values, "receiving", "TD")
        total_yards = (rush_yards or 0.0) + (receiving_yards or 0.0)
        output.append({
            "week": int(week), "game_id": game_id, "opponent": opponents.get(game_id),
            "rush_yards": rush_yards, "rush_attempts": rush_att, "rush_td": rush_td,
            "yards_per_carry": round(rush_yards / rush_att, 2) if rush_yards is not None and rush_att else None,
            "receptions": receptions,
            "receiving_yards": receiving_yards, "receiving_td": receiving_td,
            "yards_per_reception": (round(receiving_yards / receptions, 2)
                                    if receiving_yards is not None and receptions else None),
            "scrimmage_yards": total_yards if (rush_yards is not None or receiving_yards is not None) else None,
        })
    return sorted(output, key=lambda row: row["week"])


def player_game_log_table(repository, player: dict[str, Any], season: int) -> Table:
    """Career game log from stored per-game player box scores."""
    del season  # Career log intentionally spans every stored season for this identity.
    position = player.get("position")
    repository.initialize()
    with closing(repository._connect()) as connection:
        player_ids, _career_teams = _career_identity(connection, player)
        category, stat_type, stat_label = _primary_stat(connection, player_ids, position)
        raw_rows = []
        if player_ids:
            placeholders = _placeholders(player_ids)
            raw_rows = connection.execute(
                f"""SELECT gp.game_id,g.season,g.week,g.start_date,
                          g.home_team_id,g.home_team,g.home_points,g.home_pregame_elo,
                          g.away_team_id,g.away_team,g.away_points,g.away_pregame_elo,
                          gp.player_id AS box_player_id,gp.team,gp.conference,
                          gp.category,gp.stat_type,gp.numeric_value,gp.stat_value
                   FROM game_player_box_stats gp
                   JOIN games g USING(game_id)
                   WHERE gp.player_id IN ({placeholders})
                   ORDER BY g.start_date DESC,g.game_id DESC""",
                player_ids,
            ).fetchall()

        games: dict[int, dict[str, Any]] = {}
        available_stats: set[tuple[str, str]] = set()
        for raw in raw_rows:
            item = dict(raw)
            game_id = int(item["game_id"])
            game = games.setdefault(game_id, {**item, "stats": {}})
            key = _stat_key(item.get("category"), item.get("stat_type"))
            available_stats.add(key)
            value = item.get("numeric_value")
            if value is None:
                value = item.get("stat_value")
            # Multiple historical IDs can reconcile to the same player career.
            # Prefer the first non-null value instead of duplicating a game.
            game["stats"].setdefault(key, value)

        game_rows = sorted(games.values(), key=lambda item: (item["start_date"], item["game_id"]), reverse=True)
        chronological = sorted(games.values(), key=lambda item: (item["start_date"], item["game_id"]))
        career_game_number = {int(item["game_id"]): index + 1 for index, item in enumerate(chronological)}

        seasons = {int(row["season"]) for row in game_rows}
        defense_allowed = _defense_allowed_before(connection, seasons, category, stat_type)
        stat_columns = _stat_columns(position, available_stats)
        elo_by_season: dict[int, dict[int, dict[str, Any]]] = {}
        output: list[dict[str, Any]] = []

        for item in game_rows:
            game_id = int(item["game_id"])
            row_season = int(item["season"])
            player_is_home = item["team"] == item["home_team"]
            opponent = item["away_team"] if player_is_home else item["home_team"]
            opponent_id = item["away_team_id"] if player_is_home else item["home_team_id"]
            player_points = item["home_points"] if player_is_home else item["away_points"]
            opp_points = item["away_points"] if player_is_home else item["home_points"]
            pregame_elo = item["away_pregame_elo"] if player_is_home else item["home_pregame_elo"]
            result = "—"
            if player_points is not None and opp_points is not None:
                result = ("W" if player_points > opp_points else "L" if player_points < opp_points else "T") + f" {player_points}-{opp_points}"

            if row_season not in elo_by_season:
                elo_by_season[row_season] = repository.team_elo(row_season)
            current_for_season = (elo_by_season[row_season].get(opponent_id) or {}).get("elo")
            opponent_rating = current_for_season if current_for_season is not None else pregame_elo

            row = {
                "career_game": career_game_number.get(game_id),
                "season": row_season,
                "week": item["week"],
                "date": _date_label(item["start_date"]),
                "opponent": opponent,
                "opponent_elo": opponent_rating,
                "defense_avg_allowed": defense_allowed.get((game_id, str(opponent))),
                "result": result,
                "game_url": f"/college-football/games/{game_id}/box-score/",
            }
            for key in stat_columns:
                row[key] = _game_stat(item["stats"], key)
            output.append(row)

    identity_note = f" · reconciled {len(player_ids)} historical player IDs" if len(player_ids) > 1 else ""
    defense_note = ""
    if category in YARDAGE_CONTEXT and stat_type == "YDS":
        defense_note = (
            f" · Def avg allowed is the opponent's per-game {stat_label.lower()} allowed before that game"
        )
    note = (
        "Career game log · stat columns adapt to the player's position · # counts games oldest to newest · "
        "opponent Elo uses the stored season rating when available, with pregame Elo as fallback"
        f"{defense_note}{identity_note}"
    )

    columns = [
        Column("career_game", "#", format="int", align="right",
               title="Career game number, oldest game = 1"),
        Column("season", "Season", format="int", align="right"),
        Column("week", "Wk", format="int", align="right"),
        Column("date", "Date", sort="text"),
        Column("opponent", "Opponent"),
        Column("result", "Result"),
    ]
    for key in stat_columns:
        label, _category, _aliases, format_name = STAT_SPECS[key]
        columns.append(Column(key, label, format=format_name, align="right", emphasis=key in {
            "pass_yds", "rush_yds", "rec_yds", "tackles", "kick_pts", "punt_avg"
        }))
    columns.extend([
        Column("opponent_elo", "Opp Elo", format="int", align="right"),
        Column("defense_avg_allowed", "Def avg allowed", format="f1", align="right",
               title=f"Opponent defense's average {stat_label.lower()} allowed per game before this matchup"),
    ])

    return Table(
        columns=columns,
        rows=[{**row, "opponent_url": row["game_url"], "result_url": row["game_url"]} for row in output],
        caption="Career game log",
        note=note,
        dense=True,
        sortable=True,
        empty=(
            "No per-game box-score identity could be matched to this stored career. "
            "Season totals can still appear above because they come from a separate historical dataset."
        ),
    )
