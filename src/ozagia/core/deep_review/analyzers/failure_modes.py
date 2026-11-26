"""Narrow, syntax-only failure mode candidates for Python source.

This module intentionally detects only obvious unbounded retry loops and
broad exception handlers that return literal defaults. It cannot establish
whether fallback behavior is intentional, whether an operation can fail, or
whether surrounding code supplies a deadline. It never imports or executes
reviewed code.
"""
from __future__ import annotations

import ast


def _symbol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    names = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or None


def analyze(path: str, text: str) -> list[dict]:
    """Return narrow retry and broad-exception fallback candidates.

    A candidate requires ``while True`` containing a ``try`` whose exception
    handler explicitly continues the loop. Bounded retries and timeout checks
    that live outside the handler are not analyzed, so review the surrounding
    control flow before acting on a result.
    """
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return []
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    results = []
    broad_names = {"Exception", "BaseException"}

    def bindings(statements, parameters=()):
        """Collect bindings in one lexical scope, excluding nested scopes."""
        names = {arg.arg for arg in parameters}
        pending = list(statements)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
                # Decorators/defaults execute in the enclosing scope; bodies
                # have their own namespace and must not leak into this one.
                pending.extend(node.decorator_list)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pending.extend(value for value in
                                   node.args.defaults + node.args.kw_defaults
                                   if value is not None)
                    pending.extend(arg.annotation for arg in node.args.posonlyargs +
                                   node.args.args + node.args.kwonlyargs
                                   if arg.annotation is not None)
                    if node.args.vararg and node.args.vararg.annotation:
                        pending.append(node.args.vararg.annotation)
                    if node.args.kwarg and node.args.kwarg.annotation:
                        pending.append(node.args.kwarg.annotation)
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Name) and isinstance(
                    node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        names.update(broad_names)
                    else:
                        names.add(alias.asname or
                                  (alias.name.split(".")[0]
                                   if isinstance(node, ast.Import)
                                   else alias.name))
            pending.extend(ast.iter_child_nodes(node))
        return names & broad_names

    module_shadowed = bindings(tree.body)
    scope_shadowed = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            parameters = (args.posonlyargs + args.args + args.kwonlyargs +
                          ([args.vararg] if args.vararg else []) +
                          ([args.kwarg] if args.kwarg else []))
            scope_shadowed[node] = bindings(node.body, parameters)

    def symbol_for(node):
        names = []
        current = node
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)):
                names.append(current.name)
            current = parents.get(current)
        return ".".join(reversed(names)) or None

    def broad_handler(handler):
        if handler.type is None:
            return True
        if not isinstance(handler.type, ast.Name) or handler.type.id not in broad_names:
            return False
        current = parents.get(handler)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if handler.type.id in scope_shadowed.get(current, set()):
                    return False
            current = parents.get(current)
        return handler.type.id not in module_shadowed

    def literal(node):
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return all(literal(item) for item in node.elts)
        if isinstance(node, ast.Dict):
            return all(key is not None and literal(key) and literal(value)
                       for key, value in zip(node.keys, node.values))
        return False

    def default_return(handler):
        """Recognize literal fallback values that can mask a failed operation."""
        if not handler.body or not isinstance(handler.body[-1], ast.Return):
            return False
        value = handler.body[-1].value
        if value is None:
            return True
        return literal(value)

    for handler in (node for node in ast.walk(tree)
                    if isinstance(node, ast.ExceptHandler)):
        if broad_handler(handler) and default_return(handler):
            symbol = symbol_for(handler)
            result = {
                "section": "failure-modes",
                "type": "broad_exception_returns_default",
                "severity": "minor",
                "confidence": "low",
                "keys": [f"file:{path}"],
                "line": handler.lineno,
                "locations": [{"path": path, **({"symbol": symbol} if symbol else {})}],
                "summary": "A broad exception handler returns a default value, which may make a failed operation look successful.",
                "recommendation": "Preserve or report unexpected failures, and return a fallback only when it is an intentional part of the API.",
                "rule_key": f"broad_exception_returns_default:{symbol or '<module>'}:1",
                "stable_key": f"static:{path}:broad_exception_returns_default:{symbol or '<module>'}:1",
            }
            results.append(result)
    for loop in ast.walk(tree):
        if not isinstance(loop, ast.While) or not (
                isinstance(loop.test, ast.Constant) and loop.test.value is True):
            continue
        for handler in (n for stmt in loop.body for n in ast.walk(stmt)
                        if isinstance(n, ast.ExceptHandler)):
            continues_outer = False
            for stmt in handler.body:
                for candidate in ast.walk(stmt):
                    if not isinstance(candidate, ast.Continue):
                        continue
                    ancestor = parents.get(candidate)
                    while ancestor is not None and not isinstance(
                            ancestor, (ast.For, ast.AsyncFor, ast.While,
                                       ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.Lambda)):
                        ancestor = parents.get(ancestor)
                    if ancestor is loop:
                        continues_outer = True
                        break
                if continues_outer:
                    break
            if not continues_outer:
                continue
            symbol = _symbol(loop, parents)
            result = {
                "section": "failure-modes",
                "type": "unbounded_exception_retry",
                "severity": "minor",
                "confidence": "low",
                "keys": [f"file:{path}"],
                "line": loop.lineno,
                "locations": [{"path": path, **({"symbol": symbol} if symbol else {})}],
                "summary": "An exception handler continues an unconditional loop, which may retry forever.",
                "recommendation": "Bound retry attempts and use a deadline or capped backoff where retries are required.",
                "rule_key": f"unbounded_exception_retry:{symbol or '<module>'}:1",
                "stable_key": f"static:{path}:unbounded_exception_retry:{symbol or '<module>'}:1",
            }
            results.append(result)
            break
    return results
