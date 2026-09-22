import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.diagnose_kimi import diagnose


@unittest.skipUnless(shutil.which("yosys") and shutil.which("iverilog"), "requires the offline RTL toolchain")
class KimiCounterexampleTest(unittest.TestCase):
    def test_original_saved_source_omits_final_products_from_first_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = diagnose(Path(tmp) / "counterexample")
        self.assertTrue(report["compiled"])
        self.assertTrue(report["finished"])
        self.assertEqual(report["submission_sha256"], "f5eb1885b6e493590d12b0c30bd7e9cc2ef8510a7c23d999d4884849a1591726")
        self.assertEqual(report["actual"], ([48] + [64] * 15) * 2)
        self.assertEqual(report["expected_each"], 64)
        self.assertEqual(report["kind"], "rtl-diagnostic-not-benchmark-score")
        self.assertNotIn("score", report)


if __name__ == "__main__":
    unittest.main()
