"""Source-reading helpers for TeX statement environments."""

from __future__ import annotations
import re

_KINDS = ("theorem", "definition", "lemma", "proposition", "corollary")


def _plainify_latex(tex: str) -> str:
    """Turn an arXiv LaTeX environment body into the readable text the construction
    grammar + harvester read — a MINIMAL world-adapter translation, IDENTICAL in
    spirit to stacks_local._plainify_latex: the LaTeX MATH is RENDERED (not
    stripped) via the agent's LaTeX-notation grammar so the SYMBOL-CARRIED subject
    SURVIVES (the $H_n(X)$ / $\\cong$ / $\\mathbb{Z}$ a harvested invariant fact
    needs). The author \\newcommand macros + multi-line display blocks are the
    named LaTeX long-tail residual (dropped) — the math-core is read."""
    from ozagia.research import latex_render as _lx

    t = tex or ""
    t = re.sub(r"(?<!\\)%.*", " ", t)  # comments
    t = re.sub(r"\\label\{[^}]*\}", " ", t)
    # multi-line DISPLAY environments (align/equation/xymatrix/...) — the named
    # residual (kept simple-inline math below). align*/equation often carry the
    # invariant equation; we preserve a SINGLE-LINE display by unwrapping it first.
    t = (
        re.sub(
            r"\\begin\{(align\*?|equation\*?|gather\*?|displaymath|"
            r"multline\*?|eqnarray\*?)\}(.*?)\\end\{\1\}",
            lambda m: " $ " + m.group(2).replace("\\\\", " ").replace("&", " ") + " $ ",
            t,
            flags=re.DOTALL,
        )
        if False
        else t
    )

    # (the inline path below handles $...$; for displays we convert the body to
    # inline math so a one-line "H_n(X) \\cong \\mathbb{Z}" display still renders.)
    def _disp(m):
        body = m.group(2)
        # only keep a SHORT single-relation display (the invariant-bearing kind);
        # drop long multi-line blocks (the residual).
        body = body.replace("\\\\", " \\\\ ").replace("&", " ")
        if body.count("\\\\") <= 2 and len(body) < 400:
            return " $" + body + "$ "
        return " "

    t = re.sub(
        r"\\begin\{(align\*?|equation\*?|gather\*?|displaymath|"
        r"multline\*?)\}(.*?)\\end\{\1\}",
        _disp,
        t,
        flags=re.DOTALL,
    )
    # remaining nested DISPLAY/list environments -> dropped (residual).
    t = re.sub(
        r"\\begin\{(enumerate|itemize|xymatrix|matrix|array|"
        r"tikzcd|cases|pmatrix|bmatrix)\}.*?\\end\{\1\}",
        " ",
        t,
        flags=re.DOTALL,
    )
    # \ref/\eqref/\cite/\input -> dropped (provenance refs, not meaning).
    t = re.sub(
        r"\\(?:ref|eqref|cref|Cref|autoref|cite[a-z]*|input|index)"
        r"\{[^}]*\}",
        " ",
        t,
    )
    # emphasis/font wrappers -> bare term.
    t = re.sub(r"\{\\(?:it|bf|em|sl|tt)\s+([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\(?:emph|textit|textbf|textrm|text)\{([^{}]*)\}", r"\1", t)
    # PRESERVE the LaTeX MATH: render $...$ / \\[..\\] (the agent READS the notation).
    t = _lx.render(t)
    # remaining bare text-mode control sequences -> dropped (layout / macros).
    t = re.sub(r"\\[a-zA-Z]+\*?", " ", t)
    t = t.replace("{", " ").replace("}", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", " ", t)
    return t.strip()


_ENV_RE = re.compile(
    r"\\begin\{(" + "|".join(_KINDS) + r")\}(\[[^\]]*\])?(.*?)\\end\{\1\}", re.DOTALL
)
