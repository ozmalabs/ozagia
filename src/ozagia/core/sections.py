"""Sections of Markdown and TeX documents, and their keys.

The one definition of `doc:<rel>#<section>` keys: reads, outlines and the
read ledger (`ozagia.intent.read`), key edits (`ozagia.edit.key_edit`) and
the region keys of patch-miss replies (`ozagia.intent.patch`) all name and
cut sections here, so any key one of them shows the others take.

A section key is `doc:<rel>#<slug>`: the document's `doc:` key with the
section's slug after `#`. A section runs from its heading line to the line
before the next heading of the same or a higher level (or the end of the
file), so it includes its subsections. Reading the key returns that whole
range; a key edit replaces it.

Lines end at "\\n" (a CRLF line's "\\r" belongs to the line), as reads
count them. `section_offsets` gives a section's character span.

Slugs follow GitHub's anchors: the title lowercased, characters other than
letters, digits, spaces, `-` and `_` dropped, spaces turned into `-`. A
title whose slug was already used in the file gets `-1`, `-2`, ... in file
order; an empty slug is `section`. A slug depends only on the titles before
it, not on line numbers, so a key survives edits elsewhere in the file.
Every section has its own slug, so the slug alone always names it.

Path alias. `resolve_section` also takes `<a>/<b>`: a heading's title slug
after the title slugs (or slugs) of its nearest enclosing headings, e.g.
`cli/usage` for the `## Usage` under `# CLI`, whatever `-n` its own slug
got. It is accepted on input only, never emitted.

Markdown headings are ATX headings (`#` to `######`) outside fenced code
blocks (`ozagia.edit.markdown.headings`). TeX headings are `\\part`,
`\\chapter`, `\\section`, `\\subsection`, `\\subsubsection` and
`\\paragraph` (starred or not, with an optional `[short]` title) at the
start of a line, outside `%` comments; the title is the braced argument with
TeX markup removed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MD_SUFFIXES = frozenset({".md", ".markdown"})
TEX_SUFFIXES = frozenset({".tex", ".ltx", ".latex"})
_TEX_LEVELS = {"part": 0, "chapter": 1, "section": 2, "subsection": 3,
               "subsubsection": 4, "paragraph": 5}
_TEX_RE = re.compile(
    r"^\s*\\(part|chapter|section|subsection|subsubsection|paragraph)\*?"
    r"\s*(?:\[[^\]]*\])?\s*\{(?P<title>.*)")
_SLUG_DROP = re.compile(r"[^\w\- ]", re.UNICODE)


class SectionError(ValueError):
    """A section name that names no section, or several (`matches`)."""

    def __init__(self, message: str, matches: tuple[DocSection, ...] = ()) -> None:
        super().__init__(message)
        self.matches = matches


@dataclass(frozen=True)
class DocSection:
    level: int
    title: str
    slug: str       # unique in the file: the key's fragment
    start: int      # heading line, 1-based
    end: int        # last line of the section, subsections included
    base: str = ""  # the title's slug before `-n` was added
    parent: int = -1  # index of the enclosing section in the list, or -1


def has_sections(rel: str) -> bool:
    return PurePosixPath(rel).suffix.lower() in MD_SUFFIXES | TEX_SUFFIXES


def slugify(title: str) -> str:
    s = _SLUG_DROP.sub("", title.strip().lower()).replace(" ", "-")
    return s or "section"


def section_key(rel: str, slug: str) -> str:
    return f"doc:{rel}#{slug}"


def parse_section_key(key: str) -> tuple[str, str] | None:
    """(rel, fragment) of a `doc:<rel>#<fragment>` key, else None. The
    fragment is a slug or a path alias (`resolve_section`)."""
    if not key.startswith("doc:") or "#" not in key:
        return None
    rel, _, slug = key[4:].rpartition("#")
    if not rel or not slug:
        return None
    return rel, slug


def _brace_arg(rest: str) -> str:
    """The text of a braced argument whose `{` came just before `rest`."""
    depth = 1
    out = []
    for ch in rest:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    text = "".join(out)
    text = re.sub(r"\\[a-zA-Z]+\*?\s*", " ", text)
    text = text.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"\s+", " ", text).strip()


def _tex_headings(lines: list[str]) -> list[tuple[int, str, int]]:
    out = []
    for i, line in enumerate(lines, 1):
        code = re.split(r"(?<!\\)%", line, maxsplit=1)[0]
        m = _TEX_RE.match(code)
        if m:
            out.append((_TEX_LEVELS[m.group(1)], _brace_arg(m.group("title")), i))
    return out


def _md_headings(text: str) -> list[tuple[int, str, int]]:
    from ozagia.edit.markdown import headings
    return [(h.level, h.title, h.start_line) for h in headings(text)]


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def doc_sections(rel: str, text: str) -> list[DocSection]:
    """The sections of a Markdown or TeX text in file order; [] for any
    other file."""
    suffix = PurePosixPath(rel).suffix.lower()
    lines = _lines(text)
    if suffix in MD_SUFFIXES:
        raw = _md_headings(text)
    elif suffix in TEX_SUFFIXES:
        raw = _tex_headings(lines)
    else:
        return []
    total = len(lines)
    taken: set[str] = set()
    counts: dict[str, int] = {}
    out: list[DocSection] = []
    stack: list[int] = []               # indexes of the open sections
    for idx, (level, title, start) in enumerate(raw):
        end = total
        for nlevel, _t, nstart in raw[idx + 1:]:
            if nlevel <= level:
                end = nstart - 1
                break
        base = slugify(title)
        slug, n = base, counts.get(base, 0)
        while slug in taken:
            n += 1
            slug = f"{base}-{n}"
        counts[base] = n
        taken.add(slug)
        while stack and out[stack[-1]].level >= level:
            stack.pop()
        parent = stack[-1] if stack else -1
        out.append(DocSection(level, title, slug, start, max(end, start),
                              base, parent))
        stack.append(idx)
    return out


def find_section(rel: str, text: str, slug: str) -> DocSection | None:
    for s in doc_sections(rel, text):
        if s.slug == slug:
            return s
    return None


def _names(s: DocSection) -> tuple[str, str]:
    return s.slug, s.base


def resolve_section(sections: list[DocSection], fragment: str) -> DocSection:
    """The section a key's fragment names: its slug, or a path alias
    `<a>/<b>` (see the module docstring). SectionError when it names none
    or several."""
    frag = str(fragment or "").strip()
    for s in sections:
        if s.slug == frag:
            return s
    parts = [p for p in frag.strip("/").split("/") if p]
    if not parts:
        raise SectionError("empty section name")
    if len(parts) == 1:
        want = slugify(parts[0])
        hit = tuple(s for s in sections if s.slug == want)
        if hit:
            return hit[0]
        raise SectionError(f"no section {frag!r}")
    want = [p.strip() for p in parts]
    found = []
    for s in sections:
        chain = [s]
        while chain[-1].parent >= 0 and len(chain) < len(want):
            chain.append(sections[chain[-1].parent])
        if len(chain) < len(want):
            continue
        if all(p in _names(c) or slugify(p) in _names(c)
               for p, c in zip(reversed(want), chain)):
            found.append(s)
    if len(found) == 1:
        return found[0]
    if found:
        raise SectionError(f"section {frag!r} is ambiguous", tuple(found))
    raise SectionError(f"no section {frag!r}")


def innermost_section(sections: list[DocSection], start_line: int,
                      end_line: int) -> DocSection | None:
    """The innermost section holding lines `start_line..end_line`, or None
    (before the first heading, or across top-level sections)."""
    best = None
    for s in sections:
        if s.start <= start_line and end_line <= s.end:
            if best is None or s.start >= best.start:
                best = s
    return best


def line_offsets(text: str) -> list[int]:
    """The offset of each line's first character, lines ending at "\\n"."""
    out = [0]
    i = text.find("\n")
    while i >= 0:
        out.append(i + 1)
        i = text.find("\n", i + 1)
    return out


def section_offsets(text: str, section: DocSection) -> tuple[int, int]:
    """The character span of `section` in `text`: its heading line's start
    to the end of its last line, that line's newline included."""
    starts = line_offsets(text)
    s = starts[section.start - 1] if section.start - 1 < len(starts) else len(text)
    e = starts[section.end] if section.end < len(starts) else len(text)
    return s, e


def line_at(text: str, offset: int) -> int:
    """The 1-based line (lines ending at "\\n") holding `offset`."""
    return text.count("\n", 0, max(0, offset)) + 1


__all__ = ["DocSection", "MD_SUFFIXES", "SectionError", "TEX_SUFFIXES",
           "doc_sections", "find_section", "has_sections", "innermost_section",
           "line_at", "line_offsets", "parse_section_key", "resolve_section",
           "section_key", "section_offsets", "slugify"]
