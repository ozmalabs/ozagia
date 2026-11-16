"""The repo-review fixture repository, shared by tests/test_repo_review.py
and tests/test_review_goals.py (the review -> goal -> check integration).

`make_repo(tmp_path)` builds a small git repository whose review has
findings in every section; `_rich_repo(repo)` extends it so every finding
type of `core.repo_review.FINDING_TYPES` occurs.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ozagia.core.memory import build_index
from ozagia.core.working_set import save_test_record

# --------------------------------------------------------------------------- #
# fixture repository
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Review Tester",
        "GIT_AUTHOR_EMAIL": "review@example.com",
        "GIT_COMMITTER_NAME": "Review Tester",
        "GIT_COMMITTER_EMAIL": "review@example.com",
    })
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


FILES = {
    ".gitignore": ".ozagia/\n",
    # Resource admission off, as in the code-review tests.
    "ozagia/resources.toml": "enabled = false\n",
    ".ozagia.toml": '[review]\nlayers = ["src/pkg/b", "src/pkg/a"]\n',
    "src/pkg/__init__.py": "",
    "src/pkg/a/__init__.py": "",
    # pkg.a and pkg.b import each other: a package cycle.
    "src/pkg/a/mod_a.py": "from pkg.b import mod_b\n\n\ndef fa():\n"
                          "    return mod_b.fb()\n",
    "src/pkg/b/__init__.py": "",
    "src/pkg/b/mod_b.py": "from pkg.a import mod_a\n\n\ndef fb():\n"
                          "    return 1\n\n\ndef unused_thing():\n"
                          "    return mod_a\n",
    # No test and no caller.
    "src/pkg/c.py": "def lonely():\n    return 3\n",
    "src/pkg/danger.py": "import ast\nimport subprocess\n\n\n"
                         "def run_it(cmd):\n"
                         "    ast.literal_eval('1')\n"
                         "    return subprocess.run(cmd, shell=True)\n",
    "tests/test_a.py": "from pkg.a.mod_a import fa\nfrom pkg.danger import run_it\n"
                       "from pkg.b.mod_b import fb\n\n\n"
                       "def test_fa():\n    assert fa() == fb() == 1\n"
                       "    assert run_it\n",
    "README.md": "# Fixture\n\nSee [the guide](docs/guide.md).\n",
    "docs/guide.md": (
        "# Guide\n\n"
        "The code is in `src/pkg/c.py` and `src/pkg/missing.py`.\n"
        "Call `pkg.a.mod_a.fa` or `pkg.a.mod_a.nothing_here`.\n"
        "A [broken link](../src/pkg/gone.py) and a [web link](https://example.com).\n"
        "Paths of other trees such as `.codex/config.toml` are not checked.\n"
        "```\nsrc/pkg/in_fence.py\n```\n"),
    "docs/plans/old.md": "# Plan\n\nAdd `src/pkg/planned.py`.\n",
    "deploy/compose.yaml": (
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "    ports:\n"
        "      - \"8080:80\"\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    restart: always\n"
        "    healthcheck:\n"
        "      test: [\"CMD\", \"true\"]\n"),
    ".env": "DEBUG=1\n",
    ".env.example": "DEBUG=0\n",
    "keys/id_ed25519": "placeholder, not a key\n",
    "pyproject.toml": "[project]\nname = \"fixture\"\nversion = \"0\"\n"
                      "dependencies = [\"requests>=2\"]\n",
}


def _graph(repo: Path) -> Path:
    path = repo / ".ozagia" / "graph.json"
    build_index(repo, path, include_docs="features", include_git=True)
    return path



def make_repo(tmp_path: Path) -> Path:
    """The review fixture repository under `tmp_path`: committed, with a
    test record (one failing test) and a fresh graph."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "commit.gpgsign", "false")
    for rel, text in FILES.items():
        _write(root, rel, text)
    _commit(root, "fixture")
    now = time.time()
    save_test_record(root, {
        "tests": {"test:tests/test_a.py::test_fa":
                  {"outcome": "failed", "ts": now, "since": now - 60}},
        "last": {"ts": now, "failed": 1, "complete": True},
    })
    _graph(root)
    return root


def _rich_repo(repo: Path) -> None:
    """Extend the fixture so every finding type occurs."""
    _write(repo, "src/pkg/hub/__init__.py", "")
    _write(repo, "src/pkg/hub/core.py", "def hub_fn():\n    return 0\n")
    for number in range(11):
        _write(repo, f"src/pkg/hub/user{number}.py",
               "from pkg.hub.core import hub_fn\n\n\n"
               f"def use{number}():\n    return hub_fn()\n")
    body = "".join(f"    v{n} = {n}\n" for n in range(110))
    _write(repo, "src/pkg/big.py",
           "".join(f"X{n} = {n}\n" for n in range(1000))
           + "\n\ndef long_one():\n" + body + "    return v0\n"
           + "".join(f"# TODO item {n}\n" for n in range(5)))
    # A cycle between two modules of one directory.
    _write(repo, "src/pkg/cyc_p.py", "from pkg.cyc_q import qf\n\n\n"
                                     "def pf():\n    return qf\n")
    _write(repo, "src/pkg/cyc_q.py", "from pkg.cyc_p import pf\n\n\n"
                                     "def qf():\n    return pf\n")
    _write(repo, "tests/test_big.py",
           "from pkg.big import long_one\n\n\ndef test_big():\n"
           "    assert long_one() == 0\n")
    # An import no manifest declares and no interpreter has, and one
    # declared only in an optional extra.
    _write(repo, "pyproject.toml",
           FILES["pyproject.toml"]
           + "\n[project.optional-dependencies]\nplot = [\"zz-optional-pkg\"]\n")
    _write(repo, "tests/test_env.py",
           "import zz_absent_pkg\nimport zz_optional_pkg\n\n\n"
           "def test_env():\n    assert zz_absent_pkg and zz_optional_pkg\n")
    _commit(repo, "rich")
    for number in range(3):
        _write(repo, "src/pkg/c.py", f"def lonely():\n    return {number + 4}\n")
        _commit(repo, f"churn {number}")
    journal = repo / ".ozagia" / "journal.jsonl"
    now = time.time()
    journal.write_text("".join(
        json.dumps({"session": {"id": f"s{n}", "harness": "opencode"},
                    "files": ["src/pkg/c.py"], "epoch": now - n,
                    "kind": "edit"}) + "\n" for n in range(3)), encoding="utf-8")
    _graph(repo)
