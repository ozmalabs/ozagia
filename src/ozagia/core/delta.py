"""The graph delta: what one reingest changed, by node key.

`DevMemory.reingest(paths)` returns a `GraphDelta`. Edit adapters return it to
the agent, so an edit reports which nodes it changed, added or removed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

EdgeKey = tuple[str, str, str]  # (source key, edge type, target key)


@dataclass
class GraphDelta:
    files: list[str] = field(default_factory=list)       # files reingested
    added: list[str] = field(default_factory=list)       # node keys now present
    removed: list[str] = field(default_factory=list)     # node keys gone
    changed: list[str] = field(default_factory=list)     # same key, attrs differ
    edges_added: list[EdgeKey] = field(default_factory=list)
    edges_removed: list[EdgeKey] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed
                    or self.edges_added or self.edges_removed)

    def merge(self, other: "GraphDelta") -> "GraphDelta":
        """This delta followed by `other`, with keys deduplicated in order.

        Opposite entries cancel: a key (or edge) added by one delta and
        removed by the other, in either order, is in neither list. A key
        changed and then removed is only `removed`; a key added and then
        changed is only `added`."""
        def cat(a: list, b: list) -> list:
            return list(dict.fromkeys([*a, *b]))

        def net(a_add: list, a_rem: list, b_add: list,
                b_rem: list) -> tuple[list, list, set]:
            gone = (set(a_add) & set(b_rem)) | (set(a_rem) & set(b_add))
            return ([k for k in cat(a_add, b_add) if k not in gone],
                    [k for k in cat(a_rem, b_rem) if k not in gone], gone)

        added, removed, gone = net(self.added, self.removed,
                                   other.added, other.removed)
        final = set(added) | set(removed) | gone
        changed = [k for k in cat(self.changed, other.changed)
                   if k not in final]
        e_add, e_rem, _ = net(self.edges_added, self.edges_removed,
                              other.edges_added, other.edges_removed)
        return GraphDelta(
            files=cat(self.files, other.files),
            added=added, removed=removed, changed=changed,
            edges_added=e_add, edges_removed=e_rem,
            seconds=self.seconds + other.seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["edges_added"] = [list(e) for e in self.edges_added]
        d["edges_removed"] = [list(e) for e in self.edges_removed]
        return d
