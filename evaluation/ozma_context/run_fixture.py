"""Run the offline Ozma/Ozagia fixture with cold and warm preparation timing.

The fixture is copied to a temporary Git repository. No model, provider, or
network service is called. The caller supplies a local Ozma backend checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]


def _run(command: list[str], *, env: dict[str, str], cwd: Path) -> tuple[str, float]:
    start = time.perf_counter()
    done = subprocess.run(command, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120, check=True)
    return done.stdout, time.perf_counter() - start


def _revision(root: Path, env: dict[str, str]) -> str | None:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            env=env, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def run(backend: Path, budgets: str = "100,300") -> dict:
    backend = backend.resolve()
    env = dict(os.environ)
    # Ignore caller-specific project settings when constructing a clean fixture.
    for key in ("OZAGIA_ROOT", "OZAGIA_CONFIG", "OZAGIA_STATE_DIR", "OZAGIA_ADAPTERS",
                "OZMA_OZAGIA_CONTEXT", "OZMA_OZAGIA_CONTEXT_BUDGET"):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="ozagia-ozma-eval-") as directory:
        root = Path(directory) / "repo"
        shutil.copytree(HERE / "fixture", root)
        env["OZAGIA_ROOT"] = str(root)
        _run(["git", "init", "-q", str(root)], env=env, cwd=PROJECT)
        _run(["git", "-C", str(root), "add", "."], env=env, cwd=PROJECT)
        _run(["git", "-C", str(root), "-c", "user.name=Fixture Eval", "-c",
              "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
             env=env, cwd=PROJECT)
        cmd = [sys.executable, "-m", "ozagia.cli", "index", "--root", str(root), "--json"]
        cold, cold_time = _run([*cmd, "--rebuild"], env=env, cwd=PROJECT)
        warm, warm_time = _run(cmd, env=env, cwd=PROJECT)
        compare, compare_time = _run([
            sys.executable, str(HERE / "compare_assembler.py"), "--root", str(root),
            "--ozma-backend", str(backend), "--budgets", budgets,
        ], env=env, cwd=PROJECT)
        return {
            "version": 1,
            "ozagia_revision": _revision(PROJECT, env),
            "ozma_revision": _revision(backend, env),
            "preparation": {"cold": {"seconds": cold_time, "report": json.loads(cold)},
                            "warm": {"seconds": warm_time, "report": json.loads(warm)}},
            "comparison_seconds": compare_time,
            "comparison": json.loads(compare),
            "limitations": ["Temporary synthetic fixture; no model, agent or network calls.",
                            "Preparation times include process startup; revisions do not describe dirty changes.",
                            "Comparison arms and budgets are not equal-cost controls."],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ozma-backend", type=Path, required=True)
    parser.add_argument("--budgets", default="100,300")
    args = parser.parse_args()
    print(json.dumps(run(args.ozma_backend, args.budgets), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
