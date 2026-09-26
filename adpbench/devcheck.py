"""Dev-seed check for the agent-assisted track, run inside a no-network container.

Audits the file, and if the audit passes, scores it on the development seeds
only. Writes the feedback to --out as JSON and prints a one-line summary.
"""

import argparse
import json
from pathlib import Path

from .audit import audit_submission
from .durable import atomic_json
from .evaluate import evaluate_multi
from .problem import load_problem
from .seeds import DEV_SEEDS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--problem", required=True, type=Path)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--deadline-s", type=int, default=600)
    args = parser.parse_args()

    audit = audit_submission(args.file.read_text())
    feedback = {"audit": audit, "scope": "development cases only"}
    if audit["ok"]:
        result = evaluate_multi(
            load_problem(args.problem),
            [args.file],
            seeds=DEV_SEEDS,
            source="agent-development",
            tag="dev",
            deadline_s=args.deadline_s,
        )
        feedback["result"] = result.to_dict()
    atomic_json(args.out, feedback)

    correct = feedback.get("result", {}).get("correct")
    print(json.dumps({"audit_ok": audit["ok"], "correct": correct}))


if __name__ == "__main__":
    main()
