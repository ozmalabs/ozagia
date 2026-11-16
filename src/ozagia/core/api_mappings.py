"""Explicit, read-only links between API contracts and code locations.

Mappings are assertions supplied by maintainers. This module never infers a
producer, consumer, or test relationship from names or source text.
"""
from __future__ import annotations

import json
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping


RELATIONS = {"produces_api", "consumes_api", "tests_api"}


def _operation_reference(value: str) -> tuple[str, str, str] | None:
    """Parse the stable ``repo:path#operationId`` operation namespace."""
    repo, sep, remainder = value.partition(":")
    path, hash_sep, operation_id = remainder.partition("#")
    if not sep or not hash_sep or not repo or not path or not operation_id:
        return None
    if any(char.isspace() for char in value):
        return None
    return repo, path, operation_id


def _contained_file(root: Path, rel: str) -> Path:
    """Resolve a repo-relative file and reject traversal and symlink escapes."""
    posix = PurePosixPath(rel)
    windows = PureWindowsPath(rel)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive or
            "\\" in rel or posix.as_posix() != rel or
            any(part in {"", ".", ".."} for part in rel.split("/"))):
        raise ValueError(f"mapping path is not a canonical relative path: {rel}")
    base = root.resolve(strict=True)
    candidate = (base / rel).resolve(strict=True)
    if not candidate.is_file() or not candidate.is_relative_to(base):
        raise ValueError(f"mapping path is outside repository or not a file: {rel}")
    return candidate


def index_api_mappings(builder: Any, text: str, source: str,
                       repo_roots: Mapping[str, str | Path]) -> dict[str, Any]:
    """Index version 1 explicit API mappings from a JSON registry.

    Each mapping has ``relation`` (``produces_api``, ``consumes_api`` or
    ``tests_api``), ``repo``, ``path``, ``symbol`` and ``api``. ``api`` is an
    ``repo:path#operationId`` and must identify exactly one already-indexed
    OpenAPI operation whose ``repo``, ``file`` and ``operation_id`` attributes
    match. Both code and contract paths must resolve beneath their declared
    repository roots. The mapping source and JSON pointer are attached to each
    relation node.
    """
    try:
        doc = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return {"valid": False, "mappings": 0, "errors": ["invalid_json"]}
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("mappings"), list):
        return {"valid": False, "mappings": 0, "errors": ["unsupported_format"]}
    roots = {name: Path(root) for name, root in repo_roots.items()}
    operations: dict[tuple[str, str, str], list[int]] = {}
    for node_id in builder.find_nodes("ApiOperation"):
        attrs = builder.attrs(node_id)
        op_repo, file, operation_id = (attrs.get(k) for k in ("repo", "file", "operation_id"))
        if isinstance(op_repo, str) and isinstance(file, str) and isinstance(operation_id, str) and operation_id:
            operations.setdefault((op_repo, file, operation_id), []).append(node_id)
    errors: list[str] = []
    count = 0
    for i, record in enumerate(doc["mappings"]):
        pointer = f"#/mappings/{i}"
        if not isinstance(record, dict):
            errors.append(f"{pointer}:not_object")
            continue
        relation, repo, path, symbol, api = (record.get(k) for k in
                                             ("relation", "repo", "path", "symbol", "api"))
        if not isinstance(relation, str) or relation not in RELATIONS or not all(
                isinstance(v, str) and v for v in (repo, path, symbol, api)):
            errors.append(f"{pointer}:invalid_fields")
            continue
        if repo not in roots:
            errors.append(f"{pointer}:unknown_repo")
            continue
        try:
            _contained_file(roots[repo], path)
        except (OSError, ValueError):
            errors.append(f"{pointer}:unsafe_or_missing_path")
            continue
        parsed_api = _operation_reference(api)
        if parsed_api is None:
            errors.append(f"{pointer}:invalid_api_reference")
            continue
        api_repo, api_path, operation_id = parsed_api
        if api_repo not in roots:
            errors.append(f"{pointer}:unknown_api_repo")
            continue
        try:
            _contained_file(roots[api_repo], api_path)
        except (OSError, ValueError):
            errors.append(f"{pointer}:unsafe_or_missing_api_path")
            continue
        matches = operations.get((api_repo, api_path, operation_id), [])
        if not matches:
            errors.append(f"{pointer}:api_operation_not_indexed")
            continue
        if len(matches) != 1:
            errors.append(f"{pointer}:api_operation_ambiguous")
            continue
        provenance = {"source": source, "pointer": pointer,
                      "assertion": "explicit maintainer mapping"}
        endpoint = builder.node("ApiCodeReference", {
            "repo": repo, "path": path, "symbol": symbol,
        }, key=("api_code_reference", f"{repo}:{path}#{symbol}"))
        link = builder.node("ApiMapping", {
            "relation": relation, "provenance": provenance,
            "inference": False,
        }, key=("api_mapping", f"{source}{pointer}"))
        builder.edge(link, relation, endpoint)
        builder.edge(link, "refers_to_api_operation", matches[0])
        count += 1
    return {"valid": True, "mappings": count, "errors": errors}
