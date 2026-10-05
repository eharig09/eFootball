"""CLI for the 12-team playoff simulator.

    python -m sports_aggregator.cfb.playoff_cli forecast [--season 2026] [--sims 10000]
    python -m sports_aggregator.cfb.playoff_cli committee      # refit + leave-one-season-out accuracy
    python -m sports_aggregator.cfb.playoff_cli backtest       # calibration of past-season forecasts
"""
from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from sports_aggregator.cfb.repository import CFBRepository


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CFB 12-team playoff simulator")
    p.add_argument("command", choices=("forecast", "committee", "backtest"))
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--sims", type=int, default=10000)
    p.add_argument("--as-of-week", type=int, default=None)
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.add_argument("--database", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parser().parse_args(argv)
    repository = CFBRepository(args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3"))
    if args.command == "forecast":
        from sports_aggregator.cfb.playoff_service import build_forecast
        out = build_forecast(repository, args.season, n_sims=args.sims, as_of_week=args.as_of_week,
                             use_cache=False)
        if args.json:
            print(json.dumps(out))
            return 0
        print(f"{out['season']} CFP forecast, through week {out['as_of_week']}, "
              f"{out['n_sims']} sims, {out['games_remaining']} games left")
        print(f"{'Team':<20}{'Conf':<12}{'Rec':<7}{'Rtg':>6}{'Make':>7}{'Bye':>7}{'Final':>7}{'Champ':>7}{'ConfCh':>8}")
        for r in out["rows"][:args.top]:
            print(f"{r['team']:<20}{r['conference'][:11]:<12}{r['wins']}-{r['losses']:<5}{r['rating']:>6.1f}"
                  f"{r['playoff']:>7.1%}{r['bye']:>7.1%}{r['final']:>7.1%}{r['champion']:>7.1%}"
                  f"{r['conf_champion']:>8.1%}")
    elif args.command == "committee":
        from sports_aggregator.cfb.playoff_committee import leave_one_season_out
        out = leave_one_season_out(repository)
        print(json.dumps(out, indent=2))
    else:
        from sports_aggregator.cfb.playoff_backtest import run
        out = run(repository, n_sims=min(args.sims, 3000))
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
