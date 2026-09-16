"""SQLite persistence owned exclusively by the NFL vertical."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from sports_aggregator.nfl.models import (
    Game, Player, SyncReport, Team, optional_float, optional_int, optional_text,
)
from sports_aggregator.nfl.naming import TEAMS, canon_team, normalize_name


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;
CREATE TABLE IF NOT EXISTS teams (
 abbreviation TEXT PRIMARY KEY, name TEXT NOT NULL, nickname TEXT NOT NULL,
 conference TEXT NOT NULL, division TEXT NOT NULL, color TEXT,
 alternate_color TEXT, logo_url TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
 game_id TEXT PRIMARY KEY, season INTEGER NOT NULL, season_type TEXT NOT NULL,
 week INTEGER NOT NULL, game_date TEXT NOT NULL, game_time TEXT,
 away_team TEXT NOT NULL, home_team TEXT NOT NULL, away_score INTEGER,
 home_score INTEGER, completed INTEGER NOT NULL, overtime INTEGER NOT NULL,
 division_game INTEGER NOT NULL, stadium TEXT, roof TEXT, surface TEXT,
 temperature REAL, wind REAL, spread_line REAL, total_line REAL,
 away_rest INTEGER, home_rest INTEGER, away_moneyline REAL, home_moneyline REAL,
 away_spread_odds REAL, home_spread_odds REAL, under_odds REAL, over_odds REAL,
 away_coach TEXT, home_coach TEXT, weekday TEXT,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nfl_games_season_week ON games(season, week, game_date);
CREATE INDEX IF NOT EXISTS idx_nfl_games_away ON games(away_team, season);
CREATE INDEX IF NOT EXISTS idx_nfl_games_home ON games(home_team, season);
CREATE TABLE IF NOT EXISTS players (
 season INTEGER NOT NULL, player_id TEXT NOT NULL, team TEXT NOT NULL,
 full_name TEXT NOT NULL, normalized_name TEXT NOT NULL, first_name TEXT NOT NULL,
 last_name TEXT NOT NULL, position TEXT, depth_position TEXT, jersey_number INTEGER,
 status TEXT, birth_date TEXT, height REAL, weight INTEGER, college TEXT,
 years_experience INTEGER, headshot_url TEXT, pff_id TEXT, pfr_id TEXT,
 espn_id TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY (season, player_id, team)
);
CREATE INDEX IF NOT EXISTS idx_nfl_players_team ON players(season, team);
CREATE INDEX IF NOT EXISTS idx_nfl_players_name ON players(season, normalized_name);
CREATE TABLE IF NOT EXISTS player_weekly_stats (
 season INTEGER NOT NULL, week INTEGER NOT NULL, season_type TEXT NOT NULL,
 game_id TEXT NOT NULL, player_id TEXT NOT NULL, player_name TEXT NOT NULL,
 team TEXT NOT NULL, opponent_team TEXT NOT NULL, position TEXT,
 metric TEXT NOT NULL, value REAL NOT NULL,
 PRIMARY KEY (season, week, season_type, game_id, player_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_nfl_weekly_player ON player_weekly_stats(player_id, season, week);
CREATE INDEX IF NOT EXISTS idx_nfl_weekly_team ON player_weekly_stats(team, season, week, metric);
CREATE INDEX IF NOT EXISTS idx_nfl_weekly_opponent_player
 ON player_weekly_stats(opponent_team,player_id,game_id);
CREATE INDEX IF NOT EXISTS idx_nfl_weekly_game ON player_weekly_stats(game_id,player_id,metric);
CREATE TABLE IF NOT EXISTS snap_counts (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 pfr_game_id TEXT, player_key TEXT NOT NULL, pfr_player_id TEXT,
 player_name TEXT NOT NULL, normalized_name TEXT NOT NULL, position TEXT,
 team TEXT NOT NULL, opponent TEXT NOT NULL, offense_snaps REAL,
 offense_pct REAL, defense_snaps REAL, defense_pct REAL, st_snaps REAL, st_pct REAL,
 PRIMARY KEY (season, game_id, team, player_key)
);
CREATE INDEX IF NOT EXISTS idx_nfl_snaps_team ON snap_counts(season, team, week);
CREATE INDEX IF NOT EXISTS idx_nfl_snaps_player ON snap_counts(season, pfr_player_id, week);
CREATE TABLE IF NOT EXISTS depth_chart_snapshots (
 season INTEGER NOT NULL, snapshot_at TEXT NOT NULL, team TEXT NOT NULL,
 player_key TEXT NOT NULL, player_name TEXT NOT NULL, gsis_id TEXT,
 espn_id TEXT, position_group TEXT, position_id TEXT, position_name TEXT,
 position_abbreviation TEXT, position_slot INTEGER, position_rank INTEGER,
 PRIMARY KEY (season, snapshot_at, team, player_key, position_slot, position_rank)
);
CREATE INDEX IF NOT EXISTS idx_nfl_depth_latest ON depth_chart_snapshots(season, team, snapshot_at);
CREATE INDEX IF NOT EXISTS idx_nfl_depth_player ON depth_chart_snapshots(season, gsis_id, snapshot_at);
CREATE TABLE IF NOT EXISTS team_staff (
 season INTEGER NOT NULL, team TEXT NOT NULL, role TEXT NOT NULL, coach_name TEXT NOT NULL,
 playcaller INTEGER NOT NULL DEFAULT 0, as_of TEXT, source_url TEXT,
 PRIMARY KEY(season,team,role)
);
CREATE INDEX IF NOT EXISTS idx_nfl_staff_team ON team_staff(season,team);
CREATE TABLE IF NOT EXISTS team_scheme_rates (
 season INTEGER NOT NULL, team TEXT NOT NULL,
 blitz_rate REAL, light_box_rate REAL, heavy_box_rate REAL, sub_package_rate REAL,
 PRIMARY KEY(season,team)
);
CREATE TABLE IF NOT EXISTS team_pressure_rates (
 season INTEGER NOT NULL, team TEXT NOT NULL, games REAL,
 dadot REAL, air_yards REAL, yards_after_catch REAL,
 blitzes REAL, blitz_rate REAL, hurries REAL, hurry_rate REAL,
 qb_knockdowns REAL, knockdown_rate REAL, sacks REAL,
 pressures REAL, pressure_rate REAL, missed_tackles REAL,
 PRIMARY KEY(season,team)
);
CREATE TABLE IF NOT EXISTS injury_reports (
 season INTEGER NOT NULL,team TEXT NOT NULL,injury_id TEXT NOT NULL,espn_id TEXT NOT NULL,
 gsis_id TEXT,player_name TEXT NOT NULL,position TEXT,designation TEXT NOT NULL,status TEXT,
 injury_type TEXT,location TEXT,detail TEXT,side TEXT,practice_status TEXT,return_date TEXT,
 short_comment TEXT,long_comment TEXT,report_date TEXT,note_source TEXT,fetched_at TEXT NOT NULL,
 source_url TEXT NOT NULL,PRIMARY KEY(season,team,injury_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_injuries_team ON injury_reports(season,team,designation);
CREATE INDEX IF NOT EXISTS idx_nfl_injuries_player ON injury_reports(season,gsis_id);
CREATE TABLE IF NOT EXISTS player_master (
 gsis_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 first_name TEXT, last_name TEXT, birth_date TEXT, position_group TEXT, position TEXT,
 height REAL, weight INTEGER, headshot_url TEXT, college TEXT, college_conference TEXT,
 rookie_season INTEGER, last_season INTEGER, latest_team TEXT, status TEXT,
 years_experience INTEGER, draft_year INTEGER, draft_round INTEGER, draft_pick INTEGER,
 draft_team TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nfl_master_name ON player_master(normalized_name);
CREATE TABLE IF NOT EXISTS player_external_ids (
 source TEXT NOT NULL, gsis_id TEXT NOT NULL, provider TEXT NOT NULL,
 external_id TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY (source, gsis_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_nfl_external_lookup ON player_external_ids(provider, external_id);
CREATE TABLE IF NOT EXISTS team_weekly_stats (
 season INTEGER NOT NULL, week INTEGER NOT NULL, season_type TEXT NOT NULL,
 team TEXT NOT NULL, opponent_team TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
 PRIMARY KEY (season, week, season_type, team, metric)
);
CREATE INDEX IF NOT EXISTS idx_nfl_team_weekly ON team_weekly_stats(season, team, week, metric);
CREATE TABLE IF NOT EXISTS game_team_efficiency (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 team TEXT NOT NULL, opponent_team TEXT NOT NULL, plays INTEGER NOT NULL,
 total_epa REAL NOT NULL, successful_plays REAL NOT NULL,
 pass_plays INTEGER NOT NULL, pass_epa REAL NOT NULL,
 rush_plays INTEGER NOT NULL, rush_epa REAL NOT NULL,
 early_down_plays INTEGER NOT NULL, early_down_epa REAL NOT NULL,
 explosive_plays INTEGER NOT NULL,
 PRIMARY KEY (game_id, team)
);
CREATE INDEX IF NOT EXISTS idx_nfl_efficiency_team ON game_team_efficiency(season, team, week);
CREATE TABLE IF NOT EXISTS game_team_situational (
 season INTEGER NOT NULL,week INTEGER NOT NULL,game_id TEXT NOT NULL,
 team TEXT NOT NULL,opponent_team TEXT NOT NULL,plays INTEGER NOT NULL,
 drives INTEGER NOT NULL,third_down_plays INTEGER NOT NULL,third_down_conversions INTEGER NOT NULL,
 red_zone_plays INTEGER NOT NULL,red_zone_successes INTEGER NOT NULL,
 neutral_plays INTEGER NOT NULL,neutral_passes INTEGER NOT NULL,
 seconds_sum REAL NOT NULL,clocked_plays INTEGER NOT NULL,
 PRIMARY KEY(game_id,team)
);
CREATE INDEX IF NOT EXISTS idx_nfl_situational_team ON game_team_situational(season,team,week);
CREATE TABLE IF NOT EXISTS game_team_playcalling (
 season INTEGER NOT NULL,week INTEGER NOT NULL,game_id TEXT NOT NULL,
 team TEXT NOT NULL,opponent_team TEXT NOT NULL,
 pass_plays INTEGER NOT NULL,rush_plays INTEGER NOT NULL,
 early_down_plays INTEGER NOT NULL,early_down_passes INTEGER NOT NULL,
 shotgun_known INTEGER NOT NULL,shotgun_plays INTEGER NOT NULL,
 no_huddle_known INTEGER NOT NULL,no_huddle_plays INTEGER NOT NULL,
 PRIMARY KEY(game_id,team)
);
CREATE INDEX IF NOT EXISTS idx_nfl_playcalling_team ON game_team_playcalling(season,team,week);
CREATE TABLE IF NOT EXISTS nfl_elo_games (
 game_id TEXT PRIMARY KEY,season INTEGER NOT NULL,week INTEGER NOT NULL,
 away_team TEXT NOT NULL,home_team TEXT NOT NULL,away_score INTEGER NOT NULL,
 home_score INTEGER NOT NULL,away_pre REAL NOT NULL,home_pre REAL NOT NULL,
 away_post REAL NOT NULL,home_post REAL NOT NULL,home_win_probability REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nfl_elo_game_season ON nfl_elo_games(season,week);
CREATE TABLE IF NOT EXISTS nfl_elo_ratings (
 team TEXT PRIMARY KEY,rating REAL NOT NULL,games INTEGER NOT NULL,wins INTEGER NOT NULL,
 losses INTEGER NOT NULL,ties INTEGER NOT NULL,last_game_id TEXT,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_venues (
 team TEXT PRIMARY KEY, venue_name TEXT NOT NULL,
 latitude REAL NOT NULL, longitude REAL NOT NULL,
 elevation_meters REAL, dome INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nfl_game_weather (
 game_id TEXT NOT NULL, forecast_generated_at TEXT NOT NULL,
 kickoff_time TEXT NOT NULL, forecast_hour TEXT NOT NULL,
 temperature REAL, precipitation_probability REAL, precipitation_amount REAL,
 sustained_wind REAL, wind_gust REAL, humidity REAL, visibility REAL,
 weather_code INTEGER, condition TEXT NOT NULL DEFAULT '',
 flags_json TEXT NOT NULL DEFAULT '[]', indoor INTEGER NOT NULL DEFAULT 0,
 venue TEXT NOT NULL DEFAULT '', latitude REAL, longitude REAL,
 source TEXT NOT NULL, imported_at TEXT NOT NULL,
 PRIMARY KEY(game_id,forecast_generated_at)
);
CREATE INDEX IF NOT EXISTS idx_nfl_game_weather_game
  ON nfl_game_weather(game_id,forecast_generated_at DESC);
CREATE TABLE IF NOT EXISTS qb_pass_profiles (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 passer_player_id TEXT NOT NULL, passer_name TEXT NOT NULL,
 depth_bucket TEXT NOT NULL, pass_location TEXT NOT NULL,
 attempts INTEGER NOT NULL, completions INTEGER NOT NULL,
 passing_yards REAL NOT NULL, air_yards REAL NOT NULL, total_epa REAL NOT NULL,
 touchdowns INTEGER NOT NULL, interceptions INTEGER NOT NULL,
 cpoe_total REAL NOT NULL, cpoe_plays INTEGER NOT NULL,
 PRIMARY KEY (game_id,passer_player_id,depth_bucket,pass_location)
);
CREATE INDEX IF NOT EXISTS idx_nfl_qb_profile_player ON qb_pass_profiles(season,passer_player_id,week);
CREATE INDEX IF NOT EXISTS idx_nfl_qb_profile_defense ON qb_pass_profiles(season,defense_team,week);
CREATE TABLE IF NOT EXISTS receiver_pass_profiles (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 receiver_player_id TEXT NOT NULL, receiver_name TEXT NOT NULL,
 depth_bucket TEXT NOT NULL, pass_location TEXT NOT NULL,
 targets INTEGER NOT NULL, receptions INTEGER NOT NULL,
 receiving_yards REAL NOT NULL, air_yards REAL NOT NULL, yards_after_catch REAL NOT NULL,
 total_epa REAL NOT NULL, touchdowns INTEGER NOT NULL,
 PRIMARY KEY (game_id,receiver_player_id,depth_bucket,pass_location)
);
CREATE INDEX IF NOT EXISTS idx_nfl_receiver_profile_player
 ON receiver_pass_profiles(season,receiver_player_id,week);
CREATE INDEX IF NOT EXISTS idx_nfl_receiver_profile_defense
 ON receiver_pass_profiles(season,defense_team,week);
CREATE TABLE IF NOT EXISTS pass_zone_receivers (
 season INTEGER NOT NULL,week INTEGER NOT NULL,game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL,defense_team TEXT NOT NULL,passer_player_id TEXT NOT NULL,
 receiver_player_id TEXT NOT NULL,receiver_name TEXT NOT NULL,
 depth_bucket TEXT NOT NULL,pass_location TEXT NOT NULL,
 targets INTEGER NOT NULL,receptions INTEGER NOT NULL,receiving_yards REAL NOT NULL,
 air_yards REAL NOT NULL,touchdowns INTEGER NOT NULL,
 PRIMARY KEY(game_id,passer_player_id,receiver_player_id,depth_bucket,pass_location)
);
CREATE INDEX IF NOT EXISTS idx_nfl_zone_receiver_passer
 ON pass_zone_receivers(season,passer_player_id,depth_bucket,pass_location);
CREATE INDEX IF NOT EXISTS idx_nfl_zone_receiver_defense
 ON pass_zone_receivers(season,defense_team,depth_bucket,pass_location);
CREATE TABLE IF NOT EXISTS rush_direction_profiles (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 rusher_player_id TEXT NOT NULL, rusher_name TEXT NOT NULL,
 direction TEXT NOT NULL,
 attempts INTEGER NOT NULL, rushing_yards REAL NOT NULL, total_epa REAL NOT NULL,
 touchdowns INTEGER NOT NULL,
 PRIMARY KEY (game_id,rusher_player_id,direction)
);
CREATE INDEX IF NOT EXISTS idx_nfl_rush_direction_player ON rush_direction_profiles(season,rusher_player_id,week);
CREATE INDEX IF NOT EXISTS idx_nfl_rush_direction_defense ON rush_direction_profiles(season,defense_team,week);
CREATE TABLE IF NOT EXISTS rush_direction_defenders (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL, direction TEXT NOT NULL,
 defender_player_id TEXT NOT NULL, defender_name TEXT NOT NULL,
 tackles INTEGER NOT NULL,
 PRIMARY KEY (game_id,direction,defender_player_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_rush_direction_defenders_defense
 ON rush_direction_defenders(season,defense_team,direction);
CREATE TABLE IF NOT EXISTS pass_zone_defenders (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL, passer_player_id TEXT NOT NULL,
 depth_bucket TEXT NOT NULL, pass_location TEXT NOT NULL,
 defender_player_id TEXT NOT NULL, defender_name TEXT NOT NULL, event TEXT NOT NULL,
 count INTEGER NOT NULL,
 PRIMARY KEY (game_id,passer_player_id,depth_bucket,pass_location,defender_player_id,event)
);
CREATE INDEX IF NOT EXISTS idx_nfl_pass_zone_defenders_defense
 ON pass_zone_defenders(season,defense_team,depth_bucket,pass_location);
CREATE TABLE IF NOT EXISTS rush_situational_profiles (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 rusher_player_id TEXT NOT NULL, rusher_name TEXT NOT NULL,
 attempts INTEGER NOT NULL, rushing_yards REAL NOT NULL, total_epa REAL NOT NULL,
 touchdowns INTEGER NOT NULL,
 PRIMARY KEY (game_id,rusher_player_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_rush_situational_offense ON rush_situational_profiles(season,offense_team);
CREATE INDEX IF NOT EXISTS idx_nfl_rush_situational_defense ON rush_situational_profiles(season,defense_team);
CREATE TABLE IF NOT EXISTS rush_situational_defenders (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 defender_player_id TEXT NOT NULL, defender_name TEXT NOT NULL, tackles INTEGER NOT NULL,
 PRIMARY KEY (game_id,defender_player_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_rush_situational_defenders_defense
 ON rush_situational_defenders(season,defense_team);
CREATE TABLE IF NOT EXISTS qb_situational_profiles (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 passer_player_id TEXT NOT NULL, passer_name TEXT NOT NULL, situation TEXT NOT NULL,
 attempts INTEGER NOT NULL, completions INTEGER NOT NULL,
 passing_yards REAL NOT NULL, total_epa REAL NOT NULL,
 touchdowns INTEGER NOT NULL, interceptions INTEGER NOT NULL,
 PRIMARY KEY (game_id,passer_player_id,situation)
);
CREATE INDEX IF NOT EXISTS idx_nfl_qb_situational_player ON qb_situational_profiles(season,passer_player_id,week);
CREATE INDEX IF NOT EXISTS idx_nfl_qb_situational_defense ON qb_situational_profiles(season,defense_team,week);
CREATE TABLE IF NOT EXISTS receiver_situational_profiles (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL,
 receiver_player_id TEXT NOT NULL, receiver_name TEXT NOT NULL, situation TEXT NOT NULL,
 targets INTEGER NOT NULL, receptions INTEGER NOT NULL,
 receiving_yards REAL NOT NULL, total_epa REAL NOT NULL, touchdowns INTEGER NOT NULL,
 PRIMARY KEY (game_id,receiver_player_id,situation)
);
CREATE INDEX IF NOT EXISTS idx_nfl_receiver_situational_player ON receiver_situational_profiles(season,receiver_player_id,week);
CREATE INDEX IF NOT EXISTS idx_nfl_receiver_situational_defense ON receiver_situational_profiles(season,defense_team,week);
CREATE TABLE IF NOT EXISTS situational_pass_receivers (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL, passer_player_id TEXT NOT NULL,
 receiver_player_id TEXT NOT NULL, receiver_name TEXT NOT NULL, situation TEXT NOT NULL,
 targets INTEGER NOT NULL, receptions INTEGER NOT NULL,
 receiving_yards REAL NOT NULL, touchdowns INTEGER NOT NULL,
 PRIMARY KEY (game_id,passer_player_id,receiver_player_id,situation)
);
CREATE INDEX IF NOT EXISTS idx_nfl_situational_receivers_passer
 ON situational_pass_receivers(season,passer_player_id,situation);
CREATE INDEX IF NOT EXISTS idx_nfl_situational_receivers_defense
 ON situational_pass_receivers(season,defense_team,situation);
CREATE TABLE IF NOT EXISTS situational_pass_defenders (
 season INTEGER NOT NULL, week INTEGER NOT NULL, game_id TEXT NOT NULL,
 offense_team TEXT NOT NULL, defense_team TEXT NOT NULL, passer_player_id TEXT NOT NULL,
 situation TEXT NOT NULL, defender_player_id TEXT NOT NULL, defender_name TEXT NOT NULL,
 event TEXT NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY (game_id,passer_player_id,situation,defender_player_id,event)
);
CREATE INDEX IF NOT EXISTS idx_nfl_situational_defenders_defense
 ON situational_pass_defenders(season,defense_team,situation);
CREATE TABLE IF NOT EXISTS nfl_content_items (
 content_id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
 external_id TEXT NOT NULL, canonical_url TEXT NOT NULL, title TEXT NOT NULL,
 body_text TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL DEFAULT '',
 source_handle TEXT, published_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
 raw_json TEXT NOT NULL DEFAULT '{}', content_type TEXT NOT NULL DEFAULT 'update',
 editorial_score REAL NOT NULL DEFAULT 0, score_reasons_json TEXT NOT NULL DEFAULT '[]',
 UNIQUE(platform,external_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_content_recent ON nfl_content_items(published_at DESC);
CREATE TABLE IF NOT EXISTS nfl_content_teams (
 content_id INTEGER NOT NULL, team TEXT NOT NULL, confidence REAL NOT NULL,
 method TEXT NOT NULL, PRIMARY KEY(content_id,team),
 FOREIGN KEY(content_id) REFERENCES nfl_content_items(content_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nfl_content_team ON nfl_content_teams(team,content_id);
CREATE TABLE IF NOT EXISTS nfl_content_players (
 content_id INTEGER NOT NULL, season INTEGER NOT NULL, player_id TEXT NOT NULL,
 confidence REAL NOT NULL, method TEXT NOT NULL,
 PRIMARY KEY(content_id,season,player_id),
 FOREIGN KEY(content_id) REFERENCES nfl_content_items(content_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nfl_content_player ON nfl_content_players(season,player_id,content_id);
CREATE TABLE IF NOT EXISTS nfl_content_games (
 content_id INTEGER NOT NULL, game_id TEXT NOT NULL, confidence REAL NOT NULL,
 method TEXT NOT NULL, PRIMARY KEY(content_id,game_id),
 FOREIGN KEY(content_id) REFERENCES nfl_content_items(content_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nfl_content_game ON nfl_content_games(game_id,content_id);
CREATE TABLE IF NOT EXISTS nfl_content_source_checks (
 platform TEXT NOT NULL, source_key TEXT NOT NULL, display_name TEXT NOT NULL,
 team TEXT, last_checked TEXT NOT NULL, last_success TEXT,
 items_seen INTEGER NOT NULL DEFAULT 0, items_stored INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, PRIMARY KEY(platform,source_key)
);
CREATE TABLE IF NOT EXISTS nfl_content_ingestion_runs (
 run_id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
 season INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 attempted INTEGER NOT NULL, succeeded INTEGER NOT NULL,
 seen INTEGER NOT NULL, stored INTEGER NOT NULL, errors_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nfl_content_runs_platform
 ON nfl_content_ingestion_runs(platform,run_id DESC);
CREATE TABLE IF NOT EXISTS nfl_pff_catalog (
 path TEXT PRIMARY KEY, family TEXT NOT NULL, season INTEGER,
 confidence REAL, margin REAL, row_count INTEGER NOT NULL,
 usable INTEGER NOT NULL, note TEXT NOT NULL, stamp TEXT NOT NULL,
 scanned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nfl_pff_catalog_family ON nfl_pff_catalog(family,season,usable);
CREATE TABLE IF NOT EXISTS nfl_pff_players (
 season INTEGER NOT NULL, pff_id TEXT NOT NULL, gsis_id TEXT,
 team TEXT NOT NULL, player_name TEXT NOT NULL, position TEXT,
 key_source TEXT NOT NULL, match_confidence REAL NOT NULL,
 source_path TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(season,pff_id,team)
);
CREATE INDEX IF NOT EXISTS idx_nfl_pff_player_gsis ON nfl_pff_players(season,gsis_id);
CREATE INDEX IF NOT EXISTS idx_nfl_pff_player_team ON nfl_pff_players(season,team);
CREATE TABLE IF NOT EXISTS nfl_pff_player_metrics (
 season INTEGER NOT NULL, week INTEGER NOT NULL DEFAULT 0,
 family TEXT NOT NULL, pff_id TEXT NOT NULL, gsis_id TEXT,
 team TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
 source_path TEXT NOT NULL,
 PRIMARY KEY(season,week,family,pff_id,team,metric)
);
CREATE INDEX IF NOT EXISTS idx_nfl_pff_metrics_player ON nfl_pff_player_metrics(season,gsis_id,week,family);
CREATE INDEX IF NOT EXISTS idx_nfl_pff_metrics_team ON nfl_pff_player_metrics(season,team,week,family,metric);
CREATE TABLE IF NOT EXISTS nfl_pff_imports (
 import_id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 files_scanned INTEGER NOT NULL, families_imported INTEGER NOT NULL,
 player_rows INTEGER NOT NULL, metric_rows INTEGER NOT NULL,
 unresolved_players INTEGER NOT NULL, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
 sync_id INTEGER PRIMARY KEY AUTOINCREMENT, season INTEGER NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, succeeded INTEGER NOT NULL,
 details_json TEXT NOT NULL
);
"""


WEEKLY_ID_COLUMNS = {
    "season", "week", "season_type", "game_id", "player_id", "player_name",
    "player_display_name", "team", "opponent_team", "position", "position_group",
    "headshot_url",
}

MASTER_ID_COLUMNS = ("esb_id", "nfl_id", "pfr_id", "pff_id", "otc_id", "espn_id", "smart_id")
CROSSWALK_ID_COLUMNS = (
    "mfl_id", "sportradar_id", "fantasypros_id", "pff_id", "sleeper_id", "nfl_id",
    "espn_id", "yahoo_id", "fleaflicker_id", "cbs_id", "pfr_id", "cfbref_id",
    "rotowire_id", "rotoworld_id", "ktc_id", "stats_id", "stats_global_id",
    "fantasy_data_id", "swish_id",
)


def _external_id(value: Any) -> str | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except ValueError:
        pass
    return text


class NFLRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(nfl_content_items)")}
            migrations = {
                "content_type": "TEXT NOT NULL DEFAULT 'update'",
                "editorial_score": "REAL NOT NULL DEFAULT 0",
                "score_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE nfl_content_items ADD COLUMN {name} {definition}")
            game_columns = {row[1] for row in connection.execute("PRAGMA table_info(games)")}
            game_migrations = {
                "away_rest": "INTEGER", "home_rest": "INTEGER",
                "away_moneyline": "REAL", "home_moneyline": "REAL",
                "away_spread_odds": "REAL", "home_spread_odds": "REAL",
                "under_odds": "REAL", "over_odds": "REAL",
                "away_coach": "TEXT", "home_coach": "TEXT", "weekday": "TEXT",
            }
            for name, definition in game_migrations.items():
                if name not in game_columns:
                    connection.execute(f"ALTER TABLE games ADD COLUMN {name} {definition}")
            connection.commit()
        self.seed_team_venues()

    def replace_teams(self, teams: Iterable[Team]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        # nflverse retains historical relocation aliases (OAK, SD, STL) in the
        # team asset. Its active rows appear first, so first-wins prevents a
        # legacy alias from replacing Las Vegas, Los Angeles, or their branding.
        unique: dict[str, Team] = {}
        for team in teams:
            if team.abbreviation in TEAMS:
                unique.setdefault(team.abbreviation, team)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM teams")
            connection.executemany(
                "INSERT INTO teams VALUES (?,?,?,?,?,?,?,?,?)",
                [(team.abbreviation, team.name, team.nickname, team.conference, team.division,
                  team.color, team.alternate_color, team.logo_url, now) for team in unique.values()],
            )
            connection.commit()
        return len(unique)

    def replace_games(self, season: int, games: Iterable[Game]) -> int:
        rows = list(games)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM games WHERE season=?", (season,))
            connection.executemany(
                """INSERT INTO games (
                    game_id,season,season_type,week,game_date,game_time,away_team,home_team,
                    away_score,home_score,completed,overtime,division_game,stadium,roof,surface,
                    temperature,wind,spread_line,total_line,away_rest,home_rest,away_moneyline,
                    home_moneyline,away_spread_odds,home_spread_odds,under_odds,over_odds,
                    away_coach,home_coach,weekday,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(game.game_id, game.season, game.season_type, game.week, game.game_date,
                  game.game_time, game.away_team, game.home_team, game.away_score, game.home_score,
                  int(game.completed), int(game.overtime), int(game.division_game), game.stadium,
                  game.roof, game.surface, game.temperature, game.wind, game.spread_line,
                  game.total_line, game.away_rest, game.home_rest, game.away_moneyline,
                  game.home_moneyline, game.away_spread_odds, game.home_spread_odds,
                  game.under_odds, game.over_odds, game.away_coach, game.home_coach,
                  game.weekday, now) for game in rows],
            )
            connection.commit()
        return len(rows)

    def replace_players(self, season: int, players: Iterable[Player]) -> int:
        rows = list(players)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM players WHERE season=?", (season,))
            connection.executemany(
                "INSERT INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(p.season, p.player_id, p.team, p.full_name, normalize_name(p.full_name),
                  p.first_name, p.last_name, p.position, p.depth_position, p.jersey_number,
                  p.status, p.birth_date, p.height, p.weight, p.college, p.years_experience,
                  p.headshot_url, p.pff_id, p.pfr_id, p.espn_id, now) for p in rows],
            )
            connection.execute(
                """DELETE FROM nfl_content_players
                   WHERE season=? AND NOT EXISTS (
                     SELECT 1 FROM players p
                     WHERE p.season=nfl_content_players.season
                       AND p.player_id=nfl_content_players.player_id
                   )""",
                (season,),
            )
            connection.commit()
        return len(rows)

    @staticmethod
    def _pivot_weekly_values(rows: Iterable[Mapping[str, Any]]) -> list[tuple]:
        values: list[tuple] = []
        for row in rows:
            player_id = str(row.get("player_id") or "").strip()
            game_id = str(row.get("game_id") or "").strip()
            if not player_id or not game_id:
                continue
            for metric, raw_value in row.items():
                if metric in WEEKLY_ID_COLUMNS:
                    continue
                value = optional_float(raw_value)
                if value is None:
                    continue
                values.append((
                    int(row["season"]), int(row["week"]), str(row.get("season_type") or "REG"),
                    game_id, player_id,
                    str(row.get("player_display_name") or row.get("player_name") or "Unknown"),
                    str(row.get("team") or ""), str(row.get("opponent_team") or ""),
                    str(row.get("position") or "") or None, metric, value,
                ))
        return values

    def replace_weekly_stats(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        values = self._pivot_weekly_values(rows)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM player_weekly_stats WHERE season=?", (season,))
            connection.executemany(
                "INSERT INTO player_weekly_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)", values,
            )
            connection.commit()
        return len(values)

    def upsert_weekly_stats(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Add or replace specific (game, player, metric) rows without
        touching the rest of the season -- for a metric source (Next Gen
        Stats) that syncs independently of the season-wide weekly-stats
        replace above and must not wipe out whatever that one already
        wrote."""
        values = self._pivot_weekly_values(rows)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR REPLACE INTO player_weekly_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)", values,
            )
            connection.commit()
        return len(values)

    def replace_snap_counts(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for row in rows:
            game_id = str(row.get("game_id") or "").strip()
            name = str(row.get("player") or "").strip()
            team = str(row.get("team") or "").strip()
            pfr_id = str(row.get("pfr_player_id") or "").strip() or None
            if not game_id or not name or not team:
                continue
            values.append((
                season, int(row.get("week") or 0), game_id,
                str(row.get("pfr_game_id") or "").strip() or None,
                pfr_id or normalize_name(name), pfr_id, name, normalize_name(name),
                str(row.get("position") or "").strip() or None, canon_team(team),
                canon_team(row.get("opponent")), optional_float(row.get("offense_snaps")),
                optional_float(row.get("offense_pct")), optional_float(row.get("defense_snaps")),
                optional_float(row.get("defense_pct")), optional_float(row.get("st_snaps")),
                optional_float(row.get("st_pct")),
            ))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM snap_counts WHERE season=?", (season,))
            connection.executemany("INSERT OR REPLACE INTO snap_counts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            connection.commit()
        return len(values)

    def replace_depth_charts(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for row in rows:
            snapshot = str(row.get("dt") or "").strip()
            team = canon_team(row.get("team"))
            name = str(row.get("player_name") or "").strip()
            gsis_id = str(row.get("gsis_id") or "").strip() or None
            espn_id = str(row.get("espn_id") or "").strip() or None
            if not snapshot or not team or not name:
                continue
            values.append((
                season, snapshot, team, gsis_id or espn_id or normalize_name(name), name,
                gsis_id, espn_id, str(row.get("pos_grp") or "").strip() or None,
                str(row.get("pos_id") or "").strip() or None,
                str(row.get("pos_name") or "").strip() or None,
                str(row.get("pos_abb") or "").strip() or None,
                optional_int(row.get("pos_slot")), optional_int(row.get("pos_rank")),
            ))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM depth_chart_snapshots WHERE season=?", (season,))
            connection.executemany("INSERT OR REPLACE INTO depth_chart_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            connection.commit()
        return len(values)

    def replace_team_staff(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        self.initialize()
        values = []
        role_columns = (
            ("head_coach", "Head coach"),
            ("offensive_coordinator", "Offensive coordinator"),
            ("defensive_coordinator", "Defensive coordinator"),
            ("general_manager", "General manager"),
        )
        for row in rows:
            team = canon_team(row.get("team"))
            playcaller = optional_text(row.get("offensive_playcaller"))
            for key, label in role_columns:
                name = optional_text(row.get(key))
                if team and name:
                    values.append((season, team, label, name, int(name == playcaller),
                                   optional_text(row.get("as_of")),
                                   optional_text(row.get("source_url"))))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM team_staff WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO team_staff VALUES (?,?,?,?,?,?,?)", values,
            )
            connection.commit()
        return len(values)

    def replace_team_scheme_rates(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        self.initialize()
        values = [(season, canon_team(row.get("team")), optional_float(row.get("blitz_rate")),
                   optional_float(row.get("light_box_rate")), optional_float(row.get("heavy_box_rate")),
                   optional_float(row.get("sub_package_rate")))
                  for row in rows if canon_team(row.get("team"))]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM team_scheme_rates WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO team_scheme_rates VALUES (?,?,?,?,?,?)", values,
            )
            connection.commit()
        return len(values)

    def replace_team_pressure_rates(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        columns = ("games", "dadot", "air_yards", "yards_after_catch", "blitzes", "blitz_rate",
                  "hurries", "hurry_rate", "qb_knockdowns", "knockdown_rate", "sacks",
                  "pressures", "pressure_rate", "missed_tackles")
        self.initialize()
        values = [(season, canon_team(row.get("team")), *(optional_float(row.get(key)) for key in columns))
                  for row in rows if canon_team(row.get("team"))]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM team_pressure_rates WHERE season=?", (season,))
            connection.executemany(
                f"INSERT OR REPLACE INTO team_pressure_rates VALUES ({','.join('?' * (2 + len(columns)))})",
                values,
            )
            connection.commit()
        return len(values)

    def replace_injuries(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        self.initialize()
        def clean(value: Any) -> str | None:
            text = optional_text(value)
            return None if not text or text.casefold() == "not specified" else text

        records = list(rows)
        teams = {canon_team(row.get("team")) for row in records if row.get("team")}
        with closing(self._connect()) as connection:
            player_ids = {(str(row["espn_id"]), row["team"]): row["player_id"]
                          for row in connection.execute(
                              "SELECT espn_id,team,player_id FROM players WHERE season=? AND espn_id IS NOT NULL",
                              (season,))}
            values = []
            for row in records:
                team = canon_team(row.get("team"))
                espn_id = str(row.get("espn_id") or "").strip()
                if not team or not espn_id:
                    continue
                values.append((
                    season, team, str(row.get("injury_id") or f"{espn_id}:{row.get('designation')}"),
                    espn_id, player_ids.get((espn_id, team)),
                    str(row.get("player_name") or "Unknown"), clean(row.get("position")),
                    str(row.get("designation") or "Unknown"), clean(row.get("status")),
                    clean(row.get("injury_type")), clean(row.get("location")),
                    clean(row.get("detail")), clean(row.get("side")),
                    clean(row.get("practice_status")), clean(row.get("return_date")),
                    clean(row.get("short_comment")), clean(row.get("long_comment")),
                    clean(row.get("report_date")), clean(row.get("note_source")),
                    str(row.get("fetched_at") or datetime.now(timezone.utc).isoformat()),
                    str(row.get("source_url") or ""),
                ))
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM injury_reports WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO injury_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.commit()
        return len(values)

    def replace_player_master(self, rows: Iterable[Mapping[str, Any]]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        records = []
        ids = []
        for row in rows:
            gsis_id = _external_id(row.get("gsis_id"))
            name = str(row.get("display_name") or "").strip()
            if not gsis_id or not name:
                continue
            records.append((
                gsis_id, name, normalize_name(name), optional_text(row.get("first_name")),
                optional_text(row.get("last_name")), optional_text(row.get("birth_date")),
                optional_text(row.get("position_group")), optional_text(row.get("position")),
                optional_float(row.get("height")), optional_int(row.get("weight")),
                optional_text(row.get("headshot")), optional_text(row.get("college_name")),
                optional_text(row.get("college_conference")),
                optional_int(row.get("rookie_season")), optional_int(row.get("last_season")),
                canon_team(optional_text(row.get("latest_team"))) or None,
                optional_text(row.get("status")),
                optional_int(row.get("years_of_experience")), optional_int(row.get("draft_year")),
                optional_int(row.get("draft_round")), optional_int(row.get("draft_pick")),
                canon_team(optional_text(row.get("draft_team"))) or None, now,
            ))
            for provider in MASTER_ID_COLUMNS:
                value = _external_id(row.get(provider))
                if value:
                    ids.append(("nflverse_players", gsis_id, provider.removesuffix("_id"), value, now))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM player_master")
            connection.execute("DELETE FROM player_external_ids WHERE source='nflverse_players'")
            connection.executemany("INSERT INTO player_master VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", records)
            connection.executemany("INSERT OR REPLACE INTO player_external_ids VALUES (?,?,?,?,?)", ids)
            connection.commit()
        return len(records)

    def replace_player_id_crosswalk(self, rows: Iterable[Mapping[str, Any]]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        ids = []
        linked = set()
        for row in rows:
            gsis_id = _external_id(row.get("gsis_id"))
            if not gsis_id:
                continue
            linked.add(gsis_id)
            for provider in CROSSWALK_ID_COLUMNS:
                value = _external_id(row.get(provider))
                if value:
                    ids.append(("dynastyprocess", gsis_id, provider.removesuffix("_id"), value, now))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM player_external_ids WHERE source='dynastyprocess'")
            connection.executemany("INSERT OR REPLACE INTO player_external_ids VALUES (?,?,?,?,?)", ids)
            connection.commit()
        return len(linked)

    def replace_team_weekly_stats(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        identity = {"season", "week", "season_type", "team", "opponent_team"}
        values = []
        for row in rows:
            team = canon_team(row.get("team")); opponent = canon_team(row.get("opponent_team"))
            if not team or not opponent:
                continue
            for metric, raw in row.items():
                if metric in identity:
                    continue
                value = optional_float(raw)
                if value is not None:
                    values.append((season, int(row["week"]), str(row.get("season_type") or "REG"),
                                   team, opponent, metric, value))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM team_weekly_stats WHERE season=?", (season,))
            connection.executemany("INSERT OR REPLACE INTO team_weekly_stats VALUES (?,?,?,?,?,?,?)", values)
            connection.commit()
        return len(values)

    def replace_game_efficiency(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        games: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            game_id = str(row.get("game_id") or "").strip(); team = canon_team(row.get("posteam"))
            opponent = canon_team(row.get("defteam")); epa = optional_float(row.get("epa"))
            is_pass = optional_float(row.get("pass")) == 1
            is_rush = optional_float(row.get("rush")) == 1
            # Keep this an offensive efficiency model. nflfastR assigns EPA to
            # kickoffs, punts, conversions, and other non-scrimmage events too.
            if not game_id or not team or not opponent or epa is None or not (is_pass or is_rush):
                continue
            item = games.setdefault((game_id, team), {
                "week": int(row.get("week") or 0), "opponent": opponent, "plays": 0,
                "total_epa": 0.0, "successful": 0.0, "pass_plays": 0, "pass_epa": 0.0,
                "rush_plays": 0, "rush_epa": 0.0, "early_plays": 0, "early_epa": 0.0,
                "explosive": 0,
            })
            item["plays"] += 1; item["total_epa"] += epa
            item["successful"] += float(optional_float(row.get("success")) or 0)
            if is_pass:
                item["pass_plays"] += 1
                item["pass_epa"] += optional_float(row.get("qb_epa")) if optional_float(row.get("qb_epa")) is not None else epa
            if is_rush:
                item["rush_plays"] += 1; item["rush_epa"] += epa
            down = optional_int(row.get("down"))
            if down in {1, 2}:
                item["early_plays"] += 1; item["early_epa"] += epa
            if (optional_float(row.get("yards_gained")) or 0) >= 10:
                item["explosive"] += 1
        values = [(season, item["week"], game_id, team, item["opponent"], item["plays"],
                   item["total_epa"], item["successful"], item["pass_plays"], item["pass_epa"],
                   item["rush_plays"], item["rush_epa"], item["early_plays"], item["early_epa"],
                   item["explosive"]) for (game_id, team), item in games.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM game_team_efficiency WHERE season=?", (season,))
            connection.executemany("INSERT OR REPLACE INTO game_team_efficiency VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
            connection.commit()
        return len(values)

    def replace_game_situational(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist pace, down, red-zone, and neutral-script context per offense."""
        games: dict[tuple[str, str], dict[str, Any]] = {}
        last_clock: dict[tuple[str, str], float] = {}
        drives: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in rows:
            game_id = str(row.get("game_id") or "").strip()
            team = canon_team(row.get("posteam")); opponent = canon_team(row.get("defteam"))
            is_pass = optional_float(row.get("pass")) == 1
            is_rush = optional_float(row.get("rush")) == 1
            if not game_id or not team or not opponent or not (is_pass or is_rush):
                continue
            key = (game_id, team)
            item = games.setdefault(key, {
                "week": int(row.get("week") or 0), "opponent": opponent, "plays": 0,
                "third": 0, "third_converted": 0, "red_zone": 0, "red_zone_success": 0,
                "neutral": 0, "neutral_pass": 0, "seconds": 0.0, "clocked": 0,
            })
            item["plays"] += 1
            down = optional_int(row.get("down"))
            if down == 3:
                item["third"] += 1
                item["third_converted"] += int(optional_float(row.get("third_down_converted")) == 1)
            yardline = optional_float(row.get("yardline_100"))
            if yardline is not None and yardline <= 20:
                item["red_zone"] += 1
                item["red_zone_success"] += int((optional_float(row.get("success")) or 0) == 1)
            score_diff = optional_float(row.get("score_differential"))
            quarter = optional_int(row.get("qtr"))
            if score_diff is not None and abs(score_diff) <= 8 and (quarter or 0) <= 3:
                item["neutral"] += 1
                item["neutral_pass"] += int(is_pass)
            drive = str(row.get("drive") or "").strip()
            if drive:
                drives[key].add(drive)
            clock = optional_float(row.get("game_seconds_remaining"))
            prior_clock = last_clock.get(key)
            if clock is not None and prior_clock is not None:
                elapsed = prior_clock - clock
                if 0 < elapsed <= 60:
                    item["seconds"] += elapsed; item["clocked"] += 1
            if clock is not None:
                last_clock[key] = clock
        values = [(
            season, item["week"], game_id, team, item["opponent"], item["plays"],
            len(drives[(game_id, team)]), item["third"], item["third_converted"],
            item["red_zone"], item["red_zone_success"], item["neutral"],
            item["neutral_pass"], item["seconds"], item["clocked"],
        ) for (game_id, team), item in games.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM game_team_situational WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO game_team_situational VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.commit()
        return len(values)

    def replace_game_playcalling(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist auditable run/pass and formation signals per offense."""
        self.initialize()
        games: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            game_id = str(row.get("game_id") or "").strip()
            team = canon_team(row.get("posteam")); opponent = canon_team(row.get("defteam"))
            is_pass = optional_float(row.get("pass")) == 1
            is_rush = optional_float(row.get("rush")) == 1
            if not game_id or not team or not opponent or not (is_pass or is_rush):
                continue
            item = games.setdefault((game_id, team), {
                "week": int(row.get("week") or 0), "opponent": opponent,
                "pass": 0, "rush": 0, "early": 0, "early_pass": 0,
                "shotgun_known": 0, "shotgun": 0, "no_huddle_known": 0, "no_huddle": 0,
            })
            item["pass"] += int(is_pass); item["rush"] += int(is_rush)
            if optional_int(row.get("down")) in {1, 2}:
                item["early"] += 1; item["early_pass"] += int(is_pass)
            shotgun = optional_float(row.get("shotgun"))
            if shotgun is not None:
                item["shotgun_known"] += 1; item["shotgun"] += int(shotgun == 1)
            no_huddle = optional_float(row.get("no_huddle"))
            if no_huddle is not None:
                item["no_huddle_known"] += 1; item["no_huddle"] += int(no_huddle == 1)
        values = [(
            season, item["week"], game_id, team, item["opponent"], item["pass"], item["rush"],
            item["early"], item["early_pass"], item["shotgun_known"], item["shotgun"],
            item["no_huddle_known"], item["no_huddle"],
        ) for (game_id, team), item in games.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM game_team_playcalling WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO game_team_playcalling VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.commit()
        return len(values)

    def replace_qb_pass_profiles(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist actual targeted passes by depth and horizontal location.

        Sacks, spikes, and throwaways without an nflfastR air-yards value are
        excluded because they cannot be assigned honestly to a pass zone.
        """
        self.initialize()
        profiles: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        # Coverage assignment isn't in this data, but who broke up or picked
        # off a specific target is -- that's an honest, already-attributed
        # defensive event, not a fabricated one, so it rides along with the
        # same zone classification as the attempt itself.
        defenders: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            if optional_float(row.get("pass_attempt")) != 1:
                continue
            air_yards = optional_float(row.get("air_yards"))
            game_id = str(row.get("game_id") or "").strip()
            passer_id = str(row.get("passer_player_id") or "").strip()
            offense = canon_team(row.get("posteam")); defense = canon_team(row.get("defteam"))
            if air_yards is None or not game_id or not passer_id or not offense or not defense:
                continue
            depth = ("behind" if air_yards <= 0 else "short" if air_yards < 10
                     else "intermediate" if air_yards < 20 else "deep")
            location = str(row.get("pass_location") or "unknown").strip().casefold()
            if location not in {"left", "middle", "right"}:
                location = "unknown"
            key = (game_id, passer_id, depth, location)
            item = profiles.setdefault(key, {
                "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                "name": str(row.get("passer_player_name") or passer_id), "attempts": 0,
                "completions": 0, "yards": 0.0, "air_yards": 0.0, "epa": 0.0,
                "touchdowns": 0, "interceptions": 0, "cpoe_total": 0.0,
                "cpoe_plays": 0,
            })
            item["attempts"] += 1
            item["completions"] += int(optional_float(row.get("complete_pass")) == 1)
            item["yards"] += optional_float(row.get("passing_yards")) or 0.0
            item["air_yards"] += air_yards
            item["epa"] += optional_float(row.get("epa")) or 0.0
            item["touchdowns"] += int(optional_float(row.get("pass_touchdown")) == 1)
            item["interceptions"] += int(optional_float(row.get("interception")) == 1)
            cpoe = optional_float(row.get("cpoe"))
            if cpoe is not None:
                item["cpoe_total"] += cpoe; item["cpoe_plays"] += 1
            for event, id_field, name_field in (
                ("pass_defended", "pass_defense_1_player_id", "pass_defense_1_player_name"),
                ("interception", "interception_player_id", "interception_player_name"),
            ):
                defender_id = str(row.get(id_field) or "").strip()
                if not defender_id:
                    continue
                defender_key = (game_id, passer_id, depth, location, defender_id, event)
                defender = defenders.setdefault(defender_key, {
                    "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                    "name": str(row.get(name_field) or defender_id), "count": 0,
                })
                defender["count"] += 1
        values = [(
            season, item["week"], game_id, item["offense"], item["defense"], passer_id,
            item["name"], depth, location, item["attempts"], item["completions"],
            item["yards"], item["air_yards"], item["epa"], item["touchdowns"],
            item["interceptions"], item["cpoe_total"], item["cpoe_plays"],
        ) for (game_id, passer_id, depth, location), item in profiles.items()]
        defender_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], passer_id,
            depth, location, defender_id, item["name"], event, item["count"],
        ) for (game_id, passer_id, depth, location, defender_id, event), item in defenders.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM qb_pass_profiles WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO qb_pass_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.execute("DELETE FROM pass_zone_defenders WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO pass_zone_defenders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                defender_values,
            )
            connection.commit()
        return len(values)

    def replace_receiver_pass_profiles(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist targets and catch outcomes by receiver, depth, and location."""
        self.initialize()
        profiles: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        contributors: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            if optional_float(row.get("pass_attempt")) != 1:
                continue
            game_id = str(row.get("game_id") or "").strip()
            receiver_id = str(row.get("receiver_player_id") or "").strip()
            air_yards = optional_float(row.get("air_yards"))
            offense = canon_team(row.get("posteam")); defense = canon_team(row.get("defteam"))
            if not game_id or not receiver_id or air_yards is None or not offense or not defense:
                continue
            depth = ("behind" if air_yards <= 0 else "short" if air_yards < 10
                     else "intermediate" if air_yards < 20 else "deep")
            location = str(row.get("pass_location") or "unknown").strip().casefold()
            if location not in {"left", "middle", "right"}:
                location = "unknown"
            key = (game_id, receiver_id, depth, location)
            item = profiles.setdefault(key, {
                "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                "name": str(row.get("receiver_player_name") or receiver_id),
                "targets": 0, "receptions": 0, "yards": 0.0, "air_yards": 0.0,
                "yac": 0.0, "epa": 0.0, "touchdowns": 0,
            })
            item["targets"] += 1
            item["receptions"] += int(optional_float(row.get("complete_pass")) == 1)
            item["yards"] += optional_float(row.get("receiving_yards")) or 0.0
            item["air_yards"] += air_yards
            item["yac"] += optional_float(row.get("yards_after_catch")) or 0.0
            item["epa"] += optional_float(row.get("epa")) or 0.0
            item["touchdowns"] += int(optional_float(row.get("pass_touchdown")) == 1)
            passer_id = str(row.get("passer_player_id") or "").strip()
            if passer_id:
                contributor = contributors.setdefault(
                    (game_id, passer_id, receiver_id, depth, location), {
                        "week": int(row.get("week") or 0), "offense": offense,
                        "defense": defense,
                        "name": str(row.get("receiver_player_name") or receiver_id),
                        "targets": 0, "receptions": 0, "yards": 0.0,
                        "air_yards": 0.0, "touchdowns": 0,
                    })
                contributor["targets"] += 1
                contributor["receptions"] += int(optional_float(row.get("complete_pass")) == 1)
                contributor["yards"] += optional_float(row.get("receiving_yards")) or 0.0
                contributor["air_yards"] += air_yards
                contributor["touchdowns"] += int(optional_float(row.get("pass_touchdown")) == 1)
        values = [(
            season, item["week"], game_id, item["offense"], item["defense"], receiver_id,
            item["name"], depth, location, item["targets"], item["receptions"], item["yards"],
            item["air_yards"], item["yac"], item["epa"], item["touchdowns"],
        ) for (game_id, receiver_id, depth, location), item in profiles.items()]
        contributor_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], passer_id,
            receiver_id, item["name"], depth, location, item["targets"], item["receptions"],
            item["yards"], item["air_yards"], item["touchdowns"],
        ) for (game_id, passer_id, receiver_id, depth, location), item in contributors.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM receiver_pass_profiles WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO receiver_pass_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.execute("DELETE FROM pass_zone_receivers WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO pass_zone_receivers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                contributor_values,
            )
            connection.commit()
        return len(values)

    @staticmethod
    def _run_direction(location: Any, gap: Any) -> str | None:
        """Classify a rush into the standard 7-cell run-direction chart.

        nflfastR's `run_location` (left/middle/right) and `run_gap`
        (end/tackle/guard, null for middle runs) combine into the same
        left end/tackle/guard - middle - right guard/tackle/end buckets an
        NFL broadcast run chart uses, rather than the coarser 3-way split
        `pass_location` alone gives passing zones.
        """
        location = str(location or "").strip().casefold()
        gap = str(gap or "").strip().casefold()
        if location == "middle":
            return "middle"
        if location in {"left", "right"} and gap in {"end", "tackle", "guard"}:
            return f"{location} {gap}"
        return None

    def replace_rush_direction_profiles(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist carries by rusher and run direction (the broadcast run-chart cells).

        Runs without a classifiable location/gap (kneels, laterals, older
        seasons with partial charting) are excluded rather than bucketed as
        "unknown" -- an unclassified cell would not be comparable across
        games the way every other zone here is.
        """
        self.initialize()
        profiles: dict[tuple[str, str, str], dict[str, Any]] = {}
        # The ball carrier is who a "run direction" chart is normally read
        # for, but the tackler is the honest, already-attributed defensive
        # side of the same play (nflfastR records who made the stop, not
        # who was assigned to that gap) -- solo tackle wins over an assisted
        # one when both are recorded, matching how a broadcast box score
        # credits one primary tackler.
        defenders: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            if optional_float(row.get("rush")) != 1:
                continue
            rusher_id = str(row.get("rusher_player_id") or "").strip()
            game_id = str(row.get("game_id") or "").strip()
            direction = self._run_direction(row.get("run_location"), row.get("run_gap"))
            offense = canon_team(row.get("posteam")); defense = canon_team(row.get("defteam"))
            if not game_id or not rusher_id or not direction or not offense or not defense:
                continue
            key = (game_id, rusher_id, direction)
            item = profiles.setdefault(key, {
                "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                "name": str(row.get("rusher_player_name") or rusher_id),
                "attempts": 0, "yards": 0.0, "epa": 0.0, "touchdowns": 0,
            })
            item["attempts"] += 1
            item["yards"] += optional_float(row.get("yards_gained")) or 0.0
            item["epa"] += optional_float(row.get("epa")) or 0.0
            item["touchdowns"] += int(optional_float(row.get("rush_touchdown")) == 1)
            tackler_id = str(row.get("solo_tackle_1_player_id")
                             or row.get("tackle_with_assist_1_player_id") or "").strip()
            if tackler_id:
                tackler_name = str(row.get("solo_tackle_1_player_name")
                                   or row.get("tackle_with_assist_1_player_name") or tackler_id)
                defender = defenders.setdefault((game_id, direction, tackler_id), {
                    "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                    "name": tackler_name, "tackles": 0,
                })
                defender["tackles"] += 1
        values = [(
            season, item["week"], game_id, item["offense"], item["defense"], rusher_id,
            item["name"], direction, item["attempts"], item["yards"], item["epa"],
            item["touchdowns"],
        ) for (game_id, rusher_id, direction), item in profiles.items()]
        defender_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], direction,
            defender_id, item["name"], item["tackles"],
        ) for (game_id, direction, defender_id), item in defenders.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM rush_direction_profiles WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO rush_direction_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.execute("DELETE FROM rush_direction_defenders WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO rush_direction_defenders VALUES (?,?,?,?,?,?,?,?,?)",
                defender_values,
            )
            connection.commit()
        return len(values)

    def replace_rush_situational_profiles(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist red-zone carries -- the same "high-value opportunity" split
        the passing situational tables track, for the run game."""
        self.initialize()
        profiles: dict[tuple[str, str], dict[str, Any]] = {}
        defenders: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if optional_float(row.get("rush")) != 1:
                continue
            yardline = optional_float(row.get("yardline_100"))
            if yardline is None or yardline > 20:
                continue
            rusher_id = str(row.get("rusher_player_id") or "").strip()
            game_id = str(row.get("game_id") or "").strip()
            offense = canon_team(row.get("posteam")); defense = canon_team(row.get("defteam"))
            if not game_id or not rusher_id or not offense or not defense:
                continue
            key = (game_id, rusher_id)
            item = profiles.setdefault(key, {
                "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                "name": str(row.get("rusher_player_name") or rusher_id),
                "attempts": 0, "yards": 0.0, "epa": 0.0, "touchdowns": 0,
            })
            item["attempts"] += 1
            item["yards"] += optional_float(row.get("yards_gained")) or 0.0
            item["epa"] += optional_float(row.get("epa")) or 0.0
            item["touchdowns"] += int(optional_float(row.get("rush_touchdown")) == 1)
            tackler_id = str(row.get("solo_tackle_1_player_id")
                             or row.get("tackle_with_assist_1_player_id") or "").strip()
            if tackler_id:
                tackler_name = str(row.get("solo_tackle_1_player_name")
                                   or row.get("tackle_with_assist_1_player_name") or tackler_id)
                defender = defenders.setdefault((game_id, tackler_id), {
                    "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                    "name": tackler_name, "tackles": 0,
                })
                defender["tackles"] += 1
        values = [(
            season, item["week"], game_id, item["offense"], item["defense"], rusher_id,
            item["name"], item["attempts"], item["yards"], item["epa"], item["touchdowns"],
        ) for (game_id, rusher_id), item in profiles.items()]
        defender_values = [(
            season, item["week"], game_id, item["offense"], item["defense"],
            defender_id, item["name"], item["tackles"],
        ) for (game_id, defender_id), item in defenders.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM rush_situational_profiles WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO rush_situational_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            connection.execute("DELETE FROM rush_situational_defenders WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO rush_situational_defenders VALUES (?,?,?,?,?,?,?,?)",
                defender_values,
            )
            connection.commit()
        return len(values)

    def replace_situational_pass_profiles(self, season: int, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist red-zone and end-zone splits for passers and targeted receivers.

        Red zone: the play started at or inside the defense's 20 (the
        standard definition). End zone: the target depth reaches or crosses
        the goal line (`yardline_100 - air_yards <= 0`), which catches
        incompletions and interceptions thrown into the end zone as well as
        touchdowns -- a target, not just a score. A play can count toward
        both, or neither.
        """
        self.initialize()
        passers: dict[tuple[str, str, str], dict[str, Any]] = {}
        receivers: dict[tuple[str, str, str], dict[str, Any]] = {}
        contributors: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        defenders: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            if optional_float(row.get("pass_attempt")) != 1:
                continue
            game_id = str(row.get("game_id") or "").strip()
            passer_id = str(row.get("passer_player_id") or "").strip()
            offense = canon_team(row.get("posteam")); defense = canon_team(row.get("defteam"))
            if not game_id or not passer_id or not offense or not defense:
                continue
            yardline = optional_float(row.get("yardline_100"))
            air_yards = optional_float(row.get("air_yards"))
            situations = []
            if yardline is not None and yardline <= 20:
                situations.append("red_zone")
            if yardline is not None and air_yards is not None and (yardline - air_yards) <= 0:
                situations.append("end_zone")
            if not situations:
                continue
            complete = optional_float(row.get("complete_pass")) == 1
            epa = optional_float(row.get("epa")) or 0.0
            touchdown = int(optional_float(row.get("pass_touchdown")) == 1)
            interception = int(optional_float(row.get("interception")) == 1)
            passing_yards = optional_float(row.get("passing_yards")) or 0.0
            receiver_id = str(row.get("receiver_player_id") or "").strip()
            for situation in situations:
                item = passers.setdefault((game_id, passer_id, situation), {
                    "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                    "name": str(row.get("passer_player_name") or passer_id),
                    "attempts": 0, "completions": 0, "yards": 0.0, "epa": 0.0,
                    "touchdowns": 0, "interceptions": 0,
                })
                item["attempts"] += 1; item["completions"] += int(complete)
                item["yards"] += passing_yards; item["epa"] += epa
                item["touchdowns"] += touchdown; item["interceptions"] += interception
                if receiver_id:
                    target = receivers.setdefault((game_id, receiver_id, situation), {
                        "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                        "name": str(row.get("receiver_player_name") or receiver_id),
                        "targets": 0, "receptions": 0, "yards": 0.0, "epa": 0.0, "touchdowns": 0,
                    })
                    target["targets"] += 1
                    target["receptions"] += int(complete)
                    target["yards"] += optional_float(row.get("receiving_yards")) or 0.0
                    target["epa"] += epa
                    target["touchdowns"] += touchdown
                    contributor = contributors.setdefault(
                        (game_id, passer_id, receiver_id, situation), {
                            "week": int(row.get("week") or 0), "offense": offense,
                            "defense": defense,
                            "name": str(row.get("receiver_player_name") or receiver_id),
                            "targets": 0, "receptions": 0, "yards": 0.0, "touchdowns": 0,
                        })
                    contributor["targets"] += 1
                    contributor["receptions"] += int(complete)
                    contributor["yards"] += optional_float(row.get("receiving_yards")) or 0.0
                    contributor["touchdowns"] += touchdown
                for event, id_field, name_field in (
                    ("pass_defended", "pass_defense_1_player_id", "pass_defense_1_player_name"),
                    ("interception", "interception_player_id", "interception_player_name"),
                ):
                    defender_id = str(row.get(id_field) or "").strip()
                    if not defender_id:
                        continue
                    defender_key = (game_id, passer_id, situation, defender_id, event)
                    defender = defenders.setdefault(defender_key, {
                        "week": int(row.get("week") or 0), "offense": offense, "defense": defense,
                        "name": str(row.get(name_field) or defender_id), "count": 0,
                    })
                    defender["count"] += 1
        passer_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], passer_id,
            item["name"], situation, item["attempts"], item["completions"], item["yards"],
            item["epa"], item["touchdowns"], item["interceptions"],
        ) for (game_id, passer_id, situation), item in passers.items()]
        receiver_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], receiver_id,
            item["name"], situation, item["targets"], item["receptions"], item["yards"],
            item["epa"], item["touchdowns"],
        ) for (game_id, receiver_id, situation), item in receivers.items()]
        contributor_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], passer_id,
            receiver_id, item["name"], situation, item["targets"], item["receptions"],
            item["yards"], item["touchdowns"],
        ) for (game_id, passer_id, receiver_id, situation), item in contributors.items()]
        defender_values = [(
            season, item["week"], game_id, item["offense"], item["defense"], passer_id,
            situation, defender_id, item["name"], event, item["count"],
        ) for (game_id, passer_id, situation, defender_id, event), item in defenders.items()]
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM qb_situational_profiles WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO qb_situational_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                passer_values,
            )
            connection.execute("DELETE FROM receiver_situational_profiles WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO receiver_situational_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                receiver_values,
            )
            connection.execute("DELETE FROM situational_pass_receivers WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO situational_pass_receivers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                contributor_values,
            )
            connection.execute("DELETE FROM situational_pass_defenders WHERE season=?", (season,))
            connection.executemany(
                "INSERT OR REPLACE INTO situational_pass_defenders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                defender_values,
            )
            connection.commit()
        return len(passer_values)

    def record_sync(self, report: SyncReport) -> None:
        details = [vars(item) if hasattr(item, "__dict__") else {
            "dataset": item.dataset, "count": item.count, "status": item.status, "message": item.message
        } for item in report.datasets]
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO sync_runs (season,started_at,finished_at,succeeded,details_json) VALUES (?,?,?,?,?)",
                (report.season, report.started_at.isoformat(), report.finished_at.isoformat(),
                 int(report.succeeded), json.dumps(details)),
            )
            connection.commit()

    def counts(self, season: int) -> dict[str, int]:
        with closing(self._connect()) as connection:
            return {
                "teams": connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0],
                "games": connection.execute("SELECT COUNT(*) FROM games WHERE season=?", (season,)).fetchone()[0],
                "players": connection.execute("SELECT COUNT(*) FROM players WHERE season=?", (season,)).fetchone()[0],
                "weekly_metrics": connection.execute(
                    "SELECT COUNT(*) FROM player_weekly_stats WHERE season=?", (season,)
                ).fetchone()[0],
                "snap_counts": connection.execute(
                    "SELECT COUNT(*) FROM snap_counts WHERE season=?", (season,)
                ).fetchone()[0],
                "depth_snapshots": connection.execute(
                    "SELECT COUNT(*) FROM depth_chart_snapshots WHERE season=?", (season,)
                ).fetchone()[0],
                "player_master": connection.execute("SELECT COUNT(*) FROM player_master").fetchone()[0],
                "external_ids": connection.execute("SELECT COUNT(*) FROM player_external_ids").fetchone()[0],
                "team_metrics": connection.execute(
                    "SELECT COUNT(*) FROM team_weekly_stats WHERE season=?", (season,)
                ).fetchone()[0],
                "game_efficiency": connection.execute(
                    "SELECT COUNT(*) FROM game_team_efficiency WHERE season=?", (season,)
                ).fetchone()[0],
                "game_situational": connection.execute(
                    "SELECT COUNT(*) FROM game_team_situational WHERE season=?", (season,)
                ).fetchone()[0],
                "elo_games": connection.execute("SELECT COUNT(*) FROM nfl_elo_games").fetchone()[0],
            }

    def latest_season(self) -> int | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT MAX(season) FROM (SELECT season FROM games UNION ALL SELECT season FROM players)"
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def season_has_data(self, season: int) -> bool:
        """Cheap presence check, used to decide whether the real current season
        is usable as a default before falling back to whatever season is
        actually the most complete."""
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT 1 FROM games WHERE season=?
                    UNION SELECT 1 FROM players WHERE season=? LIMIT 1""",
                (season, season),
            ).fetchone()
        return row is not None

    def available_seasons(self) -> list[int]:
        """Every season with a stored schedule, most recent first -- backs the
        season selector rather than assuming a fixed range."""
        self.initialize()
        with closing(self._connect()) as connection:
            return [int(row[0]) for row in connection.execute(
                "SELECT DISTINCT season FROM games ORDER BY season DESC"
            )]

    def list_teams(self) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM teams ORDER BY conference,division,name"
            )]

    def get_team(self, abbreviation: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM teams WHERE abbreviation=?", (abbreviation,)
            ).fetchone()
        return dict(row) if row else None

    def seed_team_venues(self) -> None:
        """Upsert the static stadium geography -- idempotent, cheap, and
        independent of replace_teams()'s full nflverse-driven replace so a
        routine sync can never wipe it."""
        from sports_aggregator.nfl.venues import TEAM_VENUES
        with closing(self._connect()) as connection:
            connection.executemany(
                """INSERT INTO team_venues VALUES (?,?,?,?,?,?)
                   ON CONFLICT(team) DO UPDATE SET
                   venue_name=excluded.venue_name, latitude=excluded.latitude,
                   longitude=excluded.longitude, elevation_meters=excluded.elevation_meters,
                   dome=excluded.dome""",
                [(team, venue_name, latitude, longitude, elevation, int(dome))
                 for team, (venue_name, latitude, longitude, elevation, dome)
                 in TEAM_VENUES.items()],
            )
            connection.commit()

    def team_venues(self) -> dict[str, dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return {row["team"]: dict(row) for row in connection.execute(
                "SELECT * FROM team_venues"
            )}

    def schedule(self, season: int, *, team: str | None = None) -> list[dict[str, Any]]:
        self.initialize()
        sql = "SELECT * FROM games WHERE season=?"
        params: list[object] = [season]
        if team:
            sql += " AND (away_team=? OR home_team=?)"
            params.extend((team, team))
        sql += " ORDER BY game_date,COALESCE(game_time,''),game_id"
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(sql, params)]

    def standings(self, season: int, *, before_week: int | None = None) -> list[dict[str, Any]]:
        """Compute regular-season records from canonical completed games."""
        teams = self.list_teams()
        records = {team["abbreviation"]: {
            **team, "wins": 0, "losses": 0, "ties": 0, "points_for": 0,
            "points_against": 0, "results": [],
        } for team in teams}
        for game in self.schedule(season):
            if (not game["completed"] or game["season_type"] != "REG"
                    or (before_week is not None and game["week"] >= before_week)):
                continue
            away = records.get(game["away_team"]); home = records.get(game["home_team"])
            if away is None or home is None:
                continue
            away_score = int(game["away_score"]); home_score = int(game["home_score"])
            away["points_for"] += away_score; away["points_against"] += home_score
            home["points_for"] += home_score; home["points_against"] += away_score
            if away_score > home_score:
                away["wins"] += 1; home["losses"] += 1
                away["results"].append("W"); home["results"].append("L")
            elif home_score > away_score:
                home["wins"] += 1; away["losses"] += 1
                home["results"].append("W"); away["results"].append("L")
            else:
                away["ties"] += 1; home["ties"] += 1
                away["results"].append("T"); home["results"].append("T")
        output = []
        for row in records.values():
            played = row["wins"] + row["losses"] + row["ties"]
            row["win_pct"] = (row["wins"] + row["ties"] * 0.5) / played if played else None
            row["point_diff"] = row["points_for"] - row["points_against"]
            row["record"] = f"{row['wins']}-{row['losses']}" + (f"-{row['ties']}" if row["ties"] else "")
            if row["results"]:
                last = row["results"][-1]
                length = 0
                for result in reversed(row["results"]):
                    if result != last:
                        break
                    length += 1
                row["streak"] = f"{last}{length}"
            else:
                row["streak"] = None
            row.pop("results")
            output.append(row)
        return sorted(output, key=lambda row: (
            row["conference"], row["division"],
            -(row["win_pct"] if row["win_pct"] is not None else -1), -row["point_diff"], row["name"],
        ))

    def team_record(self, season: int, team: str, *, before_week: int | None = None) -> dict[str, Any] | None:
        return next((row for row in self.standings(season, before_week=before_week)
                     if row["abbreviation"] == team), None)

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM games WHERE game_id=?", (game_id,)
            ).fetchone()
        return dict(row) if row else None

    def game_player_stats(self, game_id: str) -> list[dict[str, Any]]:
        """Return one wide row per player for a game's available numeric metrics."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT player_id,player_name,team,position,metric,SUM(value) value
                   FROM player_weekly_stats WHERE game_id=?
                   GROUP BY player_id,player_name,team,position,metric
                   ORDER BY team,player_name,metric""", (game_id,)
            )
            players: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                key = (row["player_id"], row["team"])
                player = players.setdefault(key, {
                    "player_id": row["player_id"], "player_name": row["player_name"],
                    "team": row["team"], "position": row["position"],
                })
                player[row["metric"]] = row["value"]
        return list(players.values())

    def player_leaders(self, season: int, metric: str, *, team: str | None = None,
                       before_week: int | None = None, week: int | None = None,
                       limit: int = 10) -> list[dict[str, Any]]:
        self.initialize()
        where = "season=? AND metric=?"
        parameters: list[Any] = [season, metric]
        if team:
            where += " AND team=?"
            parameters.append(team)
        if before_week is not None:
            where += " AND week<?"
            parameters.append(before_week)
        if week is not None:
            where += " AND week=?"
            parameters.append(week)
        parameters.append(max(1, min(int(limit), 100)))
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                """SELECT player_id,player_name,team,position,SUM(value) value,
                          COUNT(DISTINCT game_id) games
                   FROM player_weekly_stats WHERE """ + where + """
                   GROUP BY player_id,player_name,team,position
                   ORDER BY value DESC,player_name LIMIT ?""",
                parameters,
            )]

    def player_leaders_for_metrics(self, season: int, metrics: Iterable[str], *,
                                   team: str, before_week: int | None = None) -> list[dict[str, Any]]:
        """Aggregate several leader categories in one team-season table scan."""
        wanted = tuple(dict.fromkeys(str(metric) for metric in metrics if metric))
        if not wanted:
            return []
        self.initialize()
        placeholders = ",".join("?" for _ in wanted)
        week_filter = " AND week<?" if before_week is not None else ""
        parameters: list[Any] = [season, team, *wanted]
        if before_week is not None:
            parameters.append(before_week)
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                f"""SELECT metric,player_id,MAX(player_name) player_name,
                           MAX(position) position,SUM(value) value,
                           COUNT(DISTINCT game_id) games
                    FROM player_weekly_stats
                    WHERE season=? AND team=? AND metric IN ({placeholders}){week_filter}
                    GROUP BY metric,player_id
                    ORDER BY metric,value DESC,player_name""",
                parameters,
            )]

    def latest_stat_week(self, season: int) -> int | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT MAX(week) FROM player_weekly_stats WHERE season=?", (season,)
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def player_season_stats(self, season: int, metrics: Iterable[str], *,
                            team: str | None = None, position: str | None = None,
                            week_from: int | None = None, week_to: int | None = None,
                            minimum_games: int = 1) -> list[dict[str, Any]]:
        """One wide row per player: every requested metric summed for the season
        (or, with week_from/week_to, for just that slice of it).

        Pivots the long (season,week,player,metric,value) table via conditional
        aggregation so the stat explorer can sort/filter/plot across many
        metrics without one query per column.
        """
        wanted = tuple(dict.fromkeys(str(metric) for metric in metrics if metric))
        if not wanted:
            return []
        self.initialize()
        where = ["season=?"]
        where_params: list[Any] = [season]
        if team:
            where.append("team=?"); where_params.append(team)
        if position:
            where.append("position=?"); where_params.append(position)
        if week_from is not None:
            where.append("week>=?"); where_params.append(week_from)
        if week_to is not None:
            where.append("week<=?"); where_params.append(week_to)
        # No ELSE 0: a player who never had a row for this metric (a
        # lineman has no "passing_yards" row, ever) must SUM to NULL, not a
        # real zero, so the scatter plot and column filters can tell "never
        # played that role" apart from "played it and recorded a zero."
        case_columns = ",".join(
            f'SUM(CASE WHEN metric=? THEN value END) "{metric}"' for metric in wanted
        )
        parameters: list[Any] = [*wanted, *where_params, max(1, int(minimum_games))]
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                f"""SELECT player_id, MAX(player_name) player_name, MAX(team) team,
                           MAX(position) position, COUNT(DISTINCT game_id) games,
                           {case_columns}
                    FROM player_weekly_stats WHERE {' AND '.join(where)}
                    GROUP BY player_id
                    HAVING games >= ?""",
                parameters,
            )]

    def team_player_usage(self, season: int, team: str, *,
                          before_week: int | None = None) -> list[dict[str, Any]]:
        """Aggregate touch and target workload into one row per player."""
        self.initialize()
        metrics = (
            "targets", "carries", "receptions", "receiving_air_yards",
            "receiving_yards", "rushing_yards", "receiving_first_downs",
            "rushing_first_downs", "receiving_20", "rushing_20",
            "receiving_epa", "rushing_epa",
        )
        placeholders = ",".join("?" for _ in metrics)
        week_filter = " AND week<?" if before_week is not None else ""
        parameters: list[Any] = [season, team, *metrics]
        if before_week is not None:
            parameters.append(before_week)
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT player_id,MAX(player_name) player_name,MAX(position) position,
                            COUNT(DISTINCT game_id) games,
                            SUM(CASE WHEN metric='targets' THEN value ELSE 0 END) targets,
                            SUM(CASE WHEN metric='carries' THEN value ELSE 0 END) carries,
                            SUM(CASE WHEN metric='receptions' THEN value ELSE 0 END) receptions,
                            SUM(CASE WHEN metric='receiving_air_yards' THEN value ELSE 0 END) air_yards,
                            SUM(CASE WHEN metric='receiving_yards' THEN value ELSE 0 END) receiving_yards,
                            SUM(CASE WHEN metric='rushing_yards' THEN value ELSE 0 END) rushing_yards,
                            SUM(CASE WHEN metric IN ('receiving_first_downs','rushing_first_downs')
                                     THEN value ELSE 0 END) first_downs,
                            SUM(CASE WHEN metric IN ('receiving_20','rushing_20')
                                     THEN value ELSE 0 END) explosive_touches,
                            SUM(CASE WHEN metric IN ('receiving_epa','rushing_epa')
                                     THEN value ELSE 0 END) opportunity_epa
                     FROM player_weekly_stats
                     WHERE season=? AND team=? AND metric IN ({placeholders}){week_filter}
                     GROUP BY player_id""",
                parameters,
            )]
        total_targets = sum(row["targets"] for row in rows)
        total_carries = sum(row["carries"] for row in rows)
        total_opportunities = total_targets + total_carries
        for row in rows:
            opportunities = row["targets"] + row["carries"]
            row["opportunities"] = opportunities
            row["target_share"] = row["targets"] / total_targets if total_targets else None
            row["carry_share"] = row["carries"] / total_carries if total_carries else None
            row["opportunity_share"] = (opportunities / total_opportunities
                                        if total_opportunities else None)
            row["scrimmage_yards"] = row["receiving_yards"] + row["rushing_yards"]
        return sorted(rows, key=lambda row: (
            -(row["opportunities"] or 0), -(row["scrimmage_yards"] or 0), row["player_name"],
        ))

    def recent_team_games(self, season: int, team: str, *, before_game_id: str,
                          limit: int = 5) -> list[dict[str, Any]]:
        """Completed form entering a matchup, excluding that game and later dates."""
        self.initialize()
        game = self.get_game(before_game_id)
        if game is None:
            return []
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT * FROM games
                   WHERE season=? AND completed=1 AND game_id<>?
                     AND (away_team=? OR home_team=?)
                     AND (game_date<? OR (game_date=? AND week<?))
                   ORDER BY game_date DESC,week DESC LIMIT ?""",
                (season, before_game_id, team, team, game["game_date"],
                 game["game_date"], game["week"], max(1, min(int(limit), 10))),
            )]
        for row in rows:
            away = row["away_team"] == team
            own = row["away_score"] if away else row["home_score"]
            other = row["home_score"] if away else row["away_score"]
            row["opponent"] = row["home_team"] if away else row["away_team"]
            row["site"] = "at" if away else "vs"
            row["result"] = "W" if own > other else ("L" if own < other else "T")
            row["score"] = f"{own}-{other}"
        return rows

    def prior_matchups(self, team_a: str, team_b: str, *, before_game_id: str,
                       limit: int = 8) -> list[dict[str, Any]]:
        """Return completed meetings before a selected game, newest first."""
        self.initialize()
        game = self.get_game(before_game_id)
        if game is None:
            return []
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM games WHERE completed=1 AND game_id<>?
                   AND ((away_team=? AND home_team=?) OR (away_team=? AND home_team=?))
                   AND (game_date<? OR (game_date=? AND NOT (season=? AND week>=?)))
                   ORDER BY game_date DESC,week DESC LIMIT ?""",
                (before_game_id, team_a, team_b, team_b, team_a, game["game_date"],
                 game["game_date"], game["season"], game["week"],
                max(1, min(int(limit), 25))),
            )]

    def history_coverage(self) -> dict[str, int | None]:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT MIN(season),MAX(season),COUNT(*) FROM games WHERE completed=1"
            ).fetchone()
        return {"from": row[0], "through": row[1], "games": row[2]}

    def players_vs_opponent(self, season: int, team: str, opponent: str,
                            *, before_game_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Career production against an opponent for players on the current roster."""
        self.initialize()
        game = self.get_game(before_game_id)
        if game is None:
            return []
        current_ids = [row["player_id"] for row in self.team_roster(season, team)]
        if not current_ids:
            return []
        placeholders = ",".join("?" for _ in current_ids)
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT w.player_id,MAX(w.player_name) player_name,MAX(w.position) position,
                          COUNT(DISTINCT w.game_id) games,MAX(w.season) last_season,
                          SUM(CASE WHEN w.metric='passing_yards' THEN w.value ELSE 0 END) passing_yards,
                          SUM(CASE WHEN w.metric='passing_tds' THEN w.value ELSE 0 END) passing_tds,
                          SUM(CASE WHEN w.metric='rushing_yards' THEN w.value ELSE 0 END) rushing_yards,
                          SUM(CASE WHEN w.metric='rushing_tds' THEN w.value ELSE 0 END) rushing_tds,
                          SUM(CASE WHEN w.metric='receptions' THEN w.value ELSE 0 END) receptions,
                          SUM(CASE WHEN w.metric='receiving_yards' THEN w.value ELSE 0 END) receiving_yards,
                          SUM(CASE WHEN w.metric='receiving_tds' THEN w.value ELSE 0 END) receiving_tds,
                          SUM(CASE WHEN w.metric='def_sacks' THEN w.value ELSE 0 END) sacks,
                          SUM(CASE WHEN w.metric='def_interceptions' THEN w.value ELSE 0 END) interceptions
                   FROM player_weekly_stats w
                   JOIN games g ON g.game_id=w.game_id
                   WHERE w.opponent_team=? AND w.player_id IN ({placeholders}) AND g.completed=1
                     AND (g.game_date<? OR (g.game_date=? AND g.game_id<>?))
                   GROUP BY w.player_id ORDER BY games DESC,
                     (passing_yards+rushing_yards+receiving_yards) DESC LIMIT ?""",
                (opponent, *current_ids, game["game_date"], game["game_date"], before_game_id,
                 max(1, min(int(limit), 50))),
            )]
        for row in rows:
            games = row.get("games") or 0
            touchdowns = sum((row.get(key) or 0) for key in (
                "passing_tds", "rushing_tds", "receiving_tds",
            ))
            row["touchdowns"] = touchdowns
            for key in ("passing_yards", "rushing_yards", "receiving_yards",
                        "receptions", "sacks", "interceptions"):
                row[f"{key}_per_game"] = ((row.get(key) or 0) / games if games else None)
            row["touchdowns_per_game"] = touchdowns / games if games else None
        return rows

    def team_situational_profile(self, season: int, team: str, *,
                                 before_week: int | None = None) -> dict[str, Any]:
        self.initialize()
        week_filter = " AND week<?" if before_week is not None else ""
        parameters = (season, team, before_week) if before_week is not None else (season, team)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(DISTINCT game_id) games,SUM(plays) plays,SUM(drives) drives,
                          SUM(third_down_plays) third_down_plays,
                          SUM(third_down_conversions) third_down_conversions,
                          SUM(red_zone_plays) red_zone_plays,SUM(red_zone_successes) red_zone_successes,
                          SUM(neutral_plays) neutral_plays,SUM(neutral_passes) neutral_passes,
                          SUM(seconds_sum) seconds_sum,SUM(clocked_plays) clocked_plays
                   FROM game_team_situational WHERE season=? AND team=?""" + week_filter,
                parameters,
            ).fetchone()
        output = dict(row) if row else {}
        games = output.get("games") or 0
        output["plays_per_game"] = output.get("plays", 0) / games if games else None
        output["drives_per_game"] = output.get("drives", 0) / games if games else None
        output["third_down_rate"] = (output.get("third_down_conversions", 0) /
                                     output["third_down_plays"] if output.get("third_down_plays") else None)
        output["red_zone_success_rate"] = (output.get("red_zone_successes", 0) /
                                           output["red_zone_plays"] if output.get("red_zone_plays") else None)
        output["neutral_pass_rate"] = (output.get("neutral_passes", 0) /
                                       output["neutral_plays"] if output.get("neutral_plays") else None)
        output["seconds_per_play"] = (output.get("seconds_sum", 0) /
                                      output["clocked_plays"] if output.get("clocked_plays") else None)
        return output

    def league_situational_profile(self, season: int, *,
                                   before_week: int | None = None) -> list[dict[str, Any]]:
        """team_situational_profile for every team at once, for leaguewide ranking."""
        self.initialize()
        week_filter = " AND week<?" if before_week is not None else ""
        parameters: tuple[Any, ...] = (season, before_week) if before_week is not None else (season,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT team,COUNT(DISTINCT game_id) games,SUM(plays) plays,SUM(drives) drives,
                          SUM(third_down_plays) third_down_plays,
                          SUM(third_down_conversions) third_down_conversions,
                          SUM(red_zone_plays) red_zone_plays,SUM(red_zone_successes) red_zone_successes,
                          SUM(neutral_plays) neutral_plays,SUM(neutral_passes) neutral_passes,
                          SUM(seconds_sum) seconds_sum,SUM(clocked_plays) clocked_plays
                   FROM game_team_situational WHERE season=?""" + week_filter + " GROUP BY team",
                parameters,
            )
            output = []
            for row in rows:
                item = dict(row)
                games = item.get("games") or 0
                item["plays_per_game"] = item.get("plays", 0) / games if games else None
                item["drives_per_game"] = item.get("drives", 0) / games if games else None
                item["third_down_rate"] = (item.get("third_down_conversions", 0) /
                                           item["third_down_plays"] if item.get("third_down_plays") else None)
                item["red_zone_success_rate"] = (item.get("red_zone_successes", 0) /
                                                 item["red_zone_plays"] if item.get("red_zone_plays") else None)
                item["neutral_pass_rate"] = (item.get("neutral_passes", 0) /
                                             item["neutral_plays"] if item.get("neutral_plays") else None)
                item["seconds_per_play"] = (item.get("seconds_sum", 0) /
                                            item["clocked_plays"] if item.get("clocked_plays") else None)
                output.append(item)
        return output

    def team_playcalling_profile(self, season: int, team: str, *,
                                 before_week: int | None = None) -> dict[str, Any]:
        self.initialize()
        week_filter = " AND week<?" if before_week is not None else ""
        parameters = (season, team, before_week) if before_week is not None else (season, team)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(DISTINCT game_id) games,SUM(pass_plays) pass_plays,
                          SUM(rush_plays) rush_plays,SUM(early_down_plays) early_down_plays,
                          SUM(early_down_passes) early_down_passes,
                          SUM(shotgun_known) shotgun_known,SUM(shotgun_plays) shotgun_plays,
                          SUM(no_huddle_known) no_huddle_known,SUM(no_huddle_plays) no_huddle_plays
                   FROM game_team_playcalling WHERE season=? AND team=?""" + week_filter,
                parameters,
            ).fetchone()
        output = dict(row) if row else {}
        total = (output.get("pass_plays") or 0) + (output.get("rush_plays") or 0)
        output["plays"] = total
        output["pass_rate"] = (output.get("pass_plays") or 0) / total if total else None
        for name, numerator, denominator in (
            ("early_down_pass_rate", "early_down_passes", "early_down_plays"),
            ("shotgun_rate", "shotgun_plays", "shotgun_known"),
            ("no_huddle_rate", "no_huddle_plays", "no_huddle_known"),
        ):
            output[name] = ((output.get(numerator) or 0) / output[denominator]
                            if output.get(denominator) else None)
        return output

    def league_playcalling_profile(self, season: int, *,
                                   before_week: int | None = None) -> list[dict[str, Any]]:
        """team_playcalling_profile for every team at once, for leaguewide ranking."""
        self.initialize()
        week_filter = " AND week<?" if before_week is not None else ""
        parameters: tuple[Any, ...] = (season, before_week) if before_week is not None else (season,)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT team,COUNT(DISTINCT game_id) games,SUM(pass_plays) pass_plays,
                          SUM(rush_plays) rush_plays,SUM(early_down_plays) early_down_plays,
                          SUM(early_down_passes) early_down_passes,
                          SUM(shotgun_known) shotgun_known,SUM(shotgun_plays) shotgun_plays,
                          SUM(no_huddle_known) no_huddle_known,SUM(no_huddle_plays) no_huddle_plays
                   FROM game_team_playcalling WHERE season=?""" + week_filter + " GROUP BY team",
                parameters,
            )
            output = []
            for row in rows:
                item = dict(row)
                total = (item.get("pass_plays") or 0) + (item.get("rush_plays") or 0)
                item["plays"] = total
                item["pass_rate"] = (item.get("pass_plays") or 0) / total if total else None
                for name, numerator, denominator in (
                    ("early_down_pass_rate", "early_down_passes", "early_down_plays"),
                    ("shotgun_rate", "shotgun_plays", "shotgun_known"),
                    ("no_huddle_rate", "no_huddle_plays", "no_huddle_known"),
                ):
                    item[name] = ((item.get(numerator) or 0) / item[denominator]
                                  if item.get(denominator) else None)
                output.append(item)
        return output

    def coach_performance(self, coach: str | None) -> dict[str, Any] | None:
        """Straight-up and scoring history for games carrying a coach identity."""
        if not coach:
            return None
        self.initialize()
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT season,away_team,home_team,away_score,home_score,away_coach,home_coach
                   FROM games WHERE completed=1 AND (away_coach=? OR home_coach=?)
                   ORDER BY game_date,game_id""", (coach, coach)
            )]
        wins = losses = ties = points_for = points_against = 0
        seasons: set[int] = set()
        for row in rows:
            away = row["away_coach"] == coach
            own = row["away_score"] if away else row["home_score"]
            other = row["home_score"] if away else row["away_score"]
            if own is None or other is None:
                continue
            seasons.add(row["season"]); points_for += own; points_against += other
            if own > other: wins += 1
            elif own < other: losses += 1
            else: ties += 1
        games = wins + losses + ties
        return {
            "games": games, "wins": wins, "losses": losses, "ties": ties,
            "record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
            "win_rate": (wins + ties * .5) / games if games else None,
            "point_diff_per_game": (points_for - points_against) / games if games else None,
            "first_season": min(seasons) if seasons else None,
        }

    def coach_against_numbers(self, coach: str | None, *, before_game_id: str,
                              limit_season: int | None = None,
                              current_spread: float | None = None,
                              current_total: float | None = None) -> dict[str, Any] | None:
        """Coach ATS and total results from stored closing consensus lines."""
        if not coach:
            return None
        game = self.get_game(before_game_id)
        if game is None:
            return None
        where_season = " AND season=?" if limit_season is not None else ""
        parameters: list[Any] = [coach, coach, game["game_date"]]
        if limit_season is not None:
            parameters.append(limit_season)
        with closing(self._connect()) as connection:
            games = [dict(row) for row in connection.execute(
                """SELECT * FROM games WHERE completed=1 AND spread_line IS NOT NULL
                   AND (away_coach=? OR home_coach=?) AND game_date<?""" + where_season +
                " ORDER BY game_date", parameters,
            )]
        def grade(selected: list[dict[str, Any]]) -> dict[str, Any]:
            wins = losses = pushes = overs = unders = total_pushes = 0
            for item in selected:
                result = item["margin"] + item["spread"]
                if result > 0: wins += 1
                elif result < 0: losses += 1
                else: pushes += 1
                if item["total_line"] is not None:
                    total_result = item["points"] - item["total_line"]
                    if total_result > 0: overs += 1
                    elif total_result < 0: unders += 1
                    else: total_pushes += 1
            decided = wins + losses
            return {"games": len(selected),
                    "ats_record": f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
                    "cover_rate": wins / decided if decided else None,
                    "total_record": f"{overs}-{unders}" + (f"-{total_pushes}" if total_pushes else ""),
                    "over_rate": overs / (overs + unders) if overs + unders else None}

        played = []
        for row in games:
            away = row["away_coach"] == coach
            played.append({"margin": ((row["away_score"] - row["home_score"]) if away else
                                       (row["home_score"] - row["away_score"])),
                           "season": row["season"],
                           "spread": row["spread_line"] if away else -row["spread_line"],
                           "site": "away" if away else "home",
                           "points": row["away_score"] + row["home_score"],
                           "total_line": row.get("total_line")})
        overall = grade(played)
        # "Vs this number" is meant to read as current form, not a career
        # tendency re-tested against today's line -- scoped to this season
        # while every other split here stays career-wide.
        this_season = [item for item in played if item["season"] == game["season"]]
        versus = None
        if current_spread is not None:
            versus_rows = [{**item, "spread": current_spread} for item in this_season]
            versus = {**grade(versus_rows), "number": current_spread}
        versus_total = None
        if current_total is not None:
            versus_total_rows = [{**item, "total_line": current_total} for item in this_season]
            versus_total = {**grade(versus_total_rows), "number": current_total}
        return {"coach": coach, **overall,
                "season": {**grade(this_season), "year": game["season"]},
                "favorite": grade([item for item in played if item["spread"] < 0]),
                "underdog": grade([item for item in played if item["spread"] > 0]),
                "home": grade([item for item in played if item["site"] == "home"]),
                "away": grade([item for item in played if item["site"] == "away"]),
                "versus": versus, "versus_total": versus_total}

    def team_season_summary(self, season: int, team: str, *,
                            before_week: int | None = None) -> dict[str, Any]:
        """Compact production context for matchup pages, normalized per game."""
        self.initialize()
        wanted = (
            "passing_yards", "rushing_yards", "passing_tds", "rushing_tds",
            "passing_interceptions", "fumbles_lost_total", "sacks_suffered",
        )
        placeholders = ",".join("?" for _ in wanted)
        week_filter = " AND week<?" if before_week is not None else ""
        parameters: tuple[Any, ...] = ((season, team, *wanted, before_week)
                                       if before_week is not None else (season, team, *wanted))
        with closing(self._connect()) as connection:
            totals = {row["metric"]: row["value"] for row in connection.execute(
                f"""SELECT metric,SUM(value) value FROM team_weekly_stats
                    WHERE season=? AND team=? AND metric IN ({placeholders})
                    {week_filter} GROUP BY metric""",
                parameters,
            )}
            game_week_filter = " AND week<?" if before_week is not None else ""
            game_parameters = ((season, team, team, before_week) if before_week is not None
                               else (season, team, team))
            scores = list(connection.execute(
                """SELECT away_team,home_team,away_score,home_score FROM games
                   WHERE season=? AND completed=1 AND (away_team=? OR home_team=?)"""
                + game_week_filter,
                game_parameters,
            ))
        games = len(scores)
        points_for = sum((row["away_score"] if row["away_team"] == team else row["home_score"]) or 0
                         for row in scores)
        points_against = sum((row["home_score"] if row["away_team"] == team else row["away_score"]) or 0
                             for row in scores)
        per_game = lambda key: totals.get(key) / games if games and totals.get(key) is not None else None
        return {
            "games": games,
            "points_per_game": points_for / games if games else None,
            "points_allowed_per_game": points_against / games if games else None,
            "passing_yards_per_game": per_game("passing_yards"),
            "rushing_yards_per_game": per_game("rushing_yards"),
            "sacks_allowed_per_game": per_game("sacks_suffered"),
            "turnovers_per_game": (
                (totals.get("passing_interceptions", 0) + totals.get("fumbles_lost_total", 0)) / games
                if games else None
            ),
        }

    def league_team_summary(self, season: int, *,
                            before_week: int | None = None) -> list[dict[str, Any]]:
        """team_season_summary for every team at once, for leaguewide ranking."""
        self.initialize()
        wanted = (
            "passing_yards", "rushing_yards", "passing_tds", "rushing_tds",
            "passing_interceptions", "fumbles_lost_total", "sacks_suffered",
        )
        placeholders = ",".join("?" for _ in wanted)
        week_filter = " AND week<?" if before_week is not None else ""
        parameters: tuple[Any, ...] = ((season, *wanted, before_week)
                                       if before_week is not None else (season, *wanted))
        with closing(self._connect()) as connection:
            totals: dict[str, dict[str, float]] = {}
            for row in connection.execute(
                f"""SELECT team,metric,SUM(value) value FROM team_weekly_stats
                    WHERE season=? AND metric IN ({placeholders}) {week_filter}
                    GROUP BY team,metric""",
                parameters,
            ):
                totals.setdefault(row["team"], {})[row["metric"]] = row["value"]
            game_week_filter = " AND week<?" if before_week is not None else ""
            game_parameters: tuple[Any, ...] = (season, before_week) if before_week is not None else (season,)
            scores = list(connection.execute(
                "SELECT away_team,home_team,away_score,home_score FROM games "
                "WHERE season=? AND completed=1" + game_week_filter,
                game_parameters,
            ))
        games: dict[str, int] = {}
        points_for: dict[str, int] = {}
        points_against: dict[str, int] = {}
        for row in scores:
            for team, own_key, other_key in ((row["away_team"], "away_score", "home_score"),
                                             (row["home_team"], "home_score", "away_score")):
                games[team] = games.get(team, 0) + 1
                points_for[team] = points_for.get(team, 0) + (row[own_key] or 0)
                points_against[team] = points_against.get(team, 0) + (row[other_key] or 0)
        output = []
        for team, count in games.items():
            team_totals = totals.get(team, {})
            per_game = lambda key: team_totals.get(key, 0) / count if count else None
            output.append({
                "team": team, "games": count,
                "points_per_game": points_for.get(team, 0) / count if count else None,
                "points_allowed_per_game": points_against.get(team, 0) / count if count else None,
                "passing_yards_per_game": per_game("passing_yards"),
                "rushing_yards_per_game": per_game("rushing_yards"),
                "sacks_allowed_per_game": per_game("sacks_suffered"),
                "turnovers_per_game": (
                    (team_totals.get("passing_interceptions", 0) + team_totals.get("fumbles_lost_total", 0))
                    / count if count else None
                ),
            })
        return output

    def team_roster(self, season: int, team: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM players WHERE season=? AND team=?
                   ORDER BY position,depth_position,full_name""", (season, team)
            )]

    def team_staff(self, season: int, team: str) -> list[dict[str, Any]]:
        self.initialize()
        order = "CASE role WHEN 'Head coach' THEN 1 WHEN 'Offensive coordinator' THEN 2 WHEN 'Defensive coordinator' THEN 3 ELSE 4 END"
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM team_staff WHERE season=? AND team=? ORDER BY {order}",
                (season, canon_team(team)),
            )]

    def team_scheme_rate(self, season: int, team: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM team_scheme_rates WHERE season=? AND team=?",
                (season, canon_team(team)),
            ).fetchone()
        return dict(row) if row else None

    def team_pressure_rate(self, season: int, team: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM team_pressure_rates WHERE season=? AND team=?",
                (season, canon_team(team)),
            ).fetchone()
        return dict(row) if row else None

    def team_injuries(self, season: int, team: str) -> list[dict[str, Any]]:
        """Latest non-active ESPN designation per athlete for a team."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT * FROM injury_reports WHERE season=? AND team=?
                   ORDER BY COALESCE(report_date,fetched_at) DESC,player_name""",
                (season, canon_team(team)),
            )]
        output = []
        seen = set()
        for row in rows:
            if row["espn_id"] in seen:
                continue
            seen.add(row["espn_id"]); output.append(row)
        return output

    def roster_movements(self, season: int, team: str) -> dict[str, list[dict[str, Any]]]:
        """Compare adjacent roster releases by GSIS ID and retain destination context."""
        self.initialize()
        prior_season = season - 1
        with closing(self._connect()) as connection:
            current = {row["player_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM players WHERE season=? AND team=?", (season, team)
            )}
            prior = {row["player_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM players WHERE season=? AND team=?", (prior_season, team)
            )}
            previous_teams: dict[str, list[str]] = defaultdict(list)
            for row in connection.execute(
                "SELECT player_id,team FROM players WHERE season=?", (prior_season,)
            ):
                previous_teams[row["player_id"]].append(row["team"])
            current_teams: dict[str, list[str]] = defaultdict(list)
            for row in connection.execute(
                "SELECT player_id,team FROM players WHERE season=?", (season,)
            ):
                current_teams[row["player_id"]].append(row["team"])
            draft_years = {row["gsis_id"]: row["draft_year"] for row in connection.execute(
                "SELECT gsis_id,draft_year FROM player_master WHERE draft_year IS NOT NULL"
            )}
        arrivals = []
        for player_id in current.keys() - prior.keys():
            row = current[player_id]
            origins = [code for code in previous_teams.get(player_id, []) if code != team]
            if origins:
                kind, detail = "NFL arrival", "From " + ", ".join(sorted(origins))
            elif draft_years.get(player_id) == season:
                kind, detail = "Rookie", f"{season} draft class"
            else:
                kind, detail = "Roster addition", "Not on an NFL roster in the prior release"
            arrivals.append({**row, "movement": kind, "detail": detail,
                             "from_teams": origins, "to_teams": [team]})
        departures = []
        for player_id in prior.keys() - current.keys():
            row = prior[player_id]
            destinations = [code for code in current_teams.get(player_id, []) if code != team]
            detail = ("Joined " + ", ".join(sorted(destinations)) if destinations
                      else f"Not on a {season} NFL roster")
            departures.append({**row, "movement": "Departure", "detail": detail,
                               "from_teams": [team], "to_teams": destinations})
        ordering = lambda row: ((row.get("position") or "ZZ"), row.get("full_name") or "")
        return {"prior_season": prior_season, "season": season,
                "arrivals": sorted(arrivals, key=ordering),
                "departures": sorted(departures, key=ordering)}

    def current_depth_chart(self, season: int, team: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM depth_chart_snapshots
                   WHERE season=? AND team=? AND snapshot_at=(
                     SELECT MAX(snapshot_at) FROM depth_chart_snapshots WHERE season=? AND team=?
                   ) ORDER BY position_group,position_slot,position_rank,player_name""",
                (season, team, season, team),
            )]

    def team_snap_leaders(self, season: int, team: str) -> list[dict[str, Any]]:
        return self.snap_leaders_for_teams(season, (team,))

    def snap_leaders_for_teams(self, season: int, teams: Iterable[str]) -> list[dict[str, Any]]:
        """Return snap leaders for several clubs with a single indexed query."""
        wanted = tuple(dict.fromkeys(canon_team(team) for team in teams if team))
        if not wanted:
            return []
        self.initialize()
        placeholders = ",".join("?" for _ in wanted)
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                f"""SELECT s.team,s.player_key,s.pfr_player_id,s.player_name,s.position,
                          MAX(p.player_id) player_id,COUNT(DISTINCT s.game_id) games,
                          SUM(s.offense_snaps) offense_snaps,AVG(s.offense_pct) offense_pct,
                          SUM(s.defense_snaps) defense_snaps,AVG(s.defense_pct) defense_pct,
                          SUM(s.st_snaps) st_snaps,AVG(s.st_pct) st_pct
                   FROM snap_counts s LEFT JOIN players p
                     ON p.season=s.season AND p.team=s.team AND p.pfr_id=s.pfr_player_id
                   WHERE s.season=? AND s.team IN ({placeholders})
                   GROUP BY s.team,s.player_key,s.pfr_player_id,s.player_name,s.position
                   ORDER BY s.team,MAX(COALESCE(offense_pct,0),COALESCE(defense_pct,0)) DESC,
                            player_name""",
                (season, *wanted),
            )]

    def get_player(self, season: int, player_id: str) -> dict[str, Any] | None:
        """Return a player's roster identity, retaining all in-season team stops."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT * FROM players WHERE season=? AND player_id=?
                   ORDER BY team""", (season, player_id)
            )]
            master_row = connection.execute(
                "SELECT * FROM player_master WHERE gsis_id=?", (player_id,)
            ).fetchone()
            external_ids = connection.execute(
                """SELECT provider,external_id,source FROM player_external_ids
                   WHERE gsis_id=? ORDER BY source='dynastyprocess',provider""", (player_id,)
            )
            identifiers = {row["provider"]: row["external_id"] for row in external_ids}
        master = dict(master_row) if master_row else {}
        if not rows and not master:
            return None
        player = rows[0] if rows else {
            "season": season, "player_id": player_id, "team": master.get("latest_team"),
            "full_name": master.get("display_name"), "first_name": master.get("first_name"),
            "last_name": master.get("last_name"), "position": master.get("position"),
            "jersey_number": None, "status": master.get("status"),
        }
        for target, source in (
            ("birth_date", "birth_date"), ("height", "height"), ("weight", "weight"),
            ("college", "college"), ("years_experience", "years_experience"),
            ("headshot_url", "headshot_url"),
        ):
            if player.get(target) is None and master.get(source) is not None:
                player[target] = master[source]
        player["teams"] = [row["team"] for row in rows] or ([master["latest_team"]] if master.get("latest_team") else [])
        player["draft"] = {key: master.get(key) for key in ("draft_year", "draft_round", "draft_pick", "draft_team")}
        player["external_ids"] = identifiers
        return player

    def identity_coverage(self) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                """SELECT provider,COUNT(DISTINCT gsis_id) players
                   FROM player_external_ids GROUP BY provider ORDER BY players DESC,provider"""
            )]

    def game_efficiency(self, game_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM game_team_efficiency WHERE game_id=? ORDER BY team", (game_id,)
            )]
        return sorted((self._efficiency_rates(row) for row in rows),
                      key=lambda row: -(row["epa_per_play"] or 0))

    def game_situational(self, game_id: str) -> list[dict[str, Any]]:
        """Return one derived situational row per team for a completed game."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM game_team_situational WHERE game_id=? ORDER BY team",
                (game_id,),
            )]
        for row in rows:
            row["third_down_rate"] = (row["third_down_conversions"] / row["third_down_plays"]
                                      if row["third_down_plays"] else None)
            row["red_zone_success_rate"] = (row["red_zone_successes"] / row["red_zone_plays"]
                                            if row["red_zone_plays"] else None)
            row["neutral_pass_rate"] = (row["neutral_passes"] / row["neutral_plays"]
                                        if row["neutral_plays"] else None)
            row["seconds_per_play"] = (row["seconds_sum"] / row["clocked_plays"]
                                       if row["clocked_plays"] else None)
        return rows

    def game_team_stats(self, game_id: str) -> dict[str, dict[str, Any]]:
        """Wide team box-score rows matched to a canonical scheduled game."""
        game = self.get_game(game_id)
        if game is None:
            return {}
        teams = (game["away_team"], game["home_team"])
        output = {team: {} for team in teams}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT team,metric,SUM(value) value FROM team_weekly_stats
                   WHERE season=? AND week=? AND season_type=? AND team IN (?,?)
                   GROUP BY team,metric""",
                (game["season"], game["week"], game["season_type"], *teams),
            )
            for row in rows:
                output.setdefault(row["team"], {})[row["metric"]] = row["value"]
        return output

    @staticmethod
    def _efficiency_rates(row: dict[str, Any]) -> dict[str, Any]:
        row["epa_per_play"] = row["total_epa"] / row["plays"] if row["plays"] else None
        row["success_rate"] = row["successful_plays"] / row["plays"] if row["plays"] else None
        row["pass_epa_per_play"] = row["pass_epa"] / row["pass_plays"] if row["pass_plays"] else None
        row["rush_epa_per_play"] = row["rush_epa"] / row["rush_plays"] if row["rush_plays"] else None
        row["early_down_epa"] = row["early_down_epa"] / row["early_down_plays"] if row["early_down_plays"] else None
        row["explosive_rate"] = row["explosive_plays"] / row["plays"] if row["plays"] else None
        return row

    def league_efficiency(self, season: int, *, before_week: int | None = None) -> list[dict[str, Any]]:
        self.initialize()
        week_filter = " AND week<?" if before_week is not None else ""
        parameters = (season, before_week) if before_week is not None else (season,)
        with closing(self._connect()) as connection:
            offense = [dict(row) for row in connection.execute(
                """SELECT team,SUM(plays) plays,SUM(total_epa) total_epa,
                          SUM(successful_plays) successful_plays,SUM(pass_plays) pass_plays,
                          SUM(pass_epa) pass_epa,SUM(rush_plays) rush_plays,SUM(rush_epa) rush_epa,
                          SUM(early_down_plays) early_down_plays,SUM(early_down_epa) early_down_epa,
                          SUM(explosive_plays) explosive_plays
                   FROM game_team_efficiency WHERE season=?""" + week_filter + " GROUP BY team",
                parameters,
            )]
            defense = {row["team"]: dict(row) for row in connection.execute(
                """SELECT opponent_team team,
                          SUM(total_epa)/SUM(plays) epa_allowed,
                          SUM(successful_plays)/SUM(plays) success_allowed,
                          SUM(pass_epa)/SUM(pass_plays) pass_epa_allowed,
                          SUM(rush_epa)/SUM(rush_plays) rush_epa_allowed,
                          SUM(explosive_plays)*1.0/SUM(plays) explosive_allowed
                   FROM game_team_efficiency WHERE season=?""" + week_filter + " GROUP BY opponent_team",
                parameters,
            )}
        profiles = []
        for row in offense:
            profile = self._efficiency_rates(row)
            resistance = defense.get(row["team"], {})
            profile["defensive_epa_allowed"] = resistance.get("epa_allowed")
            profile["defensive_success_allowed"] = resistance.get("success_allowed")
            profile["defensive_pass_epa_allowed"] = resistance.get("pass_epa_allowed")
            profile["defensive_rush_epa_allowed"] = resistance.get("rush_epa_allowed")
            profile["defensive_explosive_allowed"] = resistance.get("explosive_allowed")
            profile["net_epa"] = ((profile["epa_per_play"] or 0) - (profile["defensive_epa_allowed"] or 0))
            profiles.append(profile)
        return sorted(profiles, key=lambda row: (-row["net_epa"], row["team"]))

    def team_efficiency(self, season: int, team: str) -> dict[str, Any] | None:
        return next((row for row in self.league_efficiency(season) if row["team"] == team), None)

    def elo_ratings(self) -> list[dict[str, Any]]:
        self.initialize()
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM nfl_elo_ratings ORDER BY rating DESC,team"
            )]

    def game_elo(self, game_id: str) -> dict[str, Any] | None:
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM nfl_elo_games WHERE game_id=?", (game_id,)
            ).fetchone()
        return dict(row) if row else None

    def team_weekly_performance(self, season: int, team: str) -> list[dict[str, Any]]:
        """Completed-game scoring and efficiency series for charting."""
        games = [game for game in self.schedule(season, team=team) if game["completed"]]
        with closing(self._connect()) as connection:
            efficiency = {row["game_id"]: self._efficiency_rates(dict(row))
                          for row in connection.execute(
                "SELECT * FROM game_team_efficiency WHERE season=? AND team=?",
                (season, team),
            )}
            # The opponent's own offensive row in the same game is this team's
            # defense-allowed line -- same source table game_elo/league_efficiency
            # already use for the season-long "defensive_epa_allowed" family.
            allowed = {row["game_id"]: self._efficiency_rates(dict(row))
                      for row in connection.execute(
                "SELECT * FROM game_team_efficiency WHERE season=? AND opponent_team=?",
                (season, team),
            )}
        output = []
        for game in games:
            away = game["away_team"] == team
            points = game["away_score"] if away else game["home_score"]
            points_against = game["home_score"] if away else game["away_score"]
            faced = allowed.get(game["game_id"], {})
            output.append({
                "season": season, "week": game["week"], "game_id": game["game_id"],
                "opponent": game["home_team"] if away else game["away_team"],
                "points": points, "points_allowed": points_against,
                "point_margin": points - points_against,
                **{key: efficiency.get(game["game_id"], {}).get(key) for key in (
                    "epa_per_play", "success_rate", "pass_epa_per_play", "rush_epa_per_play",
                    "explosive_rate",
                )},
                "defensive_epa_allowed": faced.get("epa_per_play"),
                "defensive_success_allowed": faced.get("success_rate"),
                "defensive_pass_epa_allowed": faced.get("pass_epa_per_play"),
                "defensive_rush_epa_allowed": faced.get("rush_epa_per_play"),
                "defensive_explosive_allowed": faced.get("explosive_rate"),
            })
        return output

    @staticmethod
    def _pass_profile_rates(row: dict[str, Any]) -> dict[str, Any]:
        attempts = row.get("attempts") or 0
        row["completion_rate"] = row.get("completions", 0) / attempts if attempts else None
        row["yards_per_attempt"] = row.get("passing_yards", 0) / attempts if attempts else None
        row["average_depth"] = row.get("air_yards", 0) / attempts if attempts else None
        row["epa_per_attempt"] = row.get("total_epa", 0) / attempts if attempts else None
        cpoe_plays = row.get("cpoe_plays") or 0
        row["cpoe"] = row.get("cpoe_total", 0) / cpoe_plays if cpoe_plays else None
        return row

    def _pass_profile(self, season: int, field: str, value: str) -> dict[str, Any]:
        if field not in {"passer_player_id", "defense_team"}:
            raise ValueError("unsupported pass-profile field")
        self.initialize()
        aggregate = """SUM(attempts) attempts,SUM(completions) completions,
                         SUM(passing_yards) passing_yards,SUM(air_yards) air_yards,
                         SUM(total_epa) total_epa,SUM(touchdowns) touchdowns,
                         SUM(interceptions) interceptions,SUM(cpoe_total) cpoe_total,
                         SUM(cpoe_plays) cpoe_plays"""
        with closing(self._connect()) as connection:
            zones = [self._pass_profile_rates(dict(row)) for row in connection.execute(
                f"""SELECT depth_bucket,pass_location,{aggregate}
                    FROM qb_pass_profiles WHERE season=? AND {field}=?
                    GROUP BY depth_bucket,pass_location
                    ORDER BY CASE depth_bucket WHEN 'behind' THEN 1 WHEN 'short' THEN 2
                             WHEN 'intermediate' THEN 3 ELSE 4 END,pass_location""",
                (season, value),
            )]
            weekly = [self._pass_profile_rates(dict(row)) for row in connection.execute(
                f"""SELECT week,game_id,MAX(offense_team) offense_team,
                            MAX(defense_team) defense_team,{aggregate}
                    FROM qb_pass_profiles WHERE season=? AND {field}=?
                    GROUP BY week,game_id ORDER BY week,game_id""",
                (season, value),
            )]
        total = self._pass_profile_rates({key: sum(row.get(key, 0) or 0 for row in zones)
                                         for key in ("attempts", "completions", "passing_yards",
                                                     "air_yards", "total_epa", "touchdowns",
                                                     "interceptions", "cpoe_total", "cpoe_plays")})
        return {"season": season, "zones": zones, "weekly": weekly, "total": total}

    def qb_pass_profile(self, season: int, player_id: str) -> dict[str, Any]:
        return self._pass_profile(season, "passer_player_id", player_id)

    def game_qb_pass_profiles(self, game_id: str) -> list[dict[str, Any]]:
        """Charted passing profiles limited to one game, ordered by attempts."""
        self.initialize()
        aggregate = """SUM(attempts) attempts,SUM(completions) completions,
                         SUM(passing_yards) passing_yards,SUM(air_yards) air_yards,
                         SUM(total_epa) total_epa,SUM(touchdowns) touchdowns,
                         SUM(interceptions) interceptions,SUM(cpoe_total) cpoe_total,
                         SUM(cpoe_plays) cpoe_plays"""
        with closing(self._connect()) as connection:
            passers = [dict(row) for row in connection.execute(
                """SELECT passer_player_id,MAX(passer_name) passer_name,
                          MAX(offense_team) offense_team,MAX(defense_team) defense_team,
                          SUM(attempts) attempts,MAX(season) season
                   FROM qb_pass_profiles WHERE game_id=? GROUP BY passer_player_id
                   ORDER BY attempts DESC""", (game_id,),
            )]
            for passer in passers:
                zones = [self._pass_profile_rates(dict(row)) for row in connection.execute(
                    f"""SELECT depth_bucket,pass_location,{aggregate}
                        FROM qb_pass_profiles WHERE game_id=? AND passer_player_id=?
                        GROUP BY depth_bucket,pass_location""",
                    (game_id, passer["passer_player_id"]),
                )]
                total = self._pass_profile_rates({
                    key: sum(row.get(key, 0) or 0 for row in zones)
                    for key in ("attempts", "completions", "passing_yards", "air_yards",
                                "total_epa", "touchdowns", "interceptions", "cpoe_total",
                                "cpoe_plays")
                })
                passer.update({"zones": zones, "total": total})
        return passers

    def defense_pass_profile(self, season: int, team: str) -> dict[str, Any]:
        return self._pass_profile(season, "defense_team", canon_team(team))

    @staticmethod
    def _receiver_profile_rates(row: dict[str, Any]) -> dict[str, Any]:
        targets = row.get("targets") or 0
        receptions = row.get("receptions") or 0
        row["reception_rate"] = receptions / targets if targets else None
        row["yards_per_target"] = row.get("receiving_yards", 0) / targets if targets else None
        row["yards_per_reception"] = row.get("receiving_yards", 0) / receptions if receptions else None
        row["average_target_depth"] = row.get("air_yards", 0) / targets if targets else None
        row["yac_per_reception"] = row.get("yards_after_catch", 0) / receptions if receptions else None
        row["epa_per_target"] = row.get("total_epa", 0) / targets if targets else None
        return row

    def receiver_pass_profile(self, season: int, player_id: str) -> dict[str, Any]:
        self.initialize()
        aggregate = """SUM(targets) targets,SUM(receptions) receptions,
                         SUM(receiving_yards) receiving_yards,SUM(air_yards) air_yards,
                         SUM(yards_after_catch) yards_after_catch,SUM(total_epa) total_epa,
                         SUM(touchdowns) touchdowns"""
        with closing(self._connect()) as connection:
            zones = [self._receiver_profile_rates(dict(row)) for row in connection.execute(
                f"""SELECT depth_bucket,pass_location,{aggregate}
                    FROM receiver_pass_profiles WHERE season=? AND receiver_player_id=?
                    GROUP BY depth_bucket,pass_location
                    ORDER BY CASE depth_bucket WHEN 'behind' THEN 1 WHEN 'short' THEN 2
                             WHEN 'intermediate' THEN 3 ELSE 4 END,pass_location""",
                (season, player_id),
            )]
        total = self._receiver_profile_rates({
            key: sum(row.get(key, 0) or 0 for row in zones)
            for key in ("targets", "receptions", "receiving_yards", "air_yards",
                        "yards_after_catch", "total_epa", "touchdowns")
        })
        return {"season": season, "zones": zones, "total": total}

    #: Field-left-to-right order for the standard 7-cell run-direction chart.
    RUN_DIRECTIONS = ("left end", "left tackle", "left guard", "middle",
                      "right guard", "right tackle", "right end")

    @staticmethod
    def _rush_profile_rates(row: dict[str, Any]) -> dict[str, Any]:
        attempts = row.get("attempts") or 0
        row["yards_per_attempt"] = row.get("rushing_yards", 0) / attempts if attempts else None
        row["epa_per_attempt"] = row.get("total_epa", 0) / attempts if attempts else None
        return row

    def _rush_direction_profile(self, season: int, field: str, value: str) -> dict[str, Any]:
        if field not in {"rusher_player_id", "defense_team", "offense_team"}:
            raise ValueError("unsupported rush-direction-profile field")
        self.initialize()
        aggregate = """SUM(attempts) attempts,SUM(rushing_yards) rushing_yards,
                         SUM(total_epa) total_epa,SUM(touchdowns) touchdowns"""
        order = "CASE direction " + " ".join(
            f"WHEN '{name}' THEN {index}" for index, name in enumerate(self.RUN_DIRECTIONS)
        ) + " END"
        with closing(self._connect()) as connection:
            directions = [self._rush_profile_rates(dict(row)) for row in connection.execute(
                f"""SELECT direction,{aggregate} FROM rush_direction_profiles
                    WHERE season=? AND {field}=? GROUP BY direction ORDER BY {order}""",
                (season, value),
            )]
        total = self._rush_profile_rates({key: sum(row.get(key, 0) or 0 for row in directions)
                                          for key in ("attempts", "rushing_yards", "total_epa", "touchdowns")})
        return {"season": season, "directions": directions, "total": total}

    def rusher_direction_profile(self, season: int, player_id: str) -> dict[str, Any]:
        return self._rush_direction_profile(season, "rusher_player_id", player_id)

    def team_rush_direction_profile(self, season: int, team: str) -> dict[str, Any]:
        """Every rusher on the team combined -- the full run game, not just its lead back."""
        return self._rush_direction_profile(season, "offense_team", canon_team(team))

    def defense_rush_direction_profile(self, season: int, team: str) -> dict[str, Any]:
        return self._rush_direction_profile(season, "defense_team", canon_team(team))

    def rush_direction_contributors(self, season: int, *, offense_team: str | None = None,
                                    defense_team: str | None = None,
                                    game_id: str | None = None) -> list[dict[str, Any]]:
        """Every rusher's outcomes inside each run-direction cell, for hover detail."""
        if bool(offense_team) == bool(defense_team):
            raise ValueError("pass exactly one contributor scope")
        field = "offense_team" if offense_team else "defense_team"
        value = canon_team(offense_team) if offense_team else canon_team(defense_team)
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, value, game_id) if game_id else (season, value)
        with closing(self._connect()) as connection:
            rows = [self._rush_profile_rates(dict(row)) for row in connection.execute(
                f"""SELECT direction,rusher_player_id,MAX(rusher_name) rusher_name,
                           SUM(attempts) attempts,SUM(rushing_yards) rushing_yards,
                           SUM(total_epa) total_epa,SUM(touchdowns) touchdowns,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=rusher_player_id LIMIT 1) position
                    FROM rush_direction_profiles WHERE season=? AND {field}=?{game_filter}
                    GROUP BY direction,rusher_player_id
                    ORDER BY direction,attempts DESC,rushing_yards DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['rusher_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = (f"{row['attempts']:g} att · {row['rushing_yards']:g} yd · "
                             f"{row['touchdowns']:g} TD · {row['epa_per_attempt']:+.2f} EPA/att")
        return rows

    def rush_direction_defenders(self, season: int, *, defense_team: str,
                                 game_id: str | None = None) -> list[dict[str, Any]]:
        """Tacklers credited inside each run-direction cell -- the honest, already
        play-by-play-attributed defensive side (who stopped it, not who was assigned
        to that gap)."""
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, canon_team(defense_team), game_id) if game_id else (season, canon_team(defense_team))
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT direction,defender_player_id,MAX(defender_name) defender_name,
                           SUM(tackles) tackles,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=defender_player_id LIMIT 1) position
                    FROM rush_direction_defenders WHERE season=? AND defense_team=?{game_filter}
                    GROUP BY direction,defender_player_id
                    ORDER BY direction,tackles DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['defender_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['tackles']:g} tkl"
        return rows

    def _rush_situational_profile(self, season: int, field: str, value: str) -> dict[str, Any]:
        if field not in {"offense_team", "defense_team"}:
            raise ValueError("unsupported rush-situational field")
        self.initialize()
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"""SELECT SUM(attempts) attempts,SUM(rushing_yards) rushing_yards,
                           SUM(total_epa) total_epa,SUM(touchdowns) touchdowns
                    FROM rush_situational_profiles WHERE season=? AND {field}=?""",
                (season, value),
            ).fetchone()
        return self._rush_profile_rates(dict(row))

    def team_rush_situational_profile(self, season: int, team: str) -> dict[str, Any]:
        """Red-zone carries for the whole run game, not just one back."""
        return self._rush_situational_profile(season, "offense_team", canon_team(team))

    def defense_rush_situational_profile(self, season: int, team: str) -> dict[str, Any]:
        """Red-zone carries allowed by one defense."""
        return self._rush_situational_profile(season, "defense_team", canon_team(team))

    def rush_situational_contributors(self, season: int, *, offense_team: str | None = None,
                                      defense_team: str | None = None,
                                      game_id: str | None = None) -> list[dict[str, Any]]:
        """Every rusher's red-zone carries, for hover detail.

        `offense_team` scopes to that team's own backs (this game's run
        game); `defense_team` scopes to every opponent who has carried the
        ball against that defense in the red zone all season -- "what
        positions beat this defense here", not just this one game's backs.
        """
        if bool(offense_team) == bool(defense_team):
            raise ValueError("pass exactly one contributor scope")
        field = "offense_team" if offense_team else "defense_team"
        value = canon_team(offense_team) if offense_team else canon_team(defense_team)
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, value, game_id) if game_id else (season, value)
        with closing(self._connect()) as connection:
            rows = [self._rush_profile_rates(dict(row)) for row in connection.execute(
                f"""SELECT rusher_player_id,MAX(rusher_name) rusher_name,
                           SUM(attempts) attempts,SUM(rushing_yards) rushing_yards,
                           SUM(total_epa) total_epa,SUM(touchdowns) touchdowns,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=rusher_player_id LIMIT 1) position
                    FROM rush_situational_profiles WHERE season=? AND {field}=?{game_filter}
                    GROUP BY rusher_player_id ORDER BY attempts DESC,rushing_yards DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['rusher_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['attempts']:g} att · {row['rushing_yards']:g} yd · {row['touchdowns']:g} TD"
        return rows

    def rush_situational_defenders(self, season: int, *, defense_team: str,
                                   game_id: str | None = None) -> list[dict[str, Any]]:
        """Tacklers credited on red-zone runs -- the honest defensive side."""
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, canon_team(defense_team), game_id) if game_id else (season, canon_team(defense_team))
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT defender_player_id,MAX(defender_name) defender_name,SUM(tackles) tackles,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=defender_player_id LIMIT 1) position
                    FROM rush_situational_defenders WHERE season=? AND defense_team=?{game_filter}
                    GROUP BY defender_player_id ORDER BY tackles DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['defender_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['tackles']:g} tkl"
        return rows

    @staticmethod
    def _situational_rates(row: dict[str, Any]) -> dict[str, Any]:
        attempts = row.get("attempts") or 0
        row["completion_rate"] = row.get("completions", 0) / attempts if attempts else None
        row["yards_per_attempt"] = row.get("passing_yards", 0) / attempts if attempts else None
        row["epa_per_attempt"] = row.get("total_epa", 0) / attempts if attempts else None
        return row

    def _situational_pass_profile(self, season: int, field: str, value: str) -> dict[str, dict[str, Any]]:
        if field not in {"passer_player_id", "defense_team"}:
            raise ValueError("unsupported situational-profile field")
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT situation,SUM(attempts) attempts,SUM(completions) completions,
                          SUM(passing_yards) passing_yards,SUM(total_epa) total_epa,
                          SUM(touchdowns) touchdowns,SUM(interceptions) interceptions
                   FROM qb_situational_profiles WHERE season=? AND {field}=?
                   GROUP BY situation""",
                (season, value),
            )
            return {row["situation"]: self._situational_rates(dict(row)) for row in rows}

    def qb_situational_profile(self, season: int, player_id: str) -> dict[str, dict[str, Any]]:
        """Red-zone and end-zone splits for one passer, keyed by situation."""
        return self._situational_pass_profile(season, "passer_player_id", player_id)

    def defense_situational_profile(self, season: int, team: str) -> dict[str, dict[str, Any]]:
        """Red-zone and end-zone splits allowed by one defense, keyed by situation."""
        return self._situational_pass_profile(season, "defense_team", canon_team(team))

    def situational_pass_contributors(self, season: int, *, passer_player_id: str | None = None,
                                      defense_team: str | None = None,
                                      game_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Receiver outcomes inside each red-zone/end-zone split, keyed by situation."""
        if bool(passer_player_id) == bool(defense_team):
            raise ValueError("pass exactly one contributor scope")
        field = "passer_player_id" if passer_player_id else "defense_team"
        value = passer_player_id or canon_team(defense_team)
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, value, game_id) if game_id else (season, value)
        grouped: dict[str, list[dict[str, Any]]] = {}
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT situation,receiver_player_id,MAX(receiver_name) receiver_name,
                           SUM(targets) targets,SUM(receptions) receptions,
                           SUM(receiving_yards) receiving_yards,SUM(touchdowns) touchdowns,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=receiver_player_id LIMIT 1) position
                    FROM situational_pass_receivers WHERE season=? AND {field}=?{game_filter}
                    GROUP BY situation,receiver_player_id
                    ORDER BY situation,targets DESC,receiving_yards DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['receiver_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['receptions']:g}/{row['targets']:g} · {row['receiving_yards']:g} yd · {row['touchdowns']:g} TD"
            grouped.setdefault(row["situation"], []).append(row)
        return grouped

    def situational_pass_defenders(self, season: int, *, defense_team: str,
                                   game_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Pass-defended and interception credits inside each red-zone/end-zone split."""
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, canon_team(defense_team), game_id) if game_id else (season, canon_team(defense_team))
        grouped: dict[str, list[dict[str, Any]]] = {}
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT situation,defender_player_id,MAX(defender_name) defender_name,
                           event,SUM(count) count,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=defender_player_id LIMIT 1) position
                    FROM situational_pass_defenders WHERE season=? AND defense_team=?{game_filter}
                    GROUP BY situation,defender_player_id,event
                    ORDER BY situation,count DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['defender_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['count']:g} {'INT' if row['event'] == 'interception' else 'PD'}"
            grouped.setdefault(row["situation"], []).append(row)
        return grouped

    def pass_zone_contributors(self, season: int, *, passer_player_id: str | None = None,
                               defense_team: str | None = None,
                               game_id: str | None = None) -> list[dict[str, Any]]:
        """Receiver outcomes inside each exact charted pass zone."""
        if bool(passer_player_id) == bool(defense_team):
            raise ValueError("pass exactly one contributor scope")
        field = "passer_player_id" if passer_player_id else "defense_team"
        value = passer_player_id or canon_team(defense_team)
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, value, game_id) if game_id else (season, value)
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT depth_bucket,pass_location,receiver_player_id,
                           MAX(receiver_name) receiver_name,SUM(targets) targets,
                           SUM(receptions) receptions,SUM(receiving_yards) receiving_yards,
                           SUM(air_yards) air_yards,SUM(touchdowns) touchdowns,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=receiver_player_id LIMIT 1) position
                    FROM pass_zone_receivers WHERE season=? AND {field}=?{game_filter}
                    GROUP BY depth_bucket,pass_location,receiver_player_id
                    ORDER BY depth_bucket,pass_location,targets DESC,receiving_yards DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["adot"] = row["air_yards"] / row["targets"] if row["targets"] else None
            row["player_url"] = f"/nfl/players/{row['receiver_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['receptions']:g}/{row['targets']:g} · {row['receiving_yards']:g} yd · {row['touchdowns']:g} TD · {row['adot']:.1f} aDOT" if row["adot"] is not None else f"{row['receptions']:g}/{row['targets']:g} · {row['receiving_yards']:g} yd · {row['touchdowns']:g} TD"
        return rows

    def pass_zone_defenders(self, season: int, *, defense_team: str,
                            game_id: str | None = None) -> list[dict[str, Any]]:
        """Pass-defended and interception credits inside each charted pass zone."""
        game_filter = " AND game_id=?" if game_id else ""
        parameters = (season, canon_team(defense_team), game_id) if game_id else (season, canon_team(defense_team))
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT depth_bucket,pass_location,defender_player_id,
                           MAX(defender_name) defender_name,event,SUM(count) count,
                           (SELECT position FROM players
                            WHERE season=? AND player_id=defender_player_id LIMIT 1) position
                    FROM pass_zone_defenders WHERE season=? AND defense_team=?{game_filter}
                    GROUP BY depth_bucket,pass_location,defender_player_id,event
                    ORDER BY depth_bucket,pass_location,count DESC""",
                (season, *parameters),
            )]
        for row in rows:
            row["player_url"] = f"/nfl/players/{row['defender_player_id']}/?season={season}"
            row["position"] = row.get("position") or "UNK"
            row["detail"] = f"{row['count']:g} {'INT' if row['event'] == 'interception' else 'PD'}"
        return rows

    def player_weekly(self, season: int, player_id: str) -> list[dict[str, Any]]:
        """Return one wide row per game for a player's weekly stat line."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT week,season_type,game_id,team,opponent_team,player_name,
                          position,metric,SUM(value) value
                   FROM player_weekly_stats WHERE season=? AND player_id=?
                   GROUP BY week,season_type,game_id,team,opponent_team,player_name,position,metric
                   ORDER BY week,game_id,metric""", (season, player_id)
            )
            games: dict[str, dict[str, Any]] = {}
            for row in rows:
                game = games.setdefault(row["game_id"], {
                    "week": row["week"], "season": season, "season_type": row["season_type"],
                    "game_id": row["game_id"], "team": row["team"],
                    "opponent_team": row["opponent_team"],
                    "player_name": row["player_name"], "position": row["position"],
                })
                game[row["metric"]] = row["value"]
        return list(games.values())

    def player_career_weekly(self, player_id: str) -> list[dict[str, Any]]:
        """Every synced game for one player, across every season -- the same
        wide-row shape as player_weekly(), just not scoped to one year."""
        self.initialize()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT season,week,season_type,game_id,team,opponent_team,player_name,
                          position,metric,SUM(value) value
                   FROM player_weekly_stats WHERE player_id=?
                   GROUP BY season,week,season_type,game_id,team,opponent_team,player_name,position,metric
                   ORDER BY season,week,game_id,metric""", (player_id,)
            )
            games: dict[str, dict[str, Any]] = {}
            for row in rows:
                game = games.setdefault(row["game_id"], {
                    "week": row["week"], "season": row["season"], "season_type": row["season_type"],
                    "game_id": row["game_id"], "team": row["team"],
                    "opponent_team": row["opponent_team"],
                    "player_name": row["player_name"], "position": row["position"],
                })
                game[row["metric"]] = row["value"]
        return list(games.values())

    def player_totals(self, season: int, player_id: str) -> dict[str, float]:
        self.initialize()
        with closing(self._connect()) as connection:
            return {row["metric"]: row["value"] for row in connection.execute(
                """SELECT metric,SUM(value) value FROM player_weekly_stats
                   WHERE season=? AND player_id=? GROUP BY metric ORDER BY metric""",
                (season, player_id),
            )}
