# CFB notes — setup and workflow

## Create your first matchup

1. Wait for Google Drive to sync the new files into this vault.
2. Open the command palette (Ctrl+P) and choose **Templater: Create new note from template**.
3. Select **CFB Matchup Template**. Enter game date, season, week, away team, and home team. Pick existing teams whenever possible to avoid alternate spellings.
4. The note is named and moved into `06 Sports Betting/Football/CFB/Matchups`. Missing team pages are generated under `Teams` using the team page blueprint. Existing team pages are preserved.
5. Fill in the Properties at the top, then write in the team tables. Use Reading view or Live Preview to see the compiled team pages.

Your current Templater folder is `Bins/Templates`; both new templates are installed there. Dataview and DataviewJS are already enabled in the synced settings. If your local settings differ, enable Dataview's JavaScript queries and set Templater's template folder to the installed folder. No folder auto-template rules were changed.

To create a team page before a game, use **CFB Team Template** with the same command. For an existing empty note you can use **Templater: Open insert template modal**. Never insert these creation templates into a populated note.

## Market field definitions

| Field | Meaning / example |
| --- | --- |
| home_team / away_team | Full Obsidian links to team pages; generated automatically |
| season / week / game_date | Season year, week label, and actual game date |
| line | Numeric HOME spread: -3.5 = home favored; +3.5 = home underdog; 0 = pick'em |
| total | Numeric combined-points total, e.g. 48.5 |
| price | American odds for the market AND selection specified below, e.g. -110 |
| price_market | spread, total, moneyline, or other description |
| price_selection | Explicit selection, e.g. Michigan -3.5 or Under 48.5 |
| home_spread_price / away_spread_price | Separate odds for each spread side |
| over_price / under_price | Separate odds for each total side |
| home_moneyline / away_moneyline | Separate outright-win odds |
| sportsbook / market_timestamp | Book and exact time you observed the quote |
| neutral_site / venue / kickoff | Neutral-site flag, location, kickoff with time zone |
| game_context | Optional one-line context shown alongside every compiled note |
| status | research, watch, ready, bet, pass, or reviewed |
| next_action / review_date | What to check next, and when |
| home_score / away_score | Optional final results |

Leave unknown numbers blank; do not put '?' or 'TBD' in numeric properties. `line` has a single home-team sign convention; team pages automatically reverse it for away games. At neutral sites, use designated home/away and set neutral_site to true. Record the initial quoted market; keep later quotes and their timestamps in the game note instead of silently replacing the earlier price.

## + / - tables

Both teams have **Offense**, **Defense**, and **Context** sections. Each section has this table:

| + | - | Evidence / condition | Carry forward | Review / expires |
| --- | --- | --- | --- | --- |
| Hypothetical: pass protection improved | | Film / lineup evidence and opponent-quality caveat | watch | After next comparable opponent |
| | Hypothetical: thin secondary depth | Injury source and date | active | 2026-10-15 — check injury report |

Write one observation per row, usually in either + or -. + means favorable for the named team's performance; - means unfavorable. A strong defense is a team positive even if it might support an under. These are observations, not automatic wager instructions.

Carry-forward values:

- **game-only**: visible in history; excluded from the current watchlist.
- **watch**: tentative idea to test in future games.
- **active**: relevant beyond this game, subject to its stated conditions.
- **retired** or **superseded**: retained in history; excluded from the current watchlist.

A Review / expires value starting with YYYY-MM-DD is flagged REVIEW DUE on and after that date. It does not automatically retire the observation. Update its status after checking the evidence. Conditions such as 'when tackle returns' can be written as text.

Add table rows as needed. Use `<br>` for multiple sentences on different lines within a cell. Escape literal pipes as `\|`, including aliased wiki links inside tables (`[[Note\|Label]]`). Keep the invisible `<!-- cfb:... -->` markers surrounding the tables and game-context section: they identify exactly which team's content to pull. Notes outside these marked tables remain in the game note and are not included in the compiled note tables.

## Team pages

The top **Current assessment** tables are your manually curated season-level synthesis. Below them, the watchlist pulls active/watch rows; Offense, Defense, and Context pull ALL corresponding matchup rows for that team, including retired observations. Every compiled row retains its game link, opposing team link, venue role, team-relative line, total, optional one-line game context, evidence, and review condition. Game history also includes each game's full shared context and the quoted price/selection.

These are live read-only views: edit observations in the linked source matchup. They do not copy old observations into a new matchup or mix in the opponent's notes. Open each team page before researching the next game. Reopen/refresh the page after a source edit if needed; Dataview refresh is enabled.

Team pages default to the season used when first created. Change `season` for a new season, or clear it to see all seasons. Changing the filter preserves all old games. When starting a new season, refresh your manually curated Current assessment as well. The dashboard has its own season filter.

## Weekly rhythm

Before a game: review both team pages, fill market/game context, add matchup observations, and record the next action. Freeze your pregame conclusion with a timestamp. Afterward: add results and a postgame review, test the thesis against what happened, and update the carry-forward statuses. Set status to reviewed when complete.

## Files

- `Bins/Templates/CFB Matchup Template.md`: interactive game creation.
- `Bins/Templates/CFB Team Template.md`: standalone team creation.
- `06 Sports Betting/Football/CFB/Views/team-page.md`: shared team-page blueprint.
- `06 Sports Betting/Football/CFB/Views/cfb-create.js`: local template helper.
- `06 Sports Betting/Football/CFB/Views/cfb-team.js`: team compilation view.
- `06 Sports Betting/Football/CFB/CFB Dashboard.md`: game and action lists.

The creation and team-query helpers run locally. The engine importer makes read-only requests to the configured engine; it does not upload notes. Keep Views in this exact path. Avoid moving or renaming team pages after creating matchups unless Obsidian updates the links.


## Engine import and refresh

The engine connection is installed. Open **CFB Dashboard**, then use **Ctrl+P → Templater: Open insert template modal → CFB Engine Import Template**. Paste a game-page URL or numeric ID, or leave blank to select one of the next games returned by the engine. Choose a sportsbook. On first use, map unmatched team names to an existing page or create the engine's canonical team page. The matchup opens automatically when Templater finishes.

Run imports from the dashboard, not from the matchup being edited. The importer writes the matchup separately so Templater cannot overwrite its updates. Do not use Create new note from template for engine actions; that would leave an unnecessary empty note.

### What refresh preserves

- Your original Offense, Defense, Context, Game context, Decision, Postgame review, and Sources sections.
- Initial `line`, `total`, moneylines, sportsbook and timestamp once filled. Imported `engine_*` properties and Imported game context contain the latest selected-book quote. Refreshes do not change `price` or spread/total juice.
- On imported observation rows, your **+**, **-**, **Carry forward**, and **Review / expires** edits are preserved by Item ID. Evidence updates from the engine. To write your own interpretation/evidence, use the manual tables above the imported blocks.
- Previously imported evidence rows stay in the history even if they are no longer returned. Their original source/publication dates remain visible. Mark outdated carry-forward rows retired after review.
- Existing team-page assessments and season filters.

### Imported facts and attribution

Offense/Defense contain raw metrics and trailing snapshot inputs in Evidence / condition, with + and - initially blank. These are contextual evidence, not automatic betting edges. Availability items arrive as `watch`, with their source role and publication date and a game-day review date; questionable does not mean confirmed out. Schedule flags such as look-ahead are descriptions of the calendar, not claims about motivation. Shared reporting stays linked to the game because the current content API does not provide reliable per-item team ownership. Team pages show the game's reporting/context separately from their own team-specific notes.

The API does not supply spread/total juice; those fields stay blank. If your initial sportsbook differs from the selected provider, the importer does not fill initial market fields from the other book. It also omits home-campus travel estimates for neutral-site games.

If an optional endpoint fails, the note shows **Partial import**, and prior sections for that endpoint are retained. The required game endpoint must succeed before any import. Refreshing damaged or duplicated imported markers stops with an error instead of replacing unrelated text. Live data imported after kickoff is explicitly labeled retrospective and cannot become a pregame snapshot.

### Freeze before kickoff

From **CFB Dashboard**, run **CFB Freeze Pregame Template** and select the matchup. A new timestamped note in `Snapshots` preserves the entire current matchup, including your analysis and imported evidence. It never overwrites an existing snapshot and refuses after the stored kickoff. It freezes the current note; it does not fetch new data. Import/refresh immediately before saving when you want the latest available evidence. The original matchup can continue to refresh; the snapshot remains a separate historical record. Snapshots are excluded from team-note queries.

### Connection settings

`06 Sports Betting/Football/CFB/Views/engine-config.json` contains `api_url`, `timezone`, and optional `preferred_sportsbook`. Default API: `https://cfb-intelligence.onrender.com`. Timezone: `America/New_York`. Use the engine's HTTPS root address or an HTTP localhost root. No credentials are stored, and the importer never uploads your notes or betting decisions. The command runs only when invoked; this version does not schedule background refreshes.


## Easier note entry and cleaner numbers

Run **CFB Edit Notes Template** using **Templater: Open insert template modal** from the **CFB Dashboard**. Select a matchup and Add observation or Edit existing observation. A single form holds the team, Offense/Defense/Context, favorable and unfavorable observations, evidence, carry-forward status, and review date/condition. No pipe characters or Markdown rows to manage. Cancel leaves the note unchanged. Concurrent changes stop saving so another edit cannot be lost.

Imported rows let you edit +, −, carry-forward, and review; their evidence is read-only because the engine refreshes it. Add a manual observation for your own evidence or interpretation. The form changes matchup observations; the team page Current assessment remains a manually curated synthesis. Team compilation tables are read-only views of those saved observations. Run this command from the dashboard rather than inserting it into a matchup.

Imported displays use two decimal places at most for PPA and explosiveness, one for pace, yards/play, percentages, projected drives/plays/points, and whole numbers for temperature/wind. Trailing zeros are removed. Actual market quotes and numerical Properties keep their original precision. Repeated fetch and forecast times are removed from the visible imported sections; news and input dates remain for relevance checks. Exact times remain in Properties or hidden provenance comments. Refresh also cleans older retained imported sections.
