"""Atomic, crash-resistant JSON writes for harness-owned progress and result files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_json(path: Path, data: dict) -> None:
    """Write `data` as JSON to `path` so readers see either the old file or the new one.

    The JSON goes to a temporary file in the same directory, is flushed to
    disk, and then renamed over `path`. A crash at any point leaves the
    previous version intact. NaN and infinity are rejected.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        # Only exists if something failed before the rename.
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_directory(directory: Path) -> None:
    """Make the rename itself durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
