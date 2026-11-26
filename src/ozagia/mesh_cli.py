"""``ozagia mesh``: local session registry, messages and advisory leases.

    ozagia mesh status|peers [--json] [--all] [--strict]
    ozagia mesh register --harness H --session S [--pid N] [--worktree P]
                         [--branch B] [--exposure none|read|coordinate|supervise]
    ozagia mesh unregister [--harness H --session S] [--release-leases]
    ozagia mesh heartbeat [--harness H --session S]
    ozagia mesh send (--to H:S | --broadcast) --kind K --subject S
                     [--body TEXT | --body-file F] [--in-reply-to ID]
                     [--lease ID] [--ttl S]
    ozagia mesh inbox [--unread]
    ozagia mesh read MSG_ID [--peek]
    ozagia mesh ack MSG_ID
    ozagia mesh lease list|status [LEASE_ID]
    ozagia mesh lease acquire --kind K --id RESOURCE --mode read|write|merge
    ozagia mesh lease renew|release LEASE_ID
    ozagia mesh lease takeover LEASE_ID --confirm --reason TEXT
    ozagia mesh worktree request --branch B [--path P]
    ozagia mesh worktree offer [--branch B] [--path P] [--lease]
    ozagia mesh merge request --target BRANCH|PATH
    ozagia mesh prune (--expired | --delivered-before S) [--apply]

Identity defaults to ``OZAGIA_MESH_HARNESS``/``OZAGIA_MESH_SESSION``.  Every
write requires an identity; sends and lease mutations also require the
registered session to have exposure ``coordinate`` (or ``supervise``).
Read-only status/peers need no identity.

Exit status: 0 success; 1 a valid refusal such as queued, held, expired holder,
unknown peer or a full mailbox; 2 usage, configuration, IO, lock or schema
failure.  ``status/peers --strict`` returns 1 when a stale, dead, foreign or
orphaned record is present.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import mesh as M
from ozagia.core import security as SEC

PROG = "ozagia mesh"


def _add_common(child: argparse.ArgumentParser) -> None:
    child.add_argument("--root", default=None, help="repository root")
    child.add_argument("--json", action="store_true", help="print JSON")


def _add_identity(child: argparse.ArgumentParser) -> None:
    child.add_argument("--harness", default=None,
                       help="harness name (default: OZAGIA_MESH_HARNESS)")
    child.add_argument("--session", default=None,
                       help="session id (default: OZAGIA_MESH_SESSION)")


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "mesh", help="local session peers, messages and advisory leases",
        description="Coordinate live local sessions through a bounded "
                    "filesystem registry.  Leases are advisory; a merge "
                    "request never runs git.")
    children = parser.add_subparsers(dest="action", required=True)

    for name, text in (("status", "show sessions, leases and counts"),
                       ("peers", "list live or stale sessions")):
        item = children.add_parser(name, help=text)
        _add_common(item)
        item.add_argument("--all", action="store_true",
                          help="include ended, dead and foreign sessions")
        item.add_argument("--strict", action="store_true",
                          help="exit 1 when a stale, dead or orphaned entry "
                               "is present")

    registered = children.add_parser("register", help="register this session")
    _add_common(registered)
    _add_identity(registered)
    registered.add_argument("--pid", type=int, default=None)
    registered.add_argument("--worktree", default=None)
    registered.add_argument("--branch", default=None)
    registered.add_argument("--exposure", default="none", choices=M.EXPOSURES)
    registered.add_argument("--source", default="cli", choices=M.SOURCES)
    registered.add_argument("--capability", action="append", default=None)

    unregistered = children.add_parser("unregister", help="mark a session ended")
    _add_common(unregistered)
    _add_identity(unregistered)
    unregistered.add_argument("--release-leases", action="store_true",
                              help="release active leases held by this session")

    beat = children.add_parser("heartbeat", help="refresh the session heartbeat")
    _add_common(beat)
    _add_identity(beat)
    beat.add_argument("--pid", type=int, default=None)

    sent = children.add_parser("send", help="send one bounded message")
    _add_common(sent)
    _add_identity(sent)
    target = sent.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", default=None, metavar="H:S")
    target.add_argument("--broadcast", action="store_true")
    sent.add_argument("--kind", required=True, choices=M.KINDS)
    sent.add_argument("--subject", required=True)
    body = sent.add_mutually_exclusive_group()
    body.add_argument("--body", default=None)
    body.add_argument("--body-file", default=None, metavar="F")
    sent.add_argument("--in-reply-to", default=None)
    sent.add_argument("--lease", default=None)
    sent.add_argument("--ttl", type=int, default=M.MESSAGE_TTL_S)

    inbox = children.add_parser("inbox", help="list message metadata")
    _add_common(inbox)
    _add_identity(inbox)
    inbox.add_argument("--unread", action="store_true")

    read = children.add_parser("read", help="read one delivered message")
    _add_common(read)
    _add_identity(read)
    read.add_argument("msg_id")
    read.add_argument("--peek", action="store_true",
                      help="do not mark the message read")

    ack = children.add_parser("ack", help="mark one message read")
    _add_common(ack)
    _add_identity(ack)
    ack.add_argument("msg_id")

    lease = children.add_parser("lease", help="advisory resource leases")
    lease_children = lease.add_subparsers(dest="lease_action", required=True)

    listed = lease_children.add_parser("list", help="list leases")
    _add_common(listed)
    listed.add_argument("--all", action="store_true", default=True,
                        help=argparse.SUPPRESS)

    lease_status = lease_children.add_parser("status", help="lease or mesh status")
    _add_common(lease_status)
    lease_status.add_argument("lease_id", nargs="?", default=None)

    acquire = lease_children.add_parser("acquire", help="acquire or queue a lease")
    _add_common(acquire)
    _add_identity(acquire)
    acquire.add_argument("--kind", required=True, choices=M.RESOURCE_KINDS)
    acquire.add_argument("--id", dest="resource_id", required=True,
                         metavar="RESOURCE")
    acquire.add_argument("--mode", required=True, choices=M.MODES)
    acquire.add_argument("--ttl", type=int, default=M.LEASE_TTL_S)
    acquire.add_argument("--reason", default="")

    renew = lease_children.add_parser("renew", help="renew a held lease")
    _add_common(renew)
    _add_identity(renew)
    renew.add_argument("lease_id")
    renew.add_argument("--ttl", type=int, default=None)

    release = lease_children.add_parser("release", help="release a held lease")
    _add_common(release)
    _add_identity(release)
    release.add_argument("lease_id")

    takeover = lease_children.add_parser(
        "takeover", help="take over an explicitly expired lease")
    _add_common(takeover)
    _add_identity(takeover)
    takeover.add_argument("lease_id")
    takeover.add_argument("--confirm", action="store_true",
                          help="required; takeover is deliberately explicit")
    takeover.add_argument("--reason", required=True)
    takeover.add_argument("--ttl", type=int, default=M.LEASE_TTL_S)

    tree = children.add_parser("worktree", help="worktree lease requests and offers")
    tree_children = tree.add_subparsers(dest="worktree_action", required=True)
    request = tree_children.add_parser(
        "request", help="lease an existing matching worktree and branch")
    _add_common(request)
    _add_identity(request)
    request.add_argument("--branch", default=None)
    request.add_argument("--path", default=None)
    request.add_argument("--reason", default="")
    offer = tree_children.add_parser(
        "offer", help="validate and optionally advertise a free worktree")
    _add_common(offer)
    _add_identity(offer)
    offer.add_argument("--branch", default=None)
    offer.add_argument("--path", default=None)
    offer.add_argument("--lease", action="store_true",
                       help="also acquire read leases")
    offer_target = offer.add_mutually_exclusive_group()
    offer_target.add_argument("--to", default=None, metavar="H:S")
    offer_target.add_argument("--broadcast", action="store_true")

    merge = children.add_parser("merge", help="advisory merge lease negotiation")
    merge_children = merge.add_subparsers(dest="merge_action", required=True)
    merge_request = merge_children.add_parser(
        "request", help="acquire or queue the exclusive merge lease")
    _add_common(merge_request)
    _add_identity(merge_request)
    merge_request.add_argument("--target", required=True,
                               metavar="BRANCH|PATH")
    merge_request.add_argument("--ttl", type=int, default=M.LEASE_TTL_S)
    merge_request.add_argument("--reason", default="")

    prune = children.add_parser("prune", help="explicitly remove read messages")
    _add_common(prune)
    _add_identity(prune)
    which = prune.add_mutually_exclusive_group(required=True)
    which.add_argument("--expired", action="store_true")
    which.add_argument("--delivered-before", type=float, default=None,
                       metavar="S")
    prune.add_argument("--apply", action="store_true",
                       help="delete the planned messages")
    prune.add_argument("--root-ignored", action="store_true",
                       help=argparse.SUPPRESS)


def _root(args: argparse.Namespace) -> Path:
    value = getattr(args, "root", None)
    return Path(value).resolve() if value else repo_root()


def _print_json(value: Any) -> None:
    print(SEC.redact_text(json.dumps(value, indent=2, sort_keys=True,
                                     default=str)))


def _identity(args: argparse.Namespace) -> tuple[str, str]:
    harness = getattr(args, "harness", None) or os.environ.get(
        "OZAGIA_MESH_HARNESS")
    session = getattr(args, "session", None) or os.environ.get(
        "OZAGIA_MESH_SESSION")
    if not harness or not session:
        raise M.MeshConfigError(
            "an identity is required: --harness/--session or "
            "OZAGIA_MESH_HARNESS/OZAGIA_MESH_SESSION")
    return str(harness), str(session)


def _coordinate(root: Path, args: argparse.Namespace) -> tuple[str, str]:
    harness, session = _identity(args)
    state = M.status(root, harness=harness, session=session)
    me = state.get("self")
    if me is None:
        raise M.MeshRefusal("session is not registered",
                            code="unknown_session")
    if me.get("effective_state") != "live":
        raise M.MeshRefusal(
            f"session is {me.get('effective_state')}, not live",
            code="session_not_live")
    if me.get("exposure") not in M.COORDINATE:
        raise M.MeshRefusal(
            "this command needs exposure=coordinate; re-register with "
            "--exposure coordinate", code="coordinate_required")
    return harness, session


def _print_session(item: dict[str, Any]) -> str:
    lease_text = ",".join(item.get("active_leases") or ()) or "-"
    return (f"{item.get('harness')}:{item.get('session')}  "
            f"{item.get('effective_state'):<7} exposure={item.get('exposure')} "
            f"worktree={item.get('worktree') or '-'} "
            f"branch={item.get('branch') or '-'} "
            f"unread={item.get('unread', 0)} leases={lease_text}")


def _print_lease(item: dict[str, Any]) -> str:
    resource = item.get("resource") or {}
    holder = item.get("holder") or {}
    queue = len(item.get("queue") or ())
    return (f"{item.get('lease_id')}  {item.get('effective_state'):<8} "
            f"{resource.get('kind')}:{resource.get('id')} "
            f"mode={resource.get('mode')} "
            f"holder={holder.get('harness')}:{holder.get('session')} "
            f"expires_in={item.get('expires_in_s')} queue={queue}")


def _body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        path = Path(args.body_file)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise M.MeshError(f"cannot read body file: {exc}") from exc
        if len(text) > M.BODY_MAX:
            raise M.MeshRefusal(
                f"body file exceeds {M.BODY_MAX} characters",
                code="too_large")
        return text
    return str(getattr(args, "body", None) or "")


def _strict_exit(payload: dict[str, Any]) -> int:
    if payload.get("errors"):
        return 1
    for item in payload.get("peers") or payload.get("sessions") or ():
        if item.get("effective_state") in {"stale", "dead", "foreign",
                                           "unknown"}:
            return 1
    counts = payload.get("lease_counts") or payload.get("counts") or {}
    if counts.get("orphaned") or counts.get("expired") or counts.get("stale"):
        return 1
    return 0


def run(args: argparse.Namespace) -> int:
    root = _root(args)
    as_json = bool(getattr(args, "json", False))
    action = args.action
    try:
        if action == "status":
            harness = os.environ.get("OZAGIA_MESH_HARNESS")
            session = os.environ.get("OZAGIA_MESH_SESSION")
            payload = M.status(root, harness=harness, session=session)
            if as_json:
                _print_json(payload)
            else:
                print(f"repo={payload['repo_id']} mesh={payload['mesh_id']}")
                for item in payload.get("sessions", ()):
                    print(_print_session(item))
                counts = payload.get("lease_counts") or {}
                print("sessions: " + " ".join(
                    f"{key}={value}" for key, value in sorted(
                        payload.get("session_counts", {}).items())))
                print("leases: " + " ".join(
                    f"{key}={value}" for key, value in sorted(counts.items())))
            return _strict_exit(payload) if args.strict else 0
        if action == "peers":
            payload = M.peers(root, include_all=args.all)
            if as_json:
                _print_json(payload)
            else:
                print(f"repo={payload['repo_id']} mesh={payload['mesh_id']}")
                for item in payload.get("peers", ()):
                    print(_print_session(item))
            return _strict_exit(payload) if args.strict else 0
        if action == "register":
            harness, session = _identity(args)
            payload = M.register(
                root, harness, session, pid=args.pid,
                worktree=args.worktree, branch=args.branch,
                exposure=args.exposure, source=args.source,
                capabilities=args.capability or ())
            if as_json:
                _print_json(payload)
            else:
                print(_print_session(payload))
            return 0
        if action == "unregister":
            harness, session = _identity(args)
            payload = M.unregister(root, harness, session,
                                   release_leases=args.release_leases)
            if as_json:
                _print_json(payload)
            else:
                print(f"ended {harness}:{session}")
            return 0
        if action == "heartbeat":
            harness, session = _identity(args)
            payload = M.heartbeat(root, harness, session, pid=args.pid)
            if as_json:
                _print_json(payload)
            else:
                print(f"heartbeat {harness}:{session}")
            return 0
        if action == "send":
            harness, session = _coordinate(root, args)
            payload = M.send(
                root, harness, session, kind=args.kind, subject=args.subject,
                body=_body(args), to=args.to, broadcast=args.broadcast,
                in_reply_to=args.in_reply_to, lease_id=args.lease,
                ttl_s=args.ttl)
            if as_json:
                _print_json(payload)
            else:
                print(f"delivered={payload['delivered']} "
                      f"to={','.join(payload['recipients']) or '-'}")
            return 0
        if action == "inbox":
            harness, session = _identity(args)
            payload = M.inbox(root, harness, session, unread=args.unread)
            if as_json:
                _print_json(payload)
            else:
                for item in payload.get("messages", ()):
                    sender = item.get("from") or {}
                    print(f"{item.get('msg_id')}  {item.get('kind')}  "
                          f"from={sender.get('harness')}:{sender.get('session')} "
                          f"read={bool(item.get('read_at'))} "
                          f"expired={bool(item.get('expired'))} "
                          f"{item.get('subject')}")
                print(f"count={payload.get('count', 0)} "
                      f"unread={payload.get('unread', 0)}")
            return 0
        if action == "read":
            harness, session = _identity(args)
            payload = M.read(root, harness, session, args.msg_id,
                             peek=args.peek)
            if as_json:
                _print_json(payload)
            else:
                sender = payload.get("from") or {}
                print(f"{payload.get('msg_id')} {payload.get('kind')} "
                      f"from={sender.get('harness')}:{sender.get('session')}")
                print(f"subject: {payload.get('subject')}")
                if payload.get("body"):
                    print(payload["body"])
                print(f"unread={payload.get('unread')}")
            return 0
        if action == "ack":
            harness, session = _identity(args)
            payload = M.ack(root, harness, session, args.msg_id)
            if as_json:
                _print_json(payload)
            else:
                print(f"ack {args.msg_id} unread={payload.get('unread')}")
            return 0
        if action == "lease":
            return _run_lease(root, args, as_json)
        if action == "worktree":
            return _run_worktree(root, args, as_json)
        if action == "merge":
            harness, session = _coordinate(root, args)
            payload = M.merge_request(
                root, harness, session, target=args.target, ttl_s=args.ttl,
                reason=args.reason)
            if as_json:
                _print_json(payload)
            else:
                print(_print_lease(payload["lease"]))
                print(f"advisory=true target={payload.get('target')}")
            return 0 if payload.get("acquired") else 1
        if action == "prune":
            _identity(args)
            payload = M.prune(
                root, expired=args.expired,
                delivered_before=args.delivered_before, apply=args.apply)
            if as_json:
                _print_json(payload)
            else:
                verb = "removed" if args.apply else "would remove"
                print(f"{verb} {len(payload.get('candidates', ()))} "
                      f"read messages")
                for item in payload.get("candidates", ()):
                    print(f"  {item['msg_id']} {item['mailbox']} "
                          f"{item['reason']}")
            return 0
    except M.MeshRefusal as exc:
        return _refusal(exc, as_json)
    except M.MeshError as exc:
        return _error(exc, as_json)
    except (OSError, ValueError) as exc:
        return _error(M.MeshError(str(exc)), as_json)
    raise M.MeshError(f"unknown mesh action {action!r}")


def _run_lease(root: Path, args: argparse.Namespace, as_json: bool) -> int:
    action = args.lease_action
    if action in {"list", "status"}:
        payload = (M.lease_status(root, args.lease_id) if action == "status"
                   and args.lease_id else M.lease_list(root))
        if as_json:
            _print_json(payload)
        elif action == "status" and args.lease_id:
            print(_print_lease(payload["lease"]))
        else:
            for item in payload.get("leases", ()):
                print(_print_lease(item))
            print("counts: " + " ".join(
                f"{key}={value}" for key, value in sorted(
                    (payload.get("counts") or {}).items())))
        return 0
    if action == "acquire":
        harness, session = _coordinate(root, args)
        payload = M.lease_acquire(
            root, harness, session, kind=args.kind,
            resource_id=args.resource_id, mode=args.mode, ttl_s=args.ttl,
            reason=args.reason)
        if as_json:
            _print_json(payload)
        else:
            print(_print_lease(payload["lease"]))
            if payload.get("queued"):
                holder = payload.get("holder") or {}
                print(f"queued position={payload.get('position')} "
                      f"holder={holder.get('harness')}:{holder.get('session')}")
        return 0 if payload.get("acquired") else 1
    if action == "renew":
        harness, session = _coordinate(root, args)
        payload = M.lease_renew(root, harness, session, args.lease_id,
                                ttl_s=args.ttl)
        if as_json:
            _print_json(payload)
        else:
            print(_print_lease(payload["lease"]))
        return 0
    if action == "release":
        harness, session = _coordinate(root, args)
        payload = M.lease_release(root, harness, session, args.lease_id)
        if as_json:
            _print_json(payload)
        else:
            print(_print_lease(payload["lease"]))
            if payload.get("promoted"):
                print("promoted " + _print_lease(payload["promoted"]))
            for item in payload.get("skipped") or ():
                entry = item.get("entry") or {}
                print(f"skipped {entry.get('harness')}:{entry.get('session')} "
                      f"({item.get('liveness')})")
        return 0
    if action == "takeover":
        harness, session = _coordinate(root, args)
        payload = M.lease_takeover(
            root, harness, session, args.lease_id, confirm=args.confirm,
            reason=args.reason, ttl_s=args.ttl)
        if as_json:
            _print_json(payload)
        else:
            print(_print_lease(payload["lease"]))
        return 0
    raise M.MeshError(f"unknown lease action {action!r}")


def _run_worktree(root: Path, args: argparse.Namespace, as_json: bool) -> int:
    action = args.worktree_action
    if action == "request":
        harness, session = _coordinate(root, args)
        payload = M.worktree_request(
            root, harness, session, branch=args.branch, path=args.path,
            reason=args.reason)
        if as_json:
            _print_json(payload)
        else:
            item = payload.get("worktree") or {}
            print(f"worktree={item.get('path')} branch={item.get('branch')} "
                  f"acquired={payload.get('acquired')} "
                  f"queued={payload.get('queued')}")
            if payload.get("lease"):
                print(_print_lease(payload["lease"]))
            if payload.get("branch_lease"):
                print(_print_lease(payload["branch_lease"]))
        return 0 if payload.get("acquired") else 1
    if action == "offer":
        if args.lease or args.to or args.broadcast:
            harness, session = _coordinate(root, args)
        else:
            harness, session = "", ""
        payload = M.worktree_offer(
            root, harness, session, branch=args.branch, path=args.path,
            lease=args.lease, to=args.to, broadcast=args.broadcast)
        if as_json:
            _print_json(payload)
        else:
            item = payload.get("worktree") or {}
            print(f"offer worktree={item.get('path')} "
                  f"branch={item.get('branch')}")
            for lease in payload.get("leases") or ():
                if lease:
                    print(_print_lease(lease))
        return 0
    raise M.MeshError(f"unknown worktree action {action!r}")


def _refusal(exc: M.MeshRefusal, as_json: bool) -> int:
    if as_json:
        _print_json({"ok": False, "code": exc.code, "error": str(exc)})
    else:
        print(f"error: {exc}", file=sys.stderr)
    return 1


def _error(exc: M.MeshError, as_json: bool) -> int:
    if as_json:
        _print_json({"ok": False, "code": exc.code, "error": str(exc)})
    else:
        print(f"error: {exc}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    # The top-level CLI owns the parser; this entry point exists for direct
    # module use and tests.
    if argv is None:
        argv = sys.argv[1:]
    from ozagia import cli
    return cli.main(["mesh", *argv])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
