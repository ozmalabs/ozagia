"""`replace`: one pattern replaced across files (`ozagia.edit.replace`).

Input: `pattern`, `replacement`, and optionally `regex`, `flags`, `glob` /
`globs`, `key` / `keys`, `max_files`, `max_matches`, `expect`,
`allow_parse_errors`, `gitignore`, `any_suffix`, `base_shas`, `plan_id`,
and `preview` (show the change, write nothing). The scan's deadline is the
request's remaining time (`_reply_deadline`), else
`ozagia.edit.replace.SCAN_TIMEOUT_S`.

Permission. An apply asks `edit` for the files it changes: a preflight
stops after the scan with `{action: "edit", resources: <files>, metadata:
{files: <diffs>, plan_id}}`. The apply that follows is bound to that plan
(`_granted_plan_id`, set by the daemon): a plan that changed since (another
file now matches, a file's bytes changed) refuses the call, so the written
files never exceed the granted ones. A preview writes nothing: it is not
preflighted, and its permission is `read` on the same files.

Applied:

    Replaced <n> match(es) in <k> file(s)
    [ozagia] <rel>: <m> match(es) in <keys>
    [ozagia] delta: ...
    [ozagia] lint <rule> L<n> <key>: <message>

Preview (`preview: true`):

    Preview: <n> match(es) in <k> file(s); nothing written
    [ozagia] <rel>: <m> match(es) in <keys>
    <the file's unified diff, capped>
    [ozagia] plan_id <id> (pass it to apply exactly this)

A refusal (expect mismatch, a cap, the scan deadline, a stale preview or
plan, a parse the replace would break, a failed write whose batch was
restored) is a tool error with the reason and the per-file lines. A write
that failed and could not be fully restored is a tool error that leads
with `Replace partially applied:` and names the files left written.
"""
from __future__ import annotations

from typing import Any

from ozagia.intent.common import (
    IntentError,
    delta_keys,
    delta_lines,
    preflight_stop,
    tag,
)

# Files listed in a reply, and diff lines shown in a preview reply.
REPLY_FILES = 50
PREVIEW_DIFF_LINES = 400


def _list(inp: dict[str, Any], one: str, many: str) -> list[str] | None:
    vals: list[str] = []
    for name in (one, many):
        v = inp.get(name)
        if isinstance(v, str) and v.strip():
            vals.append(v)
        elif isinstance(v, list):
            vals.extend(str(x) for x in v if isinstance(x, str) and x.strip())
    return vals or None


def _int(inp: dict[str, Any], name: str) -> int | None:
    v = inp.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise IntentError(f"{name} must be an integer")
    return v


def _timeout(inp: dict[str, Any]) -> float | None:
    """Seconds left before the request's reply deadline, or None."""
    import time
    at = inp.get("_reply_deadline")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return at - time.monotonic()


def request(inp: dict[str, Any]) -> dict[str, Any]:
    """The `ozagia.edit.replace.replace` keyword arguments of an input."""
    pattern, replacement = inp.get("pattern"), inp.get("replacement")
    if not isinstance(pattern, str) or not pattern:
        raise IntentError("pattern must be a non-empty string")
    if not isinstance(replacement, str):
        raise IntentError("replacement must be a string")
    shas = inp.get("base_shas")
    return {
        "pattern": pattern, "replacement": replacement,
        "regex": bool(inp.get("regex")), "flags": inp.get("flags"),
        "globs": _list(inp, "glob", "globs"),
        "keys": _list(inp, "key", "keys"),
        "max_files": _int(inp, "max_files"),
        "max_matches": _int(inp, "max_matches"),
        "expect": _int(inp, "expect"),
        "gitignore": inp.get("gitignore") is not False,
        "any_suffix": inp.get("any_suffix") is True,
        "timeout_s": _timeout(inp),
        "allow_parse_errors": bool(inp.get("allow_parse_errors")),
        "base_shas": shas if isinstance(shas, dict) else None,
        "plan_id": inp.get("plan_id") if isinstance(inp.get("plan_id"), str)
        else None,
    }


def preview_files(memory: Any, inp: dict[str, Any]) -> list[str]:
    """The files a `replace` call would change (no counters, no writes)."""
    from ozagia.edit.replace import replace
    from ozagia.intent.common import root_of
    kw = request(inp)
    kw.pop("base_shas"), kw.pop("plan_id")
    res = replace(root_of(memory), kw.pop("pattern"), kw.pop("replacement"),
                  memory=memory, **kw)
    return list(res.get("shas") or {})


def file_lines(res: dict[str, Any]) -> list[str]:
    out = []
    files = res.get("files") or []
    for f in files[:REPLY_FILES]:
        k = f["matches"]
        what = "unchanged" if f.get("unchanged") else ", ".join(f.get("keys") or [])
        note = ""
        if f.get("parse_check") == "refused":
            pe = f.get("parse_error") or {}
            note = (f"  (would not parse: {pe.get('message')} at line "
                    f"{pe.get('line')})")
        out.append(tag(f"{f['file']}: {k} match{'es' if k != 1 else ''} in "
                       f"{what}{note}"))
    if len(files) > REPLY_FILES:
        out.append(tag(f"... {len(files) - REPLY_FILES} more files"))
    skipped = res.get("skipped") or {}
    if skipped:
        out.append(tag("skipped: " + ", ".join(
            f"{n} {why}" for why, n in sorted(skipped.items()))))
    return out


def lint_replace(memory: Any, res: dict[str, Any],
                 inp: dict[str, Any] | None = None
                 ) -> tuple[list[str], dict[str, int]]:
    """Lint lines for the lines an applied replace changed
    (`ozagia.intent.lint`); never raises."""
    try:
        from ozagia.edit.replace import changed_texts
        from ozagia.intent.common import root_of
        from ozagia.intent.lint import (
            applies,
            changed_lines,
            lint_changes,
            lint_meta,
        )
        root = root_of(memory)
        changes: dict[str, Any] = {}
        for rel, before in changed_texts(root, res):
            if not applies(memory, rel):
                continue
            after = (root / rel).read_text(encoding="utf-8", errors="replace")
            changes[rel] = changed_lines(before, after)
        if not changes:
            return [], {}
        got = lint_changes(memory, changes, inp=inp)
        return list(got["lines"]), lint_meta(got)
    except Exception:  # noqa: BLE001 - lint never fails the edit
        return [], {}


def _metadata(res: dict[str, Any]) -> dict[str, Any]:
    return {"files": [{"file": f["file"], "patch": f.get("diff") or "",
                       "status": "modified",
                       "additions": f.get("additions", 0),
                       "deletions": f.get("deletions", 0)}
                      for f in res.get("files") or [] if not f.get("unchanged")],
            "plan_id": res.get("plan_id")}


def _refusal(res: dict[str, Any]) -> IntentError:
    lines = [f"Replace refused: {res.get('error')}", *file_lines(res)]
    return IntentError("\n".join(lines),
                       keys=[k for f in res.get("files") or []
                             for k in f.get("keys") or []],
                       intent="replace_refused")


def _run(memory: Any, kw: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    from ozagia.edit.code_edit import CodeEditError
    args = dict(kw)
    try:
        res = memory.replace(args.pop("pattern"), args.pop("replacement"),
                             apply=apply, **args)
    except (CodeEditError, ValueError) as e:
        raise IntentError(str(e)) from None
    if not res.get("ok") and not res.get("applied"):
        raise _refusal(res)
    return res


def _preview_reply(res: dict[str, Any], kw: dict[str, Any]) -> dict[str, Any]:
    n, k = res["total"], res["files_changed"]
    lines = [f"Preview: {n} match{'es' if n != 1 else ''} in {k} "
             f"file{'s' if k != 1 else ''}; nothing written",
             *file_lines(res)]
    if res.get("parse_refused") and not kw["allow_parse_errors"]:
        lines.append(tag(f"an apply would be refused: it breaks the parse "
                         f"of {', '.join(res['parse_refused'])} (set "
                         f"allow_parse_errors to write anyway)"))
    budget = PREVIEW_DIFF_LINES
    for f in res["files"]:
        body = (f.get("diff") or "").splitlines()
        if not body or budget <= 0:
            continue
        lines.extend(body[:budget])
        if f.get("diff_truncated") or len(body) > budget:
            lines.append(tag(f"diff of {f['file']} truncated"))
        budget -= len(body)
    lines.append(tag(f"plan_id {res['plan_id']} (pass it to apply exactly "
                     f"this)"))
    keys = list(dict.fromkeys(k for f in res["files"]
                              for k in f.get("keys") or []))
    return {"intent": "replace_preview", "output": "\n".join(lines),
            "keys": keys, "meta": {"replace_previews": 1},
            "edited_files": [],
            "permission": {"action": "read", "resources": list(res["shas"]),
                           "save": ["*"]}}


def handle_replace(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    kw = request(inp)
    if inp.get("preview"):
        # Writes nothing: no preflight, no edit permission.
        res = _run(memory, kw, apply=False)
        if not res.get("shas"):
            raise IntentError("Replace refused: no file would change")
        return _preview_reply(res, kw)
    if inp.get("_preflight"):
        res = _run(memory, kw, apply=False)
        if not res.get("shas"):
            raise IntentError("Replace refused: no file would change")
        if res.get("parse_refused") and not kw["allow_parse_errors"]:
            raise IntentError("\n".join([
                f"Replace refused: it breaks the parse of "
                f"{', '.join(res['parse_refused'])}; set "
                f"allow_parse_errors to write anyway", *file_lines(res)]),
                intent="replace_refused")
        if kw["plan_id"] and kw["plan_id"] != res["plan_id"]:
            raise IntentError(
                f"Replace refused: stale preview: plan {kw['plan_id']} is now "
                f"{res['plan_id']} (the files or the request changed); "
                f"preview again", intent="replace_refused")
        preflight_stop(inp, _metadata(res),
                       permission={"action": "edit",
                                   "resources": list(res["shas"]),
                                   "save": ["*"]})
    granted = inp.get("_granted_plan_id")
    if isinstance(granted, str) and granted:
        if kw["plan_id"] and kw["plan_id"] != granted:
            raise IntentError(
                f"Replace refused: plan_id {kw['plan_id']} is not the "
                f"granted plan {granted}; preview again",
                intent="replace_refused")
        kw["plan_id"] = granted
    res = _run(memory, kw, apply=True)
    written = list(res.get("written") or [])
    n, k = int(res.get("written_matches") or 0), len(written)
    head = (f"Replaced {n} match{'es' if n != 1 else ''} in {k} "
            f"file{'s' if k != 1 else ''}")
    partial = bool(res.get("partial")) or not res.get("ok")
    lines = ([f"Replace partially applied: {res.get('error')}",
              tag(f"left written ({n} match{'es' if n != 1 else ''}): "
                  f"{', '.join(written)}")] if partial else [head])
    lines.extend(file_lines(res))
    if res.get("warning"):
        lines.append(tag(f"warning: {res['warning']}"))
    delta = res.get("graph_delta")
    lines.extend(delta_lines(delta))
    lint_lines, lint_meta = lint_replace(memory, res, inp)
    lines.extend(lint_lines)
    keys = list(dict.fromkeys([*(k for f in res["files"] if f.get("applied")
                                 for k in f.get("keys") or []),
                               *delta_keys(delta)]))
    meta: dict[str, Any] = {"replace_applies": 1, "replace_files": k,
                            **lint_meta}
    if partial:
        raise IntentError("\n".join(lines), keys=keys,
                          intent="replace_partial",
                          meta={**meta, "edited_files": written,
                                "replace_partial": 1})
    return {"intent": "replace", "output": "\n".join(lines), "keys": keys,
            "meta": meta, "edited_files": written,
            "permission": {"action": "edit", "resources": written,
                           "save": ["*"]}}


__all__ = ["file_lines", "handle_replace", "lint_replace", "preview_files",
           "request"]
