"""Read-only resource grants: what a task may use, and whether it is ready.

A resource grant is a named entitlement declared in ``ozagia/resources.toml`` or
``ozagia/config.toml`` under ``[resource_grants.<id>]``.  It is separate from
host admission (``ozagia.core.resources``): admission answers whether a run fits
on this host now; a grant says which VM, host or cloud account a task may use,
what it may spend and how it must be cleaned up.

This module only loads, validates and checks grants.  It does not claim, approve,
release, write state or resolve secrets.  A check is read-only: the default path
runs no commands, makes no network call and never prints a credential.  Version
probes and live identity checks are opt-in and injected.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import tomllib

SCHEMA = 1
GRANT_KINDS = ("vm", "cloud", "host", "service", "container")
PROVIDERS = ("qemu", "libvirt", "aws", "azure", "gcp", "other")
PERMISSIONS = ("observer", "user", "admin")
CLEANUP_POLICIES = ("destroy", "keep", "none")
GRANT_STATES = ("ready", "missing", "unavailable", "requires_approval",
                "unknown")
# Reserved vocabulary for the claim/release stage.  No claim is created here.
CLAIM_STATES = ("active", "released", "expired", "orphaned")

MAX_ERRORS = 20
KVM_DEVICE = "/dev/kvm"
_QEMU_BINARIES = ("qemu-system-x86_64", "qemu-img")
_LIBVIRT_BINARIES = ("virsh", "libvirtd")
_VERSION_TIMEOUT_S = 5
_MAX_VERSION_CHARS = 200

_GRANT_FILES = (
    ("ozagia/resources.toml", "resource"),
    ("ozagia/config.toml", "config"),
)
_GRANT_FIELDS = frozenset({
    "kind", "provider", "description", "capabilities", "permissions", "image",
    "region", "profile", "constraints", "budget", "cleanup",
    "requires_approval", "risk",
})
_BUDGET_FIELDS = frozenset({"max_usd", "max_hours", "max_instances",
                            "unbounded"})
_CONSTRAINT_NUMBERS = frozenset({"max_hours", "max_disk_gb", "max_memory_gb",
                                 "max_cpus"})
_CONSTRAINT_INTS = frozenset({"max_instances"})
_CONSTRAINT_STRINGS = frozenset({"arch", "os", "gpu"})
_CONSTRAINT_BOOLS = frozenset({"nested_virt"})
_CONSTRAINT_FIELDS = (_CONSTRAINT_NUMBERS | _CONSTRAINT_INTS
                      | _CONSTRAINT_STRINGS | _CONSTRAINT_BOOLS)

_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_INI_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?$")
_SECRET_KEY_RE = re.compile(
    r"(?:secret|password|passwd|token|credential|access[_-]?key|"
    r"private[_-]?key|api[_-]?key|authorization|session[_-]?key)",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGN_RE = re.compile(
    r"(?i)\b(?:secret|password|passwd|token|credential|access[_-]?key|"
    r"private[_-]?key|api[_-]?key|authorization)\s*[:=]"
)
_KEY_VALUE_RE = re.compile(r"(?:^|[\s;,])([A-Za-z_][A-Za-z0-9_.-]*)\s*=")
_ARN_RE = re.compile(r"arn:(?:aws|azure|gcp):", re.IGNORECASE)
_SECRET_REF_RE = re.compile(r"secret://[A-Za-z0-9_.:/+-]{1,256}\Z")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:mongodb|postgres|postgresql|mysql|redis|amqp|https?)://"
               r"[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
)


class GrantError(ValueError):
    """A grant config or lookup is invalid."""


class GrantRefused(GrantError):
    """A grant is well-formed but is refused for this request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _label(name: object) -> str:
    """A field name safe to put in an error, never a credential-looking one."""
    text = str(name)
    if (_SECRET_KEY_RE.search(text) or len(text) > 64
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", text)
            or any(pattern.search(text) for pattern in _SECRET_VALUE_PATTERNS)):
        return "<redacted-key>"
    return text


def _check_text(value: object, *, where: str, required: bool = False,
                max_length: int = 2048) -> str:
    if not isinstance(value, str):
        raise GrantError(f"{where} must be a string")
    text = value.strip()
    if not text:
        if required:
            raise GrantError(f"{where} must not be empty")
        return text
    if len(text) > max_length:
        raise GrantError(f"{where} is too long")
    if _SECRET_REF_RE.fullmatch(text):
        return text
    if "secret://" in text:
        raise GrantError(f"{where}: a secret:// reference must be the whole value")
    if any(pattern.search(text) for pattern in _SECRET_VALUE_PATTERNS):
        raise GrantError(f"{where}: credential-like value rejected")
    if _ARN_RE.search(text):
        raise GrantError(f"{where}: ARNs are not stored in grants")
    if _CREDENTIAL_ASSIGN_RE.search(text) or _KEY_VALUE_RE.search(text):
        raise GrantError(
            f"{where}: inline credential or KEY=VALUE text is not allowed")
    return text


def _check_token(value: object, *, where: str) -> str:
    text = _check_text(value, where=where, required=True, max_length=64)
    if not _TOKEN_RE.fullmatch(text):
        raise GrantError(f"{where} must be a name ([A-Za-z][A-Za-z0-9_.-]*)")
    return text


def _string_list(value: object, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GrantError(f"{where} must be a list")
    out: list[str] = []
    for item in value:
        token = _check_token(item, where=where)
        if token not in out:
            out.append(token)
    return tuple(out)


def _positive_number(value: object, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrantError(f"{where} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise GrantError(f"{where} must be a positive finite number")
    return number


def _positive_int(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrantError(f"{where} must be an integer")
    if value <= 0:
        raise GrantError(f"{where} must be positive")
    return value


@dataclass(frozen=True)
class GrantBudget:
    """A declared spend/time/instance cap, or explicit unboundedness.

    ``unbounded`` cannot be combined with a finite cap.  A budget with no cap
    and ``unbounded = false`` declares no cap; it does not imply either a bound
    or unlimited spend.
    """

    max_usd: float | None = None
    max_hours: float | None = None
    max_instances: int | None = None
    unbounded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.unbounded, bool):
            raise GrantError("budget.unbounded must be true or false")
        caps = {
            "max_usd": self.max_usd,
            "max_hours": self.max_hours,
            "max_instances": self.max_instances,
        }
        for name, value in caps.items():
            if value is None:
                continue
            if name == "max_instances":
                _positive_int(value, where=f"budget.{name}")
            else:
                _positive_number(value, where=f"budget.{name}")
        if self.unbounded and any(value is not None for value in caps.values()):
            raise GrantError(
                "budget.unbounded cannot be combined with max_usd, max_hours "
                "or max_instances")

    @classmethod
    def from_mapping(cls, value: object, *,
                     where: str = "budget") -> GrantBudget | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise GrantError(f"{where} must be a table")
        extra = sorted(set(value) - _BUDGET_FIELDS)
        if extra:
            raise GrantError(
                f"{where}: unknown field(s): {', '.join(_label(x) for x in extra)}")
        unbounded = value.get("unbounded", False)
        if not isinstance(unbounded, bool):
            raise GrantError(f"{where}.unbounded must be true or false")
        max_usd = (None if value.get("max_usd") is None
                   else _positive_number(value["max_usd"],
                                         where=f"{where}.max_usd"))
        max_hours = (None if value.get("max_hours") is None
                     else _positive_number(value["max_hours"],
                                           where=f"{where}.max_hours"))
        max_instances = (
            None if value.get("max_instances") is None
            else _positive_int(value["max_instances"],
                               where=f"{where}.max_instances"))
        return cls(max_usd=max_usd, max_hours=max_hours,
                   max_instances=max_instances, unbounded=unbounded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_usd": self.max_usd,
            "max_hours": self.max_hours,
            "max_instances": self.max_instances,
            "unbounded": self.unbounded,
        }


@dataclass(frozen=True)
class ResourceGrant:
    """One validated resource grant.  Values are names, paths and references."""

    id: str
    kind: str
    provider: str
    description: str | None = None
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    image: str | None = None
    region: str | None = None
    profile: str | None = None
    constraints: Mapping[str, Any] = field(default_factory=dict)
    budget: GrantBudget | None = None
    cleanup: str = "destroy"
    requires_approval: bool = False
    risk: str | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider": self.provider,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "image": self.image,
            "region": self.region,
            "profile": self.profile,
            "constraints": dict(self.constraints),
            "budget": self.budget.to_dict() if self.budget is not None else None,
            "cleanup": self.cleanup,
            "requires_approval": self.requires_approval,
            "risk": self.risk,
            "source": self.source,
        }


def _parse_constraints(value: object, *,
                       where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GrantError(f"{where} must be a table")
    out: dict[str, Any] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or name not in _CONSTRAINT_FIELDS:
            raise GrantError(
                f"{where}: unknown constraint {_label(name)!r}")
        field_where = f"{where}.{name}"
        if name in _CONSTRAINT_NUMBERS:
            out[name] = _positive_number(raw, where=field_where)
        elif name in _CONSTRAINT_INTS:
            out[name] = _positive_int(raw, where=field_where)
        elif name in _CONSTRAINT_BOOLS:
            if not isinstance(raw, bool):
                raise GrantError(f"{field_where} must be true or false")
            out[name] = raw
        else:
            out[name] = _check_text(raw, where=field_where, required=True,
                                    max_length=128)
    return out


def _parse_grant(grant_id: object, raw: object, *, source: str) -> ResourceGrant:
    if not isinstance(grant_id, str) or not _ID_RE.fullmatch(grant_id):
        raise GrantError(f"{source}: invalid resource grant id {_label(grant_id)!r}")
    where = f"resource_grants.{grant_id}"
    if not isinstance(raw, Mapping):
        raise GrantError(f"{where} must be a table")
    extra = sorted(set(raw) - _GRANT_FIELDS)
    if extra:
        raise GrantError(
            f"{where}: unknown field(s): {', '.join(_label(x) for x in extra)}")

    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in GRANT_KINDS:
        raise GrantError(f"{where}.kind must be one of {', '.join(GRANT_KINDS)}")
    provider = raw.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise GrantError(
            f"{where}.provider must be one of {', '.join(PROVIDERS)}")

    description = raw.get("description")
    if description is not None:
        description = _check_text(description, where=f"{where}.description",
                                  max_length=2000)
    risk = raw.get("risk")
    if risk is not None:
        risk = _check_text(risk, where=f"{where}.risk", max_length=2000)
    image = raw.get("image")
    if image is not None:
        image = _check_text(image, where=f"{where}.image", required=True,
                            max_length=1024)
    region = raw.get("region")
    if region is not None:
        region = _check_text(region, where=f"{where}.region", required=True,
                             max_length=128)
    profile = raw.get("profile")
    if profile is not None:
        profile = _check_text(profile, where=f"{where}.profile", required=True,
                              max_length=128)

    capabilities = _string_list(raw.get("capabilities"),
                                where=f"{where}.capabilities")
    permissions = _string_list(raw.get("permissions"),
                               where=f"{where}.permissions")
    for permission in permissions:
        if permission not in PERMISSIONS:
            raise GrantError(
                f"{where}.permissions must be one of {', '.join(PERMISSIONS)}")
    constraints = _parse_constraints(raw.get("constraints"),
                                     where=f"{where}.constraints")
    budget = GrantBudget.from_mapping(raw.get("budget"),
                                      where=f"{where}.budget")

    cleanup = raw.get("cleanup")
    if cleanup is None:
        cleanup = "none" if kind == "host" else "destroy"
    if not isinstance(cleanup, str) or cleanup not in CLEANUP_POLICIES:
        raise GrantError(
            f"{where}.cleanup must be one of {', '.join(CLEANUP_POLICIES)}")
    if kind == "cloud" and cleanup == "none":
        raise GrantError(
            f"{where}.cleanup: cloud grants require destroy or keep")

    requires_approval = raw.get("requires_approval", False)
    if not isinstance(requires_approval, bool):
        raise GrantError(f"{where}.requires_approval must be true or false")
    forced = "admin" in permissions or bool(budget and budget.unbounded)
    if forced:
        requires_approval = True
    if cleanup == "keep":
        if not requires_approval:
            raise GrantError(
                f"{where}.cleanup: keep requires requires_approval = true")
        if not risk:
            raise GrantError(f"{where}.cleanup: keep requires a risk note")

    if kind == "vm" and not image:
        raise GrantError(f"{where}.image is required for kind vm")
    if kind == "host" and not capabilities:
        raise GrantError(
            f"{where}.capabilities must declare a host capability (for "
            "example kvm)")

    return ResourceGrant(
        id=grant_id, kind=kind, provider=provider,
        description=description, capabilities=capabilities,
        permissions=permissions, image=image, region=region, profile=profile,
        constraints=constraints, budget=budget, cleanup=cleanup,
        requires_approval=requires_approval, risk=risk, source=source,
    )


def _load_grants(root: str | Path, *,
                 errors: list[str] | None = None) -> list[ResourceGrant]:
    root = Path(root).resolve()
    seen: dict[str, str] = {}
    grants: list[ResourceGrant] = []

    def record(message: str) -> None:
        if errors is None:
            raise GrantError(message)
        errors.append(message)

    for relative, _owner in _GRANT_FILES:
        path = root / relative
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            record(f"{relative}: {exc}")
            continue
        table = data.get("resource_grants")
        if table is None:
            continue
        if not isinstance(table, Mapping):
            record(f"{relative}: [resource_grants] must be a table")
            continue
        for grant_id, raw in table.items():
            try:
                grant = _parse_grant(grant_id, raw, source=relative)
            except GrantError as exc:
                record(f"{relative}: {exc}")
                continue
            previous = seen.get(grant.id)
            if previous is not None:
                record(f"duplicate resource grant id {grant.id!r} in "
                       f"{previous} and {relative}")
                continue
            seen[grant.id] = relative
            grants.append(grant)
    grants.sort(key=lambda item: item.id)
    return grants


def _bounded_errors(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_ERRORS:
        return list(errors)
    shown = errors[:MAX_ERRORS - 1]
    shown.append(f"{len(errors) - MAX_ERRORS + 1} more error(s) omitted")
    return shown


def load_grants(root: str | Path) -> list[ResourceGrant]:
    """Load and validate every grant from the two configuration files.

    A malformed grant or file is a typed :class:`GrantError`.
    """
    return _load_grants(root)


def list_grants(root: str | Path, kind: str | None = None) -> dict[str, Any]:
    """Return a read-only grant list, reporting bounded config errors.

    Unlike :func:`load_grants`, one malformed record does not hide the valid
    ones.  The result is ``{"schema", "count", "grants", "errors"}``.
    """
    if kind is not None and kind not in GRANT_KINDS:
        raise GrantError(f"kind must be one of {', '.join(GRANT_KINDS)}")
    errors: list[str] = []
    grants = _load_grants(root, errors=errors)
    items = [grant.to_dict() for grant in grants
             if kind is None or grant.kind == kind]
    return {
        "schema": SCHEMA,
        "count": len(items),
        "grants": items,
        "errors": _bounded_errors(errors),
    }


def show_grant(root: str | Path, id: str) -> ResourceGrant:
    """One validated grant, or a typed error when it is absent or malformed."""
    for grant in load_grants(root):
        if grant.id == id:
            return grant
    raise GrantError(f"unknown resource grant {_label(id)!r}")


def _add_check(checks: list[dict[str, Any]], check_id: str, level: str,
               message: str, detail: Mapping[str, Any] | None = None,
               *, state: str | None = None) -> None:
    row = {
        "id": check_id,
        "level": level,
        "message": message,
        "detail": dict(detail or {}),
    }
    if state is not None:
        row["detail"]["state"] = state
    checks.append(row)


def _approval_checks(grant: ResourceGrant, *, approval_granted: bool,
                     unbounded_allowed: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if grant.requires_approval:
        if approval_granted:
            _add_check(checks, "approval.required", "ok",
                       "approval was supplied", {"required": True})
        else:
            _add_check(checks, "approval.required", "error",
                       "this grant requires human approval",
                       {"required": True}, state="requires_approval")
    if grant.budget is not None and grant.budget.unbounded:
        if unbounded_allowed:
            _add_check(checks, "approval.unbounded", "ok",
                       "unbounded use was explicitly allowed",
                       {"unbounded": True})
        else:
            _add_check(checks, "approval.unbounded", "error",
                       "an unbounded budget requires explicit allowance",
                       {"unbounded": True}, state="requires_approval")
    return checks


def _grant_checks(grant: ResourceGrant) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    permissions = list(grant.permissions)
    _add_check(checks, "permissions.declared", "info",
               "permissions are declarative and were not verified",
               {"permissions": permissions, "verified": False})
    if "admin" in permissions:
        _add_check(checks, "permissions.admin", "warning",
                   "admin permission is declared but not verified",
                   {"permission": "admin", "verified": False})
    if grant.budget is None:
        _add_check(checks, "budget.declared", "warning",
                   "no budget is declared; spend is not bounded by this grant")
    elif grant.budget.unbounded:
        _add_check(checks, "budget.declared", "warning",
                   "the budget is unbounded",
                   grant.budget.to_dict())
    else:
        _add_check(checks, "budget.declared", "info",
                   "budget caps are declared", grant.budget.to_dict())
    if grant.cleanup == "keep":
        _add_check(checks, "cleanup.declared", "warning",
                   "cleanup keeps resources and needs a later approval",
                   {"cleanup": "keep"})
    elif grant.cleanup == "none":
        _add_check(checks, "cleanup.declared", "info",
                   "no cleanup is declared", {"cleanup": "none"})
    else:
        _add_check(checks, "cleanup.declared", "ok",
                   "cleanup destroys resources", {"cleanup": "destroy"})
    return checks


def _kvm_check(checks: list[dict[str, Any]], *, path_exists: Callable[[str], bool],
               access: Callable[[str, int], bool]) -> None:
    if not path_exists(KVM_DEVICE):
        _add_check(checks, "kvm.device", "error",
                   "KVM device is not present", {"path": KVM_DEVICE},
                   state="missing")
        return
    if not access(KVM_DEVICE, os.R_OK | os.W_OK):
        _add_check(checks, "kvm.device", "error",
                   "KVM device is present but not readable and writable",
                   {"path": KVM_DEVICE}, state="unavailable")
        return
    _add_check(checks, "kvm.device", "ok",
               "KVM device is readable and writable", {"path": KVM_DEVICE})


def _binary_checks(checks: list[dict[str, Any]], binaries: tuple[str, ...],
                   required: set[str], *,
                   which: Callable[[str], str | None]) -> None:
    for binary in binaries:
        found = which(binary)
        if found:
            _add_check(checks, f"executable.{binary}", "ok",
                       f"{binary} was found", {"binary": binary,
                                               "path": found,
                                               "required": binary in required})
        elif binary in required:
            _add_check(checks, f"executable.{binary}", "error",
                       f"{binary} is required but was not found",
                       {"binary": binary, "required": True}, state="missing")
        else:
            _add_check(checks, f"executable.{binary}", "info",
                       f"optional {binary} was not found",
                       {"binary": binary, "required": False})


def _image_check(checks: list[dict[str, Any]], grant: ResourceGrant, root: Path,
                 *, path_exists: Callable[[str], bool],
                 access: Callable[[str, int], bool]) -> Path | None:
    if not grant.image:
        return None
    if grant.image.startswith("secret://"):
        _add_check(checks, "image.path", "info",
                   "the image is a secret reference and was not resolved",
                   {"secret_reference": True}, state="unknown")
        return None
    path = Path(grant.image).expanduser()
    if not path.is_absolute():
        path = root / path
    text = str(path)
    if not path_exists(text):
        _add_check(checks, "image.path", "error",
                   "the declared image does not exist",
                   {"path": text, "declared": grant.image}, state="missing")
        return path
    if not access(text, os.R_OK):
        _add_check(checks, "image.path", "error",
                   "the declared image is not readable", {"path": text},
                   state="unavailable")
        return path
    try:
        info = os.stat(path)
    except OSError as exc:
        _add_check(checks, "image.path", "error",
                   "the declared image could not be inspected",
                   {"path": text, "error": type(exc).__name__},
                   state="unavailable")
        return path
    if not stat.S_ISREG(info.st_mode):
        _add_check(checks, "image.path", "error",
                   "the declared image is not a regular file",
                   {"path": text}, state="unavailable")
        return path
    if info.st_size <= 0:
        _add_check(checks, "image.path", "error",
                   "the declared image is empty", {"path": text},
                   state="unavailable")
        return path
    _add_check(checks, "image.path", "ok",
               "the declared image is a readable non-empty file",
               {"path": text, "size_bytes": info.st_size})
    return path


def _nested_virt_checks(checks: list[dict[str, Any]], *,
                        path_exists: Callable[[str], bool]) -> None:
    for module in ("kvm_intel", "kvm_amd"):
        path = f"/sys/module/{module}/parameters/nested"
        if not path_exists(path):
            continue
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            _add_check(checks, "nested_virt", "info",
                       "nested-virtualisation evidence could not be read",
                       {"source": path, "value": "unknown",
                        "error": type(exc).__name__})
            return
        if value in {"Y", "1"}:
            answer = "yes"
        elif value in {"N", "0"}:
            answer = "no"
        else:
            answer = "unknown"
        _add_check(checks, "nested_virt", "info",
                   f"nested virtualisation reports {answer}",
                   {"source": path, "value": answer})
        return
    _add_check(checks, "nested_virt", "info",
               "nested-virtualisation evidence is not available",
               {"value": "unknown"})


def _version_check(checks: list[dict[str, Any]], binary: str, path: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "LANG": "C",
    }
    try:
        done = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=_VERSION_TIMEOUT_S, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _add_check(checks, f"version.{binary}", "warning",
                   f"{binary} version probe failed",
                   {"binary": binary, "error": type(exc).__name__})
        return
    text = (done.stdout or done.stderr or "").strip().splitlines()
    version = text[0][:_MAX_VERSION_CHARS] if text else ""
    if done.returncode != 0:
        _add_check(checks, f"version.{binary}", "warning",
                   f"{binary} version probe returned {done.returncode}",
                   {"binary": binary, "exit_code": done.returncode})
        return
    _add_check(checks, f"version.{binary}", "ok",
               f"{binary} version was read",
               {"binary": binary, "version": version})


def _admission_checks(grant: ResourceGrant, root: Path,
                      image_path: Path | None, *,
                      admission: Callable[..., Mapping[str, Any]] | None
                      ) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    constraints = grant.constraints
    memory_gb = constraints.get("max_memory_gb")
    disk_gb = constraints.get("max_disk_gb")
    if memory_gb is None and disk_gb is None:
        _add_check(checks, "admission", "info",
                   "no memory/disk constraint is declared; host admission was "
                   "not requested")
        return checks
    memory_mb = float(memory_gb) * 1024 if memory_gb is not None else 0.0
    disk_mb = float(disk_gb) * 1024 if disk_gb is not None else 0.0
    check = admission
    if check is None:
        from ozagia.core import resources as R
        check = R.check_admission
    elif not callable(check):
        raise GrantError("admission must be callable")
    path = image_path or root
    try:
        report = check(root, kind="job", memory_mb=memory_mb,
                       disk_mb=disk_mb, path=str(path))
    except Exception as exc:  # A probe failure is a report, not a crash.
        _add_check(checks, "admission", "error",
                   "the admission check failed",
                   {"error": type(exc).__name__}, state="unavailable")
        return checks
    if not isinstance(report, Mapping):
        _add_check(checks, "admission", "error",
                   "the admission check returned an invalid report",
                   {"error": "invalid_report"}, state="unavailable")
        return checks
    if report.get("disabled"):
        _add_check(checks, "admission", "info",
                   "resource admission is disabled")
        return checks
    if report.get("ok") is False:
        _add_check(
            checks, "admission", "error",
            "the declared request does not fit on this host",
            {"reason": report.get("reason"),
             "requested": report.get("requested"),
             "shortfall": report.get("shortfall")},
            state="unavailable")
        return checks
    _add_check(checks, "admission", "ok",
               "the declared memory/disk request fits on this host",
               {"requested": report.get("requested")})
    return checks


def _local_checks(grant: ResourceGrant, root: Path, *,
                  which: Callable[[str], str | None],
                  path_exists: Callable[[str], bool],
                  access: Callable[[str, int], bool],
                  admission: Callable[..., Mapping[str, Any]] | None,
                  probe_versions: bool) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if grant.kind == "vm":
        capabilities = {item.lower() for item in grant.capabilities}
        required = set(_QEMU_BINARIES)
        if grant.provider == "libvirt" or "libvirt" in capabilities:
            required |= set(_LIBVIRT_BINARIES)
        _kvm_check(checks, path_exists=path_exists, access=access)
        _binary_checks(checks, _QEMU_BINARIES + _LIBVIRT_BINARIES, required,
                       which=which)
        image_path = _image_check(checks, grant, root,
                                  path_exists=path_exists, access=access)
        _nested_virt_checks(checks, path_exists=path_exists)
        if probe_versions:
            for binary in ("qemu-system-x86_64", "virsh"):
                found = which(binary)
                if found:
                    _version_check(checks, binary, found)
        checks.extend(_admission_checks(grant, root, image_path,
                                        admission=admission))
        return checks

    if grant.kind == "host":
        capabilities = {item.lower() for item in grant.capabilities}
        _add_check(checks, "capability.declared", "info",
                   "host capabilities are declared",
                   {"capabilities": list(grant.capabilities)})
        needs_kvm = bool(capabilities & {"kvm", "virtualization", "vm"})
        needs_libvirt = "libvirt" in capabilities
        required: set[str] = set()
        if needs_kvm:
            required.add("qemu-system-x86_64")
            _kvm_check(checks, path_exists=path_exists, access=access)
            _nested_virt_checks(checks, path_exists=path_exists)
        if needs_libvirt:
            required |= set(_LIBVIRT_BINARIES)
        if not needs_kvm and not needs_libvirt:
            _add_check(checks, "capability.probe", "info",
                       "no local probe is defined for the declared capability",
                       {"capabilities": list(grant.capabilities)},
                       state="unknown")
        _binary_checks(checks, _QEMU_BINARIES + _LIBVIRT_BINARIES, required,
                       which=which)
        if probe_versions:
            for binary in ("qemu-system-x86_64", "virsh"):
                found = which(binary)
                if found:
                    _version_check(checks, binary, found)
        checks.extend(_admission_checks(grant, root, None,
                                        admission=admission))
        return checks

    _add_check(checks, "kind.probe", "info",
               f"no readiness probe is defined for kind {grant.kind}",
               {"kind": grant.kind}, state="unknown")
    return checks


def _aws_files() -> list[tuple[str, Path]]:
    home = Path.home()
    config = Path(os.environ.get("AWS_CONFIG_FILE") or home / ".aws/config")
    credentials = Path(os.environ.get(
        "AWS_SHARED_CREDENTIALS_FILE") or home / ".aws/credentials")
    return [("config", config), ("credentials", credentials)]


def _section_names(path: Path) -> set[str]:
    names: set[str] = set()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _INI_SECTION_RE.match(line)
            if not match:
                continue
            name = match.group(1).strip()
            if name.lower().startswith("profile "):
                name = name[8:].strip()
            if name:
                names.add(name)
    return names


def _aws_checks(grant: ResourceGrant, *,
                path_exists: Callable[[str], bool]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if grant.region and grant.region.startswith("secret://"):
        _add_check(checks, "cloud.region", "info",
                   "the region is a secret reference and was not resolved",
                   {"secret_reference": True})
    elif grant.region:
        _add_check(checks, "cloud.region", "ok", "a region is declared",
                   {"region": grant.region})
    else:
        _add_check(checks, "cloud.region", "warning",
                   "no region is declared; the provider default is used")
    sections: dict[str, set[str]] = {}
    files = _aws_files()
    for label, path in files:
        if not path_exists(str(path)):
            continue
        try:
            sections[label] = _section_names(path)
        except OSError as exc:
            _add_check(checks, "cloud.config", "warning",
                       f"the {label} file could not be read",
                       {"file": label, "error": type(exc).__name__})
    if sections:
        _add_check(checks, "cloud.config", "ok",
                   "AWS config/credentials file(s) were found",
                   {"files": sorted(sections)})
    else:
        _add_check(checks, "cloud.config", "info",
                   "no AWS config or credentials file was found",
                   {"files": [label for label, _path in files]})
    if grant.profile and grant.profile.startswith("secret://"):
        _add_check(checks, "cloud.profile", "info",
                   "the profile is a secret reference and was not resolved",
                   {"secret_reference": True}, state="unknown")
    elif not grant.profile:
        _add_check(checks, "cloud.profile", "info",
                   "no profile is declared; the provider default chain is used")
    else:
        found = sorted(label for label, names in sections.items()
                       if grant.profile in names)
        if found:
            _add_check(checks, "cloud.profile", "ok",
                       "the declared profile section was found",
                       {"profile": grant.profile, "found_in": found})
        else:
            _add_check(checks, "cloud.profile", "error",
                       "the declared profile section was not found",
                       {"profile": grant.profile,
                        "files_checked": sorted(sections) or
                        [label for label, _path in files]},
                       state="unavailable")
    return checks


def _cloud_checks(grant: ResourceGrant, *,
                  live: bool,
                  which: Callable[[str], str | None],
                  path_exists: Callable[[str], bool],
                  live_runner: Callable[[ResourceGrant], Any] | None
                  ) -> tuple[list[dict[str, Any]], bool, bool, bool | None]:
    checks: list[dict[str, Any]] = []
    binary = {"aws": "aws", "azure": "az", "gcp": "gcloud"}.get(grant.provider)
    if binary:
        found = which(binary)
        if found:
            _add_check(checks, "cloud.cli", "ok",
                       f"{binary} was found",
                       {"binary": binary, "path": found})
        else:
            _add_check(checks, "cloud.cli", "error",
                       f"{binary} is required but was not found",
                       {"binary": binary}, state="missing")
    else:
        _add_check(checks, "cloud.cli", "info",
                   "no provider CLI probe is defined")

    if grant.provider == "aws":
        checks.extend(_aws_checks(grant, path_exists=path_exists))
    else:
        _add_check(checks, "cloud.config", "info",
                   "provider-specific config checks are not implemented in "
                   "this stage")

    live_checked = False
    network = False
    identity_present: bool | None = None
    if live:
        if live_runner is None:
            _add_check(checks, "live.runner_required", "error",
                       "live checking requires an injected runner; no network "
                       "call was made", {"network": False},
                       state="unavailable")
        else:
            live_checked = True
            network = True
            try:
                result = live_runner(grant)
            except Exception as exc:  # Never echo provider/account details.
                _add_check(checks, "live.identity", "error",
                           "the live identity check failed",
                           {"error": type(exc).__name__},
                           state="unavailable")
            else:
                if not isinstance(result, Mapping):
                    _add_check(checks, "live.identity", "error",
                               "the live identity check returned an invalid "
                               "result", state="unavailable")
                else:
                    identity_present = bool(result.get("identity_present"))
                    if identity_present:
                        _add_check(checks, "live.identity", "ok",
                                   "the provider reported an identity; its "
                                   "value was not retained",
                                   {"identity_present": True})
                    else:
                        _add_check(checks, "live.identity", "error",
                                   "the provider did not report an identity",
                                   {"identity_present": False},
                                   state="unavailable")
    else:
        _add_check(checks, "live.identity", "info",
                   "no live identity check was requested")
    return checks, live_checked, network, identity_present


def check_grant(
        root: str | Path, id: str, *,
        live: bool = False,
        probe_versions: bool = False,
        approval_granted: bool = False,
        unbounded_allowed: bool = False,
        which: Callable[[str], str | None] | None = None,
        path_exists: Callable[[str], bool] | None = None,
        access: Callable[[str, int], bool] | None = None,
        admission: Callable[..., Mapping[str, Any]] | None = None,
        live_runner: Callable[[ResourceGrant], Any] | None = None,
) -> dict[str, Any]:
    """Check one grant's local readiness without claiming or running anything.

    The default is offline and non-executing.  ``probe_versions`` opts into a
    bounded ``--version`` run for a found qemu/virsh binary.  ``live`` only uses
    the injected ``live_runner``; without one the report is ``unavailable`` and
    no network call happens.  Permissions remain declarative and
    ``permissions_verified`` is always false in this stage.
    """
    root = Path(root).resolve()
    grant = show_grant(root, id)
    which = shutil.which if which is None else which
    path_exists = os.path.exists if path_exists is None else path_exists
    access = os.access if access is None else access
    checks = _grant_checks(grant)
    checks.extend(_approval_checks(
        grant, approval_granted=approval_granted,
        unbounded_allowed=unbounded_allowed))
    live_checked = False
    network = False
    identity_present: bool | None = None

    if grant.kind == "cloud":
        cloud_checks, live_checked, network, identity_present = _cloud_checks(
            grant, live=live, which=which, path_exists=path_exists,
            live_runner=live_runner)
        checks.extend(cloud_checks)
    else:
        checks.extend(_local_checks(
            grant, root, which=which, path_exists=path_exists, access=access,
            admission=admission, probe_versions=probe_versions))

    states = {row["detail"].get("state") for row in checks}
    if "requires_approval" in states:
        state = "requires_approval"
    elif "missing" in states:
        state = "missing"
    elif "unavailable" in states:
        state = "unavailable"
    elif "unknown" in states:
        state = "unknown"
    else:
        state = "ready"
    if live and grant.kind == "cloud" and live_runner is None:
        # A requested live check without a runner is unavailable even when a
        # local prerequisite is also missing; the live gate is what refused.
        if state != "requires_approval":
            state = "unavailable"

    not_assessed = ["cleanup", "permissions", "spend"]
    if grant.kind == "vm":
        not_assessed.append("image_format")
    if grant.kind == "cloud":
        not_assessed.append("cloud_credentials")
        if not live_checked:
            not_assessed.append("cloud_identity")
    if not live:
        not_assessed.append("live_network")

    report = {
        "schema": SCHEMA,
        "grant": grant.to_dict(),
        "id": grant.id,
        "kind": grant.kind,
        "provider": grant.provider,
        "state": state,
        "ready": state == "ready",
        "permissions": list(grant.permissions),
        "permissions_verified": False,
        "budget": grant.budget.to_dict() if grant.budget is not None else None,
        "cleanup": grant.cleanup,
        "checks": checks,
        "live": bool(live),
        "live_checked": live_checked,
        "identity_present": identity_present,
        "checked_at": _now(),
        "source": grant.source,
        "provenance": {
            "network": bool(network),
            "wrote": False,
            "secrets_resolved": False,
        },
        "not_assessed": sorted(dict.fromkeys(not_assessed)),
        "approval": {
            "required": grant.requires_approval,
            "granted": bool(approval_granted),
            "unbounded_allowed": bool(unbounded_allowed),
        },
    }
    return report
