# Obsidian football game notebook

The main workflow is one prose note per watched game, shared between two team pages. CFB and NFL use the same layout. Run **CFB/NFL Game Notes Template** from the corresponding dashboard, import a specific historical/current game URL or ID, select existing imported research, or create a manual game.

- `Games/`: normal writing under Game context and each team's Offense, Defense, Context headings. No observation form, table editing, betting decision, or publication fields are required.
- `Teams/`: reads that team's full text from each game with the opponent, date, role, and shared context. It never copies notes or silently assigns legacy research to a historical game.
- `Matchups/`: imported source data and earlier research. Game notes link here for an optional folded comparison. Refreshing a source preserves authored film prose.
- `Observations/`, `Snapshots/`, and Writing Desk: earlier workflows remain available; legacy observations are shown once in a folded section on the relevant team page.

The Game Notes Template uses the existing importer for JSON retrieval, canonical identity mapping, quote provenance, partial-source retention, and concurrency checks. It opens the simple game note after Templater finishes. Enter a specific game URL/ID to avoid treating an upcoming schedule entry as the game watched. For comparison, the preferred or first supplied quote is used with its source label. No bet is created.

Site comparisons are cached and show their import date. Historical imports can contain retrospective information; these are distinct from the user's tape observations. NFL spreads are converted to home-spread convention, and book-unspecified schedule prices remain labeled. Follow the live site link for current information.

Install the league folders under `06 Sports Betting/Football` and the matching templates under `Bins/Templates`. Requirements: Templater, Dataview with JavaScript enabled, and Bases for the optional legacy grids. Film helpers and views are under each league's Views folder. Configuration stays in Views/engine-config.json. Run commands from the dashboard to avoid Templater overwriting an active game editor.

Existing user prose and Properties must survive a migration. Only stock generated team-page sections should be replaced; custom team writing belongs in a preserved block. No old observations, matchups, or snapshots need deletion.

Validation: `python3 -m unittest discover -s tests -p test_obsidian_integration.py` runs the CFB/NFL importer contracts, observation-button checks, and game-note workflow tests. Native Obsidian rendering needs a desktop check.
