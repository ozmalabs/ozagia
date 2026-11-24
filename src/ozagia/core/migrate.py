"""Migrate legacy ``.ozagia.toml`` settings into ``ozagia/config.toml``.

The legacy file is never deleted, rewritten or moved. Migration copies the
keys that the typed settings registry understands, the project-model tables
that the project loader reads (``[environments]``, ``[services]``,
``[relays]`` and ``[runtime]``), reports every entry it did not copy, and
refuses to replace an entry that already exists in ``ozagia/config.toml`` with
a different one.

Project models move by **named entry**: ``[environments.NAME]``,
``[services.NAME]``, ``[relays.NAME]``, ``[runtime.services.NAME]`` and
``[runtime.tunnels.NAME]``. Every copied entry is validated by the same
project parser as the legacy loader, and the whole entry is written: fields
are never merged across files. A conflicting entry is not written; while a
project-model conflict exists, no project-model table is written.

The command is preview-first: :func:`migrate` defaults to ``dry_run=True`` and
returns the exact unified diff plus a confirmation token. Applying requires
that token, so a stale preview cannot write over a file that changed after it
was shown. Writes are one atomic update; unrelated TOML and comments survive.

Unsupported legacy sections stay where they are. This module does not import
the target project and makes no network or model calls.
"""
from __future__ import annotations

import copy
import datetime
import difflib
import hashlib
import json
import math
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ozagia import config as project_config
from ozagia.config import CONFIG_FILENAME, repo_root
from ozagia.core import settings as S

SCHEMA = 1
#: The legacy project config filename at the repository root.
LEGACY_FILENAME = CONFIG_FILENAME

#: Named project models. ``runtime`` nests two named tables.
PROJECT_MODEL_SECTIONS = ("environments", "services", "relays")
RUNTIME_SECTIONS = ("services", "tunnels")
#: Stable append order for newly copied named tables.
PROJECT_TABLE_ORDER = (
    "environments", "services", "relays",
    "runtime.services", "runtime.tunnels",
)

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


class MigrateError(ValueError):
    """The legacy or target config cannot be migrated."""


@dataclass(frozen=True)
class LegacyKey:
    """How one legacy key maps into the registry."""

    target: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class TableChange:
    """One named project-model table to append, whole."""

    parts: tuple[str, ...]
    value: Mapping[str, Any]

    @property
    def label(self) -> str:
        return ".".join(self.parts)

    @property
    def header(self) -> str:
        return ".".join(_render_key(part) for part in self.parts)


# Every key the legacy loader (``ozagia.config``) accepts. A ``None`` target
# is a deliberate "reported, not migrated" decision.
LEGACY_SECTIONS: dict[str, dict[str, LegacyKey]] = {
    "index": {
        name: LegacyKey(target=f"index.{name}")
        for name in (
            "source_dirs", "map_dirs", "test_dirs", "skip_dirs", "docs",
            "docs_all", "key_docs", "command_docs", "import_roots",
            "path_prefixes", "changed_exclude", "entrypoint_prefixes",
        )
    },
    "memory": {"skip_dirs": LegacyKey(target="memory.skip_dirs")},
    "adapters": {"modules": LegacyKey(target="adapters.modules")},
    "reads": {
        "structured_outline_lines": LegacyKey(
            target="reads.structured_outline_lines"),
        "structured_outline_bytes": LegacyKey(
            target="reads.structured_outline_bytes"),
        "outline_lines": LegacyKey(target="reads.outline_lines"),
        "outline_bytes": LegacyKey(target="reads.outline_bytes"),
        "repeat": LegacyKey(target="reads.repeat"),
        # A table, expanded per harness below.
        "repeat_by_harness": LegacyKey(target="reads.repeat_by_harness"),
    },
    "tests": {
        # The registry's captured-pytest setting accepts both legacy values.
        "select": LegacyKey(
            target="testing.captured",
            reason="maps to the registry's captured-pytest mode"),
        "cache": LegacyKey(target="testing.cache"),
    },
    "jobs": {"enabled": LegacyKey(target="jobs.enabled")},
    "lint": {
        "enabled": LegacyKey(target="lint.enabled"),
        "timeout_s": LegacyKey(target="lint.timeout_s"),
        "python": LegacyKey(target="lint.python"),
        "rust": LegacyKey(target="lint.rust"),
        "trust_project_venv": LegacyKey(
            reason="this is a user-level setting; use "
                   "`ozagia config set lint.trust_project_venv true --scope user`"),
    },
    "iac": {
        "roots": LegacyKey(target="iac.roots"),
        "skip_globs": LegacyKey(target="iac.skip_globs"),
        "remote_states": LegacyKey(
            reason="an array of tables has no registry equivalent yet"),
    },
    "review": {"layers": LegacyKey(target="review.layers")},
    "migration": {
        "python": LegacyKey(target="migration.python"),
        "rust": LegacyKey(target="migration.rust"),
        "infer": LegacyKey(target="migration.infer"),
        "pairs": LegacyKey(
            reason="an array of tables has no registry equivalent yet; keep "
                   "[[migration.pairs]] in .ozagia.toml"),
    },
    "security": {
        "enabled": LegacyKey(target="security.enabled"),
        "tools": LegacyKey(target="security.tools"),
        "allow_network": LegacyKey(target="security.allow_network"),
        "fail_on": LegacyKey(target="security.fail_on"),
        "exclude": LegacyKey(target="security.exclude"),
        "max_findings": LegacyKey(target="security.max_findings"),
        "block_secrets": LegacyKey(target="security.block_secrets"),
        "secret_allowlist": LegacyKey(target="security.secret_allowlist"),
        "finding_allowlist": LegacyKey(target="security.finding_allowlist"),
    },
    "hints": {
        "library": LegacyKey(target="hints.library"),
        "max_per_edit": LegacyKey(target="hints.max_per_edit"),
        "record": LegacyKey(target="hints.record"),
        "ecosystems": LegacyKey(target="hints.ecosystems"),
    },
}

#: Legacy sections that are reported but have no migration at all. The project
#: models used to live here; they now migrate named entry by named entry.
UNMAPPED_SECTIONS: dict[str, str] = {}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path: Path, *, what: str) -> str:
    try:
        # Read bytes and decode explicitly so CRLF files stay CRLF: a text
        # read would translate newlines before the writer could preserve them.
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrateError(f"cannot read {what} {path}: {exc}") from exc


def _read_toml(path: Path, text: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(text) if text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise MigrateError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):  # tomllib always returns a mapping
        raise MigrateError(f"{path}: not a TOML table")
    return data


def _suggest(text: str, candidates: list[str]) -> str | None:
    matches = difflib.get_close_matches(text, candidates, n=1, cutoff=0.55)
    return matches[0] if matches else None


def _entry(legacy: str, status: str, *, key: str | None = None,
           value: Any = None, current: Any = None,
           message: str = "", kind: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"legacy": legacy, "status": status}
    if key is not None:
        item["key"] = key
    if status in {"move", "same", "conflict"}:
        item["value"] = value
    if current is not None:
        item["current"] = current
    if message:
        item["message"] = message
    if kind is not None:
        item["kind"] = kind
    return item


def _known_sections() -> list[str]:
    seen = (set(LEGACY_SECTIONS) | set(UNMAPPED_SECTIONS)
            | set(PROJECT_MODEL_SECTIONS) | {"runtime"})
    for item in S.schema():
        seen.add(str(item["section"]).split(".", 1)[0])
    return sorted(seen)


# --------------------------------------------------------------------------- #
# safe TOML table writer
# --------------------------------------------------------------------------- #
def _render_key(name: str) -> str:
    if not isinstance(name, str):
        raise MigrateError(f"cannot serialize a non-string TOML key: {name!r}")
    if _BARE_KEY.fullmatch(name):
        return name
    return json.dumps(name, ensure_ascii=False)


def _render_value(value: Any, *, where: str) -> str:
    """Render one TOML value, refusing anything that cannot round-trip."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not -(2 ** 63) <= value < 2 ** 63:
            raise MigrateError(f"{where}: integer is outside the TOML range")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MigrateError(f"{where}: float must be finite")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(
            _render_value(item, where=f"{where}[{index}]")
            for index, item in enumerate(value)) + "]"
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise MigrateError(
                    f"{where}: inline-table key must be a string, got {key!r}")
            parts.append(f"{_render_key(key)} = "
                         f"{_render_value(item, where=f'{where}.{key}')}")
        if not parts:
            return "{}"
        return "{ " + ", ".join(parts) + " }"
    raise MigrateError(
        f"{where}: cannot serialize a {type(value).__name__} value faithfully")


def _render_table(change: TableChange, newline: str) -> list[str]:
    lines = [f"[{change.header}]{newline}"]
    for key, value in change.value.items():
        lines.append(
            f"{_render_key(key)} = "
            f"{_render_value(value, where=f'{change.label}.{key}')}{newline}")
    return lines


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _insert_expected(root: dict[str, Any], change: TableChange,
                     *, path: Path) -> None:
    """Insert one table into the expected parsed shape, with shape checks."""
    node = root
    for index, part in enumerate(change.parts[:-1]):
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            parent = ".".join(change.parts[:index + 1])
            raise MigrateError(
                f"{path}: [{parent}] is not a table; cannot append "
                f"[{change.header}]")
        node = child
    name = change.parts[-1]
    if name in node:
        raise MigrateError(
            f"{path}: [{change.header}] already exists; preview again")
    node[name] = copy.deepcopy(dict(change.value))


def _append_tables(path: Path, text: str, changes: list[TableChange]) -> str:
    """Append named tables at the end of ``text`` and verify the result.

    Existing comments, unrelated tables and formatting are never rewritten.
    The new text is parsed again and compared with the old parsed shape plus
    exactly the appended tables; a mismatch is refused, never written.
    """
    if not changes:
        return text
    old_data = _read_toml(path, text)
    expected = copy.deepcopy(old_data)
    for change in changes:
        _insert_expected(expected, change, path=path)

    newline = _newline_for(text)
    lines = text.splitlines(keepends=True)
    if lines:
        if not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines[-1].strip():
            lines.append(newline)
    for index, change in enumerate(changes):
        if index:
            lines.append(newline)
        lines.extend(_render_table(change, newline))
    new_text = "".join(lines)

    try:
        new_data = tomllib.loads(new_text) if new_text.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise MigrateError(
            f"{path}: cannot append project-model tables without changing "
            f"the file's meaning ({exc})") from exc
    if new_data != expected:
        raise MigrateError(
            f"{path}: cannot append project-model tables without changing "
            "existing configuration")
    return new_text


def _diff(path: Path, old_text: str, new_text: str) -> str:
    if old_text == new_text:
        return ""
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path), tofile=str(path),
    ))


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _raw_names(data: Mapping[str, Any], section: str) -> frozenset[str]:
    table = data.get(section)
    if not isinstance(table, Mapping):
        return frozenset()
    return frozenset(name for name in table if isinstance(name, str))


def _validation_message(exc: Exception, *, path: Path, label: str) -> str:
    message = str(exc)
    if str(path) not in message:
        message = f"{path}: {message}"
    if f"[{label}]" not in message:
        message = f"{message} ([{label}])"
    return message


def _plan_project_entry(
        entries: list[dict[str, Any]], tables: list[TableChange], *,
        root: Path, legacy_path: Path, section: str, name: str,
        table: Any, target_table: Mapping[str, Any] | None,
        extra_environments: frozenset[str],
        extra_relays: frozenset[str]) -> None:
    label = f"{section}.{name}"
    if not isinstance(name, str):
        entries.append(_entry(
            f"{section}.{name}", "invalid", kind="project",
            message=f"{legacy_path}: [{section}] names must be strings"))
        return
    if not isinstance(table, Mapping):
        entries.append(_entry(
            label, "invalid", kind="project",
            message=f"{legacy_path}: [{label}] must be a table"))
        return

    synthetic: dict[str, Any]
    if section.startswith("runtime."):
        sub = section.split(".", 1)[1]
        synthetic = {"runtime": {sub: {name: table}}}
    else:
        synthetic = {section: {name: table}}
    try:
        project_config._parse(
            root, legacy_path, synthetic,
            extra_environment_names=extra_environments,
            extra_relay_names=extra_relays, partial_migration=True)
    except project_config.ConfigError as exc:
        entries.append(_entry(
            label, "invalid", kind="project", value=dict(table),
            message=_validation_message(exc, path=legacy_path, label=label)))
        return

    if target_table is not None and name in target_table:
        current = target_table[name]
        if current == table:
            entries.append(_entry(
                label, "same", kind="project", value=dict(table),
                message="already present in ozagia/config.toml"))
        else:
            entries.append(_entry(
                label, "conflict", kind="project", value=dict(table),
                current=current,
                message=("ozagia/config.toml already has a different entry; "
                         "it was not replaced")))
        return
    tables.append(TableChange(tuple(section.split(".")) + (name,), dict(table)))
    entries.append(_entry(
        label, "move", kind="project", value=dict(table),
        message=f"copies to [{section}.{name}]"))


def _plan_named_section(
        entries: list[dict[str, Any]], tables: list[TableChange], *,
        root: Path, legacy_path: Path, target_path: Path, section: str,
        payload: Any, target_data: Mapping[str, Any],
        extra_environments: frozenset[str],
        extra_relays: frozenset[str]) -> None:
    if not isinstance(payload, Mapping):
        entries.append(_entry(
            f"[{section}]", "invalid", kind="project",
            message=f"{legacy_path}: [{section}] must contain named tables"))
        return
    target_section = target_data.get(section)
    if target_section is not None and not isinstance(target_section, Mapping):
        raise MigrateError(
            f"{target_path}: [{section}] must be a table to receive "
            "migrated entries")
    for name, table in payload.items():
        _plan_project_entry(
            entries, tables, root=root, legacy_path=legacy_path,
            section=section, name=name, table=table,
            target_table=(target_section if isinstance(target_section, Mapping)
                          else None),
            extra_environments=extra_environments,
            extra_relays=extra_relays)


def _plan_runtime(
        entries: list[dict[str, Any]], tables: list[TableChange], *,
        root: Path, legacy_path: Path, target_path: Path, payload: Any,
        target_data: Mapping[str, Any],
        extra_environments: frozenset[str],
        extra_relays: frozenset[str]) -> None:
    if not isinstance(payload, Mapping):
        entries.append(_entry(
            "[runtime]", "invalid", kind="project",
            message=f"{legacy_path}: [runtime] must be a table"))
        return
    target_runtime = target_data.get("runtime")
    if target_runtime is not None and not isinstance(target_runtime, Mapping):
        raise MigrateError(
            f"{target_path}: [runtime] must be a table to receive migrated "
            "entries")
    for key, sub in payload.items():
        if key not in RUNTIME_SECTIONS:
            entries.append(_entry(
                f"runtime.{key}", "unknown", kind="project",
                message=f"{legacy_path}: unknown key(s) in [runtime]: {key}"))
            continue
        section = f"runtime.{key}"
        if not isinstance(sub, Mapping):
            entries.append(_entry(
                f"[{section}]", "invalid", kind="project",
                message=f"{legacy_path}: [{section}] must contain named tables"))
            continue
        target_sub = (target_runtime.get(key)
                      if isinstance(target_runtime, Mapping) else None)
        if target_sub is not None and not isinstance(target_sub, Mapping):
            raise MigrateError(
                f"{target_path}: [{section}] must be a table to receive "
                "migrated entries")
        for name, table in sub.items():
            _plan_project_entry(
                entries, tables, root=root, legacy_path=legacy_path,
                section=section, name=name, table=table,
                target_table=(target_sub if isinstance(target_sub, Mapping)
                              else None),
                extra_environments=extra_environments,
                extra_relays=extra_relays)


def _plan(root: Path, legacy_path: Path, target_path: Path) -> tuple[
        list[dict[str, Any]], dict[str, Any], list[TableChange],
        str, str]:
    """Return entries, flat values to write, named tables, and both texts."""
    if legacy_path.is_dir():
        raise MigrateError(f"{legacy_path}: is a directory, not a TOML file")
    legacy_text = _read_text(legacy_path, what="legacy config")
    target_text = _read_text(target_path, what="repository config")
    if not legacy_text.strip():
        return [], {}, [], legacy_text, target_text
    data = _read_toml(legacy_path, legacy_text)
    target_data = _read_toml(target_path, target_text)
    try:
        existing, _ = S._parse_layer(target_data, target_path, "repo")
    except (S.SettingsError, ValueError, OSError) as exc:
        raise MigrateError(f"cannot read {target_path}: {exc}") from exc
    known_sections = _known_sections()
    entries: list[dict[str, Any]] = []
    planned: dict[str, Any] = {}
    tables: list[TableChange] = []
    extra_environments = (
        _raw_names(data, "environments") | _raw_names(target_data, "environments"))
    extra_relays = (
        _raw_names(data, "relays") | _raw_names(target_data, "relays"))

    for section, payload in data.items():
        label = f"[{section}]"
        if section in PROJECT_MODEL_SECTIONS:
            _plan_named_section(
                entries, tables, root=root, legacy_path=legacy_path,
                target_path=target_path, section=section, payload=payload,
                target_data=target_data,
                extra_environments=extra_environments,
                extra_relays=extra_relays)
            continue
        if section == "runtime":
            _plan_runtime(
                entries, tables, root=root, legacy_path=legacy_path,
                target_path=target_path, payload=payload,
                target_data=target_data,
                extra_environments=extra_environments,
                extra_relays=extra_relays)
            continue
        if section in UNMAPPED_SECTIONS:
            entries.append(_entry(
                label, "unmapped",
                message=UNMAPPED_SECTIONS[section]))
            continue
        spec = LEGACY_SECTIONS.get(section)
        if spec is None:
            suggestion = _suggest(section, known_sections)
            message = "unknown section; no registry migration"
            if suggestion:
                message += f"; did you mean [{suggestion}]?"
            entries.append(_entry(label, "unknown", message=message))
            continue
        if not isinstance(payload, Mapping):
            entries.append(_entry(
                label, "unknown",
                message="a legacy section must be a TOML table"))
            continue
        for name, value in payload.items():
            dotted = f"{section}.{name}"
            if not isinstance(name, str):
                entries.append(_entry(
                    label, "unknown", message="TOML keys must be strings"))
                continue
            key_spec = spec.get(name)
            if key_spec is None:
                suggestion = _suggest(name, sorted(spec))
                message = "unknown key; no registry migration"
                if suggestion:
                    message += f"; did you mean {suggestion}?"
                entries.append(_entry(dotted, "unknown", message=message))
                continue
            if not key_spec.target:
                entries.append(_entry(
                    dotted, "unmapped", message=key_spec.reason))
                continue
            if dotted == "reads.repeat_by_harness":
                if not isinstance(value, Mapping):
                    entries.append(_entry(
                        dotted, "invalid",
                        message="must be a table of harness = policy entries"))
                    continue
                for harness, policy in value.items():
                    leaf = f"reads.repeat_by_harness.{harness}"
                    _plan_value(
                        entries, planned, existing,
                        legacy=f"{dotted}.{harness}", key=leaf, value=policy,
                        note=key_spec.reason)
                continue
            _plan_value(
                entries, planned, existing, legacy=dotted,
                key=key_spec.target, value=value, note=key_spec.reason)
    order = {name: index for index, name in enumerate(PROJECT_TABLE_ORDER)}
    tables.sort(key=lambda change: order.get(
        ".".join(change.parts[:-1]), len(PROJECT_TABLE_ORDER)))
    return entries, planned, tables, legacy_text, target_text


def _plan_value(entries: list[dict[str, Any]], planned: dict[str, Any],
                existing: Mapping[str, Any], *, legacy: str, key: str,
                value: Any, note: str = "") -> None:
    try:
        cleaned = S.validate_value(key, value)
    except (S.SettingsError, ValueError) as exc:
        entries.append(_entry(
            legacy, "invalid", key=key, message=str(exc)))
        return
    if key in existing:
        if existing[key] == cleaned:
            entries.append(_entry(
                legacy, "same", key=key, value=cleaned,
                message=note or "already present in ozagia/config.toml"))
        else:
            entries.append(_entry(
                legacy, "conflict", key=key, value=cleaned,
                current=existing[key],
                message=("ozagia/config.toml already has a different value; "
                         "it was not replaced")))
        return
    planned[key] = cleaned
    entries.append(_entry(legacy, "move", key=key, value=cleaned,
                          message=note))


def _token(legacy_path: Path, legacy_text: str, target_path: Path,
           target_text: str, planned: Mapping[str, Any],
           tables: list[TableChange]) -> str:
    payload = {
        "legacy": str(legacy_path),
        "legacy_sha256": _digest(legacy_text),
        "target": str(target_path),
        "target_sha256": _digest(target_text),
        "values": dict(planned),
        "tables": [{"path": change.label, "value": change.value}
                   for change in tables],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item["status"]) for item in entries)
    return {key: counts[key] for key in sorted(counts)}


def migrate(root: str | Path | None = None, *, dry_run: bool = True,
            confirm: str | None = None) -> dict[str, Any]:
    """Preview or apply the legacy-to-registry migration.

    ``dry_run`` is the default. When a write is needed, ``confirm`` must be
    the token from a preview of the same legacy and target contents.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    if not root_p.is_dir():
        raise MigrateError(f"repository root does not exist: {root_p}")
    legacy_path = root_p / LEGACY_FILENAME
    target_path = S.repo_config_path(root_p)
    entries, planned, tables, legacy_text, target_text = _plan(
        root_p, legacy_path, target_path)

    project_conflict = any(
        item["status"] == "conflict" and item.get("kind") == "project"
        for item in entries)
    if project_conflict:
        # A conflicting named entry is never overwritten or field-merged.
        # While one exists, no project-model table is written at all.
        tables = []

    written: dict[str, Any] = {}
    base_text = target_text
    if planned:
        try:
            prepared = S._prepare_values(
                root_p, planned, scope="repo", old_text=target_text)
        except (S.SettingsError, ValueError, OSError) as exc:
            raise MigrateError(str(exc)) from exc
        base_text = prepared["new_text"]
        written = prepared["written"]
    new_text = base_text
    if tables:
        try:
            new_text = _append_tables(target_path, base_text, tables)
        except MigrateError:
            raise
        except (OSError, ValueError) as exc:
            raise MigrateError(str(exc)) from exc

    changed = new_text != target_text
    diff = _diff(target_path, target_text, new_text)
    token = (_token(legacy_path, legacy_text, target_path, target_text,
                    planned, tables)
             if planned or tables else None)

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "root": str(root_p),
        "legacy": str(legacy_path),
        "legacy_exists": legacy_path.is_file(),
        "target": str(target_path),
        "target_exists": target_path.is_file(),
        "dry_run": dry_run,
        "ok": not any(item["status"] == "invalid" for item in entries),
        "changed": changed,
        "applied": False,
        "token": token,
        "entries": entries,
        "counts": _counts(entries),
        "conflicts": sum(1 for item in entries
                         if item["status"] == "conflict"),
        "unmapped": sum(1 for item in entries
                        if item["status"] == "unmapped"),
        "unknown": sum(1 for item in entries
                       if item["status"] == "unknown"),
        "moved_keys": [item["legacy"] for item in entries
                       if item["status"] == "move"],
        "conflict_keys": [item["legacy"] for item in entries
                          if item["status"] == "conflict"],
        "unmapped_keys": [item["legacy"] for item in entries
                          if item["status"] == "unmapped"],
        "unknown_keys": [item["legacy"] for item in entries
                         if item["status"] == "unknown"],
        "moved_tables": [change.label for change in tables],
        "diff": diff,
        "notes": [
            "the legacy .ozagia.toml is never deleted or rewritten",
            "only values without a repository conflict are written",
            "project-model entries move whole; fields are never merged",
            "a project-model conflict leaves every project-model table "
            "unwritten",
            "unmapped and unknown keys stay in the legacy file",
        ],
    }
    if dry_run:
        return report
    if not planned and not tables:
        report["notes"].append("no values to write")
        return report
    if not isinstance(confirm, str) or not confirm.strip():
        raise MigrateError(
            "--apply needs --confirm TOKEN from a dry run; preview first")
    if confirm.strip() != token:
        raise MigrateError(
            "the migration preview is stale or the token is wrong; "
            "preview again")
    if _read_text(target_path, what="repository config") != target_text:
        raise MigrateError(
            "the migration preview is stale or the target changed; "
            "preview again")
    applied = False
    if changed:
        try:
            S._atomic_write(target_path, new_text)
        except (S.SettingsError, OSError) as exc:
            raise MigrateError(str(exc)) from exc
        applied = True
    report.update({
        "applied": applied,
        "changed": changed,
        "diff": diff,
        "written": written,
        "written_tables": [change.label for change in tables] if applied else [],
    })
    if not applied and changed:  # pragma: no cover - guarded
        report["notes"].append("the target changed; run migrate again")
    return report


def format_report(report: Mapping[str, Any]) -> str:
    """A short text rendering of :func:`migrate`; JSON carries every field."""
    counts = report.get("counts") or {}
    summary = ", ".join(f"{name}={value}" for name, value in counts.items())
    mode = "dry run" if report.get("dry_run") else (
        "applied" if report.get("applied") else "no changes")
    lines = [f"{mode}: {report.get('legacy')} -> {report.get('target')}"]
    if summary:
        lines.append(f"  {summary}")
    if not report.get("legacy_exists"):
        lines.append("  legacy config does not exist; nothing to migrate")
    tables = report.get("moved_tables") or []
    if tables:
        lines.append("  tables: " + ", ".join(tables))
    for item in report.get("entries") or []:
        status = item.get("status")
        if status not in {"conflict", "unmapped", "unknown", "invalid"}:
            continue
        line = f"  {status}: {item.get('legacy')}"
        if item.get("key"):
            line += f" -> {item['key']}"
        if item.get("message"):
            line += f": {item['message']}"
        if status == "conflict" and "current" in item:
            line += f" (current: {json.dumps(item['current'], default=str)})"
        lines.append(line)
    diff = report.get("diff") or ""
    if diff:
        lines.append(diff.rstrip("\n"))
    if report.get("dry_run") and report.get("token"):
        lines.append(
            f"  token={report['token']} (pass to --apply --confirm)")
    if report.get("applied"):
        lines.append(f"  wrote {report.get('target')}")
    return "\n".join(lines)


__all__ = [
    "LEGACY_FILENAME",
    "LEGACY_SECTIONS",
    "PROJECT_MODEL_SECTIONS",
    "PROJECT_TABLE_ORDER",
    "RUNTIME_SECTIONS",
    "SCHEMA",
    "MigrateError",
    "UNMAPPED_SECTIONS",
    "format_report",
    "migrate",
]
