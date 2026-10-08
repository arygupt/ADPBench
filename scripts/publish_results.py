"""Data-only Actions publication: validate artifacts, stage a snapshot, open a PR.

    python -m scripts.publish_results --run-id <id> [--open-pr]

Runs trusted main-branch code only. It never runs artifact code, calls a
model, changes old scores, approves a PR, or merges a PR. Which workflows and
plans may publish is set by pilot/publication-policy.json, reviewed in main.

Steps:
  1. Fetch the source run; it must be a completed, manually dispatched main
     run of a registered workflow, at a commit that is an ancestor of HEAD.
  2. Read that run's plan from the workflow file *at that commit*.
  3. Check the run's jobs actually generated something.
  4. Download each artifact, verify its digest, and extract it safely.
  5. Validate every record, then stage results, site data, and a receipt.
  6. Optionally commit the staged files to a branch and open a PR.
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

from adpbench.durable import atomic_json
from scripts.go_pilot import (
    AGENT_PROTOCOLS,
    SETTING_KEYS,
    execution_health,
    is_agent_plan,
    output_limit,
    plan_attempt,
    slot_dir,
    validate_plan,
)
from scripts.publish_agent import AGENT_GENERATION_STEP, GITHUB_TIMESTAMP
from scripts.publish_agent import OUTCOMES as AGENT_OUTCOMES
from scripts.publish_agent import WORKFLOW as AGENT_WORKFLOW
from scripts.publish_agent import matches_job, publish_agent, step_ran, validate_generation, wave_slots
from scripts.publish_agent import slots as agent_slots
from scripts.publish_agent import in_waves, round_meta, round_spend, wave_dir, write_wave_records
from scripts.publish_go import (
    ACTIONS_JOB_FIELDS,
    ACTIONS_RUN_FIELDS,
    REPOSITORY,
    export_leaderboard,
    publish,
)

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_FILES = 300
MAX_API = 2 * 1024 * 1024
MAX_LISTED = 1000  # jobs or artifacts in one source run; a 64-slot round has about 260 artifacts
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")

FINISHED_JOB_CONCLUSIONS = {"success", "failure", "cancelled", "timed_out"}
SINGLE_SHOT_GENERATION_STEP = "Generate two single-shot submissions (no retries or fallback)"
RERUN_PATH = re.compile(r"(?:^|/)rep\d+-t\d+(?:_frozen)?/")
PUBLISHED_FILES = {"record.json", "generation.json", "manifest.json", "dut.v", "plan.json"}
# What a paced round may change between waves: its authorization window and pacing.
WAVE_WINDOW_FIELDS = {"generation_enabled", "expires_at", "schedule", "max_parallel"}

# Published records must never contain these fields.
CREDENTIAL_FIELD = re.compile(r'"(?:authorization|api_key|x-api-key|OPENCODE_GO_API_KEY)"\s*:', re.I)
TRANSCRIPT_FIELD = re.compile(
    r'"(?:messages|reasoning_content|raw_response|response_text|tool_calls|tool_results)"\s*:', re.I
)


# --------------------------------------------------------------------------
# Commands and strict JSON
# --------------------------------------------------------------------------


def command(args: list[str], cwd: Path | None = None, limit: int = MAX_API) -> bytes:
    """Run a command and return its stdout, bounded to `limit` bytes.

    stderr is discarded and never included in exceptions, so raw API or
    provider output cannot leak into logs.
    """
    with subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        output = proc.stdout.read(limit + 1)
        if len(output) > limit:
            proc.kill()
            raise ValueError("command output exceeds limit")
        if proc.wait(timeout=60) != 0:
            raise RuntimeError(f"{args[0]} failed; check permissions or source metadata")
        return output


def strict_json(raw: bytes) -> dict:
    """Parse a JSON object, rejecting duplicate keys, NaN, and infinities."""
    if len(raw) > MAX_FILE:
        raise ValueError("JSON file exceeds limit")
    result = json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def _reject_duplicate_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(name: str):
    # Called for NaN, Infinity and -Infinity.
    raise ValueError("nonfinite JSON value")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # e.g. 1e999
        raise ValueError("nonfinite JSON value")
    return value


def api(path: str) -> dict:
    return strict_json(command(["gh", "api", f"repos/{REPOSITORY}/{path}"]))


def api_list(path: str, key: str) -> list[dict]:
    """Every item of a paginated list endpoint (100 per page), or ValueError if incomplete."""
    items: list[dict] = []
    page = 1
    while True:
        response = api(f"{path}?per_page=100&page={page}")
        total = response.get("total_count")
        batch = response.get(key)
        if type(total) is not int or not isinstance(batch, list) or total > MAX_LISTED:
            raise ValueError("unexpected or unexpectedly large source run listing")
        items.extend(batch)
        if len(items) >= total or not batch:
            break
        page += 1
    if len(items) != total:
        raise ValueError("incomplete source run listing")
    return items


def positive_id(value) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("expected a positive integer ID")
    return value


# --------------------------------------------------------------------------
# Source run
# --------------------------------------------------------------------------


def source_plan_path(workflow: str, allowed: list[str]) -> str:
    """The literal `PLAN:` path in the source workflow file. YAML and expressions are never evaluated."""
    matches = re.findall(r"^  PLAN: (pilot/[a-z0-9-]+\.json)\s*$", workflow, re.M)
    if len(matches) != 1 or matches[0] not in allowed:
        raise ValueError("source workflow must select exactly one registered literal plan")
    return matches[0]


def validate_source(run: dict, jobs: list[dict], plan: dict, policy: dict) -> None:
    """The run must be a completed main-branch dispatch whose jobs really generated results."""
    validate_plan(plan)
    positive_id(run["id"])
    positive_id(run["run_attempt"])
    rule = policy["workflows"].get(run.get("path"))
    if (
        policy.get("schema_version") != 1
        or not rule
        or run.get("status") != "completed"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
        or not COMMIT_SHA.fullmatch(run.get("head_sha", ""))
        or run.get("html_url") != f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}"
    ):
        raise ValueError("not an allowed completed main-branch model run")

    if run.get("path") == AGENT_WORKFLOW:
        _validate_agent_jobs(run, jobs, plan)
    else:
        _validate_single_shot_jobs(run, jobs, plan)


def _validate_agent_jobs(run: dict, jobs: list[dict], plan: dict) -> None:
    if plan.get("protocol") not in AGENT_PROTOCOLS:
        raise ValueError("agent workflow requires agent-assisted protocol")
    if type(plan.get("max_turns")) is not int or not 1 <= plan["max_turns"] <= 100:
        raise ValueError("invalid agent turn budget")

    # An unpaced round runs every slot in one dispatch; a wave of a rerun or
    # paced round runs only the slots that were unclaimed when it started.
    waves = in_waves(plan)
    selected = []
    for model, problem, _, _ in agent_slots(plan, run["id"]):
        pair = [
            [job for job in jobs if matches_job(job, stage, model["id"], problem, plan["protocol"])]
            for stage in ("Generate", "Evaluate")
        ]
        if waves and pair == [[], []]:
            continue
        if any(len(matches) != 1 for matches in pair):
            raise ValueError("missing or duplicate agent slot jobs")
        generate, evaluate = pair[0][0], pair[1][0]
        # A slot released after the usage limit skips its scoring job.
        if waves and evaluate.get("conclusion") == "skipped":
            evaluate = {**evaluate, "conclusion": "success"}
        selected.extend((generate, evaluate))

    for job in selected:
        if not _job_belongs_to_run(job, run):
            raise ValueError("invalid agent slot job provenance")
    if not any(step_ran(job, AGENT_GENERATION_STEP) for job in selected):
        raise ValueError("preparation-only or duplicate-claim agent run")


def _validate_single_shot_jobs(run: dict, jobs: list[dict], plan: dict) -> None:
    if plan.get("protocol", "single-shot") != "single-shot":
        raise ValueError("single-shot workflow cannot publish another protocol")

    evaluate_names = [f"Evaluate {model['id']} · dot product + GEMV" for model in plan["models"]]
    evaluated = [job for job in jobs if job.get("name") in evaluate_names]
    if len(evaluated) != len(evaluate_names) or len({job["name"] for job in evaluated}) != len(evaluate_names):
        raise ValueError("missing or duplicate model jobs")

    # Older runs generated inside the Evaluate job; newer ones have separate Generate jobs.
    generate_names = [f"Generate {model['id']} · two single-shot submissions" for model in plan["models"]]
    generation_jobs = [job for job in jobs if job.get("name") in generate_names]
    if generation_jobs and (
        len(generation_jobs) != len(generate_names)
        or len({job["name"] for job in generation_jobs}) != len(generate_names)
    ):
        raise ValueError("missing or duplicate generation jobs")

    all_jobs = evaluated + generation_jobs
    for job in all_jobs:
        if not _job_belongs_to_run(job, run):
            raise ValueError("invalid model job provenance")
    if not any(step_ran(job, SINGLE_SHOT_GENERATION_STEP) for job in all_jobs):
        raise ValueError("preparation-only or duplicate-claim run; no model generation occurred")


def latest_jobs(jobs: list[dict]) -> list[dict]:
    """Each job once: from the latest attempt that did its work, else the latest attempt.

    A re-run attempt skips a slot it finds already claimed, so the earlier
    attempt's jobs hold that slot's generation and score. A skipped reusable
    job keeps its `${{ inputs.* }}` name, so jobs are matched by caller and stage.
    """
    def key(job: dict) -> tuple[str, str]:
        caller, nested, inner = job.get("name", "").partition(" / ")
        return (caller, inner.split(" ")[0]) if nested else (caller, "")

    def rank(job: dict) -> tuple[bool, bool, int]:
        return (job.get("conclusion") != "skipped", step_ran(job, AGENT_GENERATION_STEP), job.get("run_attempt", 1))

    latest: dict[tuple[str, str], dict] = {}
    for job in jobs:
        current = latest.get(key(job))
        if current is None or rank(job) > rank(current):
            latest[key(job)] = job
    return list(latest.values())


def _job_belongs_to_run(job: dict, run: dict) -> bool:
    positive_id(job["id"])
    return (
        job.get("run_id") == run["id"]
        and job.get("status") == "completed"
        and job.get("conclusion") in FINISHED_JOB_CONCLUSIONS
        and bool(GITHUB_TIMESTAMP.fullmatch(job.get("completed_at", "")))
    )


# --------------------------------------------------------------------------
# Artifacts and records
# --------------------------------------------------------------------------


def extract_archive(raw: bytes, target: Path) -> None:
    """Validate the whole zip first, then extract only ordinary files."""
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("archive too large")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError("expanded archive exceeds limits")

        seen_names = set()
        for entry in entries:
            if not _is_safe_entry(entry, seen_names):
                raise ValueError("unsafe artifact entry")
            seen_names.add(entry.filename.casefold())
            if entry.filename.endswith(".json") and not entry.is_dir():
                strict_json(archive.read(entry))

        target.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            if entry.is_dir():
                continue
            dest = target / entry.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(entry))


def _is_safe_entry(entry: zipfile.ZipInfo, seen_names: set[str]) -> bool:
    """A relative, non-traversing, case-unique, unencrypted regular file or directory."""
    name = entry.filename
    file_type = (entry.external_attr >> 16) & 0o170000
    parts = name.rstrip("/").split("/")
    return not (
        not name
        or "\\" in name
        or ":" in name
        or PurePosixPath(name).is_absolute()
        or any(part in {"..", ".", ""} for part in parts)
        or name.casefold() in seen_names
        or entry.file_size > MAX_FILE
        or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}
        or entry.flag_bits & 1  # encrypted
    )


def validate_records(artifacts: Path, plan: dict, run: dict) -> None:
    """Reject fabricated passing records, unexpected budgets, and sensitive fields."""
    agent_track = is_agent_plan(plan)
    if agent_track:
        planned = [(model, problem, artifacts / name) for model, problem, name, _ in agent_slots(plan, run["id"])]
    else:
        planned = [
            (model, problem, artifacts / f"go-core-{model['id']}-{run['id']}")
            for model in plan["models"]
            for problem in plan["problems"]
        ]
    for model, problem, artifact in planned:
        if not artifact.exists():
            continue  # publish() distinguishes missing evidence from failed jobs
        dest = slot_dir(artifact, model["id"], problem, plan_attempt(plan))

        generation = strict_json((dest / "generation.json").read_bytes())
        if agent_track:
            validate_generation(generation, model, problem, plan)
        if generation.get("max_output_tokens") != output_limit(plan, model):
            raise ValueError("generation budget differs from reviewed plan")
        settings = {key: model[key] for key in SETTING_KEYS if key in model}
        if generation.get("generation_settings") != settings:
            raise ValueError("generation settings differ from reviewed plan")

        record_path = dest / "record.json"
        if not record_path.exists():
            continue
        record = strict_json(record_path.read_bytes())
        if record.get("attempt") != plan_attempt(plan) or record.get("group") != plan["name"]:
            raise ValueError("unexpected record attempt or group")
        if agent_track:
            _validate_agent_record(record, generation)
        if record.get("result") is not None:
            _validate_result(record, generation)

        serialized = json.dumps(record)
        if CREDENTIAL_FIELD.search(serialized):
            raise ValueError("sensitive credential field in publication record")
        if agent_track and TRANSCRIPT_FIELD.search(serialized):
            raise ValueError("private transcript field in agent publication record")


def _validate_agent_record(record: dict, generation: dict) -> None:
    """The typed outcome, execution health, and score must all tell the same story."""
    outcome = record.get("outcome")
    result = record.get("result")
    audit_ok = record.get("audit", {}).get("ok")
    if outcome not in {"correct", "incorrect", "audit_rejected", "scoring_interrupted", *AGENT_OUTCOMES}:
        raise ValueError("unknown agent record outcome")
    if record.get("execution_health") not in {"completed", "failed"}:
        raise ValueError("missing agent execution health")
    if record["execution_health"] != execution_health(outcome):
        raise ValueError("agent execution health contradicts typed outcome")
    if (outcome == "correct") != bool(result and result.get("correct")):
        raise ValueError("agent outcome contradicts measured correctness")
    if outcome == "incorrect" and (result is None or record.get("error") or not audit_ok):
        raise ValueError("incorrect agent outcome requires completed scoring")
    if outcome == "audit_rejected" and (audit_ok or generation["outcome"] != "submitted"):
        raise ValueError("agent audit outcome contradicts submission")
    if outcome in AGENT_OUTCOMES and outcome != generation["outcome"]:
        raise ValueError("agent generation and record outcomes disagree")
    if result is not None and generation.get("outcome") != "submitted":
        raise ValueError("agent score has no submitted generation")
    if result is not None and outcome not in {"correct", "incorrect", "scoring_interrupted"}:
        raise ValueError("agent outcome contradicts scoring result")
    if generation["outcome"] == "submitted":
        if record.get("manifest", {}).get("submission_sha256") != generation["submission_sha256"]:
            raise ValueError("agent frozen hash differs from explicit submit receipt")


def _validate_result(record: dict, generation: dict) -> None:
    """A scorer result must be well-formed, and a pass must have no recorded failure."""
    result = record["result"]
    if not isinstance(result, dict) or type(result.get("correct")) is not bool:
        raise ValueError("invalid scorer result")
    if result["correct"]:
        has_failure = (
            record.get("error")
            or generation.get("error")
            or generation.get("invalid_rtl")
            or not record.get("audit", {}).get("ok")
            or result.get("metadata", {}).get("stage") != "ok"
        )
        if has_failure:
            raise ValueError("passing result contradicts recorded failures")
    for key in ("cells", "cycles", "adp", "ratio"):
        if type(result.get(key)) not in {int, float}:
            raise ValueError("invalid numeric score")
    if result["correct"]:
        if result["cells"] <= 0 or result["cycles"] <= 0 or result["adp"] != result["cells"] * result["cycles"]:
            raise ValueError("inconsistent passing score")


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------


def payload_files(root: Path) -> dict[str, str]:
    """sha256 of every published file under `root`, keyed by relative path."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name in PUBLISHED_FILES
    }


def register(catalog: dict, plan: dict) -> dict:
    """Add the plan's dataset to the site catalog (as the new default), if not already there."""
    name = plan["name"]
    if not SAFE_ID.fullmatch(name) or catalog.get("schema_version") != 1:
        raise ValueError("invalid dataset catalog")
    protocol = plan.get("protocol", "single-shot")
    if protocol not in {"single-shot", *AGENT_PROTOCOLS}:
        raise ValueError("unregistered protocol")

    entry = {
        "id": name,
        "label": f"OpenCode Go · {name} · {protocol}",
        "path": f"data/{name}/leaderboard.json",
        "protocol": protocol,
    }
    existing = next((item for item in catalog["evaluations"] if item["id"] == name), None)
    if existing is None:
        catalog["evaluations"].insert(0, entry)
        catalog["default"] = name
    elif existing["path"] != entry["path"] or existing["protocol"] != entry["protocol"]:
        raise ValueError("conflicting dataset catalog entry")
    return catalog


def stage_publication(
    repo: Path, artifacts: Path, plan: dict, run: dict, jobs: list[dict], evidence: list[dict]
) -> list[str]:
    """Write results, site data, a receipt, and the catalog entry. Returns the changed paths.

    Publishing the same run twice is a no-op; publishing different content
    over existing results is an error.
    """
    name = plan["name"]
    records_dir = repo / "pilot/results" / name
    site_dir = repo / "site/data" / name
    receipt_path = repo / "pilot/publications" / f"run-{run['id']}-attempt-{run['run_attempt']}.json"
    validate_records(artifacts, plan, run)
    if in_waves(plan):
        return _stage_wave(repo, artifacts, plan, run, jobs, evidence)

    with tempfile.TemporaryDirectory(prefix="adpbench-publish-") as temp:
        draft = Path(temp)
        publisher = publish_agent if is_agent_plan(plan) else publish
        board = publisher(artifacts, plan, run, jobs, draft / "records", draft / "site")
        hashes = payload_files(draft / "records")
        if records_dir.exists() or site_dir.exists():
            # Reruns published since then add `rep<N>-t<K>` files and change the scores.
            first_try = {
                path: digest for path, digest in payload_files(records_dir).items()
                if not RERUN_PATH.search(path)
            } if records_dir.is_dir() else None
            if not site_dir.is_dir() or first_try != hashes:
                raise ValueError("existing immutable results differ from source artifacts")
            previous = strict_json((site_dir / "leaderboard.json").read_bytes())
            rescored = bool(previous["meta"].get("reruns"))
            if (not rescored and previous["models"] != board["models"]) or previous["meta"]["git_commit"] != run["head_sha"]:
                raise ValueError("existing website scores differ from source artifacts")
        else:
            shutil.copytree(draft / "records", records_dir)
            shutil.copytree(draft / "site", site_dir)

    receipt = {
        "schema_version": 1,
        "kind": "validated-model-results-publication",
        "source_run_id": run["id"],
        "source_attempt": run["run_attempt"],
        "source_commit": run["head_sha"],
        "source_workflow": run["path"],
        "dataset": name,
        "confirmed_correct": sum(model["correct"] for model in board["models"]),
        "result_slots": sum(model["attempts"] for model in board["models"]),
        "artifacts": evidence,
        "record_file_sha256": hashes,
    }
    if receipt_path.exists() and strict_json(receipt_path.read_bytes()) != receipt:
        raise ValueError("conflicting publication receipt")
    atomic_json(receipt_path, receipt)

    catalog_path = repo / "site/data/evaluations.json"
    atomic_json(catalog_path, register(strict_json(catalog_path.read_bytes()), plan))
    return [str(path.relative_to(repo)) for path in (records_dir, site_dir, receipt_path, catalog_path)]


def _stage_wave(
    repo: Path, artifacts: Path, plan: dict, run: dict, jobs: list[dict], evidence: list[dict]
) -> list[str]:
    """Add one wave's finished slots to its round, then rebuild the round's site data.

    A paced round's first wave creates the round; later waves and reruns add
    to it. The round's existing files never change. Republishing the same
    wave is a no-op; different content for an already published slot is an
    error. The catalog is left alone: a paced round goes on the site by a
    reviewed change once it is complete.
    """
    name = plan["name"]
    records_dir = repo / "pilot/results" / name
    site_dir = repo / "site/data" / name
    receipt_path = repo / "pilot/publications" / f"run-{run['id']}-attempt-{run['run_attempt']}.json"
    first_try = plan.get("try", 1) == 1
    published = (records_dir / "plan.json").is_file() and (site_dir / "leaderboard.json").is_file()
    if not published and not first_try:
        raise ValueError("a rerun publishes into an already published round")
    if not published and (records_dir.exists() or site_dir.exists()):
        raise ValueError("existing immutable results differ from source artifacts")
    if published and first_try and _wave_settings(strict_json((records_dir / "plan.json").read_bytes())) != _wave_settings(plan):
        raise ValueError("a wave must run its round's reviewed settings")

    with tempfile.TemporaryDirectory(prefix="adpbench-publish-") as temp:
        draft = Path(temp) / "records"
        if published:
            shutil.copytree(records_dir, draft)
        else:
            draft.mkdir()
            atomic_json(draft / "plan.json", plan)
        before = payload_files(draft)
        prepared = wave_slots(artifacts, plan, run, jobs, draft)
        if not prepared:
            print("No finished slots in this wave; nothing to publish.")
            return []
        added = [slot for slot in prepared if not wave_dir(draft, slot["model_id"], slot["problem"], plan).exists()]
        with tempfile.TemporaryDirectory(prefix="adpbench-wave-") as fresh:
            # Already published slots must match this wave exactly.
            write_wave_records(Path(fresh), plan, prepared)
            ours = payload_files(Path(fresh))
        write_wave_records(draft, plan, added)
        hashes = payload_files(draft)
        if any(hashes.get(path) != digest for path, digest in {**before, **ours}.items()):
            raise ValueError("wave content differs from the published round")
        if added:
            board = _rebuild_round_site(draft, Path(temp) / "site", site_dir if published else None, plan, run)
            if published:
                shutil.rmtree(records_dir)
            shutil.copytree(draft, records_dir)
            atomic_json(records_dir / f"actions-run-{run['id']}.json", _actions(run, jobs))
            site_dir.mkdir(parents=True, exist_ok=True)
            atomic_json(site_dir / "leaderboard.json", board)
            for extra in ("problems.json", "report.json"):
                shutil.copyfile(Path(temp) / "site" / extra, site_dir / extra)
    if first_try:
        # Like a whole round's receipt, a first-try wave binds the round's plan.
        ours["plan.json"] = hashes["plan.json"]

    records = [slot["record"] for slot in prepared]
    receipt = {
        "schema_version": 1,
        "kind": "validated-model-results-publication",
        "source_run_id": run["id"],
        "source_attempt": run["run_attempt"],
        "source_commit": run["head_sha"],
        "source_workflow": run["path"],
        "dataset": name,
        "try": plan.get("try", 1),
        "confirmed_correct": sum(record.get("outcome") == "correct" for record in records),
        "result_slots": len(records),
        "artifacts": evidence,
        "record_file_sha256": ours,
    }
    if receipt_path.exists() and strict_json(receipt_path.read_bytes()) != receipt:
        raise ValueError("conflicting publication receipt")
    atomic_json(receipt_path, receipt)
    return [str(path.relative_to(repo)) for path in (records_dir, site_dir, receipt_path)]


def _wave_settings(plan: dict) -> dict:
    """A paced round's plan without the fields its waves may change."""
    settings = {key: value for key, value in plan.items() if key not in WAVE_WINDOW_FIELDS}
    settings["models"] = [{key: value for key, value in model.items() if key != "not_before"} for model in plan["models"]]
    return settings


def _rebuild_round_site(records: Path, scratch: Path, site_dir: Path | None, plan: dict, run: dict) -> dict:
    """The round's leaderboard over every counted try, keeping the first wave's provenance.

    `site_dir` is None for a paced round's first wave, which has no leaderboard yet.
    """
    round_plan = strict_json((records / "plan.json").read_bytes())
    board = export_leaderboard(records, scratch, round_plan)
    # Spend counts every try, including voided ones.
    generations = [strict_json(path.read_bytes()) for path in sorted(records.glob("*/*/rep*/generation.json"))]
    tries = [strict_json(path.read_bytes()) for path in sorted(records.glob("*/*/rep*/record.json"))]
    if site_dir is None:
        board["meta"].update(round_meta(round_plan, run, generations, tries))
        return board
    meta = {**strict_json((site_dir / "leaderboard.json").read_bytes())["meta"], **round_spend(generations, tries)}
    if plan.get("try", 1) > 1:
        meta["reruns"] = [
            *[r for r in meta.get("reruns", []) if r["workflow_url"] != run["html_url"]],
            {"try": plan["try"], "workflow_url": run["html_url"], "git_commit": run["head_sha"]},
        ]
    elif meta["workflow_url"] != run["html_url"]:
        meta["waves"] = [
            *[w for w in meta.get("waves", []) if w["workflow_url"] != run["html_url"]],
            {"workflow_url": run["html_url"], "git_commit": run["head_sha"]},
        ]
    board["meta"] = meta
    return board


def _actions(run: dict, jobs: list[dict]) -> dict:
    return {
        "run": {field: run[field] for field in ACTIONS_RUN_FIELDS if field in run},
        "jobs": [{field: job[field] for field in ACTIONS_JOB_FIELDS if field in job} for job in jobs],
    }


def open_results_pr(repo: Path, paths: list[str], run: dict) -> str | None:
    """Commit the staged paths to a new branch and open a PR. Returns its URL, if any."""
    branch = f"automation/results-{run['id']}-{run['run_attempt']}"
    existing = json.loads(
        command(["gh", "pr", "list", "--repo", REPOSITORY, "--state", "all", "--head", branch, "--json", "url,state"])
    )
    if existing:
        # Never overwrite somebody's edits or reopen a deliberately closed PR.
        print(f"Results PR already exists: {existing[0]['url']} ({existing[0]['state']})")
        return existing[0]["url"]

    command(["git", "add", "--", *paths], repo)
    changed = command(["git", "diff", "--cached", "--name-only", "-z"], repo).decode().split("\0")
    changed = [path for path in changed if path]
    if not changed:
        print("Publication already present; no new PR or model calls.")
        return None
    for path in changed:
        if not any(path == allowed or path.startswith(allowed + "/") for allowed in paths):
            raise ValueError("refusing to publish unrelated changes")

    command(["git", "switch", "-c", branch], repo)
    command(
        [
            "git",
            "-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"results: publish validated Actions run {run['id']}",
        ],
        repo,
    )
    command(["gh", "auth", "setup-git"], repo)
    remote = command(["git", "ls-remote", "origin", f"refs/heads/{branch}"], repo).strip()
    if remote:
        # An earlier push succeeded but PR creation failed. Only continue if the
        # remote branch is identical; never force-push or silently replace it.
        command(["git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}"], repo)
        command(["git", "diff", "--quiet", "HEAD", "FETCH_HEAD"], repo)
    else:
        command(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], repo)

    body = (
        f"Validated results from {run['html_url']} (attempt {run['run_attempt']}).\n\n"
        "Includes failed and interrupted attempts. No model calls, retries, score changes, or raw "
        "API responses. Source run/commit/plan, artifact digests, record identity, and frozen RTL "
        "hashes were checked.\n\n"
        "Review this PR before merging. Approval of queued CI workflows may be needed for "
        "bot-created PRs. A human merge triggers the existing Site workflow. The publisher never "
        "approves or merges PRs."
    )
    url = command(
        [
            "gh", "pr", "create", "--repo", REPOSITORY, "--base", "main", "--head", branch,
            "--title", f"results: Actions run {run['id']}", "--body", body,
        ],
        repo,
    ).decode().strip()
    print(url)
    return url


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


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

    # 1. The source run, from a registered workflow at an ancestor of HEAD.
    policy = strict_json((repo / "pilot/publication-policy.json").read_bytes())
    run = api(f"actions/runs/{args.run_id}")
    rule = policy["workflows"].get(run.get("path"))
    if not rule:
        raise ValueError("workflow is not registered for publication")
    if not COMMIT_SHA.fullmatch(run.get("head_sha", "")):
        raise ValueError("invalid source SHA")
    # Only ancestral main commits whose problems and flows match HEAD; the
    # evaluated revision's code is never executed.
    command(["git", "merge-base", "--is-ancestor", run["head_sha"], "HEAD"], repo)
    command(["git", "diff", "--quiet", run["head_sha"], "HEAD", "--", "problems", "flows"], repo)

    # 2. The plan that workflow file selected at that commit.
    workflow = command(["git", "show", f"{run['head_sha']}:{run['path']}"], repo).decode()
    plan_path = source_plan_path(workflow, rule["plans"])
    plan = strict_json(command(["git", "show", f"{run['head_sha']}:{plan_path}"], repo))

    jobs = latest_jobs(
        [
            job
            for attempt in range(1, positive_id(run["run_attempt"]) + 1)
            for job in api_list(f"actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs")
        ]
    )
    available = api_list(f"actions/runs/{run['id']}/artifacts", "artifacts")

    # 3. The jobs; 4. the artifacts; 5. validation and staging.
    with tempfile.TemporaryDirectory(prefix="adpbench-artifacts-") as temp:
        downloads = Path(temp)
        validate_source(run, jobs, plan, policy)
        evidence = []
        for final_name, early_name in _expected_artifacts(plan, run):
            artifact = _pick_artifact(available, final_name, early_name)
            if artifact is None:
                continue
            _check_artifact_metadata(artifact, run)
            raw = command(
                ["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{positive_id(artifact['id'])}/zip"],
                limit=MAX_ARCHIVE,
            )
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            if artifact.get("digest") != digest:
                raise ValueError("artifact digest mismatch or unavailable")
            extract_archive(raw, downloads / final_name)
            evidence.append({"id": artifact["id"], "name": artifact["name"], "digest": digest})
        paths = stage_publication(repo, downloads, plan, run, jobs, evidence)

    # 6. The PR.
    if not paths:
        print(json.dumps({"validated": True, "paths": [], "model_calls": 0}))
        return
    if not args.open_pr:
        print(json.dumps({"validated": True, "paths": paths, "model_calls": 0}))
        return
    url = open_results_pr(repo, paths, run)
    if url and os.environ.get("GITHUB_STEP_SUMMARY"):
        if not re.fullmatch(r"https://github.com/arygupt/ADPBench/pull/[0-9]+", url):
            raise ValueError("unexpected results PR URL")
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(f"## Validated results\n\n[Review results PR]({url})\n\nNo new model calls.\n")


def _expected_artifacts(plan: dict, run: dict) -> list[tuple[str, str]]:
    """(final artifact name, earlier backup name) for each expected artifact."""
    if is_agent_plan(plan):
        return [(final, early) for _, _, final, early in agent_slots(plan, run["id"])]
    return [
        (f"go-core-{model['id']}-{run['id']}", f"go-generation-{model['id']}-{run['id']}")
        for model in plan["models"]
    ]


def _pick_artifact(available: list[dict], final_name: str, early_name: str) -> dict | None:
    """The final artifact, else the earlier generation backup, else None."""
    matches = [artifact for artifact in available if artifact["name"] == final_name]
    if not matches:
        matches = [artifact for artifact in available if artifact["name"] == early_name]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("ambiguous model artifact")
    return matches[0]


def _check_artifact_metadata(artifact: dict, run: dict) -> None:
    source_run = artifact.get("workflow_run", {})
    if (
        artifact.get("expired")
        or artifact["size_in_bytes"] > MAX_ARCHIVE
        or source_run.get("id") != run["id"]
        or source_run.get("head_sha") != run["head_sha"]
    ):
        raise ValueError("artifact metadata does not match source run")


if __name__ == "__main__":
    main()
