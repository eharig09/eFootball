# NFL storage migration

## Why this is required

The local NFL SQLite database measured 12,372,373,504 bytes on 2026-09-26,
against a 5 GB Render disk and a new 4,000,000,000-byte NFL budget. SQLite
reported only 174 free pages (712,704 bytes), so `VACUUM` alone cannot recover
meaningful space.

`player_weekly_stats` is the main amplification point:

| Season | Rows | Zero-valued rows | Zero share |
| --- | ---: | ---: | ---: |
| 2025 | 2,460,633 | 2,305,736 | 93.70% |
| 2026 | 290,542 | 272,467 | 93.78% |

Across 2010–2026 the table contains 36,408,599 rows. The current long-form
layout repeats season, week, game, player, team, opponent, position, and metric
text even when the value is zero. Four indexes repeat much of that identity
again.

## Target layout

Use two facts with different responsibilities:

1. `player_game_appearances` has one row per player/game. It preserves identity,
   participation, team, opponent, position, season type, and week. Game counts
   and game-log membership come from this table, so removing zero metric rows
   cannot incorrectly make an appearance disappear.
2. `player_game_metrics` stores only meaningful metric observations. Nonzero
   additive values are sparse. A zero is retained only where zero is an observed
   result with a nonzero denominator or explicit source presence, rather than a
   provider-filled default for an unrelated position.

Season aggregates should be materialized after a successful weekly refresh.
Dashboard leaders, player headline ranks, and the stat explorer should query
those aggregates instead of repeatedly pivoting millions of weekly rows.

Raw nflverse Parquet remains the immutable rebuild source. SQLite is the serving
store, not the only copy of historical source data.

## Safe rollout

1. Add versioned schema migrations for the appearance, sparse-metric, and
   season-aggregate tables.
2. Dual-write one current season while retaining `player_weekly_stats`.
3. Compare old and new packets for player totals, leaderboards, game logs,
   qualification games, and team/opponent filters.
4. Rebuild a fresh database from raw releases. Do not attempt an in-place
   `VACUUM` on the 12 GB local file; it requires substantial temporary space.
5. Switch reads behind `NFL_COMPACT_STATS_READS=1`, retaining a rollback flag.
6. Build the public Render seed from the compact database.
7. Remove the legacy table only after two successful refresh cycles and packet
   parity checks.

## Semantic rules

- Missing is not zero. A lineman with no passing statistic must remain absent
  from a passing scatter plot.
- Games played come from appearances, never from the presence of a requested
  metric.
- Rate statistics are recomputed from stored numerators and denominators.
- NGS weekly rates retain their volume-weighted numerator fields.
- Archived game pages retain their as-of-week cutoff behavior.
- Current-roster filtering continues to use GSIS IDs, not names.

## Acceptance criteria

- Compact 2010–current database below 3.5 GB.
- At least 70% reduction in `player_weekly_stats` replacement storage.
- Exact parity for additive player totals on a sampled season.
- Exact parity for appearance/game counts.
- Same top-ten ordering for every production leaderboard.
- No regression in the NFL CI suite.
- Dashboard and player-stat queries complete without scanning the legacy table.
- Migration and rebuild commands are restartable and record validation results.
