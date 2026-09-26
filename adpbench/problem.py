"""Loading and validating ADPBench problem specifications.

A problem is a directory `problems/level<N>/<name>/` containing `dut.py`, the
executable specification. `dut.py` must define:

    Model        a class whose `forward(*inputs)` is the NumPy golden reference
    get_inputs   a function returning one transaction's inputs (uses np.random)
    INTERFACE    module name, parameters, input ports and their lengths, ...
    QUANT        the exact integer widths and arithmetic contract (for docs)

and may define DIRECTED, a map of edge-case name -> function(transaction)
returning that transaction's inputs.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

REQUIRED_ATTRS = ("Model", "get_inputs", "INTERFACE", "QUANT")
DEFAULT_TRANSACTIONS = 2


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def discover_problems(root: str | Path | None = None) -> list[Path]:
    """Every directory under `problems/` that contains a `dut.py`."""
    base = Path(root) if root else repo_root() / "problems"
    if not base.is_dir():
        return []
    return sorted(path.parent for path in base.glob("*/*/dut.py"))


def find_problem(spec: str) -> Path:
    """Resolve a problem directory, or a name like `001_dot_product`, to its path."""
    candidate = Path(spec)
    if (candidate / "dut.py").is_file():
        return candidate
    matches = [path for path in discover_problems() if path.name == spec or str(path).endswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no problem matching '{spec}'")
    listing = "\n  ".join(str(match) for match in matches)
    raise SystemExit(f"ambiguous problem '{spec}':\n  {listing}")


@dataclass
class Problem:
    name: str
    root: Path
    model_cls: Any
    get_inputs: Callable[..., tuple]
    interface: dict
    quant: dict
    directed: dict[str, Callable[..., tuple]]

    @property
    def top(self) -> str:
        return self.interface.get("module", "dut")

    @property
    def params(self) -> dict:
        return self.interface["params"]

    @property
    def input_ports(self) -> list[str]:
        return list(self.interface["inputs"])

    @property
    def input_lens(self) -> dict[str, int]:
        """Elements delivered per transaction on each input port."""
        lens = self.interface.get("input_lens")
        if not lens:
            raise KeyError(f"{self.name}: INTERFACE needs an 'input_lens' map")
        return {port: int(lens[port]) for port in self.input_ports}

    @property
    def transactions(self) -> int:
        """Back-to-back transactions the testbench drives without reset."""
        return int(self.interface.get("transactions", DEFAULT_TRANSACTIONS))

    @property
    def output_port(self) -> str:
        return self.interface.get("output", "out_c")

    @property
    def out_len(self) -> int:
        """Output words per transaction."""
        return int(self.interface.get("out_len", 1))

    @property
    def directed_cases(self) -> list[str]:
        return list(self.directed)

    @property
    def baseline_rtl(self) -> Path:
        return self.root / "baseline.v"

    @property
    def baseline_metrics(self) -> Path:
        return self.root / "baseline.json"

    def reference(self, *inputs) -> np.ndarray:
        """Run the golden model on one transaction's inputs."""
        output = self.model_cls().forward(*inputs)
        return np.atleast_1d(np.asarray(output))


def load_problem(root: str | Path) -> Problem:
    """Import a problem's `dut.py` and check it defines what the harness needs."""
    root = Path(root).resolve()
    spec_path = root / "dut.py"
    if not spec_path.is_file():
        raise FileNotFoundError(f"no dut.py in {root}")

    module = _import_file(spec_path, module_name=f"adpbench_problem_{root.name}")

    missing = [name for name in REQUIRED_ATTRS if not hasattr(module, name)]
    if missing:
        raise AttributeError(f"{spec_path} is missing: {', '.join(missing)}")

    interface = dict(module.INTERFACE)
    for key in ("params", "inputs", "input_lens"):
        if key not in interface:
            raise KeyError(f"{spec_path}: INTERFACE needs a '{key}' entry")

    directed = dict(getattr(module, "DIRECTED", {}))
    for name, generator in directed.items():
        if not callable(generator):
            raise TypeError(f"{spec_path}: DIRECTED[{name!r}] is not callable")

    problem = Problem(
        name=root.name,
        root=root,
        model_cls=module.Model,
        get_inputs=module.get_inputs,
        interface=interface,
        quant=dict(module.QUANT),
        directed=directed,
    )

    for port in problem.input_ports:
        if port not in interface["input_lens"]:
            raise KeyError(f"{spec_path}: input_lens is missing port {port!r}")
        if int(interface["input_lens"][port]) <= 0:
            raise ValueError(f"{spec_path}: input_lens[{port!r}] must be positive")
    if problem.transactions < 1:
        raise ValueError(f"{spec_path}: transactions must be at least 1")

    return problem


def _import_file(path: Path, module_name: str):
    """Import a Python file that is not on sys.path."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
