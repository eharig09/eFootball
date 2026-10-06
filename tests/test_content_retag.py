"""Retag only re-resolves items that need it. Before this it re-resolved the whole archive on every
refresh, and ingestion overwrote settled tags with a lesser pass that retag then had to repair."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sports_aggregator.cfb.models import Player, Team
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.social import content as content_module
from sports_aggregator.social.content import ContentRepository
from sports_aggregator.social.models import SourceEndpointProfile, SourceEntityProfile
from sports_aggregator.social.unified import UnifiedSourceRegistry

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
FRESH, MID, OLD = "2026-10-04T12:00:00Z", "2026-08-07T12:00:00Z", "2026-03-20T12:00:00Z"   # 2d, 60d, 200d


@pytest.fixture()
def repo(tmp_path):
    path = str(tmp_path / "cfb.sqlite3")
    cfb = CFBRepository(path)
    cfb.replace_teams((Team(1, "Michigan", "Wolverines", "MICH", "Big Ten", None, "fbs", None, None,
                            (), ("Michigan",), None, None),))
    cfb.replace_players(2026, (Player("p1", 2026, "Alex", "Example", "Michigan", "QB", 7, 74, 210, 3),))
    graph = UnifiedSourceRegistry(path)
    entity_id = graph.upsert_entity(SourceEntityProfile(
        name="Beat Reporter", organization="Paper", entity_type="PERSON",
        source_classes=("BEAT_REPORTER",), teams=("Michigan",), reporting_score=5))
    graph.upsert_endpoint(entity_id, SourceEndpointProfile(
        platform="bluesky", endpoint_type="BLUESKY_ACCOUNT", handle="beat.example",
        platform_id="did:plc:beat", verification_status="verified"))
    repository = ContentRepository(path)
    repository.endpoint = repository.bluesky_endpoints()[0]
    return repository


def _item(n, created_at, text=None):
    return {"post": {"uri": f"at://did:plc:beat/app.bsky.feed.post/{n}", "cid": f"cid{n}",
                     "author": {"did": "did:plc:beat", "handle": "beat.example", "displayName": "Beat"},
                     "record": {"text": text or f"Michigan starter Alex Example missed practice with an injury ({n}).",
                                "createdAt": created_at}}}


def _store(repository, n, created_at, text=None):
    content_id = repository.store_bluesky_post(repository.endpoint, _item(n, created_at, text), 2026)
    assert content_id is not None
    return content_id


def _state(repository, content_id):
    with sqlite3.connect(repository.path) as connection:
        row = connection.execute(
            "SELECT fingerprint,content_hash,source,tagged_at FROM content_tag_state WHERE content_id=?",
            (content_id,)).fetchone()
    return dict(zip(("fingerprint", "content_hash", "source", "tagged_at"), row)) if row else None


def _three(repository):
    return [_store(repository, 1, FRESH), _store(repository, 2, MID), _store(repository, 3, OLD)]


def _settle(repository, **kwargs):
    """One retag, so every stored item is canonical, with its tags stamped at NOW."""
    return repository.retag(2026, now=NOW, **kwargs)


def _why(report):
    return {key[4:]: value for key, value in report.items() if key.startswith("why_")}


# ---------------------------------------------------------------------------- state is recorded
def test_ingestion_records_who_tagged_the_item_and_from_what(repo):
    ids = _three(repo)
    for content_id in ids:
        state = _state(repo, content_id)
        assert state["source"] == "ingest" and state["content_hash"]
    assert len({_state(repo, i)["fingerprint"] for i in ids}) == 1


def test_a_retag_marks_items_canonical(repo):
    ids = _three(repo)
    report = _settle(repo)
    assert _why(report) == {"unsettled": 3}              # ingestion's truncated-text pass: settle all three
    assert all(_state(repo, i)["source"] == "retag" for i in ids)


# --------------------------------------------------------- re-ingestion must not undo canonical tags
def test_reingesting_unchanged_content_leaves_settled_tags_alone(repo):
    cid = _store(repo, 1, FRESH)
    _settle(repo)
    before = _state(repo, cid)
    assert _store(repo, 1, FRESH) == cid                 # the next refresh sees the same post again
    assert _state(repo, cid) == before                   # not re-tagged: still canonical, same timestamp


def test_reingesting_changed_content_is_tagged_again_and_needs_settling(repo):
    cid = _store(repo, 1, FRESH)
    _settle(repo)
    _store(repo, 1, FRESH, text="Michigan quarterback Alex Example is out for the season with an injury.")
    state = _state(repo, cid)
    assert state["source"] == "ingest"
    report = repo.retag(2026, now=NOW)
    assert _why(report).get("unsettled") == 1


def test_a_rules_change_makes_ingestion_retag_a_settled_item_again(repo, monkeypatch):
    cid = _store(repo, 1, FRESH)
    _settle(repo)
    monkeypatch.setattr(content_module, "_tagging_source_digest", lambda: "a-rule-changed")
    repo._fingerprint = None
    _store(repo, 1, FRESH)
    assert _state(repo, cid)["source"] == "ingest"       # tagged under the new rules, awaiting its canonical pass


# ------------------------------------------------------------------------------ what retag selects
def test_nothing_changed_means_only_the_daily_context_refresh_of_recent_items(repo):
    _three(repo)
    _settle(repo)
    # a few hours later: the recent item was tagged <20h ago, the others are old and settled
    report = repo.retag(2026, now=NOW + timedelta(hours=4))
    assert report["items"] == 0 and _why(report) == {"current": 3}
    # a day later, the 2-day-old item's tags are a day old: its roster/schedule context is refreshed
    report = repo.retag(2026, now=NOW + timedelta(days=1))
    assert report["items"] == 1 and _why(report) == {"context": 1, "current": 2}


def test_a_rule_change_reaches_the_archive_but_not_the_frozen_items(repo, monkeypatch):
    _three(repo)
    _settle(repo)
    monkeypatch.setattr(content_module, "_tagging_source_digest", lambda: "a-rule-changed")
    report = repo.retag(2026, now=NOW + timedelta(hours=4))
    assert _why(report) == {"rules": 2, "frozen": 1}     # the 2-day and 60-day items; not the 200-day one
    assert repo.retag(2026, now=NOW + timedelta(hours=4))["items"] == 0   # settled under the new rules


def test_a_change_to_team_aliases_counts_as_a_rule_change(repo):
    _three(repo)
    _settle(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("INSERT INTO team_aliases(team_id,alias,normalized_alias,alias_type) "
                           "VALUES(1,'Wolverines','wolverines','test')")
    assert _why(repo.retag(2026, now=NOW + timedelta(hours=4)))["rules"] == 2


def test_changed_content_is_retagged_whatever_its_age(repo):
    ids = _three(repo)
    _settle(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE content_items SET body_text='edited after the fact' WHERE content_id=?", (ids[2],))
    report = repo.retag(2026, now=NOW + timedelta(hours=4))
    assert _why(report) == {"changed": 1, "current": 2}


def test_force_all_re_resolves_everything(repo):
    _three(repo)
    _settle(repo)
    report = repo.retag(2026, now=NOW + timedelta(hours=4), force_all=True)
    assert report["items"] == 3 and _why(report) == {"forced": 3}


def test_items_from_before_state_was_tracked_are_retagged_once_unless_frozen(repo):
    _three(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("DELETE FROM content_tag_state")
    report = _settle(repo)
    assert _why(report) == {"untracked": 2, "frozen": 1}      # fresh + 60-day; the 200-day item is left
    assert _why(repo.retag(2026, now=NOW + timedelta(hours=4))) == {"current": 2, "frozen": 1}


def test_the_windows_are_adjustable(repo):
    _three(repo)
    _settle(repo)
    monkey = repo.retag(2026, now=NOW + timedelta(days=1), active_days=90, archive_days=365)
    assert _why(monkey) == {"context": 2, "current": 1}       # fresh and 60-day are both "active" now


def test_an_item_with_no_usable_date_is_treated_as_recent(repo):
    ids = _three(repo)
    _settle(repo)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE content_items SET published_at='not a date', ingested_at='' WHERE content_id=?",
                           (ids[2],))
        connection.execute("UPDATE content_tag_state SET content_hash=? WHERE content_id=?",
                           (content_module._content_hash(*connection.execute(
                               "SELECT title,body_text,summary FROM content_items WHERE content_id=?", (ids[2],)).fetchone()),
                            ids[2]))
    report = repo.retag(2026, now=NOW + timedelta(days=1))
    assert _why(report) == {"context": 2, "current": 1}       # the 2-day item and the undated one


def test_retagged_tags_equal_a_forced_full_retag(repo):
    """Skipping never changes an answer: what an incremental run leaves is what a full run produces."""
    ids = _three(repo)
    _settle(repo)

    def tags():
        with sqlite3.connect(repo.path) as connection:
            return sorted(connection.execute("SELECT * FROM content_teams").fetchall()
                          + connection.execute("SELECT * FROM content_players").fetchall()
                          + connection.execute("SELECT * FROM content_topics").fetchall(), key=repr)

    incremental = tags()
    repo.retag(2026, now=NOW + timedelta(days=1), force_all=True)
    assert tags() == incremental and len(incremental) >= len(ids)
