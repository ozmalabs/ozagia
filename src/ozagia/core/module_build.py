"""Preview-first module scaffolding from a repo-scan opportunity.

This is stage two of plan item 15 ("scan repo and build modules"). It reads a
completed :mod:`ozagia.core.repo_scan` report (or one explicit opportunity),
turns the selection into a deterministic scaffold plan, and writes that plan
only after a one-time confirmation token. A preview writes nothing.

The scaffold is deliberately small and dependency-free: a README, an
``__init__.py``, a ``module.py`` and one generated test. It is created under
the caller's destination (``modules/<id>/`` by default) and never edits ozagia
core, installs a dependency, runs a test, commits, merges or pushes. The plan
says this explicitly as an advisory scaffold, not a merge.

Every write is re-checked against the repository root: absolute paths,
``..``, ``.git``, ``.ozagia``, symlinked path components and files outside the
destination are refused. Existing target files are refused unless ``force``.
An audit record is appended under ``.ozagia/modules/audit.jsonl``.

No function here calls a model, reaches the network or runs a subprocess. The
generated test is runnable, but apply does not run it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import keyword
import os
import re
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ozagia.config import state_path

SCHEMA = "ozagia.module_build.plan.v1"
AUDIT_SCHEMA = "ozagia.module_build.audit.v1"
SCHEMA_VERSION = 1
MODULES_DIRNAME = "modules"
AUDIT_FILE = "audit.jsonl"
DEFAULT_DEST_TEMPLATE = "modules/{module_id}"

#: Reports written by ``python -m ozagia.repo_scan_cli scan --out``. The CLI
#: uses the first existing path when neither ``--report`` nor an explicit
#: opportunity is given.
DEFAULT_REPORT_PATHS = (
    ".ozagia/repo_scan.json",
    ".ozagia/reports/repo_scan.json",
    ".ozagia/repo_scan/latest.json",
)

NOT_MERGE = (
    "advisory scaffold, not a merge: this plan creates files under the write "
    "scope only. It does not edit ozagia core or any existing module, install "
    "a dependency, run a test, call a model, use the network, commit, merge "
    "or push."
)

LIMITS = {
    "core_edits": False,
    "dependency_install": False,
    "network": False,
    "model_calls": False,
    "auto_merge": False,
    "tests_executed_by_apply": False,
}

STANDARD_NOT_ASSESSED = (
    "whether the repo-scan evidence still reflects the current checkout",
    "whether this module name and interface match the intended design",
    "dependency availability, versions and licence terms; nothing is "
    "installed or resolved",
    "integration with ozagia core, the CLI, docs or existing packages; none "
    "is attempted",
    "whether the generated test or the generated module passes; apply does "
    "not run either",
    "review, commit, merge, push and upstream submission; all are separate "
    "and none happens here",
)

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_GENERATED_BYTES = 256 * 1024
MAX_AUDIT_RECORDS = 1000
MAX_OPPORTUNITIES = 5000
MAX_EVIDENCE = 40

_MODULE_ID = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOKEN = re.compile(r"mb1_[0-9a-f]{40}\Z")
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:/")
_SPECIAL_PARTS = frozenset({".git", ".ozagia"})
_GENERIC_STEMS = frozenset({"__init__", "__main__", "main", "module", "index"})
_GENERIC_PARENTS = frozenset({"src", "tests", "test", "lib", "pkg", "packages"})


class ModuleBuildError(ValueError):
    """A report, plan or write request is invalid, or IO failed."""


class ModuleBuildRefused(ModuleBuildError):
    """A policy check refused the plan or apply request."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _plan_digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + _sha_text(_canonical(dict(payload)))


def _confirmation_token(root: Path, digest: str) -> str:
    material = f"{SCHEMA}|{root}|{digest}"
    return "mb1_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _token_fingerprint(token: str) -> str:
    return "sha256:" + _sha_text(token)[:16]


def _one_line(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:limit]


def _optional_text(value: Any, limit: int = 4000) -> str:
    return _one_line(value, limit)


def _json_str(value: str) -> str:
    """A Python/JSON string literal with no raw control characters."""
    return json.dumps(str(value), ensure_ascii=True)


def _doc_text(value: Any, limit: int = 240) -> str:
    """One-line prose that cannot terminate a generated ``\"\"\"`` docstring."""
    return _one_line(value, limit).replace('"""', "'''")


def _resolve_root(root: str | Path) -> Path:
    try:
        path = Path(root).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ModuleBuildError(f"cannot resolve root {root!r}: {exc}") from exc
    if not path.is_dir():
        raise ModuleBuildError(f"root is not a directory: {path}")
    return path


def _special_state_rel(root: Path) -> str | None:
    """The state directory relative to ``root`` when it is inside it."""
    try:
        return state_path(root).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _refuse_special(rel: str, root: Path, *, where: str) -> None:
    parts = Path(rel).parts
    for part in parts:
        if part.lower() in _SPECIAL_PARTS:
            raise ModuleBuildRefused(
                f"{where}: {rel!r} traverses protected path {part!r}")
    state_rel = _special_state_rel(root)
    if state_rel and (rel == state_rel or rel.startswith(state_rel + "/")):
        raise ModuleBuildRefused(
            f"{where}: {rel!r} is inside the state directory {state_rel!r}")


def _relative_parts(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        raise ModuleBuildError(f"{where}: must be a non-empty relative path")
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or _WINDOWS_ABS.match(text):
        raise ModuleBuildRefused(
            f"{where}: must be relative to the repository root: {value!r}")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ModuleBuildRefused(
                f"{where}: must stay inside the repository root: {value!r}")
        parts.append(part)
    if not parts:
        raise ModuleBuildRefused(f"{where}: must name a path inside the root")
    return parts


def _contains(root: Path, candidate: Path, *, where: str) -> None:
    try:
        candidate.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise ModuleBuildRefused(
            f"{where}: resolves outside the repository root") from None


def _no_symlink_components(root: Path, parts: Sequence[str], *,
                           where: str) -> None:
    current = root
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ModuleBuildRefused(
                    f"{where}: path component {current} is a symlink")
        except OSError as exc:
            raise ModuleBuildError(f"{where}: cannot inspect {current}: {exc}") from exc


def _normalise_dest(root: Path, dest: Any, module_id: str) -> str:
    if dest is None:
        dest = DEFAULT_DEST_TEMPLATE.format(module_id=module_id)
    if not isinstance(dest, (str, Path)) or not str(dest).strip():
        raise ModuleBuildError("destination must be a non-empty path")
    text = str(dest).strip().replace("\\", "/")
    if text.startswith("/"):
        try:
            parts = list(Path(text).expanduser().resolve().relative_to(root).parts)
        except (OSError, RuntimeError, ValueError):
            raise ModuleBuildRefused(
                "destination must be inside the repository root") from None
        if not parts:
            raise ModuleBuildRefused(
                "destination must name a directory inside the root")
    elif _WINDOWS_ABS.match(text):
        raise ModuleBuildRefused(
            "destination must be inside the repository root")
    else:
        parts = _relative_parts(text, where="destination")
    if any(part.lower() in _SPECIAL_PARTS for part in parts):
        raise ModuleBuildRefused(
            "destination must not traverse .git or .ozagia")
    for part in parts:
        if not _IDENTIFIER.fullmatch(part) or keyword.iskeyword(part):
            raise ModuleBuildRefused(
                "destination parts must be valid Python identifiers so the "
                f"scaffold is importable: {part!r}")
    rel = "/".join(parts)
    _refuse_special(rel, root, where="destination")
    _no_symlink_components(root, parts, where="destination")
    _contains(root, root.joinpath(*parts), where="destination")
    return rel


def _target_path(root: Path, rel: str, dest_rel: str, *,
                 where: str = "file") -> Path:
    parts = _relative_parts(rel, where=where)
    rel_text = "/".join(parts)
    if rel_text == dest_rel or not rel_text.startswith(dest_rel + "/"):
        raise ModuleBuildRefused(
            f"{where}: {rel_text!r} is outside the write scope {dest_rel!r}")
    _refuse_special(rel_text, root, where=where)
    _no_symlink_components(root, parts, where=where)
    target = root.joinpath(*parts)
    _contains(root, target, where=where)
    try:
        target.resolve().relative_to(root.joinpath(*dest_rel.split("/")).resolve())
    except (OSError, RuntimeError, ValueError):
        raise ModuleBuildRefused(
            f"{where}: {rel_text!r} resolves outside the destination "
            f"{dest_rel!r}") from None
    return target


# --------------------------------------------------------------------------- #
# report and opportunity loading
# --------------------------------------------------------------------------- #
def default_report_path(root: str | Path) -> Path | None:
    """The first conventional repo-scan report under ``root``, or None."""
    root_p = Path(root).expanduser().resolve()
    for rel in DEFAULT_REPORT_PATHS:
        candidate = root_p / rel
        if candidate.is_file():
            return candidate
    return None


def resolve_report_path(root: str | Path, path: str | Path) -> Path:
    """Resolve a report path relative to the cwd, then to the root."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    cwd_candidate = Path.cwd() / candidate
    if cwd_candidate.is_file():
        return cwd_candidate
    root_candidate = Path(root).expanduser().resolve() / candidate
    if root_candidate.is_file():
        return root_candidate
    return cwd_candidate


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a repo-scan report or a single explicit opportunity object."""
    report_path = Path(path).expanduser()
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    if not report_path.is_file():
        raise ModuleBuildError(f"report not found: {report_path}")
    try:
        size = report_path.stat().st_size
    except OSError as exc:
        raise ModuleBuildError(f"cannot stat report {report_path}: {exc}") from exc
    if size > MAX_REPORT_BYTES:
        raise ModuleBuildError(
            f"report is larger than {MAX_REPORT_BYTES} bytes: {report_path}")
    try:
        raw = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ModuleBuildError(f"cannot read report {report_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ModuleBuildError(f"report is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ModuleBuildError("report must be a JSON object")
    if "opportunities" in raw:
        items = raw.get("opportunities")
        if not isinstance(items, list):
            raise ModuleBuildError("report.opportunities must be a list")
        if len(items) > MAX_OPPORTUNITIES:
            raise ModuleBuildError(
                f"report has more than {MAX_OPPORTUNITIES} opportunities")
        return dict(raw)
    if "id" in raw:
        return {
            "schema": "ozagia.repo_scan.explicit_opportunity.v1",
            "opportunities": [dict(raw)],
        }
    raise ModuleBuildError(
        "report has neither an opportunities list nor an opportunity id")


def select_opportunity(report: Mapping[str, Any],
                       opportunity_id: str | None = None) -> dict[str, Any]:
    """Select one opportunity by id; a one-item report needs no id."""
    items_raw = report.get("opportunities")
    if not isinstance(items_raw, list):
        raise ModuleBuildError("report.opportunities must be a list")
    items = [dict(item) for item in items_raw if isinstance(item, Mapping)]
    if not items:
        raise ModuleBuildError("report contains no opportunities")
    if opportunity_id is None:
        if len(items) == 1:
            return items[0]
        available = ", ".join(
            str(item.get("id")) for item in items[:10])
        raise ModuleBuildError(
            f"report has {len(items)} opportunities; choose one with "
            f"--opportunity ID (for example: {available})")
    wanted = str(opportunity_id).strip()
    matches = [item for item in items if str(item.get("id")) == wanted]
    if not matches:
        available = ", ".join(str(item.get("id")) for item in items[:10])
        raise ModuleBuildError(
            f"opportunity {wanted!r} is not in the report; available: "
            f"{available}")
    if len(matches) > 1:
        raise ModuleBuildError(
            f"opportunity id {wanted!r} appears {len(matches)} times")
    return matches[0]


def _normalise_opportunity(raw: Mapping[str, Any]) -> dict[str, Any]:
    ident = _one_line(raw.get("id"), 200)
    if not ident:
        raise ModuleBuildError("opportunity has no id")
    evidence_raw = raw.get("evidence")
    evidence: list[dict[str, Any]] = []
    if isinstance(evidence_raw, list):
        for item in evidence_raw[:MAX_EVIDENCE]:
            if isinstance(item, Mapping):
                evidence.append(dict(item))
    not_assessed_raw = raw.get("not_assessed")
    not_assessed: list[str] = []
    if isinstance(not_assessed_raw, list):
        not_assessed = list(dict.fromkeys(
            _one_line(item, 400) for item in not_assessed_raw
            if _one_line(item, 400)))
    effort = _one_line(raw.get("effort"), 8) or "M"
    risk = _one_line(raw.get("risk"), 16) or "medium"
    confidence = _one_line(raw.get("confidence"), 16) or "low"
    return {
        "id": ident,
        "category": _one_line(raw.get("category"), 64),
        "title": _one_line(raw.get("title"), 240) or ident,
        "expected_effect": _optional_text(raw.get("expected_effect")),
        "effort": effort,
        "risk": risk,
        "confidence": confidence,
        "evidence": evidence,
        "proposed_module": _one_line(raw.get("proposed_module"), 240),
        "action": _optional_text(raw.get("action")),
        "not_assessed": not_assessed,
        "rank": raw.get("rank"),
        "score": raw.get("score"),
    }


# --------------------------------------------------------------------------- #
# module identity
# --------------------------------------------------------------------------- #
def _slug(value: Any) -> str:
    text = str(value if value is not None else "").strip().replace("\\", "/")
    text = text.rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    if text.endswith(".py"):
        text = text[:-3]
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "m_" + text
    text = text[:48].rstrip("_")
    if keyword.iskeyword(text):
        text += "_module"
    return text


def _module_id(opportunity: Mapping[str, Any]) -> str:
    proposed = str(opportunity.get("proposed_module") or "").strip()
    candidate = proposed
    if candidate.endswith("/"):
        candidate = candidate.rstrip("/")
    if "/" in candidate:
        parent, _, name = candidate.rstrip("/").rpartition("/")
        if name.endswith(".py"):
            name = name[:-3]
        if name.startswith("test_") and len(name) > 5:
            name = name[5:]
        parent_name = parent.rsplit("/", 1)[-1]
        if (name in _GENERIC_STEMS and parent_name
                and parent_name not in _GENERIC_PARENTS
                and not parent.endswith((":", "://"))):
            candidate = parent_name
        else:
            candidate = name
    elif candidate.endswith(".py"):
        candidate = candidate[:-3]
        if candidate.startswith("test_") and len(candidate) > 5:
            candidate = candidate[5:]
    for value in (candidate, opportunity.get("id"), opportunity.get("title")):
        slug = _slug(value)
        if slug and _MODULE_ID.fullmatch(slug):
            return slug
        if slug:
            trimmed = slug[:48].rstrip("_") or "module"
            if not _MODULE_ID.fullmatch(trimmed):
                trimmed = "m_" + trimmed
            return trimmed[:48]
    return "module"


# --------------------------------------------------------------------------- #
# generated file contents
# --------------------------------------------------------------------------- #
def _format_evidence(item: Mapping[str, Any]) -> str:
    kind = str(item.get("kind") or "?")
    if kind == "file":
        line = item.get("line")
        return f"file `{item.get('path')}`" + (
            f":{line}" if line is not None else "")
    if kind == "graph_key":
        return f"graph `{item.get('key')}`"
    if kind == "command":
        return f"command `{item.get('command')}`"
    if kind == "metric":
        return f"{item.get('name')}={item.get('value')}"
    return kind


def _readme(module_id: str, name: str, dest_rel: str, opportunity: Mapping[str, Any],
            files: Sequence[Mapping[str, Any]], test_rel: str) -> str:
    evidence = opportunity.get("evidence") or []
    evidence_lines = [_format_evidence(item) for item in evidence] or ["none recorded"]
    not_assessed = list(opportunity.get("not_assessed") or [])
    if not not_assessed:
        not_assessed = ["none recorded by the opportunity"]
    lines = [
        f"# {name}",
        "",
        "> **Advisory scaffold, not a merge.** Generated by",
        "> `ozagia.module_build` from repo-scan opportunity",
        f"> `{opportunity['id']}`. This package is not imported by ozagia core,",
        "> does not change an existing module, and has not been reviewed.",
        "",
        "## Opportunity",
        "",
        f"- id: `{opportunity['id']}`",
        f"- category: `{opportunity.get('category') or 'unknown'}`",
        f"- expected effect: {opportunity.get('expected_effect') or 'not recorded'}",
        f"- effort: `{opportunity.get('effort')}`",
        f"- risk: `{opportunity.get('risk')}`",
        f"- confidence: `{opportunity.get('confidence')}`",
        f"- proposed module: `{opportunity.get('proposed_module') or module_id}`",
        f"- action: {opportunity.get('action') or 'not recorded'}",
        "",
        "## Evidence",
        "",
    ]
    lines.extend(f"- {entry}" for entry in evidence_lines)
    lines.extend([
        "",
        "## Scope",
        "",
        f"- module id: `{module_id}`",
        f"- destination: `{dest_rel}/`",
        f"- write scope: `{dest_rel}/`",
        "- dependencies: none; nothing is installed, resolved or imported "
        "from outside the standard library",
        "- core edits: none",
        "",
        "## Files",
        "",
    ])
    for item in files:
        lines.append(f"- `{item['path']}`")
    lines.extend([
        "",
        "## Review gates",
        "",
        "Run these from the repository root before treating the scaffold as "
        "usable:",
        "",
        "```bash",
        f"python -m pytest -q {test_rel}",
        f"ruff check {dest_rel}",
        f"python -c {_json_str('import ' + '.'.join(dest_rel.split('/')))}",
        "```",
        "",
        "Then review the public interface, naming, error handling and "
        "integration yourself. Generated tests are a starting point, not a "
        "proof of correctness.",
        "",
        "## Not assessed",
        "",
    ])
    lines.extend(f"- {item}" for item in not_assessed)
    lines.extend([
        "",
        "## Status",
        "",
        "`scaffold`. No merge, no commit, no dependency installation, no core "
        "edits and no automatic upstream submission.",
        "",
    ])
    return "\n".join(lines)


def _init_source(name: str, opportunity_id: str) -> str:
    title = _doc_text(name)
    opportunity = _doc_text(opportunity_id)
    return (
        f'"""{title}.\n\n'
        "Advisory scaffold generated from repo-scan opportunity\n"
        f"``{opportunity}``. It is not imported by ozagia core and is not\n"
        "merged.\n"
        '"""\n'
        "from .module import MODULE_ID, build_summary\n\n"
        '__all__ = ["MODULE_ID", "build_summary"]\n'
    )


def _module_source(module_id: str, name: str, opportunity_id: str) -> str:
    title = _doc_text(name)
    opportunity = _doc_text(opportunity_id)
    return (
        f'"""{title}.\n\n'
        "Advisory scaffold generated by ``ozagia.module_build`` from repo-scan\n"
        f"opportunity ``{opportunity}``. Replace the summary and entry point\n"
        "with the module's real behaviour after review.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        f"SCHEMA = {_json_str('ozagia.module.scaffold.v1')}\n"
        f"MODULE_ID = {_json_str(module_id)}\n"
        f"SOURCE_OPPORTUNITY = {_json_str(opportunity_id)}\n"
        f"TITLE = {_json_str(name)}\n\n\n"
        "def build_summary() -> dict[str, Any]:\n"
        '    """Return deterministic scaffold metadata for tests and callers."""\n'
        "    return {\n"
        '        "schema": SCHEMA,\n'
        '        "module": MODULE_ID,\n'
        '        "title": TITLE,\n'
        '        "source_opportunity": SOURCE_OPPORTUNITY,\n'
        '        "status": "scaffold",\n'
        "    }\n\n\n"
        "def main() -> int:\n"
        '    """Print the scaffold identity; replace with the real entry point."""\n'
        "    summary = build_summary()\n"
        '    print("{}: {} for {}".format(summary.get("module"), '
        'summary.get("status"), summary.get("source_opportunity")))\n'
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    )


def _test_source(module_id: str, opportunity_id: str,
                 dest_rel: str) -> str:
    package = ".".join(dest_rel.split("/"))
    depth = len(dest_rel.split("/")) + 1
    return (
        f'"""Generated tests for the {module_id} scaffold."""\n'
        "from __future__ import annotations\n\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        f"ROOT = Path(__file__).resolve().parents[{depth}]\n"
        "if str(ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(ROOT))\n\n"
        f"from {package} import module as scaffold  # noqa: E402\n\n\n"
        "def test_summary_is_deterministic() -> None:\n"
        "    first = scaffold.build_summary()\n"
        "    second = scaffold.build_summary()\n"
        "    assert first == second\n"
        f"    assert first['module'] == {_json_str(module_id)}\n"
        f"    assert first['source_opportunity'] == {_json_str(opportunity_id)}\n"
        "    assert first['status'] == 'scaffold'\n"
        "    assert first['schema'] == scaffold.SCHEMA\n\n\n"
        "def test_main_prints_identity(capsys) -> None:\n"
        "    assert scaffold.main() == 0\n"
        "    output = capsys.readouterr().out\n"
        f"    assert {_json_str(module_id)} in output\n"
        f"    assert {_json_str(opportunity_id)} in output\n"
    )


def _file_entry(rel: str, content: str, kind: str) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    return {
        "path": rel,
        "kind": kind,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "content": content,
    }


# --------------------------------------------------------------------------- #
# plan construction
# --------------------------------------------------------------------------- #
def _review_gates(dest_rel: str, test_rel: str) -> list[dict[str, Any]]:
    package = ".".join(dest_rel.split("/"))
    return [
        {
            "id": "scope_contained",
            "kind": "check",
            "required": True,
            "description": (
                f"every generated file resolves inside {dest_rel}/ and not "
                "inside .git, .ozagia, a symlink or outside the root"),
        },
        {
            "id": "generated_tests",
            "kind": "command",
            "required": True,
            "command": f"python -m pytest -q {test_rel}",
            "description": "run the generated test; apply does not run it",
        },
        {
            "id": "lint",
            "kind": "command",
            "required": True,
            "command": f"ruff check {dest_rel}",
            "description": "lint the generated package",
        },
        {
            "id": "importable",
            "kind": "command",
            "required": True,
            "command": f"python -c {_json_str('import ' + package)}",
            "description": "import the generated package from the repo root",
        },
        {
            "id": "human_review",
            "kind": "manual",
            "required": True,
            "description": (
                "review the interface, naming, edge cases and integration "
                "before using or merging the scaffold"),
        },
        {
            "id": "no_core_edits",
            "kind": "check",
            "required": True,
            "description": (
                "confirm the change set contains only files under the write "
                "scope; ozagia core and existing modules are untouched"),
        },
    ]


def _existing_collisions(root: Path, files: Sequence[Mapping[str, Any]],
                         dest_rel: str) -> list[dict[str, str]]:
    collisions: list[dict[str, str]] = []
    dest_path = root.joinpath(*dest_rel.split("/"))
    if dest_path.exists() and not dest_path.is_dir():
        collisions.append({"path": dest_rel, "kind": "not_a_directory"})
    for item in files:
        rel = str(item.get("path"))
        target = root.joinpath(*rel.split("/"))
        if target.exists() or target.is_symlink():
            collisions.append({"path": rel, "kind": "existing_file"})
    return collisions


def _payload(root: Path, module_id: str, name: str, dest_rel: str,
             opportunity: Mapping[str, Any], files: Sequence[Mapping[str, Any]],
             test_rel: str, source_report: Path | None) -> dict[str, Any]:
    collisions = _existing_collisions(root, files, dest_rel)
    warnings: list[str] = []
    if collisions:
        warnings.append(
            "destination already contains a target file; apply refuses "
            "unless --force is used")
    if any(item.get("path") == dest_rel for item in collisions):
        warnings.append("destination exists and is not a directory")
    not_assessed = list(dict.fromkeys([
        *[str(item) for item in opportunity.get("not_assessed") or []],
        *STANDARD_NOT_ASSESSED,
    ]))
    package = ".".join(dest_rel.split("/"))
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "advisory": True,
        "truth_assessed": False,
        "not_merge": NOT_MERGE,
        "root": str(root),
        "source": {
            "report": str(source_report) if source_report is not None else None,
            "opportunity_id": opportunity.get("id"),
        },
        "module_id": module_id,
        "module_name": name,
        "name": name,
        "destination": dest_rel,
        "file_paths": [str(item["path"]) for item in files],
        "module": {
            "id": module_id,
            "name": name,
            "destination": dest_rel,
            "package": package,
            "source": f"{dest_rel}/module.py",
            "test": test_rel,
        },
        "opportunity": dict(opportunity),
        "expected_effect": opportunity.get("expected_effect"),
        "effort": opportunity.get("effort"),
        "risk": opportunity.get("risk"),
        "confidence": opportunity.get("confidence"),
        "evidence": list(opportunity.get("evidence") or []),
        "files": [dict(item) for item in files],
        "dependencies": [],
        "dependencies_note": (
            "none by default; the scaffold uses the Python standard library "
            "only and apply installs nothing"),
        "write_scope": [dest_rel],
        "generated_tests": [{
            "path": test_rel,
            "command": f"python -m pytest -q {test_rel}",
        }],
        "review_gates": _review_gates(dest_rel, test_rel),
        "collisions": collisions,
        "applicable": not collisions,
        "warnings": list(dict.fromkeys(warnings)),
        "not_assessed": not_assessed,
        "limits": dict(LIMITS),
        "rollback": {
            "kind": "manual",
            "paths": [dest_rel],
            "note": (
                "review the change set, then remove the destination to undo "
                "the scaffold; the audit record remains"),
        },
    }


def _source_for(root: Path, *, report_path: str | Path | None,
                report: Mapping[str, Any] | str | Path | None,
                opportunity_id: str | None,
                opportunity: Mapping[str, Any] | None
                ) -> tuple[dict[str, Any], Path | None]:
    if isinstance(report, (str, Path)):
        if report_path is not None:
            raise ModuleBuildError(
                "pass only one of report_path, report or opportunity")
        report_path = report
        report = None
    supplied = sum(item is not None for item in (report_path, report, opportunity))
    if supplied > 1:
        raise ModuleBuildError(
            "pass only one of report_path, report or opportunity")
    if opportunity is not None:
        if not isinstance(opportunity, Mapping):
            raise ModuleBuildError("opportunity must be a mapping")
        return _normalise_opportunity(opportunity), None
    if report is not None:
        if not isinstance(report, Mapping):
            raise ModuleBuildError("report must be a mapping")
        return _normalise_opportunity(
            select_opportunity(report, opportunity_id)), None
    path = report_path
    if path is None:
        path = default_report_path(root)
        if path is None:
            raise ModuleBuildError(
                "no report given and no conventional report under "
                + ", ".join(DEFAULT_REPORT_PATHS))
    resolved = resolve_report_path(root, path)
    selected = _normalise_opportunity(
        select_opportunity(load_report(resolved), opportunity_id))
    return selected, resolved


def build_plan(root: str | Path = ".", *, report_path: str | Path | None = None,
               report: Mapping[str, Any] | str | Path | None = None,
               opportunity_id: str | None = None,
               opportunity: Mapping[str, Any] | None = None,
               dest: str | None = None) -> dict[str, Any]:
    """Build a deterministic scaffold plan; this function writes nothing."""
    root_p = _resolve_root(root)
    selected, source_report = _source_for(
        root_p, report_path=report_path, report=report,
        opportunity_id=opportunity_id, opportunity=opportunity)
    module_id = _module_id(selected)
    name = _one_line(selected.get("title") or module_id, 160)
    dest_rel = _normalise_dest(root_p, dest, module_id)
    test_rel = f"{dest_rel}/tests/test_{module_id}.py"
    files = [
        _file_entry(f"{dest_rel}/README.md",
                    _readme(module_id, name, dest_rel, selected,
                            [
                                {"path": f"{dest_rel}/README.md"},
                                {"path": f"{dest_rel}/__init__.py"},
                                {"path": f"{dest_rel}/module.py"},
                                {"path": test_rel},
                            ], test_rel),
                    "doc"),
        _file_entry(f"{dest_rel}/__init__.py",
                    _init_source(name, str(selected["id"])),
                    "package"),
        _file_entry(f"{dest_rel}/module.py",
                    _module_source(module_id, name, str(selected["id"])),
                    "module"),
        _file_entry(test_rel,
                    _test_source(module_id, str(selected["id"]), dest_rel),
                    "test"),
    ]
    for item in files:
        if item["bytes"] > MAX_GENERATED_BYTES:
            raise ModuleBuildError(
                f"generated file is larger than {MAX_GENERATED_BYTES} bytes: "
                f"{item['path']}")
    payload = _payload(root_p, module_id, name, dest_rel, selected, files,
                       test_rel, source_report)
    digest = _plan_digest(payload)
    plan = dict(payload)
    plan["plan_digest"] = digest
    token = _confirmation_token(root_p, digest)
    plan["confirmation_token"] = token
    plan["token"] = token
    plan["confirmation"] = {
        "required": True,
        "one_time": True,
        "token": token,
        "note": (
            "apply requires this token, this root and this exact plan; a "
            "successful apply consumes the token"),
    }
    _validate_plan(root_p, plan)
    return plan


def preview(root: str | Path = ".", **kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`build_plan`; preview is the default and writes nothing."""
    return build_plan(root, **kwargs)


# --------------------------------------------------------------------------- #
# plan validation and apply
# --------------------------------------------------------------------------- #
def _plan_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    payload.pop("confirmation", None)
    payload.pop("confirmation_token", None)
    payload.pop("token", None)
    payload.pop("plan_digest", None)
    return payload


def _validate_plan(root: Path,
                   plan: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(plan, Mapping):
        raise ModuleBuildError("plan must be a mapping")
    if plan.get("schema") != SCHEMA:
        raise ModuleBuildRefused(
            f"plan schema must be {SCHEMA!r}, got {plan.get('schema')!r}")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ModuleBuildRefused("plan schema_version is not supported")
    if plan.get("root") != str(root):
        raise ModuleBuildRefused(
            "plan was built for a different repository root")
    module = plan.get("module")
    if not isinstance(module, Mapping):
        raise ModuleBuildError("plan.module must be a mapping")
    module_id = str(module.get("id") or "")
    if not _MODULE_ID.fullmatch(module_id):
        raise ModuleBuildError(f"plan.module.id is invalid: {module_id!r}")
    dest_raw = module.get("destination")
    if not isinstance(dest_raw, str) or not dest_raw:
        raise ModuleBuildError("plan.module.destination must be set")
    dest_rel = _normalise_dest(root, dest_raw, module_id)
    if dest_rel != dest_raw:
        raise ModuleBuildRefused(
            "plan.module.destination is not in normalised form")
    write_scope = plan.get("write_scope")
    if write_scope != [dest_rel]:
        raise ModuleBuildRefused(
            "plan.write_scope must contain exactly the destination")
    files_raw = plan.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise ModuleBuildError("plan.files must be a non-empty list")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(files_raw):
        if not isinstance(item, Mapping):
            raise ModuleBuildError(f"plan.files[{index}] must be a mapping")
        rel = item.get("path")
        if not isinstance(rel, str):
            raise ModuleBuildError(f"plan.files[{index}].path must be a string")
        if rel in seen:
            raise ModuleBuildError(f"plan.files[{index}] duplicates {rel!r}")
        seen.add(rel)
        content = item.get("content")
        if not isinstance(content, str):
            raise ModuleBuildError(f"plan.files[{index}].content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_GENERATED_BYTES:
            raise ModuleBuildRefused(
                f"plan file is too large: {rel}")
        expected = hashlib.sha256(encoded).hexdigest()
        if item.get("sha256") != expected:
            raise ModuleBuildRefused(
                f"plan file content does not match its sha256: {rel}")
        if item.get("bytes") != len(encoded):
            raise ModuleBuildRefused(
                f"plan file size does not match its content: {rel}")
        _target_path(root, rel, dest_rel)
        files.append(dict(item))
    return dest_rel, files


def _read_audit(root: Path) -> list[dict[str, Any]]:
    path = state_path(root, MODULES_DIRNAME, AUDIT_FILE)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _token_used(root: Path, token: str) -> bool:
    fingerprint = _token_fingerprint(token)
    return any(
        record.get("event") == "apply"
        and record.get("token_fingerprint") == fingerprint
        for record in _read_audit(root)
    )


def _append_audit(root: Path, record: Mapping[str, Any]) -> Path:
    path = state_path(root, MODULES_DIRNAME, AUDIT_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(record), sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ModuleBuildError(f"cannot write audit {path}: {exc}") from exc
    return path


def _write_files(root: Path, files: Sequence[Mapping[str, Any]],
                 dest_rel: str) -> list[dict[str, Any]]:
    originals: dict[Path, bytes | None] = {}
    prepared: list[tuple[Path, Path, Mapping[str, Any]]] = []
    written: list[dict[str, Any]] = []
    try:
        for item in files:
            rel = str(item["path"])
            target = _target_path(root, rel, dest_rel)
            if target.exists():
                try:
                    originals[target] = target.read_bytes()
                except OSError as exc:
                    raise ModuleBuildError(
                        f"cannot read existing file {target}: {exc}") from exc
            else:
                originals[target] = None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp",
                    dir=str(target.parent))
                temporary_path = Path(temporary)
            except OSError as exc:
                raise ModuleBuildError(
                    f"cannot prepare {target}: {exc}") from exc
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(str(item["content"]).encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary_path, 0o644)
            except BaseException:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
                raise
            prepared.append((temporary_path, target, item))
        for temporary_path, target, item in prepared:
            os.replace(temporary_path, target)
            written.append({
                "path": target.relative_to(root).as_posix(),
                "sha256": str(item["sha256"]),
                "bytes": int(item["bytes"]),
                "replaced": originals.get(target) is not None,
            })
        prepared = []
    except BaseException as exc:
        for temporary_path, _target, _item in prepared:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        for entry in written:
            target = root.joinpath(*str(entry["path"]).split("/"))
            original = originals.get(target)
            try:
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise ModuleBuildError(f"cannot write scaffold: {exc}") from exc
        raise
    return written


def apply(root: str | Path, plan: Mapping[str, Any],
          confirmation: str | None, *, force: bool = False) -> dict[str, Any]:
    """Apply a previewed plan after re-checking its token and every path.

    ``force`` replaces existing target files. It never bypasses the one-time
    token, the plan digest, path containment, ``.git``/``.ozagia`` checks or
    the write scope.
    """
    root_p = _resolve_root(root)
    if not isinstance(plan, Mapping):
        raise ModuleBuildError("plan must be a mapping")
    plan_d = dict(plan)
    payload = _plan_payload(plan_d)
    digest = _plan_digest(payload)
    if plan_d.get("plan_digest") != digest:
        raise ModuleBuildRefused(
            "plan content changed after preview; preview again")
    if not isinstance(confirmation, str) or not confirmation.strip():
        raise ModuleBuildRefused(
            "confirmation token is required: run preview and pass --confirm")
    token = confirmation.strip()
    if not _TOKEN.fullmatch(token):
        raise ModuleBuildRefused("confirmation token is malformed")
    expected = _confirmation_token(root_p, digest)
    if not hmac.compare_digest(token, expected):
        raise ModuleBuildRefused(
            "confirmation token does not match this plan and repository")
    dest_rel, files = _validate_plan(root_p, plan_d)
    if _token_used(root_p, token):
        raise ModuleBuildRefused(
            "confirmation token was already used; preview again")

    dest_path = root_p.joinpath(*dest_rel.split("/"))
    if dest_path.exists() and not dest_path.is_dir():
        raise ModuleBuildRefused(
            f"destination exists and is not a directory: {dest_rel}")
    collisions = _existing_collisions(root_p, files, dest_rel)
    if collisions and not force:
        names = ", ".join(item["path"] for item in collisions[:5])
        raise ModuleBuildRefused(
            f"target file(s) already exist: {names}; preview again with "
            "--force only if replacing them is intended")

    written = _write_files(root_p, files, dest_rel)
    record = {
        "schema": AUDIT_SCHEMA,
        "event": "apply",
        "at": _now(),
        "root": str(root_p),
        "module_id": str(plan_d["module"]["id"]),
        "destination": dest_rel,
        "plan_digest": digest,
        "token_fingerprint": _token_fingerprint(token),
        "forced": bool(force),
        "files": [
            {"path": item["path"], "sha256": item["sha256"],
             "bytes": item["bytes"], "replaced": item["replaced"]}
            for item in written
        ],
        "advisory": True,
        "merged": False,
    }
    audit_path = _append_audit(root_p, record)
    return {
        "schema": AUDIT_SCHEMA,
        "ok": True,
        "applied": True,
        "root": str(root_p),
        "module_id": record["module_id"],
        "destination": dest_rel,
        "files": written,
        "audit": audit_path.relative_to(root_p).as_posix()
        if audit_path.is_relative_to(root_p) else str(audit_path),
        "plan_digest": digest,
        "forced": bool(force),
        "not_merge": NOT_MERGE,
        "next_steps": [
            f"python -m pytest -q {plan_d['module']['test']}",
            f"ruff check {dest_rel}",
            "review the scaffold before using or merging it",
        ],
    }


# --------------------------------------------------------------------------- #
# read-only audit listing
# --------------------------------------------------------------------------- #
def list_applied(root: str | Path = ".") -> list[dict[str, Any]]:
    """Applied-scaffold audit records, newest first; reads only."""
    root_p = _resolve_root(root)
    records = [dict(item) for item in _read_audit(root_p)
               if item.get("event") == "apply"]
    records.sort(key=lambda item: (
        str(item.get("at") or ""), str(item.get("module_id") or "")),
        reverse=True)
    return records[:MAX_AUDIT_RECORDS]


# --------------------------------------------------------------------------- #
# text output
# --------------------------------------------------------------------------- #
def plan_json(plan: Mapping[str, Any]) -> str:
    """Deterministic JSON for a plan."""
    return json.dumps(dict(plan), indent=2, sort_keys=True,
                      ensure_ascii=False, allow_nan=False) + "\n"


def format_plan(plan: Mapping[str, Any]) -> str:
    """A compact, content-free preview of a scaffold plan."""
    module = plan.get("module") or {}
    opportunity = plan.get("opportunity") or {}
    lines = [
        "Module scaffold preview (advisory, nothing written)",
        f"root: {plan.get('root')}",
        f"module: {module.get('id')} - {module.get('name')}",
        f"destination: {module.get('destination')}/",
        f"source opportunity: {opportunity.get('id')} "
        f"({opportunity.get('category') or 'uncategorised'})",
        f"expected effect: {plan.get('expected_effect') or 'not recorded'}",
        f"effort: {plan.get('effort')} | risk: {plan.get('risk')} | "
        f"confidence: {plan.get('confidence')}",
        "",
        "files:",
    ]
    for item in plan.get("files") or []:
        lines.append(
            f"- {item.get('path')} ({item.get('bytes')} bytes, "
            f"sha256:{str(item.get('sha256'))[:16]}...)")
    evidence = plan.get("evidence") or []
    lines.append("")
    lines.append("evidence:")
    if evidence:
        lines.extend(f"- {_format_evidence(item)}" for item in evidence)
    else:
        lines.append("- none recorded")
    lines.extend([
        "",
        "dependencies: none; apply installs nothing",
        f"write scope: {', '.join(plan.get('write_scope') or [])}",
        "",
        "generated tests:",
    ])
    for item in plan.get("generated_tests") or []:
        lines.append(f"- {item.get('path')} -> {item.get('command')}")
    lines.append("")
    lines.append("review gates:")
    for item in plan.get("review_gates") or []:
        detail = item.get("command") or item.get("description")
        lines.append(f"- {item.get('id')} ({item.get('kind')}): {detail}")
    collisions = plan.get("collisions") or []
    lines.append("")
    lines.append("collisions: " + (
        ", ".join(f"{item.get('path')} ({item.get('kind')})"
                  for item in collisions) if collisions else "none"))
    if not plan.get("applicable", True):
        lines.append("applicable: no (apply refuses unless --force is used)")
    confirmation = plan.get("confirmation") or {}
    source = plan.get("source") or {}
    apply_args = [
        "python", "-m", "ozagia.module_build_cli", "apply",
        "--root", str(plan.get("root")),
    ]
    if source.get("report"):
        apply_args.extend(["--report", str(source["report"])])
    apply_args.extend([
        "--opportunity", str(opportunity.get("id")),
        "--dest", str(module.get("destination")),
        "--confirm", str(confirmation.get("token")),
    ])
    lines.extend([
        "",
        f"confirmation token: {confirmation.get('token')}",
        "apply with:",
        "  " + shlex.join(apply_args),
        "the token is one-time and is bound to this root and plan digest; "
        "preview has written nothing",
        "",
        "not assessed:",
    ])
    lines.extend(f"- {item}" for item in plan.get("not_assessed") or [])
    lines.extend(["", "ADVISORY: " + str(plan.get("not_merge") or NOT_MERGE)])
    return "\n".join(lines).rstrip() + "\n"


def format_applied(record: Mapping[str, Any]) -> str:
    """Text for one apply result or audit record."""
    lines = [
        f"applied scaffold: {record.get('module_id')}",
        f"destination: {record.get('destination')}/",
        f"files: {len(record.get('files') or [])}",
    ]
    for item in record.get("files") or []:
        replaced = " (replaced)" if item.get("replaced") else ""
        lines.append(f"- {item.get('path')}{replaced}")
    if record.get("audit"):
        lines.append(f"audit: {record.get('audit')}")
    lines.append("ADVISORY: " + str(record.get("not_merge") or NOT_MERGE))
    return "\n".join(lines) + "\n"


def format_applied_list(records: Sequence[Mapping[str, Any]]) -> str:
    """Text for ``list``; does not write."""
    if not records:
        return "no applied module scaffolds\n"
    lines = ["applied module scaffolds:"]
    for item in records:
        lines.append(
            f"- {item.get('module_id')} -> {item.get('destination')}/ "
            f"({item.get('at')}, {len(item.get('files') or [])} files)")
    return "\n".join(lines) + "\n"
