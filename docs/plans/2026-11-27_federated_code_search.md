# Federated code search

Status: complete 2026-11-27. Verified by targeted tests, Ruff, and a curated-task
replay of the real Bigville/Ocelot/Ozagia temporary federation. This is the
selected follow-on after the curated-task replay showed federated lexical graph
search misses most expected files. It adds an opt-in, read-only code-search arm
over selected member checkouts; it does not change the existing snapshot-only
`federation.search`.

## Objective

Make multi-repository retrieval adequate for real source-localisation tasks
without silently indexing or writing member checkouts. The arm should search
each selected member's current source, merge results deterministically and
relevance-only, and report enough freshness/provenance to distinguish a source
hit from a snapshot hit. Graph-mode family ranking is unchanged.

## Design

- `federation.code_search(root, query, roots=None, limit=20,
  include_snippet=False, cache_dir=None)`:
  - resolves selected members with the existing federation rules;
  - calls `ozagia.core.search.code_search` for each existing member root with a
    cache path **outside** that member (anchor state `federation-search/` or a
    temporary fallback);
  - merges results by per-member normalized score (score divided by that
    member's best score), sorts deterministically and truncates globally; code
    mode is relevance-ranked only and has no family boost;
  - reports member aliases, roots, snapshot state, cache location, source mode
    and limits; missing source roots are reported, not guessed.
- CLI: `ozagia federation code-search QUERY` with `--root`, repeated `--repo`,
  `--limit`, `--json` and `--no-sync`. `--family-boost` stays only on
  `federation search`.
- MCP: extend `federation_search` with `mode: "graph"|"code"` (default `graph`)
  rather than adding a new tool; code mode reads current member source, writes
  only its cache outside members, and rejects `family_boost` with a structured
  error. Graph-mode family ranking is unchanged.
- Capability catalogue: include the code-search implementation and tests in the
  federation search capability, and state that code mode is relevance-only.
- Evaluation: replay `evaluation/bigville_retrieval/tasks.json` through the new
  arm over the existing real Bigville/Ocelot/Ozagia temporary federation. Keep
  the old `+0.15` rule only as a clearly labeled local experimental arm to
  preserve the negative evidence; it was rejected and removed from the shipped
  API. Record hit@1/3/5 and MRR, controls, per-task ranks and limitations under
  `docs/results/`.
- Default remains `family_boost=False` for graph search; the new code arm is
  opt-in, relevance-only and does not replace graph search.

## Boundaries

- No member index rebuild, adapter execution, target import or target write.
- The cache lives under the anchor's state directory or a temporary directory,
  never under a member root.
- Code mode reads current source, so a stale/missing member snapshot is not
  proof that the source is current; the report carries both.
- No network, embeddings, model calls or live evaluation.
- Snippets are opt-in and bounded.

## Acceptance

- Targeted code-search, federation, CLI/MCP and capability tests pass; Ruff is
  clean.
- The real-task replay records whether code search materially improves recall
  and preserves the negative experimental family evidence without shipping the
  option.
- Docs and generated reference pages are current; no member writes are observed.

## Verification

- `tests/test_federation_code_search.py` covers member source hits, deterministic
  relevance-only ordering, cache placement and fallback, no member writes,
  missing source reporting, opt-in snippets, tie ordering, and rejection of the
  removed `family_boost` argument.
- `tests/test_federation_surface.py` covers CLI `code-search` forwarding, CLI
  rejection of `--family-boost`, MCP `federation_search` `mode="code"` schema
  and dispatch, and the structured MCP error when code mode receives
  `family_boost=true`.
- `.venv/bin/python -m pytest -q tests/test_federation_code_search.py
  tests/test_federation.py tests/test_federation_surface.py
  tests/test_capabilities.py` passes.
- `.venv/bin/python evaluation/family_retrieval/real_tasks_code.py` wrote
  `docs/results/family_retrieval_code_2026-11-27.{json,md}`. Code-search baseline
  hit@1/3/5 was 62.5%/62.5%/62.5% (MRR 0.625) on the eight curated tasks versus
  0% hit@5 for the prior snapshot-only graph run. The local experimental `+0.15`
  arm was rejected and removed: it did not improve any family task and moved the
  rankable out-of-family control from rank 3 out of the top 20. The Markdown
  control is outside the code arm's source universe.
- Cache was `/tmp/opencode/family-real/bigville/.ozagia/federation-search`
  (anchor state, no fallback); all three cache files were outside non-anchor
  members; Ocelot and Ozagia had no file changes. Member snapshots reported
  `stale` because their roots differ from the recorded checkout roots.
- Shipped code mode is relevance-only: no `family_boost` on the library, CLI or
  MCP code surface. Graph-mode family ranking is unchanged; no member index was
  refreshed and no target was imported or written.
