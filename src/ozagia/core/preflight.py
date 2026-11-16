"""Offline planning primitives for an optional, isolated IaC emulator pass.

The module builds plans only.  Execution is available solely through a runner
injected by the caller and requires two explicit opt-ins.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,63}$")
_ENVIRONMENT = MappingProxyType(
    {
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CLI_AUTO_PROMPT": "off",
        "CHECKPOINT_DISABLE": "1",
        "DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "LANG": "C.UTF-8",
        "TF_IN_AUTOMATION": "1",
    }
)


@dataclass(frozen=True)
class PreflightCommand:
    root: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class PreflightPlan:
    roots: tuple[str, ...]
    commands: tuple[PreflightCommand, ...]
    emulator_bin: str
    environment: Mapping[str, str]
    provider_versions: tuple[tuple[str, str], ...]

    @property
    def provider_version_note(self) -> str:
        if not self.provider_versions:
            return "Provider versions were not supplied; compatibility is unknown."
        versions = ", ".join(f"{name}={version}" for name, version in self.provider_versions)
        return f"Declared provider versions (not verified): {versions}"


@dataclass(frozen=True)
class PreflightResult:
    success: bool
    exit_codes: tuple[int, ...]
    completed_roots: tuple[str, ...]


Runner = Callable[..., Any]


def build_preflight(
    root_paths: Sequence[str | os.PathLike[str]],
    dependencies: Mapping[str | os.PathLike[str], Sequence[str | os.PathLike[str]]],
    *,
    emulator_bin: str | os.PathLike[str],
    provider_versions: Mapping[str, str] | None = None,
    order: Sequence[str | os.PathLike[str]] | None = None,
) -> PreflightPlan:
    """Build a dry-run-only plan; no executable or provider is invoked."""
    if isinstance(root_paths, (str, bytes)) or not root_paths:
        raise ValueError("root_paths must be a non-empty sequence of roots")
    binary = _explicit_binary_path(emulator_bin)
    cwd = Path.cwd().resolve()
    roots = [_safe_root(item, cwd) for item in root_paths]
    if len(set(roots)) != len(roots):
        raise ValueError("root_paths contains duplicate roots")
    root_set = set(roots)

    dependency_map: dict[str, set[str]] = {root: set() for root in roots}
    for raw_root, raw_dependencies in dependencies.items():
        root = _safe_root(raw_root, cwd)
        if root not in root_set:
            raise ValueError("dependency map names a root not present in root_paths")
        if isinstance(raw_dependencies, (str, bytes)):
            raise ValueError("each dependency list must be a sequence of roots")
        for raw_dependency in raw_dependencies:
            dependency = _safe_root(raw_dependency, cwd)
            if dependency not in root_set:
                raise ValueError("dependency references a root not present in root_paths")
            if dependency == root:
                raise ValueError("a root cannot depend on itself")
            dependency_map[root].add(dependency)

    tie_break = _order_ranks(order, roots, cwd)
    ordered = _topological_order(roots, dependency_map, tie_break)
    versions = _provider_versions(provider_versions)
    commands = tuple(
        PreflightCommand(
            root=root,
            argv=(binary, "preflight", "--root", root),
        )
        for root in ordered
    )
    return PreflightPlan(
        roots=tuple(ordered),
        commands=commands,
        emulator_bin=binary,
        environment=_ENVIRONMENT,
        provider_versions=versions,
    )


def execute_preflight(
    plan: PreflightPlan,
    *,
    runner: Runner,
    allow_execute: bool = False,
    emulator_bin: str | os.PathLike[str] | None = None,
) -> PreflightResult:
    """Pass each planned argv to an injected runner after explicit opt-in.

    This function does not create a subprocess.  Production callers must
    provide their own reviewed runner, executable path, and explicit consent.
    """
    if not allow_execute:
        raise PermissionError("execution requires allow_execute=True")
    if emulator_bin is None:
        raise ValueError("execution requires an explicit emulator binary path")
    binary = _explicit_binary_path(emulator_bin)
    if binary != plan.emulator_bin:
        raise ValueError("execution binary does not match the planned binary")
    if not callable(runner):
        raise TypeError("runner must be an injected callable")

    exit_codes: list[int] = []
    completed: list[str] = []
    for command in plan.commands:
        result = runner(
            command.argv,
            cwd=command.root,
            env=dict(plan.environment),
        )
        code = result if type(result) is int else getattr(result, "returncode", None)
        if type(code) is not int:
            raise TypeError("runner must return an integer exit code or returncode result")
        exit_codes.append(code)
        if code != 0:
            return PreflightResult(False, tuple(exit_codes), tuple(completed))
        completed.append(command.root)
    return PreflightResult(True, tuple(exit_codes), tuple(completed))


def _explicit_binary_path(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    if not raw or "\x00" in raw:
        raise ValueError("emulator_bin must be an explicit absolute path")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("emulator_bin must be an explicit absolute path")
    return str(path)


def _safe_root(value: str | os.PathLike[str], cwd: Path) -> str:
    raw = os.fspath(value)
    if not raw or "\x00" in raw:
        raise ValueError("root paths must be non-empty paths")
    path = Path(raw)
    if ".." in path.parts:
        raise ValueError("root path escapes its workspace")
    resolved = path.resolve() if path.is_absolute() else (cwd / path).resolve()
    if not path.is_absolute():
        try:
            resolved.relative_to(cwd)
        except ValueError as exc:
            raise ValueError("root path escapes its workspace") from exc
    return str(resolved)


def _order_ranks(
    order: Sequence[str | os.PathLike[str]] | None,
    roots: list[str],
    cwd: Path,
) -> dict[str, int]:
    if order is None:
        return {root: index for index, root in enumerate(roots)}
    if isinstance(order, (str, bytes)):
        raise ValueError("order must be a sequence of root paths")
    rank: dict[str, int] = {}
    for index, item in enumerate(order):
        root = _safe_root(item, cwd)
        if root not in roots:
            raise ValueError("order references a root not present in root_paths")
        if root in rank:
            raise ValueError("order contains a duplicate root")
        rank[root] = index
    next_rank = len(rank)
    for root in roots:
        if root not in rank:
            rank[root] = next_rank
            next_rank += 1
    return rank


def _topological_order(
    roots: list[str],
    dependencies: Mapping[str, set[str]],
    rank: Mapping[str, int],
) -> list[str]:
    remaining = {root: set(items) for root, items in dependencies.items()}
    result: list[str] = []
    while remaining:
        ready = sorted(
            (root for root, required in remaining.items() if not required),
            key=lambda root: (rank[root], root),
        )
        if not ready:
            raise ValueError("dependency graph contains a cycle")
        for root in ready:
            result.append(root)
            del remaining[root]
        for required in remaining.values():
            required.difference_update(ready)
    return result


def _provider_versions(
    versions: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    if versions is None:
        return ()
    result: list[tuple[str, str]] = []
    for name, version in sorted(versions.items()):
        if not isinstance(name, str) or not _VERSION_RE.fullmatch(name):
            raise ValueError("provider names must be safe identifiers")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise ValueError("provider versions must be safe identifiers")
        result.append((name, version))
    return tuple(result)
