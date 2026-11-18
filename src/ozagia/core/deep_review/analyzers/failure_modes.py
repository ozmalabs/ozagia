"""Narrow, syntax-only failure mode candidates for Python source.

This module intentionally detects only obvious unbounded retry loops. It does
not infer network or I/O behavior from arbitrary call names: it cannot establish
that an operation can fail, that a retry is reached, or whether surrounding
code supplies a deadline. It never imports or executes reviewed code.
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
    """Return candidates for unconditional retry loops in one Python file.

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
