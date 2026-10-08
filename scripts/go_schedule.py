"""Hourly check that dispatches the next wave of a round waiting out a Go usage limit.

    python -m scripts.go_schedule check [--dispatch]   # decide; dispatch the next wave and publication if due
    python -m scripts.go_schedule names --model <id> --problem <id> --run-id <id>
                                                     # "<claim tag> <pause tag> <release?>"

Run by .github/workflows/go-agent-schedule.yml. It never calls a model. It
dispatches the agent workflow (the same reviewed plan, claim tags and limits
as a manual dispatch) only when:

- the plan that go-agent.yml selects has `schedule: hourly`, is enabled, and
  is inside its authorization window;
- no agent run is queued or in progress;
- at least one planned slot is unclaimed.

OpenCode Go's 5-hour usage limit is shared by the whole account. A slot that
the limit cuts off releases its claim (see go-agent-slot.yml), so a later
hourly check picks it up once the limit resets. Committing a scheduled plan
is the authorization, including the confirmation that Go "Use balance" is off.

A wave dispatched with the workflow token never triggers publish-results.yml,
so the same check dispatches it for the oldest finished wave of the plan that
has no publication run yet. Every wave rebuilds the same leaderboard, so it
waits while a publication runs or a results PR from this plan awaits review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from scripts.go_agent import validate
from scripts.go_pilot import claim_name, due, now_utc, pause_name, plan_slots, read_plan, select_model

REPOSITORY = "arygupt/ADPBench"
AGENT_WORKFLOW = Path(".github/workflows/go-agent.yml")
PUBLISH_WORKFLOW = "publish-results.yml"
ROUND_TITLE = "OpenCode Go · agent-assisted-v2 · round evaluation"
PLAN_LINE = re.compile(r"^  PLAN: (pilot/[a-z0-9-]+\.json)\s*$", re.M)


def selected_plan(workflow_text: str) -> str:
    """The one literal plan path the agent workflow runs."""
    matches = PLAN_LINE.findall(workflow_text)
    if len(matches) != 1:
        raise ValueError("agent workflow must select exactly one literal plan")
    return matches[0]


def decide(plan: dict, claimed: set[str], active_runs: int, now: datetime) -> tuple[bool, str]:
    """(dispatch?, reason). Pure: every input is passed in."""
    if plan.get("schedule") != "hourly":
        return False, "the selected plan is not scheduled"
    if not plan.get("generation_enabled"):
        return False, "the selected plan is disabled"
    if not all(due(plan, model, now) for model in plan["models"]):
        return False, "outside the plan's authorization window"
    if active_runs:
        return False, f"{active_runs} agent run(s) still queued or in progress"
    waiting = [slot for slot in plan_slots(plan) if claim_name(plan, *slot) not in claimed]
    if not waiting:
        return False, "every slot is claimed; nothing to run"
    return True, f"{len(waiting)} unclaimed slot(s)"


def to_publish(plan: dict, waves: list[dict], publications: list[dict], open_branches: list[str]) -> tuple[int | None, str]:
    """(source run to publish, or None; reason). Pure: every input is passed in.

    `waves` and `publications` are `gh run list` rows for go-agent.yml and
    publish-results.yml; `open_branches` are the heads of open PRs.
    """
    since = min(datetime.fromisoformat(model["not_before"]) for model in plan["models"])
    finished = sorted(
        (
            run for run in waves
            if run["status"] == "completed" and run["event"] == "workflow_dispatch" and run["headBranch"] == "main"
            and run["displayTitle"] == ROUND_TITLE and datetime.fromisoformat(run["createdAt"]) >= since
        ),
        key=lambda run: run["createdAt"],
    )
    publications = [run for run in publications if run["event"] != "pull_request"]
    started = {run["displayTitle"] for run in publications}
    waiting = [run["databaseId"] for run in finished if f"Publish results · source run {run['databaseId']}" not in started]
    if not waiting:
        return None, "every finished wave has a publication run"
    if any(run["status"] != "completed" for run in publications):
        return None, "a publication is still running"
    if any(branch.startswith(f"automation/results-{run['databaseId']}-") for run in finished for branch in open_branches):
        return None, "a results PR from this plan awaits review"
    return waiting[0], f"{len(waiting)} finished wave(s) to publish"


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def claimed_tags(plan: dict) -> set[str]:
    refs = gh("api", "--paginate", f"repos/{REPOSITORY}/git/matching-refs/tags/{plan['name']}-", "--jq", ".[].ref")
    return {ref.removeprefix("refs/tags/") for ref in refs.split()}


def active_agent_runs() -> int:
    runs = json.loads(
        gh("run", "list", "--repo", REPOSITORY, "--workflow", AGENT_WORKFLOW.name, "--limit", "20", "--json", "status")
    )
    return sum(1 for run in runs if run["status"] != "completed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="decide whether the next wave is due")
    check.add_argument("--dispatch", action="store_true", help="dispatch go-agent.yml when a wave is due")
    names = commands.add_parser("names", help="one slot's claim and pause tags, for the slot workflow")
    names.add_argument("--model", required=True)
    names.add_argument("--problem", required=True)
    names.add_argument("--run-id", required=True)
    args = parser.parse_args()

    if args.command == "names":
        plan = read_plan(Path(os.environ["PLAN"]))
        model = select_model(plan, args.model)
        if (model["id"], args.problem) not in plan_slots(plan):
            raise SystemExit("unreviewed slot")
        release = "true" if plan.get("release_on_quota") else "false"
        print(claim_name(plan, model["id"], args.problem), pause_name(plan, args.run_id), release)
        return

    plan = read_plan(Path(selected_plan(AGENT_WORKFLOW.read_text())))
    validate(plan)
    run, reason = decide(plan, claimed_tags(plan), active_agent_runs(), now_utc())
    print(f"{plan['name']} try {plan.get('try', 1)}: {'dispatch' if run else 'wait'} - {reason}")
    if run and args.dispatch:
        gh(
            "workflow", "run", AGENT_WORKFLOW.name, "--repo", REPOSITORY, "--ref", "main",
            "-f", "run_models=true", "-f", "run_canary=false", "-f", "subscription_only=true",
        )
        print("Dispatched the agent workflow; each slot still claims its own tag before any request.")

    fields = "databaseId,status,event,headBranch,displayTitle,createdAt"
    waves = json.loads(gh("run", "list", "--repo", REPOSITORY, "--workflow", AGENT_WORKFLOW.name, "--limit", "50", "--json", fields))
    publications = json.loads(gh("run", "list", "--repo", REPOSITORY, "--workflow", PUBLISH_WORKFLOW, "--limit", "100", "--json", fields))
    branches = json.loads(gh("pr", "list", "--repo", REPOSITORY, "--state", "open", "--json", "headRefName"))
    source, reason = to_publish(plan, waves, publications, [pr["headRefName"] for pr in branches])
    print(f"{plan['name']} publication: {f'publish run {source}' if source else 'wait'} - {reason}")
    if source and args.dispatch:
        gh("workflow", "run", PUBLISH_WORKFLOW, "--repo", REPOSITORY, "--ref", "main", "-f", f"run_id={source}")
        print("Dispatched the publisher; it validates the wave and opens a review-only results PR.")


if __name__ == "__main__":
    main()
