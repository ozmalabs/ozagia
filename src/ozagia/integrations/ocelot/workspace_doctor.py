"""Read-only Ocelot engine and worktree doctor.

The doctor answers one question before a measurement is trusted: is this
checkout's ``substrate_rs`` engine a build of *this tree's* Rust source? It
does so without importing Ocelot, executing the configured interpreter,
building anything, or writing into the checkout.

The important checks are intentionally mechanical:

- the native artifact: path, size, SHA-256, real path and symlink target;
- the build record beside it, including the source fingerprint and engine hash;
- the same Rust source fingerprint Ocelot's ``domains/engine_provenance.py``
  uses (sorted repo-relative ``.rs`` paths, then ``runners/dsl/Cargo.toml``);
- git worktree/branch/HEAD/dirty state, for the "shared canonical tree" hazard;
- ``substrate_rs.pth`` files that could resolve the package from another tree;
- ``data/`` and ``results/failure_baseline.json``.

A missing native artifact, unreadable build record, stale Rust source or
mismatched engine hash is ``blocked``. Missing datasets and an ambiguous
``.pth`` are ``warn`` when the engine itself is sound. ``ok`` is true only
for ``ready``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

NATIVE_REL = Path("runners/dsl/python/substrate_rs/_native.abi3.so")
BUILD_RECORD_REL = Path("runners/dsl/python/substrate_rs/_native.build.json")
PACKAGE_REL = Path("runners/dsl/python/substrate_rs")
RUST_SRC_REL = Path("runners/dsl/src")
CARGO_REL = Path("runners/dsl/Cargo.toml")
DATA_REL = Path("data")
BASELINE_REL = Path("results/failure_baseline.json")
PTH_NAME = "substrate_rs.pth"

GIT_TIMEOUT = 15.0
DIRTY_PATH_LIMIT = 50
TOP_LEVEL_LIST_LIMIT = 50
MAX_PTH_ENTRIES = 20
MAX_PTH_TEXT = 4096
MAX_BUILD_RECORD_BYTES = 8 * 1024 * 1024

_CARGO_POSIX = CARGO_REL.as_posix()


def _resolve_root(root: str | Path) -> Path:
    path = Path(root).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_hash(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.lower() == right.lower())


def _fingerprint_from_hashes(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(rel for rel in hashes if rel != _CARGO_POSIX):
        digest.update(rel.encode("utf-8"))
        digest.update(hashes[rel].encode("utf-8"))
    if _CARGO_POSIX in hashes:
        digest.update(b"Cargo.toml")
        digest.update(hashes[_CARGO_POSIX].encode("utf-8"))
    return digest.hexdigest()


def rust_source_hashes(root: str | Path) -> dict[str, str]:
    """``{repo-relative POSIX path: sha256}`` for the Rust source.

    Every ``.rs`` file under ``runners/dsl/src``, followed by
    ``runners/dsl/Cargo.toml`` when it exists. Paths are relative to ``root``
    and always use ``/``, so the result is stable across platforms.
    """
    root_path = _resolve_root(root)
    src = root_path / RUST_SRC_REL
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".rs"):
                continue
            path = Path(dirpath) / name
            found.append((path.relative_to(root_path).as_posix(), path))
    hashes = {rel: _sha256_file(path) for rel, path in sorted(found)}
    cargo = root_path / CARGO_REL
    if cargo.is_file():
        hashes[_CARGO_POSIX] = _sha256_file(cargo)
    return hashes


def rust_source_fingerprint(root: str | Path) -> str | None:
    """Content fingerprint of the Rust source, matching Ocelot's reporter.

    Returns ``None`` when ``runners/dsl/src`` does not exist, exactly like
    Ocelot's ``domains.engine_provenance.rust_source_fingerprint``.
    """
    root_path = _resolve_root(root)
    if not (root_path / RUST_SRC_REL).is_dir():
        return None
    return _fingerprint_from_hashes(rust_source_hashes(root_path))


def _native_info(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    is_symlink = path.is_symlink()
    link_target: str | None = None
    if is_symlink:
        try:
            link_target = os.readlink(path)
        except OSError:
            link_target = None

    size: int | None = None
    digest: str | None = None
    error: str | None = None
    if exists:
        try:
            size = path.stat().st_size
            digest = _sha256_file(path)
        except OSError as exc:
            error = f"{type(exc).__name__}: {exc}"

    return {
        "path": str(path),
        "exists": exists,
        "size": size,
        "sha256": digest,
        "realpath": str(path.resolve(strict=False)),
        "is_symlink": is_symlink,
        "link_target": link_target,
        "error": error,
    }


def _read_build_record(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "readable": False,
        "error": None,
        "source_fingerprint": None,
        "engine_sha256": None,
        "files": {},
        "file_count": 0,
    }
    if not result["exists"]:
        return result
    try:
        if path.stat().st_size > MAX_BUILD_RECORD_BYTES:
            result["error"] = (
                f"build record is larger than {MAX_BUILD_RECORD_BYTES} bytes")
            return result
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    if not isinstance(raw, dict):
        result["error"] = "build record is not a JSON object"
        return result

    result["readable"] = True
    source_fingerprint = raw.get("source_fingerprint")
    if isinstance(source_fingerprint, str):
        result["source_fingerprint"] = source_fingerprint
    engine_sha256 = raw.get("engine_sha256")
    if isinstance(engine_sha256, str):
        result["engine_sha256"] = engine_sha256
    files = raw.get("files")
    if isinstance(files, dict):
        result["files"] = {
            key: value for key, value in files.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    result["file_count"] = len(result["files"])
    return result


def _engine_info(root: Path) -> dict[str, Any]:
    native_path = root / NATIVE_REL
    package_path = root / PACKAGE_REL
    record_path = root / BUILD_RECORD_REL
    src_path = root / RUST_SRC_REL
    cargo_path = root / CARGO_REL

    native = _native_info(native_path)
    package_exists = package_path.is_dir()
    py_files: list[str] = []
    if package_exists:
        try:
            py_files = sorted(
                item.name for item in package_path.glob("*.py")
                if item.is_file())
        except OSError:
            py_files = []
    headless_package = package_exists and bool(py_files) and not native["exists"]

    record = _read_build_record(record_path)
    source_error: str | None = None
    source_hashes: dict[str, str] = {}
    try:
        source_hashes = rust_source_hashes(root)
    except OSError as exc:
        source_error = f"{type(exc).__name__}: {exc}"
    source_fingerprint = (
        _fingerprint_from_hashes(source_hashes)
        if src_path.is_dir() and source_error is None
        else None
    )

    recorded_fingerprint = record["source_fingerprint"]
    recorded_engine_sha = record["engine_sha256"]
    source_matches = _same_hash(source_fingerprint, recorded_fingerprint)
    engine_hash_matches = _same_hash(native["sha256"], recorded_engine_sha)

    stale_rust: list[str] = []
    changed_rust: list[str] = []
    new_rust: list[str] = []
    missing_rust: list[str] = []
    if record["readable"] and record["files"]:
        built_files: dict[str, str] = record["files"]
        stale_rust = [
            rel for rel, digest in sorted(source_hashes.items())
            if built_files.get(rel) != digest
        ]
        stale_rust += [
            rel for rel in sorted(built_files) if rel not in source_hashes
        ]
        changed_rust = sorted(
            rel for rel, digest in source_hashes.items()
            if rel in built_files and built_files[rel] != digest
        )
        new_rust = sorted(
            rel for rel in source_hashes if rel not in built_files)
        missing_rust = sorted(
            rel for rel in built_files if rel not in source_hashes)

    blockers: list[str] = []
    if not native["exists"]:
        message = f"native engine artifact is missing: {native_path}"
        if headless_package:
            message += (
                " (headless package: Python files present, no native module)")
        blockers.append(message)
    elif native["sha256"] is None:
        detail = f" ({native['error']})" if native["error"] else ""
        blockers.append(
            f"native engine artifact is unreadable: {native_path}{detail}")

    if not record["exists"]:
        blockers.append(f"build record is missing: {record_path}")
    elif not record["readable"]:
        blockers.append(
            f"build record is unreadable: {record_path}: {record['error']}")

    if source_fingerprint is None:
        detail = f" ({source_error})" if source_error else ""
        blockers.append(
            f"Rust source fingerprint is unavailable: {src_path}{detail}")
    elif record["readable"] and recorded_fingerprint is None:
        blockers.append(
            f"build record has no source_fingerprint: {record_path}")
    elif record["readable"] and not source_matches:
        blockers.append(
            "Rust source does not match the build record: "
            f"{len(stale_rust)} file(s) changed, added or removed")

    if record["readable"] and recorded_engine_sha is None:
        blockers.append(f"build record has no engine_sha256: {record_path}")
    elif (record["readable"] and native["exists"]
          and native["sha256"] is not None
          and not engine_hash_matches):
        blockers.append(
            "engine sha256 does not match the build record: "
            f"built {recorded_engine_sha[:12]}..., "
            f"current {native['sha256'][:12]}...")

    state = "blocked" if blockers else "ready"
    return {
        "state": state,
        "verdict": state,
        "native": native,
        "package": {
            "path": str(package_path),
            "exists": package_exists,
            "py_files": py_files,
            "py_file_count": len(py_files),
            "headless": headless_package,
            "headless_package": headless_package,
        },
        "headless_package": headless_package,
        "build_record": record,
        "source": {
            "path": str(src_path),
            "exists": src_path.is_dir(),
            "fingerprint": source_fingerprint,
            "file_count": len(source_hashes),
            "rust_file_count": sum(
                1 for rel in source_hashes if rel != _CARGO_POSIX),
            "cargo_toml": cargo_path.is_file(),
            "cargo_toml_path": str(cargo_path),
            "error": source_error,
        },
        "source_fingerprint": source_fingerprint,
        "build_source_fingerprint": (
            recorded_fingerprint if record["readable"] else None),
        "engine_sha256": native["sha256"],
        "build_engine_sha256": (
            recorded_engine_sha if record["readable"] else None),
        "source_matches_record": source_matches,
        "engine_hash_matches_record": engine_hash_matches,
        "build_is_stale": bool(stale_rust),
        "stale_rust": stale_rust,
        "changed_rust": changed_rust,
        "new_rust": new_rust,
        "missing_rust": missing_rust,
        "blockers": blockers,
    }


def _run_git(root: Path, *args: str) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _parse_worktrees(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line:
            if current is not None:
                entries.append(current)
                current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current is not None:
                entries.append(current)
            current = {
                "path": value,
                "head": None,
                "branch": None,
                "bare": False,
                "detached": False,
                "locked": False,
                "locked_reason": None,
                "prunable": False,
                "prunable_reason": None,
                "current": False,
            }
        elif current is None:
            continue
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "bare":
            current["bare"] = True
        elif key == "detached":
            current["detached"] = True
        elif key == "locked":
            current["locked"] = True
            current["locked_reason"] = value or None
        elif key == "prunable":
            current["prunable"] = True
            current["prunable_reason"] = value or None
    if current is not None:
        entries.append(current)
    return entries


def _git_info(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    code, stdout, stderr = _run_git(root, "rev-parse", "--show-toplevel")
    if code != 0:
        message = stderr.strip() or stdout.strip() or (
            f"git rev-parse exited {code}")
        return {
            "available": False,
            "top_level": None,
            "branch": None,
            "head": None,
            "detached": False,
            "dirty": {
                "count": None,
                "paths": [],
                "limit": DIRTY_PATH_LIMIT,
                "truncated": False,
            },
            "dirty_count": None,
            "dirty_paths": [],
            "worktrees": [],
            "worktree_count": 0,
            "is_main_worktree": None,
            "error": message,
            "errors": [message],
        }

    top_text = stdout.strip()
    top_level = Path(top_text).resolve(strict=False) if top_text else root

    code, stdout, stderr = _run_git(
        root, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = stdout.strip() if code == 0 and stdout.strip() else None
    if code != 0 and stderr.strip():
        errors.append(stderr.strip())

    code, stdout, stderr = _run_git(root, "rev-parse", "HEAD")
    head = stdout.strip() if code == 0 and stdout.strip() else None
    if code != 0 and stderr.strip():
        errors.append(stderr.strip())
    detached = head is not None and branch is None

    code, stdout, stderr = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--no-renames",
    )
    if code == 0:
        records = [record for record in stdout.split("\0") if record]
        dirty_paths = [
            record[3:] for record in records if len(record) > 3]
        dirty = {
            "count": len(dirty_paths),
            "paths": dirty_paths[:DIRTY_PATH_LIMIT],
            "limit": DIRTY_PATH_LIMIT,
            "truncated": len(dirty_paths) > DIRTY_PATH_LIMIT,
        }
    else:
        message = stderr.strip() or f"git status exited {code}"
        errors.append(message)
        dirty = {
            "count": None,
            "paths": [],
            "limit": DIRTY_PATH_LIMIT,
            "truncated": False,
        }

    code, stdout, stderr = _run_git(
        root, "worktree", "list", "--porcelain")
    if code == 0:
        worktrees = _parse_worktrees(stdout)
        for entry in worktrees:
            entry_path = Path(entry["path"]).resolve(strict=False)
            entry["current"] = entry_path == top_level
    else:
        message = stderr.strip() or f"git worktree list exited {code}"
        errors.append(message)
        worktrees = []

    is_main_worktree: bool | None = None
    if worktrees:
        main_path = Path(worktrees[0]["path"]).resolve(strict=False)
        is_main_worktree = main_path == top_level

    return {
        "available": True,
        "top_level": str(top_level),
        "branch": branch,
        "head": head,
        "detached": detached,
        "dirty": dirty,
        "dirty_count": dirty["count"],
        "dirty_paths": dirty["paths"],
        "worktrees": worktrees,
        "worktree_count": len(worktrees),
        "is_main_worktree": is_main_worktree,
        "error": errors[0] if errors else None,
        "errors": errors,
    }


def _interpreter_info(root: Path, python: str | None) -> dict[str, Any]:
    if python is not None:
        configured = str(python)
        source = "argument"
    else:
        venv_python = root / ".venv" / "bin" / "python"
        if venv_python.is_file() and os.access(venv_python, os.X_OK):
            configured = str(venv_python)
            source = "venv"
        else:
            configured = "python3"
            source = "python3"

    path = Path(configured).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        exists = path.is_file() and os.access(path, os.X_OK)
        resolved = str(path.resolve(strict=False)) if exists else None
    else:
        found = shutil.which(configured)
        exists = found is not None
        resolved = found

    return {
        "path": configured,
        "exists": exists,
        "resolved": resolved,
        "source": source,
        "executed": False,
    }


def _pth_candidates(root: Path) -> list[Path]:
    bases = [
        root / ".venv" / "lib",
        Path.home() / ".local" / "lib",
    ]
    found: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        try:
            matches = sorted(
                base.glob(f"python*/site-packages/{PTH_NAME}"))
        except OSError:
            continue
        for match in matches:
            if not match.is_file():
                continue
            key = str(match.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            found.append(match)
            if len(found) >= MAX_PTH_ENTRIES:
                return found
    return found


def _read_pth(path: Path, root: Path) -> dict[str, Any]:
    text = ""
    error: str | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"

    targets: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("import "):
            continue
        target = Path(stripped).expanduser()
        resolved = (
            target if target.is_absolute() else path.parent / target
        ).resolve(strict=False)
        targets.append({
            "text": stripped,
            "resolved": str(resolved),
            "inside_root": resolved.is_relative_to(root),
            "exists": resolved.exists(),
        })

    inside_root: bool | None = None
    if targets:
        inside_root = all(target["inside_root"] for target in targets)
    return {
        "path": str(path),
        "target": text.strip()[:MAX_PTH_TEXT],
        "targets": targets,
        "inside_root": inside_root,
        "error": error,
    }


def _pth_info(root: Path) -> dict[str, Any]:
    entries = [_read_pth(path, root) for path in _pth_candidates(root)]
    outside_targets = [
        target["resolved"]
        for entry in entries
        for target in entry["targets"]
        if not target["inside_root"]
    ]
    distinct_targets = {
        target["resolved"]
        for entry in entries
        for target in entry["targets"]
    }
    inside_flags = [
        target["inside_root"]
        for entry in entries
        for target in entry["targets"]
    ]
    return {
        "entries": entries,
        "count": len(entries),
        "outside_root": bool(outside_targets),
        "outside_targets": outside_targets,
        "ambiguous": len(distinct_targets) > 1,
        "inside_root": all(inside_flags) if inside_flags else None,
    }


def _data_info(root: Path) -> dict[str, Any]:
    path = root / DATA_REL
    exists = path.is_dir()
    top_level_dirs: list[str] = []
    if exists:
        try:
            top_level_dirs = sorted(
                item.name for item in path.iterdir() if item.is_dir())
        except OSError:
            top_level_dirs = []
    return {
        "path": str(path),
        "exists": exists,
        "top_level_dirs": len(top_level_dirs),
        "dirs": top_level_dirs[:TOP_LEVEL_LIST_LIMIT],
        "truncated": len(top_level_dirs) > TOP_LEVEL_LIST_LIMIT,
    }


def _baseline_info(root: Path) -> dict[str, Any]:
    path = root / BASELINE_REL
    return {
        "path": str(path),
        "exists": path.is_file(),
    }


def _next_commands(
    engine: dict[str, Any],
    data: dict[str, Any],
    baseline: dict[str, Any],
    pth: dict[str, Any],
) -> list[str]:
    commands: list[str] = []

    def add(command: str) -> None:
        if command not in commands:
            commands.append(command)

    if engine["blockers"] or pth["outside_root"]:
        add("bash scripts/engine_build.sh --lib-only")
    if not data["exists"]:
        add("ln -s <canonical-ocelot>/data data")
    if not baseline["exists"]:
        add(
            "python scripts/failure_baseline.py build "
            "--raw results/failure_baseline_sweep_raw.json")
    add("ozagia index --root .")
    return commands


def status(
    root: str | Path,
    *,
    python: str | None = None,
) -> dict[str, Any]:
    """Inspect an Ocelot checkout without importing or executing Ocelot.

    ``root`` is resolved to an absolute path. Engine paths are interpreted
    relative to that path; git's own top level is reported separately.
    """
    root_path = _resolve_root(root)

    git = _git_info(root_path)
    engine = _engine_info(root_path)
    interpreter = _interpreter_info(root_path, python)
    pth = _pth_info(root_path)
    data = _data_info(root_path)
    baseline = _baseline_info(root_path)

    warnings: list[str] = []
    if not data["exists"]:
        warnings.append(
            f"data directory is missing: {data['path']} "
            "(a worktree usually symlinks the canonical checkout's data/)")
    if not baseline["exists"]:
        warnings.append(
            f"failure baseline is missing: {baseline['path']}")

    for entry in pth["entries"]:
        for target in entry["targets"]:
            if not target["inside_root"]:
                warnings.append(
                    "substrate_rs.pth points outside this worktree: "
                    f"{entry['path']} -> {target['resolved']}")
    if pth["ambiguous"]:
        warnings.append(
            "multiple substrate_rs.pth targets found; which substrate_rs "
            "imports depends on sys.path order")

    blockers = list(engine["blockers"])
    state = "blocked" if blockers else ("warn" if warnings else "ready")
    return {
        "ok": state == "ready",
        "state": state,
        "root": str(root_path),
        "git": git,
        "engine": engine,
        "interpreter": interpreter,
        "pth": pth,
        "data": data,
        "baseline": baseline,
        "warnings": warnings,
        "blockers": blockers,
        "next_commands": _next_commands(engine, data, baseline, pth),
    }


def format_text(result: dict[str, Any]) -> str:
    """A compact human-readable rendering of :func:`status`."""
    lines = [f"Ocelot workspace: {result.get('state', 'unknown')}"]
    lines.append(f"root: {result.get('root', '')}")

    git = result.get("git") or {}
    if git.get("available"):
        branch = git.get("branch") or "(detached)"
        head = (git.get("head") or "")[:12]
        dirty_count = (git.get("dirty") or {}).get("count")
        lines.append(
            f"git: {branch} @ {head} | dirty: {dirty_count} | "
            f"worktrees: {git.get('worktree_count', 0)}")
    else:
        lines.append(f"git: unavailable ({git.get('error') or 'no repository'})")

    engine = result.get("engine") or {}
    native = engine.get("native") or {}
    lines.append(
        f"engine: {engine.get('state', 'unknown')} | "
        f"native: {'yes' if native.get('exists') else 'no'} | "
        f"sha256: {native.get('sha256') or '-'}")
    if engine.get("stale_rust"):
        lines.append(f"stale rust: {', '.join(engine['stale_rust'])}")

    pth = result.get("pth") or {}
    lines.append(f"substrate_rs.pth entries: {pth.get('count', 0)}")
    data = result.get("data") or {}
    lines.append(
        f"data: {'yes' if data.get('exists') else 'no'} "
        f"({data.get('top_level_dirs', 0)} top-level dirs) | "
        f"baseline: {'yes' if (result.get('baseline') or {}).get('exists') else 'no'}")

    for title, key in (
        ("warnings", "warnings"),
        ("blockers", "blockers"),
        ("next", "next_commands"),
    ):
        items = result.get(key) or []
        if items:
            lines.append(f"{title}:")
            lines.extend(f"  - {item}" for item in items)
    return "\n".join(lines)
