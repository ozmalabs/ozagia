"""Portable source-reading constructions adapted from Ocelot."""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Optional

_SEED = Path(__file__).resolve().parent / "data" / "latex_math_notation.json"

_GRAMMAR: Optional[dict] = None

_ACQUIRED: dict = {"operator_macros": [], "structure_macros": [], "indexed_heads": []}


def load_grammar(path: Path = _SEED) -> dict:
    """Load the LaTeX-notation grammar (graph data), MERGING any acquired macros
    (read at runtime). Cached for the seed; the merge picks up acquisitions. A pure
    data read — no decision."""
    global _GRAMMAR
    if _GRAMMAR is None:
        try:
            _GRAMMAR = json.loads(Path(path).read_text())
        except Exception:
            _GRAMMAR = {}
    if not any(_ACQUIRED.values()):
        return _GRAMMAR
    # merge the acquired macros in (de-dup by latex token).
    g = dict(_GRAMMAR)
    for key, items in _ACQUIRED.items():
        if not items:
            continue
        existing = {m.get("latex") for m in g.get(key, [])}
        g[key] = list(g.get(key, [])) + [m for m in items if m.get("latex") not in existing]
    return g


_RENDER_WORDS = {
    "\\partial": "boundary",
    "\\ker": "kernel",
    "\\im": "image",
    "\\operatorname{im}": "image",
    "\\operatorname{ker}": "kernel",
    "\\dim": "dimension",
    "\\deg": "degree",
    "\\operatorname{coker}": "cokernel",
    "\\operatorname{rank}": "rank",
    "\\otimes": "tensor",
    "\\oplus": "direct sum",
    "\\times": "times",
    "\\cong": "isomorphic to",
    "\\simeq": "isomorphic to",
    "\\sim": "similar to",
    "\\to": "to",
    "\\rightarrow": "to",
    "\\longrightarrow": "to",
    "\\hookrightarrow": "into",
    "\\twoheadrightarrow": "onto",
    "\\sum": "sum",
    "\\prod": "product",
    "\\int": "integral",
    "\\bigoplus": "direct sum",
    "\\bigotimes": "tensor product",
    "\\bigcup": "union",
    "\\bigcap": "intersection",
}


def _flatten_braces(s: str) -> str:
    """Remove LaTeX grouping braces, keeping their content. Iterates so nested
    `{ {Spin} }` flattens. A surface op."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\{([^{}]*)\}", r"\1", s)
    return s


def render(latex: str) -> str:
    """RENDER a LaTeX string (math fragments + `$...$` / `$$...$$` / `\\[..\\]`
    delimited math) to readable prose, the SYMBOLS SURVIVING. The loaders call
    this INSTEAD of dropping the math, so the symbol-carried subject reaches the
    reader ('$M$ is finite if $M$ is finite as an $R$-module' -> 'M is finite if M
    is finite as an R-module'). Driven by the grammar DATA — a mechanical surface
    translation, no meaning decision."""
    if not latex:
        return ""
    g = load_grammar()
    t = latex

    # 1. unwrap the math DELIMITERS, rendering the inner math, keeping the rest.
    #    $$...$$ / \[..\] (display) and $...$ (inline). Process display first.
    def _render_math(m):
        return " " + _render_math_fragment(m.group(1), g) + " "

    t = re.sub(r"\$\$(.*?)\$\$", _render_math, t, flags=re.DOTALL)
    t = re.sub(r"\\\[(.*?)\\\]", _render_math, t, flags=re.DOTALL)
    t = re.sub(r"\\\((.*?)\\\)", _render_math, t, flags=re.DOTALL)
    t = re.sub(r"\$([^$]*)\$", _render_math, t)
    # collapse whitespace; tidy spacing around hyphens ('R -module' -> 'R-module').
    t = re.sub(r"\s+-\s*", "-", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _render_math_fragment(frag: str, g: dict) -> str:
    """Render the INSIDE of a math delimiter (no `$`) to readable tokens. The
    symbols survive: single letters stay, macros render to their word/symbol, sub/
    superscripts kept compact on a head, relations to words."""
    s = frag

    # NAMED-OPERATOR SYMBOLS (graph data: g['named_symbols']) -> their readable
    # prose name FIRST (before the generic font-wrapper unwrap would strip
    # \operatorname{Ker} to the bare token 'Ker'). This is the held reader reading
    # the symbolic shorthand structurally — `\operatorname{Ker}`/`Ker` -> 'kernel',
    # `h_*` -> 'the hurewicz map' — the SAME words the prose atoms/operators cover.
    # Longest-symbol-first so a multi-char symbol wins over its substring. The
    # symbols are matched as literal LaTeX (escaped); a function-name spelling like
    # 'Ker' is bounded so it does not fire inside a longer identifier.
    for ns in sorted(g.get("named_symbols", []), key=lambda e: -len(e.get("symbol", ""))):
        sym = ns.get("symbol", "")
        word = ns.get("word", "")
        if not sym:
            continue
        if re.match(r"^[A-Za-z]", sym):
            # a bare function-name spelling ('Ker'): word-bounded so it does not
            # fire inside a longer token.
            s = re.sub(r"(?<![A-Za-z])" + re.escape(sym) + r"(?![A-Za-z])", " " + word + " ", s)
        else:
            s = s.replace(sym, " " + word + " ")
    # named operators written as \operatorname{im}/{ker}/{coker}/... -> their word
    # FIRST (before the generic font-wrapper unwrap would strip them to 'im').
    s = re.sub(r"\\operatorname\s*\{\s*im\s*\}", " image ", s)
    s = re.sub(r"\\operatorname\s*\{\s*ker\s*\}", " kernel ", s)
    s = re.sub(r"\\operatorname\s*\{\s*coker\s*\}", " cokernel ", s)
    s = re.sub(r"\\operatorname\s*\{\s*rank\s*\}", " rank ", s)

    # blackboard + font wrappers + greek -> their readable token (longest-key
    # first so '\mathbb{Z}' beats a bare '\mathbb').
    for tex, rep in sorted(g.get("blackboard", {}).items(), key=lambda kv: -len(kv[0])):
        s = s.replace(tex, " " + rep + " ")
    # font wrappers: \mathcal{C} -> C, \mathrm{Spin} -> Spin, \text{...} -> text.
    for w in sorted(g.get("font_wrappers", []), key=lambda x: -len(x)):
        s = re.sub(re.escape(w) + r"\s*\{([^{}]*)\}", r" \1 ", s)
    for tex, rep in sorted(g.get("greek", {}).items(), key=lambda kv: -len(kv[0])):
        s = re.sub(re.escape(tex) + r"(?![A-Za-z])", " " + rep + " ", s)

    # set-builder \{ x \mid P \} / \{ x : P \} -> 'the set of x such that P'.
    s = re.sub(
        r"\\\{(.*?)\\(?:mid|;)?(.*?)\\\}",
        lambda m: (
            " the set of "
            + m.group(1).strip()
            + (" such that " + m.group(2).strip() if m.group(2).strip() else "")
            + " "
        ),
        s,
        flags=re.DOTALL,
    )
    s = s.replace("\\{", " { ").replace("\\}", " } ")
    s = s.replace("\\mid", " such that ").replace("\\,", " ").replace("\\;", " ")
    s = s.replace("\\!", "").replace("\\quad", " ").replace("\\qquad", " ")

    # relation macros -> readable words.
    for rel in sorted(g.get("relation_macros", []), key=lambda r: -len(r["latex"])):
        s = re.sub(re.escape(rel["latex"]) + r"(?![A-Za-z])", " " + rel["word"] + " ", s)
    # structure macros that have a clean word: \frac{A}{B} -> 'A over B'.
    s = re.sub(r"\\d?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r" \1 over \2 ", s)
    # named-operator + structure macros -> their readable word.
    for tex, word in sorted(_RENDER_WORDS.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(re.escape(tex) + r"(?![A-Za-z])", " " + word + " ", s)

    # OPERATOR APPLICATION '<op-word> ( arg )' -> '<op-word> of arg' (the prose the
    # construction grammar's prefix-op constructions read: 'kernel of {x}'). A named
    # operator applied by PARENS (f(x)) is the SAME structure as 'f of x' — the held
    # reader renders the application to the prose form so the SAME prefix-op
    # construction fires. Driven by the operator NAMES the grammar holds (operator
    # macros + the named symbols), so it is general, not keyed to 'kernel'.
    op_words = {m.get("name") for m in g.get("operator_macros", []) if m.get("name")}
    op_words |= {
        ns.get("word")
        for ns in g.get("named_symbols", [])
        if ns.get("word") and " " not in ns.get("word", "")
    }
    for w in sorted(op_words, key=lambda x: -len(x)):
        s = re.sub(r"(?<![A-Za-z])" + re.escape(w) + r"\s*\(", w + " of ( ", s)

    # remaining macros: \operatorname{Spec} already handled by font wrappers; a
    # bare leftover control word -> its name (drop the backslash) so it survives
    # as a token rather than vanishing.
    s = re.sub(r"\\([A-Za-z]+)", r" \1 ", s)
    # drop math spacing / alignment leftovers.
    s = s.replace("&", " ").replace("\\\\", " ")
    # keep sub/superscripts compact on their head: 'H _ { n }' -> 'H_n', but a
    # standalone braces group already flattened below.
    s = re.sub(r"\s*\^\s*\{([^{}]*)\}", lambda m: "^" + _flatten_braces(m.group(1)).strip(), s)
    s = re.sub(r"\s*_\s*\{([^{}]*)\}", lambda m: "_" + _flatten_braces(m.group(1)).strip(), s)
    s = re.sub(r"\s*\^\s*([A-Za-z0-9])", r"^\1", s)
    s = re.sub(r"\s*_\s*([A-Za-z0-9])", r"_\1", s)
    s = _flatten_braces(s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()
