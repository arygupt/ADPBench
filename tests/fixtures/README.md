# Regression fixtures

Every file here is a counterexample that a review of the v0 scorer reproduced
against the real evaluation seeds. They are kept as executable regression
tests so the same exploit cannot come back silently.

| Fixture | Review item | Expected outcome |
|---|---|---|
| `parameter_mismatch.v` | 1. synthesis and simulation evaluated different hardware | correct, but synthesizes at the problem's `LANES`; it cannot score 79x on 785 cells |
| `synthesis_switch.v` | 1. `ifdef SYNTHESIS` diverges between yosys and iverilog | rejected: the simulated netlist is the synthesized (junk) branch |
| `ignores_out_ready.v` | 3. protocol bugs missed by the fixed backpressure schedule | rejected by the hold-stable conformance check |
| `unknown_output.v` | 5. malformed simulation output raises an uncaught `ValueError` | structured correctness failure, no exception |
| `comment_words.v` | 5. audit flags trigger words inside comments | audit passes; real constructs still rejected |
| `two_transactions.v` | 3. repeated transactions are untested | documented gap: the contract is not defined yet |
