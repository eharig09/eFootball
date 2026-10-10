# CFB research in Obsidian

Import Football Lab's existing CFB JSON API into linked matchup and team pages,
read compact numerical comparisons, and manage meaningful observations through a
native capture form and editable Obsidian Bases grids. Engine routes and deployment
settings do not change. The integration makes GET requests only and never uploads
vault content.

## Installation

Merge `vault/` into the vault root. Preserve custom `Views/engine-config.json`
settings and existing team/matchup notes. The installer contains no real team,
matchup or observation data.

- `Bins/Templates/`: seven Templater templates (creation, import, freeze,
  observation capture/edit, and legacy organization).
- `06 Sports Betting/Football/CFB/`: dashboard, setup guide, shared helpers,
  team blueprint and Bases views.

Requires existing Templater and Dataview plugins, Dataview JavaScript queries,
and the core Bases plugin. No additional community plugin is required. If the
configured template folder differs, move the seven templates there; keep the
CFB helper/view path as installed.

## Use

1. Open **CFB Dashboard**.
2. Run **Templater: Open insert template modal → CFB Engine Import Template**.
3. Enter a game URL/ID or select an upcoming game, then select a sportsbook.
4. Read numerical offense-versus-defense comparisons. Season metrics and trailing
   inputs have separate tables with sample/season labels. Conditions and projection
   appear once; all source blocks are preserved in folded native callouts.
5. Use **Add observation** in Reading view, or **CFB Observation Template**.
   Capture team, section, direction, evidence, carry-forward status and review.
6. Use **CFB Observations.base** to edit management fields directly. Team and
   matchup pages also embed filtered grids. Open a note for full Markdown evidence.
7. Freeze the complete note and observation text with **CFB Freeze Pregame Template**
   before kickoff. Refresh first if newer evidence is wanted.

**CFB Edit Notes Template** is an alias for observation capture/edit. Run
**CFB Organize Existing Notes Template** from the dashboard to convert personally
authored legacy rows repeatably. Original rows are preserved; converted copies
are excluded from legacy team displays. Neutral imported metrics/headlines do not
become personal observations automatically. These are on-demand commands, with no
scheduled refresh.

## Data, relevance and preservation

Required: `/api/v1/cfb/games/<id>`. Optional independent endpoints: `/preview`,
`/situation`, `/content`. The default engine is
`https://cfb-intelligence.onrender.com`; config includes root URL, timezone and
optional preferred sportsbook. `tp.obsidian.requestUrl` avoids browser CORS.

Imports preserve manual analysis, existing team assessments/season filters, and
initial quoted market fields. Latest quotes use `engine_*` properties and game
conditions. Unknown spread/total juice stays blank. Neutral-site campus-travel
assumptions are omitted. Wrong identity, ambiguous duplicates, damaged markers
and concurrent note changes stop writes. Optional failures retain older sections;
post-kickoff imports are labeled retrospective.

Current-headline screening requires both canonical team names and publication
within seven days before the game. It indicates a review candidate, not confirmed
player availability, subject-team ownership or accuracy. Aliases, older reports
and uncertain matches remain in the complete archive. Stable IDs deduplicate
source records; no original record is deleted by the relevance screen.

Display rounding affects contextual statistics/projections only. Quotes and
numerical Properties retain precision. Fetch/forecast times disappear from the
visible sections; publication/input dates and hidden provenance remain available.

Observations are individual Markdown notes with team, opponent, originating
matchup, season/week/date, category, direction, carry/review and quoted game context
Properties. Evidence/Follow-up remain full Markdown prose. Team views show full
observation text with origin links; neutral raw metrics no longer fill insight
lists. Positive/negative directions concern team performance, not automatic bets.

Snapshots contain full observation Properties and text, with live queries/grids
removed. Later edits do not affect them. Snapshots are excluded from current
team and observation datasets and cannot be created after stored kickoff.

## Verification

```sh
node --test integrations/obsidian/tests/engine.test.cjs
python -m unittest discover -s tests -p test_obsidian_integration.py
```

Contract tests cover imports and preservation, partial failures, source retention,
relevance screening, numerical presentation, forms/cancellation/concurrency,
repeatable legacy conversion, team/category/season filters, and frozen atomic
observations. Bases files are valid YAML using the documented schema. A genuine
live packet/source note was reorganized with a full YAML parser, preserving every
source block. Final native UI/render behavior needs a desktop Obsidian smoke check.
