---
type: nfl-dashboard
season: 2026
---

# NFL Game Notebook

**Start here:** Ctrl+P → Templater: Open insert template modal → **NFL Game Notes Template**.

Choose **Select team → season → game** to search a team and browse its full stored schedule, including completed and upcoming games. You can also paste a game URL/ID, choose existing research, or create a manual game. Write directly in that game's note. No separate observation form or table is required.

[[06 Sports Betting/Football/NFL/NFL Film Notes Guide|How to use game notes]]

## Games

```dataview
TABLE WITHOUT ID file.link AS Game, away_team AS Away, home_team AS Home, game_date AS Date
FROM "06 Sports Betting/Football/NFL/Games"
WHERE type = "nfl-game" AND (!this.season OR season = this.season)
SORT game_date DESC
```

## Teams

```dataview
LIST
FROM "06 Sports Betting/Football/NFL/Teams"
WHERE type = "nfl-team"
SORT team_name ASC
```

> [!abstract]- Earlier research and optional writing workflow
> Existing matchup research, observations, snapshots, and blog fields remain available.
> [[06 Sports Betting/Football/NFL/NFL Writing Desk|Writing Desk]] · [[06 Sports Betting/Football/NFL/NFL Observations.base|Earlier observations]]
>
> ```dataview
> LIST
> FROM "06 Sports Betting/Football/NFL/Matchups"
> WHERE type = "nfl-matchup"
> SORT game_date DESC
> ```
