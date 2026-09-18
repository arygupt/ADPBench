"""c = sum_i a[i] * b[i] over signed int8 inputs, exact int32 result."""

import numpy as np

LEN = 256
LANES = 32
DATA_W = 8
ACC_W = 32


class Model:
    """Golden reference. No floats anywhere: exact integer arithmetic."""

    def forward(self, a, b):
        return np.array([np.dot(a.astype(np.int64), b.astype(np.int64))], dtype=np.int64)


def get_inputs():
    a = np.random.randint(-128, 128, size=LEN).astype(np.int8)
    b = np.random.randint(-128, 128, size=LEN).astype(np.int8)
    return a, b


INTERFACE = {
    "module": "dut",
    "protocol": "valid_ready",
    "params": {"LEN": LEN, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_a_flat", "in_b_flat"],
    "output": "out_c",
    "out_len": 1,
    # element j of a beat occupies bits [j*DATA_W +: DATA_W], element 0 lowest
    "packing": "lane_major_lsb_first",
}

QUANT = {
    "in_a_flat": "int8 two's complement",
    "in_b_flat": "int8 two's complement",
    "out_c": "int32 two's complement, exact",
    "accumulator": "int32, no saturation, no rounding",
    "tolerance": "none - bit exact",
}
