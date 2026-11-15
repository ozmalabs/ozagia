# Optional research mode

Ozagia can hold source-backed claims, evidence, assumptions, notation,
derivation links and model candidates alongside its coding tools. Research
tools are opt-in; the default MCP server remains coding-only.

```bash
ozagia-mcp --root /path/to/workspace --mode research
ozagia-mcp --root /path/to/workspace --mode combined --autobuild
```

`OZAGIA_MODE` also selects `coding`, `research`, or `combined`. Research-only
does not require a development graph snapshot. Combined mode exposes both tool
sets, including `research_dev_context`, which retrieves coding and research
context separately for a session and task. Retrieval results are not asserted
to support one another; review and explicitly link relevant evidence.

Start with `research_init` and configure explicit source roots and files. Use
`research_paper_preview` / `research_paper_ingest` for selected TeX or Markdown
sources; recorded spans preserve hashes and candidate provenance. Graph nodes
and relationships can be inspected, linked and traced. The paper reader
extracts bounded formula candidates and selected scoped declarations; it does
not establish the truth of claims or completely interpret arbitrary papers.

`research_formula_resolve` compares interpretations using the graph's scoped
symbol context; `research_formula_model` can use the resulting selection to
record a native model with links to its context. Missing or conflicting
evidence remains visible. Models are interpretations, and the native execution
backend operates only on its supported Term subset. `research_export_lean`
exports supported definitions; `research_check_lean` uses a locally installed
Lean (`OZAGIA_LEAN` or `PATH`) to check generated declarations. That does not
prove fidelity to the source paper.

The graph, source index, paper ingestion and Lean export use the Python
standard library. Install `ozagia[research]` to enable optional SymPy/Lark
parse proposals. Native projection, graph-context resolution and native
execution additionally require a separately installed compatible `substrate_rs`
runtime. Its absence is reported when those operations are invoked; it is not
required to use coding mode or maintain a research ledger. A converter is not
bundled: optional LaTeXML XML is read only when a selected artifact is supplied.
For a local Ocelot source checkout, the backend can be installed into the same
environment with `python -m pip install /path/to/ocelot/runners/dsl` (which builds
the Rust extension); Ozagia then imports the installed runtime, not the target
project. Backend provenance is reported separately and is not independently
verified against an Ocelot source tree.

Research sessions are kept under `.ozagia/research` in the workspace (honoring
`OZAGIA_STATE_DIR`); existing Ocelot sessions in `var/ocelot_research` are read
as legacy sessions. Only configured and explicitly selected source files are
opened. Source references are checked against hashes, and external source
repositories are not modified.

The [real-paper port evaluation](results/research_paper_real_paper.json)
records a source-hashed example of scoped notation, two explicit native
domain choices and Lean checks. It is a single demonstration rather than a
coverage estimate. Reproduce it with `python -m ozagia.eval.research_paper
--paper /path/to/main.tex`; it requires the example paper, native backend and
Lean to exercise every part.
