# OpenCode Go core-model screen

The manually dispatched **OpenCode Go model runs** workflow supports MiMo V2.5,
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

## Provider-maximum output configuration

The workflow now selects `go-core-provider-max-20260922.json`. The old 8,192-token
plan and published results above remain unchanged. The runner no longer imposes
a separate 8,192-token ceiling: fixed plans may request larger integer limits,
and `output_budget: "provider_max"` requires a reviewed limit for every model.

Limits read on September 22 from the `opencode-go` entry in
[Models.dev](https://models.dev/api.json), the metadata source documented by
[OpenCode](https://opencode.ai/docs/providers#custom-provider):

- MiMo V2.5: 128,000 output tokens/request.
- DeepSeek V4.1 Flash: 384,000.
- Qwen3.8 Flash, GLM-5.3-Flash, MiniMax M2.7: 131,072 each.
- Kimi K2.6: 65,536.

These are advertised model maxima, not infinite output or an assurance that a
live gateway accepts every maximum. No generation request was made to test them.
The exact integers are sent using each API's required token-limit field; omitting
the field could select a smaller provider default, and Messages requires a limit.
The values are pinned for reproducibility, not silently refreshed during a run.

The new plan has **`generation_enabled: false`**. Configuring a larger output
budget does not launch another batch. Before a separately requested run, review
the metadata, enable the plan and set a current two-day-or-shorter authorization
window. A subsequent distinct batch also needs a fresh name/file and publication
policy registration; never clear old model-claim tags.

With two problems per model, the configured worst-case output allowance is
**1,941,504 tokens**, plus input tokens. Usage may be much lower, but subscription
quota can be exhausted faster. Keep Go **Use balance off**. Subscription-only
endpoints, the twelve-request limit, no retries/fallback, the ten-minute request
timeout and the one-hour scoring-job limit remain unchanged. Timeout, provider
rejection and incorrect RTL failures can still occur with larger output budgets.

Generation artifacts and job summaries record each model's actual requested
limit. Publication validates that limit against the source run's reviewed plan;
the site labels the per-model budget and keeps this dataset separate from the
earlier 8,192-token screen. No historical responses or scores are rewritten.

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

The [saved failure investigation](diagnostics/go-core-20260922.md) records Kimi's
offline RTL counterexample and synthesis-resource evidence, the limits of the
available GLM HTTP 400 evidence, and the output-cap analysis. These diagnostics
do not replace scores or repair submissions.

Completed eligible runs now feed [validated results PR publication](../site/README.md#automatic-results-prs).
The publisher preserves failed/unknown outcomes, does not call models, and never
approves or merges PRs. Human review and merge update the site build; public
Pages deployment remains separately controlled.
