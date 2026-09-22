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


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def read(self, chunks, api="chat/completions", key="fixture-secret"):
        return read_stream(Chunks(chunks), api, self.root, key, time.monotonic()+5)

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


if __name__ == "__main__":
    unittest.main()
