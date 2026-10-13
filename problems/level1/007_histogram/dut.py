"""Histogram of LEN bin IDs in [0, BINS), delivered LANES per beat.

Each ID is an 8-bit nonnegative integer. Output BINS exact int32 counts in
bin order. Every lane counts, including same-beat collisions. Counts restart
for the second transaction without a reset.
"""

import numpy as np

LEN = 256
BINS = 16
LANES = 8
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2


class Model:
    def forward(self, x):
        return np.bincount(np.asarray(x).reshape(-1).astype(np.int64), minlength=BINS)


def get_inputs():
    return (np.random.randint(0, BINS, size=LEN).astype(np.int8),)


def _same_bin(txn):
    # A count of 256 must not wrap at eight bits. Change bins between txns.
    return (np.full(LEN, 0 if txn == 0 else BINS - 1, np.int8),)


def _uniform(txn):
    return (((np.arange(LEN) + txn) % BINS).astype(np.int8),)


def _beat_collisions(txn):
    return (((np.arange(LEN) // LANES + txn * 3) % BINS).astype(np.int8),)


def _skewed(txn):
    x = np.zeros(LEN, np.int8)
    x[::2 + txn] = BINS - 1
    return (x,)


def _sparse(txn):
    ids = np.array([1, 7, 14], dtype=np.int8)
    return (ids[(np.arange(LEN) + txn) % len(ids)],)


INTERFACE = {
    "title": "16-bin histogram",
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"LEN": LEN, "BINS": BINS, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_x_flat"],
    "input_lens": {"in_x_flat": LEN},
    "output": "out_c",
    "out_len": BINS,
    "transactions": TRANSACTIONS,
    "packing": "lane_major_lsb_first",
}

DIRECTED = {
    "same_bin": _same_bin,
    "uniform": _uniform,
    "beat_collisions": _beat_collisions,
    "skewed": _skewed,
    "sparse": _sparse,
}

QUANT = {
    "in_x_flat": "8-bit bin IDs, restricted to 0 <= x < BINS (0..15)",
    "out_c": "BINS exact nonnegative int32 counts, ascending bin order",
    "accumulator": "counts include all LEN inputs; no saturation or wraparound",
    "tolerance": "none - bit exact",
}
