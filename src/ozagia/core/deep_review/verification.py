"""Deterministic corroboration of model review candidates.

This module deliberately uses a narrow evidence rule: a model candidate is
corroborated only when it cites a deterministic finding with the same subject
and exact rule/type key. This establishes that a deterministic check found the
same rule at the cited location; it does not prove the model's prose or impact
description. Consequently this module never turns ``truth_assessed`` on and
never changes the canonical deterministic findings.
"""
from __future__ import annotations

import ast
import hashlib
import os
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _key(value: Any) -> str:
    """Return a conservative key for machine identifiers (no fuzzy matching)."""
    return value.strip() if isinstance(value, str) else ""


def verify_model_candidates(
        candidates: Iterable[Mapping[str, Any]],
        deterministic_findings: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return per-candidate deterministic corroboration decisions.

    Promotion is limited to ``corroborated`` when all of these match exactly:
    candidate subject, candidate type, a cited deterministic finding ID, and
    that finding's subject/type. Candidate wording, severity and model
    confidence are never used to raise deterministic severity or truth status.
    Missing, ambiguous, or mismatched citations remain ``unverified``.
    Inputs are not modified.
    """
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for finding in deterministic_findings:
        if not isinstance(finding, Mapping):
            continue
        identifier = _key(finding.get("id"))
        if identifier:
            by_id.setdefault(identifier, []).append(finding)

    decisions: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = _key(candidate.get("id"))
        subject = _key(candidate.get("subject"))
        issue_type = _key(candidate.get("type"))
        cited: set[str] = set()
        evidence_rows = candidate.get("evidence")
        if isinstance(evidence_rows, list):
            for evidence in evidence_rows:
                if not isinstance(evidence, Mapping):
                    continue
                ids = evidence.get("citation_ids")
                if isinstance(ids, list):
                    cited.update(_key(value) for value in ids if _key(value))
        matches: list[str] = []
        for citation in sorted(cited):
            rows = by_id.get(citation, [])
            # Duplicate IDs make the evidence identity ambiguous, even if one
            # of the duplicate rows happens to match.
            if len(rows) != 1:
                continue
            finding = rows[0]
            if (_key(finding.get("subject")) == subject
                    and _key(finding.get("type")) == issue_type
                    and subject and issue_type):
                matches.append(citation)
        state = "corroborated" if matches else "unverified"
        decisions.append({
            "candidate_id": candidate_id,
            "state": state,
            "matched_finding_ids": matches,
            "promotion": ("attach_deterministic_evidence" if matches else None),
            "truth_assessed": False,
            "reason": ("exact subject/type match to a unique cited deterministic finding"
                       if matches else
                       "no unique cited deterministic finding has the same subject and type"),
        })
    return {
        "schema": "ozagia.deep_review.model_verification.v1",
        "decisions": decisions,
        "corroborated": sum(row["state"] == "corroborated" for row in decisions),
        "unverified": sum(row["state"] == "unverified" for row in decisions),
        "truth_assessed": False,
    }


PYTHON_ASSERTION_SCHEMA = "ozagia.deep_review.assertion.v1"
MAX_ASSERTION_SOURCE_BYTES = 2 * 1024 * 1024


def _mutable_default(node: ast.expr) -> bool:
    """Recognize literal containers without relying on shadowable names."""
    return isinstance(node, (ast.List, ast.Dict, ast.Set))


def _has_mutable_default(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    defaults = list(args.defaults)
    for argument, default in zip(positional[len(positional) - len(defaults):], defaults):
        if _mutable_default(default):
            return True
    return any(default is not None and _mutable_default(default)
               for default in args.kw_defaults)


class _ScopeBindings(ast.NodeVisitor):
    """Conservatively count bindings in one lexical scope, including branches."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.wildcard_import = False

    def _add(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add(node.name)
        for expression in (*node.decorator_list, *node.args.defaults,
                           *(item for item in node.args.kw_defaults if item is not None)):
            self.visit(expression)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node.name)
        for expression in (*node.decorator_list, *node.bases,
                           *(keyword.value for keyword in node.keywords)):
            self.visit(expression)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.asname or alias.name.split('.')[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == '*':
                self.wildcard_import = True
            else:
                self._add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._add(node.rest)
        self.generic_visit(node)


def _scope_bindings(body: list[ast.stmt]) -> _ScopeBindings:
    bindings = _ScopeBindings()
    for node in body:
        bindings.visit(node)
    return bindings


def _top_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Index only unambiguous module functions and direct class methods."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    definitions = _scope_bindings(tree.body)
    if definitions.wildcard_import:
        return found
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and definitions.counts[node.name] != 1):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            class_bindings = _scope_bindings(node.body)
            methods: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.setdefault(child.name, []).append(child)
            for name, matches in methods.items():
                if (len(matches) == 1 and not class_bindings.wildcard_import
                        and class_bindings.counts[name] == 1):
                    found[f"{node.name}.{name}"] = matches[0]
    return found


def _read_bounded_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, MAX_ASSERTION_SOURCE_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_ASSERTION_SOURCE_BYTES:
            return b"".join(chunks)


def _open_pinned_source(project_root: str | Path, rel: str) -> tuple[int, list[int]]:
    """Open a file beneath a pinned directory chain, refusing symlinks."""
    required = ("O_NOFOLLOW", "O_DIRECTORY")
    if (any(not hasattr(os, name) for name in required)
            or os.open not in os.supports_dir_fd):
        raise OSError("nofollow directory-relative open is unsupported")
    nofollow = os.O_NOFOLLOW
    directory = os.O_DIRECTORY
    root_fd = os.open(os.fspath(Path(project_root).absolute()),
                      os.O_RDONLY | directory | nofollow)
    opened = [root_fd]
    try:
        current_fd = root_fd
        parts = rel.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | directory | nofollow,
                              dir_fd=current_fd)
            opened.append(next_fd)
            current_fd = next_fd
        source_fd = os.open(parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                            dir_fd=current_fd)
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(source_fd)
            raise OSError("asserted source is not a regular file")
        opened.append(source_fd)
        return source_fd, opened
    except BaseException:
        for fd in reversed(opened):
            os.close(fd)
        raise


def python_source_sha256(project_root: str | Path, rel: str) -> str | None:
    """Return a bounded digest suitable for copying into a model assertion."""
    if (not isinstance(rel, str) or "\0" in rel or not rel.endswith(".py")
            or "\\" in rel
            or Path(rel).is_absolute()
            or any(part in {"", ".", ".."} for part in rel.split("/"))):
        return None
    try:
        fd, opened = _open_pinned_source(project_root, rel)
        try:
            data = _read_bounded_fd(fd)
            if len(data) > MAX_ASSERTION_SOURCE_BYTES:
                return None
            return hashlib.sha256(data).hexdigest()
        finally:
            for open_fd in reversed(opened):
                os.close(open_fd)
    except (OSError, ValueError):
        return None


def verify_python_mutable_default(
        project_root: str | Path,
        assertion: Mapping[str, Any],
) -> dict[str, Any]:
    """Check one bounded, hash-pinned Python mutable-default assertion.

    Contract fields: schema, kind (``python.mutable_default``), repository-
    relative ``path``, exact ``symbol`` (module function or Class.method),
    and lowercase SHA-256 of the complete source bytes. This checker reads
    source only, rejects symlinks through pinned no-follow file descriptors,
    imports no target code and performs no I/O beyond bounded local reads.
    """
    def refused(reason: str) -> dict[str, Any]:
        return {"state": "unverified", "assertion": "python.mutable_default",
                "reason": reason, "truth_assessed": False}

    if (assertion.get("schema") != PYTHON_ASSERTION_SCHEMA
            or assertion.get("kind") != "python.mutable_default"):
        return refused("unsupported assertion contract")
    rel = assertion.get("path")
    symbol = assertion.get("symbol")
    expected_hash = assertion.get("source_sha256")
    if (not isinstance(rel, str) or not rel or "\\" in rel
            or Path(rel).is_absolute() or any(part in {"", ".", ".."}
                                               for part in rel.split("/"))
            or not rel.endswith(".py")):
        return refused("path must be a normalized repository-relative path")
    if (not isinstance(symbol, str) or not symbol
            or any(not part.isidentifier() for part in symbol.split("."))
            or len(symbol.split(".")) > 2):
        return refused("symbol must be an exact module function or Class.method")
    if (not isinstance(expected_hash, str) or len(expected_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_hash)):
        return refused("source_sha256 must be a lowercase SHA-256 digest")

    try:
        source_fd, opened = _open_pinned_source(project_root, rel)
        try:
            source = _read_bounded_fd(source_fd)
            if len(source) > MAX_ASSERTION_SOURCE_BYTES:
                return refused("source exceeds assertion size limit")
            first_hash = hashlib.sha256(source).hexdigest()
            if first_hash != expected_hash:
                return refused("source hash does not match assertion")
            tree = ast.parse(source, filename=rel)
            matched = _top_level_functions(tree).get(symbol)
            if matched is None or not _has_mutable_default(matched):
                return refused("assertion does not match the exact source symbol")
            # Re-read the same pinned descriptor to detect in-place edits.
            os.lseek(source_fd, 0, os.SEEK_SET)
            reread = _read_bounded_fd(source_fd)
            if (len(reread) > MAX_ASSERTION_SOURCE_BYTES
                    or hashlib.sha256(reread).hexdigest() != first_hash):
                return refused("source changed during verification")
        finally:
            for fd in reversed(opened):
                os.close(fd)
    except (OSError, UnicodeError, SyntaxError, ValueError, RuntimeError):
        return refused("source could not be safely parsed")
    return {"state": "verified_assertion", "assertion": "python.mutable_default",
            "path": rel, "symbol": symbol, "source_sha256": first_hash,
            "verified_fact": "the exact function has a direct mutable default",
            "truth_assessed": False}
