"""``python -m ozagia.toolchain_cli``: detect tools and preview installs.

Commands::

    env [--root R] [--json|--text] [--no-sudo]
    list [--json|--text]
    plan --capability ID ... --path P ... [--json|--text]
         [--no-versions] [--no-sudo] [--require-ready]
    explain TOOL [--root R] [--json|--text] [--no-versions] [--no-sudo]

The command is discovery only. There is no install or apply subcommand and
nothing here uses the network; a missing tool is a report (exit 0) unless
``plan --require-ready`` names a capability with no available tool.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.core import toolchain as T


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.toolchain_cli",
        description="external-tool discovery, capability readiness and "
                    "install previews (never installs)")
    sub = parser.add_subparsers(dest="command", required=True)

    environment = sub.add_parser(
        "env", help="report the OS, package managers, sudo and approval state")
    environment.add_argument("--root", default=None)
    _output(environment)
    environment.add_argument(
        "--no-sudo", dest="probe_sudo", action="store_false", default=True,
        help="do not run the sudo -n true usability check")

    listed = sub.add_parser(
        "list", help="list the capability and tool registry")
    _output(listed)

    plan = sub.add_parser(
        "plan", help="probe relevant tools and preview installs")
    plan.add_argument("--root", default=None)
    plan.add_argument(
        "--capability", action="append", default=[], metavar="ID",
        help="capability or group id, e.g. pdf or documents (repeatable)")
    plan.add_argument(
        "--path", action="append", default=[], metavar="P",
        help="file path whose extension infers a capability (repeatable)")
    _output(plan)
    plan.add_argument(
        "--no-versions", dest="probe_versions", action="store_false",
        default=True, help="do not run bounded --version probes")
    plan.add_argument(
        "--no-sudo", dest="probe_sudo", action="store_false", default=True,
        help="do not run the sudo -n true usability check")
    plan.add_argument(
        "--require-ready", action="store_true",
        help="exit 1 when a requested capability has no available tool")

    explain = sub.add_parser(
        "explain", help="one registry tool with install recipes and status")
    explain.add_argument("tool", metavar="TOOL")
    explain.add_argument("--root", default=None)
    _output(explain)
    explain.add_argument(
        "--no-versions", dest="probe_versions", action="store_false",
        default=True)
    explain.add_argument(
        "--no-sudo", dest="probe_sudo", action="store_false", default=True)
    return parser


def _output(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", dest="json", action="store_true",
                       help="emit JSON")
    group.add_argument("--text", dest="json", action="store_false",
                       default=False, help="emit text (default)")


def _root(args: argparse.Namespace) -> Path:
    return Path(args.root).resolve() if args.root else Path.cwd().resolve()


def _json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _environment_text(environment: dict) -> str:
    platform = environment["platform"]
    distro = platform["distro"]
    lines = ["[ozagia] toolchain environment"]
    label = f"{platform['system']} {platform['arch']}".strip()
    if distro.get("pretty_name") or distro.get("id"):
        label += ", " + str(distro.get("pretty_name") or distro.get("id"))
    if distro.get("version_id"):
        label += f" {distro['version_id']}"
    lines.append("platform: " + label)
    lines.append(
        "wsl: " + ("yes" if platform["wsl"] else "no")
        + "; container: " + ("yes" if platform["container"] else "no"))
    python = environment["python"]
    lines.append(
        f"python: {python['version']} at {python['executable']} "
        f"(venv {'yes' if python['venv'] else 'no'}, "
        f"uv {'yes' if python['uv'] else 'no'})")
    managers = environment["managers"]
    if managers:
        lines.append("managers: " + ", ".join(
            f"{name}={info['path']}" for name, info in managers.items()))
    else:
        lines.append("managers: none detected")
    sudo = environment["sudo"]
    sudo_text = sudo["path"] or "missing"
    if sudo["available"]:
        sudo_text += (", non-interactive "
                      + ("yes" if sudo["noninteractive"] else "no"))
    if not sudo["probed"]:
        sudo_text += ", not probed"
    lines.append("sudo: " + sudo_text)
    approval = environment["approval"]
    lines.append(
        "approval: install={} network={} sudo={} (sources: {})".format(
            "yes" if approval["allow_install"] else "no",
            "yes" if approval["allow_network"] else "no",
            "yes" if approval["allow_sudo"] else "no",
            ", ".join(f"{key}={value}" for key, value in
                      approval["sources"].items())))
    config = environment["config"]
    lines.append(
        "config: [tools] repo={repo} legacy={legacy}; errors={errors}, "
        "unknown={unknown}".format(
            repo=config["paths"]["repo"], legacy=config["paths"]["legacy"] or "-",
            errors=len(config["errors"]), unknown=len(config["unknown"])))
    for error in config["errors"][:10]:
        lines.append(
            f"  config error [{error['source']}] {error['key']}: "
            f"{error['message']}")
    preferred = environment.get("preferred") or {}
    for capability, ids in preferred.items():
        lines.append(f"preferred {capability}: {', '.join(ids)}")
    lines.append("not checked: network reachability and installation")
    return "\n".join(lines) + "\n"


def _list_text(registry: dict) -> str:
    lines = [
        f"[ozagia] toolchain registry {registry['registry_version']} "
        f"({registry['schema']})"]
    groups = registry.get("groups") or {}
    for group_id in sorted(groups):
        group = groups[group_id]
        subtype = (f"; subtypes {', '.join(group['subtypes'])}"
                   if group.get("subtypes") else "")
        lines.append(
            f"group {group_id}: {group['title']} -> "
            + ", ".join(group["capabilities"]) + subtype)
    capabilities = registry["capabilities"]
    tools = registry["tools"]
    for capability_id in sorted(capabilities):
        capability = capabilities[capability_id]
        extensions = ", ".join(capability["extensions"]) or "explicit only"
        group = (f"; group {capability['group']}"
                 if capability.get("group") else "")
        lines.append(
            f"{capability_id}: {capability['title']} ({extensions}){group}")
        for tool_id in capability["tools"]:
            tool = tools[tool_id]
            installs = ", ".join(
                sorted({item["manager"] for item in tool["installs"]}))
            extra = f"; extra={tool['extra']}" if tool.get("extra") else ""
            remote = ("; remote" if tool.get("remote") else
                      "" if tool.get("probeable", True) else "; not probed")
            lines.append(
                f"  {tool['id']} [{tool['kind']}/{tool['format']}]: "
                + ", ".join(tool["provides"])
                + f"; writes={'yes' if tool['writes'] else 'no'}"
                + extra + remote
                + (f"; installs={installs}" if installs else ""))
    return "\n".join(lines) + "\n"


def _tool_status(plan: dict, tool_id: str) -> dict:
    for capability in plan["capabilities"]:
        for item in capability["available"]:
            if item["id"] == tool_id:
                return {"available": True, **item}
        for item in capability["missing"]:
            if item["id"] == tool_id:
                options = [option for option in capability["install"]
                           if option["tool"] == tool_id]
                return {"available": False, **item, "install": options}
        for item in capability.get("remote") or []:
            if item["id"] == tool_id:
                return {"available": False, "remote": True, **item}
        for item in capability.get("unprobed") or []:
            if item["id"] == tool_id:
                return {"available": False, "probeable": False, **item}
    return {"available": False, "id": tool_id}


def _explain_text(entry: dict, status: dict) -> str:
    if status.get("available") and status.get("path"):
        status_line = "status: available at " + str(status["path"])
    elif status.get("available"):
        status_line = "status: available (Python library)"
    elif status.get("remote"):
        status_line = ("status: remote provider (not connected; requires "
                       "network" + (" and credentials"
                                    if status.get("credentialed") else "")
                       + ")")
    elif status.get("probeable") is False:
        status_line = "status: local provider not probed"
    else:
        status_line = "status: missing"
    lines = [
        f"{entry['id']} [{entry['kind']}] {entry['title']}",
        f"capabilities: {', '.join(entry['capabilities'])}",
        f"format: {entry['format']}",
        f"probes: {', '.join(entry['probes']) or 'none (not probed)'}",
        f"provides: {', '.join(entry['provides'])}",
        f"writes: {'yes' if entry['writes'] else 'no'}",
        status_line,
    ]
    if entry.get("extra"):
        lines.append("optional extra: ozagia[" + entry["extra"] + "]")
    if status.get("version"):
        lines.append("version: " + str(status["version"]))
    if entry.get("note"):
        lines.append("note: " + entry["note"])
    if not entry["installs"]:
        lines.append(
            "install recipes: none (remote/unprobed provider; ozagia never "
            "installs it)")
        return "\n".join(lines) + "\n"
    lines.append("install recipes (previews; nothing is run):")
    for install in entry["installs"]:
        command = install["command"].replace("{package}", install["package"])
        if install["requires_sudo"]:
            command = "sudo " + command
        kind = install.get("kind") or "package"
        extra = (f", Python extra {install['extra']}"
                 if install.get("extra") else "")
        lines.append(
            f"  {install['manager']} [{kind}{extra}]: {command}")
    return "\n".join(lines) + "\n"


def _environment(args: argparse.Namespace) -> int:
    environment = T.detect_environment(
        _root(args), probe_sudo=args.probe_sudo)
    if args.json:
        _json(environment)
    else:
        print(_environment_text(environment), end="")
    return 0


def _list(_args: argparse.Namespace) -> int:
    data = T.registry()
    if _args.json:
        _json(data)
    else:
        print(_list_text(data), end="")
    return 0


def _plan(args: argparse.Namespace) -> int:
    plan = T.tool_plan(
        _root(args), capabilities=args.capability, paths=args.path,
        probe_versions=args.probe_versions, probe_sudo=args.probe_sudo)
    if args.json:
        _json(plan)
    else:
        print(T.tool_plan_text(plan), end="")
    if args.require_ready:
        not_ready = [item["id"] for item in plan["capabilities"]
                     if not item["ready"]]
        if not_ready:
            print(
                "error: no available tool for: " + ", ".join(not_ready),
                file=sys.stderr)
            return 1
    return 0


def _explain(args: argparse.Namespace) -> int:
    entry = T.tool(args.tool)
    plan = T.tool_plan(
        _root(args), capabilities=entry["capabilities"],
        probe_versions=args.probe_versions, probe_sudo=args.probe_sudo)
    status = _tool_status(plan, entry["id"])
    if args.json:
        _json({
            "schema": T.SCHEMA,
            "registry_version": T.REGISTRY_VERSION,
            "tool": entry,
            "status": status,
            "environment": plan["environment"],
        })
    else:
        print(_explain_text(entry, status), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "env":
            return _environment(args)
        if args.command == "list":
            return _list(args)
        if args.command == "plan":
            return _plan(args)
        return _explain(args)
    except (T.ToolchainError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
