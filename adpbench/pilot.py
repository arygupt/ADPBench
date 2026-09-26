"""The pilot runner: one config in, a matrix of agent runs out.

The pilot is the experiment. Every (agent, problem, repetition) "cell" becomes
an agent run under the same rules and budget, and the report is generated
from the frozen run records rather than from anything in memory.

Output layout:

    runs/pilot_<stamp>_<name>/
        plan.json                               what was planned
        <agent>/<problem>/rep<N>/record.json    one directory per cell
        report.json, REPORT.md                  the summary
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

from .agent import DEFAULT_IMAGE, run_agent
from .problem import Problem, find_problem, load_problem, repo_root
from .report import load_runs, markdown, summarize


@dataclass
class PilotAgent:
    label: str
    cmd: str
    timeout_s: int = 1800


@dataclass
class PilotConfig:
    name: str
    problems: list[str]
    agents: list[PilotAgent]
    repetitions: int = 1
    sandbox: str = "none"
    image: str = DEFAULT_IMAGE
    network: str = "bridge"

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
        )
        for entry in data["agents"]
    ]
    if not agents:
        raise ValueError("pilot config needs at least one agent")
    sandbox = data.get("sandbox", {})
    return PilotConfig(
        name=data.get("name", "pilot"),
        problems=list(data["problems"]),
        agents=agents,
        repetitions=int(data.get("repetitions", 1)),
        sandbox=sandbox.get("mode", "none"),
        image=sandbox.get("image", DEFAULT_IMAGE),
        network=sandbox.get("network", "bridge"),
    )


def run_pilot(
    config: PilotConfig,
    runs_root: Path | None = None,
    echo=print,
    jobs: int = 1,
    publish: Path | None = None,
) -> Path:
    """Execute every planned run, then write report.json and REPORT.md.

    `jobs` > 1 runs independent cells in parallel; every cell still gets its
    own directories, container names, and records, so the report is built the
    same way either way. `publish` copies the plan and report files there.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(runs_root) if runs_root else repo_root() / "runs"
    pilot_dir = root / f"pilot_{stamp}_{slug(config.name)}"
    pilot_dir.mkdir(parents=True, exist_ok=True)

    plan = {
        "name": config.name,
        "problems": config.problems,
        "agents": [
            {"label": agent.label, "cmd": agent.cmd, "timeout_s": agent.timeout_s}
            for agent in config.agents
        ],
        "repetitions": config.repetitions,
        "sandbox": {"mode": config.sandbox, "image": config.image, "network": config.network},
        "planned_runs": config.planned_runs,
        "jobs": jobs,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    (pilot_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    echo(f"pilot {config.name}: {config.planned_runs} runs, {jobs} job(s) -> {pilot_dir}")

    # Messages from parallel cells must not interleave mid-line.
    echo_lock = threading.Lock()

    def say(message: str) -> None:
        with echo_lock:
            echo(message)

    cells = _plan_cells(config)
    if jobs > 1 and len(cells) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_run_cell, cell, config, pilot_dir, say) for cell in cells]
            for future in futures:
                future.result()
    else:
        for cell in cells:
            _run_cell(cell, config, pilot_dir, say)

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


def _plan_cells(config: PilotConfig) -> list[PilotCell]:
    cells = []
    for agent, agent_dir in zip(config.agents, agent_slugs(config.agents)):
        for problem in config.problems:
            for attempt in range(1, config.repetitions + 1):
                cells.append(PilotCell(len(cells) + 1, agent, agent_dir, problem, attempt))
    return cells


def _run_cell(cell: PilotCell, config: PilotConfig, pilot_dir: Path, say) -> None:
    problem = load_problem(find_problem(cell.problem))
    agent = cell.agent
    say(f"[{cell.number}/{config.planned_runs}] {agent.label} {problem.name} rep{cell.attempt}")
    dest = pilot_dir / cell.agent_dir / problem.name / f"rep{cell.attempt}"
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
        )
        status = "correct" if record.result and record.result.get("correct") else "failed"
        say(f"    -> {status} score={record.score():.2f}x")
    except Exception as exc:  # noqa: BLE001 - one bad run must not stop the pilot
        _write_launch_failure(dest, problem, cell, config, exc)
        say(f"    -> launch failure: {type(exc).__name__}: {exc}")


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
        "traceback": traceback.format_exc(),
        "result": None,
    }
    (dest / "record.json").write_text(json.dumps(payload, indent=2) + "\n")
