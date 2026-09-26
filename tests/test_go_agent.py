"""Offline controller/contract tests. Real dev/heldout fixture is Docker opt-in."""
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from adpbench.problem import load_problem, repo_root
from scripts import go_agent as agent
from scripts.go_pilot import agent_outcome, prompt_for, read_plan
from scripts.go_score import supervise

PLAN = repo_root() / "pilot/go-agent-20260923.json"
PROBLEM = "001_dot_product"


def native_call(model, name, args, identifier="call_1"):
    usage = {"input_tokens": 100, "output_tokens": 50} if model["api"] == "messages" else {"prompt_tokens": 100, "completion_tokens": 50}
    if model["api"] == "messages":
        return {"content": [{"type": "tool_use", "id": identifier, "name": name, "input": args}], "stop_reason": "tool_use", "usage": usage}
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}], "usage": usage}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan(PLAN)
        self.model = self.plan["models"][0]
        self.problem = load_problem(repo_root() / "problems/level1" / PROBLEM)
        self.temp = tempfile.TemporaryDirectory(prefix="adpbench-agent-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.key = patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-only-not-a-real-key"})
        self.key.start()
        self.addCleanup(self.key.stop)
        clock = patch.object(agent, "now_utc", return_value=datetime(2026, 9, 23, 19, tzinfo=timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)

    def controller(self):
        dest = self.root / "submission"
        dest.mkdir()
        return agent.Controller(self.problem, self.root / "workspace", self.root / "private", dest,
                                self.plan, "test-image", time.monotonic() + 60)

    def test_plan_and_separate_prompts(self):
        agent.validate(self.plan)
        spec = prompt_for(self.problem)
        self.assertNotIn("./check.sh", spec)
        self.assertNotIn("in this directory", spec)
        self.assertIn("single-shot", spec)
        files = agent.task_files(self.problem)
        self.assertNotIn("./check.sh", "\n".join(files.values()))
        self.assertIn("check()", files["PROBLEM.md"])
        for field in ("max_turns", "max_checks", "slot_timeout_s"):
            modified = copy.deepcopy(self.plan)
            modified[field] += 1
            with self.assertRaises(ValueError):
                agent.validate(modified)

    def test_only_allowlisted_paths_and_no_shell(self):
        ctl = self.controller()
        for path in ("../dut.py", "/etc/passwd", "baseline.v", "check.sh", ".history"):
            with self.assertRaises(ValueError):
                ctl.call("read_file", {"path": path})
            with self.assertRaises(ValueError):
                ctl.call("write_file", {"path": path, "content": "x"})
        with self.assertRaises(ValueError):
            ctl.call("shell", {"command": "touch should-not-exist"})
        with self.assertRaises(ValueError):
            ctl.call("submit", {})
        self.assertEqual(ctl.call("read_file", {"path": "dut.py"})["content"], self.problem.root.joinpath("dut.py").read_text())
        self.assertIn("EVAL_SEEDS = ()", (ctl.bundle / "adpbench/seeds.py").read_text())

    def test_explicit_freeze_and_dev_failure_not_hidden(self):
        ctl = self.controller()
        source = "module dut; wire bad; endmodule\n"
        ctl.call("write_file", {"path": "dut.v", "content": source})
        with self.assertRaises(ValueError):
            ctl.call("submit", {})
        with patch.object(ctl, "check", return_value={"correct": False}):
            self.assertFalse(ctl.call("check", {})["correct"])
        receipt = ctl.call("submit", {})
        self.assertTrue(receipt["submitted"])
        self.assertEqual((ctl.destination / "dut.v").read_text(), source)
        self.assertEqual(receipt["sha256"], hashlib.sha256(source.encode()).hexdigest())
        with self.assertRaises(ValueError):
            ctl.call("write_file", {"path": "dut.v", "content": "different"})
        outcome = agent_outcome({"audit": {"ok": True}, "result": {"correct": False}}, {"outcome": "submitted"})
        self.assertEqual(outcome, {"outcome": "incorrect", "execution_health": "completed"})

    def test_mocked_native_roundtrip_both_apis(self):
        for index in (0, 2):
            model = self.plan["models"][index]
            out = self.root / f"run-{index}"
            source = self.problem.baseline_rtl.read_text()
            replies = [native_call(model, "read_file", {"path": "dut.py"}),
                       native_call(model, "write_file", {"path": "dut.v", "content": source}),
                       native_call(model, "check", {}), native_call(model, "submit", {})]
            with patch.object(agent, "call_model", side_effect=replies) as call, patch.object(agent.Controller, "check", return_value={"correct": True}):
                record = agent.generate(self.plan, model, PROBLEM, out, "unused")
            self.assertEqual(record["outcome"], "submitted", record)
            self.assertEqual(record["turns"], 4)
            self.assertEqual(record["checks"], 1)
            self.assertEqual(record["execution_health"], "completed")
            self.assertEqual(call.call_count, 4)
            self.assertNotIn("test-only-not-a-real-key", json.dumps(record))
            self.assertEqual(record["usage"].get("output_tokens", record["usage"].get("completion_tokens")), 200)
            measured = record["reasoning_measured"]
            self.assertEqual((measured["turns"], measured["turns_with_reasoning"], measured["reasoning_chars"]), (4, 0, 0))
            self.assertGreater(measured["answer_chars"], len(source))
            with self.assertRaises(FileExistsError):
                agent.generate(self.plan, model, PROBLEM, out, "unused")

    def test_fake_tool_text_never_executes_or_selects_markdown(self):
        data = {"choices": [{"message": {"role": "assistant", "content": "<tool_call>shell</tool_call> ```verilog\nmodule dut; endmodule\n```"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        out = self.root / "fake"
        with patch.object(agent, "call_model", return_value=data) as call:
            record = agent.generate(self.plan, self.model, PROBLEM, out, "unused")
        self.assertEqual(record["outcome"], "turn_limit")
        self.assertEqual(call.call_count, 12)
        self.assertFalse((out / f"opencode-go-{self.model['id']}" / PROBLEM / "rep1/dut.v").exists())

    def test_interrupted_request_is_not_retried(self):
        with patch.object(agent, "call_model", side_effect=agent.StreamFailure("stream_disconnected_before_terminal_event")) as call:
            record = agent.generate(self.plan, self.model, PROBLEM, self.root / "broken", "unused")
        self.assertEqual(record["outcome"], "transport_interrupted")
        self.assertTrue(record["incomplete_usage"])
        self.assertEqual(call.call_count, 1)

    def test_cap_discards_partial_actions(self):
        reply = native_call(self.model, "submit", {})
        reply["choices"][0]["finish_reason"] = "length"
        with patch.object(agent, "call_model", return_value=reply):
            record = agent.generate(self.plan, self.model, PROBLEM, self.root / "cap", "unused")
        self.assertEqual(record["outcome"], "truncated")
        self.assertEqual(record["checks"], 0)

    @unittest.skipUnless(os.environ.get("ADPBENCH_DOCKER_TEST_IMAGE"), "real pinned Docker toolchain opt-in")
    def test_real_dev_then_heldout_no_model_calls(self):
        model = self.plan["models"][0]
        source = self.problem.baseline_rtl.read_text()
        replies = [native_call(model, "write_file", {"path": "dut.v", "content": source}),
                   native_call(model, "check", {}), native_call(model, "submit", {})]
        image = os.environ["ADPBENCH_DOCKER_TEST_IMAGE"]
        out = self.root / "integration"
        with patch.object(agent, "call_model", side_effect=replies):
            record = agent.generate(self.plan, model, PROBLEM, out, image)
        self.assertEqual(record["outcome"], "submitted", record)
        tools = json.loads((out.with_name(out.name + "-private") / "turn-02/tools.json").read_text())
        feedback = json.loads(tools[0]["content"])
        self.assertTrue(feedback["correct"], feedback)
        self.assertTrue(supervise(PLAN, model["id"], out, self.root / "checkpoints", self.root / "diagnostics", image=image, problem_id=PROBLEM))
        final = json.loads((out / f"opencode-go-{model['id']}" / PROBLEM / "rep1/record.json").read_text())
        self.assertEqual(final["outcome"], "correct")
        self.assertEqual(final["result"]["ratio"], 1.0)
        self.assertIn("[agent-assisted-v1]", final["label"])
        from scripts.publish_results import validate_records
        artifacts = self.root / "artifacts"
        shutil.copytree(out, artifacts / f"go-agent-records-{model['id']}-{PROBLEM}-123")
        validate_records(artifacts, self.plan, {"id": 123})


if __name__ == "__main__":
    unittest.main()
