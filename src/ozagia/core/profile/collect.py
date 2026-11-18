"""Opt-in guided profiler collection.

Collection is deliberately separate from the read-only ingest path. A caller
must pass an explicit execution flag, a non-empty argv list, a working directory
inside the repository and a bounded timeout. Before the child starts, the run
uses the existing resource policy for admission (requested/expected disk,
absolute reserve and host floor) and applies the existing memory/file rlimits.
The command is never passed through a shell.

The collected artifact is then handed to :func:`ozagia.core.profile.ingest`,
so the resulting report has the same schema and graph join as an artifact the
user produced manually.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ozagia.core import resources as R

from .ingest import ingest_file
from .model import ProfileError, bounded_int, bounded_text, file_digest

TOOLS = ("cprofile", "pyspy", "node")
MAX_TIMEOUT_S = 3_600
MAX_COMMAND_ITEMS = 128
MAX_ARG_TEXT = 2_000
DEFAULT_FILE_MB = 64
COLLECT_SCHEMA = 1


class CollectionError(ProfileError):
    """The guided run could not start or did not produce an artifact."""


class CollectionRefused(CollectionError):
    """The run was not allowed or failed resource admission."""

    def __init__(self, message: str,
                 report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = dict(report) if report is not None else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_argv(argv: Sequence[str]) -> list[str]:
    """Bound argv and hide values that look like credentials."""
    secret_flags = ("token", "secret", "password", "passwd", "api-key",
                    "apikey", "authorization", "cookie")
    out: list[str] = []
    hide_next = False
    for index, item in enumerate(argv):
        text = str(item)
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if len(out) >= MAX_COMMAND_ITEMS:
            out.append("...")
            break
        text = bounded_text(text, name="argument", limit=MAX_ARG_TEXT,
                            allow_empty=True)
        name, separator, _value = text.partition("=")
        if separator and any(flag in name.lower() for flag in secret_flags):
            out.append(f"{name}=***")
            continue
        out.append(text)
        if text.startswith("-") and any(flag in text.lower()
                                        for flag in secret_flags):
            hide_next = True
    return out


def _command_key(argv: Sequence[str]) -> str:
    try:
        return R.command_key(argv)
    except (TypeError, ValueError):
        return "cmd:unknown"


def _tool_version(argv: Sequence[str], *, cwd: Path,
                  env: Mapping[str, str]) -> str | None:
    if not argv:
        return None
    try:
        result = subprocess.run(
            [argv[0], "--version"], cwd=str(cwd), env=dict(env),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return bounded_text(text[0], name="tool version", limit=200,
                        allow_empty=True) if text else None


def _python_interpreter(command: Sequence[str],
                        explicit: str | None) -> tuple[str, list[str]]:
    if command:
        first = Path(command[0]).name.lower()
        if first == "python" or first.startswith("python3") or first.startswith("python."):
            return (explicit or shutil.which(command[0]) or sys.executable,
                    list(command[1:]))
    return explicit or sys.executable, list(command)


def _pyspy_command(command: Sequence[str], artifact_dir: Path,
                   *, explicit: str | None,
                   rate: int | None) -> tuple[list[str], Path]:
    path = explicit or shutil.which("py-spy")
    if not path:
        raise CollectionError(
            "py-spy is not installed or not on PATH; install it or pass --tool-path")
    if not command:
        raise CollectionError("py-spy needs a command after --")
    artifact = artifact_dir / "profile.speedscope"
    argv = [path, "record", "-f", "speedscope", "-o", str(artifact)]
    if rate is not None:
        argv.extend(["--rate", str(rate)])
    argv.extend(["--", *command])
    return argv, artifact


def _node_command(command: Sequence[str], artifact_dir: Path,
                  *, explicit: str | None) -> tuple[list[str], Path]:
    path = explicit or shutil.which("node")
    if not path:
        raise CollectionError(
            "node is not installed or not on PATH; install it or pass --tool-path")
    rest = list(command)
    if rest and Path(rest[0]).name.lower() in ("node", "nodejs"):
        rest = rest[1:]
    if not rest:
        raise CollectionError("node needs a script or module after --")
    artifact = artifact_dir / "profile.cpuprofile"
    argv = [path, "--cpu-prof", "--cpu-prof-dir", str(artifact_dir),
            "--cpu-prof-name", artifact.name, *rest]
    return argv, artifact


def build_command(tool: str, command: Sequence[str], artifact_dir: str | Path,
                  *, tool_path: str | None = None,
                  rate: int | None = None) -> tuple[list[str], Path]:
    """Build the profiler argv and the expected artifact path.

    No shell is involved. The returned command is the exact argv that will be
    executed after the caller's admission and timeout checks.
    """
    if tool not in TOOLS:
        raise CollectionError(f"unknown collection tool {tool!r}")
    if not command:
        raise CollectionError("a command is required after --")
    if len(command) > MAX_COMMAND_ITEMS:
        raise CollectionError(f"command has more than {MAX_COMMAND_ITEMS} items")
    for item in command:
        if not isinstance(item, str) or "\x00" in item:
            raise CollectionError("every command item must be a string without NUL")
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if tool == "pyspy":
        return _pyspy_command(command, artifact_dir, explicit=tool_path, rate=rate)
    if tool == "node":
        if rate is not None:
            raise CollectionError("--rate is only supported by py-spy")
        return _node_command(command, artifact_dir, explicit=tool_path)
    if rate is not None:
        raise CollectionError("--rate is only supported by py-spy")
    interpreter, rest = _python_interpreter(command, tool_path)
    if not rest:
        raise CollectionError("cProfile needs a script or -m module after --")
    if rest[0] == "-c":
        raise CollectionError("cProfile collection does not support -c; use a script or -m module")
    artifact = artifact_dir / "profile.prof"
    return [interpreter, "-m", "cProfile", "-o", str(artifact), *rest], artifact


def _terminate(proc: subprocess.Popen[Any], grace_s: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _admission_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: report.get(key) for key in (
        "ok", "reason", "shortfall", "retry_after_s", "suggestions",
        "requested", "available", "disabled")}


def run_collection(root: str | Path, *, tool: str, command: Sequence[str],
                   out_dir: str | Path, allow_exec: bool,
                   timeout_s: int = 60, memory_mb: int | None = None,
                   disk_mb: int | None = None, cwd: str | Path | None = None,
                   tool_path: str | None = None, rate: int | None = None,
                   policy: Mapping[str, Any] | None = None,
                   format: str = "auto",
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one explicitly allowed profiler command and ingest its artifact.

    Returns ``(profile, run_record)``. ``CollectionRefused`` is raised before
    any process starts when execution is not allowed or resource admission
    fails. ``CollectionError`` is raised when the artifact is missing.
    """
    if not allow_exec:
        raise CollectionRefused(
            "guided collection needs explicit execution approval; pass --allow-exec")
    timeout_s = bounded_int(timeout_s, name="timeout", low=1,
                            high=MAX_TIMEOUT_S, default=60) or 60
    root = Path(root).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()
    workdir = Path(cwd).resolve() if cwd else root
    try:
        workdir.relative_to(root)
    except ValueError as exc:
        raise CollectionRefused(
            f"collection cwd must stay inside the repository: {workdir}") from exc
    if not workdir.is_dir():
        raise CollectionError(f"collection cwd is not a directory: {workdir}")

    loaded = dict(policy) if policy is not None else R.load_policy(root)
    limits = R.limits_from_policy(loaded, "job")
    if memory_mb is not None:
        if memory_mb < 0:
            raise CollectionError("memory_mb must be >= 0")
        limits = replace(limits, memory_mb=float(memory_mb) or None)
    if disk_mb is not None:
        if disk_mb < 0:
            raise CollectionError("disk_mb must be >= 0")
        limits = replace(limits, disk_mb=float(disk_mb) or None)
    limits = replace(limits, wall_s=float(timeout_s),
                     file_mb=limits.file_mb or DEFAULT_FILE_MB)
    admission = R.check_admission(
        root, kind="job", memory_mb=limits.memory_mb,
        disk_mb=limits.disk_mb, policy=loaded)
    if not admission.get("ok"):
        raise CollectionRefused(
            "resource admission refused the collection run: "
            f"{admission.get('reason')}", report=admission)

    artifact_dir = out_dir / "artifact"
    argv, artifact = build_command(tool, command, artifact_dir,
                                   tool_path=tool_path, rate=rate)
    stdout_path = out_dir / "run.log"
    env = dict(os.environ)
    started_at = _now()
    started = time.monotonic()
    timed_out = False
    version = _tool_version(argv[:1], cwd=workdir, env=env)
    preexec = None
    if os.name == "posix":
        def preexec() -> None:
            R.apply_child_limits(limits, apply_nproc=False)
    with stdout_path.open("wb") as stream:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, close_fds=True,
                start_new_session=True, preexec_fn=preexec)
        except OSError as exc:
            raise CollectionError(f"cannot start profiler: {exc}") from exc
        try:
            returncode = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(proc)
            returncode = proc.returncode
    duration_s = round(time.monotonic() - started, 3)
    if not artifact.is_file():
        raise CollectionError(
            f"{tool} finished without producing {artifact.name}; see {stdout_path}")
    digest, size = file_digest(artifact)
    log_size = stdout_path.stat().st_size if stdout_path.exists() else 0
    run_record = {
        "schema": COLLECT_SCHEMA,
        "tool": tool,
        "tool_version": version,
        "command_key": _command_key(command),
        "argv": _redact_argv(argv),
        "cwd": str(workdir),
        "root": str(root),
        "started_at": started_at,
        "duration_s": duration_s,
        "returncode": returncode,
        "timed_out": timed_out,
        "limits": limits.to_dict(),
        "admission": _admission_summary(admission),
        "artifact": {"path": str(artifact), "sha256": digest, "bytes": size},
        "log": {"path": str(stdout_path), "bytes": log_size},
        "executed": True,
    }
    profile = ingest_file(artifact, root=root, format=format)
    return profile, run_record


__all__ = [
    "COLLECT_SCHEMA", "CollectionError", "CollectionRefused", "MAX_TIMEOUT_S",
    "TOOLS", "build_command", "run_collection",
]
