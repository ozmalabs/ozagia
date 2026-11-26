"""``ozagia-ocelot-workspace``: query an indexed Ocelot checkout.

Commands:

    status  [--root PATH] [--json]     engine/worktree readiness (no graph)
    doctor  [--root PATH] [--json]     alias for ``status``
    seed ID [--root PATH] [--include summary|closure|tests|docs|all]
            [--json]                    one seed and its related files
    seeds   [--root PATH] [--json]     seed census
    plan    [--root PATH] [--changed PATH ...] [--base REF --head REF]
            [--seed ID ...] [--json]    an affected-test or full-gate plan

``status`` reads the checkout directly and needs no graph.  ``seed``, ``seeds``
and ``plan`` load ``<root>/.ozagia/graph.json`` and
``<root>/.ozagia/journal.jsonl`` through ``DevMemory``.  When the graph is
missing the CLI prints the exact ``ozagia index --root <root>`` command and
exits 2; it never builds the graph implicitly.

Exit codes:

* 0 - success; a plan is a success even when ``mode`` is ``full``;
* 1 - the result says ``ok: false``, or ``status`` reports ``blocked``;
* 2 - invalid arguments, or the graph is missing or unreadable.

JSON output is printed with sorted keys and two-space indentation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


def _root(value: str | None) -> Path:
    """The checkout to inspect (default: the current repository root)."""
    if value:
        return Path(value).expanduser().resolve()
    from ozagia.config import repo_root
    return repo_root()


# Thin wrappers keep the expensive imports out of ``--help`` and give tests a
# single, patchable seam for each shared-module entry point.
def status(root: Path, *, python: str | None = None) -> dict[str, Any]:
    """The workspace doctor's read-only status."""
    from .workspace_doctor import status as doctor_status
    return doctor_status(root, python=python)


def format_text(result: dict[str, Any]) -> str:
    """The workspace doctor's text rendering."""
    from .workspace_doctor import format_text as render
    return render(result)


def seed_summary(memory: Any, seed_id: str, include: str = "summary") -> dict[str, Any]:
    """One indexed seed and its related files."""
    from .workspace import seed_summary as query
    return query(memory, seed_id, include=include)


def seed_census(memory: Any) -> dict[str, Any]:
    """The indexed-seed census."""
    from .workspace import seed_census as query
    return query(memory)


def test_plan(memory: Any, changed: Iterable[str] | None = None, *,
              base: str | None = None, head: str | None = None,
              seeds: Iterable[str] | None = None) -> dict[str, Any]:
    """An affected-test plan or a conservative full-gate fallback."""
    from .workspace import test_plan as query
    return query(memory, changed=changed, base=base, head=head, seeds=seeds)


def research_summary(memory: Any, target: str = "") -> dict[str, Any]:
    """An Ocelot research chain, or the research census when target is empty."""
    from .workspace_research import research_census, research_summary as query
    return query(memory, target) if target else research_census(memory)


def engine_summary(memory: Any, target: str | None = None,
                   op: str = "primitive") -> dict[str, Any]:
    """The Ocelot Rust/Python/seed bridge query."""
    from .workspace_engine import engine_summary as query
    return query(memory, target=target, op=op)


def seed_validate(memory: Any, seed_id: str) -> dict[str, Any]:
    """Read-only structural validation of one Ocelot seed manifest."""
    from .workspace_validate import seed_validate as query
    return query(memory, seed_id)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _load_memory(root: Path) -> Any | None:
    """Load the graph snapshot, or print a clear error and return None."""
    from ozagia.config import state_path
    from ozagia.core.memory import DevMemory

    dump = state_path(root, "graph.json")
    journal = state_path(root, "journal.jsonl")
    retry = f"run `ozagia index --root {root}` first"
    if not dump.is_file():
        print(f"error: no graph snapshot at {dump}; {retry}", file=sys.stderr)
        return None
    try:
        return DevMemory(dump, journal, root=root)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read graph snapshot at {dump}: {exc}; {retry}",
              file=sys.stderr)
        return None


def _result_ok(result: Any) -> bool:
    return not (isinstance(result, dict) and result.get("ok") is False)


def _status_exit(result: Any) -> int:
    if not _result_ok(result):
        return 1
    if isinstance(result, dict) and str(result.get("state") or "").lower() == "blocked":
        return 1
    return 0


def _plan_exit(result: Any) -> int:
    """A produced plan is a success, including a full-gate plan."""
    if not _result_ok(result):
        return 1
    if isinstance(result, dict) and result.get("blocked"):
        return 1
    return 0


def _one_line(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def _capped(items: Iterable[Any], limit: int = 20) -> list[Any]:
    values = list(items)
    if len(values) <= limit:
        return values
    return values[:limit] + [f"and {len(values) - limit} more"]


def _format_seed(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        lines = [f"error: {result.get('error') or 'seed query failed'}"]
        known = result.get("seeds") or []
        if known:
            lines.append("indexed seeds: " + _one_line(_capped(known)))
        return "\n".join(lines)
    seed_id = result.get("id") or result.get("seed") or "?"
    version = result.get("version")
    lines = [f"seed: {seed_id}" + (f" (version {version})" if version else "")]
    if result.get("path"):
        lines.append(f"path: {result['path']}")
    if result.get("area"):
        lines.append(f"area: {result['area']}")
    if result.get("description"):
        lines.append(f"description: {result['description']}")
    if result.get("blast_radius"):
        lines.append(
            f"blast radius: {result['blast_radius']} "
            f"(fan-out {result.get('fanout', 0)}, "
            f"shared={'yes' if result.get('shared') else 'no'})")
    if result.get("self_load_only"):
        lines.append("self load only: yes")
    for key, label in (("depends_on", "depends on"), ("dependents", "dependents"),
                       ("dependencies", "dependencies")):
        if key in result:
            lines.append(f"{label} ({len(result.get(key) or [])}): "
                         + _one_line(result.get(key) or []))
    for key, label in (("closure", "closure"), ("reverse_closure", "reverse closure")):
        if key in result:
            lines.append(f"{label} ({len(result.get(key) or [])}): "
                         + _one_line(result.get(key) or []))
    tests = result.get("tests")
    if tests is not None:
        lines.append(f"tests ({len(tests)}):")
        for item in tests:
            if isinstance(item, dict):
                file = item.get("file") or "?"
                lines.append(f"  {file}")
                for name in item.get("tests") or []:
                    lines.append(f"    {name}")
            else:
                lines.append(f"  {item}")
        if not tests:
            lines[-1] = "tests (0): -"
    for key, label in (("docs", "docs"), ("sources", "sources"),
                       ("boot_files", "boot files"), ("terms", "terms")):
        if key in result:
            lines.append(f"{label} ({len(result.get(key) or [])}): "
                         + _one_line(result.get(key) or []))
    counts = result.get("counts")
    if counts:
        lines.append("counts: " + ", ".join(
            f"{key}={value}" for key, value in sorted(counts.items())))
    if result.get("exact_test"):
        lines.append(f"exact test: {result['exact_test']}")
    return "\n".join(lines)


def _format_census(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        return f"error: {result.get('error') or 'seed census failed'}"
    lines = [f"seed census: {result.get('seed_count', 0)} seed(s), "
             f"{result.get('stub_count', 0)} stub(s)"]
    shared = result.get("shared") or []
    lines.append(f"shared ({len(shared)}): " + _one_line(_capped(shared)))
    no_test = result.get("no_test") or []
    lines.append(f"no test ({len(no_test)}): " + _one_line(_capped(no_test)))
    no_boot = result.get("no_boot") or []
    lines.append(f"no boot ({len(no_boot)}): " + _one_line(_capped(no_boot)))
    cycles = result.get("cycles") or []
    cycle_lines = [" -> ".join(cycle) for cycle in cycles]
    lines.append(f"cycles ({len(cycles)}): " + (
        _one_line(_capped(cycle_lines)) if cycles else "-"))
    stubs = result.get("stubs") or []
    if stubs:
        lines.append(f"stubs ({len(stubs)}): " + _one_line(_capped(stubs)))
    partial = result.get("partial") or []
    if partial:
        lines.append(f"partial manifests ({len(partial)}): "
                     + _one_line(_capped(partial)))
    errors = result.get("errors") or []
    if errors:
        lines.append(f"index errors ({len(errors)}):")
        for error in _capped(errors):
            lines.append(f"  {error}")
    return "\n".join(lines)


def _format_validate(result: dict[str, Any]) -> str:
    seed_id = result.get("seed") or result.get("id") or "?"
    lines = [f"seed validation: {seed_id} "
             f"({'ok' if result.get('ok') else 'failed'})"]
    path = result.get("path")
    if path:
        lines.append(f"path: {path}")
    errors = result.get("errors") or []
    warnings = result.get("warnings") or []
    checks = result.get("checks") or []
    lines.append(f"checks: {len(checks)}; errors: {len(errors)}; "
                 f"warnings: {len(warnings)}")
    for label, values in (("error", errors), ("warning", warnings)):
        for value in _capped(values):
            if isinstance(value, dict):
                text = value.get("message") or value.get("name") or value
            else:
                text = value
            lines.append(f"  {label}: {text}")
    return "\n".join(lines)


def _format_engine(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        return f"error: {result.get('error') or 'engine query failed'}"
    lines: list[str] = []
    if "defined_terms" in result:
        lines.append(
            f"term census: {result.get('defined_count', 0)} defined, "
            f"{result.get('used_count', 0)} used, "
            f"{result.get('unknown_count', 0)} unknown, "
            f"{result.get('unused_count', 0)} unused")
        for key, label in (("unknown_terms", "unknown"),
                           ("top_terms", "top terms"),
                           ("unused_terms", "unused")):
            values = result.get(key) or []
            if values:
                shown = [
                    item.get("name") if isinstance(item, dict) else item
                    for item in _capped(values)]
                lines.append(f"{label} ({len(values)}): " + _one_line(shown))
        errors = result.get("errors") or []
        if errors:
            lines.append("errors: " + _one_line(_capped(errors)))
        return "\n".join(lines)
    target = result.get("target") or result.get("primitive") or ""
    lines.append(f"engine: {target or 'primitives'}")
    if result.get("engine_rebuild"):
        lines.append("engine rebuild: required")
    primitives = result.get("primitives") or []
    if primitives and isinstance(primitives[0], dict):
        lines.append(f"primitives ({len(primitives)}):")
        for item in primitives:
            lines.append(
                f"  {item.get('name', '?')} "
                f"({item.get('kind', '?')}, "
                f"defined={'yes' if item.get('defined') else 'no'})")
    elif primitives:
        lines.append(f"primitives ({len(primitives)}): " + _one_line(primitives))
    definitions = result.get("definitions") or []
    if definitions:
        lines.append("definitions: " + _one_line(_capped(definitions)))
    for key, label in (("python_files", "python files"), ("tests", "tests"),
                       ("test_files", "test files"), ("seeds", "seeds"),
                       ("terms", "terms"), ("resources", "resources")):
        values = result.get(key) or []
        if values:
            lines.append(f"{label} ({len(values)}): " + _one_line(_capped(values)))
    return "\n".join(lines)


def _format_research(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        return f"error: {result.get('error') or 'research query failed'}"
    if "docs" in result and "chain" not in result:
        lines = [f"research census: {result.get('docs', 0)} document(s)"]
        kinds = result.get("by_kind") or {}
        if kinds:
            lines.append("by kind: " + ", ".join(
                f"{key}={value}" for key, value in sorted(kinds.items())))
        for key, label in (
                ("prereg_without_results", "prereg without results"),
                ("results_without_prereg", "results without prereg"),
                ("missing_gates", "missing gate tests")):
            values = result.get(key) or []
            if values:
                lines.append(f"{label} ({len(values)}): " + _one_line(
                    _capped(values)))
        errors = result.get("errors") or []
        if errors:
            lines.append("errors: " + _one_line(_capped(errors)))
        return "\n".join(lines)
    target = result.get("target") or ""
    lines = [f"research: {target}"]
    nodes = result.get("nodes") or []
    if nodes:
        lines.append(f"documents ({len(nodes)}):")
        for node in nodes:
            if isinstance(node, dict):
                lines.append(
                    f"  {node.get('kind', '?')}: {node.get('path', '?')}")
            else:
                lines.append(f"  {node}")
    for key, label in (("scripts", "scripts"), ("tests", "tests"),
                       ("seeds", "seeds"), ("engine_hashes", "engine hashes"),
                       ("commit_pins", "commit pins")):
        values = result.get(key) or []
        if values:
            lines.append(f"{label} ({len(values)}): " + _one_line(_capped(values)))
    if result.get("missing_gate"):
        lines.append("missing gate: yes")
    if result.get("stale"):
        lines.append("stale: yes")
    for reason in _capped(result.get("reasons") or []):
        lines.append(f"  - {reason}")
    return "\n".join(lines)


def _format_plan(result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        return f"error: {result.get('error') or 'test plan failed'}"
    mode = result.get("mode") or "none"
    count = result.get("test_count")
    if count is None:
        count = len(result.get("tests") or [])
    lines = [f"test plan: {mode} ({count} test file(s))"]
    if result.get("engine_rebuild"):
        lines.append("engine rebuild: required")
    if result.get("full_gate"):
        lines.append("full gate: yes")
    changed = result.get("changed") or []
    if changed:
        lines.append(f"changed ({len(changed)}): " + _one_line(changed))
    seeds = result.get("seeds") or []
    if seeds:
        lines.append("seeds:")
        for seed in seeds:
            if isinstance(seed, dict):
                lines.append(
                    f"  {seed.get('id', '?')} "
                    f"({seed.get('blast_radius', '?')}, "
                    f"shared={'yes' if seed.get('shared') else 'no'})")
            else:
                lines.append(f"  {seed}")
    commands = result.get("commands") or []
    if commands:
        lines.append("commands:")
        lines.extend(f"  {command}" for command in commands)
    if result.get("full_command"):
        lines.append(f"full-gate fallback: {result['full_command']}")
    for key, label in (("reasons", "reasons"), ("full_reasons", "full-gate reasons"),
                       ("warnings", "warnings")):
        values = result.get(key) or []
        if values:
            lines.append(f"{label}:")
            lines.extend(f"  - {value}" for value in values)
    tests = result.get("tests") or []
    if tests:
        lines.append(f"tests ({len(tests)}):")
        lines.extend(f"  {test}" for test in tests)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-ocelot-workspace",
        description="query an indexed Ocelot checkout: seed impact, a scoped "
                    "test plan, the Rust/Python/seed bridge, research links, "
                    "seed validation, and a read-only engine/worktree doctor")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--root", default=None,
                             help="Ocelot checkout (default: current repository)")
        command.add_argument("--json", action="store_true",
                             help="print JSON instead of text")
        return command

    common("status", "engine, worktree and dataset readiness (no graph needed)")
    common("doctor", "alias for status")

    seed = common("seed", "one seed with dependencies, tests and docs")
    seed.add_argument("id", metavar="ID", help="seed id")
    seed.add_argument("--include", default="summary",
                      choices=("summary", "closure", "tests", "docs",
                               "sources", "terms", "all"),
                      help="detail level (default: summary)")

    common("seeds", "census of indexed seeds and their test/boot coverage")

    research = common("research", "research/evidence chain or research census")
    research.add_argument("target", nargs="?", default="",
                          metavar="TARGET",
                          help="document path, block number or topic substring")

    engine = common("engine", "Rust/Python/seed bridge query")
    engine.add_argument("--op", default="primitive",
                        choices=("primitive", "rust_impact", "term_census"),
                        help="query kind (default: primitive)")
    engine.add_argument("--target", default=None,
                        help="primitive name or Rust path for primitive/impact")

    validate = common("validate", "structural seed-manifest validation")
    validate.add_argument("id", metavar="ID", help="seed id")

    plan = common("plan", "affected-test plan or conservative full-gate fallback")
    plan.add_argument("--changed", action="append", nargs="+", default=[],
                      metavar="PATH",
                      help="changed path, relative to the checkout (repeatable)")
    plan.add_argument("--base", default=None, metavar="REF",
                      help="git base revision (default with --head: HEAD)")
    plan.add_argument("--head", default=None, metavar="REF",
                      help="git head revision (default with --base: HEAD)")
    plan.add_argument("--seed", action="append", nargs="+", default=[],
                      metavar="ID", help="changed seed id (repeatable)")
    return parser


def _flatten(groups: list[list[str]]) -> list[str]:
    return [value for group in groups for value in group if value]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)

    if args.command in ("status", "doctor"):
        result = status(root)
        if args.json:
            _print_json(result)
        else:
            print(format_text(result))
        return _status_exit(result)

    memory = _load_memory(root)
    if memory is None:
        return 2

    if args.command == "seed":
        result = seed_summary(memory, args.id, include=args.include)
        if args.json:
            _print_json(result)
        else:
            print(_format_seed(result))
        return 0 if _result_ok(result) else 1

    if args.command == "seeds":
        result = seed_census(memory)
        if args.json:
            _print_json(result)
        else:
            print(_format_census(result))
        return 0 if _result_ok(result) else 1

    if args.command == "research":
        result = research_summary(memory, args.target)
        if args.json:
            _print_json(result)
        else:
            print(_format_research(result))
        return 0 if _result_ok(result) else 1

    if args.command == "engine":
        result = engine_summary(memory, target=args.target, op=args.op)
        if args.json:
            _print_json(result)
        else:
            print(_format_engine(result))
        return 0 if _result_ok(result) else 1

    if args.command == "validate":
        result = seed_validate(memory, args.id)
        if args.json:
            _print_json(result)
        else:
            print(_format_validate(result))
        return 0 if _result_ok(result) else 1

    if args.command == "plan":
        changed = _flatten(args.changed)
        seeds = _flatten(args.seed)
        result = test_plan(memory, changed=changed or None,
                           base=args.base, head=args.head,
                           seeds=seeds or None)
        if args.json:
            _print_json(result)
        else:
            print(_format_plan(result))
        return _plan_exit(result)

    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
