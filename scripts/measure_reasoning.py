"""Measure reasoning from saved agent transcripts - counts only, never the text.

    python -m scripts.measure_reasoning --plan <plan> --run-id <id> --artifacts <dir> --out <file>

For batches published before generation receipts recorded `reasoning_measured`.
Reads the private `go-agent-raw-*` Actions artifacts and writes the same
counts the agent runner now records, as a site data file keyed by
`model/problem`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from adpbench.durable import atomic_json
from scripts.go_pilot import read_plan
from scripts.go_tools import add_reasoning_counts, empty_reasoning_totals, reasoning_counts


def measure(artifacts: Path, plan: dict, run_id: int) -> dict:
    apis = {model["id"]: model["api"] for model in plan["models"]}
    runs = {}
    for model_id, api in apis.items():
        for problem in plan["problems"]:
            private = artifacts / f"go-agent-raw-{model_id}-{problem}-{run_id}" / "model-private"
            responses = sorted(private.glob("turn-*/response.json"), key=_turn_number)
            if not responses:
                continue
            totals = empty_reasoning_totals()
            for path in responses:
                add_reasoning_counts(totals, reasoning_counts(json.loads(path.read_text()), api))
            runs[f"{model_id}/{problem}"] = totals
    return {
        "schema_version": 1,
        "dataset": plan["name"],
        "source_run_id": run_id,
        "method": "character counts of reasoning vs answer and tool-call arguments in saved responses",
        "runs": runs,
    }


def _turn_number(response_path: Path) -> int:
    """`.../turn-07/response.json` -> 7."""
    return int(re.sub(r"\D", "", response_path.parent.name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = measure(args.artifacts, read_plan(args.plan), args.run_id)
    atomic_json(args.out, result)
    print(f"{len(result['runs'])} slots measured -> {args.out}")


if __name__ == "__main__":
    main()
