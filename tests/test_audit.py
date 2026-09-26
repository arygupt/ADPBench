"""The static audit is a lexical check for executable constructs.

Review item 5: words inside comments and strings are not code, and matching
them rejected valid submissions.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from adpbench.audit import audit_submission

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class AuditCommentTest(unittest.TestCase):
    def test_trigger_words_inside_comments_are_allowed(self) -> None:
        result = audit_submission((FIXTURES / "comment_words.v").read_text())
        self.assertTrue(result["ok"], result["violations"])

    def test_block_comment_is_masked(self) -> None:
        text = "module dut;\n/* initial $display #1 force tb. */\nendmodule\n"
        self.assertTrue(audit_submission(text)["ok"])

    def test_real_initial_block_is_rejected(self) -> None:
        text = "module dut;\n  initial begin\n  end\nendmodule\n"
        self.assertFalse(audit_submission(text)["ok"])

    def test_real_delay_control_is_rejected(self) -> None:
        text = "module dut;\n  wire x;\n  assign #5 x = 1'b0;\nendmodule\n"
        self.assertFalse(audit_submission(text)["ok"])

    def test_real_display_is_rejected(self) -> None:
        text = 'module dut;\n  initial $display("hi");\nendmodule\n'
        self.assertFalse(audit_submission(text)["ok"])

    def test_missing_dut_module_is_rejected(self) -> None:
        self.assertFalse(audit_submission("module other;\nendmodule\n")["ok"])

    def test_reported_lines_survive_masking(self) -> None:
        text = "module dut;\n// comment\n  initial begin\n  end\nendmodule\n"
        result = audit_submission(text)
        self.assertFalse(result["ok"])
        self.assertEqual(result["violations"][0]["line"], 3)


if __name__ == "__main__":
    unittest.main()
