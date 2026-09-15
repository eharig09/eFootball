"""Build a compact public-data NFL SQLite seed for memory-constrained Render."""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
import shutil
import sqlite3

from sports_aggregator.nfl.repository import NFLRepository


FILTERS = {
    "teams": "1",
    "games": "season >= 2010",
    "players": "season >= 2025",
    "player_weekly_stats": "season >= 2026",
    "snap_counts": "season >= 2025",
    "depth_chart_snapshots": "season >= 2025 AND snapshot_at = (SELECT MAX(d2.snapshot_at) FROM source.depth_chart_snapshots d2 WHERE d2.season=depth_chart_snapshots.season AND d2.team=depth_chart_snapshots.team)",
    "team_staff": "season >= 2025",
    "injury_reports": "season >= 2026",
    "player_master": "1",
    "player_external_ids": "1",
    "team_weekly_stats": "season >= 2025",
    "game_team_efficiency": "season >= 2025",
    "game_team_situational": "season >= 2025",
    "game_team_playcalling": "season >= 2025",
    "nfl_elo_games": "1",
    "nfl_elo_ratings": "1",
    "qb_pass_profiles": "season >= 2025",
    "receiver_pass_profiles": "season >= 2025",
    "pass_zone_receivers": "season >= 2025",
    "pass_zone_defenders": "season >= 2025",
    "rush_direction_profiles": "season >= 2025",
    "rush_direction_defenders": "season >= 2025",
    "rush_situational_profiles": "season >= 2025",
    "rush_situational_defenders": "season >= 2025",
    "qb_situational_profiles": "season >= 2025",
    "receiver_situational_profiles": "season >= 2025",
    "situational_pass_receivers": "season >= 2025",
    "situational_pass_defenders": "season >= 2025",
    "nfl_content_items": "1",
    "nfl_content_teams": "1",
    "nfl_content_players": "season >= 2025",
    "nfl_content_games": "1",
    "nfl_content_source_checks": "1",
    "nfl_content_ingestion_runs": "season >= 2025",
}


def build(source: Path, output: Path) -> None:
    raw = output.with_suffix("") if output.suffix == ".gz" else output
    raw.unlink(missing_ok=True)
    NFLRepository(raw).initialize()
    connection = sqlite3.connect(raw)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("ATTACH DATABASE ? AS source", (str(source.resolve()),))
        for table, where in FILTERS.items():
            # Column order can drift between databases whose "games"-style
            # tables picked up columns via different ALTER TABLE histories.
            # Matching by name (not position) keeps a stray reorder from
            # silently shifting values into the wrong column.
            dest_columns = [row[1] for row in connection.execute(f'PRAGMA main.table_info("{table}")')]
            source_columns = {row[1] for row in connection.execute(f'PRAGMA source.table_info("{table}")')}
            shared = [column for column in dest_columns if column in source_columns]
            column_list = ",".join(f'"{column}"' for column in shared)
            connection.execute(
                f"INSERT OR IGNORE INTO main.{table} ({column_list}) "
                f"SELECT {column_list} FROM source.{table} WHERE {where}"
            )
            connection.commit()
        connection.execute("DETACH DATABASE source")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    finally:
        connection.close()
    if output.suffix == ".gz":
        output.parent.mkdir(parents=True, exist_ok=True)
        with raw.open("rb") as incoming, gzip.open(output, "wb", compresslevel=9) as compressed:
            shutil.copyfileobj(incoming, compressed, length=1024 * 1024)
        raw.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("instance/nfl.sqlite3"))
    parser.add_argument("--output", type=Path,
                        default=Path("data/nfl/render_seed.sqlite3.gz"))
    args = parser.parse_args()
    build(args.source, args.output)
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
