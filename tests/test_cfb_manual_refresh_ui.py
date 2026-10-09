"""The CFB data-status manual refresh panel: the page markup, the segments it offers, and the button logic under Node."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sports_aggregator.tracked_refresh import SEGMENTS

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "cfb_data_status.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "cfb_manual_refresh.js").read_text(encoding="utf-8")


def _section():
    return TEMPLATE.split('id="manual-refresh"')[1].split("</section>")[0]


def test_the_page_has_a_manual_refresh_panel_linked_from_the_sub_navigation():
    assert 'href="#manual-refresh"' in TEMPLATE
    assert "cfb_manual_refresh.js" in TEMPLATE


def test_the_panel_offers_every_segment_the_server_can_run_plus_scores_and_results():
    offered = set(re.findall(r"\('([a-z]+)', '[^']+'\)", _section()))
    # the server maps "scores" and "results" onto their profiles; every other name must be a real segment
    assert offered == set(SEGMENTS) | {"scores", "results"}


def test_projections_is_the_first_button_because_its_cron_is_the_one_that_stalls():
    assert re.findall(r"\('([a-z]+)', '[^']+'\)", _section())[0] == "projections"


def test_the_page_has_the_inline_controls_the_script_wires_up():
    for element_id in ("cfb-refresh-confirm", "cfb-refresh-confirm-text", "cfb-refresh-token-wrap",
                       "cfb-refresh-token", "cfb-refresh-go", "cfb-refresh-cancel", "cfb-refresh-result"):
        assert f'id="{element_id}"' in TEMPLATE and f'"{element_id}"' in SCRIPT
    assert 'role="status"' in _section() and 'aria-live="polite"' in _section()
    assert 'type="password"' in _section()                      # a token is never shown on screen


def test_the_new_panel_does_not_depend_on_browser_dialogs():
    # window.confirm / prompt / alert can be silently suppressed by a browser, which makes a click look dead
    for banned in ("window.confirm", "window.prompt", "window.alert"):
        assert banned not in SCRIPT


def test_the_script_uses_the_cfb_endpoints_and_the_shared_token_keys():
    assert "internal/cfb-refresh?profile=light&segment=" in SCRIPT and "internal/cfb-refresh-status" in SCRIPT
    assert "cfbAuditToken" in SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_button_logic_under_node():
    result = subprocess.run(["node", "--test", str(ROOT / "tests" / "js" / "cfb_manual_refresh.test.js")],
                            capture_output=True, text=True, cwd=ROOT,
                            env={**os.environ, "NODE_NO_WARNINGS": "1"}, timeout=120)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
