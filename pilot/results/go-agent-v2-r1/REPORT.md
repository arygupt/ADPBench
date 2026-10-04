# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/deepseek-v4-pro [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 2.54x | 0 | 1 |
| `opencode-go/deepseek-v4.1-flash [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 4.10x | 0 | 2 |
| `opencode-go/glm-5.3 [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 4.93x | 1 | 1 |
| `opencode-go/glm-5.3-flash [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 3.88x | 0 | 2 |
| `opencode-go/gpt-6-luna [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 4.04x | 0 | 1 |
| `opencode-go/grok-4.7 [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 5.03x | 0 | 1 |
| `opencode-go/kimi-k2.6 [agent-assisted-v2]` | 4 | 1 | 25% | 0 | 0% | 0.99x | 0 | 3 |
| `opencode-go/kimi-k3 [agent-assisted-v2]` | 4 | 3 | 75% | 3 | 75% | 4.34x | 0 | 1 |
| `opencode-go/mimo-v2.5 [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 2.51x | 0 | 2 |
| `opencode-go/mimo-v2.6-pro [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 4.66x | 0 | 3 |
| `opencode-go/minimax-m2.7 [agent-assisted-v2]` | 4 | 0 | 0% | 0 | 0% | -1.00x | 2 | 2 |
| `opencode-go/minimax-m3 [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 2.67x | 0 | 1 |
| `opencode-go/qwen3.8-flash [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 3.40x | 0 | 2 |
| `opencode-go/qwen3.8-max [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 4.48x | 0 | 2 |
| **all** | 56 | 28 | 50% | 27 | 48% | 3.58x | 3 | 24 |

Agent-assisted outcomes (separate from execution health):

- correct: 28
- incorrect: 3
- provider_error: 1
- quota_exhausted: 22
- transport_interrupted: 1
- truncated: 1

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
