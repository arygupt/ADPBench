# ADPBench pilot report: pilot-001

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode/ling-3.0-flash-fin-free` | 4 | 0 | 0% | 0 | 0% | -1.00x | 4 | 0 |
| `opencode/mimo-v2.5-free` | 4 | 2 | 50% | 2 | 50% | 3.27x | 2 | 0 |
| `opencode/muse-spark-1.3-contributor-free` | 4 | 2 | 50% | 2 | 50% | 2.77x | 2 | 0 |
| `opencode/nemotron-3-ultra-free` | 4 | 2 | 50% | 1 | 25% | 0.91x | 0 | 2 |
| `opencode/nemotron-3.5-lightning-free` | 4 | 0 | 0% | 0 | 0% | -1.00x | 2 | 2 |
| **all** | 20 | 6 | 30% | 5 | 25% | 2.02x | 10 | 4 |

Runs are frozen under this directory; `plan.json` lists what was planned.

Replay verification: all 6 successful submissions reproduced their recorded
scores. See [`REPLAY.md`](REPLAY.md) and [`replay.json`](replay.json).
