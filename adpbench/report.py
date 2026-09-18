"""Aggregating agent runs into a scoreboard.

A published ranking has to answer three questions, not one:

  - how often did the evaluated system produce correct hardware?
  - how often did it correctly beat the baseline? (the primary number)
  - when it worked, how much better was it? (secondary)

Failures are split into wrong RTL and infrastructure failures, attempts are
counted rather than silently averaged away, and the correct-only geometric
mean is reported as a secondary statistic, never alone.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

INFRASTRUCTURE_STAGES = {"no result", "no_result"}


@dataclass
class RunSummary:
    path: str
    problem: str
    label: str
    attempt: int
    correct: bool
    ratio: float
    stage: str
    error: str
    timed_out: bool
    group: str = ""

    @property
    def kind(self) -> str:
        """ok, wrong_rtl, or infrastructure."""
        if self.correct:
            return "ok"
        if self.error or self.timed_out:
            return "infrastructure"
        if self.stage in INFRASTRUCTURE_STAGES:
            return "infrastructure"
        return "wrong_rtl"


def _summary_from_record(path: Path) -> RunSummary:
    record = json.loads(path.read_text())
    result = record.get("result") or {}
    metadata = result.get("metadata") or {}
    error = record.get("error") or result.get("error") or ""
    return RunSummary(
        path=str(path),
        problem=record.get("problem", path.parent.name),
        label=record.get("label") or record.get("agent_cmd", "unknown"),
        attempt=int(record.get("attempt", 1)),
        correct=bool(result.get("correct")),
        ratio=float(result.get("ratio", -1.0) or -1.0),
        stage=str(metadata.get("stage", "")),
        error=str(error),
        timed_out=bool(record.get("timed_out")),
        group=record.get("group", ""),
    )


def _is_canonical(path: Path, root: Path) -> bool:
    """Only host-written record locations count.

    Task directories are agent-writable, so a nested `record.json` could be
    fabricated. Records are accepted only at the exact depths this harness
    writes: `runs/<problem>/<stamp>/record.json`, or a pilot's
    `<pilot>/<label>/<problem>/rep<N>/record.json` (also relative to a pilot
    root). Anything deeper is ignored.
    """
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return False
    if len(rel) == 3:
        return True
    if len(rel) == 4 and rel[2].startswith("rep"):
        return True
    if len(rel) == 5 and rel[0].startswith("pilot_") and rel[3].startswith("rep"):
        return True
    return False


def load_runs(root: str | Path) -> list[RunSummary]:
    root = Path(root)
    return sorted(
        (
            _summary_from_record(path)
            for path in root.glob("**/record.json")
            if _is_canonical(path, root)
        ),
        key=lambda run: (run.label, run.problem, run.attempt),
    )


def _geomean(values: list[float]) -> float:
    positive = [value for value in values if value > 0]
    if not positive:
        return -1.0
    return math.exp(sum(math.log(value) for value in positive) / len(positive))


def _bucket(runs: list[RunSummary]) -> dict:
    attempts = len(runs)
    correct = [run for run in runs if run.correct]
    beating = [run for run in correct if run.ratio > 1.0]
    kinds = Counter(run.kind for run in runs)
    return {
        "attempts": attempts,
        "correct": len(correct),
        "correctness_rate": round(len(correct) / attempts, 4) if attempts else 0.0,
        "beating_baseline": len(beating),
        "beat_baseline_rate": round(len(beating) / attempts, 4) if attempts else 0.0,
        "geomean_ratio_successful": round(_geomean([run.ratio for run in correct]), 4)
        if correct
        else -1.0,
        "failures": {
            "wrong_rtl": kinds.get("wrong_rtl", 0),
            "infrastructure": kinds.get("infrastructure", 0),
        },
        "repetitions": max((run.attempt for run in runs), default=0),
    }


def summarize(runs: list[RunSummary]) -> dict:
    labels: dict[str, list[RunSummary]] = {}
    for run in runs:
        labels.setdefault(run.label, []).append(run)

    report: dict = {"labels": {}, "totals": _bucket(runs)}
    for label, group in sorted(labels.items()):
        bucket = _bucket(group)
        problems: dict[str, list[RunSummary]] = {}
        for run in group:
            problems.setdefault(run.problem, []).append(run)
        bucket["per_problem"] = {
            problem: _bucket(problem_runs)
            for problem, problem_runs in sorted(problems.items())
        }
        report["labels"][label] = bucket
    return report


def markdown(report: dict) -> str:
    lines = [
        "| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, bucket in report["labels"].items():
        lines.append(
            "| `{label}` | {attempts} | {correct} | {correctness:.0%} | {beating} | "
            "{beat_rate:.0%} | {geomean:.2f}x | {wrong} | {infra} |".format(
                label=label,
                attempts=bucket["attempts"],
                correct=bucket["correct"],
                correctness=bucket["correctness_rate"],
                beating=bucket["beating_baseline"],
                beat_rate=bucket["beat_baseline_rate"],
                geomean=bucket["geomean_ratio_successful"],
                wrong=bucket["failures"]["wrong_rtl"],
                infra=bucket["failures"]["infrastructure"],
            )
        )
    totals = report["totals"]
    lines.append(
        "| **all** | {attempts} | {correct} | {correctness:.0%} | {beating} | "
        "{beat_rate:.0%} | {geomean:.2f}x | {wrong} | {infra} |".format(
            attempts=totals["attempts"],
            correct=totals["correct"],
            correctness=totals["correctness_rate"],
            beating=totals["beating_baseline"],
            beat_rate=totals["beat_baseline_rate"],
            geomean=totals["geomean_ratio_successful"],
            wrong=totals["failures"]["wrong_rtl"],
            infra=totals["failures"]["infrastructure"],
        )
    )
    return "\n".join(lines) + "\n"


def write_report(runs_root: str | Path, out_dir: str | Path | None = None) -> dict:
    runs_root = Path(runs_root)
    out_dir = Path(out_dir) if out_dir else runs_root
    report = summarize(load_runs(runs_root))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out_dir / "REPORT.md").write_text("# ADPBench pilot report\n\n" + markdown(report))
    return report
