# `ozagia-memory`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/core/memory.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
ozagia.core.memory — a mechanical repo-memory index and query layer.

This module builds a graph snapshot of a source checkout (files, symbols,
tests, docs, git history, known failures, plus whatever adapters add) and
answers read-only questions over it. It never decides what a coding agent
should do; the agent (OpenCode, Claude Code, ...) decides, and the snapshot is
the memory and the citation source.

Why a graph and not an embeddings index: the useful development questions are
typed and relational ("which tests cover this file?", "which files import this
module?", "what changed recently near this symbol?"). Those are edge
traversals, not fuzzy similarity. The snapshot is also stable, inspectable and
diffable.

Built-in graph shape:

    Repo -contains-> File
    File -defines-> Symbol | Doc
    File -imports-> File
    File -has_test-> Test
    Test -tests_module-> File
    Test -mentions_file-> File
    Doc -documents-> File
    Commit -changes-> File
    Failure -refers_to-> Test
    Failure -in_file-> File

Adapter indexers (`ozagia.adapters`) add project node types. The query layer
also understands `Seed` and `Rule`-like nodes (`seed`, `rule`, task anchors)
when an adapter provides them; without them those queries find nothing.

Every node has a stable key (`ozagia.core.keys`): `file:<rel>`,
`sym:<rel>::<qualname>`, `test:<rel>::<qualname>`, `doc:<rel>`,
`commit:<sha>`, `failure:<id>`, `repo:`, or an adapter's own kind. Python
symbols with the same qualname in one file share one node (the first). Rust
items are keyed by qualname too; the 2nd, 3rd ... item with the same
qualname in a file, in file order, is `<qualname>#2`, `#3`. A node created
without a key gets `<Type>:<file-or-path-attr-or-none>#<n>`.
`DevMemory.reingest` updates the graph for changed files without a full
build (see `ozagia.core.reingest`); `DevMemory.refresh` finds them.

What is indexed comes from the project configuration (`ozagia.config`).

Usage:

    # build (or refresh) the snapshot
    .venv/bin/python -m ozagia.core.memory build

    # query
    .venv/bin/python -m ozagia.core.memory search "tag cache"
    .venv/bin/python -m ozagia.core.memory symbol build_index
    .venv/bin/python -m ozagia.core.memory tests src/pkg/module.py
    .venv/bin/python -m ozagia.core.memory context "add a cache option"
    .venv/bin/python -m ozagia.core.memory record --kind decision         --task "..." --summary "..." --file src/pkg/module.py

The same surface is exposed over MCP by `ozagia.integrations.mcp.server`.
```
