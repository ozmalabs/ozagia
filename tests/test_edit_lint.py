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


@pytest.fixture(autouse=True)
def lint_env(monkeypatch, tmp_path):
    """ruff and rustfmt from PATH (outside the temp repos); no opt-in to
    in-repo linters, and an empty user-level config dir."""
    dirs = [str(Path(RUFF).parent)] if RUFF else []
    if RUSTFMT:
        dirs.append(str(Path(RUSTFMT).parent))
    monkeypatch.setenv("PATH", ":".join([*dirs, "/usr/bin", "/bin"]))
    monkeypatch.delenv(L.TRUST_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


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
    body = "".join(f"    u{i}\n" for i in range(15))
    res = _edit(mem, repo, "    return a\n", body + "    return a\n")
    lines = _lint_lines(res["output"])
    assert len(lines) == L.MAX_LINES + 1
    assert all(" F821 " in ln for ln in lines[:L.MAX_LINES])
    assert lines[-1] == "[ozagia] lint: 5 more findings on the changed lines"
    assert res["meta"]["lint_findings"] == 15


@needs_ruff
def test_preexisting_findings_on_unchanged_lines_are_not_reported(mem, repo):
    # A finding already in `kept`; the edit is elsewhere.
    path = repo / "pkg" / "mod.py"
    path.write_text(MOD.replace("return 1", "return old_undefined"))
    mem.reingest(["pkg/mod.py"])
    res = _edit(mem, repo, "def target(a):\n", "def target(a, b=2):\n")
    assert _lint_lines(res["output"]) == []
    assert res["meta"]["lint_runs"] == 1


def _slow_ruff(tmp_path: Path, monkeypatch, body: str = "sleep 5") -> Path:
    """A `ruff` outside the repository, first on PATH, that runs `body`."""
    bin_dir = tmp_path / "slowbin"
    bin_dir.mkdir()
    slow = bin_dir / "ruff"
    slow.write_text(f"#!/bin/sh\n{body}\n")
    slow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return slow


def test_lint_timeout_reports_and_keeps_the_edit(mem, repo, tmp_path, monkeypatch):
    _slow_ruff(tmp_path, monkeypatch)
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
    _write(repo / ".ozagia.toml", "[lint]\nenabled = false\n")
    res = _edit(mem, repo, "    return a\n", "    return a + nope\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res["meta"]
    _write(repo / ".ozagia.toml", '[lint]\npython = "off"\n')
    res = _edit(mem, repo, "    return a + nope\n", "    return a + nope2\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res["meta"]
    # No ruff anywhere: silent.
    (repo / ".ozagia.toml").unlink()
    monkeypatch.setenv("PATH", "/nonexistent")
    res = _edit(mem, repo, "    return a + nope2\n", "    return a + nope3\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res["meta"]


@needs_ruff
def test_no_ruff_configuration_means_no_lint_unless_named(tmp_path, layout):
    root = _repo(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": MOD})
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
    path = repo / "pkg" / "w.py"
    path.write_text("def g():\n    return missing\n")
    res = I.after(mem, "write", {"filePath": str(path), "content": path.read_text()},
                  "Wrote file successfully.")
    assert [ln.split()[2] for ln in _lint_lines(res["append"])] == ["F821"]


@needs_ruff
def test_after_codex_patch_lints_added_lines(mem, repo):
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


# --------------------------------------------------------------------------- #
# Trust: no program from inside the repository without a user-level opt-in
# --------------------------------------------------------------------------- #
def _marker_ruff(path: Path, marker: Path) -> None:
    """An executable `ruff` at `path` that records that it ran."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch {marker}\necho '[]'\n")
    path.chmod(0o755)


def test_repo_venv_ruff_is_not_run_by_default(mem, repo, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    _marker_ruff(repo / ".venv" / "bin" / "ruff", marker)
    # Not even when PATH names the repository's own bin dir.
    monkeypatch.setenv("PATH", f"{repo / '.venv' / 'bin'}:/usr/bin:/bin")
    res = _edit(mem, repo, "    return a\n", "    return a + 1\n")
    assert not marker.exists()
    assert "lint_runs" not in res.get("meta", {})
    assert L.ruff_executable(repo) is None


def test_path_symlink_into_the_repo_is_refused(repo, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    _marker_ruff(repo / "tools" / "ruff", marker)
    outside = tmp_path / "bin"
    outside.mkdir()
    os.symlink(repo / "tools" / "ruff", outside / "ruff")
    monkeypatch.setenv("PATH", f"{outside}:/usr/bin:/bin")
    assert L.find_executable(repo, "ruff", trusted=False) is None
    assert L.find_executable(repo, "ruff", trusted=True) == str(outside / "ruff")
    # A relative PATH entry (resolved against some cwd) is never used.
    monkeypatch.setenv("PATH", "tools")
    assert L.find_executable(repo, "ruff", trusted=True) is None


def test_trust_env_opts_in_to_the_repo_venv(mem, repo, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    _marker_ruff(repo / ".venv" / "bin" / "ruff", marker)
    monkeypatch.setenv(L.TRUST_ENV, "1")
    res = _edit(mem, repo, "    return a\n", "    return a + 1\n")
    assert marker.exists()
    assert res["meta"]["lint_runs"] == 1


def test_user_config_opts_in_but_not_one_inside_the_repo(mem, repo, tmp_path,
                                                        monkeypatch):
    marker = tmp_path / "ran"
    _marker_ruff(repo / ".venv" / "bin" / "ruff", marker)
    user = tmp_path / "xdg" / "ozagia" / "config.toml"
    _write(user, "[lint]\ntrust_project_venv = true\n")
    assert L.trust_project_venv(repo) is True
    assert L.ruff_executable(repo) == str(repo / ".venv" / "bin" / "ruff")
    # XDG_CONFIG_HOME pointing into the repository does not count.
    _write(repo / "xdg" / "ozagia" / "config.toml",
           "[lint]\ntrust_project_venv = true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(repo / "xdg"))
    assert L.trust_project_venv(repo) is False
    # Nor can the repository's .ozagia.toml enable it: the key is invalid
    # there, and a config that does not load turns lint off.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    _write(repo / ".ozagia.toml", "[lint]\ntrust_project_venv = true\n")
    clear_config_cache()
    res = _edit(mem, repo, "    return a\n", "    return a + 1\n")
    assert not marker.exists()
    assert "lint_runs" not in res.get("meta", {})


# --------------------------------------------------------------------------- #
# Environment, config, deadline and lock
# --------------------------------------------------------------------------- #
def test_linter_gets_the_sanitized_environment(mem, repo, tmp_path, monkeypatch):
    dump = tmp_path / "env.txt"
    _slow_ruff(tmp_path, monkeypatch, f"env > {dump}\necho '[]'")
    monkeypatch.setenv("OZAGIA_SECRET_THING", "x")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    _edit(mem, repo, "    return a\n", "    return a + 1\n")
    env = dump.read_text()
    assert "OZAGIA_SECRET_THING" not in env and "PYTHONPATH=" not in env
    # The caller's `_test_env` replaces it.
    _edit(mem, repo, "    return a + 1\n", "    return a + 2\n",
          _test_env={"PATH": "/usr/bin:/bin", "ONLY_THIS": "1"})
    env = dump.read_text()
    assert "ONLY_THIS=1" in env and "XDG_CONFIG_HOME" not in env


@needs_ruff
def test_broken_config_turns_lint_off(mem, repo):
    _write(repo / ".ozagia.toml", "[lint]\nenabled = false\nbogus = 1\n")
    res = _edit(mem, repo, "    return a\n", "    return a + nope\n")
    assert _lint_lines(res["output"]) == [] and "lint_runs" not in res.get("meta", {})
    _write(repo / ".ozagia.toml", "[lint]\ntimeout_s = -1\n")
    clear_config_cache()
    assert L.Plan(repo).linter("pkg/mod.py") is None


def test_budget_is_capped_and_follows_the_deadline(repo, monkeypatch):
    import time
    _write(repo / ".ozagia.toml", "[lint]\ntimeout_s = 600\n")
    clear_config_cache()
    assert L.Plan(repo).timeout_s == L.MAX_TIMEOUT_S
    now = time.monotonic()
    assert abs(L.lint_budget(3.0, {"_reply_deadline": now + 2.0}) - 1.0) < 0.1
    assert L.lint_budget(3.0, {"_deadline_s_remaining": 1.5}) == 0.5
    assert L.lint_budget(30.0, None) == L.MAX_TIMEOUT_S


def test_lint_stops_before_the_reply_deadline(mem, repo, tmp_path, monkeypatch):
    import time
    _slow_ruff(tmp_path, monkeypatch)
    t0 = time.monotonic()
    res = _edit(mem, repo, "    return a\n", "    return a + 1\n",
                _reply_deadline=time.monotonic() + 1.6)
    assert time.monotonic() - t0 < 1.6
    assert _lint_lines(res["output"]) == [
        "[ozagia] lint: ruff timed out after 0.6 s"]
    # No time left: no linter starts.
    res = _edit(mem, repo, "    return a + 1\n", "    return a + 2\n",
                _reply_deadline=time.monotonic() + 0.5)
    assert _lint_lines(res["output"]) == [
        "[ozagia] lint: not run; the request deadline is too near"]
    assert res["meta"]["lint_timeouts"] == 1 and res["meta"]["lint_runs"] == 0


def test_daemon_releases_the_graph_lock_while_linting(mem, repo, tmp_path,
                                                      monkeypatch):
    import threading
    import time

    from ozagia.daemon.server import DaemonState
    started = tmp_path / "started"
    _slow_ruff(tmp_path, monkeypatch, f"touch {started}\nsleep 1\necho '[]'")
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    out: dict = {}

    def edit() -> None:
        out["resp"] = st.dispatch({"op": "tool", "tool": "edit", "input": {
            "filePath": str(repo / "pkg" / "mod.py"),
            "oldString": "    return a\n", "newString": "    return a + 1\n"},
            "deadline_ms": int((time.time() + 15) * 1000)})

    th = threading.Thread(target=edit)
    th.start()
    for _ in range(200):
        if started.exists():
            break
        time.sleep(0.01)
    assert started.exists()
    got = st.lock.acquire(timeout=0.5)      # free while ruff runs
    assert got
    st.lock.release()
    th.join(5)
    assert out["resp"]["handled"] is True
    assert out["resp"]["meta"]["lint_runs"] == 1
    assert not st.lock.locked()


def test_ruff_capture_is_lock_free_and_locks_for_keys(mem, repo, monkeypatch):
    from ozagia.daemon.server import _lock_free
    assert _lock_free("tool", "shell", {"command": "ruff check pkg"})
    assert not _lock_free("tool", "edit", {"command": "ruff check pkg"})

    class Counting:
        n = 0

        def __enter__(self):
            Counting.n += 1

        def __exit__(self, *a):
            return False

    if RUFF is None:
        pytest.skip("ruff is not installed")
    (repo / "pkg" / "mod.py").write_text(MOD.replace("return a", "return a + zz"))
    res = handle_shell(mem, {"command": "ruff check pkg", "workdir": str(repo),
                             "_lock": Counting()})
    assert res["intent"] == "shell_ruff" and res["exit_code"] == 1
    assert Counting.n == 1


# --------------------------------------------------------------------------- #
# Changed lines in `after`, multi-line findings, rustfmt on a crate root
# --------------------------------------------------------------------------- #
THREE = '''def a():
    return zz


def b():
    return zz


def c():
    return 1
'''


@needs_ruff
def test_after_repeated_new_string_reports_only_the_edited_site(tmp_path, layout):
    root = _repo(tmp_path, {"pyproject.toml": PYPROJECT, "pkg/__init__.py": "",
                            "pkg/three.py": THREE})
    mem = _mem(root, tmp_path)
    path = root / "pkg" / "three.py"
    path.write_text(THREE.replace("    return 1\n", "    return zz\n"))
    res = I.after(mem, "edit", {"filePath": str(path), "oldString": "    return 1\n",
                                "newString": "    return zz\n"},
                  "Edit applied successfully.")
    assert _lint_lines(res["append"]) == [
        "[ozagia] lint F821 L10 sym:pkg/three.py::c: Undefined name `zz`"]


def test_occurrences_unique_and_replace_all(tmp_path, layout):
    text = "x = 1\ny = 2\nx = 1\n"
    assert L._occurrences(text, "x = 1\n", 2) == [{1}, {3}]
    assert L._occurrences(text, "y = 2\n", 2) == [{2}]


@needs_ruff
def test_multiline_finding_counts_when_any_line_changed(tmp_path, layout):
    root = _repo(tmp_path, {"pyproject.toml": PYPROJECT.replace('["F"]', '["I"]'),
                            "pkg/__init__.py": "",
                            "pkg/imp.py": "import abc\nimport os\n\nprint(abc, os)\n"})
    mem = _mem(root, tmp_path)
    res = I.handle(mem, "edit", {"filePath": str(root / "pkg" / "imp.py"),
                                 "oldString": "import os\n",
                                 "newString": "import aaa\n"})
    assert res["handled"] and not res["is_error"], res
    # I001 starts on line 1 (unchanged) and spans the changed line 2.
    assert [ln.split()[2:4] for ln in _lint_lines(res["output"])] == [["I001", "L1"]]
    got = L._ruff_findings(root, '[{"filename": "%s", "code": "I001", "message": "m",'
                           ' "location": {"row": 1}, "end_location": {"row": 3}}]'
                           % (root / "pkg" / "imp.py"))
    assert got[0]["line"] == 1 and got[0]["end"] == 3


@needs_rustfmt
def test_rustfmt_checks_only_the_crate_root_not_child_modules(tmp_path, layout):
    root = _repo(tmp_path, {"Cargo.toml": '[package]\nname = "t"\nversion = "0.1.0"\n'
                            'edition = "2021"\n',
                            "src/lib.rs": "mod child;\n" + LIB_RS,
                            # Does not parse: checking it would fail the run.
                            "src/child.rs": "fn broken( {\n"})
    mem = _mem(root, tmp_path)
    path = root / "src" / "lib.rs"
    res = I.handle(mem, "edit", {"filePath": str(path), "oldString": "    1\n",
                                 "newString": "    let x=1;x\n"})
    assert res["handled"] and not res["is_error"], res
    lines = _lint_lines(res["output"])
    assert len(lines) == 1, res["output"]
    assert lines[0].startswith("[ozagia] lint rustfmt L5 ")
    assert lines[0].endswith("rustfmt would reformat L5-5")


def test_rustfmt_hunks_from_stdin():
    out = "Diff in <stdin>:3:\n a\n-b\n+B\n c\n"
    got = L.rustfmt_hunks(out, Path("/r"), Path("/r/src"), stdin_rel="src/x.rs")
    assert got == [{"rel": "src/x.rs", "start": 4, "end": 4}]


# --------------------------------------------------------------------------- #
# Process cleanup and the `fix` setting of a captured `ruff check`
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("setsid") is None, reason="no setsid")
def test_run_bounded_does_not_hang_on_an_escaped_descendant(tmp_path):
    import time
    script = tmp_path / "s.sh"
    script.write_text("#!/bin/sh\nsetsid sleep 4 &\nsleep 10\n")
    script.chmod(0o755)
    t0 = time.monotonic()
    rc, _out, timed_out = L.run_bounded([str(script)], cwd=tmp_path, env=None,
                                        timeout_s=0.2)
    assert timed_out and rc is None
    assert time.monotonic() - t0 < 3.5


def test_ruff_capture_kills_the_whole_group(mem, repo, tmp_path, monkeypatch):
    import time
    pidfile = tmp_path / "child.pid"
    shim = repo / "bin" / "ruff"
    shim.parent.mkdir()
    shim.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nwait\n")
    shim.chmod(0o755)
    res = handle_shell(mem, {"command": "bin/ruff check pkg", "workdir": str(repo),
                             "timeout": 300})
    assert res["exit_code"] == 124
    pid = int(pidfile.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(pid, 9)
        raise AssertionError("the shim's child outlived the capture")


def test_ruff_fix_through_extend_and_user_config(tmp_path, layout):
    from ozagia.intent.run import _ruff_fixes
    root = tmp_path / "r"
    _write(root / "pyproject.toml", '[tool.ruff]\nextend = "base.toml"\n')
    _write(root / "base.toml", "fix = true\n")
    assert _ruff_fixes(root, [root]) is True
    _write(root / "pyproject.toml", '[tool.ruff]\nextend = "base.toml"\nfix = false\n')
    assert _ruff_fixes(root, [root]) is False
    _write(root / "pyproject.toml", '[tool.ruff]\nextend = "missing.toml"\n')
    assert _ruff_fixes(root, [root]) is True          # unsure: passthrough
    _write(root / "pyproject.toml", '[tool.ruff]\nextend = "pyproject.toml"\n')
    assert _ruff_fixes(root, [root]) is True          # a loop: unsure
    # No project configuration: the user-level one.
    bare = tmp_path / "bare"
    bare.mkdir()
    xdg = tmp_path / "userxdg"
    env = {"XDG_CONFIG_HOME": str(xdg), "HOME": str(tmp_path)}
    assert _ruff_fixes(bare, [bare], env) is False
    _write(xdg / "ruff" / "ruff.toml", "fix = true\n")
    assert _ruff_fixes(bare, [bare], env) is True


@needs_ruff
def test_ruff_capture_refuses_fix_through_extend(tmp_path, layout):
    root = _repo(tmp_path, {"pyproject.toml": PYPROJECT.replace(
        "[tool.ruff.lint]", '[tool.ruff]\nextend = "base.toml"\n\n[tool.ruff.lint]'),
        "base.toml": "fix = true\n", "pkg/mod.py": MOD})
    mem = _mem(root, tmp_path)
    res = I.handle(mem, "shell", {"command": "ruff check pkg", "workdir": str(root)})
    assert res["handled"] is False and "fix = true" in res["reason"]
    assert (root / "pkg" / "mod.py").read_text() == MOD


@needs_ruff
def test_after_changes_without_a_verified_site(tmp_path, layout):
    import hashlib
    root = _repo(tmp_path, {"pyproject.toml": PYPROJECT, "pkg/__init__.py": "",
                            "pkg/three.py": THREE})
    mem = _mem(root, tmp_path)
    path = root / "pkg" / "three.py"
    path.write_text(THREE.replace("    return 1\n", "    return zz\n"))
    inp = {"filePath": str(path), "oldString": "    return 1\n",
           "newString": "    return zz\n"}
    # No usable hash and a repeated newString: not every occurrence.
    got = L.after_changes(mem, "edit", inp, ["pkg/three.py"], None, before_sha={})
    assert 2 not in (got.get("pkg/three.py") or set())
    # A unique newString needs no hash.
    path.write_text(THREE.replace("    return 1\n", "    return 2\n"))
    got = L.after_changes(mem, "edit", {**inp, "newString": "    return 2\n"},
                          ["pkg/three.py"], None, before_sha={})
    assert got == {"pkg/three.py": {10}}
    # replaceAll, verified by the hash.
    sha = hashlib.sha256(THREE.encode()).hexdigest()
    path.write_text(THREE.replace("zz", "yy"))
    got = L.after_changes(mem, "edit", {"filePath": str(path), "oldString": "zz",
                                        "newString": "yy", "replaceAll": True},
                          ["pkg/three.py"], None, before_sha={"pkg/three.py": sha})
    assert got == {"pkg/three.py": {2, 6}}
