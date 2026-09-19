"""Leak-safe narrative-state and Line-Elo research for college football.

Narrative state is intentionally separate from the production projection model.
It describes the gap between results-based Elo, market-implied strength, other
pregame ratings, and recent expectation surprises.  Every feature is available
before the target game's kickoff; game outcomes update history only afterward.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from statistics import median
from typing import Any

from sports_aggregator.cfb.repository import schema_once
from sports_aggregator.cfb import xpoints

NARRATIVE_VERSION = "narrative-shape-v1"
LINE_ELO_VERSION = "line-elo-v1"
ELO_POINTS_PER_SCORE_POINT = 25.0
DEFAULT_HOME_FIELD_POINTS = 2.5
DEFAULT_LINE_LEARNING_RATE = 0.35

SCHEMA = """
CREATE TABLE IF NOT EXISTS cfb_narrative_state (
  game_id INTEGER NOT NULL,
  team TEXT NOT NULL,
  opponent TEXT NOT NULL,
  side TEXT NOT NULL,
  season INTEGER NOT NULL,
  week INTEGER,
  kickoff TEXT NOT NULL,
  narrative_version TEXT NOT NULL,
  line_elo_version TEXT NOT NULL,

  true_elo REAL,
  opponent_true_elo REAL,
  line_elo REAL,
  opponent_line_elo REAL,
  line_minus_true_elo REAL,
  prior_line_minus_true_elo REAL,
  perception_change REAL,
  perception_momentum_3g REAL,

  elo_expected_margin REAL,
  market_expected_margin REAL,
  market_vs_elo_margin REAL,
  fpi_margin REAL,
  core_margin REAL,

  previous_opponent_elo REAL,
  previous_market_expected_margin REAL,
  previous_elo_surprise REAL,
  previous_market_surprise REAL,
  rolling_3g_elo_surprise REAL,
  rolling_3g_market_surprise REAL,

  next_opponent TEXT,
  next_opponent_current_elo REAL,
  lookahead_score REAL,
  sandwich_score REAL,

  statement_win INTEGER NOT NULL DEFAULT 0,
  upset_win INTEGER NOT NULL DEFAULT 0,
  bad_loss INTEGER NOT NULL DEFAULT 0,
  upset_loss INTEGER NOT NULL DEFAULT 0,
  letdown_candidate INTEGER NOT NULL DEFAULT 0,
  bounceback_candidate INTEGER NOT NULL DEFAULT 0,
  won_big_then_underdog INTEGER NOT NULL DEFAULT 0,
  market_darling INTEGER NOT NULL DEFAULT 0,
  market_skepticism INTEGER NOT NULL DEFAULT 0,
  market_chase INTEGER NOT NULL DEFAULT 0,
  market_lag INTEGER NOT NULL DEFAULT 0,
  lookahead_candidate INTEGER NOT NULL DEFAULT 0,
  sandwich_candidate INTEGER NOT NULL DEFAULT 0,
  disputed_team INTEGER NOT NULL DEFAULT 0,
  tags_json TEXT NOT NULL,

  actual_margin REAL,
  market_margin_residual REAL,
  elo_margin_residual REAL,
  covered INTEGER,
  won INTEGER,

  built_at TEXT NOT NULL,
  PRIMARY KEY(game_id, team, narrative_version)
);
CREATE INDEX IF NOT EXISTS idx_cfb_narrative_state_season
  ON cfb_narrative_state(narrative_version, season, week);
CREATE INDEX IF NOT EXISTS idx_cfb_narrative_state_team
  ON cfb_narrative_state(narrative_version, team, season, week);
"""


@schema_once("narrative_shapes")
def initialize(repository) -> None:
    xpoints.initialize(repository)
    with closing(repository._connect()) as connection:
        connection.executescript(SCHEMA)
        connection.commit()


def _mean(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pearson(pairs) -> float | None:
    vals = [(float(x), float(y)) for x, y in pairs if x is not None and y is not None]
    if len(vals) < 3:
        return None
    mx = sum(x for x, _ in vals) / len(vals)
    my = sum(y for _, y in vals) / len(vals)
    num = sum((x - mx) * (y - my) for x, y in vals)
    dx = math.sqrt(sum((x - mx) ** 2 for x, _ in vals))
    dy = math.sqrt(sum((y - my) ** 2 for _, y in vals))
    return round(num / (dx * dy), 4) if dx and dy else None


def _category_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    residuals = [float(r["market_margin_residual"]) for r in rows
                 if r.get("market_margin_residual") is not None]
    covers = [int(r["covered"]) for r in rows if r.get("covered") is not None]
    if not residuals:
        return {"n": 0, "mean_market_residual": None, "median_market_residual": None,
                "cover_rate": None}
    return {
        "n": len(residuals),
        "mean_market_residual": round(sum(residuals) / len(residuals), 3),
        "median_market_residual": round(float(median(residuals)), 3),
        "cover_rate": round(sum(covers) / len(covers), 4) if covers else None,
    }


def _market_line_elo_observation(home_line_elo: float, away_line_elo: float,
                                 market_home_margin: float, *,
                                 home_field_points: float,
                                 learning_rate: float) -> tuple[float, float]:
    """Assimilate the current closing line as a noisy observation of team strength."""
    prior_diff = float(home_line_elo) - float(away_line_elo)
    target_diff = ELO_POINTS_PER_SCORE_POINT * (
        float(market_home_margin) - float(home_field_points))
    innovation = target_diff - prior_diff
    half_update = float(learning_rate) * innovation / 2.0
    return float(home_line_elo) + half_update, float(away_line_elo) - half_update


def _expected_margin_from_elo(team_elo: float | None, opponent_elo: float | None,
                              *, is_home: bool,
                              home_field_points: float) -> float | None:
    if team_elo is None or opponent_elo is None:
        return None
    neutral = (float(team_elo) - float(opponent_elo)) / ELO_POINTS_PER_SCORE_POINT
    return neutral + (float(home_field_points) if is_home else -float(home_field_points))


def _tag_names(values: dict[str, int]) -> list[str]:
    return [key for key, value in values.items() if int(value)]


def build(repository, *, from_season: int = 2022, to_season: int = 2025,
          narrative_version: str = NARRATIVE_VERSION,
          home_field_points: float = DEFAULT_HOME_FIELD_POINTS,
          line_learning_rate: float = DEFAULT_LINE_LEARNING_RATE) -> dict[str, Any]:
    """Build chronological narrative snapshots and update histories after each game."""
    initialize(repository)
    with repository._reader() as connection:
        xrows = [dict(row) for row in connection.execute(
            """SELECT x.*,g.start_date,g.home_team,g.away_team,g.home_points,g.away_points,
                      g.home_pregame_elo,g.away_pregame_elo
               FROM cfb_xpoints_dataset x
               JOIN games g USING(game_id)
               WHERE x.dataset_version=? AND x.season BETWEEN ? AND ?
               ORDER BY g.start_date,g.game_id,x.team""",
            (xpoints.DATASET_VERSION, int(from_season), int(to_season)),
        )]

    by_game: dict[int, list[dict[str, Any]]] = defaultdict(list)
    order: list[int] = []
    for row in xrows:
        gid = int(row["game_id"])
        if gid not in by_game:
            order.append(gid)
        by_game[gid].append(row)

    # Known schedule order for next-opponent identity only.  Strength for that
    # future opponent comes from last_seen_true_elo as of the current kickoff.
    team_schedule: dict[str, list[int]] = defaultdict(list)
    game_teams: dict[int, tuple[str, str]] = {}
    for gid in order:
        rows = by_game[gid]
        if not rows:
            continue
        home, away = str(rows[0]["home_team"]), str(rows[0]["away_team"])
        game_teams[gid] = (home, away)
        team_schedule[home].append(gid)
        team_schedule[away].append(gid)
    schedule_index = {
        (team, gid): idx
        for team, games in team_schedule.items()
        for idx, gid in enumerate(games)
    }

    line_elo: dict[str, float] = defaultdict(lambda: 1500.0)
    last_seen_true_elo: dict[str, float] = {}
    gap_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=3))
    elo_surprise_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=3))
    market_surprise_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=3))
    last_game: dict[str, dict[str, Any]] = {}

    output: list[tuple[Any, ...]] = []
    now = datetime.now(timezone.utc).isoformat()

    for gid in order:
        rows = by_game[gid]
        if len(rows) < 2:
            continue
        home = str(rows[0]["home_team"])
        away = str(rows[0]["away_team"])
        by_team = {str(row["team"]): row for row in rows}
        home_row, away_row = by_team.get(home), by_team.get(away)
        if not home_row or not away_row:
            continue

        home_spread = home_row.get("market_spread")
        if home_spread is not None:
            market_home_margin = -float(home_spread)
            h_line, a_line = _market_line_elo_observation(
                line_elo[home], line_elo[away], market_home_margin,
                home_field_points=home_field_points,
                learning_rate=line_learning_rate,
            )
            line_elo[home], line_elo[away] = h_line, a_line

        pregame_payload: dict[str, dict[str, Any]] = {}
        for team, opponent, side, row in (
            (home, away, "home", home_row),
            (away, home, "away", away_row),
        ):
            is_home = side == "home"
            true_elo = row.get("team_elo")
            opponent_true = row.get("opponent_elo")
            current_line_elo = float(line_elo[team])
            opponent_line = float(line_elo[opponent])
            gap = (current_line_elo - float(true_elo)) if true_elo is not None else None
            prior_gap = gap_history[team][-1] if gap_history[team] else None
            perception_change = (
                gap - prior_gap if gap is not None and prior_gap is not None else None
            )
            perception_momentum = (
                gap - gap_history[team][0]
                if gap is not None and len(gap_history[team]) >= 2 else perception_change
            )

            market_expected = row.get("vegas_margin")
            elo_expected = _expected_margin_from_elo(
                true_elo, opponent_true, is_home=is_home,
                home_field_points=home_field_points)
            market_vs_elo = (
                float(market_expected) - float(elo_expected)
                if market_expected is not None and elo_expected is not None else None
            )

            previous = last_game.get(team)
            prev_opponent_elo = previous.get("opponent_elo") if previous else None
            prev_market_expected = previous.get("market_expected_margin") if previous else None
            prev_elo_surprise = previous.get("elo_surprise") if previous else None
            prev_market_surprise = previous.get("market_surprise") if previous else None
            prev_won = bool(previous.get("won")) if previous else False
            prev_lost = bool(previous is not None and not previous.get("won"))
            statement_win = int(
                bool(previous)
                and prev_won
                and prev_opponent_elo is not None
                and float(prev_opponent_elo) >= 1600.0
                and prev_market_surprise is not None
                and float(prev_market_surprise) >= 10.0
            )
            upset_win = int(
                bool(previous) and prev_won
                and prev_market_expected is not None and float(prev_market_expected) < 0.0
            )
            bad_loss = int(
                bool(previous) and prev_lost
                and prev_market_surprise is not None and float(prev_market_surprise) <= -10.0
            )
            upset_loss = int(
                bool(previous) and prev_lost
                and prev_market_expected is not None and float(prev_market_expected) >= 3.0
            )

            next_opponent = None
            next_opponent_current_elo = None
            idx = schedule_index.get((team, gid))
            schedule = team_schedule.get(team, [])
            if idx is not None and idx + 1 < len(schedule):
                next_gid = schedule[idx + 1]
                next_home, next_away = game_teams[next_gid]
                next_opponent = next_away if next_home == team else next_home
                next_opponent_current_elo = last_seen_true_elo.get(next_opponent)

            lookahead_score = (
                float(next_opponent_current_elo) - float(opponent_true)
                if next_opponent_current_elo is not None and opponent_true is not None else None
            )
            sandwich_score = (
                min(float(prev_opponent_elo), float(next_opponent_current_elo))
                - float(opponent_true)
                if prev_opponent_elo is not None
                and next_opponent_current_elo is not None
                and opponent_true is not None else None
            )

            market_darling = int(gap is not None and gap >= 75.0)
            market_skepticism = int(gap is not None and gap <= -75.0)
            market_chase = int(perception_change is not None and perception_change >= 35.0)
            market_lag = int(perception_change is not None and perception_change <= -35.0)
            letdown = int(
                (statement_win or upset_win)
                and market_expected is not None and float(market_expected) >= 7.0
            )
            bounceback = int(
                (bad_loss or upset_loss)
                and market_expected is not None
            )
            won_big_then_underdog = int(
                statement_win and market_expected is not None and float(market_expected) < 0.0
            )
            lookahead = int(lookahead_score is not None and lookahead_score >= 100.0)
            sandwich = int(sandwich_score is not None and sandwich_score >= 100.0)

            # Ratings disagree materially on the point-margin scale.
            lenses = [
                value for value in (
                    elo_expected,
                    row.get("fpi_margin"),
                    row.get("core_margin"),
                    market_expected,
                ) if value is not None
            ]
            disputed = int(
                len(lenses) >= 3 and (max(float(v) for v in lenses) - min(float(v) for v in lenses)) >= 7.0
            )
            tags = {
                "statement_win": statement_win,
                "upset_win": upset_win,
                "bad_loss": bad_loss,
                "upset_loss": upset_loss,
                "letdown_candidate": letdown,
                "bounceback_candidate": bounceback,
                "won_big_then_underdog": won_big_then_underdog,
                "market_darling": market_darling,
                "market_skepticism": market_skepticism,
                "market_chase": market_chase,
                "market_lag": market_lag,
                "lookahead_candidate": lookahead,
                "sandwich_candidate": sandwich,
                "disputed_team": disputed,
            }
            pregame_payload[team] = {
                "row": row, "opponent": opponent, "side": side,
                "true_elo": true_elo, "opponent_true": opponent_true,
                "line_elo": current_line_elo, "opponent_line_elo": opponent_line,
                "gap": gap, "prior_gap": prior_gap,
                "perception_change": perception_change,
                "perception_momentum": perception_momentum,
                "elo_expected": elo_expected,
                "market_expected": market_expected,
                "market_vs_elo": market_vs_elo,
                "previous_opponent_elo": prev_opponent_elo,
                "previous_market_expected_margin": prev_market_expected,
                "previous_elo_surprise": prev_elo_surprise,
                "previous_market_surprise": prev_market_surprise,
                "rolling_3g_elo_surprise": _mean(elo_surprise_history[team]),
                "rolling_3g_market_surprise": _mean(market_surprise_history[team]),
                "next_opponent": next_opponent,
                "next_opponent_current_elo": next_opponent_current_elo,
                "lookahead_score": lookahead_score,
                "sandwich_score": sandwich_score,
                "tags": tags,
            }

        # Outcomes are attached only after all pregame state for both sides exists.
        home_points, away_points = home_row.get("home_points"), home_row.get("away_points")
        if home_points is None or away_points is None:
            continue
        home_margin = float(home_points) - float(away_points)

        for team in (home, away):
            p = pregame_payload[team]
            actual_margin = home_margin if p["side"] == "home" else -home_margin
            market_expected = p["market_expected"]
            elo_expected = p["elo_expected"]
            market_residual = (
                actual_margin - float(market_expected)
                if market_expected is not None else None
            )
            elo_residual = (
                actual_margin - float(elo_expected) if elo_expected is not None else None
            )
            covered = (
                int(market_residual > 0.0) if market_residual is not None and market_residual != 0 else
                (0 if market_residual == 0 else None)
            )
            won = int(actual_margin > 0.0)
            t = p["tags"]
            row = p["row"]
            output.append((
                gid, team, p["opponent"], p["side"], int(row["season"]), row.get("week"),
                str(row["start_date"]), narrative_version, LINE_ELO_VERSION,
                p["true_elo"], p["opponent_true"], p["line_elo"], p["opponent_line_elo"],
                p["gap"], p["prior_gap"], p["perception_change"], p["perception_momentum"],
                p["elo_expected"], p["market_expected"], p["market_vs_elo"],
                row.get("fpi_margin"), row.get("core_margin"),
                p["previous_opponent_elo"], p["previous_market_expected_margin"],
                p["previous_elo_surprise"], p["previous_market_surprise"],
                p["rolling_3g_elo_surprise"], p["rolling_3g_market_surprise"],
                p["next_opponent"], p["next_opponent_current_elo"],
                p["lookahead_score"], p["sandwich_score"],
                t["statement_win"], t["upset_win"], t["bad_loss"], t["upset_loss"],
                t["letdown_candidate"], t["bounceback_candidate"],
                t["won_big_then_underdog"], t["market_darling"],
                t["market_skepticism"], t["market_chase"], t["market_lag"],
                t["lookahead_candidate"], t["sandwich_candidate"], t["disputed_team"],
                json.dumps(_tag_names(t), sort_keys=True),
                actual_margin, market_residual, elo_residual, covered, won, now,
            ))

        for team in (home, away):
            p = pregame_payload[team]
            actual_margin = home_margin if p["side"] == "home" else -home_margin
            market_expected = p["market_expected"]
            elo_expected = p["elo_expected"]
            market_surprise = (
                actual_margin - float(market_expected) if market_expected is not None else None
            )
            elo_surprise = (
                actual_margin - float(elo_expected) if elo_expected is not None else None
            )
            if p["gap"] is not None:
                gap_history[team].append(float(p["gap"]))
            if market_surprise is not None:
                market_surprise_history[team].append(market_surprise)
            if elo_surprise is not None:
                elo_surprise_history[team].append(elo_surprise)
            last_game[team] = {
                "opponent": p["opponent"],
                "opponent_elo": p["opponent_true"],
                "market_expected_margin": market_expected,
                "market_surprise": market_surprise,
                "elo_surprise": elo_surprise,
                "won": actual_margin > 0.0,
            }
            if p["true_elo"] is not None:
                last_seen_true_elo[team] = float(p["true_elo"])

    columns = [
        "game_id","team","opponent","side","season","week","kickoff",
        "narrative_version","line_elo_version",
        "true_elo","opponent_true_elo","line_elo","opponent_line_elo",
        "line_minus_true_elo","prior_line_minus_true_elo","perception_change",
        "perception_momentum_3g","elo_expected_margin","market_expected_margin",
        "market_vs_elo_margin","fpi_margin","core_margin","previous_opponent_elo",
        "previous_market_expected_margin","previous_elo_surprise","previous_market_surprise",
        "rolling_3g_elo_surprise","rolling_3g_market_surprise","next_opponent",
        "next_opponent_current_elo","lookahead_score","sandwich_score",
        "statement_win","upset_win","bad_loss","upset_loss","letdown_candidate",
        "bounceback_candidate","won_big_then_underdog","market_darling",
        "market_skepticism","market_chase","market_lag","lookahead_candidate",
        "sandwich_candidate","disputed_team","tags_json","actual_margin",
        "market_margin_residual","elo_margin_residual","covered","won","built_at",
    ]
    placeholders = ",".join("?" for _ in columns)
    with repository.transaction() as connection:
        connection.execute(
            """DELETE FROM cfb_narrative_state
               WHERE narrative_version=? AND season BETWEEN ? AND ?""",
            (narrative_version, int(from_season), int(to_season)),
        )
        connection.executemany(
            f"INSERT INTO cfb_narrative_state({','.join(columns)}) VALUES({placeholders})",
            output,
        )
    return {
        "narrative_version": narrative_version,
        "line_elo_version": LINE_ELO_VERSION,
        "from_season": int(from_season),
        "to_season": int(to_season),
        "rows": len(output),
        "home_field_points": float(home_field_points),
        "line_learning_rate": float(line_learning_rate),
        "elo_points_per_score_point": ELO_POINTS_PER_SCORE_POINT,
    }


CATEGORY_COLUMNS = (
    "statement_win","upset_win","bad_loss","upset_loss","letdown_candidate",
    "bounceback_candidate","won_big_then_underdog","market_darling",
    "market_skepticism","market_chase","market_lag","lookahead_candidate",
    "sandwich_candidate","disputed_team",
)


def report(repository, *, test_season: int = 2025,
           narrative_version: str = NARRATIVE_VERSION,
           min_train_rows: int = 30, min_test_rows: int = 12) -> dict[str, Any]:
    """Measure narrative states and state interactions on a held-out season."""
    initialize(repository)
    with repository._reader() as connection:
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM cfb_narrative_state
               WHERE narrative_version=?
               ORDER BY season,week,kickoff,game_id,side""",
            (narrative_version,),
        )]
    train = [r for r in rows if int(r["season"]) < int(test_season)]
    test = [r for r in rows if int(r["season"]) == int(test_season)]

    categories = {}
    for category in CATEGORY_COLUMNS:
        train_rows = [r for r in train if int(r.get(category) or 0)]
        test_rows = [r for r in test if int(r.get(category) or 0)]
        train_summary = _category_summary(train_rows)
        test_summary = _category_summary(test_rows)
        categories[category] = {
            "train": train_summary,
            "test": test_summary,
            "eligible": (
                train_summary["n"] >= int(min_train_rows)
                and test_summary["n"] >= int(min_test_rows)
            ),
            "direction_persisted": (
                (train_summary["mean_market_residual"] > 0) ==
                (test_summary["mean_market_residual"] > 0)
                if train_summary["mean_market_residual"] is not None
                and test_summary["mean_market_residual"] is not None else None
            ),
        }

    # Team and opponent narrative interaction.  Use only the most interpretable
    # single-tag pairs; multi-tag state remains available in tags_json.
    by_game_team = {(int(r["game_id"]), str(r["team"])): r for r in rows}
    interactions: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"train": [], "test": []})
    for r in rows:
        opponent = by_game_team.get((int(r["game_id"]), str(r["opponent"])))
        if not opponent:
            continue
        own_tags = [c for c in CATEGORY_COLUMNS if int(r.get(c) or 0)]
        opp_tags = [c for c in CATEGORY_COLUMNS if int(opponent.get(c) or 0)]
        bucket = "test" if int(r["season"]) == int(test_season) else (
            "train" if int(r["season"]) < int(test_season) else None)
        if not bucket:
            continue
        for own in own_tags:
            for opp in opp_tags:
                interactions[(own, opp)][bucket].append(r)

    interaction_rows = []
    for (own, opp), buckets in interactions.items():
        train_summary = _category_summary(buckets["train"])
        test_summary = _category_summary(buckets["test"])
        if train_summary["n"] < int(min_train_rows) or test_summary["n"] < int(min_test_rows):
            continue
        interaction_rows.append({
            "team_narrative": own,
            "opponent_narrative": opp,
            "train": train_summary,
            "test": test_summary,
            "direction_persisted": (
                (train_summary["mean_market_residual"] > 0) ==
                (test_summary["mean_market_residual"] > 0)
            ),
        })
    interaction_rows.sort(
        key=lambda item: abs(float(item["test"]["mean_market_residual"] or 0.0)),
        reverse=True)

    gap_pairs = [
        (r.get("line_minus_true_elo"), r.get("market_margin_residual")) for r in test
    ]
    change_pairs = [
        (r.get("perception_change"), r.get("market_margin_residual")) for r in test
    ]
    momentum_pairs = [
        (r.get("perception_momentum_3g"), r.get("market_margin_residual")) for r in test
    ]
    market_vs_elo_pairs = [
        (r.get("market_vs_elo_margin"), r.get("market_margin_residual")) for r in test
    ]

    def gap_bin(value: float | None) -> str | None:
        if value is None:
            return None
        value = float(value)
        if value <= -100: return "<=-100"
        if value <= -50: return "-100_to_-50"
        if value < 50: return "-50_to_50"
        if value < 100: return "50_to_100"
        return ">=100"

    bins: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in test:
        label = gap_bin(row.get("line_minus_true_elo"))
        if label:
            bins[label].append(row)

    return {
        "narrative_version": narrative_version,
        "test_season": int(test_season),
        "train_seasons": (
            [min(int(r["season"]) for r in train), max(int(r["season"]) for r in train)]
            if train else None
        ),
        "training_rows": len(train),
        "test_rows": len(test),
        "line_elo_predictiveness": {
            "line_minus_true_elo_vs_market_residual_correlation": _pearson(gap_pairs),
            "perception_change_vs_market_residual_correlation": _pearson(change_pairs),
            "perception_momentum_3g_vs_market_residual_correlation": _pearson(momentum_pairs),
            "market_vs_elo_margin_vs_market_residual_correlation": _pearson(market_vs_elo_pairs),
            "heldout_gap_bins": {label: _category_summary(bin_rows)
                                 for label, bin_rows in sorted(bins.items())},
        },
        "categories": categories,
        "interactions": interaction_rows[:50],
        "notes": [
            "All narrative flags describe information available before the target game.",
            "Line Elo is a filtered rating inferred from closing-line expected margins, not actual outcomes.",
            "True Elo comes from the existing pregame Elo stored on games/xPoints.",
            "Next-opponent identity comes from the known schedule; next-opponent strength uses only the last Elo observed before the current kickoff.",
            "Category results are descriptive research until they persist out of sample with adequate counts.",
        ],
    }
