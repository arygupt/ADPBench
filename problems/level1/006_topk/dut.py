"""Largest K signed int8 values from LEN inputs, in descending order.

Duplicates are retained with multiplicity; outputs are values, not indices.
Output values are sign-extended to int32. Two transactions without reset.
"""

import numpy as np

LEN = 64
K = 8
LANES = 8
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2


class Model:
    def forward(self, x):
        return np.sort(np.asarray(x).reshape(-1).astype(np.int64))[::-1][:K].copy()


def get_inputs():
    return (np.random.randint(-128, 128, size=LEN).astype(np.int8),)


def _all_equal(txn):
    return (np.full(LEN, -128 if txn == 0 else 127, np.int8),)


def _ordered(txn):
    x = np.arange(-LEN // 2, LEN // 2, dtype=np.int8)
    return (x if txn == 0 else x[::-1].copy(),)


def _duplicates(txn):
    x = np.full(LEN, -128, np.int8)
    # Fewer than K maxima: the remaining outputs must include repeated minima.
    x[(np.arange(K - 2) * 9 + txn) % LEN] = 127
    return (x,)


def _negative_only(txn):
    x = np.arange(-128, -128 + LEN, dtype=np.int8)
    return (np.roll(x, txn * (LANES + 1)),)


def _extreme_signs(txn):
    return (np.where((np.arange(LEN) + txn) % 2, -128, 127).astype(np.int8),)


INTERFACE = {
    "title": "Top-K selection",
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"LEN": LEN, "K": K, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_x_flat"],
    "input_lens": {"in_x_flat": LEN},
    "output": "out_c",
    "out_len": K,
    "transactions": TRANSACTIONS,
    "packing": "lane_major_lsb_first",
}

DIRECTED = {
    "all_equal": _all_equal,
    "ordered": _ordered,
    "duplicates": _duplicates,
    "negative_only": _negative_only,
    "extreme_signs": _extreme_signs,
}

QUANT = {
    "in_x_flat": "signed int8 two's complement",
    "out_c": "K int32 values, sign-extended, descending; duplicates retained",
    "tolerance": "none - bit exact",
}
