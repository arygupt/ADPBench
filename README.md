# ADPBench

AI agents write Verilog. ADPBench checks it's correct, synthesizes it to gates,
simulates it at gate level, and scores the area–delay product (`cells × cycles`).
It's a proxy, not physical area, timing, or power.

Results: [adpbench.vercel.app](https://adpbench.vercel.app)

## Problems

| problem | path |
|---|---|
| dot product | `problems/level1/001_dot_product/` |
| GEMV | `problems/level1/002_gemv/` |
| matrix multiply | `problems/level1/003_matmul/` |
| 1-D convolution | `problems/level1/004_conv1d/` |
| row-wise argmax | `problems/level1/005_argmax/` |
| top-K selection | `problems/level1/006_topk/` |
| 16-bin histogram | `problems/level1/007_histogram/` |
| 2-D max pooling | `problems/level1/008_maxpool2d/` |

Each problem's spec is `dut.py`: a NumPy reference (`Model.forward`), input
generator, `INTERFACE`, `QUANT` widths, and `DIRECTED` edge cases. Submissions
are a Verilog module `dut` with `valid`/`ready` streams.

Argmax returns the first maximum's column index for each of eight 64-element
rows. Top-K returns the largest eight of 64 signed values in descending order,
retaining duplicates. Histogram counts 256 bin IDs in `[0, 15]`, including
same-beat collisions. Max pooling uses 3×3 windows, stride one, and no padding
on a 16×16 signed image, producing a 14×14 result.

Each new task includes `baseline.v`, measured `baseline.json`, and an optimized
`solutions/parallel.v` reference within its problem directory. The catalog has
eight tasks; existing published rounds retain their original problem sets.

## Scoring

```text
audit → synthesize (Yosys) → simulate gate-level (Icarus) → score
adp   = cells × cycles
score = baseline_adp / submission_adp     # higher is better
```

Every case runs with and without input gaps and output backpressure; both must
pass. The headline number is beat-baseline rate. `adpbench replay` re-scores a
frozen submission.

## Usage

Requires Python 3.10+, Yosys, and Icarus Verilog.

```bash
uv venv && uv pip install -e .
adpbench list
adpbench run 001_dot_product -f submission.v

adpbench agent 001_dot_product --label my-model --sandbox docker \
  --cmd "opencode run -m opencode/mimo-v2.5-free 'Read PROBLEM.md and write dut.v.'"

adpbench pilot --config pilot/pilot_free_models.json --jobs 4
adpbench report --runs runs/
```

Use `--sandbox docker` for untrusted agents.

## Layout

```text
adpbench/        harness, scoring, agents, reports
problems/        problem specs and baselines
solutions/       reference implementations
flows/           pinned synthesis and simulation
sandbox/         Docker agent images
pilot/           configs and published results
site/            static leaderboard
scripts/         OpenCode Go tracks
tests/           python -m unittest discover -s tests
```
