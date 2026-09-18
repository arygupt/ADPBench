"""Malformed simulator output is a failed run, not a harness crash.

Review item 5: unknown bits (`xxxxxxxx`) raised an uncaught ValueError from
output parsing, losing the run record.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from adpbench import vectors
from adpbench.problem import load_problem

PROBLEM_DIR = (
    Path(__file__).resolve().parent.parent
    / "problems"
    / "level1"
    / "001_dot_product"
)


class MalformedOutputTest(unittest.TestCase):
    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".hex", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def test_unknown_bits_raise_structured_error(self) -> None:
        path = self._write("xxxxxxxx\n")
        with self.assertRaises(vectors.MalformedOutput):
            vectors.read_outputs(path, out_len=1, width=32)

    def test_short_file_raises_structured_error(self) -> None:
        path = self._write("00000001\n")
        with self.assertRaises(vectors.MalformedOutput):
            vectors.read_outputs(path, out_len=2, width=32)

    def test_missing_file_raises_structured_error(self) -> None:
        with self.assertRaises(vectors.MalformedOutput):
            vectors.read_outputs(Path("/nonexistent/out.hex"), out_len=1, width=32)

    def test_valid_words_are_read(self) -> None:
        path = self._write("// 0x00000000\nffffffff\n")
        self.assertEqual(vectors.read_outputs(path, out_len=1, width=32), [0xFFFFFFFF])


class BuildVectorsTest(unittest.TestCase):
    def test_random_case_concatenates_transactions(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        inputs, expected = vectors.build_vectors(problem, 1000)
        self.assertEqual(
            inputs[0].size,
            problem.input_lens["in_a_flat"] * problem.transactions,
        )
        self.assertEqual(expected.size, problem.out_len * problem.transactions)

    def test_directed_cases_are_available(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        self.assertIn("zeros", problem.directed_cases)
        _, expected = vectors.build_vectors(problem, "zeros")
        self.assertTrue(all(int(value) == 0 for value in expected))

    def test_transactions_receive_different_random_data(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        inputs, _ = vectors.build_vectors(problem, 1000)
        length = problem.input_lens["in_a_flat"]
        first = inputs[0][:length]
        second = inputs[0][length:]
        self.assertFalse(np.array_equal(first, second))


if __name__ == "__main__":
    unittest.main()
