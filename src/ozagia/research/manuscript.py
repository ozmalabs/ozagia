"""Manuscript bundle export and citation checks.

The bundle describes authored records: it does not assess whether the prose is
true, whether a citation supports a claim, or whether a source actually exists.
Conversion through Pandoc/Quarto is optional and explicit, and it is invoked
with list arguments only (never through a shell).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ozagia.research import references as refs
from ozagia.research import review
from ozagia.research.graph import ResearchError

Runner = Callable[[list[str], str], Mapping[str, Any]]

_LATEX_CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|parencite|textcite|autocite)"
    r"\*?(?:\[[^\]]*\])*\{([^}]+)\}"
)
_MARKDOWN_CITE_RE = re.compile(r"\[([^\[\]\n]*@[^\[\]\n]*)\]")
_MARKDOWN_KEY_RE = re.compile(r"@([^\s,;\[\](){}]+)")
_FORMAT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


class ManuscriptError(ResearchError):
    """A manuscript body, bundle location, or conversion request is invalid."""


# --------------------------------------------------------------------------- #
# citation markers and reference lookup
# --------------------------------------------------------------------------- #
def _clean_key(value: Any) -> str:
    return str(value).strip().lstrip("@").strip().strip(".,;:")


def _citation_keys(body: str) -> list[str]:
    """Return unique citation keys in body order, accepting Markdown and LaTeX."""
    matches: list[tuple[int, list[str]]] = []
    for match in _LATEX_CITE_RE.finditer(body):
        keys = [_clean_key(value) for value in match.group(1).split(",")]
        keys = [key for key in keys if key]
        if keys:
            matches.append((match.start(), keys))
    for match in _MARKDOWN_CITE_RE.finditer(body):
        keys = []
        for chunk in re.split(r"[;,]", match.group(1)):
            for key_match in _MARKDOWN_KEY_RE.finditer(chunk):
                key = _clean_key(key_match.group(1))
                if key:
                    keys.append(key)
        if keys:
            matches.append((match.start(), keys))
    matches.sort(key=lambda item: item[0])
    ordered: list[str] = []
    seen: set[str] = set()
    for _, keys in matches:
        for key in keys:
            folded = key.casefold()
            if folded not in seen:
                seen.add(folded)
                ordered.append(key)
    return ordered


def _reference_keys(reference: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    single = reference.get("citation_key")
    if single:
        key = str(single).strip()
        if key:
            keys.append(key)
    raw = reference.get("citation_keys")
    if isinstance(raw, list):
        for item in raw:
            if item is None:
                continue
            key = str(item).strip()
            if key:
                keys.append(key)
    elif isinstance(raw, str) and raw.strip():
        keys.append(raw.strip())
    return list(dict.fromkeys(keys))


def _reference_nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and node.get("type") == "Reference"
    ]


def _claim_nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and node.get("type") == "Claim"
    ]


def _reference_index(
    references: list[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for reference in references:
        for key in _reference_keys(reference):
            index.setdefault(key.casefold(), reference)
    return index


def _source_linked(reference_id: str, edges: list[Mapping[str, Any]]) -> bool:
    return any(
        edge.get("type") == "manifestation_of" and edge.get("tgt") == reference_id
        for edge in edges
    )


def _locator(node: Mapping[str, Any]) -> Mapping[str, Any]:
    for field in ("source_locator", "locator"):
        value = node.get(field)
        if not isinstance(value, Mapping):
            continue
        locator = {
            key: item for key, item in value.items()
            if item not in (None, "", [], {})
        }
        if locator:
            return locator
    return {}


def _reference_summary(
    reference: Mapping[str, Any],
    *,
    reason: str,
    retraction_status: str = "",
) -> dict[str, Any]:
    keys = _reference_keys(reference)
    return {
        "record_id": str(reference.get("id") or ""),
        "citation_key": str(reference.get("citation_key") or (keys[0] if keys else "")),
        "citation_keys": keys,
        "title": str(reference.get("title") or ""),
        "retraction_status": retraction_status,
        "reason": reason,
    }


def _claim_summary(claim: Mapping[str, Any]) -> dict[str, Any]:
    wording = claim.get("wording") or claim.get("text") or claim.get("title") or ""
    return {
        "record_id": str(claim.get("id") or ""),
        "wording": str(wording),
        "status": str(claim.get("status") or ""),
        "claim_type": str(claim.get("claim_type") or ""),
        "reason": "Claim has no source_locator or locator.",
    }


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_manuscript(
    state: Mapping[str, Any],
    body: str,
    *,
    require_claim_sources: bool = True,
) -> dict[str, Any]:
    """Check citation keys and claim source locators against the graph.

    ``ok`` is false when any reported list is non-empty. It means the bundle's
    mechanical checks passed; it does not mean the manuscript is true or that a
    citation supports the sentence around it.
    """
    if not isinstance(state, Mapping):
        raise ManuscriptError("manuscript checks need a graph state mapping")
    if not isinstance(body, str):
        raise ManuscriptError("manuscript body must be a string")

    references = _reference_nodes(state)
    edges = [
        edge for edge in state.get("edges", [])
        if isinstance(edge, Mapping)
    ]
    key_map = _reference_index(references)
    cited_keys = _citation_keys(body)

    unresolved: list[str] = []
    cited_references: dict[str, Mapping[str, Any]] = {}
    for key in cited_keys:
        reference = key_map.get(key.casefold())
        if reference is None:
            unresolved.append(key)
            continue
        cited_references.setdefault(str(reference.get("id") or ""), reference)
    unresolved.sort(key=lambda key: (key.casefold(), key))

    retracted = []
    for reference in cited_references.values():
        retraction = reference.get("retraction")
        if not isinstance(retraction, Mapping):
            retraction = {}
        status = str(retraction.get("status") or "").casefold()
        if status in {"retracted", "concern"}:
            retracted.append(_reference_summary(
                reference,
                reason=f"Reference retraction status is {status}.",
                retraction_status=status,
            ))
    retracted.sort(key=lambda item: (
        item["citation_key"].casefold(), item["record_id"], item["retraction_status"]))

    cited_ids = set(cited_references)
    unused = []
    for reference in references:
        reference_id = str(reference.get("id") or "")
        if reference_id in cited_ids or _source_linked(reference_id, edges):
            continue
        unused.append(_reference_summary(
            reference,
            reason="Reference is not cited by the manuscript and is not linked to a Source.",
        ))
    unused.sort(key=lambda item: (item["citation_key"].casefold(), item["record_id"]))

    claims = _claim_nodes(state)
    missing = [
        _claim_summary(claim) for claim in claims
        if require_claim_sources and not _locator(claim)
    ]
    missing.sort(key=lambda item: item["record_id"])

    return {
        "ok": not (unresolved or retracted or missing or unused),
        "unresolved_citations": unresolved,
        "retracted_references": retracted,
        "missing_claim_sources": missing,
        "unused_references": unused,
        "counts": {
            "citations": len(cited_keys),
            "resolved_citations": len(cited_keys) - len(unresolved),
            "unresolved_citations": len(unresolved),
            "references": len(references),
            "cited_references": len(cited_references),
            "retracted_references": len(retracted),
            "claims": len(claims),
            "missing_claim_sources": len(missing),
            "unused_references": len(unused),
        },
        "require_claim_sources": bool(require_claim_sources),
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# bundle writing
# --------------------------------------------------------------------------- #
def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80].rstrip("-") or "manuscript"


def _default_output_dir(graph: Any, title: str) -> Path:
    graph_path = getattr(graph, "path", None)
    if graph_path is None:
        raise ManuscriptError("graph has no path; pass output_dir explicitly")
    return Path(graph_path).parent / "exports" / _slug(title)


def _resolve_output_dir(graph: Any, output_dir: str | Path | None, title: str) -> Path:
    if output_dir is None:
        return _default_output_dir(graph, title)
    if not str(output_dir).strip():
        raise ManuscriptError("output_dir must not be empty")
    return Path(output_dir).expanduser()


def _manuscript_markdown(title: str, body: str) -> str:
    text = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if text:
        return f"# {title}\n\n{text}\n"
    return f"# {title}\n"


def _write_text(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8", newline="")
    except OSError as exc:
        raise ManuscriptError(f"cannot write manuscript bundle file {path}: {exc}") from exc


def _format_name(value: Any) -> str:
    if not isinstance(value, str) or not _FORMAT_RE.fullmatch(value):
        raise ManuscriptError(
            "pandoc_format must be a simple format name such as docx, pdf or html")
    return value.casefold()


def _runner_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _default_runner(argv: list[str], cwd: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManuscriptError(f"pandoc could not be started: {exc}") from exc
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _pandoc_status(
    output_dir: Path,
    format_name: str,
    runner: Runner | None,
) -> dict[str, Any]:
    target = output_dir / f"output.{format_name}"
    argv = [
        "pandoc",
        "manuscript.md",
        "--citeproc",
        "--bibliography",
        "references.bib",
        "-o",
        target.name,
    ]
    if runner is None:
        executable = shutil.which("pandoc")
        if not executable:
            return {
                "requested": True,
                "available": False,
                "status": "unavailable",
                "format": format_name,
                "reason": "pandoc executable was not found on PATH",
                "output": str(target),
            }
        argv[0] = executable
        active_runner: Runner = _default_runner
    else:
        active_runner = runner

    try:
        outcome = active_runner(argv, str(output_dir))
    except Exception as exc:  # Bundle files are already written; report, do not crash.
        return {
            "requested": True,
            "available": True,
            "status": "error",
            "format": format_name,
            "command": list(argv),
            "cwd": str(output_dir),
            "error": str(exc),
            "output": str(target),
        }
    if not isinstance(outcome, Mapping):
        return {
            "requested": True,
            "available": True,
            "status": "error",
            "format": format_name,
            "command": list(argv),
            "cwd": str(output_dir),
            "error": "pandoc runner must return a mapping with returncode, stdout and stderr",
            "output": str(target),
        }
    returncode = outcome.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        return {
            "requested": True,
            "available": True,
            "status": "error",
            "format": format_name,
            "command": list(argv),
            "cwd": str(output_dir),
            "error": "pandoc runner returncode must be an integer",
            "output": str(target),
        }
    return {
        "requested": True,
        "available": True,
        "status": "success" if returncode == 0 else "failed",
        "format": format_name,
        "command": list(argv),
        "cwd": str(output_dir),
        "returncode": returncode,
        "stdout": _runner_text(outcome.get("stdout")),
        "stderr": _runner_text(outcome.get("stderr")),
        "output": str(target),
    }


def build_manuscript(
    graph: Any,
    *,
    title: str,
    body: str,
    output_dir: str | Path | None = None,
    run_pandoc: bool = False,
    pandoc_format: str = "docx",
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Write a deterministic manuscript bundle and return its paths and checks.

    The default output directory is ``<state>/research/exports/<title slug>``.
    Only ``manuscript.md``, ``references.bib``, ``claims.csv`` and
    ``checks.json`` are written; an existing directory is reused and those four
    files are replaced. Pandoc/Quarto conversion runs only when
    ``run_pandoc=True`` and reports ``success``, ``failed``, ``unavailable`` or
    ``error`` without discarding the written bundle.
    """
    if not isinstance(title, str):
        raise ManuscriptError("manuscript title must be a string")
    clean_title = " ".join(title.split())
    if not clean_title:
        raise ManuscriptError("manuscript title must be non-empty")
    if not isinstance(body, str):
        raise ManuscriptError("manuscript body must be a string")
    if runner is not None and not callable(runner):
        raise ManuscriptError("pandoc runner must be callable")
    format_name = _format_name(pandoc_format)

    state = graph.state()
    checks = check_manuscript(state, body)
    output_dir = _resolve_output_dir(graph, output_dir, clean_title)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ManuscriptError(
            f"cannot create manuscript output directory {output_dir}: {exc}") from exc
    if not output_dir.is_dir():
        raise ManuscriptError(f"manuscript output path is not a directory: {output_dir}")

    paths = {
        "manuscript": output_dir / "manuscript.md",
        "references": output_dir / "references.bib",
        "claims": output_dir / "claims.csv",
        "checks": output_dir / "checks.json",
    }
    _write_text(paths["manuscript"], _manuscript_markdown(clean_title, body))
    _write_text(paths["references"], refs.export_bibtex(state))
    _write_text(paths["claims"], review.evidence_table(state, format="csv")["content"])
    _write_text(paths["checks"], json.dumps(
        checks, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    result: dict[str, Any] = {
        "title": clean_title,
        "output_dir": str(output_dir),
        "bundle": {name: str(path) for name, path in paths.items()},
        "count": len(paths),
        "checks": checks,
        "pandoc": {
            "requested": False,
            "status": "not_requested",
            "format": format_name,
        },
        "truth_assessed": False,
    }
    if run_pandoc:
        result["pandoc"] = _pandoc_status(output_dir, format_name, runner)
    return result
