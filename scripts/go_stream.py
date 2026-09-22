"""Single-request Go SSE transport with durable partial output; never reconnects."""
from __future__ import annotations

import http.client
import io
import json
import re
import time
import urllib.error
from pathlib import Path

from adpbench.durable import atomic_json

MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024


class StreamFailure(RuntimeError):
    pass


class ResponseStream:
    """Assemble both Go wire formats. Only explicit protocol termination is complete."""
    def __init__(self, api: str):
        if api not in {"messages", "chat/completions"}:
            raise ValueError("unsupported streaming API")
        self.api = api
        self.data = {"id":"", "model":"", "usage":{}}
        self.text = []
        self.reasoning = []
        self.blocks = {}
        self.finish = ""
        self.done = False
        self.started = False
        self.provider_error = None

    def feed(self, payload: str) -> None:
        if self.done:
            raise StreamFailure("data_after_stream_end")
        if payload == "[DONE]":
            if self.api != "chat/completions" or not self.finish:
                raise StreamFailure("invalid_stream_end")
            self.done = True
            return
        try:
            event = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise StreamFailure("invalid_stream_json") from exc
        if not isinstance(event, dict):
            raise StreamFailure("invalid_stream_event")
        if event.get("error") or event.get("type") == "error":
            self.provider_error = event
            raise StreamFailure("provider_stream_error")
        if self.api == "chat/completions":
            self.started = True
            for key in ("id", "model"):
                if event.get(key):
                    self.data[key] = event[key]
            if event.get("usage"):
                self.data["usage"].update(event["usage"])
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise StreamFailure("multiple_stream_choices")
                delta = choice.get("delta", {})
                if delta.get("tool_calls") or delta.get("function_call"):
                    raise StreamFailure("unexpected_tool_call")
                for name, target in [("content",self.text), ("reasoning",self.reasoning), ("reasoning_content",self.reasoning)]:
                    value = delta.get(name)
                    if value is not None:
                        if not isinstance(value, str):
                            raise StreamFailure("invalid_text_delta")
                        target.append(value)
                if choice.get("finish_reason"):
                    self.finish = choice["finish_reason"]
            return
        kind = event.get("type")
        if not self.started and kind in {"content_block_start", "content_block_delta", "message_delta", "message_stop"}:
            raise StreamFailure("missing_message_start")
        if kind == "message_start":
            if self.started:
                raise StreamFailure("duplicate_message_start")
            self.started = True
            message = event["message"]
            self.data.update({k:message[k] for k in ("id", "model", "usage") if k in message})
        elif kind == "content_block_start":
            index = event["index"]
            if type(index) is not int or not 0 <= index < 100 or index in self.blocks:
                raise StreamFailure("invalid_content_index")
            block = event["content_block"]
            if block.get("type") not in {"text", "thinking", "redacted_thinking"}:
                raise StreamFailure("unexpected_content_block")
            self.blocks[index] = {**block, "pieces":[block.get("text",block.get("thinking",""))]}
        elif kind == "content_block_delta":
            block = self.blocks[event["index"]]
            delta = event["delta"]
            if delta["type"] in {"text_delta", "thinking_delta"}:
                field = "text" if delta["type"] == "text_delta" else "thinking"
                value = delta[field]
                if not isinstance(value, str) or block["type"] != ("text" if field == "text" else "thinking"):
                    raise StreamFailure("invalid_content_delta")
                block["pieces"].append(value)
            elif delta["type"] == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
        elif kind == "message_delta":
            self.finish = event.get("delta", {}).get("stop_reason") or self.finish
            self.data["usage"].update(event.get("usage", {}))  # Cumulative, never sum deltas.
        elif kind == "message_stop":
            if not self.started or not self.finish:
                raise StreamFailure("invalid_stream_end")
            self.done = True

    def snapshot(self) -> dict:
        data = dict(self.data)
        if self.api == "chat/completions":
            data["choices"] = [{"index":0, "message":{"role":"assistant", "content":"".join(self.text),
                                                       "reasoning":"".join(self.reasoning)}, "finish_reason":self.finish}]
        else:
            data["content"] = []
            for _, block in sorted(self.blocks.items()):
                value = {k:v for k,v in block.items() if k != "pieces"}
                if block["type"] in {"text", "thinking"}:
                    value["text" if block["type"] == "text" else "thinking"] = "".join(block["pieces"])
                data["content"].append(value)
            data["stop_reason"] = self.finish
        return data


def read_stream(response, api: str, evidence: Path, key: str, wall_deadline: float,
                before_read=lambda: None) -> dict:
    stream = ResponseStream(api)
    start = time.monotonic()
    saved = 0.0
    count = 0
    events = 0
    pending = b""
    lines = []
    failure = ""

    def save():
        # Save assembled content, not raw delta fragments which could split a credential.
        data = json.loads(json.dumps(stream.snapshot()).replace(key, "[REDACTED]")) if key else stream.snapshot()
        atomic_json(evidence / "response.partial.json", data)
        atomic_json(evidence / "stream-status.json", {"state":"completed" if stream.done else "incomplete",
                    "reason":failure, "bytes_received":count, "events":events,
                    "duration_s":round(time.monotonic()-start, 3)})
        if stream.provider_error is not None:
            body = json.dumps(stream.provider_error)
            if key:
                body = body.replace(key, "[REDACTED]")
            body = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", body)
            atomic_json(evidence / "provider_error.json", {"kind":"stream_error", "body":body[:16384],
                                                           "truncated":len(body)>16384})

    save()
    try:
        while not stream.done:
            if time.monotonic() >= wall_deadline:
                raise StreamFailure("stream_wall_timeout")
            before_read()
            chunk = response.read1(65536)
            if not chunk:
                raise StreamFailure("stream_disconnected_before_terminal_event")
            count += len(chunk)
            if count > MAX_STREAM_BYTES:
                raise StreamFailure("stream_byte_limit")
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if not line:
                    if lines:
                        if sum(map(len, lines)) > MAX_EVENT_BYTES:
                            raise StreamFailure("stream_event_limit")
                        payload = b"\n".join(lines).decode("utf-8")
                        lines = []
                        stream.feed(payload)
                        events += 1
                elif line.startswith(b"data:"):
                    lines.append(line[5:].lstrip(b" "))
                if stream.done:
                    break
            if len(pending) + sum(map(len, lines)) > MAX_EVENT_BYTES:
                raise StreamFailure("stream_event_limit")
            if time.monotonic() - saved >= 2:
                save()
                saved = time.monotonic()
    except TimeoutError as exc:
        failure = "stream_wall_timeout" if time.monotonic() >= wall_deadline else "stream_idle_timeout"
        raise StreamFailure(failure) from exc
    except StreamFailure as exc:
        failure = str(exc)
        raise
    except Exception as exc:
        failure = "invalid_or_interrupted_stream"
        raise StreamFailure(failure) from exc
    finally:
        save()
    return stream.snapshot()


def call_streaming(api: str, body: dict, headers: dict, evidence: Path, key: str,
                   idle_timeout: int, wall_timeout: int) -> dict:
    # Fixed Go origin; redirects are not followed, and credentials never enter logs.
    if api not in {"chat/completions", "messages"}:
        raise ValueError("unsupported Go endpoint")
    end = time.monotonic() + wall_timeout
    conn = http.client.HTTPSConnection("opencode.ai", timeout=min(idle_timeout,wall_timeout))
    path = "/zen/go/v1/" + api
    try:
        conn.connect()
        sock = conn.sock  # Keep the response socket even with Connection: close.
        conn.request("POST", path, json.dumps(body), headers)
        response = conn.getresponse()
        if response.status != 200:
            raw = response.read(16385)
            raise urllib.error.HTTPError("https://opencode.ai"+path, response.status, "Go request rejected", {}, io.BytesIO(raw))
        if "text/event-stream" not in response.getheader("Content-Type", ""):
            raise StreamFailure("provider_did_not_return_event_stream")

        def before_read():
            remaining = end-time.monotonic()
            if remaining <= 0:
                raise StreamFailure("stream_wall_timeout")
            sock.settimeout(min(idle_timeout, remaining))

        return read_stream(response, api, evidence, key, end, before_read)
    finally:
        conn.close()
