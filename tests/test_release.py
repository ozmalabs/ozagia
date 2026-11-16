"""The release tooling: the install script (offline, with a fake bundle and
a fake OpenCode), `ozagia` defaults, `ozagia self`, and the release scripts.
The real bundle is exercised by scripts/release/smoke.sh in the release
workflow."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from ozagia import cli, selfinstall

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install"
needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "curl", "tar"))
    or sys.platform not in ("linux", "darwin"), reason="needs bash, curl and tar")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "scripts" / "release" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target() -> str:
    return _load("build_bundle").host_target()


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tar(src: Path, out: Path, arcname: str) -> Path:
    with tarfile.open(out, "w:gz") as tf:
        tf.add(src, arcname=arcname)
    return out


@pytest.fixture
def release(tmp_path):
    """A fake release in GitHub's layout and a fake OpenCode download."""
    target = _target()
    version = "9.9.9"
    # OpenCode: an archive with an `opencode` that prints its version.
    oc_src = tmp_path / "oc_src"
    oc_src.mkdir()
    (oc_src / "opencode").write_text("#!/bin/sh\necho 2.3.0\n")
    (oc_src / "opencode").chmod(0o755)
    oc_dir = tmp_path / "oc_dl"
    oc_dir.mkdir()
    oc_name = f"opencode-{target}.tar.gz"
    with tarfile.open(oc_dir / oc_name, "w:gz") as tf:
        tf.add(oc_src / "opencode", arcname="opencode")
    # The bundle: python/bin/python3 runs this interpreter.
    stage = tmp_path / "stage" / f"ozagia-{target}"
    (stage / "python" / "bin").mkdir(parents=True)
    py = stage / "python" / "bin" / "python3"
    # This interpreter, importing the ozagia under test (`-I` would hide
    # PYTHONPATH, so it is dropped).
    py.write_text(f'#!/bin/sh\n[ "$1" = -I ] && shift\n'
                  f'PYTHONPATH={REPO / "src"} exec {sys.executable} "$@"\n')
    py.chmod(0o755)
    asset = {"name": oc_name, "sha256": _sha(oc_dir / oc_name)}
    manifest = {"name": "ozagia", "version": version, "target": target,
                "opencode": {"version": "2.3.0", "url": "unused/{name}",
                             "assets": {"default": asset, "baseline": asset}}}
    (stage / "manifest.json").write_text(json.dumps(manifest))
    rel = tmp_path / "releases"
    dl = rel / "download" / f"v{version}"
    dl.mkdir(parents=True)
    bundle = _tar(stage, dl / f"ozagia-{target}.tar.gz", f"ozagia-{target}")
    (dl / "SHA256SUMS").write_text(f"{_sha(bundle)}  {bundle.name}\n")
    (rel / "latest").mkdir()
    (rel / "latest" / "download").symlink_to(dl)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# mine\n")
    return {"rel": rel, "dl": dl, "home": home, "version": version, "target": target,
            "oc_url": f"file://{oc_dir}/{{name}}", "bundle": bundle}


def _install(r, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {"HOME": str(r["home"]), "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin",
           "OZAGIA_OPENCODE_URL": r["oc_url"]}
    p = subprocess.run(["bash", str(INSTALL), "--base-url", f"file://{r['rel']}", *args],
                       env=env, capture_output=True, text=True, timeout=120)
    if check:
        assert p.returncode == 0, p.stdout + p.stderr
    return p


@needs_tools
def test_install_lays_out_home_launcher_opencode_and_path(release):
    r = release
    _install(r)
    oz = r["home"] / ".ozagia"
    assert (oz / "current").resolve() == (oz / "versions" / r["version"]).resolve()
    oc = oz / "opencode" / "2.3.0" / "opencode"
    assert os.access(oc, os.X_OK)
    launcher = (oz / "bin" / "ozagia").read_text()
    assert f"OZAGIA_OPENCODE='{oc}'" in launcher
    assert '-I -m ozagia "$@"' in launcher
    rc = (r["home"] / ".bashrc").read_text()
    assert rc.startswith("# mine\n")
    assert f"# ozagia\nexport PATH={oz / 'bin'}:$PATH\n" in rc


@needs_tools
def test_reinstall_keeps_opencode_and_adds_path_once(release):
    r = release
    _install(r)
    p = _install(r, "--version", r["version"])
    assert "already installed" in p.stdout
    assert (r["home"] / ".bashrc").read_text().count("# ozagia") == 1


@needs_tools
def test_checksum_mismatch_installs_nothing(release):
    r = release
    (r["dl"] / "SHA256SUMS").write_text(f"{'0' * 64}  {r['bundle'].name}\n")
    p = _install(r, check=False)
    assert p.returncode != 0 and "checksum mismatch" in p.stderr
    assert not (r["home"] / ".ozagia" / "current").exists()


@needs_tools
def test_opencode_checksum_mismatch_fails(release):
    r = release
    oc = Path(r["oc_url"][len("file://"):].replace("/{name}", "")) / f"opencode-{r['target']}.tar.gz"
    oc.write_bytes(oc.read_bytes() + b"x")
    p = _install(r, check=False)
    assert p.returncode != 0 and "checksum mismatch for opencode" in p.stderr


@needs_tools
def test_unknown_version_and_no_opencode(release):
    r = release
    p = _install(r, "--version", "1.2.3", check=False)
    assert p.returncode != 0 and "no ozagia-" in p.stderr
    _install(r, "--no-opencode", "--no-modify-path")
    launcher = (r["home"] / ".ozagia" / "bin" / "ozagia").read_text()
    assert "OZAGIA_OPENCODE" not in launcher
    assert (r["home"] / ".bashrc").read_text() == "# mine\n"


@needs_tools
def test_local_bundle(release):
    r = release
    _install(r, "--bundle", str(r["bundle"]))
    assert (r["home"] / ".ozagia" / "versions" / r["version"]).is_dir()


# --------------------------------------------------------------------------- #
# ozagia defaults and ozagia self
# --------------------------------------------------------------------------- #
def test_bare_ozagia_and_a_directory_open_the_tui(tmp_path):
    assert cli._default_agent([]) == ["agent"]
    assert cli._default_agent([str(tmp_path), "--model", "m"]) == [
        "agent", "--workdir", str(tmp_path), "--model", "m"]
    assert cli._default_agent(["index"]) == ["index"]
    assert cli._default_agent(["self", "info"]) == ["self", "info"]
    assert cli._default_agent(["--version"]) == ["--version"]
    assert cli._default_agent(["no-such-dir"]) == ["no-such-dir"]
    assert cli._default_agent(["--research"]) == ["agent", "--research"]
    assert cli._default_agent(["-m", "x/y"]) == ["agent", "-m", "x/y"]


def test_opencode_executable_follows_the_installer(monkeypatch):
    monkeypatch.setenv("OZAGIA_OPENCODE", "/opt/oc/opencode")
    assert cli._opencode_exe(None) == "/opt/oc/opencode"
    assert cli._opencode_exe("system") == "opencode"
    assert cli._opencode_exe("/x/oc") == "/x/oc"
    monkeypatch.delenv("OZAGIA_OPENCODE")
    assert cli._opencode_exe(None) == "opencode"


def test_self_info_without_an_install(monkeypatch, capsys):
    monkeypatch.setattr(selfinstall, "manifest", lambda: None)
    info = selfinstall.info()
    assert info["installed"] is False and info["home"] is None
    assert cli.main(["self", "upgrade"]) == 2
    assert cli.main(["self", "uninstall", "--yes"]) == 2


def test_self_check_reports_a_missing_opencode(monkeypatch):
    monkeypatch.setattr(selfinstall, "manifest", lambda: None)
    monkeypatch.setenv("OZAGIA_OPENCODE", "/nonexistent/opencode")
    res = selfinstall.check()
    by = {c["name"]: c for c in res["checks"]}
    assert by["runtime"]["ok"] and by["plugin"]["ok"]
    assert by["opencode"]["ok"] is False and res["ok"] is False


def test_uninstall_strips_only_the_installer_lines(tmp_path):
    bin_dir = str(tmp_path / ".ozagia" / "bin")
    rc = tmp_path / ".bashrc"
    # As written from a HOME with `//` (macOS TMPDIR ends in a slash).
    written = str(tmp_path) + "//.ozagia/bin"
    rc.write_text(f"alias x=y\n\n# ozagia\nexport PATH={written}:$PATH\n# other\n"
                  f"# ozagia\nexport PATH=/elsewhere/bin:$PATH\n")
    assert selfinstall._strip_path_lines(rc, bin_dir)
    assert rc.read_text() == ("alias x=y\n# other\n# ozagia\n"
                              "export PATH=/elsewhere/bin:$PATH\n")
    assert not selfinstall._strip_path_lines(rc, bin_dir)


def test_upgrade_runs_the_installer_url(monkeypatch):
    monkeypatch.setenv("OZAGIA_INSTALL_URL", "https://example.test/install")
    argv = selfinstall._installer("0.2.0")
    assert argv[:2] == ["bash", "-c"]
    assert "curl -fsSL https://example.test/install | bash -s -- --version 0.2.0" in argv[2]


# --------------------------------------------------------------------------- #
# release scripts
# --------------------------------------------------------------------------- #
def test_pins_cover_every_target():
    pins = json.loads((REPO / "release" / "pins.json").read_text())
    for t in pins["targets"]:
        assert len(pins["runtime"]["assets"][t]["sha256"]) == 64
        assert len(pins["opencode"]["assets"][t]["default"]["sha256"]) == 64


def test_version_order():
    key = _load("prepare").key
    assert key("0.1.0.dev0") < key("0.1.0a1") < key("0.1.0b1") < key("0.1.0rc1")
    assert key("0.1.0rc1") < key("0.1.0") < key("0.1.1") < key("0.2.0")


def test_release_notes_use_the_changelog_section(tmp_path, monkeypatch):
    notes = _load("notes")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.2.0 (2026-12-01)\n\nNew things.\n\n## 0.1.0\n\nOld.\n")
    monkeypatch.setattr(notes, "REPO", tmp_path)
    assert notes.section("0.2.0") == "New things."
    assert notes.section("0.1.0") == "Old."


def test_wheel_version_from_name():
    bb = _load("build_bundle")
    assert bb.wheel_version(Path("ozagia-0.1.0rc1-py3-none-any.whl")) == "0.1.0rc1"
    with pytest.raises(SystemExit):
        bb.wheel_version(Path("other-1.0-py3-none-any.whl"))


def test_agent_research_flag_adds_the_mcp_server(tmp_path):
    from ozagia.harness.opencode import mcp_server_entry, write_opencode_config
    args = cli.build_parser().parse_args(["agent", "--research"])
    assert args.mcp == "combined"
    entry = mcp_server_entry(tmp_path, "research")
    assert entry["command"][-4:] == ["--root", str(tmp_path), "--mode", "research"]
    doc = json.loads(write_opencode_config(tmp_path / "c.json", mcp=entry).read_text())
    assert doc["mcp"]["ozagia"]["environment"]["OZAGIA_ROOT"] == str(tmp_path)
    assert "git push*" in doc["permission"]["bash"]
