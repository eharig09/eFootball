# Data refresh audit (2026-10-05)

Evidence: 121 local scheduled runs (`instance/scheduled_refresh_history.jsonl`, Aug 24 - Oct 5),
170 run logs, the NFL refresh history, the production status pages and APIs
(`/api/v1/cfb/status`, `/college-football/data-status/`, `/api/v1/nfl/data-status`), and the
orchestration code. The production run logs and `/internal/*` status endpoints need the refresh
token, so nothing below was read from them.

There are two separate refresh systems, and most confusion comes from treating them as one:

| | Local (your PC) | Production (Render) |
|---|---|---|
| Trigger | Windows task, 4x/day | ~12 crons POSTing to the web service |
| Shape | one `heavy` run, all 52 steps in sequence | hour-slotted segments, run as child processes of the web process |
| Constraint | the PC being awake | one small instance, one shared lock, memory shared with gunicorn |

## What was wrong, in order of impact

### Production

1. **A skipped segment waited a day.** `_segment_for_light` picked the segment from the clock hour
   (`models` only at 23:00, `analytics` only at 02:00, ...). If the shared lock was held at that
   tick the run exited "skipped" and nothing retried until the same hour tomorrow. Production shows
   `projections` last completed 3 days ago (its cron fires every 2 hours) and `models` 4 days ago.
   **Fixed:** a light-hour tick now runs the most overdue segment when the segment that owns the hour
   is not itself overdue (`SEGMENT_MAX_AGE_HOURS`, based on when each segment last ran, any outcome).
2. **NFL segments overlapped and were killed.** `/internal/nfl-refresh` spawns a child per trigger
   with no lock. Production history shows four segments launched within 7 minutes at 20:31 UTC; two
   (`core-stats`, `core-depth`) still read "running" six hours later: killed mid-run, most likely by the
   platform out-of-memory killer. **Fixed:** NFL segments now take a lock, queue up to 3 minutes, and
   otherwise record `skipped`; a dead holder's lock is reclaimed.
3. **Every busy tick spawned a full interpreter to find the lock held.** The CFB hook ran
   `Popen(tracked_refresh)` unconditionally; with ticks every 15 minutes during games that is a
   memory spike each time, on an instance that has already been OOM-killed. **Fixed:** the hook checks
   the lock first and returns `skipped` without spawning.
4. **The status page said "unknown" for healthy segments.** `_data_status_packet` read only the last
   20 history rows; `availability` writes two rows every 15 minutes, so the window was ~2.5 hours and
   `core-stats`, `core-depth`, `core-pbp`, `weather`, `pff` (3-12 hour cadences) fell out of it.
   A killed run was invisible too. **Fixed:** health is computed from the whole (now bounded) history,
   and a run that started and never finished is reported `interrupted`, with the last success shown.
5. **`/api/v1/cfb/status` `last_sync` was frozen at Aug 27** while data updated daily: only the old
   monolithic sync writes `sync_runs`; the per-dataset refresh never does. **Fixed:** the payload now
   carries a `refresh` roll-up (last run, status, last success per segment). `last_sync` is unchanged
   and should be ignored.
6. **Failures were unlabelled.** The step message was the last line of output, so a step that printed a
   coverage summary after dying, or a library notice after a traceback, read as "unknown" with nothing to
   act on (e.g. `nfl-core-stats`/`depth`, and `reddit-validate` showing "WisconsinFootball: verified" as
   its failure). **Fixed:** a failed step reports its last error-looking line; `OpenBLAS error` and
   `memory allocation` now classify as `resource`.

### Local

7. **CFBD rate limiting failed one step in a third of runs.** `cfbd-current-player-stats` failed in 32 of
   121 runs, almost always on the same three conferences (Independents, MAC, Mountain West). The log shows
   HTTP 429: once CFBD starts refusing, every call in the window fails (the MAC and MWC calls fail in 4 s
   right after Independents burns 125 s of retries). **Fixed:** failed conferences get one retry after a
   90 s cool-down, and the step message now names the cause.
8. **`reddit-validate` crashed with `AttributeError`** (`r.requested_handle`; the object has
   `endpoint_key`) whenever any endpoint was unreachable, turning a working step into a failure.
   **Fixed.**
9. **The test suite wrote fake runs into the real history.** `tests/test_nfl.py` calls `refresh_cli.main`
   without a database path, so ~1,270 of the 3,321 rows in `instance/nfl_refresh_history.jsonl` were test
   artifacts, including all 88 "weather failures" (`RuntimeError: boom`, 0.0 s) and 90 runs that "never
   finished". **Fixed:** `tests/conftest.py` points the refresh state at a temp directory. The existing
   polluted rows were not deleted (see "For you to decide").
10. **Runs were missed.** 121 ran vs ~170 expected, gaps up to 30 hours. The task has `WakeToRun` off and
    only runs while you are logged in, so a sleeping or logged-out PC skips runs and `StartWhenAvailable`
    catches up only one. That matches the gaps, though the history cannot say which cause produced each.
    **Fixed in the script** (`scripts/register_refresh_task.ps1`: network required, two retries 10 minutes
    apart, 3-hour execution cap instead of 72, optional `-WakeToRun`); it takes effect when you re-run it.
11. **Dead-process detection never worked on Windows.** `os.kill(pid, 0)` returns without error for a dead
    pid there, so a killed run's lock blocked the next run until the stale window passed. **Fixed** with a
    real `OpenProcess` probe (`sports_aggregator/process_probe.py`); the previously skipped test now runs.
12. **Unbounded history.** `nfl_refresh_history.jsonl` grew ~220 rows/day and is parsed by the status
    page. **Fixed:** trimmed to the last 4,000 rows once it exceeds 1.5 MB.

## Retag (addressed 2026-10-06)

`retag` re-resolved topic, team, player, game and conference tags for every stored item on every run
(44,890 items, 389 s and growing with the corpus). Findings from a copy of the real database:

- A full retag changed the tags of **zero** items that had been settled and not re-ingested since,
  in every age bucket (~37,000 items). Re-resolving them was entirely redundant.
- The only differences were items *re-seen by ingestion* (5-7%). Every ingest path upserts the item
  (`ON CONFLICT DO UPDATE ... ingested_at=...`) and then re-tags it from `title + description[:1500]`,
  overwriting the canonical tags retag had written from the full text. Retag then repaired them.
  So the two steps were undoing each other; `ingested_at` is "last seen", not "first ingested"
  (7,702 items carried today's date after one refresh).
- Retag is deterministic: re-resolving the same items twice changes nothing.

Policy now (`social/content.py`, notes at `RETAG_*`):

| Item | Re-resolved when |
|---|---|
| Tagged by ingestion only (new, or content changed) | next retag, whatever its age: its first full-text pass |
| Settled, content unchanged | never by ingestion (it now leaves them alone); by retag only if the rules changed (fingerprint of the classifier, the tagging source files and the team aliases), and only if published within 120 days |
| Published within 7 days | once a day (tags older than 20 h), so roster/schedule changes reach them |
| Published over 120 days ago | frozen: a rule change is not worth re-reading them |

`retag --all` forces everything; `--active-days` / `--archive-days` adjust the windows. Age is the
publication date, not `ingested_at`. Editing `content.py`, `sport.py` or `context.py` changes the
fingerprint, so the archive is re-resolved once after any tagging change without anyone bumping a
version (and once after this change ships, to build the new per-item state).

## Roster sync (addressed 2026-10-06)

`cfbd-sync` launched one interpreter per team (138). Runs alternated between real fetches (230-530 s)
and cache hits (65-85 s), and even a pure cache hit spent 60-80 s on process start-up. Rosters now sync
in one process (`dataset_cli players --all-teams`) with the same per-team calls and failure isolation,
and rate-limited teams are retried once after a cool-down.

## Planner statistics (addressed 2026-10-06)

`build-tendencies` went from 15-30 s to ~200 s on every run since Sept 28, and five other steps from
~3 s to ~20 s, while production (one season of plays) stayed at 20 s. Every one of them starts from
`cfb_play_metrics` via `idx_cfb_play_metrics_version`: the planner scans all 1.77M plays of every season
and filters to the requested one afterwards. Cause: `optimize()` runs `ANALYZE` with
`analysis_limit=400`, and sampling an index whose column has a single value records "401 rows per key",
so the planner believes the index is selective. The true figure is every row.

- `optimize()` now fully re-analyzes any table whose statistics carry that signature (19 tables, ~16 s
  once per data change). On a copy of the live database the unchanged steps dropped from 10.3 / 4.1 /
  3.3 s to 1.3 s each.
- `build-tendencies` additionally pins its join order (plays first), which does not depend on statistics:
  46.7 s -> 4.2 s on the copy with an identical result (digest of all 17,551 rows unchanged).
- Local only in effect: production holds one season, so the scan is cheap there. The fix is harmless
  there and keeps it so as history is added.
- Other queries that begin `WHERE <version column> = ?` joined to a large table are exposed to the same
  trap if statistics ever regress; `tests/test_planner_statistics.py` pins the repair.

## NFL core steps dying in production (investigated 2026-10-06)

Production showed `nfl-core-stats`, `nfl-core-depth` and `nfl-core-pbp` as "unknown" failures for four
runs. Its NFL refresh history shows each step writing a "running" row and never a result, repeatedly:
the process was killed, not failed (a Python exception would have been recorded). Findings:

- **`core-depth` was the memory hog.** The 2026 depth charts are 613,196 rows: 99 MB as a table, but
  `frame.to_dict("records")` makes ~650 MB of Python dicts and the insert built a second copy, so the
  step peaked at ~1 GB (measured). The table grows every week, which fits steps that worked until
  about four days ago and then started dying. They are now streamed in 20,000-row chunks and inserted
  per chunk: peak **1,018 MB -> 267 MB** with an identical stored result (612,495 rows, same digest).
  The other datasets are a few thousand rows each (under 110 MB to convert) and were not changed.
- **Children get allocator settings that bound virtual reservations** (`ARROW_DEFAULT_MEMORY_POOL=system`,
  `MALLOC_ARENA_MAX=2`, single-threaded BLAS/OpenMP). The address-space ceiling counts virtual memory,
  and pandas/pyarrow reserve far more than they use; glibc arenas can only be capped from the
  environment the process starts with, which is why this is set where the child is launched.
- **A failed step now explains itself.** The NFL CLI printed `weekly_stats: failed (0)` and dropped the
  exception text, and the status page showed whatever the log said last (a coverage summary). Dataset
  failure reasons are now printed, each failed step's own last 20 lines are stored with the issue, and the
  Needs-attention card shows them under "Step output".
- **Not established:** why `core-stats` (225 MB peak locally) and `core-pbp` fail. They succeed locally with
  fresh nflverse data, so it is not the data. The next production failure will carry its own output.
- **The NFL cron segments may not be running.** `core-stats` / `core-depth` last succeeded 3 days ago and
  `core-pbp` 44 hours ago, although their crons are every 3 hours; the only recent starts are the clustered
  trio the CFB `analytics` segment launches. If the Render dashboard has no `nfl-core-stats`,
  `nfl-core-depth` or `nfl-core-pbp` refresh-trigger services (the Blueprint does not always sync new
  services to an existing deployment), the daily analytics pass is the only thing updating NFL stats.
  That is also why those steps must not be removed from `ANALYTICS_STEPS` yet.

## Not fixed: recommendations

- **Local runs are slowing down: average 741 s in August, 1,426 s in September, 1,908 s in October.**
  Three causes, all addressed below ("Retag", "Roster sync", "Planner statistics"); the remaining
  visible cost is `bluesky` (29 -> 115 s, more endpoints) and the content fetches themselves.
- **Production OpenBLAS failures in `nfl-core-pbp`.** The thread env vars are already forced in
  `refresh_cli`. The remaining candidate is the address-space ceiling: `RLIMIT_AS` (1,200 MB for NFL steps)
  bounds virtual memory, which pandas/pyarrow reserve far beyond what they use. I could not read the
  production log tail (token required), so this is a hypothesis; the tail of that step's log settles it.
- **The CFB `analytics` segment also runs the four `nfl-core-*` steps** that NFL's own crons already run,
  so their failures degrade the CFB segment and the work is done twice. Remove them from `ANALYTICS_STEPS`
  once you have confirmed the NFL crons for those segments exist in the Render dashboard (the Blueprint
  does not always sync new services to existing ones).
- **`render.yaml` says `plan: starter`;** `bootstrap.py` documents an upgrade to 1 CPU / 2 GB. If the
  dashboard still says Starter (512 MB), the 1,200 MB child ceiling is larger than the machine.
- **Local `heavy` runs everything every 6 hours** (52 steps, 30+ minutes) while production runs bounded
  segments. Moving local onto the same segment schedule would keep the two honest and shorten each run.
- **Both playoff forecasts compute on the first request after data changes** (4-10 s cold). Adding them to
  the `projections` segment removes that from a visitor's request.

## For you to decide

- Delete the test-pollution rows from `instance/nfl_refresh_history.jsonl`? (They are the rows with
  `seconds == 0.0` or `error == "boom"`; a backup first is cheap.) Until then the local NFL status page
  overstates failures.
- Re-run `scripts/register_refresh_task.ps1` to apply the new task settings, and decide whether to pass
  `-WakeToRun` (it wakes the PC overnight).
