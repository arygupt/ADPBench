"""Evaluation result record, modelled on KernelBench's KernelExecResult.

Stages are recorded independently so a failure tells you *where* it failed:
`compiled` (parses and simulates), `synthesizable` (becomes gates), `correct`
(matches the golden model). Only a correct design gets a score.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class EvalResult:
    problem: str = ""
    source: str = ""

    compiled: bool = False
    synthesizable: bool = False
    correct: bool = False

    cells: int = -1
    cycles: int = -1
    adp: float = -1.0
    ratio: float = -1.0

    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metadata"] = _jsonable(self.metadata)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def summary(self) -> str:
        if not self.synthesizable:
            return "FAIL  synthesis"
        if not self.correct:
            return f"FAIL  {self.metadata.get('correctness', 'incorrect')}"
        return f"PASS  {self.ratio:.2f}x  ({self.cells} cells, {self.cycles} cycles)"


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
