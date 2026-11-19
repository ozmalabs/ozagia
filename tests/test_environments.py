"""Tests for project Python environments (core.environments, W20).

Fake virtual environments are made with `python -m venv --without-pip` plus
hand-written dist-info directories, or the probe is replaced by a stub. No
project code is run and nothing is fetched.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ozagia import cli
from ozagia.core import environments as EN
from ozagia.core import manifests as MF

from venv_fixture import make_venv


@pytest.fixture(autouse=True)
def _fresh_memo():
    EN.clear_memo()
    yield
    EN.clear_memo()


@pytest.fixture(scope="module")
def fake_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_venv(tmp_path_factory.mktemp("venvs") / "fake",
                     {"fake-numpy": ["zzfake_numpy", "pytest"],
                      "recordonly": None})


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _stub_probe(table: dict[str, Any]):
    """A probe runner: path -> set of modules, or an error string."""
    calls: list[str] = []

    def runner(python: str, *, timeout: float) -> dict[str, Any]:
        calls.append(python)
        value = table[python]
        if isinstance(value, str):
            return {"ok": False, "error": value}
        return {"ok": True, "version": "3.13.0", "executable": python,
                "prefix": f"/envs/{Path(python).name}", "base_prefix": "/usr",
                "stdlib": sorted(EN.STDLIB), "scanned": [],
                "distributions": len(value),
                "modules": {m: [m.replace("_", "-")] for m in value}}
    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _cands(*paths: str) -> list[dict[str, Any]]:
    return [{"path": p, "source": "path", "evidence": p, "also": []} for p in paths]


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discovery_order(tmp_path: Path, fake_venv: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    for name in (".venv", ".venv-extra", "outside"):
        target = root / name if name != "outside" else tmp_path / name
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "python").symlink_to(fake_venv)
    (root / "docs").mkdir()
    (root / "tools" / "py" / "bin").mkdir(parents=True)
    (root / "tools" / "py" / "bin" / "python3").symlink_to(fake_venv)
    _write(root, "README.md", "Run:\n\n```\ntools/py/bin/python3 -m pytest\n"
                              ".venv/bin/python -m pytest\n/no/such/bin/python\n```\n")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "outside"))
    on_path = tmp_path / "pathbin"
    (on_path).mkdir()
    (on_path / "python3").symlink_to(fake_venv)
    which = {"python3": str(on_path / "python3")}.get
    cands = EN.discover(root, which=which, command_docs=["README.md"])
    sources = [(c["source"], Path(c["path"]).relative_to(tmp_path).as_posix()
                if c["source"] != "ozagia" else "self") for c in cands]
    assert sources == [
        ("virtual_env", "outside/bin/python"),
        ("root_venv", "proj/.venv/bin/python"),
        ("root_venv", "proj/.venv-extra/bin/python"),
        ("command_docs", "proj/tools/py/bin/python3"),
        ("path", "pathbin/python3"),
        ("ozagia", "self"),
    ]
    # README's `.venv/bin/python` is the same interpreter as the root venv.
    assert any("command_docs" in a for a in cands[1]["also"])


def test_lock_file_tool_env(tmp_path: Path, fake_venv: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "poetry.lock").write_text("", encoding="utf-8")
    venv = tmp_path / "cache" / "proj-py3.13"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(fake_venv)
    asked: list[list[str]] = []

    def tool(argv, cwd):
        asked.append(list(argv))
        return str(venv)

    cands = EN.discover(root, which={"poetry": "/bin/poetry"}.get, tool=tool,
                        command_docs=[], include_self=False, trust=True)
    assert asked == [["poetry", "env", "info", "--path"]]
    assert [(c["source"], c["path"]) for c in cands] == [
        ("lock", str(venv / "bin" / "python"))]
    # Without the tool installed it is not asked.
    assert EN.discover(root, which=lambda _n: None, tool=tool, command_docs=[],
                       include_self=False, trust=True) == []
    assert len(asked) == 1
    # Nor untrusted, nor in read mode, nor with lock_tools=False.
    for kwargs in ({"trust": False}, {"trust": True, "cache_mode": "read"},
                   {"trust": True, "lock_tools": False}):
        assert EN.discover(root, which={"poetry": "/bin/poetry"}.get, tool=tool,
                           command_docs=[], include_self=False, **kwargs) == []
    assert len(asked) == 1


def test_worktree_and_clone_use_the_source_venv(tmp_path: Path,
                                                fake_venv: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _write(src, "a.py", "x = 1\n")
    _write(src, ".gitignore", ".venv/\n")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "base")
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "python").symlink_to(fake_venv)
    _git(src, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"))
    _git(tmp_path, "clone", "-q", str(src), str(tmp_path / "clone"))
    for work in (tmp_path / "wt", tmp_path / "clone"):
        assert EN.source_root(work) == src.resolve()
        cands = EN.discover(work, which=lambda _n: None, command_docs=[],
                            include_self=False)
        assert [c["source"] for c in cands] == ["source_venv"]
        assert cands[0]["path"] == str(src.resolve() / ".venv" / "bin" / "python")
    # A root with its own venv does not look at the source's.
    (tmp_path / "wt" / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "wt" / ".venv" / "bin" / "python").symlink_to(fake_venv)
    cands = EN.discover(tmp_path / "wt", which=lambda _n: None, command_docs=[],
                        include_self=False)
    assert [c["source"] for c in cands] == ["root_venv"]
    assert EN.source_root(src) is None


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #


def test_probe_reads_a_fake_venv(fake_venv: Path) -> None:
    result = EN.probe(str(fake_venv), cache=EN.ProbeCache(None, "off"))
    assert result["ok"] is True, result
    assert result["is_venv"] is True
    assert result["version"].startswith(f"{sys.version_info[0]}.{sys.version_info[1]}.")
    assert result["modules"]["zzfake_numpy"] == ["fake-numpy"]
    assert result["modules"]["recordonly_pkg"] == ["recordonly"]
    assert EN.provides(result, "zzfake_numpy.linalg")
    assert EN.provides(result, "json")
    assert not EN.provides(result, "numpy_not_here")


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_probe_is_bounded(tmp_path: Path) -> None:
    slow = _script(tmp_path / "slow", "sleep 5\n")
    out = EN.run_probe(slow, timeout=0.2)
    assert out == {"ok": False, "error": "probe timed out after 0.2 s"}
    noisy = _script(tmp_path / "noisy", "head -c 5000 /dev/zero\n")
    assert "over 100 bytes" in EN.run_probe(noisy, max_bytes=100)["error"]
    junk = _script(tmp_path / "junk", "echo hello\n")
    assert "no JSON" in EN.run_probe(junk)["error"]
    failing = _script(tmp_path / "failing", "echo boom >&2; exit 3\n")
    assert EN.run_probe(failing)["error"] == "probe exited 3: boom"
    assert "cannot run" in EN.run_probe(str(tmp_path / "missing"))["error"]
    # A failed probe is not cached.
    cache = EN.ProbeCache(tmp_path / "probes.json", "rw")
    assert EN.probe(slow, cache=cache, timeout=0.2)["ok"] is False
    cache.save()
    assert not (tmp_path / "probes.json").exists()


def test_probe_cache_follows_site_packages(tmp_path: Path) -> None:
    python = make_venv(tmp_path / "v", {"alpha": ["alpha"]})
    state = tmp_path / "state" / "probes.json"
    first = EN.probe(str(python), cache=EN.ProbeCache(state, "rw"))
    assert first["cached"] is False and "alpha" in first["modules"]
    # Not written yet: save() writes.
    assert not state.exists()
    cache = EN.ProbeCache(state, "rw")
    EN.clear_memo()
    EN.probe(str(python), cache=cache)
    cache.save()
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["schema"] == EN.CACHE_SCHEMA and str(python) in doc["probes"]

    def refuse(*_a, **_k):
        raise AssertionError("probed again")

    EN.clear_memo()
    again = EN.probe(str(python), cache=EN.ProbeCache(state, "read"), runner=refuse)
    assert again["cached"] == "file" and "alpha" in again["modules"]
    assert EN.probe(str(python), runner=refuse)["cached"] == "memo"
    # Installing a distribution changes site-packages: the entry is stale.
    site = next((tmp_path / "v" / "lib").glob("python*/site-packages"))
    info = site / "beta-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: beta\nVersion: 1\n",
                                   encoding="utf-8")
    (info / "top_level.txt").write_text("beta\n", encoding="utf-8")
    later = site.stat().st_mtime + 5
    os.utime(site, (later, later))
    EN.clear_memo()
    fresh = EN.probe(str(python), cache=EN.ProbeCache(state, "read"))
    assert fresh["cached"] is False and "beta" in fresh["modules"]
    # Mode "off" neither reads the file nor uses the memo.
    calls = []
    EN.probe(str(python), cache=EN.ProbeCache(state, "off"),
             runner=lambda p, timeout: calls.append(p) or {"ok": False, "error": "x"})
    assert calls == [str(python)]
    with pytest.raises(EN.EnvError):
        EN.ProbeCache(state, "sometimes")


# --------------------------------------------------------------------------- #
# declared dependencies
# --------------------------------------------------------------------------- #


def test_declared_and_optional_dependencies(tmp_path: Path) -> None:
    pyproject = (
        "[project]\nname = 'x'\ndependencies = ['requests>=2']\n"
        "[project.optional-dependencies]\nplot = ['matplotlib']\n"
        "[dependency-groups]\ndev = ['pytest', {include-group = 'lint'}]\n"
        "lint = ['ruff']\n"
        "[tool.poetry.dependencies]\npython = '^3.11'\nPyYAML = '*'\n"
        "scipy = {version = '*', optional = true}\n"
        "[tool.poetry.group.test.dependencies]\nhypothesis = '*'\n")
    parsed = MF.parse_manifest("pyproject.toml", pyproject)
    deps = {(d["name"], d["optional"], d["group"]) for d in parsed["dependencies"]}
    assert deps == {("requests", False, None), ("matplotlib", True, "plot"),
                    ("pytest", True, "dev"), ("ruff", True, "lint"),
                    ("PyYAML", False, None), ("scipy", True, "extras"),
                    ("hypothesis", True, "test")}
    _write(tmp_path, "pyproject.toml", pyproject)
    _write(tmp_path, "requirements.txt", "flask==3.0\n")
    _write(tmp_path, "requirements-dev.txt", "-r requirements.txt\ncoverage\n")
    _write(tmp_path, "requirements-arc.txt", "numpy>=1.26  # arc only\n")
    _write(tmp_path, "requirements-base.txt", "attrs\n")
    _write(tmp_path, "requirements-bad.txt", "")
    _write(tmp_path, "package.json", '{"dependencies": {"left-pad": "1"}}')
    _write(tmp_path, ".venv/lib/requirements.txt", "ignored\n")
    files = ["pyproject.toml", "requirements.txt", "requirements-dev.txt",
             "requirements-arc.txt", "requirements-base.txt", "package.json",
             ".venv/lib/requirements.txt"]
    declared = EN.declared_dependencies(tmp_path, files)
    assert "package.json" not in declared["manifests"]
    assert ".venv/lib/requirements.txt" not in declared["manifests"]
    by = declared["by_name"]
    assert by["numpy"] == [{"name": "numpy", "file": "requirements-arc.txt",
                            "optional": True, "group": "arc"}]
    assert by["attrs"][0]["optional"] is False
    assert by["coverage"][0]["group"] == "dev"
    assert by["flask"][0]["optional"] is False
    assert "ignored" not in by
    # Import names map to distributions by normalization and aliases.
    assert EN.declarations("yaml", declared)[0]["name"] == "PyYAML"
    assert EN.declarations("numpy.linalg", declared)[0]["file"] == "requirements-arc.txt"
    assert EN.declarations("left_pad", declared) == []
    # ... and through what a probed interpreter installs a module from.
    assert EN.declarations("flask_ext", declared,
                           [{"modules": {"flask_ext": ["Flask"]}}])[0]["name"] == "flask"
    assert MF.is_dev_group("dev") and MF.is_dev_group("requirements-test")
    assert not MF.is_dev_group("arc") and not MF.is_dev_group(None)


# --------------------------------------------------------------------------- #
# imports
# --------------------------------------------------------------------------- #


def test_file_imports_kinds() -> None:
    text = (
        "import os, numpy as np\n"
        "from typing import TYPE_CHECKING\n"
        "import pytest\n"
        "pytest.importorskip('torch')\n"
        "import torch\n"
        "try:\n    import ujson\nexcept ImportError:\n    import json as ujson\n"
        "try:\n    import lxml\nexcept KeyError:\n    pass\n"
        "if TYPE_CHECKING:\n    import pandas\n"
        "class A:\n    import yaml\n    def m(self):\n        import scipy\n"
        "def f():\n    sk = pytest.importorskip('sklearn')\n    from . import sib\n"
        "if __name__ == '__main__':\n    import click\n")
    kinds = {r["module"]: r["kind"] for r in EN.file_imports(text) if r["module"]}
    assert kinds == {"os": "module", "numpy": "module", "typing": "module",
                     "pytest": "module", "torch": "guarded", "ujson": "guarded",
                     "json": "guarded", "lxml": "module", "pandas": "guarded",
                     "yaml": "module", "scipy": "lazy", "sklearn": "guarded",
                     "click": "lazy"}
    rel = [r for r in EN.file_imports(text) if r["level"]]
    assert rel == [{"module": "", "names": ["sib"], "level": 1, "kind": "lazy",
                    "line": 22}]
    # A file that does not parse falls back to a line scan.
    broken = EN.file_imports("import numpy\nfrom .x import (a,\n b)\ndef (:\n")
    assert [(r["module"], r["level"], r["names"]) for r in broken] == [
        ("numpy", 0, []), ("x", 1, ["a", "b"])]
    assert EN.file_imports("x = 1\n") == []


def _tree(root: Path) -> list[str]:
    files = {
        "src/pkg/__init__.py": "import yaml\n",
        "src/pkg/core.py": "import numpy\nfrom . import helpers\n\n\n"
                           "def later():\n    import scipy\n",
        "src/pkg/helpers.py": "try:\n    import ujson\nexcept ImportError:\n"
                              "    ujson = None\n",
        "src/pkg/unused.py": "import torch\n",
        "tests/conftest.py": "import pytest\n",
        "tests/sub/conftest.py": "import hypothesis\n",
        "tests/helper_mod.py": "import requests\n",
        "tests/test_core.py": "import json\nfrom pkg.core import later\n"
                              "import helper_mod\n\n\n"
                              "def test_it():\n    import matplotlib\n",
        "tools/scripts/oddly_placed.py": "import pyarrow\n",
        "scripts/run.py": "import oddly_placed\n",
    }
    for rel, text in files.items():
        _write(root, rel, text)
    return sorted(files)


def test_transitive_requirements(tmp_path: Path) -> None:
    files = _tree(tmp_path)
    model = EN.ImportModel(tmp_path, files, import_roots=["src"])
    need = model.requirements(["tests/test_core.py"])
    assert sorted(need["required"]) == ["matplotlib", "numpy", "pytest",
                                        "requests", "yaml"]
    assert need["required"]["numpy"] == ["src/pkg/core.py"]
    # Closure-level lazy and guarded imports are optional; files nobody
    # imports (unused.py) and conftests of other dirs are not read.
    assert sorted(need["optional"]) == ["scipy", "ujson"]
    assert "json" in need["stdlib"]
    assert need["closure"] == ["src/pkg/__init__.py", "src/pkg/core.py",
                               "src/pkg/helpers.py", "tests/conftest.py",
                               "tests/helper_mod.py", "tests/test_core.py"]
    # Without lazy_required the test's function-level imports are optional.
    loose = model.requirements(["tests/test_core.py"], lazy_required=False)
    assert "matplotlib" in loose["optional"]
    # A module found only elsewhere in the tree is first-party and followed.
    assert sorted(model.requirements(["scripts/run.py"])["required"]) == ["pyarrow"]
    assert model.module_name("src/pkg/core.py") == "pkg.core"
    assert model.third_party("src/pkg/helpers.py") == {
        "ujson": {"kind": "guarded", "line": 2}}


def test_env_for_ranks_interpreters(tmp_path: Path) -> None:
    files = _tree(tmp_path)
    _write(tmp_path, "requirements-plot.txt", "matplotlib\n")
    files.append("requirements-plot.txt")
    model = EN.ImportModel(tmp_path, files, import_roots=["src"])
    full = {"numpy", "pytest", "requests", "yaml", "matplotlib"}
    runner = _stub_probe({"/e/partial": {"numpy"},
                          "/e/full": full,
                          "/e/fuller": full | {"scipy", "ujson"},
                          "/e/broken": "probe timed out after 10 s"})
    doc = EN.env_for(tmp_path, ["tests/test_core.py::test_it"],
                     candidates=_cands("/e/partial", "/e/broken", "/e/full",
                                       "/e/fuller"),
                     runner=runner, cache_mode="off", model=model)
    order = [e["path"] for e in doc["interpreters"]]
    assert order == ["/e/fuller", "/e/full", "/e/partial", "/e/broken"]
    assert doc["best"]["path"] == "/e/fuller"
    assert doc["interpreters"][1]["missing_optional"] == ["scipy", "ujson"]
    assert doc["interpreters"][2]["missing"] == ["matplotlib", "pytest",
                                                 "requests", "yaml"]
    assert doc["interpreters"][3]["ok"] is False
    assert doc["paths"] == ["tests/test_core.py"]
    numpy = doc["imports"]["numpy"]
    assert numpy["required"] and numpy["have"] == ["/e/partial", "/e/full", "/e/fuller"]
    assert doc["imports"]["matplotlib"]["declared"] == [
        {"name": "matplotlib", "file": "requirements-plot.txt", "optional": True,
         "group": "plot"}]
    assert doc["imports"]["scipy"]["required"] is False
    # An extra module (the check's `-m pytest`) is required too.
    runner = _stub_probe({"/e/a": {"numpy", "yaml", "scipy"},
                          "/e/b": {"numpy", "yaml", "scipy", "black"}})
    doc = EN.env_for(tmp_path, ["src/pkg/core.py"], extra_modules=["black"],
                     candidates=_cands("/e/a", "/e/b"), runner=runner,
                     cache_mode="off", model=model)
    assert doc["best"]["path"] == "/e/b"
    assert doc["imports"]["black"]["files"] == ["<check command>"]
    with pytest.raises(EN.EnvError):
        EN.env_for(tmp_path, ["../elsewhere.py"], candidates=[], cache_mode="off",
                   model=model)


def test_who_has(tmp_path: Path) -> None:
    runner = _stub_probe({"/e/a": {"numpy"}, "/e/b": set(), "/e/c": "broken",
                          "/e/d": {"numpy"}})
    out = EN.who_has(tmp_path, "numpy.linalg",
                     candidates=_cands("/e/a", "/e/b", "/e/c", "/e/d"),
                     runner=runner, cache_mode="off")
    assert out == [{"interpreter": "/e/a", "version": "3.13.0", "probe": "exec"},
                   {"interpreter": "/e/d", "version": "3.13.0", "probe": "exec"}]
    assert EN.who_has(tmp_path, "zz_none", candidates=_cands("/e/a"),
                      runner=runner, cache_mode="off") == []


def test_env_for_with_a_real_fake_venv(tmp_path: Path, fake_venv: Path) -> None:
    _write(tmp_path, "tests/test_x.py", "import zzfake_numpy\n")
    doc = EN.env_for(tmp_path, ["tests/test_x.py"], extra_modules=["pytest"],
                     candidates=_cands(str(fake_venv)), cache_mode="rw")
    assert doc["best"]["path"] == str(fake_venv)
    assert doc["required"] == ["pytest", "zzfake_numpy"]
    # The cache is under the state dir.
    assert (tmp_path / ".ozagia" / EN.CACHE_DIR / EN.CACHE_NAME).is_file()


# --------------------------------------------------------------------------- #
# surfaces
# --------------------------------------------------------------------------- #


def test_cli_env(tmp_path: Path, fake_venv: Path, monkeypatch,
                 capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "tests/test_x.py", "import zzfake_numpy\n")
    _write(tmp_path, "requirements.txt", "fake-numpy\n")
    real = EN.discover
    monkeypatch.setattr(EN, "discover", lambda root, **_k: _cands(str(fake_venv)))
    assert cli.main(["env", "--root", str(tmp_path), "--for", "tests/test_x.py",
                     "--json", "--no-cache"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"]["path"] == str(fake_venv)
    assert doc["imports"]["zzfake_numpy"]["declared"][0]["file"] == "requirements.txt"
    assert not (tmp_path / ".ozagia").exists()
    assert cli.main(["env", "--root", str(tmp_path), "--no-cache"]) == 0
    out = capsys.readouterr().out
    assert "interpreters (in order of evidence):" in out and "fake-numpy" in out
    _write(tmp_path, "tests/test_y.py", "import zz_nowhere\n")
    assert cli.main(["env", "--root", str(tmp_path), "--for", "tests/test_y.py",
                     "--no-cache"]) == 1
    assert "best: none" in capsys.readouterr().out
    assert cli.main(["env", "--root", str(tmp_path), "--for", "/elsewhere/x.py",
                     "--no-cache"]) == 2
    capsys.readouterr()
    # The profile subcommands are unchanged.
    assert cli.main(["env", "list", "--root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["environments"] == []
    monkeypatch.setattr(EN, "discover", real)


def test_mcp_dev_environment(tmp_path: Path, fake_venv: Path, monkeypatch) -> None:
    from ozagia.integrations.mcp.server import DevMemoryServer
    from ozagia.core.memory import DevMemory, build_index
    root = tmp_path / "repo"
    _write(root, "tests/test_x.py", "import zzfake_numpy\n")
    graph = root / ".ozagia" / "graph.json"
    build_index(root, graph)
    server = DevMemoryServer(DevMemory(graph, root / ".ozagia" / "journal.jsonl",
                                       root=root))
    monkeypatch.setattr(EN, "discover", lambda root, **_k: _cands(str(fake_venv)))

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "dev_environment",
                                         "arguments": arguments}})
        assert resp["result"]["isError"] is False, resp
        return json.loads(resp["result"]["content"][0]["text"])

    assert call({"paths": ["tests/test_x.py"]})["best"]["path"] == str(fake_venv)
    assert call({"module": "zzfake_numpy"})["interpreters"][0]["interpreter"] \
        == str(fake_venv)
    listing = call({})
    assert listing["ok"] and listing["interpreters"][0]["path"] == str(fake_venv)
    assert call({"paths": ["../x.py"]})["ok"] is False


# --------------------------------------------------------------------------- #
# trust: what is run
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_user_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Path:
    """An empty user-level config dir (XDG_CONFIG_HOME)."""
    xdg = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg


def _site(venv: Path) -> Path:
    v = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    site = venv / "lib" / v / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    return site


def _fake_dist(site: Path, name: str, tops: list[str] | None,
               record: list[str] | None = None) -> None:
    info = site / f"{name}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8")
    if tops is not None:
        (info / "top_level.txt").write_text("\n".join(tops) + "\n", encoding="utf-8")
    if record is not None:
        (info / "RECORD").write_text("\n".join(f"{r},," for r in record) + "\n",
                                     encoding="utf-8")


def _shipped_script_repo(tmp_path: Path, marker: Path) -> Path:
    """A git repo that commits a shell script as `.venv/bin/python` (next to
    a pyvenv.cfg and site-packages) and names `tools/python` in README."""
    root = tmp_path / "repo"
    root.mkdir()
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    _script(venv / "bin" / "python", f"echo venv >> {marker}\nexit 1\n")
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
        "version = 3.12.4\n", encoding="utf-8")
    _fake_dist(_site(venv), "zz-shipped", ["zz_shipped"])
    (root / "tools").mkdir()
    _script(root / "tools" / "python", f"echo tools >> {marker}\nexit 1\n")
    _write(root, "README.md", "Run `tools/python -m pytest`.\n")
    _write(root, "tests/test_s.py", "import zz_shipped\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "ship a python")
    return root


def test_committed_script_python_is_not_run_untrusted(
        tmp_path: Path, monkeypatch, no_user_config: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    from ozagia.core import repo_review as RR
    marker = tmp_path / "ran"
    root = _shipped_script_repo(tmp_path, marker)
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "state"))
    cands = EN.discover(root, which=lambda _n: None, include_self=False)
    assert [(c["source"], c["project"]) for c in cands] == [
        ("root_venv", True), ("command_docs", True)]
    assert EN.trust_project_python(root) is False
    doc = EN.environments(root, cache_mode="off")
    by_source = {e["source"]: e for e in doc["interpreters"]}
    assert by_source["root_venv"]["probe"] == "static"
    assert by_source["root_venv"]["version"] == "3.12.4"
    assert "not run" in by_source["command_docs"]["error"]
    found = EN.env_for(root, ["tests/test_s.py"], cache_mode="off")
    assert found["best"]["path"] == str(root / ".venv" / "bin" / "python")
    assert found["best"]["probe"] == "static" and found["trusted"] is False
    assert cli.main(["env", "--root", str(root), "--for", "tests/test_s.py"]) == 0
    out = capsys.readouterr().out
    assert "read statically, not run" in out and EN.TRUST_ENV in out
    packet = RR.review_repo(root, sections=["dependencies"])
    data = packet["sections"]["dependencies"]["data"]
    assert {i["probe"] for i in data["interpreters"] if i["project"]} \
        <= {"static", "failed"}
    assert data["trust_project_python"] is False
    assert not marker.exists()
    # Trusted (the variable): both are run.
    EN.clear_memo()
    monkeypatch.setenv(EN.TRUST_ENV, "1")
    doc = EN.environments(root, cache_mode="off")
    assert doc["trusted"] is True
    assert sorted(marker.read_text().split()) == ["tools", "venv"]


def test_symlinked_system_python_with_a_committed_pth_is_not_run(
        tmp_path: Path, monkeypatch, no_user_config: Path) -> None:
    """`.venv/bin/python` links to a system interpreter (its realpath is
    outside the root), but the committed pyvenv.cfg and .pth would run."""
    marker = tmp_path / "pth_ran"
    root = tmp_path / "repo"
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    base = os.path.realpath(sys.executable)
    (venv / "bin" / "python").symlink_to(base)
    (venv / "pyvenv.cfg").write_text(
        f"home = {os.path.dirname(base)}\ninclude-system-site-packages = false\n"
        f"version = {sys.version_info[0]}.{sys.version_info[1]}.0\n",
        encoding="utf-8")
    site = _site(venv)
    (site / "zz.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pth')\n",
        encoding="utf-8")
    _fake_dist(site, "zz-linked", ["zz_linked"])
    cands = EN.discover(root, which=lambda _n: None, command_docs=[],
                        include_self=False)
    assert cands[0]["project"] is True
    assert not EN.is_project_shipped(base, [root])
    out = EN.probe_candidates(root, cache_mode="off", candidates=cands)
    assert out[0]["probe"]["probe"] == "static"
    assert "zz_linked" in out[0]["probe"]["modules"]
    assert not marker.exists()
    # Trusted: run, and the .pth runs with it (what trust allows).
    EN.clear_memo()
    out = EN.probe_candidates(root, cache_mode="off", candidates=cands, trust=True)
    assert out[0]["probe"]["ok"] and out[0]["probe"]["probe"] == "exec"
    assert marker.read_text() == "pth"


def test_static_probe_reads_dist_info_as_text(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    _script(venv / "bin" / "python", "exit 1\n")
    (venv / "pyvenv.cfg").write_text(
        "home = /opt/py/bin\nversion_info = 3.11.9.final.0\n"
        "include-system-site-packages = true\n", encoding="utf-8")
    site = _site(venv)
    _fake_dist(site, "numpy", ["numpy"])
    _fake_dist(site, "python-chess", None, record=["chess/__init__.py",
                                                    "python_chess-1.0.dist-info/METADATA",
                                                    "single_mod.py", "../../bin/tool"])
    (site / "plainpkg").mkdir()
    (site / "plainpkg" / "__init__.py").write_text("", encoding="utf-8")
    extra = tmp_path / "editable_src"
    (extra / "editpkg").mkdir(parents=True)
    (site / "x.pth").write_text(f"{extra}\nimport os; os.exit(3)\n", encoding="utf-8")
    raw = EN.static_probe(str(venv / "bin" / "python"))
    assert raw["ok"] and raw["probe"] == "static" and raw["version"] == "3.11.9"
    mods = raw["modules"]
    assert mods["numpy"] == ["numpy"]
    assert mods["chess"] == ["python-chess"] and mods["single_mod"] == ["python-chess"]
    assert mods["plainpkg"] == [] and mods["editpkg"] == []
    assert raw["distributions"] == 2 and raw["system_site_packages"] is True
    assert str(extra) in raw["scanned"]
    # Not a venv: nothing to read, and nothing is run.
    (root / "tools").mkdir()
    lone = _script(root / "tools" / "python", "exit 1\n")
    assert "not run" in EN.static_probe(lone)["error"]
    # include-system-site-packages: the base's packages from a run candidate
    # in `home`.
    probed = [{"path": str(venv / "bin" / "python"),
               "probe": EN._normalize_probe(str(venv / "bin" / "python"), raw,
                                            "static")},
              {"path": "/opt/py/bin/python3",
               "probe": {"ok": True, "probe": "exec", "path": "/opt/py/bin/python3",
                         "modules": {"sysonly": ["sysonly"]}, "stdlib": ["json"]}}]
    EN.merge_system_site(probed)
    assert "sysonly" in probed[0]["probe"]["modules"]
    assert probed[0]["probe"]["system_site_from"] == "/opt/py/bin/python3"


def test_trust_from_user_config_not_the_repo(tmp_path: Path, monkeypatch,
                                             no_user_config: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert EN.trust_project_python(root) is False
    _write(no_user_config, "ozagia/config.toml",
           "[environments]\ntrust_project_python = true\n")
    assert EN.trust_project_python(root) is True
    # A user config inside the root does not count.
    _write(root, "xdg/ozagia/config.toml",
           "[environments]\ntrust_project_python = true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))
    assert EN.trust_project_python(root) is False
    # Nor does the repository's own config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    _write(root, ".ozagia.toml", "[environments]\ntrust_project_python = true\n")
    assert EN.trust_project_python(root) is False
    monkeypatch.setenv(EN.TRUST_ENV, "1")
    assert EN.trust_project_python(root) is True


def test_project_shipped_rule(tmp_path: Path, fake_venv: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    (root / "envs" / "bin").mkdir(parents=True)
    _script(root / "envs" / "bin" / "python", "exit 1\n")
    (root / "pdm.lock").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "python").symlink_to(fake_venv)
    monkeypatch.setenv("VIRTUAL_ENV", str(outside))
    # pdm's answer (a trusted project: the tool runs) points inside the root.
    cands = EN.discover(root, which={"pdm": "/bin/pdm"}.get,
                        tool=lambda argv, cwd: str(root / "envs" / "bin" / "python"),
                        command_docs=[], include_self=False, trust=True)
    assert [(c["source"], c["project"]) for c in cands] == [
        ("virtual_env", False), ("lock", True)]
    # Untrusted, `.pdm-python` is read as text: the same interpreter, judged
    # project-shipped, and pdm is not asked.
    (root / ".pdm-python").write_text("envs/bin/python\n", encoding="utf-8")

    def no_tool(argv, cwd):
        raise AssertionError(f"ran {argv}")

    cands = EN.discover(root, which={"pdm": "/bin/pdm"}.get, tool=no_tool,
                        command_docs=[], include_self=False, trust=False)
    assert [(c["source"], c["project"], c["evidence"]) for c in cands] == [
        ("virtual_env", False, f"$VIRTUAL_ENV={outside}"),
        ("lock", True, "pdm.lock: .pdm-python (read as text)")]
    # A PATH entry that links into the root is the project's too.
    into = tmp_path / "bin2"
    into.mkdir()
    (into / "python3").symlink_to(root / "envs" / "bin" / "python")
    assert EN.is_project_shipped(into / "python3", [root])
    assert EN.is_self(sys.executable)


def test_probe_environment_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    dump = tmp_path / "env.txt"
    python = _script(tmp_path / "python", f"env > {dump}\nexit 1\n")
    for key, value in {"SECRET_TOKEN": "x", "OZAGIA_STATE_DIR": "/s",
                       "PYTHONPATH": "/p", "VIRTUAL_ENV": "/v", "AWS_PROFILE": "a",
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "LC_CTYPE": "C.UTF-8",
                       "TMPDIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    assert EN.run_probe(python)["ok"] is False
    keys = {line.split("=", 1)[0] for line in dump.read_text().splitlines()
            if "=" in line}
    assert keys - {"PWD", "SHLVL", "_", "OLDPWD"} <= {"PATH", "HOME", "LANG",
                                                      "LC_ALL", "LC_CTYPE", "TMPDIR"}
    assert {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "PATH"} <= keys
    assert "SECRET_TOKEN" not in keys and "OZAGIA_STATE_DIR" not in keys


def test_failed_probes_are_remembered_for_the_run(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(python: str, *, timeout: float) -> dict[str, Any]:
        calls.append(python)
        return {"ok": False, "error": "probe timed out after 10 s"}

    cache = EN.ProbeCache(tmp_path / "probes.json", "rw")
    for _ in range(3):
        assert EN.probe("/e/hangs", cache=cache, runner=runner)["ok"] is False
    assert calls == ["/e/hangs"]
    cache.save()
    assert not (tmp_path / "probes.json").exists()


def test_pyvenv_cfg_is_in_the_cache_signature(tmp_path: Path) -> None:
    python = make_venv(tmp_path / "v", {"alpha": ["alpha"]})
    state = tmp_path / "probes.json"
    cache = EN.ProbeCache(state, "rw")
    EN.probe(str(python), cache=cache)
    cache.save()
    EN.clear_memo()
    assert EN.probe(str(python), cache=EN.ProbeCache(state, "read"))["cached"] == "file"
    cfg = tmp_path / "v" / "pyvenv.cfg"
    cfg.write_text(cfg.read_text().replace("include-system-site-packages = false",
                                           "include-system-site-packages = true"))
    later = cfg.stat().st_mtime + 5
    os.utime(cfg, (later, later))
    EN.clear_memo()
    again = EN.probe(str(python), cache=EN.ProbeCache(state, "read"))
    assert again["cached"] is False and again["ok"]


# --------------------------------------------------------------------------- #
# import kinds: function-level importorskip, version and platform branches
# --------------------------------------------------------------------------- #


def test_function_importorskip_and_conditional_imports() -> None:
    text = (
        "import sys, pytest\n"
        "def test_a():\n"
        "    import substrate_rs as early\n"
        "    pytest.importorskip('substrate_rs')\n"
        "    import substrate_rs\n"
        "    from substrate_rs.x import y\n"
        "def test_b():\n"
        "    import substrate_rs\n"
        "if sys.version_info >= (3, 11):\n    import tomllib\n"
        "else:\n    import tomli\n"
        "if sys.platform == 'win32':\n    import win32api\n"
        "elif sys.platform.startswith('linux'):\n    import zz_linux\n"
        "if platform.system() == 'Darwin':\n    import zz_mac\n"
        "def f():\n    if sys.version_info[:2] < (3, 9):\n        import zz_old\n")
    recs = EN.file_imports(text)
    kinds = [(r["module"], r["line"], r["kind"]) for r in recs if r["module"]]
    assert kinds == [
        ("sys", 1, "module"), ("pytest", 1, "module"),
        ("substrate_rs", 3, "lazy"), ("substrate_rs", 4, "guarded"),
        ("substrate_rs", 5, "guarded"), ("substrate_rs.x", 6, "guarded"),
        ("substrate_rs", 8, "lazy"),
        ("tomllib", 10, "conditional"), ("tomli", 12, "conditional"),
        ("win32api", 14, "conditional"), ("zz_linux", 16, "conditional"),
        ("zz_mac", 18, "conditional"), ("zz_old", 21, "conditional")]
    by = {r["module"]: r for r in recs}
    assert by["tomli"]["cond"] == [{"kind": "version", "op": ">=", "value": [3, 11],
                                    "slice": None, "negate": True}]
    assert by["tomli"]["base"] == "module" and by["zz_old"]["base"] == "lazy"
    assert by["zz_mac"]["cond"] == [{"kind": "unknown"}]
    # elif: the negated first test and the second one.
    assert [c["kind"] for c in by["zz_linux"]["cond"]] == ["platform", "platform"]
    ev = EN.evaluate_conditions
    assert ev(by["tomli"]["cond"], (3, 10, 1)) is True
    assert ev(by["tomli"]["cond"], (3, 13, 0)) is False
    assert ev(by["tomllib"]["cond"], None) is None
    assert ev(by["win32api"]["cond"], (3, 13), platform="linux") is False
    assert ev(by["zz_linux"]["cond"], (3, 13), platform="linux") is True
    assert ev(by["zz_old"]["cond"], (3, 8, 2)) is True
    assert ev(by["zz_mac"]["cond"], (3, 13)) is None


def _versioned_runner(table: dict[str, tuple[str, set[str]]]):
    def runner(python: str, *, timeout: float) -> dict[str, Any]:
        version, mods = table[python]
        return {"ok": True, "version": version, "executable": python,
                "prefix": python, "base_prefix": "/usr", "stdlib": [],
                "scanned": [], "distributions": len(mods),
                "modules": {m: [m] for m in mods}}
    return runner


def test_env_for_evaluates_conditions_per_interpreter(tmp_path: Path,
                                                      monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    _write(tmp_path, "tests/test_c.py",
           "import sys, pytest\n"
           "if sys.version_info >= (3, 11):\n    import tomllib\n"
           "else:\n    import tomli\n"
           "if sys.platform == 'win32':\n    import win32api\n"
           "if platform.system() == 'Darwin':\n    import zz_mac\n"
           "def test_x():\n    pytest.importorskip('substrate_rs')\n"
           "    import substrate_rs\n")
    model = EN.ImportModel(tmp_path, ["tests/test_c.py"])
    runner = _versioned_runner({"/e/py310": ("3.10.4", {"pytest"}),
                                "/e/py313": ("3.13.1", {"pytest"}),
                                "/e/py310t": ("3.10.4", {"pytest", "tomli", "zz_mac"})})
    doc = EN.env_for(tmp_path, ["tests/test_c.py"],
                     candidates=_cands("/e/py310", "/e/py313", "/e/py310t"),
                     runner=runner, cache_mode="off", model=model)
    by = {e["path"]: e for e in doc["interpreters"]}
    assert doc["required"] == ["pytest"]
    assert by["/e/py313"]["complete"] and by["/e/py313"]["missing_optional"] == [
        "substrate_rs", "zz_mac"]
    assert by["/e/py310"]["missing"] == ["tomli"]
    assert by["/e/py310t"]["complete"]
    # Both complete; py310t misses fewer optional imports.
    assert doc["best"]["path"] == "/e/py310t"
    assert doc["imports"]["tomli"]["conditional"] is True
    assert "win32api" in doc["conditional"]


def test_anywhere_fallback_yields_to_installed_distributions(tmp_path: Path) -> None:
    files = {"domains/__init__.py": "", "domains/chess.py": "import chess as pc\n",
             "domains/worlds.py": "import chess\n",
             "tests/test_chess.py": "import chess\n",
             "tools/helpers/only_here.py": "x = 1\n",
             "scripts/run.py": "import only_here\n"}
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    model = EN.ImportModel(tmp_path, sorted(files))
    # No interpreter provides chess: the old rule (a module of that name in
    # the tree).
    assert model.is_first_party("chess", "tests/test_chess.py")
    runner = _stub_probe({"/e/with": {"chess"}, "/e/without": set()})
    doc = EN.env_for(tmp_path, ["tests/test_chess.py"],
                     candidates=_cands("/e/without", "/e/with"), runner=runner,
                     cache_mode="off", model=model)
    assert doc["required"] == ["chess"]
    assert doc["best"]["path"] == "/e/with"
    assert [e["missing"] for e in doc["interpreters"]] == [[], ["chess"]]
    assert model.third_party("domains/worlds.py") == {
        "chess": {"kind": "module", "line": 1}}
    # A tree-only module nobody installs stays first-party.
    assert model.is_first_party("only_here", "scripts/run.py")


# --------------------------------------------------------------------------- #
# the import cache
# --------------------------------------------------------------------------- #


def test_import_cache_by_blob_id(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write(root, "a.py", "import numpy\n")
    _write(root, "b.py", "import requests\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    blobs = EN.blob_ids(root)
    assert set(blobs) == {"a.py", "b.py"}
    path = tmp_path / "state" / "imports.json"
    first = EN.ImportModel(root, blobs=blobs, cache=EN.ImportCache(path, "rw"))
    assert first.third_party("a.py") == {"numpy": {"kind": "module", "line": 1}}
    first.third_party("b.py")
    first.save_cache()
    assert first.cache.misses == 2 and path.is_file()

    def refuse(rel: str) -> str:
        raise AssertionError(f"read {rel}")

    again = EN.ImportModel(root, blobs=EN.blob_ids(root), read=refuse,
                           cache=EN.ImportCache(path, "read"))
    assert again.third_party("a.py") == {"numpy": {"kind": "module", "line": 1}}
    assert again.cache.hits == 1
    # A file changed in the working tree has no blob id: it is read.
    _write(root, "a.py", "import scipy  # changed\n")
    blobs = EN.blob_ids(root)
    assert "a.py" not in blobs and "b.py" in blobs
    fresh = EN.ImportModel(root, blobs=blobs, cache=EN.ImportCache(path, "rw"))
    assert set(fresh.third_party("a.py")) == {"scipy"}
    # Save keeps only the blobs of the current tree.
    fresh.third_party("b.py")
    fresh.save_cache()
    doc = json.loads(path.read_text())
    assert set(doc["blobs"]) == {blobs["b.py"]}
    assert EN.blob_ids(tmp_path / "nogit") == {}


# --------------------------------------------------------------------------- #
# W23: the failure classifier's environment detail, through the real model
# --------------------------------------------------------------------------- #


def _missing_module_log(module: str) -> tuple[str, str]:
    """A real collection-error check log (W21 fixture) for `module`."""
    fix = Path(__file__).parent / "fixtures" / "failures" / "check_learner-board-pairs.log"
    text = fix.read_text(encoding="utf-8").replace("numpy", module)
    _head, _, rest = text.partition("\n")
    out, _, err = rest.partition("\n--- stderr ---\n")
    return out, err


def test_failure_classes_use_the_real_environment_model(tmp_path: Path, monkeypatch,
                                                        no_user_config: Path) -> None:
    from ozagia.core import failures as F
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "tests/test_learner.py", "import zzfake_numpy\n")
    _write(repo, "requirements-ml.txt", "fake-numpy\n")
    _write(repo, "requirements.txt", "requests\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    venv_python = make_venv(repo / ".venv", {"fake-numpy": ["zzfake_numpy"]})
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv(EN.TRUST_ENV, raising=False)
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(EN, "_lock_candidates", lambda *a, **k: [])
    ran: list[str] = []
    real_run_probe = EN.run_probe

    def spy(python: str, **kw: Any) -> dict[str, Any]:
        ran.append(os.path.abspath(python))
        return real_run_probe(python, **kw)
    monkeypatch.setattr(EN, "run_probe", spy)
    discovered: list[Path] = []
    real_discover = EN.discover
    monkeypatch.setattr(EN, "discover",
                        lambda root, **kw: discovered.append(root) or real_discover(root, **kw))

    out, err = _missing_module_log("zzfake_numpy")
    got = F.classify(2, False, out, err, command="python -m pytest tests/test_learner.py",
                     root=repo, interpreter="/opt/tool/.venv/bin/python")
    assert got["class"] == "environment" and got["module"] == "zzfake_numpy"
    # W20's {"interpreter", "version"} items fill available_in: the repo's
    # .venv has it, read statically (untrusted), never run.
    assert str(venv_python) in got["available_in"]
    assert str(venv_python) not in ran
    assert got["declared_in"] == [{"file": "requirements-ml.txt", "optional": True,
                                   "group": got["declared_in"][0]["group"]}]
    assert "declared in requirements-ml.txt (optional)" in got["detail"]
    assert f"available in {venv_python}" in got["detail"]
    # The daemon classifies every captured pytest: the answer is memoized.
    F.classify(2, False, out, err, command="python -m pytest", root=repo)
    assert len(discovered) == 1
    # A module no manifest declares and no interpreter has.
    out2, err2 = _missing_module_log("zz_nowhere_w23")
    got = F.classify(2, False, out2, err2, command="python -m pytest", root=repo)
    assert got["declared_in"] == [] and got["available_in"] == []
    assert got["detail"].endswith("declared in no manifest")
    # Trusted by the user: the project venv is run (exec probe).
    EN.clear_memo()
    monkeypatch.setenv(EN.TRUST_ENV, "1")
    doc = EN.module_environment(repo, "zzfake_numpy.sub", cache_mode="off")
    assert doc["trusted"] is True and doc["module"] == "zzfake_numpy"
    assert {"interpreter": str(venv_python), "version": doc["available_in"][0]["version"],
            "probe": "exec"} in doc["available_in"]
    assert str(venv_python) in ran


# --------------------------------------------------------------------------- #
# W23 fix round: lock tools, the project_roots cache, module_environment memo
# --------------------------------------------------------------------------- #


def _hostile_lock_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A git repo with pyproject.toml, pdm.lock, poetry.lock, no `.venv`, and
    a committed `.pdm-python` naming `tools/python`: a script that logs its
    arguments to a marker outside the repo and execs a real Python. Fake
    `pdm` and `poetry` binaries log their calls and do what pdm does
    without a venv (pdm: create `.venv/` and `.pdm-python` in the cwd).
    Returns (repo, markers dir, fake bin dir)."""
    markers = tmp_path / "markers"
    markers.mkdir()
    root = tmp_path / "hostile"
    root.mkdir()
    _write(root, "pyproject.toml", "[project]\nname = \"hostile\"\nversion = \"0\"\n")
    _write(root, "pdm.lock", "")
    _write(root, "poetry.lock", "")
    _write(root, ".pdm-python", "tools/python\n")
    (root / "tools").mkdir()
    _script(root / "tools" / "python",
            f'echo "$@" >> {markers}/pdm_python_ran\nexec {sys.executable} "$@"\n')
    _write(root, "tests/test_learner.py", "import zzhostile_w23\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "hostile")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _script(fake_bin / "pdm",
            f'echo "$@" >> {markers}/pdm_ran\n'
            'mkdir -p .venv\necho /usr/bin/python3 > .pdm-python\n'
            f'echo {sys.executable}\n')
    _script(fake_bin / "poetry",
            f'echo "$@" >> {markers}/poetry_ran\necho {tmp_path}/no-poetry-env\n')
    return root, markers, fake_bin


def _repo_status(root: Path) -> str:
    return _git(root, "status", "--porcelain", "--ignored")


def test_lock_tools_never_run_untrusted_and_write_nothing(
        tmp_path: Path, monkeypatch, no_user_config: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    from ozagia.core import failures as F
    from ozagia.core import repo_review as RR
    from ozagia.core.memory import DevMemory, build_index
    from ozagia.eval import agent as ae
    from ozagia.integrations.mcp.server import DevMemoryServer
    from ozagia.intent.run import failure_lines
    root, markers, fake_bin = _hostile_lock_repo(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv(EN.TRUST_ENV, raising=False)
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "state"))
    assert EN.trust_project_python(root) is False
    # `.pdm-python` is read as text: its target is a candidate, judged
    # project-shipped, and pdm is not asked.
    cands = EN.discover(root, include_self=False)
    lock = [c for c in cands if c["source"] == "lock"]
    assert lock == [{"path": str(root / "tools" / "python"), "source": "lock",
                     "evidence": "pdm.lock: .pdm-python (read as text)",
                     "also": [], "project": True}]
    out, err = _missing_module_log("zzhostile_w23")
    # Every surface, untrusted.
    assert cli.main(["env", "--root", str(root)]) == 0
    assert cli.main(["env", "--root", str(root), "--for", "tests/test_learner.py"]) == 1
    capsys.readouterr()
    EN.env_for(root, ["tests/test_learner.py"])
    EN.who_has(root, "zzhostile_w23")
    EN.module_environment(root, "zzhostile_w23")
    packet = RR.review_repo(root, sections=["dependencies"])
    data = packet["sections"]["dependencies"]["data"]
    assert {i["probe"] for i in data["interpreters"] if i["project"]} \
        <= {"static", "failed"}
    EN.clear_memo()
    lines = failure_lines(root, {"exit_code": 2, "output_tail": out + err,
                                 "command": "python -m pytest"},
                          sys.executable, [])
    assert lines and lines[0].startswith("failure class environment")
    F.classify(2, False, out, err, command="python -m pytest", root=root)
    graph = tmp_path / "g" / "graph.json"
    build_index(root, graph)
    server = DevMemoryServer(DevMemory(graph, tmp_path / "g" / "journal.jsonl",
                                       root=root))
    for arguments in ({}, {"module": "zzhostile_w23"},
                      {"paths": ["tests/test_learner.py"]}):
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "dev_environment",
                                         "arguments": arguments}})
        assert resp["result"]["isError"] is False, resp
    # `report --repo`: an old record classified from its check log.
    log_dir = tmp_path / "logs" / "t-baseline-r1"
    log_dir.mkdir(parents=True)
    (log_dir / "check.log").write_text(
        f"$ {sys.executable} -m pytest tests/test_learner.py\n\n{out}\n"
        f"--- stderr ---\n{err}\n", encoding="utf-8")
    EN.clear_memo()
    got = ae.reclassify_run({"task": "t", "arm": "baseline", "repeat": 1,
                             "check_returncode": 2}, log_dir, repo=root)
    assert got["failure_class"] == "environment"
    assert sorted(p.name for p in markers.iterdir()) == []
    assert _repo_status(root) == ""
    # The eval's check interpreter choice runs project interpreters by
    # design (trust=True: it runs the repository's tests under the choice),
    # but in `read` mode no lock tool runs and nothing is written.
    EN.clear_memo()
    task = {"id": "t", "check": {"argv": ["-m", "pytest", "tests/test_learner.py"],
                                 "timeout_s": 5}}
    ae.default_check_python(root, [task])
    assert not (markers / "pdm_ran").exists() and not (markers / "poetry_ran").exists()
    assert _repo_status(root) == ""


def test_lock_tools_run_when_trusted_with_the_fake_binaries(
        tmp_path: Path, monkeypatch, no_user_config: Path) -> None:
    root, markers, fake_bin = _hostile_lock_repo(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv(EN.TRUST_ENV, "1")
    # Trusted: poetry is asked; pdm is not, `.pdm-python` already answers.
    EN.discover(root, include_self=False)
    assert (markers / "poetry_ran").read_text().split() == ["env", "info", "--path"]
    assert not (markers / "pdm_ran").exists()
    assert not (markers / "pdm_python_ran").exists()
    # Without `.pdm-python`, pdm is asked (it then writes the repo, as pdm
    # does: allowed only for a trusted project).
    (root / ".pdm-python").unlink()
    EN.discover(root, include_self=False)
    assert (markers / "pdm_ran").read_text().split() == ["info", "--python"]
    # Trusted but `read` mode: no tool runs.
    for name in ("pdm_ran", "poetry_ran"):
        (markers / name).unlink()
    (root / ".pdm-python").unlink()
    (root / ".venv").rmdir()
    EN.discover(root, include_self=False, cache_mode="read")
    assert sorted(p.name for p in markers.iterdir()) == []


def test_pdm_python_is_read_as_text(tmp_path: Path, fake_venv: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert EN.pdm_python(root) is None
    (root / ".pdm-python").write_text(f"{fake_venv}\nignored\n", encoding="utf-8")
    assert EN.pdm_python(root) == fake_venv
    (root / ".pdm-python").write_text("no/such/python\n", encoding="utf-8")
    assert EN.pdm_python(root) is None
    (root / ".pdm-python").write_text("\n", encoding="utf-8")
    assert EN.pdm_python(root) is None


def test_project_roots_is_cached_per_root(tmp_path: Path, monkeypatch,
                                          no_user_config: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "a.py", "")
    _git(root, "init", "-q")
    calls: list[Path] = []
    real = EN.source_root
    monkeypatch.setattr(EN, "source_root",
                        lambda r: calls.append(Path(r)) or real(r))
    assert EN.project_roots(root) == [root.resolve()]
    assert EN.project_roots(root) == [root.resolve()]
    assert len(calls) == 1
    # A user config is present: the trust lookup on every memo hit uses
    # the cache too.
    _write(no_user_config, "ozagia/config.toml", "[environments]\n")
    for _ in range(3):
        assert EN.trust_project_python(root) is False
    assert len(calls) == 1
    # `.git` changed: recomputed.
    st = (root / ".git").stat()
    os.utime(root / ".git", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    EN.project_roots(root)
    assert len(calls) == 2


def test_module_environment_memo_follows_site_packages_and_manifests(
        tmp_path: Path, monkeypatch, no_user_config: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "tests/test_a.py", "import zzmemo_w23\n")
    _write(repo, "requirements.txt", "requests\n")
    _write(repo, ".gitignore", ".venv/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    venv_python = make_venv(repo / ".venv", {"other": ["zzother_w23"]})
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv(EN.TRUST_ENV, raising=False)
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(EN, "_lock_candidates", lambda *a, **k: [])
    first = EN.module_environment(repo, "zzmemo_w23")
    assert first["cached"] is False and first["available_in"] == []
    assert first["declared_in"] == []
    assert EN.module_environment(repo, "zzmemo_w23")["cached"] is True
    # `pip install` into the venv: its site-packages mtime changes.
    site = next((repo / ".venv" / "lib").glob("python*/site-packages"))
    _fake_dist(site, "zzmemo-w23", ["zzmemo_w23"])
    got = EN.module_environment(repo, "zzmemo_w23")
    assert got["cached"] is False
    assert [a["interpreter"] for a in got["available_in"]] == [str(venv_python)]
    assert EN.module_environment(repo, "zzmemo_w23")["cached"] is True
    # A manifest edit (same size or not): recomputed.
    req = repo / "requirements.txt"
    req.write_text("requests\nzzmemo-w23\n", encoding="utf-8")
    st = req.stat()
    os.utime(req, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    got = EN.module_environment(repo, "zzmemo_w23")
    assert got["cached"] is False
    assert got["declared_in"] == [{"file": "requirements.txt", "optional": False,
                                   "group": got["declared_in"][0]["group"]}]
    # The TTL still bounds it.
    monkeypatch.setattr(EN, "MODULE_ENV_TTL_S", 0.0)
    assert EN.module_environment(repo, "zzmemo_w23")["cached"] is False
