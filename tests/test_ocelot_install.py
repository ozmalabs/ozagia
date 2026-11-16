"""Explicit, pinned Ocelot resolution and installation.

No test downloads anything: ``_download`` is replaced, and DNS resolution is
forbidden wherever the code path should stay offline.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from ozagia.integrations import ocelot
from ozagia.integrations.ocelot.__main__ import main as ocelot_main

URL = "https://example.com/ocelot-1.0.0.tar.gz"
ZIP_URL = "https://example.com/ocelot-1.0.0.zip"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv(ocelot.ENV_OCELOT, raising=False)
    return home


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace" / "project"
    root.mkdir(parents=True)
    return root


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(ocelot.socket, "getaddrinfo", fail)
    monkeypatch.setattr(ocelot, "_download", fail)


def _write_pyproject(path: Path, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n', encoding="utf-8")


def _write_install(home: Path, version: str,
                   content: bytes = b"ocelot\n") -> Path:
    root = home / ".local" / "share" / "ozagia" / "ocelot" / version
    root.mkdir(parents=True)
    (root / "README.md").write_bytes(content)
    files = [{
        "path": "README.md",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }]
    manifest = {
        "format": ocelot.MANIFEST_FORMAT,
        "version": version,
        "source": URL,
        "sha256": hashlib.sha256(b"archive-" + version.encode()).hexdigest(),
        "archive": {"name": "ocelot.tar.gz", "format": "tar.gz", "bytes": 1},
        "installed_at": "2026-11-16T00:00:00Z",
        "license": {"spdx": "MIT", "bundled": False},
        "tree": {
            "sha256": ocelot._tree_digest(files),
            "file_count": 1,
            "bytes": len(content),
            "files": files,
        },
    }
    (root / ocelot.MANIFEST_FILENAME).write_text(
        json.dumps(manifest), encoding="utf-8")
    return root


def _tar_bytes(files: dict[str, bytes], *, top: str = "ocelot-1.0.0",
               include_dir: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        if include_dir:
            info = tarfile.TarInfo(top)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, content in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _zip_bytes(files: dict[str, bytes], *, top: str = "ocelot-1.0.0",
               include_dir: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if include_dir:
            info = zipfile.ZipInfo(top + "/")
            info.external_attr = 0o40755 << 16
            archive.writestr(info, b"")
        for name, content in files.items():
            info = zipfile.ZipInfo(f"{top}/{name}" if top else name)
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _mock_download(monkeypatch: pytest.MonkeyPatch, payload: bytes,
                   calls: list[str] | None = None) -> None:
    def fake(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        return payload

    monkeypatch.setattr(ocelot, "_download", fake)


# --------------------------------------------------------------------------- #
# resolve and status
# --------------------------------------------------------------------------- #
def test_resolve_precedence_env_sibling_installed(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root = _root(tmp_path)
    explicit = tmp_path / "explicit-ocelot"
    _write_pyproject(explicit, "1.1.1")
    sibling = root.parent / "ocelot"
    _write_pyproject(sibling, "2.2.2")
    installed = _write_install(isolated_home, "3.3.3")

    monkeypatch.setenv(ocelot.ENV_OCELOT, str(explicit))
    result = ocelot.resolve(root)
    assert result["state"] == "ready"
    assert result["source"] == "environment"
    assert result["path"] == str(explicit.resolve())
    assert result["version"] == "1.1.1"

    monkeypatch.delenv(ocelot.ENV_OCELOT)
    result = ocelot.resolve(root)
    assert result["source"] == "sibling"
    assert result["path"] == str(sibling.resolve())
    assert result["version"] == "2.2.2"

    shutil.rmtree(sibling)
    result = ocelot.resolve(root)
    assert result["source"] == "installed"
    assert result["path"] == str(installed)
    assert result["version"] == "3.3.3"
    assert result["manifest_path"] == str(installed / ocelot.MANIFEST_FILENAME)
    assert result["manifest"]["version"] == "3.3.3"


def test_resolve_not_installed_is_clear(tmp_path: Path,
                                        isolated_home: Path) -> None:
    result = ocelot.resolve(_root(tmp_path))
    assert result["state"] == "not_installed"
    assert result["path"] is None
    assert result["version"] is None
    assert result["source"] is None
    assert result["manifest"] is None
    assert "not installed" in result["note"].lower()
    assert "automatically" in result["note"].lower()


def test_resolve_picks_newest_installed_version(tmp_path: Path,
                                                isolated_home: Path) -> None:
    _write_install(isolated_home, "1.9.0")
    newest = _write_install(isolated_home, "1.10.0")
    result = ocelot.resolve(_root(tmp_path))
    assert result["source"] == "installed"
    assert result["version"] == "1.10.0"
    assert result["path"] == str(newest)


def test_resolve_invalid_env_is_an_error(tmp_path: Path,
                                         isolated_home: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ocelot.ENV_OCELOT, str(tmp_path / "missing"))
    with pytest.raises(ocelot.OcelotError, match="OZAGIA_OCELOT"):
        ocelot.resolve(_root(tmp_path))


def test_resolve_ignores_invalid_installed_manifest(tmp_path: Path,
                                                    isolated_home: Path) -> None:
    home = isolated_home / ".local" / "share" / "ozagia" / "ocelot"
    broken = home / "1.0.0"
    broken.mkdir(parents=True)
    (broken / ocelot.MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")

    result = ocelot.resolve(_root(tmp_path))
    assert result["state"] == "not_installed"
    assert len(result["invalid_installs"]) == 1
    assert "invalid install manifest" in result["note"].lower()


def test_status_reports_manifest_and_licence(tmp_path: Path,
                                             isolated_home: Path) -> None:
    _write_install(isolated_home, "1.0.0")
    result = ocelot.status(_root(tmp_path))
    assert result["state"] == "ready"
    assert result["installed"] is True
    assert result["source"] == "installed"
    assert result["manifest"]["version"] == "1.0.0"
    assert result["manifest"]["file_count"] == 1
    assert result["license"]["spdx"] == "MIT"
    assert result["license"]["bundled"] is False
    assert "MIT" in result["license"]["note"]
    assert result["reconciliation_optional"] is True


# --------------------------------------------------------------------------- #
# install validation and dry run
# --------------------------------------------------------------------------- #
def test_install_dry_run_writes_nothing_and_stays_offline(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    dest = tmp_path / "installs"
    result = ocelot.install("1.0.0", URL, "a" * 64, dest=dest)
    assert result["action"] == "dry_run"
    assert result["applied"] is False
    assert result["would_install"] is True
    assert result["path"] == str(dest / "1.0.0")
    assert result["sha256"] == "a" * 64
    assert not dest.exists()


def test_install_requires_a_checksum_and_explicit_download(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    dest = tmp_path / "installs"
    with pytest.raises(ocelot.OcelotValidationError, match="sha256"):
        ocelot.install("1.0.0", URL, "not-a-checksum", dest=dest)
    with pytest.raises(ocelot.OcelotError, match="allow_download"):
        ocelot.install("1.0.0", URL, "a" * 64, dest=dest, dry_run=False)
    assert not dest.exists()


@pytest.mark.parametrize(("url", "match"), [
    ("http://example.com/ocelot-1.0.0.tar.gz", "HTTPS"),
    ("https://user:secret@example.com/ocelot-1.0.0.tar.gz", "credentials"),
    ("https://127.0.0.1/ocelot-1.0.0.tar.gz", "private"),
    ("https://10.1.2.3/ocelot-1.0.0.tar.gz", "private"),
    ("https://[::1]/ocelot-1.0.0.tar.gz", "private"),
    ("https://localhost/ocelot-1.0.0.tar.gz", "local"),
    ("https://example.com/ocelot-1.0.0.tar.bz2", "tar.gz"),
    ("https://example.com/ocelot-1.0.0.tar.gz#fragment", "fragment"),
])
def test_install_rejects_unsafe_or_unpinned_urls(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch, url: str, match: str) -> None:
    _forbid_network(monkeypatch)
    dest = tmp_path / "installs"
    with pytest.raises(ocelot.OcelotError, match=match):
        ocelot.install("1.0.0", url, "a" * 64, dest=dest)
    assert not dest.exists()


def test_install_rejects_empty_or_escaping_version(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_network(monkeypatch)
    for version in ("", ".", "..", "../1.0.0", "1.0.0/../../x"):
        with pytest.raises(ocelot.OcelotValidationError):
            ocelot.install(version, URL, "a" * 64, dest=tmp_path / "installs")


# --------------------------------------------------------------------------- #
# tar install, manifest and verify
# --------------------------------------------------------------------------- #
def test_install_tar_atomically_with_manifest_and_verify(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _tar_bytes({
        "README.md": b"# Ocelot\n",
        "pkg/__init__.py": b"VALUE = 1\n",
    }, include_dir=True)
    sha = hashlib.sha256(payload).hexdigest()
    calls: list[str] = []
    _mock_download(monkeypatch, payload, calls)
    dest = tmp_path / "installs"

    result = ocelot.install(
        "1.2.3", URL, sha, dest=dest, dry_run=False, allow_download=True)
    target = dest / "1.2.3"
    assert result["action"] == "installed"
    assert result["applied"] is True
    assert result["path"] == str(target)
    assert calls == [URL]
    assert (target / "README.md").read_text(encoding="utf-8") == "# Ocelot\n"
    assert (target / "pkg" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (target / "ocelot-1.2.3").exists()
    assert [p.name for p in dest.iterdir()] == ["1.2.3"]

    manifest = json.loads(
        (target / ocelot.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["format"] == ocelot.MANIFEST_FORMAT
    assert manifest["version"] == "1.2.3"
    assert manifest["source"] == URL
    assert manifest["sha256"] == sha
    assert manifest["installed_at"].endswith("Z")
    assert manifest["tree"]["file_count"] == 2
    assert manifest["license"]["spdx"] == "MIT"

    report = ocelot.verify("1.2.3", dest=dest)
    assert report["ok"] is True
    assert report["state"] == "verified"
    assert report["files_checked"] == 2
    assert report["extra_file_count"] == 0


def test_install_wrong_checksum_leaves_nothing(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _tar_bytes({"README.md": b"# Ocelot\n"})
    _mock_download(monkeypatch, payload)
    dest = tmp_path / "installs"
    dest.mkdir()
    with pytest.raises(ocelot.OcelotError, match="checksum mismatch"):
        ocelot.install("1.0.0", URL, "0" * 64, dest=dest,
                       dry_run=False, allow_download=True)
    assert list(dest.iterdir()) == []


def test_install_zip_atomically(tmp_path: Path, isolated_home: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _zip_bytes({
        "README.md": b"# Ocelot\n",
        "pkg/mod.py": b"x = 1\n",
    }, include_dir=True)
    sha = hashlib.sha256(payload).hexdigest()
    _forbid_network(monkeypatch)
    _mock_download(monkeypatch, payload)
    dest = tmp_path / "installs"
    result = ocelot.install(
        "2.0.0", ZIP_URL, sha, dest=dest, dry_run=False, allow_download=True)
    target = dest / "2.0.0"
    assert result["action"] == "installed"
    assert (target / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert ocelot.verify("2.0.0", dest=dest)["ok"] is True


def test_install_zip_traversal_is_rejected_atomically(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../evil.txt", "owned")
        archive.writestr("ok.txt", "ok")
    payload = buffer.getvalue()
    sha = hashlib.sha256(payload).hexdigest()
    _forbid_network(monkeypatch)
    _mock_download(monkeypatch, payload)
    dest = tmp_path / "installs"
    dest.mkdir()
    with pytest.raises(ocelot.OcelotError, match="unsafe"):
        ocelot.install("1.0.0", ZIP_URL, sha, dest=dest,
                       dry_run=False, allow_download=True)
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "evil.txt").exists()


def test_install_tar_symlink_is_rejected(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("ocelot-1.0.0/passwd")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
        content = b"# Ocelot\n"
        info = tarfile.TarInfo("ocelot-1.0.0/README.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    payload = buffer.getvalue()
    sha = hashlib.sha256(payload).hexdigest()
    _forbid_network(monkeypatch)
    _mock_download(monkeypatch, payload)
    dest = tmp_path / "installs"
    dest.mkdir()
    with pytest.raises(ocelot.OcelotError, match="not a regular file"):
        ocelot.install("1.0.0", URL, sha, dest=dest,
                       dry_run=False, allow_download=True)
    assert list(dest.iterdir()) == []


def test_install_never_overwrites_an_existing_version(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    first = _tar_bytes({"README.md": b"first\n"})
    first_sha = hashlib.sha256(first).hexdigest()
    _forbid_network(monkeypatch)
    _mock_download(monkeypatch, first)
    dest = tmp_path / "installs"
    ocelot.install("1.0.0", URL, first_sha, dest=dest,
                   dry_run=False, allow_download=True)

    second = _tar_bytes({"README.md": b"second\n"})
    second_sha = hashlib.sha256(second).hexdigest()
    with pytest.raises(ocelot.OcelotError, match="different archive"):
        ocelot.install("1.0.0", URL, second_sha, dest=dest,
                       dry_run=False, allow_download=True)
    assert (dest / "1.0.0" / "README.md").read_text(encoding="utf-8") == "first\n"

    # The same checksum is idempotent and does not download again.
    _forbid_network(monkeypatch)
    result = ocelot.install("1.0.0", URL, first_sha, dest=dest,
                            dry_run=False, allow_download=True)
    assert result["action"] == "already_installed"
    assert result["applied"] is False

    unmanaged = dest / "2.0.0"
    unmanaged.mkdir()
    (unmanaged / "notes.txt").write_text("user data\n", encoding="utf-8")
    with pytest.raises(ocelot.OcelotError, match="not a usable managed install"):
        ocelot.install("2.0.0", URL, first_sha, dest=dest,
                       dry_run=False, allow_download=True)
    assert (unmanaged / "notes.txt").read_text(encoding="utf-8") == "user data\n"


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _installed_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    payload = _zip_bytes({
        "README.md": b"# Ocelot\n",
        "pkg/mod.py": b"x = 1\n",
    }, include_dir=True)
    sha = hashlib.sha256(payload).hexdigest()
    _forbid_network(monkeypatch)
    _mock_download(monkeypatch, payload)
    dest = tmp_path / "installs"
    ocelot.install("1.0.0", ZIP_URL, sha, dest=dest,
                   dry_run=False, allow_download=True)
    return dest


def test_verify_detects_tampering_missing_files_and_wrong_checksum(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    dest = _installed_zip(tmp_path, monkeypatch)
    target = dest / "1.0.0"
    assert ocelot.verify(dest=dest)["ok"] is True

    (target / "README.md").write_text("tampered\n", encoding="utf-8")
    report = ocelot.verify("1.0.0", dest=dest)
    assert report["ok"] is False
    assert report["state"] == "failed"
    assert report["files_mismatched"] == ["README.md"]
    assert any("do not match" in error for error in report["errors"])

    (target / "pkg" / "mod.py").unlink()
    report = ocelot.verify("1.0.0", dest=dest, sha256="0" * 64)
    assert report["ok"] is False
    assert report["files_missing"] == ["pkg/mod.py"]
    assert any("missing" in error for error in report["errors"])
    assert any("checksum" in error for error in report["errors"])


def test_verify_reports_extra_files_without_failing(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    dest = _installed_zip(tmp_path, monkeypatch)
    target = dest / "1.0.0"
    (target / "scratch.txt").write_text("local\n", encoding="utf-8")
    (target / "__pycache__").mkdir()
    (target / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"cache")
    report = ocelot.verify("1.0.0", dest=dest)
    assert report["ok"] is True
    assert report["extra_file_count"] == 1
    assert report["extra_files"] == ["scratch.txt"]


def test_verify_without_an_install_is_an_error(tmp_path: Path,
                                               isolated_home: Path) -> None:
    with pytest.raises(ocelot.OcelotError, match="no installed Ocelot manifest"):
        ocelot.verify(dest=tmp_path / "installs")


# --------------------------------------------------------------------------- #
# download plumbing (no socket)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers: dict[str, str] = {}

    def geturl(self) -> str:
        return URL

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self.payload)
        chunk, self.payload = self.payload[:amount], self.payload[amount:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ocelot.socket, "getaddrinfo",
        lambda *args, **kwargs: [
            (ocelot.socket.AF_INET, ocelot.socket.SOCK_STREAM, 6, "",
             ("93.184.216.34", 0))])


def test_download_enforces_the_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    monkeypatch.setattr(ocelot, "_open_url",
                        lambda request, timeout: _FakeResponse(b"0123456789!"))
    with pytest.raises(ocelot.OcelotError, match="byte limit"):
        ocelot._download(URL, max_bytes=10)


def test_download_caps_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)

    def redirect(request, timeout):
        raise ocelot.HTTPError(
            URL, 302, "Found", {"Location": URL}, None)

    monkeypatch.setattr(ocelot, "_open_url", redirect)
    with pytest.raises(ocelot.OcelotError, match="redirects"):
        ocelot._download(URL, max_redirects=2)


def test_download_rejects_https_downgrade_redirect(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)

    def redirect(request, timeout):
        raise ocelot.HTTPError(
            URL, 302, "Found",
            {"Location": "http://example.com/ocelot-1.0.0.tar.gz"}, None)

    monkeypatch.setattr(ocelot, "_open_url", redirect)
    with pytest.raises(ocelot.OcelotError, match="HTTPS"):
        ocelot._download(URL)


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def test_cli_install_is_dry_run_by_default(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _forbid_network(monkeypatch)
    dest = tmp_path / "installs"
    rc = ocelot_main([
        "install", "--version", "1.0.0", "--url", URL,
        "--sha256", "a" * 64, "--dest", str(dest),
    ])
    assert rc == 0
    assert "dry run" in capsys.readouterr().out
    assert not dest.exists()


def test_cli_validation_error_exits_two(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _forbid_network(monkeypatch)
    rc = ocelot_main([
        "install", "--version", "1.0.0", "--url", "http://example.com/x.tar.gz",
        "--sha256", "a" * 64, "--dest", str(tmp_path / "installs"),
    ])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_verify_failure_exits_one(
        tmp_path: Path, isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    dest = _installed_zip(tmp_path, monkeypatch)
    (dest / "1.0.0" / "README.md").write_text("tampered\n", encoding="utf-8")
    rc = ocelot_main(["verify", "--version", "1.0.0", "--dest", str(dest), "--json"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False


def test_cli_resolve_not_installed_is_informational(
        tmp_path: Path, isolated_home: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    rc = ocelot_main(["resolve", "--root", str(_root(tmp_path))])
    assert rc == 0
    assert "not installed" in capsys.readouterr().out
