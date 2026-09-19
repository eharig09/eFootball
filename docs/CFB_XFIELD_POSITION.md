# Special Teams and Starting Field Position (Milestone 10)

This increment measures field position and special teams without converting
them directly into points. It follows the roadmap's intended chain:

`Expected Starting Field Position -> Expected Yards To Goal -> later Drive EP`

## Provider constraint and reconstruction

CFBD kickoff and punt rows carry the **pre-kick** spot. Treating that value as
the receiving team's start would produce systematically wrong field position.
`team_game_special_teams.py` instead takes the first competitive scrimmage snap
of the receiving possession. This naturally includes the return, coverage, and
accepted penalties without parsing inconsistent play prose.

For punts, net distance is reconstructed as:

`punter yards-to-goal + receiving start yards-to-goal - 100`

The team-game table stores:

- overall offensive starting yards to goal;
- starts after kickoffs and punts;
- net punt distance and punts pinning the opponent inside its 20;
- the opponent's starting position after each team's punts and kickoffs; and
- field-goal attempts, makes, approximate distance (`yards_to_goal + 17`), and
  accuracy.

The 2022-2025 build produced 7,318 team-game rows.

## Leak-safe baselines

`xfieldposition.py` uses the same 12-game, recency-weighted, strictly pregame
window as the preceding projection layers. All rate/average calculations weight
their underlying events and exposures rather than averaging game percentages.

### Overall starting yards to goal (6,997 eligible team-games)

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | **4.76952** | **6.29053** | -0.40098 |
| Team history | 5.02923 | 6.65518 | -0.28014 |
| Offense/defense blend | 4.77949 | 6.30982 | -0.21226 |

### Starting yards to goal after punts (6,634 eligible team-games)

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | **8.23328** | **11.02772** | -0.37527 |
| Return-team history | 8.68472 | 11.65799 | -0.18022 |
| Return/punt-team blend | 8.31012 | 11.19965 | +0.00525 |

### Field-goal accuracy (5,634 eligible team-games with attempts)

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | **0.30272** | **0.35737** | +0.00044 |
| Team history | 0.30813 | 0.38463 | -0.00004 |

### Net punt yards (6,809 eligible team-games)

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| League prior | **5.97855** | **9.04127** | -0.41299 |
| Team history | 6.27802 | 9.46263 | -0.06582 |

These are in-sample component diagnostics, not held-out performance claims.
They provide no evidence that recent team-level results should displace league
priors. The live projection therefore serves the winning league estimates and
retains team/matchup estimates as diagnostic fields. A future personnel-aware
model can test whether current punter/kicker identity, distance buckets, weather,
or returner usage earns a real adjustment.

## Live output

The matchup projection now includes:

- expected starting yards to goal;
- expected starting yards to goal after a punt;
- expected field-goal make rate; and
- expected net punt distance.

For the 2026-09-19 Georgia at Arkansas smoke test, the pregame league environment
was 70.1 starting yards to goal, 72.2 after punts, 75.6% field-goal accuracy,
and 38.2 net punt yards. Matchup diagnostics remain available in the API but do
not override those better-performing priors.

## Files and commands

```
sports_aggregator/cfb/team_game_special_teams.py
sports_aggregator/cfb/xfieldposition.py
tests/test_xfieldposition.py
docs/CFB_XFIELD_POSITION.md
```

CLI commands:

```
build-team-special-teams
build-xfieldposition-dataset
evaluate-xfieldposition-baselines
```

Milestone 11 is next: use field position and the existing event-aligned expected
points machinery to estimate drive outcome probabilities and complete expected
team points without collapsing the system into a black-box score model.
