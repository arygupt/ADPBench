"""The random seeds a submission is checked on.

An agent may iterate against DEV_SEEDS as often as it likes. Final scoring
uses EVAL_SEEDS, which the agent never sees. This is what makes hardcoding
expected outputs useless: a correct, general design scores identically on
both sets, while a memorised one collapses on the held-out set.

The sandbox bundle (see `environment.build_sandbox_bundle`) replaces this file
with a copy where EVAL_SEEDS is empty, so the held-out seeds never enter an
agent's container.
"""

DEV_SEEDS = (0, 1)
EVAL_SEEDS = (1000, 1001, 1002)
