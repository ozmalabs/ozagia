"""Preview-first semantic editing for Ocelot seed manifests.

The caller sends semantic operations, not JSON text and not a diff::

    seed_edit(root, "my_seed", [
        {"op": "append", "kind": "rule", "value": {...}},
        {"op": "set_metadata", "fields": {"version": "1.2.0"}},
    ])

The module translates those operations into the span-preserving path ops of
:mod:`ozagia.edit.json_edit`, so the rest of the manifest keeps its bytes, key
order and indentation.  Preview is the default.  ``write=True`` applies the
edit atomically with the edit transaction's backup, then validates the written
manifest with :func:`ozagia.integrations.ocelot.workspace_validate.validate_seed`
and restores the backup bytes if validation reports errors.

Generated and acquired caches (``seeds/generated/``, ``seeds/acquired/``) are
never edited.  The workspace adapter registers ``ocelot_seed_edit`` lazily from
``workspace:register``; a partial install can also register only this module::

    [adapters]
    modules = [
      "ozagia.integrations.ocelot.workspace:register",
      "ozagia.integrations.ocelot.workspace_seed_edit:register_seed_edit",
    ]
"""
from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ozagia.edit.code_edit import CodeEditError, MAX_SOURCE_BYTES
from ozagia.edit.json_edit import apply_json_edit_script
from ozagia.edit.txn import read_source, sha256_bytes

from .workspace_validate import (
    SAFE_SEED_ID_RE,
    validate_seed,
    validate_seed_data,
)

SEED_DIR = "seeds"
BLOCKED_SEED_DIRS = frozenset({"acquired", "generated"})
METADATA_FIELDS = (
    "version",
    "description",
    "self_load_only",
    "area",
    "order",
    "latency_class",
    "capability_keys",
    "depends_on",
)
KIND_CONTAINERS = {
    "rule": "rules",
    "node": "nodes",
    "edge": "edges",
    "production": "productions",
    "reflex": "reflexes",
    "concept": "concepts",
}
NAME_SELECTOR_KINDS = frozenset({"rule", "concept", "node", "reflex"})
ADD_OPS = {"add_node": "nodes", "add_edge": "edges"}
_MISSING = object()


class _SeedEditError(Exception):
    """A refused semantic operation or unsafe seed target."""


@dataclass
class _Pending:
    """An item appended by this script, addressed before the file is written."""

    value: dict[str, Any]


@dataclass
class _ContainerState:
    items: list[Any]
    created: bool
    original_len: int
    slots: list[int | _Pending]
    pending: list[_Pending] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)
    deletes: int = 0


def _base_result(seed_id: str, rel: str, operations: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "seed": seed_id,
        "path": rel,
        "applied": False,
        "base_sha256": None,
        "new_sha256": None,
        "ops": [],
        "operations": list(operations) if isinstance(operations, list) else [],
        "changes": [],
        "diff_view": "",
        "backup": None,
        "rolled_back": False,
        "errors": [],
        "warnings": [],
        "validation_before": None,
        "validation_after": None,
        "test_skeleton": None,
    }


def _resolve_seed(root: Path, seed_id: str) -> tuple[Path | None, str, list[str]]:
    """Resolve a top-level ``seeds/<id>.json``, refusing generated caches."""
    errors: list[str] = []
    if not seed_id:
        errors.append(
            "invalid seed id ''; expected a filename stem such as 'my_seed'")
        return None, seed_id, errors
    if (seed_id in (".", "..") or "/" in seed_id or "\\" in seed_id
            or Path(seed_id).is_absolute()
            or not SAFE_SEED_ID_RE.fullmatch(seed_id)):
        errors.append(
            f"invalid seed id {seed_id!r}; it must be a filename stem. "
            "Nested, generated/acquired and escaping paths are refused")
        return None, seed_id, errors

    seeds = (root / SEED_DIR).resolve()
    expected = seeds / f"{seed_id}.json"
    path = expected.resolve()
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        errors.append(
            f"seed path escapes the repository root: {seed_id!r}")
        return None, str(path), errors
    if path != expected:
        errors.append(
            f"{rel}: seed manifest resolves through a symlink; refusing to "
            "edit it")
        return None, rel, errors
    if path.parent != seeds:
        errors.append(
            f"{rel}: seed manifests must be directly under {SEED_DIR}/")
        return None, rel, errors
    for blocked in BLOCKED_SEED_DIRS:
        blocked_dir = (seeds / blocked).resolve()
        try:
            path.relative_to(blocked_dir)
        except ValueError:
            continue
        errors.append(
            f"{rel}: {SEED_DIR}/{blocked}/ is a generated cache; edit the "
            "source seed instead")
        return None, rel, errors
    if not path.is_file():
        for blocked in sorted(BLOCKED_SEED_DIRS):
            candidate = seeds / blocked / f"{seed_id}.json"
            if candidate.is_file():
                cached = candidate.relative_to(root).as_posix()
                errors.append(
                    f"{cached}: {SEED_DIR}/{blocked}/ is a generated cache; "
                    "edit the source seed instead")
                return None, cached, errors
        errors.append(f"{rel}: seed manifest not found")
        return None, rel, errors
    return path, rel, errors


def _item_names(value: Any) -> set[str]:
    """Non-empty ``name``/``id`` strings used by a name selector."""
    names: set[str] = set()
    if not isinstance(value, Mapping):
        return names
    sources = [value]
    attrs = value.get("attrs")
    if isinstance(attrs, Mapping):
        sources.append(attrs)
    for source in sources:
        for key in ("name", "id"):
            candidate = source.get(key)
            if isinstance(candidate, str) and candidate.strip():
                names.add(candidate)
    return names


def _free_local_id(data: Mapping[str, Any]) -> int:
    """The smallest positive local_id not used by any top-level list item."""
    used: set[int] = set()
    for values in data.values():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            local_id = item.get("local_id")
            if (isinstance(local_id, int) and not isinstance(local_id, bool)
                    and local_id > 0):
                used.add(local_id)
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def _object_value(operation: Mapping[str, Any], op_name: str) -> dict[str, Any]:
    value = operation.get("value")
    if not isinstance(value, Mapping):
        raise _SeedEditError(f"{op_name}: value must be a JSON object")
    return copy.deepcopy(dict(value))


def _kind_container(operation: Mapping[str, Any]) -> tuple[str, str]:
    raw = operation.get("kind")
    if not isinstance(raw, str) or not raw.strip():
        raise _SeedEditError(
            "operation needs a kind: " + ", ".join(sorted(KIND_CONTAINERS)))
    kind = raw.strip().lower()
    container = KIND_CONTAINERS.get(kind)
    if container is None:
        raise _SeedEditError(
            f"unknown kind {raw!r}; supported: "
            + ", ".join(sorted(KIND_CONTAINERS)))
    return kind, container


class _Translator:
    """Mutable prospective manifest plus span-preserving JSON path ops."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.working: dict[str, Any] = copy.deepcopy(dict(data))
        self.top_merge: dict[str, Any] = {}
        self.states: dict[str, _ContainerState] = {}

    # -- state ---------------------------------------------------------- #
    def _state(self, container: str) -> _ContainerState:
        state = self.states.get(container)
        if state is not None:
            return state
        if container in self.working:
            items = self.working[container]
            if not isinstance(items, list):
                raise _SeedEditError(
                    f"{container} must be a list before it can be edited")
            created = False
            original_len = len(items)
            slots: list[int | _Pending] = list(range(len(items)))
        else:
            items = []
            self.working[container] = items
            self.top_merge[container] = items
            created = True
            original_len = 0
            slots = []
        state = _ContainerState(
            items=items, created=created, original_len=original_len,
            slots=slots)
        self.states[container] = state
        return state

    # -- selectors ------------------------------------------------------ #
    def _select(self, container: str, kind: str,
                operation: Mapping[str, Any], op_name: str) -> int:
        state = self._state(container)
        has_index = "index" in operation
        has_name = "name" in operation
        if has_index and has_name:
            raise _SeedEditError(
                f"{op_name}: index and name are ambiguous; use exactly one")
        if not has_index and not has_name:
            raise _SeedEditError(
                f"{op_name}: need an index or name selector")
        if has_index:
            index = operation.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise _SeedEditError(f"{op_name}: index must be an integer")
            if index < 0 or index >= len(state.items):
                raise _SeedEditError(
                    f"{op_name}: index {index} is out of range for "
                    f"{container} ({len(state.items)} item(s))")
            return index
        if kind not in NAME_SELECTOR_KINDS:
            raise _SeedEditError(
                f"{op_name}: {kind!r} has no name selector; use index")
        name = operation.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _SeedEditError(
                f"{op_name}: name must be a non-empty string")
        matches = [index for index, item in enumerate(state.items)
                   if name in _item_names(item)]
        if not matches:
            raise _SeedEditError(
                f"{op_name}: no {kind} named {name!r} in {container}")
        if len(matches) > 1:
            raise _SeedEditError(
                f"{op_name}: {kind} name {name!r} is ambiguous "
                f"({len(matches)} matches); use an index")
        return matches[0]

    # -- semantic operations ------------------------------------------- #
    def set_metadata(self, operation: Mapping[str, Any]) -> None:
        fields = operation.get("fields")
        if not isinstance(fields, Mapping) or not fields:
            raise _SeedEditError(
                "set_metadata: fields must be a non-empty JSON object")
        unknown = sorted(set(fields) - set(METADATA_FIELDS))
        if unknown:
            raise _SeedEditError(
                "set_metadata: unknown field(s) " + ", ".join(unknown)
                + "; supported: " + ", ".join(METADATA_FIELDS))
        for key in fields:
            value = copy.deepcopy(fields[key])
            self.working[key] = value
            self.top_merge[key] = value

    def append(self, container: str, value: dict[str, Any]) -> None:
        state = self._state(container)
        pending = _Pending(value=value)
        state.items.append(value)
        state.slots.append(pending)
        state.pending.append(pending)

    def replace(self, container: str, index: int,
                value: dict[str, Any]) -> None:
        state = self._state(container)
        state.items[index] = value
        slot = state.slots[index]
        if isinstance(slot, int):
            # A repeated replace of the same original item is one edit: two
            # span edits for one value would overlap in the JSON adapter.
            for operation in state.ops:
                if (operation["op"] == "set"
                        and operation["path"] == [container, slot]):
                    operation["value"] = value
                    return
            state.ops.append({
                "op": "set",
                "path": [container, slot],
                "value": value,
            })
        else:
            slot.value = value

    def delete(self, container: str, index: int) -> None:
        state = self._state(container)
        del state.items[index]
        slot = state.slots.pop(index)
        if isinstance(slot, int):
            state.ops = [
                operation for operation in state.ops
                if not (operation["op"] == "set"
                        and operation["path"] == [container, slot])
            ]
            state.ops.append({"op": "delete", "path": [container, slot]})
            state.deletes += 1
        else:
            try:
                state.pending.remove(slot)
            except ValueError:  # pragma: no cover - defensive
                pass

    def add_item(self, container: str, value: dict[str, Any]) -> None:
        local_id = value.get("local_id", _MISSING)
        if local_id is _MISSING or local_id is None or (
                isinstance(local_id, int) and not isinstance(local_id, bool)
                and local_id == 0):
            value["local_id"] = _free_local_id(self.working)
        elif isinstance(local_id, bool) or not isinstance(local_id, int):
            raise _SeedEditError("local_id must be an integer when supplied")
        self.append(container, value)

    # -- output --------------------------------------------------------- #
    def ops(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.top_merge:
            out.append({"op": "merge", "path": [], "value": self.top_merge})
        for container, state in self.states.items():
            if state.created:
                # The create/merge below already carries the final array.
                continue
            if state.deletes > 1:
                # Adjacent array deletions can overlap in one script; a local
                # array re-render is the safe fallback for a multi-delete.
                out.append({
                    "op": "set",
                    "path": [container],
                    "value": state.items,
                })
                continue
            if state.pending:
                originals = sum(
                    1 for slot in state.slots if isinstance(slot, int))
                # An append inserts after the original last item.  If the
                # array was empty and gets several appends, or every original
                # item was deleted first, those insertions have no preceding
                # array element and would start with a bare comma.
                if ((state.original_len == 0 and len(state.pending) > 1)
                        or (state.original_len > 0 and originals == 0)):
                    out.append({
                        "op": "set",
                        "path": [container],
                        "value": state.items,
                    })
                    continue
            out.extend(state.ops)
            for pending in state.pending:
                out.append({
                    "op": "append",
                    "path": [container],
                    "value": pending.value,
                })
        return out


def _apply_operation(translator: _Translator,
                     operation: Mapping[str, Any]) -> None:
    if not isinstance(operation, Mapping):
        raise _SeedEditError("each operation must be a JSON object")
    op_name = operation.get("op")
    if not isinstance(op_name, str) or not op_name.strip():
        raise _SeedEditError("operation needs an 'op'")
    op_name = op_name.strip().lower()

    if op_name == "set_metadata":
        translator.set_metadata(operation)
        return
    if op_name == "append":
        kind, container = _kind_container(operation)
        translator.append(container, _object_value(operation, op_name))
        return
    if op_name in ("replace", "delete"):
        kind, container = _kind_container(operation)
        index = translator._select(container, kind, operation, op_name)
        if op_name == "replace":
            translator.replace(container, index,
                               _object_value(operation, op_name))
        else:
            translator.delete(container, index)
        return
    if op_name in ADD_OPS:
        translator.add_item(ADD_OPS[op_name], _object_value(operation, op_name))
        return
    raise _SeedEditError(
        f"unknown op {op_name!r}; supported: set_metadata, append, replace, "
        "delete, add_node, add_edge")


def test_skeleton(root: str | Path, seed_id: str) -> dict[str, Any]:
    """Preview a minimal ``tests/test_<id>_migrated.py``; never write it.

    The skeleton only checks that the manifest exists and carries the expected
    id. It is a starting point for a person or agent, not a gate: it does not
    claim to exercise the seed's rules, and Ocelot's own migrated tests remain
    the authority.
    """
    root_path = Path(root).expanduser().resolve()
    seed = str(seed_id or "").strip()
    target_rel = f"tests/test_{seed}_migrated.py" if seed else "tests/"
    result: dict[str, Any] = {
        "ok": False,
        "seed": seed,
        "path": target_rel,
        "exists": False,
        "written": False,
        "content": "",
        "warnings": [],
        "errors": [],
    }
    path, rel, errors = _resolve_seed(root_path, seed)
    if errors or path is None:
        result["errors"] = errors
        return result

    target = root_path / "tests" / f"test_{seed}_migrated.py"
    try:
        inside = target.resolve().relative_to(root_path)
    except ValueError:
        result["errors"] = [f"{target_rel}: path escapes the repository root"]
        result["ok"] = False
        return result
    result["path"] = inside.as_posix()
    result["exists"] = target.exists()
    if result["exists"]:
        result["warnings"].append(
            f"{result['path']} already exists; the preview does not overwrite it")
    result["content"] = (
        f'"""Migrated gate for the `{seed}` seed (preview skeleton).\n\n'
        "Generated by the ozagia Ocelot workspace adapter. Replace the\n"
        "manifest sanity checks with the behaviour this seed must preserve.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        "import json\n"
        "from pathlib import Path\n\n"
        f'SEED = Path(__file__).resolve().parents[1] / "seeds" / "{seed}.json"\n\n\n'
        "def test_manifest_exists() -> None:\n"
        "    assert SEED.is_file()\n\n\n"
        "def test_manifest_id() -> None:\n"
        '    data = json.loads(SEED.read_text(encoding="utf-8"))\n'
        f'    assert data.get("id", "{seed}") == "{seed}"\n'
    )
    result["ok"] = True
    return result


def _restore_bytes(path: Path, backup: str | None) -> str:
    """Atomically restore *backup* over *path*; return the restored sha256."""
    if not backup:
        raise OSError("adapter did not return a backup path")
    raw = Path(backup).read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".ozagia_restore")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:  # pragma: no cover - best effort durability
        pass
    return sha256_bytes(raw)


def seed_edit(root: str | Path, seed_id: str, operations: Any, *,
              write: bool = False, base_sha256: Any = None) -> dict[str, Any]:
    """Preview or apply semantic edits to one top-level Ocelot seed manifest.

    Preview is the default and never writes.  With ``write=True`` the JSON
    adapter writes atomically (with a backup), the written seed is validated,
    and any structural error restores the original bytes before returning
    ``ok: false, rolled_back: true``.
    """
    root_path = Path(root).expanduser().resolve()
    seed = str(seed_id or "").strip()
    result = _base_result(seed, seed, operations)

    if not isinstance(write, bool):
        result["errors"] = ["write must be a boolean"]
        return result
    if not isinstance(operations, list) or not operations:
        result["errors"] = ["operations must be a non-empty list"]
        return result

    path, rel, errors = _resolve_seed(root_path, seed)
    result["path"] = rel
    if errors or path is None:
        result["errors"] = errors
        return result

    try:
        source = read_source(
            root_path, rel, suffixes=(".json",),
            max_bytes=MAX_SOURCE_BYTES, error_cls=CodeEditError)
    except CodeEditError as exc:
        result["errors"] = [str(exc)]
        return result
    result["base_sha256"] = source.sha256

    try:
        original = json.loads(source.text)
    except json.JSONDecodeError as exc:
        result["errors"] = [
            f"{rel}: invalid JSON at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"]
        result["validation_before"] = validate_seed(root_path, seed)
        return result
    if not isinstance(original, dict):
        result["errors"] = [f"{rel}: manifest is not a JSON object"]
        result["validation_before"] = validate_seed(root_path, seed)
        return result

    result["validation_before"] = validate_seed(root_path, seed)
    translator = _Translator(original)
    try:
        for operation in operations:
            _apply_operation(translator, operation)
        translated = translator.ops()
        if not translated:
            raise _SeedEditError("operations produced no JSON edit")
        script = {
            "file": rel,
            "base_sha256": base_sha256 or source.sha256,
            "ops": translated,
        }
        preview = apply_json_edit_script(root_path, script, write=False)
    except (CodeEditError, _SeedEditError) as exc:
        result["errors"] = [str(exc)]
        return result

    prospective = validate_seed_data(
        root_path, seed, translator.working, rel=rel)
    result.update({
        "base_sha256": preview["base_sha256"],
        "new_sha256": preview["new_sha256"],
        "ops": preview["ops"],
        "changes": preview["changes"],
        "diff_view": preview.get("diff_view") or "",
        "validation_after": prospective,
        "test_skeleton": test_skeleton(root_path, seed),
    })

    if not write:
        result["ok"] = bool(prospective.get("ok"))
        result["warnings"].append(
            "preview only: no file was written. validation_after is computed "
            "from prospective manifest data; apply re-validates the written "
            "seed and restores the backup if validation reports errors.")
        if not prospective.get("ok"):
            result["warnings"].append(
                "prospective validation has errors; an apply would be "
                "written and then rolled back")
        return result

    try:
        applied = apply_json_edit_script(root_path, script, write=True)
    except CodeEditError as exc:
        result["errors"] = [str(exc)]
        return result

    result.update({
        "applied": True,
        "base_sha256": applied["base_sha256"],
        "new_sha256": applied["new_sha256"],
        "ops": applied["ops"],
        "changes": applied["changes"],
        "diff_view": applied.get("diff_view") or "",
        "backup": applied["backup"],
    })

    try:
        written_validation = validate_seed(root_path, seed)
    except Exception as exc:  # noqa: BLE001 - validation must never be skipped
        written_validation = {
            "ok": False,
            "seed": seed,
            "path": rel,
            "errors": [f"post-write validation failed unexpectedly: {exc}"],
            "warnings": [],
            "checks": [],
            "counts": {},
            "partial": False,
        }
    result["validation_after"] = written_validation

    integrity_error: str | None = None
    try:
        written = read_source(
            root_path, rel, suffixes=(".json",),
            max_bytes=MAX_SOURCE_BYTES, error_cls=CodeEditError)
        if json.loads(written.text) != translator.working:
            integrity_error = (
                "written manifest differs from the prospective edit; "
                "refusing to keep it")
    except (CodeEditError, json.JSONDecodeError) as exc:
        integrity_error = f"written manifest cannot be checked: {exc}"

    if written_validation.get("ok") and integrity_error is None:
        result["ok"] = True
        return result

    rollback_errors = list(written_validation.get("errors") or [])
    if integrity_error is not None:
        rollback_errors.insert(0, integrity_error)
    try:
        restored_sha = _restore_bytes(path, result.get("backup"))
    except Exception as exc:  # noqa: BLE001 - report an unrecoverable state
        result["ok"] = False
        result["rolled_back"] = False
        result["rollback_error"] = str(exc)
        result["errors"] = rollback_errors + [
            f"rollback failed: {exc}; the seed remains the invalid edit"]
        return result

    result["ok"] = False
    result["applied"] = False
    result["rolled_back"] = True
    result["restored_sha256"] = restored_sha
    result["errors"] = rollback_errors
    result["warnings"].append(
        "edit rolled back: the original seed bytes were restored")
    if restored_sha != result.get("base_sha256"):
        result["warnings"].append(
            "restored bytes do not match the original base hash; inspect the "
            "backup before retrying")
    return result


def seed_edit_tool(memory: Any, seed_id: str, operations: Any, *,
                   write: bool = False) -> dict[str, Any]:
    """Adapter helper: edit a seed in the memory's repository root."""
    root = getattr(memory, "raw_repo_root", "")
    if not root:
        result = _base_result(str(seed_id or ""), str(seed_id or ""),
                              operations)
        result["errors"] = ["memory has no raw_repo_root"]
        return result
    return seed_edit(root, str(seed_id or ""), operations, write=write)


def _tool_seed_edit(memory: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        args = {}
    write = args.get("write", False)
    if not isinstance(write, bool):
        result = _base_result(
            str(args.get("id") or ""), str(args.get("id") or ""),
            args.get("operations"))
        result["errors"] = ["write must be a boolean"]
        return result
    return seed_edit_tool(
        memory, str(args.get("id") or ""), args.get("operations"),
        write=write)


SEED_EDIT_TOOL = {
    "name": "ocelot_seed_edit",
    "description": (
        "Preview-first semantic editing of an Ocelot seed manifest: append, "
        "replace or delete rules, nodes, edges, productions, reflexes and "
        "concepts; allocate local_id values; set version, description, "
        "self_load_only, area, order, latency_class, capability_keys and "
        "depends_on. Preview is the default; write=true applies atomically "
        "with a backup, validates the written seed and rolls back on errors. "
        "Generated and acquired seed caches are refused."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "operations": {
                "type": "array",
                "items": {"type": "object"},
            },
            "write": {"type": "boolean", "default": False},
        },
        "required": ["id", "operations"],
        "additionalProperties": False,
    },
}


def register_seed_edit(registry: Any) -> None:
    """Register the preview-first seed editor as an MCP tool."""
    if getattr(registry, "_ocelot_seed_edit_registered", False):
        return
    setattr(registry, "_ocelot_seed_edit_registered", True)
    registry.add_mcp_tool(SEED_EDIT_TOOL, _tool_seed_edit)


__all__ = [
    "BLOCKED_SEED_DIRS",
    "KIND_CONTAINERS",
    "METADATA_FIELDS",
    "SEED_EDIT_TOOL",
    "register_seed_edit",
    "seed_edit",
    "seed_edit_tool",
    "test_skeleton",
]
