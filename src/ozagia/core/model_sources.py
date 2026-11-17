"""Cached model metadata sources for the advisory model graph.

The model graph in :mod:`ozagia.core.model_graph` is built from the local
model registry plus curated profiles. This module adds *explicitly cached*
external metadata: models.dev, LiteLLM's price list, OpenRouter and Epoch AI.

Nothing here runs automatically. ``graph``, ``match`` and the CLI read cache
files only; a network request happens only when a caller runs ``sync`` with
``allow_network=True``. Tests inject a transport and never contact the
network. No provider credential is read or stored.

Cache layout::

    <state>/models/<source>.json|.csv
    <state>/models/<source>.meta.json

The data file is a byte-for-byte snapshot. The metadata records the source,
URL, licence, SHA-256, size, fetch time, format and staleness window. Writes
are atomic (temporary file then ``os.replace``).

Precedence when several caches are merged is the registry order::

    models-dev > litellm > openrouter > epoch

Fields are filled independently: the first source with a usable value wins and
later sources fill the gaps. Epoch is enrichment-only; it adds parameter
counts and training-compute notes where a model name matches plausibly, and
never invents an id or a number. An explicit ``[model_graph.models]`` override
still wins over every cached source.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ozagia.config import repo_root, state_path
from ozagia.core import models as M
from ozagia.core.model_graph import CAPABILITIES, MODALITIES, TYPES

SCHEMA = 1
CACHE_DIRNAME = "models"
USER_AGENT = "ozagia-models/0.1 (+https://github.com/ozmalabs/ozagia)"
DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 600.0
MAX_BYTES_LIMIT = 64 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_TEXT = 500
MAX_NOTE_PARTS = 24

DEFAULT_ORDER = ("models-dev", "litellm", "openrouter", "epoch")

#: Fields accepted by ``model_graph._parse_external``. ``to_external`` emits
#: only these names; anything else is metadata for reports, not the graph.
EXTERNAL_FIELDS = (
    "family", "variant", "type", "size_class", "params_total_b",
    "params_active_b", "context", "input_cost", "output_cost",
    "modalities_in", "modalities_out", "capabilities", "tool_formats",
    "local", "source", "confidence", "notes", "profile_source",
)

_STATUS_OK = "ok"
_STATUS_STALE = "stale"
_STATUS_MISSING = "missing"
_STATUS_MALFORMED = "malformed"
_STATUS_FUTURE = "future"

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_JSON_TYPES = frozenset({
    "application/json", "text/json", "text/plain", "application/octet-stream",
})
_CSV_TYPES = frozenset({
    "text/csv", "application/csv", "text/plain", "application/octet-stream",
})

_TYPE_ALIASES = {
    "chat": "chat",
    "completion": "chat",
    "language": "chat",
    "text": "chat",
    "decision": "chat",
    "reasoning": "reasoning",
    "code": "code",
    "multimodal": "multimodal",
    "embedding": "embedding",
    "embeddings": "embedding",
    "image": "image",
    "image_generation": "image",
    "image-generation": "image",
    "audio": "audio",
    "audio_speech": "audio",
    "audio_transcription": "audio",
    "speech": "audio",
    "transcription": "audio",
    "realtime": "audio",
    "video": "image",
}
_MODALITY_ALIASES = {
    "text": "text",
    "image": "image",
    "images": "image",
    "vision": "image",
    "pdf": "pdf",
    "file": "pdf",
    "files": "pdf",
    "document": "pdf",
    "documents": "pdf",
    "audio": "audio",
    "video": "video",
    "embedding": "embedding",
    "embeddings": "embedding",
}
_CAPABILITY_ALIASES = {
    "tools": "tools",
    "tool": "tools",
    "tool_call": "tools",
    "tool_calls": "tools",
    "function_calling": "tools",
    "function_call": "tools",
    "vision": "vision",
    "audio": "audio",
    "pdf_native": "pdf_native",
    "pdf": "pdf_native",
    "pdf_input": "pdf_native",
    "native_pdf": "pdf_native",
    "long_context": "long_context",
    "structured_output": "structured_output",
    "structured_outputs": "structured_output",
    "response_schema": "structured_output",
    "computer_use": "computer_use",
    "code": "code",
    "reasoning": "reasoning",
}
_OPEN_WEIGHTS_TRUE = frozenset({"yes", "true", "1", "open", "open weights"})
_OPEN_WEIGHTS_FALSE = frozenset({"no", "false", "0", "closed", "proprietary"})
_NUMBER_RE = re.compile(r"^[~≈<>]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)(?:[eE]([+-]?\d+))?\s*([bBmMkKtT]?)$")


class SourceError(ValueError):
    """A source, cache or payload operation failed safely.

    ``reason`` is a short stable code for reports; the message stays readable.
    """

    def __init__(self, reason: str, message: str, *, source: str | None = None,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.source = source
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "source": self.source,
            "status": "refused" if self.reason == "network_not_allowed" else "error",
            "reason": self.reason,
            "error": str(self),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Source:
    """One metadata source and its acquisition policy."""

    id: str
    title: str
    url: str
    license: str
    attribution: str
    format: str
    max_bytes: int
    stale_after_s: int
    description: str
    fields: tuple[str, ...]
    role: str = "active"
    fallback_urls: tuple[str, ...] = ()
    notes: str = ""

    @property
    def fetchable(self) -> bool:
        return self.role in {"active", "primary"}

    @property
    def primary(self) -> bool:
        return self.role == "primary"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "fallback_urls": list(self.fallback_urls),
            "license": self.license,
            "attribution": self.attribution,
            "format": self.format,
            "max_bytes": self.max_bytes,
            "stale_after_s": self.stale_after_s,
            "description": self.description,
            "fields": list(self.fields),
            "role": self.role,
            "fetchable": self.fetchable,
            "primary": self.primary,
            "notes": self.notes,
        }


SOURCES: tuple[Source, ...] = (
    Source(
        id="models-dev",
        title="models.dev catalog",
        url="https://models.dev/catalog.json?type=all",
        fallback_urls=("https://models.dev/api.json?type=all",),
        license="MIT",
        attribution="models.dev contributors",
        format="json",
        max_bytes=32 * 1024 * 1024,
        stale_after_s=7 * 24 * 60 * 60,
        description=(
            "Primary catalogue: family, open weights, release and knowledge "
            "dates, limits, provider pricing, modalities and capabilities. It "
            "does not normally publish parameter counts."),
        fields=(
            "family", "type", "open_weights", "release_date", "knowledge",
            "context", "output_limit", "input_cost", "output_cost",
            "modalities_in", "modalities_out", "capabilities",
        ),
        role="primary",
    ),
    Source(
        id="litellm",
        title="LiteLLM model prices and context",
        url=("https://raw.githubusercontent.com/BerriAI/litellm/main/"
             "model_prices_and_context_window.json"),
        license="MIT",
        attribution="BerriAI LiteLLM contributors",
        format="json",
        max_bytes=16 * 1024 * 1024,
        stale_after_s=14 * 24 * 60 * 60,
        description=(
            "Broad fallback for context/output limits, per-token prices, mode "
            "and capability flags. Covers providers and aliases models.dev "
            "omits."),
        fields=(
            "type", "context", "output_limit", "input_cost", "output_cost",
            "modalities_in", "modalities_out", "capabilities", "notes",
        ),
    ),
    Source(
        id="openrouter",
        title="OpenRouter models",
        url="https://openrouter.ai/api/v1/models?output_modalities=all",
        license="OpenRouter public API",
        attribution="OpenRouter",
        format="json",
        max_bytes=16 * 1024 * 1024,
        stale_after_s=7 * 24 * 60 * 60,
        description=(
            "Optional cross-check: context length, input/output modalities, "
            "provider-specific per-token pricing, supported parameters, "
            "output cap, knowledge cutoff and release/expiry dates."),
        fields=(
            "context", "output_limit", "input_cost", "output_cost",
            "modalities_in", "modalities_out", "capabilities",
            "release_date", "knowledge",
        ),
        notes="Prices are the listed OpenRouter route, not necessarily the "
              "first-party provider price.",
    ),
    Source(
        id="epoch",
        title="Epoch AI models",
        url="https://epoch.ai/data/all_ai_models.csv",
        license="CC BY 4.0",
        attribution=(
            "Epoch AI, CC BY 4.0. Cite Epoch AI when using these parameter "
            "and training-compute facts."),
        format="csv",
        max_bytes=8 * 1024 * 1024,
        stale_after_s=30 * 24 * 60 * 60,
        description=(
            "Historical parameter counts and training-compute facts. Used "
            "only to enrich a model whose id or normalized name matches "
            "plausibly; it is not a current catalogue."),
        fields=(
            "params_total_b", "release_date", "open_weights",
            "training_compute",
        ),
        notes="Historical snapshot; matching is conservative and may leave "
              "models unenriched.",
    ),
    Source(
        id="huggingface",
        title="Hugging Face model metadata (future)",
        url="https://huggingface.co/api/models",
        license="Variable by model",
        attribution="Hugging Face model authors",
        format="json",
        max_bytes=8 * 1024 * 1024,
        stale_after_s=30 * 24 * 60 * 60,
        description=(
            "Future source for open-weight architecture and parameter "
            "metadata. Not fetched or imported in this slice."),
        fields=("params_total_b", "params_active_b", "context", "family"),
        role="future",
        notes="not_assessed in this slice; no adapter and no cache.",
    ),
)

SOURCES_BY_ID: dict[str, Source] = {source.id: source for source in SOURCES}


def get_source(source_id: str) -> Source:
    """Return a registered source, or raise a structured ``SourceError``."""
    if not isinstance(source_id, str) or not source_id.strip():
        raise SourceError("unknown_source", "source id must be a non-empty string")
    source = SOURCES_BY_ID.get(source_id.strip())
    if source is None:
        known = ", ".join(SOURCES_BY_ID)
        raise SourceError(
            "unknown_source", f"unknown source {source_id!r}; known: {known}",
            source=source_id)
    return source


def source_ids() -> tuple[str, ...]:
    return tuple(source.id for source in SOURCES)


# --------------------------------------------------------------------------- #
# normalized metadata
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelMetadata:
    """One model's normalized external metadata.

    Costs are USD per million tokens. ``input_limit`` is retained separately
    from ``context`` in case a source reports a prompt limit different from the
    advertised context window. ``to_external`` is the only graph-facing view.
    """

    id: str
    provider: str
    canonical_id: str | None = None
    family: str | None = None
    variant: str | None = None
    type: str | None = None
    size_class: str | None = None
    open_weights: bool | None = None
    context: int | None = None
    input_limit: int | None = None
    output_limit: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    modalities_in: tuple[str, ...] = ()
    modalities_out: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    tool_formats: tuple[str, ...] = ()
    local: bool | None = None
    release_date: str | None = None
    knowledge: str | None = None
    params_total_b: float | None = None
    params_active_b: float | None = None
    source: str = ""
    source_url: str = ""
    confidence: str = "external"
    notes: str = ""
    profile_source: str = ""

    def to_external(self) -> dict[str, Any]:
        """MAP to the fields ``model_graph._parse_external`` accepts.

        Empty lists and unknown values are omitted, never turned into guesses.
        """
        values: dict[str, Any] = {
            "family": self.family,
            "variant": self.variant,
            "type": self.type,
            "size_class": self.size_class,
            "params_total_b": self.params_total_b,
            "params_active_b": self.params_active_b,
            "context": self.context if self.context is not None
            else self.input_limit,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "modalities_in": list(self.modalities_in),
            "modalities_out": list(self.modalities_out),
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
            "local": self.local,
            "source": self.source or None,
            "confidence": self.confidence,
            "notes": self.notes or None,
            "profile_source": (self.profile_source
                               or (f"{self.source} cached metadata"
                                   if self.source else None)),
        }
        result: dict[str, Any] = {}
        for key in EXTERNAL_FIELDS:
            value = values.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple)) and not value:
                continue
            result[key] = value
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "canonical_id": self.canonical_id,
            "family": self.family,
            "variant": self.variant,
            "type": self.type,
            "size_class": self.size_class,
            "open_weights": self.open_weights,
            "context": self.context,
            "input_limit": self.input_limit,
            "output_limit": self.output_limit,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "modalities_in": list(self.modalities_in),
            "modalities_out": list(self.modalities_out),
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
            "local": self.local,
            "release_date": self.release_date,
            "knowledge": self.knowledge,
            "params_total_b": self.params_total_b,
            "params_active_b": self.params_active_b,
            "source": self.source,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "notes": self.notes,
            "profile_source": self.profile_source,
        }


# --------------------------------------------------------------------------- #
# small validation helpers
# --------------------------------------------------------------------------- #
def _text(value: Any, *, max_len: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = "".join(char for char in text
                   if ord(char) >= 32 and ord(char) != 127)
    text = text.strip()
    return text[:max_len] if text else None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
    return None


def _number(value: Any, *, maximum: float = 100_000.0) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return number


def _integer(value: Any, *, maximum: int = 100_000_000) -> int | None:
    number = _number(value, maximum=float(maximum))
    if number is None:
        return None
    return int(number) if number.is_integer() else None


def _per_million(value: Any) -> float | None:
    number = _number(value, maximum=1_000.0)
    if number is None:
        return None
    return round(number * 1_000_000.0, 6)


def _model_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return M.validate_model_id(value.strip())
    except ValueError:
        return None


def _provider_model(provider: Any, model: Any) -> str | None:
    provider_text = _text(provider, max_len=128)
    model_text = _text(model, max_len=200)
    if not provider_text or not model_text:
        return None
    candidate = model_text if "/" in model_text else (
        f"{provider_text}/{model_text}")
    return _model_id(candidate)


def _date(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if 100_000_000 <= number <= 4_000_000_000:
            return datetime.fromtimestamp(number, tz=timezone.utc).date().isoformat()
        return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if not text:
            return None
        if re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", text):
            return text
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.date().isoformat()
    return None


def _modalities(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        items = [part.strip() for part in re.split(r"[,;|]", values)]
    elif isinstance(values, Mapping):
        items = [key for key, value in values.items() if value]
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        return ()
    result: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        key = item.strip().lower().replace("-", "_").replace(" ", "_")
        mapped = _MODALITY_ALIASES.get(key)
        if mapped in MODALITIES:
            result.add(mapped)
    return tuple(sorted(result))


def _capabilities(values: Any) -> tuple[str, ...]:
    if isinstance(values, Mapping):
        items = [key for key, value in values.items() if value is True]
    elif isinstance(values, str):
        items = [part.strip() for part in re.split(r"[,;|]", values)]
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        return ()
    result: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        key = item.strip().lower().replace("-", "_").replace(" ", "_")
        mapped = _CAPABILITY_ALIASES.get(key)
        if mapped in CAPABILITIES:
            result.add(mapped)
    return tuple(sorted(result))


def _type(value: Any, notes: list[str]) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace("-", "_")
    mapped = _TYPE_ALIASES.get(key)
    if mapped in TYPES:
        if key == "decision" and "decision" not in TYPES:
            notes.append("models.dev type decision mapped to chat")
        return mapped
    return None


def _join_notes(parts: Iterable[str | None]) -> str:
    values: list[str] = []
    for part in parts:
        text = _text(part)
        if text and text not in values:
            values.append(text)
    text = "; ".join(values)
    return text[:MAX_TEXT]


def _apply_flags(capabilities: set[str], data: Mapping[str, Any],
                 flags: Mapping[str, tuple[str, str]]) -> None:
    for key, (capability, _label) in flags.items():
        if key in data and _boolean(data.get(key)) is True:
            capabilities.add(capability)


# --------------------------------------------------------------------------- #
# source adapters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParseResult:
    """The normalized result of one source snapshot."""

    models: tuple[ModelMetadata, ...] = ()
    epoch: EpochIndex | None = None
    warnings: tuple[str, ...] = ()
    shape: str = ""

    def to_external(self) -> dict[str, dict[str, Any]]:
        external: dict[str, dict[str, Any]] = {}
        # Primary provider/model ids win over aliases from another provider's
        # canonical reference. Aliases fill only absent ids.
        for model in self.models:
            fields = model.to_external()
            if fields and model.id not in external:
                external[model.id] = dict(fields)
        for model in self.models:
            if not model.canonical_id or model.canonical_id in external:
                continue
            fields = model.to_external()
            if fields:
                external[model.canonical_id] = dict(fields)
        return external

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": len(self.models),
            "epoch_rows": (len(self.epoch.entries)
                           if self.epoch is not None else 0),
            "shape": self.shape,
            "warnings": list(self.warnings),
        }


def _models_dev_models(root: Mapping[str, Any]) -> tuple[
        list[tuple[str, Mapping[str, Any]]], Mapping[str, Any] | None, str]:
    """Return provider entries, canonical model map and a shape label."""
    providers = root.get("providers")
    if isinstance(providers, Mapping):
        entries = [
            (str(provider_id), provider)
            for provider_id, provider in providers.items()
            if isinstance(provider, Mapping)
        ]
        canonical = root.get("models")
        return entries, (canonical if isinstance(canonical, Mapping) else None), \
            "providers"

    models = root.get("models")
    if isinstance(models, Mapping) and any(
            key in root for key in ("id", "name", "api", "npm", "env")):
        provider_id = _text(root.get("id"), max_len=128) or _text(
            root.get("name"), max_len=128)
        provider = {**root, "id": provider_id} if provider_id else root
        return ([(provider_id, provider)] if provider_id else []), None, \
            "provider"

    provider_entries = [
        (str(provider_id), provider)
        for provider_id, provider in root.items()
        if isinstance(provider, Mapping)
        and isinstance(provider.get("models"), Mapping)
    ]
    if provider_entries:
        normalized: list[tuple[str, Mapping[str, Any]]] = []
        for provider_id, provider in provider_entries:
            nested = _text(provider.get("id"), max_len=128)
            # A wrapper key such as "provider" is not an id when the object
            # names itself; ordinary provider maps keep their key.
            use_nested = provider_id in {"", "provider", "providers"}
            normalized.append(((nested if use_nested else None) or provider_id,
                               provider))
        canonical = root.get("models")
        return normalized, (
            canonical if isinstance(canonical, Mapping) else None), "provider-map"

    if isinstance(models, Mapping):
        # Flat model map: a top-level key is already provider/model, or the
        # entry itself names its provider. No provider is invented.
        return [("", {"models": models})], None, "models"
    return [], None, "unknown"


def _merge_models_dev(canonical: Mapping[str, Any] | None,
                      endpoint: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(canonical or {})
    for key, value in endpoint.items():
        if (key in {"limit", "cost", "modalities", "capabilities"}
                and isinstance(value, Mapping)
                and isinstance(merged.get(key), Mapping)):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _models_dev_model(
        provider_id: str, model_key: str, data: Mapping[str, Any],
        canonical: Mapping[str, Any] | None, source: Source
) -> ModelMetadata | None:
    if provider_id:
        # models.dev provider entries are keyed by the provider's own model
        # name; a key may itself contain a slash. Always keep the provider
        # prefix so one provider's alias cannot overwrite another's entry.
        model_id = _model_id(f"{provider_id}/{model_key}")
    else:
        provider_hint = _text(data.get("provider"), max_len=128)
        model_id = (_provider_model(provider_hint, model_key)
                    if provider_hint else None) or _model_id(model_key)
        if model_id is not None:
            provider_id = model_id.partition("/")[0]
    if model_id is None:
        return None
    notes: list[str] = []
    canonical_id = _model_id(data.get("canonical_model_id"))
    if canonical_id is None and canonical is not None:
        for key in (f"{provider_id}/{model_key}", model_key):
            if isinstance(canonical.get(key), Mapping):
                canonical_id = _model_id(canonical[key].get("id"))
                if canonical_id:
                    break
    if canonical_id == model_id:
        canonical_id = None
    family = _text(data.get("family"), max_len=64)
    open_weights = _boolean(data.get("open_weights"))
    if open_weights is True:
        notes.append("open weights")
    type_value = _type(data.get("type"), notes)
    limits = data.get("limit")
    limits = limits if isinstance(limits, Mapping) else {}
    context = _integer(limits.get("context"))
    input_limit = _integer(limits.get("input"))
    output_limit = _integer(limits.get("output"))
    if context is None:
        context = _integer(data.get("context"))
    if input_limit is None:
        input_limit = context
    if output_limit is None:
        output_limit = _integer(data.get("max_output_tokens"))
    costs = data.get("cost")
    costs = costs if isinstance(costs, Mapping) else {}
    input_cost = _number(costs.get("input"))
    output_cost = _number(costs.get("output"))
    modalities = data.get("modalities")
    modalities = modalities if isinstance(modalities, Mapping) else {}
    modalities_in = _modalities(modalities.get("input"))
    modalities_out = _modalities(modalities.get("output"))
    capabilities = set(_capabilities(data.get("capabilities")))
    _apply_flags(capabilities, data, {
        "tool_call": ("tools", ""),
        "tool_calls": ("tools", ""),
        "function_calling": ("tools", ""),
        "reasoning": ("reasoning", ""),
        "structured_output": ("structured_output", ""),
        "structured_outputs": ("structured_output", ""),
        "response_schema": ("structured_output", ""),
        "computer_use": ("computer_use", ""),
    })
    if _boolean(data.get("attachment")) is True:
        capabilities.add("vision")
        if not modalities_in:
            modalities_in = ("image", "text")
        elif "image" not in modalities_in:
            modalities_in = tuple(sorted({*modalities_in, "image"}))
    if not modalities_in:
        modalities_in = ("text",)
    if not modalities_out:
        modalities_out = ("text",)
    release_date = _date(data.get("release_date"))
    knowledge = _date(data.get("knowledge"))
    return ModelMetadata(
        id=model_id,
        provider=provider_id,
        canonical_id=canonical_id,
        family=family,
        type=type_value,
        open_weights=open_weights,
        context=context,
        input_limit=input_limit,
        output_limit=output_limit,
        input_cost=input_cost,
        output_cost=output_cost,
        modalities_in=modalities_in,
        modalities_out=modalities_out,
        capabilities=tuple(sorted(capabilities)),
        release_date=release_date,
        knowledge=knowledge,
        source=source.id,
        source_url=source.url,
        notes=_join_notes(notes),
        profile_source=_join_notes([
            f"{source.id}: {canonical_id or model_id}",
            f"via {provider_id}" if provider_id else None,
        ]),
    )


def _parse_models_dev(root: Mapping[str, Any], source: Source
                      ) -> ParseResult:
    entries, canonical, shape = _models_dev_models(root)
    warnings: list[str] = []
    models: list[ModelMetadata] = []
    for provider_id, provider in entries:
        raw_models = provider.get("models")
        if not isinstance(raw_models, Mapping):
            warnings.append(f"{provider_id or '<flat>'}: no models table")
            continue
        for model_key, raw in raw_models.items():
            if not isinstance(raw, Mapping):
                warnings.append(f"{provider_id}/{model_key}: not a table")
                continue
            canonical_model: Mapping[str, Any] | None = None
            if canonical:
                candidate = raw.get("canonical_model_id")
                keys = [candidate] if isinstance(candidate, str) else []
                keys.extend([f"{provider_id}/{model_key}", model_key])
                for key in keys:
                    if isinstance(key, str) and isinstance(
                            canonical.get(key), Mapping):
                        canonical_model = canonical[key]
                        break
            data = _merge_models_dev(canonical_model, raw)
            model = _models_dev_model(
                provider_id, str(model_key), data, canonical, source)
            if model is None:
                warnings.append(
                    f"{provider_id}/{model_key}: no usable provider/model id")
                continue
            models.append(model)
    if not models:
        raise SourceError(
            "malformed", "models.dev payload contains no usable models",
            source=source.id, details={"shape": shape})
    return ParseResult(models=tuple(models), warnings=tuple(warnings),
                       shape=f"models-dev:{shape}")


def _parse_litellm(root: Mapping[str, Any], source: Source) -> ParseResult:
    warnings: list[str] = []
    models: list[ModelMetadata] = []
    for key, raw in root.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            continue
        if key.startswith("_") or key in {"sample_spec", "sample_spec_2"}:
            continue
        provider = _text(raw.get("litellm_provider"), max_len=128)
        if "/" in key:
            model_id = _model_id(key)
        elif provider:
            model_id = _provider_model(provider, key)
        else:
            model_id = None
        if model_id is None:
            warnings.append(f"{key}: no provider; skipped")
            continue
        provider_name = model_id.partition("/")[0]
        notes: list[str] = []
        type_value = _type(raw.get("mode"), notes)
        context = _integer(raw.get("max_input_tokens"))
        output_limit = _integer(raw.get("max_output_tokens"))
        if output_limit is None:
            output_limit = _integer(raw.get("max_tokens"))
        input_cost = _per_million(raw.get("input_cost_per_token"))
        output_cost = _per_million(raw.get("output_cost_per_token"))
        capabilities = set(_capabilities(raw.get("capabilities")))
        _apply_flags(capabilities, raw, {
            "supports_function_calling": ("tools", ""),
            "supports_tool_choice": ("tools", ""),
            "supports_vision": ("vision", ""),
            "supports_reasoning": ("reasoning", ""),
            "supports_response_schema": ("structured_output", ""),
            "supports_structured_output": ("structured_output", ""),
            "supports_computer_use": ("computer_use", ""),
            "supports_pdf_input": ("pdf_native", ""),
            "supports_pdf": ("pdf_native", ""),
        })
        audio = False
        if _boolean(raw.get("supports_audio_input")) is True:
            capabilities.add("audio")
            audio = True
        if _boolean(raw.get("supports_audio_output")) is True:
            capabilities.add("audio")
            audio = True
        modalities_in = _modalities(
            raw.get("supported_input_modalities")
            or raw.get("supported_modalities")
            or raw.get("input_modalities"))
        modalities_out = _modalities(
            raw.get("supported_output_modalities")
            or raw.get("output_modalities"))
        if _boolean(raw.get("supports_vision")) is True:
            modalities_in = tuple(sorted({*modalities_in, "image"}))
        if _boolean(raw.get("supports_pdf_input")) is True or _boolean(
                raw.get("supports_pdf")) is True:
            modalities_in = tuple(sorted({*modalities_in, "pdf"}))
        if _boolean(raw.get("supports_audio_input")) is True or audio:
            if _boolean(raw.get("supports_audio_input")) is True:
                modalities_in = tuple(sorted({*modalities_in, "audio"}))
            if _boolean(raw.get("supports_audio_output")) is True:
                modalities_out = tuple(sorted({*modalities_out, "audio"}))
        if not modalities_in:
            modalities_in = ("text",)
        if not modalities_out:
            modalities_out = ("text",)
        if raw.get("deprecation_date"):
            notes.append(f"deprecated {_text(raw.get('deprecation_date'),
                                             max_len=32) or ''}".strip())
        upstream = _text(raw.get("source"), max_len=240)
        if upstream:
            notes.append(f"upstream source: {upstream}")
        models.append(ModelMetadata(
            id=model_id,
            provider=provider_name,
            type=type_value,
            context=context,
            output_limit=output_limit,
            input_cost=input_cost,
            output_cost=output_cost,
            modalities_in=modalities_in,
            modalities_out=modalities_out,
            capabilities=tuple(sorted(capabilities)),
            source=source.id,
            source_url=source.url,
            notes=_join_notes(notes),
            profile_source=f"{source.id}: {model_id}",
        ))
    if not models:
        raise SourceError("malformed", "LiteLLM payload contains no usable models",
                          source=source.id)
    return ParseResult(models=tuple(models), warnings=tuple(warnings),
                       shape="litellm:map")


def _parse_openrouter(root: Mapping[str, Any], source: Source
                      ) -> ParseResult:
    items = root.get("data")
    if not isinstance(items, list):
        raise SourceError("malformed", "OpenRouter payload has no data list",
                          source=source.id)
    warnings: list[str] = []
    models: list[ModelMetadata] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            warnings.append(f"data[{index}]: not a table")
            continue
        model_id = _model_id(raw.get("id"))
        if model_id is None:
            warnings.append(f"data[{index}]: no usable provider/model id")
            continue
        provider = model_id.partition("/")[0]
        canonical_id = _model_id(raw.get("canonical_slug"))
        if canonical_id == model_id:
            canonical_id = None
        architecture = raw.get("architecture")
        architecture = architecture if isinstance(architecture, Mapping) else {}
        modalities_in = _modalities(architecture.get("input_modalities"))
        modalities_out = _modalities(architecture.get("output_modalities"))
        if not modalities_in:
            modalities_in = ("text",)
        if not modalities_out:
            modalities_out = ("text",)
        pricing = raw.get("pricing")
        pricing = pricing if isinstance(pricing, Mapping) else {}
        input_cost = _per_million(pricing.get("prompt"))
        output_cost = _per_million(pricing.get("completion"))
        top_provider = raw.get("top_provider")
        top_provider = (top_provider if isinstance(top_provider, Mapping)
                        else {})
        capabilities = _capabilities(raw.get("supported_parameters"))
        notes: list[str] = []
        if raw.get("expiration_date"):
            notes.append(f"expires {_text(raw.get('expiration_date'),
                                          max_len=32) or ''}".strip())
        models.append(ModelMetadata(
            id=model_id,
            provider=provider,
            canonical_id=canonical_id,
            context=_integer(raw.get("context_length")),
            output_limit=_integer(top_provider.get("max_completion_tokens")),
            input_cost=input_cost,
            output_cost=output_cost,
            modalities_in=modalities_in,
            modalities_out=modalities_out,
            capabilities=capabilities,
            release_date=_date(raw.get("created")),
            knowledge=_date(raw.get("knowledge_cutoff")),
            source=source.id,
            source_url=source.url,
            notes=_join_notes(notes),
            profile_source=f"{source.id}: {model_id}",
        ))
    if not models:
        raise SourceError("malformed", "OpenRouter payload contains no usable models",
                          source=source.id)
    return ParseResult(models=tuple(models), warnings=tuple(warnings),
                       shape="openrouter:data")


# --------------------------------------------------------------------------- #
# Epoch: parameter facts and conservative name matching
# --------------------------------------------------------------------------- #
def parse_parameter_count_b(value: Any) -> float | None:
    """Parse a parameter count into billions.

    Accepts ``1.8e12``, ``175B``, ``2.4T``, ``70`` (billions), comma-grouped
    totals and numeric cells. Returns ``None`` when the value is not a
    plausible parameter count.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        count = float(value)
        return (round(count / 1e9, 6) if count >= 1_000_000 else count)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\u00a0", " ")
    if not text:
        return None
    match = _NUMBER_RE.match(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    exponent = int(match.group(2)) if match.group(2) else 0
    suffix = match.group(3).lower()
    count = number * (10 ** exponent)
    if suffix == "t":
        count *= 1e12
    elif suffix == "b":
        count *= 1e9
    elif suffix == "m":
        count *= 1e6
    elif suffix == "k":
        count *= 1e3
    elif suffix == "":
        # Bare values below a million are already in billions; large values
        # are raw parameter totals. This is the documented column convention.
        if count < 1_000_000:
            return round(count, 6) if count > 0 else None
        count *= 1
    if count <= 0:
        return None
    billions = count / 1e9
    if billions <= 0 or billions > 100_000:
        return None
    return round(billions, 6)


_STRIP_SUFFIXES = (
    "instruct", "chat", "base", "preview", "latest", "it", "gguf", "awq",
    "fp16", "bf16", "hf", "v1", "v2", "v1.0", "v2.0",
)


def normalize_model_name(value: Any) -> str:
    """Lower-case alphanumeric key used only for conservative matching."""
    if not isinstance(value, str):
        return ""
    text = value.casefold()
    text = text.replace("&", " and ")
    return "".join(char for char in text if char.isalnum())


def _name_aliases(model: str, organization: str | None,
                  base_model: str | None, hf_developer: str | None
                  ) -> tuple[str, ...]:
    names: list[str] = [model]
    if organization:
        names.append(f"{organization} {model}")
        names.append(f"{organization}/{model}")
    if base_model:
        names.append(base_model)
    if hf_developer:
        names.append(f"{hf_developer}/{model}")
        names.append(f"{hf_developer} {model}")
    aliases: list[str] = []
    for name in names:
        normalized = normalize_model_name(name)
        if len(normalized) < 4:
            continue
        candidates = [normalized]
        stripped = normalized
        changed = True
        while changed:
            changed = False
            for suffix in _STRIP_SUFFIXES:
                key = normalize_model_name(suffix)
                if key and stripped.endswith(key) and len(stripped) > len(key) + 3:
                    stripped = stripped[:-len(key)]
                    candidates.append(stripped)
                    changed = True
        for candidate in candidates:
            if len(candidate) >= 4 and candidate not in aliases:
                aliases.append(candidate)
    return tuple(aliases)


@dataclass(frozen=True)
class EpochEntry:
    model: str
    organization: str = ""
    publication_date: str | None = None
    params_total_b: float | None = None
    training_compute: str | None = None
    open_weights: bool | None = None
    base_model: str | None = None
    huggingface_developer: str | None = None
    domain: str | None = None
    task: str | None = None
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "organization": self.organization,
            "publication_date": self.publication_date,
            "params_total_b": self.params_total_b,
            "training_compute": self.training_compute,
            "open_weights": self.open_weights,
            "base_model": self.base_model,
            "huggingface_developer": self.huggingface_developer,
            "domain": self.domain,
            "task": self.task,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class EpochIndex:
    """A conservative name-to-facts index built from the Epoch CSV."""

    entries: tuple[EpochEntry, ...] = ()
    by_alias: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    ambiguous: tuple[str, ...] = ()

    @classmethod
    def build(cls, entries: Sequence[EpochEntry]) -> EpochIndex:
        mapping: dict[str, list[int]] = {}
        for index, entry in enumerate(entries):
            for alias in entry.aliases:
                mapping.setdefault(alias, []).append(index)
        by_alias = {key: tuple(value) for key, value in mapping.items()}
        ambiguous = tuple(sorted(
            key for key, value in by_alias.items() if len(value) > 1))
        return cls(entries=tuple(entries), by_alias=by_alias,
                   ambiguous=ambiguous)

    def match(self, model_id: str) -> EpochEntry | None:
        """Match an id/name only when the normalized name is unambiguous.

        A full ``provider/model`` match wins; otherwise the model part is
        tried. Two different rows with the same name are treated as ambiguous
        and yield no match.
        """
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        full = normalize_model_name(model_id)
        part = normalize_model_name(
            model_id.split("/", 1)[1]) if "/" in model_id else full
        for key in (full, part):
            if len(key) < 4:
                continue
            indexes = set(self.by_alias.get(key, ()))
            if len(indexes) == 1:
                return self.entries[next(iter(indexes))]
            if len(indexes) > 1:
                return None
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "aliases": len(self.by_alias),
            "ambiguous_aliases": len(self.ambiguous),
        }


def _epoch_header(row: Mapping[str, Any], name: str) -> Any:
    target = name.strip().lower()
    for key, value in row.items():
        if isinstance(key, str) and key.strip().lower() == target:
            return value
    return None


def _parse_epoch(text: str, source: Source) -> ParseResult:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SourceError("malformed", "Epoch CSV has no header", source=source.id)
    normalized = {name.strip().lower() for name in reader.fieldnames
                  if isinstance(name, str)}
    if "model" not in normalized:
        raise SourceError(
            "malformed", "Epoch CSV has no Model column", source=source.id,
            details={"columns": sorted(normalized)})
    warnings: list[str] = []
    entries: list[EpochEntry] = []
    for index, row in enumerate(reader, start=2):
        if not isinstance(row, Mapping):
            continue
        model = _text(_epoch_header(row, "Model"), max_len=200)
        if not model:
            warnings.append(f"row {index}: no Model; skipped")
            continue
        organization = _text(_epoch_header(row, "Organization"),
                             max_len=200) or ""
        base_model = _text(_epoch_header(row, "Base model"), max_len=200)
        hf_developer = _text(
            _epoch_header(row, "Hugging Face developer id"), max_len=200)
        params = parse_parameter_count_b(
            _epoch_header(row, "Parameters"))
        compute = _text(_epoch_header(row, "Training compute (FLOP)"),
                        max_len=80)
        open_value = _epoch_header(row, "Open model weights?")
        open_weights: bool | None = None
        if isinstance(open_value, str):
            key = open_value.strip().lower()
            if key in _OPEN_WEIGHTS_TRUE:
                open_weights = True
            elif key in _OPEN_WEIGHTS_FALSE:
                open_weights = False
        entries.append(EpochEntry(
            model=model,
            organization=organization,
            publication_date=_date(_epoch_header(row, "Publication date")),
            params_total_b=params,
            training_compute=compute,
            open_weights=open_weights,
            base_model=base_model,
            huggingface_developer=hf_developer,
            domain=_text(_epoch_header(row, "Domain"), max_len=80),
            task=_text(_epoch_header(row, "Task"), max_len=120),
            aliases=_name_aliases(model, organization, base_model, hf_developer),
        ))
    if not entries:
        raise SourceError("malformed", "Epoch CSV contains no model rows",
                          source=source.id)
    index = EpochIndex.build(entries)
    if index.ambiguous:
        warnings.append(
            f"{len(index.ambiguous)} Epoch names are ambiguous and are not "
            "used for enrichment")
    return ParseResult(epoch=index, warnings=tuple(warnings), shape="epoch:csv")


def apply_epoch(external: Mapping[str, Mapping[str, Any]],
                epoch: EpochIndex | None) -> dict[str, dict[str, Any]]:
    """Fill parameter counts and compute notes where the name matches.

    Only fields the graph does not already know are filled. A name that is
    absent or ambiguous is left alone; no id is invented and no number is
    copied across unrelated names. Returns a new mapping.
    """
    result: dict[str, dict[str, Any]] = {
        key: dict(value) for key, value in external.items()
    }
    if epoch is None or not epoch.entries:
        return result
    for model_id, fields in result.items():
        entry = epoch.match(model_id)
        if entry is None:
            continue
        changed = False
        if (fields.get("params_total_b") is None
                and entry.params_total_b is not None):
            fields["params_total_b"] = entry.params_total_b
            changed = True
        if entry.training_compute:
            compute = entry.training_compute
            note = (f"training compute: {compute}"
                    if "flop" in compute.casefold()
                    else f"training compute: {compute} FLOP")
            existing = fields.get("notes")
            existing_text = existing if isinstance(existing, str) else ""
            if note not in existing_text:
                fields["notes"] = _join_notes([existing_text, note])
                changed = True
        if changed:
            if not fields.get("source"):
                fields["source"] = "epoch"
            provenance = fields.get("profile_source")
            provenance_text = (provenance if isinstance(provenance, str)
                               else "")
            if "epoch" not in provenance_text:
                fields["profile_source"] = _join_notes([
                    provenance_text, "epoch enrichment"])
    return result


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #
def _decode(data: bytes, source: Source) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("malformed", f"not UTF-8 text: {exc}",
                          source=source.id) from exc


def parse_payload(source: Source, data: bytes) -> ParseResult:
    """Parse one snapshot after format/shape validation."""
    if not source.fetchable:
        raise SourceError(
            "unsupported_source",
            f"source {source.id!r} is registered but not fetchable in this slice",
            source=source.id)
    text = _decode(data, source)
    if source.format == "json":
        try:
            root = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceError(
                "malformed", f"invalid JSON: {exc}", source=source.id,
                details={"line": exc.lineno, "column": exc.colno}) from exc
        if not isinstance(root, Mapping):
            raise SourceError("malformed", "JSON root must be an object",
                              source=source.id)
        if source.id == "models-dev":
            return _parse_models_dev(root, source)
        if source.id == "litellm":
            return _parse_litellm(root, source)
        if source.id == "openrouter":
            return _parse_openrouter(root, source)
        raise SourceError("unsupported_source",
                          f"no JSON adapter for {source.id!r}",
                          source=source.id)
    if source.format == "csv":
        if source.id == "epoch":
            return _parse_epoch(text, source)
        raise SourceError("unsupported_source",
                          f"no CSV adapter for {source.id!r}",
                          source=source.id)
    raise SourceError("unsupported_source",
                      f"unsupported format {source.format!r}",
                      source=source.id)


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def cache_path(root: str | Path, source: Source | str) -> Path:
    item = get_source(source) if isinstance(source, str) else source
    return state_path(root, CACHE_DIRNAME, f"{item.id}.{item.format}")


def meta_path(root: str | Path, source: Source | str) -> Path:
    item = get_source(source) if isinstance(source, str) else source
    return state_path(root, CACHE_DIRNAME, f"{item.id}.meta.json")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _timestamp(value: Any) -> float:
    if value is None:
        return time.time()
    if isinstance(value, bool):
        raise SourceError("invalid_argument", "now must be a timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceError("invalid_argument",
                              f"cannot parse timestamp {value!r}") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    raise SourceError("invalid_argument", "now must be a timestamp")


def _parse_utc(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise SourceError("write_failed", f"cannot write {path}: {exc}",
                          details={"path": str(path)}) from exc


@dataclass(frozen=True)
class CachedSource:
    """One source cache as read by ``load_cached`` (never fetched here)."""

    source: Source
    status: str
    external: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    epoch: EpochIndex | None = None
    meta: Mapping[str, Any] | None = None
    cache_path: Path | None = None
    meta_path: Path | None = None
    age_s: float | None = None
    stale: bool = False
    warnings: tuple[str, ...] = ()
    shape: str = ""
    models: int = 0

    @property
    def loaded(self) -> bool:
        return self.status in {_STATUS_OK, _STATUS_STALE}

    @property
    def not_assessed(self) -> bool:
        return not self.loaded

    @property
    def epoch_index(self) -> EpochIndex | None:
        """Alias for callers that name the Epoch enrichment index explicitly."""
        return self.epoch

    def to_dict(self) -> dict[str, Any]:
        meta = self.meta or {}
        return {
            "source": self.source.id,
            "status": self.status,
            "loaded": self.loaded,
            "stale": self.stale,
            "not_assessed": self.not_assessed,
            "cache_path": str(self.cache_path) if self.cache_path else None,
            "meta_path": str(self.meta_path) if self.meta_path else None,
            "bytes": meta.get("bytes"),
            "sha256": meta.get("sha256"),
            "fetched_at": meta.get("fetched_at"),
            "age_s": (round(self.age_s, 3)
                      if self.age_s is not None else None),
            "format": self.source.format,
            "license": self.source.license,
            "url": self.source.url,
            "models": self.models,
            "shape": self.shape,
            "epoch": self.epoch.to_dict() if self.epoch is not None else None,
            "warnings": list(self.warnings),
        }


def _cache_failure(source: Source, status: str, message: str,
                   *, root: Path) -> CachedSource:
    return CachedSource(
        source=source,
        status=status,
        cache_path=cache_path(root, source),
        meta_path=meta_path(root, source),
        warnings=(message,),
    )


def load_cached(root: str | Path, source: str | Source, *,
                now: Any = None) -> CachedSource:
    """Read a cached snapshot. Missing, malformed or stale is reported.

    Never fetches, never writes and never raises for a cache problem. Unknown
    source ids still raise ``SourceError``.
    """
    item = get_source(source) if isinstance(source, str) else source
    root_p = Path(root).resolve()
    if not item.fetchable:
        return _cache_failure(
            item, _STATUS_FUTURE,
            f"{item.id} is listed as a future source; it was not assessed",
            root=root_p)
    data_file = cache_path(root_p, item)
    meta_file = meta_path(root_p, item)
    if not data_file.is_file() and not meta_file.is_file():
        return _cache_failure(
            item, _STATUS_MISSING,
            f"no cached {item.id} snapshot; run `sync {item.id} "
            "--allow-network` or `import`",
            root=root_p)
    if not data_file.is_file():
        return _cache_failure(
            item, _STATUS_MALFORMED,
            f"{data_file.name} is missing while its metadata exists",
            root=root_p)
    if not meta_file.is_file():
        return _cache_failure(
            item, _STATUS_MALFORMED,
            f"{item.id}.meta.json is missing while its data exists",
            root=root_p)
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _cache_failure(
            item, _STATUS_MALFORMED,
            f"cannot read cache metadata: {exc}", root=root_p)
    if not isinstance(meta, Mapping):
        return _cache_failure(
            item, _STATUS_MALFORMED, "cache metadata is not an object",
            root=root_p)
    required = {"source", "url", "license", "sha256", "bytes", "fetched_at",
                "format", "stale_after_s"}
    missing = sorted(required - set(meta))
    if missing:
        return _cache_failure(
            item, _STATUS_MALFORMED,
            "cache metadata is missing: " + ", ".join(missing), root=root_p)
    if meta.get("source") != item.id or meta.get("format") != item.format:
        return _cache_failure(
            item, _STATUS_MALFORMED,
            "cache metadata does not match this source", root=root_p)
    expected_sha = meta.get("sha256")
    size = meta.get("bytes")
    stale_after = meta.get("stale_after_s")
    if (not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha)
            or isinstance(size, bool) or not isinstance(size, int) or size < 0
            or isinstance(stale_after, bool)
            or not isinstance(stale_after, (int, float)) or stale_after <= 0):
        return _cache_failure(
            item, _STATUS_MALFORMED,
            "cache metadata has invalid sha256/bytes/stale_after_s",
            root=root_p)
    fetched = _parse_utc(meta.get("fetched_at"))
    if fetched is None:
        return _cache_failure(
            item, _STATUS_MALFORMED, "cache metadata has an invalid fetched_at",
            root=root_p)
    try:
        file_size = data_file.stat().st_size
    except OSError as exc:
        return _cache_failure(
            item, _STATUS_MALFORMED, f"cannot stat cache data: {exc}",
            root=root_p)
    if file_size > item.max_bytes:
        return _cache_failure(
            item, _STATUS_MALFORMED,
            f"cached data exceeds the {item.max_bytes}-byte limit",
            root=root_p)
    try:
        data = data_file.read_bytes()
    except OSError as exc:
        return _cache_failure(
            item, _STATUS_MALFORMED, f"cannot read cache data: {exc}",
            root=root_p)
    if len(data) != size:
        return _cache_failure(
            item, _STATUS_MALFORMED,
            f"cached data is {len(data)} bytes, metadata says {size}",
            root=root_p)
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha.lower():
        return _cache_failure(
            item, _STATUS_MALFORMED,
            "cached data does not match its recorded sha256", root=root_p)
    try:
        parsed = parse_payload(item, data)
    except SourceError as exc:
        return _cache_failure(
            item, _STATUS_MALFORMED, f"cannot parse cached data: {exc}",
            root=root_p)
    age = max(0.0, _timestamp(now) - fetched)
    stale = age >= float(stale_after)
    warnings = list(parsed.warnings)
    if stale:
        warnings.append(
            f"cache is {int(age)}s old; stale after {int(stale_after)}s")
    return CachedSource(
        source=item,
        status=_STATUS_STALE if stale else _STATUS_OK,
        external=parsed.to_external(),
        epoch=parsed.epoch,
        meta=dict(meta),
        cache_path=data_file,
        meta_path=meta_file,
        age_s=age,
        stale=stale,
        warnings=tuple(warnings),
        shape=parsed.shape,
        models=len(parsed.models),
    )


def source_status(root: str | Path | None = None, *, now: Any = None
                  ) -> dict[str, Any]:
    """Registry plus cache state for every source. Reads only."""
    root_p = Path(root).resolve() if root is not None else repo_root()
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    not_assessed: list[str] = []
    for source in SOURCES:
        cached = load_cached(root_p, source, now=now)
        entry = source.to_dict()
        entry.update(cached.to_dict())
        entry["id"] = source.id
        entry["cache"] = {
            key: entry.get(key)
            for key in ("status", "cache_path", "meta_path", "bytes",
                        "sha256", "fetched_at", "age_s", "stale",
                        "not_assessed", "models", "shape", "warnings")
        }
        entries.append(entry)
        warnings.extend(f"{source.id}: {warning}"
                        for warning in cached.warnings)
        if cached.not_assessed:
            not_assessed.append(source.id)
    return {
        "schema": SCHEMA,
        "root": str(root_p),
        "sources": entries,
        "not_assessed": not_assessed,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# network acquisition
# --------------------------------------------------------------------------- #
class _NoRedirect(HTTPRedirectHandler):
    """Follow redirects explicitly so every hop can be validated/capped."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _validate_url(url: str, *, source: str | None = None) -> str:
    if not isinstance(url, str) or not url.strip():
        raise SourceError("bad_url", "source URL must be a non-empty string",
                          source=source)
    raw = url.strip()
    if len(raw) > 4096 or any(ord(char) < 32 for char in raw):
        raise SourceError("bad_url",
                          "source URL is too long or contains control characters",
                          source=source)
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - validates the port
    except ValueError as exc:
        raise SourceError("bad_url", f"source URL is invalid: {exc}",
                          source=source) from exc
    if parts.scheme not in {"http", "https"}:
        raise SourceError("bad_url",
                          "source URL and redirects must use http or https",
                          source=source)
    if parts.username is not None or parts.password is not None:
        raise SourceError("bad_url", "source URL must not contain credentials",
                          source=source)
    if not parts.hostname:
        raise SourceError("bad_url", "source URL has no host", source=source)
    return parts.geturl()


def _validate_timeout(timeout_s: Any) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise SourceError("invalid_argument", "timeout_s must be a number")
    timeout = float(timeout_s)
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_S:
        raise SourceError(
            "invalid_argument",
            f"timeout_s must be between 0 and {int(MAX_TIMEOUT_S)} seconds")
    return timeout


def _validate_max_bytes(value: Any, source: Source) -> int:
    if value is None:
        return source.max_bytes
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceError("invalid_argument", "max_bytes must be an integer")
    if value <= 0 or value > MAX_BYTES_LIMIT:
        raise SourceError(
            "invalid_argument",
            f"max_bytes must be between 1 and {MAX_BYTES_LIMIT}")
    return value


def _content_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _check_content_type(source: Source, headers: Mapping[str, Any]) -> None:
    content_type = _content_type(
        headers.get("Content-Type") or headers.get("content-type"))
    if not content_type:
        return
    allowed = _JSON_TYPES if source.format == "json" else _CSV_TYPES
    if content_type not in allowed:
        raise SourceError(
            "bad_content_type",
            f"unexpected content type {content_type!r} for {source.format}",
            source=source.id, details={"content_type": content_type})


def _open_url(request: Request, timeout: float):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _download(source: Source, url: str, *, timeout: float, max_bytes: int,
              opener: Callable[..., Any] | None) -> tuple[bytes, str]:
    current = _validate_url(url, source=source.id)
    open_url = opener or _open_url
    for redirects in range(MAX_REDIRECTS + 1):
        request = Request(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": ("application/json, text/plain;q=0.9" if source.format
                           == "json" else "text/csv, text/plain;q=0.9"),
            })
        try:
            response = open_url(request, timeout=timeout)
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            try:
                if exc.code not in _REDIRECT_CODES or not location:
                    raise SourceError(
                        "network_error",
                        f"HTTP {exc.code} from {current}",
                        source=source.id, details={"status": exc.code}) from exc
                if redirects >= MAX_REDIRECTS:
                    raise SourceError(
                        "network_error",
                        f"download exceeded {MAX_REDIRECTS} redirects",
                        source=source.id) from exc
                current = _validate_url(urljoin(current, location),
                                        source=source.id)
            finally:
                exc.close()
            continue
        except (TimeoutError, URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                raise SourceError("timeout",
                                  f"download timed out after {timeout:g}s",
                                  source=source.id) from exc
            raise SourceError("network_error", f"download failed: {reason}",
                              source=source.id) from exc
        except OSError as exc:
            raise SourceError("network_error", f"download failed: {exc}",
                              source=source.id) from exc
        if isinstance(response, (bytes, bytearray)):
            if len(response) > max_bytes:
                raise SourceError(
                    "too_large",
                    f"download exceeds the {max_bytes}-byte limit",
                    source=source.id)
            return bytes(response), current
        try:
            context = (response if hasattr(response, "__enter__")
                       else nullcontext(response))
            with context:
                final = response.geturl() if hasattr(response, "geturl") else current
                final = _validate_url(final, source=source.id)
                code = getattr(response, "status", None)
                if code is None and hasattr(response, "getcode"):
                    code = response.getcode()
                if code is not None and not 200 <= int(code) < 300:
                    raise SourceError(
                        "network_error", f"HTTP {code} from {current}",
                        source=source.id, details={"status": int(code)})
                headers = getattr(response, "headers", {}) or {}
                _check_content_type(source, headers)
                declared = headers.get("Content-Length")
                try:
                    declared_bytes = int(declared) if declared is not None else None
                except (TypeError, ValueError):
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > max_bytes:
                    raise SourceError(
                        "too_large",
                        f"download exceeds the {max_bytes}-byte limit",
                        source=source.id,
                        details={"content_length": declared_bytes})
                chunks = bytearray()
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise SourceError(
                            "too_large",
                            f"download exceeds the {max_bytes}-byte limit",
                            source=source.id)
                return bytes(chunks), final
        except SourceError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceError("network_error", f"download failed: {exc}",
                              source=source.id) from exc
    raise SourceError("network_error",
                      f"download exceeded {MAX_REDIRECTS} redirects",
                      source=source.id)


def _write_cache(root: Path, source: Source, data: bytes, *, url: str,
                 origin: str, imported_from: str | None = None) -> dict[str, Any]:
    parsed = parse_payload(source, data)
    digest = hashlib.sha256(data).hexdigest()
    fetched_at = _utc_now()
    meta: dict[str, Any] = {
        "source": source.id,
        "url": url,
        "license": source.license,
        "attribution": source.attribution,
        "sha256": digest,
        "bytes": len(data),
        "fetched_at": fetched_at,
        "format": source.format,
        "stale_after_s": source.stale_after_s,
        "origin": origin,
    }
    if imported_from is not None:
        meta["imported_from"] = imported_from
    data_file = cache_path(root, source)
    meta_file = meta_path(root, source)
    _atomic_write(data_file, data)
    _atomic_write(
        meta_file,
        (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return {
        "schema": SCHEMA,
        "source": source.id,
        "status": "ok",
        "origin": origin,
        "url": url,
        "cache_path": str(data_file),
        "meta_path": str(meta_file),
        "cache": {"path": str(data_file), "meta_path": str(meta_file)},
        "bytes": len(data),
        "sha256": digest,
        "fetched_at": fetched_at,
        "format": source.format,
        "license": source.license,
        "stale_after_s": source.stale_after_s,
        "models": len(parsed.models),
        "epoch_rows": (len(parsed.epoch.entries)
                       if parsed.epoch is not None else 0),
        "shape": parsed.shape,
        "warnings": list(parsed.warnings),
    }


def sync(root: str | Path | None = None, source: str = "", *,
         allow_network: bool = False, timeout_s: float = DEFAULT_TIMEOUT_S,
         max_bytes: int | None = None,
         opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Fetch and cache one source. Requires an explicit network opt-in.

    ``opener`` is a test/integration seam with the same shape as
    ``urllib.request.OpenerDirector.open``: ``opener(request, timeout=...)``
    returning a context-manager response with ``read``, ``headers``,
    ``status`` and ``geturl``. It is only consulted after the network gate.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    item = get_source(source)
    if not item.fetchable:
        raise SourceError(
            "unsupported_source",
            f"source {item.id!r} is not fetchable in this slice",
            source=item.id)
    if not allow_network:
        raise SourceError(
            "network_not_allowed",
            "network access is disabled; pass --allow-network to sync "
            f"{item.id}",
            source=item.id,
            details={"url": item.url, "cache": str(cache_path(root_p, item))})
    timeout = _validate_timeout(timeout_s)
    cap = _validate_max_bytes(max_bytes, item)
    urls = (item.url, *item.fallback_urls)
    failures: list[str] = []
    for index, url in enumerate(urls):
        try:
            data, final_url = _download(
                item, url, timeout=timeout, max_bytes=cap, opener=opener)
            report = _write_cache(
                root_p, item, data, url=final_url, origin="network")
            if failures:
                report["warnings"] = [
                    *failures, *report.get("warnings", [])]
                report["fallback_used"] = True
            return report
        except SourceError as exc:
            failures.append(f"{item.id}: {url}: {exc}")
            if exc.reason != "network_error" or index == len(urls) - 1:
                details = {**exc.details, "attempts": failures}
                raise SourceError(
                    exc.reason, str(exc), source=item.id,
                    details=details) from exc
    raise SourceError("network_error", "no source URL was attempted",
                      source=item.id)


def import_file(root: str | Path | None = None, source: str = "",
                path: str | Path | None = None, *,
                max_bytes: int | None = None) -> dict[str, Any]:
    """Validate and cache a local snapshot. No network, no credentials."""
    root_p = Path(root).resolve() if root is not None else repo_root()
    item = get_source(source)
    if not item.fetchable:
        raise SourceError(
            "unsupported_source",
            f"source {item.id!r} is not fetchable in this slice",
            source=item.id)
    if path is None:
        raise SourceError("invalid_argument", "import path is required",
                          source=item.id)
    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise SourceError("invalid_argument", f"{source_path} is not a file",
                          source=item.id)
    cap = _validate_max_bytes(max_bytes, item)
    try:
        size = source_path.stat().st_size
    except OSError as exc:
        raise SourceError("invalid_argument", f"cannot stat {source_path}: {exc}",
                          source=item.id) from exc
    if size > cap:
        raise SourceError(
            "too_large",
            f"{source_path} is {size} bytes; limit is {cap}",
            source=item.id, details={"bytes": size, "max_bytes": cap})
    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise SourceError("invalid_argument", f"cannot read {source_path}: {exc}",
                          source=item.id) from exc
    if len(data) > cap:
        raise SourceError(
            "too_large", f"{source_path} exceeds the {cap}-byte limit",
            source=item.id)
    report = _write_cache(
        root_p, item, data, url=item.url, origin="import",
        imported_from=str(source_path.resolve()))
    return report


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MergedExternal:
    """Merged cached metadata and the report of which caches supplied it."""

    external: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    order: tuple[str, ...] = ()
    sources: tuple[Mapping[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    not_assessed: tuple[str, ...] = ()
    matched_models: int = 0
    epoch_matches: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "order": list(self.order),
            "sources": [dict(entry) for entry in self.sources],
            "not_assessed": list(self.not_assessed),
            "warnings": list(self.warnings),
            "metadata_models": len(self.external),
            "matched_models": self.matched_models,
            "epoch_matches": self.epoch_matches,
        }


def merge_cached(root: str | Path | None = None,
                 sources: Sequence[str] | None = None, *,
                 model_ids: Iterable[str] | None = None,
                 now: Any = None) -> MergedExternal:
    """Merge cached sources in registry precedence order.

    ``sources`` selects a subset; precedence still follows the documented
    order (models-dev, litellm, openrouter, epoch), not the argument order.
    ``model_ids`` limits the result to catalog ids after enrichment, which
    keeps graph warnings about uncatalogued metadata quiet.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    selected = tuple(sources) if sources is not None else DEFAULT_ORDER
    for source_id in selected:
        item = get_source(source_id)
        if not item.fetchable:
            continue
    order = tuple(source.id for source in SOURCES
                  if source.id in selected)
    unknown = [source_id for source_id in selected
               if source_id not in SOURCES_BY_ID]
    if unknown:
        get_source(unknown[0])
    merged: dict[str, dict[str, Any]] = {}
    reports: list[Mapping[str, Any]] = []
    warnings: list[str] = []
    not_assessed: list[str] = []
    epoch_index: EpochIndex | None = None
    for source_id in order:
        item = get_source(source_id)
        cached = load_cached(root_p, item, now=now)
        reports.append(cached.to_dict())
        warnings.extend(f"{source_id}: {warning}"
                        for warning in cached.warnings)
        if cached.not_assessed:
            not_assessed.append(source_id)
        if cached.epoch is not None:
            epoch_index = cached.epoch
        for model_id, fields in cached.external.items():
            target = merged.setdefault(model_id, {})
            for key, value in fields.items():
                if key not in target or target[key] in (None, "", (), []):
                    target[key] = value
    before = {key: dict(value) for key, value in merged.items()}
    if model_ids is not None:
        known = set(model_ids)
        merged = {key: value for key, value in merged.items()
                  if key in known}
        if epoch_index is not None:
            # Epoch is a name-matched enrichment source. Seed every catalog
            # id so a model known only to the local catalog can still receive
            # a plausible parameter count; empty seeds are dropped again.
            seeded = {model_id: merged.get(model_id, {})
                      for model_id in known}
            merged = apply_epoch(seeded, epoch_index)
            merged = {key: value for key, value in merged.items()
                      if key in known and value}
        else:
            merged = apply_epoch(merged, epoch_index)
    else:
        merged = apply_epoch(merged, epoch_index)
    epoch_matches = sum(
        1 for key, value in merged.items()
        if value != before.get(key, {}))
    return MergedExternal(
        external=merged,
        order=order,
        sources=tuple(reports),
        warnings=tuple(warnings),
        not_assessed=tuple(not_assessed),
        matched_models=len(merged),
        epoch_matches=epoch_matches,
    )


__all__ = [
    "CACHE_DIRNAME",
    "DEFAULT_ORDER",
    "EXTERNAL_FIELDS",
    "EpochEntry",
    "EpochIndex",
    "MergedExternal",
    "ModelMetadata",
    "ParseResult",
    "SCHEMA",
    "SOURCES",
    "SOURCES_BY_ID",
    "CachedSource",
    "Source",
    "SourceError",
    "apply_epoch",
    "cache_path",
    "get_source",
    "import_file",
    "load_cached",
    "merge_cached",
    "meta_path",
    "normalize_model_name",
    "parse_parameter_count_b",
    "parse_payload",
    "source_ids",
    "source_status",
    "sync",
]
