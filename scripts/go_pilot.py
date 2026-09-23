"""Finite, single-shot Go pilot. Generation and offline scoring are separate.

No model-supplied commands are executed. Only RTL is accepted, audited, and
scored in the pinned, network-disabled toolchain container. Credentials exist
only in the generation step's environment; requests are never retried.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from adpbench.agent import RunRecord, audit_submission, build_manifest, render_problem_md, render_skeleton
from adpbench.evaluate import EVAL_SEEDS, evaluate_multi
from adpbench.problem import load_problem, repo_root
from adpbench.report import write_report
from adpbench.durable import atomic_json
from scripts.go_stream import call_streaming, StreamFailure

PLAN = repo_root() / "pilot/go-20260921.json"
BASE_URL = "https://opencode.ai/zen/go/v1/"
USER_AGENT = "adpbench-coding-agent/0.0.1"
SYSTEM = (
    "You are an RTL coding agent. Implement the supplied coding task in one "
    "self-contained synthesizable SystemVerilog module named dut. "
    "Return only the complete dut.v source, optionally in one verilog code fence. "
    "There are no tool calls or repair turns in this single-shot evaluation. "
    "Prioritize correctness, independent stream handshakes, signed arithmetic, "
    "and back-to-back transactions, then minimize area times cycles."
)
SETTING_KEYS = ("thinking", "reasoning_effort", "reasoning", "token_limit_key")
ERROR_BODY_LIMIT = 16384


def provider_error_evidence(exc: urllib.error.HTTPError, key: str) -> dict:
    """Private artifact only: bounded body, known credential redacted, no headers.

    Never print provider text, retry a rejected request, or put this body in
    the public generation metadata. A missing body remains explicitly missing.
    """
    evidence = {"status": exc.code, "body_available": False, "truncated": False}
    try:
        raw = exc.read(ERROR_BODY_LIMIT + 1)
        text = raw[:ERROR_BODY_LIMIT].decode("utf-8", errors="replace")
        if key:
            text = text.replace(key, "[REDACTED]")
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", text)
        text = re.sub(r"\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
        evidence.update(body_available=bool(raw), body=text, truncated=len(raw) > ERROR_BODY_LIMIT)
    except Exception:
        evidence["read_error"] = True
    finally:
        exc.close()
    return evidence


def read_plan(path: Path = PLAN) -> dict:
    plan = json.loads(path.read_text())
    validate_plan(plan)
    return plan


def output_limit(plan: dict, model: dict) -> int:
    """The reviewed request limit, with no extra harness-wide token ceiling."""
    limits = plan["max_output_tokens"]
    return limits[model["id"]] if isinstance(limits, dict) else limits


def validate_plan(plan: dict) -> None:
    """Validate budgets while retaining finite request counts and schedules."""
    problems, models = plan["problems"], plan["models"]
    allowed = {"001_dot_product", "002_gemv", "003_matmul", "004_conv1d"}
    if not problems or len(set(problems)) != len(problems) or not set(problems) <= allowed:
        raise ValueError("invalid or duplicate problems")
    if not 1 <= len(models) <= 6 or len(models) * len(problems) > 12:
        raise ValueError("a batch may contain at most twelve requests across six models")
    for key, cap in [("max_prompt_bytes", 24000), ("request_timeout_s", 600)]:
        if type(plan[key]) is not int or not 0 < plan[key] <= cap:
            raise ValueError(f"invalid {key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", plan["name"]):
        raise ValueError("invalid batch name")
    ids = [m["id"] for m in models]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate model")
    mode = plan.get("output_budget", "fixed")
    limits = plan["max_output_tokens"]
    if mode == "provider_max":
        if not isinstance(limits, dict) or set(limits) != set(ids):
            raise ValueError("provider_max requires an explicit limit for every planned model")
        values = limits.values()
    elif mode == "fixed" and not isinstance(limits, dict):
        values = [limits]
    else:
        raise ValueError("invalid output budget mode")
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("max_output_tokens must contain positive integer provider limits")
    if type(plan.get("generation_enabled", True)) is not bool:
        raise ValueError("generation_enabled must be boolean")
    if type(plan.get("stream", False)) is not bool:
        raise ValueError("stream must be boolean")
    if type(plan.get("request_wall_timeout_s", 600)) is not int or not 0 < plan.get("request_wall_timeout_s", 600) <= 3600:
        raise ValueError("invalid request wall timeout")
    expires = datetime.fromisoformat(plan["expires_at"])
    if expires.tzinfo is None:
        raise ValueError("expiry must include a timezone")
    for model in models:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", model["id"]):
            raise ValueError("invalid model ID")
        if model["api"] not in {"chat/completions", "messages"}:
            raise ValueError("unsupported Go endpoint")
        if model.get("token_limit_key", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("invalid output limit field")
        if "reasoning" in model and model["reasoning"] not in ({"enabled": False}, {"enabled": True}):
            raise ValueError("unsupported normalized reasoning control")
        starts = datetime.fromisoformat(model["not_before"])
        if starts.tzinfo is None or not 0 < (expires - starts).total_seconds() <= 172800:
            raise ValueError("authorization window must be positive and at most two days")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def due(plan: dict, model: dict, now: datetime) -> bool:
    return plan.get("generation_enabled", True) and datetime.fromisoformat(model["not_before"]) <= now < datetime.fromisoformat(plan["expires_at"])


def select_model(plan: dict, model_id: str) -> dict:
    return next(m for m in plan["models"] if m["id"] == model_id)


def write_json(path: Path, data: dict) -> None:
    atomic_json(path, data)


def prompt_for(problem) -> str:
    baseline = json.loads(problem.baseline_metrics.read_text())
    # Single-shot callers have neither a filesystem nor tools. Do not reuse the
    # interactive workspace's instructions to run check.sh or inspect files.
    spec = render_problem_md(problem, baseline)
    spec = spec.replace("`dut.py` in this directory", "`dut.py` supplied below")
    spec = re.sub(r"## How to check your work\n.*?(?=## Rules)",
                  "## Evaluation\n\nThere are no development tools or repair turns in this single-shot track.\n\n", spec, flags=re.S)
    skeleton = render_skeleton(problem).replace("// Run ./check.sh for synthesis + simulation feedback.\n", "")
    return (
        spec
        + "\n\n## dut.py\n```python\n" + (problem.root / "dut.py").read_text()
        + "\n```\n\n## Starting dut.v\n```verilog\n" + skeleton + "\n```\n"
    )


def request_body(model: dict, prompt: str, plan: dict) -> dict:
    messages = [{"role": "user", "content": prompt}]
    body = {
        "model": model["id"], "messages": messages,
        model.get("token_limit_key", "max_tokens"): output_limit(plan, model), "stream": plan.get("stream", False),
    }
    for key in ("thinking", "reasoning_effort", "reasoning"):
        if key in model:
            body[key] = model[key]
    if model["api"] == "messages":
        body["system"] = SYSTEM
    else:
        messages.insert(0, {"role": "system", "content": SYSTEM})
        if body["stream"]:
            body["stream_options"] = {"include_usage":True}
    return body


def parse_response(data: dict, api: str) -> tuple[str, str, dict]:
    if api == "messages":
        text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        finish = data.get("stop_reason", "")
    else:
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        finish = choice.get("finish_reason", "")
    return text, finish, data.get("usage", {})


def response_diagnostics(data: dict, api: str) -> dict:
    """Keep field-level evidence even when provider token accounting is wrong."""
    if api == "messages":
        blocks = data.get("content", [])
        return {"content_block_types": [b.get("type") for b in blocks],
                "reasoning_chars": sum(len(b.get("thinking", "")) for b in blocks if b.get("type") == "thinking")}
    message = data["choices"][0]["message"]
    return {"message_fields": sorted(message),
            "answer_chars": len(message.get("content") or ""),
            "reasoning_chars": max(len(message.get(k) or "") for k in ("reasoning", "reasoning_content")),
            "has_reasoning_details": bool(message.get("reasoning_details"))}


def extract_rtl(text: str) -> str:
    blocks = re.findall(r"```(?:verilog|systemverilog|sv)?\s*\n(.*?)```", text, re.S | re.I)
    if blocks:
        if len(blocks) != 1:
            raise ValueError("expected one complete RTL source block")
        text = blocks[0]
    if not re.search(r"\bmodule\s+dut\b", text) or not re.search(r"\bendmodule\b", text):
        raise ValueError("response contains no complete dut module")
    return text.strip() + "\n"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Go credential to a different URL.
        return None


def call_model(model: dict, body: dict, key: str, session: str, timeout: int,
               *, evidence: Path | None = None, wall_timeout: int = 600) -> dict:
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json", "User-Agent": USER_AGENT,
        "x-opencode-session": session,
    }
    if model["api"] == "messages":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    if body.get("stream"):
        if evidence is None:
            raise ValueError("streaming requires a durable evidence directory")
        return call_streaming(model["api"], body, headers, evidence, key, timeout, wall_timeout)
    request = urllib.request.Request(BASE_URL + model["api"], json.dumps(body).encode(), headers)
    # urllib does not retry. Do not log headers or raw provider errors.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        return json.load(response)


def generate(plan: dict, model: dict, out: Path) -> None:
    validate_plan(plan)
    if model != select_model(plan, model["id"]):
        raise ValueError("model must match the reviewed plan")
    if not due(plan, model, now_utc()):
        raise RuntimeError("outside the model's authorized schedule window")
    key = os.environ.get("OPENCODE_GO_API_KEY", "")
    if not key:
        raise RuntimeError("OPENCODE_GO_API_KEY is missing")
    out.mkdir(parents=True, exist_ok=True)
    # Local guard complements the durable GitHub tag claim. Never overwrite a run.
    with (out / "generation-started.json").open("x") as handle:
        json.dump({"model": model["id"], "started": now_utc().isoformat()}, handle)
    write_json(out / "plan.json", plan)
    stop = ""
    # Pre-create every scheduled slot so a hard kill cannot erase later slots.
    for problem_id in plan["problems"]:
        write_json(out / ("opencode-go-" + model["id"]) / problem_id / "rep1/generation.json", {
            "model":model["id"], "problem":problem_id, "protocol":"single-shot",
            "started":now_utc().isoformat(), "max_output_tokens":output_limit(plan,model),
            "generation_settings":{k:model[k] for k in SETTING_KEYS if k in model},
            "github":{k:os.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")},
            "generation_state":"pending", "error":"not requested: generation job stopped before this request"})
    for problem_id in plan["problems"]:
        dest = out / ("opencode-go-" + model["id"]) / problem_id / "rep1"
        dest.mkdir(parents=True, exist_ok=True)
        record = {
            "model": model["id"], "problem": problem_id,
            "protocol": "single-shot", "started": now_utc().isoformat(),
            "max_output_tokens": output_limit(plan, model), "error": "",
            "generation_settings": {k: model[k] for k in SETTING_KEYS if k in model},
            "github": {k: os.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")},
        }
        if stop:
            record["error"] = "not requested: batch stopped after " + stop
            write_json(dest / "generation.json", record)
            continue
        problem = load_problem(repo_root() / "problems/level1" / problem_id)
        prompt = prompt_for(problem)
        (dest / "prompt.txt").write_text(SYSTEM + "\n\n" + prompt)
        prompt_bytes = len((SYSTEM + prompt).encode())
        record["prompt_bytes"] = prompt_bytes
        started = time.monotonic()
        requested = False
        try:
            if not due(plan, model, now_utc()):
                raise RuntimeError("schedule expired")
            if prompt_bytes > plan["max_prompt_bytes"]:
                raise RuntimeError("prompt exceeds size cap")
            session = f"adpbench-{plan['name']}-{model['id']}-{problem_id}"
            body = request_body(model, prompt, plan)
            write_json(dest / "request.json", body)
            if body["stream"]:
                record["transport"] = {"stream":True, "idle_timeout_s":plan["request_timeout_s"],
                                       "wall_timeout_s":plan.get("request_wall_timeout_s",600)}
            write_json(dest / "generation.json", {**record, "generation_state":"in_progress",
                       "incomplete_usage":True, "error":"generation interrupted before completion"})
            requested = True
            data = call_model(model, body, key, session, plan["request_timeout_s"],
                              evidence=dest, wall_timeout=plan.get("request_wall_timeout_s",600))
            # Raw model output is evidence, never executable commands. Never save credentials.
            safe_data = json.loads(json.dumps(data).replace(key, "[REDACTED]"))
            write_json(dest / "response.json", safe_data)
            text, finish, usage = parse_response(safe_data, model["api"])
            record.update({"finish_reason": finish, "usage": usage if isinstance(usage, dict) else {},
                           "response_id": data.get("id", ""), "response_model": data.get("model", "")})
            record["response_diagnostics"] = response_diagnostics(data, model["api"])
            (dest / "response.txt").write_text(text)
            if not usage:
                stop = record["error"] = "missing provider token accounting"
                record["incomplete_usage"] = True
            elif not valid_usage(usage, model["api"], output_limit(plan, model)):
                stop = record["error"] = "invalid provider token accounting or output cap exceeded"
                record["incomplete_usage"] = True
            if finish in {"length", "max_tokens"}:
                record["error"] = "generation reached output cap; no retry"
            elif finish not in {"stop", "end_turn", "stop_sequence"}:
                stop = record["error"] = "unexpected provider stop reason; no retry"
            elif not record["error"]:
                try:
                    (dest / "dut.v").write_text(extract_rtl(text))
                except ValueError as exc:
                    record["invalid_rtl"] = str(exc)
            if plan.get("stop_on_invalid_output") and (record["error"] or record.get("invalid_rtl")):
                stop = record["error"] or record["invalid_rtl"]
        except urllib.error.HTTPError as exc:
            stop = record["error"] = f"provider HTTP {exc.code}; no retry or fallback"
            write_json(dest / "provider_error.json", provider_error_evidence(exc, key))
        except StreamFailure as exc:
            stop = record["error"] = f"generation incomplete: {exc}; no retry or fallback"
            record["incomplete_usage"] = True
        except Exception as exc:
            # Exception bodies can contain sensitive server response details.
            stop = record["error"] = f"request stopped ({type(exc).__name__}); no retry"
            record["incomplete_usage"] = requested
        record["duration_s"] = round(time.monotonic() - started, 2)
        record["generation_state"] = "failed" if record["error"] or record.get("invalid_rtl") else "completed"
        write_json(dest / "generation.json", record)
        print(f"{problem_id}: {record['error'] or record.get('invalid_rtl') or 'generated'}", flush=True)
    if stop:
        raise RuntimeError(stop)


def valid_usage(usage: dict, api: str, cap: int) -> bool:
    if not isinstance(usage, dict):
        return False
    inputs = usage.get("input_tokens" if api == "messages" else "prompt_tokens")
    outputs = usage.get("output_tokens" if api == "messages" else "completion_tokens")
    return type(inputs) is int and inputs >= 0 and type(outputs) is int and 0 <= outputs <= cap


def score(plan: dict, model: dict, out: Path, *, problem_id: str | None = None,
          checkpoint_root: Path | None = None, resume: bool = False,
          deadline_s: float | None = None) -> None:
    if problem_id is not None and problem_id not in plan["problems"]:
        raise ValueError("problem is not in the reviewed plan")
    for current_problem in plan["problems"]:
        if problem_id is not None and current_problem != problem_id:
            continue
        selected = current_problem
        dest = out / ("opencode-go-" + model["id"]) / selected / "rep1"
        generation_path = dest / "generation.json"
        if not generation_path.exists():
            continue
        generation = json.loads(generation_path.read_text())
        agent_track = generation.get("protocol") == "agent-assisted-v1"
        if (dest / "record.json").exists() and not resume:
            raise FileExistsError("scoring record exists; use --resume or a fresh output directory")
        problem = load_problem(repo_root() / "problems/level1" / selected)
        record = RunRecord(
            problem=selected, agent_cmd=("adpbench Go agent-assisted-v1 (read/write/check/submit)" if agent_track else "adpbench Go API single-shot (one request, no repair)"),
            label=("scripted/baseline-fixture" if generation.get("protocol") == "fixture"
                   else "opencode-go/" + model["id"] + (" [agent-assisted-v1]" if agent_track else " [single-shot]")), group=plan["name"],
            started=generation["started"], duration_s=generation.get("duration_s", 0),
            error=generation.get("error", "") or generation.get("invalid_rtl", ""), sandbox="docker",
        )
        source = dest / "dut.v"
        frozen = None
        if source.exists():
            if agent_track and (generation.get("outcome") != "submitted" or
                                generation.get("submission_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()):
                raise ValueError("agent submission lacks a matching explicit submit receipt")
            frozen = dest.parent / "rep1_frozen/dut.v"
            frozen.parent.mkdir(exist_ok=True)
            if frozen.exists():
                if hashlib.sha256(frozen.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                    raise ValueError("frozen RTL differs from saved generation; refusing to overwrite")
            else:
                frozen.write_bytes(source.read_bytes())
            record.frozen = str(frozen)
            record.audit = audit_submission(frozen.read_text())
            if resume and (dest / "record.json").exists():
                previous = json.loads((dest / "record.json").read_text())
                if previous.get("result") is not None:
                    # A completed pass OR failure is immutable. Recovery only
                    # finishes interrupted work, never repairs a scored design.
                    print(f"{selected}: retaining completed scoring result", flush=True)
                    continue
            if record.audit["ok"] and not record.error:
                # A hard kill leaves a truthful, publishable incomplete record.
                record.manifest = build_manifest(problem, record, frozen, plan.get("slot_timeout_s", plan["request_timeout_s"]), "docker", "adpbench-go:ci", "none")
                record.manifest["generation"] = generation
                progress = json.loads(record.to_json())
                progress["error"] = "scoring interrupted before completion; see scoring checkpoints"
                if agent_track:
                    progress.update(outcome="scoring_interrupted", execution_health="failed")
                write_json(dest / "manifest.json", record.manifest)
                write_json(dest / "record.json", progress)
                write_json(dest / "scoring-state.json", {"state":"running"})
                try:
                    print(f"{selected}: scoring frozen RTL on held-out cases", flush=True)
                    checkpoint = checkpoint_root / model["id"] / selected if checkpoint_root else None
                    record.result = evaluate_multi(problem, [frozen], seeds=EVAL_SEEDS, source="go-agent-assisted-v1" if agent_track else "go-single-shot", tag="eval",
                                                   checkpoint_dir=checkpoint, resume=resume, deadline_s=deadline_s).to_dict()
                    failure = record.result.get("metadata", {}).get("failure_kind", "")
                    if failure in {"wall_timeout", "signal", "log_limit", "interrupted"} or failure.startswith("launch_error"):
                        record.error = f"scoring infrastructure: {failure}; inspect stage logs"
                except Exception as exc:
                    record.error = f"scoring failed: {type(exc).__name__}; see private scoring logs"
                    print(record.error, flush=True)
        else:
            record.audit = {"ok": False, "violations": [{"line": 0, "match": "", "reason": generation.get("invalid_rtl", "no RTL produced")}], "warnings": []}
        record.manifest = build_manifest(problem, record, frozen, plan.get("slot_timeout_s", plan["request_timeout_s"]), "docker", "adpbench-go:ci", "none")
        record.manifest["generation"] = generation
        write_json(dest / "manifest.json", record.manifest)
        payload = json.loads(record.to_json())
        if agent_track:
            payload.update(agent_outcome(payload, generation))
        write_json(dest / "record.json", payload)
        write_json(dest / "scoring-state.json", {"state":"completed", "has_result":record.result is not None})
        result = record.result or {}
        print(f"{selected}: scoring complete; correct={bool(result.get('correct'))}; "
              f"stage={(result.get('metadata') or {}).get('stage', 'no submission')}", flush=True)
    summarize(plan, model, out)


def agent_outcome(record: dict, generation: dict) -> dict:
    """Execution health is distinct from a legitimate failed measurement."""
    outcome = generation.get("outcome", "not_requested")
    if outcome == "submitted":
        if record.get("error"):
            outcome = "scoring_interrupted"
        elif not record.get("audit", {}).get("ok"):
            outcome = "audit_rejected"
        elif record.get("result") is None:
            outcome = "scoring_interrupted"
        else:
            outcome = "correct" if record["result"].get("correct") else "incorrect"
    failed_health = {"provider_error", "transport_interrupted", "harness_error", "scoring_interrupted", "not_requested", "interrupted"}
    return {"outcome": outcome, "execution_health": "failed" if outcome in failed_health else "completed"}


def summarize(plan: dict, model: dict, out: Path) -> None:
    total_input = total_output = 0
    incomplete_usage = False
    for problem_id in plan["problems"]:
        path = out / ("opencode-go-" + model["id"]) / problem_id / "rep1/generation.json"
        if not path.exists():
            # The agent workflow has one independent problem per artifact.
            # A sibling slot not assigned to this job is not missing usage.
            if plan.get("protocol") != "agent-assisted-v1":
                incomplete_usage = True
            continue
        generation = json.loads(path.read_text())
        incomplete_usage |= bool(generation.get("incomplete_usage"))
        usage = generation.get("usage", {})
        # Never turn unknown/invalid accounting into a fabricated numeric total.
        usage_cap = output_limit(plan, model) * (plan.get("max_turns", 1) if plan.get("protocol") == "agent-assisted-v1" else 1)
        if usage and valid_usage(usage, model["api"], usage_cap):
            total_input += usage.get("prompt_tokens", usage.get("input_tokens", 0))
            total_output += usage.get("completion_tokens", usage.get("output_tokens", 0))
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                value = usage.get(key, 0)
                if type(value) is int and value >= 0:
                    total_input += value
        elif usage:
            incomplete_usage = True
    write_report(out)
    tokens = {"input_tokens_including_cache": total_input, "output_tokens": total_output, "incomplete_usage":incomplete_usage}
    write_json(out / "usage.json", tokens)
    with (out / "REPORT.md").open("a") as handle:
        if plan.get("protocol") == "agent-assisted-v1":
            handle.write(f"\nProtocol: agent-assisted-v1; at most {plan['max_turns']} turns and {plan['max_checks']} development checks per slot. Explicit file submission, one held-out evaluation.\n")
            handle.write(f"\nProvider-reported input (including cache): {total_input}; output: {total_output}. Usage incomplete: {incomplete_usage}.\n")
            handle.write("\nSeparate track: do not combine with single-shot or pilot-001 scores.\n")
            return
        handle.write(f"\nProtocol: one generation per problem, no feedback or retries. Output cap: {output_limit(plan, model)} tokens/request ({plan.get('output_budget', 'fixed')}).\n")
        handle.write(f"\nProvider-reported input (including cache): {total_input}; output: {total_output} tokens.\n")
        handle.write(f"\nGeneration settings: `{json.dumps({k: model[k] for k in SETTING_KEYS if k in model})}`.\n")
        handle.write("\nThese single-shot results use a different generation budget from the iterative pilot-001.\n")
        if incomplete_usage:
            handle.write("\nToken totals are incomplete; an interrupted response may have consumed additional subscription quota.\n")


def save_scoring_failure(plan: dict, model: dict, out: Path, problem_id: str, reason: str) -> None:
    """Host-side crash receipt: unknown score, never fabricate a DUT failure/pass."""
    dest = out / ("opencode-go-" + model["id"]) / problem_id / "rep1"
    path = dest / "record.json"
    if path.exists():
        record = json.loads(path.read_text())
        if record.get("result") is not None:
            return  # Preserve a finalized score even if post-scoring cleanup failed.
    else:
        generation = json.loads((dest / "generation.json").read_text())
        problem = load_problem(repo_root() / "problems/level1" / problem_id)
        track = "agent-assisted-v1" if generation.get("protocol") == "agent-assisted-v1" else "single-shot"
        run = RunRecord(problem=problem_id, agent_cmd=f"adpbench Go API {track}",
                        label=f"opencode-go/{model['id']} [{track}]",
                        group=plan["name"], started=generation["started"], sandbox="docker")
        frozen = dest.parent / "rep1_frozen/dut.v"
        if not frozen.exists() and (dest / "dut.v").exists():
            frozen.parent.mkdir(exist_ok=True)
            frozen.write_bytes((dest / "dut.v").read_bytes())
        run.frozen = str(frozen) if frozen.exists() else ""
        run.manifest = build_manifest(problem, run, frozen if frozen.exists() else None,
                                      plan.get("slot_timeout_s", plan["request_timeout_s"]), "docker", "adpbench-go:ci", "none")
        run.manifest["generation"] = generation
        record = json.loads(run.to_json())
    record["error"] = f"scoring infrastructure: {reason}; score unknown; frozen generation preserved"
    if plan.get("protocol") == "agent-assisted-v1":
        record.update(outcome="scoring_interrupted", execution_health="failed")
    record["manifest"]["error"] = record["error"]
    write_json(dest / "manifest.json", record["manifest"])
    write_json(path, record)
    write_json(dest / "scoring-state.json", {"state":"interrupted", "reason":reason})


def gate(plan: dict, cron: str, now: datetime) -> dict:
    match = next((m for m in plan["models"] if m.get("cron") and m["cron"] == cron), None)
    return {"run": bool(match and due(plan, match, now)), "model": match["id"] if match else ""}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["gate", "generate", "score", "fixture"])
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--out", type=Path, default=Path("runs/go"))
    parser.add_argument("--problem", help="Score only this planned problem; no model calls")
    parser.add_argument("--checkpoint-root", type=Path)
    parser.add_argument("--resume", action="store_true", help="Reuse hash-verified scoring checkpoints; never regenerate")
    parser.add_argument("--deadline-s", type=float, help="Soft scoring wall deadline, below the outer job timeout")
    args = parser.parse_args()
    plan = read_plan(args.plan)
    if args.mode == "gate":
        result = gate(plan, os.environ.get("SCHEDULE", ""), now_utc())
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            for k, v in result.items():
                handle.write(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n")
        print(json.dumps(result))
        return
    model = select_model(plan, args.model)
    if args.mode == "generate":
        generate(plan, model, args.out)
    elif args.mode == "score":
        score(plan, model, args.out, problem_id=args.problem, checkpoint_root=args.checkpoint_root,
              resume=args.resume, deadline_s=args.deadline_s)
    else:
        # No HTTP calls: exercise the full scoring path with known-good RTL.
        for problem_id in plan["problems"]:
            dest = args.out / ("opencode-go-" + model["id"]) / problem_id / "rep1"
            write_json(dest / "generation.json", {"started": now_utc().isoformat(), "protocol": "fixture", "error": ""})
            (dest / "dut.v").write_bytes((repo_root() / "problems/level1" / problem_id / "baseline.v").read_bytes())
        score(plan, model, args.out)
        report = json.loads((args.out / "report.json").read_text())
        if report["totals"]["correct"] != len(plan["problems"]):
            raise RuntimeError("offline baseline fixture failed")
        print(f"All {len(plan['problems'])} offline baseline fixtures passed. No Go requests made.")


if __name__ == "__main__":
    main()
