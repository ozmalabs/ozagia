"""Bounded Python and Rust inventories for the migration overlay.

The Python side reuses ``ozagia.core.memory._python_parse`` (the same stdlib
AST parser the graph uses) and adds only what a port needs: module names,
``__all__``, dataclass fields and ``*args``/``**kwargs`` markers. The Rust side
reuses ``ozagia.index.rust_symbols.parse_rust`` (tree-sitter, regex fallback)
and adds crate/module paths from the nearest ``Cargo.toml`` and struct fields
from the parsed source span.

Neither side imports or executes the target project. Scans are bounded by file
count, per-file size and total bytes; every skipped file is reported.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ozagia.config import GENERIC_SKIP_DIRS, MigrationConfig
from ozagia.core.memory import _python_parse
from ozagia.index.rust_symbols import parse_rust

INVENTORY_VERSION = 1
MAX_FILES = 4000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SYMBOLS = 20_000
MAX_OMISSIONS = 200
_RUST_EXTRA_SKIP = frozenset({"target"})
_PYTHON_EXTRA_SKIP = frozenset({"__pycache__", "site-packages"})
_CLASS_KINDS = frozenset({"class", "dataclass", "protocol", "enum",
                          "exception"})


@dataclass(frozen=True)
class FieldInfo:
    name: str
    annotation: str
    default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "annotation": self.annotation,
                "default": self.default}


@dataclass(frozen=True)
class PythonSymbol:
    """One public Python definition."""

    key: str
    name: str
    qualname: str
    module: str
    path: str
    kind: str
    signature: str
    args: tuple[str, ...]
    returns: str
    async_: bool
    decorators: tuple[str, ...]
    doc: str
    bases: tuple[str, ...]
    fields: tuple[FieldInfo, ...]
    methods: tuple[str, ...]
    imports: tuple[str, ...]
    exported: bool
    public: bool
    api: bool
    arg_annotations: tuple[tuple[str, str], ...] = ()
    varargs: bool = False
    kwargs: bool = False
    keyword_only: bool = False
    defaults: bool = False
    source: str = "python-ast"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "qualname": self.qualname,
            "module": self.module,
            "path": self.path,
            "kind": self.kind,
            "signature": self.signature,
            "args": list(self.args),
            "returns": self.returns,
            "async": self.async_,
            "decorators": list(self.decorators),
            "doc": self.doc,
            "bases": list(self.bases),
            "fields": [field.to_dict() for field in self.fields],
            "exported": self.exported,
            "public": self.public,
            "api": self.api,
            "arg_annotations": dict(self.arg_annotations),
            "varargs": self.varargs,
            "kwargs": self.kwargs,
            "keyword_only": self.keyword_only,
            "defaults": self.defaults,
            "source": self.source,
        }


@dataclass(frozen=True)
class RustSymbol:
    """One public Rust item or public/impl/trait method."""

    key: str
    name: str
    qualname: str
    module: str
    crate: str
    path: str
    kind: str
    signature: str
    args: tuple[str, ...]
    returns: str
    async_: bool
    decorators: tuple[str, ...]
    doc: str
    visibility: str
    fields: tuple[FieldInfo, ...]
    public: bool
    api: bool
    owner: str = ""
    owner_kind: str = ""
    is_test: bool = False
    line: int = 0
    source: str = "rust"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "qualname": self.qualname,
            "module": self.module,
            "crate": self.crate,
            "path": self.path,
            "kind": self.kind,
            "signature": self.signature,
            "args": list(self.args),
            "returns": self.returns,
            "async": self.async_,
            "decorators": list(self.decorators),
            "doc": self.doc,
            "visibility": self.visibility,
            "fields": [field.to_dict() for field in self.fields],
            "public": self.public,
            "api": self.api,
            "owner": self.owner,
            "owner_kind": self.owner_kind,
            "test": self.is_test,
            "line": self.line,
            "source": self.source,
        }


@dataclass(frozen=True)
class TreeInventory:
    """One tree's bounded inventory."""

    language: str
    root: str
    files: int
    bytes_read: int
    symbols: tuple[PythonSymbol | RustSymbol, ...]
    omitted: tuple[dict[str, Any], ...]
    truncated: bool
    fingerprint: str
    backends: tuple[tuple[str, int], ...] = ()
    limits: dict[str, int] = field(default_factory=dict)

    @property
    def api(self) -> tuple[PythonSymbol | RustSymbol, ...]:
        return tuple(item for item in self.symbols if item.api)

    def by_key(self) -> dict[str, PythonSymbol | RustSymbol]:
        return {item.key: item for item in self.symbols}

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "root": self.root,
            "files": self.files,
            "bytes": self.bytes_read,
            "symbols": len(self.symbols),
            "api": len(self.api),
            "omitted": list(self.omitted),
            "omitted_count": len(self.omitted),
            "truncated": self.truncated,
            "fingerprint": self.fingerprint,
            "backends": dict(self.backends),
            "limits": dict(self.limits),
        }


def _limits() -> dict[str, int]:
    return {
        "max_files": MAX_FILES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_symbols": MAX_SYMBOLS,
    }


def _tree_files(tree: Path, suffix: str, skip: frozenset[str],
                ) -> Iterator[Path]:
    """Sorted source files under `tree`, never following directory symlinks."""
    for current, dirnames, filenames in os.walk(tree, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in skip and not name.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if name.endswith(suffix):
                yield Path(current) / name


def _tree_rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _inside_root(root: Path, tree: Path, rel: str, language: str) -> None:
    try:
        tree.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"{language} migration tree {rel!r} is outside the repository "
            "root") from exc


def _python_module(tree: Path, path: Path) -> str:
    rel = path.relative_to(tree)
    parts = list(rel.parts)
    stem = path.stem
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = stem
    module = ".".join(parts)
    return module or (tree.name or "__root__")


def _all_names(tree: ast.Module) -> tuple[set[str] | None, str]:
    """The literal module ``__all__`` (or None plus a reason)."""
    found: set[str] | None = None
    dynamic = False
    for node in tree.body:
        value: ast.AST | None = None
        target: ast.AST | None = None
        if isinstance(node, ast.Assign):
            names = [item for item in node.targets
                     if isinstance(item, ast.Name) and item.id == "__all__"]
            if names:
                target, value = names[0], node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                target, value = node.target, node.value
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                dynamic = True
        if target is None or value is None:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            dynamic = True
            continue
        if isinstance(literal, (list, tuple, set, frozenset)) and all(
                isinstance(item, str) for item in literal):
            found = {str(item) for item in literal}
        else:
            dynamic = True
    if dynamic:
        return None, "dynamic __all__ is not evaluated; public names are used"
    return found, ""


def _node_map(tree: ast.Module) -> dict[str, ast.AST]:
    """Definitions by the qualname ``_python_parse`` reports."""
    out: dict[str, ast.AST] = {}

    def walk(body: list[ast.AST], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                qual = f"{prefix}.{node.name}" if prefix else node.name
                out[qual] = node
                walk(node.body, qual)
            else:
                for child in ast.iter_child_nodes(node):
                    walk([child], prefix)

    walk(tree.body, "")
    return out


def _class_kind(bases: tuple[str, ...]) -> str:
    names = [base.split("[", 1)[0].rsplit(".", 1)[-1] for base in bases]
    if any(name == "Protocol" or name.endswith("Protocol")
           for name in names):
        return "protocol"
    if any(name == "Enum" or name.endswith("Enum") for name in names):
        return "enum"
    if any(name.endswith(("Exception", "BaseException", "Error"))
           for name in names):
        return "exception"
    return "class"


def _class_fields(node: ast.ClassDef | None) -> tuple[FieldInfo, ...]:
    if node is None:
        return ()
    out: list[FieldInfo] = []
    for statement in node.body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        if not isinstance(statement.target, ast.Name):
            continue
        try:
            annotation = ast.unparse(statement.annotation)
        except Exception:  # pragma: no cover - ast.unparse is stable
            annotation = ""
        out.append(FieldInfo(statement.target.id, annotation,
                             statement.value is not None))
    return tuple(out)


def _class_methods(node: ast.ClassDef | None) -> tuple[str, ...]:
    if node is None:
        return ()
    return tuple(
        statement.name for statement in node.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)))


def _arg_annotations(node: ast.AST | None) -> tuple[tuple[str, str], ...]:
    args = getattr(node, "args", None)
    if args is None:
        return ()
    out: list[tuple[str, str]] = []
    for group in (getattr(args, "posonlyargs", ()), args.args,
                  getattr(args, "kwonlyargs", ())):
        for arg in group:
            if arg.arg in ("self", "cls"):
                continue
            try:
                annotation = (ast.unparse(arg.annotation)
                              if arg.annotation is not None else "")
            except Exception:  # pragma: no cover - ast.unparse is stable
                annotation = ""
            out.append((arg.arg, annotation))
    return tuple(out)


def _python_signature(name: str, args: tuple[str, ...], returns: str,
                      *, is_async: bool, kind: str) -> str:
    params = ", ".join(args)
    if kind in _CLASS_KINDS:
        return f"{name}({params})"
    prefix = "async " if is_async else ""
    return f"{prefix}{name}({params}) -> {returns or '?'}"


def _python_imports(imports: list[dict[str, Any]]) -> tuple[str, ...]:
    names: set[str] = set()
    for item in imports:
        module = str(item.get("module") or "")
        if module:
            names.add(module)
        for name in item.get("names") or ():
            names.add(str(name))
    return tuple(sorted(names))


def _first_line(text: Any) -> str:
    lines = str(text or "").splitlines()
    return lines[0].strip() if lines else ""


def inventory_python(root: str | Path, config: MigrationConfig,
                     *, tree: str | None = None) -> TreeInventory:
    """Public Python definitions under the configured Python tree."""
    root_p = Path(root).resolve()
    rel_tree = tree if tree is not None else config.python
    if not rel_tree:
        raise ValueError("no Python migration tree is configured")
    tree_path = (root_p / rel_tree).resolve()
    if not tree_path.is_dir():
        raise ValueError(f"python migration tree {rel_tree!r} does not exist")
    _inside_root(root_p, tree_path, rel_tree, "python")

    files = 0
    total = 0
    symbols: list[PythonSymbol] = []
    omitted: list[dict[str, Any]] = []
    digests: list[str] = []
    truncated = False
    backends: dict[str, int] = {"python-ast": 0}

    def omit(rel: str, reason: str, kind: str = "file") -> None:
        nonlocal truncated
        if len(omitted) < MAX_OMISSIONS:
            omitted.append({"path": rel, "reason": reason, "kind": kind})
        else:
            truncated = True
        try:
            stamp = (root_p / rel).stat()
            digests.append(
                f"{rel}\0omitted\0{stamp.st_size}:{stamp.st_mtime_ns}")
        except OSError:
            digests.append(f"{rel}\0omitted")

    skip = GENERIC_SKIP_DIRS | _PYTHON_EXTRA_SKIP
    for path in _tree_files(tree_path, ".py", skip):
        rel = _tree_rel(root_p, path)
        if files >= MAX_FILES:
            truncated = True
            omit(rel, f"file limit {MAX_FILES} reached", "file_list")
            break
        try:
            size = path.stat().st_size
        except OSError as exc:
            omit(rel, f"stat failed: {exc}")
            continue
        if path.is_symlink():
            omit(rel, "symlink file is not followed")
            continue
        if size > MAX_FILE_BYTES:
            omit(rel, f"file exceeds {MAX_FILE_BYTES} bytes")
            continue
        if total + size > MAX_TOTAL_BYTES:
            truncated = True
            omit(rel, f"total byte budget {MAX_TOTAL_BYTES} reached",
                 "file_list")
            break
        try:
            raw = path.read_bytes()
        except OSError as exc:
            omit(rel, f"read failed: {exc}")
            continue
        try:
            src = raw.decode("utf-8")
        except UnicodeDecodeError:
            omit(rel, "not valid UTF-8")
            continue
        files += 1
        total += len(raw)
        digests.append(f"{rel}\0{hashlib.sha256(raw).hexdigest()}")
        parsed = _python_parse(path, src)
        if parsed is None:
            omit(rel, "could not be parsed as Python", "shape")
            continue
        try:
            tree_ast = ast.parse(src)
        except (SyntaxError, ValueError):
            omit(rel, "could not be parsed as Python", "shape")
            continue
        all_set, all_note = _all_names(tree_ast)
        if all_note:
            omit(rel, all_note, "shape")
        if all_set is not None:
            defined = {item["name"] for item in parsed.symbols
                       if "." not in item["qualname"]}
            missing = sorted(all_set - defined)
            if missing:
                omit(rel, "__all__ names not defined: " + ", ".join(missing[:10]),
                     "shape")
        nodes = _node_map(tree_ast)
        imports = _python_imports(parsed.imports)
        module = _python_module(tree_path, path)
        api_classes = {
            item["name"] for item in parsed.symbols
            if item["kind"] == "class" and "." not in item["qualname"]
            and (item["name"] in all_set if all_set is not None
                 else not item["name"].startswith("_"))
        }

        for record in parsed.symbols:
            qualname = str(record["qualname"])
            name = str(record["name"])
            kind = str(record["kind"])
            node = nodes.get(qualname)
            top = "." not in qualname
            owner_qual = qualname.rsplit(".", 1)[0] if not top else ""
            owner_api = (not owner_qual or owner_qual.rsplit(".", 1)[-1]
                         in api_classes)
            if kind in ("class", "protocol", "enum", "exception"):
                if top:
                    kind = _class_kind(tuple(record.get("bases") or ()))
                    decorators = tuple(str(item) for item in
                                       record.get("decorators") or ())
                    if kind == "class" and any(
                            item.strip().lstrip("@").split("(", 1)[0].strip()
                            .rsplit(".", 1)[-1] == "dataclass"
                            for item in decorators):
                        kind = "dataclass"
            elif not top:
                if not owner_api or name.startswith("_"):
                    continue
                kind = ("async_method" if kind == "async_function"
                        else "method")
            if not top and kind not in ("method", "async_method"):
                continue
            public = not any(part.startswith("_") for part in qualname.split("."))
            exported = bool(top and all_set is not None and name in all_set)
            api = bool(top and (exported if all_set is not None else public))
            if not public and not exported:
                continue
            fields = _class_fields(node) if top and kind in _CLASS_KINDS else ()
            methods = _class_methods(node) if top and kind in _CLASS_KINDS else ()
            raw_args = [str(item) for item in record.get("args") or ()
                        if str(item) not in ("self", "cls")]
            annotated_args = _arg_annotations(node)
            if annotated_args:
                raw_args = [name for name, _annotation in annotated_args]
            args = tuple(field.name for field in fields) if fields else tuple(raw_args)
            is_async = kind in ("async_function", "async_method")
            returns = str(record.get("returns") or "")
            varargs = bool(node is not None and getattr(node, "args", None)
                           is not None and node.args.vararg is not None)
            kwargs = bool(node is not None and getattr(node, "args", None)
                          is not None and node.args.kwarg is not None)
            keyword_only = bool(node is not None
                                and getattr(node, "args", None) is not None
                                and node.args.kwonlyargs)
            defaults = bool(node is not None
                            and getattr(node, "args", None) is not None
                            and (node.args.defaults or node.args.kw_defaults))
            if len(symbols) >= MAX_SYMBOLS:
                truncated = True
                omit(rel, f"symbol limit {MAX_SYMBOLS} reached", "file_list")
                break
            symbols.append(PythonSymbol(
                key=f"{module}.{qualname}",
                name=name,
                qualname=qualname,
                module=module,
                path=rel,
                kind=kind,
                signature=_python_signature(
                    name, args, returns, is_async=is_async, kind=kind),
                args=args,
                returns=returns,
                async_=is_async,
                decorators=tuple(str(item) for item in
                                 record.get("decorators") or ()),
                doc=_first_line(record.get("doc")),
                bases=tuple(str(item) for item in record.get("bases") or ()),
                fields=fields,
                methods=methods,
                imports=imports,
                exported=exported,
                public=public,
                api=api,
                arg_annotations=_arg_annotations(node),
                varargs=varargs,
                kwargs=kwargs,
                keyword_only=keyword_only,
                defaults=defaults,
            ))
        backends["python-ast"] += 1

    symbols.sort(key=lambda item: (item.key, item.path))
    digests.sort()
    fingerprint = hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()
    return TreeInventory(
        language="python",
        root=rel_tree,
        files=files,
        bytes_read=total,
        symbols=tuple(symbols),
        omitted=tuple(omitted),
        truncated=truncated,
        fingerprint=fingerprint,
        backends=tuple(sorted(backends.items())),
        limits=_limits(),
    )


def _crate_name(manifest: Path) -> str:
    try:
        import tomllib
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    package = data.get("package")
    if isinstance(package, dict) and isinstance(package.get("name"), str):
        return str(package["name"]).replace("-", "_")
    lib = data.get("lib")
    if isinstance(lib, dict) and isinstance(lib.get("name"), str):
        return str(lib["name"]).replace("-", "_")
    return ""


def _rust_manifest(root: Path, path: Path) -> tuple[Path | None, str]:
    current = path.parent
    while True:
        manifest = current / "Cargo.toml"
        if manifest.is_file():
            return manifest, _crate_name(manifest)
        if current == root or current.parent == current:
            return None, ""
        current = current.parent


def _rust_module(tree_path: Path, manifest: Path | None, crate: str,
                 path: Path) -> str:
    base = manifest.parent if manifest is not None else tree_path
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = path.relative_to(tree_path)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] in ("lib", "main", "mod"):
        parts = parts[:-1]
    prefix = crate or (tree_path.name or "crate").replace("-", "_")
    return "::".join([prefix, *parts]) if parts else prefix


def _rust_fields(source: str, start_line: int, end_line: int) -> tuple[FieldInfo, ...]:
    lines = source.splitlines()[start_line - 1:end_line]
    text = "\n".join(lines)
    if not re.search(r"\b(struct|union)\b", text):
        return ()
    brace = text.find("{")
    if brace != -1:
        body = text[brace + 1:text.rfind("}")]
        out: list[FieldInfo] = []
        for line in body.splitlines():
            match = re.match(
                r"\s*(?:pub(?:\([^)]*\))?\s+)?([A-Za-z_][A-Za-z0-9_]*)"
                r"\s*:\s*([^,]+),?\s*$", line)
            if match:
                out.append(FieldInfo(match.group(1), match.group(2).strip()))
        return tuple(out)
    paren = text.find("(")
    close = text.rfind(")")
    if paren != -1 and close > paren:
        out = []
        for index, part in enumerate(text[paren + 1:close].split(",")):
            value = re.sub(r"^\s*pub(?:\([^)]*\))?\s+", "", part).strip()
            if not value:
                continue
            name = f"_{index}"
            annotation = value
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)", value)
            if match:
                name, annotation = match.group(1), match.group(2)
            out.append(FieldInfo(name, annotation))
        return tuple(out)
    return ()


def inventory_rust(root: str | Path, config: MigrationConfig,
                   *, tree: str | None = None) -> TreeInventory:
    """Public Rust items under the configured Rust tree."""
    root_p = Path(root).resolve()
    rel_tree = tree if tree is not None else config.rust
    if not rel_tree:
        raise ValueError("no Rust migration tree is configured")
    tree_path = (root_p / rel_tree).resolve()
    if not tree_path.is_dir():
        raise ValueError(f"rust migration tree {rel_tree!r} does not exist")
    _inside_root(root_p, tree_path, rel_tree, "rust")

    files = 0
    total = 0
    symbols: list[RustSymbol] = []
    omitted: list[dict[str, Any]] = []
    digests: list[str] = []
    truncated = False
    backends: dict[str, int] = {}

    def omit(rel: str, reason: str, kind: str = "file") -> None:
        nonlocal truncated
        if len(omitted) < MAX_OMISSIONS:
            omitted.append({"path": rel, "reason": reason, "kind": kind})
        else:
            truncated = True
        try:
            stamp = (root_p / rel).stat()
            digests.append(
                f"{rel}\0omitted\0{stamp.st_size}:{stamp.st_mtime_ns}")
        except OSError:
            digests.append(f"{rel}\0omitted")

    skip = GENERIC_SKIP_DIRS | _RUST_EXTRA_SKIP
    for path in _tree_files(tree_path, ".rs", skip):
        rel = _tree_rel(root_p, path)
        if files >= MAX_FILES:
            truncated = True
            omit(rel, f"file limit {MAX_FILES} reached", "file_list")
            break
        try:
            size = path.stat().st_size
        except OSError as exc:
            omit(rel, f"stat failed: {exc}")
            continue
        if path.is_symlink():
            omit(rel, "symlink file is not followed")
            continue
        if size > MAX_FILE_BYTES:
            omit(rel, f"file exceeds {MAX_FILE_BYTES} bytes")
            continue
        if total + size > MAX_TOTAL_BYTES:
            truncated = True
            omit(rel, f"total byte budget {MAX_TOTAL_BYTES} reached",
                 "file_list")
            break
        try:
            raw = path.read_bytes()
        except OSError as exc:
            omit(rel, f"read failed: {exc}")
            continue
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            omit(rel, "not valid UTF-8")
            continue
        files += 1
        total += len(raw)
        digests.append(f"{rel}\0{hashlib.sha256(raw).hexdigest()}")
        parsed = parse_rust(path, source)
        backend = str(parsed.get("backend") or "unavailable")
        backends[backend] = backends.get(backend, 0) + 1
        if parsed.get("error") and not parsed.get("symbols"):
            omit(rel, f"rust parse unavailable: {parsed.get('error')}",
                 "shape")
            continue
        if parsed.get("has_errors"):
            omit(rel, "tree-sitter reported syntax errors; symbols are "
                      "reported but may be incomplete", "shape")
        if backend != "tree-sitter" and parsed.get("error"):
            omit(rel, f"rust parser fallback ({backend}): {parsed['error']}",
                 "backend")
        manifest, crate = _rust_manifest(root_p, path)
        module = _rust_module(tree_path, manifest, crate, path)
        base_source = str(parsed.get("source") or source)
        trait_names = {
            str(item["qualname"]) for item in parsed.get("symbols") or ()
            if item.get("kind") == "trait"
        }
        for record in parsed.get("symbols") or ():
            kind = str(record.get("kind") or "")
            if kind in ("impl", "mod"):
                continue
            qualname = str(record.get("qualname") or record.get("name") or "")
            name = str(record.get("name") or qualname.rsplit(".", 1)[-1])
            top = "." not in qualname
            owner = qualname.rsplit(".", 1)[0] if not top else ""
            owner_kind = ""
            if kind == "method":
                owner_kind = "trait" if owner in trait_names else "impl"
            visibility = str(record.get("visibility") or "")
            public = visibility.startswith("pub") or owner_kind == "trait"
            api = bool(top and public and kind not in ("method", "impl", "mod"))
            if not public:
                continue
            fields = (_rust_fields(base_source, int(record.get("start_line") or 1),
                                   int(record.get("end_line") or 1))
                      if kind in ("struct", "union") else ())
            raw_args = tuple(str(item) for item in record.get("args") or ()
                             if str(item) != "self")
            is_async = bool(record.get("async"))
            returns = str(record.get("returns") or "")
            if kind in ("struct", "union") and fields:
                signature = f"{name}({', '.join(field.name for field in fields)})"
            elif kind in ("function", "method"):
                signature = (f"{'async ' if is_async else ''}"
                             f"{name}({', '.join(raw_args)}) -> "
                             f"{returns or '?'}")
            else:
                signature = f"{kind} {name}"
            if len(symbols) >= MAX_SYMBOLS:
                truncated = True
                omit(rel, f"symbol limit {MAX_SYMBOLS} reached", "file_list")
                break
            symbols.append(RustSymbol(
                key=f"{module}::{qualname.replace('.', '::')}",
                name=name,
                qualname=qualname,
                module=module,
                crate=crate,
                path=rel,
                kind=kind,
                signature=signature,
                args=tuple(field.name for field in fields) if fields else raw_args,
                returns=returns,
                async_=is_async,
                decorators=tuple(str(item) for item in
                                 record.get("decorators") or ()),
                doc=_first_line(record.get("doc")),
                visibility=visibility,
                fields=fields,
                public=public,
                api=api,
                owner=owner,
                owner_kind=owner_kind,
                is_test=bool(record.get("is_test")),
                line=int(record.get("start_line") or 0),
            ))

    symbols.sort(key=lambda item: (item.key, item.path, item.line))
    digests.sort()
    fingerprint = hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()
    return TreeInventory(
        language="rust",
        root=rel_tree,
        files=files,
        bytes_read=total,
        symbols=tuple(symbols),
        omitted=tuple(omitted),
        truncated=truncated,
        fingerprint=fingerprint,
        backends=tuple(sorted(backends.items())),
        limits=_limits(),
    )


__all__ = [
    "INVENTORY_VERSION",
    "FieldInfo",
    "PythonSymbol",
    "RustSymbol",
    "TreeInventory",
    "inventory_python",
    "inventory_rust",
]
