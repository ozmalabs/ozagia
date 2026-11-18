"""Secret references, execution-boundary resolution and redaction.

Secrets are passed **by reference**; a value is resolved only at the boundary
that needs it (a child environment or process). Configuration, job records,
logs, CLI output and model messages carry ``secret://...`` references, never
the value.

Reference grammar (percent-encoding is decoded before validation)::

    secret://env/NAME
    secret://file/NAME
    secret://keyring/SERVICE/ACCOUNT
    secret://pass/PATH
    secret://op/VAULT/ITEM/FIELD
    secret://session/TOKEN

The ``literal://`` scheme does not exist: there is no plaintext-value fallback.
A malformed reference raises :class:`SecretRefError` with a stable ``code``.

Text pasted into a chat can be captured in a memory-only
:class:`SessionSecretStore` with a bounded TTL and a random
``secret://session/...`` reference; :func:`detect_text` rewrites conservative
common secret shapes to caller-supplied references. Session references resolve
only with the matching harness/session context and are never written to disk.

Resolution is deliberately explicit. :class:`SecretValue` masks itself in
``str``, ``repr`` and ``format`` output; :meth:`SecretValue.reveal` is the
single accessor. :class:`SecretBroker` resolves references through providers
that are enabled in ``[secrets]`` (default ``env,file``), registers every
resolved value with a bounded :class:`Redactor`, and returns only status for
:meth:`SecretBroker.check`/:meth:`SecretBroker.check_many`.

This module is standard-library only. ``keyring``, ``pass`` and ``op`` are
optional providers: a missing package or executable is reported as
``provider_unavailable``, never imported at module import time and never
installed here. No provider is contacted by importing this module.
"""
from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import os
import re
import secrets as _stdlib_secrets
import shlex
import shutil
import stat
import subprocess
import threading
import time
import tomllib
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote_to_bytes

from ozagia.config import (
    ConfigError,
    STATE_DIRNAME,
    config_sources,
    load_config_section,
    repo_config_path,
)

SCHEMA = "ozagia.secrets.v1"
SECRETS_SECTION = "secrets"
SECRETS_KEYS = (
    "providers",
    "allow_cli",
    "allow_network",
    "allow_absolute_files",
    "max_bytes",
    "redact_output",
)
#: Providers a project may enable in `[secrets] providers`.
CONFIG_PROVIDER_NAMES = ("env", "file", "keyring", "pass", "op")
#: Every provider a reference may name. `session` is in-memory and explicit.
PROVIDER_NAMES = (*CONFIG_PROVIDER_NAMES, "session")
CLI_PROVIDERS = ("pass", "op")
DEFAULT_PROVIDERS = ("env", "file")
DEFAULT_MAX_BYTES = 4096
MAX_CONFIG_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 1024 * 1024
MAX_REF_LENGTH = 1024
MAX_FILE_PATH = 4096
MAX_ENV_NAME = 128
MAX_CACHE_ENTRIES = 64
MAX_DISCOVERED_REFS = 256
MAX_DISCOVERY_BYTES = 1024 * 1024
MAX_DISCOVERY_DEPTH = 16
MAX_DETECT_CHARS = 512 * 1024
DEFAULT_DETECT_LIMIT = 16
MAX_DETECT_LIMIT = 256

#: Session captures are memory-only and bounded.
SESSION_CAPTURE_MAX_ENTRIES = 32
SESSION_CAPTURE_MAX_BYTES = 16 * 1024
SESSION_CAPTURE_TTL_S = 3600.0
SESSION_CAPTURE_MAX_TTL_S = 24 * 3600.0
SESSION_CAPTURE_MAX_SESSIONS = 64
SESSION_TOKEN_BYTES = 24
MIN_DETECTED_SECRET_LENGTH = 8


#: Environment names that configure the broker (not secret values).
ENV_PROVIDERS = "OZAGIA_SECRET_PROVIDERS"
ENV_ALLOW_NETWORK = "OZAGIA_ALLOW_SECRET_NETWORK"
ENV_SECRETS_DIR = "OZAGIA_SECRETS_DIR"

REF_PREFIX = "secret://"
LITERAL_PREFIX = "literal://"
REDACTED = "[REDACTED]"

STATUS_AVAILABLE = "available"
STATUS_MISSING = "missing"
STATUS_DENIED = "denied"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"
STATUS_INVALID = "invalid"
STATUS_ERROR = "error"
STATUSES = (
    STATUS_AVAILABLE,
    STATUS_MISSING,
    STATUS_DENIED,
    STATUS_PROVIDER_UNAVAILABLE,
    STATUS_INVALID,
    STATUS_ERROR,
)

_PROVIDER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,255}\Z")
_KEYRING_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+-]{0,127}\Z")
_PASS_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")
_OP_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_PARAM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}\Z")
_SECRET_REF_SPAN = re.compile(r"secret://[A-Za-z0-9._~%/:@+-]+")
_HEX = frozenset("0123456789abcdefABCDEF")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RUNNER_ENV_KEYS = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "PASSWORD_STORE_DIR", "PASSWORD_STORE_GPG_OPTS", "GNUPGHOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "OP_CONFIG_DIR", "OP_ACCOUNT",
)
_TRIVIAL_VALUES = frozenset({
    "changeme", "change-me", "default", "example", "false", "none", "null",
    "password", "placeholder", "secret", "test", "true",
})


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class SecretError(ValueError):
    """A secret reference or provider operation failed safely.

    ``code`` is a stable, machine-readable reason. Messages never contain a
    resolved value, and provider stderr/stdout is never copied into one.
    """

    def __init__(self, message: str, *, code: str = "secret_error") -> None:
        super().__init__(message)
        self.code = code


class SecretRefError(SecretError):
    """The reference is not valid."""


class SecretNotFound(SecretError):
    """The reference is valid but has no value."""


class SecretDenied(SecretError):
    """Policy or containment denies the reference."""


class SecretProviderUnavailable(SecretError):
    """An optional provider (package or executable) is unavailable."""


class SecretProviderError(SecretError):
    """A provider ran but could not return a value."""


# --------------------------------------------------------------------------- #
# references
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SecretRef:
    """A validated ``secret://`` reference.

    ``original`` is the text the caller supplied. ``segments`` are decoded and
    validated. The reference is not a secret value and may be logged, stored in
    a job record or sent in a model message.
    """

    provider: str
    segments: tuple[str, ...]
    original: str

    @property
    def scheme(self) -> str:
        return "secret"

    @property
    def path(self) -> str:
        """The provider path, joined with ``/``."""
        return "/".join(self.segments)

    @property
    def name(self) -> str:
        """The first segment (an environment or file name)."""
        return self.segments[0] if self.segments else ""

    @property
    def canonical(self) -> str:
        """A normalized reference with each segment percent-encoded."""
        return REF_PREFIX + self.provider + "/" + "/".join(
            quote(segment, safe="") for segment in self.segments)

    def __str__(self) -> str:
        return self.original

    def __repr__(self) -> str:
        return f"SecretRef({self.original!r})"


def _invalid(message: str, code: str) -> SecretRefError:
    return SecretRefError(message, code=code)


def _check_raw(reference: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in reference):
        raise _invalid("secret reference contains control characters",
                       "invalid_control")


def _decode_segment(raw: str) -> str:
    """Decode one percent-encoded segment and reject unsafe results."""
    if not raw:
        raise _invalid("secret reference has an empty segment",
                       "invalid_segment")
    for index, char in enumerate(raw):
        if char != "%":
            continue
        if index + 2 >= len(raw) or raw[index + 1] not in _HEX \
                or raw[index + 2] not in _HEX:
            raise _invalid("secret reference has invalid percent-encoding",
                           "invalid_encoding")
    try:
        data = unquote_to_bytes(raw)
        text = data.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        raise _invalid("secret reference is not valid UTF-8",
                       "invalid_encoding") from None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise _invalid("secret reference contains control characters",
                       "invalid_control")
    if "\x00" in text or "\\" in text:
        raise _invalid("secret reference contains an unsafe character",
                       "invalid_segment")
    if any(char.isspace() for char in text):
        raise _invalid("secret reference contains whitespace",
                       "invalid_segment")
    return text


def _parse_file_path(remainder: str, allow_absolute: bool) -> tuple[str, ...]:
    if remainder.startswith("/"):
        if not allow_absolute:
            raise _invalid("absolute file references are not allowed",
                           "absolute_path")
        if len(remainder) > MAX_FILE_PATH:
            raise _invalid("file reference is too long", "invalid_length")
        parts: list[str] = []
        for raw in remainder.split("/"):
            if not raw:
                continue
            part = _decode_segment(raw)
            if part in (".", "..") or ".." in part:
                raise _invalid("file reference contains traversal",
                               "invalid_traversal")
            if "/" in part or "\\" in part \
                    or not _FILE_NAME.fullmatch(part):
                raise _invalid("file reference has an unsafe name",
                               "invalid_segment")
            parts.append(part)
        if not parts:
            raise _invalid("file reference has no name", "invalid_segment")
        return ("/" + "/".join(parts),)
    if "/" in remainder:
        if any(part in (".", "..") or ".." in part
               for part in (_decode_segment(raw) for raw in remainder.split("/") if raw)):
            raise _invalid("file reference contains traversal",
                           "invalid_traversal")
        raise _invalid("file reference must name one file",
                       "invalid_segment_count")
    name = _decode_segment(remainder)
    if name.startswith("/"):
        raise _invalid("absolute file references are not allowed",
                       "absolute_path")
    if name in (".", "..") or ".." in name:
        raise _invalid("file reference contains traversal",
                       "invalid_traversal")
    if not _FILE_NAME.fullmatch(name) or len(name) > 255:
        raise _invalid("file reference has an unsafe name", "invalid_segment")
    return (name,)


def parse_ref(reference: str, *, allow_absolute: bool = False) -> SecretRef:
    """Parse and validate one ``secret://`` reference.

    Percent-encoding is decoded before validation, so encoded traversal
    (``%2E%2E``), separators (``%2F``) and control characters are rejected.
    ``literal://`` is rejected explicitly; there is no plaintext fallback.
    Absolute ``file`` references are rejected unless ``allow_absolute`` is
    true (the ``[secrets] allow_absolute_files`` escape hatch).
    """
    if not isinstance(reference, str):
        raise _invalid("secret reference must be a string", "invalid_ref")
    if not reference:
        raise _invalid("secret reference is empty", "invalid_ref")
    if len(reference) > MAX_REF_LENGTH:
        raise _invalid("secret reference is too long", "invalid_length")
    _check_raw(reference)
    if reference.startswith(LITERAL_PREFIX):
        raise _invalid("literal:// values are not secret references",
                       "literal_scheme")
    if not reference.startswith(REF_PREFIX):
        raise _invalid("expected a secret:// reference", "invalid_scheme")
    rest = reference[len(REF_PREFIX):]
    provider_raw, separator, remainder = rest.partition("/")
    if not separator or not provider_raw or not remainder:
        raise _invalid("secret reference needs a provider and a path",
                       "invalid_ref")
    if not _PROVIDER_NAME.fullmatch(provider_raw):
        raise _invalid("secret reference has an invalid provider",
                       "invalid_provider")
    provider = provider_raw.lower()
    if provider not in PROVIDER_NAMES:
        raise _invalid("unknown secret provider", "unknown_provider")

    if provider == "file":
        segments = _parse_file_path(remainder, allow_absolute)
    else:
        raw_segments = remainder.split("/")
        if any(not raw for raw in raw_segments):
            raise _invalid("secret reference has an empty segment",
                           "invalid_segment")
        segments = tuple(_decode_segment(raw) for raw in raw_segments)
        if provider == "env":
            if len(segments) != 1:
                raise _invalid("env reference takes one name",
                               "invalid_segment_count")
            name = segments[0]
            if len(name) > MAX_ENV_NAME or not _ENV_NAME.fullmatch(name) \
                    or _ENV_ASSIGN.match(name):
                raise _invalid("env reference has an invalid name",
                               "invalid_segment")
        elif provider == "keyring":
            if len(segments) != 2:
                raise _invalid("keyring reference takes SERVICE/ACCOUNT",
                               "invalid_segment_count")
            if any(not _KEYRING_NAME.fullmatch(segment)
                   for segment in segments):
                raise _invalid("keyring reference has an invalid name",
                               "invalid_segment")
        elif provider == "pass":
            if not 1 <= len(segments) <= 8:
                raise _invalid("pass reference has too many segments",
                               "invalid_segment_count")
            if len("/".join(segments)) > 512:
                raise _invalid("pass reference is too long",
                               "invalid_length")
            if any(segment in (".", "..") or not _PASS_PART.fullmatch(segment)
                   for segment in segments):
                raise _invalid("pass reference has an invalid path",
                               "invalid_segment")
        elif provider == "op":
            if len(segments) != 3:
                raise _invalid("op reference takes VAULT/ITEM/FIELD",
                               "invalid_segment_count")
            if any(not _OP_PART.fullmatch(segment) for segment in segments):
                raise _invalid("op reference has an invalid name",
                               "invalid_segment")
        elif provider == "session":
            if len(segments) != 1:
                raise _invalid("session reference takes one token",
                               "invalid_segment_count")
            if not _SESSION_TOKEN.fullmatch(segments[0]):
                raise _invalid("session reference has an invalid token",
                               "invalid_segment")
    return SecretRef(provider=provider, segments=segments, original=reference)


def try_parse_ref(reference: str, *, allow_absolute: bool = False
                  ) -> tuple[SecretRef | None, SecretRefError | None]:
    """Parse without raising; return ``(ref, None)`` or ``(None, error)``."""
    try:
        return parse_ref(reference, allow_absolute=allow_absolute), None
    except SecretRefError as exc:
        return None, exc


# --------------------------------------------------------------------------- #
# masked values and redaction
# --------------------------------------------------------------------------- #
class SecretValue:
    """A resolved value plus its reference.

    The value is not part of ``str``, ``repr``, ``format``, equality or hash.
    :meth:`reveal` is the only accessor that returns it.
    """

    __slots__ = ("ref", "_value", "warnings")

    def __init__(self, ref: SecretRef | str, value: str, *,
                 warnings: Sequence[str] = ()) -> None:
        if isinstance(ref, str):
            ref = parse_ref(ref)
        if not isinstance(ref, SecretRef):
            raise TypeError("ref must be a SecretRef or secret:// string")
        if not isinstance(value, str):
            raise TypeError("secret value must be a string")
        self.ref = ref
        self._value = value
        self.warnings = tuple(str(item) for item in warnings)

    @property
    def provider(self) -> str:
        return self.ref.provider

    @property
    def reference(self) -> str:
        return self.ref.original

    def reveal(self) -> str:
        """Return the resolved value. Callers must not log or serialize it."""
        return self._value

    def __str__(self) -> str:
        return f"SecretValue({self.ref})"

    def __repr__(self) -> str:
        return f"SecretValue({self.ref})"

    def __format__(self, spec: str) -> str:
        return str(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self.ref == other.ref
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.ref)

    def __bool__(self) -> bool:
        return bool(self._value)


class Redactor:
    """A bounded registry of resolved values and their encoded forms.

    Every registered value of at least ``min_length`` characters is replaced
    in exact, JSON-escaped, URL-encoded, shell-quoted and base64 forms. Values
    are sorted longest-first so one value cannot hide inside another. Empty
    and trivial values are never registered, and no value appears in
    ``repr`` or any report.
    """

    MIN_LENGTH = 8
    MAX_VALUES = 64
    MAX_VALUE_CHARS = 8192
    MAX_DEPTH = 32

    def __init__(self, *, min_length: int = MIN_LENGTH,
                 max_values: int = MAX_VALUES) -> None:
        self._min_length = max(1, int(min_length))
        self._max_values = max(1, int(max_values))
        self._values: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._patterns: tuple[str, ...] = ()
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def __repr__(self) -> str:
        return f"Redactor(values={len(self._values)})"

    def _trivial(self, value: str) -> bool:
        if len(value) < self._min_length or len(value) > self.MAX_VALUE_CHARS:
            return True
        if not value.strip():
            return True
        if len(set(value)) <= 1:
            return True
        return value.strip().lower() in _TRIVIAL_VALUES

    @staticmethod
    def _variants(value: str) -> tuple[str, ...]:
        out: list[str] = [value]
        for ensure_ascii in (False, True):
            escaped = json.dumps(value, ensure_ascii=ensure_ascii)[1:-1]
            if escaped and escaped != value:
                out.append(escaped)
        for safe in ("", "/"):
            encoded = quote(value, safe=safe)
            if encoded and encoded != value:
                out.append(encoded)
        shell = shlex.quote(value)
        if shell and shell != value:
            out.append(shell)
        raw = value.encode("utf-8")
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encoder(raw).decode("ascii")
            if encoded and encoded != value:
                out.append(encoded)
                if encoded.endswith("="):
                    stripped = encoded.rstrip("=")
                    if stripped:
                        out.append(stripped)
        # Never replace the marker itself or another empty target.
        return tuple(dict.fromkeys(
            item for item in out
            if item and item not in (REDACTED,)))

    def add(self, value: str) -> None:
        """Register one value. Re-registering refreshes its recency."""
        if not isinstance(value, str) or self._trivial(value):
            return
        variants = self._variants(value)
        with self._lock:
            self._values.pop(value, None)
            self._values[value] = variants
            while len(self._values) > self._max_values:
                self._values.popitem(last=False)
            self._patterns = tuple(sorted(
                {item for items in self._values.values() for item in items},
                key=lambda item: (-len(item), item)))

    def contains(self, value: str) -> bool:
        """Whether ``value`` itself is registered (not its encoded forms)."""
        with self._lock:
            return isinstance(value, str) and value in self._values

    def redact(self, text: str) -> str:
        """Replace every registered form in ``text`` with ``[REDACTED]``."""
        if not isinstance(text, str) or not text:
            return str(text)
        with self._lock:
            patterns = self._patterns
            if not patterns:
                return str(text)
            result = text
            for pattern in patterns:
                if pattern and pattern in result:
                    result = result.replace(pattern, REDACTED)
        return result

    def redact_structure(self, value: Any) -> Any:
        """Recursively redact strings in dicts, lists and tuples."""
        return self._redact_structure(value, 0, set())

    def _redact_structure(self, value: Any, depth: int,
                          seen: set[int]) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if depth >= self.MAX_DEPTH:
            return value
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                return value
            seen.add(marker)
            try:
                return {
                    (self.redact(key) if isinstance(key, str) else key):
                    self._redact_structure(item, depth + 1, seen)
                    for key, item in value.items()
                }
            finally:
                seen.discard(marker)
        if isinstance(value, list):
            marker = id(value)
            if marker in seen:
                return value
            seen.add(marker)
            try:
                return [self._redact_structure(item, depth + 1, seen)
                        for item in value]
            finally:
                seen.discard(marker)
        if isinstance(value, tuple):
            marker = id(value)
            if marker in seen:
                return value
            seen.add(marker)
            try:
                return tuple(self._redact_structure(item, depth + 1, seen)
                             for item in value)
            finally:
                seen.discard(marker)
        return value


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def _parse_name_list(value: Any, allowed: Sequence[str], key: str
                     ) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of names")
    names = tuple(dict.fromkeys(item.strip().lower() for item in value
                                if item.strip()))
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise ValueError(f"{key} must be a subset of "
                         + ", ".join(sorted(allowed)))
    return names


def _parse_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _parse_bytes(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return min(value, MAX_CONFIG_BYTES)


def _parse_secret_value(key: str, value: Any) -> Any:
    if key == "providers":
        return _parse_name_list(value, CONFIG_PROVIDER_NAMES, key)
    if key == "allow_cli":
        return _parse_name_list(value, CLI_PROVIDERS, key)
    if key == "allow_network":
        return _parse_bool(value, key)
    if key == "allow_absolute_files":
        return _parse_bool(value, key)
    if key == "max_bytes":
        return _parse_bytes(value, key)
    if key == "redact_output":
        return _parse_bool(value, key)
    raise ValueError("unknown key")


def _env_bool(raw: str) -> bool:
    text = raw.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{ENV_ALLOW_NETWORK} must be true or false")


@dataclass(frozen=True)
class SecretConfig:
    """Effective ``[secrets]`` settings plus bounded diagnostics.

    ``sources`` maps each key to ``"legacy"``, ``"repo"``, ``"env"`` or
    ``"default"``. ``errors`` and ``warnings`` never contain resolved values;
    ``unknown`` is a sorted list of unsupported key names. A malformed legacy
    file is forgiving (its values are skipped); an invalid versioned value is
    reported in ``errors``.
    """

    providers: tuple[str, ...] = DEFAULT_PROVIDERS
    allow_cli: tuple[str, ...] = ()
    allow_network: bool = False
    allow_absolute_files: bool = False
    max_bytes: int = DEFAULT_MAX_BYTES
    redact_output: bool = True
    sources: dict[str, str] = field(default_factory=dict)
    files: dict[str, str | None] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        return self.unknown

    @classmethod
    def load(cls, root: str | Path = ".", *,
             environ: Mapping[str, str] | None = None) -> SecretConfig:
        root_p = Path(root).expanduser()
        try:
            root_p = root_p.resolve()
        except (OSError, RuntimeError):
            root_p = Path(root).expanduser()
        env = os.environ if environ is None else environ
        files: dict[str, str | None] = {
            "legacy": None,
            "repo": str(repo_config_path(root_p)),
        }
        errors: list[str] = []
        warnings: list[str] = []
        unknown: set[str] = set()
        try:
            section = load_config_section(root_p, SECRETS_SECTION)
        except (ConfigError, OSError, ValueError) as exc:
            return cls(
                files=files,
                errors=(f"secrets configuration: {exc}",),
                sources={key: "default" for key in SECRETS_KEYS},
            )
        files["legacy"] = str(section.legacy) if section.legacy is not None \
            else None
        files["repo"] = str(section.repo)
        for item in section.errors:
            # A TOML decoder message can quote the offending line; do not
            # copy it into diagnostics, where it could be a pasted value.
            errors.append(
                f"{item.path}: cannot read or parse the configuration file")

        validated: dict[str, dict[str, Any]] = {"repo": {}, "legacy": {}}
        for kind in ("repo", "legacy"):
            table = section.values_by_source.get(kind, {})
            path = section.repo if kind == "repo" else section.legacy
            for key, value in table.items():
                if key not in SECRETS_KEYS:
                    unknown.add(str(key))
                    warnings.append(
                        f"{path}: [secrets] ignores unknown key {key}")
                    continue
                try:
                    validated[kind][key] = _parse_secret_value(key, value)
                except (TypeError, ValueError) as exc:
                    errors.append(f"{path}: [secrets] {key}: {exc}")

        applied: dict[str, str] = {}
        values: dict[str, Any] = {
            "providers": DEFAULT_PROVIDERS,
            "allow_cli": (),
            "allow_network": False,
            "allow_absolute_files": False,
            "max_bytes": DEFAULT_MAX_BYTES,
            "redact_output": True,
        }
        for kind in ("repo", "legacy"):
            for key, value in validated[kind].items():
                values[key] = value
                applied[key] = kind

        raw_providers = env.get(ENV_PROVIDERS)
        if raw_providers is not None:
            try:
                values["providers"] = _parse_name_list(
                    [item for item in raw_providers.split(",") if item.strip()],
                    PROVIDER_NAMES, "providers")
                applied["providers"] = "env"
            except ValueError as exc:
                errors.append(f"{ENV_PROVIDERS}: {exc}")

        raw_network = env.get(ENV_ALLOW_NETWORK)
        if raw_network is not None:
            try:
                values["allow_network"] = _env_bool(raw_network)
                applied["allow_network"] = "env"
            except ValueError as exc:
                errors.append(str(exc))

        if values["max_bytes"] > MAX_CONFIG_BYTES:
            values["max_bytes"] = MAX_CONFIG_BYTES
            warnings.append(
                f"[secrets] max_bytes capped at {MAX_CONFIG_BYTES}")
        return cls(
            providers=tuple(values["providers"]),
            allow_cli=tuple(values["allow_cli"]),
            allow_network=bool(values["allow_network"]),
            allow_absolute_files=bool(values["allow_absolute_files"]),
            max_bytes=int(values["max_bytes"]),
            redact_output=bool(values["redact_output"]),
            sources={key: applied.get(key, "default") for key in SECRETS_KEYS},
            files=files,
            errors=tuple(errors),
            warnings=tuple(warnings),
            unknown=tuple(sorted(unknown)),
        )


def load_config(root: str | Path = ".", *,
                environ: Mapping[str, str] | None = None) -> SecretConfig:
    """Load the effective ``[secrets]`` settings (see :class:`SecretConfig`)."""
    return SecretConfig.load(root, environ=environ)


def load_secret_config(root: str | Path = ".", *,
                       environ: Mapping[str, str] | None = None
                       ) -> SecretConfig:
    """Alias for :func:`load_config` with an explicit name."""
    return SecretConfig.load(root, environ=environ)


def secrets_dir(root: str | Path, *,
                environ: Mapping[str, str] | None = None) -> Path:
    """The base directory for ``secret://file/NAME``.

    ``$OZAGIA_SECRETS_DIR`` (absolute, or relative to the root) overrides the
    state directory's ``secrets/`` subdirectory. The result is resolved but
    not created.
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_SECRETS_DIR)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(root) / path
        try:
            return path.resolve()
        except (OSError, RuntimeError):
            return path
    state = env.get("OZAGIA_STATE_DIR")
    if state:
        path = Path(state).expanduser()
        base = path if path.is_absolute() else Path(root) / path
    else:
        base = Path(root) / STATE_DIRNAME
    try:
        return (base / "secrets").resolve()
    except (OSError, RuntimeError):
        return base / "secrets"


# --------------------------------------------------------------------------- #
# status and discovery records
# --------------------------------------------------------------------------- #
class SecretCheck(str):
    """A status string with bounded, value-free details.

    The object compares equal to its status (``broker.check(ref) == "missing"``)
    and also exposes ``status``, ``reference``, ``provider``, ``code`` and
    ``detail``. It never holds a resolved value.
    """

    def __new__(cls, status: str, *, reference: str = "",
                provider: str | None = None, code: str | None = None,
                detail: str = "", ref: SecretRef | None = None
                ) -> SecretCheck:
        obj = super().__new__(cls, status)
        obj.reference = reference
        obj.provider = provider
        obj.code = code
        obj.detail = detail
        obj.ref = ref
        return obj

    @property
    def status(self) -> str:
        return str(self)

    @property
    def available(self) -> bool:
        return str(self) == STATUS_AVAILABLE

    @property
    def ok(self) -> bool:
        return self.available

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "status": str(self),
            "provider": self.provider,
            "code": self.code,
            "detail": self.detail,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __repr__(self) -> str:
        return (f"SecretCheck(status={str(self)!r}, "
                f"reference={self.reference!r}, code={self.code!r})")


@dataclass(frozen=True)
class DiscoveredRef:
    """One ``secret://`` string found in a configuration file.

    ``ref`` is the original reference text, ``parsed`` the validated
    :class:`SecretRef` (or None), ``key`` the dotted TOML key and ``source``
    the file. No value is read.
    """

    ref: str
    key: str
    source: Path
    parsed: SecretRef | None = None
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.parsed is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "key": self.key,
            "source": str(self.source),
            "valid": self.valid,
            "error": self.error,
        }

    @property
    def reference(self) -> str:
        return self.ref

    @property
    def secret_ref(self) -> SecretRef | None:
        return self.parsed

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def keys(self):
        return self.as_dict().keys()

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


SecretRefHit = DiscoveredRef


class ResolvedEnv:
    """A child environment with references resolved.

    ``values`` are actual strings for the child process. ``refs`` maps each
    key to its :class:`SecretRef`; ``plain_keys`` names values that were not
    references. ``repr`` shows counts and key names only.
    """

    def __init__(self, values: Mapping[str, str], refs: Mapping[str, SecretRef],
                 plain_keys: Iterable[str], redactor: Redactor) -> None:
        self.values = dict(values)
        self.refs = dict(refs)
        self.plain_keys = tuple(plain_keys)
        self.redactor = redactor

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def __len__(self) -> int:
        return len(self.values)

    def __repr__(self) -> str:
        return (f"ResolvedEnv(keys={len(self.values)}, "
                f"refs={len(self.refs)}, plain={len(self.plain_keys)})")


# --------------------------------------------------------------------------- #
# session-scoped captures (memory only)
# --------------------------------------------------------------------------- #
def _session_context(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value \
            or len(value) > 128 or not _PARAM_NAME.fullmatch(value):
        raise SecretError(f"{what} is invalid", code="invalid_session")
    return value


@dataclass(frozen=True, repr=False)
class _SessionEntry:
    """Internal entry; the value is never in ``repr``."""

    value: str
    fingerprint: str
    label: str | None
    created: float
    expires: float

    def __repr__(self) -> str:
        return (f"_SessionEntry(fingerprint={self.fingerprint!r}, "
                f"expires={self.expires!r})")


@dataclass(frozen=True)
class CapturedSecret:
    """Metadata for one in-memory session capture; it holds no value.

    ``ref`` is a ``secret://session/<token>`` reference safe to store, log or
    paste back into a chat. ``fingerprint`` identifies the value for rotation
    without revealing it. The value itself lives only in the
    :class:`SessionSecretStore`.
    """

    ref: str
    fingerprint: str
    label: str | None
    created: float
    expires: float

    @property
    def created_at(self) -> float:
        return self.created

    @property
    def expires_at(self) -> float:
        return self.expires

    @property
    def ttl_s(self) -> float:
        return max(0.0, self.expires - self.created)

    @property
    def key(self) -> str | None:
        """The caller's label/key for this capture."""
        return self.label

    @property
    def secret_ref(self) -> SecretRef:
        return parse_ref(self.ref)

    def __str__(self) -> str:
        return self.ref

    def __format__(self, spec: str) -> str:
        return format(self.ref, spec)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "fingerprint": self.fingerprint,
            "label": self.label,
            "created": self.created,
            "expires": self.expires,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def keys(self):
        return self.as_dict().keys()

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


class SessionSecretStore:
    """A bounded, in-memory store for secrets pasted into one chat session.

    Values never touch disk. Entries are scoped by ``(harness, session)`` and
    expire after a TTL (default one hour, capped at 24 hours). A reference is
    an opaque random ``secret://session/<token>``; it is not derived from the
    value and is safe to keep in transcripts, job records and model messages.

    Bounds: ``max_entries`` per session (default 32, oldest evicted first),
    ``max_value_bytes`` per value (default 16 KiB), and ``max_sessions``
    (default 64, oldest session evicted first). Expired entries are pruned on
    access. ``clock`` is injectable for tests.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 max_entries: int = SESSION_CAPTURE_MAX_ENTRIES,
                 max_value_bytes: int = SESSION_CAPTURE_MAX_BYTES,
                 default_ttl_s: float = SESSION_CAPTURE_TTL_S,
                 max_ttl_s: float = SESSION_CAPTURE_MAX_TTL_S,
                 max_sessions: int = SESSION_CAPTURE_MAX_SESSIONS) -> None:
        self._clock = clock or time.time
        self._max_entries = max(1, int(max_entries))
        self._max_value_bytes = max(1, int(max_value_bytes))
        self._max_ttl = max(1.0, min(float(max_ttl_s),
                                    SESSION_CAPTURE_MAX_TTL_S))
        self._default_ttl = max(1.0, min(float(default_ttl_s), self._max_ttl))
        self._max_sessions = max(1, int(max_sessions))
        self._sessions: OrderedDict[
            tuple[str, str], OrderedDict[str, _SessionEntry]] = OrderedDict()
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return sum(len(entries) for entries in self._sessions.values())

    def __repr__(self) -> str:
        with self._lock:
            return (f"SessionSecretStore(sessions={len(self._sessions)}, "
                    f"entries={sum(len(v) for v in self._sessions.values())})")

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "entries": sum(len(v) for v in self._sessions.values()),
                "max_entries_per_session": self._max_entries,
                "max_sessions": self._max_sessions,
                "max_value_bytes": self._max_value_bytes,
            }

    def _ttl(self, ttl_s: float | None) -> float:
        if ttl_s is None:
            return self._default_ttl
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)):
            raise SecretError("ttl must be a number of seconds",
                              code="invalid_ttl")
        if ttl_s <= 0:
            raise SecretError("ttl must be positive", code="invalid_ttl")
        return min(float(ttl_s), self._max_ttl)

    @staticmethod
    def _fingerprint(value: str) -> str:
        from ozagia.core.security import secret_fingerprint
        return secret_fingerprint(value)

    @staticmethod
    def _label(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 128 \
                or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise SecretError("label is invalid", code="invalid_label")
        return value

    def capture(self, value: str, *, harness: str, session: str,
                label: str | None = None, ttl_s: float | None = None
                ) -> CapturedSecret:
        """Capture one value in memory and return a safe reference.

        The value must be a non-empty string of at most ``max_value_bytes``
        UTF-8 bytes. The returned :class:`CapturedSecret` contains no value.
        """
        harness = _session_context(harness, "harness")
        session = _session_context(session, "session")
        label = self._label(label)
        ttl = self._ttl(ttl_s)
        if not isinstance(value, str):
            raise SecretError("captured value must be a string",
                              code="invalid_value")
        if not value:
            raise SecretError("captured value is empty", code="invalid_value")
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_bytes:
            raise SecretError("captured value is too large", code="too_large")
        now = float(self._clock())
        fingerprint = self._fingerprint(value)
        with self._lock:
            session_key = (harness, session)
            entries = self._sessions.get(session_key)
            if entries is None:
                entries = OrderedDict()
                self._sessions[session_key] = entries
            self._sessions.move_to_end(session_key)
            token = ""
            for _ in range(8):
                candidate = _stdlib_secrets.token_urlsafe(SESSION_TOKEN_BYTES)
                if candidate not in entries:
                    token = candidate
                    break
            if not token:
                raise SecretError("cannot allocate a session token",
                                  code="token_error")
            entry = _SessionEntry(
                value=value, fingerprint=fingerprint, label=label,
                created=now, expires=now + ttl)
            entries[token] = entry
            self._prune_session(entries, now)
            while len(entries) > self._max_entries:
                entries.popitem(last=False)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
            return CapturedSecret(
                ref=f"{REF_PREFIX}session/{token}",
                fingerprint=fingerprint, label=label,
                created=now, expires=now + ttl)

    def _prune_session(self, entries: OrderedDict[str, _SessionEntry],
                       now: float) -> int:
        expired = [token for token, entry in entries.items()
                   if entry.expires <= now]
        for token in expired:
            entries.pop(token, None)
        return len(expired)

    def prune(self) -> int:
        """Drop every expired capture across sessions; return the count."""
        now = float(self._clock())
        removed = 0
        with self._lock:
            for key, entries in list(self._sessions.items()):
                removed += self._prune_session(entries, now)
                if not entries:
                    self._sessions.pop(key, None)
        return removed

    @staticmethod
    def _coerce(ref: CapturedSecret | SecretRef | str) -> SecretRef:
        if isinstance(ref, CapturedSecret):
            ref = ref.ref
        if isinstance(ref, SecretRef):
            parsed = ref
        else:
            parsed = parse_ref(ref)
        if parsed.provider != "session":
            raise SecretRefError("not a session reference",
                                 code="not_session")
        return parsed

    def _lookup(self, ref: CapturedSecret | SecretRef | str, *,
                harness: str, session: str) -> tuple[
                    str, _SessionEntry | None, str | None]:
        parsed = self._coerce(ref)
        harness = _session_context(harness, "harness")
        session = _session_context(session, "session")
        token = parsed.segments[0]
        now = float(self._clock())
        with self._lock:
            entries = self._sessions.get((harness, session))
            if entries is not None:
                entry = entries.get(token)
                if entry is not None:
                    if entry.expires <= now:
                        entries.pop(token, None)
                        return "expired", None, token
                    return "found", entry, token
            for (other_harness, other_session), other in self._sessions.items():
                if (other_harness, other_session) == (harness, session):
                    continue
                if token in other:
                    return "mismatch", None, token
            return "missing", None, token

    def resolve(self, ref: CapturedSecret | SecretRef | str, *,
                harness: str | None, session: str | None) -> SecretValue:
        """Resolve a session reference for the matching session only."""
        if harness is None or session is None:
            raise SecretDenied("session context is required",
                               code="session_context_required")
        state, entry, _token = self._lookup(
            ref, harness=harness, session=session)
        if state == "expired":
            raise SecretNotFound("session secret expired", code="expired")
        if state == "mismatch":
            raise SecretDenied("session token belongs to another session",
                               code="session_mismatch")
        if state != "found" or entry is None:
            raise SecretNotFound("session secret not found", code="not_found")
        parsed = self._coerce(ref)
        return SecretValue(parsed, entry.value,
                           warnings=("session-scoped in-memory secret",))

    def status(self, ref: CapturedSecret | SecretRef | str, *,
               harness: str | None, session: str | None
               ) -> tuple[str, str | None, str]:
        """Status only; never returns or retains the value."""
        if harness is None or session is None:
            return (STATUS_DENIED, "session_context_required",
                    "session context is required")
        try:
            state, entry, _token = self._lookup(
                ref, harness=harness, session=session)
        except SecretRefError as exc:
            return STATUS_INVALID, exc.code, str(exc)
        if state == "expired":
            return STATUS_MISSING, "expired", "session secret expired"
        if state == "mismatch":
            return (STATUS_DENIED, "session_mismatch",
                    "session token belongs to another session")
        if state != "found" or entry is None:
            return STATUS_MISSING, "not_found", "session secret not found"
        return STATUS_AVAILABLE, None, "session secret"

    def list(self, harness: str, session: str) -> list[CapturedSecret]:
        """Captured references for one session, without values."""
        harness = _session_context(harness, "harness")
        session = _session_context(session, "session")
        now = float(self._clock())
        with self._lock:
            entries = self._sessions.get((harness, session))
            if entries is None:
                return []
            self._prune_session(entries, now)
            if not entries:
                self._sessions.pop((harness, session), None)
                return []
            return [
                CapturedSecret(
                    ref=f"{REF_PREFIX}session/{token}",
                    fingerprint=entry.fingerprint, label=entry.label,
                    created=entry.created, expires=entry.expires)
                for token, entry in entries.items()
            ]

    def forget(self, ref: CapturedSecret | SecretRef | str, *,
               harness: str | None, session: str | None) -> bool:
        """Forget one capture; returns whether it existed."""
        if harness is None or session is None:
            raise SecretDenied("session context is required",
                               code="session_context_required")
        try:
            parsed = self._coerce(ref)
        except SecretRefError:
            return False
        harness = _session_context(harness, "harness")
        session = _session_context(session, "session")
        with self._lock:
            entries = self._sessions.get((harness, session))
            if entries is None:
                return False
            existed = entries.pop(parsed.segments[0], None) is not None
            if not entries:
                self._sessions.pop((harness, session), None)
            return existed

    def clear(self, harness: str, session: str) -> int:
        """Drop every capture for one session; return the number removed."""
        harness = _session_context(harness, "harness")
        session = _session_context(session, "session")
        with self._lock:
            entries = self._sessions.pop((harness, session), None)
            return len(entries) if entries is not None else 0

    def describe(self) -> dict[str, Any]:
        """Bounded counts and limits; never a value or a token listing."""
        counts = self.counts()
        counts["default_ttl_s"] = self._default_ttl
        counts["max_ttl_s"] = self._max_ttl
        counts["storage"] = "memory-only"
        return counts


# --------------------------------------------------------------------------- #
# conservative pasted-text detection
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DetectedSecret:
    """Metadata for one detected secret; never the value."""

    fingerprint: str
    label: str
    kind: str
    start: int
    end: int
    ref: str | None = None
    captured: bool = True

    @property
    def key(self) -> str:
        """The assignment key or rule name for this detection."""
        return self.label

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "label": self.label,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "ref": self.ref,
            "captured": self.captured,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def keys(self):
        return self.as_dict().keys()

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)


@dataclass(frozen=True, repr=False)
class TextCapture:
    """A rewritten text plus value-free metadata.

    ``text`` has each detected value replaced by the caller's reference (or
    ``[REDACTED]``). ``secrets`` describes captured values only; ``detected``
    is the total and ``withheld`` counts detections that were redacted without
    capture (the cap, a missing factory or a factory failure). ``repr`` shows
    counts only, never the text.
    """

    text: str
    secrets: tuple[DetectedSecret, ...]
    detected: int
    withheld: int
    not_assessed: tuple[str, ...]
    truncated: bool = False

    @property
    def captured(self) -> int:
        return len(self.secrets)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "secrets": [item.as_dict() for item in self.secrets],
            "detected": self.detected,
            "captured": self.captured,
            "withheld": self.withheld,
            "truncated": self.truncated,
            "not_assessed": list(self.not_assessed),
        }

    def __repr__(self) -> str:
        return (f"TextCapture(detected={self.detected}, "
                f"captured={self.captured}, withheld={self.withheld}, "
                f"truncated={self.truncated})")


_DETECTION_CAVEATS = (
    "values already stored by the host or sent before this hook",
    "binary, encoded or unusual secret formats the patterns do not cover",
    "whether a detected value is live, revoked or a false positive",
)


def detect_text(text: str, *, max_secrets: int = DEFAULT_DETECT_LIMIT,
                ref_factory: Callable[[str, str], Any] | None = None,
                min_length: int = MIN_DETECTED_SECRET_LENGTH
                ) -> TextCapture:
    """Find conservative common secret shapes in pasted text and rewrite them.

    ``ref_factory(value, label)`` returns the replacement reference (a string
    or :class:`CapturedSecret`); the caller normally supplies
    ``lambda value, label: store.capture(value, harness=..., session=...,
    label=label).ref``. Without a factory, every detection is replaced with
    ``[REDACTED]`` and counted as withheld.

    The scan uses the security module's existing credential patterns plus its
    quoted assignment rule. Placeholders, obvious test canaries, short values
    and existing ``secret://`` references are left alone. ``max_secrets``
    bounds captured metadata; detections beyond it are still redacted. Text
    longer than ``MAX_DETECT_CHARS`` is scanned only up to that boundary and
    reported as truncated.
    """
    if not isinstance(text, str):
        raise SecretError("text must be a string", code="invalid_text")
    try:
        limit = int(max_secrets)
    except (TypeError, ValueError):
        limit = DEFAULT_DETECT_LIMIT
    limit = max(0, min(limit, MAX_DETECT_LIMIT))
    min_length = max(1, min(int(min_length), 1024))
    truncated = len(text) > MAX_DETECT_CHARS
    scan_text = text[:MAX_DETECT_CHARS] if truncated else text

    from ozagia.core import security as security_module

    ref_spans = [match.span() for match in _SECRET_REF_SPAN.finditer(scan_text)]
    candidates: dict[tuple[int, int], tuple[int, int, str, str, str]] = {}

    def add(start: int, end: int, value: str, label: str, kind: str) -> None:
        if start < 0 or end <= start or not value:
            return
        if end - start < min_length or len(value) < min_length:
            return
        if any(left < end and right > start for left, right in ref_spans):
            return
        if security_module._PLACEHOLDER.search(value):
            return
        if value.startswith(REF_PREFIX):
            return
        candidates.setdefault((start, end), (start, end, value, label, kind))

    for rule in security_module._SECRET_RULES:
        for match in rule.pattern.finditer(scan_text):
            group = rule.value_group or 0
            try:
                value = match.group(group)
                span = match.span(group)
            except (IndexError, KeyError):
                continue
            if not isinstance(value, str) or not value:
                continue
            if rule.value_group:
                # Generic assignments must look opaque, not like prose.
                if not security_module._looks_opaque(value):
                    continue
            label = rule.name
            groups = match.groupdict()
            if groups.get("label"):
                label = str(groups["label"]).lower()
            add(span[0], span[1], value, label, rule.kind)

    for match in security_module._SECRET_ASSIGN.finditer(scan_text):
        # The assignment pattern distinguishes quoted and bare branches so
        # capture_text can replace only the secret bytes while preserving the
        # surrounding syntax.
        group = "quoted" if match.group("quote") is not None else "bare"
        value = match.group(group)
        span = match.span(group)
        if not security_module._looks_opaque(value):
            continue
        add(span[0], span[1], value, str(match.group("label")).lower(),
            "assignment")

    ordered = sorted(candidates.values(),
                     key=lambda item: (item[0], -(item[1] - item[0]),
                                       item[3]))
    selected: list[tuple[int, int, str, str, str]] = []
    last_end = -1
    for item in ordered:
        if item[0] < last_end:
            continue
        selected.append(item)
        last_end = item[1]

    output: list[str] = []
    cursor = 0
    metadata: list[DetectedSecret] = []
    withheld = 0
    for start, end, value, label, kind in selected:
        output.append(scan_text[cursor:start])
        replacement: str | None = None
        if len(metadata) < limit and ref_factory is not None:
            try:
                produced = ref_factory(value, label)
                if isinstance(produced, CapturedSecret):
                    replacement = produced.ref
                elif isinstance(produced, SecretRef):
                    replacement = produced.original
                elif isinstance(produced, str) and produced:
                    replacement = produced
            except Exception:
                replacement = None
        if replacement is None:
            replacement = REDACTED
            withheld += 1
        if replacement != REDACTED and (replacement == value
                                        or value in replacement):
            replacement = REDACTED
            withheld += 1
        if replacement != REDACTED:
            metadata.append(DetectedSecret(
                fingerprint=SessionSecretStore._fingerprint(value),
                label=label, kind=kind, start=start, end=end,
                ref=replacement, captured=True))
        output.append(replacement)
        cursor = end
    output.append(scan_text[cursor:])
    rewritten = "".join(output)
    if truncated:
        rewritten += text[MAX_DETECT_CHARS:]
    caveats = list(_DETECTION_CAVEATS)
    if truncated:
        caveats.append(
            f"text longer than {MAX_DETECT_CHARS} characters was not scanned")
    return TextCapture(
        text=rewritten, secrets=tuple(metadata),
        detected=len(selected), withheld=withheld,
        not_assessed=tuple(caveats), truncated=truncated)


# --------------------------------------------------------------------------- #
# broker
# --------------------------------------------------------------------------- #
class SecretBroker:
    """Resolve references at an execution boundary and register redactions.

    Constructor injection keeps tests offline and deterministic:
    ``provider_overrides`` maps a provider name to ``callable(ref) -> str``;
    ``runner``, ``which`` and ``module_finder`` replace subprocess, executable
    lookup and optional imports. No provider is contacted unless a reference
    is resolved or checked.
    """

    def __init__(self, root: str | Path = ".", *,
                 config: SecretConfig | None = None,
                 environ: Mapping[str, str] | None = None,
                 runner: Callable[..., Any] | None = None,
                 which: Callable[[str], str | None] | None = None,
                 module_finder: Callable[[str], Any] | None = None,
                 provider_overrides: Mapping[
                     str, Callable[[SecretRef], Any]] | None = None,
                 provider_callables: Mapping[
                     str, Callable[[SecretRef], Any]] | None = None,
                 providers: Mapping[
                     str, Callable[[SecretRef], Any]] | None = None,
                 session_store: SessionSecretStore | None = None,
                 cache_size: int = MAX_CACHE_ENTRIES) -> None:
        root_p = Path(root).expanduser()
        try:
            root_p = root_p.resolve()
        except (OSError, RuntimeError):
            root_p = Path(root).expanduser()
        self.root = root_p
        self._environ = dict(os.environ if environ is None else environ)
        self.config = (config if config is not None
                       else SecretConfig.load(self.root, environ=self._environ))
        self._runner = runner
        self._which = which or shutil.which
        self._module_finder = module_finder or importlib.import_module
        self._module_finder_injected = module_finder is not None
        overrides: dict[str, Callable[[SecretRef], Any]] = {}
        for mapping in (provider_overrides, provider_callables, providers):
            if mapping:
                overrides.update(mapping)
        self._overrides = overrides
        self.session_store = (session_store if session_store is not None
                              else SessionSecretStore())
        self._cache_size = max(1, min(int(cache_size), 1024))
        self._cache: OrderedDict[tuple[str, tuple[str, ...]],
                                 SecretValue] = OrderedDict()
        self._warnings: list[str] = []
        self.redactor = Redactor()

    def __repr__(self) -> str:
        return (f"SecretBroker(root={str(self.root)!r}, "
                f"providers={self.config.providers!r})")

    @property
    def secrets_dir(self) -> Path:
        """The file provider base directory (not created)."""
        return secrets_dir(self.root, environ=self._environ)

    def redact(self, text: str) -> str:
        """Redact resolved values when ``[secrets] redact_output`` is on."""
        if not self.config.redact_output:
            return str(text)
        return self.redactor.redact(text)

    def redact_structure(self, value: Any) -> Any:
        """Recursively redact resolved values in a report-like structure."""
        if not self.config.redact_output:
            return value
        return self.redactor.redact_structure(value)

    # -- public API ------------------------------------------------------- #
    def resolve(self, ref: SecretRef | CapturedSecret | str, *,
                harness: str | None = None,
                session: str | None = None) -> SecretValue:
        """Resolve one reference; every value is registered for redaction.

        A ``secret://session/...`` reference requires both ``harness`` and
        ``session`` for the session that captured it. Session values are never
        cached, so expiry and revocation take effect on the next resolution.
        """
        parsed = self._coerce_ref(ref)
        self._require_enabled(parsed)
        cacheable = parsed.provider != "session"
        key = (parsed.provider, parsed.segments)
        if cacheable:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        try:
            value, warnings = self._dispatch(
                parsed, harness=harness, session=session)
        except SecretError:
            raise
        except Exception:
            raise SecretProviderError(
                "secret provider failed", code="provider_error") from None
        value = self._validate_value(value)
        secret = SecretValue(parsed, value, warnings=warnings)
        self.redactor.add(value)
        if cacheable:
            self._cache[key] = secret
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return secret

    def resolve_env(self, mapping: Mapping[str, str], *,
                    harness: str | None = None,
                    session: str | None = None) -> ResolvedEnv:
        """Resolve ``secret://`` values in an environment mapping.

        Non-reference values are kept as-is and reported in ``plain_keys``.
        Nothing is auto-detected as a secret and nothing is logged. Session
        references need the same ``harness`` and ``session`` as :meth:`resolve`.
        """
        if not isinstance(mapping, Mapping):
            raise SecretError("environment mapping must be a mapping",
                              code="invalid_env")
        values: dict[str, str] = {}
        refs: dict[str, SecretRef] = {}
        plain: list[str] = []
        for key, value in mapping.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key) \
                    or len(key) > MAX_ENV_NAME:
                raise SecretError("environment key is invalid",
                                  code="invalid_env_key")
            if not isinstance(value, str):
                raise SecretError("environment value must be a string",
                                  code="invalid_env_value")
            if "\x00" in value:
                raise SecretError("environment value contains NUL",
                                  code="invalid_env_value")
            if value.startswith(REF_PREFIX):
                secret = self.resolve(value, harness=harness, session=session)
                values[key] = secret.reveal()
                refs[key] = secret.ref
            else:
                values[key] = value
                plain.append(key)
        return ResolvedEnv(values, refs, plain, self.redactor)

    def check(self, ref: SecretRef | CapturedSecret | str, *,
              harness: str | None = None,
              session: str | None = None) -> SecretCheck:
        """Report one reference's status without returning its value."""
        reference = ref if isinstance(ref, str) else str(ref)
        try:
            parsed = self._coerce_ref(ref)
        except SecretRefError as exc:
            safe_reference = (reference if reference.startswith(REF_PREFIX)
                              else "(invalid reference)")
            return SecretCheck(
                STATUS_INVALID, reference=safe_reference, code=exc.code,
                detail=str(exc))
        if not self._provider_enabled(parsed):
            return SecretCheck(
                STATUS_DENIED, reference=parsed.original,
                provider=parsed.provider, code="provider_denied",
                detail="provider is not enabled", ref=parsed)
        gate = self._gate_error(parsed)
        if gate is not None:
            return SecretCheck(
                STATUS_DENIED, reference=parsed.original,
                provider=parsed.provider, code=gate.code,
                detail=str(gate), ref=parsed)
        try:
            status, code, detail = self._check_provider(
                parsed, harness=harness, session=session)
        except SecretError as exc:
            return SecretCheck(
                self._status_for(exc), reference=parsed.original,
                provider=parsed.provider, code=exc.code,
                detail=str(exc), ref=parsed)
        except Exception:
            return SecretCheck(
                STATUS_ERROR, reference=parsed.original,
                provider=parsed.provider, code="provider_error",
                detail="secret provider failed", ref=parsed)
        return SecretCheck(
            status, reference=parsed.original, provider=parsed.provider,
            code=code, detail=detail, ref=parsed)

    def check_many(self, refs: Iterable[SecretRef | CapturedSecret | str], *,
                   harness: str | None = None,
                   session: str | None = None) -> list[SecretCheck]:
        """Check several references; no resolved value is retained."""
        return [self.check(ref, harness=harness, session=session)
                for ref in refs]

    def providers(self) -> list[dict[str, Any]]:
        """Provider names, availability, requirements and notes (no values)."""
        result: list[dict[str, Any]] = []
        for name in PROVIDER_NAMES:
            enabled = name == "session" or name in self.config.providers
            available = self._provider_available(name)
            requires: list[str] = []
            notes = ""
            if name == "env":
                notes = "reads the process environment"
            elif name == "file":
                requires.append("OZAGIA_SECRETS_DIR or <state>/secrets")
                notes = "contained regular file; one trailing newline trimmed"
            elif name == "keyring":
                requires.append("optional keyring package")
                notes = "uses keyring.get_password; provider_unavailable if absent"
            elif name == "pass":
                requires.extend(["pass executable", "allow_cli includes pass"])
                notes = "runs pass show with a sanitized environment"
            elif name == "op":
                requires.extend(["op executable", "allow_cli includes op",
                                 "allow_network = true"])
                notes = "runs op read; network approval is required"
            elif name == "session":
                requires.append("harness and session context")
                notes = ("in-memory pasted-text capture; no disk; expires")
            ready = enabled and available and self._gate_error_name(name) is None
            result.append({
                "name": name,
                "enabled": enabled,
                "available": available,
                "ready": ready,
                "requires": requires,
                "requirements": requires,
                "notes": notes,
            })
        return result

    def describe(self) -> dict[str, Any]:
        """Configuration and provider status; never a resolved value."""
        return {
            "schema": SCHEMA,
            "root": str(self.root),
            "files": dict(self.config.files),
            "secrets_dir": str(self.secrets_dir),
            "providers": self.providers(),
            "sessions": self.session_store.describe(),
            "allow_cli": list(self.config.allow_cli),
            "allow_network": self.config.allow_network,
            "allow_absolute_files": self.config.allow_absolute_files,
            "max_bytes": self.config.max_bytes,
            "redact_output": self.config.redact_output,
            "sources": dict(self.config.sources),
            "errors": list(self.config.errors),
            "warnings": list(self.config.warnings) + list(self._warnings),
            "unknown": list(self.config.unknown),
        }

    def discover_refs(self, root: str | Path | None = None
                      ) -> list[DiscoveredRef]:
        """Find ``secret://`` strings in both configuration files.

        Returns reference text, dotted key and source path. Values are not
        read, and a malformed file is skipped with a bounded warning.
        """
        root_p = self.root if root is None else Path(root).expanduser()
        try:
            root_p = Path(root_p).resolve()
        except (OSError, RuntimeError):
            root_p = Path(root_p)
        try:
            sources = config_sources(root_p)
        except (ConfigError, OSError, ValueError) as exc:
            self._warn(f"cannot read configuration sources: {exc}")
            return []
        hits: list[DiscoveredRef] = []
        for path in sources.paths:
            try:
                size = path.stat().st_size
                if size > MAX_DISCOVERY_BYTES:
                    self._warn(f"configuration is too large to inspect: {path}")
                    continue
                raw = path.read_bytes()
                data = tomllib.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                self._warn(f"cannot parse configuration: {path}")
                continue
            self._walk_refs(data, (), path, hits, 0)
        return hits

    # -- internals -------------------------------------------------------- #
    def _warn(self, message: str) -> None:
        if len(self._warnings) < 32:
            self._warnings.append(str(message)[:256])

    @staticmethod
    def _status_for(exc: SecretError) -> str:
        if isinstance(exc, SecretNotFound):
            return STATUS_MISSING
        if isinstance(exc, SecretDenied):
            return STATUS_DENIED
        if isinstance(exc, SecretProviderUnavailable):
            return STATUS_PROVIDER_UNAVAILABLE
        if isinstance(exc, SecretRefError):
            return STATUS_INVALID
        return STATUS_ERROR

    def _coerce_ref(self, ref: SecretRef | CapturedSecret | str) -> SecretRef:
        if isinstance(ref, CapturedSecret):
            return ref.secret_ref
        if isinstance(ref, SecretRef):
            return ref
        if not isinstance(ref, str):
            raise SecretRefError("secret reference must be a string",
                                 code="invalid_ref")
        return parse_ref(ref, allow_absolute=self.config.allow_absolute_files)

    def _provider_enabled(self, ref: SecretRef) -> bool:
        return ref.provider == "session" or ref.provider in self.config.providers

    def _require_enabled(self, ref: SecretRef) -> None:
        if not self._provider_enabled(ref):
            raise SecretDenied("provider is not enabled",
                               code="provider_denied")
        error = self._gate_error(ref)
        if error is not None:
            raise error

    def _gate_error(self, ref: SecretRef) -> SecretDenied | None:
        if ref.provider in CLI_PROVIDERS \
                and ref.provider not in self.config.allow_cli:
            return SecretDenied("CLI provider is not allowed",
                                code="cli_not_allowed")
        if ref.provider == "op" and not self.config.allow_network:
            return SecretDenied("network approval is required",
                                code="network_denied")
        return None

    def _gate_error_name(self, name: str) -> SecretDenied | None:
        if name in CLI_PROVIDERS and name not in self.config.allow_cli:
            return SecretDenied("CLI provider is not allowed",
                                code="cli_not_allowed")
        if name == "op" and not self.config.allow_network:
            return SecretDenied("network approval is required",
                                code="network_denied")
        return None

    def _dispatch(self, ref: SecretRef, *,
                  harness: str | None = None,
                  session: str | None = None) -> tuple[str, tuple[str, ...]]:
        override = self._overrides.get(ref.provider)
        if override is not None:
            try:
                result = override(ref)
            except SecretError:
                raise
            except Exception:
                raise SecretProviderError(
                    "secret provider failed", code="provider_error") from None
            if isinstance(result, SecretValue):
                return result.reveal(), result.warnings
            if isinstance(result, tuple) and len(result) == 2:
                value, warnings = result
                return str(value), tuple(str(item) for item in warnings or ())
            return str(result), ()
        if ref.provider == "env":
            return self._resolve_env(ref)
        if ref.provider == "file":
            return self._resolve_file(ref)
        if ref.provider == "keyring":
            return self._resolve_keyring(ref)
        if ref.provider == "pass":
            return self._resolve_pass(ref)
        if ref.provider == "op":
            return self._resolve_op(ref)
        if ref.provider == "session":
            secret = self.session_store.resolve(
                ref, harness=harness, session=session)
            return secret.reveal(), secret.warnings
        raise SecretDenied("unknown secret provider", code="unknown_provider")

    @staticmethod
    def _validate_value(value: Any) -> str:
        if not isinstance(value, str):
            raise SecretProviderError("secret provider returned a non-string",
                                      code="invalid_value")
        if not value:
            raise SecretNotFound("secret value is empty", code="empty")
        if "\x00" in value:
            raise SecretProviderError("secret value contains NUL",
                                      code="invalid_value")
        if len(value.encode("utf-8", errors="replace")) > MAX_SECRET_BYTES:
            raise SecretProviderError("secret value is too large",
                                      code="too_large")
        return value

    # -- providers -------------------------------------------------------- #
    def _resolve_env(self, ref: SecretRef) -> tuple[str, tuple[str, ...]]:
        name = ref.segments[0]
        if name not in self._environ:
            raise SecretNotFound("environment variable is not set",
                                 code="missing")
        value = self._environ[name]
        if not isinstance(value, str) or not value:
            raise SecretNotFound("environment variable is empty",
                                 code="empty")
        return value, ()

    def _file_target(self, ref: SecretRef) -> Path:
        name = ref.segments[0]
        if name.startswith("/"):
            if not self.config.allow_absolute_files:
                raise SecretDenied("absolute file references are not allowed",
                                   code="absolute_denied")
            try:
                return Path(os.path.realpath(name))
            except (OSError, RuntimeError):
                raise SecretProviderError("cannot resolve secret file",
                                          code="provider_error") from None
        base = secrets_dir(self.root, environ=self._environ)
        try:
            base_real = Path(os.path.realpath(base))
            target = Path(os.path.realpath(Path(base) / name))
        except (OSError, RuntimeError):
            raise SecretProviderError("cannot resolve secret file",
                                      code="provider_error") from None
        try:
            within = os.path.commonpath(
                [str(base_real), str(target)]) == str(base_real)
        except ValueError:
            within = False
        if not within:
            raise SecretDenied("file escapes the secrets directory",
                               code="path_escape")
        return target

    @staticmethod
    def _strip_newline(data: bytes) -> bytes:
        if data.endswith(b"\n"):
            data = data[:-1]
            if data.endswith(b"\r"):
                data = data[:-1]
        return data

    def _resolve_file(self, ref: SecretRef) -> tuple[str, tuple[str, ...]]:
        target = self._file_target(ref)
        try:
            info = os.stat(target, follow_symlinks=True)
        except FileNotFoundError:
            raise SecretNotFound("secret file does not exist",
                                 code="missing") from None
        except OSError:
            raise SecretProviderError("cannot stat secret file",
                                      code="provider_error") from None
        if not stat.S_ISREG(info.st_mode):
            raise SecretProviderError("secret path is not a regular file",
                                      code="not_file")
        if info.st_size > self.config.max_bytes:
            raise SecretProviderError("secret file exceeds max_bytes",
                                      code="too_large")
        if info.st_size == 0:
            raise SecretNotFound("secret file is empty", code="empty")
        warnings: list[str] = []
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            warnings.append(
                f"secret file is group/world accessible (mode {mode:04o})")
        try:
            data = target.read_bytes()
        except OSError:
            raise SecretProviderError("cannot read secret file",
                                      code="provider_error") from None
        if len(data) > self.config.max_bytes:
            raise SecretProviderError("secret file exceeds max_bytes",
                                      code="too_large")
        data = self._strip_newline(data)
        if not data:
            raise SecretNotFound("secret file is empty", code="empty")
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretProviderError("secret file is not UTF-8",
                                      code="encoding") from None
        return value, tuple(warnings)

    def _keyring_module(self) -> Any | None:
        try:
            module = self._module_finder("keyring")
        except Exception:
            return None
        if module is None or not callable(getattr(module, "get_password", None)):
            return None
        return module

    def _resolve_keyring(self, ref: SecretRef
                         ) -> tuple[str, tuple[str, ...]]:
        module = self._keyring_module()
        if module is None:
            raise SecretProviderUnavailable(
                "keyring package is not installed", code="provider_unavailable")
        service, account = ref.segments
        try:
            value = module.get_password(service, account)
        except Exception:
            raise SecretProviderError("keyring lookup failed",
                                      code="provider_error") from None
        if not isinstance(value, str) or not value:
            raise SecretNotFound("keyring entry is missing", code="missing")
        return value, ()

    def _runner_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _RUNNER_ENV_KEYS:
            value = self._environ.get(key)
            if isinstance(value, str) and value:
                env[key] = value
        if "PATH" not in env:
            env["PATH"] = os.defpath
        return env

    @staticmethod
    def _strip_one_newline(text: str) -> str:
        if text.endswith("\n"):
            text = text[:-1]
            if text.endswith("\r"):
                text = text[:-1]
        return text

    def _run(self, argv: Sequence[str], *, timeout: float,
             provider: str) -> str:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "capture_output": True,
            "text": False,
            "timeout": timeout,
            "check": False,
            "shell": False,
            "env": self._runner_env(),
        }
        try:
            if self._runner is not None:
                try:
                    proc = self._runner(list(argv), **kwargs)
                except TypeError:
                    proc = self._runner(list(argv))
            else:
                proc = subprocess.run(list(argv), **kwargs)
        except subprocess.TimeoutExpired:
            raise SecretProviderError(f"{provider} timed out",
                                      code="timeout") from None
        except (OSError, subprocess.SubprocessError):
            raise SecretProviderError(f"{provider} could not run",
                                      code="provider_error") from None
        if isinstance(proc, str):
            return self._strip_one_newline(proc)
        if isinstance(proc, tuple) and len(proc) >= 2:
            returncode, stdout = proc[0], proc[1]
        else:
            returncode = getattr(proc, "returncode", 0)
            stdout = getattr(proc, "stdout", b"")
        if returncode != 0:
            # Never copy stdout/stderr: either may contain the secret.
            raise SecretProviderError(f"{provider} failed",
                                      code=f"{provider}_failed")
        if isinstance(stdout, bytes):
            try:
                text = stdout.decode("utf-8")
            except UnicodeDecodeError:
                raise SecretProviderError(
                    f"{provider} returned non-UTF-8 output",
                    code="encoding") from None
        elif isinstance(stdout, str):
            text = stdout
        elif stdout is None:
            text = ""
        else:
            raise SecretProviderError(f"{provider} returned invalid output",
                                      code="invalid_value")
        return self._strip_one_newline(text)

    def _resolve_pass(self, ref: SecretRef) -> tuple[str, tuple[str, ...]]:
        error = self._gate_error(ref)
        if error is not None:
            raise error
        binary = self._which("pass")
        if not binary:
            raise SecretProviderUnavailable("pass executable was not found",
                                            code="provider_unavailable")
        value = self._run([binary, "show", ref.path], timeout=5,
                          provider="pass")
        return value, ()

    def _resolve_op(self, ref: SecretRef) -> tuple[str, tuple[str, ...]]:
        error = self._gate_error(ref)
        if error is not None:
            raise error
        binary = self._which("op")
        if not binary:
            raise SecretProviderUnavailable("op executable was not found",
                                            code="provider_unavailable")
        vault, item, field = ref.segments
        value = self._run(
            [binary, "read", f"op://{vault}/{item}/{field}"],
            timeout=10, provider="op")
        return value, ()

    # -- status checks ---------------------------------------------------- #
    def _provider_available(self, name: str) -> bool:
        if name in ("env", "file"):
            return True
        if name == "session":
            return self.session_store is not None
        if name == "keyring":
            if self._module_finder_injected:
                return self._keyring_module() is not None
            try:
                return importlib.util.find_spec("keyring") is not None
            except (ImportError, ValueError, AttributeError):
                return False
        if name in ("pass", "op"):
            return bool(self._which(name))
        return False

    def _check_provider(self, ref: SecretRef, *,
                        harness: str | None = None,
                        session: str | None = None
                        ) -> tuple[str, str | None, str]:
        if ref.provider in self._overrides:
            return STATUS_AVAILABLE, None, "injected provider"
        if ref.provider == "env":
            name = ref.segments[0]
            if name not in self._environ:
                return STATUS_MISSING, "missing", "not set"
            if not self._environ[name]:
                return STATUS_MISSING, "empty", "value is empty"
            return STATUS_AVAILABLE, None, "set"
        if ref.provider == "file":
            target = self._file_target(ref)
            try:
                info = os.stat(target, follow_symlinks=True)
            except FileNotFoundError:
                return STATUS_MISSING, "missing", "file does not exist"
            except OSError:
                return STATUS_ERROR, "provider_error", "cannot stat file"
            if not stat.S_ISREG(info.st_mode):
                return STATUS_INVALID, "not_file", "not a regular file"
            if info.st_size > self.config.max_bytes:
                return STATUS_INVALID, "too_large", "file exceeds max_bytes"
            if info.st_size == 0:
                return STATUS_MISSING, "empty", "file is empty"
            mode = stat.S_IMODE(info.st_mode)
            detail = "regular file"
            if mode & 0o077:
                detail += f"; group/world accessible (mode {mode:04o})"
            return STATUS_AVAILABLE, None, detail
        if ref.provider == "keyring":
            module = self._keyring_module()
            if module is None:
                return (STATUS_PROVIDER_UNAVAILABLE, "provider_unavailable",
                        "keyring package is not installed")
            service, account = ref.segments
            try:
                value = module.get_password(service, account)
            except Exception:
                return STATUS_ERROR, "provider_error", "keyring lookup failed"
            if not isinstance(value, str) or not value:
                return STATUS_MISSING, "missing", "keyring entry is missing"
            return STATUS_AVAILABLE, None, "keyring entry exists"
        if ref.provider == "pass":
            if not self._which("pass"):
                return (STATUS_PROVIDER_UNAVAILABLE, "provider_unavailable",
                        "pass executable was not found")
            return (STATUS_AVAILABLE, None,
                    "provider available; presence is not probed")
        if ref.provider == "op":
            if not self._which("op"):
                return (STATUS_PROVIDER_UNAVAILABLE, "provider_unavailable",
                        "op executable was not found")
            return (STATUS_AVAILABLE, None,
                    "provider available; presence is not probed")
        if ref.provider == "session":
            return self.session_store.status(
                ref, harness=harness, session=session)
        return STATUS_INVALID, "unknown_provider", "unknown secret provider"

    def clear_cache(self) -> None:
        """Drop cached resolved values (rotation/revocation after a change)."""
        self._cache.clear()

    # -- discovery -------------------------------------------------------- #
    @staticmethod
    def _key_join(parts: Sequence[str], part: str) -> tuple[str, ...]:
        return tuple(parts) + (part,)

    @staticmethod
    def _dotted(parts: Sequence[str]) -> str:
        text = ""
        for part in parts:
            if part.startswith("["):
                text += part
            elif text:
                text += "." + part
            else:
                text = part
        return text

    def _walk_refs(self, value: Any, parts: tuple[str, ...], source: Path,
                   hits: list[DiscoveredRef], depth: int) -> None:
        if len(hits) >= MAX_DISCOVERED_REFS or depth > MAX_DISCOVERY_DEPTH:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if len(hits) >= MAX_DISCOVERED_REFS:
                    return
                self._walk_refs(
                    item, self._key_join(parts, str(key)), source, hits,
                    depth + 1)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                if len(hits) >= MAX_DISCOVERED_REFS:
                    return
                self._walk_refs(
                    item, self._key_join(parts, f"[{index}]"), source, hits,
                    depth + 1)
            return
        if not isinstance(value, str) or not value.startswith(REF_PREFIX):
            return
        parsed, error = try_parse_ref(
            value, allow_absolute=self.config.allow_absolute_files)
        hits.append(DiscoveredRef(
            ref=value, key=self._dotted(parts), source=source,
            parsed=parsed, error=error.code if error is not None else None))


def discover_refs(root: str | Path = ".", *,
                  config: SecretConfig | None = None,
                  environ: Mapping[str, str] | None = None
                  ) -> list[DiscoveredRef]:
    """Find ``secret://`` strings in both configuration files of ``root``."""
    broker = SecretBroker(root, config=config, environ=environ)
    return broker.discover_refs()
