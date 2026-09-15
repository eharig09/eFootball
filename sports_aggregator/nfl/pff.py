"""Read-only PFF export catalog and normalized NFL-owned persistence."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable

import pandas as pd

from sports_aggregator.nfl.naming import canon_position, canon_team, normalize_name
from sports_aggregator.nfl.repository import NFLRepository


PFF_FAMILIES = (
    "passing_depth", "receiving_summary", "receiving_depth", "receiving_scheme",
    "receiving_concept", "rushing_summary", "offense_blocking",
    "defense_coverage_scheme", "slot_coverage",
)
PFF_EXPLORER_METRICS = {
    "receiving_summary": (
        ("grades_pass_route", "Route grade"), ("yprr", "Yards / route"),
        ("avg_depth_of_target", "Average target depth"), ("targets", "Targets"),
        ("slot_rate", "Slot rate"), ("wide_rate", "Wide rate"),
    ),
    "rushing_summary": (
        ("grades_run", "Run grade"), ("elusive_rating", "Elusive rating"),
        ("yco_attempt", "Yards after contact / attempt"), ("avoided_tackles", "Avoided tackles"),
    ),
    "offense_blocking": (
        ("grades_pass_block", "Pass-block grade"), ("grades_run_block", "Run-block grade"),
        ("pbe", "Pass-block efficiency"), ("pressures_allowed", "Pressures allowed"),
    ),
    "defense_coverage_scheme": (
        ("man_grades_coverage_defense", "Man coverage grade"),
        ("zone_grades_coverage_defense", "Zone coverage grade"),
        ("man_forced_incompletion_rate", "Man forced incompletion rate"),
        ("zone_forced_incompletion_rate", "Zone forced incompletion rate"),
    ),
    "slot_coverage": (
        ("yards_per_coverage_snap", "Slot yards / coverage snap"),
        ("qb_rating_against", "Slot QB rating against"),
        ("coverage_snaps", "Slot coverage snaps"),
    ),
    "passing_depth": (
        ("deep_grades_pass", "Deep passing grade"),
        ("medium_grades_pass", "Intermediate passing grade"),
        ("short_grades_pass", "Short passing grade"),
        ("deep_attempts_percent", "Deep attempt rate"),
        ("avg_depth_of_target", "Average depth of target"),
    ),
    "receiving_depth": (
        ("deep_grades_pass_route", "Deep route grade"),
        ("medium_grades_pass_route", "Intermediate route grade"),
        ("short_grades_pass_route", "Short route grade"),
        ("behind_los_grades_pass_route", "Behind-LOS route grade"),
        ("deep_yprr", "Deep yards / route"),
        ("deep_targets", "Deep targets"),
    ),
}
PFF_ROOTS = (Path("nfl/pff"), Path("pff_coverage_data"))
IDENTITY_COLUMNS = {"player", "player_id", "position", "team_name", "player_game_count",
                    "franchise_id", "team", "games"}
MIN_CONFIDENCE = 0.80
MIN_MARGIN = 0.15
FULL_SEASON_WEEKS = 18
MIN_SAMPLE_FRACTION = 0.12


def season_scaled_minimum(base: float, weeks_played: int | None) -> float:
    """Scale a full-season "qualified" sample floor down early in the season.

    Thresholds like 100 routes or 100 combined snaps assume a full season of
    accumulation. Applied unscaled against a database that only has one or
    two weeks of PFF data synced so far, they exclude every player who has
    actually played, so a freshly uploaded snapshot looks empty rather than
    thin. `MIN_SAMPLE_FRACTION` keeps a single unusual game from qualifying
    as a "leader" outright.
    """
    fraction = max(MIN_SAMPLE_FRACTION, min(1.0, (weeks_played or 0) / FULL_SEASON_WEEKS))
    return base * fraction


def _family(path: Path) -> str:
    return re.split(r" \(\d+\)$", path.stem)[0].strip()


def _pff_id(value: Any) -> str | None:
    try:
        if pd.isna(value):
            return None
        return str(int(float(value)))
    except (TypeError, ValueError):
        return None


class NFLPFFService:
    """Catalog sibling-repo exports and persist normalized, queryable metrics."""

    def __init__(self, repository: NFLRepository, source_root: str | Path,
                upload_root: str | Path | None = None) -> None:
        self.repository = repository
        self.source_root = Path(source_root).resolve()
        # A second, flat root for files uploaded through the browser, so a
        # deploy with no sibling scouting_report checkout (Render) can still
        # ingest PFF exports without one.
        self.upload_root = Path(upload_root).resolve() if upload_root else None

    def _paths(self) -> list[Path]:
        seen: set[Path] = set()
        output = []
        for relative in PFF_ROOTS:
            root = (self.source_root / relative).resolve()
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.csv")):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved); output.append(resolved)
        if self.upload_root and self.upload_root.is_dir():
            for path in sorted(self.upload_root.rglob("*.csv")):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved); output.append(resolved)
        return output

    def _fingerprints(self) -> dict[int, tuple[set[tuple[str, str]], set[tuple[str, str]]]]:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            seasons = [row[0] for row in connection.execute(
                "SELECT DISTINCT season FROM players ORDER BY season"
            )]
            output = {}
            for season in seasons:
                rows = connection.execute(
                    "SELECT full_name,team,pff_id FROM players WHERE season=?", (season,)
                )
                ids: set[tuple[str, str]] = set(); names: set[tuple[str, str]] = set()
                for row in rows:
                    team = canon_team(row["team"])
                    names.add((normalize_name(row["full_name"]), team))
                    pid = _pff_id(row["pff_id"])
                    if pid:
                        ids.add((pid, team))
                output[int(season)] = (ids, names)
        return output

    @staticmethod
    def _identify(frame: pd.DataFrame, fingerprints) -> tuple[int | None, float, float]:
        teams = frame.get("team_name", frame.get("team", pd.Series(dtype=object))).map(canon_team)
        names = frame.get("player", pd.Series(dtype=object)).map(normalize_name)
        raw_ids = pd.to_numeric(frame.get("player_id", pd.Series(dtype=object)), errors="coerce")
        valid = raw_ids.notna()
        ids = set(zip(raw_ids[valid].astype("int64").astype(str), teams[valid]))
        name_pairs = set(zip(names, teams))
        scores = {}
        for season, (roster_ids, roster_names) in fingerprints.items():
            id_score = len(ids & roster_ids) / len(ids) if ids else 0
            name_score = len(name_pairs & roster_names) / len(name_pairs) if name_pairs else 0
            scores[season] = max(id_score, name_score)
        if not scores:
            return None, 0, 0
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        season, confidence = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        margin = confidence - runner_up
        usable = confidence >= MIN_CONFIDENCE and margin >= MIN_MARGIN
        return (season if usable else None), confidence, margin

    def scan(self, *, force: bool = False) -> list[dict[str, Any]]:
        self.repository.initialize()
        now = datetime.now(timezone.utc).isoformat()
        fingerprints = self._fingerprints()
        with closing(self.repository._connect()) as connection:
            cached = {row["path"]: dict(row) for row in connection.execute(
                "SELECT * FROM nfl_pff_catalog"
            )}
            records = []
            for path in self._paths():
                stamp = f"{path.stat().st_size}:{path.stat().st_mtime_ns}"
                hit = cached.get(str(path))
                if hit and hit["stamp"] == stamp and not force:
                    records.append(hit); continue
                family = _family(path)
                record = {"path": str(path), "family": family, "season": None,
                          "confidence": 0.0, "margin": 0.0, "row_count": 0,
                          "usable": 0, "note": "", "stamp": stamp, "scanned_at": now}
                try:
                    columns = pd.read_csv(path, nrows=0).columns.tolist()
                    aliases = {"team": "team_name", "games": "player_game_count"}
                    use = [column for column in columns if column in IDENTITY_COLUMNS]
                    frame = pd.read_csv(path, usecols=use, low_memory=False).rename(columns=aliases)
                    record["row_count"] = len(frame)
                    if family not in PFF_FAMILIES:
                        record["note"] = "family not selected for NFL analytics"
                    elif not {"player", "team_name"}.issubset(frame.columns):
                        record["note"] = "missing player/team identity columns"
                    else:
                        season, confidence, margin = self._identify(frame, fingerprints)
                        record.update(season=season, confidence=confidence, margin=margin,
                                      usable=int(season is not None))
                        if season is None:
                            record["note"] = "season not separable from synchronized rosters"
                except Exception as exc:
                    record["note"] = f"unreadable: {exc.__class__.__name__}"
                connection.execute(
                    """INSERT OR REPLACE INTO nfl_pff_catalog
                       (path,family,season,confidence,margin,row_count,usable,note,stamp,scanned_at)
                       VALUES(:path,:family,:season,:confidence,:margin,:row_count,:usable,:note,:stamp,:scanned_at)""",
                    record,
                )
                records.append(record)
            connection.commit()
        return sorted(records, key=lambda row: (row["family"], row.get("season") or 0, row["path"]))

    def _resolve(self, catalog: Iterable[dict], family: str, season: int) -> dict | None:
        matches = [row for row in catalog if row["family"] == family
                   and row.get("season") == season and row["usable"]]
        return sorted(matches, key=lambda row: (-row["row_count"], row["path"]))[0] if matches else None

    def sync(self, season: int, *, force_scan: bool = False) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        catalog = self.scan(force=force_scan)
        with closing(self.repository._connect()) as connection:
            pff_to_gsis = {}
            name_to_gsis = {}
            for row in connection.execute(
                "SELECT player_id,team,full_name,pff_id FROM players ORDER BY season"
            ):
                pid = _pff_id(row["pff_id"])
                if pid: pff_to_gsis[pid] = row["player_id"]
            for row in connection.execute(
                "SELECT player_id,team,full_name FROM players WHERE season=?", (season,)
            ):
                name_to_gsis[(normalize_name(row["full_name"]), row["team"])] = row["player_id"]

            families = player_rows = metric_rows = unresolved = 0
            details = []
            for family in PFF_FAMILIES:
                source = self._resolve(catalog, family, season)
                if source is None:
                    continue
                frame = pd.read_csv(source["path"], low_memory=False)
                if "team" in frame and "team_name" not in frame:
                    frame = frame.rename(columns={"team": "team_name"})
                connection.execute(
                    "DELETE FROM nfl_pff_player_metrics WHERE season=? AND family=?",
                    (season, family),
                )
                identities = {}; values = []
                numeric_columns = [column for column in frame.columns
                                   if column not in IDENTITY_COLUMNS]
                numeric = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
                for index, row in frame.iterrows():
                    pid = _pff_id(row.get("player_id"))
                    if not pid:
                        continue
                    team = canon_team(row.get("team_name"))
                    name = str(row.get("player") or "").strip()
                    gsis = pff_to_gsis.get(pid)
                    key_source = "pff_id" if gsis else "name_team"
                    confidence = 1.0 if gsis else 0.75
                    if not gsis:
                        gsis = name_to_gsis.get((normalize_name(name), team))
                    if not gsis:
                        key_source = "unresolved"; confidence = 0.0; unresolved += 1
                    identities[(pid, team)] = (
                        season, pid, gsis, team, name, canon_position(row.get("position")),
                        key_source, confidence, source["path"], datetime.now(timezone.utc).isoformat(),
                    )
                    for metric in numeric_columns:
                        value = numeric.at[index, metric]
                        if pd.notna(value):
                            values.append((season, 0, family, pid, gsis, team, metric,
                                           float(value), source["path"]))
                connection.executemany(
                    "INSERT OR REPLACE INTO nfl_pff_players VALUES (?,?,?,?,?,?,?,?,?,?)",
                    identities.values(),
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO nfl_pff_player_metrics VALUES (?,?,?,?,?,?,?,?,?)",
                    values,
                )
                families += 1; player_rows += len(identities); metric_rows += len(values)
                details.append({"family": family, "path": source["path"],
                                "players": len(identities), "metrics": len(values)})
            finished = datetime.now(timezone.utc)
            connection.execute(
                """INSERT INTO nfl_pff_imports
                   (season,started_at,finished_at,files_scanned,families_imported,
                    player_rows,metric_rows,unresolved_players,details_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (season, started.isoformat(), finished.isoformat(), len(catalog), families,
                 player_rows, metric_rows, unresolved, json.dumps(details)),
            )
            connection.commit()
        return {"season": season, "files_scanned": len(catalog), "families": families,
                "players": player_rows, "metrics": metric_rows, "unresolved": unresolved,
                "details": details}

    def counts(self, season: int) -> dict[str, int]:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            return {
                "families": connection.execute(
                    "SELECT COUNT(DISTINCT family) FROM nfl_pff_player_metrics WHERE season=?", (season,)
                ).fetchone()[0],
                "players": connection.execute(
                    "SELECT COUNT(DISTINCT pff_id) FROM nfl_pff_players WHERE season=?", (season,)
                ).fetchone()[0],
                "metrics": connection.execute(
                    "SELECT COUNT(*) FROM nfl_pff_player_metrics WHERE season=?", (season,)
                ).fetchone()[0],
            }

    def catalog_rows(self) -> list[dict[str, Any]]:
        self.repository.initialize()
        with closing(self.repository._connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM nfl_pff_catalog ORDER BY family,season,path"
            )]

    def leaders(self, season: int, family: str, metric: str, *, team: str | None = None,
                limit: int = 25, lower: bool = False, minimum_metric: str | None = None,
                minimum_value: float = 0) -> list[dict[str, Any]]:
        where = "m.season=? AND m.family=? AND m.metric=? AND m.week=0"
        params: list[Any] = [season, family, metric]
        sample_join = ""
        if minimum_metric:
            sample_join = """ JOIN nfl_pff_player_metrics sample
              ON sample.season=m.season AND sample.week=m.week AND sample.family=m.family
             AND sample.pff_id=m.pff_id AND sample.team=m.team AND sample.metric=?"""
            params.insert(0, minimum_metric)
            where += " AND sample.value>=?"; params.append(minimum_value)
        if team:
            where += " AND m.team=?"; params.append(team)
        params.append(max(1, min(limit, 100)))
        with closing(self.repository._connect()) as connection:
            return [dict(row) for row in connection.execute(
                f"""SELECT m.*,p.player_name,p.position,p.key_source,p.match_confidence
                     FROM nfl_pff_player_metrics m {sample_join} JOIN nfl_pff_players p
                       ON p.season=m.season AND p.pff_id=m.pff_id AND p.team=m.team
                     WHERE {where} ORDER BY m.value {'ASC' if lower else 'DESC'} LIMIT ?""", params
            )]

    def player(self, season: int, gsis_id: str) -> dict[str, Any]:
        with closing(self.repository._connect()) as connection:
            identities = [dict(row) for row in connection.execute(
                "SELECT * FROM nfl_pff_players WHERE season=? AND gsis_id=? ORDER BY team",
                (season, gsis_id),
            )]
            metrics = [dict(row) for row in connection.execute(
                """SELECT family,week,metric,value,team,source_path
                   FROM nfl_pff_player_metrics WHERE season=? AND gsis_id=?
                   ORDER BY family,week,metric""", (season, gsis_id)
            )]
        by_family: dict[str, dict[str, float]] = {}
        for row in metrics:
            by_family.setdefault(row["family"], {})[row["metric"]] = row["value"]
        return {"identities": identities, "metrics": metrics, "by_family": by_family}

    def family_profiles(self, season: int, family: str, team: str,
                        metrics: Iterable[str]) -> list[dict[str, Any]]:
        """Return a small wide player view for matchup feature construction."""
        wanted = tuple(dict.fromkeys(metrics))
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        with closing(self.repository._connect()) as connection:
            rows = connection.execute(
                f"""SELECT m.pff_id,m.gsis_id,m.team,p.player_name,p.position,
                            m.metric,m.value
                     FROM nfl_pff_player_metrics m JOIN nfl_pff_players p
                       ON p.season=m.season AND p.pff_id=m.pff_id AND p.team=m.team
                     WHERE m.season=? AND m.week=0 AND m.family=? AND m.team=?
                       AND m.metric IN ({placeholders})
                     ORDER BY p.player_name,m.metric""",
                (season, family, team, *wanted),
            )
            players: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                key = (row["pff_id"], row["team"])
                item = players.setdefault(key, {
                    "pff_id": row["pff_id"], "gsis_id": row["gsis_id"],
                    "team": row["team"], "player_name": row["player_name"],
                    "position": row["position"],
                })
                item[row["metric"]] = row["value"]
        return list(players.values())

    def team_coverage_tendency(self, season: int, team: str) -> dict[str, Any]:
        """Aggregate player coverage snaps into a transparent man/zone proxy."""
        metrics = ("man_snap_counts_coverage", "zone_snap_counts_coverage")
        placeholders = ",".join("?" for _ in metrics)
        with closing(self.repository._connect()) as connection:
            rows = connection.execute(
                f"""SELECT metric,SUM(value) value,COUNT(DISTINCT pff_id) players
                     FROM nfl_pff_player_metrics
                     WHERE season=? AND week=0 AND family='defense_coverage_scheme'
                       AND team=? AND metric IN ({placeholders}) GROUP BY metric""",
                (season, team, *metrics),
            )
            values = {row["metric"]: row["value"] for row in rows}
        man = values.get(metrics[0]) or 0; zone = values.get(metrics[1]) or 0
        total = man + zone
        return {"season": season, "man_snaps": man, "zone_snaps": zone,
                "coverage_snaps": total, "man_rate": man / total if total else None,
                "zone_rate": zone / total if total else None}

    def available(self, season: int) -> dict[str, list[dict[str, str]]]:
        with closing(self.repository._connect()) as connection:
            present = {row[0] for row in connection.execute(
                "SELECT DISTINCT family FROM nfl_pff_player_metrics WHERE season=?", (season,)
            )}
        return {family: [{"metric": metric, "label": label} for metric, label in metrics]
                for family, metrics in PFF_EXPLORER_METRICS.items() if family in present}
