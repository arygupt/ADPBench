"""End-to-end regression tests for the scorer soundness fixes.

Each counterexample from the review is scored through the real pipeline
(yosys -> netlist -> iverilog), so the tests fail if an exploit comes back.
They need `yosys`, `iverilog`, and `vvp` on PATH.
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from adpbench.evaluate import evaluate_multi
from adpbench.problem import load_problem

REPO = Path(__file__).resolve().parent.parent
PROBLEM_DIR = REPO / "problems" / "level1" / "001_dot_product"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HAS_TOOLS = all(shutil.which(tool) for tool in ("yosys", "iverilog", "vvp"))
requires_tools = unittest.skipUnless(HAS_TOOLS, "yosys and iverilog are required")


def _score(fixture: str):
    problem = load_problem(PROBLEM_DIR)
    return evaluate_multi(problem, [FIXTURES / fixture], source=fixture, tag="test")


@requires_tools
class ParameterMismatchTest(unittest.TestCase):
    """Review item 1: defaults must not shrink the counted datapath."""

    def test_problem_parameters_are_pinned_at_synthesis(self) -> None:
        result = _score("parameter_mismatch.v")
        self.assertTrue(result.correct, result.metadata.get("correctness"))
        # The fixture declares LANES=1 but simulates at LANES=32. Before the
        # fix, synthesis counted the LANES=1 design: 785 cells and a 79x score.
        self.assertGreater(result.cells, 10_000)
        self.assertLess(result.ratio, 5.0)


@requires_tools
class SynthesisSwitchTest(unittest.TestCase):
    """Review item 1: simulation must run the synthesized branch."""

    def test_synthesis_only_junk_cannot_pass(self) -> None:
        result = _score("synthesis_switch.v")
        self.assertFalse(result.correct)
        self.assertTrue(result.synthesizable)


@requires_tools
class BackpressureTest(unittest.TestCase):
    """Review item 3: an output that ignores out_ready must be rejected."""

    def test_output_must_hold_while_stalled(self) -> None:
        result = _score("ignores_out_ready.v")
        self.assertFalse(result.correct)
        self.assertIn("valid/ready", result.metadata.get("correctness", ""))


@requires_tools
class UnknownOutputTest(unittest.TestCase):
    """Review item 5: malformed simulation output must not crash the scorer."""

    def test_unknown_bits_are_a_structured_failure(self) -> None:
        result = _score("unknown_output.v")
        self.assertFalse(result.correct)
        self.assertIn("malformed", result.metadata.get("correctness", ""))


@requires_tools
class SanityTest(unittest.TestCase):
    """The harness still rewards a genuinely better design."""

    def test_parallel_solution_beats_baseline(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        result = evaluate_multi(
            problem,
            [PROBLEM_DIR / "solutions" / "parallel.v"],
            source="parallel",
            tag="test",
        )
        self.assertTrue(result.correct, result.metadata.get("correctness"))
        self.assertGreater(result.cells, 10_000)
        self.assertLessEqual(result.cycles, 12)
        self.assertGreater(result.ratio, 3.0)


@unittest.skip("repeated-transaction contract is not defined yet (review item 3)")
class TwoTransactionTest(unittest.TestCase):
    def test_parallel_handles_second_transaction(self) -> None:
        self.fail("expected to fail until the multi-transaction contract lands")


if __name__ == "__main__":
    unittest.main()
