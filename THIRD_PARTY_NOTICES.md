# Third-party notices

## Ozma protocol research

The historical protocol specifications, documentation, selected source and
tests under `docs/research/ozma_protocol/upstream/` were imported from the
`ozma-ai/research` project. They are MIT licensed, copyright (c) 2026 Ozma Labs
Pty Ltd. The complete original notice is preserved in
`docs/research/ozma_protocol/LICENSE`.

## Ocelot

The optional Ocelot reconciliation backend in
`src/ozagia/research/ocelot_reconcile.py` and
`examples/ocelot_reconcile_worker.py` integrates with the Ocelot graph
substrate and its shape/analogy machinery.

Ocelot is MIT licensed. Copyright (c) 2026 Matthew Parslow.

Ocelot is not bundled with ozagia. It is used only when an Ocelot checkout is
configured (`OZAGIA_OCELOT` or `ocelot_root`), either in-process when its
`substrate_rs` runtime is importable or through a subprocess running in its own
environment. Its source and license remain in that checkout.

## Other optional services

`nomic-embed-text` via Ollama is an optional local embedding service. Ollama and
the model weights are not distributed with ozagia. GROBID, PyMuPDF, Poppler,
Pandoc and Tesseract are optional local tools with their own licenses.
