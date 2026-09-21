"""The RTL evaluation pipeline: synthesis, reference checks, and scoring.

    dut.v
        -> synthesize (yosys)      fixed parameters, gate netlist, cell count
        -> build vectors (numpy)   seeded random inputs + golden outputs
        -> simulate (iverilog)     the netlist, exact match? how many cycles?
        -> score                   baseline_adp / adp

Stages short-circuit: a design that does not synthesize is never simulated.
Synthesis and simulation judge one artifact: the parameters applied at
synthesis are the parameters the testbench drives, and the simulated netlist
is the netlist whose cells were counted.

Every case (a random seed or a directed pattern) drives all of the problem's
back-to-back transactions, so per-transaction state bugs are caught.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


def work_root() -> Path:
    """Where evaluation scratch lives.

    `ADPBENCH_WORKDIR` overrides the default; sandboxed feedback runs point it
    at the writable task mount because the harness bundle is read-only.
    """
    override = os.environ.get("ADPBENCH_WORKDIR")
    return Path(override) if override else repo_root() / ".adpbench"


def work_dir(problem: Problem, seed: int | str, tag: str = "", job: str = "") -> Path:
    """A per-invocation directory.

    `job` keeps concurrent evaluations of the same problem and case from
    overwriting each other's scripts, netlists, and vectors.
    """
    label = f"seed{seed}" if isinstance(seed, int) else str(seed)
    suffix = label + (f"_{tag}" if tag else "") + (f"_{job}" if job else "")
    return work_root() / problem.name / suffix


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


def _current_tools() -> dict:
    return {"yosys": synth.tool_version(), "iverilog": sim.tool_version()}


def _tool_id(version: str) -> str:
    """The reproducible part of a tool version string.

    The sandbox image builds yosys from the same commit and iverilog from the
    same release as the host, but their version strings differ in the version
    suffix (0.69+post vs 0.69+), sha length, and compiler. The git sha (for
    yosys) and the release number (for iverilog) are the authoritative pins.
    """
    version = version or ""
    yosys = re.search(r"git sha1 ([0-9a-f]{9,40})", version)
    if yosys:
        return "yosys@" + yosys.group(1)[:9]
    iverilog = re.match(r"^(Icarus Verilog version [\d.]+)", version)
    if iverilog:
        return iverilog.group(1)
    return version.strip()


def _tool_ids(tools: dict | None) -> dict:
    tools = tools or {}
    return {
        name: _tool_id(str(version))
        for name, version in tools.items()
        if version
    }


def baseline_mismatch(problem: Problem, baseline: dict) -> str:
    """Reasons a stored denominator cannot be compared with this run.

    A baseline is only meaningful under the same parameters, transactions,
    flow, and toolchain; otherwise a silently different evaluation produces a
    silently incomparable ratio.
    """
    reasons = []
    if baseline.get("transactions") != problem.transactions:
        reasons.append("transactions")
    if baseline.get("params") != problem.params:
        reasons.append("params")
    if baseline.get("input_lens") != problem.input_lens:
        reasons.append("input_lens")
    if baseline.get("flow_sha256") != _sha256_file(synth.FLOW):
        reasons.append("flow")
    if _tool_ids(baseline.get("tools")) != _tool_ids(_current_tools()):
        reasons.append("tools")
    return ",".join(reasons)


def _ratio(problem: Problem, result: EvalResult, baseline: dict | None) -> float:
    """A usable ratio, or -1 with the mismatch recorded in metadata."""
    if not baseline:
        return -1.0
    mismatch = baseline_mismatch(problem, baseline)
    if mismatch:
        result.metadata["baseline_incompatible"] = mismatch
        return -1.0
    result.metadata["baseline"] = baseline
    return scoring.ratio(result.adp, baseline.get("adp", -1.0))


def _sim_failure_message(run: dict, context: str) -> str:
    messages = {
        "TIMEOUT": f"timeout {context} - DUT never produced a full result",
        "PROTOCOL": "valid/ready or output-framing violation",
        "NO RESULT": f"simulator produced no RESULT line {context}",
    }
    return messages.get(run["status"], f"simulation failed {context} ({run['status']})")


def evaluate(
    problem: Problem,
    rtl_paths: list[Path],
    seed: int | str = 0,
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
    result.metadata["case"] = seed
    result.metadata["flow"] = "flows/synth.ys"
    result.metadata["params"] = problem.params
    result.metadata["transactions"] = problem.transactions
    result.metadata["input_lens"] = problem.input_lens
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
        result.metadata["stage"] = "synthesis"
        result.metadata["synthesis_log"] = syn["log"][-LOG_TAIL:]
        return result

    netlist = syn["netlist"]
    result.metadata["netlist_sha256"] = _sha256_file(netlist)

    inputs, expected = vectors.build_vectors(problem, seed)
    vectors.write_vectors(problem, inputs, workdir)
    total_out = problem.out_len * problem.transactions

    # Timing run: no testbench-imposed idle cycles, so `cycles` measures the
    # DUT, not the harness.
    run = sim.simulate(problem, netlist, workdir, seed, mode="score")
    result.compiled = run["compiled"]
    result.metadata["sim_log"] = run["log"][-LOG_TAIL:]
    if not run["compiled"]:
        result.metadata["stage"] = "compile"
        result.metadata["correctness"] = "failed to compile"
        return result
    if not run["finished"]:
        result.metadata["stage"] = run["status"].lower()
        result.metadata["correctness"] = _sim_failure_message(run, "in the timing run")
        return result

    width = int(problem.params["ACC_W"])
    try:
        actual = vectors.read_outputs(run["out_file"], total_out, width)
    except vectors.MalformedOutput as exc:
        result.metadata["stage"] = "malformed"
        result.metadata["correctness"] = f"malformed simulator output: {exc}"
        return result
    check = vectors.compare(actual, expected, width)
    if not check["match"]:
        result.metadata["stage"] = "incorrect"
        result.metadata["correctness"] = check["detail"]
        return result

    # Conformance run: seeded input gaps, seeded output backpressure, and a
    # hold-stable check. Results must still match; cycles are ignored.
    protocol_run = sim.simulate(problem, netlist, workdir, seed, mode="protocol")
    result.metadata["protocol_log"] = protocol_run["log"][-LOG_TAIL:]
    if not protocol_run["compiled"]:
        result.metadata["stage"] = "protocol_compile"
        result.metadata["correctness"] = "protocol testbench failed to compile"
        return result
    if not protocol_run["finished"]:
        result.metadata["stage"] = (
            "protocol"
            if protocol_run["status"] == "PROTOCOL"
            else f"protocol_{protocol_run['status'].lower()}"
        )
        result.metadata["correctness"] = _sim_failure_message(
            protocol_run, "under backpressure"
        )
        return result

    try:
        protocol_actual = vectors.read_outputs(
            protocol_run["out_file"], total_out, width
        )
    except vectors.MalformedOutput as exc:
        result.metadata["stage"] = "protocol_malformed"
        result.metadata["correctness"] = (
            f"malformed simulator output under backpressure: {exc}"
        )
        return result
    protocol_check = vectors.compare(protocol_actual, expected, width)
    if not protocol_check["match"]:
        result.metadata["stage"] = "protocol_mismatch"
        result.metadata["correctness"] = (
            f"result changed under output backpressure: {protocol_check['detail']}"
        )
        return result

    result.correct = True
    result.metadata["stage"] = "ok"
    result.metadata["correctness"] = "exact match under score and backpressure runs"
    result.cycles = run["cycles"]
    result.adp = scoring.area_delay_product(result.cells, result.cycles)
    result.ratio = _ratio(problem, result, load_baseline(problem))
    return result


def record_baseline(problem: Problem, rtl_paths: list[Path]) -> EvalResult:
    """Score the problem's baseline.v on the evaluation cases and freeze the numbers.

    The baseline runs the same seeds, directed cases, transactions, and
    testbenches as every submission, so the denominator and the numerator are
    measured the same way.
    """
    result = evaluate_multi(
        problem, rtl_paths, tag="baseline", source="baseline"
    )
    if not (result.synthesizable and result.correct):
        return result

    payload = {
        "problem": problem.name,
        "seeds": list(EVAL_SEEDS),
        "directed": list(problem.directed_cases),
        "transactions": problem.transactions,
        "params": problem.params,
        "input_lens": problem.input_lens,
        "flow_sha256": _sha256_file(synth.FLOW),
        "tools": _current_tools(),
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
    include_directed: bool = True,
) -> EvalResult:
    """Score one submission across held-out seeds and directed cases, then aggregate.

    A design must be correct on every case. Area is data-independent so the
    cell count is taken as the worst observed; cycles likewise, since a design
    whose latency depends on its data is legitimate but should not be rewarded
    for the lucky case.
    """
    cases: list[int | str] = list(seeds)
    if include_directed:
        cases += problem.directed_cases

    job = uuid4().hex[:8]
    parts = [
        evaluate(
            problem,
            rtl_paths,
            seed=case,
            source=source,
            workdir=work_dir(problem, case, tag=tag, job=job),
        )
        for case in cases
    ]
    merged = EvalResult(problem=problem.name, source=source)
    merged.synthesizable = all(p.synthesizable for p in parts)
    merged.compiled = all(p.compiled for p in parts)
    merged.correct = all(p.correct for p in parts)

    cells = [p.cells for p in parts if p.cells >= 0]
    cycles = [p.cycles for p in parts if p.cycles >= 0]
    merged.cells = max(cells) if cells else -1
    merged.cycles = max(cycles) if cycles else -1

    merged.metadata["cases"] = list(cases)
    merged.metadata["job"] = job
    merged.metadata["per_case"] = [
        {
            "case": case,
            "synthesizable": p.synthesizable,
            "correct": p.correct,
            "cells": p.cells,
            "cycles": p.cycles,
            "stage": p.metadata.get("stage", ""),
            "correctness": p.metadata.get("correctness", ""),
            "netlist_sha256": p.metadata.get("netlist_sha256", ""),
        }
        for case, p in zip(cases, parts)
    ]
    # Surface the first real failure so the run record says *why*.
    for p in parts:
        if not p.synthesizable:
            merged.metadata["stage"] = "synthesis"
            merged.metadata["correctness"] = "synthesis failed"
            merged.metadata["synthesis_log"] = p.metadata.get("synthesis_log", "")
            return merged
        if not p.correct:
            merged.metadata["stage"] = p.metadata.get("stage", "")
            merged.metadata["correctness"] = p.metadata.get("correctness", "incorrect")
            merged.metadata["case_failed"] = p.metadata.get("case")
            return merged

    merged.metadata["stage"] = "ok"
    merged.metadata["correctness"] = f"exact match on {len(cases)} cases"
    merged.adp = scoring.area_delay_product(merged.cells, merged.cycles)
    merged.ratio = _ratio(problem, merged, load_baseline(problem))
    return merged
