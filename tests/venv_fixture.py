"""Fake virtual environments for the environment tests: a venv made with
`python -m venv --without-pip` whose site-packages holds hand-written
dist-info directories (no package is installed or fetched)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def make_venv(path: Path, dists: dict[str, list[str] | None]) -> Path:
    """A venv without pip whose site-packages holds fake distributions:
    name -> top-level names (top_level.txt), or None for a RECORD only."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)],
                   check=True, capture_output=True)
    site = next((path / "lib").glob("python*/site-packages"))
    for name, tops in dists.items():
        info = site / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8")
        if tops is not None:
            (info / "top_level.txt").write_text("\n".join(tops) + "\n", encoding="utf-8")
        else:
            # importlib.metadata lists only RECORD files that exist.
            (site / f"{name}_pkg").mkdir()
            (site / f"{name}_pkg" / "__init__.py").write_text("", encoding="utf-8")
            (info / "RECORD").write_text(
                f"{name}_pkg/__init__.py,,\n{name}-1.0.dist-info/METADATA,,\n",
                encoding="utf-8")
    return path / "bin" / "python"
