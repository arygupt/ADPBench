# OpenCode Go agent-assisted-v1

This is a separate benchmark track, not a repair or replacement of historical
single-shot results. Every model follows the same operations and limits.
Standardization makes attempts reproducible and failures visible; it does not
guarantee correct RTL or uninterrupted provider service.

## Shared path

The agent reads the task, writes `dut.v`, receives development-only feedback,
optionally revises, and explicitly submits. Its four native tools are:

| Tool | Allowed operation |
|---|---|
| `read_file` | Read `PROBLEM.md`, `dut.py`, or the current `dut.v`. |
| `write_file` | Replace only `dut.v`; no other file is writable. |
| `check` | Audit, synthesize, and simulate the current revision on development cases. |
| `submit` | Freeze and hash the current `dut.v`; no further edits are accepted. |

There is no shell, directory listing, network tool, or arbitrary command
execution. Markdown code and textual/XML tool calls are not submissions.
Provider adapters expose the same four operations through each API's native
tool-call format.

## Limits and fairness

Each model/problem slot gets at most **12 model turns, 3 development checks,
and 2 hours**. Each request uses the reviewed provider-maximum output limit,
not an additional small harness token allowance. A request has a 120-second
idle deadline and at most a one-hour wall deadline, shortened to the slot's
remaining time. Each development check has at most 10 minutes. API context
limits still apply; conversation bodies are additionally bounded to 1.5 MB.

These are fixed protocol limits, not a promise of literally unlimited API
output. The exact six model configurations and authorization window are in
[`go-agent-high-20260924.json`](go-agent-high-20260924.json); the first batch
used [`go-agent-20260923.json`](go-agent-20260923.json).

## Reasoning

Every model requests its highest-equivalent reasoning setting. Controls differ
by provider ([Models.dev](https://models.dev/api.json), checked 2026-09-24):

| Model | Request | Why |
|---|---|---|
| DeepSeek V4.1 Flash | thinking on, effort `high` | effort offers low / high / max |
| GLM-5.3-Flash | effort `high` | effort offers low / high / max; a `thinking` field is rejected |
| Qwen3.8 Flash | thinking on, 16,000-token budget | OpenCode's `high` mapping for Anthropic-style APIs |
| Kimi K2.6, MiMo V2.5 | thinking on | on/off only |
| MiniMax M2.7 | provider default | always reasons; no control |

Providers do not always honor requests: in `go-agent-20260923`, DeepSeek was
asked for thinking off but 91% of its output was reasoning. Receipts therefore
record measured reasoning per slot, and the site shows both.
[`go-canary-high-reasoning-20260924.json`](go-canary-high-reasoning-20260924.json)
checks that every provider accepts these settings, with one request per model,
before the batch spends any slots.

At least one development check must be attempted before submission; it need
not pass. A model may revise after its last check and submit an unchecked
final revision. The receipt tracks whether that final revision was checked.
Neither the harness nor the publisher chooses the best intermediate answer.

Development checks run in a restricted, no-network container using the
pinned toolchain and a seed-redacted harness. The agent receives no held-out
test results. After submission, a separate job with no model credentials
scores the frozen source once against the unchanged held-out evaluator.
Wrong RTL stays wrong; it is never repaired in response to final scores.

## Actions and evidence

Open [OpenCode Go agent runs](https://github.com/arygupt/ADPBench/actions/workflows/go-agent.yml)
to see preparation and individual model/problem jobs. The batch contains
12 independent slots: six models × dot product and GEMV. At most six slot
chains run concurrently; each begins scoring after its own generation ends,
without waiting for every other model.

The live dispatch must select `main`, enable `run_models`, and confirm
`subscription_only`. Both inputs default to false. PR and offline checks
make no model requests. Preparation validates the two baselines once before
generation starts.

Only generation receives `OPENCODE_GO_API_KEY`. Requests use the Go endpoint;
**Use balance must remain off**. There is no Zen-balance fallback, automatic
provider retry, or retry-until-pass. A durable tag claims each batch/model/
problem slot before its first request; rerunning the workflow cannot spend
again on a claimed slot. Revisions within the declared agent loop are model
turns, not additional benchmark attempts.

Each slot saves a canonical generation receipt and, when submitted, frozen
RTL. Separate scoring artifacts preserve records and hashes. Raw provider
responses, conversations, development workspaces, and scoring logs remain
separate private diagnostic artifacts, not website data.

An Actions green check means its execution completed, **not** that the RTL
passed. Incorrect RTL, invalid submissions, and exhausted model budgets are
legitimate recorded benchmark outcomes. Provider, transport, and harness
failures are reported separately. Interrupted scoring has an unknown score;
it is not silently converted into a pass or a measured incorrect result.

## Publication

A completed batch triggers
[Publish validated model results](https://github.com/arygupt/ADPBench/actions/workflows/publish-results.yml).
The publisher validates run provenance, slot coverage, artifacts, and frozen
records, then opens a **results PR**. It never approves or merges that PR.
Reviewing and merging it updates the website data and triggers the existing
Site build; public deployment still depends on the repository's Pages
configuration.

The website keeps this protocol separate from single-shot and earlier pilot
tracks. Historical attempts are not rewritten or removed when a new run
improves on them.
