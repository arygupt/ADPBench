"""3x3 valid, stride-one max pooling of a 16x16 signed int8 image.

No padding. Input and output are row-major; each output is the sign-extended
int32 maximum of its K x K window. Two transactions without reset.
"""

import numpy as np

HEIGHT = 16
WIDTH = 16
K = 3
LANES = 8
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2
OUT_H = HEIGHT - K + 1
OUT_W = WIDTH - K + 1


class Model:
    def forward(self, x):
        image = np.asarray(x).reshape(HEIGHT, WIDTH).astype(np.int64)
        windows = np.lib.stride_tricks.sliding_window_view(image, (K, K))
        return windows.max(axis=(-2, -1))


def get_inputs():
    return (np.random.randint(-128, 128, size=(HEIGHT, WIDTH)).astype(np.int8),)


def _all_equal(txn):
    return (np.full((HEIGHT, WIDTH), -128 if txn == 0 else 127, np.int8),)


def _negative_only(txn):
    x = -128 + (np.arange(HEIGHT * WIDTH).reshape(HEIGHT, WIDTH) * 13 + txn * 7) % 128
    return (x.astype(np.int8),)


def _ramp(txn):
    x = (np.arange(HEIGHT * WIDTH) - 128).reshape(HEIGHT, WIDTH).astype(np.int8)
    return (x if txn == 0 else x[::-1, ::-1].copy(),)


def _checkerboard(txn):
    rows, cols = np.indices((HEIGHT, WIDTH))
    return (np.where((rows + cols + txn) % 2, -128, 127).astype(np.int8),)


def _impulses(txn):
    x = np.full((HEIGHT, WIDTH), -128, np.int8)
    # Corners and pixels around an input beat/row boundary expose window shifts.
    for row, col in ((0, 0), (0, WIDTH - 1), (HEIGHT - 1, 0),
                     (HEIGHT - 1, WIDTH - 1), (K - 1, LANES - 1), (K, LANES)):
        x[row, col] = 127
    return (x if txn == 0 else np.roll(x, 1, axis=0),)


INTERFACE = {
    "title": "2-D max pooling",
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"HEIGHT": HEIGHT, "WIDTH": WIDTH, "K": K, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_x_flat"],
    "input_lens": {"in_x_flat": HEIGHT * WIDTH},
    "output": "out_c",
    "out_len": OUT_H * OUT_W,
    "transactions": TRANSACTIONS,
    "packing": "row_major_lane_lsb_first",
}

DIRECTED = {
    "all_equal": _all_equal,
    "negative_only": _negative_only,
    "ramp": _ramp,
    "checkerboard": _checkerboard,
    "impulses": _impulses,
}

QUANT = {
    "in_x_flat": "signed int8 image, row-major HEIGHT x WIDTH",
    "out_c": "int32 sign-extended maxima, row-major (HEIGHT-K+1) x (WIDTH-K+1)",
    "pooling": "K x K maximum, stride 1, valid windows only (no padding)",
    "tolerance": "none - bit exact",
}
