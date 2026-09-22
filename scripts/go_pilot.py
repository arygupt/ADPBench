"""Finite, single-shot Go pilot. Generation and offline scoring are separate.

No model-supplied commands are executed. Only RTL is accepted, audited, and
scored in the pinned, network-disabled toolchain container. Credentials exist
only in the generation step's environment; requests are never retried.
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

from adpbench.agent import RunRecord, audit_submission, build_manifest, render_problem_md, render_skeleton
from adpbench.evaluate import EVAL_SEEDS, evaluate_multi
from adpbench.problem import load_problem, repo_root
from adpbench.report import write_report

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


def read_plan(path: Path = PLAN) -> dict:
    plan = json.loads(path.read_text())
    validate_plan(plan)
    return plan


def validate_plan(plan: dict) -> None:
    """Keep manually dispatched screens finite even if a plan is edited."""
    problems, models = plan["problems"], plan["models"]
    allowed = {"001_dot_product", "002_gemv", "003_matmul", "004_conv1d"}
    if not problems or len(set(problems)) != len(problems) or not set(problems) <= allowed:
        raise ValueError("invalid or duplicate problems")
    if not 1 <= len(models) <= 6 or len(models) * len(problems) > 12:
        raise ValueError("a batch may contain at most twelve requests across six models")
    for key, cap in [("max_output_tokens", 8192), ("max_prompt_bytes", 24000), ("request_timeout_s", 600)]:
        if type(plan[key]) is not int or not 0 < plan[key] <= cap:
            raise ValueError(f"invalid {key}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", plan["name"]):
        raise ValueError("invalid batch name")
    ids = [m["id"] for m in models]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate model")
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
    return datetime.fromisoformat(model["not_before"]) <= now < datetime.fromisoformat(plan["expires_at"])


def select_model(plan: dict, model_id: str) -> dict:
    return next(m for m in plan["models"] if m["id"] == model_id)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def prompt_for(problem) -> str:
    baseline = json.loads(problem.baseline_metrics.read_text())
    return (
        render_problem_md(problem, baseline)
        + "\n\n## dut.py\n```python\n" + (problem.root / "dut.py").read_text()
        + "\n```\n\n## Starting dut.v\n```verilog\n" + render_skeleton(problem) + "\n```\n"
    )


def request_body(model: dict, prompt: str, plan: dict) -> dict:
    messages = [{"role": "user", "content": prompt}]
    body = {
        "model": model["id"], "messages": messages,
        model.get("token_limit_key", "max_tokens"): plan["max_output_tokens"], "stream": False,
    }
    for key in ("thinking", "reasoning_effort", "reasoning"):
        if key in model:
            body[key] = model[key]
    if model["api"] == "messages":
        body["system"] = SYSTEM
    else:
        messages.insert(0, {"role": "system", "content": SYSTEM})
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


def call_model(model: dict, body: dict, key: str, session: str, timeout: int) -> dict:
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json", "User-Agent": USER_AGENT,
        "x-opencode-session": session,
    }
    if model["api"] == "messages":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
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
    for problem_id in plan["problems"]:
        dest = out / ("opencode-go-" + model["id"]) / problem_id / "rep1"
        dest.mkdir(parents=True, exist_ok=True)
        record = {
            "model": model["id"], "problem": problem_id,
            "protocol": "single-shot", "started": now_utc().isoformat(),
            "max_output_tokens": plan["max_output_tokens"], "error": "",
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
        try:
            if not due(plan, model, now_utc()):
                raise RuntimeError("schedule expired")
            if prompt_bytes > plan["max_prompt_bytes"]:
                raise RuntimeError("prompt exceeds size cap")
            session = f"adpbench-{plan['name']}-{model['id']}-{problem_id}"
            body = request_body(model, prompt, plan)
            write_json(dest / "request.json", body)
            data = call_model(model, body, key, session, plan["request_timeout_s"])
            # Raw model output is evidence, never executable commands. Never save credentials.
            safe_data = json.loads(json.dumps(data).replace(key, "[REDACTED]"))
            write_json(dest / "response.json", safe_data)
            text, finish, usage = parse_response(data, model["api"])
            record.update({"finish_reason": finish, "usage": usage, "response_id": data.get("id", ""), "response_model": data.get("model", "")})
            record["response_diagnostics"] = response_diagnostics(data, model["api"])
            (dest / "response.txt").write_text(text)
            if not usage:
                stop = record["error"] = "missing provider token accounting"
            elif not valid_usage(usage, model["api"], plan["max_output_tokens"]):
                stop = record["error"] = "invalid provider token accounting or output cap exceeded"
            if finish in {"length", "max_tokens"}:
                record["error"] = "generation reached output cap; no retry"
            elif not record["error"]:
                try:
                    (dest / "dut.v").write_text(extract_rtl(text))
                except ValueError as exc:
                    record["invalid_rtl"] = str(exc)
            if plan.get("stop_on_invalid_output") and (record["error"] or record.get("invalid_rtl")):
                stop = record["error"] or record["invalid_rtl"]
        except urllib.error.HTTPError as exc:
            stop = record["error"] = f"provider HTTP {exc.code}; no retry or fallback"
        except Exception as exc:
            # Exception bodies can contain sensitive server response details.
            stop = record["error"] = f"request stopped ({type(exc).__name__}); no retry"
        record["duration_s"] = round(time.monotonic() - started, 2)
        write_json(dest / "generation.json", record)
        print(f"{problem_id}: {record['error'] or record.get('invalid_rtl') or 'generated'}", flush=True)
    if stop:
        raise RuntimeError(stop)


def valid_usage(usage: dict, api: str, cap: int) -> bool:
    inputs = usage.get("input_tokens" if api == "messages" else "prompt_tokens")
    outputs = usage.get("output_tokens" if api == "messages" else "completion_tokens")
    return type(inputs) is int and inputs >= 0 and type(outputs) is int and 0 <= outputs <= cap


def score(plan: dict, model: dict, out: Path) -> None:
    total_input = total_output = 0
    for problem_id in plan["problems"]:
        dest = out / ("opencode-go-" + model["id"]) / problem_id / "rep1"
        generation_path = dest / "generation.json"
        if not generation_path.exists():
            continue
        generation = json.loads(generation_path.read_text())
        usage = generation.get("usage", {})
        total_input += usage.get("prompt_tokens", usage.get("input_tokens", 0))
        total_output += usage.get("completion_tokens", usage.get("output_tokens", 0))
        total_input += usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        problem = load_problem(repo_root() / "problems/level1" / problem_id)
        record = RunRecord(
            problem=problem_id, agent_cmd="adpbench Go API single-shot (one request, no repair)",
            label=("scripted/baseline-fixture" if generation.get("protocol") == "fixture"
                   else "opencode-go/" + model["id"] + " [single-shot]"), group=plan["name"],
            started=generation["started"], duration_s=generation.get("duration_s", 0),
            error=generation.get("error", "") or generation.get("invalid_rtl", ""), sandbox="docker",
        )
        source = dest / "dut.v"
        frozen = None
        if source.exists():
            frozen = dest.parent / "rep1_frozen/dut.v"
            frozen.parent.mkdir(exist_ok=True)
            frozen.write_bytes(source.read_bytes())
            record.frozen = str(frozen)
            record.audit = audit_submission(frozen.read_text())
            if record.audit["ok"]:
                try:
                    print(f"{problem_id}: scoring frozen RTL on held-out cases", flush=True)
                    record.result = evaluate_multi(problem, [frozen], seeds=EVAL_SEEDS, source="go-single-shot", tag="eval").to_dict()
                except Exception as exc:
                    record.error = f"scoring failed: {type(exc).__name__}: {exc}"
        else:
            record.audit = {"ok": False, "violations": [{"line": 0, "match": "", "reason": generation.get("invalid_rtl", "no RTL produced")}], "warnings": []}
        record.manifest = build_manifest(problem, record, frozen, plan["request_timeout_s"], "docker", "adpbench-go:ci", "none")
        record.manifest["generation"] = generation
        write_json(dest / "manifest.json", record.manifest)
        write_json(dest / "record.json", json.loads(record.to_json()))
        result = record.result or {}
        print(f"{problem_id}: scoring complete; correct={bool(result.get('correct'))}; "
              f"stage={(result.get('metadata') or {}).get('stage', 'no submission')}", flush=True)
    write_report(out)
    tokens = {"input_tokens_including_cache": total_input, "output_tokens": total_output}
    write_json(out / "usage.json", tokens)
    with (out / "REPORT.md").open("a") as handle:
        handle.write(f"\nProtocol: one generation per problem, no feedback or retries. Output cap: {plan['max_output_tokens']} tokens/request.\n")
        handle.write(f"\nProvider-reported input (including cache): {total_input}; output: {total_output} tokens.\n")
        handle.write(f"\nGeneration settings: `{json.dumps({k: model[k] for k in SETTING_KEYS if k in model})}`.\n")
        handle.write("\nThese single-shot results use a different generation budget from the iterative pilot-001.\n")


def gate(plan: dict, cron: str, now: datetime) -> dict:
    match = next((m for m in plan["models"] if m.get("cron") and m["cron"] == cron), None)
    return {"run": bool(match and due(plan, match, now)), "model": match["id"] if match else ""}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["gate", "generate", "score", "fixture"])
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--out", type=Path, default=Path("runs/go"))
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
        score(plan, model, args.out)
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
