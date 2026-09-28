"""Opponent production allowed by offensive position group and observed role."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from sports_aggregator.nfl.repository import NFLRepository


GROUPS = (
    ("WR", "Wide receivers", 4, "receiving_yards"),
    ("TE", "Tight ends", 3, "receiving_yards"),
    ("RB", "Running backs", 3, "scrimmage_yards"),
)
COUNTING_METRICS = (
    "targets", "receptions", "receiving_yards", "receiving_tds",
    "receiving_first_downs", "carries", "rushing_yards", "rushing_tds",
    "rushing_first_downs", "scrimmage_yards", "total_tds",
)


def _group(position: str | None) -> str | None:
    value = (position or "").upper()
    return "RB" if value in {"RB", "FB", "HB"} else value if value in {"WR", "TE"} else None


def _add(target: dict[str, float], row: dict[str, Any]) -> None:
    for metric in COUNTING_METRICS:
        if metric == "scrimmage_yards":
            value = float(row.get("receiving_yards") or 0) + float(row.get("rushing_yards") or 0)
        elif metric == "total_tds":
            value = float(row.get("receiving_tds") or 0) + float(row.get("rushing_tds") or 0)
        else:
            value = float(row.get(metric) or 0)
        target[metric] = target.get(metric, 0.0) + value


def _ratio(value: float, baseline: float) -> float | None:
    return 100.0 * value / baseline if baseline > 0 else None


def _prepared(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]]]:
    data = []
    players: dict[tuple[str, str, str], dict[str, Any]] = {}
    for source in rows:
        group = _group(source.get("position"))
        if group is None:
            continue
        row = dict(source)
        row["group"] = group
        row["scrimmage_yards"] = float(row.get("receiving_yards") or 0) + float(row.get("rushing_yards") or 0)
        row["total_tds"] = float(row.get("receiving_tds") or 0) + float(row.get("rushing_tds") or 0)
        data.append(row)
        key = (str(row.get("team") or ""), group, str(row.get("player_id") or ""))
        player = players.setdefault(key, {
            "player_id": row.get("player_id"), "player_name": row.get("player_name"),
            "team": row.get("team"), "group": group, "targets": 0.0, "carries": 0.0,
            "receptions": 0.0, "scrimmage_yards": 0.0, "games": set(),
        })
        for metric in ("targets", "carries", "receptions", "scrimmage_yards"):
            player[metric] += float(row.get(metric) or 0)
        player["games"].add(row.get("game_id"))

    role_players: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for player in players.values():
        player["games"] = len(player["games"])
        player["opportunities"] = player["targets"] + player["carries"]
        role_players[(player["team"], player["group"])].append(player)
    limits = {key: limit for key, _label, limit, _primary in GROUPS}
    role_by_player: dict[tuple[str, str, str], str] = {}
    for (team, group), room in role_players.items():
        room.sort(key=lambda item: (
            -item["opportunities"], -item["scrimmage_yards"], str(item["player_name"]),
        ))
        for index, player in enumerate(room, 1):
            player["role"] = f"{group}{index}"
            player["per_game_opportunities"] = player["opportunities"] / player["games"] if player["games"] else 0
            player["per_game_scrimmage_yards"] = player["scrimmage_yards"] / player["games"] if player["games"] else 0
            if index <= limits[group]:
                role_by_player[(team, group, str(player["player_id"]))] = player["role"]
        role_players[(team, group)] = room[:limits[group]]
    for row in data:
        row["role"] = role_by_player.get((row["team"], row["group"], str(row["player_id"])))
    return data, role_players


def _aggregate(data: list[dict[str, Any]]) -> tuple[dict, dict, dict]:
    # team-game production is both the actual amount a defense surrendered and
    # the leave-one-matchup-out baseline describing opponent quality.
    games: dict[tuple[str, str, str, str | None], dict[str, float]] = {}
    defense_games: dict[str, set[str]] = defaultdict(set)
    offense_games: set[tuple[str, str]] = set()
    for row in data:
        defense_games[row["opponent_team"]].add(row["game_id"])
        offense_games.add((row["team"], row["game_id"]))
        total_key = (row["team"], row["game_id"], row["group"], None)
        _add(games.setdefault(total_key, {}), row)
        if row.get("role") is not None:
            role_key = (row["team"], row["game_id"], row["group"], row["role"])
            _add(games.setdefault(role_key, {}), row)

    # Materialize zero rows as real observations. A game with no TE target (or
    # no WR4 contribution) is a defensive result, not missing data, and every
    # league/index denominator must cover the same set of team-games.
    for team, game_id in offense_games:
        for group, _label, limit, _primary in GROUPS:
            games.setdefault((team, game_id, group, None), {})
            for index in range(1, limit + 1):
                games.setdefault((team, game_id, group, f"{group}{index}"), {})

    offense_totals: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for (team, game_id, group, role), values in games.items():
        key = (team, group, role)
        total = offense_totals.setdefault(key, {"games": set(), **{metric: 0.0 for metric in COUNTING_METRICS}})
        total["games"].add(game_id)
        for metric in COUNTING_METRICS:
            total[metric] += values.get(metric, 0.0)
    return games, offense_totals, defense_games


def _profile_from_rows(rows: Iterable[dict[str, Any]], defense: str, *, offense: str | None = None) -> dict[str, Any]:
    data, role_players = _prepared(rows)
    games, offense_totals, defense_games = _aggregate(data)
    sample = len(defense_games.get(defense, set()))
    if not sample:
        return {"team": defense, "games": 0, "groups": [], "has_data": False}

    defense_values: dict[tuple[str, str | None], dict[str, float]] = defaultdict(dict)
    expected_values: dict[tuple[str, str | None], dict[str, float]] = defaultdict(dict)
    league_values: dict[tuple[str, str | None], dict[str, float]] = defaultdict(dict)
    league_games: dict[tuple[str, str | None], int] = defaultdict(int)
    actual_games: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    opponent_by_game = {(row["team"], row["game_id"]): row["opponent_team"] for row in data}

    # League allowed baseline: one observation per defense-game and role.
    for (attack, game_id, group, role), values in games.items():
        key = (group, role)
        league_games[key] += 1
        for metric in COUNTING_METRICS:
            league_values[key][metric] = league_values[key].get(metric, 0.0) + values.get(metric, 0.0)

    for (attack, game_id, group, role), values in games.items():
        opponent = opponent_by_game.get((attack, game_id))
        if opponent != defense:
            continue
        key = (group, role)
        actual_games[key].add(game_id)
        for metric in COUNTING_METRICS:
            defense_values[key][metric] = defense_values[key].get(metric, 0.0) + values.get(metric, 0.0)
            season = offense_totals[(attack, group, role)]
            other_games = len(season["games"] - {game_id})
            league_pg = league_values[key].get(metric, 0.0) / max(1, league_games[key])
            expected = ((season[metric] - values.get(metric, 0.0)) / other_games
                        if other_games else league_pg)
            expected_values[key][metric] = expected_values[key].get(metric, 0.0) + expected

    groups = []
    for group, label, limit, primary in GROUPS:
        rows_out = []
        for role in (None, *(f"{group}{index}" for index in range(1, limit + 1))):
            key = (group, role)
            observations = len(actual_games[key])
            if role is not None and not observations:
                continue
            per_game = {metric: defense_values[key].get(metric, 0.0) / sample for metric in COUNTING_METRICS}
            league = {metric: league_values[key].get(metric, 0.0) / max(1, league_games[key]) for metric in COUNTING_METRICS}
            expected = {metric: expected_values[key].get(metric, 0.0) / sample for metric in COUNTING_METRICS}
            primary_value = per_game[primary]
            primary_league = league[primary]
            primary_expected = expected[primary]
            occupant = next((item for item in role_players.get((offense, group), [])
                             if item.get("role") == role), None) if offense and role else None
            rows_out.append({
                "role": role or f"{group} total", "is_total": role is None,
                "player": occupant, "per_game": per_game,
                "league_per_game": league, "expected_per_game": expected,
                "league_index": _ratio(primary_value, primary_league),
                "opponent_quality_index": _ratio(primary_expected, primary_league),
                "adjusted_allowed_index": _ratio(primary_value, primary_expected),
                "primary_metric": primary,
            })
        if rows_out:
            groups.append({"key": group, "label": label, "primary_metric": primary,
                           "rows": rows_out})
    return {"team": defense, "games": sample, "groups": groups, "has_data": bool(groups),
            "role_method": "Observed season workload before the selected cutoff"}


def position_group_defense(repository: NFLRepository, season: int, defense: str, *,
                           before_week: int | None = None) -> dict[str, Any]:
    return _profile_from_rows(
        repository.position_group_game_stats(season, before_week=before_week), defense,
    )


def position_group_matchups(repository: NFLRepository, season: int, before_week: int,
                            away: str, home: str, *, baseline_season: int) -> list[dict[str, Any]]:
    cutoff = before_week if baseline_season == season else None
    rows = repository.position_group_game_stats(baseline_season, before_week=cutoff)
    return [
        {"offense": offense, "defense": defense,
         "profile": _profile_from_rows(rows, defense, offense=offense)}
        for offense, defense in ((away, home), (home, away))
    ]
