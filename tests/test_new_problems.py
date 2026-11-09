"""Selection, counting, and window contracts, plus real RTL regressions."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

import numpy as np

from adpbench.evaluate import evaluate_multi
from adpbench.problem import load_problem

REPO = Path(__file__).resolve().parent.parent
NAMES = ("005_argmax", "006_topk", "007_histogram", "008_maxpool2d")
HAS_TOOLS = all(shutil.which(tool) for tool in ("yosys", "iverilog", "vvp"))


def problem(name):
    return load_problem(REPO / "problems" / "level1" / name)


class NewProblemContractTest(unittest.TestCase):
    def test_argmax_signed_values_and_first_tie_across_beats(self):
        x = np.full((8, 64), -128, dtype=np.int8)
        x[0, [7, 63]] = -1
        x[2, 63] = -1
        x[3, [7, 8]] = 127
        np.testing.assert_array_equal(
            problem("005_argmax").reference(x), [7, 0, 63, 7, 0, 0, 0, 0]
        )

    def test_topk_retains_duplicates_and_signed_order(self):
        x = np.full(64, -128, dtype=np.int8)
        x[:12] = [-5, -1, -1, 127, 127, -128, 0, 3, 3, 3, -2, -127]
        np.testing.assert_array_equal(
            problem("006_topk").reference(x), [127, 127, 3, 3, 3, 0, -1, -1]
        )
        np.testing.assert_array_equal(
            problem("006_topk").reference(np.full(64, -128, np.int8)), [-128] * 8
        )

    def test_histogram_counts_collisions_and_does_not_wrap(self):
        p = problem("007_histogram")
        x = np.zeros(256, dtype=np.int8)
        x[:48] = 15
        x[48:64] = 7
        expected = np.zeros(16, dtype=np.int64)
        expected[[0, 7, 15]] = [192, 16, 48]
        np.testing.assert_array_equal(p.reference(x), expected)
        expected[:] = 0
        expected[15] = 256
        np.testing.assert_array_equal(p.reference(np.full(256, 15, np.int8)), expected)

    def test_pooling_window_positions_signed_max_and_no_padding(self):
        x = np.full((16, 16), -128, dtype=np.int8)
        x[0, 0], x[-1, -1], x[7, 8] = 127, 126, -1
        expected = np.full((14, 14), -128, dtype=np.int64)
        expected[0, 0], expected[-1, -1] = 127, 126
        expected[5:8, 6:9] = -1
        np.testing.assert_array_equal(problem("008_maxpool2d").reference(x), expected)

    def test_pooling_ramps_preserve_row_major_order(self):
        p = problem("008_maxpool2d")
        x = (np.arange(256) - 128).reshape(16, 16).astype(np.int8)
        np.testing.assert_array_equal(p.reference(x), x[2:, 2:])
        reverse = x[::-1, ::-1]
        np.testing.assert_array_equal(p.reference(reverse), reverse[:-2, :-2])

    def test_directed_inputs_match_domains_and_exercise_state_changes(self):
        for name in NAMES:
            p = problem(name)
            changes_output = False
            for case, generate in p.directed.items():
                outputs = []
                for txn in range(p.transactions):
                    with self.subTest(problem=name, case=case, txn=txn):
                        inputs = generate(txn)
                        self.assertEqual(len(inputs), 1)
                        x = np.asarray(inputs[0])
                        self.assertEqual(x.size, p.input_lens["in_x_flat"])
                        self.assertTrue(np.issubdtype(x.dtype, np.integer))
                        lo, hi = (0, 15) if name == "007_histogram" else (-128, 127)
                        self.assertTrue(np.all((x >= lo) & (x <= hi)))
                        output = p.reference(*inputs)
                        self.assertEqual(output.size, p.out_len)
                        outputs.append(output)
                changes_output |= not np.array_equal(outputs[0], outputs[1])
            self.assertTrue(changes_output, name)

    def test_site_catalog_matches_new_specs_and_measured_baselines(self):
        from adpbench.site import _problem_entry

        catalog = json.loads((REPO / "site/data/problems.json").read_text())
        entries = {entry["name"]: entry for entry in catalog["problems"]}
        for name in NAMES:
            expected = _problem_entry(problem(name))
            self.assertEqual({key: entries[name][key] for key in expected}, expected)
            self.assertTrue(entries[name]["sanity"]["correct"])
            self.assertGreater(entries[name]["sanity"]["ratio"], 1)


@unittest.skipUnless(HAS_TOOLS, "yosys, iverilog, and vvp are required")
class NewProblemRTLTest(unittest.TestCase):
    def test_baselines_and_optimized_references(self):
        # Extra seeds complement the standard seeds used to record baselines.
        # Every case includes two transactions, input gaps, and output stalls.
        for name in NAMES:
            p = problem(name)
            for rtl in (p.baseline_rtl, p.root / "solutions/parallel.v"):
                with self.subTest(problem=name, rtl=str(rtl.relative_to(p.root))):
                    result = evaluate_multi(p, [rtl], seeds=(0, 1, 17, 42), tag="regression")
                    self.assertTrue(result.correct, result.metadata)
                    self.assertGreater(result.cells, 0)
                    self.assertGreater(result.cycles, 0)
                    if rtl == p.baseline_rtl:
                        baseline = json.loads(p.baseline_metrics.read_text())
                        self.assertEqual(result.cells, baseline["cells"])
                        self.assertEqual(result.cycles, baseline["cycles"])
                    else:
                        self.assertGreater(result.ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
