"""One finite, subscription-only provider compatibility check. Never a scored run.

    python -m scripts.go_canary --plan <canary config> --out <dir> --subscription-only

Sends one tiny coding request per configured model, using the production
request builder and streaming transport, to check that each model accepts the
reviewed request settings. No reconnects, changed-setting retries, model
fallback, or benchmark calls.
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

from adpbench.durable import atomic_json
from adpbench.problem import repo_root
from scripts.go_pilot import (
    CAP_FINISHES,
    COMPLETE_FINISHES,
    call_model,
    extract_rtl,
    github_context,
    now_utc,
    parse_response,
    provider_error_evidence,
    read_plan,
    redact_key,
    request_body,
    response_diagnostics,
    valid_usage,
)
from scripts.go_stream import StreamFailure

CONFIG = repo_root() / "pilot/go-canary-high-reasoning-20260924.json"
SOURCE_PLANS = ("pilot/go-core-provider-max-20260922.json", "pilot/go-agent-high-20260924.json")
CANARY_NAME = re.compile(r"go-compatibility-[a-z0-9-]+")

# Output-token caps per kind of check. Anthropic-style APIs reject a thinking
# budget at or above max_tokens, so checking reasoning settings needs a cap
# above the plan's 16,000-token budget.
COMPATIBILITY_CAP = 128
COMPLETE_ANSWER_CAP = 4096
REASONING_CHECK_CAP = 20000
COMPLETE_ANSWER_MODELS = {"glm-5.3-flash", "minimax-m2.7"}
MAX_WINDOW_S = 24 * 3600

# Provider errors that mean no further request can succeed (auth, quota, rate).
STOP_ON_HTTP = {401, 402, 403, 429}
COMPATIBLE = {"completed", "accepted_output_cap"}

PROMPT = (
    "Return only complete synthesizable SystemVerilog for a combinational XOR: "
    "module dut(input wire a, input wire b, output wire y). Assign y = a ^ b. "
    "No explanation, testbench, tools, or additional modules."
)


def validate(config: dict) -> dict:
    """Check the canary config against its source plan. Returns the source plan."""
    identity_ok = (
        config.get("schema_version") == 1
        and config.get("kind") == "provider-compatibility-diagnostic"
        and config.get("source_plan") in SOURCE_PLANS
        and config.get("check", "compatibility") in {"compatibility", "reasoning_settings"}
        and CANARY_NAME.fullmatch(config.get("name", ""))
    )
    if not identity_ok:
        raise ValueError("invalid canary identity")
    source = read_plan(repo_root() / config["source_plan"])

    model_ids = config["models"]
    reviewed = {model["id"] for model in source["models"]}
    if (
        not isinstance(model_ids, list)
        or not 1 <= len(model_ids) <= 6
        or len(set(model_ids)) != len(model_ids)
        or not set(model_ids) <= reviewed
    ):
        raise ValueError("unreviewed or duplicate canary models")

    complete = config.get("require_complete_answer", False)
    if type(complete) is not bool:
        raise ValueError("invalid completion requirement")
    if complete and not set(model_ids) <= COMPLETE_ANSWER_MODELS:
        raise ValueError("larger diagnostic budgets are scoped to GLM and MiniMax")
    reasoning_check = config.get("check") == "reasoning_settings"
    if reasoning_check and complete:
        raise ValueError("reasoning-settings checks only verify that requests are accepted")

    if reasoning_check:
        output_cap = REASONING_CHECK_CAP
    elif complete:
        output_cap = COMPLETE_ANSWER_CAP
    else:
        output_cap = COMPATIBILITY_CAP
    limits = {
        "max_output_tokens": output_cap,
        "max_prompt_bytes": 2048,
        "idle_timeout_s": 30,
        "wall_timeout_s": 90,
    }
    for field, limit in limits.items():
        value = config.get(field)
        if type(value) is not int or not 0 < value <= limit:
            raise ValueError(f"invalid {field}")

    start = datetime.fromisoformat(config["not_before"])
    end = datetime.fromisoformat(config["expires_at"])
    window = (end - start).total_seconds()
    if start.tzinfo is None or end.tzinfo is None or not 0 < window <= MAX_WINDOW_S:
        raise ValueError("canary authorization must be timezone-aware and at most one day")
    return source


def check_window(config: dict) -> None:
    start = datetime.fromisoformat(config["not_before"])
    end = datetime.fromisoformat(config["expires_at"])
    if not start <= now_utc() < end:
        raise RuntimeError("outside the authorized canary window")


def run(config: dict, out: Path, *, subscription_only: bool) -> dict:
    """Send one request per configured model, recording each outcome. Returns the summary."""
    source = validate(config)
    check_window(config)
    if not subscription_only:
        raise RuntimeError("requires the user's Use balance OFF confirmation")
    key = os.environ.get("OPENCODE_GO_API_KEY", "")
    if not key:
        raise RuntimeError("missing Go credential")

    models = {model["id"]: model for model in source["models"]}
    plan = {
        **source,
        "output_budget": "fixed",
        "max_output_tokens": config["max_output_tokens"],
        "stream": True,
    }
    # Build and size-check every request before spending any tokens.
    bodies = {model_id: request_body(models[model_id], PROMPT, plan) for model_id in config["models"]}
    if any(len(json.dumps(body).encode()) > config["max_prompt_bytes"] for body in bodies.values()):
        raise ValueError("canary request exceeds the prompt size limit")

    out.mkdir(parents=True, exist_ok=True)
    # Never run twice into one directory.
    with (out / "started.json").open("x") as handle:
        json.dump({"name": config["name"], "started": now_utc().isoformat()}, handle)
    atomic_json(out / "plan.json", config)

    records = [
        {"model": model_id, "api": models[model_id]["api"], "status": "not_requested"}
        for model_id in config["models"]
    ]
    summarize(out, config, records)
    for record in records:
        check_window(config)
        model = models[record["model"]]
        dest = out / model["id"]
        atomic_json(dest / "request.json", bodies[model["id"]])
        # Until the response arrives, the saved state says "interrupted".
        record.update(
            status="interrupted",
            started=now_utc().isoformat(),
            max_output_tokens=config["max_output_tokens"],
            github=github_context(),
        )
        atomic_json(dest / "record.json", record)
        summarize(out, config, records)

        stop = _request_one(config, model, bodies[model["id"]], record, dest, key)
        summarize(out, config, records)
        print(f"{model['id']}: {record['status']}", flush=True)
        if stop:
            break
    return summarize(out, config, records)


def _request_one(config: dict, model: dict, body: dict, record: dict, dest: Path, key: str) -> bool:
    """Send one request and fill in `record`. Returns True if no further requests should be sent."""
    started = time.monotonic()
    stop = False
    try:
        data = call_model(
            model,
            body,
            key,
            f"adpbench-{config['name']}-{model['id']}",
            config["idle_timeout_s"],
            evidence=dest,
            wall_timeout=config["wall_timeout_s"],
        )
        safe = redact_key(data, key)
        atomic_json(dest / "response.json", safe)
        text, finish, usage = parse_response(safe, model["api"])
        accounting_valid = valid_usage(usage, model["api"], config["max_output_tokens"])
        record.update(
            finish_reason=finish,
            accounting_valid=accounting_valid,
            usage=usage if accounting_valid else {},
            answer_present=bool(text.strip()),
            diagnostics=response_diagnostics(safe, model["api"]),
        )
        record["status"] = _response_status(config, finish, text, accounting_valid, dest)
        # Do not keep spending against unknown accounting.
        stop = not accounting_valid
    except urllib.error.HTTPError as exc:
        record.update(status=f"http_{exc.code}", http_status=exc.code)
        atomic_json(dest / "provider_error.json", provider_error_evidence(exc, key))
        stop = exc.code in STOP_ON_HTTP
    except StreamFailure as exc:
        record.update(status="stream_error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        record.update(status="transport_error", error=type(exc).__name__)
        stop = True

    record["duration_s"] = round(time.monotonic() - started, 3)
    atomic_json(dest / "record.json", record)
    return stop


def _response_status(config: dict, finish: str, text: str, accounting_valid: bool, dest: Path) -> str:
    require_complete = config.get("require_complete_answer")
    if not accounting_valid:
        return "invalid_accounting"
    if finish in CAP_FINISHES:
        return "output_cap_incomplete" if require_complete else "accepted_output_cap"
    if finish not in COMPLETE_FINISHES or not text.strip():
        return "unexpected_response"
    if require_complete:
        try:
            rtl = extract_rtl(text)
        except ValueError:
            return "invalid_rtl_answer"
        (dest / "dut.v").write_text(rtl)
    return "completed"


def summarize(out: Path, config: dict, records: list[dict]) -> dict:
    """Write summary.json and REPORT.md. Provider text never enters either file."""
    requested = [record for record in records if record["status"] != "not_requested"]
    reported_output = sum(
        _output_tokens(record.get("usage", {}), 0) for record in records if record.get("accounting_valid")
    )
    complete = config.get("require_complete_answer", False)
    summary = {
        "kind": config["kind"],
        "name": config["name"],
        "configured_requests": len(records),
        "requests_started": len(requested),
        "maximum_output_tokens": len(records) * config["max_output_tokens"],
        "reported_output_tokens": reported_output,
        "incomplete_usage": any(not record.get("accounting_valid") for record in requested),
        "benchmark_results": False,
        "require_complete_answer": complete,
        "models": records,
    }
    atomic_json(out / "summary.json", summary)

    lines = [
        "# OpenCode Go compatibility canary",
        "",
        "Diagnostic only: no benchmark scores, no retries, no fallback, no full batch.",
        "",
        "| Model | API | Status | Reported output tokens | Finish |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for record in records:
        if record.get("accounting_valid"):
            tokens = _output_tokens(record.get("usage", {}), "unknown")
        else:
            tokens = "unknown"
        # Only harness-controlled values appear in this table.
        finish = record.get("finish_reason", "")
        if finish not in COMPLETE_FINISHES | CAP_FINISHES:
            finish = "—"
        lines.append(f"| {record['model']} | {record['api']} | {record['status']} | {tokens} | {finish} |")

    if complete:
        scope = "This follow-up requires a completed answer; exhausting the cap is a failure. "
    else:
        scope = "A tiny-cap response can end at its cap and still demonstrate valid streaming. "
    lines += [
        "",
        f"Reported output tokens: {summary['reported_output_tokens']}. "
        f"Accounting incomplete: {summary['incomplete_usage']}.",
        "",
        scope + "This does not validate full RTL tasks or provider-maximum token limits.",
        "",
    ]
    (out / "REPORT.md").write_text("\n".join(lines))
    return summary


def _output_tokens(usage: dict, default):
    return usage.get("completion_tokens", usage.get("output_tokens", default))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=CONFIG)
    parser.add_argument("--out", type=Path, default=Path("runs/canary"))
    parser.add_argument("--subscription-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.plan.read_text())
    result = run(config, args.out, subscription_only=args.subscription_only)
    if any(record["status"] not in COMPATIBLE for record in result["models"]):
        raise SystemExit("Compatibility checks failed or were not requested; see saved diagnostics")


if __name__ == "__main__":
    main()
