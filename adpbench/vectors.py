"""Test vector generation: numpy reference -> packed hex for the simulator.

Each transaction gets its own seeded random inputs, and the vectors for all
transactions are concatenated in order, one file per input port. `input_lens`
gives the element count per transaction; arrays may be N-dimensional, in which
case the documented layout is row-major flattening.

The numeric contract is frozen by the problem's INTERFACE/QUANT blocks, so
vectors are compared bit-for-bit. There is no floating point tolerance here.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .problem import Problem


class MalformedOutput(ValueError):
    """The simulator produced words that are not a valid two's complement result.

    Unknown bits (x/z) reach the output file when a DUT drives them, or when
    reset/pipeline logic does not settle. That is a failed run, not a harness
    crash, so the caller turns this into a structured correctness failure.
    """


def _split_ports(problem: Problem, raw) -> list[np.ndarray]:
    if not isinstance(raw, (tuple, list)):
        raw = (raw,)
    ports = problem.input_ports
    if len(raw) != len(ports):
        raise ValueError(
            f"{problem.name}: get_inputs returned {len(raw)} arrays for {len(ports)} ports"
        )
    return [np.asarray(item) for item in raw]


def _flatten(problem: Problem, port: str, arr: np.ndarray) -> np.ndarray:
    flat = np.asarray(arr).reshape(-1)
    want = problem.input_lens[port]
    if flat.size != want:
        raise ValueError(
            f"{problem.name}: port {port} expects {want} elements per transaction, "
            f"got {flat.size}"
        )
    return flat


def build_vectors(problem: Problem, case: int | str) -> tuple[tuple[np.ndarray, ...], np.ndarray]:
    """Golden inputs and outputs for every transaction.

    `case` is an integer seed for random vectors, or the name of a directed
    case in the problem's `DIRECTED` map. Returns `(inputs, expected)` where
    `inputs` has one concatenated 1-D array per port and `expected` holds every
    transaction's output words in order.
    """
    transactions = problem.transactions
    if isinstance(case, str):
        if case not in problem.directed:
            raise KeyError(f"{problem.name}: no directed case named {case!r}")
        raw_txns = [problem.directed[case](txn) for txn in range(transactions)]
    else:
        state = np.random.get_state()
        try:
            np.random.seed(int(case))
            raw_txns = [problem.get_inputs() for _ in range(transactions)]
        finally:
            np.random.set_state(state)

    ports = problem.input_ports
    chunks: list[list[np.ndarray]] = [[] for _ in ports]
    expected_parts = []
    for raw in raw_txns:
        shaped = _split_ports(problem, raw)
        for index, (port, arr) in enumerate(zip(ports, shaped)):
            chunks[index].append(_flatten(problem, port, arr))
        expected_parts.append(
            np.atleast_1d(np.asarray(problem.reference(*shaped))).reshape(-1)
        )

    inputs = tuple(np.concatenate(per_port) for per_port in chunks)
    expected = np.concatenate(expected_parts)
    return inputs, expected


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


def write_vectors(problem: Problem, inputs: tuple, workdir: Path) -> list[Path]:
    """Write one `{port}_beats.hex` file per input port, all transactions in order."""
    lanes = int(problem.params["LANES"])
    width = int(problem.params["DATA_W"])
    written = []
    for port, arr in zip(problem.input_ports, inputs):
        arr = np.asarray(arr).reshape(-1)
        if arr.size % lanes != 0:
            raise ValueError(
                f"{problem.name}: {port} length {arr.size} is not a multiple of LANES={lanes}"
            )
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
    try:
        text = path.read_text()
    except OSError as exc:
        raise MalformedOutput(f"{path}: cannot read simulator output ({exc})") from exc

    words = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        token = line.split()[0]
        try:
            words.append(int(token, 16) & ((1 << width) - 1))
        except ValueError as exc:
            raise MalformedOutput(
                f"{path}: non-hex output word {token!r} (unknown bits in result?)"
            ) from exc
    if len(words) < out_len:
        raise MalformedOutput(f"{path}: expected {out_len} words, found {len(words)}")
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
