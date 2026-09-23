"""Publish source-bound agent-assisted evidence, never model transcripts or tools."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from adpbench.problem import repo_root
from adpbench.report import write_report
from adpbench.site import export_site
from scripts.go_pilot import output_limit, write_json
from scripts.publish_go import REPOSITORY

PROTOCOL = "agent-assisted-v1"
WORKFLOW = ".github/workflows/go-agent.yml"
OUTCOMES = {
    "submitted", "provider_error", "transport_interrupted", "truncated",
    "invalid_submission", "audit_rejected", "not_requested", "turn_limit",
    "wall_timeout", "interrupted", "configuration_error", "tool_error", "harness_error", "pending",
}


def slots(plan: dict, run_id: int):
    for model in plan["models"]:
        for problem in plan["problems"]:
            identity = f"{model['id']}-{problem}-{run_id}"
            yield model, problem, f"go-agent-records-{identity}", f"go-agent-generation-{identity}"


def job_name(stage: str, model: str, problem: str) -> str:
    return f"{stage} {model} · {problem} · {PROTOCOL}"


def matches_job(job: dict, stage: str, model: str, problem: str) -> bool:
    expected = job_name(stage, model, problem)
    # Reusable Actions jobs include their caller's display name as a prefix.
    return job.get("name") in {expected, f"{model} · {problem} / {expected}"}


def validate_generation(generation: dict, model: dict, problem: str, plan: dict) -> None:
    if (generation.get("model") != model["id"] or generation.get("problem") != problem
            or generation.get("protocol") != PROTOCOL):
        raise ValueError("agent generation identity mismatch")
    if generation.get("max_output_tokens") != output_limit(plan, model):
        raise ValueError("agent generation budget differs from reviewed plan")
    expected = {k:model[k] for k in ("thinking", "reasoning_effort", "reasoning", "token_limit_key") if k in model}
    if generation.get("generation_settings") != expected:
        raise ValueError("agent generation settings differ from reviewed plan")
    turns = generation.get("turns")
    if (generation.get("max_turns") != plan["max_turns"] or type(turns) is not int
            or not 0 <= turns <= plan["max_turns"]):
        raise ValueError("agent turn budget differs from reviewed plan")
    if generation.get("outcome") not in OUTCOMES:
        raise ValueError("unknown agent generation outcome")
    if generation.get("max_checks") != plan.get("max_checks"):
        raise ValueError("agent development-check budget differs from reviewed plan")
    if generation.get("outcome") == "submitted" and not re.fullmatch(r"[a-f0-9]{64}", generation.get("submission_sha256", "")):
        raise ValueError("agent submission has no explicit submit hash")
    usage = generation.get("usage", {})
    if not isinstance(usage, dict) or any(type(value) is not int or value < 0 for value in usage.values()):
        raise ValueError("invalid agent usage receipt")
    output = usage.get("output_tokens", usage.get("completion_tokens", 0))
    if output > output_limit(plan, model) * turns:
        raise ValueError("agent usage exceeds recorded turn budgets")
    if "checks" in generation and (type(generation["checks"]) is not int
            or not 0 <= generation["checks"] <= plan.get("max_checks", 3)):
        raise ValueError("invalid agent development check count")
    # Generation receipts must never contain private transcripts, reasoning or
    # tool contents. The approved receipt format only records their counts.
    forbidden = re.compile(r'"(?:messages|reasoning_content|raw_response|response_text|tool_calls|tool_results|authorization|api_key|x-api-key|OPENCODE_GO_API_KEY)"\s*:', re.I)
    if forbidden.search(json.dumps(generation)):
        raise ValueError("private field in agent generation receipt")


def publish_agent(artifacts: Path, plan: dict, run: dict, jobs: list[dict], output: Path, site_output: Path) -> dict:
    if run.get("path") != WORKFLOW or plan.get("protocol") != PROTOCOL:
        raise ValueError("agent publication requires its own workflow and protocol")
    if output.exists() or site_output.exists():
        raise FileExistsError("never overwrite published results")
    prepared = []
    for model, problem, final, _ in slots(plan, run["id"]):
        model_id = model["id"]
        matches = [j for j in jobs if matches_job(j, "Evaluate", model_id, problem)]
        if len(matches) != 1:
            raise ValueError("missing or duplicate agent scoring job")
        job = matches[0]
        if job.get("status") != "completed" or job.get("conclusion") not in {"success", "failure", "timed_out", "cancelled"}:
            raise ValueError("agent scoring job is not completed")
        artifact = artifacts / final
        label = f"opencode-go/{model_id} [{PROTOCOL}]"
        source = None
        if not artifact.exists():
            if job["conclusion"] == "success":
                raise ValueError("successful agent job has no evidence")
            generation = {"model":model_id, "problem":problem, "protocol":PROTOCOL,
                          "outcome":"interrupted", "evidence_unavailable":True, "incomplete_usage":True}
            record = {"problem":problem, "label":label, "group":plan["name"], "attempt":1,
                      "record_origin":"github-job-status-only", "outcome":"scoring_interrupted", "execution_health":"failed", "result":None,
                      "error":"Actions job ended before canonical evidence was uploaded; score and usage are unknown.",
                      "manifest":{"generation":generation}}
        else:
            if json.loads((artifact / "plan.json").read_text()) != plan:
                raise ValueError("agent artifact plan differs from source plan")
            dest = artifact / f"opencode-go-{model_id}" / problem / "rep1"
            generation = json.loads((dest / "generation.json").read_text())
            validate_generation(generation, model, problem, plan)
            github = generation.get("github", {})
            if (github.get("GITHUB_REPOSITORY") != REPOSITORY
                    or str(github.get("GITHUB_RUN_ID")) != str(run["id"])
                    or str(github.get("GITHUB_RUN_ATTEMPT")) != str(run["run_attempt"])
                    or github.get("GITHUB_SHA") != run["head_sha"]):
                raise ValueError("agent generation provenance mismatch")
            candidates = [dest.parent / "rep1_frozen/dut.v", dest / "dut.v"]
            present = [p for p in candidates if p.is_file()]
            if present:
                source = present[0]
                if any(p.read_bytes() != source.read_bytes() for p in present[1:]):
                    raise ValueError("conflicting frozen agent submissions")
            path = dest / "record.json"
            if path.exists():
                record = json.loads(path.read_text())
                if record.get("problem") != problem or record.get("label") != label:
                    raise ValueError("agent record identity mismatch")
                if record.get("manifest", {}).get("generation") != generation:
                    raise ValueError("agent manifest generation mismatch")
            elif job["conclusion"] != "success":
                record = {"problem":problem, "label":label, "group":plan["name"], "attempt":1,
                          "record_origin":"github-generation-only", "outcome":"scoring_interrupted", "execution_health":"failed", "result":None,
                          "error":"Generation evidence was saved but scoring did not finish; score is unknown.",
                          "manifest":{"generation":generation, "submission_sha256":hashlib.sha256(source.read_bytes()).hexdigest() if source else ""}}
            else:
                raise ValueError("successful agent job has no scoring record")
            expected = record.get("manifest", {}).get("submission_sha256", "")
            if generation.get("outcome") == "submitted" and expected != generation["submission_sha256"]:
                raise ValueError("scored source differs from explicit agent submit hash")
            if source:
                if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError("frozen agent submission hash mismatch")
            elif expected or record.get("result") is not None:
                raise ValueError("agent score has no frozen submission")
        record["execution"] = {
            "kind":"model-evaluation", "repository":REPOSITORY, "run_id":run["id"],
            "run_attempt":run["run_attempt"], "job_id":job["id"], "commit":run["head_sha"],
            "completed_at":job["completed_at"], "job_conclusion":job["conclusion"],
        }
        prepared.append((model_id, problem, record, generation, source))
    output.mkdir(parents=True)
    write_json(output / "plan.json", plan)
    for model_id, problem, record, generation, source in prepared:
        dest = output / f"opencode-go-{model_id}" / problem / "rep1"
        write_json(dest / "generation.json", generation)
        write_json(dest / "record.json", record)
        write_json(dest / "manifest.json", record["manifest"])
        if source:
            target = dest.parent / "rep1_frozen/dut.v"
            target.parent.mkdir(parents=True)
            shutil.copyfile(source, target)
    write_report(output)
    export_site(output, site_output, repo_root() / "pilot/sanity.json")
    board_path = site_output / "leaderboard.json"
    board = json.loads(board_path.read_text())
    board["problems"] = [p for p in board["problems"] if p["name"] in plan["problems"]]
    generations = [g for _, _, _, g, _ in prepared]
    board["meta"].update({"git_commit":run["head_sha"], "protocol":PROTOCOL, "repetitions":1,
        "max_turns":plan["max_turns"], "max_output_tokens":plan["max_output_tokens"],
        "output_budget":plan.get("output_budget", "fixed"), "workflow_url":run["html_url"],
        "sandbox":{"mode":"docker · network disabled"},
        "generation_requests":sum(g.get("turns", 0) for g in generations),
        "incomplete_usage":any(g.get("incomplete_usage") for g in generations),
        "incomplete_evidence":any(r.get("record_origin", "scorer") != "scorer" for _, _, r, _, _ in prepared),
        "output_tokens":sum(g.get("usage", {}).get("output_tokens", g.get("usage", {}).get("completion_tokens", 0)) for g in generations)})
    write_json(board_path, board)
    write_json(output / "actions.json", {"run":{k:run[k] for k in ("id", "run_attempt", "head_sha", "html_url", "status", "conclusion", "event", "path") if k in run},
                                          "jobs":[{k:j[k] for k in ("id", "name", "status", "conclusion", "started_at", "completed_at") if k in j} for j in jobs]})
    return board
