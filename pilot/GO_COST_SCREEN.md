# Subscription-only cost-efficiency screen

This is a small, manually dispatched screen, not an ongoing sweep. It compares
MiMo V2.5, Qwen3.8 Flash, and GLM-5.3-Flash on dot product and GEMV. The reviewed
plan is `go-cost-screen-20260921.json`; it expires at 2026-09-23 00:00 UTC.

## Why these models

[OpenCode Go's pricing](https://opencode.ai/docs/go/#usage-limits), checked on
2026-09-21, lists the following USD-equivalent subscription usage per million
tokens. These are quota valuations, not additional subscription charges.

| Model | Uncached input | Output | Generation setting |
| --- | ---: | ---: | --- |
| MiMo V2.5 | $0.14 | $0.28 | Thinking disabled |
| Qwen3.8 Flash | $0.15 | $0.47 | Thinking disabled |
| GLM-5.3-Flash | $0.15 | $0.50 | Low reasoning effort |

MiMo also produced correct dot-product and GEMV submissions in pilot-001,
although that pilot used an iterative protocol. The Flash models provide two
inexpensive alternatives; this screen tests their suitability rather than
assuming their general coding performance predicts RTL correctness.

The earlier [DeepSeek run](https://github.com/arygupt/ADPBench/actions/runs/35561995563)
used all 32,768 output tokens and hit the cap on all four problems. It produced
no usable RTL. Repeating those defaults would not be an efficient next step.
This screen requests explicit thinking settings and stops if output truncates.
MiMo supports [disabling thinking](https://platform.xiaomimimo.com/docs/en-US/usage-guide/passing-back-reasoning_content);
GLM-5.3-Flash requires thinking, so it uses the documented
[low reasoning setting](https://docs.z.ai/api-reference/llm/chat-completion).
Requested settings and provider usage are saved; providers may differ in how
they implement the same token limit. This is not an identical-reasoning-budget
comparison or a statistically established model ranking.

## Limits and safety

- At most six requests: two problems per model, one attempt each.
- Maximum 8,192 output tokens per request: 49,152 across the screen. At the
  listed rates, capped output is about $0.021 of usage; input is additional.
- Maximum 24,000 prompt bytes per request and a 600-second request timeout.
- No retries, repairs, alternate providers, or automatic quota polling.
- HTTP failures, invalid accounting, malformed RTL output, or truncation stop
  all remaining model calls. A scored but incorrect design does not trigger a
  repair. Partial results remain in the workflow artifact.
- Go's **Use balance** setting must be OFF. The workflow requires explicit
  confirmation; the API client cannot independently verify this account setting.
- The durable tag `go-cost-screen-20260921` prevents a second billable batch,
  even after cancellation or failure. Do not delete it to retry.
- One cached toolchain build and one two-problem baseline check precede all
  model calls. RTL scoring runs with no network or credentials, a read-only
  repository, and CPU/memory/process limits.

## Run and inspect

PR checks are offline and do not use model quota. Dispatching with default
inputs also runs only those request-limit tests.

After confirming **Use balance** is off, the authorized invocation is:

```bash
gh workflow run go-cost-pilot.yml --ref main \
  -f run_models=true -f subscription_only=true
```

The Actions run summary contains separate reports for each model reached. Its
`go-cost-screen-<run-id>` artifact contains request payloads (never credentials),
prompts, response text, provider usage, generation settings, GitHub run identity,
frozen RTL, hashes, and scoring records. A green job means the pipeline completed,
not that all designs passed. Use the report's correctness and ADP fields.

Keep these results separate from the iterative pilot-001 leaderboard until
publication explicitly labels the two-problem single-shot protocol. Expand only
after inspecting usable RTL, correctness, token usage, and ADP improvements.
