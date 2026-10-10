# CFB notes — workflow

## Start here

1. Open **CFB Dashboard** after your vault has synced.
2. Import a game using **Ctrl+P → Templater: Open insert template modal → CFB Engine Import Template**. Enter its engine URL/ID or select an upcoming game, then a sportsbook.
3. Read the offense-versus-defense comparisons, trailing inputs, game conditions, projections, and current-matchup headline candidates.
4. Use **Add observation** under the relevant team's **Offense**, **Defense**, or **Context** section. Save what the evidence means to you, its conditions, and whether it should carry forward.
5. Open each team's page to review its active/watch observations before researching the next game.

Templater templates live in `Bins/Templates`. Dataview and its JavaScript queries must be enabled. **Bases** is a core Obsidian plugin; it is already enabled in your synced settings. Use Reading view to display the custom views and Add/Edit buttons. No additional community plugin is required.

## Reading the matchup

Season metrics and trailing-sample inputs use separate compact numerical comparison tables. Shared descriptions of season, sample and source basis appear once. Havoc is shown separately, as supplied by the engine; avoid assuming offensive havoc has the same interpretation as defensive havoc. These are contextual measures, not opponent-adjusted betting edges.

Game conditions collect weather, travel, schedule flags and the latest selected-book market. The original line/total/price remain in Properties and the initial quote. Engine projections stay explicitly labeled and retain their model label.

**Current matchup reporting — verify** is a conservative headline screen: published within the seven days before the game and naming both canonical teams. It is not a confirmation of player availability or the subject team. Headlines using aliases may stay in the archive. Older, uncertain, duplicate-by-source-ID and loosely linked items remain accessible in **Full source archive**. Expand a section to read every imported row and its original link. No report is deleted by this screen.

Imported displays round PPA/explosiveness to at most two decimals; pace, yards, percentages and projections to one; temperature/wind to whole numbers. Actual quoted markets and numerical Properties retain precision. Repeated fetch/forecast times are removed from visible sections; news/input dates remain. Exact times remain in Properties or hidden provenance comments.

## Observations: write normally, manage in a grid

Each meaningful insight is a Markdown note in `06 Sports Betting/Football/CFB/Observations`. Imported metrics and articles do not each become notes automatically.

| Field | Meaning |
| --- | --- |
| observation | The full observation text, editable in the form or Bases |
| team / opponent / matchup | Links to the subject team, opponent and originating game |
| season / week / game_date / side | Originating game context |
| category | offense, defense, context |
| direction | positive, negative, neutral, mixed |
| carry | game-only, watch, active, retired, superseded |
| review | YYYY-MM-DD or a condition |
| context / venue / neutral_site | Game setting at capture time |
| team_line / total / price / sportsbook | Original quoted market context, copied from the originating game |

Evidence and Follow-up are ordinary Markdown sections in the note. Keep longer explanations, multiple source links and qualifications there. The form supports multiline input. You can also open the note and write normally.

**+** means favorable for the subject team's performance; **−** means unfavorable. A favorable defense can support an under. Direction is not a betting instruction. Use neutral for open questions.

- **game-only:** retained in history, excluded from the carry-forward watchlist.
- **watch:** tentative idea to test in later games.
- **active:** currently relevant beyond the originating game.
- **retired / superseded:** preserved in history, excluded from the watchlist.

A review value starting with YYYY-MM-DD is flagged REVIEW DUE on and after that date. It does not retire a note automatically. Retire or revise it after checking the evidence.

### Editing options

- **Add/Edit buttons:** open the capture form in Reading view. The section/team are preselected where possible. Matchup selection shows the full game name; choose Home or Away to identify the subject team.
- **CFB Observation Template:** command-palette alternative. Use **Templater: Open insert template modal** from a dashboard or matchup. **CFB Edit Notes Template** is an alias. Avoid creating a new note from these action templates.
- **CFB Observations.base:** editable management grid with Active and watch / All observations views grouped by team. Edit observation text, direction, section, status and review directly. Open the file-name column for full evidence.
- **Embedded grids:** each matchup has a folded grid for its own observations; each team page has a grid filtered to that team and its season.

Keep relational links, originating game fields and IDs intact. Use the form if changing an observation's team or section, so team/opponent links stay consistent. Do not use an action template inside the observation being edited: Templater could overwrite concurrent file edits. Use its Reading-view Edit button or normal Properties instead. Concurrent file changes stop the form save rather than losing another edit.

## Team pages and carry-forward notes

Each team page retains a manually curated **Current assessment**, followed by Active/watch, Offense, Defense, Context, game history, and the editable observation grid. Existing assessments are preserved. New team assessments use normal prose rather than manual tables.

Observation views include the originating matchup and opposing-team links. Team pages default to the season used when created; clear `season` for all seasons. Changing a filter does not delete old notes. Game history links to each matchup's complete evidence and source archive, rather than repeating that archive for every observation.

## Organize older notes

From **CFB Dashboard**, run **CFB Organize Existing Notes Template** using the insert-template command. It performs no API requests. It converts manually authored table rows and personally assessed imported rows into linked observation notes, preserves the original rows in folded sections, and installs the compact layout. It does not turn every neutral statistic or availability headline into a personal insight. Repeating the command does not duplicate converted observations. Pregame snapshots are excluded.

Refreshing an engine note installs the new layout automatically. Use the organizer to convert any earlier authored table observations. Legacy rows still display until converted; converted copies are not shown twice in team history.

## Import, refresh and attribution

The importer makes GET requests to the configured engine; it never uploads notes. Run import/refresh from the dashboard. The importer preserves manual Game context, Decision, Postgame review and Sources; quoted original market fields; authored observations; team assessments and season filters. Imported source blocks update by stable item IDs. Older evidence remains in the archive even when it is absent from the latest response.

The engine does not supply spread/total juice; these fields stay blank. It omits home-campus travel assumptions for neutral sites. Optional endpoint failure retains that endpoint's older sections and marks the import partial. Required game data must succeed before import. Damaged or duplicate markers stop refresh. Imported data after kickoff is labeled retrospective.

`Views/engine-config.json` controls HTTPS engine root, game-date timezone and optional preferred sportsbook. Default engine is `https://cfb-intelligence.onrender.com`, timezone `America/New_York`. Commands run on demand; no background schedule is installed.

## Market Properties

`line` is the HOME spread: -3.5 = home favored, +3.5 = home underdog. `total` is combined points. `price` is American odds for `price_market` plus `price_selection`. The sportsbook and exact quote time identify the original market. Separate home/away spread, over/under and moneyline fields remain available. Leave unknown numbers blank. `engine_*` market fields show the latest selected-book quote without changing the original quote.

`status`, `next_action`, and `review_date` drive the dashboard action queue. `home_score` / `away_score` record results. `game_context` is optional one-line context. `neutral_site`, `venue`, and kickoff record the setting.

## Freeze before kickoff

Run **CFB Freeze Pregame Template** from the dashboard before stored kickoff. Refresh first if you want the latest evidence. It preserves the complete matchup plus the full text and Properties of its observation notes. Live observation queries and the editable grid are removed from the frozen copy, so later edits cannot change the historical snapshot. Snapshots have a different type and are excluded from current team/observation queries. A saved snapshot is never overwritten.

## Create manual matchups or teams

Use **Templater: Create new note from template → CFB Matchup Template** for a new matchup, or **CFB Team Template** for a standalone team. These creation templates require an empty note. Existing team pages remain intact; prefer existing canonical names to alternate spellings.

Keep `Views` at its installed path. Preserve the `<!-- cfb:... -->` markers: they identify exactly which sections the importer and organizer may change.
