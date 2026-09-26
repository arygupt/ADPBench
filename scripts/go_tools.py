"""The agent track's four allowlisted actions, over each API's native tool calls.

    read_file(path)            read PROBLEM.md, dut.py, or dut.v
    write_file(path, content)  replace dut.v (the only writable file)
    check()                    development-case feedback on the current dut.v
    submit()                   freeze the current dut.v as the submission

This module only translates data between the harness and the two wire
formats (chat/completions and messages). It never opens files, runs
commands, or decides whether a design passes; the controller in go_agent.py
owns those boundaries.
"""

from __future__ import annotations

import json
from copy import deepcopy

from scripts.go_pilot import REQUEST_SETTING_KEYS, output_limit

APIS = {"chat/completions", "messages"}
MAX_TOOL_CALLS = 16
MAX_ARGUMENT_BYTES = 1024 * 1024
MAX_TOOL_RESULT_BYTES = 1024 * 1024
MAX_IDENTIFIER_BYTES = 256
READABLE_FILES = {"PROBLEM.md", "dut.py", "dut.v"}


def _object_schema(properties: dict) -> dict:
    """A JSON schema for an object with exactly these (all required) properties."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOLS = (
    {
        "name": "read_file",
        "description": "Read a task workspace file: PROBLEM.md, dut.py, or dut.v.",
        "input_schema": _object_schema({"path": {"type": "string"}}),
    },
    {
        "name": "write_file",
        "description": (
            "Replace the entire dut.v submission file with complete synthesizable "
            "SystemVerilog. No other path is writable."
        ),
        "input_schema": _object_schema({"path": {"type": "string"}, "content": {"type": "string"}}),
    },
    {
        "name": "check",
        "description": (
            "Audit, synthesize and simulate the current dut.v using public development cases. "
            "Returns feedback; does not submit or reveal held-out tests."
        ),
        "input_schema": _object_schema({}),
    },
    {
        "name": "submit",
        "description": (
            "Finalize and freeze the current dut.v as your submission. Call after writing it "
            "and reviewing development-check feedback. No more editing after successful submission."
        ),
        "input_schema": _object_schema({}),
    },
)
TOOL_SCHEMAS = {tool["name"]: tool["input_schema"] for tool in TOOLS}


def _check_api(api: str) -> None:
    if api not in APIS:
        raise ValueError("unsupported Go tool API")


def request_body(model: dict, plan: dict, system: str, messages: list[dict]) -> dict:
    """One agent turn's request, offering the four tools in the model's native schema.

    `messages` is not modified.
    """
    api = model["api"]
    _check_api(api)
    body = {
        "model": model["id"],
        "messages": deepcopy(messages),
        "stream": True,
        model.get("token_limit_key", "max_tokens"): output_limit(plan, model),
    }
    for key in REQUEST_SETTING_KEYS:
        if key in model:
            body[key] = deepcopy(model[key])

    if api == "messages":
        body["system"] = system
        body["tools"] = deepcopy(list(TOOLS))
        body["tool_choice"] = {"type": "auto"}
    else:
        body["messages"].insert(0, {"role": "system", "content": system})
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": deepcopy(tool["input_schema"]),
                },
            }
            for tool in TOOLS
        ]
        body["tool_choice"] = "auto"
        body["stream_options"] = {"include_usage": True}
    return body


def parse_turn(data: dict, api: str) -> tuple[dict, list[dict], str, str, dict]:
    """Split one assistant response into (history message, tool calls, text, finish, usage).

    The history message is the assistant's turn exactly as it must be sent
    back next turn (thinking blocks and signatures unchanged).

    Each tool call is {"id", "name", "arguments"}. A call the harness will not
    execute also has an "error" (and arguments=None); the caller must send
    that error back as the call's result instead of running it.

    Raises ValueError for a malformed or incomplete response.
    """
    _check_api(api)
    if api == "messages":
        assistant, raw_calls, text, finish = _split_messages_turn(data)
        expected_finish = "tool_use"
    else:
        assistant, raw_calls, text, finish = _split_chat_turn(data)
        expected_finish = "tool_calls"

    if not isinstance(text, str) or not isinstance(finish, str):
        raise ValueError("invalid assistant text or finish reason")
    if len(raw_calls) > MAX_TOOL_CALLS:
        raise ValueError("too many tool calls in one turn")
    if raw_calls and finish != expected_finish:
        raise ValueError("tool turn did not finish completely")
    if not raw_calls and finish == expected_finish:
        raise ValueError("tool finish without native tool calls")

    seen_ids = set()
    calls = []
    for call_id, name, raw_arguments in raw_calls:
        call_id = _identifier(call_id, "id")
        name = _identifier(name, "name")
        if call_id in seen_ids:
            raise ValueError("duplicate tool call id")
        seen_ids.add(call_id)
        arguments, error = _parse_arguments(name, raw_arguments)
        call = {"id": call_id, "name": name, "arguments": arguments}
        if error:
            call["error"] = error
        calls.append(call)
    return assistant, calls, text, finish, data.get("usage", {})


def _split_messages_turn(data: dict) -> tuple[dict, list[tuple], str, str]:
    blocks = data.get("content", [])
    if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
        raise ValueError("invalid native content blocks")
    for block in blocks:
        known = block.get("type") in {"text", "thinking", "redacted_thinking", "tool_use"}
        if not known or "partial_json" in block:
            raise ValueError("unsupported or incomplete native content block")

    assistant = {"role": "assistant", "content": deepcopy(blocks)}
    raw_calls = [
        (block.get("id"), block.get("name"), block.get("input"))
        for block in blocks
        if block.get("type") == "tool_use"
    ]
    text = "\n".join(block["text"] for block in blocks if block.get("type") == "text")
    return assistant, raw_calls, text, data.get("stop_reason", "")


def _split_chat_turn(data: dict) -> tuple[dict, list[tuple], str, str]:
    choices = data.get("choices", [])
    if len(choices) != 1:
        raise ValueError("expected one assistant choice")
    assistant = deepcopy(choices[0]["message"])
    assistant["role"] = "assistant"
    if assistant.get("function_call"):
        raise ValueError("legacy function_call is unsupported")

    raw_calls = []
    for call in assistant.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError("invalid native tool call")
        function = call.get("function", {})
        raw_calls.append((call.get("id"), function.get("name"), function.get("arguments")))
    text = assistant.get("content") or ""
    return assistant, raw_calls, text, choices[0].get("finish_reason", "")


def _identifier(value, what: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_IDENTIFIER_BYTES:
        raise ValueError("invalid tool " + what)
    return value


def _parse_arguments(name: str, value) -> tuple[dict | None, str]:
    """(arguments, "") for a valid call, or (None, message for the model).

    Invalid actions become tool feedback, never executable instructions.
    """
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
    if any(not isinstance(item, str) for item in value.values()):
        return None, "All tool argument values must be strings."
    if name == "read_file" and value["path"] not in READABLE_FILES:
        return None, "Only PROBLEM.md, dut.py and dut.v may be read."
    if name == "write_file" and value["path"] != "dut.v":
        return None, "Only dut.v may be written."
    return value, ""


def tool_results(api: str, results: list[dict]) -> list[dict]:
    """Encode tool results as the next turn's messages, keeping every call ID.

    Each result is {"id", "content", "is_error"}; rejected actions are
    reported with is_error=True rather than dropped.
    """
    _check_api(api)
    if not 1 <= len(results) <= MAX_TOOL_CALLS:
        raise ValueError("invalid tool result count")

    encoded = []
    seen_ids = set()
    for result in results:
        call_id = _identifier(result.get("id"), "result id")
        if call_id in seen_ids:
            raise ValueError("duplicate tool result id")
        seen_ids.add(call_id)
        content = result["content"]
        if not isinstance(content, str) or len(content.encode()) > MAX_TOOL_RESULT_BYTES:
            raise ValueError("invalid or oversized tool result")
        is_error = result.get("is_error", False)
        if type(is_error) is not bool:
            raise ValueError("tool result is_error must be boolean")

        if api == "messages":
            encoded.append(
                {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}
            )
        else:
            prefix = "ERROR: " if is_error else ""
            encoded.append({"role": "tool", "tool_call_id": call_id, "content": prefix + content})

    if api == "messages":
        return [{"role": "user", "content": encoded}]
    return encoded


# --------------------------------------------------------------------------
# Reasoning measurement
# --------------------------------------------------------------------------


def reasoning_counts(data: dict, api: str) -> dict:
    """Character counts of one response's reasoning and answer - never the text itself.

    Answer characters include tool-call arguments, since that is where agent
    turns put their RTL. `reasoning_tokens` is only what the provider reports
    (None if it reports nothing).
    """
    _check_api(api)
    reasoning = 0
    answer = 0
    tokens = None

    if api == "messages":
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                reasoning += len(block.get("thinking") or "")
            elif block.get("type") == "text":
                answer += len(block.get("text") or "")
            elif block.get("type") == "tool_use":
                answer += len(json.dumps(block.get("input")))
    else:
        choices = data.get("choices") or [{}]
        first = choices[0]
        message = (first.get("message") or {}) if isinstance(first, dict) else {}
        reasoning = max(
            _text_length(message.get("reasoning_content")), _text_length(message.get("reasoning"))
        )
        answer = _text_length(message.get("content"))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                answer += _text_length((call.get("function") or {}).get("arguments"))
        details = (data.get("usage") or {}).get("completion_tokens_details") or {}
        reported = details.get("reasoning_tokens")
        if type(reported) is int and reported >= 0:
            tokens = reported

    return {"reasoning_chars": reasoning, "answer_chars": answer, "reasoning_tokens": tokens}


def _text_length(value) -> int:
    return len(value) if isinstance(value, str) else 0


def empty_reasoning_totals() -> dict:
    return {
        "turns": 0,
        "turns_with_reasoning": 0,
        "reasoning_chars": 0,
        "answer_chars": 0,
        "reasoning_tokens": None,
    }


def add_reasoning_counts(totals: dict, counts: dict) -> None:
    """Add one turn's `reasoning_counts` into running `totals` (in place)."""
    totals["turns"] += 1
    if counts["reasoning_chars"] > 0:
        totals["turns_with_reasoning"] += 1
    totals["reasoning_chars"] += counts["reasoning_chars"]
    totals["answer_chars"] += counts["answer_chars"]
    if counts["reasoning_tokens"] is not None:
        totals["reasoning_tokens"] = (totals["reasoning_tokens"] or 0) + counts["reasoning_tokens"]
