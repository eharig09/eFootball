from __future__ import annotations
import argparse, json, os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv
from sports_aggregator.social.bluesky import BlueskyIdentityClient
from sports_aggregator.social.registry import SourceRegistry
from sports_aggregator.social.seeds import CFB_BLUESKY_SEEDS
from sports_aggregator.social.reddit import RedditCommunityClient
from sports_aggregator.social.unified import UnifiedSourceRegistry, migrate_bluesky_sources
from sports_aggregator.social.local_sources import import_source_graph
from sports_aggregator.social.team_reddit import load_registry, register
from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.social.content_cli import ENDPOINT_FAILURE_TOLERANCE

LOCAL_REGISTRY = Path("data/local_sources/cfb_local_source_registry.json")

def _import_local(unified, database):
    if not LOCAL_REGISTRY.exists(): return {"entities": 0, "endpoints": 0}
    return import_source_graph(json.loads(LOCAL_REGISTRY.read_text(encoding="utf-8")), database)

def _import_team_reddit(unified, database):
    entries=load_registry()
    if not entries: return {"registered": 0, "promoted": 0}
    registered=register(CFBRepository(database),entries)
    promoted=unified.seed_team_reddit_communities(entries)
    return {"registered": registered, "promoted": promoted}

def _label(resolution) -> str:
    """What to call an endpoint in a log line. `EndpointResolution` only carries `endpoint_key`."""
    return str(getattr(resolution, "requested_handle", None)
               or getattr(resolution, "endpoint_key", None) or "unknown")


def _endpoint_exit(results, *, kind: str) -> int:
    """Fail when resolution stopped working, not when one account is gone.

    The first version of this divided the failures by the number of handles
    attempted, which cannot work: the list handed to the resolver is the list
    of handles that are *not yet verified*, so it is by construction almost all
    failures. One dead seed among forty working ones came out as "100% failed"
    and marked every refresh degraded -- the very thing the tolerance was
    added to stop.

    What actually separates the two cases is the platform's own answer. A 4xx
    on resolveHandle means the handle is not a handle: it will fail on every
    run forever and no amount of retrying will change it. Anything else -- a
    timeout, a connection error, a 5xx -- means the platform is unreachable,
    and that is worth failing over even for a single handle, because it says
    nothing about whether the account exists.
    """
    total = len(results)
    if not total:
        print(f"{kind}: nothing to resolve")
        return 0
    failed = [r for r in results if r.status != "verified"]
    gone = [r for r in failed if getattr(r, "permanent", False)]
    unreachable = [r for r in failed if not getattr(r, "permanent", False)]

    print(f"{kind}: {total - len(failed)}/{total} verified"
          + (f", gone: {', '.join(_label(r) for r in gone[:5])}"
             f"{'...' if len(gone) > 5 else ''}" if gone else "")
          + (f", unreachable: {', '.join(_label(r) for r in unreachable[:5])}"
             f"{'...' if len(unreachable) > 5 else ''}" if unreachable else ""))
    if gone:
        print(f"{kind}: {len(gone)} handle{'s' if len(gone) != 1 else ''} no longer "
              "exist and will not resolve again -- remove or correct them in the "
              "seed list; not treated as a step failure")

    share = len(unreachable) / total
    if unreachable and share > ENDPOINT_FAILURE_TOLERANCE:
        print(f"{kind}: {share:.0%} of this run could not be reached, above the "
              f"{ENDPOINT_FAILURE_TOLERANCE:.0%} tolerance -- treating this as a failure")
        return 1
    return 0


def main(argv=None):
    load_dotenv(); p=argparse.ArgumentParser(); p.add_argument('command',choices=('seed','resolve','status','prepare','validate-reddit','unified-status')); p.add_argument('--force',action='store_true'); a=p.parse_args(argv)
    database=os.getenv('CFB_DATABASE_PATH','instance/cfb.sqlite3')
    registry=SourceRegistry(database); unified=UnifiedSourceRegistry(database)
    if a.command=='seed':
        seeded=registry.seed(CFB_BLUESKY_SEEDS); migrated=migrate_bluesky_sources(database)
        reddit=unified.seed_reddit_communities(); candidates=unified.seed_media_candidates()
        configured=unified.seed_configured_endpoints(); relationships=unified.infer_organization_relationships()
        local=_import_local(unified,database); team_reddit=_import_team_reddit(unified,database)
        print(f"bluesky_seeded={seeded} entities_migrated={migrated} reddit_seeded={reddit} team_reddit_registered={team_reddit['registered']} team_reddit_promoted={team_reddit['promoted']} media_candidates={candidates} configured_endpoints={configured} relationships_added={relationships} local_entities={local['entities']} local_endpoints={local['endpoints']}"); return 0
    if a.command=='prepare':
        migrated=migrate_bluesky_sources(database); reddit=unified.seed_reddit_communities()
        candidates=unified.seed_media_candidates(); configured=unified.seed_configured_endpoints()
        relationships=unified.infer_organization_relationships()
        local=_import_local(unified,database); team_reddit=_import_team_reddit(unified,database)
        print(f"entities_migrated={migrated} reddit_seeded={reddit} team_reddit_registered={team_reddit['registered']} team_reddit_promoted={team_reddit['promoted']} media_candidates={candidates} configured_endpoints={configured} relationships_added={relationships} local_entities={local['entities']} local_endpoints={local['endpoints']}"); return 0
    if a.command=='status':
        s=registry.status(); print(f"sources={s['count']} verified={s['verified']} failed_or_unresolved={s['failed']}"); return 0
    if a.command=='unified-status':
        s=unified.status(); print(json.dumps({k:v for k,v in s.items() if k!='entities'},indent=2)); return 0
    if a.command=='validate-reddit':
        team_reddit=_import_team_reddit(unified,database)
        print(f"team_reddit_registered={team_reddit['registered']} team_reddit_promoted={team_reddit['promoted']}")
        endpoints=unified.endpoints_by_platform('reddit'); client=RedditCommunityClient(); results=[]
        for endpoint in endpoints:
            result=client.resolve(endpoint['handle']); unified.store_endpoint_resolution(result)
            results.append(result); print(f"{endpoint['handle']}: {result.status}")
        return _endpoint_exit(results, kind='reddit')
    handles=registry.unresolved_handles(a.force); client=BlueskyIdentityClient()
    with ThreadPoolExecutor(max_workers=5) as pool: results=list(pool.map(client.resolve,handles))
    for result in results: registry.store_resolution(result); print(f"{result.requested_handle}: {result.status}")
    migrate_bluesky_sources(database); unified.seed_configured_endpoints()
    return _endpoint_exit(results, kind='bluesky')
if __name__=='__main__': raise SystemExit(main())
