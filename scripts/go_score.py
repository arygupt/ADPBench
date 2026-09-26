"""Supervise one isolated, credential-free scoring container per problem.

    python -m scripts.go_score --plan <plan> --model <id> --out <dir> \\
        --checkpoints <dir> --diagnostics <dir> [--problem <id>] [--resume]

Each problem is scored by `python -m scripts.go_pilot score` inside a locked
down container with the repository mounted read-only. The outer deadline
leaves time for Actions artifact uploads. Checkpoints and logs are mounted
separately from the small canonical publication artifact (`--out`).

Generation is never retried, and neither is a completed scoring result -
including an incorrect one. A crashed container is recorded as a scoring
infrastructure failure with the score unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from adpbench.durable import atomic_json
from adpbench.problem import repo_root
from adpbench.process import is_infrastructure_failure, run_logged
from scripts.go_pilot import (
    SCORING_IMAGE,
    is_agent_plan,
    plan_attempt,
    read_plan,
    save_scoring_failure,
    select_model,
    slot_dir,
    summarize,
)

MAX_DEADLINE_S = 1200
DOCKER_TIMEOUT_S = 30


def docker(*args: str) -> subprocess.CompletedProcess:
    """Run a short docker CLI command. Arguments never include a key or model-supplied text."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)


def isolated_container_flags() -> list[str]:
    """`docker run` flags shared by every scoring and dev-check container.

    No network, bounded memory/CPU/processes, the invoking user rather than
    root, and no Linux capabilities or privilege escalation.
    """
    return [
        "--network", "none",
        "--memory", "5g",
        "--memory-swap", "5g",
        "--cpus", "2",
        "--pids-limit", "256",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]


def failure_reason(process: dict, container: dict) -> str:
    """Why a scoring container failed, or "" if it did not."""
    if container.get("OOMKilled"):
        return "out_of_memory"
    if process.get("timed_out"):
        return "wall_timeout"
    if process.get("reason"):
        return process["reason"]
    if container.get("ExitCode", 0):
        return "container_error"
    return ""


def supervise(
    plan_path: Path,
    model_id: str,
    out: Path,
    checkpoints: Path,
    diagnostics: Path,
    *,
    image: str = SCORING_IMAGE,
    deadline_s: int = MAX_DEADLINE_S,
    resume: bool = False,
    problem_id: str | None = None,
) -> bool:
    """Score each planned problem (or just `problem_id`). Returns False if anything failed.

    A wrong design is a valid, healthy measurement; only infrastructure
    failures (crashes, timeouts, missing records) make this return False.
    """
    root = repo_root().resolve()
    plan_path = plan_path.resolve()
    relative_plan = plan_path.relative_to(root)  # only a reviewed plan inside the read-only repo
    plan = read_plan(plan_path)
    model = select_model(plan, model_id)
    if problem_id is not None and problem_id not in plan["problems"]:
        raise ValueError("problem is not in reviewed plan")

    out = out.resolve()
    checkpoints = checkpoints.resolve()
    diagnostics = diagnostics.resolve()
    for path in (out, checkpoints, diagnostics):
        path.mkdir(parents=True, exist_ok=True)
    if json.loads((out / "plan.json").read_text()) != plan:
        raise ValueError("saved generation plan differs from the scoring plan")
    if not 0 < deadline_s <= MAX_DEADLINE_S:
        raise ValueError("scoring deadline must leave time for two problems and artifact uploads")

    healthy = True
    for problem in plan["problems"]:
        if problem_id is not None and problem != problem_id:
            continue
        if not (slot_dir(out, model_id, problem, plan_attempt(plan)) / "generation.json").is_file():
            raise ValueError("missing scheduled generation slot")

        name = "adpbench-score-" + uuid4().hex
        command = _scoring_command(
            name, image, relative_plan, model_id, problem, root, out, checkpoints, deadline_s, resume
        )
        diagnostic = diagnostics / model_id / problem / uuid4().hex[:12]
        diagnostic.mkdir(parents=True)
        print(f"{model_id}/{problem}: isolated scoring started (wall budget {deadline_s}s)", flush=True)
        if not _run_scoring_container(name, command, diagnostic, deadline_s, plan, model, out, problem):
            healthy = False
        if not _record_is_healthy(plan, model, out, problem):
            healthy = False
        summarize(plan, model, out)
        print(f"{model_id}/{problem}: evidence saved; healthy_so_far={healthy}", flush=True)
    return healthy


def _scoring_command(
    name: str,
    image: str,
    relative_plan: Path,
    model_id: str,
    problem: str,
    root: Path,
    out: Path,
    checkpoints: Path,
    deadline_s: int,
    resume: bool,
) -> list[str]:
    command = [
        "docker", "run", "--name", name,
        *isolated_container_flags(),
        "-e", "PYTHONPATH=/repo",
        "-e", "ADPBENCH_WORKDIR=/checkpoints/scratch",
        "-v", f"{root}:/repo:ro",
        "-v", f"{out}:/output",
        "-v", f"{checkpoints}:/checkpoints",
        "-w", "/repo",
        image,
        "python", "-m", "scripts.go_pilot", "score",
        "--plan", str(relative_plan),
        "--model", model_id,
        "--problem", problem,
        "--out", "/output",
        "--checkpoint-root", "/checkpoints",
        "--deadline-s", str(deadline_s),
    ]
    if resume:
        command.append("--resume")
    return command


def _run_scoring_container(
    name: str,
    command: list[str],
    diagnostic: Path,
    deadline_s: int,
    plan: dict,
    model: dict,
    out: Path,
    problem: str,
) -> bool:
    """Run one scoring container and record its final state. Returns False on failure."""
    process = {"reason": "supervisor_interrupted"}
    state = {}
    healthy = True
    try:
        process = run_logged(command, diagnostic, "container", timeout=deadline_s + 60)
    finally:
        try:
            state = _container_state(name)
            if state.get("Running"):
                docker("stop", "--time", "10", name)
                state = _container_state(name) or state
            reason = failure_reason(process, state)
            atomic_json(
                diagnostic / "container-state.json",
                {
                    "reason": reason,
                    "oom_killed": bool(state.get("OOMKilled")),
                    "exit_code": state.get("ExitCode"),
                    "process": {key: value for key, value in process.items() if key != "log"},
                },
            )
            if reason:
                save_scoring_failure(plan, model, out, problem, reason)
                healthy = False
        finally:
            # Remove only the uniquely named container this invocation owns.
            docker("rm", "--force", name)
    return healthy


def _container_state(name: str) -> dict:
    """`docker inspect` State for a container, or {} if unavailable."""
    info = docker("inspect", "--format", "{{json .State}}", name)
    return json.loads(info.stdout) if info.returncode == 0 else {}


def _record_is_healthy(plan: dict, model: dict, out: Path, problem: str) -> bool:
    record_path = slot_dir(out, model["id"], problem, plan_attempt(plan)) / "record.json"
    if not record_path.exists():
        save_scoring_failure(plan, model, out, problem, "missing_final_record")
        return False

    record = json.loads(record_path.read_text())
    result = record.get("result") or {}
    failure = result.get("metadata", {}).get("failure_kind", "")
    # Wrong RTL or a protocol failure is still a valid measurement.
    if is_agent_plan(plan):
        has_error = record.get("execution_health") == "failed"
    else:
        has_error = bool(record.get("error"))
    return not (has_error or is_infrastructure_failure(failure))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--problem")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--checkpoints", required=True, type=Path)
    parser.add_argument("--diagnostics", required=True, type=Path)
    parser.add_argument("--image", default=SCORING_IMAGE)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    healthy = supervise(
        args.plan,
        args.model,
        args.out,
        args.checkpoints,
        args.diagnostics,
        image=args.image,
        resume=args.resume,
        problem_id=args.problem,
    )
    if not healthy:
        raise SystemExit(
            "Scoring or generation infrastructure failed; inspect saved records and diagnostics"
        )


if __name__ == "__main__":
    main()
