"""Bounded map cache metadata under ``<root>/.ozagia/model_api/cache``.

The cache is an optimisation and an inspectable record, never an authority.
A cache key covers the map/schema, source signature, effective scope
(root/repo/checkout/snapshot/path scope/session/model), target, budgets, cursor
and host, so a model or session switch cannot reuse another partition's map.
A read additionally verifies the stored entry's scope, model, session, source,
schema fingerprint and instance hash; a mismatch is a miss, never a hit.

Only canonical JSON, map metadata and the bounded map page are stored.  No
prompt, raw log, model output or secret value enters an entry.  Writes are
atomic (temporary file in the cache directory, then ``os.replace``), limited
to the repository state directory, and pruned to a bounded number of entries.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ozagia.core import contracts as C
from ozagia.model_api import schema
from ozagia.model_api.errors import ModelAPIError

CACHE_NAMESPACE = "ozagia.model_api.cache"
CACHE_VERSION = "1.0"
MAX_ENTRY_BYTES = 512 * 1024
MAX_ENTRIES = 128
_STATE_DIR = ".ozagia"
_CACHE_PARTS = ("model_api", "cache")


def _fail(message: str, code: str, reason: str) -> ModelAPIError:
    return ModelAPIError(message, code=code, reason=reason)


def _is_hex_key(key: Any) -> bool:
    return (isinstance(key, str) and len(key) == 64
            and all(char in "0123456789abcdef" for char in key))


def cache_key(*, map_type: str, schema_fingerprint: str,
              source_signature: str, scope: Mapping[str, Any],
              target: Mapping[str, Any], budgets: Mapping[str, Any],
              cursor: str | None, policy_hash: str) -> str:
    """Return the SHA-256 partition key for one map page."""
    if map_type not in schema.MAP_TYPES:
        raise _fail(f"unknown map type {map_type!r}", "unsupported",
                    "unsupported_map")
    if not _is_hex_key(source_signature):
        raise _fail("source signature must be a SHA-256 hex digest",
                    "invalid_value", "invalid_internal_state")
    if not _is_hex_key(schema_fingerprint):
        raise _fail("schema fingerprint must be a SHA-256 hex digest",
                    "invalid_value", "invalid_internal_state")
    return C.content_hash({
        "namespace": CACHE_NAMESPACE,
        "version": CACHE_VERSION,
        "map": map_type,
        "schema": schema_fingerprint,
        "source": source_signature,
        "scope": {
            "root": scope.get("root"),
            "repo": scope.get("repo"),
            "checkout": scope.get("checkout"),
            "snapshot": scope.get("snapshot"),
            "session": scope.get("session"),
            "model": scope.get("model"),
            "paths": list(scope.get("paths") or []),
        },
        "target": dict(target),
        "budgets": dict(budgets),
        "cursor": cursor,
        "policy": policy_hash,
    })


class CacheStore:
    """A per-root, fail-closed cache of bounded map pages."""

    def __init__(self, root: str | Path, *, enabled: bool = True,
                 max_entries: int = MAX_ENTRIES) -> None:
        try:
            self.root = Path(root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise _fail("cache root does not exist", "invalid_request",
                        "policy_root_missing") from exc
        if not self.root.is_dir():
            raise _fail("cache root is not a directory", "invalid_request",
                        "policy_root_missing")
        self.enabled = bool(enabled)
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) \
                or max_entries < 1 or max_entries > 4096:
            raise _fail("max_entries is out of bounds", "invalid_request",
                        "malformed_request")
        self.max_entries = max_entries
        self.state = self.root / _STATE_DIR
        self.directory = self.state.joinpath(*_CACHE_PARTS)

    @property
    def directory_label(self) -> str:
        try:
            return self.directory.relative_to(self.root).as_posix()
        except ValueError:
            return "unavailable"

    def _check_path(self, path: Path, *, create: bool) -> Path:
        """Resolve and require a path under ``<root>/.ozagia``.

        A symlinked state or cache directory that escapes the repository is a
        refusal, not a place to write.  This is checked on every read/write
        because a symlink can be introduced between calls.
        """
        expected = self.state.resolve(strict=False)
        root = self.root.resolve(strict=True)
        if not expected.is_relative_to(root):
            raise _fail("state directory escapes the repository",
                        "forbidden", "cache_path_escape")
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(expected):
            raise _fail("cache path escapes the repository state directory",
                        "forbidden", "cache_path_escape")
        if create:
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise _fail("cache directory cannot be created",
                            "unavailable", "cache_path_escape") from exc
            resolved = resolved.resolve(strict=True)
            if not resolved.is_relative_to(expected):
                raise _fail("cache directory escapes after creation",
                            "forbidden", "cache_path_escape")
        return resolved

    def path_for(self, key: str) -> Path:
        if not _is_hex_key(key):
            raise _fail("cache key is not a SHA-256 hex digest",
                        "invalid_request", "malformed_request")
        return self.directory / f"{key}.json"

    def _relative_label(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return "unavailable"

    def read(self, key: str, *, map_type: str, schema_fingerprint: str,
             scope: Mapping[str, Any], source_signature: str,
             ) -> dict[str, Any] | None:
        """Return ``{"instance":..., "map_hash":...}`` or ``None``.

        Every mismatch is a miss.  A malformed or foreign entry is never
        returned as a hit, and a cache read never raises for bad content.
        """
        if not self.enabled:
            return None
        try:
            path = self._check_path(self.path_for(key), create=False)
        except ModelAPIError:
            raise
        if not path.is_file():
            return None
        try:
            size = path.stat().st_size
            if size <= 0 or size > MAX_ENTRY_BYTES:
                return None
            text = path.read_text(encoding="utf-8")
            entry = json.loads(text)
        except (OSError, ValueError, UnicodeError):
            return None
        if not isinstance(entry, Mapping):
            return None
        if entry.get("namespace") != CACHE_NAMESPACE \
                or entry.get("version") != CACHE_VERSION:
            return None
        if entry.get("key") != key or entry.get("map") != map_type:
            return None
        if entry.get("schema") != schema_fingerprint:
            return None
        if entry.get("source") != source_signature:
            return None
        if entry.get("scope") != dict(scope):
            return None
        if entry.get("model") != scope.get("model") \
                or entry.get("session") != scope.get("session"):
            return None
        instance = entry.get("instance")
        if not isinstance(instance, Mapping):
            return None
        try:
            normalized, digest = schema.validate_map(instance, expected=map_type)
        except (ModelAPIError, C.ContractError, ValueError):
            return None
        if entry.get("map_hash") != digest:
            return None
        instance_scope = normalized.get("scope") or {}
        for name in ("root", "repo", "checkout", "snapshot", "session", "model",
                     "paths"):
            if instance_scope.get(name) != scope.get(name):
                return None
        return {
            "instance": normalized,
            "map_hash": digest,
            "key": key,
            "path": self._relative_label(path),
        }

    def write(self, key: str, instance: Mapping[str, Any], *,
              map_type: str, scope: Mapping[str, Any], source_signature: str,
              schema_fingerprint: str) -> dict[str, Any]:
        """Atomically write one cache entry and return metadata.

        A write failure is reported to the caller as an unavailable cache; it
        must not replace or corrupt the map result.  Errors that indicate a
        path escape are refused before any create/write.
        """
        if not self.enabled:
            return {"state": "disabled", "path": None}
        path = self._check_path(self.path_for(key), create=False)
        if path.exists() and not path.is_file():
            return {"state": "unavailable", "path": self._relative_label(path)}
        directory = self._check_path(self.directory, create=True)
        entry = {
            "namespace": CACHE_NAMESPACE,
            "version": CACHE_VERSION,
            "key": key,
            "map": map_type,
            "schema": schema_fingerprint,
            "source": source_signature,
            "scope": dict(scope),
            "session": scope.get("session"),
            "model": scope.get("model"),
            "map_hash": C.instance_hash(instance),
            "instance": dict(instance),
        }
        try:
            encoded = C.canonical_json(entry).encode("utf-8")
        except C.ContractError:
            return {"state": "unavailable", "path": None}
        if len(encoded) > MAX_ENTRY_BYTES:
            return {"state": "unavailable", "path": None,
                    "reason": "entry_over_budget"}
        target = directory / f"{key}.json"
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".tmp-", suffix=".json",
                                             dir=str(directory))
            with os.fdopen(fd, "wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            os.chmod(target, 0o600)
        except OSError:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            return {"state": "unavailable", "path": None}
        self._prune()
        return {"state": "written",
                "path": self._relative_label(target)}

    def _prune(self) -> None:
        """Best-effort retention bound; never raises into a map response."""
        try:
            entries = sorted(
                (path for path in self.directory.glob("*.json")
                 if path.is_file()),
                key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return
        excess = len(entries) - self.max_entries
        if excess <= 0:
            return
        for path in entries[:excess]:
            try:
                path.unlink()
            except OSError:
                return


__all__ = [
    "CACHE_NAMESPACE", "CACHE_VERSION", "CacheStore", "MAX_ENTRIES",
    "MAX_ENTRY_BYTES", "cache_key",
]
