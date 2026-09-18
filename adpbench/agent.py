"""The agent harness: problem environments, submission auditing, run records.

The design keeps three things separate:

  environment   what the agent is allowed to see and run
  audit         what the agent is not allowed to do
  scoring       what the agent actually gets credit for

The environment is agent-agnostic: it is a directory with a spec, the golden
model, a skeleton, and a `check` command. Any agent CLI that has a shell can be
dropped into it. Nothing here knows what Claude Code or Codex is.

Scoring never trusts the environment directory. The submission is copied alone
into a clean room and scored against pristine problem files on seeds the agent
never saw.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .evaluate import DEV_SEEDS, EVAL_SEEDS, evaluate_multi
from .problem import Problem, repo_root

META_FILE = ".adpbench.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

# Rejected outright. Each of these can make a design behave differently under
# simulation than under synthesis, or let it read the expected answers.
VIOLATIONS = (
    (r"\$readmem", "file-based memory load: not synthesizable, reads external data"),
    (r"\$f(?:open|close|scanf|gets|read|eof)", "file I/O: not synthesizable"),
    (r"\$(?:display|write|monitor)", "simulator output: not synthesizable"),
    (r"\$(?:finish|stop)", "halts the simulator"),
    (r"\$(?:dumpfile|dumpvars|dumpflush)", "waveform dumping: not synthesizable"),
    (r"\binitial\b", "initial block: simulates but does not synthesize"),
    (r"#\s*\d", "delay control: not synthesizable"),
    (r"\b(?:force|release)\b", "force/release: not synthesizable"),
    (r"`include", "must be a single self-contained file"),
    (r"\btb\s*\.", "references the testbench hierarchy"),
    (r"\bu_dut\s*\.", "references its own instance path"),
)

# Recorded but allowed. These usually mean the design is confused, not cheating.
WARNINGS = (
    (r"\b(?:assert|assume|cover)\b", "SystemVerilog assertion: ignored by synthesis"),
    (r"\b(?:real|shortreal|time)\b", "non-synthesisable data type"),
    (r"\bwait\b", "wait statement: check it is synthesisable"),
)


def _scan(patterns, text: str) -> list[dict]:
    hits = []
    for pattern, reason in patterns:
        for match in re.finditer(pattern, text):
            hits.append(
                {
                    "line": text.count("\n", 0, match.start()) + 1,
                    "match": match.group(0),
                    "reason": reason,
                }
            )
    return sorted(hits, key=lambda h: h["line"])


def mask_noncode(text: str) -> str:
    """Blank out comments and string literals, preserving line numbers.

    The audit looks for executable constructs. Words inside comments or
    strings are not code, and matching them was a false positive; blanking
    keeps the reported line numbers aligned with the original file.
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif ch == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif ch == '"':
            out[i] = " "
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    i += 1
                if i < n and text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def audit_submission(text: str) -> dict:
    """Static checks on a submission. Returns {ok, violations, warnings}."""
    code = mask_noncode(text)
    violations = _scan(VIOLATIONS, code)
    warnings = _scan(WARNINGS, code)

    if not re.search(r"\bmodule\s+dut\b", code):
        violations.append(
            {"line": 0, "match": "module dut", "reason": "no module named `dut` found"}
        )

    return {
        "ok": not violations,
        "violations": violations[:20],
        "warnings": warnings[:20],
    }


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

SKELETON_COMMENT = """\
// ADPBench submission. Replace the body; keep the module name and ports.
// Run ./check.sh for synthesis + simulation feedback.
// The testbench drives several back-to-back transactions without reset, and
// every input port has its own valid/ready handshake.
"""


def render_ports(problem: Problem) -> str:
    params = problem.params
    names = list(params)
    lines = ["module dut #("]
    for i, name in enumerate(names):
        comma = "," if i < len(names) - 1 else ""
        lines.append(f"    parameter {name} = {params[name]}{comma}")
    lines.append(")(")
    lines.append("    input  wire                    clk,")
    lines.append("    input  wire                    rst_n,")
    for port in problem.input_ports:
        lines.append(f"    input  wire [LANES*DATA_W-1:0] {port},")
        lines.append(f"    input  wire                    {port}_valid,")
        lines.append(f"    output wire                    {port}_ready,")
    lines.append("    output wire                    out_valid,")
    lines.append("    input  wire                    out_ready,")
    lines.append(f"    output wire signed [ACC_W-1:0] {problem.output_port}")
    lines.append(");")
    return "\n".join(lines)


def render_skeleton(problem: Problem) -> str:
    return f"{SKELETON_COMMENT}\n{render_ports(problem)}\n\n    // TODO\n\nendmodule\n"


def render_problem_md(problem: Problem, baseline: dict | None) -> str:
    params = "\n".join(f"| `{k}` | {v} |" for k, v in problem.params.items())
    quant = "\n".join(f"| `{k}` | {v} |" for k, v in problem.quant.items())
    streams = "\n".join(
        f"| `{port}` | {problem.input_lens[port]} |" for port in problem.input_ports
    )

    target = "no baseline recorded yet"
    if baseline:
        target = (
            f"{baseline['cells']} cells x {baseline['cycles']} cycles "
            f"= adp {baseline['adp']:.0f}"
        )

    return f"""\
# Task: {problem.name}

Write a synthesizable SystemVerilog module named `dut` implementing the
operator defined by `dut.py` in this directory.

## Deliverable

A single self-contained file `dut.v`. No `` `include ``, no other files.

## Specification

`dut.py` is the specification. Read it. `Model.forward()` is the golden
reference: your module must produce the same values, exactly.

## Parameters

| Name | Value |
|---|---|
{params}

## Interface

```verilog
{render_ports(problem)}
```

Each input port has its own valid/ready handshake and its own element count
per transaction:

| Input port | Elements per transaction |
|---|---|
{streams}

Elements move `LANES` at a time. One transfer happens on each rising clock
edge where `<port>_valid && <port>_ready`, and identically on the output side.
Assert a port's `ready` to accept a beat, lower it to apply backpressure.
Streams are independent: a short port finishes while a longer one continues.

Element `j` of a beat occupies bits `[j*DATA_W +: DATA_W]`, element 0 lowest.
Signed values are two's complement.

## Transactions

The testbench drives **{problem.transactions} back-to-back transactions
without an intervening reset**. Each transaction delivers the full element
counts on every port, and each produces `{problem.out_len}` output word(s).
A transaction ends when its last output word is accepted; clear any
per-transaction state before the next transaction's inputs arrive. A design
that only works once will fail.

## Numeric contract

| Signal | Contract |
|---|---|
{quant}

There is no tolerance. Comparison is bit-exact.

## Scoring

Synthesis gives a cell count (an area proxy). Simulation gives a cycle count,
summed over the back-to-back transactions, from the first accepted input to
the last accepted output.

    adp   = cells * cycles
    score = baseline_adp / adp

Correctness is a hard gate: a wrong design scores nothing regardless of speed.
Width alone cannot buy score - doubling the datapath doubles area to halve
cycles. Score comes from doing less work per result. Correctness is checked on
random seeds and on directed edge cases (zeros, extreme signed values), and on
a backpressure run where `ready` is withheld at seeded intervals.

**Target to beat:** {target}

## How to check your work

```sh
./check.sh
```

Runs synthesis and simulation and prints pass/fail, cells, cycles and score.
Uses a fixed set of dev seeds. Final scoring uses different seeds, so a design
that only works for the ones you can see will not pass.

## Rules

- Must synthesize with yosys. Simulator-only constructs (`initial`, `#delay`,
  `$readmemh`, `$display`, ...) are rejected by the audit.
- Must not reference the testbench or its own instance path.
- Must be a single file.
"""


def render_check_sh(problem: Problem, python: str) -> str:
    return f"""\
#!/bin/sh
# ADPBench feedback command. Dev seeds only.
cd "$(dirname "$0")" || exit 1
exec "{python}" -m adpbench check --problem "{problem.root}" --file dut.v
"""


def build_environment(
    problem: Problem, dest: Path, python: str | None = None, force: bool = False
) -> Path:
    """Create a self-contained task directory for an agent to work in."""
    dest = Path(dest).resolve()
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists (pass force=True to reuse)")
    dest.mkdir(parents=True, exist_ok=True)

    python = python or sys.executable
    baseline = problem.baseline_metrics
    baseline_data = json.loads(baseline.read_text()) if baseline.is_file() else None

    shutil.copy2(problem.root / "dut.py", dest / "dut.py")
    skeleton = render_skeleton(problem)
    (dest / "dut.v").write_text(skeleton)
    (dest / "PROBLEM.md").write_text(render_problem_md(problem, baseline_data))
    (dest / META_FILE).write_text(
        json.dumps({"skeleton_sha256": sha256_text(skeleton)}, indent=2) + "\n"
    )

    check = dest / "check.sh"
    check.write_text(render_check_sh(problem, python))
    check.chmod(0o755)

    (dest / ".history").mkdir(exist_ok=True)
    return dest


def snapshot(env_dir: Path, rtl_path: Path) -> Path | None:
    """Keep every version the agent tested, so the trajectory is recoverable."""
    if not rtl_path.is_file():
        return None
    history = Path(env_dir) / ".history"
    history.mkdir(exist_ok=True)
    n = len(list(history.glob("*_dut.v"))) + 1
    dest = history / f"{n:04d}_dut.v"
    shutil.copy2(rtl_path, dest)
    return dest


# --------------------------------------------------------------------------
# Run record
# --------------------------------------------------------------------------


@dataclass
class RunRecord:
    problem: str
    agent_cmd: str
    started: str = ""
    duration_s: float = 0.0
    exit_code: int = -1
    timed_out: bool = False
    audit: dict = field(default_factory=dict)
    result: dict | None = None
    error: str = ""
    history: list[str] = field(default_factory=list)

    def score(self) -> float:
        """Baseline-relative score. Zero unless the submission passed the gate."""
        if not self.result or not self.result.get("correct"):
            return 0.0
        ratio = float(self.result.get("ratio", -1.0))
        return ratio if ratio > 0 else 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def run_agent(
    problem: Problem,
    agent_cmd: str,
    dest: Path | None = None,
    timeout_s: int = 1800,
    python: str | None = None,
    force: bool = True,
) -> RunRecord:
    """Spawn an agent CLI inside a fresh environment, then audit and score it.

    The agent command runs through the shell with cwd set to the environment
    directory. It is given full access to that directory and nothing else is
    assumed about it.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(dest).resolve() if dest else repo_root() / "runs" / problem.name / stamp
    build_environment(problem, dest, python=python, force=force)

    record = RunRecord(
        problem=problem.name,
        agent_cmd=agent_cmd,
        started=datetime.now().isoformat(timespec="seconds"),
    )

    started = time.time()
    proc = subprocess.Popen(
        agent_cmd,
        shell=True,
        cwd=dest,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        record.exit_code = proc.returncode
        record.timed_out = False
        log = f"$ {agent_cmd}\n\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    except subprocess.TimeoutExpired:
        # The command runs through a shell, so killing the shell alone can
        # leave the agent running. Kill and reap the whole process group.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        record.exit_code = -1
        record.timed_out = True
        log = (
            f"$ {agent_cmd}\n\n--- TIMEOUT after {timeout_s}s"
            f" (process group killed) ---\n{stdout}\n{stderr}\n"
        )

    record.duration_s = round(time.time() - started, 2)
    (dest / "agent.log").write_text(log)

    history = sorted((dest / ".history").glob("*_dut.v"))
    record.history = [p.name for p in history]

    dut = dest / "dut.v"
    text = dut.read_text() if dut.is_file() else ""
    record.audit = audit_submission(text)

    meta = dest / META_FILE
    skeleton_hash = json.loads(meta.read_text()).get("skeleton_sha256") if meta.is_file() else None

    if not text:
        record.audit["violations"].append(
            {"line": 0, "match": "", "reason": "no dut.v produced"}
        )
        record.audit["ok"] = False
    elif skeleton_hash and sha256_text(text) == skeleton_hash:
        record.audit["violations"].append(
            {"line": 0, "match": "", "reason": "submission is unchanged from the starting skeleton"}
        )
        record.audit["ok"] = False

    if record.audit["ok"]:
        # Clean room: copy the submission alone and score against pristine
        # problem files on seeds the agent never saw.
        clean = dest / "clean"
        clean.mkdir(exist_ok=True)
        shutil.copy2(dut, clean / "dut.v")
        try:
            result = evaluate_multi(
                problem, [clean / "dut.v"], seeds=EVAL_SEEDS, source="agent", tag="eval"
            )
            record.result = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - the record must survive scorer bugs
            record.error = f"{type(exc).__name__}: {exc}"
            record.result = {"error": record.error}
    else:
        (dest / "dut.v").replace(dest / "rejected_dut.v")

    (dest / "record.json").write_text(record.to_json() + "\n")
    return record


__all__ = [
    "DEV_SEEDS",
    "EVAL_SEEDS",
    "RunRecord",
    "audit_submission",
    "build_environment",
    "render_ports",
    "render_problem_md",
    "run_agent",
    "snapshot",
]
