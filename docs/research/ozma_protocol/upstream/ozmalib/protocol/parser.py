"""
parser.py — Parse structured protocol output from model responses.

Handles structural protocol types (L1/L2): EDIT, INTERACT, PLAN, CLAIM, REASONING, MEMORY.
Gracefully ignores unrecognised lines (they become prose text).

The parser is line-oriented and does not backtrack. It's designed to be
fast enough to run on every response without a model call.
"""

from __future__ import annotations

import re
from typing import Iterator

from .types import (
    AgentStateBlock, AckHeader, AttnAnnotation, ClaimBlock,
    CollabBlock, DepEdge, DocStub, DPBudget, MotionBlock,
    EditCmd, EditOp, EmbedBlock, EffectAnnotation, FederatedRoute,
    GrammarBlock, GraphBlock, GraphEdge, HandoffPacket, HintBlock,
    InteractCmd, InteractVerb, LearnedBPEBlock, MathBlock,
    MemoryWrite, MemScope, OutlineBlock, OutlineItem,
    PlanBlock, PlanStep, PolyglotBlock, ReasoningBlock, ReasoningStep,
    SchemaDecl, StructuredConf, SubPrompt, TensorShape, TestSpec,
    TreeBlock, TreeNode, Turn,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# EDIT / DELETE / ADD operations
_EDIT_RE = re.compile(
    r'^(edit|delete)\s+((?:[\w./]+:)?§(?:[0-9a-f]{8}|[0-9]+))'
    r'(?:\s+confidence=([\d.]+))?(?:\s+(preview))?',
    re.IGNORECASE,
)
_ADD_RE = re.compile(
    r'^add\s+((?:[\w./]+:)?§(?:[0-9]+|[0-9a-f]{8})):(before|after)'
    r'(?:\s+confidence=([\d.]+))?',
    re.IGNORECASE,
)
_ADD_FILE_RE = re.compile(
    r'^add\s+([\w./]+\.\w+)',
    re.IGNORECASE,
)

# INTERACT commands
_INTERACT_RE = re.compile(
    r'^(expand doc|expand|callers|callees|outline|recall|persona\?|'
    r'search semantic|search)\s*(.*)',
    re.IGNORECASE,
)

# PLAN block header and step
_PLAN_HEADER_RE = re.compile(r'^plan\s+(P\d+)\s*:', re.IGNORECASE)
_PLAN_STEP_RE = re.compile(
    r'^\s+(p\d+)\s*:\s*(.+?)(?:\s+\[([^\]]*)\])?(?:\s+confidence=([\d.]+))?$',
    re.IGNORECASE,
)
# Key=value pairs inside plan step qualifiers: [complexity=C category=K file=F depends_on=p1,p2]
_PLAN_STEP_KV_RE = re.compile(r'([\w-]+)=([\w./,]+)')

# CLAIM / INVARIANT
_CLAIM_RE = re.compile(
    r'^(claim|invariant)\s+(§(?:[0-9]+|[0-9a-f]{8})|):\s*(.+?)(?:\s+confidence=([\d.]+))?$',
    re.IGNORECASE,
)
_FORMAL_CLAIM_RE = re.compile(
    r'^formal-claim\s+(§[^\s:]+|)\s*\[(smt2|dafny|lean4|z3)\]:\s*$',
    re.IGNORECASE,
)

# REASONING block
_REASONING_START_RE = re.compile(r'^reasoning:\s*$', re.IGNORECASE)
_REASONING_STEP_RE = re.compile(
    r'^\s+(h\d+)\s*:\s*(.+?)\s*(?:→|->)\s*(accept|reject|defer)(?:\s+confidence=([\d.]+))?$',
    re.IGNORECASE,
)
_REASONING_STEP_FUZZY_RE = re.compile(
    r'^\s*(h\d+)\s*:\s*(.+?)\s*(?:→|->)\s*(accept|reject|defer)(?:\s+confidence=([\d.]+))?$',
    re.IGNORECASE,
)
_REASONING_CONC_RE = re.compile(r'^\s+conclusion\s*:\s*(.+)$', re.IGNORECASE)
_REASONING_CONC_FUZZY_RE = re.compile(r'^\s*conclusion\s*:\s*(.+)$', re.IGNORECASE)

# MEMORY
_REMEMBER_RE = re.compile(
    r'^remember(?::(workspace|session|user|immediate))?\s*'
    r'(§(?:[0-9]+|[0-9a-f]{8}))?\s*:\s*(.+)$',
    re.IGNORECASE,
)

# Code fence
_FENCE_RE = re.compile(r'^```(\w*)$')

# ---------------------------------------------------------------------------
# Phase 2: Code-intelligence patterns
# ---------------------------------------------------------------------------

_TEST_SPEC_RE = re.compile(
    r'^test-spec\s+((?:[\w./]+:)?§(?:[0-9a-f]{8}|[0-9]+))'
    r'(?:\s+\[(\w+)\])?:\s*$',
    re.IGNORECASE,
)
_EFFECTS_RE = re.compile(
    r'^effects\s+((?:[\w./]+:)?§(?:[0-9a-f]{8}|[0-9]+)):\s*(.+)$',
    re.IGNORECASE,
)
_DEP_RE = re.compile(
    r'^dep\s+(§(?:[0-9a-f]{8}|[0-9]+))\s*(?:→|->)\s*(§(?:[0-9a-f]{8}|[0-9]+))'
    r'(?:\s+\[([^\]]*)\])?',
    re.IGNORECASE,
)
_SCHEMA_RE = re.compile(
    r'^schema\s+((?:[\w./]+:)?§(?:[0-9a-f]{8}|[0-9]+))'
    r'(?:\s+\[([^\]]+)\])?:\s*$',
    re.IGNORECASE,
)
_OUTLINE_START_RE = re.compile(r'^outline:\s*[/\-*]?\s*$', re.IGNORECASE)
_OUTLINE_ITEM_RE = re.compile(
    r'^\s+(§(?:[0-9a-f]{8}|[0-9]+)):\s*(.+)$',
    re.IGNORECASE,
)
# Fuzzy: §N at col-0 or indented, colon or space separator
_OUTLINE_ITEM_FUZZY_RE = re.compile(
    r'^\s*(§(?:[0-9a-f]{8}|[0-9]+))[\s:](.+)$',
    re.IGNORECASE,
)
_HINT_RE = re.compile(r'^hint\s+\[([^\]]+)\]:\s*(.+)$', re.IGNORECASE)
_DOCSTUB_RE = re.compile(
    r'^docstub\s+((?:[\w./]+:)?§(?:[0-9a-f]{8}|[0-9]+)):\s*$',
    re.IGNORECASE,
)
_WIRE_RE = re.compile(r'^wire:\s*(.*)$', re.IGNORECASE)   # optional inline content

# ---------------------------------------------------------------------------
# Phase 3: Agent / orchestration patterns
# ---------------------------------------------------------------------------

_PROMPT_RE = re.compile(r'^prompt\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_HANDOFF_RE = re.compile(r'^handoff\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_AGENT_STATE_RE = re.compile(r'^agent-state\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_MOTION_START_RE = re.compile(r'^motion\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Phase 4: Math / ML / multi-model patterns
# ---------------------------------------------------------------------------

_MATH_RE = re.compile(r'^math\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_TENSOR_RE = re.compile(
    r'^tensor\s+(§(?:[0-9a-f]{8}|[0-9]+)):\s*\[([^\]]+)\]'
    r'(?:\s+dtype=(\S+))?',
    re.IGNORECASE,
)
_GRAPH_START_RE = re.compile(r'^graph\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_GRAPH_EDGE_RE = re.compile(
    r'^\s+(§[^\s→]+)\s*(?:→|->)\s*(§\S+)(?:\s+\[([^\]]*)\])?',
    re.IGNORECASE,
)
_TREE_START_RE = re.compile(r'^tree\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_TREE_NODE_RE = re.compile(r'^(\s*)(§(?:[0-9a-f]{8}|[0-9]+))(?:\s+\[([^\]]*)\])?', re.IGNORECASE)
_STRUCT_CONF_RE = re.compile(
    r'^structured-conf\s+(§(?:[0-9a-f]{8}|[0-9]+)):\s*$',
    re.IGNORECASE,
)
_STRUCT_CONF_ITEM_RE = re.compile(r'^\s+(\w[\w -]*):\s*([\d.]+)', re.IGNORECASE)
_DP_RE = re.compile(
    r'^dp\s+budget=ε=([\d.e+-]+),δ=([\d.e+-]+)(?:\s+mechanism=(\S+))?',
    re.IGNORECASE,
)
# Fuzzy: accept optional label prefix like "Formal: dp ..." or "Privacy: dp ..."
_DP_FUZZY_RE = re.compile(
    r'^(?:\w[\w\s]*:\s*)?dp\s+budget=ε=([\d.e+-]+),δ=([\d.e+-]+)(?:\s+mechanism=(\S+))?',
    re.IGNORECASE,
)
_GRAMMAR_RE = re.compile(r'^grammar\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_GRAMMAR_FUZZY_RE = re.compile(r'^grammar\s+([\w][\w_-]*):\s*$', re.IGNORECASE)
_ATTN_RE = re.compile(
    r'^attn\s+(§(?:[0-9a-f]{8}|[0-9]+)):\s*(.+)$',
    re.IGNORECASE,
)
_ATTN_WEIGHT_RE = re.compile(r'(§[0-9a-f]{8}|§\d+)=([\d.]+)')
_EMBED_RE = re.compile(
    r'^embed\s+(§(?:[0-9a-f]{8}|[0-9]+)):\s*\[([^\]]+)\]',
    re.IGNORECASE,
)
_COLLAB_RE = re.compile(
    r'^collab\s+\[([^\]|]+)(?:\s*\|\s*([^\]]*))?\]:\s*$',
    re.IGNORECASE,
)
_FEDERATED_RE = re.compile(
    r'^federated\s+\[target=(\S+?)(?:\s+region=(\S+?))?\]:\s*$',
    re.IGNORECASE,
)
_POLYGLOT_RE = re.compile(r'^polyglot\s+\[([^\]]+)\]:\s*$', re.IGNORECASE)
_LEARNED_BPE_RE = re.compile(r'^learned-bpe:\s*$', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TurnParser:
    """
    Stateful line-by-line parser for Ozma protocol model output.

    Usage::

        parser = TurnParser()
        turn = parser.parse(model_response_text)
    """

    def parse(self, text: str, *, strict: bool = True) -> Turn:
        """
        Parse a model response into a Turn.

        Args:
            text:   raw model output
            strict: True  = trained model; only accept exact protocol syntax.
                    False = prompted model; accept near-misses (bare code
                            bodies, inline wire: content, etc.).
        """
        lines = text.splitlines()
        turn = Turn(raw=text, text="")
        prose_lines: list[str] = []

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # ── Fuzzy: strip leading ``` when it wraps a protocol keyword ──
            # Some models write ```structured-conf §1:\n...\n``` instead of
            # bare structured-conf §1:\n...\n. Strip the fence and re-parse.
            if not strict and stripped.startswith("```") and len(stripped) > 3:
                inner = stripped[3:].strip()
                if inner and not inner.startswith("`"):
                    stripped = inner
                    line = inner

            # ── EDIT / DELETE ────────────────────────────────────────────
            m = _EDIT_RE.match(stripped)
            if m:
                op_str, ref, conf_str, prev_str = m.groups()
                op = EditOp.REPLACE if op_str.lower() == "edit" else EditOp.DELETE
                conf = float(conf_str) if conf_str else 1.0
                preview = bool(prev_str)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.edits.append(EditCmd(
                    op=op, ref=ref, body=body,
                    confidence=conf, preview=preview,
                ))
                continue

            # ── ADD §REF:before|after ────────────────────────────────────
            m = _ADD_RE.match(stripped)
            if m:
                ref, direction, conf_str = m.groups()
                conf = float(conf_str) if conf_str else 1.0
                op = EditOp.ADD_BEFORE if direction.lower() == "before" else EditOp.ADD_AFTER
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.edits.append(EditCmd(op=op, ref=ref, body=body, confidence=conf))
                continue

            # ── ADD file ────────────────────────────────────────────────
            m = _ADD_FILE_RE.match(stripped)
            if m and not _INTERACT_RE.match(stripped):
                fname = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.edits.append(EditCmd(op=EditOp.ADD_FILE, ref=fname, body=body))
                continue

            # ── Block starters that share keywords with INTERACT ─────────
            # These must be checked before _INTERACT_RE because "outline",
            # "recall", etc. appear in both, but block forms end with ":"
            # or have "[...]" qualifiers, making them unambiguous.

            # ── OUTLINE ──────────────────────────────────────────────────
            if _OUTLINE_START_RE.match(stripped):
                block = OutlineBlock()
                i += 1
                while i < len(lines):
                    sm = _OUTLINE_ITEM_RE.match(lines[i])
                    if sm:
                        block.items.append(OutlineItem(ref=sm.group(1), description=sm.group(2).strip()))
                        i += 1
                        continue
                    if not strict:
                        sm = _OUTLINE_ITEM_FUZZY_RE.match(lines[i])
                        if sm:
                            block.items.append(OutlineItem(ref=sm.group(1), description=sm.group(2).strip()))
                            i += 1
                            continue
                    break
                turn.outline = block
                continue

            # ── WIRE ─────────────────────────────────────────────────────
            m = _WIRE_RE.match(stripped)
            if m:
                inline = m.group(1).strip()
                if strict and inline:
                    # Strict: wire: must be alone on its line; inline content
                    # means this isn't a wire marker — treat as prose.
                    prose_lines.append(line)
                    i += 1
                    continue
                turn.wire_encoded = True
                if inline:   # fuzzy: inline content goes to prose
                    prose_lines.append(inline)
                i += 1
                continue

            # ── MOTION ───────────────────────────────────────────────────
            m = _MOTION_START_RE.match(stripped)
            if m:
                block = MotionBlock(device=m.group(1).strip())
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip():
                    block.raw_actions.append(lines[i].strip())
                    i += 1
                turn.motion_blocks.append(block)
                continue

            # ── INTERACT commands ────────────────────────────────────────
            m = _INTERACT_RE.match(stripped)
            if m:
                verb_str, target = m.groups()
                verb = self._parse_verb(verb_str)
                if verb is not None:
                    turn.interacts.append(InteractCmd(
                        verb=verb,
                        target=target.strip(),
                        raw=stripped,
                    ))
                    i += 1
                    continue

            # ── PLAN block ───────────────────────────────────────────────
            m = _PLAN_HEADER_RE.match(stripped)
            if m:
                plan_id = m.group(1).upper()
                plan = PlanBlock(plan_id=plan_id)
                i += 1
                while i < len(lines):
                    sm = _PLAN_STEP_RE.match(lines[i])
                    if sm:
                        label, desc, quals_str, conf_str = sm.groups()
                        conf = float(conf_str) if conf_str else 1.0
                        step = PlanStep(label=label, description=desc.strip(), confidence=conf)
                        if quals_str:
                            for kv in _PLAN_STEP_KV_RE.finditer(quals_str):
                                k, v = kv.group(1).lower(), kv.group(2)
                                if k == "complexity":
                                    step.complexity = v
                                elif k == "category":
                                    step.category = v
                                elif k == "file":
                                    step.file = v
                                elif k == "depends_on":
                                    step.depends_on = [x for x in v.split(",") if x]
                                elif k == "confidence":
                                    try:
                                        step.confidence = float(v)
                                    except ValueError:
                                        pass
                        plan.steps.append(step)
                        i += 1
                    else:
                        break
                turn.plans.append(plan)
                continue

            # ── CLAIM / INVARIANT ────────────────────────────────────────
            m = _CLAIM_RE.match(stripped)
            if m:
                kind, pos, assertion, conf_str = m.groups()
                conf = float(conf_str) if conf_str else 1.0
                turn.claims.append(ClaimBlock(
                    position=pos or "",
                    assertion=assertion.strip(),
                    confidence=conf,
                    is_invariant=kind.lower() == "invariant",
                ))
                i += 1
                continue

            # ── FORMAL-CLAIM ─────────────────────────────────────────────
            m = _FORMAL_CLAIM_RE.match(stripped)
            if m:
                pos, lang = m.groups()
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.claims.append(ClaimBlock(
                    position=pos or "",
                    assertion="",
                    formal_lang=lang,
                    formal_body=body,
                ))
                continue

            # ── REASONING block ──────────────────────────────────────────
            if _REASONING_START_RE.match(stripped):
                block = ReasoningBlock()
                i += 1
                step_re = _REASONING_STEP_RE if strict else _REASONING_STEP_FUZZY_RE
                conc_re = _REASONING_CONC_RE if strict else _REASONING_CONC_FUZZY_RE
                while i < len(lines):
                    sm = step_re.match(lines[i])
                    if sm:
                        _, hyp, verdict, conf_str = sm.groups()
                        conf = float(conf_str) if conf_str else 1.0
                        block.steps.append(ReasoningStep(
                            hypothesis=hyp.strip(), verdict=verdict.lower(), confidence=conf,
                        ))
                        i += 1
                        continue
                    cm = conc_re.match(lines[i])
                    if cm:
                        block.conclusion = cm.group(1).strip()
                        i += 1
                        continue
                    break
                turn.reasoning = block
                continue

            # ── MEMORY writes ────────────────────────────────────────────
            m = _REMEMBER_RE.match(stripped)
            if m:
                scope_str, pos, content = m.groups()
                scope = MemScope.SESSION if scope_str == "session" \
                    else MemScope.USER if scope_str == "user" \
                    else MemScope.WORKSPACE
                immediate = scope_str == "immediate"
                turn.memory_writes.append(MemoryWrite(
                    content=content.strip(),
                    scope=scope,
                    position=pos,
                    immediate=immediate,
                ))
                i += 1
                continue

            # ── TEST-SPEC ────────────────────────────────────────────────
            m = _TEST_SPEC_RE.match(stripped)
            if m:
                ref, lang = m.group(1), m.group(2) or "text"
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.test_specs.append(TestSpec(ref=ref, lang=lang, body=body))
                continue

            # ── EFFECTS ──────────────────────────────────────────────────
            m = _EFFECTS_RE.match(stripped)
            if m:
                ref, effects_str = m.group(1), m.group(2)
                effects: dict[str, list[str]] = {}
                # Support both space-separated "k=v1,v2 k2=v3" and
                # all-comma "k=v1,k2=v2" (fuzzy model output).
                # Split by whitespace first; if any token looks like
                # "k=v,k2=v2" (multiple = signs after comma-split),
                # re-tokenize by comma.
                tokens = effects_str.split()
                if len(tokens) == 1 and "," in tokens[0] and tokens[0].count("=") > 1:
                    tokens = tokens[0].split(",")
                for token in tokens:
                    token = token.strip()
                    if "=" in token:
                        k, v = token.split("=", 1)
                        effects[k.strip()] = [x for x in v.split(",") if x]
                turn.effects.append(EffectAnnotation(ref=ref, effects=effects))
                i += 1
                continue

            # ── DEP ──────────────────────────────────────────────────────
            m = _DEP_RE.match(stripped)
            if m:
                turn.dep_edges.append(DepEdge(
                    from_ref=m.group(1),
                    to_ref=m.group(2),
                    qualifier=m.group(3) or "",
                ))
                i += 1
                continue

            # ── SCHEMA ───────────────────────────────────────────────────
            m = _SCHEMA_RE.match(stripped)
            if m:
                ref, lang = m.group(1), m.group(2) or "json-schema"
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.schemas.append(SchemaDecl(ref=ref, lang=lang, body=body))
                continue

            # ── HINT ─────────────────────────────────────────────────────
            m = _HINT_RE.match(stripped)
            if m:
                turn.hints.append(HintBlock(tag=m.group(1), content=m.group(2).strip()))
                i += 1
                continue

            # ── DOCSTUB ──────────────────────────────────────────────────
            m = _DOCSTUB_RE.match(stripped)
            if m:
                ref = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.docstubs.append(DocStub(ref=ref, body=body))
                continue

            # ── PROMPT ───────────────────────────────────────────────────
            m = _PROMPT_RE.match(stripped)
            if m:
                label = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.sub_prompts.append(SubPrompt(label=label, body=body))
                continue

            # ── HANDOFF ──────────────────────────────────────────────────
            m = _HANDOFF_RE.match(stripped)
            if m:
                target = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.handoffs.append(HandoffPacket(target_model=target, body=body))
                continue

            # ── AGENT-STATE ──────────────────────────────────────────────
            m = _AGENT_STATE_RE.match(stripped)
            if m:
                scope = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.agent_states.append(AgentStateBlock(scope=scope, body=body))
                continue

            # ── MATH ─────────────────────────────────────────────────────
            m = _MATH_RE.match(stripped)
            if m:
                label = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.math_blocks.append(MathBlock(label=label, body=body))
                continue

            # ── TENSOR-SHAPE ─────────────────────────────────────────────
            m = _TENSOR_RE.match(stripped)
            if m:
                ref, dims_str, dtype = m.group(1), m.group(2), m.group(3)
                dims: dict[str, int] = {}
                for part in dims_str.split(","):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        try:
                            dims[k.strip()] = int(v.strip())
                        except ValueError:
                            pass
                    else:
                        try:
                            dims[str(len(dims))] = int(part)
                        except ValueError:
                            pass
                turn.tensor_shapes.append(TensorShape(ref=ref, dims=dims, dtype=dtype))
                i += 1
                continue

            # ── GRAPH ────────────────────────────────────────────────────
            m = _GRAPH_START_RE.match(stripped)
            if m:
                gb = GraphBlock(label=m.group(1))
                i += 1
                while i < len(lines):
                    em = _GRAPH_EDGE_RE.match(lines[i])
                    if em:
                        gb.edges.append(GraphEdge(
                            from_ref=em.group(1), to_ref=em.group(2), label=em.group(3) or "",
                        ))
                        i += 1
                    else:
                        break
                turn.graphs.append(gb)
                continue

            # ── TREE ─────────────────────────────────────────────────────
            m = _TREE_START_RE.match(stripped)
            if m:
                tb = TreeBlock(label=m.group(1))
                i += 1
                # Build tree from indent-depth hierarchy
                stack: list[tuple[int, TreeNode]] = []  # (indent, node)
                while i < len(lines):
                    nm = _TREE_NODE_RE.match(lines[i])
                    if not nm:
                        break
                    indent = len(nm.group(1))
                    node = TreeNode(ref=nm.group(2), label=(nm.group(3) or ""))
                    # Pop stack to find parent
                    while stack and stack[-1][0] >= indent:
                        stack.pop()
                    if stack:
                        stack[-1][1].children.append(node)
                    else:
                        tb.root = node
                    stack.append((indent, node))
                    i += 1
                turn.trees.append(tb)
                continue

            # ── STRUCTURED-CONF ──────────────────────────────────────────
            m = _STRUCT_CONF_RE.match(stripped)
            if m:
                sc = StructuredConf(ref=m.group(1))
                i += 1
                while i < len(lines):
                    sm = _STRUCT_CONF_ITEM_RE.match(lines[i])
                    if sm:
                        try:
                            sc.breakdown[sm.group(1).strip()] = float(sm.group(2))
                        except ValueError:
                            pass
                        i += 1
                    else:
                        break
                turn.structured_confs.append(sc)
                continue

            # ── DP ───────────────────────────────────────────────────────
            m = _DP_RE.match(stripped)
            if not m and not strict:
                m = _DP_FUZZY_RE.match(stripped)
            if m:
                try:
                    turn.dp_budgets.append(DPBudget(
                        epsilon=float(m.group(1)),
                        delta=float(m.group(2)),
                        mechanism=m.group(3) or "gaussian",
                    ))
                except ValueError:
                    pass
                i += 1
                continue

            # ── GRAMMAR ──────────────────────────────────────────────────
            m = _GRAMMAR_RE.match(stripped)
            if not m and not strict:
                m = _GRAMMAR_FUZZY_RE.match(stripped)
            if m:
                name = m.group(1)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.grammars.append(GrammarBlock(name=name, body=body))
                continue

            # ── ATTN ─────────────────────────────────────────────────────
            m = _ATTN_RE.match(stripped)
            if m:
                ref, weights_str = m.group(1), m.group(2)
                weights: dict[str, float] = {}
                for wm in _ATTN_WEIGHT_RE.finditer(weights_str):
                    try:
                        weights[wm.group(1)] = float(wm.group(2))
                    except ValueError:
                        pass
                turn.attn_annotations.append(AttnAnnotation(ref=ref, weights=weights))
                i += 1
                continue

            # ── EMBED ────────────────────────────────────────────────────
            m = _EMBED_RE.match(stripped)
            if m:
                ref, vals_str = m.group(1), m.group(2)
                values: list[float] = []
                for v in vals_str.split(","):
                    try:
                        values.append(float(v.strip()))
                    except ValueError:
                        pass
                turn.embeds.append(EmbedBlock(ref=ref, values=values))
                i += 1
                continue

            # ── COLLAB ───────────────────────────────────────────────────
            m = _COLLAB_RE.match(stripped)
            if m:
                turn.collab_blocks.append(CollabBlock(
                    author=m.group(1).strip(),
                    attribution=(m.group(2) or "").strip(),
                ))
                i += 1
                continue

            # ── FEDERATED ────────────────────────────────────────────────
            m = _FEDERATED_RE.match(stripped)
            if m:
                target, region = m.group(1), m.group(2)
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.federated_routes.append(FederatedRoute(target=target, region=region, body=body))
                continue

            # ── POLYGLOT ─────────────────────────────────────────────────
            m = _POLYGLOT_RE.match(stripped)
            if m:
                langs = [l.strip() for l in m.group(1).split(",")]
                bodies: dict[str, str] = {}
                i += 1
                for lang in langs:
                    if i < len(lines) and _FENCE_RE.match(lines[i].rstrip()):
                        body, i = self._read_code_body(lines, i, strict=strict)
                        bodies[lang] = body
                turn.polyglot_blocks.append(PolyglotBlock(langs=langs, bodies=bodies))
                continue

            # ── LEARNED-BPE ──────────────────────────────────────────────
            if _LEARNED_BPE_RE.match(stripped):
                body, i = self._read_code_body(lines, i + 1, strict=strict)
                turn.learned_bpe = LearnedBPEBlock(body=body)
                continue

            # ── Prose (unrecognised) ─────────────────────────────────────
            prose_lines.append(line)
            i += 1

        turn.text = "\n".join(prose_lines).strip()
        return turn

    # ── Helpers ──────────────────────────────────────────────────────────

    def _read_code_body(
        self, lines: list[str], start: int, *, strict: bool = True
    ) -> tuple[str, int]:
        """
        Read a fenced code block starting at lines[start].

        If lines[start] starts with ``` reads until the closing ```.
        Otherwise reads a single indented block until a blank line or
        a new protocol keyword.

        Returns (body_text, next_line_index).
        """
        if start >= len(lines):
            return "", start

        # Fenced block
        fence_m = _FENCE_RE.match(lines[start].rstrip())
        if fence_m:
            body_lines: list[str] = []
            i = start + 1
            while i < len(lines):
                if lines[i].rstrip() == "```":
                    return "\n".join(body_lines), i + 1
                body_lines.append(lines[i])
                i += 1
            return "\n".join(body_lines), i

        # Indented block (no fence) — 2+ spaces
        body_lines = []
        i = start
        while i < len(lines) and (lines[i].startswith("  ") or lines[i].strip() == ""):
            body_lines.append(lines[i])
            i += 1
        if body_lines:
            return "\n".join(body_lines).strip(), i

        # Fuzzy only: bare non-indented block — prompted model wrote body
        # flush-left without a fence. Read until a blank line whose next
        # non-blank line is at column 0. Blank lines inside indented content
        # (e.g. inside a docstring) are preserved.
        if strict:
            return "", start

        body_lines = []
        i = start
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                # Peek: find next non-blank line
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                # Stop if nothing follows, or next content is flush-left
                if j >= len(lines) or not lines[j].startswith(" "):
                    break
                # Blank line interior to an indented block — keep it
                body_lines.append(line)
                i += 1
            else:
                body_lines.append(line)
                i += 1
        return "\n".join(body_lines).strip(), i

    @staticmethod
    def _parse_verb(verb_str: str) -> InteractVerb | None:
        v = verb_str.lower().strip()
        mapping = {
            "expand doc": InteractVerb.DOCEXPAND,
            "expand": InteractVerb.EXPAND,
            "callers": InteractVerb.CALLERS,
            "callees": InteractVerb.CALLEES,
            "outline": InteractVerb.OUTLINE,
            "recall": InteractVerb.RECALL,
            "persona?": InteractVerb.PERSONA,
            "search semantic": InteractVerb.SEMANTIC,
            "search": InteractVerb.SEARCH,
        }
        return mapping.get(v)
