# ADPBench pilot report

| evaluated system | attempts | correct | correctness | beat baseline | beat rate | geomean (successful) | wrong RTL | infra |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `opencode-go/deepseek-v4.1-flash [single-shot]` | 2 | 2 | 100% | 2 | 100% | 3.88x | 0 | 0 |
| `opencode-go/glm-5.3-flash [single-shot]` | 2 | 1 | 50% | 1 | 50% | 2.56x | 1 | 0 |
| `opencode-go/kimi-k2.6 [single-shot]` | 2 | 0 | 0% | 0 | 0% | -1.00x | 1 | 1 |
| `opencode-go/mimo-v2.5 [single-shot]` | 2 | 0 | 0% | 0 | 0% | -1.00x | 2 | 0 |
| `opencode-go/minimax-m2.7 [single-shot]` | 2 | 0 | 0% | 0 | 0% | -1.00x | 2 | 0 |
| `opencode-go/qwen3.8-flash [single-shot]` | 2 | 0 | 0% | 0 | 0% | -1.00x | 1 | 1 |
| **all** | 12 | 3 | 25% | 3 | 25% | 3.38x | 7 | 2 |
