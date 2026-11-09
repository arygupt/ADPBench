# Timing-aware rescore

`timed_adp = area × cycles × (depth + 3)`. Area is Nangate45 X1 area (µm²); depth is the longest
register-to-register path in gate levels. Correct, scored designs only; ratios are
against the same problem's baseline measured the same way. Experimental, not the headline score.

Geomeans: `cells × cycles` is today's score, `+ depth` multiplies in the clock period
only, and `timed` also swaps cell count for area.

## Baselines

| problem | cells | cycles | area µm² | depth |
|---|---:|---:|---:|---:|
| 001_dot_product | 2142 | 546 | 5879 | 37 |
| 002_gemv | 3295 | 2216 | 8956 | 38 |
| 003_matmul | 6253 | 2194 | 20894 | 39 |
| 004_conv1d | 4565 | 5018 | 13607 | 42 |

## go-agent-20260923

| model | correct | beat baseline | timed beat | cells × cycles | + depth | timed | rank | timed rank |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| opencode-go/glm-5.3-flash [agent-assisted-v1] | 1 | 1 | 1 | 4.89× | 3.65× | 6.37× | 1 | 1 |
| opencode-go/deepseek-v4.1-flash [agent-assisted-v1] | 2 | 2 | 2 | 4.62× | 2.92× | 5.56× | 2 | 2 |
| opencode-go/kimi-k2.6 [agent-assisted-v1] | 1 | 1 | 1 | 1.83× | 1.11× | 2.14× | 3 | 3 |
| opencode-go/mimo-v2.5 [agent-assisted-v1] | 1 | 0 | 0 | 0.57× | 0.31× | 0.36× | 4 | 4 |

## go-agent-v2-r1

| model | correct | beat baseline | timed beat | cells × cycles | + depth | timed | rank | timed rank |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| opencode-go/deepseek-v4.1-flash [agent-assisted-v2] | 4 | 4 | 4 | 5.26× | 3.96× | 6.16× | 1 | 2 |
| opencode-go/glm-5.3 [agent-assisted-v2] | 3 | 3 | 3 | 5.19× | 4.21× | 6.55× | 2 | 1 |
| opencode-go/qwen3.8-max [agent-assisted-v2] | 4 | 4 | 4 | 5.08× | 3.61× | 5.71× | 3 | 4 |
| opencode-go/grok-4.7 [agent-assisted-v2] | 4 | 4 | 4 | 4.94× | 3.46× | 5.61× | 4 | 6 |
| opencode-go/mimo-v2.6-pro [agent-assisted-v2] | 2 | 2 | 2 | 4.79× | 3.75× | 4.96× | 5 | 8 |
| opencode-go/glm-5.3-flash [agent-assisted-v2] | 3 | 3 | 3 | 4.74× | 3.34× | 5.63× | 6 | 5 |
| opencode-go/kimi-k3 [agent-assisted-v2] | 4 | 4 | 4 | 4.46× | 3.49× | 5.88× | 7 | 3 |
| opencode-go/gpt-6-luna [agent-assisted-v2] | 4 | 4 | 4 | 4.09× | 3.27× | 4.82× | 8 | 10 |
| opencode-go/mimo-v2.5 [agent-assisted-v2] | 4 | 4 | 4 | 3.87× | 3.08× | 5.05× | 9 | 7 |
| opencode-go/qwen3.8-flash [agent-assisted-v2] | 3 | 3 | 3 | 3.80× | 2.97× | 4.84× | 10 | 9 |
| opencode-go/deepseek-v4-pro [agent-assisted-v2] | 4 | 4 | 4 | 2.93× | 1.90× | 2.85× | 11 | 11 |
| opencode-go/minimax-m3 [agent-assisted-v2] | 2 | 2 | 2 | 2.67× | 1.76× | 2.74× | 12 | 12 |
| opencode-go/kimi-k2.6 [agent-assisted-v2] | 3 | 2 | 2 | 2.22× | 2.21× | 2.61× | 13 | 13 |

## go-agent-v2-r1-free

| model | correct | beat baseline | timed beat | cells × cycles | + depth | timed | rank | timed rank |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| opencode-go/longcat-2.5-preview-free [agent-assisted-v2] | 1 | 1 | 1 | 5.22× | 3.06× | 5.29× | 1 | 1 |
| opencode-go/space-bunny-free [agent-assisted-v2] | 2 | 2 | 2 | 4.37× | 3.42× | 4.91× | 2 | 2 |

## go-agent-v2-r2

| model | correct | beat baseline | timed beat | cells × cycles | + depth | timed | rank | timed rank |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| opencode-go/kimi-k3 [agent-assisted-v2] | 1 | 1 | 1 | 3.44× | 2.06× | 3.95× | 1 | 1 |
| opencode-go/gpt-6-luna [agent-assisted-v2] | 1 | 1 | 1 | 1.60× | 1.16× | 1.96× | 2 | 2 |

## Largest drops

Designs whose ratio falls most once area and clock period count.

| dataset | model | problem | cycles | depth | ratio | + depth | timed ratio |
|---|---|---|---:|---:|---:|---:|---:|
| go-agent-20260923 | opencode-go/mimo-v2.5 [agent-assisted-v1] | 002_gemv | 290 | 74 | 0.57× | 0.31× | 0.36× |
| go-agent-v2-r1 | opencode-go/deepseek-v4-pro [agent-assisted-v2] | 001_dot_product | 20 | 72 | 1.97× | 1.05× | 1.49× |
| go-agent-v2-r1 | opencode-go/qwen3.8-max [agent-assisted-v2] | 001_dot_product | 16 | 86 | 3.83× | 1.72× | 3.29× |
| go-agent-v2-r1 | opencode-go/kimi-k2.6 [agent-assisted-v2] | 001_dot_product | 546 | 38 | 0.99× | 0.97× | 0.88× |
| go-agent-v2-r1-free | opencode-go/space-bunny-free [agent-assisted-v2] | 003_matmul | 274 | 53 | 4.52× | 3.39× | 4.11× |
| go-agent-v2-r1 | opencode-go/gpt-6-luna [agent-assisted-v2] | 002_gemv | 138 | 59 | 3.07× | 2.03× | 2.81× |
| go-agent-v2-r1 | opencode-go/grok-4.7 [agent-assisted-v2] | 003_matmul | 144 | 71 | 6.10× | 3.46× | 5.62× |
| go-agent-v2-r1 | opencode-go/minimax-m3 [agent-assisted-v2] | 002_gemv | 170 | 67 | 3.14× | 1.84× | 2.90× |
| go-agent-v2-r1 | opencode-go/qwen3.8-flash [agent-assisted-v2] | 002_gemv | 168 | 56 | 3.07× | 2.14× | 2.88× |
| go-agent-v2-r1 | opencode-go/deepseek-v4-pro [agent-assisted-v2] | 004_conv1d | 1016 | 51 | 4.50× | 3.75× | 4.25× |
