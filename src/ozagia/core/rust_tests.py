"""Cargo test targets and the tests they run.

The model is read from ``Cargo.toml`` and Rust sources with
:mod:`ozagia.core.rust_modules`; ``cargo metadata`` is never used and nothing
is built or executed.

Each Cargo target (``lib``, ``bin``, ``test``, ``bench``, ``example``) owns a
module tree. A ``#[test]`` function found in that tree is one entry with:

* ``file`` and ``qualname``: the graph's Test key
  (``test:<file>::<qualname>``), using the qualname local to the file;
* ``cargo``: the name the libtest harness prints
  (``module::path::function``);
* ``targets``: every target whose module tree contains the function.

Doctests are not graph Test nodes; they are represented by a pseudo target
with kind ``doc`` and one invocation per package.
"""
from __future__ import annotations

import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ozagia.core import rust_modules

# Kinds Cargo's default `cargo test` runs (examples are only built).
DEFAULT_KINDS = ("lib", "bin", "test")
ALL_KINDS = ("lib", "bin", "test", "bench", "example")
DOC_KIND = "doc"
DOC_PREFIX = "doc:"


@dataclass(frozen=True)
class TestEntry:
    """One graph Test node and the Cargo target(s) that run it."""

    key: str
    file: str
    qualname: str
    cargo: str
    package: str
    targets: tuple[str, ...]
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "file": self.file,
                "qualname": self.qualname, "cargo": self.cargo,
                "package": self.package, "targets": list(self.targets),
                "line": self.line}


@dataclass
class CargoTests:
    """The Cargo targets, their module files and their tests."""

    root: Path
    workspace: rust_modules.Workspace
    targets: dict[str, rust_modules.CrateTarget] = field(default_factory=dict)
    target_files: dict[str, set[str]] = field(default_factory=dict)
    file_targets: dict[str, set[str]] = field(default_factory=dict)
    target_tests: dict[str, list[TestEntry]] = field(default_factory=dict)
    tests: dict[str, TestEntry] = field(default_factory=dict)
    package_targets: dict[str, set[str]] = field(default_factory=dict)
    package_lib: dict[str, str] = field(default_factory=dict)
    package_manifests: dict[str, str] = field(default_factory=dict)
    package_dirs: dict[str, str] = field(default_factory=dict)
    proc_macro: set[str] = field(default_factory=set)
    package_deps: dict[str, set[str]] = field(default_factory=dict)
    omissions: list[str] = field(default_factory=list)
    _reverse_deps: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set), repr=False)
    # (target id, cargo name) -> entries. Usually one; a file reachable from
    # two module paths of the same target can produce more.
    _by_cargo: dict[tuple[str, str], list[TestEntry]] = field(
        default_factory=lambda: defaultdict(list), repr=False)

    # -- target helpers ---------------------------------------------------- #
    def target(self, target_id: str) -> rust_modules.CrateTarget | None:
        return self.targets.get(target_id)

    def is_doc(self, target_id: str) -> bool:
        return target_id.startswith(DOC_PREFIX)

    def doc_target_id(self, package: str) -> str:
        return f"{DOC_PREFIX}{package}"

    def doc_packages(self) -> list[str]:
        return sorted(self.package_lib)

    def package_of(self, target_id: str) -> str:
        if self.is_doc(target_id):
            return target_id[len(DOC_PREFIX):]
        target = self.targets.get(target_id)
        return target.package if target is not None else ""

    def target_root(self, target_id: str) -> str:
        if self.is_doc(target_id):
            package = self.package_of(target_id)
            return self.package_lib.get(package, "")
        target = self.targets.get(target_id)
        return target.root if target is not None else ""

    def target_kind(self, target_id: str) -> str:
        if self.is_doc(target_id):
            return DOC_KIND
        target = self.targets.get(target_id)
        return target.kind if target is not None else ""

    def target_name(self, target_id: str) -> str:
        if self.is_doc(target_id):
            return self.package_of(target_id)
        target = self.targets.get(target_id)
        return target.name if target is not None else ""

    def package_targets_of(self, package: str, kinds: Iterable[str] = ALL_KINDS
                            ) -> list[str]:
        wanted = set(kinds)
        out = [tid for tid in self.package_targets.get(package, ())
               if self.target_kind(tid) in wanted]
        if DOC_KIND in wanted and package in self.package_lib:
            out.append(self.doc_target_id(package))
        return sorted(out)

    def dependents(self, package: str) -> set[str]:
        """Packages that depend on ``package``, transitively."""
        if not self._reverse_deps:
            for name, deps in self.package_deps.items():
                for dep in deps:
                    self._reverse_deps[dep].add(name)
        out: set[str] = set()
        todo = [package]
        while todo:
            current = todo.pop()
            for name in self._reverse_deps.get(current, ()):
                if name not in out:
                    out.add(name)
                    todo.append(name)
        return out

    def default_targets(self, packages: Iterable[str] | None = None,
                        *, docs: bool = True) -> list[str]:
        """The targets a plain ``cargo test`` runs, in Cargo's order.

        A target with Cargo's ``test = False`` is not run by ``cargo test``,
        so it is not selected (nor its unit tests counted). Doctests stay a
        pseudo target for a package with a library."""
        selected = set(packages) if packages is not None else set(
            self.package_targets)
        out: list[str] = []
        for target in self.targets.values():
            if target.package not in selected:
                continue
            if target.kind not in DEFAULT_KINDS or not target.test:
                continue
            out.append(target.id)
        out.sort(key=self._target_order)
        if docs:
            for package in sorted(selected):
                if package in self.package_lib:
                    out.append(self.doc_target_id(package))
        return out

    def custom_harness(self, target_id: str) -> bool:
        """Whether `target_id` uses a harness other than libtest."""
        if self.is_doc(target_id):
            return False
        target = self.targets.get(target_id)
        return target is not None and not target.harness

    def _target_order(self, target_id: str) -> tuple:
        target = self.targets.get(target_id)
        if target is None:
            return (999, target_id)
        kind_rank = {kind: i for i, kind in enumerate(ALL_KINDS)}
        return (self.package_dirs.get(target.package, ""),
                kind_rank.get(target.kind, 99), target.name, target.root)

    def tests_of(self, target_id: str) -> list[TestEntry]:
        if self.is_doc(target_id):
            return []
        return self.target_tests.get(target_id, [])

    def target_deps(self, target_id: str) -> set[str]:
        """Files whose content can change what this target runs.

        The package's target trees (bins and integration tests link the
        library), a build script, and every workspace dependency package,
        transitively. Manifests, the lock file and the toolchain fingerprint
        are common inputs handled by the caller.
        """
        return self._package_files(self.package_of(target_id))

    def _package_files(self, package: str,
                       seen: set[str] | None = None) -> set[str]:
        if seen is None:
            seen = set()
        if package in seen:
            return set()
        seen.add(package)
        files: set[str] = set()
        for target_id in self.package_targets.get(package, ()):
            files |= self.target_files.get(target_id, set())
        lib = self.package_lib.get(package)
        if lib:
            files.add(lib)
            lib_target = self._lib_target_id(package)
            if lib_target:
                files |= self.target_files.get(lib_target, set())
        files |= self._build_files(package)
        for dependency in self.package_deps.get(package, ()):
            files |= self._package_files(dependency, seen)
        return files

    def _lib_target_id(self, package: str) -> str | None:
        for target in self.targets.values():
            if target.package == package and target.kind == "lib":
                return target.id
        return None

    def _build_files(self, package: str) -> set[str]:
        manifest = self.package_manifests.get(package)
        if not manifest:
            return set()
        base = PurePosixPath(manifest).parent
        build = (base / "build.rs").as_posix()
        out = set()
        if (self.root / build).is_file():
            out.add(build)
        return out

    def locate(self, target_id: str, cargo_name: str) -> TestEntry | None:
        """The Test entry for a cargo-printed name in a target, if known."""
        if self.is_doc(target_id):
            return None
        exact = self._by_cargo.get((target_id, cargo_name))
        if exact:
            return exact[0]
        # Older Cargo versions and `--exact`-filtered output can omit a
        # leading module path; accept a unique suffix match.
        matches = [entry for (tid, name), entries in self._by_cargo.items()
                   if tid == target_id and (name.endswith("::" + cargo_name)
                                            or cargo_name.endswith("::" + name))
                   for entry in entries]
        unique = {entry.key: entry for entry in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def test_keys(self, target_id: str) -> list[str]:
        return [entry.key for entry in self.tests_of(target_id)]

    def summary(self) -> dict[str, Any]:
        return {
            "targets": len(self.targets),
            "packages": sorted(self.package_targets),
            "tests": len(self.tests),
            "proc_macro": sorted(self.proc_macro),
            "omissions": list(self.omissions),
        }


def _proc_macro_packages(root: Path, manifests: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for manifest in sorted(set(manifests)):
        try:
            data = tomllib.loads((root / manifest).read_text(
                encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        package = data.get("package")
        if isinstance(package, Mapping) and package.get("proc-macro") is True:
            name = str(package.get("name") or "")
            if name:
                out.add(name)
    return out


def _manifest_deps(data: Mapping[str, Any], known: set[str]) -> set[str]:
    """Workspace packages this manifest depends on (any dependency kind)."""
    out: set[str] = set()

    def table(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for name, spec in value.items():
            real = name
            if isinstance(spec, Mapping) and spec.get("package"):
                real = str(spec["package"])
            if real in known:
                out.add(str(real))

    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        table(data.get(key))
    targets = data.get("target")
    if isinstance(targets, Mapping):
        for config in targets.values():
            if isinstance(config, Mapping):
                for key in ("dependencies", "dev-dependencies",
                            "build-dependencies"):
                    table(config.get(key))
    return out


def _package_deps(root: Path, model: CargoTests) -> dict[str, set[str]]:
    known = set(model.package_manifests)
    out: dict[str, set[str]] = {}
    for package, manifest in model.package_manifests.items():
        try:
            data = tomllib.loads((root / manifest).read_text(
                encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            out[package] = set()
            continue
        out[package] = _manifest_deps(data, known)
    return out


def _local_qualname(qualname: str, prefix: str) -> str:
    if prefix and (qualname == prefix or qualname.startswith(prefix + ".")):
        return qualname[len(prefix):].lstrip(".")
    return qualname


def discover(root: str | Path) -> CargoTests:
    """Read the Cargo workspace and map every ``#[test]`` to its targets."""
    root = Path(root).resolve()
    workspace = rust_modules.discover(root)
    model = CargoTests(root=root, workspace=workspace,
                       omissions=list(workspace.not_checked))
    for target in workspace.targets.values():
        model.targets[target.id] = target
        model.package_targets.setdefault(target.package, set()).add(target.id)
        model.package_manifests.setdefault(target.package, target.manifest)
        model.package_dirs[target.package] = PurePosixPath(
            target.manifest).parent.as_posix().removeprefix(".")
        if target.kind == "lib":
            model.package_lib.setdefault(target.package, target.root)

    for rel, entries in workspace.file_entries.items():
        for entry in entries:
            target = entry.target
            model.target_files.setdefault(target.id, set()).add(rel)
            model.file_targets.setdefault(rel, set()).add(target.id)

    for rel, parsed in workspace.parsed_files.items():
        entries = workspace.file_entries.get(rel) or []
        for symbol in parsed.get("symbols") or []:
            if not symbol.get("is_test"):
                continue
            qualname = str(symbol.get("qualname") or symbol.get("name") or "")
            if not qualname:
                continue
            key = f"test:{rel}::{qualname}"
            for entry in entries:
                local = _local_qualname(qualname, entry.prefix)
                parts = [part for part in local.split(".") if part]
                cargo = "::".join((*entry.module, *parts))
                if not cargo:
                    continue
                test = TestEntry(
                    key=key, file=rel, qualname=qualname, cargo=cargo,
                    package=entry.target.package,
                    targets=(entry.target.id,),
                    line=int(symbol.get("start_line") or 0))
                model._by_cargo[(entry.target.id, cargo)].append(test)
                model.target_tests.setdefault(entry.target.id, []).append(test)
                model.tests.setdefault(key, test)

    targets_by_key: dict[str, set[str]] = defaultdict(set)
    for (target_id, _name), entries in model._by_cargo.items():
        for entry in entries:
            targets_by_key[entry.key].add(target_id)
    for target_id, tests in model.target_tests.items():
        unique: dict[str, TestEntry] = {}
        for test in tests:
            unique.setdefault(test.key, test)
        model.target_tests[target_id] = sorted(
            unique.values(), key=lambda t: (t.file, t.line, t.cargo))
    for key, test in list(model.tests.items()):
        model.tests[key] = TestEntry(
            key=test.key, file=test.file, qualname=test.qualname,
            cargo=test.cargo, package=test.package,
            targets=tuple(sorted(targets_by_key.get(key, ()))),
            line=test.line)
    model.proc_macro = _proc_macro_packages(
        root, (t.manifest for t in model.targets.values()))
    model.package_deps = _package_deps(root, model)
    return model


def package_of_file(model: CargoTests, rel: str) -> set[str]:
    """Packages whose targets contain ``rel`` (empty when unrelated)."""
    return {model.package_of(tid) for tid in model.file_targets.get(rel, ())}


__all__ = [
    "ALL_KINDS",
    "CargoTests",
    "DEFAULT_KINDS",
    "DOC_KIND",
    "DOC_PREFIX",
    "TestEntry",
    "discover",
    "package_of_file",
]
