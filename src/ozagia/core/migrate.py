"""Migrate legacy ``.ozagia.toml`` settings into ``ozagia/config.toml``.

The legacy file is never deleted, rewritten or moved. Migration copies the
keys that the typed settings registry understands into the versioned
repository config, reports every key it did not copy, and refuses to replace
a value that already exists in ``ozagia/config.toml`` with a different one.

The command is preview-first: :func:`migrate` defaults to ``dry_run=True`` and
returns the exact unified diff plus a confirmation token. Applying requires
that token, so a stale preview cannot write over a file that changed after it
was shown. Writes are one atomic update; unrelated TOML and comments survive.

Unsupported legacy sections stay where they are. This module does not import
the target project and makes no network or model calls.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ozagia.config import CONFIG_FILENAME, repo_root
from ozagia.core import settings as S

SCHEMA = 1
#: The legacy project config filename at the repository root.
LEGACY_FILENAME = CONFIG_FILENAME


class MigrateError(ValueError):
    """The legacy or target config cannot be migrated."""


@dataclass(frozen=True)
class LegacyKey:
    """How one legacy key maps into the registry."""

    target: str | None = None
    reason: str = ""


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
    },
    "hints": {
        "library": LegacyKey(target="hints.library"),
        "max_per_edit": LegacyKey(target="hints.max_per_edit"),
        "record": LegacyKey(target="hints.record"),
        "ecosystems": LegacyKey(target="hints.ecosystems"),
    },
}

#: Legacy sections that are project models, not registry settings.
UNMAPPED_SECTIONS: dict[str, str] = {
    "environments": "environment profiles stay in .ozagia.toml",
    "services": "service definitions stay in .ozagia.toml",
    "relays": "relay definitions stay in .ozagia.toml",
    "runtime": "runtime services and tunnels stay in .ozagia.toml",
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path: Path, *, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
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
           message: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"legacy": legacy, "status": status}
    if key is not None:
        item["key"] = key
    if status in {"move", "same", "conflict"}:
        item["value"] = value
    if current is not None:
        item["current"] = current
    if message:
        item["message"] = message
    return item


def _known_sections() -> list[str]:
    seen = set(LEGACY_SECTIONS) | set(UNMAPPED_SECTIONS)
    for item in S.schema():
        seen.add(str(item["section"]).split(".", 1)[0])
    return sorted(seen)


def _plan(root: Path, legacy_path: Path, target_path: Path) -> tuple[
        list[dict[str, Any]], dict[str, Any], str, str]:
    """Return entries, values to write, legacy text and target text."""
    if legacy_path.is_dir():
        raise MigrateError(f"{legacy_path}: is a directory, not a TOML file")
    legacy_text = _read_text(legacy_path, what="legacy config")
    target_text = _read_text(target_path, what="repository config")
    if not legacy_text.strip():
        return [], {}, legacy_text, target_text
    data = _read_toml(legacy_path, legacy_text)
    try:
        existing = S.layer_values(root, scope="repo")
    except (S.SettingsError, ValueError, OSError) as exc:
        raise MigrateError(f"cannot read {target_path}: {exc}") from exc
    known_sections = _known_sections()
    entries: list[dict[str, Any]] = []
    planned: dict[str, Any] = {}

    for section, payload in data.items():
        label = f"[{section}]"
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
    return entries, planned, legacy_text, target_text


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
           target_text: str, planned: Mapping[str, Any]) -> str:
    payload = {
        "legacy": str(legacy_path),
        "legacy_sha256": _digest(legacy_text),
        "target": str(target_path),
        "target_sha256": _digest(target_text),
        "values": dict(planned),
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
    entries, planned, legacy_text, target_text = _plan(
        root_p, legacy_path, target_path)
    token = _token(legacy_path, legacy_text, target_path, target_text,
                   planned) if planned else None
    diff = ""
    if planned:
        try:
            preview = S.set_values(root_p, planned, scope="repo", dry_run=True)
        except (S.SettingsError, ValueError, OSError) as exc:
            raise MigrateError(str(exc)) from exc
        diff = preview.get("diff") or ""

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "root": str(root_p),
        "legacy": str(legacy_path),
        "legacy_exists": legacy_path.is_file(),
        "target": str(target_path),
        "target_exists": target_path.is_file(),
        "dry_run": dry_run,
        "ok": not any(item["status"] == "invalid" for item in entries),
        "changed": bool(planned),
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
        "diff": diff,
        "notes": [
            "the legacy .ozagia.toml is never deleted or rewritten",
            "only values without a repository conflict are written",
            "unmapped and unknown keys stay in the legacy file",
        ],
    }
    if dry_run:
        return report
    if not planned:
        report["notes"].append("no values to write")
        return report
    if not isinstance(confirm, str) or not confirm.strip():
        raise MigrateError(
            "--apply needs --confirm TOKEN from a dry run; preview first")
    if confirm.strip() != token:
        raise MigrateError(
            "the migration preview is stale or the token is wrong; "
            "preview again")
    try:
        result = S.set_values(root_p, planned, scope="repo", dry_run=False)
    except (S.SettingsError, ValueError, OSError) as exc:
        raise MigrateError(str(exc)) from exc
    report.update({
        "applied": bool(result.get("applied")),
        "changed": bool(result.get("changed")),
        "diff": result.get("diff") or "",
        "written": result.get("written") or {},
    })
    if not report["applied"] and report["changed"]:  # pragma: no cover - guarded
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
            f"  token: {report['token']} (pass to --apply --confirm)")
    if report.get("applied"):
        lines.append(f"  wrote {report.get('target')}")
    return "\n".join(lines)


__all__ = [
    "LEGACY_FILENAME",
    "LEGACY_SECTIONS",
    "SCHEMA",
    "MigrateError",
    "UNMAPPED_SECTIONS",
    "format_report",
    "migrate",
]
