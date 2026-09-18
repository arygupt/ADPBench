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
- `INTERFACE` — module name, parameters, ports, per-port lengths, transactions
- `QUANT` — the numeric contract (fixed point, accumulator width, no tolerance)
- `DIRECTED` — named edge-case generators (zeros, extreme signed values)

Submissions are Verilog modules named `dut` implementing that interface.

## The contract

Every problem uses the same streaming handshake. Each input port has its own
`valid`/`ready` pair and its own element count per transaction; streams of
different lengths run independently. The DUT may apply backpressure by
lowering any `ready`.

```verilog
module dut #(
  parameter ...
)(
  input  wire clk, rst_n,
  input  wire [LANES*DATA_W-1:0] in_a_flat,   // per problem
  input  wire                    in_a_flat_valid,
  output wire                    in_a_flat_ready,
  ...                                          // one triple per input port
  output wire                    out_valid,
  input  wire                    out_ready,
  output wire [ACC_W-1:0]        out_c
);
```

Element `j` of a beat occupies bits `[j*DATA_W +: DATA_W]`, element 0 lowest.
Arrays are row-major. The testbench drives **back-to-back transactions without
an intervening reset**: a transaction ends when its last output word is
accepted, and any per-transaction state must be cleared before the next one.

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

Every case (a held-out random seed or a directed pattern) is simulated twice:

- a **score** run with inputs back-to-back and `out_ready` held high, which is
  the timing measurement, and
- a **protocol** run with seeded input gaps, seeded output backpressure, and a
  hold-stable check that fails a DUT which drops or changes an output while
  stalled.

A design must pass both, on every case.

## Scoring

With the clock pinned, time is proportional to cycles, so

```
adp   = cells * cycles
score = baseline_adp / submission_adp      // higher is better
```

A design cannot buy score by simply widening its datapath: duplicating hardware
doubles cells to halve cycles. Score comes from doing less work per result —
exploiting structure, narrowing datapaths, reusing hardware across beats.

`score.py` has the correctness-gated geometric mean, and `report.py` builds the
published view: correctness rate, the fraction of planned attempts that
correctly beat the baseline (the primary number), improvement among successful
attempts (secondary), and a split between wrong RTL and infrastructure
failures. Attempts and repetitions are disclosed, never averaged away.

## Usage

```bash
uv venv && uv pip install -e .
.venv/bin/python -m adpbench list
.venv/bin/python -m adpbench baseline 001_dot_product        # freeze the denominator
.venv/bin/python -m adpbench run 001_dot_product -f submission.v
.venv/bin/python -m adpbench seeds --out seeds.json          # publish the case manifest
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
to produce one, runs it behind an optional container boundary, and records what
it did.

```bash
adpbench agent 001_dot_product --label "my-model" \
  --cmd "opencode run -m opencode/mimo-v2.5-free 'Read PROBLEM.md and write dut.v.'"
```

A run does four things:

**Build an environment.** A fresh directory containing `PROBLEM.md` (the task,
interface, transactions, numeric contract, rules), `dut.py` (the golden model),
`dut.v` (the skeleton), and `check.sh` (the dev-seed feedback loop). The agent
is expected to run `./check.sh` repeatedly. It is agent-agnostic by
construction — anything with a shell can be dropped in.

**Run the agent, optionally in Docker.** `--sandbox docker` mounts the task
directory read-write at `/work` and a per-run harness bundle read-only at
`/adpbench`, with the held-out seeds redacted. See `sandbox/README.md`; the
shipped image pins the same yosys commit and iverilog release as the host, so
dev feedback and final scoring agree on cell counts.

**Audit.** The submission is scanned for constructs that behave differently
under simulation than under synthesis, or that let it read the expected
answers: `initial`, `#delay`, `$readmemh`, `$display`, `force`, `` `include ``,
testbench references. An untouched skeleton is also rejected. Comments and
string literals are masked first, so naming a construct in a comment is fine.

**Freeze and score.** Nothing in the agent-writable task directory is trusted:
the submission is read symlink-free and copied into a host-only directory next
to the run, hashed, and scored there on the held-out cases.

A run record lands in `runs/<problem>/<timestamp>/`:

```
record.json          duration, exit code, audit, per-case scores, label, attempt
manifest.json        hashes, seeds, tool versions, git commit, sandbox config
agent.log            raw agent stdout/stderr
dut.v                final submission (agent-written)
.history/            every version the agent tested
../<run>_frozen/     the frozen copy that was actually scored
../<run>_pkg/        read-only harness bundle mounted in the container
```

Re-score a frozen run to check the number reproduces; replay compares the
manifest's submission hash and the ratio, not just cells and cycles:

```bash
adpbench replay runs/001_dot_product/<timestamp>      # prints MATCH or MISMATCH
adpbench report --runs runs/                          # scoreboard by label
```

## The pilot

The pilot is a model x problem x repetition matrix under one budget:

```json
{
  "name": "pilot-001",
  "problems": ["001_dot_product", "002_gemv", "003_matmul", "004_conv1d"],
  "repetitions": 1,
  "sandbox": {"mode": "docker", "image": "adpbench-agent", "network": "bridge"},
  "agents": [
    {"label": "opencode/mimo-v2.5-free", "cmd": "opencode run -m opencode/mimo-v2.5-free 'Read PROBLEM.md and write dut.v.'", "timeout_s": 1200}
  ]
}
```

```bash
adpbench pilot --config pilot.json
```

`plan.json`, every run record, `report.json`, and `REPORT.md` are written under
`runs/pilot_<timestamp>_<name>/`. The report is built from the frozen records,
so publishing is a copy, not a re-computation.

> Running an agent with shell access executes arbitrary code. Use
> `--sandbox docker` for untrusted models; host mode trusts the command.

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

v0.2. Four problems, zero-dependency Python harness, pilot harness ready.

```
problem                      baseline                       sanity solution
001_dot_product              2127 cells    528 cycles 1.00x   3.09x (lane-parallel)
002_gemv                     3295 cells   2214 cycles 1.00x   none yet
003_matmul                   6253 cells   2192 cycles 1.00x   none yet
004_conv1d                   4565 cells   5016 cycles 1.00x   none yet
```

The sanity solution is `problems/level1/001_dot_product/solutions/parallel.v`.
The first real agent run (`opencode/mimo-v2.5-free`, host mode) scored 1.16x
with a leaner serial design; it replays exactly.

Known gaps:

- Only one sanity solution so far; the other problems have no proof that a
  better design is reachable, only that the baseline is beatable in principle.
- Failure attribution for combinational loops is a simulation timeout rather
  than a synthesis-time diagnosis.
- No timing feasibility check. A 100 MHz testbench clock does not prove the
  netlist closes at 100 MHz, and `cells * cycles` counts generic cells before
  ABC, so it is a proxy, not physical area or silicon performance.
- No published pilot yet: the 20-run matrix has not been executed at scale.
- Per-port data widths are shared (`DATA_W`); mixed-width ports need an
  explicit map.

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
