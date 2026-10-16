---
type: cfb-dashboard
season: 2026
---

# CFB Game Notebook

**Start here:** Ctrl+P → Templater: Open insert template modal → **CFB Game Notes Template**.

Choose **Select team → season → game** to search a team and browse its full stored schedule, including completed and upcoming games. You can also paste a game URL/ID, choose existing research, or create a manual game. Write directly in that game's note. No separate observation form or table is required.

[[06 Sports Betting/Football/CFB/CFB Film Notes Guide|How to use game notes]]

## Games

```dataview
TABLE WITHOUT ID file.link AS Game, away_team AS Away, home_team AS Home, game_date AS Date
FROM "06 Sports Betting/Football/CFB/Games"
WHERE type = "cfb-game" AND (!this.season OR season = this.season)
SORT game_date DESC
```

## Teams

```dataview
LIST
FROM "06 Sports Betting/Football/CFB/Teams"
WHERE type = "cfb-team"
SORT team_name ASC
```

> [!abstract]- Earlier research and optional writing workflow
> Existing matchup research, observations, snapshots, and blog fields remain available.
> [[06 Sports Betting/Football/CFB/CFB Writing Desk|Writing Desk]] · [[06 Sports Betting/Football/CFB/CFB Observations.base|Earlier observations]]
>
> ```dataview
> LIST
> FROM "06 Sports Betting/Football/CFB/Matchups"
> WHERE type = "cfb-matchup"
> SORT game_date DESC
> ```
