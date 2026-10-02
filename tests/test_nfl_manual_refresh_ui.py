"""The NFL data-status manual refresh controls: the page markup, and the button logic run under Node."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "nfl_data_status.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "nfl_manual_refresh.js").read_text(encoding="utf-8")


def test_the_page_no_longer_depends_on_browser_dialogs():
    # window.confirm / prompt / alert can be silently suppressed by the browser, which made a click look dead
    for banned in ("window.confirm", "window.prompt", "window.alert"):
        assert banned not in TEMPLATE
        assert banned not in SCRIPT
    assert "nfl_manual_refresh.js" in TEMPLATE


def test_the_page_has_the_inline_controls_the_script_wires_up():
    for element_id in ("nfl-refresh-confirm", "nfl-refresh-confirm-text", "nfl-refresh-token-wrap",
                       "nfl-refresh-token", "nfl-refresh-go", "nfl-refresh-cancel", "nfl-refresh-result"):
        assert f'id="{element_id}"' in TEMPLATE and f'"{element_id}"' in SCRIPT
    assert 'role="status"' in TEMPLATE and 'aria-live="polite"' in TEMPLATE        # the result is announced to screen readers
    assert 'type="password"' in TEMPLATE                                           # a token is never shown on screen
    section = TEMPLATE.split('id="manual-refresh"')[1].split("</section>")[0]
    segments = re.findall(r"\('([a-z\-]+)','[^']+'\)", section)
    assert "weather" in segments and len(segments) == 8


def test_the_script_still_uses_the_same_endpoints_and_token_keys():
    assert "internal/nfl-refresh?segment=" in SCRIPT and "internal/nfl-refresh-status" in SCRIPT
    assert "nflAuditToken" in SCRIPT and "cfbAuditToken" in SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_button_logic_under_node():
    result = subprocess.run(["node", "--test", str(ROOT / "tests" / "js" / "nfl_manual_refresh.test.js")], capture_output=True, text=True,
                            cwd=ROOT, env={**os.environ, "NODE_NO_WARNINGS": "1"}, timeout=120)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
