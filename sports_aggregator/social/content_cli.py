"""Background-style CFB social ingestion command."""

from __future__ import annotations

import argparse
from concurrent.futures import (
    ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed)
import threading
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from sports_aggregator.social.bluesky import BlueskyIdentityClient
from sports_aggregator.social.media import MediaRegistry
from sports_aggregator.social.reddit import RedditContentClient
from sports_aggregator.social.content import ContentRepository
from sports_aggregator.social.stories import StoryRepository
from sports_aggregator.catalog import get_league
from sports_aggregator.service import build_default_service


#: Virtual address space each worker thread reserves for its stack.
#:
#: The refresh runs its steps under an RLIMIT_AS ceiling, and that limit counts
#: *address space*, not resident memory. glibc reserves 8 MB per thread stack,
#: so an eight-worker pool asks for 64 MB of address space it will never touch.
#: With the ceiling already accounted for by real data, local-articles died on
#: `RuntimeError: can't start new thread` while resident memory sat at 273 MB,
#: comfortably inside the 320 MB cap. Half a megabyte is ample for fetching and
#: parsing a feed, and drops the pool's reservation from 64 MB to 4 MB.
WORKER_STACK_BYTES = 512 * 1024


#: Share of endpoints that may fail before a step is reported as failed.
#:
#: Feeds and social endpoints are flaky in ordinary operation: a publisher
#: rotates a URL, a host times out, an account goes private. Treating any error
#: as failure meant a step that reached 328 of 350 feeds and stored 1,808
#: articles reported exactly like one that died on its first line, so
#: "degraded" stopped carrying information — three steps sat in that list every
#: run while the refresh was working. A step now fails when it did not do its
#: job, not when the internet was imperfect.
ENDPOINT_FAILURE_TOLERANCE = 0.2


#: How long ingest-local-reporting waits for its feeds before keeping what it has.
#:
#: Three hundred and fifty Google News feeds behind eight workers, against an
#: aggregator that throttles. A heavy refresh spent 1800 of its 2580 seconds
#: here and stored nothing, because the driver killed the process rather than
#: the step deciding it had waited long enough.
LOCAL_REPORTING_DEADLINE = 420.0

#: Feeds fetched at once by ingest-local-reporting.
#:
#: Eight got 268 of 350 refused with 503 on the instance. The aggregator is
#: throttling the burst, so the way through it is to ask for less at a time and
#: let the retry backoff carry the rest. The deadline is what makes that safe:
#: going slower can no longer mean running until something kills it.
LOCAL_REPORTING_WORKERS = 4

MAX_TOLERATED_FAILURE_SHARE = 0.25


def _step_exit_code(*, attempted: int, errors: int, stored: int) -> int:
    """0 when the step did its job, 1 when it genuinely did not.

    Failure means one of two things: nothing was stored despite work being
    available, or more than a quarter of the endpoints failed. Everything else
    is partial success, and the printed counts already say how partial.
    """
    if attempted <= 0:
        return 0
    if errors >= attempted:
        return 1
    if stored <= 0:
        return 1
    return 1 if (errors / attempted) > MAX_TOLERATED_FAILURE_SHARE else 0


def _reporting_exit_code(feeds, errors, stored: int) -> int:
    """Exit code for the RSS reporting step.

    Newsletter feeds (source_type "analysis") are extras that publishers' bot defences often refuse from a datacenter
    address; losing them is not a failed step, so only the news-desk feeds decide it. Their errors stay in the
    recorded run and the printed counts.
    """
    extras = {feed.name for feed in feeds if feed.source_type == "analysis"}
    return _step_exit_code(attempted=len([feed for feed in feeds if feed.name not in extras]),
                           errors=len([error for error in errors if error.source not in extras]), stored=stored)


def _use_small_thread_stacks() -> None:
    """Ask for modest thread stacks, where the platform allows it."""
    try:
        threading.stack_size(WORKER_STACK_BYTES)
    except (ValueError, RuntimeError):
        # Some platforms enforce a larger minimum; the default still works when
        # no address-space ceiling is in force.
        pass


def main(argv=None) -> int:
    _use_small_thread_stacks()
    load_dotenv(); parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=("ingest","ingest-reddit","ingest-youtube",
                                          "ingest-podcasts","ingest-reporting",
                                          "ingest-local-reporting",
                                          "prune-local-non-football",
                                          "retag","roles","score","status","cluster",
                                          "review-export","review-import","review-report")); parser.add_argument("--season",type=int,default=datetime.now().year)
    parser.add_argument("--limit",type=int,default=15)
    parser.add_argument(
        "--workers", type=int, default=LOCAL_REPORTING_WORKERS,
        help="ingest-local-reporting: feeds to fetch at once. Fewer is slower "
             "and less likely to be throttled.")
    parser.add_argument(
        "--deadline", type=float, default=LOCAL_REPORTING_DEADLINE,
        help="ingest-local-reporting: stop waiting for feeds after this many "
             "seconds and keep what arrived. 0 waits indefinitely.")
    parser.add_argument("--input")
    parser.add_argument("--output",default="instance/cfb_content_review.csv")
    parser.add_argument("--reviewer",default="local")
    parser.add_argument("--review-mode",choices=("triage","stratified"),default="triage")
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args(argv)
    repository=ContentRepository(os.getenv("CFB_DATABASE_PATH","instance/cfb.sqlite3"))
    if args.command.startswith("review-"):
        from sports_aggregator.social.review import ContentReviewRepository
        reviews = ContentReviewRepository(repository.path)
        if args.command == "review-export":
            report = reviews.export_csv(Path(args.output), limit=args.limit,
                                        reviewer=args.reviewer, mode=args.review_mode)
        elif args.command == "review-import":
            if not args.input:
                parser.error("review-import requires --input")
            report = reviews.import_csv(Path(args.input), reviewer=args.reviewer)
        else:
            report = reviews.report(reviewer=args.reviewer)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command=="retag":
        report=repository.retag(args.season)
        print(" ".join(f"{key}={value}" for key,value in report.items()))
        print(" ".join(f"{key}={value}" for key,value in repository.rescore().items()))
        return 0
    if args.command=="prune-local-non-football":
        report=repository.prune_local_non_football(dry_run=args.dry_run)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command=="roles":
        report=repository.redetermine_roles()
        print(" ".join(f"{key}={value}" for key,value in report.items()))
        return 0
    if args.command=="score":
        print(" ".join(f"{key}={value}" for key,value in repository.rescore().items()))
        return 0
    if args.command=="cluster":
        report=StoryRepository(repository.path).rebuild(); print(" ".join(f"{key}={value}" for key,value in report.items()))
        return 0
    if args.command=="status":
        report = repository.summary()
        report["recent_items_returned"] = len(repository.recent(min(max(args.limit, 1), 100)))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command=="ingest-reddit":
        from sports_aggregator.social.team_reddit import load_registry
        from sports_aggregator.social.unified import UnifiedSourceRegistry
        UnifiedSourceRegistry(repository.path).seed_team_reddit_communities(load_registry())
        endpoints=repository.reddit_endpoints()
        if not endpoints:
            print("no verified subreddit endpoints"); return 1
        client=RedditContentClient(); seen=stored=succeeded=0; errors=[]
        started=datetime.now(timezone.utc).isoformat()
        for endpoint in endpoints:
            try:
                submissions=client.submissions(endpoint["handle"],limit=args.limit)
                succeeded+=1; seen+=len(submissions)
                stored+=sum(repository.store_reddit_submission(endpoint,item,args.season) is not None
                            for item in submissions)
            except Exception as exc:
                errors.append({"endpoint":endpoint["endpoint_key"],"error":str(exc)})
        repository.record_run(started,datetime.now(timezone.utc).isoformat(),
                              len(endpoints),succeeded,seen,stored,errors,platform="reddit")
        print(f"subreddits={len(endpoints)} succeeded={succeeded} seen={seen} stored={stored} errors={len(errors)}")
        return _step_exit_code(attempted=len(endpoints), errors=len(errors), stored=stored)
    if args.command=="ingest-youtube":
        from sports_aggregator.providers.youtube import YouTubeDataClient
        registry=MediaRegistry(repository.path); endpoints=registry.youtube_endpoints()
        if not endpoints:
            print("no verified YouTube endpoints; run the media validator first"); return 1
        client=YouTubeDataClient(); seen=stored=succeeded=0; errors=[]
        started=datetime.now(timezone.utc).isoformat()
        for endpoint in endpoints:
            try:
                videos=client.uploads(endpoint["platform_id"],max_results=args.limit)
                succeeded+=1; seen+=len(videos)
                stored+=sum(repository.store_youtube_video(endpoint,video,args.season) is not None
                            for video in videos)
            except Exception as exc:
                errors.append({"endpoint":endpoint["endpoint_key"],"error":str(exc)})
        repository.record_run(started,datetime.now(timezone.utc).isoformat(),
                              len(endpoints),succeeded,seen,stored,errors,platform="youtube")
        print(f"channels={len(endpoints)} succeeded={succeeded} seen={seen} stored={stored} errors={len(errors)}")
        return _step_exit_code(attempted=len(endpoints), errors=len(errors), stored=stored)
    if args.command=="ingest-podcasts":
        from sports_aggregator.providers.podcast import PodcastRSSClient
        registry=MediaRegistry(repository.path); endpoints=registry.podcast_endpoints()
        if not endpoints:
            print("no verified podcast endpoints; run the media validator first"); return 1
        client=PodcastRSSClient(); seen=stored=succeeded=0; errors=[]
        started=datetime.now(timezone.utc).isoformat()
        for endpoint in endpoints:
            try:
                episodes=client.episodes(endpoint["platform_id"],limit=args.limit)
                succeeded+=1; seen+=len(episodes)
                stored+=sum(repository.store_podcast_episode(endpoint,episode,args.season) is not None
                            for episode in episodes)
            except Exception as exc:
                errors.append({"endpoint":endpoint["endpoint_key"],"error":str(exc)})
        repository.record_run(started,datetime.now(timezone.utc).isoformat(),
                              len(endpoints),succeeded,seen,stored,errors,platform="podcast")
        print(f"feeds={len(endpoints)} succeeded={succeeded} seen={seen} stored={stored} errors={len(errors)}")
        return _step_exit_code(attempted=len(endpoints), errors=len(errors), stored=stored)
    if args.command=="ingest-reporting":
        started=datetime.now(timezone.utc).isoformat()
        result=build_default_service().aggregate(get_league("college-football"),force_refresh=True)
        stored=sum(repository.store_article(article,args.season) is not None for article in result.articles)
        errors=[{"source": error.source, "error": error.message} for error in result.errors]
        repository.record_run(
            started,datetime.now(timezone.utc).isoformat(),len(result.league.feeds),
            len(result.league.feeds)-len(errors),len(result.articles),stored,errors,
            platform="rss",
        )
        # Story clustering is its own step (`cluster`), which runs right after
        # this one in every content pass. Rebuilding 21 days of clusters a
        # second time here was the memory spike that made `articles` fail with
        # a MemoryError on the constrained instance.
        print(f"articles={len(result.articles)} stored={stored} errors={len(result.errors)}")
        for error in result.errors:
            print(f"  {error.source}: {str(error.message)[:200]}")
        return _reporting_exit_code(result.league.feeds, result.errors, stored)
    if args.command=="ingest-local-reporting":
        from sports_aggregator.models import FeedConfig
        from sports_aggregator.providers.rss import RSSNewsProvider
        from sports_aggregator.social.local_sources import article_matches_team, _publisher_id
        registry_path=Path("data/local_sources/cfb_local_source_registry.json")
        if not registry_path.exists():
            print("local source registry is missing; run local_sources_cli research"); return 1
        registry=json.loads(registry_path.read_text(encoding="utf-8")); tasks=[]
        for team in registry["teams"].values():
            for source in team["sources"]:
                publisher_id=_publisher_id(source["domain"])
                tasks.append((team,source,FeedConfig(
                    name=source["name"],url=source["google_news_rss"],
                    max_articles=args.limit,source_type="local_reporting",
                    reliability=4 if source["confidence"]=="high" else 3,
                    source_entity_key=f"local-publisher:{publisher_id}",
                    source_endpoint_key=f"rss:google-news:{publisher_id}:{team['team_id']}",
                )))
        errors=[]; succeeded=0; combined={}; team_ids={}; abandoned=0
        started=datetime.now(timezone.utc).isoformat()
        def fetch_local(task):
            team,source,config=task
            return team,source,RSSNewsProvider(config).fetch()
        # Three hundred and fifty feeds against one aggregator, which throttles.
        # Without a deadline this ran until the refresh driver killed the whole
        # process at thirty minutes, and everything fetched up to that point was
        # lost with it: seventy per cent of a heavy refresh for nothing stored.
        # It now stops waiting, keeps what arrived, and says what it gave up on.
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures={pool.submit(fetch_local,task):task for task in tasks}
            pending=set(futures)
            try:
                for future in as_completed(futures, timeout=args.deadline or None):
                    pending.discard(future)
                    team,source,_=futures[future]
                    try:
                        _,_,articles=future.result(); succeeded+=1
                        # Process each completed feed immediately instead of
                        # holding every response until the slowest one ends.
                        for article in articles:
                            if not article_matches_team(
                                f"{article.title} {article.summary}", team,
                                publisher=article.publisher or source["name"],
                            ):
                                continue
                            combined.setdefault(article.identity,article)
                            team_ids.setdefault(article.identity,set()).add(int(team["team_id"]))
                    except Exception as exc:
                        errors.append({"team":team["team"],"domain":source["domain"],"error":str(exc)})
            except FuturesTimeoutError:
                abandoned=len(pending)
                for future in pending:
                    future.cancel()
                errors.append({"team":"-","domain":"-",
                               "error":f"deadline of {args.deadline:g}s reached with "
                                       f"{abandoned} feeds outstanding"})
        stored=0
        for identity,article in combined.items():
            repository.store_article(replace(article,team_ids=tuple(sorted(team_ids[identity]))),args.season)
            stored+=1
        repository.record_run(
            started,datetime.now(timezone.utc).isoformat(),len(tasks),succeeded,
            len(combined),stored,errors,platform="rss-local",
        )
        print(f"feeds={len(tasks)} succeeded={succeeded} abandoned={abandoned} "
              f"articles={len(combined)} stored={stored} errors={len(errors)}")
        # A count alone says a quarter of the feeds worked without saying why
        # the rest did not, and the reasons are only in the database. The
        # distinct ones are few even when the count is in the hundreds.
        if errors:
            tally: dict[str, int] = {}
            for error in errors:
                tally[str(error["error"])[:120]] = tally.get(str(error["error"])[:120], 0) + 1
            print("error kinds:")
            for message, count in sorted(tally.items(), key=lambda item: -item[1])[:5]:
                print(f"  {count:>4}  {message}")
        # Feeds abandoned at the deadline are not failures to fetch; they are
        # feeds that were never reached. Judging the run on what it attempted
        # would call a healthy partial pass a failure.
        return _step_exit_code(attempted=len(tasks) - abandoned,
                               errors=len(errors) - (1 if abandoned else 0),
                               stored=stored)
    started=datetime.now(timezone.utc).isoformat(); endpoints=repository.bluesky_endpoints()
    client=BlueskyIdentityClient(); succeeded=seen=stored=0; errors=[]
    def fetch(endpoint):
        return endpoint, client.author_feed(endpoint["platform_id"],args.limit)
    fetched=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures={pool.submit(fetch,endpoint):endpoint for endpoint in endpoints}
        for future in as_completed(futures):
            endpoint=futures[future]
            try:
                fetched.append(future.result())
            except Exception as exc:
                errors.append({"endpoint":endpoint["endpoint_key"],"error":str(exc)})
    for endpoint,feed in fetched:
        try:
            succeeded+=1; seen+=len(feed)
            stored+=sum(repository.store_bluesky_post(endpoint,item,args.season) is not None for item in feed)
        except Exception as exc:
            errors.append({"endpoint":endpoint["endpoint_key"],"error":str(exc)})
    finished=datetime.now(timezone.utc).isoformat()
    repository.record_run(started,finished,len(endpoints),succeeded,seen,stored,errors)
    print(f"endpoints={len(endpoints)} succeeded={succeeded} seen={seen} stored={stored} errors={len(errors)}")
    return _step_exit_code(attempted=len(endpoints), errors=len(errors), stored=stored)


if __name__=="__main__": raise SystemExit(main())
