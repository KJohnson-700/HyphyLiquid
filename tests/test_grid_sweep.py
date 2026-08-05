"""Tests for event range grid parameter sweep helpers."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_grid_sweep import _passes_watch, _top_win_share  # noqa: E402


class TestGridSweep(unittest.TestCase):
    def test_top_win_share_flags_outlier_concentration(self):
        self.assertAlmostEqual(_top_win_share([1.0, 1.0, -0.5]), 0.5)
        self.assertEqual(_top_win_share([-1.0, 0.0]), 0.0)

    def test_watch_pass_requires_n_pf_median_and_outlier_control(self):
        row = {
            "n": 20,
            "profit_factor": 1.8,
            "median_net_return_pct": 0.01,
            "top_win_share": 0.25,
        }
        self.assertTrue(_passes_watch(row, 20))

        too_small = dict(row, n=19)
        self.assertFalse(_passes_watch(too_small, 20))

        negative_median = dict(row, median_net_return_pct=-0.01)
        self.assertFalse(_passes_watch(negative_median, 20))

        concentrated = dict(row, top_win_share=0.40)
        self.assertFalse(_passes_watch(concentrated, 20))

        weak_pf = dict(row, profit_factor=1.1)
        self.assertFalse(_passes_watch(weak_pf, 20))


if __name__ == "__main__":
    unittest.main()
