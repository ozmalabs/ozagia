"""Which external tools a task needs, what is installed, and how it is approved.

This module answers a question a model should not have to guess before it
starts work: for a capability such as PDF editing, which external tools are
already installed, which are missing, and what would installing one involve
on this host? It is discovery and planning only:

- it never installs anything and never runs an install command;
- it makes no network calls;
- the only commands it runs are bounded ``--version`` probes (2 s) and the
  sudo usability check (``sudo -n true``, 2 s);
- the core is standard-library only and a missing tool is a report, not an
  error.

The registry
------------
The capability and tool registry is versioned data (``REGISTRY_VERSION``) in
the style of :mod:`ozagia.core.library_hints`: a tool entry names its stable
id, title, kind (``binary`` or ``python``), probe names, the bounded
capabilities it provides, whether it can write files, and per-package-manager
install metadata. Capability entries add a title and the file extensions that
infer them. Unknown extensions are never guessed.

Detection and approval
----------------------
:func:`detect_environment` reports the OS, distribution, Python, available
package managers, sudo usability and the ``[tools]`` approval settings. The
approval defaults are all false; ``ozagia/config.toml`` (or the legacy config
file, read through :func:`ozagia.config.load_config_section`) can set
``allow_install``, ``allow_network``, ``allow_sudo``, ``probe_versions``,
``probe_sudo``, ``preferred`` and ``brief``. ``OZAGIA_ALLOW_INSTALL``,
``OZAGIA_ALLOW_NETWORK`` and ``OZAGIA_ALLOW_SUDO`` override them when set to
``1``, ``true`` or ``yes``. ``brief = false`` turns off the session brief's
``[ozagia] tools:`` block (:func:`brief_enabled`).

``sudo -n true`` is only a non-interactive usability check. It can update the
sudo timestamp (within the configured timeout), which may make a later
interactive ``sudo`` prompt less likely; the check never reads a password.

Planning
--------
:func:`tool_plan` infers capabilities from paths (``.pdf`` -> ``pdf``),
probes only the relevant tools, and builds install options from the managers
that were actually detected. Every option is a preview: ``would_run`` is
always false and every blocker is named (``install``, ``network``, ``sudo``,
``manager``). A deterministic option is marked ``recommended`` when one is
feasible, preferring user-level uv/pip/npm over sudo, then the distribution
manager, then Homebrew on macOS.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform as platform_module
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ozagia.config import load_config_section

SCHEMA = "ozagia.toolchain.v1"
SCHEMA_VERSION = 1
REGISTRY_VERSION = "2026.11.1"
TOOLS_SECTION = "tools"

VERSION_TIMEOUT_S = 2.0
SUDO_TIMEOUT_S = 2.0
MAX_VERSION_OUTPUT_BYTES = 4 * 1024
MAX_VERSION_CHARS = 300
MAX_PATHS = 64
MAX_CAPABILITIES = 32
MAX_PATH_CHARS = 2_000
MAX_CONFIG_TEXT = 300
MAX_PREFERRED_ENTRIES = 64
MAX_BRIEF_CAPABILITIES = 8
MAX_TEXT_TOOLS = 12
MAX_TEXT_INSTALLS = 12

TRUTHY = frozenset({"1", "true", "yes"})
APPROVAL_ENV = {
    "allow_install": "OZAGIA_ALLOW_INSTALL",
    "allow_network": "OZAGIA_ALLOW_NETWORK",
    "allow_sudo": "OZAGIA_ALLOW_SUDO",
}
APPROVAL_KEYS = ("allow_install", "allow_network", "allow_sudo")
PROBE_KEYS = ("probe_versions", "probe_sudo")
#: Boolean ``[tools]`` key: whether the session brief may add a tools block.
BRIEF_KEY = "brief"
CONFIG_KEYS = frozenset(
    {*APPROVAL_KEYS, *PROBE_KEYS, "preferred", BRIEF_KEY})
BLOCK_ORDER = ("install", "network", "sudo", "manager")
MODEL_FAMILY_ALIASES = {
    "claude": "claude", "claude-code": "claude", "anthropic": "claude",
    "codex": "codex", "gpt": "codex", "gpt-5": "codex", "openai": "codex",
    "gemini": "gemini", "gemini-cli": "gemini",
    "cursor": "cursor", "aider": "aider", "openhands": "openhands",
    "local": "local", "llama": "local", "qwen": "local",
}

NOT_ASSESSED = (
    "version output is only the first line of an optional bounded probe; it "
    "is not checked against a pinned or minimum version range",
    "network reachability and package availability in each repository were "
    "not checked",
    "nothing was installed; every install command is a preview "
    "(would_run: false)",
    "GUI-only applications and app bundles may be installed without a "
    "discoverable executable",
    "remote MCP/API providers were not connected; their credentials, quotas "
    "and network reachability were not checked",
    "tool behaviour, package provenance and licences were not checked",
)

CHROMIUM_PROBES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
)
IMAGEMAGICK_PROBES = ("magick", "convert")
FFMPEG_VERSION_ARGS = ("-version",)


class ToolchainError(ValueError):
    """A plan request or registry value is invalid."""

    def __init__(self, message: str, *, errors: Iterable[Mapping[str, Any]] = ()):
        super().__init__(message)
        self.errors = [dict(error) for error in errors]


# --------------------------------------------------------------------------- #
# package managers (data)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ManagerSpec:
    id: str
    executables: tuple[str, ...]
    kind: str
    #: The command template used for this manager. ``{package}`` is replaced.
    command: str
    requires_sudo: bool
    description: str


_MANAGER_ORDER = (
    "uv", "pip", "pipx", "brew", "apt", "dnf", "yum", "pacman", "zypper",
    "apk", "npm", "cargo", "go", "gem",
)
_MANAGERS: dict[str, ManagerSpec] = {
    "uv": ManagerSpec(
        "uv", ("uv",), "python", "uv pip install {package}", False,
        "user-level Python packages, usually into the active environment"),
    "pip": ManagerSpec(
        "pip", ("pip", "pip3"), "python",
        "pip install --user {package}", False,
        "Python packages for the current user"),
    "pipx": ManagerSpec(
        "pipx", ("pipx",), "python", "pipx install {package}", False,
        "isolated Python command-line applications"),
    "brew": ManagerSpec(
        "brew", ("brew",), "system", "brew install {package}", False,
        "Homebrew (macOS and Linux)"),
    "apt": ManagerSpec(
        "apt", ("apt-get", "apt"), "system",
        "apt-get install -y {package}", True, "Debian/Ubuntu apt"),
    "dnf": ManagerSpec(
        "dnf", ("dnf",), "system", "dnf install -y {package}", True,
        "Fedora/RHEL dnf"),
    "yum": ManagerSpec(
        "yum", ("yum",), "system", "yum install -y {package}", True,
        "older RHEL/CentOS yum"),
    "pacman": ManagerSpec(
        "pacman", ("pacman",), "system",
        "pacman -S --noconfirm {package}", True, "Arch pacman"),
    "zypper": ManagerSpec(
        "zypper", ("zypper",), "system",
        "zypper --non-interactive install {package}", True, "openSUSE zypper"),
    "apk": ManagerSpec(
        "apk", ("apk",), "system", "apk add {package}", True, "Alpine apk"),
    "npm": ManagerSpec(
        "npm", ("npm",), "node", "npm install -g {package}", False,
        "global Node packages"),
    "cargo": ManagerSpec(
        "cargo", ("cargo",), "rust", "cargo install {package}", False,
        "Rust binaries"),
    "go": ManagerSpec(
        "go", ("go",), "go", "go install {package}", False, "Go binaries"),
    "gem": ManagerSpec(
        "gem", ("gem",), "ruby", "gem install --user-install {package}",
        False, "Ruby gems"),
}
_DISTRO_MANAGERS = {
    "debian": "apt", "ubuntu": "apt", "linuxmint": "apt", "pop": "apt",
    "raspbian": "apt",
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf",
    "almalinux": "dnf", "ol": "dnf",
    "arch": "pacman", "manjaro": "pacman", "endeavouros": "pacman",
    "opensuse": "zypper", "opensuse-leap": "zypper",
    "opensuse-tumbleweed": "zypper", "sles": "zypper",
    "alpine": "apk",
}


# --------------------------------------------------------------------------- #
# tool and capability registry (data, not inference)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Install:
    """One package-manager install recipe. Nothing here is executed."""

    manager: str
    package: str
    command: str
    requires_sudo: bool
    requires_network: bool = True
    notes: str = ""
    #: Name of the ``ozagia[...]`` optional extra this recipe installs.
    extra: str | None = None

    def rendered(self) -> str:
        """The command as a human would type it (still only a preview)."""
        command = self.command.replace("{package}", self.package)
        if self.requires_sudo and not command.startswith("sudo "):
            command = "sudo " + command
        return command

    @property
    def kind(self) -> str:
        if self.extra:
            return "python-extra"
        if self.manager in ("uv", "pip", "pipx"):
            return "python-package"
        if self.manager == "npm":
            return "npm-package"
        if self.manager in ("apt", "dnf", "yum", "pacman", "zypper", "apk",
                            "brew"):
            return "system-package"
        return "toolchain-package"


@dataclass(frozen=True)
class ToolSpec:
    id: str
    title: str
    kind: str
    probes: tuple[str, ...]
    provides: tuple[str, ...]
    writes: bool
    capabilities: tuple[str, ...]
    installs: tuple[Install, ...]
    version_args: tuple[str, ...] = ("--version",)
    distribution: str | None = None
    #: ``ozagia[...]`` optional extra that installs this Python library.
    extra: str | None = None
    note: str = ""
    #: library, cli, mcp, remote, node or app. Empty: derive from ``kind``.
    format: str = ""
    #: A remote/MCP/API provider: no local probe and no automatic install.
    remote: bool = False
    #: Needs a token, account, DSN or other credential.
    credentialed: bool = False
    #: False for a local Node library, app bundle or other unprobeable tool.
    probeable: bool = True
    #: Other tool ids that must also be available for this one to be usable.
    requires: tuple[str, ...] = ()
    requires_network: bool = False
    native_for: tuple[str, ...] = ()
    preferred_by: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    title: str
    description: str
    extensions: tuple[str, ...]
    note: str = ""
    #: Umbrella group id such as ``documents`` or ``media``.
    group: str | None = None


@dataclass(frozen=True)
class GroupSpec:
    id: str
    title: str
    description: str
    capabilities: tuple[str, ...]
    #: Document subtypes covered by a child capability, for display.
    subtypes: tuple[str, ...] = ()


def _install(manager: str, package: str, *, command: str | None = None,
             notes: str = "", extra: str | None = None) -> Install:
    spec = _MANAGERS[manager]
    return Install(
        manager=manager, package=package,
        command=command or spec.command,
        requires_sudo=spec.requires_sudo,
        requires_network=True, notes=notes, extra=extra,
    )


def _system(package: str, *, apt: str | None = None, dnf: str | None = None,
            pacman: str | None = None, brew: str | None = None,
            brew_command: str | None = None,
            notes: str = "") -> tuple[Install, ...]:
    """The usual apt/dnf/pacman/brew recipes for a system package."""
    entries = (
        _install("apt", apt or package, notes=notes),
        _install("dnf", dnf or package, notes=notes),
        _install("pacman", pacman or package, notes=notes),
        _install("brew", brew or package, command=brew_command, notes=notes),
    )
    return entries


def _python(package: str, *, dist: str | None = None,
            notes: str = "", extra: str | None = None) -> tuple[Install, ...]:
    """uv and pip recipes for a Python distribution/import package.

    A tool that belongs to an optional extra installs the extra
    (``ozagia[pdf]``), not the bare package, so the plan names the same
    supported set the project documents. The bare package remains the
    distribution this entry reports and versions from metadata.
    """
    name = dist or package
    if extra:
        target = f"ozagia[{extra}]"
        note = notes or f"optional extra {extra}"
        return (
            _install("uv", target,
                     command="uv pip install '{package}'", notes=note,
                     extra=extra),
            _install("pip", target,
                     command="python -m pip install '{package}'",
                     notes=note, extra=extra),
        )
    return (_install("uv", name, notes=notes),
            _install("pip", name, notes=notes))


def _npm(package: str, *, notes: str = "") -> tuple[Install, ...]:
    return (_install("npm", package, notes=notes),)


def _remote(tool_id: str, title: str, *, provides: tuple[str, ...],
            capabilities: tuple[str, ...], format: str = "mcp",
            credentialed: bool = False, workflows: tuple[str, ...] = (),
            native_for: tuple[str, ...] = (),
            preferred_by: tuple[str, ...] = (), note: str = "") -> ToolSpec:
    """A remote/MCP/API provider: never probed and never auto-installed."""
    return ToolSpec(
        tool_id, title, "remote", (), provides, False, capabilities, (),
        format=format, remote=True, credentialed=credentialed,
        probeable=False, requires_network=True, native_for=native_for,
        preferred_by=preferred_by, workflows=workflows, note=note)


def _node(tool_id: str, title: str, package: str, *,
          provides: tuple[str, ...], capabilities: tuple[str, ...],
          workflows: tuple[str, ...] = (),
          preferred_by: tuple[str, ...] = (), note: str = "") -> ToolSpec:
    """A local Node library: installable, but not discoverable here."""
    return ToolSpec(
        tool_id, title, "node", (), provides, True, capabilities,
        _npm(package), format="library", probeable=False,
        preferred_by=preferred_by, workflows=workflows,
        note=note or "Node library; local presence is not probed")


_GROUPS: tuple[GroupSpec, ...] = (
    GroupSpec(
        "documents", "Documents",
        "PDF, OCR and office documents. Office covers the docx, xlsx and "
        "pptx subtypes.",
        ("pdf", "ocr", "office"), ("docx", "xlsx", "pptx")),
    GroupSpec(
        "media", "Media",
        "Images, audio and video: inspect, convert, transcribe and edit.",
        ("image", "audio", "video")),
    GroupSpec(
        "web", "Web",
        "Browser automation and web research providers.",
        ("browser", "research")),
)


_TOOLS: tuple[ToolSpec, ...] = (
    # PDF
    ToolSpec(
        "qpdf", "qpdf PDF transformation", "binary", ("qpdf",),
        ("merge/split", "rotate/crop", "encrypt/decrypt", "repair",
         "extract pages"),
        True, ("pdf",), _system("qpdf")),
    ToolSpec(
        "pdftk", "pdftk PDF toolkit", "binary", ("pdftk",),
        ("merge/split", "fill forms", "stamp", "encrypt/decrypt"),
        True, ("pdf",),
        _system("pdftk-java", dnf="pdftk", pacman="pdftk",
                brew="pdftk-java")),
    ToolSpec(
        "mutool", "MuPDF command-line tools", "binary", ("mutool",),
        ("render pages", "extract text", "merge/split", "extract images"),
        True, ("pdf",),
        _system("mupdf-tools", pacman="mupdf", brew="mupdf-tools")),
    ToolSpec(
        "pdftotext", "poppler pdftotext", "binary", ("pdftotext",),
        ("extract text",), True, ("pdf",),
        _system("poppler-utils", pacman="poppler", brew="poppler"),
        version_args=("-v",)),
    ToolSpec(
        "pdftoppm", "poppler pdftoppm", "binary", ("pdftoppm",),
        ("render pages", "convert to images"), True, ("pdf",),
        _system("poppler-utils", pacman="poppler", brew="poppler"),
        version_args=("-v",),
        workflows=("render PDF pages to images", "visual check")),
    ToolSpec(
        "ghostscript", "Ghostscript", "binary", ("gs",),
        ("render pages", "convert", "compress", "print to PDF"),
        True, ("pdf",), _system("ghostscript")),
    ToolSpec(
        "pypdf", "pypdf (Python)", "python", ("pypdf",),
        ("pure-Python PDF editing", "merge/split", "rotate/crop",
         "extract text", "fill forms", "encrypt/decrypt"),
        True, ("pdf",), _python("pypdf", extra="pdf"),
        distribution="pypdf", extra="pdf",
        preferred_by=("claude",),
        workflows=("PDF merge/split", "PDF rotate", "PDF skill")),
    ToolSpec(
        "pikepdf", "pikepdf (Python)", "python", ("pikepdf",),
        ("merge/split", "repair", "encrypt/decrypt", "extract pages"),
        True, ("pdf",), _python("pikepdf"), distribution="pikepdf",
        note="needs the qpdf library"),
    ToolSpec(
        "pdfplumber", "pdfplumber (Python)", "python", ("pdfplumber",),
        ("pure-Python text extraction", "extract text", "extract tables"),
        False, ("pdf",), _python("pdfplumber", extra="pdf"),
        distribution="pdfplumber", extra="pdf",
        preferred_by=("claude",),
        workflows=("PDF text extraction", "PDF tables", "PDF skill")),
    ToolSpec(
        "fitz", "PyMuPDF (Python, import fitz)", "python", ("fitz",),
        ("extract text", "render pages", "merge/split", "annotate",
         "extract images"),
        True, ("pdf",), _python("PyMuPDF"), distribution="PyMuPDF"),
    ToolSpec(
        "reportlab", "ReportLab (Python)", "python", ("reportlab",),
        ("pure-Python PDF creation", "create", "add text/images"), True,
        ("pdf",), _python("reportlab", extra="pdf"),
        distribution="reportlab", extra="pdf",
        preferred_by=("claude",),
        workflows=("create PDF", "PDF report")),
    _node(
        "pdf-lib", "pdf-lib (Node library)", "pdf-lib",
        provides=("fill forms", "merge/split", "modify metadata"),
        capabilities=("pdf",),
        workflows=("fill PDF forms from JavaScript", "PDF manipulation"),
        note="Node library; checked by the project, not by ozagia"),
    # OCR
    ToolSpec(
        "tesseract", "Tesseract OCR", "binary", ("tesseract",),
        ("OCR", "extract text"), True, ("ocr",),
        _system("tesseract-ocr", dnf="tesseract", pacman="tesseract",
                brew="tesseract"),
        workflows=("OCR scanned images", "OCR scanned PDFs")),
    ToolSpec(
        "pytesseract", "pytesseract (Python wrapper for Tesseract)", "python",
        ("pytesseract",),
        ("OCR", "extract text"), False, ("ocr",),
        _python("pytesseract", extra="ocr"), distribution="pytesseract",
        extra="ocr", requires=("tesseract",),
        note="needs the Tesseract engine",
        preferred_by=("claude",),
        workflows=("OCR from Python", "PDF skill OCR")),
    ToolSpec(
        "ocrmypdf", "OCRmyPDF", "binary", ("ocrmypdf",),
        ("OCR", "add text layer", "deskew"), True, ("ocr",),
        (*_system("ocrmypdf"), *_python("ocrmypdf")),
        note="needs tesseract and Ghostscript"),
    # Image
    ToolSpec(
        "imagemagick", "ImageMagick", "binary", IMAGEMAGICK_PROBES,
        ("convert", "resize", "compose", "extract frames"),
        True, ("image",),
        _system("imagemagick", dnf="ImageMagick"), version_args=("-version",)),
    ToolSpec(
        "ffmpeg", "FFmpeg", "binary", ("ffmpeg",),
        ("convert", "transcode", "extract audio", "extract frames",
         "resize"),
        True, ("image", "audio", "video"), _system("ffmpeg"),
        version_args=FFMPEG_VERSION_ARGS,
        workflows=("transcode media", "extract audio", "extract frames",
                   "edit video")),
    ToolSpec(
        "whisper", "OpenAI Whisper CLI", "binary", ("whisper",),
        ("transcribe audio", "translate speech"), True, ("audio", "video"),
        _python("openai-whisper"), format="cli",
        workflows=("video -> transcript", "audio -> transcript")),
    ToolSpec(
        "faster-whisper", "faster-whisper (Python)", "python",
        ("faster_whisper",),
        ("transcribe audio", "translate speech"), False,
        ("audio", "video"), _python("faster-whisper", extra="media"),
        distribution="faster-whisper", extra="media",
        workflows=("local transcription", "video -> transcript")),
    ToolSpec(
        "opencv", "OpenCV (Python, import cv2)", "python", ("cv2",),
        ("read frames", "inspect images", "compose"), True,
        ("image", "video"), _python("opencv-python", extra="media"),
        distribution="opencv-python", extra="media",
        workflows=("visual check", "frame analysis")),
    ToolSpec(
        "pillow", "Pillow (Python, import PIL)", "python", ("PIL",),
        ("pure-Python image editing", "convert", "resize", "compose",
         "create"),
        True, ("image",), _python("Pillow", extra="image"),
        distribution="Pillow", extra="image"),
    # Office
    ToolSpec(
        "libreoffice", "LibreOffice", "binary", ("libreoffice", "soffice"),
        ("convert", "render pages", "edit documents"),
        True, ("office",),
        _system("libreoffice", pacman="libreoffice-fresh",
                brew="libreoffice", brew_command="install --cask {package}")),
    ToolSpec(
        "pandoc", "Pandoc", "binary", ("pandoc",),
        ("convert", "extract text"), True, ("office",),
        _system("pandoc")),
    ToolSpec(
        "python-docx", "python-docx (Python, import docx)", "python",
        ("docx",),
        ("pure-Python Word editing", "create", "edit documents",
         "extract text"),
        True, ("office",), _python("python-docx", extra="office"),
        distribution="python-docx", extra="office",
        preferred_by=("claude",),
        workflows=("DOCX create/edit", "DOCX skill")),
    ToolSpec(
        "openpyxl", "openpyxl (Python)", "python", ("openpyxl",),
        ("pure-Python spreadsheet editing", "create", "edit spreadsheets",
         "extract data"),
        True, ("office",), _python("openpyxl", extra="office"),
        distribution="openpyxl", extra="office",
        preferred_by=("claude",),
        workflows=("XLSX create/edit", "XLSX skill")),
    ToolSpec(
        "python-pptx", "python-pptx (Python, import pptx)", "python",
        ("pptx",),
        ("pure-Python presentation editing", "create",
         "edit presentations", "extract text"),
        True, ("office",), _python("python-pptx", extra="office"),
        distribution="python-pptx", extra="office",
        preferred_by=("claude",),
        workflows=("PPTX create/edit", "PPTX skill")),
    # Audio/video companion
    ToolSpec(
        "ffprobe", "ffprobe", "binary", ("ffprobe",),
        ("inspect streams", "inspect metadata"), False, ("audio", "video"),
        _system("ffmpeg"), version_args=FFMPEG_VERSION_ARGS),
    # Archive
    ToolSpec(
        "7z", "7-Zip", "binary", ("7z", "7zz", "7za"),
        ("create", "extract", "list", "encrypt/decrypt"), True, ("archive",),
        _system("p7zip-full", dnf="p7zip", pacman="p7zip", brew="p7zip")),
    ToolSpec(
        "unzip", "Info-ZIP unzip", "binary", ("unzip",),
        ("extract", "list"), True, ("archive",), _system("unzip"),
        version_args=("-v",)),
    ToolSpec(
        "tar", "GNU tar", "binary", ("tar",),
        ("create", "extract", "list"), True, ("archive",),
        _system("tar", brew="gnu-tar"),
        note="Homebrew installs GNU tar as gtar"),
    # Diagram
    ToolSpec(
        "dot", "Graphviz dot", "binary", ("dot",),
        ("render diagrams", "create"), True, ("diagram",),
        _system("graphviz"), version_args=("-V",)),
    ToolSpec(
        "mmdc", "Mermaid CLI", "binary", ("mmdc",),
        ("render diagrams", "create"), True, ("diagram",),
        _npm("@mermaid-js/mermaid-cli")),
    # Data
    ToolSpec(
        "jq", "jq", "binary", ("jq",), ("query", "transform"), False,
        ("data",), _system("jq")),
    ToolSpec(
        "yq", "yq", "binary", ("yq",),
        ("query", "transform", "convert"), False, ("data",),
        _system("yq", pacman="go-yq", brew="yq"),
        note="apt/dnf packages may be the Python yq, not mikefarah/yq"),
    ToolSpec(
        "sqlite3", "SQLite shell", "binary", ("sqlite3",),
        ("query", "modify database"), True, ("data",),
        _system("sqlite3", dnf="sqlite", pacman="sqlite", brew="sqlite")),
    ToolSpec(
        "duckdb", "DuckDB", "binary", ("duckdb",),
        ("query", "modify database", "convert"), True, ("data",),
        _system("duckdb"),
        note="distribution repositories may not carry this package"),
    ToolSpec(
        "pandas", "pandas (Python)", "python", ("pandas",),
        ("read/write tabular data", "transform", "summarise"), True,
        ("data",), _python("pandas", extra="data"),
        distribution="pandas", extra="data",
        workflows=("CSV/Parquet analysis", "dataframe transform")),
    ToolSpec(
        "jupyter", "Jupyter", "binary", ("jupyter",),
        ("run notebooks", "inspect data"), True, ("data",),
        _python("jupyter"), format="cli",
        workflows=("notebook analysis", "interactive data check")),
    ToolSpec(
        "psql", "PostgreSQL psql", "binary", ("psql",),
        ("query database", "modify database"), True, ("data", "ops"),
        _system("postgresql-client", dnf="postgresql", pacman="postgresql",
                brew="libpq"),
        note="use a read-only DSN for inspection",
        workflows=("read-only SQL", "database inspection")),
    _remote(
        "postgres-mcp-pro", "Postgres MCP Pro",
        provides=("read-only SQL", "schema inspection"),
        capabilities=("data",), format="mcp", credentialed=True,
        workflows=("database inspection", "read-only SQL"),
        note="remote MCP; use a read-only DSN"),
    # Browser
    ToolSpec(
        "chromium", "Chromium/Chrome", "binary", CHROMIUM_PROBES,
        ("render pages", "screenshot", "print to PDF",
         "headless automation"),
        True, ("browser",),
        _system("chromium", brew="chromium",
                brew_command="install --cask {package}")),
    ToolSpec(
        "playwright", "Playwright CLI", "binary", ("playwright",),
        ("headless automation", "screenshot", "print to PDF",
         "render pages"),
        True, ("browser",), _npm("playwright"), format="cli",
        note="browser binaries need a separate `playwright install` step; "
             "the MCP form is also available",
        native_for=("codex", "cursor", "claude", "openhands"),
        preferred_by=("codex", "cursor", "claude", "openhands"),
        workflows=("deterministic browser automation",
                   "accessibility-tree interaction", "screenshot",
                   "visual check")),
    _remote(
        "chrome-devtools-mcp", "Chrome DevTools MCP",
        provides=("page inspection", "performance traces", "network traces"),
        capabilities=("browser",), format="mcp",
        workflows=("debug a page", "performance trace"),
        native_for=("codex", "cursor"),
        note="local MCP server; start it from the harness"),
    ToolSpec(
        "browser-use", "browser-use (Python)", "python", ("browser_use",),
        ("goal-driven browser automation", "screenshot"), True,
        ("browser",), _python("browser-use"), distribution="browser-use",
        preferred_by=("openhands",),
        workflows=("goal-driven browser task", "web workflow")),
    _node(
        "puppeteer", "Puppeteer (Node library)", "puppeteer",
        provides=("Chrome automation", "screenshot", "print to PDF"),
        capabilities=("browser",),
        workflows=("Chrome-specific automation", "legacy browser script"),
        preferred_by=("cursor",)),
    # Research / web
    _remote(
        "exa", "Exa", provides=("web search", "company research"),
        capabilities=("research",), format="mcp", credentialed=True,
        workflows=("web search", "company research"),
        native_for=("codex", "cursor")),
    _remote(
        "firecrawl", "Firecrawl",
        provides=("crawl site", "extract markdown", "web search"),
        capabilities=("research",), format="mcp", credentialed=True,
        workflows=("crawl documentation", "site to markdown")),
    _remote(
        "fetch", "Fetch MCP", provides=("fetch URL", "extract markdown"),
        capabilities=("research",), format="mcp",
        workflows=("fetch a page", "read documentation URL")),
    _remote(
        "context7", "Context7",
        provides=("version-aware library docs", "code examples"),
        capabilities=("research",), format="mcp",
        workflows=("library documentation", "version-specific API"),
        native_for=("codex", "cursor", "claude")),
    _remote(
        "arxiv", "arXiv", provides=("paper search", "paper metadata"),
        capabilities=("research",), format="api",
        workflows=("paper search", "literature review")),
    _remote(
        "zotero", "Zotero", provides=("reference library", "citation export"),
        capabilities=("research",), format="app", credentialed=True,
        workflows=("reference library", "citation management")),
    # Operations
    ToolSpec(
        "terraform", "Terraform", "binary", ("terraform",),
        ("plan infrastructure", "apply infrastructure", "inspect state"),
        True, ("ops",),
        _system("terraform", notes="distribution repositories may not carry "
                                   "this package"),
        note="distribution repositories may not carry this package",
        workflows=("Terraform plan", "infrastructure review")),
    ToolSpec(
        "kubectl", "kubectl", "binary", ("kubectl",),
        ("inspect cluster", "apply manifests", "logs"), True, ("ops",),
        _system("kubectl", notes="distribution repositories may not carry "
                                 "this package"),
        note="distribution repositories may not carry this package",
        workflows=("cluster inspection", "Kubernetes manifests")),
    _remote(
        "github-mcp", "GitHub MCP",
        provides=("issues", "pull requests", "repository metadata"),
        capabilities=("ops",), format="mcp", credentialed=True,
        workflows=("issue triage", "pull request review")),
    _remote(
        "sentry-mcp", "Sentry MCP",
        provides=("error events", "release health"),
        capabilities=("ops",), format="mcp", credentialed=True,
        workflows=("error triage", "release diagnosis")),
    _remote(
        "cloudflare-mcp", "Cloudflare MCP",
        provides=("DNS", "workers", "deployments"),
        capabilities=("ops",), format="mcp", credentialed=True,
        workflows=("DNS inspection", "deployment status")),
    # Design
    _remote(
        "figma-mcp", "Figma MCP",
        provides=("design inspection", "component metadata"),
        capabilities=("design",), format="mcp", credentialed=True,
        workflows=("design handoff", "UI implementation"),
        native_for=("cursor",)),
    _remote(
        "canva-mcp", "Canva MCP",
        provides=("design creation", "asset export"),
        capabilities=("design",), format="mcp", credentialed=True,
        workflows=("social asset", "presentation design")),
    _remote(
        "image-generation", "Image generation provider",
        provides=("create image", "edit image"),
        capabilities=("design",), format="remote", credentialed=True,
        workflows=("generate illustration", "edit image")),
    # Communication
    _remote(
        "notion-mcp", "Notion MCP",
        provides=("pages", "databases", "search"),
        capabilities=("communication",), format="mcp",
        credentialed=True, workflows=("team notes", "project wiki")),
    _remote(
        "linear-mcp", "Linear MCP",
        provides=("issues", "projects", "cycles"),
        capabilities=("communication",), format="mcp",
        credentialed=True, workflows=("issue tracking", "sprint planning")),
    _remote(
        "slack-mcp", "Slack MCP",
        provides=("messages", "channels", "search"),
        capabilities=("communication",), format="mcp",
        credentialed=True, workflows=("team chat", "incident channel")),
    _remote(
        "email", "Email (SMTP/IMAP)", provides=("send email", "search mail"),
        capabilities=("communication",), format="api",
        credentialed=True, workflows=("send status email", "search inbox")),
)

_CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "archive", "Archives",
        "Create, inspect or extract ZIP, 7z and tar archives.",
        (".zip", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
         ".tar.xz", ".txz", ".gz", ".bz2", ".xz", ".rar")),
    CapabilitySpec(
        "audio", "Audio",
        "Transcode, inspect, transcribe or extract audio tracks.",
        (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus"),
        group="media"),
    CapabilitySpec(
        "browser", "Browser automation",
        "Render HTML, screenshot a page or drive a headless browser.",
        (".html", ".htm", ".xhtml"), group="web"),
    CapabilitySpec(
        "communication", "Communication",
        "Team chat, issues, documents and email providers.",
        (".eml",)),
    CapabilitySpec(
        "data", "Structured data",
        "Query, transform or inspect JSON, YAML, CSV, SQLite and Parquet.",
        (".sqlite", ".sqlite3", ".db", ".csv", ".tsv", ".parquet",
         ".jsonl", ".ndjson", ".duckdb", ".ipynb")),
    CapabilitySpec(
        "design", "Design",
        "Design handoff, asset creation and image generation providers.",
        (".fig",)),
    CapabilitySpec(
        "diagram", "Diagrams",
        "Render Graphviz or Mermaid sources into images.",
        (".svg", ".dot", ".gv", ".mmd", ".mermaid")),
    CapabilitySpec(
        "image", "Images",
        "Convert, resize, compose or extract frames from images.",
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
         ".heic", ".avif"), group="media"),
    CapabilitySpec(
        "ocr", "OCR",
        "Recognise text in scanned images or PDFs.",
        (), group="documents"),
    CapabilitySpec(
        "office", "Office documents",
        "Convert, render or edit office documents; covers docx, xlsx and "
        "pptx subtypes.",
        (".docx", ".doc", ".odt", ".pptx", ".ppt", ".odp", ".xlsx", ".xls",
         ".ods", ".rtf"), group="documents"),
    CapabilitySpec(
        "ops", "Operations",
        "Infrastructure, cluster, issue-tracker and incident providers.",
        (".tf", ".tfvars")),
    CapabilitySpec(
        "pdf", "PDF editing and text extraction",
        "Inspect, extract from, create, merge, split or fill PDF files.",
        (".pdf",), group="documents"),
    CapabilitySpec(
        "research", "Research and web",
        "Web search, crawling, documentation lookup and papers. Mostly "
        "remote MCP/API providers.",
        (), group="web"),
    CapabilitySpec(
        "video", "Video",
        "Transcode, inspect, extract frames or transcribe video.",
        (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"), group="media"),
)

_GROUPS_BY_ID: dict[str, GroupSpec] = {group.id: group for group in _GROUPS}
_GROUP_CAPABILITIES: dict[str, tuple[str, ...]] = {
    group.id: group.capabilities for group in _GROUPS}
_TOOLS_BY_ID: dict[str, ToolSpec] = {tool.id: tool for tool in _TOOLS}
_CAPABILITIES_BY_ID: dict[str, CapabilitySpec] = {
    capability.id: capability for capability in _CAPABILITIES}
_CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    capability.id: tuple(sorted(
        tool.id for tool in _TOOLS if capability.id in tool.capabilities))
    for capability in _CAPABILITIES
}
_EXTENSION_TO_CAPABILITY: dict[str, str] = {
    extension.lower(): capability.id
    for capability in _CAPABILITIES for extension in capability.extensions
}
_EXTENSIONS_LONGEST_FIRST = tuple(sorted(
    _EXTENSION_TO_CAPABILITY, key=lambda value: (-len(value), value)))


def _tool_format(tool: ToolSpec) -> str:
    if tool.format:
        return tool.format
    return "library" if tool.kind == "python" else "cli"


def registry() -> dict[str, Any]:
    """The versioned registry as plain JSON-safe data (a fresh copy)."""
    capabilities: dict[str, Any] = {}
    for capability in sorted(_CAPABILITIES, key=lambda item: item.id):
        capabilities[capability.id] = {
            "id": capability.id,
            "title": capability.title,
            "description": capability.description,
            "extensions": list(capability.extensions),
            "tools": list(_CAPABILITY_TOOLS[capability.id]),
            "note": capability.note,
            "group": capability.group,
        }
    tools: dict[str, Any] = {}
    for tool in sorted(_TOOLS, key=lambda item: item.id):
        tools[tool.id] = {
            "id": tool.id,
            "title": tool.title,
            "kind": tool.kind,
            "format": _tool_format(tool),
            "probes": list(tool.probes),
            "provides": list(tool.provides),
            "writes": tool.writes,
            "capabilities": list(tool.capabilities),
            "version_args": list(tool.version_args),
            "distribution": tool.distribution,
            "extra": tool.extra,
            "remote": tool.remote,
            "credentialed": tool.credentialed,
            "probeable": tool.probeable,
            "requires": list(tool.requires),
            "requires_network": tool.requires_network,
            "native_for": list(tool.native_for),
            "preferred_by": list(tool.preferred_by),
            "workflows": list(tool.workflows),
            "note": tool.note,
            "installs": [
                {
                    "manager": install.manager,
                    "package": install.package,
                    "command": install.command,
                    "requires_sudo": install.requires_sudo,
                    "requires_network": install.requires_network,
                    "notes": install.notes,
                    "extra": install.extra,
                    "kind": install.kind,
                }
                for install in tool.installs
            ],
        }
    groups = {
        group.id: {
            "id": group.id,
            "title": group.title,
            "description": group.description,
            "capabilities": list(group.capabilities),
            "subtypes": list(group.subtypes),
        }
        for group in sorted(_GROUPS, key=lambda item: item.id)
    }
    managers = {
        manager_id: {
            "id": spec.id,
            "executables": list(spec.executables),
            "kind": spec.kind,
            "command": spec.command,
            "requires_sudo": spec.requires_sudo,
            "description": spec.description,
        }
        for manager_id, spec in sorted(_MANAGERS.items())
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "capabilities": capabilities,
        "groups": groups,
        "tools": tools,
        "managers": managers,
    }


def capability_ids() -> tuple[str, ...]:
    return tuple(sorted(_CAPABILITIES_BY_ID))


def group_ids() -> tuple[str, ...]:
    return tuple(sorted(_GROUPS_BY_ID))


def tool_ids() -> tuple[str, ...]:
    return tuple(sorted(_TOOLS_BY_ID))


def tool(tool_id: str) -> dict[str, Any]:
    """One registry entry, or a structured error for an unknown id.

    The id is matched case-insensitively, and a probe name (``gs``, ``PIL``,
    ``convert``) is accepted as an alias so ``explain`` works with the command
    a person naturally types.
    """
    found = _TOOLS_BY_ID.get(tool_id)
    if found is None:
        wanted = tool_id.strip().lower()
        found = next(
            (item for item in _TOOLS
             if item.id.lower() == wanted
             or (item.distribution is not None
                 and item.distribution.lower() == wanted)
             or any(probe.lower() == wanted for probe in item.probes)),
            None)
    if found is None:
        raise ToolchainError(
            f"unknown tool {tool_id!r}; known tools: {', '.join(tool_ids())}")
    return registry()["tools"][found.id]


def infer_capabilities(paths: Sequence[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Capabilities from path extensions; unknown extensions are not guessed."""
    inferred: set[str] = set()
    unmatched: list[dict[str, str]] = []
    for path in paths:
        name = PurePosixPath(str(path).replace("\\", "/")).name.lower()
        match = next((extension for extension in _EXTENSIONS_LONGEST_FIRST
                      if name.endswith(extension)), None)
        if match is None:
            unmatched.append({"path": str(path),
                              "suffix": PurePosixPath(name).suffix.lower()})
        else:
            inferred.add(_EXTENSION_TO_CAPABILITY[match])
    return sorted(inferred), unmatched


# --------------------------------------------------------------------------- #
# environment detection
# --------------------------------------------------------------------------- #
def _safe_which(which: Callable[[str], Any], name: str) -> str | None:
    try:
        path = which(name)
    except (OSError, ValueError):
        return None
    return str(path) if path else None


def _safe_find_spec(module_finder: Callable[[str], Any],
                    name: str) -> Any | None:
    try:
        return module_finder(name)
    except Exception:  # noqa: BLE001 - a broken finder means "not found"
        return None


def _read_os_release(path: str | Path) -> dict[str, Any]:
    """Parse a bounded os-release file. Missing or malformed is reported."""
    source = Path(path)
    report: dict[str, Any] = {
        "source": str(source), "available": False, "id": None, "name": None,
        "pretty_name": None, "version": None, "version_id": None,
        "id_like": [],
    }
    try:
        if source.stat().st_size > 64 * 1024:
            return report
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return report
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and len(key) <= 64 and len(value) <= 300:
            values[key] = value
    report.update({
        "available": bool(values),
        "id": values.get("ID"),
        "name": values.get("NAME"),
        "pretty_name": values.get("PRETTY_NAME"),
        "version": values.get("VERSION") or values.get("VERSION_ID"),
        "version_id": values.get("VERSION_ID"),
        "id_like": [part for part in (values.get("ID_LIKE") or "").split()
                    if part],
    })
    return report


def _platform_report(os_release_path: str | Path | None,
                     environ: Mapping[str, str]) -> dict[str, Any]:
    system = platform_module.system()
    distro = _read_os_release(os_release_path or "/etc/os-release")
    hints: list[str] = []
    wsl = bool(environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"))
    if wsl:
        hints.append("environment:WSL")
    container_hint = str(environ.get("container") or "").strip()
    if container_hint:
        hints.append(f"environment:container={container_hint}")
    for candidate in ("/.dockerenv", "/run/.containerenv"):
        try:
            if Path(candidate).exists():
                hints.append(f"file:{candidate}")
        except OSError:
            pass
    family = "macos" if system == "Darwin" else (
        "windows" if system == "Windows" else (
            "linux" if system == "Linux" else "other"))
    return {
        "system": system,
        "release": platform_module.release(),
        "version": platform_module.version(),
        "machine": platform_module.machine(),
        "arch": platform_module.machine(),
        "family": family,
        "distro": distro,
        "wsl": wsl,
        "container": bool(hints),
        "container_hints": sorted(set(hints)),
    }


def _python_report(managers: Mapping[str, Any],
                   environ: Mapping[str, str]) -> dict[str, Any]:
    return {
        "executable": sys.executable,
        "version": platform_module.python_version(),
        "venv": bool(sys.prefix != sys.base_prefix
                     or environ.get("VIRTUAL_ENV")),
        "uv": "uv" in managers,
    }


def _detect_managers(which: Callable[[str], Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for manager_id in _MANAGER_ORDER:
        spec = _MANAGERS[manager_id]
        for executable in spec.executables:
            path = _safe_which(which, executable)
            if path is None:
                continue
            found[executable] = {
                "executable": executable,
                "family": manager_id,
                "kind": spec.kind,
                "path": path,
            }
    return dict(sorted(found.items()))


def _command_env(environ: Mapping[str, str]) -> dict[str, str]:
    """A small, sanitized environment for version and sudo probes."""
    keep = (
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
        "TMP", "TEMP", "SYSTEMROOT", "USERPROFILE",
    )
    env = {key: str(environ[key]) for key in keep if key in environ}
    env.setdefault("PATH", os.defpath)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("LC_ALL", "C")
    return env


def _result_value(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _run_command(argv: Sequence[str], *, runner: Callable[..., Any] | None,
                 timeout: float, cwd: str | Path,
                 environ: Mapping[str, str]) -> Any:
    """Run one bounded argv list; never a shell. Injected runners are honored."""
    env = _command_env(environ)
    if runner is not None:
        try:
            return runner(list(argv), timeout=timeout, cwd=str(cwd),
                          env=dict(env), stdin=subprocess.DEVNULL)
        except TypeError:
            # Older runners accept only the command, timeout and cwd.
            return runner(list(argv), timeout=timeout, cwd=str(cwd))
    return subprocess.run(
        list(argv), cwd=str(cwd), env=env, capture_output=True, text=True,
        errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        shell=False, check=False,
    )


def _version_line(result: Any) -> str | None:
    stdout = str(_result_value(result, "stdout", "") or "")
    stderr = str(_result_value(result, "stderr", "") or "")
    text = stdout if stdout.strip() else stderr
    if not text:
        return None
    data = text.encode("utf-8", errors="replace")[:MAX_VERSION_OUTPUT_BYTES]
    for line in data.decode("utf-8", errors="replace").splitlines():
        clean = line.strip()
        if clean:
            return clean[:MAX_VERSION_CHARS]
    return None


def _module_distributions() -> dict[str, tuple[str, ...]]:
    """import-name -> distribution names, from installed metadata only."""
    try:
        mapping = importlib.metadata.packages_distributions()
    except Exception:  # noqa: BLE001 - metadata is advisory
        return {}
    return {
        str(name).lower(): tuple(str(item) for item in values)
        for name, values in mapping.items()
    }


def _python_distribution(tool: ToolSpec) -> str | None:
    """The distribution that owns this module, without importing it."""
    if tool.distribution:
        return tool.distribution
    for probe in tool.probes:
        distributions = _module_distributions().get(probe.lower(), ())
        if distributions:
            return distributions[0]
    return None


def _probe_version(tool: ToolSpec, presence: Mapping[str, Any], *,
                   root: Path, runner: Callable[..., Any] | None,
                   environ: Mapping[str, str]) -> str | None:
    if not presence.get("available"):
        return None
    if tool.kind == "python":
        distribution = _python_distribution(tool)
        if not distribution:
            return None
        try:
            return str(importlib.metadata.version(distribution))
        except Exception:  # noqa: BLE001 - metadata is advisory
            return None
    path = presence.get("path")
    if not path:
        return None
    try:
        result = _run_command(
            [str(path), *tool.version_args], runner=runner,
            timeout=VERSION_TIMEOUT_S, cwd=root, environ=environ)
    except Exception:  # noqa: BLE001 - a version failure is not unavailable
        return None
    return _version_line(result)


def _sudo_probe(path: str, *, root: Path, runner: Callable[..., Any] | None,
                environ: Mapping[str, str]) -> bool:
    try:
        result = _run_command(
            [path, "-n", "true"], runner=runner, timeout=SUDO_TIMEOUT_S,
            cwd=root, environ=environ)
    except Exception:  # noqa: BLE001 - any failure means not usable
        return False
    try:
        return int(_result_value(result, "returncode",
                                 _result_value(result, "exit_code", 1))) == 0
    except (TypeError, ValueError):
        return False


def _config_error(key: str, source: str, message: str) -> dict[str, str]:
    return {"key": key, "source": source, "message": message[:MAX_CONFIG_TEXT]}


def _validate_tools_table(table: Mapping[str, Any], source: str
                          ) -> tuple[dict[str, Any], list[dict[str, str]],
                                     list[dict[str, str]]]:
    """Validate one raw ``[tools]`` table. Bad values keep safe defaults."""
    clean: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    for key in sorted(table):
        value = table[key]
        if key not in CONFIG_KEYS:
            unknown.append({"key": key, "source": source})
            errors.append(_config_error(
                f"tools.{key}", source,
                "unknown [tools] key; ignored"))
            continue
        if key in (*APPROVAL_KEYS, *PROBE_KEYS, BRIEF_KEY):
            if not isinstance(value, bool):
                errors.append(_config_error(
                    f"tools.{key}", source,
                    "must be true or false; using the default"))
                continue
            clean[key] = value
            continue
        if key == "preferred":
            if not isinstance(value, dict):
                errors.append(_config_error(
                    "tools.preferred", source,
                    "must be a table of capability -> tool id list; ignored"))
                continue
            if len(value) > 64:
                errors.append(_config_error(
                    "tools.preferred", source,
                    "exceeds the 64-entry limit; ignored"))
                continue
            preferred: dict[str, list[str]] = {}
            for capability, raw_ids in sorted(value.items()):
                if capability not in _CAPABILITIES_BY_ID:
                    errors.append(_config_error(
                        f"tools.preferred.{capability}", source,
                        "unknown capability; ignored"))
                    continue
                if (not isinstance(raw_ids, list)
                        or not all(isinstance(item, str) for item in raw_ids)):
                    errors.append(_config_error(
                        f"tools.preferred.{capability}", source,
                        "must be a list of tool ids; ignored"))
                    continue
                ids: list[str] = []
                for item in raw_ids[:MAX_PREFERRED_ENTRIES]:
                    if item not in _CAPABILITY_TOOLS[capability]:
                        errors.append(_config_error(
                            f"tools.preferred.{capability}", source,
                            f"tool {item!r} does not provide this "
                            "capability; ignored"))
                        continue
                    if item not in ids:
                        ids.append(item)
                if ids:
                    preferred[capability] = ids
            clean["preferred"] = preferred
    return clean, errors, unknown


def _tools_config(root: Path) -> dict[str, Any]:
    section = load_config_section(root, TOOLS_SECTION)
    errors: list[dict[str, str]] = [
        _config_error(f"tools.{error.kind}", error.kind,
                      f"cannot read {error.path}: {error.message}")
        for error in section.errors
    ]
    unknown: list[dict[str, str]] = []
    # Validate every file's table, including a value a higher-priority file
    # overrides, then the effective merged values.
    for kind in ("legacy", "repo"):
        table = section.values_by_source.get(kind)
        if table is None:
            continue
        _clean, source_errors, source_unknown = _validate_tools_table(
            table, kind)
        errors.extend(source_errors)
        unknown.extend(source_unknown)
    clean, effective_errors, effective_unknown = _validate_tools_table(
        section.values, "merged" if section.values_by_source else "default")
    if not section.values_by_source:
        # Without a source table this is the only validation pass. With one,
        # every effective key came from a source table already validated
        # above, so reporting again under "merged" would duplicate errors.
        errors.extend(effective_errors)
        unknown.extend(effective_unknown)
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in errors:
        identity = (item["key"], item["source"], item["message"])
        if identity not in seen:
            seen.add(identity)
            deduped.append(item)
    unknown_deduped: list[dict[str, str]] = []
    seen_unknown: set[tuple[str, str]] = set()
    for item in unknown:
        identity = (item["key"], item["source"])
        if identity not in seen_unknown:
            seen_unknown.add(identity)
            unknown_deduped.append(item)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key in (*APPROVAL_KEYS, *PROBE_KEYS):
        if key in clean and isinstance(clean[key], bool):
            values[key] = clean[key]
            sources[key] = section.source_of(key) or (
                "merged" if section.values_by_source else "default")
        else:
            values[key] = False if key in APPROVAL_KEYS else True
            sources[key] = "default"
    values[BRIEF_KEY] = clean.get(BRIEF_KEY, True)
    sources[BRIEF_KEY] = (
        section.source_of(BRIEF_KEY) or
        ("merged" if BRIEF_KEY in section.values else "default"))
    values["preferred"] = clean.get("preferred", {})
    sources["preferred"] = (
        section.source_of("preferred") or
        ("merged" if "preferred" in section.values else "default"))
    return {
        "section": TOOLS_SECTION,
        "paths": {
            "legacy": str(section.legacy) if section.legacy else None,
            "repo": str(section.repo),
        },
        "sources": sources,
        "errors": deduped,
        "unknown": unknown_deduped,
        "values": values,
    }


def brief_enabled(root: str | Path) -> bool:
    """Whether the session brief may add its ``[ozagia] tools:`` block.

    ``[tools] brief = false`` (repository or legacy config) turns it off; it
    is on by default. A broken config falls back to on, like the other
    ``[tools]`` settings, and never raises here.
    """
    try:
        config = _tools_config(Path(root).resolve())
        return bool(config["values"].get(BRIEF_KEY, True))
    except Exception:  # noqa: BLE001 - a brief must not fail for config
        return True


def _approval(config: Mapping[str, Any],
              environ: Mapping[str, str]) -> dict[str, Any]:
    values = config["values"]
    sources = config["sources"]
    overrides: list[str] = []
    report: dict[str, Any] = {}
    for key in APPROVAL_KEYS:
        value = bool(values.get(key, False))
        source = str(sources.get(key, "default"))
        if source in ("legacy", "repo", "merged"):
            source = "config"
        variable = APPROVAL_ENV[key]
        raw = str(environ.get(variable, "")).strip().lower()
        if raw in TRUTHY:
            value = True
            source = "environment"
            overrides.append(variable)
        report[key] = value
        report.setdefault("sources", {})[key] = source
    report["environment_overrides"] = sorted(overrides)
    return report


def detect_environment(
    root: str | Path, *, which: Callable[[str], Any] | None = None,
    module_finder: Callable[[str], Any] | None = None,
    runner: Callable[..., Any] | None = None,
    probe_versions: bool = True, probe_sudo: bool = True,
    environ: Mapping[str, str] | None = None,
    os_release_path: str | Path | None = None,
) -> dict[str, Any]:
    """Report the host, package managers, sudo state and ``[tools]`` approval.

    ``which`` and ``module_finder`` are injectable for tests. ``runner`` is
    called as ``runner(argv, timeout=..., cwd=..., env=..., stdin=...)``;
    older three-keyword runners are accepted. ``probe_versions`` and
    ``probe_sudo`` can only turn probing off: a configured ``false`` wins over
    their default ``true``. ``environ`` defaults to ``os.environ`` and is also
    used for the approval env overrides and the sanitized probe environment.
    """
    root_p = Path(root).resolve()
    env_map = os.environ if environ is None else environ
    which_fn = which or shutil.which
    config = _tools_config(root_p)
    approval = _approval(config, env_map)
    configured = config["values"]
    versions_enabled = bool(probe_versions) and bool(
        configured.get("probe_versions", True))
    sudo_enabled = bool(probe_sudo) and bool(
        configured.get("probe_sudo", True))
    manager_report = _detect_managers(which_fn)
    platform_report = _platform_report(os_release_path, env_map)
    sudo_path = _safe_which(which_fn, "sudo")
    sudo_noninteractive = False
    if sudo_enabled and sudo_path is not None:
        sudo_noninteractive = _sudo_probe(
            sudo_path, root=root_p, runner=runner, environ=env_map)
    preferred = {
        capability: list(ids)
        for capability, ids in sorted(
            config["values"].get("preferred", {}).items())
    }
    probe_sources = {
        "versions": config["sources"].get("probe_versions", "default"),
        "sudo": config["sources"].get("probe_sudo", "default"),
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "root": str(root_p),
        "platform": platform_report,
        "python": _python_report(manager_report, env_map),
        "managers": manager_report,
        "sudo": {
            "path": sudo_path,
            "available": sudo_path is not None,
            "noninteractive": sudo_noninteractive,
            "probed": bool(sudo_enabled and sudo_path is not None),
            "note": (
                "sudo -n true checks non-interactive usability for at most "
                f"{SUDO_TIMEOUT_S:g}s; it can update the sudo timestamp and "
                "never reads a password"),
        },
        "approval": approval,
        "config": config,
        "probe": {
            "versions": versions_enabled,
            "sudo": sudo_enabled,
            "sources": probe_sources,
        },
        "probe_versions": versions_enabled,
        "probe_sudo": sudo_enabled,
        "preferred": preferred,
    }


# --------------------------------------------------------------------------- #
# plans
# --------------------------------------------------------------------------- #
def _normalize_capabilities(
    values: Iterable[str] | str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    items = (values,) if isinstance(values, str) else tuple(values)
    if len(items) > MAX_CAPABILITIES:
        raise ToolchainError(
            f"at most {MAX_CAPABILITIES} capabilities may be requested")
    cleaned: list[str] = []
    groups: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ToolchainError("capability ids must be non-empty strings")
        wanted = item.strip().lower()
        if wanted in _GROUPS_BY_ID:
            if wanted not in groups:
                groups.append(wanted)
            for child in _GROUP_CAPABILITIES[wanted]:
                if child not in cleaned:
                    cleaned.append(child)
            continue
        if wanted not in _CAPABILITIES_BY_ID:
            raise ToolchainError(
                f"unknown capability {item!r}; known capabilities: "
                + ", ".join(capability_ids())
                + "; groups: " + ", ".join(group_ids()))
        if wanted not in cleaned:
            cleaned.append(wanted)
    return tuple(sorted(cleaned)), tuple(sorted(groups))


def _normalize_paths(values: Iterable[str | Path] | str | Path
                     ) -> tuple[str, ...]:
    if isinstance(values, (str, Path)):
        items: tuple[Any, ...] = (values,)
    else:
        items = tuple(values)
    if len(items) > MAX_PATHS:
        raise ToolchainError(f"at most {MAX_PATHS} paths may be requested")
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, (str, Path)):
            raise ToolchainError("paths must be strings")
        text = str(item)
        if not text:
            raise ToolchainError("paths must be non-empty")
        if len(text) > MAX_PATH_CHARS:
            raise ToolchainError(
                f"a path exceeds the {MAX_PATH_CHARS}-character limit")
        if "\x00" in text:
            raise ToolchainError("paths must not contain NUL bytes")
        if text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def _manager_info(manager_id: str, managers: Mapping[str, Any]
                  ) -> dict[str, Any] | None:
    spec = _MANAGERS[manager_id]
    for executable in spec.executables:
        found = managers.get(executable)
        if found is not None:
            return found
    return None


def _manager_rank(manager_id: str, environment: Mapping[str, Any]) -> int:
    """User-level first, then the distro manager, then Homebrew on macOS."""
    if manager_id == "uv":
        return 0
    if manager_id == "pip":
        return 1
    if manager_id == "pipx":
        return 2
    if manager_id == "npm":
        return 3
    platform = environment.get("platform") or {}
    family = str(platform.get("family") or "")
    distro = platform.get("distro") or {}
    distro_id = str(distro.get("id") or "").lower()
    distro_like = [str(item).lower() for item in distro.get("id_like") or []]
    distro_manager = next(
        (_DISTRO_MANAGERS[item] for item in (distro_id, *distro_like)
         if item in _DISTRO_MANAGERS), None)
    if manager_id == "brew":
        return 10 if family == "macos" else 30
    if distro_manager == manager_id:
        return 20
    if _MANAGERS[manager_id].requires_sudo:
        return 40
    return 50


def _tool_order(tools: Sequence[str], preferred: Sequence[str]) -> dict[str, int]:
    rank = {tool_id: index for index, tool_id in enumerate(preferred)}
    ordered = sorted(
        tools, key=lambda item: (
            0 if item in rank else 1, rank.get(item, 0), item))
    return {tool_id: index for index, tool_id in enumerate(ordered)}


def _tool_presence(tool: ToolSpec, *, which: Callable[[str], Any],
                   module_finder: Callable[[str], Any]) -> dict[str, Any]:
    if tool.kind == "binary":
        for probe in tool.probes:
            path = _safe_which(which, probe)
            if path is not None:
                return {"available": True, "probe": probe, "path": path,
                        "module": None}
        return {"available": False, "probe": None, "path": None,
                "module": None}
    for probe in tool.probes:
        spec = _safe_find_spec(module_finder, probe)
        if spec is not None:
            return {"available": True, "probe": probe, "path": None,
                    "module": probe}
    return {"available": False, "probe": None, "path": None, "module": None}


@dataclass
class _PlanState:
    environment: dict[str, Any]
    root: Path
    which: Callable[[str], Any]
    module_finder: Callable[[str], Any]
    runner: Callable[..., Any] | None
    environ: Mapping[str, str]
    presence: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions: dict[str, str | None] = field(default_factory=dict)

    def inspect(self, tool: ToolSpec) -> dict[str, Any]:
        if tool.id not in self.presence:
            self.presence[tool.id] = _tool_presence(
                tool, which=self.which, module_finder=self.module_finder)
        presence = self.presence[tool.id]
        if tool.id not in self.versions:
            self.versions[tool.id] = (
                _probe_version(
                    tool, presence, root=self.root, runner=self.runner,
                    environ=self.environ)
                if self.environment["probe"]["versions"] else None)
        return presence


def _install_options(tool: ToolSpec, state: _PlanState) -> list[dict[str, Any]]:
    environment = state.environment
    managers = environment["managers"]
    approval = environment["approval"]
    sudo_usable = bool(environment["sudo"]["noninteractive"])
    options: list[dict[str, Any]] = []
    for install in tool.installs:
        manager = _manager_info(install.manager, managers)
        available = manager is not None
        blockers: set[str] = set()
        if not available:
            blockers.add("manager")
        if not approval["allow_install"]:
            blockers.add("install")
        if install.requires_network and not approval["allow_network"]:
            blockers.add("network")
        if install.requires_sudo and not (
                approval["allow_sudo"] and sudo_usable):
            blockers.add("sudo")
        blocked_by = [name for name in BLOCK_ORDER if name in blockers]
        reasons: list[str] = []
        if "install" in blockers:
            reasons.append("installation not approved")
        if "network" in blockers:
            reasons.append("network access not approved")
        if "sudo" in blockers:
            reasons.append(
                "sudo not approved" if not approval["allow_sudo"]
                else "sudo not usable non-interactively")
        if "manager" in blockers:
            reasons.append(f"package manager {install.manager} not installed")
        options.append({
            "tool": tool.id,
            "manager": install.manager,
            "executable": (
                manager["executable"] if manager else
                _MANAGERS[install.manager].executables[0]),
            "path": manager["path"] if manager else None,
            "package": install.package,
            "command": install.rendered(),
            "available": available,
            "requires_sudo": install.requires_sudo,
            "requires_network": install.requires_network,
            "blocked_by": blocked_by,
            "blocked_reasons": reasons,
            "would_run": False,
            "recommended": False,
            "extra": install.extra,
            "install_kind": install.kind,
            "notes": install.notes,
        })
    options.sort(key=lambda item: (
        _manager_rank(item["manager"], environment), item["manager"],
        item["command"]))
    feasible = [item for item in options if not item["blocked_by"]]
    if feasible:
        feasible[0]["recommended"] = True
    return options


def _tool_record(tool: ToolSpec, state: _PlanState,
                 presence: Mapping[str, Any], preferred: Sequence[str],
                 missing_requirements: Sequence[str] = ()
                 ) -> dict[str, Any]:
    return {
        "id": tool.id,
        "title": tool.title,
        "kind": tool.kind,
        "format": _tool_format(tool),
        "path": presence.get("path"),
        "module": presence.get("module"),
        "probe": presence.get("probe"),
        "version": state.versions.get(tool.id),
        "provides": list(tool.provides),
        "writes": tool.writes,
        "distribution": tool.distribution,
        "extra": tool.extra,
        "install_kind": (
            "python-extra" if tool.extra else
            "python-package" if tool.kind == "python" else "system-package"),
        "remote": tool.remote,
        "credentialed": tool.credentialed,
        "probeable": tool.probeable,
        "requires": list(tool.requires),
        "missing_requirements": list(missing_requirements),
        "usable": not missing_requirements,
        "requires_network": tool.requires_network,
        "native_for": list(tool.native_for),
        "preferred_by": list(tool.preferred_by),
        "workflows": list(tool.workflows),
        "recommended": False,
        "note": tool.note,
        "preferred": tool.id in preferred,
    }


def _missing_record(tool: ToolSpec) -> dict[str, Any]:
    return {
        "id": tool.id,
        "title": tool.title,
        "kind": tool.kind,
        "format": _tool_format(tool),
        "probes": list(tool.probes),
        "provides": list(tool.provides),
        "writes": tool.writes,
        "distribution": tool.distribution,
        "extra": tool.extra,
        "requires": list(tool.requires),
        "requires_network": tool.requires_network,
        "native_for": list(tool.native_for),
        "preferred_by": list(tool.preferred_by),
        "workflows": list(tool.workflows),
        "note": tool.note,
    }


def _remote_record(tool: ToolSpec) -> dict[str, Any]:
    return {
        "id": tool.id,
        "title": tool.title,
        "kind": tool.kind,
        "format": _tool_format(tool),
        "provides": list(tool.provides),
        "remote": True,
        "credentialed": tool.credentialed,
        "probeable": False,
        "requires_network": True,
        "native_for": list(tool.native_for),
        "preferred_by": list(tool.preferred_by),
        "workflows": list(tool.workflows),
        "note": tool.note,
    }


def _unprobed_record(tool: ToolSpec) -> dict[str, Any]:
    return {
        "id": tool.id,
        "title": tool.title,
        "kind": tool.kind,
        "format": _tool_format(tool),
        "provides": list(tool.provides),
        "remote": False,
        "credentialed": tool.credentialed,
        "probeable": False,
        "requires_network": tool.requires_network,
        "native_for": list(tool.native_for),
        "preferred_by": list(tool.preferred_by),
        "workflows": list(tool.workflows),
        "note": tool.note,
    }


def tool_plan(
    root: str | Path, *, capabilities: Iterable[str] | str = (),
    paths: Iterable[str | Path] | str | Path = (),
    which: Callable[[str], Any] | None = None,
    module_finder: Callable[[str], Any] | None = None,
    runner: Callable[..., Any] | None = None,
    probe_versions: bool = True, probe_sudo: bool = True,
    environ: Mapping[str, str] | None = None,
    os_release_path: str | Path | None = None,
) -> dict[str, Any]:
    """Plan the relevant tools for explicit capabilities and/or file paths.

    Only the tools of the requested capabilities are probed. Every install
    option is a preview (``would_run`` is always false); this function never
    installs anything and never uses the network itself.
    """
    root_p = Path(root).resolve()
    env_map = os.environ if environ is None else environ
    explicit, explicit_groups = (
        _normalize_capabilities(capabilities) if capabilities else ((), ()))
    normalized_paths = _normalize_paths(paths) if paths else ()
    inferred, unmatched = infer_capabilities(normalized_paths)
    requested = tuple(sorted(set(explicit) | set(inferred)))
    environment = detect_environment(
        root_p, which=which, module_finder=module_finder, runner=runner,
        probe_versions=probe_versions, probe_sudo=probe_sudo,
        environ=env_map, os_release_path=os_release_path)
    preferred = environment.get("preferred") or {}
    state = _PlanState(
        environment=environment, root=root_p,
        which=which or shutil.which,
        module_finder=module_finder or importlib.util.find_spec,
        runner=runner, environ=env_map)

    capability_reports: list[dict[str, Any]] = []
    for capability_id in requested:
        capability = _CAPABILITIES_BY_ID[capability_id]
        preferred_ids = preferred.get(capability_id, [])
        order = _tool_order(_CAPABILITY_TOOLS[capability_id], preferred_ids)
        tools = sorted(_CAPABILITY_TOOLS[capability_id],
                       key=lambda item: order[item])
        available: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        installs: list[dict[str, Any]] = []
        remote: list[dict[str, Any]] = []
        unprobed: list[dict[str, Any]] = []
        for tool_id in tools:
            tool = _TOOLS_BY_ID[tool_id]
            if tool.remote:
                remote.append(_remote_record(tool))
                continue
            if not tool.probeable:
                unprobed.append(_unprobed_record(tool))
                continue
            presence = state.inspect(tool)
            if presence["available"]:
                missing_requirements = [
                    requirement for requirement in tool.requires
                    if not state.inspect(
                        _TOOLS_BY_ID[requirement])["available"]]
                available.append(_tool_record(
                    tool, state, presence, preferred_ids,
                    missing_requirements))
            else:
                missing.append(_missing_record(tool))
                installs.extend(_install_options(tool, state))
        installs.sort(key=lambda item: (
            order[item["tool"]],
            _manager_rank(item["manager"], environment),
            item["manager"], item["command"]))
        usable = [item for item in available if item["usable"]]
        ready = bool(usable)
        installable = any(not item["blocked_by"] for item in installs)
        recommended_tool = next(
            (item for item in usable if item.get("extra")),
            usable[0] if usable else None)
        if recommended_tool is not None:
            recommended_tool["recommended"] = True
            available = [recommended_tool, *(
                item for item in available if item is not recommended_tool)]
        recommended_install = next(
            (item for item in installs
             if item["recommended"] and item["install_kind"] == "python-extra"),
            None)
        if recommended_install is None:
            recommended_install = next(
                (item for item in installs if item["recommended"]), None)
        if recommended_tool is not None:
            recommended_path = recommended_tool["install_kind"]
        elif recommended_install is not None:
            recommended_path = recommended_install["install_kind"]
        else:
            recommended_path = None
        capability_reports.append({
            "id": capability.id,
            "title": capability.title,
            "description": capability.description,
            "group": capability.group,
            "ready": ready,
            "installable": installable,
            "blocked": (not ready) and not installable,
            "preferred": list(preferred_ids),
            "recommended_tool": (
                recommended_tool["id"] if recommended_tool else None),
            "recommended_path": recommended_path,
            "recommended_install": recommended_install,
            "available": available,
            "missing": missing,
            "remote": remote,
            "unprobed": unprobed,
            "install": installs,
        })

    ready_count = sum(1 for item in capability_reports if item["ready"])
    missing_count = len(capability_reports) - ready_count
    blocked_count = sum(
        1 for item in capability_reports if item["blocked"])
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "root": str(root_p),
        "request": {
            "capabilities": list(requested),
            "groups": list(explicit_groups),
            "explicit": list(explicit),
            "inferred": list(inferred),
            "paths": list(normalized_paths),
            "unmatched_paths": unmatched,
        },
        "summary": {
            "capabilities": len(capability_reports),
            "ready": ready_count,
            "missing": missing_count,
            "blocked": blocked_count,
            "installable": sum(
                1 for item in capability_reports
                if item["installable"] and not item["ready"]),
            "available_tools": len({
                item["id"] for report in capability_reports
                for item in report["available"]}),
            "usable_tools": len({
                item["id"] for report in capability_reports
                for item in report["available"] if item["usable"]}),
            "missing_tools": len({
                item["id"] for report in capability_reports
                for item in report["missing"]}),
            "remote_providers": len({
                item["id"] for report in capability_reports
                for item in report["remote"]}),
            "unprobed_providers": len({
                item["id"] for report in capability_reports
                for item in report["unprobed"]}),
        },
        "capabilities": capability_reports,
        "environment": environment,
        "not_assessed": list(NOT_ASSESSED),
    }


# --------------------------------------------------------------------------- #
# model-facing text
# --------------------------------------------------------------------------- #
def _approval_text(environment: Mapping[str, Any]) -> str:
    approval = environment.get("approval") or {}
    return (
        "install " + ("approved" if approval.get("allow_install")
                       else "not approved")
        + ", network " + ("approved" if approval.get("allow_network")
                          else "not approved")
        + ", sudo " + ("approved" if approval.get("allow_sudo")
                       else "not approved"))


def _sudo_text(environment: Mapping[str, Any]) -> str:
    sudo = environment.get("sudo") or {}
    if not sudo.get("available"):
        return "sudo missing"
    if not sudo.get("probed"):
        return "sudo not probed"
    return ("sudo usable" if sudo.get("noninteractive")
            else "sudo not usable non-interactively")


def _environment_line(environment: Mapping[str, Any]) -> str:
    platform = environment.get("platform") or {}
    distro = platform.get("distro") or {}
    system = str(platform.get("system") or "unknown")
    arch = str(platform.get("arch") or "")
    label = system + (f" {arch}" if arch else "")
    pretty = distro.get("pretty_name") or distro.get("name")
    if distro.get("id"):
        distro_text = f"{pretty} (id {distro['id']})" if pretty else (
            f"id {distro['id']}")
        label += f", {distro_text}"
    managers = sorted((environment.get("managers") or {}))
    manager_text = ", ".join(managers[:8]) if managers else "none detected"
    if len(managers) > 8:
        manager_text += f", +{len(managers) - 8} more"
    return (f"{label}; managers: {manager_text}; {_sudo_text(environment)}; "
            f"{_approval_text(environment)}")


def _short_provides(tool: Mapping[str, Any], limit: int = 4) -> str:
    provides = [str(item) for item in tool.get("provides") or []]
    text = ", ".join(provides[:limit])
    if len(provides) > limit:
        text += f", +{len(provides) - limit} more"
    return text or "tool"


def _install_kind_label(item: Mapping[str, Any]) -> str:
    kind = str(item.get("install_kind") or "")
    if kind == "python-extra":
        return f"Python extra {item.get('extra')}"
    if kind == "python-package":
        return "Python package"
    if kind == "npm-package":
        return "npm package"
    if kind == "system-package":
        return "system package"
    return "toolchain package"


def _tool_kind_label(tool: Mapping[str, Any]) -> str:
    if tool.get("install_kind") == "python-extra":
        return f"Python extra {tool.get('extra')}"
    if tool.get("kind") == "python":
        return "Python library"
    return "system package"


def _tool_line(tool: Mapping[str, Any]) -> str:
    version = f" {tool['version']}" if tool.get("version") else ""
    if tool.get("install_kind") == "python-extra":
        where = f" ({_tool_kind_label(tool)})"
    elif tool.get("path"):
        where = f" ({tool['path']})"
    elif tool.get("kind") == "python":
        where = " (Python library)"
    else:
        where = ""
    marker = "* " if tool.get("recommended") else ""
    suffix = ""
    if not tool.get("usable", True):
        needs = ", ".join(
            str(item) for item in tool.get("missing_requirements") or [])
        suffix = f" (unusable: needs {needs or 'a dependency'})"
    return (f"{marker}{tool['id']}{version}{where}: "
            f"{_short_provides(tool)}{suffix}")


def _provider_line(provider: Mapping[str, Any]) -> str:
    requirement = ("network/credentials"
                   if provider.get("credentialed") else "network")
    text = (f"{provider['id']} [{provider.get('format')}; requires "
            f"{requirement}]")
    workflows = [str(item) for item in provider.get("workflows") or []]
    if workflows:
        text += ": " + ", ".join(workflows[:3])
    return text


def _install_choice(capability: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One option per missing tool: the recommended or the first preview."""
    choices: list[dict[str, Any]] = []
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for option in capability.get("install") or []:
        by_tool.setdefault(str(option["tool"]), []).append(option)
    for tool_id in [str(item["id"]) for item in capability.get("missing") or []]:
        options = by_tool.get(tool_id, [])
        if not options:
            choices.append({"tool": tool_id, "none": True})
            continue
        chosen = next(
            (item for item in options if item.get("recommended")), None)
        if chosen is None:
            chosen = next(
                (item for item in options if item.get("available")),
                options[0])
        choices.append(chosen)
    return choices


def tool_plan_text(plan: Mapping[str, Any]) -> str:
    """A bounded, deterministic text plan for a person or a model."""
    request = plan.get("request") or {}
    lines = [
        "[ozagia] toolchain plan",
        "environment: " + _environment_line(plan.get("environment") or {}),
    ]
    capabilities = [str(item) for item in request.get("capabilities") or []]
    paths = [str(item) for item in request.get("paths") or []]
    request_text = ", ".join(capabilities) if capabilities else "none"
    groups = [str(item) for item in request.get("groups") or []]
    if groups:
        request_text += " (groups: " + ", ".join(groups) + ")"
    if paths:
        shown = ", ".join(paths[:3])
        if len(paths) > 3:
            shown += f", +{len(paths) - 3} more"
        request_text += f" (paths: {shown})"
    lines.append("request: " + request_text)
    reports = plan.get("capabilities") or []
    if not reports:
        lines.append(
            "no capabilities requested; pass --capability or --path to probe "
            "relevant tools")
    for report in reports:
        remote = report.get("remote") or []
        unprobed = report.get("unprobed") or []
        if report.get("ready"):
            status = "ready"
        elif remote and not report.get("missing") and not report.get(
                "available"):
            status = "remote only (requires network/credentials)"
        elif unprobed and not report.get("missing") and not report.get(
                "available"):
            status = "unprobed (local presence not checked)"
        else:
            status = ("missing (blocked)" if report.get("blocked")
                      else "missing")
        recommended = report.get("recommended_tool")
        recommended_tool = next(
            (item for item in report.get("available") or []
             if item.get("id") == recommended), None)
        if recommended_tool is not None:
            path_label = _tool_kind_label(recommended_tool)
        elif isinstance(report.get("recommended_install"), Mapping):
            path_label = _install_kind_label(report["recommended_install"])
        else:
            path_label = None
        if path_label:
            status += f" (recommended path: {path_label})"
        lines.append(f"{report['id']}: {status}")
        available = report.get("available") or []
        if available:
            lines.append("  available: " + "; ".join(
                _tool_line(item) for item in available[:MAX_TEXT_TOOLS]))
            if len(available) > MAX_TEXT_TOOLS:
                lines.append(
                    f"  ... and {len(available) - MAX_TEXT_TOOLS} more "
                    "available")
        missing = report.get("missing") or []
        if missing:
            shown_missing = ", ".join(
                str(item["id"]) for item in missing[:MAX_TEXT_TOOLS])
            if len(missing) > MAX_TEXT_TOOLS:
                shown_missing += (
                    f", +{len(missing) - MAX_TEXT_TOOLS} more")
            lines.append("  missing: " + shown_missing)
        if remote:
            lines.append("  remote providers (not connected): " + "; ".join(
                _provider_line(item) for item in remote[:MAX_TEXT_TOOLS]))
            if len(remote) > MAX_TEXT_TOOLS:
                lines.append(
                    f"  ... and {len(remote) - MAX_TEXT_TOOLS} more remote")
        if unprobed:
            lines.append(
                "  unprobed local providers (presence not checked): "
                + ", ".join(str(item["id"]) for item in unprobed))
        if not report.get("ready"):
            choices = _install_choice(report)
            if choices:
                lines.append("  install previews:")
                for choice in choices[:MAX_TEXT_INSTALLS]:
                    if choice.get("none"):
                        lines.append(
                            f"    {choice['tool']}: no package-manager "
                            "option recorded")
                        continue
                    tool_id = str(choice["tool"])
                    kind = _install_kind_label(choice)
                    if not (plan.get("environment") or {}).get(
                            "approval", {}).get("allow_install"):
                        lines.append(
                            f"    {tool_id} [{kind}]: installation is not "
                            f"approved ({', '.join(choice['blocked_by']) or 'blocked'})")
                        continue
                    line = f"    {tool_id} [{kind}]: {choice['command']}"
                    if choice.get("recommended"):
                        line += " (recommended)"
                    if choice.get("blocked_reasons"):
                        line += (" - blocked: "
                                 + "; ".join(choice["blocked_reasons"]))
                    lines.append(line)
                if len(choices) > MAX_TEXT_INSTALLS:
                    lines.append(
                        f"    ... and {len(choices) - MAX_TEXT_INSTALLS} more")
    summary = plan.get("summary") or {}
    if summary:
        lines.append(
            "summary: {ready} ready, {missing} missing, {blocked} blocked "
            "of {capabilities} capabilit{plural}".format(
                ready=summary.get("ready", 0),
                missing=summary.get("missing", 0),
                blocked=summary.get("blocked", 0),
                capabilities=summary.get("capabilities", 0),
                plural="y" if summary.get("capabilities") == 1 else "ies"))
    lines.append("not checked: " + "; ".join(
        str(item) for item in plan.get("not_assessed") or []))
    return "\n".join(lines) + "\n"


def _brief_tool(report: Mapping[str, Any], model_family: str | None
                ) -> Mapping[str, Any] | None:
    usable = [item for item in report.get("available") or []
              if item.get("usable", True)]
    if not usable:
        return None
    if model_family:
        wanted = model_family.lower()
        match = next(
            (item for item in usable
             if wanted in [str(name).lower()
                           for name in item.get("native_for") or []]
             or wanted in [str(name).lower()
                           for name in item.get("preferred_by") or []]),
            None)
        if match is not None:
            return match
    recommended = next(
        (item for item in usable if item.get("recommended")), None)
    return recommended or usable[0]


def brief_lines(plan: Mapping[str, Any], *,
                model_family: str | None = None) -> list[str]:
    """Compact model-facing lines: environment, readiness, providers, installs.

    ``model_family`` (``claude``, ``codex``, ``gemini``, ``cursor``,
    ``aider``, ``openhands`` or ``local``) selects a tool whose registry
    ``native_for``/``preferred_by`` metadata names that family. The line count
    stays small on purpose: extra tools and MCP servers cost context and
    accuracy.
    """
    environment = plan.get("environment") or {}
    lines = ["[ozagia] tools: " + _environment_line(environment)]
    reports = plan.get("capabilities") or []
    if not reports:
        lines.append(
            "no external-tool capability requested; pass a path or a "
            "capability such as pdf")
        return lines
    family = model_family.strip().lower() if model_family else None
    if family:
        family = MODEL_FAMILY_ALIASES.get(family, family)
    for report in reports[:MAX_BRIEF_CAPABILITIES]:
        available = report.get("available") or []
        remote = report.get("remote") or []
        unprobed = report.get("unprobed") or []
        if report.get("ready"):
            chosen = _brief_tool(report, family)
            if chosen is None:
                continue
            label = (f"for {family} prefer " if family else "prefer ")
            useful = (f"{chosen['id']} ({_tool_kind_label(chosen)}: "
                      f"{_short_provides(chosen, 3)})")
            others = [str(item["id"]) for item in available
                      if item is not chosen and item.get("usable", True)][:3]
            line = f"{report['id']}: ready; {label}{useful}"
            if others:
                line += "; also " + ", ".join(others)
            lines.append(line)
            continue
        missing_items = [str(item["id"])
                         for item in report.get("missing") or []]
        missing = ", ".join(missing_items[:5])
        if len(missing_items) > 5:
            missing += f", +{len(missing_items) - 5} more"
        unusable = [item for item in available if not item.get("usable", True)]
        unusable_text = ""
        if unusable:
            unusable_text = "installed but unusable: " + ", ".join(
                f"{item['id']} (needs "
                f"{', '.join(item.get('missing_requirements') or []) or 'a dependency'})"
                for item in unusable[:2]) + "; "
        remote_text = ", ".join(
            f"{item['id']} (network/credentials)"
            if item.get("credentialed") else f"{item['id']} (network)"
            for item in remote[:3])
        if not missing and (remote or unprobed):
            kind = "remote only" if remote else "unprobed"
            providers = remote_text or ", ".join(
                str(item["id"]) for item in unprobed[:3])
            lines.append(
                f"{report['id']}: {kind}; {unusable_text}{providers}")
            continue
        choices = _install_choice(report)
        approved = bool((environment.get("approval") or {}).get(
            "allow_install"))
        if not approved:
            path = ""
            if isinstance(report.get("recommended_install"), Mapping):
                path = (f"; recommended "
                        f"{_install_kind_label(report['recommended_install'])}")
            lines.append(
                f"{report['id']}: missing; {unusable_text}{missing}{path}; "
                "installation not approved")
            if remote_text:
                lines.append(
                    f"  remote providers: {remote_text}")
            continue
        chosen = (report.get("recommended_install")
                  if isinstance(report.get("recommended_install"), Mapping)
                  else None)
        if chosen is None:
            chosen = next(
                (item for item in choices if not item.get("none")), None)
        if chosen is None:
            lines.append(
                f"{report['id']}: missing; {unusable_text}{missing}; no "
                "install option")
            continue
        line = (f"{report['id']}: missing; {unusable_text}{missing}; install "
                f"{chosen['tool']} with {chosen['command']} "
                f"({_install_kind_label(chosen)})")
        if chosen.get("blocked_reasons"):
            line += " (blocked: " + "; ".join(chosen["blocked_reasons"]) + ")"
        lines.append(line)
    if len(reports) > MAX_BRIEF_CAPABILITIES:
        lines.append(
            f"... and {len(reports) - MAX_BRIEF_CAPABILITIES} more capability "
            "reports")
    lines.append(
        "keep the provider count small: extra tools and MCP servers cost "
        "context and accuracy")
    lines.append(
        "not checked: versions, remote credentials/network and installation; "
        "nothing was installed")
    return lines
