"""Supervise one isolated scoring container per problem, without model credentials.

Outer deadlines leave time for Actions artifact uploads. Checkpoints/logs are
host-mounted separately from the small canonical publication artifact. Never
retry generation or a completed scoring result, including an incorrect result.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from adpbench.durable import atomic_json
from adpbench.process import run_logged
from adpbench.problem import repo_root
from scripts.go_pilot import read_plan, select_model, save_scoring_failure, summarize


def docker(*args: str) -> subprocess.CompletedProcess:
    # Commands contain no model key and no agent-supplied shell fragments.
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)


def failure_reason(process: dict, container: dict) -> str:
    if container.get("OOMKilled"):
        return "out_of_memory"
    if process.get("timed_out"):
        return "wall_timeout"
    if process.get("reason"):
        return process["reason"]
    if container.get("ExitCode", 0):
        return "container_error"
    return ""


def supervise(plan_path: Path, model_id: str, out: Path, checkpoints: Path,
              diagnostics: Path, *, image: str = "adpbench-go:ci", deadline_s: int = 1200,
              resume: bool = False, problem_id: str | None = None) -> bool:
    root = repo_root().resolve()
    plan_path = plan_path.resolve()
    relative_plan = plan_path.relative_to(root)  # Only the read-only reviewed repo plan.
    plan = read_plan(plan_path)
    model = select_model(plan, model_id)
    if problem_id is not None and problem_id not in plan["problems"]:
        raise ValueError("problem is not in reviewed plan")
    out, checkpoints, diagnostics = (p.resolve() for p in (out, checkpoints, diagnostics))
    for path in (out, checkpoints, diagnostics):
        path.mkdir(parents=True, exist_ok=True)
    if json.loads((out / "plan.json").read_text()) != plan:
        raise ValueError("saved generation plan differs from the scoring plan")
    if not 0 < deadline_s <= 1200:
        raise ValueError("scoring deadline must leave time for two problems and artifact uploads")
    healthy = True
    for problem in plan["problems"]:
        if problem_id is not None and problem != problem_id:
            continue
        destination = out / f"opencode-go-{model_id}" / problem / "rep1"
        if not (destination / "generation.json").is_file():
            raise ValueError("missing scheduled generation slot")
        diagnostic = diagnostics / model_id / problem / uuid4().hex[:12]
        diagnostic.mkdir(parents=True)
        name = "adpbench-score-" + uuid4().hex
        command = ["docker", "run", "--name", name, "--network", "none", "--memory", "5g",
                   "--memory-swap", "5g", "--cpus", "2", "--pids-limit", "256",
                   "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "-e", "PYTHONPATH=/repo",
                   "-e", "ADPBENCH_WORKDIR=/checkpoints/scratch",
                   "-v", f"{root}:/repo:ro", "-v", f"{out}:/output",
                   "-v", f"{checkpoints}:/checkpoints", "-w", "/repo", image,
                   "python", "-m", "scripts.go_pilot", "score", "--plan", str(relative_plan),
                   "--model", model_id, "--problem", problem, "--out", "/output",
                   "--checkpoint-root", "/checkpoints", "--deadline-s", str(deadline_s)]
        if resume:
            command.append("--resume")
        process = {"reason":"supervisor_interrupted"}
        state = {}
        print(f"{model_id}/{problem}: isolated scoring started (wall budget {deadline_s}s)", flush=True)
        try:
            process = run_logged(command, diagnostic, "container", timeout=deadline_s + 60)
        finally:
            try:
                info = docker("inspect", "--format", "{{json .State}}", name)
                if info.returncode == 0:
                    state = json.loads(info.stdout)
                    if state.get("Running"):
                        docker("stop", "--time", "10", name)
                        info = docker("inspect", "--format", "{{json .State}}", name)
                        if info.returncode == 0:
                            state = json.loads(info.stdout)
                reason = failure_reason(process, state)
                atomic_json(diagnostic / "container-state.json", {
                    "reason":reason, "oom_killed":bool(state.get("OOMKilled")),
                    "exit_code":state.get("ExitCode"), "process":{k:v for k,v in process.items() if k != "log"}})
                if reason:
                    save_scoring_failure(plan, model, out, problem, reason)
                    healthy = False
            finally:
                # Remove only the uniquely named container this invocation owns.
                docker("rm", "--force", name)
        record_path = destination / "record.json"
        if not record_path.exists():
            save_scoring_failure(plan, model, out, problem, "missing_final_record")
            healthy = False
        else:
            record = json.loads(record_path.read_text())
            result = record.get("result") or {}
            failure = result.get("metadata", {}).get("failure_kind", "")
            # Wrong RTL/protocol remains a valid failed benchmark measurement.
            has_error = record.get("execution_health") == "failed" if plan.get("protocol") == "agent-assisted-v1" else record.get("error")
            if (has_error or failure in {"wall_timeout", "signal", "log_limit", "interrupted"}
                    or failure.startswith("launch_error")):
                healthy = False
        summarize(plan, model, out)
        print(f"{model_id}/{problem}: evidence saved; healthy_so_far={healthy}", flush=True)
    return healthy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--problem")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--checkpoints", required=True, type=Path)
    parser.add_argument("--diagnostics", required=True, type=Path)
    parser.add_argument("--image", default="adpbench-go:ci")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not supervise(args.plan, args.model, args.out, args.checkpoints, args.diagnostics,
                     image=args.image, resume=args.resume, problem_id=args.problem):
        raise SystemExit("Scoring or generation infrastructure failed; inspect saved records and diagnostics")


if __name__ == "__main__":
    main()
