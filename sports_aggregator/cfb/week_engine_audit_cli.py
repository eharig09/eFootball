"""Compact weekly audit for pregame two-engine CFB states."""
from __future__ import annotations

import argparse
import json
import os

from sports_aggregator.cfb.repository import CFBRepository
from sports_aggregator.cfb.two_engine_live import display_packet


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--week", type=int, default=4)
    parser.add_argument("--database", default=None)
    parser.add_argument("--full-json", action="store_true")
    args = parser.parse_args(argv)

    repository = CFBRepository(
        args.database or os.getenv("CFB_DATABASE_PATH", "instance/cfb.sqlite3")
    )
    with repository._reader() as connection:
        games = [
            dict(row) for row in connection.execute(
                """SELECT * FROM games
                   WHERE season=? AND week=?
                   ORDER BY start_date,game_id""",
                (int(args.season), int(args.week)),
            )
        ]

    rows = []
    for game in games:
        if game.get("completed") or (
            game.get("home_points") is not None
            and game.get("away_points") is not None
        ):
            continue
        packet = display_packet(repository, game)
        a = packet.get("engine_a") or {}
        b = packet.get("engine_b") or {}
        rows.append({
            "game_id": int(game["game_id"]),
            "matchup": str(game["away_team"]) + " @ " + str(game["home_team"]),
            "kickoff": game.get("start_date"),
            "state": packet.get("state"),
            "state_label": packet.get("state_label"),
            "source": packet.get("source"),
            "selected_team": packet.get("selected_team"),
            "engine_a": {
                "qualified": bool(a.get("qualified")),
                "route": a.get("route"),
                "selected_team": a.get("selected_team"),
                "agreement_label": a.get("agreement_label"),
                "lights": {
                    str(light.get("label")): str(light.get("state"))
                    for light in (a.get("lights") or [])
                },
            },
            "engine_b": {
                "qualified": bool(b.get("qualified")),
                "rules": b.get("rules") or [],
                "selected_team": b.get("selected_team"),
                "qb_diff": b.get("qb_diff"),
                "families": b.get("families") or {},
            },
        })

    firing = [
        row for row in rows
        if row["engine_a"]["qualified"] or row["engine_b"]["qualified"]
    ]
    payload = {
        "season": int(args.season),
        "week": int(args.week),
        "counts": {
            "pregame_games": len(rows),
            "engine_a_fires": sum(r["engine_a"]["qualified"] for r in rows),
            "engine_b_fires": sum(r["engine_b"]["qualified"] for r in rows),
            "agreement": sum(r["state"] == "agreement" for r in rows),
            "conflict": sum(r["state"] == "conflict" for r in rows),
            "pending": sum(r["state"] == "pending" for r in rows),
            "no_signal": sum(r["state"] == "none" for r in rows),
        },
        "visual_inspection_shortlist": firing,
        "all_games": rows if args.full_json else None,
        "purpose": "pregame visual inspection only; no outcome grading",
    }

    if args.full_json:
        print(json.dumps(payload, indent=2))
        return 0

    c = payload["counts"]
    print(
        "Week %s: A=%s B=%s agreement=%s conflict=%s pending=%s no-signal=%s"
        % (
            args.week, c["engine_a_fires"], c["engine_b_fires"],
            c["agreement"], c["conflict"], c["pending"], c["no_signal"],
        )
    )
    if not firing:
        print("No games currently fire Engine A or Engine B.")
        return 0

    print()
    for row in firing:
        print(
            "%s | %s | %s | selected=%s | source=%s"
            % (
                row["game_id"], row["matchup"], row["state_label"],
                row["selected_team"] or "-", row["source"],
            )
        )
        if row["engine_a"]["qualified"]:
            lights = ", ".join(
                key + "=" + value
                for key, value in row["engine_a"]["lights"].items()
            )
            print(
                "  A: %s -> %s [%s; %s]"
                % (
                    row["engine_a"]["route"],
                    row["engine_a"]["selected_team"],
                    row["engine_a"]["agreement_label"] or "-",
                    lights,
                )
            )
        if row["engine_b"]["qualified"]:
            print(
                "  B: %s -> %s [QB diff %s]"
                % (
                    " + ".join(row["engine_b"]["rules"]),
                    row["engine_b"]["selected_team"],
                    row["engine_b"]["qb_diff"],
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
