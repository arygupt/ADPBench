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


if __name__ == "__main__":
    unittest.main()
