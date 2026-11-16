#!/usr/bin/env python3
"""Prepare a release: set the version, add the changelog entry, commit, tag.

    python scripts/release/prepare.py 0.1.0            # commit + tag v0.1.0
    python scripts/release/prepare.py 0.1.0 --dry-run  # show what it would do
    python scripts/release/prepare.py 0.1.0 --push     # ... and push branch + tag

Checks first: on `main`, clean working tree, tag not taken, version is
X.Y.Z or a PEP 440 pre-release (X.Y.ZrcN, X.Y.ZbN, X.Y.ZaN; the tag is the
same string with a `v`) and newer than the current one. The
changelog entry lists the commit subjects since the last tag, under a heading
for the version; edit CHANGELOG.md before running it to replace the list with
prose (an existing "## Unreleased" section is renamed instead). Pushing the
tag starts .github/workflows/release.yml, which makes a draft release.
Standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"
CHANGELOG = REPO / "CHANGELOG.md"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_PRE = {"a": 0, "b": 1, "rc": 2}


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def key(version: str) -> tuple:
    """Sort key; X.Y.Z.devN counts as before every other X.Y.Z form."""
    m = VERSION_RE.match(version)
    if m:
        a, b, c, pre, n = m.groups()
        return (int(a), int(b), int(c), _PRE[pre] if pre else 9, int(n or 0))
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        raise SystemExit(f"cannot read version {version!r}")
    return (*map(int, m.groups()), -1, 0)


def current_version() -> str:
    m = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("no version in pyproject.toml")
    return m.group(1)


def changelog_entry(version: str) -> str:
    try:
        last = git("describe", "--tags", "--abbrev=0", "--match", "v*")
        rng = f"{last}..HEAD"
    except subprocess.CalledProcessError:
        last, rng = None, "HEAD"
    subjects = [s for s in git("log", "--no-merges", "--format=%s", rng).splitlines()
                if s and not s.startswith("Release v")]
    head = f"## {version} ({time.strftime('%Y-%m-%d')})\n\n"
    if last is None:
        head += "First release.\n\n"
    return head + "".join(f"- {s}\n" for s in subjects) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--allow-branch", action="store_true",
                    help="release from a branch other than main")
    args = ap.parse_args(argv)
    version = args.version.lstrip("v")
    if not VERSION_RE.match(version):
        raise SystemExit(f"version must be X.Y.Z or X.Y.Z(rc|b|a)N: {version}")
    cur = current_version()
    if key(version) <= key(cur):
        raise SystemExit(f"{version} is not newer than {cur}")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main" and not args.allow_branch:
        raise SystemExit(f"on {branch}, not main (use --allow-branch)")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("working tree has uncommitted changes")
    tag = f"v{version}"
    if git("tag", "--list", tag):
        raise SystemExit(f"tag {tag} exists")

    entry = changelog_entry(version)
    text = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else "# Changelog\n\n"
    if "## Unreleased" in text:
        new_cl = text.replace("## Unreleased",
                              f"## {version} ({time.strftime('%Y-%m-%d')})", 1)
    else:
        title, _, rest = text.partition("\n\n")
        new_cl = f"{title}\n\n{entry}{rest}"
    new_py = re.sub(r'^version = "[^"]+"', f'version = "{version}"',
                    PYPROJECT.read_text(encoding="utf-8"), count=1, flags=re.M)
    print(f"version {cur} -> {version}, tag {tag}, branch {branch}")
    if "## Unreleased" in text:
        print("changelog: the '## Unreleased' section becomes "
              f"'## {version} ({time.strftime('%Y-%m-%d')})'")
    else:
        print("changelog entry (no '## Unreleased' section):\n" + entry)
    if args.dry_run:
        return 0
    PYPROJECT.write_text(new_py, encoding="utf-8")
    CHANGELOG.write_text(new_cl, encoding="utf-8")
    git("add", "pyproject.toml", "CHANGELOG.md")
    git("commit", "-m", f"Release {tag}")
    git("tag", "-a", tag, "-m", f"ozagia {version}")
    print(f"committed and tagged {tag}")
    if args.push:
        git("push", "origin", branch)
        git("push", "origin", tag)
        print("pushed; the release workflow builds a draft release")
    else:
        print(f"push with: git push origin {branch} && git push origin {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
