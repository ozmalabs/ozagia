"""Read-only static checks for explicitly linked Python/Kubernetes config."""
from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ozagia.core import federation

MAX_SOURCE_BYTES = 2 * 1024 * 1024


def _inside_file(root: Path, relative: str) -> Path | None:
    """Resolve a repo-relative file while rejecting symlinks and escapes."""
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(part in {"..", ""} for part in rel.parts):
        return None
    candidate = root
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _read_text(root: Path, relative: str) -> tuple[str | None, str | None]:
    path = _inside_file(root, relative)
    if path is None:
        return None, "source path is missing, unsafe, or outside the repository"
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return None, "source exceeds 2 MiB limit"
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError):
        return None, "source could not be read as UTF-8"


def _python_env(text: str) -> tuple[set[str], list[str]]:
    """Return direct-indexed env names and bounded uncertainty reasons."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return set(), ["Python source could not be parsed"]
    names: set[str] = set()
    unknown: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and _is_os_environ(node.value)):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value:
                names.add(key.value)
            else:
                unknown.append("dynamic os.environ subscript")
        if isinstance(node, ast.Call):
            func = node.func
            optional_lookup = (
                isinstance(func, ast.Attribute) and func.attr == "get"
                and _is_os_environ(func.value)
            ) or (
                isinstance(func, ast.Attribute) and func.attr == "getenv"
                and isinstance(func.value, ast.Name) and func.value.id == "os"
            )
            if optional_lookup and node.args and not (
                    isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                unknown.append("dynamic optional environment lookup")
    return names, list(dict.fromkeys(unknown))


def _is_os_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _load_documents(path: Path, text: str) -> tuple[list[Any] | None, str | None]:
    try:
        from ozagia.core.kubernetes_graph import KubernetesParseError, _documents
    except ImportError:
        return None, "Kubernetes parser unavailable"
    try:
        documents = _documents(text, path.as_posix(), max_documents=100)
    except KubernetesParseError as exc:
        reason = str(exc)
        if "YAML parser unavailable" in reason:
            reason = "YAML parser unavailable; install the iac-yaml extra"
        return None, f"Kubernetes source is invalid or unsupported ({reason})"
    return documents, None


def _pod_spec(document: dict[str, Any], kind: str) -> dict[str, Any] | None:
    spec = document.get("spec")
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    template = spec.get("template")
    pod = template.get("spec") if isinstance(template, dict) else None
    return pod if isinstance(pod, dict) else None


def _manifest_env(path: Path, text: str, expected: dict[str, Any]
                  ) -> tuple[set[str] | None, list[str]]:
    docs, error = _load_documents(path, text)
    if error:
        return None, [error]
    assert docs is not None
    pointer = expected.get("pointer")
    selected: dict[str, Any] | None = None
    if isinstance(pointer, str):
        prefix, _, raw_index = pointer.partition("/documents/")
        if prefix != "" or not raw_index.isdigit():
            return None, ["linked Kubernetes source pointer is invalid"]
        index = int(raw_index)
        if index >= len(docs) or not isinstance(docs[index], dict):
            return None, ["linked Kubernetes source pointer is unavailable"]
        selected = docs[index]
    matches = []
    for doc in docs if selected is None else [selected]:
        if not isinstance(doc, dict):
            continue
        metadata = doc.get("metadata")
        if (doc.get("kind") == expected.get("kind")
                and isinstance(metadata, dict)
                and metadata.get("name") == expected.get("name")
                and (not expected.get("namespace")
                     or metadata.get("namespace") == expected.get("namespace"))):
            matches.append(doc)
    if len(matches) != 1:
        return None, ["linked Kubernetes resource is missing or ambiguous in current source"]
    pod = _pod_spec(matches[0], str(expected.get("kind", "")))
    if pod is None:
        return None, ["linked resource has no statically readable pod spec"]
    containers = []
    for key in ("containers", "initContainers", "ephemeralContainers"):
        values = pod.get(key, [])
        if values is not None and not isinstance(values, list):
            return None, ["container declaration is dynamic or malformed"]
        containers.extend(value for value in values or [] if isinstance(value, dict))
    if len(containers) != 1:
        return None, ["multiple or missing containers make env ownership ambiguous"]
    container = containers[0]
    if container.get("envFrom"):
        return None, ["envFrom may provide variables not visible in this manifest"]
    env = container.get("env", [])
    if not isinstance(env, list):
        return None, ["container env declaration is dynamic or malformed"]
    names: set[str] = set()
    for item in env:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None, ["container env contains a dynamic or malformed name"]
        names.add(item["name"])
    return names, []


def _member_map(members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(member.get("id")): member for member in members}


def check(root: str | os.PathLike[str],
          roots: Iterable[str | os.PathLike[str]] | None = None) -> dict[str, Any]:
    """Compare linked Python env lookups with one linked Kubernetes workload.

    Only explicit federation links with relation ``configured_by`` participate.
    The report describes current source observations, not deployed runtime state.
    """
    selected = Path(root).expanduser().resolve()
    selected_roots = tuple(roots) if roots is not None else None
    report = federation.discover(selected, selected_roots)
    members = _member_map(report.get("members", []))
    links = report.get("links", {})
    errors = list(links.get("errors", [])) + list(links.get("member_errors", []))
    selected_links = [link for link in links.get("links", [])
                      if link.get("type") == "configured_by"]
    results: list[dict[str, Any]] = []
    for link in selected_links:
        source_key, target_key = link.get("source"), link.get("target")
        source = federation.node(selected, str(source_key), selected_roots)
        target = federation.node(selected, str(target_key), selected_roots)
        base = {"source": source_key, "target": target_key,
                "relation": "configured_by", "provenance": link.get("provenance"),
                "source_manifest": link.get("source_manifest")}
        if not source.get("ok") or not target.get("ok"):
            results.append({**base, "status": "unknown",
                            "reasons": ["linked snapshot node could not be inspected"]})
            continue
        if source.get("kind") != "File" or target.get("kind") != "InfraResource":
            results.append({**base, "status": "unknown",
                            "reasons": ["link endpoints must be a Python File and InfraResource"]})
            continue
        target_attrs = target.get("attrs", {})
        if target_attrs.get("family") != "kubernetes":
            results.append({**base, "status": "unknown",
                            "reasons": ["target is not a Kubernetes infrastructure resource"]})
            continue
        source_id, target_id = str(source_key).partition("::")[0], str(target_key).partition("::")[0]
        source_member, target_member = members.get(source_id), members.get(target_id)
        if not source_member or not target_member:
            results.append({**base, "status": "unknown",
                            "reasons": ["linked repository membership is unavailable"]})
            continue
        freshness = {"source": source_member.get("state"), "target": target_member.get("state")}
        source_path = source.get("attrs", {}).get("path")
        target_path = target_attrs.get("path")
        reasons: list[str] = []
        if not isinstance(source_path, str) or not source_path.endswith(".py"):
            reasons.append("source node is not a Python file")
        if not isinstance(target_path, str):
            reasons.append("infrastructure source path is unavailable")
        if reasons:
            results.append({**base, "status": "unknown", "reasons": reasons,
                            "freshness": freshness})
            continue
        code_text, error = _read_text(Path(source_member["root"]), source_path)
        if error:
            results.append({**base, "status": "unknown", "reasons": [error],
                            "freshness": freshness})
            continue
        required, uncertain = _python_env(code_text or "")
        if not required:
            uncertain.append("no required literal os.environ lookups found")
        manifest_path = _inside_file(Path(target_member["root"]), target_path)
        if manifest_path is None:
            results.append({**base, "status": "unknown",
                            "reasons": ["infrastructure source path is missing, unsafe, or outside the repository"],
                            "required_variables": sorted(required), "freshness": freshness})
            continue
        try:
            if manifest_path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("source exceeds 2 MiB limit")
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            results.append({**base, "status": "unknown", "reasons": [str(exc) or "source could not be read"],
                            "required_variables": sorted(required), "freshness": freshness})
            continue
        declared, manifest_reasons = _manifest_env(manifest_path, manifest_text, target_attrs)
        reasons.extend(uncertain)
        reasons.extend(manifest_reasons)
        if declared is None:
            results.append({**base, "status": "unknown", "reasons": list(dict.fromkeys(reasons)),
                            "required_variables": sorted(required), "freshness": freshness,
                            "source_paths": {"code": source_path, "infrastructure": target_path}})
            continue
        missing = sorted(required - declared)
        # Snapshot freshness is always stated separately. Even a matching HEAD
        # cannot establish that the indexed graph reflects the current tree.
        state = "unknown" if reasons else ("observed_missing" if missing else "observed_match")
        results.append({**base, "status": state, "required_variables": sorted(required),
                        "declared_variables": sorted(declared), "missing_variables": missing,
                        "reasons": list(dict.fromkeys(reasons)), "freshness": freshness,
                        "source_paths": {"code": source_path, "infrastructure": target_path}})
    if not selected_links:
        errors.append("no explicitly declared configured_by links were available")
    return {"ok": True, "status": "unknown" if errors or not results
            or any(item["status"] == "unknown" for item in results)
            or any(state != "fresh" for item in results
                   for state in item.get("freshness", {}).values())
            else "observed",
            "root": str(selected), "results": results,
            "errors": list(dict.fromkeys(errors)),
            "disclaimer": "Current-source observations only; this does not verify deployed configuration."}


__all__ = ["check"]
