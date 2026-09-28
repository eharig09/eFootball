# NFL content source audit — 2026-09-28

## Decisions

The audit compared the configured directory with source checks, stored output,
and the last 30 Bluesky ingestion runs.

- Before changes: 158 Bluesky accounts; 146 had produced stored content.
- Six handles returned HTTP 400 in all 30 recent runs and never succeeded.
- Six additional accounts had no stored items during the audit window: three
  returned empty feeds and three returned only ineligible reply/repost-shaped
  items in the current sample.
- Those 12 accounts are disabled in
  `data/nfl/nfl_content_source_overrides.json`.
- `hawkblogger.com` was added as a team-scoped Seahawks reporting/film source
  after its public feed returned ten of ten original posts.
- Active Bluesky directory after reconciliation: 147 accounts.

The retired handles and reasons remain in the override manifest so the decision
is reviewable and reversible. Re-importing the directory now removes stale NFL
scope rows instead of leaving deleted accounts active in SQLite.

## RSS changes

Three permanent failures were disabled:

- FantasySP NFL Headlines — HTTP 404
- FantasySP NFL Player News — HTTP 404
- RotoViz — HTTP 403

Six keyless feeds were fetched and parsed successfully before being added:

| Source | Role |
|---|---|
| Yahoo Sports NFL | National reporting and injury/news aggregation |
| MatchQuarters | Defensive structure and film analysis |
| Unexpected Points | Advanced review and quantitative analysis |
| Sports Info Solutions | Charting-derived analysis |
| Sharp Football Analysis | Weekly analytical features |
| Over the Cap | Contracts, cap, and transaction context |

Reddit feeds remain configured, but their errors are tracked separately as
intermittent HTTP 429 throttling. They should not be treated as reporting or as
a dependable refresh dependency. If the rate persists, the next step is one
credentialed Reddit API collector with caching, not more concurrent `.rss`
requests.

## Additional accessible avenues

1. **Publisher and Substack RSS** — strongest no-key expansion path. Prefer the
   reporter/outlet's own feed over social repost accounts.
2. **Google News RSS discovery** — useful as a fallback for local newspapers
   without native RSS. Preserve the original publisher and canonical URL, and
   rank below native feeds.
3. **Bluesky public API** — use actor search only for discovery, then require a
   resolvable identity and a sample of original posts before activation.
4. **Podcast RSS** — accessible without platform credentials. Good for analysis
   and interviews, but episodes need transcription or strong descriptions to
   support player/game linking.
5. **YouTube channel feeds** — channel Atom feeds are keyless for new-upload
   discovery. The YouTube Data API remains better when quotas are available for
   richer metadata and stable channel resolution.
6. **Official transaction/injury endpoints** — ESPN's structured NFL injury
   endpoint and nflverse should remain the primary structured sources; team and
   league press releases are supporting reporting, not replacements.
7. **Credentialed local beat feeds** — the highest-value next expansion. Build
   one or two native/local feeds per team and use Google News RSS only where the
   publisher offers no feed.

Avoid scraping X timelines, paywalled article bodies, or sites whose robots or
access controls reject the collector. Links and permitted feed summaries are
sufficient for discovery and provenance.

## Durable expansion plan

Source expansion is paused after this audit. Resume it from this ordered
backlog rather than adding feeds opportunistically.

| Phase | Avenue | Deliverable | Admission gate | Operating rule |
|---|---|---|---|---|
| 1 | Local beat and native publisher RSS | Two independent reporting feeds per team, recorded in the source directory with team scope | Feed resolves; at least 5 of the latest 10 items are original, NFL-relevant reporting; canonical article URLs survive parsing | Refresh with the normal RSS cycle. Review teams with fewer than two producing sources monthly. |
| 2 | Publisher/Substack analysis feeds | National film, scheme, analytics, and cap coverage that fills a documented topic gap | Same 5-of-10 relevance gate; named author/outlet; no duplicate canonical URLs against active feeds | Add only when the source increases team or topic coverage. Quarterly yield review. |
| 3 | Google News RSS fallback | A discovery feed only for a team whose local publisher exposes no usable native feed | Query is team-specific; original publisher and canonical URL are retained; duplicates are suppressed | Rank below native feeds. Replace it when a first-party feed becomes available. |
| 4 | Podcast and YouTube discovery | Episode/upload metadata plus transcript text when legally and technically available | Stable publisher identity; descriptions or transcripts contain enough text for entity linking; no title-only analysis records | Store episodes as a distinct content type. Do not let them satisfy the two-reporting-source target. |
| 5 | Credentialed Reddit API | Cached community-sentiment input, separate from reporting | OAuth credentials, rate-limit handling, subreddit allowlist, and provenance fields are in place | Never block the reporting refresh. Label as community discussion and expire sentiment features quickly. |
| 6 | Bluesky discovery refresh | Candidates found through actor search and reporter/outlet cross-checking | Resolvable identity plus 5 original eligible posts in the latest 10 | Audit quarterly. Disable after 30 consecutive failed runs or a 30-run window with no eligible originals. |

The structured injury, transaction, roster, schedule, and stat collectors stay
outside this content-source backlog. ESPN/nflverse remain the primary data
feeds; reporting sources can explain those facts but do not overwrite them.

### Resume checklist and measures

1. Snapshot active, producing, failing, and team-scoped source counts before a
   phase starts.
2. Add candidates to the override manifest with discovery URL, scope, role,
   and admission evidence; never add an unreviewed URL directly to runtime
   configuration.
3. Run a sampled fetch, parser test, duplicate check, and entity-linking check
   before activation.
4. Observe three scheduled refreshes before considering a source established.
5. Track weekly eligible items, unique canonical URLs, team/topic coverage,
   error rate, and duplicate rate. A source is retained for coverage quality,
   not raw volume.
6. Stop a phase when its coverage target is met or when ten consecutive
   candidates fail the admission gate; record the gap and move to the next
   avenue instead of relaxing the gate.
