"""Timing-aware area-delay product. Experimental: not the headline score.

The headline score is `cells * cycles`. It ignores how much logic sits
between registers, so a design that finishes in few cycles with a long
combinational path looks fast even though it could only run at a slow clock.
This pass measures the same synthesized design and adds two things:

    area   = Nangate45 X1 area of each generic cell, summed (um^2)
    depth  = longest register-to-register path, in gate levels (`ltp -noff`)
    period = depth + REGISTER_LEVELS
    timed_adp = area * cycles * period

Depth is unit-delay: every gate counts as one level. The flow still skips
ABC (see flows/synth.ys), so the gates are generic and unoptimized, and these
numbers rank designs rather than predict silicon.

It re-runs the pinned synthesis script with `ltp` appended, so the measured
netlist is the scored one, and it leaves flows/synth.ys and every recorded
baseline unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import synth
from .problem import Problem
from .process import run_logged

# Nangate45 X1 cell areas in um^2 (NangateOpenCellLibrary_typical.lib).
INV, AND2, OR2, XOR2, MUX2, DFF, DFFR = 0.532, 1.064, 1.064, 1.596, 1.862, 4.522, 5.320
GATE_AREA = {
    "$_NOT_": INV,
    "$_AND_": AND2,
    "$_OR_": OR2,
    "$_XOR_": XOR2,
    "$_XNOR_": XOR2,
    "$_NAND_": 0.798,
    "$_NOR_": 0.798,
    "$_ANDNOT_": AND2 + INV,
    "$_ORNOT_": OR2 + INV,
    "$_MUX_": MUX2,
}
# Yosys flip-flop names: $_DFF_P_, $_DFFE_PN0P_, $_SDFFCE_PP1P_, ...
FLOP = re.compile(r"\$_(S?)DFF(C?E?)_([NP]+)([01]?)([NP]?)_")

# Flip-flop clock-to-Q plus setup, in gate levels. In Nangate45 that is about
# 120 ps against about 40 ps for an AND2, OR2, or XOR2.
REGISTER_LEVELS = 3

CELL_COUNT = re.compile(r"^\s*(\d+)\s+(\$_\w+)\s*$", re.MULTILINE)
LONGEST_PATH = re.compile(r"Longest topological path in \S+ \(length=(\d+)\)")


def cell_area(cell: str) -> float | None:
    """Area of one generic cell, or None for a type with no priced equivalent.

    An enable adds a mux in front of the flip-flop and a synchronous
    reset or set adds a gate; an asynchronous one uses the resettable flop.
    """
    if cell in GATE_AREA:
        return GATE_AREA[cell]
    match = FLOP.fullmatch(cell)
    if not match:
        return None
    sync, enable, _, init, _ = match.groups()
    total = DFFR if init and not sync else DFF
    if sync:
        total += AND2
    if enable:
        total += MUX2
    return total


def area(cells: dict[str, int]) -> float:
    """Total area of a cell-type histogram. Raises ValueError on an unpriced type."""
    unpriced = sorted(cell for cell in cells if cell_area(cell) is None)
    if unpriced:
        raise ValueError("no area for cell types: " + ", ".join(unpriced))
    return sum(count * cell_area(cell) for cell, count in cells.items())


def period(depth: int) -> int:
    """Clock period in gate levels: the logic plus the register overhead."""
    return depth + REGISTER_LEVELS


def timed_adp(area_um2: float, cycles: int, depth: int) -> float:
    """area * cycles * clock period, or -1 when a value is missing."""
    if area_um2 <= 0 or cycles <= 0 or depth < 0:
        return -1.0
    return area_um2 * cycles * period(depth)


def parse(log: str) -> dict:
    """Cell histogram and logic depth from a synthesis log with `ltp -noff` appended."""
    stats = synth.STATS_MARKER.search(log)
    cells = {}
    if stats:
        cells = {cell: int(count) for count, cell in CELL_COUNT.findall(log, stats.start())}
    depth = LONGEST_PATH.search(log)
    return {"cells": cells, "depth": int(depth.group(1)) if depth else None}


def measure(
    problem: Problem,
    rtl_paths: list[Path],
    workdir: Path,
    deadline_at: float | None = None,
) -> dict:
    """Synthesize with the pinned flow and measure area and depth.

    Returns {"ok", "cells", "area", "depth", "log"}; on failure `ok` is False
    and `error` says why.
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    script = synth.build_script(problem, rtl_paths, workdir)
    script.write_text(script.read_text().rstrip("\n") + "\nltp -noff\n")

    proc = run_logged(["yosys", str(script)], workdir, "yosys", deadline_at=deadline_at)
    log = proc["log"]
    if proc["returncode"] != 0 or proc["reason"]:
        return {"ok": False, "error": proc["reason"] or "yosys failed", "log": log}
    measured = parse(log)
    if not measured["cells"] or measured["depth"] is None:
        return {"ok": False, "error": "no cell statistics or longest path in yosys output", "log": log}
    try:
        total = area(measured["cells"])
    except ValueError as error:
        return {"ok": False, "error": str(error), "log": log}
    return {"ok": True, **measured, "area": total, "log": log}
