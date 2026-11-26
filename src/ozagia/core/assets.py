"""Durable external assets: a metadata-only catalog over existing layouts.

An **asset** is a durable file or directory outside the temporary class: a
dataset, model, corpus, install, long-lived cache, tool tree or other reference
data.  Assets may be shared across repositories and may live outside every
checkout.  This module records where they are, who owns them, what they came
from, how large they are and whether they may ever be deleted; it does not copy,
download or read their contents.

Layout
------

``$XDG_DATA_HOME/ozagia/assets/registry/<id>.json``
    One atomic record per asset.  When ``XDG_DATA_HOME`` is unset or not an
    absolute path, ``~/.local/share`` is used.  If that location cannot be
    made writable, the registry falls back to ``<state>/assets/registry``.
    Registry directories are 0700 and files 0600.

``.ozagia-asset.json``
    An optional marker inside a directory.  It is only ever written for an
    ``owner=ozagia`` asset after an explicit ``mark=True``/``--mark``; an
    external or user-owned path is never marked.  A marker holds identity and
    provenance only.

Both are written atomically (same-directory ``mkstemp``, ``fsync``,
``os.replace``).  Records contain metadata only: no file contents, prompts,
credentials or environment.

Safety
------

Paths are canonicalised and must not be empty, a filesystem root, a symlink or
under the temp workspace.  External and home paths are allowed for assets.
Measurement reuses :func:`ozagia.core.temp_space.walk_usage`: it never follows
symlinks, never reads file contents, and reports a bounded walk as ``partial``.
Hashing only happens in :func:`verify` when the caller asks for it.

Garbage collection is a **dry run only** in this stage.  A candidate must be
``owner=ozagia``, ``deletable=True``, unpinned and in ``present`` or
``verified`` state.  Ocelot, Hugging Face, user, upstream and external assets
are never candidates.  ``apply=True`` is refused rather than deleting files.

The module uses the standard library only and never contacts the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ozagia.config import state_path
from ozagia.core import temp_space as TS

SCHEMA = "ozagia.asset/1"
MARKER_NAME = ".ozagia-asset.json"
REGISTRY_DIRNAME = "registry"

KINDS = ("dataset", "model", "corpus", "install", "cache", "tool", "other")
OWNERS = ("ozagia", "ocelot", "huggingface", "user", "upstream", "external")
SCOPES = ("user", "repo", "shared", "system")
SOURCE_KINDS = ("url", "huggingface", "git", "local", "manifest", "unknown")
STATES = ("present", "missing", "partial", "verified", "mismatch", "unverified")
LICENSE_STATUSES = ("unknown", "declared", "verified")

DEFAULT_KIND = "other"
DEFAULT_OWNER = "external"
DEFAULT_SCOPE = "user"

MAX_REASON = 500
MAX_TOKEN = 200
MAX_DIGESTS = 1_000
MAX_MARKER_BYTES = 64 * 1024
MAX_SCAN_LIMIT = 10_000

GC_OWNER = "ozagia"
GC_STATES = frozenset({"present", "verified"})
GC_KIND_ORDER = {"cache": 0, "dataset": 1, "model": 1}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class AssetError(Exception):
    """An asset request is invalid."""


class AssetPathError(AssetError):
    """A path is unsafe to catalog, measure or mark."""


class AssetNotFound(AssetError):
    """No registry record has the requested id."""


class AssetConflict(AssetError):
    """An explicit id already belongs to a different asset."""


class AssetMarkerError(AssetPathError):
    """A marker exists but does not identify this asset."""


class AssetGcRefused(AssetError):
    """Garbage collection was asked to do something this stage forbids."""


# --------------------------------------------------------------------------- #
# locations
# --------------------------------------------------------------------------- #


def _data_home() -> Path | None:
    """The user data home, or ``None`` when no absolute location is known."""
    value = os.environ.get("XDG_DATA_HOME")
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
    try:
        return Path.home() / ".local" / "share"
    except (OSError, RuntimeError):
        return None


def user_registry_dir() -> Path | None:
    """The preferred user-level registry directory, even if not writable."""
    home = _data_home()
    if home is None:
        return None
    return home / "ozagia" / "assets" / REGISTRY_DIRNAME


def registry_dir(root: str | Path) -> Path:
    """The writable registry directory for ``root``.

    The user-level location wins.  The state directory is only used when that
    location cannot be created or written, so a read-only home never breaks a
    repository-local catalog.
    """
    candidates: list[Path] = []
    preferred = user_registry_dir()
    if preferred is not None:
        candidates.append(preferred)
    candidates.append(state_path(root, "assets", REGISTRY_DIRNAME))
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            _ensure_dir(candidate)
        except OSError as exc:
            last_error = exc
            continue
        if _writable_dir(candidate):
            return candidate
    detail = f": {last_error}" if last_error is not None else ""
    raise AssetError(f"no writable asset registry location{detail}")


def _writable_dir(path: Path) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise AssetError(f"not a directory: {path}")
    os.chmod(path, 0o700)


def _record_path(root: str | Path, asset_id: str) -> Path:
    return registry_dir(root) / f"{asset_id}.json"


# --------------------------------------------------------------------------- #
# atomic JSON
# --------------------------------------------------------------------------- #


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------- #
# validation and normalisation
# --------------------------------------------------------------------------- #


def _text(value: Any, label: str, *, limit: int = MAX_TOKEN) -> str:
    if not isinstance(value, str):
        raise AssetError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise AssetError(f"{label} must not be empty")
    if len(text) > limit:
        raise AssetError(f"{label} must be at most {limit} characters")
    if _CONTROL_RE.search(text):
        raise AssetError(f"{label} must not contain control characters")
    return text


def _optional_text(value: Any, label: str,
                   *, limit: int = MAX_TOKEN) -> str | None:
    if value is None:
        return None
    text = _text(value, label, limit=limit)
    return text


def _reason(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AssetError("reason must be a string")
    text = value.strip()
    if len(text) > MAX_REASON:
        raise AssetError(f"reason must be at most {MAX_REASON} characters")
    if _CONTROL_RE.search(text):
        raise AssetError("reason must not contain control characters")
    return text


def _validate_kind(value: Any) -> str:
    text = _text(value, "kind")
    if text not in KINDS:
        raise AssetError(
            f"unknown kind {text!r}; expected one of {', '.join(KINDS)}")
    return text


def _validate_owner(value: Any) -> str:
    text = _text(value, "owner")
    if text not in OWNERS:
        raise AssetError(
            f"unknown owner {text!r}; expected one of {', '.join(OWNERS)}")
    return text


def _validate_scope(value: Any) -> str:
    text = _text(value, "scope")
    if text not in SCOPES:
        raise AssetError(
            f"unknown scope {text!r}; expected one of {', '.join(SCOPES)}")
    return text


def _validate_state(value: Any) -> str:
    text = _text(value, "state")
    if text not in STATES:
        raise AssetError(
            f"unknown state {text!r}; expected one of {', '.join(STATES)}")
    return text


def _clean_id(value: Any) -> str:
    text = _text(value, "id", limit=64)
    if ".." in text or not _ID_RE.match(text):
        raise AssetError(
            "id must be 1-64 characters of letters, digits, '.', '_' or '-', "
            "and must not start with a punctuation mark")
    return text


def _validate_sha256(value: Any) -> str:
    text = _text(value, "sha256", limit=64).lower()
    if not _SHA256_RE.match(text):
        raise AssetError("sha256 must be 64 hexadecimal characters")
    return text


def _normalise_source(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "unknown", "url": None, "dataset": None,
        "revision": None, "commit": None, "manifest": None,
    }
    if value is None:
        return result
    if isinstance(value, str):
        result.update({"kind": "url",
                       "url": _text(value, "source", limit=4096)})
        return result
    if not isinstance(value, Mapping):
        raise AssetError("source must be a string or an object")
    kind = value.get("kind")
    if kind is None:
        if value.get("dataset") is not None:
            kind = "huggingface"
        elif value.get("manifest") is not None:
            kind = "manifest"
        elif value.get("commit") is not None:
            kind = "git"
        elif value.get("url") is not None:
            kind = "url"
        else:
            kind = "unknown"
    kind = _text(kind, "source.kind")
    if kind not in SOURCE_KINDS:
        raise AssetError(
            f"unknown source kind {kind!r}; expected one of "
            + ", ".join(SOURCE_KINDS))
    result["kind"] = kind
    for key in ("url", "dataset", "revision", "commit", "manifest"):
        result[key] = _optional_text(value.get(key), f"source.{key}",
                                     limit=4096)
    return result


def _merge_source(existing: Any, value: Any) -> dict[str, Any]:
    base = _normalise_source(existing)
    if value is None:
        return base
    incoming = _normalise_source(value)
    if isinstance(value, Mapping) and value.get("kind") is None:
        # A supplied object with only some fields keeps the existing kind.
        incoming["kind"] = base["kind"]
    for key in ("kind", "url", "dataset", "revision", "commit", "manifest"):
        if incoming[key] is not None:
            base[key] = incoming[key]
    return base


def _normalise_license(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"spdx": None, "status": "unknown",
                              "source": None}
    if value is None:
        return result
    if isinstance(value, str):
        result.update({"spdx": _text(value, "license"),
                       "status": "declared", "source": "caller"})
        return result
    if not isinstance(value, Mapping):
        raise AssetError("license must be a string or an object")
    spdx = _optional_text(value.get("spdx"), "license.spdx")
    status = value.get("status")
    if status is None:
        status = "declared" if spdx else "unknown"
    status = _text(status, "license.status")
    if status not in LICENSE_STATUSES:
        raise AssetError(
            f"unknown license status {status!r}; expected one of "
            + ", ".join(LICENSE_STATUSES))
    result.update({"spdx": spdx, "status": status,
                   "source": _optional_text(value.get("source"),
                                            "license.source")})
    return result


def _normalise_digests(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise AssetError("digests must be a list")
    if len(value) > MAX_DIGESTS:
        raise AssetError(f"digests must have at most {MAX_DIGESTS} entries")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise AssetError(f"digests[{index}] must be an object")
        path = item.get("path")
        path = _text(path, f"digests[{index}].path")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise AssetError(
                f"digests[{index}].path must be a relative path inside the asset")
        sha = item.get("sha256")
        sha = _validate_sha256(sha) if sha is not None else None
        size = item.get("bytes")
        if size is None:
            size = 0
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AssetError(f"digests[{index}].bytes must be a non-negative integer")
        result.append({
            "path": path,
            "sha256": sha,
            "bytes": int(size),
            "verified": bool(item.get("verified", False)),
            "derived": bool(item.get("derived", False)),
        })
    return result


# --------------------------------------------------------------------------- #
# paths and measurement
# --------------------------------------------------------------------------- #


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _canonical(root: str | Path | None, path: str | Path) -> Path:
    if path is None:
        raise AssetPathError("path is required")
    text = str(path).strip()
    if not text:
        raise AssetPathError("path must not be empty")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise AssetPathError(f"cannot inspect {candidate}: {exc}") from exc
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise AssetPathError(f"refusing a symlink path: {candidate}")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AssetPathError(f"cannot resolve path: {candidate}") from exc
    if resolved.parent == resolved or resolved == Path(resolved.anchor):
        raise AssetPathError(f"refusing a filesystem root: {resolved}")
    if root is not None:
        root_path = Path(root).expanduser().resolve(strict=False)
        if resolved == root_path:
            raise AssetPathError("refusing the repository root as an asset")
        try:
            tmp_root = state_path(root_path, "tmp").resolve(strict=False)
        except OSError:
            tmp_root = None
        if tmp_root is not None and _is_within(resolved, tmp_root):
            raise AssetPathError(
                f"refusing an asset under the temp space: {resolved}")
        try:
            registry = registry_dir(root_path).resolve(strict=False)
        except (AssetError, OSError):
            registry = None
        if registry is not None and _is_within(resolved, registry):
            raise AssetPathError(
                f"refusing to catalog the asset registry itself: {resolved}")
    try:
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError):
        home = None
    if home is not None and resolved == home:
        raise AssetPathError("refusing the home directory itself as an asset")
    return resolved


def _probe(path: Path) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "error"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "present"


def _stored_target(root: str | Path, record: Mapping[str, Any]) -> Path:
    """A stored path that is safe to measure again, or a typed error."""
    text = record.get("path")
    if not isinstance(text, str) or not text:
        raise AssetPathError("asset record has no path")
    target = Path(text)
    if not target.is_absolute() or target.parent == target:
        raise AssetPathError(f"unsafe asset path in registry: {target}")
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise AssetPathError(f"cannot resolve asset path: {target}") from exc
    root_path = Path(root).expanduser().resolve(strict=False)
    if resolved == root_path:
        raise AssetPathError("refusing the repository root as an asset")
    try:
        tmp_root = state_path(root_path, "tmp").resolve(strict=False)
    except OSError:
        tmp_root = None
    if tmp_root is not None and _is_within(resolved, tmp_root):
        raise AssetPathError(
            f"refusing an asset under the temp space: {resolved}")
    try:
        registry = registry_dir(root_path).resolve(strict=False)
    except (AssetError, OSError):
        registry = None
    if registry is not None and _is_within(resolved, registry):
        raise AssetPathError(
            f"refusing to catalog the asset registry itself: {resolved}")
    try:
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError):
        home = None
    if home is not None and resolved == home:
        raise AssetPathError("refusing the home directory itself as an asset")
    return target


def _blank_measurement(allocation: str = "unknown") -> dict[str, Any]:
    return {
        "bytes": 0,
        "apparent_bytes": 0,
        "inodes": 0,
        "hardlinks": 0,
        "allocation": allocation,
        "walk": "complete",
        "cross_device": False,
    }


def _measure(path: Path) -> dict[str, Any]:
    usage = TS.walk_usage(path)
    return {
        "bytes": int(usage["allocated_bytes"]),
        "apparent_bytes": int(usage["apparent_bytes"]),
        "inodes": int(usage["inodes"]),
        "hardlinks": int(usage["hardlinks"]),
        "allocation": usage["allocation"],
        "walk": usage["walk"],
        "cross_device": bool(usage.get("cross_device")),
    }


def _state_for(probe: str, measurement: Mapping[str, Any] | None) -> str:
    if probe == "missing":
        return "missing"
    if probe == "symlink":
        return "mismatch"
    if probe == "error":
        return "partial"
    if measurement is None:
        return "present"
    if measurement.get("walk") == "partial" or measurement.get("cross_device"):
        return "partial"
    return "present"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #


def _blank_record(asset_id: str, path: Path, kind: str, owner: str,
                  scope: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": asset_id,
        "path": str(path),
        "kind": kind,
        "owner": owner,
        "scope": scope,
        "source": _normalise_source(None),
        "version": None,
        "revision": None,
        "sha256": None,
        "digests": [],
        "bytes": 0,
        "apparent_bytes": 0,
        "inodes": 0,
        "hardlinks": 0,
        "allocation": "unknown",
        "walk": "complete",
        "cross_device": False,
        "license": _normalise_license(None),
        "created_at": None,
        "verified_at": None,
        "last_used_at": None,
        "pinned": False,
        "deletable": False,
        "state": "unverified",
        "reason": "",
        "manifest_path": None,
        "marker": None,
    }


def _normalise_record(record: Mapping[str, Any]) -> dict[str, Any]:
    entry = _blank_record(
        str(record.get("id") or ""),
        Path(str(record.get("path") or "")),
        str(record.get("kind") or DEFAULT_KIND),
        str(record.get("owner") or DEFAULT_OWNER),
        str(record.get("scope") or DEFAULT_SCOPE),
    )
    entry.update(record)
    entry["schema"] = SCHEMA
    entry["source"] = _normalise_source(record.get("source"))
    entry["license"] = _normalise_license(record.get("license"))
    entry["digests"] = _normalise_digests(record.get("digests"))
    for key in ("bytes", "apparent_bytes", "inodes", "hardlinks"):
        value = record.get(key)
        entry[key] = int(value) if isinstance(value, int) and not isinstance(
            value, bool) and value >= 0 else 0
    for key in ("pinned", "deletable", "cross_device"):
        entry[key] = bool(record.get(key, False))
    for key in ("created_at", "verified_at", "last_used_at"):
        value = record.get(key)
        entry[key] = int(value) if isinstance(value, int) and not isinstance(
            value, bool) else None
    state = record.get("state")
    entry["state"] = state if state in STATES else "unverified"
    allocation = record.get("allocation")
    entry["allocation"] = (allocation if allocation in
                           ("blocks", "apparent-fallback", "unknown")
                           else "unknown")
    entry["walk"] = "partial" if record.get("walk") == "partial" else "complete"
    entry["reason"] = str(record.get("reason") or "")
    entry["marker"] = (str(record["marker"])
                       if record.get("marker") is not None else None)
    entry["manifest_path"] = (str(record["manifest_path"])
                              if record.get("manifest_path") is not None
                              else None)
    return entry


def _coerce_record(record: dict[str, Any]) -> dict[str, Any]:
    """Fill optional fields without trusting a single malformed value."""
    try:
        return _normalise_record(record)
    except (AssetError, TypeError, ValueError):
        base = _blank_record(
            str(record.get("id") or ""),
            Path(str(record.get("path") or "")),
            str(record.get("kind") or DEFAULT_KIND),
            str(record.get("owner") or DEFAULT_OWNER),
            str(record.get("scope") or DEFAULT_SCOPE),
        )
        for key in ("bytes", "apparent_bytes", "inodes", "hardlinks"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) \
                    and value >= 0:
                base[key] = value
        for key in ("pinned", "deletable", "cross_device"):
            base[key] = bool(record.get(key, False))
        for key in ("created_at", "verified_at", "last_used_at"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                base[key] = value
        state = record.get("state")
        base["state"] = state if state in STATES else "unverified"
        base["reason"] = str(record.get("reason") or "")
        return base


def _valid_record(record: Any, filename_id: str) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("schema") != SCHEMA:
        return False
    if record.get("id") != filename_id:
        return False
    try:
        _clean_id(record.get("id"))
        _validate_kind(record.get("kind"))
        _validate_owner(record.get("owner"))
        _validate_scope(record.get("scope"))
        _validate_state(record.get("state"))
    except AssetError:
        return False
    path = record.get("path")
    if not isinstance(path, str) or not path:
        return False
    candidate = Path(path)
    return candidate.is_absolute() and candidate.parent != candidate


def _load_records(root: str | Path, *,
                  errors: list[str] | None = None) -> list[dict[str, Any]]:
    directory = registry_dir(root)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    except OSError as exc:
        if errors is not None:
            errors.append(f"registry unreadable: {exc}")
        return []
    records: list[dict[str, Any]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        filename_id = name[:-5]
        path = directory / name
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            if errors is not None:
                errors.append(f"registry record {name}: {exc}")
            continue
        if not _valid_record(record, filename_id):
            if errors is not None:
                errors.append(f"registry record {name}: invalid asset record")
            continue
        records.append(_coerce_record(record))
    return records


def _read_record(root: str | Path, asset_id: str) -> dict[str, Any]:
    try:
        asset_id = _clean_id(asset_id)
    except AssetError as exc:
        raise AssetNotFound(f"unknown asset {asset_id!r}") from exc
    path = _record_path(root, asset_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError as exc:
        raise AssetNotFound(f"unknown asset {asset_id!r}") from exc
    except (OSError, ValueError) as exc:
        raise AssetError(f"unreadable asset record {asset_id}: {exc}") from exc
    if not _valid_record(record, asset_id):
        raise AssetError(f"invalid asset record {asset_id}")
    return _coerce_record(record)


def _find_identity(root: str | Path, path: Path, owner: str,
                   kind: str) -> dict[str, Any] | None:
    for record in _load_records(root):
        if (record.get("path") == str(path)
                and record.get("owner") == owner
                and record.get("kind") == kind):
            return record
    return None


def _stable_id(path: Path, owner: str, kind: str) -> str:
    material = f"{path}\x00{owner}\x00{kind}".encode("utf-8")
    return "ast_" + hashlib.sha256(material).hexdigest()[:12]


def _choose_id(root: str | Path, path: Path, owner: str, kind: str,
               explicit: str | None, existing: Mapping[str, Any] | None
               ) -> str:
    if explicit is not None:
        asset_id = _clean_id(explicit)
        if existing is not None and existing.get("id") != asset_id:
            raise AssetConflict(
                f"asset {existing.get('id')} already tracks this path; "
                f"refusing explicit id {asset_id}")
        if existing is None:
            try:
                clash = _read_record(root, asset_id)
            except AssetNotFound:
                clash = None
            if clash is not None:
                raise AssetConflict(
                    f"asset id {asset_id} already belongs to {clash['path']}")
        return asset_id
    if existing is not None:
        return str(existing["id"])
    candidate = _stable_id(path, owner, kind)
    for attempt in range(32):
        try:
            clash = _read_record(root, candidate)
        except AssetNotFound:
            return candidate
        if (clash.get("path") == str(path)
                and clash.get("owner") == owner
                and clash.get("kind") == kind):
            return candidate
        candidate = "ast_" + secrets.token_hex(6)
    raise AssetError("could not allocate a unique asset id")


def _view(record: Mapping[str, Any]) -> dict[str, Any]:
    entry = _normalise_record(record)
    probe = _probe(Path(entry["path"]))
    if probe == "missing":
        entry["state"] = "missing"
        entry.update(_blank_measurement())
    elif probe == "symlink":
        entry["state"] = "mismatch"
        entry.update(_blank_measurement())
    elif probe == "error":
        entry["state"] = "partial"
    elif entry.get("state") == "missing":
        # The path returned, but only a new verify can re-measure it.
        entry["state"] = "present"
    return entry


def _marker_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": record["id"],
        "path": record["path"],
        "kind": record["kind"],
        "owner": record["owner"],
        "scope": record["scope"],
        "created_at": record.get("created_at"),
        "reason": record.get("reason", ""),
        "version": record.get("version"),
        "pinned": bool(record.get("pinned")),
        "deletable": bool(record.get("deletable")),
    }


def _write_marker(path: Path, record: Mapping[str, Any]) -> Path:
    marker = path / MARKER_NAME
    if os.path.lexists(marker):
        try:
            info = os.lstat(marker)
        except OSError as exc:
            raise AssetMarkerError(f"cannot inspect existing marker: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise AssetMarkerError(f"refusing a symlink marker: {marker}")
        try:
            with open(marker, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, ValueError) as exc:
            raise AssetMarkerError(
                f"refusing to overwrite an unreadable marker: {marker}") from exc
        if not isinstance(existing, dict) or existing.get("id") != record["id"]:
            raise AssetMarkerError(
                f"refusing to overwrite an existing marker in {path}")
    _write_json(marker, _marker_payload(record))
    return marker


# --------------------------------------------------------------------------- #
# register / adopt
# --------------------------------------------------------------------------- #


def register(root: str | Path, path: str | Path, *, kind: str,
             owner: str, scope: str = DEFAULT_SCOPE, source: Any = None,
             version: str | None = None, revision: str | None = None,
             sha256: str | None = None, digests: Any = None,
             license: Any = None, pinned: bool = False,
             deletable: bool = False, mark: bool = False,
             id: str | None = None, reason: str = "",
             manifest_path: str | None = None) -> dict[str, Any]:
    """Record an existing (or currently missing) durable asset.

    ``bytes`` is allocated space, ``apparent_bytes`` is ``st_size``.  A path
    that is currently absent is recorded as ``state="missing"`` rather than as
    a silent zero.  ``mark`` writes ``.ozagia-asset.json`` only when the owner
    is ``ozagia``; external paths are never marked.
    """
    kind = _validate_kind(kind)
    owner = _validate_owner(owner)
    scope = _validate_scope(scope)
    reason = _reason(reason)
    version = _optional_text(version, "version")
    revision = _optional_text(revision, "revision")
    if sha256 is not None:
        sha256 = _validate_sha256(sha256)
    digests = _normalise_digests(digests)
    manifest_path = _optional_text(manifest_path, "manifest_path",
                                   limit=4096)
    target = _canonical(root, path)
    existing = _find_identity(root, target, owner, kind)
    asset_id = _choose_id(root, target, owner, kind, id, existing)

    now = int(time.time())
    base = _blank_record(asset_id, target, kind, owner, scope)
    if existing is not None:
        base.update(_coerce_record(existing))
        base["id"] = asset_id
        base["path"] = str(target)
        base["kind"] = kind
        base["owner"] = owner
        base["scope"] = scope
        if not reason:
            reason = str(existing.get("reason") or "")
        if version is None:
            version = existing.get("version")
        if revision is None:
            revision = existing.get("revision")
        if sha256 is None:
            sha256 = existing.get("sha256")
        if not digests:
            digests = _normalise_digests(existing.get("digests"))
        source_value = _merge_source(existing.get("source"), source)
        license_value = (_normalise_license(existing.get("license"))
                         if license is None else _normalise_license(license))
        if manifest_path is None:
            manifest_path = existing.get("manifest_path")
        verified_at = existing.get("verified_at")
        if sha256 != existing.get("sha256"):
            verified_at = None
    else:
        source_value = _normalise_source(source)
        license_value = _normalise_license(license)
        verified_at = None
    if source_value["revision"] is None and revision is not None:
        source_value["revision"] = revision
    if source_value["manifest"] is None and manifest_path is not None:
        source_value["manifest"] = manifest_path

    probe = _probe(target)
    if probe == "present":
        try:
            info = os.lstat(target)
        except OSError as exc:
            raise AssetPathError(f"cannot inspect {target}: {exc}") from exc
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise AssetPathError(
                "asset path must be a regular file or directory")
        measurement = _measure(target)
    else:
        measurement = _blank_measurement()
    state = _state_for(probe, measurement)
    if state == "present" and (sha256 or digests):
        state = "unverified"

    base.update({
        "schema": SCHEMA,
        "id": asset_id,
        "path": str(target),
        "kind": kind,
        "owner": owner,
        "scope": scope,
        "source": source_value,
        "version": version,
        "revision": revision,
        "sha256": sha256,
        "digests": digests,
        "license": license_value,
        "created_at": (existing.get("created_at") if existing is not None
                       and isinstance(existing.get("created_at"), int)
                       else now),
        "verified_at": verified_at if isinstance(verified_at, int) else None,
        "last_used_at": now,
        "pinned": bool(pinned),
        "deletable": bool(deletable),
        "state": state,
        "reason": reason,
        "manifest_path": manifest_path,
        "marker": None,
    })
    base.update(measurement)
    if state == "mismatch":
        base["reason"] = reason or "path is now a symlink"

    if mark:
        if owner != "ozagia":
            raise AssetMarkerError("a marker is only written for owner=ozagia")
        if probe != "present":
            raise AssetMarkerError("a marker needs an existing directory")
        try:
            info = os.lstat(target)
        except OSError as exc:
            raise AssetMarkerError(f"cannot inspect {target}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise AssetMarkerError("a marker needs a directory, not a file")
        marker_path = _write_marker(target, base)
        base["marker"] = str(marker_path)

    _write_json(_record_path(root, asset_id), base)
    return _normalise_record(base)


def adopt(root: str | Path, path: str | Path, *, kind: str = DEFAULT_KIND,
          owner: str = "ozagia", **kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`register` with ``owner`` defaulting to ``ozagia``.

    ``mark`` remains explicit and is still refused for any other owner.
    """
    kwargs.setdefault("scope", DEFAULT_SCOPE)
    return register(root, path, kind=kind, owner=owner, **kwargs)


# --------------------------------------------------------------------------- #
# read views
# --------------------------------------------------------------------------- #


def list_assets(root: str | Path, *, owner: str | None = None,
                kind: str | None = None, state: str | None = None,
                pinned: bool | None = None) -> list[dict[str, Any]]:
    """Assets with their current existence state (no hashing or re-walk)."""
    if owner is not None:
        owner = _validate_owner(owner)
    if kind is not None:
        kind = _validate_kind(kind)
    if state is not None:
        state = _validate_state(state)
    result: list[dict[str, Any]] = []
    for record in _load_records(root):
        entry = _view(record)
        if owner is not None and entry["owner"] != owner:
            continue
        if kind is not None and entry["kind"] != kind:
            continue
        if state is not None and entry["state"] != state:
            continue
        if pinned is not None and bool(entry["pinned"]) is not pinned:
            continue
        result.append(entry)
    result.sort(key=lambda item: (str(item.get("id")), item["path"]))
    return result


def get(root: str | Path, id: str) -> dict[str, Any]:
    """One asset record, with its current existence state."""
    return _view(_read_record(root, id))


def _empty_bucket() -> dict[str, int]:
    return {"entries": 0, "bytes": 0, "apparent_bytes": 0, "inodes": 0,
            "pinned": 0, "deletable": 0}


def status(root: str | Path, *, top: int | None = None) -> dict[str, Any]:
    """Totals by kind, owner and state, plus optional largest entries.

    This never hashes and never re-walks an asset; it uses the recorded
    measurement and refreshes only whether the path still exists.
    """
    if top is not None and (not isinstance(top, int) or isinstance(top, bool)
                            or top < 0):
        raise AssetError("top must be a non-negative integer")
    errors: list[str] = []
    entries = [_view(record) for record in _load_records(root, errors=errors)]
    totals = {
        "entries": len(entries), "bytes": 0, "apparent_bytes": 0,
        "inodes": 0, "hardlinks": 0, "pinned": 0, "deletable": 0,
        "present": 0, "missing": 0, "partial": 0, "mismatch": 0,
        "verified": 0, "unverified": 0, "cross_device": 0,
    }
    by_kind: dict[str, dict[str, int]] = {}
    by_owner: dict[str, dict[str, int]] = {}
    by_state: dict[str, dict[str, int]] = {}
    for entry in entries:
        buckets = (
            (by_kind, str(entry["kind"])),
            (by_owner, str(entry["owner"])),
            (by_state, str(entry["state"])),
        )
        for mapping, key in buckets:
            row = mapping.setdefault(key, _empty_bucket())
            row["entries"] += 1
            row["bytes"] += int(entry["bytes"])
            row["apparent_bytes"] += int(entry["apparent_bytes"])
            row["inodes"] += int(entry["inodes"])
            row["pinned"] += 1 if entry["pinned"] else 0
            row["deletable"] += 1 if entry["deletable"] else 0
        totals["bytes"] += int(entry["bytes"])
        totals["apparent_bytes"] += int(entry["apparent_bytes"])
        totals["inodes"] += int(entry["inodes"])
        totals["hardlinks"] += int(entry["hardlinks"])
        totals["pinned"] += 1 if entry["pinned"] else 0
        totals["deletable"] += 1 if entry["deletable"] else 0
        totals["cross_device"] += 1 if entry["cross_device"] else 0
        state = entry["state"]
        if state in STATES:
            totals[state] += 1
    result: dict[str, Any] = {
        "totals": totals,
        "by_kind": by_kind,
        "by_owner": by_owner,
        "by_state": by_state,
        "registry": str(registry_dir(root)),
        "registry_errors": errors,
    }
    if top is not None:
        ordered = sorted(entries, key=lambda item: (
            -int(item["bytes"]), int(item.get("created_at") or 0),
            str(item["id"])))
        result["top"] = ordered[:top]
    return result


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


def _digest_target(base: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AssetError(f"digest path escapes the asset: {relative}")
    target = (base / candidate).resolve(strict=False)
    if not _is_within(target, base):
        raise AssetError(f"digest path escapes the asset: {relative}")
    return target


def _verify_digests(base: Path, digests: list[dict[str, Any]],
                    *, hash_files: bool) -> tuple[bool, list[str], bool]:
    """Return ``(all_match, notes, checked)`` for declared digests."""
    notes: list[str] = []
    checked = False
    all_match = True
    for digest in digests:
        relative = digest["path"]
        try:
            target = _digest_target(base, relative)
        except AssetError as exc:
            notes.append(str(exc))
            digest["verified"] = False
            all_match = False
            continue
        probe = _probe(target)
        if probe != "present":
            notes.append(f"digest path is {probe}: {relative}")
            digest["verified"] = False
            all_match = False
            continue
        try:
            raw_info = os.lstat(base / Path(relative))
        except OSError as exc:
            notes.append(f"cannot inspect {relative}: {exc}")
            digest["verified"] = False
            all_match = False
            continue
        if stat.S_ISLNK(raw_info.st_mode):
            notes.append(f"refusing a symlink digest path: {relative}")
            digest["verified"] = False
            all_match = False
            continue
        try:
            info = os.lstat(target)
        except OSError as exc:
            notes.append(f"cannot inspect {relative}: {exc}")
            digest["verified"] = False
            all_match = False
            continue
        if not stat.S_ISREG(info.st_mode):
            notes.append(f"digest path is not a regular file: {relative}")
            digest["verified"] = False
            all_match = False
            continue
        digest["bytes"] = int(info.st_size)
        expected = digest.get("sha256")
        if not hash_files:
            if expected:
                notes.append(f"hash not checked: {relative}")
                digest["verified"] = False
            continue
        checked = True
        try:
            actual = _file_sha256(target)
        except OSError as exc:
            notes.append(f"cannot hash {relative}: {exc}")
            digest["verified"] = False
            all_match = False
            continue
        if expected is None:
            digest["sha256"] = actual
            digest["verified"] = True
        elif actual == expected:
            digest["verified"] = True
        else:
            digest["verified"] = False
            all_match = False
            notes.append(f"sha256 mismatch: {relative}")
    return all_match, notes, checked


def verify(root: str | Path, id: str, *,
           hash_files: bool = False) -> dict[str, Any]:
    """Re-check one asset's existence, size and optional digests.

    No network is used.  File contents are read only when ``hash_files`` is
    true.  The updated state and measurement are written back to the registry.
    """
    record = _read_record(root, id)
    target = _stored_target(root, record)
    probe = _probe(target)
    if probe == "missing":
        record.update(_blank_measurement())
        record.update({"state": "missing", "walk": "complete",
                       "reason": "path does not exist"})
        _write_json(_record_path(root, record["id"]), record)
        return _normalise_record(record)
    if probe == "symlink":
        record.update(_blank_measurement())
        record.update({"state": "mismatch",
                       "reason": "path is now a symlink"})
        _write_json(_record_path(root, record["id"]), record)
        return _normalise_record(record)
    if probe == "error":
        record.update({"state": "partial",
                       "reason": "path cannot be inspected"})
        _write_json(_record_path(root, record["id"]), record)
        return _normalise_record(record)

    measurement = _measure(target)
    record.update(measurement)
    notes: list[str] = []
    checked = False
    mismatch = False
    try:
        info = os.lstat(target)
    except OSError:
        info = None
    is_regular = info is not None and stat.S_ISREG(info.st_mode)
    is_directory = info is not None and stat.S_ISDIR(info.st_mode)
    special = info is not None and not (is_regular or is_directory)

    if hash_files and is_regular:
        checked = True
        try:
            actual = _file_sha256(target)
        except OSError as exc:
            notes.append(f"cannot hash: {exc}")
            mismatch = True
            actual = None
        if actual is not None:
            expected = record.get("sha256")
            if expected is None:
                record["sha256"] = actual
                notes.append("sha256 recorded")
            elif actual == expected:
                notes.append("sha256 matches")
            else:
                mismatch = True
                notes.append("sha256 mismatch")
    elif is_directory:
        digests = _normalise_digests(record.get("digests"))
        if digests:
            all_match, digest_notes, digest_checked = _verify_digests(
                target, digests, hash_files=hash_files)
            notes.extend(digest_notes)
            record["digests"] = digests
            checked = checked or digest_checked
            if not all_match:
                mismatch = True
        elif record.get("sha256"):
            notes.append("sha256 is declared for a directory; no tree hash")
    elif record.get("digests"):
        notes.append("declared digests belong to a directory")

    partial = measurement.get("walk") == "partial" or bool(
        measurement.get("cross_device"))
    if mismatch or special:
        state = "mismatch"
        if special:
            notes.append("path is not a regular file or directory")
    elif partial:
        state = "partial"
    elif checked:
        state = "verified"
    elif record.get("sha256") or record.get("digests"):
        state = "unverified"
    else:
        state = "present"
    record["state"] = state
    if notes:
        record["reason"] = "; ".join(notes)
    elif state in ("present", "partial"):
        record["reason"] = "measurement complete"
    elif state == "verified":
        record["reason"] = "hash verified"
    elif state == "unverified":
        record["reason"] = "hash declared; not checked"
    else:
        record["reason"] = ""
    if state == "verified":
        record["verified_at"] = int(time.time())
    _write_json(_record_path(root, record["id"]), record)
    return _normalise_record(record)


def verify_all(root: str | Path, *, hash_files: bool = False
               ) -> list[dict[str, Any]]:
    """Verify every record, returning one result per asset."""
    results: list[dict[str, Any]] = []
    for record in _load_records(root):
        try:
            results.append(verify(root, record["id"], hash_files=hash_files))
        except AssetError as exc:
            entry = _normalise_record(record)
            entry["state"] = "unverified"
            entry["reason"] = f"verify failed: {exc}"
            results.append(entry)
    return results


# --------------------------------------------------------------------------- #
# pinning and forgetting
# --------------------------------------------------------------------------- #


def _update(root: str | Path, id: str, **changes: Any) -> dict[str, Any]:
    record = _read_record(root, id)
    record.update(changes)
    _write_json(_record_path(root, record["id"]), record)
    return _normalise_record(record)


def pin(root: str | Path, id: str) -> dict[str, Any]:
    """Pin an asset: it is never a GC candidate."""
    return _view(_update(root, id, pinned=True))


def unpin(root: str | Path, id: str) -> dict[str, Any]:
    """Clear the pin; deletability is a separate, explicit property."""
    return _view(_update(root, id, pinned=False))


def forget(root: str | Path, id: str) -> dict[str, Any]:
    """Remove only the registry record; assets and markers are untouched."""
    record = _read_record(root, id)
    path = _record_path(root, record["id"])
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise AssetNotFound(f"unknown asset {record['id']!r}") from exc
    except OSError as exc:
        raise AssetError(f"cannot remove asset record: {exc}") from exc
    _fsync_dir(path.parent)
    return _view(record)


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MARKER_BYTES:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("schema") != SCHEMA:
        return None
    try:
        _clean_id(marker.get("id"))
        _validate_kind(marker.get("kind", DEFAULT_KIND))
        _validate_owner(marker.get("owner", "ozagia"))
    except AssetError:
        return None
    return marker


def _marker_asset(root: str | Path, marker_path: Path,
                  marker: Mapping[str, Any]) -> dict[str, Any]:
    directory = marker_path.parent.resolve(strict=False)
    declared = marker.get("path")
    if declared is not None:
        try:
            declared_path = Path(str(declared)).expanduser()
            if not declared_path.is_absolute():
                declared_path = directory / declared_path
            declared_path = declared_path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise AssetMarkerError(f"invalid marker path: {exc}") from exc
        if declared_path != directory:
            raise AssetMarkerError(
                f"marker path does not match its directory: {declared}")
    owner = marker.get("owner", "ozagia")
    if owner != "ozagia":
        raise AssetMarkerError("only ozagia-owned markers are scanned")
    record = register(
        root, directory, kind=marker.get("kind", DEFAULT_KIND), owner=owner,
        scope=marker.get("scope", DEFAULT_SCOPE),
        version=marker.get("version"),
        pinned=bool(marker.get("pinned", False)),
        deletable=bool(marker.get("deletable", False)),
        id=marker["id"], reason=str(marker.get("reason") or ""),
        mark=False,
    )
    if record.get("marker") != str(marker_path):
        record["marker"] = str(marker_path)
        _write_json(_record_path(root, record["id"]), record)
    return _normalise_record(record)


def scan(root: str | Path, *, paths: Sequence[str | Path] | None = None,
         limit: int = MAX_SCAN_LIMIT) -> dict[str, Any]:
    """Register explicit paths or ``.ozagia-asset.json`` markers only.

    This stage never guesses a known root and never walks ``$HOME`` or an
    arbitrary tree.  An explicit path is inspected for a marker in that same
    directory (or is itself a marker file); without one it is reported skipped.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise AssetError("limit must be a non-negative integer")
    limit = min(limit, MAX_SCAN_LIMIT)
    if isinstance(paths, (str, Path)):
        explicit = [paths]
    else:
        explicit = list(paths or ())
    found = 0
    examined = 0
    registered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[Path] = set()
    try:
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError):
        home = None
    for raw in explicit:
        if examined >= limit:
            skipped.append({"path": str(raw),
                            "reason": "scan limit reached"})
            continue
        examined += 1
        try:
            candidate = Path(str(raw)).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            errors.append(f"cannot resolve {raw}: {exc}")
            continue
        if home is not None and resolved == home:
            skipped.append({"path": str(resolved),
                            "reason": "refusing to scan the home directory"})
            continue
        try:
            _canonical(root, resolved)
        except AssetError as exc:
            skipped.append({"path": str(resolved), "reason": str(exc)})
            continue
        probe = _probe(resolved)
        if probe == "missing":
            skipped.append({"path": str(resolved),
                            "reason": "path does not exist"})
            continue
        if probe == "symlink":
            skipped.append({"path": str(resolved),
                            "reason": "refusing a symlink"})
            continue
        marker_path: Path | None = None
        if resolved.is_file() and resolved.name == MARKER_NAME:
            marker_path = resolved
        elif resolved.is_dir():
            marker_path = resolved / MARKER_NAME
            try:
                info = os.lstat(marker_path)
            except FileNotFoundError:
                info = None
            except OSError as exc:
                errors.append(f"cannot inspect marker in {resolved}: {exc}")
                continue
            if info is None:
                skipped.append({"path": str(resolved),
                                "reason": "no .ozagia-asset.json marker"})
                continue
        else:
            skipped.append({"path": str(resolved),
                            "reason": "explicit file has no marker"})
            continue
        if marker_path in seen:
            skipped.append({"path": str(marker_path),
                            "reason": "duplicate marker"})
            continue
        seen.add(marker_path)
        marker = _read_marker(marker_path)
        if marker is None:
            skipped.append({"path": str(marker_path),
                            "reason": "invalid marker"})
            continue
        found += 1
        try:
            record = _marker_asset(root, marker_path, marker)
        except AssetError as exc:
            errors.append(f"{marker_path}: {exc}")
            continue
        registered.append(record)
    return {
        "paths": len(explicit),
        "found": found,
        "registered": registered,
        "skipped": skipped,
        "errors": errors,
        "limit": limit,
    }


# --------------------------------------------------------------------------- #
# garbage-collection planning (dry run only)
# --------------------------------------------------------------------------- #


def _gc_keep_reason(entry: Mapping[str, Any], *, older_than_s: int | None,
                    now: int) -> str | None:
    if entry.get("owner") != GC_OWNER:
        return f"owner is {entry.get('owner')}; never a candidate"
    if entry.get("pinned"):
        return "pinned"
    if not entry.get("deletable"):
        return "not deletable"
    state = entry.get("state")
    if state not in GC_STATES:
        return f"state is {state}"
    if older_than_s is not None:
        used = entry.get("last_used_at") or entry.get("created_at") or 0
        if now - int(used) < older_than_s:
            return "not old enough"
    return None


def _gc_sort_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    kind = str(entry.get("kind"))
    rank = GC_KIND_ORDER.get(kind, 2)
    used = entry.get("last_used_at")
    created = entry.get("created_at")
    return (rank, int(used) if isinstance(used, int) else 0,
            int(created) if isinstance(created, int) else 0,
            str(entry.get("id")))


def gc_plan(root: str | Path, *, max_bytes: int | None = None,
            max_inodes: int | None = None,
            older_than_s: int | None = None) -> dict[str, Any]:
    """Plan a safe deletion order without deleting anything.

    Only ``owner=ozagia``, ``deletable=True``, unpinned assets in ``present``
    or ``verified`` state can be candidates.  The order prefers ``cache``,
    then ``dataset``/``model``, then other kinds, oldest ``last_used_at`` first.
    """
    for name, value in (("max_bytes", max_bytes), ("max_inodes", max_inodes),
                        ("older_than_s", older_than_s)):
        if value is not None and (not isinstance(value, int)
                                  or isinstance(value, bool) or value < 0):
            raise AssetError(f"{name} must be a non-negative integer")
    errors: list[str] = []
    entries = [_view(record) for record in _load_records(root, errors=errors)]
    now = int(time.time())
    candidates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for entry in entries:
        reason = _gc_keep_reason(entry, older_than_s=older_than_s, now=now)
        row = {
            "id": entry["id"],
            "path": entry["path"],
            "kind": entry["kind"],
            "owner": entry["owner"],
            "state": entry["state"],
            "bytes": entry["bytes"],
            "inodes": entry["inodes"],
        }
        if reason is not None:
            kept.append({**row, "why": reason})
        else:
            candidates.append(entry)
    candidates.sort(key=_gc_sort_key)
    selected: list[dict[str, Any]] = []
    selected_bytes = 0
    selected_inodes = 0
    budget_hit: str | None = None
    for index, entry in enumerate(candidates):
        size = int(entry["bytes"])
        inodes = int(entry["inodes"])
        if budget_hit is None and max_bytes is not None \
                and selected_bytes + size > max_bytes:
            budget_hit = "max_bytes"
        if budget_hit is None and max_inodes is not None \
                and selected_inodes + inodes > max_inodes:
            budget_hit = "max_inodes"
        if budget_hit is not None:
            kept.append({
                "id": entry["id"], "path": entry["path"],
                "kind": entry["kind"], "owner": entry["owner"],
                "state": entry["state"], "bytes": size,
                "inodes": inodes, "why": f"budget: {budget_hit}",
            })
            continue
        selected.append(entry)
        selected_bytes += size
        selected_inodes += inodes
    totals = {
        "entries": len(entries),
        "candidates": len(candidates),
        "selected": len(selected),
        "selected_bytes": selected_bytes,
        "selected_inodes": selected_inodes,
        "candidate_bytes": sum(int(entry["bytes"]) for entry in candidates),
        "candidate_inodes": sum(int(entry["inodes"]) for entry in candidates),
        "max_bytes": max_bytes,
        "max_inodes": max_inodes,
        "older_than_s": older_than_s,
    }
    return {
        "dry_run": True,
        "apply": False,
        "candidates": selected,
        "kept": kept,
        "totals": totals,
        "registry_errors": errors,
    }


def gc(root: str | Path, *, apply: bool = False, max_bytes: int | None = None,
       max_inodes: int | None = None,
       older_than_s: int | None = None) -> dict[str, Any]:
    """Refuse ``apply``; otherwise return :func:`gc_plan`."""
    if apply:
        raise AssetGcRefused(
            "gc --apply is not implemented in this stage; "
            "the asset catalog is dry-run only")
    return gc_plan(root, max_bytes=max_bytes, max_inodes=max_inodes,
                   older_than_s=older_than_s)


__all__ = [
    "AssetConflict", "AssetError", "AssetGcRefused", "AssetMarkerError",
    "AssetNotFound", "AssetPathError", "DEFAULT_KIND", "DEFAULT_OWNER",
    "DEFAULT_SCOPE", "GC_OWNER", "GC_STATES", "KINDS", "LICENSE_STATUSES",
    "MARKER_NAME", "OWNERS", "SCHEMA", "SCOPES", "SOURCE_KINDS", "STATES",
    "adopt", "forget", "gc", "gc_plan", "get", "list_assets", "pin",
    "register", "registry_dir", "scan", "status", "unpin", "user_registry_dir",
    "verify", "verify_all",
]
