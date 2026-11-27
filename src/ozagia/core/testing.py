"""Deterministic test policy and affected-test planning.

This module answers one question: *which test files should a caller run for
this change?* It does not run them. A plan is a selection with its evidence,
its omissions and the reasons for both; a subset run is never described as a
full pass.

Modes, least to most inclusive:

``none``
    No tests. A deliberate decision for docs-only, config-only or review-only
    work.
``smoke``
    Test files the graph links to the changed files.
``affected``
    Graph-linked tests, changed test files, tests that failed in the last
    known run, flaky tests and tests of the changed files' packages.
``package``
    All tests in the changed files' packages/modules, still capped.
``all``
    Every discovered test file.

Policy precedence (highest first): a task specification's own
``test_policy``, the repository ``[testing.tasks]`` entry for the task class,
a matching repository ``[testing.paths]`` glob, the repository
``ozagia/config.toml [testing]`` values (through the typed settings registry),
then the built-in default. The default for a bounded task is ``affected``;
the interactive captured default is ``testing.captured`` (``all``).

Planning is read-only:

- changed files come from an explicit list or ``git diff``; untracked files
  are included when the comparison is against the working tree;
- an existing ``.ozagia/graph.json`` is loaded read-only and is only trusted
  when it is fresh; a missing, stale, unknown or unreadable snapshot is
  reported and falls back;
- graph evidence is ``File -has_test-> Test``, ``tests_module``,
  ``mentions_file`` and :func:`ozagia.intent.common.covering_tests`;
- changed test files, tests that failed last time and flaky tests are always
  selected (whole files) unless the mode is explicitly ``none``;
- a changed file with no linked test uses ``fallback`` (``all`` by default,
  ``refuse`` for strict tasks, or ``affected_with_warning`` for a best-effort
  subset that does not claim affected coverage);
- ``max_tests`` caps the selection, but mandatory tests (changed, failed,
  flaky and explicitly declared complete-coverage tests) are never silently
  dropped; the plan reports an exceeded cap.

The plan reports ``mode``, changed files, selected test files and node ids,
counts, omitted files, fallback reason, graph freshness and
``not_assessed``. ``truth_assessed`` is always false.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import state_path, state_rel
from ozagia.core import settings as S

SCHEMA = "ozagia.testing.plan.v1"
MODES = S.TESTING_MODES
FALLBACKS = S.TESTING_FALLBACKS
MODE_RANK = {mode: rank for rank, mode in enumerate(MODES)}

DEFAULT_MODE = "affected"
DEFAULT_FALLBACK = "all"
DEFAULT_MAX_TESTS = 50
DEFAULT_TIMEOUT_S = 300
DEFAULT_CAPTURED = "all"

#: How the interactive captured-pytest executor (:mod:`ozagia.intent.affected`)
#: maps a configured mode. It implements `all` and `affected`; `none`/`off`
#: disables selection and runs the command as typed. `smoke` uses the affected
#: subset, which is over-inclusion; `package` is broader than the implemented
#: selector, so it runs all. An unimplemented mode is never reported as
#: honoured, and a mode never runs fewer tests than it requested.
CAPTURED_SELECT = {
    "none": "all",
    "smoke": "affected",
    "affected": "affected",
    "package": "all",
    "all": "all",
}

GIT_TIMEOUT_S = 20
MAX_LISTED = 200
MAX_TEST_FILES = 50_000
_TEST_KEY_PREFIX = "test:"

_CACHE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".git", ".ozagia",
})
_DOC_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt",
})
_CONFIG_SUFFIXES = frozenset({
    ".toml", ".yaml", ".yml", ".jsonc", ".ini", ".cfg", ".conf",
    ".lock", ".xml", ".properties",
})
_CONFIG_NAMES = frozenset({
    "pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", ".gitignore",
    ".gitattributes", "requirements.txt", "requirements-dev.txt",
    "Makefile", "Dockerfile",
})
_TEST_SUFFIXES = frozenset({
    ".py", ".pyi", ".go", ".rs", ".js", ".jsx", ".mjs", ".cjs", ".ts",
    ".tsx", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".scala",
})
_TEST_NAME = re.compile(
    r"(?:^test_.*|.*_test|.*\.test|.*\.spec)\.(?:py|pyi|go|rs|js|jsx|mjs|cjs|"
    r"ts|tsx|java|kt|rb|php|cs|swift|scala)\Z")
_TEST_DIR_NAMES = frozenset({"tests", "test", "spec", "specs", "__tests__"})


class TestPolicyError(ValueError):
    """A test policy, path or plan request is invalid."""


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TestPolicy:
    """The resolved test policy for one caller."""

    mode: str = DEFAULT_MODE
    fallback: str = DEFAULT_FALLBACK
    max_tests: int = DEFAULT_MAX_TESTS
    timeout_s: int = DEFAULT_TIMEOUT_S
    captured: str = DEFAULT_CAPTURED
    tasks: Mapping[str, str] = field(default_factory=dict)
    paths: Mapping[str, str] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise TestPolicyError(
                f"mode must be one of {', '.join(MODES)}, got {self.mode!r}")
        if self.fallback not in FALLBACKS:
            raise TestPolicyError(
                f"fallback must be one of {', '.join(FALLBACKS)}, "
                f"got {self.fallback!r}")
        if isinstance(self.max_tests, bool) or not isinstance(
                self.max_tests, int) or not 1 <= self.max_tests <= 100_000:
            raise TestPolicyError("max_tests must be an integer between 1 and "
                                  "100000")
        if isinstance(self.timeout_s, bool) or not isinstance(
                self.timeout_s, int) or not 1 <= self.timeout_s <= 86_400:
            raise TestPolicyError("timeout_s must be an integer between 1 and "
                                  "86400")
        if self.captured not in MODES:
            raise TestPolicyError(
                f"captured must be one of {', '.join(MODES)}, "
                f"got {self.captured!r}")
        for name, table in (("tasks", self.tasks), ("paths", self.paths)):
            if not isinstance(table, Mapping):
                raise TestPolicyError(f"{name} must be a mapping")
            for key, value in table.items():
                if not isinstance(key, str) or not key:
                    raise TestPolicyError(f"{name} keys must be non-empty strings")
                if value not in MODES:
                    raise TestPolicyError(
                        f"{name}[{key!r}] must be one of {', '.join(MODES)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fallback": self.fallback,
            "max_tests": self.max_tests,
            "timeout_s": self.timeout_s,
            "captured": self.captured,
            "tasks": dict(self.tasks),
            "paths": dict(self.paths),
            "sources": dict(self.sources),
        }

    @property
    def task_overrides(self) -> dict[str, str]:
        return dict(self.tasks)

    @property
    def path_overrides(self) -> dict[str, str]:
        return dict(self.paths)


def _mode(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or value not in MODES:
        raise TestPolicyError(
            f"{where}: must be one of {', '.join(MODES)}, got {value!r}")
    return value


def _fallback(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or value not in FALLBACKS:
        raise TestPolicyError(
            f"{where}: must be one of {', '.join(FALLBACKS)}, got {value!r}")
    return value


def captured_select(mode: str) -> str:
    """The captured-pytest selector for a configured mode.

    ``off`` is accepted as an alias for ``none``. The result is always one of
    the two selectors the existing executor implements: ``all`` or
    ``affected``.
    """
    text = mode.strip().lower() if isinstance(mode, str) else mode
    if text == "off":
        text = "none"
    if text not in CAPTURED_SELECT:
        raise TestPolicyError(
            f"captured mode must be one of {', '.join(MODES)} or 'off', "
            f"got {mode!r}")
    return CAPTURED_SELECT[text]


def _int_setting(value: Any, *, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TestPolicyError(f"{where}: must be an integer")
    if not low <= value <= high:
        raise TestPolicyError(f"{where}: must be between {low} and {high}")
    return value


def _normal_paths(paths: Any) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    result: list[str] = []
    for raw in paths:
        if not isinstance(raw, (str, os.PathLike)):
            raise TestPolicyError(f"path must be a string, got {raw!r}")
        rel = str(raw).replace("\\", "/").strip()
        if not rel:
            continue
        if rel.startswith("/") or re.match(r"^[A-Za-z]:/", rel):
            raise TestPolicyError(f"path must be repository-relative: {rel!r}")
        parts = [part for part in rel.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise TestPolicyError(f"path must stay inside the repository: {rel!r}")
        result.append("/".join(parts))
    return result


def _pattern_matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    # A directory named in a path override also selects its `/**` rule.
    if pattern.endswith("/**") and fnmatch.fnmatchcase(path, pattern[:-3]):
        return True
    return False


def _matching_path_mode(paths: Sequence[str],
                        table: Mapping[str, str]) -> str | None:
    """The most inclusive override matching any path (TOML order per path)."""
    best: str | None = None
    best_rank = -1
    for path in paths:
        for pattern, value in table.items():
            if _pattern_matches(path, pattern):
                rank = MODE_RANK.get(value, -1)
                if rank > best_rank:
                    best, best_rank = value, rank
                break
    return best


def load_policy(root: str | Path, *, task_class: str | None = None,
                paths: Any = None, task_mode: str | None = None,
                captured: bool = False) -> TestPolicy:
    """Resolve the test policy from the settings registry and overrides.

    Precedence: ``task_mode`` (a task specification) before
    ``[testing.tasks]`` for ``task_class`` before a matching
    ``[testing.paths]`` glob before ``[testing]`` in the repository or user
    config before the built-in default. ``captured=True`` selects
    ``testing.captured`` for an interactive captured pytest.
    """
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        raise TestPolicyError(f"repository root does not exist: {root_p}")
    values = S.values(root_p)
    tables = S.testing_overrides(root_p)

    mode_source = "default"
    fallback_source = values["testing.fallback"]["source"]
    max_source = values["testing.max_tests"]["source"]
    timeout_source = values["testing.timeout_s"]["source"]
    captured_mode = _mode(values["testing.captured"]["value"],
                          where="testing.captured")
    fallback = _fallback(values["testing.fallback"]["value"],
                         where="testing.fallback")
    max_tests = _int_setting(values["testing.max_tests"]["value"],
                             where="testing.max_tests", low=1, high=100_000)
    timeout_s = _int_setting(values["testing.timeout_s"]["value"],
                             where="testing.timeout_s", low=1, high=86_400)

    if captured:
        mode = captured_mode
        mode_source = "captured"
    elif task_mode is not None:
        mode = _mode(task_mode, where="task test_policy.mode")
        mode_source = "task"
    elif task_class is not None and task_class in tables["tasks"]:
        mode = tables["tasks"][task_class]
        mode_source = "task_class"
    else:
        matched = _matching_path_mode(_normal_paths(paths), tables["paths"])
        if matched is not None:
            mode = matched
            mode_source = "path"
        else:
            mode = _mode(values["testing.mode"]["value"],
                         where="testing.mode")
            mode_source = values["testing.mode"]["source"]

    return TestPolicy(
        mode=mode,
        fallback=fallback,
        max_tests=max_tests,
        timeout_s=timeout_s,
        captured=captured_mode,
        tasks=dict(tables["tasks"]),
        paths=dict(tables["paths"]),
        sources={
            "mode": mode_source,
            "fallback": fallback_source,
            "max_tests": max_source,
            "timeout_s": timeout_source,
            "captured": values["testing.captured"]["source"],
        },
    )


def with_overrides(policy: TestPolicy, *, mode: str | None = None,
                   fallback: str | None = None,
                   max_tests: int | None = None,
                   timeout_s: int | None = None) -> TestPolicy:
    """A copy of ``policy`` with a task specification's overrides applied."""
    sources = dict(policy.sources)
    if mode is not None:
        mode = _mode(mode, where="test_policy.mode")
        sources["mode"] = "task"
    if fallback is not None:
        fallback = _fallback(fallback, where="test_policy.fallback")
        sources["fallback"] = "task"
    if max_tests is not None:
        max_tests = _int_setting(max_tests, where="test_policy.max_tests",
                                 low=1, high=100_000)
        sources["max_tests"] = "task"
    if timeout_s is not None:
        timeout_s = _int_setting(timeout_s, where="test_policy.timeout_s",
                                 low=1, high=86_400)
        sources["timeout_s"] = "task"
    return replace(
        policy,
        mode=policy.mode if mode is None else mode,
        fallback=policy.fallback if fallback is None else fallback,
        max_tests=policy.max_tests if max_tests is None else max_tests,
        timeout_s=policy.timeout_s if timeout_s is None else timeout_s,
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# git and files
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> bytes | None:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
           "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], env=env,
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _split_z(out: bytes) -> list[str]:
    return [part.decode("utf-8", "surrogateescape")
            for part in out.split(b"\0") if part]


def _head(root: Path) -> str | None:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    return out.decode().strip() if out else None


def _resolve_commit(root: Path, rev: str) -> str | None:
    out = _git(root, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
    return out.decode().strip() if out else None


def _excluded(rel: str, state: str | None) -> bool:
    parts = PurePosixPath(rel).parts
    if any(part in _CACHE_DIRS for part in parts):
        return True
    if rel.endswith((".pyc", ".pyo")):
        return True
    return bool(state) and (rel == state or rel.startswith(state + "/"))


def _diff_names(root: Path, spec: str) -> list[str]:
    out = _git(root, "diff", "--name-status", "-z", "--no-renames",
               "--relative", spec, "--")
    if out is None:
        raise TestPolicyError(f"git cannot diff {spec} in {root}")
    tokens = _split_z(out)
    changed: list[str] = []
    index = 0
    while index + 1 < len(tokens):
        status = tokens[index]
        path = tokens[index + 1]
        index += 2
        if status.startswith(("R", "C")) and index < len(tokens):
            # --no-renames normally prevents this; keep the old path if a
            # caller's git configuration turns renames back on.
            changed.extend([path, tokens[index]])
            index += 1
        else:
            changed.append(path)
    return changed


def _untracked(root: Path) -> list[str]:
    out = _git(root, "ls-files", "-z", "--others", "--exclude-standard", "--")
    if out is None:
        raise TestPolicyError(f"git cannot list untracked files in {root}")
    return _split_z(out)


def changed_files(root: str | Path, *, base: str = "HEAD",
                  head: str | None = None,
                  changed: Any = None) -> list[str]:
    """Repository-relative changed files, deterministically ordered."""
    root_p = Path(root).expanduser().resolve()
    if changed is not None:
        return sorted(set(_normal_paths(changed)))
    state = state_rel(root_p)
    paths: list[str] = []
    if head is not None:
        paths = _diff_names(root_p, f"{base}..{head}")
    else:
        paths = _diff_names(root_p, base)
    current = _head(root_p)
    include_untracked = head is None
    if head is not None:
        target = _resolve_commit(root_p, head)
        include_untracked = bool(target and current and target == current)
    if include_untracked:
        paths.extend(_untracked(root_p))
    return sorted({path for path in paths
                   if path and not _excluded(path, state)})


def _is_docs(rel: str) -> bool:
    path = PurePosixPath(rel)
    return (path.suffix.lower() in _DOC_SUFFIXES
            or (path.parts and path.parts[0] in {"docs", "doc"}))


def _is_config(rel: str) -> bool:
    path = PurePosixPath(rel)
    name = path.name
    if name in _CONFIG_NAMES:
        return True
    if path.suffix.lower() in _CONFIG_SUFFIXES:
        return True
    return bool(path.parts and path.parts[0] in {"ozagia", ".github"})


def _is_test_file(rel: str, test_dirs: Sequence[str]) -> bool:
    path = PurePosixPath(rel)
    name = path.name
    if name == "conftest.py":
        return False
    suffix = path.suffix.lower()
    if suffix not in _TEST_SUFFIXES:
        return False
    if _TEST_NAME.match(name):
        return True
    # A conventional test directory alone is not enough: pytest only collects
    # `test_*.py` and `*_test.py` by default. A file whose nearest test-dir
    # ancestor is configured and whose name carries a test dot/suffix is
    # handled above.
    return any(part in _TEST_DIR_NAMES for part in path.parts[:-1]) and (
        name.startswith("test_") or "_test." in name or ".test." in name
        or ".spec." in name)


def _classify(rel: str, test_dirs: Sequence[str]) -> str:
    if _is_test_file(rel, test_dirs):
        return "test"
    if _is_docs(rel):
        return "docs"
    if _is_config(rel):
        return "config"
    return "source"


def _test_dirs(root: Path) -> list[str]:
    try:
        value = S.get(root, "index.test_dirs")["value"]
    except (S.SettingsError, OSError, ValueError):
        return ["tests"]
    return [str(item).rstrip("/") for item in value if str(item).strip()]


def _repo_files(root: Path) -> list[str] | None:
    out = _git(root, "ls-files", "-z", "-c", "-o", "--exclude-standard")
    if out is None:
        return None
    state = state_rel(root)
    return sorted({rel for rel in _split_z(out) if not _excluded(rel, state)})


def _walk_files(root: Path) -> list[str]:
    """A bounded fallback for a non-git tree."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _CACHE_DIRS)
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in sorted(filenames):
            found.append(f"{rel_dir}/{name}" if rel_dir else name)
            if len(found) >= MAX_TEST_FILES:
                return found
    return found


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
def _graph_info(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "available": False,
        "freshness": "missing",
        "usable": False,
        "snapshot_head": None,
        "snapshot_root": None,
        "head": None,
        "nodes": 0,
        "edges": 0,
        "reason": None,
    }


def _load_graph(root: Path, graph_path: str | Path | None,
                head_commit: str | None) -> tuple[
                    dict[str, Any], dict[str, Any] | None]:
    """Load an existing snapshot read-only and decide whether it is usable."""
    if graph_path is None:
        path = state_path(root, "graph.json")
    else:
        path = Path(graph_path).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
    info = _graph_info(path, root)
    info["head"] = head_commit
    if not path.is_file():
        info["reason"] = f"no graph snapshot at {path}"
        return info, None
    try:
        from ozagia.core.memory import DevMemory
        dev = DevMemory(dump=path, root=root)
        staleness = dev.staleness()
    except Exception as exc:  # noqa: BLE001 - any load failure is "error"
        info["available"] = True
        info["freshness"] = "error"
        info["reason"] = f"cannot read graph snapshot: {exc}"
        return info, None
    info["available"] = True
    repo_attrs: dict[str, Any] = {}
    for nid in dev.nodes_of_type("Repo"):
        raw = (dev.node(nid) or {}).get("attrs")
        if isinstance(raw, Mapping):
            repo_attrs = dict(raw)
            break
    info.update({
        "snapshot_head": repo_attrs.get("head"),
        "snapshot_root": repo_attrs.get("root"),
        "nodes": len(dev.nodes),
        "edges": len(dev.edges),
    })
    reasons: list[str] = []
    snapshot_root = repo_attrs.get("root")
    if snapshot_root:
        try:
            if Path(str(snapshot_root)).resolve() != root:
                reasons.append(f"snapshot root {snapshot_root} is not {root}")
        except (OSError, RuntimeError, ValueError):
            reasons.append(f"snapshot root {snapshot_root} cannot be resolved")
    snapshot_head = repo_attrs.get("head")
    if head_commit and snapshot_head and str(snapshot_head) != head_commit:
        reasons.append(
            f"snapshot head {str(snapshot_head)[:12]} does not match "
            f"planned head {head_commit[:12]}")
    if reasons:
        info["freshness"] = "stale"
        info["reason"] = "; ".join(reasons)
        return info, None
    freshness = str(staleness.get("freshness") or "unknown")
    info["freshness"] = freshness
    if freshness == "fresh":
        info["reason"] = "snapshot root, head and working-tree digest match"
    elif freshness == "stale":
        detail: list[str] = []
        if staleness.get("root_mismatch"):
            detail.append("snapshot root differs")
        if staleness.get("tree_changed"):
            detail.append("working tree changed since the snapshot")
        if (staleness.get("snapshot_head")
                and staleness.get("snapshot_head") != staleness.get("head")):
            detail.append("HEAD moved since the snapshot")
        info["reason"] = "; ".join(detail) or "snapshot is stale"
    else:
        errors = staleness.get("verification_errors") or []
        info["reason"] = ("; ".join(str(value) for value in errors)
                          or "graph freshness could not be verified")
    info["usable"] = freshness == "fresh"
    if not info["usable"]:
        return info, None
    return info, _graph_index(dev)


def _graph_index(dev: Any) -> dict[str, Any]:
    from ozagia.core.keys import key_file

    files_by_path: dict[str, int] = {}
    tests_by_file: dict[str, set[str]] = {}
    test_nodes: dict[int, tuple[str, str]] = {}
    for nid, node in dev.nodes.items():
        node_type = node.get("type")
        attrs = node.get("attrs")
        if not isinstance(attrs, Mapping):
            attrs = {}
        if node_type == "File":
            path = _safe_repo_relpath(attrs.get("path"))
            if path:
                files_by_path[path] = int(nid)
        elif node_type == "Test":
            path = _safe_repo_relpath(attrs.get("file")) or _safe_repo_relpath(
                key_file(str(dev.key_of(int(nid)) or "")))
            if not path:
                continue
            key = str(dev.key_of(int(nid)) or _TEST_KEY_PREFIX
                      + path + "::" + str(attrs.get("qualname") or ""))
            tests_by_file.setdefault(path, set()).add(key)
            test_nodes[int(nid)] = (path, key)
    file_tests: dict[str, set[str]] = {}
    file_for_node = {nid: path for path, nid in files_by_path.items()}
    for path, fid in files_by_path.items():
        file_tests.setdefault(path, set())
        try:
            from ozagia.intent.common import covering_tests
            for tid in covering_tests(dev, fid):
                got = test_nodes.get(int(tid))
                if got is not None:
                    file_tests[path].add(got[0])
        except Exception:  # noqa: BLE001 - direct edges below still decide
            pass
    # Accept either edge orientation: some builders record the relationship
    # from the Test to the File, others from the File to the Test.
    for edge in dev.edges:
        try:
            src, tgt = int(edge["src"]), int(edge["tgt"])
        except (KeyError, TypeError, ValueError):
            continue
        etype = str(edge.get("type") or "")
        if etype == "has_test":
            file_path = file_for_node.get(src, "")
            test = test_nodes.get(tgt)
            if file_path and test is not None:
                file_tests.setdefault(file_path, set()).add(test[0])
        elif etype in {"tests_module", "mentions_file", "tested_by_file"}:
            src_test = test_nodes.get(src)
            tgt_test = test_nodes.get(tgt)
            tgt_file = file_for_node.get(tgt, "")
            src_file = file_for_node.get(src, "")
            if src_test is not None and tgt_file:
                file_tests.setdefault(tgt_file, set()).add(src_test[0])
            elif tgt_test is not None and src_file:
                file_tests.setdefault(src_file, set()).add(tgt_test[0])
    return {
        "files_by_path": files_by_path,
        "tests_by_file": tests_by_file,
        "file_tests": file_tests,
    }


def _safe_repo_relpath(value: Any) -> str:
    """Return a canonical repository-relative graph path, or an empty string."""
    if not isinstance(value, str) or not value:
        return ""
    path = value.replace("\\", "/")
    if "\x00" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return ""
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _is_repo_file(root: Path, rel: str) -> bool:
    """Check that a relative candidate resolves to a file inside root."""
    safe = _safe_repo_relpath(rel)
    if not safe:
        return False
    try:
        path = (root / safe).resolve()
        path.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return path.is_file()


def _test_coverage_contract(root: Path, test_dirs: Sequence[str],
                            discovered: set[str]) -> dict[str, Any]:
    """Load optional, explicit complete data-to-test rules from project config.

    Graph adapter links are advisory. This separate contract is accepted only
    from validated, repository-owned TOML and requires an explicit completeness
    flag, changed-path globs, existing test files and a rationale.
    """
    result: dict[str, Any] = {"status": "not_configured", "rules": [],
                              "reason": None}
    project_config = root / ".ozagia.toml"
    if not project_config.exists() and not project_config.is_symlink():
        return result
    if not _is_repo_file(root, ".ozagia.toml"):
        result.update(status="invalid", reason=(
            ".ozagia.toml must resolve to a file inside the repository"))
        return result
    try:
        project_values = tomllib.loads(
            project_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        result.update(status="invalid", reason=(
            f"cannot load {project_config}: {exc}"))
        return result
    raw_section = project_values.get("test_coverage")
    if raw_section is None:
        return result
    if not isinstance(raw_section, Mapping) or set(raw_section) != {"rules"}:
        result.update(status="invalid", reason=(
            "[test_coverage] must define only rules"))
        return result
    raw_rules = raw_section.get("rules")
    if raw_rules is None:
        return result
    if not isinstance(raw_rules, list) or not raw_rules:
        result.update(status="invalid", reason="rules must be a non-empty array")
        return result
    rules: list[dict[str, Any]] = []
    allowed = {"paths", "tests", "complete", "evidence"}
    for number, raw in enumerate(raw_rules, 1):
        where = f"test_coverage.rules[{number}]"
        if not isinstance(raw, Mapping) or set(raw) != allowed:
            result.update(status="invalid", reason=(
                f"{where} must define exactly paths, tests, complete, evidence"))
            return result
        patterns = raw.get("paths")
        tests = raw.get("tests")
        evidence = raw.get("evidence")
        if raw.get("complete") is not True:
            result.update(status="invalid", reason=f"{where}.complete must be true")
            return result
        if (not isinstance(patterns, list) or not patterns
                or any(not isinstance(item, str) or not item.strip()
                       for item in patterns)):
            result.update(
                status="invalid",
                reason=f"{where}.paths must be a non-empty string array")
            return result
        normalized_patterns: list[str] = []
        for pattern in patterns:
            pattern = pattern.replace("\\", "/").strip()
            if (pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern)
                    or "\x00" in pattern
                    or any(part == ".." for part in pattern.split("/"))):
                result.update(status="invalid", reason=f"{where}.paths contains an unsafe pattern")
                return result
            normalized_patterns.append(pattern)
        if not isinstance(tests, list) or not tests:
            result.update(
                status="invalid",
                reason=f"{where}.tests must be a non-empty string array")
            return result
        normalized_tests: list[str] = []
        for test in tests:
            safe = _safe_repo_relpath(test)
            if (not safe or not _is_test_file(safe, test_dirs)
                    or safe not in discovered or not _is_repo_file(root, safe)):
                result.update(status="invalid", reason=(
                    f"{where}.tests contains a missing, unsafe, or undiscovered test"))
                return result
            normalized_tests.append(safe)
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            result.update(
                status="invalid",
                reason=(f"{where}.evidence must be a non-empty string up to "
                        "1000 characters"))
            return result
        rules.append({"paths": sorted(set(normalized_patterns)),
                      "tests": sorted(set(normalized_tests)),
                      "complete": True, "evidence": evidence.strip()})
    result.update(status="valid", rules=rules)
    return result


def _declared_coverage_for(path: str, contract: Mapping[str, Any]) -> set[str]:
    tests: set[str] = set()
    if contract.get("status") != "valid":
        return tests
    for rule in contract.get("rules", ()):
        if any(_pattern_matches(path, pattern) for pattern in rule["paths"]):
            tests.update(rule["tests"])
    return tests


def _discover_tests(root: Path, graph: Mapping[str, Any] | None,
                    test_dirs: Sequence[str]) -> set[str]:
    found: set[str] = set()
    if graph is not None:
        for path in (graph.get("tests_by_file") or {}):
            if _is_repo_file(root, path):
                found.add(path)
    files = _repo_files(root)
    if files is None:
        files = _walk_files(root)
    for rel in files:
        if len(found) >= MAX_TEST_FILES:
            break
        if rel and _is_test_file(rel, test_dirs) and _is_repo_file(root, rel):
            found.add(rel)
    return found


def _package_tests(root: Path, graph: Mapping[str, Any],
                   changed: Sequence[str],
                   discovered: Iterable[str]) -> set[str]:
    """Tests in the changed files' packages/modules.

    Uses graph coverage for every File node in the changed files' directories
    plus the conventional mirrored test directory (`tests/<pkg>` for
    `src/<pkg>/...`).
    """
    directories = {PurePosixPath(rel).parent.as_posix() for rel in changed}
    directories.discard("")
    tests: set[str] = set()
    files_by_path = graph.get("files_by_path") or {}
    file_tests = graph.get("file_tests") or {}
    for path in files_by_path:
        if PurePosixPath(path).parent.as_posix() in directories:
            tests.update(file_tests.get(path) or ())
    candidates: set[str] = set(directories)
    discovered_list = list(discovered)
    for directory in directories:
        parts = PurePosixPath(directory).parts
        candidates.add(directory)
        candidates.add("/".join((directory, "tests")))
        if len(parts) >= 2 and parts[0] == "src":
            # `src/pkg/mod.py` -> `tests/pkg`, but a root-level `src/app.py`
            # does not make every test a package test.
            package_parts = parts[1:]
            if package_parts:
                candidates.add("/".join(("tests", *package_parts)))
    for path in discovered_list:
        parent = PurePosixPath(path).parent.as_posix()
        for candidate in candidates:
            if candidate in ("", "."):
                continue
            if parent == candidate or parent.startswith(candidate + "/"):
                tests.add(path)
                break
    return {path for path in tests if _is_repo_file(root, path)}


# --------------------------------------------------------------------------- #
# last known test results
# --------------------------------------------------------------------------- #
def _result_state(root: Path) -> dict[str, Any]:
    path = state_path(root, "tests", "results.json")
    info: dict[str, Any] = {
        "available": False,
        "path": str(path),
        "failing": [],
        "flaky": [],
        "reason": None,
    }
    if not path.is_file():
        info["reason"] = "no test-result state recorded"
        return info
    try:
        from ozagia.intent import affected as A
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or raw.get("version") != A.VERSION:
            info["reason"] = "test-result state has an unsupported version"
            return info
        state = A.load_state(root)
    except (OSError, ValueError):
        info["reason"] = "test-result state is unreadable"
        return info
    info["available"] = True
    info["reason"] = "last known test results"
    failing: set[str] = set()
    for suite in (state.get("failing") or {}).values():
        if not isinstance(suite, Mapping):
            continue
        for rel in (suite.get("files") or {}):
            safe = _safe_repo_relpath(rel)
            if safe:
                failing.add(safe)
    flaky: set[str] = set()
    for nodeid in state.get("flaky") or {}:
        if isinstance(nodeid, str) and "::" in nodeid:
            rel = nodeid.split("::", 1)[0]
            safe = _safe_repo_relpath(rel)
            if safe:
                flaky.add(safe)
    info["failing"] = sorted(failing)
    info["flaky"] = sorted(flaky)
    return info


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #
def _reason_for_unlinked(path: str, root: Path) -> str:
    if PurePosixPath(path).name == "conftest.py":
        return f"conftest.py changed ({path}); it can affect tests in its tree"
    if not (root / path).exists():
        return f"{path} was deleted and no test is linked to it"
    return f"no test is linked to {path}"


def _best_effort(root: Path, changed: Sequence[str], graph: Mapping[str, Any]
                 | None, discovered: set[str], test_dirs: Sequence[str],
                 state: Mapping[str, Any],
                 changed_tests: set[str]) -> set[str]:
    selected = set(changed_tests)
    selected.update(state.get("failing") or ())
    selected.update(state.get("flaky") or ())
    if graph is not None:
        for path in changed:
            selected.update((graph.get("file_tests") or {}).get(path) or ())
        selected.update(_package_tests(root, graph, changed, discovered))
    else:
        for path in changed:
            stem = PurePosixPath(path).stem
            for candidate in discovered:
                if PurePosixPath(candidate).name in {
                        f"test_{stem}.py", f"{stem}_test.py",
                        f"{stem}_test.go"}:
                    selected.add(candidate)
    return {path for path in selected if _is_repo_file(root, path)}


def plan(root: str | Path, *, base: str = "HEAD", head: str | None = None,
         changed: Any = None, task_class: str | None = None,
         policy: TestPolicy | None = None,
         graph_path: str | Path | None = None,
         max_tests: int | None = None) -> dict[str, Any]:
    """Build a deterministic test plan. Tests are never executed."""
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        raise TestPolicyError(f"repository root does not exist: {root_p}")
    if policy is not None and isinstance(policy, Mapping):
        try:
            policy = TestPolicy(**dict(policy))
        except TypeError as exc:
            raise TestPolicyError(f"invalid test policy: {exc}") from None
    if policy is not None and not isinstance(policy, TestPolicy):
        raise TestPolicyError("policy must be a TestPolicy or a mapping")
    changed_paths = changed_files(root_p, base=base, head=head, changed=changed)
    if policy is None:
        policy = load_policy(root_p, task_class=task_class,
                             paths=changed_paths)
    if max_tests is not None:
        max_tests = _int_setting(max_tests, where="max_tests", low=1,
                                 high=100_000)
    cap = max_tests if max_tests is not None else policy.max_tests
    test_dirs = _test_dirs(root_p)
    head_commit = (_resolve_commit(root_p, head) if head is not None
                   else _head(root_p))
    graph_info, graph = _load_graph(root_p, graph_path, head_commit)
    if graph_info["freshness"] != "fresh":
        graph = None
    discovered = _discover_tests(root_p, graph, test_dirs)
    coverage_contract = _test_coverage_contract(root_p, test_dirs, discovered)
    state = _result_state(root_p)
    state_tests = {*(state.get("failing") or ()), *(state.get("flaky") or ())}
    missing_state = sorted(
        rel for rel in state_tests if not _is_repo_file(root_p, rel))
    for rel in state_tests:
        if _is_repo_file(root_p, rel):
            discovered.add(rel)
    kinds = {path: _classify(path, test_dirs) for path in changed_paths}
    changed_tests = {path for path, kind in kinds.items() if kind == "test"}
    missing_changed_tests = sorted(
        path for path in changed_tests if not _is_repo_file(root_p, path))
    code_changed = [path for path, kind in kinds.items()
                    if kind not in {"docs", "config"}]
    declared_by_file = {
        path: _declared_coverage_for(path, coverage_contract)
        for path in code_changed
    }
    warnings: list[str] = []
    if coverage_contract["status"] == "invalid":
        warnings.append(
            "complete test-coverage declaration is invalid; configured fallback applies: "
            + str(coverage_contract.get("reason") or "invalid declaration"))
    omitted: list[dict[str, str]] = []
    selected_sources: dict[str, set[str]] = {}
    fallback_used = False
    fallback_reason: str | None = None
    refused = False
    mode_reason: str | None = None
    effective = policy.mode

    def choose(paths: Iterable[str], source: str) -> set[str]:
        got = {path for path in paths if path in discovered}
        for path in got:
            selected_sources.setdefault(path, set()).add(source)
        return got

    def docs_only() -> bool:
        return bool(changed_paths) and not code_changed

    # Explicit none and empty/docs-only changes need no graph.
    if policy.mode == "none":
        effective = "none"
        mode_reason = "the test policy is none"
    elif not changed_paths and policy.mode != "all":
        effective = "none"
        mode_reason = "no files changed"
    elif docs_only() and policy.mode in {"smoke", "affected", "package"}:
        effective = "none"
        mode_reason = "documentation and configuration files changed only"

    fallback_needed = False
    if effective != "none":
        if policy.mode in {"smoke", "affected", "package"}:
            unlinked: list[str] = []
            package = (_package_tests(root_p, graph, code_changed, discovered)
                       if graph is not None and graph_info["freshness"] == "fresh"
                       and policy.mode in {
                           "affected", "package"} else set())
            for path in code_changed:
                if kinds[path] == "test":
                    if (graph is None or graph_info["freshness"] != "fresh") \
                            and not declared_by_file.get(path):
                        unlinked.append(path)
                    continue
                if PurePosixPath(path).name == "conftest.py":
                    unlinked.append(path)
                    continue
                if declared_by_file.get(path):
                    continue
                if graph is None or graph_info["freshness"] != "fresh":
                    unlinked.append(path)
                    continue
                linked = set((graph.get("file_tests") or {}).get(path) or ())
                if policy.mode == "package" and package:
                    linked.update(package)
                if not linked:
                    unlinked.append(path)
            if unlinked:
                fallback_needed = True
                if graph is None or graph_info["freshness"] != "fresh":
                    fallback_reason = (
                        f"graph snapshot is {graph_info['freshness']}: "
                        f"{graph_info.get('reason') or 'not usable'}; "
                        f"{_reason_for_unlinked(sorted(unlinked)[0], root_p)}")
                else:
                    fallback_reason = _reason_for_unlinked(
                        sorted(unlinked)[0], root_p)
                if len(unlinked) > 1:
                    fallback_reason += f" (and {len(unlinked) - 1} more)"

    mandatory = choose(changed_tests, "changed_test")
    mandatory |= choose(state.get("failing") or (), "failed_last_run")
    mandatory |= choose(state.get("flaky") or (), "flaky")
    declared_tests = {test for tests in declared_by_file.values()
                      for test in tests}
    mandatory |= choose(declared_tests, "complete_project_declaration")

    selected: set[str] = set()
    pool: set[str] = set(discovered)
    cap_exceeded = False
    if effective == "none":
        selected = set()
    elif fallback_needed and policy.fallback == "refuse":
        refused = True
        fallback_used = True
        effective = policy.mode
    elif fallback_needed and policy.fallback == "all":
        fallback_used = True
        effective = "all"
        selected = choose(discovered, "all")
    elif fallback_needed:  # affected_with_warning
        fallback_used = True
        effective = policy.mode
        selected = _best_effort(root_p, code_changed, graph, discovered,
                                test_dirs, state, changed_tests)
        warnings.append(
            "graph/affected coverage was not assessed; the selection is "
            "best-effort and is not an affected-only result")
        for path in selected:
            selected_sources.setdefault(path, set()).add("best_effort")
    elif policy.mode == "all":
        selected = choose(discovered, "all")
    else:
        optional: list[tuple[str, set[str]]] = []
        if graph is not None:
            linked: set[str] = set()
            for path in code_changed:
                if kinds[path] == "test":
                    continue
                linked.update((graph.get("file_tests") or {}).get(path) or ())
            optional.append(("graph", choose(linked, "graph")))
            if policy.mode in {"affected", "package"}:
                optional.append((
                    "package",
                    choose(_package_tests(root_p, graph, code_changed,
                                          discovered), "package")))
        selected = set(mandatory)
        selected |= optional[0][1] if optional else set()
        if policy.mode in {"affected", "package"}:
            for _name, got in optional[1:]:
                selected |= got

    if effective not in {"none", "all"} and not refused:
        selected |= declared_tests

    # The cap is applied after mandatory tests, so changed/failed/flaky and
    # explicitly declared complete-coverage tests are never dropped. A strict
    # cap that would drop them is reported.
    if effective != "none" and not refused:
        optional_selected = sorted(selected - mandatory)
        if len(mandatory) >= cap:
            selected = set(mandatory)
            cap_exceeded = (len(mandatory) > cap or bool(optional_selected))
        else:
            room = cap - len(mandatory)
            selected = set(mandatory) | set(optional_selected[:room])
            cap_exceeded = len(optional_selected) > room
    if effective == "none":
        omitted = []
        omitted_total = 0
    else:
        missing = sorted(pool - selected)
        missing_rows: dict[str, str] = {
            path: "test file from the last known results no longer exists"
            for path in missing_state
        }
        for path in missing_changed_tests:
            missing_rows.setdefault(
                path, "changed test file was deleted and cannot run")
        omitted_total = len(missing) + len(missing_rows)
        for path in sorted(missing_rows):
            omitted.append({"file": path, "reason": missing_rows[path]})
        for path in missing[:MAX_LISTED]:
            if refused:
                why = "the test policy refused to select tests"
            elif cap_exceeded:
                why = f"max_tests={cap} cap"
            elif effective in {"smoke", "affected", "package"}:
                why = "not linked to a changed file"
            else:
                why = "not selected by the policy"
            omitted.append({"file": path, "reason": why})
        if len(omitted) > MAX_LISTED:
            omitted = omitted[:MAX_LISTED]

    selected_tests = []
    tests_by_file = (graph or {}).get("tests_by_file") or {}
    for path in sorted(selected):
        selected_tests.append({
            "file": path,
            "node_ids": sorted(str(value)
                               for value in tests_by_file.get(path, ())),
            "sources": sorted(selected_sources.get(path, {"policy"})),
        })

    not_assessed = [
        "no test was executed; this plan selects tests only",
        "a passing subset would not prove correctness",
        "flaky and failed-last-run tests are selected whole-file; their "
        "interactions, order and shared state were not assessed",
        "tests outside the repository's test naming conventions or graph may "
        "be missing from this plan",
    ]
    if effective == "none":
        not_assessed.append(
            "no test was selected; the changed files were not exercised "
            f"({mode_reason or 'policy decision'})")
    if fallback_used:
        not_assessed.append(
            f"the {policy.fallback} fallback was used: {fallback_reason}")
        if policy.fallback == "affected_with_warning":
            not_assessed.append(
                "the selection is best-effort and is not an affected-only "
                "result")
    if refused:
        not_assessed.append(
            "the test policy refused to select tests; no test result is "
            "available")
    if fallback_needed:
        not_assessed.append(
            "affected coverage was not assessed because the graph snapshot "
            f"is {graph_info['freshness']}" if graph is None else
            "affected coverage was not assessed for every changed file")
    if omitted:
        not_assessed.append(
            f"{omitted_total} test file(s) were not selected; the omitted "
            "list says why")
    if cap_exceeded:
        mandatory_description = "changed, failed, flaky"
        if declared_tests:
            mandatory_description += ", and complete-project coverage"
        not_assessed.append(
            f"the max_tests={cap} cap omits optional tests; mandatory tests "
            f"({mandatory_description}) were kept")
    if effective == "all" and not fallback_used:
        not_assessed.append(
            "all discovered test files were selected, but discovery is "
            "limited to the graph and the repository's test naming rules")

    return {
        "schema": SCHEMA,
        "root": str(root_p),
        "mode": effective,
        "requested_mode": policy.mode,
        "mode_reason": mode_reason,
        "task_class": task_class,
        "changed_files": changed_paths,
        "changed_kinds": kinds,
        "selected_files": sorted(selected),
        "selected_tests": selected_tests,
        "counts": {
            "changed": len(changed_paths),
            "changed_files": len(changed_paths),
            "selected": len(selected),
            "selected_files": len(selected),
            "selected_node_ids": sum(len(item["node_ids"])
                                     for item in selected_tests),
            "omitted": omitted_total,
            "omitted_files": omitted_total,
            "max_tests": cap,
            "cap_exceeded": cap_exceeded,
        },
        "omitted": omitted,
        "omitted_truncated": omitted_total > MAX_LISTED,
        "fallback": policy.fallback,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "refused": refused,
        "graph": dict(graph_info),
        "graph_freshness": graph_info["freshness"],
        "test_coverage_declaration": coverage_contract,
        "test_results": state,
        "policy": policy.to_dict(),
        "range": {
            "base": base,
            "head": head,
            "base_commit": (_resolve_commit(root_p, base)
                            if changed is None else None),
            "head_commit": head_commit,
        },
        "max_tests": cap,
        "timeout_s": policy.timeout_s,
        "executed": False,
        "truth_assessed": False,
        "claim": "test selection only; no test was executed",
        "not_run_reason": "tests were not executed; this is a selection plan, "
                          "not a result",
        "warnings": warnings,
        "not_assessed": not_assessed,
    }


__all__ = [
    "CAPTURED_SELECT",
    "DEFAULT_CAPTURED",
    "DEFAULT_FALLBACK",
    "DEFAULT_MAX_TESTS",
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_S",
    "FALLBACKS",
    "MODES",
    "SCHEMA",
    "TestPolicy",
    "TestPolicyError",
    "captured_select",
    "changed_files",
    "load_policy",
    "plan",
    "with_overrides",
]
