#!/usr/bin/env python3
"""Generate ``docs/reference/`` from the code.

The reference is a rendering of what the code declares, so it cannot drift:

* ``[project.scripts]`` in ``pyproject.toml`` — the command inventory;
* each CLI module's ``build_parser()`` argparse tree — the commands,
  subcommands, flags, types, defaults and choices;
* the MCP tool lists (``integrations/mcp/server.py``, ``graph_surface.py``,
  ``sre.py`` and ``research/mcp.py``) — the tools, per server mode;
* the typed settings registry (``core/settings.py``) and the resolved
  ``.ozagia.toml`` model (``config.py``) — the configuration reference.

Usage:
    python scripts/docs/generate_docs.py            # (re)write docs/reference/
    python scripts/docs/generate_docs.py --check    # exit 1 when stale (CI)

Standard library only, per the core's dependency rule. The generated pages
are committed to the repository; do not edit them by hand — change the
source and regenerate.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import importlib
import inspect
import json
import re
import sys
import tomllib
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "docs" / "reference"
SUPPRESS = argparse.SUPPRESS
# Committed pages must not depend on the terminal that generated them.
USAGE_WIDTH = 80
# Stable stand-in for this checkout's root in rendered defaults.
REPO_PLACEHOLDER = "<repo>"
# Commands whose page opens with the module docstring (the canonical
# synopsis) in full, not just its first line.
FULL_DOC_INTROS = frozenset({"ozagia"})


class _FixedWidthHelpFormatter(argparse.HelpFormatter):
    """Usage formatter pinned to ``USAGE_WIDTH``, ignoring ``COLUMNS``."""

    def __init__(self, prog: str, indent_increment: int = 2,
                 max_help_position: int = 24,
                 width: int | None = USAGE_WIDTH):
        super().__init__(prog, indent_increment, max_help_position, width)


# -- provenance ------------------------------------------------------------- #

def _version() -> str:
    # The package version straight from ``[project] version`` in pyproject
    # (not the install metadata, which can lag an editable install): stable
    # across a branch, changing only on a release bump, so regenerated pages
    # are byte-identical and the `--check` gate has no self-referential input.
    try:
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        return data["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def header(title: str, sources: list[str]) -> list[str]:
    return [
        f"# {title}",
        "",
        "> **Generated file.** Built by `scripts/docs/generate_docs.py` from "
        + ", ".join(f"`{s}`" for s in sources)
        + f" (ozagia `{_version()}`). Do not edit by hand: change the source "
          "code and regenerate.",
        "",
    ]


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# -- argparse tree ------------------------------------------------------------ #

def _type_name(action: argparse.Action) -> str:
    t = action.type
    if t is None:
        return ""
    if t in (int, float, str, bool):
        return t.__name__
    name = getattr(t, "__name__", str(t))
    mod = getattr(t, "__module__", "")
    if mod in ("builtins", "__main__"):
        return name
    return f"{mod}.{name}"


# The "one optional value" marker: a private sentinel before 3.13, the
# integer 0 from 3.13 on (`argparse.CONST` was removed).
_CONST = getattr(argparse, "CONST", None)


def _nargs_text(nargs: object) -> str:
    if nargs is None:
        return ""
    if nargs == 0 or (_CONST is not None and nargs is _CONST):
        return "optional value"
    if nargs == argparse.ONE_OR_MORE:
        return "one or more"
    if nargs == argparse.ZERO_OR_MORE:
        return "zero or more"
    if nargs == argparse.REMAINDER:
        return "the remainder"
    if nargs == argparse.PARSER:
        return "a subparser"
    return str(nargs)  # "append", "extend", a count, ...


def _normalize_repo_path(text: str) -> str:
    """Replace this checkout's root with ``REPO_PLACEHOLDER``.

    The pages are committed and read on other machines, so a default that
    names the checkout must not embed the generating machine's path. Only
    this checkout's root (and paths under it) is replaced; unrelated
    absolute defaults such as ``/tmp`` stay as they are.
    """
    root = str(REPO)
    if root in ("", "/"):
        return text
    return re.sub(re.escape(root) + r"(?=$|[\\/])", REPO_PLACEHOLDER, text)


def _default_text(action: argparse.Action) -> str:
    d = action.default
    if d is SUPPRESS or d is None:
        return ""
    if type(action).__name__ in ("_StoreTrueAction", "_StoreFalseAction",
                                 "_CountAction"):
        return ""
    if isinstance(d, str):
        return f"`{_normalize_repo_path(d)}`"
    return _normalize_repo_path(json.dumps(d, ensure_ascii=False))


def _choices_text(action: argparse.Action) -> str:
    c = action.choices
    if c is None or isinstance(c, dict):
        return ""
    return ", ".join(f"`{x}`" for x in c)


_FLAG_ACTIONS = frozenset({"_StoreTrueAction", "_StoreFalseAction",
                            "_CountAction"})


def describe_option(action: argparse.Action) -> dict:
    flag = type(action).__name__ in _FLAG_ACTIONS
    opts = action.option_strings
    return {
        "name": ", ".join(f"`{o}`" for o in opts),
        "type": _type_name(action),
        # store_true & co. take no value (in 3.13+ their nargs is 0, which
        # would otherwise render as "optional value").
        "nargs": "" if flag else _nargs_text(action.nargs),
        "default": _default_text(action),
        "choices": _choices_text(action),
        "required": bool(action.required),
        "help": md_escape(action.help or ""),
    }


def describe_positional(action: argparse.Action) -> dict:
    return {
        "name": f"`{action.metavar or action.dest}`",
        "type": _type_name(action),
        "nargs": _nargs_text(action.nargs),
        "choices": _choices_text(action),
        "required": action.nargs is None or (
            isinstance(action.nargs, int) and action.nargs > 0)
        or action.nargs in (argparse.ONE_OR_MORE, argparse.REMAINDER, argparse.PARSER),
        "help": md_escape(action.help or ""),
    }


def walk_parser(parser: argparse.ArgumentParser) -> dict:
    original_formatter = parser.formatter_class
    parser.formatter_class = _FixedWidthHelpFormatter
    try:
        usage = parser.format_usage().strip()
    finally:
        parser.formatter_class = original_formatter
    node = {
        "prog": parser.prog,
        "description": parser.description,
        "usage": usage,
        "positionals": [],
        "options": [],
        "subcommands": [],
    }
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Choice help lives on pseudo-actions, not on the subparsers.
            choice_help: dict[str, object] = {}
            for ca in getattr(action, "_choices_actions", []):
                for n in ca.option_strings:  # name, then any aliases
                    choice_help.setdefault(n, ca.help)
            for name, sub in action.choices.items():
                if choice_help.get(name) == SUPPRESS:
                    continue  # hidden entry point (e.g. `profile-run`)
                child = walk_parser(sub)
                child["name"] = name
                child["help"] = choice_help.get(name)
                child["required"] = action.required
                node["subcommands"].append(child)
            continue
        if action.option_strings == ["-h", "--help"]:
            continue
        if action.option_strings:
            node["options"].append(describe_option(action))
        else:
            node["positionals"].append(describe_positional(action))
    return node


def is_stub(node: dict) -> bool:
    """A subparser with no arguments of its own: the CLI delegates every
    argument to another module's `main` (see `DELEGATES` in ozagia/cli.py)."""
    return not (node["options"] or node["positionals"] or node["subcommands"])


def delegation_targets(cli_module) -> dict[str, str]:
    """Delegate name -> target module, read from the delegate functions'
    `from <module> import main` lines."""
    out: dict[str, str] = {}
    for name, (fn_name, _help) in cli_module.DELEGATES.items():
        try:
            src = inspect.getsource(getattr(cli_module, fn_name))
        except (OSError, TypeError):
            continue
        m = re.search(r"from\s+([A-Za-z_][\w.]*)\s+import\s+main", src)
        if m:
            out[name] = m.group(1)
    return out


# -- CLI pages ---------------------------------------------------------------- #

def option_table(options: list[dict]) -> list[str]:
    lines = [
        "| option | type | values | default | help |",
        "|---|---|---|---|---|",
    ]
    for o in options:
        lines.append(
            f"| {o['name']} | {o['type']} | {o['nargs']} | {o['default']} "
            f"| {o['help'] or '—'} |")
    return lines


def positional_table(positionals: list[dict]) -> list[str]:
    lines = [
        "| argument | type | values | required | help |",
        "|---|---|---|---|---|",
    ]
    for p in positionals:
        lines.append(
            f"| {p['name']} | {p['type']} | {p['nargs']} "
            f"| {'yes' if p['required'] else 'no'} | {p['help'] or '—'} |")
    return lines


def render_node(lines: list[str], prefix: str, node: dict, depth: int,
                delegations: dict[str, str],
                module_to_page: dict[str, str]) -> None:
    name = prefix if depth == 0 else f"{prefix} {node['name']}"
    lines.append(f"{'#' * (1 + depth)} `{name}`")
    lines.append("")
    intro = node.get("description") if depth == 0 else node.get("help")
    if intro:
        lines.append(md_escape(intro) + "\n")
    if is_stub(node) and depth >= 1:
        target = delegations.get(node["name"])
        if target:
            lines.append(f"Delegates every argument to `python -m {target}` "
                         "(its own `--help` shows the flags).")
            page = module_to_page.get(target)
            if page:
                lines.append(f"Reference: [{page}](cli/{page}.md).")
        else:
            lines.append("Takes no arguments of its own in this parser.")
        lines.append("")
        return
    if node["usage"] and node["usage"].lower().startswith("usage:"):
        lines.append("```")
        lines.append(node["usage"])
        lines.append("```")
        lines.append("")
    if node["positionals"]:
        lines.extend(positional_table(node["positionals"]))
        lines.append("")
    if node["options"]:
        lines.extend(option_table(node["options"]))
        lines.append("")
    for sub in node["subcommands"]:
        render_node(lines, name, sub, depth + 1, delegations, module_to_page)


def render_cli_page(command: str, module, module_path: str,
                    delegations: dict[str, str],
                    module_to_page: dict[str, str]) -> str:
    build_parser = getattr(module, "build_parser", None)
    doc = (module.__doc__ or "").strip()
    if build_parser is None:
        # No argparse tree: the module docstring carries the command synopsis.
        lines = header(f"`{command}`", [module_path])
        lines.append("This command's module does not expose a `build_parser()`;")
        lines.append("its usage synopsis comes from the module docstring:")
        lines.append("")
        lines.append("```")
        lines.append(doc)
        lines.append("```")
        lines.append("")
        return "\n".join(lines)

    parser = build_parser()
    parser.prog = command
    node = walk_parser(parser)
    lines = header(f"`{command}`", [module_path])
    if command in FULL_DOC_INTROS and doc:
        lines.append("Synopsis from the module docstring:")
        lines.append("")
        lines.append("```")
        lines.append(doc)
        lines.append("```")
        lines.append("")
    lines.append(f"Source: `{module_path}` (`{command}` entry point).")
    lines.append("")
    render_node(lines, command, node, 0, delegations, module_to_page)
    return "\n".join(lines)


# -- MCP pages ---------------------------------------------------------------- #

def render_tool(lines: list[str], tool: dict) -> None:
    lines.append(f"### `{tool['name']}`")
    lines.append("")
    lines.append(md_escape(tool.get("description", "").strip()) + "\n")
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if props:
        lines.append("| parameter | type | required | default | description |")
        lines.append("|---|---|---|---|---|")
        for pname, spec in props.items():
            default = spec.get("default")
            default = "" if default is None else json.dumps(
                default, ensure_ascii=False)
            lines.append(
                f"| `{pname}` | {spec.get('type', '')} "
                f"| {'yes' if pname in required else ''} | {default} "
                f"| {md_escape(spec.get('description', '')) or '—'} |")
        for pname, spec in props.items():  # one level of nesting
            if spec.get("type") == "object" and spec.get("properties"):
                lines.append("")
                lines.append(f"Fields of `{pname}`:")
                lines.append("")
                for n2, s2 in spec["properties"].items():
                    lines.append(
                        f"- `{n2}` ({s2.get('type', '')}) — "
                        f"{md_escape(s2.get('description', '')) or '—'}")
                lines.append("")
    elif schema:
        lines.append(f"Input schema: `{json.dumps(schema, ensure_ascii=False)}`")
        lines.append("")


def _bridge_spec() -> dict | None:
    """The combined-mode `research_dev_context` bridge spec, lifted from the
    literal dict in `_load_research_dev_context` (best effort)."""
    try:
        from ozagia.integrations.mcp import server
        src = inspect.getsource(
            server.DevMemoryServer._load_research_dev_context)
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name) and t.id == "spec" \
                        and isinstance(node.value, ast.Dict):
                    return ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError):
        pass
    return None


def mcp_surface_tools() -> dict[str, list[dict]]:
    from ozagia.integrations.mcp import graph_surface, server, sre
    from ozagia.research import fallback, mcp as research_mcp

    sre_stub = types.SimpleNamespace(raw_repo_root=REPO)
    sre_list = [spec for spec, _handler in sre.sre_tools(sre_stub)]
    fetch = [fallback.TOOL_SPEC]

    coding = list(server._TOOLS) + sre_list + fetch
    research = list(research_mcp.research_tools()) + fetch
    combined = coding + list(research_mcp.research_tools())
    bridge = _bridge_spec()
    if bridge is not None:
        combined.append(bridge)
    graph = (list(graph_surface.GRAPH_TOOLS)
             + [t for t in server._TOOLS if t["name"].startswith("federation_")])
    return {"coding": coding, "research": research,
            "combined": combined, "graph": graph}


def render_mcp_page(surface: str, tools: list[dict],
                    sources: list[str], notes: list[str]) -> str:
    lines = header(f"MCP tools — {surface} surface", sources)
    lines.append(f"{len(tools)} tools.")
    lines.append("")
    for note in notes:
        lines.append(f"> {note}")
        lines.append("")
    for tool in sorted(tools, key=lambda t: t["name"]):
        render_tool(lines, tool)
    return "\n".join(lines)


def render_mcp_index(counts: dict[str, int], sources: list[str]) -> str:
    lines = header("MCP tools", sources)
    lines.append("The MCP server (`ozagia mcp` / `ozagia-mcp`) exposes the graph "
                 "as tools for any MCP client. The tool set depends on the mode "
                 "(`--mode coding|research|combined`, default `coding`) and, for "
                 "coding, the surface (`--surface full|graph`, default `full`).")
    lines.append("")
    lines.append("| surface | tools | page |")
    lines.append("|---|---|---|")
    rows = [
        ("coding (default)", "coding"),
        ("research", "research"),
        ("combined", "combined"),
        ("graph (coding/combined with `--surface graph`)", "graph"),
    ]
    for label, key in rows:
        lines.append(f"| {label} | {counts[key]} | [{key}.md]({key}.md) |")
    lines.append("")
    lines.append("A project's `[adapters]` configuration can add further tools "
                 "at runtime; those are not part of the built-in surface.")
    lines.append("")
    return "\n".join(lines)


# -- config page --------------------------------------------------------------- #

def render_config_page() -> str:
    from ozagia import config as C
    from ozagia.core import settings as S

    sources = ["src/ozagia/core/settings.py", "src/ozagia/config.py"]
    items = S.schema()
    lines = header("Configuration", sources)
    lines.append("Two layers: the typed **settings registry** (read with "
                 "`ozagia config schema`, written with `ozagia config set`) "
                 "and the **project file** (`.ozagia.toml` at the repository "
                 "root, resolved into `ozagia.config.ProjectConfig`).")
    lines.append("")

    lines.append(f"## Settings registry ({len(items)} keys)")
    lines.append("")
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["group"], []).append(item)
    for group, rows in groups.items():
        lines.append(f"### `{group}`")
        lines.append("")
        lines.append("| key | type | default | scope | env | validation | description |")
        lines.append("|---|---|---|---|---|---|---|")
        for it in rows:
            default = it["default"]
            default = "" if default is None else json.dumps(
                default, ensure_ascii=False)
            lines.append(
                f"| `{it['key']}` | {it['type']} | {default} | {it['scope']} "
                f"| {it.get('env') or ''} | {md_escape(it['validation'])} "
                f"| {md_escape(it['description'])} |")
        lines.append("")

    fields = dataclasses.fields(C.ProjectConfig)
    lines.append(f"## `.ozagia.toml` — resolved model ({len(fields)} fields)")
    lines.append("")
    lines.append("Fields of `ozagia.config.ProjectConfig` after resolution "
                 "(precedence, legacy migration and registry mapping applied).")
    lines.append("")
    lines.append("| field | type | default |")
    lines.append("|---|---|---|")
    for f in fields:
        if f.default is not dataclasses.MISSING:
            default = repr(f.default)
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[union-attr]
            default = f"factory: {f.default_factory.__name__}"  # type: ignore[union-attr]
        else:
            default = "—"
        lines.append(f"| `{f.name}` | `{f.type}` | {md_escape(default)} |")
    lines.append("")
    return "\n".join(lines)


# -- index --------------------------------------------------------------------- #

def render_index_page(entries: list[tuple[str, str, str]],
                      mcp_counts: dict[str, int]) -> str:
    lines = header("ozagia reference", ["pyproject.toml", "src/ozagia/"])
    lines.append("Generated from the code by `scripts/docs/generate_docs.py`: "
                 "the command inventory from `[project.scripts]`, the command "
                 "pages from each module's argparse tree, the MCP pages from "
                 "the server's tool lists, and the configuration page from the "
                 "settings registry. Regenerate after changing any of them.")
    lines.append("")
    lines.append(f"## Commands ({len(entries)})")
    lines.append("")
    lines.append("| command | what it does | reference |")
    lines.append("|---|---|---|")
    for command, blurb, _page in entries:
        lines.append(f"| `{command}` | {md_escape(blurb)} "
                     f"| [cli/{command}.md](cli/{command}.md) |")
    lines.append("")
    lines.append("## MCP tools")
    lines.append("")
    lines.append("| surface | tools | reference |")
    lines.append("|---|---|---|")
    for label, key in (("coding (default)", "coding"), ("research", "research"),
                       ("combined", "combined"),
                       ("graph (`--surface graph`)", "graph")):
        lines.append(f"| {label} | {mcp_counts[key]} "
                     f"| [mcp/{key}.md](mcp/{key}.md) |")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append("[config.md](config.md) — the settings registry and the "
                 "`.ozagia.toml` model.")
    lines.append("")
    return "\n".join(lines)


# -- build ---------------------------------------------------------------------- #

def read_entry_points() -> list[tuple[str, str, str]]:
    """(command, 'module:fn', module) in pyproject order."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    out = []
    for command, target in scripts.items():
        module_name, _, _fn = target.partition(":")
        out.append((command, target, module_name))
    return out


def build_all() -> dict[str, str]:
    pages: dict[str, str] = {}
    entries = read_entry_points()

    # module -> page, for delegation cross-links
    module_to_page: dict[str, str] = {}
    for command, _target, module_name in entries:
        module_to_page.setdefault(module_name, command)

    one_liners: dict[str, str] = {}
    for command, _target, module_name in entries:
        module = importlib.import_module(module_name)
        module_path = f"src/{module_name.replace('.', '/')}.py"
        one_liner = ""
        build_parser = getattr(module, "build_parser", None)
        if build_parser is not None:
            try:
                one_liner = build_parser().description or ""
            except Exception:  # noqa: BLE001 - a broken parser is reported below
                one_liner = ""
        if not one_liner and module.__doc__:
            one_liner = module.__doc__.strip().splitlines()[0]
        one_liners[command] = one_liner

        if module_name == "ozagia.cli":
            delegations = delegation_targets(module)
        else:
            delegations = {}
        pages[f"cli/{command}.md"] = render_cli_page(
            command, module, module_path, delegations, module_to_page)

    surfaces = mcp_surface_tools()
    mcp_sources = [
        "src/ozagia/integrations/mcp/server.py",
        "src/ozagia/integrations/mcp/graph_surface.py",
        "src/ozagia/integrations/mcp/sre.py",
        "src/ozagia/research/mcp.py",
    ]
    conditional = ""
    try:
        from ozagia.integrations.mcp import server as _server
        if _server._TOOL_NODE_TYPES:
            bits = [f"`{n}` (only when the snapshot has {t} nodes)"
                    for n, t in sorted(_server._TOOL_NODE_TYPES.items())]
            conditional = ("Conditionally listed: " + ", ".join(bits) + ".")
    except Exception:  # noqa: BLE001
        pass
    mcp_notes = {
        "coding": [
            "Built-in tools from `server._TOOLS`, plus the offline SRE/IaC "
            "tools (`sre.py`) and `research_fetch`.",
            conditional,
            "A project's `[adapters]` configuration can add tools at runtime.",
        ],
        "research": [
            "Tools from `research/mcp.py.research_tools()`, plus "
            "`research_fetch`.",
        ],
        "combined": [
            "The coding and research sets together, plus the "
            "`research_dev_context` bridge tool.",
            conditional,
        ],
        "graph": [
            "The compact graph-verb surface (`graph_surface.py`) plus the "
            "federation tools; the OpenCode capture plugin uses it.",
        ],
    }
    for surface, tools in surfaces.items():
        pages[f"mcp/{surface}.md"] = render_mcp_page(
            surface, tools, mcp_sources,
            [n for n in mcp_notes[surface] if n])
    pages["mcp/index.md"] = render_mcp_index(
        {k: len(v) for k, v in surfaces.items()}, mcp_sources)

    pages["config.md"] = render_config_page()
    pages["index.md"] = render_index_page(
        [(c, b, p) for c, b, p in
         ((command, one_liners[command], f"cli/{command}.md")
          for command, _t, _m in entries)],
        {k: len(v) for k, v in surfaces.items()})
    return pages


# -- write / check --------------------------------------------------------------- #

def write_pages(out: Path, pages: dict[str, str]) -> None:
    for rel, text in sorted(pages.items()):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text.endswith("\n") else text + "\n",
                        encoding="utf-8")


def check_pages(out: Path, pages: dict[str, str]) -> list[str]:
    stale: list[str] = []
    for rel, text in sorted(pages.items()):
        text = text if text.endswith("\n") else text + "\n"
        path = out / rel
        if not path.exists():
            stale.append(f"{rel} (missing)")
        elif path.read_text(encoding="utf-8") != text:
            stale.append(rel)
    # Committed pages the generator no longer produces.
    managed = {"index.md", "config.md"}
    managed |= {p for p in pages if p.startswith(("cli/", "mcp/"))}
    for path in sorted(out.rglob("*.md")):
        rel = path.relative_to(out).as_posix()
        if rel not in managed:
            stale.append(f"{rel} (no longer generated)")
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="generate docs/reference/ from the code")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="output directory (default: docs/reference)")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when the committed pages are stale")
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()

    pages = build_all()
    if args.check:
        stale = check_pages(out, pages)
        if stale:
            print(f"docs/reference is stale ({len(stale)} file(s)); "
                  "run `python scripts/docs/generate_docs.py` and commit:",
                  file=sys.stderr)
            for rel in stale:
                print(f"  {rel}", file=sys.stderr)
            return 1
        print(f"docs/reference is up to date ({len(pages)} pages)")
        return 0
    write_pages(out, pages)
    print(f"wrote {len(pages)} pages to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
