# ADPBench

A KernelBench-shaped benchmark for ASIC/RTL generation, scored on silicon cost.

The name is the metric: **A**rea **D**elay **P**roduct, `cells x cycles` — the
number a design is actually graded on. We do not claim power or timing
signoff, because we do not measure them.

KernelBench asks: given a PyTorch operator, can a model write a fast CUDA
kernel? ADPBench asks the same question one level down: given a numeric
operator, can a model write synthesizable Verilog that is *cheap* in gates and
*fast* in cycles?

## How a problem is defined

There is no English spec. `dut.py` is the specification:

- `Model.forward(...)` — a numpy golden reference
- `get_inputs()` — seeded random input generation
- `INTERFACE` — module name, parameters, port list, packing order
- `QUANT` — the numeric contract (fixed point, accumulator width, no tolerance)

Submissions are Verilog modules named `dut` implementing that interface.

## The contract

Every problem uses the same streaming handshake. Inputs move together on one
valid/ready pair, `LEN` elements are delivered `LANES` at a time, and the DUT
may apply backpressure by lowering `in_ready`.

```verilog
module dut #(
  parameter LEN, LANES, DATA_W, ACC_W
)(
  input  wire clk, rst_n,
  input  wire in_valid,
  output wire in_ready,
  input  wire [LANES*DATA_W-1:0] in_a_flat,   // per problem
  input  wire [LANES*DATA_W-1:0] in_b_flat,
  output wire out_valid,
  input  wire out_ready,
  output wire [ACC_W-1:0] out_c
);
```

Element `j` of a beat occupies bits `[j*DATA_W +: DATA_W]`, element 0 lowest.

## Pipeline

```
submission.v
  -> synthesize   yosys, pinned flow        -> netlist + cells   (area proxy)
  -> vectors      numpy golden model        -> inputs + expected outputs
  -> simulate     icarus, gate-level        -> cycles, bit-exact pass/fail
  -> score        baseline_adp / adp
```

Synthesis applies the problem's parameters with `chparam` before elaboration
and writes the resulting netlist. Simulation runs that netlist - not the RTL -
so the cells that are counted and the behaviour that is checked describe one
artifact. Comparison is exact integer equality; there is no floating point
tolerance, because the problem fixes the arithmetic completely.

Each seed is simulated twice:

- a **score** run with inputs back-to-back and `out_ready` held high, which is
  the timing measurement, and
- a **protocol** run with seeded input gaps, seeded output backpressure, and a
  hold-stable check that fails a DUT which drops or changes an output while
  stalled.

A design must pass both.

## Scoring

With the clock pinned, time is proportional to cycles, so

```
adp   = cells * cycles
score = baseline_adp / submission_adp      // higher is better
```

A design cannot buy score by simply widening its datapath: duplicating hardware
doubles cells to halve cycles. Score comes from doing less work per result —
exploiting structure, narrowing datapaths, reusing hardware across beats.

Aggregate across a problem set with `score.geometric_mean_ratio_correct_only`
and `score.fast_p`, the same shape as KernelBench's aggregation.

## Usage

```bash
uv venv && uv pip install -e .
.venv/bin/python -m adpbench list
.venv/bin/python -m adpbench baseline 001_dot_product        # freeze the denominator
.venv/bin/python -m adpbench run 001_dot_product -f submission.v
```

Requires `yosys` and `iverilog` on PATH.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The integration tests score the counterexamples from a scorer review through
the real pipeline (see `tests/fixtures/README.md`), so they need `yosys` and
`iverilog`.

## The agent layer

The harness above judges a finished file. The agent layer gives a model a loop
to produce one, and records what it did.

```bash
# Run any agent CLI inside a generated task directory, then audit and score it
adpbench agent 001_dot_product --cmd "claude -p 'Read PROBLEM.md and complete the task.'"
adpbench agent 001_dot_product --cmd "codex exec 'Read PROBLEM.md and complete the task.'"
```

A run does four things:

**Build an environment.** A fresh directory containing:

| File | Role |
|---|---|
| `PROBLEM.md` | the task, the exact interface, the numeric contract, the rules |
| `dut.py` | the golden model, so the agent knows the required semantics |
| `dut.v` | the skeleton to fill in |
| `check.sh` | the feedback loop: synthesis + simulation + score, dev seeds only |

The agent is expected to run `./check.sh` repeatedly. It is agent-agnostic by
construction — anything with a shell can be dropped in.

**Record the trajectory.** `check.sh` snapshots `dut.v` on every run into
`.history/`, so the full sequence of attempts survives even though the harness
does not control the agent's internal loop.

**Audit.** The submission is scanned for constructs that behave differently
under simulation than under synthesis, or that let it read the expected
answers: `initial`, `#delay`, `$readmemh`, `$display`, `force`, `` `include ``,
testbench references. An untouched skeleton is also rejected.

**Score from a fresh copy.** The submission is copied *alone* into a fresh
directory and scored against pristine problem files on `EVAL_SEEDS`, which are
not in the environment. Nothing in the environment directory is trusted.

This is not yet an enforced security boundary: the agent runs with the same
user's shell, so it can read the repository and the grader. Until runs happen
inside a container or VM, held-out seeds protect against accidental
overfitting, not against a determined agent.

A run record lands in `runs/<problem>/<timestamp>/`:

```
record.json     duration, exit code, audit result, per-seed scores
agent.log       raw agent stdout/stderr
dut.v           final submission
.history/       every version the agent tested
clean/          the clean-room copy that was actually scored
```

Correctness is a hard gate, so a run scores nothing unless it passes on every
held-out seed. Hardcoding the visible dev answers buys nothing.

> Running an agent with shell access executes arbitrary code. Run it in a
> container if the model is untrusted.

## The pinned substrate

KernelBench is meaningful because everyone runs on the same GPU. The equivalent
here is a frozen toolchain: `flows/synth.ys`, the yosys version, the cell
counting convention, and the simulation models for the synthesized cells.
Changing any of them invalidates every stored baseline and requires re-running
`adpbench baseline` for every problem.

The flow applies `chparam` for every parameter in `INTERFACE["params"]` before
`hierarchy`, synthesizes to gates, and writes `netlist.v`. That netlist is
simulated against the `simlib.v` shipped with the same yosys release, so the
simulation models are pinned alongside the synthesizer. `ADPBENCH_SIMLIB`
overrides discovery of that file.

`flows/synth.ys` runs `synth -noabc`. ABC's generic-gate optimisation is
superlinear on wide arithmetic (a 32-multiplier datapath exceeded 180s), which
is unusable in a fast eval tier. Cell counts are therefore post-techmap and
pre-optimisation: a deterministic, comparable proxy for area, not an absolute
gate count. A slow tier using ABC or OpenROAD is future work.

## Status

v0. One problem (`level1/001_dot_product`), zero-dependency Python harness.

```
baseline.v                   2126 cells    264 cycles    561264 adp    1.00x
solutions/parallel.v        18598 cells      9 cycles    167382 adp    3.35x
```

Known gaps:

- Only one problem, and vector I/O is one-dimensional per port; matrix
  problems need an explicit layout and transaction-framing contract.
- Repeated transactions are outside the contract: the testbench drives one
  transaction, and `solutions/parallel.v` still fails a second one without
  reset (`tests/fixtures/two_transactions.v`).
- Failure attribution for combinational loops is a simulation timeout rather
  than a synthesis-time diagnosis.
- No timing feasibility check. A 100 MHz testbench clock does not prove the
  netlist closes at 100 MHz, and `cells * cycles` counts generic cells before
  ABC, so it is a proxy, not physical area or silicon performance.
- No directed arithmetic cases yet (zeros, extreme signed values); coverage is
  seeded random vectors plus the protocol runs.
- Aggregation is correctness-gated geometric mean only. Published rankings
  must also report correctness rate and the fraction of attempts that beat the
  baseline, and separate infrastructure failures from wrong RTL.
- Agent runs share the host user; final scoring is not an enforced boundary
  until runs are containerised.
- No prompt harness or leaderboard yet. The harness scores Verilog; producing
  that Verilog is out of scope so far.

## Related work

The module-level generation benchmarks: VerilogEval, RTLLM, CVDP, ChipBench,
ChipVerilog, HWE-Bench.

The synthesis-scored ones ADPBench sits beside: **HQI** (Synthesis-in-the-Loop
Evaluation, GLSVLSI '26) scores post-synthesis area and delay against expert
references on VerilogEval/RTLLM tasks, and **HINT** optimises area-delay product
directly.

The gap ADPBench aims at: those tasks are module-shaped ("write a FIFO") with
fixed testbenches. ADPBench tasks are operator-shaped, with a runnable numeric
reference, randomised multi-trial correctness, and a frozen quantization
contract — KernelBench's structure, scored on silicon.
