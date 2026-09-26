"""The task directory an agent works in, and the sandbox bundle it can run.

A task directory is agent-agnostic. It holds:

    PROBLEM.md   the task description, rendered from the problem spec
    dut.py       the executable specification (golden model)
    dut.v        a skeleton with the required module name and ports
    check.sh     dev-seed feedback: synthesis + simulation + score
    .history/    every tested dut.v and the check.sh output log

Any agent CLI with a shell can be dropped into it. Nothing here knows what a
specific agent is.

For sandboxed runs, a read-only copy of the harness (the "bundle") is mounted
into the container so check.sh works there. The bundle has the held-out
evaluation seeds removed and contains no reference solutions.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .hashing import sha256_text
from .problem import Problem, repo_root
from .seeds import DEV_SEEDS

# Records the skeleton's hash, so an unchanged skeleton can be rejected later.
META_FILE = ".adpbench.json"

# Container paths used by sandboxed runs.
SANDBOX_MOUNT = "/adpbench"  # read-only harness bundle
WORK_MOUNT = "/work"  # writable task directory

SKELETON_COMMENT = """\
// ADPBench submission. Replace the body; keep the module name and ports.
// Run ./check.sh for synthesis + simulation feedback.
// The testbench drives several back-to-back transactions without reset, and
// every input port has its own valid/ready handshake.
"""

# Written over adpbench/seeds.py in the sandbox bundle.
REDACTED_SEEDS_FILE = f'''\
"""Sandbox copy: the held-out evaluation seeds are not included."""

DEV_SEEDS = {DEV_SEEDS!r}
EVAL_SEEDS = ()
'''


# --------------------------------------------------------------------------
# Rendered files
# --------------------------------------------------------------------------


def render_ports(problem: Problem) -> str:
    """The `module dut #(...)(...);` header every submission must match."""
    lines = ["module dut #("]
    names = list(problem.params)
    for index, name in enumerate(names):
        comma = "," if index < len(names) - 1 else ""
        lines.append(f"    parameter {name} = {problem.params[name]}{comma}")
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
    params = "\n".join(f"| `{name}` | {value} |" for name, value in problem.params.items())
    quant = "\n".join(f"| `{name}` | {value} |" for name, value in problem.quant.items())
    streams = "\n".join(
        f"| `{port}` | {problem.input_lens[port]} |" for port in problem.input_ports
    )

    if baseline:
        target = (
            f"{baseline['cells']} cells x {baseline['cycles']} cycles "
            f"= adp {baseline['adp']:.0f}"
        )
    else:
        target = "no baseline recorded yet"

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


def render_check_sh(problem: Problem, python: str, sandbox: str = "none") -> str:
    """The dev-seed feedback script.

    Every invocation is appended to `.history/check.log` with a UTC timestamp
    and the exit status, so the whole feedback trajectory is recorded even
    when the terminal recording is unavailable.
    """
    if sandbox == "docker":
        relative_problem = problem.root.relative_to(repo_root())
        header = (
            "# ADPBench feedback command. Dev seeds only. Runs inside the sandbox, where\n"
            f"# the sanitized harness is mounted read-only at {SANDBOX_MOUNT} and scratch\n"
            "# work is written under the writable task mount.\n"
        )
        check_command = (
            f'ADPBENCH_WORKDIR="{WORK_MOUNT}/.adpbench" python -m adpbench check '
            f'--problem "{SANDBOX_MOUNT}/{relative_problem}" --file dut.v'
        )
    else:
        header = "# ADPBench feedback command. Dev seeds only.\n"
        check_command = f'"{python}" -m adpbench check --problem "{problem.root}" --file dut.v'

    return f"""\
#!/bin/sh
{header}cd "$(dirname "$0")" || exit 1
mkdir -p .history
{check_command} > .history/check_tmp.log 2>&1
status=$?
{{
  echo "=== check $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  cat .history/check_tmp.log
  echo "exit=$status"
}} >> .history/check.log
cat .history/check_tmp.log
exit $status
"""


# --------------------------------------------------------------------------
# Building directories
# --------------------------------------------------------------------------


def remove_path(path: Path) -> None:
    """Delete a file, symlink, or directory tree, never following symlinks."""
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def bundle_path(task_dir: Path) -> Path:
    """Where the read-only harness bundle for a task directory lives.

    It sits next to the task directory, not inside it: if it lived inside the
    writable mount, the agent could rewrite the read-only copy through the
    writable one.
    """
    task_dir = Path(task_dir)
    return task_dir.parent / f"{task_dir.name}_pkg"


def build_environment(
    problem: Problem,
    dest: Path,
    python: str | None = None,
    force: bool = False,
    sandbox: str = "none",
) -> Path:
    """Create a self-contained task directory for an agent to work in."""
    dest = Path(dest).resolve()
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists (pass force=True to reuse)")
    dest.mkdir(parents=True, exist_ok=True)

    baseline = None
    if problem.baseline_metrics.is_file():
        baseline = json.loads(problem.baseline_metrics.read_text())

    skeleton = render_skeleton(problem)
    shutil.copy2(problem.root / "dut.py", dest / "dut.py")
    (dest / "dut.v").write_text(skeleton)
    (dest / "PROBLEM.md").write_text(render_problem_md(problem, baseline))
    meta = {"skeleton_sha256": sha256_text(skeleton)}
    (dest / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")

    check = dest / "check.sh"
    check.write_text(render_check_sh(problem, python or sys.executable, sandbox=sandbox))
    check.chmod(0o755)

    (dest / ".history").mkdir(exist_ok=True)
    if sandbox == "docker":
        build_sandbox_bundle(problem, bundle_path(dest), force=True)
    return dest


def build_sandbox_bundle(problem: Problem, dest: Path, force: bool = False) -> Path:
    """Copy the harness so it can be mounted read-only inside an agent container.

    The copy contains the `adpbench` package, the synthesis flow, and this
    problem's `dut.py`, `baseline.v`, and a seed-free `baseline.json`. The
    held-out EVAL_SEEDS are replaced with an empty tuple. The container can run
    the dev-seed feedback loop but cannot read the held-out cases or any
    reference solution.
    """
    dest = Path(dest).resolve()
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists (pass force=True to reuse)")
        remove_path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    source = repo_root()
    skip_caches = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(source / "adpbench", dest / "adpbench", ignore=skip_caches)
    shutil.copytree(source / "flows", dest / "flows", ignore=skip_caches)
    (dest / "adpbench" / "seeds.py").write_text(REDACTED_SEEDS_FILE)

    problem_copy = dest / problem.root.relative_to(source)
    problem_copy.mkdir(parents=True, exist_ok=True)
    shutil.copy2(problem.root / "dut.py", problem_copy / "dut.py")
    shutil.copy2(problem.root / "baseline.v", problem_copy / "baseline.v")
    if problem.baseline_metrics.is_file():
        baseline = json.loads(problem.baseline_metrics.read_text())
        baseline.pop("seeds", None)
        baseline.pop("directed", None)
        (problem_copy / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    return dest


def snapshot(task_dir: Path, rtl_path: Path) -> Path | None:
    """Copy a tested dut.v into .history/, so the agent's trajectory is recoverable."""
    if not rtl_path.is_file():
        return None
    history = Path(task_dir) / ".history"
    history.mkdir(exist_ok=True)
    number = len(list(history.glob("*_dut.v"))) + 1
    dest = history / f"{number:04d}_dut.v"
    shutil.copy2(rtl_path, dest)
    return dest
