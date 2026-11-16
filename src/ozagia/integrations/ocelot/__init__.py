"""Explicit, pinned Ocelot availability, installation and verification.

Ocelot is a separate MIT-licensed checkout. ozagia does not bundle it and
never downloads it implicitly. Resolution order:

1. ``$OZAGIA_OCELOT`` (an explicit checkout or install directory);
2. a sibling checkout, ``<root>/../ocelot`` (development);
3. an installed manifest under
   ``<data home>/ozagia/ocelot/<version>/.ozagia-ocelot.json``.

An empty result is reported as ``not_installed``; it is not an error and no
first-run download is attempted. Reconciliation with Ocelot remains optional.

Installation is a two-step action: a dry run by default, then ``--apply`` with
a pinned SHA-256. The URL must be HTTPS, must not contain credentials and must
not resolve to a private address. Redirects are followed explicitly and
capped. The archive is verified before it is extracted, extraction is atomic
under a temporary directory, and an existing version is never overwritten.

The downloaded archive is trusted only through the caller-supplied checksum.
This module does not verify signatures, audit source code, or decide whether
Ocelot should be used.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ozagia.config import repo_root

ENV_OCELOT = "OZAGIA_OCELOT"
MANIFEST_FILENAME = ".ozagia-ocelot.json"
MANIFEST_FORMAT = "ozagia-ocelot-install/1"
LICENSE_SPDX = "MIT"
LICENSE_NOTE = (
    "Ocelot is MIT-licensed. ozagia does not bundle Ocelot; an installed "
    "copy is a separate checkout, and using it for reconciliation is optional."
)

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_EXTRACT_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
HTTP_TIMEOUT = 30.0
MAX_URL_LENGTH = 4096
USER_AGENT = "ozagia-ocelot-installer/0.1 (+https://github.com/ozmalabs/ozagia)"
MAX_ERROR_DETAILS = 20
MAX_PATH_LENGTH = 1024
MAX_PATH_PART = 255

_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class OcelotError(Exception):
    """Ocelot resolution, installation or verification failed safely."""


class OcelotValidationError(OcelotError, ValueError):
    """A caller-supplied Ocelot value is not acceptable."""


# --------------------------------------------------------------------------- #
# paths and metadata
# --------------------------------------------------------------------------- #
def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def data_home() -> Path:
    """The user data directory (``$XDG_DATA_HOME`` or ``~/.local/share``)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return base


def ocelot_home(dest: str | Path | None = None) -> Path:
    """The root that holds versioned Ocelot installs."""
    if dest is None:
        return data_home() / "ozagia" / "ocelot"
    path = Path(dest).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _validate_version(version: str) -> str:
    if (not isinstance(version, str) or not _VERSION_RE.fullmatch(version)
            or ".." in version or version.endswith(".")):
        raise OcelotValidationError(
            "Ocelot version must be a bounded name such as '1.2.3'")
    return version


def _normalize_sha256(value: str) -> str:
    if not isinstance(value, str):
        raise OcelotValidationError("sha256 must be a hexadecimal string")
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    if not _SHA256_RE.fullmatch(text):
        raise OcelotValidationError("sha256 must be exactly 64 hexadecimal characters")
    return text


def _checkout_version(path: Path) -> str | None:
    """Best-effort version of an unmanaged checkout, or None."""
    pyproject = path / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            data = {}
        project = data.get("project")
        if isinstance(project, dict) and isinstance(project.get("version"), str):
            return project["version"]
        poetry = data.get("tool", {}).get("poetry") if isinstance(data, dict) else None
        if isinstance(poetry, dict) and isinstance(poetry.get("version"), str):
            return poetry["version"]
    for name in ("VERSION", "version.txt"):
        candidate = path / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text and len(text) <= 64 and "\n" not in text:
            return text
    return None


def _version_key(version: str) -> tuple[tuple[int, Any], ...]:
    parts: list[tuple[int, Any]] = []
    for chunk in re.split(r"([0-9]+)", version):
        if not chunk:
            continue
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk.casefold()))
    return tuple(parts)


def _manifest_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not manifest:
        return None
    tree = manifest.get("tree") if isinstance(manifest.get("tree"), dict) else {}
    summary: dict[str, Any] = {
        key: manifest[key]
        for key in ("format", "version", "source", "sha256", "installed_at")
        if key in manifest
    }
    if isinstance(manifest.get("archive"), dict):
        summary["archive"] = dict(manifest["archive"])
    if isinstance(manifest.get("license"), dict):
        summary["license"] = dict(manifest["license"])
    files = tree.get("files") if isinstance(tree, dict) else None
    summary["file_count"] = tree.get("file_count", len(files) if isinstance(files, list) else 0)
    summary["tree_bytes"] = tree.get("bytes")
    summary["tree_sha256"] = tree.get("sha256")
    return summary


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _resolution(home: Path, *, state: str, path: Path | None = None,
                version: str | None = None, source: str | None = None,
                manifest: dict[str, Any] | None = None,
                manifest_path: Path | None = None,
                invalid: list[dict[str, str]] | None = None,
                note: str = "") -> dict[str, Any]:
    return {
        "state": state,
        "path": str(path) if path is not None else None,
        "version": version,
        "source": source,
        "manifest": manifest,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "install_root": str(home),
        "invalid_installs": invalid or [],
        "note": note,
    }


def _installed_candidates(home: Path) -> tuple[list[tuple[Path, dict[str, Any]]],
                                               list[dict[str, str]]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    invalid: list[dict[str, str]] = []
    if not home.is_dir():
        return candidates, invalid
    try:
        children = sorted(home.iterdir())
    except OSError as exc:
        raise OcelotError(f"cannot read Ocelot install root {home}: {exc}") from exc
    for child in children:
        if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
            continue
        manifest_path = child / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = _read_manifest_file(manifest_path)
            _validate_manifest(manifest, expected_version=child.name)
        except OcelotError as exc:
            invalid.append({"path": str(child), "error": str(exc)})
            continue
        candidates.append((child, manifest))
    return candidates, invalid


def resolve(root: str | Path | None = None, *, dest: str | Path | None = None) -> dict[str, Any]:
    """Resolve the Ocelot checkout ozagia should use.

    ``root`` is the repository ozagia works on (default: ``repo_root()``);
    the sibling candidate is ``<root>/../ocelot``. ``dest`` overrides the
    install root that holds versioned installs.
    """
    root_p = Path(root).expanduser().resolve() if root is not None else repo_root()
    home = ocelot_home(dest)

    explicit = os.environ.get(ENV_OCELOT)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve(strict=False)
        if not path.is_dir():
            raise OcelotError(
                f"${ENV_OCELOT} points to {path}, which is not a directory")
        return _resolution(
            home, state="ready", path=path, version=_checkout_version(path),
            source="environment",
            note=f"resolved from ${ENV_OCELOT}; this checkout is not managed by "
                 "the ozagia installer")

    sibling = root_p.parent / "ocelot"
    if sibling.is_dir():
        return _resolution(
            home, state="ready", path=sibling.resolve(),
            version=_checkout_version(sibling), source="sibling",
            note="resolved a sibling Ocelot checkout; it is not managed by the "
                 "ozagia installer")

    candidates, invalid = _installed_candidates(home)
    if candidates:
        candidates.sort(key=lambda item: _version_key(item[1]["version"]),
                        reverse=True)
        path, manifest = candidates[0]
        return _resolution(
            home, state="ready", path=path,
            version=manifest.get("version"), source="installed",
            manifest=manifest, manifest_path=path / MANIFEST_FILENAME,
            invalid=invalid,
            note="resolved the newest installed Ocelot version")

    note = ("Ocelot is not installed. Set OZAGIA_OCELOT, use a sibling "
            "'../ocelot' checkout, or run the explicit installer with a pinned "
            "checksum. No download is attempted automatically.")
    if invalid:
        note += f" {len(invalid)} invalid install manifest(s) were ignored."
    return _resolution(home, state="not_installed", invalid=invalid, note=note)


def status(root: str | Path | None = None, *, dest: str | Path | None = None) -> dict[str, Any]:
    """The resolved Ocelot path, version, source, manifest and licence note."""
    resolved = resolve(root, dest=dest)
    return {
        "state": resolved["state"],
        "installed": resolved["source"] == "installed",
        "path": resolved["path"],
        "version": resolved["version"],
        "source": resolved["source"],
        "manifest": _manifest_summary(resolved["manifest"]),
        "manifest_path": resolved["manifest_path"],
        "install_root": resolved["install_root"],
        "invalid_installs": resolved["invalid_installs"],
        "license": {
            "spdx": LICENSE_SPDX,
            "name": "MIT",
            "bundled": False,
            "note": LICENSE_NOTE,
        },
        "reconciliation_optional": True,
        "note": resolved["note"],
    }


# --------------------------------------------------------------------------- #
# URL, download and checksum
# --------------------------------------------------------------------------- #
def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def _ip_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _resolve_public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise OcelotError(f"cannot resolve host {host!r}: {exc}") from exc
    if not infos:
        raise OcelotError(f"cannot resolve host {host!r}")
    for info in infos:
        address_text = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError as exc:
            raise OcelotError(
                f"host {host!r} returned an unusable address {address_text!r}") from exc
        if not _ip_is_public(address):
            raise OcelotError(f"host {host!r} resolves to a private address")


def _validate_url(url: str, *, resolve_hosts: bool = False) -> str:
    """Accept only HTTPS URLs without credentials or a private destination."""
    if not isinstance(url, str) or not url.strip():
        raise OcelotValidationError("archive URL must be a non-empty string")
    raw = url.strip()
    if len(raw) > MAX_URL_LENGTH or any(ord(char) < 32 for char in raw):
        raise OcelotValidationError("archive URL is too long or contains control characters")
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # noqa: B018 - validates the port
    except ValueError as exc:
        raise OcelotValidationError(f"archive URL is invalid: {exc}") from exc
    if parts.scheme != "https":
        raise OcelotValidationError("archive URL must use HTTPS")
    if parts.username is not None or parts.password is not None:
        raise OcelotValidationError("archive URL must not contain credentials")
    if parts.fragment:
        raise OcelotValidationError("archive URL must not contain a fragment")
    if not host:
        raise OcelotValidationError("archive URL has no host")
    folded = host.casefold().rstrip(".")
    if folded in _LOCAL_HOSTS or folded.endswith(_LOCAL_HOST_SUFFIXES):
        raise OcelotError(f"archive host {host!r} is a local name")
    literal = _literal_ip(host)
    if literal is not None:
        if not _ip_is_public(literal):
            raise OcelotError(f"archive host {host!r} is a private address")
    elif resolve_hosts:
        _resolve_public(host)
    return parts.geturl()


def _redacted_url(url: str) -> str:
    """The URL without a query or fragment, for logs and manifests."""
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        parts = parts._replace(query="", fragment="")
    return parts.geturl()


def _archive_format(url: str) -> str:
    path = urlsplit(url).path.casefold()
    if path.endswith(".tar.gz") or path.endswith(".tgz"):
        return "tar.gz"
    if path.endswith(".zip"):
        return "zip"
    raise OcelotValidationError(
        "archive URL must end in .tar.gz, .tgz or .zip")


def _archive_name(url: str) -> str:
    name = PurePosixPath(unquote(urlsplit(url).path)).name
    clean = re.sub(r"[^A-Za-z0-9._+-]", "_", name)[:128]
    return clean or "archive"


class _NoRedirect(HTTPRedirectHandler):
    """Make redirects explicit so every hop is validated and counted."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_url(request: Request, timeout: float):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _download(url: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES,
              timeout: float = HTTP_TIMEOUT,
              max_redirects: int = MAX_REDIRECTS) -> bytes:
    """Download an HTTPS archive with explicit redirect and size caps."""
    if max_bytes <= 0:
        raise OcelotValidationError("download size cap must be positive")
    if max_redirects < 0:
        raise OcelotValidationError("redirect cap must not be negative")
    current = _validate_url(url, resolve_hosts=True)
    for redirects in range(max_redirects + 1):
        request = Request(current, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            response = _open_url(request, timeout)
        except HTTPError as exc:
            try:
                if exc.code not in _REDIRECT_CODES:
                    raise OcelotError(
                        f"download failed with HTTP {exc.code} for "
                        f"{_redacted_url(current)}") from exc
                location = exc.headers.get("Location") if exc.headers else None
                if not location:
                    raise OcelotError(
                        f"HTTP {exc.code} redirect has no Location for "
                        f"{_redacted_url(current)}") from exc
                if redirects >= max_redirects:
                    raise OcelotError(
                        f"download exceeded {max_redirects} redirects for "
                        f"{_redacted_url(current)}") from exc
                current = _validate_url(
                    urljoin(current, location), resolve_hosts=True)
            finally:
                exc.close()
            continue
        except URLError as exc:
            raise OcelotError(
                f"download failed for {_redacted_url(current)}: {exc.reason}") from exc
        except OSError as exc:
            raise OcelotError(
                f"download failed for {_redacted_url(current)}: {exc}") from exc
        except (ValueError, http.client.HTTPException) as exc:
            raise OcelotError(
                f"download failed for {_redacted_url(current)}: {exc}") from exc
        try:
            with response:
                final = response.geturl()
                if final != current:
                    current = _validate_url(final, resolve_hosts=True)
                code = getattr(response, "status", 200) or 200
                if not 200 <= code < 300:
                    raise OcelotError(
                        f"download failed with HTTP {code} for {_redacted_url(current)}")
                declared = response.headers.get("Content-Length")
                try:
                    declared_bytes = int(declared) if declared is not None else None
                except (TypeError, ValueError):
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > max_bytes:
                    raise OcelotError(
                        f"download exceeds the {max_bytes}-byte limit")
                chunks = bytearray()
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise OcelotError(
                            f"download exceeds the {max_bytes}-byte limit")
                return bytes(chunks)
        except OcelotError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise OcelotError(
                f"download failed for {_redacted_url(current)}: {exc}") from exc
    raise OcelotError(f"download exceeded {max_redirects} redirects")


# --------------------------------------------------------------------------- #
# archive extraction
# --------------------------------------------------------------------------- #
def _safe_parts(name: Any) -> tuple[str, ...]:
    """A relative POSIX path with no escape, drive, backslash or control byte."""
    if not isinstance(name, str) or not name:
        raise OcelotValidationError("archive member has an empty path")
    if (len(name) > MAX_PATH_LENGTH or "\\" in name or "\x00" in name
            or any(ord(char) < 32 for char in name)):
        raise OcelotValidationError(f"archive member path {name!r} is unsafe")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise OcelotValidationError(f"archive member path {name!r} is absolute")
    parts = PurePosixPath(name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OcelotValidationError(f"archive member path {name!r} is unsafe")
    if any(len(part) > MAX_PATH_PART for part in parts):
        raise OcelotValidationError(f"archive member path {name!r} is too long")
    return parts


def _common_top(entries: list[dict[str, Any]]) -> str | None:
    """The single top-level directory of an archive, if there is one.

    An explicit ``ocelot-1.0/`` directory member and its files share the
    prefix; a top-level file with the same name does not.
    """
    if not entries:
        return None
    first = entries[0]["parts"][0]
    if not all(entry["parts"][0] == first for entry in entries):
        return None
    if any(len(entry["parts"]) == 1 and entry["kind"] == "file"
           for entry in entries):
        return None
    if not any(len(entry["parts"]) > 1 for entry in entries):
        return None
    return first


def _plan_entries(entries: list[dict[str, Any]],
                  archive: str) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    top = _common_top(entries)
    seen: dict[str, str] = {}
    planned: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    for entry in entries:
        parts = entry["parts"][1:] if top is not None else entry["parts"]
        if not parts:
            if entry["kind"] == "file":
                raise OcelotError(f"{archive} has a file at the archive root")
            continue
        key = "/".join(parts)
        previous = seen.get(key)
        if previous is not None:
            if previous == "dir" and entry["kind"] == "dir":
                continue
            raise OcelotError(f"{archive} has a duplicate member {key!r}")
        seen[key] = entry["kind"]
        planned.append((parts, entry))
    if not any(entry["kind"] == "file" for _, entry in planned):
        raise OcelotError(f"{archive} contains no regular files")
    return planned


def _ensure_dir(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise OcelotError(f"archive path {current} became a symlink")
        if current.exists():
            if not current.is_dir():
                raise OcelotError(f"archive path {current} collides with a file")
        else:
            current.mkdir(mode=0o755)
    return current


def _write_stream(target: Path, parts: tuple[str, ...], stream: Any,
                  expected: int, executable: bool, total_so_far: int) -> dict[str, Any]:
    if expected < 0 or expected > MAX_EXTRACT_BYTES:
        raise OcelotError("archive member exceeds the extraction size limit")
    if total_so_far + expected > MAX_EXTRACT_BYTES:
        raise OcelotError("archive exceeds the extraction size limit")
    parent = _ensure_dir(target, parts[:-1])
    path = parent / parts[-1]
    if path.is_symlink() or path.exists():
        raise OcelotError(f"archive member {path} already exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o755 if executable else 0o644)
    except OSError as exc:
        raise OcelotError(f"cannot create {path}: {exc}") from exc
    digest = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected:
                    raise OcelotError(
                        f"archive member {path} is larger than declared")
                if total_so_far + written > MAX_EXTRACT_BYTES:
                    raise OcelotError("archive exceeds the extraction size limit")
                digest.update(chunk)
                handle.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if written != expected:
        path.unlink(missing_ok=True)
        raise OcelotError(f"archive member {path} is shorter than declared")
    try:
        os.chmod(path, 0o755 if executable else 0o644)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise OcelotError(f"cannot set the mode of {path}: {exc}") from exc
    rel = "/".join(parts)
    return {"path": rel, "sha256": digest.hexdigest(), "size": written}


def _extract_tar(data: bytes, target: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    total = 0
    written = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise OcelotError("tar.gz archive is empty")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise OcelotError("tar.gz archive has too many members")
            entries: list[dict[str, Any]] = []
            for member in members:
                if member.isdir():
                    kind, size = "dir", 0
                elif member.isfile():
                    kind, size = "file", member.size
                else:
                    raise OcelotError(
                        f"tar.gz member {member.name!r} is not a regular file "
                        "or directory")
                if kind == "dir" and member.name.rstrip("/") in ("", "."):
                    continue
                if size < 0 or total + size > MAX_EXTRACT_BYTES:
                    raise OcelotError("tar.gz archive exceeds the extraction size limit")
                total += size
                entries.append({
                    "parts": _safe_parts(member.name), "kind": kind, "size": size,
                    "executable": bool(member.mode & 0o111), "member": member,
                })
            for parts, entry in _plan_entries(entries, "tar.gz"):
                if entry["kind"] == "dir":
                    _ensure_dir(target, parts)
                    continue
                extracted = archive.extractfile(entry["member"])
                if extracted is None:
                    raise OcelotError(
                        f"cannot read tar.gz member {entry['member'].name!r}")
                with extracted:
                    item = _write_stream(
                        target, parts, extracted, entry["size"],
                        entry["executable"], written)
                written += item["size"]
                files.append(item)
    except OcelotError:
        raise
    except (tarfile.TarError, EOFError, OSError, UnicodeError, ValueError,
            NotImplementedError) as exc:
        raise OcelotError(f"cannot extract tar.gz archive: {exc}") from exc
    return files


def _extract_zip(data: bytes, target: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    total = 0
    written = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos:
                raise OcelotError("zip archive is empty")
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise OcelotError("zip archive has too many members")
            entries: list[dict[str, Any]] = []
            for info in infos:
                if info.flag_bits & 0x1:
                    raise OcelotError("zip archive is encrypted")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise OcelotError(f"zip member {info.filename!r} is a symlink")
                if info.is_dir() or stat.S_ISDIR(mode):
                    kind, size = "dir", 0
                elif mode and stat.S_IFMT(mode) and not stat.S_ISREG(mode):
                    raise OcelotError(
                        f"zip member {info.filename!r} is not a regular file "
                        "or directory")
                else:
                    kind, size = "file", info.file_size
                if kind == "dir" and info.filename.rstrip("/") in ("", "."):
                    continue
                if size < 0 or total + size > MAX_EXTRACT_BYTES:
                    raise OcelotError("zip archive exceeds the extraction size limit")
                total += size
                entries.append({
                    "parts": _safe_parts(info.filename), "kind": kind, "size": size,
                    "executable": bool(mode & 0o111), "info": info,
                })
            for parts, entry in _plan_entries(entries, "zip"):
                if entry["kind"] == "dir":
                    _ensure_dir(target, parts)
                    continue
                with archive.open(entry["info"]) as extracted:
                    item = _write_stream(
                        target, parts, extracted, entry["size"],
                        entry["executable"], written)
                written += item["size"]
                files.append(item)
    except OcelotError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError,
            UnicodeError, ValueError, RuntimeError, NotImplementedError) as exc:
        raise OcelotError(f"cannot extract zip archive: {exc}") from exc
    return files


def _extract(data: bytes, target: Path, archive_format: str) -> list[dict[str, Any]]:
    if archive_format == "tar.gz":
        files = _extract_tar(data, target)
    elif archive_format == "zip":
        files = _extract_zip(data, target)
    else:  # pragma: no cover - _archive_format only returns the two above
        raise OcelotValidationError(f"unsupported archive format {archive_format!r}")
    return sorted(files, key=lambda item: item["path"])


def _tree_digest(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry["path"]):
        digest.update(
            f"{item['path']}\0{item['sha256']}\0{item['size']}\n".encode("utf-8"))
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# manifests
# --------------------------------------------------------------------------- #
def _read_manifest_file(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise OcelotError(f"manifest {path} is too large")
        raw = path.read_bytes()
    except OSError as exc:
        raise OcelotError(f"cannot read manifest {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OcelotError(f"manifest {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OcelotError(f"manifest {path} must be a JSON object")
    return data


def _validate_manifest(manifest: dict[str, Any],
                       *, expected_version: str | None = None) -> None:
    if not isinstance(manifest, dict):
        raise OcelotValidationError("manifest must be a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise OcelotValidationError(
            f"manifest format must be {MANIFEST_FORMAT!r}")
    version = _validate_version(manifest.get("version"))
    if expected_version is not None and version != expected_version:
        raise OcelotValidationError(
            f"manifest version {version!r} does not match directory "
            f"{expected_version!r}")
    _normalize_sha256(manifest.get("sha256"))
    source = manifest.get("source")
    if not isinstance(source, str) or not source:
        raise OcelotValidationError("manifest source must be a non-empty URL")
    _validate_url(source)
    installed_at = manifest.get("installed_at")
    if (not isinstance(installed_at, str) or not installed_at
            or len(installed_at) > 64):
        raise OcelotValidationError("manifest installed_at must be a timestamp")
    tree = manifest.get("tree")
    if not isinstance(tree, dict):
        raise OcelotValidationError("manifest tree must be an object")
    files = tree.get("files")
    if not isinstance(files, list) or not files:
        raise OcelotValidationError("manifest tree must list at least one file")
    if len(files) > MAX_ARCHIVE_MEMBERS:
        raise OcelotValidationError("manifest lists too many files")
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise OcelotValidationError("manifest file entries must be objects")
        rel = item.get("path")
        parts = _safe_parts(rel)
        key = "/".join(parts)
        if key in seen:
            raise OcelotValidationError(f"manifest repeats file {key!r}")
        seen.add(key)
        _normalize_sha256(item.get("sha256"))
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise OcelotValidationError(f"manifest size for {key!r} is invalid")
        total += size
        if total > MAX_EXTRACT_BYTES:
            raise OcelotValidationError("manifest file sizes exceed the limit")
    tree_sha = tree.get("sha256")
    if tree_sha is not None:
        _normalize_sha256(tree_sha)
    file_count = tree.get("file_count")
    if file_count is not None and file_count != len(files):
        raise OcelotValidationError("manifest file_count is inconsistent")
    tree_bytes = tree.get("bytes")
    if tree_bytes is not None and tree_bytes != total:
        raise OcelotValidationError("manifest tree bytes are inconsistent")


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    path = directory / MANIFEST_FILENAME
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise OcelotError(f"cannot write manifest {path}: {exc}") from exc
    return path


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #
def _existing_install(target: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not os.path.lexists(target):
        return None, None
    if target.is_symlink() or not target.is_dir():
        raise OcelotError(
            f"destination {target} already exists and is not a directory; "
            "refusing to overwrite")
    manifest_path = target / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None, "the directory exists but has no ozagia install manifest"
    try:
        manifest = _read_manifest_file(manifest_path)
        _validate_manifest(manifest, expected_version=target.name)
    except OcelotError as exc:
        return None, str(exc)
    return manifest, None


def install(version: str, url: str, sha256: str, *, dest: str | Path | None = None,
            dry_run: bool = True, allow_download: bool = False) -> dict[str, Any]:
    """Install a pinned Ocelot archive into a versioned directory.

    Dry run is the default: nothing is downloaded or written. A real install
    needs both ``dry_run=False`` and ``allow_download=True`` (the CLI's
    ``--apply``), plus a matching SHA-256. Existing installations are never
    overwritten, including an existing directory for the same version.
    """
    version = _validate_version(version)
    expected = _normalize_sha256(sha256)
    source = _validate_url(url)
    archive_format = _archive_format(source)
    home = ocelot_home(dest)
    target = home / version
    if os.path.lexists(home) and not home.is_dir():
        raise OcelotError(f"install root {home} is not a directory")

    existing, problem = _existing_install(target)
    if problem is not None:
        raise OcelotError(
            f"destination {target} is not a usable managed install ({problem}); "
            "refusing to overwrite it")
    if existing is not None:
        installed_sha = _normalize_sha256(existing.get("sha256"))
        if installed_sha != expected:
            raise OcelotError(
                f"Ocelot {version} is already installed at {target} from a "
                "different archive; refusing to overwrite it")
        report = verify(version, dest=home)
        if not report["ok"]:
            raise OcelotError(
                f"existing Ocelot {version} at {target} failed verification; "
                "remove it and install again")
        return {
            "action": "already_installed",
            "applied": False,
            "version": version,
            "source": _redacted_url(source),
            "sha256": expected,
            "path": str(target),
            "manifest_path": str(target / MANIFEST_FILENAME),
            "manifest": _manifest_summary(existing),
        }

    if dry_run:
        return {
            "action": "dry_run",
            "applied": False,
            "would_install": True,
            "version": version,
            "source": _redacted_url(source),
            "sha256": expected,
            "dest": str(home),
            "path": str(target),
            "archive": {"name": _archive_name(source), "format": archive_format},
            "note": "no download or write was performed; re-run with "
                    "allow_download=True (CLI --apply) to install",
        }
    if not allow_download:
        raise OcelotError(
            "refusing to download Ocelot: pass allow_download=True "
            "(CLI --apply) with a pinned checksum")

    data = _download(source)
    if not isinstance(data, bytes):
        raise OcelotError("downloader did not return archive bytes")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise OcelotError("downloaded archive exceeds the size limit")
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise OcelotError(
            f"checksum mismatch: expected sha256:{expected}, got sha256:{actual}; "
            "nothing was extracted")

    temp: Path | None = None
    try:
        try:
            home.mkdir(parents=True, exist_ok=True)
            if not home.is_dir():
                raise OcelotError(f"install root {home} is not a directory")
            temp = Path(tempfile.mkdtemp(prefix=f".{version}.tmp-", dir=home))
            os.chmod(temp, 0o755)
        except OcelotError:
            raise
        except OSError as exc:
            raise OcelotError(f"cannot prepare install root {home}: {exc}") from exc
        files = _extract(data, temp, archive_format)
        manifest = {
            "format": MANIFEST_FORMAT,
            "version": version,
            "source": _redacted_url(source),
            "sha256": expected,
            "archive": {
                "name": _archive_name(source),
                "format": archive_format,
                "bytes": len(data),
            },
            "installed_at": _utc(),
            "license": {"spdx": LICENSE_SPDX, "bundled": False},
            "tree": {
                "sha256": _tree_digest(files),
                "file_count": len(files),
                "bytes": sum(item["size"] for item in files),
                "files": files,
            },
        }
        _write_manifest(temp, manifest)
        if os.path.lexists(target):
            raise OcelotError(
                f"destination {target} appeared while installing; refusing to "
                "overwrite it")
        try:
            os.rename(temp, target)
        except OSError as exc:
            raise OcelotError(
                f"cannot move the installed tree into {target}: {exc}") from exc
        temp = None
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)

    return {
        "action": "installed",
        "applied": True,
        "version": version,
        "source": _redacted_url(source),
        "sha256": expected,
        "path": str(target),
        "manifest_path": str(target / MANIFEST_FILENAME),
        "manifest": _manifest_summary(manifest),
    }


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _no_symlink_ancestors(target: Path, parts: tuple[str, ...]) -> bool:
    current = target
    for part in parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _extra_files(target: Path, recorded: set[str]) -> tuple[list[str], list[str]]:
    extras: list[str] = []
    unsafe: list[str] = []
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            candidate = base / name
            rel = candidate.relative_to(target).as_posix()
            if candidate.is_symlink():
                unsafe.append(rel)
                dirnames.remove(name)
            elif name == "__pycache__":
                dirnames.remove(name)
        for name in filenames:
            candidate = base / name
            rel = candidate.relative_to(target).as_posix()
            if rel == MANIFEST_FILENAME:
                continue
            if candidate.is_symlink():
                unsafe.append(rel)
                continue
            if name.endswith((".pyc", ".pyo")):
                continue
            if rel not in recorded:
                extras.append(rel)
    return sorted(extras), sorted(unsafe)


def verify(version: str | None = None, *, dest: str | Path | None = None,
           sha256: str | None = None) -> dict[str, Any]:
    """Verify an installed manifest and every file it records.

    With no ``version`` the newest installed checkout under the install root
    is used; a development checkout is never treated as an install. A
    supplied ``sha256`` is compared with the archive checksum recorded in the
    manifest. Missing or malformed manifests raise ``OcelotError``; a
    readable install that fails its checks returns ``ok: false``.
    """
    expected = _normalize_sha256(sha256) if sha256 is not None else None
    if version is None:
        home = ocelot_home(dest)
        candidates, invalid = _installed_candidates(home)
        if not candidates:
            detail = f" ({len(invalid)} invalid manifest(s) ignored)" if invalid else ""
            raise OcelotError(
                f"no installed Ocelot manifest to verify under {home}{detail}")
        candidates.sort(key=lambda item: _version_key(item[1]["version"]),
                        reverse=True)
        target, manifest = candidates[0]
        version = manifest["version"]
    else:
        version = _validate_version(version)
        target = ocelot_home(dest) / version
    if not target.is_dir() or target.is_symlink():
        raise OcelotError(f"Ocelot install directory {target} does not exist")
    manifest_path = target / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise OcelotError(f"Ocelot install {target} has no manifest")
    manifest = _read_manifest_file(manifest_path)
    _validate_manifest(manifest, expected_version=version)

    missing: list[str] = []
    mismatched: list[str] = []
    unsafe: list[str] = []
    checked = 0
    files = manifest["tree"]["files"]
    for item in files:
        rel = item["path"]
        parts = _safe_parts(rel)
        path = target.joinpath(*parts)
        if not _no_symlink_ancestors(target, parts):
            unsafe.append(rel)
            continue
        if not path.is_file():
            missing.append(rel)
            continue
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != item["sha256"] or path.stat().st_size != item["size"]:
                mismatched.append(rel)
            else:
                checked += 1
        except OSError:
            missing.append(rel)

    recorded = {item["path"] for item in files}
    extras, extra_unsafe = _extra_files(target, recorded)
    unsafe.extend(item for item in extra_unsafe if item not in unsafe)
    errors: list[str] = []
    if missing:
        errors.append(f"{len(missing)} recorded file(s) are missing")
    if mismatched:
        errors.append(f"{len(mismatched)} recorded file(s) do not match their hash or size")
    if unsafe:
        errors.append(f"{len(unsafe)} path(s) are symlinks or otherwise unsafe")
    tree_sha = manifest["tree"].get("sha256")
    if tree_sha is not None and _tree_digest(files) != tree_sha:
        errors.append("manifest tree digest does not match its file list")
    if expected is not None and expected != manifest["sha256"]:
        errors.append("manifest archive checksum does not match the expected checksum")

    def _limit(values: list[str]) -> list[str]:
        return values[:MAX_ERROR_DETAILS]

    return {
        "ok": not errors,
        "state": "verified" if not errors else "failed",
        "version": version,
        "path": str(target),
        "manifest_path": str(manifest_path),
        "sha256": manifest["sha256"],
        "expected_sha256": expected,
        "manifest": _manifest_summary(manifest),
        "files_checked": checked,
        "files_missing": _limit(missing),
        "files_mismatched": _limit(mismatched),
        "files_unsafe": _limit(unsafe),
        "extra_files": _limit(extras),
        "extra_file_count": len(extras),
        "errors": errors,
        "errors_truncated": (
            max(0, len(missing) - MAX_ERROR_DETAILS)
            + max(0, len(mismatched) - MAX_ERROR_DETAILS)
            + max(0, len(unsafe) - MAX_ERROR_DETAILS)
        ) > 0,
    }


__all__ = [
    "ENV_OCELOT",
    "LICENSE_NOTE",
    "LICENSE_SPDX",
    "MANIFEST_FILENAME",
    "MANIFEST_FORMAT",
    "OcelotError",
    "OcelotValidationError",
    "data_home",
    "install",
    "ocelot_home",
    "resolve",
    "status",
    "verify",
]
