#!/usr/bin/env python3
"""Build a self-contained ozagia bundle for one platform.

    python scripts/release/build_bundle.py --wheel dist/ozagia-X-py3-none-any.whl \
        [--target linux-x64] [--out dist] [--cache ~/.cache/ozagia-release]

The bundle `ozagia-<target>.tar.gz` holds one directory, `ozagia-<target>/`:

    python/          a relocatable CPython (python-build-standalone, pinned in
                     release/pins.json) with ozagia and its extras installed
    manifest.json    name, version, target, python, and the pinned OpenCode
                     release the installer downloads (name and sha256 per variant)
    LICENSE, THIRD_PARTY.md

The runtime is downloaded once into the cache and checked against its pinned
sha256. Extras with native code (tree-sitter) install from wheels for the
platform this script runs on, so build each target on its own platform (the
release workflow does). `--target` defaults to this machine's.

Standard library only: run it with any Python 3.12+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PINS = REPO / "release" / "pins.json"


def host_target() -> str:
    """This machine's target name (linux-x64, linux-arm64, darwin-arm64,
    darwin-x64)."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "darwin"}.get(system)
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64"}.get(machine)
    if not os_name or not arch:
        raise SystemExit(f"unsupported platform: {system}/{machine}")
    return f"{os_name}-{arch}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path, sha256: str) -> Path:
    """`url` into `dest` (kept if it already has `sha256`), verified."""
    if dest.exists() and sha256_file(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"download {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=120) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    got = sha256_file(tmp)
    if got != sha256:
        tmp.unlink()
        raise SystemExit(f"sha256 mismatch for {url}: {got} != {sha256}")
    tmp.replace(dest)
    return dest


def wheel_version(wheel: Path) -> str:
    # ozagia-<version>-py3-none-any.whl
    parts = wheel.name.split("-")
    if len(parts) < 5 or parts[0] != "ozagia":
        raise SystemExit(f"not an ozagia wheel: {wheel.name}")
    return parts[1]


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    tf.extractall(dest, filter="tar")


def build(wheel: Path, target: str, out: Path, cache: Path) -> Path:
    pins = json.loads(PINS.read_text(encoding="utf-8"))
    if target not in pins["targets"]:
        raise SystemExit(f"unknown target {target}; known: {pins['targets']}")
    if target != host_target():
        raise SystemExit(f"build {target} on {target}: native wheels come from this "
                         f"machine ({host_target()})")
    version = wheel_version(wheel)
    rt = pins["runtime"]
    asset = rt["assets"][target]
    archive = fetch(rt["url"].format(name=asset["name"]), cache / asset["name"],
                    asset["sha256"])

    name = f"ozagia-{target}"
    with tempfile.TemporaryDirectory(prefix="ozagia-bundle-") as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        with tarfile.open(archive) as tf:
            _safe_extract(tf, stage)          # -> stage/python
        python = stage / "python" / "bin" / "python3"
        if not python.exists():
            raise SystemExit(f"runtime has no python/bin/python3: {archive}")
        extras = ",".join(pins.get("extras") or [])
        spec = f"{wheel.resolve()}[{extras}]" if extras else str(wheel.resolve())
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PYTHON", "PIP_", "VIRTUAL_ENV"))}
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-input",
                        "--no-warn-script-location", "--only-binary", ":all:", spec],
                       check=True, env=env)
        # Console scripts carry this build's absolute path; the installer's
        # launcher runs `python -I -m ozagia` instead.
        for script in (stage / "python" / "bin").glob("ozagia*"):
            script.unlink()
        got = subprocess.run([str(python), "-I", "-c",
                              "import importlib.metadata as m; print(m.version('ozagia'))"],
                             check=True, capture_output=True, text=True, env=env).stdout.strip()
        if got != version:
            raise SystemExit(f"installed ozagia {got}, expected {version}")
        manifest = {
            "name": "ozagia",
            "version": version,
            "target": target,
            "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": {"version": rt["python"], "release": rt["release"],
                       "source": rt["source"]},
            "opencode": {"version": pins["opencode"]["version"],
                         "source": pins["opencode"]["source"],
                         "url": pins["opencode"]["url"],
                         "assets": pins["opencode"]["assets"][target]},
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                             encoding="utf-8")
        shutil.copy(REPO / "LICENSE", stage / "LICENSE")
        (stage / "THIRD_PARTY.md").write_text(THIRD_PARTY.format(
            python=rt["python"], release=rt["release"],
            opencode=pins["opencode"]["version"]), encoding="utf-8")
        out.mkdir(parents=True, exist_ok=True)
        bundle = out / f"{name}.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(stage, arcname=name)
    print(bundle)
    return bundle


THIRD_PARTY = """# Third-party software in this bundle

- CPython {python} from python-build-standalone release {release}
  (https://github.com/astral-sh/python-build-standalone), under the Python
  Software Foundation License and the licenses listed in
  `python/lib/python3.*/LICENSE.txt` and the build's own notices.
- Python packages installed in `python/lib/python3.*/site-packages`, each
  under the license in its `*.dist-info` directory.

Not in this bundle: OpenCode {opencode} (MIT, https://github.com/anomalyco/opencode).
The installer downloads it from OpenCode's GitHub release and checks its
pinned sha256.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wheel", required=True, type=Path)
    ap.add_argument("--target", default=None, help="default: this machine")
    ap.add_argument("--out", default=Path("dist"), type=Path)
    ap.add_argument("--cache", default=Path.home() / ".cache" / "ozagia-release",
                    type=Path)
    args = ap.parse_args(argv)
    build(args.wheel, args.target or host_target(), args.out, args.cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
