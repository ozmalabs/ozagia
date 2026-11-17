"""Bounded fast-forward updates for explicitly designated federation references."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TIMEOUT = 60
_CACHE_SECONDS = 30.0
_lock = threading.Lock()
_cache: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}
_checkout_locks: dict[str, threading.Lock] = {}


def _git(root: Path, *args: str, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    env["GIT_CONFIG_VALUE_0"] = os.devnull
    env["GIT_CONFIG_KEY_1"] = "credential.interactive"
    env["GIT_CONFIG_VALUE_1"] = "never"
    env["GIT_CONFIG_KEY_2"] = "protocol.ext.allow"
    env["GIT_CONFIG_VALUE_2"] = "never"
    env["GIT_CONFIG_KEY_3"] = "credential.helper"
    env["GIT_CONFIG_VALUE_3"] = ""
    env["GIT_CONFIG_COUNT"] = "4"
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, timeout=timeout, check=False, env=env)


def _error(root: Path, reason: str, detail: str = "") -> dict[str, Any]:
    # Never return raw Git output: transports can echo credential-bearing URLs
    # and status output can disclose unrelated filenames.
    safe_detail = ("working tree has staged, modified, or untracked files"
                   if reason == "working_tree_dirty" else "")
    return {"root": str(root), "state": "error", "reason": reason,
            "detail": safe_detail, "updated": False}


def sync_reference(root: str | Path, *, pin: str | None = None,
                   force: bool = False) -> dict[str, Any]:
    """Fast-forward an unpinned clean checkout from its matching origin branch."""
    supplied = Path(root).expanduser()
    if supplied.is_symlink():
        return _error(supplied.absolute(), "symlink_checkout")
    path = supplied.resolve()
    checkout_key = str(path)
    with _lock:
        checkout_lock = _checkout_locks.setdefault(checkout_key, threading.Lock())
    # Serialize the cache check and the entire Git transaction for this path.
    # Other checkouts use different locks and can proceed concurrently.
    with checkout_lock:
        return _sync_reference_locked(path, pin=pin, force=force)


def _sync_reference_locked(path: Path, *, pin: str | None,
                           force: bool) -> dict[str, Any]:
    cache_key = (str(path), pin)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(cache_key)
        if not force and cached and now - cached[0] < _CACHE_SECONDS:
            return {**cached[1], "cached": True}

    try:
        if not path.is_dir() or path.is_symlink():
            result = _error(path, "not_a_directory")
        elif pin is not None:
            if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", pin):
                result = _error(path, "invalid_pin", "pin must be a full commit object ID")
            else:
                top = _git(path, "rev-parse", "--show-toplevel")
                if top.returncode or Path(top.stdout.strip()).resolve() != path:
                    result = _error(path, "not_a_git_checkout")
                else:
                    head = _git(path, "rev-parse", "HEAD")
                    if head.returncode:
                        result = _error(path, "not_a_git_checkout")
                    elif head.stdout.strip().lower() != pin.lower():
                        result = {**_error(path, "pin_mismatch"),
                                  "head": head.stdout.strip(), "pin": pin}
                    else:
                        result = {"root": str(path), "state": "pinned", "updated": False,
                                  "head": head.stdout.strip(), "pin": pin}
        else:
            result = _sync_unpinned(path)
    except subprocess.TimeoutExpired:
        result = _error(path, "timeout")
    except OSError as exc:
        result = _error(path, "git_unavailable", str(exc))

    with _lock:
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        _cache[cache_key] = (time.monotonic(), result)
    return dict(result)


def _sync_unpinned(path: Path) -> dict[str, Any]:
    if not path.is_dir() or path.is_symlink():
        return _error(path, "not_a_directory")
    inside = _git(path, "rev-parse", "--show-toplevel")
    if inside.returncode or Path(inside.stdout.strip()).resolve() != path:
        return _error(path, "not_a_git_checkout")
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode or not branch.stdout.strip():
        return _error(path, "detached_head")
    branch_name = branch.stdout.strip()
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode or upstream.stdout.strip() != f"origin/{branch_name}":
        return _error(path, "upstream_not_origin_branch")
    status = _git(path, "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        return _error(path, "status_failed")
    if status.stdout:
        return _error(path, "working_tree_dirty")
    before = _git(path, "rev-parse", "HEAD")
    if before.returncode:
        return _error(path, "head_unavailable")
    remote_ref = f"refs/remotes/origin/{branch_name}"
    fetched = _git(path, "fetch", "--no-tags", "--no-recurse-submodules",
                   "origin", f"refs/heads/{branch_name}:{remote_ref}")
    if fetched.returncode:
        return _error(path, "fetch_failed")
    target = _git(path, "rev-parse", remote_ref)
    if target.returncode:
        return _error(path, "remote_branch_missing")
    ancestor = _git(path, "merge-base", "--is-ancestor", "HEAD", remote_ref)
    if ancestor.returncode == 1:
        return _error(path, "not_fast_forward")
    if ancestor.returncode:
        return _error(path, "ancestry_check_failed")
    if target.stdout.strip() == before.stdout.strip():
        return {"root": str(path), "state": "current", "updated": False,
                "head": before.stdout.strip(), "upstream": f"origin/{branch_name}"}
    status = _git(path, "status", "--porcelain", "--untracked-files=all")
    if status.returncode:
        return _error(path, "status_failed")
    if status.stdout:
        return _error(path, "working_tree_dirty")
    merged = _git(path, "merge", "--ff-only", "--no-edit", remote_ref)
    if merged.returncode:
        return _error(path, "fast_forward_failed")
    after = _git(path, "rev-parse", "HEAD")
    if after.returncode or after.stdout.strip() != target.stdout.strip():
        return _error(path, "post_update_verification_failed")
    return {"root": str(path), "state": "updated", "updated": True,
            "previous_head": before.stdout.strip(), "head": after.stdout.strip(),
            "upstream": f"origin/{branch_name}",
            "index_state": "stale_until_reindexed"}


__all__ = ["sync_reference"]


def cached_status(root: str | Path) -> list[dict[str, Any]]:
    """Return cached sync results without reading Git or accessing the network."""
    with _lock:
        return [dict(value[1], cached=True) for (path, _pin), value in _cache.items()
                if path == str(Path(root).expanduser().resolve())]
