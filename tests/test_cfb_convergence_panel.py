import tempfile
import unittest
from pathlib import Path

from sports_aggregator.cfb import convergence_panel as panel

HEADER = ("scope,season,policy,source_games,keep_n,fade_n,pass_n,acted_n,n,wins,hit_rate,hit_rate_ci95,"
          "mean_aligned_residual,mean_aligned_residual_bootstrap_ci95,median_aligned_residual,"
          "worst_aligned_miss,best_aligned_result\n")
ROWS = (
    "pooled,,baseline_keep_all_full_convergence,155,155,0,0,155,155,82,0.529,0.4507|0.606,3.9,1|6,3,-31,47\n"
    "pooled,,pass_spread_14_plus,155,121,0,34,121,121,69,0.5702,0.4812|0.655,4.8,2|8,3,-31,47\n"
    "year,2021,baseline_keep_all_full_convergence,36,36,0,0,36,36,16,0.4444,0.2954|0.6042,2,-2|7,0,-30,35\n"
    "year,2022,baseline_keep_all_full_convergence,25,25,0,0,25,25,20,0.8,0.6|0.9,2,-2|7,0,-30,35\n"
)


class ConvergencePanelTests(unittest.TestCase):
    def _build(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary.csv"
            path.write_text(text, encoding="utf-8")
            return panel.build(path)

    def test_an_interval_that_includes_fifty_is_not_called_proof(self):
        result = self._build(HEADER + ROWS)
        self.assertEqual(result["summary"]["record"], "82-73")
        self.assertEqual(result["summary"]["verdict"], "Not distinguishable from 50%")
        reads = {row["label"]: row["verdict"] for row in result["seasons_table"].rows}
        self.assertEqual(reads["2021"], "Not distinguishable from 50%")
        self.assertEqual(reads["2022"], "Interval clears 50%")

    def test_policy_table_leads_with_the_frozen_rule(self):
        result = self._build(HEADER + ROWS)
        labels = [row["label"] for row in result["policies_table"].rows]
        self.assertEqual(labels[0], panel.POLICY_LABELS[panel.BASELINE])
        self.assertIn("Pass when the spread is 14+", labels)

    def test_missing_study_degrades_to_unavailable(self):
        self.assertEqual(panel.build(Path("does-not-exist.csv")), {"available": False})


if __name__ == "__main__":
    unittest.main()
