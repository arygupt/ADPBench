"""Evaluation work directories must not collide between concurrent runs.

Review item 4: directories keyed only by problem/seed/tag let parallel jobs
overwrite each other's scripts, netlists, vectors, and outputs.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from adpbench.evaluate import work_dir
from adpbench.problem import load_problem

PROBLEM_DIR = (
    Path(__file__).resolve().parent.parent
    / "problems"
    / "level1"
    / "001_dot_product"
)


class WorkDirTest(unittest.TestCase):
    def test_job_id_keeps_concurrent_work_dirs_apart(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        first = work_dir(problem, seed=1000, tag="eval", job="aaaa1111")
        second = work_dir(problem, seed=1000, tag="eval", job="bbbb2222")
        self.assertNotEqual(first, second)

    def test_seed_and_tag_still_identify_the_run(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        path = work_dir(problem, seed=1000, tag="eval", job="aaaa1111")
        self.assertIn("seed1000", path.name)
        self.assertIn("eval", path.name)


if __name__ == "__main__":
    unittest.main()
