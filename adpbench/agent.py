"""Running an agent CLI against a problem, then auditing and scoring its work.

The harness keeps three things separate:

  environment   what the agent is allowed to see and run  (environment.py)
  audit         what the agent is not allowed to do       (audit.py)
  scoring       what the agent actually gets credit for   (evaluate.py)

Scoring never trusts the task directory: it is agent-writable. The submission
is copied out on its own ("frozen") and scored against pristine problem files
on seeds the agent never saw.

A finished run directory contains:

    record.json     machine-readable run record (RunRecord)
    manifest.json   hashes, cases, tool versions, git commit
    trajectory.md   a readable index of the run
    agent.log       the ANSI-stripped terminal session
    terminal.log    the raw terminal recording (when `script` is available)
    .history/       every tested dut.v plus the check.sh feedback log

and the frozen submission sits next to it in `<run>_frozen/dut.v`.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from shutil import which
from uuid import uuid4

from . import sim, synth
from .audit import audit_submission
from .environment import (
    META_FILE,
    SANDBOX_MOUNT,
    WORK_MOUNT,
    build_environment,
    bundle_path,
    remove_path,
)
from .evaluate import evaluate_multi
from .hashing import sha256_file, sha256_text
from .problem import Problem, repo_root
from .seeds import EVAL_SEEDS

DEFAULT_IMAGE = "adpbench-agent:latest"
TERMINAL_LOG = "terminal.log"
CHECK_LOG_LIMIT = 12000

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07?")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*=\s*\S+"
)


@dataclass
class RunRecord:
    problem: str
    agent_cmd: str
    label: str = ""
    attempt: int = 1
    group: str = ""
    sandbox: str = "none"
    started: str = ""
    duration_s: float = 0.0
    exit_code: int = -1
    timed_out: bool = False
    audit: dict = field(default_factory=dict)
    result: dict | None = None
    error: str = ""
    frozen: str = ""
    manifest: dict = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    def score(self) -> float:
        """Baseline-relative score. Zero unless the submission passed the gate."""
        if not self.result or not self.result.get("correct"):
            return 0.0
        ratio = float(self.result.get("ratio", -1.0))
        return ratio if ratio > 0 else 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """Remove terminal control sequences so a recording reads as plain text."""
    text = ANSI_ESCAPE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def redact_secrets(text: str) -> str:
    """Replace the value of any `SOMETHING_KEY=value`-style assignment with ***."""
    return SECRET_ASSIGNMENT.sub(r"\1=***", text)


def adpbench_version() -> str:
    try:
        from importlib.metadata import version

        return version("adpbench")
    except Exception:  # noqa: BLE001 - version metadata is best-effort
        return "unknown"


def git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _write_host_file(path: Path, text: str) -> None:
    """Write a harness-owned file, replacing (never following) any symlink."""
    path = Path(path)
    path.unlink(missing_ok=True)
    path.write_text(text)


def freeze_submission(task_dir: Path, frozen_dir: Path) -> tuple[Path | None, str]:
    """Copy dut.v out of the agent-writable task directory.

    Returns (frozen_path, "") on success or (None, reason) on failure. A
    symlinked dut.v is refused: it could point anywhere on the host.
    """
    dut = Path(task_dir) / "dut.v"
    if dut.is_symlink():
        return None, "submission dut.v is a symlink"
    if not dut.is_file():
        return None, "no dut.v produced"
    remove_path(frozen_dir)
    Path(frozen_dir).mkdir(parents=True, exist_ok=True)
    frozen = Path(frozen_dir) / "dut.v"
    frozen.write_bytes(dut.read_bytes())
    return frozen, ""


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def script_argv(agent_cmd: str, log_path: str, sandbox: str = "none") -> list[str]:
    """Wrap the agent command in `script`, which records the full terminal session.

    Most agent CLIs draw an interactive UI, so plain stdout capture loses the
    session. `script` allocates a pseudo-terminal and records everything,
    including the command's own stdout and stderr, to `log_path`.
    """
    if sandbox == "docker":
        # util-linux `script`, as installed in the sandbox image.
        return ["script", "-qefc", agent_cmd, log_path]
    # BSD `script`, as on a macOS host.
    return ["script", "-q", log_path, "sh", "-c", agent_cmd]


def docker_command(
    env_dir: Path,
    bundle: Path,
    image: str,
    network: str,
    argv: list[str],
    cpus: float = 4,
    memory: str = "4g",
    pids_limit: int = 1024,
    name: str = "",
) -> list[str]:
    """The `docker run` command that runs an agent inside the sandbox.

    Only the task directory is writable. The harness bundle is mounted
    read-only and contains no evaluation seeds. `argv` is the in-container
    command, usually the terminal recorder wrapping the agent CLI.
    """
    command = [
        "docker", "run", "--rm",
        "-e", f"PYTHONPATH={SANDBOX_MOUNT}",
        "-v", f"{Path(bundle).resolve()}:{SANDBOX_MOUNT}:ro",
        "-v", f"{Path(env_dir).resolve()}:{WORK_MOUNT}",
        "-w", WORK_MOUNT,
        "--network", network,
        "--memory", memory,
        "--cpus", str(cpus),
        "--pids-limit", str(pids_limit),
    ]
    if name:
        command += ["--name", name]
    return command + [image] + list(argv)


# --------------------------------------------------------------------------
# Manifest and trajectory
# --------------------------------------------------------------------------


def build_manifest(
    problem: Problem,
    record: RunRecord,
    submission: Path | None,
    timeout_s: int,
    sandbox: str,
    image: str,
    network: str,
) -> dict:
    """Everything needed to replay and audit a run, in one dict."""
    result = record.result or {}
    metadata = result.get("metadata", {})

    per_case = metadata.get("per_case", [])
    netlist_sha = per_case[0].get("netlist_sha256", "") if per_case else ""

    problem_hashes = {}
    for name in ("dut.py", "baseline.v", "baseline.json"):
        if (problem.root / name).is_file():
            problem_hashes[name] = sha256_file(problem.root / name)

    submission_sha = ""
    if submission is not None and Path(submission).is_file():
        submission_sha = sha256_file(submission)

    return {
        "adpbench_version": adpbench_version(),
        "git_commit": git_commit(),
        "problem": problem.name,
        "problem_sha256": problem_hashes,
        "cases": {
            "seeds": list(EVAL_SEEDS),
            "directed": problem.directed_cases,
            "transactions": problem.transactions,
            "input_lens": problem.input_lens,
        },
        "tools": {"yosys": synth.tool_version(), "iverilog": sim.tool_version()},
        "submission_sha256": submission_sha,
        "netlist_sha256": netlist_sha,
        "agent_cmd": redact_secrets(record.agent_cmd),
        "budget_s": timeout_s,
        "sandbox": {
            "mode": sandbox,
            "image": image if sandbox == "docker" else "",
            "network": network if sandbox == "docker" else "",
        },
        "result": {
            "correct": bool(result.get("correct")),
            "ratio": result.get("ratio", -1.0),
            "cells": result.get("cells", -1),
            "cycles": result.get("cycles", -1),
            "stage": metadata.get("stage", "") if record.result else "error",
        },
        "error": record.error,
        "timed_out": record.timed_out,
        "duration_s": record.duration_s,
    }


def render_trajectory(record: RunRecord, run_dir: Path) -> str:
    """A readable markdown index of everything a run recorded."""
    run_dir = Path(run_dir)
    result = record.result or {}
    audit_status = "passed" if (record.audit or {}).get("ok") else "rejected"
    timed_out = " (timed out)" if record.timed_out else ""

    lines = [
        f"# Run trajectory — {record.problem}",
        "",
        f"- label: `{record.label}`",
        f"- attempt: {record.attempt}",
        f"- started: {record.started}",
        f"- duration: {record.duration_s}s",
        f"- exit code: {record.exit_code}{timed_out}",
        f"- audit: {audit_status}",
    ]
    if result.get("correct"):
        lines.append(
            f"- score: {record.score():.2f}x baseline "
            f"({result.get('cells')} cells × {result.get('cycles')} cycles)"
        )
    elif "error" in result:
        lines.append(f"- result: ERROR — {result.get('error')}")
    elif result:
        metadata = result.get("metadata") or {}
        stage = metadata.get("stage", "failed")
        lines.append(f"- result: {stage} — {metadata.get('correctness', '')}")
    else:
        lines.append("- result: not scored")
    if record.error:
        lines.append(f"- harness error: {record.error}")

    lines += [
        "",
        "## Artifacts",
        "",
        "- `record.json` — machine-readable run record",
        "- `manifest.json` — hashes, cases, tools, git commit",
        "- `agent.log` — ANSI-stripped terminal session",
        "- `terminal.log` — raw terminal recording",
        "- `.history/` — every tested `dut.v` snapshot plus the `check.sh` feedback log",
        f"- `{record.frozen}` — frozen submission that was scored",
    ]

    snapshots = sorted((run_dir / ".history").glob("*_dut.v"))
    if snapshots:
        lines += ["", "## Submissions tested"]
        for path in snapshots:
            lines.append(f"- `{path.name}` ({path.stat().st_size} bytes)")

    check_log = run_dir / ".history" / "check.log"
    if check_log.is_file():
        content = strip_ansi(check_log.read_text(errors="replace"))
        if len(content) > CHECK_LOG_LIMIT:
            content = content[-CHECK_LOG_LIMIT:] + "\n... (truncated)"
        lines += ["", "## check.sh feedback log", "", "```", content.rstrip(), "```"]

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Running an agent
# --------------------------------------------------------------------------


def run_agent(
    problem: Problem,
    agent_cmd: str,
    dest: Path | None = None,
    timeout_s: int = 1800,
    python: str | None = None,
    label: str = "",
    attempt: int = 1,
    group: str = "",
    sandbox: str = "none",
    image: str = DEFAULT_IMAGE,
    network: str = "bridge",
) -> RunRecord:
    """Run an agent CLI in a fresh task directory, then audit and score its dut.v.

    With `sandbox="docker"` the agent runs in a container that mounts the task
    directory read-write and a sanitized harness bundle read-only. Final
    scoring always happens on the host, outside the container.
    """
    if sandbox not in ("none", "docker"):
        raise ValueError(f"unknown sandbox {sandbox!r}")

    if dest:
        dest = Path(dest).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = repo_root() / "runs" / problem.name / stamp
    if dest.is_symlink():
        raise ValueError(f"run directory is a symlink: {dest}")
    # Start from a clean directory: leftover agent-written symlinks or files
    # must never be reused or followed.
    remove_path(dest)
    build_environment(problem, dest, python=python, force=True, sandbox=sandbox)

    record = RunRecord(
        problem=problem.name,
        agent_cmd=redact_secrets(agent_cmd),
        label=label or agent_cmd,
        attempt=attempt,
        group=group,
        sandbox=sandbox,
        started=datetime.now().isoformat(timespec="seconds"),
    )

    log = _run_agent_process(record, agent_cmd, dest, timeout_s, sandbox, image, network)
    _write_host_file(dest / "agent.log", log)
    record.history = [path.name for path in sorted((dest / ".history").glob("*_dut.v"))]

    # Nothing in the agent-writable directory is trusted: the submission is
    # copied, symlink-free, into a host-only directory before it is scored.
    frozen = _freeze_and_audit(record, dest)

    if record.audit["ok"]:
        # Score the frozen copy against pristine problem files, on seeds the
        # agent never saw.
        try:
            result = evaluate_multi(problem, [frozen], seeds=EVAL_SEEDS, source="agent", tag="eval")
            record.result = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - the record must survive scorer bugs
            record.error = f"{type(exc).__name__}: {exc}"
            record.result = {"error": record.error}

    try:
        record.manifest = build_manifest(problem, record, frozen, timeout_s, sandbox, image, network)
        _write_host_file(dest / "manifest.json", json.dumps(record.manifest, indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001 - never lose the run record
        record.manifest = {"error": f"{type(exc).__name__}: {exc}"}

    _write_host_file(dest / "trajectory.md", render_trajectory(record, dest))
    _write_host_file(dest / "record.json", record.to_json() + "\n")
    return record


def _run_agent_process(
    record: RunRecord,
    agent_cmd: str,
    dest: Path,
    timeout_s: int,
    sandbox: str,
    image: str,
    network: str,
) -> str:
    """Run the agent to completion or timeout. Fills in exit/timing fields on
    `record` and returns the text for agent.log."""
    container = ""
    cwd = dest
    shell = False
    if sandbox == "docker":
        container = f"adpbench_{uuid4().hex[:10]}"
        recorder = script_argv(agent_cmd, f"{WORK_MOUNT}/{TERMINAL_LOG}", sandbox="docker")
        command = docker_command(dest, bundle_path(dest), image, network, recorder, name=container)
        cwd = None
        shown = " ".join(command)
    elif which("script"):
        command = script_argv(agent_cmd, str(dest / TERMINAL_LOG))
        shown = " ".join(command)
    else:
        command = agent_cmd
        shell = True
        shown = agent_cmd
    shown = redact_secrets(shown)

    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started = time.time()
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        record.exit_code = process.returncode
        record.timed_out = False
        log = f"$ {shown}\n\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    except subprocess.TimeoutExpired:
        # The agent runs under the recorder (or a docker client), so killing
        # that one process can leave the agent running. Kill the whole process
        # group, and the container if there is one.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        if container:
            subprocess.run(["docker", "kill", container], capture_output=True, timeout=30)
        stdout, stderr = process.communicate()
        record.exit_code = -1
        record.timed_out = True
        log = (
            f"$ {shown}\n\n--- TIMEOUT after {timeout_s}s"
            f" (process group killed) ---\n{stdout}\n{stderr}\n"
        )
    record.duration_s = round(time.time() - started, 2)

    # The terminal recording, when there is one, is the better log.
    terminal = dest / TERMINAL_LOG
    if terminal.is_file():
        raw = terminal.read_text(errors="replace")
        log = f"$ {shown}\n\n--- terminal session ---\n{strip_ansi(raw)}\n"
    return log


def _freeze_and_audit(record: RunRecord, dest: Path) -> Path | None:
    """Freeze dut.v next to the run directory and fill in `record.audit`."""
    frozen, freeze_error = freeze_submission(dest, dest.parent / f"{dest.name}_frozen")
    record.frozen = str(frozen) if frozen else ""
    text = frozen.read_text() if frozen else ""
    record.audit = audit_submission(text)

    meta_path = dest / META_FILE
    skeleton_hash = None
    if meta_path.is_file():
        skeleton_hash = json.loads(meta_path.read_text()).get("skeleton_sha256")

    if frozen is None:
        _reject(record, freeze_error)
    elif skeleton_hash and sha256_text(text) == skeleton_hash:
        _reject(record, "submission is unchanged from the starting skeleton")
    return frozen


def _reject(record: RunRecord, reason: str) -> None:
    record.audit["violations"].append({"line": 0, "match": "", "reason": reason})
    record.audit["ok"] = False
