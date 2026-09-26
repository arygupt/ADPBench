"""Offline interruption/recovery and scorer-supervisor fault injection."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adpbench.durable import atomic_json
from adpbench.evaluate import evaluate_multi
from adpbench.process import run_logged
from adpbench.problem import load_problem, repo_root
from adpbench.result import EvalResult
from adpbench import sim
from scripts.go_pilot import generate, read_plan, score, save_scoring_failure
from scripts.go_score import failure_reason, supervise
from scripts.go_stream import StreamFailure


class ProcessTest(unittest.TestCase):
    def test_atomic_replace_failure_keeps_previous_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            atomic_json(path, {"state":"before"})
            with patch("adpbench.durable.os.replace", side_effect=OSError("injected")), self.assertRaises(OSError):
                atomic_json(path, {"state":"after"})
            self.assertEqual(json.loads(path.read_text()), {"state":"before"})
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_timeout_keeps_logs_and_kills_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "child-survived"
            child = f"import time; from pathlib import Path; time.sleep(0.8); Path({str(marker)!r}).touch()"
            parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('started',flush=True); time.sleep(10)"
            result = run_logged([sys.executable,"-c",parent], root, "fault", timeout=0.25)
            self.assertTrue(result["timed_out"])
            self.assertIn("started", (root / "fault.log").read_text())
            self.assertEqual(json.loads((root / "fault.status.json").read_text())["reason"], "wall_timeout")
            time.sleep(0.9)
            self.assertFalse(marker.exists())

    def test_nonzero_exit_and_log_limit_are_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_logged([sys.executable,"-c","print('RESULT OK CYCLES 10'); raise SystemExit(2)"],root,"exit")
            self.assertEqual(result["reason"], "tool_error")
            result = run_logged([sys.executable,"-c","print('x'*10000)"],root,"size",log_limit=128)
            self.assertEqual(result["reason"], "log_limit")
            self.assertEqual((root / "size.log").stat().st_size, 128)

    def test_parent_deadline_prevents_tool_launch(self):
        with tempfile.TemporaryDirectory() as tmp, patch("adpbench.process.subprocess.Popen") as popen:
            result = run_logged(["never-execute"],Path(tmp),"deadline",deadline_at=time.monotonic() - 1)
            self.assertTrue(result["timed_out"])
            popen.assert_not_called()

    def test_simulator_ok_line_with_nonzero_exit_does_not_pass(self):
        problem = load_problem(repo_root() / "problems/level1/001_dot_product")
        success = {"returncode":0,"reason":"","timed_out":False,"log":""}
        crashed = {"returncode":2,"reason":"tool_error","timed_out":False,"log":"RESULT OK CYCLES 10"}
        with tempfile.TemporaryDirectory() as tmp, patch("adpbench.sim.simlib_path", return_value=Path("fake.v")), patch("adpbench.sim.run_logged", side_effect=[success,crashed]):
            result = sim.simulate(problem,Path("netlist.v"),Path(tmp))
            self.assertFalse(result["finished"])
            self.assertEqual(result["status"], "TOOL_ERROR")


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.problem = load_problem(repo_root() / "problems/level1/001_dot_product")
        self.source = self.root / "dut.v"
        self.source.write_text("module dut; endmodule")
        self.tools = patch("adpbench.evaluate._current_tools", return_value={})
        self.tools.start()
        self.addCleanup(self.tools.stop)

    def synth(self, problem, paths, workdir, deadline_at=None):
        workdir.mkdir(parents=True,exist_ok=True)
        netlist = workdir / "netlist.v"
        netlist.write_text("trusted netlist fixture")
        return {"ok":True, "cells":10, "netlist":netlist, "log":"fixture synthesis"}

    def evaluate(self, problem, paths, **kwargs):
        return EvalResult(problem=problem.name,synthesizable=True,compiled=True,correct=True,cells=10,cycles=20,
                          metadata={"stage":"ok", "case":kwargs["seed"], "netlist_sha256":"test"})

    def run_score(self, **kwargs):
        return evaluate_multi(self.problem,[self.source],seeds=(1,2),include_directed=False,
                              checkpoint_dir=self.root / "checkpoint",**kwargs)

    def test_synthesize_once_and_resume_only_unfinished_cases(self):
        with patch("adpbench.evaluate.synth.synthesize",side_effect=self.synth) as syn:
            with patch("adpbench.evaluate.evaluate",side_effect=[self.evaluate(self.problem,[],seed=1),KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt):
                    self.run_score()
            with patch("adpbench.evaluate.evaluate",side_effect=self.evaluate) as evaluate:
                result = self.run_score(resume=True)
                self.assertTrue(result.correct)
                self.assertEqual(evaluate.call_count,1)
                self.assertEqual(evaluate.call_args.kwargs["seed"],2)
            self.assertEqual(syn.call_count,1)
        with patch("adpbench.evaluate.synth.synthesize") as syn, patch("adpbench.evaluate.evaluate") as evaluate:
            self.assertTrue(self.run_score(resume=True).correct)
            syn.assert_not_called()
            evaluate.assert_not_called()

    def test_changed_rtl_and_tampered_cache_refuse_reuse(self):
        with patch("adpbench.evaluate.synth.synthesize",side_effect=self.synth), patch("adpbench.evaluate.evaluate",side_effect=self.evaluate):
            self.run_score()
        self.source.write_text("changed source")
        with self.assertRaisesRegex(ValueError,"identity changed"):
            self.run_score(resume=True)
        self.source.write_text("module dut; endmodule")
        netlist = self.root / "checkpoint/synthesis/netlist.v"
        netlist.write_text("tampered")
        with self.assertRaisesRegex(ValueError,"netlist hash"):
            self.run_score(resume=True)
        netlist.write_text("trusted netlist fixture")
        (self.root / "checkpoint/case-0/result.json").write_text('{}')
        with self.assertRaisesRegex(ValueError,"case checkpoint hash"):
            self.run_score(resume=True)

    def test_completed_wrong_rtl_is_not_retried_or_made_green(self):
        wrong = self.evaluate(self.problem,[],seed=1)
        wrong.correct = False
        wrong.metadata.update(stage="incorrect",correctness="exact mismatch")
        with patch("adpbench.evaluate.synth.synthesize",side_effect=self.synth), patch("adpbench.evaluate.evaluate",return_value=wrong):
            self.assertFalse(self.run_score().correct)
        with patch("adpbench.evaluate.evaluate") as evaluate:
            self.assertFalse(self.run_score(resume=True).correct)
            evaluate.assert_not_called()

    def test_empty_case_set_cannot_vacuously_pass(self):
        with self.assertRaises(ValueError):
            evaluate_multi(self.problem,[self.source],seeds=(),include_directed=False)


class ScoringSupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "model"
        self.plan_path = repo_root() / "pilot/go-core-provider-max-20260922.json"
        self.plan = read_plan(self.plan_path)
        self.model = self.plan["models"][0]
        atomic_json(self.out / "plan.json",self.plan)
        for problem in self.plan["problems"]:
            dest = self.out / f"opencode-go-{self.model['id']}" / problem / "rep1"
            atomic_json(dest / "generation.json",{"started":"2026-09-22T00:00:00Z","error":""})
            (dest / "dut.v").write_text("module dut; endmodule")

    def test_oom_is_not_inferred_from_sigkill_alone(self):
        self.assertEqual(failure_reason({"reason":"signal"},{"OOMKilled":True}),"out_of_memory")
        self.assertEqual(failure_reason({"reason":"signal"},{}),"signal")
        self.assertEqual(failure_reason({"timed_out":True},{}),"wall_timeout")

    def test_first_container_crash_keeps_evidence_and_runs_second_problem(self):
        calls = []
        def command(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,json.dumps({"OOMKilled":True,"ExitCode":137,"Running":False}),"")
        with patch("scripts.go_score.docker",side_effect=command), patch("scripts.go_score.run_logged",return_value={"reason":"tool_error","returncode":137}) as run:
            healthy = supervise(self.plan_path,self.model["id"],self.out,self.root / "checkpoints",self.root / "diagnostics")
        self.assertFalse(healthy)
        self.assertEqual(run.call_count,2)
        for call in run.call_args_list:
            argv = call.args[0]
            self.assertNotIn("OPENCODE_GO_API_KEY", " ".join(argv))
            self.assertIn("none",argv)
            self.assertIn("/checkpoints",argv)
        self.assertEqual(sum(c[0] == "rm" for c in calls),2)
        for record in self.out.glob("**/record.json"):
            data = json.loads(record.read_text())
            self.assertIsNone(data["result"])
            self.assertIn("out_of_memory",data["error"])
            self.assertTrue(data["manifest"]["submission_sha256"])
        self.assertTrue((self.out / "REPORT.md").exists())

    def test_scorer_resume_does_not_repeat_completed_failed_evaluation(self):
        problem = self.plan["problems"][0]
        result = EvalResult(problem=problem,metadata={"stage":"incorrect"})
        with patch("scripts.go_pilot.evaluate_multi",return_value=result) as evaluate:
            score(self.plan,self.model,self.out,problem_id=problem,checkpoint_root=self.root / "checks")
            record = next(self.out.glob("**/record.json"))
            original = record.read_bytes()
            score(self.plan,self.model,self.out,problem_id=problem,checkpoint_root=self.root / "checks",resume=True)
            self.assertEqual(evaluate.call_count,1)
            self.assertEqual(record.read_bytes(),original)
            save_scoring_failure(self.plan,self.model,self.out,problem,"cleanup_error")
            self.assertEqual(record.read_bytes(),original)


class GenerationInterruptionTest(unittest.TestCase):
    def test_disconnect_never_retries_and_records_every_scheduled_slot(self):
        from datetime import datetime
        plan = read_plan(repo_root() / "pilot/go-core-provider-max-20260922.json")
        plan["generation_enabled"] = True
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{"OPENCODE_GO_API_KEY":"offline-fixture"}), patch("scripts.go_pilot.now_utc",return_value=datetime.fromisoformat("2026-09-22T01:00:00+00:00")), patch("scripts.go_pilot.call_model",side_effect=StreamFailure("stream_idle_timeout")) as call:
            with self.assertRaisesRegex(RuntimeError,"stream_idle_timeout"):
                generate(plan,plan["models"][0],Path(tmp))
            self.assertEqual(call.call_count,1)
            generations = sorted(Path(tmp).glob("**/generation.json"))
            self.assertEqual(len(generations),2)
            first,second = [json.loads(p.read_text()) for p in generations]
            self.assertTrue(first["incomplete_usage"])
            self.assertIn("not requested",second["error"])
            self.assertFalse(list(Path(tmp).glob("**/dut.v")))


if __name__ == "__main__":
    unittest.main()
