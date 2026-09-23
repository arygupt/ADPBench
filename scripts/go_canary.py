"""One finite, subscription-only compatibility diagnostic; never a scored run.

Uses the production request builder and streaming transport with a tiny coding
task. No reconnects, changed-setting retries, model fallback, or benchmark calls.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
from datetime import datetime
from pathlib import Path

from adpbench.problem import repo_root
from scripts.go_pilot import (call_model, extract_rtl, now_utc, parse_response, provider_error_evidence,
                             read_plan, request_body, response_diagnostics, valid_usage, write_json)
from scripts.go_stream import StreamFailure

CONFIG = repo_root() / "pilot/go-canary-glm-minimax-20260923-01.json"
SOURCE_PLAN = "pilot/go-core-provider-max-20260922.json"
PROMPT = ("Return only complete synthesizable SystemVerilog for a combinational XOR: "
          "module dut(input wire a, input wire b, output wire y). Assign y = a ^ b. "
          "No explanation, testbench, tools, or additional modules.")
COMPATIBLE = {"completed", "accepted_output_cap"}


def validate(config: dict) -> dict:
    if (config.get("schema_version") != 1 or config.get("kind") != "provider-compatibility-diagnostic"
            or config.get("source_plan") != SOURCE_PLAN
            or not re.fullmatch(r"go-compatibility-[a-z0-9-]+", config.get("name", ""))):
        raise ValueError("invalid canary identity")
    source = read_plan(repo_root() / SOURCE_PLAN)
    ids = config["models"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 6 or len(set(ids)) != len(ids)
            or not set(ids) <= {m["id"] for m in source["models"]}):
        raise ValueError("unreviewed or duplicate canary models")
    complete = config.get("require_complete_answer", False)
    if type(complete) is not bool:
        raise ValueError("invalid completion requirement")
    if complete and not set(ids) <= {"glm-5.3-flash", "minimax-m2.7"}:
        raise ValueError("larger diagnostic budgets are scoped to GLM and MiniMax")
    for field, limit in [("max_output_tokens",4096 if complete else 128), ("max_prompt_bytes",2048),
                         ("idle_timeout_s",30), ("wall_timeout_s",90)]:
        if type(config.get(field)) is not int or not 0 < config[field] <= limit:
            raise ValueError(f"invalid {field}")
    start, end = (datetime.fromisoformat(config[k]) for k in ("not_before", "expires_at"))
    if start.tzinfo is None or end.tzinfo is None or not 0 < (end-start).total_seconds() <= 86400:
        raise ValueError("canary authorization must be timezone-aware and at most one day")
    return source


def check_window(config: dict) -> None:
    if not datetime.fromisoformat(config["not_before"]) <= now_utc() < datetime.fromisoformat(config["expires_at"]):
        raise RuntimeError("outside the authorized canary window")


def summarize(out: Path, config: dict, records: list[dict]) -> dict:
    requested = [r for r in records if r["status"] != "not_requested"]
    tokens = sum(r.get("usage", {}).get("completion_tokens", r.get("usage", {}).get("output_tokens", 0))
                 for r in records if r.get("accounting_valid"))
    summary = {"kind":config["kind"], "name":config["name"], "configured_requests":len(records),
               "requests_started":len(requested), "maximum_output_tokens":len(records)*config["max_output_tokens"],
               "reported_output_tokens":tokens,
               "incomplete_usage":any(not r.get("accounting_valid") for r in requested),
               "benchmark_results":False, "require_complete_answer":config.get("require_complete_answer",False),
               "models":records}
    write_json(out / "summary.json", summary)
    lines = ["# OpenCode Go compatibility canary", "",
             "Diagnostic only: no benchmark scores, no retries, no fallback, no full batch.", "",
             "| Model | API | Status | Reported output tokens | Finish |",
             "| --- | --- | --- | ---: | --- |"]
    for r in records:
        usage = r.get("usage", {})
        tokens = usage.get("completion_tokens",usage.get("output_tokens","unknown")) if r.get("accounting_valid") else "unknown"
        # Status/model are harness-controlled; provider text never enters this summary.
        finish = r.get("finish_reason", "")
        finish = finish if finish in {"stop","end_turn","stop_sequence","length","max_tokens"} else "—"
        lines.append(f"| {r['model']} | {r['api']} | {r['status']} | {tokens} | {finish} |")
    lines += ["", f"Reported output tokens: {summary['reported_output_tokens']}. Accounting incomplete: {summary['incomplete_usage']}.",
              "", ("This follow-up requires a completed answer; exhausting the cap is a failure. "
                     if config.get("require_complete_answer") else
                     "A tiny-cap response can end at its cap and still demonstrate valid streaming. ") +
              "This does not validate full RTL tasks or provider-maximum token limits.", ""]
    (out / "REPORT.md").write_text("\n".join(lines))
    return summary


def run(config: dict, out: Path, *, subscription_only: bool) -> dict:
    source = validate(config)
    check_window(config)
    if not subscription_only:
        raise RuntimeError("requires the user's Use balance OFF confirmation")
    key = os.environ.get("OPENCODE_GO_API_KEY", "")
    if not key:
        raise RuntimeError("missing Go credential")
    models = {m["id"]:m for m in source["models"]}
    plan = {**source, "output_budget":"fixed", "max_output_tokens":config["max_output_tokens"], "stream":True}
    # Validate every request before spending any tokens.
    bodies = {model_id:request_body(models[model_id], PROMPT, plan) for model_id in config["models"]}
    if any(len(json.dumps(b).encode()) > config["max_prompt_bytes"] for b in bodies.values()):
        raise ValueError("canary request exceeds the prompt size limit")
    out.mkdir(parents=True, exist_ok=True)
    with (out / "started.json").open("x") as handle:
        json.dump({"name":config["name"], "started":now_utc().isoformat()},handle)
    write_json(out / "plan.json", config)
    records = [{"model":m,"api":models[m]["api"],"status":"not_requested"} for m in config["models"]]
    summarize(out,config,records)
    stop = False
    for record in records:
        if stop:
            break
        check_window(config)
        model = models[record["model"]]
        dest = out / model["id"]
        write_json(dest / "request.json", bodies[model["id"]])
        record.update(status="interrupted", started=now_utc().isoformat(),
                      max_output_tokens=config["max_output_tokens"],
                      github={k:os.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA")})
        write_json(dest / "record.json",record)
        summarize(out,config,records)
        start = time.monotonic()
        try:
            data = call_model(model,bodies[model["id"]],key,f"adpbench-{config['name']}-{model['id']}",
                              config["idle_timeout_s"],evidence=dest,wall_timeout=config["wall_timeout_s"])
            safe = json.loads(json.dumps(data).replace(key,"[REDACTED]"))
            write_json(dest / "response.json",safe)
            text, finish, usage = parse_response(safe,model["api"])
            accounting = valid_usage(usage,model["api"],config["max_output_tokens"])
            record.update(finish_reason=finish, accounting_valid=accounting,
                          usage=usage if accounting else {}, answer_present=bool(text.strip()),
                          diagnostics=response_diagnostics(safe,model["api"]))
            if not accounting:
                record["status"] = "invalid_accounting"
                stop = True  # Do not continue spending against unknown accounting.
            elif finish in {"length","max_tokens"}:
                record["status"] = "output_cap_incomplete" if config.get("require_complete_answer") else "accepted_output_cap"
            elif finish in {"stop","end_turn","stop_sequence"} and text.strip():
                record["status"] = "completed"
                if config.get("require_complete_answer"):
                    try:
                        rtl = extract_rtl(text)
                    except ValueError:
                        record["status"] = "invalid_rtl_answer"
                    else:
                        (dest / "dut.v").write_text(rtl)
            else:
                record["status"] = "unexpected_response"
        except urllib.error.HTTPError as exc:
            record.update(status=f"http_{exc.code}", http_status=exc.code)
            write_json(dest / "provider_error.json",provider_error_evidence(exc,key))
            stop = exc.code in {401,402,403,429}  # Auth/quota/rate errors stop the whole canary.
        except StreamFailure as exc:
            record.update(status="stream_error", error=str(exc))
        except Exception as exc:
            record.update(status="transport_error", error=type(exc).__name__)
            stop = True
        record["duration_s"] = round(time.monotonic()-start,3)
        write_json(dest / "record.json",record)
        summarize(out,config,records)
        print(f"{model['id']}: {record['status']}",flush=True)
    return summarize(out,config,records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan",type=Path,default=CONFIG)
    parser.add_argument("--out",type=Path,default=Path("runs/canary"))
    parser.add_argument("--subscription-only",action="store_true")
    args = parser.parse_args()
    result = run(json.loads(args.plan.read_text()),args.out,subscription_only=args.subscription_only)
    if any(r["status"] not in COMPATIBLE for r in result["models"]):
        raise SystemExit("Compatibility checks failed or were not requested; see saved diagnostics")


if __name__ == "__main__":
    main()
