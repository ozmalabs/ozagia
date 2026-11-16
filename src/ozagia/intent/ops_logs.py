"""`[ozagia] ops:<key>` lines after container and unit log commands.

After `podman logs <name>`, `docker logs <name>`, `docker compose logs
[<svc>...]` (also `podman compose`, `docker-compose`, `podman-compose`),
`journalctl -u <unit>` and `systemctl status <unit>` ran, name the
operations graph nodes (`ozagia.core.ops_graph`) the command is about, the
way pytest's summary maps to `test:` keys. These commands are never
captured or answered: this only annotates output the built-in already
produced, and reads nothing but the command line and the graph.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import PurePosixPath
from typing import Any

from ozagia.intent.common import tag

MAX_LINES = 8
_TOOLS = re.compile(r"\b(?:podman|docker|docker-compose|podman-compose|"
                    r"journalctl|systemctl)\b")
_ENGINE_GLOBAL_VALUE = frozenset({
    "-H", "--host", "--context", "-c", "--config", "-l", "--log-level",
    "--connection", "--url", "--identity", "--root", "--runroot",
    "--storage-driver", "--cgroup-manager", "--tlscacert", "--tlscert",
    "--tlskey", "--module",
})
_LOGS_VALUE = frozenset({"--since", "--until", "--tail", "-n", "--index"})
_COMPOSE_VALUE = frozenset({
    "-f", "--file", "-p", "--project-name", "--project-directory",
    "--env-file", "--profile", "--ansi", "--progress", "--parallel",
})
_JOURNAL_SHORT_VALUE = frozenset("nposSUtTDMgIu")
_JOURNAL_LONG_VALUE = frozenset({
    "--lines", "--priority", "--output", "--since", "--until", "--identifier",
    "--directory", "--machine", "--grep", "--cursor", "--after-cursor",
    "--boot", "--facility", "--root", "--file", "--namespace",
})
_SYSTEMCTL_VALUE = frozenset({
    "-H", "--host", "-M", "--machine", "-n", "--lines", "-o", "--output",
    "-p", "--property", "-t", "--type", "--state", "--root",
})


def _positionals(args: list[str], value_opts: frozenset[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            if "=" not in a and a in value_opts:
                i += 1
        else:
            out.append(a)
        i += 1
    return out


def _compose(args: list[str]) -> tuple[str, Any] | None:
    """`("compose", (files, services))` for `... logs [svc...]`."""
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file") and i + 1 < len(args):
            files.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--file="):
            files.append(a.split("=", 1)[1])
        elif a.startswith("-") and a in _COMPOSE_VALUE:
            i += 2
            continue
        elif not a.startswith("-"):
            if a != "logs":
                return None
            return "compose", (files, _positionals(args[i + 1:], _LOGS_VALUE))
        i += 1
    return None


def log_requests(argv: list[str]) -> list[tuple[str, Any]]:
    """What one command's logs are about: `("container", name)`,
    `("compose", (files, services))` or `("unit", name)`."""
    if not argv:
        return []
    base = os.path.basename(argv[0])
    rest = argv[1:]
    if base in ("docker-compose", "podman-compose"):
        found = _compose(rest)
        return [found] if found else []
    if base in ("podman", "docker"):
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if (rest[i] in _ENGINE_GLOBAL_VALUE and "=" not in rest[i]) else 1
        words = rest[i:]
        if not words:
            return []
        if words[0] == "compose":
            found = _compose(words[1:])
            return [found] if found else []
        if words[0] in ("container", "pod") and len(words) > 1:
            words = words[1:]
        if words[0] != "logs":
            return []
        return [("container", n) for n in _positionals(words[1:], _LOGS_VALUE)]
    if base == "journalctl":
        units: list[str] = []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a in ("--unit", "--user-unit") and i + 1 < len(rest):
                units.append(rest[i + 1])
                i += 2
                continue
            if a.startswith(("--unit=", "--user-unit=")):
                units.append(a.split("=", 1)[1])
            elif a.startswith("--"):
                if a in _JOURNAL_LONG_VALUE:
                    i += 1
            elif a.startswith("-") and len(a) > 1:
                for j, ch in enumerate(a[1:], 1):
                    if ch in _JOURNAL_SHORT_VALUE:
                        value = a[j + 1:]
                        if not value and i + 1 < len(rest):
                            i += 1
                            value = rest[i]
                        if ch == "u" and value:
                            units.append(value)
                        break
            i += 1
        return [("unit", u) for u in units]
    if base == "systemctl":
        words = _positionals(rest, _SYSTEMCTL_VALUE)
        if words and words[0] == "status":
            return [("unit", u) for u in words[1:]]
    return []


def _unit_name(name: str) -> str:
    """systemd's mangling: a name without a suffix is a service; a glob
    stays as it is."""
    if any(c in name for c in "*?[") or "." in name.rsplit("/", 1)[-1]:
        return name
    return f"{name}.service"


def _template(name: str) -> str | None:
    m = re.fullmatch(r"([^@]+)@[^.]*(\.[a-z]+)", name)
    return f"{m.group(1)}@{m.group(2)}" if m else None


def _rel_dir(root: str, wd: Any) -> str | None:
    if not wd:
        return "."
    path = os.path.realpath(os.path.join(root, str(wd)))
    rel = os.path.relpath(path, root)
    return None if rel.startswith("..") else PurePosixPath(rel).as_posix()


def _match(nodes: list[tuple[int, str, dict[str, Any]]], kind: str, value: Any,
           root: str, wd: Any) -> list[tuple[int, str, dict[str, Any]]]:
    if kind == "unit":
        unit = _unit_name(str(value))
        names = {unit}
        templ = _template(unit)
        if templ:
            names.add(templ)
        glob = any(c in unit for c in "*?[")

        def hit(attrs: dict[str, Any]) -> bool:
            kinds = str(attrs.get("kind", ""))
            if kinds.startswith("systemd_"):
                cand = str(attrs.get("name", ""))
            elif kinds.startswith("quadlet_"):
                cand = str(attrs.get("service_unit", ""))
            else:
                return False
            return cand in names or (glob and fnmatch.fnmatchcase(cand, unit))
        return [x for x in nodes if hit(x[2])]
    if kind == "container":
        name = str(value)
        exact = [x for x in nodes if x[2].get("container_name") == name]
        if exact:
            return exact
        found = []
        for x in nodes:
            attrs = x[2]
            if attrs.get("kind") != "compose_service":
                continue
            project = re.escape(str(attrs.get("project") or ""))
            svc = re.escape(str(attrs.get("name") or ""))
            if project and re.fullmatch(rf"{project}([-_]){svc}\1\d+", name):
                found.append(x)
        if found:
            return found
        return [x for x in nodes if (
            attrs := x[2]).get("kind") in ("compose_service", "quadlet_container")
            and (attrs.get("name") == name
                 or PurePosixPath(str(attrs.get("name", ""))).stem == name)]
    files, services = value
    wd_rel = _rel_dir(root, wd)
    paths: set[str] = set()
    for f in files:
        if wd_rel is None:
            continue
        rel = os.path.normpath(os.path.join(wd_rel, f))
        if not rel.startswith(".."):
            paths.add(PurePosixPath(rel).as_posix())
    projects = [x for x in nodes if x[2].get("kind") == "compose_project"]
    if paths:
        projects = [x for x in projects if x[2].get("path") in paths]
    elif wd_rel is not None:
        here = [x for x in projects
                if PurePosixPath(str(x[2].get("path"))).parent.as_posix() == wd_rel]
        projects = here or projects
    chosen = {str(x[2].get("path")) for x in projects}
    if not services:
        return projects
    return [x for x in nodes if x[2].get("kind") == "compose_service"
            and x[2].get("path") in chosen and x[2].get("name") in services]


def _summary(memory: Any, nid: int, key: str, attrs: dict[str, Any]) -> str:
    from ozagia.core.ops_graph import describe
    extra: list[str] = []
    image = attrs.get("image")
    if isinstance(image, str):
        extra.append(f"image {image}")
    files = []
    nodes = getattr(memory, "nodes", {})
    for edge in getattr(memory, "_out", {}).get(nid, []):
        if str(edge.get("type", "")) == "ops_runs":
            target = (nodes.get(int(edge["tgt"])) or {}).get("key")
            if target and target not in files:
                files.append(str(target))
    if files:
        extra.append("runs " + ", ".join(files[:3]))
    return describe(key, attrs) + ("; " + "; ".join(extra) if extra else "")


def ops_log_lines(memory: Any, command: str, wd: Any = None
                  ) -> tuple[list[str], list[str]]:
    """`[ozagia] ops:<key>: ...` lines naming the ops nodes a log or status
    command is about, and their keys. Empty when the command is none of
    these or the graph has no matching node."""
    if not isinstance(command, str) or not _TOOLS.search(command):
        return [], []
    from ozagia.core.ops_graph import ops_nodes
    from ozagia.intent.common import root_of
    from ozagia.intent.shell import command_heads
    requests = [r for head in command_heads(command) for r in log_requests(head)]
    if not requests:
        return [], []
    nodes = ops_nodes(memory)
    if not nodes:
        return [], []
    root = str(root_of(memory))
    lines: list[str] = []
    keys: list[str] = []
    for kind, value in requests:
        for nid, key, attrs in _match(nodes, kind, value, root, wd):
            if key in keys:
                continue
            keys.append(key)
            if len(lines) < MAX_LINES:
                lines.append(tag(_summary(memory, nid, key, attrs)))
    if len(keys) > len(lines):
        lines.append(tag(f"{len(keys) - len(lines)} more ops node(s) match; "
                         "infra_ops {\"query\": \"exposes\", ...} lists them"))
    return lines, keys
