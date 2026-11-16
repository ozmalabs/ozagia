"""Deterministic review of a whole repository (``ozagia review-repo``).

Where ``core.code_review`` reviews one commit range, this module reviews the
checkout as it is: architecture, tests, code health, churn and ownership,
docs, security hygiene, operations sources and dependency manifests. It reads
the tracked files, ``git log``, the recorded graph (``.ozagia/graph.json``),
the test record and the edit journal. It never calls a model, never fetches,
and never writes to the repository. ``save_packet`` writes a packet under the
state directory only when a caller asks for it.

The graph is used only when it is fresh. A missing or stale graph is reported
in ``packet["graph"]`` and the checks that need it are listed in
``not_checked`` instead of running on outdated edges; ``index=True`` rebuilds
the graph first (the only write, and only to the state directory).

Every finding has a stable id: a hash of its type and its graph keys
(``file:``, ``sym:``, ``test:``, ``ops:`` keys, see ``core.keys``; a package
cycle hashes its package names). Two packets
can therefore be compared (``compare_packets``): ids only in the new packet
are new, ids only in the old one are resolved when the check covered the
same population in both. A finding is evidence for a
human or an agent to check; ``truth_assessed`` is always false.

Each finding carries a ``suggested_goal`` with machine-checkable criteria in
the kinds the goal system understands (``CRITERIA_KINDS``), so a finding can be
turned into a goal whose completion is checked against the graph. The goal
system adds its own ``review_resolved`` criterion when it converts a finding;
it asks ``finding_state`` whether the finding is still present in a later
packet. Each packet records who wrote it (``generator``), when
(``generated_at``, microseconds) and the working tree it saw (``tree``, a
``core.fingerprint`` taken before any file is read), so a goal can require a
later review of the same files.

Optional model review is a seam (``run_model_pass``): the caller injects the
runner, and model output is kept as separate, attributed findings.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import tomllib
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.core import code_review as CR
from ozagia.core import resources

SCHEMA = 1
PACKET_SCHEMA = "ozagia.repo_review.packet.v1"
COMPACT_SCHEMA = "ozagia.repo_review.compact.v1"
# Written into every packet `review_repo` builds; the goal system resolves
# a `review_resolved` criterion only from packets that carry it.
GENERATOR = "ozagia.repo_review"

SECTIONS = ("architecture", "tests", "health", "churn", "docs", "security",
            "ops", "dependencies")
SEVERITIES = CR.SEVERITIES
SEVERITY_RANK = CR.SEVERITY_RANK
FAIL_ON = CR.FAIL_ON

# Criteria kinds of the goal system (docs/plans/2026-11-16_repo_review_and_
# goals.md, W18). Suggested goals use only these.
CRITERIA_KINDS = (
    "test_passes", "test_fails", "symbol_exists", "symbol_absent", "calls",
    "not_calls", "no_callers", "file_exists", "file_absent", "unchanged",
    "lint_clean", "review_resolved", "ops_has",
)

# Finding type -> section.
FINDING_TYPES = {
    "import_cycle": "architecture",
    "package_cycle": "architecture",
    "layer_violation": "architecture",
    "hub_module": "architecture",
    "failing_test": "tests",
    "untested_module": "tests",
    "untested_symbol": "tests",
    "large_file": "health",
    "large_function": "health",
    "todo_density": "health",
    "unreferenced_symbol": "health",
    "churn_untested": "churn",
    "many_sessions": "churn",
    "stale_doc_reference": "docs",
    "committed_env_file": "security",
    "sensitive_file": "security",
    "dangerous_construct": "security",
    "unpinned_image": "ops",
    "no_restart_policy": "ops",
    "no_healthcheck": "ops",
    "published_port": "ops",
    "missing_lock_file": "dependencies",
}

# Finding type -> the check that produces it. `--compare` compares a type
# only when its check ran to completion in both packets.
FINDING_CHECKS = {
    "import_cycle": "import_cycles",
    "package_cycle": "package_cycles",
    "layer_violation": "layers",
    "hub_module": "hubs",
    "failing_test": "failing_tests",
    "untested_module": "untested_modules",
    "untested_symbol": "untested_symbols",
    "large_file": "large_files",
    "large_function": "large_functions",
    "todo_density": "todo_density",
    "unreferenced_symbol": "unreferenced_symbols",
    "churn_untested": "hotspots",
    "many_sessions": "sessions",
    "stale_doc_reference": "links_and_code_spans",
    "committed_env_file": "sensitive_names",
    "sensitive_file": "sensitive_names",
    "dangerous_construct": "dangerous_constructs",
    "unpinned_image": "image_pinning",
    "no_restart_policy": "restart_and_healthcheck",
    "no_healthcheck": "restart_and_healthcheck",
    "published_port": "published_ports",
    "missing_lock_file": "lock_files",
}

DEFAULT_BUDGET_S = 60.0
DEFAULT_MEMORY_MB = 512.0
DEFAULT_DISK_MB = 64.0
MAX_FINDINGS_PER_SECTION = 50
MAX_EVIDENCE = 10
MAX_KEYS = 20
MAX_DATA_ITEMS = 50
MAX_READ_BYTES = 1_000_000
MAX_CACHE_BYTES = 64 * 1024 * 1024
GIT_LOG_TIMEOUT = 120

HUB_TOP = 10
HUB_MIN_IN_DEGREE = 10
LARGE_FILE_LINES = 1000
LARGE_FUNCTION_LINES = 100
TODO_MIN_COUNT = 5
CHURN_DAYS = 90
CHURN_MIN_COMMITS = 3
CHURN_TOP = 10
MANY_SESSIONS = 3

REVIEWS_DIR = "reviews"
LATEST_NAME = "latest.json"
PACKET_PREFIX = "repo_review-"
MAX_SAVED_PACKETS = 20
COMPACT_NAME = "compact.json"
COMPACT_MAX_FINDINGS = 60

FUNCTION_KINDS = frozenset({"function", "async_function", "method", "fn"})
# Files whose lack of tests or references is expected (entry points and
# package markers). Mirrors core.architecture's dead-code exclusions.
ENTRYPOINT_NAMES = frozenset({
    "__init__.py", "__main__.py", "conftest.py", "setup.py", "manage.py",
    "wsgi.py", "asgi.py", "noxfile.py", "fabfile.py",
})
ENTRYPOINT_PREFIXES = ("scripts/", "tools/", "bin/")
FIXTURE_PARTS = frozenset({"fixture", "fixtures", "testdata", "example",
                           "examples", "samples"})
# Method names called by frameworks rather than by name in the code.
DYNAMIC_NAME = re.compile(
    r"^(?:__\w+__|test\w*|visit_\w+|do_\w+|on_\w+|handle_\w*|setUp\w*|"
    r"tearDown\w*|main)$")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TODO_RE = re.compile(r"\b(?:TODO|FIXME|XXX)\b")

# Doc files that record history (plans, attempts, changelogs) name paths
# that existed then or were planned; they are not checked for stale refs.
DOC_HISTORY_PARTS = frozenset({"plans", "attempts", "history", "archive",
                               "changelog", "changelogs", "adr", "adrs",
                               "results"})
DOC_HISTORY_NAMES = frozenset({"changelog.md", "history.md", "changes.md",
                               "news.md", "releases.md"})
DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst"})
_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_SPAN_RE = re.compile(r"`([^`\n]{2,200})`")
_PATH_SPAN = re.compile(r"[A-Za-z0-9_.\-/]+(?:::[A-Za-z_][\w.]*)?(?::\d+)?")
_DOTTED_SPAN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\(\))?")
_KNOWN_SUFFIXES = (CR.SOURCE_SUFFIXES | CR.CONFIG_SUFFIXES | DOC_SUFFIXES
                   | frozenset({".txt", ".lock", ".cfg", ".service",
                                ".container", ".sh"}))

ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist",
                         ".defaults", ".tmpl", ".in")
# A dotted part of an env file name that marks it as a template
# (`example.env`, `prod.env.sample`).
ENV_TEMPLATE_WORDS = frozenset(s.lstrip(".") for s in ENV_TEMPLATE_SUFFIXES)
SECRET_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".pypirc", ".npmrc",
    ".netrc", ".htpasswd", "credentials.json", "secrets.yaml", "secrets.yml",
    "secrets.json", "service-account.json",
})
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
                   ".kdbx", ".ppk")
# Word-bounded forms of code_review.DANGEROUS_TOKENS, so `ast.literal_eval(`
# is not read as `eval(`.
DANGEROUS_PATTERNS = tuple(
    (re.compile((r"(?<![\w.])" if token[0].isalpha() else "")
                + re.escape(token)), label)
    for token, label in CR.DANGEROUS_TOKENS)

# `yaml.load(..., Loader=X)` is flagged only when X is a loader that builds
# arbitrary objects; a custom or safe loader is an explicit choice.
_YAML_LOAD = "unsafe YAML load"
_LOADER_ARG = re.compile(r"\bLoader\s*=\s*([\w.]+)")
UNSAFE_YAML_LOADERS = frozenset({"yaml.Loader", "yaml.UnsafeLoader",
                                 "yaml.FullLoader", "UnsafeLoader",
                                 "FullLoader"})

LOCK_FILES = {
    "python": ("uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock",
               "pylock.toml", "requirements.lock"),
    "node": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
             "pnpm-lock.yaml", "bun.lock", "bun.lockb"),
    "rust": ("Cargo.lock",),
    "go": ("go.sum",),
}

SECURITY_NOTE = (
    "Security hygiene only: file names and token matches. This is not a "
    "security audit; no secret scanning of contents, dependency advisories "
    "or data-flow analysis ran.")


class RepoReviewError(CR.CodeReviewError):
    """The review could not run: a bad root, git failure or bad input."""


class PreflightError(RepoReviewError):
    """Preflight refused the run. ``report`` is a JSON-safe refusal."""

    def __init__(self, message: str, report: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.report = dict(report)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def finding_id(ftype: str, keys: Iterable[str]) -> str:
    """The stable id of a finding: a hash of its type and sorted keys."""
    text = json.dumps([str(ftype), sorted({str(k) for k in keys})],
                      separators=(",", ":"))
    return "RR-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _file_key(rel: str) -> str:
    return f"file:{rel}"


def _key_file(key: str) -> str | None:
    if key.startswith("ops:"):
        return None
    return CR._file_from_key(key)


def _iso(ts: Any) -> str | None:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _utc_now() -> str:
    """Now as ISO 8601 UTC with microseconds, so two reviews in the same
    second still order (``generated_at``)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_generated_at(value: Any) -> float | None:
    """A packet's ``generated_at`` (with or without fractional seconds) as
    a POSIX time, or None when it is missing or malformed."""
    from datetime import datetime, timezone
    if not isinstance(value, str) or len(value) > 40:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _is_test(rel: str) -> bool:
    return CR._classify_path(rel) == "test"


def _is_source(rel: str) -> bool:
    return CR._classify_path(rel) == "source"


def _entrypoint(rel: str) -> bool:
    """Entry points, package markers and fixture or example trees: files
    whose lack of tests or references is expected."""
    path = PurePosixPath(rel)
    if path.name in ENTRYPOINT_NAMES or rel.startswith(ENTRYPOINT_PREFIXES):
        return True
    return any(part.casefold() in FIXTURE_PARTS for part in path.parts[:-1])


def _fixture(rel: str) -> bool:
    """A file in a fixture, test-data or example tree."""
    return any(part.casefold() in FIXTURE_PARTS
               for part in PurePosixPath(rel).parts[:-1])


_OPS_FILE_KINDS = ("compose", "containerfile", "quadlet", "unit")


def _key_files(keys: Iterable[str]) -> list[str]:
    """The files that graph keys live in, including the source file of an
    ``ops:<kind>/<rel>[#...]`` key."""
    out: list[str] = []
    for key in keys:
        key = str(key)
        if key.startswith("ops:"):
            kind, _sep, rest = key[len("ops:"):].partition("/")
            rel = rest.split("#", 1)[0]
            path = rel if kind in _OPS_FILE_KINDS and rel else None
        else:
            path = CR._file_from_key(key)
        if path and path not in out:
            out.append(path)
    return out


def _excluded_note(files: Sequence[str]) -> str:
    shown = ", ".join(files[:10]) + (" ..." if len(files) > 10 else "")
    return (f"{len(files)} file(s) in fixture or example trees were not "
            f"checked: {shown}")


_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n#;]+)"
    r"|import[ \t]+([^\n#;]+))", re.M)


def _imported_modules(text: str) -> set[str]:
    """Dotted names a Python file imports: `import a.b` gives a.b, and
    `from a import b` gives a and a.b (b may be a module)."""
    out: set[str] = set()
    for match in _IMPORT_RE.finditer(text):
        base, names, plain = match.groups()
        if plain is not None:
            for item in plain.split(","):
                word = item.strip().split(" ", 1)[0]
                if word and not word.startswith("."):
                    out.add(word)
            continue
        if not base or base.startswith("."):
            continue
        out.add(base)
        names = re.sub(r"#[^\n]*", "", names)
        for item in names.strip("()").replace("\n", " ").split(","):
            word = item.strip().split(" ", 1)[0]
            if IDENT.fullmatch(word):
                out.add(f"{base}.{word}")
    return out


def _git_text(root: Path, args: Sequence[str], *,
              timeout: int = CR.GIT_TIMEOUT) -> str:
    try:
        return CR._decode(CR._git_required(root, args, timeout=timeout))
    except CR.CodeReviewError as exc:
        raise RepoReviewError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #


def _resource_report(root: Path, *, memory_mb: float,
                     disk_mb: float) -> dict[str, Any]:
    expected = resources.estimate(root, "repo_review", default_mb=memory_mb,
                                  default_disk_mb=disk_mb)
    want_memory = expected.get("memory_mb")
    want_disk = expected.get("disk_mb")
    if not isinstance(want_memory, (int, float)):
        want_memory = memory_mb
    if not isinstance(want_disk, (int, float)):
        want_disk = disk_mb
    admission = resources.check_admission(
        root, kind="task", memory_mb=float(want_memory),
        disk_mb=float(want_disk), key="repo_review")
    keep = ("ok", "disabled", "reason", "requested", "available",
            "shortfall", "retry_after_s", "checks", "suggestions")
    report = {name: admission.get(name) for name in keep if name in admission}
    report["estimate"] = expected
    report.setdefault("requested", {"memory_mb": want_memory,
                                    "disk_mb": want_disk})
    report["note"] = ("admission for the review's own reads; no test, build "
                      "or linter is run")
    return report


def preflight(root: str | Path, *, memory_mb: float = DEFAULT_MEMORY_MB,
              disk_mb: float = DEFAULT_DISK_MB) -> dict[str, Any]:
    """Check the root, HEAD, conflicts and resource admission."""
    try:
        path = CR._repository_root(root)
    except CR.CodeReviewError as exc:
        raise RepoReviewError(str(exc)) from exc
    if memory_mb < 0 or disk_mb < 0:
        raise RepoReviewError("memory_mb and disk_mb must be >= 0")
    checks: list[dict[str, Any]] = []
    head_proc = CR._git(path, ["rev-parse", "--verify", "--quiet",
                               "HEAD^{commit}"])
    head = CR._decode(head_proc.stdout).strip() if head_proc.returncode == 0 else ""
    if head:
        CR._check(checks, "ok", "git.head", f"HEAD is {head[:12]}",
                  command="git rev-parse --verify HEAD")
    else:
        CR._check(checks, "error", "git.head",
                  "HEAD does not name a commit; commit something first",
                  command="git rev-parse --verify HEAD")
    branch_proc = CR._git(path, ["symbolic-ref", "--short", "-q", "HEAD"])
    branch = (CR._decode(branch_proc.stdout).strip() or None
              if branch_proc.returncode == 0 else None)
    unmerged = CR._unmerged_paths(path)
    if unmerged:
        CR._check(checks, "warn", "worktree.conflicts",
                  f"{len(unmerged)} unresolved conflict path(s); they are "
                  "reviewed as they are on disk",
                  command="git ls-files -u -z", paths=unmerged[:20])
    else:
        CR._check(checks, "ok", "worktree.conflicts", "no unresolved conflicts",
                  command="git ls-files -u -z")
    report = _resource_report(path, memory_mb=memory_mb, disk_mb=disk_mb)
    if report.get("ok"):
        CR._check(checks, "ok", "resources.admission",
                  "the review's footprint fits the host reserves",
                  requested=report.get("requested"))
    else:
        CR._check(checks, "error", "resources.admission",
                  f"resource admission refused ({report.get('reason')})",
                  shortfall=report.get("shortfall"),
                  suggestions=report.get("suggestions"))
    errors = [item for item in checks if item["level"] == "error"]
    return {
        "schema": SCHEMA,
        "ok": not errors,
        "reason": errors[0]["id"] if errors else None,
        "root": str(path),
        "head": head or None,
        "branch": branch,
        "checks": checks,
        "resources": report,
    }


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #


def _graph_file(root: Path, graph_path: str | Path | None) -> Path:
    if graph_path is None:
        from ozagia.config import state_path
        return state_path(root, "graph.json")
    path = Path(graph_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _load_graph(root: Path, graph_file: Path,
                head: str) -> tuple[dict[str, Any], Any, dict[str, Any] | None]:
    """(info, DevMemory or None, index or None). The index is None unless
    the snapshot is fresh for this checkout."""
    info: dict[str, Any] = {
        "path": str(graph_file), "available": False, "freshness": "missing",
        "usable": False, "snapshot_head": None, "snapshot_root": None,
        "generated_at": None, "nodes": 0, "edges": 0, "reason": None,
    }
    if not graph_file.is_file():
        info["reason"] = (f"no graph snapshot at {graph_file}; run "
                          "`ozagia index` or pass --index")
        return info, None, None
    try:
        from ozagia.core.memory import DevMemory
        dev = DevMemory(dump=graph_file, root=root)
        staleness = dev.staleness()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        info.update(available=True, freshness="error",
                    reason=f"cannot read graph snapshot: {exc}")
        return info, None, None
    repo_attrs: dict[str, Any] = {}
    for node in dev.nodes.values():
        if node.get("type") == "Repo" and isinstance(node.get("attrs"), dict):
            repo_attrs = dict(node["attrs"])
            break
    info.update({
        "available": True,
        "snapshot_head": repo_attrs.get("head"),
        "snapshot_root": repo_attrs.get("root"),
        "generated_at": repo_attrs.get("generated_at"),
        "nodes": len(dev.nodes),
        "edges": len(dev.edges),
    })
    reasons: list[str] = []
    if staleness.get("root_mismatch"):
        reasons.append(f"snapshot root {repo_attrs.get('root')} is not {root}")
    if repo_attrs.get("head") and repo_attrs.get("head") != head:
        reasons.append(f"snapshot head {str(repo_attrs['head'])[:12]} is not "
                       f"HEAD {head[:12]}")
    if staleness.get("tree_changed"):
        reasons.append("working tree changed since the snapshot")
    freshness = str(staleness.get("freshness") or "unknown")
    if reasons:
        freshness = "stale"
    info["freshness"] = freshness
    if freshness == "fresh":
        info["reason"] = "snapshot head and working-tree digest match"
    elif reasons:
        info["reason"] = "; ".join(reasons)
    else:
        errors = staleness.get("verification_errors") or []
        info["reason"] = ("; ".join(str(e) for e in errors)
                          or "graph freshness could not be verified")
    info["usable"] = freshness == "fresh"
    if not info["usable"]:
        info["reason"] += ("; graph checks were skipped (`--index` rebuilds "
                           "the graph first)")
        return info, dev, None
    return info, dev, CR._graph_index(dev)


def _default_indexer(root: Path, graph_file: Path) -> dict[str, Any]:
    """Build or refresh the snapshot the way `ozagia index` does."""
    import sys
    from ozagia.config import config_path
    from ozagia.harness.session import prepare_snapshot, tool_env
    env = tool_env(root, tool_python=sys.executable, config=config_path(root))
    report = prepare_snapshot(root, graph_file, build="auto", env=env)
    if report.get("still_stale"):
        report = prepare_snapshot(root, graph_file, build="always", env=env)
    return report


# --------------------------------------------------------------------------- #
# context and section collector
# --------------------------------------------------------------------------- #


def read_text(root: Path, rel: str, *, real_root: Path | None = None
              ) -> tuple[str | None, str | None]:
    """A tracked file's text and None, or None and why it was not read:
    outside the root, a symlink (never followed), resolving outside the
    root, missing, over ``MAX_READ_BYTES`` or binary."""
    real_root = real_root or root.resolve()
    path = root / rel
    try:
        if not _inside(rel):
            return None, "outside the root"
        if path.is_symlink():
            return None, "symlink"
        if not path.resolve().is_relative_to(real_root):
            return None, "resolves outside the root"
        if not path.is_file():
            return None, "missing"
        if path.stat().st_size > MAX_READ_BYTES:
            return None, "over the read limit"
        data = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable: {type(exc).__name__}"
    if b"\0" in data[:8192]:
        return None, "binary"
    return data.decode("utf-8", errors="replace"), None


class _Context:
    """What every section reads: tracked files, file text, graph, clock."""

    def __init__(self, root: Path, head: str, files: list[str],
                 graph_info: dict[str, Any], dev: Any,
                 index: dict[str, Any] | None, *, layers: Sequence[str],
                 deadline: float, clock: Callable[[], float],
                 churn_days: int, cap: int) -> None:
        self.root = root
        self.head = head
        self.files = files
        self.tracked = set(files)
        self.graph_info = graph_info
        self.dev = dev
        self.index = index
        self.layers = tuple(layers)
        self.deadline = deadline
        self.clock = clock
        self.churn_days = churn_days
        self.cap = cap
        self._text: dict[str, str | None] = {}
        self._cached_bytes = 0
        self.real_root = root.resolve()
        # rel -> why the file could not be read.
        self.unread: dict[str, str] = {}
        self._modules: dict[str, str] | None = None
        self._imports: dict[str, set[str]] | None = None
        self._names_by_file: dict[str, set[str]] | None = None
        self.tests = sorted(rel for rel in files if _is_test(rel))
        self.tracked_dirs = {parent.as_posix() for rel in files
                             for parent in PurePosixPath(rel).parents
                             if parent.as_posix() != "."}
        self.graph_reason = (
            f"graph is {graph_info.get('freshness')}: {graph_info.get('reason')}")

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def text(self, rel: str) -> str | None:
        """The file's text, or None when it is missing, binary, too big, a
        symlink or resolves outside the root (the reason is kept in
        ``unread``)."""
        if rel in self._text:
            return self._text[rel]
        value, reason = read_text(self.root, rel, real_root=self.real_root)
        if value is None:
            self.unread[rel] = reason or "unreadable"
        if value is not None and self._cached_bytes + len(value) <= MAX_CACHE_BYTES:
            self._text[rel] = value
            self._cached_bytes += len(value)
        elif value is None:
            self._text[rel] = None
        return value

    def module_names(self) -> dict[str, str]:
        """Dotted module name -> file, for tracked Python sources."""
        if self._modules is None:
            self._modules = _module_index(self)
        return self._modules

    def _test_imports(self) -> dict[str, set[str]]:
        """Test file -> the dotted module names it imports (Python only)."""
        if self._imports is None:
            self._imports = {}
            for rel in self.tests:
                if not rel.endswith(".py"):
                    continue
                self._imports[rel] = _imported_modules(self.text(rel) or "")
        return self._imports

    def importing_tests(self, rel: str) -> list[str]:
        """Test files that import the module `rel` by its dotted name."""
        if self._names_by_file is None:
            self._names_by_file = defaultdict(set)
            for name, path in self.module_names().items():
                self._names_by_file[path].add(name)
        names = self._names_by_file.get(rel)
        if not names:
            return []
        return sorted(test for test, imported in self._test_imports().items()
                      if imported & names)

    # graph views --------------------------------------------------------- #

    def graph_nid(self, rel: str) -> int | None:
        if self.index is None:
            return None
        return (self.index.get("files_by_path") or {}).get(rel)

    def symbols(self, rel: str) -> list[dict[str, Any]]:
        """Symbol attrs (name, qualname, kind, lines, ...) plus `key`."""
        if self.index is None:
            return []
        out = []
        for nid in (self.index.get("symbols_by_file") or {}).get(rel, []):
            entry = self.index["nodes"].get(nid) or {}
            out.append({**(entry.get("attrs") or {}), "key": entry.get("key")})
        return out

    def tests_for(self, rel: str) -> tuple[list[dict[str, Any]], str]:
        """Covering tests and the mode of the lookup: ``graph`` when the
        file is in a fresh graph (test edges, then test file names and test
        imports), else ``filename`` (test file names and imports only).
        Each test says how it was found (``via``)."""
        out: dict[str, dict[str, Any]] = {}
        mode = "filename"
        if self.index is not None and self.graph_nid(rel) is not None:
            mode = "graph"
            for nid in (self.index.get("tests_by_file") or {}).get(rel, []):
                entry = self.index["nodes"].get(nid) or {}
                attrs = entry.get("attrs") or {}
                name = str(attrs.get("file") or entry.get("key"))
                out.setdefault(name, {"key": entry.get("key"),
                                      "file": attrs.get("file"),
                                      "via": "graph"})
        for path in CR._fallback_tests(rel, self.tests):
            out.setdefault(path, {"key": None, "file": path, "via": "filename"})
        if rel.endswith(".py"):
            for path in self.importing_tests(rel):
                out.setdefault(path, {"key": None, "file": path, "via": "import"})
        return [out[name] for name in sorted(out)], mode

    def source_modules(self) -> list[str]:
        """Tracked source modules a test is expected for."""
        if self.index is not None:
            out = []
            for rel in sorted((self.index.get("files_by_path") or {})):
                if (rel in self.tracked and _is_source(rel)
                        and not _entrypoint(rel) and self.symbols(rel)):
                    out.append(rel)
            return out
        return [rel for rel in self.files
                if rel.endswith(".py") and _is_source(rel)
                and not _entrypoint(rel)
                and not CR._is_generated(rel, ())]


def _fair_cap(ordered: Sequence[dict[str, Any]],
              cap: int) -> list[dict[str, Any]]:
    """At most `cap` findings, so one plentiful type cannot crowd out the
    others: each type first gets an equal share, in severity order, then the
    rest of the room is filled in the same order."""
    if len(ordered) <= cap:
        return list(ordered)
    types = sorted({f["type"] for f in ordered})
    quota = max(1, cap // len(types))
    taken: Counter[str] = Counter()
    kept: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for finding in ordered:
        if len(kept) < cap and taken[finding["type"]] < quota:
            taken[finding["type"]] += 1
            kept.append(finding)
        else:
            rest.append(finding)
    kept.extend(rest[:cap - len(kept)])
    order = {f["id"]: n for n, f in enumerate(ordered)}
    return sorted(kept, key=lambda f: order[f["id"]])


class _Section:
    def __init__(self, name: str, ctx: _Context) -> None:
        self.name = name
        self.ctx = ctx
        self.found: dict[str, dict[str, Any]] = {}
        # id -> the files of all its keys (also of keys cut by MAX_KEYS).
        self.files_of: dict[str, list[str]] = {}
        self.checked: list[str] = []
        self.not_checked: list[dict[str, str]] = []
        self.data: dict[str, Any] = {}
        self.modes: dict[str, str] = {}
        # check -> {file: reason} for files the check could not read or
        # parse; their findings are not reported as resolved by --compare.
        self.skipped_files: dict[str, dict[str, str]] = {}
        self.truncated: str | None = None
        self.started = ctx.clock()

    def ran(self, check: str) -> None:
        if check not in self.checked:
            self.checked.append(check)

    def skip(self, check: str, reason: str) -> None:
        self.not_checked.append({"section": self.name, "check": check,
                                 "reason": reason})

    def mode(self, check: str, mode: str) -> None:
        """How a check found its population (`graph`, `filename`)."""
        self.modes[check] = mode

    def skip_file(self, checks: str | Iterable[str], rel: str,
                  reason: str) -> None:
        for check in [checks] if isinstance(checks, str) else checks:
            self.skipped_files.setdefault(check, {})[rel] = reason

    def read(self, rel: str, *checks: str) -> str | None:
        """The file's text; an unreadable file is recorded as skipped for
        `checks`."""
        text = self.ctx.text(rel)
        if text is None and checks:
            self.skip_file(checks, rel, self.ctx.unread.get(rel, "unreadable"))
        return text

    def expired(self) -> bool:
        if self.ctx.expired():
            self.truncated = "budget"
            return True
        return False

    def add(self, ftype: str, severity: str, keys: Iterable[str],
            message: str, *, evidence: Iterable[str] = (),
            confidence: str = "medium", note: str = "",
            criteria: Iterable[Mapping[str, Any]] = (),
            title: str | None = None,
            id_keys: Iterable[str] | None = None) -> dict[str, Any]:
        """Record a finding. The id hashes the type and ``id_keys`` (default:
        the keys, capped at MAX_KEYS); the capped keys are the targets."""
        if FINDING_TYPES.get(ftype) != self.name:
            raise ValueError(f"finding type {ftype!r} is not in section "
                             f"{self.name!r}")
        if severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity {severity!r}")
        all_keys = sorted({str(k) for k in keys if k})
        key_list = all_keys[:MAX_KEYS]
        fid = finding_id(ftype, key_list if id_keys is None else id_keys)
        evidence = [str(item) for item in evidence]
        existing = self.found.get(fid)
        if existing is not None:
            for item in evidence:
                if (item not in existing["evidence"]
                        and len(existing["evidence"]) < MAX_EVIDENCE):
                    existing["evidence"].append(item)
            existing["occurrences"] += 1
            return existing
        goal_criteria = [dict(item) for item in criteria]
        for item in goal_criteria:
            if item.get("kind") not in CRITERIA_KINDS:
                raise ValueError(f"unknown criterion kind {item.get('kind')!r}")
            if item.get("kind") == "review_resolved":
                raise ValueError("review_resolved is added by the goal system")
        files = _key_files(key_list)
        finding = {
            "id": fid,
            "section": self.name,
            "check": FINDING_CHECKS[ftype],
            "type": ftype,
            "severity": severity,
            "message": message,
            "keys": key_list,
            "file": files[0] if files else None,
            "evidence": list(dict.fromkeys(evidence))[:MAX_EVIDENCE],
            "confidence": confidence,
            "confidence_note": note,
            "occurrences": 1,
            "source": "deterministic",
            "suggested_goal": {
                "title": title or message,
                "targets": key_list,
                "criteria": goal_criteria,
            },
        }
        self.found[fid] = finding
        self.files_of[fid] = _key_files(all_keys)
        return finding

    def finish(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        ordered = sorted(
            self.found.values(),
            key=lambda f: (SEVERITY_RANK[f["severity"]], f["type"],
                           f["keys"], f["id"]))
        kept = _fair_cap(ordered, self.ctx.cap)
        kept_ids = {f["id"] for f in kept}
        dropped = [f for f in ordered if f["id"] not in kept_ids]
        if dropped and self.truncated is None:
            self.truncated = "cap"
        status = "truncated" if self.truncated else "ok"
        if self.truncated == "budget" and not self.checked:
            status = "skipped"
        section = {
            "status": status,
            "checked": list(self.checked),
            "not_checked": list(self.not_checked),
            "truncated": bool(self.truncated),
            "truncated_reason": self.truncated,
            "findings": len(kept),
            "found": len(ordered),
            "dropped": dict(sorted(Counter(f["type"] for f in dropped).items())),
            # Every id the section found, kept or dropped, for --compare.
            "ids": sorted(f["id"] for f in ordered),
            "ids_by_type": {
                ftype: sorted(f["id"] for f in ordered if f["type"] == ftype)
                for ftype in sorted({f["type"] for f in ordered})},
            # The files each id is about, so --compare can hold back ids
            # whose files a later run could not read.
            "id_files": {fid: self.files_of.get(fid, [])
                         for fid in sorted(f["id"] for f in ordered)},
            "modes": dict(sorted(self.modes.items())),
            "skipped_files": {check: dict(sorted(files.items()))
                              for check, files in sorted(
                                  self.skipped_files.items())},
            "data": self.data,
            "elapsed_s": round(max(0.0, self.ctx.clock() - self.started), 3),
        }
        return section, kept


# --------------------------------------------------------------------------- #
# architecture
# --------------------------------------------------------------------------- #


def _common_dir(paths: Sequence[str]) -> str:
    parts = [PurePosixPath(p).parts[:-1] for p in paths]
    if not parts:
        return ""
    common: list[str] = []
    for group in zip(*parts):
        if len(set(group)) != 1:
            break
        common.append(group[0])
    return "/".join(common)


def _package(rel: str, common: str) -> str | None:
    """The top-level package of a module: the first directory below the
    directory all modules share. A module directly in that directory is in
    no package (its cycles are import cycles)."""
    rest = rel[len(common) + 1:] if common else rel
    head, sep, _tail = rest.partition("/")
    if not sep:
        return None
    return f"{common}/{head}" if common else head


def _layer(rel: str, layers: Sequence[str]) -> int | None:
    best: tuple[int, int] | None = None
    for number, prefix in enumerate(layers):
        if prefix in (".", "") or rel == prefix or rel.startswith(prefix + "/"):
            length = 0 if prefix in (".", "") else len(prefix)
            if best is None or length > best[1]:
                best = (number, length)
    return best[0] if best else None


def _architecture(sec: _Section) -> None:
    from ozagia.core.architecture import _tarjan_sccs
    from ozagia.core.repo_map import personalized_pagerank
    ctx = sec.ctx
    checks = ("import_cycles", "package_cycles", "hubs", "layers")
    if ctx.index is None:
        for check in checks:
            sec.skip(check, ctx.graph_reason)
        return
    index = ctx.index
    files = {rel: nid for rel, nid in (index.get("files_by_path") or {}).items()
             if rel in ctx.tracked and _is_source(rel)}
    by_nid = {nid: rel for rel, nid in files.items()}
    pairs: set[tuple[str, str]] = set()
    for rel, nid in files.items():
        for tgt, etype in (index.get("outgoing") or {}).get(nid, []):
            other = by_nid.get(tgt)
            if etype == "imports" and other and other != rel:
                pairs.add((rel, other))
    edges = [(src, tgt, 1.0) for src, tgt in sorted(pairs)]
    names = sorted(files)
    sec.data.update({"modules": len(names), "import_edges": len(edges)})

    for comp in _tarjan_sccs(names, edges):
        members = set(comp)
        inside = [f"{s} imports {t}" for s, t, _ in edges
                  if s in members and t in members]
        sec.add("import_cycle", "minor", [_file_key(r) for r in comp],
                f"{len(comp)} modules import each other in a cycle: "
                + ", ".join(comp[:5]) + (" ..." if len(comp) > 5 else ""),
                evidence=inside, confidence="high",
                note="from imports edges in a fresh graph; imports inside "
                     "functions are included",
                title=f"Break the import cycle between {comp[0]} and "
                      f"{len(comp) - 1} other module(s)")
    sec.ran("import_cycles")
    if sec.expired():
        return

    groups: dict[str, list[str]] = defaultdict(list)
    for rel in names:
        groups[PurePosixPath(rel).parts[0] if "/" in rel else ""].append(rel)
    commons = {group: _common_dir(members) for group, members in groups.items()}
    pkg_of = {rel: _package(rel, commons[group])
              for group, members in groups.items() for rel in members}
    pkg_pairs: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for src, tgt, _ in edges:
        a, b = pkg_of[src], pkg_of[tgt]
        if a is not None and b is not None and a != b:
            pkg_pairs[(a, b)].append((src, tgt))
    packages = sorted({p for p in pkg_of.values() if p is not None})
    pkg_edges = [(a, b, 1.0) for a, b in sorted(pkg_pairs)]
    sec.data["packages"] = len(packages)
    sec.data["package_roots"] = sorted(c or "." for c in commons.values())
    for comp in _tarjan_sccs(packages, pkg_edges):
        members = set(comp)
        via = [(pair, link) for pair, links in sorted(pkg_pairs.items())
               if pair[0] in members and pair[1] in members for link in links]
        keys = sorted({_file_key(f) for _pair, link in via for f in link})
        # The id follows the packages, not the import links between them.
        sec.add("package_cycle", "major", keys,
                f"packages {', '.join(comp)} depend on each other",
                id_keys=[f"package:{name}" for name in comp],
                evidence=[f"{a} -> {b}: {s} imports {t}"
                          for (a, b), (s, t) in via],
                confidence="high",
                note="package = first directory below the directory the "
                     "modules of its top-level tree share; modules directly "
                     "in that directory belong to no package",
                title=f"Remove the dependency cycle between packages "
                      f"{', '.join(comp)}")
    sec.ran("package_cycles")
    if sec.expired():
        return

    in_deg: Counter[str] = Counter(t for _s, t, _ in edges)
    out_deg: Counter[str] = Counter(s for s, _t, _ in edges)
    ranks = personalized_pagerank(
        {"edges": [{"src": s, "tgt": t, "weight": w} for s, t, w in edges]})
    hubs = []
    for rel in sorted(ranks, key=lambda r: (-ranks[r], r))[:HUB_TOP]:
        hubs.append({"file": rel, "key": _file_key(rel),
                     "in_degree": in_deg[rel], "out_degree": out_deg[rel],
                     "pagerank": round(ranks[rel], 6)})
        if in_deg[rel] >= HUB_MIN_IN_DEGREE:
            sec.add("hub_module", "note", [_file_key(rel)],
                    f"{rel} is imported by {in_deg[rel]} modules",
                    evidence=[f"in_degree={in_deg[rel]}",
                              f"out_degree={out_deg[rel]}",
                              f"pagerank={round(ranks[rel], 6)}"],
                    confidence="high",
                    note="a hub is a review focus, not a defect",
                    title=f"Review the interface of hub module {rel}")
    sec.data["hubs"] = hubs
    sec.ran("hubs")

    if not ctx.layers:
        sec.skip("layers", "no [review] layers are configured")
        return
    sec.data["layers"] = list(ctx.layers)
    for src, tgt, _ in edges:
        a, b = _layer(src, ctx.layers), _layer(tgt, ctx.layers)
        if a is not None and b is not None and a < b:
            sec.add("layer_violation", "major", [_file_key(src), _file_key(tgt)],
                    f"{src} (layer {ctx.layers[a]}) imports {tgt} "
                    f"(higher layer {ctx.layers[b]})",
                    evidence=[f"{src} imports {tgt}",
                              f"layers: {' < '.join(ctx.layers)}"],
                    confidence="high",
                    title=f"Remove the import of {tgt} from {src}")
    sec.ran("layers")


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def _test_record(root: Path) -> dict[str, Any] | None:
    """The test record, or None when it is missing or unreadable (then no
    failing test can be reported as resolved)."""
    from ozagia.core import working_set as WS
    path = WS._record_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tests"), dict):
        return None
    return WS.load_test_record(root)


def _tests(sec: _Section) -> None:
    _failing_tests(sec)
    _untested(sec)


def _failing_tests(sec: _Section) -> None:
    from ozagia.core import working_set as WS
    ctx = sec.ctx
    record = _test_record(ctx.root)
    if record is None:
        sec.skip("failing_tests", f"no readable test record at "
                                  f"{WS._record_path(ctx.root)}")
        sec.data["test_record"] = None
        return
    stale = 0
    for key, rec in sorted((record.get("tests") or {}).items()):
        test_file = _key_file(key)
        if test_file and test_file not in ctx.tracked:
            stale += 1
            continue
        evidence = [f"outcome={rec.get('outcome') or 'failed'}"]
        if _iso(rec.get("ts")):
            evidence.append(f"last failed at {_iso(rec.get('ts'))}")
        if _iso(rec.get("since")):
            evidence.append(f"failing since {_iso(rec.get('since'))}")
        sec.add("failing_test", "major", [key],
                f"{key[len('test:'):]} failed in the last recorded run",
                evidence=evidence, confidence="high",
                note="from the test record (.ozagia/working_set/tests.json); "
                     "the review did not run tests",
                criteria=[{"kind": "test_passes", "args": {"key": key}}],
                title=f"Make {key[len('test:'):]} pass")
    last = record.get("last") or {}
    sec.data["test_record"] = {
        "failing": len(record.get("tests") or {}),
        "last_run": _iso(last.get("ts")),
        "skipped_untracked": stale,
    }
    sec.ran("failing_tests")


def _untested(sec: _Section) -> None:
    ctx = sec.ctx
    mode = "graph" if ctx.index is not None else "filename"
    if ctx.index is None:
        sec.skip("untested_modules.graph",
                 ctx.graph_reason + "; test file names and test imports were "
                 "compared instead (Python only)")
    per_package: dict[str, dict[str, int]] = defaultdict(
        lambda: {"source_files": 0, "tested_files": 0})
    covered: dict[str, list[dict[str, Any]]] = {}
    for rel in ctx.source_modules():
        if sec.expired():
            return
        tests, how = ctx.tests_for(rel)
        package = PurePosixPath(rel).parent.as_posix()
        per_package[package]["source_files"] += 1
        if tests:
            per_package[package]["tested_files"] += 1
            covered[rel] = tests
            continue
        stem = PurePosixPath(rel).stem
        evidence = [f"no test_{stem}.py or {stem}_test.py is tracked"]
        if rel.endswith(".py"):
            evidence.append("no tracked test imports the module")
        if how == "graph":
            evidence.insert(0, "no test edge to the file in the graph")
            confidence, note = "medium", (
                "no test edge in a fresh graph, no test named after the file "
                "and no test import of it; a test may still exercise it "
                "indirectly")
        else:
            confidence, note = "low", (
                "no usable graph; only test file names and test imports were "
                "compared, so a test may cover it under another name")
        sec.add("untested_module", "minor", [_file_key(rel)],
                f"{rel} has no test", evidence=evidence,
                confidence=confidence, note=note,
                title=f"Add a test that imports {rel}")
    sec.ran("untested_modules")
    sec.mode("untested_modules", mode)
    sec.data["mode"] = mode
    sec.data["packages"] = {
        pkg: {**counts, "ratio": round(counts["tested_files"]
                                       / counts["source_files"], 3)}
        for pkg, counts in sorted(per_package.items())[:MAX_DATA_ITEMS]}
    sec.data["source_modules"] = sum(c["source_files"]
                                     for c in per_package.values())
    sec.data["tested_modules"] = sum(c["tested_files"]
                                     for c in per_package.values())

    if ctx.index is None:
        sec.skip("untested_symbols", ctx.graph_reason)
        return
    for rel, tests in sorted(covered.items()):
        if sec.expired():
            return
        texts = [ctx.text(str(t.get("file"))) or "" for t in tests
                 if t.get("file")]
        tokens: set[str] = set()
        for text in texts:
            tokens.update(IDENT.findall(text))
        for symbol in ctx.symbols(rel):
            name = str(symbol.get("name") or "")
            qualname = str(symbol.get("qualname") or "")
            if (not name or "." in qualname or name.startswith("_")
                    or str(symbol.get("kind") or "") not in FUNCTION_KINDS
                    or DYNAMIC_NAME.match(name) or name in tokens):
                continue
            sec.add("untested_symbol", "note", [str(symbol.get("key"))],
                    f"{qualname} in {rel} is not named by any test of the file",
                    evidence=["covering tests: " + ", ".join(
                        sorted({str(t.get("file")) for t in tests}))[:300]],
                    confidence="low",
                    note="name matching in the covering test files; a test "
                         "can exercise it indirectly",
                    title=f"Add a test that calls {qualname}")
    sec.ran("untested_symbols")


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def _reference_tokens(ctx: _Context, sec: _Section) -> Counter[str] | None:
    """Identifier counts over tracked source, test and config files."""
    counts: Counter[str] = Counter()
    for rel in ctx.files:
        if sec.expired():
            return None
        kind = CR._classify_path(rel)
        if kind not in ("source", "test", "config") or CR._is_generated(rel, ()):
            continue
        text = ctx.text(rel)
        if text:
            counts.update(IDENT.findall(text))
    return counts


def _health(sec: _Section) -> None:
    ctx = sec.ctx
    largest: list[tuple[int, str]] = []
    todo_total = 0
    todo_files: list[tuple[int, str]] = []
    for rel in ctx.files:
        if sec.expired():
            return
        kind = CR._classify_path(rel)
        if kind not in ("source", "test") or CR._is_generated(rel, ()):
            continue
        text = sec.read(rel, "large_files", "todo_density")
        if text is None:
            continue
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        largest.append((lines, rel))
        if kind == "source" and lines > LARGE_FILE_LINES:
            sec.add("large_file", "minor", [_file_key(rel)],
                    f"{rel} has {lines} lines",
                    evidence=[f"lines={lines}", f"threshold={LARGE_FILE_LINES}"],
                    confidence="high", note="a size flag, not a defect",
                    title=f"Split {rel} into smaller modules")
        todos = len(TODO_RE.findall(text))
        if todos:
            todo_total += todos
            todo_files.append((todos, rel))
            if todos >= TODO_MIN_COUNT:
                sec.add("todo_density", "note", [_file_key(rel)],
                        f"{rel} has {todos} TODO/FIXME/XXX markers",
                        evidence=[f"markers={todos}", f"lines={lines}"],
                        confidence="high",
                        title=f"Resolve the TODO markers in {rel}")
    largest.sort(key=lambda item: (-item[0], item[1]))
    todo_files.sort(key=lambda item: (-item[0], item[1]))
    sec.data["largest_files"] = [{"file": rel, "lines": n}
                                 for n, rel in largest[:10]]
    sec.data["todo"] = {"total": todo_total,
                        "files": [{"file": rel, "markers": n}
                                  for n, rel in todo_files[:10]]}
    sec.ran("large_files")
    sec.ran("todo_density")

    if ctx.index is None:
        sec.skip("large_functions", ctx.graph_reason)
        sec.skip("unreferenced_symbols", ctx.graph_reason)
        return
    symbols: list[tuple[str, dict[str, Any]]] = [
        (rel, attrs)
        for rel in sorted(ctx.index.get("symbols_by_file") or {})
        if rel in ctx.tracked for attrs in ctx.symbols(rel)]
    for rel, attrs in symbols:
        if str(attrs.get("kind") or "") not in FUNCTION_KINDS:
            continue
        try:
            span = int(attrs["end_line"]) - int(attrs["start_line"]) + 1
        except (KeyError, TypeError, ValueError):
            continue
        if span > LARGE_FUNCTION_LINES:
            sec.add("large_function", "minor", [str(attrs.get("key"))],
                    f"{attrs.get('qualname')} in {rel} spans {span} lines",
                    evidence=[f"lines {attrs.get('start_line')}-"
                              f"{attrs.get('end_line')}",
                              f"threshold={LARGE_FUNCTION_LINES}"],
                    confidence="high", note="line count from the symbol span",
                    title=f"Split {attrs.get('qualname')} into smaller functions")
    sec.ran("large_functions")
    if sec.expired():
        return

    counts = _reference_tokens(ctx, sec)
    if counts is None:
        return
    defs: Counter[str] = Counter(str(attrs.get("name") or "")
                                 for _rel, attrs in symbols)
    class_bases = {(rel, str(attrs.get("qualname"))): attrs.get("bases") or []
                   for rel, attrs in symbols if attrs.get("kind") == "class"}
    excluded: Counter[str] = Counter()
    for rel, attrs in symbols:
        name = str(attrs.get("name") or "")
        qualname = str(attrs.get("qualname") or "")
        if not rel.endswith(".py"):
            excluded["non-Python"] += 1
            continue
        if _is_test(rel) or _entrypoint(rel):
            excluded["tests, entry points and fixtures"] += 1
            continue
        if not name or DYNAMIC_NAME.match(name):
            excluded["dunder and framework-called names"] += 1
            continue
        if attrs.get("decorators"):
            excluded["decorated (may be registered)"] += 1
            continue
        if "." in qualname:
            parent = qualname.rsplit(".", 1)[0]
            bases = [b for b in class_bases.get((rel, parent), [])
                     if b not in ("object", "")]
            if bases:
                excluded["methods of subclasses (may override)"] += 1
                continue
        if counts.get(name, 0) > defs.get(name, 0):
            continue
        sec.add("unreferenced_symbol", "minor", [str(attrs.get("key"))],
                f"{qualname} in {rel} has no callers or references",
                evidence=[f"the name {name!r} occurs only at its "
                          f"definition(s) in tracked source, test and config "
                          f"files ({counts.get(name, 0)} occurrence(s))"],
                confidence="low",
                note="name matching, not call resolution; getattr with a "
                     "computed name, plugins and external callers are not "
                     "seen",
                criteria=[{"kind": "symbol_absent",
                           "args": {"key": str(attrs.get("key"))}}],
                title=f"Remove {qualname} from {rel}")
    sec.data["unreferenced_excluded"] = dict(sorted(excluded.items()))
    for reason, count in sorted(excluded.items()):
        sec.skip("unreferenced_symbols",
                 f"{count} symbol(s) not checked: {reason}")
    sec.ran("unreferenced_symbols")


# --------------------------------------------------------------------------- #
# churn and ownership
# --------------------------------------------------------------------------- #


def _churn_counts(root: Path, days: int) -> Counter[str]:
    text = _git_text(root, ["-c", "core.quotepath=off", "log",
                            f"--since={int(days)}.days.ago", "--no-merges",
                            "--pretty=tformat:@@commit", "--name-only", "HEAD", "--"],
                     timeout=GIT_LOG_TIMEOUT)
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for line in text.splitlines():
        if line == "@@commit":
            seen = set()
            continue
        path = line.strip()
        if path and path not in seen:
            seen.add(path)
            counts[path] += 1
    return counts


def _churn(sec: _Section) -> None:
    from ozagia.config import state_path
    from ozagia.core import owners
    ctx = sec.ctx
    counts = _churn_counts(ctx.root, ctx.churn_days)
    hot = sorted(((n, rel) for rel, n in counts.items()
                  if rel in ctx.tracked and n >= CHURN_MIN_COMMITS),
                 key=lambda item: (-item[0], item[1]))
    sec.data["window_days"] = ctx.churn_days
    sec.data["files_changed"] = sum(1 for rel in counts if rel in ctx.tracked)
    sec.data["hotspots"] = [{"file": rel, "commits": n}
                            for n, rel in hot[:CHURN_TOP]]
    modules = set(ctx.source_modules())
    sec.mode("hotspots", "graph" if ctx.index is not None else "filename")
    for n, rel in hot[:CHURN_TOP]:
        if rel not in modules:
            continue
        tests, how = ctx.tests_for(rel)
        if tests:
            continue
        sec.add("churn_untested", "major", [_file_key(rel)],
                f"{rel} changed in {n} commits in {ctx.churn_days} days and "
                "has no test",
                evidence=[f"commits={n} since {ctx.churn_days} days ago",
                          f"tests found by {how}: none"],
                confidence="medium" if how == "graph" else "low",
                note="churn from git log; the test gap from graph edges, "
                     "test file names and test imports"
                     if how == "graph" else
                     "churn from git log; test gap from test file names and "
                     "imports only",
                title=f"Add tests for frequently changed {rel}")
    sec.ran("hotspots")
    if sec.expired():
        return

    journal = state_path(ctx.root, "journal.jsonl")
    if not journal.is_file():
        sec.skip("sessions", f"no edit journal at {journal}")
        return
    sessions: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for edit in owners.edits(journal):
        for rel in edit.get("files") or []:
            if rel in ctx.tracked:
                sessions[rel].add((str(edit["harness"]), str(edit["session"])))
    busy = sorted(((len(s), rel) for rel, s in sessions.items()),
                  key=lambda item: (-item[0], item[1]))
    sec.data["session_files"] = [{"file": rel, "sessions": n}
                                 for n, rel in busy[:CHURN_TOP]]
    for n, rel in busy:
        if n < MANY_SESSIONS:
            break
        sec.add("many_sessions", "note", [_file_key(rel)],
                f"{rel} was edited by {n} agent sessions",
                evidence=[f"sessions={n} in the edit journal tail"],
                confidence="medium",
                note="from the journal tail (core.owners); older edits are "
                     "not counted",
                title=f"Give {rel} a clear owner")
    sec.ran("sessions")


# --------------------------------------------------------------------------- #
# docs
# --------------------------------------------------------------------------- #


def _doc_files(ctx: _Context) -> tuple[list[str], list[str]]:
    checked, history = [], []
    for rel in ctx.files:
        path = PurePosixPath(rel)
        if path.suffix.casefold() not in DOC_SUFFIXES:
            continue
        parts = {part.casefold() for part in path.parts[:-1]}
        if parts & DOC_HISTORY_PARTS or path.name.casefold() in DOC_HISTORY_NAMES:
            history.append(rel)
        else:
            checked.append(rel)
    return checked, history


def _inside(rel: str) -> bool:
    norm = os.path.normpath(rel)
    return not (os.path.isabs(norm) or norm == ".." or norm.startswith("../"))


def _resolve(ctx: _Context, base: str, target: str) -> str | None:
    """The tracked file or directory `target` names, from the doc's
    directory or the root, as a normalised path inside the root. Untracked
    local files do not count: the reference would not resolve in another
    clone."""
    for start in ([base, ""] if base else [""]):
        rel = os.path.normpath(os.path.join(start, target)) if start else \
            os.path.normpath(target)
        if _inside(rel) and (rel in ctx.tracked or rel in ctx.tracked_dirs
                             or rel == "."):
            return rel
    return None


def _exists(ctx: _Context, base: str, target: str) -> bool:
    return _resolve(ctx, base, target) is not None


def _module_index(ctx: _Context) -> dict[str, str]:
    """Dotted module name -> file, for tracked Python sources."""
    from ozagia.config import load_project_config
    try:
        roots = [r for r in load_project_config(ctx.root).import_roots if r]
    except Exception:  # noqa: BLE001 - a bad config is reported elsewhere
        roots = []
    out: dict[str, str] = {}
    for rel in ctx.files:
        if not rel.endswith(".py") or _is_test(rel):
            continue
        stem = rel[:-3]
        if stem.endswith("/__init__"):
            stem = stem[:-len("/__init__")]
        names = [stem]
        for root in roots:
            if stem.startswith(root.rstrip("/") + "/"):
                names.append(stem[len(root.rstrip("/")) + 1:])
        for name in names:
            out.setdefault(name.replace("/", "."), rel)
    return out


def _stale_dotted(ctx: _Context, span: str,
                  modules: Mapping[str, str]) -> str | None:
    """A reason when `pkg.mod.name` names a module that lacks `name`."""
    parts = span.removesuffix("()").split(".")
    # A one-part prefix (`pkg.anything`) is too often a schema or config
    # name; only `pkg.module.name` and deeper are checked.
    for cut in range(len(parts) - 1, 1, -1):
        module = ".".join(parts[:cut])
        rel = modules.get(module)
        if rel is None:
            continue
        name = parts[cut]
        if ".".join(parts[:cut + 1]) in modules:
            return None
        text = ctx.text(rel)
        if text is None or name in set(IDENT.findall(text)):
            return None
        return f"{module} ({rel}) has no name {name!r}"
    return None


def _doc_targets(text: str) -> Iterable[tuple[int, str, str]]:
    """(line, kind, value) for links and code spans outside fenced blocks."""
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None or line.startswith("    "):
            continue
        for match in _LINK_RE.finditer(line):
            yield number, "link", match.group(1)
        for match in _SPAN_RE.finditer(line):
            yield number, "span", match.group(1).strip()


def _check_target(ctx: _Context, doc: str, kind: str, value: str,
                  modules: Mapping[str, str]) -> str | None:
    base = PurePosixPath(doc).parent.as_posix()
    base = "" if base == "." else base
    if kind == "link":
        if (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or value.startswith("#")
                or value.startswith("//")):
            return None
        target = value.split("#", 1)[0].split("?", 1)[0]
        if not target:
            return None
        if target.startswith("/"):
            target = target.lstrip("/")
            base = ""
        target = target.replace("%20", " ")
        return None if _exists(ctx, base, target) else f"link target {target}"
    if " " in value or any(ch in value for ch in "<>*?{}$~|=\"'[]@"):
        return None
    if value.startswith(("-", "/", ".ozagia", "http")) or "..." in value:
        return None
    if _PATH_SPAN.fullmatch(value) and "/" in value:
        path, _sep, qual = value.partition("::")
        path = re.sub(r":\d+$", "", path)
        if path.endswith("/") or "YYYY" in path:
            return None
        if PurePosixPath(path).suffix.casefold() not in _KNOWN_SUFFIXES:
            return None
        # A span is taken as a path in this repository only when its first
        # component exists here; `.codex/config.toml` or `logs/x.json`
        # name files in other trees.
        first = PurePosixPath(path.removeprefix("./")).parts[0]
        if not _exists(ctx, base, first):
            return None
        resolved = _resolve(ctx, base, path)
        if resolved is None:
            return f"path {path}"
        if qual and resolved.endswith(".py"):
            # Read the in-root file the span resolved to, not the raw span.
            text = ctx.text(resolved)
            name = qual.split(".")[0]
            if text is not None and name not in set(IDENT.findall(text)):
                return f"symbol {qual} in {path}"
        return None
    if (_DOTTED_SPAN.fullmatch(value)
            and PurePosixPath(value).suffix.casefold() not in _KNOWN_SUFFIXES):
        return _stale_dotted(ctx, value, modules)
    return None


def _docs(sec: _Section) -> None:
    ctx = sec.ctx
    docs, history = _doc_files(ctx)
    if history:
        sec.skip("history_docs",
                 f"{len(history)} plan/changelog doc(s) record past or planned "
                 "paths and were not checked")
    modules = ctx.module_names()
    refs = 0
    for doc in docs:
        if sec.expired():
            return
        text = sec.read(doc, "links_and_code_spans")
        if text is None:
            continue
        for line, kind, value in _doc_targets(text):
            refs += 1
            reason = _check_target(ctx, doc, kind, value, modules)
            if reason is None:
                continue
            sec.add("stale_doc_reference", "minor", [_file_key(doc)],
                    f"{doc} refers to something that does not exist",
                    evidence=[f"line {line}: {reason} does not resolve"],
                    confidence="medium",
                    note="links and code spans outside code blocks; a span "
                         "may name a path in another repository",
                    title=f"Fix the stale references in {doc}")
    sec.data.update({"docs": len(docs), "history_docs": len(history),
                     "references": refs})
    sec.ran("links_and_code_spans")


# --------------------------------------------------------------------------- #
# security hygiene
# --------------------------------------------------------------------------- #


def _env_file(name: str) -> bool:
    """`.env`, `.env.<x>` and `<x>.env`, except templates (`.env.example`,
    `example.env`, `prod.env.sample`)."""
    if name == ".env":
        return True
    if not (name.startswith(".env.") or name.endswith(".env")):
        return False
    if name.endswith(ENV_TEMPLATE_SUFFIXES):
        return False
    return not any(part in ENV_TEMPLATE_WORDS for part in name.split("."))


def _secret_name(name: str) -> bool:
    if name.startswith(".env") or name.endswith(".env"):
        return False
    return (name in SECRET_NAMES or name in CR.SENSITIVE_NAMES
            or name.endswith(SECRET_SUFFIXES))


def _enclosing_symbol(ctx: _Context, rel: str, line: int) -> str | None:
    best: tuple[int, str] | None = None
    for symbol in ctx.symbols(rel):
        try:
            start, end = int(symbol["start_line"]), int(symbol["end_line"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= line <= end and (best is None or start > best[0]):
            best = (start, str(symbol.get("key")))
    return best[1] if best else None


def _call_args(text: str, start: int, limit: int = 2000) -> str:
    """The text from `start` (just after an opening parenthesis) to its
    closing parenthesis, at most `limit` characters."""
    depth = 1
    for pos in range(start, min(len(text), start + limit)):
        ch = text[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:pos]
    return text[start:start + limit]


def _safe_yaml_load(text: str, offset: int) -> bool:
    """A `yaml.load(` call at `offset` passes an explicit Loader= that is not
    one of the object-building loaders."""
    args = _call_args(text, offset)
    match = _LOADER_ARG.search(args)
    return bool(match) and match.group(1) not in UNSAFE_YAML_LOADERS


def _security(sec: _Section) -> None:
    ctx = sec.ctx
    sec.data["note"] = SECURITY_NOTE
    sensitive_paths: list[str] = []
    fixtures = [rel for rel in ctx.files if _fixture(rel)]
    if fixtures:
        sec.skip("sensitive_names", _excluded_note(fixtures))
    fixture_set = set(fixtures)
    for rel in ctx.files:
        if rel in fixture_set:
            continue
        name = PurePosixPath(rel).name.casefold()
        if _env_file(name):
            sec.add("committed_env_file", "major", [_file_key(rel)],
                    f"{rel} is a tracked environment file",
                    evidence=[f"tracked by git: {rel}"], confidence="high",
                    note="the name matches; the contents were not inspected",
                    criteria=[{"kind": "file_absent",
                               "args": {"key": _file_key(rel)}}],
                    title=f"Stop tracking {rel}")
        elif _secret_name(name):
            sec.add("sensitive_file", "major", [_file_key(rel)],
                    f"{rel} has a credential or key file name",
                    evidence=[f"tracked by git: {rel}"], confidence="medium",
                    note="the name matches; a .pem may be a public "
                         "certificate",
                    criteria=[{"kind": "file_absent",
                               "args": {"key": _file_key(rel)}}],
                    title=f"Remove {rel} from the repository")
        elif _is_source(rel) and CR._sensitive_path(rel):
            sensitive_paths.append(rel)
    sec.data["sensitive_paths"] = {"count": len(sensitive_paths),
                                   "files": sensitive_paths[:MAX_DATA_ITEMS]}
    sec.ran("sensitive_names")

    constructs = 0
    source_fixtures = [rel for rel in fixtures if _is_source(rel)]
    if source_fixtures:
        sec.skip("dangerous_constructs", _excluded_note(source_fixtures))
    for rel in ctx.files:
        if sec.expired():
            return
        if (rel in fixture_set or not _is_source(rel)
                or CR._is_generated(rel, ())):
            continue
        text = sec.read(rel, "dangerous_constructs")
        if not text:
            continue
        offset = 0
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            line_start, offset = offset, offset + len(line)
            stripped = line.lstrip()
            if stripped.startswith(("#", "//")):
                continue
            for pattern, label in DANGEROUS_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if label == _YAML_LOAD and _safe_yaml_load(
                        text, line_start + match.end()):
                    continue
                constructs += 1
                where = _enclosing_symbol(ctx, rel, number)
                sec.add("dangerous_construct", "minor", [_file_key(rel)],
                        f"{rel} uses sensitive constructs",
                        evidence=[f"line {number}: {label}"
                                  + (f" in {where}" if where else "")],
                        confidence="medium",
                        note="token matching is a review flag, not a "
                             "vulnerability finding",
                        title=f"Review the sensitive constructs in {rel}")
    sec.data["dangerous_constructs"] = constructs
    sec.ran("dangerous_constructs")
    sec.skip("secret_contents", "file contents are not scanned for secrets")


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #


def _unpinned(image: str) -> bool:
    from ozagia.core.ops_graph import image_pinned
    return image_pinned(image) is False


def declared_ops_nodes(root: str | Path, rels: Iterable[str] | None = None
                       ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The ops nodes declared in the repository's tracked operations sources
    (``core.ops_graph``, parsed as the ops section parses them), for callers
    that need an ``ops:`` key when the main graph holds no ops nodes (the
    goal system). ``rels`` limits the files read (default: every tracked
    source; fixture trees included, since a goal may name any source).
    Returns ``({key: attrs}, {rel: error})``; attrs carry ``kind``, ``name``,
    ``path`` and ``line`` like the graph's ops nodes."""
    from ozagia.core import ops_graph as OG
    path = Path(root).resolve()
    all_files, _skipped = _tracked_files(path)
    tracked = set(all_files)
    files = all_files if rels is None else [rel for rel in all_files
                                            if rel in set(rels)]
    nodes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    combined = OG.OpsGraph()
    for rel, _fmt, _text, graph, error in OG.iter_ops_sources(
            files, lambda rel: read_text(path, rel, real_root=path)[0]):
        if graph is None:
            errors[rel] = str(error)
            continue
        combined.extend(graph)
    try:
        # Cross-file references, which also add the shared `ops:image/` nodes.
        OG.resolve(combined, file_exists=lambda rel: rel in tracked, root=str(path))
    except Exception as exc:  # noqa: BLE001 - the parsed nodes still count
        errors["*"] = f"resolve: {type(exc).__name__}: {exc}"
    for node in combined.nodes:
        attrs = {"family": "ops", "kind": node.kind, "name": node.name,
                 "path": node.path, **node.attrs}
        if isinstance(node.line, int):
            attrs["line"] = node.line
        nodes.setdefault(node.key, attrs)
    return nodes, errors


def _port_scope(port: str) -> str:
    text = port.split("/", 1)[0]
    if any(host in text for host in ("127.0.0.1", "localhost", "[::1]", "::1:")):
        return "loopback"
    return "all interfaces"


def _ops(sec: _Section) -> None:
    from ozagia.core import ops_graph as OG
    ctx = sec.ctx
    ops_checks = ("image_pinning", "published_ports", "restart_and_healthcheck")
    found = [rel for rel in ctx.files if OG.ops_source_format(rel)]
    excluded = [rel for rel in found if _fixture(rel)]
    sources = [rel for rel in found if not _fixture(rel)]
    if excluded:
        sec.skip("ops_source", _excluded_note(excluded))
    sec.data["sources"] = len(sources)
    if ctx.dev is not None and ctx.index is not None:
        sec.data["graph_ops_nodes"] = len(OG.ops_nodes(ctx.dev))
    yaml_missing = False
    services = 0
    for rel, fmt, text, graph, error in OG.iter_ops_sources(
            sources, lambda rel: sec.read(rel, *ops_checks)):
        if sec.expired():
            return
        if text is None:
            sec.skip("ops_source", f"{rel} is unreadable or too large")
            continue
        if graph is None:
            sec.skip("ops_source", f"{rel}: {error}")
            sec.skip_file(ops_checks, rel,
                          f"parse error: {str(error).split(':', 1)[0]}")
            continue
        compose_specs: dict[str, Any] | None = None
        if fmt == "compose":
            data, error = OG._load_yaml(text)
            if error is not None:
                yaml_missing = yaml_missing or "unavailable" in error
                if "unavailable" not in error:
                    sec.skip("restart_and_healthcheck", f"{rel}: {error}")
                    sec.skip_file("restart_and_healthcheck", rel,
                                  "YAML error")
            elif isinstance(data, dict) and isinstance(data.get("services"), dict):
                compose_specs = data["services"]
        unit_entries = (OG.parse_unit_ini(text) if fmt == "quadlet" else [])
        for node in graph.nodes:
            attr = "image" if node.attrs.get("image") else "base_image"
            image = node.attrs.get(attr)
            if isinstance(image, str) and _unpinned(image):
                sec.add("unpinned_image", "minor", [node.key],
                        f"{node.kind.replace('_', ' ')} {node.name} in {rel} "
                        f"uses {image} without a pinned tag or digest",
                        evidence=[f"{rel}:{node.line}: image {image}"],
                        confidence="high",
                        note="an untagged reference resolves to latest",
                        criteria=[{"kind": "ops_has",
                                   "args": {"key": node.key, "attr": attr,
                                            "pinned": True}}],
                        title=f"Pin the image of {node.name} in {rel}")
            ports = [p for p in node.attrs.get("ports") or [] if isinstance(p, str)]
            if ports and node.kind in ("compose_service", "quadlet_container"):
                scopes = {p: _port_scope(p) for p in ports}
                wide = any(s == "all interfaces" for s in scopes.values())
                sec.add("published_port", "minor" if wide else "note",
                        [node.key],
                        f"{node.name} in {rel} publishes {len(ports)} port(s)"
                        + (" on all interfaces" if wide else " on loopback"),
                        evidence=[f"{p} ({s})" for p, s in scopes.items()],
                        confidence="high",
                        note="declared in source; live state was not checked",
                        title=f"Review the published ports of {node.name}")
            if node.kind == "compose_service":
                services += 1
                if compose_specs is None:
                    continue
                spec = compose_specs.get(node.name)
                spec = spec if isinstance(spec, dict) else {}
                deploy = spec.get("deploy") if isinstance(spec.get("deploy"), dict) else {}
                if not spec.get("restart") and not deploy.get("restart_policy"):
                    sec.add("no_restart_policy", "minor", [node.key],
                            f"compose service {node.name} in {rel} has no "
                            "restart policy",
                            evidence=[f"{rel}:{node.line}: no restart or "
                                      "deploy.restart_policy"],
                            confidence="high",
                            title=f"Set a restart policy for {node.name}")
                health = spec.get("healthcheck")
                if not isinstance(health, dict) or health.get("disable"):
                    sec.add("no_healthcheck", "note", [node.key],
                            f"compose service {node.name} in {rel} has no "
                            "health check",
                            evidence=[f"{rel}:{node.line}: no healthcheck"],
                            confidence="medium",
                            note="the image may define HEALTHCHECK itself",
                            title=f"Add a health check for {node.name}")
            elif node.kind == "quadlet_container":
                services += 1
                keys = {(section, key) for section, key, _v, _l in unit_entries}
                if ("Service", "Restart") not in keys:
                    sec.add("no_restart_policy", "minor", [node.key],
                            f"quadlet {rel} has no Restart= in [Service]",
                            evidence=[f"{rel}: no [Service] Restart="],
                            confidence="high",
                            title=f"Set Restart= in {rel}")
                if ("Container", "HealthCmd") not in keys:
                    sec.add("no_healthcheck", "note", [node.key],
                            f"quadlet {rel} has no HealthCmd=",
                            evidence=[f"{rel}: no [Container] HealthCmd="],
                            confidence="medium",
                            note="the image may define HEALTHCHECK itself",
                            title=f"Add HealthCmd= to {rel}")
    sec.data["services"] = services
    sec.ran("image_pinning")
    sec.ran("published_ports")
    if yaml_missing:
        sec.skip("restart_and_healthcheck",
                 "compose restart and healthcheck need PyYAML (the iac-yaml "
                 "extra)")
    else:
        sec.ran("restart_and_healthcheck")
    sec.skip("live_state", "only declared sources were read; no container or "
                           "unit state was queried")


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #


def _requirement_names(items: Iterable[Any]) -> list[str]:
    out = []
    for item in items:
        if isinstance(item, str):
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", item)
            if match:
                out.append(match.group(1))
    return out


def _manifest(rel: str, text: str) -> tuple[str, list[str], bool] | None:
    """(ecosystem, dependency names, is its own lock) or None."""
    name = PurePosixPath(rel).name
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project") or {}
            deps = _requirement_names(project.get("dependencies") or [])
            for group in (project.get("optional-dependencies") or {}).values():
                deps += _requirement_names(group or [])
            for group in (data.get("dependency-groups") or {}).values():
                deps += _requirement_names(group or [])
            poetry = ((data.get("tool") or {}).get("poetry") or {})
            deps += [k for k in (poetry.get("dependencies") or {}) if k != "python"]
            return "python", deps, False
        if name == "package.json":
            data = json.loads(text)
            deps = []
            for field in ("dependencies", "devDependencies",
                          "optionalDependencies", "peerDependencies"):
                if isinstance(data.get(field), dict):
                    deps += list(data[field])
            return "node", deps, False
        if name == "Cargo.toml":
            data = tomllib.loads(text)
            deps = []
            for field in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps += list((data.get(field) or {}))
            deps += list(((data.get("workspace") or {}).get("dependencies") or {}))
            return "rust", deps, False
        if name == "go.mod":
            deps = re.findall(r"^\s*(?:require\s+)?([a-z0-9][\w.\-]*\.[a-z]{2,}"
                              r"/[\w.\-/]+)\s+v[\w.\-+]+", text, re.M)
            return "go", deps, False
        if name.startswith("requirements") and name.endswith(".txt"):
            lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
            reqs = [line for line in lines
                    if line and not line.startswith(("-", "--"))]
            pinned = bool(reqs) and all("==" in line or "@" in line
                                        for line in reqs)
            return "python", _requirement_names(reqs), pinned
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        return None
    return None


def _lock_for(ctx: _Context, rel: str, ecosystem: str) -> str | None:
    directory = PurePosixPath(rel).parent
    while True:
        for name in LOCK_FILES[ecosystem]:
            candidate = (directory / name).as_posix()
            candidate = candidate[2:] if candidate.startswith("./") else candidate
            if candidate in ctx.tracked:
                return candidate
        if directory.as_posix() in (".", ""):
            return None
        directory = directory.parent


def _dependencies(sec: _Section) -> None:
    ctx = sec.ctx
    manifests = []
    for rel in ctx.files:
        name = PurePosixPath(rel).name
        if not (name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod")
                or (name.startswith("requirements") and name.endswith(".txt"))):
            continue
        if any(part in CR.GENERATED_PARTS for part in PurePosixPath(rel).parts[:-1]):
            continue
        text = sec.read(rel, "manifests", "lock_files")
        parsed = _manifest(rel, text or "") if text is not None else None
        if parsed is None:
            sec.skip("manifest", f"{rel} could not be parsed")
            if text is not None:
                sec.skip_file(("manifests", "lock_files"), rel, "parse error")
            continue
        ecosystem, deps, own_lock = parsed
        lock = rel if own_lock else _lock_for(ctx, rel, ecosystem)
        manifests.append({"file": rel, "ecosystem": ecosystem,
                          "dependencies": len(deps),
                          "names": sorted(set(deps))[:MAX_DATA_ITEMS],
                          "lock": lock})
        if deps and lock is None:
            sec.add("missing_lock_file", "minor", [_file_key(rel)],
                    f"{rel} declares {len(deps)} {ecosystem} dependencies and "
                    "no lock file is tracked",
                    evidence=["looked for: " + ", ".join(LOCK_FILES[ecosystem])
                              + " in its directory and parents"],
                    confidence="medium",
                    note="libraries often leave lock files out on purpose",
                    title=f"Commit a lock file for {rel}")
    sec.data["manifests"] = manifests[:MAX_DATA_ITEMS]
    sec.ran("manifests")
    sec.ran("lock_files")
    sec.skip("advisories", "no vulnerability database was consulted")


SECTION_RUNNERS: dict[str, Callable[[_Section], None]] = {
    "architecture": _architecture,
    "tests": _tests,
    "health": _health,
    "churn": _churn,
    "docs": _docs,
    "security": _security,
    "ops": _ops,
    "dependencies": _dependencies,
}


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #


def load_packet(path: str | Path) -> dict[str, Any]:
    """A repo-review packet from a JSON file."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepoReviewError(f"cannot read packet {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != PACKET_SCHEMA:
        raise RepoReviewError(f"{path} is not a {PACKET_SCHEMA} packet")
    return data


# Checks whose population depends on the graph: `graph` covers every graph
# file with symbols, `filename` only tracked Python files.
FULL_MODE = "graph"


def _check_state(packet: Mapping[str, Any],
                 ftype: str) -> dict[str, Any] | None:
    """What a packet knows about the check behind `ftype`: its ids, mode,
    skipped files and the files of each id. None when the check did not run
    to completion in the packet."""
    section_name = FINDING_TYPES[ftype]
    check = FINDING_CHECKS[ftype]
    section = (packet.get("sections") or {}).get(section_name)
    if not isinstance(section, Mapping):
        return None
    if check not in (section.get("checked") or []):
        return None
    by_type = section.get("ids_by_type")
    if isinstance(by_type, Mapping):
        ids = [str(i) for i in by_type.get(ftype) or []]
    else:
        ids = [str(f.get("id")) for f in packet.get("findings") or []
               if isinstance(f, Mapping) and f.get("type") == ftype]
    skipped = set(((section.get("skipped_files") or {}).get(check) or {}))
    skipped |= set(packet.get("skipped_files") or {})
    id_files = section.get("id_files")
    files: dict[str, list[str]] = {}
    if isinstance(id_files, Mapping):
        files = {str(k): [str(f) for f in v or []] for k, v in id_files.items()}
    for finding in packet.get("findings") or []:
        if isinstance(finding, Mapping) and str(finding.get("id")) not in files:
            files[str(finding.get("id"))] = _key_files(finding.get("keys") or [])
    return {"check": check, "ids": ids,
            "mode": (section.get("modes") or {}).get(check),
            "skipped": skipped, "files": files}


def _type_ids(packet: Mapping[str, Any], ftype: str) -> list[str] | None:
    """The ids of one finding type, or None when its check did not run to
    completion in the packet."""
    state = _check_state(packet, ftype)
    return None if state is None else state["ids"]


def finding_state(packet: Mapping[str, Any], finding_id: str, *,
                  type: str | None = None,  # noqa: A002 - the goal system's name
                  keys: Iterable[str] | None = None) -> str:
    """Whether a finding is ``present``, ``absent`` or ``not_checked`` in a
    packet, by the rules ``compare_packets`` uses.

    Present: the id is in a section's ``ids`` (capped findings included).
    Absent: the check that produces ``type`` ran to completion over its full
    population: it is in ``checked``, its mode (for graph-dependent checks)
    is ``graph``, and it skipped no file the finding is about (with
    ``keys``) or no file at all (without). A missing test record leaves
    ``failing_tests`` unchecked. Anything else, including an unknown
    ``type``, is not_checked.
    """
    fid = str(finding_id)
    for section in (packet.get("sections") or {}).values():
        if isinstance(section, Mapping) and fid in (section.get("ids") or []):
            return "present"
    if type not in FINDING_TYPES:
        return "not_checked"
    state = _check_state(packet, type)
    if state is None:
        return "not_checked"
    if state["mode"] is not None and state["mode"] != FULL_MODE:
        return "not_checked"
    if state["skipped"]:
        if keys is None:
            return "not_checked"
        if set(_key_files(keys)) & state["skipped"]:
            return "not_checked"
    return "absent"


def compare_packets(current: Mapping[str, Any],
                    previous: Mapping[str, Any]) -> dict[str, Any]:
    """New, resolved and unchanged finding ids between two packets.

    A finding type is compared only when the check that produces it ran to
    completion in both packets over the same population: same root, same
    mode (graph or file names) and, for a resolved id, no file of the
    finding skipped by the current run (unreadable, too large, binary, a
    symlink or unparseable). The others are listed in ``not_compared`` or
    ``held``, so their findings are not reported as resolved.
    """
    new: list[str] = []
    resolved: list[str] = []
    unchanged: list[str] = []
    not_compared: list[dict[str, str]] = []
    held: list[dict[str, str]] = []
    root_now, root_before = current.get("root"), previous.get("root")
    root_mismatch = bool(root_now and root_before and root_now != root_before)
    for ftype in FINDING_TYPES:
        entry = {"section": FINDING_TYPES[ftype], "type": ftype,
                 "check": FINDING_CHECKS[ftype]}
        if root_mismatch:
            not_compared.append({**entry, "reason": (
                f"the previous packet is for {root_before}, not {root_now}")})
            continue
        now = _check_state(current, ftype)
        before = _check_state(previous, ftype)
        if now is None and before is None:
            continue
        if now is None or before is None:
            not_compared.append({**entry, "reason": (
                "check did not run to completion in the current packet"
                if now is None else
                "check did not run to completion in the previous packet")})
            continue
        if now["mode"] != before["mode"]:
            not_compared.append({**entry, "reason": (
                f"the check ran in {before['mode'] or 'default'} mode before "
                f"and in {now['mode'] or 'default'} mode now")})
            continue
        a, b = set(now["ids"]), set(before["ids"])
        new += sorted(a - b)
        unchanged += sorted(a & b)
        for fid in sorted(b - a):
            files = before["files"].get(fid)
            if files is None and now["skipped"]:
                reason = ("the current run skipped files and the finding's "
                          "files are not recorded")
            elif files and set(files) & now["skipped"]:
                reason = ("the current run could not read "
                          + ", ".join(sorted(set(files) & now["skipped"])[:5]))
            else:
                resolved.append(fid)
                continue
            held.append({"id": fid, "type": ftype, "reason": reason})
    previous_findings = {str(f.get("id")): f for f in previous.get("findings") or []
                         if isinstance(f, Mapping)}
    return {
        "previous_root": root_before,
        "previous_head": previous.get("head_commit"),
        "previous_generated_at": previous.get("generated_at"),
        "root_mismatch": root_mismatch,
        "new": sorted(new),
        "resolved": sorted(resolved),
        "unchanged": sorted(unchanged),
        "resolved_findings": [
            {k: previous_findings[i].get(k)
             for k in ("id", "section", "type", "severity", "message", "keys")}
            for i in sorted(resolved) if i in previous_findings][:MAX_DATA_ITEMS],
        "not_compared": not_compared,
        "held": held,
    }


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #


def _tracked_files(root: Path) -> tuple[list[str], dict[str, str]]:
    """Tracked regular files inside the root, and the tracked paths left out
    because they are symlinks or resolve outside the root (path -> reason).
    A symlink is never followed: its target may be any file on the host."""
    from ozagia.config import state_rel
    out = _git_text(root, ["ls-files", "-z", "--cached"]).split("\0")
    state = state_rel(root)
    real_root = root.resolve()
    files: list[str] = []
    skipped: dict[str, str] = {}
    for rel in sorted(set(out)):
        if not rel:
            continue
        if state and (rel == state or rel.startswith(state + "/")):
            continue
        path = root / rel
        try:
            if path.is_symlink():
                skipped[rel] = "symlink"
                continue
            if not path.resolve().is_relative_to(real_root):
                skipped[rel] = "resolves outside the root"
                continue
            if path.is_file():
                files.append(rel)
        except OSError as exc:
            skipped[rel] = f"unreadable: {type(exc).__name__}"
    return files, skipped


def _sections_arg(sections: Iterable[str] | str | None) -> list[str]:
    if sections is None:
        return list(SECTIONS)
    if isinstance(sections, str):
        sections = [sections]
    wanted: list[str] = []
    for item in sections:
        for name in str(item).split(","):
            name = name.strip()
            if not name:
                continue
            if name not in SECTIONS:
                raise RepoReviewError(
                    f"unknown section {name!r}; known: {', '.join(SECTIONS)}")
            if name not in wanted:
                wanted.append(name)
    return [name for name in SECTIONS if name in wanted] or list(SECTIONS)


def _summary(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "findings": len(findings),
        **{severity: sum(1 for f in findings if f.get("severity") == severity)
           for severity in SEVERITIES},
        "by_section": dict(sorted(Counter(
            str(f.get("section")) for f in findings).items())),
    }


def review_repo(root: str | Path, *,
                sections: Iterable[str] | str | None = None,
                graph_path: str | Path | None = None,
                compare: str | Path | Mapping[str, Any] | None = None,
                budget_s: float = DEFAULT_BUDGET_S,
                index: bool = False,
                churn_days: int = CHURN_DAYS,
                max_findings_per_section: int = MAX_FINDINGS_PER_SECTION,
                memory_mb: float = DEFAULT_MEMORY_MB,
                disk_mb: float = DEFAULT_DISK_MB,
                indexer: Callable[[Path, Path], Mapping[str, Any]] | None = None,
                clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Review the checkout at ``root`` and return a repo-review packet.

    ``sections`` selects sections (default all, ``SECTIONS``). ``compare``
    is a previous packet (path or mapping) to diff finding ids against.
    ``budget_s`` bounds the wall-clock time of the sections; a section cut
    short says so. ``index`` rebuilds the graph first with ``indexer``
    (default: the same build as ``ozagia index``). Raises PreflightError when
    preflight refuses the run. No model is called.
    """
    for name, value in (("budget_s", budget_s), ("memory_mb", memory_mb),
                        ("disk_mb", disk_mb)):
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            raise RepoReviewError(f"{name} must be a finite number >= 0")
    if churn_days < 1:
        raise RepoReviewError("churn_days must be >= 1")
    if max_findings_per_section < 1:
        raise RepoReviewError("max_findings_per_section must be >= 1")
    wanted = _sections_arg(sections)
    previous: Mapping[str, Any] | None = None
    if compare is not None:
        previous = (load_packet(compare) if not isinstance(compare, Mapping)
                    else compare)
        if previous.get("schema") != PACKET_SCHEMA:
            raise RepoReviewError(f"compare packet is not {PACKET_SCHEMA}")
    started = clock()
    pre = preflight(root, memory_mb=memory_mb, disk_mb=disk_mb)
    if not pre["ok"]:
        refusal = {
            "schema": PACKET_SCHEMA, "ok": False, "refused": True,
            "reason": pre["reason"], "root": pre["root"],
            "preflight": {"ok": False, "checks": pre["checks"]},
            "resources": pre["resources"], "truth_assessed": False,
        }
        raise PreflightError(
            "review refused by " + str(pre["reason"]) + ": " + "; ".join(
                c["message"] for c in pre["checks"] if c["level"] == "error"),
            refusal)
    path = Path(pre["root"])
    head = str(pre["head"])
    # The working tree as the review saw it (taken before any file is
    # read): a goal compares its files' content now against this to tell
    # whether the packet still describes them (`core.fingerprint`).
    from ozagia.core.fingerprint import tree_fingerprint
    tree = tree_fingerprint(path)
    generated_at = _utc_now()
    graph_file = _graph_file(path, graph_path)
    index_report: dict[str, Any] | None = None
    if index:
        try:
            report = (indexer or _default_indexer)(path, graph_file)
            index_report = {"ok": True, **{k: report.get(k) for k in
                                           ("action", "nodes", "edges", "seconds")
                                           if k in report}}
        except Exception as exc:  # noqa: BLE001 - reported, then degrade
            index_report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    graph_info, dev, graph_index = _load_graph(path, graph_file, head)
    graph_info["indexed"] = index_report
    try:
        from ozagia.config import load_project_config
        layers = load_project_config(path).review_layers
        config_error = None
    except Exception as exc:  # noqa: BLE001 - review the rest
        layers, config_error = (), f"{type(exc).__name__}: {exc}"
    files, skipped_files = _tracked_files(path)
    ctx = _Context(path, head, files, graph_info, dev, graph_index,
                   layers=layers, deadline=started + float(budget_s),
                   clock=clock, churn_days=int(churn_days),
                   cap=int(max_findings_per_section))

    section_out: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    checked: list[str] = []
    not_checked: list[dict[str, str]] = []
    truncated: dict[str, str] = {}
    for name in wanted:
        sec = _Section(name, ctx)
        if ctx.expired():
            sec.truncated = "budget"
            sec.skip("all", "the time budget ran out before this section")
        else:
            try:
                SECTION_RUNNERS[name](sec)
            except RepoReviewError as exc:
                sec.skip("all", f"section failed: {exc}")
        if name == "architecture" and config_error:
            sec.skip("layers", f"project config could not be read: {config_error}")
        out, kept = sec.finish()
        section_out[name] = out
        findings.extend(kept)
        checked.extend(f"{name}.{check}" for check in out["checked"])
        not_checked.extend(out["not_checked"])
        if out["truncated_reason"]:
            truncated[name] = out["truncated_reason"]
    if skipped_files:
        shown = ", ".join(list(skipped_files)[:10])
        not_checked.append({
            "section": "*", "check": "skipped_files",
            "reason": f"{len(skipped_files)} tracked path(s) were not read "
                      f"(symlinks or paths outside the root): {shown}"
                      + (" ..." if len(skipped_files) > 10 else "")})
    not_checked.extend([
        {"section": "*", "check": "tests_run",
         "reason": "no tests were run; failing tests come from the record"},
        {"section": "*", "check": "lint_and_types",
         "reason": "no linter or type checker was run"},
        {"section": "*", "check": "model_review",
         "reason": "no model was called"},
    ])
    findings.sort(key=lambda f: (SEVERITY_RANK[f["severity"]],
                                 SECTIONS.index(f["section"]), f["type"],
                                 f["keys"], f["id"]))
    elapsed = max(0.0, clock() - started)
    packet: dict[str, Any] = {
        "schema": PACKET_SCHEMA,
        "schema_version": SCHEMA,
        "generator": GENERATOR,
        "root": str(path),
        "head_commit": head,
        "branch": pre["branch"],
        "generated_at": generated_at,
        "tree": tree,
        "preflight": {"ok": True, "checks": pre["checks"]},
        "resources": pre["resources"],
        "graph": graph_info,
        "sections_requested": wanted,
        "sections": section_out,
        "findings": findings,
        "summary": _summary(findings),
        "checked": checked,
        "not_checked": not_checked,
        "truncated": {"any": bool(truncated), "sections": truncated},
        "budget": {"budget_s": float(budget_s), "elapsed_s": round(elapsed, 3),
                   "exhausted": ctx.expired()},
        "limits": {
            "max_findings_per_section": int(max_findings_per_section),
            "max_evidence": MAX_EVIDENCE, "max_keys": MAX_KEYS,
            "max_read_bytes": MAX_READ_BYTES,
            "large_file_lines": LARGE_FILE_LINES,
            "large_function_lines": LARGE_FUNCTION_LINES,
            "churn_days": int(churn_days),
            "churn_min_commits": CHURN_MIN_COMMITS,
        },
        "files": len(files),
        # Tracked paths no section read (symlinks, paths outside the root).
        "skipped_files": dict(sorted(skipped_files.items())),
        "comparison": None,
        "model_review": {"status": "not_run", "reason": "no model was called",
                         "truth_assessed": False},
        "security_note": SECURITY_NOTE,
        "truth_assessed": False,
    }
    if previous is not None:
        packet["comparison"] = compare_packets(packet, previous)
        if isinstance(compare, (str, Path)):
            packet["comparison"]["previous"] = str(compare)
    return packet


# --------------------------------------------------------------------------- #
# optional model pass (injected runner only)
# --------------------------------------------------------------------------- #


def compact_packet(packet: Mapping[str, Any], *,
                   max_findings: int = COMPACT_MAX_FINDINGS) -> dict[str, Any]:
    """A small packet for a model reviewer: findings without goals or ids
    of dropped findings, plus what was not checked."""
    findings = [
        {k: f.get(k) for k in ("id", "section", "type", "severity", "message",
                               "keys", "evidence", "confidence")}
        for f in (packet.get("findings") or [])[:max(0, int(max_findings))]]
    return {
        "schema": COMPACT_SCHEMA,
        "source_schema": packet.get("schema"),
        "head_commit": packet.get("head_commit"),
        "graph": {k: (packet.get("graph") or {}).get(k)
                  for k in ("freshness", "usable", "reason")},
        "summary": packet.get("summary"),
        "sections": {name: {k: s.get(k) for k in ("status", "checked",
                                                  "truncated", "data")}
                     for name, s in (packet.get("sections") or {}).items()},
        "findings": findings,
        "findings_omitted": max(0, len(packet.get("findings") or []) - len(findings)),
        "not_checked": packet.get("not_checked"),
        "instructions": (
            "Deterministic findings below are evidence, not proof. Report "
            "additional issues as findings with wording, severity "
            "(blocking|major|minor|note), issue_type and citation_keys that "
            "name graph keys from this packet."),
        "truth_assessed": False,
    }


def validate_model_output(output: Mapping[str, Any]) -> dict[str, Any]:
    """Shape check for a model reviewer's output (no truth check)."""
    errors: list[str] = []
    items = output.get("findings") if isinstance(output, Mapping) else None
    if not isinstance(items, list):
        errors.append("findings must be a list")
        items = []
    for number, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append(f"finding {number} is not an object")
            continue
        if not str(item.get("wording") or "").strip():
            errors.append(f"finding {number} has no wording")
        if str(item.get("severity") or "") not in SEVERITY_RANK:
            errors.append(f"finding {number} has an unknown severity")
    return {"valid": not errors, "errors": errors}


def run_model_pass(packet: Mapping[str, Any], *,
                   model_runner: Callable[..., Any],
                   models: Iterable[str] | str,
                   roles: Iterable[str] | str = ("critic",),
                   validator: Callable[..., Any] | None = None,
                   max_findings: int = COMPACT_MAX_FINDINGS) -> dict[str, Any]:
    """Run injected model reviewers over the compact packet.

    Returns a copy of ``packet`` whose ``model_review`` holds the runs and
    the model findings, each attributed to its role and model. Deterministic
    findings are not changed. The runner owns network access and gating.
    """
    from ozagia.harness import review as HR
    compact = compact_packet(packet, max_findings=max_findings)
    result = HR.run_review_matrix(
        {"id": "repo-review"}, compact, model_runner=model_runner,
        roles=roles, models=models,
        validator=validator or validate_model_output)
    out = json.loads(json.dumps(dict(packet)))
    keys = {k for f in packet.get("findings") or [] for k in f.get("keys") or []}
    model_findings: list[dict[str, Any]] = []
    for run in result["runs"]:
        for item in (run.get("output") or {}).get("findings") or []:
            wording = str(item.get("wording") or "").strip()
            cited = [str(k) for k in item.get("citation_keys") or []]
            model_findings.append({
                "id": "RRM-" + hashlib.sha256(
                    f"{run['reviewer_id']}\0{wording}".encode()).hexdigest()[:16],
                "source": "model",
                "reviewer": run["reviewer_id"],
                "role": run["role"],
                "model": run["model"],
                "type": str(item.get("issue_type") or "unknown"),
                "severity": str(item.get("severity")),
                "message": wording,
                "keys": [k for k in cited if k in keys],
                "unknown_keys": [k for k in cited if k not in keys],
                "evidence": [str(item["quote"])] if item.get("quote") else [],
                "confidence": item.get("confidence"),
                "truth_assessed": False,
            })
    out["model_review"] = {
        "status": "injected",
        "runs": [{k: run.get(k) for k in ("role", "model", "reviewer_id",
                                          "usage", "provider", "packet_sha256")}
                 for run in result["runs"]],
        "errors": result["errors"],
        "findings": model_findings,
        "note": "model output is unverified and kept apart from the "
                "deterministic findings",
        "truth_assessed": False,
    }
    out["truth_assessed"] = False
    return out


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


exit_code = CR.exit_code
atomic_write = CR.atomic_write
packet_json = CR.packet_json


def reviews_dir(root: str | Path) -> Path:
    from ozagia.config import state_path
    return state_path(Path(root).resolve(), REVIEWS_DIR)


def prune_packets(directory: Path, keep: int = MAX_SAVED_PACKETS) -> list[str]:
    """Delete all but the newest `keep` timestamped packets in `directory`
    (``latest.json`` and ``compact.json`` are kept). Returns the deleted
    paths."""
    packets = sorted((p for p in directory.glob(f"{PACKET_PREFIX}*.json")
                      if p.is_file() and not p.is_symlink()),
                     key=lambda p: p.name, reverse=True)
    removed = []
    for path in packets[max(0, int(keep)):]:
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            continue
    return removed


def save_packet(root: str | Path, packet: Mapping[str, Any]) -> dict[str, str]:
    """Write the packet under ``<state dir>/reviews/`` (atomic): a
    timestamped file and ``latest.json``, then keep only the newest
    MAX_SAVED_PACKETS timestamped packets. Returns both paths."""
    directory = reviews_dir(root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RepoReviewError(f"cannot create {directory}: {exc}") from exc
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    head = str(packet.get("head_commit") or "nohead")[:12]
    text = packet_json(packet)
    path = directory / f"{PACKET_PREFIX}{stamp}-{head}.json"
    atomic_write(path, text)
    latest = directory / LATEST_NAME
    atomic_write(latest, text)
    prune_packets(directory)
    return {"path": str(path), "latest": str(latest)}


def save_compact(root: str | Path, packet: Mapping[str, Any]) -> str:
    directory = reviews_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / COMPACT_NAME
    atomic_write(path, packet_json(compact_packet(packet)))
    return str(path)


def brief(packet: Mapping[str, Any], *, max_findings: int = 10,
          max_chars: int = 240) -> dict[str, Any]:
    """A budgeted summary: counts, graph state, truncation and top findings."""
    findings = list(packet.get("findings") or [])
    top = []
    for f in findings[:max(0, int(max_findings))]:
        message = str(f.get("message") or "")
        if len(message) > max_chars:
            message = message[:max_chars - 3] + "..."
        top.append({"id": f.get("id"), "section": f.get("section"),
                    "type": f.get("type"), "severity": f.get("severity"),
                    "keys": list(f.get("keys") or [])[:5],
                    "message": message})
    graph = packet.get("graph") or {}
    out = {
        "schema": packet.get("schema"),
        "head_commit": packet.get("head_commit"),
        "summary": packet.get("summary"),
        "graph": {k: graph.get(k) for k in ("freshness", "usable", "reason")},
        "truncated": packet.get("truncated"),
        "not_checked": len(packet.get("not_checked") or []),
        "top_findings": top,
        "findings_omitted": max(0, len(findings) - len(top)),
        "truth_assessed": False,
    }
    if packet.get("comparison"):
        comparison = packet["comparison"]
        out["comparison"] = {k: len(comparison.get(k) or [])
                             for k in ("new", "resolved", "unchanged")}
    return out


def format_packet(packet: Mapping[str, Any]) -> str:
    graph = packet.get("graph") or {}
    summary = packet.get("summary") or {}
    lines = [
        f"repo review: {packet.get('root')} at "
        f"{str(packet.get('head_commit') or '')[:12]} "
        f"({packet.get('branch') or 'detached'}), {packet.get('files')} files",
        f"graph: {graph.get('freshness')} ({graph.get('path')})"
        + (f" - {graph.get('reason')}" if graph.get("reason") else ""),
        "findings: " + ", ".join(
            f"{summary.get(s, 0)} {s}" for s in SEVERITIES),
    ]
    for name, section in (packet.get("sections") or {}).items():
        state = section.get("status")
        if section.get("truncated_reason"):
            state += f", truncated by {section['truncated_reason']}"
        lines.append(f"[{name}] {state}: {section.get('findings')} finding(s); "
                     f"checked {', '.join(section.get('checked') or []) or 'nothing'}")
        for finding in packet.get("findings") or []:
            if finding.get("section") != name:
                continue
            lines.append(f"  [{finding['severity']}] {finding['id']} "
                         f"{finding['type']}: {finding['message']}")
            for item in finding.get("evidence") or []:
                lines.append(f"    evidence: {item}")
            if finding.get("confidence_note"):
                lines.append(f"    confidence: {finding['confidence']} - "
                             f"{finding['confidence_note']}")
    comparison = packet.get("comparison")
    if comparison:
        lines.append(
            f"compare: {len(comparison['new'])} new, "
            f"{len(comparison['resolved'])} resolved, "
            f"{len(comparison['unchanged'])} unchanged")
        for item in comparison.get("not_compared") or []:
            lines.append(f"  not compared: {item['section']}.{item['check']} "
                         f"({item['type']}): {item['reason']}")
    lines.append("not checked:")
    for item in packet.get("not_checked") or []:
        lines.append(f"  - {item['section']}.{item['check']}: {item['reason']}")
    model = packet.get("model_review") or {}
    if model.get("status") == "prepared":
        lines.append(f"model pass: compact packet at {model.get('compact_path')}; "
                     "no model was called")
    lines.append(packet.get("security_note") or SECURITY_NOTE)
    lines.append("truth_assessed: false")
    return "\n".join(lines) + "\n"


def format_refusal(report: Mapping[str, Any]) -> str:
    lines = [f"repo review refused: {report.get('reason')}"]
    for check in (report.get("preflight") or {}).get("checks") or []:
        if check.get("level") == "error":
            lines.append(f"  error {check.get('id')}: {check.get('message')}")
    return "\n".join(lines) + "\n"


__all__ = [
    "COMPACT_SCHEMA", "CRITERIA_KINDS", "DEFAULT_BUDGET_S", "FAIL_ON",
    "FINDING_TYPES", "PACKET_SCHEMA", "PreflightError", "RepoReviewError",
    "SCHEMA", "SECTIONS", "SEVERITIES", "atomic_write", "brief",
    "compact_packet", "compare_packets", "exit_code", "finding_id",
    "finding_state", "prune_packets",
    "format_packet", "format_refusal", "load_packet", "packet_json",
    "preflight", "review_repo", "reviews_dir", "run_model_pass",
    "save_compact", "save_packet", "validate_model_output",
]
