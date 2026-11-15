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
        """This delta followed by `other`, with keys deduplicated in order."""
        def cat(a: list, b: list) -> list:
            return list(dict.fromkeys([*a, *b]))
        return GraphDelta(
            files=cat(self.files, other.files),
            added=cat(self.added, other.added),
            removed=cat(self.removed, other.removed),
            changed=cat(self.changed, other.changed),
            edges_added=cat(self.edges_added, other.edges_added),
            edges_removed=cat(self.edges_removed, other.edges_removed),
            seconds=self.seconds + other.seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["edges_added"] = [list(e) for e in self.edges_added]
        d["edges_removed"] = [list(e) for e in self.edges_removed]
        return d
