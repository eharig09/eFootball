"""The analytics steps had no way to run in production.

The hourly trigger drives the segments in `tracked_refresh`, and none of them
named `pbp`, `epa`, `coordinators` or the rest. The only other route -- asking
the web hook for a "heavy" profile -- is remapped to the core segment. So the
box score's EPA, its turning points, the middle-of-field splits and the
coordinator tendencies had no path to the deployed database at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from sports_aggregator.bootstrap import steps
from sports_aggregator.tracked_refresh import (
    ANALYTICS_STEPS, CONTENT_STEPS, CORE_STEPS, MODEL_STEPS, PROJECTION_STEPS,
    ROSTER_STEPS, SEGMENTS,
    _segment_for_light,
)


def test_every_analytics_step_is_a_real_refresh_step():
    plan = {step.name for step in steps(2026) if "refresh" in step.phases}
    missing = [name for name in ANALYTICS_STEPS if name not in plan]
    assert not missing, missing


def test_every_projection_step_is_a_real_refresh_step():
    plan = {step.name for step in steps(2026) if "refresh" in step.phases}
    missing = [name for name in PROJECTION_STEPS if name not in plan]
    assert not missing, missing


def test_projection_actuals_follow_pbp_derivation():
    order = [step.name for step in steps(2026) if step.name in PROJECTION_STEPS]
    assert order.index("pbp") < order.index("pbp-derive")
    for name in ("team-pace", "team-scoring", "team-special-teams", "team-drive-outcomes"):
        assert order.index("pbp-derive") < order.index(name)


def test_the_core_segment_names_the_pregame_snapshot():
    """It has to run on the most frequent segment there is -- a snapshot stage
    missed before kickoff cannot be recaptured."""
    plan = {step.name for step in steps(2026) if "refresh" in step.phases}
    assert "pregame-snapshot" in CORE_STEPS
    assert set(CORE_STEPS) <= plan


def test_the_play_ingest_comes_before_anything_derived_from_it():
    """`_run_low_memory_phase` keeps the plan's order, not this list's, so the
    guarantee is that the plan puts them in a workable order."""
    order = [step.name for step in steps(2026) if step.name in ANALYTICS_STEPS]
    assert order.index("pbp") < order.index("pbp-derive")
    assert order.index("pbp-derive") < order.index("epa")
    # play-detail parses the raw plays; build-tendencies rolls those up against
    # the ep-v2 scores, so both must land after their inputs.
    assert order.index("pbp") < order.index("play-detail")
    assert order.index("play-detail") < order.index("build-tendencies")
    assert order.index("epa") < order.index("build-tendencies")


def test_the_expensive_steps_are_scheduled_at_the_quietest_hour():
    moment = datetime(2026, 9, 3, 2, tzinfo=ZoneInfo("America/New_York"))
    assert _segment_for_light(moment.astimezone(timezone.utc)) == "analytics"


@pytest.mark.parametrize("hour,expected", [
    (6, "core"), (10, "content"), (12, "rosters"), (22, "stats"), (23, "models"),
])
def test_the_other_segments_keep_their_hours(hour, expected):
    moment = datetime(2026, 9, 3, hour, tzinfo=ZoneInfo("America/New_York"))
    assert _segment_for_light(moment.astimezone(timezone.utc)) == expected


def test_no_refresh_step_is_left_without_a_way_to_run():
    """The gap this closes, stated as the rule it broke. A step in the refresh
    phase that no segment names and no profile reaches will never run, however
    long the deployment lives.
    """
    reachable = (set(CONTENT_STEPS) | set(ROSTER_STEPS) | set(MODEL_STEPS)
                 | set(ANALYTICS_STEPS) | set(PROJECTION_STEPS) | set(CORE_STEPS)
                 # Run by their own splitters inside a segment, or by a profile
                 # of their own rather than by name.
                 | {"cfbd-sync", "cfbd-current-player-stats", "local-articles"})
    orphans = sorted(step.name for step in steps(2026)
                     if "refresh" in step.phases and step.name not in reachable)
    assert orphans == ["bluesky-resolve", "media-seed", "media-validate",
                       "reddit-validate"], orphans


def test_the_analytics_hour_is_in_the_trigger_window():
    """The trigger only calls at the hours in CFB_REFRESH_HOURS, so a segment
    scheduled outside that list is scheduled for never."""
    render = Path("render.yaml").read_text(encoding="utf-8")
    hours = next(line.split("value:")[1].strip()
                 for line in render.splitlines()
                 if "value:" in line and line.strip().startswith("value: 2,"))
    assert "2" in hours.split(",")


def test_a_segment_can_be_asked_for_by_name(tmp_path):
    """A backfill should not have to wait for its hour to come round."""
    from sports_aggregator import tracked_refresh
    with patch.object(tracked_refresh, "_run_segment",
                      return_value={"status": "success", "exit_code": 0}) as run:
        tracked_refresh.main(["--season", "2026", "--segment", "analytics"])
    assert run.call_args.args[0] == "analytics"


def test_heavy_with_no_segment_runs_every_maintenance_segment(tmp_path):
    """`profile=heavy` used to silently narrow to just the core segment --
    exactly the route an operator reaches for to catch a stale database up in
    one go -- so pbp/EPA/tendencies/box-score never ran no matter how it was
    invoked."""
    from sports_aggregator.tracked_refresh import HEAVY_SEGMENTS, SEGMENTS

    assert "news" not in HEAVY_SEGMENTS
    assert set(HEAVY_SEGMENTS) == set(SEGMENTS) - {"news"}

    from sports_aggregator import tracked_refresh
    with patch.object(tracked_refresh, "_run_segment",
                      return_value={"status": "success", "exit_code": 0,
                                    "profile": "x", "seconds": 1.0}) as run:
        exit_code = tracked_refresh.main(["--season", "2026", "--profile", "heavy"])
    called = [call.args[0] for call in run.call_args_list]
    assert called == list(HEAVY_SEGMENTS)
    assert exit_code == 0


def test_a_required_failure_in_any_segment_fails_the_heavy_run():
    from sports_aggregator import tracked_refresh

    def fake_run_segment(segment, *_args, **_kwargs):
        failed = segment == "models"
        return {"status": "failed" if failed else "success",
                "exit_code": 1 if failed else 0, "profile": segment, "seconds": 1.0,
                "required_failure_count": 1 if failed else 0, "degraded_count": 0}

    with patch.object(tracked_refresh, "_run_segment", side_effect=fake_run_segment):
        exit_code = tracked_refresh.main(["--season", "2026", "--profile", "heavy"])
    assert exit_code == 1


def test_an_unknown_segment_is_refused():
    from sports_aggregator import tracked_refresh
    with pytest.raises(SystemExit):
        tracked_refresh.main(["--season", "2026", "--segment", "nonsense"])


def test_analytics_is_one_of_the_named_segments():
    assert "analytics" in SEGMENTS


def test_projections_is_one_of_the_named_segments():
    assert "projections" in SEGMENTS


def test_render_has_a_projection_refresh_trigger():
    render = Path("render.yaml").read_text(encoding="utf-8")
    assert "name: cfb-projection-refresh-trigger" in render
    assert "value: projections" in render
    assert 'schedule: "15 */2 * * *"' in render


def test_the_hook_passes_a_requested_segment_through():
    from app import create_app
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_REFRESH_TOKEN": "t", "CFB_DEFAULT_SEASON": 2026})
    with patch("app.subprocess.Popen") as popen:
        response = app.test_client().post(
            "/internal/cfb-refresh?profile=light&segment=analytics",
            headers={"Authorization": "Bearer t"})
    assert response.status_code == 202
    assert response.get_json()["segment"] == "analytics"
    command = popen.call_args.args[0]
    assert "--segment" in command and "analytics" in command


def test_the_hook_refuses_a_segment_it_does_not_have():
    from app import create_app
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_REFRESH_TOKEN": "t", "CFB_DEFAULT_SEASON": 2026})
    with patch("app.subprocess.Popen") as popen:
        response = app.test_client().post(
            "/internal/cfb-refresh?segment=everything",
            headers={"Authorization": "Bearer t"})
    assert response.status_code == 400
    popen.assert_not_called()


def test_asking_for_nothing_in_particular_still_works():
    """The trigger posts without a segment and must keep behaving as before."""
    from app import create_app
    app = create_app({"TESTING": True, "REGISTER_LEGACY_DASHBOARDS": False,
                      "CFB_REFRESH_TOKEN": "t", "CFB_DEFAULT_SEASON": 2026})
    with patch("app.subprocess.Popen") as popen:
        response = app.test_client().post(
            "/internal/cfb-refresh", headers={"Authorization": "Bearer t"})
    assert response.status_code == 202
    assert "segment" not in response.get_json()
    assert "--segment" not in popen.call_args.args[0]
