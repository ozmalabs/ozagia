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
                        command_docs=[], include_self=False)
    assert asked == [["poetry", "env", "info", "--path"]]
    assert [(c["source"], c["path"]) for c in cands] == [
        ("lock", str(venv / "bin" / "python"))]
    # Without the tool installed it is not asked.
    assert EN.discover(root, which=lambda _n: None, tool=tool, command_docs=[],
                       include_self=False) == []
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
    assert out == [{"interpreter": "/e/a", "version": "3.13.0"},
                   {"interpreter": "/e/d", "version": "3.13.0"}]
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
