# NFL production allowed by position group

The NFL team and matchup pages measure the production a defense concedes to
wide receivers, tight ends, and running backs. Each group begins with a true
group total and then expands into observed workload roles:

- WR1 through WR4
- TE1 through TE3
- QB rushing plus RB1 through RB3 (RB roles include players sourced as FB or HB)

## Role assignment

These labels are observed roles, not claims about an official depth-chart
designation. Within the selected data window, players are ordered by total
opportunities (targets plus carries), then scrimmage yards. The game-page
window ends before kickoff, preventing the game being previewed from changing
its own WR/RB/TE ordering. Group totals retain every player, including players
beyond the displayed depth slots.

Quarterbacks never consume an RB depth slot. All quarterback carries, rushing
yards, and rushing touchdowns are included in the backfield group total and
combined into a separate `QB rush` sub-row; QB passing production is not part
of this ledger.

This choice resolves a structural problem in the provider depth feed: an NFL
formation can list several simultaneous first-team wide receivers, so its raw
position rank does not uniquely identify one WR1, WR2, or WR3.

## Measures

Every row reports per-defense-game targets, receptions, receiving yards,
carries, rushing yards, scrimmage yards, and touchdowns. Three indices provide
context:

- **vs NFL**: production allowed divided by the league per-game allowance for
  that group/role. The WR and TE index uses receiving yards; RB uses scrimmage
  yards.
- **Opponent quality**: the expected allowance based on what each opponent
  produced in its other games, divided by league average.
- **Adjusted**: actual allowance divided by that opponent-quality expectation.

All indices use 100 as average/expected. Because these are defensive allowance
measures, lower is better. An adjusted value of 82 means the defense allowed
18% less than the opponents' other-game production predicted; 118 means 18%
more.

The opponent baseline is leave-one-matchup-out to prevent circular grading. If
an opponent has no other game in the selected window, the league baseline is
used rather than treating the missing sample as zero. Matchup pages use only
weeks before the selected game. When the application intentionally falls back
to a prior-season baseline, it uses that completed season without a current-
week cutoff.

## Sources and limits

The ledger uses nflverse weekly player statistics stored in
`player_weekly_stats`. It does not infer coverage responsibility or claim that
a specific defender surrendered a result. Injuries and in-season role changes
can shift an observed role; the named matchup player is therefore presented as
a workload identity, not an official depth-chart ruling.
