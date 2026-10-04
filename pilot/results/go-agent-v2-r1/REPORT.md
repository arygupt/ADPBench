# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/deepseek-v4-pro [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 3.16x | 0 | 2 |
| `opencode-go/deepseek-v4.1-flash [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 3.42x | 0 | 3 |
| `opencode-go/glm-5.3 [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 0 | 4 |
| `opencode-go/glm-5.3-flash [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 3.21x | 0 | 3 |
| `opencode-go/gpt-6-luna [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 3.02x | 0 | 2 |
| `opencode-go/grok-4.7 [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 3.78x | 0 | 3 |
| `opencode-go/kimi-k2.6 [agent-assisted-v2]` | 4 | 1 | 25% | 0 | 0% | 0.99x | 0 | 3 |
| `opencode-go/kimi-k3 [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 3.80x | 0 | 3 |
| `opencode-go/mimo-v2.5 [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 1.74x | 0 | 3 |
| `opencode-go/mimo-v2.6-pro [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 0 | 4 |
| `opencode-go/minimax-m2.7 [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 1 | 3 |
| `opencode-go/minimax-m3 [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 2.28x | 0 | 3 |
| `opencode-go/qwen3.8-flash [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 3.77x | 0 | 3 |
| `opencode-go/qwen3.8-max [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 0 | 4 |
| **all** | 56 | 12 | 21% | 11 | 20% | 2.78x | 1 | 43 |

Agent-assisted outcomes (separate from execution health):

- correct: 12
- incorrect: 1
- quota_exhausted: 43

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
