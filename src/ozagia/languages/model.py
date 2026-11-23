"""Language support packages: components, probes and bounded installs.

Every language module exports one :class:`LanguageSupport`. Importing the
package only builds plain data; probing and installing happen only when a
caller asks for them.

Safety model:

- the only commands this module can run are a `uv pip install` (falling back
  to `python -m pip install`) of a package spec declared in this package, or a
  rustup component add;
- no shell, no sudo, no OS package manager, and no arbitrary command from the
  target project;
- network access and installation are explicit flags (``install`` and
  ``network``);
- a component that needs a system package or a global npm install is reported
  with the manual command, never run implicitly.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PREFIX = "[ozagia] languages:"
INSTALL_TIMEOUT_S = 600
DEFAULT_MAX_FILES = 50_000
AUTO_INSTALL_ENV = "OZAGIA_LANGUAGE_AUTOINSTALL"


@dataclass(frozen=True)
class Component:
    """One thing a language needs to be fully supported."""

    id: str
    label: str
    #: ``"python"`` probes an importable module, ``"binary"`` a PATH binary
    #: (or ``<root>/.venv/bin/<binary>`` for project-local tools).
    kind: str = "python"
    probe: str = ""
    #: ``"pip"``, ``"rustup"``, ``"npm"``, ``"system"`` or ``"none"``.
    manager: str = "none"
    #: Package/spec passed to the manager.
    package: str = ""
    #: ``ozagia[...]`` extra name that installs the component, when one exists.
    extra: str = ""
    required: bool = True
    requires_network: bool = True
    requires_sudo: bool = False
    note: str = ""

    @property
    def automatic(self) -> bool:
        """Whether ozagia can install this itself without a sudo prompt."""
        return self.manager in ("pip", "rustup")

    def install_command(self) -> list[str] | None:
        if self.manager == "pip":
            return python_install_command([self.package])
        if self.manager == "rustup":
            return ["rustup", "component", "add", self.package]
        if self.manager == "npm":
            return ["npm", "install", "-g", *self.package.split()]
        return None

    def effective_manager(self) -> str:
        """The manager actually used: uv for Python when it is installed."""
        return _manager_label(self.manager)

    def describe(self) -> str:
        bits = [self.label]
        if self.manager != "none":
            bits.append(f"via {self.effective_manager()}")
        if self.extra:
            bits.append(f"ozagia[{self.extra}]")
        if self.requires_sudo:
            bits.append("needs sudo")
        return " ".join(bits)


@dataclass(frozen=True)
class LanguageSupport:
    """One language's support package."""

    id: str
    label: str
    suffixes: tuple[str, ...]
    components: tuple[Component, ...]
    notes: str = ""

    @property
    def extra(self) -> str:
        return f"lang-{self.id}"


def _manager_label(manager: str) -> str:
    if manager == "pip" and shutil.which("uv"):
        return "uv pip"
    return manager


def python_install_command(packages: list[str]) -> list[str]:
    """Prefer uv, falling back to the active interpreter's pip."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *packages]
    return [sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", *packages]


def _default_probe(component: Component, root: Path | None) -> bool:
    if component.kind == "python":
        try:
            return importlib.util.find_spec(component.probe) is not None
        except (ImportError, ValueError):
            return False
    if shutil.which(component.probe):
        return True
    if root is not None:
        local = Path(root) / ".venv" / "bin" / component.probe
        return local.is_file()
    return False


def component_status(component: Component, *,
                     root: Path | None = None,
                     probe: Callable[[Component], bool] | None = None
                     ) -> dict[str, Any]:
    check = probe or (lambda item: _default_probe(item, root))
    installed = bool(check(component))
    return {
        "id": component.id,
        "label": component.label,
        "installed": installed,
        "required": component.required,
        "manager": component.manager,
        "effective_manager": component.effective_manager(),
        "package": component.package,
        "extra": component.extra,
        "requires_network": component.requires_network,
        "requires_sudo": component.requires_sudo,
        "note": component.note,
        "install": component.install_command(),
    }


def support_status(support: LanguageSupport, *,
                   root: Path | None = None,
                   probe: Callable[[Component], bool] | None = None
                   ) -> dict[str, Any]:
    components = [component_status(component, root=root, probe=probe)
                  for component in support.components]
    missing = [item for item in components if not item["installed"]]
    missing_required = [item for item in missing if item["required"]]
    return {
        "language": support.id,
        "label": support.label,
        "suffixes": list(support.suffixes),
        "extra": support.extra,
        "ready": not missing_required,
        "components": components,
        "missing": missing,
        "notes": support.notes,
    }


def _emit(report: Callable[[str], None] | None, text: str) -> None:
    (report or _stderr)(text)


def _stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _run(argv: list[str], runner: Callable[[list[str]], Any] | None) -> tuple[bool, str]:
    if runner is not None:
        try:
            result = runner(argv)
        except Exception as exc:  # runner is injectable in tests
            return False, f"{type(exc).__name__}: {exc}"
        code = getattr(result, "returncode", 1)
        if code == 0:
            return True, ""
        detail = (getattr(result, "stderr", "") or
                  getattr(result, "stdout", "") or "").strip()
        return False, f"exit {code}: {detail[:300]}"
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=INSTALL_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout or "").strip()
    return False, f"exit {result.returncode}: {detail[:300]}"


def ensure(support: LanguageSupport, *, root: Path | None = None,
           install: bool = False, network: bool = True,
           report: Callable[[str], None] | None = None,
           probe: Callable[[Component], bool] | None = None,
           runner: Callable[[list[str]], Any] | None = None) -> dict[str, Any]:
    """Report and (when asked) install one language's missing components.

    Returns the post-attempt status plus ``installed``/``failed``/``commands``.
    Never raises for a missing tool or a failed install.
    """
    root = Path(root) if root is not None else None
    check = probe or (lambda item: _default_probe(item, root))
    missing = [item for item in support.components if not check(item)]
    result: dict[str, Any] = {
        **support_status(support, root=root, probe=check),
        "installed": [],
        "failed": [],
        "blocked": [],
        "commands": [],
    }
    if not missing:
        return result

    automatic = [item for item in missing if item.automatic]
    manual = [item for item in missing if not item.automatic]
    _emit(report, f"{PREFIX} detected {support.label} support "
                  f"({', '.join(support.suffixes)})")

    if automatic:
        labels = "; ".join(item.describe() for item in automatic)
        needs_network = any(item.requires_network for item in automatic)
        if not install:
            _emit(report, f"{PREFIX} {support.label} is missing required "
                          f"components: {labels}. Run "
                          f"`ozagia languages ensure --root .` to install.")
        elif needs_network and not network:
            _emit(report, f"{PREFIX} cannot install required components for "
                          f"{support.label}: network access is disabled "
                          f"({labels}).")
            result["blocked"].extend(item.id for item in automatic)
        else:
            _emit(report, f"{PREFIX} installing required components for "
                          f"{support.label}: {labels}")

    if manual:
        labels = "; ".join(item.describe() for item in manual)
        _emit(report, f"{PREFIX} {support.label} also needs manual "
                      f"components: {labels}")

    if install and network:
        pip_components = [item for item in automatic if item.manager == "pip"]
        if pip_components:
            argv = python_install_command(
                [item.package for item in pip_components])
            result["commands"].append(argv)
            ok, error = _run(argv, runner)
            if ok:
                result["installed"].extend(item.id for item in pip_components)
            else:
                result["failed"].extend(item.id for item in pip_components)
                _emit(report, f"{PREFIX} could not install "
                              f"{', '.join(item.id for item in pip_components)}: "
                              f"{error}")
        for item in automatic:
            if item.manager != "rustup":
                continue
            argv = item.install_command()
            if not argv:
                continue
            result["commands"].append(argv)
            ok, error = _run(argv, runner)
            if ok:
                result["installed"].append(item.id)
            else:
                result["failed"].append(item.id)
                _emit(report, f"{PREFIX} could not install {item.id}: {error}")

    after = support_status(support, root=root, probe=check)
    result.update(after)
    if install and (result["installed"] or result["failed"]):
        if result["ready"]:
            _emit(report, f"{PREFIX} {support.label} support ready")
        else:
            still = [item["id"] for item in result["missing"]
                     if item["id"] in result["failed"] or item["required"]]
            if still:
                _emit(report, f"{PREFIX} {support.label} still missing: "
                              f"{', '.join(still)}")
    return result


def detect_languages(root: str | Path, *,
                     max_files: int = DEFAULT_MAX_FILES) -> dict[str, Any]:
    """Find the support packages for the source suffixes present under `root`."""
    from ozagia.config import GENERIC_SKIP_DIRS
    from ozagia.languages import BY_SUFFIX, SUPPORTS

    root = Path(root)
    found: dict[str, Any] = {}
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in GENERIC_SKIP_DIRS and not name.startswith(".")
        )
        for name in filenames:
            scanned += 1
            if scanned > max_files:
                truncated = True
                break
            suffix = Path(name).suffix.lower()
            support = BY_SUFFIX.get(suffix)
            if support is not None:
                found[support.id] = support
        if truncated or len(found) == len(SUPPORTS):
            break
    return {
        "root": str(root),
        "languages": [found[key] for key in sorted(found)],
        "files_scanned": scanned,
        "truncated": truncated,
    }


def ensure_for_root(root: str | Path, *, install: bool = False,
                    network: bool = True,
                    report: Callable[[str], None] | None = None,
                    probe: Callable[[Component], bool] | None = None,
                    runner: Callable[[list[str]], Any] | None = None,
                    max_files: int = DEFAULT_MAX_FILES) -> dict[str, Any]:
    """Detect the languages under `root` and ensure each support package.

    Reports only languages with missing components; a fully ready checkout is
    silent. Results are plain JSON-safe dictionaries.
    """
    detection = detect_languages(root, max_files=max_files)
    results = [
        ensure(support, root=Path(root), install=install, network=network,
               report=report, probe=probe, runner=runner)
        for support in detection["languages"]
    ]
    return {
        "ok": all(item["ready"] for item in results),
        "root": detection["root"],
        "detection": {
            "languages": [support.id
                          for support in detection["languages"]],
            "files_scanned": detection["files_scanned"],
            "truncated": detection["truncated"],
        },
        "languages": results,
    }


def registry_payload() -> list[dict[str, Any]]:
    """The support-package registry as JSON-safe data (CLI/list output)."""
    from ozagia.languages import SUPPORTS

    return [
        {
            "language": support.id,
            "label": support.label,
            "suffixes": list(support.suffixes),
            "extra": support.extra,
            "notes": support.notes,
            "components": [
                {
                    "id": item.id,
                    "label": item.label,
                    "kind": item.kind,
                    "probe": item.probe,
                    "manager": item.manager,
                    "effective_manager": item.effective_manager(),
                    "package": item.package,
                    "extra": item.extra,
                    "required": item.required,
                    "requires_network": item.requires_network,
                    "requires_sudo": item.requires_sudo,
                    "install": item.install_command(),
                }
                for item in support.components
            ],
        }
        for support in (SUPPORTS[key] for key in sorted(SUPPORTS))
    ]
