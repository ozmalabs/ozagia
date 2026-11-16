"""Available-model registry, configuration overlay and deterministic resolution.

This module answers three questions without calling a model:

1. which models does the local OpenCode CLI advertise (``opencode models``);
2. which models does ``ozagia/config.toml`` configure as the default, fallback
   or per-task choice;
3. which model should a task use, and why.

The answer is a registry, not a router. There is no classifier and no
automatic cost/latency optimisation here. Resolution is deterministic: a task
mapping wins over the configured default, an unavailable model is replaced
only by the explicitly configured fallback, and a host default is reported as
such rather than guessed. A later ``model_classify`` bounded task may advise a
choice, but this module does not run it (see ``CLASSIFIER_TASK_CLASS``).

Discovery
---------
``opencode models`` is a read-only listing command. It is invoked through an
injected *runner* so tests and callers never have to run the real CLI::

    def runner(argv, timeout_s):
        return subprocess.CompletedProcess(argv, 0, "local/qwen\\n", "")

    item = catalog(root, runner=runner)

The default runner runs ``["opencode", "models"]`` with a timeout and stdin
closed. It never runs a model. A missing executable, a timeout, a non-zero exit
or unparseable output is reported as ``unavailable`` or ``error``; configured
models then carry ``status = "unknown"``. The registry never invents context
lengths, costs, capabilities or locality that discovery did not provide.

Configuration
-------------
Only the ``[models]`` table of the user config and
``<root>/ozagia/config.toml`` is read. Other sections are left to their own
loaders. The supported shape is::

    [models]
    default = "provider/model"
    fallback = "provider/model"
    local_only = false

    [models.tasks]
    diff_review = "provider/model"
    paper_triage = "provider/model"

    [models.local]
    providers = ["ollama", "lmstudio"]
    models = ["vllm/private-model"]

``by_task_class`` is accepted as an alias for ``tasks`` (not both at once).
Model ids are OpenCode ids: ``provider/model``, bounded and free of whitespace
and control characters. Empty ``default`` and ``fallback`` values have their
usual meanings: the host CLI default and no fallback.

Precedence follows ``ozagia.core.settings``: built-in default < user config <
repository config < environment < explicit override. ``OZAGIA_MODELS_DEFAULT``
and ``OZAGIA_MODELS_LOCAL_ONLY`` are honoured. Task entries from later layers
override the same task class and keep unrelated classes. Local declarations are
unioned across layers.

Locality
--------
A provider or model is local only when the configuration says so. Discovery
does not report locality, so an undeclared model has ``local = None`` and is
filtered when ``local_only`` is true. This is deliberately conservative: a
remote gateway named ``ollama`` must not be assumed local.
"""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.config import repo_root
from ozagia.core import settings as settings_registry

SCHEMA = 1
DISCOVERY_COMMAND = ("opencode", "models")
DISCOVERY_TIMEOUT_S = 60.0
ENV_MODELS_DEFAULT = "OZAGIA_MODELS_DEFAULT"
ENV_MODELS_LOCAL_ONLY = "OZAGIA_MODELS_LOCAL_ONLY"

#: The bounded task class that will later advise model selection. It is not
#: run by this module; ``resolve`` stays deterministic until an offline
#: evaluation shows the classifier beats the explicit default.
CLASSIFIER_TASK_CLASS = "model_classify"

SOURCE_OPENCODE = "opencode"
SOURCE_CONFIG = "config"
SOURCE_SETTINGS = "settings"
SOURCE_OVERRIDE = "override"
RECORD_SOURCES = (SOURCE_OPENCODE, SOURCE_CONFIG, SOURCE_SETTINGS,
                  SOURCE_OVERRIDE)

STATUS_AVAILABLE = "available"
STATUS_UNKNOWN = "unknown"
RECORD_STATUSES = (STATUS_AVAILABLE, STATUS_UNKNOWN)

DISCOVERY_AVAILABLE = "available"
DISCOVERY_UNAVAILABLE = "unavailable"
DISCOVERY_ERROR = "error"
DISCOVERY_STATUSES = (DISCOVERY_AVAILABLE, DISCOVERY_UNAVAILABLE,
                      DISCOVERY_ERROR)

RESOLUTION_RESOLVED = "resolved"
RESOLUTION_UNKNOWN = "unknown"
RESOLUTION_UNRESOLVED = "unresolved"
RESOLUTION_HOST_DEFAULT = "host_default"
RESOLUTION_STATUSES = (RESOLUTION_RESOLVED, RESOLUTION_UNKNOWN,
                       RESOLUTION_UNRESOLVED, RESOLUTION_HOST_DEFAULT)

_MODELS_KEYS = frozenset({
    "default", "fallback", "local_only", "tasks", "by_task_class", "local",
})
_LOCAL_KEYS = frozenset({"providers", "models"})
_MAX_MODEL_ID = 200
_MAX_LIST = 500
_MAX_MESSAGE = 5000
_PROVIDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+~-]{0,63}\Z")
_MODEL_PART_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/@+~#\[\]-]{0,199}\Z")
_TASK_CLASS_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

Runner = Callable[[Sequence[str], float], Any]


class ModelError(ValueError):
    """A model id, configuration or registry operation is invalid."""


class ModelConfigError(ModelError):
    """The model configuration cannot be read or validated."""


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _error(where: str, message: str) -> ModelConfigError:
    return ModelConfigError(f"{where}: {message}")


def validate_model_id(value: Any, *, where: str = "model id",
                      allow_empty: bool = False) -> str:
    """Validate an OpenCode ``provider/model`` id and return it trimmed.

    ``allow_empty`` is for ``models.default`` and ``models.fallback``, whose
    empty value means the host default or no fallback.
    """
    if not isinstance(value, str):
        raise _error(where, "must be a string")
    text = value.strip()
    if not text:
        if allow_empty:
            return ""
        raise _error(where, "must not be empty")
    if len(text) > _MAX_MODEL_ID:
        raise _error(where, f"must be at most {_MAX_MODEL_ID} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise _error(where, "must not contain control characters")
    if any(char.isspace() for char in text):
        raise _error(where, "must not contain whitespace")
    provider, separator, model = text.partition("/")
    if not separator or not provider or not model:
        raise _error(where, "must be provider/model")
    if not _PROVIDER_RE.fullmatch(provider):
        raise _error(where, "provider must be a bounded provider name")
    if not _MODEL_PART_RE.fullmatch(model):
        raise _error(where, "model must be a bounded model name")
    if model.endswith("/") or "//" in model:
        raise _error(where, "model must not contain an empty path segment")
    return text


def _validate_provider(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise _error(where, "must be a string")
    text = value.strip()
    if not text or not _PROVIDER_RE.fullmatch(text):
        raise _error(where, "must be a bounded provider name")
    return text


def _validate_task_class(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise _error(where, "must be a string")
    text = value.strip()
    if not text or not _TASK_CLASS_RE.fullmatch(text):
        raise _error(where, "must be a bounded task class id")
    return text


def _validate_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise _error(where, "must be true or false")
    return value


def _string_list(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, list):
        raise _error(where, "must be a list of strings")
    if len(value) > _MAX_LIST:
        raise _error(where, f"must contain at most {_MAX_LIST} entries")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _error(f"{where}[{index}]", "must be a non-empty string")
        result.append(item.strip())
    return result


def _split_model_id(model_id: str) -> tuple[str, str]:
    provider, _, model = model_id.partition("/")
    return provider, model


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Discovery:
    """The result of one read-only ``opencode models`` invocation."""

    command: tuple[str, ...]
    status: str
    message: str | None = None
    models: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == DISCOVERY_AVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "status": self.status,
            "message": self.message,
            "models": list(self.models),
            "warnings": list(self.warnings),
        }


def default_runner(argv: Sequence[str], timeout_s: float
                   ) -> subprocess.CompletedProcess[str]:
    """Run the discovery command without a shell and with stdin closed."""
    return subprocess.run(
        list(argv), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def parse_models_output(stdout: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Valid model ids and ignored-line warnings from discovery output.

    The real command prints one ``provider/model`` per line. Blank lines and
    ``#`` comments are ignored. A line that is not a valid id is reported, not
    coerced into one.
    """
    ids: set[str] = set()
    warnings: list[str] = []
    for number, raw in enumerate(stdout.splitlines(), 1):
        line = _ANSI_RE.sub("", raw).strip()
        if not line or line.startswith("#"):
            continue
        try:
            model_id = validate_model_id(
                line, where=f"opencode models line {number}")
        except ModelConfigError as exc:
            warnings.append(str(exc))
            continue
        ids.add(model_id)
    return tuple(sorted(ids)), tuple(warnings)


def discover(*, runner: Runner | None = None,
             timeout_s: float = DISCOVERY_TIMEOUT_S) -> Discovery:
    """List models with an injected or default runner; never fatal.

    A missing ``opencode``, a timeout, a non-zero exit or unparseable output
    returns a ``Discovery`` whose status is ``unavailable`` or ``error``. It
    never falls back to a guessed model list.
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ModelConfigError("timeout_s must be a positive number")
    if timeout_s <= 0:
        raise ModelConfigError("timeout_s must be a positive number")
    run = runner if runner is not None else default_runner
    command = DISCOVERY_COMMAND
    try:
        result = run(command, float(timeout_s))
    except FileNotFoundError as exc:
        return Discovery(
            command, DISCOVERY_UNAVAILABLE,
            f"opencode is not available: {exc}", ())
    except subprocess.TimeoutExpired:
        return Discovery(
            command, DISCOVERY_UNAVAILABLE,
            f"`opencode models` timed out after {timeout_s:g} s", ())
    except OSError as exc:
        return Discovery(
            command, DISCOVERY_UNAVAILABLE,
            f"cannot run `opencode models`: {exc}", ())
    except Exception as exc:  # an injected runner is an external boundary
        return Discovery(
            command, DISCOVERY_ERROR,
            f"{type(exc).__name__}: {exc}", ())

    if result is None:
        return Discovery(command, DISCOVERY_ERROR, "runner returned no result", ())
    returncode = getattr(result, "returncode", None)
    stdout = _as_text(getattr(result, "stdout", ""))
    stderr = _as_text(getattr(result, "stderr", ""))
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return Discovery(command, DISCOVERY_ERROR,
                         "runner returned no exit status", ())
    if returncode != 0:
        detail = (stderr.strip() or stdout.strip()
                  or f"exit status {returncode}")
        return Discovery(command, DISCOVERY_ERROR, detail[:_MAX_MESSAGE], ())
    models, warnings = parse_models_output(stdout)
    if not models:
        return Discovery(
            command, DISCOVERY_ERROR,
            "`opencode models` listed no recognisable provider/model ids",
            warnings)
    return Discovery(command, DISCOVERY_AVAILABLE, None, models, warnings)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelSettings:
    """The effective ``[models]`` configuration after all layers."""

    default: str = ""
    fallback: str = ""
    local_only: bool = False
    tasks: tuple[tuple[str, str], ...] = ()
    task_sources: tuple[tuple[str, str], ...] = ()
    local_providers: tuple[str, ...] = ()
    local_models: tuple[str, ...] = ()
    local_model_sources: tuple[tuple[str, str], ...] = ()
    local_source: str = "default"
    sources: tuple[tuple[str, str], ...] = ()
    config_files: tuple[str, ...] = ()

    def task_model(self, task_class: str) -> str | None:
        return dict(self.tasks).get(task_class)

    def task_source(self, task_class: str) -> str | None:
        return dict(self.task_sources).get(task_class)

    def source(self, key: str) -> str:
        return dict(self.sources).get(key, "default")

    def local_model_source(self, model_id: str) -> str | None:
        return dict(self.local_model_sources).get(model_id)

    def is_local(self, model_id: str, provider: str) -> bool | None:
        """True when explicitly local, else None (remote is never assumed)."""
        if model_id in self.local_models or provider in self.local_providers:
            return True
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default,
            "fallback": self.fallback,
            "local_only": self.local_only,
            "tasks": dict(self.tasks),
            "task_sources": dict(self.task_sources),
            "local": {
                "providers": list(self.local_providers),
                "models": list(self.local_models),
                "model_sources": dict(self.local_model_sources),
                "source": self.local_source,
            },
            "sources": dict(self.sources),
            "config_files": list(self.config_files),
        }


@dataclass
class _SettingsState:
    default: str = ""
    fallback: str = ""
    local_only: bool = False
    sources: dict[str, str] = field(default_factory=lambda: {
        "default": "default", "fallback": "default",
        "local_only": "default",
    })
    tasks: dict[str, tuple[str, str]] = field(default_factory=dict)
    local_providers: set[str] = field(default_factory=set)
    local_models: set[str] = field(default_factory=set)
    local_model_layers: dict[str, str] = field(default_factory=dict)
    local_source: str = "default"
    files: list[str] = field(default_factory=list)


def _unknown_key(where: str, key: str, allowed: Iterable[str]) -> str:
    message = f"{where}: unknown key {key!r} in [models]"
    matches = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.55)
    if matches:
        message += f"; did you mean {matches[0]!r}?"
    return message


def _read_models_table(path: Path) -> dict[str, Any] | None:
    """The ``[models]`` table of one TOML file, or None when it is absent."""
    if path.is_dir():
        raise ModelConfigError(f"{path}: is a directory, not a TOML file")
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ModelConfigError(f"{path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelConfigError(f"{path}: not UTF-8: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ModelConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelConfigError(f"{path}: expected a TOML table")
    models = data.get("models")
    if models is None:
        return {}
    if not isinstance(models, dict):
        raise ModelConfigError(f"{path}: [models] must be a table")
    return models


def _validate_tasks(value: Any, *, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _error(where, "must be a table of task class to model id")
    if len(value) > _MAX_LIST:
        raise _error(where, f"must contain at most {_MAX_LIST} entries")
    result: dict[str, str] = {}
    for raw_class, raw_model in value.items():
        task_class = _validate_task_class(
            raw_class, where=f"{where} task class")
        model_id = validate_model_id(
            raw_model, where=f"{where}.{task_class}")
        result[task_class] = model_id
    return result


def _validate_local(value: Any, *, where: str
                    ) -> tuple[list[str], list[str]]:
    if not isinstance(value, Mapping):
        raise _error(where, "must be a table")
    unknown = [key for key in value
               if not isinstance(key, str) or key not in _LOCAL_KEYS]
    if unknown:
        key = unknown[0]
        if not isinstance(key, str):
            raise ModelConfigError(f"{where}: keys must be strings")
        message = f"{where}: unknown key {key!r}"
        matches = difflib.get_close_matches(
            key, sorted(_LOCAL_KEYS), n=1, cutoff=0.55)
        if matches:
            message += f"; did you mean {matches[0]!r}?"
        raise ModelConfigError(message)
    providers = [
        _validate_provider(item, where=f"{where}.providers")
        for item in _string_list(value.get("providers", []),
                                 where=f"{where}.providers")
    ]
    models = [
        validate_model_id(item, where=f"{where}.models")
        for item in _string_list(value.get("models", []),
                                 where=f"{where}.models")
    ]
    return providers, models


def _apply_table(state: _SettingsState, table: Mapping[str, Any], *,
                 layer: str, where: str) -> None:
    if not isinstance(table, Mapping):
        raise _error(where, "[models] must be a table")
    unknown = sorted(set(table) - _MODELS_KEYS)
    if unknown:
        raise ModelConfigError(_unknown_key(where, unknown[0], _MODELS_KEYS))

    if "default" in table:
        state.default = validate_model_id(
            table["default"], where=f"{where}: [models] default",
            allow_empty=True)
        state.sources["default"] = layer
    if "fallback" in table:
        state.fallback = validate_model_id(
            table["fallback"], where=f"{where}: [models] fallback",
            allow_empty=True)
        state.sources["fallback"] = layer
    if "local_only" in table:
        state.local_only = _validate_bool(
            table["local_only"], where=f"{where}: [models] local_only")
        state.sources["local_only"] = layer

    if "tasks" in table and "by_task_class" in table:
        raise _error(where, "define [models.tasks] or [models.by_task_class], "
                            "not both")
    for key in ("tasks", "by_task_class"):
        if key not in table:
            continue
        tasks = _validate_tasks(
            table[key], where=f"{where}: [models.{key}]")
        for task_class, model_id in sorted(tasks.items()):
            state.tasks[task_class] = (model_id, layer)

    if "local" in table:
        providers, models = _validate_local(
            table["local"], where=f"{where}: [models.local]")
        state.local_providers.update(providers)
        state.local_models.update(models)
        for model_id in models:
            state.local_model_layers[model_id] = layer
        state.local_source = layer


def _normalise_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(overrides, Mapping):
        raise ModelConfigError(
            "overrides must be a mapping of model setting to value")
    table: dict[str, Any] = {}
    for raw_key, value in overrides.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ModelConfigError("override keys must be non-empty strings")
        key = raw_key.strip()
        if key.startswith("models."):
            key = key[len("models."):]
        if key not in _MODELS_KEYS:
            raise ModelConfigError(_unknown_key("override", key, _MODELS_KEYS))
        if key in table:
            raise ModelConfigError(f"override: {key!r} is defined twice")
        table[key] = value
    return table


def _parse_env_bool(name: str, text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    raise ModelConfigError(f"${name}: must be true or false, got {text!r}")


def load_settings(root: str | Path | None = None, *,
                  overrides: Mapping[str, Any] | None = None
                  ) -> ModelSettings:
    """Read and validate the effective ``[models]`` settings.

    Reads only ``[models]`` from the user config and
    ``<root>/ozagia/config.toml``; unrelated sections are ignored. Environment
    values and explicit overrides are applied last.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    try:
        layers = (
            ("user", settings_registry.user_config_path()),
            ("repo", settings_registry.repo_config_path(root_p)),
        )
    except settings_registry.SettingsError as exc:
        raise ModelConfigError(str(exc)) from exc

    state = _SettingsState()
    for layer, path in layers:
        table = _read_models_table(path)
        if table is None:
            continue
        state.files.append(str(path))
        _apply_table(state, table, layer=layer, where=str(path))

    if ENV_MODELS_DEFAULT in os.environ:
        state.default = validate_model_id(
            os.environ[ENV_MODELS_DEFAULT],
            where=f"${ENV_MODELS_DEFAULT}", allow_empty=True)
        state.sources["default"] = "env"
    if ENV_MODELS_LOCAL_ONLY in os.environ:
        state.local_only = _parse_env_bool(
            ENV_MODELS_LOCAL_ONLY, os.environ[ENV_MODELS_LOCAL_ONLY])
        state.sources["local_only"] = "env"

    if overrides is not None:
        _apply_table(state, _normalise_overrides(overrides),
                     layer="override", where="override")

    ordered_tasks = tuple(
        (task_class, model_id)
        for task_class, (model_id, _layer) in sorted(state.tasks.items()))
    task_sources = tuple(
        (task_class, layer)
        for task_class, (_model_id, layer) in sorted(state.tasks.items()))
    return ModelSettings(
        default=state.default,
        fallback=state.fallback,
        local_only=state.local_only,
        tasks=ordered_tasks,
        task_sources=task_sources,
        local_providers=tuple(sorted(state.local_providers)),
        local_models=tuple(sorted(state.local_models)),
        local_model_sources=tuple(sorted(state.local_model_layers.items())),
        local_source=state.local_source,
        sources=tuple(sorted(state.sources.items())),
        config_files=tuple(state.files),
    )


#: Alias used by callers that think of this as model configuration.
load_config = load_settings


# --------------------------------------------------------------------------- #
# records and catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelRecord:
    """One model in the catalog.

    ``context`` is in tokens; ``input_cost`` and ``output_cost`` are USD per
    million tokens. All three are ``None`` when unknown. ``local`` is ``None``
    when the configuration does not declare the model local. ``references``
    names the configuration entries that mention the model (``default``,
    ``fallback``, ``task:<class>``, ``local``).
    """

    id: str
    provider: str
    model: str
    context: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    capabilities: tuple[str, ...] = ()
    local: bool | None = None
    source: str = SOURCE_OPENCODE
    status: str = STATUS_AVAILABLE
    references: tuple[str, ...] = ()

    @property
    def remote(self) -> bool | None:
        """The complement of ``local`` when known, else None."""
        return None if self.local is None else not self.local

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "context": self.context,
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "capabilities": list(self.capabilities),
            "local": self.local,
            "remote": self.remote,
            "source": self.source,
            "status": self.status,
            "references": list(self.references),
        }


@dataclass(frozen=True)
class Candidate:
    """One considered model in a resolution."""

    id: str
    role: str
    source: str
    layer: str
    status: str
    selected: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "source": self.source,
            "layer": self.layer,
            "status": self.status,
            "selected": self.selected,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Resolution:
    """The deterministic choice for a task class or the configured default."""

    root: Path
    task_class: str | None
    requested: str | None
    requested_role: str | None
    requested_layer: str | None
    selected: ModelRecord | None
    source: str
    status: str
    reason: str
    fallback_used: bool
    candidates: tuple[Candidate, ...]
    discovery: Discovery
    settings: ModelSettings
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        requested = None
        if self.requested is not None:
            requested = {
                "id": self.requested,
                "role": self.requested_role,
                "config_source": self.requested_layer,
            }
        return {
            "schema": SCHEMA,
            "root": str(self.root),
            "task_class": self.task_class,
            "requested": requested,
            "selected": (self.selected.to_dict()
                         if self.selected is not None else None),
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "fallback_used": self.fallback_used,
            "candidates": [candidate.to_dict()
                           for candidate in self.candidates],
            "discovery": self.discovery.to_dict(),
            "settings": self.settings.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class Catalog:
    """Models known from discovery and configuration."""

    root: Path
    records: tuple[ModelRecord, ...]
    discovery: Discovery
    settings: ModelSettings
    warnings: tuple[str, ...] = ()

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]

    def get(self, model_id: str) -> ModelRecord | None:
        text = validate_model_id(model_id)
        for record in self.records:
            if record.id == text:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "root": str(self.root),
            "discovery": self.discovery.to_dict(),
            "settings": self.settings.to_dict(),
            "models": [record.to_dict() for record in self.records],
            "warnings": list(self.warnings),
        }


def _configured_source(sources: Iterable[str]) -> str:
    values = set(sources)
    if SOURCE_OVERRIDE in values:
        return SOURCE_OVERRIDE
    if SOURCE_CONFIG in values:
        return SOURCE_CONFIG
    return SOURCE_SETTINGS


def _build_catalog(root: Path, model_settings: ModelSettings,
                   discovery: Discovery) -> Catalog:
    references: dict[str, set[str]] = {}
    ref_sources: dict[str, set[str]] = {}

    def note(model_id: str, label: str, source: str) -> None:
        if not model_id:
            return
        references.setdefault(model_id, set()).add(label)
        ref_sources.setdefault(model_id, set()).add(source)

    default_source = (SOURCE_OVERRIDE
                      if model_settings.source("default") == "override"
                      else SOURCE_SETTINGS)
    fallback_source = (SOURCE_OVERRIDE
                       if model_settings.source("fallback") == "override"
                       else SOURCE_SETTINGS)
    note(model_settings.default, "default", default_source)
    note(model_settings.fallback, "fallback", fallback_source)
    for task_class, model_id in model_settings.tasks:
        source = (SOURCE_OVERRIDE
                  if model_settings.task_source(task_class) == "override"
                  else SOURCE_CONFIG)
        note(model_id, f"task:{task_class}", source)
    for model_id in model_settings.local_models:
        source = (SOURCE_OVERRIDE
                  if model_settings.local_model_source(model_id) == "override"
                  else SOURCE_CONFIG)
        note(model_id, "local", source)

    records: dict[str, ModelRecord] = {}
    for model_id in discovery.models:
        provider, model = _split_model_id(model_id)
        records[model_id] = ModelRecord(
            id=model_id,
            provider=provider,
            model=model,
            local=model_settings.is_local(model_id, provider),
            source=SOURCE_OPENCODE,
            status=STATUS_AVAILABLE,
            references=tuple(sorted(references.get(model_id, ()))),
        )

    warnings = list(discovery.warnings)
    configured_missing: list[str] = []
    for model_id in sorted(references):
        if model_id in records:
            continue
        provider, model = _split_model_id(model_id)
        source = _configured_source(ref_sources.get(model_id, ()))
        records[model_id] = ModelRecord(
            id=model_id,
            provider=provider,
            model=model,
            local=model_settings.is_local(model_id, provider),
            source=source,
            status=STATUS_UNKNOWN,
            references=tuple(sorted(references[model_id])),
        )
        if discovery.available:
            configured_missing.append(model_id)
    if not discovery.available and discovery.message:
        warnings.append(discovery.message)
    for model_id in configured_missing:
        labels = ", ".join(sorted(references[model_id]))
        warnings.append(
            f"configured model {model_id!r} ({labels}) is not listed by "
            "`opencode models`")

    return Catalog(
        root=root,
        records=tuple(records[model_id] for model_id in sorted(records)),
        discovery=discovery,
        settings=model_settings,
        warnings=tuple(warnings),
    )


def catalog(root: str | Path | None = None, *, runner: Runner | None = None,
            timeout_s: float = DISCOVERY_TIMEOUT_S,
            overrides: Mapping[str, Any] | None = None) -> Catalog:
    """Discover models and overlay the effective ``[models]`` settings.

    Reads only; never creates ``ozagia/`` or ``.ozagia/`` and never calls a
    model. Discovery failure is reported in ``Catalog.discovery`` and
    ``Catalog.warnings``, not raised.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    model_settings = load_settings(root_p, overrides=overrides)
    discovery = discover(runner=runner, timeout_s=timeout_s)
    return _build_catalog(root_p, model_settings, discovery)


def get(root: str | Path | None, model_id: str, *, runner: Runner | None = None,
        timeout_s: float = DISCOVERY_TIMEOUT_S,
        overrides: Mapping[str, Any] | None = None) -> ModelRecord | None:
    """One model by id, or None when it is neither listed nor configured."""
    return catalog(root, runner=runner, timeout_s=timeout_s,
                   overrides=overrides).get(model_id)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _assess(item: Catalog, model_id: str, *, role: str, layer: str,
            local_only: bool) -> Candidate:
    record = item.get(model_id)
    if record is None:  # configured ids are always merged into the catalog
        return Candidate(model_id, role, SOURCE_SETTINGS, layer, STATUS_UNKNOWN,
                         False, "model is not in the catalog")
    if local_only and record.local is not True:
        detail = ("declared remote" if record.local is False
                  else "not declared local")
        return Candidate(model_id, role, record.source, layer, "filtered", False,
                         f"local_only: {detail}")
    if record.status == STATUS_AVAILABLE:
        return Candidate(model_id, role, record.source, layer, STATUS_AVAILABLE,
                         False, "listed by `opencode models`")
    if item.discovery.available:
        return Candidate(model_id, role, record.source, layer, STATUS_UNKNOWN,
                         False, "not listed by `opencode models`")
    return Candidate(
        model_id, role, record.source, layer, STATUS_UNKNOWN, False,
        f"discovery {item.discovery.status}; availability not verified")


def _selected(candidate: Candidate) -> Candidate:
    return Candidate(candidate.id, candidate.role, candidate.source,
                     candidate.layer, candidate.status, True, candidate.reason)


def resolve(root: str | Path | None = None, task_class: str | None = None, *,
            runner: Runner | None = None,
            timeout_s: float = DISCOVERY_TIMEOUT_S,
            overrides: Mapping[str, Any] | None = None) -> Resolution:
    """Choose a model deterministically and explain the choice.

    Order: the task mapping (when one exists) or the configured default; then
    the explicitly configured fallback if the first choice is unavailable or
    filtered by ``local_only``. With no configured default the result is
    ``host_default``: the host CLI chooses, and this module does not invent a
    model. Discovery failure leaves a configured choice selected with
    ``status = "unknown"`` unless locality cannot be verified under
    ``local_only``.
    """
    root_p = Path(root).resolve() if root is not None else repo_root()
    if task_class is not None:
        task_class = _validate_task_class(task_class, where="task class")
    item = catalog(root_p, runner=runner, timeout_s=timeout_s,
                   overrides=overrides)
    model_settings = item.settings
    local_only = model_settings.local_only

    requested: str | None = None
    requested_role = "default"
    requested_layer = model_settings.source("default")
    if task_class is not None:
        mapped = model_settings.task_model(task_class)
        if mapped:
            requested = mapped
            requested_role = "task"
            requested_layer = (model_settings.task_source(task_class)
                               or requested_layer)
    if requested is None:
        requested = model_settings.default

    if not requested:
        return Resolution(
            root=root_p, task_class=task_class, requested=None,
            requested_role=None, requested_layer=None, selected=None,
            source="host", status=RESOLUTION_HOST_DEFAULT,
            reason=("no default model is configured; the host CLI chooses "
                    "its default"),
            fallback_used=False, candidates=(), discovery=item.discovery,
            settings=model_settings, warnings=item.warnings)

    primary = _assess(item, requested, role=requested_role,
                       layer=requested_layer, local_only=local_only)
    candidates = [primary]

    if primary.status == STATUS_AVAILABLE:
        return Resolution(
            root=root_p, task_class=task_class, requested=requested,
            requested_role=requested_role, requested_layer=requested_layer,
            selected=item.get(requested), source=requested_role,
            status=RESOLUTION_RESOLVED,
            reason=f"selected the {requested_role} model {requested!r}; it is "
                   "listed by `opencode models`",
            fallback_used=False, candidates=(_selected(primary),),
            discovery=item.discovery, settings=model_settings,
            warnings=item.warnings)

    if primary.status == STATUS_UNKNOWN and not item.discovery.available:
        return Resolution(
            root=root_p, task_class=task_class, requested=requested,
            requested_role=requested_role, requested_layer=requested_layer,
            selected=item.get(requested), source=requested_role,
            status=RESOLUTION_UNKNOWN,
            reason=f"selected the configured {requested_role} model "
                   f"{requested!r}; discovery is {item.discovery.status}, so "
                   "availability is not verified",
            fallback_used=False, candidates=(_selected(primary),),
            discovery=item.discovery, settings=model_settings,
            warnings=item.warnings)

    fallback = model_settings.fallback
    fallback_assessed: Candidate | None = None
    if fallback and fallback != requested:
        fallback_assessed = _assess(
            item, fallback, role="fallback",
            layer=model_settings.source("fallback"), local_only=local_only)
        candidates.append(fallback_assessed)
        if fallback_assessed.status == STATUS_AVAILABLE:
            return Resolution(
                root=root_p, task_class=task_class, requested=requested,
                requested_role=requested_role,
                requested_layer=requested_layer,
                selected=item.get(fallback), source="fallback",
                status=RESOLUTION_RESOLVED,
                reason=f"{requested_role} model {requested!r} is unavailable "
                       f"({primary.reason}); selected the explicit fallback "
                       f"{fallback!r}",
                fallback_used=True, candidates=(primary, _selected(
                    fallback_assessed)),
                discovery=item.discovery, settings=model_settings,
                warnings=item.warnings)
        if (fallback_assessed.status == STATUS_UNKNOWN
                and not item.discovery.available):
            return Resolution(
                root=root_p, task_class=task_class, requested=requested,
                requested_role=requested_role,
                requested_layer=requested_layer,
                selected=item.get(fallback), source="fallback",
                status=RESOLUTION_UNKNOWN,
                reason=f"{requested_role} model {requested!r} is not usable "
                       f"({primary.reason}); selected the explicit fallback "
                       f"{fallback!r}, whose availability is not verified",
                fallback_used=True,
                candidates=(primary, _selected(fallback_assessed)),
                discovery=item.discovery, settings=model_settings,
                warnings=item.warnings)

    detail = primary.reason
    if fallback_assessed is not None:
        detail += f"; fallback {fallback!r} is not usable " \
                  f"({fallback_assessed.reason})"
    elif not fallback:
        detail += "; no fallback is configured"
    else:
        detail += "; the fallback is the same model"
    return Resolution(
        root=root_p, task_class=task_class, requested=requested,
        requested_role=requested_role, requested_layer=requested_layer,
        selected=None, source=requested_role,
        status=RESOLUTION_UNRESOLVED,
        reason=f"no usable model for this choice: {detail}",
        fallback_used=False, candidates=tuple(candidates),
        discovery=item.discovery, settings=model_settings,
        warnings=item.warnings)


__all__ = [
    "CLASSIFIER_TASK_CLASS",
    "Candidate",
    "Catalog",
    "DISCOVERY_AVAILABLE",
    "DISCOVERY_COMMAND",
    "DISCOVERY_ERROR",
    "DISCOVERY_STATUSES",
    "DISCOVERY_TIMEOUT_S",
    "DISCOVERY_UNAVAILABLE",
    "Discovery",
    "ENV_MODELS_DEFAULT",
    "ENV_MODELS_LOCAL_ONLY",
    "ModelConfigError",
    "ModelError",
    "ModelRecord",
    "ModelSettings",
    "RECORD_SOURCES",
    "RECORD_STATUSES",
    "RESOLUTION_HOST_DEFAULT",
    "RESOLUTION_RESOLVED",
    "RESOLUTION_STATUSES",
    "RESOLUTION_UNKNOWN",
    "RESOLUTION_UNRESOLVED",
    "Resolution",
    "SCHEMA",
    "SOURCE_CONFIG",
    "SOURCE_OPENCODE",
    "SOURCE_OVERRIDE",
    "SOURCE_SETTINGS",
    "STATUS_AVAILABLE",
    "STATUS_UNKNOWN",
    "catalog",
    "default_runner",
    "discover",
    "get",
    "load_config",
    "load_settings",
    "parse_models_output",
    "resolve",
    "validate_model_id",
]
