"""ozagia.core.fingerprint — what the working tree looked like when a
pytest run started, and whether a file has changed since.

`tree_fingerprint(root)` is taken by the daemon just before it runs a
captured pytest (`ozagia.daemon.server`), and stored with the run in the
test record (`ozagia.core.working_set`):

    {"head": <commit sha> | None,       # HEAD at the start of the run
     "status": <sha256>,                # of `git status --porcelain` and the
                                        # mtime_ns/size of each dirty file
     "dirty": {path: <blob sha> | None},  # each dirty file's content (git
                                        # blob id; None: deleted)
     "unhashed": [path, ...],           # dirty files over the hashing caps
                                        # (content unknown)
     "overflow": bool}                  # too many dirty files even to name:
                                        # every file outside `dirty` is of
                                        # unknown content

Paths are root-relative POSIX paths; the state dir is left out. A file's
content at the start of the run is `dirty[path]` when it was dirty, else
its blob in `head` (absent: the file did not exist). At check time
(`ozagia.core.goals`), `changed_since(root, fp, paths)` hashes the current
content of each path with `git hash-object` and compares: no mtime enters
the decision, so backdating a file (`touch -d`, `os.utime`) does not hide
an edit. The status digest is only a cheap "something moved" signal for
the evidence.

The cost on the daemon's hot path is three git commands (`rev-parse`,
`status`, `hash-object` over the dirty files) per captured pytest run,
nothing per edit. Outside a git repository there is no fingerprint (None).

Standard library only.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

GIT_TIMEOUT_S = 5
# Dirty files hashed per run; beyond either cap the fingerprint is marked
# `overflow` (files outside `dirty` are then of unknown content).
MAX_DIRTY = 500
MAX_DIRTY_BYTES = 64 * 1024 * 1024
# Dirty files beyond the caps are kept by name (`unhashed`: of unknown
# content); beyond this many as well, `overflow` (every file not in
# `dirty` is then of unknown content).
MAX_UNHASHED = 2000
MAX_PATH_CHARS = 1000
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _git(root: Path, *args: str, stdin: bytes | None = None,
         timeout: float = GIT_TIMEOUT_S) -> bytes | None:
    # `git status` may opportunistically refresh the target index. Reviews and
    # fingerprints only observe repository state, so prevent Git from taking
    # its optional index lock and writing that refresh back to the checkout.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], input=stdin,
                              capture_output=True, timeout=timeout, check=False,
                              env=env,
                              **({} if stdin is not None
                                 else {"stdin": subprocess.DEVNULL}))
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _prefix(root: Path) -> str | None:
    """The root's path inside its git work tree (`""` at the top, `"a/b/"`
    below), or None outside a git repository."""
    out = _git(root, "rev-parse", "--show-prefix")
    if out is None:
        return None
    return out.decode("utf-8", "surrogateescape").strip()


def _exclude(root: Path) -> list[str]:
    """Pathspecs that leave out the state dir."""
    from ozagia.config import state_rel
    rel = state_rel(root)
    return [".", f":(exclude){rel}"] if rel else ["."]


def _strip(path: str, prefix: str) -> str | None:
    if not path.startswith(prefix):
        return None
    rel = path[len(prefix):]
    return rel if rel and len(rel) <= MAX_PATH_CHARS else None


def _status(root: Path, prefix: str) -> tuple[bytes, list[tuple[str, str]]] | None:
    """`git status` output and its (code, root-relative path) entries."""
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
               "--no-renames", "--", *_exclude(root))
    if raw is None:
        return None
    out: list[tuple[str, str]] = []
    for entry in raw.split(b"\0"):
        if len(entry) < 4:
            continue
        code = entry[:2].decode("ascii", "replace")
        rel = _strip(entry[3:].decode("utf-8", "surrogateescape"), prefix)
        if rel is not None:
            out.append((code, rel))
    return raw, out


def blob_ids(root: Path, paths: Iterable[str]) -> dict[str, str | None]:
    """The git blob id of each path's current content (with the repo's
    filters, as `git add` would store it); None for a path that is not a
    regular file."""
    root = Path(root)
    out: dict[str, str | None] = {}
    files: list[str] = []
    for rel in dict.fromkeys(paths):
        p = root / rel
        try:
            ok = p.is_file() and not p.is_symlink()
        except OSError:
            ok = False
        if ok:
            files.append(rel)
        else:
            out[rel] = None
    if not files:
        return out
    stdin = "".join(str(root / rel) + "\n" for rel in files).encode(
        "utf-8", "surrogateescape")
    raw = _git(root, "hash-object", "--stdin-paths", stdin=stdin,
               timeout=max(GIT_TIMEOUT_S, len(files) / 200))
    ids = raw.decode("ascii", "replace").split() if raw is not None else []
    if len(ids) == len(files):
        out.update(zip(files, ids))
        return out
    # A file vanished mid-batch (or git failed): hash each file in Python.
    for rel in files:
        out[rel] = _py_blob(root / rel)
    return out


def _py_blob(p: Path) -> str | None:
    try:
        data = p.read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_fingerprint(root: str | Path) -> dict[str, Any] | None:
    """The working tree's fingerprint now (see the module docstring), or
    None outside a git repository or when git fails."""
    root = Path(root).resolve()
    prefix = _prefix(root)
    if prefix is None:
        return None
    head_raw = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    head = head_raw.decode("ascii", "replace").strip() if head_raw else None
    st = _status(root, prefix)
    if st is None:
        return None
    raw, entries = st
    digest = hashlib.sha256(raw)
    hashed: list[str] = []
    deleted: list[str] = []
    unhashed: list[str] = []
    total = 0
    for _code, rel in entries:
        try:
            s = os.lstat(root / rel)
        except OSError:
            deleted.append(rel)
            digest.update(f"\0{rel}\0-".encode("utf-8", "surrogateescape"))
            continue
        digest.update(f"\0{rel}\0{s.st_mtime_ns}:{s.st_size}".encode(
            "utf-8", "surrogateescape"))
        if len(hashed) >= MAX_DIRTY or total + s.st_size > MAX_DIRTY_BYTES:
            unhashed.append(rel)
            continue
        hashed.append(rel)
        total += s.st_size
    unhashed.extend(deleted[MAX_DIRTY:])
    dirty: dict[str, str | None] = {rel: None for rel in deleted[:MAX_DIRTY]}
    dirty.update(blob_ids(root, hashed))
    return {"head": head if head and _SHA_RE.match(head) else None,
            "status": digest.hexdigest(), "dirty": dirty,
            "unhashed": unhashed[:MAX_UNHASHED],
            "overflow": len(unhashed) > MAX_UNHASHED}


def clean_fingerprint(fp: Any) -> dict[str, Any] | None:
    """A stored fingerprint checked field by field (None when malformed)."""
    if not isinstance(fp, dict):
        return None
    head = fp.get("head")
    status = fp.get("status")
    dirty = fp.get("dirty")
    if head is not None and not (isinstance(head, str) and _SHA_RE.match(head)):
        return None
    if not isinstance(status, str) or not isinstance(dirty, dict):
        return None
    out: dict[str, str | None] = {}
    overflow = fp.get("overflow") is True
    for rel, blob in dirty.items():
        if len(out) >= MAX_DIRTY:
            overflow = True
            break
        if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_CHARS:
            overflow = True
            continue
        if blob is not None and not (isinstance(blob, str) and _SHA_RE.match(blob)):
            overflow = True
            continue
        out[rel] = blob
    raw_un = fp.get("unhashed")
    unhashed = [p for p in raw_un if isinstance(p, str) and p
                and len(p) <= MAX_PATH_CHARS] if isinstance(raw_un, list) else []
    if len(unhashed) > MAX_UNHASHED or (isinstance(raw_un, list)
                                        and len(unhashed) != len(raw_un)):
        overflow = True
    return {"head": head, "status": status[:64], "dirty": out,
            "unhashed": unhashed[:MAX_UNHASHED], "overflow": overflow}


class TreeView:
    """Check-time access to the repository: blobs of a commit's tree, the
    tracked and untracked paths now, the current content of files, and
    the status digest now. Each git call runs at most once."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.prefix = _prefix(self.root)
        self._trees: dict[str, dict[str, str] | None] = {}
        self._paths: set[str] | None = None
        self._blobs: dict[str, str | None] = {}
        self._status: str | None = None

    @property
    def is_git(self) -> bool:
        return self.prefix is not None

    def tree(self, commit: str | None) -> dict[str, str] | None:
        """{root-relative path: blob id} of `commit` ({} for None: no
        commit yet), or None when git cannot read it."""
        if commit is None:
            return {}
        if commit not in self._trees:
            raw = _git(self.root, "ls-tree", "-r", "-z", "--full-tree", commit)
            if raw is None or self.prefix is None:
                self._trees[commit] = None
            else:
                tree: dict[str, str] = {}
                for entry in raw.split(b"\0"):
                    meta, _, path = entry.partition(b"\t")
                    parts = meta.split()
                    if len(parts) != 3 or parts[1] != b"blob":
                        continue
                    rel = _strip(path.decode("utf-8", "surrogateescape"), self.prefix)
                    if rel is not None:
                        tree[rel] = parts[2].decode("ascii", "replace")
                self._trees[commit] = tree
        return self._trees[commit]

    def paths(self) -> set[str]:
        """Tracked and untracked (not ignored) paths now, state dir left
        out."""
        if self._paths is None:
            self._paths = set()
            if self.prefix is not None:
                raw = _git(self.root, "ls-files", "-z", "--full-name", "-c", "-o",
                           "--exclude-standard", "--", *_exclude(self.root))
                for entry in (raw or b"").split(b"\0"):
                    rel = _strip(entry.decode("utf-8", "surrogateescape"), self.prefix)
                    if rel is not None:
                        self._paths.add(rel)
        return self._paths

    def blobs(self, paths: Iterable[str]) -> dict[str, str | None]:
        want = [p for p in dict.fromkeys(paths) if p not in self._blobs]
        if want:
            self._blobs.update(blob_ids(self.root, want))
        return {p: self._blobs.get(p) for p in paths}

    def status_digest(self) -> str | None:
        """The fingerprint's `status` field as it would be now."""
        if self._status is None:
            fp = tree_fingerprint(self.root)
            self._status = fp["status"] if fp else ""
        return self._status or None


def changed_since(view: TreeView, fp: dict[str, Any], paths: Iterable[str],
                  *, skip_ignored: bool = False
                  ) -> tuple[list[str], str | None]:
    """The paths whose current content differs from their content when
    `fp` was taken, and a reason when that cannot be told for some path
    (then the caller treats the evidence as stale). With `skip_ignored`,
    paths git ignores now that were neither in the commit nor dirty when
    `fp` was taken are not compared (their content was never recorded)."""
    tree = view.tree(fp.get("head"))
    if tree is None:
        return [], f"cannot read commit {fp.get('head')} of the run"
    dirty = fp.get("dirty") or {}
    unhashed = set(fp.get("unhashed") or [])
    paths = list(dict.fromkeys(paths))
    now = view.blobs(paths)
    visible = view.paths()
    changed: list[str] = []
    for rel in paths:
        if rel in dirty:
            then = dirty[rel]
        elif rel in unhashed or fp.get("overflow"):
            return [rel], (f"the run's fingerprint has no content hash for {rel} "
                           "(too many dirty files when it ran)")
        elif skip_ignored and rel not in tree and rel not in visible:
            # Git-ignored now and neither committed nor dirty then: the
            # fingerprint never covers ignored files, so their content is
            # not compared (a file untracked and ignored after a review).
            # Only review freshness asks for this; test freshness keeps
            # such files fail-closed (always changed).
            continue
        else:
            then = tree.get(rel)
        if now[rel] != then:
            changed.append(rel)
    return changed, None


__all__ = ["MAX_DIRTY", "TreeView", "blob_ids", "changed_since",
           "clean_fingerprint", "tree_fingerprint"]
