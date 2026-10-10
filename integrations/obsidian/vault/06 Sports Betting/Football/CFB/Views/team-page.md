---
type: cfb-team
team_name: __TEAM_JSON__
season: __SEASON__
conference:
tags:
  - football/cfb/team
---

# __TEAM_NAME__

[[06 Sports Betting/Football/CFB/CFB Dashboard|CFB Dashboard]] · [[06 Sports Betting/Football/CFB/CFB Setup Guide|Instructions]]

> Notes below are pulled from matchups. Edit the source matchup to change a note. Set `season` to a year, or leave it blank for all seasons.

## Current assessment

### Offense

| + | - | Evidence / condition | Review / expires |
| --- | --- | --- | --- |
| | | | |

### Defense

| + | - | Evidence / condition | Review / expires |
| --- | --- | --- | --- |
| | | | |

### Context

| + | - | Evidence / condition | Review / expires |
| --- | --- | --- | --- |
| | | | |

## Carry-forward watchlist

Rows marked `active` or `watch` in this team's matchup notes. A dated review condition is flagged when due; rows remain visible until you change their status in the source.

```dataviewjs
await dv.view("06 Sports Betting/Football/CFB/Views/cfb-team", {mode: "active"});
```

## Offense — all matchup notes

```dataviewjs
await dv.view("06 Sports Betting/Football/CFB/Views/cfb-team", {mode: "offense"});
```

## Defense — all matchup notes

```dataviewjs
await dv.view("06 Sports Betting/Football/CFB/Views/cfb-team", {mode: "defense"});
```

## Context — all matchup notes

```dataviewjs
await dv.view("06 Sports Betting/Football/CFB/Views/cfb-team", {mode: "context"});
```

## Game history and full game context

```dataviewjs
await dv.view("06 Sports Betting/Football/CFB/Views/cfb-team", {mode: "games"});
```
