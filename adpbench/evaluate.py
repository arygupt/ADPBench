"""The evaluation pipeline: the `eval_kernel_against_ref` equivalent.

    dut.v
        -> synthesize (yosys)      fixed parameters, gate netlist, cell count
        -> build vectors (numpy)   seeded random inputs + golden outputs
        -> simulate (iverilog)     the netlist, exact match? how many cycles?
        -> score                   baseline_adp / adp

Stages short-circuit: a design that does not synthesize is never simulated.
Synthesis and simulation judge one artifact: the parameters applied at
synthesis are the parameters the testbench drives, and the simulated netlist
is the netlist whose cells were counted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from . import score as scoring
from . import sim, synth, vectors
from .problem import Problem, repo_root
from .result import EvalResult

LOG_TAIL = 4000

# The agent is allowed to iterate against DEV_SEEDS. Final scoring uses
# EVAL_SEEDS, which the agent never sees. This is what makes hardcoding the
# expected outputs useless: a correct general design scores identically on
# both, a memorised one collapses on the held-out set.
DEV_SEEDS = (0, 1)
EVAL_SEEDS = (1000, 1001, 1002)


def work_dir(problem: Problem, seed: int, tag: str = "", job: str = "") -> Path:
    """A per-invocation directory.

    `job` keeps concurrent evaluations of the same problem and seed from
    overwriting each other's scripts, netlists, and vectors.
    """
    suffix = f"seed{seed}" + (f"_{tag}" if tag else "") + (f"_{job}" if job else "")
    return repo_root() / ".adpbench" / problem.name / suffix


def load_baseline(problem: Problem) -> dict | None:
    path = problem.baseline_metrics
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluate(
    problem: Problem,
    rtl_paths: list[Path],
    seed: int = 0,
    source: str = "",
    workdir: Path | None = None,
) -> EvalResult:
    rtl_paths = [Path(p).resolve() for p in rtl_paths]
    workdir = (
        Path(workdir).resolve()
        if workdir
        else work_dir(problem, seed, job=uuid4().hex[:8])
    )
    workdir.mkdir(parents=True, exist_ok=True)

    result = EvalResult(problem=problem.name, source=source or ", ".join(p.name for p in rtl_paths))
    result.metadata["seed"] = seed
    result.metadata["flow"] = "flows/synth.ys"
    result.metadata["params"] = problem.params
    result.metadata["workdir"] = str(workdir)
    result.metadata["tools"] = {
        "yosys": synth.tool_version(),
        "iverilog": sim.tool_version(),
    }

    syn = synth.synthesize(problem, rtl_paths, workdir)
    result.synthesizable = syn["ok"]
    result.cells = syn["cells"]
    result.metadata["cells"] = syn["cells"]
    if not syn["ok"]:
        result.metadata["synthesis_log"] = syn["log"][-LOG_TAIL:]
        return result

    netlist = syn["netlist"]
    result.metadata["netlist_sha256"] = _sha256_file(netlist)

    inputs, expected = vectors.build_vectors(problem, seed)
    vectors.write_vectors(problem, inputs, workdir)

    # Timing run: no testbench-imposed idle cycles, so `cycles` measures the
    # DUT, not the harness.
    run = sim.simulate(problem, netlist, workdir, seed, mode="score")
    result.compiled = run["compiled"]
    result.metadata["sim_log"] = run["log"][-LOG_TAIL:]
    if not run["compiled"]:
        result.metadata["correctness"] = "failed to compile"
        return result
    if not run["finished"]:
        result.metadata["correctness"] = {
            "TIMEOUT": "timeout - DUT never produced a full result",
            "PROTOCOL": "valid/ready violation - output changed or dropped while stalled",
            "NO RESULT": "simulator produced no RESULT line",
        }.get(run["status"], f"simulation failed ({run['status']})")
        return result

    width = int(problem.params["ACC_W"])
    try:
        actual = vectors.read_outputs(run["out_file"], problem.out_len, width)
    except vectors.MalformedOutput as exc:
        result.metadata["correctness"] = f"malformed simulator output: {exc}"
        return result
    check = vectors.compare(actual, expected, width)
    if not check["match"]:
        result.metadata["correctness"] = check["detail"]
        return result

    # Conformance run: seeded input gaps, seeded output backpressure, and a
    # hold-stable check. Results must still match; cycles are ignored.
    protocol_run = sim.simulate(problem, netlist, workdir, seed, mode="protocol")
    result.metadata["protocol_log"] = protocol_run["log"][-LOG_TAIL:]
    if not protocol_run["compiled"]:
        result.metadata["correctness"] = "protocol testbench failed to compile"
        return result
    if not protocol_run["finished"]:
        result.metadata["correctness"] = {
            "TIMEOUT": "timeout under output backpressure",
            "PROTOCOL": "valid/ready violation - output changed or dropped while stalled",
            "NO RESULT": "protocol run produced no RESULT line",
        }.get(protocol_run["status"], f"protocol run failed ({protocol_run['status']})")
        return result

    try:
        protocol_actual = vectors.read_outputs(
            protocol_run["out_file"], problem.out_len, width
        )
    except vectors.MalformedOutput as exc:
        result.metadata["correctness"] = (
            f"malformed simulator output under backpressure: {exc}"
        )
        return result
    protocol_check = vectors.compare(protocol_actual, expected, width)
    if not protocol_check["match"]:
        result.metadata["correctness"] = (
            f"result changed under output backpressure: {protocol_check['detail']}"
        )
        return result

    result.correct = True
    result.metadata["correctness"] = "exact match under score and backpressure runs"
    result.cycles = run["cycles"]
    result.adp = scoring.area_delay_product(result.cells, result.cycles)

    baseline = load_baseline(problem)
    if baseline:
        baseline_adp = baseline.get("adp", -1.0)
        result.ratio = scoring.ratio(result.adp, baseline_adp)
        result.metadata["baseline"] = baseline

    return result


def record_baseline(problem: Problem, rtl_paths: list[Path]) -> EvalResult:
    """Score the problem's baseline.v on the evaluation seeds and freeze the numbers.

    The baseline runs the same seeds and testbenches as every submission, so
    the denominator and the numerator are measured the same way.
    """
    result = evaluate_multi(
        problem, rtl_paths, seeds=EVAL_SEEDS, source="baseline", tag="baseline"
    )
    if not (result.synthesizable and result.correct):
        return result

    payload = {
        "problem": problem.name,
        "seeds": list(EVAL_SEEDS),
        "cells": result.cells,
        "cycles": result.cycles,
        "adp": result.adp,
        "source": ", ".join(Path(p).name for p in rtl_paths),
    }
    problem.baseline_metrics.write_text(json.dumps(payload, indent=2) + "\n")
    return result


def evaluate_multi(
    problem: Problem,
    rtl_paths: list[Path],
    seeds: tuple[int, ...] = EVAL_SEEDS,
    source: str = "",
    tag: str = "eval",
) -> EvalResult:
    """Score one submission across several held-out seeds and aggregate.

    A design must be correct on every seed. Area is data-independent so the
    cell count is taken as the worst observed; cycles likewise, since a design
    whose latency depends on its data is legitimate but should not be rewarded
    for the lucky seed.
    """
    job = uuid4().hex[:8]
    parts = [
        evaluate(
            problem,
            rtl_paths,
            seed=seed,
            source=source,
            workdir=work_dir(problem, seed, tag=tag, job=job),
        )
        for seed in seeds
    ]
    merged = EvalResult(problem=problem.name, source=source)
    merged.synthesizable = all(p.synthesizable for p in parts)
    merged.compiled = all(p.compiled for p in parts)
    merged.correct = all(p.correct for p in parts)

    cells = [p.cells for p in parts if p.cells >= 0]
    cycles = [p.cycles for p in parts if p.cycles >= 0]
    merged.cells = max(cells) if cells else -1
    merged.cycles = max(cycles) if cycles else -1

    merged.metadata["seeds"] = list(seeds)
    merged.metadata["job"] = job
    merged.metadata["per_seed"] = [
        {
            "seed": s,
            "synthesizable": p.synthesizable,
            "correct": p.correct,
            "cells": p.cells,
            "cycles": p.cycles,
            "correctness": p.metadata.get("correctness", ""),
            "netlist_sha256": p.metadata.get("netlist_sha256", ""),
        }
        for s, p in zip(seeds, parts)
    ]
    # Surface the first real failure so the run record says *why*.
    for p in parts:
        if not p.synthesizable:
            merged.metadata["correctness"] = "synthesis failed"
            merged.metadata["synthesis_log"] = p.metadata.get("synthesis_log", "")
            return merged
        if not p.correct:
            merged.metadata["correctness"] = p.metadata.get("correctness", "incorrect")
            merged.metadata["seed_failed"] = p.metadata.get("seed")
            return merged

    merged.metadata["correctness"] = f"exact match on {len(seeds)} seeds"
    merged.adp = scoring.area_delay_product(merged.cells, merged.cycles)

    baseline = load_baseline(problem)
    if baseline:
        merged.ratio = scoring.ratio(merged.adp, baseline.get("adp", -1.0))
        merged.metadata["baseline"] = baseline

    return merged
