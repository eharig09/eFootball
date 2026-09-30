"""Guards the NFL token consolidation against quietly drifting back.

`nfl_components.css` never had CFB's zero-literal-color discipline
(`tests/test_theme.py`), and a full retrofit isn't in scope here -- the file
carries two overlapping generations of table styling and genuinely one-off
colors (heatmap cells, status pills) that a blanket ban would either miss or
wrongly flag. What *is* in scope: a frequency scan found the same intended
color (a panel background, a border, muted text, white) had drifted into a
dozen-plus near-duplicate literals because nothing enforced the token that
already existed for it. Those specific duplicates were consolidated onto
NFL's existing tokens (`static/nfl.css`); this test is the narrow regression
guard that keeps them from silently reappearing.
"""

from __future__ import annotations

import os
import unittest


STYLESHEET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "nfl_components.css")

#: The exact near-duplicate literals consolidated onto NFL's existing tokens.
#: Not every hex color in the file -- see the module docstring.
CONSOLIDATED_AWAY = (
    "#fff",
    "#101920", "#101a21", "#111c24", "#101820", "#14222b", "#14222a",
    "#0e181f", "#0d171d", "#15232c", "#111b22", "#15222c", "#15212a", "#0c151b",
    "#30434f", "#304655", "#314655", "#344650", "#2b3943", "#293640",
    "#8fa1af", "#7f929f", "#91a4b0", "#8296a4", "#8295a3", "#78909f", "#81939f",
)


class TokenConsolidationTests(unittest.TestCase):
    def setUp(self):
        with open(STYLESHEET, encoding="utf-8") as handle:
            self.css = handle.read()

    def test_consolidated_literals_do_not_reappear(self):
        present = [value for value in CONSOLIDATED_AWAY if value in self.css]
        self.assertEqual(present, [],
                         "these colors were consolidated onto an existing NFL "
                         "token (static/nfl.css) and should not be reintroduced "
                         "as literals")

    def test_the_shared_table_macro_uses_the_panel_token(self):
        """`.table-wrap`/`.table-caption` back the shared `_tables.html` macro
        used by every table on the site; its background should follow the
        theme rather than repeat one of the consolidated literals."""
        self.assertIn("var(--panel)", self.css)

    def test_nfl_tokens_are_still_defined(self):
        """A sanity check that the tokens these literals moved onto still
        exist, so a future rename of `static/nfl.css`'s :root doesn't leave
        this file's `var(--panel)`/`var(--line)`/etc. pointing at nothing."""
        root = os.path.join(os.path.dirname(STYLESHEET), "nfl.css")
        with open(root, encoding="utf-8") as handle:
            tokens = handle.read()
        for name in ("--ink", "--muted", "--surface", "--panel", "--line"):
            self.assertIn(f"{name}:", tokens, name)


if __name__ == "__main__":
    unittest.main()
