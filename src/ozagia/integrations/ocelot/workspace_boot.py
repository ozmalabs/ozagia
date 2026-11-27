"""Preview-first editing of Ocelot boot lists and the seed registry.

The Ocelot workspace plan keeps ``substrate/boot_core.py``,
``substrate/boot_profiles.py`` and ``domains/seed_registry.py`` out of ordinary
seed edits: changing them is a separate, explicitly requested operation.  This
module implements that request with byte-preserving Python AST span edits::

    boot_edit(root, [
        {"op": "add_boot", "file": "substrate/boot_core.py",
         "name": "CORE_SEEDS", "seed": "my_seed"},
        {"op": "add_registry", "seed": "my_seed", "version": "1.0.0",
         "depends_on": ["core"], "description": "My seed."},
    ])

Only these targets are editable:

    substrate/boot_core.py       CORE_SEEDS, OPTIONAL_CORE_SEEDS
    substrate/boot_profiles.py   CONVERSATION_SEED_ANCHORS,
                                 CONVERSATION_SELF_IMPROVEMENT_ANCHORS
    domains/seed_registry.py     REGISTRY.register(Seed(...))

The target files are parsed with :mod:`ast` and never imported or executed.
Adding/removing a boot seed edits only the exact tuple/frozenset element; the
rest of the file keeps its bytes, comments and line endings.  A registry add
appends one standard ``REGISTRY.register(Seed(...))`` block using
``_installer_from_manifest(<id>)`` and refuses a manifest whose ``id``,
``version`` or ``depends_on`` disagrees with the requested entry.

Preview is the default.  ``write=True`` writes through
:mod:`ozagia.edit.txn` (atomic replace, backup), re-parses every written file,
verifies the target value/entry, and restores the backup bytes on any failure.
No Ocelot import, model call, network access or target test execution is
involved.

Register it alongside the workspace adapter::

    [adapters]
    modules = [
      "ozagia.integrations.ocelot.workspace:register",
      "ozagia.integrations.ocelot.workspace_boot:register_boot_edit",
    ]
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ozagia.edit.code_edit import MAX_SOURCE_BYTES, CodeEditError
from ozagia.edit.txn import (
    SourceFile,
    check_base,
    encode_text,
    read_source,
    sha256_bytes,
    write_source,
)

from .workspace_validate import SAFE_SEED_ID_RE, validate_seed
from .workspace_validate import _read_manifest

BOOT_FILES: dict[str, tuple[str, ...]] = {
    "substrate/boot_core.py": ("CORE_SEEDS", "OPTIONAL_CORE_SEEDS"),
    "substrate/boot_profiles.py": (
        "CONVERSATION_SEED_ANCHORS",
        "CONVERSATION_SELF_IMPROVEMENT_ANCHORS",
    ),
}
REGISTRY_FILE = "domains/seed_registry.py"
REGISTRY_NAME = "REGISTRY"
REGISTRY_ADD_OP = "add_registry"
REGISTRY_REMOVE_OP = "remove_registry"
BOOT_ADD_OP = "add_boot"
BOOT_REMOVE_OP = "remove_boot"
SUPPORTED_OPS = (BOOT_ADD_OP, BOOT_REMOVE_OP, REGISTRY_ADD_OP, REGISTRY_REMOVE_OP)
BLOCKED_SEED_DIRS = ("acquired", "generated")
_DIFF_MAX_LINES = 400
_MISSING = object()


class _BootEditError(Exception):
    """A refused operation or an unsafe boot/registry target."""


@dataclass(frozen=True)
class _Literal:
    """The AST value, its inner container and the two kind names."""

    value: ast.AST
    container: ast.AST
    kind: str     # "tuple" or "set" for the inner literal
    outer: str    # "tuple", "set" or "frozenset"


@dataclass
class _FileState:
    rel: str
    path: Path
    source: SourceFile
    text: str
    newline: str
    boot_expected: dict[str, list[str]] = field(default_factory=dict)
    registry_added: dict[str, dict[str, Any]] = field(default_factory=dict)
    registry_removed: set[str] = field(default_factory=set)
    backup: str | None = None
    restored_sha256: str | None = None
    parse_error: str | None = None


# --------------------------------------------------------------------------- #
# Result and small utilities
# --------------------------------------------------------------------------- #

def _base_result(operations: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "applied": False,
        "operations": list(operations) if isinstance(operations, list) else [],
        "ops": [],
        "changes": [],
        "files": [],
        "base_sha256": None,
        "new_sha256": None,
        "backup": None,
        "rolled_back": False,
        "errors": [],
        "warnings": [],
        "validation_before": None,
        "validation_after": None,
        "diff_view": "",
    }


def _line_starts(text: str) -> list[int]:
    return [0, *(match.end() for match in re.finditer("\n", text))]


def _line_start(text: str, offset: int) -> int:
    return text.rfind("\n", 0, offset) + 1


def _line_end(text: str, offset: int) -> int:
    newline = text.find("\n", offset)
    return len(text) if newline < 0 else newline


def _line_break_end(text: str, offset: int) -> int:
    newline = text.find("\n", offset)
    return len(text) if newline < 0 else newline + 1


def _line_indent(text: str, offset: int) -> str:
    start = _line_start(text, offset)
    line = text[start:offset]
    return line[:len(line) - len(line.lstrip())]


def _ast_offset(text: str, starts: list[int], lineno: int,
                byte_col: int) -> int:
    """Convert an AST (line, UTF-8 byte column) pair to a character offset."""
    if lineno < 1 or lineno > len(starts):
        raise _BootEditError(f"AST line {lineno} is outside the file")
    start = starts[lineno - 1]
    line = text[start:_line_end(text, start)]
    prefix = line.encode("utf-8")[:max(0, byte_col)]
    return start + len(prefix.decode("utf-8", errors="ignore"))


def _ast_span(text: str, starts: list[int],
              node: ast.AST) -> tuple[int, int]:
    return (
        _ast_offset(text, starts, node.lineno, node.col_offset),
        _ast_offset(text, starts, node.end_lineno, node.end_col_offset),
    )


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    if not edits:
        return text
    for start, end, replacement in sorted(
            edits, key=lambda item: (item[0], item[1]), reverse=True):
        if not 0 <= start <= end <= len(text):
            raise _BootEditError("internal error: edit span is outside the file")
        text = text[:start] + replacement + text[end:]
    return text


def _strip_one_break(text: str, newline: str) -> str:
    if text.startswith(newline):
        return text[len(newline):]
    for marker in ("\r\n", "\n", "\r"):
        if text.startswith(marker):
            return text[len(marker):]
    return text


def _quote_style(text: str, starts: list[int],
                 elements: list[ast.AST]) -> str:
    quotes: set[str] = set()
    for element in elements:
        start, end = _ast_span(text, starts, element)
        match = re.match(r"[rRuU]*(['\"])", text[start:end])
        if match:
            quotes.add(match.group(1))
    return "'" if quotes == {"'"} else '"'


def _diff(rel: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}",
    )
    return "".join(list(lines)[:_DIFF_MAX_LINES])


# --------------------------------------------------------------------------- #
# Boot literals
# --------------------------------------------------------------------------- #

def _literal_of(value: ast.AST) -> _Literal | None:
    if isinstance(value, ast.Tuple):
        return _Literal(value=value, container=value, kind="tuple",
                        outer="tuple")
    if isinstance(value, ast.Set):
        return _Literal(value=value, container=value, kind="set",
                        outer="set")
    if (isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in ("frozenset", "set")
            and not value.keywords):
        if not value.args:
            return _Literal(value=value, container=value, kind="set",
                            outer=value.func.id)
        if len(value.args) != 1 or value.func.id != "frozenset":
            return None
        argument = value.args[0]
        if isinstance(argument, ast.Tuple):
            return _Literal(value=value, container=argument, kind="tuple",
                            outer="frozenset")
        if isinstance(argument, ast.Set):
            return _Literal(value=value, container=argument, kind="set",
                            outer="frozenset")
    return None


def _container_elements(container: ast.AST) -> list[ast.AST]:
    if isinstance(container, (ast.Tuple, ast.Set, ast.List)):
        return list(container.elts)
    return []


def _find_boot_literal(text: str, name: str) -> _Literal:
    """The first assignment of `name` whose value is the base literal."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise _BootEditError(
            f"cannot parse Python at line {exc.lineno}: {exc.msg}") from exc
    candidates: list[_Literal] = []
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if (isinstance(node.target, ast.Name)
                    and node.target.id == name
                    and node.value is not None):
                value = node.value
        if value is None:
            continue
        literal = _literal_of(value)
        if literal is not None:
            candidates.append(literal)
    if not candidates:
        raise _BootEditError(
            f"no base tuple/set/frozenset assignment to {name}; the allowlist "
            "only edits the literal declaration")
    if len(candidates) > 1:
        raise _BootEditError(
            f"{name} has {len(candidates)} literal assignments; refusing an "
            "ambiguous edit")
    return candidates[0]


def _literal_elements(text: str, starts: list[int],
                      literal: _Literal) -> list[tuple[int, str, int, int]]:
    elements: list[tuple[int, str, int, int]] = []
    for index, element in enumerate(_container_elements(literal.container)):
        if not (isinstance(element, ast.Constant)
                and isinstance(element.value, str)):
            raise _BootEditError(
                "the target literal contains a non-string entry; refusing to "
                "edit it")
        start, end = _ast_span(text, starts, element)
        elements.append((index, element.value, start, end))
    return elements


def _insert_boot_seed(text: str, literal: _Literal, seed: str,
                      newline: str) -> str:
    starts = _line_starts(text)
    elements = _literal_elements(text, starts, literal)
    quote = _quote_style(
        text, starts, _container_elements(literal.container))
    quoted = f"{quote}{seed}{quote}"
    container_start, container_end = _ast_span(
        text, starts, literal.container)
    if not elements and isinstance(literal.container, ast.Call):
        # An empty `set()`/`frozenset()` has no literal to insert into.
        value_start, value_end = _ast_span(text, starts, literal.value)
        if literal.outer == "frozenset":
            replacement = f"frozenset({{{quoted}}})"
        else:
            replacement = f"{{{quoted}}}"
        return text[:value_start] + replacement + text[value_end:]
    if (container_end <= container_start
            or text[container_start] not in "({"):
        raise _BootEditError(
            "the target literal is not a parenthesised tuple/set; refusing to "
            "guess at its delimiters")
    close = container_end - 1
    if not elements:
        addition = quoted + ("," if literal.kind == "tuple" else "")
        return text[:close] + addition + text[close:]

    last = _container_elements(literal.container)[-1]
    last_end = _ast_span(text, starts, last)[1]
    between = text[last_end:close]
    close_line_start = _line_start(text, close)
    close_own_line = not text[close_line_start:close].strip()
    has_comma = "," in between

    if "\n" in between and close_own_line:
        indent = _line_indent(text, last_end) or " " * 4
        addition = f"{indent}{quoted},{newline}"
        edits = [(close_line_start, close_line_start, addition)]
        if not has_comma:
            edits.append((last_end, last_end, ","))
        return _splice(text, edits)
    if "\n" in text[container_start:close]:
        # A multiline literal whose closing delimiter shares the last
        # element's line still gets a new element line.
        indent = _line_indent(text, last_end) or " " * 4
        addition = f"{newline}{indent}{quoted}" + ("," if has_comma else "")
        edits = [(close, close, addition)]
        if not has_comma:
            edits.append((last_end, last_end, ","))
        return _splice(text, edits)
    if has_comma:
        return text[:close] + f" {quoted}," + text[close:]
    return text[:close] + f", {quoted}" + text[close:]


def _after_element_line(text: str, element_end: int, close: int) -> int:
    position = element_end
    while position < close and text[position] in " \t":
        position += 1
    if position < close and text[position] == ",":
        position += 1
    while position < close and text[position] in " \t":
        position += 1
    if position < len(text) and text.startswith("\r\n", position):
        return position + 2
    if position < len(text) and text[position] in "\r\n":
        return position + 1
    return position


def _after_element_same_line(text: str, element_end: int, close: int) -> int:
    position = element_end
    while position < close and text[position] in " \t":
        position += 1
    if position < close and text[position] == ",":
        position += 1
    while position < close and text[position] in " \t":
        position += 1
    return position


def _remove_boot_seed(text: str, literal: _Literal, index: int) -> str:
    starts = _line_starts(text)
    elements = _literal_elements(text, starts, literal)
    if len(elements) == 1:
        value_start, value_end = _ast_span(text, starts, literal.value)
        replacement = {
            "tuple": "()",
            "set": "set()",
            "frozenset": "frozenset()",
        }[literal.outer]
        return text[:value_start] + replacement + text[value_end:]

    nodes = _container_elements(literal.container)
    node = nodes[index]
    element_start, element_end = _ast_span(text, starts, node)
    container_start, container_end = _ast_span(
        text, starts, literal.container)
    close = container_end - 1
    edits: list[tuple[int, int, str]] = []

    if index < len(elements) - 1:
        next_start = _ast_span(text, starts, nodes[index + 1])[0]
        if "\n" in text[element_end:next_start]:
            line_start = _line_start(text, element_start)
            start = (line_start if not text[line_start:element_start].strip()
                     else element_start)
            end = _line_start(text, next_start)
            if end <= start:
                end = next_start
        else:
            start, end = element_start, next_start
        edits.append((start, end, ""))
    else:
        previous_end = _ast_span(text, starts, nodes[index - 1])[1]
        own_line = (
            "\n" in text[previous_end:element_start]
            and not text[_line_start(text, element_start):
                         element_start].strip())
        if own_line:
            end = _after_element_line(text, element_end, close)
            if "\n" in text[element_end:end]:
                start = _line_start(text, element_start)
            else:
                # Keep the line's indentation and a closing delimiter that
                # shares this line: only the quoted element and its comma go.
                start = element_start
            edits.append((start, end, ""))
        else:
            start = previous_end
            end = _after_element_same_line(text, element_end, close)
            replacement = (
                "," if literal.kind == "tuple" and len(elements) == 2 else "")
            edits.append((start, end, replacement))

    if literal.kind == "tuple" and len(elements) == 2:
        remaining_index = 1 if index == 0 else 0
        remaining = nodes[remaining_index]
        remaining_end = _ast_span(text, starts, remaining)[1]
        if index == 0:
            tail = text[remaining_end:close]
            if "," not in tail:
                edits.append((remaining_end, remaining_end, ","))
        else:
            previous_end = _ast_span(text, starts, nodes[0])[1]
            if "\n" in text[previous_end:element_start]:
                # The remaining element keeps its own line; add the tuple's
                # required trailing comma when that line has none.
                tail = text[remaining_end:element_start]
                if "," not in tail:
                    edits.append((remaining_end, remaining_end, ","))
    return _splice(text, edits)


# --------------------------------------------------------------------------- #
# Registry statements
# --------------------------------------------------------------------------- #

def _registry_statements(
        text: str) -> list[tuple[ast.stmt, ast.Call, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise _BootEditError(
            f"cannot parse Python at line {exc.lineno}: {exc.msg}") from exc
    out: list[tuple[ast.stmt, ast.Call, str]] = []
    for statement in tree.body:
        if (not isinstance(statement, ast.Expr)
                or not isinstance(statement.value, ast.Call)):
            continue
        call = statement.value
        if (not isinstance(call.func, ast.Attribute)
                or call.func.attr != "register"
                or not isinstance(call.func.value, ast.Name)
                or call.func.value.id != REGISTRY_NAME):
            continue
        if len(call.args) != 1 or not isinstance(call.args[0], ast.Call):
            continue
        seed_call = call.args[0]
        if (not isinstance(seed_call.func, ast.Name)
                or seed_call.func.id != "Seed"):
            continue
        id_keyword = next(
            (keyword for keyword in seed_call.keywords
             if keyword.arg == "id"), None)
        if (id_keyword is None
                or not isinstance(id_keyword.value, ast.Constant)
                or not isinstance(id_keyword.value.value, str)):
            continue
        out.append((statement, seed_call, id_keyword.value.value))
    return out


def _keyword_value(seed_call: ast.Call, name: str) -> Any:
    keyword = next(
        (item for item in seed_call.keywords if item.arg == name), None)
    if keyword is None:
        return _MISSING
    return keyword.value


def _constant_string(node: Any) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _depends_on(node: Any) -> list[str] | None:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_constant_string(item) for item in node.elts]
        if all(value is not None for value in values):
            return [value for value in values if value is not None]
    return None


def _installer_seed(node: Any) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if (not isinstance(node.func, ast.Name)
            or node.func.id != "_installer_from_manifest"
            or len(node.args) != 1 or node.keywords):
        return None
    return _constant_string(node.args[0])


def _registry_entries(text: str) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for _statement, seed_call, seed_id in _registry_statements(text):
        if seed_id in entries:
            raise _BootEditError(
                f"registry entry {seed_id!r} is declared more than once; "
                "refusing an ambiguous edit")
        version = _constant_string(_keyword_value(seed_call, "version"))
        depends = _depends_on(_keyword_value(seed_call, "depends_on"))
        description = _constant_string(
            _keyword_value(seed_call, "description"))
        installer = _installer_seed(_keyword_value(seed_call, "installer"))
        entries[seed_id] = {
            "id": seed_id,
            "version": version,
            "depends_on": depends,
            "description": description,
            "installer": installer,
        }
    return entries


def _registry_indent(text: str, statements: list[tuple[ast.stmt, ast.Call,
                                                      str]]) -> str:
    if not statements:
        return "    "
    seed_call = statements[0][1]
    keyword = next(
        (item for item in seed_call.keywords if item.arg == "id"), None)
    if keyword is None:
        return "    "
    line_start = _line_start(
        text,
        _ast_offset(text, _line_starts(text), keyword.value.lineno,
                    keyword.value.col_offset))
    line = text[line_start:_line_end(text, line_start)]
    detected = line[:len(line) - len(line.lstrip())]
    return detected or "    "


def _tuple_literal(values: list[str]) -> str:
    quoted = [json.dumps(value, ensure_ascii=False) for value in values]
    if not quoted:
        return "()"
    if len(quoted) == 1:
        return f"({quoted[0]},)"
    return "(" + ", ".join(quoted) + ")"


def _registry_block(indent: str, seed: str, version: str,
                    depends_on: list[str], description: str,
                    newline: str) -> str:
    return newline.join([
        "REGISTRY.register(Seed(",
        (f"{indent}id={json.dumps(seed, ensure_ascii=False)}, "
         f"version={json.dumps(version, ensure_ascii=False)}, "
         f"depends_on={_tuple_literal(depends_on)},"),
        f"{indent}installer=_installer_from_manifest("
        f"{json.dumps(seed, ensure_ascii=False)}),",
        f"{indent}description={json.dumps(description, ensure_ascii=False)},",
        "))",
        "",
    ])


def _registry_add_text(text: str, seed: str, version: str,
                       depends_on: list[str], description: str,
                       newline: str) -> str:
    statements = _registry_statements(text)
    indent = _registry_indent(text, statements)
    block = _registry_block(
        indent, seed, version, depends_on, description, newline)
    if statements:
        last = statements[-1][0]
        starts = _line_starts(text)
        insert_at = _line_break_end(text, starts[last.end_lineno - 1])
        prefix = text[:insert_at]
        rest = text[insert_at:]
        if not prefix.endswith("\n"):
            prefix += newline
        rest = _strip_one_break(rest, newline)
        suffix = f"{newline}{rest}" if rest else ""
        return prefix + newline + block + suffix
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += newline
    separator = newline if prefix.strip() else ""
    return prefix + separator + block


def _registry_remove_text(text: str, seed: str) -> str:
    statements = _registry_statements(text)
    matches = [statement for statement in statements if statement[2] == seed]
    if not matches:
        raise _BootEditError(
            f"no REGISTRY.register(Seed(id={seed!r})) entry to remove")
    if len(matches) > 1:
        raise _BootEditError(
            f"registry entry {seed!r} is declared more than once; refusing an "
            "ambiguous removal")
    statement = matches[0][0]
    starts = _line_starts(text)
    line_start = starts[statement.lineno - 1]
    prefix = text[line_start:_ast_span(text, starts, statement)[0]]
    start = line_start if not prefix.strip() else _ast_span(
        text, starts, statement)[0]
    end = _line_break_end(text, starts[statement.end_lineno - 1])
    while end < len(text):
        next_end = _line_break_end(text, end)
        if text[end:next_end].strip():
            break
        end = next_end
    return text[:start] + text[end:]


# --------------------------------------------------------------------------- #
# Paths, seed ids and manifests
# --------------------------------------------------------------------------- #

def _normalize_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def _resolve_target(root: Path, rel: str) -> Path:
    expected = root / rel
    try:
        resolved = expected.resolve()
    except OSError as exc:
        raise _BootEditError(f"{rel}: cannot resolve path: {exc}") from exc
    if resolved != expected:
        raise _BootEditError(
            f"{rel}: path resolves through a symlink or outside the "
            "repository; refusing to edit it")
    if not expected.is_file():
        raise _BootEditError(f"{rel}: file not found")
    return expected


def _normalize_seed_id(raw: Any, op_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _BootEditError(f"{op_name}: seed must be a non-empty string")
    seed = raw.strip()
    if (seed in (".", "..") or "/" in seed or "\\" in seed
            or Path(seed).is_absolute()
            or not SAFE_SEED_ID_RE.fullmatch(seed)):
        raise _BootEditError(
            f"{op_name}: invalid seed id {seed!r}; expected a filename stem "
            "such as 'my_seed'. Nested, generated/acquired and escaping paths "
            "are refused")
    return seed


def _seed_manifest_path(root: Path, seed: str) -> Path:
    seeds = root / "seeds"
    expected = seeds / f"{seed}.json"
    try:
        resolved = expected.resolve()
    except OSError as exc:
        raise _BootEditError(
            f"seeds/{seed}.json: cannot resolve path: {exc}") from exc
    if resolved != expected:
        raise _BootEditError(
            f"seeds/{seed}.json: manifest resolves through a symlink; "
            "refusing to use it")
    if not expected.is_file():
        for cache in BLOCKED_SEED_DIRS:
            cached = seeds / cache / f"{seed}.json"
            if cached.is_file():
                raise _BootEditError(
                    f"seeds/{cache}/{seed}.json is a generated cache; edit the "
                    "source seed instead")
        raise _BootEditError(f"seed manifest not found: seeds/{seed}.json")
    return expected


def _check_seed_not_cached(root: Path, seed: str, op_name: str) -> None:
    manifest = root / "seeds" / f"{seed}.json"
    if manifest.is_symlink():
        raise _BootEditError(
            f"seeds/{seed}.json: manifest resolves through a symlink; "
            "refusing to use it")
    if manifest.is_file():
        return
    for cache in BLOCKED_SEED_DIRS:
        cached = root / "seeds" / cache / f"{seed}.json"
        if cached.is_file():
            raise _BootEditError(
                f"{op_name}: seeds/{cache}/{seed}.json is a generated cache; "
                "edit the source seed instead")


def _manifest_agreement(root: Path, seed: str, version: str,
                        depends_on: list[str]) -> tuple[list[str], dict[str, Any]]:
    try:
        path = _seed_manifest_path(root, seed)
    except _BootEditError as exc:
        return [str(exc)], {}
    data, _partial, error = _read_manifest(path)
    rel = f"seeds/{seed}.json"
    if error:
        return [f"{rel}: {error}"], {}
    errors: list[str] = []
    actual_id = data.get("id", _MISSING)
    if actual_id is _MISSING:
        errors.append(f"{rel}: manifest has no id; expected {seed!r}")
    elif actual_id != seed:
        errors.append(
            f"{rel}: manifest id {actual_id!r} disagrees with the requested "
            f"seed {seed!r}")
    actual_version = data.get("version", _MISSING)
    if actual_version is _MISSING:
        errors.append(
            f"{rel}: manifest has no version; the registry expects "
            f"{version!r}")
    elif actual_version != version:
        errors.append(
            f"{rel}: manifest version {actual_version!r} disagrees with the "
            f"registry version {version!r}")
    actual_depends = data.get("depends_on", [])
    if not isinstance(actual_depends, list) or actual_depends != depends_on:
        errors.append(
            f"{rel}: manifest depends_on {actual_depends!r} disagrees with "
            f"the registry depends_on {depends_on!r}")
    try:
        report = validate_seed(root, seed)
    except Exception as exc:  # noqa: BLE001 - validator must not escape
        errors.append(f"{rel}: manifest validation failed unexpectedly: {exc}")
    else:
        if not report.get("ok"):
            errors.extend(
                f"{rel}: {message}" for message in report.get("errors") or [])
    return errors, data


# --------------------------------------------------------------------------- #
# Operation normalization and validation snapshots
# --------------------------------------------------------------------------- #

def _normalize_operations(
        operations: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, operation in enumerate(operations):
        where = f"operations[{index}]"
        if not isinstance(operation, Mapping):
            errors.append(f"{where}: each operation must be an object")
            continue
        raw_op = operation.get("op")
        if not isinstance(raw_op, str) or not raw_op.strip():
            errors.append(f"{where}: operation needs an 'op'")
            continue
        op_name = raw_op.strip().lower()
        if op_name in (BOOT_ADD_OP, BOOT_REMOVE_OP):
            raw_file = operation.get("file")
            if not isinstance(raw_file, str) or not raw_file.strip():
                errors.append(
                    f"{where} ({op_name}): file must be a non-empty "
                    "repository-relative path")
                continue
            rel = raw_file.strip()
            if Path(rel).is_absolute() or rel.startswith("~"):
                errors.append(
                    f"{where} ({op_name}): absolute paths are refused: "
                    f"{raw_file!r}")
                continue
            if "\\" in rel:
                errors.append(
                    f"{where} ({op_name}): backslash paths are refused: "
                    f"{raw_file!r}")
                continue
            if ".." in PurePosixPath(rel).parts:
                errors.append(
                    f"{where} ({op_name}): path escapes the repository root: "
                    f"{raw_file!r}")
                continue
            if rel not in BOOT_FILES:
                errors.append(
                    f"{where} ({op_name}): unknown file {raw_file!r}; allowed: "
                    + ", ".join(sorted(BOOT_FILES)))
                continue
            raw_name = operation.get("name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            allowed_names = BOOT_FILES[rel]
            if name not in allowed_names:
                errors.append(
                    f"{where} ({op_name}): unknown name {raw_name!r} for "
                    f"{rel}; allowed: " + ", ".join(allowed_names))
                continue
            seed = operation.get("seed")
            normalized.append({
                "op": op_name,
                "file": rel,
                "name": name,
                "seed": seed,
            })
            continue
        if op_name in (REGISTRY_ADD_OP, REGISTRY_REMOVE_OP):
            raw_file = operation.get("file", REGISTRY_FILE)
            if raw_file != REGISTRY_FILE:
                errors.append(
                    f"{where} ({op_name}): registry operations edit "
                    f"{REGISTRY_FILE} only; refused {raw_file!r}")
                continue
            if "name" in operation:
                errors.append(
                    f"{where} ({op_name}): registry operations do not take "
                    "'name'")
                continue
            seed = operation.get("seed")
            if op_name == REGISTRY_ADD_OP:
                version = operation.get("version")
                depends_on = operation.get("depends_on", [])
                description = operation.get("description", "")
                if not isinstance(version, str) or not version.strip():
                    errors.append(
                        f"{where} (add_registry): version must be a "
                        "non-empty string")
                    continue
                if (not isinstance(depends_on, list)
                        or any(not isinstance(item, str) or not item.strip()
                               for item in depends_on)):
                    errors.append(
                        f"{where} (add_registry): depends_on must be a list "
                        "of non-empty seed ids")
                    continue
                if not isinstance(description, str):
                    errors.append(
                        f"{where} (add_registry): description must be a "
                        "string")
                    continue
                normalized.append({
                    "op": op_name,
                    "file": REGISTRY_FILE,
                    "seed": seed,
                    "version": version.strip(),
                    "depends_on": list(depends_on),
                    "description": description,
                })
            else:
                normalized.append({
                    "op": op_name,
                    "file": REGISTRY_FILE,
                    "seed": seed,
                })
            continue
        errors.append(
            f"{where}: unknown op {raw_op!r}; supported: "
            + ", ".join(SUPPORTED_OPS))
    return normalized, errors


def _base_expectations(
        base_sha256: Any, files: list[str]) -> tuple[list[str], dict[str, str]]:
    if base_sha256 is None:
        return [], {}
    if isinstance(base_sha256, str):
        if len(files) != 1:
            return ([
                "base_sha256 must be a mapping of file path to hash when "
                "operations touch more than one file"], {})
        return [], {files[0]: base_sha256}
    if isinstance(base_sha256, Mapping):
        expected: dict[str, str] = {}
        errors: list[str] = []
        for rel in files:
            value = base_sha256.get(rel)
            if value is None:
                continue
            if not isinstance(value, str):
                errors.append(
                    f"base_sha256[{rel!r}] must be a string")
                continue
            expected[rel] = value
        return errors, expected
    return (["base_sha256 must be a string or a mapping of file path to hash"],
            {})


def _snapshot(text: str, rel: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "ok": True,
        "path": rel,
        "errors": [],
        "warnings": [],
        "targets": {},
        "registry": [],
    }
    try:
        ast.parse(text)
    except SyntaxError as exc:
        snapshot["errors"].append(
            f"{rel}: text does not parse at line {exc.lineno}: {exc.msg}")
        snapshot["ok"] = False
        return snapshot
    if rel in BOOT_FILES:
        for name in BOOT_FILES[rel]:
            try:
                literal = _find_boot_literal(text, name)
                starts = _line_starts(text)
                snapshot["targets"][name] = [
                    value for _index, value, _start, _end
                    in _literal_elements(text, starts, literal)]
            except _BootEditError as exc:
                snapshot["warnings"].append(str(exc))
    if rel == REGISTRY_FILE:
        try:
            snapshot["registry"] = sorted(_registry_entries(text))
        except _BootEditError as exc:
            snapshot["errors"].append(str(exc))
    snapshot["ok"] = not snapshot["errors"]
    return snapshot


def _aggregate(snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    for snapshot in snapshots.values():
        errors.extend(snapshot.get("errors") or [])
        warnings.extend(snapshot.get("warnings") or [])
    return {
        "ok": not errors,
        "files": dict(snapshots),
        "errors": errors,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Applying operations to one file
# --------------------------------------------------------------------------- #

def _state_for(
        root: Path,
        rel: str,
        states: dict[str, _FileState],
        expectations: dict[str, str],
) -> _FileState:
    state = states.get(rel)
    if state is not None:
        return state
    path = _resolve_target(root, rel)
    try:
        source = read_source(
            root, rel, suffixes=(".py",), max_bytes=MAX_SOURCE_BYTES,
            error_cls=CodeEditError)
        check_base(source, expectations.get(rel), CodeEditError)
    except CodeEditError as exc:
        raise _BootEditError(str(exc)) from exc
    try:
        ast.parse(source.text)
    except SyntaxError as exc:
        raise _BootEditError(
            f"{rel}: cannot parse the current file at line {exc.lineno}: "
            f"{exc.msg}; refusing to edit it") from exc
    state = _FileState(
        rel=rel,
        path=path,
        source=source,
        text=source.text,
        newline=source.newline,
    )
    states[rel] = state
    return state


def _apply_boot_add(root: Path, state: _FileState,
                    operation: Mapping[str, Any]) -> None:
    op_name = operation["op"]
    seed = _normalize_seed_id(operation.get("seed"), op_name)
    _check_seed_not_cached(root, seed, op_name)
    name = operation["name"]
    literal = _find_boot_literal(state.text, name)
    starts = _line_starts(state.text)
    values = [value for _index, value, _start, _end
              in _literal_elements(state.text, starts, literal)]
    if seed in values:
        raise _BootEditError(
            f"{state.rel}: {name} already contains seed {seed!r}; refusing a "
            "duplicate")
    if name not in state.boot_expected:
        state.boot_expected[name] = list(values)
    state.text = _insert_boot_seed(
        state.text, literal, seed, state.newline)
    state.boot_expected[name].append(seed)


def _apply_boot_remove(root: Path, state: _FileState,
                       operation: Mapping[str, Any]) -> None:
    op_name = operation["op"]
    seed = _normalize_seed_id(operation.get("seed"), op_name)
    _check_seed_not_cached(root, seed, op_name)
    name = operation["name"]
    literal = _find_boot_literal(state.text, name)
    starts = _line_starts(state.text)
    elements = _literal_elements(state.text, starts, literal)
    matches = [index for index, value, _start, _end in elements
               if value == seed]
    if not matches:
        raise _BootEditError(
            f"{state.rel}: {name} does not contain seed {seed!r}; nothing to "
            "remove")
    if len(matches) > 1:
        raise _BootEditError(
            f"{state.rel}: {name} contains seed {seed!r} more than once; "
            "refusing an ambiguous removal")
    if name not in state.boot_expected:
        state.boot_expected[name] = [value for _index, value, _start, _end
                                     in elements]
    state.text = _remove_boot_seed(state.text, literal, matches[0])
    state.boot_expected[name] = [
        value for value in state.boot_expected[name] if value != seed]


def _apply_registry_add(root: Path, state: _FileState,
                        operation: Mapping[str, Any]) -> None:
    op_name = operation["op"]
    seed = _normalize_seed_id(operation.get("seed"), op_name)
    version = operation["version"]
    depends_on = list(operation["depends_on"])
    description = operation["description"]
    entries = _registry_entries(state.text)
    if seed in entries:
        raise _BootEditError(
            f"{state.rel}: registry already declares seed {seed!r}; refusing a "
            "duplicate")
    agreement_errors, _data = _manifest_agreement(
        root, seed, version, depends_on)
    if agreement_errors:
        raise _BootEditError(
            f"{state.rel}: refusing to register {seed!r}: "
            + "; ".join(agreement_errors))
    state.text = _registry_add_text(
        state.text, seed, version, depends_on, description, state.newline)
    state.registry_added[seed] = {
        "id": seed,
        "version": version,
        "depends_on": depends_on,
        "description": description,
    }
    state.registry_removed.discard(seed)


def _apply_registry_remove(root: Path, state: _FileState,
                           operation: Mapping[str, Any]) -> None:
    op_name = operation["op"]
    seed = _normalize_seed_id(operation.get("seed"), op_name)
    _check_seed_not_cached(root, seed, op_name)
    state.text = _registry_remove_text(state.text, seed)
    state.registry_removed.add(seed)
    state.registry_added.pop(seed, None)


def _apply_operation(root: Path, state: _FileState,
                     operation: Mapping[str, Any]) -> None:
    op_name = operation["op"]
    if op_name == BOOT_ADD_OP:
        _apply_boot_add(root, state, operation)
    elif op_name == BOOT_REMOVE_OP:
        _apply_boot_remove(root, state, operation)
    elif op_name == REGISTRY_ADD_OP:
        _apply_registry_add(root, state, operation)
    elif op_name == REGISTRY_REMOVE_OP:
        _apply_registry_remove(root, state, operation)
    else:  # pragma: no cover - normalization rejects unknown ops
        raise _BootEditError(f"unknown op {op_name!r}")


def _operation_record(operation: Mapping[str, Any]) -> dict[str, Any]:
    record = {"op": operation["op"], "file": operation["file"]}
    if "name" in operation:
        record["name"] = operation["name"]
    record["seed"] = operation["seed"]
    if operation["op"] == REGISTRY_ADD_OP:
        record["version"] = operation["version"]
        record["depends_on"] = list(operation["depends_on"])
        record["installer"] = f"_installer_from_manifest({operation['seed']!r})"
    return record


def _change_record(operation: Mapping[str, Any]) -> dict[str, Any]:
    record = {
        "op": operation["op"],
        "file": operation["file"],
        "seed": operation["seed"],
        "summary": "",
    }
    if operation["op"] == BOOT_ADD_OP:
        record["name"] = operation["name"]
        record["summary"] = (
            f"added {operation['seed']!r} to {operation['name']}")
    elif operation["op"] == BOOT_REMOVE_OP:
        record["name"] = operation["name"]
        record["summary"] = (
            f"removed {operation['seed']!r} from {operation['name']}")
    elif operation["op"] == REGISTRY_ADD_OP:
        record["version"] = operation["version"]
        record["summary"] = (
            f"registered {operation['seed']!r} "
            f"version {operation['version']!r}")
    else:
        record["summary"] = f"unregistered {operation['seed']!r}"
    return record


# --------------------------------------------------------------------------- #
# Result summaries, write verification and rollback
# --------------------------------------------------------------------------- #

def _file_summary(state: _FileState) -> dict[str, Any]:
    new_sha = sha256_bytes(encode_text(state.source, state.text))
    return {
        "path": state.rel,
        "base_sha256": state.source.sha256,
        "new_sha256": new_sha,
        "backup": state.backup,
        "restored_sha256": state.restored_sha256,
        "diff_view": _diff(state.rel, state.source.text, state.text),
        "parse_error": state.parse_error,
    }


def _refresh(result: dict[str, Any], states: dict[str, _FileState],
             order: list[str]) -> None:
    summaries = [_file_summary(states[rel]) for rel in order]
    result["files"] = summaries
    if len(order) == 1:
        summary = summaries[0]
        result["base_sha256"] = summary["base_sha256"]
        result["new_sha256"] = summary["new_sha256"]
        result["backup"] = summary["backup"]
    else:
        result["base_sha256"] = {
            item["path"]: item["base_sha256"] for item in summaries}
        result["new_sha256"] = {
            item["path"]: item["new_sha256"] for item in summaries}
        result["backup"] = {
            item["path"]: item["backup"] for item in summaries}
    result["diff_view"] = "".join(item["diff_view"] for item in summaries)


def _restore_bytes(path: Path, backup: str | None) -> str:
    """Atomically restore *backup* over *path*; return the restored sha256."""
    if not backup:
        raise OSError("adapter did not return a backup path")
    raw = Path(backup).read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.",
        suffix=".ozagia_restore")
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


def _verify_written(root: Path, state: _FileState) -> list[str]:
    try:
        written = read_source(
            root, state.rel, suffixes=(".py",), max_bytes=MAX_SOURCE_BYTES,
            error_cls=CodeEditError)
    except CodeEditError as exc:
        return [f"{state.rel}: written file cannot be checked: {exc}"]
    errors: list[str] = []
    expected_sha = sha256_bytes(encode_text(state.source, state.text))
    if written.sha256 != expected_sha:
        errors.append(
            f"{state.rel}: written bytes differ from the previewed edit")
    try:
        ast.parse(written.text)
    except SyntaxError as exc:
        errors.append(
            f"{state.rel}: written file does not parse at line "
            f"{exc.lineno}: {exc.msg}")
        return errors
    if state.rel in BOOT_FILES:
        for name, expected in state.boot_expected.items():
            try:
                literal = _find_boot_literal(written.text, name)
                starts = _line_starts(written.text)
                actual = [value for _index, value, _start, _end
                          in _literal_elements(written.text, starts, literal)]
            except _BootEditError as exc:
                errors.append(f"{state.rel}: {exc}")
                continue
            if actual != expected:
                errors.append(
                    f"{state.rel}: {name} is {actual!r} after the write, "
                    f"expected {expected!r}")
    if state.rel == REGISTRY_FILE:
        try:
            entries = _registry_entries(written.text)
        except _BootEditError as exc:
            errors.append(f"{state.rel}: {exc}")
            entries = {}
        for seed, spec in state.registry_added.items():
            entry = entries.get(seed)
            if entry is None:
                errors.append(
                    f"{state.rel}: registry entry {seed!r} is missing after "
                    "the write")
            else:
                if entry.get("version") != spec["version"]:
                    errors.append(
                        f"{state.rel}: registry version for {seed!r} is "
                        f"{entry.get('version')!r}, expected "
                        f"{spec['version']!r}")
                if entry.get("depends_on") != spec["depends_on"]:
                    errors.append(
                        f"{state.rel}: registry depends_on for {seed!r} is "
                        f"{entry.get('depends_on')!r}, expected "
                        f"{spec['depends_on']!r}")
                if entry.get("installer") != seed:
                    errors.append(
                        f"{state.rel}: registry installer for {seed!r} is "
                        f"{entry.get('installer')!r}, expected "
                        "_installer_from_manifest")
                if entry.get("description") != spec["description"]:
                    errors.append(
                        f"{state.rel}: registry description for {seed!r} was "
                        "not written exactly")
            agreement_errors, _data = _manifest_agreement(
                root, seed, spec["version"], spec["depends_on"])
            errors.extend(agreement_errors)
        for seed in state.registry_removed:
            if seed in entries:
                errors.append(
                    f"{state.rel}: registry entry {seed!r} is still present "
                    "after the write")
    if state.parse_error:
        errors.append(
            f"{state.rel}: prospective text had a parse error: "
            f"{state.parse_error}")
    return errors


def _rollback(written: list[_FileState]) -> list[str]:
    errors: list[str] = []
    for state in reversed(written):
        try:
            state.restored_sha256 = _restore_bytes(state.path, state.backup)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            errors.append(f"{state.rel}: rollback failed: {exc}")
    return errors


def _write_and_verify(root: Path, states: dict[str, _FileState],
                      order: list[str], result: dict[str, Any]) -> dict[str, Any]:
    written: list[_FileState] = []
    try:
        for rel in order:
            state = states[rel]
            backup = write_source(
                root, state.source, state.text, error_cls=CodeEditError)
            state.backup = str(backup)
            written.append(state)
    except Exception as exc:  # noqa: BLE001 - any write failure rolls back
        rollback_errors = _rollback(written)
        result["errors"] = [f"write failed: {exc}", *rollback_errors]
        result["applied"] = False
        result["rolled_back"] = bool(written) and not rollback_errors
        _refresh(result, states, order)
        return result

    problems: list[str] = []
    try:
        for rel in order:
            problems.extend(_verify_written(root, states[rel]))
    except Exception as exc:  # noqa: BLE001 - verification must roll back
        problems = [f"post-write verification failed unexpectedly: {exc}"]
    _refresh(result, states, order)
    if not problems:
        result["ok"] = True
        result["applied"] = True
        result["rolled_back"] = False
        result["errors"] = []
        if isinstance(result.get("validation_after"), dict):
            result["validation_after"]["file_written"] = True
        return result

    rollback_errors = _rollback(written)
    result["ok"] = False
    result["applied"] = False
    result["rolled_back"] = bool(written) and not rollback_errors
    result["errors"] = problems + rollback_errors
    if rollback_errors:
        result["warnings"].append(
            "rollback failed; inspect the backup before retrying")
    else:
        result["warnings"].append(
            "edit rolled back: the original bytes were restored")
    if isinstance(result.get("validation_after"), dict):
        result["validation_after"]["ok"] = False
        result["validation_after"]["errors"] = (
            list(result["validation_after"].get("errors") or []) + problems)
        result["validation_after"]["file_written"] = False
        result["validation_after"]["rolled_back"] = result["rolled_back"]
    _refresh(result, states, order)
    return result


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def boot_edit(root: str | Path, operations: Any, *,
              write: bool = False,
              base_sha256: Any = None) -> dict[str, Any]:
    """Preview or apply explicit boot-list and seed-registry edits.

    Preview is the default and never writes.  ``write=True`` writes every
    touched file atomically with a backup, re-parses it, verifies the target
    tuple/frozenset value or registry entry, and restores the original bytes if
    any check fails.
    """
    result = _base_result(operations)
    if not isinstance(write, bool):
        result["errors"] = ["write must be a boolean"]
        return result
    if not isinstance(operations, list) or not operations:
        result["errors"] = ["operations must be a non-empty list"]
        return result

    normalized, errors = _normalize_operations(operations)
    if errors:
        result["errors"] = errors
        return result
    order: list[str] = []
    for operation in normalized:
        rel = operation["file"]
        if rel not in order:
            order.append(rel)
    base_errors, expectations = _base_expectations(base_sha256, order)
    if base_errors:
        result["errors"] = base_errors
        return result

    root_path = _normalize_root(root)
    states: dict[str, _FileState] = {}
    before: dict[str, dict[str, Any]] = {}
    for rel in order:
        try:
            state = _state_for(root_path, rel, states, expectations)
        except _BootEditError as exc:
            result["errors"] = [str(exc)]
            result["validation_before"] = _aggregate(before)
            return result
        before[rel] = _snapshot(state.source.text, rel)
    result["validation_before"] = _aggregate(before)

    last_for_file: dict[str, int] = {}
    for index, operation in enumerate(normalized):
        last_for_file[operation["file"]] = index
    accepted_ops: list[dict[str, Any]] = []
    accepted_changes: list[dict[str, Any]] = []
    for index, operation in enumerate(normalized):
        state = states[operation["file"]]
        try:
            _apply_operation(root_path, state, operation)
        except _BootEditError as exc:
            result["errors"].append(str(exc))
            break
        accepted_ops.append(_operation_record(operation))
        accepted_changes.append(_change_record(operation))
        try:
            ast.parse(state.text)
        except SyntaxError as exc:
            state.parse_error = (
                f"line {exc.lineno}: {exc.msg}")
        else:
            state.parse_error = None
        if state.parse_error and index < last_for_file[operation["file"]]:
            result["errors"].append(
                f"{state.rel}: the edit does not parse "
                f"({state.parse_error}); later operations cannot be applied")
            break

    result["ops"] = accepted_ops
    result["changes"] = accepted_changes
    prospective: dict[str, dict[str, Any]] = {
        rel: _snapshot(states[rel].text, rel) for rel in order
    }
    result["validation_after"] = _aggregate(prospective)
    if result["errors"]:
        _refresh(result, states, order)
        return result

    if not result["validation_after"]["ok"]:
        result["errors"] = list(result["validation_after"]["errors"])
        if not write:
            _refresh(result, states, order)
            return result
        result["warnings"].append(
            "the prospective text does not parse or verify; write=True will "
            "write it and restore the backup when post-write validation fails")

    if not write:
        result["ok"] = bool(result["validation_after"]["ok"])
        _refresh(result, states, order)
        result["warnings"].append(
            "preview only: no file was written. validation_after is computed "
            "from the prospective text; write=true re-parses every written "
            "file and restores the backup on any failure.")
        return result

    result = _write_and_verify(root_path, states, order, result)
    return result


# --------------------------------------------------------------------------- #
# Adapter tool (registered by the workspace adapter when wired in)
# --------------------------------------------------------------------------- #

def boot_edit_tool(memory: Any, operations: Any, *, write: bool = False,
                   base_sha256: Any = None) -> dict[str, Any]:
    """Adapter helper: edit the boot/registry files in the memory root."""
    root = getattr(memory, "raw_repo_root", "")
    if not root:
        result = _base_result(operations)
        result["errors"] = ["memory has no raw_repo_root"]
        return result
    return boot_edit(root, operations, write=write, base_sha256=base_sha256)


def _tool_boot_edit(memory: Any, args: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        args = {}
    write = args.get("write", False)
    if not isinstance(write, bool):
        result = _base_result(args.get("operations"))
        result["errors"] = ["write must be a boolean"]
        return result
    return boot_edit_tool(
        memory, args.get("operations"), write=write,
        base_sha256=args.get("base_sha256"))


BOOT_EDIT_TOOL = {
    "name": "ocelot_boot_edit",
    "description": (
        "Preview-first explicit editing of Ocelot boot lists and "
        "domains/seed_registry.py. Boot ops add/remove one seed in "
        "CORE_SEEDS, OPTIONAL_CORE_SEEDS, CONVERSATION_SEED_ANCHORS or "
        "CONVERSATION_SELF_IMPROVEMENT_ANCHORS. Registry ops append or remove "
        "one REGISTRY.register(Seed(...)) block and refuse a manifest whose "
        "id, version or depends_on disagrees. Preview is the default; "
        "write=true writes atomically with a backup, re-parses every file and "
        "rolls back on any failure."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {"type": "object"},
            },
            "write": {"type": "boolean", "default": False},
            "base_sha256": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "object"},
                ],
            },
        },
        "required": ["operations"],
        "additionalProperties": False,
    },
}


def register_boot_edit(registry: Any) -> None:
    """Register the preview-first boot/registry editor as an MCP tool."""
    if getattr(registry, "_ocelot_boot_edit_registered", False):
        return
    setattr(registry, "_ocelot_boot_edit_registered", True)
    registry.add_mcp_tool(BOOT_EDIT_TOOL, _tool_boot_edit)


__all__ = [
    "BLOCKED_SEED_DIRS",
    "BOOT_EDIT_TOOL",
    "BOOT_FILES",
    "REGISTRY_FILE",
    "SUPPORTED_OPS",
    "boot_edit",
    "boot_edit_tool",
    "register_boot_edit",
]
