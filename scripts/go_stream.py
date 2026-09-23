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

MAX_STREAM_BYTES = 256 * 1024 * 1024  # Includes repeated SSE/JSON framing, not just tokens.
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_CONTENT_BYTES = 16 * 1024 * 1024
MAX_TOOL_CALLS = 16
MAX_TOOL_ARGUMENT_BYTES = 1024 * 1024


class StreamFailure(RuntimeError):
    pass


class ResponseStream:
    """Assemble both Go wire formats. Only explicit protocol termination is complete."""
    def __init__(self, api: str, *, allow_tools: bool = False):
        if api not in {"messages", "chat/completions"}:
            raise ValueError("unsupported streaming API")
        self.api = api
        self.allow_tools = allow_tools
        self.data = {"id":"", "model":"", "usage":{}}
        self.text = []
        self.reasoning = []
        self.reasoning_fields = {"reasoning": [], "reasoning_content": []}
        self.tools = {}
        self.blocks = {}
        self.closed_blocks = set()
        self.finish = ""
        self.done = False
        self.started = False
        self.provider_error = None
        self.content_bytes = 0

    def account_content(self, value: str) -> None:
        if not isinstance(value, str):
            raise StreamFailure("invalid_text_delta")
        self.content_bytes += len(value.encode("utf-8"))
        if self.content_bytes > MAX_CONTENT_BYTES:
            raise StreamFailure("assembled_output_byte_limit")

    def feed(self, payload: str) -> None:
        if self.done:
            raise StreamFailure("data_after_stream_end")
        if payload == "[DONE]":
            if self.api != "chat/completions" or not self.finish:
                raise StreamFailure("invalid_stream_end")
            self.validate_tools()
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
                    if not isinstance(event[key], str) or len(event[key]) > 65536:
                        raise StreamFailure("invalid_stream_metadata")
                    self.data[key] = event[key]
            if event.get("usage"):
                self.data["usage"].update(event["usage"])
                if len(json.dumps(self.data["usage"])) > 65536:
                    raise StreamFailure("invalid_stream_usage")
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise StreamFailure("multiple_stream_choices")
                delta = choice.get("delta", {})
                if delta.get("function_call"):
                    raise StreamFailure("unexpected_tool_call")
                if delta.get("tool_calls"):
                    if not self.allow_tools:
                        raise StreamFailure("unexpected_tool_call")
                    self.chat_tools(delta["tool_calls"])
                for name, target in [("content",self.text), ("reasoning",self.reasoning), ("reasoning_content",self.reasoning)]:
                    value = delta.get(name)
                    if value is not None:
                        self.account_content(value)
                        target.append(value)
                        if name in self.reasoning_fields:
                            self.reasoning_fields[name].append(value)
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
            if len(json.dumps({k:message[k] for k in ("id", "model", "usage") if k in message})) > 65536:
                raise StreamFailure("invalid_stream_metadata")
            self.data.update({k:message[k] for k in ("id", "model", "usage") if k in message})
        elif kind == "content_block_start":
            index = event["index"]
            if type(index) is not int or not 0 <= index < 100 or index in self.blocks:
                raise StreamFailure("invalid_content_index")
            block = event["content_block"]
            allowed = {"text", "thinking", "redacted_thinking"}
            if self.allow_tools:
                allowed.add("tool_use")
            if block.get("type") not in allowed:
                raise StreamFailure("unexpected_content_block")
            initial = block.get("text", block.get("thinking", ""))
            if not isinstance(initial, str):
                raise StreamFailure("invalid_text_delta")
            self.account_content(json.dumps(block))
            self.blocks[index] = {**block, "pieces":[initial]}
            if block["type"] == "tool_use":
                if sum(b["type"] == "tool_use" for b in self.blocks.values()) > MAX_TOOL_CALLS:
                    raise StreamFailure("too_many_tool_calls")
                self.tool_identifier(block.get("id"))
                self.tool_identifier(block.get("name"))
                if not isinstance(block.get("input"), dict):
                    raise StreamFailure("invalid_tool_input")
                if len(json.dumps(block["input"]).encode()) > MAX_TOOL_ARGUMENT_BYTES:
                    raise StreamFailure("tool_argument_byte_limit")
                self.blocks[index]["argument_pieces"] = []
                self.blocks[index]["argument_bytes"] = 0
        elif kind == "content_block_delta":
            if event["index"] not in self.blocks or event["index"] in self.closed_blocks:
                raise StreamFailure("invalid_content_index")
            block = self.blocks[event["index"]]
            delta = event["delta"]
            if delta["type"] in {"text_delta", "thinking_delta"}:
                field = "text" if delta["type"] == "text_delta" else "thinking"
                value = delta[field]
                if not isinstance(value, str) or block["type"] != ("text" if field == "text" else "thinking"):
                    raise StreamFailure("invalid_content_delta")
                self.account_content(value)
                block["pieces"].append(value)
            elif delta["type"] == "signature_delta":
                if block["type"] != "thinking":
                    raise StreamFailure("invalid_signature_delta")
                self.account_content(delta.get("signature", ""))
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif delta["type"] == "input_json_delta":
                if not self.allow_tools or block["type"] != "tool_use":
                    raise StreamFailure("unexpected_tool_call")
                value = delta.get("partial_json")
                self.account_content(value)
                block["argument_bytes"] += len(value.encode())
                if block["argument_bytes"] > MAX_TOOL_ARGUMENT_BYTES:
                    raise StreamFailure("tool_argument_byte_limit")
                if block["input"]:
                    raise StreamFailure("conflicting_tool_input")
                block["argument_pieces"].append(value)
            elif self.allow_tools:
                raise StreamFailure("unknown_content_delta")
        elif kind == "content_block_stop":
            index = event["index"]
            if index not in self.blocks or index in self.closed_blocks:
                raise StreamFailure("invalid_content_index")
            block = self.blocks[index]
            if block["type"] == "tool_use" and "".join(block["argument_pieces"]):
                try:
                    value = json.loads("".join(block["argument_pieces"]))
                except (ValueError, TypeError):
                    # The provider reports max_tokens only in message_delta,
                    # after block_stop. Retain malformed partial arguments
                    # until that terminal reason distinguishes truncation from
                    # a malformed supposedly-complete tool call.
                    block["argument_error"] = "invalid_tool_arguments_json"
                else:
                    if not isinstance(value, dict):
                        block["argument_error"] = "invalid_tool_input"
                    else:
                        block["input"] = value
            self.closed_blocks.add(index)
        elif kind == "message_delta":
            self.finish = event.get("delta", {}).get("stop_reason") or self.finish
            self.data["usage"].update(event.get("usage", {}))  # Cumulative, never sum deltas.
            if len(json.dumps(self.data["usage"])) > 65536:
                raise StreamFailure("invalid_stream_usage")
        elif kind == "message_stop":
            if not self.started or not self.finish:
                raise StreamFailure("invalid_stream_end")
            self.validate_tools()
            self.done = True

    @staticmethod
    def tool_identifier(value):
        if not isinstance(value, str) or not value or len(value.encode()) > 256:
            raise StreamFailure("invalid_tool_identifier")

    def chat_tools(self, calls):
        if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
            raise StreamFailure("invalid_tool_calls")
        for delta in calls:
            if not isinstance(delta, dict):
                raise StreamFailure("invalid_tool_call_delta")
            index = delta.get("index")
            if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                raise StreamFailure("invalid_tool_index")
            if delta.get("type") not in (None, "function"):
                raise StreamFailure("invalid_tool_type")
            tool = self.tools.setdefault(index, {"id": "", "type": "function",
                                               "function": {"name": "", "arguments": ""}})
            function = delta.get("function", {})
            if not isinstance(function, dict):
                raise StreamFailure("invalid_tool_function")
            for owner, field, value, limit in (
                (tool, "id", delta.get("id"), 256),
                (tool["function"], "name", function.get("name"), 256),
                (tool["function"], "arguments", function.get("arguments"), MAX_TOOL_ARGUMENT_BYTES),
            ):
                if value is not None:
                    self.account_content(value)
                    # Metadata may be repeated by compatible gateways; unlike
                    # argument text it does not represent appended bytes.
                    if field in {"id", "name"} and owner[field] == value:
                        continue
                    if len(owner[field].encode()) + len(value.encode()) > limit:
                        raise StreamFailure("tool_argument_byte_limit" if field == "arguments" else "invalid_tool_identifier")
                    owner[field] += value

    def validate_tools(self):
        if not self.allow_tools:
            return
        if self.finish in {"length", "max_tokens"}:
            # Explicit provider termination is transport completion, not a
            # usable tool turn. Preserve usage and partial arguments; the
            # controller rejects cap finishes before parsing/executing calls.
            return
        if self.api == "chat/completions":
            calls = list(self.tools.values())
            if calls and self.finish != "tool_calls":
                # A length-limited tool call is not complete and must not execute.
                raise StreamFailure("incomplete_tool_turn")
            if self.finish == "tool_calls" and not calls:
                raise StreamFailure("missing_tool_calls")
            ids = []
            for call in calls:
                self.tool_identifier(call["id"])
                self.tool_identifier(call["function"]["name"])
                ids.append(call["id"])
        else:
            calls = [b for b in self.blocks.values() if b["type"] == "tool_use"]
            for block in calls:
                if block.get("argument_error"):
                    raise StreamFailure(block["argument_error"])
            if self.blocks.keys() != self.closed_blocks:
                raise StreamFailure("unclosed_content_blocks")
            if calls and self.finish != "tool_use":
                raise StreamFailure("incomplete_tool_turn")
            if self.finish == "tool_use" and not calls:
                raise StreamFailure("missing_tool_calls")
            ids = [b["id"] for b in calls]
        if len(set(ids)) != len(ids):
            raise StreamFailure("duplicate_tool_call_id")

    def snapshot(self) -> dict:
        data = dict(self.data)
        if self.api == "chat/completions":
            message = {"role":"assistant", "content":"".join(self.text)}
            if self.allow_tools:
                message.update({k:"".join(v) for k,v in self.reasoning_fields.items() if v})
                if self.tools:
                    message["tool_calls"] = [deepcopy_tool(t) for _,t in sorted(self.tools.items())]
            else:
                message["reasoning"] = "".join(self.reasoning)
            data["choices"] = [{"index":0, "message":message, "finish_reason":self.finish}]
        else:
            data["content"] = []
            for index, block in sorted(self.blocks.items()):
                value = {k:v for k,v in block.items() if k not in {"pieces", "argument_pieces", "argument_bytes", "argument_error"}}
                if block["type"] in {"text", "thinking"}:
                    value["text" if block["type"] == "text" else "thinking"] = "".join(block["pieces"])
                if block["type"] == "tool_use" and (index not in self.closed_blocks or block.get("argument_error")):
                    value["partial_json"] = "".join(block.get("argument_pieces", []))
                data["content"].append(value)
            data["stop_reason"] = self.finish
        return data


def deepcopy_tool(tool: dict) -> dict:
    return {**tool, "function": dict(tool["function"])}


def read_stream(response, api: str, evidence: Path, key: str, wall_deadline: float,
                before_read=lambda: None, *, allow_tools: bool = False) -> dict:
    stream = ResponseStream(api, allow_tools=allow_tools)
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

        return read_stream(response, api, evidence, key, end, before_read,
                           allow_tools=bool(body.get("tools")))
    finally:
        conn.close()
