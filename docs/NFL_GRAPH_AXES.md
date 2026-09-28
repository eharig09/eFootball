# NFL team and player graph axes

The weekly graph workbench permits any number of selected series provided they
occupy no more than two compatible axes. Compatibility means both the same unit
and a comparable game-level range; sharing a Python or database type is not
enough. This prevents a 35-attempt series from flattening a two-touchdown
series even though both are integer counts.

## Team axes

| Axis | Metrics |
|---|---|
| Points | Point margin, points scored, points allowed |
| EPA / play | Overall, dropback, and rush EPA/play on offense; overall, dropback, and rush EPA/play allowed |
| Rate | Offensive and defensive success rate; offensive and defensive explosive rate |

## Player axes

| Axis | Metrics |
|---|---|
| Yards | Passing, passing air, rushing, receiving, receiving air, and yards after catch |
| Total EPA | Passing, rushing, and receiving EPA |
| Opportunities | Attempts, completions, carries, targets, receptions, and rushing first downs |
| TDs / turnovers | Passing/rushing/receiving touchdowns and passing interceptions |
| Percentage | CPOE, aggressiveness, and stacked-box percentage |
| Seconds | Time to throw |
| Tracking yards | Rush yards over expected, receiver separation, cushion, and YAC over expected |
| Defensive events | Solo/assisted tackles, QB hits, sacks, tackles for loss, interceptions, passes defended, and forced fumbles |

The metric picker displays each series' axis family. Once two families are in
use, metrics from either family remain selectable and metrics requiring a third
axis are disabled. Removing the last series from one family makes third-family
choices available again. Tooltip formatting remains metric-specific; tick
formatting belongs to the shared axis family.

Axis assignments live in `sports_aggregator/nfl/charts.py`. New metrics must be
assigned there rather than receiving an implicit scale. Create a new family
when either the unit or practical weekly range is incompatible with every
existing family, and update this inventory with the same change.
