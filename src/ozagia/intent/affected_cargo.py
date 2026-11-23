"""Affected-target selection and a target-result cache for a captured
``cargo test``.

The settings are the pytest ones (``[tests] select`` and ``cache``, or
``testing.captured``): selection stays off by default, and
``OZAGIA_TESTS_ALL=1`` always runs the command as typed.

A suite is the cargo executable, toolchain, selected packages, feature and
profile options, and the environment. For each suite the state keeps the last
fully green run of the command as typed: the git HEAD, the hashes of the
files that were dirty then, the Cargo manifests and lock file, the cargo and
rustc versions, and per-target test counts and cache keys.

Changed files are mapped to Cargo targets with
:mod:`ozagia.core.rust_tests`. A change in a package's library or binary
selects every default test target of that package; a change in an integration
test or its helper modules selects only those test targets. Doctests are a
pseudo target per package. A narrowed run names its targets explicitly
(``cargo test -p <package> --test <name>``), so only linked targets run.

Everything runs as typed when the model cannot decide: no green run, a
changed manifest/lock file/toolchain/build script/non-Rust file, a proc-macro
crate, a changed Rust file outside every target, a changed example/bench
target, no file changed, too many files, or the command itself selects
targets or tests. The output then says ``[ozagia] ran all tests: <reason>``.

Only a run of the command as typed that exits 0 and leaves the tree and
toolchain unchanged moves the green run forward. A failure is never cached:
the failing target's key is removed. A target is served from the cache only
when its key matches and every test in it passed or was ignored.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ozagia.config import state_rel
from ozagia.core import rust_tests
from ozagia.intent import affected as A

SLOW_TARGET_S = A.SLOW_S
_SELECTION_VALUE = frozenset({"-p", "--package", "--exclude", "--test",
                              "--bin", "--bench", "--example"})
_SELECTION_FLAGS = frozenset({"--workspace", "--all", "--lib", "--bins",
                              "--tests", "--benches", "--examples",
                              "--all-targets", "--doc"})
_TARGET_FLAGS = ("lib", "bins", "tests", "benches", "examples",
                 "all_targets", "doc")
_CFG_NAMES = frozenset({"Cargo.toml", "Cargo.lock"})
_TOOLCHAIN_PREFIXES = ("rust-toolchain",)
_CARGO_FP: dict[tuple[str, int, str], str | None] = {}


@dataclass
class CargoPlan:
    """What a captured cargo test runs, and what to record after it."""

    root: Path
    select: str
    cache: bool
    suite: str
    run_all_cmd: str
    early_stop: bool
    # None: the command as typed.
    runs: list[list[str]] | None = None
    reason: str | None = None
    forced: bool = False
    selected: list[str] = field(default_factory=list)
    not_run: dict[str, int] = field(default_factory=dict)
    cached: dict[str, dict[str, int]] = field(default_factory=dict)
    cached_since: dict[str, float] = field(default_factory=dict)
    keys: dict[str, str | None] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    model: rust_tests.CargoTests | None = None
    cargo: str = ""
    env_hash: str = ""
    features: str = ""
    manifests: str = ""
    typed: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] | None = None
    can_be_green: bool = True

    @property
    def nothing_to_run(self) -> bool:
        return bool(self.runs is not None and not self.runs and self.cached)

    @property
    def full(self) -> bool:
        return self.runs is None


def _features(cls: dict[str, Any]) -> str:
    return json.dumps({
        "features": sorted(str(f) for f in cls.get("features") or []),
        "all_features": bool(cls.get("all_features")),
        "no_default_features": bool(cls.get("no_default_features")),
        "release": bool(cls.get("release")),
        "profile": cls.get("profile"),
        "target": cls.get("target_triple"),
        "toolchain": cls.get("toolchain"),
        "extra": [str(x) for x in cls.get("extra_args") or []],
    }, sort_keys=True)


def _cargo_fingerprint(cargo: str, toolchain: str | None,
                       env: dict[str, str]) -> str | None:
    """cargo and rustc versions for the toolchain, or None."""
    try:
        real = os.path.realpath(cargo)
        mtime = os.stat(real).st_mtime_ns
    except OSError:
        return None
    key = (real, mtime, toolchain or "")
    hit = _CARGO_FP.get(key)
    if hit is None and key not in _CARGO_FP:
        def version(command: str) -> str:
            argv = [command]
            if toolchain:
                argv.append("+" + toolchain)
            argv.append("--version")
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=20,
                    stdin=subprocess.DEVNULL, env=env, check=False)
            except (OSError, subprocess.SubprocessError):
                return ""
            return proc.stdout.strip() if proc.returncode == 0 else ""
        rustc = shutil.which("rustc", path=env.get("PATH"))
        rows = [version(real), version(rustc) if rustc else "",
                toolchain or "", env.get("RUSTUP_TOOLCHAIN", "")]
        hit = hashlib.sha256("\0".join(rows).encode("utf-8", "surrogateescape")
                             ).hexdigest()
        _CARGO_FP[key] = hit
    return hit


def _manifest_hash(root: Path, files: list[str] | None,
                   model: rust_tests.CargoTests) -> str | None:
    candidates = set(files or ())
    candidates.update(model.package_manifests.values())
    candidates.update({"Cargo.toml", "Cargo.lock",
                       ".cargo/config", ".cargo/config.toml"})
    files = sorted(candidates)
    h = hashlib.sha256()
    found = False
    for rel in sorted(files):
        name = PurePosixPath(rel).name
        path = PurePosixPath(rel)
        if (name not in _CFG_NAMES and not name.startswith(_TOOLCHAIN_PREFIXES)
                and not (path.parts[:1] == (".cargo",)
                         and name.startswith("config"))):
            continue
        found = True
        sha = A._sha(root / rel)
        h.update(f"{rel}\0{sha or '-'}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest() if found else None


def _base_args(cls: dict[str, Any]) -> list[str]:
    """The typed cargo arguments without package/target selection."""
    args = list(cls.get("args") or [])
    out: list[str] = []
    i = 0
    if i < len(args) and args[i].startswith("+"):
        i += 1
    if i < len(args) and args[i] == "test":
        i += 1
    while i < len(args):
        tok = args[i]
        name = tok.split("=", 1)[0]
        if tok == "--":
            out.extend(args[i:])
            break
        if tok in _SELECTION_VALUE:
            i += 2
            continue
        if name in _SELECTION_VALUE and name != tok:
            i += 1
            continue
        if tok in _SELECTION_FLAGS:
            i += 1
            continue
        if tok.startswith("-p") and len(tok) > 2:
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _target_flag(model: rust_tests.CargoTests, target_id: str) -> list[str] | None:
    kind = model.target_kind(target_id)
    name = model.target_name(target_id)
    if kind == "lib":
        return ["--lib"]
    if kind == "bin":
        return ["--bin", name]
    if kind == "test":
        return ["--test", name]
    if kind == "bench":
        return ["--bench", name]
    if kind == "example":
        return ["--example", name]
    if kind == "doc":
        return ["--doc"]
    return None


def _invocations(model: rust_tests.CargoTests, cls: dict[str, Any],
                 selected: Iterable[str]) -> list[list[str]]:
    """One cargo invocation per package that names the selected targets."""
    base = _base_args(cls)
    prefix = ["+" + cls["toolchain"]] if cls.get("toolchain") else []
    by_package: dict[str, list[str]] = {}
    for tid in selected:
        by_package.setdefault(model.package_of(tid), []).append(tid)
    runs: list[list[str]] = []
    for package in sorted(by_package):
        chosen = set(by_package[package])
        defaults = set(model.default_targets([package]))
        args = [*prefix, "test", *base, "-p", package]
        if chosen != defaults:
            for tid in sorted(chosen, key=model._target_order):
                flag = _target_flag(model, tid)
                if flag:
                    args.extend(flag)
        runs.append(args)
    return runs


def _candidate_targets(model: rust_tests.CargoTests,
                       packages: set[str]) -> list[str]:
    return [tid for tid in model.default_targets(packages)
            if tid in model.targets or model.is_doc(tid)]


def _target_label(model: rust_tests.CargoTests, target_id: str) -> str:
    kind = model.target_kind(target_id)
    package = model.package_of(target_id)
    if kind == "doc":
        return f"-p {package} --doc"
    if kind == "lib":
        return f"-p {package} --lib"
    return f"-p {package} --{kind} {model.target_name(target_id)}"


def _suite_id(cargo: str, packages: list[str], features: str,
              env_hash: str) -> str:
    blob = json.dumps([os.path.realpath(cargo), sorted(packages), features,
                       env_hash])
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _target_key(plan: CargoPlan, target_id: str, common: str) -> str | None:
    model = plan.model
    if model is None:
        return None
    h = hashlib.sha256(common.encode())
    h.update(f"{target_id}\n".encode())
    for rel in sorted(model.target_deps(target_id)):
        sha = A._sha(plan.root / rel)
        if sha is None:
            return None
        h.update(f"{rel}\0{sha}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _common(plan: CargoPlan) -> str | None:
    if not plan.cargo or not plan.env_hash or not plan.manifests:
        return None
    return json.dumps([A.VERSION, plan.cargo, plan.manifests, plan.features,
                       plan.env_hash])


def _snapshot(plan: CargoPlan, common: str) -> dict[str, Any] | None:
    head = A._head(plan.root)
    if head is None:
        return None
    dirty = A._dirty_since(plan.root, head)
    if dirty is None:
        return None
    srel = state_rel(plan.root)
    rels = sorted(r for r in set(dirty[0]) | set(dirty[1])
                  if not A._excluded(r, srel))
    if len(rels) > A.MAX_CHANGED or len(dirty[1]) > A.MAX_UNTRACKED:
        return None
    return {"head": head, "dirty": {r: A._sha(plan.root / r) for r in rels},
            "cargo": plan.cargo, "manifests": plan.manifests,
            "features": plan.features, "env": plan.env_hash,
            "common": common}


def _matches_member(package_dir: str, patterns: Iterable[Any]) -> bool:
    import fnmatch
    path = package_dir.strip().rstrip("/")
    for raw in patterns:
        pattern = str(raw).strip().rstrip("/")
        if pattern.endswith("/Cargo.toml"):
            pattern = pattern[: -len("/Cargo.toml")].rstrip("/")
        if not pattern:
            continue
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatchcase(path, pattern):
                return True
        elif path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def _workspace_scope(root: Path, model: rust_tests.CargoTests,
                     cls: dict[str, Any]) -> set[str] | None:
    """The packages a plain `cargo test` at `root` selects, or None."""
    try:
        data = tomllib.loads((root / "Cargo.toml").read_text(
            encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    package = data.get("package") if isinstance(data.get("package"),
                                                Mapping) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"),
                                                    Mapping) else {}
    known = set(model.package_targets)
    root_package = str(package.get("name") or "")
    by_dir = {model.package_dirs[p]: p for p in model.package_dirs}

    def from_patterns(patterns: Iterable[Any]) -> set[str] | None:
        found: set[str] = set()
        for directory, name in by_dir.items():
            if _matches_member(directory, patterns):
                found.add(name)
        return found

    members: set[str]
    raw_members = workspace.get("members")
    if isinstance(raw_members, list) and raw_members:
        got = from_patterns(raw_members)
        if got is None:
            return None
        members = got
        if not members:
            return None
    else:
        members = set(known)
    raw_excludes = workspace.get("exclude")
    if isinstance(raw_excludes, list) and raw_excludes:
        excluded = from_patterns(raw_excludes)
        if excluded:
            members -= excluded
    if root_package:
        members.add(root_package)
    members &= known
    if not members:
        return None

    raw_default = workspace.get("default-members")
    if isinstance(raw_default, list) and raw_default and not (
            cls.get("workspace")):
        default = from_patterns(raw_default)
        if default:
            return default & members
    if package and not cls.get("workspace"):
        # A root package with a workspace: plain `cargo test` tests the root
        # package, not every member.
        return {root_package} & members
    return members


def _typed_packages(root: Path, model: rust_tests.CargoTests,
                    cls: dict[str, Any]) -> set[str] | None:
    scope = _workspace_scope(root, model, cls)
    if scope is None:
        return None
    packages = [str(p) for p in cls.get("packages") or []]
    if packages:
        selected = set(packages)
        if not selected <= scope:
            return None
    else:
        selected = set(scope)
    excludes = [str(p) for p in cls.get("excludes") or []]
    if excludes:
        if not (cls.get("workspace") or packages):
            return None
        if any(p not in scope for p in excludes):
            return None
        selected -= set(excludes)
    return selected


def _target_scoped(cls: dict[str, Any]) -> bool:
    return (any(cls.get(k) for k in _TARGET_FLAGS)
            or any(cls.get(k) for k in ("test_targets", "bin_targets",
                                        "bench_targets", "example_targets")))


def _as_typed(p: CargoPlan, reason: str | None, *,
              keep_keys: bool) -> None:
    p.runs = None
    p.reason = reason
    p.forced = p.select == "affected"
    p.selected, p.not_run = [], {}
    p.cached, p.cached_since = {}, {}
    if not keep_keys:
        p.keys = {}


def plan(memory: Any, cls: dict[str, Any], inp: dict[str, Any], *,
         root: Path, wd: Path, cargo: str) -> CargoPlan | None:
    """The plan for a captured cargo test, or None when neither feature is on
    or the command is one this module does not change."""
    select, cache = A._settings(root)
    if select == "all" and not cache:
        return None
    if not cls.get("selection_safe", False):
        return None
    if cls.get("no_run") or _target_scoped(cls):
        return None
    if wd != root:
        return None
    manifest = cls.get("manifest_path")
    if manifest:
        candidate = Path(str(manifest))
        candidate = (candidate if candidate.is_absolute() else wd / candidate)
        try:
            same = candidate.resolve() == (root / "Cargo.toml").resolve()
        except OSError:
            same = False
        if not same:
            return None
    if not (root / "Cargo.toml").is_file():
        return None
    budget = A._plan_budget(inp)
    deadline = time.monotonic() + budget
    try:
        A._check_time(deadline)
        model = rust_tests.discover(root)
        A._check_time(deadline)
    except A._OutOfTime:
        return None
    except Exception:  # noqa: BLE001 - run as typed when the model fails
        return None
    if not model.targets:
        return None
    packages = _typed_packages(root, model, cls)
    if packages is None:
        return None
    candidates = _candidate_targets(model, packages)
    if not candidates:
        return None
    eff_env = dict(inp.get("_test_env") if isinstance(
        inp.get("_test_env"), dict) else os.environ)
    env_hash = A._env_hash(eff_env)
    features = _features(cls)
    p = CargoPlan(
        root=root, select=select, cache=cache,
        suite=_suite_id(cargo, sorted(packages), features, env_hash),
        run_all_cmd=A._run_all_cmd(inp, cls),
        early_stop=not bool(cls.get("no_fail_fast")
                            or cls.get("keep_going")),
        cargo=cargo, env_hash=env_hash, features=features,
        typed=list(cls.get("args") or []))
    p.model = model
    p.state = A.load_state(root)

    def typed(reason: str | None, *, keep: bool = False) -> CargoPlan:
        _as_typed(p, reason, keep_keys=keep)
        return p

    # A custom test harness may print anything: its output is not libtest,
    # so keep the cache/selection from narrowing or counting it.
    if any(model.custom_harness(tid) for tid in candidates):
        p.can_be_green = False
        return typed("a target uses a custom test harness")

    try:
        p.manifests = _manifest_hash(
            root, A._git_files(root, state_rel(root)), model) or ""
        p.cargo = _cargo_fingerprint(cargo, cls.get("toolchain"), eff_env) or ""
        common = _common(p)
        if common is None:
            return typed("cannot fingerprint the Cargo toolchain or manifests")
        p.keys = {tid: _target_key(p, tid, common) for tid in candidates}
        A._check_time(deadline)
        _plan(p, model, cls, inp, candidates, eff_env, common, deadline)
    except A._OutOfTime:
        return typed(f"planning took longer than {budget:g} s")
    if p.runs is not None:
        chars = sum(len(a) + 1 for run in p.runs for a in run)
        if chars > A.MAX_SELECTOR_CHARS:
            return typed("too many Cargo targets to name on the command line")
    if p.runs is None and p.can_be_green:
        p.snapshot = _snapshot(p, common)
    return p


def _plan(p: CargoPlan, model: rust_tests.CargoTests, cls: dict[str, Any],
          inp: dict[str, Any], candidates: list[str],
          eff_env: dict[str, str], common: str, deadline: float) -> None:
    root, select, cache, state = p.root, p.select, p.cache, p.state
    srel = state_rel(root)
    A._check_time(deadline)
    top = A._below_toplevel(root)
    if top is not None:
        p.can_be_green = False
        return _as_typed(
            p, f"the root is below the git top level {top}, so changes "
               "outside it are not seen", keep_keys=False)
    base = state["cargo"].get(p.suite)

    def everything(reason: str) -> None:
        _as_typed(p, reason, keep_keys=True)

    if cls.get("env") == {A.ENV_ALL: "1"} or eff_env.get(A.ENV_ALL) == "1":
        return everything(f"{A.ENV_ALL}=1")
    if base is None:
        return everything("no fully green run of this suite recorded yet")
    head = base.get("head")
    if not isinstance(head, str) or A._git(
            root, "cat-file", "-e", f"{head}^{{commit}}") is None:
        return everything("the commit of the last green run is not available")
    if base.get("cargo") != p.cargo:
        return everything("the Cargo toolchain changed since the last green run")
    if base.get("manifests") != p.manifests:
        return everything("Cargo manifests or the lock file changed since the "
                          "last green run")
    if base.get("features") != p.features:
        return everything("the feature or profile options changed since the "
                          "last green run")
    if base.get("env") != p.env_hash:
        return everything("the environment changed since the last green run")
    A._check_time(deadline)
    dirty = A._dirty_since(root, head)
    if dirty is None:
        return everything("git cannot list the changed files")
    tracked, untracked = dirty
    if len(untracked) > A.MAX_UNTRACKED:
        return everything(f"more than {A.MAX_UNTRACKED} untracked files")
    recorded = base.get("dirty") or {}
    changed: list[str] = []
    for rel in sorted(set(tracked) | set(untracked) | set(recorded)):
        if A._excluded(rel, srel):
            continue
        now = A._sha(root / rel)
        if rel in recorded and recorded[rel] == now:
            continue
        changed.append(rel)
    if len(changed) > A.MAX_CHANGED:
        return everything(f"{len(changed)} files changed since the last "
                          "green run")
    for rel in changed:
        name = PurePosixPath(rel).name
        if name in _CFG_NAMES or name.startswith(_TOOLCHAIN_PREFIXES) or (
                PurePosixPath(rel).parts[:1] == (".cargo",)
                and name.startswith("config")):
            return everything(f"Cargo configuration changed: {rel}")
        if name == "build.rs":
            return everything(f"build script changed: {rel}")
        if not rel.endswith(".rs"):
            return everything(f"non-Rust file changed: {rel}")
        if rel not in model.file_targets:
            return everything(f"changed Rust file is not in a Cargo target: {rel}")
        kinds = {model.target_kind(tid) for tid in model.file_targets[rel]}
        if kinds & {"bench", "example"}:
            return everything(f"a changed {sorted(kinds & {'bench', 'example'})[0]} "
                              f"target is not run by the default command: {rel}")
        for package in rust_tests.package_of_file(model, rel):
            if package in model.proc_macro:
                return everything(f"proc-macro crate changed: {package}")

    affected: set[str] = set()
    for rel in changed:
        tids = model.file_targets.get(rel) or set()
        kinds = {model.target_kind(tid) for tid in tids}
        if kinds <= {"test", "bench", "example"}:
            affected |= set(tids)
        else:
            for package in {model.package_of(tid) for tid in tids}:
                for name in {package} | model.dependents(package):
                    affected |= set(model.package_targets_of(name))
                    if name in model.package_lib:
                        affected.add(model.doc_target_id(name))
    affected &= set(candidates)

    if select == "all" or not affected:
        if select == "affected":
            reason = ("no files changed since the last green run"
                      if not changed else
                      "no Cargo test target is affected by the changes")
            if not cache:
                return everything(reason)
            p.forced, p.reason = True, reason
        affected = set(candidates)

    p.selected = [tid for tid in candidates if tid in affected]
    green = base.get("targets") or {}
    p.not_run = {tid: int(green.get(tid) or 0) for tid in candidates
                 if tid not in affected and int(green.get(tid) or 0) > 0}
    if (select == "affected" and not p.forced and not p.reason
            and set(affected) == set(candidates)):
        p.reason = "every Cargo test target is affected by the changes"
    if cache:
        _apply_cache(p)
    else:
        p.runs = _invocations(model, cls, p.selected)
    # Every candidate runs, whole, and none is cached: that is the command as
    # typed, so run it as typed (a full run; the green run can move).
    if (p.runs is not None and not p.cached and not p.not_run
            and not p.reason):
        _as_typed(p, None, keep_keys=True)
    return None


def _apply_cache(p: CargoPlan) -> None:
    suite = p.state["cargo"].get(p.suite) or {}
    keys = suite.get("keys") or {}
    totals = suite.get("targets") or {}
    passed = suite.get("passed") or {}
    ignored = suite.get("ignored") or {}
    run: list[str] = []
    for tid in p.selected:
        key = p.keys.get(tid)
        total = totals.get(tid)
        if (p.cache and key is not None and keys.get(tid) == key
                and isinstance(total, int)
                and int(passed.get(tid) or 0) + int(ignored.get(tid) or 0)
                == total):
            p.cached[tid] = {"passed": int(passed.get(tid) or 0),
                             "ignored": int(ignored.get(tid) or 0)}
            p.cached_since[tid] = float(suite.get("t") or 0)
        else:
            run.append(tid)
    p.runs = _invocations(p.model, _cls_for(p), run) if p.model else []
    if not run:
        p.runs = []


def _cls_for(p: CargoPlan) -> dict[str, Any]:
    """The parts of the typed command the invocations keep."""
    return {"toolchain": p.typed[0][1:] if p.typed and
            p.typed[0].startswith("+") else None,
            "args": p.typed}


def _record(p: CargoPlan, res: dict[str, Any] | None, *,
            full: bool) -> list[str]:
    """Store the run's per-target outcomes; return slow-target lines."""
    from ozagia.intent.common import tag
    lines: list[str] = []
    if res is None or p.model is None:
        return lines
    now = time.time()
    model = p.model
    blocks = [b for b in res.get("blocks") or []
              if b.get("target") and b.get("harness", True)]
    by_target: dict[str, dict[str, int]] = {}
    for block in blocks:
        tid = str(block.get("target") or "")
        if not tid:
            continue
        counts = block.get("counts") or {}
        row = by_target.setdefault(tid, {"passed": 0, "ignored": 0,
                                         "failed": 0, "total": 0})
        row["passed"] += int(counts.get("passed") or 0)
        row["ignored"] += int(counts.get("ignored") or 0)
        row["failed"] += int(counts.get("failed") or 0)
        row["total"] += (int(counts.get("passed") or 0)
                         + int(counts.get("ignored") or 0)
                         + int(counts.get("failed") or 0))
    passed_as_typed = (full and res.get("exit_code") == 0
                       and res.get("status") == "passed")
    green: dict[str, Any] | None = None
    if passed_as_typed and p.snapshot is not None:
        common = _common(p)
        after = _snapshot(p, common) if common else None
        if after == p.snapshot:
            green = after
        else:
            lines.append(tag("green run not recorded: files or the Cargo "
                             "toolchain changed during the run"))
    with A._locked(p.root) as data:
        suite = data["cargo"].get(p.suite)
        if green is not None:
            data["cargo"][p.suite] = {
                **green,
                "targets": {tid: row["total"]
                            for tid, row in by_target.items()},
                "passed": {tid: row["passed"]
                           for tid, row in by_target.items()},
                "ignored": {tid: row["ignored"]
                            for tid, row in by_target.items()},
                "keys": {tid: p.keys[tid] for tid in by_target
                         if p.keys.get(tid)},
                "t": now,
            }
        elif suite is not None:
            keys = dict(suite.get("keys") or {})
            totals = dict(suite.get("targets") or {})
            passes = dict(suite.get("passed") or {})
            ignores = dict(suite.get("ignored") or {})
            for tid, row in by_target.items():
                if row["failed"]:
                    keys.pop(tid, None)
                    totals.pop(tid, None)
                    passes.pop(tid, None)
                    ignores.pop(tid, None)
                else:
                    key = p.keys.get(tid)
                    if key is not None:
                        keys[tid] = key
                    totals[tid] = row["total"]
                    passes[tid] = row["passed"]
                    ignores[tid] = row["ignored"]
            suite.update({"keys": keys, "targets": totals,
                          "passed": passes, "ignored": ignores, "t": now})
    for block in blocks:
        seconds = float(block.get("seconds") or 0)
        if seconds > SLOW_TARGET_S:
            lines.append(tag(
                f"slow target (> {SLOW_TARGET_S:g} s): "
                f"{_target_label(model, str(block['target']))} "
                f"{seconds:.1f} s"))
    return lines


def _tally(p: CargoPlan, res: dict[str, Any] | None
           ) -> tuple[int, int, int, int]:
    counts = (res or {}).get("counts") or {}
    ran = (int(counts.get("passed") or 0) + int(counts.get("failed") or 0)
           + int(counts.get("ignored") or 0))
    cached_p = sum(c.get("passed", 0) for c in p.cached.values())
    cached_s = sum(c.get("ignored", 0) for c in p.cached.values())
    return ran, cached_p, cached_s, sum(p.not_run.values())


def scope(p: CargoPlan | None, res: dict[str, Any] | None) -> str:
    """The head line's scope, or "" for a run of the command as typed."""
    if p is None or p.runs is None:
        return ""
    ran, cached_p, cached_s, not_run = _tally(p, res)
    kind = ("affected" if p.select == "affected" and not p.forced
            else "cached")
    total = ran + cached_p + cached_s + not_run
    parts = [f"{ran} of {total} tests run"]
    if cached_p or cached_s:
        parts.append(f"{cached_p + cached_s} cached")
    if not_run:
        parts.append(f"{not_run} not run")
    return f" ({kind} subset: {', '.join(parts)})"


def finish(p: CargoPlan, res: dict[str, Any] | None
           ) -> tuple[list[str], list[str], dict[str, Any]]:
    """(lines after the cargo head, lines at the end, meta)."""
    from ozagia.intent.common import tag
    head: list[str] = []
    counts = {"affected_runs": 0, "tests_skipped_unaffected": 0,
              "cached_passes": 0, "full_runs_forced": 0,
              "cargo_targets_run": 0}
    full = p.full
    if full:
        if p.forced:
            counts["full_runs_forced"] = 1
        if p.select == "affected" or p.reason:
            head.append(tag(f"ran all tests: {p.reason}"))
    else:
        ran, cached_p, cached_s, not_run = _tally(p, res)
        total = ran + cached_p + cached_s + not_run
        word = ("affected tests" if p.select == "affected" and not p.forced
                else "tests")
        parts = [f"ran {ran} {word} of {total}"]
        if cached_p or cached_s:
            parts.append(f"cached: {cached_p} passed"
                         + (f", {cached_s} ignored" if cached_s else ""))
        if not_run:
            nt = len(p.not_run)
            targets = ", ".join(
                _target_label(p.model, tid) if p.model else tid
                for tid in sorted(p.not_run)[:A.MAX_LISTED])
            parts.append(f"not run: {not_run} test{'s' * (not_run != 1)} in "
                         f"{nt} target{'s' * (nt != 1)} ({targets})")
        parts.append(f"run all: {p.run_all_cmd}")
        head.append(tag("; ".join(parts)))
        if p.forced and p.reason:
            head.append(tag(f"selection off for this run: {p.reason}"))
        shown = [tid for tid in sorted(p.cached)
                 if p.cached[tid]["passed"] or p.cached[tid]["ignored"]]
        for tid in shown[:A.MAX_CACHED_LINES]:
            cached = p.cached[tid]
            since = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(p.cached_since.get(tid) or 0))
            label = _target_label(p.model, tid) if p.model else tid
            head.append(tag(
                f"cached pass {label}: {cached['passed']} passed"
                + (f", {cached['ignored']} ignored" if cached["ignored"] else "")
                + f" (inputs unchanged since {since})"))
        if len(shown) > A.MAX_CACHED_LINES:
            head.append(tag(f"cached pass: "
                            f"+{len(shown) - A.MAX_CACHED_LINES} more "
                            "targets"))
        if p.select == "affected" and not p.forced:
            counts["affected_runs"] = 1
        counts["tests_skipped_unaffected"] = not_run
        counts["cached_passes"] = cached_p
    targets_run = {str(b.get("target")) for b in (res or {}).get(
        "blocks") or [] if b.get("target")}
    counts["cargo_targets_run"] = len(targets_run)
    try:
        tail = _record(p, res, full=full)
    except Exception as e:  # noqa: BLE001 - the run's own lines still stand
        tail = [tag(f"warning: Cargo test results not stored: {e}")]
    return head, tail, {"tests": counts}


__all__ = ["CargoPlan", "finish", "plan", "scope"]
