from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from sports_aggregator.social.models import IdentityResolution, LeagueSourceProfile, SourceProfile


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 source_id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL DEFAULT 'bluesky',
 handle TEXT NOT NULL UNIQUE, did TEXT UNIQUE, display_name TEXT NOT NULL,
 organization TEXT, source_type TEXT NOT NULL, reliability INTEGER NOT NULL,
 original_reporting_score INTEGER NOT NULL, analysis_score INTEGER NOT NULL,
 breaking_news_score INTEGER NOT NULL, prospect_score INTEGER NOT NULL,
 g5_score INTEGER NOT NULL, priority INTEGER NOT NULL, active INTEGER NOT NULL,
 trust_status TEXT NOT NULL DEFAULT 'TRUSTED_SEED', resolution_status TEXT NOT NULL DEFAULT 'unresolved',
 resolved_handle TEXT, verified_at TEXT, last_checked TEXT, last_error TEXT,
 profile_description TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_specialties (
 source_id INTEGER NOT NULL, specialty TEXT NOT NULL, PRIMARY KEY(source_id,specialty),
 FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS source_teams (
 source_id INTEGER NOT NULL, team TEXT NOT NULL, expertise REAL NOT NULL DEFAULT 1.0,
 PRIMARY KEY(source_id,team), FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS source_conferences (
 source_id INTEGER NOT NULL, conference TEXT NOT NULL, expertise REAL NOT NULL DEFAULT 1.0,
 PRIMARY KEY(source_id,conference), FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_sources_did ON sources(did);
CREATE TABLE IF NOT EXISTS source_league_scopes (
 source_id INTEGER NOT NULL, league TEXT NOT NULL, scope TEXT NOT NULL,
 conference TEXT, division TEXT, team TEXT, account_type TEXT NOT NULL,
 directory_priority INTEGER NOT NULL, profile_url TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '', verification TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(source_id,league),
 FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_league_team
 ON source_league_scopes(league,team,directory_priority);
CREATE TABLE IF NOT EXISTS source_league_tags (
 source_id INTEGER NOT NULL, league TEXT NOT NULL, tag TEXT NOT NULL,
 tag_type TEXT NOT NULL, section TEXT NOT NULL,
 PRIMARY KEY(source_id,league,tag,tag_type),
 FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_league_section
 ON source_league_tags(league,section,source_id);
"""


class SourceRegistry:
    def __init__(self, database_path: str | Path): self.path = Path(database_path)
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db=sqlite3.connect(self.path,timeout=20); db.row_factory=sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON"); return db
    def initialize(self):
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)
            columns={row[1] for row in db.execute("PRAGMA table_info(sources)")}
            if "resolved_handle" not in columns: db.execute("ALTER TABLE sources ADD COLUMN resolved_handle TEXT")
            if "last_error" not in columns: db.execute("ALTER TABLE sources ADD COLUMN last_error TEXT")
            db.commit()
    def seed(self, profiles: Iterable[SourceProfile]) -> int:
        items=tuple(profiles); self.initialize(); now=datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db:
            for p in items:
                db.execute("""INSERT INTO sources(handle,display_name,organization,source_type,reliability,
                 original_reporting_score,analysis_score,breaking_news_score,prospect_score,g5_score,priority,active,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET display_name=excluded.display_name,
                 organization=excluded.organization,source_type=excluded.source_type,reliability=excluded.reliability,
                 original_reporting_score=excluded.original_reporting_score,analysis_score=excluded.analysis_score,
                 breaking_news_score=excluded.breaking_news_score,prospect_score=excluded.prospect_score,
                 g5_score=excluded.g5_score,priority=excluded.priority,active=excluded.active,updated_at=excluded.updated_at""",
                 (p.handle,p.display_name,p.organization,p.source_type,p.reliability,p.original_reporting_score,
                  p.analysis_score,p.breaking_news_score,p.prospect_score,p.g5_score,p.priority,int(p.active),now))
                source_id=db.execute("SELECT source_id FROM sources WHERE handle=?",(p.handle,)).fetchone()[0]
                db.execute("DELETE FROM source_specialties WHERE source_id=?",(source_id,)); db.execute("DELETE FROM source_teams WHERE source_id=?",(source_id,)); db.execute("DELETE FROM source_conferences WHERE source_id=?",(source_id,))
                db.executemany("INSERT INTO source_specialties VALUES(?,?)",[(source_id,x) for x in p.specialties])
                db.executemany("INSERT INTO source_teams VALUES(?,?,1.0)",[(source_id,x) for x in p.teams])
                db.executemany("INSERT INTO source_conferences VALUES(?,?,1.0)",[(source_id,x) for x in p.conferences])
            db.commit()
        return len(items)

    def seed_league(self, profiles: Iterable[LeagueSourceProfile]) -> int:
        """Upsert league scope without overwriting another sport's metadata."""
        items = tuple(profiles); self.initialize(); now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db:
            for item in items:
                p = item.source
                db.execute(
                    """INSERT INTO sources(handle,display_name,organization,source_type,reliability,
                       original_reporting_score,analysis_score,breaking_news_score,prospect_score,
                       g5_score,priority,active,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(handle) DO UPDATE SET
                         priority=MAX(sources.priority,excluded.priority),active=1,
                         updated_at=excluded.updated_at""",
                    (p.handle, p.display_name, p.organization, p.source_type, p.reliability,
                     p.original_reporting_score, p.analysis_score, p.breaking_news_score,
                     p.prospect_score, p.g5_score, p.priority, int(p.active), now),
                )
                source_id = db.execute(
                    "SELECT source_id FROM sources WHERE handle=?", (p.handle,)
                ).fetchone()[0]
                db.execute(
                    """INSERT INTO source_league_scopes
                       (source_id,league,scope,conference,division,team,account_type,
                        directory_priority,profile_url,notes,verification)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id,league) DO UPDATE SET
                         scope=excluded.scope,conference=excluded.conference,
                         division=excluded.division,team=excluded.team,
                         account_type=excluded.account_type,
                         directory_priority=excluded.directory_priority,
                         profile_url=excluded.profile_url,notes=excluded.notes,
                         verification=excluded.verification""",
                    (source_id, item.league, item.scope, item.conference, item.division,
                     item.team, item.account_type, item.directory_priority,
                     item.profile_url, item.notes, item.verification),
                )
                db.execute(
                    "DELETE FROM source_league_tags WHERE source_id=? AND league=?",
                    (source_id, item.league),
                )
                db.executemany(
                    "INSERT INTO source_league_tags VALUES(?,?,?,?,?)",
                    [(source_id, item.league, tag, tag_type, section)
                     for tag, tag_type, section in item.tags],
                )
            db.commit()
        return len(items)

    def list_league_sources(
        self, league: str, *, section: str | None = None,
        team: str | None = None, limit: int | None = None,
    ) -> list[dict]:
        self.initialize()
        conditions = ["ls.league=?", "s.active=1"]
        params: list[object] = [league]
        if section:
            conditions.append(
                "EXISTS (SELECT 1 FROM source_league_tags lt "
                "WHERE lt.source_id=s.source_id AND lt.league=ls.league AND lt.section=?)"
            )
            params.append(section)
        if team:
            conditions.append("(ls.team=? OR ls.team IS NULL)")
            params.append(team)
        sql = f"""SELECT s.source_id,s.handle,s.did,s.display_name,s.organization,
                   s.source_type,s.reliability,s.resolution_status,s.last_checked,
                   s.verified_at,s.last_error,
                   ls.scope,ls.conference,ls.division,ls.team,ls.account_type,
                   ls.directory_priority,ls.profile_url,ls.notes,ls.verification
                   FROM sources s JOIN source_league_scopes ls USING(source_id)
                   WHERE {' AND '.join(conditions)}
                   ORDER BY ls.directory_priority,s.display_name"""
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with closing(self._connect()) as db:
            rows = [dict(row) for row in db.execute(sql, params)]
            for row in rows:
                row["tags"] = [dict(tag) for tag in db.execute(
                    """SELECT tag,tag_type,section FROM source_league_tags
                       WHERE source_id=? AND league=? ORDER BY tag_type,tag""",
                    (row["source_id"], league),
                )]
        return rows
    def unresolved_handles(self, force=False):
        self.initialize()
        with closing(self._connect()) as db:
            sql="SELECT handle FROM sources WHERE active=1" + ("" if force else " AND (did IS NULL OR resolution_status!='verified')")
            return [r[0] for r in db.execute(sql).fetchall()]
    def store_resolution(self, result: IdentityResolution):
        self.initialize(); now=datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db:
            db.execute("""UPDATE sources SET
             did=CASE WHEN ?='verified' THEN ? ELSE did END,
             resolved_handle=COALESCE(?,resolved_handle),resolution_status=?,
             verified_at=CASE WHEN ?='verified' THEN ? ELSE verified_at END,
             last_checked=?,last_error=CASE WHEN ?='verified' THEN NULL ELSE ? END,
             profile_description=CASE WHEN ?='verified' THEN ? ELSE profile_description END,
             updated_at=? WHERE handle=?""",
             (result.status,result.did,result.current_handle,result.status,result.status,now,now,
              result.status,result.description,result.status,result.description,now,result.requested_handle)); db.commit()
    def list_sources(self):
        self.initialize()
        with closing(self._connect()) as db:
            rows=db.execute("SELECT * FROM sources ORDER BY priority DESC,reliability DESC,display_name").fetchall(); result=[]
            for row in rows:
                item=dict(row); sid=item['source_id']
                item['specialties']=[r[0] for r in db.execute("SELECT specialty FROM source_specialties WHERE source_id=? ORDER BY specialty",(sid,))]
                item['teams']=[r[0] for r in db.execute("SELECT team FROM source_teams WHERE source_id=? ORDER BY team",(sid,))]
                item['conferences']=[r[0] for r in db.execute("SELECT conference FROM source_conferences WHERE source_id=? ORDER BY conference",(sid,))]
                result.append(item)
            return result
    def coverage(self):
        self.initialize()
        with closing(self._connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teams'").fetchone():
                return {'conferences': [], 'teams': []}
            conferences=[dict(row) for row in db.execute("""SELECT t.conference,
              COUNT(DISTINCT CASE WHEN s.active=1 AND s.resolution_status='verified' THEN s.source_id END) source_count
              FROM teams t LEFT JOIN source_conferences sc ON sc.conference=t.conference
              LEFT JOIN sources s ON s.source_id=sc.source_id WHERE t.conference IS NOT NULL
              GROUP BY t.conference ORDER BY source_count,t.conference""")]
            teams=[dict(row) for row in db.execute("""SELECT t.team_id,t.school,t.conference,
              COUNT(DISTINCT CASE WHEN s.active=1 AND s.resolution_status='verified' THEN s.source_id END) source_count
              FROM teams t LEFT JOIN source_teams st ON st.team=t.school
              LEFT JOIN sources s ON s.source_id=st.source_id
              GROUP BY t.team_id,t.school,t.conference ORDER BY source_count,t.school""")]
        for row in conferences:
            row['rating']='EXCELLENT' if row['source_count']>=5 else ('GOOD' if row['source_count']>=2 else 'LOW')
        return {'conferences':conferences,'teams':teams}
    def status(self):
        sources=self.list_sources(); return {'count':len(sources),'verified':sum(x['resolution_status']=='verified' for x in sources),'failed':sum(x['resolution_status']!='verified' for x in sources),'sources':sources,'coverage':self.coverage()}
