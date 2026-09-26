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

`evaluate` scores one case. `evaluate_multi` scores every case, synthesizing
once, and keeps an on-disk checkpoint so an interrupted run can resume.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from . import score as scoring
from . import sim, synth, vectors
from .durable import atomic_json
from .hashing import sha256_file
from .problem import Problem, repo_root
from .result import EvalResult
from .seeds import EVAL_SEEDS

LOG_TAIL = 4000

# The files whose contents decide a score. A checkpoint made by different
# versions of any of these is not reused.
HARNESS_FILES = (
    "evaluate.py",
    "synth.py",
    "sim.py",
    "vectors.py",
    "score.py",
    "result.py",
    "process.py",
    "durable.py",
    "hashing.py",
)

# --------------------------------------------------------------------------
# Working directories and baselines
# --------------------------------------------------------------------------


def work_root() -> Path:
    """Where evaluation scratch lives.

    `ADPBENCH_WORKDIR` overrides the default; sandboxed feedback runs point it
    at the writable task mount because the harness bundle is read-only.
    """
    override = os.environ.get("ADPBENCH_WORKDIR")
    return Path(override) if override else repo_root() / ".adpbench"


def work_dir(problem: Problem, seed: int | str, tag: str = "", job: str = "") -> Path:
    """A per-invocation scratch directory.

    `job` keeps concurrent evaluations of the same problem and case from
    overwriting each other's scripts, netlists, and vectors.
    """
    name = f"seed{seed}" if isinstance(seed, int) else str(seed)
    if tag:
        name += f"_{tag}"
    if job:
        name += f"_{job}"
    return work_root() / problem.name / name


def load_baseline(problem: Problem) -> dict | None:
    if not problem.baseline_metrics.is_file():
        return None
    return json.loads(problem.baseline_metrics.read_text())


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
    return {name: _tool_id(str(version)) for name, version in (tools or {}).items() if version}


def baseline_mismatch(problem: Problem, baseline: dict) -> str:
    """Comma-separated reasons a stored baseline cannot be compared with this run.

    A baseline is only meaningful under the same parameters, transactions,
    flow, and toolchain; otherwise a silently different evaluation produces a
    silently incomparable ratio. Returns "" when the baseline is usable.
    """
    reasons = []
    if baseline.get("transactions") != problem.transactions:
        reasons.append("transactions")
    if baseline.get("params") != problem.params:
        reasons.append("params")
    if baseline.get("input_lens") != problem.input_lens:
        reasons.append("input_lens")
    if baseline.get("flow_sha256") != sha256_file(synth.FLOW):
        reasons.append("flow")
    if _tool_ids(baseline.get("tools")) != _tool_ids(_current_tools()):
        reasons.append("tools")
    return ",".join(reasons)


def _ratio(problem: Problem, result: EvalResult, baseline: dict | None) -> float:
    """The baseline ratio, or -1 (with the reason recorded in metadata)."""
    if not baseline:
        return -1.0
    mismatch = baseline_mismatch(problem, baseline)
    if mismatch:
        result.metadata["baseline_incompatible"] = mismatch
        return -1.0
    result.metadata["baseline"] = baseline
    return scoring.ratio(result.adp, baseline.get("adp", -1.0))


def record_baseline(problem: Problem, rtl_paths: list[Path]) -> EvalResult:
    """Score the problem's baseline.v on the evaluation cases and freeze the numbers.

    The baseline runs the same seeds, directed cases, transactions, and
    testbenches as every submission, so the denominator and the numerator are
    measured the same way.
    """
    result = evaluate_multi(problem, rtl_paths, tag="baseline", source="baseline")
    if not (result.synthesizable and result.correct):
        return result

    payload = {
        "problem": problem.name,
        "seeds": list(EVAL_SEEDS),
        "directed": list(problem.directed_cases),
        "transactions": problem.transactions,
        "params": problem.params,
        "input_lens": problem.input_lens,
        "flow_sha256": sha256_file(synth.FLOW),
        "tools": _current_tools(),
        "cells": result.cells,
        "cycles": result.cycles,
        "adp": result.adp,
        "source": ", ".join(Path(p).name for p in rtl_paths),
    }
    problem.baseline_metrics.write_text(json.dumps(payload, indent=2) + "\n")
    return result


# --------------------------------------------------------------------------
# One case
# --------------------------------------------------------------------------


def evaluate(
    problem: Problem,
    rtl_paths: list[Path],
    seed: int | str = 0,
    source: str = "",
    workdir: Path | None = None,
    *,
    synthesis: dict | None = None,
    deadline_at: float | None = None,
) -> EvalResult:
    """Score a design on one case: an integer seed or a directed-case name.

    Pass `synthesis` to reuse an earlier `synth.synthesize` result instead of
    synthesizing again. `deadline_at` is an absolute `time.monotonic()` limit
    for every tool this call runs.
    """
    rtl_paths = [Path(p).resolve() for p in rtl_paths]
    if workdir:
        workdir = Path(workdir).resolve()
    else:
        workdir = work_dir(problem, seed, job=uuid4().hex[:8])
    workdir.mkdir(parents=True, exist_ok=True)

    result = EvalResult(
        problem=problem.name,
        source=source or ", ".join(p.name for p in rtl_paths),
    )
    result.metadata["case"] = seed
    result.metadata["flow"] = "flows/synth.ys"
    result.metadata["params"] = problem.params
    result.metadata["transactions"] = problem.transactions
    result.metadata["input_lens"] = problem.input_lens
    result.metadata["workdir"] = str(workdir)
    result.metadata["tools"] = _current_tools()

    # 1. Synthesis.
    if synthesis is None:
        synthesis = synth.synthesize(problem, rtl_paths, workdir, deadline_at=deadline_at)
    result.synthesizable = synthesis["ok"]
    result.cells = synthesis["cells"]
    result.metadata["cells"] = synthesis["cells"]
    if not synthesis["ok"]:
        result.metadata["stage"] = "synthesis"
        result.metadata["synthesis_log"] = synthesis["log"][-LOG_TAIL:]
        result.metadata["failure_kind"] = synthesis.get("failure_kind", "synthesis_error")
        return result
    netlist = synthesis["netlist"]
    result.metadata["netlist_sha256"] = sha256_file(netlist)

    # 2. Test vectors.
    inputs, expected = vectors.build_vectors(problem, seed)
    vectors.write_vectors(problem, inputs, workdir)

    # 3. Timing run: no testbench-imposed idle cycles, so `cycles` measures
    #    the DUT, not the harness.
    timing_run = sim.simulate(problem, netlist, workdir, seed, mode="score", deadline_at=deadline_at)
    result.compiled = timing_run["compiled"]
    result.metadata["sim_log"] = timing_run["log"][-LOG_TAIL:]
    if timing_run.get("failure_kind"):
        result.metadata["failure_kind"] = timing_run["failure_kind"]
    failure = _timing_run_failure(problem, timing_run, expected)
    if failure:
        return _failed(result, *failure)

    # 4. Protocol run: seeded input gaps, seeded output backpressure, and a
    #    hold-stable check. Results must still match; cycles are ignored.
    protocol_run = sim.simulate(problem, netlist, workdir, seed, mode="protocol", deadline_at=deadline_at)
    result.metadata["protocol_log"] = protocol_run["log"][-LOG_TAIL:]
    if protocol_run.get("failure_kind"):
        result.metadata["failure_kind"] = protocol_run["failure_kind"]
    failure = _protocol_run_failure(problem, protocol_run, expected)
    if failure:
        return _failed(result, *failure)

    # 5. Score.
    result.correct = True
    result.metadata["stage"] = "ok"
    result.metadata["correctness"] = "exact match under score and backpressure runs"
    result.cycles = timing_run["cycles"]
    result.adp = scoring.area_delay_product(result.cells, result.cycles)
    result.ratio = _ratio(problem, result, load_baseline(problem))
    return result


def _failed(result: EvalResult, stage: str, correctness: str) -> EvalResult:
    result.metadata["stage"] = stage
    result.metadata["correctness"] = correctness
    return result


def _timing_run_failure(problem: Problem, run: dict, expected) -> tuple[str, str] | None:
    """(stage, correctness message) if the timing run failed, else None."""
    if not run["compiled"]:
        return "compile", "failed to compile"
    if not run["finished"]:
        return run["status"].lower(), _simulation_failure_message(run, "in the timing run")

    width = int(problem.params["ACC_W"])
    try:
        actual = vectors.read_outputs(run["out_file"], _total_output_words(problem), width)
    except vectors.MalformedOutput as exc:
        return "malformed", f"malformed simulator output: {exc}"

    check = vectors.compare(actual, expected, width)
    if not check["match"]:
        return "incorrect", check["detail"]
    return None


def _protocol_run_failure(problem: Problem, run: dict, expected) -> tuple[str, str] | None:
    """(stage, correctness message) if the protocol run failed, else None."""
    if not run["compiled"]:
        return "protocol_compile", "protocol testbench failed to compile"
    if not run["finished"]:
        if run["status"] == "PROTOCOL":
            stage = "protocol"
        else:
            stage = f"protocol_{run['status'].lower()}"
        return stage, _simulation_failure_message(run, "under backpressure")

    width = int(problem.params["ACC_W"])
    try:
        actual = vectors.read_outputs(run["out_file"], _total_output_words(problem), width)
    except vectors.MalformedOutput as exc:
        return "protocol_malformed", f"malformed simulator output under backpressure: {exc}"

    check = vectors.compare(actual, expected, width)
    if not check["match"]:
        return "protocol_mismatch", f"result changed under output backpressure: {check['detail']}"
    return None


def _total_output_words(problem: Problem) -> int:
    return problem.out_len * problem.transactions


def _simulation_failure_message(run: dict, context: str) -> str:
    status = run["status"]
    if status == "TIMEOUT":
        return f"timeout {context} - DUT never produced a full result"
    if status == "PROTOCOL":
        return "valid/ready or output-framing violation"
    if status == "NO RESULT":
        return f"simulator produced no RESULT line {context}"
    return f"simulation failed {context} ({status})"


# --------------------------------------------------------------------------
# Every case, with a resumable checkpoint
# --------------------------------------------------------------------------


def evaluate_multi(
    problem: Problem,
    rtl_paths: list[Path],
    seeds: tuple[int, ...] = EVAL_SEEDS,
    source: str = "",
    tag: str = "eval",
    include_directed: bool = True,
    checkpoint_dir: Path | None = None,
    resume: bool = False,
    deadline_s: float | None = None,
) -> EvalResult:
    """Score one submission on every seed and directed case, then aggregate.

    A design must be correct on every case. Area is data-independent so the
    cell count is taken as the worst observed; cycles likewise, since a design
    whose latency depends on its data is legitimate but should not be rewarded
    for the lucky case.

    Progress is written to a checkpoint directory:

        checkpoint.json       what is being scored, and sha256 of finished files
        synthesis.json        the synthesis result
        synthesis/netlist.v   the netlist every case simulates
        case-<i>/result.json  each finished case
        result.json           the aggregated result

    With `resume=True`, finished work in an existing checkpoint is reused -
    but only after its hash and identity are checked, so stale or tampered
    evidence is refused rather than trusted. `deadline_s` bounds the wall time
    of the whole call; cases not started before it are reported as a timeout.
    """
    cases: list[int | str] = list(seeds)
    if include_directed:
        cases += problem.directed_cases
    if not cases:
        raise ValueError("evaluation requires at least one case")

    deadline_at = time.monotonic() + deadline_s if deadline_s is not None else None
    job = uuid4().hex[:8]
    if checkpoint_dir:
        root = Path(checkpoint_dir).resolve()
    else:
        root = work_dir(problem, "checkpoint", tag=tag, job=job)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "checkpoint.json"
    identity = _checkpoint_identity(problem, rtl_paths, cases)

    with (root / ".lock").open("a") as lock:
        # Only one scorer may use a checkpoint directory at a time.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        state = _start_or_resume_checkpoint(state_path, identity, job, resume)
        synthesis = _synthesize_once(problem, rtl_paths, root, state, state_path, deadline_at)

        parts = []
        for index, case in enumerate(cases):
            part_path = root / f"case-{index}" / "result.json"
            key = str(index)
            if key in state["completed"]:
                parts.append(_load_finished_case(part_path, state["completed"][key]))
                continue

            state.update(state="case", current_case=case)
            atomic_json(state_path, state)
            if deadline_at is not None and time.monotonic() >= deadline_at:
                # Record the timeout, but never claim this case completed.
                parts.append(_deadline_result(problem, source, case))
                break

            part = evaluate(
                problem,
                rtl_paths,
                seed=case,
                source=source,
                workdir=part_path.parent,
                synthesis=synthesis,
                deadline_at=deadline_at,
            )
            atomic_json(part_path, part.to_dict())
            state["completed"][key] = sha256_file(part_path)
            atomic_json(state_path, state)
            parts.append(part)

        merged = _aggregate(problem, parts, cases, source, state["job"])
        result_path = root / "result.json"
        atomic_json(result_path, merged.to_dict())
        finished_every_case = len(state["completed"]) == len(cases)
        state["state"] = "completed" if finished_every_case else "interrupted"
        state["result_sha256"] = sha256_file(result_path)
        atomic_json(state_path, state)
        return merged


def _checkpoint_identity(problem: Problem, rtl_paths: list[Path], cases: list) -> dict:
    """Everything that decides the score. A checkpoint is only resumed if this matches."""
    baseline_sha = None
    if problem.baseline_metrics.exists():
        baseline_sha = sha256_file(problem.baseline_metrics)
    package_dir = Path(__file__).parent
    return {
        "schema_version": 1,
        "problem": problem.name,
        "params": problem.params,
        "cases": cases,
        "rtl": [sha256_file(path) for path in rtl_paths],
        "spec": sha256_file(problem.root / "dut.py"),
        "flow": sha256_file(synth.FLOW),
        "baseline": baseline_sha,
        "tools": _tool_ids(_current_tools()),
        "harness": {name: sha256_file(package_dir / name) for name in HARNESS_FILES},
    }


def _start_or_resume_checkpoint(state_path: Path, identity: dict, job: str, resume: bool) -> dict:
    if not state_path.exists():
        state = {"identity": identity, "job": job, "state": "started", "completed": {}}
        atomic_json(state_path, state)
        return state

    if not resume:
        raise FileExistsError(
            "checkpoint already exists; explicitly resume or select a new directory"
        )
    state = json.loads(state_path.read_text())
    if state.get("identity") != identity:
        raise ValueError("checkpoint identity changed; refusing stale scoring evidence")
    return state


def _synthesize_once(
    problem: Problem,
    rtl_paths: list[Path],
    root: Path,
    state: dict,
    state_path: Path,
    deadline_at: float | None,
) -> dict:
    """Synthesize into the checkpoint, or reuse a hash-verified earlier synthesis."""
    saved_path = root / "synthesis.json"
    netlist_path = root / "synthesis" / synth.NETLIST

    if state.get("synthesis_sha256"):
        if sha256_file(saved_path) != state["synthesis_sha256"]:
            raise ValueError("synthesis checkpoint hash mismatch")
        synthesis = json.loads(saved_path.read_text())
        if synthesis["ok"]:
            if sha256_file(netlist_path) != synthesis["netlist_sha256"]:
                raise ValueError("cached netlist hash mismatch")
            synthesis["netlist"] = netlist_path
        return synthesis

    state["state"] = "synthesis"
    atomic_json(state_path, state)
    synthesis = synth.synthesize(problem, rtl_paths, root / "synthesis", deadline_at=deadline_at)

    saved = dict(synthesis)
    saved["netlist"] = synth.NETLIST if synthesis["ok"] else None
    saved["log"] = synthesis["log"][-LOG_TAIL:]
    if synthesis["ok"]:
        saved["netlist_sha256"] = sha256_file(synthesis["netlist"])
    atomic_json(saved_path, saved)

    state["synthesis_sha256"] = sha256_file(saved_path)
    atomic_json(state_path, state)
    return synthesis


def _load_finished_case(path: Path, expected_sha: str) -> EvalResult:
    if sha256_file(path) != expected_sha:
        raise ValueError("case checkpoint hash mismatch")
    return EvalResult(**json.loads(path.read_text()))


def _deadline_result(problem: Problem, source: str, case: int | str) -> EvalResult:
    return EvalResult(
        problem=problem.name,
        source=source,
        metadata={
            "stage": "evaluation_timeout",
            "case": case,
            "failure_kind": "wall_timeout",
            "correctness": "evaluation wall deadline exhausted",
        },
    )


def _aggregate(
    problem: Problem, parts: list[EvalResult], cases: list, source: str, job: str
) -> EvalResult:
    """Combine per-case results: correct only if every case is correct."""
    merged = EvalResult(problem=problem.name, source=source)
    merged.synthesizable = all(part.synthesizable for part in parts)
    merged.compiled = all(part.compiled for part in parts)
    merged.correct = all(part.correct for part in parts)

    cells = [part.cells for part in parts if part.cells >= 0]
    cycles = [part.cycles for part in parts if part.cycles >= 0]
    merged.cells = max(cells) if cells else -1
    merged.cycles = max(cycles) if cycles else -1

    merged.metadata["cases"] = list(cases)
    merged.metadata["job"] = job
    merged.metadata["per_case"] = [
        {
            "case": case,
            "synthesizable": part.synthesizable,
            "correct": part.correct,
            "cells": part.cells,
            "cycles": part.cycles,
            "stage": part.metadata.get("stage", ""),
            "correctness": part.metadata.get("correctness", ""),
            "netlist_sha256": part.metadata.get("netlist_sha256", ""),
        }
        for case, part in zip(cases, parts)
    ]

    # Surface the first real failure so the run record says *why*.
    for part in parts:
        if not part.synthesizable:
            merged.metadata["stage"] = "synthesis"
            merged.metadata["correctness"] = "synthesis failed"
            merged.metadata["synthesis_log"] = part.metadata.get("synthesis_log", "")
            merged.metadata["failure_kind"] = part.metadata.get("failure_kind", "synthesis_error")
            if part.metadata.get("stage") == "evaluation_timeout":
                merged.metadata["stage"] = "evaluation_timeout"
                merged.metadata["correctness"] = part.metadata["correctness"]
            return merged
        if not part.correct:
            merged.metadata["stage"] = part.metadata.get("stage", "")
            merged.metadata["correctness"] = part.metadata.get("correctness", "incorrect")
            merged.metadata["case_failed"] = part.metadata.get("case")
            if part.metadata.get("failure_kind"):
                merged.metadata["failure_kind"] = part.metadata["failure_kind"]
            return merged

    merged.metadata["stage"] = "ok"
    merged.metadata["correctness"] = f"exact match on {len(cases)} cases"
    merged.adp = scoring.area_delay_product(merged.cells, merged.cycles)
    merged.ratio = _ratio(problem, merged, load_baseline(problem))
    return merged
