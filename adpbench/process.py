"""Bounded tool execution with on-disk logs, process-tree cleanup and progress."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .durable import atomic_json

DEADLINE = ContextVar("adpbench_deadline", default=None)
LOG_LIMIT = 16 * 1024 * 1024


@contextmanager
def deadline(seconds: float | None):
    end = time.monotonic() + seconds if seconds is not None else DEADLINE.get()
    previous = DEADLINE.get()
    if previous is not None and end is not None:
        end = min(previous, end)
    token = DEADLINE.set(end)
    try:
        yield
    finally:
        DEADLINE.reset(token)


def stop_tree(proc: subprocess.Popen) -> None:
    # Each tool owns a new process group; never signal the caller's group.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
    proc.wait(timeout=5)


def run_logged(args: list[str], cwd: Path, name: str, timeout: float = 600,
               log_limit: int = LOG_LIMIT) -> dict:
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    log_path = cwd / f"{name}.log"
    status_path = cwd / f"{name}.status.json"
    start = time.monotonic()
    end = start + timeout
    if DEADLINE.get() is not None:
        end = min(end, DEADLINE.get())
    state = {"state":"running", "tool":args[0], "returncode":None,
             "timed_out":False, "reason":"", "log_file":log_path.name}
    atomic_json(status_path, state)
    proc = None
    size = 0
    with log_path.open("wb", buffering=0) as log:
        try:
            if time.monotonic() >= end:
                state.update(timed_out=True, reason="wall_timeout")
            else:
                proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        if time.monotonic() >= end:
                            state.update(timed_out=True, reason="wall_timeout")
                            stop_tree(proc)
                            break
                        for key, _ in selector.select(timeout=min(0.1, max(0, end-time.monotonic()))):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            allowed = max(0, log_limit - size)
                            log.write(chunk[:allowed])
                            size += len(chunk)
                            if size > log_limit:
                                state["reason"] = "log_limit"
                                stop_tree(proc)
                                break
                        if state["reason"]:
                            break
                # A tool can close stdout without exiting; the deadline still applies.
                try:
                    proc.wait(timeout=max(0.001, end-time.monotonic()))
                except subprocess.TimeoutExpired:
                    state.update(timed_out=True, reason="wall_timeout")
                    stop_tree(proc)
                state["returncode"] = proc.returncode
                if proc.returncode and not state["reason"]:
                    state["reason"] = "signal" if proc.returncode < 0 else "tool_error"
        except OSError as exc:
            state["reason"] = f"launch_error:{type(exc).__name__}"
        except BaseException:
            state["reason"] = "interrupted"
            raise
        finally:
            if proc is not None:
                stop_tree(proc)  # Also terminate descendants whose parent closed stdout and exited.
                proc.stdout.close()
                state["returncode"] = proc.returncode
            state.update(state="completed", duration_s=round(time.monotonic()-start, 3), log_bytes=min(size,log_limit))
            atomic_json(status_path, state)
    state["log"] = log_path.read_text(errors="replace")
    if state["reason"]:
        state["log"] += f"\n[adpbench] {name}: {state['reason']} (exit {state['returncode']})\n"
    return state
