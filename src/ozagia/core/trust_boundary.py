"""Bounded, offline Python trust-boundary flow candidates.

This deliberately recognizes only direct, same-function flows from a small
set of common external inputs to security-sensitive calls. It is a candidate
generator, not a taint engine; aliases through attributes, helpers, and
framework wrappers are outside its scope.
"""
from __future__ import annotations

import ast
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 5000
_SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules", "vendor",
              "dist", "build", "target", "__pycache__"}

_SOURCES = {
    "input", "get_json", "get_data", "form", "args", "values",
    "query_params", "argv", "environ", "getenv",
}
_SINKS = {
    "system": ("command", "cwe:78"), "popen": ("command", "cwe:78"),
    "run": ("command", "cwe:78"), "Popen": ("command", "cwe:78"),
    "execute": ("sql", "cwe:89"), "executemany": ("sql", "cwe:89"),
    "raw": ("sql", "cwe:89"), "urlopen": ("ssrf", "cwe:918"),
    "open": ("path", "cwe:22"), "send_file": ("path", "cwe:22"),
    "eval": ("code", "cwe:95"), "exec": ("code", "cwe:95"),
}


def _dotted_name(node: ast.AST) -> str:
    """Return a simple dotted name, without trying to resolve aliases."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _request_value_get(node: ast.Call) -> bool:
    """Recognize common request containers without treating all `.get`s as input."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return False
    receiver = _dotted_name(node.func.value).lower().split(".")
    return any(part in {"request", "args", "form", "params", "query_params",
                        "headers", "cookies"} for part in receiver)


def _http_get_post(node: ast.Call, http_clients: set[str]) -> bool:
    """Recognize conventional HTTP client calls, not arbitrary object methods."""
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in {"get", "post"}:
        return False
    receiver = _dotted_name(node.func.value).lower().split(".")
    # Generic `session` and `client` names are ambiguous. Accept them only
    # when an earlier assignment in this function binds the name to a known
    # requests/httpx constructor.
    return any(part in {"requests", "httpx"} for part in receiver) or (
        isinstance(node.func.value, ast.Name)
        and node.func.value.id in http_clients
    )


def _http_client_constructor(node: ast.AST) -> bool:
    name = _dotted_name(node.func) if isinstance(node, ast.Call) else ""
    return name in {"requests.Session", "httpx.Client", "httpx.AsyncClient"}


@dataclass(frozen=True)
class Candidate:
    rule: str
    category: str
    cwe: str
    file: str
    line: int
    source_line: int
    message: str
    confidence: str = "low"
    not_assessed: tuple[str, ...] = (
        "interprocedural flow", "framework validation", "runtime reachability",
        "authorization policy")

    def as_dict(self) -> dict[str, object]:
        return {**self.__dict__, "not_assessed": list(self.not_assessed)}


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _source_line(node: ast.AST) -> int | None:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if name in _SOURCES or _request_value_get(child):
                return child.lineno
        if isinstance(child, ast.Attribute) and child.attr in _SOURCES:
            return child.lineno
    return None


class _Flow(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.taint: dict[str, int] = {}
        self.http_clients: set[str] = set()
        self.candidates: list[Candidate] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved = self.taint
        saved_clients = self.http_clients
        self.taint = {}
        self.http_clients = set()
        for stmt in node.body:
            self.visit(stmt)
        self.taint = saved
        self.http_clients = saved_clients

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        source = _source_line(node.value)
        origins = [self.taint[n] for n in _names(node.value) if n in self.taint]
        origin = source if source is not None else min(origins, default=None)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if _http_client_constructor(node.value):
                    self.http_clients.add(target.id)
                else:
                    self.http_clients.discard(target.id)
                if origin is None:
                    self.taint.pop(target.id, None)
                else:
                    self.taint[target.id] = origin
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            fake = ast.Assign(targets=[node.target], value=node.value)
            self.visit_Assign(fake)

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
        sink = _SINKS.get(name)
        if sink is None and _http_get_post(node, self.http_clients):
            sink = ("ssrf", "cwe:918")
        if sink:
            source_lines = [self.taint[n] for n in _names(node) if n in self.taint]
            direct = [_source_line(a) for a in node.args]
            origin = min(source_lines + [x for x in direct if x is not None], default=None)
            if origin is not None:
                category, cwe = sink
                self.candidates.append(Candidate(
                    rule=f"trust-flow-{category}", category=category, cwe=cwe,
                    file=self.file, line=node.lineno, source_line=origin,
                    message=f"external input may reach {category} sink {name}()"))
        self.generic_visit(node)


def analyze_source(source: str, *, file: str = "<memory>") -> list[dict[str, object]]:
    """Return same-function Python source-to-sink candidates from source text."""
    try:
        tree = ast.parse(source, filename=file)
    except (SyntaxError, ValueError):
        return []
    flow = _Flow(file)
    flow.visit(tree)
    return [item.as_dict() for item in flow.candidates]


def analyze_file(path: Path, *, root: Path | None = None) -> list[dict[str, object]]:
    """Analyze one bounded Python file; unreadable/oversize files are skipped."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    label = path.as_posix()
    if root is not None:
        try:
            label = path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return []
    return analyze_source(source, file=label)


def analyze_tree(root: Path, *, budget_s: float = 5.0,
                 max_files: int = MAX_FILES) -> dict[str, object]:
    """Scan bounded Python files under root; never follows directory symlinks."""
    deadline = time.monotonic() + max(0.0, budget_s)
    findings: list[dict[str, object]] = []
    checked = 0
    stopped = ""
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and
                         not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            if time.monotonic() >= deadline:
                stopped = "trust-boundary time budget exhausted"
                break
            if not name.endswith((".py", ".pyi")):
                continue
            if checked >= max_files:
                stopped = "trust-boundary file limit reached"
                break
            path = Path(directory) / name
            if path.is_symlink():
                continue
            checked += 1
            findings.extend(analyze_file(path, root=root))
        if stopped:
            break
    if not stopped and time.monotonic() >= deadline:
        stopped = "trust-boundary time budget exhausted"
    return {"findings": findings, "checked": checked,
            "not_checked": ([{"reason": stopped}] if stopped else [])}
