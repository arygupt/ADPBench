"""Publish cli-agent-v1 results: vendor coding CLIs on subscription logins.

    python -m scripts.publish_cli_agent PILOT_DIR [PILOT_DIR ...]

cli-agent-v1 ran as several local pilots (one per subscription, so one
usage-limit pause never blocks the other vendor). This merges their frozen
records into one board, keeps only models with a score for every planned
cell, and writes site/data/cli-agent-v1/ plus a catalog entry.

Only scores, hashes and timings are copied (adpbench.site's run fields).
Agent logs, trajectories and the prompt never leave the pilot directories.
cli-agent-v1 is its own leaderboard: model plus vendor harness. It is never
pooled with the agent-assisted boards.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path

from adpbench.report import superseded
from adpbench.site import export_site

PROTOCOL = "cli-agent-v1"
ROOT = Path(__file__).resolve().parents[1]
SITE_DATA = ROOT / "site" / "data"
CATALOG = SITE_DATA / "evaluations.json"
CATALOG_LABEL = "Vendor CLIs · cli-agent-v1 · Claude Code and Codex on subscription logins"
# Harness versions baked into sandbox/Dockerfile.cli when the runs were made.
HARNESS_VERSIONS = {"claude-code": "2.1.321", "codex": "0.167.0"}
EFFORT = re.compile(r"(?:--effort |model_reasoning_effort=)(\w+)")
PRIVATE = re.compile(r'"(?:agent_cmd|trajectory|messages|transcript|prompt)"\s*:', re.I)


def agent_dir(label: str) -> str:
    """'codex/gpt-6.1-sol' -> 'codex-gpt-6.1-sol', the pilot's per-agent directory."""
    return label.replace("/", "-")


def scored_cells(directory: Path) -> set[tuple[str, str]]:
    """(problem, repetition) pairs with a final, non-infrastructure record."""
    cells = set()
    for path in directory.glob("*/rep*/record.json"):
        if superseded(path):
            continue
        record = json.loads(path.read_text())
        if record.get("infra_failure"):
            continue
        cells.add((path.parts[-3], path.parent.name.split("-t")[0]))
    return cells


def collect(pilots: list[Path]) -> tuple[dict, dict[str, Path], dict[str, int]]:
    """The shared plan, each finished agent's directory, and how far each unfinished one got."""
    plan, finished, unfinished = None, {}, {}
    for pilot in pilots:
        pilot_plan = json.loads((pilot / "plan.json").read_text())
        if plan is None:
            plan = {key: value for key, value in pilot_plan.items() if key != "agents"} | {"agents": []}
        elif (pilot_plan["problems"], pilot_plan["repetitions"]) != (plan["problems"], plan["repetitions"]):
            raise ValueError(f"{pilot} planned a different matrix")
        planned = len(pilot_plan["problems"]) * pilot_plan["repetitions"]
        for agent in pilot_plan["agents"]:
            directory = pilot / agent_dir(agent["label"])
            done = len(scored_cells(directory)) if directory.is_dir() else 0
            if done == planned:
                finished[agent["label"]] = directory
                plan["agents"].append(agent)
            else:
                unfinished[agent["label"]] = done
    if plan is None:
        raise ValueError("no pilot directories given")
    return plan, finished, unfinished


def harness(agent: dict) -> dict:
    cli = agent["label"].split("/", 1)[0]
    effort = EFFORT.search(str(agent.get("cmd", "")))
    return {
        "cli": cli,
        "cli_version": HARNESS_VERSIONS.get(cli, ""),
        "reasoning_effort": effort.group(1) if effort else "",
        "timeout_s": agent.get("timeout_s"),
    }


def publish(pilots: list[Path], out_dir: Path = SITE_DATA / PROTOCOL, catalog: Path = CATALOG) -> dict:
    plan, finished, unfinished = collect(pilots)
    if not finished:
        raise ValueError("no model has finished every cell yet")
    with tempfile.TemporaryDirectory() as tmp:
        merged = Path(tmp) / PROTOCOL
        merged.mkdir()
        for label, directory in finished.items():
            shutil.copytree(directory, merged / agent_dir(label))
        (merged / "plan.json").write_text(json.dumps(plan | {"name": PROTOCOL, "protocol": PROTOCOL}))
        export_site(merged, out_dir)

    leaderboard_path = out_dir / "leaderboard.json"
    leaderboard = json.loads(leaderboard_path.read_text())
    leaderboard["meta"] |= {
        "protocol": PROTOCOL,
        "harnesses": {agent["label"]: harness(agent) for agent in plan["agents"]},
        # Models still running are named, so a missing model is not read as a no-show.
        "in_progress": unfinished,
        "cells_per_model": len(plan["problems"]) * plan["repetitions"],
    }
    leaderboard_path.write_text(json.dumps(leaderboard, indent=2) + "\n")
    for path in out_dir.glob("*.json"):
        if PRIVATE.search(path.read_text()):
            raise ValueError(f"{path} contains a private field")
    register(catalog)
    return leaderboard


def register(catalog: Path) -> None:
    """Add the board to evaluations.json once; the default board is left alone."""
    data = json.loads(catalog.read_text())
    entry = {
        "id": PROTOCOL,
        "label": CATALOG_LABEL,
        "path": f"data/{PROTOCOL}/leaderboard.json",
        "protocol": PROTOCOL,
    }
    others = [item for item in data["evaluations"] if item["id"] != PROTOCOL]
    data["evaluations"] = others + [entry]
    catalog.write_text(json.dumps(data, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pilots", nargs="+", type=Path, help="local cli-agent-v1 pilot directories")
    args = parser.parse_args(argv)
    leaderboard = publish(args.pilots)
    for model in leaderboard["models"]:
        print(f"{model['label']}: {model['beating']}/{model['attempts']} beat, geomean {model['geomean']:.2f}x")
    for label, done in leaderboard["meta"]["in_progress"].items():
        print(f"{label}: still running ({done}/{leaderboard['meta']['cells_per_model']}), not published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
