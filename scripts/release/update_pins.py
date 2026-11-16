#!/usr/bin/env python3
"""Pin new upstream versions in release/pins.json.

    python scripts/release/update_pins.py --opencode 2.4.0
    python scripts/release/update_pins.py --runtime 20261201 --python 3.13.19

Reads the asset digests from the GitHub releases API (with `gh` when it is
installed, else anonymously), so every pinned sha256 is the one GitHub serves.
Run the smoke test on each platform (the release workflow does) before a
release that changes a pin.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PINS = REPO / "release" / "pins.json"
TRIPLES = {"linux-x64": "x86_64-unknown-linux-gnu",
           "linux-arm64": "aarch64-unknown-linux-gnu",
           "darwin-arm64": "aarch64-apple-darwin",
           "darwin-x64": "x86_64-apple-darwin"}
OPENCODE = {"linux-x64": ("opencode-linux-x64.tar.gz", "opencode-linux-x64-baseline.tar.gz"),
            "linux-arm64": ("opencode-linux-arm64.tar.gz", None),
            "darwin-arm64": ("opencode-darwin-arm64.zip", None),
            "darwin-x64": ("opencode-darwin-x64.zip", "opencode-darwin-x64-baseline.zip")}


def assets(repo: str, tag: str) -> dict[str, str]:
    path = f"repos/{repo}/releases/tags/{tag}"
    if shutil.which("gh"):
        raw = subprocess.run(["gh", "api", path], check=True, capture_output=True,
                             text=True).stdout
    else:
        with urllib.request.urlopen(f"https://api.github.com/{path}", timeout=60) as r:
            raw = r.read().decode()
    out = {}
    for a in json.loads(raw)["assets"]:
        digest = a.get("digest") or ""
        if digest.startswith("sha256:"):
            out[a["name"]] = digest.split(":", 1)[1]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--opencode", help="OpenCode version, e.g. 2.4.0")
    ap.add_argument("--runtime", help="python-build-standalone release tag")
    ap.add_argument("--python", help="CPython version in that release")
    args = ap.parse_args(argv)
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    if args.opencode:
        v = args.opencode.lstrip("v")
        got = assets("anomalyco/opencode", f"v{v}")
        new = {}
        for target, (default, baseline) in OPENCODE.items():
            new[target] = {"default": {"name": default, "sha256": got[default]}}
            if baseline:
                new[target]["baseline"] = {"name": baseline, "sha256": got[baseline]}
        pins["opencode"].update(version=v, assets=new,
                                url=f"https://github.com/anomalyco/opencode/releases/download/v{v}/{{name}}")
    if args.runtime or args.python:
        if not (args.runtime and args.python):
            raise SystemExit("--runtime and --python go together")
        got = assets("astral-sh/python-build-standalone", args.runtime)
        new = {}
        for target, triple in TRIPLES.items():
            name = f"cpython-{args.python}+{args.runtime}-{triple}-install_only_stripped.tar.gz"
            new[target] = {"name": name, "sha256": got[name]}
        pins["runtime"].update(
            release=args.runtime, python=args.python, assets=new,
            url=("https://github.com/astral-sh/python-build-standalone/releases/download/"
                 f"{args.runtime}/{{name}}"))
    PINS.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    print(f"pinned OpenCode {pins['opencode']['version']}, CPython "
          f"{pins['runtime']['python']} ({pins['runtime']['release']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
