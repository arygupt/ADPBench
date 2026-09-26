"""Running an external tool (yosys, iverilog, vvp, docker) with limits.

`run_logged` streams the tool's combined stdout/stderr into `<name>.log` and
its progress into `<name>.status.json` in the working directory, so a killed
or interrupted run still leaves evidence on disk.

Each tool runs in its own process group. When the tool times out, exceeds the
log limit, or finishes, the whole group is stopped - including any children
it spawned and left behind.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .durable import atomic_json

LOG_LIMIT = 16 * 1024 * 1024
READ_SIZE = 64 * 1024

# `reason` values that mean the tool was stopped from outside (a time or size
# limit, a signal, an interrupt), not that the design under test was wrong.
INFRASTRUCTURE_REASONS = ("wall_timeout", "signal", "log_limit", "interrupted")


def is_infrastructure_failure(reason: str) -> bool:
    return reason in INFRASTRUCTURE_REASONS or reason.startswith("launch_error")


def stop_process_group(process: subprocess.Popen) -> None:
    """Terminate, then kill, the process group a tool was started in.

    Only groups created by `run_logged` (via start_new_session) are signalled;
    the caller's own process group is never touched.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break  # the group is already gone
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
    process.wait(timeout=5)


def run_logged(
    args: list[str],
    cwd: Path,
    name: str,
    timeout: float = 600,
    log_limit: int = LOG_LIMIT,
    deadline_at: float | None = None,
) -> dict:
    """Run a tool and return its status.

    The tool is stopped after `timeout` seconds, or at `deadline_at` (an
    absolute `time.monotonic()` value shared by several tools), whichever
    comes first. It is also stopped if it writes more than `log_limit` bytes.

    The returned dict has:
        returncode   the exit code, or None if it never started
        timed_out    True if a time limit stopped it
        reason       "" on success, otherwise one of: wall_timeout, log_limit,
                     signal, tool_error, launch_error:<Exception>, interrupted
        log          the log text, with a trailing note when `reason` is set
    plus state, tool, log_file, duration_s and log_bytes.
    """
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    log_path = cwd / f"{name}.log"
    status_path = cwd / f"{name}.status.json"

    started = time.monotonic()
    end = started + timeout
    if deadline_at is not None:
        end = min(end, deadline_at)

    status = {
        "state": "running",
        "tool": args[0],
        "returncode": None,
        "timed_out": False,
        "reason": "",
        "log_file": log_path.name,
    }
    atomic_json(status_path, status)

    process = None
    with log_path.open("wb", buffering=0) as log:
        try:
            if time.monotonic() >= end:
                status.update(timed_out=True, reason="wall_timeout")
            else:
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                _copy_output_to_log(process, log, end, log_limit, status)
                _wait_for_exit(process, end, status)
        except OSError as exc:
            status["reason"] = f"launch_error:{type(exc).__name__}"
        except BaseException:
            status["reason"] = "interrupted"
            raise
        finally:
            if process is not None:
                # Also stops descendants that outlived the tool itself.
                stop_process_group(process)
                process.stdout.close()
                status["returncode"] = process.returncode
            status.update(
                state="completed",
                duration_s=round(time.monotonic() - started, 3),
                log_bytes=log.tell(),
            )
            atomic_json(status_path, status)

    status["log"] = log_path.read_text(errors="replace")
    if status["reason"]:
        status["log"] += f"\n[adpbench] {name}: {status['reason']} (exit {status['returncode']})\n"
    return status


def _copy_output_to_log(
    process: subprocess.Popen, log, end: float, log_limit: int, status: dict
) -> None:
    """Copy the tool's output into `log` until it closes its output.

    Stops the tool early (recording the reason in `status`) when the time
    limit passes or the output exceeds `log_limit`. Output beyond the limit is
    not written.
    """
    produced = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = end - time.monotonic()
            if remaining <= 0:
                status.update(timed_out=True, reason="wall_timeout")
                stop_process_group(process)
                return

            for key, _ in selector.select(timeout=min(0.1, remaining)):
                chunk = os.read(key.fd, READ_SIZE)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                log.write(chunk[: max(0, log_limit - produced)])
                produced += len(chunk)
                if produced > log_limit:
                    status["reason"] = "log_limit"
                    stop_process_group(process)
                    return


def _wait_for_exit(process: subprocess.Popen, end: float, status: dict) -> None:
    """Wait for the tool to exit and classify a nonzero exit code.

    A tool can close its output without exiting, so the time limit still
    applies here.
    """
    try:
        process.wait(timeout=max(0.001, end - time.monotonic()))
    except subprocess.TimeoutExpired:
        status.update(timed_out=True, reason="wall_timeout")
        stop_process_group(process)
    status["returncode"] = process.returncode
    if process.returncode and not status["reason"]:
        status["reason"] = "signal" if process.returncode < 0 else "tool_error"
