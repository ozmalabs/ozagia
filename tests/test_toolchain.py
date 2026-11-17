"""Toolchain registry, environment detection and capability plans."""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from ozagia.core import toolchain as T


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeWhich:
    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = mapping or {}
        self.calls: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.calls.append(name)
        return self.mapping.get(name)


class FakeFinder:
    def __init__(self, available: set[str] | None = None):
        self.available = available or set()
        self.calls: list[str] = []

    def __call__(self, name: str):
        self.calls.append(name)
        return object() if name in self.available else None


class FakeRunner:
    """A runner that records argv and never receives a shell keyword."""

    def __init__(self, responses: dict[str, dict] | None = None,
                 default: dict | None = None):
        self.responses = responses or {}
        self.default = default or {"returncode": 0, "stdout": "", "stderr": ""}
        self.calls: list[dict] = []

    def __call__(self, argv: list[str], **kwargs):
        assert "shell" not in kwargs or kwargs["shell"] is False
        self.calls.append({"argv": list(argv), **kwargs})
        response = self.responses.get(argv[0], self.default)
        if isinstance(response, BaseException):
            raise response
        return response


def _config(root: Path, text: str) -> Path:
    path = root / "ozagia" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _os_release(root: Path) -> Path:
    path = root / "os-release"
    path.write_text(
        'NAME="Fixture Linux"\n'
        'PRETTY_NAME="Fixture Linux 9.1"\n'
        'ID=fixture\n'
        'ID_LIKE="debian ubuntu"\n'
        'VERSION_ID="9.1"\n',
        encoding="utf-8")
    return path


def _pdf_plan(root: Path, **kwargs):
    capabilities = kwargs.pop("capabilities", ["pdf"])
    kwargs.setdefault("which", FakeWhich())
    kwargs.setdefault("module_finder", FakeFinder())
    kwargs.setdefault("probe_versions", False)
    kwargs.setdefault("probe_sudo", False)
    kwargs.setdefault("environ", {})
    return T.tool_plan(root, capabilities=capabilities, **kwargs)


def _find_option(plan: dict, tool_id: str, manager: str) -> dict:
    for capability in plan["capabilities"]:
        for option in capability["install"]:
            if option["tool"] == tool_id and option["manager"] == manager:
                return option
    raise AssertionError(f"no {manager} option for {tool_id}")


# --------------------------------------------------------------------------- #
# registry consistency
# --------------------------------------------------------------------------- #
def test_registry_is_versioned_and_consistent() -> None:
    registry = T.registry()
    assert registry["schema"] == T.SCHEMA
    assert registry["schema_version"] == T.SCHEMA_VERSION
    assert registry["registry_version"] == T.REGISTRY_VERSION
    assert set(registry["capabilities"]) == {
        "archive", "audio", "browser", "communication", "data", "design",
        "diagram", "image", "ocr", "office", "ops", "pdf", "research",
        "video"}
    assert set(registry["groups"]) == {"documents", "media", "web"}
    assert len(registry["tools"]) == len(set(registry["tools"]))
    for tool_id, tool in registry["tools"].items():
        assert tool["id"] == tool_id
        assert tool["kind"] in ("binary", "python", "remote", "node")
        assert tool["format"] in (
            "library", "cli", "mcp", "remote", "api", "app")
        assert tool["provides"], tool_id
        assert isinstance(tool["writes"], bool)
        assert tool["capabilities"], tool_id
        assert isinstance(tool["remote"], bool)
        assert isinstance(tool["credentialed"], bool)
        assert isinstance(tool["probeable"], bool)
        for capability in tool["capabilities"]:
            assert capability in registry["capabilities"]
            assert tool_id in registry["capabilities"][capability]["tools"]
        if tool["remote"]:
            assert tool["probes"] == [], tool_id
            assert tool["installs"] == [], tool_id
            assert tool["probeable"] is False
            assert tool["format"] in ("mcp", "remote", "api", "app")
        else:
            if tool["probeable"]:
                assert tool["probes"], tool_id
            else:
                assert tool["probes"] == [], tool_id
            assert tool["installs"], tool_id
        for install in tool["installs"]:
            assert install["manager"] in registry["managers"]
            assert install["package"]
            assert "{package}" in install["command"]
            assert install["requires_network"] is True
    for capability_id, capability in registry["capabilities"].items():
        assert capability["id"] == capability_id
        assert capability["tools"], capability_id
        if capability.get("group"):
            assert capability["group"] in registry["groups"]
            assert capability_id in registry["groups"][
                capability["group"]]["capabilities"]
        for tool_id in capability["tools"]:
            assert tool_id in registry["tools"]
    for group_id, group in registry["groups"].items():
        assert group["id"] == group_id
        assert group["capabilities"]
        for capability_id in group["capabilities"]:
            assert capability_id in registry["capabilities"]
            assert registry["capabilities"][capability_id]["group"] == group_id


def test_registry_covers_the_required_tool_families() -> None:
    registry = T.registry()
    tools = registry["tools"]
    probes = {name for tool in tools.values() for name in tool["probes"]}
    for name in ("qpdf", "pdftk", "mutool", "pdftotext", "pdftoppm", "gs",
                 "pypdf", "pikepdf", "pdfplumber", "fitz", "reportlab",
                 "tesseract", "pytesseract", "ocrmypdf", "magick", "convert",
                 "ffmpeg", "PIL", "libreoffice", "soffice", "pandoc",
                 "7z", "unzip", "tar", "dot", "mmdc", "jq", "yq", "sqlite3",
                 "duckdb", "chromium", "playwright"):
        assert name in probes, name
    system_managers = {item["manager"] for item in tools["qpdf"]["installs"]}
    assert {"apt", "dnf", "pacman", "brew"} <= system_managers
    for tool_id in ("pypdf", "pikepdf", "pdfplumber", "fitz", "reportlab",
                    "pillow", "pytesseract", "faster-whisper", "opencv",
                    "pandas"):
        managers = {item["manager"] for item in tools[tool_id]["installs"]}
        assert {"uv", "pip"} <= managers, tool_id
    for tool_id in ("mmdc", "playwright"):
        managers = {item["manager"] for item in tools[tool_id]["installs"]}
        assert "npm" in managers, tool_id
    assert tools["exa"]["remote"] is True
    assert tools["exa"]["credentialed"] is True
    assert tools["playwright"]["native_for"]


def test_capability_extensions_are_unique() -> None:
    seen: dict[str, str] = {}
    for capability in T.registry()["capabilities"].values():
        for extension in capability["extensions"]:
            assert extension not in seen, extension
            seen[extension] = capability["id"]
    assert ".pdf" in seen and seen[".pdf"] == "pdf"
    assert ".png" in seen and seen[".png"] == "image"
    assert ".docx" in seen and seen[".docx"] == "office"
    assert ".mp4" in seen and seen[".mp4"] == "video"
    assert ".tar.gz" in seen and seen[".tar.gz"] == "archive"
    assert ".svg" in seen and seen[".svg"] == "diagram"
    assert ".ipynb" in seen and seen[".ipynb"] == "data"
    assert ".tf" in seen and seen[".tf"] == "ops"
    assert ".fig" in seen and seen[".fig"] == "design"
    assert ".eml" in seen and seen[".eml"] == "communication"


def test_groups_expand_to_child_capabilities(tmp_path: Path) -> None:
    documents = T.tool_plan(
        tmp_path, capabilities=["documents"], which=FakeWhich(),
        module_finder=FakeFinder(), probe_versions=False, probe_sudo=False,
        environ={})
    assert documents["request"]["groups"] == ["documents"]
    assert documents["request"]["capabilities"] == ["ocr", "office", "pdf"]
    assert {item["id"]: item for item in documents["capabilities"]}[
        "pdf"]["group"] == "documents"
    # A Node library is reported but not guessed at.
    assert "pdf-lib" in {
        item["id"] for item in documents["capabilities"][2]["unprobed"]}
    media = T.tool_plan(
        tmp_path, capabilities=["media"], which=FakeWhich(),
        module_finder=FakeFinder(), probe_versions=False, probe_sudo=False,
        environ={})
    assert media["request"]["groups"] == ["media"]
    assert media["request"]["capabilities"] == ["audio", "image", "video"]
    web = T.tool_plan(
        tmp_path, capabilities=["web"], which=FakeWhich(),
        module_finder=FakeFinder(), probe_versions=False, probe_sudo=False,
        environ={})
    assert web["request"]["groups"] == ["web"]
    assert web["request"]["capabilities"] == ["browser", "research"]


def test_remote_providers_are_not_probed_or_installed(tmp_path: Path) -> None:
    which = FakeWhich()
    plan = T.tool_plan(
        tmp_path, capabilities=["research"], which=which,
        module_finder=FakeFinder(), probe_versions=False, probe_sudo=False,
        environ={})
    capability = plan["capabilities"][0]
    assert capability["ready"] is False
    assert capability["missing"] == []
    assert capability["install"] == []
    assert capability["unprobed"] == []
    assert {"exa", "firecrawl", "context7"} <= {
        item["id"] for item in capability["remote"]}
    assert all(item["requires_network"] for item in capability["remote"])
    assert all(item["remote"] is True for item in capability["remote"])
    assert "exa" not in which.calls
    assert plan["summary"]["remote_providers"] >= 4
    text = T.tool_plan_text(plan)
    assert "research: remote only (requires network/credentials)" in text
    assert "remote providers (not connected)" in text
    brief = "\n".join(T.brief_lines(plan))
    assert "remote only" in brief
    assert "network/credentials" in brief
    assert "uv pip install" not in brief
    assert "apt-get install" not in brief


def test_pytesseract_is_unusable_without_the_tesseract_engine(
        tmp_path: Path) -> None:
    plan = T.tool_plan(
        tmp_path, capabilities=["ocr"], which=FakeWhich(),
        module_finder=FakeFinder({"pytesseract"}), probe_versions=False,
        probe_sudo=False, environ={})
    capability = plan["capabilities"][0]
    available = {item["id"]: item for item in capability["available"]}
    assert available["pytesseract"]["usable"] is False
    assert available["pytesseract"]["missing_requirements"] == ["tesseract"]
    assert capability["ready"] is False
    assert capability["recommended_tool"] is None
    assert "tesseract" in {item["id"] for item in capability["missing"]}

    ready = T.tool_plan(
        tmp_path, capabilities=["ocr"],
        which=FakeWhich({"tesseract": "/usr/bin/tesseract"}),
        module_finder=FakeFinder({"pytesseract"}), probe_versions=False,
        probe_sudo=False, environ={})
    capability = ready["capabilities"][0]
    available = {item["id"]: item for item in capability["available"]}
    assert available["pytesseract"]["usable"] is True
    assert capability["ready"] is True
    assert capability["recommended_tool"] == "pytesseract"


def test_brief_prefers_a_model_specific_provider(tmp_path: Path) -> None:
    plan = T.tool_plan(
        tmp_path, capabilities=["browser"],
        which=FakeWhich({"chromium": "/usr/bin/chromium",
                         "playwright": "/usr/bin/playwright"}),
        module_finder=FakeFinder(), probe_versions=False, probe_sudo=False,
        environ={})
    capability = plan["capabilities"][0]
    assert {"chromium", "playwright"} <= {
        item["id"] for item in capability["available"]}
    codex = "\n".join(T.brief_lines(plan, model_family="codex"))
    assert "for codex prefer playwright" in codex
    alias = "\n".join(T.brief_lines(plan, model_family="openai"))
    assert "for codex prefer playwright" in alias
    generic = "\n".join(T.brief_lines(plan))
    assert "prefer chromium" in generic
    assert "extra tools and MCP servers cost context" in codex


def test_python_extras_are_optional_and_not_forced_on_all_or_dev() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    assert pyproject["project"]["dependencies"] == []
    detection_text = json.dumps(extras) + json.dumps(
        pyproject["project"]["dependencies"])
    for name in ("cli-tool-audit", "whichcraft", "find-exe", "pox"):
        assert name not in detection_text
    assert "pypdf" in " ".join(extras["pdf"])
    assert "pdfplumber" in " ".join(extras["pdf"])
    assert "reportlab" in " ".join(extras["pdf"])
    assert any("Pillow" in item for item in extras["image"])
    assert any("python-docx" in item for item in extras["office"])
    assert any("pytesseract" in item for item in extras["ocr"])
    assert any("faster-whisper" in item for item in extras["media"])
    assert any("pandas" in item for item in extras["data"])
    for group in ("all", "dev"):
        assert all("pdf" not in item and "Pillow" not in item
                   and "pytesseract" not in item
                   and "faster-whisper" not in item
                   and "pandas" not in item
                   for item in extras[group])
    registry = T.registry()
    for tool_id, extra in (("pypdf", "pdf"), ("pdfplumber", "pdf"),
                           ("reportlab", "pdf"), ("pillow", "image"),
                           ("python-docx", "office"), ("openpyxl", "office"),
                           ("python-pptx", "office"),
                           ("pytesseract", "ocr"),
                           ("faster-whisper", "media"), ("opencv", "media"),
                           ("pandas", "data")):
        assert registry["tools"][tool_id]["extra"] == extra
        managers = {item["manager"] for item in
                    registry["tools"][tool_id]["installs"]}
        assert {"uv", "pip"} <= managers
        for install in registry["tools"][tool_id]["installs"]:
            assert install["extra"] == extra
            assert install["kind"] == "python-extra"
            assert install["package"] == f"ozagia[{extra}]"


# --------------------------------------------------------------------------- #
# environment detection
# --------------------------------------------------------------------------- #
def test_environment_reports_platform_managers_python_and_sudo(
        tmp_path: Path) -> None:
    which = FakeWhich({
        "apt-get": "/usr/bin/apt-get",
        "pip3": "/usr/bin/pip3",
        "uv": "/usr/local/bin/uv",
        "sudo": "/usr/bin/sudo",
    })
    runner = FakeRunner()
    environment = T.detect_environment(
        tmp_path, which=which, module_finder=FakeFinder(), runner=runner,
        environ={"PATH": "/usr/bin", "WSL_DISTRO_NAME": "Ubuntu"},
        os_release_path=_os_release(tmp_path))
    assert environment["platform"]["system"] == "Linux"
    assert environment["platform"]["family"] == "linux"
    assert environment["platform"]["distro"]["id"] == "fixture"
    assert environment["platform"]["distro"]["pretty_name"] == (
        "Fixture Linux 9.1")
    assert environment["platform"]["distro"]["id_like"] == ["debian", "ubuntu"]
    assert environment["platform"]["wsl"] is True
    assert "environment:WSL" in environment["platform"]["container_hints"]
    assert environment["managers"]["apt-get"]["family"] == "apt"
    assert environment["managers"]["pip3"]["family"] == "pip"
    assert environment["managers"]["uv"]["path"] == "/usr/local/bin/uv"
    assert environment["python"]["uv"] is True
    assert environment["sudo"]["path"] == "/usr/bin/sudo"
    assert environment["sudo"]["available"] is True
    assert environment["sudo"]["noninteractive"] is True
    assert environment["sudo"]["probed"] is True
    assert "sudo timestamp" in environment["sudo"]["note"]
    assert len(runner.calls) == 1
    assert runner.calls[0]["argv"] == ["/usr/bin/sudo", "-n", "true"]
    assert runner.calls[0]["timeout"] == 2.0
    assert runner.calls[0]["cwd"] == str(tmp_path.resolve())
    assert "OZAGIA_ALLOW_INSTALL" not in runner.calls[0]["env"]
    assert runner.calls[0]["env"]["LC_ALL"] == "C"
    assert environment["approval"]["allow_install"] is False
    assert environment["approval"]["sources"]["allow_install"] == "default"
    assert environment["probe"]["versions"] is True
    assert environment["probe"]["sudo"] is True


def environment_env_dict(value: dict) -> dict:
    """The probe env is sanitized; compare against the same sanitized shape."""
    return dict(value)


def test_sudo_false_missing_and_not_probed(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    base = dict(which=FakeWhich(), module_finder=FakeFinder(),
                environ={}, os_release_path=_os_release(tmp_path))
    missing = T.detect_environment(tmp_path, runner=FakeRunner(), **base)
    assert missing["sudo"]["available"] is False
    assert missing["sudo"]["noninteractive"] is False
    assert missing["sudo"]["probed"] is False

    which = FakeWhich({"sudo": "/usr/bin/sudo"})
    denied = FakeRunner(default={"returncode": 1, "stdout": "", "stderr": ""})
    report = T.detect_environment(
        tmp_path, which=which, module_finder=FakeFinder(), runner=denied,
        environ={}, os_release_path=_os_release(tmp_path))
    assert report["sudo"]["noninteractive"] is False
    assert report["sudo"]["probed"] is True

    timeout = FakeRunner(default=subprocess.TimeoutExpired(["sudo"], 2.0))
    report = T.detect_environment(
        tmp_path, which=which, module_finder=FakeFinder(), runner=timeout,
        environ={}, os_release_path=_os_release(tmp_path))
    assert report["sudo"]["noninteractive"] is False

    not_probed = T.detect_environment(
        tmp_path, which=which, module_finder=FakeFinder(), runner=FakeRunner(),
        probe_sudo=False, environ={}, os_release_path=_os_release(tmp_path))
    assert not_probed["sudo"]["probed"] is False
    assert not_probed["sudo"]["noninteractive"] is False


def test_config_approval_preferred_and_probe_flags(tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
allow_install = true
allow_network = false
allow_sudo = true
probe_versions = false
probe_sudo = false
preferred = { pdf = ["pypdf", "qpdf"] }
""")
    which = FakeWhich({"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"})
    runner = FakeRunner()
    environment = T.detect_environment(
        tmp_path, which=which, module_finder=FakeFinder(), runner=runner,
        environ={}, os_release_path=_os_release(tmp_path))
    approval = environment["approval"]
    assert approval["allow_install"] is True
    assert approval["allow_network"] is False
    assert approval["allow_sudo"] is True
    assert approval["sources"]["allow_install"] == "config"
    assert environment["probe"]["versions"] is False
    assert environment["probe"]["sudo"] is False
    assert environment["preferred"] == {
        "pdf": ["pypdf", "qpdf"]}
    assert runner.calls == []


def test_environment_overrides_only_truthy_values(tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
allow_install = false
allow_network = true
allow_sudo = false
""")
    environment = T.detect_environment(
        tmp_path, which=FakeWhich(), module_finder=FakeFinder(),
        environ={"OZAGIA_ALLOW_INSTALL": "true",
                 "OZAGIA_ALLOW_NETWORK": "0",
                 "OZAGIA_ALLOW_SUDO": "yes"},
        probe_sudo=False, os_release_path=_os_release(tmp_path))
    approval = environment["approval"]
    assert approval["allow_install"] is True
    assert approval["allow_network"] is True
    assert approval["allow_sudo"] is True
    assert approval["sources"] == {
        "allow_install": "environment", "allow_network": "config",
        "allow_sudo": "environment"}
    assert approval["environment_overrides"] == [
        "OZAGIA_ALLOW_INSTALL", "OZAGIA_ALLOW_SUDO"]


def test_wrong_config_types_and_unknown_keys_are_reported(
        tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
allow_install = "yes"
allow_network = 1
probe_sudo = "off"
unknown_key = true
preferred = { pdf = "qpdf", nope = ["qpdf"] }
""")
    environment = T.detect_environment(
        tmp_path, which=FakeWhich(), module_finder=FakeFinder(),
        environ={}, os_release_path=_os_release(tmp_path))
    approval = environment["approval"]
    assert approval["allow_install"] is False
    assert approval["allow_network"] is False
    assert environment["probe"]["sudo"] is True
    assert environment["preferred"] == {}
    messages = " ".join(
        item["message"] for item in environment["config"]["errors"])
    assert "must be true or false" in messages
    assert "unknown [tools] key" in messages
    assert "must be a list of tool ids" in messages
    assert "unknown capability" in messages
    assert any(item["key"] == "unknown_key"
               for item in environment["config"]["unknown"])


def test_tools_section_is_owned_by_toolchain_config(tmp_path: Path) -> None:
    from ozagia.config import load_project_config
    from ozagia.core import settings as S
    from ozagia.doctor import diagnose

    repo = _config(tmp_path, '[tools]\nallow_install = true\n')
    config = load_project_config(tmp_path)
    assert config.source == repo.resolve()
    assert config.unmapped == (
        ("tools", "read by ozagia.core.toolchain"),)
    # The settings registry does not own [tools]; its keys must not appear as
    # unknown settings or turn doctor's settings section invalid.
    assert not any(key.startswith("tools.") for key in S.values(tmp_path))
    report = diagnose(tmp_path)
    assert report["config"]["unmapped"] == [
        {"key": "tools", "reason": "read by ozagia.core.toolchain"}]
    assert report["settings"]["valid"] is True
    assert report["settings"]["errors"] == []


# --------------------------------------------------------------------------- #
# capability inference and probing
# --------------------------------------------------------------------------- #
def test_pdf_is_inferred_from_a_path(tmp_path: Path) -> None:
    plan = _pdf_plan(tmp_path, capabilities=(), paths=["docs/report.pdf"])
    assert plan["request"]["explicit"] == []
    assert plan["request"]["inferred"] == ["pdf"]
    assert plan["request"]["capabilities"] == ["pdf"]
    assert plan["request"]["unmatched_paths"] == []
    assert [item["id"] for item in plan["capabilities"]] == ["pdf"]


def test_explicit_capabilities_and_unknown_extensions(tmp_path: Path) -> None:
    plan = T.tool_plan(
        tmp_path, capabilities=["ocr"], paths=["notes.unknown"],
        which=FakeWhich(), module_finder=FakeFinder(),
        probe_versions=False, probe_sudo=False, environ={})
    assert plan["request"]["explicit"] == ["ocr"]
    assert plan["request"]["inferred"] == []
    assert plan["request"]["capabilities"] == ["ocr"]
    assert plan["request"]["unmatched_paths"] == [
        {"path": "notes.unknown", "suffix": ".unknown"}]


def test_multiple_capabilities_and_longest_extension(tmp_path: Path) -> None:
    plan = T.tool_plan(
        tmp_path, paths=["a.pdf", "b.PNG", "c.tar.gz", "d.svg"],
        which=FakeWhich(), module_finder=FakeFinder(),
        probe_versions=False, probe_sudo=False, environ={})
    assert plan["request"]["capabilities"] == [
        "archive", "diagram", "image", "pdf"]


def test_only_relevant_tools_are_probed(tmp_path: Path) -> None:
    which = FakeWhich({"qpdf": "/usr/bin/qpdf"})
    finder = FakeFinder()
    plan = _pdf_plan(tmp_path, which=which, module_finder=finder)
    assert "qpdf" in which.calls
    assert "tesseract" not in which.calls
    assert "pdftotext" in which.calls
    assert plan["capabilities"][0]["available"][0]["id"] == "qpdf"
    assert plan["capabilities"][0]["ready"] is True


def test_python_modules_are_found_with_find_spec(tmp_path: Path) -> None:
    finder = FakeFinder({"pypdf"})
    plan = _pdf_plan(tmp_path, module_finder=finder)
    available = {item["id"]: item for item in
                 plan["capabilities"][0]["available"]}
    assert set(available) == {"pypdf"}
    assert available["pypdf"]["path"] is None
    assert available["pypdf"]["module"] == "pypdf"
    assert available["pypdf"]["version"] is None
    assert "pypdf" in finder.calls


def test_version_probe_is_bounded_and_failure_keeps_tool_available(
        tmp_path: Path) -> None:
    which = FakeWhich({"qpdf": "/usr/bin/qpdf"})
    runner = FakeRunner({
        "/usr/bin/qpdf": {
            "returncode": 0, "stdout": "qpdf version 11.9.1\n", "stderr": ""}})
    plan = T.tool_plan(
        tmp_path, capabilities=["pdf"], which=which,
        module_finder=FakeFinder(), runner=runner, probe_versions=True,
        probe_sudo=False, environ={})
    available = {item["id"]: item for item in
                 plan["capabilities"][0]["available"]}
    assert available["qpdf"]["version"] == "qpdf version 11.9.1"
    assert runner.calls[0]["argv"] == ["/usr/bin/qpdf", "--version"]
    assert runner.calls[0]["timeout"] == T.VERSION_TIMEOUT_S

    failed = FakeRunner({
        "/usr/bin/qpdf": subprocess.TimeoutExpired(["/usr/bin/qpdf"], 2.0)})
    plan = T.tool_plan(
        tmp_path, capabilities=["pdf"], which=which,
        module_finder=FakeFinder(), runner=failed, probe_versions=True,
        probe_sudo=False, environ={})
    available = {item["id"]: item for item in
                 plan["capabilities"][0]["available"]}
    assert "qpdf" in available
    assert available["qpdf"]["version"] is None
    assert plan["capabilities"][0]["ready"] is True


def test_no_versions_skips_version_probes_and_sudo_probes(tmp_path: Path) -> None:
    which = FakeWhich({"qpdf": "/usr/bin/qpdf", "sudo": "/usr/bin/sudo"})
    runner = FakeRunner()
    plan = _pdf_plan(tmp_path, which=which, runner=runner)
    assert runner.calls == []
    assert plan["environment"]["probe"]["versions"] is False
    assert plan["environment"]["probe"]["sudo"] is False


def test_python_versions_come_from_distribution_metadata(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def metadata_version(distribution: str) -> str:
        seen.append(distribution)
        return "5.1.0"

    monkeypatch.setattr(T.importlib.metadata, "version", metadata_version)
    plan = T.tool_plan(
        tmp_path, capabilities=["pdf"], which=FakeWhich(),
        module_finder=FakeFinder({"pypdf"}), runner=FakeRunner(),
        probe_versions=True, probe_sudo=False, environ={})
    available = {item["id"]: item for item in
                 plan["capabilities"][0]["available"]}
    assert available["pypdf"]["version"] == "5.1.0"
    assert "pypdf" in seen
    # No interpreter, pip show or module import is used for the version.
    assert all("pip" not in item for item in seen)


def test_installed_python_extra_is_the_preferred_path(tmp_path: Path) -> None:
    plan = _pdf_plan(
        tmp_path, which=FakeWhich({"qpdf": "/usr/bin/qpdf"}),
        module_finder=FakeFinder({"pypdf"}))
    capability = plan["capabilities"][0]
    available = {item["id"]: item for item in capability["available"]}
    assert {"qpdf", "pypdf"} <= set(available)
    assert capability["recommended_tool"] == "pypdf"
    assert capability["recommended_path"] == "python-extra"
    assert capability["available"][0]["id"] == "pypdf"
    assert available["pypdf"]["recommended"] is True
    assert available["qpdf"]["recommended"] is False
    text = T.tool_plan_text(plan)
    assert "recommended path: Python extra pdf" in text
    assert "* pypdf" in text
    brief = "\n".join(T.brief_lines(plan))
    assert "Python extra pdf" in brief


def test_office_and_image_extras_are_planned(tmp_path: Path) -> None:
    office = T.tool_plan(
        tmp_path, paths=["memo.docx"], which=FakeWhich(),
        module_finder=FakeFinder({"docx"}), probe_versions=False,
        probe_sudo=False, environ={})
    assert office["request"]["inferred"] == ["office"]
    capability = office["capabilities"][0]
    assert capability["recommended_tool"] == "python-docx"
    assert capability["recommended_path"] == "python-extra"
    image = T.tool_plan(
        tmp_path, paths=["photo.png"], which=FakeWhich(),
        module_finder=FakeFinder({"PIL"}), probe_versions=False,
        probe_sudo=False, environ={})
    capability = image["capabilities"][0]
    assert capability["recommended_tool"] == "pillow"
    assert capability["recommended_path"] == "python-extra"


def test_plan_is_json_safe_and_deterministic(tmp_path: Path) -> None:
    first = _pdf_plan(tmp_path)
    second = _pdf_plan(tmp_path)
    assert json.dumps(first, sort_keys=True) == json.dumps(
        second, sort_keys=True)
    assert first["not_assessed"] == list(T.NOT_ASSESSED)
    assert any("GUI-only" in note for note in first["not_assessed"])
    assert any("network" in note for note in first["not_assessed"])
    assert any("installed" in note for note in first["not_assessed"])


# --------------------------------------------------------------------------- #
# install options and approval blockers
# --------------------------------------------------------------------------- #
def test_install_options_are_previews_and_blocked_without_approval(
        tmp_path: Path) -> None:
    which = FakeWhich({
        "apt-get": "/usr/bin/apt-get", "uv": "/usr/bin/uv",
        "pip": "/usr/bin/pip", "sudo": "/usr/bin/sudo"})
    plan = _pdf_plan(tmp_path, which=which)
    options = [option for capability in plan["capabilities"]
               for option in capability["install"]]
    assert options
    assert all(option["would_run"] is False for option in options)
    assert all("install" in option["blocked_by"] for option in options)
    assert not any(option["recommended"] for option in options)
    assert plan["summary"]["blocked"] == 1
    assert plan["capabilities"][0]["installable"] is False
    qpdf = _find_option(plan, "qpdf", "apt")
    assert qpdf["blocked_by"] == ["install", "network", "sudo"]
    assert qpdf["command"] == "sudo apt-get install -y qpdf"


def test_user_level_uv_is_recommended_over_sudo(tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
allow_install = true
allow_network = true
allow_sudo = false
""")
    which = FakeWhich({
        "apt-get": "/usr/bin/apt-get", "uv": "/usr/bin/uv",
        "pip": "/usr/bin/pip", "sudo": "/usr/bin/sudo"})
    runner = FakeRunner(default={"returncode": 1, "stdout": "", "stderr": ""})
    plan = _pdf_plan(tmp_path, which=which, runner=runner)
    uv = _find_option(plan, "pypdf", "uv")
    pip = _find_option(plan, "pypdf", "pip")
    apt = _find_option(plan, "qpdf", "apt")
    assert uv["blocked_by"] == []
    assert uv["recommended"] is True
    assert pip["blocked_by"] == []
    assert pip["recommended"] is False
    assert apt["blocked_by"] == ["sudo"]
    assert apt["recommended"] is False
    assert uv["command"] == "uv pip install 'ozagia[pdf]'"
    assert pip["command"] == "python -m pip install 'ozagia[pdf]'"
    assert uv["package"] == "ozagia[pdf]"
    assert uv["extra"] == "pdf"
    assert uv["install_kind"] == "python-extra"
    assert pip["install_kind"] == "python-extra"
    # A user-level option makes the capability installable without sudo.
    assert plan["capabilities"][0]["installable"] is True
    assert plan["capabilities"][0]["recommended_path"] == "python-extra"


def test_network_and_sudo_blockers_have_distinct_reasons(tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
allow_install = true
allow_network = false
allow_sudo = true
""")
    which = FakeWhich({
        "apt-get": "/usr/bin/apt-get", "uv": "/usr/bin/uv",
        "sudo": "/usr/bin/sudo"})
    runner = FakeRunner(default={"returncode": 1, "stdout": "", "stderr": ""})
    plan = _pdf_plan(tmp_path, which=which, runner=runner)
    uv = _find_option(plan, "pypdf", "uv")
    apt = _find_option(plan, "qpdf", "apt")
    assert uv["blocked_by"] == ["network"]
    assert "network access not approved" in uv["blocked_reasons"]
    assert apt["blocked_by"] == ["network", "sudo"]
    assert "sudo not usable non-interactively" in apt["blocked_reasons"]
    assert not any(option["recommended"]
                   for capability in plan["capabilities"]
                   for option in capability["install"])


def test_missing_manager_is_a_blocker_without_running_anything(
        tmp_path: Path) -> None:
    plan = _pdf_plan(tmp_path)
    apt = _find_option(plan, "qpdf", "apt")
    assert apt["available"] is False
    assert apt["path"] is None
    assert apt["blocked_by"] == ["install", "network", "sudo", "manager"]
    assert "package manager apt not installed" in apt["blocked_reasons"]
    assert apt["command"] == "sudo apt-get install -y qpdf"


def test_install_options_order_user_level_first(tmp_path: Path) -> None:
    which = FakeWhich({"uv": "/usr/bin/uv", "pip": "/usr/bin/pip"})
    plan = _pdf_plan(tmp_path, which=which)
    managers = [option["manager"] for option in plan["capabilities"][0]["install"]
                if option["tool"] == "pypdf"]
    assert managers == ["uv", "pip"]


def test_approved_plan_mentions_exact_command_and_blocked_reason(
        tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
allow_install = true
allow_network = true
allow_sudo = false
""")
    which = FakeWhich({"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"})
    runner = FakeRunner(default={"returncode": 1, "stdout": "", "stderr": ""})
    plan = T.tool_plan(
        tmp_path, capabilities=["ocr"], which=which,
        module_finder=FakeFinder(), runner=runner, probe_versions=False,
        probe_sudo=True, environ={}, os_release_path=_os_release(tmp_path))
    text = T.tool_plan_text(plan)
    assert "sudo apt-get install -y ocrmypdf" in text
    assert "sudo not usable non-interactively" in text
    assert "installation is not approved" not in text
    # The plan did not execute an install; the only process was the sudo check.
    assert all("install" not in call["argv"] for call in runner.calls)
    assert all(option["would_run"] is False
               for capability in plan["capabilities"]
               for option in capability["install"])


def test_text_and_brief_omit_install_commands_without_approval(
        tmp_path: Path) -> None:
    plan = _pdf_plan(tmp_path)
    text = T.tool_plan_text(plan)
    brief = "\n".join(T.brief_lines(plan))
    assert "pdf: missing" in text
    assert "installation is not approved" in text
    assert "apt-get install" not in text
    assert "uv pip install" not in text
    assert "pdf: missing" in brief
    assert "installation not approved" in brief
    for line in T.brief_lines(plan):
        assert "\n" not in line


def test_brief_lines_name_ready_tools_and_provides(tmp_path: Path) -> None:
    finder = FakeFinder({"pypdf"})
    plan = _pdf_plan(tmp_path, module_finder=finder)
    brief = "\n".join(T.brief_lines(plan))
    assert "pdf: ready" in brief
    assert "pypdf" in brief
    assert "merge/split" in brief
    assert "not checked" in brief


def test_plan_without_request_does_not_probe(tmp_path: Path) -> None:
    which = FakeWhich({"qpdf": "/usr/bin/qpdf"})
    plan = T.tool_plan(
        tmp_path, which=which, module_finder=FakeFinder(),
        probe_versions=False, probe_sudo=False, environ={})
    assert plan["capabilities"] == []
    assert plan["summary"]["capabilities"] == 0
    assert "qpdf" not in which.calls
    assert "no capabilities requested" in T.tool_plan_text(plan)


def test_unknown_capability_and_bounded_inputs(tmp_path: Path) -> None:
    with pytest.raises(T.ToolchainError, match="unknown capability"):
        T.tool_plan(tmp_path, capabilities=["nope"], which=FakeWhich(),
                    module_finder=FakeFinder())
    with pytest.raises(T.ToolchainError, match="at most"):
        T.tool_plan(tmp_path, capabilities=["pdf"] * 33,
                    which=FakeWhich(), module_finder=FakeFinder())
    with pytest.raises(T.ToolchainError, match="at most"):
        T.tool_plan(tmp_path, paths=[f"{i}.pdf" for i in range(65)],
                    which=FakeWhich(), module_finder=FakeFinder())


def test_config_can_prefer_tool_order_in_plan(tmp_path: Path) -> None:
    _config(tmp_path, """
[tools]
preferred = { pdf = ["pypdf", "qpdf"] }
""")
    plan = T.tool_plan(
        tmp_path, capabilities=["pdf"], which=FakeWhich(),
        module_finder=FakeFinder({"pypdf"}), probe_versions=False,
        probe_sudo=False, environ={}, os_release_path=_os_release(tmp_path))
    capability = plan["capabilities"][0]
    assert capability["preferred"] == ["pypdf", "qpdf"]
    assert capability["available"][0]["preferred"] is True


def test_brief_config_opt_out_and_default(tmp_path: Path) -> None:
    assert T.brief_enabled(tmp_path) is True
    _config(tmp_path, "[tools]\nbrief = false\n")
    assert T.brief_enabled(tmp_path) is False
    environment = T.detect_environment(
        tmp_path, which=FakeWhich(), module_finder=FakeFinder(),
        probe_versions=False, probe_sudo=False, environ={},
        os_release_path=_os_release(tmp_path))
    assert environment["config"]["values"]["brief"] is False
    assert environment["config"]["sources"]["brief"] == "repo"
    # A wrong type is reported and leaves the block on, like other settings.
    _config(tmp_path, '[tools]\nbrief = "no"\n')
    assert T.brief_enabled(tmp_path) is True
    environment = T.detect_environment(
        tmp_path, which=FakeWhich(), module_finder=FakeFinder(),
        probe_versions=False, probe_sudo=False, environ={},
        os_release_path=_os_release(tmp_path))
    assert any(item["key"] == "tools.brief"
               and "must be true or false" in item["message"]
               for item in environment["config"]["errors"])
