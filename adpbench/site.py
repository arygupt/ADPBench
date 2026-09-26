"""Static-site data export.

The site is plain HTML/JS that renders committed JSON under `site/data/`.
This module produces that JSON from a frozen pilot directory and the problem
catalog, so the published leaderboard is a copy of the frozen records - the
same ones `adpbench report` and `adpbench replay` consume.

    export_site    pilot directory -> leaderboard.json, problems.json, report.json
    export_sanity  evaluates every problem's reference solution -> sanity metrics
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .agent import git_commit
from .evaluate import evaluate_multi
from .problem import Problem, discover_problems, load_problem
from .report import load_runs, summarize
from .seeds import DEV_SEEDS, EVAL_SEEDS

AGENT_PROTOCOL = "agent-assisted-v1"

PROBLEM_TITLES = {
    "001_dot_product": "Dot product",
    "002_gemv": "Matrix-vector (GEMV)",
    "003_matmul": "Matrix multiply",
    "004_conv1d": "1-D convolution",
}

# Generation fields copied into each published run. Nothing here is model text.
GENERATION_FIELDS = (
    "model",
    "finish_reason",
    "usage",
    "generation_settings",
    "response_diagnostics",
    "error",
    "invalid_rtl",
    "evidence_unavailable",
)
AGENT_GENERATION_FIELDS = (
    "protocol",
    "outcome",
    "turns",
    "max_turns",
    "incomplete_usage",
    "reasoning_measured",
)


def export_site(
    pilot_dir: str | Path,
    out_dir: str | Path,
    sanity_file: str | Path | None = None,
) -> Path:
    """Write leaderboard.json, problems.json and report.json for the static site."""
    pilot_dir = Path(pilot_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = json.loads((pilot_dir / "plan.json").read_text())

    problems = [load_problem(path) for path in discover_problems()]
    sanity = {}
    if sanity_file and Path(sanity_file).is_file():
        sanity = json.loads(Path(sanity_file).read_text())
    problem_entries = []
    for problem in problems:
        entry = _problem_entry(problem)
        entry["sanity"] = sanity.get(problem.name, {})
        problem_entries.append(entry)

    records = [
        json.loads(path.read_text()) for path in sorted(pilot_dir.glob("*/**/rep*/record.json"))
    ]
    runs = [_run_entry(record) for record in records]

    report = summarize(load_runs(pilot_dir))
    agent_track = plan.get("protocol") == AGENT_PROTOCOL
    models = [
        _model_entry(label, label_runs, report["labels"].get(label, {}), agent_track)
        for label, label_runs in _group_by_label(runs).items()
    ]

    first_manifest = next((record["manifest"] for record in records if record.get("manifest")), {})
    first_agent = (plan.get("agents") or [{}])[0]
    leaderboard = {
        "meta": {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "git_commit": git_commit(),
            "pilot": plan.get("name", pilot_dir.name),
            "problems_planned": plan.get("problems", []),
            "sandbox": plan.get("sandbox", {}),
            "budget_s": first_agent.get("timeout_s"),
            "repetitions": plan.get("repetitions"),
            "jobs": plan.get("jobs"),
            "cases": {
                "seeds": list(EVAL_SEEDS),
                "dev_seeds": list(DEV_SEEDS),
                "transactions": problems[0].transactions if problems else 0,
            },
            "tools": first_manifest.get("tools", {}),
            "adpbench_version": first_manifest.get("adpbench_version", ""),
        },
        "problems": problem_entries,
        "models": models,
    }

    _write_json(out_dir / "leaderboard.json", leaderboard)
    _write_json(out_dir / "problems.json", {"problems": problem_entries})
    _write_json(out_dir / "report.json", report)
    return out_dir


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _problem_entry(problem: Problem) -> dict:
    baseline = {}
    if problem.baseline_metrics.is_file():
        baseline = json.loads(problem.baseline_metrics.read_text())
    level = problem.root.parent.name.removeprefix("level") or 1
    return {
        "name": problem.name,
        "title": problem.interface.get("title") or PROBLEM_TITLES.get(problem.name, problem.name),
        "level": int(level),
        "params": problem.params,
        "input_lens": problem.input_lens,
        "out_len": problem.out_len,
        "transactions": problem.transactions,
        "directed": problem.directed_cases,
        "quant": problem.quant,
        "baseline": {
            "cells": baseline.get("cells", -1),
            "cycles": baseline.get("cycles", -1),
            "adp": baseline.get("adp", -1.0),
        },
    }


def _run_entry(record: dict) -> dict:
    """One published run, built from a record.json."""
    result = record.get("result") or {}
    metadata = result.get("metadata") or {}
    manifest = record.get("manifest") or {}
    generation = manifest.get("generation") or {}

    entry = {
        "label": record.get("label", ""),
        "problem": record.get("problem", ""),
        "attempt": record.get("attempt", 1),
        "correct": bool(result.get("correct")),
        "ratio": float(result.get("ratio", -1.0) or -1.0),
        "cells": result.get("cells", -1),
        "cycles": result.get("cycles", -1),
        "adp": result.get("adp", -1.0),
        "stage": metadata.get("stage", ""),
        "correctness": metadata.get("correctness", ""),
        "duration_s": record.get("duration_s", 0.0),
        "timed_out": bool(record.get("timed_out")),
        "error": record.get("error", "") or result.get("error", ""),
        "audit_ok": bool((record.get("audit") or {}).get("ok")),
        "submission_sha256": manifest.get("submission_sha256", ""),
        "netlist_sha256": manifest.get("netlist_sha256", ""),
        "history": len(record.get("history", [])),
        "execution": record.get("execution"),
        "record_origin": record.get("record_origin", "scorer"),
        "generation": None,
    }
    if generation:
        entry["generation"] = {name: generation.get(name) for name in GENERATION_FIELDS}

    if generation.get("protocol") == AGENT_PROTOCOL:
        entry["outcome"] = record.get("outcome", "scoring_interrupted")
        entry["execution_health"] = record.get("execution_health", "failed")
        # Only a completed held-out score says correct or not; anything else is unknown.
        if entry["outcome"] in ("correct", "incorrect"):
            entry["correct"] = bool(result.get("correct"))
        else:
            entry["correct"] = None
        for name in AGENT_GENERATION_FIELDS:
            entry["generation"][name] = generation.get(name)
        entry["generation"]["dev_checks"] = generation.get("checks", generation.get("dev_checks"))
    return entry


def _group_by_label(runs: list[dict]) -> dict[str, list[dict]]:
    """Runs grouped by label, in order of first appearance."""
    groups: dict[str, list[dict]] = {}
    for run in runs:
        groups.setdefault(run.get("label", "unknown"), []).append(run)
    return groups


def _model_entry(label: str, runs: list[dict], bucket: dict, agent_track: bool) -> dict:
    """One leaderboard row: the report bucket for a label, plus its runs."""
    attempts = bucket.get("attempts", len(runs))
    correct = bucket.get("correct", 0)
    beating = bucket.get("beating_baseline", 0)
    failures = bucket.get("failures", {})
    model = {
        "label": label,
        "attempts": attempts,
        "correct": correct,
        "beating": beating,
        "correctness_rate": bucket.get("correctness_rate", 0.0),
        "beat_rate": bucket.get("beat_baseline_rate", 0.0),
        "geomean": bucket.get("geomean_ratio_successful", -1.0),
        "wrong_rtl": failures.get("wrong_rtl", 0),
        "infra": failures.get("infrastructure", 0),
        "score_ci": _interval_text(*_wilson_interval(beating, attempts)),
        "correctness_ci": _interval_text(*_wilson_interval(correct, attempts)),
        "runs": runs,
    }

    if agent_track:
        outcomes: dict[str, int] = {}
        for run in runs:
            outcome = run.get("outcome", "scoring_interrupted")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        model["outcomes"] = outcomes
        model["scored"] = sum(1 for run in runs if isinstance(run["correct"], bool))
        model["unscored"] = sum(1 for run in runs if run["correct"] is None)
        model["wrong_rtl"] = outcomes.get("incorrect", 0)
        model["infra"] = sum(1 for run in runs if run.get("execution_health") == "failed")
    return model


def _wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial rate."""
    if trials <= 0:
        return 0.0, 0.0
    p_hat = successes / trials
    denominator = 1 + z * z / trials
    center = (p_hat + z * z / (2 * trials)) / denominator
    half_width = z * (p_hat * (1 - p_hat) / trials + z * z / (4 * trials * trials)) ** 0.5
    half_width /= denominator
    return max(0.0, center - half_width), min(1.0, center + half_width)


def _interval_text(low: float, high: float) -> str:
    half = (high - low) / 2
    return f"±{half * 100:.0f}%" if half > 0 else ""


def export_sanity(out: str | Path) -> Path:
    """Evaluate every problem's `solutions/parallel.v` and write the metrics file.

    Slow (full gate per problem); the results are committed so exports never
    need to re-run synthesis.
    """
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results = {}
    for path in discover_problems():
        problem = load_problem(path)
        solution = path / "solutions" / "parallel.v"
        if not solution.is_file():
            continue
        evaluated = evaluate_multi(problem, [solution], source="sanity", tag="sanity")
        results[problem.name] = {
            "correct": evaluated.correct,
            "cells": evaluated.cells,
            "cycles": evaluated.cycles,
            "adp": evaluated.adp,
            "ratio": evaluated.ratio,
        }
    _write_json(out_path, results)
    return out_path
