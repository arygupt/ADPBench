"""Test vectors: NumPy reference -> packed hex files for the simulator, and back.

Each transaction gets its own inputs, and the vectors for all transactions are
concatenated in order, one file per input port. `input_lens` gives the element
count per transaction; arrays may be N-dimensional, in which case they are
flattened row-major.

The numeric contract is frozen by the problem's INTERFACE/QUANT blocks, so
outputs are compared bit-for-bit. There is no floating point tolerance.
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


def build_vectors(problem: Problem, case: int | str) -> tuple[tuple[np.ndarray, ...], np.ndarray]:
    """Golden inputs and outputs for every transaction of one case.

    `case` is an integer seed for random vectors, or the name of a directed
    case in the problem's `DIRECTED` map. Returns `(inputs, expected)`, where
    `inputs` has one concatenated 1-D array per port and `expected` holds every
    transaction's output words in order.
    """
    transaction_inputs = _inputs_per_transaction(problem, case)

    per_port: list[list[np.ndarray]] = [[] for _ in problem.input_ports]
    expected_parts = []
    for raw in transaction_inputs:
        arrays = _split_ports(problem, raw)
        for index, (port, array) in enumerate(zip(problem.input_ports, arrays)):
            per_port[index].append(_flatten(problem, port, array))
        expected_parts.append(np.asarray(problem.reference(*arrays)).reshape(-1))

    inputs = tuple(np.concatenate(chunks) for chunks in per_port)
    expected = np.concatenate(expected_parts)
    return inputs, expected


def _inputs_per_transaction(problem: Problem, case: int | str) -> list:
    """Call the problem's input generator once per transaction."""
    if isinstance(case, str):
        if case not in problem.directed:
            raise KeyError(f"{problem.name}: no directed case named {case!r}")
        generator = problem.directed[case]
        return [generator(txn) for txn in range(problem.transactions)]

    # Problem specs draw from NumPy's global random state. Seed it for this
    # case, then restore it so callers are unaffected.
    saved_state = np.random.get_state()
    try:
        np.random.seed(int(case))
        return [problem.get_inputs() for _ in range(problem.transactions)]
    finally:
        np.random.set_state(saved_state)


def _split_ports(problem: Problem, raw) -> list[np.ndarray]:
    if not isinstance(raw, (tuple, list)):
        raw = (raw,)
    ports = problem.input_ports
    if len(raw) != len(ports):
        raise ValueError(
            f"{problem.name}: get_inputs returned {len(raw)} arrays for {len(ports)} ports"
        )
    return [np.asarray(item) for item in raw]


def _flatten(problem: Problem, port: str, array: np.ndarray) -> np.ndarray:
    flat = np.asarray(array).reshape(-1)
    expected_size = problem.input_lens[port]
    if flat.size != expected_size:
        raise ValueError(
            f"{problem.name}: port {port} expects {expected_size} elements per transaction, "
            f"got {flat.size}"
        )
    return flat


def write_vectors(problem: Problem, inputs: tuple, workdir: Path) -> list[Path]:
    """Write one `{port}_beats.hex` file per input port, one beat per line."""
    lanes = int(problem.params["LANES"])
    width = int(problem.params["DATA_W"])
    written = []
    for port, array in zip(problem.input_ports, inputs):
        array = np.asarray(array).reshape(-1)
        if array.size % lanes != 0:
            raise ValueError(
                f"{problem.name}: {port} length {array.size} is not a multiple of LANES={lanes}"
            )
        beats = array.reshape(-1, lanes)
        path = workdir / f"{port}_beats.hex"
        path.write_text("\n".join(_pack_beat(beat, width) for beat in beats) + "\n")
        written.append(path)
    return written


def _pack_beat(beat: np.ndarray, width: int) -> str:
    """Pack one beat's elements into a single hex word.

    Element j occupies bits [j*width +: width]; element 0 is the low bits.
    """
    mask = (1 << width) - 1
    word = 0
    for j, value in enumerate(beat):
        word |= (int(value) & mask) << (j * width)
    hex_digits = (width * len(beat)) // 4
    return f"{word:0{hex_digits}x}"


def read_outputs(path: Path, out_len: int, width: int) -> list[int]:
    """Read the simulator's output file back as unsigned `width`-bit words.

    Icarus emits `// 0xADDR` markers between chunks; those are skipped.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise MalformedOutput(f"{path}: cannot read simulator output ({exc})") from exc

    mask = (1 << width) - 1
    words = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        token = line.split()[0]
        try:
            words.append(int(token, 16) & mask)
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
    """Bit-exact comparison. Returns {"match", "detail"}; detail names the first mismatch."""
    mask = (1 << width) - 1
    wrapped_expected = [int(value) & mask for value in expected]
    if len(wrapped_expected) != len(actual):
        return {
            "match": False,
            "detail": f"length mismatch: expected {len(wrapped_expected)}, got {len(actual)}",
        }

    for index, (got, want) in enumerate(zip(actual, wrapped_expected)):
        if got != want:
            return {
                "match": False,
                "detail": (
                    f"first mismatch at index {index}: "
                    f"expected {to_signed(want, width)}, got {to_signed(got, width)}"
                ),
            }
    return {"match": True, "detail": ""}
