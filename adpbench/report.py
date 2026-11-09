"""Aggregating agent runs into a scoreboard.

A published ranking has to answer three questions, not one:

  - how often did the evaluated system produce correct hardware?
  - how often did it correctly beat the baseline? (the primary number)
  - when it worked, how much better was it? (secondary)

Failures are split into wrong RTL and infrastructure failures, attempts are
counted rather than silently averaged away, and the correct-only geometric
mean is reported as a secondary statistic, never alone.

Runs of pay-as-you-go models also carry a list-price cost (adpbench/pricing.py),
reported as a total and as dollars per baseline-beating design. The total
counts every attempt, since a failed attempt was paid for too.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from adpbench.pricing import PRICES_CHECKED_AT, run_cost

INFRASTRUCTURE_STAGES = {"no result", "no_result"}
# `rep1` is a round's first try; `rep1-t2` is its rerun after an infrastructure failure.
TRY_DIR = re.compile(r"rep(\d+)(?:-t(\d+))?")


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
    # Only set for the agent-assisted track, which records a typed outcome.
    outcome: str = ""
    execution_health: str = ""
    # List-price cost in USD; None when the model has no per-token price or
    # the run has no token receipt.
    cost_usd: float | None = None
    # Some usage went unreported (a dropped stream), so the cost is a lower bound.
    cost_incomplete: bool = False

    @property
    def kind(self) -> str:
        """How the run ended: ok, wrong_rtl, infrastructure, or submission_failure.

        Infrastructure means nothing was scored: a harness error or a run that
        timed out before producing any submission. A timeout that still left a
        scorable (wrong) design counts as wrong RTL.
        """
        if self.correct:
            return "ok"

        if self.outcome:
            # Agent-assisted track: trust the typed outcome.
            if self.execution_health == "failed":
                return "infrastructure"
            if self.outcome == "incorrect":
                return "wrong_rtl"
            return "submission_failure"

        if self.error:
            return "infrastructure"
        if self.stage in INFRASTRUCTURE_STAGES:
            return "infrastructure"
        if self.timed_out and not self.stage:
            return "infrastructure"
        return "wrong_rtl"


# --------------------------------------------------------------------------
# Loading records
# --------------------------------------------------------------------------


def load_runs(root: str | Path) -> list[RunSummary]:
    """Every harness-written record.json under `root`, sorted by label/problem/attempt."""
    root = Path(root)
    runs = [
        _summary_from_record(path)
        for path in root.glob("**/record.json")
        if _is_canonical(path, root) and not superseded(path)
    ]
    runs.sort(key=lambda run: (run.label, run.problem, run.attempt))
    return runs


def _is_canonical(path: Path, root: Path) -> bool:
    """Only records at the exact locations the harness writes are counted.

    Task directories are agent-writable, so a nested `record.json` could be
    fabricated. Records are accepted only at these depths:

        <problem>/<stamp>/record.json                     (a single `adpbench agent` run)
        <label>/<problem>/rep<N>/record.json              (inside a pilot directory)
        pilot_<...>/<label>/<problem>/rep<N>/record.json  (a runs/ directory of pilots)

    Anything deeper is ignored.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    if len(parts) == 3:
        return True
    if len(parts) == 4 and parts[2].startswith("rep"):
        return True
    if len(parts) == 5 and parts[0].startswith("pilot_") and parts[3].startswith("rep"):
        return True
    return False


def superseded(path: Path) -> bool:
    """Whether a later try of the same repetition replaces this record in scoring.

    A rerun is published next to the voided try (`rep1` and `rep1-t2`), which
    stays in the records but no longer counts.
    """
    match = TRY_DIR.fullmatch(path.parent.name)
    if not match:
        return False
    rep, current = match[1], int(match[2] or 1)
    for sibling in path.parent.parent.glob(f"rep{rep}-t*/record.json"):
        later = TRY_DIR.fullmatch(sibling.parent.name)
        if later and later[1] == rep and int(later[2] or 1) > current:
            return True
    return False


def _summary_from_record(path: Path) -> RunSummary:
    record = json.loads(path.read_text())
    result = record.get("result") or {}
    metadata = result.get("metadata") or {}
    error = record.get("error") or result.get("error") or ""
    label = record.get("label") or record.get("agent_cmd", "unknown")
    generation = (record.get("manifest") or {}).get("generation") or {}
    return RunSummary(
        path=str(path),
        problem=record.get("problem", path.parent.name),
        label=label,
        attempt=int(record.get("attempt", 1)),
        correct=bool(result.get("correct")),
        ratio=float(result.get("ratio", -1.0) or -1.0),
        stage=str(metadata.get("stage", "")),
        error=str(error),
        timed_out=bool(record.get("timed_out")),
        group=record.get("group", ""),
        outcome=record.get("outcome", ""),
        execution_health=record.get("execution_health", ""),
        cost_usd=run_cost(label, generation),
        cost_incomplete=bool(generation.get("incomplete_usage")),
    )


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def summarize(runs: list[RunSummary]) -> dict:
    """{"labels": {label: bucket with "per_problem"}, "totals": bucket}."""
    by_label: dict[str, list[RunSummary]] = {}
    for run in runs:
        by_label.setdefault(run.label, []).append(run)

    report: dict = {"labels": {}, "totals": _bucket(runs)}
    for label, label_runs in sorted(by_label.items()):
        by_problem: dict[str, list[RunSummary]] = {}
        for run in label_runs:
            by_problem.setdefault(run.problem, []).append(run)

        bucket = _bucket(label_runs)
        bucket["per_problem"] = {
            problem: _bucket(problem_runs) for problem, problem_runs in sorted(by_problem.items())
        }
        report["labels"][label] = bucket
    return report


def _bucket(runs: list[RunSummary]) -> dict:
    """Counts and rates for a group of runs. Rates use every attempt as the denominator."""
    attempts = len(runs)
    correct = [run for run in runs if run.correct]
    beating = [run for run in correct if run.ratio > 1.0]
    kinds = Counter(run.kind for run in runs)

    bucket = {
        "attempts": attempts,
        "correct": len(correct),
        "correctness_rate": _rate(len(correct), attempts),
        "beating_baseline": len(beating),
        "beat_baseline_rate": _rate(len(beating), attempts),
        "geomean_ratio_successful": (
            round(_geomean([run.ratio for run in correct]), 4) if correct else -1.0
        ),
        "failures": {
            "wrong_rtl": kinds.get("wrong_rtl", 0),
            "infrastructure": kinds.get("infrastructure", 0),
        },
        "repetitions": max((run.attempt for run in runs), default=0),
    }
    if any(run.outcome for run in runs):
        bucket["outcomes"] = dict(Counter(run.outcome for run in runs))
        bucket["failures"]["submission_failure"] = kinds.get("submission_failure", 0)

    costs = [run.cost_usd for run in runs if run.cost_usd is not None]
    if costs:
        total = sum(costs)
        bucket["cost_usd"] = round(total, 4)
        bucket["cost_incomplete"] = len(costs) < attempts or any(run.cost_incomplete for run in runs)
        # Every attempt's cost, failed ones included, over the designs that beat the baseline.
        bucket["usd_per_baseline_beating"] = round(total / len(beating), 4) if beating else None
    return bucket


def _rate(count: int, attempts: int) -> float:
    return round(count / attempts, 4) if attempts else 0.0


def _geomean(values: list[float]) -> float:
    positive = [value for value in values if value > 0]
    if not positive:
        return -1.0
    return math.exp(sum(math.log(value) for value in positive) / len(positive))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def markdown(report: dict) -> str:
    totals = report["totals"]
    # Cost columns appear only when some run has a per-token price.
    priced = "cost_usd" in totals
    lines = [
        "| evaluated system | attempts | correct | correctness | beat baseline | beat rate "
        "| geomean (successful) | wrong RTL | infra |" + (" cost | $ per baseline-beating design |" if priced else ""),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|" + ("---:|---:|" if priced else ""),
    ]
    for label, bucket in report["labels"].items():
        lines.append(_table_row(f"`{label}`", bucket, priced))
    lines.append(_table_row("**all**", totals, priced))
    if priced:
        lines += [
            "",
            f"Cost is provider-reported tokens at list prices (checked {PRICES_CHECKED_AT}; "
            "see adpbench/pricing.py), over every attempt including failed ones. "
            "`≥` marks a total with unreported usage or unpriced runs.",
        ]

    if totals.get("outcomes"):
        lines += ["", "Agent-assisted outcomes (separate from execution health):", ""]
        for outcome, count in sorted(totals["outcomes"].items()):
            lines.append(f"- {outcome}: {count}")
        lines += [
            "",
            "Rates use all scheduled slots. Unscored submissions and interrupted jobs are not "
            "claims of incorrect arithmetic. Development checks are not held-out benchmark passes.",
        ]
    return "\n".join(lines) + "\n"


def _table_row(name: str, bucket: dict, priced: bool = False) -> str:
    cells = [
        name,
        str(bucket["attempts"]),
        str(bucket["correct"]),
        f"{bucket['correctness_rate']:.0%}",
        str(bucket["beating_baseline"]),
        f"{bucket['beat_baseline_rate']:.0%}",
        f"{bucket['geomean_ratio_successful']:.2f}x",
        str(bucket["failures"]["wrong_rtl"]),
        str(bucket["failures"]["infrastructure"]),
    ]
    if priced:
        cells += [_dollars(bucket, "cost_usd"), _dollars(bucket, "usd_per_baseline_beating")]
    return "| " + " | ".join(cells) + " |"


def _dollars(bucket: dict, key: str) -> str:
    value = bucket.get(key)
    if value is None:
        return "—"
    return f"{'≥' if bucket.get('cost_incomplete') else ''}${value:,.2f}"


def write_report(runs_root: str | Path, out_dir: str | Path | None = None) -> dict:
    """Summarize the runs under `runs_root` into report.json and REPORT.md."""
    runs_root = Path(runs_root)
    out_dir = Path(out_dir) if out_dir else runs_root
    report = summarize(load_runs(runs_root))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out_dir / "REPORT.md").write_text("# ADPBench pilot report\n\n" + markdown(report))
    return report
