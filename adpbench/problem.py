"""Loading and validating ADPBench problem specifications."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

REQUIRED_ATTRS = ("Model", "get_inputs", "INTERFACE", "QUANT")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@dataclass
class Problem:
    name: str
    root: Path
    model_cls: Any
    get_inputs: Callable[..., tuple]
    interface: dict
    quant: dict

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
    def output_port(self) -> str:
        return self.interface.get("output", "out_c")

    @property
    def out_len(self) -> int:
        return int(self.interface.get("out_len", 1))

    @property
    def baseline_rtl(self) -> Path:
        return self.root / "baseline.v"

    @property
    def baseline_metrics(self) -> Path:
        return self.root / "baseline.json"

    def reference(self, *inputs) -> np.ndarray:
        out = self.model_cls().forward(*inputs)
        return np.atleast_1d(np.asarray(out))


def load_problem(root: str | Path) -> Problem:
    root = Path(root).resolve()
    spec = root / "dut.py"
    if not spec.is_file():
        raise FileNotFoundError(f"no dut.py in {root}")

    mod = importlib.util.spec_from_file_location(f"adpbench_problem_{root.name}", spec)
    if mod is None or mod.loader is None:
        raise ImportError(f"cannot load {spec}")
    module = importlib.util.module_from_spec(mod)
    mod.loader.exec_module(module)

    missing = [a for a in REQUIRED_ATTRS if not hasattr(module, a)]
    if missing:
        raise AttributeError(f"{spec} is missing: {', '.join(missing)}")

    interface = dict(module.INTERFACE)
    for key in ("params", "inputs"):
        if key not in interface:
            raise KeyError(f"{spec}: INTERFACE needs a '{key}' entry")

    return Problem(
        name=root.name,
        root=root,
        model_cls=module.Model,
        get_inputs=module.get_inputs,
        interface=interface,
        quant=dict(module.QUANT),
    )
