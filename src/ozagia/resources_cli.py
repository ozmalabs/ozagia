"""``ozagia-resources``: read-only host/resource observation and admission."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import resource_grants as RG
from ozagia.core import resources as R


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _freeing(values: list[str] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for text in values or []:
        try:
            memory, eta = text.split(":", 1)
            out.append({"memory_mb": float(memory), "disk_mb": 0.0,
                        "eta_s": float(eta)})
        except ValueError as exc:
            raise ValueError(f"--freeing must be MEM_MB:ETA_S, got {text!r}") from exc
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-resources",
        description="read-only host resources, history and admission checks")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="host memory/disk/load and top groups")
    status.add_argument("--root", default=None)
    status.add_argument("--path", default=None, help="also show this filesystem")
    status.add_argument("--top", type=int, default=10)
    status.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="would this job/task/worktree fit?")
    check.add_argument("--for", dest="kind", choices=("job", "task", "worktree"),
                       default="job")
    check.add_argument("--memory-mb", type=float, default=None)
    check.add_argument("--disk-mb", type=float, default=None)
    check.add_argument("--path", default=None)
    check.add_argument("--key", default=None,
                       help="history key (e.g. a command key or task class)")
    check.add_argument("--freeing", action="append", default=None,
                       metavar="MEM_MB:ETA_S",
                       help="an ozagia job expected to free memory; repeatable")
    check.add_argument("--root", default=None)
    check.add_argument("--json", action="store_true")

    history = sub.add_parser("history", help="bounded observed-run history")
    history.add_argument("--key", default=None)
    history.add_argument("--limit", type=int, default=2000)
    history.add_argument("--root", default=None)
    history.add_argument("--json", action="store_true")

    explain = sub.add_parser("explain", help="show the admission checks as text")
    explain.add_argument("--for", dest="kind", choices=("job", "task", "worktree"),
                         default="job")
    explain.add_argument("--memory-mb", type=float, default=None)
    explain.add_argument("--disk-mb", type=float, default=None)
    explain.add_argument("--path", default=None)
    explain.add_argument("--key", default=None)
    explain.add_argument("--root", default=None)
    explain.add_argument("--json", action="store_true")

    grants = sub.add_parser("grants", help="read-only resource-grant readiness")
    actions = grants.add_subparsers(dest="grant_command", required=True)

    glist = actions.add_parser("list", help="list declared resource grants")
    glist.add_argument("--kind", default=None, choices=RG.GRANT_KINDS)
    glist.add_argument("--root", default=None)
    glist.add_argument("--json", action="store_true")

    gshow = actions.add_parser("show", help="show one resource grant")
    gshow.add_argument("id")
    gshow.add_argument("--root", default=None)
    gshow.add_argument("--json", action="store_true")

    gcheck = actions.add_parser("check", help="check grant readiness, read-only")
    gcheck.add_argument("id")
    gcheck.add_argument("--live", action="store_true",
                        help="allow the injected live identity check")
    gcheck.add_argument("--probe-versions", action="store_true",
                        help="run bounded qemu/virsh --version probes")
    gcheck.add_argument("--require-ready", action="store_true",
                        help="exit 1 when the grant is not ready")
    gcheck.add_argument("--root", default=None)
    gcheck.add_argument("--json", action="store_true")

    gclaim = actions.add_parser(
        "claim", help="claim a ready grant (writes the claim ledger)")
    gclaim.add_argument("id")
    gclaim.add_argument("--reason", required=True,
                        help="why this claim is needed (recorded)")
    gclaim.add_argument("--task", default=None)
    gclaim.add_argument("--session", default=None)
    gclaim.add_argument("--approve", action="store_true",
                        help="explicit host approval for this claim")
    gclaim.add_argument("--allow-unbounded", action="store_true",
                        help="explicitly allow an admin/unbounded grant")
    gclaim.add_argument("--allow-keep", action="store_true",
                        help="explicitly allow a keep cleanup policy")
    gclaim.add_argument("--max-usd", type=float, default=None)
    gclaim.add_argument("--max-minutes", type=float, default=None)
    gclaim.add_argument("--root", default=None)
    gclaim.add_argument("--json", action="store_true")

    grelease = actions.add_parser(
        "release", help="preview or apply a claim release")
    grelease.add_argument("claim")
    grelease.add_argument("--apply", action="store_true",
                          help="close the claim; without it this is a preview")
    grelease.add_argument("--observed-usd", type=float, default=None)
    grelease.add_argument("--observed-minutes", type=float, default=None)
    grelease.add_argument("--cleanup", choices=RG.CLEANUP_STATES,
                          default=None)
    grelease.add_argument("--root", default=None)
    grelease.add_argument("--json", action="store_true")

    gstatus = actions.add_parser(
        "status", help="read-only active/expired/orphaned claims")
    gstatus.add_argument("--status", choices=RG.CLAIM_STATES, default=None)
    gstatus.add_argument("--grant", dest="grant_id", default=None)
    gstatus.add_argument("--root", default=None)
    gstatus.add_argument("--json", action="store_true")
    return parser


def _format_budget(budget: dict[str, Any] | None) -> str:
    if not budget:
        return "none"
    if budget.get("unbounded"):
        return "unbounded"
    parts: list[str] = []
    if budget.get("max_usd") is not None:
        parts.append(f"max_usd={budget['max_usd']}")
    if budget.get("max_hours") is not None:
        parts.append(f"max_hours={budget['max_hours']}")
    if budget.get("max_instances") is not None:
        parts.append(f"max_instances={budget['max_instances']}")
    return ", ".join(parts) if parts else "no caps"


def _format_grant_summary(grant: dict[str, Any]) -> str:
    approval = " approval=required" if grant.get("requires_approval") else ""
    return (f"{grant['id']}: {grant['kind']}/{grant['provider']}"
            f"{approval} source={grant['source']}")


def _format_grant(grant: dict[str, Any]) -> str:
    lines = [f"{grant['id']}: {grant['kind']}/{grant['provider']}"]
    if grant.get("description"):
        lines.append(f"  description: {grant['description']}")
    if grant.get("capabilities"):
        lines.append("  capabilities: " + ", ".join(grant["capabilities"]))
    if grant.get("permissions"):
        lines.append("  permissions: " + ", ".join(grant["permissions"])
                     + " (declared, not verified)")
    if grant.get("image"):
        lines.append(f"  image: {grant['image']}")
    if grant.get("region"):
        lines.append(f"  region: {grant['region']}")
    if grant.get("profile"):
        lines.append(f"  profile: {grant['profile']}")
    lines.append(f"  budget: {_format_budget(grant.get('budget'))}")
    lines.append(f"  cleanup: {grant.get('cleanup')}")
    lines.append("  requires_approval: "
                 f"{str(grant.get('requires_approval')).lower()}")
    if grant.get("risk"):
        lines.append(f"  risk: {grant['risk']}")
    lines.append(f"  source: {grant.get('source')}")
    return "\n".join(lines)


def _format_grant_check(report: dict[str, Any]) -> str:
    lines = [f"{report['id']}: {report['state']} "
             f"(ready={str(report['ready']).lower()})"]
    for check in report.get("checks", []):
        lines.append(f"  [{check['level']}] {check['id']}: {check['message']}")
    if report.get("not_assessed"):
        lines.append("  not assessed: " + ", ".join(report["not_assessed"]))
    return "\n".join(lines)


def _format_claim_budget(budget: dict[str, Any] | None) -> str:
    if not budget:
        return "unknown"
    parts: list[str] = []
    if budget.get("unbounded"):
        parts.append("unbounded")
    if budget.get("max_usd") is not None:
        parts.append(f"max_usd={budget['max_usd']}")
    if budget.get("max_minutes") is not None:
        parts.append(f"max_minutes={budget['max_minutes']}")
    if budget.get("max_instances") is not None:
        parts.append(f"max_instances={budget['max_instances']}")
    if budget.get("ttl_minutes") is not None:
        parts.append(f"ttl_minutes={budget['ttl_minutes']}")
    return ", ".join(parts) if parts else "no caps"


def _format_observed(observed: dict[str, Any] | None) -> str:
    observed = observed or {}
    cost = observed.get("cost_usd")
    minutes = observed.get("minutes")
    cost_text = "unknown" if cost is None else str(cost)
    minutes_text = "unknown" if minutes is None else str(minutes)
    return f"cost_usd={cost_text}, minutes={minutes_text}"


def _format_claim(claim: dict[str, Any], *, indent: str = "") -> str:
    orphan = " orphaned=true" if claim.get("orphaned") else ""
    lines = [f"{indent}{claim.get('claim_id')}: {claim.get('status')}{orphan} "
             f"grant={claim.get('grant_id')}"]
    if claim.get("task") or claim.get("session"):
        lines.append(f"{indent}  task={claim.get('task') or '-'} "
                     f"session={claim.get('session') or '-'}")
    lines.append(f"{indent}  started: {claim.get('started')} "
                 f"expires: {claim.get('expires')}")
    approval = claim.get("approval") or {}
    flags = approval.get("flags") or {}
    approved = [name for name in ("approve", "allow_unbounded", "allow_keep")
                if flags.get(name)]
    lines.append(f"{indent}  approval: "
                 f"{'host' if approval.get('granted') else 'not granted'}"
                 + (f" ({', '.join(approved)})" if approved else ""))
    lines.append(f"{indent}  budget: {_format_claim_budget(claim.get('budget'))}")
    cleanup = claim.get("cleanup") or {}
    lines.append(f"{indent}  cleanup: {cleanup.get('policy')}/"
                 f"{cleanup.get('status')} (verified=false)")
    lines.append(f"{indent}  observed: "
                 f"{_format_observed(claim.get('observed'))}")
    if claim.get("reason"):
        lines.append(f"{indent}  reason: {claim.get('reason')}")
    return "\n".join(lines)


def _format_claims(report: dict[str, Any], *, header: str = "claims") -> str:
    counts = report.get("counts") or {}
    cleanup = counts.get("cleanup") or {}
    lines = [f"{header}: total={counts.get('total', 0)} "
             f"active={counts.get('active', 0)} "
             f"expired={counts.get('expired', 0)} "
             f"orphaned={counts.get('orphaned', 0)} "
             f"released={counts.get('released', 0)} "
             f"cleanup(done={cleanup.get('done', 0)},"
             f"failed={cleanup.get('failed', 0)},"
             f"unknown={cleanup.get('unknown', 0)})"]
    for error in report.get("errors") or []:
        lines.append(f"  warning: {error}")
    if not report.get("claims"):
        lines.append("  no claims")
    for claim in report.get("claims") or []:
        lines.append(_format_claim(claim, indent="  "))
    return "\n".join(lines)



def _grants_command(args: argparse.Namespace) -> int:
    root = _root(args.root)
    action = args.grant_command
    if action == "list":
        report = RG.list_grants(root, kind=args.kind)
        if args.json:
            _print_json({
                "ok": True,
                "schema": report["schema"],
                "count": report["count"],
                "grants": report["grants"],
                "errors": report["errors"],
            })
        else:
            for error in report["errors"]:
                print(f"warning: {error}", file=sys.stderr)
            if not report["grants"]:
                print("no resource grants")
            for grant in report["grants"]:
                print(_format_grant_summary(grant))
        return 0
    if action == "show":
        grant = RG.show_grant(root, args.id)
        if args.json:
            _print_json({"ok": True, "schema": RG.SCHEMA,
                         "grant": grant.to_dict()})
        else:
            print(_format_grant(grant.to_dict()))
        return 0
    if action == "check":
        report = RG.check_grant(
            root, args.id, live=args.live,
            probe_versions=args.probe_versions)
        if args.json:
            _print_json(report)
        else:
            print(_format_grant_check(report))
        if args.require_ready and not report["ready"]:
            return 1
        return 0
    if action == "claim":
        budget = {}
        if args.max_usd is not None:
            budget["max_usd"] = args.max_usd
        if args.max_minutes is not None:
            budget["max_minutes"] = args.max_minutes
        record = RG.claim(
            root, args.id, reason=args.reason, task=args.task,
            session=args.session, approve=args.approve,
            allow_unbounded=args.allow_unbounded,
            allow_keep=args.allow_keep, budget=budget or None)
        if args.json:
            _print_json({"ok": True, "schema": RG.CLAIM_SCHEMA,
                         "claim": record})
        else:
            print(_format_claim(record))
        return 0
    if action == "release":
        observed: dict[str, Any] = {}
        if args.observed_usd is not None:
            observed["cost_usd"] = args.observed_usd
        if args.observed_minutes is not None:
            observed["minutes"] = args.observed_minutes
        if args.cleanup is not None:
            observed["cleanup"] = args.cleanup
        report = RG.release(root, args.claim, apply=args.apply,
                            observed=observed or None)
        if args.json:
            _print_json({"ok": True, **report})
        else:
            head = ("release preview (dry-run; pass --apply to close)"
                    if report.get("dry_run") else "released")
            print(f"{head}: {report['claim_id']}")
            print(_format_claim(report, indent="  "))
        return 0
    if action == "status":
        report = RG.claims(root, status=args.status,
                           grant_id=args.grant_id)
        if args.json:
            _print_json({"ok": True, **report})
        else:
            print(_format_claims(report))
        return 0
    raise ValueError(f"unknown grants command {action!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(args.root)
    try:
        if args.command == "grants":
            return _grants_command(args)
        if args.command == "status":
            report = R.status(root, path=args.path, top=args.top)
            if args.json:
                _print_json(report)
            else:
                print(R.format_status(report))
            return 0
        if args.command == "history":
            report = R.history_summary(root, key=args.key, limit=args.limit)
            if args.json:
                _print_json(report)
            else:
                if not report:
                    print("no history")
                for key, item in report.items():
                    memory = (item.get("memory_mb") or {}).get("p90")
                    print(f"{key}: samples={item.get('samples')} "
                          f"confidence={item.get('confidence')} "
                          f"p90_mb={memory}")
            return 0
        policy = R.load_policy(root)
        report = R.check_admission(
            root, kind=args.kind, memory_mb=args.memory_mb,
            disk_mb=args.disk_mb, path=args.path, key=args.key,
            freeing=_freeing(getattr(args, "freeing", None)), policy=policy,
        )
        if args.json:
            _print_json(report)
        else:
            print(R.format_check(report))
        return 0 if report.get("ok") else 1
    except RG.GrantRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
