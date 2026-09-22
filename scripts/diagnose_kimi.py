"""Offline diagnostic only: never produces or replaces a benchmark score.

Simulate the exact saved Kimi GEMV RTL on an all-ones counterexample, then
optionally probe one synthesis invocation with its original 600-second cap.
Run in the credential-free, network-disabled ADPBench toolchain container.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import numpy as np

from adpbench import sim, synth, vectors
from adpbench.agent import audit_submission
from adpbench.problem import load_problem, repo_root
from scripts.go_pilot import write_json

SOURCE = Path("pilot/results/go-core-20260922/opencode-go-kimi-k2.6/002_gemv/rep1_frozen/dut.v")


def diagnose(out: Path, probe_synthesis: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    source = repo_root() / SOURCE
    assert audit_submission(source.read_text())["ok"], "submission failed source audit"
    problem = load_problem(repo_root() / "problems/level1/002_gemv")
    # These source defaults match the actual benchmark parameters. The normal
    # scorer elaborates parameters in Yosys; this direct RTL diagnostic does not.
    assert problem.params == {"ROWS": 16, "COLS": 64, "LANES": 16, "DATA_W": 8, "ACC_W": 32}
    inputs = tuple(np.ones((problem.transactions, problem.input_lens[name]), dtype=np.int64)
                   for name in problem.input_ports)
    vectors.write_vectors(problem, inputs, out)
    result = sim.simulate(problem, source, out, seed=0, mode="score")
    actual = vectors.read_outputs(result["out_file"], problem.transactions * problem.out_len, 32) if result["finished"] else []
    report = {
        "kind": "rtl-diagnostic-not-benchmark-score", "source": str(SOURCE),
        "submission_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "tools": {"yosys": synth.tool_version(), "iverilog": sim.tool_version()},
        "case": "all matrix and vector elements equal one; two back-to-back transactions",
        "compiled": result["compiled"], "finished": result["finished"],
        "actual": [int(v) for v in actual], "expected_each": 64,
        "explanation": "out_c_reg receives the pre-update acc[0] when the final 16 products are added with nonblocking assignments; the first output of each transaction omits them",
    }
    write_json(out / "diagnosis.json", report)
    print(json.dumps(report), flush=True)
    if probe_synthesis:
        work = out / "synthesis"
        work.mkdir()
        script = synth._build_script(problem, [source], work)
        start = time.monotonic()
        # Stream the log to disk so an interrupted tool leaves stage evidence.
        with (work / "yosys.log").open("w") as log:
            try:
                proc = subprocess.run(["yosys", str(script)], cwd=work, stdout=log, stderr=subprocess.STDOUT, timeout=600)
                observation = {"returncode": proc.returncode, "timed_out": False}
            except subprocess.TimeoutExpired:
                observation = {"returncode": None, "timed_out": True}
        observation["duration_s"] = round(time.monotonic() - start, 2)
        observation["log_tail"] = (work / "yosys.log").read_text()[-4000:]
        report["synthesis_probe"] = observation
        write_json(out / "diagnosis.json", report)
        print(json.dumps(observation), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--synthesis", action="store_true")
    args = parser.parse_args()
    diagnose(args.out.resolve(), args.synthesis)
