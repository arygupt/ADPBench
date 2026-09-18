"""ADPBench: a KernelBench-shaped benchmark scored on silicon cost.

Problem = a runnable numeric reference (dut.py) + a port contract.
Submission = synthesizable Verilog implementing that reference.
Score = correctness gate, then baseline area-delay product / submission's.
"""

from .evaluate import evaluate, load_baseline, record_baseline, work_dir
from .problem import Problem, load_problem, repo_root
from .result import EvalResult

__all__ = [
    "EvalResult",
    "Problem",
    "evaluate",
    "load_baseline",
    "load_problem",
    "record_baseline",
    "repo_root",
    "work_dir",
]
