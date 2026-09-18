"""C = A @ B for A (M x K) and B (K x N), row-major int8, exact int32 C.

Two back-to-back transactions without reset. `in_a_flat` carries A row-major
(A[i][k] at i*K+k), `in_b_flat` carries B row-major (B[k][j] at k*N+j). The
streams are independent and start together each transaction; C is emitted
row-major, one word per element.
"""

import numpy as np

M = 8
N = 8
K = 16
LANES = 16
DATA_W = 8
ACC_W = 32
TRANSACTIONS = 2


class Model:
    """Golden reference: exact integer matrix product."""

    def forward(self, a, b):
        return (a.astype(np.int64) @ b.astype(np.int64)).astype(np.int64)


def get_inputs():
    a = np.random.randint(-128, 128, size=(M, K)).astype(np.int8)
    b = np.random.randint(-128, 128, size=(K, N)).astype(np.int8)
    return a, b


def _zeros(txn):
    return np.zeros((M, K), np.int8), np.zeros((K, N), np.int8)


def _min_values(txn):
    return np.full((M, K), -128, np.int8), np.full((K, N), -128, np.int8)


def _extreme_signs(txn):
    aidx = np.arange(M * K).reshape(M, K)
    bidx = np.arange(K * N).reshape(K, N)
    a = np.where((aidx + txn) % 2 == 0, -128, 127).astype(np.int8)
    b = np.where((bidx + txn) % 2 == 0, 127, -128).astype(np.int8)
    return a, b


INTERFACE = {
    "module": "dut",
    "protocol": "valid_ready_streams",
    "params": {"M": M, "N": N, "K": K, "LANES": LANES, "DATA_W": DATA_W, "ACC_W": ACC_W},
    "inputs": ["in_a_flat", "in_b_flat"],
    "input_lens": {"in_a_flat": M * K, "in_b_flat": K * N},
    "output": "out_c",
    "out_len": M * N,
    "transactions": TRANSACTIONS,
    "packing": "row_major_lane_lsb_first",
}

DIRECTED = {
    "zeros": _zeros,
    "min_values": _min_values,
    "extreme_signs": _extreme_signs,
}

QUANT = {
    "in_a_flat": "int8 two's complement, row-major A (M x K)",
    "in_b_flat": "int8 two's complement, row-major B (K x N)",
    "out_c": "int32 two's complement, exact, row-major C (M x N)",
    "accumulator": "int32, no saturation, no rounding",
    "tolerance": "none - bit exact",
}
