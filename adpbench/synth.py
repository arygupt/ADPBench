"""Synthesis via Yosys: RTL -> gate netlist, plus the cell count that stands in for area.

The flow is pinned in `flows/synth.ys` so that cell counts are comparable
across submissions. Changing the flow invalidates every stored baseline.

Synthesis is where the problem's parameters are fixed: every parameter in
`INTERFACE["params"]` is applied with `chparam` before elaboration. The
resulting netlist is the artifact that gets simulated, so the design whose
cells are counted is the design whose behaviour is checked.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from .problem import Problem, repo_root
from .process import run_logged

FLOW = repo_root() / "flows" / "synth.ys"
NETLIST = "netlist.v"

# Yosys changed the `stat` report format over time; accept either.
CELL_COUNT_PATTERNS = (
    re.compile(r"Number of cells:\s+(\d+)"),
    re.compile(r"^\s*(\d+)\s+cells\s*$", re.MULTILINE),
)
STATS_MARKER = re.compile(r"Printing statistics", re.MULTILINE)
CELL_TYPE_LINE = re.compile(r"^\s*\d+\s+(\\?\$[A-Za-z0-9_$]+)\s*$", re.MULTILINE)


def build_script(problem: Problem, rtl_paths: list[Path], workdir: Path) -> Path:
    """Fill in the pinned flow template and write it to `workdir/synth.ys`."""
    reads = "\n".join(f"read_verilog -sv {Path(p).resolve()}" for p in rtl_paths)
    chparams = "\n".join(
        f"chparam -set {name} {value} {problem.top}" for name, value in problem.params.items()
    )
    script = FLOW.read_text().format(reads=reads, chparams=chparams, top=problem.top)
    path = Path(workdir).resolve() / "synth.ys"
    path.write_text(script)
    return path


def synthesize(
    problem: Problem,
    rtl_paths: list[Path],
    workdir: Path,
    deadline_at: float | None = None,
) -> dict:
    """Synthesize the design. Returns {"ok", "cells", "netlist", "log"}.

    `ok` means the design elaborated at the problem's parameters, reduced to
    gate-level cells, and produced the netlist used for simulation. On
    failure, `cells` is -1, `netlist` is None, and the log explains why.
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    script = build_script(problem, rtl_paths, workdir)
    netlist = workdir / NETLIST
    netlist.unlink(missing_ok=True)

    proc = run_logged(["yosys", str(script)], workdir, "yosys", deadline_at=deadline_at)
    log = proc["log"]
    if proc["returncode"] != 0 or proc["reason"]:
        return _failed(log, failure_kind=proc["reason"], returncode=proc["returncode"])

    cells = _parse_cells(log)
    if cells is None:
        return _failed(log + "\n[adpbench] no cell count in yosys stat output")
    if not netlist.is_file():
        return _failed(log + f"\n[adpbench] flow produced no {NETLIST}")

    disallowed = _disallowed_cells(CELL_TYPE_LINE.findall(log))
    if disallowed:
        return _failed(
            log + "\n[adpbench] design did not reduce to gate-level cells: " + ", ".join(disallowed)
        )

    return {"ok": True, "cells": cells, "netlist": netlist, "log": log}


def _failed(log: str, **extra) -> dict:
    return {"ok": False, "cells": -1, "netlist": None, "log": log, **extra}


def _parse_cells(log: str) -> int | None:
    """Cell count for the flattened top module.

    Yosys omits zero-count rows, and an empty module produces no stats section
    at all. Since this is only called after a clean exit, a missing count after
    the stats pass means the design reduced to nothing - which is a real,
    if useless, netlist rather than a synthesis failure.
    """
    for pattern in CELL_COUNT_PATTERNS:
        matches = pattern.findall(log)
        if matches:
            return int(matches[-1])
    if STATS_MARKER.search(log):
        return 0
    return None


def _disallowed_cells(cell_types: list[str]) -> list[str]:
    """Every cell type that is not a gate-level internal cell (`$_AND_`, ...).

    The pinned flow is expected to techmap arithmetic to gate-level cells. A
    residual `$mul`, `$div`, `$mem`, or a blackbox instance still simulates
    (simlib has behavioural models) but counts as one cell - so it must be
    rejected, not scored.
    """
    return sorted({cell for cell in cell_types if not cell.lstrip("\\").startswith("$_")})


@lru_cache(maxsize=1)
def tool_version() -> str:
    """First line of `yosys -V`, or "unknown"."""
    try:
        proc = subprocess.run(["yosys", "-V"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    return lines[0] if lines else "unknown"
