"""CLI for the NFL playoff simulator.

    python -m sports_aggregator.nfl.playoff_cli forecast [--season 2026] [--sims 10000]
    python -m sports_aggregator.nfl.playoff_cli backtest [--from-year 2015 --to-year 2025]
"""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.nfl.repository import NFLRepository


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="NFL playoff simulator")
    p.add_argument("command", choices=("forecast", "backtest"))
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--sims", type=int, default=10000)
    p.add_argument("--as-of-week", type=int, default=None)
    p.add_argument("--from-year", type=int, default=2015)
    p.add_argument("--to-year", type=int, default=2025)
    p.add_argument("--top", type=int, default=32)
    p.add_argument("--json", action="store_true")
    p.add_argument("--database", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = NFLRepository(args.database or os.getenv("NFL_DATABASE_PATH", "instance/nfl.sqlite3"))
    if args.command == "forecast":
        from sports_aggregator.nfl.playoff_service import build_forecast
        season = args.season
        if season is None:
            repository.initialize()
            season = repository.latest_season()
        out = build_forecast(repository, season, n_sims=args.sims, as_of_week=args.as_of_week,
                             use_cache=False)
        if args.json:
            print(json.dumps(out))
            return 0
        print(f"{out['season']} NFL playoff forecast, through week {out['as_of_week']}, "
              f"{out['n_sims']} sims, {out['games_remaining']} games left")
        print(f"{'Team':<6}{'Division':<12}{'Rec':<8}{'Rtg':>6}{'ProjW':>7}{'Div':>7}{'Playoff':>9}{'Bye':>7}{'SB':>7}{'Champ':>7}")
        for r in out["rows"][:args.top]:
            rec = f"{r['wins']}-{r['losses']}" + (f"-{r['ties']}" if r["ties"] else "")
            print(f"{r['team']:<6}{r['division']:<12}{rec:<8}{r['rating']:>6.1f}{r['projected_wins']:>7.1f}"
                  f"{r['division_title']:>7.1%}{r['playoff']:>9.1%}{r['bye']:>7.1%}"
                  f"{r['super_bowl']:>7.1%}{r['champion']:>7.1%}")
    else:
        from sports_aggregator.nfl.playoff_backtest import run
        print(json.dumps(run(repository, seasons=range(args.from_year, args.to_year + 1),
                             n_sims=min(args.sims, 2000)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
