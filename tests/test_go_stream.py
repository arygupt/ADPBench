"""Offline SSE fault injection: zero API calls, no subscription consumption."""
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.go_stream import call_streaming, read_stream, StreamFailure


def sse(*events):
    return b"".join(b"data: " + (e.encode() if isinstance(e, str) else json.dumps(e).encode()) + b"\n\n" for e in events)


def chat(text="", finish=None, **extra):
    return {"id":"test-stream", "model":"fixture", "choices":[{"index":0, "delta":{"content":text},
                                                               "finish_reason":finish}], **extra}


class Chunks:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read1(self, size):
        chunk = next(self.chunks, b"")
        if isinstance(chunk, Exception):
            raise chunk
        return chunk


class StreamCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def read(self, chunks, api="chat/completions", key="fixture-secret", *, allow_tools=False):
        return read_stream(Chunks(chunks), api, self.root, key, time.monotonic()+5, allow_tools=allow_tools)


class StreamTest(StreamCase):

    def test_chat_arbitrary_byte_boundaries_and_late_usage(self):
        payload = sse(chat("module "), chat("dut; endmodule", "stop"),
                      {"choices":[], "usage":{"prompt_tokens":5, "completion_tokens":8}}, "[DONE]")
        result = self.read([payload[i:i+7] for i in range(0,len(payload),7)])
        self.assertEqual(result["choices"][0]["message"]["content"], "module dut; endmodule")
        self.assertEqual(result["usage"]["completion_tokens"], 8)
        self.assertEqual(json.loads((self.root / "stream-status.json").read_text())["state"], "completed")

    def test_messages_thinking_and_cumulative_usage(self):
        result = self.read([sse(
            {"type":"message_start", "message":{"id":"m", "usage":{"input_tokens":12, "output_tokens":1}}},
            {"type":"content_block_start", "index":0, "content_block":{"type":"thinking", "thinking":""}},
            {"type":"content_block_delta", "index":0, "delta":{"type":"thinking_delta", "thinking":"reason"}},
            {"type":"content_block_stop", "index":0},
            {"type":"content_block_start", "index":1, "content_block":{"type":"text", "text":""}},
            {"type":"content_block_delta", "index":1, "delta":{"type":"text_delta", "text":"module dut; endmodule"}},
            {"type":"message_delta", "delta":{"stop_reason":"end_turn"}, "usage":{"output_tokens":25}},
            {"type":"message_stop"})], "messages")
        self.assertEqual(result["usage"], {"input_tokens":12,"output_tokens":25})
        self.assertEqual(result["content"][0]["thinking"], "reason")
        self.assertEqual(result["stop_reason"], "end_turn")

    def test_disconnect_keeps_partial_but_never_claims_completion(self):
        with self.assertRaisesRegex(StreamFailure, "disconnected"):
            self.read([sse(chat("module dut; endmodule", "stop"))])
        saved = json.loads((self.root / "response.partial.json").read_text())
        self.assertIn("endmodule", saved["choices"][0]["message"]["content"])
        self.assertEqual(json.loads((self.root / "stream-status.json").read_text())["state"], "incomplete")
        self.assertFalse((self.root / "dut.v").exists())

    def test_idle_and_wall_timeouts_save_progress(self):
        with self.assertRaisesRegex(StreamFailure, "idle_timeout"):
            self.read([sse(chat("module dut;")), TimeoutError()])
        self.assertIn("module dut", (self.root / "response.partial.json").read_text())
        with self.assertRaisesRegex(StreamFailure, "wall_timeout"):
            read_stream(Chunks([]), "messages", self.root, "", time.monotonic()-1)

    def test_split_credential_is_redacted_in_assembled_private_artifact(self):
        self.read([sse(chat("fixture-")), sse(chat("secret", "stop"), "[DONE]")])
        self.assertNotIn("fixture-secret", (self.root / "response.partial.json").read_text())
        self.assertIn("[REDACTED]", (self.root / "response.partial.json").read_text())

    def test_provider_stream_error_body_is_private_and_redacted(self):
        with self.assertRaisesRegex(StreamFailure, "provider_stream_error"):
            self.read([sse({"type":"error", "error":{"message":"bad field; fixture-secret"}})])
        body = (self.root / "provider_error.json").read_text()
        self.assertIn("bad field", body)
        self.assertNotIn("fixture-secret", body)
        self.assertNotIn("bad field", (self.root / "stream-status.json").read_text())

    def test_malformed_tool_and_premature_end_events_are_rejected(self):
        bad = [sse("[DONE]"), sse("{broken"), sse(chat("", None, error={"message":"rejected"})),
               sse({"choices":[{"delta":{"tool_calls":[{}]}}]})]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(StreamFailure):
                self.read([payload])
        with self.assertRaisesRegex(StreamFailure, "missing_message_start"):
            self.read([sse({"type":"message_stop"})], "messages")

    def test_byte_and_single_event_limits_are_fail_closed(self):
        with patch("scripts.go_stream.MAX_EVENT_BYTES", 10), self.assertRaisesRegex(StreamFailure, "event_limit"):
            self.read([sse(chat("large event"))])
        with patch("scripts.go_stream.MAX_STREAM_BYTES", 10), self.assertRaisesRegex(StreamFailure, "byte_limit"):
            self.read([sse(chat("large event"))])
        with patch("scripts.go_stream.MAX_CONTENT_BYTES", 4), self.assertRaisesRegex(StreamFailure, "assembled_output_byte_limit"):
            self.read([sse(chat("12")),sse(chat("345"))])
        self.assertEqual(json.loads((self.root / "response.partial.json").read_text())["choices"][0]["message"]["content"], "12")

    def test_http_rejection_never_reconnects_or_uses_zen_fallback(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 400
        response.read.return_value = b'{"error":"invalid field"}'
        with patch("scripts.go_stream.http.client.HTTPSConnection", return_value=conn) as create:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                call_streaming("chat/completions", {"stream":True}, {}, self.root, "", 120, 1800)
            self.assertIn(b"invalid field", caught.exception.read())
            caught.exception.close()
            create.assert_called_once_with("opencode.ai", timeout=120)
        conn.request.assert_called_once()
        self.assertEqual(conn.request.call_args.args[1], "/zen/go/v1/chat/completions")
        conn.close.assert_called_once()

    def test_success_transport_uses_response_socket_and_one_post(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [sse(chat("RTL", "stop"), "[DONE]")]
        with patch("scripts.go_stream.http.client.HTTPSConnection", return_value=conn):
            result = call_streaming("chat/completions", {"stream":True}, {}, self.root, "", 120, 1800)
        self.assertEqual(result["choices"][0]["message"]["content"], "RTL")
        conn.request.assert_called_once()
        conn.sock.settimeout.assert_called_once()

    def test_native_chat_multiple_function_deltas_and_reasoning_roundtrip(self):
        from scripts.go_tools import parse_turn
        payload = sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "plan "}}]},
            {"choices": [{"delta": {"reasoning_content": "complete", "tool_calls": [
                {"index": 0, "id": "call_a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}},
                {"index": 1, "id": "call_b", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"dut.v",'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 1, "function": {"arguments": '"content":"module dut; endmodule"}'}},
                {"index": 0, "function": {"arguments": '"dut.py"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 12}}, "[DONE]")
        data = self.read([payload[i:i+9] for i in range(0, len(payload), 9)], allow_tools=True)
        [message], calls, _, finish, _ = parse_turn(data, "chat/completions")
        self.assertEqual(message["reasoning_content"], "plan complete")
        self.assertNotIn("reasoning", message)
        self.assertEqual([c["id"] for c in calls], ["call_a", "call_b"])
        self.assertEqual(calls[0]["arguments"], {"path": "dut.py"})
        self.assertEqual(calls[1]["arguments"]["content"], "module dut; endmodule")
        self.assertEqual(finish, "tool_calls")

    def test_native_messages_input_deltas_thinking_signature_and_redacted_blocks(self):
        from scripts.go_tools import parse_turn
        events = (
            {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 12, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "plan"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "opaque"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "redacted_thinking", "data": "opaque2"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call1", "name": "read_file", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '"dut.v"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "call2", "name": "check", "input": {}}},
            {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": ""}},
            {"type": "content_block_stop", "index": 3},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 25}},
            {"type": "message_stop"})
        data = self.read([sse(*events)], "messages", allow_tools=True)
        [message], calls, _, finish, usage = parse_turn(data, "messages")
        self.assertEqual(message["content"][0], {"type": "thinking", "thinking": "plan", "signature": "opaque"})
        self.assertEqual(message["content"][1], {"type": "redacted_thinking", "data": "opaque2"})
        self.assertEqual(calls, [{"id": "call1", "name": "read_file", "arguments": {"path": "dut.v"}},
                                 {"id": "call2", "name": "check", "arguments": {}}])
        self.assertEqual(finish, "tool_use")
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 25})

    def test_tool_mode_malformed_and_truncated_turns_never_execute(self):
        prefix = [
            {"type": "message_start", "message": {"id": "m", "usage": {}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "id1", "name": "write_file", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}}]
        with self.assertRaisesRegex(StreamFailure, "invalid_tool_arguments_json"):
            self.read([sse(*prefix, {"type": "content_block_stop", "index": 0},
                           {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                           {"type": "message_stop"})], "messages", allow_tools=True)
        partial = json.loads((self.root / "response.partial.json").read_text())
        self.assertEqual(partial["content"][0]["partial_json"], '{"path":')
        self.assertEqual(json.loads((self.root / "stream-status.json").read_text())["state"], "incomplete")
        with self.assertRaisesRegex(StreamFailure, "unclosed_content_blocks"):
            self.read([sse(*prefix, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"})], "messages", allow_tools=True)
        tool = {"index": 0, "id": "id1", "type": "function", "function": {"name": "submit", "arguments": "{}"}}
        with self.assertRaisesRegex(StreamFailure, "incomplete_tool_turn"):
            self.read([sse({"choices": [{"delta": {"tool_calls": [tool]}, "finish_reason": "stop"}]}, "[DONE]")], allow_tools=True)

    def test_native_tool_caps_keep_terminal_usage_but_are_not_executable(self):
        from scripts.go_tools import parse_turn
        tool = {"index": 0, "id": "call1", "type": "function", "function": {"name": "write_file", "arguments": '{"path":'}}
        data = self.read([sse({"choices": [{"delta": {"tool_calls": [tool]}, "finish_reason": "length"}]},
                             {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 99}}, "[DONE]")], allow_tools=True)
        self.assertEqual(data["choices"][0]["finish_reason"], "length")
        self.assertEqual(data["usage"]["completion_tokens"], 99)
        self.assertEqual(json.loads((self.root / "stream-status.json").read_text())["state"], "completed")
        with self.assertRaisesRegex(ValueError, "did not finish"):
            parse_turn(data, "chat/completions")
        for close_block in (False, True):
            events = [
                {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 12, "output_tokens": 1}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call1", "name": "write_file", "input": {}}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}}]
            if close_block:
                events.append({"type": "content_block_stop", "index": 0})
            events.extend([{"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 99}},
                           {"type": "message_stop"}])
            with self.subTest(close_block=close_block):
                data = self.read([sse(*events)], "messages", allow_tools=True)
                self.assertEqual(data["stop_reason"], "max_tokens")
                self.assertEqual(data["usage"], {"input_tokens": 12, "output_tokens": 99})
                self.assertEqual(data["content"][0]["partial_json"], '{"path":')
                self.assertEqual(json.loads((self.root / "stream-status.json").read_text())["state"], "completed")
                with self.assertRaises(ValueError):
                    parse_turn(data, "messages")

    def test_native_tool_count_identifier_and_argument_limits_are_bounded(self):
        tool = {"index": 0, "id": "id1", "type": "function", "function": {"name": "submit", "arguments": "{}"}}
        for changed, reason in (({**tool, "index": 16}, "invalid_tool_index"),
                                ({**tool, "id": "x"*257}, "invalid_tool_identifier")):
            with self.assertRaisesRegex(StreamFailure, reason):
                self.read([sse({"choices": [{"delta": {"tool_calls": [changed]}}]})], allow_tools=True)
        with patch("scripts.go_stream.MAX_TOOL_ARGUMENT_BYTES", 1), self.assertRaisesRegex(StreamFailure, "tool_argument_byte_limit"):
            self.read([sse({"choices": [{"delta": {"tool_calls": [tool]}}]})], allow_tools=True)
        duplicate = {**tool, "index": 1}
        with self.assertRaisesRegex(StreamFailure, "duplicate_tool_call_id"):
            self.read([sse({"choices": [{"delta": {"tool_calls": [tool, duplicate]}, "finish_reason": "tool_calls"}]}, "[DONE]")], allow_tools=True)

    def test_messages_tool_blocks_still_rejected_by_single_shot_default(self):
        with self.assertRaisesRegex(StreamFailure, "unexpected_content_block"):
            self.read([sse({"type": "message_start", "message": {"id": "m", "usage": {}}},
                           {"type": "content_block_start", "index": 0, "content_block": {
                               "type": "tool_use", "id": "id1", "name": "submit", "input": {}}})], "messages")

    def test_transport_enables_tools_only_for_explicit_native_tools_body(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        response.read1.side_effect = [sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call1", "type": "function", "function": {"name": "submit", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}, "[DONE]")]
        with patch("scripts.go_stream.http.client.HTTPSConnection", return_value=conn):
            result = call_streaming("chat/completions", {"stream": True, "tools": [{"type": "function"}]}, {}, self.root, "", 120, 1800)
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "submit")


def created():
    return {"type": "response.created", "response": {"id": "resp_1", "model": "fixture", "output": [], "usage": None}}


def completed(output, usage=None, kind="response.completed", **extra):
    return {"type": kind, "response": {"id": "resp_1", "model": "fixture", "output": output,
                                       "usage": usage or {"input_tokens": 9, "output_tokens": 7}, **extra}}


class ResponsesStreamTest(StreamCase):
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_a", "name": "write_file", "arguments": ""}

    def tool_events(self, terminal_output=None):
        done_call = {**self.call, "arguments": '{"path":"dut.v","content":"module dut; endmodule"}', "status": "completed"}
        events = [
            created(),
            {"type": "response.output_item.added", "output_index": 0, "item": {**self.reasoning, "encrypted_content": None}},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "plan"},
            {"type": "response.output_item.done", "output_index": 0, "item": self.reasoning},
            {"type": "response.output_item.added", "output_index": 1, "item": self.call},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"path":"dut.v",'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"content":"module dut; endmodule"}'},
            {"type": "response.function_call_arguments.done", "output_index": 1, "arguments": done_call["arguments"]},
            {"type": "response.output_item.done", "output_index": 1, "item": done_call},
        ]
        output = [self.reasoning, done_call] if terminal_output is None else terminal_output
        usage = {"input_tokens": 9, "output_tokens": 7, "output_tokens_details": {"reasoning_tokens": 4}}
        return events + [completed(output, usage)], done_call

    def test_responses_tool_turn_keeps_encrypted_reasoning_for_the_next_turn(self):
        from scripts.go_tools import parse_turn
        events, done_call = self.tool_events()
        payload = sse(*events)
        data = self.read([payload[i:i+11] for i in range(0, len(payload), 11)], "responses", allow_tools=True)
        self.assertEqual(data["stop_reason"], "tool_calls")
        self.assertEqual(data["usage"]["output_tokens_details"], {"reasoning_tokens": 4})
        history, calls, _, finish, _ = parse_turn(data, "responses")
        self.assertEqual(history, [self.reasoning, done_call])
        self.assertEqual(calls, [{"id": "call_a", "name": "write_file",
                                  "arguments": {"path": "dut.v", "content": "module dut; endmodule"}}])
        self.assertEqual(finish, "tool_calls")

    def test_responses_empty_terminal_output_falls_back_to_done_items(self):
        events, done_call = self.tool_events(terminal_output=[])
        data = self.read([sse(*events)], "responses", allow_tools=True)
        self.assertEqual(data["output"], [self.reasoning, done_call])

    def test_responses_text_answer_is_a_stop(self):
        message = {"type": "message", "id": "msg_1", "role": "assistant", "content": []}
        final = {**message, "content": [{"type": "output_text", "text": "module dut; endmodule"}]}
        data = self.read([sse(created(),
                              {"type": "response.output_item.added", "output_index": 0, "item": message},
                              {"type": "response.output_text.delta", "output_index": 0, "delta": "module dut; endmodule"},
                              {"type": "response.output_item.done", "output_index": 0, "item": final},
                              completed([final]))], "responses")
        self.assertEqual(data["stop_reason"], "stop")
        from scripts.go_pilot import parse_response
        self.assertEqual(parse_response(data, "responses")[0], "module dut; endmodule")

    def test_responses_output_cap_is_complete_but_not_executable(self):
        from scripts.go_tools import parse_turn
        events = [created(),
                  {"type": "response.output_item.added", "output_index": 0, "item": self.call},
                  {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path":'},
                  completed([], kind="response.incomplete", incomplete_details={"reason": "max_output_tokens"})]
        data = self.read([sse(*events)], "responses", allow_tools=True)
        self.assertEqual(data["stop_reason"], "length")
        self.assertEqual(data["output"][0]["arguments"], '{"path":')
        self.assertEqual(json.loads((self.root / "stream-status.json").read_text())["state"], "completed")
        with self.assertRaises(ValueError):
            parse_turn(data, "responses")

    def test_responses_failure_disconnect_and_protocol_errors_fail_closed(self):
        with self.assertRaisesRegex(StreamFailure, "provider_stream_error"):
            self.read([sse(created(), {"type": "response.failed", "response": {"error": {"message": "overloaded; fixture-secret"}}})],
                      "responses", allow_tools=True)
        self.assertNotIn("fixture-secret", (self.root / "provider_error.json").read_text())
        events, _ = self.tool_events()
        with self.assertRaisesRegex(StreamFailure, "disconnected"):
            self.read([sse(*events[:6])], "responses", allow_tools=True)
        partial = json.loads((self.root / "response.partial.json").read_text())
        self.assertEqual(partial["output"][1]["arguments"], '{"path":"dut.v",')
        with self.assertRaisesRegex(StreamFailure, "missing_response_start"):
            self.read([sse(events[1])], "responses", allow_tools=True)
        with self.assertRaisesRegex(StreamFailure, "unexpected_tool_call"):
            self.read([sse(*events)], "responses")
        with self.assertRaisesRegex(StreamFailure, "invalid_content_delta"):
            self.read([sse(created(), {"type": "response.output_item.added", "output_index": 0, "item": self.reasoning},
                           {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{}"})],
                      "responses", allow_tools=True)
        with self.assertRaisesRegex(StreamFailure, "unexpected_content_block"):
            self.read([sse(created(), {"type": "response.output_item.added", "output_index": 0,
                                       "item": {"type": "web_search_call"}})], "responses", allow_tools=True)

    def test_responses_tool_limits_are_bounded(self):
        with patch("scripts.go_stream.MAX_TOOL_ARGUMENT_BYTES", 4), self.assertRaisesRegex(StreamFailure, "tool_argument_byte_limit"):
            self.read([sse(created(), {"type": "response.output_item.added", "output_index": 0, "item": self.call},
                           {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path":'})],
                      "responses", allow_tools=True)
        duplicate = {**self.call, "arguments": "{}"}
        with self.assertRaisesRegex(StreamFailure, "duplicate_tool_call_id"):
            self.read([sse(created(), completed([duplicate, {**duplicate, "id": "fc_2"}]))], "responses", allow_tools=True)

    def test_responses_transport_posts_to_the_responses_endpoint(self):
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "text/event-stream"
        events, _ = self.tool_events()
        response.read1.side_effect = [sse(*events)]
        with patch("scripts.go_stream.http.client.HTTPSConnection", return_value=conn):
            result = call_streaming("responses", {"stream": True, "tools": [{"type": "function"}]}, {}, self.root, "", 120, 1800)
        self.assertEqual(conn.request.call_args.args[1], "/zen/go/v1/responses")
        self.assertEqual(result["stop_reason"], "tool_calls")


if __name__ == "__main__":
    unittest.main()
