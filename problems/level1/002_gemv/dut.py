"""y = A @ x for A (ROWS x COLS) row-major int8 and x (COLS) int8.

y is exact int32. Two back-to-back transactions without reset. `in_a_flat`
carries A row-major (A[i][j] at i*COLS+j), `in_x_flat` carries x. The streams
are independent and start together each transaction.
"""

import numpy as np

ROWS = 16
COLS = 64
LANES = 16
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2


class Model:
    """Golden reference: exact integer matrix-vector product."""

    def forward(self, a, x):
        return (a.astype(np.int64) @ x.astype(np.int64)).astype(np.int64)


def get_inputs():
    a = np.random.randint(-128, 128, size=(ROWS, COLS)).astype(np.int8)
    x = np.random.randint(-128, 128, size=COLS).astype(np.int8)
    return a, x


def _zeros(txn):
    return np.zeros((ROWS, COLS), np.int8), np.zeros(COLS, np.int8)


def _min_values(txn):
    return np.full((ROWS, COLS), -128, np.int8), np.full(COLS, -128, np.int8)


def _extreme_signs(txn):
    rows = np.arange(ROWS * COLS).reshape(ROWS, COLS)
    a = np.where((rows + txn) % 2 == 0, -128, 127).astype(np.int8)
    x = np.where(np.arange(COLS) % 2 == txn % 2, 127, -128).astype(np.int8)
    return a, x


INTERFACE = {
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"ROWS": ROWS, "COLS": COLS, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_a_flat", "in_x_flat"],
    "input_lens": {"in_a_flat": ROWS * COLS, "in_x_flat": COLS},
    "output": "out_c",
    "out_len": ROWS,
    "transactions": TRANSACTIONS,
    "packing": "row_major_lane_lsb_first",
}

DIRECTED = {
    "zeros": _zeros,
    "min_values": _min_values,
    "extreme_signs": _extreme_signs,
}

QUANT = {
    "in_a_flat": "int8 two's complement, row-major A",
    "in_x_flat": "int8 two's complement",
    "out_c": "int32 two's complement, exact, one word per row",
    "accumulator": "int32, no saturation, no rounding",
    "tolerance": "none - bit exact",
}
