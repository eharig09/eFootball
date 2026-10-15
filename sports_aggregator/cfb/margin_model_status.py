"""What the live margin model (margin-v2) can actually use in this database.

The game page's Football Lab margin comes from the richest FEATURE_SETS tier that has a fitted model, and a
tier is only fitted when enough history rows carry all of its inputs. A database missing one input in its
history (FPI for prior seasons, say) silently lands on a poorer tier and projects differently from one that
has it, with nothing on the page to say why. This reads the same cached fit the game pages use and lays the
facts side by side.
"""

from __future__ import annotations

from typing import Any

from sports_aggregator.cfb import derived_cache
from sports_aggregator.cfb.live_margin_calibration import (
    FEATURE_SETS, MODEL_VERSION, _historical_rows, fit_models,
)


def _season(repository) -> int | None:
    with repository._reader() as connection:
        row = connection.execute("SELECT MAX(season) FROM games").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _fitted_models(repository, season: int) -> dict[str, dict[str, Any] | None]:
    # Same name and key predict_live() uses, so this is one shared fit rather than a second one.
    return derived_cache.derived(
        repository, "live_margin_models",
        lambda: fit_models(_historical_rows(repository, target_season=season)),
        season,
    )


def _rows(repository, sql: str) -> list[Any]:
    try:
        with repository._reader() as connection:
            return connection.execute(sql).fetchall()
    except Exception:
        return []


def margin_model_status(repository) -> dict[str, Any]:
    """Tier-by-tier fit status plus the history the richer tiers depend on. Never raises."""
    season = _season(repository)
    if season is None:
        return {"available": False, "reason": "No games are stored yet."}
    try:
        models = _fitted_models(repository, season)
    except Exception as exc:
        return {"available": False, "reason": f"The margin model could not be fitted: {str(exc)[:160]}"}

    tiers = []
    for label, features in FEATURE_SETS:
        model = models.get(label)
        tiers.append({
            "label": label, "features": ", ".join(features),
            "fitted": model is not None,
            "training_rows": int(model["training_rows"]) if model else None,
        })
    best = next((tier["label"] for tier in tiers if tier["fitted"]), None)
    richest = FEATURE_SETS[0][0]

    fpi = [{"season": int(r[0]), "rows": int(r[1])} for r in _rows(
        repository, "SELECT season,COUNT(*) FROM fpi_game_projections GROUP BY season ORDER BY season")]
    backtest = [{"season": int(r[0]), "games": int(r[1]), "inputs_version": str(r[2] or "")} for r in _rows(
        repository, "SELECT season,games,inputs_version FROM cfb_projection_backtest_meta ORDER BY season")]

    history = [row["season"] for row in backtest if row["season"] < season]
    fpi_seasons = {row["season"] for row in fpi}
    no_fpi = [s for s in history if s not in fpi_seasons]

    if best is None:
        note = "No tier has a fitted model, so every game falls back to the raw projection."
    elif best == richest:
        note = f"The richest tier ({richest}) is fitted; games whose inputs are all present use it."
    else:
        note = (f"The richest tier ({richest}) has no fitted model here, so no game can use it: the best "
                f"reachable tier is {best}.")
        if no_fpi:
            note += (" Every richer tier needs FPI, and there is no FPI history for "
                     + ", ".join(str(s) for s in no_fpi) + ".")
    return {
        "available": True, "season": season, "model_version": MODEL_VERSION,
        "tiers": tiers, "best": best, "richest": richest, "note": note,
        "fpi": fpi, "backtest": backtest, "missing_fpi_seasons": no_fpi,
    }
