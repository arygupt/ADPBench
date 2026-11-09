"""Row-wise argmax over signed int8 values; ties choose the lowest column.

Input is ROWS x COLS in row-major order, LANES elements per beat. Output is
one nonnegative int32 column index per row. Two transactions without reset.
"""

import numpy as np

ROWS = 8
COLS = 64
LANES = 8
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2


class Model:
    def forward(self, x):
        return np.argmax(np.asarray(x).reshape(ROWS, COLS), axis=1).astype(np.int64)


def get_inputs():
    return (np.random.randint(-128, 128, size=(ROWS, COLS)).astype(np.int8),)


def _all_equal(txn):
    return (np.full((ROWS, COLS), -128 if txn == 0 else 127, np.int8),)


def _negative_only(txn):
    x = -128 + (np.arange(ROWS * COLS).reshape(ROWS, COLS) * 7 + txn) % 127
    x[np.arange(ROWS), (np.arange(ROWS) * 9 + txn) % COLS] = -1
    return (x.astype(np.int8),)


def _tied_maxima(txn):
    x = np.full((ROWS, COLS), -128, np.int8)
    # Equal winners straddle a beat boundary and appear again at the row end.
    x[:, [LANES - 1, LANES, COLS - 1]] = 127
    x[txn::2, 0] = 127
    return (x,)


def _edge_winners(txn):
    x = np.full((ROWS, COLS), -1, np.int8)
    for row in range(ROWS):
        x[row, 0 if (row + txn) % 2 == 0 else COLS - 1] = 0
    return (x,)


def _ordered(txn):
    x = np.tile(np.arange(-COLS, 0, dtype=np.int8), (ROWS, 1))
    x[txn::2] = x[txn::2, ::-1]
    return (x,)


INTERFACE = {
    "title": "Row-wise argmax",
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"ROWS": ROWS, "COLS": COLS, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_x_flat"],
    "input_lens": {"in_x_flat": ROWS * COLS},
    "output": "out_c",
    "out_len": ROWS,
    "transactions": TRANSACTIONS,
    "packing": "row_major_lane_lsb_first",
}

DIRECTED = {
    "all_equal": _all_equal,
    "negative_only": _negative_only,
    "tied_maxima": _tied_maxima,
    "edge_winners": _edge_winners,
    "ordered": _ordered,
}

QUANT = {
    "in_x_flat": "signed int8 two's complement, row-major ROWS x COLS",
    "out_c": "int32 column index, zero-based; first (lowest) index wins ties",
    "tolerance": "none - bit exact",
}
