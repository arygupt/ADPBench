"""Zero-token tests for the six-request, subscription-only diagnostic guard."""
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts.go_canary import CONFIG, run, validate


class CanaryTest(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        for p in [patch.dict("os.environ",{"OPENCODE_GO_API_KEY":"fake-test-credential"}),
                  patch("scripts.go_canary.now_utc",return_value=datetime.fromisoformat("2026-09-23T04:00:00+00:00"))]:
            p.start()
            self.addCleanup(p.stop)

    def response(self, model, body, *args, **kwargs):
        text = "module dut(input wire a,b,output wire y); assign y=a^b; endmodule"
        if model["api"] == "messages":
            return {"content":[{"type":"text","text":text}],"stop_reason":"end_turn",
                    "usage":{"input_tokens":50,"output_tokens":20}}
        return {"choices":[{"message":{"content":text},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":50,"completion_tokens":20}}

    def test_six_single_requests_768_total_and_local_reentry_guard(self):
        with patch("scripts.go_canary.call_model",side_effect=self.response) as call:
            result = run(self.config,self.out,subscription_only=True)
            self.assertEqual(call.call_count,6)
            self.assertEqual(call.call_args_list[0].args[0]["id"],"glm-5.3-flash")
            self.assertEqual(result["maximum_output_tokens"],768)
            self.assertEqual(result["reported_output_tokens"],120)
            self.assertFalse(result["benchmark_results"])
            for entry in call.call_args_list:
                model,body = entry.args[:2]
                self.assertEqual(body[model.get("token_limit_key","max_tokens")],128)
                self.assertTrue(body["stream"])
                self.assertNotIn("tools",body)
                self.assertEqual(entry.kwargs["wall_timeout"],90)
            with self.assertRaises(FileExistsError):
                run(self.config,self.out,subscription_only=True)
            self.assertEqual(call.call_count,6)

    def test_glm_rejection_is_preserved_without_retry_and_other_models_tested(self):
        def respond(model,*args,**kwargs):
            if model["id"] == "glm-5.3-flash":
                raise urllib.error.HTTPError("https://opencode.ai/zen/go/v1/chat/completions",400,"private",{},
                                             io.BytesIO(b'{"error":"parameter rejected; fake-test-credential"}'))
            return self.response(model,*args,**kwargs)
        with patch("scripts.go_canary.call_model",side_effect=respond) as call:
            result = run(self.config,self.out,subscription_only=True)
            self.assertEqual(call.call_count,6)
            self.assertEqual(result["models"][0]["status"],"http_400")
            evidence = (self.out / "glm-5.3-flash/provider_error.json").read_text()
            self.assertIn("parameter rejected",evidence)
            self.assertNotIn("fake-test-credential",evidence)
            self.assertNotIn("parameter rejected",(self.out / "REPORT.md").read_text())

    def test_quota_or_auth_rejection_stops_all_remaining_spend(self):
        for code in (401,402,403,429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                error = urllib.error.HTTPError("https://opencode.ai/zen/go/v1/chat/completions",code,"",{},io.BytesIO(b"quota"))
                with patch("scripts.go_canary.call_model",side_effect=error) as call:
                    result = run(self.config,Path(tmp),subscription_only=True)
                    self.assertEqual(call.call_count,1)
                    self.assertEqual(result["requests_started"],1)
                    self.assertTrue(result["incomplete_usage"])
                    self.assertTrue(all(r["status"] == "not_requested" for r in result["models"][1:]))

    def test_no_requests_without_subscription_confirmation_or_current_window(self):
        with patch("scripts.go_canary.call_model") as call:
            with self.assertRaises(RuntimeError):
                run(self.config,self.out,subscription_only=False)
            with patch("scripts.go_canary.now_utc",return_value=datetime.fromisoformat("2026-09-25T00:00:00+00:00")):
                with self.assertRaises(RuntimeError):
                    run(self.config,self.out,subscription_only=True)
            call.assert_not_called()

    def test_invalid_budgets_models_prompts_and_windows_never_call_provider(self):
        for change in [{"max_output_tokens":129},{"max_output_tokens":True},{"models":["unreviewed-model"]},
                       {"models":self.config["models"]*2},{"wall_timeout_s":91},
                       {"source_plan":"../outside.json"},{"expires_at":"2026-09-26T00:00:00+00:00"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate({**self.config,**change})
        with patch("scripts.go_canary.call_model") as call, self.assertRaises(ValueError):
            run({**self.config,"max_prompt_bytes":1},self.out,subscription_only=True)
        call.assert_not_called()

    def test_cap_is_not_misreported_as_benchmark_failure_or_successful_answer(self):
        def capped(model,body,*args,**kwargs):
            result = self.response(model,body)
            if model["api"] == "messages":
                result.update(content=[],stop_reason="max_tokens")
                result["usage"]["output_tokens"] = 128
            else:
                result["choices"][0].update(message={"content":""},finish_reason="length")
                result["usage"]["completion_tokens"] = 128
            return result
        with patch("scripts.go_canary.call_model",side_effect=capped):
            result = run(self.config,self.out,subscription_only=True)
            self.assertEqual(result["reported_output_tokens"],768)
            self.assertTrue(all(r["status"] == "accepted_output_cap" and not r["answer_present"] for r in result["models"]))
            self.assertFalse(list(self.out.glob("**/dut.v")))

    def test_invalid_accounting_stops_following_requests(self):
        with patch("scripts.go_canary.call_model",return_value={"choices":[{"message":{"content":"code"},"finish_reason":"stop"}]} ) as call:
            result = run(self.config,self.out,subscription_only=True)
            self.assertEqual(call.call_count,1)
            self.assertEqual(result["models"][0]["status"],"invalid_accounting")
            self.assertEqual(result["reported_output_tokens"],0)
            self.assertTrue(result["incomplete_usage"])


if __name__ == "__main__":
    unittest.main()
