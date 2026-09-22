# OpenCode Go: six-family single-shot screen

Actual model generation and scoring ran in [Actions run 35671789622](https://github.com/arygupt/ADPBench/actions/runs/35671789622),
evaluating commit `a03c6aaf7bbdaf811d34163b5fdb216c74c315ed`. Finished September 22, 2026 UTC.
These are new single-shot attempts, not replays or iterative pilot-001 results.

| Model | Dot product | GEMV |
| --- | --- | --- |
| MiMo V2.5 | Incorrect RTL: protocol timeout | Incorrect RTL: timeout |
| DeepSeek V4.1 Flash | Incorrect RTL: protocol check | Generation output cap |
| Qwen3.8 Flash | Incorrect RTL: protocol check | Incorrect RTL |
| GLM-5.3-Flash | Provider HTTP 400 | Not requested after safety stop |
| Kimi K2.6 | Generation output cap | Generated; scoring interrupted, score unknown |
| MiniMax M2.7 | Generation output cap | Generation output cap |

There are **zero confirmed-correct submissions**, not six successful benchmark
results. The twelve scheduled slots comprise ten saved model responses, one
provider rejection, and one unrequested safety-stop slot. Available responses
report **42,503 output tokens**. Requests used only the Go endpoints, without
retries or Zen fallback; the user confirmed subscription balance fallback off.

Kimi's job [106570070544](https://github.com/arygupt/ADPBench/actions/runs/35671789622/job/106570070544)
hit its one-hour maximum during offline scoring. GitHub records its conclusion
as `cancelled`; its check annotation confirms the execution-time limit. Cleanup
uploaded the artifact. Dot product has a final cap-error record. GEMV has saved
generation, usage and frozen RTL, but **no final scorer record**. Its published
`github-generation-only` entry preserves that distinction: no measured score,
audit result or scoring duration is invented.

The publisher validated the artifact plan, model/problem identities, original
run/attempt/commit, and submission hashes. Records include direct job provenance
in `execution`. Raw API responses remain in the private Actions artifacts; only
reviewed metadata, frozen RTL and score records are committed here. Future jobs
upload generation artifacts before expensive scoring as an additional safeguard.

The website exposes this batch as a separate dataset and preserves the original
pilot-001 dataset. A green model job means evaluation completed, not that its RTL
was correct; generation failures and interrupted scoring are not green passes.
