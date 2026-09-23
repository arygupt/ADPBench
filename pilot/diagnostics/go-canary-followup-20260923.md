# GLM and MiniMax follow-up — September 23, 2026

Both models returned complete, correct answers to the tiny XOR diagnostic in
[Actions run 35817913080](https://github.com/arygupt/ADPBench/actions/runs/35817913080),
source commit `28c4487ad8918e4abdaef3912976f72d82621bb7`, attempt 1. The user
authorized follow-up attempts until these two models worked. Only **one additional
request per model** was needed; the other four models were not called again.

| Model | Change from original diagnostic | Finish | Input / output tokens |
| --- | --- | --- | ---: |
| GLM-5.3-Flash | Remove unsupported `thinking`; retain `reasoning_effort: low`; cap 128 → 1,024 | `stop` | 140 / 32 |
| MiniMax M2.7 | Keep native Messages settings; cap 128 → 1,024 | `end_turn` | 141 / 265 |

MiniMax reported 226 thinking tokens within its 265 output tokens. This explains
why the original 128-token diagnostic allowance was insufficient for that answer.
GLM now accepts the corrected profile. Its removed `thinking` field was explicitly
identified by the preceding HTTP 400; low reasoning effort remains accepted.

This follow-up used **281 reported input tokens and 297 output tokens** in total,
with valid accounting for both responses. The configured maximum was 2,048
output tokens. Requests used only the Go subscription endpoint, relying on the
user's Use balance OFF confirmation. No Zen fallback, automatic retries,
continuations, or full benchmark requests were made.

## Actual RTL checked, not just HTTP success

The [GLM RTL](go-canary-followup-20260923/glm-5.3-flash.v) and
[MiniMax RTL](go-canary-followup-20260923/minimax-m2.7.v) are exact copies of the
saved extracted answers, not repairs. Each passed:

- Yosys synthesis and `check -assert`, yielding one XOR cell.
- Icarus Verilog compilation.
- All four input combinations in [the XOR testbench](canary_xor_tb.sv).

These checks ran **locally**, separately from the source Actions job, in the
repository's pinned toolchain container with networking disabled, read-only
inputs, 512 MiB memory, one CPU, 64 PIDs, and per-tool deadlines. The
[receipt](go-canary-followup-20260923.json) records source identity, artifact and
RTL hashes, tool versions, and verification results. Raw reasoning remains only
in the private Actions artifact `go-canary-35817913080`.

PR CI also repeats these zero-token synthesis and simulation checks on the frozen
answers, after its ordinary baseline, recovery, and anti-cheat tests.

To repeat the zero-token check from the repository root after building the
`adpbench-agent:latest` image from `sandbox/Dockerfile`:

```sh
for model in glm-5.3-flash minimax-m2.7; do
  docker run --rm --pull never --network none --memory 512m --cpus 1 \
    --pids-limit 64 --cap-drop ALL --security-opt no-new-privileges \
    --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
    -v "$PWD/pilot/diagnostics/go-canary-followup-20260923/$model.v:/input/dut.v:ro" \
    -v "$PWD/pilot/diagnostics/canary_xor_tb.sv:/input/tb.sv:ro" \
    -w /tmp adpbench-agent:latest sh -c '
      timeout 20 yosys -q -p "read_verilog -sv /input/dut.v; hierarchy -check -top dut; synth -top dut -noabc; check -assert" &&
      timeout 20 iverilog -g2012 -s canary_xor_tb -o /tmp/canary.vvp /input/dut.v /input/tb.sv &&
      timeout 10 vvp /tmp/canary.vvp' || exit 1
done
```

## What this does not prove

The full benchmark remains disabled. These are compatibility diagnostics, **not
new dot-product/GEMV scores** and not proof of provider-maximum limit acceptance
or long-job reliability. The original failed Actions run, HTTP 400, MiniMax cap,
and all historical benchmark results remain unchanged. Nothing was made green
by suppressing an error. In this follow-up, caps, missing answers, malformed RTL
boundaries, stream failures, or invalid accounting fail the diagnostic.

Each finite diagnostic plan has a distinct durable claim. Re-dispatching this
already-claimed plan makes no new model requests; merging PR #16 likewise starts
no model calls.
