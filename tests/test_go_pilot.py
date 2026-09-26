"""Guard the finite schedule and billable request count without spending tokens."""
import json
import io
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts.go_pilot import (
    due, extract_rtl, gate, generate, parse_response, read_plan, request_body,
    validate_plan, valid_usage, response_diagnostics,
    provider_error_evidence, ERROR_BODY_LIMIT, output_limit,
    frozen_path, plan_slots, slot_dir,
)


class GoPilotTest(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan()
        self.model = self.plan["models"][0]
        self.now = datetime.fromisoformat("2026-09-21T00:10:00+00:00")

    def test_only_deepseek_is_scheduled(self):
        self.assertEqual([m["id"] for m in self.plan["models"]], ["deepseek-v4.1-flash"])
        self.assertEqual(len(self.plan["problems"]), 4)

    def test_no_early_expired_or_yearly_repeats(self):
        for when in ["2026-09-20T23:59:00+00:00", "2026-09-22T00:00:00+00:00", "2027-09-21T00:07:00+00:00"]:
            self.assertFalse(due(self.plan, self.model, datetime.fromisoformat(when)))
        self.assertTrue(due(self.plan, self.model, self.now))
        self.assertFalse(gate(self.plan, "", self.now)["run"])
        self.assertFalse(gate(self.plan, "unrecognized cron", self.now)["run"])
        self.assertTrue(gate(self.plan, self.model["cron"], self.now)["run"])

    def test_payload_caps_output(self):
        body = request_body(self.model, "code task", self.plan)
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(body["model"], "deepseek-v4.1-flash")
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)

    def test_extract_rejects_incomplete_or_ambiguous_output(self):
        self.assertEqual(extract_rtl("```verilog\nmodule dut; endmodule\n```"), "module dut; endmodule\n")
        for text in ["module dut;", "hello", "```sv\nmodule dut; endmodule\n```\n```sv\nmodule dut; endmodule\n```"]:
            with self.assertRaises(ValueError):
                extract_rtl(text)

    def response(self, finish="stop"):
        return {"id": "test", "choices": [{"message": {"content": "module dut; endmodule"}, "finish_reason": finish}], "usage": {"prompt_tokens": 100, "completion_tokens": 30}}

    def test_four_requests_and_local_reentry_guard(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "fake"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=self.response()) as call:
            generate(self.plan, self.model, Path(tmp))
            self.assertEqual(call.call_count, 4)
            with self.assertRaises(FileExistsError):
                generate(self.plan, self.model, Path(tmp))
            self.assertEqual(call.call_count, 4)

    def test_provider_error_aborts_without_retries(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "private provider error", {}, None)
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "fake"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", side_effect=error) as call:
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                generate(self.plan, self.model, Path(tmp))
            self.assertEqual(call.call_count, 1)
            records = list(Path(tmp).glob("**/generation.json"))
            self.assertEqual(len(records), 4)
            for path in records:
                self.assertNotIn("private provider error", path.read_text())
                self.assertTrue(json.loads(path.read_text())["error"])

    def test_provider_error_body_is_bounded_private_and_redacted(self):
        body = b'{"error":"bad parameter; Bearer otherlongsecret; key=fixture-secret"}'
        error = urllib.error.HTTPError("https://example.invalid", 400, "ignored reason", {"Authorization":"never save"}, io.BytesIO(body))
        evidence = provider_error_evidence(error, "fixture-secret")
        self.assertEqual(evidence["status"], 400)
        self.assertTrue(evidence["body_available"])
        self.assertIn("bad parameter", evidence["body"])
        self.assertNotIn("fixture-secret", json.dumps(evidence))
        self.assertNotIn("otherlongsecret", json.dumps(evidence))
        self.assertNotIn("Authorization", evidence)
        error = urllib.error.HTTPError("https://example.invalid", 400, "", {}, io.BytesIO(b"x" * (ERROR_BODY_LIMIT + 2)))
        evidence = provider_error_evidence(error, "fixture-secret")
        self.assertTrue(evidence["truncated"])
        self.assertEqual(len(evidence["body"]), ERROR_BODY_LIMIT)

    def test_truncation_does_not_trigger_repairs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "fake"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=self.response("length")) as call:
            generate(self.plan, self.model, Path(tmp))
            self.assertEqual(call.call_count, 4)
            self.assertFalse(list(Path(tmp).glob("**/dut.v")))

    def test_missing_accounting_stops_further_spend(self):
        response = self.response()
        del response["usage"]
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "fake"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=response) as call:
            with self.assertRaisesRegex(RuntimeError, "accounting"):
                generate(self.plan, self.model, Path(tmp))
            self.assertEqual(call.call_count, 1)

    def test_no_calls_outside_schedule_or_with_oversized_prompt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "fake"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model") as call:
            plan = {**self.plan, "max_prompt_bytes": 1}
            with self.assertRaises(RuntimeError):
                generate(plan, self.model, Path(tmp))
            call.assert_not_called()
        with patch("scripts.go_pilot.now_utc", return_value=datetime.fromisoformat("2026-09-20T23:00:00+00:00")), patch("scripts.go_pilot.call_model") as call:
            with self.assertRaises(RuntimeError):
                generate(self.plan, self.model, Path("unused"))
            call.assert_not_called()


class CostScreenTest(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-cost-screen-20260921.json")
        self.now = datetime.fromisoformat("2026-09-21T19:00:00+00:00")

    def test_six_requests_with_explicit_reasoning_settings(self):
        self.assertEqual(len(self.plan["models"]) * len(self.plan["problems"]), 6)
        self.assertEqual([m["id"] for m in self.plan["models"]], ["mimo-v2.5", "qwen3.8-flash", "glm-5.3-flash"])
        for model in self.plan["models"]:
            body = request_body(model, "RTL coding task", self.plan)
            self.assertEqual(body[model.get("token_limit_key", "max_tokens")], 8192)
            self.assertNotIn("tools", body)
            self.assertEqual(body["thinking"]["type"], "enabled" if model["id"].startswith("glm") else "disabled")
        self.assertEqual(request_body(self.plan["models"][2], "task", self.plan)["reasoning_effort"], "low")
        self.assertFalse(gate(self.plan, "", self.now)["run"])

    def test_no_oversized_duplicate_or_unsafe_plans(self):
        for change in [
            {"max_output_tokens": 0}, {"max_output_tokens": True},
            {"request_timeout_s": 601}, {"max_prompt_bytes": 24001},
            {"problems": ["001_dot_product"] * 2}, {"problems": ["../../secret"]},
            {"models": self.plan["models"] * 2}, {"name": "../escape"},
            {"expires_at": "2028-09-23T00:00:00+00:00"},
            {"models": [{**self.plan["models"][0], "api": "https://example.invalid"}]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan({**self.plan, **change})

    def test_stop_after_first_truncated_response(self):
        response = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {"prompt_tokens": 10, "completion_tokens": 8192}}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "fake"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=response) as call:
            with self.assertRaisesRegex(RuntimeError, "output cap"):
                generate(self.plan, self.plan["models"][0], Path(tmp))
            self.assertEqual(call.call_count, 1)
            self.assertEqual(len(list(Path(tmp).glob("**/generation.json"))), 2)
            self.assertFalse(list(Path(tmp).glob("**/dut.v")))
            request = json.loads(next(Path(tmp).glob("**/request.json")).read_text())
            self.assertEqual(request["thinking"], {"type": "disabled"})
            self.assertNotIn("fake", json.dumps(request))

    def test_messages_response_and_usage_limits(self):
        response = {"content": [{"type": "text", "text": "module dut; endmodule"}], "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 20}}
        text, finish, usage = parse_response(response, "messages")
        self.assertEqual(text, "module dut; endmodule")
        self.assertEqual(finish, "end_turn")
        self.assertTrue(valid_usage(usage, "messages", 8192))
        for usage in [{}, {"prompt_tokens": 10, "completion_tokens": 8193}, {"prompt_tokens": 10, "completion_tokens": -1}, {"prompt_tokens": 10, "completion_tokens": True}]:
            self.assertFalse(valid_usage(usage, "chat/completions", 8192))


class CoreModelsTest(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-core-20260922.json")
        self.now = datetime.fromisoformat("2026-09-22T01:00:00+00:00")

    def test_twelve_bounded_requests_and_correct_mimo_control(self):
        self.assertEqual(len(self.plan["models"]) * len(self.plan["problems"]), 12)
        self.assertFalse(self.plan["stop_on_invalid_output"])
        body = request_body(self.plan["models"][0], "task", self.plan)
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertNotIn("thinking", body)
        self.assertEqual(body["max_completion_tokens"], 8192)
        with self.assertRaises(ValueError):
            validate_plan({**self.plan, "problems": ["001_dot_product", "002_gemv", "003_matmul"]})

    def test_reasoning_is_recorded_despite_zero_provider_reasoning_count(self):
        response = {"choices": [{"message": {"content": None, "reasoning": "hidden diagnostic", "reasoning_details": [{"type": "reasoning.text"}]}, "finish_reason": "length"}], "usage": {"prompt_tokens": 10, "completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 0}}}
        self.assertEqual(response_diagnostics(response, "chat/completions")["reasoning_chars"], 17)
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "test-credential"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=response) as call:
            generate(self.plan, self.plan["models"][0], Path(tmp))
            self.assertEqual(call.call_count, 2)
            self.assertEqual(len(list(Path(tmp).glob("**/response.json"))), 2)
            self.assertFalse(list(Path(tmp).glob("**/dut.v")))
            record = json.loads(next(Path(tmp).glob("**/generation.json")).read_text())
            self.assertEqual(record["response_diagnostics"]["reasoning_chars"], 17)

    def test_bad_accounting_never_creates_a_submission(self):
        response = {"choices": [{"message": {"content": "module dut; endmodule"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 8193}}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY": "test-credential"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=response):
            with self.assertRaises(RuntimeError):
                generate(self.plan, self.plan["models"][0], Path(tmp))
            self.assertFalse(list(Path(tmp).glob("**/dut.v")))


class ProviderMaximumTest(unittest.TestCase):
    def test_new_authorized_batch_has_fresh_identity_full_limits_and_publication(self):
        root = Path(__file__).resolve().parent.parent
        path = "pilot/go-core-provider-max-20260923.json"
        plan = read_plan(root / path)
        previous = read_plan(root / "pilot/go-core-provider-max-20260922.json")
        self.assertNotEqual(plan["name"],previous["name"])
        self.assertFalse(previous["generation_enabled"])
        self.assertTrue(plan["generation_enabled"])
        self.assertEqual(plan["max_output_tokens"],previous["max_output_tokens"])
        self.assertEqual(plan["request_wall_timeout_s"],3600)
        self.assertEqual(len(plan["models"])*len(plan["problems"]),12)
        policy = json.loads((root / "pilot/publication-policy.json").read_text())
        self.assertIn(path,policy["workflows"][".github/workflows/go-core.yml"]["plans"])
        workflow = (root / ".github/workflows/go-core.yml").read_text()
        self.assertIn(f"  PLAN: {path}",workflow)
        self.assertIn("timeout-minutes: 135",workflow)
        for model in plan["models"]:
            self.assertTrue(due(plan,model,datetime.fromisoformat("2026-09-23T19:00:00+00:00")))
            self.assertFalse(due(plan,model,datetime.fromisoformat("2026-09-25T00:00:00+00:00")))
            body = request_body(model,"task",plan)
            self.assertEqual(body[model.get("token_limit_key","max_tokens")],plan["max_output_tokens"][model["id"]])
            if model["id"] == "glm-5.3-flash":
                self.assertNotIn("thinking",body)

    def setUp(self):
        self.plan = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-core-provider-max-20260922.json")
        self.now = datetime.fromisoformat("2026-09-22T01:00:00+00:00")

    def test_every_model_gets_its_full_reviewed_limit(self):
        expected = {"mimo-v2.5":128000, "deepseek-v4.1-flash":384000,
                    "qwen3.8-flash":131072, "glm-5.3-flash":131072,
                    "kimi-k2.6":65536, "minimax-m2.7":131072}
        self.assertEqual(self.plan["max_output_tokens"], expected)
        for model in self.plan["models"]:
            body = request_body(model, "task", self.plan)
            key = model.get("token_limit_key", "max_tokens")
            self.assertEqual(body[key], expected[model["id"]])
            self.assertIs(type(body[key]), int)  # Never send null/Infinity/dict to Messages.
            self.assertTrue(body["stream"])
            if model["api"] == "chat/completions":
                self.assertEqual(body["stream_options"], {"include_usage": True})
            self.assertNotIn("tools", body)

    def test_no_harness_wide_8192_ceiling(self):
        legacy = read_plan()
        for cap in (8193, 65536, 384000):
            changed = {**legacy, "max_output_tokens":cap}
            validate_plan(changed)
            self.assertEqual(output_limit(changed, changed["models"][0]), cap)
        self.assertEqual(legacy["max_output_tokens"], 8192)

    def test_missing_extra_or_invalid_provider_limits_are_rejected(self):
        limits = self.plan["max_output_tokens"]
        for change in [{"max_output_tokens":8192}, {"max_output_tokens":{}},
                       {"max_output_tokens":{**limits,"unreviewed-model":99999}},
                       *[{"max_output_tokens":{**limits,"kimi-k2.6":v}} for v in (None, True, -1, 0, 1.5, "unlimited")],
                       {"output_budget":"unlimited"}, {"generation_enabled":"true"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan({**self.plan, **change})

    def test_configuring_limits_does_not_authorize_new_calls(self):
        for model in self.plan["models"]:
            self.assertFalse(due(self.plan, model, self.now))
            with patch("scripts.go_pilot.call_model") as call, patch("scripts.go_pilot.now_utc", return_value=self.now):
                with self.assertRaisesRegex(RuntimeError, "authorized schedule"):
                    generate(self.plan, model, Path("unused"))
                call.assert_not_called()

    def test_large_complete_outputs_accepted_truncation_still_rejected(self):
        # Explicitly enable a temporary test-only plan; every request is mocked.
        plan = {**self.plan, "generation_enabled":True}
        model = plan["models"][0]
        for finish, tokens in [("stop",20000), ("length",128000)]:
            response = {"id":"test", "choices":[{"message":{"content":"module dut; endmodule"},"finish_reason":finish}],
                        "usage":{"prompt_tokens":100,"completion_tokens":tokens}}
            with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"OPENCODE_GO_API_KEY":"test-credential"}), patch("scripts.go_pilot.now_utc", return_value=self.now), patch("scripts.go_pilot.call_model", return_value=response) as call:
                generate(plan, model, Path(tmp))
                self.assertEqual(call.call_count, 2)  # Still no retry or continuation.
                self.assertEqual(len(list(Path(tmp).glob("**/dut.v"))), 2 if finish == "stop" else 0)
                for path in Path(tmp).glob("**/generation.json"):
                    self.assertEqual(json.loads(path.read_text())["max_output_tokens"], 128000)
        self.assertFalse(valid_usage({"prompt_tokens":1,"completion_tokens":128001}, "chat/completions", 128000))


class RoundPlanTest(unittest.TestCase):
    """agent-assisted-v2 rounds: larger matrices, attempts, reruns, and the responses API."""

    def setUp(self):
        self.plan = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-agent-v2-r1.json")

    def test_reviewed_round_is_problem_major(self):
        slots = plan_slots(self.plan)
        self.assertEqual(len(slots), 56)
        self.assertEqual({problem for _, problem in slots[:14]}, {"001_dot_product"})
        self.assertEqual(len({model for model, _ in slots[:14]}), 14)
        self.assertEqual(slot_dir(Path("out"), "m", "p", 3), Path("out/opencode-go-m/p/rep3"))
        self.assertEqual(frozen_path(Path("out/opencode-go-m/p/rep3")), Path("out/opencode-go-m/p/rep3_frozen/dut.v"))

    def test_round_bounds(self):
        extra = [{**self.plan["models"][0], "id": f"extra-{index}"} for index in range(3)]
        too_many = {**self.plan, "models": self.plan["models"] + extra,
                    "max_output_tokens": {**self.plan["max_output_tokens"], **{m["id"]: 1000 for m in extra}}}
        for changed in (too_many, {**self.plan, "attempt": 4}, {**self.plan, "attempt": 0},
                        {**self.plan, "try": 3}, {**self.plan, "try": 2}):
            with self.assertRaises(ValueError):
                validate_plan(changed)
        # A single-shot plan keeps its twelve-request ceiling.
        single = {**self.plan, "protocol": "single-shot", "models": self.plan["models"][8:]}
        single["max_output_tokens"] = {m["id"]: 1000 for m in single["models"]}
        with self.assertRaisesRegex(ValueError, "twelve"):
            validate_plan(single)

    def test_rerun_lists_only_reviewed_slots(self):
        rerun = {**self.plan, "try": 2, "slots": [{"model": "kimi-k3", "problem": "002_gemv"},
                                                  {"model": "gpt-6-luna", "problem": "001_dot_product"}]}
        validate_plan(rerun)
        self.assertEqual(plan_slots(rerun), [("gpt-6-luna", "001_dot_product"), ("kimi-k3", "002_gemv")])
        for slots in ([], [{"model": "unknown", "problem": "002_gemv"}],
                      [{"model": "kimi-k3", "problem": "002_gemv"}] * 2,
                      [{"model": "kimi-k3", "problem": "002_gemv", "attempt": 2}]):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                validate_plan({**rerun, "slots": slots})

    def test_responses_models_need_explicit_effort_and_v2(self):
        index = next(i for i, m in enumerate(self.plan["models"]) if m["api"] == "responses")
        for change in ({"reasoning": {"enabled": True}}, {"reasoning": {"effort": "extreme"}},
                       {"token_limit_key": "max_tokens"}, {"reasoning_effort": "high"}):
            models = list(self.plan["models"])
            models[index] = {**models[index], **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan({**self.plan, "models": models})
        v1 = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-agent-20260923.json")
        responses = {**self.plan["models"][index], "not_before": v1["models"][0]["not_before"]}
        with self.assertRaisesRegex(ValueError, "agent-assisted-v2"):
            validate_plan({**v1, "models": [responses], "max_output_tokens": {responses["id"]: 1000}})


if __name__ == "__main__":
    unittest.main()
