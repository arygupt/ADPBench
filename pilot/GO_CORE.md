# OpenCode Go core-model screen

The manually dispatched **OpenCode Go model runs** workflow supports MiMo V2.5,
DeepSeek V4.1 Flash, Qwen3.8 Flash, GLM-5.3-Flash, Kimi K2.6 and MiniMax M2.7.
These cover six common model families offered by Go, not a claim about measured
market-share rankings. Cost-conscious variants are used; no Claude/OpenAI calls.

`go-core-20260922.json` authorizes 12 requests before 2026-09-23 00:00 UTC:
one dot-product and one GEMV attempt per model, 8,192 output tokens each,
24,000 prompt bytes and 600 seconds per request. There are no retries, repairs,
schedules, or non-Go endpoints. The user must confirm Go **Use balance is off**.

Preparation validates both baselines once. Six independently inspectable generation
jobs save responses; six separate scoring jobs evaluate frozen RTL in
network-disabled, credential-free containers.
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
endpoints, the twelve-request limit and no retries/fallback remain unchanged.
The new streaming plan uses a **120-second idle timeout and 30-minute total
request deadline**, with a 75-minute generation job budget. The scoring job has
its own 60-minute budget. Provider rejection, quota exhaustion and incorrect RTL
can still occur; a larger budget cannot guarantee a successful submission.

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

The [September 23 live compatibility canary](diagnostics/go-canary-20260923.md)
tested six tiny requests through the production streaming transport. Four models
completed answers, MiniMax streamed thinking until its diagnostic cap, and GLM
explicitly rejected the `thinking` field. The full benchmark remains disabled.
Canary dispatches are labeled separately and never published as benchmark scores.

## Interruption-safe execution

- **Stream and preserve:** Chat Completions and Messages SSE are assembled into
  atomic `response.partial.json` snapshots every two seconds while data arrives.
  Explicit terminal events are required. Disconnects, idle/wall timeouts,
  malformed streams and byte limits remain incomplete failures, never valid RTL.
  Unknown token usage is labeled incomplete. No reconnect or automatic repair.
- **Separate generation from scoring:** `go-generation-MODEL-RUN` contains the
  small canonical generation evidence. `go-raw-generation-MODEL-RUN` holds private
  prompts/responses/provider errors. Final scoring cannot erase either artifact.
- **Synthesize once:** every case uses the same hashed netlist. Held-out seeds,
  directed tests, backpressure, arithmetic comparisons and score formulas are
  unchanged. No design is credited without passing every required case.
- **Bound resource failures:** each problem runs in a separate 5 GiB / 2 CPU
  container, with no swap expansion, a 20-minute soft deadline and a 21-minute
  outer deadline. Individual tools have ten-minute limits. A failed problem
  does not cancel the next problem or another model. Process groups are cleaned
  up; Docker's `OOMKilled` flag is recorded instead of guessing from exit 137.
- **Checkpoint before the job ends:** synthesis, completed cases, tool logs,
  return codes and container status persist on host mounts. Final small records
  upload before the larger `go-checkpoints-MODEL-RUN` artifact. Tool logs are
  bounded at 16 MiB; streamed content at 16 MiB, wire traffic (including SSE/JSON
  framing) at 256 MiB, and individual SSE events at 2 MiB. Wire overhead is not
  mistaken for generated output tokens.

Runner loss, forced cancellation or an upload outage can still prevent the
latest checkpoint reaching GitHub. Evidence already uploaded by the generation
job survives a scoring-job failure. This is failure containment, not a promise
that hardware, providers or generated RTL can never fail.

### Scoring-only recovery

Use the exact source checkout, reviewed plan and trusted same-run artifacts.
Restore `go-generation-...` into `runs/model` and `go-checkpoints-...` into `runs`
(its top-level directories are `checkpoints` and `diagnostics`). Then:

```sh
python -m scripts.go_score \
  --plan pilot/go-core-provider-max-20260922.json --model mimo-v2.5 \
  --out runs/model --checkpoints runs/checkpoints --diagnostics runs/diagnostics \
  --image adpbench-go:ci --resume
```

This command has no generation path or API credential. It verifies the saved
plan and frozen RTL, and checkpoints bind RTL/spec/flow/toolchain/harness hashes.
It resumes unfinished cases, not completed incorrect results. Do not edit a
cache to make it pass, clear claim tags, or overwrite published results. Recovery
evidence needs separate review; this command does not republish an old attempt.
The current workflow does not automatically retry interrupted model requests.

Offline CI fault-injects disconnects, malformed streams, timeouts, process-tree
failures, OOM status, interrupted checkpoints and cache tampering. Real pinned
toolchain tests compare both baseline scores; PRs changing the scorer also
replay all six successful pilot-001 submissions. No model tokens are used.
