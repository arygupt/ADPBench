"""Publish agent-assisted-v1 results bound to their source run - never transcripts or tool contents.

Used by publish_results.py for runs of the agent workflow. Each (model,
problem) slot ran as its own pair of Actions jobs ("Generate ..." and
"Evaluate ...") and uploaded its own artifacts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from adpbench.hashing import sha256_bytes
from scripts.go_pilot import SETTING_KEYS, output_limit, slot_dir
from scripts.publish_go import (
    FINISHED_JOB_CONCLUSIONS,
    REPOSITORY,
    execution_evidence,
    export_leaderboard,
    output_tokens,
    save_leaderboard,
    write_published_records,
)

PROTOCOL = "agent-assisted-v1"
WORKFLOW = ".github/workflows/go-agent.yml"

# Every outcome a generation receipt may record.
OUTCOMES = {
    "submitted",
    "provider_error",
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
    for model in plan["models"]:
        for problem in plan["problems"]:
            identity = f"{model['id']}-{problem}-{run_id}"
            yield model, problem, f"go-agent-records-{identity}", f"go-agent-generation-{identity}"


def job_name(stage: str, model: str, problem: str) -> str:
    return f"{stage} {model} · {problem} · {PROTOCOL}"


def matches_job(job: dict, stage: str, model: str, problem: str) -> bool:
    expected = job_name(stage, model, problem)
    # Reusable-workflow jobs are prefixed with their caller's display name.
    return job.get("name") in {expected, f"{model} · {problem} / {expected}"}


def validate_generation(generation: dict, model: dict, problem: str, plan: dict) -> None:
    """A generation receipt must match the reviewed plan and contain nothing private."""
    if (
        generation.get("model") != model["id"]
        or generation.get("problem") != problem
        or generation.get("protocol") != PROTOCOL
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


def publish_agent(
    artifacts: Path, plan: dict, run: dict, jobs: list[dict], output: Path, site_output: Path
) -> dict:
    """Validate each slot's artifacts against its jobs, then publish. Returns the leaderboard."""
    if run.get("path") != WORKFLOW or plan.get("protocol") != PROTOCOL:
        raise ValueError("agent publication requires its own workflow and protocol")
    if output.exists() or site_output.exists():
        raise FileExistsError("never overwrite published results")

    prepared = []
    for model, problem, records_artifact, _ in slots(plan, run["id"]):
        job = _scoring_job(jobs, model["id"], problem)
        slot = _prepare_slot(artifacts / records_artifact, plan, run, job, model, problem)
        slot["record"]["execution"] = execution_evidence(run, job)
        prepared.append(slot)

    write_published_records(output, plan, prepared)
    board = export_leaderboard(output, site_output, plan)
    generations = [slot["generation"] for slot in prepared]
    records = [slot["record"] for slot in prepared]
    board["meta"].update(
        {
            "git_commit": run["head_sha"],
            "protocol": PROTOCOL,
            "repetitions": 1,
            "max_turns": plan["max_turns"],
            "max_output_tokens": plan["max_output_tokens"],
            "output_budget": plan.get("output_budget", "fixed"),
            "workflow_url": run["html_url"],
            "sandbox": {"mode": "docker · network disabled"},
            "generation_requests": sum(g.get("turns", 0) for g in generations),
            "incomplete_usage": any(g.get("incomplete_usage") for g in generations),
            "incomplete_evidence": any(r.get("record_origin", "scorer") != "scorer" for r in records),
            "output_tokens": sum(output_tokens(g, prefer="output_tokens") for g in generations),
        }
    )
    save_leaderboard(output, site_output, board, run, jobs)
    return board


def _scoring_job(jobs: list[dict], model_id: str, problem: str) -> dict:
    matches = [job for job in jobs if matches_job(job, "Evaluate", model_id, problem)]
    if len(matches) != 1:
        raise ValueError("missing or duplicate agent scoring job")
    job = matches[0]
    if job.get("status") != "completed" or job.get("conclusion") not in FINISHED_JOB_CONCLUSIONS:
        raise ValueError("agent scoring job is not completed")
    return job


def _prepare_slot(artifact: Path, plan: dict, run: dict, job: dict, model: dict, problem: str) -> dict:
    """The record, receipt and frozen RTL to publish for one slot, after every check."""
    model_id = model["id"]
    label = f"opencode-go/{model_id} [{PROTOCOL}]"

    if not artifact.exists():
        if job["conclusion"] == "success":
            raise ValueError("successful agent job has no evidence")
        generation = {
            "model": model_id,
            "problem": problem,
            "protocol": PROTOCOL,
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
        return {"model_id": model_id, "problem": problem, "record": record, "generation": generation, "source": None}

    if json.loads((artifact / "plan.json").read_text()) != plan:
        raise ValueError("agent artifact plan differs from source plan")
    dest = slot_dir(artifact, model_id, problem)
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
    return {"model_id": model_id, "problem": problem, "record": record, "generation": generation, "source": source}


def _check_provenance(github_context: dict, run: dict) -> None:
    if (
        github_context.get("GITHUB_REPOSITORY") != REPOSITORY
        or str(github_context.get("GITHUB_RUN_ID")) != str(run["id"])
        or str(github_context.get("GITHUB_RUN_ATTEMPT")) != str(run["run_attempt"])
        or github_context.get("GITHUB_SHA") != run["head_sha"]
    ):
        raise ValueError("agent generation provenance mismatch")


def _frozen_source(dest: Path) -> Path | None:
    """The submitted dut.v (scored copy preferred). Every copy present must be identical."""
    candidates = [dest.parent / "rep1_frozen" / "dut.v", dest / "dut.v"]
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
        "attempt": 1,
        "record_origin": origin,
        "outcome": "scoring_interrupted",
        "execution_health": "failed",
        "result": None,
        "error": error,
        "manifest": manifest,
    }
