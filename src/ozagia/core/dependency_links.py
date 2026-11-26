"""Read-only dependency links between federation members.

The report matches *declared* package dependencies in one member against
package names provided by another member's own manifest. It never infers a
relationship from repository or symbol names, never resolves transitive
dependencies, and never runs a package manager. Manifest files are read from
each member's checkout root; graph snapshots are not required or modified.

Supported declarations: Python ``pyproject.toml`` (PEP 621, Poetry and
dependency groups), ``setup.cfg`` and root ``requirements*.txt``; Rust
``Cargo.toml``; Node ``package.json``; Go ``go.mod``. Other manifest formats
and lockfiles are listed as skipped rather than guessed at.
"""
from __future__ import annotations

import configparser
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.core import manifests

MAX_MANIFEST_BYTES = 1_000_000
MAX_MANIFESTS_PER_MEMBER = 16
MAX_DEPENDENCIES_PER_MEMBER = 250
MAX_LINKS = 500
MAX_SKIPPED = 24

_SUPPORTED_NAMES = ("pyproject.toml", "setup.cfg", "package.json",
                    "Cargo.toml", "go.mod")
_UNSUPPORTED_NAMES = {
    "setup.py": "executable Python setup scripts are not run",
    "Pipfile": "Pipfile declarations are not parsed",
    "composer.json": "Composer manifests are not parsed",
    "Gemfile": "Ruby manifests are not parsed",
    "pom.xml": "Maven manifests are not parsed",
    "build.gradle": "Gradle manifests are not parsed",
    "build.gradle.kts": "Gradle manifests are not parsed",
    "pubspec.yaml": "Dart manifests are not parsed",
    "mix.exs": "Elixir manifests are not parsed",
}
_LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "Cargo.lock", "go.sum", "poetry.lock", "uv.lock",
    "pdm.lock", "Pipfile.lock", "Gemfile.lock", "composer.lock",
}
_REQUIREMENT_PEP508 = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_GO_REQUIRE = re.compile(r"^(?:require\s+)?([^\s()]+)\s+(v[^\s]+)$")


def _safe_text(root: Path, rel: str) -> tuple[str | None, str | None]:
    """Read one member-local file without following symlinks or leaving it."""
    candidate = root / rel
    try:
        if candidate.is_symlink():
            return None, "symlink not followed"
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            return None, "not a regular file"
        if resolved.stat().st_size > MAX_MANIFEST_BYTES:
            return None, f"file exceeds {MAX_MANIFEST_BYTES} bytes"
        return resolved.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return None, f"cannot read: {type(exc).__name__}: {exc}"


def _find_line(text: str, needle: str, *, start: int = 1) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if number >= start and needle in line:
            return number
    return None


def _toml_key_line(text: str, key: str, section: str) -> int | None:
    current = ""
    pattern = re.compile(rf'^\s*["\']?{re.escape(key)}["\']?\s*=')
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            continue
        if current == section and pattern.match(line):
            return number
    return None


def _json_member_line(text: str, field: str, name: str) -> int | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(rf'"{re.escape(field)}"\s*:', line):
            for number in range(index + 1, len(lines) + 1):
                candidate = lines[number - 1]
                if number > index + 1 and re.match(r"^\s*}\s*,?\s*$", candidate):
                    break
                if re.search(rf'"{re.escape(name)}"\s*:', candidate):
                    return number
            return index + 1
    return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dependency(name: str, requirement: str | None, *, ecosystem: str,
                manifest: str, line: int | None, optional: bool = False,
                group: str | None = None, marker: str | None = None,
                direct: bool = False) -> dict[str, Any]:
    return {"name": name, "requirement": requirement or "", "ecosystem": ecosystem,
            "manifest": manifest, "line": line, "optional": bool(optional),
            "group": group, "marker": marker, "direct": bool(direct)}


def _python_requirement(rel: str, value: str, text: str, line: int | None, *,
                        optional: bool = False, group: str | None = None
                        ) -> dict[str, Any] | None:
    raw = value.strip()
    if not raw:
        return None
    marker = None
    # PEP 508 markers are not evaluated; keep the declaration and mark it.
    if ";" in raw:
        raw, _, marker = raw.partition(";")
        raw = raw.strip()
        marker = marker.strip() or None
    match = _REQUIREMENT_PEP508.match(raw)
    if not match:
        return None
    name = match.group(1)
    rest = match.group(2).strip()
    direct = rest.startswith("@") or rest.startswith(("git+", "http://",
                                                      "https://", "file:"))
    if rest.startswith("@"):
        rest = rest[1:].strip()
    if line is None:
        line = _find_line(text, value.strip())
    return _dependency(name, rest, ecosystem="python", manifest=rel, line=line,
                       optional=optional, group=group, marker=marker,
                       direct=direct)


def _pyproject(rel: str, text: str) -> tuple[list[dict[str, Any]],
                                            list[dict[str, Any]], list[str]]:
    data = tomllib.loads(text)
    if not isinstance(data, Mapping):
        return [], [], ["pyproject.toml is not a TOML table"]
    providers: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    project = data.get("project") if isinstance(data.get("project"), Mapping) else {}
    name = _text(project.get("name"))
    version = _text(project.get("version"))
    if name:
        providers.append({"name": name, "version": version,
                          "manifest": rel,
                          "line": _toml_key_line(text, "name", "project")})
    project_dependencies = project.get("dependencies")
    if isinstance(project_dependencies, list):
        for value in project_dependencies:
            if isinstance(value, str):
                dep = _python_requirement(rel, value, text, None)
                if dep:
                    dependencies.append(dep)
    optional_deps = project.get("optional-dependencies") or {}
    if isinstance(optional_deps, Mapping):
        for extra, values in optional_deps.items():
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str):
                    dep = _python_requirement(rel, value, text, None,
                                              optional=True, group=str(extra))
                    if dep:
                        dependencies.append(dep)
    groups = data.get("dependency-groups") or {}
    if isinstance(groups, Mapping):
        for group, values in groups.items():
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str):
                    dep = _python_requirement(rel, value, text, None,
                                              optional=True, group=str(group))
                    if dep:
                        dependencies.append(dep)

    poetry = ((data.get("tool") or {}).get("poetry")
              if isinstance(data.get("tool"), Mapping) else None)
    if isinstance(poetry, Mapping):
        if not providers:
            poetry_name = _text(poetry.get("name"))
            if poetry_name:
                providers.append({
                    "name": poetry_name, "version": _text(poetry.get("version")),
                    "manifest": rel,
                    "line": _toml_key_line(text, "name", "tool.poetry")})
        sections = [("tool.poetry.dependencies", poetry.get("dependencies"))]
        sections.append(("tool.poetry.dev-dependencies",
                         poetry.get("dev-dependencies")))
        poetry_groups = poetry.get("group") or {}
        if isinstance(poetry_groups, Mapping):
            for group_name, group in poetry_groups.items():
                if isinstance(group, Mapping):
                    sections.append((f"tool.poetry.group.{group_name}.dependencies",
                                     group.get("dependencies")))
        for section, values in sections:
            if not isinstance(values, Mapping):
                continue
            group = "dev" if section.endswith("dev-dependencies") else None
            if ".group." in section:
                group = section.split(".")[-2]
            for dep_name, spec in values.items():
                if dep_name == "python":
                    continue
                requirement = spec if isinstance(spec, str) else None
                direct = False
                optional = group is not None
                if isinstance(spec, Mapping):
                    requirement = _text(spec.get("version"))
                    direct = not requirement
                    optional = optional or bool(spec.get("optional"))
                    if not requirement:
                        requirement = _text(spec.get("git")) or _text(spec.get("path")) \
                            or _text(spec.get("url"))
                dependencies.append(_dependency(
                    str(dep_name), requirement, ecosystem="python", manifest=rel,
                    line=_toml_key_line(text, str(dep_name), section),
                    optional=optional,
                    group=group or ("extras" if optional else None),
                    direct=direct))
    return providers, dependencies, []


def _requirements(rel: str, text: str) -> list[dict[str, Any]]:
    group = manifests.requirements_group(rel)
    optional = bool(group and group.lower() not in manifests.REQUIRED_GROUPS)
    dependencies: list[dict[str, Any]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        line = line.split("--hash", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        dep = _python_requirement(rel, line, text, number,
                                  optional=optional, group=group)
        if dep:
            dependencies.append(dep)
    return dependencies


def _setup_cfg(rel: str, text: str) -> tuple[list[dict[str, Any]],
                                             list[dict[str, Any]], list[str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    providers: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    notes: list[str] = []
    if parser.has_section("metadata"):
        name = _text(parser.get("metadata", "name", fallback=None))
        version = _text(parser.get("metadata", "version", fallback=None))
        if name:
            providers.append({"name": name, "version": version,
                              "manifest": rel,
                              "line": _find_line(text, "name")})
    if parser.has_section("options"):
        values = parser.get("options", "install_requires", fallback="")
        for value in values.splitlines():
            value = value.split("#", 1)[0].strip()
            if not value:
                continue
            if value.startswith(("file:", "-")):
                notes.append("setup.cfg install_requires uses an unsupported "
                             "file/directive reference")
                continue
            dep = _python_requirement(rel, value, text, None)
            if dep:
                dependencies.append(dep)
    if parser.has_section("options.extras_require"):
        for extra, values in parser.items("options.extras_require"):
            for value in values.splitlines():
                value = value.split("#", 1)[0].strip()
                if not value or value.startswith(("file:", "-")):
                    continue
                dep = _python_requirement(rel, value, text, None,
                                          optional=True, group=extra)
                if dep:
                    dependencies.append(dep)
    return providers, dependencies, notes


def _node(rel: str, text: str) -> tuple[list[dict[str, Any]],
                                       list[dict[str, Any]], list[str]]:
    data = json.loads(text)
    if not isinstance(data, Mapping):
        return [], [], ["package.json is not a JSON object"]
    providers: list[dict[str, Any]] = []
    name = _text(data.get("name"))
    if name:
        providers.append({"name": name, "version": _text(data.get("version")),
                          "manifest": rel, "line": _find_line(text, '"name"')})
    dependencies: list[dict[str, Any]] = []
    for field in ("dependencies", "devDependencies", "optionalDependencies",
                  "peerDependencies"):
        section = data.get(field)
        if not isinstance(section, Mapping):
            continue
        for dep_name, spec in section.items():
            if not isinstance(spec, str):
                continue
            optional = field != "dependencies"
            dependencies.append(_dependency(
                str(dep_name), spec, ecosystem="node", manifest=rel,
                line=_json_member_line(text, field, str(dep_name)),
                optional=optional, group=field))
    return providers, dependencies, []


def _cargo_version(data: Mapping[str, Any]) -> str | None:
    package = data.get("package")
    if not isinstance(package, Mapping):
        return None
    value = package.get("version")
    if isinstance(value, Mapping) and value.get("workspace"):
        workspace = data.get("workspace")
        if isinstance(workspace, Mapping):
            workspace_package = workspace.get("package")
            if isinstance(workspace_package, Mapping):
                value = workspace_package.get("version")
    return _text(value)


def _cargo(rel: str, text: str) -> tuple[list[dict[str, Any]],
                                         list[dict[str, Any]], list[str]]:
    data = tomllib.loads(text)
    if not isinstance(data, Mapping):
        return [], [], ["Cargo.toml is not a TOML table"]
    providers: list[dict[str, Any]] = []
    package = data.get("package")
    if isinstance(package, Mapping):
        name = _text(package.get("name"))
        if name:
            providers.append({"name": name, "version": _cargo_version(data),
                              "manifest": rel,
                              "line": _toml_key_line(text, "name", "package")})
    dependencies: list[dict[str, Any]] = []
    sections: list[tuple[str, Any]] = [
        ("dependencies", data.get("dependencies")),
        ("dev-dependencies", data.get("dev-dependencies")),
        ("build-dependencies", data.get("build-dependencies")),
        ("workspace.dependencies",
         (data.get("workspace") or {}).get("dependencies")
         if isinstance(data.get("workspace"), Mapping) else None),
    ]
    targets = data.get("target")
    if isinstance(targets, Mapping):
        for target_name, target in targets.items():
            if not isinstance(target, Mapping):
                continue
            for field in ("dependencies", "dev-dependencies", "build-dependencies"):
                sections.append((f"target.{target_name}.{field}", target.get(field)))
    for section, values in sections:
        if not isinstance(values, Mapping):
            continue
        group = section.split(".")[-1]
        for dep_name, spec in values.items():
            requirement = spec if isinstance(spec, str) else None
            direct = False
            optional = section.endswith("dev-dependencies")
            package_name = str(dep_name)
            if isinstance(spec, Mapping):
                package_name = _text(spec.get("package")) or package_name
                requirement = _text(spec.get("version"))
                direct = not requirement
                optional = bool(spec.get("optional"))
                if not requirement:
                    requirement = (_text(spec.get("path"))
                                   or _text(spec.get("git"))
                                   or ("workspace" if spec.get("workspace") else None))
            dependencies.append(_dependency(
                package_name, requirement, ecosystem="rust", manifest=rel,
                line=_toml_key_line(text, str(dep_name), section),
                optional=optional, group=group, direct=direct))
    return providers, dependencies, []


def _go(rel: str, text: str) -> tuple[list[dict[str, Any]],
                                      list[dict[str, Any]], list[str]]:
    providers: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    in_require = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("module "):
            value = line[len("module "):].strip()
            if value:
                providers.append({"name": value, "version": None,
                                  "manifest": rel, "line": number})
            continue
        if re.match(r"^require\s*\($", line):
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        if not in_require and not line.startswith("require "):
            continue
        match = _GO_REQUIRE.match(line)
        if match:
            dependencies.append(_dependency(
                match.group(1), match.group(2), ecosystem="go", manifest=rel,
                line=number, group="indirect" if "// indirect" in raw else None))
    return providers, dependencies, []


def _collect_member(root: Path) -> dict[str, Any]:
    manifests_out: list[dict[str, Any]] = []
    all_dependencies: list[dict[str, Any]] = []
    notes: list[str] = []
    skipped: list[dict[str, str]] = []
    candidates = list(_SUPPORTED_NAMES)
    try:
        requirements = sorted((path for path in root.iterdir()
                               if path.is_file() and not path.is_symlink()
                               and path.name.startswith("requirements")
                               and path.name.endswith(".txt")),
                              key=lambda path: path.name.casefold())
    except OSError as exc:
        requirements = []
        notes.append(f"cannot list checkout root: {type(exc).__name__}: {exc}")
    candidates.extend(path.name for path in requirements)
    if len(candidates) > MAX_MANIFESTS_PER_MEMBER:
        notes.append(f"more than {MAX_MANIFESTS_PER_MEMBER} manifests; "
                     "later files were not read")
        candidates = candidates[:MAX_MANIFESTS_PER_MEMBER]
    for rel in candidates:
        if rel not in _SUPPORTED_NAMES and not (
                rel.startswith("requirements") and rel.endswith(".txt")):
            continue
        text, error = _safe_text(root, rel)
        if error == "missing":
            continue
        record: dict[str, Any] = {"path": rel, "state": "parsed",
                                  "providers": [], "dependencies": 0}
        if error:
            record.update({"state": "error", "error": error})
            manifests_out.append(record)
            notes.append(f"{rel}: {error}")
            continue
        try:
            if rel == "pyproject.toml":
                ecosystem = "python"
                providers, dependencies, parser_notes = _pyproject(rel, text)
            elif rel == "setup.cfg":
                ecosystem = "python"
                providers, dependencies, parser_notes = _setup_cfg(rel, text)
            elif rel.startswith("requirements"):
                ecosystem = "python"
                providers, dependencies, parser_notes = [], _requirements(rel, text), []
            elif rel == "package.json":
                ecosystem = "node"
                providers, dependencies, parser_notes = _node(rel, text)
            elif rel == "Cargo.toml":
                ecosystem = "rust"
                providers, dependencies, parser_notes = _cargo(rel, text)
            else:
                ecosystem = "go"
                providers, dependencies, parser_notes = _go(rel, text)
        except (ValueError, tomllib.TOMLDecodeError, configparser.Error,
                AttributeError, TypeError, KeyError) as exc:
            record.update({"state": "error",
                           "error": f"cannot parse: {type(exc).__name__}: {exc}"})
            manifests_out.append(record)
            notes.append(f"{rel}: {record['error']}")
            continue
        truncated = len(dependencies) > MAX_DEPENDENCIES_PER_MEMBER
        dependencies = dependencies[:MAX_DEPENDENCIES_PER_MEMBER]
        all_dependencies.extend(dependencies)
        record.update({
            "ecosystem": ecosystem,
            "providers": providers,
            "dependencies": len(dependencies),
            "truncated": truncated,
        })
        manifests_out.append(record)
        notes.extend(parser_notes)
        if truncated:
            notes.append(f"{rel}: dependency list truncated at "
                         f"{MAX_DEPENDENCIES_PER_MEMBER} entries")
    for name, reason in _UNSUPPORTED_NAMES.items():
        if (root / name).is_file():
            skipped.append({"path": name, "state": "unsupported", "reason": reason})
    for name in sorted(_LOCKFILES):
        if (root / name).is_file():
            skipped.append({
                "path": name, "state": "skipped",
                "reason": "lockfile is not used for declared requirement comparison",
            })
    return {"manifests": manifests_out, "dependencies": all_dependencies,
            "notes": notes, "skipped": skipped[:MAX_SKIPPED],
            "skipped_truncated": len(skipped) > MAX_SKIPPED}


def _normalized(ecosystem: str, name: str) -> str:
    if ecosystem == "python":
        return manifests.normalize(name)
    if ecosystem in {"node", "rust"}:
        return name.strip().casefold()
    return name.strip()


def _release(value: str) -> tuple[int, ...] | None:
    text = str(value).strip().lower()
    text = text.split("+", 1)[0]
    text = text.lstrip("v")
    if not re.fullmatch(r"\d+(?:\.\d+)*", text):
        return None
    return tuple(int(part) for part in text.split("."))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    a = left + (0,) * (size - len(left))
    b = right + (0,) * (size - len(right))
    return (a > b) - (a < b)


def _python_satisfies(requirement: str, provided: str
                      ) -> tuple[bool | None, str]:
    spec = (requirement or "").strip()
    if not spec:
        return None, "requirement_has_no_version_constraint"
    if spec.startswith("@") or "://" in spec or spec.startswith(
            ("git+", "file:", "./", "../", "/")):
        return None, "direct_reference_requirement"
    if spec.lower() in {"*", "latest", "x"}:
        return None, "requirement_has_no_version_constraint"
    clauses = [part.strip() for part in spec.split(",") if part.strip()]
    if not clauses:
        return None, "requirement_has_no_version_constraint"
    version = _release(provided)
    if version is None:
        return None, "provided_version_unparseable"
    for clause in clauses:
        if clause.startswith("==="):
            if str(provided).strip() != clause[3:].strip():
                return False, "provided_version_does_not_match_exact"
            continue
        match = re.match(r"^(~=|==|!=|>=|<=|>|<)\s*(\S+)$", clause)
        if not match:
            return None, "unsupported_requirement"
        operator, raw = match.groups()
        if raw.endswith(".*"):
            if operator not in {"==", "!="}:
                return None, "unsupported_wildcard_requirement"
            prefix = _release(raw[:-2])
            if prefix is None:
                return None, "requirement_version_unparseable"
            padded = (version + (0,) * max(0, len(prefix) - len(version)))
            same = padded[:len(prefix)] == prefix
            if operator == "==" and not same:
                return False, "provided_version_does_not_match_requirement"
            if operator == "!=" and same:
                return False, "provided_version_does_not_match_requirement"
            continue
        target = _release(raw)
        if target is None:
            return None, "requirement_version_unparseable"
        relation = _compare(version, target)
        if operator == "==" and relation != 0:
            return False, "provided_version_does_not_match_requirement"
        if operator == "!=" and relation == 0:
            return False, "provided_version_does_not_match_requirement"
        if operator == ">=" and relation < 0:
            return False, "provided_version_does_not_match_requirement"
        if operator == ">" and relation <= 0:
            return False, "provided_version_does_not_match_requirement"
        if operator == "<=" and relation > 0:
            return False, "provided_version_does_not_match_requirement"
        if operator == "<" and relation >= 0:
            return False, "provided_version_does_not_match_requirement"
        if operator == "~=":
            if len(target) < 2:
                return None, "compatible_release_requires_two_components"
            if relation < 0:
                return False, "provided_version_does_not_match_requirement"
            upper = list(target[:-1])
            upper[-1] += 1
            if _compare(version, tuple(upper)) >= 0:
                return False, "provided_version_does_not_match_requirement"
    return True, "requirement_satisfied"


def _semver(value: str) -> tuple[int, int, int] | None:
    text = str(value).strip().lower().lstrip("v")
    text = text.split("+", 1)[0]
    if "-" in text or not re.fullmatch(r"\d+(?:\.\d+){0,2}", text):
        return None
    parts = [int(part) for part in text.split(".")]
    parts += [0] * (3 - len(parts))
    return tuple(parts[:3])


def _semver_parts(value: str) -> tuple[tuple[int, int, int], int] | None:
    text = str(value).strip().lower().lstrip("v")
    text = text.split("+", 1)[0]
    if "-" in text or not re.fullmatch(r"\d+(?:\.\d+){0,2}", text):
        return None
    parts = [int(part) for part in text.split(".")]
    count = len(parts)
    parts += [0] * (3 - len(parts))
    return tuple(parts[:3]), count


def _caret_upper(parts: tuple[int, int, int],
                 count: int) -> tuple[int, int, int]:
    major, minor, patch = parts
    if count == 1:
        return major + 1, 0, 0
    if count == 2 and not major:
        return 0, minor + 1, 0
    if major:
        return major + 1, 0, 0
    if minor:
        return 0, minor + 1, 0
    return 0, 0, patch + 1


def _tilde_upper(parts: tuple[int, int, int],
                 count: int) -> tuple[int, int, int]:
    if count >= 2:
        return parts[0], parts[1] + 1, 0
    return parts[0] + 1, 0, 0


def _range_upper(parts: tuple[int, int, int],
                 count: int) -> tuple[int, int, int]:
    if count >= 3:
        return parts[0], parts[1], parts[2] + 1
    if count == 2:
        return parts[0], parts[1] + 1, 0
    return parts[0] + 1, 0, 0


def _semver_satisfies(requirement: str, provided: str, ecosystem: str
                      ) -> tuple[bool | None, str]:
    spec = (requirement or "").strip()
    if not spec:
        return None, "requirement_has_no_version_constraint"
    if spec.lower() in {"*", "latest", "x"}:
        return None, "requirement_has_no_version_constraint"
    if "||" in spec or " - " in spec or "://" in spec or spec.startswith(
            ("workspace:", "file:", "link:", "git+", "git@", "npm:",
             "./", "../", "/")):
        return None, "unsupported_requirement_form"
    version = _semver(provided)
    if version is None:
        return None, "provided_version_unparseable"
    spec = re.sub(r"([<>=~^]+)\s+", r"\1", spec)
    clauses = [part for part in re.split(r"[,\s]+", spec) if part]
    for clause in clauses:
        match = re.match(r"^(\^|~|>=|<=|>|<|=)?\s*(.+)$", clause)
        if not match:
            return None, "unsupported_requirement"
        operator, raw = match.groups()
        if "x" in raw.lower() or "*" in raw:
            if operator not in {None, "="}:
                return None, "unsupported_wildcard_requirement"
            components = raw.split(".")
            wildcard_at = next(
                (index for index, part in enumerate(components)
                 if "x" in part.lower() or "*" in part), len(components))
            wildcard = re.sub(r"[xX*]", "0", raw)
            parsed = _semver_parts(wildcard)
            if parsed is None or wildcard_at == 0:
                return None, "requirement_version_unparseable"
            parts, _count = parsed
            if _compare(version, parts) < 0 or _compare(
                    version, _range_upper(parts, wildcard_at)) >= 0:
                return False, "provided_version_does_not_match_requirement"
            continue
        parsed = _semver_parts(raw)
        if parsed is None:
            return None, "requirement_version_unparseable"
        parts, count = parsed
        if operator == "^":
            if (_compare(version, parts) < 0
                    or _compare(version, _caret_upper(parts, count)) >= 0):
                return False, "provided_version_does_not_match_requirement"
        elif operator == "~":
            if (_compare(version, parts) < 0
                    or _compare(version, _tilde_upper(parts, count)) >= 0):
                return False, "provided_version_does_not_match_requirement"
        elif operator == "=":
            if _compare(version, parts) != 0:
                return False, "provided_version_does_not_match_requirement"
        elif operator:
            relation = _compare(version, parts)
            if operator == ">=" and relation < 0:
                return False, "provided_version_does_not_match_requirement"
            if operator == ">" and relation <= 0:
                return False, "provided_version_does_not_match_requirement"
            if operator == "<=" and relation > 0:
                return False, "provided_version_does_not_match_requirement"
            if operator == "<" and relation >= 0:
                return False, "provided_version_does_not_match_requirement"
        elif ecosystem == "rust":
            if (_compare(version, parts) < 0
                    or _compare(version, _caret_upper(parts, count)) >= 0):
                return False, "provided_version_does_not_match_requirement"
        elif count >= 3:
            if _compare(version, parts) != 0:
                return False, "provided_version_does_not_match_requirement"
        else:
            if (_compare(version, parts) < 0
                    or _compare(version, _range_upper(parts, count)) >= 0):
                return False, "provided_version_does_not_match_requirement"
    return True, "requirement_satisfied"


def _go_satisfies(requirement: str, provided: str
                  ) -> tuple[bool | None, str]:
    required = _release(requirement)
    version = _release(provided)
    if required is None:
        return None, "requirement_version_unparseable"
    if version is None:
        return None, "provided_version_unparseable"
    if _compare(version, required) < 0:
        return False, "provided_version_does_not_match_requirement"
    return True, "requirement_satisfied"


def _version_check(dependency: Mapping[str, Any], provided: str | None
                   ) -> tuple[bool | None, str]:
    if dependency.get("marker"):
        return None, "environment_marker_not_evaluated"
    if dependency.get("direct"):
        return None, "direct_reference_requirement"
    if not provided:
        return None, "provided_version_unavailable"
    ecosystem = dependency["ecosystem"]
    requirement = str(dependency.get("requirement") or "")
    if ecosystem == "python":
        return _python_satisfies(requirement, provided)
    if ecosystem in {"node", "rust"}:
        return _semver_satisfies(requirement, provided, ecosystem)
    if ecosystem == "go":
        return _go_satisfies(requirement, provided)
    return None, "unsupported_ecosystem"


def _provider_candidates(providers: Iterable[Mapping[str, Any]]
                         ) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for provider in providers:
        key = (provider.get("member"), provider.get("version"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"member": provider.get("member"),
                    "version": provider.get("version"),
                    "manifest": provider.get("manifest"),
                    "line": provider.get("line")})
    return out


def _link(dependency: Mapping[str, Any], providers: list[Mapping[str, Any]]
          ) -> dict[str, Any]:
    candidates = _provider_candidates(providers)
    base = {
        "consumer": dependency["consumer"],
        "provider": None,
        "package": dependency["name"],
        "ecosystem": dependency["ecosystem"],
        "requirement": dependency.get("requirement") or "",
        "provided_version": None,
        "source_manifest": dependency.get("manifest"),
        "source_line": dependency.get("line"),
        "provider_manifest": None,
        "provider_line": None,
        "provenance": "declared_dependency",
        "optional": bool(dependency.get("optional")),
        "group": dependency.get("group"),
        "member_states": dependency.get("member_states"),
    }
    if not candidates:
        base.update({"status": "unresolved",
                     "reason": "no_member_provides_package"})
        return base
    members = {item.get("member") for item in candidates}
    versions = {item.get("version") for item in candidates}
    if len(members) > 1:
        base.update({"status": "ambiguous",
                     "reason": "multiple_members_provide_package",
                     "candidates": candidates})
        return base
    if len(versions) > 1:
        base.update({"status": "ambiguous",
                     "reason": "multiple_versions_provided_by_member",
                     "candidates": candidates})
        return base
    provider = candidates[0]
    base.update({"provider": provider.get("member"),
                 "provided_version": provider.get("version"),
                 "provider_manifest": provider.get("manifest"),
                 "provider_line": provider.get("line")})
    result, reason = _version_check(dependency, provider.get("version"))
    if result is None:
        base.update({"status": "unknown", "reason": reason})
    elif result:
        base.update({"status": "satisfied", "reason": reason})
    else:
        base.update({"status": "mismatch", "reason": reason})
    return base


def analyze(members: Iterable[dict[str, Any]],
            member_states: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build the declared-dependency report for federation member records.

    ``members`` are the records returned by ``federation._members``. The
    caller owns freshness evaluation and can pass ``member_states`` to retain
    it in the report. Nothing is written and no snapshot is required.
    """
    states = member_states or {}
    member_reports: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    providers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    notes: list[str] = []
    skipped: list[dict[str, str]] = []
    for item in members:
        alias = item["alias"]
        root = Path(item["root"])
        collected = _collect_member(root)
        member_state = states.get(alias)
        if member_state is None and item.get("error"):
            member_state = "missing" if item["error"] == "missing snapshot" else "invalid"
        for manifest in collected["manifests"]:
            for provider in manifest["providers"]:
                provider = dict(provider)
                provider.update({"member": alias, "root": str(root),
                                 "ecosystem": manifest.get("ecosystem")})
                key = (str(provider["ecosystem"]),
                       _normalized(str(provider["ecosystem"]),
                                   str(provider["name"])))
                providers.setdefault(key, []).append(provider)
        # Dependencies are kept separately to avoid a second file read.
        for dependency in collected.get("dependencies", []):
            dependency = dict(dependency)
            dependency.update({"consumer": alias, "member_states": {
                "consumer": member_state,
            }})
            dependencies.append(dependency)
        member_reports.append({
            "id": alias,
            "root": str(root),
            "state": member_state,
            "identity_source": item.get("identity_source", "derived"),
            "manifests": collected["manifests"],
            "skipped": collected["skipped"],
            "notes": collected["notes"],
        })
        if item.get("identity_error"):
            member_reports[-1]["identity_error"] = item["identity_error"]
        notes.extend(f"{alias}: {note}" for note in collected["notes"])
        skipped.extend(collected["skipped"])
    links: list[dict[str, Any]] = []
    for dependency in dependencies:
        key = (str(dependency["ecosystem"]),
               _normalized(str(dependency["ecosystem"]), str(dependency["name"])))
        candidates = [provider for provider in providers.get(key, [])
                      if provider.get("member") != dependency["consumer"]]
        link = _link(dependency, candidates)
        consumer_state = dependency.get("member_states", {}).get("consumer")
        if link.get("provider"):
            link["member_states"] = {
                "consumer": consumer_state,
                "provider": states.get(str(link["provider"])),
            }
        links.append(link)
    ordered = sorted(links, key=lambda link: (
        {"ambiguous": 0, "mismatch": 1, "unknown": 2, "satisfied": 3,
         "unresolved": 4}.get(link.get("status"), 5),
        str(link.get("consumer")), str(link.get("package"))))
    truncated = len(ordered) > MAX_LINKS
    ordered = ordered[:MAX_LINKS]
    counts = {status: sum(link.get("status") == status for link in links)
              for status in ("satisfied", "mismatch", "unknown", "ambiguous",
                             "unresolved")}
    if not any(report["manifests"] for report in member_reports):
        state = "absent"
    elif any(manifest["state"] == "error"
             for report in member_reports for manifest in report["manifests"]):
        state = "partial"
    elif truncated or any(manifest.get("truncated")
                          for report in member_reports
                          for manifest in report["manifests"]):
        state = "partial"
    else:
        state = "ok"
    return {
        "state": state,
        "members": member_reports,
        "links": ordered,
        "counts": counts,
        "ambiguities": [link for link in ordered
                        if link.get("status") == "ambiguous"],
        "truncated": truncated,
        "skipped": skipped[:MAX_SKIPPED],
        "skipped_truncated": len(skipped) > MAX_SKIPPED,
        "notes": notes,
        "note": ("Static declared-manifest comparison only. Direct dependencies "
                 "are matched by package name within one ecosystem; transitive "
                 "dependencies, symbol names, repository names and unparsed "
                 "formats are not used."),
    }


def check(root: str | Path | None = None,
          roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Read-only dependency-link report over selected federation members."""
    from ozagia.core import federation

    selected = federation._root(root)
    if roots is not None:
        roots = tuple(roots)
    # The standalone report is cache-only; callers that want reference
    # synchronization use federation/cross-repo check explicitly.
    with federation.without_reference_sync():
        members = federation._members(selected, roots)
    states = {item["alias"]: federation._freshness(item)[0] for item in members}
    return analyze(members, states)


__all__ = ["analyze", "check"]
