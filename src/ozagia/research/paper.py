"""Source-located, reviewable paper ingestion for the Ocelot research graph.

Parsing records source assertions and unverified interpretation candidates. It
never turns an imported statement into a verified proof or executable model.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from bisect import bisect_right
from pathlib import Path
from typing import Any, Mapping

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError
from ozagia.research.sources import _git_state, _safe_file


_TEX_STATEMENT = re.compile(
    r"\\begin\{(theorem|definition|lemma|proposition|corollary|conjecture|hypothesis)\}"
    r"(\[[^\]]*\])?(.*?)\\end\{\1\}", re.S)
_TEX_PROOF = re.compile(r"\\begin\{proof\}(?:\[[^\]]*\])?(.*?)\\end\{proof\}", re.S)
_LABEL = re.compile(r"\\label\{([^}]+)\}")
_REF = re.compile(r"\\(?:ref|eqref|cref|Cref|autoref)\{([^}]+)\}")
_CITE = re.compile(r"\\cite[a-zA-Z*]*\{([^}]+)\}")
_MATH_MACRO_HEAD = re.compile(
    r"\\(?P<decl>newcommand|renewcommand|providecommand)\s*"
    r"(?:\{\\(?P<braced>[A-Za-z@]+)\}|\\(?P<plain>[A-Za-z@]+))"
    r"(?:\s*\[(?P<arity>\d+)\])?(?:\s*\[[^]]*\])?\s*\{|"
    r"\\(?P<operator>DeclareMathOperator)\*?\s*\{\\(?P<opname>[A-Za-z@]+)\}\s*\{")


def _tex_group_end(text: str, opening: int) -> int | None:
    depth = 0
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{" and not escaped:
            depth += 1
        elif char == "}" and not escaped:
            depth -= 1
            if depth == 0:
                return index + 1
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return None


_MATH_SCOPE_ENVS = frozenset({"theorem", "definition", "lemma", "remark",
                              "proposition", "corollary", "conjecture", "hypothesis"})


def _math_scope_spans(text: str) -> list[tuple[str, int, int]]:
    """Return balanced spans for recognized statement environments."""
    token_re = re.compile(r"\\(?P<op>begin|end)\{(?P<name>[^}]+)\}")
    stack: list[tuple[str, int]] = []
    spans: list[tuple[str, int, int]] = []
    for match in token_re.finditer(text):
        name = match.group("name")
        if name not in _MATH_SCOPE_ENVS:
            continue
        if match.group("op") == "begin":
            stack.append((name, match.start()))
        elif stack and stack[-1][0] == name:
            _, start = stack.pop()
            spans.append((name, start, match.end()))
    return spans
_DISPLAY_MATH = re.compile(
    r"\\begin\{(equation\*?|align\*?|gather\*?|displaymath|multline\*?|eqnarray\*?)\}"
    r"(.*?)\\end\{\1\}|\\\[(.*?)\\\]|(?<!\\)\$\$(.*?)\$\$", re.S)
_CONTEXT_ENV = re.compile(r"\\begin\{(remark|theorem|definition|lemma|proposition|corollary|proof)\}"
                          r"(?:\[[^\]]*\])?(.*?)\\end\{\1\}", re.S)
_TYPE_MAP = {"theorem": "theorem", "definition": "definition",
             "lemma": "theorem", "proposition": "theorem",
             "corollary": "theorem", "conjecture": "hypothesis",
             "hypothesis": "hypothesis"}


def _root_file(graph: Any, root_id: str, relative_path: str) -> tuple[dict, Path]:
    state = graph.state() if hasattr(graph, "state") else graph
    roots = state.get("roots", []) if isinstance(state, Mapping) else []
    root = next((dict(r) for r in roots if r.get("root_id") == root_id), None)
    if root is None:
        raise ResearchError(f"unknown configured source root: {root_id}")
    files = root.get("files", {})
    if isinstance(files, Mapping):
        selected = relative_path in files
    else:
        selected = relative_path in files if isinstance(files, list) else False
    if not selected:
        raise ResearchError("paper file is not explicitly selected in this root manifest")
    try:
        path = _safe_file(Path(root["path"]), relative_path)
    except (OSError, ValueError) as exc:
        raise ResearchError(f"selected paper file is unavailable or unsafe: {exc}") from exc
    return root, path


def _bibliography_for_paper(
    graph: Any,
    *,
    root_id: str,
    path: str,
    bibliography_path: str | None,
    text: str,
    suffix: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Parse an explicit bibliography or the paper's embedded one.

    Entries are unverified metadata; the caller records them as References.
    """
    entries: list[dict[str, Any]] = []
    report = None
    if suffix == ".tex":
        entries = refs.parse_tex_bibliography(text)
        if bibliography_path:
            _, bibliography_file = _root_file(graph, root_id, bibliography_path)
            try:
                raw = bibliography_file.read_bytes()
                bibliography_text = raw.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise ResearchError(f"cannot read selected bibliography: {exc}") from exc
            entries = refs.parse_reference_file(bibliography_path, bibliography_text)
            report = {
                "path": bibliography_path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "format": Path(bibliography_path).suffix.lower().lstrip("."),
                "count": len(entries),
                "metadata_status": "unverified",
            }
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        key = str(entry.get("citation_key") or "").casefold()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(entry)
    return deduped, report


def _interpret(text: str) -> dict[str, Any]:
    """Run held construction grammar readers; return candidates, never verdicts."""
    from ozagia.research import construction as cg
    candidates: dict[str, Any] = {}
    try:
        definition = cg.extract_definition(text)
        if definition:
            candidates["definition"] = definition
    except Exception as exc:
        candidates["definition_error"] = f"{type(exc).__name__}: {exc}"
    try:
        claim = cg.extract_claim(text)
        if claim:
            candidates["quantified_claim"] = claim
    except Exception as exc:
        candidates["claim_error"] = f"{type(exc).__name__}: {exc}"
    return {"status": "unverified_candidate" if candidates else "unresolved",
            "verified": False, "candidates": candidates,
            "residual": [] if candidates else ["construction grammar found no supported interpretation"]}


def _latexml_document_report(xml_bytes: bytes, *, source_sha256: str,
                             document_path: str) -> dict[str, Any]:
    """Summarize optional LaTeXML XML without replacing source-derived records.

    The converter output has no reliable line mapping back to the input TeX,
    so it is deliberately limited to supplemental structure and provenance.
    """
    digest = hashlib.sha256(xml_bytes).hexdigest()
    if len(xml_bytes) > 20_000_000:
        raise ResearchError("LaTeXML document exceeds the 20 MB parsing limit")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", xml_bytes, re.I):
        raise ResearchError("LaTeXML document with DTD/entity declarations is not accepted")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ResearchError(f"invalid LaTeXML XML document: {exc}") from exc
    local = lambda tag: tag.rsplit("}", 1)[-1].lower()
    counts: dict[str, int] = {}
    labels: list[str] = []
    equation_candidates: list[dict[str, Any]] = []
    equation_limit = 32
    candidate_chars = 4000
    title = ""
    for element in root.iter():
        kind = local(element.tag)
        if kind in {"theorem", "proof", "equation", "figure", "table"}:
            counts[kind] = counts.get(kind, 0) + 1
        label = element.attrib.get("label")
        if label:
            labels.append(label)
        if not title and kind == "title":
            title = " ".join("".join(element.itertext()).split())
        if kind == "equation" and len(equation_candidates) < equation_limit:
            math_elements = [child for child in element.iter()
                             if child is not element and local(child.tag) in {"math", "mathml"}]
            candidate_element = math_elements[0] if math_elements else element
            serialized = ET.tostring(candidate_element, encoding="unicode")
            truncated = len(serialized) > candidate_chars
            serialized = serialized[:candidate_chars]
            text = " ".join("".join(candidate_element.itertext()).split())
            equation_label = next((child.attrib["label"] for child in element.iter()
                                   if child.attrib.get("label")), None)
            ordinal = counts.get("equation", 0)
            equation_candidates.append({
                "candidate_id": "latexml-equation-" + hashlib.sha256(
                    f"{digest}\\0{ordinal}\\0{equation_label or ''}".encode()).hexdigest()[:20],
                "ordinal": ordinal,
                "label": equation_label,
                "text": text[:candidate_chars],
                "serialized_xml": serialized,
                "truncated": truncated or len(text) > candidate_chars,
                "artifact_provenance": {
                    "format": "latexml_xml", "document_path": document_path,
                    "document_sha256": digest, "source_sha256": source_sha256,
                    "source_correspondence_verified": False,
                    "source_locator": None,
                },
                "interpretation_status": "unverified_converter_candidate",
                "verified": False,
            })
    return {"format": "latexml_xml", "document_path": document_path,
            "document_sha256": digest, "source_sha256": source_sha256,
            "title": title, "structure_counts": counts,
            "equation_candidates": equation_candidates,
            "equation_candidates_truncated": counts.get("equation", 0) > equation_limit,
            "equation_candidate_limit": equation_limit,
            "labels": labels, "source_locators_available": False,
            "source_correspondence_verified": False,
            "converter_version": None,
            "use_for_claim_ingestion": False}


def _tex_records(text: str) -> list[dict[str, Any]]:
    # Reuse arxiv_local's environment family and LaTeX normalization. This
    # adapter adds exact file/line locators and retains unresolved refs.
    from ozagia.research import tex_reader as ax
    env_re = ax._ENV_RE
    plainify = ax._plainify_latex
    records = []
    lines = text.splitlines(keepends=True)
    for index, match in enumerate(env_re.finditer(text), start=1):
        start = text.count("\n", 0, match.start()) + 1
        end = text.count("\n", 0, match.end()) + 1
        raw = match.group(3)
        label_match = _LABEL.search(raw)
        label = label_match.group(1) if label_match else ""
        label_line = (text.count("\n", 0, match.start(3) + label_match.start()) + 1
                      if label_match else start)
        following = _TEX_PROOF.search(text, match.end())
        proof = None
        proof_refs: list[str] = []
        proof_locator = None
        if following and not text[match.end():following.start()].strip():
            proof = plainify(following.group(1))
            proof_refs = [ref.strip() for item in _REF.findall(following.group(1))
                          for ref in item.split(",") if ref.strip()]
            proof_locator = {"start_line": text.count("\n", 0, following.start()) + 1,
                             "end_line": text.count("\n", 0, following.end()) + 1}
        body = plainify(raw)
        if not body:
            continue
        records.append({"kind": match.group(1), "label": label,
                        "wording": body, "raw_excerpt": "".join(lines[start - 1:end]).strip(),
                        "locator": {"start_line": start, "end_line": end,
                                    "label_line": label_line},
                        "proof": proof, "proof_locator": proof_locator,
                        "proof_references": proof_refs,
                        "references": [ref for item in _REF.findall(raw)
                                       for ref in item.split(",") if ref.strip()],
                        "citations": [ref for item in _CITE.findall(raw)
                                      for ref in item.split(",") if ref.strip()]})
    return records


def _markdown_records(text: str) -> list[dict[str, Any]]:
    from ozagia.research.markdown_reader import _extract_formal_objects, _HEADER_FORMAL_PATTERNS
    lines = text.splitlines()
    starts = [(i, len(m.group(1)), m.group(2).strip())
              for i, line in enumerate(lines)
              if (m := re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line))]
    records = []
    for n, (at, level, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        content = "\n".join(lines[at + 1:end])
        objects = _extract_formal_objects(content)
        header_obj = None
        for pattern, kind in _HEADER_FORMAL_PATTERNS:
            m = re.search(pattern, title)
            if m:
                header_obj = {"node_type": kind,
                              "label": str(m.group(1)) if m.lastindex else "",
                              "title": str(m.group(2)) if m.lastindex and m.lastindex >= 2 else "",
                              "text": content[:2500], "from_header": True}
                break
        if header_obj:
            objects.append(header_obj)
        for obj in objects:
            raw = obj.get("text", "")
            if obj.get("from_header") and not raw:
                raw = title
            # Locator to the section with a best-effort exact label/text line.
            needle = obj.get("label") or obj.get("title")
            relative = next((i for i, line in enumerate(lines[at:end], start=at + 1)
                             if needle and needle.casefold() in line.casefold()), at + 1)
            records.append({"kind": obj["node_type"].removeprefix("Paper").lower(),
                            "label": obj.get("label", ""),
                            "wording": raw.strip() or title,
                            "raw_excerpt": raw.strip()[:2500],
                            "section": title,
                            "locator": {"start_line": relative, "end_line": min(end, relative + max(0, raw.count("\n")))},
                            "proof": None, "proof_locator": None,
                            "proof_references": [], "references": [], "citations": []})
    return records


def _mask_comments_and_fences(text: str, *, markdown: bool) -> str:
    """Mask comments/fenced code in place so regex offsets still match source."""
    chars = list(text)
    offset = 0
    in_fence: tuple[str, int] | None = None
    in_html_comment = False
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if markdown:
            fence_match = re.match(r"^\s*(`{3,}|~{3,})", content)
            if in_fence:
                marker, width = in_fence
                if fence_match and fence_match.group(1)[0] == marker and len(fence_match.group(1)) >= width:
                    for i in range(offset, offset + len(line)):
                        if chars[i] not in "\r\n":
                            chars[i] = " "
                    in_fence = None
                else:
                    for i in range(offset, offset + len(line)):
                        if chars[i] not in "\r\n":
                            chars[i] = " "
                offset += len(line)
                continue
            if fence_match:
                in_fence = (fence_match.group(1)[0], len(fence_match.group(1)))
                for i in range(offset, offset + len(line)):
                    if chars[i] not in "\r\n":
                        chars[i] = " "
                offset += len(line)
                continue
            if in_html_comment or "<!--" in content:
                j = offset
                while j < offset + len(line):
                    if in_html_comment:
                        close = text.find("-->", j, offset + len(line))
                        end = offset + len(line) if close < 0 else close + 3
                        for i in range(j, end):
                            if chars[i] not in "\r\n":
                                chars[i] = " "
                        in_html_comment = close < 0
                        j = end
                    else:
                        open_at = text.find("<!--", j, offset + len(line))
                        if open_at < 0:
                            break
                        close = text.find("-->", open_at + 4, offset + len(line))
                        end = offset + len(line) if close < 0 else close + 3
                        for i in range(open_at, end):
                            if chars[i] not in "\r\n":
                                chars[i] = " "
                        in_html_comment = close < 0
                        j = end
            # Markdown inline code spans suppress math markers. Match an exact
            # run of backticks on this physical line; unmatched runs are left
            # untouched rather than masking arbitrary prose.
            i = 0
            while i < len(content):
                if content[i] != "`":
                    i += 1
                    continue
                run_end = i + 1
                while run_end < len(content) and content[run_end] == "`":
                    run_end += 1
                width = run_end - i
                closing = re.search(r"(?<!`)`{" + str(width) + r"}(?!`)", content[run_end:])
                if closing is None:
                    i = run_end
                    continue
                close_end = run_end + closing.end()
                for index in range(offset + i, offset + close_end):
                    if chars[index] not in "\r\n":
                        chars[index] = " "
                i = close_end
        else:
            # A TeX percent sign starts a comment iff preceded by an even
            # number of backslashes.
            for local, char in enumerate(content):
                if char != "%":
                    continue
                backslashes = 0
                pos = local - 1
                while pos >= 0 and content[pos] == "\\":
                    backslashes += 1
                    pos -= 1
                if backslashes % 2 == 0:
                    for i in range(offset + local, offset + len(line)):
                        if chars[i] not in "\r\n":
                            chars[i] = " "
                    break
        offset += len(line)
    return "".join(chars)


def _inline_math_spans(text: str, *, scan_cap: int) -> tuple[list[tuple[int, int, int, int, str, str]], list[dict[str, Any]]]:
    """Pair strict one-line inline delimiters, respecting odd/even backslashes."""
    candidates: list[tuple[int, int, int, int, str, str]] = []
    unmatched: list[dict[str, Any]] = []
    line_starts = [0]
    line_starts.extend(i + 1 for i, char in enumerate(text) if char == "\n")
    offset = 0
    for line_no, line_with_nl in enumerate(text.splitlines(keepends=True), start=1):
        line = line_with_nl.rstrip("\r\n")
        dollars: list[int] = []
        parens: list[tuple[str, int]] = []
        i = 0
        while i < len(line):
            if line[i] == "$" and (i == 0 or line[i - 1] != "$") and (i + 1 >= len(line) or line[i + 1] != "$"):
                slash_n = 0
                j = i - 1
                while j >= 0 and line[j] == "\\":
                    slash_n += 1
                    j -= 1
                if slash_n % 2 == 0:
                    dollars.append(i)
                i += 1
                continue
            if line.startswith(r"\(", i) or line.startswith(r"\)", i):
                slash_n = 0
                j = i - 1
                while j >= 0 and line[j] == "\\":
                    slash_n += 1
                    j -= 1
                if slash_n % 2 == 0:
                    parens.append((line[i + 1], i))
                i += 2
                continue
            i += 1
        if len(dollars) % 2:
            unmatched.append({"delimiter": "$", "line": line_no,
                              "reason": "unpaired single-dollar delimiter"})
        for left, right in zip(dollars[::2], dollars[1::2]):
            candidates.append((offset + left, offset + right + 1,
                               offset + left + 1, offset + right,
                               "inline_dollar", "inline"))
        stack: list[int] = []
        for kind, pos in parens:
            if kind == "(":
                stack.append(pos)
            elif stack:
                left = stack.pop()
                candidates.append((offset + left, offset + pos + 2,
                                   offset + left + 2, offset + pos,
                                   "inline_parenthesis", "inline"))
            else:
                unmatched.append({"delimiter": r"\(...\)", "line": line_no,
                                  "reason": "unpaired inline-parenthesis delimiter"})
        for _ in stack:
            unmatched.append({"delimiter": r"\(...\)", "line": line_no,
                              "reason": "unpaired inline-parenthesis delimiter"})
        offset += len(line_with_nl)
        if len(candidates) >= scan_cap:
            # The source scan is bounded for memory; enough candidates have
            # been found to serve the requested page plus a lookahead.
            break
    candidates.sort(key=lambda item: (item[0], item[1]))
    nonoverlap = []
    end = -1
    for candidate in candidates:
        if candidate[0] < end:
            unmatched.append({"delimiter": "inline math",
                              "line": bisect_right(line_starts, candidate[0]),
                              "reason": "overlapping inline delimiters; candidate omitted as ambiguous"})
            continue
        nonoverlap.append(candidate)
        end = candidate[1]
        if len(nonoverlap) >= scan_cap:
            break
    return nonoverlap, unmatched


def _formula_candidates_report(text: str, *, markdown: bool = False,
                               max_candidates: int = 256,
                               offset: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Find displayed and inline math as located candidates, never Claims."""
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool) or max_candidates < 0:
        raise ValueError("max_candidates must be a nonnegative integer")
    if max_candidates > 5000:
        raise ValueError("max_candidates must not exceed 5000 per page")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10000:
        raise ValueError("offset must be an integer between 0 and 10000")
    scan_cap = offset + max_candidates + 1
    lines = text.splitlines(keepends=True)
    masked = _mask_comments_and_fences(text, markdown=markdown)
    spans: list[tuple[int, int, int, int, str, str]] = []
    contexts = [] if markdown else [
        (m.start(), m.end(), m.group(1).lower()) for m in _CONTEXT_ENV.finditer(text)]
    display_spans = []
    for match in _DISPLAY_MATH.finditer(masked):
        if match.group(1):
            formula_start, formula_end = match.start(2), match.end(2)
            display = "environment:" + match.group(1)
        elif match.group(3) is not None:
            formula_start, formula_end = match.start(3), match.end(3)
            display = "bracket"
        else:
            formula_start, formula_end = match.start(4), match.end(4)
            display = "dollars"
        start_char, end_char = match.start(), match.end()
        spans.append((start_char, end_char, formula_start, formula_end, display, "display"))
        display_spans.append((start_char, end_char))
        if len(spans) >= scan_cap:
            break
    display_starts = [span[0] for span in display_spans]
    inline_spans, unmatched = _inline_math_spans(masked, scan_cap=scan_cap)
    inline_matches = 0
    for start_char, end_char, formula_start, formula_end, display, presentation in inline_spans:
        span_index = bisect_right(display_starts, start_char) - 1
        if (span_index >= 0 and start_char < display_spans[span_index][1]
                and end_char > display_spans[span_index][0]):
            continue
        spans.append((start_char, end_char, formula_start, formula_end, display, presentation))
        inline_matches += 1
        if inline_matches >= scan_cap:
            break
    spans.sort(key=lambda item: (item[0], item[1]))
    has_more = len(spans) > offset + max_candidates
    page = spans[offset:offset + max_candidates]
    records = []
    line_starts = [0]
    line_starts.extend(i + 1 for i, ch in enumerate(text) if ch == "\n")
    heading_starts = []
    if markdown:
        heading_offset = 0
        for line in text.splitlines(keepends=True):
            m = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line.rstrip("\r\n"))
            if m:
                heading_starts.append((heading_offset, m.group(1).strip()))
            heading_offset += len(line)
    for start_char, end_char, formula_start, formula_end, display, presentation in page:
        formula = text[formula_start:formula_end]
        start_line_index = bisect_right(line_starts, start_char) - 1
        end_line_index = bisect_right(line_starts, end_char) - 1
        start_line = start_line_index + 1
        end_line = end_line_index + 1
        line_start = line_starts[start_line_index]
        end_line_start = line_starts[end_line_index]
        containing = next((kind for left, right, kind in contexts
                           if left <= start_char and end_char <= right), None)
        if markdown:
            heading_pos = bisect_right([h[0] for h in heading_starts], start_char) - 1
            heading = heading_starts[heading_pos][1] if heading_pos >= 0 else None
            containing = "remark" if heading and re.search(r"\bremark\b", heading, re.I) else "prose"
        context_start = max(1, start_line - 2)
        context_end = min(len(lines), end_line + 2)
        context_text = "".join(lines[context_start - 1:context_end]).strip()
        ambiguity = []
        # Do not tokenize adjacent letters into multiplication or a single
        # variable: report the ambiguity for downstream review.
        if re.search(r"(?<![\\A-Za-z])[A-Za-z]{2,}(?![A-Za-z])", formula):
            ambiguity.append("adjacent letters may denote implicit multiplication or a multi-letter symbol")
        records.append({
            "kind": "displayed_formula_candidate", "formula": formula.strip(),
            "raw_excerpt": text[start_char:end_char], "display": display,
            "presentation": presentation,
            "context_kind": containing or "prose", "context_excerpt": context_text,
            "locator": {"start_line": start_line, "start_column": start_char - line_start + 1,
                        "end_line": end_line, "end_column": end_char - end_line_start + 1,
                        "start_offset": start_char, "end_offset": end_char,
                        "offset_unit": "unicode_codepoints"},
            "interpretation_status": "unparsed_candidate",
            "is_claim": False, "ambiguity": ambiguity,
        })
    report = {"status": "more_available" if has_more else "complete",
              "max_candidates": max_candidates, "offset": offset,
              "candidate_count": len(records), "next_offset": offset + len(records),
              "matches_observed_lower_bound": max(offset + len(spans), offset + len(records)),
              "has_more": has_more, "truncated": has_more,
              "unmatched_delimiters": unmatched,
              "limitations": ["Inline delimiters are paired within one physical line."]}
    if has_more:
        report["reason"] = "formula candidate page is bounded; request the next offset to continue extraction"
    return records, report


def _formula_candidates(text: str, *, markdown: bool = False,
                        max_candidates: int = 256, offset: int = 0) -> list[dict[str, Any]]:
    """Compatibility helper returning candidates without the extraction report."""
    return _formula_candidates_report(text, markdown=markdown,
                                      max_candidates=max_candidates,
                                      offset=offset)[0]


def _math_context(text: str, *, root_id: str, path: str,
                  sha256: str, limit: int = 256) -> dict[str, Any]:
    """Extract explicitly declared TeX notation and definition environments.

    This records author-supplied source spans only. A macro expansion is not
    treated as a semantic interpretation (e.g. ``\\mathbb{N}`` does not imply
    a convention about whether zero is included).
    """
    masked = _mask_comments_and_fences(text, markdown=False)
    chars = list(masked)
    # TeX code-like environments contain literal examples, not declarations
    # in the mathematical source language.
    for code_env in ("verbatim", "Verbatim", "lstlisting", "minted", "comment"):
        for match in re.finditer(r"\\begin\{" + re.escape(code_env) + r"\}.*?\\end\{" + re.escape(code_env) + r"\}", masked, re.S):
            for index in range(match.start(), match.end()):
                if chars[index] not in "\r\n":
                    chars[index] = " "
    masked = "".join(chars)
    starts = [0]
    starts.extend(i + 1 for i, char in enumerate(text) if char == "\n")
    records: list[dict[str, Any]] = []
    for match in _MATH_MACRO_HEAD.finditer(masked):
        end = _tex_group_end(masked, match.end() - 1)
        if end is None:
            continue
        declaration = match.group("decl") or match.group("operator")
        name = match.group("braced") or match.group("plain") or match.group("opname")
        expansion = text[match.end():end - 1]
        line = bisect_right(starts, match.start())
        records.append({"kind": "symbol", "name": "\\" + name,
                        "expansion": expansion,
                        "arity": int(match.group("arity") or 0),
                        "declaration": declaration, "start": match.start(),
                        "end": end, "line": line,
                        "raw_excerpt": text[match.start():end]})
    for match in re.finditer(r"\\begin\{definition\}(.*?)\\end\{definition\}", masked, re.S):
        line = bisect_right(starts, match.start())
        records.append({"kind": "definition", "name": None,
                        "expansion": None, "arity": None,
                        "declaration": "definition_environment",
                        "start": match.start(), "end": match.end(),
                        "line": line, "raw_excerpt": text[match.start():match.end()]})
    # Capture only explicit domain membership/assignment statements. These are
    # scoped source facts, not a claim about a notation's global convention.
    domain_pattern = re.compile(
        r"(?P<math>\$[^$\n]+\$|\\\([^\n]*?\\\))")
    membership = re.compile(r"^\s*([A-Za-z](?:\s*,\s*[A-Za-z])*)\s*\\in\s*(.+?)\s*$")
    prose_domain = re.compile(
        r"\b(?:let|take|choose)\s+\$?([A-Za-z](?:\s*,\s*[A-Za-z])*)\$?\s+"
        r"(?:be|as)\s+(?:an?\s+)?(real numbers?|integers?|naturals?|positive integers?)\b",
        re.I)
    for match in domain_pattern.finditer(masked):
        formula = match.group("math").strip("$\\() ")
        parsed = membership.match(formula)
        if not parsed:
            continue
        line = bisect_right(starts, match.start())
        records.append({"kind": "definition", "name": parsed.group(1),
                        "expansion": formula, "arity": None,
                        "declaration": "explicit_domain_membership",
                        "start": match.start(), "end": match.end(),
                        "line": line, "raw_excerpt": text[match.start():match.end()]})
    for match in prose_domain.finditer(masked):
        line = bisect_right(starts, match.start())
        records.append({"kind": "definition", "name": match.group(1),
                        "expansion": match.group(2), "arity": None,
                        "declaration": "explicit_prose_domain",
                        "start": match.start(), "end": match.end(),
                        "line": line, "raw_excerpt": text[match.start():match.end()]})
    scope_spans = _math_scope_spans(masked)
    records.sort(key=lambda item: item["start"])
    total = len(records)
    records = records[:max(0, limit)]
    for ordinal, item in enumerate(records, start=1):
        declaration_start, declaration_end = item["start"], item["end"]
        containing = [(env, start, end) for env, start, end in scope_spans
                      if start <= declaration_start and declaration_end <= end]
        if containing:
            env, scope_start, scope_end = min(containing, key=lambda span: span[2] - span[1])
            scope_kind = "statement_environment"
        elif item["declaration"].startswith("explicit_"):
            env, scope_start, scope_end = "exact_declaration", declaration_start, declaration_end
            scope_kind = "exact_declaration"
        else:
            env, scope_start, scope_end = "paper", 0, len(text)
            scope_kind = "paper"
        item["scope"] = {"kind": scope_kind, "environment": env,
                         "start_offset": scope_start, "end_offset": scope_end,
                         "start_line": bisect_right(starts, scope_start),
                         "end_line": bisect_right(starts, max(scope_start, scope_end - 1))}
        item["candidate_id"] = "math-context-" + hashlib.sha256(
            f"{root_id}\\0{path}\\0{sha256}\\0{item['start']}\\0{item['end']}\\0{item['kind']}".encode()).hexdigest()[:20]
        item["locator"] = {"start_line": item.pop("line"),
                           "end_line": bisect_right(starts, max(item["start"], item["end"] - 1)),
                           "start_offset": item["start"], "end_offset": item["end"]}
        item["ordinal"] = ordinal
        item["interpretation_status"] = "source_declared_uninterpreted"
        item["semantics_verified"] = False
    return {"records": records, "total": total, "truncated": total > len(records),
            "limit": limit,
            "limitations": ["Only explicit macro declarations and definition environments are captured.",
                            "Macro expansions are verbatim and are not assigned mathematical meaning."]}


def preview_paper(graph: Any, *, root_id: str, path: str,
                  max_formula_candidates: int = 256,
                  formula_offset: int = 0,
                  latexml_path: str | None = None,
                  bibliography_path: str | None = None) -> dict[str, Any]:
    """Parse a manifest-selected Markdown or LaTeX paper without graph writes."""
    root, source_path = _root_file(graph, root_id, path)
    try:
        raw_bytes = source_path.read_bytes()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ResearchError(f"cannot read selected paper source: {exc}") from exc
    suffix = source_path.suffix.lower()
    if suffix not in {".md", ".markdown", ".tex"}:
        raise ResearchError("paper source must be .tex, .md, or .markdown")
    records = _tex_records(text) if suffix == ".tex" else _markdown_records(text)
    formula_candidates, formula_extraction = _formula_candidates_report(
        text, markdown=suffix != ".tex", max_candidates=max_formula_candidates,
        offset=formula_offset)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    math_context = (_math_context(text, root_id=root_id, path=path, sha256=digest)
                    if suffix == ".tex" else {"records": [], "total": 0,
                    "truncated": False, "limit": 256, "limitations": []})
    latexml_report = None
    if latexml_path is not None:
        if suffix != ".tex":
            raise ResearchError("LaTeXML documents can only supplement a .tex source")
        _, converted_path = _root_file(graph, root_id, latexml_path)
        try:
            latexml_report = _latexml_document_report(
                converted_path.read_bytes(), source_sha256=digest,
                document_path=latexml_path)
        except OSError as exc:
            raise ResearchError(f"cannot read selected LaTeXML document: {exc}") from exc
    bibliography, bibliography_report = _bibliography_for_paper(
        graph,
        root_id=root_id,
        path=path,
        bibliography_path=bibliography_path,
        text=text,
        suffix=suffix,
    )
    bibliography_keys = {
        str(key).casefold()
        for entry in bibliography
        for key in [entry.get("citation_key"), *(entry.get("citation_keys") or [])]
        if key
    }
    unresolved_citations = [
        {"citation_key": key, "claim_label": record.get("label"),
         "locator": record.get("locator")}
        for record in records
        for key in record.get("citations", [])
        if key.casefold() not in bibliography_keys
    ]
    git = _git_state(Path(root["path"]), [path])
    for record in records:
        record["interpretation"] = _interpret(record["wording"])
    for formula in formula_candidates:
        span = formula["locator"]
        formula["containing_claim_indexes"] = [
            index for index, record in enumerate(records)
            if
            record["locator"]["start_line"] <= span["start_line"] <= record["locator"]["end_line"]]
        formula["containing_claim_refs"] = [
            {"source_record_index": index,
             "label": records[index].get("label") or None}
            for index in formula["containing_claim_indexes"]]
        stable = f"{root_id}\0{path}\0{digest}\0{span['start_offset']}\0{span['end_offset']}\0{formula['formula']}"
        formula["candidate_id"] = "formula-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
    unresolved = [{"kind": "no_formal_statement_extracted", "path": path,
                   "reason": "selected source contained no supported formal statement"}]
    if records:
        unresolved = []
    if formula_extraction["truncated"]:
        unresolved.append({"kind": "formula_candidate_limit",
                           "reason": formula_extraction["reason"],
                           "max_candidates": max_formula_candidates})
    for delimiter_issue in formula_extraction["unmatched_delimiters"]:
        unresolved.append({"kind": "unmatched_inline_math_delimiter", **delimiter_issue})
    known_labels = {r["label"] for r in records if r.get("label")}
    unresolved_refs = []
    for record in records:
        for ref in record.get("proof_references", []) + record.get("references", []):
            if ref not in known_labels:
                unresolved_refs.append({"reference": ref, "claim_label": record.get("label"),
                                        "locator": record.get("proof_locator") or record["locator"]})
    state = graph.state() if hasattr(graph, "state") else graph
    page_done = [n for n in state.get("nodes", []) if n.get("type") == "Decision" and
                   n.get("paper_ingest_page_complete") is True and
                   n.get("root_id") == root_id and n.get("path") == path and
                   n.get("sha256") == digest]
    duplicate = any(n.get("formula_offset") == formula_offset and
                    n.get("formula_limit") == max_formula_candidates for n in page_done)
    has_any_page = bool(page_done)
    partial = any(n.get("type") == "Source" and n.get("paper_import") is True and
                  isinstance(n.get("locator"), Mapping) and
                  n["locator"].get("root_id") == root_id and
                  n["locator"].get("path") == path and
                  n["locator"].get("sha256") == digest for n in state.get("nodes", [])) and not has_any_page
    return {"root_id": root_id, "repository": root.get("repository", root_id),
            "path": path, "sha256": digest, "revision": git.get("revision"),
            "dirty": path in git.get("dirty_files", []),
            "format": suffix[1:], "records": records,
            "formula_candidates": formula_candidates,
            "formula_extraction": formula_extraction,
            "mathematical_context": math_context,
            "unresolved_extractions": unresolved,
            "unresolved_references": unresolved_refs,
            "unresolved_citations": unresolved_citations,
            "bibliography": bibliography,
            "bibliography_report": bibliography_report,
            "latexml_document": latexml_report,
            "formula_offset": formula_offset,
            "duplicate": duplicate, "partial_import": partial}


def ingest_paper(graph: Any, *, root_id: str, path: str,
                 max_formula_candidates: int = 256,
                 formula_offset: int = 0,
                 latexml_path: str | None = None,
                 bibliography_path: str | None = None) -> dict[str, Any]:
    """Preview then record Source and parsed Claim nodes in an existing graph.

    Repeated identical imports are reported without graph mutation. Claims have
    status ``unresolved``; grammar output is stored only as an unverified
    interpretation candidate. Proof text and unresolved references remain data.
    """
    preview = preview_paper(graph, root_id=root_id, path=path,
                            max_formula_candidates=max_formula_candidates,
                            formula_offset=formula_offset,
                            latexml_path=latexml_path,
                            bibliography_path=bibliography_path)
    if preview["duplicate"] or preview["partial_import"]:
        existing_page = next((n for n in (graph.state() if hasattr(graph, "state") else graph).get("nodes", [])
                              if n.get("type") == "Decision"
                              and n.get("paper_ingest_page_complete") is True
                              and n.get("root_id") == root_id and n.get("path") == path
                              and n.get("sha256") == preview["sha256"]
                              and n.get("formula_offset") == formula_offset), None)
        return {"ingested": False, "duplicate": preview["duplicate"], "preview": preview,
                "partial_import": preview["partial_import"],
                "source": None, "claims": [], "formula_evidence": [],
                "mathematical_context": [], "references": [],
                "frame_id": existing_page.get("frame_id") if existing_page else None}
    root = next(r for r in (graph.state() if hasattr(graph, "state") else graph).get("roots", [])
                if r.get("root_id") == root_id)
    state = graph.state() if hasattr(graph, "state") else graph
    source = next((n for n in state.get("nodes", [])
                   if n.get("type") == "Source" and n.get("paper_import") is True
                   and isinstance(n.get("locator"), Mapping)
                   and n["locator"].get("root_id") == root_id
                   and n["locator"].get("path") == path
                   and n["locator"].get("sha256") == preview["sha256"]), None)
    source = source or graph.record("Source", name=Path(path).name,
                                    paper_import=True,
                                    locator={"root_id": root_id, "repository": root.get("repository", root_id),
                                             "path": path, "sha256": preview["sha256"],
                                             "revision": preview.get("revision"),
                                             "dirty": preview.get("dirty", False), "kind": "paper"})
    bibliography_report = preview.get("bibliography_report") or {}
    bibliography_path = bibliography_report.get("path") or path
    bibliography_sha256 = bibliography_report.get("sha256") or preview["sha256"]
    reference_nodes: list[dict[str, Any]] = []
    references_by_key: dict[str, dict[str, Any]] = {}
    for entry in preview.get("bibliography", []):
        recorded = refs.record_reference(
            graph,
            entry,
            provenance={
                "kind": "paper_bibliography",
                "paper_path": path,
                "paper_sha256": preview["sha256"],
                "bibliography_path": bibliography_path,
                "bibliography_sha256": bibliography_sha256,
            },
            source_locator={
                "root_id": root_id,
                "repository": root.get("repository", root_id),
                "path": bibliography_path,
                "sha256": bibliography_sha256,
                "kind": "bibliography",
            },
        )
        reference = recorded["reference"]
        reference_nodes.append(reference)
        for key in reference.get("citation_keys", []):
            references_by_key[str(key).casefold()] = reference
    state = graph.state()
    base_pages = [n for n in state.get("nodes", []) if n.get("type") == "Decision" and
                  n.get("paper_ingest_page_complete") is True and
                  n.get("root_id") == root_id and n.get("path") == path and
                  n.get("sha256") == preview["sha256"] and n.get("formula_offset") == 0]
    if formula_offset > 0 and not base_pages:
        raise ResearchError("formula pagination requires the offset-zero import page first")
    prior_page = next((n for n in state.get("nodes", []) if n.get("type") == "Decision"
                       and n.get("paper_ingest_page_complete") is True
                       and n.get("root_id") == root_id and n.get("path") == path
                       and n.get("sha256") == preview["sha256"] and n.get("formula_offset") == 0), None)
    frame_id = prior_page.get("frame_id") if prior_page else None
    frame = next((n for n in state.get("nodes", []) if n.get("id") == frame_id), None)
    if frame is None:
        frame = graph.record("Frame", boundary=f"Source-scoped mathematical context in {path}",
                             state="Only declarations explicitly present in the selected TeX source",
                             grounding="TeX source spans and content hash",
                             limitations="Notation meaning, symbol conventions, and mathematical truth are not inferred.")
        frame_id = frame["id"]
    claims = []
    formula_evidence = []
    mathematical_context = []
    label_to_id: dict[str, str] = {}
    claim_ids: list[str] = []
    if formula_offset == 0 and not base_pages:
      for record in preview["records"]:
        locator = {"root_id": root_id, "repository": root.get("repository", root_id),
                   "path": path, "sha256": preview["sha256"],
                   "revision": preview.get("revision"),
                   "dirty": preview.get("dirty", False), **record["locator"]}
        claim = graph.record("Claim", wording=record["wording"],
                             claim_type=_TYPE_MAP.get(record["kind"], "inference"),
                             status="unresolved", paper_label=record.get("label", ""),
                             section=record.get("section"), source_locator=locator,
                             raw_excerpt=record.get("raw_excerpt", ""),
                             parsed_assertion=True, semantics_verified=False,
                             interpretation_candidate=record["interpretation"],
                             proof_text=record.get("proof"),
                             proof_locator=({**locator, **(record["proof_locator"] or {})}
                                            if record.get("proof_locator") else None),
                             proof_references=record.get("proof_references", []),
                             citations=record.get("citations", []),
                             unresolved_references=[r for r in preview["unresolved_references"]
                                                   if r.get("claim_label") == record.get("label")])
        graph.link(claim["id"], "cites", source["id"],
                   provenance={"kind": "source_import", "sha256": preview["sha256"]},
                   rationale="Claim wording was parsed from this source; this link does not assess truth.")
        for citation_key in record.get("citations", []):
            reference = references_by_key.get(str(citation_key).casefold())
            if reference is not None:
                graph.link(
                    claim["id"], "cites", reference["id"],
                    provenance={
                        "kind": "author_citation_key",
                        "citation_key": citation_key,
                        "source_path": path,
                        "source_sha256": preview["sha256"],
                    },
                    rationale="The author wrote this citation key in the statement; this does not establish support.",
                )
        claims.append(claim)
        if record.get("label"):
            label_to_id[record["label"]] = claim["id"]
    if base_pages:
        claim_ids = list(base_pages[0].get("claim_ids", []))
        by_id = {n["id"]: n for n in state.get("nodes", [])}
        claims = [by_id[cid] for cid in claim_ids if cid in by_id]
        label_to_id = {claim.get("paper_label"): claim["id"] for claim in claims
                       if claim.get("paper_label")}
    else:
        claim_ids = [claim["id"] for claim in claims]
    existing_evidence = {n.get("candidate_id"): n for n in
                         (graph.state() if hasattr(graph, "state") else graph).get("nodes", [])
                         if n.get("type") == "Evidence" and n.get("evidence_type") == "sourced_formula_candidate"}
    existing_math_context = {n.get("candidate_id"): n for n in
                             (graph.state() if hasattr(graph, "state") else graph).get("nodes", [])
                             if n.get("type") in {"MathematicalSymbol", "MathematicalDefinition"}}
    local_scope_by_span: dict[tuple[int, int], str] = {}
    existing_local_frames = {n.get("candidate_id"): n for n in
                             (graph.state() if hasattr(graph, "state") else graph).get("nodes", [])
                             if n.get("type") == "Frame" and n.get("scope_kind") == "local_source_context"}
    local_items = [item for item in preview["mathematical_context"]["records"]
                   if item.get("scope", {}).get("kind") != "paper"]
    local_scope_specs: dict[tuple[int, int], dict[str, Any]] = {}
    for item in local_items:
        scope = item["scope"]
        key = (scope["start_offset"], scope["end_offset"])
        local_scope_specs.setdefault(key, scope)
    local_scope_by_span = {}
    scope_frame_by_span: dict[tuple[int, int], dict[str, Any]] = {}
    for (start, end), scope in sorted(local_scope_specs.items(), key=lambda pair: pair[0][1] - pair[0][0], reverse=True):
        frame_key = "math-local-frame-" + hashlib.sha256(
            f"{root_id}\\0{path}\\0{preview['sha256']}\\0{start}\\0{end}".encode()).hexdigest()[:20]
        local_frame = existing_local_frames.get(frame_key)
        if local_frame is None:
            source_locator = {"root_id": root_id, "repository": root.get("repository", root_id),
                              "path": path, "sha256": preview["sha256"],
                              "start_line": scope["start_line"], "end_line": scope["end_line"],
                              "start_offset": start, "end_offset": end}
            local_frame = graph.record("Frame", candidate_id=frame_key,
                                       boundary=f"Local mathematical context in {scope['environment']}",
                                       state="Applies inside the smallest enclosing recognized statement environment or exact local declaration",
                                       scope_kind="local_source_context", source_locator=source_locator,
                                       grounding="Source-located explicit TeX notation or domain declaration",
                                       limitations="Does not apply outside this statement environment.")
        scope_frame_by_span[(start, end)] = local_frame
        parents = [(candidate_span, candidate_frame) for candidate_span, candidate_frame in scope_frame_by_span.items()
                   if candidate_span != (start, end) and candidate_span[0] <= start
                   and end <= candidate_span[1]]
        parent_frame = min(parents, key=lambda pair: pair[0][1] - pair[0][0])[1] if parents else None
        parent_id = parent_frame["id"] if parent_frame else frame_id
        has_parent = any(edge.get("src") == local_frame["id"] and edge.get("type") == "scoped_by"
                         and edge.get("tgt") == parent_id
                         for edge in (graph.state() if hasattr(graph, "state") else graph).get("edges", []))
        if not has_parent:
            graph.link(local_frame["id"], "scoped_by", parent_id,
                       provenance={"kind": "local_scope_parent", "sha256": preview["sha256"]},
                       rationale="This local statement frame inherits notation from its nearest enclosing paper or statement scope.")
    for item in local_items:
        scope = item["scope"]
        key = (scope["start_offset"], scope["end_offset"])
        local_scope_by_span[(item["start"], item["end"])] = scope_frame_by_span[key]["id"]
    if formula_offset == 0 and not base_pages:
        for record, claim in zip(preview["records"], claims):
            for label in record.get("proof_references", []):
                target_id = label_to_id.get(label)
                if target_id:
                    graph.link(claim["id"], "derived_from", target_id,
                               provenance={"kind": "syntactic_author_reference",
                                           "source_path": path,
                                           "source_sha256": preview["sha256"],
                                           "line": (record.get("proof_locator") or {}).get("start_line"),
                                           "reference_label": label,
                                           "established_dependency": False},
                               rationale="A parsed proof contains this author-written reference; this does not establish that the proof depends on or proves the cited claim.")
    for formula in preview["formula_candidates"]:
        existing = existing_evidence.get(formula["candidate_id"])
        if existing is not None:
            formula_evidence.append(existing)
            continue
        locator = {"root_id": root_id, "repository": root.get("repository", root_id),
                   "path": path, "sha256": preview["sha256"],
                   "revision": preview.get("revision"),
                   "dirty": preview.get("dirty", False), **formula["locator"]}
        evidence = graph.record("Evidence", evidence_type="sourced_formula_candidate",
                                candidate_id=formula["candidate_id"],
                                wording=formula["formula"], raw_excerpt=formula["raw_excerpt"],
                                context_excerpt=formula["context_excerpt"],
                                context_kind=formula["context_kind"],
                                display=formula["display"], locator=locator,
                                interpretation_status=formula["interpretation_status"],
                                is_claim=False, ambiguity=formula["ambiguity"],
                                truth_assessed=False)
        graph.link(evidence["id"], "cites", source["id"],
                   provenance={"kind": "display_math_extraction",
                               "candidate_id": formula["candidate_id"],
                               "sha256": preview["sha256"]},
                   rationale="This locates displayed source math as a candidate; it is not a claim or semantic reading.")
        formula_start = formula["locator"]["start_offset"]
        formula_end = formula["locator"]["end_offset"]
        matching_local_scopes = [((start, end), local_frame["id"])
                                 for (start, end), local_frame in scope_frame_by_span.items()
                                 if start <= formula_start and formula_end <= end]
        if matching_local_scopes:
            smallest_span, smallest_frame = min(matching_local_scopes,
                                                key=lambda pair: pair[0][1] - pair[0][0])
            scopes = [smallest_frame]
        else:
            scopes = [frame_id]
        for scope_id in scopes:
            graph.link(evidence["id"], "scoped_by", scope_id,
                       provenance={"kind": "enclosing_local_statement" if matching_local_scopes else "paper_source_scope",
                                   "sha256": preview["sha256"]},
                       rationale="The formula candidate is scoped to the smallest enclosing local statement with an explicit domain declaration, or to the paper frame when no such statement contains it.")
        for record_index in formula["containing_claim_indexes"]:
            if 0 <= record_index < len(claims):
                claim_id = claims[record_index]["id"]
                graph.link(evidence["id"], "derived_from", claim_id,
                           provenance={"kind": "source_span_containment",
                                       "candidate_id": formula["candidate_id"],
                                       "source_record_index": record_index,
                                       "established_dependency": False},
                           rationale="The formula source span lies within this parsed statement span; this is a locator relation, not an inference or proof dependency.")
        formula_evidence.append(evidence)
    for item in preview["mathematical_context"]["records"]:
        existing = existing_math_context.get(item["candidate_id"])
        if existing is not None:
            mathematical_context.append(existing)
            continue
        locator = {"root_id": root_id, "repository": root.get("repository", root_id),
                   "path": path, "sha256": preview["sha256"],
                   "revision": preview.get("revision"), "dirty": preview.get("dirty", False),
                   **item["locator"]}
        node_type = "MathematicalSymbol" if item["kind"] == "symbol" else "MathematicalDefinition"
        fields = {key: value for key, value in item.items()
                  if key not in {"kind", "start", "end", "locator", "ordinal"}}
        node = graph.record(node_type, **fields, locator=locator,
                            source_excerpt=item["raw_excerpt"], truth_assessed=False)
        graph.link(node["id"], "cites", source["id"],
                   provenance={"kind": "source_math_context", "sha256": preview["sha256"]},
                   rationale="This records explicit notation or definition text at its source location; no mathematical semantics are inferred.")
        scope = local_scope_by_span.get((item["start"], item["end"]), frame_id)
        graph.link(node["id"], "scoped_by", scope,
                   provenance={"kind": "local_source_scope" if scope != frame_id else "paper_source_scope",
                               "sha256": preview["sha256"]},
                   rationale="This source declaration is scoped either to its exact local domain span or to the paper-wide notation frame.")
        if item["declaration"] in {"explicit_domain_membership", "explicit_prose_domain"}:
            names = [name.strip() for name in (item.get("name") or "").split(",") if name.strip()]
            for name in names:
                symbol_key = item["candidate_id"] + "-symbol-" + name
                symbol = existing_math_context.get(symbol_key)
                if symbol is None:
                    symbol = graph.record("MathematicalSymbol", candidate_id=symbol_key,
                                          name=name, notation=name,
                                          symbol_role="explicit_local_scalar",
                                          locality="local_source_context",
                                          locator=locator,
                                          interpretation_status="source_declared_uninterpreted",
                                          semantics_verified=False, truth_assessed=False)
                    existing_math_context[symbol_key] = symbol
                    graph.link(symbol["id"], "cites", source["id"],
                               provenance={"kind": "source_math_context", "sha256": preview["sha256"]},
                               rationale="The variable name is explicitly present in the cited domain declaration.")
                    graph.link(symbol["id"], "scoped_by", scope,
                               provenance={"kind": "local_source_scope", "sha256": preview["sha256"]},
                               rationale="This scalar name is scoped only to the exact source declaration.")
                if symbol not in mathematical_context:
                    mathematical_context.append(symbol)
                graph.link(node["id"], "defines", symbol["id"],
                           provenance={"kind": "explicit_domain_membership", "sha256": preview["sha256"]},
                           rationale="The source explicitly assigns this named scalar to the stated domain; no additional convention is inferred.")
        mathematical_context.append(node)
    # A completion marker is written last. Partial mutation failures are
    # recognizable and will not be mistaken for a successfully imported paper.
    page_complete = bool(claims or formula_evidence or mathematical_context or preview["records"] or formula_offset == 0)
    if page_complete:
        graph.record("Decision", wording=f"Paper import page completed for {path}",
                     paper_ingest_page_complete=True, root_id=root_id, path=path,
                     sha256=preview["sha256"], source_id=source["id"],
                     formula_offset=formula_offset,
                     formula_limit=max_formula_candidates,
                     formula_next_offset=preview["formula_extraction"]["next_offset"],
                     formula_has_more=preview["formula_extraction"]["has_more"],
                     claim_ids=claim_ids,
                     formula_evidence_ids=[evidence["id"] for evidence in formula_evidence],
                     mathematical_context_ids=[node["id"] for node in mathematical_context],
                     frame_id=frame_id,
                     unresolved_extractions=preview["unresolved_extractions"],
                     truth_assessed=False)
    return {"ingested": page_complete, "duplicate": False,
            "partial_import": not page_complete,
            "formula_has_more": preview["formula_extraction"]["has_more"],
            "formula_next_offset": preview["formula_extraction"]["next_offset"],
            "preview": preview,
            "source": source, "claims": claims, "formula_evidence": formula_evidence,
            "mathematical_context": mathematical_context, "references": reference_nodes,
            "frame_id": frame_id}


__all__ = ["preview_paper", "ingest_paper"]
