"""Cargo workspace and Rust module/import resolution.

The model is read from `Cargo.toml` and source files only; `cargo metadata`,
builds and network access are never used. It maps:

* package/lib/binary/test/bench roots to crate targets;
* `crate`, `self`, `super` and workspace crate paths to module files;
* inline modules to a file plus a qualname prefix;
* `use` records produced by :mod:`ozagia.index.rust_symbols` to a target file
  and symbol prefix, resolving aliases, groups, globs and one-level re-exports.

External crates are reported as external, never guessed from a same-named local
symbol. Ambiguous module paths and missing files are reported as unresolved.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ozagia.config import GENERIC_SKIP_DIRS
from ozagia.index.rust_symbols import parse_rust

MAX_MANIFESTS = 2_000
MAX_MODULE_FILES = 10_000
MAX_DEPTH = 64
MAX_REEXPORT_DEPTH = 3
_SKIP_DIRS = frozenset(GENERIC_SKIP_DIRS)
_CRATE_ROOT_FILES = frozenset({"lib.rs", "main.rs", "build.rs"})
_TEST_ROOT_DIRS = frozenset({"tests", "benches", "examples"})


@dataclass(frozen=True)
class CrateTarget:
    id: str
    package: str
    name: str
    kind: str
    root: str
    manifest: str


@dataclass(frozen=True)
class ModuleEntry:
    target: CrateTarget
    file: str
    module: tuple[str, ...]
    prefix: str


@dataclass
class Workspace:
    root: Path
    targets: dict[str, CrateTarget] = field(default_factory=dict)
    crates: dict[str, CrateTarget] = field(default_factory=dict)
    modules: dict[tuple[str, tuple[str, ...]], ModuleEntry] = field(
        default_factory=dict)
    file_entries: dict[str, list[ModuleEntry]] = field(default_factory=dict)
    parsed_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    pub_bindings: dict[tuple[str, str, str], Mapping[str, Any]] = field(
        default_factory=dict)
    ambiguous: dict[str, str] = field(default_factory=dict)
    not_checked: list[str] = field(default_factory=list)
    files_scanned: int = 0

    # -- contexts ---------------------------------------------------------- #
    def contexts(self, rel: str, scope: str = "") -> list[ModuleEntry]:
        entries = self.file_entries.get(rel) or []
        if not entries:
            return []
        scope = str(scope or "")
        matches = [entry for entry in entries
                   if not entry.prefix or scope == entry.prefix
                   or scope.startswith(entry.prefix + ".")]
        if not matches:
            root = [entry for entry in entries if not entry.prefix]
            return root[:1]
        longest = max(len(entry.prefix) for entry in matches)
        return [entry for entry in matches if len(entry.prefix) == longest]

    def context(self, rel: str, scope: str = "") -> ModuleEntry | None:
        matches = self.contexts(rel, scope)
        return matches[0] if len(matches) == 1 else None

    # -- import resolution ------------------------------------------------- #
    def resolve(self, rel: str, tag: Mapping[str, Any], *,
                depth: int = 0) -> dict[str, Any]:
        """Resolve one `use`-style record to a file/qualname or an honest gap."""
        module = str(tag.get("module") or "")
        imported = str(tag.get("imported") or "")
        scope = str(tag.get("scope") or "")
        contexts = self.contexts(rel, scope)
        if not contexts:
            return {"status": "unresolved", "reason": "no module context",
                    "module": module, "imported": imported}
        results = [self._resolve_in_context(entry, module, imported, depth)
                   for entry in contexts]
        resolved = [item for item in results if item.get("status") == "resolved"]
        if len(resolved) == 1:
            return resolved[0]
        if len(resolved) > 1:
            unique = {(item["file"], item["qualname"]) for item in resolved}
            if len(unique) == 1:
                return resolved[0]
            return {"status": "ambiguous", "results": resolved,
                    "module": module, "imported": imported}
        if any(item.get("status") == "external" for item in results):
            return next(item for item in results
                        if item.get("status") == "external")
        return results[0]

    def _resolve_in_context(self, context: ModuleEntry, module: str,
                            imported: str, depth: int) -> dict[str, Any]:
        parts = [part for part in module.split("::") if part]
        target = context.target
        base = list(context.module)
        if parts and parts[0] == "crate":
            parts = parts[1:]
        elif parts and parts[0] == "self":
            parts = parts[1:]
        elif parts and parts[0] == "super":
            while parts and parts[0] == "super":
                if not base:
                    return {"status": "unresolved",
                            "reason": "super escapes the crate root"}
                base.pop()
                parts = parts[1:]
        elif parts:
            first = parts[0]
            if (target.id, tuple(base) + (first,)) in self.modules:
                # A child module of the current module (`inner::go`).
                pass
            elif (target.id, (first,)) in self.modules:
                base = []
            elif first in self.crates:
                target = self.crates[first]
                base = []
                parts = parts[1:]
            elif first[:1].isupper():
                # A relative type path such as `Foo::bar`; leave it to local
                # scope/method resolution rather than calling it external.
                return {"status": "unresolved",
                        "reason": "relative type path"}
            else:
                return {"status": "external", "crate": first}
        module_path = tuple(base) + tuple(parts)
        if not module_path and not parts:
            # `use crate;` or a pathless import: the crate root.
            module_path = ()
        entry = self.modules.get((target.id, module_path))
        if entry is None:
            if not module_path:
                entry = self.modules.get((target.id, ()))
            if entry is None:
                return {"status": "unresolved",
                        "reason": "module file is not in the indexed crate",
                        "module": module, "module_path": list(module_path)}
        if imported and imported != "*":
            child = self.modules.get((target.id, module_path + (imported,)))
            if child is not None:
                return {"status": "resolved", "target": child,
                        "file": child.file, "qualname": child.prefix,
                        "imported": imported, "is_module": True,
                        "module_path": list(child.module)}
        qualname = entry.prefix
        if imported and imported != "*":
            qualname = f"{qualname}.{imported}" if qualname else imported
        if imported and imported != "*" and depth < MAX_REEXPORT_DEPTH:
            binding = self.pub_bindings.get(
                (entry.file, entry.prefix, imported))
            if binding is not None:
                reexport = self.resolve(entry.file, binding, depth=depth + 1)
                if reexport.get("status") == "resolved":
                    return {**reexport, "reexported": True}
        return {"status": "resolved", "target": entry, "file": entry.file,
                "qualname": qualname, "imported": imported,
                "is_module": imported == "*" or not imported,
                "module_path": list(module_path)}

    def resolve_path(self, rel: str, path: str, scope: str = "") -> dict[str, Any]:
        """Resolve a qualified reference path such as `crate::a::b`.

        Relative paths are attempted too: `inner::go` resolves when `inner` is
        a child module of the importing module. A type/method path such as
        `Foo::bar` is reported unresolved and left to local scope resolution.
        """
        text = str(path or "").strip()
        parts = [part for part in text.split("::") if part]
        if len(parts) < 2:
            return {"status": "unresolved", "reason": "not a qualified path"}
        return self.resolve(rel, {"module": "::".join(parts[:-1]),
                                  "imported": parts[-1], "scope": scope,
                                  "glob": False, "name": parts[-1]})

    def import_file(self, rel: str, tag: Mapping[str, Any]) -> str | None:
        result = self.resolve(rel, tag)
        return result.get("file") if result.get("status") == "resolved" else None

    def summary(self) -> dict[str, Any]:
        return {
            "targets": len(self.targets),
            "crates": sorted(self.crates),
            "modules": len(self.modules),
            "files_with_contexts": len(self.file_entries),
            "ambiguous": dict(sorted(self.ambiguous.items())),
            "files_scanned": self.files_scanned,
            "not_checked": list(self.not_checked),
        }


# --------------------------------------------------------------------------- #
# Cargo discovery
# --------------------------------------------------------------------------- #

def _rel_join(*parts: str) -> str:
    return PurePosixPath(*[part for part in parts if part not in ("", ".")]).as_posix()


def _inside(root: Path, rel: str) -> bool:
    try:
        return (root / rel).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _manifest_paths(root: Path) -> tuple[list[str], int, bool, list[str]]:
    paths: list[str] = []
    scanned = 0
    truncated = False
    omissions: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames
                             if name not in _SKIP_DIRS and not name.startswith("."))
        for name in sorted(filenames):
            if name != "Cargo.toml":
                continue
            scanned += 1
            if scanned > MAX_MANIFESTS:
                truncated = True
                omissions.append("manifest limit reached")
                break
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            paths.append(rel)
        if truncated:
            break
    return paths, scanned, truncated, omissions


def _table(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _target_specs(manifest: str, text: str, root: Path) -> list[CrateTarget]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    package = _table(data.get("package"))
    package_name = str(package.get("name") or "")
    if not package_name:
        return []
    manifest_dir = PurePosixPath(manifest).parent.as_posix()
    if manifest_dir == ".":
        manifest_dir = ""
    targets: list[CrateTarget] = []

    def add(kind: str, path: str, name: str) -> None:
        rel = _rel_join(manifest_dir, path)
        if not rel or rel.startswith("../") or not _inside(root, rel):
            return
        if not (root / rel).is_file():
            return
        target = CrateTarget(
            id=f"{manifest}:{kind}:{rel}", package=package_name,
            name=name, kind=kind, root=rel, manifest=manifest)
        if all(item.id != target.id for item in targets):
            targets.append(target)

    lib = _table(data.get("lib"))
    lib_path = str(lib.get("path") or "src/lib.rs")
    lib_name = str(lib.get("name") or package_name.replace("-", "_"))
    add("lib", lib_path, lib_name)

    bins = data.get("bin")
    if isinstance(bins, list) and bins:
        for item in bins:
            value = _table(item)
            path = str(value.get("path") or "")
            name = str(value.get("name") or
                       (Path(path).stem if path else package_name))
            if path:
                add("bin", path, name)
    else:
        add("bin", "src/main.rs", package_name)
        bin_dir = root / manifest_dir / "src" / "bin"
        if bin_dir.is_dir():
            for path in sorted(bin_dir.glob("*.rs")):
                add("bin", (Path(manifest_dir) / "src" / "bin" / path.name).as_posix(),
                    path.stem)
            for path in sorted(bin_dir.glob("*/main.rs")):
                add("bin", (Path(manifest_dir) / "src" / "bin" / path.parent.name /
                            "main.rs").as_posix(), path.parent.name)

    for kind, field_name, default_dir in (
            ("test", "test", "tests"), ("bench", "bench", "benches")):
        configured = data.get(field_name)
        if isinstance(configured, list) and configured:
            for item in configured:
                value = _table(item)
                path = str(value.get("path") or "")
                if path:
                    add(kind, path, str(value.get("name") or Path(path).stem))
        else:
            base = root / manifest_dir / default_dir
            if base.is_dir():
                for path in sorted(base.glob("*.rs")):
                    add(kind, (Path(manifest_dir) / default_dir / path.name).as_posix(),
                        path.stem)
                for path in sorted(base.glob("*/main.rs")):
                    add(kind, (Path(manifest_dir) / default_dir / path.parent.name /
                               "main.rs").as_posix(), path.parent.name)
    return targets


# --------------------------------------------------------------------------- #
# module tree
# --------------------------------------------------------------------------- #

def _module_base(rel: str) -> str:
    p = PurePosixPath(rel)
    parent = p.parent.as_posix()
    if parent == ".":
        parent = ""
    if p.name in _CRATE_ROOT_FILES or p.name == "mod.rs":
        return parent
    parts = p.parts
    for i, part in enumerate(parts[:-1]):
        if part in _TEST_ROOT_DIRS and (i == 0 or parts[i - 1] != "src"):
            return parent
    if len(parts) >= 3 and parts[-3] == "src" and parts[-2] == "bin":
        return parent
    return p.with_suffix("").as_posix()


def _module_candidates(rel: str, parent: tuple[str, ...], name: str,
                       path_attribute: str | None) -> list[str]:
    if path_attribute:
        base = PurePosixPath(rel).parent
        return [(base / path_attribute).as_posix()]
    base = _module_base(rel)
    if parent:
        base = _join(base, *parent)
    stem = _join(base, name)
    return [f"{stem}.rs", f"{stem}/mod.rs"]


def _join(base: str, *parts: str) -> str:
    return PurePosixPath(base, *parts).as_posix().lstrip("./")


def _populate(workspace: Workspace, target: CrateTarget) -> None:
    root = workspace.root
    queue: list[tuple[str, tuple[str, ...], int]] = [(target.root, (), 0)]
    visited: set[tuple[str, str]] = set()
    while queue:
        rel, module_path, depth = queue.pop(0)
        key = (target.id, rel)
        if key in visited:
            workspace.ambiguous[f"{target.id}:{rel}"] = (
                "file is reachable from more than one module path")
            continue
        visited.add(key)
        if depth > MAX_DEPTH or len(visited) > MAX_MODULE_FILES:
            workspace.not_checked.append("module tree depth/file limit reached")
            break
        absolute = root / rel
        try:
            text = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            workspace.not_checked.append(f"{rel}: unreadable module file")
            continue
        parsed = parse_rust(absolute, text)
        workspace.parsed_files[rel] = parsed
        entry = ModuleEntry(target, rel, module_path, "")
        workspace.modules.setdefault((target.id, module_path), entry)
        workspace.file_entries.setdefault(rel, []).append(entry)
        for module in parsed.get("modules") or []:
            name = str(module.get("name") or "")
            if not name:
                continue
            parent = tuple(str(part) for part in module.get("parent") or ())
            full = module_path + parent + (name,)
            if module.get("inline"):
                inline_prefix = ".".join(parent + (name,))
                inline = ModuleEntry(target, rel, full, inline_prefix)
                if (target.id, full) in workspace.modules:
                    workspace.ambiguous[f"{target.id}:{'::'.join(full)}"] = (
                        "duplicate inline module path")
                else:
                    workspace.modules[(target.id, full)] = inline
                workspace.file_entries.setdefault(rel, []).append(inline)
                continue
            candidates = _module_candidates(
                rel, parent, name, module.get("path_attribute"))
            found = next((candidate for candidate in candidates
                          if _inside(root, candidate)
                          and (root / candidate).is_file()), None)
            if found is None:
                workspace.not_checked.append(
                    f"{rel}: module {name!r} has no indexed file inside the root")
                continue
            child = ModuleEntry(target, found, full, "")
            if (target.id, full) in workspace.modules:
                workspace.ambiguous[f"{target.id}:{'::'.join(full)}"] = (
                    "duplicate module path")
            else:
                workspace.modules[(target.id, full)] = child
            queue.append((found, full, depth + 1))
    # Prefix-scoped public re-exports, used to follow `pub use ... as ...`.
    for rel, parsed in workspace.parsed_files.items():
        for tag in parsed.get("imports") or []:
            if not tag.get("pub"):
                continue
            scope = str(tag.get("scope") or "")
            name = str(tag.get("name") or "")
            if name:
                workspace.pub_bindings[(rel, scope, name)] = tag


def discover(root: str | Path, *, max_manifests: int = MAX_MANIFESTS
             ) -> Workspace:
    """Read Cargo targets and build the module/import map for `root`."""
    root = Path(root).resolve()
    workspace = Workspace(root)
    paths, scanned, truncated, omissions = _manifest_paths(root)
    workspace.files_scanned = scanned
    workspace.not_checked.extend(omissions)
    if truncated:
        workspace.not_checked.append("Cargo manifest scan truncated")
    if len(paths) > max_manifests:
        paths = paths[:max_manifests]
        workspace.not_checked.append("Cargo manifest limit reached")
    targets: list[CrateTarget] = []
    for rel in paths:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        targets.extend(_target_specs(rel, text, root))
    for target in targets:
        workspace.targets[target.id] = target
        if target.kind == "lib":
            if target.name in workspace.crates:
                workspace.ambiguous[f"crate:{target.name}"] = (
                    "workspace has more than one library crate with this name")
            else:
                workspace.crates[target.name] = target
    for target in targets:
        _populate(workspace, target)
    return workspace


__all__ = [
    "CrateTarget",
    "MAX_REEXPORT_DEPTH",
    "ModuleEntry",
    "Workspace",
    "discover",
]
