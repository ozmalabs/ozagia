"""Portable source-reading constructions adapted from Ocelot."""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Optional

_DEFN_STRUCTURE: dict = {
    "copula": ["is", "are", "is defined as", "is called", "refers to", "means"],
    "article": ["a", "an", "the", "this", "that", "these", "those"],
    "pronoun_predicate": [
        "it",
        "this",
        "that",
        "these",
        "those",
        "they",
        "them",
        "one",
        "something",
        "anything",
        "he",
        "she",
        "i",
        "you",
        "we",
    ],
    # The two leading-adjunct families. NOTHING is stripped: a FRAME adjunct
    # ('in <field>') is comprehended into frame=<field> (which Mt to ground in);
    # a REGISTER adjunct (hedge / exactness word) into register=approximate|exact
    # (the epistemic-confidence band). frame_phrase_skip are filler words inside
    # 'in the field of <field>' the frame extractor drops to reach the field head.
    "frame_markers": ["in", "within"],
    "frame_phrase_skip": [
        "the",
        "field",
        "of",
        "domain",
        "area",
        "context",
        "study",
        "branch",
        "discipline",
        "subject",
    ],
    "register_markers": {
        "roughly": "approximate",
        "informally": "approximate",
        "loosely": "approximate",
        "broadly": "approximate",
        "generally": "approximate",
        "intuitively": "approximate",
        "essentially": "approximate",
        "basically": "approximate",
        "simply": "approximate",
        "speaking": "approximate",
        "formally": "exact",
        "precisely": "exact",
        "technically": "exact",
        "strictly": "exact",
        "rigorously": "exact",
        "exactly": "exact",
    },
    "differentia_markers": [
        "that",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "used",
        "with",
        "for",
        "of",
        "in",
        "on",
        "from",
        "to",
        "having",
        "containing",
        "consisting",
        "characterized",
        "characterised",
        "defined",
        "similar",
        "such",
        "whereby",
        "given",
        "about",
        "concerned",
        "related",
        "called",
        "denoted",
        "satisfying",
        "equipped",
        "endowed",
        "together",
        "between",
        "among",
        "over",
        "under",
        "above",
        "below",
    ],
    "adjective_stop": [","],
    # ---- RESEARCH REGISTER (the dense "Let X be Y. A Z is a W ..." prose) ----
    # A PREAMBLE is the research-prose CONTEXT-SETTER: a leading "Let X be Y" /
    # "Given a W" / "Suppose ..." / "Fix ..." / "Consider ..." sentence (or
    # leading comma-clause) that BINDS objects for the definition to range over —
    # it is NOT the definition of the TERM. Comprehend it (the bindings ARE
    # context, not stripped) then look PAST it for the sentence that defines the
    # headword. preamble_markers lead such a clause/sentence; preamble_copula is
    # the binding copula inside it ('let X BE a ring' -> bind X is-a ring). The
    # connective_skip words ('then'/'thus'/'so'/'hence') lead the term definition
    # AFTER the preamble ('Fix C. THEN a poset is ...') — skipped to reach the term.
    "preamble_markers": [
        "let",
        "given",
        "suppose",
        "fix",
        "consider",
        "assume",
        "take",
        "write",
        "denote",
    ],
    "preamble_copula": ["be", "is", "are", "to be"],
    "connective_skip": ["then", "thus", "so", "hence", "therefore", "now"],
    # CONSTRUCTIVE / STRUCTURED definition forms (the research register's
    # non-copular definitions): "a <term> CONSISTS OF <parts>", "a <term> is a
    # <genus> TOGETHER WITH <structure>", "a <term> is a <genus> EQUIPPED WITH
    # ...". The constructive copula introduces the genus the same way 'is' does;
    # the parts/structure tail is the (constructive) differentia, kept not lost.
    "constructive_copula": [
        "consists of",
        "consist of",
        "is given by",
        "is defined to be",
        "is defined as",
    ],
    "constructive_markers": [
        "together",
        "equipped",
        "endowed",
        "consisting",
        "comprising",
        "comprised",
        "consists",
        "with",
    ],
    # A PARTITIVE HEDGE noun head ('a kind/sort/type of X'): genus = X (head PAST
    # the partitive) AND register = approximate (the partitive is a hedge).
    "partitive_hedges": {
        "kind": "approximate",
        "sort": "approximate",
        "type": "approximate",
        "form": "approximate",
        "variety": "approximate",
        "class": "approximate",
        "species": "approximate",
        "flavor": "approximate",
        "flavour": "approximate",
    },
    # The GENERATIVE MODEL: which slots are EXPECTED + how absence/difference
    # reads (mirrors the seed; absence/difference = SIGNAL, not a null).
    "expected_slots": {
        "frame": {"expected": True, "absent_means": "inherited", "different_means": "sense_shift"},
        "register": {"expected": True, "absent_means": "canonical", "hedge_means": "hedged"},
        "genus": {"expected": True, "different_means": "surprise"},
    },
    # register-class -> {status, confidence}. The UNMARKED case is 'canonical'.
    "register_band": {
        "canonical": {"status": "canonical", "confidence": 1.0},
        "exact": {"status": "exact", "confidence": 1.0},
        "technical": {"status": "exact", "confidence": 1.0},
        "approximate": {"status": "hedged", "confidence": 0.85},
        "informal": {"status": "hedged", "confidence": 0.8},
    },
    # marker word -> register CLASS (band key).
    "register_class": {
        "roughly": "approximate",
        "loosely": "approximate",
        "broadly": "approximate",
        "generally": "approximate",
        "essentially": "approximate",
        "basically": "approximate",
        "simply": "approximate",
        "speaking": "approximate",
        "informally": "informal",
        "intuitively": "informal",
        "formally": "exact",
        "precisely": "exact",
        "technically": "technical",
        "strictly": "exact",
        "rigorously": "exact",
        "exactly": "exact",
    },
    "domain_mt_map": {
        "math": "mathematics",
        "maths": "mathematics",
        "geometry": "mathematics",
        "topology": "mathematics",
        "algebra": "mathematics",
        "arithmetic": "mathematics",
        "calculus": "mathematics",
        "physics": "physics",
        "mechanics": "physics",
        "chemistry": "chemistry",
        "biology": "biology",
        "computing": "computer_science",
        "cs": "computer_science",
        "music": "music",
        "linguistics": "language",
        "grammar": "language",
    },
}

_ACQUIRED_DEFN: dict = {
    # definitional OPENERS to SKIP past (a 'we say / we call' discourse-frame that
    # introduces the definiendum but is not itself part of the term). Merged into
    # the preamble-handling so the reader reaches the actual term.
    "definitional_openers": [],
    # additional COPULA surface forms the construction uses ('is said to be').
    "extra_copula": [],
    # additional DIFFERENTIA TERMINATORS that bound the genus (the property P) —
    # 'if'/'iff'/'whenever' end the property and begin the gating condition Q.
    "extra_differentia": [],
}


def _merge_acquired_defn(base: dict) -> dict:
    """MERGE the acquired definitional construction-shapes into a definition
    structure (the agent extends what its definition reader recognises by
    ACQUIRING new construction-types — graph data appended to _ACQUIRED_DEFN).
    Returns a NEW dict so the seed/base is not mutated. The SAME merge pattern
    load_math_prose uses for _ACQUIRED."""
    openers = _ACQUIRED_DEFN.get("definitional_openers", [])
    extra_cop = _ACQUIRED_DEFN.get("extra_copula", [])
    extra_diff = _ACQUIRED_DEFN.get("extra_differentia", [])
    if not (openers or extra_cop or extra_diff):
        return base
    s = dict(base)
    if openers:
        # a definitional opener ('we say', 'we call') is a discourse FRAME that
        # introduces the definiendum but is not part of the term — it is STRIPPED to
        # reach the actual term. Carried as graph data; extract_definition strips a
        # leading opener phrase before parsing (the SAME spirit as connective_skip,
        # one register wider — a multi-word opener phrase, not a single connective).
        s["definitional_openers"] = list(openers)
    if extra_cop:
        cop = list(s.get("copula", _DEFN_STRUCTURE["copula"]))
        for c in extra_cop:
            if c not in cop:
                cop = cop + [c]
        s["copula"] = cop
    if extra_diff:
        diff = list(s.get("differentia_markers", _DEFN_STRUCTURE["differentia_markers"]))
        for d in extra_diff:
            if d not in diff:
                diff = diff + [d]
        s["differentia_markers"] = diff
    return s


def load_definition_structure(substrate=None) -> dict:
    """Return the genus-differentia STRUCTURE the agent HOLDS. Reads the
    `definition` Concept's `structure` attr from the graph when a substrate is
    given (the held understanding drives the application); else the in-module
    mirror of the seed. The ACQUIRED definitional construction-shapes
    (_ACQUIRED_DEFN — research-register construction-types the agent read/abstracted)
    are MERGED in so extract_definition recognises them. A pure graph/data read +
    the acquired-shape merge — no decision."""
    base = _DEFN_STRUCTURE
    if substrate is not None:
        try:
            for cid in substrate.nodes("Concept"):
                a = substrate.node(cid)["attrs"]
                if a.get("name") == "definition" and a.get("structure"):
                    s = json.loads(a["structure"])
                    if isinstance(s, dict) and s.get("differentia_markers"):
                        base = s
                        break
        except Exception:
            pass
    return _merge_acquired_defn(base)


def _parse_adjuncts(
    toks: list[str],
    copula: list[str],
    frame_markers: set[str],
    frame_skip: set[str],
    register_class: dict[str, str],
) -> tuple[list[str], Optional[str], Optional[str]]:
    """COMPREHEND the leading adjuncts into structure — NOTHING is stripped /
    discarded. A leading comma-delimited segment that does not itself contain the
    copula is an adjunct the agent comprehends:

      · a FRAME adjunct (FIRST token a frame marker 'in'/'within',
        e.g. 'In mathematics,' / 'In the field of music,') -> frame=<field>:
        WHICH WORLD/MICROTHEORY the concept is grounded in (grounding is
        frame-relative — the frame says WHERE to ground). The field is the
        segment after the marker with the frame_skip filler words ('the field
        of') dropped.
      · a REGISTER adjunct (FIRST token a register marker 'roughly'/'formally'/…)
        -> a register CLASS ('approximate'/'informal'/'exact'/'technical'): HOW
        PRECISE/CONFIDENT the definition is (the epistemic-confidence band). A
        hedge lowers confidence; an exactness marker is full confidence.

    Returns (remaining tokens, frame, register_class). frame/register are None
    when no such adjunct is present (the unmarked case the caller reads as
    CANONICAL). Driven by the held markers — general, not a per-phrase list."""
    frame: Optional[str] = None
    register: Optional[str] = None
    cset = {c.split()[0] for c in copula}
    while True:
        try:
            comma = toks.index(",")
        except ValueError:
            break
        seg = toks[:comma]
        if not seg:
            toks = toks[comma + 1 :]
            continue
        low = [t.lower() for t in seg]
        # an adjunct must not itself carry the copula (else it is the predicate).
        if any(w in cset for w in low):
            break
        first = low[0]
        if first in frame_markers:
            # FRAME adjunct: the field is what follows the marker, with the
            # 'the field of' / 'the domain of' filler words dropped.
            field_toks = [t for t in seg[1:] if t.lower() not in frame_skip]
            if field_toks:
                frame = " ".join(field_toks).strip()
                toks = toks[comma + 1 :]
                continue
            break
        if first in register_class:
            # REGISTER adjunct: the epistemic band CLASS. The marker word carries
            # it ('roughly speaking' -> approximate via 'roughly').
            register = register_class[first]
            toks = toks[comma + 1 :]
            continue
        break
    return toks, frame, register


_SENT_SPLIT = re.compile(r"(?<=[.;])\s+")


def _split_sentences(text: str) -> list[str]:
    """Split a definition into sentences on '. ' / '; ' (the research-register
    'Let ... . A <term> is ... .' has the preamble as its own sentence). Keeps
    each sentence's surface; empties dropped."""
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _parse_preamble_clause(
    toks: list[str],
    preamble_markers: set[str],
    preamble_copula: list[str],
    articles: set[str],
    connective_skip: set[str],
) -> tuple[list[str], list[dict]]:
    """COMPREHEND + SKIP a leading PREAMBLE clause/sentence within a token list,
    returning (remaining tokens, bindings). A preamble is a leading segment whose
    FIRST token is a preamble marker (let/given/suppose/fix/consider/...). It is
    consumed up to its clause boundary — the first comma at top level for a
    leading 'Given X,' clause, or the whole sentence for 'Let X be Y'. Each
    'Let/Given <subject> [be] <kind>' yields a BINDING {var, kind} (the context
    the definition ranges over — comprehended, NOT discarded). A leading
    connective ('Then'/'Thus') after the preamble is also skipped so the genus
    extractor reaches the term. Returns the tokens AFTER the preamble (+ any
    leading connective) and the list of bindings (possibly empty)."""
    bindings: list[dict] = []
    changed = True
    # iterate: a definition may stack several preambles ('Let R be a ring. Let M
    # be an R-module. ...') — each is bound + skipped in turn.
    while changed and toks:
        changed = False
        low = [t.lower() for t in toks]
        if low[0] not in preamble_markers:
            break
        # find this preamble's boundary: a top-level comma (for 'Given X, ...')
        # bounds a leading clause; absent a comma the preamble IS the sentence
        # (the sentence-splitter already isolated it) -> consume all of it.
        try:
            comma = low.index(",")
        except ValueError:
            comma = None
        seg = toks[1:comma] if comma is not None else toks[1:]
        # COMPREHEND the binding: '<subject> [be/is/are] <kind>' -> {var, kind}.
        # Split on the binding copula; left = the bound variable surface, right =
        # the kind it ranges over (with a leading article dropped).
        seg_low = [t.lower() for t in seg]
        cop_at = None
        for ci, w in enumerate(seg_low):
            if w in preamble_copula:
                cop_at = ci
                break
        if cop_at is not None:
            # 'Let X be a ring' / 'Suppose X is a group' -> {var:X, kind:ring}.
            var = " ".join(seg[:cop_at]).strip(" ,")
            kind_toks = seg[cop_at + 1 :]
            if kind_toks and kind_toks[0].lower() in articles:
                kind_toks = kind_toks[1:]
            kind = " ".join(t for t in kind_toks if t != ",").strip()
            if kind:
                bindings.append({"var": var or None, "kind": kind})
        elif seg and seg[0].lower() in articles:
            # 'Given a 2-ring R' (no explicit copula) -> the introduced object IS
            # of the named kind (the article-led phrase, article dropped). Context
            # captured (var unknown without a copula) rather than discarded.
            kind = " ".join(t for t in seg[1:] if t != ",").strip()
            if kind:
                bindings.append({"var": None, "kind": kind})
        # consume the preamble.
        toks = toks[comma + 1 :] if comma is not None else []
        changed = True
        # skip a leading connective ('Then'/'Thus'/'so') + a following article so
        # the term definition head is reached ('Then a poset is ...').
        while toks and toks[0].lower() in connective_skip:
            toks = toks[1:]
    return toks, bindings


def _defines_term(toks: list[str], headword: str, copula: list[str], articles: set[str]) -> bool:
    """True iff a sentence's token list DEFINES the headword term: it contains a
    copula AND its subject (the tokens before the first copula, minus a leading
    article) mentions the headword. Used to PREFER the sentence about the TERM
    over a preamble's binding sentence ('Let R be a ring' does NOT define
    'module'; 'A module is finite type ...' does)."""
    if not headword:
        return False
    span = _find_copula(toks, copula)
    if span is None:
        return False
    subj = " ".join(t for t in toks[: span[0]] if t != ",").lower()
    hw = headword.replace("_", " ").lower().strip()
    head = hw.split()[-1] if hw else ""
    return bool((hw and hw in subj) or (head and head in subj))


def _find_copula(toks: list[str], copula: list[str]) -> Optional[tuple[int, int]]:
    """Find the FIRST copula in the token stream. Supports multi-word copulas
    ('is defined as', 'refers to'). Returns (start_index, end_index_exclusive)
    of the copula span, or None (no copula -> not a definitional sentence)."""
    low = [t.lower() for t in toks]
    # longest copulas first so 'is defined as' wins over 'is'.
    for cop in sorted(copula, key=lambda c: -len(c.split())):
        parts = cop.split()
        n = len(parts)
        for i in range(len(low) - n + 1):
            if low[i : i + n] == parts:
                return (i, i + n)
    return None


def extract_definition(
    text: str, *, substrate=None, structure: Optional[dict] = None, term: Optional[str] = None
) -> Optional[dict]:
    """THE ONE general genus-extraction — realize the held genus-differentia
    STRUCTURE over ANY definitional sentence.

    RESEARCH REGISTER (the dense "Let X be Y. A Z is a W ..." prose): a leading
    PREAMBLE ("Let X be Y" / "Given a W" / "Suppose ..." / "Fix ..." / a leading
    "Given X," clause / a "Then ..." connective after it) BINDS context and is
    NOT the definition of the TERM. It is COMPREHENDED into `bindings` (X is-a Y
    — context, never stripped) and SKIPPED; the genus is then extracted from the
    sentence that DEFINES the headword (`term`, the page title, when supplied —
    else the first non-preamble copula sentence). This is what stops the grammar
    grabbing the preamble's genus ('Let A be an abelian category. A spectral
    sequence is ...' -> binds A is-a abelian_category, extracts genus from the
    spectral-sequence sentence). The CONSTRUCTIVE forms ('consists of <parts>',
    'is a <genus> together with <structure>') are read like the copula — the
    genus extracted, the parts/structure kept as constructive differentia.

    COMPREHEND AGAINST EXPECTATIONS (the entity part). A competent reader holds a
    generative MODEL of a definition (expects FRAME, REGISTER, TERM is-a GENUS,
    DIFFERENTIA) and reads each slot PRESENT | ABSENT | DIFFERENT — absence and
    difference are SIGNAL (prediction-error = information), not a null default:
      · REGISTER ABSENT (unmarked) = the CANONICAL, asserted-as-fact case ->
        register 'canonical', status 'canonical', FULL confidence (a comprehended
        signal 'stated as definitional truth', not a missing value).
      · REGISTER a hedge ('roughly'/'loosely'/'informally', OR the partitive
        'a kind/sort/type of') = a DEVIATION -> register 'approximate'/'informal',
        status 'hedged', LOWER confidence.
      · FRAME ABSENT = inherited from the active discourse frame / universal.
      · GENUS is read as the HEAD past a PARTITIVE hedge ('a kind of X' -> X).

    Find the COPULA -> the term precedes it -> the GENUS NP follows: skip an
    inline adverbial / article, take the head noun phrase up to a DIFFERENTIA
    marker. Inline / post-copula register markers ('X is, roughly speaking, a Y',
    'X is informally a Y') are COMPREHENDED (scanned inside the predicate too,
    not only leading). Returns {term, genus, differentia, frame, register,
    register_status, confidence, signals, slots} — or None when there is no
    extractable genus (no copula / a pronoun-only predicate).

    `signals` / `slots` carry the present|absent|different STATUS per slot (the
    entity reading): the caller's grounding consumes them (sense-shift on a
    different frame; surprise on a different genus; canonical-vs-hedged confidence
    from the register). Driven by the ONE held structure (read from the
    `definition` concept), generalising across surface forms — NOT a per-form
    pattern."""
    if not text:
        return None
    st = structure or load_definition_structure(substrate)
    copula = st.get("copula", _DEFN_STRUCTURE["copula"])
    articles = set(st.get("article", _DEFN_STRUCTURE["article"]))
    frame_markers = set(st.get("frame_markers", _DEFN_STRUCTURE["frame_markers"]))
    frame_skip = set(st.get("frame_phrase_skip", _DEFN_STRUCTURE["frame_phrase_skip"]))
    # register_class: marker word -> register CLASS (band key). Back-compat: a
    # held structure may still only carry the older register_markers
    # (word -> 'approximate'|'exact'); fall through to the fallback class map.
    register_class = st.get("register_class")
    if not isinstance(register_class, dict):
        register_class = _DEFN_STRUCTURE["register_class"]
    register_band = st.get("register_band", _DEFN_STRUCTURE["register_band"])
    if not isinstance(register_band, dict):
        register_band = _DEFN_STRUCTURE["register_band"]
    partitive = st.get("partitive_hedges", _DEFN_STRUCTURE["partitive_hedges"])
    if not isinstance(partitive, dict):
        partitive = _DEFN_STRUCTURE["partitive_hedges"]
    diff_markers = set(st.get("differentia_markers", _DEFN_STRUCTURE["differentia_markers"]))
    pronouns = set(st.get("pronoun_predicate", _DEFN_STRUCTURE["pronoun_predicate"]))
    # research-register markers (graph data; back-compat fallbacks for an older
    # held structure that predates the research register).
    preamble_markers = set(st.get("preamble_markers", _DEFN_STRUCTURE["preamble_markers"]))
    preamble_copula = st.get("preamble_copula", _DEFN_STRUCTURE["preamble_copula"])
    connective_skip = set(st.get("connective_skip", _DEFN_STRUCTURE["connective_skip"]))
    # ACQUIRED definitional OPENERS ('we say', 'we call') — a discourse frame that
    # introduces the definiendum; stripped to reach the term. Empty unless acquired.
    definitional_openers = [op.lower().split() for op in st.get("definitional_openers", [])]
    constructive_copula = st.get("constructive_copula", _DEFN_STRUCTURE["constructive_copula"])
    # the genus-introducing copulas = the plain copula + the CONSTRUCTIVE ones
    # ('consists of', 'is given by') — both introduce the genus the same way.
    gen_copula = list(copula) + [c for c in constructive_copula if c not in copula]

    # RESEARCH-REGISTER PREAMBLE handling (the live fix). Split the definition
    # into sentences; COMPREHEND + SKIP any leading preamble sentence(s) ('Let X
    # be Y') into BINDINGS, and PREFER the sentence that DEFINES the headword
    # term over the preamble's binding sentence. NOTHING is stripped — the
    # bindings are returned as context. Falls through unchanged for a plain
    # one-sentence definition (no preamble marker -> bindings empty, same text).
    bindings: list[dict] = []
    preamble_seen = False
    sentences = _split_sentences(text)
    if len(sentences) > 1:
        # comprehend leading whole-sentence preambles into bindings + drop them,
        # keeping the remaining (definition-bearing) sentences.
        kept: list[str] = []
        for si, sent in enumerate(sentences):
            stoks = re.findall(r"[A-Za-z0-9'-]+|,", sent)
            slow = [t.lower() for t in stoks]
            is_pre = bool(slow) and slow[0] in preamble_markers
            # a preamble sentence that ALSO defines the headword is kept (rare:
            # 'Let X be the smallest ...' naming the term) — only skip it when it
            # does NOT define the term.
            if is_pre and not _defines_term(stoks, term or "", gen_copula, articles):
                _, b = _parse_preamble_clause(
                    stoks, preamble_markers, preamble_copula, articles, connective_skip
                )
                bindings.extend(b)
                preamble_seen = True
                continue
            kept.append(sent)
        # CHOOSE the working sentence: PREFER one whose subject mentions the
        # headword term (the definition OF the term); else the FIRST kept
        # copula sentence; else the first kept sentence.
        chosen = None
        if term:
            for sent in kept:
                stoks = re.findall(r"[A-Za-z0-9'-]+|,", sent)
                if _defines_term(stoks, term, gen_copula, articles):
                    chosen = sent
                    break
        # an ACQUIRED definitional OPENER sentence ('We say X is P if Q') IS the
        # definition of the term — PREFER it over an earlier preamble/constructive
        # copula sentence (the predicative-definition construction the agent
        # acquired). Empty unless a definitional opener was acquired.
        if chosen is None and definitional_openers:
            for sent in kept:
                slow = [t.lower() for t in re.findall(r"[A-Za-z0-9'-]+", sent)]
                if any(slow[: len(op)] == op for op in definitional_openers):
                    chosen = sent
                    break
        if chosen is None:
            for sent in kept:
                stoks = re.findall(r"[A-Za-z0-9'-]+|,", sent)
                if _find_copula(stoks, gen_copula) is not None:
                    chosen = sent
                    break
        if chosen is None and kept:
            chosen = kept[0]
        if chosen is not None:
            text = chosen

    # tokenise keeping commas (they bound the leading adjuncts + adjective runs).
    # Underscore is kept INSIDE a token: an underscore-joined name
    # ('body_of_water', 'physical_entity') is a single LEXICAL UNIT (the WordNet
    # lemma / seeded-concept convention), not a genus head + 'of'-differentia to
    # split. This matches the acquired-construction tokenizer (which already
    # keeps '_'); the genus extractor was the inconsistent path.
    toks = re.findall(r"[A-Za-z0-9'_-]+|,", (text or "").strip())
    if not toks:
        return None

    # ACQUIRED DEFINITIONAL OPENER ('We say', 'We call', 'We say that') — a
    # discourse frame that introduces the definiendum but is NOT part of the term.
    # STRIP a matching leading opener phrase (+ a trailing 'that'/'an'/'a' the
    # opener carries) so the reader reaches the actual term. This is the
    # construction-shape the agent ACQUIRED (cmsg81); empty unless acquired.
    def _strip_opener(tk):
        if not definitional_openers:
            return tk, False
        # the opener may sit after a leading comma-bounded adjunct that carries no
        # copula ('For 0 <= ell <= infinity, we say X is P ...') — if so, drop that
        # leading segment first so the opener is reached (NOTHING definitional is
        # dropped: the segment has no copula, it is a quantifier-range adjunct).
        cand = tk
        if "," in [t.lower() for t in tk]:
            comma = [t.lower() for t in tk].index(",")
            seg = [t.lower() for t in tk[:comma]]
            cset = {c.split()[0] for c in gen_copula}
            after = tk[comma + 1 :]
            if (
                after
                and not any(w in cset for w in seg)
                and after[0].lower() in {o[0] for o in definitional_openers if o}
            ):
                cand = after
        low_t = [t.lower() for t in cand]
        for opener in sorted(definitional_openers, key=lambda o: -len(o)):
            n = len(opener)
            if n and low_t[:n] == opener:
                cand = cand[n:]
                if cand and cand[0].lower() == "that":  # 'we say THAT X is'
                    cand = cand[1:]
                return cand, True
        return tk, False

    toks, _opened = _strip_opener(toks)
    if _opened:
        preamble_seen = True

    # A leading CONNECTIVE on the working sentence ('Then a poset is ...', 'Thus
    # its objects are ...') — strip it so the term definition head is reached. A
    # definitional sentence never legitimately opens with a discourse connective
    # ('then'/'thus'/'hence'/...): it is the tail of a preamble whose context-
    # setting sentence preceded it (often already dropped by the corpus reader).
    # Bounded: a leading run of held connective words, then stop. Flags the
    # preamble (the connective marks one was present) so the bound-as-context
    # signal fires.
    if toks and toks[0].lower() in connective_skip:
        while toks and toks[0].lower() in connective_skip:
            toks = toks[1:]
        preamble_seen = True
    # A leading PREAMBLE CLAUSE within the working sentence ('Given a 2-ring, a
    # 2-line over R is ...'): comprehend it into bindings + skip to the term
    # definition. Runs on the chosen sentence so a one-sentence 'Given X, a
    # <term> is ...' is also handled.
    if toks and toks[0].lower() in preamble_markers:
        preamble_seen = True
    toks, clause_binds = _parse_preamble_clause(
        toks, preamble_markers, preamble_copula, articles, connective_skip
    )
    bindings.extend(clause_binds)
    if not toks:
        return None

    # COMPREHEND the leading frame + register adjuncts (NOTHING stripped). The
    # leading register is a CLASS ('approximate'/'informal'/'exact'/'technical').
    toks, frame, register = _parse_adjuncts(
        toks, gen_copula, frame_markers, frame_skip, register_class
    )
    # a definitional opener can sit AFTER a leading 'For ...,' adjunct/preamble that
    # the steps above consumed ('For 0 <= ell <= infinity, we say X is P if Q') —
    # strip it again now that the leading clause is gone (the acquired construction).
    toks, _opened2 = _strip_opener(toks)
    if _opened2:
        preamble_seen = True
    span = _find_copula(toks, gen_copula)
    if span is None:
        return None  # no copula -> not a definitional sentence (honest None).
    ci, cj = span

    # TERM = the subject before the copula, minus a leading article. A pronoun-
    # only predicate (the GENUS side) is handled below; an empty term is honest
    # un-comprehension.
    term_toks = [t for t in toks[:ci] if t != ","]
    if term_toks and term_toks[0].lower() in articles:
        term_toks = term_toks[1:]
    term = " ".join(term_toks).strip()
    if not term:
        return None

    # PREDICATE = everything after the copula.
    pred = toks[cj:]
    # COMPREHEND an inline / post-copula adverbial register marker ('is, roughly
    # speaking, a geometrical space'): the comma-delimited segment right after the
    # copula. NOTHING dropped — if the segment is a register marker, it is read
    # into `register` (the deviation), then the segment is consumed.
    if pred and pred[0] == ",":
        try:
            close = pred.index(",", 1)
            seg = [t for t in pred[1:close] if t != ","]
        except ValueError:
            close = None
            seg = [t for t in pred[1:] if t != ","]
        if seg and seg[0].lower() in register_class:
            register = register or register_class[seg[0].lower()]
        pred = pred[close + 1 :] if close is not None else pred[1:]
    # drop a leading article.
    if pred and pred[0].lower() in articles:
        pred = pred[1:]
    # COMPREHEND a NON-comma inline register marker right after the copula/article
    # ('is informally a device', 'is roughly a device'): the marker word leads the
    # predicate, optionally followed by another article. Read it into `register`,
    # consume the marker (+ a following article), so the genus head is reached.
    if pred and pred[0].lower() in register_class:
        register = register or register_class[pred[0].lower()]
        pred = pred[1:]
        if pred and pred[0].lower() in articles:
            pred = pred[1:]
    # drop a leading comma-separated ADJECTIVE run ('a smooth, projective,
    # algebraic curve' -> the genus head is 'curve'): strip any (word ,)* prefix.
    while len(pred) >= 2 and pred[1] == ",":
        pred = pred[2:]

    # GENUS span = head noun phrase up to the first DIFFERENTIA marker / comma.
    genus_toks: list[str] = []
    diff_start = len(pred)
    for k, t in enumerate(pred):
        tl = t.lower()
        if t == "," or tl in diff_markers:
            diff_start = k
            break
        genus_toks.append(t)
    differentia = " ".join(t for t in pred[diff_start:] if t != ",").strip()

    # PARTITIVE HEDGE: 'a kind/sort/type of X' -> the genus is X (HEAD PAST the
    # partitive), and the partitive itself is a hedge (register approximate). The
    # genus head word is a partitive noun and the differentia tail begins 'of …':
    # comprehend the head of that 'of'-phrase as the real genus, NOTHING dropped.
    partitive_hedge = False
    if genus_toks and genus_toks[-1].lower() in partitive and differentia.lower().startswith("of "):
        partitive_marker = genus_toks[-1].lower()
        of_rest = differentia[3:].strip()
        of_toks = of_rest.split()
        # the real genus = the of-phrase head NP up to the next differentia marker.
        real_genus: list[str] = []
        rest_after: list[str] = []
        crossed = False
        for w in of_toks:
            if not crossed and w.lower() not in diff_markers:
                real_genus.append(w)
            else:
                crossed = True
                rest_after.append(w)
        if real_genus:
            genus_toks = real_genus
            differentia = " ".join(rest_after).strip()
            # the partitive itself is the hedge -> register approximate (its held
            # mapping); a leading register adjunct, if any, already stands.
            register = register or partitive.get(partitive_marker, "approximate")
            partitive_hedge = True

    genus = " ".join(genus_toks).strip()
    if not genus:
        return None  # empty predicate -> honest un-comprehension.
    # a PRONOUN-only predicate ('X is it / this / that') names no genus the agent
    # can ground out -> honest un-comprehension (the genuine remaining limit).
    if all(g.lower() in pronouns for g in genus_toks):
        return None

    # READ-AGAINST-EXPECTATIONS: resolve the register CLASS -> {status, confidence}
    # band. ABSENT register (unmarked) = the CANONICAL asserted-as-fact case (a
    # comprehended signal, FULL confidence), not a missing value.
    reg_class = register or "canonical"
    band = (
        register_band.get(reg_class)
        or register_band.get("canonical")
        or {"status": "canonical", "confidence": 1.0}
    )
    register_status = band.get("status", "canonical")
    confidence = float(band.get("confidence", 1.0))

    # The present|absent|different SIGNALS per slot (the entity reading). frame
    # and genus DIFFERENCE are resolved by the GROUNDING (which holds the term's
    # existing frame/genus); here we surface the locally-readable status.
    signals: dict[str, Any] = {}
    slots: dict[str, str] = {}
    # register: present (marked) vs absent (canonical/unmarked).
    if register is None:
        slots["register"] = "absent"
        signals["register"] = "canonical"  # asserted-as-fact (comprehended).
    else:
        slots["register"] = "present"
        signals["register"] = "hedged" if register_status == "hedged" else "marked_exact"
    if partitive_hedge:
        signals["partitive_hedge"] = True
    # frame: present vs absent (inherited). DIFFERENT (sense-shift) is decided at
    # GROUND time against the term's held frame; flagged present here.
    slots["frame"] = "present" if frame else "absent"
    if not frame:
        signals["frame"] = "inherited"
    # genus is always present here (None-genus returned above); SURPRISE
    # (different from the held genus) is decided at GROUND time.
    slots["genus"] = "present"

    # PREAMBLE BINDINGS (the research-register context) carried through — NOT
    # discarded. The bindings are the 'Let X be Y' objects the definition ranges
    # over (X is-a Y); the grounding can use them as context. A present-but-empty
    # list = no preamble (the canonical plain definition). `preamble` flags that a
    # preamble was comprehended-and-bound (the live fix fired).
    if preamble_seen or bindings:
        slots["preamble"] = "present"
        signals["preamble"] = "bound" if bindings else "skipped"
    return {
        "term": term,
        "genus": genus,
        "differentia": differentia,
        "frame": frame,
        "register": reg_class,
        "register_status": register_status,
        "confidence": confidence,
        "signals": signals,
        "slots": slots,
        "bindings": bindings,
    }


_FACT_HARVEST = Path(__file__).resolve().parent / "data" / "math_fact_harvest.json"

CONSTRUCTION_TYPE_VOCAB: dict = {
    "constructive_definition": {
        "kind": "definitional",
        # a constructive definition INTRODUCES the genus via a structural copula
        # ('a monoid CONSISTS OF a set ...', 'a module is a group EQUIPPED WITH ...').
        "openers": [],
        "copulas": ["consists of", "consist of", "is given by"],
        "conditions": [],
    },
    "universal_property_definition": {
        "kind": "definitional",
        # a universal-property definition is a plain copula 'X is the TERMINAL/INITIAL
        # OBJECT ...' — the genus is the universal object; no extra opener needed, the
        # base copula already reads it (so the acquire is expected to be REDUNDANT).
        "openers": [],
        "copulas": [],
        "conditions": [],
    },
    # the LOGICAL/QUANTIFIED types carry their recurring opener under `quantifiers`
    # (NOT `openers`): a quantifier BINDS a variable / a relation — it does NOT
    # introduce a definiendum, so it must NOT be minted as a definitional opener (that
    # would mis-read a bound variable as a defined object — the over-grab the precision
    # floor exists to reject). The agent RECOGNISES the type by its quantifier but
    # mints NO definitional marker -> the honest dry-up (a DIFFERENT KIND of claim).
    "existential_claim": {
        "kind": "logical",
        # 'there exists Z such that P' — a quantified EXISTENCE claim. The opener is a
        # quantifier binding a NEW variable Z, not a definiendum; there is no
        # object->genus (the 'term' would be the bound variable / the such-that clause).
        "openers": [],
        "copulas": [],
        "conditions": [],
        "quantifiers": ["there exists", "there is", "there exist"],
    },
    "universal_claim": {
        "kind": "logical",
        # 'for all/every X, P' — a quantified UNIVERSAL claim. The opener binds a
        # RANGE, not a definiendum; the 'term' is the quantified clause -> no
        # object->genus.
        "openers": [],
        "copulas": [],
        "conditions": [],
        "quantifiers": ["for all", "for every", "for any", "for each"],
    },
    "tfae_claim": {
        "kind": "logical",
        # 'the following are equivalent' — a LOGICAL-EQUIVALENCE claim over a list of
        # conditions; no single object IS-A genus.
        "openers": [],
        "copulas": [],
        "conditions": [],
        "quantifiers": [
            "the following are equivalent",
            "the following conditions are equivalent",
            "tfae",
        ],
    },
    "adjunction_claim": {
        "kind": "logical",
        # 'F is left/right adjoint to G' — a RELATIONAL claim between two functors;
        # 'left adjoint' is a relation, not a genus the object is a kind of.
        "openers": [],
        "copulas": [],
        "conditions": [],
        # the relational markers the agent reads to RECOGNISE the type (it is a
        # relation, not a definition — surfaced so the WALK can name it).
        "quantifiers": ["left adjoint", "right adjoint", "adjoint to", "adjunction"],
    },
}


def load_fact_harvest(path: Path = _FACT_HARVEST) -> dict:
    """Load the fact-harvest PATTERNS (graph data). {invariants:[...],
    identifications:[...]}; empty families on a read miss."""
    try:
        g = json.loads(Path(path).read_text())
    except Exception:
        return {"invariants": [], "identifications": []}
    return {"invariants": g.get("invariants", []), "identifications": g.get("identifications", [])}


def _clean_slot(s: str) -> str:
    """Tidy a captured object/value surface: collapse whitespace, strip stray
    punctuation/brackets, drop a leading article + a leading discourse
    connective ('Then'/'Thus'/'Every'/...) the greedy match dragged in. A
    surface-normalisation (the lexicon convention), NOT a meaning decision."""
    t = re.sub(r"\s+", " ", (s or "")).strip().strip(" ,.;:").strip()
    # drop a leading discourse connective + article the left-object grab pulled in
    # ('Then M^4' -> 'M^4'; 'Every leaf of' handled by the dangling-prep reject).
    t = re.sub(
        r"^(?:then|thus|so|hence|therefore|now|moreover|indeed|"
        r"clearly|every|each|the|a|an|that|this)\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"^(the|a|an)\s+", "", t, flags=re.IGNORECASE)
    return t.strip()


_QUALITATIVE_VALUES = {
    "computable",
    "effectively",
    "finitely",
    "generated",
    "nilpotent",
    "bounded",
    "free",
    "abelian",
    "cyclic",
    "torsion",
    "nontrivial",
    "nondegenerate",
    "smooth",
    "compact",
    "connected",
    "simply",
    "class",
    "above",
    "below",
    "infinite",
    "unbounded",
    "constant",
    "well",
    "defined",
    "surjective",
    "injective",
    "bijective",
    "continuous",
    "open",
    "closed",
}


def _concrete_value(v: str) -> bool:
    """True iff a captured invariant VALUE is a CONCRETE computed answer (a group
    / number / named object), not a qualitative property word. '0', 'Z_2', 'Z^r',
    'Z times Z', 'Q' pass; 'computable', 'finitely generated' fail."""
    t = _clean_slot(v)
    if not t:
        return False
    words = [w for w in re.findall(r"[A-Za-z]+", t.lower())]
    if words and all(w in _QUALITATIVE_VALUES for w in words):
        return False
    # a bare number, or a value carrying a group/space symbol, is concrete.
    if re.fullmatch(r"-?\d+", t):
        return True
    if re.search(
        r"[ZQRCNzZ0-9/^_]|times|oplus|otimes|wedge|sphere|"
        r"ball|torus|space|P\^|S\^|R\^|C\^",
        t,
    ):
        return True
    return False


def _plausible_object(s: str) -> bool:
    """A captured slot is a plausible math OBJECT/value: non-empty, not pure
    filler, balanced brackets, not a mid-token fragment, has an alphanumeric
    token. The anchored honest floor — a noisy capture (only stop-words, a
    fragment starting mid-expression like ')^n-orbit', or an unbalanced-paren
    grab) is REJECTED. This is the precision lever: prefer a clean object over a
    truncated one (recall traded for precision, the honest residual)."""
    t = _clean_slot(s)
    if not t or len(t) > 80:
        return False
    if not re.search(r"[A-Za-z0-9]", t):
        return False
    # a capture that OPENS with a closing bracket / stray operator is a
    # mid-expression fragment (the greedy-match residual) — reject it.
    if t[0] in ")]}^_/,;:":
        return False
    # balanced (round) brackets — an unbalanced grab ran past / short of a
    # delimiter; the object boundary is wrong.
    if t.count("(") != t.count(")"):
        return False
    # reject a capture that is ONLY connective/filler words.
    filler = {
        "is",
        "are",
        "and",
        "or",
        "the",
        "a",
        "an",
        "of",
        "to",
        "be",
        "this",
        "that",
        "it",
        "we",
        "such",
        "some",
        "any",
        "then",
        "where",
        "which",
        "for",
        "if",
        "with",
        "so",
        "thus",
        "hence",
        "note",
        "one",
        "groups",
        "let",
        "given",
        "above",
        "below",
        "bounded",
    }
    words = [w for w in re.findall(r"[A-Za-z0-9]+", t.lower())]
    if not words:
        return False
    if all(w in filler for w in words):
        return False
    # a phrase ending in a dangling preposition/connective is a truncated grab.
    if words[-1] in {"of", "to", "in", "on", "the", "a", "an", "over", "for", "with", "and", "or"}:
        return False
    # a clausal connective INSIDE the capture ('... such that ...', '... to X
    # ...') means the grab crossed a clause boundary — a fragment, reject.
    low = " " + t.lower() + " "
    if re.search(r"\b(such that|to which|in which|so that|note that)\b", low):
        return False
    # a left/right object should not OPEN with a bare lower-case word fragment
    # whose first letters look truncated ('ssociated', 'estricted') — heuristic:
    # a single long lower-case opening word that is not a known math token and
    # not in a small math-word whitelist is likely a chopped word. Conservative:
    # only reject when the WHOLE capture is one lower-case alpha word > 6 chars.
    if (
        len(words) == 1
        and t.islower()
        and t.isalpha()
        and len(t) > 6
        and t
        not in {
            "boundary",
            "manifold",
            "cohomology",
            "homology",
            "homotopy",
            "spectrum",
            "spectra",
            "category",
            "morphism",
            "operator",
            "subspace",
            "subgroup",
        }
    ):
        return False
    # the OPENING word must not be a SUFFIX-fragment of a relation/connective
    # keyword the regex matched mid-word ('...is iSOMORPHIC to' -> 'somorphic';
    # '...aSSOCIATED to' -> 'ssociated'). A chopped opener = a fragment grab.
    first = words[0]
    _CHOP = (
        "somorphic",
        "omorphic",
        "morphic",
        "iffeomorphic",
        "ffeomorphic",
        "quivalent",
        "uivalent",
        "ssociated",
        "ssociate",
        "estricted",
        "efined",
        "enoted",
        "iven",
        "ndowed",
        "quipped",
    )
    if first in _CHOP:
        return False
    return True


def harvest_invariants(prose: str, patterns: Optional[list] = None) -> list[dict]:
    """Harvest COMPUTED-INVARIANT facts from a prose statement via the seeded
    invariant patterns. Returns [{object, invariant_kind, value, pattern}], the
    object->invariant->value rows. Each pattern is graph data: an object_re
    anchor (a homology/homotopy/rank operator on a space), an optional joiner_re
    (the iso/equality marker), and a value_re (the concrete group/number). Empty
    list = no anchored invariant found (the honest miss)."""
    if not prose:
        return []
    pats = patterns if patterns is not None else load_fact_harvest()["invariants"]
    text = re.sub(r"\s+", " ", prose)
    out: list[dict] = []
    seen: set = set()
    for p in pats:
        obj_re = p.get("object_re")
        val_re = p.get("value_re")
        if not obj_re or not val_re:
            continue
        join = p.get("joiner_re", "")
        full = obj_re + join + val_re
        try:
            rx = re.compile(full)
        except re.error:
            continue
        for m in rx.finditer(text):
            grps = [g for g in m.groups() if g is not None]
            if not grps:
                continue
            # the LAST captured group is the value; the rest are object parts.
            value = _clean_slot(grps[-1])
            obj_parts = [_clean_slot(g) for g in grps[:-1] if _clean_slot(g)]
            obj = " ".join(obj_parts).strip()
            kind = p.get("kind", "invariant")
            # a templated kind ('{group_kind}') resolves to the first captured
            # object part (the invariant-kind word, e.g. 'homology').
            if kind.startswith("{") and obj_parts:
                kind = obj_parts[0].lower()
                obj = " ".join(obj_parts).strip()
            if not _plausible_object(obj) or not _plausible_object(value):
                continue
            # the value of a COMPUTED invariant must be a concrete answer (a
            # group / number / named object), not a qualitative property word.
            if not _concrete_value(value):
                continue
            key = (obj.lower(), kind, value.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "object": obj,
                    "invariant_kind": kind,
                    "value": value,
                    "pattern": p.get("name", ""),
                }
            )
    return out


def harvest_identifications(prose: str, patterns: Optional[list] = None) -> list[dict]:
    """Harvest IDENTIFICATION facts (object IS homeomorphic / diffeomorphic /
    isomorphic / equivalent / identified-with / classifying-space-of another
    object) via the seeded identification patterns. Returns [{object, relation,
    target, pattern}]. Empty list = no anchored identification (honest miss)."""
    if not prose:
        return []
    pats = patterns if patterns is not None else load_fact_harvest()["identifications"]
    text = re.sub(r"\s+", " ", prose)
    out: list[dict] = []
    seen: set = set()
    for p in pats:
        left_re = p.get("left_re")
        right_re = p.get("right_re")
        if not left_re or not right_re:
            continue
        try:
            rx = re.compile(left_re + right_re)
        except re.error:
            continue
        for m in rx.finditer(text):
            grps = [g for g in m.groups() if g is not None]
            if len(grps) < 2:
                continue
            left = _clean_slot(grps[0])
            right = _clean_slot(grps[-1])
            if not _plausible_object(left) or not _plausible_object(right):
                continue
            rel = p.get("relation", "identified_with")
            key = (left.lower(), rel, right.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {"object": left, "relation": rel, "target": right, "pattern": p.get("name", "")}
            )
    return out


def harvest_facts(prose: str) -> dict:
    """Harvest BOTH families from one statement's prose. Returns
    {invariants:[...], identifications:[...]} — the object-level facts the agent's
    construction grammar mined. The harvest entry point the arXiv pipeline calls
    over each parsed statement."""
    return {
        "invariants": harvest_invariants(prose),
        "identifications": harvest_identifications(prose),
    }


def _quantifier_markers() -> list[tuple[str, str]]:
    """(marker, kind) pairs from the held logical CONSTRUCTION_TYPE_VOCAB entries —
    the agent's OWN quantifier recognition, longest-marker-first so 'for every'
    wins over 'for'. kind = forall / exists / tfae / adjunction (the vocab key
    stem). NOT a new list — a read of the held vocab."""
    kind_of = {
        "universal_claim": "forall",
        "existential_claim": "exists",
        "tfae_claim": "tfae",
        "adjunction_claim": "adjunction",
    }
    out: list[tuple[str, str]] = []
    for key, kind in kind_of.items():
        for q in CONSTRUCTION_TYPE_VOCAB.get(key, {}).get("quantifiers", []):
            out.append((q.lower(), kind))
    return sorted(out, key=lambda t: -len(t[0]))


_DOMAIN_MARKERS = ("in", "of", "over", "on", "with", "from")


def _split_var_domain(tail: str) -> tuple[str, str]:
    """Split the post-quantifier tail into (bound VARIABLE, DOMAIN). The variable is
    the leading noun phrase; the domain is the 'in/of/over ...' restrictor that
    follows it (or, for 'for all <kind> X', the kind is the domain and the trailing
    symbol the variable). A surface split (the same shape as the genus/differentia
    boundary), NOT a meaning decision."""
    t = re.sub(r"\s+", " ", (tail or "")).strip().strip(",")
    if not t:
        return "", ""
    low = t.lower().split()
    # 'for all X in <domain>' / 'for all X with <domain>' -> var before, domain after.
    for i, w in enumerate(low[1:], start=1):
        if w in _DOMAIN_MARKERS:
            var = " ".join(t.split()[:i]).strip(" ,")
            dom = " ".join(t.split()[i + 1 :]).strip(" ,")
            return var, dom
    # 'for all n >= 1' / 'for all n > 0' — the variable then a numeric range domain.
    m = re.match(r"^([A-Za-z][A-Za-z0-9_'^]*)\s*([<>=].*)$", t)
    if m:
        return m.group(1), m.group(2).strip()
    # 'for all finite groups G' — a multi-word KIND then a trailing 1-2 char symbol:
    # the kind is the domain, the symbol the variable.
    toks = t.split()
    if len(toks) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_'^]?", toks[-1]):
        return toks[-1], " ".join(toks[:-1]).strip(" ,")
    # bare variable, no explicit domain (the range is inherited / universe).
    return toks[0], ""


def extract_claim(text: str, *, term: Optional[str] = None) -> Optional[dict]:
    """COMPREHEND a logical/quantified statement INTO the CLAIM REPRESENTATION the
    agent named — {quantifier, kind, variable, domain, predicate} — composing the
    agent's HELD quantifier-recognition + harvester + recursive comprehension. The
    predicate is itself comprehended (harvest_facts -> object->invariant /
    identification rows; extract_definition -> an inner is-a) so the asserted
    content is structured, not raw text. Returns None when no held quantifier
    marker is present (the honest non-claim case — a plain definition or un-read
    prose). The KEY property: the result is INSTANTIABLE (bind `variable` -> the
    predicate becomes a goal the derivation engine evaluates)."""
    if not text:
        return None
    # work over the claim's statement sentence(s); a quantifier can lead any one.
    # The WHOLE text is tried FIRST so a TFAE/adjunction marker is not split across
    # the '; ' / ': ' sentence boundary inside its own list of conditions.
    sents = [re.sub(r"\s+", " ", text).strip()] + (_split_sentences(text) or [text])
    markers = _quantifier_markers()
    for sent in sents:
        norm = re.sub(r"\s+", " ", sent).strip()
        mlow = norm.lower()
        for marker, kind in markers:
            # the marker must sit on a word boundary (start-or-space before, and
            # space/colon/comma/end after) — so 'for' inside 'before' never matches
            # but 'the following are equivalent:' does.
            mi = -1
            for m in re.finditer(re.escape(marker), mlow):
                a, b = m.start(), m.end()
                before_ok = a == 0 or mlow[a - 1] == " "
                after_ok = b >= len(mlow) or mlow[b] in " :,.;"
                if before_ok and after_ok:
                    mi = a
                    break
            if mi < 0:
                continue
            after = norm[mi + len(marker) :].strip(" ,")
            # the PREDICATE is the assertion body — for forall/exists it follows the
            # variable's range (after the first comma OR the 'such that'); for TFAE /
            # adjunction the whole clause is the predicate (a list / a relation).
            variable, domain, predicate = "", "", after
            if kind in ("forall", "exists"):
                # the predicate begins after 'such that' (existential) or the first
                # top-level comma (universal) — the variable+domain precede it.
                cut = None
                st_pos = after.lower().find(" such that ")
                if st_pos >= 0:
                    cut = (st_pos, st_pos + len(" such that "))
                else:
                    cpos = after.find(",")
                    if cpos >= 0:
                        cut = (cpos, cpos + 1)
                if cut is not None:
                    head, predicate = after[: cut[0]], after[cut[1] :].strip()
                    variable, domain = _split_var_domain(head)
                else:
                    variable, domain = _split_var_domain(after)
                    predicate = after
            elif kind == "adjunction":
                # 'F is left adjoint to G' — the relation is the quantifier; the two
                # functors are the related objects (the predicate's content).
                predicate = norm
            elif kind == "tfae":
                predicate = norm  # a list of conditions; the claim is over the list.

            # RECURSIVELY COMPREHEND the predicate's content (the agent's harvester +
            # definition reader) — the asserted structure, not raw text.
            pfacts = harvest_facts(predicate)
            pdef = extract_definition(predicate, term=term)
            pred_struct = {
                "text": re.sub(r"\s+", " ", predicate).strip(),
                "invariants": pfacts["invariants"],
                "identifications": pfacts["identifications"],
            }
            if pdef and pdef.get("term") and pdef.get("genus"):
                pred_struct["isa"] = {"term": pdef["term"], "genus": pdef["genus"]}

            variable = _clean_slot(variable) if variable else ""
            domain = re.sub(r"\s+", " ", domain).strip(" ,") if domain else ""
            # a quantifier with NO readable bound variable AND no predicate content is
            # not a usable claim grounding (honest miss) — TFAE/adjunction excepted
            # (their structure is the list/relation, not a single bound variable).
            if kind in ("forall", "exists") and not variable:
                continue
            return {
                "kind": kind,
                "quantifier": marker,
                "variable": variable or None,
                "domain": domain or None,
                "predicate": pred_struct,
                "statement": norm,
            }
    return None
