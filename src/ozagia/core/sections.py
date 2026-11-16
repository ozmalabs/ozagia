"""Sections of Markdown and TeX documents, and their keys.

A section key is `doc:<rel>#<slug>`: the document's `doc:` key with the
section's slug after `#`. A section runs from its heading line to the line
before the next heading of the same or a higher level (or the end of the
file), so it includes its subsections. Reading the key returns that whole
range.

Slugs follow GitHub's anchors: the title lowercased, characters other than
letters, digits, spaces, `-` and `_` dropped, spaces turned into `-`. A
title whose slug was already used in the file gets `-1`, `-2`, ... in file
order; an empty slug is `section`. A slug depends only on the titles before
it, not on line numbers, so a key survives edits elsewhere in the file.

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


@dataclass(frozen=True)
class DocSection:
    level: int
    title: str
    slug: str
    start: int      # heading line, 1-based
    end: int        # last line of the section, subsections included


def has_sections(rel: str) -> bool:
    return PurePosixPath(rel).suffix.lower() in MD_SUFFIXES | TEX_SUFFIXES


def slugify(title: str) -> str:
    s = _SLUG_DROP.sub("", title.strip().lower()).replace(" ", "-")
    return s or "section"


def section_key(rel: str, slug: str) -> str:
    return f"doc:{rel}#{slug}"


def parse_section_key(key: str) -> tuple[str, str] | None:
    """(rel, slug) of a `doc:<rel>#<slug>` key, else None."""
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


def doc_sections(rel: str, text: str) -> list[DocSection]:
    """The sections of a Markdown or TeX text in file order; [] for any
    other file."""
    suffix = PurePosixPath(rel).suffix.lower()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
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
        out.append(DocSection(level, title, slug, start, max(end, start)))
    return out


def find_section(rel: str, text: str, slug: str) -> DocSection | None:
    for s in doc_sections(rel, text):
        if s.slug == slug:
            return s
    return None


__all__ = ["DocSection", "MD_SUFFIXES", "TEX_SUFFIXES", "doc_sections",
           "find_section", "has_sections", "parse_section_key", "section_key",
           "slugify"]
