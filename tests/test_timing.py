"""Timing-aware ADP: cell pricing, log parsing, and a real synthesis run."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adpbench import timing
from adpbench.problem import load_problem

REPO = Path(__file__).resolve().parent.parent
HAS_YOSYS = shutil.which("yosys") is not None

LOG = """
9. Printing statistics.

=== dut ===

       11 ports
       10 cells
        4   $_AND_
        2   $_DFFE_PP_
        3   $_XOR_
        1   $_SDFF_PP0_

10. Executing LTP pass (find longest path).

Longest topological path in dut (length=7):
    0: \\a [0]
"""


class CellAreaTest(unittest.TestCase):
    def test_gates_use_nangate45_x1_areas(self):
        self.assertEqual(timing.cell_area("$_NOT_"), 0.532)
        self.assertEqual(timing.cell_area("$_XOR_"), 1.596)
        self.assertEqual(timing.cell_area("$_MUX_"), 1.862)

    def test_flops_add_enable_and_reset_logic(self):
        dff, dffr, and2, mux2 = 4.522, 5.320, 1.064, 1.862
        self.assertEqual(timing.cell_area("$_DFF_P_"), dff)
        self.assertEqual(timing.cell_area("$_DFF_PN0_"), dffr)
        self.assertEqual(timing.cell_area("$_DFFE_PP_"), dff + mux2)
        self.assertEqual(timing.cell_area("$_DFFE_PN1P_"), dffr + mux2)
        self.assertEqual(timing.cell_area("$_SDFF_PP0_"), dff + and2)
        self.assertEqual(timing.cell_area("$_SDFFCE_PN0P_"), dff + and2 + mux2)

    def test_unpriced_cells_are_rejected_not_guessed(self):
        self.assertIsNone(timing.cell_area("$_DLATCH_P_"))
        self.assertIsNone(timing.cell_area("$_DFFSR_PNN_"))
        with self.assertRaisesRegex(ValueError, r"\$mul"):
            timing.area({"$_AND_": 2, "$mul": 1})


class ScoreTest(unittest.TestCase):
    def test_parse_reads_histogram_and_depth(self):
        measured = timing.parse(LOG)
        self.assertEqual(measured["cells"], {"$_AND_": 4, "$_DFFE_PP_": 2, "$_XOR_": 3, "$_SDFF_PP0_": 1})
        self.assertEqual(measured["depth"], 7)

    def test_timed_adp_multiplies_area_cycles_and_period(self):
        self.assertEqual(timing.timed_adp(10.0, 4, 7), 10.0 * 4 * (7 + timing.REGISTER_LEVELS))
        self.assertEqual(timing.timed_adp(10.0, 0, 7), -1.0)


@unittest.skipUnless(HAS_YOSYS, "yosys not installed")
class MeasureTest(unittest.TestCase):
    def test_measures_the_scored_netlist(self):
        problem = load_problem(REPO / "problems/level1/001_dot_product")
        baseline = json.loads(problem.baseline_metrics.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            measured = timing.measure(problem, [problem.baseline_rtl], Path(tmp))
        self.assertTrue(measured["ok"], measured.get("error"))
        self.assertEqual(sum(measured["cells"].values()), baseline["cells"])
        self.assertGreater(measured["depth"], 0)
        self.assertAlmostEqual(measured["area"], timing.area(measured["cells"]))


if __name__ == "__main__":
    unittest.main()
