# Checks Engine

*Trust but verify.*

Checks are the output-side complement to hints. Where hints inject guidance
**before** the model runs, checks validate what the model **produced** and
trigger actions — warnings, resubmissions, tool runs, or hard blocks.

Same YAML format, same regex engine, different stage in the pipeline.

| | Hints | Checks |
|---|---|---|
| **When** | Before model sees the request | After model produces output |
| **Matches against** | User message + file context | Generated code / text |
| **What it does** | Injects guidance into system context | Flags issues, runs tools, requests regen |
| **Cost** | Zero tokens when irrelevant | Zero cost (regex + optional command) |

## How it works

```
Model output
    │
    ▼
┌─────────────┐
│ load_checks │  ← loads + compiles YAML once
└─────────────┘
    │
    ▼
┌─────────────┐
│ run_checks  │  ← regex match: detect ∧ ¬guard → fire
└─────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│ Actions:                                     │
│   warn     → log, continue                  │
│   resubmit → send feedback to model          │
│   fix      → run command, replace output     │
│   run      → run command (linter), log output│
│   block    → reject output entirely          │
└─────────────────────────────────────────────┘
```

## API

```python
from ozmalib.checks import load_checks, run_checks, format_resubmit_prompt

checks = load_checks()
results = run_checks(generated_code, checks)

for r in results:
    if r.action == "resubmit":
        # Send corrective feedback to the model
        feedback = format_resubmit_prompt(results)
        # ... re-call model with feedback appended
    elif r.action == "fix":
        # Command already ran (in dev loop) or run it yourself
        cmd = r.meta.get("command")
    elif r.action == "run":
        # Analysis command (linter, type checker) — output logged
        cmd = r.meta.get("command")
    elif r.action == "block":
        # Reject entirely
        raise ValueError(f"Output blocked: {r.name}")
```

### `run_checks(output, checks, *, types, max_checks) -> list[CheckResult]`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `output` | `str` | required | Model's generated code/text |
| `checks` | `CheckSet` | `None` (loads built-in) | Pre-loaded check set |
| `types` | `set[str]` | `None` (all) | Only evaluate checks of these types |
| `max_checks` | `int` | `0` (unlimited) | Maximum results to return |

### `CheckResult` dataclass

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Check identifier |
| `type` | `str` | Category: `security`, `correctness`, `quality` |
| `action` | `str` | `warn`, `resubmit`, `fix`, `run`, `block` |
| `message` | `str` | Corrective feedback / explanation |
| `severity` | `str` | `critical`, `high`, `medium`, `low` |
| `confidence` | `float` | 0–1 |
| `matched_patterns` | `list[str]` | Which patterns matched (debugging) |
| `meta` | `dict` | Extra YAML fields + `command` for fix/run actions |

### Helper functions

```python
from ozmalib.checks.engine import has_blocking, get_resubmit_feedback

has_blocking(results)           # True if any action is "block"
get_resubmit_feedback(results)  # List of corrective messages
format_resubmit_prompt(results) # Formatted prompt for model resubmission
```

## Check YAML spec

### Required fields

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Unique identifier |
| `detect` | `list[str]` | Regex patterns. ANY match fires the check. |
| `message` | `str` | Corrective feedback (for resubmit) or explanation (for warn) |

### Optional fields

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | `str` | `"custom"` | Category: `security`, `correctness`, `quality` |
| `action` | `str` | `"warn"` | `warn`, `resubmit`, `fix`, `run`, `block` |
| `severity` | `str` | `"medium"` | `critical`, `high`, `medium`, `low` |
| `guard` | `list[str]` | `[]` | Regex patterns. ANY match suppresses the check. |
| `match_all` | `bool` | `false` | Require ALL detect patterns to match |
| `priority` | `int` | `50` | 1–100. Used in sorting. |
| `command` | `str` | — | Command to run for `fix`/`run` actions. `{file}` is replaced with a temp file containing the output. |

## Actions

### `warn` — informational

Log the finding. Don't block or modify output. Good for style concerns
and non-critical quality issues.

### `resubmit` — corrective feedback

Send the check's message back to the model as corrective feedback.
The model regenerates with the issue flagged. Used for security
vulnerabilities and correctness bugs the model can fix.

### `fix` — auto-fix via command

Run a command to fix the output (e.g., `ruff format {file}`). The command
receives the output as a temp file (via `{file}` placeholder). The fixed
file content replaces the original output.

### `run` — analysis via command

Run a command for analysis (e.g., `ruff check {file}`, `mypy {file}`).
Output is captured and logged but doesn't modify the code. Used for
linting and type checking.

### `block` — hard rejection

Reject the output entirely. Used for critical security issues that the
model consistently fails to fix, or policy violations.

## Built-in checks (42)

### Security (10)

| Name | Severity | Action | What |
|---|---|---|---|
| `fstring-sql` | critical | resubmit | f-string/format SQL construction |
| `eval-exec` | critical | resubmit | eval()/exec() usage |
| `pickle` | critical | resubmit | pickle deserialization |
| `hardcoded-secret` | critical | resubmit | Hardcoded API keys/passwords |
| `shell-injection` | critical | resubmit | subprocess shell=True, os.system |
| `shell-string` | critical | resubmit | Shell commands via string concatenation |
| `dangerous-innerhtml` | critical | resubmit | innerHTML/dangerouslySetInnerHTML XSS |
| `yaml-load` | critical | resubmit | yaml.load() without SafeLoader |
| `jwt-no-verify` | critical | resubmit | JWT decode without verification |
| `insecure-random` | high | resubmit | random module for security-sensitive ops |
| `path-traversal` | high | resubmit | Dynamic file path construction |
| `weak-hash` | high | resubmit | MD5/SHA-1 for crypto |
| `csrf-missing` | high | resubmit | Form without CSRF protection |

### Correctness (19)

| Name | Severity | Action | What |
|---|---|---|---|
| `placeholder` | high | resubmit | TODO/NotImplementedError stubs |
| `mutable-default` | high | resubmit | Mutable default arguments (list/dict) |
| `missing-await` | high | resubmit | Missing await on async calls |
| `truncated-output` | high | resubmit | Truncated/incomplete code output |
| `regex-raw-string` | high | resubmit | Regex without raw string (backslash issues) |
| `go-ignored-error` | high | warn | Ignored error return in Go |
| `goroutine-leak` | high | warn | Goroutine without cancellation/cleanup |
| `sync-db-async` | high | warn | Synchronous DB calls in async context |
| `n-plus-one` | high | warn | N+1 query pattern in loops |
| `float-money` | high | warn | Float arithmetic for currency |
| `debug-code` | medium | resubmit | breakpoint/print debug/console.log |
| `gather-awaited` | medium | resubmit | asyncio.gather missing unpack |
| `bare-except` | medium | warn | Bare except/Exception catch |
| `async-sync-mix` | medium | warn | time.sleep in async code |
| `naive-datetime` | medium | warn | Naive datetime (no timezone) |
| `unbounded-query` | medium | warn | SELECT without LIMIT |
| `missing-timeout` | medium | warn | HTTP calls without timeout |
| `silent-except` | medium | warn | Except with pass (silenced errors) |
| `eq-without-hash` | medium | warn | __eq__ without __hash__ |
| `assert-validation` | medium | warn | assert for input validation |
| `exception-chain` | medium | warn | raise without from (lost chain) |
| `file-not-closed` | medium | warn | File open without context manager |
| `shadowed-builtin` | medium | warn | Shadowed Python builtin names |

### Quality (5)

| Name | Severity | Action | What |
|---|---|---|---|
| `no-error-handling` | low | warn | HTTP calls without error handling (Python) |
| `no-error-handling-js` | low | warn | fetch/axios without catch (JS/TS) |
| `typescript-any` | medium | warn | Excessive `any` type in TypeScript |
| `format-python` | low | fix | Run ruff format |
| `lint-python` | low | run | Run ruff check |
| `typecheck-python` | low | run | Run mypy |

## Dev loop integration

Checks run automatically between tests and review in the dev loop:

```
Tests → Checks → Review → Report
```

1. **fix** actions run first (formatter, etc.)
2. **run** actions execute (linter, type checker) — output logged
3. **resubmit** feedback is collected and sent back to the fill model
4. After resubmission, checks run again to verify the fix
5. Results stored in `LoopResult.check_results`

```python
result = run(task, map_fn=..., fill_fn=...)
if result.checks_passed:
    print("No blocking checks")
for cr in result.check_results:
    print(cr.name, cr.action, cr.severity)
```

## Examples

### Security check (resubmit)

```yaml
name: fstring-sql
type: security
severity: critical
action: resubmit

detect:
  - 'f[''"].*(?:SELECT|INSERT|UPDATE|DELETE)\b'
  - '\.format\(.*(?:SELECT|INSERT|UPDATE|DELETE)\b'

guard:
  - 'execute\([^,]+,\s*[\(\[]'

message: |
  CRITICAL: Generated code constructs SQL via string formatting.
  Fix: Use parameterized queries.
```

### Quality check (run linter)

```yaml
name: lint-python
type: quality
severity: low
action: run
command: ruff check --select E,W,F {file}

detect:
  - '\bdef\s+\w+'
  - '\bclass\s+\w+'
  - '\bimport\s+'

match_all: true

message: |
  Running ruff linter on generated Python code.
```

### Auto-fix (formatter)

```yaml
name: format-python
type: quality
severity: low
action: fix
command: ruff format {file}

detect:
  - '^\s{1,3}[a-z]'

message: |
  Running ruff format on generated code.
```
