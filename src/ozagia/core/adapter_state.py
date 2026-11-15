"""What the snapshot records about adapter indexers, beyond their nodes and
edges, so a reingest can tell when to run them again and keep what they
wrote (see `ozagia.adapters.Registry.add_indexer`, `ozagia.core.reingest`).

`adapter_info` (the snapshot's `adapter` section) fields kept here:

    watch_stamps  {rel: stamp} for every file matching a `watch` glob at the
                  build or the last adapter pass (`reingest.file_stamp`).
    text_files    {rel: matches}: the indexed files whose text a `texts`
                  regex matched then, and the distinct strings it matched.
    attrs         the overlay: {node key: {"set": {attr: value}, "del":
                  [attr], "orig": {attr: [present, value]}}} for every core
                  node (sym, test, doc, file) an indexer changed attributes
                  of; `orig` is the parsed value before the indexer.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Iterable

# Core node kinds whose attributes a reingest re-parses (Repo attributes are
# copied over by the reingest itself).
OVERLAY_KINDS = frozenset({"sym", "test", "doc", "file"})
_MISSING = object()


def _kind(key: str | None) -> str:
    return key.partition(":")[0] if key else ""


def matches(rel: str, globs: Iterable[str]) -> bool:
    """`rel` matches one of the root-relative `globs`."""
    from ozagia.core.memory import _glob_regex
    return any(_glob_regex(g).fullmatch(rel) for g in globs)


def watched_files(root: Path, globs: Iterable[str]) -> list[str]:
    """Root-relative files matching `globs`, outside skipped directories
    and the state directory, sorted."""
    from ozagia.core import memory as M
    globs = list(globs)
    if not globs:
        return []
    root = Path(root)
    srel = M.state_rel(root)
    out: set[str] = set()
    for g in globs:
        try:
            found = list(root.glob(g))
        except (ValueError, OSError):
            continue
        for p in found:
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            if M._skipped(rel) or (srel and (rel == srel
                                             or rel.startswith(srel + "/"))):
                continue
            out.add(rel)
    return sorted(out)


def text_regexes(inputs: Iterable[Any]) -> list[Any]:
    return [i.texts for i in inputs if i.texts not in (None, False)]


def any_text(inputs: Iterable[Any]) -> bool:
    """Some indexer may depend on any indexed text (`texts=None`)."""
    return any(i.texts is None for i in inputs)


def text_matches(text: str, regexes: list[Any]) -> list[str]:
    """The distinct strings `regexes` match in `text`, sorted."""
    return sorted({m.group(0) for rx in regexes
                   for m in rx.finditer(text or "")})


def text_files(items: Iterable[Any], regexes: list[Any]) -> dict[str, list[str]]:
    """{rel: matches} for each indexed item (IndexedSource/Tests/Doc) whose
    text `regexes` match."""
    out: dict[str, list[str]] = {}
    if not regexes:
        return out
    for it in items:
        got = text_matches(it.text, regexes)
        if got:
            out[it.rel] = got
    return dict(sorted(out.items()))


class AttrLog:
    """Records the attributes of core nodes before an indexer first touches
    them, to build the overlay afterwards."""

    def __init__(self) -> None:
        self.before: dict[int, dict[str, Any]] = {}

    def note(self, nid: int, attrs: dict[str, Any]) -> None:
        if nid not in self.before:
            self.before[nid] = copy.deepcopy(attrs)

    def overlay(self, key_of: Any, attrs_of: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for nid, before in self.before.items():
            key = key_of(nid)
            if _kind(key) not in OVERLAY_KINDS:
                continue
            now = attrs_of(nid)
            if now is None:
                continue
            sets = {k: copy.deepcopy(v) for k, v in now.items()
                    if before.get(k, _MISSING) != v}
            dels = sorted(k for k in before if k not in now)
            if not sets and not dels:
                continue
            orig = {k: ([True, copy.deepcopy(before[k])] if k in before
                        else [False, None]) for k in [*sets, *dels]}
            out[key] = {"set": sets, "del": dels, "orig": orig}
        return out


def reapply(overlay: dict[str, Any], key_to_id: Any, attrs_of: Any,
            set_attrs: Any, *, parsed: set[int] = frozenset()) -> None:
    """Put the overlay back on nodes whose parsed attributes were replaced
    (`parsed`); their parsed values become the new `orig`. Entries of
    nodes that are gone are dropped."""
    for key, ov in list(overlay.items()):
        nid = key_to_id(key)
        if nid is None:
            del overlay[key]
            continue
        attrs = attrs_of(nid)
        fresh = nid in parsed
        new = dict(attrs)
        for k, v in ov["set"].items():
            if fresh:
                ov["orig"][k] = [k in attrs, copy.deepcopy(attrs.get(k))]
            new[k] = copy.deepcopy(v)
        for k in ov["del"]:
            if fresh:
                ov["orig"][k] = [k in attrs, copy.deepcopy(attrs.get(k))]
            new.pop(k, None)
        if new != attrs:
            set_attrs(nid, new)


def strip(overlay: dict[str, Any], key_to_id: Any, attrs_of: Any,
          set_attrs: Any, *, skip: set[int] = frozenset()) -> None:
    """Undo the overlay before the indexers run again: give every overlay
    attribute its parsed value (`orig`) back. Nodes in `skip` were parsed
    again in this reingest and already have their parsed values."""
    for key, ov in overlay.items():
        nid = key_to_id(key)
        if nid is None or nid in skip:
            continue
        attrs = attrs_of(nid)
        new = dict(attrs)
        for k in [*ov["set"], *ov["del"]]:
            present, val = ov["orig"].get(k, [False, None])
            if present:
                new[k] = copy.deepcopy(val)
            else:
                new.pop(k, None)
        if new != attrs:
            set_attrs(nid, new)


__all__ = ["AttrLog", "OVERLAY_KINDS", "any_text", "matches", "reapply",
           "strip", "text_files", "text_matches", "text_regexes",
           "watched_files"]
