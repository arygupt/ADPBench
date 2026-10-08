"""Publish agent-assisted results bound to their source run - never transcripts or tool contents.

Used by publish_results.py for runs of the agent workflow. Each (model,
problem) slot ran as its own pair of Actions jobs ("Generate ..." and
"Evaluate ...") and uploaded its own artifacts.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from adpbench.durable import atomic_json
from adpbench.hashing import sha256_bytes
from scripts.go_pilot import (
    AGENT_PROTOCOLS,
    SETTING_KEYS,
    frozen_path,
    output_limit,
    plan_attempt,
    plan_slots,
    slot_dir,
)
from scripts.publish_go import (
    FINISHED_JOB_CONCLUSIONS,
    REPOSITORY,
    execution_evidence,
    export_leaderboard,
    output_tokens,
    save_leaderboard,
    write_published_records,
)

WORKFLOW = ".github/workflows/go-agent.yml"
AGENT_GENERATION_STEP = "Run standardized agent"
GITHUB_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# First-try outcomes a rerun may replace: failures that say nothing about the
# model (pilot/agent-assisted-v2.md, "Rounds and reruns").
RERUNNABLE = {"quota_exhausted", "provider_error", "transport_interrupted", "harness_error"}

# Every outcome a generation receipt may record.
OUTCOMES = {
    "submitted",
    "provider_error",
    "quota_exhausted",
    "transport_interrupted",
    "truncated",
    "invalid_submission",
    "audit_rejected",
    "not_requested",
    "turn_limit",
    "wall_timeout",
    "interrupted",
    "configuration_error",
    "tool_error",
    "harness_error",
    "pending",
}

# Field names that must never appear in a published receipt: transcripts,
# reasoning, tool contents, or credentials. Receipts record only counts.
PRIVATE_FIELDS = re.compile(
    r'"(?:messages|reasoning_content|raw_response|response_text|tool_calls|tool_results'
    r'|authorization|api_key|x-api-key|OPENCODE_GO_API_KEY)"\s*:',
    re.I,
)


def slots(plan: dict, run_id: int):
    """Yield (model, problem, records artifact name, generation artifact name) per slot."""
    models = {model["id"]: model for model in plan["models"]}
    for model_id, problem in plan_slots(plan):
        identity = f"{model_id}-{problem}-{run_id}"
        yield models[model_id], problem, f"go-agent-records-{identity}", f"go-agent-generation-{identity}"


def in_waves(plan: dict) -> bool:
    """Whether the plan publishes wave by wave: a rerun, or a round paced under the usage limit."""
    return plan.get("try", 1) > 1 or bool(plan.get("release_on_quota"))


def wave_dir(out: Path, model_id: str, problem: str, plan: dict) -> Path:
    """Where a wave publishes a slot: `rep<N>` for a first try, or next to the try it replaces (`rep<N>-t2`)."""
    first = slot_dir(out, model_id, problem, plan_attempt(plan))
    return first if plan.get("try", 1) == 1 else first.with_name(f"{first.name}-t{plan['try']}")


def step_ran(job: dict, step_name: str) -> bool:
    """Whether the named step actually started (so a model may have been called).

    A step that succeeded or failed ran. A cancelled or timed-out step only
    counts if it has a start time; otherwise it never began.
    """
    for step in job.get("steps", []):
        if step.get("name") != step_name:
            continue
        if step.get("conclusion") in {"success", "failure"}:
            return True
        started = GITHUB_TIMESTAMP.fullmatch(step.get("started_at") or "")
        if step.get("conclusion") in {"cancelled", "timed_out"} and started:
            return True
    return False


def job_name(stage: str, model: str, problem: str, protocol: str) -> str:
    return f"{stage} {model} · {problem} · {protocol}"


def matches_job(job: dict, stage: str, model: str, problem: str, protocol: str) -> bool:
    expected = job_name(stage, model, problem, protocol)
    # Reusable-workflow jobs are prefixed with their caller's display name.
    names = {expected, f"{model} · {problem} / {expected}"}
    if job.get("conclusion") == "skipped":
        # GitHub never expands a skipped reusable job's own name; only the caller prefix names the slot.
        names.add(f"{model} · {problem} / {job_name(stage, '${{ inputs.model }}', '${{ inputs.problem }}', protocol)}")
    return job.get("name") in names


def validate_generation(generation: dict, model: dict, problem: str, plan: dict) -> None:
    """A generation receipt must match the reviewed plan and contain nothing private."""
    if (
        generation.get("model") != model["id"]
        or generation.get("problem") != problem
        or generation.get("protocol") != plan["protocol"]
    ):
        raise ValueError("agent generation identity mismatch")
    if generation.get("max_output_tokens") != output_limit(plan, model):
        raise ValueError("agent generation budget differs from reviewed plan")
    expected_settings = {key: model[key] for key in SETTING_KEYS if key in model}
    if generation.get("generation_settings") != expected_settings:
        raise ValueError("agent generation settings differ from reviewed plan")

    turns = generation.get("turns")
    if (
        generation.get("max_turns") != plan["max_turns"]
        or type(turns) is not int
        or not 0 <= turns <= plan["max_turns"]
    ):
        raise ValueError("agent turn budget differs from reviewed plan")
    if "transport_retries" in plan:
        _validate_round_receipt(generation, plan)
    if generation.get("outcome") not in OUTCOMES:
        raise ValueError("unknown agent generation outcome")
    if generation.get("max_checks") != plan.get("max_checks"):
        raise ValueError("agent development-check budget differs from reviewed plan")
    submitted = generation.get("outcome") == "submitted"
    if submitted and not re.fullmatch(r"[a-f0-9]{64}", generation.get("submission_sha256", "")):
        raise ValueError("agent submission has no explicit submit hash")

    usage = generation.get("usage", {})
    if not isinstance(usage, dict) or any(type(value) is not int or value < 0 for value in usage.values()):
        raise ValueError("invalid agent usage receipt")
    if output_tokens(generation, prefer="output_tokens") > output_limit(plan, model) * turns:
        raise ValueError("agent usage exceeds recorded turn budgets")
    if "checks" in generation:
        checks = generation["checks"]
        if type(checks) is not int or not 0 <= checks <= plan.get("max_checks", 3):
            raise ValueError("invalid agent development check count")

    if PRIVATE_FIELDS.search(json.dumps(generation)):
        raise ValueError("private field in agent generation receipt")


def _validate_round_receipt(generation: dict, plan: dict) -> None:
    """v2 receipts name their round and try, and list every resent request."""
    if generation.get("attempt") != plan_attempt(plan) or generation.get("try") != plan["try"]:
        raise ValueError("agent generation round differs from reviewed plan")
    retries = generation.get("transport_retries")
    turns = generation.get("turns")
    if not isinstance(retries, list) or len(retries) > plan["transport_retries"] * max(turns, 1):
        raise ValueError("invalid agent transport retries")
    for retry in retries:
        if (
            not isinstance(retry, dict)
            or set(retry) != {"turn", "retry", "reason", "usage_known"}
            or type(retry["turn"]) is not int
            or not 1 <= retry["turn"] <= turns
            or type(retry["retry"]) is not int
            or not 1 <= retry["retry"] <= plan["transport_retries"]
            or not isinstance(retry["reason"], str)
            or not re.fullmatch(r"[a-z0-9_]{1,64}", retry["reason"])
            or type(retry["usage_known"]) is not bool
        ):
            raise ValueError("invalid agent transport retry entry")


def publish_agent(
    artifacts: Path, plan: dict, run: dict, jobs: list[dict], output: Path, site_output: Path
) -> dict:
    """Validate each slot's artifacts against its jobs, then publish. Returns the leaderboard."""
    if run.get("path") != WORKFLOW or plan.get("protocol") not in AGENT_PROTOCOLS:
        raise ValueError("agent publication requires its own workflow and protocol")
    if in_waves(plan):
        raise ValueError("a rerun or paced round is published wave by wave with wave_slots")
    if output.exists() or site_output.exists():
        raise FileExistsError("never overwrite published results")

    prepared = []
    for model, problem, records_artifact, _ in slots(plan, run["id"]):
        job = _scoring_job(jobs, model["id"], problem, plan["protocol"])
        slot = _prepare_slot(artifacts / records_artifact, plan, run, job, model, problem)
        slot["record"]["execution"] = execution_evidence(run, job)
        prepared.append(slot)

    write_published_records(output, plan, prepared)
    board = export_leaderboard(output, site_output, plan)
    generations = [slot["generation"] for slot in prepared]
    records = [slot["record"] for slot in prepared]
    board["meta"].update(round_meta(plan, run, generations, records))
    save_leaderboard(output, site_output, board, run, jobs)
    return board


def round_meta(plan: dict, run: dict, generations: list[dict], records: list[dict]) -> dict:
    """A round leaderboard's provenance (from `run`) and spend (over `generations`)."""
    return {
        "git_commit": run["head_sha"],
        "protocol": plan["protocol"],
        "repetitions": plan_attempt(plan),
        "max_turns": plan["max_turns"],
        "max_output_tokens": plan["max_output_tokens"],
        "output_budget": plan.get("output_budget", "fixed"),
        "workflow_url": run["html_url"],
        "sandbox": {"mode": "docker · network disabled"},
        **round_spend(generations, records),
    }


def round_spend(generations: list[dict], records: list[dict]) -> dict:
    return {
        "generation_requests": sum(g.get("turns", 0) for g in generations),
        "incomplete_usage": any(g.get("incomplete_usage") for g in generations),
        "incomplete_evidence": any(r.get("record_origin", "scorer") != "scorer" for r in records),
        "output_tokens": sum(output_tokens(g, prefer="output_tokens") for g in generations),
    }


def wave_slots(artifacts: Path, plan: dict, run: dict, jobs: list[dict], round_dir: Path) -> list[dict]:
    """The finished slots one wave adds to its round.

    A wave runs only the slots it claimed. A slot the usage limit cut off
    released its claim and runs again in a later wave, so it is skipped; its
    evidence stays in the Actions artifacts. A rerun slot must replace a first
    try in `round_dir` that failed for infrastructure reasons.
    """
    if run.get("path") != WORKFLOW or plan.get("protocol") not in AGENT_PROTOCOLS or not in_waves(plan):
        raise ValueError("not an agent wave")
    rerun = plan.get("try", 1) > 1
    prepared = []
    for model, problem, records_artifact, _ in slots(plan, run["id"]):
        artifact = artifacts / records_artifact
        generate = [job for job in jobs if matches_job(job, "Generate", model["id"], problem, plan["protocol"])]
        # Evidence is uploaded only by the run that claimed the slot; a re-run
        # attempt of that run scores it without generating again.
        if not artifact.exists() and not any(step_ran(job, AGENT_GENERATION_STEP) for job in generate):
            continue  # not in this wave, or paused before any model call
        receipt = slot_dir(artifact, model["id"], problem, plan_attempt(plan)) / "generation.json"
        if (
            plan.get("release_on_quota")
            and receipt.is_file()
            and json.loads(receipt.read_text()).get("outcome") == "quota_exhausted"
        ):
            continue

        if rerun:
            first = slot_dir(round_dir, model["id"], problem, plan_attempt(plan)) / "record.json"
            if not first.is_file():
                raise ValueError("a rerun needs its round's published first try")
            voided = json.loads(first.read_text())
            if voided.get("outcome") not in RERUNNABLE or voided.get("execution_health") != "failed":
                raise ValueError("a rerun may only replace an infrastructure failure")

        job = _scoring_job(jobs, model["id"], problem, plan["protocol"])
        slot = _prepare_slot(artifact, plan, run, job, model, problem)
        slot["record"]["execution"] = execution_evidence(run, job)
        if rerun:
            slot["record"]["try"] = plan["try"]
        prepared.append(slot)
    return prepared


def write_wave_records(round_dir: Path, plan: dict, prepared: list[dict]) -> None:
    """Add each slot of a wave to its round. Never touches existing files."""
    for slot in prepared:
        dest = wave_dir(round_dir, slot["model_id"], slot["problem"], plan)
        dest.mkdir(parents=True)
        atomic_json(dest / "generation.json", slot["generation"])
        atomic_json(dest / "record.json", slot["record"])
        atomic_json(dest / "manifest.json", slot["record"]["manifest"])
        if slot["source"] is not None:
            frozen = frozen_path(dest)
            frozen.parent.mkdir(parents=True)
            shutil.copyfile(slot["source"], frozen)


def _scoring_job(jobs: list[dict], model_id: str, problem: str, protocol: str) -> dict:
    matches = [job for job in jobs if matches_job(job, "Evaluate", model_id, problem, protocol)]
    if len(matches) != 1:
        raise ValueError("missing or duplicate agent scoring job")
    job = matches[0]
    if job.get("status") != "completed" or job.get("conclusion") not in FINISHED_JOB_CONCLUSIONS:
        raise ValueError("agent scoring job is not completed")
    return job


def _prepare_slot(artifact: Path, plan: dict, run: dict, job: dict, model: dict, problem: str) -> dict:
    """The record, receipt and frozen RTL to publish for one slot, after every check."""
    model_id = model["id"]
    label = f"opencode-go/{model_id} [{plan['protocol']}]"
    attempt = plan_attempt(plan)

    if not artifact.exists():
        if job["conclusion"] == "success":
            raise ValueError("successful agent job has no evidence")
        generation = {
            "model": model_id,
            "problem": problem,
            "protocol": plan["protocol"],
            "outcome": "interrupted",
            "evidence_unavailable": True,
            "incomplete_usage": True,
        }
        record = _unscored_record(
            plan,
            problem,
            label,
            origin="github-job-status-only",
            error="Actions job ended before canonical evidence was uploaded; score and usage are unknown.",
            manifest={"generation": generation},
        )
        return {"model_id": model_id, "problem": problem, "attempt": attempt, "record": record,
                "generation": generation, "source": None}

    if json.loads((artifact / "plan.json").read_text()) != plan:
        raise ValueError("agent artifact plan differs from source plan")
    dest = slot_dir(artifact, model_id, problem, attempt)
    generation = json.loads((dest / "generation.json").read_text())
    validate_generation(generation, model, problem, plan)
    _check_provenance(generation.get("github", {}), run)
    source = _frozen_source(dest)

    record_path = dest / "record.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        if record.get("problem") != problem or record.get("label") != label:
            raise ValueError("agent record identity mismatch")
        if record.get("manifest", {}).get("generation") != generation:
            raise ValueError("agent manifest generation mismatch")
    elif job["conclusion"] != "success":
        record = _unscored_record(
            plan,
            problem,
            label,
            origin="github-generation-only",
            error="Generation evidence was saved but scoring did not finish; score is unknown.",
            manifest={
                "generation": generation,
                "submission_sha256": sha256_bytes(source.read_bytes()) if source else "",
            },
        )
    else:
        raise ValueError("successful agent job has no scoring record")

    scored_sha = record.get("manifest", {}).get("submission_sha256", "")
    if generation.get("outcome") == "submitted" and scored_sha != generation["submission_sha256"]:
        raise ValueError("scored source differs from explicit agent submit hash")
    if source:
        if sha256_bytes(source.read_bytes()) != scored_sha:
            raise ValueError("frozen agent submission hash mismatch")
    elif scored_sha or record.get("result") is not None:
        raise ValueError("agent score has no frozen submission")
    return {"model_id": model_id, "problem": problem, "attempt": attempt, "record": record,
            "generation": generation, "source": source}


def _check_provenance(github_context: dict, run: dict) -> None:
    # A slot claims once per run, so a re-run attempt scores the earlier
    # attempt's generation instead of generating again.
    attempts = {str(attempt) for attempt in range(1, run["run_attempt"] + 1)}
    if (
        github_context.get("GITHUB_REPOSITORY") != REPOSITORY
        or str(github_context.get("GITHUB_RUN_ID")) != str(run["id"])
        or str(github_context.get("GITHUB_RUN_ATTEMPT")) not in attempts
        or github_context.get("GITHUB_SHA") != run["head_sha"]
    ):
        raise ValueError("agent generation provenance mismatch")


def _frozen_source(dest: Path) -> Path | None:
    """The submitted dut.v (scored copy preferred). Every copy present must be identical."""
    candidates = [frozen_path(dest), dest / "dut.v"]
    present = [path for path in candidates if path.is_file()]
    if not present:
        return None
    source = present[0]
    if any(path.read_bytes() != source.read_bytes() for path in present[1:]):
        raise ValueError("conflicting frozen agent submissions")
    return source


def _unscored_record(plan: dict, problem: str, label: str, *, origin: str, error: str, manifest: dict) -> dict:
    """A record for a slot whose score is unknown: health failed, no result."""
    return {
        "problem": problem,
        "label": label,
        "group": plan["name"],
        "attempt": plan_attempt(plan),
        "record_origin": origin,
        "outcome": "scoring_interrupted",
        "execution_health": "failed",
        "result": None,
        "error": error,
        "manifest": manifest,
    }
