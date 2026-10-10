---
type: nfl-guide
---

# NFL research setup

[[06 Sports Betting/Football/NFL/NFL Dashboard|NFL Dashboard]] · [[06 Sports Betting/Football/NFL/NFL Writing Desk|Writing Desk]]

## Start a matchup

From the NFL Dashboard, press **Ctrl+P → Templater: Open insert template modal → NFL Engine Import Template**. Paste an engine URL such as `/nfl/games/2026_05_CHI_GB/` or the complete game ID. Leave blank to enter the season and select an unfinished matchup from the engine's current slate. Games outside that slate can be imported directly by URL/ID.

Manual alternative: **Templater: Create new note from template → NFL Matchup Template**. Select existing teams or add one; fill market Properties afterward. NFL Team Template creates additional team pages. Thirty-two team pages are included; their season filter starts at 2026. Leave a team page's season blank to include all seasons.

Requirements: Templater, Dataview (JavaScript queries enabled), and the core Bases plugin. These are the same requirements as the CFB workflow. Helpers live in NFL/Views; keep that path stable. Engine address, timezone, and preferred quote-source label are in Views/engine-config.json.

## Market fields

`line` is the **HOME spread**: negative means home favored. The NFL feed stores the opposite sign, which the importer converts. `total` is combined points. `price` is American odds for `price_market` and `price_selection`; it is never inferred from another market. Imported spread, total, and moneyline prices have separate fields.

The current schedule market has **no sportsbook provenance**. It is labeled `nflverse schedule (book unspecified)` rather than borrowing the provider from a different line-movement dataset. Verify a real book's line and price before acting. Enter your actual sportsbook and quote in Properties; refresh preserves original quoted fields and stores the latest supplied values in engine_* fields. Blank initial market fields fill only when their source label matches. Do not relabel a feed quote as a bookmaker quote without checking it.

## Read and capture

The first section compares each offense against the opposing defense using season EPA, dropback/rush EPA, success rate, and explosive rate. Season pace, neutral pass rate, sample, and prior-season fallback are labeled separately. Values are rounded for reading; market fields remain exact. Timestamps stay in metadata, while imported reading sections show dates. Full sources, availability reports, article excerpts, and supplied model/personnel details stay in folded archives.

The engine's NFL JSON packet differs from CFB: it supplies combined game, profiles, projection, situation, availability, and content rather than separate preview endpoints. Missing sections are flagged; old evidence remains available. Dedicated weather forecasts may be unavailable. International venues receive a site-assumptions reminder even when the source's neutral-site flag is false. Reported injury/practice status is preserved and does not imply a confirmed inactive or betting direction.

Use **Add observation** in Reading view, or **NFL Observation Template** from the dashboard. Choose team, Offense/Defense/Context, and +/−/neutral/mixed; write full evidence and follow-up as prose. Team pages compile observations with the originating matchup, opponent link, and quoted game context. Carry-forward states: game-only, watch, active, retired, superseded. Use **Edit** or the editable **NFL Observations.base** grid; no Markdown table editing is needed.

## Act, review, and write

Optional observation fields: implication, applies_when, invalidated_by, next_check, resolution, publishable. Set a date or a condition for next_check. Add a clearly testable prediction and optional binary-event probability (0–100%), then record actual and prediction_result after the game. Prediction results are unresolved, held, failed, mixed, or not-testable; they are separate from a bet's win/loss.

Write the matchup's **Decision brief** in normal prose: thesis, strongest evidence, counterargument, unresolved information, explicit market conditions, pass conditions, and next check. Existing decision/postgame sections remain available.

Before kickoff, run **NFL Freeze Pregame Template** from the dashboard. It creates an independent snapshot containing the complete pregame note and linked observation Properties/evidence, replacing live queries with frozen content. Imports after kickoff are marked retrospective. Snapshot creation after kickoff is refused.

Open **NFL Writing Desk** for checks, unresolved predictions, selected evidence, and article stages. Set article_status to idea → outline → draft → published → reviewed; add reader_question, central_argument, publish_by, and published_url in Properties or **NFL Writing Pipeline.base**. Mark selected observations publishable, then expand **Blog draft** in a matchup for pregame/postgame outlines. Nothing is published automatically.

## Refresh and maintenance

Run import again with the same game ID from the dashboard. It updates marked engine evidence without replacing your assessments or authored prose. Concurrent edits cause a stop instead of an overwrite. Team abbreviation identities prevent duplicate mappings; resolve ambiguous mappings before proceeding.

**NFL Organize Existing Notes Template** upgrades typed NFL matchup notes and converts authored legacy table assessments without duplicating them. NFL Edit Notes Template is an alias for the observation workflow. Do not run import/freeze/organization from an active matchup editor: Templater can overwrite the note it is executing in.
