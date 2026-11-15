"""Opt-in registry for external research providers.

Ozagia ships only documented open metadata/API providers in ``fetch.py``. An
external package can register a provider here, but registration is not
enablement: ``fetch_preview`` and ``ref_resolve`` use an external provider only
when the caller names it. This is the arm's-length boundary for unofficial
services such as Google Scholar scrapers.

The environment variable ``OZAGIA_RESEARCH_PROVIDERS`` is an explicit opt-in:
a comma-separated list of ``module:attribute`` values. Each attribute must be
a provider mapping or a zero-argument callable returning one. Installed entry
points in the ``ozagia.research.providers`` group are also discovered, but
remain unused unless named.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import os
from typing import Any, Callable, Mapping

from ozagia.research.graph import ResearchError

CAPABILITIES = ("metadata", "search", "fulltext")
ENV_PROVIDERS = "OZAGIA_RESEARCH_PROVIDERS"
ENTRY_POINT_GROUP = "ozagia.research.providers"
_EXTERNAL: dict[str, dict[str, Any]] = {}
_LOADED = False


class ProviderError(ResearchError):
    """An external provider is missing, malformed or unsupported."""


def _normalize_provider(name: str, provider: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(provider, Mapping):
        raise ProviderError(f"provider {name!r} must be a mapping")
    capabilities = [
        capability for capability in CAPABILITIES if callable(provider.get(capability))
    ]
    if not capabilities:
        raise ProviderError(f"provider {name!r} has no metadata/search/fulltext callable")
    return {
        **dict(provider),
        "name": name,
        "capabilities": capabilities,
        "source": str(provider.get("source") or "external"),
        "unofficial": bool(provider.get("unofficial", True)),
    }


def register_provider(
    name: str,
    provider: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Register an external provider; it is still unused until named."""
    clean = str(name).strip()
    if not clean or any(char in clean for char in " \t\r\n:/"):
        raise ProviderError("provider name must be a simple non-empty identifier")
    if callable(provider):
        provider = provider()
    normalized = _normalize_provider(clean, provider)
    if clean in _EXTERNAL and not replace:
        raise ProviderError(f"provider already registered: {clean}")
    _EXTERNAL[clean] = normalized
    return normalized


def _load_env_providers() -> None:
    value = os.environ.get(ENV_PROVIDERS, "").strip()
    if not value:
        return
    for spec in value.split(","):
        spec = spec.strip()
        if not spec:
            continue
        module_name, separator, attribute = spec.partition(":")
        if not separator or not module_name or not attribute:
            raise ProviderError(
                f"{ENV_PROVIDERS} entries must be module:attribute, got {spec!r}")
        try:
            module = importlib.import_module(module_name)
            candidate = getattr(module, attribute)
        except (ImportError, AttributeError) as exc:
            raise ProviderError(f"cannot load provider {spec!r}: {exc}") from exc
        if callable(candidate) and not isinstance(candidate, Mapping):
            candidate = candidate()
        if isinstance(candidate, Mapping) and "name" in candidate:
            register_provider(str(candidate["name"]), candidate, replace=True)
        else:
            register_provider(attribute, candidate, replace=True)


def _load_entry_points() -> None:
    try:
        entries = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # Python 3.12+ returns a SelectableGroups-like object.
        entries = importlib.metadata.entry_points().select(group=ENTRY_POINT_GROUP)
    except Exception:  # metadata backends vary; discovery is best-effort.
        return
    for entry in entries:
        if entry.name in _EXTERNAL:
            continue
        try:
            candidate = entry.load()
            if callable(candidate) and not isinstance(candidate, Mapping):
                candidate = candidate()
            register_provider(entry.name, candidate, replace=False)
        except Exception:
            # A broken optional package must not break the default provider set.
            continue


def load_external_providers() -> dict[str, dict[str, Any]]:
    """Discover external providers once; discovery never enables one."""
    global _LOADED
    if not _LOADED:
        _LOADED = True
        _load_env_providers()
        _load_entry_points()
    return dict(_EXTERNAL)


def get_provider(name: str) -> dict[str, Any]:
    providers = load_external_providers()
    if name not in providers:
        raise ProviderError(
            f"external provider {name!r} is not registered; set {ENV_PROVIDERS} "
            "or install a package that registers it")
    return providers[name]


def provider_status() -> dict[str, Any]:
    providers = load_external_providers()
    return {
        "providers": [
            {
                "name": provider["name"],
                "capabilities": provider["capabilities"],
                "source": provider["source"],
                "unofficial": provider["unofficial"],
            }
            for provider in sorted(providers.values(), key=lambda item: item["name"])
        ],
        "enabled_by_default": [],
        "note": "External providers are used only when named by an operation.",
    }


def reset_providers() -> None:
    """Test helper: clear registrations and discovery state."""
    global _LOADED
    _EXTERNAL.clear()
    _LOADED = False
