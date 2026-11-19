"""Bounded offline inventory of common dependency manifests and lockfiles.

This module reads repository text only. It does not install packages, import
the target, contact registries, or make vulnerability/license assertions. A
deterministic SBOM-shaped inventory is returned for a caller to write; this
module never writes a file.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote

from ozagia.core import manifests

MAX_FILES = 3000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 20_000_000
MAX_ITEMS = 500
MAX_CONFLICTS = 50
MAX_CONFLICT_LOCATIONS = 50
CVE_STATE = "unknown"
SBOM_SCHEMA = "CycloneDX"
SBOM_SPEC_VERSION = "1.5"
SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules",
             "__pycache__", ".tox", "target", "dist"}
_LOCK_NAMES = {
    "python": ("uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock"),
    "node": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
             "yarn.lock"),
    "rust": ("Cargo.lock",),
    "go": ("go.sum",),
    "ruby": ("Gemfile.lock",),
    "composer": ("composer.lock",),
    "maven": ("gradle.lockfile",),
}
_LICENSE_FILE = re.compile(
    r"^(?:licen[cs]e|copying|notice)(?:[-._][A-Za-z0-9._-]+)?$", re.I)
_LICENSE_CLASSIFIER = "License ::"
_VERSION_TOKEN = re.compile(r"^[vV]?(\d+(?:\.\d+)*(?:[-+][\w.]+)?)$")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GO_REQ = re.compile(r"^\s*(?:require\s+)?([a-z0-9][\w.\-]*\.[a-z]{2,}"
                     r"/[\w.\-/]+)\s+(v[\w.\-+]+)", re.M)
_PNPM_PACKAGE = re.compile(
    r"^\s{2,}['\"]?/?((?:@[^/\s'\"]+/)?[^@\s'\"]+)@([^:\s'\"]+)")
_YARN_VERSION = re.compile(r"^\s+version\s*:?\s*['\"]?([^\s'\"]+)")
_RUBY_GEM_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?:\.[A-Za-z0-9_-]+)*\Z")
_RUBY_GEM_VERSION = re.compile(
    r"\d+(?:\.\d+)*(?:[-.][A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*)?\Z")
_COMPOSER_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_COMPOSER_VERSION = re.compile(
    r"(?i)v?\d+(?:\.\d+)*(?:[-.]?(?:dev|alpha\d*|a\d*|beta\d*|b\d*|"
    r"rc\d*|patch\d*|p\d+))?\Z")
_MAVEN_PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,118}"
_MAVEN_COORDINATE = re.compile(rf"({_MAVEN_PART}):({_MAVEN_PART})\Z")
_MAVEN_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAVEN_UNPINNED_VERSIONS = frozenset({
    "latest", "latest.integration", "latest.release", "release", "unspecified",
})
_GRADLE_LOCK_ROW = re.compile(
    rf"({_MAVEN_PART}:{_MAVEN_PART}):([^=\s]+)=([A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*)\Z")


def _walk(root: Path, deadline: float):
    """Yield sorted regular files without following symlinks, within hard caps."""
    count = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        if time.monotonic() >= deadline or count >= MAX_FILES:
            return
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and
                         not (Path(base) / d).is_symlink())
        for name in sorted(names):
            if time.monotonic() >= deadline or count >= MAX_FILES:
                return
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            count += 1
            yield path


def _read(path: Path, remaining: int) -> str | None:
    try:
        if path.stat().st_size > min(MAX_FILE_BYTES, remaining):
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _license_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("text", "file", "type"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _manifest_licenses(rel: str, text: str) -> list[str]:
    """Return project licence declarations found in one manifest as text."""
    name = PurePosixPath(rel).name
    found: list[str] = []
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project") or {}
            for value in (project.get("license"),
                          *([] if not isinstance(project.get("license-files"), list)
                            else project.get("license-files") or [])):
                license_text = _license_value(value)
                if license_text:
                    found.append(license_text)
            for classifier in project.get("classifiers") or []:
                if isinstance(classifier, str) and classifier.startswith(
                        _LICENSE_CLASSIFIER):
                    value = classifier.split("::")[-1].strip()
                    if value:
                        found.append(value)
            poetry = (data.get("tool") or {}).get("poetry") or {}
            value = _license_value(poetry.get("license"))
            if value:
                found.append(value)
        elif name == "package.json":
            data = json.loads(text)
            value = _license_value(data.get("license"))
            if value:
                found.append(value)
            licenses = data.get("licenses")
            if isinstance(licenses, list):
                for item in licenses:
                    value = _license_value(item)
                    if value:
                        found.append(value)
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            package = data.get("package") or {}
            for value in (package.get("license"), package.get("license-file")):
                license_text = _license_value(value)
                if license_text:
                    found.append(license_text)
            workspace = (data.get("workspace") or {}).get("package") or {}
            for value in (workspace.get("license"),
                          workspace.get("license-file")):
                license_text = _license_value(value)
                if license_text:
                    found.append(license_text)
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return []
    return sorted({item for item in found if item})


def _exact_version(spec: str, *, bare_exact: bool = True) -> str | None:
    """Return a pinned version from a requirement spec, or None for a range."""
    value = spec.split(";", 1)[0].strip()
    if not value:
        return None
    match = re.match(r"^(===|==)\s*([^,\s]+)$", value)
    if match:
        candidate = match.group(2)
        if "*" in candidate:
            return None
        version = _VERSION_TOKEN.match(candidate)
        return version.group(1) if version else None
    if any(operator in value for operator in (">=", "<=", "!=", "~=", ">", "<")):
        return None
    if value.startswith(("^", "~", "@", "file:", "git+", "http:", "https:")):
        return None
    if not bare_exact:
        return None
    version = _VERSION_TOKEN.match(value)
    return version.group(1) if version else None


def _requirement(name: str, spec: str, *, bare_exact: bool = True) -> dict[str, Any]:
    spec = spec.split(";", 1)[0].strip()
    return {"name": name, "constraint": spec or "*",
            "version": _exact_version(spec, bare_exact=bare_exact)}


def _parse_pep508(value: Any, *, bare_exact: bool = True) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    match = _REQUIREMENT_NAME.match(value)
    if not match:
        return None
    return _requirement(match.group(1), value[match.end():],
                        bare_exact=bare_exact)


def _declared_dependencies(rel: str, text: str) -> list[dict[str, Any]]:
    """Exact pins and declared constraints, without importing the target."""
    name = PurePosixPath(rel).name
    rows: list[dict[str, Any]] = []
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project") or {}
            groups = [project.get("dependencies") or []]
            groups += list((project.get("optional-dependencies") or {}).values())
            groups += list((data.get("dependency-groups") or {}).values())
            for group in groups:
                for value in group or []:
                    item = _parse_pep508(value)
                    if item:
                        rows.append(item)
            poetry = (data.get("tool") or {}).get("poetry") or {}
            for dep_name, spec in (poetry.get("dependencies") or {}).items():
                if dep_name == "python":
                    continue
                if isinstance(spec, Mapping):
                    rows.append(_requirement(dep_name, str(spec.get("version") or "")))
                elif isinstance(spec, str):
                    rows.append(_requirement(dep_name, spec))
            for dep_name in (poetry.get("dev-dependencies") or {}):
                rows.append(_requirement(dep_name, ""))
            for group in (poetry.get("group") or {}).values():
                if isinstance(group, Mapping):
                    for dep_name, spec in (group.get("dependencies") or {}).items():
                        version = (str(spec.get("version") or "")
                                   if isinstance(spec, Mapping) else str(spec))
                        rows.append(_requirement(dep_name, version))
        elif name == "package.json":
            data = json.loads(text)
            for field in ("dependencies", "devDependencies",
                          "optionalDependencies", "peerDependencies"):
                section = data.get(field)
                if isinstance(section, Mapping):
                    for dep_name, spec in section.items():
                        if isinstance(spec, str):
                            rows.append(_requirement(dep_name, spec))
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
                        version = str(spec.get("version") or "")
                    elif isinstance(spec, str):
                        version = spec
                    else:
                        continue
                    # A bare Cargo version is a caret range, not a pin.
                    rows.append(_requirement(dep_name, version, bare_exact=False))
        elif name == "go.mod":
            for dep_name, version in _GO_REQ.findall(text):
                rows.append(_requirement(dep_name, version))
        elif name.startswith("requirements") and name.endswith(".txt"):
            for line in text.splitlines():
                value = line.split("#", 1)[0].strip()
                if not value or value.startswith(("-", "--")):
                    continue
                item = _parse_pep508(value)
                if item:
                    rows.append(item)
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return []
    unique = {(row["name"], row["constraint"], row["version"]) for row in rows}
    return [{"name": item[0], "constraint": item[1], "version": item[2]}
            for item in sorted(unique)]


def _yarn_name(selector: str) -> str | None:
    value = selector.strip().strip("'\"")
    if not value:
        return None
    if "@npm:" in value:
        descriptor, reference = value.split("@npm:", 1)
        # Berry descriptors use npm:<range> for ordinary packages, but an
        # npm:<package>@<range> reference names the real alias target.
        alias = re.fullmatch(r"((?:@[^/@\s]+/)?[^/@\s]+)@[^@\s]+", reference)
        value = alias.group(1) if alias else descriptor
    if value.startswith("@"):
        split = value.rfind("@")
        return value[:split] if split > 0 else value
    return value.split("@", 1)[0]


def _yarn_packages(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    selectors: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace() and line.rstrip().endswith(":"):
            selectors = [part.strip().strip("'\"")
                         for part in line.rstrip()[:-1].split(",")]
            continue
        match = _YARN_VERSION.match(line)
        if not match or not selectors:
            continue
        for selector in selectors:
            name = _yarn_name(selector)
            if name:
                row = {"name": name, "version": match.group(1)}
                # Explicit non-registry Yarn selectors describe a different
                # source even when their package name/version matches npm.
                if re.search(r"@(?:git\+|git:|github:|gitlab:|bitbucket:|ssh:)", selector, re.I):
                    row["provenance"] = "git"
                elif re.search(r"@(?:file:|link:|workspace:)", selector, re.I):
                    row["provenance"] = "workspace-or-path"
                rows.append(row)
        selectors = []
    return rows


def _pnpm_packages(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    in_packages = False
    for line in text.splitlines():
        if not in_packages:
            in_packages = re.match(r"^packages:\s*$", line) is not None
            continue
        if line and not line[0].isspace():
            break
        match = _PNPM_PACKAGE.match(line)
        if match:
            version = match.group(2)
            # pnpm appends peer-resolution context to a package key; it is
            # not part of the published package version. Only strip simple,
            # well-formed suffixes from a recognizable version token.
            peer_context = re.fullmatch(r"([^()]+)(?:\([^()]+\))+", version)
            if peer_context and _VERSION_TOKEN.fullmatch(peer_context.group(1)):
                version = peer_context.group(1)
            rows.append({"name": match.group(1), "version": version})
    return rows


def _bundler_packages(text: str) -> tuple[list[dict[str, Any]], list[str], int]:
    """Read only Bundler's top-level specs; skip malformed pins visibly."""
    rows: list[dict[str, Any]] = []
    omissions: list[str] = []
    source_kind: str | None = None
    in_specs = False
    entries = 0
    unsupported = 0
    truncated = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if line and not line[0].isspace():
            source_kind = {"GEM": "rubygems", "GIT": "git", "PATH": "path"}.get(line)
            in_specs = False
            continue
        if source_kind and line.strip() == "specs:":
            in_specs = True
            continue
        if not in_specs or not line.startswith("    ") or line.startswith("      "):
            continue
        match = re.fullmatch(r"    ([^\s()]+) \(([^\s()]+)\)", line)
        if not match:
            if len(omissions) < 100:
                omissions.append(f"Gemfile.lock:{line_no}: unsupported gem spec row")
            unsupported += 1
            continue
        name, version = match.groups()
        entries += 1
        if (not _RUBY_GEM_NAME.fullmatch(name)
                or not _RUBY_GEM_VERSION.fullmatch(version)):
            unsupported += 1
            if len(omissions) < 100:
                omissions.append(f"Gemfile.lock:{line_no}: unsupported gem name/version")
            continue
        if len(rows) < MAX_ITEMS:
            rows.append({"name": name, "version": version,
                         "source": source_kind})
        else:
            truncated = True
    if unsupported > len(omissions):
        omissions.append(f"Gemfile.lock: {unsupported} unsupported gem pins total")
    if truncated:
        omissions.append(f"Gemfile.lock: package inventory truncated at {MAX_ITEMS} entries")
    return rows, omissions, entries


def _composer_packages(text: str) -> tuple[list[dict[str, Any]], list[str], int]:
    """Read bounded Composer package identities; unsupported pins stay visible."""
    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    document = json.loads(text, object_pairs_hook=object_without_duplicates,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValueError(f"invalid JSON constant {value}")))
    if not isinstance(document, dict):
        raise ValueError("Composer lock must be a JSON object")
    rows: list[dict[str, Any]] = []
    omissions: list[str] = []
    entries = unsupported = 0
    arrays = []
    for field in ("packages", "packages-dev"):
        value = document.get(field, [])
        if not isinstance(value, list):
            omissions.append(f"composer.lock: {field} is not an array")
            continue
        arrays.extend((field, package) for package in value)
    for field, package in arrays:
        entries += 1
        name = package.get("name") if isinstance(package, dict) else None
        version = package.get("version") if isinstance(package, dict) else None
        if (not isinstance(name, str) or not name.isascii()
                or not _COMPOSER_NAME.fullmatch(name)
                or not isinstance(version, str) or not version.isascii()
                or not _COMPOSER_VERSION.fullmatch(version)):
            unsupported += 1
            if len(omissions) < 100:
                omissions.append(
                    f"composer.lock: unsupported package identity/version in {field}")
            continue
        source = package.get("source")
        dist = package.get("dist")
        source_type = (source.get("type") if isinstance(source, dict)
                       and isinstance(source.get("type"), str) else None)
        dist_type = (dist.get("type") if isinstance(dist, dict)
                     and isinstance(dist.get("type"), str) else None)
        # Keep only non-sensitive provenance labels. URLs and references can
        # contain credentials or private repository details.
        provenance = ("path" if "path" in {source_type, dist_type}
                      else "git" if source_type in {"git", "hg", "svn", "fossil", "perforce"}
                      else "dist" if dist_type else "unknown")
        row = {"name": name.lower(), "version": version,
               "source": "composer.lock", "provenance": provenance}
        if len(rows) < MAX_ITEMS:
            rows.append(row)
    if unsupported > len([item for item in omissions
                          if "unsupported package identity/version" in item]):
        omissions.append(f"composer.lock: {unsupported} unsupported package pins total")
    if entries > MAX_ITEMS:
        omissions.append(f"composer.lock: package inventory truncated at {MAX_ITEMS} entries")
    return rows, omissions, entries


def _composer_custom_repositories(text: str) -> bool:
    """Whether composer.json can resolve packages from non-Packagist sources.

    Composer lock entries don't identify which configured repository supplied
    a package. When any custom repository is configured, provenance is
    ambiguous for every locked package, so local Packagist advisories must not
    be treated as a package identity match.
    """
    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    document = json.loads(text, object_pairs_hook=object_without_duplicates,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValueError(f"invalid JSON constant {value}")))
    if not isinstance(document, dict):
        raise ValueError("composer.json must be a JSON object")
    repositories = document.get("repositories", [])
    if repositories is False:
        return True
    if not isinstance(repositories, list):
        raise ValueError("composer.json repositories must be an array")
    # Only an explicit canonical Packagist declaration is provenance-neutral.
    # A disabled Packagist source or any other repository makes package origin
    # ambiguous for lock entries, which do not say which repository won.
    return any(not (isinstance(row, dict) and row.get("packagist.org") is True)
               for row in repositories)


def _gradle_lock_packages(text: str) -> tuple[list[dict[str, Any]], list[str], int]:
    """Read Gradle's resolved group:module:version lock rows only.

    Repository URLs are not stored in Gradle lockfiles, so provenance remains
    explicitly unknown. Dynamic and non-coordinate rows are omitted.
    """
    rows: list[dict[str, Any]] = []
    omissions: list[str] = []
    entries = unsupported = 0
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "empty=":
            continue
        entries += 1
        match = _GRADLE_LOCK_ROW.fullmatch(stripped)
        if not match:
            unsupported += 1
            if len(omissions) < 100:
                omissions.append(
                    f"gradle.lockfile:{line_no}: unsupported or dynamic module pin")
            continue
        coordinate, version, _configurations = match.groups()
        module = _MAVEN_COORDINATE.fullmatch(coordinate)
        if (not module or not _MAVEN_VERSION.fullmatch(version)
                or version.lower() in _MAVEN_UNPINNED_VERSIONS
                or version.lower().endswith("-snapshot")):
            unsupported += 1
            if len(omissions) < 100:
                omissions.append(
                    f"gradle.lockfile:{line_no}: unsupported Maven coordinate/version")
            continue
        group, artifact = module.groups()
        if len(rows) < MAX_ITEMS:
            rows.append({"name": f"{group}:{artifact}", "version": version,
                         "source": "gradle.lockfile",
                         "provenance": "repository-unknown"})
    if unsupported > len(omissions):
        omissions.append(f"gradle.lockfile: {unsupported} unsupported module pins total")
    if entries > MAX_ITEMS:
        omissions.append(
            f"gradle.lockfile: package inventory truncated at {MAX_ITEMS} entries")
    return rows, omissions, entries


def _npm_lock_row(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    # npm v2/v3 aliases retain the original package name in the entry.
    actual_name = value.get("name")
    row = {"name": actual_name if isinstance(actual_name, str) and actual_name else name,
           "version": value.get("version")}
    resolved = value.get("resolved")
    if value.get("link") is True or (isinstance(resolved, str) and
                                     resolved.startswith(("file:", "link:", "workspace:"))):
        row["provenance"] = "workspace-or-path"
    elif isinstance(resolved, str) and resolved.startswith(
            ("git+", "git:", "github:", "gitlab:", "bitbucket:", "ssh:")):
        row["provenance"] = "git"
    elif isinstance(resolved, str) and resolved.startswith(("http://", "https://")):
        from urllib.parse import urlsplit
        try:
            host = (urlsplit(resolved).hostname or "").lower()
        except ValueError:
            host = ""
        row["provenance"] = ("npm-registry" if host == "registry.npmjs.org"
                             else "other-or-unknown-registry")
    return row


def _python_non_registry_source(package: Mapping[str, Any]) -> str | None:
    """Classify explicit direct sources without retaining sensitive URLs."""
    source = package.get("source")
    if isinstance(source, Mapping):
        kind = source.get("type")
        if kind == "git" or "git" in source:
            return "git"
        if kind in {"directory", "file"} or any(
                key in source for key in ("directory", "editable", "path")):
            return "workspace-or-path"
        if kind == "url" or "url" in source:
            return "direct-url"
    if "git" in package:
        return "git"
    if any(key in package for key in ("path", "file", "editable")):
        return "workspace-or-path"
    return None


def _lock_inventory_raw(rel: str, text: str) -> dict[str, Any] | None:
    name = PurePosixPath(rel).name
    try:
        if name in ("package-lock.json", "npm-shrinkwrap.json"):
            doc = json.loads(text)
            packages = doc.get("packages", {})
            deps = doc.get("dependencies", {})
            rows = []
            if isinstance(packages, dict) and packages:
                for path, value in packages.items():
                    if path and isinstance(value, dict):
                        row = _npm_lock_row(path.rsplit("node_modules/", 1)[-1], value)
                        license_text = _license_value(value.get("license"))
                        if license_text:
                            row["license"] = license_text
                        rows.append(row)
            elif isinstance(deps, dict):
                # npm lockfile v1 nests installed transitive dependencies under
                # each package. Traverse iteratively so deep dependency trees
                # do not consume Python's call stack.
                pending = [deps]
                while pending:
                    section = pending.pop()
                    for key, value in section.items():
                        if not isinstance(value, dict):
                            continue
                        rows.append(_npm_lock_row(key, value))
                        nested = value.get("dependencies")
                        if isinstance(nested, dict):
                            pending.append(nested)
            result = {"file": rel, "ecosystem": "node", "format": "npm",
                      "entries": len(rows),
                      "packages": sorted(rows, key=lambda item: (item["name"],
                                                                    str(item["version"])))[:MAX_ITEMS]}
            if len(rows) > MAX_ITEMS:
                result["omissions"] = [f"{rel}: package inventory truncated at {MAX_ITEMS} entries"]
            return result
        if name == "Cargo.lock":
            doc = tomllib.loads(text)
            packages = doc.get("package", [])
            rows = []
            if isinstance(packages, list):
                for package in packages:
                    if isinstance(package, dict) and isinstance(package.get("name"), str):
                        source = package.get("source")
                        if isinstance(source, str) and source in {"registry+https://github.com/rust-lang/crates.io-index",
                                      "sparse+https://index.crates.io/"}:
                            provenance = "crates-io"
                        elif isinstance(source, str) and source.startswith("git+"):
                            provenance = "git"
                        elif source is None:
                            provenance = "workspace-or-path"
                        else:
                            provenance = "other-or-unknown-registry"
                        row = {"name": package["name"],
                               "version": package.get("version"),
                               "provenance": provenance}
                        license_text = _license_value(package.get("license"))
                        if license_text:
                            row["license"] = license_text
                        rows.append(row)
            return {"file": rel, "ecosystem": "rust", "format": "cargo",
                    "entries": len(rows),
                    "packages": sorted(rows, key=lambda item: (
                        item["name"], str(item["version"])))[:MAX_ITEMS]}
        if name in ("uv.lock", "poetry.lock", "pdm.lock"):
            doc = tomllib.loads(text)
            packages = doc.get("package", doc.get("packages", []))
            rows = []
            if isinstance(packages, (list, dict)):
                iterable = packages if isinstance(packages, list) else packages.values()
                for package in iterable:
                    if not isinstance(package, dict) or not isinstance(
                            package.get("name"), str):
                        continue
                    row = {"name": package["name"],
                           "version": package.get("version")}
                    provenance = _python_non_registry_source(package)
                    if provenance:
                        row["provenance"] = provenance
                    license_text = _license_value(package.get("license"))
                    if license_text:
                        row["license"] = license_text
                    rows.append(row)
            return {"file": rel, "ecosystem": "python", "format": name,
                    "entries": len(packages) if isinstance(packages, list) else
                    len(packages) if isinstance(packages, dict) else 0,
                    "packages": sorted(rows, key=lambda item: (item["name"],
                                                                  str(item["version"])))[:MAX_ITEMS]}
        if name == "Pipfile.lock":
            doc = json.loads(text)
            rows = []
            for group in ("default", "develop"):
                section = doc.get(group)
                if not isinstance(section, dict):
                    continue
                for dep_name, value in section.items():
                    version = None
                    if isinstance(value, dict):
                        version = _exact_version(str(value.get("version") or ""))
                    row = {"name": dep_name, "version": version}
                    if isinstance(value, dict):
                        provenance = _python_non_registry_source(value)
                        if provenance:
                            row["provenance"] = provenance
                    rows.append(row)
            return {"file": rel, "ecosystem": "python", "format": "pipenv",
                    "entries": len(rows), "packages": sorted(
                        rows, key=lambda item: (item["name"],
                                                str(item["version"])))[:MAX_ITEMS]}
        if name == "go.sum":
            rows = {}
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith("//"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                version = parts[1].split("/go.mod", 1)[0]
                rows[(parts[0], version)] = {"name": parts[0], "version": version}
            return {"file": rel, "ecosystem": "go", "format": "go.sum",
                    "entries": len(rows),
                    "packages": sorted(rows.values(), key=lambda item: (
                        item["name"], item["version"]))[:MAX_ITEMS]}
        if name == "Gemfile.lock":
            rows, omissions, entries = _bundler_packages(text)
            return {"file": rel, "ecosystem": "ruby", "format": "bundler",
                    "entries": entries, "packages": sorted(
                        rows, key=lambda item: (item["name"], item["version"],
                                                item["source"]))[:MAX_ITEMS],
                    "omissions": omissions}
        if name == "composer.lock":
            rows, omissions, entries = _composer_packages(text)
            return {"file": rel, "ecosystem": "composer", "format": "composer",
                    "entries": entries, "packages": sorted(
                        rows, key=lambda item: (item["name"], item["version"]))[:MAX_ITEMS],
                    "omissions": omissions}
        if name == "gradle.lockfile":
            rows, omissions, entries = _gradle_lock_packages(text)
            return {"file": rel, "ecosystem": "maven", "format": "gradle",
                    "entries": entries, "packages": sorted(
                        rows, key=lambda item: (item["name"], item["version"]))[:MAX_ITEMS],
                    "omissions": omissions}
        if name == "yarn.lock":
            rows = _yarn_packages(text)
            return {"file": rel, "ecosystem": "node", "format": "yarn",
                    "entries": len(rows), "packages": sorted(
                        rows, key=lambda item: (item["name"],
                                                str(item["version"])))[:MAX_ITEMS]}
        if name == "pnpm-lock.yaml":
            rows = _pnpm_packages(text)
            return {"file": rel, "ecosystem": "node", "format": "pnpm",
                    "entries": len(rows), "packages": sorted(
                        rows, key=lambda item: (item["name"],
                                                str(item["version"])))[:MAX_ITEMS]}
    except (ValueError, TypeError, RecursionError, tomllib.TOMLDecodeError):
        return None
    return None


def _lock_inventory(rel: str, text: str) -> dict[str, Any] | None:
    """Record possible package truncation for every supported lock format."""
    result = _lock_inventory_raw(rel, text)
    if result is None:
        return None
    packages = result.get("packages")
    entries = result.get("entries")
    omissions = result.setdefault("omissions", [])
    # Format parsers use a basename in diagnostics. Attribute those messages
    # to the actual lockfile so coverage scoring can identify nested paths.
    basename = PurePosixPath(rel).name
    if rel != basename:
        omissions[:] = [rel + item[len(basename):]
                        if isinstance(item, str) and item.startswith(basename + ":")
                        else item for item in omissions]
    if (isinstance(packages, list) and len(packages) == MAX_ITEMS
            and isinstance(entries, int) and entries > len(packages)
            and not any("inventory truncated" in str(item) for item in omissions)):
        # Some formats count unsupported entries separately, so do not assert
        # that every additional entry would have produced a package row.
        omissions.append(f"{rel}: package inventory may be truncated at {MAX_ITEMS} entries")
    return result


def _pinned_requirements(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip()
        if not value or value.startswith(("-", "--")):
            continue
        item = _parse_pep508(value)
        if item and item["version"]:
            rows.append({"name": item["name"], "version": item["version"]})
    return sorted(rows, key=lambda item: (item["name"], item["version"]))


def _find_lock(manifest_path: str, ecosystem: str, locks: list[dict[str, Any]]) -> str | None:
    directory = PurePosixPath(manifest_path).parent
    names = _LOCK_NAMES.get(ecosystem, ())
    while True:
        for lock in locks:
            p = PurePosixPath(lock["file"])
            if p.parent == directory and p.name in names:
                return lock["file"]
        if directory in (PurePosixPath("."), PurePosixPath("")):
            break
        directory = directory.parent
    return None


def _dependency_key(ecosystem: str, name: str) -> tuple[str, str]:
    normalized = (manifests.normalize(name) if ecosystem == "python"
                  else name if ecosystem in {"ruby", "maven"}
                  else name.lower())
    return ecosystem, normalized


def _normalized_version(ecosystem: str, version: Any) -> str:
    text = str(version)
    if ecosystem == "go" and text.startswith("v"):
        return text[1:]
    if ecosystem == "composer":
        return text.lower().removeprefix("v")
    return text


def _version_facts(manifest_details: list[dict[str, Any]],
                   locks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                         list[dict[str, Any]]]:
    """Duplicate declarations and exact-version conflicts across inputs."""
    declarations: dict[tuple[str, str], dict[str, set[str]]] = {}
    versions: dict[tuple[str, str], dict[str, set[str]]] = {}
    display: dict[tuple[str, str], str] = {}
    ecosystems: dict[tuple[str, str], str] = {}
    for detail in manifest_details:
        rel = detail["file"]
        ecosystem = detail["ecosystem"]
        for row in detail.get("declarations") or []:
            key = _dependency_key(ecosystem, row["name"])
            display.setdefault(key, row["name"])
            ecosystems[key] = ecosystem
            declarations.setdefault(key, {}).setdefault(
                str(row.get("constraint") or "*"), set()).add(rel)
            if row.get("version"):
                versions.setdefault(key, {}).setdefault(
                    _normalized_version(ecosystem, row["version"]), set()).add(rel)
    for lock in locks:
        ecosystem = str(lock.get("ecosystem") or "unknown")
        for row in lock.get("packages") or []:
            if not row.get("version"):
                continue
            key = _dependency_key(ecosystem, str(row["name"]))
            display.setdefault(key, str(row["name"]))
            ecosystems[key] = ecosystem
            versions.setdefault(key, {}).setdefault(
                _normalized_version(ecosystem, row["version"]), set()).add(
                    str(lock["file"]))

    conflicts = []
    for key, values in sorted(versions.items()):
        if len(values) < 2:
            continue
        conflicts.append({
            "dependency": display.get(key, key[1]),
            "ecosystem": ecosystems.get(key, key[0]),
            "versions": sorted(values),
            "sources": sorted({source for sources in values.values()
                               for source in sources}),
        })
    duplicates = []
    for key, values in sorted(declarations.items()):
        sources = sorted({source for group in values.values() for source in group})
        if len(sources) < 2:
            continue
        duplicates.append({
            "dependency": display.get(key, key[1]),
            "ecosystem": ecosystems.get(key, key[0]),
            "constraints": sorted(values),
            "sources": sources,
        })
    return conflicts[:MAX_CONFLICTS], duplicates[:MAX_CONFLICTS]


def _purl(ecosystem: str, name: str, version: str | None) -> str:
    if ecosystem == "python":
        path = quote(manifests.normalize(name), safe="")
        purl = f"pkg:pypi/{path}"
    elif ecosystem == "node":
        if name.startswith("@") and "/" in name:
            scope, package = name.split("/", 1)
            path = f"{quote(scope, safe='')}/{quote(package, safe='')}"
        else:
            path = quote(name, safe="")
        purl = f"pkg:npm/{path}"
    elif ecosystem == "rust":
        purl = f"pkg:cargo/{quote(name, safe='')}"
    elif ecosystem == "go":
        purl = f"pkg:golang/{quote(name, safe='/')}"
    elif ecosystem == "ruby":
        purl = f"pkg:gem/{quote(name, safe='')}"
    elif ecosystem == "composer":
        purl = f"pkg:composer/{quote(name.lower(), safe='/')}"
    elif ecosystem == "maven":
        parts = name.split(":", 1)
        if len(parts) == 2:
            purl = (f"pkg:maven/{quote(parts[0], safe='')}/"
                    f"{quote(parts[1], safe='')}")
        else:
            purl = f"pkg:maven/{quote(name, safe='')}"
    else:
        purl = f"pkg:generic/{quote(name, safe='')}"
    return f"{purl}@{quote(version, safe='')}" if version else purl


def build_sbom(source: Mapping[str, Any], *, root_name: str = "repository") -> dict[str, Any]:
    """Return a deterministic, CycloneDX-shaped inventory; write nothing.

    ``source`` may be an :func:`analyze` result or its ``inventory`` mapping.
    Components are built only from paths that were already read. CVE state is
    explicitly unknown for every component; no advisory database was queried.
    """
    inventory = source.get("inventory") if isinstance(
        source.get("inventory"), Mapping) else source
    components: dict[tuple[str, str, str], dict[str, Any]] = {}
    resolved: set[tuple[str, str]] = set()

    def add(ecosystem: str, name: str, version: str | None,
            source_file: str, license_text: str | None = None,
            provenance: str | None = None) -> None:
        key_name = (manifests.normalize(name) if ecosystem == "python"
                    else name.lower() if ecosystem in {"node", "composer"}
                    else name)
        key = (ecosystem, key_name, version or "")
        component = components.setdefault(key, {
            "type": "library",
            "bom-ref": _purl(ecosystem, name, version),
            "name": name,
            **({"version": version} if version else {}),
            "purl": _purl(ecosystem, name, version),
            "properties": [
                {"name": "ozagia:ecosystem", "value": ecosystem},
                {"name": "ozagia:cve_state", "value": CVE_STATE},
            ],
        })
        sources = {source_file}
        existing = next((item for item in component["properties"]
                         if item["name"] == "ozagia:sources"), None)
        if existing is not None:
            sources.update(str(existing["value"]).split(","))
        existing_license = next(
            (item["value"] for item in component["properties"]
             if item["name"] == "ozagia:license"), None)
        existing_provenance = next(
            (item["value"] for item in component["properties"]
             if item["name"] == "ozagia:provenance"), None)
        existing_sources = sorted(item for item in sources if item)
        provenances = set(str(existing_provenance or "").split(","))
        if provenance:
            provenances.add(provenance)
        component["properties"] = [
            item for item in component["properties"]
            if item["name"] not in {"ozagia:sources", "ozagia:license",
                                     "ozagia:provenance"}]
        component["properties"].append(
            {"name": "ozagia:sources", "value": ",".join(existing_sources)})
        if license_text or existing_license:
            component["properties"].append(
                {"name": "ozagia:license",
                 "value": license_text or existing_license})
        provenances.discard("")
        if provenances:
            component["properties"].append(
                {"name": "ozagia:provenance",
                 "value": ",".join(sorted(provenances))})
        component["properties"].sort(key=lambda item: item["name"])

    for lock in sorted(
            (item for item in inventory.get("lockfiles") or []
             if isinstance(item, Mapping)),
            key=lambda item: str(item.get("file") or "")):
        ecosystem = str(lock.get("ecosystem") or "unknown")
        for row in sorted(
                (item for item in lock.get("packages") or []
                 if isinstance(item, Mapping) and isinstance(item.get("name"), str)),
                key=lambda item: (str(item.get("name")),
                                  str(item.get("version") or ""))):
            version = row.get("version")
            version = str(version) if version else None
            add(ecosystem, row["name"], version, str(lock.get("file") or ""),
                str(row.get("license")) if row.get("license") else None,
                str(row.get("provenance")) if row.get("provenance") else None)
            if version:
                resolved.add(_dependency_key(ecosystem, row["name"]))
    for manifest in sorted(
            (item for item in inventory.get("manifests") or []
             if isinstance(item, Mapping)),
            key=lambda item: str(item.get("file") or "")):
        ecosystem = str(manifest.get("ecosystem") or "unknown")
        rel = str(manifest.get("file") or "")
        for dep_name in sorted(manifest.get("dependencies") or [], key=str):
            if not isinstance(dep_name, str):
                continue
            if _dependency_key(ecosystem, dep_name) in resolved:
                continue
            add(ecosystem, dep_name, None, rel)
    def ecosystem_of(item: Mapping[str, Any]) -> str:
        return next((str(prop.get("value")) for prop in item.get("properties") or []
                     if prop.get("name") == "ozagia:ecosystem"), "")

    def sort_name(item: Mapping[str, Any]) -> str:
        name = str(item.get("name") or "")
        return (manifests.normalize(name) if ecosystem_of(item) == "python"
                else name.lower())

    ordered = sorted(components.values(), key=lambda item: (
        ecosystem_of(item), sort_name(item), item.get("version", ""),
        item["purl"]))
    refs = [item["bom-ref"] for item in ordered]
    return {
        "bomFormat": SBOM_SCHEMA,
        "specVersion": SBOM_SPEC_VERSION,
        "version": 1,
        "metadata": {
            "tools": [{"name": "ozagia", "vendor": "ozagia"}],
            "component": {"type": "application", "name": root_name,
                          "bom-ref": "ozagia:repository"},
            "properties": [
                {"name": "ozagia:cve_state", "value": CVE_STATE},
                {"name": "ozagia:scope", "value":
                 "offline manifests and lockfiles only"},
            ],
        },
        "components": ordered,
        "dependencies": ([{"ref": "ozagia:repository", "dependsOn": refs}]
                         if refs else []),
        "properties": [
            {"name": "ozagia:cve_state", "value": CVE_STATE},
            {"name": "ozagia:license_assertions", "value": "not_assessed"},
        ],
    }


def analyze(root: str | Path, *, budget_s: float = 10.0,
            local_advisories: str | Path | Mapping[str, Any] | None = None,
            license_policy: str | Path | Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
    """Return an inventory and conservative findings within explicit limits.

    Result shape: ``{"findings": [...], "inventory": {...}, "sbom": {...},
    "checked": bool, "not_checked": [...]}``. Findings use the raw finding
    shape consumed by deep review (section/type/severity/keys/summary).
    Licence facts remain inventory only. Optional ``local_advisories`` is an
    explicit JSON path/object; it is never fetched and only parsed lockfile
    versions can match supported exact records or local SemVer ranges.
    Non-matches keep CVE coverage unknown.
    """
    root = Path(root).resolve()
    try:
        budget = float(budget_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_s must be finite and non-negative") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_s must be finite and non-negative")
    budget = min(budget, 60.0)
    started = time.monotonic()
    deadline = started + budget
    files_seen = total_bytes = 0
    manifests_found: list[tuple[str, dict[str, Any]]] = []
    manifest_details: list[dict[str, Any]] = []
    license_files: list[str] = []
    locks: list[dict[str, Any]] = []
    composer_repository_configs: dict[str, bool | None] = {}
    omissions: list[str] = []
    for path in _walk(root, deadline):
        files_seen += 1
        rel = path.relative_to(root).as_posix()
        name = PurePosixPath(rel).name
        if _LICENSE_FILE.match(name):
            license_files.append(rel)
        remaining = MAX_TOTAL_BYTES - total_bytes
        if remaining <= 0:
            omissions.append("total read size limit reached")
            break
        if manifests.is_manifest(rel) or name in {
                "package-lock.json", "npm-shrinkwrap.json", "Cargo.lock",
                "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock",
                "go.sum", "Gemfile.lock", "composer.lock", "yarn.lock",
                "composer.json", "pnpm-lock.yaml", "gradle.lockfile"}:
            text = _read(path, remaining)
            if text is None:
                omissions.append(f"{rel}: unreadable or exceeds per-file/total size limit")
                continue
            total_bytes += len(text.encode("utf-8"))
            if name == "composer.json":
                try:
                    composer_repository_configs[rel] = _composer_custom_repositories(text)
                except (ValueError, TypeError, RecursionError):
                    composer_repository_configs[rel] = None
                    omissions.append(f"{rel}: Composer repository provenance is unknown")
            parsed = manifests.parse_manifest(rel, text) if manifests.is_manifest(rel) else None
            if parsed is not None:
                manifests_found.append((rel, parsed))
                details = {
                    "file": rel,
                    "ecosystem": parsed["ecosystem"],
                    "declarations": _declared_dependencies(rel, text),
                    "licenses": _manifest_licenses(rel, text),
                }
                manifest_details.append(details)
                if parsed["own_lock"]:
                    locks.append({"file": rel, "ecosystem": parsed["ecosystem"],
                                  "format": "pinned-requirements",
                                  "entries": len(parsed["dependencies"]),
                                  "packages": _pinned_requirements(text)})
            elif manifests.is_manifest(rel):
                # A recognized filename that cannot be parsed is still an
                # uncovered manifest; silently dropping it made inventory
                # completeness look stronger than it was.
                omissions.append(f"{rel}: manifest is invalid or unsupported")
            lock = _lock_inventory(rel, text)
            if lock is not None:
                locks.append(lock)
                omissions.extend(lock.get("omissions") or [])
            elif name in {n for names in _LOCK_NAMES.values() for n in names}:
                omissions.append(f"{rel}: lockfile format is invalid or unsupported")
        if time.monotonic() >= deadline:
            omissions.append("time budget exhausted")
            break
    if files_seen >= MAX_FILES:
        omissions.append("file-count limit reached")
    if time.monotonic() >= deadline:
        omissions.append("time budget exhausted")

    inventories = []
    findings = []
    lockfiles = sorted(locks, key=lambda item: item["file"])
    composer_config_files = sorted(composer_repository_configs)
    for lock in lockfiles:
        if lock.get("ecosystem") != "composer":
            continue
        lock_parent = PurePosixPath(lock["file"]).parent
        matching_configs = []
        while True:
            matching_configs = [path for path in composer_config_files
                                if PurePosixPath(path).parent == lock_parent]
            if matching_configs or lock_parent in (PurePosixPath("."),
                                                    PurePosixPath("")):
                break
            lock_parent = lock_parent.parent
        config_path = matching_configs[0] if matching_configs else None
        repository_state = (composer_repository_configs.get(config_path)
                            if config_path is not None else None)
        has_custom_repositories = repository_state is True
        lock["custom_repositories"] = has_custom_repositories
        lock["advisory_matchable"] = (repository_state is False)
        if has_custom_repositories:
            lock.setdefault("omissions", []).append(
                "custom Composer repositories make locked package provenance ambiguous")
            omissions.append(
                f"{lock['file']}: custom Composer repositories make package provenance ambiguous")
        elif repository_state is None:
            lock.setdefault("omissions", []).append(
                "composer.json is missing or invalid; package provenance is unknown")
            omissions.append(
                f"{lock['file']}: composer.json is missing or invalid; package provenance is unknown")
    for rel, parsed in sorted(manifests_found):
        ecosystem = parsed["ecosystem"]
        deps = parsed["dependencies"]
        lock_path = rel if parsed["own_lock"] else _find_lock(rel, ecosystem, lockfiles)
        names = sorted({str(dep["name"]) for dep in deps})
        details = next((item for item in manifest_details if item["file"] == rel), {})
        row = {"file": rel, "ecosystem": ecosystem,
               "dependency_count": len(deps),
               "dependencies": names[:MAX_ITEMS],
               "dependencies_truncated": len(names) > MAX_ITEMS,
               "lockfile": lock_path}
        if details.get("licenses"):
            row["licenses"] = list(details["licenses"])
        inventories.append(row)
        if deps and not lock_path:
            findings.append({"section": "dependencies", "type": "missing_lock_file",
                "severity": "minor", "keys": [f"file:{rel}"],
                "summary": f"{rel} declares {len(deps)} {ecosystem} dependencies and no recognized lockfile was found.",
                "recommendation": "Review whether this project should commit a lockfile for reproducible installs."})
    if not manifests_found and not omissions:
        omissions.append("no supported dependency manifests found")

    conflicts, duplicates = _version_facts(manifest_details, lockfiles)
    for conflict in conflicts:
        versions = ", ".join(conflict["versions"])
        sources = ", ".join(conflict["sources"])
        source_paths = conflict["sources"]
        findings.append({
            "section": "dependencies",
            "type": "conflicting_dependency_versions",
            "severity": "note",
            "confidence": "high",
            "keys": [f"dependency:{conflict['ecosystem']}:{conflict['dependency']}"],
            "locations": [{"path": path} for path in
                          source_paths[:MAX_CONFLICT_LOCATIONS]],
            **({"scope": (f"Conflict spans {len(source_paths)} inventory files; "
                            f"only the first {MAX_CONFLICT_LOCATIONS} locations are listed.")}
               if len(source_paths) > MAX_CONFLICT_LOCATIONS else {}),
            "summary": (f"Dependency {conflict['dependency']} has {len(conflict['versions'])} "
                        f"exact versions in the inventory ({versions}); sources: {sources}."),
            "recommendation": ("Confirm whether the duplicate versions are intentional "
                               "and whether each consumer resolves the expected one."),
        })
    for duplicate in duplicates:
        constraints = ", ".join(duplicate["constraints"])
        sources = ", ".join(duplicate["sources"])
        source_paths = duplicate["sources"]
        findings.append({
            "section": "dependencies",
            "type": "duplicate_dependency_declaration",
            "severity": "note",
            "confidence": "high",
            "keys": [f"dependency:{duplicate['ecosystem']}:{duplicate['dependency']}"],
            "locations": [{"path": path} for path in
                          source_paths[:MAX_CONFLICT_LOCATIONS]],
            **({"scope": (f"Declaration spans {len(source_paths)} manifest files; "
                            f"only the first {MAX_CONFLICT_LOCATIONS} locations are listed.")}
               if len(source_paths) > MAX_CONFLICT_LOCATIONS else {}),
            "summary": (f"Dependency {duplicate['dependency']} is declared in "
                        f"{len(duplicate['sources'])} manifests ({sources}) "
                        f"with constraints: {constraints}."),
            "recommendation": ("Check that repeated declarations use a consistent "
                               "version policy, especially in monorepos."),
        })
    declared_licenses = []
    for detail in sorted(manifest_details, key=lambda item: item["file"]):
        for license_text in detail.get("licenses") or []:
            declared_licenses.append({"file": detail["file"],
                                      "license": license_text})
    dependency_licenses = []
    for lock in lockfiles:
        for row in lock.get("packages") or []:
            if isinstance(row, dict) and row.get("license"):
                dependency_licenses.append({
                    "file": lock["file"],
                    "dependency": row.get("name"),
                    "version": row.get("version"),
                    "license": row["license"],
                })
    licenses = {
        "declared": declared_licenses[:MAX_ITEMS],
        "files": sorted(license_files)[:MAX_ITEMS],
        "dependency_declarations": sorted(
            dependency_licenses,
            key=lambda item: (str(item["dependency"]), str(item["version"]),
                              str(item["file"])))[:MAX_ITEMS],
        "state": ("declared" if declared_licenses or license_files else
                  "not_declared_in_inventoried_files" if inventories else "unknown"),
        "dependency_license_assertions": "not_assessed",
    }
    if (len(declared_licenses) > MAX_ITEMS or len(license_files) > MAX_ITEMS
            or len(dependency_licenses) > MAX_ITEMS):
        omissions.append("license declaration limit reached")
    if conflicts and len(conflicts) >= MAX_CONFLICTS:
        omissions.append(f"dependency version conflict limit reached ({MAX_CONFLICTS})")
    if duplicates and len(duplicates) >= MAX_CONFLICTS:
        omissions.append(f"duplicate declaration limit reached ({MAX_CONFLICTS})")

    inventory = {"manifests": inventories[:MAX_ITEMS],
                 "manifests_truncated": len(inventories) > MAX_ITEMS,
                 "lockfiles": lockfiles[:MAX_ITEMS],
                 "lockfiles_truncated": len(lockfiles) > MAX_ITEMS,
                 "licenses": licenses,
                 "version_conflicts": conflicts,
                 "duplicate_declarations": duplicates,
                 "cve_state": CVE_STATE,
                 "files_seen": files_seen, "bytes_read": total_bytes,
                 "duration_s": round(time.monotonic() - started, 6)}
    from .local_advisories import match_local_advisories
    try:
        advisory_result = match_local_advisories(inventory, local_advisories)
    except ValueError:
        advisory_result = {
            "state": "invalid", "source_hash": None,
            "advisories_loaded": 0, "pinned_packages": 0,
            "matched_packages": 0, "findings": [], "truncated": False,
            "not_checked": "supplied local advisory data was invalid or exceeded a bound",
        }
    findings.extend(advisory_result["findings"])
    inventory["local_advisories"] = {
        key: advisory_result.get(key) for key in (
            "state", "source_hash", "advisories_loaded", "pinned_packages",
            "matched_packages", "truncated")}
    from .license_policy import evaluate_license_policy
    try:
        policy_result = evaluate_license_policy(inventory, license_policy)
    except ValueError:
        policy_result = {
            "state": "invalid", "policy_hash": None, "allowed": 0,
            "denied": 0, "unknown": 0, "results": [], "findings": [],
            "truth_assessed": False,
            "not_checked": "supplied local license policy was invalid or exceeded a bound",
        }
    findings.extend(policy_result["findings"])
    inventory["license_policy"] = {
        key: policy_result.get(key) for key in (
            "state", "policy_hash", "allowed", "denied", "unknown",
            "truncated", "truth_assessed", "not_checked")}
    if license_policy is not None:
        inventory["licenses"]["policy_evaluation"] = policy_result
        omissions.append(str(policy_result["not_checked"]))
    if inventories or lockfiles:
        omissions.append(str(advisory_result["not_checked"]))
    # Even an exact hit is only a match against the caller's local dataset;
    # absence of hits cannot establish a clean dependency set.
    inventory["cve_state"] = CVE_STATE
    sbom = build_sbom(inventory, root_name=root.name)
    return {"findings": findings, "inventory": inventory, "sbom": sbom,
            "cve_state": CVE_STATE,
            "checked": bool(manifests_found or locks),
            "not_checked": [{"reason": item} for item in sorted(set(omissions))],
            "local_advisories": inventory["local_advisories"]}


__all__ = ["CVE_STATE", "SBOM_SCHEMA", "analyze", "build_sbom"]
