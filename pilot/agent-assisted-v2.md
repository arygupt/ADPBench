# OpenCode Go agent-assisted-v2

v2 is the [agent-assisted-v1](agent-assisted-v1.md) protocol with more turns,
retries for provider failures, the Responses API, and repeated rounds. Everything
not listed here is unchanged:

- the four tools (`read_file`, `write_file`, `check`, `submit`) and their schemas
- the system prompt, apart from the version name
- 3 development checks and 2 hours per slot
- no hidden-test feedback, and submission only through `submit()`
- one held-out scoring run in a separate job with no model credentials

v1 and v2 results are separate leaderboards and are never pooled.

## What changed

| | v1 | v2 |
|---|---|---|
| Model turns | 12 | **20** |
| Turns-left reminders | at 3 and 1 | same |
| APIs | chat/completions, messages | + **responses** (GPT, Grok) |
| Provider failure | ends the slot | the identical request is resent up to **2** times |
| Infrastructure-failed slot | final | may be **rerun once** |
| Attempts | one | one per **round**, recorded as `rep1`–`rep3` |

The limits are fixed in `PROTOCOL_LIMITS` in `scripts/go_agent.py`. A plan
must match them exactly.

## Retries

A request is resent unchanged, after 30 s and then 120 s, only when the failure
says nothing about the model:

- the stream disconnected, went idle, or broke mid-read
- the provider sent a stream error event
- HTTP 429, 500, 502, 503 or 504

A retry is skipped unless at least a minute of the slot would remain after
the wait. The partial response from a failed try is kept as private evidence
(`turn-NN/retry-K/`) and never shown to the model.

Never retried: HTTP 400, 401, 403 or 404; the one-hour stream wall timeout;
oversized streams; output-cap truncation; and malformed tool calls.

Each retry is recorded in the receipt's `transport_retries`, with the turn,
the reason, and whether its token usage is known. A dropped stream may have
been billed without reporting usage, so the receipt's `incomplete_usage` then
stays true. A 429 that persists after both retries ends the slot as
`quota_exhausted`, which counts as an infrastructure failure, not a model
failure.

## Rounds and reruns

OpenCode Go limits spending per model: the $15 tier gets $3 per 5 hours and
$7.50 per week. So each round is its own dispatch and plan file
(`go-agent-v2-r1.json`, `-r2`, `-r3`, with `attempt` 1–3). Each round runs
every model on every problem once, problem by problem, so a model's slots are
spread over time. Every model has a complete, comparable set after each
round.

A rerun plan has `try: 2` and lists its `slots` explicitly. Only slots whose
first try ended with one of these outcomes are eligible:

- `quota_exhausted`
- `provider_error` (HTTP 429 or 5xx)
- `transport_interrupted` (except the wall timeout)
- `harness_error`

Wrong RTL, turn-limit, truncated and invalid submissions are model outcomes
and are never rerun. The voided first try stays in the published records.

## Reasoning

Every model requests "high" where it offers it, or the nearest setting
otherwise ([Models.dev](https://models.dev/api.json), checked 2026-09-26).
The [compatibility canary](go-canary-v2.json) confirms each setting before a
round spends any slots.

| Model | API | Request |
|---|---|---|
| GPT-6 Luna, Grok 4.7 | responses | `reasoning.effort: high` |
| Kimi K3 | chat/completions | thinking on (its only effort is max) |
| DeepSeek V4 Pro | chat/completions | thinking on, effort `high` |
| GLM-5.3 | chat/completions | effort `high` |
| Qwen3.8 Max, MiniMax M3 | messages | thinking on, 16,000-token budget |
| MiMo V2.6 Pro | chat/completions | `reasoning.enabled` |
| Six v1 budget models | as in v1 | unchanged from [`go-agent-high-20260924.json`](go-agent-high-20260924.json) |

Responses requests set `store: false` and include encrypted reasoning, so a
model's reasoning goes back with the history on the next turn, as thinking
blocks do for messages.

## Canary

`python -m scripts.go_agent canary --config pilot/go-canary-v2.json --model <id>`
makes one tiny write, check and submit round trip, capped at 20,000 output
tokens per request and 4 turns. It uses the same request builder, transport,
tool encoding and reasoning settings as a scored slot. A model passes when
its second request is accepted (tool results, and reasoning for Responses,
were encoded correctly) and it submits. Nothing is retried, and canary
results are never scores.

Both the canary and round 1 are committed with `generation_enabled: false`.
Enabling either needs a reviewed authorization window.
