"""
code_review.py — First-class code review verb for the Ozma protocol.

Mirrors the shape of audit.py (async, fast/smart routing, prompt caching,
filter gates, merged batch calls) but operates on source-code diffs rather
than compiled slices. Built on four lessons from the 2025 LLM code review
literature:

  * **Classify/route first** (CodeRabbit) — small model triages hunks into
    {cosmetic, docs, logic, security, test}. Cosmetic+docs skip the
    expensive model entirely.
  * **Checklist-driven review** (Sphinx, arXiv 2601.04252) — generate
    verification items from the diff, then score against them. More
    interpretable than free-form critique; beats BLEU/ROUGE evaluation.
  * **Context is king** (ContextCRBench, arXiv 2511.07017) — textual
    context (PR description, linked issue) outperforms code context alone.
    Enclosing fn via tree-sitter, not full file.
  * **Linters + LLM combo** (CodeRabbit) — deterministic tools handle
    style/docs as seeded findings; LLM focuses on semantic/security.

Ozma-specific additions on top:

  * Fired hints (regex, zero-cost) inject expert guidance into the review
    prompt via the standard fenced-markdown format.
  * Linter findings (via ozmalib.checks) are free deterministic ground
    truth AND context for the review model.
  * Tree-sitter stubs (via ozmalib.compress) compress cross-file context.

Pipeline:

    1. load_diff()          — unified diff → hunks
    2. run_linters()        — local, per file, free
    3. check_hints()        — regex fire per hunk, free
    4. classify_hunks()     — fast, merged batch
    5. assemble_context()   — local tree-sitter
    6. filter_hunks()       — fast YES/NO worth-reviewing
    7. generate_checklist() — fast, per surviving hunk
    8. run_review()         — smart, per hunk (context+hints+linter+checklist)
    9. multi_pass()         — optional, smart × N
    10. score_and_filter()  — local dedupe + threshold

Stages 4/6/7/8/9 run concurrently under a shared semaphore.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

HunkCategory = Literal["cosmetic", "docs", "logic", "security", "test", "config", "unknown"]
ReviewSeverity = Literal["critical", "high", "medium", "low", "nitpick"]
CommentStatus = Literal[
    "raw", "confirmed", "suppressed",
    "adjudicated-keep", "adjudicated-drop", "adjudicated-rescored",
]
AdjudicationVerdict = Literal["keep", "drop", "downgrade", "upgrade"]
Stage = Literal[
    "linters", "classify", "filter", "checklist", "review", "aggregate",
    "adjudicate",
]


@dataclass
class ReviewTarget:
    """What's being reviewed.

    Two modes:

      * **Diff mode** (default): `base`/`head` (or `diff_text`) define a
        unified diff. Hunks come from the parser.
      * **Whole-file/codebase mode**: set `paths` to a list of files or
        directories. Each file is synthesised into hunks via tree-sitter
        (`chunk_mode="function"`) or kept whole (`chunk_mode="file"`).

    `pr_description` and `linked_issue_body` are textual intent — the #1
    predictor of review quality per ContextCRBench. In whole-file mode,
    treat `pr_description` as "what the reviewer should be looking for"
    (e.g. "security audit of auth surface").
    """
    repo: Path
    base: str = "main"
    head: str = "HEAD"
    pr_description: str = ""
    linked_issue_body: str = ""
    diff_text: str | None = None       # if None, computed via git diff
    # Whole-file / codebase mode. Files or directories (recursed).
    paths: list[Path] | None = None
    chunk_mode: Literal["file", "function"] = "function"


@dataclass
class Hunk:
    """One hunk from a unified diff, with enough metadata to locate it."""
    file: str                          # new path (post-change)
    old_file: str                      # for renames/deletes
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    content: str                       # the `@@` header + +/-/space lines
    language: str = "unknown"
    is_new_file: bool = False
    is_deleted: bool = False
    is_rename: bool = False

    @property
    def id(self) -> str:
        raw = f"{self.file}|{self.new_start}|{self.new_count}".encode()
        return hashlib.sha1(raw).hexdigest()[:12]

    @property
    def changed_text(self) -> str:
        """Just the +/- lines (no context), prefix-stripped."""
        out = []
        for ln in self.content.splitlines():
            if ln[:1] in ("+", "-") and ln[:3] not in ("+++", "---"):
                out.append(ln)
        return "\n".join(out)


@dataclass
class LinterFinding:
    """Free deterministic finding from a regex check or subprocess linter."""
    file: str
    line: int | None
    name: str                          # e.g. "fstring-sql" or "ruff:E501"
    severity: str                      # "critical", "high", "medium", "low"
    message: str
    source: str = "check"              # "check" | "ruff" | "mypy" | "eslint"
    type: str = ""                     # "security", "correctness", "quality", ...


@dataclass
class HunkContext:
    """Context pack assembled for one hunk. Generous by default."""
    hunk: Hunk
    enclosing_fn: str = ""             # full function/class (not stubbed)
    file_stubs: str = ""               # stubs of the rest of the file
    caller_stubs: str = ""             # stubs of callers in same package (best-effort)
    hints_block: str = ""              # format_hints_qualified() output
    fired_hint_ids: list[str] = field(default_factory=list)  # ids that matched this hunk
    linter_findings: list[LinterFinding] = field(default_factory=list)
    pr_description: str = ""
    linked_issue: str = ""


@dataclass
class AdjudicationDecision:
    """One adjudicator verdict on a single review comment.

    The adjudicator sees the comment plus the hunk, enclosing function, and
    the full guidance of any hints the reviewer cited. It makes the call a
    senior engineer would on the same evidence: keep, drop (false positive),
    or re-score (downgrade/upgrade). Which model runs this stage is a tuning
    decision — routes through the same pool as classify/filter by default;
    callers can aim it at a stronger model when empirical data says the
    cheap one over-drops or over-keeps.
    """
    verdict: AdjudicationVerdict       # keep | drop | downgrade | upgrade
    new_severity: str = ""             # set when verdict in {downgrade, upgrade}
    rationale: str = ""                # why — one or two sentences
    confidence: float = 0.5


@dataclass
class Suppression:
    """One persisted adjudicator DROP. Suppresses matching future findings.

    Lives in a `.hints.yaml` file in the same directory as the reviewed
    source. Shape intentionally flat so humans can edit or delete entries
    to un-suppress a finding.
    """
    file: str                          # basename — file must sit next to .hints.yaml
    signature: str                     # _comment_signature() of the dropped text
    comment: str                       # original comment, for human review
    hint_ids: list[str] = field(default_factory=list)
    category: str = ""
    severity: str = ""
    rationale: str = ""                # why the adjudicator dropped it
    dropped_at: str = ""               # ISO8601 UTC


@dataclass
class ReviewComment:
    """One review comment. Mirrors audit.Finding but for source code."""
    id: str
    file: str
    line: int                          # 1-indexed new-file line
    severity: ReviewSeverity
    category: HunkCategory             # what class of issue
    cwe: str = ""                      # "" when non-security
    comment: str = ""                  # the actual review text
    confidence: float = 0.5
    status: CommentStatus = "raw"
    source: str = "llm"                # "llm" | "linter" | "hint"
    linter_name: str = ""              # set when source=linter
    hint_ids: list[str] = field(default_factory=list)  # attributed hint rule names
    adjudication: AdjudicationDecision | None = None  # set when adjudicator runs


@dataclass
class ReviewReport:
    target: ReviewTarget
    comments: list[ReviewComment]
    linter_findings: list[LinterFinding]
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    stages: dict[str, float] = field(default_factory=dict)
    hunk_categories: dict[str, HunkCategory] = field(default_factory=dict)
    suppressions_written: list[Path] = field(default_factory=list)
    suppressed_count: int = 0         # pre-adjudicator drops via .hints.yaml


# ---------------------------------------------------------------------------
# LLM invocation contract — identical shape to audit.py
# ---------------------------------------------------------------------------

@dataclass
class LLMRequest:
    """Structured request so adapters can apply caching / tuned max_tokens.

    The `cached_prefix` is text repeated across calls (repo map, PR
    description). Adapters flag it via `cache_control` (Anthropic) or rely
    on automatic prefix caching (OpenAI).
    """
    prompt: str
    stage: Stage
    cached_prefix: str | None = None
    max_tokens: int | None = None

    def as_prompt(self) -> str:
        if self.cached_prefix:
            return f"{self.cached_prefix}\n\n{self.prompt}"
        return self.prompt


AsyncLLMCallable = Callable[[LLMRequest], Awaitable[tuple[str, dict]]]
LLMCallable = Callable[[str], tuple[str, dict]]


def _wrap_llm(llm: Any) -> AsyncLLMCallable:
    """Adapt {sync-str, async-str, sync-request, async-request} → AsyncLLMCallable."""
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
        return llm
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


_MAX_TOKENS: dict[Stage, int] = {
    "linters": 0,                      # local, no LLM
    "classify": 400,                   # merged — scales ~20 tokens/hunk
    "filter": 20,
    "checklist": 300,
    "review": 800,
    "aggregate": 800,
    "adjudicate": 400,                 # one verdict + short rationale
}


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """You are the triage stage of a code review pipeline.
Classify each hunk into exactly ONE category:

  cosmetic  — whitespace, formatting, rename-only, reordering
  docs      — comments, docstrings, README/markdown
  test      — test files only (no production change)
  config    — lockfiles, requirements, dependency bumps, yaml/toml infra
  logic    — behaviour-changing code in production files
  security — code touching auth, crypto, SQL, input parsing, file I/O,
             network, subprocess/exec, deserialization, secrets

When in doubt between logic and security, pick security. When in doubt
between logic and cosmetic, pick logic.

## Hunks
{hunks_block}

Output format — one line per hunk, in the same order as above:
  <hunk_id>|<category>|<one-clause reason>

e.g.
  abc123def456|security|adds raw SQL concatenation in query builder
  789abc012def|cosmetic|rename variable; no behaviour change
"""


_FILTER_PROMPT = """You are a fast worth-reviewing gate for a code review
pipeline. Reply with ONE WORD — YES if this hunk could contain a real bug
or security issue worth flagging, NO if it's clearly trivial (pure rename,
import reorder, whitespace, minor type annotation with no semantic impact).

Err on the side of YES unless obviously trivial.

## Hunk
```{language}
{hunk_content}
```

Reply with exactly one word: YES or NO.
"""


_CHECKLIST_PROMPT = """You are the checklist stage of a code review. Given
a hunk + its context, emit a short list of verification questions a senior
reviewer would ask. Keep each item as a single sentence ending in '?'.

Focus on functional concerns (logic, security, error handling, concurrency,
boundary cases). Skip style/formatting — a linter handles those.

## Context — PR intent
{pr_intent}

## Context — enclosing function
```{language}
{enclosing_fn}
```

## Hunk
```{language}
{hunk_content}
```

Output format — up to 8 single-sentence questions, each prefixed with `- `:
- Does the new branch at line X handle the Y == 0 case?
- Is the buffer at line Z bounded before the memcpy?

Emit exactly the questions, no preamble.
"""


_REVIEW_PROMPT = """You are a senior engineer conducting a code review. You
have: the hunk, the enclosing function, the PR intent, any linter findings
already reported, and a verification checklist. Produce review comments
ONLY for real issues a human reviewer would flag.

Rules:
  * Cite line numbers from the NEW file (post-change). Use the line
    numbers embedded in the diff's `@@` header.
  * For each issue, give a severity (critical/high/medium/low/nitpick)
    and a category (logic/security/test/config/cosmetic/docs).
  * If the issue is security-relevant, include a CWE (best guess).
  * Do NOT restate what linters already flagged — focus on what they
    MISSED (semantic/logic issues).
  * Do NOT comment on style — linter territory.
  * If the hunk looks correct given the context, emit NONE.
  * If a finding directly relates to one of the fired project hints listed
    below, append `[[hint-id]]` (or `[[id1,id2]]` for multiple) to the end
    of the comment text so the adjudicator can cite the rule.

## Fired project hints (cite by id when relevant)
{available_hints}

## PR intent
{pr_intent}

## Linter findings already reported (do not duplicate)
{linter_block}

## Verification checklist (use as prompts, not as required output)
{checklist}

## Enclosing function (for context)
```{language}
{enclosing_fn}
```

## The hunk under review
```{language}
{hunk_content}
```

Output format — one line per comment, pipe-delimited:

  <new_file_line>|<severity>|<category>|<CWE-NN or ->|<comment text>|<0.0-1.0 confidence>

e.g.
  142|high|security|CWE-89|SQL is concatenated from user input; use parameterised query [[owasp-sql-injection]]|0.9
  157|medium|logic|-|off-by-one: loop should use `<=` since end is inclusive|0.7

Emit `NONE` (one line, uppercase) if the hunk has no real issues.
"""


_AGGREGATE_PROMPT = """You are the aggregator for a multi-pass review. N
independent reviewers each produced a set of comments on the same diff.
Merge them into one final set:

  * Deduplicate by (file, line, category) — keep the most detailed version.
  * Downgrade severity when only 1/N reviewers flagged it AND the others
    were silent on that location.
  * Upgrade confidence when 2+ reviewers independently flagged the same
    (file, line, category).
  * Drop comments that contradict each other at the same location.
  * PRESERVE any `[[hint-id]]` markers in comments — they attribute the
    finding to a project rule. Union ids when merging duplicates.

## Reviewer outputs
{reviews_block}

Output format — same as input (one line per comment):
  <line>|<severity>|<category>|<CWE or ->|<comment>|<confidence>

Emit `NONE` if no comments survive.
"""


_ADJUDICATE_PROMPT = """You are the adjudicator for a code-review pipeline.
A reviewer flagged ONE finding. Your job is to decide — with the full
context — whether a senior engineer would keep it, drop it as a false
positive, or re-score it.

Rules:
  * KEEP if the finding is correct AND material.
  * DROP if the finding is wrong, superseded by context, or non-material
    (code is idiomatic in this project, guarded upstream, covered by tests,
    a test fixture, a migration, a generated file, etc.).
  * DOWNGRADE if the concern is real but over-severe for this context.
  * UPGRADE if the reviewer under-called it (e.g. labelled "low" on a
    critical security hole).
  * Use the cited hint rules — their full guidance is below — as authority
    on whether the style/correctness concern is actually a project rule.
  * Be strict about DROPping — if in doubt, KEEP.

## The finding under adjudication
file: {file}
line: {line}
severity: {severity}
category: {category}
CWE: {cwe}
comment: {comment}
cited hints: {hint_ids}

## Cited hint guidance (authority for rule-based findings)
{hint_guidance}

## Enclosing function (for context)
```{language}
{enclosing_fn}
```

## The hunk the finding points at
```{language}
{hunk_content}
```

## PR intent
{pr_intent}

Output EXACTLY one line, pipe-delimited:

  <verdict>|<new_severity or ->|<rationale>|<0.0-1.0 confidence>

Where verdict is one of: keep, drop, downgrade, upgrade
And new_severity is one of: critical, high, medium, low, nitpick, or `-`
(only set when verdict is downgrade or upgrade).

e.g.
  keep|-|SQL concat from a request param is a real CWE-89; PR intent does not justify it|0.95
  drop|-|This is a test fixture; the "hardcoded password" is expected|0.9
  downgrade|low|Real issue but only reachable via admin tooling; not production-path|0.75
"""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_CLASSIFY_LINE = re.compile(
    r"^\s*([0-9a-f]{6,16})\s*\|\s*(\w+)\s*\|\s*(.+?)\s*$"
)

_REVIEW_LINE = re.compile(
    r"^\s*(\d+)\s*\|\s*(critical|high|medium|low|nitpick)\s*\|"
    r"\s*(\w+)\s*\|\s*(CWE-[\w\-]+|-)\s*\|\s*(.+?)\s*\|\s*([0-9.]+)\s*$",
    re.IGNORECASE,
)


def _parse_classify(text: str) -> dict[str, tuple[HunkCategory, str]]:
    out: dict[str, tuple[HunkCategory, str]] = {}
    valid: set[HunkCategory] = {
        "cosmetic", "docs", "logic", "security", "test", "config", "unknown",
    }
    for line in text.splitlines():
        m = _CLASSIFY_LINE.match(line)
        if not m:
            continue
        hid, cat, reason = m.groups()
        cat = cat.lower()
        if cat not in valid:
            cat = "unknown"
        out[hid] = (cat, reason.strip())  # type: ignore[arg-type]
    return out


_HINT_MARKER = re.compile(r"\[\[\s*([a-zA-Z0-9_\-,\s]+?)\s*\]\]")


_ADJUDICATE_LINE = re.compile(
    r"^\s*(keep|drop|downgrade|upgrade)\s*\|"
    r"\s*(critical|high|medium|low|nitpick|-)\s*\|"
    r"\s*(.+?)\s*\|\s*([0-9.]+)\s*$",
    re.IGNORECASE,
)


def _parse_adjudication(text: str) -> AdjudicationDecision | None:
    """Parse a single adjudicator response. Tolerant to surrounding noise."""
    for line in text.splitlines():
        m = _ADJUDICATE_LINE.match(line)
        if not m:
            continue
        verdict_s, sev_s, rationale, conf_s = m.groups()
        try:
            conf = float(conf_s)
        except ValueError:
            continue
        verdict = verdict_s.lower()
        new_sev = "" if sev_s.strip() == "-" else sev_s.lower()
        return AdjudicationDecision(
            verdict=verdict,  # type: ignore[arg-type]
            new_severity=new_sev,
            rationale=rationale.strip(),
            confidence=conf,
        )
    return None


def _extract_hint_ids(comment: str) -> tuple[str, list[str]]:
    """Strip `[[id1,id2]]` markers from a comment; return (clean, ids)."""
    ids: list[str] = []
    def collect(m: re.Match[str]) -> str:
        for raw in m.group(1).split(","):
            name = raw.strip()
            if name:
                ids.append(name)
        return ""
    clean = _HINT_MARKER.sub(collect, comment).strip()
    # dedupe, preserve order
    seen: set[str] = set()
    uniq = [i for i in ids if not (i in seen or seen.add(i))]
    return clean, uniq


def _parse_review(
    text: str,
) -> list[tuple[int, str, str, str, str, float, list[str]]]:
    """Return list of (line, severity, category, cwe, comment, confidence, hint_ids)."""
    out: list[tuple[int, str, str, str, str, float, list[str]]] = []
    for line in text.splitlines():
        if line.strip().upper() == "NONE":
            return []
        m = _REVIEW_LINE.match(line)
        if not m:
            continue
        ln_s, sev, cat, cwe, comment, conf_s = m.groups()
        try:
            ln = int(ln_s)
            conf = float(conf_s)
        except ValueError:
            continue
        sev = sev.lower()
        cat = cat.lower()
        if cwe.strip() == "-":
            cwe = ""
        clean_comment, hint_ids = _extract_hint_ids(comment.strip())
        out.append((ln, sev, cat, cwe, clean_comment, conf, hint_ids))
    return out


# ---------------------------------------------------------------------------
# Local helpers — diff parsing, context assembly, linter wrappers
# ---------------------------------------------------------------------------

def _fetch_diff(target: ReviewTarget) -> str:
    """Return the unified diff for a target. Uses git diff when diff_text is unset."""
    if target.diff_text is not None:
        return target.diff_text
    try:
        cp = subprocess.run(
            ["git", "diff", "--unified=5", f"{target.base}...{target.head}"],
            cwd=target.repo, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return cp.stdout if cp.returncode == 0 else ""


def _detect_lang_for_file(path: str) -> str:
    try:
        from ozmalib.compress.tree import _detect_language
        return _detect_language(path)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Suppressions — adjudicator DROPs persist as per-directory .hints.yaml
# ---------------------------------------------------------------------------

_SUPPRESSIONS_FILENAME = ".hints.yaml"

_SUPPRESSIONS_HEADER = (
    "# Auto-generated by the ozma code review adjudicator.\n"
    "# Each entry is a finding a past review dropped as a false positive.\n"
    "# Matching future findings are skipped before the review LLM runs.\n"
    "# Delete an entry to un-suppress that finding.\n"
)


def _comment_signature(text: str) -> str:
    """Stable 12-char fingerprint of a comment, for suppression matching."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def _suppression_path_for(repo_root: Path, file_relpath: str) -> Path:
    """Where the `.hints.yaml` for a given reviewed file lives.

    File-local: sits in the SAME directory as the source. Moving the source
    to a different directory resets its suppressions — intentional, since
    context changes with location.
    """
    return (repo_root / file_relpath).parent / _SUPPRESSIONS_FILENAME


def _load_suppressions_from(path: Path) -> list[Suppression]:
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore[import-not-found]
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: list[Suppression] = []
    for raw in (data.get("suppressions") or []):
        if not isinstance(raw, dict):
            continue
        try:
            out.append(Suppression(
                file=str(raw.get("file", "")),
                signature=str(raw.get("signature", "")),
                comment=str(raw.get("comment", "")),
                hint_ids=list(raw.get("hint_ids") or []),
                category=str(raw.get("category", "")),
                severity=str(raw.get("severity", "")),
                rationale=str(raw.get("rationale", "")),
                dropped_at=str(raw.get("dropped_at", "")),
            ))
        except Exception:
            continue
    return out


def _comment_matches_suppression(c: ReviewComment, s: Suppression) -> bool:
    """A finding is suppressed if the file+category match AND either the
    cited hints overlap OR the normalized text signature is identical.

    Category match is a safety gate — a stored `cosmetic` drop must not
    silence a future `security` finding even if they share a hint id.
    """
    if Path(s.file).name != Path(c.file).name:
        return False
    if s.category and c.category and s.category != c.category:
        return False
    if s.hint_ids and c.hint_ids and set(s.hint_ids) & set(c.hint_ids):
        return True
    if s.signature and s.signature == _comment_signature(c.comment):
        return True
    return False


def _persist_suppression(
    repo_root: Path,
    comment: ReviewComment,
    decision: AdjudicationDecision,
) -> Path | None:
    """Append a DROP verdict to the `.hints.yaml` next to the reviewed file.

    Idempotent — existing entry with the same signature is a no-op.
    Returns the file path, or None if yaml isn't importable.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None

    path = _suppression_path_for(repo_root, comment.file)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text())
            if isinstance(loaded, dict):
                existing_data = loaded
        except Exception:
            existing_data = {}

    entries: list[dict[str, Any]] = list(existing_data.get("suppressions") or [])
    sig = _comment_signature(comment.comment)
    for e in entries:
        if isinstance(e, dict) and e.get("signature") == sig:
            return path  # already recorded — idempotent

    entries.append({
        "file": Path(comment.file).name,
        "signature": sig,
        "comment": comment.comment,
        "hint_ids": list(comment.hint_ids),
        "category": comment.category,
        "severity": comment.severity,
        "rationale": decision.rationale,
        "dropped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    existing_data["suppressions"] = entries

    body = yaml.safe_dump(existing_data, sort_keys=False, default_flow_style=False)
    path.write_text(_SUPPRESSIONS_HEADER + body)
    return path


def _clean_diff_path(path: str) -> str:
    """Strip `a/`/`b/` prefix and any trailing mtime (tab-separated)."""
    # `diff -u` emits `--- a/foo.py\t2026-04-22 01:58:17.058349020 +1000`
    path = path.split("\t", 1)[0].strip()
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def load_diff(target: ReviewTarget) -> list[Hunk]:
    """Parse a diff into Hunks, reusing ozmalib.compress.diffs parser."""
    from ozmalib.compress.diffs import _parse_unified_diff

    text = _fetch_diff(target)
    if not text.strip():
        return []

    files = _parse_unified_diff(text)
    hunks: list[Hunk] = []
    for fd in files:
        if fd.is_binary:
            continue
        new_path = _clean_diff_path(fd.new_path or fd.old_path)
        old_path = _clean_diff_path(fd.old_path)

        lang = _detect_lang_for_file(new_path)
        for h in fd.hunks:
            body = h.header + "\n" + "\n".join(h.lines)
            hunks.append(Hunk(
                file=new_path,
                old_file=old_path,
                old_start=h.old_start,
                old_count=h.old_count,
                new_start=h.new_start,
                new_count=h.new_count,
                content=body,
                language=lang,
                is_new_file=fd.is_new_file,
                is_deleted=fd.is_deleted_file,
                is_rename=fd.is_rename,
            ))
    return hunks


def _file_as_hunk(file: str, content: str, language: str) -> Hunk:
    """Wrap an entire file as one synthetic Hunk (every line `+`-prefixed).

    Used when reviewing whole files/codebases rather than diffs. The `+`
    prefix keeps `Hunk.changed_text` working for hint firing and the
    review prompt's pseudo-diff rendering.
    """
    lines = content.splitlines()
    n = max(1, len(lines))
    header = f"@@ -0,0 +1,{n} @@ whole-file"
    body = header + "\n" + "\n".join("+" + ln for ln in lines)
    return Hunk(
        file=file, old_file=file,
        old_start=0, old_count=0,
        new_start=1, new_count=n,
        content=body, language=language,
        is_new_file=True,
    )


def _functions_as_hunks(file: str, content: str, language: str) -> list[Hunk]:
    """Split a file into one synthetic Hunk per top-level function/class.

    Lines outside any function are collected into a module-level hunk.
    Falls back to a single whole-file hunk if tree-sitter can't parse.
    """
    try:
        from ozmalib.compress.tree import stub_functions
        _, smap = stub_functions(content, language=language)
    except Exception:
        return [_file_as_hunk(file, content, language)]

    if not smap or not smap.stubs:
        return [_file_as_hunk(file, content, language)]

    lines = content.splitlines()
    spans = sorted(smap.stubs, key=lambda s: s.orig_start)
    hunks: list[Hunk] = []
    cursor = 0
    module_buf: list[tuple[int, str]] = []

    def flush_module():
        if not module_buf:
            return
        start0 = module_buf[0][0]
        n = len(module_buf)
        header = f"@@ -0,0 +{start0 + 1},{n} @@ module-level"
        body = header + "\n" + "\n".join("+" + ln for _, ln in module_buf)
        hunks.append(Hunk(
            file=file, old_file=file,
            old_start=0, old_count=0,
            new_start=start0 + 1, new_count=n,
            content=body, language=language,
            is_new_file=True,
        ))
        module_buf.clear()

    for s in spans:
        if cursor < s.orig_start:
            for i in range(cursor, s.orig_start):
                module_buf.append((i, lines[i]))
            flush_module()
        fn_lines = lines[s.orig_start:s.orig_end + 1]
        n = max(1, len(fn_lines))
        header = f"@@ -0,0 +{s.orig_start + 1},{n} @@ {s.name}"
        body = header + "\n" + "\n".join("+" + ln for ln in fn_lines)
        hunks.append(Hunk(
            file=file, old_file=file,
            old_start=0, old_count=0,
            new_start=s.orig_start + 1, new_count=n,
            content=body, language=language,
            is_new_file=True,
        ))
        cursor = s.orig_end + 1

    if cursor < len(lines):
        for i in range(cursor, len(lines)):
            module_buf.append((i, lines[i]))
        flush_module()

    return hunks


_CODE_EXTS = frozenset({
    "py", "pyi",
    "js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts",
    "cs", "go", "rs", "java", "kt", "kts", "swift", "rb", "php",
    "c", "cc", "cpp", "cxx", "h", "hpp", "hxx", "hh",
    "lua", "scala", "ex", "exs", "dart", "hs", "sh", "bash",
    "ml", "mli", "nix", "tf", "hcl",
    "sql",
})


def _is_code_file(path: Path) -> bool:
    ext = path.suffix.lstrip(".").lower()
    return ext in _CODE_EXTS


def _resolve_paths(target: ReviewTarget) -> list[Path]:
    """Expand target.paths into a flat list of files. Directories walked recursively."""
    out: list[Path] = []
    for p in target.paths or []:
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if not child.is_file():
                    continue
                parts = child.relative_to(p).parts
                if any(part.startswith(".") or part in {
                    "node_modules", "__pycache__", "dist", "build",
                    "target", "venv", ".venv",
                } for part in parts):
                    continue
                if not _is_code_file(child):
                    continue
                out.append(child)
        elif p.is_file():
            out.append(p)
    return out


def _hunks_from_paths(target: ReviewTarget) -> list[Hunk]:
    out: list[Hunk] = []
    for p in _resolve_paths(target):
        try:
            content = p.read_text()
        except OSError:
            continue
        try:
            rel = p.resolve().relative_to(target.repo.resolve())
            file_str = str(rel)
        except ValueError:
            file_str = str(p)
        lang = _detect_lang_for_file(file_str)
        if target.chunk_mode == "file":
            out.append(_file_as_hunk(file_str, content, lang))
        else:
            out.extend(_functions_as_hunks(file_str, content, lang))
    return out


def load_hunks(target: ReviewTarget) -> list[Hunk]:
    """Load hunks for review. Diff-mode or whole-file mode based on target.paths."""
    if target.paths:
        return _hunks_from_paths(target)
    return load_diff(target)


def run_linters_on_diff(
    hunks: list[Hunk],
    repo: Path,
    *,
    extra_check_dirs: list[Path] | None = None,
) -> list[LinterFinding]:
    """Run ozmalib.checks against each changed file's current content.

    This is the free deterministic layer — no LLM calls. Findings are
    returned AND seeded as `ReviewComment`s downstream.
    """
    try:
        from ozmalib.checks.engine import load_checks, run_checks
    except ImportError:
        return []

    checks = load_checks(*(extra_check_dirs or []))
    out: list[LinterFinding] = []
    seen_files: set[str] = set()
    for h in hunks:
        if h.file in seen_files or h.is_deleted:
            continue
        seen_files.add(h.file)
        path = repo / h.file
        if not path.exists():
            continue
        try:
            content = path.read_text()
        except OSError:
            continue
        results = run_checks(content, checks)
        for r in results:
            out.append(LinterFinding(
                file=h.file,
                line=_derive_line(content, r.matched_patterns),
                name=r.name,
                severity=r.severity,
                message=r.message.strip()[:300],
                source="check",
                type=r.type,
            ))
    return out


def _derive_line(content: str, matched_patterns: list[str]) -> int | None:
    """Best-effort line number from a CheckResult's first matched substring."""
    for frag in matched_patterns:
        if not frag:
            continue
        # matched_patterns are truncated to 80 chars — the prefix still
        # identifies the line uniquely in practice.
        idx = content.find(frag)
        if idx >= 0:
            return content.count("\n", 0, idx) + 1
    return None


def fire_hints_for_hunk(
    hunk: Hunk,
    hints: Any | None = None,
    *,
    file_context: str = "",
) -> tuple[str, list[str]]:
    """Fire ozmalib.hints against a hunk's changed text + context.

    Returns `(block, ids)`:
      * `block` is the standard fenced-markdown hint block (via
        format_hints_qualified) ready to inject into the review prompt.
      * `ids` is the ordered list of hint rule names that matched — used
        downstream to ask the reviewer model to cite them and to attribute
        review findings back to specific rules.
    Returns `("", [])` when no hints fire or hints package is unavailable.
    """
    try:
        from ozmalib.hints.engine import (
            load_hints, check_hints, format_hints_qualified,
        )
    except ImportError:
        return "", []

    hs = hints if hints is not None else load_hints()
    matched = check_hints(
        hunk.changed_text,
        hs,
        file_context=file_context,
        language=hunk.language if hunk.language != "unknown" else None,
        max_hints=6,
        max_tokens=1500,
    )
    if not matched:
        return "", []
    ids = [getattr(h, "name", "") for h in matched if getattr(h, "name", "")]
    return format_hints_qualified(matched, include_rubric=False), ids


def _extract_enclosing_fn(
    file_content: str, new_start_line: int, language: str,
) -> str:
    """Return the function/class enclosing `new_start_line` (1-indexed).

    Uses ozmalib.compress.stub_functions to locate function spans; keeps
    only the one enclosing our line. Returns the full body (NOT a stub).
    Falls back to ±25 lines around the target on failure.
    """
    lines = file_content.splitlines()

    try:
        from ozmalib.compress.tree import stub_functions
        _, smap = stub_functions(file_content, language=language)
    except Exception:
        smap = None

    if smap and smap.stubs:
        target = new_start_line - 1        # stubs use 0-indexed rows
        best = None
        for s in smap.stubs:
            if s.orig_start <= target <= s.orig_end:
                # pick the tightest enclosing span
                if best is None or (s.orig_end - s.orig_start) < (best.orig_end - best.orig_start):
                    best = s
        if best is not None:
            return "\n".join(lines[best.orig_start:best.orig_end + 1])

    start = max(0, new_start_line - 26)
    end = min(len(lines), new_start_line + 25)
    return "\n".join(lines[start:end])


def _stub_rest_of_file(file_content: str, language: str) -> str:
    """Return the file with all bodies stubbed — for high-level surround."""
    try:
        from ozmalib.compress.tree import stub_functions
        stubbed, _ = stub_functions(file_content, language=language)
        return stubbed
    except Exception:
        return ""


def assemble_context(
    hunk: Hunk,
    repo: Path,
    *,
    hints: Any | None = None,
    pr_description: str = "",
    linked_issue: str = "",
    linter_findings: list[LinterFinding] | None = None,
    include_file_stubs: bool = True,
    max_enclosing_chars: int = 4000,
    max_stub_chars: int = 4000,
) -> HunkContext:
    """Build a generous context pack for one hunk.

    Layers:
      * enclosing function/class (tree-sitter, un-stubbed)
      * rest-of-file stubs (tree-sitter, bodies replaced with `...`)
      * fired hints (regex, zero-cost, qualified-fence format)
      * linter findings relevant to this file
      * PR description + linked issue body
    """
    path = repo / hunk.file
    enclosing = ""
    file_stubs = ""
    if path.exists() and not hunk.is_deleted:
        try:
            content = path.read_text()
        except OSError:
            content = ""
        if content:
            enclosing = _extract_enclosing_fn(content, hunk.new_start, hunk.language)
            enclosing = enclosing[:max_enclosing_chars]
            if include_file_stubs:
                file_stubs = _stub_rest_of_file(content, hunk.language)[:max_stub_chars]

    hints_block, fired_hint_ids = fire_hints_for_hunk(hunk, hints, file_context=enclosing)

    relevant_linter: list[LinterFinding] = []
    for lf in linter_findings or []:
        if lf.file == hunk.file:
            relevant_linter.append(lf)

    return HunkContext(
        hunk=hunk,
        enclosing_fn=enclosing,
        file_stubs=file_stubs,
        hints_block=hints_block,
        fired_hint_ids=fired_hint_ids,
        linter_findings=relevant_linter,
        pr_description=pr_description,
        linked_issue=linked_issue,
    )


def _format_linter_block(findings: list[LinterFinding]) -> str:
    if not findings:
        return "(none)"
    lines = []
    for f in findings[:20]:
        loc = f"L{f.line}" if f.line else ""
        lines.append(f"- [{f.severity}] {f.name} {loc}: {f.message[:120]}")
    return "\n".join(lines)


_SECURITY_LINTER_NAME_HINTS = (
    "sql", "inject", "xss", "csrf", "eval", "pickle", "secret",
    "password", "crypto", "deserialize", "ssrf", "xxe", "traversal",
    "shell", "exec",
)


def _linter_category(lf: LinterFinding) -> HunkCategory:
    """Map a LinterFinding → HunkCategory using the check's type + name."""
    t = (lf.type or "").lower()
    if t == "security":
        return "security"
    if t in {"correctness", "logic"}:
        return "logic"
    if t == "quality":
        return "cosmetic"
    # Heuristic fallback: name-based for checks with no type.
    name_l = lf.name.lower()
    if any(h in name_l for h in _SECURITY_LINTER_NAME_HINTS):
        return "security"
    return "logic"


def _format_pr_intent(pr_description: str, linked_issue: str) -> str:
    parts = []
    if pr_description.strip():
        parts.append(f"PR description:\n{pr_description.strip()[:1500]}")
    if linked_issue.strip():
        parts.append(f"Linked issue:\n{linked_issue.strip()[:1500]}")
    return "\n\n".join(parts) if parts else "(no PR description or linked issue)"


# ---------------------------------------------------------------------------
# ReviewSession
# ---------------------------------------------------------------------------

class ReviewSession:
    """One code-review run.

    Owns the pipeline state, the cost ledger, and the hunk→context map.
    Efficiency knobs mirror AuditSession:

      * `llm_fast` drives triage stages (classify, filter, checklist).
      * `llm_smart` drives reasoning stages (review, aggregate).
      * Every stage fans out with `asyncio.gather`, bounded by a Semaphore
        of size `max_concurrency`.
      * Classify is MERGED (one call for N hunks).
      * Filter gates cheap YES/NO per hunk before checklist/review.
      * Cached prefix: PR description + repo map (same across all hunks).
    """

    _SMART_STAGES: set[Stage] = {"review", "aggregate"}

    def __init__(
        self,
        target: ReviewTarget,
        llm: LLMCallable | AsyncLLMCallable | None = None,
        *,
        llm_fast: LLMCallable | AsyncLLMCallable | None = None,
        llm_smart: LLMCallable | AsyncLLMCallable | None = None,
        budget_usd: float = 0.50,
        max_concurrency: int = 8,
        context_budget_tokens: int = 3000,
        multi_pass: int = 1,
        hints: Any | None = None,
        packs: list[str] | None = None,
        checks: Any | None = None,
        nitpick_threshold: float = 0.6,
        adjudicate: bool = False,
        adjudicate_min_confidence: float = 0.6,
        write_suppressions: bool = True,
        apply_suppressions: bool = True,
    ):
        if llm_fast is None and llm_smart is None and llm is None:
            raise ValueError("ReviewSession needs an llm= or llm_fast/llm_smart pair")
        self.target = target
        self.llm_fast = _wrap_llm(llm_fast or llm)
        self.llm_smart = _wrap_llm(llm_smart or llm or llm_fast)
        self.budget_usd = budget_usd
        self.cost_usd = 0.0
        self.stages: dict[str, float] = {}
        self.hunks: list[Hunk] = []
        self.linter_findings: list[LinterFinding] = []
        self.contexts: dict[str, HunkContext] = {}
        self.categories: dict[str, HunkCategory] = {}
        self.comments: list[ReviewComment] = []
        self.multi_pass = max(1, multi_pass)
        self.context_budget_tokens = context_budget_tokens
        self.nitpick_threshold = nitpick_threshold
        self.packs = list(packs) if packs else []
        # If caller passed a preloaded HintSet, use it. Otherwise, if packs
        # were requested, preload once here so every hunk reuses the same set.
        if hints is None and self.packs:
            try:
                from ozmalib.hints.engine import load_hints
                hints = load_hints(packs=self.packs)
            except ImportError:
                hints = None
        self.hints = hints
        self.checks = checks
        self.adjudicate = adjudicate
        self.adjudicate_min_confidence = adjudicate_min_confidence
        self.write_suppressions = write_suppressions
        self.apply_suppressions = apply_suppressions
        self.suppressions_written: list[Path] = []
        self.suppressed_count: int = 0
        self._sem = asyncio.Semaphore(max_concurrency)

    # -- LLM + budget plumbing ----------------------------------------------

    async def _acall(self, req: LLMRequest) -> str:
        if self.cost_usd >= self.budget_usd:
            raise RuntimeError(
                f"budget exhausted: ${self.cost_usd:.4f} >= ${self.budget_usd:.4f}"
            )
        if req.max_tokens is None:
            req.max_tokens = _MAX_TOKENS.get(req.stage) or 500
        llm = self.llm_smart if req.stage in self._SMART_STAGES else self.llm_fast
        async with self._sem:
            text, usage = await llm(req)
        self.cost_usd += float(usage.get("cost", 0.0))
        return text

    async def _atime_stage(
        self, name: str, coro_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        t0 = time.time()
        try:
            return await coro_fn()
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.time() - t0)

    # -- Stage: linters (local, parallel via to_thread) ---------------------

    async def _arun_linters(self) -> list[LinterFinding]:
        findings = await asyncio.to_thread(
            run_linters_on_diff, self.hunks, self.target.repo,
        )
        self.linter_findings = findings
        return findings

    # -- Stage: classify (merged batch, fast) -------------------------------

    async def _aclassify_hunks(self) -> dict[str, HunkCategory]:
        if not self.hunks:
            return {}
        # Merge hunks into a single call. Truncate each to ~800 chars
        # to keep the total prompt bounded.
        parts = []
        for h in self.hunks:
            snippet = h.content[:800]
            parts.append(f"### {h.id}  file={h.file}  lang={h.language}\n```\n{snippet}\n```")
        req = LLMRequest(
            prompt=_CLASSIFY_PROMPT.format(hunks_block="\n\n".join(parts)),
            stage="classify",
            max_tokens=max(200, 30 * len(self.hunks)),
        )
        resp = await self._acall(req)
        parsed = _parse_classify(resp)
        out: dict[str, HunkCategory] = {}
        for h in self.hunks:
            cat, _reason = parsed.get(h.id, ("unknown", ""))
            out[h.id] = cat
        self.categories = out
        return out

    # -- Stage: assemble_context (local, per hunk in parallel) --------------

    async def _aassemble_contexts(self) -> dict[str, HunkContext]:
        def build_one(h: Hunk) -> tuple[str, HunkContext]:
            ctx = assemble_context(
                h, self.target.repo,
                hints=self.hints,
                pr_description=self.target.pr_description,
                linked_issue=self.target.linked_issue_body,
                linter_findings=self.linter_findings,
            )
            return h.id, ctx

        results = await asyncio.gather(*[
            asyncio.to_thread(build_one, h) for h in self.hunks
        ])
        out = {hid: ctx for hid, ctx in results}
        self.contexts = out
        return out

    # -- Stage: filter (per-hunk YES/NO, fast) ------------------------------

    async def _afilter(self, hunks: list[Hunk]) -> list[Hunk]:
        async def check_one(h: Hunk) -> Hunk | None:
            req = LLMRequest(
                prompt=_FILTER_PROMPT.format(
                    language=h.language, hunk_content=h.content[:1500],
                ),
                stage="filter",
            )
            resp = (await self._acall(req)).strip().upper()
            return h if not resp.startswith("NO") else None

        results = await asyncio.gather(*[check_one(h) for h in hunks])
        return [h for h in results if h is not None]

    # -- Stage: checklist (per hunk, fast) ----------------------------------

    async def _agenerate_checklist(self, hunk: Hunk) -> str:
        ctx = self.contexts.get(hunk.id)
        if ctx is None:
            return ""
        req = LLMRequest(
            prompt=_CHECKLIST_PROMPT.format(
                pr_intent=_format_pr_intent(
                    self.target.pr_description, self.target.linked_issue_body,
                ),
                language=hunk.language,
                enclosing_fn=ctx.enclosing_fn[:2500],
                hunk_content=hunk.content[:2500],
            ),
            stage="checklist",
        )
        return await self._acall(req)

    # -- Stage: review (per hunk, smart, main LLM call) ---------------------

    async def _arun_review_one(
        self, hunk: Hunk, checklist: str, *, pass_idx: int = 0,
    ) -> list[ReviewComment]:
        ctx = self.contexts.get(hunk.id)
        if ctx is None:
            return []
        pr_intent = _format_pr_intent(
            self.target.pr_description, self.target.linked_issue_body,
        )
        linter_block = _format_linter_block(ctx.linter_findings)
        available_hints = (
            "\n".join(f"- {i}" for i in ctx.fired_hint_ids)
            if ctx.fired_hint_ids
            else "(none)"
        )
        cached_prefix = (
            (ctx.hints_block + "\n\n" if ctx.hints_block else "")
            + f"## PR intent\n{pr_intent}\n\n"
            + f"## File stubs ({hunk.file})\n```{hunk.language}\n{ctx.file_stubs[:3000]}\n```"
        )
        req = LLMRequest(
            prompt=_REVIEW_PROMPT.format(
                pr_intent="(see cached prefix)",
                available_hints=available_hints,
                linter_block=linter_block,
                checklist=checklist.strip() or "(none)",
                language=hunk.language,
                enclosing_fn=ctx.enclosing_fn[:3000],
                hunk_content=hunk.content[:3000],
            ),
            stage="review",
            cached_prefix=cached_prefix,
        )
        resp = await self._acall(req)
        out: list[ReviewComment] = []
        for line, sev, cat, cwe, comment, conf, hint_ids in _parse_review(resp):
            raw = f"{hunk.file}|{line}|{cat}|{pass_idx}|{comment[:40]}"
            cid = hashlib.sha1(raw.encode()).hexdigest()[:12]
            out.append(ReviewComment(
                id=cid,
                file=hunk.file,
                line=line,
                severity=sev,  # type: ignore[arg-type]
                category=cat if cat in {"cosmetic", "docs", "logic", "security", "test", "config"} else "logic",  # type: ignore[arg-type]
                cwe=cwe,
                comment=comment,
                confidence=conf,
                source="llm",
                hint_ids=hint_ids,
            ))
        return out

    # -- Stage: multi_pass aggregate (optional, smart) ----------------------

    async def _aaggregate(
        self, hunk: Hunk, passes: list[list[ReviewComment]],
    ) -> list[ReviewComment]:
        if len(passes) <= 1:
            return passes[0] if passes else []
        # Flatten passes into a text block the aggregator can merge.
        # Preserve [[hint-id]] markers on re-emission so aggregator can cite.
        blocks = []
        for i, p in enumerate(passes):
            if not p:
                blocks.append(f"### Reviewer {i + 1}\n(no comments)")
                continue
            lines = []
            for c in p:
                suffix = f" [[{','.join(c.hint_ids)}]]" if c.hint_ids else ""
                lines.append(
                    f"{c.line}|{c.severity}|{c.category}|{c.cwe or '-'}|{c.comment}{suffix}|{c.confidence:.2f}"
                )
            blocks.append(f"### Reviewer {i + 1}\n" + "\n".join(lines))
        req = LLMRequest(
            prompt=_AGGREGATE_PROMPT.format(reviews_block="\n\n".join(blocks)),
            stage="aggregate",
        )
        resp = await self._acall(req)
        out: list[ReviewComment] = []
        for line, sev, cat, cwe, comment, conf, hint_ids in _parse_review(resp):
            raw = f"{hunk.file}|{line}|{cat}|agg|{comment[:40]}"
            cid = hashlib.sha1(raw.encode()).hexdigest()[:12]
            out.append(ReviewComment(
                id=cid,
                file=hunk.file,
                line=line,
                severity=sev,  # type: ignore[arg-type]
                category=cat if cat in {"cosmetic", "docs", "logic", "security", "test", "config"} else "logic",  # type: ignore[arg-type]
                cwe=cwe,
                comment=comment,
                confidence=conf,
                source="llm",
                hint_ids=hint_ids,
            ))
        return out

    # -- Post: dedupe + threshold -------------------------------------------

    def _seed_linter_comments(self) -> list[ReviewComment]:
        sev_map = {
            "critical": "critical", "high": "high",
            "medium": "medium", "low": "low",
        }
        out: list[ReviewComment] = []
        for lf in self.linter_findings:
            sev = sev_map.get(lf.severity, "low")
            raw = f"{lf.file}|{lf.line}|linter|{lf.name}"
            cid = hashlib.sha1(raw.encode()).hexdigest()[:12]
            out.append(ReviewComment(
                id=cid,
                file=lf.file,
                line=lf.line or 0,
                severity=sev,  # type: ignore[arg-type]
                category=_linter_category(lf),
                cwe="",
                comment=f"[{lf.name}] {lf.message}",
                confidence=1.0,
                source="linter",
                linter_name=lf.name,
            ))
        return out

    # -- Suppressions --------------------------------------------------------

    def _collect_suppressions(self, files: set[str]) -> list[Suppression]:
        """Load suppressions from every `.hints.yaml` sitting next to a
        reviewed file. Missing files are silently skipped."""
        seen: set[Path] = set()
        out: list[Suppression] = []
        for rel in files:
            sup_path = _suppression_path_for(self.target.repo, rel)
            if sup_path in seen:
                continue
            seen.add(sup_path)
            out.extend(_load_suppressions_from(sup_path))
        return out

    def _filter_suppressed(
        self, comments: list[ReviewComment],
    ) -> list[ReviewComment]:
        """Drop comments that match a persisted suppression."""
        if not self.apply_suppressions:
            return comments
        files = {c.file for c in comments}
        sups = self._collect_suppressions(files)
        if not sups:
            return comments
        kept: list[ReviewComment] = []
        for c in comments:
            if any(_comment_matches_suppression(c, s) for s in sups):
                self.suppressed_count += 1
                continue
            kept.append(c)
        return kept

    # -- Stage: adjudicate (per-finding, routes through same pool as review) --

    def _resolve_hint_guidance(self, hint_ids: list[str]) -> str:
        """Pull the raw `guidance` text for each cited hint id.

        The adjudicator needs the full rule text to know what the reviewer's
        `[[hint-id]]` marker actually asserts. No hints loaded → empty.
        """
        if not hint_ids or self.hints is None:
            return "(no hints cited)"
        by_name: dict[str, Any] = {
            getattr(r, "name", ""): r for r in self.hints
        }
        parts: list[str] = []
        for hid in hint_ids:
            rule = by_name.get(hid)
            if rule is None:
                continue
            guidance = getattr(rule, "guidance", "") or ""
            if guidance:
                parts.append(f"### {hid}\n{guidance.strip()}")
        return "\n\n".join(parts) if parts else "(cited hints not found in loaded set)"

    async def _aadjudicate_one(
        self, comment: ReviewComment,
    ) -> ReviewComment:
        """Run the adjudicator on one finding; apply its verdict in place."""
        # Resolve the hunk this comment points at. No ctx → cannot adjudicate.
        ctx = next(
            (c for c in self.contexts.values() if c.hunk.file == comment.file),
            None,
        )
        if ctx is None:
            return comment
        hunk = ctx.hunk
        pr_intent = _format_pr_intent(
            self.target.pr_description, self.target.linked_issue_body,
        )
        hint_guidance = self._resolve_hint_guidance(comment.hint_ids)
        req = LLMRequest(
            prompt=_ADJUDICATE_PROMPT.format(
                file=comment.file,
                line=comment.line,
                severity=comment.severity,
                category=comment.category,
                cwe=comment.cwe or "-",
                comment=comment.comment,
                hint_ids=", ".join(comment.hint_ids) or "(none)",
                hint_guidance=hint_guidance,
                language=hunk.language,
                enclosing_fn=ctx.enclosing_fn[:3000],
                hunk_content=hunk.content[:3000],
                pr_intent=pr_intent,
            ),
            stage="adjudicate",
        )
        resp = await self._acall(req)
        decision = _parse_adjudication(resp)
        if decision is None:
            return comment
        comment.adjudication = decision
        # Only apply verdicts above the min-confidence bar. Below it we keep
        # the raw finding and leave the decision attached for humans.
        if decision.confidence < self.adjudicate_min_confidence:
            comment.status = "adjudicated-keep"
            return comment
        if decision.verdict == "drop":
            comment.status = "adjudicated-drop"
            if self.write_suppressions:
                written = _persist_suppression(
                    self.target.repo, comment, decision,
                )
                if written is not None:
                    self.suppressions_written.append(written)
        elif decision.verdict in ("downgrade", "upgrade") and decision.new_severity:
            comment.severity = decision.new_severity  # type: ignore[assignment]
            comment.status = "adjudicated-rescored"
        else:
            comment.status = "adjudicated-keep"
        return comment

    async def _aadjudicate(
        self, comments: list[ReviewComment],
    ) -> list[ReviewComment]:
        """Fan out adjudication over every LLM comment in parallel.

        Linter findings are NOT adjudicated — they're deterministic and
        already come with pattern attribution. Adjudicator only sees
        ambiguous model output.
        """
        targets = [c for c in comments if c.source == "llm"]
        if not targets:
            return comments
        await asyncio.gather(*[self._aadjudicate_one(c) for c in targets])
        # Drop those the adjudicator dropped with high confidence.
        return [c for c in comments if c.status != "adjudicated-drop"]

    def score_and_filter(self, comments: list[ReviewComment]) -> list[ReviewComment]:
        """Dedupe by (file, line, category, comment-prefix); drop nitpicks
        below confidence threshold; severity-sort."""
        seen: dict[tuple[str, int, str, str], ReviewComment] = {}
        for c in comments:
            key = (c.file, c.line, c.category, c.comment[:60].lower())
            existing = seen.get(key)
            if existing is None or c.confidence > existing.confidence:
                seen[key] = c
        filtered = []
        for c in seen.values():
            if c.severity == "nitpick" and c.confidence < self.nitpick_threshold:
                continue
            filtered.append(c)
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "nitpick": 4}
        filtered.sort(key=lambda c: (order.get(c.severity, 5), c.file, c.line))
        return filtered

    # -- One-shot orchestration ---------------------------------------------

    async def arun(self) -> ReviewReport:
        self.hunks = await asyncio.to_thread(load_hunks, self.target)
        if not self.hunks:
            return self.report()

        # Linters + classify run concurrently (both are cheap).
        linter_task = self._atime_stage("linters", self._arun_linters)
        classify_task = self._atime_stage("classify", self._aclassify_hunks)
        await asyncio.gather(linter_task, classify_task)

        await self._atime_stage("assemble_context", self._aassemble_contexts)

        reviewable = [
            h for h in self.hunks
            if self.categories.get(h.id, "unknown") in ("logic", "security")
        ]

        surviving = await self._atime_stage(
            "filter", lambda: self._afilter(reviewable),
        )

        async def review_hunk(h: Hunk) -> list[ReviewComment]:
            checklist = await self._agenerate_checklist(h)
            passes = await asyncio.gather(*[
                self._arun_review_one(h, checklist, pass_idx=i)
                for i in range(self.multi_pass)
            ])
            if self.multi_pass == 1:
                return passes[0] if passes else []
            return await self._aaggregate(h, list(passes))

        batches = await self._atime_stage(
            "review", lambda: asyncio.gather(*[review_hunk(h) for h in surviving]),
        )
        llm_comments = [c for batch in batches for c in batch]

        all_comments = self._seed_linter_comments() + llm_comments
        filtered = self.score_and_filter(all_comments)
        # Apply persisted suppressions BEFORE adjudicating — no point paying
        # to re-adjudicate a finding a past review already dismissed.
        filtered = self._filter_suppressed(filtered)
        if self.adjudicate:
            filtered = await self._atime_stage(
                "adjudicate", lambda: self._aadjudicate(filtered),
            )
        self.comments = filtered
        return self.report()

    def run(self) -> ReviewReport:
        return asyncio.run(self.arun())

    def report(self) -> ReviewReport:
        return ReviewReport(
            target=self.target,
            comments=self.comments,
            linter_findings=self.linter_findings,
            cost_usd=self.cost_usd,
            duration_sec=sum(self.stages.values()),
            stages=dict(self.stages),
            hunk_categories=dict(self.categories),
            suppressions_written=list(self.suppressions_written),
            suppressed_count=self.suppressed_count,
        )
