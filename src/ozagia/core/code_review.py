"""Deterministic, graph-aware review of a git diff.

The harness reads a commit range with git plumbing, runs a small preflight
(worktree/branch, unresolved conflicts and resource admission), then derives
findings from the diff and, when a fresh ``.ozagia/graph.json`` exists, from
the recorded graph. It never builds the graph, never calls a model, never
fetches and never writes to the repository.

The packet says what was checked and what was not. A deterministic finding is
evidence, not proof: ``truth_assessed`` is always false, and every finding
carries its evidence and a confidence note.

Optional independent model review and test execution are seams, not
implementations here. See :func:`run_injected_review`,
:func:`run_injected_reviews` and :func:`run_injected_tests`: the caller
supplies the runner(s) and owns network, provider, test-process and approval
behaviour. Reviewer agreement and caller-supplied test results are never
treated as proof.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from ozagia.core import library_hints as LH
from ozagia.core import ocelot_code
from ozagia.core import resources
from ozagia.core import testing as test_plans
from ozagia.core import worktree as W
from ozagia.core import git_safety as GS

SCHEMA = 1
PACKET_SCHEMA = "ozagia.code_review.packet.v1"

SEVERITIES = ("blocking", "major", "minor", "note")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
FAIL_ON = ("blocking", "major", "never")
SECURITY_TO_REVIEW = {
    "critical": "blocking",
    "blocking": "blocking",
    "high": "major",
    "major": "major",
    "medium": "minor",
    "minor": "minor",
    "low": "note",
    "info": "note",
    "note": "note",
}

FINDING_TYPES = (
    "breaking_change",
    "missing_test",
    "unhandled_error",
    "stale_reference",
    "scope_creep",
    "duplicate_logic",
    "contract_mismatch",
    "security_sensitive",
    "performance_risk",
    "test_failure",
    "unknown",
)

GIT_TIMEOUT = 60
DEFAULT_MAX_FILE_BYTES = 1_000_000
MAX_ITEMS = 100
MAX_WHITESPACE_FINDINGS = 50
MAX_CONFLICT_FINDINGS = 50
MAX_SENSITIVE_FINDINGS = 50
# Library hints in a packet: changed source files inspected, hints shown. The
# per-file cap is the configured ``hints.max_per_edit``.
MAX_LIBRARY_HINT_FILES = 40
MAX_LIBRARY_HINTS = 10

# Injected reviewers: reviewer error text and agreement/dissent detail are
# bounded so a misbehaving caller cannot grow the packet without limit.
MAX_REVIEWER_ERROR_CHARS = 500
MAX_REVIEW_DETAIL = 20

# Injected test results: output is an excerpt, never a full log.
MAX_TEST_OUTPUT_LINES = 100
MAX_TEST_OUTPUT_BYTES = 8192
MAX_TEST_ERROR_CHARS = 500
MAX_TEST_NOTE_CHARS = 300

# Extensions treated as executable source. A diff that changes one of these
# without a test is reported, unless a covering test is recorded.
SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".rs", ".go", ".js", ".jsx", ".mjs", ".cjs", ".ts",
    ".tsx", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".cxx",
    ".hpp", ".hh", ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash",
    ".zsh", ".lua", ".sql", ".graphql", ".gql", ".proto", ".tf", ".hcl",
    ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".dart", ".vue",
    ".svelte",
})
DOC_SUFFIXES = frozenset({".md", ".rst", ".adoc", ".asciidoc", ".txt"})
CONFIG_SUFFIXES = frozenset({
    ".toml", ".yaml", ".yml", ".json", ".jsonc", ".ini", ".cfg", ".conf",
    ".properties", ".xml", ".plist",
})

GENERATED_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "poetry.lock", "pdm.lock", "uv.lock", "cargo.lock",
    "composer.lock", "gemfile.lock", "go.sum", "flake.lock",
})
GENERATED_PARTS = frozenset({
    "node_modules", "dist", "build", "target", "vendor", "__pycache__",
    ".venv", "venv", "site-packages", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox",
})
GENERATED_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".pb.go", ".g.dart", ".generated",
)
GENERATED_MARKERS = ("code generated", "do not edit", "@generated")

SENSITIVE_TOKENS = frozenset({
    "auth", "authn", "authz", "crypto", "cryptography", "secret", "secrets",
    "security", "password", "passwords", "permission", "permissions", "tls",
    "cert", "certs", "certificate", "certificates", "iam", "ssh",
})
SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
})
DANGEROUS_TOKENS = (
    ("eval(", "dynamic eval"),
    ("exec(", "dynamic exec"),
    ("os.system(", "shell command"),
    ("shell=True", "shell=True"),
    ("pickle.loads(", "pickle deserialisation"),
    ("marshal.loads(", "marshal deserialisation"),
    ("yaml.load(", "unsafe YAML load"),
)

CALL_EDGE = "calls"
TEST_EDGES = ("tests_module", "mentions_file", "tested_by_file")


class CodeReviewError(RuntimeError):
    """The review could not run: a bad root/range, git failure or bad graph."""


class PreflightError(CodeReviewError):
    """The worktree, conflict or resource preflight refused the run.

    ``report`` is a JSON-safe refusal object. Nothing was reviewed.
    """

    def __init__(self, message: str, report: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.report = dict(report)


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def _git(root: str | Path, args: Sequence[str], *,
         timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    # Git inherits caller environment configuration, which can redirect the
    # repository/index or inject global config. Review must operate on the
    # supplied worktree with host-safe, empty global/system config.
    command = GS.git_subcommand(args)
    try:
        result = GS.run_git(root, args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodeReviewError(
            f"git {' '.join(args)} could not run: {exc}") from exc
    except GS.UnsafeGitConfig as exc:
        raise CodeReviewError(
            f"refusing git {command}: {exc}") from exc
    return result


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _git_required(root: str | Path, args: Sequence[str], *,
                  timeout: int = GIT_TIMEOUT) -> bytes:
    proc = _git(root, args, timeout=timeout)
    if proc.returncode != 0:
        detail = _decode(proc.stderr or proc.stdout).strip()[:500]
        raise CodeReviewError(
            f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


def _repository_root(root: str | Path) -> Path:
    try:
        path = Path(root).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise CodeReviewError(f"cannot resolve root {root!r}: {exc}") from exc
    if not path.is_dir():
        raise CodeReviewError(f"root does not exist: {path}")
    proc = _git(path, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        detail = _decode(proc.stderr or proc.stdout).strip()[:500]
        raise CodeReviewError(f"{path} is not a git worktree: {detail}")
    top = Path(_decode(proc.stdout).strip()).resolve()
    if top != path:
        raise CodeReviewError(
            f"root {path} is not the git top level ({top}); "
            "pass the repository root")
    return path


def _valid_rev(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise CodeReviewError(
            f"{label} must be a commit or revision, got {value!r}")
    text = value.strip()
    if not text or text.startswith("-") or "\n" in text or "\0" in text:
        raise CodeReviewError(f"{label} must be a commit or revision, got {value!r}")
    return text


def _resolve_commit(root: Path, rev: str, label: str) -> str:
    rev = _valid_rev(rev, label)
    proc = _git(root, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CodeReviewError(f"{label} {rev!r} is not a commit in {root}")
    return _decode(proc.stdout).strip()


def parse_range(spec: str) -> tuple[str, str]:
    """Split ``BASE..HEAD`` into two revision strings.

    Three-dot ranges are rejected: the packet reports a two-ended diff, and a
    merge-base compare would change what ``base`` means.
    """
    text = (spec or "").strip()
    if not text:
        raise CodeReviewError("range is empty; use BASE..HEAD")
    if "..." in text:
        raise CodeReviewError(
            f"range {spec!r} uses three dots; use BASE..HEAD")
    parts = text.split("..")
    if len(parts) != 2:
        raise CodeReviewError(f"range {spec!r} must be BASE..HEAD")
    base, head = (part.strip() for part in parts)
    if not base or not head:
        raise CodeReviewError(f"range {spec!r} must name both BASE and HEAD")
    return base, head


def _unmerged_paths(root: Path) -> list[str]:
    proc = _git(root, ["ls-files", "-u", "-z"])
    if proc.returncode != 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        _, _, raw = entry.partition(b"\t")
        path = os.fsdecode(raw)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return sorted(out)


def _worktree_status(root: Path, base: str,
                     config_dir: str | Path | None) -> dict[str, Any]:
    """The current worktree item from ``worktree.check``."""
    try:
        report = W.check(root, base=base, config_dir=config_dir)
    except W.WorktreeError as exc:
        raise CodeReviewError(f"worktree check failed: {exc}") from exc
    current = next(
        (item for item in report.get("worktrees") or []
         if Path(str(item.get("path"))).resolve() == root),
        None,
    )
    if current is None:
        raise CodeReviewError(
            f"{root} is not one of the worktrees git reports")
    return current


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #


def _check(checks: list[dict[str, Any]], level: str, ident: str,
           message: str, *, command: str | None = None,
           **extra: Any) -> None:
    checks.append({"id": ident, "level": level, "message": message,
                   "command": command, **extra})


def _resource_report(root: Path, *, memory_mb: float, disk_mb: float) -> dict[str, Any]:
    expected = resources.estimate(
        root, "code_review", default_mb=memory_mb,
        default_disk_mb=disk_mb)
    requested_memory = expected.get("memory_mb")
    requested_disk = expected.get("disk_mb")
    if not isinstance(requested_memory, (int, float)):
        requested_memory = memory_mb
    if not isinstance(requested_disk, (int, float)):
        requested_disk = disk_mb
    admission = resources.check_admission(
        root, kind="task", memory_mb=float(requested_memory),
        disk_mb=float(requested_disk), key="code_review")
    # Keep the packet reproducible: the resource sampler's clock value and
    # host sample are not part of the review evidence.
    keep = ("ok", "disabled", "reason", "requested", "available",
            "shortfall", "retry_after_s", "checks", "suggestions")
    compact = {name: admission.get(name) for name in keep
               if name in admission}
    compact["estimate"] = expected
    if "requested" not in compact:
        compact["requested"] = {
            "memory_mb": expected.get("memory_mb"),
            "disk_mb": expected.get("disk_mb"),
        }
    compact["note"] = (
        "admission only; the review did not run tests, builds or linters")
    return compact


def preflight(root: str | Path, *, base: str, head: str = "HEAD",
              base_commit: str | None = None,
              head_commit: str | None = None,
              memory_mb: float = 2048.0, disk_mb: float = 4096.0,
              config_dir: str | Path | None = None,
              worktree_base: str = "main") -> dict[str, Any]:
    """Check the worktree, conflicts and resources before any review work.

    ``base`` is the commit range's base. ``worktree_base`` is the branch the
    worktree mapping expects (``core.worktree`` uses the same name for its
    expected main branch and drift count); it is not the range base.
    """
    root = _repository_root(root)
    if memory_mb < 0 or disk_mb < 0:
        raise CodeReviewError("memory_mb and disk_mb must be >= 0")
    base_rev = _valid_rev(base, "base")
    head_rev = _valid_rev(head, "head")
    base_sha = base_commit or _resolve_commit(root, base_rev, "base")
    head_sha = head_commit or _resolve_commit(root, head_rev, "head")
    checks: list[dict[str, Any]] = []

    current = _worktree_status(
        root, _valid_rev(worktree_base, "worktree_base"), config_dir)
    if current.get("error"):
        _check(checks, "error", "worktree.status",
               f"worktree status failed: {current['error']}",
               command="git status --porcelain=v2 --branch")
    else:
        _check(checks, "ok", "worktree.status", "worktree status read",
               command="git status --porcelain=v2 --branch")
    expected = current.get("expected") or {}
    branch = current.get("branch")
    expected_branch = expected.get("branch")
    if expected_branch and branch and expected_branch != branch:
        _check(checks, "error", "worktree.expected_branch",
               f"expected branch {expected_branch!r}, found {branch!r}",
               command="git symbolic-ref --short HEAD",
               expected=expected_branch, actual=branch)
    else:
        _check(checks, "ok", "worktree.expected_branch",
               f"branch is {branch!r}" if branch else "no branch is checked out",
               command="git symbolic-ref --short HEAD")
    if current.get("detached"):
        _check(checks, "warn", "worktree.detached",
               "HEAD is detached; review still reads the commit range",
               command="git symbolic-ref --short HEAD")
    if current.get("operation"):
        _check(checks, "error", "worktree.operation",
               f"a {current['operation']} operation is in progress",
               command="git status --porcelain=v2 --branch")
    else:
        _check(checks, "ok", "worktree.operation",
               "no merge, rebase, cherry-pick, revert or bisect in progress")

    unmerged = _unmerged_paths(root)
    indexed_conflicts = int((current.get("dirty") or {}).get("conflicted") or 0)
    if unmerged:
        _check(checks, "error", "worktree.conflicts",
               f"{len(unmerged)} unresolved conflict path(s)",
               command="git ls-files -u -z",
               paths=unmerged[:20])
    elif indexed_conflicts:
        _check(checks, "error", "worktree.conflicts",
               f"{indexed_conflicts} conflicted path(s)",
               command="git status --porcelain=v2 --branch")
    else:
        _check(checks, "ok", "worktree.conflicts", "no unresolved conflicts",
               command="git ls-files -u -z")

    dirty = current.get("dirty") or {}
    changed = sum(int(dirty.get(key, 0)) for key in
                  ("staged", "unstaged", "untracked", "conflicted"))
    _check(checks, "info" if changed else "ok", "worktree.dirty",
           f"{changed} uncommitted or untracked path(s)"
           if changed else "worktree is clean",
           command="git status --porcelain=v2 --branch", dirty=dict(dirty))

    ancestor = _git(root, ["merge-base", "--is-ancestor", base_sha, head_sha])
    if ancestor.returncode == 0:
        _check(checks, "ok", "git.base_ancestor",
               f"{base_rev} ({base_sha[:12]}) is an ancestor of "
               f"{head_rev} ({head_sha[:12]})",
               command=f"git merge-base --is-ancestor {base_sha} {head_sha}")
    elif ancestor.returncode == 1:
        _check(checks, "error", "git.base_ancestor",
               f"{base_rev} ({base_sha[:12]}) is not an ancestor of "
               f"{head_rev} ({head_sha[:12]}); the range is not a line of "
               "history from base to head",
               command=f"git merge-base --is-ancestor {base_sha} {head_sha}")
    else:
        _check(checks, "error", "git.base_ancestor",
               "git could not compare the base and head commits",
               command=f"git merge-base --is-ancestor {base_sha} {head_sha}")

    resource_report = _resource_report(
        root, memory_mb=memory_mb, disk_mb=disk_mb)
    if resource_report.get("ok"):
        _check(checks, "ok", "resources.admission",
               "the expected test/build footprint fits the host reserves",
               requested=resource_report.get("requested"))
    else:
        _check(checks, "error", "resources.admission",
               f"resource admission refused ({resource_report.get('reason')})",
               shortfall=resource_report.get("shortfall"),
               suggestions=resource_report.get("suggestions"),
               requested=resource_report.get("requested"))

    errors = [item for item in checks if item.get("level") == "error"]
    worktree = {
        "path": str(root),
        "head": current.get("head"),
        "branch": branch,
        "detached": bool(current.get("detached")),
        "expected": dict(expected),
        "dirty": dict(dirty),
        "clean": changed == 0,
        "operation": current.get("operation"),
        "base": dict(current.get("base") or {}),
    }
    return {
        "schema": SCHEMA,
        "ok": not errors,
        "reason": errors[0]["id"] if errors else None,
        "range": {
            "spec": f"{base_rev}..{head_rev}",
            "base": base_rev,
            "head": head_rev,
            "base_commit": base_sha,
            "head_commit": head_sha,
        },
        "worktree": worktree,
        "resources": resource_report,
        "checks": checks,
    }


def _refusal(preflight_report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PACKET_SCHEMA,
        "ok": False,
        "refused": True,
        "reason": preflight_report.get("reason"),
        "range": preflight_report.get("range"),
        "worktree": preflight_report.get("worktree"),
        "resources": preflight_report.get("resources"),
        "preflight": {
            "ok": False,
            "checks": preflight_report.get("checks") or [],
        },
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def _split_z(data: bytes) -> list[str]:
    return [os.fsdecode(part) for part in data.split(b"\0")]


def _name_status(root: Path, base: str, head: str) -> list[dict[str, Any]]:
    out = _git_required(
        root, ["diff", "--name-status", "-z", "--find-renames",
               "--no-ext-diff", "--no-textconv", base, head, "--"])
    tokens = _split_z(out)
    files: list[dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        if len(status) < 1 or not status[0].isalpha():
            i += 1
            continue
        kind = status[0]
        if kind in ("R", "C"):
            if i + 2 >= len(tokens):
                break
            old_path, path = tokens[i + 1], tokens[i + 2]
            i += 3
        else:
            if i + 1 >= len(tokens):
                break
            old_path, path = None, tokens[i + 1]
            i += 2
        files.append({
            "status": status,
            "kind": kind,
            "old_path": old_path,
            "path": path,
            "deleted": kind == "D",
            "renamed": kind == "R",
            "copied": kind == "C",
            "binary": kind == "B",
            "additions": None,
            "deletions": None,
            "size": None,
            "large": False,
            "generated": False,
        })
    return files


def _numstat(root: Path, base: str, head: str) -> dict[str, tuple[int | None,
                                                              int | None,
                                                              bool]]:
    out = _git_required(
        root, ["diff", "--numstat", "-z", "--find-renames",
               "--no-ext-diff", "--no-textconv", base, head, "--"])
    tokens = _split_z(out)
    stats: dict[str, tuple[int | None, int | None, bool]] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        parts = token.split("\t", 2)
        if len(parts) != 3:
            i += 1
            continue
        added, deleted, path = parts
        if path:
            i += 1
            target = path
        else:
            if i + 2 >= len(tokens):
                break
            target = tokens[i + 2]
            i += 3
        binary = added == "-" or deleted == "-"
        try:
            additions = None if binary else int(added)
            deletions = None if binary else int(deleted)
        except ValueError:
            additions = deletions = None
            binary = True
        stats[target] = (additions, deletions, binary)
    return stats


def _blob_size(root: Path, commit: str, path: str) -> int | None:
    proc = _git(root, ["cat-file", "-s", f"{commit}:{path}"])
    if proc.returncode != 0:
        return None
    try:
        return int(_decode(proc.stdout).strip())
    except ValueError:
        return None


def _classify_path(path: str) -> str:
    p = PurePosixPath(path)
    name = p.name.casefold()
    parts = {part.casefold() for part in p.parts[:-1]}
    suffix = p.suffix.casefold()
    if (name == "conftest.py" or name.startswith("test_")
            or name.endswith("_test.py") or name.endswith("_test.go")
            or "tests" in parts or "test" in parts):
        return "test"
    if (name in {"readme.md", "changelog.md", "license", "license.md",
                 "contributing.md", "security.md"}
            or "docs" in parts or suffix in DOC_SUFFIXES):
        return "doc"
    if suffix in SOURCE_SUFFIXES:
        return "source"
    if suffix in CONFIG_SUFFIXES:
        return "config"
    return "other"


def _is_generated(path: str, marked: Iterable[str]) -> bool:
    p = PurePosixPath(path)
    name = p.name.casefold()
    if path in set(marked):
        return True
    if name in GENERATED_NAMES:
        return True
    if any(part.casefold() in GENERATED_PARTS for part in p.parts[:-1]):
        return True
    if name.endswith(GENERATED_SUFFIXES):
        return True
    return ".generated." in name


def _sensitive_path(path: str) -> str | None:
    p = PurePosixPath(path)
    name = p.name.casefold()
    if name in SENSITIVE_NAMES or name.startswith(".env."):
        return name
    tokens = {part for part in re.split(r"[^a-z0-9]+", path.casefold())
              if part}
    hits = sorted(tokens & SENSITIVE_TOKENS)
    return hits[0] if hits else None


def _parse_patch(text: str) -> dict[str, Any]:
    """Added-line facts: first line, conflict markers and dangerous tokens."""
    first_added: dict[str, int] = {}
    marker_candidates: dict[str, list[tuple[int, str]]] = {}
    dangerous: dict[tuple[str, str], int] = {}
    generated: set[str] = set()
    current: str | None = None
    new_line = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = None
            in_hunk = False
            continue
        if line.startswith("+++ ") and not in_hunk:
            value = line[4:].strip()
            if value == "/dev/null":
                current = None
            elif value.startswith("b/"):
                current = value[2:]
            else:
                current = value.strip('"')
            continue
        if line.startswith("--- ") and not in_hunk:
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            in_hunk = True
            new_line = int(match.group(1))
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file" is not a source line.
            continue
        if line.startswith("+"):
            if current is None:
                continue
            content = line[1:]
            first_added.setdefault(current, new_line)
            stripped = content.strip()
            low = stripped.casefold()
            if (stripped.startswith("<<<<<<<")
                    or stripped.startswith(">>>>>>>")
                    or re.fullmatch(r"={7,}", stripped)):
                marker_candidates.setdefault(current, []).append(
                    (new_line, stripped))
            for token, label in DANGEROUS_TOKENS:
                if token in content:
                    dangerous.setdefault((current, label), new_line)
            if any(marker in low for marker in GENERATED_MARKERS):
                generated.add(current)
        if not line.startswith("-"):
            new_line += 1
    conflicts: list[dict[str, Any]] = []
    for path, candidates in sorted(marker_candidates.items()):
        opens = any(text.startswith(("<<<<<<<", ">>>>>>>"))
                    for _line, text in candidates)
        if not opens:
            continue
        for number, marker in candidates:
            conflicts.append({"file": path, "line": number, "marker": marker})
            if len(conflicts) >= MAX_CONFLICT_FINDINGS:
                break
        if len(conflicts) >= MAX_CONFLICT_FINDINGS:
            break
    return {
        "first_added": first_added,
        "conflicts": conflicts,
        "dangerous": [
            {"file": path, "line": line, "label": label}
            for (path, label), line in sorted(dangerous.items())
        ],
        "generated": generated,
    }


def _whitespace_errors(root: Path, base: str, head: str) -> list[dict[str, Any]]:
    proc = _git(root, ["diff", "--check", "--no-ext-diff", "--no-textconv",
                       "--find-renames", base, head, "--"])
    text = _decode(proc.stdout) + _decode(proc.stderr)
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for line in text.splitlines():
        match = re.match(r"^(.*):(\d+): (.*)$", line)
        if match is None:
            continue
        path, number, message = match.group(1), int(match.group(2)), match.group(3)
        if "conflict marker" in message.casefold():
            # The conflict-marker finding below has the exact added lines;
            # do not report the same problem twice as whitespace.
            continue
        key = (path, number, message)
        if key in seen:
            continue
        seen.add(key)
        errors.append({"file": path, "line": number, "message": message})
        if len(errors) >= MAX_WHITESPACE_FINDINGS:
            break
    return errors


def _tree_tests(root: Path, head: str) -> list[str]:
    """Tracked test files at the reviewed commit (file-level fallback)."""
    out = _git_required(
        root, ["ls-tree", "-r", "--name-only", "-z", head])
    return sorted(path for path in _split_z(out)
                  if path and _classify_path(path) == "test")


def _diff_files(root: Path, base: str, head: str, *,
                max_file_bytes: int) -> tuple[list[dict[str, Any]],
                                              dict[str, Any]]:
    stat = _numstat(root, base, head)
    patch_text = _decode(_git_required(
        root, ["diff", "--unified=3", "--no-color", "--no-ext-diff",
               "--no-textconv", "--find-renames", base, head, "--"]))
    patch = _parse_patch(patch_text)
    files = _name_status(root, base, head)
    for item in files:
        path = item["path"]
        if path in stat:
            additions, deletions, binary = stat[path]
            item["additions"] = additions
            item["deletions"] = deletions
            item["binary"] = item["binary"] or binary
        item["size"] = (None if item["deleted"]
                        else _blob_size(root, head, path))
        item["large"] = bool(
            item["size"] is not None and item["size"] > max_file_bytes)
        item["generated"] = _is_generated(path, patch["generated"])
        item["file_kind"] = _classify_path(path)
        item["first_added_line"] = patch["first_added"].get(path)
        item["graph_keys"] = []
        item["symbols"] = []
        item["covering_tests"] = []
    files.sort(key=lambda value: (value["path"], value["status"]))
    return files, {
        "files": len(files),
        "added": sum(1 for value in files if value["kind"] == "A"),
        "modified": sum(1 for value in files if value["kind"] == "M"),
        "deleted": sum(1 for value in files if value["deleted"]),
        "renamed": sum(1 for value in files if value["renamed"]),
        "binary": sum(1 for value in files if value["binary"]),
        "large": sum(1 for value in files if value["large"]),
        "generated": sum(1 for value in files if value["generated"]),
        "additions": sum(value["additions"] or 0 for value in files),
        "deletions": sum(value["deletions"] or 0 for value in files),
        "patch_lines": len(patch_text.splitlines()),
        "conflict_markers": patch["conflicts"],
        "dangerous_tokens": patch["dangerous"],
        "whitespace": _whitespace_errors(root, base, head),
    }


# --------------------------------------------------------------------------- #
# graph snapshot
# --------------------------------------------------------------------------- #


def _resolve_graph(root: Path, graph_path: str | Path | None) -> Path:
    if graph_path is not None:
        try:
            path = Path(graph_path).expanduser()
        except (OSError, RuntimeError) as exc:
            raise CodeReviewError(
                f"cannot resolve graph snapshot {graph_path!r}: {exc}") from exc
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.is_file():
            raise CodeReviewError(f"graph snapshot does not exist: {path}")
        return path
    return root / ".ozagia" / "graph.json"


def _graph_index(dev: Any) -> dict[str, Any]:
    nodes: dict[int, dict[str, Any]] = {}
    files_by_path: dict[str, int] = {}
    symbols_by_file: dict[str, list[int]] = {}
    tests_by_file: dict[str, list[int]] = {}
    for nid, node in dev.nodes.items():
        key = dev.key_of(nid)
        attrs = node.get("attrs")
        if not isinstance(attrs, dict):
            attrs = {}
        entry = {
            "type": node.get("type"),
            "attrs": attrs,
            "key": key,
        }
        nodes[int(nid)] = entry
        if entry["type"] == "File":
            path = str(entry["attrs"].get("path") or "")
            if path:
                files_by_path[path] = int(nid)
        elif entry["type"] == "Symbol":
            path = str(entry["attrs"].get("file") or "")
            if path:
                symbols_by_file.setdefault(path, []).append(int(nid))
    incoming: dict[int, list[tuple[int, str]]] = {}
    outgoing: dict[int, list[tuple[int, str]]] = {}
    call_edges = 0
    for edge in dev.edges:
        try:
            src, tgt = int(edge["src"]), int(edge["tgt"])
        except (KeyError, TypeError, ValueError):
            continue
        etype = str(edge.get("type") or "")
        incoming.setdefault(tgt, []).append((src, etype))
        outgoing.setdefault(src, []).append((tgt, etype))
        if etype == CALL_EDGE:
            call_edges += 1
    # A Test node that imports or mentions a file covers it.
    for tid, entry in nodes.items():
        if entry["type"] != "Test":
            continue
        path = str(entry["attrs"].get("file") or "")
        if path:
            tests_by_file.setdefault(path, []).append(tid)
        for tgt, etype in outgoing.get(tid, []):
            if etype in TEST_EDGES and nodes.get(tgt, {}).get("type") == "File":
                target_path = str(nodes[tgt]["attrs"].get("path") or "")
                if target_path and tid not in tests_by_file.setdefault(
                        target_path, []):
                    tests_by_file[target_path].append(tid)
    # `File -has_test-> Test` covers tests defined in the file itself.
    for fid, entry in nodes.items():
        if entry["type"] != "File":
            continue
        path = str(entry["attrs"].get("path") or "")
        if not path:
            continue
        for tgt, etype in outgoing.get(fid, []):
            if (etype == "has_test"
                    and nodes.get(tgt, {}).get("type") == "Test"
                    and tgt not in tests_by_file.setdefault(path, [])):
                tests_by_file[path].append(tgt)
    for path in tests_by_file:
        tests_by_file[path].sort()
    for path in symbols_by_file:
        symbols_by_file[path].sort()
    return {
        "nodes": nodes,
        "files_by_path": files_by_path,
        "symbols_by_file": symbols_by_file,
        "tests_by_file": tests_by_file,
        "incoming": incoming,
        "outgoing": outgoing,
        "call_edges": call_edges,
    }


def _load_graph(graph_path: Path, root: Path, head_commit: str,
                worktree_head: str | None) -> tuple[dict[str, Any],
                                                    dict[str, Any] | None]:
    info: dict[str, Any] = {
        "path": str(graph_path),
        "available": False,
        "freshness": "missing",
        "usable": False,
        "snapshot_head": None,
        "snapshot_root": None,
        "head": head_commit,
        "generated_at": None,
        "nodes": 0,
        "edges": 0,
        "call_edges": 0,
        "reason": None,
    }
    if not graph_path.is_file():
        info["reason"] = f"no graph snapshot at {graph_path}"
        return info, None
    try:
        from ozagia.core.memory import DevMemory
        dev = DevMemory(dump=graph_path, root=root)
        staleness = dev.staleness()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        info["available"] = True
        info["freshness"] = "error"
        info["reason"] = f"cannot read graph snapshot: {exc}"
        return info, None
    info["available"] = True
    repo_attrs: dict[str, Any] = {}
    for node in dev.nodes.values():
        if node.get("type") == "Repo":
            raw_attrs = node.get("attrs")
            repo_attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
            break
    index = _graph_index(dev)
    info.update({
        "snapshot_head": repo_attrs.get("head"),
        "snapshot_root": repo_attrs.get("root"),
        "generated_at": repo_attrs.get("generated_at"),
        "nodes": len(dev.nodes),
        "edges": len(dev.edges),
        "call_edges": index["call_edges"],
    })
    reasons: list[str] = []
    snapshot_root = repo_attrs.get("root")
    if snapshot_root:
        try:
            root_matches = Path(str(snapshot_root)).resolve() == root
        except (OSError, RuntimeError, ValueError):
            root_matches = False
        if not root_matches:
            reasons.append(f"snapshot root {snapshot_root} is not {root}")
    snapshot_head = repo_attrs.get("head")
    if snapshot_head and snapshot_head != head_commit:
        reasons.append(
            f"snapshot head {str(snapshot_head)[:12]} does not match "
            f"reviewed head {head_commit[:12]}")
    if worktree_head and head_commit != worktree_head:
        reasons.append(
            "reviewed head is not the checkout HEAD; the snapshot's working "
            "tree cannot be verified against it")
    if reasons:
        info["freshness"] = "stale"
        info["reason"] = "; ".join(reasons)
    else:
        freshness = str(staleness.get("freshness") or "unknown")
        info["freshness"] = freshness
        if freshness == "fresh":
            info["reason"] = "snapshot head and working-tree digest match"
        elif freshness == "stale":
            detail = []
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
            info["reason"] = "; ".join(str(value) for value in errors) or (
                "graph freshness could not be verified")
    info["usable"] = info["freshness"] == "fresh"
    return info, index


def _graph_file_key(index: Mapping[str, Any], path: str) -> str | None:
    nid = (index.get("files_by_path") or {}).get(path)
    if nid is None:
        return None
    return (index["nodes"].get(nid) or {}).get("key")


def _graph_symbols(index: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for nid in (index.get("symbols_by_file") or {}).get(path, []):
        entry = index["nodes"].get(nid) or {}
        attrs = entry.get("attrs") or {}
        out.append({
            "id": nid,
            "key": entry.get("key"),
            "qualname": attrs.get("qualname"),
            "name": attrs.get("name"),
            "start_line": attrs.get("start_line"),
            "end_line": attrs.get("end_line"),
        })
    return out


def _graph_tests(index: Mapping[str, Any], path: str,
                 changed_paths: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for nid in (index.get("tests_by_file") or {}).get(path, []):
        entry = index["nodes"].get(nid) or {}
        attrs = entry.get("attrs") or {}
        test_file = attrs.get("file")
        out.append({
            "id": nid,
            "key": entry.get("key"),
            "file": test_file,
            "qualname": attrs.get("qualname"),
            "changed": test_file in changed_paths,
        })
    return out


def _graph_callers(index: Mapping[str, Any],
                   symbol_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src, etype in (index.get("incoming") or {}).get(symbol_id, []):
        if etype != CALL_EDGE:
            continue
        entry = index["nodes"].get(src) or {}
        attrs = entry.get("attrs") or {}
        path = attrs.get("file")
        if not path and entry.get("key"):
            path = _file_from_key(str(entry["key"]))
        out.append({
            "id": src,
            "key": entry.get("key"),
            "file": path,
            "qualname": attrs.get("qualname"),
        })
    return out


def _file_from_key(key: str) -> str | None:
    prefix, _, rest = key.partition(":")
    if prefix in ("file", "doc"):
        return rest or None
    if prefix in ("sym", "test"):
        return rest.split("::", 1)[0] or None
    if prefix == "text":
        return rest.split("#", 1)[0] or None
    return None


# --------------------------------------------------------------------------- #
# findings
# --------------------------------------------------------------------------- #


def _finding(ftype: str, severity: str, file: str | None, line: int | None,
             message: str, *, evidence: Iterable[str] = (),
             graph_keys: Iterable[str] = (), confidence: str = "high",
             confidence_note: str = "") -> dict[str, Any]:
    if ftype not in FINDING_TYPES:
        ftype = "unknown"
    if severity not in SEVERITY_RANK:
        severity = "note"
    return {
        "id": None,
        "type": ftype,
        "severity": severity,
        "file": file,
        "line": line,
        "message": message,
        "evidence": list(dict.fromkeys(str(value) for value in evidence)),
        "graph_keys": sorted({str(value) for value in graph_keys if value}),
        "confidence": confidence,
        "confidence_note": confidence_note,
    }


def _finding_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    def line_key(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return (
        SEVERITY_RANK.get(str(item.get("severity")), 99),
        str(item.get("type") or ""),
        str(item.get("file") or ""),
        line_key(item.get("line")),
        str(item.get("message") or ""),
    )


def _finalize_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in findings:
        item = dict(raw)
        item["graph_keys"] = sorted(set(item.get("graph_keys") or []))
        item["evidence"] = list(dict.fromkeys(item.get("evidence") or []))
        key = (item.get("type"), item.get("severity"), item.get("file"),
               item.get("line"), item.get("message"),
               tuple(item["evidence"]), tuple(item["graph_keys"]))
        unique.setdefault(key, item)

    ordered = sorted(unique.values(), key=_finding_sort_key)
    for number, item in enumerate(ordered, start=1):
        item["id"] = f"CR-{number:03d}"
        item["type"] = str(item.get("type") or "unknown")
        item["severity"] = str(item.get("severity") or "note")
        if item["type"] not in FINDING_TYPES:
            item["type"] = "unknown"
        if item["severity"] not in SEVERITY_RANK:
            item["severity"] = "note"
    return ordered


def _recompute_summary(packet: dict[str, Any]) -> None:
    findings = packet.get("findings") or []
    summary = dict(packet.get("summary") or {})
    summary.update({
        "findings": len(findings),
        "blocking": sum(1 for item in findings
                        if item.get("severity") == "blocking"),
        "major": sum(1 for item in findings
                     if item.get("severity") == "major"),
        "minor": sum(1 for item in findings
                     if item.get("severity") == "minor"),
        "note": sum(1 for item in findings
                    if item.get("severity") == "note"),
    })
    packet["summary"] = summary


def _fallback_tests(source: str, repo_tests: Iterable[str]) -> list[str]:
    p = PurePosixPath(source)
    names = {f"test_{p.stem}.py", f"{p.stem}_test.py", f"{p.stem}_test.go"}
    return sorted(path for path in repo_tests
                  if PurePosixPath(path).name in names)


def _derive_findings(files: Sequence[dict[str, Any]],
                     diff_info: Mapping[str, Any],
                     graph_info: Mapping[str, Any],
                     graph: Mapping[str, Any] | None,
                     changed_paths: set[str],
                     repo_tests: Sequence[str]) -> tuple[list[dict[str, Any]],
                                                        list[str],
                                                        list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    omissions: list[str] = []
    not_assessed: list[dict[str, Any]] = []
    usable = bool(graph_info.get("usable") and graph is not None)
    changed_tests = [item["path"] for item in files
                     if item["file_kind"] == "test"]
    graph_missing_files: list[str] = []

    for item in files:
        path = item["path"]
        kind = item["file_kind"]
        graph_keys: list[str] = []
        symbols: list[dict[str, Any]] = []
        covering: list[dict[str, Any]] = []
        if usable and graph is not None:
            fkey = _graph_file_key(graph, path)
            if fkey:
                graph_keys.append(fkey)
            symbols = _graph_symbols(graph, path)
            graph_keys.extend(
                str(symbol["key"]) for symbol in symbols if symbol.get("key"))
            covering = _graph_tests(graph, path, changed_paths)
            for entry in covering:
                entry["source"] = "graph"
            graph_keys.extend(
                str(test["key"]) for test in covering if test.get("key"))
            if not fkey:
                graph_missing_files.append(path)
        allow_filename_fallback = (
            not usable or graph is None or _graph_file_key(graph, path) is None)
        if allow_filename_fallback:
            for test_path in _fallback_tests(path, repo_tests):
                if not any(entry.get("file") == test_path for entry in covering):
                    covering.append({
                        "key": None,
                        "file": test_path,
                        "qualname": None,
                        "changed": test_path in changed_paths,
                        "source": "filename",
                    })
        item["graph_keys"] = sorted(set(graph_keys))
        item["symbols"] = [
            {
                "key": symbol.get("key"),
                "qualname": symbol.get("qualname"),
                "name": symbol.get("name"),
                "start_line": symbol.get("start_line"),
                "end_line": symbol.get("end_line"),
                "callers": (_graph_callers(graph, symbol["id"])[:MAX_ITEMS]
                            if usable and graph is not None else []),
            }
            for symbol in symbols[:MAX_ITEMS]
        ]
        item["covering_tests"] = covering[:MAX_ITEMS]

        if item["deleted"]:
            if kind == "test":
                severity, ftype = "major", "missing_test"
                message = f"test file {path} was deleted"
            elif kind == "source":
                severity, ftype = "major", "breaking_change"
                message = (
                    f"source file {path} was deleted; imports or callers may "
                    "still reference it")
            else:
                severity, ftype = "note", "breaking_change"
                message = f"file {path} was deleted"
            findings.append(_finding(
                ftype, severity, path, None, message,
                evidence=[f"git diff --name-status: D {path}",
                          f"deletions={item['deletions']}"],
                graph_keys=item["graph_keys"],
                confidence="high",
                confidence_note="the diff status is deterministic",
            ))
        if item["binary"]:
            findings.append(_finding(
                "unknown", "note", path, None,
                f"binary file {path} changed; its content was not reviewed",
                evidence=[f"git diff --numstat: binary (-/-) {path}"],
                graph_keys=item["graph_keys"],
                confidence="high",
                confidence_note="binary detection is deterministic; content "
                                "review is not attempted",
            ))
            not_assessed.append({
                "area": f"content:{path}",
                "reason": "binary content was not reviewed",
            })
        if item["large"]:
            findings.append(_finding(
                "performance_risk", "minor", path, None,
                f"{path} is {item['size']} bytes, above the "
                f"{diff_info.get('max_file_bytes', DEFAULT_MAX_FILE_BYTES)} "
                "byte review threshold",
                evidence=[f"git cat-file -s {path}: {item['size']}",
                          f"max_file_bytes={diff_info.get('max_file_bytes')}"],
                graph_keys=item["graph_keys"],
                confidence="high",
                confidence_note="size is deterministic; it is a review-risk "
                                "flag, not a measured performance regression",
            ))
        if item["generated"]:
            findings.append(_finding(
                "scope_creep", "minor", path, None,
                f"generated file {path} changed; review the generator or "
                "source instead of the output",
                evidence=["path/name matches a generated-file rule",
                          f"status={item['status']}"],
                graph_keys=item["graph_keys"],
                confidence="medium",
                confidence_note="generated-file detection is heuristic",
            ))
        if (kind == "source" and not item["deleted"]
                and not item["binary"] and not item["generated"]
                and (item["additions"] or item["deletions"])):
            if not covering:
                graph_has_file = bool(
                    usable and graph is not None
                    and _graph_file_key(graph, path) is not None)
                if graph_has_file:
                    confidence = "high"
                    note = ("a fresh snapshot records no covering test for "
                            "this file")
                elif usable and graph is not None:
                    confidence = "medium"
                    note = ("the file is not in the graph snapshot; only "
                            "test file names at the reviewed commit were "
                            "compared")
                else:
                    confidence = "medium"
                    note = ("no usable graph; only test file names at the "
                            "reviewed commit were compared, so an unrelated "
                            "covering test may exist")
                evidence = [
                    ("fresh graph: no test edge to "
                     f"{path}") if graph_has_file
                    else "graph unavailable, stale or missing the file",
                ]
                if not graph_has_file:
                    evidence.append(
                        "test file names at head: " + (
                            ", ".join(repo_tests[:20]) if repo_tests
                            else "none"))
                evidence.append(
                    "changed test files: " + (", ".join(changed_tests)
                                              if changed_tests else "none"))
                findings.append(_finding(
                    "missing_test", "major", path,
                    item.get("first_added_line"),
                    f"source file {path} changed without a changed or "
                    "covering test",
                    evidence=evidence, graph_keys=item["graph_keys"],
                    confidence=confidence, confidence_note=note,
                ))
        if (not item["deleted"]
                and kind in {"source", "config", "other"}):
            sensitive = _sensitive_path(path)
            if sensitive:
                evidence = [f"path token: {sensitive}"]
                if item["first_added_line"] is not None:
                    evidence.append(
                        f"first added line: {item['first_added_line']}")
                findings.append(_finding(
                    "security_sensitive", "minor", path,
                    item["first_added_line"],
                    f"{path} is a security-sensitive path ({sensitive})",
                    evidence=evidence,
                    graph_keys=item["graph_keys"],
                    confidence="medium",
                    confidence_note="path matching is a review flag, not a "
                                    "vulnerability assessment",
                ))

    for conflict in diff_info.get("conflict_markers") or []:
        findings.append(_finding(
            "unhandled_error", "blocking", conflict["file"], conflict["line"],
            f"conflict marker in added line: {conflict['marker']}",
            evidence=[f"git diff --unified: {conflict['marker']!r}",
                      "unresolved conflict markers are syntax errors"],
            confidence="high",
            confidence_note="the marker is in an added line",
        ))
    for error in diff_info.get("whitespace") or []:
        findings.append(_finding(
            "unknown", "minor", error["file"], error["line"],
            f"whitespace error: {error['message']}",
            evidence=["git diff --check", error["message"]],
            confidence="high",
            confidence_note="git diff --check reported this exactly",
        ))
    for dangerous in (diff_info.get("dangerous_tokens") or [])[
            :MAX_SENSITIVE_FINDINGS]:
        path = dangerous["file"]
        if _classify_path(path) != "source":
            continue
        findings.append(_finding(
            "security_sensitive", "minor", path, dangerous["line"],
            f"added code uses a sensitive construct: {dangerous['label']}",
            evidence=[f"added line contains {dangerous['label']!r}"],
            confidence="medium",
            confidence_note="token matching is a review flag, not a "
                            "vulnerability finding",
        ))

    if usable and graph is not None:
        symbol_total = 0
        for item in files:
            if item["file_kind"] != "source" or item["deleted"]:
                continue
            for symbol in _graph_symbols(graph, item["path"]):
                symbol_total += 1
                callers = _graph_callers(graph, symbol["id"])
                outside: list[dict[str, Any]] = []
                for caller in callers:
                    caller_file = caller.get("file")
                    if not caller_file:
                        continue
                    if caller_file == item["path"]:
                        continue
                    if _classify_path(caller_file) == "test":
                        continue
                    if caller_file in changed_paths:
                        continue
                    outside.append(caller)
                if not outside:
                    continue
                keys = [str(symbol.get("key") or "")] + [
                    str(caller.get("key") or "") for caller in outside]
                evidence = [
                    f"{CALL_EDGE} edge caller file not in diff: "
                    f"{caller.get('file')}"
                    for caller in outside[:20]
                ]
                findings.append(_finding(
                    "stale_reference", "major", item["path"],
                    symbol.get("start_line"),
                    f"{len(outside)} recorded caller(s) of "
                    f"{symbol.get('qualname')!r} are not in the diff",
                    evidence=evidence, graph_keys=keys,
                    confidence="medium",
                    confidence_note="call edges are syntactic graph records; "
                                    "an unchanged caller can still be correct",
                ))
        if symbol_total and not graph.get("call_edges"):
            omissions.append(
                "The graph snapshot records no calls edges; changed-symbol "
                "caller coverage was not assessed.")
            not_assessed.append({
                "area": "callers",
                "reason": "the graph snapshot has no calls edges",
            })
        elif not symbol_total and any(
                item["file_kind"] == "source" and not item["deleted"]
                for item in files):
            omissions.append(
                "The graph snapshot records no Symbol nodes for the changed "
                "source files; caller coverage was not assessed.")
            not_assessed.append({
                "area": "callers",
                "reason": "the graph has no symbols for the changed files",
            })
    elif graph_info.get("freshness") in {"missing", "stale", "unknown", "error"}:
        omissions.append(
            "Graph evidence was not used: "
            f"{graph_info.get('freshness')}. Only file-level diff checks ran.")
        not_assessed.append({
            "area": "graph",
            "reason": str(graph_info.get("reason") or "graph is not fresh"),
        })

    if graph_info.get("freshness") == "stale":
        findings.append(_finding(
            "stale_reference", "minor", None, None,
            "graph snapshot is stale; graph-derived callers and tests were "
            "not used",
            evidence=[str(graph_info.get("reason") or "stale graph"),
                      f"snapshot: {graph_info.get('path')}"],
            confidence="high",
            confidence_note="freshness compares the snapshot head and working-"
                            "tree digest with the reviewed commit",
        ))
    if graph_missing_files:
        omissions.append(
            f"{len(graph_missing_files)} changed file(s) are not in the graph "
            "snapshot; their symbols, callers and tests were not checked: "
            + ", ".join(graph_missing_files[:10]))
        not_assessed.append({
            "area": "graph-files",
            "reason": "changed files are absent from the snapshot",
            "files": graph_missing_files[:20],
        })
    omissions.append(
        "No model review ran; deterministic findings are evidence, not proof.")
    omissions.append(
        "Tests, builds, linters and type checks were not run. Only resource "
        "admission for a possible run was checked.")
    omissions.append(
        "Semantic compatibility, duplicate logic and contract mismatches "
        "were not assessed without a model or type checker.")
    not_assessed.extend([
        {"area": "model_review", "reason": "optional multi-model review is out "
                                           "of scope for this slice"},
        {"area": "tests", "reason": "no tests were run"},
        {"area": "lint", "reason": "no linter was run"},
        {"area": "types", "reason": "no type checker was run"},
    ])
    if not any(item["file_kind"] == "test" for item in files):
        not_assessed.append({
            "area": "changed-tests",
            "reason": "the diff has no changed test file",
        })
    return findings, list(dict.fromkeys(omissions)), not_assessed


# --------------------------------------------------------------------------- #
# packet
# --------------------------------------------------------------------------- #


def _plan_tests(root: Path, base_commit: str, head_commit: str,
                changed: Sequence[str], graph_file: Path) -> dict[str, Any]:
    """The selection plan recorded in the packet. No test is executed."""
    try:
        plan = test_plans.plan(
            root, base=base_commit, head=head_commit,
            changed=sorted(changed), task_class="code_review",
            graph_path=graph_file)
    except Exception as exc:  # noqa: BLE001 - a review must report, not crash
        return {
            "schema": test_plans.SCHEMA,
            "available": False,
            "executed": False,
            "truth_assessed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "not_run_reason": "the deterministic review does not execute tests",
        }
    plan["executed"] = False
    plan["not_run_reason"] = (
        "the deterministic code review does not execute tests; the plan is "
        "evidence for a later runner")
    return plan


def _library_hint_files(files: Sequence[Mapping[str, Any]]
                        ) -> list[Mapping[str, Any]]:
    """Changed source files the hint layer may read, in diff order."""
    out: list[Mapping[str, Any]] = []
    for item in files:
        if item.get("deleted") or item.get("binary") or item.get("generated"):
            continue
        if item.get("file_kind") != "source":
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            out.append(item)
        if len(out) >= MAX_LIBRARY_HINT_FILES:
            break
    return out


def _library_before_text(root: Path, base: str,
                         files: Sequence[Mapping[str, Any]]
                         ) -> dict[str, str]:
    """The base commit's text for each changed source file, bounded.

    ``None``/absent for an added file is what ``hints_for_paths`` expects.
    A failed or oversized read is skipped; hints are advisory.
    """
    before: dict[str, str] = {}
    for item in files:
        path = str(item.get("path") or "")
        old = str(item.get("old_path") or path)
        if not path or not old:
            continue
        size = _blob_size(root, base, old)
        if size is None or size > LH.MAX_FILE_BYTES:
            continue
        proc = _git(root, ["show", f"{base}:{old}"])
        if proc.returncode != 0:
            continue
        data = proc.stdout
        if len(data) > LH.MAX_FILE_BYTES or b"\0" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        before[path] = text[1:] if text.startswith("\ufeff") else text
    return before


def _library_hints_section(root: Path, base: str,
                           files: Sequence[dict[str, Any]], *,
                           requested: bool) -> dict[str, Any]:
    """The ``library_hints`` packet section. Advisory, read-only, bounded.

    The detector reads the named changed files and the manifests beside them.
    It never records events here (a review writes nothing) and its evidence is
    rule labels plus file/symbol names, never file contents.
    """
    try:
        settings = LH.settings_for(root)
    except Exception:  # noqa: BLE001 - a hint preference must never fail
        settings = LH.HintSettings()
    section: dict[str, Any] = {
        "schema": LH.SCHEMA,
        "rule_version": LH.RULE_VERSION,
        "requested": bool(requested),
        "mode": settings.mode,
        "state": "disabled",
        "files": [],
        "hints": [],
        "counts": {"hints": 0, "suppressed": 0, "files": 0,
                   "truncated": False},
        "recorded": False,
        "truth_assessed": False,
        "source": "working_tree",
        "note": ("capability hints from a curated registry; the named files "
                 "are read from the working tree, so a dirty or non-checkout "
                 "head may differ; no refactor, dependency change, model or "
                 "network call was made"),
    }
    if not requested:
        section["note"] = "library hints were disabled for this review"
        return section
    if settings.mode == "off":
        section["state"] = "off"
        section["note"] = ("library hints are off (settings or "
                           "OZAGIA_LIBRARY_HINTS); no check was made")
        return section
    selected = _library_hint_files(files)
    section["files"] = [str(item["path"]) for item in selected]
    section["counts"]["files"] = len(selected)
    if not selected:
        section["state"] = "no_files"
        return section
    try:
        before = _library_before_text(root, base, selected)
        got = LH.hints_for_paths(
            root, [str(item["path"]) for item in selected],
            mode=settings.mode, before=before, settings=settings,
            record=False, max_per_edit=settings.max_per_edit)
    except Exception as exc:  # noqa: BLE001 - a review must report, not crash
        section["state"] = "error"
        section["error"] = f"{type(exc).__name__}: {exc}"
        return section
    hints = [dict(item) for item in (got.get("hints") or [])
             if isinstance(item, Mapping)]
    suppressed = [dict(item) for item in (got.get("suppressed") or [])
                  if isinstance(item, Mapping)]
    truncated = len(hints) > MAX_LIBRARY_HINTS
    section["hints"] = hints[:MAX_LIBRARY_HINTS]
    section["counts"].update({
        "hints": len(section["hints"]),
        "suppressed": len(suppressed),
        "truncated": truncated,
    })
    section["state"] = "ready" if section["hints"] else "no_hints"
    return section


def _security_section(report: Mapping[str, Any]) -> dict[str, Any]:
    """Copy supplied security findings without running or renumbering them."""
    raw_findings = report.get("findings")
    if not isinstance(raw_findings, list):
        raw_findings = []
    findings = [dict(item) for item in raw_findings
                if isinstance(item, Mapping)][:200]
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        summary = {"total": len(findings)}
    not_assessed = report.get("not_assessed")
    if not isinstance(not_assessed, list):
        not_assessed = []
    return {
        "provided": True,
        "schema": report.get("schema"),
        "findings": findings,
        "summary": dict(summary),
        "not_assessed": [str(value) for value in not_assessed],
        "truth_assessed": False,
        "note": ("security findings were supplied by the caller; this "
                 "review did not run a scan and does not prove "
                 "exploitability"),
    }


OcelotClusterer = Callable[..., Mapping[str, Any]]


def _ocelot_section(findings: Sequence[Mapping[str, Any]], *, root: Path,
                    ocelot: bool | OcelotClusterer | None,
                    ocelot_root: str | Path | None,
                    ocelot_mode: str, ocelot_timeout: int,
                    clusterer: OcelotClusterer | None
                    ) -> dict[str, Any] | None:
    if callable(ocelot) and clusterer is None:
        clusterer = ocelot
        ocelot = True
    if not ocelot and clusterer is None:
        return None
    return ocelot_code.cluster_section(
        findings, root=root, ocelot_root=ocelot_root, mode=ocelot_mode,
        timeout=ocelot_timeout, clusterer=clusterer)


def _ocelot_impact_section(root: Path, changed: Sequence[str], *,
                           graph_path: str | Path | None,
                           test_plan: Mapping[str, Any] | None,
                           requested: Any, ocelot_root: str | Path | None,
                           ocelot_mode: str, ocelot_timeout: int,
                           clusterer: Any) -> dict[str, Any] | None:
    """The optional ``ocelot.impact`` section. No test is executed."""
    if callable(requested) and clusterer is None:
        clusterer = requested
        requested = True
    if not requested and clusterer is None:
        return None
    try:
        shape = ocelot_code.change_shape(
            root, changed=sorted(changed), graph_path=graph_path)
    except Exception as exc:  # noqa: BLE001 - a review must report, not crash
        return ocelot_code.impact_error_section(exc, mode=ocelot_mode)
    if isinstance(test_plan, Mapping):
        selected = sorted(str(path) for path
                          in (test_plan.get("selected_files") or [])
                          if isinstance(path, str))
        shape["planned_tests"] = selected
        shape["test_plan"] = {
            "schema": test_plan.get("schema"),
            "mode": test_plan.get("mode"),
            "selected_files": selected,
            "fallback_used": bool(test_plan.get("fallback_used")),
            "refused": bool(test_plan.get("refused")),
            "executed": False,
            "truth_assessed": False,
            "claim": "test selection only; no test was executed",
        }
    return ocelot_code.impact_section(
        [shape], ocelot_root=ocelot_root, mode=ocelot_mode,
        timeout=ocelot_timeout, clusterer=clusterer, root=root)


def _ocelot_patterns_section(root: Path, changed: Sequence[str], *,
                             graph_path: str | Path | None,
                             requested: Any,
                             ocelot_root: str | Path | None,
                             ocelot_mode: str, ocelot_timeout: int,
                             clusterer: Any) -> dict[str, Any] | None:
    """The optional ``ocelot.patterns`` section. Candidate patterns only."""
    if callable(requested) and clusterer is None:
        clusterer = requested
        requested = True
    if not requested and clusterer is None:
        return None
    return ocelot_code.pattern_section(
        sorted(changed), root=root, graph_path=graph_path,
        ocelot_root=ocelot_root, mode=ocelot_mode,
        timeout=ocelot_timeout, clusterer=clusterer)


def _packet(root: Path, pre: Mapping[str, Any], graph_info: Mapping[str, Any],
            files: Sequence[dict[str, Any]], diff_info: Mapping[str, Any],
            findings: Sequence[Mapping[str, Any]], omissions: Sequence[str],
            not_assessed: Sequence[Mapping[str, Any]],
            test_plan: Mapping[str, Any] | None = None,
            security_report: Mapping[str, Any] | None = None,
            ocelot_section: Mapping[str, Any] | None = None,
            library_hints_section: Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
    range_info = dict(pre.get("range") or {})
    packet = {
        "schema": PACKET_SCHEMA,
        "schema_version": SCHEMA,
        "range": range_info,
        "base": range_info.get("base"),
        "head": range_info.get("head"),
        "base_commit": range_info.get("base_commit"),
        "head_commit": range_info.get("head_commit"),
        "worktree": dict(pre.get("worktree") or {}),
        "preflight": {
            "ok": bool(pre.get("ok")),
            "checks": pre.get("checks") or [],
        },
        "graph": dict(graph_info),
        "resources": dict(pre.get("resources") or {}),
        "diff": {
            "files": diff_info.get("files", 0),
            "added": diff_info.get("added", 0),
            "modified": diff_info.get("modified", 0),
            "deleted": diff_info.get("deleted", 0),
            "renamed": diff_info.get("renamed", 0),
            "binary": diff_info.get("binary", 0),
            "large": diff_info.get("large", 0),
            "generated": diff_info.get("generated", 0),
            "additions": diff_info.get("additions", 0),
            "deletions": diff_info.get("deletions", 0),
            "max_file_bytes": diff_info.get("max_file_bytes"),
        },
        "files": list(files),
        "findings": list(findings),
        "test_plan": dict(test_plan) if test_plan is not None else None,
        "tests_executed": False,
        "summary": {
            "findings": len(findings),
            "blocking": sum(1 for item in findings
                            if item.get("severity") == "blocking"),
            "major": sum(1 for item in findings
                         if item.get("severity") == "major"),
            "minor": sum(1 for item in findings
                         if item.get("severity") == "minor"),
            "note": sum(1 for item in findings
                        if item.get("severity") == "note"),
        },
        "omissions": list(omissions),
        "not_assessed": list(not_assessed),
        "model_review": {
            "status": "not_run",
            "reason": "optional multi-model review is out of scope for this "
                      "slice; no model was called",
            "truth_assessed": False,
        },
        "library_hints": dict(library_hints_section)
        if library_hints_section is not None else {
            "requested": False,
            "state": "disabled",
            "hints": [],
            "truth_assessed": False,
        },
        "truth_assessed": False,
        "root": str(root),
    }
    if library_hints_section is not None:
        packet["summary"]["library_hints"] = len(
            library_hints_section.get("hints") or [])
        packet["summary"]["library_hints_state"] = library_hints_section.get(
            "state")
    if security_report is not None:
        section = _security_section(security_report)
        packet["security"] = section
        packet["summary"]["security_findings"] = len(section["findings"])
    if ocelot_section is not None:
        packet["ocelot"] = dict(ocelot_section)
        packet["summary"]["ocelot_clusters"] = len(
            ocelot_section.get("clusters") or [])
        packet["summary"]["ocelot_state"] = ocelot_section.get("state")
        impact = ocelot_section.get("impact")
        if isinstance(impact, Mapping):
            packet["summary"]["ocelot_impact_clusters"] = len(
                impact.get("clusters") or [])
            packet["summary"]["ocelot_impact_state"] = impact.get("state")
        patterns = ocelot_section.get("patterns")
        if isinstance(patterns, Mapping):
            packet["summary"]["ocelot_patterns"] = len(
                patterns.get("patterns") or [])
            packet["summary"]["ocelot_patterns_state"] = patterns.get("state")
    return packet


def review_diff(root: str | Path, *, base: str, head: str = "HEAD",
                graph_path: str | Path | None = None,
                max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                memory_mb: float = 2048.0, disk_mb: float = 4096.0,
                config_dir: str | Path | None = None,
                worktree_base: str = "main",
                security_report: Mapping[str, Any] | None = None,
                ocelot: bool | OcelotClusterer | None = False,
                ocelot_root: str | Path | None = None,
                ocelot_mode: str = "auto",
                ocelot_timeout: int = ocelot_code.DEFAULT_TIMEOUT,
                ocelot_clusterer: OcelotClusterer | None = None,
                ocelot_impact: Any = False,
                ocelot_patterns: Any = False,
                ocelot_impact_clusterer: Any = None,
                ocelot_patterns_clusterer: Any = None,
                library_hints: bool = True
                ) -> dict[str, Any]:
    """Review ``base..head`` deterministically and return a review packet.

    Raises :class:`PreflightError` when the worktree, conflicts, range or
    resource admission refuse the run. No model is called.
    """
    if max_file_bytes < 0:
        raise CodeReviewError("max_file_bytes must be >= 0")
    if memory_mb < 0 or disk_mb < 0:
        raise CodeReviewError("memory_mb and disk_mb must be >= 0")
    root_path = _repository_root(root)
    base_rev = _valid_rev(base, "base")
    head_rev = _valid_rev(head, "head")
    base_sha = _resolve_commit(root_path, base_rev, "base")
    head_sha = _resolve_commit(root_path, head_rev, "head")
    pre = preflight(
        root_path, base=base_rev, head=head_rev,
        base_commit=base_sha, head_commit=head_sha,
        memory_mb=memory_mb, disk_mb=disk_mb, config_dir=config_dir,
        worktree_base=worktree_base,
    )
    if not pre.get("ok"):
        refused = _refusal(pre)
        message = (f"review refused by {pre.get('reason')}: "
                   + "; ".join(item["message"] for item in pre["checks"]
                               if item.get("level") == "error"))
        raise PreflightError(message, refused)
    graph_file = _resolve_graph(root_path, graph_path)
    graph_info, graph = _load_graph(
        graph_file, root_path, head_sha,
        (pre.get("worktree") or {}).get("head"))
    files, diff_info = _diff_files(
        root_path, base_sha, head_sha, max_file_bytes=max_file_bytes)
    diff_info["max_file_bytes"] = max_file_bytes
    changed_paths = {item["path"] for item in files}
    changed_paths.update(
        item["old_path"] for item in files if item.get("old_path"))
    repo_tests = _tree_tests(root_path, head_sha)
    findings, omissions, not_assessed = _derive_findings(
        files, diff_info, graph_info, graph, changed_paths, repo_tests)
    test_plan = _plan_tests(root_path, base_sha, head_sha,
                            sorted(changed_paths), graph_file)
    if test_plan.get("selected_files"):
        not_assessed.append({
            "area": "tests",
            "reason": (
                f"{len(test_plan['selected_files'])} test file(s) were planned "
                "but not executed; the packet records a selection, not a "
                "result"),
        })
    else:
        not_assessed.append({
            "area": "tests",
            "reason": (
                test_plan.get("not_run_reason")
                or "no test was selected and no test was executed"),
        })
    if security_report is not None:
        not_assessed.append({
            "area": "security",
            "reason": (
                "security findings were supplied by the caller; this review "
                "did not run a scan, and a finding is not a proof of "
                "exploitability"),
        })
    final_findings = _finalize_findings(findings)
    ocelot_section = _ocelot_section(
        final_findings, root=root_path, ocelot=ocelot,
        ocelot_root=ocelot_root, ocelot_mode=ocelot_mode,
        ocelot_timeout=ocelot_timeout, clusterer=ocelot_clusterer)
    if ocelot_section is not None:
        state = str(ocelot_section.get("state") or "unknown")
        not_assessed.append({
            "area": "ocelot",
            "reason": (
                "structural clustering is a candidate signal; it did not "
                "assess truth, and exact findings and dissent remain"),
        })
        if state == "no_findings":
            omissions.append(
                "Ocelot clustering was requested but there were no findings "
                "to cluster.")
        elif state != "ready":
            omissions.append(
                f"Ocelot structural clustering was requested but is {state}; "
                "exact clusters by issue family and target were reported. No "
                "install or download was attempted.")
    impact_section = _ocelot_impact_section(
        root_path, sorted(changed_paths), graph_path=graph_file,
        test_plan=test_plan, requested=ocelot_impact,
        ocelot_root=ocelot_root, ocelot_mode=ocelot_mode,
        ocelot_timeout=ocelot_timeout,
        clusterer=ocelot_impact_clusterer)
    patterns_section = _ocelot_patterns_section(
        root_path, sorted(changed_paths), graph_path=graph_file,
        requested=ocelot_patterns, ocelot_root=ocelot_root,
        ocelot_mode=ocelot_mode, ocelot_timeout=ocelot_timeout,
        clusterer=ocelot_patterns_clusterer)
    if impact_section is not None or patterns_section is not None:
        if ocelot_section is None:
            ocelot_section = ocelot_code.empty_cluster_section(
                mode=ocelot_mode,
                note=("finding clustering was not requested; only the "
                      "requested impact/pattern sections were computed"))
        if impact_section is not None:
            ocelot_section["impact"] = impact_section
            not_assessed.append({
                "area": "ocelot-impact",
                "reason": (
                    "structural test-impact analogy is a candidate; the "
                    "suggested test set is not complete and no test was run"),
            })
            if str(impact_section.get("state")) not in {"ready", "no_shapes"}:
                omissions.append(
                    "Ocelot impact clustering was requested but is "
                    f"{impact_section.get('state')}; exact digest/overlap "
                    "clusters were reported and no install was attempted.")
        if patterns_section is not None:
            ocelot_section["patterns"] = patterns_section
            not_assessed.append({
                "area": "ocelot-patterns",
                "reason": (
                    "architecture patterns are candidate clusters; they do "
                    "not establish an anti-pattern, a defect or intent"),
            })
            if str(patterns_section.get("state")) not in {"ready", "no_shapes"}:
                omissions.append(
                    "Ocelot pattern clustering was requested but is "
                    f"{patterns_section.get('state')}; exact signature/"
                    "overlap patterns were reported and no install was "
                    "attempted.")
    library_section = _library_hints_section(
        root_path, base_sha, files, requested=library_hints)
    if library_section["state"] in {"disabled", "off"}:
        not_assessed.append({
            "area": "library-hints",
            "reason": (
                "library hints were not checked; no library-reuse signal was "
                "assessed"),
        })
    else:
        not_assessed.append({
            "area": "library-hints",
            "reason": (
                "capability hints are heuristic and cover only the changed "
                "source files; no refactor or dependency change was made"),
        })
    packet = _packet(root_path, pre, graph_info, files, diff_info,
                     final_findings, omissions, not_assessed,
                     test_plan, security_report, ocelot_section,
                     library_section)
    return packet


def review_range(root: str | Path, range_spec: str, **kwargs: Any) -> dict[str, Any]:
    """``review_diff`` for a ``BASE..HEAD`` string."""
    base, head = parse_range(range_spec)
    return review_diff(root, base=base, head=head, **kwargs)


# --------------------------------------------------------------------------- #
# optional injected review (no live model call in this module)
# --------------------------------------------------------------------------- #


class ReviewRunner(Protocol):
    """A caller-supplied reviewer.

    The callable receives a read-only packet and returns model-shaped
    findings. It may call a model or a subprocess; this module does not. The
    caller owns gating (``OZAGIA_EVAL_LIVE``, explicit approval, budgets).
    """

    def __call__(self, packet: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        ...


def _copy_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(packet)))


def _bounded_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return []
    if isinstance(value, (set, frozenset)):
        try:
            return [str(entry) for entry in sorted(value, key=str)]
        except TypeError:
            return [str(entry) for entry in value]
    try:
        values = list(value)
    except TypeError:
        return []
    return [str(entry) for entry in values]


def _runner_name(runner: Any) -> str:
    name = getattr(runner, "__name__", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return type(runner).__name__


def _reviewer_pairs(runners: Any) -> list[tuple[str, Any]] | None:
    pairs: list[tuple[str, Any]] = []
    if isinstance(runners, Mapping):
        pairs = [(str(name), runner) for name, runner in runners.items()]
    elif callable(runners):
        pairs = [(_runner_name(runners), runners)]
    elif (isinstance(runners, Sequence)
          and not isinstance(runners, (str, bytes, bytearray))):
        pairs = [(_runner_name(runner), runner) for runner in runners]
    else:
        return None
    # Sort before naming so mapping insertion order cannot change the packet.
    ordered = sorted(enumerate(pairs), key=lambda pair: (pair[1][0], pair[0]))
    counts: dict[str, int] = {}
    result: list[tuple[str, Any]] = []
    for _, (base, runner) in ordered:
        base = base.strip() or "reviewer"
        counts[base] = counts.get(base, 0) + 1
        name = base if counts[base] == 1 else f"{base}#{counts[base]}"
        result.append((name, runner))
    return result


def _reviewer_finding(item: Any, reviewer: str) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    message = str(item.get("message") or "").strip()
    if not message:
        return None
    ftype = str(item.get("type") or "unknown")
    severity = str(item.get("severity") or "note").casefold()
    line = item.get("line")
    if isinstance(line, str) and line.isdigit():
        line = int(line)
    if isinstance(line, bool) or not isinstance(line, int):
        line = None
    return {
        "id": None,
        "type": ftype if ftype in FINDING_TYPES else "unknown",
        "severity": severity if severity in SEVERITY_RANK else "note",
        "file": item.get("file") if isinstance(item.get("file"), str) else None,
        "line": line,
        "message": message,
        "evidence": _string_list(item.get("evidence")),
        "graph_keys": _string_list(item.get("graph_keys")),
        "confidence": str(item.get("confidence") or "low"),
        "confidence_note": str(
            item.get("confidence_note")
            or "injected reviewer output; unverified and not proof"),
        "source": "model",
        "reviewer": reviewer,
        "reviewers": [reviewer],
        "agreement": 1,
    }


def _finding_identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
    line = item.get("line")
    if isinstance(line, bool):
        line = None
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = None
    return (
        str(item.get("type") or ""),
        str(item.get("severity") or ""),
        "" if item.get("file") is None else str(item.get("file")),
        line,
        str(item.get("message") or ""),
        tuple(_string_list(item.get("evidence"))),
        tuple(_string_list(item.get("graph_keys"))),
    )


def _normalise_reviewer_output(
        raw: Any, reviewer: str) -> tuple[list[dict[str, Any]], str | None]:
    if raw is None:
        return [], None
    if isinstance(raw, Mapping):
        values = [raw]
    elif isinstance(raw, (str, bytes, bytearray)):
        return [], (
            "reviewer output must be a sequence of findings, got "
            f"{type(raw).__name__}")
    else:
        try:
            values = list(raw)
        except TypeError:
            return [], (
                "reviewer output must be a sequence of findings, got "
                f"{type(raw).__name__}")
    seen: set[tuple[Any, ...]] = set()
    findings: list[dict[str, Any]] = []
    for value in values:
        item = _reviewer_finding(value, reviewer)
        if item is None:
            continue
        key = _finding_identity(item)
        if key in seen:
            continue
        seen.add(key)
        findings.append(item)
    findings.sort(key=_finding_sort_key)
    return findings, None


def _merge_reviewer_findings(
        existing: Sequence[Mapping[str, Any]],
        reviewer_findings: Sequence[tuple[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[tuple[Any, ...], str],
           dict[str, set[tuple[Any, ...]]],
           dict[tuple[Any, ...], set[str]]]:
    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in existing:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        item["graph_keys"] = sorted(set(_string_list(item.get("graph_keys"))))
        item["evidence"] = list(
            dict.fromkeys(_string_list(item.get("evidence"))))
        records.setdefault(_finding_identity(item), item)
    names: dict[tuple[Any, ...], set[str]] = {}
    per_reviewer: dict[str, set[tuple[Any, ...]]] = {}
    for reviewer, findings in reviewer_findings:
        keys = per_reviewer.setdefault(reviewer, set())
        for finding in findings:
            key = _finding_identity(finding)
            records.setdefault(key, dict(finding))
            names.setdefault(key, set()).add(reviewer)
            keys.add(key)
    combined: list[dict[str, Any]] = []
    for key, item in records.items():
        labels = sorted(names.get(key) or ())
        if labels:
            item["reviewers"] = labels
            item["agreement"] = len(labels)
            if len(labels) == 1:
                item["reviewer"] = labels[0]
            else:
                item.pop("reviewer", None)
        combined.append(item)
    final = _finalize_findings(combined)
    id_by_key = {_finding_identity(item): str(item["id"]) for item in final}
    return final, id_by_key, per_reviewer, names


def _reviewer_error(name: str, error: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "error",
        "findings": 0,
        "error": _bounded_text(error, MAX_REVIEWER_ERROR_CHARS),
        "finding_ids": [],
    }


def run_injected_reviews(
        packet: Mapping[str, Any],
        runners: (Mapping[str, ReviewRunner] | Sequence[ReviewRunner]
                  | ReviewRunner),
) -> dict[str, Any]:
    """Run injected reviewers and merge their findings into a packet copy.

    ``runners`` is a mapping of reviewer name to callable, or a sequence of
    callables named from ``__name__``/their type. Reviewers are ordered by
    name and each receives its own deep copy of the original packet, so one
    reviewer cannot modify another's input or the source packet. A reviewer
    exception or an invalid top-level return becomes a per-reviewer
    ``status: "error"`` entry; it never aborts the review.

    Exact duplicate findings across reviewers collapse to one finding with a
    ``reviewers`` label and an ``agreement`` count. Agreement, dissent and
    reviewer errors are recorded, but ``truth_assessed`` stays false: more
    than one reviewer saying the same thing is not correctness, and no
    finding is proof.
    """
    pairs = _reviewer_pairs(runners)
    result = _copy_packet(packet)
    if pairs is None:
        result["model_review"] = {
            "status": "error",
            "error": ("runners must be a mapping of names to callables or a "
                      "sequence of callables"),
            "reviewers": [],
            "reviewer_count": 0,
            "findings": 0,
            "agreement": 0,
            "dissent": 0,
            "truth_assessed": False,
            "note": "no reviewer was called",
        }
        result["truth_assessed"] = False
        return result

    entries: list[dict[str, Any]] = []
    accepted: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
    accepted_count = 0
    for name, runner in pairs:
        if not callable(runner):
            entries.append(_reviewer_error(
                name, "reviewer is not callable; it was not run"))
            continue
        try:
            raw = runner(_copy_packet(packet))
        except Exception as exc:  # noqa: BLE001 - a reviewer is an external edge
            entries.append(_reviewer_error(
                name, f"{type(exc).__name__}: {exc}"))
            continue
        findings, error = _normalise_reviewer_output(raw, name)
        if error is not None:
            entries.append(_reviewer_error(name, error))
            continue
        accepted.append((name, findings))
        accepted_count += len(findings)
        entries.append({
            "name": name,
            "status": "ok",
            "findings": len(findings),
            "error": None,
            "finding_ids": [],
        })

    final, id_by_key, per_reviewer, names = _merge_reviewer_findings(
        list(result.get("findings") or []), accepted)
    order = {str(item["id"]): number
             for number, item in enumerate(final)}
    for entry in entries:
        ids = [id_by_key[key] for key in per_reviewer.get(entry["name"], ())
               if key in id_by_key]
        entry["finding_ids"] = sorted(
            set(ids), key=lambda value: order.get(value, 0))

    groups: list[tuple[dict[str, Any], list[str]]] = []
    for item in final:
        labels = sorted(names.get(_finding_identity(item)) or ())
        if labels:
            groups.append((item, labels))
    agreement_detail: list[dict[str, Any]] = []
    dissent_detail: list[dict[str, Any]] = []
    agreement = 0
    dissent = 0
    for item, labels in groups:
        if len(labels) > 1:
            agreement += 1
            if len(agreement_detail) < MAX_REVIEW_DETAIL:
                agreement_detail.append({
                    "finding": item["id"],
                    "type": item["type"],
                    "file": item.get("file"),
                    "reviewers": labels,
                    "count": len(labels),
                })
        else:
            dissent += 1
            if len(dissent_detail) < MAX_REVIEW_DETAIL:
                dissent_detail.append({
                    "finding": item["id"],
                    "type": item["type"],
                    "file": item.get("file"),
                    "reviewer": labels[0],
                })

    result["findings"] = final
    _recompute_summary(result)
    section: dict[str, Any] = {
        "status": "injected",
        "reviewer_count": len(entries),
        "reviewers": entries,
        "findings": len(groups),
        "accepted_findings": accepted_count,
        "duplicate_findings": max(0, accepted_count - len(groups)),
        "agreement": agreement,
        "dissent": dissent,
        "agreement_detail": agreement_detail,
        "dissent_detail": dissent_detail,
        "detail_truncated": (
            agreement > len(agreement_detail)
            or dissent > len(dissent_detail)),
        "errors": [{"reviewer": entry["name"], "error": entry["error"]}
                   for entry in entries if entry["status"] == "error"],
        "truth_assessed": False,
        "note": ("injected reviewer output is unverified; agreement is not "
                 "correctness and is not proof"),
    }
    if len(entries) == 1:
        section["runner"] = entries[0]["name"]
    if not entries:
        section["note"] = "no reviewer was supplied; no model was called"
    result["model_review"] = section
    result["truth_assessed"] = False
    return result


def run_injected_review(packet: Mapping[str, Any],
                        runner: ReviewRunner) -> dict[str, Any]:
    """Merge one injected reviewer's findings into a copy of the packet.

    This is the single-reviewer compatibility wrapper over
    :func:`run_injected_reviews`. Reviewer output is labelled ``model`` and
    remains unverified. Agreement between reviewers is still not proof, so
    ``truth_assessed`` stays false.
    """
    return run_injected_reviews(packet, [runner])


# --------------------------------------------------------------------------- #
# optional injected test execution (no test process in this module)
# --------------------------------------------------------------------------- #


class TestRunner(Protocol):
    """A caller-supplied test executor for an existing ``test_plan``.

    The callable receives a deep copy of one plan and returns a result
    mapping. It may run a test command; this module never does. The caller
    owns the process, sandbox, timeout, network and approval policy.
    """

    def __call__(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


def _plan_selected(plan: Mapping[str, Any]) -> list[str]:
    raw = plan.get("selected_files") or []
    if isinstance(raw, (str, bytes, bytearray, Mapping)):
        return []
    try:
        values = list(raw)
    except TypeError:
        return []
    return list(dict.fromkeys(
        path for path in values
        if isinstance(path, str) and path.strip()))


def _plan_reason(plan: Mapping[str, Any], fallback: str) -> str:
    reason = plan.get("not_run_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return fallback


def _without_tests_not_assessed(packet: Mapping[str, Any]) -> list[Any]:
    kept: list[Any] = []
    for entry in packet.get("not_assessed") or []:
        if isinstance(entry, Mapping) and entry.get("area") == "tests":
            continue
        kept.append(entry)
    return kept


def _test_execution_skipped(result: dict[str, Any], name: str, status: str,
                            reason: str, selected: Sequence[str]
                            ) -> dict[str, Any]:
    result["test_execution"] = {
        "status": status,
        "source": name,
        "verified": False,
        "selected": list(selected),
        "reason": reason,
        "note": "no test was run",
        "truth_assessed": False,
    }
    result["truth_assessed"] = False
    return result


def _test_execution_error(result: dict[str, Any], name: str,
                          error: str, selected: Sequence[str]
                          ) -> dict[str, Any]:
    detail = _bounded_text(error, MAX_TEST_ERROR_CHARS)
    result["test_execution"] = {
        "status": "error",
        "source": name,
        "verified": False,
        "selected": list(selected),
        "error": detail,
        "note": ("the injected test result was rejected; no test result was "
                 "recorded"),
        "truth_assessed": False,
    }
    not_assessed = _without_tests_not_assessed(result)
    not_assessed.append({
        "area": "tests",
        "reason": _bounded_text(
            f"the injected test runner failed or returned a malformed "
            f"result ({error}); no test result was recorded",
            MAX_TEST_NOTE_CHARS),
    })
    result["not_assessed"] = not_assessed
    result["truth_assessed"] = False
    return result


def _bounded_output(text: str) -> tuple[str, bool]:
    truncated = False
    lines = text.splitlines()
    if len(lines) > MAX_TEST_OUTPUT_LINES:
        text = "\n".join(lines[:MAX_TEST_OUTPUT_LINES])
        truncated = True
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEST_OUTPUT_BYTES:
        text = encoded[:MAX_TEST_OUTPUT_BYTES].decode("utf-8", "ignore")
        truncated = True
    return text, truncated


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_test_result(
        raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, Mapping):
        return None, (
            "the result must be a mapping, got "
            f"{type(raw).__name__}")
    exit_code = raw.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return None, "exit_code must be an integer"
    if not 0 <= exit_code <= 255:
        return None, "exit_code must be between 0 and 255"
    counts: dict[str, int] = {}
    for key in ("passed", "failed", "skipped"):
        if key not in raw:
            return None, f"missing count: {key}"
        value = raw[key]
        if not _is_count(value):
            return None, f"{key} must be a non-negative integer"
        counts[key] = value
    if "errors" in raw:
        value = raw["errors"]
        if not _is_count(value):
            return None, "errors must be a non-negative integer"
        counts["errors"] = value
    output = raw.get("output")
    if output is None:
        output = ""
    if not isinstance(output, str):
        return None, "output must be a string"
    excerpt, output_truncated = _bounded_output(output)
    execution: dict[str, Any] = {
        "status": "injected",
        "verified": False,
        "exit_code": exit_code,
        "counts": counts,
        "output_excerpt": excerpt,
        "output_truncated": output_truncated,
        "note": ("results were supplied by a caller-run test runner; they "
                 "are unverified and were not independently checked"),
        "truth_assessed": False,
    }
    if "duration_s" in raw and raw["duration_s"] is not None:
        duration = raw["duration_s"]
        if (isinstance(duration, bool)
                or not isinstance(duration, (int, float))):
            return None, "duration_s must be a non-negative finite number"
        try:
            finite = math.isfinite(duration) and duration >= 0
        except (OverflowError, ValueError):
            finite = False
        if not finite:
            return None, "duration_s must be a non-negative finite number"
        execution["duration_s"] = float(duration)
    return execution, None


def run_injected_tests(packet: Mapping[str, Any],
                       runner: TestRunner) -> dict[str, Any]:
    """Record one injected test runner's result against a packet's plan.

    Tests run only when the packet has a ``test_plan`` that is not refused and
    has a non-empty ``selected_files``. Otherwise the copy records
    ``test_execution.status`` ``skipped`` or ``refused`` with the plan's own
    reason and nothing is invented.

    A malformed result (or a runner exception) becomes a structured
    ``status: "error"`` entry instead of an exception. A non-zero exit code
    adds a blocking ``test_failure`` finding so :func:`exit_code` can fail.
    ``truth_assessed`` stays false: the results are caller-supplied and were
    not independently verified here. This module does not run tests, git or a
    subprocess.
    """
    result = _copy_packet(packet)
    name = _runner_name(runner)
    plan = result.get("test_plan")
    if not isinstance(plan, Mapping):
        return _test_execution_skipped(
            result, name, "skipped",
            "no test plan in the packet; no test was run", [])
    selected = _plan_selected(plan)
    if bool(plan.get("refused")):
        return _test_execution_skipped(
            result, name, "refused",
            _plan_reason(
                plan,
                "the test plan refused to select tests; no test was run"),
            selected)
    if not selected:
        return _test_execution_skipped(
            result, name, "skipped",
            _plan_reason(
                plan,
                "the test plan selected no test files; no test was run"),
            selected)
    if not callable(runner):
        return _test_execution_error(
            result, name, "runner is not callable", selected)
    try:
        raw = runner(_copy_packet(plan))
    except Exception as exc:  # noqa: BLE001 - a test runner is an external edge
        return _test_execution_error(
            result, name, f"{type(exc).__name__}: {exc}", selected)
    execution, error = _validate_test_result(raw)
    if execution is None:
        return _test_execution_error(
            result, name, str(error), selected)
    execution["source"] = name
    execution["selected"] = list(selected)
    result["test_execution"] = execution
    result["tests_executed"] = True
    plan_copy = dict(plan)
    plan_copy["executed"] = True
    plan_copy["not_run_reason"] = None
    plan_copy["claim"] = (
        "tests were executed by a caller-supplied runner; results are "
        "unverified")
    plan_copy["truth_assessed"] = False
    result["test_plan"] = plan_copy
    not_assessed = _without_tests_not_assessed(result)
    not_assessed.append({
        "area": "tests",
        "reason": _bounded_text(
            "test results were supplied by a caller-run test runner; they are "
            "unverified, and a passing run does not prove correctness",
            MAX_TEST_NOTE_CHARS),
    })
    result["not_assessed"] = not_assessed
    if execution["exit_code"] != 0:
        failure = _finding(
            "test_failure", "blocking", None, None,
            (f"injected test runner reported "
             f"{execution['counts']['failed']} failed test(s) with exit code "
             f"{execution['exit_code']}"),
            evidence=["test_plan"],
            confidence="low",
            confidence_note=(
                "injected test runner; not independently verified"),
        )
        result["findings"] = _finalize_findings(
            list(result.get("findings") or []) + [failure])
        _recompute_summary(result)
    result["truth_assessed"] = False
    return result


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


def exit_code(packet: Mapping[str, Any], fail_on: str = "blocking") -> int:
    """0 unless a finding is at or above ``fail_on`` (``never``: always 0).

    When the caller supplied a security report, its findings participate:
    critical maps to blocking, high to major, medium to minor, and low/info
    to note. The review still does not assess exploitability.
    """
    if fail_on not in FAIL_ON:
        raise ValueError(f"fail_on must be one of {FAIL_ON}")
    if fail_on == "never":
        return 0
    threshold = SEVERITY_RANK[fail_on]
    for item in packet.get("findings") or []:
        if SEVERITY_RANK.get(str(item.get("severity")), 99) <= threshold:
            return 1
    security = packet.get("security")
    if isinstance(security, Mapping):
        for item in security.get("findings") or []:
            if not isinstance(item, Mapping):
                continue
            mapped = SECURITY_TO_REVIEW.get(str(item.get("severity")), "note")
            if SEVERITY_RANK[mapped] <= threshold:
                return 1
    return 0


def atomic_write(path: str | Path, text: str) -> None:
    """Write text to ``path`` atomically in the same directory."""
    try:
        target = Path(path).expanduser()
    except (OSError, RuntimeError) as exc:
        raise CodeReviewError(f"cannot resolve output path {path!r}: {exc}") from exc
    parent = target.parent
    if not parent.is_dir():
        raise CodeReviewError(
            f"output directory does not exist: {parent}")
    handle = None
    tmp: str | None = None
    try:
        handle, tmp = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            handle = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        tmp = None
    except OSError as exc:
        raise CodeReviewError(f"cannot write {target}: {exc}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def packet_json(packet: Mapping[str, Any]) -> str:
    return json.dumps(packet, indent=2, sort_keys=True) + "\n"


def format_packet(packet: Mapping[str, Any]) -> str:
    range_info = packet.get("range") or {}
    worktree = packet.get("worktree") or {}
    graph = packet.get("graph") or {}
    resources_info = packet.get("resources") or {}
    lines = [
        f"code review: {range_info.get('spec') or 'unknown range'} "
        f"({str(range_info.get('base_commit') or '')[:12]} -> "
        f"{str(range_info.get('head_commit') or '')[:12]})",
        f"worktree: {worktree.get('path')} "
        f"({worktree.get('branch') or 'detached'}, "
        f"{'clean' if worktree.get('clean') else 'dirty'})",
        f"graph: {graph.get('freshness')} ({graph.get('path')})"
        + (f" - {graph.get('reason')}" if graph.get("reason") else ""),
        f"resources: {'ok' if resources_info.get('ok') else 'refused'} "
        f"(requested {resources_info.get('requested')})",
    ]
    diff = packet.get("diff") or {}
    lines.append(
        f"files: {diff.get('files', 0)} changed, {diff.get('additions', 0)} "
        f"added lines, {diff.get('deletions', 0)} deleted lines")
    test_plan = packet.get("test_plan") or {}
    if test_plan:
        executed = bool(packet.get("tests_executed")
                        or test_plan.get("executed"))
        lines.append(
            f"test plan: {test_plan.get('mode')} "
            f"({len(test_plan.get('selected_files') or [])} selected, "
            f"fallback={test_plan.get('fallback_used')}); "
            + ("tests were run by an injected runner (unverified)"
               if executed else "tests were not run")
            + (f" - {test_plan['not_run_reason']}"
               if not executed and test_plan.get("not_run_reason") else ""))
    execution = packet.get("test_execution")
    if isinstance(execution, Mapping):
        if execution.get("status") == "injected":
            counts = execution.get("counts") or {}
            lines.append(
                f"test execution: injected by {execution.get('source')}, "
                f"exit {execution.get('exit_code')}, "
                f"{counts.get('passed', 0)} passed, "
                f"{counts.get('failed', 0)} failed, "
                f"{counts.get('skipped', 0)} skipped"
                + (f", {counts['errors']} error(s)"
                   if "errors" in counts else "")
                + "; results are unverified")
            if execution.get("output_truncated"):
                lines.append("  output excerpt was truncated")
        else:
            reason = execution.get("reason") or execution.get("error") or ""
            lines.append(
                f"test execution: {execution.get('status')}"
                + (f" - {reason}" if reason else "")
                + "; no test result was recorded")
    model_review = packet.get("model_review")
    if isinstance(model_review, Mapping) and model_review.get("status") == "injected":
        reviewers = model_review.get("reviewers") or []
        errors = model_review.get("errors") or []
        lines.append(
            f"model review: injected, {len(reviewers)} reviewer(s), "
            f"{model_review.get('findings', 0)} finding(s), "
            f"agreement={model_review.get('agreement', 0)}, "
            f"dissent={model_review.get('dissent', 0)}"
            + (f", {len(errors)} reviewer error(s)" if errors else "")
            + "; unverified, agreement is not correctness")
        for entry in reviewers:
            if not isinstance(entry, Mapping):
                continue
            detail = (f" ({entry['error']})"
                      if entry.get("error") else "")
            lines.append(
                f"  {entry.get('name')}: {entry.get('status')}, "
                f"{entry.get('findings', 0)} finding(s){detail}")
    library = packet.get("library_hints")
    if isinstance(library, Mapping):
        hints = library.get("hints") or []
        if library.get("state") in {"disabled", "off"}:
            lines.append(
                f"library hints: {library.get('state')}; not checked")
        else:
            lines.append(
                f"library hints: {library.get('state')}, {len(hints)} "
                f"candidate(s) from {library.get('counts', {}).get('files', 0)} "
                "file(s); advisory only, nothing was refactored")
            for hint in hints:
                lines.append(
                    f"  {hint.get('file')}::{hint.get('symbol')} "
                    f"{hint.get('capability')} -> {hint.get('package')} "
                    f"({hint.get('availability')}, "
                    f"{hint.get('confidence')})")
    ocelot = packet.get("ocelot")
    if isinstance(ocelot, Mapping):
        clusters = ocelot.get("clusters") or []
        analogies = ocelot.get("analogies") or []
        lines.append(
            f"ocelot: {ocelot.get('state')} ({ocelot.get('engine')}), "
            f"{len(clusters)} cluster(s), {len(analogies)} analogies; "
            "candidate only, truth not assessed")
        impact = ocelot.get("impact")
        if isinstance(impact, Mapping):
            impact_clusters = impact.get("clusters") or []
            lines.append(
                f"ocelot impact: {impact.get('state')} "
                f"({impact.get('engine')}), {len(impact_clusters)} "
                "cluster(s); candidate test sets, no test was run")
            for cluster in impact_clusters:
                if not isinstance(cluster, Mapping):
                    continue
                tests = cluster.get("candidate_tests") or []
                lines.append(
                    f"  {cluster.get('cluster_id')} "
                    f"[{cluster.get('family')}] "
                    f"{len(cluster.get('shape_ids') or [])} shape(s), "
                    f"tests: {', '.join(tests[:8]) if tests else '-'}; "
                    "not complete")
        patterns = ocelot.get("patterns")
        if isinstance(patterns, Mapping):
            found = patterns.get("patterns") or []
            lines.append(
                f"ocelot patterns: {patterns.get('state')} "
                f"({patterns.get('engine')}), {len(found)} candidate "
                "pattern(s); not a verdict")
            for pattern in found:
                if not isinstance(pattern, Mapping):
                    continue
                lines.append(
                    f"  {pattern.get('pattern_id')} "
                    f"[{pattern.get('family')}] "
                    f"{pattern.get('file_count', 0)} file(s), "
                    f"origin {pattern.get('origin')}; candidate only")
    findings = packet.get("findings") or []
    lines.append(f"findings: {len(findings)}")
    for item in findings:
        location = item.get("file") or "(packet)"
        if item.get("line"):
            location += f":{item['line']}"
        lines.append(
            f"  [{item.get('severity')}] {item.get('id')} "
            f"{item.get('type')} {location}: {item.get('message')}")
        for evidence in item.get("evidence") or []:
            lines.append(f"    evidence: {evidence}")
        if item.get("graph_keys"):
            lines.append(f"    graph: {', '.join(item['graph_keys'])}")
        if item.get("confidence_note"):
            lines.append(f"    confidence: {item.get('confidence')} - "
                         f"{item.get('confidence_note')}")
    if not findings:
        lines.append("  none")
    security = packet.get("security")
    if isinstance(security, Mapping):
        supplied = security.get("findings") or []
        lines.append(f"security: {len(supplied)} supplied finding(s); "
                     "no security scan was run by the review")
        for item in supplied[:10]:
            location = item.get("file") or "(no file)"
            if item.get("line"):
                location += f":{item['line']}"
            lines.append(
                f"  [{item.get('severity')}] {item.get('id')} "
                f"{item.get('category')} {location}: {item.get('message')}")
        if len(supplied) > 10:
            lines.append(f"  ... {len(supplied) - 10} more")
    omissions = packet.get("omissions") or []
    lines.append("omissions / not assessed:")
    if omissions:
        for omission in omissions:
            lines.append(f"  - {omission}")
    else:
        lines.append("  none")
    for entry in packet.get("not_assessed") or []:
        if not isinstance(entry, Mapping):
            continue
        area = entry.get("area") or "unknown"
        reason = entry.get("reason") or ""
        lines.append(f"  - not assessed: {area}: {reason}")
    lines.append("truth_assessed: false")
    return "\n".join(lines) + "\n"


def format_refusal(report: Mapping[str, Any]) -> str:
    lines = [f"review refused: {report.get('reason')}"]
    range_info = report.get("range") or {}
    if range_info:
        lines.append(f"range: {range_info.get('spec')}")
    worktree = report.get("worktree") or {}
    if worktree:
        lines.append(f"worktree: {worktree.get('path')} "
                     f"({worktree.get('branch') or 'detached'})")
    for check in (report.get("preflight") or {}).get("checks") or []:
        if check.get("level") == "error":
            lines.append(f"  error {check.get('id')}: {check.get('message')}")
    return "\n".join(lines) + "\n"


__all__ = [
    "CodeReviewError", "DEFAULT_MAX_FILE_BYTES", "FAIL_ON", "FINDING_TYPES",
    "MAX_REVIEW_DETAIL", "MAX_REVIEWER_ERROR_CHARS", "MAX_TEST_OUTPUT_BYTES",
    "MAX_TEST_OUTPUT_LINES", "OcelotClusterer", "PACKET_SCHEMA",
    "PreflightError", "ReviewRunner", "SCHEMA", "SEVERITIES", "TestRunner",
    "atomic_write", "exit_code", "format_packet", "format_refusal",
    "packet_json", "parse_range", "preflight", "review_diff", "review_range",
    "run_injected_review", "run_injected_reviews", "run_injected_tests",
]
