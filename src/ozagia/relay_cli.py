"""Small JSON-spec and command helpers for the relay CLI.

The parser only describes chain shape and policy. Live execution still needs
the explicit ``allow_live``/``allow_network`` flags from the caller. No
credentials belong in a spec, and no command is run while loading one.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ozagia.core.relay import Relay, RelayPolicy, RelayRegistry, RelaySpec, TunnelTarget

MAX_SPEC_BYTES = 64 * 1024
_TOP_KEYS = {"specs", "policy"}
_SPEC_KEYS = {"kind", "options", "name"}
_POLICY_KEYS = {
    "allowed_kinds", "allow_live", "allow_network", "max_timeout_s",
    "command_allowlist", "max_output_bytes",
}


class RelaySpecFileError(ValueError):
    """A supplied relay chain document is malformed or unsafe."""


def load_document(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise RelaySpecFileError("relay spec exceeds the size limit")
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise RelaySpecFileError("relay spec is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RelaySpecFileError("relay spec must be a JSON object")
    unknown = set(value) - _TOP_KEYS
    if unknown:
        raise RelaySpecFileError("relay spec contains an unknown top-level field")
    specs = value.get("specs")
    if not isinstance(specs, list) or not specs or len(specs) > 32:
        raise RelaySpecFileError("relay spec needs between 1 and 32 chain entries")
    return value


def load_file(path: str | Path) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RelaySpecFileError("relay spec file could not be read") from exc
    return load_document(text)


def _mapping_options(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RelaySpecFileError("relay options must be an object")
    return dict(value)


def build_policy(document: Mapping[str, Any], *, allow_live: bool,
                 allow_network: bool) -> RelayPolicy:
    policy = document.get("policy") or {}
    if not isinstance(policy, dict):
        raise RelaySpecFileError("relay policy must be an object")
    unknown = set(policy) - _POLICY_KEYS
    if unknown:
        raise RelaySpecFileError("relay policy contains an unknown field")
    raw_allow = policy.get("allowed_kinds", [])
    if not isinstance(raw_allow, list) or any(
            not isinstance(item, str) for item in raw_allow):
        raise RelaySpecFileError("allowed_kinds must be a list of strings")
    command_allowlist = policy.get("command_allowlist")
    if command_allowlist is not None:
        if not isinstance(command_allowlist, list) or not command_allowlist:
            raise RelaySpecFileError("command_allowlist must be a non-empty list")
        converted = []
        for item in command_allowlist:
            if not isinstance(item, list) or not item or any(
                    not isinstance(part, str) for part in item):
                raise RelaySpecFileError("command_allowlist entries must be argv lists")
            converted.append(tuple(item))
        command_allowlist = tuple(converted)
    try:
        return RelayPolicy(
            allowed_kinds=frozenset(raw_allow),
            allow_live=bool(allow_live),
            allow_network=bool(allow_network),
            max_timeout_s=policy.get("max_timeout_s", 0),
            command_allowlist=command_allowlist,
            max_output_bytes=policy.get("max_output_bytes", 4096),
        )
    except (TypeError, ValueError) as exc:
        raise RelaySpecFileError("relay policy is invalid") from exc


def build_relay(document: Mapping[str, Any], *, allow_live: bool = False,
                allow_network: bool = False, ephemeral: bool = False,
                registry: RelayRegistry | None = None) -> Relay:
    raw_specs = document.get("specs")
    if not isinstance(raw_specs, list) or not raw_specs:
        raise RelaySpecFileError("relay spec has no chain entries")
    specs: list[RelaySpec] = []
    for raw in raw_specs:
        if not isinstance(raw, dict) or set(raw) - _SPEC_KEYS:
            raise RelaySpecFileError("relay chain entry has an unknown shape")
        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise RelaySpecFileError("relay chain entry needs a kind")
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise RelaySpecFileError("relay chain name must be text")
        specs.append(RelaySpec(kind, _mapping_options(raw.get("options")), name))
    policy = build_policy(document, allow_live=allow_live,
                          allow_network=allow_network)
    try:
        return Relay.from_specs(specs, registry=registry, policy=policy,
                                ephemeral=ephemeral)
    except (TypeError, ValueError) as exc:
        raise RelaySpecFileError("relay chain is invalid") from exc


def parse_argv(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(values)
    if not result:
        raise RelaySpecFileError("a command is required")
    return result


def parse_target(value: str) -> TunnelTarget:
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise RelaySpecFileError("target must be HOST:PORT")
    return TunnelTarget(host, int(port))


def read_stdin() -> str:
    return sys.stdin.read(MAX_SPEC_BYTES + 1)
