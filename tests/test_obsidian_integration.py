"""Run the vault integration contract tests when Node is available."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ObsidianIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for Obsidian JavaScript tests")
    def test_engine_import_contract(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "--test", "integrations/obsidian/tests/engine.test.cjs",
             "integrations/obsidian/tests/nfl.test.cjs"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
