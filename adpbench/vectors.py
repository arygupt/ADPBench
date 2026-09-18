"""Test vector generation: numpy reference -> packed hex for the simulator.

The numeric contract is frozen by the problem's INTERFACE/QUANT blocks, so
vectors are compared bit-for-bit. There is no floating point tolerance here.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .problem import Problem


def _lane_width(problem: Problem, port: str) -> int:
    """Bit width of one element on the given port.

    v0 convention: DATA_W is shared by every input port and is declared in
    INTERFACE["params"]. Per-port widths will need an explicit map later.
    """
    return int(problem.params["DATA_W"])


def _pack_row(row: np.ndarray, width: int) -> str:
    """Pack one beat's elements into a single hex word.

    Element j occupies bits [j*width +: width]; element 0 is the low bits.
    """
    mask = (1 << width) - 1
    word = 0
    for j, value in enumerate(row):
        word |= (int(value) & mask) << (j * width)
    n_hex = (width * len(row)) // 4
    return f"{word:0{n_hex}x}"


def build_vectors(problem: Problem, seed: int) -> tuple[tuple, np.ndarray]:
    """Run the golden model on seeded random inputs."""
    state = np.random.get_state()
    try:
        np.random.seed(seed)
        inputs = problem.get_inputs()
    finally:
        np.random.set_state(state)

    if not isinstance(inputs, (tuple, list)):
        inputs = (inputs,)
    arrays = tuple(np.asarray(x) for x in inputs)
    expected = problem.reference(*arrays)
    return arrays, expected


def write_vectors(problem: Problem, inputs: tuple, workdir: Path) -> list[Path]:
    """Write one `{port}_beats.hex` file per input port."""
    lanes = int(problem.params["LANES"])
    written = []

    for port, arr in zip(problem.input_ports, inputs):
        if arr.ndim != 1:
            raise ValueError(f"{problem.name}: port {port} expects a 1-D vector, got {arr.shape}")
        if arr.shape[0] % lanes != 0:
            raise ValueError(
                f"{problem.name}: {port} length {arr.shape[0]} is not a multiple of LANES={lanes}"
            )
        width = _lane_width(problem, port)
        rows = arr.reshape(-1, lanes)
        path = workdir / f"{port}_beats.hex"
        path.write_text("\n".join(_pack_row(row, width) for row in rows) + "\n")
        written.append(path)

    return written


def quantize_expected(value: int, width: int) -> int:
    """Two's complement wrap so expected and simulated values compare in the same domain."""
    return int(value) & ((1 << width) - 1)


def read_outputs(path: Path, out_len: int, width: int) -> list[int]:
    """Read `out.hex` back as unsigned two's complement words.

    Icarus emits `// 0xADDR` markers between chunks; those are skipped.
    """
    words = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        words.append(int(line.split()[0], 16) & ((1 << width) - 1))
    if len(words) < out_len:
        raise ValueError(f"{path}: expected {out_len} words, found {len(words)}")
    return words[:out_len]


def to_signed(value: int, width: int) -> int:
    value &= (1 << width) - 1
    if value >= (1 << (width - 1)):
        value -= 1 << width
    return value


def compare(actual: list[int], expected: np.ndarray, width: int) -> dict:
    """Bit-exact comparison, reporting the first mismatch with signed values."""
    exp = [quantize_expected(int(v), width) for v in expected]
    if len(exp) != len(actual):
        return {"match": False, "detail": f"length mismatch: expected {len(exp)}, got {len(actual)}"}

    for i, (a, e) in enumerate(zip(actual, exp)):
        if a != e:
            return {
                "match": False,
                "detail": (
                    f"first mismatch at index {i}: "
                    f"expected {to_signed(e, width)}, got {to_signed(a, width)}"
                ),
            }
    return {"match": True, "detail": ""}
