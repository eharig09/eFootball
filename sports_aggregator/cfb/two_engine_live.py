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

from sports_aggregator.cfb import coach_elo
from sports_aggregator.cfb import conditional_convergence as cc
from sports_aggregator.cfb import convergence_routing_holdout as routing
from sports_aggregator.cfb import internal_power_lenses as ipl
from sports_aggregator.cfb import narrative_family_rating_interactions as families
from sports_aggregator.cfb import narrative_shapes as ns
from sports_aggregator.cfb import narrative_shapes_v2 as nsv2
from sports_aggregator.cfb import qb_elo
from sports_aggregator.cfb.game_projection import project_matchup
from sports_aggregator.cfb.lines import game_lines
from sports_aggregator.cfb.live_margin_calibration import predict_live as predict_live_margin
from sports_aggregator.cfb.matchup_research import (
    _live_narrative_context,
    matchup_research_packet,
)
from sports_aggregator.cfb.repository import CFBRepository

MANIFEST_VERSION = "two-engine-pregame-v1"
HISTORICAL_END_SEASON = 2025

FROZEN_LENS_SCALES = {
    "margin_power_edge": 9.110221102599258,
    "football_lab_edge": 6.1027852283045005,
    "elo_edge": 6.0505729824328895,
    "efficiency_power_edge": 12.377866509585655,
    "line_elo_edge": 7.007189888407126,
    "narrative_interaction_edge": 0.36044178601547494,
}
FROZEN_HC_STATS = (19.431141388518345, 208.13062292367948)
FROZEN_QB_STATS = (-0.09133302884958344, 82.76569033618398)

#: Reconciled 2026-09-21: internal_power_lenses._football_lab_margin_lookup
#: used to fit its own one-variable calibration (raw offense-points margin
#: -> actual margin), separate from the margin-v2 model the live Structural
#: signal has always actually read (research["spread"]["projected_home_margin"],
#: i.e. live_margin_calibration.predict_live). Discovery/validation and the
#: live signal were quietly built from two different models. Both now read
#: the same walk-forward margin-v2 (see internal_power_lenses.py's docstring
#: on _football_lab_margin_lookup); these five win rates were re-measured
#: via convergence_routing_holdout.report() under that single, consistent
#: model. The route predicates themselves are untouched -- only the
#: football_lab_edge input feeding "Structural" changed, so these numbers
#: moved by a few points each (largest: fade_old_2_of_3, 63.6% -> 55.0%),
#: not by a redefinition of what "confirms" means.
#:
#: Re-reconciled 2026-09-21 (same day, second pass): game_projection.py's
#: drive-count projection was rewired from the plain Baseline C blend to the
#: validated walk-forward advanced xdrives regression (see xdrives.py's
#: ADVANCED_FEATURES_LIVE). Margin Power's own prediction
#: (live_margin_calibration.predict_live) uses drive_diff as a feature, so a
#: more accurate drives projection changes Margin Power's numeric edge for
#: every game, which can flip which spread-bucket/elo-agreement route a game
#: lands in even though no route predicate or threshold changed.
#:
#: Re-reconciled again 2026-09-21 (same day, third pass): re-measuring
#: FROZEN_LENS_SCALES for the pass above surfaced a real bug the drives
#: rewiring exposed, not just routing movement -- football_lab_edge's stdev
#: had jumped 5.26 -> 8.15 (+55%), traced to ~40 FBS-vs-FCS buy games (e.g.
#: TCU vs Tarleton State, edge -74pts) where the opponent has no pregame Elo
#: on record. margin-v2 used to fall back to an elo-less "base" feature tier
#: for these, extrapolating a model fit on well-matched FBS games onto a
#: blowout. Fixed in live_margin_calibration.py: a team with no Elo is now
#: not assessed by margin-v2 at all (FEATURE_SETS' "base" tier removed;
#: predict_live returns variant="not_assessed_missing_elo"). Every number
#: below is measured under that fix, superseding the second pass -- the
#: football_lab_edge stdev used for FROZEN_LENS_SCALES above is now 6.10, not
#: 8.15 (still up from 5.26: a genuine, more modest widening from the better
#: drives input on ordinary games, not an extrapolation artifact).
ENGINE_A_HISTORY = {
    "positive_4_of_4_spread_lt_14": {"n": 51, "hit_rate": 0.6471, "mean_residual": 7.072},
    "positive_old_2_of_3_elo_agrees_spread_3_to_6_5": {"n": 34, "hit_rate": 0.5882, "mean_residual": 5.481},
    "positive_old_3_of_3_elo_disagrees_spread_lt_3": {"n": 26, "hit_rate": 0.6923, "mean_residual": 7.655},
    "fade_old_2_of_3_elo_agrees_spread_lt_3": {"n": 23, "hit_rate": 0.5652, "mean_residual": 3.619},
    "fade_old_3_of_3_elo_disagrees_spread_14_plus": {"n": 22, "hit_rate": 0.7273, "mean_residual": 3.298},
}
ENGINE_B_HISTORY = {
    "rebound_vs_momentum_qb_opposes": {"n": 181, "hit_rate": 0.5801, "mean_residual": 2.706},
    "rebound_vs_post_success_qb_opposes": {"n": 82, "hit_rate": 0.6098, "mean_residual": 3.780},
}
#: Reconciled alongside ENGINE_A_HISTORY above (two_engine_portfolio.report(),
#: non_conflicting_combined_portfolio.all_2020_2025). Engine B's own policy
#: definitions (narrative-family + rating-direction, not Margin Power) are
#: untouched by the xdrives rewiring and the missing-Elo fix, so
#: ENGINE_B_HISTORY above is unchanged; this combined figure still moves
#: because the A/B overlap partition shifts whenever Engine A's routed game
#: set shifts (321->326, 61.68%->61.35%).
PORTFOLIO_HISTORY = {"n": 326, "hit_rate": 0.6135, "mean_residual": 4.335}


def _pooled(history: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Sample-weighted pool across every route/rule in a history dict --
    the record a game with NO specific route/rule firing can still show,
    instead of a blank dash, since "the engine has no pick here" and "the
    engine has no track record" are different facts."""
    total_n = sum(int(v["n"]) for v in history.values())
    if not total_n:
        return {"n": 0, "hit_rate": None, "mean_residual": None}
    hits = sum(int(v["n"]) * float(v["hit_rate"]) for v in history.values())
    residual = sum(int(v["n"]) * float(v["mean_residual"]) for v in history.values())
    return {
        "n": total_n,
        "hit_rate": round(hits / total_n, 4),
        "mean_residual": round(residual / total_n, 3),
    }


ENGINE_A_OVERALL = _pooled(ENGINE_A_HISTORY)
ENGINE_B_OVERALL = _pooled(ENGINE_B_HISTORY)

#: Plain-language versions of the five frozen route names -- the raw slug
#: ("positive_old_2_of_3_elo_agrees_spread_3_to_6_5") encodes real meaning
#: but only to someone who already knows the vocabulary. Keyed by the exact
#: route name convergence_routing_holdout.ROUTES freezes, so this reads
#: straight off whatever a stored (possibly old) manifest packet already
#: has -- nothing needs to be re-frozen for this to apply.
ROUTE_PLAIN_LANGUAGE = {
    "positive_4_of_4_spread_lt_14": (
        "Every signal agrees -- Margin Power, Structural, Line Elo, and "
        "HC/QB Elo -- and the line isn't a lopsided one. Follow the favored side."
    ),
    "positive_old_2_of_3_elo_agrees_spread_3_to_6_5": (
        "Margin Power plus one other signal agree, and HC/QB Elo backs the "
        "same side, in a moderate (3-6.5 point) line. Follow the favored side."
    ),
    "positive_old_3_of_3_elo_disagrees_spread_lt_3": (
        "Margin Power, Structural, and Line Elo all agree -- even though "
        "HC/QB Elo favors the other team -- in a near-even game (under 3 "
        "points). Follow the favored side; full agreement among the "
        "original three signals has held up here even against HC/QB Elo."
    ),
    "fade_old_2_of_3_elo_agrees_spread_lt_3": (
        "Margin Power plus one other signal agree, and HC/QB Elo backs the "
        "same side, in a near-even game (under 3 points) -- but history "
        "says to take the OTHER team here."
    ),
    "fade_old_3_of_3_elo_disagrees_spread_14_plus": (
        "Margin Power, Structural, and Line Elo all agree, but HC/QB Elo "
        "disagrees, in a lopsided line (14+ points) -- history says to "
        "take the OTHER team here."
    ),
}


def route_plain_language(route_name: str | None) -> str | None:
    if not route_name:
        return None
    return ROUTE_PLAIN_LANGUAGE.get(route_name)


#: Both Engine B rules share the same rebounding side and QB-opposition
#: condition (see _engine_b()) -- they differ only in what the opponent's
#: overreaction-worthy state is -- so unlike ROUTE_PLAIN_LANGUAGE this is
#: composed from clauses rather than a flat per-rule sentence, letting a
#: game where both rules fire together read as one coherent explanation
#: instead of two concatenated slugs.
_ENGINE_B_CLAUSES = {
    "rebound_vs_momentum_qb_opposes": "its opponent is riding a hot streak",
    "rebound_vs_post_success_qb_opposes":
        "its opponent is coming off a big recent success (real letdown risk)",
}


def engine_b_rules_plain_language(rules: list[str] | None) -> str | None:
    if not rules:
        return None
    clauses = [_ENGINE_B_CLAUSES[rule] for rule in rules if rule in _ENGINE_B_CLAUSES]
    if not clauses:
        return None
    return (
        f"The rebounding side is coming off a rough recent result while "
        f"{' and '.join(clauses)} -- and the market hasn't priced that "
        "rebound into the QB ratings yet. Follow the rebounding side."
    )

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
    """Production-safe historical context using frozen research artifacts."""
    key = str(repository.path)
    cached = _HISTORICAL_CACHE.get(key)
    if cached is not None:
        return cached

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
        "lens_scales": dict(FROZEN_LENS_SCALES),
        "hc_stats": FROZEN_HC_STATS,
        "qb_stats": FROZEN_QB_STATS,
        "narrative_rows": narrative_rows,
        "line_state": line_state,
        "line_rate": float(line_rate),
        "historical_calibration_source": "frozen_2015_2025_research_artifacts",
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


def _line_elo_teams(game: dict[str, Any], historical: dict[str, Any]) -> tuple[float, float]:
    """Each team's own pregame Line Elo rating -- the market-fit rating
    _line_elo_edge reduces to a single margin edge. 1500.0 (the base
    rating) for a team with no prior Line Elo history, same default
    _line_elo_edge already used before this was split out."""
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
    return home_value, away_value


def _line_elo_edge(
    game: dict[str, Any],
    market_home_margin: float | None,
    historical: dict[str, Any],
) -> float | None:
    if market_home_margin is None:
        return None
    home_value, away_value = _line_elo_teams(game, historical)
    predicted = (
        (home_value - away_value) / ns.ELO_POINTS_PER_SCORE_POINT
        + ns.DEFAULT_HOME_FIELD_POINTS
    )
    return predicted - float(market_home_margin)


_current_coach_rating = coach_elo.current_rating
_current_qb_rating = qb_elo.current_rating


def _raw_hc_qb(
    repository: CFBRepository, game: dict[str, Any]
) -> dict[str, Any]:
    """Pregame HC/QB differences for completed-history or upcoming games."""
    game_id = int(game["game_id"])
    with repository._reader() as connection:
        hc = connection.execute(
            """SELECT home_pre_elo,away_pre_elo
               FROM cfb_coach_elo_games WHERE game_id=?""",
            (game_id,),
        ).fetchone()
        qb_rows = connection.execute(
            """SELECT side,pre_rating,player_id
               FROM cfb_qb_elo_games WHERE game_id=?""",
            (game_id,),
        ).fetchall()

    qb = {str(row["side"]): float(row["pre_rating"]) for row in qb_rows}
    if hc is not None and "home" in qb and "away" in qb:
        return {
            "hc_diff": float(hc["home_pre_elo"]) - float(hc["away_pre_elo"]),
            "qb_diff": qb["home"] - qb["away"],
            "source": "game_pregame_rows",
        }

    home_coach, home_coach_name = _current_coach_rating(
        repository, season=int(game["season"]), team_id=int(game["home_team_id"])
    )
    away_coach, away_coach_name = _current_coach_rating(
        repository, season=int(game["season"]), team_id=int(game["away_team_id"])
    )
    home_qb, home_qb_meta = _current_qb_rating(
        repository, season=int(game["season"]), team=str(game["home_team"])
    )
    away_qb, away_qb_meta = _current_qb_rating(
        repository, season=int(game["season"]), team=str(game["away_team"])
    )
    return {
        "hc_diff": (
            home_coach - away_coach
            if home_coach is not None and away_coach is not None else None
        ),
        "qb_diff": (
            home_qb - away_qb
            if home_qb is not None and away_qb is not None else None
        ),
        "source": "carried_current_ratings",
        "home_coach": home_coach_name,
        "away_coach": away_coach_name,
        "home_qb": home_qb_meta,
        "away_qb": away_qb_meta,
    }


def team_ratings_display(
    repository: CFBRepository, game: dict[str, Any], historical: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Each team's own HC Elo, QB Elo, CFBD "True" (pregame) Elo, and Line
    Elo, for display -- not the combined edges/z-scores the engine lights
    use, the raw per-team numbers those get built from. Computed fresh on
    every call rather than stored in a frozen manifest packet: this is
    supporting context, not part of the decision itself, so it can stay
    live even for a game whose Engine A/B classification is already frozen.
    """
    historical = historical or _historical_context(repository)
    game_id = int(game["game_id"])
    home_team, away_team = str(game["home_team"]), str(game["away_team"])

    with repository._reader() as connection:
        hc_row = connection.execute(
            """SELECT home_pre_elo,away_pre_elo,home_coach_id,away_coach_id
               FROM cfb_coach_elo_games WHERE game_id=?""",
            (game_id,),
        ).fetchone()
        qb_rows = connection.execute(
            """SELECT side,pre_rating,player_id FROM cfb_qb_elo_games WHERE game_id=?""",
            (game_id,),
        ).fetchall()
    qb_by_side = {str(row["side"]): row for row in qb_rows}

    def _coach_name(coach_id: Any) -> str | None:
        if coach_id is None:
            return None
        with repository._reader() as connection:
            row = connection.execute(
                "SELECT first_name,last_name FROM cfb_coach_elo_ratings WHERE coach_id=?", (coach_id,)
            ).fetchone()
        if row is None:
            return None
        name = f'{row["first_name"] or ""} {row["last_name"] or ""}'.strip()
        return name or None

    def _qb_name(player_id: Any) -> str | None:
        if player_id is None:
            return None
        with repository._reader() as connection:
            row = connection.execute(
                "SELECT name FROM cfb_qb_elo_ratings WHERE player_id=?", (str(player_id),)
            ).fetchone()
        return str(row["name"]) if row and row["name"] else None

    if hc_row is not None:
        hc_home, hc_away = float(hc_row["home_pre_elo"]), float(hc_row["away_pre_elo"])
        home_coach_name = _coach_name(hc_row["home_coach_id"])
        away_coach_name = _coach_name(hc_row["away_coach_id"])
    else:
        hc_home, home_coach_name = _current_coach_rating(
            repository, season=int(game["season"]), team_id=int(game["home_team_id"]))
        hc_away, away_coach_name = _current_coach_rating(
            repository, season=int(game["season"]), team_id=int(game["away_team_id"]))

    if "home" in qb_by_side and "away" in qb_by_side:
        qb_home = float(qb_by_side["home"]["pre_rating"])
        qb_away = float(qb_by_side["away"]["pre_rating"])
        home_qb_name = _qb_name(qb_by_side["home"]["player_id"])
        away_qb_name = _qb_name(qb_by_side["away"]["player_id"])
        qb_source = "completed_game_pregame_row"
    else:
        qb_home, home_qb_meta = _current_qb_rating(
            repository, season=int(game["season"]), team=home_team)
        qb_away, away_qb_meta = _current_qb_rating(
            repository, season=int(game["season"]), team=away_team)
        home_qb_name = home_qb_meta.get("player")
        away_qb_name = away_qb_meta.get("player")
        qb_source = "carried_current_rating"

    line_home, line_away = _line_elo_teams(game, historical)

    def _team_block(hc: float | None, hc_name: str | None, qb: float | None, qb_name: str | None,
                    true_elo: float | None, line: float | None) -> dict[str, Any]:
        return {
            "hc_elo": round(hc, 1) if hc is not None else None,
            "hc_name": hc_name,
            "qb_elo": round(qb, 1) if qb is not None else None,
            "qb_name": qb_name,
            "true_elo": round(float(true_elo), 1) if true_elo is not None else None,
            "line_elo": round(float(line), 1) if line is not None else None,
        }

    return {
        "home": _team_block(
            hc_home, home_coach_name, qb_home, home_qb_name,
            game.get("home_pregame_elo"), line_home,
        ),
        "away": _team_block(
            hc_away, away_coach_name, qb_away, away_qb_name,
            game.get("away_pregame_elo"), line_away,
        ),
        "source": "completed_game_pregame_row" if hc_row is not None else "carried_current_rating",
        "qb_source": qb_source,
    }


def _hc_qb_score(
    repository: CFBRepository,
    game: dict[str, Any],
    historical: dict[str, Any],
) -> tuple[float | None, dict[str, Any]]:
    raw = _raw_hc_qb(repository, game)
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
        repository, game, historical
    )
    if state is None:
        # Margin Power itself didn't resolve, so there's no primary direction
        # for the other three signals to confirm or deny against -- the route
        # genuinely can't qualify. But that's a different fact from "this
        # signal's own input is missing," and the two used to be conflated:
        # every light showed "pending" even when, say, HC/QB Elo was already
        # sitting right there (confirmed live: Engine B's qb_diff resolves for
        # several games where Engine A's HC/QB light showed blank pending).
        # Each light now reports whether ITS OWN input is available, with the
        # raw value attached where it is, while staying "pending" (not
        # on/off) since none of them can be scored as agree/disagree without
        # Margin Power's direction to compare against.
        structural_inputs = [lens_row.get(key) for key in cc.STRUCTURAL_KEYS]
        structural_available = sum(1 for v in structural_inputs if v is not None) >= cc.MIN_STRUCTURAL_COMPONENTS
        structural_value = (
            round(sum(v for v in structural_inputs if v is not None)
                  / sum(1 for v in structural_inputs if v is not None), 3)
            if structural_available else None
        )
        return {
            "ready": False, "qualified": False, "reason": "Margin Power unavailable.",
            "margin_power": margin_meta, "ratings": rating_meta,
            "lights": [
                {"key": "margin_power", "label": "Margin Power", "requirement": "|z| ≥ 1.0",
                 "state": "pending", "value": None},
                {"key": "structural", "label": "Structural", "requirement": "same direction",
                 "state": "pending", "value": structural_value},
                {"key": "line_elo", "label": "Line Elo", "requirement": "same direction",
                 "state": "pending", "value": round(float(line_edge), 3) if line_edge is not None else None},
                {"key": "hc_qb", "label": "HC/QB", "requirement": "route-specific vote",
                 "state": "pending", "value": round(float(hc_qb_z), 3) if hc_qb_z is not None else None},
            ],
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
    inputs_complete = (
        structural_confirms is not None
        and market_confirms is not None
        and hc_qb_aligned is not None
    )
    margin_threshold_met = abs(float(state["margin_z"])) >= 1.0
    ready = inputs_complete
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
    if ready and margin_threshold_met:
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
        "margin_threshold_met": margin_threshold_met,
        "lights": [
            {
                "key": "margin_power",
                "label": "Margin Power",
                "requirement": "|z| ≥ 1.0",
                "state": "on" if margin_threshold_met else "off",
                "value": round(float(state["margin_z"]), 3),
            },
            {
                "key": "structural",
                "label": "Structural",
                "requirement": "same direction",
                "state": (
                    "on" if structural_confirms is True
                    else "off" if structural_confirms is False
                    else "pending"
                ),
                "value": (
                    round(float(state["structural_z"]), 3)
                    if state.get("structural_z") is not None else None
                ),
            },
            {
                "key": "line_elo",
                "label": "Line Elo",
                "requirement": "same direction",
                "state": (
                    "on" if market_confirms is True
                    else "off" if market_confirms is False
                    else "pending"
                ),
                "value": (
                    round(float(state["market_z"]), 3)
                    if state.get("market_z") is not None else None
                ),
            },
            {
                "key": "hc_qb",
                "label": "HC/QB",
                "requirement": "route-specific vote",
                "state": (
                    "on" if hc_qb_aligned is not None and float(hc_qb_aligned) > 0
                    else "off" if hc_qb_aligned is not None
                    else "pending"
                ),
                "value": round(float(hc_qb_z), 3) if hc_qb_z is not None else None,
            },
        ],
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
    raw = _raw_hc_qb(repository, game)
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

    family_map = {
        home: sorted(_active_family_set(by_team.get(home, {}))),
        away: sorted(_active_family_set(by_team.get(away, {}))),
    }
    if not rules:
        return {
            "ready": True, "qualified": False, "qb_diff": round(float(qb_diff), 3),
            "families": family_map,
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
        "families": family_map,
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
    if frozen is not None and frozen.get("state") != "pending":
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
    if frozen is not None:
        packet["source"] = "pending_manifest_live_preview"
        packet["pending_manifest_at"] = frozen.get("frozen_at")
    else:
        packet["source"] = "live_preview"
    return packet


def freeze_week(repository: CFBRepository, *, season: int, week: int) -> dict[str, Any]:
    """Freeze or finalize one week's pregame portfolio classifications.

    A pending row is not a prediction. It may be replaced before kickoff once
    both engines have enough inputs to resolve to a real portfolio state.
    Resolved rows remain immutable.
    """
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
    finalized_pending = []
    pending_remaining = []
    skipped_started = []
    already_frozen = []
    for game in games:
        kickoff = datetime.fromisoformat(str(game["start_date"]).replace("Z", "+00:00"))
        if kickoff <= now or game.get("home_points") is not None or game.get("away_points") is not None:
            skipped_started.append(int(game["game_id"]))
            continue

        existing = frozen_manifest_for_game(repository, int(game["game_id"]))
        if existing is not None and existing.get("state") != "pending":
            already_frozen.append(int(game["game_id"]))
            continue

        packet = classify_game(repository, game)
        frozen_at = datetime.now(timezone.utc).isoformat()
        if existing is not None and existing.get("state") == "pending":
            if packet["state"] == "pending":
                pending_remaining.append(int(game["game_id"]))
                continue
            packet["initial_pending_at"] = existing.get("frozen_at")
            packet["finalized_from_pending"] = True
            payload = json.dumps(packet, sort_keys=True)
            with repository.transaction() as connection:
                connection.execute(
                    """UPDATE cfb_two_engine_manifest
                       SET frozen_at=?,state=?,selected_side=?,selected_team=?,packet_json=?
                       WHERE manifest_version=? AND game_id=? AND state='pending'""",
                    (
                        frozen_at, packet["state"], packet.get("selected_side"),
                        packet.get("selected_team"), payload,
                        MANIFEST_VERSION, int(game["game_id"]),
                    ),
                )
            finalized_pending.append({
                "game_id": int(game["game_id"]),
                "state": packet["state"],
                "selected_team": packet.get("selected_team"),
            })
            continue

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
        if packet["state"] == "pending":
            pending_remaining.append(int(game["game_id"]))

    return {
        "manifest_version": MANIFEST_VERSION,
        "season": int(season),
        "week": int(week),
        "frozen": frozen,
        "finalized_pending": finalized_pending,
        "pending_remaining_game_ids": pending_remaining,
        "already_frozen_game_ids": already_frozen,
        "skipped_started_or_completed_game_ids": skipped_started,
        "immutability": (
            "resolved classifications are immutable; pending placeholders may "
            "finalize once before kickoff"
        ),
    }


def season_record(repository: CFBRepository, season: int) -> dict[str, Any]:
    """Live-graded record of this season's frozen picks -- Engine A, Engine B,
    and the Totals research lean -- against the closing line.

    This is a results tracker, not a classifier: it runs after the fact, over
    games the manifest already froze pregame, and never feeds back into
    `classify_game` or a manifest row. It starts at 0-0 the day the manifest
    is first populated for a season and fills in as those games complete, so
    an early-season read here is a small sample by construction, not a bug.
    """
    _initialize_manifest(repository)
    with repository._reader() as connection:
        rows = [
            dict(r) for r in connection.execute(
                """SELECT m.game_id, m.packet_json, g.home_points, g.away_points
                   FROM cfb_two_engine_manifest m
                   JOIN games g ON g.game_id = m.game_id
                   WHERE m.manifest_version=? AND m.season=?
                     AND g.completed=1
                     AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL""",
                (MANIFEST_VERSION, int(season)),
            )
        ]

    def _blank() -> dict[str, Any]:
        return {"wins": 0, "losses": 0, "pushes": 0, "n": 0}

    engine_a, engine_b, totals = _blank(), _blank(), _blank()

    def _grade(bucket: dict[str, Any], edge: float) -> None:
        if abs(edge) < 1e-9:
            bucket["pushes"] += 1
        elif edge > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["n"] += 1

    for row in rows:
        packet = json.loads(row["packet_json"])
        home_margin = float(row["home_points"]) - float(row["away_points"])
        lines = game_lines(repository, int(row["game_id"]))
        spread = lines.get("consensus_spread")

        if spread is not None:
            for engine_key, bucket in (("engine_a", engine_a), ("engine_b", engine_b)):
                leg = packet.get(engine_key) or {}
                side = leg.get("selected_side")
                if not leg.get("qualified") or side not in ("home", "away"):
                    continue
                margin = home_margin if side == "home" else -home_margin
                side_spread = float(spread) if side == "home" else -float(spread)
                _grade(bucket, margin + side_spread)

        # Totals has no frozen action rule and so no stored pick to read back;
        # it's recomputed the same way the live page shows it. The underlying
        # calibration only ever fits on seasons before this one, so replaying
        # it against a game that has since finished doesn't let that game's
        # own result into the number.
        game = repository.get_game(int(row["game_id"]))
        if game is None:
            continue
        projection = project_matchup(
            repository, game["home_team"], game["away_team"],
            as_of_date=game.get("start_date"), game_id=game.get("game_id"),
        )
        research = matchup_research_packet(repository, game, projection, lines)
        totals_signal = research.get("totals") or {}
        direction = totals_signal.get("model_direction")
        line_total = totals_signal.get("closing_total")
        regime = totals_signal.get("regime_benchmark")
        if not regime or not regime.get("tracked") or direction not in ("over", "under") \
                or line_total is None:
            continue
        actual_total = float(row["home_points"]) + float(row["away_points"])
        diff = actual_total - float(line_total)
        _grade(totals, diff if direction == "over" else -diff)

    def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
        bucket["record"] = (
            f"{bucket['wins']}-{bucket['losses']}"
            + (f"-{bucket['pushes']}" if bucket["pushes"] else "")
        )
        decided = bucket["wins"] + bucket["losses"]
        bucket["hit_rate"] = round(bucket["wins"] / decided, 4) if decided else None
        return bucket

    return {
        "season": int(season),
        "engine_a": _finish(engine_a),
        "engine_b": _finish(engine_b),
        "totals": _finish(totals),
    }
