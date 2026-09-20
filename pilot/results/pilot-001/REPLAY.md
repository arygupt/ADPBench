# ADPBench pilot-001 replay verification

All six successful submissions from `pilot-001` were replayed on 2026-09-20
with the current harness. Every replay matched its frozen record for
correctness, synthesized cell count, cycle count, and ADP ratio.

| evaluated system | problem | cells | cycles | ADP ratio | result |
|---|---|---:|---:|---:|---|
| `opencode/mimo-v2.5-free` | `001_dot_product` | 19,193 | 18 | 3.3853x | MATCH |
| `opencode/mimo-v2.5-free` | `002_gemv` | 13,725 | 168 | 3.1667x | MATCH |
| `opencode/muse-spark-1.3-contributor-free` | `001_dot_product` | 19,272 | 18 | 3.3714x | MATCH |
| `opencode/muse-spark-1.3-contributor-free` | `003_matmul` | 14,975 | 402 | 2.2789x | MATCH |
| `opencode/nemotron-3-ultra-free` | `001_dot_product` | 7,910 | 530 | 0.2790x | MATCH |
| `opencode/nemotron-3-ultra-free` | `002_gemv` | 14,490 | 170 | 2.9642x | MATCH |

Verification used harness commit `587d801969d109016432d17ae05d142a3b5f52d2`,
Yosys 0.69+post (`143eb14f9cc55d6f8927e68523b0c9d2166ed02c`), and
Icarus Verilog 13.0. The original runs used harness commit
`48004a3950baeeb79be31934a35bf89df6be1575` with the same synthesis and
simulation tool versions.

The machine-readable results, including submission hashes and full-precision
ratios, are in [`replay.json`](replay.json).
