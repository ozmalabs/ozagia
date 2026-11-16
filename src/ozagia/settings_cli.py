"""``python -m ozagia.settings_cli``: inspect and preview settings writes.

Reads the typed registry in :mod:`ozagia.core.settings`. ``set`` writes unless
``--dry-run`` is given; the Python API defaults to a dry run. ``migrate``
previews the legacy ``.ozagia.toml`` migration unless ``--apply --confirm
TOKEN`` is given. Exit status is 2 for validation, configuration or IO errors.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import migrate as M
from ozagia.core import settings as S


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _format(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _print_schema(items: list[dict[str, Any]]) -> None:
    group = None
    for item in items:
        if item["group"] != group:
            group = item["group"]
            print(f"[{group}]")
        leaf = item["key"].split(".", 1)[1]
        flags = f"scope={item['scope']}"
        if item["restart_required"]:
            flags += " restart_required"
        print(f"  {leaf:<26} {item['type']:<11} "
              f"default={_format(item['default'])}  {flags}")
        print(f"    {item['description']}")
        print(f"    validation: {item['validation']}")
        toml_path = f"{item['section']}.{item['name']}"
        if toml_path != item["key"]:
            print(f"    toml: {toml_path}")
        if item["aliases"]:
            print(f"    aliases: {', '.join(item['aliases'])}")
        if item["env"]:
            print(f"    env: {item['env']}")


def _print_values(items: dict[str, dict[str, Any]]) -> None:
    for key, item in items.items():
        print(f"{key} = {_format(item['value'])}  ({item['source']})")


def _print_get(item: dict[str, Any]) -> None:
    print(f"{item['key']} = {_format(item['value'])}  ({item['source']})")
    print(f"  type: {item['type']}  scope: {item['scope']}  "
          f"default: {_format(item['default'])}")
    print(f"  {item['description']}")


def _print_set(result: dict[str, Any]) -> None:
    diff = result.get("diff") or ""
    if diff:
        print(diff, end="" if diff.endswith("\n") else "\n")
    elif not result.get("changed"):
        print(f"no change: {result['path']}")
    if result.get("dry_run"):
        print(f"dry run: {result['path']} was not changed")
    elif result.get("applied"):
        print(f"wrote {result['path']}")
    source = (result.get("current") or {}).get("source")
    if source in {"env", "override"}:
        print(f"note: {source} currently overrides this value")


def build_parser(prog: str = "ozagia-settings") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="inspect the typed settings registry and preview writes")
    sub = parser.add_subparsers(dest="command", required=True)

    schema = sub.add_parser("schema", help="print the setting registry")
    schema.add_argument("--json", action="store_true")

    get = sub.add_parser("get", help="print one setting, its value and source")
    get.add_argument("key")
    get.add_argument("--root", default=None)
    get.add_argument("--json", action="store_true")

    values = sub.add_parser("values", help="print every setting and its source")
    values.add_argument("--root", default=None)
    values.add_argument("--json", action="store_true")

    set_value = sub.add_parser(
        "set", help="validate and write a setting (preview with --dry-run)")
    set_value.add_argument("key")
    set_value.add_argument("value")
    set_value.add_argument("--scope", choices=("repo", "user"), default="repo")
    set_value.add_argument("--root", default=None)
    set_value.add_argument("--dry-run", action="store_true")
    set_value.add_argument("--json", action="store_true")

    migrate = sub.add_parser(
        "migrate", help="preview or apply the .ozagia.toml migration")
    migrate.add_argument("--root", default=None)
    mode = migrate.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="preview only (the default)")
    mode.add_argument("--apply", action="store_true",
                      help="write the previewed values; needs --confirm")
    migrate.add_argument("--confirm", default=None,
                         metavar="TOKEN",
                         help="the token printed by a previous dry run")
    migrate.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None, *,
         prog: str = "ozagia-settings") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        if args.command == "schema":
            items = S.schema()
            if args.json:
                _print_json({"schema": S.SCHEMA, "settings": items})
            else:
                _print_schema(items)
            return 0

        root = _root(getattr(args, "root", None))
        if args.command == "values":
            items = S.values(root)
            if args.json:
                _print_json({"schema": S.SCHEMA, "root": str(root),
                             "locations": S.locations(root), "settings": items})
            else:
                _print_values(items)
            return 0

        if args.command == "get":
            item = S.get(root, args.key)
            if args.json:
                _print_json({"schema": S.SCHEMA, "root": str(root),
                             "setting": item})
            else:
                _print_get(item)
            return 0

        if args.command == "migrate":
            report = M.migrate(root, dry_run=not args.apply,
                               confirm=args.confirm)
            if args.json:
                _print_json(report)
            else:
                print(M.format_report(report))
            return 0 if report.get("ok") else 1

        result = S.set_value(root, args.key, args.value, scope=args.scope,
                             dry_run=args.dry_run)
        if args.json:
            _print_json(result)
        else:
            _print_set(result)
        return 0
    except (S.SettingsError, M.MigrateError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
