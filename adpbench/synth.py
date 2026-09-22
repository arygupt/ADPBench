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

# Yosys changed the `stat` report format; accept either.
CELL_RES = (
    re.compile(r"Number of cells:\s+(\d+)"),
    re.compile(r"^\s*(\d+)\s+cells\s*$", re.MULTILINE),
)
STATS_MARKER = re.compile(r"Printing statistics", re.MULTILINE)
CELL_TYPE_RE = re.compile(r"^\s*\d+\s+(\\?\$[A-Za-z0-9_$]+)\s*$", re.MULTILINE)
FLOW = repo_root() / "flows" / "synth.ys"
NETLIST = "netlist.v"


def _parse_cells(log: str) -> int | None:
    """Cell count for the flattened top module.

    Yosys omits zero-count rows, and an empty module produces no stats section
    at all. Since this is only called after a clean exit, a missing count after
    the stats pass means the design reduced to nothing - which is a real,
    if useless, netlist rather than a synthesis failure.
    """
    for pattern in CELL_RES:
        matches = pattern.findall(log)
        if matches:
            return int(matches[-1])
    if STATS_MARKER.search(log):
        return 0
    return None


def _parse_cell_types(log: str) -> list[str]:
    """Cell types in the final stat pass, in report order."""
    return CELL_TYPE_RE.findall(log)


def _disallowed_cells(types: list[str]) -> list[str]:
    """Anything that is not a gate-level internal cell.

    The pinned flow is expected to techmap arithmetic to `$_AND_`-style cells.
    A residual `$mul`, `$div`, `$mem`, or a blackbox instance still simulates
    (simlib has behavioural models) but counts as one cell - so it must be
    rejected, not scored.
    """
    return sorted(
        {cell for cell in types if not cell.lstrip("\\").startswith("$_")}
    )


def _build_script(problem: Problem, rtl_paths: list[Path], workdir: Path) -> Path:
    reads = "\n".join(f"read_verilog -sv {Path(p).resolve()}" for p in rtl_paths)
    chparams = "\n".join(
        f"chparam -set {name} {value} {problem.top}"
        for name, value in problem.params.items()
    )
    script = FLOW.read_text().format(
        reads=reads,
        chparams=chparams,
        top=problem.top,
    )
    path = Path(workdir).resolve() / "synth.ys"
    path.write_text(script)
    return path


def synthesize(problem: Problem, rtl_paths: list[Path], workdir: Path) -> dict:
    """Returns {ok, cells, netlist, log}.

    `ok` means the design elaborated at the problem's parameters, synthesized,
    and produced the netlist used for simulation.
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    script = _build_script(problem, rtl_paths, workdir)
    netlist = workdir / NETLIST
    if netlist.exists():
        netlist.unlink()
    proc = run_logged(["yosys", str(script)], workdir, "yosys")
    log = proc["log"]
    if proc["returncode"] != 0 or proc["reason"]:
        return {"ok": False, "cells": -1, "netlist": None, "log": log,
                "failure_kind":proc["reason"], "returncode":proc["returncode"]}

    cells = _parse_cells(log)
    if cells is None:
        return {
            "ok": False,
            "cells": -1,
            "netlist": None,
            "log": log + "\n[adpbench] no cell count in yosys stat output",
        }
    if not netlist.is_file():
        return {
            "ok": False,
            "cells": -1,
            "netlist": None,
            "log": log + f"\n[adpbench] flow produced no {NETLIST}",
        }

    disallowed = _disallowed_cells(_parse_cell_types(log))
    if disallowed:
        return {
            "ok": False,
            "cells": -1,
            "netlist": None,
            "log": log
            + "\n[adpbench] design did not reduce to gate-level cells: "
            + ", ".join(disallowed),
        }

    return {"ok": True, "cells": cells, "netlist": netlist, "log": log}


@lru_cache(maxsize=1)
def tool_version() -> str:
    try:
        proc = subprocess.run(
            ["yosys", "-V"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    output = (proc.stdout + proc.stderr).strip().splitlines()
    return output[0] if output else "unknown"
