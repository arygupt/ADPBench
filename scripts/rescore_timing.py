"""Rescore published designs with the timing-aware ADP. Offline, no model calls.

    python -m scripts.rescore_timing [--results pilot/results] [--out pilot/timing]

Every correct, scored, non-superseded record with a frozen submission is
re-synthesized with adpbench.timing, alongside each problem's baseline.
Correctness and cycle counts come from the published records; only area and
logic depth are new. Writes timing.json (one row per design) and REPORT.md
(how each dataset's ranking moves).
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adpbench import timing
from adpbench.durable import atomic_json
from adpbench.problem import discover_problems, load_problem, repo_root
from adpbench.report import superseded
from scripts.go_pilot import frozen_path


def scored_designs(results: Path, problems: dict) -> list[dict]:
    """Correct, scored records whose frozen RTL is published."""
    designs = []
    for path in sorted(results.glob("*/*/*/rep*/record.json")):
        record = json.loads(path.read_text())
        result = record.get("result") or {}
        source = frozen_path(path.parent)
        if superseded(path) or not result.get("correct") or result.get("ratio", -1) <= 0:
            continue
        if record["problem"] not in problems or not source.is_file():
            continue
        designs.append({
            "dataset": path.relative_to(results).parts[0],
            "label": record["label"],
            "problem": record["problem"],
            "slot": path.parent.name,
            "source": source,
            "cells": result["cells"],
            "cycles": result["cycles"],
            "ratio": result["ratio"],
        })
    return designs


def measure_all(jobs: list[tuple], workers: int) -> list[dict]:
    """Run timing.measure on (problem, rtl) pairs, each in its own directory."""
    with tempfile.TemporaryDirectory() as tmp:
        def run(index_job):
            index, (problem, rtl) = index_job
            measured = timing.measure(problem, [rtl], Path(tmp) / str(index))
            measured.pop("log")
            return measured

        with ThreadPoolExecutor(workers) as pool:
            return list(pool.map(run, enumerate(jobs)))


def rescore(results: Path, workers: int) -> dict:
    problems = {path.name: load_problem(path) for path in discover_problems()}
    designs = scored_designs(results, problems)
    baselines = {name: json.loads(problem.baseline_metrics.read_text()) for name, problem in problems.items()}

    jobs = [(problem, problem.baseline_rtl) for problem in problems.values()]
    jobs += [(problems[design["problem"]], design["source"]) for design in designs]
    measured = measure_all(jobs, workers)

    baseline_rows = {}
    for problem, m in zip(problems.values(), measured):
        if not m["ok"]:
            raise RuntimeError(f"{problem.name} baseline: {m['error']}")
        cycles = baselines[problem.name]["cycles"]
        baseline_rows[problem.name] = {
            "cells": baselines[problem.name]["cells"],
            "cycles": cycles,
            "area": round(m["area"], 3),
            "depth": m["depth"],
            "timed_adp": timing.timed_adp(m["area"], cycles, m["depth"]),
        }

    rows = []
    for design, m in zip(designs, measured[len(problems):]):
        row = {key: value for key, value in design.items() if key != "source"}
        row["source"] = str(design["source"].relative_to(results))
        if not m["ok"]:
            rows.append({**row, "error": m["error"]})
            continue
        base = baseline_rows[design["problem"]]
        tadp = timing.timed_adp(m["area"], design["cycles"], m["depth"])
        rows.append({
            **row,
            "area": round(m["area"], 3),
            "depth": m["depth"],
            # The headline ratio with the clock period added and cells kept as area.
            "depth_ratio": round(design["ratio"] * timing.period(base["depth"]) / timing.period(m["depth"]), 4),
            "timed_adp": tadp,
            "timed_ratio": round(base["timed_adp"] / tadp, 4),
        })
    return {"register_levels": timing.REGISTER_LEVELS, "baselines": baseline_rows, "designs": rows}


def _geomean(values: list[float]) -> float:
    return math.exp(sum(math.log(value) for value in values) / len(values)) if values else 0.0


def markdown(data: dict) -> str:
    lines = [
        "# Timing-aware rescore",
        "",
        "`timed_adp = area × cycles × (depth + "
        f"{data['register_levels']})`. Area is Nangate45 X1 area (µm²); depth is the longest",
        "register-to-register path in gate levels. Correct, scored designs only; ratios are",
        "against the same problem's baseline measured the same way. Experimental, not the headline score.",
        "",
        "Geomeans: `cells × cycles` is today's score, `+ depth` multiplies in the clock period",
        "only, and `timed` also swaps cell count for area.",
        "",
        "## Baselines",
        "",
        "| problem | cells | cycles | area µm² | depth |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, base in data["baselines"].items():
        lines.append(f"| {name} | {base['cells']} | {base['cycles']} | {base['area']:.0f} | {base['depth']} |")

    scored = [row for row in data["designs"] if "timed_ratio" in row]
    for dataset in sorted({row["dataset"] for row in scored}):
        rows = [row for row in scored if row["dataset"] == dataset]
        models = {}
        for row in rows:
            models.setdefault(row["label"], []).append(row)
        ranked = sorted(models, key=lambda label: -_geomean([r["ratio"] for r in models[label]]))
        timed_rank = sorted(models, key=lambda label: -_geomean([r["timed_ratio"] for r in models[label]]))
        lines += [
            "",
            f"## {dataset}",
            "",
            "| model | correct | beat baseline | timed beat | cells × cycles | + depth | timed | rank | timed rank |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for label in ranked:
            runs = models[label]
            lines.append(
                f"| {label} | {len(runs)} | {sum(r['ratio'] > 1 for r in runs)} | "
                f"{sum(r['timed_ratio'] > 1 for r in runs)} | {_geomean([r['ratio'] for r in runs]):.2f}× | "
                f"{_geomean([r['depth_ratio'] for r in runs]):.2f}× | "
                f"{_geomean([r['timed_ratio'] for r in runs]):.2f}× | {ranked.index(label) + 1} | "
                f"{timed_rank.index(label) + 1} |"
            )

    movers = sorted(scored, key=lambda row: row["timed_ratio"] / row["ratio"])
    lines += [
        "",
        "## Largest drops",
        "",
        "Designs whose ratio falls most once area and clock period count.",
        "",
        "| dataset | model | problem | cycles | depth | ratio | + depth | timed ratio |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in movers[:10]:
        lines.append(
            f"| {row['dataset']} | {row['label']} | {row['problem']} | {row['cycles']} | {row['depth']} | "
            f"{row['ratio']:.2f}× | {row['depth_ratio']:.2f}× | {row['timed_ratio']:.2f}× |"
        )

    failed = [row for row in data["designs"] if "error" in row]
    if failed:
        lines += ["", "## Not measured", ""]
        lines += [f"- {row['dataset']} · {row['label']} · {row['problem']}: {row['error']}" for row in failed]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, default=repo_root() / "pilot/results")
    parser.add_argument("--out", type=Path, default=repo_root() / "pilot/timing")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()

    data = rescore(args.results.resolve(), args.jobs)
    args.out.mkdir(parents=True, exist_ok=True)
    atomic_json(args.out / "timing.json", data)
    (args.out / "REPORT.md").write_text(markdown(data))
    print(f"{len(data['designs'])} designs -> {args.out}")


if __name__ == "__main__":
    main()
