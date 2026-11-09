# cli-agent-v1

cli-agent-v1 runs vendor coding agents, Claude Code and Codex, on ADPBench
through their subscription logins. It measures the model together with its
vendor's agent harness, so it is its own leaderboard: never pooled with
agent-assisted-v1 or v2, which give every model the same four tools and turn
budget.

| | agent-assisted-v2 | cli-agent-v1 |
|---|---|---|
| Agent loop | ours: `read_file`, `write_file`, `check`, `submit` | the vendor CLI's own tools and shell |
| Budget | 20 turns, 3 checks, 2 hours | 2 hours wall clock, unlimited `./check.sh` |
| Billing | OpenCode Go | Claude and ChatGPT subscriptions |
| Token usage | recorded per request | not reported by subscriptions |

## What runs

[`cli-agent-v1.json`](cli-agent-v1.json): every problem in the catalog, three
repetitions, four agents on two subscriptions. 96 runs, 24 per agent.

| Label | CLI | Model and reasoning |
|---|---|---|
| `claude-code/claude-fable-5-1` | Claude Code 2.1.321 | `--model claude-fable-5-1 --effort high` |
| `claude-code/claude-fable-5-2` | Claude Code 2.1.321 | `--model claude-fable-5-2 --effort high` |
| `codex/gpt-6.1-sol` | Codex 0.167.0 | `--model gpt-6.1-sol`, `model_reasoning_effort=high` |
| `codex/gpt-6-luna` | Codex 0.167.0 | `--model gpt-6-luna`, `model_reasoning_effort=high` |

GPT-6.1 Sol is Codex's default model, and GPT-6 Luna is the OpenAI model in the
agent-assisted-v2 rounds. Grok and Kimi have no subscription CLI, so they are
not in this track; they need API keys and an agent-assisted-v2 plan.

Reasoning is "high" for all four, matching v2's high-or-nearest policy. All get
the same prompt as the [free-model pilot](pilot_free_models.json). Web search
and fetch are off: Claude Code with `--disallowedTools WebSearch WebFetch`,
Codex with `web_search=disabled`. The network stays on for the model API.

## The sandbox

Each run is one `adpbench agent --sandbox docker` cell in the
[`adpbench-agent-cli`](../sandbox/Dockerfile.cli) image, which is the pinned
Yosys and Icarus substrate plus both CLIs at fixed versions. As in every
sandboxed run, the task directory is the only writable harness path, the
bundle has the held-out seeds redacted, and scoring happens on the host from a
frozen copy. The CLIs run as a non-root `agent` user and skip their own
permission prompts, because the container is the sandbox.

The full session is recorded: Claude Code's `stream-json` and Codex's `--json`
event streams land in each run's `terminal.log` and `agent.log`.

## Logins

Credentials stay on the host and are never written to the repo, the image, or
the run records. A record keeps only the variable name and mount target.

- **Claude Code**: `claude setup-token` prints a long-lived subscription
  token. Export it as `CLAUDE_CODE_OAUTH_TOKEN`; the harness passes it by name
  (`docker run -e CLAUDE_CODE_OAUTH_TOKEN`), so the value never appears in a
  command line or log.
- **Codex**: sign in once into a dedicated directory, which each run mounts
  at `/home/agent/.codex`:

  ```bash
  mkdir -p ~/.adpbench/codex
  CODEX_HOME=~/.adpbench/codex codex login
  ```

  A separate login keeps your everyday `~/.codex` out of the container, and
  avoids two copies of one refresh token invalidating each other.

## Running

```bash
docker build -t adpbench-agent sandbox/
docker build -f sandbox/Dockerfile.cli -t adpbench-agent-cli sandbox/
export CLAUDE_CODE_OAUTH_TOKEN=...          # from `claude setup-token`

adpbench pilot --config pilot/cli-agent-v1-canary.json --jobs 2   # one easy problem each
adpbench pilot --config pilot/cli-agent-v1.json --jobs 2
```

The canary proves each login and model name before the 96 runs start: a model
the subscription does not offer fails there, in minutes.

`"order": "round"` runs repetition 1 of every agent on every problem before
repetition 2, so a session cut short still leaves complete rounds. `--jobs 2`
keeps two runs going at once; more would press harder on the two
subscriptions' usage limits, which the four agents share in pairs.

## Usage limits

Subscriptions have rolling usage windows. When a run's log matches one of its
agent's `infra_patterns` (a usage limit, a rate limit, an expired login) and
the design was not correct, the run is recorded as an infrastructure failure,
not a model failure, and that agent is paused for the rest of the session so
it does not burn its remaining cells. Continue once the limit resets:

```bash
adpbench pilot --config pilot/cli-agent-v1.json --resume runs/pilot_<stamp>_cli-agent-v1
```

A resume runs the cells that never ran and retries each infrastructure failure
in `rep<N>-t<k>`. The voided try keeps its record; the report scores the latest
try only. A resume refuses a config that differs from the pilot's `plan.json`.

Wrong RTL, timeouts, and anything else the model did are final and never
retried.

## Limits of the comparison

- A subscription does not report tokens or cost per run, so this track has no
  cost column.
- The vendor may serve a different model build behind the same name over
  time. Each run records the CLI version and the requested model.
- The agent has a shell and network access for its model API. Web tools are
  off, but a shell could still fetch pages; review `agent.log` for any fetch of
  the ADPBench repository before publishing.
- Running the official CLIs with your own subscription is the intended use.
  Do not extract the subscription's OAuth token into another client.
