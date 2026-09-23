"""Four allowlisted agent actions over native Go-compatible tool-call APIs.

This module translates data only. It never opens files, executes commands, or
decides whether a submitted design passes. The runner owns those boundaries.
"""
from __future__ import annotations

from copy import deepcopy
import json

from scripts.go_pilot import output_limit

MAX_TOOL_CALLS = 16
MAX_ARGUMENT_BYTES = 1024 * 1024
MAX_TOOL_RESULT_BYTES = 1024 * 1024


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TOOLS = (
    {"name": "read_file", "description": "Read a task workspace file: PROBLEM.md, dut.py, or dut.v.",
     "input_schema": _schema({"path": {"type": "string"}})},
    {"name": "write_file", "description": "Replace the entire dut.v submission file with complete synthesizable SystemVerilog. No other path is writable.",
     "input_schema": _schema({"path": {"type": "string"}, "content": {"type": "string"}})},
    {"name": "check", "description": "Audit, synthesize and simulate the current dut.v using public development cases. Returns feedback; does not submit or reveal held-out tests.",
     "input_schema": _schema({})},
    {"name": "submit", "description": "Finalize and freeze the current dut.v as your submission. Call after writing it and reviewing development-check feedback. No more editing after successful submission.",
     "input_schema": _schema({})},
)
TOOL_SCHEMAS = {tool["name"]: tool["input_schema"] for tool in TOOLS}


def _api(api: str) -> None:
    if api not in {"chat/completions", "messages"}:
        raise ValueError("unsupported Go tool API")


def request_body(model: dict, plan: dict, system: str, messages: list[dict]) -> dict:
    """Build the same four operations using each provider's native wire schema."""
    api = model["api"]
    _api(api)
    body = {"model": model["id"], "messages": deepcopy(messages), "stream": True,
            model.get("token_limit_key", "max_tokens"): output_limit(plan, model)}
    for key in ("thinking", "reasoning_effort", "reasoning"):
        if key in model:
            body[key] = deepcopy(model[key])
    if api == "messages":
        body["system"] = system
        body["tools"] = deepcopy(list(TOOLS))
        body["tool_choice"] = {"type": "auto"}
    else:
        body["messages"].insert(0, {"role": "system", "content": system})
        body["tools"] = [{"type": "function", "function": {
            "name": tool["name"], "description": tool["description"],
            "parameters": deepcopy(tool["input_schema"])}} for tool in TOOLS]
        body["tool_choice"] = "auto"
        body["stream_options"] = {"include_usage": True}
    return body


def _identifier(value, what: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 256:
        raise ValueError("invalid tool " + what)
    return value


def _arguments(name: str, value) -> tuple[dict | None, str]:
    """Invalid actions become tool feedback, never executable instructions."""
    if isinstance(value, str):
        if len(value.encode()) > MAX_ARGUMENT_BYTES:
            return None, "Tool arguments exceed the byte limit."
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None, "Tool arguments must be a complete JSON object."
    if not isinstance(value, dict):
        return None, "Tool arguments must be a JSON object."
    if len(json.dumps(value).encode()) > MAX_ARGUMENT_BYTES:
        return None, "Tool arguments exceed the byte limit."
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return None, "Unknown tool. Use read_file, write_file, check, or submit."
    if set(value) != set(schema["properties"]):
        return None, "Tool arguments must contain exactly: " + ", ".join(schema["properties"])
    if any(not isinstance(v, str) for v in value.values()):
        return None, "All tool argument values must be strings."
    if name == "read_file" and value["path"] not in {"PROBLEM.md", "dut.py", "dut.v"}:
        return None, "Only PROBLEM.md, dut.py and dut.v may be read."
    if name == "write_file" and value["path"] != "dut.v":
        return None, "Only dut.v may be written."
    return value, ""


def parse_turn(data: dict, api: str) -> tuple[dict, list[dict], str, str, dict]:
    """Return native assistant history plus validated, normalized tool calls.

    A call with an ``error`` has ``arguments=None`` and MUST NOT be executed.
    The caller returns that error as the corresponding tool result. Native
    thinking and signature fields remain unchanged for the next model turn.
    """
    _api(api)
    if api == "messages":
        blocks = data.get("content", [])
        if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
            raise ValueError("invalid native content blocks")
        if any(b.get("type") not in {"text", "thinking", "redacted_thinking", "tool_use"}
               or "partial_json" in b for b in blocks):
            raise ValueError("unsupported or incomplete native content block")
        assistant = {"role": "assistant", "content": deepcopy(blocks)}
        native = [b for b in blocks if b.get("type") == "tool_use"]
        text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
        finish = data.get("stop_reason", "")
        entries = [(b.get("id"), b.get("name"), b.get("input")) for b in native]
    else:
        choices = data.get("choices", [])
        if len(choices) != 1:
            raise ValueError("expected one assistant choice")
        assistant = deepcopy(choices[0]["message"])
        assistant["role"] = "assistant"
        if assistant.get("function_call"):
            raise ValueError("legacy function_call is unsupported")
        native = assistant.get("tool_calls") or []
        text = assistant.get("content") or ""
        finish = choices[0].get("finish_reason", "")
        entries = []
        for call in native:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("invalid native tool call")
            function = call.get("function", {})
            entries.append((call.get("id"), function.get("name"), function.get("arguments")))
    if not isinstance(text, str) or not isinstance(finish, str):
        raise ValueError("invalid assistant text or finish reason")
    if len(entries) > MAX_TOOL_CALLS:
        raise ValueError("too many tool calls in one turn")
    expected_finish = "tool_use" if api == "messages" else "tool_calls"
    if entries and finish != expected_finish:
        raise ValueError("tool turn did not finish completely")
    if not entries and finish == expected_finish:
        raise ValueError("tool finish without native tool calls")
    ids = set()
    calls = []
    for identifier, name, raw in entries:
        identifier, name = _identifier(identifier, "id"), _identifier(name, "name")
        if identifier in ids:
            raise ValueError("duplicate tool call id")
        ids.add(identifier)
        arguments, error = _arguments(name, raw)
        call = {"id": identifier, "name": name, "arguments": arguments}
        if error:
            call["error"] = error
        calls.append(call)
    usage = data.get("usage", {})
    return assistant, calls, text, finish, usage


def tool_results(api: str, results: list[dict]) -> list[dict]:
    """Encode results, preserving every call ID (including rejected actions)."""
    _api(api)
    if not 1 <= len(results) <= MAX_TOOL_CALLS:
        raise ValueError("invalid tool result count")
    native = []
    ids = set()
    for result in results:
        identifier = _identifier(result.get("id"), "result id")
        if identifier in ids:
            raise ValueError("duplicate tool result id")
        ids.add(identifier)
        content = result["content"]
        if not isinstance(content, str) or len(content.encode()) > MAX_TOOL_RESULT_BYTES:
            raise ValueError("invalid or oversized tool result")
        error = result.get("is_error", False)
        if type(error) is not bool:
            raise ValueError("tool result is_error must be boolean")
        if api == "messages":
            native.append({"type": "tool_result", "tool_use_id": identifier,
                           "content": content, "is_error": error})
        else:
            native.append({"role": "tool", "tool_call_id": identifier,
                           "content": ("ERROR: " if error else "") + content})
    return [{"role": "user", "content": native}] if api == "messages" else native
