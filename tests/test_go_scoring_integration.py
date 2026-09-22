"""Opt-in real Docker scoring/recovery smoke test, using baseline RTL only."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from adpbench.durable import atomic_json
from adpbench.problem import repo_root
from scripts.go_pilot import read_plan
from scripts.go_score import supervise


@unittest.skipUnless(os.environ.get("ADPBENCH_DOCKER_TEST_IMAGE"), "set ADPBENCH_DOCKER_TEST_IMAGE to a pinned local toolchain")
class ScoringIntegrationTest(unittest.TestCase):
    def test_baseline_scores_and_completed_resume_are_unchanged(self):
        root = repo_root()
        plan_path = root / "pilot/go-core-provider-max-20260922.json"
        plan = read_plan(plan_path)
        model = plan["models"][0]["id"]
        # Temporary owned fixture only; no generation/API path is called.
        with tempfile.TemporaryDirectory(prefix="adpbench-scoring-test-") as temp:
            work = Path(temp)
            out = work / "model"
            atomic_json(out / "plan.json",plan)
            for problem in plan["problems"]:
                dest = out / f"opencode-go-{model}" / problem / "rep1"
                atomic_json(dest / "generation.json",{"started":"2026-09-22T00:00:00Z","protocol":"fixture","error":""})
                (dest / "dut.v").write_bytes((root / "problems/level1" / problem / "baseline.v").read_bytes())
            args = (plan_path,model,out,work / "checkpoints",work / "diagnostics")
            image = os.environ["ADPBENCH_DOCKER_TEST_IMAGE"]
            self.assertTrue(supervise(*args,image=image))
            snapshots = {}
            for problem in plan["problems"]:
                path = out / f"opencode-go-{model}" / problem / "rep1/record.json"
                snapshots[path] = path.read_bytes()
                result = json.loads(snapshots[path])["result"]
                expected = json.loads((root / "problems/level1" / problem / "baseline.json").read_text())
                self.assertTrue(result["correct"],result)
                for key in ("cells","cycles","adp"):
                    self.assertEqual(result[key],expected[key],f"{problem}/{key}")
                self.assertEqual(result["ratio"],1.0)
                state = json.loads((work / "checkpoints" / model / problem / "checkpoint.json").read_text())
                self.assertEqual(state["state"],"completed")
                self.assertEqual(len(state["completed"]),len(state["identity"]["cases"]))
            self.assertTrue(supervise(*args,image=image,resume=True))
            self.assertEqual({p:p.read_bytes() for p in snapshots},snapshots)


if __name__ == "__main__":
    unittest.main()
