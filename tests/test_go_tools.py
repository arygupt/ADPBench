"""Offline native tool protocol tests; no model calls or shell execution."""
from copy import deepcopy
import unittest

from scripts.go_tools import request_body, parse_turn, tool_results


class GoToolsTest(unittest.TestCase):
    def model(self, api):
        return {"id": "fixture", "api": api, "reasoning_effort": "low"}

    def test_native_schemas_are_same_allowlisted_actions_and_do_not_mutate_history(self):
        history = [{"role": "user", "content": "Read the task."}]
        before = deepcopy(history)
        for api in ("chat/completions", "messages"):
            model = self.model(api)
            model["token_limit_key"] = "max_completion_tokens"
            body = request_body(model, {"max_output_tokens": {"fixture": 1000}}, "system", history)
            self.assertEqual(body["max_completion_tokens"], 1000)
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertTrue(body["stream"])
            tools = body["tools"] if api == "messages" else [t["function"] for t in body["tools"]]
            self.assertEqual([t["name"] for t in tools], ["read_file", "write_file", "check", "submit"])
            for t in tools:
                schema = t.get("input_schema", t.get("parameters"))
                self.assertFalse(schema["additionalProperties"])
            self.assertEqual(history, before)

    def test_chat_roundtrip_preserves_reasoning_and_native_argument_strings(self):
        message = {"role": "assistant", "content": "I will inspect the task.",
                   "reasoning_content": "private reasoning", "tool_calls": [
                       {"id": "read1", "type": "function", "function": {
                           "name": "read_file", "arguments": '{"path":"dut.py"}'}},
                       {"id": "read2", "type": "function", "function": {
                           "name": "read_file", "arguments": '{"path":"dut.v"}'}}]}
        data = {"choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 3}}
        assistant, calls, text, finish, usage = parse_turn(data, "chat/completions")
        self.assertEqual(assistant, message)
        self.assertEqual(calls[0], {"id": "read1", "name": "read_file", "arguments": {"path": "dut.py"}})
        results = tool_results("chat/completions", [{"id": c["id"], "content": "fixture", "is_error": False} for c in calls])
        self.assertEqual([r["tool_call_id"] for r in results], ["read1", "read2"])
        self.assertEqual(finish, "tool_calls")
        self.assertEqual(usage["completion_tokens"], 3)
        self.assertIn("inspect", text)

    def test_messages_roundtrip_preserves_thinking_signatures_and_redacted_blocks(self):
        blocks = [{"type": "thinking", "thinking": "plan", "signature": "opaque"},
                  {"type": "redacted_thinking", "data": "redacted-opaque"},
                  {"type": "text", "text": "Checking."},
                  {"type": "tool_use", "id": "call1", "name": "check", "input": {}}]
        data = {"content": blocks, "stop_reason": "tool_use", "usage": {"output_tokens": 5}}
        assistant, calls, text, finish, usage = parse_turn(data, "messages")
        self.assertEqual(assistant, {"role": "assistant", "content": blocks})
        self.assertEqual(calls, [{"id": "call1", "name": "check", "arguments": {}}])
        messages = tool_results("messages", [{"id": "call1", "content": "compilation failed", "is_error": True}])
        self.assertEqual(messages, [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1",
                                                                 "content": "compilation failed", "is_error": True}]}])
        self.assertEqual(text, "Checking.")
        self.assertEqual(finish, "tool_use")
        self.assertEqual(usage["output_tokens"], 5)

    def test_invalid_actions_have_corrective_errors_and_never_executable_arguments(self):
        cases = [("shell", '{"command":"rm -rf /"}'), ("read_file", '{"path":"../secret"}'),
                 ("write_file", '{"path":"check.sh","content":"evil"}'),
                 ("write_file", '{"path":"dut.v","content":42}'), ("check", '{"command":"x"}'),
                 ("submit", '{broken'), ("submit", "[]"), ("read_file", "{}")]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                native = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call1", "type": "function", "function": {"name": name, "arguments": arguments}}]}
                data = {"choices": [{"message": native, "finish_reason": "tool_calls"}]}
                assistant, calls, *_ = parse_turn(data, "chat/completions")
                self.assertIsNone(calls[0]["arguments"])
                self.assertTrue(calls[0]["error"])
                self.assertEqual(assistant, native)

    def test_duplicate_ids_invalid_ids_and_legacy_calls_fail_closed(self):
        native = {"role": "assistant", "tool_calls": [{"id": "id1", "type": "function", "function": {
            "name": "submit", "arguments": "{}"}}]}
        duplicate = deepcopy(native)
        duplicate["tool_calls"] *= 2
        invalid = deepcopy(native)
        invalid["tool_calls"][0]["id"] = ""
        for message in (duplicate, invalid, {"function_call": {"name": "submit"}}):
            with self.assertRaises(ValueError):
                parse_turn({"choices": [{"message": message}]}, "chat/completions")
        with self.assertRaises(ValueError):
            tool_results("messages", [{"id": "same", "content": "ok"}] * 2)

    def test_regular_assistant_text_has_no_implied_file_write_or_submission(self):
        for api, data in (("chat/completions", {"choices": [{"message": {"content": "module dut; endmodule"}, "finish_reason": "stop"}]}),
                          ("messages", {"content": [{"type": "text", "text": "module dut; endmodule"}], "stop_reason": "end_turn"})):
            _, calls, text, _, _ = parse_turn(data, api)
            self.assertEqual(calls, [])
            self.assertEqual(text, "module dut; endmodule")


if __name__ == "__main__":
    unittest.main()
