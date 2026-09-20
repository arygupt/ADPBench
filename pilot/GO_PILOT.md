# Scheduled Go pilot

The one-off GitHub Actions workflow `go-pilot.yml` schedules **DeepSeek V4.1
Flash only**, using the user's existing OpenCode Go subscription. GLM and Qwen
are not scheduled. No OpenAI or Anthropic model is selected.

The scheduled time is **September 21, 2026 at 00:07 UTC** (September 20 at
8:07 p.m. America/New_York), after the approximately five-hour reset requested
at 18:48 UTC. GitHub can start cron jobs late; this is an earliest start, not
a guarantee that the account's quota has reset. The date gate expires at
00:00 UTC on September 22, including if the workflow somehow remains enabled.

## Budget and protocol

- One request for each of the four Level 1 problems, in order; one repetition.
- Model: `opencode-go/deepseek-v4.1-flash`; no alternate model or provider.
- Maximum 8,192 output tokens per request, **32,768 output tokens total**.
  Input tokens are additional. The four current prompts total approximately
  29 KB of UTF-8 text; each prompt has an enforced 24,000-byte size limit.
- No repair turns, tool-use loops, automatic HTTP retries, or quota polling.
- HTTP errors, timeouts, and missing provider usage accounting stop further
  requests. The API may bill a request that times out; it is never resubmitted.
- A durable tag, `go-single-shot-20260921-deepseek-v4.1-flash`, is created
  **before** generation. A retry or rerun cannot spend again after that claim.
  Even failed attempts remain claimed. Manual dispatch only validates offline.
- The workflow disables itself after the scheduled attempt. The date gate
  independently prevents token usage if disabling fails or a future cron fires.

This is a **single-shot** coding evaluation through the documented Go API, not
the iterative OpenCode CLI protocol used in pilot-001. Scores carry the
`[single-shot]` label and are not silently merged into the existing leaderboard.
The Go request identifies `adpbench-coding-agent/0.0.1` and sends a stable
`x-opencode-session` for each model/problem conversation.

## Execution and evidence

The workflow first builds/restores the existing pinned Yosys/Icarus image and
scores all four known-good baselines offline. Only after this succeeds does it
claim the batch and call Go. `OPENCODE_GO_API_KEY` is an Actions secret, passed
only to the claim/generation steps. It is never written to a file, embedded in
the Docker image, included in a prompt, or forwarded to the scoring container.

Generation accepts only RTL text. Scoring audits the frozen submission and
runs the ordinary held-out ADPBench cases in a network-disabled container.
The workflow summary reports correctness, baseline improvements, and usage.
The `go-pilot-<run id>` artifact retains prompts, responses, generated/frozen
RTL, generation metadata, usage, records, manifests, and reports for 90 days.
Baseline-fixture results are separately labelled and stored.

To validate without using Go tokens:

```sh
python -m unittest discover -s tests -p test_go_pilot.py -v
gh workflow run go-pilot.yml
```

To cancel before it starts:

```sh
gh workflow disable go-pilot.yml
```

## Model selection sources (checked September 20, 2026)

[OpenCode's Go docs](https://opencode.ai/docs/go/) list DeepSeek V4.1 Flash,
DeepSeek V4 Pro, GLM-5.3, and Qwen3.8 Max as available Go models. The public
[`/models` endpoint](https://opencode.ai/zen/go/v1/models) and local
`opencode models opencode-go` also confirm availability. No official Go
popularity ranking was found. Flash was selected for this initial small pilot;
GLM/Qwen remain candidates for a later user-authorized run.

[GitHub schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
explains default-branch execution and possible cron delays.
