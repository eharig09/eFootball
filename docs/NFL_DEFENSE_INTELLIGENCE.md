# NFL defensive intelligence

The team and matchup pages use one defensive data contract so the same number
cannot acquire a different definition on a different surface.

## Evidence families

| Family | Definition |
|---|---|
| Traditional | Opponent team box scores grouped by the defense faced: points, pass/rush yards, completion rate, yards per attempt/carry, first downs, touchdowns, and giveaways. Credited sacks, QB hits, tackles for loss, and passes defended come from the defending team's own weekly row. |
| Efficiency | Opponent offensive nflfastR plays grouped by defense: overall, dropback, and rushing EPA/play; success rate; explosive-play rate. |
| Next Gen allowed | Public offensive NGS results grouped by `opponent_team`. These are the volume-weighted results opponents produced against the defense, not private defender-tracking measurements or inferred coverage assignments. |
| PFF | Snap- or opportunity-weighted linked player grades. These are application-computed unit summaries, not official PFF team grades. A family is omitted when its licensed export is unavailable. |

All current-season matchup values are filtered to weeks before the selected
game. When either club lacks a pregame efficiency baseline, the matchup page
uses the prior season consistently for both teams. PFF may use the latest
linked comparison season and is labeled separately.

## Advantage rules

Offense and defense receive league quality ranks with the direction normalized
per metric. Lower allowed production is good defense; more offensive production
is good offense; fewer turnovers or sacks allowed are good offense; more
takeaways or sacks generated are good defense. A displayed advantage requires
at least five ranks of separation. Twelve or more ranks is styled as a strong
separation.

The comparison is descriptive. It is not a calibrated expected value, does not
adjust for opponent strength, and is not currently an NFL Engine feature. Any
future model use should be introduced through a separately versioned feature,
walk-forward validation, and the forecast ledger.

## Extension rules

1. Add new raw fields to the repository aggregation first.
2. State whether high or low is favorable before ranking the metric.
3. Match an offensive stat only to the same defensive outcome definition.
4. Preserve source and season labels; do not fill a missing PFF/NGS value with
   a similarly named traditional statistic.
5. Require a stable sample denominator before adding rate metrics.
