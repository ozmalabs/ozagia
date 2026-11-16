"""Deterministic git worktree, branch and policy checks.

No agent and no model: this module runs git plumbing, parses the result and
reports what it found. It never commits, merges, pushes, switches branches or
deletes a worktree. Mutating actions are a later, preview-first layer.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import tomllib

from ozagia.core.resources import disk_usage

SCHEMA = 1
GIT_TIMEOUT = 20
MAX_GIT_LOG = 200


class WorktreeError(RuntimeError):
    """A git command needed for the check could not run."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(path: str | Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LC_ALL", "C")
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
            env=env, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"git {' '.join(args)} could not run: {exc}") from exc


def _git_text(path: str | Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT) -> str | None:
    proc = _git(path, args, timeout=timeout)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def discover(root: str | Path) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` for ``root``."""
    root = Path(root).resolve()
    proc = _git(root, ["worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or proc.stdout or "git worktree list failed").strip()[:500])
    records: list[dict[str, Any]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current:
            return
        path = current.get("worktree")
        if not path:
            current.clear()
            return
        branch_ref = current.get("branch", "")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
        records.append({
            "path": str(Path(path).resolve()),
            "head": current.get("HEAD"),
            "branch": branch,
            "detached": "detached" in current,
            "bare": "bare" in current,
            "locked": current.get("locked"),
            "prunable": current.get("prunable"),
            "is_main": not records,
            "exists": Path(path).is_dir(),
        })
        current.clear()

    for line in proc.stdout.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    flush()
    if not records:
        raise WorktreeError(f"no git worktrees found for {root}")
    return records


def _parse_status(text: str) -> dict[str, Any]:
    state: dict[str, Any] = {
        "head": None, "branch": None, "upstream": None,
        "ahead": None, "behind": None,
        "dirty": {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0},
        "stashes": 0,
    }
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            state["head"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.head "):
            value = line.split(" ", 2)[2]
            state["branch"] = None if value == "(detached)" else value
        elif line.startswith("# branch.upstream "):
            state["upstream"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    state["ahead"] = int(parts[2].lstrip("+"))
                    state["behind"] = int(parts[3].lstrip("-"))
                except ValueError:
                    pass
        elif line.startswith("# stash "):
            try:
                state["stashes"] = int(line.split(" ", 2)[2])
            except (ValueError, IndexError):
                pass
        elif line.startswith("1 ") or line.startswith("2 "):
            parts = line.split(" ", 2)
            if len(parts) >= 2 and len(parts[1]) >= 2:
                if parts[1][0] != ".":
                    state["dirty"]["staged"] += 1
                if parts[1][1] != ".":
                    state["dirty"]["unstaged"] += 1
        elif line.startswith("u "):
            state["dirty"]["conflicted"] += 1
        elif line.startswith("? "):
            state["dirty"]["untracked"] += 1
    return state


def _operation(path: Path) -> str | None:
    names = {
        "MERGE_HEAD": "merge",
        "REBASE_HEAD": "rebase",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
        "rebase-merge": "rebase",
        "rebase-apply": "rebase",
    }
    for name, label in names.items():
        proc = _git(path, ["rev-parse", "--git-path", name])
        if proc.returncode != 0:
            continue
        value = proc.stdout.strip()
        if not value:
            continue
        marker = Path(value)
        if not marker.is_absolute():
            marker = path / marker
        if marker.exists():
            return label
    return None


def _stash_count(path: Path, parsed: int) -> int:
    if parsed:
        return parsed
    text = _git_text(path, ["stash", "list"])
    return len(text.splitlines()) if text else 0


def _base_counts(path: Path, base: str) -> tuple[int | None, int | None, str | None]:
    proc = _git(path, ["rev-list", "--left-right", "--count", f"{base}...HEAD"])
    if proc.returncode != 0:
        return None, None, None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None, None, None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None, None
    merge_base = _git_text(path, ["merge-base", base, "HEAD"])
    return behind, ahead, merge_base


def status(path: str | Path, *, base: str = "main") -> dict[str, Any]:
    """Status for one worktree. Never writes to the repository."""
    path = Path(path).resolve()
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_dir(),
        "head": None,
        "branch": None,
        "detached": False,
        "upstream": None,
        "ahead": None,
        "behind": None,
        "dirty": {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0},
        "stashes": 0,
        "operation": None,
        "filesystem": disk_usage(path),
        "base": {"branch": base, "ahead": None, "behind": None, "merge_base": None},
        "error": None,
        "findings": [],
    }
    if not path.is_dir():
        result["error"] = "worktree path does not exist"
        return result
    proc = _git(path, ["status", "--porcelain=v2", "--branch", "--show-stash"])
    if proc.returncode != 0:
        result["error"] = (proc.stderr or proc.stdout or "git status failed").strip()[:500]
        return result
    parsed = _parse_status(proc.stdout)
    result.update(parsed)
    result["detached"] = parsed.get("branch") is None and parsed.get("head") is not None
    result["stashes"] = _stash_count(path, parsed["stashes"])
    result["operation"] = _operation(path)
    behind, ahead, merge_base = _base_counts(path, base)
    result["base"] = {"branch": base, "ahead": ahead, "behind": behind,
                      "merge_base": merge_base}
    return result


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorktreeError(f"cannot read {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _config_dir(root: Path, main_path: Path, config_dir: str | Path | None) -> Path | None:
    candidates = []
    if config_dir is not None:
        candidates.append(Path(config_dir).expanduser())
    candidates.extend([root / "ozagia", main_path / "ozagia"])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _worktree_map(root: Path, main_path: Path,
                  config_dir: str | Path | None) -> dict[str, dict[str, Any]]:
    directory = _config_dir(root, main_path, config_dir)
    data = _load_toml(directory / "worktrees.toml" if directory else None)
    entries = data.get("worktrees") or {}
    if not isinstance(entries, dict):
        raise WorktreeError("[worktrees] must be a table")
    mapping: dict[str, dict[str, Any]] = {}
    for name, value in entries.items():
        if not isinstance(value, dict):
            continue
        raw_path = value.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = main_path / candidate
        resolved = str(candidate.resolve())
        mapping[resolved] = {"name": str(name), **value}
    return mapping


def _expected(path: Path, main_path: Path, mapping: Mapping[str, Mapping[str, Any]],
              base: str) -> dict[str, Any]:
    entry = mapping.get(str(path.resolve()))
    if entry is not None:
        return {
            "branch": entry.get("branch"),
            "source": "worktrees.toml",
            "name": entry.get("name"),
            "protected": bool(entry.get("protected", False)),
            "base": entry.get("base") or base,
        }
    if path.resolve() == main_path.resolve():
        return {"branch": base, "source": "main", "protected": True,
                "name": "main", "base": base}
    if path.parent.name == "ozagia-wt":
        return {"branch": f"feat/{path.name}", "source": "pattern",
                "protected": False, "name": path.name, "base": base}
    return {"branch": None, "source": "none", "protected": False,
            "name": None, "base": base}


def _findings(item: Mapping[str, Any], expected: Mapping[str, Any]) -> list[dict[str, Any]]:
    path = item.get("path")
    findings: list[dict[str, Any]] = []

    def add(level: str, ident: str, message: str, **extra: Any) -> None:
        findings.append({"level": level, "id": ident, "message": message,
                         "worktree": path, **extra})

    if not item.get("exists"):
        add("error", "worktree.missing", "worktree path does not exist")
        return findings
    if item.get("error"):
        add("error", "worktree.status", str(item["error"]))
    expected_branch = expected.get("branch")
    actual = item.get("branch")
    if expected_branch and actual and expected_branch != actual:
        add("error", "worktree.expected_branch",
            f"expected branch {expected_branch!r}, found {actual!r}",
            expected=expected_branch, actual=actual)
    if item.get("detached"):
        add("warn", "worktree.detached",
            "HEAD is detached; commits/merges/pushes still require a branch")
    if item.get("operation"):
        add("error", "worktree.operation",
            f"a {item['operation']} operation is in progress")
    dirty = item.get("dirty") or {}
    changed = sum(int(dirty.get(key, 0)) for key in ("staged", "unstaged",
                                                     "untracked", "conflicted"))
    if changed:
        add("info", "worktree.dirty",
            f"{changed} uncommitted or untracked path(s)",
            dirty=dict(dirty))
    if dirty.get("conflicted"):
        add("error", "worktree.conflicts",
            f"{dirty['conflicted']} conflicted path(s)")
    if item.get("prunable"):
        add("warn", "worktree.prunable", "git reports this worktree as prunable")
    if item.get("locked"):
        add("info", "worktree.locked", "worktree is locked")
    filesystem = item.get("filesystem") or {}
    if filesystem.get("is_tmpfs"):
        add("warn", "worktree.tmpfs",
            f"worktree is on tmpfs ({filesystem.get('mount')}); it consumes memory")
    return findings


def check(root: str | Path, *, base: str = "main",
          config_dir: str | Path | None = None) -> dict[str, Any]:
    """Check every worktree against its expected branch, drift and disk."""
    root = Path(root).resolve()
    worktrees = discover(root)
    main_path = Path(worktrees[0]["path"])
    mapping = _worktree_map(root, main_path, config_dir)
    checked: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for item in worktrees:
        item_status = status(item["path"], base=base)
        expected = _expected(Path(item["path"]), main_path, mapping, base)
        item_status["expected"] = expected
        item_findings = _findings(item_status, expected)
        item_status["findings"] = item_findings
        checked.append(item_status)
        findings.extend(item_findings)
    ok = not any(finding.get("level") == "error" for finding in findings)
    return {
        "schema": SCHEMA,
        "root": str(root),
        "base": base,
        "checked_at": _now(),
        "ok": ok,
        "worktrees": checked,
        "findings": findings,
    }


DEFAULT_POLICY: dict[str, Any] = {
    "commit": {
        "protected": ["main"],
        "branch_pattern": None,
        "message_pattern": None,
        "require_signed": False,
        "staged_only": False,
        "allow_merge_commits": False,
        "allow_fixup": False,
        "allow_protected": False,
    },
    "merge": {
        "target": "main",
        "require_clean": True,
        "require_up_to_date": False,
        "mode": "either",
        "required_checks": [],
    },
    "push": {
        "remote": "origin",
        "protected": ["main"],
        "allow_force": False,
        "allow_tags": False,
        "require_upstream": False,
    },
}


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    out = {key: (dict(value) if isinstance(value, Mapping) else value)
           for key, value in base.items()}
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _policy_config(root: Path, main_path: Path,
                   config_dir: str | Path | None) -> dict[str, Any]:
    directory = _config_dir(root, main_path, config_dir)
    data = _load_toml(directory / "policies.toml" if directory else None)
    return _deep_merge(DEFAULT_POLICY, data)


def _result(checks: list[dict[str, Any]], *, kind: str, path: Path,
            branch: str | None, expected: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "kind": kind,
        "path": str(path),
        "branch": branch,
        "expected": dict(expected),
        "checked_at": _now(),
        "ok": not any(item.get("level") == "error" for item in checks),
        "checks": checks,
    }


def _add(checks: list[dict[str, Any]], level: str, ident: str, message: str,
         *, command: str | None = None, **extra: Any) -> None:
    checks.append({"level": level, "id": ident, "message": message,
                   "command": command, **extra})


def _merge_preview(path: Path, target: str, head: str) -> dict[str, Any]:
    command = f"git merge-tree --write-tree --no-messages {target} {head}"
    proc = _git(path, ["merge-tree", "--write-tree", "--no-messages", target, head],
                timeout=30)
    if proc.returncode in (0, 1):
        return {"supported": True, "conflicts": proc.returncode == 1,
                "command": command, "exit_code": proc.returncode}
    merge_base = _git_text(path, ["merge-base", target, head])
    if merge_base:
        command = f"git merge-tree {merge_base[:12]} {target} {head}"
        legacy = _git(path, ["merge-tree", merge_base, target, head], timeout=30)
        if legacy.returncode in (0, 1):
            text = legacy.stdout
            conflicts = ("<<<<<<<" in text or "changed in both" in text)
            return {"supported": True, "conflicts": conflicts,
                    "command": command, "exit_code": legacy.returncode}
    return {"supported": False, "conflicts": None, "command": command,
            "exit_code": proc.returncode}


def _branches_containing(path: Path, branch: str) -> list[str]:
    text = _git_text(path, ["worktree", "list", "--porcelain"])
    if not text:
        return []
    out: list[str] = []
    current: list[str] = []
    for line in text.splitlines() + [""]:
        if not line.strip():
            if any(item == f"branch refs/heads/{branch}" for item in current):
                for item in current:
                    if item.startswith("worktree "):
                        out.append(item.split(" ", 1)[1])
            current = []
        else:
            current.append(line)
    return out


def _secret_policy_checks(path: Path, kind: str, base: str,
                          allow_secrets: Sequence[str],
                          security: Mapping[str, Any] | None
                          ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the default secret gate for a commit or push policy check."""
    from ozagia.core import security as security_core
    checks: list[dict[str, Any]] = []
    prefix = "commit" if kind == "commit" else "push"
    settings = security_core.load_config(path)
    override = dict(security or {})
    block = bool(override.get("block_secrets", settings["block_secrets"]))
    allowlist = override.get("secret_allowlist", settings["secret_allowlist"])
    if not isinstance(allowlist, list):
        allowlist = list(settings["secret_allowlist"])
    allowlist = [str(item) for item in allowlist if isinstance(item, str)]
    tokens_raw = ([allow_secrets] if isinstance(allow_secrets, str)
                  else list(allow_secrets or []))
    tokens = [str(item) for item in tokens_raw
              if str(item).startswith("sha256:")
              and len(str(item)) > len("sha256:") + 4]
    invalid_tokens = [str(item) for item in tokens_raw
                      if not (str(item).startswith("sha256:")
                              and len(str(item)) > len("sha256:") + 4)]
    info: dict[str, Any] = {
        "block_secrets": block,
        "secret_allowlist": allowlist,
        "overrides": [],
        "mode": "staged" if kind == "commit" else f"{base}..HEAD",
        "findings": 0,
        "allowed": 0,
        "disabled": not block,
        "truncated": False,
    }
    if invalid_tokens:
        _add(checks, "warn", f"{prefix}.secret_override_invalid",
             "ignored an override that is not a sha256: fingerprint")
    try:
        if kind == "commit":
            report = security_core.scan_secrets(
                path, staged=True, allowlist=allowlist, block_secrets=block)
        else:
            report = security_core.scan_secrets(
                path, diff=f"{base}..HEAD", allowlist=allowlist,
                block_secrets=block)
    except security_core.SecurityError as exc:
        _add(checks, "not_checked", f"{prefix}.secret_scan",
             f"secret scan unavailable: {exc}",
             command=("git diff --cached --unified=0" if kind == "commit"
                      else f"git diff {base}..HEAD --unified=0"))
        info["error"] = str(exc)
        return checks, info
    info.update({
        "findings": int((report.get("summary") or {}).get("total", 0)),
        "allowed": int((report.get("summary") or {}).get("allowed", 0)),
        "truncated": bool((report.get("summary") or {}).get("truncated")),
        "notes": list(report.get("notes") or []),
    })
    if not block:
        _add(checks, "warn", f"{prefix}.secret_gate",
             "secret gate disabled by [security] block_secrets=false; "
             "findings are reported but do not block")
        if info["findings"]:
            _add(checks, "warn", f"{prefix}.secret",
                 f"{info['findings']} secret-like finding(s) are not blocking "
                 "because the gate is disabled")
        info["disabled"] = True
        return checks, info
    if info["truncated"]:
        _add(checks, "error", f"{prefix}.secret_scan",
             "the secret diff exceeded the scan bound; the gate is not "
             "checked completely",
             command=("git diff --cached --unified=0" if kind == "commit"
                      else f"git diff {base}..HEAD --unified=0"))
    if info["allowed"]:
        _add(checks, "info", f"{prefix}.secret_allowlisted",
             f"{info['allowed']} secret finding(s) matched the configured "
             "allowlist")
    for finding in report.get("findings") or []:
        fingerprint = str(finding.get("secret_fingerprint") or "")
        rule = str(finding.get("secret_rule") or "secret")
        file_path = finding.get("file")
        line = finding.get("line")
        location = str(file_path or "unknown")
        if line:
            location += f":{line}"
        override = next(
            (token for token in tokens if fingerprint == token
             or (fingerprint and fingerprint.startswith(token))), None)
        if override:
            try:
                record = security_core.record_secret_decision(
                    path, kind=prefix, finding=finding,
                    decision="allowed_once", mode=str(info["mode"]))
            except security_core.SecurityError as exc:
                _add(checks, "error", f"{prefix}.secret_override_audit",
                     f"secret override could not be recorded: {exc}",
                     fingerprint=fingerprint)
                _add(checks, "error", f"{prefix}.secret",
                     f"secret-like value matched rule {rule} at {location}; "
                     f"fingerprint {fingerprint}; value withheld",
                     command=("git diff --cached --unified=0"
                              if kind == "commit" else
                              f"git diff {base}..HEAD --unified=0"),
                     rule=rule, path=file_path, line=line,
                     fingerprint=fingerprint)
                continue
            info["overrides"].append(record)
            _add(checks, "warn", f"{prefix}.secret_override",
                 f"secret-like value matched rule {rule} at {location}; "
                 f"allowed once by explicit override (fingerprint "
                 f"{fingerprint}); the value is withheld",
                 rule=rule, path=file_path, line=line,
                 fingerprint=fingerprint)
            continue
        _add(checks, "error", f"{prefix}.secret",
             f"secret-like value matched rule {rule} at {location}; "
             f"fingerprint {fingerprint}; value withheld",
             command=("git diff --cached --unified=0" if kind == "commit"
                      else f"git diff {base}..HEAD --unified=0"),
             rule=rule, path=file_path, line=line,
             fingerprint=fingerprint)
    if not info["findings"] and not info["truncated"]:
        _add(checks, "ok", f"{prefix}.secret",
             "no secret-like content found in the checked range")
    return checks, info


def policy_check(path: str | Path, kind: str, *, base: str = "main",
                 target: str | None = None, message: str | None = None,
                 checks: Mapping[str, bool] | None = None,
                 force: bool = False, tags: bool = False,
                 config_dir: str | Path | None = None,
                 allow_secrets: Sequence[str] = (),
                 security: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Check commit, merge or push policy for one worktree. No mutation.

    Commit checks scan staged content for secrets and push checks scan the
    outgoing ``base..HEAD`` diff. The gate is on by default; set
    ``[security] block_secrets = false`` to disable it. ``allow_secrets`` is a
    one-time override by fingerprint and is recorded under the state
    directory. ``security`` may override ``block_secrets`` and
    ``secret_allowlist`` for this call only.
    """
    if kind not in {"commit", "merge", "push"}:
        raise ValueError("kind must be commit, merge or push")
    path = Path(path).resolve()
    root = path
    worktrees = discover(root)
    main_path = Path(worktrees[0]["path"])
    mapping = _worktree_map(root, main_path, config_dir)
    expected = _expected(path, main_path, mapping, base)
    item = status(path, base=base)
    item["expected"] = expected
    policy = _policy_config(root, main_path, config_dir)
    checks_out: list[dict[str, Any]] = []
    branch = item.get("branch")
    protected = {str(value) for value in
                 (policy.get(kind) or {}).get("protected", [])
                 if isinstance(value, str)}

    if expected.get("branch") and branch and expected["branch"] != branch:
        _add(checks_out, "error", "worktree.expected_branch",
             f"expected branch {expected['branch']!r}, found {branch!r}",
             command="git symbolic-ref --short HEAD")
    if not branch:
        _add(checks_out, "error", f"{kind}.branch",
             "HEAD is detached; no branch to check",
             command="git symbolic-ref --short HEAD")

    if kind == "commit":
        section = policy.get("commit") or {}
        if branch in protected and not section.get("allow_protected"):
            _add(checks_out, "error", "commit.protected_branch",
                 f"branch {branch!r} is protected", command="git branch --show-current")
        pattern = section.get("branch_pattern")
        if isinstance(pattern, str) and pattern and branch:
            if re.fullmatch(pattern, branch) is None:
                _add(checks_out, "error", "commit.branch_pattern",
                     f"branch {branch!r} does not match {pattern!r}",
                     command="git branch --show-current")
        dirty = item.get("dirty") or {}
        if dirty.get("conflicted"):
            _add(checks_out, "error", "commit.conflicts",
                 f"{dirty['conflicted']} conflicted path(s)",
                 command="git status --short")
        if section.get("staged_only"):
            if not dirty.get("staged"):
                _add(checks_out, "error", "commit.nothing_staged",
                     "no staged changes", command="git diff --cached --name-only")
            if dirty.get("unstaged"):
                _add(checks_out, "warn", "commit.unstaged",
                     f"{dirty['unstaged']} unstaged path(s)",
                     command="git status --short")
        name = _git_text(path, ["config", "user.name"])
        email = _git_text(path, ["config", "user.email"])
        if not name or not email:
            _add(checks_out, "error", "commit.author",
                 "git user.name/user.email is not configured",
                 command="git config user.name")
        else:
            _add(checks_out, "ok", "commit.author",
                 f"author identity is {name} <{email}>",
                 command="git config user.name")
        if section.get("require_signed"):
            signed = _git_text(path, ["config", "--bool", "commit.gpgsign"])
            if signed != "true":
                _add(checks_out, "error", "commit.signed",
                     "commit signing is required but commit.gpgsign is not true",
                     command="git config --bool commit.gpgsign")
        if message is not None:
            pattern = section.get("message_pattern")
            if isinstance(pattern, str) and pattern:
                if re.fullmatch(pattern, message) is None:
                    _add(checks_out, "error", "commit.message",
                         "commit message does not match the policy",
                         command=None)
                else:
                    _add(checks_out, "ok", "commit.message",
                         "commit message matches the policy")
            else:
                _add(checks_out, "not_checked", "commit.message",
                     "no message policy is configured")
        else:
            _add(checks_out, "not_checked", "commit.message",
                 "no commit message was supplied")
        log = _git_text(path, ["log", f"{base}..HEAD", "--format=%s",
                               f"--max-count={MAX_GIT_LOG}"])
        if log is None:
            _add(checks_out, "not_checked", "commit.base",
                 f"base {base!r} could not be compared")
        else:
            subjects = log.splitlines() if log else []
            fixups = [s for s in subjects if s.startswith(("fixup!", "squash!"))]
            if fixups and not section.get("allow_fixup"):
                _add(checks_out, "error", "commit.fixup",
                     f"{len(fixups)} fixup/squash commit(s) on the branch",
                     command=f"git log {base}..HEAD --format=%s")
            merges = _git_text(path, ["log", f"{base}..HEAD", "--merges",
                                      "--oneline", f"--max-count={MAX_GIT_LOG}"])
            if merges and not section.get("allow_merge_commits"):
                _add(checks_out, "error", "commit.merge_commits",
                     "merge commits are not allowed on this branch",
                     command=f"git log {base}..HEAD --merges --oneline")
        unstaged = _git(path, ["diff", "--check"])
        staged = _git(path, ["diff", "--cached", "--check"])
        whitespace = [text for text in (unstaged.stdout, staged.stdout) if text.strip()]
        if whitespace:
            _add(checks_out, "error", "commit.whitespace",
                 "whitespace errors in the working tree or index",
                 command="git diff --check")
        else:
            _add(checks_out, "ok", "commit.whitespace",
                 "no whitespace errors", command="git diff --check")

    elif kind == "merge":
        section = policy.get("merge") or {}
        target = target or section.get("target") or "main"
        target_ok = _git(path, ["rev-parse", "--verify", f"{target}^{{commit}}"])
        if target_ok.returncode != 0:
            _add(checks_out, "error", "merge.target_missing",
                 f"target {target!r} does not exist",
                 command=f"git rev-parse --verify {target}^{{commit}}")
        if branch == target:
            _add(checks_out, "error", "merge.same_branch",
                 f"source and target are both {branch!r}")
        if section.get("require_clean"):
            dirty = item.get("dirty") or {}
            changed = sum(int(dirty.get(key, 0)) for key in
                          ("staged", "unstaged", "untracked", "conflicted"))
            if changed:
                _add(checks_out, "error", "merge.clean",
                     f"{changed} uncommitted or untracked path(s)",
                     command="git status --short")
            else:
                _add(checks_out, "ok", "merge.clean",
                     "worktree is clean", command="git status --short")
        preview = _merge_preview(path, target, "HEAD")
        if not preview["supported"]:
            _add(checks_out, "not_checked", "merge.conflicts",
                 "git merge-tree conflict preview is unavailable",
                 command=preview["command"])
        elif preview["conflicts"]:
            _add(checks_out, "error", "merge.conflicts",
                 f"merging into {target!r} has conflicts",
                 command=preview["command"])
        else:
            _add(checks_out, "ok", "merge.conflicts",
                 f"merging into {target!r} is conflict-free",
                 command=preview["command"])
        base_info = item.get("base") or {}
        if isinstance(base_info.get("behind"), int) and base_info["behind"] > 0:
            level = "error" if section.get("require_up_to_date") else "warn"
            _add(checks_out, level, "merge.up_to_date",
                 f"branch is {base_info['behind']} commit(s) behind {base}",
                 command=f"git rev-list --left-right --count {base}...HEAD")
        target_worktrees = [w for w in worktrees if w.get("branch") == target]
        if target_worktrees:
            target_status = status(target_worktrees[0]["path"], base=base)
            dirty = target_status.get("dirty") or {}
            changed = sum(int(dirty.get(key, 0)) for key in
                          ("staged", "unstaged", "untracked", "conflicted"))
            if changed:
                _add(checks_out, "error", "merge.target_clean",
                     f"target worktree has {changed} uncommitted path(s)",
                     command=f"git -C {target_worktrees[0]['path']} status --short")
        required = [name for name in section.get("required_checks") or []
                    if isinstance(name, str)]
        for name in required:
            if checks is None or name not in checks:
                _add(checks_out, "error", "merge.check_missing",
                     f"required check {name!r} was not supplied")
            elif not checks[name]:
                _add(checks_out, "error", "merge.check_failed",
                     f"required check {name!r} failed")
        if required and checks is not None and all(
                checks.get(name) for name in required):
            _add(checks_out, "ok", "merge.checks",
                 "all required checks passed")

    else:
        section = policy.get("push") or {}
        if branch in protected and not section.get("allow_protected"):
            _add(checks_out, "error", "push.protected_branch",
                 f"direct push to {branch!r} is not allowed",
                 command="git branch --show-current")
        pattern = section.get("branch_pattern")
        if isinstance(pattern, str) and pattern and branch:
            if re.fullmatch(pattern, branch) is None:
                _add(checks_out, "error", "push.branch_pattern",
                     f"branch {branch!r} does not match {pattern!r}")
        remote = section.get("remote") or "origin"
        remote_ok = _git(path, ["remote", "get-url", remote])
        if remote_ok.returncode != 0:
            _add(checks_out, "error", "push.remote",
                 f"remote {remote!r} is not configured",
                 command=f"git remote get-url {remote}")
        else:
            _add(checks_out, "ok", "push.remote",
                 f"remote {remote!r} is configured",
                 command=f"git remote get-url {remote}")
        upstream = item.get("upstream")
        if not upstream:
            level = "error" if section.get("require_upstream") else "warn"
            _add(checks_out, level, "push.upstream",
                 "branch has no upstream", command="git status --branch")
        if force and not section.get("allow_force"):
            _add(checks_out, "error", "push.force",
                 "force-push is not allowed by policy")
        if tags and not section.get("allow_tags"):
            _add(checks_out, "error", "push.tags",
                 "pushing tags is not allowed by policy")
        if item.get("ahead") == 0:
            _add(checks_out, "info", "push.nothing_to_push",
                 "branch has no commits ahead of its upstream")
        holders = _branches_containing(path, branch) if branch else []
        if len(holders) > 1:
            _add(checks_out, "error", "push.branch_checked_out",
                 f"branch is checked out in {len(holders)} worktrees")

    secret_info: dict[str, Any] = {}
    if kind in ("commit", "push"):
        secret_checks, secret_info = _secret_policy_checks(
            path, kind, base, allow_secrets, security)
        checks_out.extend(secret_checks)

    result = _result(checks_out, kind=kind, path=path, branch=branch,
                     expected=expected)
    if secret_info:
        result["security"] = secret_info
    return result


def prune_dry_run(root: str | Path) -> dict[str, Any]:
    """Show what ``git worktree prune`` would remove; never prunes."""
    root = Path(root).resolve()
    proc = _git(root, ["worktree", "prune", "--dry-run", "--verbose"])
    return {
        "schema": SCHEMA,
        "root": str(root),
        "checked_at": _now(),
        "ok": proc.returncode == 0,
        "dry_run": True,
        "output": proc.stdout.strip(),
        "error": (proc.stderr or "").strip() or None,
    }


def format_list(worktrees: Sequence[Mapping[str, Any]]) -> str:
    lines = []
    for item in worktrees:
        branch = item.get("branch") or "(detached)"
        marker = "main" if item.get("is_main") else "    "
        dirty = item.get("dirty") or {}
        changed = sum(int(dirty.get(key, 0)) for key in
                      ("staged", "unstaged", "untracked", "conflicted"))
        lines.append(f"{marker} {item.get('path')}  {branch}  "
                     f"dirty={changed}  head={(item.get('head') or '')[:12]}")
    return "\n".join(lines)


def format_check(report: Mapping[str, Any]) -> str:
    head = "ok" if report.get("ok") else "problems"
    lines = [f"{head}: {len(report.get('worktrees') or [])} worktree(s) "
             f"against {report.get('base')}"]
    for finding in report.get("findings") or []:
        lines.append(f"  {finding.get('level')}: {finding.get('worktree')}: "
                     f"{finding.get('message')}")
    if not report.get("findings"):
        lines.append("  no findings")
    return "\n".join(lines)


def format_policy(report: Mapping[str, Any]) -> str:
    head = "ok" if report.get("ok") else "refused"
    lines = [f"{head}: {report.get('kind')} on {report.get('path')} "
             f"({report.get('branch')})"]
    for check in report.get("checks") or []:
        if check.get("level") in {"ok", "not_checked"}:
            continue
        lines.append(f"  {check.get('level')}: {check.get('id')}: "
                     f"{check.get('message')}")
    if not any(check.get("level") not in {"ok", "not_checked"}
               for check in report.get("checks") or []):
        lines.append("  no policy problems")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_POLICY", "WorktreeError", "check", "discover", "format_check",
    "format_list", "format_policy", "policy_check", "prune_dry_run", "status",
]
