"""Finite, single-shot OpenCode Go pilot. Generation and offline scoring are separate.

    python -m scripts.go_pilot gate       # GitHub cron: is a model due now?
    python -m scripts.go_pilot generate   # one request per planned problem
    python -m scripts.go_pilot score      # score saved RTL in the toolchain container
    python -m scripts.go_pilot fixture    # score the baselines; no model calls

No model-supplied commands are executed. Only RTL is accepted, audited, and
scored in the pinned, network-disabled toolchain container. Credentials exist
only in the generation step's environment; requests are never retried.

This module also holds the plan handling and small helpers that the other Go
scripts (agent track, canary, publication) share.

Output layout, per model and problem ("slot"):

    <out>/opencode-go-<model>/<problem>/rep1/
        generation.json   what was requested and what came back (no model text)
        prompt.txt, request.json, response.json, response.txt
        dut.v             the extracted RTL, if any
        record.json, manifest.json, scoring-state.json   written by `score`
    <out>/opencode-go-<model>/<problem>/rep1_frozen/dut.v   the scored copy
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from adpbench.agent import RunRecord, build_manifest
from adpbench.audit import audit_submission
from adpbench.durable import atomic_json
from adpbench.environment import render_problem_md, render_skeleton
from adpbench.evaluate import evaluate_multi
from adpbench.hashing import sha256_bytes
from adpbench.problem import Problem, load_problem, repo_root
from adpbench.process import is_infrastructure_failure
from adpbench.report import write_report
from adpbench.seeds import EVAL_SEEDS
from scripts.go_stream import StreamFailure, call_streaming

PLAN = repo_root() / "pilot/go-20260921.json"
BASE_URL = "https://opencode.ai/zen/go/v1/"
USER_AGENT = "adpbench-coding-agent/0.0.1"
SCORING_IMAGE = "adpbench-go:ci"
AGENT_PROTOCOL = "agent-assisted-v1"

SYSTEM = (
    "You are an RTL coding agent. Implement the supplied coding task in one "
    "self-contained synthesizable SystemVerilog module named dut. "
    "Return only the complete dut.v source, optionally in one verilog code fence. "
    "There are no tool calls or repair turns in this single-shot evaluation. "
    "Prioritize correctness, independent stream handshakes, signed arithmetic, "
    "and back-to-back transactions, then minimize area times cycles."
)

# Model settings that are sent with each request and recorded with each result.
SETTING_KEYS = ("thinking", "reasoning_effort", "reasoning", "token_limit_key")
REQUEST_SETTING_KEYS = ("thinking", "reasoning_effort", "reasoning")
GITHUB_KEYS = ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")

ERROR_BODY_LIMIT = 16384
CAP_FINISHES = {"length", "max_tokens"}
COMPLETE_FINISHES = {"stop", "end_turn", "stop_sequence"}

# Outcomes where the provider or harness failed, rather than the model.
INFRASTRUCTURE_OUTCOMES = {
    "provider_error",
    "transport_interrupted",
    "harness_error",
    "scoring_interrupted",
    "not_requested",
    "interrupted",
}

# Plan limits. A plan outside these bounds is rejected before any request.
ALLOWED_PROBLEMS = {"001_dot_product", "002_gemv", "003_matmul", "004_conv1d"}
MAX_MODELS = 6
MAX_REQUESTS = 12
MAX_PROMPT_BYTES = 24000
MAX_REQUEST_TIMEOUT_S = 600
MAX_REQUEST_WALL_TIMEOUT_S = 3600
MAX_AUTHORIZATION_WINDOW_S = 2 * 24 * 3600
PLAN_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,79}")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def slot_dir(out: Path, model_id: str, problem_id: str) -> Path:
    """Where one model's attempt at one problem is recorded."""
    return out / f"opencode-go-{model_id}" / problem_id / "rep1"


def problem_dir(problem_id: str) -> Path:
    return repo_root() / "problems" / "level1" / problem_id


def generation_settings(model: dict) -> dict:
    """The reviewed request settings for a model, as recorded with each result."""
    return {key: model[key] for key in SETTING_KEYS if key in model}


def github_context() -> dict:
    """Identifies the GitHub Actions run a generation happened in ("" locally)."""
    return {key: os.environ.get(key, "") for key in GITHUB_KEYS}


def redact_key(data: dict, key: str) -> dict:
    """A copy of JSON-compatible `data` with every occurrence of `key` replaced."""
    return json.loads(json.dumps(data).replace(key, "[REDACTED]"))


def redact_credentials(text: str, key: str) -> str:
    """Remove the known key, bearer tokens, and common API-token shapes from text."""
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return text


def execution_health(outcome: str) -> str:
    return "failed" if outcome in INFRASTRUCTURE_OUTCOMES else "completed"


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


def read_plan(path: Path = PLAN) -> dict:
    plan = json.loads(path.read_text())
    validate_plan(plan)
    return plan


def select_model(plan: dict, model_id: str) -> dict:
    for model in plan["models"]:
        if model["id"] == model_id:
            return model
    raise ValueError(f"model {model_id!r} is not in the reviewed plan")


def output_limit(plan: dict, model: dict) -> int:
    """The reviewed output-token limit for one request, with no extra harness-wide ceiling."""
    limits = plan["max_output_tokens"]
    if isinstance(limits, dict):
        return limits[model["id"]]
    return limits


def due(plan: dict, model: dict, now: datetime) -> bool:
    """Whether `model` may be called at `now` under the plan's authorization window."""
    if not plan.get("generation_enabled", True):
        return False
    starts = datetime.fromisoformat(model["not_before"])
    expires = datetime.fromisoformat(plan["expires_at"])
    return starts <= now < expires


def gate(plan: dict, cron: str, now: datetime) -> dict:
    """Which model (if any) a scheduled GitHub cron trigger should run now."""
    for model in plan["models"]:
        if model.get("cron") and model["cron"] == cron:
            return {"run": due(plan, model, now), "model": model["id"]}
    return {"run": False, "model": ""}


def _is_positive_int(value, maximum: int | None = None) -> bool:
    # `type(...) is int` rather than isinstance: JSON `true` must not count as 1.
    if type(value) is not int or value <= 0:
        return False
    return maximum is None or value <= maximum


def validate_plan(plan: dict) -> None:
    """Reject a plan that could send more, or different, requests than were reviewed.

    Bounds the number of requests, prompt size, timeouts, output limits, and
    each model's authorization window. Raises ValueError on the first problem.
    """
    problems = plan["problems"]
    models = plan["models"]
    if not problems or len(set(problems)) != len(problems) or not set(problems) <= ALLOWED_PROBLEMS:
        raise ValueError("invalid or duplicate problems")
    if not 1 <= len(models) <= MAX_MODELS or len(models) * len(problems) > MAX_REQUESTS:
        raise ValueError("a batch may contain at most twelve requests across six models")
    if not _is_positive_int(plan["max_prompt_bytes"], MAX_PROMPT_BYTES):
        raise ValueError("invalid max_prompt_bytes")
    if not _is_positive_int(plan["request_timeout_s"], MAX_REQUEST_TIMEOUT_S):
        raise ValueError("invalid request_timeout_s")
    if not PLAN_NAME.fullmatch(plan["name"]):
        raise ValueError("invalid batch name")

    model_ids = [model["id"] for model in models]
    if len(set(model_ids)) != len(model_ids):
        raise ValueError("duplicate model")
    _validate_output_limits(plan, model_ids)

    if type(plan.get("generation_enabled", True)) is not bool:
        raise ValueError("generation_enabled must be boolean")
    if type(plan.get("stream", False)) is not bool:
        raise ValueError("stream must be boolean")
    if not _is_positive_int(plan.get("request_wall_timeout_s", 600), MAX_REQUEST_WALL_TIMEOUT_S):
        raise ValueError("invalid request wall timeout")

    expires = datetime.fromisoformat(plan["expires_at"])
    if expires.tzinfo is None:
        raise ValueError("expiry must include a timezone")
    for model in models:
        _validate_model(model, expires)


def _validate_output_limits(plan: dict, model_ids: list[str]) -> None:
    """`fixed`: one integer limit for every model. `provider_max`: one per model."""
    mode = plan.get("output_budget", "fixed")
    limits = plan["max_output_tokens"]
    if mode == "provider_max":
        if not isinstance(limits, dict) or set(limits) != set(model_ids):
            raise ValueError("provider_max requires an explicit limit for every planned model")
        values = list(limits.values())
    elif mode == "fixed" and not isinstance(limits, dict):
        values = [limits]
    else:
        raise ValueError("invalid output budget mode")
    if not all(_is_positive_int(value) for value in values):
        raise ValueError("max_output_tokens must contain positive integer provider limits")


def _validate_model(model: dict, expires: datetime) -> None:
    if not MODEL_ID.fullmatch(model["id"]):
        raise ValueError("invalid model ID")
    if model["api"] not in {"chat/completions", "messages"}:
        raise ValueError("unsupported Go endpoint")
    if model.get("token_limit_key", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("invalid output limit field")
    if "reasoning" in model and model["reasoning"] not in ({"enabled": False}, {"enabled": True}):
        raise ValueError("unsupported normalized reasoning control")
    starts = datetime.fromisoformat(model["not_before"])
    window = (expires - starts).total_seconds()
    if starts.tzinfo is None or not 0 < window <= MAX_AUTHORIZATION_WINDOW_S:
        raise ValueError("authorization window must be positive and at most two days")


# --------------------------------------------------------------------------
# Requests and responses
# --------------------------------------------------------------------------


def prompt_for(problem: Problem) -> str:
    """PROBLEM.md, dut.py, and the skeleton, adapted for a caller with no tools."""
    baseline = json.loads(problem.baseline_metrics.read_text())
    # Single-shot callers have neither a filesystem nor tools, so drop the
    # interactive workspace's instructions to run check.sh or inspect files.
    spec = render_problem_md(problem, baseline)
    spec = spec.replace("`dut.py` in this directory", "`dut.py` supplied below")
    spec = re.sub(
        r"## How to check your work\n.*?(?=## Rules)",
        "## Evaluation\n\nThere are no development tools or repair turns in this single-shot track.\n\n",
        spec,
        flags=re.S,
    )
    skeleton = render_skeleton(problem).replace(
        "// Run ./check.sh for synthesis + simulation feedback.\n", ""
    )
    spec_source = (problem.root / "dut.py").read_text()
    return (
        spec
        + "\n\n## dut.py\n```python\n" + spec_source
        + "\n```\n\n## Starting dut.v\n```verilog\n" + skeleton + "\n```\n"
    )


def request_body(model: dict, prompt: str, plan: dict) -> dict:
    """The request for one single-shot generation, in the model's wire format."""
    messages = [{"role": "user", "content": prompt}]
    stream = plan.get("stream", False)
    body = {
        "model": model["id"],
        "messages": messages,
        model.get("token_limit_key", "max_tokens"): output_limit(plan, model),
        "stream": stream,
    }
    for key in REQUEST_SETTING_KEYS:
        if key in model:
            body[key] = model[key]

    if model["api"] == "messages":
        body["system"] = SYSTEM
    else:
        messages.insert(0, {"role": "system", "content": SYSTEM})
        if stream:
            body["stream_options"] = {"include_usage": True}
    return body


def parse_response(data: dict, api: str) -> tuple[str, str, dict]:
    """(answer text, finish reason, usage) from either wire format."""
    if api == "messages":
        blocks = data.get("content", [])
        text = "\n".join(block["text"] for block in blocks if block.get("type") == "text")
        finish = data.get("stop_reason", "")
    else:
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        finish = choice.get("finish_reason", "")
    return text, finish, data.get("usage", {})


def response_diagnostics(data: dict, api: str) -> dict:
    """Field-level evidence about a response, kept even when token accounting is wrong."""
    if api == "messages":
        blocks = data.get("content", [])
        thinking = [block for block in blocks if block.get("type") == "thinking"]
        return {
            "content_block_types": [block.get("type") for block in blocks],
            "reasoning_chars": sum(len(block.get("thinking", "")) for block in thinking),
        }

    message = data["choices"][0]["message"]
    return {
        "message_fields": sorted(message),
        "answer_chars": len(message.get("content") or ""),
        "reasoning_chars": max(
            len(message.get("reasoning") or ""), len(message.get("reasoning_content") or "")
        ),
        "has_reasoning_details": bool(message.get("reasoning_details")),
    }


def valid_usage(usage: dict, api: str, cap: int) -> bool:
    """Whether provider token accounting is present, sane, and within `cap` output tokens."""
    if not isinstance(usage, dict):
        return False
    if api == "messages":
        inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
    else:
        inputs, outputs = usage.get("prompt_tokens"), usage.get("completion_tokens")
    inputs_ok = type(inputs) is int and inputs >= 0
    outputs_ok = type(outputs) is int and 0 <= outputs <= cap
    return inputs_ok and outputs_ok


def extract_rtl(text: str) -> str:
    """The single complete `module dut ... endmodule` in a response, or ValueError."""
    blocks = re.findall(r"```(?:verilog|systemverilog|sv)?\s*\n(.*?)```", text, re.S | re.I)
    if blocks:
        if len(blocks) != 1:
            raise ValueError("expected one complete RTL source block")
        text = blocks[0]
    if not re.search(r"\bmodule\s+dut\b", text) or not re.search(r"\bendmodule\b", text):
        raise ValueError("response contains no complete dut module")
    return text.strip() + "\n"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the Go credential must never be sent to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call_model(
    model: dict,
    body: dict,
    key: str,
    session: str,
    timeout: int,
    *,
    evidence: Path | None = None,
    wall_timeout: int = 600,
) -> dict:
    """Send one request to OpenCode Go and return the parsed response. Never retries.

    Streaming requests save partial output under `evidence` as it arrives.
    """
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": session,
    }
    if model["api"] == "messages":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"

    if body.get("stream"):
        if evidence is None:
            raise ValueError("streaming requires a durable evidence directory")
        return call_streaming(model["api"], body, headers, evidence, key, timeout, wall_timeout)

    # urllib does not retry. Headers and raw provider errors are never logged.
    request = urllib.request.Request(BASE_URL + model["api"], json.dumps(body).encode(), headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        return json.load(response)


def provider_error_evidence(exc: urllib.error.HTTPError, key: str) -> dict:
    """A private record of a rejected request: bounded body, credentials redacted, no headers.

    Never print provider text, retry a rejected request, or put this body in
    the public generation metadata. A missing body is recorded as missing.
    """
    evidence = {"status": exc.code, "body_available": False, "truncated": False}
    try:
        raw = exc.read(ERROR_BODY_LIMIT + 1)
        text = raw[:ERROR_BODY_LIMIT].decode("utf-8", errors="replace")
        evidence["body_available"] = bool(raw)
        evidence["body"] = redact_credentials(text, key)
        evidence["truncated"] = len(raw) > ERROR_BODY_LIMIT
    except Exception:  # noqa: BLE001 - evidence collection must not raise
        evidence["read_error"] = True
    finally:
        exc.close()
    return evidence


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def generate(plan: dict, model: dict, out: Path) -> None:
    """Request one single-shot generation per planned problem.

    Every scheduled slot gets a generation.json before any request, so a hard
    kill still leaves a record for each slot. Requests are never retried.
    After a provider, transport, or accounting failure the remaining slots are
    recorded as not requested, and RuntimeError is raised at the end.
    """
    validate_plan(plan)
    if model != select_model(plan, model["id"]):
        raise ValueError("model must match the reviewed plan")
    if not due(plan, model, now_utc()):
        raise RuntimeError("outside the model's authorized schedule window")
    key = os.environ.get("OPENCODE_GO_API_KEY", "")
    if not key:
        raise RuntimeError("OPENCODE_GO_API_KEY is missing")

    out.mkdir(parents=True, exist_ok=True)
    # Never generate twice into one directory. This local guard complements
    # the durable GitHub tag claim.
    with (out / "generation-started.json").open("x") as handle:
        json.dump({"model": model["id"], "started": now_utc().isoformat()}, handle)
    atomic_json(out / "plan.json", plan)

    for problem_id in plan["problems"]:
        pending = _new_generation_record(plan, model, problem_id)
        pending["generation_state"] = "pending"
        pending["error"] = "not requested: generation job stopped before this request"
        atomic_json(slot_dir(out, model["id"], problem_id) / "generation.json", pending)

    stop_reason = ""
    for problem_id in plan["problems"]:
        dest = slot_dir(out, model["id"], problem_id)
        dest.mkdir(parents=True, exist_ok=True)
        record = _new_generation_record(plan, model, problem_id)
        if stop_reason:
            record["error"] = "not requested: batch stopped after " + stop_reason
            atomic_json(dest / "generation.json", record)
            continue

        stop_reason = _generate_slot(plan, model, problem_id, dest, record, key)
        outcome = record["error"] or record.get("invalid_rtl") or "generated"
        print(f"{problem_id}: {outcome}", flush=True)

    if stop_reason:
        raise RuntimeError(stop_reason)


def _new_generation_record(plan: dict, model: dict, problem_id: str) -> dict:
    return {
        "model": model["id"],
        "problem": problem_id,
        "protocol": "single-shot",
        "started": now_utc().isoformat(),
        "max_output_tokens": output_limit(plan, model),
        "error": "",
        "generation_settings": generation_settings(model),
        "github": github_context(),
    }


def _generate_slot(plan: dict, model: dict, problem_id: str, dest: Path, record: dict, key: str) -> str:
    """Make one request and save its evidence. Returns why the batch must stop, or ""."""
    problem = load_problem(problem_dir(problem_id))
    prompt = prompt_for(problem)
    (dest / "prompt.txt").write_text(SYSTEM + "\n\n" + prompt)
    prompt_bytes = len((SYSTEM + prompt).encode())
    record["prompt_bytes"] = prompt_bytes

    started = time.monotonic()
    requested = False
    stop_reason = ""
    try:
        if not due(plan, model, now_utc()):
            raise RuntimeError("schedule expired")
        if prompt_bytes > plan["max_prompt_bytes"]:
            raise RuntimeError("prompt exceeds size cap")

        body = request_body(model, prompt, plan)
        atomic_json(dest / "request.json", body)
        wall_timeout = plan.get("request_wall_timeout_s", 600)
        if body["stream"]:
            record["transport"] = {
                "stream": True,
                "idle_timeout_s": plan["request_timeout_s"],
                "wall_timeout_s": wall_timeout,
            }
        # If the process dies mid-request, this is what remains on disk.
        in_progress = {
            **record,
            "generation_state": "in_progress",
            "incomplete_usage": True,
            "error": "generation interrupted before completion",
        }
        atomic_json(dest / "generation.json", in_progress)

        requested = True
        session = f"adpbench-{plan['name']}-{model['id']}-{problem_id}"
        data = call_model(
            model,
            body,
            key,
            session,
            plan["request_timeout_s"],
            evidence=dest,
            wall_timeout=wall_timeout,
        )
        # Raw model output is evidence, never executable commands.
        safe_data = redact_key(data, key)
        atomic_json(dest / "response.json", safe_data)
        text, finish, usage = parse_response(safe_data, model["api"])
        record["finish_reason"] = finish
        record["usage"] = usage if isinstance(usage, dict) else {}
        record["response_id"] = data.get("id", "")
        record["response_model"] = data.get("model", "")
        record["response_diagnostics"] = response_diagnostics(data, model["api"])
        (dest / "response.txt").write_text(text)

        # Unknown accounting means unknown spend: stop the batch.
        if not usage:
            record["error"] = "missing provider token accounting"
            record["incomplete_usage"] = True
            stop_reason = record["error"]
        elif not valid_usage(usage, model["api"], output_limit(plan, model)):
            record["error"] = "invalid provider token accounting or output cap exceeded"
            record["incomplete_usage"] = True
            stop_reason = record["error"]

        if finish in CAP_FINISHES:
            record["error"] = "generation reached output cap; no retry"
        elif finish not in COMPLETE_FINISHES:
            record["error"] = "unexpected provider stop reason; no retry"
            stop_reason = record["error"]
        elif not record["error"]:
            try:
                (dest / "dut.v").write_text(extract_rtl(text))
            except ValueError as exc:
                record["invalid_rtl"] = str(exc)

        if plan.get("stop_on_invalid_output") and (record["error"] or record.get("invalid_rtl")):
            stop_reason = record["error"] or record["invalid_rtl"]

    except urllib.error.HTTPError as exc:
        record["error"] = f"provider HTTP {exc.code}; no retry or fallback"
        stop_reason = record["error"]
        atomic_json(dest / "provider_error.json", provider_error_evidence(exc, key))
    except StreamFailure as exc:
        record["error"] = f"generation incomplete: {exc}; no retry or fallback"
        record["incomplete_usage"] = True
        stop_reason = record["error"]
    except Exception as exc:  # noqa: BLE001
        # Exception text can contain sensitive server response details; only
        # the exception type is recorded.
        record["error"] = f"request stopped ({type(exc).__name__}); no retry"
        record["incomplete_usage"] = requested
        stop_reason = record["error"]

    record["duration_s"] = round(time.monotonic() - started, 2)
    failed = record["error"] or record.get("invalid_rtl")
    record["generation_state"] = "failed" if failed else "completed"
    atomic_json(dest / "generation.json", record)
    return stop_reason


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score(
    plan: dict,
    model: dict,
    out: Path,
    *,
    problem_id: str | None = None,
    checkpoint_root: Path | None = None,
    resume: bool = False,
    deadline_s: float | None = None,
) -> None:
    """Score every generated slot (or just `problem_id`) on the held-out cases.

    A completed score, passing or failing, is never recomputed: `resume` only
    finishes interrupted work.
    """
    if problem_id is not None and problem_id not in plan["problems"]:
        raise ValueError("problem is not in the reviewed plan")
    for current in plan["problems"]:
        if problem_id is None or current == problem_id:
            _score_slot(plan, model, out, current, checkpoint_root, resume, deadline_s)
    summarize(plan, model, out)


def _score_slot(
    plan: dict,
    model: dict,
    out: Path,
    problem_id: str,
    checkpoint_root: Path | None,
    resume: bool,
    deadline_s: float | None,
) -> None:
    dest = slot_dir(out, model["id"], problem_id)
    generation_path = dest / "generation.json"
    if not generation_path.exists():
        return
    generation = json.loads(generation_path.read_text())
    agent_track = generation.get("protocol") == AGENT_PROTOCOL
    record_path = dest / "record.json"
    if record_path.exists() and not resume:
        raise FileExistsError("scoring record exists; use --resume or a fresh output directory")

    problem = load_problem(problem_dir(problem_id))
    if agent_track:
        agent_cmd = "adpbench Go agent-assisted-v1 (read/write/check/submit)"
    else:
        agent_cmd = "adpbench Go API single-shot (one request, no repair)"
    record = RunRecord(
        problem=problem_id,
        agent_cmd=agent_cmd,
        label=_run_label(model["id"], generation),
        group=plan["name"],
        started=generation["started"],
        duration_s=generation.get("duration_s", 0),
        error=generation.get("error", "") or generation.get("invalid_rtl", ""),
        sandbox="docker",
    )

    source = dest / "dut.v"
    frozen = None
    if not source.exists():
        reason = generation.get("invalid_rtl", "no RTL produced")
        record.audit = {
            "ok": False,
            "violations": [{"line": 0, "match": "", "reason": reason}],
            "warnings": [],
        }
    else:
        if agent_track:
            submitted = generation.get("outcome") == "submitted"
            receipt_matches = generation.get("submission_sha256") == sha256_bytes(source.read_bytes())
            if not (submitted and receipt_matches):
                raise ValueError("agent submission lacks a matching explicit submit receipt")

        frozen = _freeze(source, dest.parent / "rep1_frozen" / "dut.v")
        record.frozen = str(frozen)
        record.audit = audit_submission(frozen.read_text())

        if resume and record_path.exists():
            previous = json.loads(record_path.read_text())
            if previous.get("result") is not None:
                # A completed pass OR failure is immutable. Recovery only
                # finishes interrupted work, never repairs a scored design.
                print(f"{problem_id}: retaining completed scoring result", flush=True)
                return

        if record.audit["ok"] and not record.error:
            _write_interrupted_record(plan, problem, record, frozen, generation, dest, agent_track)
            checkpoint = checkpoint_root / model["id"] / problem_id if checkpoint_root else None
            source_label = "go-agent-assisted-v1" if agent_track else "go-single-shot"
            try:
                print(f"{problem_id}: scoring frozen RTL on held-out cases", flush=True)
                result = evaluate_multi(
                    problem,
                    [frozen],
                    seeds=EVAL_SEEDS,
                    source=source_label,
                    tag="eval",
                    checkpoint_dir=checkpoint,
                    resume=resume,
                    deadline_s=deadline_s,
                )
                record.result = result.to_dict()
                failure = record.result.get("metadata", {}).get("failure_kind", "")
                if is_infrastructure_failure(failure):
                    record.error = f"scoring infrastructure: {failure}; inspect stage logs"
            except Exception as exc:  # noqa: BLE001 - the record must survive scorer failures
                record.error = f"scoring failed: {type(exc).__name__}; see private scoring logs"
                print(record.error, flush=True)

    record.manifest = _scoring_manifest(plan, problem, record, frozen, generation)
    atomic_json(dest / "manifest.json", record.manifest)
    payload = _record_dict(record)
    if agent_track:
        payload.update(agent_outcome(payload, generation))
    atomic_json(record_path, payload)
    atomic_json(dest / "scoring-state.json", {"state": "completed", "has_result": record.result is not None})

    result = record.result or {}
    stage = (result.get("metadata") or {}).get("stage", "no submission")
    print(
        f"{problem_id}: scoring complete; correct={bool(result.get('correct'))}; stage={stage}",
        flush=True,
    )


def _run_label(model_id: str, generation: dict) -> str:
    protocol = generation.get("protocol")
    if protocol == "fixture":
        return "scripted/baseline-fixture"
    track = AGENT_PROTOCOL if protocol == AGENT_PROTOCOL else "single-shot"
    return f"opencode-go/{model_id} [{track}]"


def _freeze(source: Path, frozen: Path) -> Path:
    """Copy the generated RTL to the scored location, refusing to overwrite different bytes."""
    frozen.parent.mkdir(exist_ok=True)
    if frozen.exists():
        if frozen.read_bytes() != source.read_bytes():
            raise ValueError("frozen RTL differs from saved generation; refusing to overwrite")
    else:
        frozen.write_bytes(source.read_bytes())
    return frozen


def _scoring_manifest(
    plan: dict, problem: Problem, record: RunRecord, frozen: Path | None, generation: dict
) -> dict:
    budget_s = plan.get("slot_timeout_s", plan["request_timeout_s"])
    manifest = build_manifest(problem, record, frozen, budget_s, "docker", SCORING_IMAGE, "none")
    manifest["generation"] = generation
    return manifest


def _record_dict(record: RunRecord) -> dict:
    """A plain, JSON-compatible copy of a run record."""
    return json.loads(record.to_json())


def _write_interrupted_record(
    plan: dict,
    problem: Problem,
    record: RunRecord,
    frozen: Path,
    generation: dict,
    dest: Path,
    agent_track: bool,
) -> None:
    """Before scoring starts, save a truthful 'interrupted' record.

    If the scorer is killed, this publishable record remains rather than
    nothing (or a stale result).
    """
    record.manifest = _scoring_manifest(plan, problem, record, frozen, generation)
    progress = _record_dict(record)
    progress["error"] = "scoring interrupted before completion; see scoring checkpoints"
    if agent_track:
        progress["outcome"] = "scoring_interrupted"
        progress["execution_health"] = "failed"
    atomic_json(dest / "manifest.json", record.manifest)
    atomic_json(dest / "record.json", progress)
    atomic_json(dest / "scoring-state.json", {"state": "running"})


def agent_outcome(record: dict, generation: dict) -> dict:
    """The typed outcome of an agent-track slot, and whether execution was healthy.

    Execution health is distinct from a legitimate failed measurement: wrong
    RTL is a completed measurement, a provider error is not.
    """
    outcome = generation.get("outcome", "not_requested")
    if outcome == "submitted":
        if record.get("error"):
            outcome = "scoring_interrupted"
        elif not record.get("audit", {}).get("ok"):
            outcome = "audit_rejected"
        elif record.get("result") is None:
            outcome = "scoring_interrupted"
        elif record["result"].get("correct"):
            outcome = "correct"
        else:
            outcome = "incorrect"
    return {"outcome": outcome, "execution_health": execution_health(outcome)}


def save_scoring_failure(plan: dict, model: dict, out: Path, problem_id: str, reason: str) -> None:
    """Record that scoring crashed. The score is unknown: never claim a pass or a DUT failure."""
    dest = slot_dir(out, model["id"], problem_id)
    record_path = dest / "record.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        if record.get("result") is not None:
            return  # keep a finalized score even if post-scoring cleanup failed
    else:
        record = _unscored_record(plan, model, problem_id, dest)

    record["error"] = f"scoring infrastructure: {reason}; score unknown; frozen generation preserved"
    if plan.get("protocol") == AGENT_PROTOCOL:
        record["outcome"] = "scoring_interrupted"
        record["execution_health"] = "failed"
    record["manifest"]["error"] = record["error"]
    atomic_json(dest / "manifest.json", record["manifest"])
    atomic_json(record_path, record)
    atomic_json(dest / "scoring-state.json", {"state": "interrupted", "reason": reason})


def _unscored_record(plan: dict, model: dict, problem_id: str, dest: Path) -> dict:
    """A record (with manifest) for a slot whose scorer never wrote one."""
    generation = json.loads((dest / "generation.json").read_text())
    problem = load_problem(problem_dir(problem_id))
    track = AGENT_PROTOCOL if generation.get("protocol") == AGENT_PROTOCOL else "single-shot"
    record = RunRecord(
        problem=problem_id,
        agent_cmd=f"adpbench Go API {track}",
        label=f"opencode-go/{model['id']} [{track}]",
        group=plan["name"],
        started=generation["started"],
        sandbox="docker",
    )
    frozen = dest.parent / "rep1_frozen" / "dut.v"
    if not frozen.exists() and (dest / "dut.v").exists():
        frozen.parent.mkdir(exist_ok=True)
        frozen.write_bytes((dest / "dut.v").read_bytes())
    record.frozen = str(frozen) if frozen.exists() else ""
    record.manifest = _scoring_manifest(
        plan, problem, record, frozen if frozen.exists() else None, generation
    )
    return _record_dict(record)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


def summarize(plan: dict, model: dict, out: Path) -> None:
    """Write report.json and REPORT.md for `out`, plus usage.json with token totals."""
    agent_track = plan.get("protocol") == AGENT_PROTOCOL
    turns = plan.get("max_turns", 1) if agent_track else 1
    usage_cap = output_limit(plan, model) * turns

    total_input = 0
    total_output = 0
    incomplete_usage = False
    for problem_id in plan["problems"]:
        path = slot_dir(out, model["id"], problem_id) / "generation.json"
        if not path.exists():
            # Each agent-track job scores one problem, so a sibling slot that
            # belongs to another job is not missing usage.
            if not agent_track:
                incomplete_usage = True
            continue

        generation = json.loads(path.read_text())
        if generation.get("incomplete_usage"):
            incomplete_usage = True
        usage = generation.get("usage", {})
        if not usage:
            continue
        # Never turn unknown or invalid accounting into a made-up total.
        if not valid_usage(usage, model["api"], usage_cap):
            incomplete_usage = True
            continue
        total_input += usage.get("prompt_tokens", usage.get("input_tokens", 0))
        total_output += usage.get("completion_tokens", usage.get("output_tokens", 0))
        for cache_key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(cache_key, 0)
            if type(value) is int and value >= 0:
                total_input += value

    write_report(out)
    atomic_json(
        out / "usage.json",
        {
            "input_tokens_including_cache": total_input,
            "output_tokens": total_output,
            "incomplete_usage": incomplete_usage,
        },
    )

    with (out / "REPORT.md").open("a") as report:
        if agent_track:
            report.write(
                f"\nProtocol: agent-assisted-v1; at most {plan['max_turns']} turns and "
                f"{plan['max_checks']} development checks per slot. Explicit file submission, "
                "one held-out evaluation.\n"
            )
            report.write(
                f"\nProvider-reported input (including cache): {total_input}; "
                f"output: {total_output}. Usage incomplete: {incomplete_usage}.\n"
            )
            report.write("\nSeparate track: do not combine with single-shot or pilot-001 scores.\n")
            return

        budget = plan.get("output_budget", "fixed")
        report.write(
            "\nProtocol: one generation per problem, no feedback or retries. "
            f"Output cap: {output_limit(plan, model)} tokens/request ({budget}).\n"
        )
        report.write(
            f"\nProvider-reported input (including cache): {total_input}; "
            f"output: {total_output} tokens.\n"
        )
        report.write(f"\nGeneration settings: `{json.dumps(generation_settings(model))}`.\n")
        report.write(
            "\nThese single-shot results use a different generation budget from the "
            "iterative pilot-001.\n"
        )
        if incomplete_usage:
            report.write(
                "\nToken totals are incomplete; an interrupted response may have consumed "
                "additional subscription quota.\n"
            )


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def run_fixture(plan: dict, model: dict, out: Path) -> None:
    """Exercise the full scoring path with each problem's known-good baseline. No HTTP calls."""
    for problem_id in plan["problems"]:
        dest = slot_dir(out, model["id"], problem_id)
        generation = {"started": now_utc().isoformat(), "protocol": "fixture", "error": ""}
        atomic_json(dest / "generation.json", generation)
        (dest / "dut.v").write_bytes((problem_dir(problem_id) / "baseline.v").read_bytes())
    score(plan, model, out)
    report = json.loads((out / "report.json").read_text())
    if report["totals"]["correct"] != len(plan["problems"]):
        raise RuntimeError("offline baseline fixture failed")
    print(f"All {len(plan['problems'])} offline baseline fixtures passed. No Go requests made.")


def write_github_outputs(values: dict) -> None:
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        for name, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else value
            handle.write(f"{name}={text}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["gate", "generate", "score", "fixture"])
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--out", type=Path, default=Path("runs/go"))
    parser.add_argument("--problem", help="Score only this planned problem; no model calls")
    parser.add_argument("--checkpoint-root", type=Path)
    parser.add_argument(
        "--resume", action="store_true", help="Reuse hash-verified scoring checkpoints; never regenerate"
    )
    parser.add_argument(
        "--deadline-s", type=float, help="Soft scoring wall deadline, below the outer job timeout"
    )
    args = parser.parse_args()
    plan = read_plan(args.plan)

    if args.mode == "gate":
        result = gate(plan, os.environ.get("SCHEDULE", ""), now_utc())
        write_github_outputs(result)
        print(json.dumps(result))
        return

    model = select_model(plan, args.model)
    if args.mode == "generate":
        generate(plan, model, args.out)
    elif args.mode == "score":
        score(
            plan,
            model,
            args.out,
            problem_id=args.problem,
            checkpoint_root=args.checkpoint_root,
            resume=args.resume,
            deadline_s=args.deadline_s,
        )
    else:
        run_fixture(plan, model, args.out)


if __name__ == "__main__":
    main()
