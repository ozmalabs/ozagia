# `ozagia-symbols`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/core/symbol_graph.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
ozagia.core.symbol_graph — symbol-level callers, callees and impact.

Builds a graph of symbols (definitions) and the references between them from
the tag cache that ``ozagia.core.repo_map`` maintains. Each node is one definition,
id ``"<rel path>::<qualname>"``. Each edge is "caller symbol references callee
symbol", where the caller is the innermost enclosing definition of the
reference (``"<rel path>::<module>"`` for module-level code).

Resolution is syntactic, in three levels with descending confidence:

* ``local``: a reference names a definition in the same file. A call on
  ``self``/``cls`` inside class ``A`` prefers ``A.<name>``.
* ``import``: the file binds the name with ``from X import name``, where ``X``
  resolves to an indexed Python file. The edge goes to that file's definition.
* ``name``: a global definition with that name exists, there are at most
  ``MAX_NAME_CANDIDATES`` of them, and the name is not a generic identifier.

There is no type inference. ``obj.method()`` resolves by name, and dynamic
dispatch, ``getattr`` and star re-exports are not followed. The precise layer is
LSP (``ozagia.lsp``).

Read-only: nothing here edits code.

CLI::

    python -m ozagia.core.symbol_graph {callers,callees,impact,resolve} SYMBOL
        [--depth N] [--root R] [--cache C] [--min-confidence X]
```
