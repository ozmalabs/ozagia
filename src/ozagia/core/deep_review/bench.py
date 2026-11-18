"""Explicit, bounded runner for caller-supplied benchmark commands.

This module is deliberately not wired into collection or the CLI. Calling
``run_bench(..., enabled=True)`` is the opt-in boundary. Commands are argument
vectors (never shell strings); callers are responsible for choosing commands
that are safe to run against their checkout.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from ozagia.core.security import redact_text

MAX_COMMANDS = 8
MAX_ARGUMENTS = 64
MAX_ARGUMENT_CHARS = 4096
MAX_TIMEOUT_S = 300.0
DEFAULT_OUTPUT_LIMIT = 16 * 1024
MAX_OUTPUT_LIMIT = 64 * 1024


def _bounded_output(pipe: Any, limit: int, result: dict[str, Any], key: str) -> None:
    kept = bytearray()
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            room = max(0, limit - len(kept))
            if room:
                kept.extend(chunk[:room])
            if len(chunk) > room:
                result[f"{key}_truncated"] = True
    except (OSError, ValueError):
        result[f"{key}_read_error"] = True
    finally:
        result[key] = redact_text(kept.decode("utf-8", errors="replace"))
        try:
            pipe.close()
        except OSError:
            pass


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Stop the command and, on POSIX, its process group."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - exercised on Windows CI
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - exercised on Windows CI
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.SubprocessError):
            pass


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Stop descendants that outlive the command while keeping its pipes open."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows CI
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _validate_commands(commands: Sequence[Sequence[str]]) -> list[list[str]]:
    if isinstance(commands, (str, bytes)) or not isinstance(commands, Sequence):
        raise ValueError("commands must be a sequence of argument vectors")
    if not 1 <= len(commands) <= MAX_COMMANDS:
        raise ValueError(f"commands must contain 1 to {MAX_COMMANDS} entries")
    clean: list[list[str]] = []
    for index, argv in enumerate(commands):
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
            raise ValueError(f"command {index} must be an argument vector")
        if not 1 <= len(argv) <= MAX_ARGUMENTS:
            raise ValueError(f"command {index} must contain 1 to {MAX_ARGUMENTS} arguments")
        if any(not isinstance(arg, str) or not arg or len(arg) > MAX_ARGUMENT_CHARS
               or "\x00" in arg for arg in argv):
            raise ValueError(f"command {index} contains an invalid argument")
        clean.append(list(argv))
    return clean


def run_bench(root: str | Path, commands: Sequence[Sequence[str]], *,
              enabled: bool = False, timeout_s: float = 30.0,
              output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT,
              labels: Sequence[str] | None = None,
              budget_s: float | None = None) -> dict[str, Any]:
    """Run explicit argv commands and return bounded, redacted measurements.

    Each command gets its own timeout. Stdout and stderr are drained while
    retaining at most ``output_limit_bytes`` each. No shell is used, stdin is
    closed, and POSIX child processes share a killable process group.
    """
    if enabled is not True:
        raise ValueError("benchmark execution requires enabled=True")
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not 0 < timeout_s <= MAX_TIMEOUT_S):
        raise ValueError(f"timeout_s must be greater than 0 and at most {MAX_TIMEOUT_S}")
    if (isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int)
            or not 1 <= output_limit_bytes <= MAX_OUTPUT_LIMIT):
        raise ValueError(f"output_limit_bytes must be between 1 and {MAX_OUTPUT_LIMIT}")
    if (budget_s is not None and
            (isinstance(budget_s, bool) or not isinstance(budget_s, (int, float))
             or not 0 <= budget_s < float("inf"))):
        raise ValueError("budget_s must be >= 0")
    argv_list = _validate_commands(commands)
    if labels is not None:
        if (isinstance(labels, (str, bytes)) or not isinstance(labels, Sequence)
                or len(labels) != len(argv_list)
                or any(not isinstance(label, str) or not label.strip() for label in labels)):
            raise ValueError("labels must provide one non-empty string per command")
    cwd = Path(root).resolve()
    if not cwd.is_dir():
        raise ValueError("root must be a readable directory")

    started_all = time.monotonic()
    results: list[dict[str, Any]] = []
    for index, argv in enumerate(argv_list):
        remaining_budget = (None if budget_s is None else
                            float(budget_s) - (time.monotonic() - started_all))
        if remaining_budget is not None and remaining_budget <= 0:
            results.extend({
                "label": redact_text((labels[i] if labels is not None else
                                      Path(argv_list[i][0]).name)[:120]),
                "status": "budget_exhausted", "returncode": None,
                "duration_ms": 0, "stdout": "", "stderr": "",
                "stdout_truncated": False, "stderr_truncated": False,
            } for i in range(index, len(argv_list)))
            break
        label = labels[index] if labels is not None else Path(argv[0]).name
        row: dict[str, Any] = {
            "label": redact_text(label[:120]), "status": "failed",
            "returncode": None, "duration_ms": 0,
            "stdout": "", "stderr": "", "stdout_truncated": False,
            "stderr_truncated": False,
        }
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                start_new_session=(os.name == "posix"), close_fds=True)
        except (OSError, ValueError) as exc:
            row["error"] = type(exc).__name__
            row["duration_ms"] = int((time.monotonic() - started) * 1000)
            results.append(row)
            continue

        readers: list[threading.Thread] = []
        for stream_name in ("stdout", "stderr"):
            pipe = getattr(process, stream_name)
            thread = threading.Thread(
                target=_bounded_output, args=(pipe, output_limit_bytes, row, stream_name),
                daemon=True)
            readers.append(thread)
            thread.start()
        timed_out = False
        try:
            command_timeout = float(timeout_s)
            if remaining_budget is not None:
                command_timeout = min(command_timeout, remaining_budget)
            process.wait(timeout=command_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop(process)
        for thread in readers:
            thread.join(timeout=0.5)
        # A command can exit after spawning a background child that inherited
        # stdout/stderr. Such a child otherwise keeps daemon reader threads
        # alive indefinitely and escapes the per-command process lifecycle.
        if any(thread.is_alive() for thread in readers):
            _stop_group(process)
            row["orphaned_process_group_stopped"] = True
            for thread in readers:
                thread.join(timeout=0.5)
        if any(thread.is_alive() for thread in readers):
            row["output_incomplete"] = True
        row["returncode"] = process.returncode
        row["status"] = "timed_out" if timed_out else (
            "completed" if process.returncode == 0 else "failed")
        row["duration_ms"] = int((time.monotonic() - started) * 1000)
        results.append(row)
    return {
        "schema": "ozagia.deep_review.bench.v1",
        "root": str(cwd),
        "timeout_s_per_command": float(timeout_s),
        "budget_s": None if budget_s is None else float(budget_s),
        "elapsed_s": time.monotonic() - started_all,
        "output_limit_bytes_per_stream": output_limit_bytes,
        "commands": results,
        "limitations": [
            "Commands were supplied by the caller and can modify the checkout.",
            "Elapsed time is a single-run observation, not a performance verdict.",
            "Captured output is capped and redacted; omitted output may contain context.",
        ],
    }


__all__ = ["run_bench", "MAX_COMMANDS", "MAX_TIMEOUT_S", "MAX_OUTPUT_LIMIT"]
