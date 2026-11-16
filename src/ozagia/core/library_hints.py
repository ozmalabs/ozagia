"""Library-first hints: notice a hand-written capability and name a library.

This is a hint layer, not a refactor. It never rewrites code, installs a
package, calls a model or uses the network. It reads new or changed symbols,
matches a small versioned registry of common "reinvented wheel" patterns and
returns one short line naming a standard-library or well-supported package.

Inputs
------
Detection works on an explicit set of files: a diff, a changed list, or the
before and after text of one handled edit. It never scans a repository on its
own. The graph is not needed; the layer also works from the CLI.

Evidence
--------
Every rule combines a body pattern with optional name evidence. A name alone
never produces a hint. Examples: a retry loop with a bounded attempt count and
a sleep/backoff, manual ``sys.argv`` flag matching, line-by-line YAML parsing,
a date regex with ``int(...group(...))``, a manual cache dictionary, a
hand-written glob translation, a DP-matrix diff, a slug function that lowercases
and replaces non-alphanumerics, a failure counter with a threshold and an open
state.

Availability
------------
The strongest level is ``stdlib`` (a standard-library primitive) or
``declared`` (named in pyproject.toml, package.json, Cargo.toml or
requirements.txt). Next is ``installed`` (Python distributions found through
``importlib.metadata``), then ``known``. A package already imported and used by
the file suppresses the hint (and is recorded as suppressed when recording is
on). A thin wrapper around an imported package is suppressed too.

Modes and output
----------------
``off`` returns nothing and writes no events. ``gentle`` (the default) looks
only at new or changed symbols; ``strict`` also looks at unchanged symbols in
the explicitly named files. ``max_per_edit`` caps hints per file. Ordering is
deterministic.

Events go to ``<state>/library_hints/events.jsonl`` with the timestamp,
session, file, symbol, capability, package, availability, confidence,
shown/suppressed status, reason, rule version and hint id. They never contain
code, prompts or file contents. Feedback (``accepted``/``ignored``) is stored
locally in ``feedback.jsonl`` and is never sent anywhere.

Settings (``ozagia.core.settings``)
-----------------------------------
``hints.library`` (off|gentle|strict), ``hints.max_per_edit``,
``hints.record`` and ``hints.ecosystems``. ``$OZAGIA_LIBRARY_HINTS=0`` is a
hard local opt-out, and ``off``/``gentle``/``strict`` in that variable
overrides the mode for one process.

The module is standard-library only. It reads manifests and project files; it
never imports a target project.
"""
from __future__ import annotations

import ast
import copy
import difflib
import functools
import hashlib
import importlib.metadata
import json
import os
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import state_path

SCHEMA = 1
RULE_VERSION = "2026.11.1"
EVENTS_DIR = "library_hints"
EVENTS_FILE = "events.jsonl"
FEEDBACK_FILE = "feedback.jsonl"
MODES = ("off", "gentle", "strict")
ECOSYSTEMS = ("python", "javascript", "rust")
ENV_HINTS = "OZAGIA_LIBRARY_HINTS"
# A file larger than this is not inspected. The intent layer also bounds reads.
MAX_FILE_BYTES = 256 * 1024
MAX_SYMBOLS = 400
MAX_HINT_FILES = 100
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_SESSION_CHARS = 120
MAX_SYMBOL_CHARS = 160
MAX_FILE_CHARS = 240
MAX_MESSAGE_CHARS = 320
MAX_EVIDENCE_SIGNALS = 6

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
AVAILABILITY_RANK = {"stdlib": 4, "declared": 4, "installed": 2, "known": 1}
AVAILABILITY_TEXT = {
    "stdlib": "standard library",
    "declared": "already a dependency",
    "installed": "installed but not declared",
    "known": "known package",
}
LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".mts": "javascript",
    ".cts": "javascript",
    ".rs": "rust",
}


class LibraryHintsError(RuntimeError):
    """A hint run, manifest or event log could not be read or written."""


# --------------------------------------------------------------------------- #
# capability registry (data, not inference)
# --------------------------------------------------------------------------- #
_CAPABILITIES: dict[str, dict[str, Any]] = {
    "retry": {
        "title": "retry/backoff",
        "stdlib_note": "the standard library has no retry primitive",
        "languages": {
            "python": [
                {"package": "tenacity", "note": "declarative retries with backoff"},
                {"package": "backoff", "note": "retry decorators with backoff"},
            ],
            "javascript": [
                {"package": "p-retry", "note": "promise retries with backoff"},
            ],
            "rust": [
                {"package": "backoff", "note": "retry with exponential backoff"},
                {"package": "tokio-retry", "modules": ["tokio_retry"]},
            ],
        },
    },
    "http": {
        "title": "HTTP client",
        "languages": {
            "python": [
                {"package": "urllib.request", "stdlib": True,
                 "modules": ["urllib.request", "urllib"],
                 "note": "urllib.request is the standard-library client"},
                {"package": "httpx"},
                {"package": "requests"},
            ],
            "javascript": [
                {"package": "fetch", "stdlib": True, "modules": ["fetch"],
                 "usage": r"\bfetch\s*\(", "note": "the built-in fetch API"},
                {"package": "axios"},
                {"package": "undici"},
            ],
            "rust": [
                {"package": "reqwest", "note": "async and blocking HTTP clients"},
            ],
        },
    },
    "validation": {
        "title": "validation/schema",
        "languages": {
            "python": [
                {"package": "pydantic", "note": "typed models with validation"},
                {"package": "jsonschema"},
            ],
            "javascript": [
                {"package": "zod", "note": "TypeScript-first schemas"},
                {"package": "ajv"},
            ],
            "rust": [
                {"package": "serde", "note": "derive-based deserialisation"},
                {"package": "validator"},
            ],
        },
    },
    "cli": {
        "title": "CLI parsing",
        "languages": {
            "python": [
                {"package": "argparse", "stdlib": True, "modules": ["argparse"]},
                {"package": "click"},
                {"package": "typer"},
            ],
            "javascript": [
                {"package": "commander"},
                {"package": "yargs"},
            ],
            "rust": [
                {"package": "clap", "note": "derive or builder CLIs"},
            ],
        },
    },
    "config": {
        "title": "YAML/TOML",
        "languages": {
            "python": [
                {"package": "tomllib", "stdlib": True, "modules": ["tomllib"]},
                {"package": "PyYAML", "dist": "PyYAML", "modules": ["yaml"]},
                {"package": "json", "stdlib": True, "modules": ["json"],
                 "usage": r"\bjson\.(?:loads|load|dumps|dump)\b"},
            ],
            "javascript": [
                {"package": "yaml"},
                {"package": "JSON", "stdlib": True, "modules": ["JSON"],
                 "usage": r"\bJSON\.(?:parse|stringify)\b"},
            ],
            "rust": [
                {"package": "toml"},
                {"package": "serde_yaml"},
            ],
        },
    },
    "datetime": {
        "title": "date/time parsing",
        "languages": {
            "python": [
                {"package": "datetime", "stdlib": True, "modules": ["datetime"],
                 "note": "datetime.fromisoformat and zoneinfo"},
                {"package": "dateutil", "modules": ["dateutil"]},
            ],
            "javascript": [
                {"package": "date-fns"},
                {"package": "luxon"},
            ],
            "rust": [
                {"package": "chrono"},
                {"package": "time"},
            ],
        },
    },
    "cache": {
        "title": "caching",
        "languages": {
            "python": [
                {"package": "functools.lru_cache", "stdlib": True,
                 "modules": ["functools"], "usage": r"\blru_cache\b"},
                {"package": "cachetools"},
            ],
            "javascript": [
                {"package": "lru-cache"},
            ],
            "rust": [
                {"package": "moka"},
                {"package": "cached"},
            ],
        },
    },
    "glob_diff": {
        "title": "glob/diff/patch",
        "languages": {
            "python": [
                {"package": "fnmatch", "stdlib": True, "modules": ["fnmatch"]},
                {"package": "pathlib", "stdlib": True, "modules": ["pathlib"],
                 "usage": r"\bPath\("},
                {"package": "difflib", "stdlib": True, "modules": ["difflib"]},
            ],
            "javascript": [
                {"package": "picomatch"},
                {"package": "diff"},
            ],
            "rust": [
                {"package": "globset"},
                {"package": "similar"},
            ],
        },
    },
    "ids": {
        "title": "IDs/slugs",
        "languages": {
            "python": [
                {"package": "uuid", "stdlib": True, "modules": ["uuid"],
                 "usage": r"\buuid\."},
                {"package": "secrets", "stdlib": True, "modules": ["secrets"],
                 "usage": r"\bsecrets\."},
                {"package": "python-slugify", "dist": "python-slugify",
                 "modules": ["slugify"]},
            ],
            "javascript": [
                {"package": "nanoid"},
                {"package": "slugify"},
            ],
            "rust": [
                {"package": "uuid"},
                {"package": "slug"},
            ],
        },
    },
    "rate_limit": {
        "title": "rate limiting",
        "languages": {
            "python": [
                {"package": "limits"},
                {"package": "slowapi"},
            ],
            "javascript": [
                {"package": "rate-limiter-flexible"},
            ],
            "rust": [
                {"package": "governor"},
            ],
        },
    },
    "semver": {
        "title": "semver",
        "languages": {
            "python": [
                {"package": "packaging", "modules": ["packaging"],
                 "note": "packaging.version"},
                {"package": "semver"},
            ],
            "javascript": [
                {"package": "semver"},
            ],
            "rust": [
                {"package": "semver"},
            ],
        },
    },
    "circuit": {
        "title": "circuit breaker",
        "languages": {
            "python": [
                {"package": "pybreaker"},
            ],
            "javascript": [
                {"package": "opossum"},
            ],
            "rust": [
                {"package": "recloser"},
            ],
        },
    },
}
_CAPABILITY_ORDER = {name: index for index, name in enumerate(_CAPABILITIES)}


@dataclass(frozen=True)
class Candidate:
    package: str
    dist: str | None = None
    stdlib: bool = False
    modules: tuple[str, ...] = ()
    usage: str | None = None
    note: str = ""


def _candidate(entry: Mapping[str, Any]) -> Candidate:
    package = str(entry["package"])
    return Candidate(
        package=package,
        dist=str(entry.get("dist") or package),
        stdlib=bool(entry.get("stdlib")),
        modules=tuple(str(m) for m in entry.get("modules") or (package,)),
        usage=entry.get("usage"),
        note=str(entry.get("note") or ""),
    )


@functools.lru_cache(maxsize=None)
def _candidates(capability: str, language: str) -> tuple[Candidate, ...]:
    entries = _CAPABILITIES[capability]["languages"].get(language, ())
    return tuple(_candidate(entry) for entry in entries)


def registry() -> dict[str, Any]:
    """The versioned capability registry as plain data (a copy)."""
    return {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "capabilities": copy.deepcopy(_CAPABILITIES),
    }


def capability_titles() -> dict[str, str]:
    return {name: str(data["title"]) for name, data in _CAPABILITIES.items()}


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Signal:
    pattern: re.Pattern[str]
    label: str
    minimum: int = 1


def _sig(pattern: str, label: str, minimum: int = 1) -> Signal:
    return Signal(re.compile(pattern, re.MULTILINE), label, minimum)


@dataclass(frozen=True)
class Rule:
    id: str
    capability: str
    language: str
    signals: tuple[Signal, ...]
    name: re.Pattern[str] | None = None
    min_signals: int = 2
    min_signals_named: int = 1
    min_lines: int = 0
    confidence: str = "medium"
    packages: tuple[str, ...] = ()


def _rule(rule_id: str, capability: str, language: str, *,
          name: str | None, signals: Sequence[Signal],
          min_signals: int = 2, min_signals_named: int = 1,
          min_lines: int = 0, confidence: str = "medium",
          packages: Sequence[str] = ()) -> Rule:
    return Rule(
        id=rule_id, capability=capability, language=language,
        name=re.compile(name, re.MULTILINE) if name else None,
        signals=tuple(signals), min_signals=min_signals,
        min_signals_named=min_signals_named, min_lines=min_lines,
        confidence=confidence, packages=tuple(packages),
    )


def _rules(language: str) -> tuple[Rule, ...]:
    if language == "python":
        return _PYTHON_RULES
    if language == "javascript":
        return _JAVASCRIPT_RULES
    if language == "rust":
        return _RUST_RULES
    return ()


_PYTHON_RULES: tuple[Rule, ...] = (
    _rule(
        "python-retry", "retry", "python",
        name=r"(?i)(?:^|[._])(?:retry|retries|with_retries|backoff)(?:$|[._])",
        signals=(
            _sig(r"\bfor\s+\w+\s+in\s+range\(", "bounded attempts"),
            _sig(r"\bwhile\b", "retry loop"),
            _sig(r"\btime\.sleep\(|\bsleep\(", "sleep"),
            _sig(r"(?:\*\s*2|2\s*\*\*|\*\*\s*\w+|\*=\s*(?:2|3|4|5|10)\b|"
                 r"backoff|exponential)", "backoff"),
            _sig(r"\b(?:max_attempts|max_retries|retries|attempts)\b",
                 "attempt count"),
        ),
        min_signals=3, min_signals_named=2, min_lines=4,
        packages=("tenacity", "backoff"),
    ),
    _rule(
        "python-http", "http", "python",
        name=r"(?i)(?:^|[._])(?:fetch|get_json|post_json|http_get|http_post|"
             r"request|download|_request)(?:$|[._])",
        signals=(
            _sig(r"HTTP/1\.[01]|b[\"'](?:GET|POST) ", "manual HTTP request"),
            _sig(r"\bsocket\.(?:socket|create_connection)\b", "raw socket"),
            _sig(r"\burllib\.request\b|\burlopen\b", "uses urllib"),
            _sig(r"\brequests\.|\bhttpx\.", "uses an HTTP package"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="low",
        packages=("urllib.request", "httpx", "requests"),
    ),
    _rule(
        "python-validation", "validation", "python",
        name=r"(?i)(?:^|[._])(?:validate|validation|validator|check_schema)"
             r"(?:$|[._])",
        signals=(
            _sig(r"\bisinstance\(", "isinstance checks", minimum=2),
            _sig(r"\braise\s+(?:TypeError|ValueError|KeyError)\b",
                 "manual errors"),
            _sig(r"\brequired\b.*\b(?:in|not in)\b", "required keys"),
            _sig(r"\.keys\(\)|\bfor\s+\w+\s+in\s+payload\b", "key checks"),
        ),
        min_signals=2, min_signals_named=1, min_lines=5, confidence="low",
        packages=("pydantic", "jsonschema"),
    ),
    _rule(
        "python-cli", "cli", "python",
        name=r"(?i)(?:^|[._])(?:main|cli|parse_args|parse_cli|run_cli)(?:$|[._])",
        signals=(
            _sig(r"\bsys\.argv\b", "reads sys.argv"),
            _sig(r"\bstartswith\(\s*[\"']--|\b==\s*[\"']--", "manual flags"),
            _sig(r"\blen\(\s*sys\.argv\s*\)", "argv indexing"),
            _sig(r"(?i)\busage:", "usage text"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("argparse", "click", "typer"),
    ),
    _rule(
        "python-yaml", "config", "python",
        name=r"(?i)(?:^|[._])(?:parse_yaml|load_yaml|read_yaml|yaml|"
             r"load_config|parse_config)(?:$|[._])",
        signals=(
            _sig(r"\.splitlines\(\)", "line-by-line parsing"),
            _sig(r"\.(?:split|partition)\(\s*[\"']:[\"']\s*\)",
                 "key: value split"),
            _sig(r"\.startswith\(\s*[\"']- ", "list item parsing"),
            _sig(r"\.lstrip\(\)\.startswith\(\s*[\"']#", "comment parsing"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("PyYAML",),
    ),
    _rule(
        "python-toml", "config", "python",
        name=r"(?i)(?:^|[._])(?:parse_toml|load_toml|read_toml|toml)(?:$|[._])",
        signals=(
            _sig(r"\.(?:split|partition)\(\s*[\"']=[\"']\s*\)",
                 "key = value split"),
            _sig(r"\.startswith\(\s*[\"']\[", "section headers"),
            _sig(r"\.strip\(\)\.startswith\(\s*[\"']\[", "section headers"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("tomllib",),
    ),
    _rule(
        "python-json", "config", "python",
        name=r"(?i)(?:^|[._])(?:parse_json|load_json|json)(?:$|[._])",
        signals=(
            _sig(r"\.(?:split|partition)\(\s*[\"'],[\"']\s*\)",
                 "comma splitting"),
            _sig(r"\.(?:split|partition)\(\s*[\"']:[\"']\s*\)",
                 "key: value split"),
            _sig(r"\.strip\(\s*[\"']\{\}[\"']\s*\)", "brace stripping"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("json",),
    ),
    _rule(
        "python-datetime", "datetime", "python",
        name=r"(?i)(?:^|[._])(?:parse_date|parse_time|parse_datetime|"
             r"from_timestamp|to_datetime)(?:$|[._])",
        signals=(
            _sig(r"\\d\{4\}", "date regex"),
            _sig(r"\bint\([^)]*\[[012]\]|\bint\([^)]*group\(",
                 "manual field conversion"),
            _sig(r"\.split\(\s*[\"'][-/][\"']\s*\)", "field split"),
            _sig(r"\bstrptime\(|\bfromisoformat\(", "manual format parsing"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("datetime", "dateutil"),
    ),
    _rule(
        "python-cache", "cache", "python",
        name=r"(?i)(?:^|[._])(?:cache|cached|memoize|memo|get_or_compute)"
             r"(?:$|[._])",
        signals=(
            _sig(r"\bif\s+[^:\n]*\bin\s+self\._cache\b", "manual cache lookup"),
            _sig(r"\bif\s+\w+\s+in\s+_?cache\b", "manual cache lookup"),
            _sig(r"self\._cache\[|\b_cache\[", "cache dictionary"),
            _sig(r"\bdel\s+.*_cache|\b_cache\.pop\(", "cache eviction"),
        ),
        min_signals=2, min_signals_named=1, min_lines=3, confidence="medium",
        packages=("functools.lru_cache", "cachetools"),
    ),
    _rule(
        "python-glob", "glob_diff", "python",
        name=r"(?i)(?:^|[._])(?:glob|match_pattern|match_path|find_files|"
             r"walk_files)(?:$|[._])",
        signals=(
            _sig(r"\.replace\(\s*[\"']\*[\"']\s*,\s*[\"']\.\*[\"']",
                 "glob translated to regex"),
            _sig(r"\.replace\(\s*[\"']\?[\"']", "wildcard translation"),
            _sig(r"\bre\.(?:compile|match|fullmatch)\(", "regex matching"),
            _sig(r"\bos\.walk\(", "manual tree walk"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="high",
        packages=("fnmatch", "pathlib"),
    ),
    _rule(
        "python-diff", "glob_diff", "python",
        name=r"(?i)(?:^|[._])(?:diff|lcs|longest_common|edit_distance)"
             r"(?:$|[._])",
        signals=(
            _sig(r"\bdp\b", "DP table"),
            _sig(r"\[\[0\]\s*\*|\bmatrix\b", "matrix"),
            _sig(r"\bfor\s+\w+\s+in\s+range\(\s*1\s*,\s*\w+\s*\+\s*1\s*\)",
                 "nested index loops"),
            _sig(r"\bLCS\b", "LCS"),
        ),
        min_signals=2, min_signals_named=2, min_lines=4, confidence="medium",
        packages=("difflib",),
    ),
    _rule(
        "python-slug", "ids", "python",
        name=r"(?i)(?:^|[._])(?:slugify|make_slug|slug|to_slug)(?:$|[._])",
        signals=(
            _sig(r"\.lower\(\)", "lowercased"),
            _sig(r"\bre\.sub\(|\bre\.compile\(", "regex replacement"),
            _sig(r"\.replace\(\s*[\"'] [\"']\s*,\s*[\"']-[\"']",
                 "spaces to hyphens"),
            _sig(r"\.strip\(\s*[\"']-[\"']\s*\)", "hyphen cleanup"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="medium",
        packages=("python-slugify",),
    ),
    _rule(
        "python-id", "ids", "python",
        name=r"(?i)(?:^|[._])(?:generate_id|make_id|new_id|unique_id|"
             r"random_id|token)(?:$|[._])",
        signals=(
            _sig(r"\brandom\.(?:random|randint|choice)\b", "random module"),
            _sig(r"\bos\.urandom\b", "urandom"),
            _sig(r"\bhashlib\.", "hash of clock/random"),
            _sig(r"\btime\.time\(\)|\bstr\(time\.", "clock seed"),
        ),
        min_signals=2, min_signals_named=1, min_lines=2, confidence="medium",
        packages=("secrets", "uuid"),
    ),
    _rule(
        "python-rate", "rate_limit", "python",
        name=r"(?i)(?:^|[._])(?:rate_limit|ratelimit|throttle|limiter|wait)"
             r"(?:$|[._])",
        signals=(
            _sig(r"\btime\.monotonic\(\)|\btime\.time\(\)", "clock check"),
            _sig(r"\btime\.sleep\(", "sleep to wait"),
            _sig(r"\bself\._(?:interval|period|rate)\b", "interval budget"),
        ),
        min_signals=3, min_signals_named=2, min_lines=4, confidence="low",
        packages=("limits", "slowapi"),
    ),
    _rule(
        "python-semver", "semver", "python",
        name=r"(?i)(?:^|[._])(?:parse_version|version_tuple|compare_version|"
             r"is_newer|semver|version)(?:$|[._])",
        signals=(
            _sig(r"\.split\(\s*[\"']\.[\"']\s*\)", "version split on '.'"),
            _sig(r"\bint\([^)]*\)", "numeric parts"),
            _sig(r"\b(?:major|minor|patch)\b", "major/minor fields"),
            _sig(r"tuple\([^)]*\)\s*[<>]=?", "tuple comparison"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("packaging", "semver"),
    ),
    _rule(
        "python-circuit", "circuit", "python",
        name=r"(?i)(?:^|[._])(?:circuit|breaker|fuse|guard)(?:$|[._])",
        signals=(
            _sig(r"\bfailures?\s*\+=", "failure counter"),
            _sig(r"\bif\s+.*\bfailures?\s*>=?", "failure threshold"),
            _sig(r"\b(?:half_open|tripped|is_open|state\s*=\s*[\"']open)",
                 "open state"),
        ),
        min_signals=2, min_signals_named=2, min_lines=5, confidence="medium",
        packages=("pybreaker",),
    ),
)


_JAVASCRIPT_RULES: tuple[Rule, ...] = (
    _rule(
        "javascript-retry", "retry", "javascript",
        name=r"(?i)(?:^|[._$])(?:retry|fetchWithRetry|withRetry|backoff)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bfor\s*\(", "bounded attempts"),
            _sig(r"\bsetTimeout\s*\(", "delay"),
            _sig(r"\battempts?\b", "attempt count"),
            _sig(r"Math\.pow\(|\*\s*2\b|\bbackoff\b", "backoff"),
        ),
        min_signals=3, min_signals_named=2, min_lines=4,
        packages=("p-retry",),
    ),
    _rule(
        "javascript-http", "http", "javascript",
        name=r"(?i)(?:^|[._$])(?:fetch|request|http|getJson|postJson|api)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bhttps?\.request\b|\bXMLHttpRequest\b",
                 "manual HTTP request"),
            _sig(r"\bnew\s+URL\b", "URL construction"),
            _sig(r"\bmethod\s*:\s*[\"'](?:GET|POST|PUT|DELETE)",
                 "request options"),
            _sig(r"\bheaders\s*:\s*\{", "request headers"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="low",
        packages=("fetch", "axios", "undici"),
    ),
    _rule(
        "javascript-validation", "validation", "javascript",
        name=r"(?i)(?:^|[._$])(?:validate|validator|checkSchema|parseSchema)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\btypeof\s+\w+\s*!==?", "typeof checks"),
            _sig(r"\bArray\.isArray\(", "array checks"),
            _sig(r"\bthrow\s+new\s+(?:TypeError|Error)\b", "manual errors"),
            _sig(r"\bObject\.keys\(", "key checks"),
        ),
        min_signals=2, min_signals_named=1, min_lines=4, confidence="low",
        packages=("zod", "ajv"),
    ),
    _rule(
        "javascript-cli", "cli", "javascript",
        name=r"(?i)(?:^|[._$])(?:main|cli|parseArgs|runCli)(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bprocess\.argv\b", "reads process.argv"),
            _sig(r"\bprocess\.exit\b", "process exit"),
            _sig(r"\bswitch\s*\(", "manual switch"),
            _sig(r"\b(?:startsWith|==)\s*\(\s*[\"']--", "manual flags"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("commander", "yargs"),
    ),
    _rule(
        "javascript-yaml", "config", "javascript",
        name=r"(?i)(?:^|[._$])(?:parseYaml|loadYaml|yaml|loadConfig)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\.split\(\s*[\"']\\n[\"']\s*\)", "line splitting"),
            _sig(r"\.split\(\s*[\"']:[\"']\s*\)", "key: value split"),
            _sig(r"\.startsWith\(\s*[\"']- ", "list item parsing"),
            _sig(r"\.trim\(\)\.split\(", "line-by-line parsing"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("yaml",),
    ),
    _rule(
        "javascript-datetime", "datetime", "javascript",
        name=r"(?i)(?:^|[._$])(?:parseDate|parseTime|formatDate|toDate)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bnew\s+Date\(", "Date construction"),
            _sig(r"\bgetFullYear\(|\bgetMonth\(|\bgetDate\(", "date fields"),
            _sig(r"\bDate\.parse\(", "Date.parse"),
            _sig(r"\.split\(\s*[\"'][-/][\"']\s*\)", "field split"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("date-fns", "luxon"),
    ),
    _rule(
        "javascript-cache", "cache", "javascript",
        name=r"(?i)(?:^|[._$])(?:cache|cached|memoize|memo)(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bnew\s+(?:Map|WeakMap)\(", "manual map cache"),
            _sig(r"\.has\(", "cache lookup"),
            _sig(r"\.set\(", "cache store"),
            _sig(r"\bDate\.now\(\)", "clock"),
        ),
        min_signals=3, min_signals_named=2, min_lines=4, confidence="medium",
        packages=("lru-cache",),
    ),
    _rule(
        "javascript-glob", "glob_diff", "javascript",
        name=r"(?i)(?:^|[._$])(?:glob|matchPath|match|diff|patch)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\.replace\([^)]*\*", "glob translated to regex"),
            _sig(r"\bnew\s+RegExp\(", "regex matching"),
            _sig(r"\.startsWith\(\s*[\"']@@", "diff hunk parsing"),
            _sig(r"\.split\(\s*[\"']\\n[\"']\s*\)", "line splitting"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("picomatch", "diff"),
    ),
    _rule(
        "javascript-slug", "ids", "javascript",
        name=r"(?i)(?:^|[._$])(?:slugify|slug|makeId|newId|generateId)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\.toLowerCase\(\)", "lowercased"),
            _sig(r"\.replace\([^)]*[^a-zA-Z0-9]", "non-alphanumerics replaced"),
            _sig(r"\.replace\([^)]*[\"'] [\"']", "spaces replaced"),
            _sig(r"Math\.random\(\)|\btoString\(36\)", "random id source"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="medium",
        packages=("nanoid", "slugify"),
    ),
    _rule(
        "javascript-rate", "rate_limit", "javascript",
        name=r"(?i)(?:^|[._$])(?:rateLimit|throttle|limiter)(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bDate\.now\(\)", "clock check"),
            _sig(r"\b(?:lastCall|lastRequest|lastTime)\b", "last-call time"),
            _sig(r"\bsetTimeout\s*\(", "sleep to wait"),
            _sig(r"\b(?:interval|minInterval|period)\b", "interval budget"),
        ),
        min_signals=2, min_signals_named=2, min_lines=4, confidence="low",
        packages=("rate-limiter-flexible",),
    ),
    _rule(
        "javascript-semver", "semver", "javascript",
        name=r"(?i)(?:^|[._$])(?:parseVersion|compareVersion|version|semver)"
             r"(?:$|[._$A-Z])",
        signals=(
            _sig(r"\.split\(\s*[\"']\.[\"']\s*\)", "version split on '.'"),
            _sig(r"\bparseInt\(", "numeric parts"),
            _sig(r"\b(?:major|minor|patch)\b", "major/minor fields"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("semver",),
    ),
    _rule(
        "javascript-circuit", "circuit", "javascript",
        name=r"(?i)(?:^|[._$])(?:circuit|breaker|guard)(?:$|[._$A-Z])",
        signals=(
            _sig(r"\bfailures?\b", "failure counter"),
            _sig(r"\bthreshold\b", "failure threshold"),
            _sig(r"\b(?:halfOpen|isOpen|open|tripped)\b", "open state"),
        ),
        min_signals=2, min_signals_named=2, min_lines=5, confidence="medium",
        packages=("opossum",),
    ),
)


_RUST_RULES: tuple[Rule, ...] = (
    _rule(
        "rust-retry", "retry", "rust",
        name=r"(?:^|[._])(?:retry|with_retries|backoff)(?:$|[._])",
        signals=(
            _sig(r"\bloop\s*\{", "retry loop"),
            _sig(r"\bsleep\s*\(|Duration::from_millis", "sleep"),
            _sig(r"\battempts?\s*\+=|\bfor\s+_?\s*in\s+0\.\.",
                 "bounded attempts"),
            _sig(r"\bbackoff\b|Duration::from_secs", "backoff"),
        ),
        min_signals=2, min_signals_named=2, min_lines=4,
        packages=("backoff", "tokio-retry"),
    ),
    _rule(
        "rust-http", "http", "rust",
        name=r"(?:^|[._])(?:fetch|request|http|get|post)(?:$|[._A-Z])",
        signals=(
            _sig(r"\bTcpStream\b", "raw socket"),
            _sig(r"\bRequest::|\bResponse::", "manual HTTP request"),
            _sig(r"\.header\(", "request headers"),
            _sig(r"https?://", "URL construction"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="low",
        packages=("reqwest",),
    ),
    _rule(
        "rust-validation", "validation", "rust",
        name=r"(?:^|[._])(?:validate|validator|check_schema)(?:$|[._])",
        signals=(
            _sig(r"\.is_empty\(\)", "emptiness checks"),
            _sig(r"\breturn\s+Err\(", "manual errors"),
            _sig(r"\bif\s+.*\.len\(\)", "length checks"),
            _sig(r"\bserde\b", "serde attributes"),
        ),
        min_signals=2, min_signals_named=1, min_lines=4, confidence="low",
        packages=("serde", "validator"),
    ),
    _rule(
        "rust-cli", "cli", "rust",
        name=r"(?:^|[._])(?:main|cli|parse_args|run_cli)(?:$|[._])",
        signals=(
            _sig(r"\benv::args\(|std::env", "reads process arguments"),
            _sig(r"\bmatch\s+\w+\s*\{", "manual switch"),
            _sig(r"""["']--""", "manual flags"),
            _sig(r"\bprocess::exit\b", "process exit"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("clap",),
    ),
    _rule(
        "rust-config", "config", "rust",
        name=r"(?:^|[._])(?:parse_yaml|parse_toml|load_config|yaml|toml)"
             r"(?:$|[._])",
        signals=(
            _sig(r"\.lines\(\)", "line-by-line parsing"),
            _sig(r'\.split\(\s*":"|\.split\(\s*"="', "key/value split"),
            _sig(r'\.starts_with\(\s*"\["', "section headers"),
            _sig(r"\btrim_start_matches\b", "manual trimming"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="high",
        packages=("toml", "serde_yaml"),
    ),
    _rule(
        "rust-datetime", "datetime", "rust",
        name=r"(?:^|[._])(?:parse_date|parse_time|parse_datetime|from_timestamp)"
             r"(?:$|[._])",
        signals=(
            _sig(r'\.split\(\s*"[-/]"', "field split"),
            _sig(r"\bparse::<", "manual numeric parsing"),
            _sig(r"\bNaiveDate\b|\bDateTime\b", "manual date construction"),
            _sig(r"\btimestamp\b", "timestamp conversion"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("chrono", "time"),
    ),
    _rule(
        "rust-cache", "cache", "rust",
        name=r"(?:^|[._])(?:cache|cached|memoize|memo)(?:$|[._])",
        signals=(
            _sig(r"\bHashMap\b", "manual map cache"),
            _sig(r"\.get\(&", "cache lookup"),
            _sig(r"\.insert\(", "cache store"),
            _sig(r"\bInstant::now\b", "clock"),
        ),
        min_signals=3, min_signals_named=2, min_lines=4, confidence="medium",
        packages=("moka", "cached"),
    ),
    _rule(
        "rust-glob", "glob_diff", "rust",
        name=r"(?:^|[._])(?:glob|match_path|match|diff|patch)(?:$|[._])",
        signals=(
            _sig(r"\bPattern::new\(|\bglob::", "glob matching"),
            _sig(r"\.lines\(\)", "line-by-line parsing"),
            _sig(r'\.starts_with\(\s*"@@"', "diff hunk parsing"),
            _sig(r"\bHashSet\b", "manual matching"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("globset", "similar"),
    ),
    _rule(
        "rust-ids", "ids", "rust",
        name=r"(?:^|[._])(?:slugify|slug|new_id|generate_id|uuid)(?:$|[._])",
        signals=(
            _sig(r"\.to_lowercase\(\)", "lowercased"),
            _sig(r"\.replace\(", "non-alphanumerics replaced"),
            _sig(r"\bUuid::new_v4\b|\brand::", "random id source"),
            _sig(r"\.to_string\(\)", "string conversion"),
        ),
        min_signals=2, min_signals_named=2, min_lines=2, confidence="medium",
        packages=("uuid", "slug"),
    ),
    _rule(
        "rust-rate", "rate_limit", "rust",
        name=r"(?:^|[._])(?:rate_limit|throttle|limiter)(?:$|[._])",
        signals=(
            _sig(r"\bInstant::now\b", "clock check"),
            _sig(r"\bDuration::", "interval budget"),
            _sig(r"\bsleep\(", "sleep to wait"),
            _sig(r"\blast_call\b|\blast_request\b", "last-call time"),
        ),
        min_signals=3, min_signals_named=2, min_lines=4, confidence="low",
        packages=("governor",),
    ),
    _rule(
        "rust-semver", "semver", "rust",
        name=r"(?:^|[._])(?:parse_version|compare_version|semver|version)"
             r"(?:$|[._])",
        signals=(
            _sig(r'\.split\(\s*"\."', "version split on '.'"),
            _sig(r"\bparse::<u", "numeric parts"),
            _sig(r"\b(?:major|minor|patch)\b", "major/minor fields"),
            _sig(r"\.cmp\(", "manual comparison"),
        ),
        min_signals=2, min_signals_named=2, min_lines=3, confidence="medium",
        packages=("semver",),
    ),
    _rule(
        "rust-circuit", "circuit", "rust",
        name=r"(?:^|[._])(?:circuit|breaker|guard)(?:$|[._])",
        signals=(
            _sig(r"\bfailures?\b", "failure counter"),
            _sig(r"\bthreshold\b", "failure threshold"),
            _sig(r"\b(?:half_open|is_open|open|tripped)\b", "open state"),
        ),
        min_signals=2, min_signals_named=2, min_lines=5, confidence="medium",
        packages=("recloser",),
    ),
)


# --------------------------------------------------------------------------- #
# settings and opt-out
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HintSettings:
    mode: str = "gentle"
    max_per_edit: int = 2
    record: bool = True
    ecosystems: tuple[str, ...] = ECOSYSTEMS

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "max_per_edit": self.max_per_edit,
                "record": self.record, "ecosystems": list(self.ecosystems)}


def _normalize_mode(value: Any) -> str | None:
    text = str(value).strip().lower()
    return text if text in MODES else None


def _normalize_ecosystems(values: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = str(value).strip().lower()
        if text == "typescript":
            text = "javascript"
        if text in ECOSYSTEMS and text not in out:
            out.append(text)
    return tuple(out)


def settings_for(root: str | Path) -> HintSettings:
    """The effective hint settings. A malformed config falls back to defaults;
    an edit must never fail for a hint preference."""
    mode = "gentle"
    max_per_edit = 2
    record = True
    ecosystems: tuple[str, ...] = ECOSYSTEMS
    try:
        from ozagia.core import settings as S
        values = S.values(root)
        mode = _normalize_mode(values["hints.library"]["value"]) or mode
        raw_max = values["hints.max_per_edit"]["value"]
        if isinstance(raw_max, int) and not isinstance(raw_max, bool):
            max_per_edit = max(0, min(int(raw_max), 20))
        record = bool(values["hints.record"]["value"])
        raw_eco = values["hints.ecosystems"]["value"]
        if isinstance(raw_eco, (list, tuple)):
            ecosystems = _normalize_ecosystems(raw_eco)
    except Exception:  # noqa: BLE001 - hints are advisory, never fatal
        pass
    env = os.environ.get(ENV_HINTS, "").strip().lower()
    if env in ("0", "false", "no", "off"):
        mode = "off"
    elif env in MODES:
        mode = env
    return HintSettings(mode=mode, max_per_edit=max_per_edit, record=record,
                        ecosystems=ecosystems)


def language_of(rel: str) -> str | None:
    return LANGUAGE_SUFFIXES.get(PurePosixPath(rel).suffix.lower())


def enabled_for(rel: str, settings: HintSettings) -> bool:
    language = language_of(rel)
    return language is not None and language in settings.ecosystems


def events_path(root: str | Path) -> Path:
    return state_path(root, EVENTS_DIR, EVENTS_FILE)


def feedback_path(root: str | Path) -> Path:
    return state_path(root, EVENTS_DIR, FEEDBACK_FILE)


# --------------------------------------------------------------------------- #
# text, chunks and changed symbols
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FileChange:
    rel: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class Chunk:
    symbol: str
    text: str
    start: int
    end: int
    module: bool = False
    lines: frozenset[int] = frozenset()


def read_source(path: str | Path, *, max_bytes: int = MAX_FILE_BYTES
                ) -> str | None:
    """The UTF-8 text of a source file, or None for binary/oversized/missing."""
    p = Path(path)
    try:
        if p.stat().st_size > max_bytes:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("\ufeff") else text


def _normalize_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _line_count(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "//", "/*", "*", "--")):
            count += 1
    return count


def _python_chunks(text: str) -> list[Chunk]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    lines = text.splitlines()
    chunks: list[Chunk] = []
    covered: set[int] = set()

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                decorators = [d.lineno for d in child.decorator_list]
                start = min([child.lineno, *decorators])
                end = int(getattr(child, "end_lineno", start) or start)
                body = "\n".join(lines[start - 1:end])
                chunks.append(Chunk(qualname, body, start, end))
                covered.update(range(start, min(end, len(lines)) + 1))
                visit(child, qualname)
            elif isinstance(child, ast.ClassDef):
                class_name = f"{prefix}.{child.name}" if prefix else child.name
                start = int(getattr(child, "lineno", 1) or 1)
                end = int(getattr(child, "end_lineno", start) or start)
                covered.update(range(start, min(end, len(lines)) + 1))
                visit(child, class_name)
            else:
                visit(child, prefix)

    visit(tree, "")
    module_lines = [
        "" if index in covered else line
        for index, line in enumerate(lines, start=1)
    ]
    module_text = "\n".join(module_lines).strip("\n")
    if module_text.strip():
        line_numbers = frozenset(
            index for index, line in enumerate(lines, start=1)
            if line.strip() and index not in covered)
        chunks.append(Chunk("<module>", module_text, 1, len(lines),
                            module=True, lines=line_numbers))
    if not chunks:
        chunks.append(Chunk("<module>", text, 1, max(1, len(lines)),
                            module=True))
    return chunks[:MAX_SYMBOLS]


_JS_FUNCTION_START = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+"
    r"([A-Za-z_$][\w$]*)")
_JS_ASSIGNED = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
_RUST_FN = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+"
    r"([A-Za-z_]\w*)")


def _brace_span(lines: list[str], start: int, limit: int) -> int:
    depth = 0
    seen = False
    for index in range(start, min(len(lines), start + limit)):
        line = lines[index]
        if line.lstrip().startswith(("//", "/*", "*")):
            continue
        depth += line.count("{") - line.count("}")
        if "{" in line:
            seen = True
        if seen and depth <= 0:
            return index + 1
        if not seen and index > start and line.rstrip().endswith(";"):
            return index + 1
    return min(len(lines), start + limit)


def _heuristic_chunks(text: str, language: str) -> list[Chunk]:
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = (_JS_FUNCTION_START.match(line) if language == "javascript"
                 else _RUST_FN.match(line))
        if match is None and language == "javascript":
            match = _JS_ASSIGNED.match(line)
        if match is not None:
            starts.append((index, match.group(1)))
    chunks: list[Chunk] = []
    covered: set[int] = set()
    for position, (index, name) in enumerate(starts):
        end = _brace_span(lines, index, MAX_SYMBOLS)
        if position + 1 < len(starts):
            end = min(end, starts[position + 1][0] + 1)
        body = "\n".join(lines[index:end])
        chunks.append(Chunk(name, body, index + 1, end))
        covered.update(range(index + 1, end + 1))
    module_lines = [
        "" if index in covered else line
        for index, line in enumerate(lines, start=1)
    ]
    module_text = "\n".join(module_lines).strip("\n")
    if module_text.strip():
        line_numbers = frozenset(
            index for index, line in enumerate(lines, start=1)
            if line.strip() and index not in covered)
        chunks.append(Chunk("<module>", module_text, 1, len(lines),
                            module=True, lines=line_numbers))
    if not chunks:
        chunks.append(Chunk("<module>", text, 1, max(1, len(lines)),
                            module=True))
    return chunks[:MAX_SYMBOLS]


def _chunks_for(rel: str, text: str) -> list[Chunk]:
    language = language_of(rel)
    if language == "python":
        return _python_chunks(text)
    if language in ("javascript", "rust"):
        return _heuristic_chunks(text, language)
    return []


def _changed_lines(before: str, after: str) -> set[int]:
    a = before.splitlines()
    b = after.splitlines()
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if j2 > j1:
            changed.update(range(j1 + 1, j2 + 1))
        else:
            for line in (j1, j1 + 1):
                if 1 <= line <= len(b):
                    changed.add(line)
    return changed


def _select_chunks(rel: str, before: str | None, after: str,
                   mode: str) -> list[Chunk]:
    chunks = _chunks_for(rel, after)
    if mode == "strict" or before is None:
        return chunks
    before_chunks = {chunk.symbol: _normalize_text(chunk.text)
                     for chunk in _chunks_for(rel, before)}
    changed = _changed_lines(before, after)
    selected: list[Chunk] = []
    for chunk in chunks:
        previous = before_chunks.get(chunk.symbol)
        if (previous is None
                or previous != _normalize_text(chunk.text)
                or (chunk.module and bool(chunk.lines & changed))
                or any(chunk.start <= line <= chunk.end for line in changed)):
            selected.append(chunk)
    return selected


def _short_symbol(symbol: str) -> str:
    return symbol.rsplit(".", 1)[-1]


# --------------------------------------------------------------------------- #
# imports, manifests and availability
# --------------------------------------------------------------------------- #
def _add_module(out: set[str], name: str, *, scoped: bool = False) -> None:
    name = name.strip().strip("\"'")
    if not name:
        return
    out.add(name.lower())
    if scoped and name.startswith("@") and "/" in name:
        scope, _, package = name.partition("/")
        out.add(f"{scope}/{package}".lower())
        return
    out.add(name.split("/", 1)[0].lower())
    out.add(name.split(".", 1)[0].lower())


def _imported_modules(source: str, language: str) -> set[str]:
    out: set[str] = set()
    if language == "python":
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        _add_module(out, alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    _add_module(out, node.module)
            return out
        for match in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
                                 source, re.MULTILINE):
            _add_module(out, match.group(1) or match.group(2) or "")
        return out
    if language == "javascript":
        for match in re.finditer(
                r"(?:import\s+(?:[^\"']*?\s+from\s+)?[\"']([^\"']+)[\"']"
                r"|require\(\s*[\"']([^\"']+)[\"']\s*\)"
                r"|import\(\s*[\"']([^\"']+)[\"']\s*\))", source):
            module = next(value for value in match.groups() if value)
            _add_module(out, module, scoped=True)
        return out
    for match in re.finditer(
            r"^\s*(?:pub\s+)?use\s+([A-Za-z0-9_]+)|^\s*extern\s+crate\s+"
            r"([A-Za-z0-9_]+)", source, re.MULTILINE):
        _add_module(out, match.group(1) or match.group(2) or "")
    return out


def _dependency_name(value: str) -> str | None:
    text = value.strip()
    if not text or text.startswith(("-", "#", "/")):
        return None
    match = re.match(r"([A-Za-z0-9@][A-Za-z0-9@._/-]*)", text)
    if not match:
        return None
    name = match.group(1).rstrip(".,;")
    if name.startswith("@") and name.count("@") > 1:
        # @scope/package@1.2 -> @scope/package
        scope, _, rest = name.partition("/")
        package = rest.split("@", 1)[0]
        name = f"{scope}/{package}"
    return name or None


def _normalize_dep(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _dep_names(values: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if isinstance(value, str):
            name = _dependency_name(value)
            if name:
                out.add(_normalize_dep(name))
    return out


def _pyproject_dependencies(path: Path) -> set[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    found: set[str] = set()
    project = data.get("project")
    if isinstance(project, Mapping):
        found |= _dep_names(project.get("dependencies") or ())
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for values in optional.values():
                found |= _dep_names(values if isinstance(values, list) else ())
    poetry = data.get("tool", {})
    if isinstance(poetry, Mapping):
        poetry = poetry.get("poetry", {})
    if isinstance(poetry, Mapping):
        deps = poetry.get("dependencies")
        if isinstance(deps, Mapping):
            found |= {_normalize_dep(str(name)) for name in deps}
        groups = poetry.get("group")
        if isinstance(groups, Mapping):
            for group in groups.values():
                if isinstance(group, Mapping):
                    deps = group.get("dependencies")
                    if isinstance(deps, Mapping):
                        found |= {_normalize_dep(str(name)) for name in deps}
    groups = data.get("dependency-groups")
    if isinstance(groups, Mapping):
        for values in groups.values():
            if isinstance(values, list):
                found |= _dep_names(values)
    return found


def _requirements_dependencies(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    found: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        name = _dependency_name(line)
        if name:
            found.add(_normalize_dep(name))
    return found


def _package_json_dependencies(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return set()
    found: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
        values = data.get(section)
        if isinstance(values, Mapping):
            found |= {_normalize_dep(str(name)) for name in values}
    return found


def _cargo_dependencies(path: Path) -> set[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return set()
    found: set[str] = set()

    def walk(table: Mapping[str, Any]) -> None:
        for key, value in table.items():
            if isinstance(value, Mapping):
                if "dependencies" in str(key):
                    found.update(_normalize_dep(str(name)) for name in value)
                else:
                    walk(value)

    walk(data)
    return found


def _declared_dependencies(root: Path, rel: str, language: str,
                           cache: dict[tuple[str, str], set[str]]) -> set[str]:
    parent = PurePosixPath(rel).parent
    directories = [parent, *parent.parents]
    found: set[str] = set()
    for directory in directories:
        key = (str(directory), language)
        if key in cache:
            found |= cache[key]
            continue
        base = root / str(directory)
        values: set[str] = set()
        try:
            if language == "python":
                pyproject = base / "pyproject.toml"
                if pyproject.is_file():
                    values |= _pyproject_dependencies(pyproject)
                for requirements in sorted(base.glob("requirements*.txt"))[:10]:
                    values |= _requirements_dependencies(requirements)
            elif language == "javascript":
                package = base / "package.json"
                if package.is_file():
                    values |= _package_json_dependencies(package)
            elif language == "rust":
                cargo = base / "Cargo.toml"
                if cargo.is_file():
                    values |= _cargo_dependencies(cargo)
        except OSError:
            values = set()
        cache[key] = values
        found |= values
    return found


def _installed_version(dist: str | None) -> str | None:
    if not dist:
        return None
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - metadata must never fail a hint
        return None


def _availability(root: Path, rel: str, language: str, candidate: Candidate,
                  cache: dict[tuple[str, str], set[str]]) -> str:
    if candidate.stdlib:
        return "stdlib"
    declared = _declared_dependencies(root, rel, language, cache)
    names = {candidate.package, candidate.dist or candidate.package}
    if any(_normalize_dep(name) in declared for name in names):
        return "declared"
    if language == "python" and _installed_version(candidate.dist):
        return "installed"
    return "known"


def _candidate_used(source: str, candidate: Candidate, language: str,
                    imports: set[str]) -> bool:
    if candidate.usage and re.search(candidate.usage, source, re.MULTILINE):
        return True
    modules = candidate.modules or (candidate.package,)
    imported = False
    for module in modules:
        normalized = _normalize_dep(module)
        if any(_normalize_dep(name) == normalized for name in imports):
            imported = True
            break
    if not imported:
        return False
    if candidate.stdlib and candidate.usage:
        # Importing functools is not using functools.lru_cache.
        return False
    return True


def _choose_candidate(rule: Rule, language: str, availability: dict[str, str]
                      ) -> tuple[Candidate | None, str | None]:
    candidates = {candidate.package: candidate
                  for candidate in _candidates(rule.capability, language)}
    order = rule.packages or tuple(candidates)
    known = [candidates[name] for name in order if name in candidates]
    if not known:
        return None, None
    for candidate in known:
        if AVAILABILITY_RANK.get(availability.get(candidate.package, ""), 0) >= 2:
            return candidate, availability.get(candidate.package)
    first = known[0]
    return first, availability.get(first.package, "known")


# --------------------------------------------------------------------------- #
# matching and hints
# --------------------------------------------------------------------------- #
def _match_rule(rule: Rule, chunk: Chunk) -> tuple[list[Signal], bool] | None:
    if _line_count(chunk.text) < rule.min_lines:
        return None
    matched: list[Signal] = []
    for signal in rule.signals:
        count = sum(1 for _ in signal.pattern.finditer(chunk.text))
        if count >= signal.minimum:
            matched.append(signal)
    name_match = bool(rule.name and rule.name.search(_short_symbol(chunk.symbol)))
    if name_match and len(matched) >= rule.min_signals_named:
        return matched, True
    if len(matched) >= rule.min_signals:
        return matched, name_match
    return None


def _evidence(rule: Rule, matched: Sequence[Signal], name_match: bool,
              chunk: Chunk) -> str:
    labels = [signal.label for signal in matched]
    if name_match:
        labels.append(f"name `{_short_symbol(chunk.symbol)}`")
    return ", ".join(labels[:MAX_EVIDENCE_SIGNALS])


def _hint_id(file: str, symbol: str, capability: str, package: str) -> str:
    payload = "\0".join((RULE_VERSION, file, symbol, capability, package))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _one_line(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _message(file: str, symbol: str, capability: str, candidate: Candidate,
             availability: str | None, evidence: str, confidence: str) -> str:
    level = availability or "known"
    title = _CAPABILITIES[capability]["title"]
    if level == "stdlib":
        head = (f"[ozagia] library hint: `{file}::{symbol}` {title} is "
                f"available from the {AVAILABILITY_TEXT[level]}: "
                f"{candidate.package}.")
    else:
        head = (f"[ozagia] library hint: `{file}::{symbol}` {title} resembles "
                f"{candidate.package} ({AVAILABILITY_TEXT[level]}).")
    note = candidate.note
    if not note and level != "stdlib":
        note = str(_CAPABILITIES[capability].get("stdlib_note") or "")
    if note:
        head += " " + note.rstrip(".") + "."
    head += f" Evidence: {evidence}. Confidence: {confidence}."
    return _one_line(head, MAX_MESSAGE_CHARS)


def _hint(file: str, symbol: str, language: str, capability: str,
          candidate: Candidate, availability: str, evidence: str,
          confidence: str) -> dict[str, Any]:
    return {
        "hint_id": _hint_id(file, symbol, capability, candidate.package),
        "file": file,
        "symbol": symbol,
        "language": language,
        "capability": capability,
        "package": candidate.package,
        "confidence": confidence,
        "evidence": evidence,
        "availability": availability,
        "message": _message(file, symbol, capability, candidate, availability,
                            evidence, confidence),
    }


def _suppressed(file: str, symbol: str, language: str, capability: str,
                candidate: Candidate, availability: str, confidence: str,
                reason: str) -> dict[str, Any]:
    return {
        "hint_id": _hint_id(file, symbol, capability, candidate.package),
        "file": file,
        "symbol": symbol,
        "language": language,
        "capability": capability,
        "package": candidate.package,
        "confidence": confidence,
        "availability": availability,
        "reason": reason,
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _event(item: Mapping[str, Any], *, status: str, reason: str | None,
           session: str | None) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ts": _now(),
        "session": _one_line(session or "", MAX_SESSION_CHARS) or None,
        "file": _one_line(item.get("file") or "", MAX_FILE_CHARS) or None,
        "symbol": _one_line(item.get("symbol") or "", MAX_SYMBOL_CHARS) or None,
        "capability": item.get("capability"),
        "package": item.get("package"),
        "availability": item.get("availability"),
        "confidence": item.get("confidence"),
        "status": status,
        "reason": reason,
        "rule_version": RULE_VERSION,
        "hint_id": item.get("hint_id"),
    }


def _empty_result(mode: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "mode": mode,
        "hints": [],
        "suppressed": [],
        "counts": {"hints": 0, "suppressed": 0, "files": 0},
        "recorded": False,
        "events_path": None,
    }


def _safe_rel(root: Path, rel: Any) -> str | None:
    if not isinstance(rel, (str, os.PathLike)):
        return None
    raw = Path(str(rel))
    if raw.is_absolute():
        try:
            raw = raw.resolve().relative_to(root)
        except (OSError, ValueError):
            return None
    text = str(raw).replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _detect_file(root: Path, rel: str, before: str | None, after: str,
                 *, mode: str, max_per_edit: int,
                 cache: dict[tuple[str, str], set[str]]) -> tuple[
                     list[dict[str, Any]], list[dict[str, Any]]]:
    language = language_of(rel)
    if language is None:
        return [], []
    chunks = _select_chunks(rel, before, after, mode)
    if not chunks:
        return [], []
    imports = _imported_modules(after, language)
    shown: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    used: set[str] = set()
    rules = _rules(language)
    for chunk in chunks:
        for rule in rules:
            match = _match_rule(rule, chunk)
            if match is None:
                continue
            matched, name_match = match
            candidates = _candidates(rule.capability, language)
            availability = {
                candidate.package: _availability(
                    root, rel, language, candidate, cache)
                for candidate in candidates
            }
            if rule.capability in used:
                candidate, level = _choose_candidate(rule, language, availability)
                if candidate is not None:
                    suppressed.append(_suppressed(
                        rel, chunk.symbol, language, rule.capability, candidate,
                        level or "known", rule.confidence, "duplicate"))
                continue
            used_candidate = None
            for candidate in candidates:
                if _candidate_used(after, candidate, language, imports):
                    used_candidate = candidate
                    break
            if used_candidate is not None:
                level = availability.get(used_candidate.package, "known")
                thin = _line_count(chunk.text) <= 3
                suppressed.append(_suppressed(
                    rel, chunk.symbol, language, rule.capability,
                    used_candidate, level, rule.confidence,
                    "thin_wrapper" if thin else "already_imported"))
                used.add(rule.capability)
                continue
            candidate, level = _choose_candidate(rule, language, availability)
            if candidate is None:
                continue
            evidence = _evidence(rule, matched, name_match, chunk)
            shown.append(_hint(rel, chunk.symbol, language, rule.capability,
                               candidate, level or "known", evidence,
                               rule.confidence))
            used.add(rule.capability)
    shown.sort(key=lambda item: (
        _CAPABILITY_ORDER.get(str(item["capability"]), 99),
        -CONFIDENCE_RANK.get(str(item["confidence"]), 0),
        str(item["symbol"]), str(item["package"])))
    kept = shown[:max(0, max_per_edit)]
    for item in shown[max(0, max_per_edit):]:
        suppressed.append({
            "hint_id": item["hint_id"],
            "file": item["file"],
            "symbol": item["symbol"],
            "language": item["language"],
            "capability": item["capability"],
            "package": item["package"],
            "confidence": item["confidence"],
            "availability": item["availability"],
            "reason": "max_per_edit",
        })
    return kept, suppressed


def detect_files(root: str | Path, changes: Iterable[FileChange], *,
                 mode: str = "gentle", max_per_edit: int = 2,
                 ecosystems: Sequence[str] = ECOSYSTEMS,
                 session: str | None = None,
                 record: bool = False) -> dict[str, Any]:
    """Detect hints for an explicit set of file changes.

    ``before`` is the full previous text (None for a new file); ``after`` is
    the current text (None for a deleted file, which is skipped). The caller
    chooses the files: this function never scans the repository.
    """
    mode = _normalize_mode(mode) or "gentle"
    if mode == "off":
        return _empty_result(mode)
    allowed = set(_normalize_ecosystems(ecosystems))
    root_p = Path(root).resolve()
    max_per_edit = max(0, min(int(max_per_edit), 20))
    hints: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    cache: dict[tuple[str, str], set[str]] = {}
    for change in changes:
        if len(seen_files) >= MAX_HINT_FILES:
            break
        rel = _safe_rel(root_p, change.rel)
        if rel is None or rel in seen_files:
            continue
        language = language_of(rel)
        if language is None or language not in allowed:
            continue
        after = change.after
        if after is None or len(after.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
            continue
        seen_files.add(rel)
        hints_file, suppressed_file = _detect_file(
            root_p, rel, change.before, after, mode=mode,
            max_per_edit=max_per_edit, cache=cache)
        hints.extend(hints_file)
        suppressed.extend(suppressed_file)
    hints.sort(key=lambda item: (
        str(item["file"]),
        _CAPABILITY_ORDER.get(str(item["capability"]), 99),
        -CONFIDENCE_RANK.get(str(item["confidence"]), 0),
        str(item["symbol"]), str(item["package"])))
    suppressed.sort(key=lambda item: (
        str(item["file"]),
        _CAPABILITY_ORDER.get(str(item["capability"]), 99),
        str(item["symbol"]), str(item["package"]),
        str(item["reason"])))
    result = {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "mode": mode,
        "hints": hints,
        "suppressed": suppressed,
        "counts": {"hints": len(hints), "suppressed": len(suppressed),
                   "files": len(seen_files)},
        "recorded": False,
        "events_path": None,
    }
    if record and (hints or suppressed):
        events = [_event(item, status="shown", reason=None, session=session)
                  for item in hints]
        events.extend(_event(item, status="suppressed",
                             reason=str(item.get("reason") or ""),
                             session=session) for item in suppressed)
        path = record_events(root_p, events)
        result["recorded"] = True
        result["events_path"] = str(path)
    return result


def hints_for_edit(root: str | Path, rel: str, before: str | None, after: str,
                   *, session: str | None = None,
                   settings: HintSettings | None = None) -> dict[str, Any]:
    """Hints for one handled edit/write, using the configured mode and cap."""
    effective = settings or settings_for(root)
    if effective.mode == "off" or not enabled_for(rel, effective):
        return _empty_result(effective.mode)
    return detect_files(
        root, [FileChange(rel, before, after)], mode=effective.mode,
        max_per_edit=effective.max_per_edit,
        ecosystems=effective.ecosystems, session=session,
        record=effective.record)


def hints_for_paths(root: str | Path, paths: Iterable[str | Path], *,
                    mode: str | None = None,
                    before: Mapping[str, str | None] | None = None,
                    session: str | None = None,
                    settings: HintSettings | None = None,
                    record: bool | None = None,
                    max_per_edit: int | None = None) -> dict[str, Any]:
    """Hints for an explicit changed-path list (for code review/repo scan).

    With no ``before`` text, every symbol in the named files is treated as new
    or changed because the caller already selected those files. Pass ``before``
    for precise gentle-mode behaviour, or ``mode="strict"`` to inspect all
    symbols in the named files.
    """
    effective = settings or settings_for(root)
    effective_mode = _normalize_mode(mode) if mode is not None else effective.mode
    effective_mode = effective_mode or effective.mode
    if effective_mode == "off":
        return _empty_result(effective_mode)
    root_p = Path(root).resolve()
    changes: list[FileChange] = []
    for path in paths:
        rel = _safe_rel(root_p, path)
        if rel is None or language_of(rel) not in effective.ecosystems:
            continue
        after = read_source(root_p / rel)
        if after is None:
            continue
        previous = before.get(rel) if before is not None else None
        changes.append(FileChange(rel, previous, after))
    return detect_files(
        root_p, changes, mode=effective_mode,
        max_per_edit=(effective.max_per_edit if max_per_edit is None
                      else max_per_edit),
        ecosystems=effective.ecosystems, session=session,
        record=(effective.record if record is None else record))


# --------------------------------------------------------------------------- #
# event log and local feedback
# --------------------------------------------------------------------------- #
def record_events(root: str | Path, events: Iterable[Mapping[str, Any]]) -> Path:
    """Append events to the state-dir log. Metadata only; never contents."""
    path = events_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(dict(event), sort_keys=True,
                                        ensure_ascii=False) + "\n")
    except OSError as exc:
        raise LibraryHintsError(f"cannot write {path}: {exc}") from exc
    return path


def read_events(root: str | Path, *, limit: int = 5000) -> list[dict[str, Any]]:
    """The most recent events (bounded tail; metadata only)."""
    path = events_path(root)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_EVENT_BYTES))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > MAX_EVENT_BYTES and lines:
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events[-max(0, limit):]


def record_feedback(root: str | Path, hint_id: str, verdict: str, *,
                    session: str | None = None) -> dict[str, Any]:
    """Store local-only feedback for a hint id: accepted or ignored."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{4,128}", str(hint_id or "")):
        raise LibraryHintsError("hint id must be 4-128 safe characters")
    if verdict not in ("accepted", "ignored"):
        raise LibraryHintsError("verdict must be accepted or ignored")
    record = {
        "schema": SCHEMA,
        "ts": _now(),
        "session": _one_line(session or "cli", MAX_SESSION_CHARS) or "cli",
        "hint_id": hint_id,
        "verdict": verdict,
    }
    path = feedback_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        raise LibraryHintsError(f"cannot write {path}: {exc}") from exc
    return record


def explain(root: str | Path, query: str, *, limit: int = 10) -> dict[str, Any]:
    """Explain a symbol, hint id, capability or package from the local log
    and the registry. Reads metadata only."""
    text = str(query or "").strip()
    events = [event for event in read_events(root)
              if text in (str(event.get("symbol") or ""),
                          str(event.get("hint_id") or ""),
                          str(event.get("capability") or ""),
                          str(event.get("package") or ""))]
    matches: list[dict[str, Any]] = []
    lowered = text.lower()
    for name, data in _CAPABILITIES.items():
        packages = sorted({candidate.package
                           for language in data["languages"].values()
                           for candidate in
                           (_candidate(entry) for entry in language)})
        title = str(data["title"])
        if lowered in (name.lower(), title.lower()) or lowered in {
                package.lower() for package in packages}:
            matches.append({
                "capability": name,
                "title": title,
                "stdlib_note": data.get("stdlib_note"),
                "packages": packages,
            })
    return {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "query": text,
        "events": events[-max(0, limit):],
        "registry": matches,
    }
