"""Catalog-level checks: every shipped problem is complete and consistent."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from adpbench.problem import load_problem

REPO = Path(__file__).resolve().parent.parent


class ProblemCatalogTest(unittest.TestCase):
    def test_catalog_has_four_problems_with_baselines(self) -> None:
        roots = sorted((REPO / "problems").glob("*/*"))
        self.assertGreaterEqual(len(roots), 4)
        for root in roots:
            problem = load_problem(root)
            self.assertTrue(problem.baseline_rtl.is_file(), problem.name)
            self.assertTrue(problem.baseline_metrics.is_file(), problem.name)
            baseline = json.loads(problem.baseline_metrics.read_text())
            self.assertEqual(baseline["transactions"], problem.transactions)
            self.assertGreater(baseline["adp"], 0, problem.name)

    def test_input_lengths_are_beat_aligned(self) -> None:
        for root in sorted((REPO / "problems").glob("*/*")):
            problem = load_problem(root)
            lanes = int(problem.params["LANES"])
            for port, length in problem.input_lens.items():
                self.assertEqual(length % lanes, 0, f"{problem.name}:{port}")

    def test_every_problem_has_directed_cases(self) -> None:
        for root in sorted((REPO / "problems").glob("*/*")):
            problem = load_problem(root)
            self.assertTrue(problem.directed_cases, problem.name)

    def test_default_transactions_are_at_least_two(self) -> None:
        for root in sorted((REPO / "problems").glob("*/*")):
            problem = load_problem(root)
            self.assertGreaterEqual(problem.transactions, 2, problem.name)


if __name__ == "__main__":
    unittest.main()
