"""Offline diagnostic only: never produces or replaces a benchmark score.

    python -m scripts.diagnose_kimi --out <new dir> [--synthesis]

Simulates the exact saved Kimi GEMV RTL on an all-ones counterexample, then
optionally probes one synthesis invocation with its original 600-second cap.
Run in the credential-free, network-disabled ADPBench toolchain container.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import numpy as np

from adpbench import sim, synth, vectors
from adpbench.audit import audit_submission
from adpbench.durable import atomic_json
from adpbench.hashing import sha256_bytes
from adpbench.problem import load_problem, repo_root

SOURCE = Path("pilot/results/go-core-20260922/opencode-go-kimi-k2.6/002_gemv/rep1_frozen/dut.v")
EXPECTED_PARAMS = {"ROWS": 16, "COLS": 64, "LANES": 16, "DATA_W": 8, "ACC_W": 32}
SYNTHESIS_TIMEOUT_S = 600


def diagnose(out: Path, probe_synthesis: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    source = repo_root() / SOURCE
    if not audit_submission(source.read_text())["ok"]:
        raise RuntimeError("submission failed source audit")
    problem = load_problem(repo_root() / "problems/level1/002_gemv")
    # The scorer applies parameters during synthesis; this diagnostic simulates
    # the RTL directly, so it relies on the source defaults matching them.
    if problem.params != EXPECTED_PARAMS:
        raise RuntimeError("problem parameters changed; the diagnostic no longer applies")

    inputs = tuple(
        np.ones((problem.transactions, problem.input_lens[port]), dtype=np.int64)
        for port in problem.input_ports
    )
    vectors.write_vectors(problem, inputs, out)
    result = sim.simulate(problem, source, out, seed=0, mode="score")
    actual = []
    if result["finished"]:
        actual = vectors.read_outputs(result["out_file"], problem.transactions * problem.out_len, 32)

    report = {
        "kind": "rtl-diagnostic-not-benchmark-score",
        "source": str(SOURCE),
        "submission_sha256": sha256_bytes(source.read_bytes()),
        "tools": {"yosys": synth.tool_version(), "iverilog": sim.tool_version()},
        "case": "all matrix and vector elements equal one; two back-to-back transactions",
        "compiled": result["compiled"],
        "finished": result["finished"],
        "actual": [int(value) for value in actual],
        "expected_each": 64,
        "explanation": (
            "out_c_reg receives the pre-update acc[0] when the final 16 products are added with "
            "nonblocking assignments; the first output of each transaction omits them"
        ),
    }
    atomic_json(out / "diagnosis.json", report)
    print(json.dumps(report), flush=True)

    if probe_synthesis:
        report["synthesis_probe"] = _probe_synthesis(problem, source, out / "synthesis")
        atomic_json(out / "diagnosis.json", report)
        print(json.dumps(report["synthesis_probe"]), flush=True)
    return report


def _probe_synthesis(problem, source: Path, work: Path) -> dict:
    """Run yosys once with the original cap, streaming its log to disk."""
    work.mkdir()
    script = synth.build_script(problem, [source], work)
    log_path = work / "yosys.log"
    started = time.monotonic()
    with log_path.open("w") as log:
        try:
            proc = subprocess.run(
                ["yosys", str(script)],
                cwd=work,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=SYNTHESIS_TIMEOUT_S,
            )
            observation = {"returncode": proc.returncode, "timed_out": False}
        except subprocess.TimeoutExpired:
            observation = {"returncode": None, "timed_out": True}
    observation["duration_s"] = round(time.monotonic() - started, 2)
    observation["log_tail"] = log_path.read_text()[-4000:]
    return observation


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--synthesis", action="store_true")
    args = parser.parse_args()
    diagnose(args.out.resolve(), args.synthesis)
