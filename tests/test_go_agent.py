"""Offline controller/contract tests. Real dev/heldout fixture is Docker opt-in."""
import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
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
    usage = {"input_tokens": 100, "output_tokens": 50} if model["api"] != "chat/completions" else {"prompt_tokens": 100, "completion_tokens": 50}
    if model["api"] == "responses":
        return {"output": [{"type": "reasoning", "id": "rs_" + identifier, "summary": [], "encrypted_content": "opaque"},
                           {"type": "function_call", "id": "fc_" + identifier, "call_id": identifier, "name": name,
                            "arguments": json.dumps(args), "status": "completed"}],
                "stop_reason": "tool_calls", "usage": usage}
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


V2_PLAN = repo_root() / "pilot/go-agent-v2-r1.json"
CANARY = repo_root() / "pilot/go-canary-v2.json"
REAL_CHECK = agent.Controller.check  # AgentV2Tests mocks it; the Docker test needs the real one


def http_error(code):
    return urllib.error.HTTPError("https://opencode.ai/zen/go/v1/responses", code, "rejected", {}, io.BytesIO(b"{}"))


class AgentV2Tests(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan(V2_PLAN)
        self.plan["generation_enabled"] = True
        self.problem = load_problem(repo_root() / "problems/level1" / PROBLEM)
        self.temp = tempfile.TemporaryDirectory(prefix="adpbench-agent-v2-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        for patcher in (patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-only-not-a-real-key"}),
                        patch.object(agent, "now_utc", return_value=datetime(2026, 9, 27, 12, tzinfo=timezone.utc)),
                        patch.object(agent.time, "sleep"),
                        patch.object(agent.Controller, "check", return_value={"correct": True})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def model(self, api):
        return next(model for model in self.plan["models"] if model["api"] == api)

    def replies(self, model):
        source = self.problem.baseline_rtl.read_text()
        return [native_call(model, "write_file", {"path": "dut.v", "content": source}, "call_1"),
                native_call(model, "check", {}, "call_2"), native_call(model, "submit", {}, "call_3")]

    def run_slot(self, model, replies, plan=None, name="run", image="unused"):
        with patch.object(agent, "call_model", side_effect=replies) as call:
            record = agent.generate(plan or self.plan, model, PROBLEM, self.root / name, image)
        return record, call

    def test_versioned_limits(self):
        agent.validate(self.plan)
        self.assertEqual((self.plan["max_turns"], self.plan["max_checks"], self.plan["transport_retries"]), (20, 3, 2))
        for field, value in (("max_turns", 12), ("max_checks", 5), ("transport_retries", 0)):
            changed = {**self.plan, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                agent.validate(changed)
        without_retries = {key: value for key, value in self.plan.items() if key != "transport_retries"}
        with self.assertRaises(ValueError):
            agent.validate(without_retries)
        v1 = read_plan(PLAN)
        agent.validate(v1)
        with self.assertRaises(ValueError):
            agent.validate({**v1, "transport_retries": 2})
        self.assertIn("agent-assisted-v2 track", agent.system_prompt(self.plan))

    def test_responses_roundtrip_carries_reasoning_and_records_attempt(self):
        model = self.model("responses")
        record, call = self.run_slot(model, self.replies(model))
        self.assertEqual(record["outcome"], "submitted", record)
        self.assertEqual((record["protocol"], record["attempt"], record["try"]), ("agent-assisted-v2", 1, 1))
        self.assertEqual((record["transport_retries"], record["incomplete_usage"]), ([], False))
        self.assertEqual(record["usage"], {"input_tokens": 300, "output_tokens": 150})
        second = call.call_args_list[1].args[1]
        self.assertEqual(second["input"][1]["encrypted_content"], "opaque")
        self.assertEqual(second["input"][2]["call_id"], "call_1")
        self.assertEqual(second["input"][3]["type"], "function_call_output")
        self.assertTrue((self.root / "run" / f"opencode-go-{model['id']}" / PROBLEM / "rep1/dut.v").is_file())

    def test_later_round_writes_its_own_repetition(self):
        model = self.model("chat/completions")
        record, _ = self.run_slot(model, self.replies(model), {**self.plan, "attempt": 2})
        self.assertEqual(record["attempt"], 2)
        slot = self.root / "run" / f"opencode-go-{model['id']}" / PROBLEM
        self.assertTrue((slot / "rep2/dut.v").is_file())
        self.assertFalse((slot / "rep1").exists())

    def test_dropped_stream_resends_the_identical_request(self):
        model = self.model("messages")
        replies = self.replies(model)
        replies.insert(1, agent.StreamFailure("stream_disconnected_before_terminal_event"))
        record, call = self.run_slot(model, replies)
        self.assertEqual(record["outcome"], "submitted", record)
        self.assertEqual(call.call_count, 4)
        self.assertEqual(call.call_args_list[1].args[1], call.call_args_list[2].args[1])
        self.assertEqual(call.call_args_list[2].kwargs["evidence"].name, "retry-1")
        self.assertEqual(record["transport_retries"], [
            {"turn": 2, "retry": 1, "reason": "stream_disconnected_before_terminal_event", "usage_known": False}])
        self.assertTrue(record["incomplete_usage"])
        self.assertEqual(record["turns"], 3)
        agent.time.sleep.assert_called_once_with(30)

    def test_rejected_requests_retry_only_transient_statuses(self):
        model = self.model("chat/completions")
        replies = self.replies(model)
        replies.insert(0, http_error(503))
        record, call = self.run_slot(model, replies, name="unavailable")
        self.assertEqual(record["outcome"], "submitted", record)
        self.assertEqual(record["transport_retries"][0]["usage_known"], True)
        self.assertFalse(record["incomplete_usage"])

        record, call = self.run_slot(model, [http_error(400)], name="bad-request")
        self.assertEqual((record["outcome"], call.call_count), ("provider_error", 1))
        record, call = self.run_slot(model, [agent.StreamFailure("stream_wall_timeout")], name="wall")
        self.assertEqual((record["outcome"], call.call_count), ("transport_interrupted", 1))

    def test_exhausted_quota_is_its_own_infrastructure_outcome(self):
        model = self.model("chat/completions")
        record, call = self.run_slot(model, [http_error(429)] * 3)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(record["outcome"], "quota_exhausted")
        self.assertEqual(record["execution_health"], "failed")
        self.assertEqual([retry["reason"] for retry in record["transport_retries"]], ["http_429", "http_429"])

    def test_v1_still_never_retries(self):
        v1 = read_plan(PLAN)
        with patch.object(agent, "now_utc", return_value=datetime(2026, 9, 23, 19, tzinfo=timezone.utc)):
            record, call = self.run_slot(v1["models"][0], [http_error(429)], v1)
        self.assertEqual((record["outcome"], call.call_count), ("provider_error", 1))
        self.assertNotIn("transport_retries", record)

    @unittest.skipUnless(os.environ.get("ADPBENCH_DOCKER_TEST_IMAGE"), "real pinned Docker toolchain opt-in")
    def test_real_v2_dev_then_heldout_no_model_calls(self):
        plan = read_plan(V2_PLAN)  # unmodified, so held-out scoring accepts the saved plan
        model = next(model for model in plan["models"] if model["api"] == "responses")
        image = os.environ["ADPBENCH_DOCKER_TEST_IMAGE"]
        out = self.root / "integration"
        with patch.object(agent, "due", return_value=True), patch.object(agent.Controller, "check", REAL_CHECK):
            record, _ = self.run_slot(model, self.replies(model), plan, name="integration", image=image)
        self.assertEqual(record["outcome"], "submitted", record)
        tools = json.loads((out.with_name(out.name + "-private") / "turn-02/tools.json").read_text())
        feedback = json.loads(tools[0]["content"])
        self.assertTrue(feedback["development_check_completed"], feedback)
        self.assertTrue(feedback["correct"], feedback)
        self.assertTrue(supervise(V2_PLAN, model["id"], out, self.root / "checkpoints", self.root / "diagnostics",
                                  image=image, problem_id=PROBLEM))
        final = json.loads((out / f"opencode-go-{model['id']}" / PROBLEM / "rep1/record.json").read_text())
        self.assertEqual((final["outcome"], final["attempt"]), ("correct", 1))
        self.assertEqual(final["result"]["ratio"], 1.0)
        self.assertIn("[agent-assisted-v2]", final["label"])
        self.assertTrue((out / f"opencode-go-{model['id']}" / PROBLEM / "rep1_frozen/dut.v").is_file())
        from scripts.publish_results import validate_records
        artifacts = self.root / "artifacts"
        shutil.copytree(out, artifacts / f"go-agent-records-{model['id']}-{PROBLEM}-123")
        validate_records(artifacts, plan, {"id": 123})

    def test_canary_round_trip_and_disabled_window(self):
        config = {**json.loads(CANARY.read_text()), "generation_enabled": False}
        model = self.model("responses")
        with patch.object(agent, "call_model") as call:
            record = agent.canary(config, model["id"], self.root / "off")
        self.assertEqual(record["status"], "not_requested")
        call.assert_not_called()

        config.update(generation_enabled=True, not_before="2026-09-27T00:00:00+00:00",
                      expires_at="2026-09-28T00:00:00+00:00")
        replies = [native_call(model, "write_file", {"path": "dut.v", "content": agent.CANARY_RTL}, "call_1"),
                   native_call(model, "check", {}, "call_2"), native_call(model, "submit", {}, "call_3")]
        with patch.object(agent, "call_model", side_effect=replies) as call:
            record = agent.canary(config, model["id"], self.root / "on")
        self.assertEqual(record["status"], "completed", record)
        self.assertTrue(record["tool_results_accepted"])
        self.assertEqual(record["tool_calls"], ["write_file", "check", "submit"])
        self.assertEqual(call.call_args_list[0].args[1]["max_output_tokens"], 20000)
        saved = json.loads((self.root / "on" / model["id"] / "canary.json").read_text())
        self.assertEqual(saved["status"], "completed")
        with self.assertRaises(FileExistsError):
            agent.canary(config, model["id"], self.root / "on")


if __name__ == "__main__":
    unittest.main()
