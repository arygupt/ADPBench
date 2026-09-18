"""c = sum_i a[i] * b[i] over signed int8 inputs, exact int32 result.

Two back-to-back transactions without reset. Inputs arrive on two independent
valid/ready streams of LEN elements each, LANES elements per beat, in order.
"""

import numpy as np

LEN = 256
LANES = 32
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2


class Model:
    """Golden reference. No floats anywhere: exact integer arithmetic."""

    def forward(self, a, b):
        return np.array([np.dot(a.astype(np.int64), b.astype(np.int64))], dtype=np.int64)


def get_inputs():
    a = np.random.randint(-128, 128, size=LEN).astype(np.int8)
    b = np.random.randint(-128, 128, size=LEN).astype(np.int8)
    return a, b


def _zeros(txn):
    return np.zeros(LEN, dtype=np.int8), np.zeros(LEN, dtype=np.int8)


def _min_values(txn):
    return np.full(LEN, -128, np.int8), np.full(LEN, -128, np.int8)


def _extreme_signs(txn):
    idx = np.arange(LEN)
    a = np.where(idx % 2 == txn % 2, -128, 127).astype(np.int8)
    b = np.where(idx % 2 == (txn + 1) % 2, 127, -128).astype(np.int8)
    return a, b


INTERFACE = {
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"LEN": LEN, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_a_flat", "in_b_flat"],
    "input_lens": {"in_a_flat": LEN, "in_b_flat": LEN},
    "output": "out_c",
    "out_len": 1,
    "transactions": TRANSACTIONS,
    # element j of a beat occupies bits [j*DATA_W +: DATA_W], element 0 lowest
    "packing": "lane_major_lsb_first",
}

DIRECTED = {
    "zeros": _zeros,
    "min_values": _min_values,
    "extreme_signs": _extreme_signs,
}

QUANT = {
    "in_a_flat": "int8 two's complement",
    "in_b_flat": "int8 two's complement",
    "out_c": "int32 two's complement, exact",
    "accumulator": "int32, no saturation, no rounding",
    "tolerance": "none - bit exact",
}
