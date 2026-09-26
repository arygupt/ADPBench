"""Validate real Actions artifacts and export them as a separate single-shot dataset.

    python -m scripts.publish_go --artifacts <dir> --run-id <id>

No model calls, no invented scores, no automatic git commit or push. Only
canonical records for the reviewed plan can be published, and every
submission is checked against the scorer's frozen SHA-256. Raw reasoning stays
in Actions artifacts and never reaches the website.

When a job ended before uploading its evidence, the published record says so
(`record_origin`) and carries no score.

This module also holds the steps shared with publish_agent.py: writing the
published record tree and building the leaderboard from it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from adpbench.durable import atomic_json
from adpbench.hashing import sha256_bytes
from adpbench.problem import repo_root
from adpbench.report import write_report
from adpbench.site import export_site
from scripts.go_pilot import frozen_path, read_plan, slot_dir

REPOSITORY = "arygupt/ADPBench"
WORKFLOW = ".github/workflows/go-core.yml"
PROTOCOL = "single-shot"
FINISHED_JOB_CONCLUSIONS = {"success", "failure", "timed_out", "cancelled"}
FAILED_JOB_CONCLUSIONS = {"failure", "timed_out", "cancelled"}
ACTIONS_RUN_FIELDS = ("id", "run_attempt", "head_sha", "html_url", "status", "conclusion", "event", "path")
ACTIONS_JOB_FIELDS = ("id", "name", "status", "conclusion", "started_at", "completed_at")


def github(path: str) -> dict:
    """GET a GitHub REST API path for this repository, via the `gh` CLI."""
    command = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
    return json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)


# --------------------------------------------------------------------------
# Shared with publish_agent.py
# --------------------------------------------------------------------------


def execution_evidence(run: dict, job: dict) -> dict:
    """Where a published result came from: the exact Actions run, attempt, job, and commit."""
    return {
        "kind": "model-evaluation",
        "repository": REPOSITORY,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "job_id": job["id"],
        "commit": run["head_sha"],
        "completed_at": job["completed_at"],
        "job_conclusion": job["conclusion"],
    }


def write_published_records(output: Path, plan: dict, slots: list[dict]) -> None:
    """Write plan.json and each slot's generation, record, manifest, and frozen RTL.

    Each slot is {"model_id", "problem", "record", "generation", "source"},
    plus an optional "attempt" (default 1), where `source` is the frozen dut.v
    to copy, or None.
    """
    output.mkdir(parents=True)
    atomic_json(output / "plan.json", plan)
    for slot in slots:
        dest = slot_dir(output, slot["model_id"], slot["problem"], slot.get("attempt", 1))
        atomic_json(dest / "generation.json", slot["generation"])
        atomic_json(dest / "record.json", slot["record"])
        atomic_json(dest / "manifest.json", slot["record"]["manifest"])
        if slot["source"] is not None:
            frozen = frozen_path(dest)
            frozen.parent.mkdir(parents=True)
            shutil.copyfile(slot["source"], frozen)


def export_leaderboard(output: Path, site_output: Path, plan: dict) -> dict:
    """Report and site data for the published records. Returns the leaderboard to finish."""
    write_report(output)
    export_site(output, site_output, repo_root() / "pilot/sanity.json")
    board = json.loads((site_output / "leaderboard.json").read_text())
    board["problems"] = [problem for problem in board["problems"] if problem["name"] in plan["problems"]]
    return board


def save_leaderboard(output: Path, site_output: Path, board: dict, run: dict, jobs: list[dict]) -> None:
    """Write the finished leaderboard, plus actions.json describing the source run and jobs."""
    atomic_json(site_output / "leaderboard.json", board)
    actions = {
        "run": {field: run[field] for field in ACTIONS_RUN_FIELDS if field in run},
        "jobs": [{field: job[field] for field in ACTIONS_JOB_FIELDS if field in job} for job in jobs],
    }
    atomic_json(output / "actions.json", actions)


def check_frozen_hash(source: Path | None, record: dict) -> None:
    """The published RTL must be exactly the RTL the recorded score was computed on."""
    expected = record["manifest"].get("submission_sha256")
    if source is not None:
        if sha256_bytes(source.read_bytes()) != expected:
            raise ValueError("frozen submission hash mismatch")
    elif expected or (record.get("result") or {}).get("correct"):
        raise ValueError("score has no matching frozen submission")


def output_tokens(generation: dict, *, prefer: str = "completion_tokens") -> int:
    usage = generation.get("usage", {})
    other = "output_tokens" if prefer == "completion_tokens" else "completion_tokens"
    return usage.get(prefer, usage.get(other, 0))


# --------------------------------------------------------------------------
# Single-shot publication
# --------------------------------------------------------------------------


def publish(
    artifacts: Path, plan: dict, run: dict, jobs: list[dict], output: Path, site_output: Path
) -> dict:
    """Validate a completed go-core run's artifacts and publish them. Returns the leaderboard."""
    if run["status"] != "completed" or run["event"] != "workflow_dispatch" or run["path"] != WORKFLOW:
        raise ValueError("only a completed manually dispatched Go core evaluation may be published")
    if run.get("repository", {}).get("full_name", "").lower() != REPOSITORY.lower():
        raise ValueError("unexpected repository")
    if not re.fullmatch(r"[a-f0-9]{40}", run["head_sha"]):
        raise ValueError("invalid evaluated commit")
    if output.exists() or site_output.exists():
        raise FileExistsError("never overwrite published results")

    slots = []
    for model in plan["models"]:
        slots += _model_slots(artifacts, plan, run, jobs, model["id"])

    write_published_records(output, plan, slots)
    board = export_leaderboard(output, site_output, plan)
    generations = [slot["generation"] for slot in slots]
    records = [slot["record"] for slot in slots]
    board["meta"].update(
        {
            "git_commit": run["head_sha"],
            "protocol": PROTOCOL,
            "repetitions": 1,
            "max_output_tokens": plan["max_output_tokens"],
            "sandbox": {"mode": "docker · network disabled"},
            "workflow_url": run["html_url"],
            "generation_requests": sum(1 for g in generations if g.get("response_id")),
            "incomplete_evidence": any(r.get("record_origin", "scorer") != "scorer" for r in records),
            "incomplete_usage": any(
                g.get("evidence_unavailable") or g.get("incomplete_usage") for g in generations
            ),
            "output_tokens": sum(output_tokens(g) for g in generations),
        }
    )
    if plan.get("output_budget") == "provider_max":
        board["meta"]["output_budget"] = "provider_max"
    save_leaderboard(output, site_output, board, run, jobs)
    return board


def _model_slots(artifacts: Path, plan: dict, run: dict, jobs: list[dict], model_id: str) -> list[dict]:
    """The publishable slots for one model, from its artifact (or its job status alone)."""
    job = _evaluation_job(jobs, model_id)
    if job["status"] != "completed" or job["conclusion"] not in FINISHED_JOB_CONCLUSIONS:
        raise ValueError(f"model job not evaluated: {model_id}")
    label = f"opencode-go/{model_id} [{PROTOCOL}]"
    artifact = artifacts / f"go-core-{model_id}-{run['id']}"

    if not artifact.exists() and job["conclusion"] in FAILED_JOB_CONCLUSIONS:
        # A job-level timeout can prevent always() artifact-upload steps. Publish
        # only the observed job status - never made-up scores, provider
        # responses, token counts, or submission hashes.
        return [
            _job_status_only_slot(plan, run, job, model_id, label, problem) for problem in plan["problems"]
        ]

    if json.loads((artifact / "plan.json").read_text()) != plan:
        raise ValueError("artifact plan differs from the reviewed plan")
    slots = []
    for problem in plan["problems"]:
        dest = slot_dir(artifact, model_id, problem)
        generation = json.loads((dest / "generation.json").read_text())
        if (
            generation["model"] != model_id
            or generation["problem"] != problem
            or generation["protocol"] != PROTOCOL
        ):
            raise ValueError("record identity mismatch")
        _check_provenance(generation["github"], run)

        source = dest.parent / "rep1_frozen" / "dut.v"
        if (dest / "record.json").exists():
            record = json.loads((dest / "record.json").read_text())
        elif job["conclusion"] in FAILED_JOB_CONCLUSIONS:
            # Scoring never finished. Keep the verified generation evidence, but
            # invent no result. The hash only identifies the saved RTL; it does
            # not claim the RTL was evaluated.
            if not source.exists() and (dest / "dut.v").is_file():
                source = dest / "dut.v"
            record = {
                "problem": problem,
                "label": label,
                "attempt": 1,
                "group": plan["name"],
                "record_origin": "github-generation-only",
                "error": (
                    f"GitHub job {job['conclusion']}; generation artifacts were saved but scoring "
                    "did not produce a final record. Score is unknown."
                ),
                "result": None,
                "manifest": {
                    "generation": generation,
                    "submission_sha256": sha256_bytes(source.read_bytes()) if source.exists() else "",
                },
            }
        else:
            raise ValueError("successful job has no scoring record")

        if record["problem"] != problem or record["label"] != label:
            raise ValueError("record identity mismatch")
        if record["manifest"]["generation"] != generation:
            raise ValueError("manifest generation mismatch")
        source = source if source.exists() else None
        check_frozen_hash(source, record)
        record["execution"] = execution_evidence(run, job)
        slots.append(
            {"model_id": model_id, "problem": problem, "record": record, "generation": generation, "source": source}
        )
    return slots


def _evaluation_job(jobs: list[dict], model_id: str) -> dict:
    name = f"Evaluate {model_id} · dot product + GEMV"
    for job in jobs:
        if job["name"] == name:
            return job
    raise ValueError(f"missing evaluation job: {model_id}")


def _check_provenance(github_context: dict, run: dict) -> None:
    """The generation must have happened in exactly this Actions run and commit."""
    if (
        github_context["GITHUB_REPOSITORY"].lower() != REPOSITORY.lower()
        or int(github_context["GITHUB_RUN_ID"]) != run["id"]
        or int(github_context["GITHUB_RUN_ATTEMPT"]) != run["run_attempt"]
        or github_context["GITHUB_SHA"] != run["head_sha"]
    ):
        raise ValueError("generation provenance mismatch")


def _job_status_only_slot(
    plan: dict, run: dict, job: dict, model_id: str, label: str, problem: str
) -> dict:
    ended = {"timed_out": "timed out", "cancelled": "was cancelled", "failure": "failed"}[job["conclusion"]]
    generation = {"model": model_id, "problem": problem, "protocol": PROTOCOL, "evidence_unavailable": True}
    record = {
        "problem": problem,
        "label": label,
        "attempt": 1,
        "group": plan["name"],
        "record_origin": "github-job-status-only",
        "error": (
            f"GitHub job {ended} before final artifacts were uploaded. Per-problem generation, "
            "scores, RTL and token usage are unavailable."
        ),
        "result": None,
        "manifest": {"generation": generation},
        "execution": execution_evidence(run, job),
    }
    return {"model_id": model_id, "problem": problem, "record": record, "generation": generation, "source": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--run-id", required=True, type=int)
    args = parser.parse_args()
    plan = read_plan(repo_root() / "pilot/go-core-20260922.json")
    run = github(f"actions/runs/{args.run_id}")
    jobs = github(f"actions/runs/{args.run_id}/jobs?per_page=100")["jobs"]
    board = publish(
        args.artifacts,
        plan,
        run,
        jobs,
        repo_root() / "pilot/results" / plan["name"],
        repo_root() / "site/data" / plan["name"],
    )
    summary = {
        "models": len(board["models"]),
        "attempts": sum(model["attempts"] for model in board["models"]),
        "correct": sum(model["correct"] for model in board["models"]),
        "output_tokens": board["meta"]["output_tokens"],
    }
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
