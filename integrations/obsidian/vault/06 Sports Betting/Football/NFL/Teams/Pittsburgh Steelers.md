---
type: nfl-team
engine_team_id: "PIT"
team_name: "Pittsburgh Steelers"
season: 2026
conference: "AFC"
division: "AFC North"
tags:
  - football/nfl/team
---

# Pittsburgh Steelers

[[06 Sports Betting/Football/NFL/NFL Dashboard|NFL Dashboard]] · [[06 Sports Betting/Football/NFL/NFL Setup Guide|Instructions]]

> Notes below are pulled from matchups. Use Add/Edit observation, or edit the observation note Properties. Set `season` to a year, or leave it blank for all seasons.

## Current assessment

### Offense

- Assessment:
- Evidence to revisit:

### Defense

- Assessment:
- Evidence to revisit:

### Context

- Assessment:
- Evidence to revisit:

## Carry-forward watchlist

Observations marked `active` or `watch` for this team. A dated review condition is flagged when due; rows remain visible until you change their status in the source.

```dataviewjs
await dv.view("06 Sports Betting/Football/NFL/Views/nfl-team", {mode: "active"});
```

## Offense — all matchup notes

```dataviewjs
await dv.view("06 Sports Betting/Football/NFL/Views/nfl-team", {mode: "offense"});
```

## Defense — all matchup notes

```dataviewjs
await dv.view("06 Sports Betting/Football/NFL/Views/nfl-team", {mode: "defense"});
```

## Context — all matchup notes

```dataviewjs
await dv.view("06 Sports Betting/Football/NFL/Views/nfl-team", {mode: "context"});
```

## Game history and full game context

```dataviewjs
await dv.view("06 Sports Betting/Football/NFL/Views/nfl-team", {mode: "games"});
```


## Manage observations in an editable grid

![[06 Sports Betting/Football/NFL/Views/team-observations.base]]
