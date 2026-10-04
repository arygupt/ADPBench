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

Each round is its own dispatch and plan file (`go-agent-v2-r1.json`, `-r2`,
`-r3`, with `attempt` 1–3). Each round runs every model on every problem
once, problem by problem, so every model has a complete, comparable set after
each round.

A rerun plan has `try: 2` and lists its `slots` explicitly. Only slots whose
first try ended with one of these outcomes are eligible:

- `quota_exhausted`
- `provider_error` (HTTP 429 or 5xx)
- `transport_interrupted` (except the wall timeout)
- `harness_error`

Wrong RTL, turn-limit, truncated and invalid submissions are model outcomes
and are never rerun. The voided first try stays in the published records.

### The shared usage limit

OpenCode Go's 5-hour usage limit is shared by the whole account, not set per
model. When it runs out, every model gets HTTP 429 `GoUsageLimitError`
(`"limitName": "5 hour"`). Round 1 ran 14 slots at once and used the window
in about 30 minutes: roughly $6 at list prices, across 13 finished slots and
43 cut off. A finished slot cost about $0.27 on average and $1.11 at most
(Kimi K3).

So a plan paces itself with these fields:

| Field | Effect |
|---|---|
| `max_parallel` | How many slots may spend at once (1–14). |
| `release_on_quota` | A slot that ends `quota_exhausted` keeps its evidence in the Actions artifacts, then releases its claim tag, so a later wave runs it fresh. It also sets a pause tag, so the rest of that run skips instead of each sending rejected requests. |
| `schedule: hourly` | [OpenCode Go agent schedule](../.github/workflows/go-agent-schedule.yml) checks every half hour (:07 and :37). It dispatches the next wave only when the plan is enabled and inside its window, no agent run is active, and a slot is unclaimed. Committing the plan is the authorization, including the confirmation that Go "Use balance" is off. |

[`go-agent-v2-r1-t2.json`](go-agent-v2-r1-t2.json) reruns round 1's 43
`quota_exhausted` slots this way, 3 at a time. At about $0.30 per slot that needs about three 5-hour windows,
if Go's weekly and monthly limits allow. A weekly or monthly cutoff looks the
same to the scheduler: slots release and wait. Only the plan's two-day window
ends the schedule. The first window (2026-09-27 00:20 UTC) finished 2 slots
before the monthly limit ran out; the window reopened at 2026-09-28 23:15 UTC
once it refilled.

### Free models

[`go-agent-v2-r1-free.json`](go-agent-v2-r1-free.json) runs round 1 for two
free Go models, Space Bunny (`space-bunny-free`, effort `high`) and LongCat 2.5
Preview (`longcat-2.5-preview-free`, thinking on), under the same protocol and
limits. Free models did not count against the Go monthly limit on 2026-09-28,
when every paid model returned `"limitName": "monthly"`. Its canary is
[`go-canary-v2-free.json`](go-canary-v2-free.json). While it is the selected
plan, the hourly scheduler leaves the `-t2` rerun alone; select the rerun again
once the monthly limit resets.

The free plan was selected on 2026-09-28 but replaced by the reopened rerun
before its canary ran, so none of its slots started. The rerun's second window
(to 2026-09-30 23:15 UTC) finished 21 of 43 slots before every wave from
2026-09-29 onward returned `"limitName": "weekly"`. The weekly limit still
blocked paid models on 2026-10-03 while both free models answered, so the free
plan and its canary were selected again with new windows from 2026-10-03
23:15 UTC. The rerun's remaining 22 slots (8 matmul, 14 conv1d) wait for the
weekly limit to reset.

Its first dispatch ([run 37161748425](https://github.com/arygupt/ADPBench/actions/runs/37161748425))
stopped every slot at validation, before any claim or model call, because
`go-agent-slot.yml` named the `-t2` plan in its own `PLAN` line. The slot
workflow now takes the plan from `go-agent.yml` and checks it against that
file's literal.

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

## Running a round

Both dispatches of [OpenCode Go agent runs](https://github.com/arygupt/ADPBench/actions/workflows/go-agent.yml)
must select `main` and confirm `subscription_only`. Keep Go **Use balance** off.

1. **Canary.** Set `generation_enabled: true` and a current window (at most
   one day) in `go-canary-v2.json`, merge, then dispatch with `run_canary`.
   Each model runs as its own job and claims the tag
   `go-compatibility-v2-…-<model>`, so a rerun cannot spend again.
2. **Round.** Once every model's canary is `completed`, set
   `generation_enabled: true` and a window of at most two days on the round
   plan (and its models' `not_before`), merge, then dispatch with
   `run_models`. The slot matrix comes from the plan. Each slot claims
   `<plan name>-<model>-<problem>`, and a rerun adds `-t2`.
3. **Publish.** A finished round triggers the publisher, which opens a
   review-only results PR for that round's dataset.

Not implemented yet: a leaderboard that combines rounds. Each round publishes
as its own dataset.

### Publishing a rerun

A rerun wave publishes into its round once the round's first try is merged.
Each finished slot goes next to the try it replaces, as `rep1-t2/` (and
`rep1-t2_frozen/dut.v`), and scoring counts it instead of `rep1`. The voided
try and every other published file stay unchanged; only the round's
leaderboard, `report.json` and `REPORT.md` are rebuilt, and the leaderboard's
`meta.reruns` lists each wave. The publisher refuses a slot whose first try is
not a published infrastructure failure (`quota_exhausted`, `provider_error`,
`transport_interrupted` or `harness_error`), skips slots a wave released after
the usage limit, and treats a republished wave as a no-op.

The scheduler dispatches waves with the workflow token, which never triggers
the publisher, so publish each wave by dispatching
[Publish OpenCode Go results](https://github.com/arygupt/ADPBench/actions/workflows/publish-results.yml)
with its run ID. A wave with no finished slot publishes nothing. Merge each
wave's results PR before dispatching the next, since every wave rebuilds the
same leaderboard. For round 1 the waves with finished slots are 36328258003,
36496973482 and 36562354781.
