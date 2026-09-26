"""Scoring.

ADPBench uses cell count times cycle count as an area-delay product proxy:

    adp = cells * cycles

A submission's score is `baseline_adp / adp`, higher is better. This balances
cell count against cycle count, but does not measure physical area or verify
that designs meet a common clock period.

Both functions return -1 when a value is missing or not positive.
"""

from __future__ import annotations


def area_delay_product(cells: int, cycles: int) -> float:
    if cells <= 0 or cycles <= 0:
        return -1.0
    return float(cells) * float(cycles)


def ratio(submission_adp: float, baseline_adp: float) -> float:
    if submission_adp <= 0 or baseline_adp <= 0:
        return -1.0
    return baseline_adp / submission_adp
