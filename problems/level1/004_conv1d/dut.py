"""y[f][o] = sum_k x[o+k] * w[f][k] for 1-D valid convolution.

x has IN_LEN int8 samples, w has F filters of K int8 taps, and y has F rows
of OUT_POS = IN_LEN - K + 1 exact int32 results. Two back-to-back
transactions without reset. `in_x_flat` and `in_w_flat` are independent
streams; `in_w_flat` carries w row-major (w[f][k] at f*K+k). Output words are
row-major y.
"""

import numpy as np

IN_LEN = 128
F = 4
K = 4
LANES = 16
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2
OUT_POS = IN_LEN - K + 1


class Model:
    """Golden reference: exact integer 1-D convolution."""

    def forward(self, x, w):
        x = x.astype(np.int64)
        w = w.astype(np.int64)
        out = np.zeros((F, OUT_POS), dtype=np.int64)
        for f in range(F):
            for o in range(OUT_POS):
                out[f, o] = int(np.dot(x[o:o + K], w[f]))
        return out


def get_inputs():
    x = np.random.randint(-128, 128, size=IN_LEN).astype(np.int8)
    w = np.random.randint(-128, 128, size=(F, K)).astype(np.int8)
    return x, w


def _zeros(txn):
    return np.zeros(IN_LEN, np.int8), np.zeros((F, K), np.int8)


def _zero_weights(txn):
    x = np.random.RandomState(1234 + txn)  # noqa: NPY002 - directed, fixed state
    return x.randint(-128, 128, size=IN_LEN).astype(np.int8), np.zeros((F, K), np.int8)


def _extreme_signs(txn):
    x = np.where((np.arange(IN_LEN) + txn) % 2 == 0, -128, 127).astype(np.int8)
    w = np.where(np.arange(F * K).reshape(F, K) % 2 == 0, 127, -128).astype(np.int8)
    return x, w


INTERFACE = {
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"IN_LEN": IN_LEN, "F": F, "K": K, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_x_flat", "in_w_flat"],
    "input_lens": {"in_x_flat": IN_LEN, "in_w_flat": F * K},
    "output": "out_c",
    "out_len": F * OUT_POS,
    "transactions": TRANSACTIONS,
    "packing": "row_major_lane_lsb_first",
}

DIRECTED = {
    "zeros": _zeros,
    "zero_weights": _zero_weights,
    "extreme_signs": _extreme_signs,
}

QUANT = {
    "in_x_flat": "int8 two's complement",
    "in_w_flat": "int8 two's complement, row-major filters (F x K)",
    "out_c": "int32 two's complement, exact, row-major y (F x OUT_POS)",
    "accumulator": "int32, no saturation, no rounding",
    "tolerance": "none - bit exact",
}
