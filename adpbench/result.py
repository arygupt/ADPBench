"""Evaluation result record for RTL correctness and area-delay measurements.

Stages are recorded independently so a failure tells you *where* it failed:
`compiled` (parses and simulates), `synthesizable` (becomes gates), `correct`
(matches the golden model). Only a correct design gets a score.

`metadata` holds everything else: the failing stage, logs, per-case details.
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
        data = asdict(self)
        data["metadata"] = _jsonable(self.metadata)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _jsonable(value):
    """A copy of `value` that json.dumps accepts; Paths and the like become strings."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
