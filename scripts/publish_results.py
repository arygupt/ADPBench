"""Data-only Actions publication: validate artifacts, stage a snapshot, open a PR.

Run trusted main-branch code only. Never run artifact code, call a model, change
old scores, approve a PR, or merge a PR. Publication policy is reviewed in main.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from scripts.go_pilot import output_limit, validate_plan, write_json
from scripts.publish_go import REPOSITORY, publish

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_FILES = 300
MAX_API = 2 * 1024 * 1024
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")


def command(args: list[str], cwd: Path | None = None, limit: int = MAX_API) -> bytes:
    """Bound stdout, don't include raw API/provider output in exceptions."""
    with subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        value = proc.stdout.read(limit + 1)
        if len(value) > limit:
            proc.kill()
            raise ValueError("command output exceeds limit")
        if proc.wait(timeout=60) != 0:
            raise RuntimeError(f"{args[0]} failed; check permissions or source metadata")
        return value


def strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("nonfinite JSON value")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            reject(value)
        return parsed

    if len(raw) > MAX_FILE:
        raise ValueError("JSON file exceeds limit")
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject, parse_float=finite_float)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def api(path: str) -> dict:
    return strict_json(command(["gh", "api", f"repos/{REPOSITORY}/{path}"]))


def positive_id(value) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("expected a positive integer ID")
    return value


def source_plan_path(workflow: str, allowed: list[str]) -> str:
    """Read the literal reviewed PLAN from source code; never evaluate YAML/expressions."""
    matches = re.findall(r"^  PLAN: (pilot/[a-z0-9-]+\.json)\s*$", workflow, re.M)
    if len(matches) != 1 or matches[0] not in allowed:
        raise ValueError("source workflow must select exactly one registered literal plan")
    return matches[0]


def validate_source(run: dict, jobs: list[dict], plan: dict, policy: dict) -> None:
    validate_plan(plan)
    positive_id(run["id"])
    positive_id(run["run_attempt"])
    rule = policy["workflows"].get(run.get("path"))
    if (policy.get("schema_version") != 1 or not rule
            or run.get("status") != "completed" or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main"
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_repository", {}).get("full_name") != REPOSITORY
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))
            or run.get("html_url") != f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}"):
        raise ValueError("not an allowed completed main-branch model run")
    names = [f"Evaluate {model['id']} · dot product + GEMV" for model in plan["models"]]
    evaluated = [j for j in jobs if j.get("name") in names]
    if len(evaluated) != len(names) or len({j["name"] for j in evaluated}) != len(names):
        raise ValueError("missing or duplicate model jobs")
    generation_names = [f"Generate {model['id']} · two single-shot submissions" for model in plan["models"]]
    generation_jobs = [j for j in jobs if j.get("name") in generation_names]
    if generation_jobs and (len(generation_jobs) != len(generation_names)
                            or len({j["name"] for j in generation_jobs}) != len(generation_names)):
        raise ValueError("missing or duplicate generation jobs")
    generated = False
    for job in [*evaluated, *generation_jobs]:
        positive_id(job["id"])
        if (job.get("run_id") != run["id"] or job.get("status") != "completed"
                or job.get("conclusion") not in {"success", "failure", "cancelled", "timed_out"}
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", job.get("completed_at", ""))):
            raise ValueError("invalid model job provenance")
        generated |= any(s.get("name") == "Generate two single-shot submissions (no retries or fallback)"
                         and s.get("conclusion") in {"success", "failure"} for s in job.get("steps", []))
    if not generated:
        raise ValueError("preparation-only or duplicate-claim run; no model generation occurred")


def extract_archive(raw: bytes, target: Path) -> None:
    """Validate the entire zip before extracting ordinary files only."""
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("archive too large")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError("expanded archive exceeds limits")
        names = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            mode = (entry.external_attr >> 16) & 0o170000
            if (not name or "\\" in name or ":" in name or path.is_absolute()
                    or any(part in {"..", ".", ""} for part in name.rstrip("/").split("/"))
                    or name.casefold() in names or entry.file_size > MAX_FILE
                    or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                    or entry.flag_bits & 1):
                raise ValueError("unsafe artifact entry")
            names.add(name.casefold())
            if name.endswith(".json") and not entry.is_dir():
                strict_json(archive.read(entry))
        target.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            if entry.is_dir():
                continue
            dest = target / entry.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(entry))


def validate_records(artifacts: Path, plan: dict, run: dict) -> None:
    """Reject fabricated passing records, unexpected budgets and sensitive fields."""
    for model in plan["models"]:
        root = artifacts / f"go-core-{model['id']}-{run['id']}"
        if not root.exists():
            continue  # publish() explicitly distinguishes missing failed-job evidence.
        for problem in plan["problems"]:
            dest = root / f"opencode-go-{model['id']}" / problem / "rep1"
            generation = strict_json((dest / "generation.json").read_bytes())
            if generation.get("max_output_tokens") != output_limit(plan, model):
                raise ValueError("generation budget differs from reviewed plan")
            settings = {k: model[k] for k in ("thinking", "reasoning_effort", "reasoning", "token_limit_key") if k in model}
            if generation.get("generation_settings") != settings:
                raise ValueError("generation settings differ from reviewed plan")
            record_path = dest / "record.json"
            if not record_path.exists():
                continue
            record = strict_json(record_path.read_bytes())
            if record.get("attempt") != 1 or record.get("group") != plan["name"]:
                raise ValueError("unexpected record attempt or group")
            result = record.get("result")
            if result is not None:
                if not isinstance(result, dict) or type(result.get("correct")) is not bool:
                    raise ValueError("invalid scorer result")
                if result["correct"] and (record.get("error") or generation.get("error") or generation.get("invalid_rtl")
                                          or not record.get("audit", {}).get("ok")
                                          or result.get("metadata", {}).get("stage") != "ok"):
                    raise ValueError("passing result contradicts recorded failures")
                for key in ("cells", "cycles", "adp", "ratio"):
                    if type(result.get(key)) not in {int, float}:
                        raise ValueError("invalid numeric score")
                if result["correct"] and (result["cells"] <= 0 or result["cycles"] <= 0
                                          or result["adp"] != result["cells"] * result["cycles"]):
                    raise ValueError("inconsistent passing score")
            serialized = json.dumps(record)
            if re.search(r'"(?:authorization|api_key|x-api-key|OPENCODE_GO_API_KEY)"\s*:', serialized, re.I):
                raise ValueError("sensitive credential field in publication record")


def payload_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.name in {"record.json", "generation.json", "manifest.json", "dut.v", "plan.json"}}


def register(catalog: dict, plan: dict) -> dict:
    name = plan["name"]
    if not SAFE_ID.fullmatch(name) or catalog.get("schema_version") != 1:
        raise ValueError("invalid dataset catalog")
    entry = {"id": name, "label": f"OpenCode Go · {name} · single-shot", "path": f"data/{name}/leaderboard.json", "protocol": "single-shot"}
    existing = next((e for e in catalog["evaluations"] if e["id"] == name), None)
    if existing:
        if existing["path"] != entry["path"] or existing["protocol"] != entry["protocol"]:
            raise ValueError("conflicting dataset catalog entry")
    else:
        catalog["evaluations"].insert(0, entry)
        catalog["default"] = name
    return catalog


def stage_publication(repo: Path, artifacts: Path, plan: dict, run: dict, jobs: list[dict], evidence: list[dict]) -> list[str]:
    name = plan["name"]
    records = repo / "pilot/results" / name
    website = repo / "site/data" / name
    receipt = repo / "pilot/publications" / f"run-{run['id']}-attempt-{run['run_attempt']}.json"
    validate_records(artifacts, plan, run)
    with tempfile.TemporaryDirectory(prefix="adpbench-publish-") as temp:
        draft = Path(temp)
        board = publish(artifacts, plan, run, jobs, draft / "records", draft / "site")
        hashes = payload_files(draft / "records")
        if records.exists() or website.exists():
            if not records.is_dir() or not website.is_dir() or payload_files(records) != hashes:
                raise ValueError("existing immutable results differ from source artifacts")
            previous = strict_json((website / "leaderboard.json").read_bytes())
            if previous["models"] != board["models"] or previous["meta"]["git_commit"] != run["head_sha"]:
                raise ValueError("existing website scores differ from source artifacts")
        else:
            shutil.copytree(draft / "records", records)
            shutil.copytree(draft / "site", website)
    data = {"schema_version": 1, "kind": "validated-model-results-publication",
            "source_run_id": run["id"], "source_attempt": run["run_attempt"],
            "source_commit": run["head_sha"], "source_workflow": run["path"],
            "dataset": name, "confirmed_correct": sum(m["correct"] for m in board["models"]),
            "result_slots": sum(m["attempts"] for m in board["models"]),
            "artifacts": evidence, "record_file_sha256": hashes}
    if receipt.exists() and strict_json(receipt.read_bytes()) != data:
        raise ValueError("conflicting publication receipt")
    write_json(receipt, data)
    catalog_path = repo / "site/data/evaluations.json"
    write_json(catalog_path, register(strict_json(catalog_path.read_bytes()), plan))
    return [str(p.relative_to(repo)) for p in (records, website, receipt, catalog_path)]


def open_results_pr(repo: Path, paths: list[str], run: dict) -> str | None:
    branch = f"automation/results-{run['id']}-{run['run_attempt']}"
    found = json.loads(command(["gh", "pr", "list", "--repo", REPOSITORY, "--state", "all", "--head", branch, "--json", "url,state"]))
    if found:
        # Never overwrite somebody's edits or reopen a deliberately closed PR.
        print(f"Results PR already exists: {found[0]['url']} ({found[0]['state']})")
        return found[0]["url"]
    command(["git", "add", "--", *paths], repo)
    changed = command(["git", "diff", "--cached", "--name-only", "-z"], repo).decode().split("\0")
    changed = [p for p in changed if p]
    if not changed:
        print("Publication already present; no new PR or model calls.")
        return None
    if any(not any(p == allowed or p.startswith(allowed + "/") for allowed in paths) for p in changed):
        raise ValueError("refusing to publish unrelated changes")
    command(["git", "switch", "-c", branch], repo)
    command(["git", "-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
             "commit", "-m", f"results: publish validated Actions run {run['id']}"], repo)
    command(["gh", "auth", "setup-git"], repo)
    remote = command(["git", "ls-remote", "origin", f"refs/heads/{branch}"], repo).strip()
    if remote:
        # Recover a push that succeeded before PR creation failed. A conflicting
        # remote branch is never force-pushed or silently replaced.
        command(["git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}"], repo)
        command(["git", "diff", "--quiet", "HEAD", "FETCH_HEAD"], repo)
    else:
        command(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], repo)
    body = (f"Validated results from {run['html_url']} (attempt {run['run_attempt']}).\n\n"
            "Includes failed and interrupted attempts. No model calls, retries, score changes, or raw API responses. "
            "Source run/commit/plan, artifact digests, record identity, and frozen RTL hashes were checked.\n\n"
            "Review this PR before merging. Approval of queued CI workflows may be needed for bot-created PRs. "
            "A human merge triggers the existing Site workflow. The publisher never approves or merges PRs.")
    url = command(["gh", "pr", "create", "--repo", REPOSITORY, "--base", "main", "--head", branch,
                   "--title", f"results: Actions run {run['id']}", "--body", body], repo).decode().strip()
    print(url)
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--open-pr", action="store_true")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo = args.repo.resolve()
    positive_id(args.run_id)
    if args.open_pr and command(["git", "status", "--porcelain"], repo).strip():
        raise ValueError("publication requires a clean trusted checkout")
    policy = strict_json((repo / "pilot/publication-policy.json").read_bytes())
    run = api(f"actions/runs/{args.run_id}")
    rule = policy["workflows"].get(run.get("path"))
    if not rule:
        raise ValueError("workflow is not registered for publication")
    # Only ancestral main commits; never execute the evaluated revision.
    if not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        raise ValueError("invalid source SHA")
    command(["git", "merge-base", "--is-ancestor", run["head_sha"], "HEAD"], repo)
    command(["git", "diff", "--quiet", run["head_sha"], "HEAD", "--", "problems", "flows"], repo)
    workflow = command(["git", "show", f"{run['head_sha']}:{run['path']}"], repo).decode()
    path = source_plan_path(workflow, rule["plans"])
    plan = strict_json(command(["git", "show", f"{run['head_sha']}:{path}"], repo))
    jobs_response = api(f"actions/runs/{run['id']}/attempts/{positive_id(run['run_attempt'])}/jobs?per_page=100")
    artifacts_response = api(f"actions/runs/{run['id']}/artifacts?per_page=100")
    if jobs_response["total_count"] > 100 or artifacts_response["total_count"] > 100:
        raise ValueError("unexpectedly large source run")
    jobs = jobs_response["jobs"]
    entries = artifacts_response["artifacts"]
    with tempfile.TemporaryDirectory(prefix="adpbench-artifacts-") as temp:
        dest = Path(temp)
        evidence = []
        # The source workflow selects its reviewed plan, not downloaded artifacts.
        validate_source(run, jobs, plan, policy)
        for model in plan["models"]:
            final = f"go-core-{model['id']}-{run['id']}"
            early = f"go-generation-{model['id']}-{run['id']}"
            matches = [a for a in entries if a["name"] == final]
            if not matches:
                matches = [a for a in entries if a["name"] == early]
            if not matches:
                continue
            if len(matches) != 1:
                raise ValueError("ambiguous model artifact")
            artifact = matches[0]
            if (artifact.get("expired") or artifact["size_in_bytes"] > MAX_ARCHIVE
                    or artifact.get("workflow_run", {}).get("id") != run["id"]
                    or artifact.get("workflow_run", {}).get("head_sha") != run["head_sha"]):
                raise ValueError("artifact metadata does not match source run")
            raw = command(["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{positive_id(artifact['id'])}/zip"], limit=MAX_ARCHIVE)
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            if artifact.get("digest") != digest:
                raise ValueError("artifact digest mismatch or unavailable")
            extract_archive(raw, dest / final)
            evidence.append({"id": artifact["id"], "name": artifact["name"], "digest": digest})
        paths = stage_publication(repo, dest, plan, run, jobs, evidence)
    if args.open_pr:
        url = open_results_pr(repo, paths, run)
        if url and os.environ.get("GITHUB_STEP_SUMMARY"):
            if not re.fullmatch(r"https://github.com/arygupt/ADPBench/pull/[0-9]+", url):
                raise ValueError("unexpected results PR URL")
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(f"## Validated results\n\n[Review results PR]({url})\n\nNo new model calls.\n")
    else:
        print(json.dumps({"validated": True, "paths": paths, "model_calls": 0}))


if __name__ == "__main__":
    main()
