# ozma

> Caveman not have H100. Caveman only have club.
> But caveman learn which club fit which rock.

## The story

It started with a question: **why does my local 8B model get the same tokens as GPT-5?**

I was running [Caveman](https://github.com/juliusbrussee/caveman) — the insight that
you don't need frontier models if you compress intelligently. Stub out functions,
send structure not source, reconstruct on the other side. Beautiful. 50–98% token
savings. But then I watched my little rnj-1:8b on an Arc B580 nail a rate limiter,
choke on a parser, and completely die on a vague spec.

Same model. Same hardware. Wildly different results. The model wasn't the problem
— the context was. Most of those tokens were function bodies the model never needed
to see.

That insight became ozma: **strip code to its structure, send less, pay less,
get the same result.** Tree-sitter parses the AST, replaces bodies with `...`,
keeps signatures and docstrings. The model works with the shape of your code, not
the noise. Round-trip expansion puts it all back.

---

## A request through ozma vs a standard LLM

A standard LLM request is: dump code into the context window, ask a question,
get prose back, manually apply it. Every step wastes tokens. ozma replaces each
of those steps with something measurably better.

Here's what happens to a request — in order, from submission to result.

---

### Step 1 — Localization: find the right 2% of the codebase

**Standard LLM:** You guess which files are relevant and paste them in. Usually too
many (waste), sometimes wrong (miss), never verified.

**ozma:** The pipeline runs 6 localization strategies against the issue description
or error, then cross-references them:

| Strategy | Input | What it finds |
|---|---|---|
| Issue text | Bug report / PR title | Mentioned symbols, file paths, function names |
| Stack trace | Exception with frames | Exact crash site, call chain into it |
| Distributed trace | Span IDs, service names | Which service boundary the latency/error is in |
| Profiler output | CPU/memory flamegraph | Hot path that needs to change |
| SQL EXPLAIN | Query plan | Table, index, and join causing the scan |
| Coverage report | Line/branch coverage | Uncovered paths near the reported bug |

Each strategy produces a ranked list of `(symbol, confidence)` pairs. The pipeline
takes the intersection across strategies. The result: 1–5 code positions that almost
certainly contain the bug, rather than 10 files that might.

**Impact:** A 10,000-token codebase typically localizes to 200–600 tokens of
targeted context. The model sees less and gets more signal per token.

---

### Step 2 — Context compression: send structure, not source

**Standard LLM:** Entire files paste into context. A 500-line file is 500 lines
regardless of how many matter.

**ozma:** Four compression layers apply in sequence. Each is independent and
composable; they stack.

#### 2a. Tree-sitter stubbing

The AST parser replaces function bodies with `...`, preserving signatures and
docstrings. A 934-line file becomes ~40 lines — everything needed to understand
the API surface, nothing needed to understand the implementation.

```python
# Before (7,184 tokens for wallet_stripe.py)
class StripeWallet:
    def charge(self, amount: int, currency: str, customer_id: str) -> str:
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount,
                currency=currency,
                customer=customer_id,
                ...
            )
            return intent.id
        except stripe.error.CardError as e:
            raise PaymentError(str(e)) from e

# After (150 tokens — 97.9% reduction)
class StripeWallet:
    def charge(self, amount: int, currency: str, customer_id: str) -> str: ...
```

Supports 15 languages: Python, JS/JSX, TS/TSX, C#, Go, Rust, Java, Kotlin,
Swift, Ruby, PHP, SQL, HTML, CSS, YAML.

#### 2b. Semantic retrieval

For large codebases, even the stub outline is too big. A Chroma vector index
over stub signatures lets the pipeline retrieve only the top-K functions
relevant to the task, within a token budget.

```
9,680 tokens (full stubs) → 444 tokens (task-relevant subset)  — 95.4% reduction
```

#### 2c. Ozma IR — keyword substitution

Greek symbols replace language keywords. Every common keyword becomes a single
token. A per-request legend handles long identifiers.

```python
# Python IR
ƒ load(α) → dict:          # def load(self) -> dict:
  μ open(α.path) ν f:      #   with open(self.path) as f:
    → json.load(f)         #     return json.load(f)
```

| Language | Examples |
|---|---|
| Python | `ƒ`=def `β`=class `←`=import `→`=return `θ`=if `η`=for `α`=self |
| JavaScript | `ƒ`=function `β`=class `←`=import `→`=return `α`=async `μ`=await |
| Go | `ƒ`=func `σ`=struct `ι`=interface `←`=import `→`=return `η`=for |
| Rust | `ƒ`=fn `σ`=struct `τ`=trait `ι`=impl `←`=use `→`=return `μ`=mut |
| SQL | `σ`=SELECT `φ`=FROM `ω`=WHERE `∧`=AND `∨`=OR `∅`=NULL |

IR substitution is AST-level only — never inside string literals, comments,
docstrings, or regex. `λ-calculus` in a comment stays `λ-calculus`.

Long identifiers get minified: `user_authentication_service` → `a1` in a
per-request `<legend>` block. The model's output is auto-expanded before the
client sees it.

#### 2d. Embedded language compression

SQL inside `cursor.execute()`, HTML in JS template literals, Jinja2 templates
— all detected and compressed independently. Multi-line SQL becomes a single
compact line; HTML templates strip whitespace and redundant attributes.

**Combined pipeline savings:**

| File | Language | Raw tokens | After pipeline | Reduction |
|---|---|---:|---:|---:|
| wallet_stripe.py (934 lines) | Python | 7,184 | 150 | **97.9%** |
| stripe_connect.jsx (678 lines) | JS | 5,916 | 232 | **96.1%** |
| sample.java (60 lines) | Java | 348 | 59 | **83.0%** |
| sample.go (48 lines) | Go | 300 | 75 | **75.0%** |
| sample.rb (69 lines) | Ruby | 491 | 123 | **74.9%** |
| sample.rs (62 lines) | Rust | 365 | 122 | **66.6%** |
| embedded_sql.py | Python | 405 | 50 | **87.7%** |

---

### Step 3 — Context assembly: structure, not dump

**Standard LLM:** Files pasted in arbitrary order. Model has to infer structure.

**ozma:** The context assembled for the model is structured and layered. Every
element is there for a reason.

```
[System — stable, cache-friendly]
  IR legend (all language tables, unconditional)
  Protocol instructions

[User-role prefix — per-request]
  <legend>a1=user_auth_service, a2=validate_token</legend>

[User message]
  Context: Config.load() crashes on empty file (TypeError: expecting value)

  Likely targets (2):
    §6=3f9a1c2d  method Config.load  L34-42   0.95  (crash site — json.load raises on empty)
    §8=h8i9j0k1  function parse_args  L55-61   0.7   (only caller of Config.load)

  --- Code outline ---
  §1=a1b2c3d4  module app
  §4=d4e5f6g7    class Config
  §5=e5f6g7h8      def __init__(self, path: str)
  §6=3f9a1c2d      def load(self) -> dict
  §7=g7h8i9j0      def save(self, data: dict) -> None
  §8=h8i9j0k1  def parse_args() -> Config

  --- Target code ---
  # §6=3f9a1c2d method Config.load (L34-42)
  ƒ load(α) → dict:
    μ open(α.path) ν f:
      → json.load(f)

  --- Call graph ---
  §6 Config.load: callers: parse_args, main; callees: open, json.load

  --- Test coverage ---
  §6: 0 direct test cases — no test exercises Config.load
```

**Impact:** The model gets exactly what it needs: the crash site, one level up in
the call chain, a description of the relationships, and the coverage gap. Nothing
irrelevant. Positions are `§N` markers with stable IDs — if code is inserted before
`§6`, the model can still address `§3f9a1c2d` and the shim resolves it correctly.

---

### Step 4 — Conditional hints: expert guidance, zero tokens when irrelevant

**Standard LLM:** System prompts are always loaded. Every token fires on every
request whether relevant or not.

**ozma:** 136 YAML-driven hints fire only when detected in the message text.
0.02ms per check, zero tokens when irrelevant.

```python
from ozmalib.hints import load_hints, check_hints, format_hints

hints = load_hints()                    # load once at startup
matched = check_hints(message, hints)   # regex match, ~0.02ms, no model call
block = format_hints(matched)           # inject into system message if non-empty
```

| Category | Count | Examples |
|---|---|---|
| Framework | 32 | Next.js, React, Django, FastAPI, Vue, Angular, Svelte, Rails, Spring |
| Security | 23 | SQL injection, XSS, CSRF, IDOR, file upload, JWT, CORS, secrets |
| Language | 12 | TypeScript strict mode, Go error handling, Rust ownership, Python async |
| Tool | 12 | Alembic, Prisma, Terraform, Vite, GitHub Actions |
| Porting | 13 | Django→FastAPI, Python→Rust, JS→TypeScript, Passport→NextAuth |
| Testing | 5 | Mock boundaries, snapshot traps, flaky tests |
| Reliability | 8 | Connection pooling, graceful shutdown, database indexing |

A migration hint also fires a 5-phase decomposition: source compressed via
stub + IR, decomposed into phases, each routed to the cheapest capable model,
chained. One-shot Opus: $0.161. Pipeline (4 phases, compressed): **$0.00089**.

---

### Step 5 — Model selection: cheapest capable model

**Standard LLM:** You pick a model. Usually the same one every time, regardless
of task.

**ozma:** Every request is classified by difficulty (1–5) and clarity (1–5). The
router sends it to the cheapest model whose tournament record shows it can handle
this category at this difficulty. Built on 232 fights across 19 models and 47 tasks.

- Simple refactors → small local model (sub-cent)
- Complex reasoning → Sonnet tier
- Ambiguous specs → clarify before routing (prevents expensive wrong attempts)

40–60% cost reduction with no quality loss, because most requests aren't hard
— they just got the same heavyweight model as the hard ones.

---

### Step 6 — Checks: output-side validation before the result lands

**Standard LLM:** You review the model's output manually. Security bugs, mutable
default arguments, missing awaits, and N+1 queries ship because nobody caught them.

**ozma:** 42 built-in checks run against every model output via regex, costing
zero tokens and no model calls. Checks have five possible actions:

| Action | What happens |
|---|---|
| `resubmit` | Send corrective feedback to the model, regenerate |
| `fix` | Run a command (e.g. `ruff format {file}`) and replace the output |
| `run` | Run a linter/type checker, log results |
| `warn` | Log and continue |
| `block` | Hard reject — not returned to the client |

**Security checks (critical/resubmit):** f-string SQL, `eval()`/`exec()`,
`pickle` deserialization, hardcoded API keys, `subprocess(shell=True)`, innerHTML
XSS, `yaml.load()` without SafeLoader, JWT decode without verification, weak hash
algorithms (`md5`/`sha1` for crypto), path traversal.

**Correctness checks (high/resubmit):** `TODO`/`NotImplementedError` stubs,
mutable default arguments, missing `await` on async calls, truncated output,
debug code left in (`print`/`breakpoint`/`console.log`), N+1 query patterns.

**Quality checks (auto-fix):** `ruff format` runs automatically on generated
Python. `ruff check` and `mypy` run and log results. `any` overuse in TypeScript.

```python
from ozmalib.checks import load_checks, run_checks, format_resubmit_prompt

checks = load_checks()
results = run_checks(generated_code, checks)

if any(r.action == "resubmit" for r in results):
    feedback = format_resubmit_prompt(results)
    # re-call model with feedback — model sees exactly what was wrong
```

The model sees precise machine-generated feedback ("Generated code constructs SQL
via string formatting. Fix: Use parameterized queries.") rather than a generic
re-prompt. Critical security checks that the model consistently fails to fix
escalate to `block`.

---

### Step 7 — Dev loop: spec → generate → test → fix → review

**Standard LLM:** You write a prompt, get code, run it, find it broken, iterate
manually. Architecture reasoning and implementation run in the same model call
even though they need different capabilities.

**ozma:** The dev loop decomposes a task and runs it through a staged pipeline,
using a different (appropriately-sized) model at each stage:

```
Spec       →  Generate  →  TestGen  →  TestRun  →  Fix loop  →  Review  →  Report
mid-tier       small          small       local      small        different    small
cloud          local          local        CPU        local        cloud        local
```

**Stage breakdown:**

1. **Spec** — A mid-tier model decomposes the task into user story, requirements,
   and sub-tasks. Architecture reasoning needs more capability than implementation.

2. **Map** — Generates stub signatures for all required functions. The design is
   done here; bodies left as `...`. Uses the map model (mid-tier).

3. **Fill** — Each stub body generated independently, in parallel. Isolated
   function-level reasoning — a 1.5b–3b local model handles this fine. This is
   where Ozma IR and brevity constraints pay off most: the fill model sees a single
   compressed function signature and generates only the body.

4. **TestGen** — Generates pytest tests from the spec + assembled code stubs.
   Same model tier as fill (function-level reasoning).

5. **TestRun** — Tests execute locally. Failures are captured and compressed:
   only the failing assertion, the relevant stack frames, and the function under
   test are sent back.

6. **Fix loop** — Compressed failures routed back to the fill model. Only
   implicated functions are re-filled — not the whole file. Up to N iterations
   (default 3). Each iteration: resubmit → re-fill → re-test → checks.

7. **Review** — A *different* cloud model (different from the map model)
   reviews the final code against the spec and test results. Independent
   perspective prevents blind spots from the generation model's assumptions.

8. **Report** — A light model summarises what was built and what issues remain.

**Why role-split models matter:** The fill model runs dozens of times on isolated
function bodies. At 1.5b parameters on a local Arc B580, each fill costs ~$0. The
review model runs once at the end and uses a different cloud provider to avoid
correlated failures. The spec model runs once and uses mid-tier to get the
architecture right. Same cost budget as one frontier model call, completely
different quality profile.

```python
from ozmalib.dev.loop import run, make_openrouter_fn, make_ollama_fn

result = run(
    "Build a RateLimiter class with token bucket algorithm",
    map_fn=make_openrouter_fn("google/gemma-4-26b-a4b-it"),
    fill_fn=make_ollama_fn("qwen2.5-coder:1.5b"),
    review_fn=make_openrouter_fn("meta-llama/llama-3.3-70b-instruct"),
    max_iterations=3,
)
print(result.report)
```

---

### Step 8 — Security review: built-in, effectively free

**Standard LLM:** Security review is manual, or an expensive extra model call.

**ozma:** Two layers, both nearly free:

**Pre-routing (every request, 0 tokens, $0):** Regex hints detect security-sensitive
patterns — SQL string building, auth code, crypto, file upload, eval/exec, SSRF
patterns, hardcoded secrets, CORS configuration. When detected, OWASP-aligned
guidance is injected into the model's system context before the call. The model
sees the guidance; you don't pay for it except when it's relevant.

**CI static scan (every commit, 0 tokens, $0):** Scans git diffs for definite
vulnerabilities: f-string SQL, pickle deserialization, eval with user input,
hardcoded credentials, AWS keys, weak hash algorithms. Blocks merges on critical
findings.

**Optional AI review (~$0.00012/review):** For subtle logic flaws. Compresses the
diff with Ozma IR first, then sends to cheapest capable model. Marginal cost.

---

### Step 9 — Output: structured edits with position addressing

**Standard LLM:** Model writes a prose description of a change or dumps a full
modified file. You read it, understand it, apply it manually.

**ozma (L1+):** The model produces position-addressed edits. `§N` markers (§ can't
appear in source code) reference the outline positions. The shim applies them
directly to the original file via the `SourceMap`.

```
edit §6
```python
ƒ load(α) → dict:
    θ ¬α.path.exists():
        → {}
    μ open(α.path) ν f:
        → json.load(f)
```
```

The shim:
1. Parses the edit, resolves `§6` to `Config.load` via the current outline
2. Expands IR symbols to real syntax
3. Expands minified identifiers via the legend
4. Splices the result back into the original file at the correct line range
5. Produces a clean diff — no manual application needed

**With the trained LoRA (L3):** The model also produces:
- `rationale §6: added empty-file guard — json.load raises on empty string` — one line per changed position, stripped before client delivery, stored for training signal
- `hints:` output when asked to localize without pre-computed context — the model identifies likely targets directly
- `graph:` output showing inferred call relationships

---

### Step 10 — Scope check: verify the model did the right thing

**Standard LLM:** You inspect the diff manually. Scope creep (model changed more
than asked) and scope miss (model didn't change what was needed) go undetected.

**ozma:** After the model produces edits, the pipeline diffs old vs new `CodeIndex`
and checks three things:

| Check | What | Response |
|---|---|---|
| **Scope** | Modified positions outside the targeted set | Feed back: "you changed §3 and §5 — only §6 was targeted" |
| **Miss** | Targeted positions not modified | Feed back: "§6 was the target but wasn't changed" |
| **API break** | Signature of a public symbol changed | Feed back: "§8 signature changed — this breaks callers" |

If checks fail, the feedback is returned as a `user` turn. The model corrects.
This loop runs up to 3 times before surfacing to the human. Most corrections
succeed on the first retry because the feedback is machine-precise.

---

### Step 11 — Expansion: lossless reconstruction

**Standard LLM:** The model's output is the final text. If it's wrong, you re-run.

**ozma:** The shim holds the `SourceMap` from the original stub pass. Any `...`
stub the model left unchanged is automatically restored from the original body.
Only changed positions are spliced. The result is always a valid, complete file —
never a stub with unexpanded `...` bodies.

Output tokens cost 3–5× more than input tokens. IR minification also cuts
the model's output: 33% fewer output tokens on average (measured on REST
endpoints, data pipelines, and cache decorators).

---

### What a trained LoRA adds (L2 → L3)

The IR LoRA (L2) already exists. It handles reading IR code fluently.

The full bidirectional LoRA (L3) adds output-side fluency:
- Writes IR natively (not translated) — no expansion latency
- Produces `§N`-addressed edits without being instructed on the format
- Emits `rationale §N: ...` annotations alongside edits
- Produces `hints:` localization output from outline + issue description
- Emits compact `graph:` and `tree:` output
- Uses WIRE prose compression on all explanatory text

The protocol degrades gracefully: a stock model (L1) gets the same compressed
context and structured prompts, produces prose edits that the shim parses with
a forgiving parser, and still benefits from all the localization, compression,
and scope-checking. The LoRA just makes the model's output side as efficient as
the input side.

---

## Compression savings at a glance

| Stage | Savings | What changes |
|---|---|---|
| Localization | 90–99% of codebase out of scope | From whole repo to 2–5 functions |
| Tree-sitter stubbing | 44–97% of target code tokens | Bodies → `...`, structure preserved |
| Semantic retrieval | 90–96% on large codebases | Only relevant stubs |
| Ozma IR | 5–23% additional | Keywords → single tokens |
| Minification | 6–20% additional | Long identifiers → short aliases |
| Embedded compression | 4–21% additional | SQL/HTML in strings |
| Output IR | ~33% output token reduction | Model writes compact, shim expands |
| Brevity constraint | ~50% output token reduction | "No explanation. Code only." |
| Routing | 40–60% cost reduction | Cheap model for easy tasks |

These stack multiplicatively. A 97.9% input reduction combined with a 33% output
reduction on a task routed to a 10× cheaper model is a 100–500× effective cost
reduction end to end.

---

## Quick start

### Python library

```bash
pip install ozmalib
```

```python
from ozmalib.compress import stub_functions

compressed, source_map = stub_functions(code, language="python")
# 500 lines → 40 lines, round-trippable

from ozmalib.compress import expand
patched = expand(original_code, source_map, llm_output)
```

### MCP server (TypeScript)

Works with Claude Code, Copilot CLI, Cursor, Windsurf, Cline — any MCP client.

```bash
# Claude Code
claude mcp add ozma -- npx @ozma/mcp-server

# Copilot CLI
/mcp add ozma -- npx @ozma/mcp-server
```

No API key needed. Compression runs locally using tree-sitter WASM grammars.

### MCP server (Python)

```bash
claude mcp add ozma -- uv run python -m ozmalib.claude_code_mcp
```

### Use ozma as an OpenAI-compatible endpoint

```bash
export OPENAI_BASE_URL=https://ozma.com/v1
export OPENAI_API_KEY=cr-your-key
```

Everything else is handled transparently — your client speaks OpenAI,
ozma handles the compression, routing, and protocol.

---

## Agent plugin

| Agent | Install |
|-------|---------|
| **Claude Code** | `claude mcp add ozma -- npx @ozma/mcp-server` |
| **Copilot CLI** | `/mcp add ozma -- npx @ozma/mcp-server` |
| **Cursor** | `npx skills add ozmalabs/ozma -a cursor` |
| **Windsurf** | `npx skills add ozmalabs/ozma -a windsurf` |
| **Cline** | `npx skills add ozmalabs/ozma -a cline` |
| **Any MCP client** | Run `npx @ozma/mcp-server` via stdio |

---

## Install

```bash
pip install ozmalib           # Python library (compression + retrieval)
npm install @ozma/mcp-server  # TypeScript MCP server
```

## Docs

- [Protocol](docs/protocol.md) — wire protocol spec, capability levels, data types, grammar
- [Compression](docs/compression.md) — tree-sitter stubbing, semantic retrieval, brevity constraints
- [Architecture](docs/architecture.md) — file map, references

For model routing, dev loops, and efficiency maps: [ozma.com](https://ozma.com)

## References

- [Caveman](https://github.com/juliusbrussee/caveman) — the original insight: you don't need frontier models
- [arXiv:2601.19929](https://arxiv.org/abs/2601.19929) — Stingy Context / TREEFRAG structural compression
- [arXiv:2307.15337](https://arxiv.org/abs/2307.15337) — Skeleton-of-Thought: Prompting LLMs for Efficient Parallel Generation

---

Brought to you by Ozma from [ozmalabs.com](https://ozmalabs.com).
