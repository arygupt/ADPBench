# OpenCode Go core-model screen

The manually dispatched **OpenCode Go model runs** workflow evaluates MiMo V2.5,
DeepSeek V4.1 Flash, Qwen3.8 Flash, GLM-5.3-Flash, Kimi K2.6 and MiniMax M2.7.
These cover six common model families offered by Go, not a claim about measured
market-share rankings. Cost-conscious variants are used; no Claude/OpenAI calls.

`go-core-20260922.json` authorizes 12 requests before 2026-09-23 00:00 UTC:
one dot-product and one GEMV attempt per model, 8,192 output tokens each,
24,000 prompt bytes and 600 seconds per request. There are no retries, repairs,
schedules, or non-Go endpoints. The user must confirm Go **Use balance is off**.

Preparation validates both baselines once. Six independently inspectable model
jobs then generate and score in a network-disabled, credential-free container.
Matrix fail-fast is disabled: a model failure cannot cancel another model.
Per-model durable tags and local exclusive-create guards prevent duplicate
spending when a workflow is rerun. A new batch needs a new reviewed plan and
authorization, not deletion of an existing claim.

MiMo uses `reasoning: {enabled: false}`, validated by a three-request diagnostic.
Its old `thinking: {type: disabled}` setting still produced reasoning through
Go while provider accounting incorrectly reported zero reasoning tokens.
The runner now saves redacted full API responses and field-level diagnostics.
Kimi K2.6 is selected because it supports disabling thinking; MiniMax uses its
native behavior, GLM low effort, and DeepSeek requests disabled thinking/low
effort. Actual settings and token accounting remain in every artifact.

Each job summary distinguishes measured correctness from workflow completion.
Artifacts contain request settings, prompts, raw responses, token usage, frozen
RTL, manifests and score records. Green means evaluation completed, not that a
design beat the baseline. Generation failures are explicitly red. Results are
a separate single-shot dataset and must not be pooled with iterative pilot-001.
