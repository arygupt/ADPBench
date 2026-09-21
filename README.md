# ADPBench

AI agents write synthesizable Verilog. ADPBench checks whether the result is
correct, synthesizes the exact design to gates, simulates it at gate level, and
scores the area–delay product: `cells × cycles`.

Given a numeric operator and an executable reference, can an agent produce
hardware that is both correct and cheap? ADPBench measures this with an
area–delay proxy. It does not claim physical area, timing closure, or power
signoff.

Live results: [`site/`](site/) · preview with `python3 -m http.server 8000 --directory site`

## Benchmarks

| bench | path | what | published view |
|---|---|---|---|
| dot product | `problems/level1/001_dot_product/` | lane-parallel vector reduction | leaderboard |
| GEMV | `problems/level1/002_gemv/` | matrix–vector multiply | leaderboard |
| matrix multiply | `problems/level1/003_matmul/` | tiled integer matmul | leaderboard |
| 1-D convolution | `problems/level1/004_conv1d/` | streaming convolution | leaderboard |

Each problem owns its executable spec, baseline, directed cases, and verified
solutions. The spec is `dut.py`, not a prose prompt:

- `Model.forward(...)` — NumPy golden reference
- `get_inputs()` — seeded input generation
- `INTERFACE` — module name, parameters, ports, stream lengths, transactions
- `QUANT` — exact integer widths and arithmetic contract
- `DIRECTED` — named edge cases such as zeros and extreme signed values

The submission is a Verilog module named `dut`. Input streams use independent
`valid`/`ready` handshakes; transactions run back-to-back without reset. A DUT
must hold output data stable while stalled and clear all transaction state
before the next transaction.

## Evaluation

```text
submission.v
  → audit         reject reward-hacking constructs and untouched skeletons
  → synthesize    pinned Yosys flow       → gate netlist + cell count
  → vectors       NumPy reference         → inputs + expected outputs
  → simulate      Icarus, gate level       → cycles + exact pass/fail
  → score         baseline_adp / adp       → published ratio
```

Every case runs twice: a score run with back-to-back inputs and ready output,
and a protocol run with seeded input gaps, output backpressure, and hold-stable
checks. A design must pass both. Synthesis parameters and simulation use the
same netlist, so the counted artifact is the checked artifact.

## Scoring and records

```text
adp   = cells × cycles
score = baseline_adp / submission_adp     # higher is better
```

The primary published number is beat-baseline rate: correct, faster-than-
baseline attempts divided by all planned attempts. Correctness rate, successful
geometric mean, wrong RTL, infrastructure failures, repetitions, hashes, tool
versions, and case manifests remain visible. `adpbench replay` re-scores a
frozen submission and verifies the recorded ratio.

The current pilot is a 20-run matrix across four operators and five free-tier
agent configurations. Results are committed under `pilot/results/` and rendered
into `site/data/` from frozen records.

## Local usage

Requires Python 3.10+, Yosys, and Icarus Verilog. The repository uses a local
virtualenv; install the package with `uv`:

```bash
uv venv
uv pip install -e .
.venv/bin/python -m adpbench list
.venv/bin/python -m adpbench baseline 001_dot_product
.venv/bin/python -m adpbench run 001_dot_product -f submission.v
```

Run an agent through the sandbox and record its trajectory:

```bash
adpbench agent 001_dot_product --label "my-model" \
  --cmd "opencode run -m opencode/mimo-v2.5-free 'Read PROBLEM.md and write dut.v.'" \
  --sandbox docker
```

Pilot and reporting commands:

```bash
adpbench pilot --config pilot/pilot_free_models.json --jobs 4
adpbench report --runs runs/
adpbench replay runs/<pilot>/<model>/<problem>/rep1
```

The agent-writable directory is not trusted. Submissions are copied
symlink-free, hashed, audited, and scored in a host-only directory. Docker is
recommended for untrusted agents; host mode executes the supplied command
directly.

## Layout

```text
adpbench/                 Python harness, scoring, agents, reports, site export
problems/level1/          executable operator problems and baselines
solutions/                reference implementations used for sanity checks
flows/                    pinned synthesis and simulation substrate
sandbox/                  Docker boundary and agent image definitions
pilot/                    configs plus published pilot results
site/                     zero-dependency static leaderboard
tests/                    pipeline, protocol, audit, report, and site tests
```

Methodology lives with the implementation in the problem specs and harness
modules. The published site is a static copy of frozen records; it never runs
the toolchain in the browser.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The integration tests exercise real synthesis and gate-level simulation when
Yosys and Icarus are available.
