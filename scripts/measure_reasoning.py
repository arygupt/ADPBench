"""Measure reasoning from saved agent transcripts; counts only, never the text.

For batches published before generation receipts recorded `reasoning_measured`.
Reads the private `go-agent-raw-*` Actions artifacts and writes the same counts
the agent runner now records, as a site data file keyed by `model/problem`.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from scripts.go_pilot import read_plan, write_json
from scripts.go_tools import reasoning_counts


def measure(artifacts: Path, plan: dict, run_id: int) -> dict:
    apis = {model["id"]: model["api"] for model in plan["models"]}
    runs = {}
    for model_id in apis:
        for problem in plan["problems"]:
            root = artifacts / f"go-agent-raw-{model_id}-{problem}-{run_id}" / "model-private"
            turns = sorted(root.glob("turn-*/response.json"),
                           key=lambda p: int(re.sub(r"\D", "", p.parent.name)))
            if not turns:
                continue
            total = {"turns": 0, "turns_with_reasoning": 0, "reasoning_chars": 0,
                     "answer_chars": 0, "reasoning_tokens": None}
            for path in turns:
                counts = reasoning_counts(json.loads(path.read_text()), apis[model_id])
                total["turns"] += 1
                total["turns_with_reasoning"] += counts["reasoning_chars"] > 0
                total["reasoning_chars"] += counts["reasoning_chars"]
                total["answer_chars"] += counts["answer_chars"]
                if counts["reasoning_tokens"] is not None:
                    total["reasoning_tokens"] = (total["reasoning_tokens"] or 0) + counts["reasoning_tokens"]
            runs[f"{model_id}/{problem}"] = total
    return {"schema_version": 1, "dataset": plan["name"], "source_run_id": run_id,
            "method": "character counts of reasoning vs answer and tool-call arguments in saved responses",
            "runs": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = measure(args.artifacts, read_plan(args.plan), args.run_id)
    write_json(args.out, result)
    print(f"{len(result['runs'])} slots measured -> {args.out}")


if __name__ == "__main__":
    main()
