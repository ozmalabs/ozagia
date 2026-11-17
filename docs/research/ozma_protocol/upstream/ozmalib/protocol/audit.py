"""
audit.py — First-class code audit verb for the Ozma protocol.

Implements the 5-stage pipeline from LATTE (Liu et al., TOSEM 2025,
10.1145/3711816), adapted for multi-substrate input and DWARF-backed
citation resolution. LATTE's code is not public; this is a from-paper
reimplementation with the following named stages:

    1. preprocess()              — compile/lift, emit per-function slices + sidecar maps
    2. identify_sinks()          — LLM picks call sites of sensitive funcs (VDs)
    3. construct_call_chains()   — backward slicing from VDs (static, not LLM)
    4. identify_sources()        — LLM picks externally-controllable inputs; intersect
                                   with CCs to form DFs (source→sink paths)
    5. run_prompt_sequences()    — per-DF chained prompts that walk the call chain

Ozma-specific additions on top of LATTE's shape:
    * multi-substrate (ir/x86/wasm) + opt-level dimensions on Target
    * DWARF-backed CodeRef resolution via sidecar offset maps
    * cross_substrate_dedupe() — same bug flagged in ir+x86 = one Finding
    * ground_truth() — CVE-GENIE-style verification hook (stub in MVP)
    * AuditSession.budget_usd — cost ledger owned by the protocol

MVP scope: stages 1, 2, and 5 run end-to-end; stages 3 and 4 are collapsed
to single-function (the sinks and sources within one function form a trivial
call chain). Cross-substrate dedup and ground_truth are stubs. The API is
wide enough to grow into.
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import inspect
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Substrate = Literal["ir", "x86", "wasm"]
Severity = Literal["H", "M", "L"]
FindingStatus = Literal["raw", "triaged", "confirmed", "rejected"]
PSRole = Literal["source_scope", "propagation", "sink_scope"]


@dataclass
class CodeRef:
    """A location in a substrate, optionally resolved to source via DWARF."""
    substrate: Substrate
    fn: str
    loc: str                         # "+0xNN" (x86) | "%NNN" (IR) | "offset:N" (wasm)
    source_file: str | None = None
    source_line: int | None = None

    def pretty(self) -> str:
        s = f"{self.substrate}:{self.fn}{self.loc}"
        if self.source_file and self.source_line:
            s += f" ({Path(self.source_file).name}:{self.source_line})"
        return s


@dataclass
class FnSlice:
    """One compiled function, prompt-ready text + sidecar offset→source map."""
    fn: str
    substrate: Substrate
    text: str
    entry_addr: int | None = None
    offsets: list[tuple[int, str, int]] = field(default_factory=list)  # (off, file, line)
    opt_level: str = "-Os"

    def resolve(self, loc: str) -> tuple[str | None, int | None]:
        """Floor-lookup loc (e.g. '+0x13e6') to source (file, line)."""
        if not loc.startswith("+0x") or not self.offsets:
            return (None, None)
        try:
            target = int(loc[1:], 16)
        except ValueError:
            return (None, None)
        keys = [o[0] for o in self.offsets]
        idx = bisect.bisect_right(keys, target) - 1
        if idx < 0:
            return (None, None)
        off, fl, ln = self.offsets[idx]
        return (fl, ln)


@dataclass
class BuildSpec:
    """Minimal info to compile a harness against the target.

    Required only when ground_truth() is called; LATTE-style stages don't
    need it. `sources` is the list of .c files to link into the fuzzer
    binary (the target + any helpers). `cflags` defaults to `-O1 -g` so
    line numbers survive for ASan reports.
    """
    sources: list[Path]
    include_dirs: list[Path] = field(default_factory=list)
    cflags: list[str] = field(default_factory=lambda: ["-O1", "-g"])
    extra_libs: list[str] = field(default_factory=list)


@dataclass
class Target:
    module: Path
    opt_levels: list[str] = field(default_factory=lambda: ["-Os"])
    substrates: list[Substrate] = field(default_factory=lambda: ["x86"])
    sensitive_sinks: list[str] | None = None    # None → LLM-auto
    source_hints: list[str] | None = None       # None → LLM-auto
    build_spec: BuildSpec | None = None         # None → ground_truth skipped


@dataclass
class VulnerableDestination:        # LATTE "VD"
    call_site: CodeRef
    sink_fn: str
    rationale: str = ""


@dataclass
class CallChain:                    # LATTE "CC"
    vd: VulnerableDestination
    functions: list[str]            # sink-nearest last


@dataclass
class DangerousFlow:                # LATTE "DF"
    cc: CallChain
    source: CodeRef
    source_fn: str
    rationale: str = ""


@dataclass
class PSStep:
    fn: str
    role: PSRole
    prompt: str
    response: str
    usage: dict = field(default_factory=dict)


@dataclass
class PromptSequence:               # LATTE "PS"
    df: DangerousFlow
    steps: list[PSStep] = field(default_factory=list)
    verdict: str = ""               # final exploitability assessment


@dataclass
class Finding:
    id: str
    severity: Severity
    cwe: str
    fn: str
    substrate: Substrate
    evidence: list[CodeRef]
    trace: str
    confidence: float = 0.5
    status: FindingStatus = "raw"
    # Ozma extensions:
    substrate_agreement: list[Substrate] = field(default_factory=list)
    opt_level_agreement: list[str] = field(default_factory=list)
    ps: PromptSequence | None = None


@dataclass
class AuditReport:
    target: Target
    findings: list[Finding]
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    stages: dict[str, float] = field(default_factory=dict)   # stage → wall time


# ---------------------------------------------------------------------------
# Human-SotA verb inputs/outputs — variant analysis, fuzz synthesis, 1-day.
# ---------------------------------------------------------------------------

@dataclass
class VariantSeed:
    """Pattern template for find_variants(). Describes a known-bad shape."""
    pattern: str
    cwe: str | None = None
    example: CodeRef | None = None
    example_source: str | None = None      # small code snippet, if available
    severity_hint: Severity = "M"


@dataclass
class PatchSeed:
    """Upstream security patch used as input to patch_diff()."""
    url_or_path: str
    content: str | None = None             # if None, patch_diff() fetches
    cve: str | None = None


@dataclass
class FuzzHarness:
    """libFuzzer entry point synthesized from a Finding."""
    finding_id: str
    code: str
    entry_fn: str
    notes: str = ""                        # e.g. "UNREACHABLE" or build hints


@dataclass
class GroundTruthResult:
    finding_id: str
    status: Literal["confirmed", "rejected", "inconclusive"]
    crash_output: str = ""
    runtime_sec: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# LLM invocation contract
# ---------------------------------------------------------------------------

Stage = Literal[
    "sinks", "sources", "filter", "ps",
    "variant", "patch_extract", "harness",
]


@dataclass
class LLMRequest:
    """Structured request so adapters can apply caching / tuned max_tokens.

    The `cached_prefix` is a chunk of text that repeats across many calls
    (e.g. a disassembly dump referenced by 3+ stages for the same fn). An
    adapter can flag it with `cache_control` (Anthropic) or rely on
    automatic prefix caching (OpenAI). For providers that don't support
    either, concatenate and send as one prompt — `cached_prefix + prompt`.
    """
    prompt: str
    stage: Stage
    cached_prefix: str | None = None
    max_tokens: int | None = None

    def as_prompt(self) -> str:
        """Flatten back to a single string for adapters without caching."""
        if self.cached_prefix:
            return f"{self.cached_prefix}\n\n{self.prompt}"
        return self.prompt


# New contract: async + structured request.
AsyncLLMCallable = Callable[[LLMRequest], Awaitable[tuple[str, dict]]]

# Legacy contract (still accepted by AuditSession.__init__): sync str→tuple.
LLMCallable = Callable[[str], tuple[str, dict]]


def _wrap_llm(llm: Any) -> AsyncLLMCallable:
    """Adapt any of {sync-str, async-str, sync-request, async-request} to the
    internal AsyncLLMCallable. Detection is duck-typed by first-param
    annotation; async-ness via iscoroutinefunction. Legacy sync callables
    (`def fn(prompt: str) -> (str, dict)`) keep working unchanged.
    """
    is_async = inspect.iscoroutinefunction(llm)
    takes_request = False
    try:
        sig = inspect.signature(llm)
        first = next(iter(sig.parameters.values()), None)
        if first is not None:
            ann = first.annotation
            takes_request = ann is LLMRequest or (
                isinstance(ann, str) and ann.endswith("LLMRequest")
            )
    except (TypeError, ValueError):
        pass

    if is_async and takes_request:
        return llm                                        # native shape
    if is_async:
        async def wrap_async_str(req: LLMRequest) -> tuple[str, dict]:
            return await llm(req.as_prompt())
        return wrap_async_str
    if takes_request:
        async def wrap_sync_req(req: LLMRequest) -> tuple[str, dict]:
            return await asyncio.to_thread(llm, req)
        return wrap_sync_req

    async def wrap_sync_str(req: LLMRequest) -> tuple[str, dict]:
        return await asyncio.to_thread(llm, req.as_prompt())
    return wrap_sync_str


# Per-stage max_tokens hints — adapters SHOULD honor if the provider
# supports it. Kept small for filter/ps since outputs are tightly formatted.
_MAX_TOKENS: dict[Stage, int] = {
    "sinks": 800,
    "sources": 600,
    "filter": 20,
    "ps": 500,
    "variant": 500,
    "patch_extract": 400,
    "harness": 1500,
}


# ---------------------------------------------------------------------------
# Prompt templates — LATTE §3 style, kept concise
# ---------------------------------------------------------------------------

_SINK_PROMPT = """You are auditing a compiled function for taint-style
vulnerabilities. This is OPTIMIZED compiled code — sinks are usually
INLINED, not explicit calls. List every location reaching a
SECURITY-SENSITIVE sink, including ALL of the following patterns:

  * Explicit call/jmp to memcpy/memmove/strcpy/strncpy/sprintf/system/exec*
  * Inlined memory-copy: `rep movsb/movsq/stosb`, unrolled `mov [dst+i], ...`
    loops, SSE/AVX copies (`movdqu/movups`) into caller-provided buffers
  * Indirect dispatch: `call reg`, `jmp reg`, `jmp [reg+idx*8]` (dispatcher
    tables, vtable calls — indexed by potentially attacker data)
  * Byte/word stores through an attacker-influenced index or pointer:
    `mov [rdi+rcx], al`, `mov [rsi+rdx*1], r8b` etc.
  * Length-derived loop counters: `rep` with rcx from an input field,
    `sub rcx, N ; loop ...` where rcx traces to the input
  * Allocation sizing: computed size passed to malloc/calloc/realloc

Cite ONLY locations present in the listing. The disassembly is prefixed
with `+0xNN ` offsets — use those as the loc.

Output format, one line per VD:
  <loc>|<sink_fn_or_pattern>|<one-clause reason>

e.g.
  +0x1a2|rep movsb|unbounded copy, rcx from header byte at +0x14
  +0x3c|jmp rdi|indirect dispatch through computed target

Emit `NONE` only if the function is genuinely sink-free (getters, pure
arithmetic helpers). `rep movsb`, indexed stores, and `jmp reg` are
ALWAYS sinks in this framing — do not skip them.

## Function: {fn}  [{substrate}{opt}]

```
{text}
```
"""

_SOURCE_PROMPT = """You are auditing a compiled function for taint-style
vulnerabilities. Identify every location where EXTERNALLY-CONTROLLABLE data
enters this function — function parameters derived from attacker input,
loads from attacker-controlled buffers (e.g. ZIP/deflate header bytes),
or return values from funcs like recv/read/getenv/fgets.

Output format (one line per source):
  <loc>|<source_kind>|<one-clause reason>

Emit `NONE` if all data is internally sourced.

## Function: {fn}  [{substrate}{opt}]

```
{text}
```
"""

_PS_PROMPT = """You are performing the final stage of a LATTE-style taint
audit. A source has been identified at {source_loc}; a potentially vulnerable
destination has been identified at {sink_loc} in function {fn}. Your task:
trace whether attacker-controlled data from the source can reach the sink
WITHOUT sanitization (bounds check, length cap, sign normalization).

Answer in exactly this shape:

SEVERITY: <H|M|L|NONE>
CWE: <CWE-NN>
TRACE: <one-sentence dataflow from source to sink, citing at least 2 locs>
CONFIDENCE: <0.0-1.0>

Do NOT pattern-match. If you cannot trace the flow in the code below, emit
SEVERITY: NONE.

## Function: {fn}  [{substrate}{opt}]

```
{text}
```
"""


_FILTER_PROMPT = """You are a fast plausibility gate for a taint-audit
pipeline. Given a source and a sink in the same function, reply with ONE
word — YES if data from the source could plausibly reach the sink, NO
otherwise. Err on the side of YES unless clearly impossible (source after
sink in straight-line code, unrelated registers, etc.).

Source: {source_loc} ({source_kind})
Sink:   {sink_loc} ({sink_kind})

Function body (windowed around the flow):
```
{text}
```

Reply with exactly one word: YES or NO.
"""


_PS_MERGED_PROMPT = """You are performing the final stage of a LATTE-style
taint audit. A sink has been identified at {sink_loc} in function {fn}.
Multiple candidate sources have been flagged. Evaluate EACH source
independently: trace whether attacker-controlled data from that source can
reach the sink WITHOUT sanitization.

## Sink
{sink_loc} — {sink_kind}

## Candidate sources
{sources_block}

## Function body (windowed source→sink ±20 lines)
```
{text}
```

Output format — one line per source, pipe-delimited, verdict FIRST:

  <source_loc>|<H|M|L|NONE>|<CWE-NN>|<one-sentence trace>|<0.0-1.0 confidence>

Emit one line for every source listed above, in the same order. Use NONE
as the severity when the source cannot reach the sink without sanitization.
Do not add prose outside the lines.
"""


_VARIANT_PROMPT = """You are performing VARIANT ANALYSIS. A known bug pattern
is used as a template to find structurally-identical bugs elsewhere. This is
the technique that GitHub Security Lab and Project Zero use at scale.

## Known bug pattern
{pattern}
{example_block}
## Your task
Scan the function below and cite EVERY location whose shape matches the
pattern above. Do not flag unrelated bugs — be literal about structural
match. The disassembly is prefixed with `+0xNN ` offsets; cite those.

Output format, one line per variant:
  <loc>|<matched_element>|<why this matches>

Emit `NONE` if this function has no variants.

## Function: {fn}  [{substrate}{opt}]

```
{text}
```
"""

_HARNESS_PROMPT = """You are writing a libFuzzer harness to reproduce a
suspected vulnerability. The harness will be compiled with clang
`-fsanitize=fuzzer,address,undefined` and run for 30-60 seconds. If the
bug is real, ASan/UBSan will crash; if not, libFuzzer runs to timeout.

## Finding
Function: {fn}
Severity: {severity}    CWE: {cwe}
Trace: {trace}
Source cited: {cited_source}

## Target function context (source snippet around cited line)
```c
{signature_hint}
```

## Your task
Write exactly one `LLVMFuzzerTestOneInput` entry point in C. Constraints:

  * Derive EVERY attacker-controlled input from the `(data, size)` pair —
    do not invent constants that a real attacker couldn't supply.
  * If the function needs a context/state struct, initialize it with sane
    defaults and free/teardown before returning.
  * `#include` only what's necessary. Assume the target's header is
    available as if the source tree is on the include path.
  * Return 0 on success. No printf/fprintf/fflush.
  * If the finding is NOT reachable via the public API (private static
    function with no caller exposure), emit exactly:
        // UNREACHABLE: <one-line reason>
    and nothing else.

Output exactly one fenced ```c ... ``` block. No prose outside the block.
"""

_PATCH_EXTRACT_PROMPT = """You are analyzing a security patch to derive the
pre-patch bug shape, for use as a variant-analysis seed.

## Patch
```
{patch_content}
```

## Your task
In ≤6 lines total, describe:
  * What check/sanitization the patch adds
  * What the pre-patch code pattern looked like (the bug)
  * How an unpatched instance would be recognizable

Output exactly this structure:
PATTERN: <one paragraph describing the pre-patch pattern>
CWE: <CWE-NN, best guess>
SIGNATURE: <a short, recognizable code fragment of the pre-patch shape>
"""


# ---------------------------------------------------------------------------
# Parsers for the LLM responses
# ---------------------------------------------------------------------------

_LINE = re.compile(r"^\s*(\S+?)\s*\|\s*([^|]+?)\s*\|\s*(.*?)\s*$")
_FIELD = re.compile(r"^(SEVERITY|CWE|TRACE|CONFIDENCE):\s*(.*?)\s*$", re.MULTILINE)


def _parse_pipe_lines(text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("NONE"):
            continue
        m = _LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _parse_ps_verdict(text: str) -> dict[str, str]:
    fields = {k: v for k, v in _FIELD.findall(text)}
    return fields


_C_BLOCK = re.compile(r"```(?:c|C|cpp|c\+\+)?\s*\n(.*?)```", re.DOTALL)


def _extract_c_block(text: str) -> str:
    m = _C_BLOCK.search(text)
    return m.group(1).strip() if m else text.strip()


_PATCH_FIELD = re.compile(
    r"^(PATTERN|CWE|SIGNATURE):\s*(.*?)(?=\n(?:PATTERN|CWE|SIGNATURE):|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _parse_patch_extract(text: str) -> dict[str, str]:
    return {k: v.strip() for k, v in _PATCH_FIELD.findall(text)}


_LOC_PREFIX = re.compile(r"^\s*(\+0x[0-9a-fA-F]+)\b")


def _find_loc_line_index(lines: list[str], loc: str) -> int | None:
    """Return the index of the first disasm line whose leading offset equals loc."""
    for i, line in enumerate(lines):
        m = _LOC_PREFIX.match(line)
        if m and m.group(1).lower() == loc.lower():
            return i
    return None


def _window_text(text: str, locs: list[str], pad: int = 20) -> str:
    """Return a [min(locs)-pad, max(locs)+pad] slice of the disasm.

    Falls back to full text if any loc is missing. Preserves line ordering.
    When the window collapses to the whole function anyway, returns the
    original text unchanged so we don't waste the optimization.
    """
    if not locs:
        return text
    lines = text.splitlines()
    indices: list[int] = []
    for loc in locs:
        idx = _find_loc_line_index(lines, loc)
        if idx is None:
            return text
        indices.append(idx)
    lo, hi = min(indices), max(indices)
    start = max(0, lo - pad)
    end = min(len(lines), hi + pad + 1)
    if start == 0 and end == len(lines):
        return text
    return "\n".join(lines[start:end])


_MERGED_PS_LINE = re.compile(
    r"^\s*(\+0x[0-9a-fA-F]+)\s*\|\s*(H|M|L|NONE|HIGH|MEDIUM|LOW)\s*\|"
    r"\s*(CWE-[^|]+?)\s*\|\s*(.+?)\s*\|\s*([0-9.]+)\s*$",
    re.IGNORECASE,
)


def _parse_merged_ps(text: str) -> list[tuple[str, str, str, str, float]]:
    """Parse a merged-PS response. Tolerates loose numeric forms in confidence."""
    out: list[tuple[str, str, str, str, float]] = []
    for line in text.splitlines():
        m = _MERGED_PS_LINE.match(line)
        if not m:
            continue
        loc, sev, cwe, trace, conf_s = m.groups()
        sev = sev.upper()
        sev = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}.get(sev, sev)
        try:
            conf = float(conf_s)
        except ValueError:
            conf = 0.5
        out.append((loc, sev, cwe.strip(), trace.strip(), conf))
    return out


# ---------------------------------------------------------------------------
# AuditSession
# ---------------------------------------------------------------------------

class AuditSession:
    """One audit run. Owns the pipeline state, findings DB, and cost ledger.

    Efficiency knobs:
      * `llm_fast` drives pattern-recognition stages (sinks, sources, filter,
        variant sweeps). Use a cheap fast model here.
      * `llm_smart` drives reasoning stages (merged PS, harness synth, patch
        extract). Use your best model here.
      * Every stage fans out with `asyncio.gather`, bounded by a Semaphore
        of size `max_concurrency`.
      * Stage 5 groups DFs by (fn, sink) and runs a cheap YES/NO filter per
        group; surviving groups get ONE merged PS call with verdict-per-
        source output (not N calls).
      * PS prompts are windowed (source→sink ±20 disasm lines).
    """

    def __init__(
        self,
        target: Target,
        llm: LLMCallable | AsyncLLMCallable | None = None,
        *,
        llm_fast: LLMCallable | AsyncLLMCallable | None = None,
        llm_smart: LLMCallable | AsyncLLMCallable | None = None,
        budget_usd: float = 1.0,
        max_concurrency: int = 8,
    ):
        if llm_fast is None and llm_smart is None and llm is None:
            raise ValueError("AuditSession needs an llm= or llm_fast/llm_smart pair")
        self.target = target
        self.llm_fast = _wrap_llm(llm_fast or llm)
        self.llm_smart = _wrap_llm(llm_smart or llm or llm_fast)
        self.budget_usd = budget_usd
        self.cost_usd = 0.0
        self.stages: dict[str, float] = {}
        self.slices: list[FnSlice] = []
        self.vds: list[VulnerableDestination] = []
        self.ccs: list[CallChain] = []
        self.dfs: list[DangerousFlow] = []
        self.findings: list[Finding] = []
        self._sem = asyncio.Semaphore(max_concurrency)

    # -- LLM + budget plumbing ----------------------------------------------

    _SMART_STAGES: set[Stage] = {"ps", "harness", "patch_extract"}

    async def _acall(self, req: LLMRequest) -> str:
        if self.cost_usd >= self.budget_usd:
            raise RuntimeError(
                f"budget exhausted: ${self.cost_usd:.4f} >= ${self.budget_usd:.4f}"
            )
        if req.max_tokens is None:
            req.max_tokens = _MAX_TOKENS.get(req.stage)
        llm = self.llm_smart if req.stage in self._SMART_STAGES else self.llm_fast
        async with self._sem:
            text, usage = await llm(req)
        self.cost_usd += float(usage.get("cost", 0.0))
        return text

    def _time_stage(self, name: str, fn: Callable[[], Any]) -> Any:
        t0 = time.time()
        try:
            return fn()
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.time() - t0)

    async def _atime_stage(self, name: str, coro_fn: Callable[[], Awaitable[Any]]) -> Any:
        t0 = time.time()
        try:
            return await coro_fn()
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.time() - t0)

    # -- Stage 1: preprocess (pure-Python, no LLM) --------------------------

    def preprocess(self, slices: list[FnSlice]) -> list[FnSlice]:
        self.slices = slices
        return slices

    # -- Stage 2: identify sinks (VDs) — parallel across slices -------------

    async def _aidentify_sinks(self, slices: list[FnSlice]) -> list[VulnerableDestination]:
        async def per_slice(sl: FnSlice) -> list[VulnerableDestination]:
            opt = f" {sl.opt_level}" if sl.opt_level else ""
            # Cache the disasm prefix — it's sent by sinks/sources/PS for the
            # same fn. Adapters that support it will pay ingestion once.
            prompt_tail = _SINK_PROMPT.format(
                fn=sl.fn, substrate=sl.substrate, opt=opt, text="<<<DISASM>>>",
            ).replace("<<<DISASM>>>", "")
            req = LLMRequest(
                prompt=prompt_tail,
                stage="sinks",
                cached_prefix=f"## {sl.fn} [{sl.substrate}{opt}] disasm:\n```\n{sl.text}\n```",
            )
            resp = await self._acall(req)
            vds: list[VulnerableDestination] = []
            for loc, sink_fn, reason in _parse_pipe_lines(resp):
                ref = CodeRef(substrate=sl.substrate, fn=sl.fn, loc=loc)
                vds.append(VulnerableDestination(
                    call_site=ref, sink_fn=sink_fn, rationale=reason,
                ))
            return vds

        results = await asyncio.gather(*[per_slice(sl) for sl in slices])
        flat: list[VulnerableDestination] = [v for batch in results for v in batch]
        self.vds = flat
        return flat

    def identify_sinks(self, slices: list[FnSlice]) -> list[VulnerableDestination]:
        return asyncio.run(self._atime_stage(
            "identify_sinks", lambda: self._aidentify_sinks(slices)
        ))

    # -- Stage 3: construct call chains (MVP: single-function chains) -------

    def construct_call_chains(self, vds: list[VulnerableDestination]) -> list[CallChain]:
        def _go() -> list[CallChain]:
            self.ccs = [CallChain(vd=vd, functions=[vd.call_site.fn]) for vd in vds]
            return self.ccs
        return self._time_stage("construct_call_chains", _go)

    # -- Stage 4: identify sources → Dangerous Flows ------------------------

    async def _aidentify_sources(self, ccs: list[CallChain]) -> list[DangerousFlow]:
        by_fn = {sl.fn: sl for sl in self.slices}
        unique_fns = []
        seen = set()
        for cc in ccs:
            fn = cc.vd.call_site.fn
            if fn in seen or fn not in by_fn:
                continue
            seen.add(fn)
            unique_fns.append(fn)

        async def per_fn(fn: str) -> tuple[str, list[tuple[str, str, str]]]:
            sl = by_fn[fn]
            opt = f" {sl.opt_level}" if sl.opt_level else ""
            prompt_tail = _SOURCE_PROMPT.format(
                fn=sl.fn, substrate=sl.substrate, opt=opt, text="<<<DISASM>>>",
            ).replace("<<<DISASM>>>", "")
            req = LLMRequest(
                prompt=prompt_tail,
                stage="sources",
                cached_prefix=f"## {sl.fn} [{sl.substrate}{opt}] disasm:\n```\n{sl.text}\n```",
            )
            resp = await self._acall(req)
            return fn, _parse_pipe_lines(resp)

        results = await asyncio.gather(*[per_fn(fn) for fn in unique_fns])
        sources_by_fn = dict(results)

        dfs: list[DangerousFlow] = []
        for cc in ccs:
            fn = cc.vd.call_site.fn
            for loc, kind, reason in sources_by_fn.get(fn, []):
                src_ref = CodeRef(substrate=cc.vd.call_site.substrate, fn=fn, loc=loc)
                dfs.append(DangerousFlow(
                    cc=cc, source=src_ref, source_fn=fn, rationale=reason,
                ))
        self.dfs = dfs
        return dfs

    def identify_sources(self, ccs: list[CallChain]) -> list[DangerousFlow]:
        return asyncio.run(self._atime_stage(
            "identify_sources", lambda: self._aidentify_sources(ccs)
        ))

    # -- Stage 5: filter + merged PS, parallel across (fn, sink) groups -----

    async def _arun_prompt_sequences(
        self, dfs: list[DangerousFlow], slices: list[FnSlice] | None = None,
    ) -> list[Finding]:
        by_fn = {sl.fn: sl for sl in (slices or self.slices)}

        # Group DFs by (fn, sink.loc) so one merged PS call covers N sources.
        groups: dict[tuple[str, str], list[DangerousFlow]] = {}
        for df in dfs:
            fn = df.source_fn
            if fn not in by_fn:
                continue
            key = (fn, df.cc.vd.call_site.loc)
            groups.setdefault(key, []).append(df)

        async def process_group(
            key: tuple[str, str], group_dfs: list[DangerousFlow],
        ) -> list[Finding]:
            fn, sink_loc = key
            sl = by_fn[fn]
            sink_vd = group_dfs[0].cc.vd
            source_locs = [df.source.loc for df in group_dfs]
            # 1. Cheap YES/NO filter on the (fn, sink) group.
            window = _window_text(sl.text, source_locs + [sink_loc], pad=20)
            # Represent all sources as a single source list for the filter.
            filter_source = ", ".join(f"{df.source.loc} ({df.rationale or 'src'})" for df in group_dfs)
            freq = LLMRequest(
                prompt=_FILTER_PROMPT.format(
                    source_loc=filter_source,
                    source_kind="candidate",
                    sink_loc=sink_loc,
                    sink_kind=sink_vd.sink_fn,
                    text=window,
                ),
                stage="filter",
            )
            filter_resp = (await self._acall(freq)).strip().upper()
            if filter_resp.startswith("NO"):
                return []

            # 2. Merged PS: one call, verdict-per-source.
            sources_block = "\n".join(
                f"- {df.source.loc} ({df.rationale or 'candidate source'})"
                for df in group_dfs
            )
            opt = f" {sl.opt_level}" if sl.opt_level else ""
            preq = LLMRequest(
                prompt=_PS_MERGED_PROMPT.format(
                    sink_loc=sink_loc,
                    sink_kind=sink_vd.sink_fn,
                    fn=sl.fn,
                    sources_block=sources_block,
                    text=window,
                ) + f"\n\n[substrate: {sl.substrate}{opt}]",
                stage="ps",
                cached_prefix=f"## {sl.fn} [{sl.substrate}{opt}] windowed disasm:\n```\n{window}\n```",
            )
            resp = await self._acall(preq)
            findings: list[Finding] = []
            verdicts = _parse_merged_ps(resp)
            by_loc = {v[0].lower(): v for v in verdicts}
            for df in group_dfs:
                v = by_loc.get(df.source.loc.lower())
                if v is None:
                    continue
                _, sev, cwe, trace, conf = v
                if sev == "NONE":
                    continue
                if sev not in ("H", "M", "L"):
                    continue
                evidence = [df.source, df.cc.vd.call_site]
                fid = hashlib.sha1(
                    f"{sl.substrate}|{sl.fn}|{df.source.loc}|{sink_loc}|{cwe}".encode()
                ).hexdigest()[:12]
                ps = PromptSequence(
                    df=df,
                    steps=[PSStep(fn=sl.fn, role="sink_scope",
                                  prompt=preq.prompt, response=resp)],
                    verdict=sev,
                )
                findings.append(Finding(
                    id=fid,
                    severity=sev,  # type: ignore[arg-type]
                    cwe=cwe,
                    fn=sl.fn,
                    substrate=sl.substrate,
                    evidence=evidence,
                    trace=trace,
                    confidence=conf,
                    opt_level_agreement=[sl.opt_level],
                    ps=ps,
                ))
            return findings

        batches = await asyncio.gather(*[
            process_group(k, v) for k, v in groups.items()
        ])
        out = [f for batch in batches for f in batch]
        self.findings = out
        return out

    def run_prompt_sequences(
        self, dfs: list[DangerousFlow], slices: list[FnSlice] | None = None,
    ) -> list[Finding]:
        return asyncio.run(self._atime_stage(
            "run_prompt_sequences",
            lambda: self._arun_prompt_sequences(dfs, slices),
        ))

    # -- Post: resolve DWARF, dedupe (stub), report -------------------------

    def resolve(self, findings: list[Finding] | None = None) -> None:
        findings = findings if findings is not None else self.findings
        by_fn_sub = {(sl.fn, sl.substrate): sl for sl in self.slices}
        for f in findings:
            for ref in f.evidence:
                sl = by_fn_sub.get((ref.fn, ref.substrate))
                if sl is None:
                    continue
                src_file, src_line = sl.resolve(ref.loc)
                ref.source_file, ref.source_line = src_file, src_line

    def cross_substrate_dedupe(self) -> None:
        """Stub: MVP runs single-substrate. Promote once multi-substrate lands."""
        pass

    # -- Human-SotA verb: variant analysis ----------------------------------

    async def _afind_variants(
        self,
        seed: VariantSeed,
        slices: list[FnSlice] | None = None,
    ) -> list[Finding]:
        targets = slices if slices is not None else self.slices
        if seed.example or seed.example_source:
            ref_str = seed.example.pretty() if seed.example else "(unspecified)"
            src = seed.example_source or ""
            example_block = f"\n## Known-bad example\nLocation: {ref_str}\n```\n{src}\n```"
        else:
            example_block = ""

        async def per_slice(sl: FnSlice) -> list[Finding]:
            opt = f" {sl.opt_level}" if sl.opt_level else ""
            prompt_tail = _VARIANT_PROMPT.format(
                pattern=seed.pattern,
                example_block=example_block,
                fn=sl.fn, substrate=sl.substrate, opt=opt, text="<<<DISASM>>>",
            ).replace("<<<DISASM>>>", "")
            req = LLMRequest(
                prompt=prompt_tail,
                stage="variant",
                cached_prefix=f"## {sl.fn} [{sl.substrate}{opt}] disasm:\n```\n{sl.text}\n```",
            )
            resp = await self._acall(req)
            out: list[Finding] = []
            for loc, matched, reason in _parse_pipe_lines(resp):
                ref = CodeRef(substrate=sl.substrate, fn=sl.fn, loc=loc)
                fl, ln = sl.resolve(loc)
                ref.source_file, ref.source_line = fl, ln
                cwe = seed.cwe or "CWE-???"
                fid = hashlib.sha1(
                    f"{sl.substrate}|{sl.fn}|{loc}|variant|{cwe}".encode()
                ).hexdigest()[:12]
                out.append(Finding(
                    id=fid,
                    severity=seed.severity_hint,
                    cwe=cwe,
                    fn=sl.fn,
                    substrate=sl.substrate,
                    evidence=[ref],
                    trace=f"variant[{matched}]: {reason}",
                    confidence=0.5,
                    opt_level_agreement=[sl.opt_level],
                ))
            return out

        results = await asyncio.gather(*[per_slice(sl) for sl in targets])
        flat = [f for batch in results for f in batch]
        self.findings.extend(flat)
        return flat

    def find_variants(
        self,
        seed: VariantSeed,
        slices: list[FnSlice] | None = None,
    ) -> list[Finding]:
        return asyncio.run(self._atime_stage(
            "find_variants", lambda: self._afind_variants(seed, slices)
        ))

    # -- Human-SotA verb: 1-day hunting via patch diff ----------------------

    async def _apatch_diff(
        self,
        patch: PatchSeed,
        slices: list[FnSlice] | None = None,
    ) -> list[Finding]:
        content = patch.content if patch.content is not None else _fetch_patch(patch.url_or_path)
        if not content:
            return []
        req = LLMRequest(
            prompt=_PATCH_EXTRACT_PROMPT.format(patch_content=content[:6000]),
            stage="patch_extract",
        )
        resp = await self._acall(req)
        fields = _parse_patch_extract(resp)
        if "PATTERN" not in fields:
            return []
        seed = VariantSeed(
            pattern=fields["PATTERN"],
            cwe=fields.get("CWE") or patch.cve or "CWE-???",
            example_source=fields.get("SIGNATURE"),
            severity_hint="M",
        )
        return await self._afind_variants(seed, slices=slices)

    def patch_diff(
        self,
        patch: PatchSeed,
        slices: list[FnSlice] | None = None,
    ) -> list[Finding]:
        return asyncio.run(self._atime_stage(
            "patch_diff", lambda: self._apatch_diff(patch, slices)
        ))

    # -- Human-SotA verb: libFuzzer harness synth + ASan ground truth -------

    async def _asynthesize_harness(self, finding: Finding) -> FuzzHarness:
        signature_hint = _signature_hint_for(finding)
        cited_source = ", ".join(
            r.pretty() for r in finding.evidence if r.source_file
        ) or "(no DWARF resolution)"
        req = LLMRequest(
            prompt=_HARNESS_PROMPT.format(
                fn=finding.fn,
                severity=finding.severity,
                cwe=finding.cwe,
                trace=finding.trace,
                cited_source=cited_source,
                signature_hint=signature_hint,
            ),
            stage="harness",
        )
        resp = await self._acall(req)
        code = _extract_c_block(resp)
        notes = "UNREACHABLE" if "UNREACHABLE" in code else ""
        return FuzzHarness(
            finding_id=finding.id,
            code=code,
            entry_fn=finding.fn,
            notes=notes,
        )

    def synthesize_harness(self, finding: Finding) -> FuzzHarness:
        return asyncio.run(self._atime_stage(
            "synthesize_harness", lambda: self._asynthesize_harness(finding)
        ))

    async def _aground_truth(
        self,
        findings: list[Finding] | None = None,
        timeout_sec: int = 60,
        workdir: Path | None = None,
    ) -> list[GroundTruthResult]:
        targets = findings if findings is not None else self.findings
        spec = self.target.build_spec
        if spec is None:
            return [
                GroundTruthResult(
                    finding_id=f.id, status="inconclusive",
                    reason="Target.build_spec not set",
                )
                for f in targets
            ]
        if not shutil.which("clang"):
            return [
                GroundTruthResult(
                    finding_id=f.id, status="inconclusive",
                    reason="clang not on PATH",
                )
                for f in targets
            ]
        wd = workdir or Path(tempfile.mkdtemp(prefix="audit_gt_"))
        wd.mkdir(parents=True, exist_ok=True)

        harnesses = await asyncio.gather(*[
            self._asynthesize_harness(f) for f in targets
        ])

        async def build_and_run(f: Finding, h: FuzzHarness) -> GroundTruthResult:
            if h.notes == "UNREACHABLE":
                return GroundTruthResult(
                    finding_id=f.id, status="inconclusive",
                    reason="harness marked UNREACHABLE by LLM",
                )
            return await asyncio.to_thread(
                _compile_and_run_harness, h, spec, wd, timeout_sec,
            )

        results = await asyncio.gather(*[
            build_and_run(f, h) for f, h in zip(targets, harnesses)
        ])
        self._apply_ground_truth(list(results))
        return list(results)

    def ground_truth(
        self,
        findings: list[Finding] | None = None,
        timeout_sec: int = 60,
        workdir: Path | None = None,
    ) -> list[GroundTruthResult]:
        return asyncio.run(self._atime_stage(
            "ground_truth",
            lambda: self._aground_truth(findings, timeout_sec, workdir),
        ))

    def _apply_ground_truth(self, results: list[GroundTruthResult]) -> None:
        by_id = {f.id: f for f in self.findings}
        for r in results:
            f = by_id.get(r.finding_id)
            if f is None:
                continue
            if r.status == "confirmed":
                f.status = "confirmed"
            elif r.status == "rejected":
                f.status = "rejected"

    def report(self) -> AuditReport:
        return AuditReport(
            target=self.target,
            findings=self.findings,
            cost_usd=self.cost_usd,
            duration_sec=sum(self.stages.values()),
            stages=dict(self.stages),
        )

    # -- One-shot orchestration ---------------------------------------------

    async def arun(self, slices: list[FnSlice]) -> AuditReport:
        self.preprocess(slices)
        vds = await self._atime_stage(
            "identify_sinks", lambda: self._aidentify_sinks(self.slices)
        )

        async def _build_ccs() -> list[CallChain]:
            ccs = [CallChain(vd=vd, functions=[vd.call_site.fn]) for vd in vds]
            self.ccs = ccs
            return ccs

        ccs = await self._atime_stage("construct_call_chains", _build_ccs)
        dfs = await self._atime_stage(
            "identify_sources", lambda: self._aidentify_sources(ccs)
        )
        await self._atime_stage(
            "run_prompt_sequences",
            lambda: self._arun_prompt_sequences(dfs),
        )
        self.resolve()
        self.cross_substrate_dedupe()
        return self.report()

    def run(self, slices: list[FnSlice]) -> AuditReport:
        return asyncio.run(self.arun(slices))


# ---------------------------------------------------------------------------
# Slice loader — reads the `+0xNN <insn>` prompt format + `.map.json` sidecar
# produced by the `build_x86_fns.py`-style splitter.
# ---------------------------------------------------------------------------

def _fetch_patch(url_or_path: str) -> str | None:
    """Load a patch from a local path or http(s) URL. Returns None on failure."""
    p = Path(url_or_path)
    if p.exists():
        try:
            return p.read_text()
        except OSError:
            return None
    if url_or_path.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(url_or_path, timeout=30) as r:  # noqa: S310
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


def _signature_hint_for(finding: Finding) -> str:
    """Pull ±10 lines of C source around the first DWARF-resolved evidence ref.

    If no resolution is available, return a short placeholder — the LLM can
    still produce a harness from the trace text alone, just less accurately.
    """
    for ref in finding.evidence:
        if not (ref.source_file and ref.source_line):
            continue
        path = Path(ref.source_file)
        if not path.exists():
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        start = max(0, ref.source_line - 10)
        end = min(len(lines), ref.source_line + 3)
        numbered = [f"{i + 1:5d}  {lines[i]}" for i in range(start, end)]
        return "\n".join(numbered)
    return f"// source not resolved — function: {finding.fn}"


def _compile_and_run_harness(
    harness: FuzzHarness,
    spec: BuildSpec,
    workdir: Path,
    timeout_sec: int,
) -> GroundTruthResult:
    """Compile a harness with clang+ASan+UBSan and run libFuzzer for timeout_sec.

    Return values follow GroundTruthResult.status semantics — compile
    failures and hangs map to `inconclusive` (not rejected), so the LLM's
    harness mistakes don't get mistaken for clean runs.
    """
    harness_src = workdir / f"harness_{harness.finding_id}.c"
    harness_src.write_text(harness.code)
    binary = workdir / f"harness_{harness.finding_id}"
    cmd = [
        "clang",
        "-fsanitize=fuzzer,address,undefined",
        "-g",
        *spec.cflags,
        *[f"-I{p}" for p in spec.include_dirs],
        str(harness_src),
        *[str(s) for s in spec.sources],
        *spec.extra_libs,
        "-o", str(binary),
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return GroundTruthResult(
            finding_id=harness.finding_id, status="inconclusive",
            reason="compile timed out",
        )
    if cp.returncode != 0:
        return GroundTruthResult(
            finding_id=harness.finding_id, status="inconclusive",
            reason="compile failed",
            crash_output=(cp.stderr or "")[-2000:],
        )

    t0 = time.time()
    try:
        run = subprocess.run(
            [str(binary),
             f"-max_total_time={timeout_sec}",
             "-print_final_stats=1"],
            capture_output=True, text=True, timeout=timeout_sec + 30,
        )
    except subprocess.TimeoutExpired as e:
        return GroundTruthResult(
            finding_id=harness.finding_id, status="inconclusive",
            runtime_sec=time.time() - t0,
            reason="libFuzzer hung past deadline",
            crash_output=(e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))[-2000:],
        )
    runtime = time.time() - t0
    output = (run.stderr or "") + (run.stdout or "")
    sanitizer_hit = (
        "ERROR: AddressSanitizer" in output
        or "ERROR: UndefinedBehaviorSanitizer" in output
        or "runtime error:" in output
        or "SUMMARY: AddressSanitizer" in output
    )
    if run.returncode != 0 and sanitizer_hit:
        return GroundTruthResult(
            finding_id=harness.finding_id, status="confirmed",
            runtime_sec=runtime,
            crash_output=output[-3000:],
        )
    return GroundTruthResult(
        finding_id=harness.finding_id, status="rejected",
        runtime_sec=runtime,
        reason="ran to timeout without crash",
        crash_output=output[-3000:],
    )


def load_x86_slice(asm_path: Path, map_path: Path, opt_level: str = "-Os") -> FnSlice:
    text = Path(asm_path).read_text()
    sidecar = json.loads(Path(map_path).read_text())
    offsets = [(int(o, 16), fl, ln) for o, fl, ln in sidecar.get("offsets", [])]
    offsets.sort(key=lambda t: t[0])
    return FnSlice(
        fn=sidecar["fn"],
        substrate="x86",
        text=text,
        entry_addr=sidecar.get("entry_addr"),
        offsets=offsets,
        opt_level=opt_level,
    )
