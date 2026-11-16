"""Install or remove ozagia's Codex integration.

Three pieces, for a project (`scope="project"`, under `<workdir>/.codex/`
and `<workdir>/AGENTS.md`) or for the user (`scope="user"`, under
`$CODEX_HOME`, default `~/.codex/`):

- `hooks.json`: ozagia's command handlers for `SessionStart`, `PreToolUse`
  (Bash), `PostToolUse` (Bash, apply_patch), `PreCompact` and `SessionEnd`
  (both clear the read ledger). Other keys and handlers are
  kept. ozagia's handlers are those whose command ends with this package's
  `hook` subcommand; installing again replaces them.
- `config.toml`: the `[mcp_servers.ozagia]` table (the ozagia MCP server in
  coding mode). Only that table (and any `[mcp_servers.ozagia.*]`
  subtables) is added or replaced; every other byte of the file stays as it
  was, except that a table appended to a file whose last line has no
  newline needs one there. The file is parsed with `tomllib` before and
  after, and the edit is refused unless everything outside ozagia's table
  parses the same (an empty `[mcp_servers]` counts as no `mcp_servers`).
- `AGENTS.md`: a section between `<!-- ozagia:begin -->` and
  `<!-- ozagia:end -->` telling the agent which ozagia tools to prefer.

When install has to add that newline (to `config.toml` or `AGENTS.md`), it
says so in a marker line inside its own table or section (`NO_EOL_TOML`,
`NO_EOL_MD`), and uninstall takes the newline away again. So install and
then uninstall gives back the original bytes.

`uninstall` removes the three pieces and leaves the rest. A file that
holds nothing else afterwards is deleted.

Codex runs a non-managed hook only after the user has reviewed and trusted
it (`/hooks` in the Codex CLI), and loads project hooks only in a trusted
project; `TRUST_STEPS` says so.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from ozagia.integrations.codex.hook import HOOK_TIMEOUT_S

MODULE = "ozagia.integrations.codex"
HOOK_SUFFIX = f"-m {MODULE} hook"
SCOPES = ("project", "user")
MCP_NAME = "ozagia"
MCP_MODE = "coding"
MATCHERS: dict[str, str | None] = {
    "SessionStart": None,
    "PreToolUse": "^Bash$",
    "PostToolUse": "^(Bash|apply_patch)$",
    "PreCompact": None,
    "SessionEnd": None,
}
BEGIN = "<!-- ozagia:begin -->"
END = "<!-- ozagia:end -->"
# The marker lines for "install added a newline after the user's last line".
NO_EOL_TOML = "# (ozagia added the newline after the last line above; uninstall removes it)"
NO_EOL_MD = "<!-- ozagia: added the newline after the last line above -->"
AGENTS_SECTION = f"""{BEGIN}
## ozagia

This project has the ozagia code graph, served by the `ozagia` MCP server.
Prefer its tools to reading whole files and repeating greps:

- Read one symbol by key with `dev_symbol` or `dev_node`
  (`sym:path/to/file.py::name`) instead of reading the whole file.
- Get a file's outline with `dev_outline` before reading it.
- Check what depends on a module with `dev_impact` before changing it.
- Start a task with `dev_context`.

Shell commands and patches work as usual; ozagia adds graph context to their
results (node keys, failing tests, the graph delta after a patch). Git
commands that move refs (commit, push, checkout, reset, stash, ...) are
refused; leave them to the user.
{END}
"""
TRUST_STEPS = """\
Next steps:
  1. Codex runs these hooks only after you review and trust them: start Codex
     in this project and open /hooks to review the ozagia hooks. Codex skips
     new or changed hooks until they are trusted (also after every reinstall
     that changes them). Project hooks load only in a trusted project.
  2. The graph daemon starts on its own on the first hook call (a few seconds
     for a large repository; the first calls may come back without context)
     and stops after 30 minutes without requests. Start it early with
     `ozagia daemon ensure --root <repo>`."""


class SettingsError(ValueError):
    """A file exists but cannot be merged safely."""


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def paths(workdir: str | Path, scope: str = "project",
          home: str | Path | None = None) -> dict[str, Path]:
    """`{"hooks", "config", "agents"}` for `scope`."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {list(SCOPES)}, not {scope!r}")
    if scope == "project":
        base = Path(workdir).resolve()
        codex = base / ".codex"
        agents = base / "AGENTS.md"
    else:
        codex = Path(home) if home is not None else codex_home()
        agents = codex / "AGENTS.md"
    return {"hooks": codex / "hooks.json", "config": codex / "config.toml",
            "agents": agents}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    # newline="": keep CRLF line ends as they are.
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# hooks.json
# --------------------------------------------------------------------------- #
def hook_command(python: str = sys.executable) -> str:
    return f"{shlex.quote(python)} {HOOK_SUFFIX}"


def is_ozagia_handler(handler: Any) -> bool:
    return (isinstance(handler, dict) and handler.get("type") == "command"
            and isinstance(handler.get("command"), str)
            and handler["command"].rstrip().endswith(HOOK_SUFFIX))


def _load_hooks(path: Path) -> dict[str, Any]:
    text = _read(path)
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: not a JSON object")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise SettingsError(f"{path}: `hooks` is not an object")
    for event, groups in (hooks or {}).items():
        if not isinstance(groups, list):
            raise SettingsError(f"{path}: `hooks.{event}` is not a list")
    return data


def _dump_hooks(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _strip_hooks(data: dict[str, Any]) -> bool:
    """Remove ozagia's handlers from `data` in place. True if any were removed."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for event in list(hooks):
        kept_groups = []
        for group in hooks[event]:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [h for h in handlers if not is_ozagia_handler(h)]
            if len(kept) == len(handlers):
                kept_groups.append(group)
                continue
            removed = True
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if removed and not hooks:
        del data["hooks"]
    return removed


def merge_hooks(data: dict[str, Any], python: str) -> dict[str, Any]:
    _strip_hooks(data)
    hooks = data.setdefault("hooks", {})
    for event, matcher in MATCHERS.items():
        group: dict[str, Any] = {}
        if matcher is not None:
            group["matcher"] = matcher
        group["hooks"] = [{"type": "command", "command": hook_command(python),
                           "timeout": HOOK_TIMEOUT_S}]
        hooks.setdefault(event, []).append(group)
    return data


# --------------------------------------------------------------------------- #
# config.toml
# --------------------------------------------------------------------------- #
_HEADER = re.compile(r"""^\s*\[\[?\s*([A-Za-z0-9_\-."' ]+?)\s*\]\]?\s*(?:#.*)?$""")
_KEY_PART = re.compile(r"""\s*("(?:[^"\\]|\\.)*"|'[^']*'|[A-Za-z0-9_\-]+)\s*(?:\.|$)""")


def _header_parts(line: str) -> list[str] | None:
    """The key parts of a table header line, or None if it is not one."""
    m = _HEADER.match(line.rstrip("\r\n"))
    if not m:
        return None
    name, parts, pos = m.group(1), [], 0
    while pos < len(name):
        km = _KEY_PART.match(name, pos)
        if not km or km.end() == pos:
            return None
        part = km.group(1)
        if part[0] == '"':
            try:
                part = json.loads(part)
            except ValueError:
                return None
        elif part[0] == "'":
            part = part[1:-1]
        parts.append(part)
        pos = km.end()
    return parts


def _is_ours(parts: list[str] | None) -> bool:
    return parts is not None and parts[:2] == ["mcp_servers", MCP_NAME]


def _toml_str(value: str) -> str:
    # A JSON string with ASCII escapes is a TOML basic string.
    return json.dumps(value, ensure_ascii=True)


def mcp_entry(workdir: str | Path, scope: str, python: str) -> dict[str, Any]:
    """`{"command", "args", "env"}` for the ozagia MCP server. The project
    entry names the project root; the user entry has no root, so the server
    takes the repository of its working directory."""
    from ozagia.harness.opencode import mcp_server_entry
    root = Path(workdir).resolve()
    entry = mcp_server_entry(root, MCP_MODE, tool_python=python)
    cmd = list(entry["command"])
    env = dict(entry["environment"])
    if scope == "user":
        at = cmd.index("--root")
        del cmd[at:at + 2]
        env.pop("OZAGIA_ROOT", None)
    return {"command": cmd[0], "args": cmd[1:], "env": env}


def mcp_table(entry: dict[str, Any], no_eol: bool = False) -> str:
    args = ", ".join(_toml_str(a) for a in entry["args"])
    env = ", ".join(f"{k} = {_toml_str(v)}" for k, v in sorted(entry["env"].items()))
    return (f"[mcp_servers.{MCP_NAME}]\n"
            "# Added by `ozagia install codex`; removed by\n"
            "# `python -m ozagia.integrations.codex uninstall`.\n"
            + (NO_EOL_TOML + "\n" if no_eol else "") +
            f"command = {_toml_str(entry['command'])}\n"
            f"args = [{args}]\n"
            f"env = {{ {env} }}\n")


def _blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


def _our_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """`[start, end)` line ranges of ozagia's tables. A block ends before the
    next header, without the blank and comment lines just before it (they
    may belong to the next table)."""
    headers = [(i, _header_parts(x)) for i, x in enumerate(lines)]
    headers = [(i, p) for i, p in headers if p is not None]
    blocks = []
    for n, (i, parts) in enumerate(headers):
        if not _is_ours(parts):
            continue
        end = headers[n + 1][0] if n + 1 < len(headers) else len(lines)
        while end > i + 1 and _blank_or_comment(lines[end - 1]):
            end -= 1
        blocks.append((i, end))
    return blocks


def _parse_toml(path: Path, text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: not valid TOML ({exc})") from exc


def _without_ours(data: dict[str, Any]) -> dict[str, Any]:
    """`data` without `mcp_servers.ozagia`, and without `mcp_servers` when
    nothing else is in it (an explicit empty `[mcp_servers]` header and no
    header at all mean the same)."""
    out = dict(data)
    servers = out.get("mcp_servers")
    if isinstance(servers, dict):
        servers = {k: v for k, v in servers.items() if k != MCP_NAME}
        if servers:
            out["mcp_servers"] = servers
        else:
            del out["mcp_servers"]
    return out


def _isolate(path: Path, text: str) -> tuple[dict[str, Any], str, int | None,
                                               dict[str, Any]]:
    """`(parsed text, text without ozagia's tables, where they were, parsed
    rest as `_without_ours` gives it)`. Refuses, naming the line, when the line scanner cannot tell
    where ozagia's table ends: what is left must parse to the same settings
    as the file without `mcp_servers.ozagia`."""
    before = _parse_toml(path, text)
    stripped, at = _remove_blocks(text)
    lines = text.splitlines()
    blocks = _our_blocks(text.splitlines(keepends=True))

    def refuse(why: str) -> SettingsError:
        a, b = blocks[0]
        return SettingsError(
            f"{path}: ozagia could not isolate its [mcp_servers.{MCP_NAME}] table "
            f"(line {a + 1}: `{lines[a].strip()}`): {why}; edit the file by hand "
            "or move that table to the end of the file, and install again")

    try:
        rest = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError as exc:
        assert blocks  # the text itself parsed
        raise refuse("without the lines from there to the next table header the "
                     f"file is not valid TOML ({exc}); is that header inside a "
                     "multi-line string or array?") from None
    servers = rest.get("mcp_servers")
    if (_without_ours(before) == _without_ours(rest)
            and not (isinstance(servers, dict) and MCP_NAME in servers)):
        return before, stripped, at, _without_ours(rest)
    if not blocks:
        raise SettingsError(
            f"{path}: mcp_servers.{MCP_NAME} is defined in a form ozagia does not "
            "edit (dotted keys or an inline table); remove it and install again")
    for a, b in blocks:
        for i in range(a + 1, b):
            if lines[i].lstrip().startswith("[") and _header_parts(lines[i]) is None:
                try:
                    tomllib.loads(lines[i])
                except tomllib.TOMLDecodeError:
                    continue  # not a header on its own (an array line, say)
                raise refuse(f"line {i + 1} (`{lines[i].strip()}`) is a table header "
                             "ozagia cannot read, so it cannot tell where its table ends")
    raise refuse("removing the lines from there to the next table header changes "
                 "other settings (dotted keys or an inline table for "
                 f"mcp_servers.{MCP_NAME} elsewhere?)")


def _remove_blocks(text: str) -> tuple[str, int | None]:
    """`text` without ozagia's tables, and the line index where the first
    one was (None if there was none). A newline that install added after
    the user's last line (`NO_EOL_TOML` in the last table) goes too."""
    lines = text.splitlines(keepends=True)
    blocks = _our_blocks(lines)
    if not blocks:
        return text, None
    drop: set[int] = set()
    for a, b in blocks:
        drop.update(range(a, b))
    # Blank lines between two of ozagia's tables go with them.
    for (_, b), (a2, _) in zip(blocks, blocks[1:]):
        if all(not lines[i].strip() for i in range(b, a2)):
            drop.update(range(b, a2))
    # A block at the end of the file takes the blank line before it along
    # (the one `install` added when appending).
    last_a, last_b = blocks[-1]
    if last_b == len(lines) and last_a > 0 and not lines[last_a - 1].strip():
        drop.add(last_a - 1)
    first = blocks[0][0]
    kept = [x for i, x in enumerate(lines) if i not in drop]
    at = sum(1 for i in range(first) if i not in drop)
    out = "".join(kept)
    if (last_b == len(lines) and out.endswith("\n")
            and any(lines[i].strip() == NO_EOL_TOML for i in range(last_a, last_b))):
        out = out[:-1]
    return out, at


def merge_config(path: Path, text: str, entry: dict[str, Any]) -> str:
    """`text` with ozagia's table added or replaced (see the module docstring)."""
    _, stripped, at, rest = _isolate(path, text)
    lines = stripped.splitlines(keepends=True)
    if at is None or at >= len(lines):
        no_eol = bool(stripped) and not stripped.endswith("\n")
        if no_eol:
            stripped += "\n"
        table = mcp_table(entry, no_eol=no_eol)
        # One blank line before the table; uninstall takes it away again.
        sep = "\n" if stripped.strip() else ""
        new = stripped + sep + table
    else:
        table = mcp_table(entry)
        if at > 0 and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        nxt = "\n" if lines[at].strip() else ""
        new = "".join(lines[:at]) + table + nxt + "".join(lines[at:])
    after = _parse_toml(path, new)
    if _without_ours(after) != rest or after["mcp_servers"][MCP_NAME] != entry:
        raise SettingsError(f"{path}: the merged file does not parse as expected; "
                            "not written")
    return new


def unmerge_config(path: Path, text: str) -> str | None:
    """`text` without ozagia's table, or None if it had none."""
    if not _our_blocks(text.splitlines(keepends=True)):
        _parse_toml(path, text)
        return None
    _, stripped, _, _ = _isolate(path, text)
    return stripped


# --------------------------------------------------------------------------- #
# AGENTS.md
# --------------------------------------------------------------------------- #
def _section_span(text: str) -> tuple[int, int] | None:
    a = text.find(BEGIN)
    if a < 0:
        return None
    b = text.find(END, a)
    if b < 0:
        raise SettingsError(f"AGENTS.md has `{BEGIN}` without `{END}`")
    b += len(END)
    if text[b:b + 1] == "\n":
        b += 1
    return a, b


def _agents_section(no_eol: bool) -> str:
    if not no_eol:
        return AGENTS_SECTION
    return AGENTS_SECTION.replace(BEGIN + "\n", f"{BEGIN}\n{NO_EOL_MD}\n", 1)


def merge_agents(text: str | None) -> str:
    text = text or ""
    span = _section_span(text)
    if span is not None:
        no_eol = NO_EOL_MD in text[span[0]:span[1]]
        return text[:span[0]] + _agents_section(no_eol) + text[span[1]:]
    if not text.strip():
        return AGENTS_SECTION
    no_eol = not text.endswith("\n")
    if no_eol:
        text += "\n"
    return text + "\n" + _agents_section(no_eol)


def unmerge_agents(text: str) -> str | None:
    span = _section_span(text)
    if span is None:
        return None
    a, b = span
    head, tail = text[:a], text[b:]
    if not tail.strip() and head.endswith("\n\n"):
        head = head[:-1]
        if NO_EOL_MD in text[a:b]:
            head = head[:-1]
    return head + tail


# --------------------------------------------------------------------------- #
# install / uninstall
# --------------------------------------------------------------------------- #
def install(workdir: str | Path, *, scope: str = "project",
            python: str = sys.executable, home: str | Path | None = None
            ) -> dict[str, Path]:
    """Add the hooks, the MCP server and the AGENTS.md section. Idempotent.
    Every file is checked before any is written."""
    p = paths(workdir, scope, home)
    hooks_text = _dump_hooks(merge_hooks(_load_hooks(p["hooks"]), python))
    config_text = merge_config(p["config"], _read(p["config"]) or "",
                               mcp_entry(workdir, scope, python))
    agents_text = merge_agents(_read(p["agents"]))
    for key, text in (("hooks", hooks_text), ("config", config_text),
                      ("agents", agents_text)):
        if _read(p[key]) != text:
            _atomic_write(p[key], text)
    return p


def _write_or_delete(path: Path, text: str) -> None:
    if text.strip():
        _atomic_write(path, text)
    else:
        path.unlink()


def uninstall(workdir: str | Path, *, scope: str = "project",
              home: str | Path | None = None) -> list[Path]:
    """Remove ozagia's hooks, MCP table and AGENTS.md section. Returns the
    files that changed."""
    p = paths(workdir, scope, home)
    changes: list[tuple[Path, str]] = []
    if p["hooks"].exists():
        data = _load_hooks(p["hooks"])
        if _strip_hooks(data):
            changes.append((p["hooks"], "" if not data else _dump_hooks(data)))
    text = _read(p["config"])
    if text is not None:
        new = unmerge_config(p["config"], text)
        if new is not None:
            changes.append((p["config"], new))
    text = _read(p["agents"])
    if text is not None:
        new = unmerge_agents(text)
        if new is not None:
            changes.append((p["agents"], new))
    for path, new in changes:
        _write_or_delete(path, new)
    return [path for path, _ in changes]
