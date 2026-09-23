"""Event-aligned expected points and EPA (ep-v2).

The provider mixes pre-play football state (down/distance/field position) with
score fields that are often post-play, stale, corrected on later rows, or
repeated on administrative rows. ep-v1 used consecutive raw score states and
therefore could assign touchdown/safety points to the row immediately before the
actual scoring event.

ep-v2 keeps the same compact state representation, but derives next-score
training targets from the scoring event itself. EPA is likewise aligned to the
current football event and advances to the next usable football state rather
than the next raw provider row.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import math
import re
from typing import Any

from sports_aggregator.cfb.expected_points_v2 import initialize, state_key

MODEL_VERSION = "ep-v2"
WRITE_BATCH = 1000
# Expanded-history temporal tuning (2015-2023 -> 2024, confirmed with
# 2015-2024 -> 2025) favored lighter shrinkage than the original four-season
# fit. See the event-aligned validate command for reproducible holdout checks.
MIN_CELL = 10

_ADMIN_PLAY_TYPES = ("kickoff", "extra point", "two point", "end period", "timeout")
_ADMIN_TEXT = ("end period", "end of quarter", "end of half", "end of game", "end of regulation")


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_home_offense(row: dict[str, Any]) -> bool | None:
    offense = str(row.get("offense") or "")
    home = str(row.get("home_team") or "")
    away = str(row.get("away_team") or "")
    if offense and home and offense == home:
        return True
    if offense and away and offense == away:
        return False
    return None


def _segment(row: dict[str, Any]) -> int:
    period = _int(row.get("period")) or 0
    if period in (1, 2):
        return 1
    if period in (3, 4):
        return 2
    return 0


def _text(row: dict[str, Any]) -> str:
    return f"{row.get('play_type') or ''} {row.get('play_text') or ''}".casefold()


def _touchdown_points(text: str) -> int:
    # Touchdown itself is six. Providers often append the conversion to the same
    # text row, so include it only when success is explicit or conventional.
    points = 6
    if "two-point conversion" in text or "two point conversion" in text:
        if not any(term in text for term in ("failed", "no good", "unsuccessful")):
            points += 2
    elif "kick attempt good" in text or re.search(r"\([^)]*\bKICK\s*\)", text, re.I):
        if not any(term in text for term in ("kick attempt failed", "kick attempt no good", "no good")):
            points += 1
    return points


def _scoring_event(row: dict[str, Any]) -> tuple[int, str] | None:
    """Return (points, beneficiary), beneficiary is offense or defense.

    This is intentionally event-text/type driven rather than scoreboard-delta
    driven because provider scoreboard snapshots can lag or be corrected later.
    """
    text = _text(row)
    play_type = str(row.get("play_type") or "").casefold()

    if "touchdown" in text:
        points = _touchdown_points(text)
        defensive = any(token in play_type for token in (
            "fumble return", "interception return", "punt return", "kickoff return",
        )) or (
            "return" in text and any(token in text for token in ("intercept", "fumble", "punt", "kickoff"))
        )
        return points, "defense" if defensive else "offense"

    if "safety" in text:
        return 2, "defense"

    if "field goal" in text and not any(term in text for term in ("miss", "no good", "blocked")):
        return 3, "offense"

    if "extra point" in play_type or "pat" == play_type.strip():
        if not any(term in text for term in ("miss", "failed", "no good", "blocked")):
            return 1, "offense"

    if "two point" in play_type or "two-point" in play_type:
        if not any(term in text for term in ("failed", "no good", "unsuccessful")):
            return 2, "offense"

    return None


def _usable_state(row: dict[str, Any]) -> bool:
    """True when row represents a real pre-play football state.

    Provider descriptions sometimes append a timeout, PAT, or conversion to the
    actual scoring play. A recognized scoring event must therefore remain usable
    even when its text contains those administrative fragments. True timeout,
    kickoff, standalone conversion, end-period, and NO PLAY rows remain excluded.
    """
    key = state_key(row)
    if 0 in key or _segment(row) not in (1, 2):
        return False

    play_type = str(row.get("play_type") or "").casefold()
    text = _text(row)

    # The event classifier is authoritative for bundled scoring rows. This must
    # happen before searching the free text for appended timeout/conversion prose.
    if _scoring_event(row) is not None:
        return True

    if "no play" in text:
        return False
    if any(token in play_type for token in _ADMIN_PLAY_TYPES):
        return False
    if any(token in text for token in _ADMIN_TEXT):
        return False
    return True


def _home_net_event(row: dict[str, Any]) -> float | None:
    event = _scoring_event(row)
    if event is None:
        return None
    points, beneficiary = event
    home_offense = _is_home_offense(row)
    if home_offense is None:
        return None
    beneficiary_is_home = home_offense if beneficiary == "offense" else not home_offense
    return float(points if beneficiary_is_home else -points)


def _iter_games(repository, from_season: int | None, to_season: int | None):
    clauses = ["g.completed=1", "g.home_points IS NOT NULL", "g.away_points IS NOT NULL"]
    params: list[Any] = []
    if from_season is not None:
        clauses.append("p.season>=?")
        params.append(int(from_season))
    if to_season is not None:
        clauses.append("p.season<=?")
        params.append(int(to_season))
    sql = f"""
      SELECT p.play_id,p.game_id,p.drive_number,p.play_number,p.offense,p.defense,
             p.home_team,p.away_team,p.offense_score,p.defense_score,p.period,
             p.clock_minutes,p.clock_seconds,p.down,p.distance,p.yards_to_goal,
             p.yards_gained,p.scoring,p.play_type,p.play_text,p.season,p.week,
             g.home_points,g.away_points
      FROM cfb_plays p JOIN games g USING(game_id)
      WHERE {' AND '.join(clauses)}
      ORDER BY p.game_id,p.period,p.clock_minutes DESC,p.clock_seconds DESC,
               COALESCE(p.drive_number,0),COALESCE(p.play_number,0),p.play_id
    """
    with closing(repository._connect()) as connection:
        current_game: int | None = None
        rows: list[dict[str, Any]] = []
        for raw in connection.execute(sql, params):
            row = dict(raw)
            game_id = int(row["game_id"])
            if current_game is not None and game_id != current_game:
                yield rows
                rows = []
            rows.append(row)
            current_game = game_id
        if rows:
            yield rows


def _targets_for_game(rows: list[dict[str, Any]]) -> list[float | None]:
    """Offense-perspective next-score targets aligned to the current event."""
    targets: list[float | None] = [None] * len(rows)
    next_event_home: float | None = None
    next_segment: int | None = None

    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        segment = _segment(row)
        if segment == 0:
            continue
        if next_segment is not None and segment != next_segment:
            next_event_home = None

        # The current scoring event is the next score from its own pre-play
        # state, so update before assigning this row's target.
        event_home = _home_net_event(row)
        if event_home is not None:
            next_event_home = event_home

        home_offense = _is_home_offense(row)
        if home_offense is not None and _usable_state(row):
            if next_event_home is None:
                targets[index] = 0.0
            else:
                targets[index] = next_event_home if home_offense else -next_event_home
        next_segment = segment
    return targets


def _weighted(candidates: list[tuple[float, int]]) -> float | None:
    if not candidates:
        return None
    total = sum(n for _, n in candidates)
    return sum(value * n for value, n in candidates) / total if total else None


def _lookup(table: dict[tuple[int, int, int, int], tuple[float, int]],
            key: tuple[int, int, int, int]) -> float | None:
    if 0 in key:
        return None
    if key in table:
        return table[key][0]
    down, _dist, field, time = key
    value = _weighted([(v, n) for (d, _x, f, t), (v, n) in table.items()
                       if d == down and f == field and t == time])
    if value is not None:
        return value
    value = _weighted([(v, n) for (d, _x, f, _t), (v, n) in table.items()
                       if d == down and f == field])
    if value is not None:
        return value
    value = _weighted([(v, n) for (_d, _x, f, t), (v, n) in table.items()
                       if f == field and t == time])
    if value is not None:
        return value
    return _weighted([(v, n) for (_d, _x, f, _t), (v, n) in table.items() if f == field])


def _load_model(repository, model_version: str):
    with closing(repository._connect()) as connection:
        rows = connection.execute("""
          SELECT down_bucket,distance_bucket,field_bucket,time_bucket,expected_points,samples
          FROM cfb_expected_points_state WHERE model_version=?
        """, (model_version,)).fetchall()
    return {
        (int(r[0]), int(r[1]), int(r[2]), int(r[3])): (float(r[4]), int(r[5]))
        for r in rows
    }


def fit_model(repository, *, from_season: int | None = None,
              to_season: int | None = None, model_version: str = MODEL_VERSION,
              min_cell: int = MIN_CELL) -> dict[str, Any]:
    initialize(repository)
    cells: dict[tuple[int, int, int, int], list[float]] = {}
    global_sum = 0.0
    global_count = 0
    games = 0
    event_rows = 0

    for rows in _iter_games(repository, from_season, to_season):
        games += 1
        targets = _targets_for_game(rows)
        event_rows += sum(1 for row in rows if _home_net_event(row) is not None)
        for row, target in zip(rows, targets):
            if target is None:
                continue
            key = state_key(row)
            if 0 in key:
                continue
            bucket = cells.setdefault(key, [0.0, 0.0])
            bucket[0] += float(target)
            bucket[1] += 1.0
            global_sum += float(target)
            global_count += 1

    global_mean = global_sum / global_count if global_count else 0.0
    now = datetime.now(timezone.utc).isoformat()
    output: list[tuple[Any, ...]] = []
    for key, (cell_sum, raw_count) in cells.items():
        n = int(raw_count)
        raw = cell_sum / n
        weight = n / (n + max(1, int(min_cell)))
        estimate = weight * raw + (1.0 - weight) * global_mean
        output.append((model_version, *key, n, estimate, now))

    with closing(repository._connect()) as connection:
        connection.execute("DELETE FROM cfb_expected_points_state WHERE model_version=?", (model_version,))
        connection.executemany("""
          INSERT INTO cfb_expected_points_state
          (model_version,down_bucket,distance_bucket,field_bucket,time_bucket,samples,expected_points,fitted_at)
          VALUES(?,?,?,?,?,?,?,?)
        """, output)
        connection.commit()

    return {
        "model_version": model_version,
        "from_season": from_season,
        "to_season": to_season,
        "games": games,
        "plays": global_count,
        "cells": len(output),
        "event_rows": event_rows,
        "global_expected_points": round(global_mean, 4),
    }


def _next_usable_indices(rows: list[dict[str, Any]]) -> list[int | None]:
    result: list[int | None] = [None] * len(rows)
    next_index: int | None = None
    next_segment: int | None = None
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        segment = _segment(row)
        if next_segment is not None and segment != next_segment:
            next_index = None
        result[index] = next_index
        if _usable_state(row):
            next_index = index
            next_segment = segment
    return result


def score_plays(repository, *, from_season: int | None = None,
                to_season: int | None = None, model_version: str = MODEL_VERSION) -> dict[str, Any]:
    initialize(repository)
    table = _load_model(repository, model_version)
    if not table:
        return {"model_version": model_version, "scored": 0, "reason": "model_not_fitted"}

    clauses: list[str] = []
    params: list[Any] = []
    if from_season is not None:
        clauses.append("season>=?")
        params.append(int(from_season))
    if to_season is not None:
        clauses.append("season<=?")
        params.append(int(to_season))
    with closing(repository._connect()) as connection:
        if clauses:
            connection.execute(f"""
              DELETE FROM cfb_play_epa WHERE model_version=? AND play_id IN (
                SELECT play_id FROM cfb_plays WHERE {' AND '.join(clauses)}
              )
            """, (model_version, *params))
        else:
            connection.execute("DELETE FROM cfb_play_epa WHERE model_version=?", (model_version,))
        connection.commit()

    now = datetime.now(timezone.utc).isoformat()
    batch: list[tuple[Any, ...]] = []
    scored = 0
    event_scored = 0
    possession_changes = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        with closing(repository._connect()) as writer:
            writer.executemany("""
              INSERT OR REPLACE INTO cfb_play_epa
              (play_id,model_version,ep_before,ep_after,immediate_net_points,possession_changed,epa,scored_at)
              VALUES(?,?,?,?,?,?,?,?)
            """, batch)
            writer.commit()
        batch = []

    for rows in _iter_games(repository, from_season, to_season):
        next_indices = _next_usable_indices(rows)
        for index, row in enumerate(rows):
            if not _usable_state(row):
                continue
            before = _lookup(table, state_key(row))
            if before is None:
                continue
            home_offense = _is_home_offense(row)
            if home_offense is None:
                continue

            home_event = _home_net_event(row)
            immediate_home = 0.0 if home_event is None else float(home_event)
            immediate = immediate_home if home_offense else -immediate_home
            event_scored += int(home_event is not None)

            next_index = next_indices[index]
            signed_after = 0.0
            changed = False
            if next_index is not None:
                next_row = rows[next_index]
                if _segment(next_row) == _segment(row):
                    next_ep = _lookup(table, state_key(next_row))
                    if next_ep is not None:
                        changed = str(next_row.get("offense") or "") != str(row.get("offense") or "")
                        signed_after = -float(next_ep) if changed else float(next_ep)

            epa = float(immediate) + float(signed_after) - float(before)
            batch.append((row["play_id"], model_version, before, signed_after, immediate,
                          int(changed), epa, now))
            scored += 1
            possession_changes += int(changed)
            if len(batch) >= WRITE_BATCH:
                flush()
    flush()

    return {
        "model_version": model_version,
        "from_season": from_season,
        "to_season": to_season,
        "scored": scored,
        "event_scored": event_scored,
        "possession_changes": possession_changes,
    }


def validate_model(repository, *, from_season: int | None = None,
                   to_season: int | None = None,
                   model_version: str = MODEL_VERSION) -> dict[str, Any]:
    """Evaluate event-aligned EP against realized next-score targets.

    Use a separately named model fitted only on seasons before the requested
    range when this is used as a temporal holdout.
    """
    initialize(repository)
    table = _load_model(repository, model_version)
    if not table:
        return {"model_version": model_version, "plays": 0, "reason": "model_not_fitted"}

    n = 0
    sum_prediction = sum_target = absolute_error = squared_error = 0.0
    by_field: dict[int, list[float]] = {}
    for rows in _iter_games(repository, from_season, to_season):
        targets = _targets_for_game(rows)
        for row, target in zip(rows, targets):
            if target is None:
                continue
            prediction = _lookup(table, state_key(row))
            if prediction is None:
                continue
            error = float(prediction) - float(target)
            n += 1
            sum_prediction += float(prediction)
            sum_target += float(target)
            absolute_error += abs(error)
            squared_error += error * error
            field = state_key(row)[2]
            acc = by_field.setdefault(field, [0.0, 0.0, 0.0])
            acc[0] += float(prediction)
            acc[1] += float(target)
            acc[2] += 1.0

    return {
        "model_version": model_version,
        "from_season": from_season,
        "to_season": to_season,
        "overall": {
            "plays": n,
            "mean_prediction": round(sum_prediction / n, 4) if n else None,
            "mean_target": round(sum_target / n, 4) if n else None,
            "mae": round(absolute_error / n, 5) if n else None,
            "rmse": round(math.sqrt(squared_error / n), 5) if n else None,
            "bias": round((sum_prediction - sum_target) / n, 5) if n else None,
        },
        "by_field_bucket": {
            str(field): {
                "plays": int(values[2]),
                "mean_prediction": round(values[0] / values[2], 4),
                "mean_target": round(values[1] / values[2], 4),
                "bias": round((values[0] - values[1]) / values[2], 4),
            }
            for field, values in sorted(by_field.items()) if values[2]
        },
        "evaluation_note": (
            "Targets use ep-v2's event-aligned next score within each half; "
            "overtime and administrative states are excluded."
        ),
    }


def audit_game(repository, game_id: int, *, model_version: str = MODEL_VERSION) -> list[dict[str, Any]]:
    """Return event rows with ep-v2 values for quick attribution validation."""
    initialize(repository)
    with closing(repository._connect()) as connection:
        rows = [dict(r) for r in connection.execute("""
          SELECT p.play_id,p.period,p.clock_minutes,p.clock_seconds,p.offense,p.defense,
                 p.play_type,p.play_text,p.scoring,p.offense_score,p.defense_score,
                 e.ep_before,e.ep_after,e.immediate_net_points,e.possession_changed,e.epa
          FROM cfb_plays p
          LEFT JOIN cfb_play_epa e ON e.play_id=p.play_id AND e.model_version=?
          WHERE p.game_id=? ORDER BY p.period,p.clock_minutes DESC,p.clock_seconds DESC,
                 COALESCE(p.drive_number,0),COALESCE(p.play_number,0),p.play_id
        """, (model_version, int(game_id))).fetchall()]
    return [row for row in rows if _scoring_event(row) is not None]
