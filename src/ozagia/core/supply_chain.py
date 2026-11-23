"""Offline supply-chain attack detection for dependency inputs.

The analyzer reads manifests, lockfiles, registry configuration and package
metadata under a checkout. It never fetches, installs, imports or executes the
target project. Online facts — maintainer accounts, registry yanks, tarball
contents, signatures, attestations and malware feeds — are explicitly *not
assessed*; absence of a finding is never a clean verdict.

The first-class rules cover the attack paths that are visible offline:

``untrusted_registry``
    A registry or index outside the allowlist, including plain HTTP.
``registry_credentials``
    Credentials or tokens embedded in registry configuration or URLs.
``unsafe_tls``
    TLS verification disabled or a trusted-host override.
``mutable_git_dependency``
    A git dependency without an immutable commit.
``direct_source_dependency``
    A URL/path dependency where a registry package is expected.
``wildcard_dependency``
    A wildcard or "latest" version with no lower/upper bound.
``missing_integrity``
    A lockfile package that should carry a hash/integrity value and does not.
``install_script``
    Dependency or project code that runs during install/build, or an unknown
    Python build backend.
``typosquat_candidate``
    A declared name one edit away from a popular package name.
``dependency_confusion_candidate``
    An internal package name resolved from a public/untrusted registry.

The policy file is explicit JSON (``--policy``); without one, conservative
built-in registry and backend allowlists apply. Every finding is a review
candidate with evidence, confidence, CWE and an explicit ``not_assessed`` list.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from ozagia.config import GENERIC_SKIP_DIRS

MAX_FILES = 25_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 32_000_000
MAX_NODE_PACKAGE_FILES = 2_000
MAX_FINDINGS = 200
MAX_LOCATIONS = 5
MAX_EXAMPLES = 3
DEFAULT_BUDGET_S = 10.0

NOT_ASSESSED = (
    "registry metadata, maintainer accounts and ownership changes",
    "package tarball contents and runtime behaviour",
    "signatures, attestations and transparency-log inclusion",
    "online advisory, malware and yank feeds",
    "dependency resolution beyond the committed lockfiles",
)

DEFAULT_REGISTRIES = {
    "node": ("https://registry.npmjs.org/",),
    "python": ("https://pypi.org/simple", "https://files.pythonhosted.org/"),
    "rust": ("https://index.crates.io/",
             "https://github.com/rust-lang/crates.io-index",
             "https://static.crates.io/"),
    "go": ("https://proxy.golang.org/", "https://sum.golang.org/"),
    "ruby": ("https://rubygems.org/",),
    "composer": ("https://repo.packagist.org/", "https://packagist.org/"),
}

DEFAULT_BUILD_BACKENDS = frozenset({
    "setuptools.build_meta",
    "setuptools.build_meta:__legacy__",
    "hatchling.build",
    "flit_core.buildapi",
    "poetry.core.masonry.api",
    "pdm.backend",
    "pdm.pep517.api",
    "maturin",
    "scikit_build_core.build",
    "mesonpy",
})

_LIFECYCLE = frozenset({"preinstall", "install", "postinstall", "prepare"})
_CONFIG_NAMES = frozenset({".npmrc", ".yarnrc", ".yarnrc.yml", "pip.conf",
                           "pip.ini", ".pypirc"})
_LOCK_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock",
    "Gemfile.lock", "composer.lock", "go.sum", "gradle.lockfile",
})
_MANIFEST_NAMES = frozenset({"pyproject.toml", "requirements.txt", "package.json",
                             "Cargo.toml", "go.mod", "Gemfile", "composer.json"})
_BUILD_NAMES = frozenset({"setup.py", "setup.cfg", "build.rs"})
_SKIP_DIRS = frozenset(GENERIC_SKIP_DIRS - {"node_modules"})
_GIT_URL = re.compile(r"(?:git\+|git@|github:|git://)(\S+)", re.I)
_SEVERITIES = ("critical", "high", "medium", "low", "note")


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    confidence: str
    cwe: tuple[str, ...]
    recommendation: str


RULES: dict[str, Rule] = {
    item.id: item for item in (
        Rule("untrusted_registry", "Untrusted package registry", "medium", "high",
             ("cwe:494", "cwe:829"),
             "Pin the dependency source to an allowlisted HTTPS registry, or add "
             "the intended internal registry to the supply-chain policy."),
        Rule("registry_credentials", "Credentials in registry configuration",
             "high", "high", ("cwe:798", "cwe:522"),
             "Remove the credential from the repository and rotate it; use an "
             "environment or secret store outside the checkout."),
        Rule("unsafe_tls", "Registry TLS verification disabled", "high", "high",
             ("cwe:295", "cwe:494"),
             "Re-enable TLS verification and install the issuing CA instead of "
             "trusting an arbitrary host."),
        Rule("mutable_git_dependency", "Mutable git dependency", "medium", "high",
             ("cwe:494", "cwe:829"),
             "Pin the dependency to a full commit hash, or vendor the code and "
             "review updates explicitly."),
        Rule("direct_source_dependency", "Dependency from a direct source",
             "medium", "medium", ("cwe:829",),
             "Use a registry release with integrity metadata, or allowlist and "
             "review the path/URL source in the supply-chain policy."),
        Rule("wildcard_dependency", "Wildcard dependency version", "medium", "high",
             ("cwe:1104",),
             "Pin a reviewed version range with an upper bound, and commit the "
             "lockfile that selects the exact version."),
        Rule("missing_integrity", "Lockfile entries without integrity metadata",
             "medium", "high", ("cwe:494", "cwe:345"),
             "Regenerate the lockfile with integrity/hash metadata and verify "
             "the package before installing."),
        Rule("install_script", "Install/build-time code execution", "high", "medium",
             ("cwe:829", "cwe:506"),
             "Review the script or build hook, allowlist only known packages, "
             "and run installs with scripts disabled where possible."),
        Rule("typosquat_candidate", "Possible typosquatted dependency", "medium",
             "low", ("cwe:829",),
             "Confirm the package name with the maintainer or registry listing; "
             "the name is one edit or transposition away from a popular package."),
        Rule("dependency_confusion_candidate", "Possible dependency confusion",
             "high", "medium", ("cwe:829", "cwe:1357"),
             "Resolve internal package names from the private registry, or scope "
             "them to an owned namespace; do not fall back to a public registry."),
    )
}

_POPULAR = {
    "node": frozenset({
        "lodash", "react", "react-dom", "express", "axios", "chalk", "debug",
        "moment", "request", "commander", "typescript", "webpack", "babel-core",
        "event-stream", "ua-parser-js", "colors", "faker", "node-fetch", "jest",
        "mocha", "eslint", "prettier", "next", "vue", "jquery", "underscore",
    }),
    "python": frozenset({
        "requests", "urllib3", "setuptools", "numpy", "pandas", "flask",
        "django", "boto3", "pyyaml", "cryptography", "pytest", "pillow", "six",
        "attrs", "jinja2", "sqlalchemy", "pydantic", "fastapi", "click",
        "aiohttp", "httpx", "beautifulsoup4", "scipy", "matplotlib",
    }),
    "rust": frozenset({
        "serde", "tokio", "rand", "log", "regex", "clap", "anyhow", "thiserror",
        "reqwest", "hyper", "chrono", "uuid", "bytes", "futures", "syn",
        "quote", "proc-macro2", "tracing", "indexmap", "hashbrown",
    }),
    "ruby": frozenset({
        "rails", "rack", "rake", "bundler", "json", "nokogiri", "rake",
        "rspec", "puma", "sidekiq", "devise", "activeadmin", "bootsnap",
    }),
    "composer": frozenset({
        "symfony/console", "symfony/http-foundation", "monolog/monolog",
        "guzzlehttp/guzzle", "laravel/framework", "doctrine/orm",
        "phpunit/phpunit", "psr/log", "composer/semver",
    }),
}


def _valid_severity(value: str) -> bool:
    return value in _SEVERITIES


def load_policy(source: str | Path | Mapping[str, Any] | None = None,
                *, root: Path | None = None) -> dict[str, Any]:
    """Load and normalize a supply-chain policy.

    ``source`` is None (defaults), a mapping, or a JSON file path. Unknown
    keys, wrong types, invalid severities and non-HTTP(S) registries raise
    ``ValueError``; the caller reports that instead of guessing.
    """
    raw: dict[str, Any] = {}
    if source is not None and not isinstance(source, Mapping):
        path = Path(source)
        if not path.is_absolute() and root is not None:
            path = root / path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read supply-chain policy {path}: {exc}") from exc
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("supply-chain policy exceeds the size limit")
        try:
            raw = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"supply-chain policy is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("supply-chain policy must be a JSON object")
    elif isinstance(source, Mapping):
        raw = dict(source)

    allowed = {"registries", "internal_packages", "allow_git_hosts",
               "allow_install_scripts", "allow_build_backends",
               "allow_direct_sources", "require_integrity", "disabled_rules",
               "severity_overrides"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown supply-chain policy keys: {', '.join(unknown)}")

    def strings(key: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str)
                                                 for item in value):
            raise ValueError(f"{key} must be a list of strings")
        return [item.strip() for item in value if item.strip()]

    registries = strings("registries")
    if not registries:
        registries = sorted({url for urls in DEFAULT_REGISTRIES.values()
                             for url in urls})
    for url in registries:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"registry must be an http(s) URL: {url!r}")
    internal = strings("internal_packages")
    git_hosts = [host.lower().lstrip(".") for host in strings("allow_git_hosts")]
    install_scripts = strings("allow_install_scripts")
    backends = sorted(DEFAULT_BUILD_BACKENDS | set(strings("allow_build_backends")))
    direct_sources = strings("allow_direct_sources")
    require_integrity = raw.get("require_integrity", False)
    if not isinstance(require_integrity, bool):
        raise ValueError("require_integrity must be a boolean")
    disabled = set(strings("disabled_rules"))
    unknown_rules = sorted(disabled - set(RULES))
    if unknown_rules:
        raise ValueError(f"unknown disabled supply-chain rules: "
                         f"{', '.join(unknown_rules)}")
    overrides = raw.get("severity_overrides", {})
    if not isinstance(overrides, Mapping):
        raise ValueError("severity_overrides must be an object")
    normalized_overrides: dict[str, str] = {}
    for rule, severity in overrides.items():
        if rule not in RULES:
            raise ValueError(f"unknown severity override rule: {rule!r}")
        if not isinstance(severity, str) or not _valid_severity(severity):
            raise ValueError(f"invalid severity for {rule!r}")
        normalized_overrides[rule] = severity
    policy = {
        "registries": tuple(registries),
        "internal_packages": tuple(internal),
        "allow_git_hosts": tuple(git_hosts),
        "allow_install_scripts": tuple(install_scripts),
        "allow_build_backends": tuple(backends),
        "allow_direct_sources": tuple(direct_sources),
        "require_integrity": require_integrity,
        "disabled_rules": tuple(sorted(disabled)),
        "severity_overrides": dict(sorted(normalized_overrides.items())),
    }
    return policy


def policy_hash(policy: Mapping[str, Any]) -> str:
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iter_sources(root: Path, deadline: float) -> dict[str, Any]:
    """Read the bounded set of files the supply-chain checks need."""
    sources: dict[str, str] = {}
    node_files = 0
    read_files = 0
    read_bytes = 0
    truncated = False
    omissions: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() >= deadline:
            omissions.append("time budget exhausted")
            truncated = True
            break
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _SKIP_DIRS and not name.startswith(".git"))
        in_node_modules = "node_modules" in Path(dirpath).parts
        for name in sorted(filenames):
            if time.monotonic() >= deadline:
                omissions.append("time budget exhausted")
                truncated = True
                break
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            take = (name in _CONFIG_NAMES or name in _LOCK_NAMES
                    or name in _MANIFEST_NAMES
                    or name.startswith("requirements") and name.endswith(".txt")
                    or name in _BUILD_NAMES
                    or (in_node_modules and name == "package.json"))
            if not take:
                continue
            if in_node_modules and name == "package.json":
                node_files += 1
                if node_files > MAX_NODE_PACKAGE_FILES:
                    truncated = True
                    omissions.append("node_modules package metadata limit reached")
                    continue
            try:
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    omissions.append(
                        f"{path.relative_to(root).as_posix()}: file exceeds the size limit")
                    continue
                if read_bytes + size > MAX_TOTAL_BYTES:
                    truncated = True
                    omissions.append("total read size limit reached")
                    break
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            sources[rel] = text
            read_files += 1
            read_bytes += len(text.encode("utf-8"))
            if read_files >= MAX_FILES:
                truncated = True
                omissions.append("file-count limit reached")
                break
        if truncated:
            break
    return {"sources": sources, "files_read": read_files,
            "bytes_read": read_bytes, "truncated": truncated,
            "omissions": omissions}


def _source_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _url_parts(url: str) -> tuple[str, str, str, bool]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    path = parsed.path or "/"
    credentials = bool(parsed.username or parsed.password)
    return scheme, host, path, credentials


def _registry_allowed(url: str, policy: Mapping[str, Any]) -> bool:
    scheme, host, path, _ = _url_parts(url)
    if scheme not in ("http", "https") or not host:
        return False
    for allowed in policy["registries"]:
        a_scheme, a_host, a_path, _ = _url_parts(str(allowed))
        if scheme != a_scheme or host != a_host:
            continue
        if not a_path or a_path == "/" or path.startswith(a_path):
            return True
    return False


def _find_credentials(text: str) -> bool:
    return bool(re.search(r"(?i)(?:^|[?&;:=\s])(?:token|password|passwd|secret|"
                          r"_authToken|_auth|api[_-]?key)\b", text))


def _levenshtein(left: str, right: str, cutoff: int = 2) -> int:
    if abs(len(left) - len(right)) > cutoff:
        return cutoff + 1
    previous = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        current = [i]
        for j, other in enumerate(right, 1):
            current.append(min(previous[j] + 1,
                               current[j - 1] + 1,
                               previous[j - 1] + (char != other)))
        previous = current
    return previous[-1]


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _internal_name(name: str, policy: Mapping[str, Any]) -> bool:
    normalized = _normalized(name)
    return any(fnmatch.fnmatchcase(name, pattern)
               or fnmatch.fnmatchcase(normalized, pattern)
               for pattern in policy["internal_packages"])


def _direct_source_allowed(value: str, policy: Mapping[str, Any]) -> bool:
    normalized = str(value)
    return any(fnmatch.fnmatchcase(normalized, pattern)
               for pattern in policy["allow_direct_sources"])


def _install_script_allowed(name: str, policy: Mapping[str, Any]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern)
               or fnmatch.fnmatchcase(_normalized(name), pattern)
               for pattern in policy["allow_install_scripts"])


def _add(ctx: "_Context", rule: str, key: str, *,
         file: str | None = None, line: int | None = None,
         summary: str, evidence: Mapping[str, Any] | None = None,
         extra: Mapping[str, Any] | None = None,
         severity: str | None = None,
         confidence: str | None = None) -> None:
    spec = RULES[rule]
    if rule in ctx.policy["disabled_rules"]:
        return
    chosen = severity or ctx.policy["severity_overrides"].get(rule) or spec.severity
    if not _valid_severity(chosen):
        chosen = spec.severity
    identity = (rule, key)
    current = ctx.findings.get(identity)
    if current is not None:
        current["extra"]["occurrences"] = current["extra"].get("occurrences", 1) + 1
        if file and len(current["locations"]) < MAX_LOCATIONS:
            location = {"path": file}
            if line:
                location["line"] = line
            if location not in current["locations"]:
                current["locations"].append(location)
        return
    if len(ctx.findings) >= ctx.max_findings:
        ctx.omissions.append("finding limit reached")
        return
    location = {"path": file} if file else {}
    if line and location:
        location["line"] = line
    finding = {
        "section": "dependencies",
        "type": rule,
        "severity": chosen,
        "confidence": confidence or spec.confidence,
        "evidence_kind": "declared",
        "keys": [key, *([f"file:{file}"] if file else [])],
        "locations": [location] if location else [],
        "file": file,
        "summary": summary,
        "recommendation": spec.recommendation,
        "cwe": list(spec.cwe),
        "not_assessed": list(NOT_ASSESSED),
        "extra": {
            "rule": rule,
            "title": spec.title,
            **(dict(extra or {})),
        },
    }
    ctx.findings[identity] = finding


class _Context:
    def __init__(self, root: Path, sources: Mapping[str, str],
                 inventory: Mapping[str, Any], policy: Mapping[str, Any],
                 max_findings: int) -> None:
        self.root = root
        self.sources = sources
        self.inventory = inventory
        self.policy = policy
        self.max_findings = max_findings
        self.findings: dict[tuple[str, str], dict[str, Any]] = {}
        self.omissions: list[str] = []
        self.install_scripts: list[dict[str, Any]] = []
        self.packages = 0
        self.packages_with_integrity = 0
        self.packages_without_integrity = 0
        self.public_locks: set[tuple[str, str]] = set()
        self.git_sources: set[str] = set()


# --------------------------------------------------------------------------- #
# configuration files
# --------------------------------------------------------------------------- #

def _check_url_config(ctx: _Context, rel: str, line: int, url: str,
                      ecosystem: str, *, label: str) -> None:
    if not url or url.startswith("@") or url.startswith("$"):
        return
    scheme, host, _, credentials = _url_parts(url)
    if not host:
        return
    source = _source_hash(url)
    if credentials:
        _add(ctx, "registry_credentials", f"registry:{source}:credentials",
             file=rel, line=line,
             summary=f"{label} embeds credentials.",
             evidence={"source": source, "kind": "registry-config"})
    if scheme == "http":
        _add(ctx, "unsafe_tls", f"registry:{source}:http", file=rel, line=line,
             summary=f"{label} uses plain HTTP.",
             evidence={"source": source, "scheme": scheme})
    if not _registry_allowed(url, ctx.policy):
        _add(ctx, "untrusted_registry", f"registry:{source}",
             file=rel, line=line,
             summary=f"{label} points outside the registry allowlist.",
             evidence={"source": source, "kind": "registry-config"})


def _check_npmrc(ctx: _Context, rel: str, text: str) -> None:
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        match = re.match(r"^([^=\s]+)\s*=\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        lowered = key.lower()
        if lowered.endswith("registry") or lowered == "registry":
            _check_url_config(ctx, rel, number, value, "node",
                              label=f"npm registry setting {key}")
        if lowered in ("strict-ssl", "strict_ssl") and value.lower() in (
                "false", "0", "no"):
            _add(ctx, "unsafe_tls", f"config:{rel}:strict-ssl",
                 file=rel, line=number,
                 summary="npm TLS verification is disabled.",
                 evidence={"setting": key, "value": value})
        if ("_authtoken" in lowered or "_auth" in lowered
                or _find_credentials(line)):
            _add(ctx, "registry_credentials", f"config:{rel}:token",
                 file=rel, line=number,
                 summary="npm configuration contains an authentication token.",
                 evidence={"setting": key, "value": "<redacted>"})


def _check_ini_like(ctx: _Context, rel: str, text: str) -> None:
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        match = re.match(r"^([^=:\s]+)\s*[=:]\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key in ("index-url", "extra-index-url"):
            for url in re.split(r"[\s,]+", value):
                _check_url_config(ctx, rel, number, url, "python",
                                  label=f"pip {key}")
        elif key == "trusted-host":
            _add(ctx, "unsafe_tls", f"config:{rel}:trusted-host",
                 file=rel, line=number,
                 summary="pip trusts a host without normal TLS validation.",
                 evidence={"setting": key, "source": _source_hash(value)})
        elif _find_credentials(line):
            _add(ctx, "registry_credentials", f"config:{rel}:credentials",
                 file=rel, line=number,
                 summary="pip configuration contains a credential or token.",
                 evidence={"setting": key, "value": "<redacted>"})


def _check_requirements(ctx: _Context, rel: str, text: str) -> None:
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        option = re.match(
            r"^--(index-url|extra-index-url|trusted-host)(?:\s+|=)(\S+)",
            line, re.I)
        if option:
            name, value = option.group(1).lower(), option.group(2)
            if name == "trusted-host":
                _add(ctx, "unsafe_tls", f"config:{rel}:trusted-host",
                     file=rel, line=number,
                     summary="requirements trust a host without normal TLS "
                             "validation.",
                     evidence={"setting": name, "source": _source_hash(value)})
            else:
                _check_url_config(ctx, rel, number, value, "python",
                                  label=f"pip {name}")
            continue
        if line.lower().startswith(("-r ", "--requirement", "-c ",
                                    "--constraint", "--find-links")):
            continue
        _check_requirement(ctx, rel, number, line, "python")


def _check_pyproject(ctx: _Context, rel: str, text: str) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    build_system = data.get("build-system") or {}
    backend = build_system.get("build-backend")
    if isinstance(backend, str):
        if backend not in ctx.policy["allow_build_backends"]:
            _add(ctx, "install_script", f"python:backend:{backend}",
                 file=rel, line=1,
                 summary=f"Python build backend {backend!r} is not in the "
                         "allowlist and runs during install/build.",
                 evidence={"build_backend": backend})
        if build_system.get("backend-path"):
            _add(ctx, "install_script", f"python:backend-path:{rel}",
                 file=rel, line=1,
                 summary="The Python build backend is loaded from a local "
                         "path, so project code runs at build time.",
                 evidence={"backend_path": build_system.get("backend-path")})
    tool = data.get("tool") or {}
    uv = tool.get("uv") or {}
    if isinstance(uv, Mapping):
        for key in ("index-url", "extra-index-url"):
            value = uv.get(key)
            if isinstance(value, str):
                for url in re.split(r"[\s,]+", value):
                    _check_url_config(ctx, rel, 1, url, "python",
                                      label=f"uv {key}")
        for host in uv.get("allow-insecure-host") or []:
            if isinstance(host, str):
                _add(ctx, "unsafe_tls", f"config:{rel}:insecure-host",
                     file=rel, line=1,
                     summary="uv allows an insecure registry host.",
                     evidence={"setting": "allow-insecure-host",
                               "source": _source_hash(host)})
        for index in uv.get("index") or []:
            if isinstance(index, Mapping) and isinstance(index.get("url"), str):
                _check_url_config(ctx, rel, 1, index["url"], "python",
                                  label=f"uv index {index.get('name') or ''}".strip())
    poetry = tool.get("poetry") or {}
    if isinstance(poetry, Mapping):
        for source in poetry.get("source") or []:
            if isinstance(source, Mapping) and isinstance(source.get("url"), str):
                _check_url_config(ctx, rel, 1, source["url"], "python",
                                  label=f"Poetry source {source.get('name') or ''}".strip())


def _check_cargo_config(ctx: _Context, rel: str, text: str) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    sources = data.get("source") or {}
    if isinstance(sources, Mapping):
        for name, value in sources.items():
            if not isinstance(value, Mapping):
                continue
            if value.get("replace-with"):
                _add(ctx, "untrusted_registry", f"cargo:source:{name}",
                     file=rel, line=1,
                     summary=f"Cargo source {name!r} is replaced with "
                             f"{value['replace-with']!r}.",
                     evidence={"source": name,
                               "replace_with": value["replace-with"]})
            if isinstance(value.get("registry"), str):
                _check_url_config(ctx, rel, 1, value["registry"], "rust",
                                  label=f"Cargo source {name}")
    registries = data.get("registries") or {}
    if isinstance(registries, Mapping):
        for name, value in registries.items():
            if isinstance(value, Mapping) and isinstance(value.get("index"), str):
                _check_url_config(ctx, rel, 1, value["index"], "rust",
                                  label=f"Cargo registry {name}")


def _check_composer(ctx: _Context, rel: str, text: str) -> None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return
    if not isinstance(data, Mapping):
        return
    for repository in data.get("repositories") or []:
        if isinstance(repository, str):
            if repository.lower() == "packagist.org":
                continue
            _add(ctx, "untrusted_registry",
                 f"composer:repository:{_source_hash(repository)}",
                 file=rel, line=1,
                 summary="Composer replaces or disables the default Packagist "
                         "repository.",
                 evidence={"source": _source_hash(repository)})
        elif isinstance(repository, Mapping):
            url = repository.get("url")
            if isinstance(url, str):
                _check_url_config(ctx, rel, 1, url, "composer",
                                  label=f"Composer repository {repository.get('type') or ''}".strip())
    config = data.get("config") or {}
    if isinstance(config, Mapping) and config.get("secure-http") is False:
        _add(ctx, "unsafe_tls", f"composer:secure-http:{rel}",
             file=rel, line=1,
             summary="Composer secure-http is disabled.",
             evidence={"setting": "secure-http", "value": False})
    scripts = data.get("scripts") or {}
    if isinstance(scripts, Mapping):
        lifecycle = sorted(name for name in scripts
                           if name.lower() in {
                               "pre-install-cmd", "post-install-cmd",
                               "pre-package-install", "post-package-install",
                               "post-autoload-dump", "post-create-project-cmd"})
        if lifecycle:
            _add(ctx, "install_script", f"composer:scripts:{rel}",
                 file=rel, line=1,
                 summary="Composer install lifecycle scripts execute project "
                         "commands: " + ", ".join(lifecycle) + ".",
                 evidence={"scripts": lifecycle})
    allow_plugins = config.get("allow-plugins") if isinstance(config, Mapping) else None
    if allow_plugins is True or (isinstance(allow_plugins, Mapping)
                                 and any(value is True
                                         for value in allow_plugins.values())):
        _add(ctx, "install_script", f"composer:plugins:{rel}",
             file=rel, line=1,
             summary="Composer plugins are allowed to execute at install time.",
             evidence={"allow_plugins": allow_plugins})


def _check_gemfile(ctx: _Context, rel: str, text: str) -> None:
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source = re.match(r'^source\s+["\']([^"\']+)["\']', line)
        if source:
            url = source.group(1)
            if url.startswith("http"):
                _check_url_config(ctx, rel, number, url, "ruby",
                                  label="Bundler source")
        git = re.search(r'\bgit:\s*["\']([^"\']+)["\']', line)
        if git:
            _check_git(ctx, rel, number, line, git.group(1), "ruby")
        if re.search(r"\bpath:\s*[\"']", line):
            _check_direct(ctx, rel, number, line, "ruby", line)


def _check_go_mod(ctx: _Context, rel: str, text: str) -> None:
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("replace "):
            target = line.split("=>", 1)[-1].strip()
            _check_direct(ctx, rel, number, line, "go", target)
        elif line.startswith("exclude "):
            continue


def _check_git(ctx: _Context, rel: str, line: int, raw: str, url: str,
               ecosystem: str) -> None:
    url = re.sub(r"^git\+", "", url)
    name = _git_name(raw, url)
    if url.startswith("git@"):
        url = "ssh://" + url.replace(":", "/", 1)
    _, host, _, _ = _url_parts(url)
    allowed_hosts = ctx.policy["allow_git_hosts"]
    source = _source_hash(url)
    if allowed_hosts and host and not any(
            host == allowed or host.endswith("." + allowed)
            for allowed in allowed_hosts):
        _add(ctx, "untrusted_registry", f"git:{source}",
             file=rel, line=line,
             summary="Git dependency uses a host outside the git allowlist.",
             evidence={"source": source})
    if _git_is_mutable(raw, url):
        _add(ctx, "mutable_git_dependency", f"dependency:{ecosystem}:{name}",
             file=rel, line=line,
             summary=f"Git dependency {name or 'unnamed'} is not pinned to a "
                     "full commit.",
             evidence={"source": source})


def _git_name(raw: str, url: str) -> str:
    match = re.search(r"#egg=([A-Za-z0-9_.-]+)", raw)
    if match:
        return match.group(1)
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _git_is_mutable(raw: str, url: str) -> bool:
    if re.search(r"[@#]([0-9a-f]{40})\b", url, re.I):
        return False
    if re.search(r"\b(?:branch|tag|rev)\s*[:=]\s*[\"']?[\w./-]+", raw, re.I):
        # An explicit `rev` that is a full commit is immutable; branch/tag are not.
        if re.search(r"\brev\s*[:=]\s*[\"']?[0-9a-f]{40}\b", raw, re.I):
            return False
        return True
    return bool(re.search(r"@(?:main|master|develop|dev|latest|HEAD)\b", url, re.I)
                or "@" not in url.rsplit("/", 1)[-1])


def _check_direct(ctx: _Context, rel: str, line: int, raw: str,
                  ecosystem: str, target: str) -> None:
    name = _direct_name(raw, target)
    if _direct_source_allowed(target, ctx.policy):
        return
    _add(ctx, "direct_source_dependency", f"dependency:{ecosystem}:{name}",
         file=rel, line=line,
         summary=f"Dependency {name or 'unnamed'} comes from a direct URL or "
                 "path, bypassing registry integrity metadata.",
         evidence={"source": _source_hash(target)})


def _direct_name(raw: str, target: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(?:@|==|$)", raw)
    if match:
        return match.group(1)
    return target.rstrip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# manifest specifications
# --------------------------------------------------------------------------- #

def _requirement_spec(value: str) -> tuple[str, str]:
    text = str(value).strip()
    if not text:
        return "", ""
    if text.startswith(("git+", "git@", "github:", "http://", "https://",
                        "file:", "./", "../", "/")):
        return "", text
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$", text)
    if not match:
        return "", text
    return match.group(1), match.group(2).strip()


def _check_dep_spec(ctx: _Context, rel: str, line: int, name: str, spec: str,
                    ecosystem: str, *, allow_direct: bool = False) -> None:
    # PEP 508 direct references use `name @ url`; npm/Cargo do not use the `@`.
    spec = spec.strip()
    if spec.startswith("@"):
        spec = spec[1:].strip()
    if not name:
        if spec and not allow_direct:
            if spec.startswith(("git+", "git@", "github:")):
                _check_git(ctx, rel, line, spec, spec, ecosystem)
                return
            if spec.startswith(("http://", "https://", "file:", "./", "../", "/")):
                _check_direct(ctx, rel, line, spec, ecosystem, spec)
        return
    if _is_wildcard(spec):
        _add(ctx, "wildcard_dependency", f"dependency:{ecosystem}:{name}",
             file=rel, line=line,
             summary=f"Dependency {name} has a wildcard/latest version spec "
                     f"{spec!r}.",
             evidence={"spec": spec})
    if "git+" in spec.lower() or spec.lower().startswith(("git@", "github:")):
        _check_git(ctx, rel, line, f"{name} {spec}", spec, ecosystem)
    elif re.search(r"(?:https?://|file:|(?:^|\s)(?:\.\.?/|/))",
                   spec, re.I) and not allow_direct:
        _check_direct(ctx, rel, line, f"{name} {spec}", ecosystem, spec)


def _check_requirement(ctx: _Context, rel: str, line: int, value: str,
                       ecosystem: str, *, allow_direct: bool = False) -> None:
    name, spec = _requirement_spec(value)
    _check_dep_spec(ctx, rel, line, name, spec, ecosystem,
                    allow_direct=allow_direct)


def _is_wildcard(spec: str) -> bool:
    value = spec.strip().lower()
    # A bare name is not flagged here: the lockfile is the pinning evidence.
    return value in ("*", "latest", "x", ">=0", ">=0.0", ">=0.0.0")


def _manifest_specs(rel: str, text: str) -> list[tuple[str, str, int]]:
    """(name, spec, line) for manifest declarations we can parse cheaply."""
    name = Path(rel).name
    rows: list[tuple[str, str, int]] = []
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project") or {}
            groups = [project.get("dependencies") or []]
            groups += list((project.get("optional-dependencies") or {}).values())
            groups += list((data.get("dependency-groups") or {}).values())
            for group in groups:
                for value in group or []:
                    dep, spec = _requirement_spec(str(value))
                    if dep:
                        rows.append((dep, spec, 1))
            poetry = (data.get("tool") or {}).get("poetry") or {}
            for dep_name, spec in (poetry.get("dependencies") or {}).items():
                if dep_name == "python":
                    continue
                value = spec.get("version", "") if isinstance(spec, Mapping) else spec
                rows.append((dep_name, str(value), 1))
            for group in (poetry.get("group") or {}).values():
                if isinstance(group, Mapping):
                    for dep_name, spec in (group.get("dependencies") or {}).items():
                        value = spec.get("version", "") if isinstance(spec, Mapping) else spec
                        rows.append((dep_name, str(value), 1))
        elif name == "package.json":
            data = json.loads(text)
            for field in ("dependencies", "devDependencies", "optionalDependencies",
                          "peerDependencies"):
                section = data.get(field)
                if isinstance(section, Mapping):
                    for dep_name, spec in section.items():
                        if isinstance(spec, str):
                            rows.append((dep_name, spec, 1))
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            sections = [data.get(field) or {} for field in
                        ("dependencies", "dev-dependencies", "build-dependencies")]
            sections.append((data.get("workspace") or {}).get("dependencies") or {})
            for section in sections:
                if not isinstance(section, Mapping):
                    continue
                for dep_name, spec in section.items():
                    if isinstance(spec, Mapping):
                        if spec.get("git"):
                            rows.append((dep_name, "git+" + str(spec.get("git")), 1))
                        elif spec.get("path"):
                            rows.append((dep_name, str(spec.get("path")), 1))
                        else:
                            rows.append((dep_name, str(spec.get("version") or ""), 1))
                    elif isinstance(spec, str):
                        rows.append((dep_name, spec, 1))
        elif name.startswith("requirements") and name.endswith(".txt"):
            for number, raw in enumerate(text.splitlines(), 1):
                line = raw.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                dep, spec = _requirement_spec(line)
                if dep:
                    rows.append((dep, spec, number))
        elif name == "go.mod":
            for number, raw in enumerate(text.splitlines(), 1):
                match = re.match(r"^\s*(?:require\s+)?([a-z0-9][\w.\-]*\.[a-z]{2,}"
                                 r"/[\w.\-/]+)\s+(v[\w.\-+]+)", raw)
                if match:
                    rows.append((match.group(1), match.group(2), number))
        elif name == "Gemfile":
            for number, raw in enumerate(text.splitlines(), 1):
                match = re.match(r'^\s*gem\s+["\']([^"\']+)["\'](?:\s*,\s*["\']([^"\']+)["\'])?',
                                 raw)
                if match:
                    rows.append((match.group(1), match.group(2) or "", number))
        elif name == "composer.json":
            data = json.loads(text)
            for field in ("require", "require-dev"):
                section = data.get(field)
                if isinstance(section, Mapping):
                    for dep_name, spec in section.items():
                        if dep_name == "php" or not isinstance(spec, str):
                            continue
                        rows.append((dep_name, spec, 1))
    except (ValueError, TypeError, KeyError, tomllib.TOMLDecodeError):
        return []
    return rows


# --------------------------------------------------------------------------- #
# lockfile checks
# --------------------------------------------------------------------------- #

def _package_rows(rel: str, text: str) -> dict[str, Any] | None:
    name = Path(rel).name
    try:
        if name in ("package-lock.json", "npm-shrinkwrap.json"):
            doc = json.loads(text)
            rows: list[dict[str, Any]] = []
            packages = doc.get("packages")
            if isinstance(packages, Mapping) and packages:
                for path, value in packages.items():
                    if not path or not isinstance(value, Mapping):
                        continue
                    dep_name = str(value.get("name") or path.rsplit("node_modules/", 1)[-1])
                    rows.append(_npm_row(dep_name, value))
            else:
                pending = [doc.get("dependencies") or {}]
                while pending:
                    section = pending.pop()
                    if not isinstance(section, Mapping):
                        continue
                    for dep_name, value in section.items():
                        if not isinstance(value, Mapping):
                            continue
                        rows.append(_npm_row(str(dep_name), value))
                        nested = value.get("dependencies")
                        if isinstance(nested, Mapping):
                            pending.append(nested)
            return {"ecosystem": "node", "format": "npm", "rows": rows}
        if name == "yarn.lock":
            return {"ecosystem": "node", "format": "yarn", "rows": _yarn_rows(text)}
        if name == "pnpm-lock.yaml":
            return {"ecosystem": "node", "format": "pnpm", "rows": _pnpm_rows(text)}
        if name == "Cargo.lock":
            doc = tomllib.loads(text)
            rows = []
            for package in doc.get("package") or []:
                if not isinstance(package, Mapping) or not isinstance(
                        package.get("name"), str):
                    continue
                source = str(package.get("source") or "")
                row = {"name": package["name"],
                       "version": str(package.get("version") or ""),
                       "source": "local",
                       "integrity": bool(package.get("checksum")),
                       "registry": ""}
                if source.startswith(("registry+", "sparse+")):
                    row["source"] = "registry"
                    row["registry"] = source.split("+", 1)[1]
                elif source.startswith("git+"):
                    row["source"] = "git"
                    row["git"] = source[4:]
                elif source:
                    row["source"] = "registry"
                    row["registry"] = source
                rows.append(row)
            return {"ecosystem": "rust", "format": "cargo", "rows": rows}
        if name in ("uv.lock", "poetry.lock", "pdm.lock"):
            doc = tomllib.loads(text)
            rows = []
            for package in doc.get("package") or []:
                if not isinstance(package, Mapping) or not isinstance(
                        package.get("name"), str):
                    continue
                source = package.get("source") if isinstance(package.get("source"),
                                                             Mapping) else {}
                if source.get("git"):
                    rows.append({"name": package["name"],
                                 "version": str(package.get("version") or ""),
                                 "source": "git", "git": str(source["git"]),
                                 "integrity": False, "registry": ""})
                    continue
                if source.get("editable") or source.get("directory") or source.get("path"):
                    continue
                if source.get("url"):
                    rows.append({"name": package["name"],
                                 "version": str(package.get("version") or ""),
                                 "source": "url",
                                 "integrity": bool(package.get("hash")),
                                 "registry": "", "git": ""})
                    continue
                registry = str(source.get("registry") or "")
                if name == "uv.lock" and not source:
                    # A uv package without a source is a workspace/local entry.
                    continue
                hashes = bool(package.get("hash"))
                if isinstance(package.get("files"), list):
                    hashes = hashes or any(
                        isinstance(item, Mapping) and item.get("hash")
                        for item in package["files"])
                for key in ("sdist", "wheel", "wheels"):
                    value = package.get(key)
                    if isinstance(value, Mapping):
                        hashes = hashes or bool(value.get("hash"))
                    elif isinstance(value, list):
                        hashes = hashes or any(
                            isinstance(item, Mapping) and item.get("hash")
                            for item in value)
                rows.append({"name": package["name"],
                             "version": str(package.get("version") or ""),
                             "source": "registry", "integrity": hashes,
                             "registry": registry})
            return {"ecosystem": "python", "format": name, "rows": rows}
        if name == "Pipfile.lock":
            doc = json.loads(text)
            rows = []
            for group in ("default", "develop"):
                section = doc.get(group)
                if not isinstance(section, Mapping):
                    continue
                for dep_name, value in section.items():
                    if not isinstance(value, Mapping):
                        continue
                    if value.get("git") or value.get("path") or value.get("file"):
                        rows.append({"name": dep_name,
                                     "version": str(value.get("version") or ""),
                                     "source": "git" if value.get("git") else "path",
                                     "git": str(value.get("git") or ""),
                                     "integrity": False, "registry": ""})
                        continue
                    rows.append({"name": dep_name,
                                 "version": str(value.get("version") or ""),
                                 "source": "registry",
                                 "integrity": bool(value.get("hashes")),
                                 "registry": ""})
            return {"ecosystem": "python", "format": "pipenv", "rows": rows}
        if name == "composer.lock":
            doc = json.loads(text)
            rows = []
            for group in ("packages", "packages-dev"):
                for package in doc.get(group) or []:
                    if not isinstance(package, Mapping) or not isinstance(
                            package.get("name"), str):
                        continue
                    dist = package.get("dist") if isinstance(package.get("dist"),
                                                              Mapping) else {}
                    source = package.get("source") if isinstance(package.get("source"),
                                                                  Mapping) else {}
                    url = str(dist.get("url") or source.get("url") or "")
                    if dist.get("type") == "path":
                        source_kind = "path"
                    elif source.get("type") == "git":
                        source_kind = "git"
                    elif dist.get("type") == "url" or dist.get("type") == "zip":
                        source_kind = "url"
                    else:
                        source_kind = "registry"
                    reference = str(source.get("reference") or "")
                    pinned = bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", reference))
                    rows.append({"name": package["name"],
                                 "version": str(package.get("version") or ""),
                                 "source": source_kind,
                                 "integrity": bool(dist.get("shasum") or pinned),
                                 "registry": url if source_kind == "registry" else "",
                                 "git": url if source_kind == "git" else "",
                                 "pinned": pinned,
                                 "reference": reference})
            return {"ecosystem": "composer", "format": "composer", "rows": rows}
        if name == "Gemfile.lock":
            return {"ecosystem": "ruby", "format": "bundler",
                    "rows": _gem_rows(text)}
    except (ValueError, TypeError, KeyError, tomllib.TOMLDecodeError):
        return None
    return None


def _npm_row(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    resolved = str(value.get("resolved") or "")
    source = "path" if value.get("link") else "registry"
    git = ""
    registry = resolved
    if resolved.startswith(("git+", "git@")):
        source, git, registry = "git", resolved, ""
    elif resolved.startswith(("file:", "../", "./", "/")):
        source, registry = "path", ""
    elif resolved and not resolved.startswith(("http://", "https://")):
        source, registry = "url", ""
    return {"name": name,
            "version": str(value.get("version") or ""),
            "source": source,
            "integrity": bool(value.get("integrity")),
            "registry": registry,
            "git": git}


def _yarn_rows(text: str) -> list[dict[str, Any]]:
    rows = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if current:
                rows.append(current)
                current = None
            continue
        if not raw.startswith(" ") and line.endswith(":"):
            if current:
                rows.append(current)
            selector = line[:-1].strip().strip('"')
            current = {"name": selector.split("@", 1)[0] if not selector.startswith("@")
                       else "@" + selector[1:].split("@", 1)[0],
                       "version": "", "source": "registry", "integrity": False,
                       "registry": "", "git": ""}
            continue
        if current is None:
            continue
        match = re.match(r'\s+version\s+"?([^"\s]+)"?', line)
        if match:
            current["version"] = match.group(1)
        match = re.match(r'\s+resolved\s+"?([^"\s]+)"?', line)
        if match:
            current["registry"] = match.group(1)
        if re.match(r"\s+integrity\s+", line):
            current["integrity"] = True
    if current:
        rows.append(current)
    return rows


def _pnpm_rows(text: str) -> list[dict[str, Any]]:
    rows = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = re.match(r"^\s{2,}/?(@?[^@\s:]+)@([^:\s]+):\s*$", line)
        if match:
            if current:
                rows.append(current)
            current = {"name": match.group(1), "version": match.group(2),
                       "source": "registry", "integrity": False,
                       "registry": "", "git": ""}
            continue
        if current is None:
            continue
        if re.search(r"\bintegrity:\s*\S+", line):
            current["integrity"] = True
        tarball = re.search(r"\btarball:\s*(\S+)", line)
        if tarball:
            current["registry"] = tarball.group(1)
        if re.search(r"\b(?:git|repository):", line):
            current["source"] = "git"
    if current:
        rows.append(current)
    return rows


def _gem_rows(text: str) -> list[dict[str, Any]]:
    rows = []
    mode = ""
    remote = ""
    revision = ""
    branch = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line in ("GIT", "PATH", "GEM"):
            mode = line
            remote = revision = branch = ""
            continue
        if line.startswith("  remote:"):
            remote = line.split(":", 1)[1].strip()
            continue
        if line.startswith("  revision:"):
            revision = line.split(":", 1)[1].strip()
            continue
        if line.startswith("  branch:"):
            branch = line.split(":", 1)[1].strip()
            continue
        if line.startswith("    ") and not line.startswith("      "):
            parts = line.strip().split(" ")
            if len(parts) >= 2:
                name, version = parts[0], parts[1].strip("()")
                if mode == "GIT":
                    pinned = bool(revision and re.fullmatch(
                        r"[0-9a-fA-F]{7,40}", revision))
                    rows.append({"name": name, "version": version,
                                 "source": "git", "integrity": pinned,
                                 "registry": remote, "git": remote,
                                 "pinned": pinned, "branch": branch})
                elif mode == "PATH":
                    rows.append({"name": name, "version": version,
                                 "source": "path", "integrity": False,
                                 "registry": "", "git": ""})
                else:
                    rows.append({"name": name, "version": version,
                                 "source": "registry", "integrity": True,
                                 "registry": remote, "git": ""})
    return rows


def _check_lock(ctx: _Context, rel: str, text: str) -> None:
    parsed = _package_rows(rel, text)
    if parsed is None:
        return
    ecosystem = parsed["ecosystem"]
    rows = parsed["rows"]
    missing_integrity: list[str] = []
    untrusted: Counter[str] = Counter()
    mutable: list[str] = []
    direct: list[str] = []
    missing_versions = 0
    with_integrity = 0
    for row in rows:
        ctx.packages += 1
        name = str(row.get("name") or "")
        if not name:
            continue
        version = str(row.get("version") or "")
        source = str(row.get("source") or "registry")
        if not version:
            missing_versions += 1
        if source == "git":
            pinned = bool(row.get("pinned")) or bool(re.search(
                r"#(?:[0-9a-fA-F]{7,40})\b", str(row.get("git") or "")))
            if not pinned:
                mutable.append(name)
        elif source in ("path", "url"):
            direct.append(name)
        elif source == "registry":
            registry = str(row.get("registry") or "")
            if row.get("integrity"):
                ctx.packages_with_integrity += 1
                with_integrity += 1
            else:
                ctx.packages_without_integrity += 1
                missing_integrity.append(name)
            if registry:
                scheme, host, _, credentials = _url_parts(registry)
                source = _source_hash(registry)
                if credentials:
                    _add(ctx, "registry_credentials", f"registry:{source}:credentials",
                         file=rel, line=1,
                         summary=f"Lockfile source for {name} embeds credentials.",
                         evidence={"package": name, "source": source})
                if scheme == "http" or not _registry_allowed(registry, ctx.policy):
                    untrusted[source] += 1
                if host and _registry_allowed(registry, ctx.policy):
                    ctx.public_locks.add((ecosystem, _normalized(name)))
    if missing_integrity and (with_integrity or ctx.policy["require_integrity"]):
        severity = "high" if ctx.policy["require_integrity"] else None
        _add(ctx, "missing_integrity", f"lock:{rel}:integrity",
             file=rel, line=1, severity=severity,
             summary=(f"{len(missing_integrity)} package entries in {rel} have "
                      "no integrity/hash metadata"
                      + (f" (for example {', '.join(missing_integrity[:MAX_EXAMPLES])})"
                         if missing_integrity else "") + "."),
             evidence={"lockfile": rel, "count": len(missing_integrity),
                       "examples": missing_integrity[:MAX_EXAMPLES]},
             extra={"ecosystem": ecosystem})
    for source, count in sorted(untrusted.items()):
        _add(ctx, "untrusted_registry", f"lock:{rel}:{source}",
             file=rel, line=1,
             summary=f"{count} package entries in {rel} resolve from a registry "
                     "outside the allowlist.",
             evidence={"lockfile": rel, "source": source, "count": count},
             extra={"ecosystem": ecosystem})
    if mutable:
        _add(ctx, "mutable_git_dependency", f"lock:{rel}:git",
             file=rel, line=1,
             summary=f"{len(mutable)} git dependency entries in {rel} are not "
                     "pinned to an immutable revision"
                     + (f" ({', '.join(mutable[:MAX_EXAMPLES])})" if mutable else "")
                     + ".",
             evidence={"lockfile": rel, "packages": mutable[:MAX_EXAMPLES]},
             extra={"ecosystem": ecosystem})
    if direct:
        _add(ctx, "direct_source_dependency", f"lock:{rel}:direct",
             file=rel, line=1,
             summary=f"{len(direct)} entries in {rel} come from a path or "
                     "direct URL rather than a registry."
                     + (f" ({', '.join(direct[:MAX_EXAMPLES])})" if direct else ""),
             evidence={"lockfile": rel, "packages": direct[:MAX_EXAMPLES]},
             extra={"ecosystem": ecosystem})
    if missing_versions:
        _add(ctx, "wildcard_dependency", f"lock:{rel}:version",
             file=rel, line=1,
             summary=f"{missing_versions} entries in {rel} have no resolved "
                     "version.",
             evidence={"lockfile": rel, "count": missing_versions},
             extra={"ecosystem": ecosystem})


# --------------------------------------------------------------------------- #
# install-time code and typosquats
# --------------------------------------------------------------------------- #

def _check_node_packages(ctx: _Context) -> None:
    for rel, text in ctx.sources.items():
        if Path(rel).name != "package.json":
            continue
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, Mapping):
            continue
        scripts = data.get("scripts") if isinstance(data.get("scripts"), Mapping) else {}
        lifecycle = sorted(name for name in scripts if name.lower() in _LIFECYCLE)
        if not lifecycle:
            continue
        in_node_modules = "node_modules" in Path(rel).parts
        name = str(data.get("name") or "")
        version = str(data.get("version") or "")
        if in_node_modules:
            if not name or _install_script_allowed(name, ctx.policy):
                continue
            ctx.install_scripts.append({"name": name, "version": version,
                                        "scripts": lifecycle, "file": rel})
            _add(ctx, "install_script", f"package:node:{name}",
                 file=rel, line=1,
                 summary=f"Dependency {name} {version} runs install lifecycle "
                         f"scripts: {', '.join(lifecycle)}.",
                 evidence={"package": name, "version": version,
                           "scripts": lifecycle})
        else:
            _add(ctx, "install_script", f"project:node:{rel}",
                 file=rel, line=1, severity="low",
                 summary="The project package runs install lifecycle scripts: "
                         + ", ".join(lifecycle) + ".",
                 evidence={"scripts": lifecycle, "scope": "project"})


def _check_build_files(ctx: _Context) -> None:
    for rel, text in ctx.sources.items():
        name = Path(rel).name
        if name == "setup.py":
            if re.search(r"\b(cmdclass|subprocess|os\.system|exec\(|eval\()", text):
                _add(ctx, "install_script", f"python:setup.py:{rel}",
                     file=rel, line=1,
                     summary="setup.py contains custom or executable build logic.",
                     evidence={"markers": sorted(set(re.findall(
                         r"\b(cmdclass|subprocess|os\.system|exec|eval)\b", text)))})
            else:
                _add(ctx, "install_script", f"python:setup.py:{rel}",
                     file=rel, line=1, severity="low",
                     summary="setup.py executes project code during build/install.",
                     evidence={"markers": ["setup.py"]})
        elif name == "setup.cfg":
            if re.search(r"^\s*(cmdclass|build_ext|options\.extras_require)",
                         text, re.M):
                _add(ctx, "install_script", f"python:setup.cfg:{rel}",
                     file=rel, line=1,
                     summary="setup.cfg declares custom build command settings.",
                     evidence={"file": rel})
        elif name == "build.rs":
            suspicious = sorted(set(re.findall(
                r"\b(Command::new|std::process|reqwest|curl|wget|TcpStream|"
                r"std::net|include_bytes!)\b|env!\(\s*\"OUT_DIR\"", text)))
            if suspicious:
                _add(ctx, "install_script", f"rust:build.rs:{rel}",
                     file=rel, line=1,
                     summary="Cargo build script contains process/network/file "
                             "logic that runs at build time.",
                     evidence={"markers": suspicious})


def _check_typosquats(ctx: _Context) -> None:
    seen: set[tuple[str, str]] = set()
    for manifest in ctx.inventory.get("manifests") or []:
        if not isinstance(manifest, Mapping):
            continue
        ecosystem = str(manifest.get("ecosystem") or "")
        popular = _POPULAR.get(ecosystem)
        if not popular:
            continue
        for name in manifest.get("dependencies") or []:
            dep = str(name)
            normalized = _normalized(dep)
            if (ecosystem, normalized) in seen or normalized in popular:
                continue
            seen.add((ecosystem, normalized))
            if dep.startswith("@") or len(normalized) < 4:
                continue
            closest = min(popular, key=lambda item: _levenshtein(normalized, item))
            distance = _levenshtein(normalized, closest)
            transposed = (sorted(normalized) == sorted(closest)
                          and normalized != closest)
            if distance != 1 and not transposed:
                continue
            _add(ctx, "typosquat_candidate", f"dependency:{ecosystem}:{dep}",
                 file=str(manifest.get("file") or ""),
                 summary=f"Dependency {dep} is suspiciously close to popular "
                         f"package {closest}.",
                 evidence={"package": dep, "popular": closest,
                           "distance": distance})


def _check_confusion(ctx: _Context) -> None:
    if not ctx.policy["internal_packages"]:
        return
    seen: set[tuple[str, str]] = set()
    for manifest in ctx.inventory.get("manifests") or []:
        if not isinstance(manifest, Mapping):
            continue
        ecosystem = str(manifest.get("ecosystem") or "")
        for name in manifest.get("dependencies") or []:
            dep = str(name)
            if not _internal_name(dep, ctx.policy):
                continue
            key = (ecosystem, _normalized(dep))
            if key in seen:
                continue
            seen.add(key)
            if key in ctx.public_locks:
                _add(ctx, "dependency_confusion_candidate",
                     f"dependency:{ecosystem}:{dep}",
                     file=str(manifest.get("file") or ""),
                     summary=f"Internal package {dep} resolves from a public "
                             "registry in the lockfile.",
                     evidence={"package": dep,
                               "registry": "public"})
            else:
                _add(ctx, "dependency_confusion_candidate",
                     f"dependency:{ecosystem}:{dep}",
                     file=str(manifest.get("file") or ""),
                     summary=f"Internal package {dep} is declared without a "
                             "locked private-registry source.",
                     evidence={"package": dep,
                               "registry": "unknown"},
                     severity="medium", confidence="low")


def _source_specs(ctx: _Context) -> Iterable[tuple[str, str, int, str]]:
    for rel, text in sorted(ctx.sources.items()):
        name = Path(rel).name
        if name != "package.json" and "node_modules" in Path(rel).parts:
            continue
        specs = _manifest_specs(rel, text)
        for dep_name, spec, line in specs:
            yield rel, dep_name, line, spec


def _check_manifests(ctx: _Context) -> None:
    for rel, dep_name, line, spec in _source_specs(ctx):
        ecosystem = ("node" if Path(rel).name == "package.json" else
                     "rust" if Path(rel).name == "Cargo.toml" else
                     "go" if Path(rel).name == "go.mod" else
                     "ruby" if Path(rel).name == "Gemfile" else
                     "composer" if Path(rel).name == "composer.json" else
                     "python")
        _check_dep_spec(ctx, rel, line, dep_name, spec, ecosystem)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def analyze(root: str | Path, *, inventory: Mapping[str, Any] | None = None,
            policy: str | Path | Mapping[str, Any] | None = None,
            budget_s: float = DEFAULT_BUDGET_S,
            max_findings: int = MAX_FINDINGS) -> dict[str, Any]:
    """Run the offline checks and return findings plus a bounded summary.

    ``inventory`` is the dependency inventory from the deep-review analyzer;
    when omitted, ``check()`` builds it first. No network, install or target
    import is performed.
    """
    root = Path(root).resolve()
    try:
        budget = float(budget_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_s must be finite and non-negative") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_s must be finite and non-negative")
    budget = min(budget, 60.0)
    try:
        normalized = load_policy(policy, root=root)
    except ValueError as exc:
        return {
            "findings": [], "checked": False, "truth_assessed": False,
            "policy_hash": None, "summary": {"state": "invalid"},
            "not_checked": [{"reason": str(exc)}],
        }
    started = time.monotonic()
    deadline = started + budget
    collected = _iter_sources(root, deadline)
    sources = collected["sources"]
    ctx = _Context(root, sources, inventory or {}, normalized,
                   max(0, int(max_findings)))
    ctx.omissions.extend(collected["omissions"])
    for rel, text in sorted(sources.items()):
        name = Path(rel).name
        if name in (".npmrc", ".yarnrc", ".yarnrc.yml"):
            _check_npmrc(ctx, rel, text)
        elif name in ("pip.conf", "pip.ini", ".pypirc"):
            _check_ini_like(ctx, rel, text)
        elif name.startswith("requirements") and name.endswith(".txt"):
            _check_requirements(ctx, rel, text)
        elif name == "pyproject.toml":
            _check_pyproject(ctx, rel, text)
        elif name == "Cargo.toml":
            _check_cargo_config(ctx, rel, text)
        elif name == "composer.json":
            _check_composer(ctx, rel, text)
        elif name == "Gemfile":
            _check_gemfile(ctx, rel, text)
        elif name == "go.mod":
            _check_go_mod(ctx, rel, text)
    _check_manifests(ctx)
    for rel, text in sorted(sources.items()):
        if Path(rel).name in _LOCK_NAMES:
            _check_lock(ctx, rel, text)
    _check_node_packages(ctx)
    _check_build_files(ctx)
    _check_typosquats(ctx)
    _check_confusion(ctx)

    findings = list(ctx.findings.values())
    by_severity = Counter(finding["severity"] for finding in findings)
    by_rule = Counter(finding["type"] for finding in findings)
    summary = {
        "state": "checked" if sources else "no_inputs",
        "policy_hash": policy_hash(normalized),
        "rules": sorted(RULES),
        "findings": len(findings),
        "by_severity": {name: by_severity.get(name, 0)
                        for name in _SEVERITIES},
        "by_rule": dict(sorted(by_rule.items())),
        "packages_checked": ctx.packages,
        "packages_with_integrity": ctx.packages_with_integrity,
        "packages_without_integrity": ctx.packages_without_integrity,
        "install_scripts": ctx.install_scripts[:MAX_EXAMPLES],
        "files_read": collected["files_read"],
        "bytes_read": collected["bytes_read"],
        "duration_s": round(time.monotonic() - started, 6),
        "truncated": bool(collected["truncated"]),
        "not_assessed": list(NOT_ASSESSED),
        "truth_assessed": False,
    }
    return {
        "findings": findings,
        "summary": summary,
        "checked": bool(sources),
        "truth_assessed": False,
        "policy_hash": summary["policy_hash"],
        "not_checked": [{"reason": item}
                        for item in sorted(set(ctx.omissions))],
    }


def check(root: str | Path, *, policy: str | Path | Mapping[str, Any] | None = None,
          budget_s: float = 20.0,
          max_findings: int = MAX_FINDINGS) -> dict[str, Any]:
    """Build the dependency inventory, then run the supply-chain checks."""
    from ozagia.core.deep_review.analyzers.dependencies import analyze as dep_analyze
    half = max(1.0, budget_s / 2)
    dependency = dep_analyze(root, budget_s=half, supply_chain=False)
    result = analyze(root, inventory=dependency["inventory"], policy=policy,
                     budget_s=half, max_findings=max_findings)
    result["dependency"] = {
        "checked": dependency.get("checked"),
        "cve_state": dependency.get("cve_state"),
        "manifests": len(dependency["inventory"].get("manifests") or []),
        "lockfiles": len(dependency["inventory"].get("lockfiles") or []),
        "not_checked": dependency.get("not_checked") or [],
    }
    return result


__all__ = ["MAX_FINDINGS", "NOT_ASSESSED", "RULES", "analyze", "check",
           "load_policy", "policy_hash"]
