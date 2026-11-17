"""A model graph: curated profiles, model-space distance and advisory matching.

The registry in :mod:`ozagia.core.models` is authoritative about *which* models
exist and how ``models.resolve`` chooses one. This module adds an approximate
map around that registry:

* a :class:`ModelNode` per catalog model, with a curated family/type/size
  profile where one is known and an inferred profile otherwise;
* a bounded graph of family, provider and nearest-neighbour similarity edges;
* a weighted Gower-style :func:`distance` over known features;
* :func:`neighbours` and :func:`match`, which rank models for a task profile;
* :func:`tool_fit`, which annotates model/tool-format combinations.

Nothing here calls a model, a provider or the network. ``match`` is advisory:
it returns ranked candidates and reasons, and it never changes
``models.resolve`` or selects a model for a caller. Metadata is curated and
approximate; a missing number stays missing rather than being invented.

Profiles
--------
Built-in profiles live in the packaged ``model_profiles.toml`` and match model
names with ``fnmatch`` patterns. A repository can add or override metadata in
``ozagia/config.toml``::

    [model_graph.models."openai/gpt-5"]
    context = 400000
    capabilities = ["tools", "vision", "long_context"]

    [model_graph.tasks.diff_review]
    capabilities = ["tools", "code"]

    [model_graph.weights]
    family = 0.25
    capabilities = 0.10

Precedence for a node is exact id override > pattern profile (repository
family declarations before built-in profiles) > inferred. Task requirements
use a repository task override > the built-in task profile; explicit arguments
extend both. Unknown configuration keys are reported in the graph report, not
silently ignored and not fatal.

Distance caveat
---------------
``distance`` is bounded in ``0..1`` and symmetric, but it is **not** a strict
metric. It skips features that are unknown for either node, so the same pair
can have different coverage than another pair; ``coverage`` reports how much
of the configured weight was comparable. Treat the result as a similarity
indicator, not as a transitive distance.
"""
from __future__ import annotations

import fnmatch
import math
import re
import tomllib
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import load_config_section, repo_root
from ozagia.core import models as M

SCHEMA = 1
PROFILE_RESOURCE = "model_profiles.toml"
CONFIG_SECTION = "model_graph"

TYPES = ("chat", "reasoning", "code", "multimodal", "embedding", "image",
         "audio")
SIZE_CLASSES = ("tiny", "small", "medium", "large", "frontier", "unknown")
SIZE_RANK = {name: rank for rank, name in enumerate(SIZE_CLASSES[:-1])}
CONFIDENCES = ("curated", "declared", "external", "inferred")

CAPABILITIES = frozenset({
    "tools", "vision", "audio", "pdf_native", "long_context",
    "structured_output", "computer_use", "code", "reasoning",
})
TOOL_FORMATS = frozenset({
    "apply_patch", "replace", "whole_file", "library_call", "cli", "mcp",
    "native_pdf",
})
MODALITIES = frozenset({
    "text", "image", "pdf", "audio", "video", "embedding",
})

FEATURES = ("family", "variant", "type", "size", "params", "context", "cost",
            "capabilities", "modalities", "tool_formats")
DEFAULT_WEIGHTS = {
    "family": 0.20,
    "variant": 0.08,
    "type": 0.12,
    "size": 0.12,
    "params": 0.08,
    "context": 0.08,
    "cost": 0.06,
    "capabilities": 0.12,
    "modalities": 0.07,
    "tool_formats": 0.07,
}

MAX_NODES = 500
MAX_NODES_LIMIT = 5000
MAX_EDGES = 2000
MAX_EDGES_LIMIT = 20000
DEFAULT_TOP_K = 3
DEFAULT_SIMILARITY_THRESHOLD = 0.55
MAX_LIST = 500
MAX_TEXT = 500

MAX_CONTEXT_FOR_DISTANCE = 10_000_000
MAX_COST_FOR_DISTANCE = 1000.0
MAX_PARAMS_FOR_DISTANCE = 2000.0

MATCH_LIMIT_MAX = 100

MATCH_WEIGHTS = {
    "capability": 0.30,
    "tools": 0.15,
    "type": 0.10,
    "size": 0.10,
    "context": 0.12,
    "cost": 0.10,
    "availability": 0.08,
    "preference": 0.05,
}

_TYPE_AFFINITY = {
    ("chat", "reasoning"): 0.6,
    ("chat", "code"): 0.6,
    ("reasoning", "code"): 0.7,
    ("multimodal", "chat"): 0.6,
    ("multimodal", "reasoning"): 0.6,
    ("multimodal", "code"): 0.5,
    ("multimodal", "image"): 0.7,
    ("multimodal", "audio"): 0.6,
    ("embedding", "chat"): 0.2,
    ("embedding", "reasoning"): 0.2,
    ("embedding", "code"): 0.2,
    ("image", "audio"): 0.2,
}

_PARAM_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")
_ACTIVE_RE = re.compile(r"a(\d+(?:\.\d+)?)b")
_FAMILY_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*")
_TASK_CLASS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_FAMILY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

_STOP_FAMILY_TOKENS = frozenset({
    "model", "models", "chat", "instruct", "base", "it", "latest", "preview",
    "free", "online", "local", "api", "the", "and", "of",
})
_VARIANT_TOKENS = (
    "opus", "sonnet", "haiku", "mini", "nano", "flash", "pro", "max", "ultra",
    "turbo", "coder", "codex", "reasoner", "reasoning", "thinking", "think",
    "r1", "scout", "maverick",
)

_PROFILE_FIELDS = frozenset({
    "family", "variant", "type", "size_class", "params_total_b",
    "params_active_b", "context", "input_cost", "output_cost",
    "modalities_in", "modalities_out", "capabilities", "tool_formats", "local",
    "source", "confidence", "notes", "match", "providers",
})
_OVERRIDE_FIELDS = _PROFILE_FIELDS - {"match", "providers"}
_FAMILY_FIELDS = frozenset({
    "patterns", "type", "size_class", "params_total_b", "params_active_b",
    "context", "input_cost", "output_cost", "modalities_in", "modalities_out",
    "capabilities", "tool_formats", "local", "source", "confidence", "notes",
})
_GRAPH_KEYS = frozenset({"models", "families", "tasks", "weights"})
_TASK_FIELDS = frozenset({
    "capabilities", "tool_formats", "local_only", "min_context", "max_cost",
    "size", "type", "modalities_in", "modalities_out", "preferred_families",
    "preferred_types", "description", "notes",
})


class ModelGraphError(ValueError):
    """A profile, configuration value, distance weight or query is invalid."""


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #
def _fail(where: str, message: str) -> ModelGraphError:
    return ModelGraphError(f"{where}: {message}")


def _text(value: Any, where: str, *, max_len: int = MAX_TEXT,
          allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(where, "must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise _fail(where, "must not be empty")
    if len(text) > max_len:
        raise _fail(where, f"must be at most {max_len} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise _fail(where, "must not contain control characters")
    return text


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(where, "must be true or false")
    return value


def _integer(value: Any, where: str, *, minimum: int = 0,
             maximum: int = 1_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(where, "must be an integer")
    if value < minimum or value > maximum:
        raise _fail(where, f"must be between {minimum} and {maximum}")
    return value


def _number(value: Any, where: str, *, minimum: float = 0.0,
            maximum: float = 1_000_000.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(where, "must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise _fail(where, "must be finite")
    if number < minimum or number > maximum:
        raise _fail(where, f"must be between {minimum:g} and {maximum:g}")
    return number


def _choice(value: Any, allowed: Iterable[str], where: str) -> str:
    text = _text(value, where, max_len=64)
    if text not in allowed:
        options = ", ".join(sorted(allowed))
        raise _fail(where, f"must be one of: {options}")
    return text


def _text_list(value: Any, where: str, *, allowed: Iterable[str] | None = None,
               max_items: int = 128, allow_empty: bool = False
               ) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail(where, "must be a list of strings")
    if len(value) > max_items:
        raise _fail(where, f"must contain at most {max_items} entries")
    result: list[str] = []
    allowed_set = set(allowed) if allowed is not None else None
    for index, item in enumerate(value):
        text = _text(item, f"{where}[{index}]", max_len=128,
                     allow_empty=allow_empty)
        if allowed_set is not None and text not in allowed_set:
            options = ", ".join(sorted(allowed_set))
            raise _fail(f"{where}[{index}]", f"must be one of: {options}")
        if text not in result:
            result.append(text)
    return tuple(result)


def _parse_profile_fields(table: Mapping[str, Any], where: str,
                          unknown: list[str] | None,
                          allowed_extra: Iterable[str] = ()
                          ) -> dict[str, Any]:
    """Validate the profile-compatible fields of one table."""
    fields: dict[str, Any] = {}
    known = set(_OVERRIDE_FIELDS if unknown is not None else _PROFILE_FIELDS)
    known.update(allowed_extra)
    for key in table:
        if not isinstance(key, str):
            if unknown is None:
                raise _fail(where, "keys must be strings")
            unknown.append(f"{where}.<non-string>")
        elif key not in known:
            if unknown is None:
                raise _fail(where, f"unknown key {key!r}")
            unknown.append(f"{where}.{key}")

    if "family" in table:
        fields["family"] = _text(table["family"], f"{where}.family",
                                 max_len=64)
    if "variant" in table:
        fields["variant"] = _text(table["variant"], f"{where}.variant",
                                  max_len=64)
    if "type" in table:
        fields["type"] = _choice(table["type"], TYPES, f"{where}.type")
    if "size_class" in table:
        fields["size_class"] = _choice(
            table["size_class"], SIZE_CLASSES, f"{where}.size_class")
    if "params_total_b" in table:
        fields["params_total_b"] = _number(
            table["params_total_b"], f"{where}.params_total_b",
            maximum=100_000.0)
    if "params_active_b" in table:
        fields["params_active_b"] = _number(
            table["params_active_b"], f"{where}.params_active_b",
            maximum=100_000.0)
    if "context" in table:
        fields["context"] = _integer(
            table["context"], f"{where}.context", minimum=1,
            maximum=100_000_000)
    if "input_cost" in table:
        fields["input_cost"] = _number(
            table["input_cost"], f"{where}.input_cost", maximum=100_000.0)
    if "output_cost" in table:
        fields["output_cost"] = _number(
            table["output_cost"], f"{where}.output_cost", maximum=100_000.0)
    if "modalities_in" in table:
        fields["modalities_in"] = _text_list(
            table["modalities_in"], f"{where}.modalities_in",
            allowed=MODALITIES)
    if "modalities_out" in table:
        fields["modalities_out"] = _text_list(
            table["modalities_out"], f"{where}.modalities_out",
            allowed=MODALITIES)
    if "capabilities" in table:
        fields["capabilities"] = _text_list(
            table["capabilities"], f"{where}.capabilities",
            allowed=CAPABILITIES)
    if "tool_formats" in table:
        fields["tool_formats"] = _text_list(
            table["tool_formats"], f"{where}.tool_formats",
            allowed=TOOL_FORMATS)
    if "local" in table:
        fields["local"] = _boolean(table["local"], f"{where}.local")
    if "source" in table:
        fields["source"] = _text(table["source"], f"{where}.source")
    if "confidence" in table:
        fields["confidence"] = _choice(
            table["confidence"], CONFIDENCES, f"{where}.confidence")
    if "notes" in table:
        fields["notes"] = _text(table["notes"], f"{where}.notes",
                                allow_empty=True)
    return fields


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Profile:
    """One curated family/variant pattern or repository family declaration."""

    family: str
    match: tuple[str, ...]
    variant: str = "default"
    type: str = "chat"
    size_class: str = "unknown"
    params_total_b: float | None = None
    params_active_b: float | None = None
    context: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    modalities_in: tuple[str, ...] = ("text",)
    modalities_out: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ()
    tool_formats: tuple[str, ...] = ()
    local: bool | None = None
    source: str = ""
    confidence: str = "curated"
    notes: str = ""
    providers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "match": list(self.match),
            "providers": list(self.providers),
            "variant": self.variant,
            "type": self.type,
            "size_class": self.size_class,
            "params_total_b": self.params_total_b,
            "params_active_b": self.params_active_b,
            "context": self.context,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "modalities_in": list(self.modalities_in),
            "modalities_out": list(self.modalities_out),
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
            "local": self.local,
            "source": self.source,
            "confidence": self.confidence,
            "notes": self.notes,
        }


@lru_cache(maxsize=1)
def builtin_profiles() -> tuple[Profile, ...]:
    """Load and validate the packaged ``model_profiles.toml`` once."""
    try:
        text = resources.files("ozagia.core").joinpath(
            PROFILE_RESOURCE).read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError) as exc:  # pragma: no cover
        raise ModelGraphError(
            f"cannot read the packaged {PROFILE_RESOURCE}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover
        raise ModelGraphError(f"{PROFILE_RESOURCE}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelGraphError(f"{PROFILE_RESOURCE}: expected a table")
    unknown = sorted(set(data) - {"schema", "profiles"})
    if unknown:  # pragma: no cover - packaged data is reviewed
        raise ModelGraphError(
            f"{PROFILE_RESOURCE}: unknown top-level key {unknown[0]!r}")
    if data.get("schema") != SCHEMA:
        raise ModelGraphError(
            f"{PROFILE_RESOURCE}: schema must be {SCHEMA}")
    raw_profiles = data.get("profiles", [])
    if not isinstance(raw_profiles, list):
        raise ModelGraphError(f"{PROFILE_RESOURCE}: profiles must be an array")
    profiles: list[Profile] = []
    for index, table in enumerate(raw_profiles):
        where = f"{PROFILE_RESOURCE}: profiles[{index}]"
        if not isinstance(table, dict):
            raise ModelGraphError(f"{where}: must be a table")
        family = _text(table.get("family"), f"{where}.family", max_len=64)
        match = _text_list(table.get("match"), f"{where}.match",
                           max_items=32)
        if not match:
            raise ModelGraphError(f"{where}.match: must not be empty")
        providers = _text_list(table.get("providers", []),
                               f"{where}.providers", max_items=32)
        fields = _parse_profile_fields(table, where, None)
        fields.pop("family", None)
        profiles.append(Profile(
            family=family, match=match, providers=providers, **fields))
    return tuple(profiles)


# --------------------------------------------------------------------------- #
# repository configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GraphConfig:
    """The validated ``[model_graph]`` overlay and its diagnostics."""

    models: tuple[tuple[str, dict[str, Any]], ...] = ()
    profiles: tuple[Profile, ...] = ()
    tasks: tuple[tuple[str, dict[str, Any]], ...] = ()
    weights: tuple[tuple[str, float], ...] = ()
    unknown_keys: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    sources: tuple[tuple[str, str], ...] = ()

    def model_override(self, model_id: str) -> dict[str, Any] | None:
        return dict(self.models).get(model_id)

    def task_override(self, task: str) -> dict[str, Any] | None:
        return dict(self.tasks).get(task)

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": [model_id for model_id, _ in self.models],
            "families": [profile.family for profile in self.profiles],
            "tasks": [task for task, _ in self.tasks],
            "weights": dict(self.weights),
            "unknown_keys": list(self.unknown_keys),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "files": list(self.files),
            "sources": dict(self.sources),
        }


def _load_graph_config(root: Path) -> GraphConfig:
    section = load_config_section(root, CONFIG_SECTION)
    unknown: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    for item in section.errors:
        errors.append(f"{item.path}: {item.message}")
    for key in sorted(section.values):
        if key not in _GRAPH_KEYS:
            unknown.append(f"{CONFIG_SECTION}.{key}")

    models: list[tuple[str, dict[str, Any]]] = []
    raw_models = section.values.get("models", {})
    if raw_models is not None:
        if not isinstance(raw_models, Mapping):
            raise _fail(f"{CONFIG_SECTION}.models", "must be a table")
        for raw_id, raw_table in raw_models.items():
            model_id = M.validate_model_id(
                raw_id, where=f"{CONFIG_SECTION}.models")
            where = f'{CONFIG_SECTION}.models."{model_id}"'
            if not isinstance(raw_table, Mapping):
                raise _fail(where, "must be a table")
            fields = _parse_profile_fields(raw_table, where, unknown)
            fields.setdefault("source", "override")
            fields.setdefault("confidence", "declared")
            models.append((model_id, fields))

    profiles: list[Profile] = []
    raw_families = section.values.get("families", {})
    if raw_families is not None:
        if not isinstance(raw_families, Mapping):
            raise _fail(f"{CONFIG_SECTION}.families", "must be a table")
        for raw_name, raw_table in raw_families.items():
            name = _text(raw_name, f"{CONFIG_SECTION}.families",
                         max_len=64).lower()
            if not _FAMILY_NAME_RE.fullmatch(name):
                raise _fail(f"{CONFIG_SECTION}.families.{name}",
                            "must be a bounded family name")
            where = f"{CONFIG_SECTION}.families.{name}"
            if not isinstance(raw_table, Mapping):
                raise _fail(where, "must be a table")
            unknown.extend(
                f"{where}.{key}" for key in sorted(raw_table)
                if key not in _FAMILY_FIELDS)
            patterns_raw = raw_table.get("patterns", [f"*{name}*"])
            patterns = _text_list(patterns_raw, f"{where}.patterns",
                                  max_items=32)
            if not patterns:
                raise _fail(f"{where}.patterns", "must not be empty")
            fields = _parse_profile_fields(
                raw_table, where, unknown, allowed_extra={"patterns"})
            fields.pop("family", None)
            fields.setdefault("source", f"{CONFIG_SECTION}.families")
            fields.setdefault("confidence", "declared")
            profiles.append(Profile(
                family=name, match=patterns, **fields))

    tasks: list[tuple[str, dict[str, Any]]] = []
    raw_tasks = section.values.get("tasks", {})
    if raw_tasks is not None:
        if not isinstance(raw_tasks, Mapping):
            raise _fail(f"{CONFIG_SECTION}.tasks", "must be a table")
        for raw_task, raw_table in raw_tasks.items():
            name = _text(raw_task, f"{CONFIG_SECTION}.tasks", max_len=64)
            if not _TASK_CLASS_RE.fullmatch(name):
                raise _fail(f"{CONFIG_SECTION}.tasks.{name}",
                            "must be a bounded task class")
            where = f"{CONFIG_SECTION}.tasks.{name}"
            if not isinstance(raw_table, Mapping):
                raise _fail(where, "must be a table")
            unknown.extend(
                f"{where}.{key}" for key in sorted(raw_table)
                if key not in _TASK_FIELDS)
            tasks.append((name, _parse_task_fields(raw_table, where)))

    weights: list[tuple[str, float]] = []
    raw_weights = section.values.get("weights", {})
    if raw_weights is not None:
        if not isinstance(raw_weights, Mapping):
            raise _fail(f"{CONFIG_SECTION}.weights", "must be a table")
        for key, value in raw_weights.items():
            if not isinstance(key, str) or key not in FEATURES:
                unknown.append(f"{CONFIG_SECTION}.weights.{key}")
                continue
            weights.append((key, _number(
                value, f"{CONFIG_SECTION}.weights.{key}",
                minimum=0.0, maximum=1000.0)))

    files = tuple(str(path) for path in (section.legacy, section.repo)
                  if path is not None)
    return GraphConfig(
        models=tuple(models),
        profiles=tuple(profiles),
        tasks=tuple(tasks),
        weights=tuple(weights),
        unknown_keys=tuple(sorted(set(unknown))),
        warnings=tuple(warnings),
        errors=tuple(errors),
        files=files,
        sources=tuple(sorted(section.sources.items())),
    )


def _parse_task_fields(table: Mapping[str, Any], where: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "capabilities" in table:
        fields["capabilities"] = _text_list(
            table["capabilities"], f"{where}.capabilities",
            allowed=CAPABILITIES)
    if "tool_formats" in table:
        fields["tool_formats"] = _text_list(
            table["tool_formats"], f"{where}.tool_formats",
            allowed=TOOL_FORMATS)
    if "local_only" in table:
        fields["local_only"] = _boolean(
            table["local_only"], f"{where}.local_only")
    if "min_context" in table:
        fields["min_context"] = _integer(
            table["min_context"], f"{where}.min_context", minimum=1,
            maximum=100_000_000)
    if "max_cost" in table:
        fields["max_cost"] = _number(
            table["max_cost"], f"{where}.max_cost", maximum=100_000.0)
    if "size" in table:
        fields["size"] = _choice(
            table["size"], SIZE_CLASSES[:-1], f"{where}.size")
    if "type" in table:
        fields["type"] = _choice(table["type"], TYPES, f"{where}.type")
    if "modalities_in" in table:
        fields["modalities_in"] = _text_list(
            table["modalities_in"], f"{where}.modalities_in",
            allowed=MODALITIES)
    if "modalities_out" in table:
        fields["modalities_out"] = _text_list(
            table["modalities_out"], f"{where}.modalities_out",
            allowed=MODALITIES)
    if "preferred_families" in table:
        fields["preferred_families"] = _text_list(
            table["preferred_families"], f"{where}.preferred_families",
            max_items=64)
    if "preferred_types" in table:
        fields["preferred_types"] = _text_list(
            table["preferred_types"], f"{where}.preferred_types",
            allowed=TYPES)
    if "description" in table:
        fields["description"] = _text(
            table["description"], f"{where}.description")
    if "notes" in table:
        fields["notes"] = _text(
            table["notes"], f"{where}.notes", allow_empty=True)
    return fields


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelNode:
    """One model on the graph, with curated or inferred metadata."""

    id: str
    provider: str
    model: str
    family: str
    variant: str
    type: str
    size_class: str
    params_total_b: float | None
    params_active_b: float | None
    context: int | None
    input_cost: float | None
    output_cost: float | None
    modalities_in: tuple[str, ...]
    modalities_out: tuple[str, ...]
    capabilities: tuple[str, ...]
    tool_formats: tuple[str, ...]
    local: bool | None
    status: str
    source: str
    confidence: str
    notes: str
    catalog_source: str
    profile_source: str

    @property
    def remote(self) -> bool | None:
        return None if self.local is None else not self.local

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "family": self.family,
            "variant": self.variant,
            "type": self.type,
            "size_class": self.size_class,
            "params_total_b": self.params_total_b,
            "params_active_b": self.params_active_b,
            "context": self.context,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "modalities_in": list(self.modalities_in),
            "modalities_out": list(self.modalities_out),
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
            "local": self.local,
            "remote": self.remote,
            "status": self.status,
            "source": self.source,
            "confidence": self.confidence,
            "notes": self.notes,
            "catalog_source": self.catalog_source,
            "profile_source": self.profile_source,
        }


def _infer_variant(name: str) -> str:
    for token in _VARIANT_TOKENS:
        if token in name:
            return token
    match = _PARAM_RE.search(name)
    return f"{match.group(1)}b" if match else "default"


def _infer_family(name: str, provider: str) -> str:
    for token in _FAMILY_TOKEN_RE.findall(name.lower()):
        if token in _STOP_FAMILY_TOKENS:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            continue
        cleaned = re.sub(r"\d+$", "", token)
        if cleaned and cleaned not in _STOP_FAMILY_TOKENS:
            return cleaned
    cleaned = re.sub(r"[^a-z0-9_.-]+", "", provider.lower())
    return cleaned or "unknown"


def _infer_type(name: str) -> str:
    if any(token in name for token in ("embed", "bge-", "e5-")):
        return "embedding"
    if any(token in name for token in (
            "dall-e", "imagen", "flux", "stable-diffusion", "-image-",
            "image-gen")):
        return "image"
    if any(token in name for token in ("whisper", "tts", "text-to-speech")):
        return "audio"
    if any(token in name for token in ("coder", "-code", "codex", "codestral",
                                       "devstral")):
        return "code"
    if any(token in name for token in ("reason", "thinking", "o1", "o3",
                                       "o4", "-r1")):
        return "reasoning"
    if any(token in name for token in ("vision", "multimodal", "omni")):
        return "multimodal"
    return "chat"


def _size_for_params(params: float | None) -> str:
    if params is None:
        return "unknown"
    if params < 4:
        return "tiny"
    if params < 15:
        return "small"
    if params < 80:
        return "medium"
    if params < 400:
        return "large"
    return "frontier"


def _inferred_fields(model: str, provider: str) -> dict[str, Any]:
    name = model.lower()
    params_match = _PARAM_RE.search(name)
    params = (float(params_match.group(1))
              if params_match and float(params_match.group(1)) <= 100_000
              else None)
    active_match = _ACTIVE_RE.search(name)
    active = (float(active_match.group(1))
              if active_match and float(active_match.group(1)) <= 100_000
              else None)
    if active is not None and params is None:
        params = active
    inferred_type = _infer_type(name)
    modalities_in = ("text",)
    modalities_out = ("text",)
    if inferred_type == "multimodal":
        modalities_in = ("text", "image")
    elif inferred_type == "embedding":
        modalities_out = ("embedding",)
    elif inferred_type == "image":
        modalities_in = ("text", "image")
        modalities_out = ("image",)
    elif inferred_type == "audio":
        if any(token in name for token in ("whisper", "speech-to-text")):
            modalities_in = ("audio",)
        else:
            modalities_out = ("audio",)
    return {
        "family": _infer_family(name, provider),
        "variant": _infer_variant(name),
        "type": inferred_type,
        "size_class": _size_for_params(params),
        "params_total_b": params,
        "params_active_b": active,
        "context": None,
        "input_cost": None,
        "output_cost": None,
        "modalities_in": modalities_in,
        "modalities_out": modalities_out,
        "capabilities": (),
        "tool_formats": (),
        "local": None,
        "source": "inferred",
        "confidence": "inferred",
        "notes": "family and size inferred from the model id; values are approximate",
        "profile_source": "inferred from provider/model id",
    }


def _profile_fields(profile: Profile) -> dict[str, Any]:
    return {
        "family": profile.family,
        "variant": profile.variant,
        "type": profile.type,
        "size_class": profile.size_class,
        "params_total_b": profile.params_total_b,
        "params_active_b": profile.params_active_b,
        "context": profile.context,
        "input_cost": profile.input_cost,
        "output_cost": profile.output_cost,
        "modalities_in": profile.modalities_in,
        "modalities_out": profile.modalities_out,
        "capabilities": profile.capabilities,
        "tool_formats": profile.tool_formats,
        "local": profile.local,
        "source": "profile",
        "confidence": profile.confidence,
        "notes": profile.notes,
        "profile_source": profile.source or f"curated profile: {profile.family}",
    }


def _match_profile(provider: str, model: str, config: GraphConfig
                   ) -> Profile | None:
    name = model.lower()
    provider_name = provider.lower()
    for profile in (*config.profiles, *builtin_profiles()):
        if profile.providers and not any(
                fnmatch.fnmatchcase(provider_name, pattern)
                for pattern in profile.providers):
            continue
        if any(fnmatch.fnmatchcase(name, pattern)
               for pattern in profile.match):
            return profile
    return None


def _build_node(record: M.ModelRecord, config: GraphConfig,
                external: Mapping[str, Mapping[str, Any]] | None = None
                ) -> ModelNode:
    profile = _match_profile(record.provider, record.model, config)
    fields = (_profile_fields(profile) if profile is not None
              else _inferred_fields(record.model, record.provider))
    external_fields = (external or {}).get(record.id)
    if external_fields:
        fields.update(external_fields)
        if (external_fields.get("params_total_b") is not None
                and "size_class" not in external_fields):
            fields["size_class"] = _size_for_params(
                external_fields["params_total_b"])
        if "source" not in external_fields:
            fields["source"] = "external"
        if "confidence" not in external_fields:
            fields["confidence"] = "external"
        if "profile_source" not in external_fields:
            fields["profile_source"] = "external model metadata"
    override = config.model_override(record.id)
    if override is not None:
        fields.update(override)
        fields["source"] = override.get("source", "override")
        fields["confidence"] = override.get("confidence", "declared")
        if "profile_source" not in override:
            fields["profile_source"] = f"{CONFIG_SECTION}.models"

    context = record.context if record.context is not None else fields["context"]
    input_cost = (record.input_cost if record.input_cost is not None
                  else fields["input_cost"])
    output_cost = (record.output_cost if record.output_cost is not None
                   else fields["output_cost"])
    capabilities = tuple(sorted(
        set(record.capabilities) | set(fields["capabilities"])))
    local = record.local if record.local is not None else fields["local"]
    return ModelNode(
        id=record.id,
        provider=record.provider,
        model=record.model,
        family=fields["family"],
        variant=fields["variant"],
        type=fields["type"],
        size_class=fields["size_class"],
        params_total_b=fields["params_total_b"],
        params_active_b=fields["params_active_b"],
        context=context,
        input_cost=input_cost,
        output_cost=output_cost,
        modalities_in=tuple(fields["modalities_in"]),
        modalities_out=tuple(fields["modalities_out"]),
        capabilities=capabilities,
        tool_formats=tuple(fields["tool_formats"]),
        local=local,
        status=record.status,
        source=fields["source"],
        confidence=fields["confidence"],
        notes=fields["notes"],
        catalog_source=record.source,
        profile_source=fields["profile_source"],
    )


def _build_nodes(item: M.Catalog, config: GraphConfig,
                 external: Mapping[str, Mapping[str, Any]] | None = None
                 ) -> tuple[ModelNode, ...]:
    return tuple(_build_node(record, config, external)
                 for record in item.records)


def _parse_external(external: Mapping[str, Mapping[str, Any]] | None
                    ) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Validate producer-provided metadata keyed by model id.

    This is the seam for a later cached models.dev/LiteLLM/OpenRouter/Epoch/HF
    importer. It performs no IO: callers pass plain mappings. Unknown fields
    are reported, known fields with invalid types are rejected.
    """
    if external is None:
        return {}, ()
    if not isinstance(external, Mapping):
        raise ModelGraphError("external must be a mapping of model id to fields")
    parsed: dict[str, dict[str, Any]] = {}
    unknown: list[str] = []
    for raw_id, raw_table in external.items():
        model_id = M.validate_model_id(raw_id, where="external")
        where = f'external."{model_id}"'
        if not isinstance(raw_table, Mapping):
            raise ModelGraphError(f"{where}: must be a table")
        parsed[model_id] = _parse_profile_fields(raw_table, where, unknown)
    return parsed, tuple(sorted(set(unknown)))


# --------------------------------------------------------------------------- #
# distance
# --------------------------------------------------------------------------- #
def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalise_weights(weights: Mapping[str, Any] | None
                       ) -> dict[str, float]:
    result = dict(DEFAULT_WEIGHTS)
    if weights is None:
        return result
    if not isinstance(weights, Mapping):
        raise ModelGraphError("weights must be a mapping")
    for key, value in weights.items():
        if not isinstance(key, str) or key not in FEATURES:
            raise ModelGraphError(f"weights: unknown feature {key!r}")
        result[key] = _number(value, f"weights.{key}", minimum=0.0,
                              maximum=1000.0)
    return result


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)


def _type_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    return _TYPE_AFFINITY.get((left, right),
                              _TYPE_AFFINITY.get((right, left), 0.0))


def _cost_value(value: Any) -> float | None:
    input_cost = _field(value, "input_cost")
    output_cost = _field(value, "output_cost")
    known = [float(cost) for cost in (input_cost, output_cost)
             if cost is not None]
    if not known:
        return None
    return sum(known) / len(known)


def _params_value(value: Any) -> float | None:
    total = _field(value, "params_total_b")
    active = _field(value, "params_active_b")
    if total is not None:
        return float(total)
    if active is not None:
        return float(active)
    return None


def _log_similarity(left: float, right: float, ceiling: float) -> float:
    span = math.log1p(ceiling)
    if span <= 0:
        return 1.0
    return max(0.0, 1.0 - abs(math.log1p(max(0.0, left))
                              - math.log1p(max(0.0, right))) / span)


def distance(a: Any, b: Any, weights: Mapping[str, Any] | None = None
             ) -> dict[str, Any]:
    """Weighted Gower-style distance between two nodes or node mappings.

    Known features contribute; a feature unknown for either side is skipped
    and reported in ``missing``. ``coverage`` is the share of the configured
    weight that was comparable. Identical ids are exactly ``0.0``. The result
    is symmetric, deterministic and bounded in ``0..1``, but it is not a
    strict metric: skipping missing features means it is not transitive.
    """
    effective = _normalise_weights(weights)
    total_weight = sum(effective.values())
    components: dict[str, float | None] = {}
    missing: list[str] = []

    left_id = _field(a, "id")
    right_id = _field(b, "id")
    if left_id is not None and right_id is not None and left_id == right_id:
        return {
            "distance": 0.0,
            "similarity": 1.0,
            "components": {feature: 1.0 for feature in FEATURES},
            "weights_used": dict(effective),
            "coverage": 1.0,
            "missing": [],
        }

    left_family = _field(a, "family")
    right_family = _field(b, "family")
    if left_family and right_family:
        components["family"] = 1.0 if left_family == right_family else 0.0
    else:
        components["family"] = None

    left_variant = _field(a, "variant")
    right_variant = _field(b, "variant")
    if left_variant and right_variant:
        components["variant"] = 1.0 if left_variant == right_variant else 0.0
    else:
        components["variant"] = None

    left_type = _field(a, "type")
    right_type = _field(b, "type")
    if left_type and right_type:
        components["type"] = _type_similarity(left_type, right_type)
    else:
        components["type"] = None

    left_size = _field(a, "size_class")
    right_size = _field(b, "size_class")
    if left_size in SIZE_RANK and right_size in SIZE_RANK:
        components["size"] = 1.0 - abs(
            SIZE_RANK[left_size] - SIZE_RANK[right_size]) / 4.0
    else:
        components["size"] = None

    left_params = _params_value(a)
    right_params = _params_value(b)
    if left_params is not None and right_params is not None:
        components["params"] = _log_similarity(
            left_params, right_params, MAX_PARAMS_FOR_DISTANCE)
    else:
        components["params"] = None

    left_context = _field(a, "context")
    right_context = _field(b, "context")
    if left_context is not None and right_context is not None:
        components["context"] = _log_similarity(
            float(left_context), float(right_context),
            MAX_CONTEXT_FOR_DISTANCE)
    else:
        components["context"] = None

    left_cost = _cost_value(a)
    right_cost = _cost_value(b)
    if left_cost is not None and right_cost is not None:
        components["cost"] = _log_similarity(
            left_cost, right_cost, MAX_COST_FOR_DISTANCE)
    else:
        components["cost"] = None

    left_caps = tuple(_field(a, "capabilities", ()) or ())
    right_caps = tuple(_field(b, "capabilities", ()) or ())
    if left_caps and right_caps:
        components["capabilities"] = _jaccard(left_caps, right_caps)
    else:
        components["capabilities"] = None

    left_modalities = {
        f"in:{name}" for name in tuple(_field(a, "modalities_in", ()) or ())
    } | {
        f"out:{name}" for name in tuple(_field(a, "modalities_out", ()) or ())
    }
    right_modalities = {
        f"in:{name}" for name in tuple(_field(b, "modalities_in", ()) or ())
    } | {
        f"out:{name}" for name in tuple(_field(b, "modalities_out", ()) or ())
    }
    if left_modalities and right_modalities:
        components["modalities"] = _jaccard(
            left_modalities, right_modalities)
    else:
        components["modalities"] = None

    left_tools = tuple(_field(a, "tool_formats", ()) or ())
    right_tools = tuple(_field(b, "tool_formats", ()) or ())
    if left_tools and right_tools:
        components["tool_formats"] = _jaccard(left_tools, right_tools)
    else:
        components["tool_formats"] = None

    weights_used: dict[str, float] = {}
    weighted = 0.0
    known_weight = 0.0
    for feature in FEATURES:
        similarity = components[feature]
        if similarity is None:
            missing.append(feature)
            continue
        weight = effective[feature]
        if weight <= 0:
            continue
        weights_used[feature] = weight
        weighted += weight * max(0.0, min(1.0, similarity))
        known_weight += weight
    coverage = known_weight / total_weight if total_weight else 0.0
    if known_weight <= 0:
        value = 1.0
    else:
        value = 1.0 - (weighted / known_weight)
    value = round(max(0.0, min(1.0, value)), 4)
    return {
        "distance": value,
        "similarity": round(1.0 - value, 4),
        "components": {
            feature: (None if components[feature] is None
                      else round(float(components[feature]), 4))
            for feature in FEATURES
        },
        "weights_used": weights_used,
        "coverage": round(coverage, 4),
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Edge:
    """One bounded graph edge."""

    kind: str
    source: str
    target: str
    weight: float
    similarity: float
    coverage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "weight": self.weight,
            "similarity": self.similarity,
            "coverage": self.coverage,
        }


@dataclass(frozen=True)
class ModelGraph:
    """The bounded model graph and the catalog it came from."""

    root: Path
    nodes: tuple[ModelNode, ...]
    edges: tuple[Edge, ...]
    discovery: M.Discovery
    weights: dict[str, float]
    config: GraphConfig
    families: tuple[tuple[str, int], ...]
    providers: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    omitted_nodes: int = 0
    omitted_edges: int = 0

    def get(self, model_id: str) -> ModelNode | None:
        text = M.validate_model_id(model_id)
        for node in self.nodes:
            if node.id == text:
                return node
        return None

    def neighbours(self, model_id: str, limit: int = 5
                   ) -> tuple[dict[str, Any], ...]:
        limit = _limit(limit, "limit")
        node = self.get(model_id)
        if node is None:
            raise ModelGraphError(
                f"model {model_id!r} is not in the model graph")
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for other in self.nodes:
            if other.id == node.id:
                continue
            result = distance(node, other, self.weights)
            ranked.append((
                -result["similarity"], other.id,
                {
                    "id": other.id,
                    "family": other.family,
                    "type": other.type,
                    "size_class": other.size_class,
                    "status": other.status,
                    "local": other.local,
                    "distance": result["distance"],
                    "similarity": result["similarity"],
                    "coverage": result["coverage"],
                },
            ))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "root": str(self.root),
            "discovery": self.discovery.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "counts": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "families": len(self.families),
                "providers": len(self.providers),
            },
            "families": dict(self.families),
            "providers": dict(self.providers),
            "weights": dict(self.weights),
            "config": self.config.to_dict(),
            "unknown_keys": list(self.config.unknown_keys),
            "warnings": list(self.warnings),
            "errors": list(self.config.errors),
            "truncated": self.truncated,
            "omitted_nodes": self.omitted_nodes,
            "omitted_edges": self.omitted_edges,
        }


def _limit(value: Any, where: str, *, maximum: int = MATCH_LIMIT_MAX) -> int:
    return _integer(value, where, minimum=1, maximum=maximum)


def _build_edges(nodes: Sequence[ModelNode], weights: Mapping[str, float],
                 *, top_k: int, threshold: float, max_edges: int
                 ) -> tuple[tuple[Edge, ...], int]:
    edges: list[Edge] = []

    def add(kind: str, left: ModelNode, right: ModelNode, weight: float) -> None:
        result = distance(left, right, weights)
        edges.append(Edge(
            kind=kind,
            source=left.id,
            target=right.id,
            weight=round(weight, 4),
            similarity=result["similarity"],
            coverage=result["coverage"],
        ))

    by_family: dict[str, list[ModelNode]] = {}
    by_provider: dict[str, list[ModelNode]] = {}
    for node in nodes:
        by_family.setdefault(node.family, []).append(node)
        by_provider.setdefault(node.provider, []).append(node)
    for family in sorted(by_family):
        group = sorted(by_family[family], key=lambda node: node.id)
        for left, right in zip(group, group[1:]):
            add("family", left, right, 1.0)
    for provider in sorted(by_provider):
        group = sorted(by_provider[provider], key=lambda node: node.id)
        for left, right in zip(group, group[1:]):
            add("provider", left, right, 0.75)

    similarity: dict[tuple[str, str], Edge] = {}
    node_list = list(nodes)
    for node in node_list:
        scored: list[tuple[float, str, ModelNode, dict[str, Any]]] = []
        for other in node_list:
            if other.id == node.id:
                continue
            result = distance(node, other, weights)
            if result["similarity"] < threshold:
                continue
            scored.append((-result["similarity"], other.id, other, result))
        scored.sort(key=lambda item: (item[0], item[1]))
        for _neg, _other_id, other, result in scored[:top_k]:
            source, target = sorted((node.id, other.id))
            key = (source, target)
            if key in similarity:
                continue
            similarity[key] = Edge(
                kind="similarity",
                source=source,
                target=target,
                weight=result["similarity"],
                similarity=result["similarity"],
                coverage=result["coverage"],
            )
    ordered_similarity = sorted(
        similarity.values(),
        key=lambda edge: (-edge.similarity, edge.source, edge.target))
    edges.extend(ordered_similarity)

    omitted = max(0, len(edges) - max_edges)
    return tuple(edges[:max_edges]), omitted


def graph(root: str | Path | None = None, *, runner: M.Runner | None = None,
          timeout_s: float = M.DISCOVERY_TIMEOUT_S,
          overrides: Mapping[str, Any] | None = None,
          catalog: M.Catalog | None = None,
          external: Mapping[str, Mapping[str, Any]] | None = None,
          max_nodes: int = MAX_NODES,
          max_edges: int = MAX_EDGES,
          top_k: int = DEFAULT_TOP_K,
          similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
          ) -> ModelGraph:
    """Build the bounded model graph. Reads only; never calls a model.

    ``external`` is an optional, already-cached mapping of model id to profile
    fields (for example a later models.dev importer). It is merged after the
    curated pattern/inferred layer and before an explicit
    ``[model_graph.models]`` override, so the precedence is explicit override >
    external metadata > curated profile > inferred. Nothing is fetched here.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    max_nodes = _integer(max_nodes, "max_nodes", minimum=1,
                         maximum=MAX_NODES_LIMIT)
    max_edges = _integer(max_edges, "max_edges", minimum=0,
                         maximum=MAX_EDGES_LIMIT)
    top_k = _integer(top_k, "top_k", minimum=1, maximum=50)
    threshold = _number(similarity_threshold, "similarity_threshold",
                        maximum=1.0)
    item = catalog if catalog is not None else M.catalog(
        root_p, runner=runner, timeout_s=timeout_s, overrides=overrides)
    config = _load_graph_config(root_p)
    external_fields, external_unknown = _parse_external(external)
    if external_unknown:
        config = replace(
            config,
            unknown_keys=tuple(sorted(set(config.unknown_keys)
                                     | set(external_unknown))))
    weights = _normalise_weights(dict(config.weights))

    all_nodes = _build_nodes(item, config, external_fields)
    omitted_nodes = max(0, len(all_nodes) - max_nodes)
    nodes = all_nodes[:max_nodes]
    edges, omitted_edges = _build_edges(
        nodes, weights, top_k=top_k, threshold=threshold, max_edges=max_edges)

    warnings = list(item.warnings)
    warnings.extend(config.warnings)
    for error in config.errors:
        warnings.append(f"model_graph configuration: {error}")
    for unknown in config.unknown_keys:
        warnings.append(f"unknown model_graph key: {unknown}")
    known_ids = {record.id for record in item.records}
    for model_id, _fields in config.models:
        if model_id not in known_ids:
            warnings.append(
                f"model_graph override for {model_id!r} is not in the "
                "catalog; it was not added as a node")
    for model_id in external_fields:
        if model_id not in known_ids:
            warnings.append(
                f"external metadata for {model_id!r} is not in the catalog; "
                "it was not added as a node")
    if omitted_nodes:
        warnings.append(
            f"model graph truncated to {max_nodes} nodes; "
            f"{omitted_nodes} omitted")
    if omitted_edges:
        warnings.append(
            f"model graph truncated to {max_edges} edges; "
            f"{omitted_edges} omitted")

    family_counts: dict[str, int] = {}
    provider_counts: dict[str, int] = {}
    for node in nodes:
        family_counts[node.family] = family_counts.get(node.family, 0) + 1
        provider_counts[node.provider] = (
            provider_counts.get(node.provider, 0) + 1)
    return ModelGraph(
        root=root_p,
        nodes=nodes,
        edges=edges,
        discovery=item.discovery,
        weights=weights,
        config=config,
        families=tuple(sorted(family_counts.items())),
        providers=tuple(sorted(provider_counts.items())),
        warnings=tuple(warnings),
        truncated=bool(omitted_nodes or omitted_edges),
        omitted_nodes=omitted_nodes,
        omitted_edges=omitted_edges,
    )


def neighbours(root: str | Path | None = None, model_id: str = "", *,
               limit: int = 5, runner: M.Runner | None = None,
               timeout_s: float = M.DISCOVERY_TIMEOUT_S,
               overrides: Mapping[str, Any] | None = None,
               catalog: M.Catalog | None = None,
               external: Mapping[str, Mapping[str, Any]] | None = None,
               max_nodes: int = MAX_NODES) -> tuple[dict[str, Any], ...]:
    """Nearest models to ``model_id`` by :func:`distance`, deterministic order."""
    item = graph(root, runner=runner, timeout_s=timeout_s, overrides=overrides,
                 catalog=catalog, external=external, max_nodes=max_nodes)
    return item.neighbours(model_id, limit)


#: Alias for callers that prefer the planner's word.
similar = neighbours


# --------------------------------------------------------------------------- #
# task profiles and requirements
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TaskProfile:
    """Built-in advisory requirements for one task class."""

    task: str
    description: str
    capabilities: tuple[str, ...] = ()
    tool_formats: tuple[str, ...] = ()
    local_only: bool = False
    min_context: int | None = None
    max_cost: float | None = None
    size: str | None = None
    type: str | None = None
    modalities_in: tuple[str, ...] = ()
    modalities_out: tuple[str, ...] = ()
    preferred_families: tuple[str, ...] = ()
    preferred_types: tuple[str, ...] = ()
    source: str = "builtin"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
            "local_only": self.local_only,
            "min_context": self.min_context,
            "max_cost": self.max_cost,
            "size": self.size,
            "type": self.type,
            "modalities_in": list(self.modalities_in),
            "modalities_out": list(self.modalities_out),
            "preferred_families": list(self.preferred_families),
            "preferred_types": list(self.preferred_types),
            "source": self.source,
            "notes": self.notes,
        }


def _task(name: str, description: str, **values: Any) -> TaskProfile:
    return TaskProfile(task=name, description=description, **values)


TASK_PROFILES: dict[str, TaskProfile] = {
    "code_review": _task(
        "code_review", "Review a code change for correctness and risk.",
        capabilities=("tools", "code", "reasoning", "long_context"),
        tool_formats=("apply_patch", "replace", "library_call", "mcp"),
        min_context=64000, size="large", type="reasoning",
        preferred_families=("claude", "gpt", "deepseek"),
    ),
    "diff_review": _task(
        "diff_review", "Review one diff and return findings.",
        capabilities=("tools", "code", "structured_output"),
        tool_formats=("apply_patch", "replace", "library_call"),
        min_context=32000, size="medium", type="code",
        preferred_types=("code", "reasoning"),
        preferred_families=("qwen", "deepseek", "gpt", "claude"),
    ),
    "paper_triage": _task(
        "paper_triage", "Triage a paper or PDF collection.",
        capabilities=("long_context", "pdf_native", "reasoning",
                      "structured_output"),
        tool_formats=("native_pdf", "library_call", "mcp"),
        modalities_in=("pdf", "text"), min_context=128000, size="large",
        type="reasoning",
        preferred_families=("claude", "gemini", "gpt"),
    ),
    "repo_scan": _task(
        "repo_scan", "Scan a repository for opportunities and gaps.",
        capabilities=("tools", "code", "long_context"),
        tool_formats=("replace", "library_call", "cli", "mcp"),
        min_context=64000, size="large", type="code",
        preferred_families=("deepseek", "qwen", "gpt", "claude"),
    ),
    "test_triage": _task(
        "test_triage", "Explain a failing test and propose a fix.",
        capabilities=("tools", "code", "reasoning"),
        tool_formats=("apply_patch", "replace", "cli"),
        min_context=32000, size="medium", type="code",
        preferred_types=("code", "reasoning"),
        preferred_families=("deepseek", "qwen", "gpt", "claude"),
    ),
    "pdf_edit": _task(
        "pdf_edit", "Edit a PDF with structural and visual checks.",
        capabilities=("tools", "pdf_native", "vision", "structured_output"),
        tool_formats=("native_pdf", "whole_file", "replace", "library_call"),
        modalities_in=("pdf", "text"), min_context=128000, size="large",
        type="multimodal",
        preferred_families=("gemini", "claude", "gpt"),
        notes="Prefer a native PDF path; a rendered-image fallback is separate.",
    ),
    "document_extract": _task(
        "document_extract", "Extract structured data from PDFs and images.",
        capabilities=("pdf_native", "vision", "structured_output",
                      "long_context"),
        tool_formats=("native_pdf", "library_call", "cli", "mcp"),
        modalities_in=("pdf", "image", "text"), min_context=128000,
        size="large", type="multimodal",
        preferred_families=("gemini", "claude", "gpt"),
    ),
    "image_review": _task(
        "image_review", "Review a diagram or screenshot.",
        capabilities=("vision", "structured_output", "reasoning"),
        tool_formats=("library_call", "mcp"),
        modalities_in=("image", "text"), min_context=32000, size="medium",
        type="multimodal",
        preferred_families=("gemini", "gpt", "claude"),
    ),
    "research_summarize": _task(
        "research_summarize", "Summarise long research material.",
        capabilities=("long_context", "reasoning", "structured_output"),
        tool_formats=("library_call", "mcp"),
        modalities_in=("text", "pdf"), min_context=128000, size="large",
        type="reasoning",
        preferred_families=("claude", "gemini", "gpt"),
    ),
    "model_classify": _task(
        "model_classify", "Advise a model choice with confidence and reason.",
        capabilities=("structured_output", "reasoning"),
        tool_formats=("library_call", "mcp"),
        min_context=32000, size="medium", type="reasoning",
        preferred_families=("qwen", "gpt", "claude", "deepseek"),
        notes="Advisory only; this module never invokes a model.",
    ),
    "module_build": _task(
        "module_build", "Build a selected module behind review gates.",
        capabilities=("tools", "code", "long_context", "structured_output"),
        tool_formats=("apply_patch", "replace", "cli", "mcp"),
        min_context=64000, size="large", type="code",
        preferred_families=("claude", "gpt", "deepseek", "qwen"),
    ),
    "log_triage": _task(
        "log_triage", "Cluster failures in logs and propose fixes.",
        capabilities=("tools", "code", "structured_output", "long_context"),
        tool_formats=("replace", "library_call", "cli", "mcp"),
        min_context=64000, size="medium", type="reasoning",
        preferred_families=("gpt", "claude", "deepseek", "qwen"),
    ),
    "security_review": _task(
        "security_review", "Review a change for security and antipatterns.",
        capabilities=("tools", "code", "reasoning", "long_context"),
        tool_formats=("apply_patch", "replace", "library_call", "cli", "mcp"),
        min_context=128000, size="large", type="reasoning",
        preferred_families=("claude", "gpt", "deepseek"),
    ),
}


@dataclass(frozen=True)
class Requirements:
    """Effective advisory requirements after a task profile and arguments."""

    task: str | None = None
    capabilities: tuple[str, ...] = ()
    tool_formats: tuple[str, ...] = ()
    local_only: bool = False
    min_context: int | None = None
    max_cost: float | None = None
    size: str | None = None
    type: str | None = None
    modalities_in: tuple[str, ...] = ()
    modalities_out: tuple[str, ...] = ()
    preferred_families: tuple[str, ...] = ()
    preferred_types: tuple[str, ...] = ()
    source: str = "explicit"
    notes: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
            "local_only": self.local_only,
            "min_context": self.min_context,
            "max_cost": self.max_cost,
            "size": self.size,
            "type": self.type,
            "modalities_in": list(self.modalities_in),
            "modalities_out": list(self.modalities_out),
            "preferred_families": list(self.preferred_families),
            "preferred_types": list(self.preferred_types),
            "source": self.source,
            "notes": self.notes,
            "warnings": list(self.warnings),
        }


def _merge_task_profile(task: str, config: GraphConfig
                        ) -> tuple[TaskProfile | None, str,
                                   tuple[str, ...]]:
    base = TASK_PROFILES.get(task)
    override = config.task_override(task)
    warnings: list[str] = []
    if base is None and override is None:
        warnings.append(f"no built-in task profile for {task!r}")
        return None, "explicit", tuple(warnings)
    values = (base.to_dict() if base is not None else {
        "task": task, "description": "", "capabilities": [],
        "tool_formats": [], "local_only": False, "min_context": None,
        "max_cost": None, "size": None, "type": None,
        "modalities_in": [], "modalities_out": [],
        "preferred_families": [], "preferred_types": [],
        "source": "builtin", "notes": "",
    })
    values.update(override or {})
    profile = TaskProfile(
        task=task,
        description=values["description"],
        capabilities=tuple(values["capabilities"]),
        tool_formats=tuple(values["tool_formats"]),
        local_only=values["local_only"],
        min_context=values["min_context"],
        max_cost=values["max_cost"],
        size=values["size"],
        type=values["type"],
        modalities_in=tuple(values["modalities_in"]),
        modalities_out=tuple(values["modalities_out"]),
        preferred_families=tuple(values["preferred_families"]),
        preferred_types=tuple(values["preferred_types"]),
        source="config" if override else "builtin",
        notes=values["notes"],
    )
    source = "config:task" if override is not None else "builtin:task"
    if override is not None and config.files:
        warnings.append(
            f"task {task!r} overridden by {CONFIG_SECTION}.tasks")
    return profile, source, tuple(warnings)


def requirements(root: str | Path | None = None, task: str | None = None,
                 capabilities: Sequence[str] = (),
                 tool_formats: Sequence[str] = (),
                 local_only: bool = False, min_context: int | None = None,
                 max_cost: float | None = None, size: str | None = None,
                 type: str | None = None,
                 modalities_in: Sequence[str] = (),
                 modalities_out: Sequence[str] = (),
                 ) -> Requirements:
    """Merge a built-in task profile with explicit requirements.

    Explicit capabilities and tool formats *extend* the task profile. Explicit
    ``local_only``, ``min_context``, ``max_cost``, ``size`` and ``type`` win
    when given; ``size`` is a preference (maximum acceptable class), while
    ``type`` is a hard filter in :func:`match`. Repository
    ``[model_graph.tasks.<class>]`` values override the built-in profile.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    config = _load_graph_config(root_p)
    warnings: list[str] = []
    selected_caps = _text_list(list(capabilities), "capabilities",
                               allowed=CAPABILITIES)
    selected_tools = _text_list(list(tool_formats), "tool_formats",
                                allowed=TOOL_FORMATS)
    selected_modalities_in = _text_list(
        list(modalities_in), "modalities_in", allowed=MODALITIES)
    selected_modalities_out = _text_list(
        list(modalities_out), "modalities_out", allowed=MODALITIES)
    if task is not None:
        task = _text(task, "task", max_len=64)
        if not _TASK_CLASS_RE.fullmatch(task):
            raise _fail("task", "must be a bounded task class")

    profile: TaskProfile | None = None
    source = "explicit"
    if task is not None:
        profile, source, task_warnings = _merge_task_profile(task, config)
        warnings.extend(task_warnings)

    if size is not None:
        size = _choice(size, SIZE_CLASSES[:-1], "size")
    if type is not None:
        type = _choice(type, TYPES, "type")
    if min_context is not None:
        min_context = _integer(min_context, "min_context", minimum=1,
                               maximum=100_000_000)
    if max_cost is not None:
        max_cost = _number(max_cost, "max_cost", maximum=100_000.0)
    local_only = _boolean(local_only, "local_only")

    merged_caps = tuple(sorted(set(
        (profile.capabilities if profile else ()) + selected_caps)))
    merged_tools = tuple(sorted(set(
        (profile.tool_formats if profile else ()) + selected_tools)))
    merged_in = tuple(sorted(set(
        (profile.modalities_in if profile else ()) + selected_modalities_in)))
    merged_out = tuple(sorted(set(
        (profile.modalities_out if profile else ()) + selected_modalities_out)))

    return Requirements(
        task=task,
        capabilities=merged_caps,
        tool_formats=merged_tools,
        local_only=(local_only or (profile.local_only if profile else False)),
        min_context=(min_context if min_context is not None
                     else (profile.min_context if profile else None)),
        max_cost=(max_cost if max_cost is not None
                  else (profile.max_cost if profile else None)),
        size=(size if size is not None
              else (profile.size if profile else None)),
        type=type,
        modalities_in=merged_in,
        modalities_out=merged_out,
        preferred_families=(profile.preferred_families if profile else ()),
        preferred_types=(profile.preferred_types if profile else ()),
        source=source,
        notes=(profile.notes if profile else ""),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchCandidate:
    id: str
    score: float
    family: str
    type: str
    size_class: str
    status: str
    local: bool | None
    confidence: str
    reasons: tuple[str, ...]
    components: tuple[tuple[str, float], ...]
    capabilities: tuple[str, ...] = ()
    tool_formats: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": self.score,
            "family": self.family,
            "type": self.type,
            "size_class": self.size_class,
            "status": self.status,
            "local": self.local,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "components": dict(self.components),
            "capabilities": list(self.capabilities),
            "tool_formats": list(self.tool_formats),
        }


@dataclass(frozen=True)
class MatchResult:
    root: Path
    requirements: Requirements
    selected: MatchCandidate | None
    candidates: tuple[MatchCandidate, ...]
    excluded: tuple[dict[str, Any], ...]
    not_assessed: tuple[dict[str, Any], ...]
    discovery: M.Discovery
    warnings: tuple[str, ...] = ()
    unknown_keys: tuple[str, ...] = ()
    source: str = "advisory"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "root": str(self.root),
            "source": self.source,
            "advisory": True,
            "task": self.requirements.task,
            "requirements": self.requirements.to_dict(),
            "selected": (self.selected.to_dict()
                         if self.selected is not None else None),
            "candidates": [candidate.to_dict()
                           for candidate in self.candidates],
            "excluded": list(self.excluded),
            "not_assessed": list(self.not_assessed),
            "discovery": self.discovery.to_dict(),
            "warnings": list(self.warnings),
            "unknown_keys": list(self.unknown_keys),
        }


def _cost_text(value: float | None) -> str:
    return "unknown" if value is None else f"${value:g}"


def _filter_reasons(node: ModelNode, req: Requirements) -> list[str]:
    reasons: list[str] = []
    if req.local_only and node.local is not True:
        reasons.append("local_only: model is not declared local")
    if req.min_context is not None:
        if node.context is None:
            reasons.append(
                f"context unknown; cannot verify >= {req.min_context}")
        elif node.context < req.min_context:
            reasons.append(
                f"context {node.context} < {req.min_context}")
    if req.max_cost is not None:
        cost = _cost_value(node)
        if cost is None:
            reasons.append(
                f"cost unknown; cannot verify <= {_cost_text(req.max_cost)}")
        elif cost > req.max_cost:
            reasons.append(
                f"cost {_cost_text(cost)} > {_cost_text(req.max_cost)}")
    missing_caps = sorted(set(req.capabilities) - set(node.capabilities))
    if missing_caps:
        reasons.append("missing capabilities: " + ", ".join(missing_caps))
    if req.type is not None and node.type != req.type:
        reasons.append(f"type {node.type} != required {req.type}")
    missing_in = sorted(set(req.modalities_in) - set(node.modalities_in))
    if missing_in:
        reasons.append("missing input modalities: " + ", ".join(missing_in))
    missing_out = sorted(set(req.modalities_out) - set(node.modalities_out))
    if missing_out:
        reasons.append("missing output modalities: " + ", ".join(missing_out))
    return reasons


def _size_score(node_size: str, requested: str | None) -> tuple[float, str]:
    if requested is None:
        return 1.0, "size not constrained"
    if node_size not in SIZE_RANK:
        return 0.2, f"size {node_size} is unknown"
    requested_rank = SIZE_RANK[requested]
    node_rank = SIZE_RANK[node_size]
    if node_rank <= requested_rank:
        if node_rank == requested_rank:
            return 1.0, f"size {node_size} matches preferred {requested}"
        return 0.85, f"size {node_size} is below preferred {requested}"
    return 0.0, f"size {node_size} exceeds preferred {requested}"


def _match_score(node: ModelNode, req: Requirements
                 ) -> tuple[float, dict[str, float], tuple[str, ...]]:
    reasons: list[str] = []
    components: dict[str, float] = {}

    required_caps = set(req.capabilities)
    if required_caps:
        covered = required_caps & set(node.capabilities)
        components["capability"] = len(covered) / len(required_caps)
        reasons.append(
            f"capabilities {len(covered)}/{len(required_caps)}")
    else:
        components["capability"] = 1.0

    required_tools = set(req.tool_formats)
    if required_tools:
        covered_tools = required_tools & set(node.tool_formats)
        components["tools"] = len(covered_tools) / len(required_tools)
        reasons.append(
            f"tool formats {len(covered_tools)}/{len(required_tools)}")
    else:
        components["tools"] = 1.0

    if req.type is not None:
        components["type"] = 1.0
        reasons.append(f"type {node.type} is required")
    elif req.preferred_types:
        if node.type in req.preferred_types:
            components["type"] = 1.0
            reasons.append(f"type {node.type} is preferred")
        else:
            affinity = max(
                (_type_similarity(node.type, name)
                 for name in req.preferred_types), default=0.0)
            components["type"] = round(max(0.2, affinity), 4)
            reasons.append(f"type {node.type} is not a preferred type")
    else:
        components["type"] = 1.0

    size_value, size_reason = _size_score(node.size_class, req.size)
    components["size"] = size_value
    reasons.append(size_reason)

    if req.min_context is not None and node.context is not None:
        ratio = node.context / req.min_context
        components["context"] = round(min(1.0, 0.5 + min(ratio - 1.0, 3.0)
                                          / 6.0), 4)
        reasons.append(
            f"context {node.context} ({ratio:.1f}x minimum)")
    elif node.context is not None:
        components["context"] = round(min(
            1.0, math.log1p(node.context)
            / math.log1p(2_000_000)), 4)
        reasons.append(f"context {node.context}")
    else:
        components["context"] = 0.2
        reasons.append("context unknown")

    cost = _cost_value(node)
    if cost is None:
        components["cost"] = 0.3
        reasons.append("cost unknown")
    elif req.max_cost is not None:
        if req.max_cost <= 0:
            components["cost"] = 1.0 if cost == 0 else 0.0
        else:
            components["cost"] = round(
                max(0.0, min(1.0, 1.0 - cost / req.max_cost)), 4)
        reasons.append(
            f"cost {_cost_text(cost)} <= {_cost_text(req.max_cost)}")
    else:
        components["cost"] = round(
            max(0.0, min(1.0, 1.0 - cost / 50.0)), 4)
        reasons.append(f"cost {_cost_text(cost)}")

    components["availability"] = 1.0
    reasons.append("available")

    if req.preferred_families:
        if node.family in req.preferred_families:
            components["preference"] = 1.0
            reasons.append(f"preferred family {node.family}")
        else:
            components["preference"] = 0.0
            reasons.append(f"family {node.family} is not preferred")
    else:
        components["preference"] = 1.0

    score = sum(MATCH_WEIGHTS[name] * components[name]
                for name in MATCH_WEIGHTS)
    return round(max(0.0, min(1.0, score)), 4), components, tuple(reasons)


def match(root: str | Path | None = None, requirements: Requirements | None = None,
          *, limit: int = 5, runner: M.Runner | None = None,
          timeout_s: float = M.DISCOVERY_TIMEOUT_S,
          overrides: Mapping[str, Any] | None = None,
          catalog: M.Catalog | None = None,
          external: Mapping[str, Mapping[str, Any]] | None = None,
          max_nodes: int = MAX_NODES) -> MatchResult:
    """Rank available models against advisory requirements.

    Hard filters are locality, minimum context, maximum cost, required
    capabilities, required modalities and an explicit model type. Models with
    ``status = "unknown"`` that pass the filters are reported in
    ``not_assessed`` with their status; they are not silently dropped. This
    function never calls ``models.resolve`` or a model.
    """
    limit = _limit(limit, "limit")
    if requirements is None:
        requirements = Requirements()
    if not isinstance(requirements, Requirements):
        raise ModelGraphError("requirements must be a Requirements instance")
    item = graph(root, runner=runner, timeout_s=timeout_s, overrides=overrides,
                 catalog=catalog, external=external, max_nodes=max_nodes)
    warnings = list(item.warnings)
    warnings.extend(requirements.warnings)
    candidates: list[MatchCandidate] = []
    excluded: list[dict[str, Any]] = []
    not_assessed: list[dict[str, Any]] = []
    for node in item.nodes:
        failures = _filter_reasons(node, requirements)
        if failures:
            excluded.append({
                "id": node.id,
                "status": node.status,
                "family": node.family,
                "type": node.type,
                "size_class": node.size_class,
                "reasons": failures,
            })
            continue
        if node.status != M.STATUS_AVAILABLE:
            reason = ("discovery unavailable; availability not verified"
                      if not item.discovery.available
                      else "not listed by `opencode models`")
            not_assessed.append({
                "id": node.id,
                "status": node.status,
                "family": node.family,
                "type": node.type,
                "size_class": node.size_class,
                "reasons": [reason],
            })
            continue
        score, components, reasons = _match_score(node, requirements)
        candidates.append(MatchCandidate(
            id=node.id,
            score=score,
            family=node.family,
            type=node.type,
            size_class=node.size_class,
            status=node.status,
            local=node.local,
            confidence=node.confidence,
            reasons=reasons,
            components=tuple(components.items()),
            capabilities=node.capabilities,
            tool_formats=node.tool_formats,
        ))
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.id))
    ranked = tuple(candidates[:limit])
    return MatchResult(
        root=item.root,
        requirements=requirements,
        selected=ranked[0] if ranked else None,
        candidates=ranked,
        excluded=tuple(excluded),
        not_assessed=tuple(not_assessed),
        discovery=item.discovery,
        warnings=tuple(warnings),
        unknown_keys=item.config.unknown_keys,
    )


# --------------------------------------------------------------------------- #
# tool fit
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolDescriptor:
    id: str
    format: str | None
    provides: tuple[str, ...]
    preferred_by: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "format": self.format,
            "provides": list(self.provides),
            "preferred_by": list(self.preferred_by),
        }


def _descriptor_list(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ModelGraphError(f"{where}: must be a list of strings")
    if isinstance(value, (set, frozenset)):
        value = sorted(value)
    if not isinstance(value, (list, tuple)):
        raise ModelGraphError(f"{where}: must be a list of strings")
    return _text_list(list(value), where, max_items=64)


def _describe_tool(value: Any, index: int) -> ToolDescriptor:
    where = f"tools[{index}]"

    def field(name: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    tool_id = _text(field("id"), f"{where}.id", max_len=128)
    raw_format = field("format")
    format_name = (None if raw_format is None
                   else _choice(raw_format, TOOL_FORMATS, f"{where}.format"))
    raw_provides = field("provides", ()) or ()
    provides = (_descriptor_list(raw_provides, f"{where}.provides")
                if raw_provides else ())
    raw_preferred = field("preferred_by", ()) or ()
    preferred_by = (_descriptor_list(raw_preferred,
                                     f"{where}.preferred_by")
                    if raw_preferred else ())
    return ToolDescriptor(tool_id, format_name, provides, preferred_by)


_RELATED_TOOL_FORMATS = {
    "apply_patch": ({"replace", "whole_file"}, 0.6),
    "replace": ({"apply_patch", "whole_file"}, 0.6),
    "whole_file": ({"apply_patch", "replace"}, 0.6),
    "library_call": ({"cli", "mcp"}, 0.6),
    "cli": ({"library_call", "mcp"}, 0.6),
    "mcp": ({"library_call", "cli"}, 0.6),
    "native_pdf": ({"library_call", "mcp"}, 0.4),
}


def _tool_fit_for_node(node: ModelNode, tool: ToolDescriptor
                       ) -> dict[str, Any]:
    components: dict[str, float] = {}
    present: dict[str, float] = {}
    reasons: list[str] = []
    if tool.format is None:
        reasons.append("tool format not stated")
    elif tool.format in node.tool_formats:
        present["format"] = 1.0
        components["format"] = 1.0
        reasons.append(f"format {tool.format} is directly supported")
    elif (tool.format in _RELATED_TOOL_FORMATS
          and _RELATED_TOOL_FORMATS[tool.format][0]
          & set(node.tool_formats)):
        value = _RELATED_TOOL_FORMATS[tool.format][1]
        present["format"] = value
        components["format"] = value
        reasons.append(f"format {tool.format} has a supported near equivalent")
    elif tool.format == "native_pdf" and "pdf_native" in node.capabilities:
        present["format"] = 0.7
        components["format"] = 0.7
        reasons.append("native PDF is available via the pdf_native capability")
    else:
        present["format"] = 0.0
        components["format"] = 0.0
        reasons.append(f"format {tool.format} is not supported")

    if tool.provides:
        provided = set(tool.provides) & set(node.capabilities)
        value = len(provided) / len(set(tool.provides))
        present["provides"] = value
        components["provides"] = round(value, 4)
        reasons.append(
            f"provides {len(provided)}/{len(set(tool.provides))} capabilities")
    else:
        reasons.append("tool provides no capability hints")

    if tool.preferred_by:
        names = [node.id, node.provider, node.family, node.variant]
        matched = any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in tool.preferred_by for name in names)
        present["preferred_by"] = 1.0 if matched else 0.0
        components["preferred_by"] = present["preferred_by"]
        reasons.append("preferred by this model" if matched
                       else "not preferred by this model")

    total = sum(present.values())
    fit = round(total / len(present), 4) if present else 0.0
    return {
        "id": tool.id,
        "format": tool.format,
        "provides": list(tool.provides),
        "preferred_by": list(tool.preferred_by),
        "fit": fit,
        "components": components,
        "reasons": reasons,
    }


def tool_fit(root: str | Path | None = None, model_id: str = "",
             tools: Sequence[Any] = (), *, runner: M.Runner | None = None,
             timeout_s: float = M.DISCOVERY_TIMEOUT_S,
             overrides: Mapping[str, Any] | None = None,
             catalog: M.Catalog | None = None,
             external: Mapping[str, Mapping[str, Any]] | None = None,
             graph_item: ModelGraph | None = None) -> dict[str, Any]:
    """Annotate and rank tool descriptors for one model.

    ``tools`` accepts plain mappings or objects with ``id``, ``format``,
    ``provides`` and ``preferred_by`` attributes, so the toolchain module can
    pass its own descriptors later. Nothing is executed. The result is ordered
    by fit and tool id.
    """
    item = graph_item if graph_item is not None else graph(
        root, runner=runner, timeout_s=timeout_s, overrides=overrides,
        catalog=catalog, external=external)
    node = item.get(model_id)
    if node is None:
        raise ModelGraphError(f"model {model_id!r} is not in the model graph")
    if len(tools) > MAX_LIST:
        raise ModelGraphError(
            f"tools: must contain at most {MAX_LIST} entries")
    descriptors = [_describe_tool(tool, index)
                   for index, tool in enumerate(tools)]
    ranked = [_tool_fit_for_node(node, tool) for tool in descriptors]
    ranked.sort(key=lambda entry: (-entry["fit"], entry["id"]))
    return {
        "schema": SCHEMA,
        "source": "advisory",
        "model": node.to_dict(),
        "tools": ranked,
        "best": ranked[0] if ranked else None,
        "warnings": list(item.warnings),
    }


def tool_matrix(root: str | Path | None = None, tools: Sequence[Any] = (), *,
                limit: int = 20, runner: M.Runner | None = None,
                timeout_s: float = M.DISCOVERY_TIMEOUT_S,
                overrides: Mapping[str, Any] | None = None,
                catalog: M.Catalog | None = None,
                external: Mapping[str, Mapping[str, Any]] | None = None,
                graph_item: ModelGraph | None = None) -> dict[str, Any]:
    """Rank model/tool pairs by fit across the graph (advisory, bounded)."""
    limit = _limit(limit, "limit", maximum=200)
    item = graph_item if graph_item is not None else graph(
        root, runner=runner, timeout_s=timeout_s, overrides=overrides,
        catalog=catalog, external=external)
    if len(tools) > MAX_LIST:
        raise ModelGraphError(
            f"tools: must contain at most {MAX_LIST} entries")
    descriptors = [_describe_tool(tool, index)
                   for index, tool in enumerate(tools)]
    pairs: list[dict[str, Any]] = []
    for node in item.nodes:
        for descriptor in descriptors:
            entry = _tool_fit_for_node(node, descriptor)
            pairs.append({
                "model_id": node.id,
                "tool_id": entry["id"],
                "fit": entry["fit"],
                "format": entry["format"],
                "reasons": entry["reasons"],
            })
    pairs.sort(key=lambda entry: (-entry["fit"], entry["model_id"],
                                  entry["tool_id"]))
    return {
        "schema": SCHEMA,
        "source": "advisory",
        "pairs": pairs[:limit],
        "total": len(pairs),
        "warnings": list(item.warnings),
    }


__all__ = [
    "CAPABILITIES",
    "CONFIDENCES",
    "CONFIG_SECTION",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DEFAULT_TOP_K",
    "DEFAULT_WEIGHTS",
    "Edge",
    "FEATURES",
    "GraphConfig",
    "MATCH_WEIGHTS",
    "MAX_EDGES",
    "MAX_NODES",
    "MODALITIES",
    "MatchCandidate",
    "MatchResult",
    "ModelGraph",
    "ModelGraphError",
    "ModelNode",
    "PROFILE_RESOURCE",
    "Profile",
    "Requirements",
    "SCHEMA",
    "SIZE_CLASSES",
    "SIZE_RANK",
    "TASK_PROFILES",
    "TOOL_FORMATS",
    "TYPES",
    "TaskProfile",
    "ToolDescriptor",
    "builtin_profiles",
    "distance",
    "graph",
    "match",
    "neighbours",
    "requirements",
    "similar",
    "tool_fit",
    "tool_matrix",
]
