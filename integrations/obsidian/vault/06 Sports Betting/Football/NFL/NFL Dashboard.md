---
type: nfl-dashboard
season: 2026
---

# NFL Dashboard

[[06 Sports Betting/Football/NFL/NFL Setup Guide|How to create a matchup]]

## Import or refresh from the engine

From this dashboard, press **Ctrl+P → Templater: Open insert template modal → NFL Engine Import Template**. Paste a game URL/ID or leave blank to select an upcoming game. Choose the supplied quote source; verify the bookmaker before acting. The matchup opens after import.

To save your pregame analysis, run **NFL Freeze Pregame Template** from this dashboard. Snapshots include your manual notes and cannot be created after kickoff.

## Observations

[[06 Sports Betting/Football/NFL/NFL Observations.base|Open the editable observations grid]]

Use the **Add observation** buttons in matchup/team sections in Reading view. The form records team, section, direction, evidence, carry-forward status, and review date/condition. **Edit** opens the same form; **Evidence / full note** opens the complete observation.

Command alternative: **Ctrl+P → Templater: Open insert template modal → NFL Observation Template**. **NFL Edit Notes Template** is an alias for the same workflow.

```dataviewjs
await dv.view("06 Sports Betting/Football/NFL/Views/nfl-observation-view", {active: true});
```

For older notes, run **NFL Organize Existing Notes Template** from this dashboard once. It preserves the source rows, converts authored observations without duplicates, and installs the compact layout. Engine refresh uses the compact layout automatically.

## Writing and review

[[06 Sports Betting/Football/NFL/NFL Writing Desk|Open the Writing Desk]] for next checks, unresolved predictions, selected evidence, and article stages.

## Action queue

```dataview
TABLE game_date AS Date, away_team AS Away, home_team AS Home, line AS "Home spread", total AS Total, price AS Price, next_action AS "Next action", review_date AS Review
FROM "06 Sports Betting/Football/NFL/Matchups"
WHERE type = "nfl-matchup" AND (!this.season OR season = this.season) AND status != "reviewed" AND status != "pass"
SORT game_date ASC
```

## All matchups

```dataview
TABLE game_date AS Date, away_team AS Away, home_team AS Home, line AS "Home spread", total AS Total, sportsbook AS Book, status AS Status
FROM "06 Sports Betting/Football/NFL/Matchups"
WHERE type = "nfl-matchup" AND (!this.season OR season = this.season)
SORT game_date DESC
```

## Teams

```dataview
TABLE team_name AS Team, season AS "Page season filter", conference AS Conference, division AS Division
FROM "06 Sports Betting/Football/NFL/Teams"
WHERE type = "nfl-team"
SORT team_name ASC
```

## Pregame snapshots

```dataview
TABLE snapshot_at AS Saved, source_matchup AS Matchup
FROM "06 Sports Betting/Football/NFL/Snapshots"
WHERE type = "nfl-pregame-snapshot"
SORT snapshot_at DESC
```
