# CFB engine → Obsidian

Import the existing Football Lab JSON endpoints directly from Obsidian. No
server deployment, Python process, credentials, or new Obsidian plugin is needed.
Requires the vault's existing **Templater** and **Dataview** plugins, with Dataview
JavaScript queries enabled.

## Installation

Copy the contents of `vault/` into the Obsidian vault root, merging folders:

- `Bins/Templates/`: five Templater templates, including Engine Import and Freeze.
- `06 Sports Betting/Football/CFB/`: dashboard, instructions, team blueprint,
  local creation/import helpers, and shared Dataview views.

No team or matchup notes are included in the installer, so existing research is
not replaced. Preserve custom `Views/engine-config.json` settings when updating.
If templates live elsewhere in your vault, place the five templates in your
configured Templater folder. The data/view path stays as shown above.

## Use

1. Open **CFB Dashboard**.
2. **Ctrl+P → Templater: Open insert template modal → CFB Engine Import Template**.
3. Paste an engine game-page URL/ID, or leave blank to select an upcoming game.
4. Select a sportsbook. Map an unfamiliar team name to an existing page if needed.
5. The new or refreshed matchup opens after Templater finishes.

Run the same command again to refresh. The importer identifies games by canonical
ID, with an exact team/season/week match for existing manual notes. It refuses
ambiguous duplicates, wrong-team/season targets, malformed import markers, and
concurrent note edits rather than overwriting unrelated text.

Use **CFB Freeze Pregame Template** from the dashboard before kickoff to preserve
the whole note (including your reasoning) in `Snapshots`. It does not fetch new
data; refresh first. Snapshots have a different type and are excluded from team
queries. This is a user-invoked importer, not a scheduled background task.

## Data and refresh behavior

Required: `/api/v1/cfb/games/<id>` (identity, schedule, venue, neutral flag, basic
metrics). Optional requests run independently:

- `/preview`: quality/metrics, trailing offensive/defensive inputs, projections.
- `/situation`: per-book opening/current spread and total, moneylines, quote
  timestamp, schedule context, availability and forecast.
- `/content`: attributed reporting layers with original source URLs and dates.

The default engine address is `https://cfb-intelligence.onrender.com`. Configure
the root URL, local game-date timezone, and preferred sportsbook in
`Views/engine-config.json`. `tp.obsidian.requestUrl` avoids browser CORS restrictions.
Only GET requests are made; vault notes and decisions never leave the device.

Manual note sections and existing quoted-market fields remain intact. Latest
quotes use `engine_*` properties and Imported game context. No consensus book is
substituted. Missing spread/total juice stays unknown; the `price` field is never
guessed. Blank initial market fields are filled only when the initial sportsbook
is blank or agrees with the selected provider.

Imported fact rows have stable IDs. Evidence refreshes, while user edits to +, -,
carry-forward status, and review condition persist. Older rows/reporting are
retained by ID, with their source dates. They must be reviewed for continued
relevance. Raw metrics are neutral evidence (not automatic + or - signals).
Availability defaults to `watch`, retains the reporting role and publication
date, and does not turn questionable into confirmed out. Shared reporting stays
at game level because the current content endpoint does not include reliable
per-item team ownership. Neutral-site travel estimates based on home campuses are
omitted. Imports after kickoff are labeled retrospective.

Optional endpoint failures produce a partial-import label and retain existing
blocks from the missing endpoint. The required game request must succeed before
any writes. Imported block delimiters are reserved for refresh; write personal
analysis in the manual tables. Existing user assessments and team season filters
are preserved.

## Verification

From the repository root:

```sh
node --test integrations/obsidian/tests/engine.test.cjs
python -m unittest discover -s tests -p test_obsidian_integration.py
```

The tests use a mocked Obsidian runtime and the live API's field shapes. They
exercise manual-note and quote preservation, stable-ID deduplication/lifecycle
edits, partial failures, missing prices, neutral venues, timezone dates, canonical
identity checks, team-note compilation, pregame snapshots, and end-to-end imports.
Final display and command behavior should also be checked in desktop Obsidian.


Use **CFB Edit Notes Template** from the dashboard to add or edit observations in
a native form. Manual evidence is editable; imported evidence is read-only while
direction, carry-forward and review edits persist. No additional plugin is needed.
The helper refuses concurrent changes and excludes snapshots from selection.

Imported displays round PPA/explosiveness to at most two decimals; pace, yards,
percentages and projections to one; temperature/wind to whole numbers. Market
quotes and Properties retain precision. Fetch/forecast times disappear from the
visible sections; news/input dates remain. Exact times remain in Properties or
hidden provenance comments. Retained blocks are cleaned on refresh as well.
