# Repository shape reference (`ozagia overview`)

Status: implemented 2026-11-25 as a bounded, read-only orientation feature.
`DevMemory.brief` remains the short token-budgeted session summary; this is the
stable single-page reference that answers *what is this repository, what are
its parts and responsibilities, where do I start, how do the docs map to the
code, and which commands and conventions apply*.

The reference is generated from the checkout. It never imports the target
project, executes its code, starts a process, opens a socket, calls a model or
writes to the repository. The only write is an explicit `--out` at the CLI
edge.

The document has two layers. The **stable reference** is the checkout/source
shape: identity metadata, layout, modules, entry points, docs map, commands,
filename/import test links, and stable coverage/truncation facts. In a Git
checkout it is derived from the **Git index**: `git ls-files --stage` names the
stable file set and `git cat-file` supplies blob content. Neither an untracked
file nor a modified working-tree copy of a tracked file can change the
committed reference; only staged/committed content does. A non-Git directory
has no index and falls back to the bounded filesystem walk.

The **generation environment** is informational and marked *observed; not
checked*: git HEAD/ref, graph-snapshot head/state/counts/generated time,
tag-cache availability and path, files read and budget exhaustion, the
count/sample of untracked files, and the count/sample of tracked files that are
dirty in the working tree. Only the stable layer is hashed and compared by
`--check`.

## Use

```bash
ozagia overview                         # Markdown to stdout (the default)
ozagia shape --json                     # alias; stable, key-sorted JSON
ozagia overview --root /path/to/repo
ozagia overview --budget-s 10 --max-modules 40
ozagia overview --out REPO_SHAPE.md     # the only write; atomic
ozagia overview --check REPO_SHAPE.md   # stable drift 1; missing/unreadable 2
```

MCP clients get the same report through the read-only `dev_overview` tool:

```json
{"format": "markdown", "budget_tokens": 6000, "max_modules": 40}
```

`format: "json"` returns the report as a JSON string. The MCP resource URIs are
`ozagia://overview` (Markdown) and `ozagia://overview.json` (JSON). Both formats
are bounded by `budget_tokens`: Markdown is cut with a note, while JSON first
shortens row lists and then, if still over budget, returns an explicit essential
projection under a `view` block. `content_hash` always identifies the stable
checkout/source projection, not the environment observations. The tool also
reports `truncated` when the budget cut the view.

`ozagia overview --check PATH` is intended for CI or a repository guard: it
builds the report with the checked file excluded (so the generated artifact
does not describe itself), reads the **stable content hash** embedded in the
existing file, and exits:

- `0` when the stable hash matches, including when only local observations
  differ (it then prints that the stable shape matches and that the local
  snapshot/HEAD, uncommitted tracked edits or environment difference is not
  drift);
- `1` only when the index-backed stable source shape drifted (a staged or
  committed source/doc change);
- `2` when the file is missing, unreadable, or has no stable hash.

## Sources

| Section | Source | Read-only behaviour |
| --- | --- | --- |
| identity | checkout name, `pyproject.toml` `[project]`, `LICENSE` | stable; index blobs in Git, no VCS state |
| source | README first prose/highlights plus package metadata | index/bounded read; missing values are labelled |
| layout | Git index file set (or bounded `os.walk` without Git) using source/test/skip rules | hidden, untracked, dirty-worktree, state and configured skip directories are not entered |
| entry points | `[project.scripts]`, `[project.entry-points]`, Python AST `add_parser(...)`, `__main__.py` names | static only; no import |
| modules | Python AST module docstrings, symbol counts; leading comments for other languages | bounded file count and bytes |
| documentation | README/docs titles, headings, links and path mentions; canonical roadmap/plans/results counts | titles and links only; full prose is not copied |
| commands | Markdown fences in README/AGENTS/contributing docs | extracted, never executed |
| tests | filename/import signals only from index content | stable; graph edges are not used |
| architecture | existing `.ozagia/tags.json` through `core.architecture.architecture_report(update_cache=False)` | observed only; excluded from hash/check |
| environment | current `.git/HEAD`, snapshot head/state/counts/time, tag-cache path/availability, files read, untracked and tracked-dirty counts/samples | observed only; excluded from hash/check |

Graph-dependent facts degrade honestly and visibly under `environment`: with no
snapshot, the report says `missing`; with a stale snapshot, `stale`; with no tag
cache, architecture is `available: false` with a file-level fallback. Stable
unknowns and truncation are listed in `coverage.unknowns` and
`coverage.truncated`; environment observations have their own list. Untracked
files and working-tree-only edits to tracked files are reported only by count
and a bounded path sample; they never enter the stable layout, module rows,
docs index, tests, commands or hash.

## Report sections

The JSON schema is `ozagia.repo_shape.v1` and is serialized with sorted keys
and a trailing newline. Top-level sections:

- `schema`, `root_name`;
- `identity`: name, package, version, description, license, language
  histogram and file/source/test/doc/module/symbol/test counts; **no git head
  or snapshot state**;
- `source`: README title/prose/headings/highlights and package metadata;
- `layout`: bounded top-level directory counts, key files and configured
  source/test/skip directories;
- `entry_points`: console scripts, entry-point groups, `__main__` modules and
  statically discovered CLI command names (with source modules in JSON);
- `modules`: package table and core/integrations/tools/other groups, with a
  one-line responsibility from each module docstring;
- `documentation`: README, doc index (bounded), group counts, canonical
  roadmap/plans/results/research counts and cheap link/path coverage;
- `architecture`: hubs, cycles, coupling, modules, test/source ratio and
  dead-code candidate count when the tag cache exists; otherwise a reason and
  file-level facts. **Observed only and excluded from the hash/check**;
- `tests`: stable file count and filename/import test-to-source links;
- `commands` and `conventions`: build/test/lint/run/other commands and
  configured tool conventions;
- `coverage`: files walked, stable unknowns and stable truncation flags;
- `environment`: the labelled, informational observation block: git
  head/ref/state, Git file-view state, tracked count and index-content caps,
  snapshot head/state/counts/generated time/path, tag-cache path/availability,
  files read, budget exhaustion, source states, untracked count/sample,
  tracked-dirty count/sample, observed graph test links and environment
  unknowns. **Excluded from hash/check**;
- `content_hash`: `sha256:` of the canonical JSON of the **stable projection**
  only. The projection is `schema`, `root_name`, `identity`, `source`,
  `layout`, `entry_points`, `modules`, `documentation`, `tests`, `commands`,
  `conventions` and `coverage`.

Determinism: no absolute paths, run dates or elapsed times enter the stable
projection. Two runs over unchanged checkout/source inputs produce the same
stable hash and byte-identical stable Markdown/JSON sections. The environment
block can differ between runs (for example after a commit or index rebuild)
without changing the hash.

## Bounds and failure modes

- Files considered: 50,000; Python modules analysed: 2,000; test files: 50,000
  (analysis is bounded by the same module cap); files read at most 256 KiB
  each; README at most 128 KiB; each document at most 48 KiB. In a Git checkout
  the file set is the index (`git ls-files --stage`), and content is read from
  index blobs (`git cat-file --batch`) without materialising a checkout:
  individual blobs over 4 MiB or more than 64 MiB total are skipped and
  reported in `environment.git_files.index_content`. Untracked and
  tracked-dirty samples are capped at 20 paths each.
- Documents indexed: 200; modules shown: `--max-modules` (default 40,
  clamped to 1..500); command rows: 8 per kind; test mapping rows: 20.
- Graph snapshots larger than 96 MiB are not parsed; the report says why.
- `--budget-s` (default 10 s) is a safety valve. Exhausting it sets
  `environment.budget_exceeded`; it can make the observation partial but never
  changes the target or the stable projection.
- A malformed project configuration is reported as unknown; defaults are used.
- Symlinks are not followed for reads. The report never writes a temporary
  file except the atomic `--out` replacement in the requested directory.

## Regenerate and check this repository

`REPO_SHAPE.md` at the repository root is generated, not hand-edited:

```bash
ozagia overview --out REPO_SHAPE.md
ozagia overview --check REPO_SHAPE.md
```

Regenerate it from the **staged tree** after staging changes to README/package
metadata, layout, module docstrings, entry points, docs map, commands or
filename/import test links. Committing the file changes git HEAD, untracked
files can appear or disappear, working-tree edits can remain uncommitted, and
rebuilding the graph changes snapshot state; none is drift, so `--check` still
exits `0` and says the stable shape matches. The reference is linked from
`README.md`; the focused tests are in `tests/test_repo_shape.py`.

## Deliberately not claimed

- No model-written summary or intent inference.
- No full documentation-to-code traceability: coverage counts links and
  path-like tokens, not whether prose is still correct.
- Untracked files and uncommitted working-tree edits are deliberately not part
  of the stable orientation; a change that should shape the committed reference
  must be staged in Git.
- Architecture is file-level and syntactic; it is unavailable without the tag
  cache and is not freshness-certified.
- Stable test links are candidates from filename/import signals, not proof of
  coverage; graph-derived links are observation only and are not hashed.
- Commands are extracted from documentation fences; nothing is executed.
