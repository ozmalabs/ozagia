"""Lint inside the edit (`ozagia.intent.lint`) and the `ruff check` capture,
with the real ruff (the dev extra installs it) and rustfmt on temp repos."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.config import ConfigError, clear_config_cache, load_project_config
from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.intent import lint as L
from ozagia.intent.run import handle_shell
from ozagia.intent.shell import classify

VENV_RUFF = Path(sys.executable).parent / "ruff"
RUFF = str(VENV_RUFF) if VENV_RUFF.is_file() else shutil.which("ruff")
RUSTFMT = shutil.which("rustfmt")
needs_ruff = pytest.mark.skipif(RUFF is None, reason="ruff is not installed")
needs_rustfmt = pytest.mark.skipif(RUSTFMT is None, reason="rustfmt is not installed")

PYPROJECT = '[project]\nname = "t"\n\n[tool.ruff.lint]\nselect = ["F"]\n'
MOD = '''import os


def kept():
    return 1


def target(a):
    return a
'''


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(root), "PATH": "/usr/bin:/bin"})


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(["pkg", "tests"]))
    for var in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    clear_config_cache()
    yield
    clear_config_cache()


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, text in files.items():
        _write(root / rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _mem(root: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(root, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)


@pytest.fixture
def repo(tmp_path: Path, layout) -> Path:
    return _repo(tmp_path, {"pyproject.toml": PYPROJECT, "pkg/__init__.py": "",
                            "pkg/mod.py": MOD})


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    return _mem(repo, tmp_path)


def _venv_ruff(root: Path) -> None:
    """`<root>/.venv/bin/ruff` -> the real ruff."""
    (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    os.symlink(RUFF, root / ".venv" / "bin" / "ruff")


def _lint_lines(output: str) -> list[str]:
    return [ln for ln in output.splitlines() if ln.startswith("[ozagia] lint")]


def _edit(mem, repo, old, new, **extra):
    res = I.handle(mem, "edit", {"filePath": str(repo / "pkg" / "mod.py"),
                                 "oldString": old, "newString": new, **extra})
    assert res["handled"] is True and res["is_error"] is False, res
    return res


# --------------------------------------------------------------------------- #
# Handled edits
# --------------------------------------------------------------------------- #
@needs_ruff
def test_edit_reports_findings_on_changed_lines_only(mem, repo):
    _venv_ruff(repo)
    res = _edit(mem, repo, "    return a\n", "    return a + undefined_b\n")
    lines = _lint_lines(res["output"])
    # F401 (`import os`, line 1) is not on a changed line.
    assert lines == ["[ozagia] lint F821 L9 sym:pkg/mod.py::target: "
                     "Undefined name `undefined_b`"]
    assert res["meta"]["lint_runs"] == 1
    assert res["meta"]["lint_findings"] == 1
    assert res["meta"]["lint_timeouts"] == 0
    # The linter changed nothing and wrote no cache into the repository.
    assert (repo / "pkg" / "mod.py").read_text() == MOD.replace(
        "    return a\n", "    return a + undefined_b\n")
    assert not (repo / ".ruff_cache").exists()


@needs_ruff
def test_ruff_on_path_and_clean_edit(mem, repo, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(RUFF).parent}:/usr/bin:/bin")
    res = _edit(mem, repo, "    return a\n", "    return a * 2\n")
    assert _lint_lines(res["output"]) == []
    assert res["meta"]["lint_runs"] == 1
    assert res["meta"]["lint_findings"] == 0


@needs_ruff
def test_edit_caps_lint_lines(mem, repo):
    _venv_ruff(repo)
    body = "".join(f"    u{i}\n" for i in range(15))
    res = _edit(mem, repo, "    return a\n", body + "    return a\n")
    lines = _lint_lines(res["output"])
    assert len(lines) == L.MAX_LINES + 1
    assert all(" F821 " in ln for ln in lines[:L.MAX_LINES])
    assert lines[-1] == "[ozagia] lint: 5 more findings on the changed lines"
    assert res["meta"]["lint_findings"] == 15


@needs_ruff
def test_preexisting_findings_on_unchanged_lines_are_not_reported(mem, repo):
    _venv_ruff(repo)
    # A finding already in `kept`; the edit is elsewhere.
    path = repo / "pkg" / "mod.py"
    path.write_text(MOD.replace("return 1", "return old_undefined"))
    mem.reingest(["pkg/mod.py"])
    res = _edit(mem, repo, "def target(a):\n", "def target(a, b=2):\n")
    assert _lint_lines(res["output"]) == []
    assert res["meta"]["lint_runs"] == 1


def test_lint_timeout_reports_and_keeps_the_edit(mem, repo):
    (repo / ".venv" / "bin").mkdir(parents=True)
    slow = repo / ".venv" / "bin" / "ruff"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    _write(repo / ".ozagia.toml", "[lint]\ntimeout_s = 0.3\n")
    import time
    t0 = time.monotonic()
    res = _edit(mem, repo, "    return a\n", "    return a + 1\n")
    assert time.monotonic() - t0 < 3
    assert _lint_lines(res["output"]) == ["[ozagia] lint: ruff timed out after 0.3 s"]
    assert res["meta"]["lint_timeouts"] == 1
    assert "return a + 1" in (repo / "pkg" / "mod.py").read_text()


@needs_ruff
def test_config_off_and_missing_linter_are_silent(mem, repo, monkeypatch):
    _venv_ruff(repo)
    _write(repo / ".ozagia.toml", "[lint]\nenabled = false\n")
    res = _edit(mem, repo, "    return a\n", "    return a + nope\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res["meta"]
    _write(repo / ".ozagia.toml", '[lint]\npython = "off"\n')
    res = _edit(mem, repo, "    return a + nope\n", "    return a + nope2\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res["meta"]
    # No ruff anywhere: silent.
    (repo / ".ozagia.toml").unlink()
    (repo / ".venv" / "bin" / "ruff").unlink()
    monkeypatch.setattr(L.shutil, "which", lambda *_a, **_k: None)
    res = _edit(mem, repo, "    return a + nope2\n", "    return a + nope3\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res["meta"]


@needs_ruff
def test_no_ruff_configuration_means_no_lint_unless_named(tmp_path, layout):
    root = _repo(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": MOD})
    _venv_ruff(root)
    mem = _mem(root, tmp_path)
    res = _edit(mem, root, "    return a\n", "    return a + nope\n")
    assert _lint_lines(res["output"]) == []
    _write(root / ".ozagia.toml", '[lint]\npython = "ruff"\n')
    res = _edit(mem, root, "    return a + nope\n", "    return a + nope2\n")
    assert any("F821" in ln and "nope2" in ln for ln in _lint_lines(res["output"]))


@needs_ruff
def test_ruff_toml_counts_as_configuration(tmp_path, layout):
    root = _repo(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": MOD,
                            "pkg/ruff.toml": '[lint]\nselect = ["F"]\n'})
    assert L.ruff_config(root, "pkg/mod.py") == root / "pkg" / "ruff.toml"
    assert L.ruff_config(root, "other.py") is None


@needs_ruff
def test_write_and_key_edit_are_linted(mem, repo):
    _venv_ruff(repo)
    res = I.handle(mem, "write", {"filePath": str(repo / "pkg" / "new.py"),
                                  "content": "import sys\n\n\ndef f():\n    return zz\n"})
    assert res["handled"] and not res["is_error"], res
    rules = [ln.split()[2] for ln in _lint_lines(res["output"])]
    assert rules == ["F401", "F821"]          # a new file: every line
    # Overwrite: only the changed lines.
    res = I.handle(mem, "write", {"filePath": str(repo / "pkg" / "new.py"),
                                  "content": "import sys\n\n\ndef f():\n    return 1\n"})
    assert _lint_lines(res["output"]) == []
    node = mem.node(mem.node_by_key("sym:pkg/mod.py::target"))
    sha = __import__("hashlib").sha256(
        "def target(a):\n    return a\n".encode()).hexdigest()
    res = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::target",
                                 "newString": "def target(a):\n    return qq\n",
                                 "base_sha256": sha})
    assert node is not None
    assert res["handled"] and not res["is_error"], res["output"]
    assert [ln.split()[2] for ln in _lint_lines(res["output"])] == ["F821"]


# --------------------------------------------------------------------------- #
# Built-in edits, writes and patches: `after`
# --------------------------------------------------------------------------- #
@needs_ruff
def test_after_builtin_edit_lints_the_new_text(mem, repo):
    _venv_ruff(repo)
    path = repo / "pkg" / "mod.py"
    path.write_text(MOD.replace("    return a\n", "    return a + gone\n"))
    res = I.after(mem, "edit", {"filePath": str(path), "oldString": "    return a\n",
                                "newString": "    return a + gone\n"},
                  "Edit applied successfully.")
    assert _lint_lines(res["append"]) == [
        "[ozagia] lint F821 L9 sym:pkg/mod.py::target: Undefined name `gone`"]
    assert res["meta"]["lint_runs"] == 1 and res["meta"]["lint_findings"] == 1


@needs_ruff
def test_after_builtin_write_lints_the_file(mem, repo):
    _venv_ruff(repo)
    path = repo / "pkg" / "w.py"
    path.write_text("def g():\n    return missing\n")
    res = I.after(mem, "write", {"filePath": str(path), "content": path.read_text()},
                  "Wrote file successfully.")
    assert [ln.split()[2] for ln in _lint_lines(res["append"])] == ["F821"]


@needs_ruff
def test_after_codex_patch_lints_added_lines(mem, repo):
    _venv_ruff(repo)
    path = repo / "pkg" / "mod.py"
    path.write_text(MOD.replace("    return a\n", "    return a + via_patch\n"))
    patch = ("*** Begin Patch\n*** Update File: pkg/mod.py\n@@ def target(a):\n"
             "-    return a\n+    return a + via_patch\n*** End Patch\n")
    res = I.after(mem, "patch", {"patch": patch},
                  "Success. Updated the following files:\nM pkg/mod.py\n",
                  harness="codex")
    assert _lint_lines(res["append"]) == [
        "[ozagia] lint F821 L9 sym:pkg/mod.py::target: Undefined name `via_patch`"]
    # An added file: every line.
    _write(repo / "pkg" / "added.py", "import json\n")
    patch = ("*** Begin Patch\n*** Add File: pkg/added.py\n+import json\n"
             "*** End Patch\n")
    res = I.after(mem, "patch", {"patch": patch}, "Success.", harness="codex")
    assert [ln.split()[2] for ln in _lint_lines(res["append"])] == ["F401"]


# --------------------------------------------------------------------------- #
# Changed lines and output parsing
# --------------------------------------------------------------------------- #
def test_changed_lines():
    assert L.changed_lines(None, "a\nb\n") is None
    assert L.changed_lines("a\nb\nc\n", "a\nB\nc\n") == {2}
    assert L.changed_lines("a\nb\nc\n", "a\nb\nx\ny\nc\n") == {3, 4}
    assert L.changed_lines("a\nb\nc\n", "a\nc\n") == {1, 2}   # around a deletion
    assert L.changed_lines("a\n", "a\n") == set()


def test_rustfmt_hunks_both_header_forms(tmp_path):
    out = ("Diff in /r/src/lib.rs:1:\n-fn main(){\n-let x=1;\n+fn main() {\n"
           "+    let x = 1;\n     println!();\n }\n \n"
           "Diff in /r/src/lib.rs at line 6:\n fn ok() {}\n \n-fn g( ){ }\n"
           "+fn g() {}\n \n"
           "Diff in /r/src/lib.rs:20:\n a\n+inserted\n b\n")
    root = Path("/r")
    got = L.rustfmt_hunks(out, root, root)
    assert [(h["start"], h["end"]) for h in got] == [(1, 2), (8, 8), (21, 21)]
    assert {h["rel"] for h in got} == {"src/lib.rs"}


def test_lint_config_validation(tmp_path, layout):
    root = tmp_path / "r"
    root.mkdir()
    cfg = load_project_config(root)
    assert (cfg.lint_enabled, cfg.lint_timeout_s, cfg.lint_python,
            cfg.lint_rust) == (None, 3.0, None, None)
    for bad in ("enabled = 1", "timeout_s = 0", 'python = "flake8"',
                'rust = "clippy"', "other = 1"):
        (root / ".ozagia.toml").write_text(f"[lint]\n{bad}\n")
        clear_config_cache()
        with pytest.raises(ConfigError):
            load_project_config(root)
    (root / ".ozagia.toml").write_text('[lint]\nenabled = true\ntimeout_s = 1.5\n'
                                       'python = "ruff"\nrust = "off"\n')
    clear_config_cache()
    cfg = load_project_config(root)
    assert (cfg.lint_enabled, cfg.lint_timeout_s, cfg.lint_python,
            cfg.lint_rust) == (True, 1.5, "ruff", "off")


def test_cargo_edition(tmp_path):
    root = tmp_path / "r"
    _write(root / "Cargo.toml", '[workspace]\nmembers = ["a", "b"]\n'
           '[workspace.package]\nedition = "2021"\n')
    _write(root / "a" / "Cargo.toml", '[package]\nname = "a"\nedition = "2018"\n')
    _write(root / "b" / "Cargo.toml", '[package]\nname = "b"\nedition.workspace = true\n')
    _write(root / "c" / "Cargo.toml", '[package]\nname = "c"\n')
    assert L.cargo_edition(root, "a/src/lib.rs") == "2018"
    assert L.cargo_edition(root, "b/src/lib.rs") == "2021"
    assert L.cargo_edition(root, "c/src/lib.rs") == "2015"
    assert L.cargo_edition(tmp_path / "none", "x.rs") is None


# --------------------------------------------------------------------------- #
# rustfmt
# --------------------------------------------------------------------------- #
LIB_RS = """pub fn kept( ){ }

pub fn target() -> i32 {
    1
}
"""


@needs_rustfmt
def test_rustfmt_reports_changed_hunks_only(tmp_path, layout):
    root = _repo(tmp_path, {"Cargo.toml": '[package]\nname = "t"\nversion = "0.1.0"\n'
                            'edition = "2021"\n', "src/lib.rs": LIB_RS})
    mem = _mem(root, tmp_path)
    path = root / "src" / "lib.rs"
    res = I.handle(mem, "edit", {"filePath": str(path), "oldString": "    1\n",
                                 "newString": "    let x=1;x\n"})
    assert res["handled"] and not res["is_error"], res
    lines = _lint_lines(res["output"])
    assert len(lines) == 1, res["output"]
    assert lines[0].startswith("[ozagia] lint rustfmt L4 ")
    assert lines[0].endswith("rustfmt would reformat L4-4")
    assert path.read_text() == LIB_RS.replace("    1\n", "    let x=1;x\n")
    assert res["meta"]["lint_runs"] == 1


# --------------------------------------------------------------------------- #
# The `ruff check` capture
# --------------------------------------------------------------------------- #
CLASSIFY_RUFF = [
    ("ruff check", "ruff"),
    ("ruff check src tests", "ruff"),
    (".venv/bin/ruff check src --select F401", "ruff"),
    ("ruff check --select=E,F --output-format concise pkg", "ruff"),
    ("ruff check --output-format=full -- pkg", "ruff"),
    ("ruff check --fix", "other"),
    ("ruff check --unsafe-fixes src", "other"),
    ("ruff check --watch", "other"),
    ("ruff check --output-format json", "other"),
    ("ruff check --select", "other"),
    ("ruff format", "other"),
    ("ruff format --check src", "other"),
    ("ruff", "other"),
]


@pytest.mark.parametrize("command,cls", CLASSIFY_RUFF)
def test_classify_ruff(command, cls):
    assert classify(command)["class"] == cls


def _real(root: Path, argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(root), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


@needs_ruff
@pytest.mark.parametrize("args", [
    ["check", "pkg"],
    ["check", "pkg", "--select", "F401", "--output-format", "concise"],
    ["check", "--output-format=full", "pkg/mod.py"],
])
def test_ruff_capture_is_real_ruff_plus_key_lines(mem, repo, args):
    _venv_ruff(repo)
    (repo / "pkg" / "mod.py").write_text(MOD.replace("return a", "return a + zz"))
    before = (repo / "pkg" / "mod.py").read_text()
    command = " ".join([".venv/bin/ruff", *args])
    res = handle_shell(mem, {"command": command, "workdir": str(repo)})
    real = _real(repo, [str(repo / ".venv" / "bin" / "ruff"), *args])
    text = real.stdout.decode().rstrip("\n")
    assert res["intent"] == "shell_ruff"
    assert res["exit_code"] == real.returncode == 1
    assert res["output"].startswith(text + "\n[ozagia] ")
    tail = res["output"][len(text) + 1:].splitlines()
    want = ["[ozagia] lint F401 pkg/mod.py:1 file:pkg/mod.py"]
    if "F401" not in args:
        want.append("[ozagia] lint F821 pkg/mod.py:9 sym:pkg/mod.py::target")
    assert tail == [*want, "[ozagia] exit 1"]
    assert "sym:pkg/mod.py::target" in res["keys"] or "F401" in args
    assert (repo / "pkg" / "mod.py").read_text() == before


@needs_ruff
def test_ruff_capture_on_path_clean_and_through_handle(mem, repo):
    env = {"PATH": f"{Path(RUFF).parent}:/usr/bin:/bin", "HOME": str(repo)}
    (repo / "pkg" / "mod.py").write_text("def kept():\n    return 1\n")
    res = I.handle(mem, "shell", {"command": "ruff check pkg", "workdir": str(repo),
                                  "_test_env": env})
    real = subprocess.run([RUFF, "check", "pkg"], cwd=str(repo), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert res["handled"] is True and res["meta"]["intent"] == "shell_ruff"
    assert real.returncode == 0
    assert res["output"] == real.stdout.decode().rstrip("\n")


@needs_ruff
def test_ruff_capture_refuses_fix_configuration(tmp_path, layout):
    root = _repo(tmp_path, {"pyproject.toml": PYPROJECT.replace(
        "[tool.ruff.lint]", "[tool.ruff]\nfix = true\n\n[tool.ruff.lint]"),
        "pkg/mod.py": MOD})
    _venv_ruff(root)
    mem = _mem(root, tmp_path)
    res = I.handle(mem, "shell", {"command": ".venv/bin/ruff check pkg",
                                  "workdir": str(root)})
    assert res["handled"] is False
    assert "fix = true" in res["reason"]
    assert (root / "pkg" / "mod.py").read_text() == MOD


def test_ruff_capture_missing_executable_is_passthrough(mem, repo):
    res = I.handle(mem, "shell", {"command": ".venv/bin/ruff check pkg",
                                  "workdir": str(repo)})
    assert res["handled"] is False


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #
def test_daemon_and_eval_count_lint(tmp_path):
    from ozagia.daemon.server import DaemonState
    from ozagia.eval import agent as ae
    st = DaemonState(tmp_path, object(), tmp_path / "j.jsonl")
    st.record({"op": "tool", "tool": "edit"},
              {"ok": True, "handled": True, "meta": {
                  "intent": "edit", "lint_runs": 1, "lint_findings": 2,
                  "lint_timeouts": 0}}, 1.0)
    st.record({"op": "after", "tool": "patch"},
              {"ok": True, "meta": {"intent": "after_refresh", "lint_runs": 2,
                                    "lint_findings": 1, "lint_timeouts": 1}}, 1.0)
    st.record({"op": "tool", "tool": "shell"},
              {"ok": True, "handled": True, "meta": {"intent": "shell_ruff"}}, 1.0)
    stats = st.stats()
    assert {k: stats[k] for k in ae.LINT_FIELDS} == {
        "lint_runs": 3, "lint_findings": 3, "lint_timeouts": 1, "ruff_captures": 1}
    fields = ae.graph_stats_fields({"hellos": 1, **stats})
    assert {k: fields[k] for k in ae.LINT_FIELDS} == {
        "lint_runs": 3, "lint_findings": 3, "lint_timeouts": 1, "ruff_captures": 1}
    assert all(ae.graph_stats_fields(None)[k] is None for k in ae.LINT_FIELDS)
    rows = ae.aggregate([{"arm": "graph_intent", "daemon_ok": True, **fields},
                         {"arm": "graph_intent", "daemon_ok": True, "lint_runs": 1}],
                        ["graph_intent"])
    assert rows[0]["total_lint_runs"] == 4
    assert rows[0]["total_ruff_captures"] == 1
