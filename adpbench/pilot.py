"""The pilot runner: one config in, a matrix of agent runs out.

The pilot is the experiment. Every (agent, problem, repetition) "cell" becomes
an agent run under the same rules and budget, and the report is generated
from the frozen run records rather than from anything in memory.

Output layout:

    runs/pilot_<stamp>_<name>/
        plan.json                               what was planned
        resume_<stamp>.json                     one per `--resume` session
        <agent>/<problem>/rep<N>/record.json    one directory per cell
        <agent>/<problem>/rep<N>-t2/...         a retry after an infrastructure failure
        report.json, REPORT.md                  the summary

An agent that hits an infrastructure failure (a usage limit, an expired
login) is paused for the rest of the session: its remaining cells are skipped
instead of each failing the same way. `--resume` runs the cells that never ran
and retries the infrastructure failures in a new `-t<N>` directory, which
supersedes the voided try in the report while the voided record is kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .agent import DEFAULT_IMAGE, Mount, run_agent
from .problem import Problem, find_problem, load_problem, repo_root
from .report import TRY_DIR, load_runs, markdown, summarize

ORDERS = ("agent", "round")


@dataclass
class PilotAgent:
    label: str
    cmd: str
    timeout_s: int = 1800
    # Host environment variables passed into the sandbox by name.
    env: tuple[str, ...] = ()
    # Extra host directories for the sandbox, such as the CLI's login.
    mounts: tuple[Mount, ...] = ()
    # Log patterns that mean the failure was not the model's.
    infra_patterns: tuple[str, ...] = ()

    def plan_entry(self) -> dict:
        entry: dict = {"label": self.label, "cmd": self.cmd, "timeout_s": self.timeout_s}
        if self.env:
            entry["env"] = list(self.env)
        if self.mounts:
            entry["mounts"] = [
                {"host": m.host, "container": m.container, "read_only": m.read_only}
                for m in self.mounts
            ]
        if self.infra_patterns:
            entry["infra_patterns"] = list(self.infra_patterns)
        return entry


@dataclass
class PilotConfig:
    name: str
    problems: list[str]
    agents: list[PilotAgent]
    repetitions: int = 1
    sandbox: str = "none"
    image: str = DEFAULT_IMAGE
    network: str = "bridge"
    # "agent": every cell of one agent, then the next agent.
    # "round": repetition 1 of every agent on every problem, then repetition 2,
    # so a session cut short still leaves complete, comparable rounds.
    order: str = "agent"

    @property
    def planned_runs(self) -> int:
        return len(self.problems) * len(self.agents) * self.repetitions


@dataclass
class PilotCell:
    """One planned run: an agent on a problem, for one repetition."""

    number: int  # 1-based, for progress messages
    agent: PilotAgent
    agent_dir: str
    problem: str
    attempt: int


def slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-").lower()
    return cleaned or "run"


def agent_slugs(agents: list[PilotAgent]) -> list[str]:
    """Directory names for agents, disambiguated when labels collapse.

    `a/b` and `a-b` would otherwise share a directory and silently overwrite
    each other's runs.
    """
    used: set[str] = set()
    slugs = []
    for agent in agents:
        candidate = slug(agent.label)
        if candidate in used:
            digest = hashlib.sha1(agent.label.encode()).hexdigest()[:6]
            candidate = f"{candidate}-{digest}"
        while candidate in used:
            candidate = f"{candidate}x"
        used.add(candidate)
        slugs.append(candidate)
    return slugs


def load_pilot(path: str | Path) -> PilotConfig:
    data = json.loads(Path(path).read_text())
    agents = [
        PilotAgent(
            label=entry["label"],
            cmd=entry["cmd"],
            timeout_s=int(entry.get("timeout_s", 1800)),
            env=tuple(entry.get("env", ())),
            mounts=tuple(
                Mount(m["host"], m["container"], bool(m.get("read_only", False)))
                for m in entry.get("mounts", ())
            ),
            infra_patterns=tuple(entry.get("infra_patterns", ())),
        )
        for entry in data["agents"]
    ]
    if not agents:
        raise ValueError("pilot config needs at least one agent")
    order = data.get("order", "agent")
    if order not in ORDERS:
        raise ValueError(f"order must be one of {ORDERS}, got {order!r}")
    sandbox = data.get("sandbox", {})
    if sandbox.get("mode", "none") != "docker" and any(a.env or a.mounts for a in agents):
        raise ValueError("agent env and mounts need the docker sandbox")
    return PilotConfig(
        name=data.get("name", "pilot"),
        problems=list(data["problems"]),
        agents=agents,
        repetitions=int(data.get("repetitions", 1)),
        sandbox=sandbox.get("mode", "none"),
        image=sandbox.get("image", DEFAULT_IMAGE),
        network=sandbox.get("network", "bridge"),
        order=order,
    )


def run_pilot(
    config: PilotConfig,
    runs_root: Path | None = None,
    echo=print,
    jobs: int = 1,
    publish: Path | None = None,
    resume: Path | None = None,
) -> Path:
    """Execute every planned run, then write report.json and REPORT.md.

    `jobs` > 1 runs independent cells in parallel; every cell still gets its
    own directories, container names, and records, so the report is built the
    same way either way. `publish` copies the plan and report files there.
    `resume` continues an earlier pilot directory of the same plan.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan = {
        "name": config.name,
        "problems": config.problems,
        "agents": [agent.plan_entry() for agent in config.agents],
        "repetitions": config.repetitions,
        "order": config.order,
        "sandbox": {"mode": config.sandbox, "image": config.image, "network": config.network},
        "planned_runs": config.planned_runs,
        "jobs": jobs,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    if resume is not None:
        pilot_dir = Path(resume)
        _check_same_plan(pilot_dir, plan)
        (pilot_dir / f"resume_{stamp}.json").write_text(json.dumps(plan, indent=2) + "\n")
        echo(f"pilot {config.name}: resuming {pilot_dir}, {jobs} job(s)")
    else:
        root = Path(runs_root) if runs_root else repo_root() / "runs"
        pilot_dir = root / f"pilot_{stamp}_{slug(config.name)}"
        pilot_dir.mkdir(parents=True, exist_ok=True)
        (pilot_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        echo(f"pilot {config.name}: {config.planned_runs} runs, {jobs} job(s) -> {pilot_dir}")

    # Messages from parallel cells must not interleave mid-line.
    echo_lock = threading.Lock()

    def say(message: str) -> None:
        with echo_lock:
            echo(message)

    cells = _plan_cells(config)
    paused: dict[str, str] = {}
    paused_lock = threading.Lock()

    def run(cell: PilotCell) -> None:
        with paused_lock:
            reason = paused.get(cell.agent.label)
        if reason:
            say(f"[{cell.number}/{config.planned_runs}] {cell.agent.label} {cell.problem}"
                f" rep{cell.attempt}: skipped, agent paused")
            return
        failure = _run_cell(cell, config, pilot_dir, say)
        if failure:
            with paused_lock:
                if cell.agent.label not in paused:
                    paused[cell.agent.label] = failure
                    say(f"    -> pausing {cell.agent.label}: {failure}")

    if jobs > 1 and len(cells) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(run, cell) for cell in cells]
            for future in futures:
                future.result()
    else:
        for cell in cells:
            run(cell)
    if paused:
        echo(f"paused agents: {', '.join(sorted(paused))}; continue with --resume {pilot_dir}")

    report = summarize(load_runs(pilot_dir))
    (pilot_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (pilot_dir / "REPORT.md").write_text(
        f"# ADPBench pilot report: {config.name}\n\n"
        + markdown(report)
        + "\nRuns are frozen under this directory; `plan.json` lists what was planned.\n"
    )
    echo(f"report -> {pilot_dir / 'REPORT.md'}")

    if publish is not None:
        publish = Path(publish)
        publish.mkdir(parents=True, exist_ok=True)
        for name in ("plan.json", "report.json", "REPORT.md"):
            shutil.copy2(pilot_dir / name, publish / name)
        echo(f"published -> {publish}")
    return pilot_dir


def _check_same_plan(pilot_dir: Path, plan: dict) -> None:
    """A resume must continue the same experiment, not a different one."""
    previous_path = pilot_dir / "plan.json"
    if not previous_path.is_file():
        raise ValueError(f"no plan.json in {pilot_dir}")
    previous = json.loads(previous_path.read_text())
    for key in ("name", "problems", "agents", "repetitions", "sandbox"):
        if previous.get(key) != plan.get(key):
            raise ValueError(f"--resume config differs from {previous_path} in {key!r}")


def _plan_cells(config: PilotConfig) -> list[PilotCell]:
    pairs = list(zip(config.agents, agent_slugs(config.agents)))
    reps = range(1, config.repetitions + 1)
    if config.order == "round":
        order = [(a, p, r) for r in reps for p in config.problems for a in pairs]
    else:
        order = [(a, p, r) for a in pairs for p in config.problems for r in reps]
    return [
        PilotCell(number, agent, agent_dir, problem, attempt)
        for number, ((agent, agent_dir), problem, attempt) in enumerate(order, start=1)
    ]


def cell_destination(base: Path, attempt: int) -> Path | None:
    """Where a cell should run next, or None when it already has a final record.

    `base` is `<pilot>/<agent>/<problem>`. A cell with no record runs in
    `rep<N>`; one whose latest try was an infrastructure failure runs again in
    `rep<N>-t<k+1>`.
    """
    latest_try, latest = 0, None
    for path in base.glob(f"rep{attempt}*/record.json"):
        match = TRY_DIR.fullmatch(path.parent.name)
        if not match or int(match[1]) != attempt:
            continue
        number = int(match[2] or 1)
        if number > latest_try:
            latest_try, latest = number, path
    if latest is None:
        return base / f"rep{attempt}"
    record = json.loads(latest.read_text())
    if record.get("infra_failure"):
        return base / f"rep{attempt}-t{latest_try + 1}"
    return None


def _run_cell(cell: PilotCell, config: PilotConfig, pilot_dir: Path, say) -> str:
    """Run one cell. Returns the infrastructure failure, if there was one."""
    problem = load_problem(find_problem(cell.problem))
    agent = cell.agent
    dest = cell_destination(pilot_dir / cell.agent_dir / problem.name, cell.attempt)
    if dest is None:
        return ""
    tag = "" if dest.name == f"rep{cell.attempt}" else f" ({dest.name})"
    say(f"[{cell.number}/{config.planned_runs}] {agent.label} {problem.name} rep{cell.attempt}{tag}")
    try:
        record = run_agent(
            problem,
            agent.cmd,
            dest=dest,
            timeout_s=agent.timeout_s,
            label=agent.label,
            attempt=cell.attempt,
            group=config.name,
            sandbox=config.sandbox,
            image=config.image,
            network=config.network,
            env_names=agent.env,
            mounts=agent.mounts,
            infra_patterns=agent.infra_patterns,
        )
    except Exception as exc:  # noqa: BLE001 - one bad run must not stop the pilot
        _write_launch_failure(dest, problem, cell, config, exc)
        say(f"    -> launch failure: {type(exc).__name__}: {exc}")
        return f"launch failure: {exc}"
    if record.infra_failure:
        say(f"    -> infrastructure failure: {record.infra_failure}")
        return record.infra_failure
    status = "correct" if record.result and record.result.get("correct") else "failed"
    say(f"    -> {status} score={record.score():.2f}x")
    return ""


def _write_launch_failure(
    dest: Path, problem: Problem, cell: PilotCell, config: PilotConfig, exc: BaseException
) -> None:
    """A record for a run that crashed before the harness could write one."""
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "problem": problem.name,
        "agent_cmd": cell.agent.cmd,
        "label": cell.agent.label,
        "attempt": cell.attempt,
        "group": config.name,
        "sandbox": config.sandbox,
        "timed_out": False,
        "error": f"{type(exc).__name__}: {exc}",
        "infra_failure": f"launch failure: {type(exc).__name__}",
        "traceback": traceback.format_exc(),
        "result": None,
    }
    (dest / "record.json").write_text(json.dumps(payload, indent=2) + "\n")
