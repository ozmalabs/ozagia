"""Offline, bounded static checks for Terraform/OpenTofu source.

This module only reads source files and the local filesystem. It never invokes
Terraform/OpenTofu, providers, plugins, module installers, or project code.
Returned findings contain identifiers and source locations only, not HCL values.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any

from ozagia.core.terraform_graph import (
    index_terraform_text,
    merge_terraform_graphs,
)

MAX_FILES = 500
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
_SUPPORTED_SUFFIXES = (".tf", ".tf.json", ".tofu", ".tofu.json")
_SKIP_DIRS = frozenset({
    ".git", ".ozagia", ".terraform", ".terragrunt-cache", "vendor",
})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class IacCheckFinding:
    path: str
    line: int
    kind: str
    message: str
    evidence: str


@dataclass(frozen=True)
class IacCheckResult:
    status: str
    findings: tuple[IacCheckFinding, ...]
    diagnostics: tuple[str, ...]
    files_checked: int


def _discover(root: Path) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    diagnostics: list[str] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            name for name in dirs
            if name not in _SKIP_DIRS and not name.startswith(".")
            and not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            if not name.lower().endswith(_SUPPORTED_SUFFIXES):
                continue
            candidate = current_path / name
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                diagnostics.append("source path escapes root; file skipped")
                continue
            files.append(candidate)
            if len(files) > MAX_FILES:
                diagnostics.append("IaC file count exceeds 500; remaining files skipped")
                return files[:MAX_FILES], diagnostics
    return files, diagnostics


def _mask_hcl(text: str) -> str:
    """Mask comments, strings and heredocs while preserving offsets/newlines."""
    out = list(text)
    i = 0
    while i < len(text):
        if text[i] == '"':
            start = i
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            else:
                return ""
            for j in range(start, i):
                if text[j] != "\n":
                    out[j] = " "
            continue
        if text.startswith("<<", i):
            match = re.match(r"<<(-?)[ \t]*([A-Za-z_][A-Za-z0-9_-]*)", text[i:])
            if match:
                after = i + match.end()
                end = text.find("\n", after)
                if end >= 0:
                    delimiter = re.compile(
                        rf"[ \t]*{re.escape(match.group(2))}[ \t]*"
                        if match.group(1) else rf"{re.escape(match.group(2))}[ \t]*"
                    )
                    line_start = end + 1
                    while line_start <= len(text):
                        line_end = text.find("\n", line_start)
                        line_end = len(text) if line_end < 0 else line_end
                        if delimiter.fullmatch(text[line_start:line_end].rstrip("\r")):
                            end = min(len(text), line_end + (line_end < len(text)))
                            for j in range(i, end):
                                if text[j] != "\n":
                                    out[j] = " "
                            i = end
                            break
                        if line_end == len(text):
                            i = len(text)
                            break
                        line_start = line_end + 1
                    continue
        if text.startswith("#", i) or text.startswith("//", i):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            for j in range(i, end):
                out[j] = " "
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return ""
            end += 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def _required_providers_hcl(text: str) -> set[str]:
    clean = _mask_hcl(text)
    if not clean:
        return set()
    requirements: set[str] = set()
    for match in re.finditer(r"\brequired_providers\b", clean):
        opening = clean.find("{", match.end())
        if opening < 0:
            continue
        depth = 1
        end = opening + 1
        while end < len(clean) and depth:
            if clean[end] == "{":
                depth += 1
            elif clean[end] == "}":
                depth -= 1
            end += 1
        if depth:
            continue
        body = clean[opening + 1:end - 1]
        level = 0
        for line in body.splitlines():
            if level == 0:
                assignment = re.match(r"[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=", line)
                if assignment:
                    requirements.add(assignment.group(1))
            level += line.count("{") - line.count("}")
    return requirements


def _required_providers_json(text: str) -> set[str]:
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return set()
    terraform = document.get("terraform", {}) if isinstance(document, dict) else {}
    if isinstance(terraform, list):
        terraform = terraform[0] if terraform and isinstance(terraform[0], dict) else {}
    if not isinstance(terraform, dict):
        return set()
    required = terraform.get("required_providers", {})
    return {key for key in required if isinstance(key, str)} if isinstance(required, dict) else set()


def _provider_uses_hcl(text: str) -> set[str]:
    clean = _mask_hcl(text)
    return {
        match.group(1)
        for match in re.finditer(
            r"\bprovider[ \t]*=[ \t]*([A-Za-z_][A-Za-z0-9_-]*)(?:\.[A-Za-z_][A-Za-z0-9_-]*)?",
            clean,
        )
    }


def _provider_uses_json(text: str) -> set[str]:
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return set()
    names: set[str] = set()
    for group in ("resource", "data", "ephemeral"):
        entries = document.get(group, {}) if isinstance(document, dict) else {}
        if not isinstance(entries, dict):
            continue
        for by_name in entries.values():
            if not isinstance(by_name, dict):
                continue
            for body in by_name.values():
                if isinstance(body, dict) and isinstance(body.get("provider"), str):
                    names.add(body["provider"].split(".", 1)[0])
    return names


def _line_for_reference(text: str, reference: str, fallback: int) -> int:
    """Find a bounded exact source occurrence; fallback is the declaration line."""
    if not reference or len(reference) > 256:
        return fallback
    match = re.search(rf"(?<![A-Za-z0-9_-]){re.escape(reference)}(?![A-Za-z0-9_-])", text)
    return text.count("\n", 0, match.start()) + 1 if match else fallback


def _finding(path: str, line: int, kind: str, message: str) -> IacCheckFinding:
    return IacCheckFinding(path, max(1, line), kind, message, "static source analysis")


def check_iac(root: str | os.PathLike[str], memory: Any = None) -> IacCheckResult:
    """Check a local Terraform/OpenTofu root without running infrastructure tools.

    `memory` is accepted for callers that expose graph-backed checks, but this
    implementation deliberately uses only bounded source files and filesystem
    metadata.
    """
    del memory
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return IacCheckResult(
            "unsupported", (), ("IaC root is not a directory",), 0
        )
    paths, diagnostics = _discover(root_path)
    loaded: list[tuple[str, str, str]] = []
    total_bytes = 0
    for path in paths:
        try:
            relative = path.resolve().relative_to(root_path).as_posix()
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                diagnostics.append(f"{relative}: source exceeds 2 MiB limit")
                continue
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                diagnostics.append("IaC source exceeds 20 MiB total limit")
                break
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            diagnostics.append("source file could not be read as UTF-8; file skipped")
            continue
        scope = PurePosixPath(relative).parent.as_posix()
        loaded.append((relative, scope, text))

    graphs = [
        index_terraform_text(relative, text, module_scope=scope)
        for relative, scope, text in loaded
    ]
    graph = merge_terraform_graphs(graphs)
    findings: list[IacCheckFinding] = []
    for path, message in graph.errors:
        diagnostics.append(f"{path}: {message}")

    nodes = {node.key: node for node in graph.nodes}
    source_by_path = {path: (scope, text) for path, scope, text in loaded}
    duplicates: dict[tuple[str, str, str], int] = {}
    for node in graph.nodes:
        key = (node.module_scope, node.kind, node.address)
        duplicates[key] = duplicates.get(key, 0) + 1

    for edge in graph.unresolved:
        if edge.relation != "references":
            continue
        source_node = nodes.get(edge.source)
        if source_node is None:
            continue
        scope_text = source_by_path.get(source_node.path)
        text = scope_text[1] if scope_text else ""
        line = _line_for_reference(text, edge.evidence, source_node.start_line)
        parts = edge.evidence.split(".")
        if parts[0] == "var" and len(parts) > 1:
            kind, message = "undeclared_variable", "reference to an undeclared input variable"
        elif parts[0] == "local" and len(parts) > 1:
            kind, message = "undeclared_local", "reference to an undeclared local value"
        else:
            kind, message = "unresolved_reference", "source reference does not resolve statically"
        ambiguous = any(
            duplicates.get((source_node.module_scope, candidate_kind, ".".join(parts[1:stop])), 0) > 1
            for candidate_kind in ("local", "variable", "module", "resource", "data", "output")
            for stop in range(len(parts), 1, -1)
        )
        if ambiguous:
            kind, message = "ambiguous_reference", "source reference has multiple declarations"
        findings.append(_finding(source_node.path, line, kind, message))

    # Local module sources are allowlisted by the graph parser and are checked
    # with path containment before consulting the filesystem.
    for node in graph.nodes:
        if node.kind != "module" or not node.module_source:
            if node.kind == "module":
                diagnostics.append(
                    f"{node.path}: module source is not statically available"
                )
            continue
        source = node.module_source
        if not source.startswith(("./", "../")):
            continue
        parent = PurePosixPath(node.path).parent.as_posix()
        target = posixpath.normpath(posixpath.join(parent, source))
        if target in ("", ".", "..") or target.startswith("../") or target.startswith("/"):
            findings.append(_finding(
                node.path, node.start_line, "unsafe_module_source",
                "local module source escapes the IaC root",
            ))
            continue
        target_path = (root_path / target).resolve()
        try:
            target_path.relative_to(root_path)
        except ValueError:
            findings.append(_finding(
                node.path, node.start_line, "unsafe_module_source",
                "local module source escapes the IaC root",
            ))
            continue
        if not target_path.is_dir():
            findings.append(_finding(
                node.path, node.start_line, "missing_local_module",
                "local module source directory does not exist",
            ))

    providers_by_scope: dict[str, set[str]] = {}
    used_by_scope: dict[str, set[str]] = {}
    for relative, scope, text in loaded:
        suffix = relative.lower()
        required = (_required_providers_json(text) if suffix.endswith(".json")
                    else _required_providers_hcl(text))
        providers_by_scope.setdefault(scope, set()).update(required)
        provider_uses = (_provider_uses_json(text) if suffix.endswith(".json")
                         else _provider_uses_hcl(text))
        if provider_uses:
            used_by_scope.setdefault(scope, set()).update(provider_uses)
    for node in graph.nodes:
        used: str | None = None
        if node.kind in ("resource", "data", "ephemeral"):
            address = node.address
            if node.kind in ("data", "ephemeral"):
                prefix = f"{node.kind}."
                if address.startswith(prefix):
                    address = address[len(prefix):]
            used = address.split(".", 1)[0].split("_", 1)[0]
        elif node.kind == "provider":
            used = node.address.split(".", 1)[0]
        if used and _IDENT.fullmatch(used):
            used_by_scope.setdefault(node.module_scope, set()).add(used)
    for edge in (*graph.edges, *graph.unresolved):
        target = nodes.get(edge.target)
        if edge.relation == "references" and target is not None and target.kind == "provider":
            used_by_scope.setdefault(target.module_scope, set()).add(
                target.address.split(".", 1)[0]
            )
    node_for_scope = {
        scope: min(
            (node for node in graph.nodes if node.module_scope == scope),
            key=lambda node: (node.path, node.start_line),
        )
        for scope in used_by_scope
    }
    for scope, used in used_by_scope.items():
        missing = sorted(used - providers_by_scope.get(scope, set()))
        node = node_for_scope.get(scope)
        if node is None:
            continue
        for _provider_name in missing:
            findings.append(_finding(
                node.path, node.start_line, "missing_provider_requirement",
                "used provider has no required_providers declaration",
            ))

    # Any parser/limit diagnostic means some source was not statically checked.
    diagnostics = list(dict.fromkeys(diagnostics))
    findings = list({
        (finding.path, finding.line, finding.kind, finding.message): finding
        for finding in findings
    }.values())
    findings.sort(key=lambda finding: (finding.path, finding.line, finding.kind))
    if diagnostics:
        status = "unsupported"
    elif any(finding.kind == "ambiguous_reference" for finding in findings):
        status = "ambiguous"
    elif findings:
        status = "findings"
    else:
        status = "clean"
    return IacCheckResult(status, tuple(findings), tuple(diagnostics), len(loaded))


__all__ = ["IacCheckFinding", "IacCheckResult", "check_iac"]
