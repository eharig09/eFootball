"""The neutral retint stylesheet is generated; keep it in step with its sources."""

import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_nfl_neutral_css", ROOT / "scripts" / "build_nfl_neutral_css.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NeutralCssTests(unittest.TestCase):
    def setUp(self):
        self.module = _load()

    def test_only_dark_quiet_blue_grays_are_recolored(self):
        tint = lambda value: self.module.HEX.sub(self.module.retint, value)
        self.assertEqual(tint("#101b23"), "#191a1d")   # blue-gray card -> neutral
        self.assertEqual(tint("#d50a0a"), "#d50a0a")                     # brand red untouched
        self.assertEqual(tint("#1d5f90"), "#1d5f90")                     # saturated accent untouched
        self.assertEqual(tint("#8dc8fb"), "#8dc8fb")                     # light link color untouched

    def test_declarations_split_on_semicolons_outside_parens_and_quotes(self):
        parts = self.module.split_declarations('content:"a;b";background:linear-gradient(#111,#222);color:red')
        self.assertEqual(len(parts), 3)

    def test_committed_stylesheet_matches_generator_output(self):
        import os
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            generated = self.module.build()
        finally:
            os.chdir(cwd)
        committed = (ROOT / "static" / "nfl_neutral.css").read_text(encoding="utf-8")
        self.assertEqual(committed, generated,
                         "run: python scripts/build_nfl_neutral_css.py")

    def test_no_selector_is_left_unscoped(self):
        css = (ROOT / "static" / "nfl_neutral.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = re.findall(r"^([^{@/\n][^{]*)\{", css, re.M)
        for selector in rules:
            for part in selector.split(","):
                scopes = [f".{scope}" for scope in self.module.SCOPES]
                scopes += [f"body.{scope}" for scope in self.module.SCOPES]
                self.assertTrue(part.strip().startswith(tuple(scopes)), part)


if __name__ == "__main__":
    unittest.main()
