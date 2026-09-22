"""Static-site data export.

The site is plain HTML/JS that renders committed JSON under `site/data/`.
This module produces that JSON from a frozen pilot directory and the problem
catalog, so the published leaderboard is a copy of the frozen records - the
same ones `adpbench report` and `adpbench replay` consume.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .agent import git_commit
from .evaluate import DEV_SEEDS, EVAL_SEEDS, evaluate_multi
from .problem import Problem, discover_problems, load_problem, repo_root
from .report import load_runs, summarize

PROBLEM_TITLES = {
    "001_dot_product": "Dot product",
    "002_gemv": "Matrix-vector (GEMV)",
    "003_matmul": "Matrix multiply",
    "004_conv1d": "1-D convolution",
}


def _problem_entry(problem: Problem) -> dict:
    baseline = problem.baseline_metrics
    baseline_data = json.loads(baseline.read_text()) if baseline.is_file() else {}
    return {
        "name": problem.name,
        "title": problem.interface.get("title") or PROBLEM_TITLES.get(problem.name, problem.name),
        "level": int(problem.root.parent.name.removeprefix("level") or 1),
        "params": problem.params,
        "input_lens": problem.input_lens,
        "out_len": problem.out_len,
        "transactions": problem.transactions,
        "directed": problem.directed_cases,
        "quant": problem.quant,
        "baseline": {
            "cells": baseline_data.get("cells", -1),
            "cycles": baseline_data.get("cycles", -1),
            "adp": baseline_data.get("adp", -1.0),
        },
    }


def _run_entry(record: dict) -> dict:
    result = record.get("result") or {}
    metadata = result.get("metadata") or {}
    manifest = record.get("manifest") or {}
    generation = manifest.get("generation") or {}
    return {
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
        "generation": {
            k: generation.get(k) for k in (
                "model", "finish_reason", "usage", "generation_settings",
                "response_diagnostics", "error", "invalid_rtl",
            )
        } if generation else None,
    }


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial rate."""
    if n <= 0:
        return 0.0, 0.0
    p_hat = k / n
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    half = z * (p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return max(0.0, center - half), min(1.0, center + half)


def _ci_text(low: float, high: float) -> str:
    half = (high - low) / 2
    return f"±{half * 100:.0f}%" if half > 0 else ""


def export_site(
    pilot_dir: str | Path,
    out_dir: str | Path,
    sanity_file: str | Path | None = None,
) -> Path:
    """Write data/leaderboard.json and data/problems.json for the static site."""
    pilot_dir = Path(pilot_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = json.loads((pilot_dir / "plan.json").read_text())

    problems = [load_problem(path) for path in discover_problems()]
    problem_entries = [_problem_entry(problem) for problem in problems]

    sanity: dict = {}
    if sanity_file and Path(sanity_file).is_file():
        sanity = json.loads(Path(sanity_file).read_text())
    for entry in problem_entries:
        entry["sanity"] = sanity.get(entry["name"], {})

    records = []
    for path in sorted(pilot_dir.glob("*/**/rep*/record.json")):
        records.append(json.loads(path.read_text()))
    run_entries = [_run_entry(record) for record in records]

    report = summarize(load_runs(pilot_dir))
    label_buckets = report["labels"]

    models = []
    for record_group in _group_by_label(run_entries):
        label = record_group[0]["label"]
        bucket = label_buckets.get(label, {})
        attempts = bucket.get("attempts", len(record_group))
        correct = bucket.get("correct", 0)
        beating = bucket.get("beating_baseline", 0)
        beat_rate = bucket.get("beat_baseline_rate", 0.0)
        correctness_rate = bucket.get("correctness_rate", 0.0)
        beat_low, beat_high = _wilson(beating, attempts)
        correct_low, correct_high = _wilson(correct, attempts)
        models.append(
            {
                "label": label,
                "attempts": attempts,
                "correct": correct,
                "beating": beating,
                "correctness_rate": correctness_rate,
                "beat_rate": beat_rate,
                "geomean": bucket.get("geomean_ratio_successful", -1.0),
                "wrong_rtl": bucket.get("failures", {}).get("wrong_rtl", 0),
                "infra": bucket.get("failures", {}).get("infrastructure", 0),
                "score_ci": _ci_text(beat_low, beat_high),
                "correctness_ci": _ci_text(correct_low, correct_high),
                "runs": record_group,
            }
        )

    first_manifest = {}
    for record in records:
        if record.get("manifest"):
            first_manifest = record["manifest"]
            break

    leaderboard = {
        "meta": {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "git_commit": git_commit(),
            "pilot": plan.get("name", pilot_dir.name),
            "problems_planned": plan.get("problems", []),
            "sandbox": plan.get("sandbox", {}),
            "budget_s": (plan.get("agents") or [{}])[0].get("timeout_s"),
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

    (out_dir / "leaderboard.json").write_text(json.dumps(leaderboard, indent=2) + "\n")
    (out_dir / "problems.json").write_text(
        json.dumps({"problems": problem_entries}, indent=2) + "\n"
    )
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return out_dir


def _group_by_label(runs: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for run in runs:
        groups.setdefault(run.get("label", "unknown"), []).append(run)
    return list(groups.values())


def export_sanity(out: str | Path, jobs: int = 1) -> Path:
    """Evaluate every sanity solution and write the metrics file.

    Slow (full gate per problem); results are cached so exports never need to
    re-run synthesis.
    """
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results: dict = {}
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
    out_path.write_text(json.dumps(results, indent=2) + "\n")
    return out_path


def site_root() -> Path:
    return repo_root() / "site"
