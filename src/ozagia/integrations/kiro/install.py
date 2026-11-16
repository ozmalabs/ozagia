"""Install or remove ozagia's Kiro CLI integration.

kiro-cli 2.37 loads hooks from two places, by engine (see docs/kiro.md for
how this was established):

- `--format v3` (the default): a standalone hook file,
  `.kiro/hooks/ozagia.json` (`{"version": "v1", "hooks": [...]}`, PascalCase
  triggers). The V3 engine loads it in every session of the workspace,
  whatever the agent. It is what `kiro-cli chat` runs when
  `chat.agentEngine` is `"v3"` (or with `--v3`). The Rust engines (V1, V2)
  do not read hook files.
- `--format agent`: the hooks in an agent profile, `.kiro/agents/<agent>.json`
  (default agent name `ozagia`), as an array of hook documents with the
  CLI 2.x trigger names (`agentSpawn`, `preToolUse`, `postToolUse`): the
  form `kiro-cli agent upgrade` calls universal, which every engine reads
  and the upgrade leaves alone. (A profile that already uses the CLI 2.x
  object form, `hooks` keyed by trigger, gets ozagia's hooks in that form.)
  Only a session that uses the agent runs them
  (`kiro-cli chat --agent ozagia`). A new profile gets all tools
  (`"tools": ["*"]`) and the workspace MCP servers (`"includeMcpJson":
  true`); an existing one keeps everything and gets only ozagia's hooks.
- `--format both`: both. In a V3 session with the ozagia agent the hooks
  then run twice (harmless, but twice the work).

In each case the ozagia MCP server goes into `.kiro/settings/mcp.json`
(`mcpServers.ozagia`; other servers and keys are kept).

`scope="project"` writes under `<workdir>/.kiro/`; `scope="user"` under
`~/.kiro/` (`hooks/`, `agents/`, `settings/mcp.json`).

Every file is JSON, read and checked before anything is written, merged
(ozagia's entries are those whose command ends with this package's `hook`
subcommand, and the `ozagia` MCP server), and written atomically. A file
that is not valid JSON or has the wrong shape is refused and nothing is
written. If a write fails partway, the files already written get their old
content back. A symlinked file is written through the link. Re-installing
keeps keys the user added to the `ozagia` MCP entry. `uninstall` removes
ozagia's entries (from every `agents/*.json` unless `agent` is given) and
deletes a file left with nothing else in it (the hook file, a profile that
is exactly what install created, an `mcp.json` with no other server or
key); a symlinked one is kept with what is left.

Kiro runs workspace hooks only in a trusted workspace (V3 asks on first
launch; `--trust-workspace` skips the question for one launch);
`NEXT_STEPS` says so.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from ozagia.integrations.kiro.hook import HOOK_TIMEOUT_S

MODULE = "ozagia.integrations.kiro"
HOOK_SUFFIX = f"-m {MODULE} hook"
SCOPES = ("project", "user")
FORMATS = ("v3", "agent", "both")
DEFAULT_FORMAT = "v3"
DEFAULT_AGENT = "ozagia"
HOOK_FILE = "ozagia.json"
MCP_NAME = "ozagia"
MCP_MODE = "coding"
DESCRIPTION = ("Added by `ozagia install kiro`; removed by "
               "`python -m ozagia.integrations.kiro uninstall`.")

# (name, V3 trigger, CLI 2.x trigger, matcher). Matchers are Kiro's tool
# categories, which every engine knows: shell (execute_bash), read (file
# reads, grep, file search, directory listing), write (fs_write, fs_append,
# str_replace).
HOOKS: tuple[tuple[str, str, str, str | None], ...] = (
    ("ozagia-session-brief", "SessionStart", "agentSpawn", None),
    ("ozagia-pre-shell", "PreToolUse", "preToolUse", "shell"),
    ("ozagia-pre-read", "PreToolUse", "preToolUse", "read"),
    ("ozagia-pre-write", "PreToolUse", "preToolUse", "write"),
    ("ozagia-post-read", "PostToolUse", "postToolUse", "read"),
    ("ozagia-post-write", "PostToolUse", "postToolUse", "write"),
    ("ozagia-post-shell", "PostToolUse", "postToolUse", "shell"),
    # A plain word is tried as a regex against the tool ID: web_fetch.
    ("ozagia-post-web-fetch", "PostToolUse", "postToolUse", "web_fetch"),
)
# Hooks only the V3 hook file gets: (name, trigger, matcher). PostCompact
# (CLI 3.1.0+) hands back the working set (hook.compaction_context). The
# 2.37 V3 loader skips a hook whose trigger it does not know, with a
# warning, and loads the rest of the file; the CLI 2.x agent validator
# rejects an unknown trigger, so profiles do not get it. PreCompact is not
# registered: a non-zero exit there would cancel the compaction.
V3_ONLY_HOOKS: tuple[tuple[str, str, str | None], ...] = (
    ("ozagia-post-compact", "PostCompact", None),
)

NEXT_STEPS = """\
Next steps:
  1. Kiro runs workspace hooks only in a trusted workspace: the first
     `kiro-cli chat` in the project asks "Trust this workspace?" (V3).
  2. The hook file (.kiro/hooks/ozagia.json) is loaded by the V3 engine
     (`chat.agentEngine = "v3"` or `kiro-cli --v3`). For the V1/V2 engines
     install with `--format agent` and start `kiro-cli chat --agent ozagia`.
  3. The graph daemon starts on its own on the first hook call and stops
     after 30 minutes without requests. Start it early with
     `ozagia daemon ensure --root <repo>`."""


class SettingsError(ValueError):
    """A file exists but cannot be merged safely."""


# --------------------------------------------------------------------------- #
# paths and files
# --------------------------------------------------------------------------- #
def kiro_home() -> Path:
    return Path.home() / ".kiro"


def paths(workdir: str | Path, scope: str = "project", home: str | Path | None = None,
          agent: str = DEFAULT_AGENT) -> dict[str, Path]:
    """`{"hooks", "agent", "mcp"}` for `scope`."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {list(SCOPES)}, not {scope!r}")
    if not agent or "/" in agent or agent.startswith("."):
        raise ValueError(f"not an agent name: {agent!r}")
    if scope == "project":
        base = Path(workdir).resolve() / ".kiro"
    else:
        base = Path(home) if home is not None else kiro_home()
    return {"hooks": base / "hooks" / HOOK_FILE,
            "agent": base / "agents" / f"{agent}.json",
            "mcp": base / "settings" / "mcp.json"}


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path`'s content with `text` atomically. A symlink is written
    through: its target gets the new content and the link stays."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o7777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _apply(changes: list[tuple[Path, str | None]]) -> None:
    """Write each `(path, text)` (None: delete the file) in order. If one
    fails, the files already changed get their old content back (or are
    removed again if they did not exist) and the error is raised."""
    done: list[tuple[Path, bytes | None]] = []
    try:
        for path, text in changes:
            old = path.read_bytes() if path.exists() else None
            if text is None:
                path.unlink()
            else:
                _atomic_write(path, text)
            done.append((path, old))
    except BaseException:
        for path, old in reversed(done):
            try:
                if old is None:
                    path.resolve().unlink(missing_ok=True)
                else:
                    _atomic_write(path, old.decode("utf-8"))
            except OSError:
                pass
        raise


def _load(path: Path) -> dict[str, Any] | None:
    """The JSON object in `path`, None if there is no file (or it is blank)."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: not a JSON object")
    return data


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# hook commands
# --------------------------------------------------------------------------- #
def hook_command(python: str = sys.executable) -> str:
    return f"{shlex.quote(python)} {HOOK_SUFFIX}"


def _is_ours(command: Any) -> bool:
    return isinstance(command, str) and command.rstrip().endswith(HOOK_SUFFIX)


def is_ozagia_hook(hook: Any) -> bool:
    """A V3 hook document (`{name, trigger, action: {type, command}}`) or a
    CLI 2.x hook entry (`{command, ...}`) that runs ozagia's hook."""
    if not isinstance(hook, dict):
        return False
    action = hook.get("action")
    if isinstance(action, dict):
        return action.get("type") == "command" and _is_ours(action.get("command"))
    return _is_ours(hook.get("command"))


def v3_hooks(python: str) -> list[dict[str, Any]]:
    """ozagia's hook documents for a `.kiro/hooks/*.json` file."""
    return ([_hook_doc(name, trigger, matcher, python)
             for name, trigger, _, matcher in HOOKS]
            + [_hook_doc(name, trigger, matcher, python)
               for name, trigger, matcher in V3_ONLY_HOOKS])


def _hook_doc(name: str, trigger: str, matcher: str | None, python: str
              ) -> dict[str, Any]:
    doc: dict[str, Any] = {"name": name, "description": DESCRIPTION, "trigger": trigger}
    if matcher is not None:
        doc["matcher"] = matcher
    doc["action"] = {"type": "command", "command": hook_command(python)}
    doc["timeout"] = HOOK_TIMEOUT_S
    return doc


def profile_hooks(python: str) -> list[dict[str, Any]]:
    """ozagia's hooks for an agent profile, in the array form with the CLI
    2.x trigger names: what `kiro-cli agent upgrade` calls the universal
    format, read by every engine and left alone by the upgrade."""
    return [_hook_doc(name, trigger, matcher, python)
            for name, _, trigger, matcher in HOOKS]


def legacy_hooks(python: str) -> dict[str, list[dict[str, Any]]]:
    """ozagia's hooks in the CLI 2.x object form, keyed by trigger, for a
    profile that already uses that form."""
    out: dict[str, list[dict[str, Any]]] = {}
    for _, _, trigger, matcher in HOOKS:
        entry: dict[str, Any] = {}
        if matcher is not None:
            entry["matcher"] = matcher
        entry["command"] = hook_command(python)
        entry["timeout_ms"] = HOOK_TIMEOUT_S * 1000
        out.setdefault(trigger, []).append(entry)
    return out


# --------------------------------------------------------------------------- #
# .kiro/hooks/ozagia.json
# --------------------------------------------------------------------------- #
def _check_hook_file(path: Path, data: dict[str, Any]) -> list[Any]:
    version = data.get("version")
    if version is not None and version != "v1":
        raise SettingsError(f"{path}: version {version!r} is not \"v1\"")
    hooks = data.get("hooks", [])
    if not isinstance(hooks, list):
        raise SettingsError(f"{path}: `hooks` is not a list")
    return hooks


def merge_hook_file(path: Path, data: dict[str, Any] | None, python: str) -> dict[str, Any]:
    data = dict(data or {})
    kept = [h for h in _check_hook_file(path, data) if not is_ozagia_hook(h)]
    return {**data, "version": "v1", "hooks": kept + v3_hooks(python)}


def unmerge_hook_file(path: Path, data: dict[str, Any]) -> dict[str, Any] | None:
    """`data` without ozagia's hooks, `{}` if nothing else is left, None if
    it had none."""
    hooks = _check_hook_file(path, data)
    kept = [h for h in hooks if not is_ozagia_hook(h)]
    if len(kept) == len(hooks):
        return None
    if not kept:
        # A hook file needs at least one hook; other keys alone are not a file.
        return {}
    return {**data, "hooks": kept}


# --------------------------------------------------------------------------- #
# .kiro/agents/<agent>.json
# --------------------------------------------------------------------------- #
def new_profile(agent: str) -> dict[str, Any]:
    """What install puts in a profile it creates, besides the hooks."""
    return {"name": agent,
            "description": ("Kiro's tools with ozagia's graph hooks and MCP server. "
                            + DESCRIPTION),
            "tools": ["*"], "includeMcpJson": True}


def _check_profile_hooks(path: Path, data: dict[str, Any]) -> list[Any] | dict[str, Any]:
    hooks = data.get("hooks")
    if hooks is None:
        return []
    if isinstance(hooks, list):
        return hooks
    if not isinstance(hooks, dict):
        raise SettingsError(f"{path}: `hooks` is neither a list nor an object")
    for trigger, entries in hooks.items():
        if not isinstance(entries, list):
            raise SettingsError(f"{path}: `hooks.{trigger}` is not a list")
    return hooks


def _strip_profile_hooks(hooks: list[Any] | dict[str, Any]) -> list[Any] | dict[str, Any]:
    if isinstance(hooks, list):
        return [h for h in hooks if not is_ozagia_hook(h)]
    kept = {t: [e for e in entries if not is_ozagia_hook(e)] for t, entries in hooks.items()}
    return {t: e for t, e in kept.items() if e}


def _count(hooks: list[Any] | dict[str, Any]) -> int:
    return len(hooks) if isinstance(hooks, list) else sum(map(len, hooks.values()))


def merge_profile(path: Path, data: dict[str, Any] | None, python: str,
                  agent: str) -> dict[str, Any]:
    """`data` (or a new profile) with ozagia's hooks. They are added in the
    form the profile already uses: the object form stays the object form,
    anything else gets the array form (`profile_hooks`)."""
    data = dict(data) if data is not None else new_profile(agent)
    hooks = _strip_profile_hooks(_check_profile_hooks(path, data))
    if isinstance(hooks, dict) and "hooks" in data:
        for trigger, entries in legacy_hooks(python).items():
            hooks.setdefault(trigger, []).extend(entries)
        data["hooks"] = hooks
    else:
        data["hooks"] = list(hooks) + profile_hooks(python)
    return data


def unmerge_profile(path: Path, data: dict[str, Any], agent: str) -> dict[str, Any] | None:
    """`data` without ozagia's hooks (`{}` when it is then exactly the
    profile install creates), None if it had none."""
    hooks = _check_profile_hooks(path, data)
    kept = _strip_profile_hooks(hooks)
    if _count(kept) == _count(hooks):
        return None
    out = dict(data)
    if kept:
        out["hooks"] = kept
    else:
        out.pop("hooks", None)
    return {} if out == new_profile(agent) else out


# --------------------------------------------------------------------------- #
# .kiro/settings/mcp.json
# --------------------------------------------------------------------------- #
def mcp_entry(workdir: str | Path, scope: str, python: str) -> dict[str, Any]:
    """`{"command", "args", "env"}` for the ozagia MCP server. The project
    entry names the project root; the user entry has no root, so the server
    takes the repository of its working directory."""
    from ozagia.harness.opencode import mcp_server_entry
    entry = mcp_server_entry(Path(workdir).resolve(), MCP_MODE, tool_python=python)
    cmd = list(entry["command"])
    env = dict(entry["environment"])
    if scope == "user":
        at = cmd.index("--root")
        del cmd[at:at + 2]
        env.pop("OZAGIA_ROOT", None)
    return {"command": cmd[0], "args": cmd[1:], "env": env}


def _servers(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise SettingsError(f"{path}: `mcpServers` is not an object")
    return servers


def merge_mcp(path: Path, data: dict[str, Any] | None, entry: dict[str, Any]) -> dict[str, Any]:
    data = dict(data or {})
    servers = dict(_servers(path, data))
    old = servers.get(MCP_NAME)
    # Keep keys the user added to the entry (autoApprove, disabled, timeout).
    servers[MCP_NAME] = {**old, **entry} if isinstance(old, dict) else entry
    data["mcpServers"] = servers
    return data


def unmerge_mcp(path: Path, data: dict[str, Any]) -> dict[str, Any] | None:
    servers = _servers(path, data)
    if MCP_NAME not in servers:
        return None
    out = dict(data)
    out["mcpServers"] = {k: v for k, v in servers.items() if k != MCP_NAME}
    if not out["mcpServers"] and set(out) == {"mcpServers"}:
        return {}
    return out


# --------------------------------------------------------------------------- #
# install / uninstall
# --------------------------------------------------------------------------- #
def _wants(fmt: str) -> tuple[bool, bool]:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {list(FORMATS)}, not {fmt!r}")
    return fmt in ("v3", "both"), fmt in ("agent", "both")


def install(workdir: str | Path, *, scope: str = "project", python: str = sys.executable,
            fmt: str = DEFAULT_FORMAT, agent: str = DEFAULT_AGENT,
            home: str | Path | None = None) -> dict[str, Path]:
    """Write the hooks (as `fmt` says) and the MCP server. Idempotent. Every
    file is checked before any is written. Returns the files written to,
    by kind (`hooks`, `agent`, `mcp`)."""
    p = paths(workdir, scope, home, agent)
    hook_file, profile = _wants(fmt)
    plan: dict[str, str] = {}
    if hook_file:
        plan["hooks"] = _dump(merge_hook_file(p["hooks"], _load(p["hooks"]), python))
    if profile:
        plan["agent"] = _dump(merge_profile(p["agent"], _load(p["agent"]), python, agent))
    plan["mcp"] = _dump(merge_mcp(p["mcp"], _load(p["mcp"]),
                                  mcp_entry(workdir, scope, python)))
    _apply([(p[key], text) for key, text in plan.items()
            if not p[key].exists() or p[key].read_text(encoding="utf-8") != text])
    return {key: p[key] for key in plan}


def _emptied(key: str, data: dict[str, Any]) -> dict[str, Any]:
    """What is left of `data` without ozagia, for a file uninstall would
    delete but keeps because it is a symlink."""
    if key == "hooks":
        return {**data, "hooks": []}
    if key == "mcp":
        return {**data, "mcpServers": {}}
    return {k: v for k, v in data.items() if k != "hooks"}


def uninstall(workdir: str | Path, *, scope: str = "project", agent: str | None = None,
              home: str | Path | None = None) -> list[Path]:
    """Remove ozagia's hooks (hook file and agent profiles, whichever are
    there) and its MCP server. With `agent`, only `agents/<agent>.json` is
    looked at; without, every `agents/*.json` (one that cannot be read is
    skipped). A file left with nothing else is deleted, unless it is a
    symlink: then what is left is written through the link. Returns the
    files that changed."""
    p = paths(workdir, scope, home, agent or DEFAULT_AGENT)
    targets: list[tuple[str, Path, str, bool]] = [("hooks", p["hooks"], "", False)]
    if agent:
        targets.append(("agent", p["agent"], agent, False))
    else:
        targets += [("agent", f, f.stem, True)
                    for f in sorted(p["agent"].parent.glob("*.json"))]
    targets.append(("mcp", p["mcp"], "", False))
    changes: list[tuple[Path, str | None]] = []
    for key, path, name, optional in targets:
        try:
            data = _load(path)
            if data is None:
                continue
            if key == "hooks":
                new = unmerge_hook_file(path, data)
            elif key == "agent":
                new = unmerge_profile(path, data, name)
            else:
                new = unmerge_mcp(path, data)
        except (SettingsError, UnicodeDecodeError):
            if optional:
                continue
            raise
        if new is None:
            continue
        if not new and path.is_symlink():
            new = _emptied(key, data)
        changes.append((path, _dump(new) if new else None))
    _apply(changes)
    return [path for path, _ in changes]
