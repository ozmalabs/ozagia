"""Small OpenAPI graph indexer with optional safe YAML input.

An adapter can select files from ``ctx.root`` and call
``index_openapi_document(ctx.builder, ctx.file_node(rel), rel, text)``; this
keeps spec discovery policy with the project adapter and avoids filesystem
walks. JSON uses the standard library; YAML requires the optional ``openapi-yaml``
extra (PyYAML). References are reported, never fetched or followed.
"""
from __future__ import annotations

import json
from typing import Any


HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options",
                         "head", "patch", "trace"})
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_NODES = 100_000
MAX_DOCUMENT_DEPTH = 100


def _parse_document(rel: str, text: str) -> tuple[Any, str | None]:
    if not isinstance(text, str) or len(text) > MAX_DOCUMENT_BYTES:
        return None, "document exceeds the 5 MiB input limit"
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        return None, "document exceeds the 5 MiB input limit"
    if rel.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            return None, "YAML support requires the optional dependency: pip install 'ozagia[openapi-yaml]'"
        try:
            # Inspect the representation graph first. Alias occurrences point
            # to the same Node object; rejecting repeated identities prevents
            # both recursive aliases and exponential alias expansion during
            # construction.
            # BaseLoader keeps implicit scalars as strings (including dates
            # and timestamps), which keeps graph attributes JSON-compatible.
            root = yaml.compose(text, Loader=yaml.BaseLoader)
        except Exception:
            return None, "invalid or unsafe YAML document"
        seen_yaml_nodes: set[int] = set()
        yaml_count = 0
        def check_yaml_node(node: Any, depth: int = 0) -> bool:
            nonlocal yaml_count
            ident = id(node)
            if ident in seen_yaml_nodes:
                return False
            allowed_tags = {
                "tag:yaml.org,2002:str", "tag:yaml.org,2002:null",
                "tag:yaml.org,2002:bool", "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:float", "tag:yaml.org,2002:binary",
                "tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:map",
                "tag:yaml.org,2002:seq",
            }
            if node.tag not in allowed_tags:
                return False
            seen_yaml_nodes.add(ident)
            yaml_count += 1
            if yaml_count > MAX_DOCUMENT_NODES or depth > MAX_DOCUMENT_DEPTH:
                return False
            if isinstance(node, yaml.MappingNode):
                children = (child for pair in node.value for child in pair)
            elif isinstance(node, yaml.SequenceNode):
                children = iter(node.value)
            else:
                children = iter(())
            return all(check_yaml_node(child, depth + 1) for child in children)
        if root is not None and not check_yaml_node(root):
            return None, "YAML tags, aliases, or document structure are unsupported"
        try:
            doc = yaml.load(text, Loader=yaml.BaseLoader)
        except Exception:
            return None, "invalid or unsafe YAML document"
    else:
        try:
            doc = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            return None, "invalid JSON document"

    # YAML aliases can create recursive graphs. Bound traversal and reject
    # cycles before the indexer's recursive reference walk sees them.
    seen: set[int] = set()
    active: set[int] = set()
    count = 0
    def check(value: Any, depth: int = 0) -> bool:
        nonlocal count
        if not isinstance(value, (dict, list)):
            return True
        ident = id(value)
        if ident in active:
            return False
        if ident in seen:
            return True
        count += 1
        if count > MAX_DOCUMENT_NODES or depth > MAX_DOCUMENT_DEPTH:
            return False
        seen.add(ident)
        active.add(ident)
        children = (item for pair in value.items() for item in pair) if isinstance(value, dict) else value
        ok = all(check(child, depth + 1) for child in children)
        active.remove(ident)
        return ok
    if not check(doc):
        return None, "document exceeds structural limits or contains recursive aliases"
    return doc, None


def index_openapi_document(builder: Any, file_node: int, rel: str,
                           text: str, repo: str | None = None) -> dict[str, Any]:
    """Add operation and component-schema nodes from one JSON or YAML OpenAPI file.

    Operation keys are stable by file, method and path. Component schemas are
    linked to operations when their request or response schema uses a local
    ``#/components/schemas/...`` reference. Swagger 2.0 ``#/definitions/...``
    references are supported too. ``valid`` means only that this looks like a
    supported OpenAPI/Swagger document; it is not full specification
    validation. Other local references are listed as unsupported. References
    are never followed. Invalid or unrecognized documents do not modify the graph.
    """
    empty = {"valid": False, "operations": 0, "schemas": 0,
             "unresolved_references": [], "external_references": [],
             "unsupported_references": []}
    doc, error = _parse_document(rel, text)
    if error:
        return {**empty, "error": error}
    if not isinstance(doc, dict):
        return empty
    version = str(doc.get("openapi", ""))
    swagger = doc.get("swagger") == "2.0"
    paths = doc.get("paths")
    if not ((version.startswith("3.") or swagger) and
            isinstance(paths, dict)):
        return empty

    if swagger:
        schemas = doc.get("definitions", {})
    else:
        components = doc.get("components")
        schemas = (components.get("schemas", {})
                   if isinstance(components, dict) else {})
    ref_prefix = "#/definitions/" if swagger else "#/components/schemas/"

    def pointer_escape(value: str) -> str:
        return value.replace("~", "~0").replace("/", "~1")

    def pointer_unescape(value: str) -> str:
        return value.replace("~1", "/").replace("~0", "~")

    schema_nodes: dict[str, int] = {}
    if isinstance(schemas, dict):
        for name, schema in sorted(schemas.items(), key=lambda item: str(item[0])):
            if not isinstance(name, str) or not isinstance(schema, dict):
                continue
            attrs = {
                "name": name, "file": rel,
                "description": schema.get("description", ""),
                "schema_type": schema.get("type", "object"),
                "source_pointer": f"{ref_prefix}{pointer_escape(name)}",
            }
            if repo is not None:
                attrs["repo"] = repo
            schema_key = f"{repo}:{rel}#{name}" if repo is not None else f"{rel}#{name}"
            nid = builder.node("ApiSchema", attrs, key=("api_schema", schema_key))
            builder.edge(file_node, "defines_api_schema", nid)
            schema_nodes[name] = nid

    operation_count = 0
    unresolved: set[str] = set()
    external: set[str] = set()
    unsupported: set[str] = set()
    for path, path_item in sorted(paths.items(), key=lambda item: str(item[0])):
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        path_pointer = f"#/paths/{pointer_escape(path)}"
        for method, operation in sorted(path_item.items()):
            method = str(method).lower()
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_pointer = f"{path_pointer}/{method}"
            attrs = {
                "method": method.upper(), "path": path, "file": rel,
                "operation_id": operation.get("operationId", ""),
                "summary": operation.get("summary", ""),
                "tags": operation.get("tags", []),
                "source_pointer": operation_pointer,
            }
            if repo is not None:
                attrs["repo"] = repo
            op_key = (f"{repo}:{rel}#{method}:{path}" if repo is not None
                      else f"{rel}#{method}:{path}")
            opid = builder.node("ApiOperation", attrs,
                                key=("api_operation", op_key))
            builder.edge(file_node, "defines_api_operation", opid)
            refs: set[str] = set()

            def collect(value: Any) -> None:
                if isinstance(value, dict):
                    ref = value.get("$ref")
                    if isinstance(ref, str):
                        if ref.startswith(ref_prefix):
                            suffix = ref[len(ref_prefix):]
                            if "/" in suffix:
                                unsupported.add(ref)
                            else:
                                refs.add(pointer_unescape(suffix))
                        elif ref.startswith("#"):
                            unsupported.add(ref)
                        else:
                            external.add(ref)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(operation)
            for name in sorted(refs):
                target = schema_nodes.get(name)
                if target is None:
                    unresolved.add(f"{ref_prefix}{pointer_escape(name)}")
                else:
                    builder.edge(opid, "uses_api_schema", target)
            operation_count += 1
    return {"valid": True, "operations": operation_count,
            "schemas": len(schema_nodes),
            "unresolved_references": sorted(unresolved),
            "external_references": sorted(external),
            "unsupported_references": sorted(unsupported)}

