# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/deepseek-v4.1-flash [agent-assisted-v1]` | 2 | 2 | 100% | 2 | 100% | 4.62x | 0 | 0 |
| `opencode-go/glm-5.3-flash [agent-assisted-v1]` | 2 | 1 | 50% | 1 | 50% | 4.89x | 0 | 1 |
| `opencode-go/kimi-k2.6 [agent-assisted-v1]` | 2 | 1 | 50% | 1 | 50% | 1.83x | 0 | 1 |
| `opencode-go/mimo-v2.5 [agent-assisted-v1]` | 2 | 1 | 50% | 0 | 0% | 0.57x | 1 | 0 |
| `opencode-go/minimax-m2.7 [agent-assisted-v1]` | 2 | 0 | 0% | 0 | 0% | -1.00x | 2 | 0 |
| `opencode-go/qwen3.8-flash [agent-assisted-v1]` | 2 | 0 | 0% | 0 | 0% | -1.00x | 0 | 0 |
| **all** | 12 | 5 | 42% | 4 | 33% | 2.56x | 3 | 2 |

Agent-assisted outcomes (separate from execution health):

- correct: 5
- incorrect: 3
- transport_interrupted: 2
- turn_limit: 2

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
