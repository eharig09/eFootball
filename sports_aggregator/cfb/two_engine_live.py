"""Pregame-only two-engine classification and immutable manifest support.

Engine A keeps the five frozen convergence route definitions, using:
- same-season Margin Power from completed prior games only,
- Structural confirmation from live Football Lab + pregame Elo (and efficiency
  only when a leak-safe live value is available in the future),
- pre-line Line Elo,
- corrected prior-seasons-only HC/QB normalization.

Engine B keeps the two frozen Narrative/QB overreaction policies.

This module never grades a game and never reads the target game's final score.
A manifest row is immutable once written; rerunning a freeze does not overwrite
the original pregame snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sports_aggregator.cfb import conditional_convergence as cc
from sports_aggregator.cfb import convergence_routing_holdout as routing
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb import narrative_family_rating_interactions as families
from sports_aggregator.cfb import narrative_shapes as ns
from sports_aggregator.cfb import narrative_shapes_v2 as nsv2
from sports_aggregator.cfb.game_projection import project_matchup
from sports_aggregator.cfb.lines import game_lines
from sports_aggregator.cfb.live_margin_calibration import predict_live as predict_live_margin
from sports_aggregator.cfb.matchup_research import (
    _live_narrative_context,
    matchup_research_packet,
)
from sports_aggregator.cfb.rating_predictive_power import _load_dataset
from sports_aggregator.cfb.rating_walkforward import _mean_std
from sports_aggregator.cfb.repository import CFBRepository

MANIFEST_VERSION = "two-engine-pregame-v1"
HISTORICAL_END_SEASON = 2025

ENGINE_A_HISTORY = {
    "positive_4_of_4_spread_lt_14": {"n": 46, "hit_rate": 0.6304, "mean_residual": 6.750},
    "positive_old_2_of_3_elo_agrees_spread_3_to_6_5": {"n": 33, "hit_rate": 0.7273, "mean_residual": 7.238},
    "positive_old_3_of_3_elo_disagrees_spread_lt_3": {"n": 23, "hit_rate": 0.6522, "mean_residual": 6.103},
    "fade_old_2_of_3_elo_agrees_spread_lt_3": {"n": 22, "hit_rate": 0.6364, "mean_residual": 3.405},
    "fade_old_3_of_3_elo_disagrees_spread_14_plus": {"n": 22, "hit_rate": 0.6818, "mean_residual": 2.964},
}
ENGINE_B_HISTORY = {
    "rebound_vs_momentum_qb_opposes": {"n": 181, "hit_rate": 0.5801, "mean_residual": 2.706},
    "rebound_vs_post_success_qb_opposes": {"n": 82, "hit_rate": 0.6098, "mean_residual": 3.780},
}
PORTFOLIO_HISTORY = {"n": 317, "hit_rate": 0.6183, "mean_residual": 4.077}

_HISTORICAL_CACHE: dict[str, dict[str, Any]] = {}


def _initialize_manifest(repository: CFBRepository) -> None:
    with repository.transaction() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cfb_two_engine_manifest (
                   manifest_version TEXT NOT NULL,
                   game_id INTEGER NOT NULL,
                   season INTEGER NOT NULL,
                   week INTEGER,
                   kickoff TEXT,
                   frozen_at TEXT NOT NULL,
                   state TEXT NOT NULL,
                   selected_side TEXT,
                   selected_team TEXT,
                   packet_json TEXT NOT NULL,
                   PRIMARY KEY (manifest_version, game_id)
               )"""
        )


def _historical_context(repository: CFBRepository) -> dict[str, Any]:
    key = str(repository.path)
    cached = _HISTORICAL_CACHE.get(key)
    if cached is not None:
        return cached

    lens_rows = ipl.build_lens_rows(repository, test_season=HISTORICAL_END_SEASON)
    lens_train = [row for row in lens_rows if int(row["season"]) <= HISTORICAL_END_SEASON]
    scales = cc._lens_scales(lens_train)

    rating_rows = [
        row for row in _load_dataset(
            repository, start_season=2015, end_season=HISTORICAL_END_SEASON
        )
        if row.get("qb_diff") is not None
    ]
    hc_stats = _mean_std([float(row["hc_diff"]) for row in rating_rows])
    qb_stats = _mean_std([float(row["qb_diff"]) for row in rating_rows])

    narrative_rows = nsv2._load_rows(repository)
    historical_narrative = [
        row for row in narrative_rows if int(row["season"]) <= HISTORICAL_END_SEASON
    ]
    line_rate, _ = nsv2._choose_line_rate(
        historical_narrative, validation_season=2024
    )
    line_state, _ = nsv2._line_elo_pass(
        narrative_rows, learning_rate=float(line_rate)
    )

    cached = {
        "lens_scales": scales,
        "hc_stats": hc_stats,
        "qb_stats": qb_stats,
        "narrative_rows": narrative_rows,
        "line_state": line_state,
        "line_rate": float(line_rate),
    }
    _HISTORICAL_CACHE[key] = cached
    return cached


def _completed_prior_games(repository: CFBRepository, game: dict[str, Any]) -> list[dict[str, Any]]:
    with repository._reader() as connection:
        return [
            dict(row) for row in connection.execute(
                """SELECT game_id,season,week,start_date,home_team,away_team,
                          home_points,away_points
                   FROM games
                   WHERE season=? AND start_date<?
                     AND home_points IS NOT NULL AND away_points IS NOT NULL
                   ORDER BY start_date,game_id""",
                (int(game["season"]), str(game.get("start_date") or "")),
            )
        ]


def _margin_power_edge(
    repository: CFBRepository,
    game: dict[str, Any],
    market_home_margin: float | None,
) -> tuple[float | None, dict[str, Any]]:
    prior = _completed_prior_games(repository, game)
    ratings, counts = ipl._solve_srs(prior)
    home, away = str(game["home_team"]), str(game["away_team"])
    home_n, away_n = counts.get(home, 0), counts.get(away, 0)
    if (
        market_home_margin is None
        or home_n < ipl.MIN_SRS_GAMES
        or away_n < ipl.MIN_SRS_GAMES
        or home not in ratings
        or away not in ratings
    ):
        return None, {
            "home_prior_games": home_n,
            "away_prior_games": away_n,
            "minimum_prior_games": ipl.MIN_SRS_GAMES,
        }
    margin = float(ratings[home]) - float(ratings[away]) + ipl.HFA_POINTS
    return margin - float(market_home_margin), {
        "home_prior_games": home_n,
        "away_prior_games": away_n,
        "projected_home_margin": round(margin, 3),
    }


def _line_elo_edge(
    game: dict[str, Any],
    market_home_margin: float | None,
    historical: dict[str, Any],
) -> float | None:
    if market_home_margin is None:
        return None
    rows = historical["narrative_rows"]
    state = historical["line_state"]
    kickoff = str(game.get("start_date") or "")
    latest: dict[str, tuple[str, float]] = {}
    for row in rows:
        if str(row.get("kickoff") or "") >= kickoff:
            continue
        team = str(row["team"])
        if team not in {str(game["home_team"]), str(game["away_team"])}:
            continue
        packet = state.get((int(row["game_id"]), team)) or {}
        post = packet.get("post_line_elo")
        if post is not None:
            latest[team] = (str(row.get("kickoff") or ""), float(post))
    home_value = latest.get(str(game["home_team"]), ("", 1500.0))[1]
    away_value = latest.get(str(game["away_team"]), ("", 1500.0))[1]
    predicted = (
        (home_value - away_value) / ns.ELO_POINTS_PER_SCORE_POINT
        + ns.DEFAULT_HOME_FIELD_POINTS
    )
    return predicted - float(market_home_margin)


def _raw_hc_qb(repository: CFBRepository, game_id: int) -> dict[str, float | None]:
    with repository._reader() as connection:
        hc = connection.execute(
            """SELECT home_pre_elo,away_pre_elo
               FROM cfb_coach_elo_games WHERE game_id=?""",
            (int(game_id),),
        ).fetchone()
        qb_rows = connection.execute(
            """SELECT side,pre_rating FROM cfb_qb_elo_games WHERE game_id=?""",
            (int(game_id),),
        ).fetchall()
    qb = {str(row["side"]): float(row["pre_rating"]) for row in qb_rows}
    return {
        "hc_diff": (
            float(hc["home_pre_elo"]) - float(hc["away_pre_elo"])
            if hc is not None else None
        ),
        "qb_diff": (
            qb["home"] - qb["away"]
            if "home" in qb and "away" in qb else None
        ),
    }


def _hc_qb_score(
    repository: CFBRepository,
    game_id: int,
    historical: dict[str, Any],
) -> tuple[float | None, dict[str, Any]]:
    raw = _raw_hc_qb(repository, game_id)
    hc_stats, qb_stats = historical["hc_stats"], historical["qb_stats"]
    if (
        raw["hc_diff"] is None or raw["qb_diff"] is None
        or hc_stats is None or qb_stats is None
    ):
        return None, raw
    hc_z = (float(raw["hc_diff"]) - hc_stats[0]) / hc_stats[1]
    qb_z = (float(raw["qb_diff"]) - qb_stats[0]) / qb_stats[1]
    return (hc_z + qb_z) / 2.0, {
        **raw, "hc_z": round(hc_z, 3), "qb_z": round(qb_z, 3)
    }


def _spread_bucket(market_home_margin: float | None) -> str | None:
    if market_home_margin is None:
        return None
    value = abs(float(market_home_margin))
    if value < 3:
        return "<3"
    if value < 7:
        return "3-6.5"
    if value < 14:
        return "7-13.5"
    return "14+"


def _engine_a(
    repository: CFBRepository,
    game: dict[str, Any],
    research: dict[str, Any],
    market_home_margin: float | None,
    historical: dict[str, Any],
) -> dict[str, Any]:
    margin_edge, margin_meta = _margin_power_edge(
        repository, game, market_home_margin
    )
    projected = (research.get("spread") or {}).get("projected_home_margin")
    football_lab_edge = (
        float(projected) - float(market_home_margin)
        if projected is not None and market_home_margin is not None else None
    )
    home_elo = game.get("home_pregame_elo")
    away_elo = game.get("away_pregame_elo")
    elo_margin = ns._expected_margin_from_elo(
        home_elo, away_elo, is_home=True,
        home_field_points=ns.DEFAULT_HOME_FIELD_POINTS,
    )
    elo_edge = (
        float(elo_margin) - float(market_home_margin)
        if elo_margin is not None and market_home_margin is not None else None
    )
    line_edge = _line_elo_edge(game, market_home_margin, historical)

    lens_row = {
        "margin_power_edge": margin_edge,
        "football_lab_edge": football_lab_edge,
        "elo_edge": elo_edge,
        "efficiency_power_edge": None,
        "line_elo_edge": line_edge,
        "narrative_interaction_edge": None,
    }
    state = cc._state(lens_row, historical["lens_scales"])
    hc_qb_z, rating_meta = _hc_qb_score(
        repository, int(game["game_id"]), historical
    )
    if state is None:
        return {
            "ready": False, "qualified": False, "reason": "Margin Power unavailable.",
            "margin_power": margin_meta, "ratings": rating_meta,
        }

    direction = int(state["primary_direction"])
    selected_side = "home" if direction > 0 else "away"
    selected_team = (
        str(game["home_team"]) if selected_side == "home"
        else str(game["away_team"])
    )
    hc_qb_aligned = (
        float(hc_qb_z) * direction if hc_qb_z is not None else None
    )
    structural_confirms = state.get("structural_confirms")
    market_confirms = state.get("market_confirms")
    ready = (
        abs(float(state["margin_z"])) >= 1.0
        and structural_confirms is not None
        and market_confirms is not None
        and hc_qb_aligned is not None
    )
    old_count = (
        1 + int(bool(structural_confirms)) + int(bool(market_confirms))
        if structural_confirms is not None and market_confirms is not None else None
    )
    agreement_count = (
        int(old_count) + int(float(hc_qb_aligned) > 0)
        if old_count is not None and hc_qb_aligned is not None else None
    )
    route_row = {
        "old_agreement_count": old_count,
        "hc_qb_elo_confirms": (
            float(hc_qb_aligned) > 0 if hc_qb_aligned is not None else False
        ),
        "agreement_count": agreement_count,
        "spread_bucket": _spread_bucket(market_home_margin),
    }
    matched = []
    if ready:
        for route in routing.ROUTES:
            if route["predicate"](route_row):
                matched.append(route)

    route = matched[0] if matched else None
    routed_side = selected_side
    if route and str(route["action"]) == "fade":
        routed_side = "away" if selected_side == "home" else "home"
    routed_team = (
        str(game["home_team"]) if routed_side == "home"
        else str(game["away_team"])
    ) if route else None

    return {
        "ready": ready,
        "qualified": route is not None,
        "route": str(route["name"]) if route else None,
        "action": str(route["action"]) if route else None,
        "selected_side": routed_side if route else None,
        "selected_team": routed_team,
        "primary_side": selected_side,
        "primary_team": selected_team,
        "agreement_count": agreement_count,
        "agreement_label": f"{agreement_count}/4" if agreement_count is not None else None,
        "spread_bucket": route_row["spread_bucket"],
        "historical": ENGINE_A_HISTORY.get(str(route["name"])) if route else None,
        "components": {
            "margin_power_z": round(float(state["margin_z"]), 3),
            "structural_z": (
                round(float(state["structural_z"]), 3)
                if state.get("structural_z") is not None else None
            ),
            "structural_members": state.get("structural_members") or [],
            "structural_confirms": structural_confirms,
            "line_elo_z": (
                round(float(state["market_z"]), 3)
                if state.get("market_z") is not None else None
            ),
            "line_elo_confirms": market_confirms,
            "hc_qb_z": round(float(hc_qb_z), 3) if hc_qb_z is not None else None,
            "hc_qb_aligned": (
                round(float(hc_qb_aligned), 3)
                if hc_qb_aligned is not None else None
            ),
            "hc_qb_confirms": (
                float(hc_qb_aligned) > 0 if hc_qb_aligned is not None else None
            ),
        },
        "margin_power": margin_meta,
        "ratings": rating_meta,
    }


def _active_family_set(team_packet: dict[str, Any]) -> set[str]:
    tags = [str(card["tag"]) for card in team_packet.get("tags") or []]
    family_set, _ = families._family_tags(tags)
    return family_set


def _engine_b(
    repository: CFBRepository,
    game: dict[str, Any],
    narrative: dict[str, Any],
) -> dict[str, Any]:
    if not narrative.get("available"):
        return {"ready": False, "qualified": False, "reason": "Narrative state unavailable."}
    by_team = {str(row["team"]): row for row in narrative.get("teams") or []}
    home, away = str(game["home_team"]), str(game["away_team"])
    raw = _raw_hc_qb(repository, int(game["game_id"]))
    qb_diff = raw.get("qb_diff")
    if qb_diff is None:
        return {
            "ready": False, "qualified": False,
            "reason": "Pregame QB Elo unavailable.", "qb_diff": None,
        }

    rules = []
    for rebound_team, opponent in ((home, away), (away, home)):
        rebound_families = _active_family_set(by_team.get(rebound_team, {}))
        opponent_families = _active_family_set(by_team.get(opponent, {}))
        if "negative_result_rebound" not in rebound_families:
            continue
        rebound_is_home = rebound_team == home
        qb_favors_rebound = float(qb_diff) > 0 if rebound_is_home else float(qb_diff) < 0
        if qb_favors_rebound:
            continue
        if "positive_result_momentum" in opponent_families:
            rules.append("rebound_vs_momentum_qb_opposes")
        if "post_success_risk" in opponent_families:
            rules.append("rebound_vs_post_success_qb_opposes")

    if not rules:
        return {
            "ready": True, "qualified": False, "qb_diff": round(float(qb_diff), 3),
        }

    # Both frozen rules share the rebound side. If both appear, preserve both
    # reasons but emit one Engine B selection.
    rebound_team = None
    for team in (home, away):
        fams = _active_family_set(by_team.get(team, {}))
        if "negative_result_rebound" in fams:
            team_is_home = team == home
            qb_favors_team = float(qb_diff) > 0 if team_is_home else float(qb_diff) < 0
            if not qb_favors_team:
                rebound_team = team
                break
    selected_side = "home" if rebound_team == home else "away"
    return {
        "ready": True,
        "qualified": True,
        "rules": rules,
        "selected_side": selected_side,
        "selected_team": rebound_team,
        "qb_diff": round(float(qb_diff), 3),
        "historical": [
            {"rule": rule, **ENGINE_B_HISTORY[rule]} for rule in rules
        ],
        "families": {
            home: sorted(_active_family_set(by_team.get(home, {}))),
            away: sorted(_active_family_set(by_team.get(away, {}))),
        },
    }


def _implication(state: str, team: str | None) -> str:
    if state == "agreement":
        return (
            f"Both independent engines point to {team}. Quantitative convergence "
            "and the Narrative/QB overreaction setup agree."
        )
    if state == "engine_a_only":
        return (
            f"Engine A points to {team}. Multiple quantitative lenses meet one "
            "of the frozen convergence routes; Engine B does not qualify."
        )
    if state == "engine_b_only":
        return (
            f"Engine B points to {team}. The recent-result narrative plus QB "
            "pricing matches the frozen overreaction family; Engine A does not qualify."
        )
    if state == "conflict":
        return (
            "Engine A and Engine B point to opposite sides. The combined portfolio "
            "treats this as a conflict, not a selection."
        )
    if state == "pending":
        return "Not all pregame inputs are ready yet; no frozen classification is recorded."
    return "No frozen two-engine rule qualifies for this matchup."


def classify_game(
    repository: CFBRepository,
    game: dict[str, Any],
    *,
    projection: dict[str, Any] | None = None,
    lines: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    historical = _historical_context(repository)
    lines = lines if lines is not None else game_lines(repository, int(game["game_id"]))
    projection = projection if projection is not None else project_matchup(
        repository, game["home_team"], game["away_team"],
        as_of_date=game.get("start_date"), game_id=game.get("game_id"),
    )
    research = research if research is not None else matchup_research_packet(
        repository, game, projection, lines
    )
    spread = lines.get("consensus_spread")
    market_home_margin = -float(spread) if spread is not None else None
    narrative = research.get("narrative") or _live_narrative_context(repository, game, lines)

    engine_a = _engine_a(
        repository, game, research, market_home_margin, historical
    )
    engine_b = _engine_b(repository, game, narrative)

    a_side = engine_a.get("selected_side") if engine_a.get("qualified") else None
    b_side = engine_b.get("selected_side") if engine_b.get("qualified") else None
    if a_side and b_side:
        state = "agreement" if a_side == b_side else "conflict"
    elif a_side:
        state = "engine_a_only"
    elif b_side:
        state = "engine_b_only"
    elif not engine_a.get("ready") or not engine_b.get("ready"):
        state = "pending"
    else:
        state = "none"

    selected_side = (
        a_side if state in {"engine_a_only", "agreement"}
        else b_side if state == "engine_b_only" else None
    )
    selected_team = (
        str(game["home_team"]) if selected_side == "home"
        else str(game["away_team"]) if selected_side == "away" else None
    )

    return {
        "manifest_version": MANIFEST_VERSION,
        "game_id": int(game["game_id"]),
        "season": int(game["season"]),
        "week": int(game["week"]) if game.get("week") is not None else None,
        "kickoff": game.get("start_date"),
        "state": state,
        "state_label": {
            "agreement": "ENGINE A + B",
            "engine_a_only": "ENGINE A",
            "engine_b_only": "ENGINE B",
            "conflict": "A / B CONFLICT",
            "pending": "INPUTS PENDING",
            "none": "NO QUALIFIED SIGNAL",
        }[state],
        "qualified": state in {"agreement", "engine_a_only", "engine_b_only"},
        "selected_side": selected_side,
        "selected_team": selected_team,
        "implication": _implication(state, selected_team),
        "engine_a": engine_a,
        "engine_b": engine_b,
        "portfolio_reference": PORTFOLIO_HISTORY,
        "method_note": (
            "Pregame-only classification. Engine A uses corrected walk-forward "
            "HC/QB normalization; Engine B uses the two frozen Narrative/QB rules."
        ),
    }


def frozen_manifest_for_game(repository: CFBRepository, game_id: int) -> dict[str, Any] | None:
    _initialize_manifest(repository)
    with repository._reader() as connection:
        row = connection.execute(
            """SELECT packet_json,frozen_at FROM cfb_two_engine_manifest
               WHERE manifest_version=? AND game_id=?""",
            (MANIFEST_VERSION, int(game_id)),
        ).fetchone()
    if row is None:
        return None
    packet = json.loads(str(row["packet_json"]))
    packet["frozen_at"] = row["frozen_at"]
    packet["source"] = "frozen_manifest"
    return packet


def manifest_for_games(repository: CFBRepository, game_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not game_ids:
        return {}
    _initialize_manifest(repository)
    placeholders = ",".join("?" for _ in game_ids)
    with repository._reader() as connection:
        rows = connection.execute(
            f"""SELECT game_id,packet_json,frozen_at
                FROM cfb_two_engine_manifest
                WHERE manifest_version=? AND game_id IN ({placeholders})""",
            (MANIFEST_VERSION, *[int(gid) for gid in game_ids]),
        ).fetchall()
    out = {}
    for row in rows:
        packet = json.loads(str(row["packet_json"]))
        packet["frozen_at"] = row["frozen_at"]
        packet["source"] = "frozen_manifest"
        out[int(row["game_id"])] = packet
    return out


def display_packet(
    repository: CFBRepository,
    game: dict[str, Any],
    *,
    projection: dict[str, Any] | None = None,
    lines: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = frozen_manifest_for_game(repository, int(game["game_id"]))
    if frozen is not None:
        return frozen
    if game.get("completed") or (
        game.get("home_points") is not None and game.get("away_points") is not None
    ):
        return {
            "state": "unfrozen_completed",
            "state_label": "NO PREGAME MANIFEST",
            "qualified": False,
            "selected_team": None,
            "implication": "This completed game was not frozen into the pregame two-engine manifest.",
            "source": "none",
        }
    packet = classify_game(
        repository, game, projection=projection, lines=lines, research=research
    )
    packet["source"] = "live_preview"
    return packet


def freeze_week(repository: CFBRepository, *, season: int, week: int) -> dict[str, Any]:
    _initialize_manifest(repository)
    now = datetime.now(timezone.utc)
    with repository._reader() as connection:
        games = [
            dict(row) for row in connection.execute(
                """SELECT * FROM games
                   WHERE season=? AND week=?
                   ORDER BY start_date,game_id""",
                (int(season), int(week)),
            )
        ]

    frozen = []
    skipped_started = []
    already_frozen = []
    for game in games:
        kickoff = datetime.fromisoformat(str(game["start_date"]).replace("Z", "+00:00"))
        if kickoff <= now or game.get("home_points") is not None or game.get("away_points") is not None:
            skipped_started.append(int(game["game_id"]))
            continue
        if frozen_manifest_for_game(repository, int(game["game_id"])) is not None:
            already_frozen.append(int(game["game_id"]))
            continue
        packet = classify_game(repository, game)
        frozen_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(packet, sort_keys=True)
        with repository.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO cfb_two_engine_manifest (
                       manifest_version,game_id,season,week,kickoff,frozen_at,
                       state,selected_side,selected_team,packet_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    MANIFEST_VERSION, int(game["game_id"]), int(game["season"]),
                    int(game["week"]) if game.get("week") is not None else None,
                    game.get("start_date"), frozen_at, packet["state"],
                    packet.get("selected_side"), packet.get("selected_team"), payload,
                ),
            )
        frozen.append({
            "game_id": int(game["game_id"]),
            "state": packet["state"],
            "selected_team": packet.get("selected_team"),
        })

    return {
        "manifest_version": MANIFEST_VERSION,
        "season": int(season),
        "week": int(week),
        "frozen": frozen,
        "already_frozen_game_ids": already_frozen,
        "skipped_started_or_completed_game_ids": skipped_started,
        "immutable": True,
    }
