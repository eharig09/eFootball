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
