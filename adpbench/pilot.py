"""The pilot runner: one config in, a matrix of agent runs out.

The pilot is the experiment. Every (agent, problem, repetition) becomes an
agent run under the same rules and budget, and the report is generated from
the frozen run records rather than from anything in memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .agent import DEFAULT_IMAGE, run_agent
from .problem import Problem, discover_problems, load_problem, repo_root
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


def _resolve_problem(spec: str) -> Problem:
    candidate = Path(spec)
    if (candidate / "dut.py").is_file():
        return load_problem(candidate)
    matches = [path for path in discover_problems() if path.name == spec or str(path).endswith(spec)]
    if len(matches) != 1:
        raise SystemExit(f"pilot: problem {spec!r} matched {len(matches)} entries")
    return load_problem(matches[0])


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
    return PilotConfig(
        name=data.get("name", "pilot"),
        problems=list(data["problems"]),
        agents=agents,
        repetitions=int(data.get("repetitions", 1)),
        sandbox=data.get("sandbox", {}).get("mode", "none"),
        image=data.get("sandbox", {}).get("image", DEFAULT_IMAGE),
        network=data.get("sandbox", {}).get("network", "bridge"),
    )


def _write_launch_failure(
    dest: Path, problem: Problem, agent: PilotAgent, attempt: int, config: PilotConfig, exc: BaseException
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "problem": problem.name,
        "agent_cmd": agent.cmd,
        "label": agent.label,
        "attempt": attempt,
        "group": config.name,
        "sandbox": config.sandbox,
        "timed_out": False,
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(),
        "result": None,
    }
    (dest / "record.json").write_text(json.dumps(payload, indent=2) + "\n")


def run_pilot(
    config: PilotConfig,
    runs_root: Path | None = None,
    echo=print,
    jobs: int = 1,
    publish: Path | None = None,
) -> Path:
    """Execute every planned run, then write report.json and REPORT.md.

    `jobs` > 1 runs independent (agent, problem, repetition) cells in
    parallel; every cell still gets its own directories, container names, and
    records, so the report is built the same way either way.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(runs_root) if runs_root else repo_root() / "runs"
    pilot_dir = root / f"pilot_{stamp}_{slug(config.name)}"
    pilot_dir.mkdir(parents=True, exist_ok=True)

    plan = {
        "name": config.name,
        "problems": config.problems,
        "agents": [
            {"label": a.label, "cmd": a.cmd, "timeout_s": a.timeout_s} for a in config.agents
        ],
        "repetitions": config.repetitions,
        "sandbox": {"mode": config.sandbox, "image": config.image, "network": config.network},
        "planned_runs": config.planned_runs,
        "jobs": jobs,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    (pilot_dir / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    echo(f"pilot {config.name}: {config.planned_runs} runs, {jobs} job(s) -> {pilot_dir}")

    cells = [
        (agent, agent_slug, problem_spec, attempt)
        for agent, agent_slug in zip(config.agents, agent_slugs(config.agents))
        for problem_spec in config.problems
        for attempt in range(1, config.repetitions + 1)
    ]
    lock = threading.Lock()
    counter = [0]

    def say(message: str) -> None:
        with lock:
            echo(message)

    def run_cell(cell) -> None:
        agent, agent_slug, problem_spec, attempt = cell
        problem = _resolve_problem(problem_spec)
        with lock:
            counter[0] += 1
            index = counter[0]
        say(f"[{index}/{config.planned_runs}] {agent.label} {problem.name} rep{attempt}")
        dest = pilot_dir / agent_slug / problem.name / f"rep{attempt}"
        try:
            record = run_agent(
                problem,
                agent.cmd,
                dest=dest,
                timeout_s=agent.timeout_s,
                label=agent.label,
                attempt=attempt,
                group=config.name,
                sandbox=config.sandbox,
                image=config.image,
                network=config.network,
            )
            status = "correct" if record.result and record.result.get("correct") else "failed"
            say(f"    -> {status} score={record.score():.2f}x")
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the pilot
            _write_launch_failure(dest, problem, agent, attempt, config, exc)
            say(f"    -> launch failure: {type(exc).__name__}: {exc}")

    if jobs > 1 and len(cells) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(run_cell, cell) for cell in cells]
            for future in as_completed(futures):
                future.result()
    else:
        for cell in cells:
            run_cell(cell)

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
        shutil.copy2(pilot_dir / "plan.json", publish / "plan.json")
        shutil.copy2(pilot_dir / "report.json", publish / "report.json")
        shutil.copy2(pilot_dir / "REPORT.md", publish / "REPORT.md")
        echo(f"published -> {publish}")
    return pilot_dir
