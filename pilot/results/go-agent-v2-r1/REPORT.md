# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/deepseek-v4-pro [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 2.93x | 0 | 0 |
| `opencode-go/deepseek-v4.1-flash [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 5.17x | 0 | 1 |
| `opencode-go/glm-5.3 [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 4.93x | 1 | 1 |
| `opencode-go/glm-5.3-flash [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 4.74x | 0 | 1 |
| `opencode-go/gpt-6-luna [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 4.09x | 0 | 0 |
| `opencode-go/grok-4.7 [agent-assisted-v2]` | 4 | 4 | 100% | 4 | 100% | 4.94x | 0 | 0 |
| `opencode-go/kimi-k2.6 [agent-assisted-v2]` | 4 | 2 | 50% | 1 | 25% | 1.69x | 0 | 2 |
| `opencode-go/kimi-k3 [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 4.34x | 0 | 1 |
| `opencode-go/mimo-v2.5 [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 3.78x | 0 | 1 |
| `opencode-go/mimo-v2.6-pro [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 4.66x | 0 | 2 |
| `opencode-go/minimax-m2.7 [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 3 | 1 |
| `opencode-go/minimax-m3 [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 2.67x | 0 | 1 |
| `opencode-go/qwen3.8-flash [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 3.80x | 0 | 1 |
| `opencode-go/qwen3.8-max [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 5.33x | 0 | 1 |
| **all** | 56 | 37 | 66% | 36 | 64% | 3.98x | 4 | 13 |

Agent-assisted outcomes (separate from execution health):

- correct: 37
- incorrect: 4
- provider_error: 1
- quota_exhausted: 11
- transport_interrupted: 1
- truncated: 2

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
