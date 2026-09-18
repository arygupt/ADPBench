"""Pilot config parsing and one end-to-end pilot with a scripted agent."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adpbench.pilot import load_pilot, run_pilot, slug

REPO = Path(__file__).resolve().parent.parent
PROBLEM_DIR = REPO / "problems" / "level1" / "001_dot_product"
HAS_TOOLS = all(shutil.which(tool) for tool in ("yosys", "iverilog", "vvp"))
requires_tools = unittest.skipUnless(HAS_TOOLS, "yosys and iverilog are required")


class SlugTest(unittest.TestCase):
    def test_labels_become_directory_names(self) -> None:
        self.assertEqual(slug("opencode/mimo-v2.5-free"), "opencode-mimo-v2.5-free")
        self.assertEqual(slug("  "), "run")


class LoadPilotTest(unittest.TestCase):
    def test_config_parses_agents_and_budget(self) -> None:
        config = {
            "name": "pilot-001",
            "problems": ["001_dot_product"],
            "repetitions": 1,
            "sandbox": {"mode": "none"},
            "agents": [{"label": "m", "cmd": "true", "timeout_s": 60}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps(config))
            parsed = load_pilot(path)
        self.assertEqual(parsed.planned_runs, 1)
        self.assertEqual(parsed.agents[0].timeout_s, 60)
        self.assertEqual(parsed.problems, ["001_dot_product"])


@requires_tools
class PilotIntegrationTest(unittest.TestCase):
    def test_one_scripted_run_produces_a_reportable_score(self) -> None:
        config = {
            "name": "scripted",
            "problems": [str(PROBLEM_DIR)],
            "repetitions": 1,
            "sandbox": {"mode": "none"},
            "agents": [
                {
                    "label": "scripted/parallel",
                    "cmd": f"cp '{PROBLEM_DIR / 'solutions' / 'parallel.v'}' dut.v",
                    "timeout_s": 300,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps(config))
            pilot_dir = run_pilot(
                load_pilot(path), runs_root=Path(tmp) / "runs", echo=lambda *_: None
            )
            report = json.loads((pilot_dir / "report.json").read_text())
            plan = json.loads((pilot_dir / "plan.json").read_text())

        self.assertEqual(plan["planned_runs"], 1)
        bucket = report["labels"]["scripted/parallel"]
        self.assertEqual(bucket["attempts"], 1)
        self.assertEqual(bucket["correctness_rate"], 1.0)
        self.assertEqual(bucket["beat_baseline_rate"], 1.0)
        self.assertGreater(bucket["geomean_ratio_successful"], 2.0)


if __name__ == "__main__":
    unittest.main()
