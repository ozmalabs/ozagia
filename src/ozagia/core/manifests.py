"""Dependency manifests: which file declares which dependency.

Shared by the repo review (`core.repo_review`, lock files) and the
environment model (`core.environments`, declared versus imported
packages). Reads pyproject.toml (PEP 621 dependencies, optional
dependencies, dependency groups, Poetry), requirements*.txt, package.json,
Cargo.toml and go.mod. Text in, records out: nothing here touches the disk.

Each dependency says whether its declaration is optional and in which
group: an extra (`[project.optional-dependencies] plot`), a dependency
group, a Poetry group or dev dependencies, a non-required section of
package.json or Cargo.toml, or a `requirements-<group>.txt` file. A
`requirements-<group>.txt` whose group names the main set (base, prod,
runtime, ...) is not optional.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import PurePosixPath
from typing import Any, Iterable

MANIFEST_NAMES = frozenset({"pyproject.toml", "package.json", "Cargo.toml",
                            "go.mod"})
# requirements-<group>.txt groups that name the main dependency set.
REQUIRED_GROUPS = frozenset({"base", "main", "prod", "production", "core",
                             "runtime", "common", "install", "requirements"})
# Groups whose dependencies a project's tests may rely on.
DEV_GROUPS = frozenset({"dev", "devel", "develop", "development", "test",
                        "tests", "testing", "ci", "lint", "check", "checks",
                        "qa", "dev-dependencies", "dev_dependencies"})

_REQ_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GO_REQ = re.compile(r"^\s*(?:require\s+)?([a-z0-9][\w.\-]*\.[a-z]{2,}"
                     r"/[\w.\-/]+)\s+v[\w.\-+]+", re.M)


def normalize(name: str) -> str:
    """A distribution name in PEP 503 form, with `_` for the separator:
    `PyYAML` and `pyyaml` match, as do `foo-bar`, `foo.bar` and `foo_bar`."""
    return re.sub(r"[-_.]+", "_", str(name)).lower()


def is_manifest(rel: str) -> bool:
    """A file name the readers here understand."""
    name = PurePosixPath(rel).name
    return name in MANIFEST_NAMES or (name.startswith("requirements")
                                      and name.endswith(".txt"))


def requirement_names(items: Iterable[Any]) -> list[str]:
    """Distribution names of PEP 508 requirement strings (others ignored)."""
    out = []
    for item in items:
        if isinstance(item, str):
            match = _REQ_NAME.match(item)
            if match:
                out.append(match.group(1))
    return out


def requirements_group(rel: str) -> str | None:
    """The group of a requirements file: None for requirements.txt, else
    the part after `requirements-`/`requirements_` (`dev`, `arc`, ...)."""
    stem = PurePosixPath(rel).name[:-len(".txt")]
    rest = stem[len("requirements"):].lstrip("-_.")
    return rest or None


def is_dev_group(group: str | None) -> bool:
    """A development or test group (`dev`, `test`, `requirements-dev.txt`)."""
    if not group:
        return False
    words = set(re.split(r"[-_.]+", group.lower()))
    return group.lower() in DEV_GROUPS or bool(words & DEV_GROUPS)


def _dep(name: str, optional: bool = False,
         group: str | None = None) -> dict[str, Any]:
    return {"name": name, "optional": bool(optional), "group": group}


def _pyproject(text: str) -> list[dict[str, Any]]:
    data = tomllib.loads(text)
    project = data.get("project") or {}
    deps = [_dep(n) for n in requirement_names(project.get("dependencies") or [])]
    for extra, group in (project.get("optional-dependencies") or {}).items():
        deps += [_dep(n, True, str(extra)) for n in requirement_names(group or [])]
    for name, group in (data.get("dependency-groups") or {}).items():
        deps += [_dep(n, True, str(name)) for n in requirement_names(group or [])]
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for name, spec in (poetry.get("dependencies") or {}).items():
        if name == "python":
            continue
        optional = isinstance(spec, dict) and bool(spec.get("optional"))
        deps.append(_dep(name, optional, "extras" if optional else None))
    for name in (poetry.get("dev-dependencies") or {}):
        deps.append(_dep(name, True, "dev"))
    for gname, group in (poetry.get("group") or {}).items():
        if isinstance(group, dict):
            for name in (group.get("dependencies") or {}):
                deps.append(_dep(name, True, str(gname)))
    return deps


def _requirements(rel: str, text: str) -> tuple[list[dict[str, Any]], bool]:
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    reqs = [line for line in lines if line and not line.startswith(("-", "--"))]
    pinned = bool(reqs) and all("==" in line or "@" in line for line in reqs)
    group = requirements_group(rel)
    optional = group is not None and group.lower() not in REQUIRED_GROUPS
    return [_dep(n, optional, group) for n in requirement_names(reqs)], pinned


def parse_manifest(rel: str, text: str) -> dict[str, Any] | None:
    """{"file", "ecosystem", "own_lock", "dependencies": [{"name",
    "optional", "group"}]} for a manifest, or None when the file is not one
    or cannot be parsed. `own_lock` is true for a requirements file whose
    every line is pinned (it is its own lock)."""
    name = PurePosixPath(rel).name
    own_lock = False
    try:
        if name == "pyproject.toml":
            ecosystem, deps = "python", _pyproject(text)
        elif name == "package.json":
            data = json.loads(text)
            ecosystem, deps = "node", []
            for field in ("dependencies", "devDependencies",
                          "optionalDependencies", "peerDependencies"):
                if isinstance(data.get(field), dict):
                    optional = field != "dependencies"
                    deps += [_dep(k, optional, None if not optional else field)
                             for k in data[field]]
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            ecosystem, deps = "rust", []
            for field in ("dependencies", "dev-dependencies", "build-dependencies"):
                optional = field == "dev-dependencies"
                deps += [_dep(k, optional, field if optional else None)
                         for k in (data.get(field) or {})]
            deps += [_dep(k) for k in
                     ((data.get("workspace") or {}).get("dependencies") or {})]
        elif name == "go.mod":
            ecosystem, deps = "go", [_dep(n) for n in _GO_REQ.findall(text)]
        elif name.startswith("requirements") and name.endswith(".txt"):
            ecosystem = "python"
            deps, own_lock = _requirements(rel, text)
        else:
            return None
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        return None
    return {"file": rel, "ecosystem": ecosystem, "own_lock": own_lock,
            "dependencies": deps}
