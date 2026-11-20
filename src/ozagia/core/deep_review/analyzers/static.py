"""Small, conservative AST checks used by deep review Phase 1.

These checks intentionally make narrow claims. They inspect source text only;
they never import or execute the reviewed project.
"""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

from . import failure_modes
from ._bounded_read import read_text as _read_text

SUBJECTS = ("performance", "failure-modes", "data-structures", "scalability")
SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
MAX_SOURCE_BYTES = 1_000_000
MAX_FILES = 3_000
MAX_TOTAL_BYTES = 20_000_000
MAX_AST_NODES = 100_000


def _name(node):
    return node.id if isinstance(node, ast.Name) else None


def _functools_imports(tree):
    """Return module-scope functools imports used to resolve decorators.

    Imports inside functions and classes do not bind names in the module
    namespace, so they must not make an unrelated module-level decorator look
    like a functools decorator.
    """
    modules, names = set(), {}

    class ModuleImports(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            return

        def visit_Import(self, node):
            for alias in node.names:
                if alias.name == "functools":
                    modules.add(alias.asname or "functools")

        def visit_ImportFrom(self, node):
            if node.module == "functools":
                names.update({alias.asname or alias.name: alias.name
                              for alias in node.names
                              if alias.name in {"cache", "lru_cache"}})

    ModuleImports().visit(tree)
    return modules, names


def _unbounded_cache(decorator, modules, names):
    """Recognize imported functools.cache and explicitly unbounded lru_cache."""
    called = isinstance(decorator, ast.Call)
    target = decorator.func if called else decorator
    if isinstance(target, ast.Name):
        name = names.get(target.id)
        imported = name is not None
    elif (isinstance(target, ast.Attribute)
          and isinstance(target.value, ast.Name)):
        name = target.attr
        imported = (target.value.id in modules
                    and name in {"cache", "lru_cache"})
    else:
        return False
    if not imported:
        return False
    if name == "cache":
        return True
    if not called:
        # @lru_cache uses functools' bounded default maxsize=128.
        return False
    maxsize = (decorator.args[0] if decorator.args else None)
    maxsize = next((keyword.value for keyword in decorator.keywords
                    if keyword.arg == "maxsize"), maxsize)
    return isinstance(maxsize, ast.Constant) and maxsize.value is None


def _mutable_default(default, shadowed_builtins=frozenset()):
    """Recognize common mutable default literals and builtin constructors.

    Calls such as ``dict()`` and ``set()`` create one shared object at function
    definition time just like ``{}`` and ``set()`` literals. Keep this list
    narrow: arbitrary constructor calls may return immutable values.
    """
    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
        return True
    return (isinstance(default, ast.Call)
            and isinstance(default.func, ast.Name)
            and default.func.id not in shadowed_builtins
            and default.func.id in {"list", "dict", "set", "bytearray"})


def _shadowed_builtin_names(tree):
    """Names bound in source that could shadow builtin constructors.

    This deliberately treats a binding in any scope as a shadow: that can miss
    a real builtin use, but avoids asserting that a same-spelled user factory
    returns a mutable object.
    """
    candidates = {"list", "dict", "set", "bytearray"}
    shadowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id in candidates:
                shadowed.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in candidates:
                shadowed.add(node.name)
            if isinstance(node, ast.ClassDef):
                continue
            for arg in (*node.args.posonlyargs, *node.args.args,
                        *node.args.kwonlyargs):
                if arg.arg in candidates:
                    shadowed.add(arg.arg)
            if node.args.vararg and node.args.vararg.arg in candidates:
                shadowed.add(node.args.vararg.arg)
            if node.args.kwarg and node.args.kwarg.arg in candidates:
                shadowed.add(node.args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or (alias.name.split(".")[0]
                                         if isinstance(node, ast.Import)
                                         else alias.name)
                if bound in candidates or alias.name == "*":
                    shadowed.update(candidates)
        elif isinstance(node, ast.ExceptHandler):
            # Unlike assignment targets, the exception alias is stored as a
            # string in the AST, so the Name(Store) check above misses it.
            if node.name in candidates:
                shadowed.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name in candidates:
            # Structural pattern capture names are represented as strings.
            shadowed.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest in candidates:
            shadowed.add(node.rest)
    return shadowed


def _finding(subject, kind, path, node, summary, recommendation, symbol=None,
             confidence="low"):
    return {"section": subject, "type": kind, "severity": "minor",
            "confidence": confidence,
            "keys": [f"file:{path}"], "line": getattr(node, "lineno", 1),
            # Keep the file key for older consumers; graph prioritization needs
            # a precise symbol location and must never fan out from file alone.
            "locations": [{"path": path, **({"symbol": symbol} if symbol else {})}],
            "summary": summary, "recommendation": recommendation}


def _analyze(path: str, text: str) -> list[dict] | None:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return []
    found = []
    # Bound all subsequent walks. An oversized syntax tree is unknown rather
    # than an invitation to spend unbounded time analyzing generated code.
    node_count = 0
    for _ in ast.walk(tree):
        node_count += 1
        if node_count > MAX_AST_NODES:
            return None
    functools_modules, functools_names = _functools_imports(tree)
    shadowed_builtins = _shadowed_builtin_names(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def symbol_at(node):
        scopes = []
        parent = node
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scopes.append(parent.name)
            parent = parents.get(parent)
        return ".".join(reversed(scopes)) or None

    def add(subject, kind, node, summary, recommendation, confidence="low"):
        finding = _finding(subject, kind, path, node, summary, recommendation,
                           symbol_at(node), confidence)
        found.append(finding)
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            outer_iter = _name(node.iter)
            if outer_iter:
                for child in ast.walk(node):
                    if child is node:
                        continue
                    if isinstance(child, (ast.For, ast.AsyncFor)) and _name(child.iter) == outer_iter:
                        add("performance", "repeated_nested_iteration", child,
                            f"Nested loop iterates `{outer_iter}` again; work may grow quadratically.",
                            "Check whether the inner scan can use a set, map, or precomputed index.",
                            confidence="low")
                        break
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            broad = isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
            # Restrict this candidate to an empty handler: a return or continue
            # elsewhere in a handler does not imply the exception was unreported.
            silently_exits = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            if broad and silently_exits:
                add("failure-modes", "silently_suppressed_exception", node,
                    "A broad exception handler suppresses the error or exits without reporting it.",
                    "Preserve the failure context or catch the expected exception types explicitly.")
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                add("failure-modes", "silently_suppressed_exception", node,
                    "A bare exception handler suppresses every exception and exits without reporting it.",
                    "Catch the expected exception types and preserve unexpected failures.")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
            for default in defaults:
                if _mutable_default(default, shadowed_builtins):
                    add("data-structures", "mutable_default_argument", node,
                        "A mutable object is used as a function default and is shared between calls.",
                        "Use `None` as the default and create a fresh object inside the function.")
                    break
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "list" and node.args and isinstance(node.args[0], ast.Call) \
                    and isinstance(node.args[0].func, ast.Name) and node.args[0].func.id == "range":
                add("scalability", "materialized_range", node,
                    "A range is eagerly materialized into a list, using memory proportional to its length.",
                    "Iterate over the range directly unless the full list is required.",
                    confidence="medium")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in {"sorted", "tuple", "set"} and node.args \
                and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Name) \
                and node.args[0].func.id in {"range", "enumerate"}:
            add("scalability", "eager_sequence_conversion", node,
                f"`{node.func.id}` eagerly consumes an iteration, potentially retaining all items in memory.",
                "Stream the values or process them in bounded batches when the full result is unnecessary.",
                confidence="low")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _unbounded_cache(decorator, functools_modules, functools_names)
                for decorator in node.decorator_list):
            add("scalability", "unbounded_function_cache", node,
                "A function uses an unbounded cache, so distinct calls can retain results for the process lifetime.",
                "Use a bounded `lru_cache(maxsize=...)` or an explicit eviction policy when inputs can grow.",
                confidence="low")
    # Keep this candidate family isolated from the broad pattern loop above;
    # the file-level traversal still owns parsing limits and coverage reporting.
    found.extend(failure_modes.analyze(path, text))
    # Give repeated rules in each symbol distinct identities without using lines.
    # A new issue in another function cannot renumber existing findings.
    ordinals = {}
    for finding in found:
        kind = finding["type"]
        symbol = finding["locations"][0].get("symbol", "<module>")
        scope = (kind, symbol)
        ordinals[scope] = ordinals.get(scope, 0) + 1
        finding["rule_key"] = f"{kind}:{symbol}:{ordinals[scope]}"
        finding["stable_key"] = f"static:{path}:{kind}:{symbol}:{ordinals[scope]}"
    return found


def analyze(root, *, budget_s=60):
    """Return static findings and coverage for supported Python files."""
    root = Path(root).resolve()
    deadline = time.monotonic() + max(0.0, float(budget_s))
    findings = []
    checked, skipped = [], []
    total_bytes = 0
    files_seen = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(current) / d).is_symlink())
        for name in sorted(files):
            if time.monotonic() >= deadline:
                skipped.append({"reason": "time budget exhausted before all Python files were checked"})
                return findings, checked, skipped
            if not name.endswith(".py"):
                continue
            if files_seen >= MAX_FILES:
                skipped.append({"reason": f"Python file limit reached ({MAX_FILES}); remaining files not checked"})
                return findings, checked, skipped
            files_seen += 1
            file_path = Path(current) / name
            if file_path.is_symlink():
                skipped.append({"path": file_path.relative_to(root).as_posix(),
                                "reason": "symlinked source is outside static scan scope"})
                continue
            rel = file_path.relative_to(root).as_posix()
            try:
                text, size = _read_text(
                    file_path, min(MAX_SOURCE_BYTES, MAX_TOTAL_BYTES - total_bytes), root=root)
            except ValueError:
                skipped.append({"path": rel, "reason": "source or total read limit reached"})
                continue
            except (OSError, UnicodeError) as exc:
                skipped.append({"path": rel, "reason": f"source unavailable: {type(exc).__name__}"})
                continue
            checked.append(rel)
            total_bytes += size
            try:
                ast.parse(text, filename=rel)
            except (SyntaxError, ValueError):
                checked.pop()
                skipped.append({"path": rel, "reason": "Python source could not be parsed; findings are unknown"})
                continue
            analyzed = _analyze(rel, text)
            if analyzed is None:
                checked.pop()
                skipped.append({"path": rel, "reason": f"AST exceeds {MAX_AST_NODES} node analysis limit; findings are unknown"})
                continue
            findings.extend(analyzed)
    return findings, checked, skipped
