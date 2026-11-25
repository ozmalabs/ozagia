# `ozagia-edit`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/edit/code_edit.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
ozagia.edit.code_edit — graph-addressed structural edits for Python source.

THE MODEL. The canonical change is an **edit script**: semantic operations
against a file's definition graph (replace a symbol, replace a function body,
insert an import, rename a symbol, delete a symbol, ...). A unified diff is
only a rendered VIEW for humans. The tool does not accept a raw diff/patch as
input; editing by text-patch is the thing this module exists to replace.

What is graph-addressed here:

  * the target is a `Symbol` node from the repo graph (qualified name), not a
    line range the caller guessed;
  * the operation names the semantic effect (`replace_body`, `insert_import`,
    `rename_symbol`, ...), not "swap these characters";
  * the result records before/after content hashes, the affected graph node,
    and the operation list — a replayable transaction;
  * the diff is emitted at the end, explicitly labelled `diff_view`.

This is still a mechanical host-side applier (per CLAUDE.md): it parses source
into an AST, resolves spans, applies the semantic edit, and writes the result.
The DECISION (which symbol, which operation) belongs to the graph/agent. The
longer-term path is to make the edit algebra itself a set of Rust DSL Terms
over the hub IR (the `Prog*`/`RenderRule` machinery), so the agent can edit the
code graph without a text round-trip at all. This module is the first,
honest step: graph targets in, graph-recorded transaction out.

Python only for v0. Rust/CST editing is deliberately refused rather than
half-done; the Rust path belongs with the existing tree-sitter/hub-IR readers.

Edit ordering. Every op's edits are applied bottom-up against the base text.
Two inserts at the same offset land in op order (the first op's text comes
first). An insert at X and a replacement that starts at X always put the
insert before the replaced text, whichever op came first.
```
