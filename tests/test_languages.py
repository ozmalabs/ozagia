"""Per-language support packages: registry, detection, reporting and installs."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from ozagia.index.tag_languages import LANGUAGES as TAG_SUFFIXES
from ozagia.languages import (
    BY_SUFFIX,
    Component,
    LanguageSupport,
    SUPPORTS,
    detect_languages,
    ensure,
    support_for,
)
from ozagia.languages_cli import main as languages_main


class _Result:
    def __init__(self, code: int, stderr: str = "") -> None:
        self.returncode = code
        self.stdout = ""
        self.stderr = stderr


def _support() -> LanguageSupport:
    return LanguageSupport(
        id="testish",
        label="Testish",
        suffixes=(".tish",),
        components=(
            Component(
                id="tish-grammar",
                label="Testish grammar",
                kind="python",
                probe="tish_grammar",
                manager="pip",
                package="tree-sitter-tish>=1",
                extra="lang-testish",
            ),
            Component(
                id="tish-server",
                label="Testish server",
                kind="binary",
                probe="tish-lsp",
                manager="rustup",
                package="tish-lsp",
            ),
            Component(
                id="tish-linter",
                label="Testish linter",
                kind="python",
                probe="tish_lint",
                manager="pip",
                package="tish-lint>=2",
                required=False,
            ),
        ),
    )


def test_every_tag_suffix_has_a_support_package() -> None:
    missing = sorted(set(TAG_SUFFIXES) - set(BY_SUFFIX))
    assert missing == []
    assert support_for("src/lib.rs") is SUPPORTS["rust"]
    assert support_for("app.TSX") is SUPPORTS["typescript"]
    assert support_for("notes.txt") is None


def test_every_support_package_has_a_lang_extra() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))[
        "project"]["optional-dependencies"]
    for language, support in SUPPORTS.items():
        assert f"lang-{language}" in extras, language
        assert extras[f"lang-{language}"], language
        for component in support.components:
            if component.extra:
                assert component.extra == support.extra


def test_detect_languages_uses_source_suffixes_and_skips_build_dirs(
        tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn main() {}\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.ts").write_text("const x = 1;\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "generated.rs").write_text("fn generated() {}\n")

    found = detect_languages(tmp_path)

    assert [item.id for item in found["languages"]] == [
        "python", "rust", "typescript"]
    assert found["truncated"] is False
    assert found["files_scanned"] >= 3


def test_python_install_prefers_uv(monkeypatch) -> None:
    monkeypatch.setattr(
        "ozagia.languages.model.shutil.which",
        lambda name: "/usr/bin/uv" if name == "uv" else None)
    command = _support().components[0].install_command()
    assert command is not None
    assert command[:4] == ["/usr/bin/uv", "pip", "install", "--python"]
    assert command[4] == sys.executable
    assert command[-1] == "tree-sitter-tish>=1"


def test_python_install_falls_back_to_pip(monkeypatch) -> None:
    monkeypatch.setattr("ozagia.languages.model.shutil.which", lambda name: None)
    command = _support().components[0].install_command()
    assert command is not None
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--disable-pip-version-check" in command


def test_ensure_reports_and_installs_automatic_components() -> None:
    support = _support()
    state = {item.id: False for item in support.components}
    calls: list[list[str]] = []
    lines: list[str] = []

    def probe(component: Component) -> bool:
        return state[component.id]

    def runner(argv: list[str]) -> _Result:
        calls.append(argv)
        for key in state:
            state[key] = True
        return _Result(0)

    result = ensure(support, install=True, probe=probe, runner=runner,
                    report=lines.append)

    text = "\n".join(lines)
    assert "installing required components for Testish" in text
    assert "Testish support ready" in text
    assert result["ready"] is True
    assert set(result["installed"]) == {
        "tish-grammar", "tish-server", "tish-linter"}
    assert any("pip" in call[1] or "pip" in call[2] for call in calls)
    assert any(call[:3] == ["rustup", "component", "add"] for call in calls)
    assert all(call[:2] != ["npm", "install"] for call in calls)


def test_ensure_without_install_only_reports() -> None:
    support = _support()
    calls: list[list[str]] = []
    lines: list[str] = []

    def runner(argv: list[str]) -> _Result:  # pragma: no cover - must not run
        calls.append(argv)
        return _Result(0)

    result = ensure(support, install=False, probe=lambda item: False,
                    runner=runner, report=lines.append)

    text = "\n".join(lines)
    assert "missing required components" in text
    assert "ozagia languages ensure" in text
    assert calls == []
    assert result["ready"] is False


def test_ensure_reports_system_components_as_manual() -> None:
    support = LanguageSupport(
        id="clangy",
        label="Clangy",
        suffixes=(".clangy",),
        components=(
            Component(
                id="clangd",
                label="clangd",
                kind="binary",
                probe="definitely-not-installed-clangd",
                manager="system",
                package="clangd",
                required=False,
                requires_sudo=True,
            ),
        ),
    )
    lines: list[str] = []

    result = ensure(support, install=True, probe=lambda item: False,
                    report=lines.append)

    text = "\n".join(lines)
    assert "manual components" in text
    assert "needs sudo" in text
    assert result["commands"] == []
    assert result["failed"] == []


def test_network_block_is_reported_without_running() -> None:
    support = _support()
    calls: list[list[str]] = []
    lines: list[str] = []

    result = ensure(
        support, install=True, network=False, probe=lambda item: False,
        runner=lambda argv: calls.append(argv) or _Result(0),
        report=lines.append)

    text = "\n".join(lines)
    assert "network access is disabled" in text
    assert calls == []
    assert result["blocked"]


def test_languages_cli_list_and_detect(tmp_path: Path, capsys) -> None:
    assert languages_main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["language"] for row in rows} >= {"rust", "python", "lua"}

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn main() {}\n")
    assert languages_main(
        ["detect", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["language"] for item in payload["languages"]] == ["rust"]
    assert payload["languages"][0]["components"]
