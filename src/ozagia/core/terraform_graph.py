"""Bounded, source-only Terraform/OpenTofu structure extraction.

This deliberately stores declaration identifiers and references, never
arbitrary HCL attribute values. Captured literals are limited to an allowlisted
static module source path and structural Terraform backend identity fields
(backend type, workspace, bucket, key and region). It does not evaluate
expressions, load modules, or inspect state. The stdlib backend handles common
block syntax and Terraform JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import posixpath
from pathlib import PurePosixPath
import re

EXTRACTOR_VERSION = "terraform-source-v3"
MAX_FILE_BYTES = 2 * 1024 * 1024
SUPPORTED_SUFFIXES = (".tf", ".tf.json", ".tofu", ".tofu.json")
_BLOCK = re.compile(
    r"(?m)^\s*(terraform|provider|resource|data|ephemeral|variable|locals|"
    r"output|module|moved|import|removed|check)\b")
_REF = re.compile(r"\b([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*){1,5})\b")
_INTERPOLATION = re.compile(r"\$\{([^}]{1,256})\}")
_ADDR_PREFIX = {"variable": "var", "local": "local", "module": "module",
                "data": "data"}
_MODULE_SOURCE = re.compile(r"[A-Za-z0-9_./-]{1,256}\Z")
_PROVIDER_ALIAS = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SAFE_LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_MODULE_SOURCE_TARGETS = 256
_UNLABELED_BLOCKS = frozenset({"moved", "import", "removed"})
_ONE_LABEL_BLOCKS = frozenset({"provider", "variable", "output", "module", "check"})
_TWO_LABEL_BLOCKS = frozenset({"resource", "data", "ephemeral"})
_REF_PREFIXES = frozenset({
    "var", "local", "module", "data", "ephemeral", "each", "count", "path",
    "terraform", "self",
})


@dataclass(frozen=True)
class TerraformNode:
    key: str
    kind: str
    address: str
    path: str
    start_line: int
    end_line: int
    dialect: str
    module_scope: str
    source_digest: str
    extractor_version: str = EXTRACTOR_VERSION
    provenance: str = "declared"
    confidence: str = "low"
    module_source: str | None = None
    instance_mode: str | None = None
    remote_backend_type: str | None = None
    remote_workspace: str | None = None
    remote_bucket: str | None = None
    remote_key: str | None = None
    remote_region: str | None = None
    remote_dynamic: bool = False


@dataclass(frozen=True)
class TerraformEdge:
    source: str
    target: str
    relation: str
    provenance: str
    scope: str
    evidence: str
    confidence: str = "low"


@dataclass(frozen=True)
class TerraformBackend:
    path: str
    dialect: str
    backend_type: str
    workspace: str | None = None
    bucket: str | None = None
    key: str | None = None
    region: str | None = None
    dynamic: bool = False


@dataclass(frozen=True)
class TerraformGraph:
    nodes: tuple[TerraformNode, ...]
    edges: tuple[TerraformEdge, ...]
    unresolved: tuple[TerraformEdge, ...]
    errors: tuple[tuple[str, str], ...]
    backends: tuple[TerraformBackend, ...] = ()


def discover_terraform_files(
    root: str | __import__("os").PathLike[str],
    *,
    skip_dirs: frozenset[str] = frozenset({".git", ".ozagia", ".terraform", "vendor"}),
) -> tuple[str, ...]:
    """Return repository-relative eligible source paths; never include state/plans."""
    import os

    base = os.fspath(root)
    found: list[str] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith("."))
        for name in files:
            low = name.lower()
            if low.endswith(SUPPORTED_SUFFIXES) and not low.endswith(
                (".tfstate", ".tfstate.backup")
            ):
                found.append(
                    PurePosixPath(os.path.relpath(os.path.join(current, name), base)).as_posix()
                )
    return tuple(sorted(found))


def _heredoc_at(text: str, i: int):
    """Return a bounded heredoc opener at i.

    HCL has no shift operator, so ``<<`` outside a quoted string or comment is
    a heredoc opener. It may appear as a direct assignment value, inside a
    list, tuple or function call; masking must not depend on the enclosing
    expression shape.
    """
    if not text.startswith("<<", i):
        return None
    j = i + 2
    indented = j < len(text) and text[j] == "-"
    if indented:
        j += 1
    while j < len(text) and text[j] in " \t":
        j += 1
    delimiter = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", text[j:])
    if delimiter is None:
        return None
    name = delimiter.group(0)
    j += len(name)
    line_end = text.find("\n", j)
    if line_end < 0:
        line_end = len(text)
        trailing = text[j:line_end]
    else:
        trailing = text[j:line_end]
    trailing = trailing.rstrip("\r")
    if trailing.strip(" \t") and not trailing.lstrip(" \t").startswith(("#", "//")):
        return None
    return i, line_end, name, indented


def _heredoc_end(text: str, opener_end: int, delimiter: str, indented: bool):
    line_start = opener_end + 1 if opener_end < len(text) else opener_end
    pattern = re.compile(
        rf"[ \t]*{re.escape(delimiter)}[ \t]*"
        if indented
        else rf"{re.escape(delimiter)}[ \t]*"
    )
    while line_start < len(text):
        line_end = text.find("\n", line_start)
        if line_end < 0:
            line_end = len(text)
            next_start = len(text)
        else:
            next_start = line_end + 1
        line = text[line_start:line_end].rstrip("\r")
        if pattern.fullmatch(line):
            return line_start, line_end, next_start
        line_start = next_start
    return None


def _template_refs(text: str, start: int, end: int) -> list[tuple[int, str]]:
    """Expression references inside template interpolations in one range."""
    refs: list[tuple[int, str]] = []
    i = start
    while i < end:
        if (text.startswith("${", i)
                and (i == 0 or text[i - 1] != "$")):
            close = text.find("}", i + 2, min(end, i + 258))
            if close >= 0:
                refs.extend(
                    (i, ref) for ref in _expression_refs(
                        text[i + 2:close], _REF_PREFIXES))
                i = close + 1
                continue
        i += 1
    return refs


def _clean_hcl(text: str) -> tuple[str, bool, list[tuple[int, str]]]:
    # Preserve offsets/newlines while masking comments, strings and heredocs.
    # Interpolation expressions inside strings/heredocs are retained as safe
    # references with their offsets; literal template text is not retained.
    out = list(text)
    templates: list[tuple[int, str]] = []
    i = 0
    quote = False
    while i < len(text):
        if quote:
            if text.startswith("${", i) and (i == 0 or text[i - 1] != "$"):
                close = text.find("}", i + 2, min(len(text), i + 258))
                if close >= 0:
                    templates.extend(
                        (i, ref) for ref in _expression_refs(
                            text[i + 2:close], _REF_PREFIXES))
                    for k in range(i, close + 1):
                        if text[k] != "\n":
                            out[k] = " "
                    i = close + 1
                    continue
            if text[i] == "\\":
                if i + 1 < len(text):
                    out[i + 1] = " "
                out[i] = " "
                i += 2
                continue
            if text[i] == '"':
                quote = False
            if text[i] != "\n":
                out[i] = " "
            i += 1
            continue
        if text[i] == '"':
            quote = True
            out[i] = " "
            i += 1
            continue
        heredoc = _heredoc_at(text, i)
        if heredoc is not None:
            opener_start, opener_end, delimiter, indented = heredoc
            end = _heredoc_end(text, opener_end, delimiter, indented)
            if end is None:
                for k in range(opener_start, len(text)):
                    if text[k] != "\n":
                        out[k] = " "
                return "".join(out), True, templates
            _, _, next_i = end
            templates.extend(_template_refs(text, opener_start, next_i))
            for k in range(opener_start, next_i):
                if text[k] != "\n":
                    out[k] = " "
            i = next_i
            continue
        if text.startswith("//", i) or text[i] == "#":
            j = text.find("\n", i)
            if j < 0:
                j = len(text)
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = len(text) if j < 0 else j + 2
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out), False, templates


def _safe_provider_alias(value: object) -> str | None:
    if isinstance(value, str) and _PROVIDER_ALIAS.fullmatch(value):
        return value
    return None


def _safe_module_source(value: object) -> str | None:
    local_path = isinstance(value, str) and value.startswith(("./", "../"))
    first_component = value.split("/", 1)[0] if isinstance(value, str) else ""
    if (
        isinstance(value, str)
        and _MODULE_SOURCE.fullmatch(value)
        and not value.startswith("/")
        and (local_path or "." not in first_component)
    ):
        return value
    return None


def _hcl_literal_attr(raw_body: str, clean_body: str, attr: str) -> str | None:
    # Find the assignment in the comment/string-masked view, then read only a
    # simple literal from the same offsets in the original source.
    assignments = list(re.finditer(rf"\b{re.escape(attr)}[ \t]*=", clean_body))
    if len(assignments) != 1:
        return None
    i = assignments[0].end()
    while i < len(raw_body) and raw_body[i] in " \t":
        i += 1
    literal = re.match(r'"([^"\\\r\n]*)"', raw_body[i:])
    return literal.group(1) if literal is not None else None


_SAFE_BACKEND_VALUE = re.compile(r"[A-Za-z0-9_./:@+=,-]{1,256}\Z")


def _safe_backend_value(value: object) -> str | None:
    if isinstance(value, str) and _SAFE_BACKEND_VALUE.fullmatch(value):
        return value
    return None


def _json_remote_state(body: object) -> tuple[str | None, str | None,
                                               str | None, str | None,
                                               str | None, bool]:
    if not isinstance(body, dict):
        return None, None, None, None, None, True
    backend = _safe_backend_value(body.get("backend"))
    workspace = _safe_backend_value(body.get("workspace"))
    config = body.get("config")
    config = config if isinstance(config, dict) else {}
    bucket = _safe_backend_value(config.get("bucket"))
    key = _safe_backend_value(config.get("key"))
    region = _safe_backend_value(config.get("region"))
    dynamic = (backend is None or key is None
               or ("workspace" in body and workspace is None)
               or ("config" in body and not isinstance(body.get("config"), dict))
                or any(field in config and value is None
                       for field, value in (
                           ("bucket", bucket), ("key", key), ("region", region))))
    return backend, workspace, bucket, key, region, dynamic


def _hcl_block_end(clean: str, open_brace: int) -> int | None:
    depth = 1
    pos = open_brace + 1
    while pos < len(clean) and depth:
        if clean[pos] == "{":
            depth += 1
        elif clean[pos] == "}":
            depth -= 1
        pos += 1
    return pos if depth == 0 else None


def _dynamic_hcl_attrs(clean_body: str, attrs: tuple[str, ...],
                       values: dict[str, str | None]) -> bool:
    return any(
        values.get(attr) is None
        and re.search(rf"\b{re.escape(attr)}[ \t]*=", clean_body) is not None
        for attr in attrs
    )


def _hcl_remote_state(raw_body: str, clean_body: str
                      ) -> tuple[str | None, str | None, str | None,
                                 str | None, str | None, bool]:
    backend = _safe_backend_value(
        _hcl_literal_attr(raw_body, clean_body, "backend"))
    workspace = _safe_backend_value(
        _hcl_literal_attr(raw_body, clean_body, "workspace"))
    config_match = re.search(r"\bconfig\s*=\s*\{", clean_body)
    config_raw = config_clean = ""
    if config_match:
        open_brace = clean_body.find("{", config_match.start())
        end = _hcl_block_end(clean_body, open_brace)
        if end is not None:
            config_clean = clean_body[open_brace + 1:end - 1]
            config_raw = raw_body[open_brace + 1:end - 1]
    bucket = _safe_backend_value(
        _hcl_literal_attr(config_raw, config_clean, "bucket"))
    key = _safe_backend_value(
        _hcl_literal_attr(config_raw, config_clean, "key"))
    region = _safe_backend_value(
        _hcl_literal_attr(config_raw, config_clean, "region"))
    dynamic = (
        backend is None or key is None
        or _dynamic_hcl_attrs(
            clean_body, ("backend", "workspace"),
            {"backend": backend, "workspace": workspace},
        )
        or (re.search(r"\bconfig[ \t]*=", clean_body) is not None
            and not config_match)
        or _dynamic_hcl_attrs(
            config_clean, ("bucket", "key", "region"),
            {"bucket": bucket, "key": key, "region": region},
        )
    )
    return backend, workspace, bucket, key, region, dynamic


def _hcl_backends(path: str, dialect: str, text: str,
                  clean: str) -> tuple[TerraformBackend, ...]:
    result: list[TerraformBackend] = []
    for kind, _labels, begin, end, body in _blocks(text, clean):
        if kind != "terraform":
            continue
        body_start = clean.find(body, begin, end)
        if body_start < 0:
            continue
        raw_block = text[body_start:body_start + len(body)]
        for match in re.finditer(r"\bbackend\b", body):
            open_brace = body.find("{", match.end())
            if open_brace < 0:
                continue
            label = re.fullmatch(
                r'\s*"([^"\n]*)"\s*', raw_block[match.end():open_brace])
            backend_type = (_safe_backend_value(label.group(1))
                            if label is not None else None)
            if backend_type is None:
                continue
            block_end = _hcl_block_end(body, open_brace)
            if block_end is None:
                continue
            local_clean = body[open_brace + 1:block_end - 1]
            body_raw = raw_block[open_brace + 1:block_end - 1]
            bucket = _safe_backend_value(
                _hcl_literal_attr(body_raw, local_clean, "bucket"))
            key = _safe_backend_value(
                _hcl_literal_attr(body_raw, local_clean, "key"))
            region = _safe_backend_value(
                _hcl_literal_attr(body_raw, local_clean, "region"))
            workspace = _safe_backend_value(
                _hcl_literal_attr(body_raw, local_clean, "workspace"))
            result.append(TerraformBackend(
                path, dialect, backend_type,
                workspace=workspace, bucket=bucket, key=key, region=region,
                dynamic=_dynamic_hcl_attrs(
                    local_clean, ("bucket", "key", "region", "workspace"),
                    {"bucket": bucket, "key": key, "region": region,
                     "workspace": workspace},
                ),
            ))
    return tuple(result)


def _json_backends(path: str, dialect: str, root: dict[str, object]
                   ) -> tuple[TerraformBackend, ...]:
    terraform = root.get("terraform")
    blocks = terraform if isinstance(terraform, list) else [terraform]
    found: list[TerraformBackend] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        backends = block.get("backend")
        if not isinstance(backends, dict):
            continue
        for backend_type, config in backends.items():
            safe_type = _safe_backend_value(backend_type)
            if safe_type is None or not isinstance(config, dict):
                continue
            bucket = _safe_backend_value(config.get("bucket"))
            key = _safe_backend_value(config.get("key"))
            region = _safe_backend_value(config.get("region"))
            workspace = _safe_backend_value(config.get("workspace"))
            found.append(TerraformBackend(
                path, dialect, safe_type,
                workspace=workspace, bucket=bucket, key=key, region=region,
                dynamic=any(field in config and value is None
                            for field, value in (
                                ("bucket", bucket), ("key", key),
                                ("region", region), ("workspace", workspace))),
            ))
    return tuple(found)


def _hcl_module_source(raw_body: str, clean_body: str) -> str | None:
    return _hcl_literal_attr(raw_body, clean_body, "source")


def _top_level_assignments(clean_body: str) -> list[str]:
    """Names assigned at a block's top level, not nested object keys."""
    names: list[str] = []
    depth = 0
    for line in clean_body.splitlines():
        if depth == 0:
            match = re.match(r"\s*([A-Za-z_][\w-]*)\s*=", line)
            if match:
                names.append(match.group(1))
        depth += line.count("{") - line.count("}")
        if depth < 0:
            depth = 0
    return names


def _instance_mode(clean_body: str) -> str | None:
    names = set(_top_level_assignments(clean_body))
    if "for_each" in names:
        return "for_each"
    if "count" in names:
        return "count"
    return None


def _looks_like_reference(value: str, allowed: frozenset[str]) -> bool:
    if not _REF.fullmatch(value):
        return False
    first = value.split(".", 1)[0]
    return first in allowed or "_" in first


def _expression_refs(expression: str, allowed: frozenset[str]) -> list[str]:
    # Mask quoted strings inside an interpolation so a literal dotted value is
    # not mistaken for an expression reference.
    masked = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', " ", expression)
    return [match.group(1) for match in _REF.finditer(masked)
            if _looks_like_reference(match.group(1), allowed)]


def _json_references(value: object, allowed: frozenset[str], *,
                     depth: int = 0) -> list[str]:
    """Extract explicit Terraform-JSON expressions, never literal strings."""
    if depth > 32:
        return []
    found: list[str] = []
    if isinstance(value, str):
        for match in _INTERPOLATION.finditer(value):
            found.extend(_expression_refs(match.group(1), allowed))
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(_json_references(child, allowed, depth=depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(_json_references(child, allowed, depth=depth + 1))
    return list(dict.fromkeys(found))[:256]


def _module_source_edge(node: TerraformNode, source: str) -> TerraformEdge:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    if source.startswith("./") or source.startswith("../"):
        reason = (
            "local module source; target requires repository discovery"
            if node.module_source is not None
            else "local module source is not an allowlisted static path"
        )
        scope = "module-source"
    elif source.startswith("/"):
        reason = "absolute module source is not repository-relative"
        scope = "module-source"
    else:
        reason = "remote module source"
        scope = "remote"
    return TerraformEdge(
        node.key,
        _reference_key(node.path, node.dialect, scope, digest),
        "module_source",
        "declared",
        node.module_scope,
        reason,
    )


def _blocks(text: str, clean: str):
    for match in _BLOCK.finditer(clean):
        start = match.start(1)
        i = match.end()
        # A block declaration has labels followed by an opening brace; skip
        # assignments or malformed lines rather than guessing their meaning.
        while i < len(clean) and clean[i] not in "{\n":
            i += 1
        if i >= len(clean) or clean[i] != "{":
            continue
        labels = re.findall(r'"([^"\n]*)"', text[match.end() : i])
        depth = 1
        j = i + 1
        while j < len(clean) and depth:
            if clean[j] == "{":
                depth += 1
            elif clean[j] == "}":
                depth -= 1
            j += 1
        if depth:
            continue
        yield match.group(1), labels, start, j, clean[i + 1 : j - 1]


def index_terraform_text(path: str, text: str, *, module_scope: str = "root") -> TerraformGraph:
    """Extract declarations from one .tf/.tofu source. No filesystem access."""
    path = PurePosixPath(path).as_posix()
    low = path.lower()
    if not low.endswith(SUPPORTED_SUFFIXES):
        return TerraformGraph((), (), (), ((path, "unsupported Terraform source suffix"),))
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return TerraformGraph((), (), (), ((path, "source exceeds 2 MiB limit"),))
    dialect = "opentofu" if low.endswith((".tofu", ".tofu.json")) else "terraform"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    decls: list[TerraformNode] = []
    bodies: dict[str, str] = {}
    module_sources: list[tuple[TerraformNode, str]] = []
    errors: list[tuple[str, str]] = []
    backends: list[TerraformBackend] = []
    if low.endswith(".json"):
        try:
            root = json.loads(text)
        except (ValueError, RecursionError):
            return TerraformGraph((), (), (), ((path, "invalid Terraform JSON"),))
        if not isinstance(root, dict):
            return TerraformGraph((), (), (), ((path, "Terraform JSON root must be an object"),))
        allowed_refs = set(_REF_PREFIXES)
        for group_name in ("resource", "data", "ephemeral"):
            group = root.get(group_name, {})
            if isinstance(group, dict):
                allowed_refs.update(str(name) for name in group)
        providers = root.get("provider", {})
        if isinstance(providers, dict):
            allowed_refs.update(str(name) for name in providers)
        allowed_refs_frozen = frozenset(allowed_refs)
        for kind in ("resource", "data", "ephemeral"):
            group = root.get(kind, {})
            if not isinstance(group, dict):
                continue
            for typ, entries in group.items():
                if not isinstance(entries, dict):
                    continue
                for name, body in entries.items():
                    if not isinstance(body, dict):
                        continue
                    address = f"{typ}.{name}"
                    if kind in ("data", "ephemeral"):
                        address = f"{kind}.{address}"
                    mode = ("for_each" if "for_each" in body
                            else "count" if "count" in body else None)
                    remote = (_json_remote_state(body)
                              if kind == "data" and typ == "terraform_remote_state"
                              else (None, None, None, None, None, False))
                    identity = f"{kind}:{address}"
                    node = _node(
                        path, dialect, module_scope, identity, kind, address,
                        1, 1, digest, instance_mode=mode,
                        remote_backend_type=remote[0],
                        remote_workspace=remote[1],
                        remote_bucket=remote[2], remote_key=remote[3],
                        remote_region=remote[4], remote_dynamic=remote[5],
                    )
                    decls.append(node)
                    bodies[node.key] = " ".join(
                        _json_references(body, allowed_refs_frozen))
        for kind in ("variable", "output", "module", "provider"):
            group = root.get(kind, {})
            if not isinstance(group, dict):
                continue
            for name, body in group.items():
                if not isinstance(body, dict):
                    continue
                address = str(name)
                if kind == "provider":
                    alias = _safe_provider_alias(body.get("alias"))
                    if alias:
                        address = f"{address}.{alias}"
                identity = f"{kind}:{address}"
                source = body.get("source") if kind == "module" else None
                mode = ("for_each" if "for_each" in body
                        else "count" if "count" in body else None)
                node = _node(
                    path, dialect, module_scope, identity, kind, address,
                    1, 1, digest,
                    module_source=_safe_module_source(source),
                    instance_mode=mode,
                )
                decls.append(node)
                bodies[node.key] = " ".join(
                    _json_references(body, allowed_refs_frozen))
                if kind == "module" and isinstance(source, str):
                    module_sources.append((node, source))
        backends.extend(_json_backends(path, dialect, root))
        for kind in ("moved", "import", "removed", "check"):
            group = root.get(kind, {})
            entries: list[tuple[str, object]] = []
            if isinstance(group, list):
                entries = [(str(index), body) for index, body in enumerate(group)]
            elif isinstance(group, dict):
                entries = [(str(name), body) for name, body in group.items()]
            for index, (name, body) in enumerate(entries):
                if not isinstance(body, dict):
                    continue
                safe_name = name if _SAFE_LABEL.fullmatch(name) else str(index)
                address = safe_name if kind == "check" else f"{kind}@{safe_name}"
                node = _node(
                    path, dialect, module_scope, f"{kind}:{address}", kind,
                    address, 1, 1, digest,
                )
                decls.append(node)
                bodies[node.key] = " ".join(
                    _json_references(body, allowed_refs_frozen))
    else:
        clean, unterminated_heredoc, templates = _clean_hcl(text)
        if unterminated_heredoc:
            return TerraformGraph((), (), (), ((path, "unterminated HCL heredoc"),))

        def body_with_templates(begin: int, end: int, masked: str) -> str:
            extra = [ref for offset, ref in templates if begin <= offset < end]
            return masked + ((" " + " ".join(extra)) if extra else "")

        depth = 0
        for char in clean:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    return TerraformGraph((), (), (), ((path, "malformed HCL block structure"),))
        if depth:
            return TerraformGraph((), (), (), ((path, "malformed HCL block structure"),))
        backends.extend(_hcl_backends(path, dialect, text, clean))
        for kind, labels, begin, end, body in _blocks(text, clean):
            start_line = text.count("\n", 0, begin) + 1
            end_line = text.count("\n", 0, end) + 1
            if kind == "locals":
                for name in _top_level_assignments(body):
                    addr = name
                    ident = f"local:{addr}"
                    node = _node(
                        path, dialect, module_scope, ident, "local", addr,
                        start_line, end_line, digest,
                    )
                    decls.append(node)
                    bodies[node.key] = body_with_templates(
                        begin, end, clean[begin:end])
                continue
            if kind == "terraform":
                continue
            if kind in _TWO_LABEL_BLOCKS:
                if len(labels) != 2:
                    continue
                address = f"{labels[0]}.{labels[1]}"
                if kind in ("data", "ephemeral"):
                    address = f"{kind}.{address}"
            elif kind in _ONE_LABEL_BLOCKS:
                if len(labels) != 1:
                    continue
                address = labels[0]
            elif kind in _UNLABELED_BLOCKS:
                if labels:
                    continue
                address = f"{kind}@{start_line}"
            else:
                continue
            raw_body = text[begin:end]
            clean_body = clean[begin:end]
            if kind == "provider":
                alias = _safe_provider_alias(
                    _hcl_literal_attr(raw_body, clean_body, "alias"))
                if alias:
                    address = f"{address}.{alias}"
            identity = f"{kind}:{address}"
            source = _hcl_module_source(raw_body, clean_body) if kind == "module" else None
            remote = (_hcl_remote_state(raw_body, clean_body)
                      if kind == "data" and labels[0] == "terraform_remote_state"
                      else (None, None, None, None, None, False))
            node = _node(
                path, dialect, module_scope, identity, kind, address,
                start_line, end_line, digest,
                module_source=_safe_module_source(source),
                instance_mode=_instance_mode(body),
                remote_backend_type=remote[0],
                remote_workspace=remote[1],
                remote_bucket=remote[2], remote_key=remote[3],
                remote_region=remote[4], remote_dynamic=remote[5],
            )
            decls.append(node)
            bodies[node.key] = body_with_templates(begin, end, clean_body)
            if kind == "module" and source is not None:
                module_sources.append((node, source))
    by_ref: dict[str, TerraformNode] = {}
    for node in decls:
        by_ref[f"{node.kind}.{node.address}"] = node
        prefix = _ADDR_PREFIX.get(node.kind)
        if prefix:
            by_ref[f"{prefix}.{node.address}"] = node
        if node.kind in ("resource", "data", "ephemeral", "provider"):
            by_ref[node.address] = node
    edges: list[TerraformEdge] = []
    unresolved: list[TerraformEdge] = []
    for node in decls:
        for match in _REF.finditer(bodies.get(node.key, "")):
            ref = match.group(1)
            # Terraform references have an address prefix and are structural
            # evidence, but expressions with indexes/dynamic scope stay open.
            target = by_ref.get(ref)
            if target is None:
                parts = ref.split(".")
                for stop in range(len(parts) - 1, 1, -1):
                    target = by_ref.get(".".join(parts[:stop]))
                    if target is not None:
                        break
            if target:
                edges.append(
                    TerraformEdge(node.key, target.key, "references", "declared", module_scope, ref)
                )
            else:
                unresolved.append(
                    TerraformEdge(
                        node.key,
                        _reference_key(path, dialect, module_scope, ref),
                        "references",
                        "declared",
                        module_scope,
                        ref,
                    )
                )
    for node, source in module_sources:
        unresolved.append(_module_source_edge(node, source))
    return TerraformGraph(
        tuple(decls), tuple(_dedupe(edges)), tuple(_dedupe(unresolved)),
        tuple(errors), tuple(backends),
    )


def _local_module_target_scope(node: TerraformNode) -> tuple[str | None, str | None]:
    """Normalized local target directory, or (None, unresolved reason)."""
    source = node.module_source
    if not source:
        return None, None
    if source.startswith("/"):
        return None, "absolute module source is not repository-relative"
    if not source.startswith(("./", "../")):
        return None, None
    parent = PurePosixPath(node.path).parent.as_posix()
    target_scope = posixpath.normpath(posixpath.join(parent, source))
    if (target_scope in ("", ".", "..")
            or target_scope.startswith("../")
            or target_scope.startswith("/")):
        return None, "local module source escapes repository root"
    return target_scope, None


def merge_terraform_graphs(graphs) -> TerraformGraph:
    """Merge per-file graphs and resolve bounded local modules and references.

    Ordinary references require identical dialect and module scope. Local
    module sources target declarations in their normalized repository directory
    and dialect. Duplicate address candidates remain unresolved.
    """
    nodes = tuple(node for graph in graphs for node in graph.nodes)
    all_edges = [edge for graph in graphs for edge in graph.edges]
    pending = [edge for graph in graphs for edge in graph.unresolved]
    edges = [edge for edge in all_edges if edge.relation != "references"]
    pending.extend(edge for edge in all_edges if edge.relation == "references")
    errors = tuple(error for graph in graphs for error in graph.errors)
    node_by_key = {node.key: node for node in nodes}

    declarations: dict[tuple[str, str, str], list[TerraformNode]] = {}
    for node in nodes:
        candidates = {
            f"{node.kind}.{node.address}",
            node.address,
        }
        prefix = _ADDR_PREFIX.get(node.kind)
        if prefix:
            candidates.add(f"{prefix}.{node.address}")
        for address in candidates:
            declarations.setdefault((node.dialect, node.module_scope, address), []).append(node)

    module_declarations: dict[tuple[str, str, str], list[TerraformNode]] = {}
    for node in nodes:
        if node.kind == "module":
            key = (node.dialect, node.module_scope, node.address)
            module_declarations.setdefault(key, []).append(node)

    unresolved: list[TerraformEdge] = []
    for edge in pending:
        if edge.relation == "module_source":
            source_node = node_by_key.get(edge.source)
            if source_node is None or source_node.module_source is None:
                unresolved.append(edge)
                continue
            identity = (
                source_node.dialect,
                source_node.module_scope,
                source_node.address,
            )
            if len(module_declarations.get(identity, ())) != 1:
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        edge.scope,
                        "duplicate module declarations are ambiguous",
                    )
                )
                continue
            target_scope, reason = _local_module_target_scope(source_node)
            if target_scope is None:
                if reason is None:
                    unresolved.append(edge)
                else:
                    unresolved.append(
                        TerraformEdge(
                            edge.source,
                            edge.target,
                            edge.relation,
                            edge.provenance,
                            edge.scope,
                            reason,
                        )
                    )
                continue
            targets = sorted(
                (
                    node
                    for node in nodes
                    if node.dialect == source_node.dialect
                    and node.module_scope == target_scope
                    and node.path != source_node.path
                ),
                key=lambda node: node.key,
            )
            if not targets:
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        target_scope,
                        "local module source has no statically discovered declarations",
                    )
                )
                continue
            for target in targets[:MAX_MODULE_SOURCE_TARGETS]:
                edges.append(
                    TerraformEdge(
                        source_node.key,
                        target.key,
                        "module_source",
                        "declared",
                        target_scope,
                        target_scope,
                    )
                )
            if len(targets) > MAX_MODULE_SOURCE_TARGETS:
                unresolved.append(
                    TerraformEdge(
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.provenance,
                        target_scope,
                        "local module source declaration limit exceeded",
                    )
                )
            continue
        if edge.relation != "references":
            unresolved.append(edge)
            continue
        # The source node carries dialect information; unresolved references
        # are intentionally scoped by the edge, not by their placeholder key.
        source = node_by_key.get(edge.source)
        if source is None:
            unresolved.append(edge)
            continue
        ref = edge.evidence
        parts = ref.split(".")
        if parts[0] == "module" and len(parts) >= 3:
            modules = module_declarations.get(
                (source.dialect, edge.scope, parts[1]), [])
            if len(modules) == 1:
                target_scope, _reason = _local_module_target_scope(modules[0])
                if target_scope:
                    outputs = [
                        node for node in nodes
                        if node.dialect == source.dialect
                        and node.module_scope == target_scope
                        and node.kind == "output"
                        and node.address == parts[2]
                    ]
                    if len(outputs) == 1:
                        edges.append(
                            TerraformEdge(
                                edge.source, outputs[0].key, "module_output",
                                "declared", target_scope, ref,
                            )
                        )
        target_candidates: list[TerraformNode] = []
        for stop in range(len(parts), 1, -1):
            key = (source.dialect, edge.scope, ".".join(parts[:stop]))
            found = declarations.get(key, [])
            if found:
                target_candidates = found
                break
        # A resource or data reference's final attribute is not part of its
        # declaration address, so longest matching declaration prefix wins.
        if len(target_candidates) == 1:
            edges.append(
                TerraformEdge(
                    edge.source,
                    target_candidates[0].key,
                    edge.relation,
                    edge.provenance,
                    edge.scope,
                    edge.evidence,
                )
            )
        else:
            unresolved.append(edge)
    return TerraformGraph(
        nodes,
        tuple(_dedupe(edges)),
        tuple(_dedupe(unresolved)),
        errors,
        tuple(backend for graph in graphs for backend in graph.backends),
    )


def resolve_terraform_roots(
    graph: TerraformGraph,
    source_paths: tuple[str, ...] | list[str],
    *,
    lock_dirs: tuple[str, ...] | list[str] = (),
    explicit_roots: tuple[str, ...] | list[str] = (),
    remote_states: tuple[object, ...] | list[object] = (),
) -> TerraformGraph:
    """Add conservative root nodes and declared/static remote-state links.

    Backend identities are compared transiently and are not copied onto root
    nodes or edges. Only a source backend's backend type, key and any additional
    identity fields explicitly present on the consumer participate in matching.
    """
    paths = tuple(sorted(set(PurePosixPath(p).as_posix() for p in source_paths)))
    if explicit_roots:
        roots = sorted(set(PurePosixPath(p).as_posix() for p in explicit_roots))
    else:
        source_dirs = {PurePosixPath(path).parent.as_posix() for path in paths}
        locked = {
            PurePosixPath(path).as_posix()
            for path in lock_dirs
        }
        roots = sorted(locked, key=lambda value: (value.count("/"), value))
        for source_dir in sorted(source_dirs, key=lambda value: (value.count("/"), value)):
            if any(_in_root(source_dir, root) for root in roots):
                continue
            roots.append(source_dir)
        roots.sort()

    def owning_root(path: str) -> str | None:
        matches = [root for root in roots if _in_root(path, root)]
        return max(matches, key=len) if matches else None

    root_nodes: list[TerraformNode] = []
    by_root: dict[str, TerraformNode] = {}
    for root in roots:
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
        node = _node(
            root, "terraform", root, f"root:{root}", "root", root,
            0, 0, digest,
        )
        root_nodes.append(node)
        by_root[root] = node

    backend_by_root: dict[str, list[TerraformBackend]] = {}
    for backend in graph.backends:
        root = owning_root(backend.path)
        if root is not None:
            backend_by_root.setdefault(root, []).append(backend)

    edges = list(graph.edges)
    unresolved = list(graph.unresolved)
    declarations_by_identity: dict[tuple[str, str], list[TerraformNode]] = {}
    for node in graph.nodes:
        if node.kind == "data" and node.address.startswith(
                "data.terraform_remote_state."):
            consumer = owning_root(node.path)
            if consumer is not None:
                label = node.address.rsplit(".", 1)[-1]
                declarations_by_identity.setdefault((consumer, label), []).append(node)

    mappings = list(remote_states)
    for (consumer, label), declarations in sorted(declarations_by_identity.items()):
        if len(declarations) != 1:
            unresolved.extend(_unresolved_remote(
                node, consumer, "duplicate remote-state declarations are ambiguous")
                for node in declarations)
            continue
        node = declarations[0]
        mapping_candidates = [
            item for item in mappings
            if getattr(item, "consumer", None) == consumer
            and getattr(item, "name", None) in {label, node.address}
        ]
        if len(mapping_candidates) > 1:
            unresolved.append(_unresolved_remote(
                node, consumer, "multiple project-config mappings are ambiguous"))
            continue
        if mapping_candidates:
            producer_path = str(getattr(mapping_candidates[0], "producer", ""))
            producer = by_root.get(producer_path)
            if producer is None:
                unresolved.append(_unresolved_remote(
                    node, consumer, "configured producer root is not present"))
                continue
            edges.append(TerraformEdge(
                node.key, producer.key, "remote_state", "declared",
                consumer, "project config", "high",
            ))
            continue

        static_match = (
            not node.remote_dynamic
            and node.remote_backend_type is not None
            and node.remote_key is not None
        )
        if not static_match:
            unresolved.append(_unresolved_remote(
                node, consumer, "remote-state backend identity is dynamic or incomplete"))
            continue
        candidates: set[str] = set()
        for producer_path, backend_configs in backend_by_root.items():
            for backend in backend_configs:
                if (backend.dynamic
                        or backend.backend_type != node.remote_backend_type
                        or backend.key != node.remote_key):
                    continue
                supplied = (
                    ("bucket", node.remote_bucket, backend.bucket),
                    ("region", node.remote_region, backend.region),
                    ("workspace", node.remote_workspace, backend.workspace),
                )
                if any(
                        wanted is not None
                        and not (_field == "workspace" and actual is None)
                        and wanted != actual
                        for _field, wanted, actual in supplied):
                    continue
                candidates.add(producer_path)
        if len(candidates) == 1:
            producer = by_root[next(iter(candidates))]
            edges.append(TerraformEdge(
                node.key, producer.key, "remote_state", "inferred",
                consumer, "unique static backend/key match", "medium",
            ))
        elif len(candidates) > 1:
            unresolved.append(_unresolved_remote(
                node, consumer, "static backend/key match is ambiguous"))
        else:
            unresolved.append(_unresolved_remote(
                node, consumer, "no static backend/key producer matched"))

    return TerraformGraph(
        tuple((*graph.nodes, *root_nodes)),
        tuple(_dedupe(edges)),
        tuple(_dedupe(unresolved)),
        graph.errors,
        graph.backends,
    )


def _in_root(path: str, root: str) -> bool:
    return root in {"", "."} or path == root or path.startswith(root + "/")


def _unresolved_remote(node: TerraformNode, consumer: str,
                       reason: str) -> TerraformEdge:
    return TerraformEdge(
        node.key, _reference_key(node.path, node.dialect, consumer,
                                 node.address),
        "remote_state", "declared", consumer, reason, "low",
    )


def _node(
    path,
    dialect,
    scope,
    ident,
    kind,
    address,
    start,
    end,
    digest,
    *,
    module_source=None,
    instance_mode=None,
    remote_backend_type=None,
    remote_workspace=None,
    remote_bucket=None,
    remote_key=None,
    remote_region=None,
    remote_dynamic=False,
):
    key = f"infra:v1:{dialect}:{path}:{scope}:{ident}"
    return TerraformNode(
        key,
        kind,
        address,
        path,
        start,
        end,
        dialect,
        scope,
        digest,
        module_source=module_source,
        instance_mode=instance_mode,
        remote_backend_type=remote_backend_type,
        remote_workspace=remote_workspace,
        remote_bucket=remote_bucket,
        remote_key=remote_key,
        remote_region=remote_region,
        remote_dynamic=remote_dynamic,
    )


def _reference_key(path, dialect, scope, ref):
    safe_ref = re.sub(r"[^A-Za-z0-9_.:-]", "_", ref)[:120]
    return f"infra:v1:{dialect}:{path}:{scope}:unresolved:{safe_ref}"


def _dedupe(edges):
    return sorted(
        set(edges),
        key=lambda edge: (
            edge.source,
            edge.target,
            edge.relation,
            edge.provenance,
            edge.scope,
            edge.evidence,
        ),
    )
