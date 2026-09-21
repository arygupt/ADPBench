"""Guard the finite schedule and billable request count without spending tokens."""
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts.go_pilot import (
    due, extract_rtl, gate, generate, parse_response, read_plan, request_body,
    validate_plan, valid_usage,
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
            {"max_output_tokens": 8193}, {"max_output_tokens": True},
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


if __name__ == "__main__":
    unittest.main()
