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


def read_plan() -> dict:
    return json.loads(PLAN.read_text())


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
        "max_tokens": plan["max_output_tokens"], "stream": False,
    }
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
            data = call_model(model, request_body(model, prompt, plan), key, session, plan["request_timeout_s"])
            text, finish, usage = parse_response(data, model["api"])
            record.update({"finish_reason": finish, "usage": usage, "response_id": data.get("id", "")})
            (dest / "response.txt").write_text(text)
            if not usage:
                stop = "missing provider token accounting"
            if finish in {"length", "max_tokens"}:
                record["error"] = "generation reached output cap; no retry"
            else:
                try:
                    (dest / "dut.v").write_text(extract_rtl(text))
                except ValueError as exc:
                    record["invalid_rtl"] = str(exc)
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
            error=generation.get("error", ""), sandbox="docker",
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
                    record.result = evaluate_multi(problem, [frozen], seeds=EVAL_SEEDS, source="go-single-shot", tag="eval").to_dict()
                except Exception as exc:
                    record.error = f"scoring failed: {type(exc).__name__}: {exc}"
        else:
            record.audit = {"ok": False, "violations": [{"line": 0, "match": "", "reason": generation.get("invalid_rtl", "no RTL produced")}], "warnings": []}
        record.manifest = build_manifest(problem, record, frozen, plan["request_timeout_s"], "docker", "adpbench-go:ci", "none")
        record.manifest["generation"] = generation
        write_json(dest / "manifest.json", record.manifest)
        write_json(dest / "record.json", json.loads(record.to_json()))
    write_report(out)
    tokens = {"input_tokens_including_cache": total_input, "output_tokens": total_output}
    write_json(out / "usage.json", tokens)
    with (out / "REPORT.md").open("a") as handle:
        handle.write(f"\nProtocol: one generation per problem, no feedback or retries. Output cap: {plan['max_output_tokens']} tokens/request.\n")
        handle.write(f"\nProvider-reported input (including cache): {total_input}; output: {total_output} tokens.\n")
        handle.write("\nThese single-shot results use a different generation budget from the iterative pilot-001.\n")


def gate(plan: dict, cron: str, now: datetime) -> dict:
    match = next((m for m in plan["models"] if m["cron"] == cron), None)
    return {"run": bool(match and due(plan, match, now)), "model": match["id"] if match else ""}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["gate", "generate", "score", "fixture"])
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--out", type=Path, default=Path("runs/go"))
    args = parser.parse_args()
    plan = read_plan()
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
        print("All four offline baseline fixtures passed. No Go requests made.")


if __name__ == "__main__":
    main()
