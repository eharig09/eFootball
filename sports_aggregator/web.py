"""Flask HTML and JSON interfaces for every cataloged league."""

from __future__ import annotations

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for

from sports_aggregator.catalog import get_league, list_leagues
from sports_aggregator.service import AggregationService


league_pages = Blueprint("leagues", __name__)


def _service() -> AggregationService:
    return current_app.extensions["league_aggregation_service"]


def _result_for(slug: str):
    league = get_league(slug)
    if league is None:
        abort(404)
    return league, _service().aggregate(league)


@league_pages.get("/leagues/<league_slug>/")
def league_page(league_slug: str):
    if league_slug == "college-football":
        return redirect(url_for("cfb.today"))
    if league_slug == "nfl":
        return redirect(url_for("nfl.dashboard"))
    league, result = _result_for(league_slug)
    return render_template("league.html", league=league, result=result)


@league_pages.get("/api/v1/leagues")
def league_index_api():
    return jsonify(
        {
            "leagues": [
                {
                    "slug": league.slug,
                    "name": league.name,
                    "sport": league.sport,
                    "abbreviation": league.abbreviation,
                    "url": (url_for("nfl.dashboard") if league.slug == "nfl"
                            else url_for("leagues.league_page", league_slug=league.slug)),
                }
                for league in list_leagues()
            ]
        }
    )


@league_pages.get("/api/v1/leagues/<league_slug>/articles")
def league_articles_api(league_slug: str):
    _, result = _result_for(league_slug)
    limit = min(max(request.args.get("limit", 25, type=int) or 25, 1), 100)
    payload = result.to_dict()
    payload["articles"] = payload["articles"][:limit]
    payload["count"] = len(payload["articles"])
    return jsonify(payload)


@league_pages.get("/api/v1/nfl/articles")
def nfl_articles_api():
    """NFL-native alias for clients that should not depend on generic URLs."""
    return league_articles_api("nfl")
