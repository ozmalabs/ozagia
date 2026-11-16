"""The versioned task-class library: built-ins plus repository overrides.

A task class is a :mod:`ozagia.core.tasks` specification. The library adds the
browse metadata the runner does not need: a domain, a source (built-in or
repository), the path and digest, and the defaults a caller would copy.

Built-ins live in package data, one file per class, grouped by domain::

    src/ozagia/task_specs/research/paper_triage.toml
    src/ozagia/task_specs/coding/diff_review.toml
    src/ozagia/task_specs/ops/log_inspect.toml

A repository overrides a built-in in ``<root>/ozagia/tasks/<id>.toml``. The
override must keep the built-in id and bump the version when its content
changes; a same-version edit is a conflict, and an older version is stale.
An unknown id is an error, because this first library exposes overrides, not
new classes. :func:`copy_task` is the supported way to start an override: it
previews by default and writes only with explicit confirmation.

No function here calls a model, reaches the network or executes a task.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ozagia.core import tasks as T

SCHEMA = 1
BUILTIN_DIRNAME = "task_specs"
REPO_CONFIG_DIRNAME = "ozagia"
REPO_TASKS_DIRNAME = "tasks"
DOMAINS = ("research", "coding", "ops")


class TaskLibraryError(ValueError):
    """The library, a lookup or a copy request is invalid.

    ``errors`` holds validation messages when the failure is a bad library or
    spec (the CLI exits 1); an empty list means a usage, lookup or IO error
    (the CLI exits 2).
    """

    def __init__(self, message: str, *,
                 errors: Iterable[str] = ()) -> None:
        self.errors = list(errors)
        super().__init__(message)


@dataclass(frozen=True)
class Issue:
    """One validation problem, with a stable code and location."""

    code: str
    message: str
    severity: str = "error"
    task_id: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "task_id": self.task_id,
            "path": self.path,
        }


@dataclass(frozen=True)
class LibraryTask:
    """A validated task class plus its library metadata."""

    spec: T.TaskSpec
    domain: str
    source: str
    path: Path
    builtin_version: int | None = None
    overrides: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def version(self) -> int:
        return self.spec.version

    def entry(self) -> dict[str, Any]:
        """Catalog metadata, safe to print and serialise as JSON."""
        spec = self.spec
        return {
            "id": spec.id,
            "domain": self.domain,
            "version": spec.version,
            "title": spec.title,
            "description": spec.objective,
            "source": self.source,
            "path": str(self.path),
            "digest": spec.digest,
            "overrides": self.overrides,
            "builtin_version": self.builtin_version,
            "warnings": list(self.warnings),
            "defaults": {
                "worktree": spec.worktree.to_dict(),
                "resources": spec.resources.to_dict(),
                "budget": spec.budget.to_dict(),
                "gates": spec.gates.to_dict(),
                "approval": spec.approval.to_dict(),
                "output": spec.output,
                "tools": list(spec.tools),
                "write_scope": list(spec.write_scope),
                "requires_graph": spec.requires_graph,
                "steps": len(spec.steps),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Catalog metadata plus the full normalised specification."""
        return {"entry": self.entry(), "spec": self.spec.to_dict()}

    def read_text(self) -> str:
        """The exact TOML source, for a preview or a copy."""
        return self.path.read_text(encoding="utf-8")


@dataclass
class Library:
    """Built-ins and accepted overrides, with every validation issue."""

    root: Path | None
    builtin_dir: Path
    tasks: dict[str, LibraryTask] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    builtin_count: int = 0
    repo_count: int = 0

    def errors(self) -> list[str]:
        return [issue.message for issue in self.issues
                if issue.severity == "error"]

    def warnings(self) -> list[str]:
        return [issue.message for issue in self.issues
                if issue.severity == "warning"]

    def sorted_tasks(self) -> list[LibraryTask]:
        order = {domain: index for index, domain in enumerate(DOMAINS)}
        return sorted(
            self.tasks.values(),
            key=lambda task: (order.get(task.domain, len(DOMAINS)), task.id),
        )

    def entries(self) -> list[dict[str, Any]]:
        return [task.entry() for task in self.sorted_tasks()]


# --------------------------------------------------------------------------- #
# locations and loading
# --------------------------------------------------------------------------- #
def builtin_dir() -> Path:
    """The package data directory that holds the built-in specs."""
    return Path(__file__).resolve().parent.parent / BUILTIN_DIRNAME


def repo_tasks_dir(root: str | Path) -> Path:
    """``<root>/ozagia/tasks``, the repository override directory."""
    return (Path(root).expanduser().resolve()
            / REPO_CONFIG_DIRNAME / REPO_TASKS_DIRNAME)


def _issue(code: str, message: str, *, severity: str = "error",
           task_id: str | None = None, path: Path | None = None) -> Issue:
    return Issue(code=code, message=message, severity=severity,
                 task_id=task_id, path=str(path) if path is not None else None)


def _load_spec(path: Path, *, issues: list[Issue]) -> T.TaskSpec | None:
    """Load one spec, converting validation failures into issues."""
    try:
        spec = T.load_spec(path)
    except T.TaskSpecError as exc:
        for message in exc.errors:
            issues.append(_issue("invalid_spec", f"{path}: {message}",
                                 path=path))
        for message in exc.warnings:
            issues.append(_issue("spec_warning", f"{path}: {message}",
                                 severity="warning", path=path))
        return None
    for message in spec.warnings:
        issues.append(_issue("spec_warning", f"{path}: {message}",
                             severity="warning", task_id=spec.id, path=path))
    return spec


def _load_builtins(library: Library) -> None:
    directory = library.builtin_dir
    if not directory.is_dir():
        raise TaskLibraryError(
            f"built-in task specs directory is missing: {directory}")
    for child in sorted(directory.iterdir()):
        if child.is_dir() and child.name not in DOMAINS:
            library.issues.append(_issue(
                "unknown_domain",
                f"{child}: unknown task domain {child.name!r}; expected "
                + ", ".join(DOMAINS),
                path=child,
            ))
    paths = sorted(directory.rglob("*.toml"))
    if not paths:
        raise TaskLibraryError(
            f"no built-in task specs found under {directory}")
    for path in paths:
        relative = path.relative_to(directory)
        if len(relative.parts) != 2 or relative.parts[0] not in DOMAINS:
            library.issues.append(_issue(
                "library_layout",
                f"{path}: built-in specs must live in "
                f"<domain>/<id>.toml with a domain in {', '.join(DOMAINS)}",
                path=path,
            ))
            continue
        domain = relative.parts[0]
        spec = _load_spec(path, issues=library.issues)
        if spec is None:
            continue
        existing = library.tasks.get(spec.id)
        if existing is not None:
            library.issues.append(_issue(
                "duplicate_builtin",
                f"duplicate built-in task class {spec.id!r}: "
                f"{existing.path} and {path}",
                task_id=spec.id, path=path,
            ))
            continue
        library.tasks[spec.id] = LibraryTask(
            spec=spec,
            domain=domain,
            source="builtin",
            path=path,
            builtin_version=spec.version,
            overrides=False,
            warnings=spec.warnings,
        )
        library.builtin_count += 1


def _load_repo(root: Path, library: Library) -> None:
    directory = repo_tasks_dir(root)
    if not directory.is_dir():
        return
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.toml")):
        spec = _load_spec(path, issues=library.issues)
        if spec is None:
            continue
        builtin = library.tasks.get(spec.id)
        if builtin is None:
            available = ", ".join(sorted(library.tasks)) or "(none)"
            library.issues.append(_issue(
                "unknown_override",
                f"{path}: task class {spec.id!r} does not override a built-in; "
                f"available: {available}",
                task_id=spec.id, path=path,
            ))
            continue
        if spec.id in seen:
            library.issues.append(_issue(
                "duplicate_override",
                f"duplicate repository override for task class {spec.id!r}: "
                f"{seen[spec.id]} and {path}",
                task_id=spec.id, path=path,
            ))
            continue
        seen[spec.id] = path
        if spec.version < builtin.version:
            library.issues.append(_issue(
                "stale_override",
                f"{path}: version {spec.version} is older than built-in "
                f"{spec.id} version {builtin.version}; bump the override "
                "version",
                task_id=spec.id, path=path,
            ))
            continue
        if spec.version == builtin.version and spec.digest != builtin.spec.digest:
            library.issues.append(_issue(
                "version_conflict",
                f"{path}: version {spec.version} conflicts with built-in "
                f"{spec.id} version {builtin.version} because the content "
                "differs; bump the override version",
                task_id=spec.id, path=path,
            ))
            continue
        library.tasks[spec.id] = LibraryTask(
            spec=spec,
            domain=builtin.domain,
            source="repo",
            path=path,
            builtin_version=builtin.version,
            overrides=True,
            warnings=spec.warnings,
        )
        library.repo_count += 1


def load_library(root: str | Path | None = None) -> Library:
    """Load built-ins and, when a root is given, repository overrides.

    Spec and override problems are returned as issues; only a missing built-in
    directory or a missing repository root raises.
    """
    root_p = Path(root).expanduser().resolve() if root is not None else None
    library = Library(root=root_p, builtin_dir=builtin_dir())
    _load_builtins(library)
    if root_p is not None:
        if not root_p.is_dir():
            raise TaskLibraryError(
                f"repository root does not exist: {root_p}")
        _load_repo(root_p, library)
    return library


def _require_valid(library: Library) -> None:
    errors = library.errors()
    if errors:
        raise TaskLibraryError(
            "task library is invalid: " + "; ".join(errors), errors=errors)


# --------------------------------------------------------------------------- #
# public lookup and validation
# --------------------------------------------------------------------------- #
def _check_domain(domain: str) -> None:
    if not isinstance(domain, str) or domain not in DOMAINS:
        raise TaskLibraryError(
            f"unknown task domain {domain!r}; expected one of "
            + ", ".join(DOMAINS))


def list_tasks(root: str | Path | None = None, *,
               domain: str | None = None) -> list[dict[str, Any]]:
    """Catalog metadata for every available task class, by domain then id."""
    library = load_library(root)
    _require_valid(library)
    if domain is not None:
        _check_domain(domain)
    return [task.entry() for task in library.sorted_tasks()
            if domain is None or task.domain == domain]


def get_task(task_id: str, *,
             root: str | Path | None = None) -> LibraryTask:
    """One task class by id, with repository overrides applied."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskLibraryError("task id must be a non-empty string")
    library = load_library(root)
    _require_valid(library)
    task = library.tasks.get(task_id.strip())
    if task is None:
        available = ", ".join(sorted(library.tasks))
        raise TaskLibraryError(
            f"unknown task class {task_id!r}; available: {available}")
    return task


def catalog(root: str | Path | None = None) -> dict[str, Any]:
    """The whole library as one deterministic metadata object."""
    library = load_library(root)
    _require_valid(library)
    domains = []
    for domain in DOMAINS:
        tasks = [task for task in library.sorted_tasks()
                 if task.domain == domain]
        domains.append({
            "id": domain,
            "count": len(tasks),
            "task_ids": [task.id for task in tasks],
        })
    return {
        "schema": SCHEMA,
        "root": str(library.root) if library.root is not None else None,
        "builtin_dir": str(library.builtin_dir),
        "counts": {
            "builtin": library.builtin_count,
            "repo": library.repo_count,
            "total": len(library.tasks),
        },
        "domains": domains,
        "tasks": library.entries(),
    }


def validate_library(root: str | Path | None = None) -> dict[str, Any]:
    """Validate built-ins and overrides without running or copying anything.

    Returns ``ok``, ``errors``, ``warnings``, structured ``issues``, counts and
    the catalog entries that did load. A bad spec is an error, not an
    exception, so a caller can report every problem at once.
    """
    library = load_library(root)
    errors = library.errors()
    return {
        "schema": SCHEMA,
        "ok": not errors,
        "root": str(library.root) if library.root is not None else None,
        "builtin_dir": str(library.builtin_dir),
        "counts": {
            "builtin": library.builtin_count,
            "repo": library.repo_count,
            "total": len(library.tasks),
        },
        "errors": errors,
        "warnings": library.warnings(),
        "issues": [issue.to_dict() for issue in library.issues],
        "tasks": library.entries(),
    }


# --------------------------------------------------------------------------- #
# copy workflow
# --------------------------------------------------------------------------- #
def _resolve_destination(root: Path, destination: str | Path,
                         task_id: str) -> Path:
    if not isinstance(destination, (str, Path)) or not str(destination).strip():
        raise TaskLibraryError("destination must be a non-empty path")
    raw = Path(destination).expanduser()
    if not raw.is_absolute():
        raw = root / raw
    raw = raw.resolve()
    text = str(destination).replace("\\", "/")
    if raw.is_dir() or text.endswith("/"):
        raw = raw / f"{task_id}.toml"
    if raw.suffix.lower() != ".toml":
        raise TaskLibraryError(
            f"destination must be a .toml file or a directory: {raw}")
    return raw


def _unified_diff(path: Path, old: str | None, new: str) -> str:
    return "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=str(path) if old is not None else "/dev/null",
        tofile=str(path),
    ))


def _atomic_write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, path)
        except BaseException:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise TaskLibraryError(f"cannot write {path}: {exc}") from exc


def copy_task(root: str | Path, task_id: str, destination: str | Path, *,
              dry_run: bool = True, confirm: bool = False,
              force: bool = False) -> dict[str, Any]:
    """Copy a task class into the repository config.

    ``dry_run`` is the default and writes nothing. A real write needs both
    ``dry_run=False`` and ``confirm=True``; ``force`` replaces an existing
    file. The destination must resolve inside ``<root>/ozagia/tasks``.
    """
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        raise TaskLibraryError(f"repository root does not exist: {root_p}")
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskLibraryError("task id must be a non-empty string")
    task_id = task_id.strip()
    if not dry_run and not confirm:
        raise TaskLibraryError(
            "copy requires explicit confirmation: pass confirm=True "
            "(or use --apply)")
    destination_path = _resolve_destination(root_p, destination, task_id)
    tasks_root = repo_tasks_dir(root_p)
    try:
        destination_path.relative_to(tasks_root)
    except ValueError:
        raise TaskLibraryError(
            f"destination must be inside {tasks_root}: {destination_path}"
        ) from None
    if destination_path.is_dir():
        raise TaskLibraryError(
            f"destination is a directory: {destination_path}")

    # A broken destination override must not make the file impossible to
    # repair. Fall back to the built-in source only when every library error
    # belongs to that destination; another broken override is still reported.
    library_errors: list[str] = []
    try:
        task = get_task(task_id, root=root_p)
    except TaskLibraryError as exc:
        if not exc.errors:
            raise
        library = load_library(root_p)
        errors = [issue for issue in library.issues
                  if issue.severity == "error"]
        if (not errors
                or any(issue.path != str(destination_path)
                       for issue in errors)):
            raise
        builtin_task = load_library(None).tasks.get(task_id)
        if builtin_task is None:
            raise
        task = builtin_task
        library_errors = list(exc.errors)

    text = task.read_text()
    existing: str | None = None
    if destination_path.is_file():
        try:
            existing = destination_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TaskLibraryError(
                f"cannot read {destination_path}: {exc}") from exc
    unchanged = existing == text
    if existing is not None and not unchanged and not force:
        raise TaskLibraryError(
            f"{destination_path} already exists with different content; "
            "pass force=True to overwrite")
    preview = dry_run or unchanged
    if not preview:
        _atomic_write(destination_path, text)
    encoded = text.encode("utf-8")
    return {
        "schema": SCHEMA,
        "ok": True,
        "action": "copy_task",
        "task_id": task.id,
        "domain": task.domain,
        "version": task.version,
        "source": str(task.path),
        "source_kind": task.source,
        "fallback": bool(library_errors),
        "library_errors": library_errors,
        "destination": str(destination_path),
        "relative_destination": destination_path.relative_to(
            root_p).as_posix(),
        "dry_run": bool(dry_run),
        "written": not preview,
        "unchanged": unchanged,
        "overwrote": bool(existing is not None and not unchanged),
        "bytes": len(encoded),
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "diff": _unified_diff(destination_path, existing, text),
    }


__all__ = [
    "BUILTIN_DIRNAME",
    "DOMAINS",
    "Issue",
    "Library",
    "LibraryTask",
    "SCHEMA",
    "TaskLibraryError",
    "builtin_dir",
    "catalog",
    "copy_task",
    "get_task",
    "list_tasks",
    "load_library",
    "repo_tasks_dir",
    "validate_library",
]
