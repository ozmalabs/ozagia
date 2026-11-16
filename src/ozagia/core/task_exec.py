"""A small, local, read-only executor for bounded task steps.

:class:`LocalExecutor` is the opt-in adapter behind
``ozagia.tasks_cli run ... --executor local``. It implements the
:class:`ozagia.core.tasks.TaskRunner` executor protocol for six tools:

``read``
    A bounded UTF-8 file/range read inside the step's declared paths.
``grep``
    A bounded literal or regular-expression search inside the step's paths.
``glob``
    A bounded path glob inside the step's paths.
``graph_search``
    A read-only search of an existing ``.ozagia/graph.json`` via
    :class:`ozagia.core.memory.DevMemory`.
``graph_impact``
    Read-only reverse dependency and covering-test lookup over that snapshot.
``test``
    ``python -m pytest`` on the files selected by the task's ``test_plan``.

The executor never writes to the repository (the test process is told not to
write bytecode or a pytest cache), never builds or refreshes the graph and
never calls a model. Mutating, model and unimplemented tools return a
structured ``blocked`` result. A missing, stale or unreadable graph is a
structured error, not a reason to build one.

All results use the keys accepted by ``tasks._normalize_result``. Evidence
and artifacts are metadata only: a file's text may appear in ``output`` (the
tool result), but never in an evidence label, summary or artifact entry.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Sequence

from ozagia.config import state_dir, state_path, state_rel
from ozagia.core import tasks as task_spec

SCHEMA = "ozagia.task_exec.v1"

SUPPORTED_TOOLS = frozenset({
    "read", "grep", "glob", "graph_search", "graph_impact", "test",
})

#: Tools this slice answers with ``blocked`` instead of running.
MUTATING_OR_MODEL_TOOLS = frozenset({"write", "edit", "patch", "shell", "model"})

_WINDOWS_ABS = re.compile(r"^[A-Za-z]:/")
_MAX_SUMMARY = 200
_MAX_EVIDENCE = 200
_MAX_POLICY_TEXT = 500

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".ozagia",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".tox", ".nox", ".venv", "venv", "node_modules",
})


class LocalExecError(ValueError):
    """A local tool call was refused or could not be completed."""

    def __init__(self, code: str, message: str,
                 details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True)
class LocalPolicy:
    """Caps and switches for :class:`LocalExecutor`.

    Integer caps must be positive; ``allow_tests`` and ``allow_graph`` are
    booleans.
    """

    max_read_bytes: int = 256 * 1024
    max_read_lines: int = 1000
    max_file_bytes: int = 4 * 1024 * 1024
    max_grep_matches: int = 200
    max_grep_files: int = 2000
    max_grep_bytes: int = 8 * 1024 * 1024
    max_glob_results: int = 1000
    max_walk_entries: int = 100_000
    max_graph_results: int = 50
    max_graph_depth: int = 3
    max_test_files: int = 100
    test_timeout_s: int = 900
    max_test_output_bytes: int = 64 * 1024
    max_output_bytes: int = 512 * 1024
    allow_tests: bool = True
    allow_graph: bool = True

    @classmethod
    def from_value(cls, value: Any) -> LocalPolicy:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("policy must be a LocalPolicy or a mapping")
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(
                "unknown local executor policy key(s): " + ", ".join(unknown))
        data: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in value:
                continue
            raw = value[item.name]
            default = getattr(cls(), item.name)
            if isinstance(default, bool):
                if not isinstance(raw, bool):
                    raise ValueError(f"policy.{item.name}: must be true or false")
                data[item.name] = raw
                continue
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError(f"policy.{item.name}: must be an integer")
            if raw < 1:
                raise ValueError(f"policy.{item.name}: must be at least 1")
            data[item.name] = int(raw)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass
class _GraphCache:
    key: tuple[str, int, int]
    memory: Any
    files_by_path: dict[str, int]
    incoming: dict[int, list[dict[str, Any]]]
    outgoing: dict[int, list[dict[str, Any]]]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _bounded(value: Any, limit: int = _MAX_SUMMARY) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class LocalExecutor:
    """Read-only local task executor.

    ``policy`` is a :class:`LocalPolicy` or a mapping of its fields.
    ``graph_path`` defaults to ``<root>/.ozagia/graph.json``; a relative path
    is resolved against ``root``. ``python`` defaults to the interpreter
    running this process, so a test step does not depend on ``PATH``.
    """

    def __init__(self, root: str | Path, *, policy: Any = None,
                 graph_path: str | Path | None = None,
                 python: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"repository root does not exist: {self.root}")
        self.policy = LocalPolicy.from_value(policy)
        if graph_path is None:
            self.graph_path = state_path(self.root, "graph.json")
        else:
            path = Path(graph_path).expanduser()
            if not path.is_absolute():
                path = self.root / path
            self.graph_path = path.resolve(strict=False)
        self.python = (os.fspath(python) if python is not None
                       else sys.executable)
        if not self.python:
            raise ValueError("python must not be empty")
        self._state_dir = state_dir(self.root)
        self._state_rel = state_rel(self.root)
        self._graph_cache: _GraphCache | None = None

    # -- protocol ----------------------------------------------------------- #
    def __call__(self, step: Mapping[str, Any],
                 context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.execute(step, context)

    def execute(self, step: Mapping[str, Any],
                context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run one step and return a ``tasks._normalize_result`` table."""
        started = time.monotonic()
        tool = "?"
        try:
            if not isinstance(step, Mapping):
                raise LocalExecError("bad_step", "step must be a table")
            raw_tool = step.get("tool")
            if not isinstance(raw_tool, str) or not raw_tool:
                raise LocalExecError("bad_step", "step.tool must be a string")
            tool = raw_tool
            if context is None:
                context = {}
            if not isinstance(context, Mapping):
                raise LocalExecError("bad_context", "context must be a table")
            context_root = context.get("root")
            if context_root is not None:
                try:
                    other = Path(str(context_root)).expanduser().resolve()
                except (OSError, RuntimeError, ValueError) as exc:
                    raise LocalExecError(
                        "bad_context", f"context.root is invalid: {exc}") from exc
                if other != self.root:
                    return self._finish(
                        self._blocked(
                            tool,
                            f"context root {other} does not match executor "
                            f"root {self.root}"),
                        tool, started)
            result = self._dispatch(tool, step, context)
        except LocalExecError as exc:
            result = self._error(tool, exc.code, str(exc), exc.details)
        except Exception as exc:  # noqa: BLE001 - a tool must report, not crash
            result = self._error(
                tool, "internal_error", f"{type(exc).__name__}: {exc}")
        return self._finish(result, tool, started)

    def _dispatch(self, tool: str, step: Mapping[str, Any],
                  context: Mapping[str, Any]) -> dict[str, Any]:
        if tool in SUPPORTED_TOOLS:
            if tool == "read":
                return self._read(step)
            if tool == "grep":
                return self._grep(step)
            if tool == "glob":
                return self._glob(step)
            if tool == "graph_search":
                return self._graph_search(step)
            if tool == "graph_impact":
                return self._graph_impact(step)
            return self._test(step, context)
        if tool in task_spec.TOOL_CLASSES:
            if tool in MUTATING_OR_MODEL_TOOLS:
                reason = (
                    "the local executor is read-only in this slice; "
                    f"{tool!r} is not available")
            else:
                reason = f"tool {tool!r} is not implemented by LocalExecutor"
            return self._blocked(tool, reason)
        raise LocalExecError("unknown_tool", f"unknown tool {tool!r}")

    # -- result helpers ----------------------------------------------------- #
    def _blocked(self, tool: str, reason: str) -> dict[str, Any]:
        reason = _bounded(reason, _MAX_POLICY_TEXT)
        return {
            "status": "blocked",
            "summary": _bounded(f"{tool} blocked: {reason}"),
            "artifacts": [],
            "evidence": [{
                "label": _bounded(f"{tool} blocked by the local executor"),
                "kind": "policy",
            }],
            "output": {
                "ok": False, "blocked": True, "tool": tool,
                "reason": reason,
            },
        }

    def _error(self, tool: str, code: str, message: str,
               details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        message = _bounded(message, _MAX_POLICY_TEXT)
        output: dict[str, Any] = {
            "ok": False, "tool": tool, "error_code": code,
            "error": message,
        }
        if details:
            output["details"] = dict(details)
            if code.startswith("graph_"):
                output["graph"] = dict(details)
        return {
            "status": "error",
            "summary": _bounded(f"{tool}: {message}"),
            "artifacts": [],
            "evidence": [],
            "output": output,
        }

    def _finish(self, result: Mapping[str, Any], tool: str,
                started: float) -> dict[str, Any]:
        elapsed = round(time.monotonic() - started, 3)
        usage = dict(result.get("usage") or {})
        reported = usage.get("wall_s")
        try:
            reported_wall = float(reported or 0.0)
        except (TypeError, ValueError):
            reported_wall = 0.0
        usage.setdefault("tokens", 0)
        usage.setdefault("model_calls", 0)
        usage.setdefault("cost_usd", 0.0)
        usage["wall_s"] = round(max(elapsed, reported_wall), 3)
        cleaned: dict[str, Any] = {
            "status": result.get("status", "error"),
            "summary": _bounded(result.get("summary")),
            "usage": usage,
            "artifacts": [],
            "evidence": self._clean_evidence(result.get("evidence")),
        }
        if "output" in result:
            output = self._fit_output(result.get("output"))
            cleaned["output"] = output
            cleaned["state"] = self._state(tool, output)
        else:
            cleaned["state"] = self._state(
                tool, {"status": cleaned["status"]})
        return cleaned

    @staticmethod
    def _clean_evidence(value: Any) -> list[dict[str, str]]:
        if not value:
            return []
        entries = value if isinstance(value, list) else [value]
        result: list[dict[str, str]] = []
        for item in entries:
            if isinstance(item, str):
                label, kind = item, ""
            elif isinstance(item, Mapping):
                label = item.get("label")
                kind = item.get("kind", "")
            else:
                continue
            if not isinstance(label, str) or not label.strip():
                continue
            result.append({
                "label": _bounded(label, _MAX_EVIDENCE),
                "kind": _bounded(kind, 80),
            })
        return result

    def _fit_output(self, output: Any) -> dict[str, Any]:
        if not isinstance(output, Mapping):
            output = {"value": output}
        data = dict(output)
        try:
            text = _canonical(data)
        except (TypeError, ValueError):
            return {
                "ok": False, "truncated": True,
                "error_code": "unserializable_output",
                "error": "tool output is not JSON-serializable",
            }
        if len(text.encode("utf-8")) <= self.policy.max_output_bytes:
            return data
        data["truncated"] = True
        for _ in range(40):
            try:
                size = len(_canonical(data).encode("utf-8"))
            except (TypeError, ValueError):
                break
            if size <= self.policy.max_output_bytes:
                return data
            largest_key = None
            largest_len = 0
            for key, value in list(data.items()):
                if isinstance(value, str) and len(value) > largest_len:
                    largest_key, largest_len = key, len(value)
                elif (isinstance(value, list) and len(value) > largest_len
                      and value):
                    largest_key, largest_len = key, len(value)
            if largest_key is None:
                break
            value = data[largest_key]
            data[largest_key] = value[: max(0, len(value) // 2)]
        return {
            "ok": data.get("ok", False),
            "tool": data.get("tool"),
            "truncated": True,
            "error_code": "output_cap",
            "error": "tool output exceeded the local output cap",
        }

    @staticmethod
    def _state(tool: str, output: Any) -> str:
        try:
            text = _canonical({"tool": tool, "output": output})
        except (TypeError, ValueError):
            text = f"{tool}:unserializable"
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    # -- argument helpers --------------------------------------------------- #
    @staticmethod
    def _args(step: Mapping[str, Any]) -> dict[str, Any]:
        raw = step.get("args") or {}
        if not isinstance(raw, Mapping):
            raise LocalExecError("bad_step", "step.args must be a table")
        return dict(raw)

    @staticmethod
    def _reject_unknown(args: Mapping[str, Any], allowed: set[str],
                        tool: str) -> None:
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise LocalExecError(
                "bad_args",
                f"{tool} does not support argument(s): " + ", ".join(unknown))

    @staticmethod
    def _integer_arg(args: Mapping[str, Any], names: Sequence[str], *,
                     default: int | None = None, low: int = 1,
                     where: str) -> int | None:
        for name in names:
            if name not in args:
                continue
            raw = args[name]
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise LocalExecError(
                    "bad_args", f"{where}: must be an integer")
            if raw < low:
                raise LocalExecError(
                    "bad_args", f"{where}: must be at least {low}")
            return int(raw)
        return default

    @staticmethod
    def _bool_arg(args: Mapping[str, Any], name: str, *,
                  default: bool) -> bool:
        if name not in args:
            return default
        raw = args[name]
        if not isinstance(raw, bool):
            raise LocalExecError("bad_args", f"{name}: must be true or false")
        return raw

    @staticmethod
    def _string_arg(args: Mapping[str, Any], names: Sequence[str], *,
                    where: str, max_len: int = 500) -> str:
        for name in names:
            if name not in args:
                continue
            raw = args[name]
            if not isinstance(raw, str) or not raw.strip():
                raise LocalExecError(
                    "bad_args", f"{where}: must be a non-empty string")
            text = raw.strip()
            if len(text) > max_len:
                raise LocalExecError(
                    "bad_args", f"{where}: must be at most {max_len} characters")
            return text
        raise LocalExecError("bad_args", f"{where}: is required")

    # -- paths -------------------------------------------------------------- #
    @staticmethod
    def _norm_rel(value: Any, *, where: str = "path") -> str:
        if not isinstance(value, str):
            raise LocalExecError("bad_path", f"{where}: must be a string path")
        text = value.strip().replace("\\", "/")
        if not text:
            raise LocalExecError("bad_path", f"{where}: must not be empty")
        if text.startswith("/") or _WINDOWS_ABS.match(text):
            raise LocalExecError(
                "bad_path", f"{where}: must be relative to the repository root")
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise LocalExecError(
                "bad_path", f"{where}: must not contain control characters")
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise LocalExecError(
                "bad_path", f"{where}: must stay inside the repository root")
        return "/".join(parts) if parts else "."

    def _forbidden(self, rel: str, resolved: Path) -> str | None:
        parts = [] if rel == "." else rel.split("/")
        if ".git" in parts:
            return "inside .git"
        if ".hg" in parts or ".svn" in parts:
            return "inside version-control metadata"
        if self._state_rel and (rel == self._state_rel
                                or rel.startswith(self._state_rel + "/")):
            return "inside the state directory"
        if _is_relative(resolved, self._state_dir):
            return "inside the state directory"
        return None

    def _resolve(self, rel: str, *, must_exist: bool = False) -> Path:
        rel = self._norm_rel(rel)
        parts = [] if rel == "." else rel.split("/")
        candidate = self.root if not parts else self.root.joinpath(*parts)
        current = self.root
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise LocalExecError(
                    "bad_path", f"cannot inspect {rel!r}: {exc}") from exc
            if stat_module.S_ISLNK(info.st_mode):
                raise LocalExecError(
                    "symlink_refused",
                    f"{rel!r} contains a symlink; the local executor does not "
                    "follow symlinks")
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise LocalExecError(
                "bad_path", f"cannot resolve {rel!r}: {exc}") from exc
        if not _is_relative(resolved, self.root):
            raise LocalExecError(
                "outside_root",
                f"{rel!r} resolves outside the repository root")
        if must_exist and not candidate.exists():
            raise LocalExecError("not_found", f"{rel!r} does not exist")
        reason = self._forbidden(rel, resolved)
        if reason:
            raise LocalExecError("forbidden_path", f"{rel!r} is {reason}")
        return resolved

    def _step_paths(self, step: Mapping[str, Any]) -> list[str]:
        raw = step.get("paths")
        if raw is None:
            return []
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
            raise LocalExecError(
                "bad_step", "step.paths must be a list of paths")
        return [self._norm_rel(item, where="step path") for item in raw]

    def _in_step_scope(self, rel: str, step: Mapping[str, Any]) -> bool:
        paths = self._step_paths(step)
        if not paths:
            return True
        for entry in paths:
            if entry == "." or rel == entry or rel.startswith(entry + "/"):
                return True
        return False

    def _require_scope(self, rel: str, step: Mapping[str, Any]) -> None:
        if not self._in_step_scope(rel, step):
            raise LocalExecError(
                "outside_scope",
                f"{rel!r} is outside the step's declared paths")

    def _relative_to_scopes(self, rel: str,
                            step: Mapping[str, Any]) -> list[str]:
        """The root-relative path plus its suffix under each step path."""
        candidates = [rel]
        for entry in self._step_paths(step):
            if entry == ".":
                continue
            if rel == entry:
                suffix = rel.rsplit("/", 1)[-1]
            elif rel.startswith(entry + "/"):
                suffix = rel[len(entry) + 1:]
            else:
                continue
            if suffix and suffix not in candidates:
                candidates.append(suffix)
        return candidates

    def _scope_target(self, rel: str, step: Mapping[str, Any]) -> str:
        """Accept a path relative to a single step path when that exists."""
        if self._in_step_scope(rel, step):
            return rel
        try:
            if (self.root / rel).exists():
                # A root-relative path wins over the step-relative reading.
                return rel
        except OSError:
            pass
        for entry in self._step_paths(step):
            if entry == ".":
                continue
            if rel == entry.rsplit("/", 1)[-1]:
                candidate = entry
            else:
                candidate = f"{entry}/{rel}"
            if not self._in_step_scope(candidate, step):
                continue
            try:
                if (self.root / candidate).exists():
                    return candidate
            except OSError:
                continue
        return rel

    def _scope_roots(self, step: Mapping[str, Any]) -> list[tuple[str, Path]]:
        paths = self._step_paths(step) or ["."]
        roots: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for rel in paths:
            resolved = self._resolve(rel, must_exist=True)
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            roots.append((rel, resolved))
        return roots

    def _skip_dir(self, path: Path, name: str) -> bool:
        if name in _SKIP_DIRS:
            return True
        try:
            if path.resolve() == self._state_dir:
                return True
        except (OSError, RuntimeError):
            return True
        return os.path.islink(path)

    def _walk(self, step: Mapping[str, Any]) -> tuple[
            list[tuple[str, Path]], bool]:
        """Regular files under the step paths, deterministic and bounded."""
        roots = self._scope_roots(step)
        found: list[tuple[str, Path]] = []
        seen: set[str] = set()
        truncated = False
        for rel_root, path in roots:
            if path.is_file():
                if rel_root not in seen:
                    found.append((rel_root, path))
                    seen.add(rel_root)
                continue
            if not path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(
                    path, followlinks=False):
                base = Path(dirpath)
                dirnames[:] = sorted(
                    name for name in dirnames
                    if not self._skip_dir(base / name, name))
                rel_dir = base.relative_to(self.root).as_posix()
                rel_dir = "" if rel_dir == "." else rel_dir
                for name in sorted(filenames):
                    if len(found) >= self.policy.max_walk_entries:
                        return sorted(found, key=lambda item: item[0]), True
                    full = base / name
                    rel = f"{rel_dir}/{name}" if rel_dir else name
                    if rel in seen or self._forbidden(rel, full):
                        continue
                    try:
                        info = full.lstat()
                    except OSError:
                        continue
                    if (stat_module.S_ISLNK(info.st_mode)
                            or not stat_module.S_ISREG(info.st_mode)):
                        continue
                    found.append((rel, full))
                    seen.add(rel)
        found.sort(key=lambda item: item[0])
        return found, truncated

    @staticmethod
    def _glob_regex(pattern: str) -> re.Pattern[str]:
        """Root-relative glob semantics: `**` crosses directories.

        `*` and `?` stay inside one path component, `[...]` is a character
        class, and a trailing `**` matches everything below that directory.
        A pattern without `**` therefore does not recurse.
        """
        parts = [part for part in pattern.split("/") if part not in ("", ".")]
        if not parts or parts[-1] == "**":
            parts = [*parts, "**"]
        out: list[str] = []
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if part == "**":
                out.append(r".*" if last else r"(?:[^/]+/)*")
                continue
            segment: list[str] = []
            position = 0
            while position < len(part):
                char = part[position]
                if char == "*":
                    segment.append(r"[^/]*")
                elif char == "?":
                    segment.append(r"[^/]")
                elif char == "[":
                    end = part.find("]", position + 1)
                    if end < 0:
                        segment.append(re.escape(char))
                    else:
                        body = part[position + 1:end]
                        if body.startswith("!"):
                            body = "^" + body[1:]
                        segment.append("[" + body.replace("\\", "\\\\") + "]")
                        position = end
                else:
                    segment.append(re.escape(char))
                position += 1
            out.append("".join(segment) + ("" if last else "/"))
        return re.compile("".join(out) + r"\Z")

    @staticmethod
    def _norm_pattern(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise LocalExecError(
                "bad_args", "pattern: must be a non-empty string")
        text = value.strip().replace("\\", "/")
        if text.startswith("/") or _WINDOWS_ABS.match(text):
            raise LocalExecError(
                "bad_args", "pattern: must be repository-relative")
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise LocalExecError(
                "bad_args", "pattern: must not contain control characters")
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise LocalExecError(
                "bad_args", "pattern: must stay inside the repository root")
        if not parts:
            return "*"
        result = "/".join(parts)
        if text.endswith("/"):
            result += "/**"
        return result

    # -- read --------------------------------------------------------------- #
    def _read(self, step: Mapping[str, Any]) -> dict[str, Any]:
        args = self._args(step)
        self._reject_unknown(
            args, {"path", "file", "start", "end", "offset", "limit"}, "read")
        raw_path = args.get("path") or args.get("file")
        if raw_path is None:
            scope = self._step_paths(step)
            if len(scope) == 1 and scope[0] != ".":
                raw_path = scope[0]
            else:
                raise LocalExecError("bad_args", "read: path is required")
        rel = self._norm_rel(raw_path, where="read path")
        rel = self._scope_target(rel, step)
        self._require_scope(rel, step)
        resolved = self._resolve(rel, must_exist=True)
        if not resolved.is_file():
            raise LocalExecError("not_a_file", f"{rel!r} is not a regular file")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise LocalExecError("read_failed", f"cannot stat {rel!r}: {exc}") from exc
        if size > self.policy.max_file_bytes:
            raise LocalExecError(
                "file_too_large",
                f"{rel!r} is {size} bytes; the local read cap is "
                f"{self.policy.max_file_bytes} bytes")
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            raise LocalExecError("read_failed", f"cannot read {rel!r}: {exc}") from exc
        if b"\0" in data:
            raise LocalExecError("binary_file", f"{rel!r} looks binary")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LocalExecError(
                "not_utf8", f"{rel!r} is not valid UTF-8: {exc}") from exc
        lines = text.splitlines(keepends=True)
        total = len(lines)
        start_arg = self._integer_arg(
            args, ("start",), where="start", low=1)
        offset_arg = self._integer_arg(
            args, ("offset",), where="offset", low=0)
        if start_arg is not None and offset_arg is not None:
            raise LocalExecError(
                "bad_args", "read: give start or offset, not both")
        end_arg = self._integer_arg(args, ("end",), where="end", low=1)
        limit_arg = self._integer_arg(
            args, ("limit",), where="limit", low=1)
        if offset_arg is not None:
            start_index = offset_arg
        elif start_arg is not None:
            start_index = start_arg - 1
        else:
            start_index = 0
        if end_arg is not None:
            if end_arg <= start_index:
                raise LocalExecError(
                    "bad_args", "read: end must be after start")
            requested_end = end_arg
        elif limit_arg is not None:
            requested_end = start_index + int(limit_arg)
        else:
            requested_end = total
        if start_index >= total and total > 0:
            raise LocalExecError(
                "range_error",
                f"read: start line {start_index + 1} is past the end "
                f"({total} lines)")
        available = max(0, total - start_index)
        wanted = max(0, requested_end - start_index)
        selected = min(wanted, self.policy.max_read_lines, available)
        end_index = start_index + selected
        truncated = selected < min(wanted, available)
        chunks: list[str] = []
        used = 0
        actual_end = start_index
        for line in lines[start_index:end_index]:
            chunk = line
            encoded = chunk.encode("utf-8")
            if used + len(encoded) > self.policy.max_read_bytes:
                room = max(0, self.policy.max_read_bytes - used)
                if room:
                    chunks.append(encoded[:room].decode("utf-8", "ignore"))
                    actual_end += 1
                truncated = True
                break
            chunks.append(chunk)
            used += len(encoded)
            actual_end += 1
        if actual_end < end_index:
            truncated = True
        body = "".join(chunks)
        output = {
            "ok": True,
            "tool": "read",
            "path": rel,
            "start": start_index + 1,
            "end": actual_end if actual_end > start_index else 0,
            "total_lines": total,
            "text": body,
            "truncated": truncated,
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
        if actual_end > start_index:
            label = (f"read {rel} lines {start_index + 1}-{actual_end} "
                     f"of {total}")
        else:
            label = f"read {rel} (no lines)"
        return {
            "status": "ok",
            "summary": _bounded(
                f"read {rel}: {actual_end - start_index} line(s) of {total}"),
            "artifacts": [],
            "evidence": [{"label": label, "kind": "read"}],
            "output": output,
        }

    # -- grep and glob ------------------------------------------------------ #
    def _grep(self, step: Mapping[str, Any]) -> dict[str, Any]:
        args = self._args(step)
        self._reject_unknown(
            args, {"pattern", "query", "text", "regex", "case_sensitive",
                   "max_matches", "limit"}, "grep")
        pattern = self._string_arg(
            args, ("pattern", "query", "text"), where="grep pattern",
            max_len=500)
        use_regex = self._bool_arg(args, "regex", default=False)
        case_sensitive = self._bool_arg(
            args, "case_sensitive", default=True)
        requested = self._integer_arg(
            args, ("max_matches", "limit"), where="grep limit", low=1)
        limit = min(requested or self.policy.max_grep_matches,
                    self.policy.max_grep_matches)
        flags = 0 if case_sensitive else re.IGNORECASE
        if use_regex:
            try:
                matcher = re.compile(pattern, flags)
            except re.error as exc:
                raise LocalExecError(
                    "bad_args", f"grep pattern is not a valid regex: {exc}") from exc
        else:
            needle = pattern if case_sensitive else pattern.lower()
        files, walk_truncated = self._walk(step)
        matches: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        files_scanned = 0
        bytes_scanned = 0
        truncated = walk_truncated
        for rel, path in files:
            if len(matches) >= limit:
                truncated = True
                break
            if files_scanned >= self.policy.max_grep_files:
                truncated = True
                break
            if bytes_scanned >= self.policy.max_grep_bytes:
                truncated = True
                break
            try:
                size = path.stat().st_size
            except OSError as exc:
                if len(skipped) < 20:
                    skipped.append({
                        "path": rel, "reason": _bounded(exc, 120)})
                continue
            if size > self.policy.max_file_bytes:
                if len(skipped) < 20:
                    skipped.append({
                        "path": rel,
                        "reason": f"larger than "
                                  f"{self.policy.max_file_bytes} bytes",
                    })
                continue
            remaining = self.policy.max_grep_bytes - bytes_scanned
            if size > remaining:
                truncated = True
                break
            try:
                data = path.read_bytes()
            except OSError as exc:
                if len(skipped) < 20:
                    skipped.append({
                        "path": rel, "reason": _bounded(exc, 120)})
                continue
            bytes_scanned += len(data)
            if b"\0" in data[:8192]:
                if len(skipped) < 20:
                    skipped.append({"path": rel, "reason": "binary file"})
                continue
            body = data.decode("utf-8", "ignore")
            files_scanned += 1
            for number, line in enumerate(body.splitlines(), 1):
                hit = (matcher.search(line) is not None if use_regex
                       else needle in (line if case_sensitive else line.lower()))
                if not hit:
                    continue
                matches.append({
                    "path": rel, "line": number,
                    "text": line[:300],
                })
                if len(matches) >= limit:
                    truncated = True
                    break
            if bytes_scanned >= self.policy.max_grep_bytes:
                truncated = True
                break
        if len(skipped) > 20:
            skipped = skipped[:20]
            truncated = True
        output = {
            "ok": True,
            "tool": "grep",
            "pattern": _bounded(pattern, 200),
            "regex": use_regex,
            "case_sensitive": case_sensitive,
            "matches": matches,
            "count": len(matches),
            "files_scanned": files_scanned,
            "files_skipped": skipped,
            "bytes_scanned": bytes_scanned,
            "truncated": truncated,
        }
        suffix = " (truncated)" if truncated else ""
        return {
            "status": "ok",
            "summary": _bounded(
                f"grep matched {len(matches)} line(s) in {files_scanned} "
                f"file(s){suffix}"),
            "artifacts": [],
            "evidence": [{
                "label": _bounded(
                    f"grep matched {len(matches)} line(s) in "
                    f"{files_scanned} file(s)"),
                "kind": "grep",
            }],
            "output": output,
        }

    def _glob(self, step: Mapping[str, Any]) -> dict[str, Any]:
        args = self._args(step)
        self._reject_unknown(
            args, {"pattern", "glob", "max_results", "limit"}, "glob")
        pattern = self._norm_pattern(
            self._string_arg(args, ("pattern", "glob"), where="glob pattern"))
        try:
            matcher = self._glob_regex(pattern)
        except re.error as exc:
            raise LocalExecError(
                "bad_args", f"glob pattern is invalid: {exc}") from exc
        requested = self._integer_arg(
            args, ("max_results", "limit"), where="glob limit", low=1)
        limit = min(requested or self.policy.max_glob_results,
                    self.policy.max_glob_results)
        files, walk_truncated = self._walk(step)
        paths: list[str] = []
        truncated = walk_truncated
        for rel, _path in files:
            candidates = self._relative_to_scopes(rel, step)
            if not any(matcher.match(candidate) is not None
                       for candidate in candidates):
                continue
            paths.append(rel)
            if len(paths) >= limit:
                truncated = True
                break
        paths.sort()
        output = {
            "ok": True,
            "tool": "glob",
            "pattern": pattern,
            "paths": paths,
            "count": len(paths),
            "truncated": truncated,
        }
        suffix = " (truncated)" if truncated else ""
        return {
            "status": "ok",
            "summary": _bounded(
                f"glob matched {len(paths)} path(s){suffix}"),
            "artifacts": [],
            "evidence": [{
                "label": _bounded(f"glob matched {len(paths)} path(s)"),
                "kind": "glob",
            }],
            "output": output,
        }

    # -- graph -------------------------------------------------------------- #
    def _graph(self) -> tuple[_GraphCache, dict[str, Any]]:
        path = self.graph_path
        info: dict[str, Any] = {
            "path": str(path), "freshness": "missing", "state": "missing",
            "snapshot_root": None, "snapshot_head": None, "head": None,
            "tree_changed": None, "verification_errors": [],
            "nodes": 0, "edges": 0,
        }
        if not path.is_file():
            raise LocalExecError(
                "graph_missing",
                f"no graph snapshot at {path}; the local executor does not "
                "build or refresh one", details=info)
        try:
            status = path.stat()
            key = (str(path), status.st_mtime_ns, status.st_size)
        except OSError as exc:
            raise LocalExecError(
                "graph_invalid", f"cannot stat {path}: {exc}", details=info) from exc
        cache = self._graph_cache
        if cache is None or cache.key != key:
            try:
                from ozagia.core.memory import DevMemory
                memory = DevMemory(
                    dump=path,
                    journal=state_path(self.root, "journal.jsonl"),
                    root=self.root,
                )
            except Exception as exc:  # noqa: BLE001 - any load failure
                info.update(freshness="invalid", state="invalid")
                raise LocalExecError(
                    "graph_invalid",
                    f"cannot read graph snapshot {path}: {exc}",
                    details=info) from exc
            files_by_path: dict[str, int] = {}
            for nid, node in memory.nodes.items():
                attrs = node.get("attrs")
                if not isinstance(attrs, Mapping):
                    continue
                rel = attrs.get("path") or attrs.get("file")
                if isinstance(rel, str) and rel:
                    files_by_path.setdefault(
                        rel.replace("\\", "/"), int(nid))
            incoming: dict[int, list[dict[str, Any]]] = {}
            outgoing: dict[int, list[dict[str, Any]]] = {}
            for edge in memory.edges:
                try:
                    src, tgt = int(edge["src"]), int(edge["tgt"])
                except (KeyError, TypeError, ValueError):
                    continue
                incoming.setdefault(tgt, []).append(edge)
                outgoing.setdefault(src, []).append(edge)
            cache = _GraphCache(
                key=key, memory=memory, files_by_path=files_by_path,
                incoming=incoming, outgoing=outgoing)
            self._graph_cache = cache
        memory = cache.memory
        info.update({
            "nodes": len(memory.nodes), "edges": len(memory.edges),
        })
        try:
            # DevMemory memoises staleness for a few seconds. A graph tool
            # must decide freshness on every call, so clear the memo first.
            memory._staleness_memo = None
            staleness = memory.staleness()
        except Exception as exc:  # noqa: BLE001 - verification failed
            info.update(freshness="invalid", state="invalid")
            raise LocalExecError(
                "graph_invalid",
                f"cannot verify graph freshness: {exc}",
                details=info) from exc
        freshness = str(staleness.get("freshness") or "unknown")
        info.update({
            "freshness": freshness,
            "state": freshness,
            "snapshot_root": staleness.get("snapshot_root"),
            "snapshot_head": staleness.get("snapshot_head"),
            "head": staleness.get("head"),
            "tree_changed": staleness.get("tree_changed"),
            "verification_errors": [
                _bounded(item, 200)
                for item in (staleness.get("verification_errors") or [])[:5]
            ],
        })
        if freshness != "fresh":
            code = "graph_stale" if freshness == "stale" else "graph_unverified"
            detail = "; ".join(info["verification_errors"]) or freshness
            raise LocalExecError(
                code,
                f"graph snapshot is {freshness}: {detail}",
                details=info)
        return cache, info

    def _graph_search(self, step: Mapping[str, Any]) -> dict[str, Any]:
        if not self.policy.allow_graph:
            return self._blocked("graph_search", "graph tools are disabled by policy")
        args = self._args(step)
        self._reject_unknown(
            args, {"query", "q", "types", "limit"}, "graph_search")
        query = self._string_arg(
            args, ("query", "q"), where="graph_search query", max_len=500)
        limit_arg = self._integer_arg(
            args, ("limit",), where="graph_search limit", low=1,
            default=self.policy.max_graph_results)
        limit = min(int(limit_arg), self.policy.max_graph_results)
        types = args.get("types")
        if types is not None:
            if (isinstance(types, (str, bytes))
                    or not isinstance(types, (list, tuple))
                    or not all(isinstance(item, str) and item
                               for item in types)):
                raise LocalExecError(
                    "bad_args", "types: must be a list of node type names")
            types = list(types)
        cache, info = self._graph()
        results = cache.memory.search(query, types=types, limit=limit)
        output = {
            "ok": True,
            "tool": "graph_search",
            "query": _bounded(query, 200),
            "results": results,
            "count": len(results),
            "truncated": len(results) >= limit,
            "graph": info,
        }
        return {
            "status": "ok",
            "summary": _bounded(f"graph_search returned {len(results)} node(s)"),
            "artifacts": [],
            "evidence": [{
                "label": _bounded(f"graph_search -> {len(results)} node(s)"),
                "kind": "graph",
            }],
            "output": output,
        }

    def _graph_impact(self, step: Mapping[str, Any]) -> dict[str, Any]:
        if not self.policy.allow_graph:
            return self._blocked("graph_impact", "graph tools are disabled by policy")
        args = self._args(step)
        self._reject_unknown(args, {"path", "file", "depth"}, "graph_impact")
        raw_path = args.get("path") or args.get("file")
        if raw_path is None:
            raise LocalExecError("bad_args", "graph_impact: path is required")
        rel = self._norm_rel(raw_path, where="graph_impact path")
        rel = self._scope_target(rel, step)
        self._require_scope(rel, step)
        # A deleted file can still be in a snapshot; only lexical containment
        # and symlink components are checked here.
        self._resolve(rel, must_exist=False)
        requested_depth = self._integer_arg(
            args, ("depth",), where="graph_impact depth", low=1,
            default=2)
        depth = min(int(requested_depth or 2), self.policy.max_graph_depth)
        cache, info = self._graph()
        memory = cache.memory
        target = cache.files_by_path.get(rel)
        if target is None:
            raise LocalExecError(
                "file_not_indexed",
                f"{rel!r} is not indexed in the graph snapshot",
                details=info)
        impacted: list[dict[str, Any]] = []
        seen_nodes = {int(target)}
        seen_paths: set[str] = set()
        queue: deque[tuple[int, int]] = deque([(int(target), 0)])
        truncated = False
        node_budget = max(100, self.policy.max_graph_results * 20)
        while queue:
            if len(seen_nodes) >= node_budget:
                truncated = True
                break
            nid, distance = queue.popleft()
            if distance >= depth:
                continue
            for edge in cache.incoming.get(nid, ()):
                try:
                    src = int(edge["src"])
                except (KeyError, TypeError, ValueError):
                    continue
                if src in seen_nodes:
                    continue
                seen_nodes.add(src)
                source = memory.node(src) or {}
                if source.get("type") == "Test":
                    continue
                path = self._node_file(memory, src)
                if not path or path == rel or path in seen_paths:
                    continue
                if len(impacted) >= self.policy.max_graph_results:
                    truncated = True
                    queue.clear()
                    break
                impacted.append({"path": path, "depth": distance + 1})
                seen_paths.add(path)
                queue.append((src, distance + 1))
        impacted.sort(key=lambda item: (item["depth"], item["path"]))
        tests: list[str] = []
        for edge in cache.outgoing.get(int(target), ()):
            if edge.get("type") not in {
                    "has_test", "tests_module", "mentions_file",
                    "tested_by"}:
                continue
            try:
                nid = int(edge["tgt"])
            except (KeyError, TypeError, ValueError):
                continue
            node = memory.node(nid) or {}
            if node.get("type") != "Test":
                continue
            path = self._node_file(memory, nid)
            if path and path not in tests:
                tests.append(path)
        for edge in cache.incoming.get(int(target), ()):
            if edge.get("type") not in {
                    "has_test", "tests_module", "mentions_file",
                    "tested_by_file"}:
                continue
            try:
                nid = int(edge["src"])
            except (KeyError, TypeError, ValueError):
                continue
            node = memory.node(nid) or {}
            if node.get("type") != "Test":
                continue
            path = self._node_file(memory, nid)
            if path and path not in tests:
                tests.append(path)
        tests.sort()
        output = {
            "ok": True,
            "tool": "graph_impact",
            "file": rel,
            "depth": depth,
            "impacted": impacted,
            "count": len(impacted),
            "tests": tests,
            "test_count": len(tests),
            "truncated": truncated,
            "graph": info,
        }
        return {
            "status": "ok",
            "summary": _bounded(
                f"graph_impact {rel}: {len(impacted)} dependent file(s), "
                f"{len(tests)} test file(s)"),
            "artifacts": [],
            "evidence": [{
                "label": _bounded(
                    f"graph_impact {rel} -> {len(impacted)} file(s), "
                    f"{len(tests)} test(s)"),
                "kind": "graph",
            }],
            "output": output,
        }

    @staticmethod
    def _node_file(memory: Any, nid: int) -> str | None:
        node = memory.node(nid) or {}
        attrs = node.get("attrs")
        if isinstance(attrs, Mapping):
            rel = attrs.get("path") or attrs.get("file")
            if isinstance(rel, str) and rel:
                return rel.replace("\\", "/")
        key = memory.key_of(nid)
        if isinstance(key, str) and key.startswith("test:") and "::" in key:
            return key[len("test:"):].split("::", 1)[0]
        return None

    # -- tests -------------------------------------------------------------- #
    def _test(self, step: Mapping[str, Any],
              context: Mapping[str, Any]) -> dict[str, Any]:
        if not self.policy.allow_tests:
            return self._blocked("test", "tests are disabled by policy")
        args = self._args(step)
        allowed = {"test_plan"}
        self._reject_unknown(args, allowed, "test")
        plan = args.get("test_plan", context.get("test_plan"))
        if plan is None:
            return self._blocked(
                "test",
                "no test plan is attached; refusing to select or run tests "
                "locally")
        if not isinstance(plan, Mapping):
            raise LocalExecError("invalid_test_plan", "test_plan must be a table")
        if plan.get("available") is False:
            return self._blocked(
                "test",
                "the test plan is unavailable: "
                f"{plan.get('error') or 'unknown error'}")
        if plan.get("refused"):
            return self._blocked(
                "test",
                "the test policy refused to select tests: "
                f"{plan.get('fallback_reason') or 'no reason'}")
        if plan.get("mode") == "none":
            return self._blocked(
                "test",
                "the test policy selected none: "
                f"{plan.get('mode_reason') or 'no test selected'}")
        plan_root = plan.get("root")
        if plan_root is not None:
            try:
                other = Path(str(plan_root)).expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise LocalExecError(
                    "invalid_test_plan", f"test plan root is invalid: {exc}") from exc
            if other != self.root:
                raise LocalExecError(
                    "invalid_test_plan",
                    f"test plan root {other} does not match {self.root}")
        selected = plan.get("selected_files")
        if selected is None:
            selected_tests = plan.get("selected_tests")
            if isinstance(selected_tests, (list, tuple)):
                selected = [
                    item.get("file") if isinstance(item, Mapping) else item
                    for item in selected_tests
                ]
        if not isinstance(selected, (list, tuple)) or not selected:
            return self._blocked(
                "test",
                "the test plan selected no test files; refusing to run the "
                "full suite")
        selected = list(selected)
        if len(selected) > self.policy.max_test_files:
            return self._blocked(
                "test",
                f"the test plan selected {len(selected)} files, above the "
                f"local cap of {self.policy.max_test_files}")
        files: list[str] = []
        for index, item in enumerate(selected):
            if not isinstance(item, str):
                raise LocalExecError(
                    "invalid_test_plan",
                    f"selected_files[{index}] must be a string path")
            try:
                rel = self._norm_rel(item, where=f"selected_files[{index}]")
                if rel == ".":
                    raise LocalExecError(
                        "invalid_test_plan",
                        "selected_files must name test files")
                resolved = self._resolve(rel, must_exist=True)
            except LocalExecError as exc:
                raise LocalExecError(
                    "invalid_test_plan",
                    f"selected_files[{index}]: {exc}") from exc
            if not resolved.is_file():
                raise LocalExecError(
                    "invalid_test_plan", f"{rel!r} is not a regular file")
            if resolved.suffix != ".py":
                raise LocalExecError(
                    "invalid_test_plan",
                    f"{rel!r} is not a Python test file; this executor runs "
                    "pytest only")
            if rel not in files:
                files.append(rel)
        if not files:
            return self._blocked(
                "test", "the test plan selected no usable test files")
        files.sort()
        plan_timeout = plan.get("timeout_s")
        if plan_timeout is not None:
            if (isinstance(plan_timeout, bool)
                    or not isinstance(plan_timeout, int)
                    or plan_timeout < 1):
                raise LocalExecError(
                    "invalid_test_plan", "test_plan.timeout_s must be an integer")
            timeout = min(int(plan_timeout), self.policy.test_timeout_s)
        else:
            timeout = self.policy.test_timeout_s
        home = tempfile.mkdtemp(prefix="ozagia-task-test-")
        command = [
            self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--rootdir={self.root}", f"--basetemp={home}/pytest", "--",
            *files,
        ]
        started = time.monotonic()
        timed_out = False
        returncode: int | None = None
        raw = b""
        try:
            env = self._test_env(Path(home))
            with tempfile.TemporaryFile() as log:
                try:
                    proc = subprocess.run(
                        command, cwd=str(self.root), env=env,
                        stdin=subprocess.DEVNULL, stdout=log,
                        stderr=subprocess.STDOUT, timeout=timeout,
                        check=False, shell=False)
                except subprocess.TimeoutExpired:
                    timed_out = True
                except OSError as exc:
                    raise LocalExecError(
                        "test_spawn_failed", f"cannot start pytest: {exc}") from exc
                duration = round(time.monotonic() - started, 3)
                log.flush()
                size = log.tell()
                log.seek(0)
                raw = log.read(min(size, self.policy.max_test_output_bytes + 1))
                output_truncated = size > self.policy.max_test_output_bytes
            if not timed_out:
                returncode = int(proc.returncode)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        text = raw[:self.policy.max_test_output_bytes].decode(
            "utf-8", errors="replace")
        if timed_out:
            outcome = "timed_out"
        elif returncode == 0:
            outcome = "passed"
        elif returncode is not None and returncode > 0:
            outcome = "failed"
        else:
            outcome = "error"
        output = {
            "ok": (not timed_out and returncode == 0),
            "tool": "test",
            "command": command,
            "files": files,
            "selected_count": len(files),
            "mode": plan.get("mode"),
            "exit_code": returncode,
            "returncode": returncode,
            "outcome": outcome,
            "timed_out": timed_out,
            "timeout_s": timeout,
            "duration_s": duration,
            "output_truncated": output_truncated,
            "text": text,
        }
        if timed_out:
            summary = f"pytest timed out after {timeout}s"
            status = "error"
            label = f"pytest timed out after {timeout}s"
        elif returncode is None:
            summary = "pytest did not return an exit code"
            status = "error"
            label = summary
        elif returncode < 0:
            summary = f"pytest terminated by signal {-returncode}"
            status = "error"
            label = summary
        elif returncode == 0:
            summary = f"pytest passed: {len(files)} file(s)"
            status = "ok"
            label = summary
        else:
            summary = f"pytest exit {returncode}: {len(files)} file(s)"
            status = "ok"
            label = summary
        return {
            "status": status,
            "summary": _bounded(summary),
            "usage": {"wall_s": duration},
            "artifacts": [],
            "evidence": [{"label": _bounded(label), "kind": "test"}],
            "output": output,
        }

    def _test_env(self, home: Path) -> dict[str, str]:
        """A minimal environment; no task, proxy or secret variables."""
        env: dict[str, str] = {}
        for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
                     "SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        env.update({
            "HOME": str(home),
            "TMPDIR": str(home),
            "TEMP": str(home),
            "TMP": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PYTEST_ADDOPTS": "",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "GIT_TERMINAL_PROMPT": "0",
            "PIP_NO_INDEX": "1",
        })
        return env


__all__ = [
    "LocalExecError", "LocalExecutor", "LocalPolicy", "SCHEMA",
    "SUPPORTED_TOOLS",
]
