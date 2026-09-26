"""Single-request OpenCode Go streaming (SSE) transport. Never reconnects.

OpenCode Go speaks three wire formats:

  chat/completions   OpenAI-style: `data: {"choices": [{"delta": ...}]}` events,
                     ended by `data: [DONE]`
  messages           Anthropic-style: typed events (message_start,
                     content_block_start/delta/stop, message_delta, message_stop)
  responses          OpenAI Responses: typed events (response.created,
                     response.output_item.added/done, *.delta), ended by
                     response.completed or response.incomplete

`ResponseStream` assembles each format into the same shape a non-streaming
response would have. A response only counts as complete after an explicit,
well-formed end-of-stream; anything else raises StreamFailure. Partial output
is saved to disk every couple of seconds (and on failure) as evidence.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import time
import urllib.error
from pathlib import Path

from adpbench.durable import atomic_json

GO_HOST = "opencode.ai"
GO_PATH = "/zen/go/v1/"
APIS = {"messages", "chat/completions", "responses"}

MAX_STREAM_BYTES = 256 * 1024 * 1024  # includes repeated SSE/JSON framing, not just tokens
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_CONTENT_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 65536
MAX_TOOL_CALLS = 16
MAX_TOOL_ARGUMENT_BYTES = 1024 * 1024
MAX_IDENTIFIER_BYTES = 256
MAX_CONTENT_BLOCKS = 100
ERROR_BODY_LIMIT = 16384
SAVE_INTERVAL_S = 2

CAP_FINISHES = {"length", "max_tokens"}
PRIVATE_BLOCK_KEYS = {"pieces", "argument_pieces", "argument_bytes", "argument_error"}

# Responses output items, and the finish reason each completed response maps to.
RESPONSE_ITEM_TYPES = {"message", "reasoning", "function_call"}
RESPONSE_CAP_REASONS = {"max_output_tokens"}


class StreamFailure(RuntimeError):
    """The stream ended without a complete, valid response. The message is a short reason code."""


class ResponseStream:
    """Assembles streamed events into a complete response.

    `allow_tools` permits native tool calls (the agent track). Without it, any
    tool call in the stream is a failure (the single-shot track).
    """

    def __init__(self, api: str, *, allow_tools: bool = False):
        if api not in APIS:
            raise ValueError("unsupported streaming API")
        self.api = api
        self.allow_tools = allow_tools

        self.data = {"id": "", "model": "", "usage": {}}  # response metadata
        self.finish = ""  # the provider's finish/stop reason
        self.started = False
        self.done = False
        self.provider_error = None  # the error event, if the provider sent one
        self.content_bytes = 0

        # chat/completions state
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.reasoning_fields: dict[str, list[str]] = {"reasoning": [], "reasoning_content": []}
        self.tools: dict[int, dict] = {}  # tool call index -> {"id", "type", "function"}

        # messages state
        self.blocks: dict[int, dict] = {}  # content block index -> block being assembled
        self.closed_blocks: set[int] = set()

        # responses state
        self.items: dict[int, dict] = {}  # output index -> {"item", "pieces", "done"}
        self.output: list[dict] | None = None  # the final output from the terminal event

    # -- entry point --------------------------------------------------------

    def feed(self, payload: str) -> None:
        """Process the data of one SSE event."""
        if self.done:
            raise StreamFailure("data_after_stream_end")

        if payload == "[DONE]":
            if self.api != "chat/completions" or not self.finish:
                raise StreamFailure("invalid_stream_end")
            self._check_tool_calls()
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
            self._feed_chat_event(event)
        elif self.api == "messages":
            self._feed_messages_event(event)
        else:
            self._feed_responses_event(event)

    def _add_content(self, value: str) -> None:
        """Count assembled output toward the content limit."""
        if not isinstance(value, str):
            raise StreamFailure("invalid_text_delta")
        self.content_bytes += len(value.encode("utf-8"))
        if self.content_bytes > MAX_CONTENT_BYTES:
            raise StreamFailure("assembled_output_byte_limit")

    def _check_usage_size(self) -> None:
        if len(json.dumps(self.data["usage"])) > MAX_METADATA_BYTES:
            raise StreamFailure("invalid_stream_usage")

    # -- chat/completions ---------------------------------------------------

    def _feed_chat_event(self, event: dict) -> None:
        self.started = True
        for key in ("id", "model"):
            if event.get(key):
                if not isinstance(event[key], str) or len(event[key]) > MAX_METADATA_BYTES:
                    raise StreamFailure("invalid_stream_metadata")
                self.data[key] = event[key]
        if event.get("usage"):
            self.data["usage"].update(event["usage"])
            self._check_usage_size()

        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                raise StreamFailure("multiple_stream_choices")
            delta = choice.get("delta", {})
            if delta.get("function_call"):
                raise StreamFailure("unexpected_tool_call")
            if delta.get("tool_calls"):
                if not self.allow_tools:
                    raise StreamFailure("unexpected_tool_call")
                self._add_chat_tool_deltas(delta["tool_calls"])

            for field in ("content", "reasoning", "reasoning_content"):
                value = delta.get(field)
                if value is None:
                    continue
                self._add_content(value)
                if field == "content":
                    self.text.append(value)
                else:
                    self.reasoning.append(value)
                    self.reasoning_fields[field].append(value)

            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def _add_chat_tool_deltas(self, deltas) -> None:
        """Append streamed pieces of tool calls. Each piece names its call by index."""
        if not isinstance(deltas, list) or len(deltas) > MAX_TOOL_CALLS:
            raise StreamFailure("invalid_tool_calls")
        for delta in deltas:
            if not isinstance(delta, dict):
                raise StreamFailure("invalid_tool_call_delta")
            index = delta.get("index")
            if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                raise StreamFailure("invalid_tool_index")
            if delta.get("type") not in (None, "function"):
                raise StreamFailure("invalid_tool_type")
            function = delta.get("function", {})
            if not isinstance(function, dict):
                raise StreamFailure("invalid_tool_function")

            if index not in self.tools:
                self.tools[index] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            tool = self.tools[index]
            self._append_tool_field(tool, "id", delta.get("id"), MAX_IDENTIFIER_BYTES)
            self._append_tool_field(tool["function"], "name", function.get("name"), MAX_IDENTIFIER_BYTES)
            self._append_tool_field(
                tool["function"], "arguments", function.get("arguments"), MAX_TOOL_ARGUMENT_BYTES
            )

    def _append_tool_field(self, owner: dict, field: str, value, limit: int) -> None:
        if value is None:
            return
        self._add_content(value)
        # Some compatible gateways repeat the id and name on every piece. Unlike
        # argument text, a repeat is not new data.
        if field in ("id", "name") and owner[field] == value:
            return
        if len(owner[field].encode()) + len(value.encode()) > limit:
            if field == "arguments":
                raise StreamFailure("tool_argument_byte_limit")
            raise StreamFailure("invalid_tool_identifier")
        owner[field] += value

    # -- messages -----------------------------------------------------------

    def _feed_messages_event(self, event: dict) -> None:
        kind = event.get("type")
        needs_start = {"content_block_start", "content_block_delta", "message_delta", "message_stop"}
        if not self.started and kind in needs_start:
            raise StreamFailure("missing_message_start")

        if kind == "message_start":
            self._on_message_start(event)
        elif kind == "content_block_start":
            self._on_block_start(event)
        elif kind == "content_block_delta":
            self._on_block_delta(event)
        elif kind == "content_block_stop":
            self._on_block_stop(event)
        elif kind == "message_delta":
            self.finish = event.get("delta", {}).get("stop_reason") or self.finish
            # Usage in message_delta is cumulative: replace, never sum.
            self.data["usage"].update(event.get("usage", {}))
            self._check_usage_size()
        elif kind == "message_stop":
            if not self.started or not self.finish:
                raise StreamFailure("invalid_stream_end")
            self._check_tool_calls()
            self.done = True
        # Other event types (such as "ping") carry nothing to assemble.

    def _on_message_start(self, event: dict) -> None:
        if self.started:
            raise StreamFailure("duplicate_message_start")
        self.started = True
        message = event["message"]
        metadata = {key: message[key] for key in ("id", "model", "usage") if key in message}
        if len(json.dumps(metadata)) > MAX_METADATA_BYTES:
            raise StreamFailure("invalid_stream_metadata")
        self.data.update(metadata)

    def _on_block_start(self, event: dict) -> None:
        index = event["index"]
        if type(index) is not int or not 0 <= index < MAX_CONTENT_BLOCKS or index in self.blocks:
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
        self._add_content(json.dumps(block))
        self.blocks[index] = {**block, "pieces": [initial]}

        if block["type"] == "tool_use":
            tool_blocks = [b for b in self.blocks.values() if b["type"] == "tool_use"]
            if len(tool_blocks) > MAX_TOOL_CALLS:
                raise StreamFailure("too_many_tool_calls")
            _check_identifier(block.get("id"))
            _check_identifier(block.get("name"))
            if not isinstance(block.get("input"), dict):
                raise StreamFailure("invalid_tool_input")
            if len(json.dumps(block["input"]).encode()) > MAX_TOOL_ARGUMENT_BYTES:
                raise StreamFailure("tool_argument_byte_limit")
            self.blocks[index]["argument_pieces"] = []
            self.blocks[index]["argument_bytes"] = 0

    def _on_block_delta(self, event: dict) -> None:
        index = event["index"]
        if index not in self.blocks or index in self.closed_blocks:
            raise StreamFailure("invalid_content_index")
        block = self.blocks[index]
        delta = event["delta"]
        kind = delta["type"]

        if kind in ("text_delta", "thinking_delta"):
            field = "text" if kind == "text_delta" else "thinking"
            value = delta[field]
            # A text delta must go to a text block, a thinking delta to a thinking block.
            if not isinstance(value, str) or block["type"] != field:
                raise StreamFailure("invalid_content_delta")
            self._add_content(value)
            block["pieces"].append(value)
        elif kind == "signature_delta":
            if block["type"] != "thinking":
                raise StreamFailure("invalid_signature_delta")
            signature = delta.get("signature", "")
            self._add_content(signature)
            block["signature"] = block.get("signature", "") + signature
        elif kind == "input_json_delta":
            if not self.allow_tools or block["type"] != "tool_use":
                raise StreamFailure("unexpected_tool_call")
            value = delta.get("partial_json")
            self._add_content(value)
            block["argument_bytes"] += len(value.encode())
            if block["argument_bytes"] > MAX_TOOL_ARGUMENT_BYTES:
                raise StreamFailure("tool_argument_byte_limit")
            if block["input"]:
                raise StreamFailure("conflicting_tool_input")
            block["argument_pieces"].append(value)
        elif self.allow_tools:
            raise StreamFailure("unknown_content_delta")

    def _on_block_stop(self, event: dict) -> None:
        index = event["index"]
        if index not in self.blocks or index in self.closed_blocks:
            raise StreamFailure("invalid_content_index")
        block = self.blocks[index]
        arguments = "".join(block.get("argument_pieces", []))
        if block["type"] == "tool_use" and arguments:
            # The provider only reports max_tokens in message_delta, after this
            # event. So keep malformed arguments as an error for now; the
            # finish reason later decides whether it was truncation (allowed)
            # or a broken, supposedly complete tool call (a failure).
            try:
                value = json.loads(arguments)
            except (ValueError, TypeError):
                block["argument_error"] = "invalid_tool_arguments_json"
            else:
                if isinstance(value, dict):
                    block["input"] = value
                else:
                    block["argument_error"] = "invalid_tool_input"
        self.closed_blocks.add(index)

    # -- responses ----------------------------------------------------------

    def _feed_responses_event(self, event: dict) -> None:
        kind = event.get("type")
        if not isinstance(kind, str):
            raise StreamFailure("invalid_stream_event")
        if kind == "response.created":
            if self.started:
                raise StreamFailure("duplicate_response_start")
            self.started = True
            self._set_response_metadata(event.get("response"))
            return
        if not self.started and kind.startswith("response."):
            raise StreamFailure("missing_response_start")

        if kind == "response.output_item.added":
            self._on_item_added(event)
        elif kind in ("response.output_text.delta", "response.function_call_arguments.delta"):
            self._on_item_delta(event, "arguments" if "function_call" in kind else "text")
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            self._on_item_delta(event, "reasoning")
        elif kind == "response.output_item.done":
            self._on_item_done(event)
        elif kind in ("response.completed", "response.incomplete"):
            self._on_response_end(event)
        elif kind == "response.failed":
            self.provider_error = event
            raise StreamFailure("provider_stream_error")
        # Other events (in_progress, *.done text parts, ...) repeat assembled data.

    def _set_response_metadata(self, response) -> None:
        if not isinstance(response, dict):
            raise StreamFailure("invalid_stream_metadata")
        for key in ("id", "model"):
            value = response.get(key)
            if value:
                if not isinstance(value, str) or len(value) > MAX_METADATA_BYTES:
                    raise StreamFailure("invalid_stream_metadata")
                self.data[key] = value

    def _response_index(self, event: dict) -> int:
        index = event.get("output_index")
        if type(index) is not int or not 0 <= index < MAX_CONTENT_BLOCKS:
            raise StreamFailure("invalid_content_index")
        return index

    def _check_response_item(self, item) -> None:
        if not isinstance(item, dict) or item.get("type") not in RESPONSE_ITEM_TYPES:
            raise StreamFailure("unexpected_content_block")
        if item["type"] == "function_call" and not self.allow_tools:
            raise StreamFailure("unexpected_tool_call")

    def _on_item_added(self, event: dict) -> None:
        index = self._response_index(event)
        if index in self.items:
            raise StreamFailure("invalid_content_index")
        item = event.get("item")
        self._check_response_item(item)
        self._add_content(json.dumps(item))
        self.items[index] = {"item": item, "pieces": [], "bytes": 0, "done": False}
        tool_items = [entry for entry in self.items.values() if entry["item"]["type"] == "function_call"]
        if len(tool_items) > MAX_TOOL_CALLS:
            raise StreamFailure("too_many_tool_calls")

    def _on_item_delta(self, event: dict, kind: str) -> None:
        index = self._response_index(event)
        entry = self.items.get(index)
        if entry is None or entry["done"]:
            raise StreamFailure("invalid_content_index")
        expected = {"text": "message", "arguments": "function_call", "reasoning": "reasoning"}[kind]
        if entry["item"]["type"] != expected:
            raise StreamFailure("invalid_content_delta")
        value = event.get("delta")
        self._add_content(value)
        entry["pieces"].append(value)
        entry["bytes"] += len(value.encode())
        if kind == "arguments" and entry["bytes"] > MAX_TOOL_ARGUMENT_BYTES:
            raise StreamFailure("tool_argument_byte_limit")

    def _on_item_done(self, event: dict) -> None:
        index = self._response_index(event)
        entry = self.items.get(index)
        if entry is None or entry["done"]:
            raise StreamFailure("invalid_content_index")
        item = event.get("item")
        self._check_response_item(item)
        if item["type"] != entry["item"]["type"]:
            raise StreamFailure("invalid_content_delta")
        entry.update(item=item, done=True)

    def _on_response_end(self, event: dict) -> None:
        response = event.get("response")
        self._set_response_metadata(response)
        usage = response.get("usage")
        if isinstance(usage, dict):
            self.data["usage"] = dict(usage)
            self._check_usage_size()

        incomplete = event["type"] == "response.incomplete"
        output = response.get("output")
        if isinstance(output, list) and output:
            for item in output:
                self._check_response_item(item)
            self.output = output
        elif incomplete:
            self.output = self._partial_output()
        else:
            # Some gateways leave the terminal output empty; the done items are the output.
            if any(not entry["done"] for entry in self.items.values()):
                raise StreamFailure("unclosed_content_blocks")
            self.output = [entry["item"] for _, entry in sorted(self.items.items())]

        if incomplete:
            reason = (response.get("incomplete_details") or {}).get("reason")
            # A capped response is a complete transport, but not a usable turn.
            self.finish = "length" if reason in RESPONSE_CAP_REASONS else f"incomplete:{reason}"
        else:
            calls = [item for item in self.output if item["type"] == "function_call"]
            self.finish = "tool_calls" if calls else "stop"
            self._check_tool_calls()
        self.done = True

    # -- completion ---------------------------------------------------------

    def _check_tool_calls(self) -> None:
        """At end of stream: tool calls must be complete and match the finish reason."""
        if not self.allow_tools:
            return
        if self.finish in CAP_FINISHES:
            # Hitting the output cap is a complete transport, but not a usable
            # tool turn. Usage and partial arguments are kept; the controller
            # rejects capped turns before executing anything.
            return

        if self.api == "chat/completions":
            calls = list(self.tools.values())
            if calls and self.finish != "tool_calls":
                raise StreamFailure("incomplete_tool_turn")
            if self.finish == "tool_calls" and not calls:
                raise StreamFailure("missing_tool_calls")
            for call in calls:
                _check_identifier(call["id"])
                _check_identifier(call["function"]["name"])
            ids = [call["id"] for call in calls]
        elif self.api == "responses":
            calls = [item for item in self.output or [] if item["type"] == "function_call"]
            if len(calls) > MAX_TOOL_CALLS:
                raise StreamFailure("too_many_tool_calls")
            for call in calls:
                _check_identifier(call.get("call_id"))
                _check_identifier(call.get("name"))
                arguments = call.get("arguments")
                if not isinstance(arguments, str) or len(arguments.encode()) > MAX_TOOL_ARGUMENT_BYTES:
                    raise StreamFailure("tool_argument_byte_limit")
            ids = [call["call_id"] for call in calls]
        else:
            calls = [block for block in self.blocks.values() if block["type"] == "tool_use"]
            for block in calls:
                if block.get("argument_error"):
                    raise StreamFailure(block["argument_error"])
            if self.blocks.keys() != self.closed_blocks:
                raise StreamFailure("unclosed_content_blocks")
            if calls and self.finish != "tool_use":
                raise StreamFailure("incomplete_tool_turn")
            if self.finish == "tool_use" and not calls:
                raise StreamFailure("missing_tool_calls")
            ids = [block["id"] for block in calls]

        if len(set(ids)) != len(ids):
            raise StreamFailure("duplicate_tool_call_id")

    def snapshot(self) -> dict:
        """The response assembled so far, shaped like a non-streaming response."""
        data = dict(self.data)
        if self.api == "chat/completions":
            message = {"role": "assistant", "content": "".join(self.text)}
            if self.allow_tools:
                for field, pieces in self.reasoning_fields.items():
                    if pieces:
                        message[field] = "".join(pieces)
                if self.tools:
                    message["tool_calls"] = [
                        {**tool, "function": dict(tool["function"])}
                        for _, tool in sorted(self.tools.items())
                    ]
            else:
                message["reasoning"] = "".join(self.reasoning)
            data["choices"] = [{"index": 0, "message": message, "finish_reason": self.finish}]
            return data
        if self.api == "responses":
            data["output"] = self.output if self.output is not None else self._partial_output()
            data["stop_reason"] = self.finish
            return data

        data["content"] = []
        for index, block in sorted(self.blocks.items()):
            value = {key: item for key, item in block.items() if key not in PRIVATE_BLOCK_KEYS}
            if block["type"] in ("text", "thinking"):
                value[block["type"]] = "".join(block["pieces"])
            unfinished = index not in self.closed_blocks or block.get("argument_error")
            if block["type"] == "tool_use" and unfinished:
                value["partial_json"] = "".join(block.get("argument_pieces", []))
            data["content"].append(value)
        data["stop_reason"] = self.finish
        return data


    def _partial_output(self) -> list[dict]:
        """Responses output items assembled so far, for evidence of an unfinished stream."""
        output = []
        for _, entry in sorted(self.items.items()):
            item = dict(entry["item"])
            if not entry["done"]:
                text = "".join(entry["pieces"])
                if item["type"] == "message":
                    item["content"] = [{"type": "output_text", "text": text}]
                elif item["type"] == "function_call":
                    item["arguments"] = text
                else:
                    item["summary"] = [{"type": "summary_text", "text": text}]
            output.append(item)
        return output


def _check_identifier(value) -> None:
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_IDENTIFIER_BYTES:
        raise StreamFailure("invalid_tool_identifier")


# --------------------------------------------------------------------------
# Reading a response
# --------------------------------------------------------------------------


def read_stream(
    response,
    api: str,
    evidence: Path,
    key: str,
    wall_deadline: float,
    before_read=None,
    *,
    allow_tools: bool = False,
) -> dict:
    """Read an SSE response to its end-of-stream event and return the assembled response.

    `response` needs a `read1(size)` method. `before_read`, if given, is
    called before each read (the transport uses it to set socket timeouts).
    Progress is saved under `evidence`:

        response.partial.json   the assembled response so far (key redacted)
        stream-status.json      completed/incomplete, failure reason, byte counts
        provider_error.json     the provider's error event, if it sent one
    """
    stream = ResponseStream(api, allow_tools=allow_tools)
    progress = {"started": time.monotonic(), "bytes": 0, "events": 0, "failure": ""}
    _save_progress(stream, progress, evidence, key)
    last_saved = 0.0
    pending = b""  # bytes after the last complete line
    event_lines: list[bytes] = []  # `data:` lines of the event being read

    try:
        while not stream.done:
            if time.monotonic() >= wall_deadline:
                raise StreamFailure("stream_wall_timeout")
            if before_read:
                before_read()
            chunk = response.read1(65536)
            if not chunk:
                raise StreamFailure("stream_disconnected_before_terminal_event")
            progress["bytes"] += len(chunk)
            if progress["bytes"] > MAX_STREAM_BYTES:
                raise StreamFailure("stream_byte_limit")

            pending += chunk
            while b"\n" in pending and not stream.done:
                line, pending = pending.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line.startswith(b"data:"):
                    event_lines.append(line[5:].lstrip(b" "))
                elif not line and event_lines:
                    # A blank line ends an event.
                    if sum(len(part) for part in event_lines) > MAX_EVENT_BYTES:
                        raise StreamFailure("stream_event_limit")
                    payload = b"\n".join(event_lines).decode("utf-8")
                    event_lines = []
                    stream.feed(payload)
                    progress["events"] += 1

            if len(pending) + sum(len(part) for part in event_lines) > MAX_EVENT_BYTES:
                raise StreamFailure("stream_event_limit")
            if time.monotonic() - last_saved >= SAVE_INTERVAL_S:
                _save_progress(stream, progress, evidence, key)
                last_saved = time.monotonic()
    except TimeoutError as exc:
        timed_out = time.monotonic() >= wall_deadline
        progress["failure"] = "stream_wall_timeout" if timed_out else "stream_idle_timeout"
        raise StreamFailure(progress["failure"]) from exc
    except StreamFailure as exc:
        progress["failure"] = str(exc)
        raise
    except Exception as exc:
        progress["failure"] = "invalid_or_interrupted_stream"
        raise StreamFailure(progress["failure"]) from exc
    finally:
        _save_progress(stream, progress, evidence, key)
    return stream.snapshot()


def _save_progress(stream: ResponseStream, progress: dict, evidence: Path, key: str) -> None:
    # Save the assembled content rather than raw delta fragments: a fragment
    # boundary could split the credential and defeat redaction.
    snapshot = stream.snapshot()
    if key:
        snapshot = json.loads(json.dumps(snapshot).replace(key, "[REDACTED]"))
    atomic_json(evidence / "response.partial.json", snapshot)
    atomic_json(
        evidence / "stream-status.json",
        {
            "state": "completed" if stream.done else "incomplete",
            "reason": progress["failure"],
            "bytes_received": progress["bytes"],
            "events": progress["events"],
            "duration_s": round(time.monotonic() - progress["started"], 3),
        },
    )
    if stream.provider_error is not None:
        body = json.dumps(stream.provider_error)
        if key:
            body = body.replace(key, "[REDACTED]")
        body = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", body)
        atomic_json(
            evidence / "provider_error.json",
            {
                "kind": "stream_error",
                "body": body[:ERROR_BODY_LIMIT],
                "truncated": len(body) > ERROR_BODY_LIMIT,
            },
        )


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def call_streaming(
    api: str,
    body: dict,
    headers: dict,
    evidence: Path,
    key: str,
    idle_timeout: int,
    wall_timeout: int,
) -> dict:
    """POST one streaming request to the fixed Go origin and read the full response.

    Redirects are not followed, the request is never retried, and credentials
    never enter logs. A non-200 status raises urllib.error.HTTPError.
    """
    if api not in APIS:
        raise ValueError("unsupported Go endpoint")
    end = time.monotonic() + wall_timeout
    path = GO_PATH + api
    connection = http.client.HTTPSConnection(GO_HOST, timeout=min(idle_timeout, wall_timeout))
    try:
        connection.connect()
        sock = connection.sock  # keep the response socket even with Connection: close
        connection.request("POST", path, json.dumps(body), headers)
        response = connection.getresponse()
        if response.status != 200:
            raw = response.read(ERROR_BODY_LIMIT + 1)
            raise urllib.error.HTTPError(
                f"https://{GO_HOST}{path}", response.status, "Go request rejected", {}, io.BytesIO(raw)
            )
        if "text/event-stream" not in response.getheader("Content-Type", ""):
            raise StreamFailure("provider_did_not_return_event_stream")

        def set_read_timeout() -> None:
            # Wait at most `idle_timeout` for the next bytes, and never past the wall deadline.
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise StreamFailure("stream_wall_timeout")
            sock.settimeout(min(idle_timeout, remaining))

        allow_tools = bool(body.get("tools"))
        return read_stream(response, api, evidence, key, end, set_read_timeout, allow_tools=allow_tools)
    finally:
        connection.close()
