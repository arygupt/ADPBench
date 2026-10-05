# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/deepseek-v4-pro [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 2.93x | 0 | 0 |
| `opencode-go/deepseek-v4.1-flash [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 5.26x | 0 | 0 |
| `opencode-go/glm-5.3 [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 5.19x | 1 | 0 |
| `opencode-go/glm-5.3-flash [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 4.74x | 0 | 1 |
| `opencode-go/gpt-6-luna [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 4.09x | 0 | 0 |
| `opencode-go/grok-4.7 [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 4.94x | 0 | 0 |
| `opencode-go/kimi-k2.6 [agent-assisted-v2]` | 4 | 3 | 75% | 2 | 50% | 2.22x | 0 | 1 |
| `opencode-go/kimi-k3 [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 4.46x | 0 | 0 |
| `opencode-go/mimo-v2.5 [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 3.87x | 0 | 0 |
| `opencode-go/mimo-v2.6-pro [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 4.79x | 0 | 1 |
| `opencode-go/minimax-m2.7 [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 4 | 0 |
| `opencode-go/minimax-m3 [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 2.67x | 1 | 0 |
| `opencode-go/qwen3.8-flash [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 3.80x | 0 | 0 |
| `opencode-go/qwen3.8-max [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 5.08x | 0 | 0 |
| **all** | 56 | 44 | 79% | 43 | 77% | 4.09x | 6 | 3 |

Agent-assisted outcomes (separate from execution health):

- correct: 44
- incorrect: 6
- provider_error: 1
- transport_interrupted: 2
- truncated: 2
- turn_limit: 1

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
