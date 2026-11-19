"""Fail-closed Git invocation helpers for read-only repository inspection.

Git status and worktree diff can invoke configured clean/process filters. These
helpers run those commands against an ephemeral Git dir that contains no target
config. They never execute configured filter commands.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence


class UnsafeGitConfig(RuntimeError):
    """The repository's Git config cannot be inspected safely for filters."""


MAX_INDEX_BYTES = 256 * 1024 * 1024
MAX_METADATA_FILES = 100_000
MAX_METADATA_BYTES = 128 * 1024 * 1024


def safe_git_env() -> dict[str, str]:
    """Remove caller Git overrides and disable system/global config."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    })
    env.setdefault("LC_ALL", "C")
    return env


def git_subcommand(args: Sequence[str]) -> str | None:
    """Find the first Git subcommand after global option/value pairs."""
    value_options = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                     "--config-env"}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in value_options:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def _plumbing(root: str | Path, args: Sequence[str], env: Mapping[str, str],
              timeout: float) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True,
            timeout=min(timeout, 5), env=dict(env), stdin=subprocess.DEVNULL,
            check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsafeGitConfig("cannot prepare isolated Git metadata") from exc
    if result.returncode != 0:
        raise UnsafeGitConfig("cannot prepare isolated Git metadata")
    return result.stdout


def _git_path(root: str | Path, name: str, env: Mapping[str, str],
              timeout: float) -> Path:
    value = _plumbing(root, ["rev-parse", "--path-format=absolute", "--git-path", name],
                      env, timeout).decode("utf-8", errors="strict").strip()
    path = Path(value)
    if not path.is_absolute():
        path = (Path(root) / path).resolve()
    return path


def _copy_regular(source: Path, destination: Path, *, limit: int) -> int:
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise UnsafeGitConfig("Git metadata is symlinked or exceeds its safe bound")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise UnsafeGitConfig("cannot read Git metadata safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (len(raw) != before.st_size or len(raw) > limit
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise UnsafeGitConfig("Git metadata changed while being copied")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return len(raw)


def _copy_tree(source: Path, destination: Path, *, file_budget: int,
               byte_budget: int) -> tuple[int, int]:
    if not source.exists():
        return 0, 0
    count = total = 0
    try:
        for directory, dirnames, filenames in os.walk(source, followlinks=False):
            current = Path(directory)
            safe_dirs = []
            for name in sorted(dirnames):
                path = current / name
                if path.is_symlink():
                    raise UnsafeGitConfig("symlinked Git refs are unsupported")
                safe_dirs.append(name)
            dirnames[:] = safe_dirs
            rel = current.relative_to(source)
            for name in sorted(filenames):
                count += 1
                if count > file_budget:
                    raise UnsafeGitConfig("Git refs exceed their file-count bound")
                total += _copy_regular(current / name, destination / rel / name,
                                       limit=byte_budget)
                if total > byte_budget:
                    raise UnsafeGitConfig("Git refs exceed their byte bound")
    except OSError as exc:
        raise UnsafeGitConfig("cannot copy Git refs safely") from exc
    return count, total


def _config_entries(path: Path, env: Mapping[str, str], timeout: float):
    if not path.exists():
        return []
    result = subprocess.run(
        ["git", "config", "--null", "--no-includes", "--file", str(path), "--list"],
        capture_output=True, timeout=min(timeout, 3), env=dict(env),
        stdin=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise UnsafeGitConfig("cannot read local Git config safely")
    entries = []
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"\n")
        if not separator:
            value = b""
        try:
            entries.append((key.decode("utf-8").lower(),
                            value.decode("utf-8")))
        except UnicodeError as exc:
            raise UnsafeGitConfig("local Git config is not valid UTF-8") from exc
    return entries


def _quote_config(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
        "\n", "\\n").replace("\t", "\\t") + '"'


def run_isolated_worktree_git(root: str | Path, args: Sequence[str], *,
                              timeout: float = 20,
                              input_data: bytes | None = None):
    """Run status/diff against a temporary Git dir with no target config.

    The temporary metadata copies bounded refs and the index, and reads Git
    objects through a read-only alternate. The target's config, includes,
    filter commands, fsmonitor, diff drivers and environment overrides are not
    present in the process that inspects the worktree.
    """
    env = safe_git_env()
    root_path = Path(root).resolve()
    git_dir_raw = _plumbing(root_path, ["rev-parse", "--absolute-git-dir"], env, timeout)
    common_raw = _plumbing(root_path,
                           ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                           env, timeout)
    git_dir = Path(git_dir_raw.decode("utf-8", errors="strict").strip()).resolve()
    common_dir = Path(common_raw.decode("utf-8", errors="strict").strip()).resolve()
    objects = _git_path(root_path, "objects", env, timeout)
    index = _git_path(root_path, "index", env, timeout)
    head = git_dir / "HEAD"
    config = common_dir / "config"
    entries = _config_entries(config, env, timeout)
    worktree_config = git_dir / "config.worktree"
    entries.extend(_config_entries(worktree_config, env, timeout))
    values: dict[str, list[str]] = {}
    for key, value in entries:
        values.setdefault(key, []).append(value)
    extensions = {key.removeprefix("extensions.") for key in values
                  if key.startswith("extensions.")}
    unsupported_extensions = extensions - {"objectformat", "worktreeconfig"}
    if unsupported_extensions:
        raise UnsafeGitConfig("repository uses unsupported Git extensions")
    if values.get("core.sparsecheckout", ["false"])[-1].lower() == "true" or \
            values.get("index.sparse", ["false"])[-1].lower() == "true":
        raise UnsafeGitConfig("sparse worktrees are unsupported for safe status/diff")
    if not head.is_file() or head.is_symlink():
        raise UnsafeGitConfig("cannot copy HEAD safely")
    object_format = values.get("extensions.objectformat", ["sha1"])[-1].lower()
    if object_format not in {"sha1", "sha256"}:
        raise UnsafeGitConfig("unsupported Git object format")
    try:
        head_bytes = head.read_bytes()
    except OSError as exc:
        raise UnsafeGitConfig("cannot read HEAD safely") from exc

    with tempfile.TemporaryDirectory(prefix="ozagia-safe-git-") as tmp:
        safe_dir = Path(tmp) / "repo.git"
        init_args = ["git", "init", "--bare", "--template=/dev/null"]
        if object_format == "sha256":
            init_args.append("--object-format=sha256")
        init_args.append(str(safe_dir))
        try:
            initialized = subprocess.run(
                init_args, capture_output=True, timeout=min(timeout, 5),
                env=dict(env), stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UnsafeGitConfig("cannot initialize isolated Git metadata") from exc
        if initialized.returncode != 0:
            raise UnsafeGitConfig("cannot initialize isolated Git metadata")
        (safe_dir / "HEAD").write_bytes(head_bytes)
        repo_version = "1" if object_format == "sha256" else "0"
        config_lines = ["[core]", f"\trepositoryformatversion = {repo_version}",
                        "\tbare = false", f"\tworktree = {_quote_config(str(root_path))}",
                        "\tfsmonitor = false"]
        if object_format == "sha256":
            config_lines.extend(("[extensions]", "\tobjectFormat = sha256"))
        for key in ("core.filemode", "core.ignorecase", "core.symlinks",
                    "core.precomposeunicode"):
            if key in values:
                config_lines.extend((f"[{key.split('.')[0]}]",
                                     f"\t{key.split('.', 1)[1]} = "
                                     f"{_quote_config(values[key][-1])}"))
        (safe_dir / "config").write_text("\n".join(config_lines) + "\n",
                                         encoding="utf-8")
        (safe_dir / "objects" / "info" / "alternates").write_text(
            str(objects.resolve()) + "\n", encoding="utf-8")
        refs_source = common_dir / "refs"
        _copy_tree(refs_source, safe_dir / "refs", file_budget=MAX_METADATA_FILES,
                   byte_budget=MAX_METADATA_BYTES)
        packed = common_dir / "packed-refs"
        if packed.exists():
            _copy_regular(packed, safe_dir / "packed-refs", limit=MAX_METADATA_BYTES)
        if index.exists():
            _copy_regular(index, safe_dir / "index", limit=MAX_INDEX_BYTES)
            shared = sorted(index.parent.glob("sharedindex.*"), key=str)
            if len(shared) > 1000:
                raise UnsafeGitConfig("too many split-index shared files")
            for item in shared:
                _copy_regular(item, safe_dir / item.name, limit=MAX_INDEX_BYTES)
        info_exclude = common_dir / "info" / "exclude"
        if info_exclude.exists():
            _copy_regular(info_exclude, safe_dir / "info" / "exclude",
                          limit=1024 * 1024)
        stash_log = common_dir / "logs" / "refs" / "stash"
        if stash_log.exists():
            _copy_regular(stash_log, safe_dir / "logs" / "refs" / "stash",
                          limit=MAX_METADATA_BYTES)
        # Validate the generated config before running any worktree conversion.
        generated = _config_entries(safe_dir / "config", env, timeout)
        if any(key.startswith(("filter.", "include.", "includeif."))
               for key, _value in generated):
            raise UnsafeGitConfig("isolated Git config unexpectedly contains filters")
        command = list(args)
        subcommand = git_subcommand(command)
        if subcommand == "diff":
            index_pos = command.index("diff")
            command[index_pos + 1:index_pos + 1] = ["--no-ext-diff", "--no-textconv"]
        isolated_env = dict(env)
        isolated_env["GIT_DIR"] = str(safe_dir)
        isolated_env["GIT_WORK_TREE"] = str(root_path)
        try:
            return subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "--git-dir", str(safe_dir),
                 "--work-tree", str(root_path), *command],
                input=input_data, capture_output=True, timeout=timeout,
                env=isolated_env,
                stdin=(None if input_data is not None else subprocess.DEVNULL),
                check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UnsafeGitConfig("isolated Git status/diff could not run") from exc


def run_git(root: str | Path, args: Sequence[str], *, timeout: float = 20,
            input_data: bytes | None = None):
    """Run Git with sanitized environment and isolated worktree conversions."""
    command = git_subcommand(args)
    if command in {"status", "diff", "hash-object"}:
        return run_isolated_worktree_git(root, args, timeout=timeout,
                                         input_data=input_data)
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            input=input_data, capture_output=True, timeout=timeout,
            env=safe_git_env(), stdin=(None if input_data is not None
                                      else subprocess.DEVNULL),
            check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UnsafeGitConfig("Git command could not run safely") from exc
