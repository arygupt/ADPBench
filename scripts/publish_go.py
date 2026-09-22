"""Validate actual Actions artifacts and export a separate single-shot dataset.

No model calls. No invented scores. No automatic git commit or repository push.
Only canonical records for the reviewed plan can be published; all submissions
are checked against the scorer's frozen SHA-256. Raw reasoning stays in Actions
artifacts and is never included in the website.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from adpbench.problem import repo_root
from adpbench.report import write_report
from adpbench.site import export_site
from scripts.go_pilot import read_plan, write_json

REPOSITORY = "arygupt/ADPBench"


def github(path: str) -> dict:
    return json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/{path}"], check=True, capture_output=True, text=True).stdout)


def publish(artifacts: Path, plan: dict, run: dict, jobs: list[dict], output: Path, site_output: Path) -> dict:
    if run["status"] != "completed" or run["event"] != "workflow_dispatch" or run["path"] != ".github/workflows/go-core.yml":
        raise ValueError("only a completed manually dispatched Go core evaluation may be published")
    if run.get("repository", {}).get("full_name", "").lower() != REPOSITORY.lower():
        raise ValueError("unexpected repository")
    if not re.fullmatch(r"[a-f0-9]{40}", run["head_sha"]):
        raise ValueError("invalid evaluated commit")
    if output.exists() or site_output.exists():
        raise FileExistsError("never overwrite published results")
    prepared = []
    for model in plan["models"]:
        model_id = model["id"]
        job = next(j for j in jobs if j["name"] == f"Evaluate {model_id} · dot product + GEMV")
        if job["status"] != "completed" or job["conclusion"] not in {"success", "failure", "timed_out", "cancelled"}:
            raise ValueError(f"model job not evaluated: {model_id}")
        artifact = artifacts / f"go-core-{model_id}-{run['id']}"
        if not artifact.exists() and job["conclusion"] in {"failure", "timed_out", "cancelled"}:
            # A job-level timeout can prevent always() artifact-upload steps.
            # Publish only the observed job status, never fabricated scores,
            # provider responses, token counts, or submission hashes.
            for problem in plan["problems"]:
                outcome = {"timed_out": "timed out", "cancelled": "was cancelled", "failure": "failed"}[job["conclusion"]]
                error = f"GitHub job {outcome} before final artifacts were uploaded. Per-problem generation, scores, RTL and token usage are unavailable."
                gen = {"model": model_id, "problem": problem, "protocol": "single-shot", "evidence_unavailable": True}
                record = {
                    "problem": problem, "label": f"opencode-go/{model_id} [single-shot]", "attempt": 1,
                    "group": plan["name"], "record_origin": "github-job-status-only", "error": error,
                    "result": None, "manifest": {"generation": gen},
                    "execution": {"kind": "model-evaluation", "repository": REPOSITORY,
                                  "run_id": run["id"], "run_attempt": run["run_attempt"], "job_id": job["id"],
                                  "commit": run["head_sha"], "completed_at": job["completed_at"],
                                  "job_conclusion": job["conclusion"]},
                }
                prepared.append((model_id, problem, record, gen, artifact / "missing.v"))
            continue
        if json.loads((artifact / "plan.json").read_text()) != plan:
            raise ValueError("artifact plan differs from the reviewed plan")
        for problem in plan["problems"]:
            path = artifact / f"opencode-go-{model_id}" / problem / "rep1"
            gen = json.loads((path / "generation.json").read_text())
            if gen["model"] != model_id or gen["problem"] != problem or gen["protocol"] != "single-shot":
                raise ValueError("record identity mismatch")
            meta = gen["github"]
            if (meta["GITHUB_REPOSITORY"].lower() != REPOSITORY.lower()
                    or int(meta["GITHUB_RUN_ID"]) != run["id"]
                    or int(meta["GITHUB_RUN_ATTEMPT"]) != run["run_attempt"]
                    or meta["GITHUB_SHA"] != run["head_sha"]):
                raise ValueError("generation provenance mismatch")
            source = path.parent / "rep1_frozen/dut.v"
            if (path / "record.json").exists():
                record = json.loads((path / "record.json").read_text())
            elif job["conclusion"] in {"failure", "timed_out", "cancelled"}:
                # Preserve verified generation evidence without inventing an
                # interrupted scorer's result. This hash identifies the saved
                # artifact only; it is not a claim of completed evaluation.
                if not source.exists() and (path / "dut.v").is_file():
                    source = path / "dut.v"  # Pre-scoring generation artifact; not a measured score.
                record = {
                    "problem": problem, "label": f"opencode-go/{model_id} [single-shot]", "attempt": 1,
                    "group": plan["name"], "record_origin": "github-generation-only",
                    "error": f"GitHub job {job['conclusion']}; generation artifacts were saved but scoring did not produce a final record. Score is unknown.",
                    "result": None,
                    "manifest": {"generation": gen, "submission_sha256": hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else ""},
                }
            else:
                raise ValueError("successful job has no scoring record")
            if record["problem"] != problem or record["label"] != f"opencode-go/{model_id} [single-shot]":
                raise ValueError("record identity mismatch")
            if record["manifest"]["generation"] != gen:
                raise ValueError("manifest generation mismatch")
            expected = record["manifest"].get("submission_sha256")
            if source.exists():
                if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError("frozen submission hash mismatch")
            elif expected or (record.get("result") or {}).get("correct"):
                raise ValueError("score has no matching frozen submission")
            record["execution"] = {
                "kind": "model-evaluation", "repository": REPOSITORY,
                "run_id": run["id"], "run_attempt": run["run_attempt"], "job_id": job["id"],
                "commit": run["head_sha"], "completed_at": job["completed_at"],
                "job_conclusion": job["conclusion"],
            }
            prepared.append((model_id, problem, record, gen, source))
    output.mkdir(parents=True)
    write_json(output / "plan.json", plan)
    for model_id, problem, record, gen, source in prepared:
        dest = output / f"opencode-go-{model_id}" / problem / "rep1"
        write_json(dest / "record.json", record)
        write_json(dest / "generation.json", gen)
        write_json(dest / "manifest.json", record["manifest"])
        if source.exists():
            frozen = dest.parent / "rep1_frozen/dut.v"
            frozen.parent.mkdir(parents=True)
            shutil.copyfile(source, frozen)
    write_report(output)
    export_site(output, site_output, repo_root() / "pilot/sanity.json")
    board_path = site_output / "leaderboard.json"
    board = json.loads(board_path.read_text())
    board["problems"] = [p for p in board["problems"] if p["name"] in plan["problems"]]
    board["meta"].update({
        "git_commit": run["head_sha"], "protocol": "single-shot", "repetitions": 1,
        "max_output_tokens": plan["max_output_tokens"], "sandbox": {"mode": "docker · network disabled"},
        "workflow_url": run["html_url"], "generation_requests": sum(bool(g.get("response_id")) for _, _, _, g, _ in prepared),
        "incomplete_evidence": any(r.get("record_origin", "scorer") != "scorer" for _, _, r, _, _ in prepared),
        "incomplete_usage": any(g.get("evidence_unavailable") or g.get("incomplete_usage") for _, _, _, g, _ in prepared),
        "output_tokens": sum(g.get("usage", {}).get("completion_tokens", g.get("usage", {}).get("output_tokens", 0)) for _, _, _, g, _ in prepared),
    })
    if plan.get("output_budget") == "provider_max":
        board["meta"]["output_budget"] = "provider_max"
    write_json(board_path, board)
    write_json(output / "actions.json", {
        "run": {k: run[k] for k in ("id", "run_attempt", "head_sha", "html_url", "status", "conclusion", "event", "path") if k in run},
        "jobs": [{k: job[k] for k in ("id", "name", "status", "conclusion", "started_at", "completed_at") if k in job} for job in jobs],
    })
    return board


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--run-id", required=True, type=int)
    args = parser.parse_args()
    plan = read_plan(repo_root() / "pilot/go-core-20260922.json")
    run = github(f"actions/runs/{args.run_id}")
    jobs = github(f"actions/runs/{args.run_id}/jobs?per_page=100")["jobs"]
    board = publish(args.artifacts, plan, run, jobs,
                    repo_root() / "pilot/results" / plan["name"], repo_root() / "site/data" / plan["name"])
    print(json.dumps({"models": len(board["models"]), "attempts": sum(m["attempts"] for m in board["models"]),
                      "correct": sum(m["correct"] for m in board["models"]), "output_tokens": board["meta"]["output_tokens"]}))


if __name__ == "__main__":
    main()
