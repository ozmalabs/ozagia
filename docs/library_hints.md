# Library-first hints and NIH capture

`ozagia.core.library_hints` notices the most common "reinvented wheel"
patterns in code an agent just wrote, names a standard-library primitive or a
well-supported package, and records that the hint was shown. It is advisory:

- no code is rewritten;
- no package is installed;
- no model is called;
- no network request is made;
- no claim is made that a library is better in every context.

The layer is standard-library only. It reads manifests and source files; it
never imports the target project.

## Capabilities

The registry is versioned data (`RULE_VERSION`), not an inference over the
whole package ecosystem. Every language lists packages in preference order;
a standard-library or platform primitive comes first where one fits.

| Capability | Python | JavaScript/TypeScript | Rust |
| --- | --- | --- | --- |
| retry/backoff | `tenacity`, `backoff` (no stdlib primitive) | `p-retry` | `backoff`, `tokio-retry` |
| HTTP client | `urllib.request` (stdlib), `httpx`, `requests` | `fetch` (built in), `axios`, `undici` | `reqwest` |
| validation/schema | `pydantic`, `jsonschema` | `zod`, `ajv` | `serde`, `validator` |
| CLI parsing | `argparse` (stdlib), `click`, `typer` | `commander`, `yargs` | `clap` |
| YAML/TOML | `tomllib` (stdlib), `PyYAML`, `json` (stdlib) | `yaml`, `JSON` (built in) | `toml`, `serde_yaml` |
| date/time | `datetime` + `zoneinfo` (stdlib), `dateutil` | `date-fns`, `luxon` | `chrono`, `time` |
| caching | `functools.lru_cache` (stdlib), `cachetools` | `lru-cache` | `moka`, `cached` |
| glob/diff/patch | `fnmatch`, `pathlib`, `difflib` (stdlib) | `picomatch`, `diff` | `globset`, `similar` |
| IDs/slugs | `uuid`, `secrets` (stdlib), `python-slugify` | `nanoid`, `slugify` | `uuid`, `slug` |
| rate limiting | `limits`, `slowapi` | `rate-limiter-flexible` | `governor` |
| semver | `packaging`, `semver` | `semver` | `semver` |
| circuit breaker | `pybreaker` | `opossum` | `recloser` |

`registry()` returns the data as JSON-serialisable values. TypeScript uses the
`javascript` registry and ecosystem name.

## Detection

A hint run needs an explicit set of files:

- a committed range (`detect --diff BASE..HEAD`);
- an explicit changed-path list (`detect --changed PATH ...`);
- the before/after text of one handled edit or write (the intent layer).

The detector never scans a repository on its own. Reads are bounded
(`MAX_FILE_BYTES`, symbol count and per-call file count). Python files use the
AST to find function/class spans and new or changed symbols. Other languages
use bounded function/brace heuristics and changed-line windows.

### Evidence

Every rule combines body patterns with optional name evidence. A name alone
never produces a hint. Representative evidence:

- a retry loop with a bounded attempt count and `sleep`/backoff;
- `sys.argv` slicing, manual `--flag` matching and usage text;
- line-by-line YAML/TOML parsing (`splitlines`, `partition(":")`, section
  headers);
- a date regex plus manual `int(...group(...))` conversion;
- a manual cache dictionary with a lookup and a store;
- a hand-written glob-to-regex translation or a DP-matrix diff;
- a slug helper that lowercases and replaces non-alphanumerics;
- a random/clock ID source;
- a rate limiter clock/interval/sleep combination;
- a version split plus numeric comparison;
- a failure counter, threshold and open state.

Evidence is reported as short labels ("bounded attempts, sleep, backoff"), not
as source text. Confidence is `low`, `medium` or `high` from the rule; the
example retry rule is `medium`.

### Availability

| Level | Meaning |
| --- | --- |
| `stdlib` | A standard-library or platform primitive; preferred where it fits. |
| `declared` | Named in `pyproject.toml`, `requirements*.txt`, `package.json` or `Cargo.toml`. |
| `installed` | A Python distribution found through `importlib.metadata` but not declared. |
| `known` | A curated package that is neither installed nor declared. |

A package already imported and used by the file produces no hint. Importing a
standard-library module with a narrower capability (for example `functools`
without `lru_cache`) does not suppress a `functools.lru_cache` hint, but using
the capability (`lru_cache`) does. A thin wrapper around an imported package is
suppressed as well. Suppression is recorded when event recording is on, so the
behaviour can be measured later.

## Modes and settings

```toml
[hints]
library = "gentle"        # off | gentle | strict
max_per_edit = 2
record = true
ecosystems = ["python", "javascript", "rust"]
```

| Key | Default | Effect |
| --- | --- | --- |
| `hints.library` | `gentle` | `off`: nothing, no events. `gentle`: new/changed symbols. `strict`: also existing symbols in explicitly named files. |
| `hints.max_per_edit` | `2` | Hints shown per edited file (0-20). Suppressed candidates are recorded. |
| `hints.record` | `true` | Append shown/suppressed events to the state directory. |
| `hints.ecosystems` | all three | Restrict languages. TypeScript counts as `javascript`. |

`$OZAGIA_LIBRARY_HINTS=0` is a hard local opt-out. The values `off`, `gentle`
or `strict` in that variable override the mode for one process. The typed
settings also have the usual environment overrides
(`OZAGIA_HINTS_LIBRARY`, `OZAGIA_HINTS_MAX_PER_EDIT`, `OZAGIA_HINTS_RECORD`,
`OZAGIA_HINTS_ECOSYSTEMS`).

Ordering is deterministic: file, capability registry order, confidence,
symbol, package. A capability is reported at most once per file. Only metadata
is stored; evidence labels are in the output, not in the event log.

## Capture and privacy

Events live in `.ozagia/library_hints/events.jsonl` (or under
`$OZAGIA_STATE_DIR`). Each line has:

```json
{"schema": 1, "ts": "2026-11-16T12:00:00Z", "session": "ses_...",
 "file": "src/app.py", "symbol": "_retry", "capability": "retry",
 "package": "tenacity", "availability": "declared", "confidence": "medium",
 "status": "shown", "reason": null, "rule_version": "2026.11.1",
 "hint_id": "..."}
```

`status` is `shown` or `suppressed`; `reason` is one of `already_imported`,
`thin_wrapper`, `duplicate` or `max_per_edit`. The log never contains code,
prompts, file contents or evidence text. It is local state, not telemetry.

Feedback is also local: `.ozagia/library_hints/feedback.jsonl` stores only the
hint id, `accepted`/`ignored`, a timestamp and a session label. It is never
sent anywhere.

## CLI

```bash
python -m ozagia.library_hints_cli detect --root R --diff BASE..HEAD
python -m ozagia.library_hints_cli detect --root R --diff BASE..HEAD --json
python -m ozagia.library_hints_cli detect --root R --changed src/app.py
python -m ozagia.library_hints_cli detect --root R --diff BASE..HEAD --max 1 --no-record
python -m ozagia.library_hints_cli explain _retry --root R
python -m ozagia.library_hints_cli feedback HINT_ID accepted --root R
```

`detect` prints one `[ozagia] library hint:` line per hint, or `no library
hints`. `--json` returns the hints, suppressed candidates, counts, mode and
rule version. `--out FILE` writes the same text atomically. `explain` reads the
local event log and the registry; it accepts a symbol, hint id, capability or
package. `feedback` is a local write only.

## Intent and daemon integration

After a successful handled `edit` or `write` of a supported code file,
`ozagia.intent` reads the text before the call (when the file already exists),
computes hints for new/changed symbols and appends the bounded lines to the
tool output. The edit result carries:

- `meta.library_hints`: hints shown;
- `meta.library_hint_files`: 1 when the edited file produced a hint;
- `meta.library_hints_suppressed`: candidates suppressed.

A hint failure never fails the edit. `off` and
`$OZAGIA_LIBRARY_HINTS=0` produce no hints, no `meta` counters and no events.
Built-in (non-handled) edits are not hooked yet; that remains a gap.

`ozagia daemon` sums those `meta` counters into
`stats.library_hints`, `stats.library_hint_files` and
`stats.library_hints_suppressed`.

`hints_for_paths(root, paths, ...)` is the library surface for later code
review and repo-scan sections. It takes the explicit changed paths and
optional before-text. Without before-text, every symbol in the named files is
treated as new/changed because the caller already selected those files.

## Limits and non-goals

- Detection is heuristic. A missed hint is expected; a false positive is
  possible. Hints are advisory and never block a call.
- Only Python has structural symbol extraction. JavaScript/TypeScript and Rust
  use bounded textual heuristics; very large or minified files are skipped.
- The registry is curated and versioned. It is not a package-index search and
  does not compare maintenance, licence or bundle size.
- Manifests are read from the file's ancestors up to the repository root.
  Lockfiles are not parsed for availability.
- No automatic refactor, dependency installation, network lookup or model
  judgement of duplicate code.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_library_hints.py
ruff check src tests
```

The tests cover the retry/tenacity example, declared/imported suppression,
standard-library preference, gentle versus strict, off mode, the per-file cap,
deterministic ordering, metadata-only events, the CLI and the intent/daemon
counters. They use no network and install nothing.
