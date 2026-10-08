# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/gpt-6-luna [agent-assisted-v2]` | 1 | 1 | 100% | 1 | 100% | 1.60x | 0 | 0 |
| `opencode-go/kimi-k3 [agent-assisted-v2]` | 1 | 1 | 100% | 1 | 100% | 3.44x | 0 | 0 |
| **all** | 2 | 2 | 100% | 2 | 100% | 2.35x | 0 | 0 |

Agent-assisted outcomes (separate from execution health):

- correct: 2

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
