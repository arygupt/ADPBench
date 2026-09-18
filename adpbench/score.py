"""Scoring.

KernelBench averages speed ratios across correct submissions. The silicon
analogue is the area-delay product: with the clock pinned, time is proportional
to cycles, so

    adp = cells * cycles          (silicon cost x time)

and a submission's score is `baseline_adp / adp`, higher is better. A design
that merely widens the datapath cannot buy score this way: duplicating hardware
doubles cells to halve cycles. Score comes from doing less work per result.
"""

from __future__ import annotations

import math

import numpy as np


def area_delay_product(cells: int, cycles: int) -> float:
    if cells <= 0 or cycles <= 0:
        return -1.0
    return float(cells) * float(cycles)


def ratio(submission_adp: float, baseline_adp: float) -> float:
    if submission_adp <= 0 or baseline_adp <= 0:
        return -1.0
    return baseline_adp / submission_adp


def geometric_mean_ratio_correct_only(
    correct: np.ndarray, baseline_adp: np.ndarray, actual_adp: np.ndarray
) -> float:
    """KernelBench's aggregation, applied to area-delay ratio."""
    mask = np.asarray(correct, dtype=bool)
    if mask.sum() == 0:
        return 0.0
    ratios = np.asarray(baseline_adp)[mask] / np.asarray(actual_adp)[mask]
    return float(math.prod(ratios) ** (1.0 / mask.sum()))


def fast_p(
    correct: np.ndarray, baseline_adp: np.ndarray, actual_adp: np.ndarray, threshold: float = 1.0
) -> float:
    """Fraction of correct submissions beating the baseline by more than `threshold`."""
    mask = np.asarray(correct, dtype=bool)
    n = len(mask)
    if n == 0:
        return 0.0
    ratios = np.asarray(baseline_adp)[mask] / np.asarray(actual_adp)[mask]
    return float((ratios > threshold).sum() / n)
