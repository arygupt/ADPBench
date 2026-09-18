# Running agents in the sandbox

`adpbench agent` and `adpbench pilot` can run an agent CLI inside Docker:

```bash
docker build -t adpbench-agent sandbox/
adpbench agent 001_dot_product --sandbox docker --image adpbench-agent-opencode \
  --label "opencode/mimo-v2.5-free" \
  --cmd "opencode run --model opencode/mimo-v2.5-free 'Read PROBLEM.md and write dut.v.'"
```

What the boundary does:

- The task directory is mounted read-write at `/work`. It contains
  `PROBLEM.md`, `dut.py`, the skeleton, and `check.sh`.
- A per-run harness bundle is mounted read-only at `/adpbench`. It contains
  the `adpbench` package, `flows/`, and the current problem, with
  `EVAL_SEEDS` redacted to `()`. The container can run `./check.sh` on dev
  seeds but cannot read the held-out scoring cases.
- The network is on by default (the model API needs it). Use
  `--network none` for a fully offline run.
- `--memory`, `--cpus`, and `--pids-limit` are bounded.
- Final scoring happens on the host, outside the container, from a frozen
  copy of the submission. The run's `manifest.json` records the submission
  hash, netlist hash, seeds, tool versions, and the git commit.

The base image ships the toolchain only. Add your agent CLI in a derived
image (see the comment at the top of `Dockerfile`). For a smoke test without
an agent, any shell command works:

```bash
adpbench agent 001_dot_product --sandbox docker --image adpbench-agent \
  --cmd "cp /adpbench/problems/level1/001_dot_product/baseline.v dut.v"
```
