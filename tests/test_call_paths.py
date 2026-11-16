"""Command- and patch-derived write paths (`ozagia.intent.call_paths`)."""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from ozagia.intent.call_paths import command_paths, patch_paths


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    return root


# --------------------------------------------------------------------------- #
# command_paths: redirections and simple write commands
# --------------------------------------------------------------------------- #
def test_redirections(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert command_paths("echo hi > out.txt", root=root) == ["out.txt"]
    assert command_paths("echo hi >> 'notes file.md'", root=root) == [
        "notes file.md"]
    assert command_paths("make 2> err.log &> all.log", root=root) == [
        "err.log", "all.log"]
    assert command_paths("cmd <> read-write.txt", root=root) == [
        "read-write.txt"]
    assert command_paths("echo x >| clobber.txt", root=root) == [
        "clobber.txt"]
    assert command_paths("echo x 10>> large.log", root=root) == ["large.log"]
    # Descriptor duplications are not files.
    assert command_paths("echo x 2>&1 > out.txt", root=root) == ["out.txt"]
    assert command_paths("echo x >&2", root=root) == []
    # The call's cwd resolves relative targets.
    assert command_paths("touch note.md", root=root, cwd="sub") == [
        "sub/note.md"]


def test_simple_write_commands(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert command_paths("tee -a one.log two.log", root=root) == [
        "one.log", "two.log"]
    assert command_paths("touch -d 2026-01-01 old.md", root=root) == [
        "old.md"]
    assert command_paths("mkdir -p -m 755 docs/plans", root=root) == [
        "docs/plans"]
    assert command_paths("rm -rf build", root=root) == ["build"]
    assert command_paths("rmdir empty", root=root) == ["empty"]
    assert command_paths("mv src.txt dst.txt", root=root) == [
        "src.txt", "dst.txt"]
    assert command_paths("cp -r src dst", root=root) == ["src", "dst"]
    assert command_paths("install -m 644 src dest", root=root) == [
        "src", "dest"]
    assert command_paths("ln -s target link", root=root) == [
        "target", "link"]
    assert command_paths("truncate -s 0 app.log", root=root) == ["app.log"]
    assert command_paths("sed -i.bak -e 's/a/b/' one.py two.py",
                         root=root) == ["one.py", "two.py"]
    assert command_paths("perl -pi -e 's/a/b/' one.pl", root=root) == [
        "one.pl"]
    assert command_paths("dd if=input of=output.bin bs=1", root=root) == [
        "output.bin"]
    # Reads are not write targets.
    assert command_paths("sed -n '1p' one.py", root=root) == []
    assert command_paths("perl -ne 'print' one.pl", root=root) == []


def test_compound_commands_pipelines_and_cd(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert command_paths(
        "cd sub && touch new.md && echo x > log.txt", root=root) == [
            "sub/new.md", "sub/log.txt"]
    assert command_paths("printf x | tee pipeline.log", root=root) == [
        "pipeline.log"]
    assert command_paths(
        "( cd sub; touch inner.md ) && touch outer.md", root=root) == [
            "sub/inner.md", "outer.md"]
    assert command_paths("cd .. && touch outside.md", root=root) == []
    assert command_paths("cd && touch guessed.md", root=root) == []
    assert command_paths("cd \"$HOME\" && touch guessed.md", root=root) == []
    assert command_paths("cd - && touch guessed.md", root=root) == []
    # A shell -c script is parsed once when it is a literal.
    assert command_paths("bash -c 'cd sub && touch from-shell.md'",
                         root=root) == ["sub/from-shell.md"]


def test_no_guessing_for_unparsed_code(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert command_paths("touch $(mktemp)", root=root) == []
    assert command_paths("touch \"$(mktemp)\"", root=root) == []
    assert command_paths("touch `cat list`", root=root) == []
    assert command_paths("touch *.md", root=root) == []
    assert command_paths("touch {a,b}.md", root=root) == []
    assert command_paths("touch ~/note.md", root=root) == []
    assert command_paths("xargs touch guessed.md", root=root) == []
    assert command_paths("find . -name '*.py' -exec touch found \\;",
                         root=root) == []
    assert command_paths("eval 'touch evaled.md'", root=root) == []
    assert command_paths("python -c 'open(\"py.md\", \"w\")'", root=root) == []
    assert command_paths("sh -c 'touch $(mktemp)'", root=root) == []
    assert command_paths("bash -c \"bash -c 'touch deep.md'\"",
                         root=root) == []
    assert command_paths("touch 'unbalanced", root=root) == []
    assert command_paths("cat <<EOF > heredoc.md\nx\nEOF", root=root) == []


def test_paths_are_normalised_and_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert command_paths("touch /etc/passwd", root=root) == []
    assert command_paths("touch ../outside.md", root=root) == []
    assert command_paths("touch sub/../../outside.md", root=root) == []
    assert command_paths("touch .git/config", root=root) == []
    assert command_paths("touch .ozagia/files/note.md", root=root) == []
    assert command_paths("touch sub/./note.md", root=root) == [
        "sub/note.md"]
    # An absolute path inside the root is returned relative to it.
    absolute = (root / "sub" / "abs.md").as_posix()
    assert command_paths(f"touch {absolute}", root=root) == ["sub/abs.md"]
    # A symlink that leaves the root is rejected.
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    assert command_paths("touch escape/nope.md", root=root) == []


def test_limit_and_dedupe(tmp_path: Path) -> None:
    root = _root(tmp_path)
    names = " ".join(f"f{i}.txt" for i in range(30))
    got = command_paths(f"touch {names}", root=root, limit=5)
    assert got == [f"f{i}.txt" for i in range(5)]
    assert command_paths("touch a.txt a.txt b.txt", root=root) == [
        "a.txt", "b.txt"]
    assert command_paths("touch a.txt", root=root, limit=0) == []
    many = " ".join(f"m{i}.txt" for i in range(100))
    assert len(command_paths(f"touch {many}", root=root)) == 64


def test_never_runs_the_command_or_uses_the_network(tmp_path: Path,
                                                    monkeypatch) -> None:
    root = _root(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("call_paths ran a process or opened a socket")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "socket", boom)
    assert command_paths("touch safe.md && echo x > out.md",
                         root=root) == ["safe.md", "out.md"]


# --------------------------------------------------------------------------- #
# patch_paths: the apply_patch tool
# --------------------------------------------------------------------------- #
def test_patch_text_paths_and_moves(tmp_path: Path) -> None:
    root = _root(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: pkg/new.py\n"
        "+x = 1\n"
        "*** Update File: pkg/old.py\n"
        "*** Move to: pkg/moved.py\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** Delete File: gone.py\n"
        "*** End Patch\n"
    )
    assert patch_paths({"patch": patch}, root=root) == [
        "pkg/new.py", "pkg/old.py", "pkg/moved.py", "gone.py"]
    assert patch_paths({"patch": patch}, root=root, cwd="sub") == [
        "sub/pkg/new.py", "sub/pkg/old.py", "sub/pkg/moved.py",
        "sub/gone.py"]
    # A parseable patch wins over explicit path keys.
    assert patch_paths({"patch": patch, "path": "ignored.py"},
                       root=root) == [
        "pkg/new.py", "pkg/old.py", "pkg/moved.py", "gone.py"]
    # The other accepted text keys.
    parts = patch.splitlines(keepends=True)
    assert patch_paths({"input": "".join(parts)}, root=root)
    assert patch_paths("".join(parts), root=root)


def test_patch_malformed_and_explicit_fallback(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert patch_paths({"patch": "not a patch", "path": "fallback.py"},
                       root=root) == ["fallback.py"]
    assert patch_paths({"patch": "*** Begin Patch\nbroken",
                        "filePath": "pkg/fallback.py", "root": root},
                       root=root) == ["pkg/fallback.py"]
    assert patch_paths({"files": ["a.py", "b.py"]}, root=root) == [
        "a.py", "b.py"]
    assert patch_paths({}, root=root) == []
    assert patch_paths("not a patch", root=root) == []


def test_patch_paths_are_bounded_and_safe(tmp_path: Path) -> None:
    root = _root(tmp_path)
    patch = (
        "*** Begin Patch\n"
        + "".join(f"*** Add File: f{i}.txt\n+x\n" for i in range(20))
        + "*** End Patch\n"
    )
    assert len(patch_paths({"patch": patch}, root=root, limit=3)) == 3
    assert patch_paths({"path": "/etc/passwd"}, root=root) == []
    assert patch_paths({"path": "../outside.md"}, root=root) == []
    assert patch_paths({"path": ".git/config"}, root=root) == []
    assert patch_paths({"path": ".ozagia/state.json"}, root=root) == []


# --------------------------------------------------------------------------- #
# git apply and patch read a named patch file
# --------------------------------------------------------------------------- #
_UNIFIED = (
    "diff --git a/one.txt b/one.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/one.txt\n"
    "@@ -0,0 +1 @@\n"
    "+one\n"
    "diff --git a/two.txt b/three.txt\n"
    "--- a/two.txt\n"
    "+++ b/three.txt\n"
    "@@ -1 +1 @@\n"
    "-two\n"
    "+three\n"
)


def test_git_apply_and_patch_targets(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "changes.patch").write_text(_UNIFIED, encoding="utf-8")
    assert command_paths("git apply changes.patch", root=root) == [
        "one.txt", "two.txt", "three.txt"]
    assert command_paths("git apply -p1 changes.patch", root=root) == [
        "one.txt", "two.txt", "three.txt"]
    assert command_paths("git apply < changes.patch", root=root) == [
        "one.txt", "two.txt", "three.txt"]
    assert command_paths("git apply --check changes.patch", root=root) == []
    assert command_paths("git apply --cached changes.patch", root=root) == []
    assert command_paths("git apply -", root=root) == []
    assert command_paths("patch -p1 < changes.patch", root=root) == [
        "one.txt", "two.txt", "three.txt"]
    assert command_paths("patch -p1 changes.patch", root=root) == [
        "one.txt", "two.txt", "three.txt"]
    assert command_paths("patch --dry-run -p1 changes.patch",
                         root=root) == []
    assert command_paths("patch -p1 ../outside.patch", root=root) == []


def test_patch_header_outside_paths_are_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "absolute.patch").write_text(
        "--- /etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8")
    assert command_paths("patch -p0 absolute.patch", root=root) == []
    assert command_paths("git apply -p0 absolute.patch", root=root) == []
    (root / "escape.patch").write_text(
        "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8")
    assert command_paths("patch -p1 escape.patch", root=root) == []


def test_patch_command_strips_components(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "plain.patch").write_text(
        "--- old.txt\n+++ new.txt\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8")
    assert command_paths("patch -p0 plain.patch", root=root) == [
        "old.txt", "new.txt"]
    assert command_paths("patch -p1 plain.patch", root=root) == []
    assert command_paths("patch -d sub -p0 plain.patch", root=root) == [
        "sub/old.txt", "sub/new.txt"]
