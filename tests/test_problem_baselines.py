"""Each shipped problem's baseline must pass its own reference.

The dot-product counterexamples exercise the harness; these tests exercise the
other three problems' arithmetic, output ordering, and unequal-length stream
wiring through the real pipeline.
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from adpbench.evaluate import evaluate
from adpbench.problem import load_problem

REPO = Path(__file__).resolve().parent.parent
HAS_TOOLS = all(shutil.which(tool) for tool in ("yosys", "iverilog", "vvp"))
requires_tools = unittest.skipUnless(HAS_TOOLS, "yosys and iverilog are required")


@requires_tools
class BaselineBehaviorTest(unittest.TestCase):
    def _check(self, name: str) -> None:
        root = REPO / "problems" / "level1" / name
        problem = load_problem(root)
        result = evaluate(
            problem, [problem.baseline_rtl], seed=1000, source="baseline"
        )
        self.assertTrue(result.correct, result.metadata.get("correctness"))
        self.assertGreater(result.cells, 0, name)
        self.assertGreater(result.cycles, 0, name)

    def test_gemv_baseline(self) -> None:
        self._check("002_gemv")

    def test_matmul_baseline(self) -> None:
        self._check("003_matmul")

    def test_conv1d_baseline(self) -> None:
        self._check("004_conv1d")


if __name__ == "__main__":
    unittest.main()
