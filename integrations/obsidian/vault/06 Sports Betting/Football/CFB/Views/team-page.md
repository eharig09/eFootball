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


## Manage observations in an editable grid

![[06 Sports Betting/Football/CFB/Views/team-observations.base]]
