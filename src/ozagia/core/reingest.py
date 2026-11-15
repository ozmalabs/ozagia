"""Incremental reingest: bring a loaded snapshot up to date for a few files.

`DevMemory.reingest(paths)` re-parses only the given repository-relative
files (modified, created or deleted), updates the in-memory graph and its
indexes, rewrites the snapshot and returns a `GraphDelta`. The result is the
graph a full `build_index` of the current tree would produce (compared by
node key, type and attrs and by edge (source key, type, target key)), with
the exceptions listed under "Not recomputed" below.

Ownership. A source, test or doc file F owns the Symbol, Test and Doc nodes
whose key names F (`sym:F::..`, `test:F::..`, `doc:F`) and the built-in
per-file out-edges of those nodes and of its File node (defines, defined_in,
contains_symbol, imports, has_test, tested_by_file, tests_module,
mentions_file, documents to a File). F is parsed again into a scratch
builder (`memory.FileIndexer`, the code `build_index` runs per file) and the
result is merged by key: a node whose key survives keeps its id and gets the
new attrs; nodes with keys that are gone are removed with all their edges;
new keys get fresh ids from `next_id`. Removed ids are not reused, except
that an adapter node removed and re-created by the same reingest keeps its id.

References into F. The snapshot's `files` table records, per indexed file,
every reference to another file, resolved or not (`["imports", module,
level]`, `["rs_mod", rel]`, `["mentions", rel]`, `["documents", rel]`; see
`memory.FileIndexer`). A file whose references could resolve to a reingested
file (by the import candidate paths) has its reference edges resolved again
from that table, without being parsed.

File nodes. After the update a File node exists while something gives it a
reason: F is indexed (a source, test or doc file that exists), a built-in
reference edge points at it, an adapter used it, or a commit (`changes`) or
the failure baseline (`in_file`) names it. Its attrs are those of a fresh
build: `indexed` is 0 only when history or failures are the only reason. So
a deleted file that is still in the git history or the failure baseline
keeps an unindexed File node; otherwise its File node goes.

Adapters. What an adapter indexer derived from a file is unknown, and the
Ocelot seed indexer takes seconds over all seeds. A reingest runs the
adapter indexers again only when a reingested file is one an adapter node is
`defined_in`, or one an adapter passed to `ctx.file_node`: then all adapter
nodes and adapter edges are removed and the indexers run over the current
sources, tests and docs (all read again). Otherwise adapters are skipped:
adapter edges of nodes whose keys survive are kept, adapter edges of removed
nodes go, and new nodes get none until a build or an adapter pass. Adapters
may later declare per-file indexing so that a reingest can call them for
the reingested files only.

Not recomputed. Git history (`changes`, `head`) and the failure baseline
(`has_failure`, `in_file`) are left as built; edges to removed nodes go
away, and a failure in a reingested test file is linked again
(`refers_to`). Repo `generated_at` keeps the build time. The Repo counts
are updated.

Staleness. The Repo `tree_digest` is set to the current digest only when
`pending_changes` finds nothing left to reingest and `unhandled_changes` is
empty: every tracked path git reports as changed is one reingest handles
(an indexed file or an adapter-owned file). Tracked paths of the other kind
that were already changed when the digest was taken are stamped in the
snapshot's `other_stamps`; only a change since then counts. So
`staleness()` reports `stale: False` once every changed file has been
reingested (with HEAD unchanged), and keeps reporting stale after a change
to a tracked file reingest cannot take in (the failure baseline, a tracked
file only a build reads) until a build. Untracked files a build does not
index do not hold the digest back; a new, untracked file that only an
adapter would read (a new seed manifest) still needs a build.

Adapter-owned files that are not indexed (a seed manifest) are stamped
(size, mtime_ns, sha256; the snapshot's `adapter.stamps`) at the build and
after each adapter pass; `pending_changes` reports one that changed, so a
`refresh` reingests it and runs the adapter pass. Snapshots without stamps
(built before they existed) report such a file only when git lists it.

Version 1 snapshots have no `files` table; reingest refuses them.
"""
from __future__ import annotations

import copy
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from ozagia.core import memory as M
from ozagia.core.delta import GraphDelta
from ozagia.core.keys import file_key, format_key, key_file, test_key

_OWNED = ("sym", "test", "doc")
_CORE = frozenset({"file", "sym", "test", "doc", "commit", "failure", "repo"})
_FILE_SIDE = frozenset({"file", "sym", "test", "doc"})
# Built-in per-file out-edges of a file's File, Symbol, Test and Doc nodes.
FILE_EDGES = frozenset({"defines", "defined_in", "contains_symbol", "imports",
                        "has_test", "tested_by_file", "tests_module",
                        "mentions_file", "documents"})
REF_EDGES = frozenset({"imports", "tests_module", "mentions_file", "documents"})
HISTORY_EDGES = frozenset({"changes", "in_file"})
_REPO_VOLATILE = frozenset({"generated_at", "tree_digest", "tree_digest_error"})


def _prefix(key: str | None) -> str:
    return key.partition(":")[0] if key else ""


class _State:
    """Per-DevMemory indexes reingest needs: owned nodes per file and the
    files whose references name a path."""

    def __init__(self, mem: Any) -> None:
        self.owned: dict[str, set[int]] = {}
        for nid, key in mem._id_to_key.items():
            if _prefix(key) in _OWNED:
                rel = key_file(key)
                if rel:
                    self.owned.setdefault(rel, set()).add(nid)
        self.cands: dict[str, set[str]] = {}
        self._entry_cands: dict[str, set[str]] = {}
        for rel, entry in (mem._files_table or {}).items():
            self.set_entry(rel, entry)

    def set_entry(self, rel: str, entry: dict[str, Any] | None) -> None:
        for c in self._entry_cands.pop(rel, ()):
            users = self.cands.get(c)
            if users is not None:
                users.discard(rel)
                if not users:
                    del self.cands[c]
        if entry is None:
            return
        cs: set[str] = set()
        for ref in entry.get("refs") or ():
            if ref[0] == "imports":
                try:
                    cs.update(M._import_candidates(rel, ref[1], ref[2]))
                except ValueError:
                    continue
            else:
                cs.add(str(ref[1]))
        self._entry_cands[rel] = cs
        for c in cs:
            self.cands.setdefault(c, set()).add(rel)


class _Txn:
    """Graph edits on a DevMemory that keep its indexes current and record
    the before-state of every node and edge they touch."""

    def __init__(self, mem: Any, st: _State, root: Path) -> None:
        self.mem = mem
        self.st = st
        self.root = root
        self.repo = mem.node_by_key("repo:")
        self.node_before: dict[str, tuple[str, dict[str, Any]] | None] = {}
        self.edge_before: dict[tuple[str, str, str], bool] = {}
        self.removed: set[int] = set()
        self.out_sets: dict[int, set[tuple[str, int]]] = {}
        self.reuse: dict[str, int] = {}
        self.touched: set[str] = set()
        self.watched: set[int] = set()
        self.adapter_core = {tuple(e) for e in
                             mem._adapter_info.get("edges") or ()}

    # -- recording ---------------------------------------------------------- #
    def key(self, nid: int) -> str | None:
        return self.mem._id_to_key.get(nid)

    def _record_node(self, key: str | None) -> None:
        if key is None or key in self.node_before:
            return
        nid = self.mem._key_to_id.get(key)
        self.node_before[key] = None if nid is None else (
            self.mem.nodes[nid]["type"],
            copy.deepcopy(self.mem.nodes[nid]["attrs"]))

    def _record_edge(self, s: int, t: str, g: int, existed: bool) -> None:
        sk, gk = self.key(s), self.key(g)
        if sk is not None and gk is not None:
            self.edge_before.setdefault((sk, t, gk), existed)

    # -- nodes -------------------------------------------------------------- #
    def add_node(self, type_name: str, attrs: dict[str, Any], key: str) -> int:
        mem = self.mem
        nid = mem._key_to_id.get(key)
        if nid is not None:
            return nid
        self._record_node(key)
        nid = self.reuse.pop(key, None)
        if nid is None:
            nid = mem._next_id
            mem._next_id += 1
        mem.nodes[nid] = {"id": nid, "type": type_name,
                          "attrs": dict(attrs), "key": key}
        mem._key_to_id[key] = nid
        mem._id_to_key[nid] = key
        if _prefix(key) in _OWNED:
            self.st.owned.setdefault(key_file(key) or "", set()).add(nid)
        mem._index_add(nid)
        return nid

    def remove_node(self, nid: int) -> None:
        mem = self.mem
        if nid not in mem.nodes:
            return
        self._unwatch(nid)
        key = self.key(nid)
        self._record_node(key)
        for e in list(mem._out.get(nid, ())):
            self.remove_edge(e)
        for e in list(mem._in.get(nid, ())):
            self.remove_edge(e)
        mem._index_remove(nid)
        del mem.nodes[nid]
        mem._out.pop(nid, None)
        mem._in.pop(nid, None)
        self.out_sets.pop(nid, None)
        if key is not None:
            mem._key_to_id.pop(key, None)
            mem._id_to_key.pop(nid, None)
            if _prefix(key) in _OWNED:
                self.st.owned.get(key_file(key) or "", set()).discard(nid)

    def set_attrs(self, nid: int, attrs: dict[str, Any]) -> bool:
        mem = self.mem
        self._unwatch(nid)
        if mem.nodes[nid]["attrs"] == attrs:
            return False
        self._record_node(self.key(nid))
        mem._index_remove(nid)
        mem.nodes[nid]["attrs"] = dict(attrs)
        mem._index_add(nid)
        return True

    def watch(self, nid: int) -> None:
        """`nid`'s attrs are about to be changed in place (by an adapter)."""
        if nid in self.watched or nid not in self.mem.nodes:
            return
        self._record_node(self.key(nid))
        self.mem._index_remove(nid)
        self.watched.add(nid)

    def _unwatch(self, nid: int) -> None:
        if nid in self.watched:
            self.watched.discard(nid)
            self.mem._index_add(nid)

    def ensure_file(self, rel: str, attrs: dict[str, Any] | None = None) -> int:
        """The File node for `rel`, created (with a Repo `contains` edge) if
        missing. Its attrs are settled later (`_settle_file`)."""
        self.touched.add(rel)
        key = file_key(rel)
        nid = self.mem._key_to_id.get(key)
        if nid is None:
            nid = self.add_node("File", attrs if attrs is not None else
                                M._file_attrs(self.root, rel), key)
            self.add_edge(self.repo, "contains", nid)
        return nid

    # -- edges -------------------------------------------------------------- #
    def out_set(self, s: int) -> set[tuple[str, int]]:
        got = self.out_sets.get(s)
        if got is None:
            got = {(e["type"], int(e["tgt"])) for e in self.mem._out.get(s, ())}
            self.out_sets[s] = got
        return got

    def add_edge(self, s: int | None, t: str, g: int | None,
                 attrs: dict[str, Any] | None = None) -> bool:
        if s is None or g is None:
            return False
        s, g = int(s), int(g)
        if (t, g) in self.out_set(s):
            return False
        mem = self.mem
        e = {"src": s, "type": t, "tgt": g, "attrs": dict(attrs or {})}
        mem.edges.append(e)
        mem._out.setdefault(s, []).append(e)
        mem._in.setdefault(g, []).append(e)
        self.out_sets[s].add((t, g))
        self._record_edge(s, t, g, False)
        return True

    def remove_edge(self, e: dict[str, Any]) -> None:
        if id(e) in self.removed:
            return
        s, g = int(e["src"]), int(e["tgt"])
        self._record_edge(s, e["type"], g, True)
        gk = self.key(g)
        if _prefix(gk) == "file":
            # The target may have lost its last reason to exist.
            self.touched.add(key_file(gk) or "")
        for lst in (self.mem._out.get(s), self.mem._in.get(g)):
            if lst is not None:
                for i, x in enumerate(lst):
                    if x is e:
                        del lst[i]
                        break
        self.removed.add(id(e))
        if s in self.out_sets:
            self.out_sets[s].discard((e["type"], g))

    def is_file_edge(self, e: dict[str, Any]) -> bool:
        """A built-in per-file edge (not one an adapter added)."""
        if e["type"] not in FILE_EDGES:
            return False
        sk, gk = self.key(int(e["src"])), self.key(int(e["tgt"]))
        return (_prefix(gk) in _FILE_SIDE
                and (sk, e["type"], gk) not in self.adapter_core)

    # -- finish ------------------------------------------------------------- #
    def flush(self) -> None:
        for nid in list(self.watched):
            self._unwatch(nid)
        if self.removed:
            removed = self.removed
            self.mem.edges[:] = [e for e in self.mem.edges
                                 if id(e) not in removed]
            self.removed = set()

    def delta(self, files: list[str]) -> GraphDelta:
        mem = self.mem
        d = GraphDelta(files=list(files))
        for key, before in self.node_before.items():
            nid = mem._key_to_id.get(key)
            if before is None:
                if nid is not None:
                    d.added.append(key)
                continue
            if nid is None:
                d.removed.append(key)
                continue
            after = mem.nodes[nid]
            a_attrs, b_attrs = after["attrs"], before[1]
            if _prefix(key) == "repo":
                a_attrs = {k: v for k, v in a_attrs.items()
                           if k not in _REPO_VOLATILE}
                b_attrs = {k: v for k, v in b_attrs.items()
                           if k not in _REPO_VOLATILE}
            if after["type"] != before[0] or a_attrs != b_attrs:
                d.changed.append(key)
        for (sk, t, gk), existed in self.edge_before.items():
            s, g = mem._key_to_id.get(sk), mem._key_to_id.get(gk)
            now = s is not None and g is not None and (t, g) in self.out_set(s)
            if existed and not now:
                d.edges_removed.append((sk, t, gk))
            elif now and not existed:
                d.edges_added.append((sk, t, gk))
        for lst in (d.added, d.removed, d.changed, d.edges_added,
                    d.edges_removed):
            lst.sort()
        return d


def _state(mem: Any) -> _State:
    if mem._reingest_state is None:
        mem._reingest_state = _State(mem)
    return mem._reingest_state


def _normalize(root: Path, paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        s = str(raw).strip()
        if not s:
            continue
        p = Path(s)
        if p.is_absolute():
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                try:
                    rel = p.resolve().relative_to(root).as_posix()
                except (OSError, ValueError):
                    raise ValueError(f"{raw}: outside the repository {root}") from None
        else:
            rel = p.as_posix()
        if rel in ("", ".") or rel == ".." or rel.startswith("../"):
            raise ValueError(f"{raw}: not a file in the repository")
        if rel not in out:
            out.append(rel)
    return out


def _role(root: Path, rel: str, opts: dict[str, Any]) -> str | None:
    return M.file_role(root, rel,
                       include_docs=str(opts.get("include_docs", "features")),
                       include_scripts=bool(opts.get("include_scripts", True)))


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def _parse(root: Path, rel: str, opts: dict[str, Any]
           ) -> tuple[M.Builder, dict[str, Any]] | None:
    """The scratch build of `rel` alone, or None when it is not indexed."""
    role = _role(root, rel, opts)
    if role is None or not M._walked(root, rel):
        return None
    b = M.Builder()
    repo = b.node("Repo", {}, key=("repo",))
    ix = M.FileIndexer(b, root, repo)
    ix.index(role, root / rel, rel)
    return b, ix.table[rel]


def _apply_fragment(txn: _Txn, rel: str,
                    frag: tuple[M.Builder, dict[str, Any]] | None,
                    walked_attrs: dict[str, dict[str, Any]],
                    test_order: dict[str, list[tuple[str, int]]]) -> None:
    mem, st = txn.mem, txn.st
    table = mem._files_table
    txn.touched.add(rel)
    # Path references give their target a File node even without an edge
    # (a test file without tests), so their targets are settled again.
    for e in (table.get(rel), frag[1] if frag else None):
        for ref in (e or {}).get("refs") or ():
            if ref[0] != "imports":
                txn.touched.add(str(ref[1]))
    old_owned = set(st.owned.get(rel, ()))
    if frag is None:
        for nid in old_owned:
            txn.remove_node(nid)
        fid = mem.node_by_key(file_key(rel))
        if fid is not None and rel in table:
            # A file that is not indexed any more has no per-file edges, and
            # no adapter edges derived from its text. `has_failure` belongs
            # to the failure baseline. (A file that never was indexed, such
            # as a seed manifest, keeps its edges: adapters own them.)
            for e in list(mem._out.get(fid, ())):
                if e["type"] == "has_failure":
                    continue
                if txn.is_file_edge(e):
                    txn.touched.add(key_file(txn.key(int(e["tgt"])) or "") or "")
                txn.remove_edge(e)
        if rel in table:
            del table[rel]
        st.set_entry(rel, None)
        return

    b, entry = frag
    old_entry = table.get(rel)
    if old_entry and old_entry.get("ctx") and not entry.get("ctx"):
        # The file no longer parses, so adapters would not see it: drop the
        # adapter edges of its File node, as a build would not make them.
        fid = mem.node_by_key(file_key(rel))
        for e in list(mem._out.get(fid, ()) if fid is not None else ()):
            if e["type"] != "has_failure" and not txn.is_file_edge(e):
                txn.remove_edge(e)
    idmap: dict[int, int] = {}
    scratch_repo = None
    new_owned: dict[str, dict[str, Any]] = {}
    for n in b.nodes:
        key = n["key"]
        kind = _prefix(key)
        if kind == "repo":
            scratch_repo = n["id"]
            idmap[n["id"]] = txn.repo
        elif kind == "file":
            frel = key_file(key) or ""
            if frel == rel:
                walked_attrs[rel] = n["attrs"]
            idmap[n["id"]] = txn.ensure_file(frel, n["attrs"])
        else:
            new_owned[key] = n
    for nid in old_owned:
        if txn.key(nid) not in new_owned:
            txn.remove_node(nid)
    for key, n in new_owned.items():
        nid = mem.node_by_key(key)
        if nid is None:
            nid = txn.add_node(n["type"], n["attrs"], key)
        else:
            txn.set_attrs(nid, n["attrs"])
        idmap[n["id"]] = nid
    test_order[rel] = [(str(n["attrs"].get("qualname", "")), idmap[n["id"]])
                       for n in b.nodes if _prefix(n["key"]) == "test"]

    fid = mem.node_by_key(file_key(rel))
    srcs = {fid} | set(st.owned.get(rel, ()))
    old_edges = {(s, e["type"], int(e["tgt"])): e
                 for s in srcs for e in mem._out.get(s, ())
                 if txn.is_file_edge(e)}
    new_edges = set()
    for e in b.edges:
        if e["src"] == scratch_repo and e["type"] == "contains":
            continue
        new_edges.add((idmap[e["src"]], e["type"], idmap[e["tgt"]]))
    for k, e in old_edges.items():
        if k not in new_edges:
            txn.touched.add(key_file(txn.key(k[2]) or "") or "")
            txn.remove_edge(e)
    for s, t, g in new_edges:
        if (s, t, g) not in old_edges:
            txn.add_edge(s, t, g)
    table[rel] = entry
    st.set_entry(rel, entry)


def _rederive(txn: _Txn, rel: str) -> None:
    """Resolve the references of unchanged file `rel` again."""
    mem, root = txn.mem, txn.root
    entry = mem._files_table.get(rel)
    fid = mem.node_by_key(file_key(rel))
    if entry is None or fid is None:
        return
    role = entry.get("role")
    owned = txn.st.owned.get(rel, set())
    tests = [n for n in owned if _prefix(txn.key(n)) == "test"]
    docs = [n for n in owned if _prefix(txn.key(n)) == "doc"]
    new: set[tuple[int, str, int]] = set()
    for ref in entry.get("refs") or ():
        kind = ref[0]
        if kind == "imports":
            try:
                target = M._resolve_candidates(
                    root, M._import_candidates(rel, ref[1], ref[2]))
            except ValueError:
                target = None
        else:
            target = ref[1] if (root / ref[1]).is_file() else None
        if not target:
            continue
        g = txn.ensure_file(target)
        if kind in ("imports", "rs_mod"):
            new.add((fid, "imports", g))
        if kind == "imports" and role == "test":
            new.update((t, "tests_module", g) for t in tests)
        if kind == "mentions":
            new.update((t, "mentions_file", g) for t in tests)
        if kind == "documents":
            new.update((d, "documents", g) for d in docs)
    wanted = ([(fid, ("imports",))] + [(t, ("tests_module", "mentions_file"))
                                       for t in tests]
              + [(d, ("documents",)) for d in docs])
    old: dict[tuple[int, str, int], dict[str, Any]] = {}
    for s, types in wanted:
        for e in mem._out.get(s, ()):
            g = int(e["tgt"])
            if (e["type"] in types and txn.is_file_edge(e)
                    and _prefix(txn.key(g)) == "file"):
                old[(s, e["type"], g)] = e
    for k, e in old.items():
        if k not in new:
            txn.touched.add(key_file(txn.key(k[2]) or "") or "")
            txn.remove_edge(e)
    for s, t, g in new:
        if (s, t, g) not in old:
            txn.add_edge(s, t, g)


def _relink_failures(txn: _Txn, rel: str,
                     order: list[tuple[str, int]]) -> None:
    """`refers_to` for failures in reingested test file `rel`, as the build
    resolves it: the exact `test:<rel>::<name>` key, else the first test of
    the file (in file order) whose qualname ends with the name."""
    mem = txn.mem
    fid = mem.node_by_key(file_key(rel))
    if fid is None:
        return
    for e in list(mem._out.get(fid, ())):
        if e["type"] != "has_failure":
            continue
        fail = int(e["tgt"])
        attrs = mem.nodes[fail]["attrs"]
        if attrs.get("file") != rel:
            continue
        for fe in list(mem._out.get(fail, ())):
            if fe["type"] == "refers_to":
                txn.remove_edge(fe)
        node_id = str(attrs.get("node") or "")
        name = node_id.rsplit("::", 1)[-1] if "::" in node_id else ""
        if not name:
            continue
        tid = mem.node_by_key(test_key(rel, name))
        if tid is None:
            tid = next((nid for q, nid in order if q.endswith(name)), None)
        if tid is not None:
            txn.add_edge(fail, "refers_to", tid)


def _adapter_files(mem: Any) -> set[str]:
    return set(mem._adapter_info.get("files") or ())


def _adapter_known(mem: Any) -> set[str]:
    """Adapter files, and stamped former ones (deleted before the last
    adapter pass): changes to either are handled by an adapter pass."""
    return _adapter_files(mem) | set(mem._adapter_info.get("stamps") or ())


def _needs_adapter_pass(txn: _Txn, files: list[str]) -> bool:
    mem = txn.mem
    afiles = _adapter_files(mem)
    for rel in files:
        if rel in afiles:
            return True
        fid = mem.node_by_key(file_key(rel))
        if fid is not None and any(
                e["type"] == "defined_in"
                and _prefix(txn.key(int(e["src"]))) not in _CORE
                for e in mem._in.get(fid, ())):
            return True
    return False


class _AdapterBuilder:
    """The `ctx.builder` adapters see during a reingest adapter pass: the
    `Builder` methods adapters use, over the loaded graph."""

    def __init__(self, txn: _Txn) -> None:
        self.txn = txn
        self.mem = txn.mem
        self.core_edges: list[list[str]] = []
        self.unkeyed = 0
        self._counts: dict[tuple[str, str], int] = {}

    def node(self, type_name: str, attrs: dict[str, Any] | None = None,
             *, key: tuple | None = None) -> int:
        if key is None:
            key = M.default_node_key(self._counts, type_name, attrs)
            self.unkeyed += 1
        skey = format_key(key)
        nid = self.mem.node_by_key(skey)
        if nid is not None:
            return nid
        return self.txn.add_node(type_name, dict(attrs or {}), skey)

    def edge(self, src: int | None, type_name: str, tgt: int | None,
             attrs: dict[str, Any] | None = None) -> None:
        if self.txn.add_edge(src, type_name, tgt, attrs):
            sk, gk = self.txn.key(int(src)), self.txn.key(int(tgt))
            if _prefix(sk) in _CORE and _prefix(gk) in _CORE:
                self.core_edges.append([sk, type_name, gk])

    def find(self, key: tuple) -> int | None:
        return self.mem.node_by_key(format_key(key))

    def attrs(self, nid: int) -> dict[str, Any]:
        self.txn.watch(int(nid))
        return self.mem.nodes[int(nid)]["attrs"]

    def count(self, type_name: str) -> int:
        return sum(1 for n in self.mem.nodes.values() if n["type"] == type_name)


def _adapter_pass(txn: _Txn, indexers: list[Any], opts: dict[str, Any]) -> None:
    """Remove every adapter node and edge and run the indexers again over
    the current sources, tests and docs."""
    from ozagia.adapters import (IndexContext, IndexedDoc, IndexedSource,
                                 IndexedTests)
    mem, root = txn.mem, txn.root
    info = mem._adapter_info
    adapter_nodes = [nid for nid, k in mem._id_to_key.items()
                     if _prefix(k) not in _CORE]
    for nid in adapter_nodes:
        txn.reuse[mem._id_to_key[nid]] = nid
    for nid in adapter_nodes:
        txn.remove_node(nid)
    for sk, t, gk in info.get("edges") or ():
        s, g = mem.node_by_key(sk), mem.node_by_key(gk)
        for e in list(mem._out.get(s, ()) if s is not None else ()):
            if e["type"] == t and int(e["tgt"]) == g:
                txn.remove_edge(e)
    txn.adapter_core = set()
    old_files = list(info.get("files") or ())

    sources, tests, docs = [], [], []
    for rel, entry in mem._files_table.items():
        if not entry.get("ctx"):
            continue
        p = root / rel
        fid = mem.node_by_key(file_key(rel))
        role = entry.get("role")
        if role == "source":
            text = M._read_text(p) or ""
            if p.suffix == ".py" and text.startswith("\ufeff"):
                text = text[1:]
            sources.append(IndexedSource(rel, fid, text))
        elif role == "test":
            tids = sorted((n for n in txn.st.owned.get(rel, ())
                           if _prefix(txn.key(n)) == "test"),
                          key=lambda n: (int(mem.nodes[n]["attrs"].get(
                              "start_line") or 0), n))
            tests.append(IndexedTests(rel, fid, M._read_text(p) or "", tids))
        elif role == "doc":
            docs.append(IndexedDoc(rel, mem.node_by_key(f"doc:{rel}"), fid,
                                   M._read_text(p, limit=1_000_000) or ""))

    builder = _AdapterBuilder(txn)
    new_files: list[str] = []

    def file_node(rel: str, *, indexed: bool = True) -> int:
        if rel not in new_files:
            new_files.append(rel)
        return txn.ensure_file(rel, M._file_attrs(root, rel, indexed=indexed))

    srel = M.state_rel(root)
    ctx = IndexContext(
        builder=builder, root=root, repo=txn.repo, file_node=file_node,
        skipped=lambda rel: M._skipped(rel) or bool(
            srel and (rel == srel or rel.startswith(srel + "/"))),
        sources=sources, tests=tests, docs=docs, options=dict(opts))
    for indexer in indexers:
        indexer(ctx)
    mem._adapter_info = {"files": new_files, "edges": builder.core_edges,
                         "unkeyed": builder.unkeyed,
                         "stamps": stamps(root, list(dict.fromkeys(
                             [*new_files, *(f for f in old_files
                                            if not (root / f).exists())])))}
    txn.adapter_core = {tuple(e) for e in builder.core_edges}
    txn.touched.update(old_files, new_files)


def _referenced(txn: _Txn, rel: str) -> bool:
    """Some indexed file's references resolve to `rel` (as `file_node(rel)`
    in a build, with or without an edge)."""
    mem, root = txn.mem, txn.root
    for src in txn.st.cands.get(rel, ()):
        entry = mem._files_table.get(src)
        for ref in (entry or {}).get("refs") or ():
            if ref[0] == "imports":
                try:
                    cands = M._import_candidates(src, ref[1], ref[2])
                except ValueError:
                    continue
                if rel in cands and M._resolve_candidates(root, cands) == rel:
                    return True
            elif ref[1] == rel and (root / rel).is_file():
                return True
    return False


def _settle_file(txn: _Txn, rel: str,
                 walked_attrs: dict[str, dict[str, Any]]) -> None:
    """Create, update or remove File(rel) by the reasons for it to exist."""
    if not rel:
        return
    mem = txn.mem
    fid = mem.node_by_key(file_key(rel))
    walked = rel in mem._files_table
    core_ref = adapter = history = False
    if fid is not None:
        for e in mem._in.get(fid, ()):
            sk = _prefix(txn.key(int(e["src"])))
            if e["type"] in REF_EDGES and sk in _FILE_SIDE:
                core_ref = True
            elif e["type"] in HISTORY_EDGES:
                history = True
            elif sk not in _CORE:
                adapter = True
    adapter = adapter or rel in _adapter_files(mem)
    core_ref = core_ref or _referenced(txn, rel)
    if not (walked or core_ref or adapter or history):
        if fid is not None:
            txn.remove_node(fid)
        return
    if walked:
        attrs = walked_attrs.get(rel)
    else:
        attrs = M._file_attrs(txn.root, rel, indexed=core_ref or adapter)
    if fid is None:
        fid = txn.ensure_file(rel, attrs)
    if attrs is not None:
        txn.set_attrs(fid, attrs)


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def _require_table(mem: Any) -> None:
    if mem._files_table is None:
        raise ValueError(
            f"{mem.dump}: snapshot version {mem.version} has no per-file "
            f"table; rebuild it (`python -m ozagia.core.memory build`)")


def reingest(mem: Any, paths: Iterable[str], *, write: bool = True) -> GraphDelta:
    """See the module docstring and `DevMemory.reingest`."""
    from ozagia.adapters import registry
    t0 = time.perf_counter()
    _require_table(mem)
    root = Path(mem.raw_repo_root).resolve()
    files = _normalize(root, paths)
    opts = mem._build_opts
    timings: dict[str, float] = {}

    tick = time.perf_counter()
    frags = {rel: _parse(root, rel, opts) for rel in files}
    timings["parse"] = time.perf_counter() - tick

    tick = time.perf_counter()
    st = _state(mem)
    timings["state"] = time.perf_counter() - tick
    tick = time.perf_counter()
    txn = _Txn(mem, st, root)
    walked_attrs: dict[str, dict[str, Any]] = {}
    test_order: dict[str, list[tuple[str, int]]] = {}
    for rel in files:
        _apply_fragment(txn, rel, frags[rel], walked_attrs, test_order)
    referrers: set[str] = set()
    for rel in files:
        referrers |= st.cands.get(rel, set())
    for rel in sorted(referrers - set(files)):
        _rederive(txn, rel)
    for rel in files:
        _relink_failures(txn, rel, test_order.get(rel, []))
    timings["update"] = time.perf_counter() - tick

    tick = time.perf_counter()
    indexers = registry(root).indexers
    adapter_pass = bool(indexers) and _needs_adapter_pass(txn, files)
    if adapter_pass:
        _adapter_pass(txn, indexers, opts)
    timings["adapters"] = time.perf_counter() - tick

    tick = time.perf_counter()
    settled: set[str] = set()
    while txn.touched - settled:
        for rel in sorted(txn.touched - settled):
            settled.add(rel)
            _settle_file(txn, rel, walked_attrs)
    txn.flush()
    for rel in files:
        mem._drop_cached(rel)
    mem._n_labels = max(1, len(mem.nodes))
    timings["update"] += time.perf_counter() - tick

    tick = time.perf_counter()
    digest_changed = False
    if txn.repo is not None:
        attrs = dict(mem.nodes[txn.repo]["attrs"])
        attrs.update(M.repo_counts(n["type"] for n in mem.nodes.values()))
        status = _git_status(root)
        if (not pending_changes(mem, status=status)
                and not unhandled_changes(mem, status=status)):
            digest, err = M._tree_digest(root, dump=mem.dump)
            if digest is not None:
                attrs.pop("tree_digest_error", None)
                attrs["tree_digest"] = digest
            else:
                attrs.pop("tree_digest", None)
                attrs["tree_digest_error"] = err or "unknown"
            mem._other_stamps = stamps(root, unhandled_paths(
                root, mem._files_table, _adapter_known(mem), opts, mem.dump,
                status=status))
            old = mem.nodes[txn.repo]["attrs"]
            digest_changed = (old.get("tree_digest") != attrs.get("tree_digest")
                              or old.get("tree_digest_error")
                              != attrs.get("tree_digest_error"))
        txn.set_attrs(txn.repo, attrs)
    mem._staleness_memo = None
    timings["summary"] = time.perf_counter() - tick

    delta = txn.delta(files)
    timings["write"] = 0.0
    if write and (not delta.empty or digest_changed or files):
        if getattr(mem, "defer_writes", False):
            # A long-lived owner (the daemon) writes once, at the end.
            mem.snapshot_dirty = True
        else:
            timings["write"] = mem.write_snapshot()
    delta.seconds = round(time.perf_counter() - t0, 4)
    timings["adapter_pass"] = 1.0 if adapter_pass else 0.0
    mem.last_reingest_timings = {k: round(v, 4) for k, v in timings.items()}
    return delta


def _git_status(root: Path) -> list[tuple[str, str]]:
    """(XY status code, path) for each path `git status` reports; a rename
    gives both paths."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        for path in line[3:].strip().split(" -> "):
            path = path.strip().strip('"')
            if path and path not in seen:
                seen.add(path)
                out.append((line[:2], path))
    return out


def _git_status_paths(root: Path) -> list[str]:
    return [path for _code, path in _git_status(root)]


# --------------------------------------------------------------------------- #
# stamps of files the `files` table does not cover
# --------------------------------------------------------------------------- #
def file_stamp(root: Path, rel: str) -> list[Any] | None:
    """[size, mtime_ns, sha256] of `rel` (sha256 None above
    MAX_PARSE_BYTES), or None when it is not a readable file."""
    p = Path(root) / rel
    try:
        st = p.stat()
        if not p.is_file():
            return None
        sha = (hashlib.sha256(p.read_bytes()).hexdigest()
               if st.st_size <= M.MAX_PARSE_BYTES else None)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, sha]


def stamps(root: Path, rels: Iterable[str]) -> dict[str, list[Any] | None]:
    return {rel: file_stamp(root, rel) for rel in rels}


def _stamp_changed(root: Path, rel: str, stamp: Any) -> bool:
    """`rel` differs from `stamp` (by size and mtime, then sha256). A
    same-content stamp gets the new mtime, so the hash is skipped next time."""
    p = Path(root) / rel
    try:
        st = p.stat()
        exists = p.is_file()
    except OSError:
        exists = False
    if not stamp:
        return exists
    if not exists:
        return True
    if [st.st_size, st.st_mtime_ns] == list(stamp[:2]):
        return False
    if stamp[2] is None or st.st_size > M.MAX_PARSE_BYTES:
        return True
    try:
        now = hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return True
    if now != stamp[2]:
        return True
    stamp[0], stamp[1] = st.st_size, st.st_mtime_ns
    return False


def unhandled_paths(root: Path, table: dict[str, Any], adapter_files: Iterable[str],
                    opts: dict[str, Any], dump: Any, *,
                    status: list[tuple[str, str]] | None = None) -> list[str]:
    """Tracked paths `git status` reports as changed (modified, deleted,
    renamed, staged; not untracked) that no reingest handles: not indexed,
    not adapter-owned, not a file a build would index, not in ozagia's own
    directories. Untracked files a build would not index are left out: the
    graph of a fresh build does not depend on them."""
    excludes = M._digest_excludes(root, dump)
    afiles = set(adapter_files)
    out: list[str] = []
    for code, rel in (status if status is not None else _git_status(root)):
        if code in ("??", "!!") or rel in table or rel in afiles:
            continue
        if rel in excludes or rel.startswith(excludes):
            continue
        if _role(root, rel, opts):
            continue  # indexed by path: pending_changes / reingest handle it
        out.append(rel)
    return out


def unhandled_changes(mem: Any, *,
                      status: list[tuple[str, str]] | None = None) -> list[str]:
    """Changed tracked paths the graph cannot take in by reingest:
    `unhandled_paths` that differ from their stamp in the snapshot
    (`other_stamps`, taken when the tree digest was), and stamped paths that
    are clean again. While any
    is left, reingest keeps the old `tree_digest`, so `staleness()` reports
    stale until a build."""
    _require_table(mem)
    root = Path(mem.raw_repo_root).resolve()
    if status is None:
        status = _git_status(root)
    table = mem._files_table
    afiles = _adapter_known(mem)
    recorded = mem._other_stamps or {}
    cur = unhandled_paths(root, table, afiles, mem._build_opts, mem.dump,
                          status=status)
    out = [rel for rel in cur
           if rel not in recorded or _stamp_changed(root, rel, recorded[rel])]
    now = set(cur)
    out += [rel for rel in recorded
            if rel not in now and rel not in table and rel not in afiles]
    return out


def pending_changes(mem: Any, *,
                    status: list[tuple[str, str]] | None = None) -> list[str]:
    """Files a `refresh` would reingest: indexed files whose content differs
    from the snapshot (by size and mtime, then sha256) or that are gone,
    adapter-owned files whose content differs from their stamp (`adapter`
    `stamps`, taken at the build and after each adapter pass) or that are
    gone, and files `git status` reports that a build would index but the
    snapshot does not have, or that are adapter-owned without a stamp."""
    _require_table(mem)
    root = Path(mem.raw_repo_root).resolve()
    table = mem._files_table
    out: list[str] = []
    for rel, entry in table.items():
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            out.append(rel)
            continue
        if not p.is_file():
            out.append(rel)
            continue
        if (st.st_size, st.st_mtime_ns) == (entry.get("size"),
                                            entry.get("mtime_ns")):
            continue
        fid = mem.node_by_key(file_key(rel))
        sha = ((mem.nodes[fid]["attrs"].get("sha256") if fid is not None
                else None))
        if sha is None or st.st_size > M.MAX_PARSE_BYTES:
            out.append(rel)
            continue
        try:
            now = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out.append(rel)
            continue
        if now != sha:
            out.append(rel)
        else:
            # Same content: remember the new stamp to skip the hash next time.
            entry["size"], entry["mtime_ns"] = st.st_size, st.st_mtime_ns
    afiles = _adapter_files(mem)
    astamps = mem._adapter_info.get("stamps") or {}
    for rel in sorted(afiles):
        if rel in table or rel in out or rel not in astamps:
            continue
        if _stamp_changed(root, rel, astamps[rel]):
            out.append(rel)
    opts = mem._build_opts
    for _code, rel in (status if status is not None else _git_status(root)):
        if rel in table or rel in out:
            continue
        if _role(root, rel, opts) and M._walked(root, rel):
            out.append(rel)
        elif rel in afiles and rel not in astamps:
            out.append(rel)
    return out
