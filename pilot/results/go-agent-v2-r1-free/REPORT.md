# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/longcat-2.5-preview-free [agent-assisted-v2]` | 4 | 1 | 25% | 1 | 25% | 5.22x | 2 | 1 |
| `opencode-go/space-bunny-free [agent-assisted-v2]` | 4 | 2 | 50% | 2 | 50% | 4.37x | 1 | 0 |
| **all** | 8 | 3 | 38% | 3 | 38% | 4.64x | 3 | 1 |

Agent-assisted outcomes (separate from execution health):

- correct: 3
- incorrect: 3
- transport_interrupted: 1
- wall_timeout: 1

Rates use all scheduled slots. Unscored submissions and interrupted jobs are not claims of incorrect arithmetic. Development checks are not held-out benchmark passes.
