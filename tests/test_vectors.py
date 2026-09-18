"""Malformed simulator output is a failed run, not a harness crash.

Review item 5: unknown bits (`xxxxxxxx`) raised an uncaught ValueError from
output parsing, losing the run record.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adpbench import vectors


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


if __name__ == "__main__":
    unittest.main()
