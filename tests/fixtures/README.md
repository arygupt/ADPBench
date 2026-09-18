# Regression fixtures

Every file here is a counterexample that a review of the v0 scorer reproduced
against the real evaluation seeds. They are kept as executable regression
tests so the same exploit cannot come back silently.

| Fixture | Review item | Expected outcome |
|---|---|---|
| `parameter_mismatch.v` | 1. synthesis and simulation evaluated different hardware | correct, but synthesizes at the problem's `LANES`; it cannot score on a shrunken datapath |
| `synthesis_switch.v` | 1. `ifdef SYNTHESIS` diverges between yosys and iverilog | rejected: the simulated netlist is the synthesized (junk) branch |
| `ignores_out_ready.v` | 3. protocol bugs missed by the fixed backpressure schedule | rejected by the hold-stable conformance check |
| `state_leak.v` | 3. state that survives into the next transaction | rejected: the second transaction's result is wrong |
| `two_transactions.v` | 3. repeated transactions are part of the contract | passes: the sanity-check parallel design handles back-to-back transactions |
| `unknown_output.v` | 5. malformed simulation output raises an uncaught `ValueError` | structured correctness failure, no exception |
| `comment_words.v` | 5. audit flags trigger words inside comments | audit passes; real constructs still rejected |
| `blackbox_mul.v` | adversarial review: blackbox `$mul` counts as one cell but simulates | rejected at synthesis: not reduced to gate-level cells |
| `zero_handshake.v` | adversarial review: outputs credited without consuming inputs | rejected: transaction inputs were never accepted |
