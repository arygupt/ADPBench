"""Site data export: the published leaderboard is a copy of frozen records."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adpbench.site import export_site

REPO = Path(__file__).resolve().parent.parent


class ExportSiteTest(unittest.TestCase):
    def test_export_reflects_frozen_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pilot = root / "pilot_test"
            label = "opencode-test-model"
            (pilot / label / "001_dot_product" / "rep1").mkdir(parents=True)
            (pilot / label / "002_gemv" / "rep1").mkdir(parents=True)
            (pilot / "plan.json").write_text(
                json.dumps(
                    {
                        "name": "test",
                        "problems": ["001_dot_product", "002_gemv"],
                        "agents": [{"label": label, "cmd": "true", "timeout_s": 60}],
                        "repetitions": 1,
                        "sandbox": {"mode": "none"},
                    }
                )
            )
            (pilot / label / "001_dot_product" / "rep1" / "record.json").write_text(
                json.dumps(
                    {
                        "problem": "001_dot_product",
                        "label": label,
                        "attempt": 1,
                        "timed_out": False,
                        "result": {
                            "correct": True,
                            "ratio": 3.39,
                            "cells": 19193,
                            "cycles": 18,
                            "metadata": {"stage": "ok"},
                        },
                        "manifest": {
                            "tools": {"yosys": "y", "iverilog": "i"},
                            "submission_sha256": "a" * 64,
                            "netlist_sha256": "b" * 64,
                        },
                    }
                )
            )
            (pilot / label / "002_gemv" / "rep1" / "record.json").write_text(
                json.dumps(
                    {
                        "problem": "002_gemv",
                        "label": label,
                        "attempt": 1,
                        "timed_out": True,
                        "result": {
                            "correct": False,
                            "ratio": -1.0,
                            "metadata": {"stage": "incorrect"},
                        },
                    }
                )
            )
            out = export_site(pilot, root / "data")

            leaderboard = json.loads((out / "leaderboard.json").read_text())
            self.assertEqual(leaderboard["meta"]["pilot"], "test")
            self.assertEqual(len(leaderboard["models"]), 1)
            model = leaderboard["models"][0]
            self.assertEqual(model["label"], label)
            self.assertEqual(model["correct"], 1)
            self.assertEqual(model["attempts"], 2)
            runs = {run["problem"]: run for run in model["runs"]}
            self.assertEqual(runs["001_dot_product"]["ratio"], 3.39)
            self.assertEqual(runs["002_gemv"]["stage"], "incorrect")
            self.assertEqual(len(leaderboard["problems"]), 4)
            self.assertIn("title", leaderboard["problems"][0])

            problems = json.loads((out / "problems.json").read_text())
            self.assertEqual(len(problems["problems"]), 4)


if __name__ == "__main__":
    unittest.main()
