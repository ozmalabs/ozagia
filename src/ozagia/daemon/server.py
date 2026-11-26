"""The graph daemon: `ozagia.intent` behind the Unix socket protocol.

`serve(root)` loads the snapshot (building it when missing), listens on
`protocol.socket_path(root)` and answers the protocol's JSON-line requests
until `shutdown`, SIGTERM or SIGINT, or, with `parent_pid`, until that
process is gone (checked every `PARENT_POLL_S` seconds). Connections are
served by threads; one global lock serialises `tool` and `after`, because
`DevMemory` is not thread-safe. `ping`, `hello` and `stats` do not take that
lock, and neither does a captured pytest, cargo test or `ruff check` (`shell`
that classifies as `pytest`, `cargo_test` or `ruff`): it reads no graph state
while it runs, so a long run does not hold up the edits and reads sent
meanwhile. Lint after an edit (`ozagia.intent.lint`) releases the lock while
its linters run (`_unlock`), and its budget ends a margin before
`deadline_ms` (`_reply_deadline`).

A `tool` or `after` request whose `deadline_ms` (Unix epoch, ms) has passed
when its turn comes (after the lock is taken, before the handler runs) is
answered `expired` and not executed (see `ozagia.daemon.protocol`).

Captured pytest, captured cargo test, captured `ruff check` and lint run with
`test_env` (default: this process's environment without `OZAGIA_*`,
`PYTHONPATH` and `COLUMNS`, the variables the harness adds for ozagia itself),
passed to the intent layer as `_test_env` (for `tool` and `after`). Reserved
`_` fields a client puts in `input` are dropped, except `_reason`: the bounded
one-line label the created-file ledger records for a handled write or an
`after`
that created files.

Every request is journaled, one JSON object per line, to
`<state dir>/daemon_journal.jsonl` (or `journal`):

    {"ts", "op", "tool", "intent", "handled", "is_error", "expired", "ms",
     "keys" (at most 20), "reason", "fallback", "session", "harness",
     "files"}

`files` lists the files the request edited (a handled edit or write, or a
built-in edit, write or patch reported by `after`), null otherwise; with
`session` and `harness` it says who changed what (`ozagia.core.owners`).

Path resolution and ownership: `path_check`, `owner_check` and `who`
(`ozagia.daemon.protocol`). Every request with a `session` (and `hello`)
marks that session live (`ozagia.core.owners.Owners`), except a
`session_event` `end`, which ends it; a handled edit of a file another
live session left uncommitted gets an `[ozagia] warning:` line. `stats` adds `path_suggestions`, `ownership_warnings` and
`live_sessions`.

With `idle_timeout` (seconds), the daemon shuts down once no request has
arrived for that long, none is in progress and no background job runs (the
idle time then counts from the last job's end; checked every `IDLE_POLL_S`
seconds at most).

Background jobs (`ozagia.daemon.jobs`): the ops `job_start`, `job_wait`,
`job_list`, `job_log`, `job_stop`, `job_events` and `job_ps` go to the job table and
take no graph lock (a `job_wait` blocks only its own connection).
`job_start` is refused unless the project enables jobs (`jobs.jobs_enabled`).
When the daemon exits through its shutdown path, it stops every job it
started (SIGTERM to the job's process group, SIGKILL after
`jobs.SHUTDOWN_GRACE_S`) except those started with `detach`, which it leaves
running and names in its log; jobs a killed daemon left running are stopped
by the next one when it loads the table.
A captured `shell` that is a `pgrep`/`ps -p` form about a job's process is
answered from /proc and the job table (`jobs.capture_ps`, intent
`shell_ps`), and `after` for `shell` adds the job hints
(`JobTable.shell_hints`). `python -m ozagia.daemon ensure` starts
daemons this way, so ones started by hooks don't linger.

`stats` counts (`DaemonState.stats`): `by_harness` (requests that name a
harness, by harness); `handled` {"true", "false"} over
`tool` requests; `passthrough` (handled false by design, not expired);
`expired` (`tool` requests dropped by their deadline; `after_expired` for
`after`); `late` (handled, but finished after the deadline, so the plugin
had likely given up); `plugin_fallbacks` (`after` requests carrying
`meta.fallback`: the plugin ran the built-in, or gave up, because the
daemon failed or timed out; `unknown_outcomes` of them carry
"unknown_outcome"); `fallbacks` = `plugin_fallbacks`, once per call: every
plugin fallback (daemon error, timeout, `ok: false`, unknown outcome) sends
exactly one such `after`. `expired` and `tool_failures` are the daemon-side
view of some of the same calls and are reported separately. `shell` counts
the `shell` tool calls the daemon answered (not expired, not preflights):
`captured` (handled), `refused` (ref-moving git) and `passthrough`, with
`capture_rate` = captured / (captured + passthrough), `by_intent` for the
captured ones, and the 30 most common `passthrough_reasons` (the
classifier's `... is not mapped` reasons; 200 distinct ones are kept, the
rest count as "(other)").

Read ledger (`ozagia.intent.ledger`): one `SessionStore` of per-session
ledgers, handed to `intent.handle`/`after` as `state`; `session_event`
clears or drops them, `read_check` answers a hook's PreToolUse.
`stats` adds `repeats_seen`, `stubs_served`, `notes_served`,
`outlines_served`, `bytes_saved` (from the answers' `meta.repeat` and
`meta.bytes_saved`) and `ledger` (sessions, evictions, events).

Working set (`ozagia.core.working_set`): after each `tool`/`after` answer
the daemon notes in the session state's `work` what it edited (handled
`edit`/`write`/key edits; `after_refresh` of a built-in edit, write or
patch with its `meta.edited_files` or patch text, of a shell command with
`meta.changed_paths` or its command-derived paths) and
which tests a pytest run failed (also in the root's test record in the
state dir); the ledger copies reads into it. The `working_set` op renders
it; `stats` adds `working_sets_served`, `working_set_chars` and
`session_briefs_served`.

Toolchain (`ozagia.core.toolchain`): the read-only `toolchain` op plans the
external tools a capability or a path needs (`pdf`, `office`, `image`, ...),
reports installed and missing ones, and previews what installing a missing
one would involve. It never installs, never uses the network and, unless the
request sets `probe_versions`/`probe_sudo`, runs no version or sudo probe.
Plans are cached in memory for `TOOLCHAIN_CACHE_TTL_S` (at most
`TOOLCHAIN_CACHE_MAX`, keyed by root and request; `refresh` bypasses the
cache). `stats` adds `toolchain_plans` (plans served, cache hits included),
`toolchain_cache_hits` and `toolchain_errors` (invalid or failed requests).

Created-file ledger (`ozagia.core.files`): a handled `write` (or `edit`
that creates the file) records it from the intent layer, with the reserved
`_reason` as its reason; `after` for `edit`, `write`, `patch` and `shell`
records newly created files among `meta.changed_paths`/`edited_files` (and,
for edit and write, the file they name). A `patch`'s patch text and a
`shell` command's write targets are derived too when the meta lists are
absent or incomplete (`ozagia.intent.call_paths`), merged, deduplicated and
capped; one bounded `git status` pathspec scan runs for those paths only;
`session_event` `start` writes a bounded manifest of tracked and untracked
files under `.ozagia/files/sessions/`; `end` diffs against it and records only
new/changed files (moves and deletes keep metadata tombstones), so files that
were already untracked before the session are not attributed to it. Without a
baseline the daemon falls back to one current scan marked `baseline: missing`
and records no session attribution. A request with `file_ledger: false`
skips the `after` and session work; `$OZAGIA_FILE_LEDGER=0` turns them
off. `stats` adds `files_recorded`, `file_ledger_scans`,
`file_ledger_errors`, `file_manifest_sessions` and `file_manifest_errors`.

Library-first hints (`ozagia.core.library_hints`): a handled `edit` or `write`
computes hints for new/changed symbols; a built-in `edit`, `write` or `patch`
reported by `after` gets bounded `[ozagia] library hint:` lines when the same
file was not just annotated by a handled call (a patch's files come from
`meta.edited_files` or its parsed patch text). `stats` adds `library_hints`,
`library_hint_files` and `library_hints_suppressed` (from handled calls) and
`library_hints_builtin` / `library_hint_builtin_files` (from `after`). Hints
are advisory text only and are never applied.

Goals (`ozagia.core.goals`): the read-only `goals` op summarises the
store as last checked. After a `tool`/`after` answer that edited files,
a session that claimed a goal (by harness and session) gets `[ozagia]
goal ...` lines: the goal whose targets the edit touched with the
criteria now affected, and an advisory note for files outside the union of
the session's claimed scopes (never a block). The brief and the working set
text list the open and claimed goals with unmet criteria (without titles
for `free_text: false`; read before the graph lock is taken). Before a
`tool` shell call that classifies as pytest, the daemon takes the working
tree's fingerprint (`ozagia.core.fingerprint`) and records the run as
`captured` with it; `after` runs are `reported`, which goal criteria do
not take as evidence. `stats` adds `goal_hints`.

Test selection (`ozagia.intent.affected`): `stats` adds `affected_runs`,
`tests_skipped_unaffected`, `cached_passes` and `full_runs_forced`, summed
from the answers' `meta.tests`.

The socket is created mode 0600. A socket file left at the path by a daemon
that no longer answers `ping` is replaced; a live daemon or a non-socket
file at the path is an error.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socketserver
import stat
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ozagia.daemon import jobs as J
from ozagia.daemon import protocol as P

JOURNAL_NAME = "daemon_journal.jsonl"
# Handled `tool` intents that edit a file (for the working set).
EDIT_INTENTS = frozenset({"edit", "write", "edit_key"})
# Tools whose `tool`/`after` may lint (`ozagia.intent.lint`): they get
# `_unlock` and `_reply_deadline`.
LINTED_TOOLS = frozenset({"edit", "write", "patch", "replace"})
# `replace` preflight plans remembered at most (oldest dropped first).
REPLACE_PLANS_KEPT = 256
# Toolchain plans (`ozagia.core.toolchain`): the daemon's bounded TTL cache,
# keyed by root, capabilities, paths, model family and probe flags. Probing
# the host is cheap but not free; a session that asks twice gets one answer.
TOOLCHAIN_CACHE_TTL_S = 60.0
TOOLCHAIN_CACHE_MAX = 32
TOOLCHAIN_MODEL_FAMILY_CHARS = 64
TOOLCHAIN_CAPABILITY_CHARS = 80
# Built-in tools whose `after` can have created files (file ledger).
FILE_LEDGER_TOOLS = frozenset({"edit", "write", "patch", "shell"})
# Files one session delta records at most, and paths an after scan checks at
# most.
FILE_LEDGER_SCAN_LIMIT = 100
FILE_LEDGER_PATHS = 20
# Built-in edits/writes/patches whose `after` may get library hints: tools,
# changed paths per call, and how long a handled/`after` annotation suppresses
# a repeat for the same session and unchanged file.
LIBRARY_HINT_TOOLS = frozenset({"edit", "write", "patch"})
LIBRARY_HINT_FILES = 20
LIBRARY_HINT_DEDUPE_S = 300.0
LIBRARY_HINT_SEEN_KEPT = 512
# Ranked repo-map delivery in `hint`: the accepted token budget, the paths
# named in `meta.repo_map.files`, and the reason text's bound.
REPO_MAP_MIN_TOKENS = 200
REPO_MAP_MAX_TOKENS = 8000
REPO_MAP_FILES_SHOWN = 10
REPO_MAP_REASON_CHARS = 120
# `$OZAGIA_FILE_LEDGER=0` turns the daemon's ledger scans off.
ENV_FILE_LEDGER = "OZAGIA_FILE_LEDGER"
# `meta` counters the daemon sums into its stats.
META_COUNTERS = ("path_suggestions", "ownership_warnings", "files_recorded",
                 "file_ledger_errors", "fetch_fallback_hints",
                 "ops_after_annotations",
                 "library_hints", "library_hint_files",
                 "library_hints_suppressed", "library_hints_builtin",
                 "library_hint_builtin_files")
# Keys kept per journal record.
JOURNAL_KEYS = 20
# Distinct shell passthrough reasons kept, and shown in `stats`.
SHELL_REASONS = 200
# `meta.tests` counters of a captured pytest or cargo test
# (`ozagia.intent.affected` / `ozagia.intent.affected_cargo`).
TEST_COUNTERS = ("affected_runs", "tests_skipped_unaffected", "cached_passes",
                 "full_runs_forced", "cargo_targets_run")
SHELL_REASONS_SHOWN = 30
# `meta` counters of the `replace` tool (`ozagia.intent.replace`): previews,
# applies, and files the applies wrote.
REPLACE_COUNTERS = ("replace_previews", "replace_applies", "replace_files")
# The `goals` op's text (`ozagia.core.goals.brief_lines`) stays under this.
W_GOALS_CHARS = 2000
# `secrets` op actions (bounded; none resolves or returns a value).
SECRET_ACTIONS = ("capture_text", "capture", "list", "forget", "providers",
                  "check", "request", "request_list", "request_status",
                  "request_fulfill", "request_decline")
MAX_SECRET_CHECK_REFS = 64
# `tmp` op (`ozagia.core.temp_space`): managed scratch directories for one
# harness session. The daemon root and state directory are authoritative; a
# caller never supplies a path. `create` is the only write.
TMP_ACTIONS = ("preflight", "create", "info", "list", "status")
TMP_HARNESSES = ("opencode",)
TMP_WRITABLE_KIND = "scratch"
TMP_DEFAULT_TTL_S = 7 * 24 * 60 * 60
TMP_MIN_TTL_S = 60
TMP_MAX_TTL_S = 90 * 24 * 60 * 60
TMP_SESSION_MAX_CHARS = 128
TMP_REASON_MAX_CHARS = 200
TMP_LABEL_MAX_CHARS = 200
TMP_LIST_DEFAULT = 100
TMP_LIST_MAX = 500
_TMP_SESSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TMP_ENVELOPE = frozenset({"op", "id", "deadline_ms", "action"})
_TMP_TRACKED_FIELDS = {
    "preflight": frozenset({"harness", "session", "reason", "ttl_s",
                            "kind", "model", "run_id"}),
    "create": frozenset({"harness", "session", "reason", "ttl_s", "kind",
                         "model", "run_id", "preflight_path"}),
    "info": frozenset({"entry_id"}),
    "list": frozenset({"kind", "session", "owned", "include_all", "limit"}),
    "status": frozenset(),
}
_SECRET_REF_TEXT = re.compile(r"secret://[A-Za-z0-9._~%/:@+-]+")

# Passthrough reasons that name a file or directory, rewritten to one key
# each so paths do not fill the reason table: (pattern, replacement), in
# order.
_REASON_PATHS = [
    (re.compile(r"^(cannot read|cannot stat|find -newer|find traversal failed|"
                r"ls directory cannot be read)\b.*", re.S), r"\1 <path>"),
    (re.compile(r"^(no such file|no such path|not a directory|not a regular "
                r"file|find root does not exist): .*", re.S), r"\1: <path>"),
    (re.compile(r"^(cd:|git -C|workdir) .*?( is not a directory|: not a "
                r"directory| is outside the project root)$", re.S),
     r"\1 <path>\2"),
    (re.compile(r"^.*?( is binary| is larger than \d+ bytes| is not UTF-8|"
                r" is outside the project root| has no interpreter line|"
                r" is not on PATH| is not an executable)$", re.S),
     r"<path>\1"),
    (re.compile(r"^\S+(: interpreter( line)? ).*", re.S), r"<path>\1..."),
]


def shell_reason_key(reason: str) -> str:
    """A passthrough reason with any file path in it replaced by `<path>`,
    for counting."""
    for rx, repl in _REASON_PATHS:
        if rx.match(reason):
            return rx.sub(repl, reason, count=1)
    return reason
STALE_PING_TIMEOUT_S = 1.0
# How often a daemon started with `parent_pid` checks that its parent lives.
PARENT_POLL_S = 2.0
# The longest wait between idle checks.
IDLE_POLL_S = 5.0
# Variables the harness sets for ozagia's own processes, not for the agent's
# test runs.
_TOOL_ONLY_ENV = ("PYTHONPATH", "COLUMNS")
# The longest path an AF_UNIX socket address holds on Linux.
SUN_PATH_MAX = 107


class DaemonStartError(RuntimeError):
    """The daemon cannot start (socket in use, bad path)."""


class TempOpError(ValueError):
    """A `tmp` op request is invalid; `code` is a stable reply code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ensure_root(root: Path) -> None:
    """`ozagia.core.memory` reads its layout from `$OZAGIA_ROOT` when it is
    first imported; point it at `root` before that happens."""
    if "ozagia.core.memory" not in sys.modules:
        os.environ["OZAGIA_ROOT"] = str(root)
        return
    from ozagia.core import memory as M
    if Path(M.ROOT).resolve() != root:
        print(f"warning: ozagia.core.memory was configured for {M.ROOT}, "
              f"not {root}", file=sys.stderr, flush=True)


def load_memory(root: Path, dump: str | Path | None, *,
                build_if_missing: bool = True):
    _ensure_root(root)
    from ozagia.config import state_path
    from ozagia.core.memory import DevMemory, build_index
    if dump:
        dump_p = Path(dump)
        mem_journal = dump_p.parent / "journal.jsonl"
    else:
        dump_p = state_path(root, "graph.json")
        mem_journal = state_path(root, "journal.jsonl")
    if not dump_p.exists():
        if not build_if_missing:
            raise FileNotFoundError(f"no snapshot at {dump_p}")
        build_index(root, dump_p)
    return DevMemory(dump_p, mem_journal, root=root)


def captured_test_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for captured pytest runs: `environ` (default this
    process's) without `OZAGIA_*`, `PYTHONPATH` and `COLUMNS`."""
    src = os.environ if environ is None else environ
    return {k: v for k, v in src.items()
            if not k.startswith("OZAGIA_") and k not in _TOOL_ONLY_ENV}


def _last(hellos: list[dict[str, Any]], field: str) -> Any:
    """`field` of the last hello that has it, or None."""
    for h in reversed(hellos):
        if h.get(field) is not None:
            return h[field]
    return None


def _expired(msg: dict[str, Any]) -> bool:
    deadline = msg.get("deadline_ms")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return time.time() * 1000 > deadline


def _reply_deadline(msg: dict[str, Any]) -> float | None:
    """The request's `deadline_ms` as a `time.monotonic()` value, or None."""
    deadline = msg.get("deadline_ms")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return None
    return time.monotonic() + (deadline / 1000.0 - time.time())


def _lock_free(op: Any, tool: Any, inp: Any) -> bool:
    """A captured pytest or cargo test reads no graph state, so it skips the
    lock; so does a captured `ruff check`, which takes the lock itself
    (`_lock`) only for its node keys. So does a compound command with a
    pytest step: it takes the lock itself for the steps that read the graph.
    So does `after` of a web fetch (`webfetch`): its hint is a local check of
    the URL and output (`ozagia.research.fallback.after_lines`) and reads no
    graph state."""
    if op == "after" and tool == "webfetch":
        return True
    if op != "tool" or tool != "shell" or not isinstance(inp, dict):
        return False
    from ozagia.intent.shell import classify, compound_steps
    cls = classify(str(inp.get("command") or ""))
    if cls["class"] == "compound":
        return any(s["kind"] == "pytest" for s in compound_steps(cls))
    return cls["class"] in ("pytest", "cargo_test", "ruff")


def _research_fetch_counters(root: Path) -> dict[str, int]:
    """`research_fetches` and `research_fetch_cache_hits` of `root`."""
    try:
        from ozagia.research.fallback import read_counters
        return read_counters(root)
    except Exception:  # noqa: BLE001 - stats never fail for a counter
        return {"research_fetches": 0, "research_fetch_cache_hits": 0}


def _bounded_strings(value: Any, name: str, *, limit: int,
                     max_chars: int) -> list[str]:
    """A string or list of strings, deduplicated and bounded.

    The daemon validates before `ozagia.core.toolchain.tool_plan` sees the
    request, so a malformed request is an `{ok: false}` answer, not an
    exception through the socket.
    """
    if value is None:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"{name} must be a string or a list of strings")
    if len(items) > limit:
        raise ValueError(f"at most {limit} {name} may be requested")
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must be non-empty strings")
        text = item.strip()
        if len(text) > max_chars:
            raise ValueError(
                f"a {name} entry exceeds the {max_chars}-character limit")
        if "\x00" in text:
            raise ValueError(f"{name} must not contain NUL bytes")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _toolchain_flag(msg: dict[str, Any], name: str) -> bool:
    value = msg.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


# -- `tmp` request validation ---------------------------------------------- #


def _tmp_has_control(text: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7f for char in text)


def _tmp_harness(msg: dict[str, Any]) -> str:
    harness = msg.get("harness")
    if not isinstance(harness, str) or harness not in TMP_HARNESSES:
        raise TempOpError(
            "invalid_harness",
            "harness must be one of " + ", ".join(TMP_HARNESSES))
    return harness


def _tmp_session(msg: dict[str, Any]) -> str:
    value = msg.get("session")
    if not isinstance(value, str):
        raise TempOpError("invalid_session", "session must be a string")
    value = value.strip()
    if (not value or len(value) > TMP_SESSION_MAX_CHARS
            or not _TMP_SESSION_RE.fullmatch(value)):
        raise TempOpError(
            "invalid_session",
            f"session must be 1..{TMP_SESSION_MAX_CHARS} characters from "
            "[A-Za-z0-9._:-]")
    return value


def _tmp_reason(msg: dict[str, Any]) -> str:
    value = msg.get("reason")
    if not isinstance(value, str):
        raise TempOpError("invalid_reason", "reason must be a string")
    value = value.strip()
    if not value:
        raise TempOpError("invalid_reason", "reason must not be empty")
    if len(value) > TMP_REASON_MAX_CHARS:
        raise TempOpError(
            "invalid_reason",
            f"reason must be at most {TMP_REASON_MAX_CHARS} characters")
    if _tmp_has_control(value):
        raise TempOpError(
            "invalid_reason", "reason must not contain control characters")
    return value


def _tmp_ttl(msg: dict[str, Any]) -> int:
    value = msg.get("ttl_s")
    if value is None:
        return TMP_DEFAULT_TTL_S
    if isinstance(value, bool) or not isinstance(value, int):
        raise TempOpError("invalid_ttl", "ttl_s must be an integer")
    if not TMP_MIN_TTL_S <= value <= TMP_MAX_TTL_S:
        raise TempOpError(
            "invalid_ttl",
            f"ttl_s must be between {TMP_MIN_TTL_S} and {TMP_MAX_TTL_S} "
            "seconds")
    return value


def _tmp_kind(msg: dict[str, Any], *, writable: bool) -> str | None:
    from ozagia.core import temp_space as T
    value = msg.get("kind")
    if value is None:
        return TMP_WRITABLE_KIND if writable else None
    if not isinstance(value, str) or value not in T.KINDS:
        raise TempOpError(
            "invalid_kind", "kind must be one of " + ", ".join(T.KINDS))
    if writable and value != TMP_WRITABLE_KIND:
        raise TempOpError(
            "invalid_kind", f"create only makes {TMP_WRITABLE_KIND} entries")
    return value


def _tmp_label(msg: dict[str, Any], name: str) -> str | None:
    value = msg.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TempOpError(f"invalid_{name}", f"{name} must be a string")
    value = value.strip()
    if not value:
        raise TempOpError(f"invalid_{name}", f"{name} must not be empty")
    if len(value) > TMP_LABEL_MAX_CHARS:
        raise TempOpError(
            f"invalid_{name}",
            f"{name} must be at most {TMP_LABEL_MAX_CHARS} characters")
    if _tmp_has_control(value):
        raise TempOpError(
            f"invalid_{name}", f"{name} must not contain control characters")
    return value


def _tmp_bool(msg: dict[str, Any], name: str, default: bool | None
              ) -> bool | None:
    value = msg.get(name, default)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TempOpError(f"invalid_{name}", f"{name} must be true or false")
    return value


def _tmp_limit(msg: dict[str, Any]) -> int:
    value = msg.get("limit", TMP_LIST_DEFAULT)
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= TMP_LIST_MAX):
        raise TempOpError(
            "invalid_limit",
            f"limit must be an integer between 1 and {TMP_LIST_MAX}")
    return value


class DaemonState:
    """What the request handlers share: the graph, the lock, the counters."""

    def __init__(self, root: Path, memory: Any, journal: Path, *,
                 test_env: dict[str, str] | None = None,
                 idle_timeout: float | None = None) -> None:
        self.root = root
        self.idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None
        self.memory = memory
        self.journal = journal
        self.test_env = test_env
        from ozagia.core.secrets import SecretBroker, SessionSecretStore
        self.secret_store = SessionSecretStore()
        self.secret_broker = SecretBroker(root, session_store=self.secret_store)
        self.secret_captures = 0
        self.secret_checks = 0
        self.secret_redactions = 0
        self.secret_errors = 0
        self.lock = threading.Lock()          # serialises handle/after
        self.lock_wait_total_ms = 0.0
        self.lock_wait_max_ms = 0.0
        self.lock_wait_count = 0
        self._stats_lock = threading.RLock()
        self.started = time.time()
        self.by_op: dict[str, int] = {}
        self.by_tool: dict[str, int] = {}
        self.by_intent: dict[str, int] = {}
        self.by_harness: dict[str, int] = {}
        self.active = 0          # requests in progress
        self.last_activity = time.monotonic()
        self.idle_shutdown = False
        self.handled = {"true": 0, "false": 0}
        self.errors = 0          # ok: false responses
        self.tool_errors = 0     # handled, is_error
        self.passthrough = 0     # tool: handled false by design
        self.expired = 0         # tool: dropped by deadline_ms
        self.after_expired = 0   # after: dropped by deadline_ms
        self.late = 0            # tool: handled, finished after deadline_ms
        self.plugin_fallbacks = 0  # after with meta.fallback
        self.unknown_outcomes = 0  # ... whose fallback is "unknown_outcome"
        self.tool_failures = 0   # tool: ok false
        self.preflights = 0      # tool: preflight answered "permission required"
        # Region replies: the current text of the nearest region sent for a
        # patch hunk or an edit whose old text is not in the file.
        self.region_replies = 0
        # Lint inside the edit (`ozagia.intent.lint`): linter runs, findings
        # reported on changed lines, linter runs cut off by the timeout.
        self.lint = {"lint_runs": 0, "lint_findings": 0, "lint_timeouts": 0}
        # Multi-file replace (`ozagia.intent.replace`).
        self.replace = {k: 0 for k in REPLACE_COUNTERS}
        # shell tool calls: captured (handled), refused (git refs) and
        # passthrough, with the passthrough reasons, so an eval can measure
        # the shell capture rate.
        self.shell = {"captured": 0, "refused": 0, "passthrough": 0}
        self.shell_intents: dict[str, int] = {}
        self.shell_reasons: dict[str, int] = {}
        self.hellos: list[dict[str, Any]] = []
        from ozagia.intent.ledger import SessionStore
        # Per-session read ledgers (`ozagia.intent.ledger`).
        self.sessions = SessionStore()
        self.reads = {"repeats_seen": 0, "stubs_served": 0, "notes_served": 0,
                      "bytes_saved": 0}
        # Affected-test selection and the test-result cache
        # (`ozagia.intent.affected`), from the answers' `meta.tests`.
        self.tests = {k: 0 for k in TEST_COUNTERS}
        self.ms_total = 0.0
        self.ms_max = 0.0
        self.ms_count = 0
        self.server: socketserver.BaseServer | None = None
        from ozagia.core import resources as R
        # Observation, admission and the first enforcement layer share one
        # loaded policy for the daemon's lifetime (a config edit needs a
        # restart, like the other daemon-loaded settings).
        self.jobs = J.JobTable(root, base_env=test_env,
                               resource_policy=R.load_policy(root),
                               secret_broker=self.secret_broker)
        from ozagia.core.working_set import TestRecord
        # The root's failing tests across sessions (`working_set` with
        # `since`), persisted in the state dir.
        self.test_record = TestRecord(root)
        self.working = {"working_sets_served": 0, "working_set_chars": 0,
                        "session_briefs_served": 0}
        # Toolchain plans (`ozagia.core.toolchain`): plans served (including
        # cache hits), hits, and invalid or failed requests. The cache is per
        # daemon, so one root's plan is never served for another.
        self.toolchain_counts = {"toolchain_plans": 0,
                                 "toolchain_cache_hits": 0,
                                 "toolchain_errors": 0}
        self.toolchain_cache: OrderedDict[
            tuple[str, tuple[str, ...], tuple[str, ...], str, bool, bool],
            tuple[float, dict[str, Any], str]] = OrderedDict()
        from ozagia.core.owners import Owners
        # Live sessions and ownership warnings (`ozagia.core.owners`).
        self.owners = Owners(root, daemon_journal=journal)
        self.path_suggestions = 0
        self.ownership_warnings = 0
        # The plan of each `replace` preflight, by (harness, session,
        # input digest), until the call it permitted (`_granted_plan`).
        self.replace_plans: dict[tuple[str, Any, str], str] = {}
        # `[ozagia]` lines naming `research_fetch` after a failed web fetch.
        self.fetch_fallback_hints = 0
        # Ops graph (`ozagia.core.ops_graph`): `[ozagia] ops:` lines added
        # after container/unit log and status commands.
        self.ops_after_annotations = 0
        # Created-file ledger (`ozagia.core.files`): rows appended from
        # handled writes and `after`/session deltas, scans run, and
        # ledger/git failures (the tool call itself never fails for them).
        self.files_recorded = 0
        self.file_ledger_scans = 0
        self.file_ledger_errors = 0
        # Session manifest snapshots/deltas written, and manifest failures.
        self.file_manifest_sessions = 0
        self.file_manifest_errors = 0
        # Library-first hints (`ozagia.core.library_hints`): hints shown,
        # edited files that produced one, and candidates suppressed.
        self.library_hints = 0
        self.library_hint_files = 0
        self.library_hints_suppressed = 0
        # Built-in `after` edits/writes/patches: hints shown and files with
        # one, plus the recent (harness, session, file) annotations used to
        # avoid repeating a hint for an unchanged file.
        self.library_hints_builtin = 0
        self.library_hint_builtin_files = 0
        self.library_seen: dict[tuple[str, str, str], tuple[int, int, float]] = {}
        # Goal lines added after edits (`ozagia.core.goals.edit_hints`).
        self.goal_hints = 0
        # Ranked repo-map delivery (`hint` with `repo_map_tokens`): maps
        # delivered, requests skipped (stale/empty/error), and the estimated
        # tokens and file paths of the delivered maps.
        self.repo_map_delivered = 0
        self.repo_map_skipped = 0
        self.repo_map_tokens = 0
        self.repo_map_files = 0
        self.repo_map_last_skip: str | None = None

    # -- requests ------------------------------------------------------------ #
    def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        from ozagia import intent
        op = msg.get("op")
        if op == "session_event" and msg.get("event") == "end":
            self.owners.end(msg.get("harness"), msg.get("session"))
        elif op not in ("ping", "stats", "shutdown"):
            self.owners.touch(msg.get("harness"), msg.get("session"))
        if op == "ping":
            return {"ok": True, "version": P.PROTOCOL_VERSION,
                    "root": str(self.root), "pid": os.getpid()}
        if op == "hello":
            tools = msg.get("tools") if isinstance(msg.get("tools"), dict) else None
            with self._stats_lock:
                self.hellos.append({
                    "ts": time.time(), "harness": msg.get("harness"),
                    "plugin": msg.get("plugin"), "version": msg.get("version"),
                    "session": msg.get("session"), "phase": msg.get("phase"),
                    "tools": tools,
                    "permission_assert": msg.get("permission_assert"),
                    "compaction_hook": msg.get("compaction_hook")})
            if msg.get("compaction_hook") is True:
                from ozagia.intent.ledger import CAP_COMPACTION
                self.sessions.confirm(str(msg.get("harness") or "opencode"),
                                      msg.get("session"), CAP_COMPACTION)
            return {"ok": True}
        if op == "stats":
            return {"ok": True, "stats": self.stats()}
        if op == "shutdown":
            return {"ok": True}
        if op == "session_event":
            from ozagia.intent.ledger import SESSION_EVENTS
            session, event = msg.get("session"), msg.get("event")
            if not isinstance(session, str) or not session:
                return {"ok": False, "error": "session is required"}
            if event not in SESSION_EVENTS:
                return {"ok": False, "error": "event must be one of "
                        + ", ".join(SESSION_EVENTS)}
            self.session_state(msg)     # caps, acks, a pending reset
            n = self.sessions.event(str(msg.get("harness") or "opencode"),
                                    session, event)
            self._files_session_event(msg, event)
            secrets_cleared = None
            requests_cleared = None
            if event == "end":
                harness = str(msg.get("harness") or "opencode")
                try:
                    secrets_cleared = self.secret_store.clear(harness, session)
                    requests_cleared = self.secret_store.clear_requests(
                        harness, session)
                except Exception:  # noqa: BLE001 - session end must not fail
                    self.secret_errors += 1
            body: dict[str, Any] = {"ok": True, "cleared": n,
                                    "meta": {"intent": f"session_{event}",
                                             "keys": []}}
            if secrets_cleared is not None:
                body["secrets_cleared"] = secrets_cleared
            if requests_cleared is not None:
                body["secret_requests_cleared"] = requests_cleared
            return body
        if op == "profile_event":
            try:
                from ozagia.session_profile_cli import store_profile_event
                stored = store_profile_event(self.root, msg)
            except TimeoutError as exc:
                return {"ok": False, "error": f"profile event persistence timed out; event was dropped: {exc}"}
            except (OSError, ValueError, TypeError) as exc:
                return {"ok": False, "error": f"invalid profile event: {exc}"}
            return stored
        if op == "read_check":
            tool = msg.get("tool")
            inp = msg.get("input")
            if not isinstance(tool, str) or not isinstance(inp, dict):
                return {"ok": False, "error": "tool and input are required"}
            inp = {k: v for k, v in inp.items() if not str(k).startswith("_")}
            state = self.session_state(msg)
            with self.lock:
                if _expired(msg):
                    return {"ok": True, "repeat": False, "stub": None,
                            "expired": True,
                            "meta": {"intent": "check_expired", "keys": []}}
                body = intent.check_read(self.memory, tool, inp, state=state)
            return {"ok": True, **body}
        if op in ("path_check", "owner_check", "who"):
            return self._owners_op(op, msg)
        if op == "secrets":
            return self.secrets_op(msg)
        if op in J.OPS:
            return self.jobs.dispatch(op, msg)
        if op == "working_set":
            return self.working_set(msg)
        if op == "toolchain":
            return self.toolchain_op(msg)
        if op == "tmp":
            return self.tmp_op(msg)
        if op == "goals":
            return self.goals(msg)
        if op == "structural":
            args = msg.get("input")
            if not isinstance(args, dict):
                return {"ok": False, "error": "input must be an object"}
            with self._graph_lock():
                if _expired(msg):
                    return {"ok": True, "expired": True, "result": {
                        "ok": False, "error": "structural request expired",
                        "resources": []}}
                from ozagia.intent.structural import query
                result = query(self.memory, args.get("kind"), args.get("target"),
                               depth=args.get("depth", 1),
                               budget_tokens=args.get("budget_tokens", 240))
            return {"ok": True, "result": result}
        if op == "hint":
            prompt = msg.get("prompt")
            history = msg.get("history", [])
            if not isinstance(prompt, str) or len(prompt) > 12000:
                return {"ok": False, "error": "prompt must be a string of at most 12000 characters"}
            if (not isinstance(history, list) or len(history) > 6
                    or any(not isinstance(item, dict)
                           or item.get("role") not in ("user", "assistant")
                           or not isinstance(item.get("text"), str)
                           or len(item["text"]) > 2000 for item in history)):
                return {"ok": False, "error": "history must contain at most six bounded user/assistant messages"}
            repo_map_tokens = msg.get("repo_map_tokens")
            if repo_map_tokens is not None and (
                    isinstance(repo_map_tokens, bool)
                    or not isinstance(repo_map_tokens, int)
                    or not REPO_MAP_MIN_TOKENS <= repo_map_tokens <= REPO_MAP_MAX_TOKENS):
                return {"ok": False, "error": (
                    "repo_map_tokens must be an integer between "
                    f"{REPO_MAP_MIN_TOKENS} and {REPO_MAP_MAX_TOKENS}")}
            with self._graph_lock():
                if _expired(msg):
                    return {"ok": True, "context": "", "expired": True,
                            "meta": {"intent": "hint_expired", "keys": []}}
                if repo_map_tokens is not None:
                    return self._repo_map_hint(prompt, repo_map_tokens)
                from ozagia.core.proactive_context import build_context
                try:
                    hint = build_context(self.memory, prompt,
                                         history=[item["text"] for item in history],
                                         budget_tokens=400, max_history_turns=2,
                                         structural_tool_available=(
                                             msg.get("harness") == "opencode"
                                             and msg.get("structural_tool") is True))
                    infra = hint.get("infra") or {}
                    result = {"context": hint.get("context", ""),
                              "meta": {
                                  "intent": "hint",
                                  "keys": hint.get("keys", []),
                                  "structural_intent": hint.get("structural_intent"),
                                  "freshness": hint.get("freshness", "unknown"),
                                  "truncated": hint.get("truncated", False),
                                  "estimated_tokens": hint.get("estimated_tokens"),
                                  "token_estimate_method": hint.get(
                                      "token_estimate_method"),
                                  "allocation": hint.get("allocation"),
                                  "infra_results": len(infra.get("results") or []),
                                  "infra_edges": len(infra.get("edges") or []),
                              }}
                except Exception as exc:  # a hint must never block the agent
                    return {"ok": True, "context": "", "meta": {
                        "intent": "hint_error", "keys": [], "error": type(exc).__name__}}
            return {"ok": True, **result}
        if op in ("tool", "after"):
            tool = msg.get("tool")
            if not isinstance(tool, str) or not tool:
                return {"ok": False, "error": "tool is required"}
            inp = msg.get("input")
            if inp is None:
                inp = {}
            if isinstance(inp, dict):
                # `_reason` is the one reserved field kept (a bounded label
                # for the created-file ledger); the rest are caller-only.
                reason = inp.get("_reason")
                inp = {k: v for k, v in inp.items()
                       if not str(k).startswith("_")}
                if isinstance(reason, str) and reason.strip():
                    inp["_reason"] = reason
                if self.test_env is not None:
                    inp["_test_env"] = dict(self.test_env)
            harness = str(msg.get("harness") or "opencode")
            session = msg.get("session")
            if op == "tool" and tool == "shell" and isinstance(inp, dict):
                captured = self._capture_ps(inp, msg)
                if captured is not None:
                    return captured
            state = self.session_state(msg)
            started = self._pytest_start(op, tool, inp)
            free = _lock_free(op, tool, inp)
            if free and isinstance(inp, dict):
                inp["_lock"] = self.lock
            lock = contextlib.nullcontext() if free else self._graph_lock()
            lints = isinstance(inp, dict) and tool in LINTED_TOOLS
            if not free and lints:
                # Lint after an edit runs its linters with the lock released.
                inp["_unlock"] = self._unlocked
            with lock:
                reply_at = _reply_deadline(msg)
                if reply_at is not None and lints:
                    inp["_reply_deadline"] = reply_at
                if _expired(msg):
                    if op == "tool":
                        return {"ok": True, "handled": False,
                                "reason": "expired", "expired": True,
                                "meta": {"intent": f"{tool}_expired",
                                         "keys": [], "ms": 0.0}}
                    return {"ok": True, "append": "", "expired": True,
                            "meta": {"intent": "after_expired", "keys": [],
                                     "ms": 0.0}}
                if op == "tool":
                    is_pre = msg.get("preflight") is True
                    # Interactive `ozagia agent` sessions leave ref-moving
                    # git to OpenCode's built-in shell, whose permission
                    # service remains the approval authority. Eval sessions
                    # never set this flag and keep the intent refusal.
                    if (tool == "shell" and msg.get("git_refs_passthrough") is True
                            and isinstance(inp, dict)
                            and isinstance(inp.get("command"), str)):
                        from ozagia.intent.shell import classify
                        if classify(inp["command"]).get("class") == "git_refs":
                            body = {"handled": False,
                                    "reason": "git refs delegated to OpenCode",
                                    "meta": {"intent": "shell_passthrough",
                                             "keys": [], "ms": 0.0}}
                            if is_pre:
                                body.update(
                                    preflight=True,
                                    permission=intent.permission_request(
                                        self.memory, "shell", inp))
                        else:
                            body = None
                    else:
                        body = None
                    if body is None:
                        granted = self._granted_plan(msg, tool, inp, harness,
                                                     session, is_pre)
                        body = intent.handle(self.memory, tool, inp,
                                             harness=harness, session=session,
                                             preflight=is_pre,
                                             state=state, owners=self.owners,
                                             granted_plan_id=granted)
                        if is_pre:
                            self._remember_plan(body, tool, inp, harness, session)
                else:
                    req_meta = msg.get("meta")
                    body = intent.after(self.memory, tool, inp,
                                        msg.get("output"), harness=harness,
                                        session=session, state=state,
                                        delivered=msg.get("delivered"),
                                        meta=req_meta if isinstance(
                                            req_meta, dict) else None)
            if op == "tool" and body.get("handled") and _expired(msg):
                body = {**body, "late": True}
            if op == "after" and tool == "shell" and isinstance(inp, dict):
                hints = self.jobs.shell_hints(str(inp.get("command") or ""),
                                              session)
                if hints:
                    body = {**body, "append": "\n".join(
                        [x for x in [body.get("append") or ""] if x] + hints)}
            edited = self._observe(op, tool, inp, msg, body, state, started)
            if edited is not None:
                body = self._goal_hints(op, body, harness, session, *edited)
            self._files_observe(op, tool, inp, msg, body)
            if op == "tool":
                self._note_library_hint_handled(tool, inp, msg, body)
            else:
                body = self._library_hints_after(op, tool, inp, msg, body)
            return {"ok": True, **self._job_meta(body)}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # -- ranked repo map ----------------------------------------------------- #
    @staticmethod
    def _bounded_reason(value: Any) -> str:
        text = " ".join(str(value or "").split())
        return text[:REPO_MAP_REASON_CHARS]

    def _repo_map_skip(self, reason: Any, freshness: Any) -> dict[str, Any]:
        """An empty hint that says why no repo map was served."""
        state = self._bounded_reason(freshness) or "unknown"
        with self._stats_lock:
            self.repo_map_skipped += 1
            self.repo_map_last_skip = self._bounded_reason(reason)
        return {
            "ok": True,
            "context": "",
            "meta": {
                "intent": "hint_repo_map_skipped",
                "keys": [],
                "repo_map": {
                    "delivered": False,
                    "reason": self._bounded_reason(reason),
                    "freshness": state,
                },
            },
        }

    def _repo_map_hint(self, prompt: str, tokens: int) -> dict[str, Any]:
        """The `hint` answer for `repo_map_tokens`: a bounded map from a
        snapshot verified fresh now, or an empty context and a reason.

        Called with the graph lock held. A staleness check that cannot
        establish freshness is a skip, never a stale map.
        """
        try:
            snapshot = self.memory.staleness(force=True)
        except Exception:  # noqa: BLE001 - a hint must never block the agent
            return self._repo_map_skip("snapshot freshness is unknown",
                                       "unknown")
        if not isinstance(snapshot, dict):
            return self._repo_map_skip("snapshot freshness is unknown",
                                       "unknown")
        freshness = self._bounded_reason(
            snapshot.get("freshness")
            or ("stale" if snapshot.get("stale") is True else "unknown"))
        if freshness != "fresh" or snapshot.get("stale") is True:
            state = "stale" if freshness == "fresh" else freshness
            return self._repo_map_skip(f"snapshot freshness is {state}", state)
        try:
            result = self.memory.repo_map(prompt, budget_tokens=tokens)
        except Exception as exc:  # noqa: BLE001
            return self._repo_map_skip(
                f"repo map unavailable ({type(exc).__name__})", freshness)
        if not isinstance(result, dict) or result.get("ok") is False:
            return self._repo_map_skip("repo map unavailable", freshness)
        markdown = result.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            return self._repo_map_skip("repo map is empty", freshness)
        paths: list[str] = []
        files = result.get("files")
        for item in files if isinstance(files, list) else ():
            path = item.get("path") if isinstance(item, dict) else item
            if isinstance(path, str) and path and path not in paths:
                paths.append(path[:256])
            if len(paths) >= REPO_MAP_FILES_SHOWN:
                break
        cache = result.get("cache") if isinstance(result.get("cache"), dict) else {}
        graph_hash = cache.get("root_hash") or result.get("root_hash")
        header = (
            "[ozagia repo map] This is a ranked repo map for this task, from a "
            f"verified snapshot (freshness: {freshness}, budget: {tokens} tokens)."
        )
        instruction = (
            "[ozagia] Verify paths and symbols with read or ozagia_structure "
            "before relying on them; ordinary search and reads remain available."
        )
        body = markdown.strip()
        estimate = result.get("estimated_tokens")
        if isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0:
            estimated_tokens = min(estimate, tokens)
        else:
            estimated_tokens = max(1, len(body) // 4)
        context = f"{header}\n\n{body}\n\n{instruction}"
        meta = {
            "delivered": True,
            "tokens": tokens,
            "estimated_tokens": estimated_tokens,
            "files": paths,
            "freshness": freshness,
            "graph_hash": str(graph_hash)[:64] if graph_hash else None,
            "cache": {key: cache.get(key) for key in
                      ("root_hash", "cached_files", "parsed", "reused")},
            "truncated": False,
        }
        with self._stats_lock:
            self.repo_map_delivered += 1
            self.repo_map_tokens += estimated_tokens
            self.repo_map_files += len(paths)
        return {
            "ok": True,
            "context": context,
            "meta": {"intent": "hint_repo_map", "keys": [], "repo_map": meta},
        }

    # -- secrets ------------------------------------------------------------- #
    def _secret_stat(self, name: str, n: int = 1) -> None:
        if n <= 0:
            return
        with self._stats_lock:
            setattr(self, name, getattr(self, name) + n)

    def _redact_response_text(self, text: str) -> str:
        original = text
        from ozagia.core import security
        refs: list[str] = []

        def keep(match: re.Match[str]) -> str:
            refs.append(match.group(0))
            return f"\x00ozagia-ref-{len(refs) - 1}\x00"

        masked = _SECRET_REF_TEXT.sub(keep, text)
        try:
            masked = self.secret_broker.redactor.redact(masked)
        except Exception:  # noqa: BLE001 - fail closed
            masked = "[redacted]"
        masked = security.redact_text(masked)
        for index, ref in enumerate(refs):
            masked = masked.replace(f"\x00ozagia-ref-{index}\x00", ref)
        if masked != original:
            self._secret_stat("secret_redactions")
        return masked

    def _redact_values(self, value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return self._redact_response_text(value)
        if depth >= 24:
            return value
        if isinstance(value, dict):
            return {(self._redact_response_text(key)
                     if isinstance(key, str) else key):
                    self._redact_values(item, depth + 1)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_values(item, depth + 1) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_values(item, depth + 1)
                         for item in value)
        return value

    def redact_response(self, resp: dict[str, Any]) -> dict[str, Any]:
        """Redact every value before a response is sent or journaled.

        Resolved values and captured session values go first (the broker's
        redactor); the security module's generic patterns are defense in
        depth. ``secret://`` references are preserved, never redacted.
        """
        if not isinstance(resp, dict):
            return resp
        try:
            redacted = self._redact_values(resp)
            return redacted if isinstance(redacted, dict) else resp
        except Exception:  # noqa: BLE001 - never send an unredacted response
            self._secret_stat("secret_errors")
            return {"id": resp.get("id"), "ok": False,
                    "error": "response redaction failed"}

    @staticmethod
    def _require_secret_session(session: Any) -> str:
        from ozagia.core.secrets import SecretError
        if (not isinstance(session, str) or not session
                or len(session) > 256
                or any(ord(char) < 32 or ord(char) == 127
                       for char in session)):
            raise SecretError("session is required", code="session_required")
        return session

    def secrets_op(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Session captures and status. Never captures from argv, resolves,
        or returns a value."""
        from ozagia.core.secrets import SecretError, detect_text
        action = msg.get("action")
        if action not in SECRET_ACTIONS:
            self._secret_stat("secret_errors")
            return {"ok": False, "code": "invalid_action",
                    "error": "action must be one of " + ", ".join(SECRET_ACTIONS)}
        harness = msg.get("harness")
        harness = harness if isinstance(harness, str) and harness else "opencode"
        session = msg.get("session")
        try:
            self.secret_store.prune()
            if action == "providers":
                return {"ok": True, "providers": self.secret_broker.providers(),
                        "storage": "memory-only",
                        "redact_output": self.secret_broker.config.redact_output}
            if action == "capture_text":
                text = msg.get("text")
                if not isinstance(text, str):
                    raise SecretError("text must be a string",
                                      code="invalid_text")
                # Validate and prune the session before detection, so a bad
                # context is an error instead of silently withheld matches.
                self.secret_store.list(harness, self._require_secret_session(session))

                def factory(value: str, label: str) -> Any:
                    self.secret_broker.redactor.add(value)
                    return self.secret_store.capture(
                        value, harness=harness, session=session, label=label)

                capture = detect_text(text, ref_factory=factory)
                self._secret_stat("secret_captures", capture.captured)
                entries = {item.ref: item for item in self.secret_store.list(
                    harness, self._require_secret_session(session))}
                secrets = []
                for item in capture.secrets:
                    data = item.as_dict()
                    entry = entries.get(data.get("ref") or "")
                    if entry is not None:
                        data["created"] = entry.created
                        data["expires"] = entry.expires
                    secrets.append(data)
                return {"ok": True, **capture.as_dict(), "secrets": secrets,
                        "count": capture.captured}
            if action == "capture":
                value = msg.get("value")
                if not isinstance(value, str):
                    raise SecretError("value must be a string",
                                      code="invalid_value")
                captured = self.secret_store.capture(
                    value, harness=harness,
                    session=self._require_secret_session(session),
                    label=msg.get("label"), ttl_s=msg.get("ttl_s"))
                self.secret_broker.redactor.add(value)
                self._secret_stat("secret_captures")
                return {"ok": True, **captured.as_dict()}
            if action == "list":
                items = self.secret_store.list(
                    harness, self._require_secret_session(session))
                return {"ok": True, "secrets": [item.as_dict() for item in items],
                        "count": len(items), "storage": "memory-only"}
            if action == "forget":
                ref = msg.get("ref")
                if not isinstance(ref, str) or not ref:
                    raise SecretError("ref is required", code="invalid_ref")
                forgotten = self.secret_store.forget(
                    ref, harness=harness,
                    session=self._require_secret_session(session))
                return {"ok": True, "forgotten": forgotten, "ref": ref}
            if action == "request":
                request = self.secret_store.request(
                    harness=harness,
                    session=self._require_secret_session(session),
                    label=msg.get("label"), purpose=msg.get("purpose"),
                    target=msg.get("target"), ttl_s=msg.get("ttl_s"))
                return {"ok": True, **request.as_dict(),
                        "storage": "memory-only"}
            if action == "request_list":
                items = self.secret_store.requests(
                    harness, self._require_secret_session(session))
                return {"ok": True,
                        "requests": [item.as_dict() for item in items],
                        "count": len(items), "storage": "memory-only"}
            if action in ("request_status", "request_fulfill",
                          "request_decline"):
                request_id = msg.get("request_id")
                if not isinstance(request_id, str) or not request_id:
                    raise SecretError("request_id is required",
                                      code="invalid_request_id")
                current_session = self._require_secret_session(session)
                if action == "request_status":
                    request = self.secret_store.request_status(
                        request_id, harness=harness, session=current_session)
                elif action == "request_decline":
                    request = self.secret_store.decline_request(
                        request_id, harness=harness, session=current_session)
                else:
                    value = msg.get("value")
                    if not isinstance(value, str):
                        raise SecretError("value must be a string",
                                          code="invalid_value")
                    before = self.secret_store.request_status(
                        request_id, harness=harness,
                        session=current_session)
                    request = self.secret_store.fulfill_request(
                        request_id, value, harness=harness,
                        session=current_session, ttl_s=msg.get("ttl_s"))
                    if (before.state == "pending"
                            and request.state == "fulfilled"):
                        self.secret_broker.redactor.add(value)
                        self._secret_stat("secret_captures")
                return {"ok": True, **request.as_dict(),
                        "storage": "memory-only"}
            refs = msg.get("refs")
            if (not isinstance(refs, list) or not refs
                    or len(refs) > MAX_SECRET_CHECK_REFS
                    or not all(isinstance(ref, str)
                               and 0 < len(ref) <= 1024 for ref in refs)):
                raise SecretError(
                    f"refs must be a non-empty list of at most "
                    f"{MAX_SECRET_CHECK_REFS} references",
                    code="invalid_refs")
            checks = self.secret_broker.check_many(
                refs, harness=harness,
                session=session if isinstance(session, str) else None)
            self._secret_stat("secret_checks", len(checks))
            return {"ok": True, "checks": [check.as_dict() for check in checks]}
        except SecretError as exc:
            self._secret_stat("secret_errors")
            return {"ok": False, "error": self._redact_response_text(str(exc)),
                    "code": exc.code}
        except Exception:  # noqa: BLE001 - a secrets op must not leak a value
            self._secret_stat("secret_errors")
            return {"ok": False, "code": "secret_error",
                    "error": "secrets operation failed"}

    @staticmethod
    def _plan_key(tool: str, inp: Any, harness: str, session: Any
                  ) -> tuple[str, Any, str] | None:
        if tool != "replace" or not isinstance(inp, dict):
            return None
        import hashlib
        import json as _json
        public = {k: v for k, v in inp.items() if not str(k).startswith("_")}
        try:
            blob = _json.dumps(public, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return None
        return (harness, session, hashlib.sha256(blob.encode()).hexdigest())

    def _granted_plan(self, msg: dict[str, Any], tool: str, inp: Any,
                      harness: str, session: Any, preflight: bool
                      ) -> str | None:
        """The plan a `replace` call was permitted with: the caller's
        `granted` permission, else this session's preflight of the same
        input (consumed)."""
        key = self._plan_key(tool, inp, harness, session)
        if key is None or preflight:
            return None
        remembered = self.replace_plans.pop(key, None)
        granted = msg.get("granted")
        meta = granted.get("metadata") if isinstance(granted, dict) else None
        sent = meta.get("plan_id") if isinstance(meta, dict) else None
        if isinstance(sent, str) and sent:
            return sent
        return remembered

    def _remember_plan(self, body: dict[str, Any], tool: str, inp: Any,
                       harness: str, session: Any) -> None:
        key = self._plan_key(tool, inp, harness, session)
        perm = body.get("permission") if body.get("preflight") else None
        meta = perm.get("metadata") if isinstance(perm, dict) else None
        plan = meta.get("plan_id") if isinstance(meta, dict) else None
        if key is None or not isinstance(plan, str):
            return
        self.replace_plans.pop(key, None)
        self.replace_plans[key] = plan
        while len(self.replace_plans) > REPLACE_PLANS_KEPT:
            self.replace_plans.pop(next(iter(self.replace_plans)))

    def session_state(self, msg: dict[str, Any]) -> Any:
        """The ledger of the request's (harness, session, sub), with the
        harness's configured repeat policy (`ledger.effective_policy` may
        lower `stub` to `note`); None without a session id. Before the
        request itself: `compaction_hook: true` confirms that the harness
        reports compaction, `acks` records staged served reads, and
        `reset_pending: true` (a `session_event` that failed earlier) is
        applied as a `compact`, in that order."""
        from ozagia.intent.ledger import CAP_COMPACTION, policy_for
        harness = str(msg.get("harness") or "opencode")
        session = msg.get("session")
        if not isinstance(session, str) or not session:
            return None
        if msg.get("compaction_hook") is True:
            self.sessions.confirm(harness, session, CAP_COMPACTION)
        st = self.sessions.get(harness, session, msg.get("sub"),
                               policy=policy_for(self.root, harness))
        if msg.get("acks"):
            st.ack(msg.get("acks"))
        if msg.get("reset_pending") is True:
            self.sessions.event(harness, session, "compact")
        return st

    # -- the working set (`ozagia.core.working_set`) ------------------------ #
    def _ws_rel(self, path: Any, base: str | Path | None = None) -> str | None:
        """`path` (absolute, or relative to the root or `base`) relative to
        the root, or None for a node key, an empty path or one outside the
        root."""
        if not isinstance(path, str) or not path:
            return None
        from ozagia.core.working_set import KEY_PREFIXES
        if path.startswith(KEY_PREFIXES + ("file:",)):
            return None
        p = Path(path)
        base_path = Path(base) if base else self.root
        if not base_path.is_absolute():
            base_path = self.root / base_path
        full = os.path.realpath(p if p.is_absolute() else base_path / p)
        rel = os.path.relpath(full, self.root).replace(os.sep, "/")
        return None if rel.startswith("..") or rel == "." else rel

    def _pytest_start(self, op: str, tool: str, inp: Any
                      ) -> tuple[float, dict[str, Any] | None] | None:
        """For a `tool` shell call the daemon may run as a captured pytest or
        cargo test: the start time and the working tree's fingerprint now
        (`ozagia.core.fingerprint`), stored with the run so goal criteria
        can tell whether a file changed since it started. None otherwise.
        Never raises."""
        if op != "tool" or tool != "shell" or not isinstance(inp, dict):
            return None
        command = inp.get("command")
        if not isinstance(command, str) or (
                "pytest" not in command and "cargo" not in command):
            return None
        try:
            from ozagia.intent.shell import classify
            if classify(command).get("class") not in ("pytest", "cargo_test"):
                return None
            from ozagia.core.fingerprint import tree_fingerprint
            at = time.time()
            return at, tree_fingerprint(self.root)
        except Exception as e:  # noqa: BLE001 - bookkeeping only
            print(f"test fingerprint failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return None

    def _observe(self, op: str, tool: str, inp: Any, msg: dict[str, Any],
                 body: dict[str, Any], state: Any,
                 started: tuple[float, dict[str, Any] | None] | None = None
                 ) -> tuple[list[str], list[str]] | None:
        """Note in the session's working set what a `tool`/`after` answer
        shows it edited (handled edits and writes, `after` refreshes) and
        which tests a pytest run failed (also in the root's test record).
        A pytest the daemon ran itself (`shell_pytest`) is recorded as
        `captured`, with `started` (its start time and fingerprint); a
        captured compound command as `compound`; output an `after` hands
        over as `reported` (`ozagia.core.working_set.TestRun`). Returns the
        (files, keys) the answer shows edited, or None. Never raises."""
        result: tuple[list[str], list[str]] | None = None
        try:
            from ozagia.core import working_set as W
            if not isinstance(inp, dict):
                return None
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            it = meta.get("intent")
            keys = [k for k in meta.get("keys") or [] if isinstance(k, str)]
            work = state.work if state is not None else None
            files: list[str | None] = []
            edited = False
            if (op == "tool" and body.get("handled") and not body.get("is_error")
                    and it in EDIT_INTENTS):
                edited = True
                files.append(self._ws_rel(inp.get("path", inp.get("filePath"))))
                if it == "edit_key":
                    files.append(W.key_file(str(inp.get("key") or "")))
            elif op == "after" and it == "after_refresh":
                # A built-in edit, write or patch: the files this call
                # changed (`meta.edited_files`, session-scoped), plus the
                # files its patch text names when the meta list is absent or
                # incomplete. A shell command: the changed files it may have
                # written (`meta.changed_paths`, recently written ones only),
                # plus the paths its command line itself names. Not every
                # pending change in the root: other sessions and the user's
                # editor change files too.
                edited = True
                if tool in ("edit", "write", "patch"):
                    files.extend(p for p in meta.get("edited_files") or []
                                 if isinstance(p, str))
                    if tool == "patch":
                        files.extend(self._derived_paths("patch", inp))
                elif tool == "shell":
                    files.extend(p for p in meta.get("changed_paths") or []
                                 if isinstance(p, str))
                    files.extend(self._derived_paths("shell", inp))
            if edited:
                named = list(dict.fromkeys(f for f in files if f))
                if op == "after":
                    # The refresh's delta covers every pending file: keep
                    # the keys of the files this call changed.
                    keys = [k for k in keys if W.key_file(k) in named]
                if work is not None:
                    work.note_edit(named, keys)
                result = (named, keys)
            command = inp.get("command")
            if tool != "shell" or not isinstance(command, str):
                return result
            if op == "tool":
                if not body.get("handled"):
                    return result
                text = str(body.get("output") or "")
            else:
                out = msg.get("output")
                text = (out if isinstance(out, str) else "") + "\n" + str(
                    body.get("append") or "")
            cargo = it == "shell_cargo_test" or (
                op == "after" and "cargo" in command
                and "[ozagia] cargo test " in text)
            if not cargo and "pytest" not in command:
                return result
            from ozagia.intent.shell import command_heads
            heads = command_heads(command)
            run = None
            if cargo:
                source = "captured" if op == "tool" else "reported"
                run = W.cargo_run(command, heads, text, source=source)
            if run is None and "pytest" in command:
                source = "reported"
                if op == "tool":
                    source = "captured" if it == "shell_pytest" else "compound"
                env = inp.get("_test_env") if isinstance(
                    inp.get("_test_env"), dict) else dict(os.environ)
                run = W.pytest_run(command, heads, text,
                                   workdir=inp.get("workdir"), root=self.root,
                                   source=source, env=env)
            if run is None:
                return result
            if run.source == "captured" and started is not None:
                run.ts, run.tree = started
            if work is not None:
                work.note_tests(run)
            self.test_record.apply(run)
        except Exception as e:  # noqa: BLE001 - bookkeeping only
            print(f"working set update failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        return result

    # -- goals (`ozagia.core.goals`) ---------------------------------------- #
    def _goal_hints(self, op: str, body: dict[str, Any], harness: str,
                    session: Any, files: list[str], keys: list[str]
                    ) -> dict[str, Any]:
        """`body` with the goal lines for an edit by (harness, session):
        the claimed goals whose targets it touched and the criteria now
        affected, and an advisory note for files outside a claimed goal's
        scope (`goals.edit_hints`). Added to `output` (tool) or `append`
        (after); `meta.goal_hints` counts them. Never blocks or raises."""
        if not isinstance(session, str) or not session or not files:
            return body
        try:
            from ozagia.core import goals as G
            lines = G.edit_hints(self.root, harness, session, files, keys)
        except Exception as e:  # noqa: BLE001 - advisory only
            print(f"goal hints failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return body
        if not lines:
            return body
        field = "output" if op == "tool" else "append"
        text = "\n".join([x for x in [str(body.get(field) or "")] if x] + lines)
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        with self._stats_lock:
            self.goal_hints += len(lines)
        return {**body, field: text, "meta": {**meta, "goal_hints": len(lines)}}

    def goals(self, msg: dict[str, Any]) -> dict[str, Any]:
        """The `goals` op (read-only): a summary of each goal not dropped
        (`all: true`: dropped ones too) from the store as last checked, with
        `mine` for the goals the request's (harness, session) claimed. It
        evaluates nothing; `ozagia goal check` or `goal_check` does."""
        from ozagia.core import goals as G
        harness = msg.get("harness") if isinstance(msg.get("harness"), str) else None
        session = msg.get("session") if isinstance(msg.get("session"), str) else None
        try:
            goals = G.list_goals(self.root, include_dropped=msg.get("all") is True)
        except G.GoalError as e:
            return {"ok": False, "error": str(e)}
        out = [{**G.summary(g), "mine": G.claimed_by(g, harness, session)}
               for g in goals]
        return {"ok": True, "goals": out,
                "text": "\n".join(G.brief_lines(self.root, harness=harness,
                                                session=session, goals=goals,
                                                max_chars=W_GOALS_CHARS)),
                "meta": {"intent": "goals", "keys": [], "goals": len(out)}}

    # -- the created-file ledger (`ozagia.core.files`) ---------------------- #
    def _file_ledger_enabled(self, msg: dict[str, Any]) -> bool:
        """The request's `file_ledger: false` or `$OZAGIA_FILE_LEDGER=0`
        turns the daemon's `after` and session scans off."""
        if msg.get("file_ledger") is False:
            return False
        return os.environ.get(ENV_FILE_LEDGER, "1").strip().lower() not in (
            "0", "false", "no", "off")

    @staticmethod
    def _ledger_reason(inp: dict[str, Any]) -> str:
        reason = inp.get("_reason")
        return reason if isinstance(reason, str) and reason.strip() else "unknown"

    def _files_session_event(self, msg: dict[str, Any], event: Any) -> None:
        """Snapshot the worktree at session start and diff it at session end.

        Bounded and metadata-only. A missing baseline (old daemon, crash,
        pruned state) falls back to one current scan, marked `baseline:
        missing` and without session attribution. Never raises and never
        fails the session event.
        """
        if event not in ("start", "end") or not self._file_ledger_enabled(msg):
            return
        session = msg.get("session")
        if not isinstance(session, str) or not session:
            return
        harness = str(msg.get("harness") or "opencode")
        try:
            from ozagia.core import files as F
            recorded: list[dict[str, Any]] = []
            if event == "start":
                F.session_snapshot(self.root, session, harness=harness)
            else:
                delta = F.session_delta(
                    self.root, session, harness=harness, record=True,
                    reason="unknown", tool="session_event",
                    limit=FILE_LEDGER_SCAN_LIMIT)
                recorded = list(delta.get("recorded") or [])
        except Exception as e:  # noqa: BLE001 - bookkeeping only
            with self._stats_lock:
                self.file_ledger_errors += 1
                self.file_manifest_errors += 1
            print(f"file session manifest failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return
        with self._stats_lock:
            self.file_ledger_scans += 1
            self.file_manifest_sessions += 1
            self.files_recorded += len(recorded)

    def _derived_paths(self, tool: str, inp: Any,
                       limit: int = FILE_LEDGER_PATHS) -> list[str]:
        """Paths a built-in `patch` or `shell` call names in its patch text or
        command (`ozagia.intent.call_paths`), root-relative. The harness may
        not report them in `meta`, or may name only some; deriving is bounded
        and read-only. Never raises."""
        try:
            if tool not in ("patch", "shell") or not isinstance(inp, dict):
                return []
            from ozagia.intent.call_paths import command_paths, patch_paths
            wd = inp.get("workdir")
            cwd = wd if isinstance(wd, str) and wd else "."
            if tool == "patch":
                return patch_paths(inp, root=self.root, cwd=cwd, limit=limit)
            command = inp.get("command")
            if not isinstance(command, str) or not command.strip():
                return []
            return command_paths(command, root=self.root, cwd=cwd,
                                 limit=limit)
        except Exception:  # noqa: BLE001 - bookkeeping only
            return []

    def _files_observe(self, op: str, tool: str, inp: Any, msg: dict[str, Any],
                       body: dict[str, Any]) -> None:
        """Record files a built-in `edit`, `write`, `patch` or `shell` call
        created: the `after` answer's `meta.changed_paths`/`edited_files`, the
        file an edit or write names, and the paths a `patch` or `shell` call
        itself names (merged when the meta list is absent or incomplete). One
        bounded `git status` pathspec run, only for the paths named (never a
        whole-repository scan). Never raises."""
        if op != "after" or tool not in FILE_LEDGER_TOOLS:
            return
        if not isinstance(inp, dict) or not self._file_ledger_enabled(msg):
            return
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        paths: list[str] = []
        for key in ("changed_paths", "edited_files"):
            changed = meta.get(key)
            if isinstance(changed, list):
                paths.extend(p for p in changed if isinstance(p, str))
        if tool in ("edit", "write"):
            rel = self._ws_rel(inp.get("path", inp.get("filePath")))
            if rel:
                paths.append(rel)
        elif tool in ("patch", "shell"):
            paths.extend(self._derived_paths(tool, inp, FILE_LEDGER_PATHS))
        paths = list(dict.fromkeys(paths))[:FILE_LEDGER_PATHS]
        if not paths:
            return
        try:
            from ozagia.core import files as F
            recorded = F.scan_new_files(
                self.root, session=msg.get("session") if isinstance(
                    msg.get("session"), str) else None,
                harness=str(msg.get("harness") or "opencode"),
                reason=self._ledger_reason(inp), tool=tool, paths=paths,
                limit=FILE_LEDGER_SCAN_LIMIT)
        except Exception as e:  # noqa: BLE001 - bookkeeping only
            with self._stats_lock:
                self.file_ledger_errors += 1
            print(f"file ledger update failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return
        with self._stats_lock:
            self.file_ledger_scans += 1
            self.files_recorded += len(recorded)

    # -- library hints for built-in edits (`ozagia.core.library_hints`) ---- #
    @staticmethod
    def _library_seen_key(msg: dict[str, Any],
                          rel: str) -> tuple[str, str, str]:
        return (str(msg.get("harness") or "opencode"),
                str(msg.get("session") or ""), rel)

    def _library_stamp(self, rel: str) -> tuple[int, int] | None:
        try:
            st = (self.root / rel).stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _library_already_annotated(self, msg: dict[str, Any], rel: str) -> bool:
        """Whether this session got a library hint for this exact file
        content recently (a handled call or an earlier `after`)."""
        stamp = self._library_stamp(rel)
        if stamp is None:
            return False
        seen = self.library_seen.get(self._library_seen_key(msg, rel))
        if seen is None:
            return False
        mtime, size, when = seen
        return ((mtime, size) == stamp
                and time.time() - when <= LIBRARY_HINT_DEDUPE_S)

    def _mark_library_annotated(self, msg: dict[str, Any], rel: str) -> None:
        stamp = self._library_stamp(rel)
        if stamp is None:
            return
        key = self._library_seen_key(msg, rel)
        self.library_seen.pop(key, None)
        self.library_seen[key] = (stamp[0], stamp[1], time.time())
        while len(self.library_seen) > LIBRARY_HINT_SEEN_KEPT:
            self.library_seen.pop(next(iter(self.library_seen)))

    def _note_library_hint_handled(self, tool: str, inp: Any,
                                   msg: dict[str, Any],
                                   body: dict[str, Any]) -> None:
        """Remember files a handled edit/write already annotated, so a later
        `after` for the same unchanged content does not repeat the hint."""
        if (tool not in ("edit", "write") or not body.get("handled")
                or body.get("is_error") or body.get("preflight")):
            return
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        for rel in meta.get("edited_files") or []:
            if isinstance(rel, str) and rel:
                self._mark_library_annotated(msg, rel)

    def _library_after_paths(self, tool: str, inp: Any,
                             meta: dict[str, Any]) -> list[str]:
        """Changed paths a built-in `after` names, root-relative, bounded."""
        raw: list[str] = []
        for key in ("edited_files", "changed_paths"):
            values = meta.get(key)
            if isinstance(values, list):
                raw.extend(value for value in values
                           if isinstance(value, str) and value)
        if tool in ("edit", "write") and isinstance(inp, dict):
            rel = self._ws_rel(inp.get("path", inp.get("filePath")))
            if rel:
                raw.append(rel)
        elif tool == "patch":
            # The patch text names every file and move target, even when
            # `meta.edited_files` is absent or incomplete (`intent.patch`).
            raw.extend(self._derived_paths("patch", inp, LIBRARY_HINT_FILES))
        out: list[str] = []
        for rel in raw:
            if rel not in out:
                out.append(rel)
            if len(out) >= LIBRARY_HINT_FILES:
                break
        return out

    def _library_hints_after(self, op: str, tool: str, inp: Any,
                             msg: dict[str, Any], body: dict[str, Any]
                             ) -> dict[str, Any]:
        """Append bounded `[ozagia] library hint:` lines after a built-in
        edit, write or patch. Never fails the tool and never edits code."""
        if op != "after" or tool not in LIBRARY_HINT_TOOLS:
            return body
        if body.get("preflight") or body.get("expired"):
            return body
        req_meta = msg.get("meta") if isinstance(msg.get("meta"), dict) else {}
        if req_meta.get("builtin_error") is True:
            return body
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        if meta.get("intent") == "after_edit_miss":
            return body
        paths = self._library_after_paths(tool, inp, meta)
        if not paths:
            return body
        try:
            from ozagia.core import library_hints as LH
            settings = LH.settings_for(self.root)
            if settings.mode == "off" or settings.max_per_edit <= 0:
                return body
            candidates = [
                rel for rel in paths
                if LH.enabled_for(rel, settings)
                and not self._library_already_annotated(msg, rel)
            ]
            if not candidates:
                return body
            got = LH.hints_for_paths(
                self.root, candidates, settings=settings,
                record=settings.record, max_per_edit=settings.max_per_edit)
            hints = [hint for hint in (got.get("hints") or [])
                     if isinstance(hint, dict) and hint.get("message")]
            hints = hints[:max(0, settings.max_per_edit)]
            suppressed = got.get("suppressed") or []
            # Even with no hint, the same unchanged content should not be
            # recomputed on every `after` in this session.
            for rel in candidates:
                self._mark_library_annotated(msg, rel)
            if not hints:
                if suppressed:
                    meta["library_hints_suppressed"] = (
                        int(meta.get("library_hints_suppressed") or 0)
                        + len(suppressed))
                    body = {**body, "meta": meta}
                return body
            lines = [str(hint["message"]) for hint in hints]
            existing = body.get("append")
            body = {
                **body,
                "append": "\n".join(
                    [value for value in [existing if isinstance(existing, str)
                                         else ""] + lines if value]),
            }
            meta = dict(meta)
            meta["library_hints_builtin"] = len(lines)
            meta["library_hint_builtin_files"] = len({
                str(hint.get("file")) for hint in hints if hint.get("file")})
            if suppressed:
                meta["library_hints_suppressed"] = (
                    int(meta.get("library_hints_suppressed") or 0)
                    + len(suppressed))
            body["meta"] = meta
        except Exception as exc:  # noqa: BLE001 - the tool already ran
            print(f"library hint after failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
        return body

    def _running_jobs(self) -> list[dict[str, Any]]:
        try:
            return [self.jobs.public(r) for r in self.jobs.running()]
        except Exception:  # noqa: BLE001 - the text goes without jobs
            return []

    def _goal_lines(self, harness: str | None, session: str | None,
                    max_chars: int, *, titles: bool = True) -> list[str]:
        """Goal lines for the brief and the working set text (the session's
        claims first; without titles for `free_text: false`), or []
        (`ozagia.core.goals.brief_lines`, through its store cache)."""
        try:
            from ozagia.core import goals as G
            return G.brief_lines(self.root, harness=harness, session=session,
                                 max_chars=min(G.BRIEF_CHARS, max_chars),
                                 titles=titles)
        except Exception:  # noqa: BLE001 - the text goes without goals
            return []

    def toolchain_op(self, msg: dict[str, Any]) -> dict[str, Any]:
        """The `toolchain` op: which external tools a task needs and what
        installing the missing ones would involve (`ozagia.core.toolchain`).

        Inputs: `capabilities` and/or `paths` (a string or a bounded list),
        `model_family`, `probe_versions` and `probe_sudo` (false by default),
        and `refresh` to bypass the cache. The answer is
        `{ok, plan, text, cached}`; the plan is advisory and nothing is
        installed. Plans are cached in memory for `TOOLCHAIN_CACHE_TTL_S`,
        at most `TOOLCHAIN_CACHE_MAX`, keyed by root and request.
        """
        from ozagia.core import toolchain as T
        try:
            capabilities = _bounded_strings(
                msg.get("capabilities"), "capabilities",
                limit=T.MAX_CAPABILITIES, max_chars=TOOLCHAIN_CAPABILITY_CHARS)
            paths = _bounded_strings(
                msg.get("paths"), "paths", limit=T.MAX_PATHS,
                max_chars=T.MAX_PATH_CHARS)
            family_raw = msg.get("model_family")
            if family_raw is not None and not isinstance(family_raw, str):
                raise ValueError("model_family must be a string")
            family = (family_raw or "").strip() or None
            if family and len(family) > TOOLCHAIN_MODEL_FAMILY_CHARS:
                raise ValueError(
                    "model_family exceeds the "
                    f"{TOOLCHAIN_MODEL_FAMILY_CHARS}-character limit")
            probe_versions = _toolchain_flag(msg, "probe_versions")
            probe_sudo = _toolchain_flag(msg, "probe_sudo")
            refresh = _toolchain_flag(msg, "refresh")
        except ValueError as exc:
            with self._stats_lock:
                self.toolchain_counts["toolchain_errors"] += 1
            return {"ok": False, "error": str(exc)}
        key = (str(self.root), tuple(capabilities), tuple(paths),
               family or "", probe_versions, probe_sudo)
        if not refresh:
            with self._stats_lock:
                cached = self.toolchain_cache.get(key)
                if cached is not None:
                    if time.monotonic() - cached[0] <= TOOLCHAIN_CACHE_TTL_S:
                        self.toolchain_cache.move_to_end(key)
                        self.toolchain_counts["toolchain_plans"] += 1
                        self.toolchain_counts["toolchain_cache_hits"] += 1
                        return {"ok": True, "plan": cached[1],
                                "text": cached[2], "cached": True}
                    del self.toolchain_cache[key]
        try:
            plan = T.tool_plan(
                self.root, capabilities=capabilities, paths=paths,
                probe_versions=probe_versions, probe_sudo=probe_sudo)
            text = ("\n".join(T.brief_lines(plan, model_family=family))
                    if family else T.tool_plan_text(plan))
        except T.ToolchainError as exc:
            with self._stats_lock:
                self.toolchain_counts["toolchain_errors"] += 1
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the op must not kill a socket
            with self._stats_lock:
                self.toolchain_counts["toolchain_errors"] += 1
            return {"ok": False, "error": (
                f"toolchain plan failed: {type(exc).__name__}: {exc}")}
        with self._stats_lock:
            self.toolchain_cache[key] = (time.monotonic(), plan, text)
            self.toolchain_cache.move_to_end(key)
            while len(self.toolchain_cache) > TOOLCHAIN_CACHE_MAX:
                self.toolchain_cache.popitem(last=False)
            self.toolchain_counts["toolchain_plans"] += 1
        return {"ok": True, "plan": plan, "text": text, "cached": False}

    def tmp_op(self, msg: dict[str, Any]) -> dict[str, Any]:
        """The `tmp` op: managed temporary space (`ozagia.core.temp_space`).

        `preflight` validates a create request and returns a suggested path
        without creating anything; `create` makes one owned `scratch` entry
        and returns its path, expiry and usage; `info`, `list` and `status`
        are read-only. The harness is a closed allowlist, the session is
        required and bounded, the reason is 1..200 characters without control
        characters, and the TTL is 60..90 days (default seven days). A caller
        never supplies a path: unknown fields (including `path`) are refused.
        """
        from ozagia.core import temp_space as T
        action = msg.get("action")
        if action not in TMP_ACTIONS:
            return {"ok": False, "code": "invalid_action",
                    "error": "action must be one of " + ", ".join(TMP_ACTIONS)}
        unknown = sorted(set(msg) - _TMP_ENVELOPE
                         - _TMP_TRACKED_FIELDS[action])
        if unknown:
            return {"ok": False, "code": "unknown_field",
                    "error": "unknown field(s): " + ", ".join(unknown)}
        try:
            if action == "status":
                return {"ok": True, **T.status(self.root)}
            if action == "info":
                entry_id = msg.get("entry_id")
                if entry_id is None and isinstance(msg.get("id"), str):
                    # Direct callers may put the entry id in `id`; over the
                    # socket that field is the request id.
                    entry_id = msg.get("id")
                if not isinstance(entry_id, str):
                    raise TempOpError("not_found",
                                      "entry_id must be a temporary entry id")
                try:
                    entry = T.get(self.root, entry_id)
                except T.TempNotFound as exc:
                    raise TempOpError("not_found", str(exc)) from exc
                return {"ok": True, "entry": entry}
            if action == "list":
                kind = _tmp_kind(msg, writable=False)
                session = msg.get("session")
                if session is not None:
                    session = _tmp_session(msg)
                owned = _tmp_bool(msg, "owned", None)
                include_all = _tmp_bool(msg, "include_all", False) is True
                limit = _tmp_limit(msg)
                entries = T.list_entries(
                    self.root, kind=kind, session=session, owned=owned,
                    include_all=include_all)
                return {"ok": True, "entries": entries[:limit],
                        "count": len(entries),
                        "truncated": len(entries) > limit, "limit": limit}
            harness = _tmp_harness(msg)
            session = _tmp_session(msg)
            reason = _tmp_reason(msg)
            ttl_s = _tmp_ttl(msg)
            kind = _tmp_kind(msg, writable=True)
            model = _tmp_label(msg, "model")
            run_id = _tmp_label(msg, "run_id")
            session_value = {"harness": harness, "id": session}
            if action == "preflight":
                path = T.suggest_path(self.root, str(kind), reason,
                                      session_value)
                return {"ok": True, "id": None, "entry_id": None,
                        "path": str(path), "exists": False, "kind": kind}
            # Create at the same managed path preflight named, so the write
            # permission is for the directory that is actually made. The
            # plugin echoes the preflight path; the daemon verifies it is
            # inside the managed kind root. Without it, derive a fresh path.
            T.ensure_default_root(self.root, str(kind))
            pinned = msg.get("preflight_path")
            if pinned is not None:
                if not isinstance(pinned, str) or not pinned:
                    raise TempOpError(
                        "invalid_path", "preflight_path must be a non-empty string")
                base = T.default_kind_root(self.root, str(kind)).resolve()
                candidate = Path(pinned).resolve(strict=False)
                if candidate == base or not candidate.is_relative_to(base):
                    raise TempOpError(
                        "invalid_path",
                        "preflight_path must be inside the managed scratch root")
                path = candidate
            else:
                path = T.suggest_path(self.root, str(kind), reason,
                                      session_value)
            entry = T.create(self.root, kind=str(kind), reason=reason,
                             session=session_value, ttl_s=ttl_s, model=model,
                             run_id=run_id, path=path)
            # `id` is the entry id; on the wire the envelope repeats the
            # request id, so `entry_id` carries the entry id unambiguously.
            return {"ok": True, "id": entry["id"],
                    "entry_id": entry["id"], "path": entry["path"],
                    "expires_at": entry.get("expires_at"),
                    "bytes": entry.get("bytes", 0),
                    "files": entry.get("files", 0),
                    "tmpfs": entry.get("tmpfs")}
        except TempOpError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        except T.TempSpaceError as exc:
            return {"ok": False, "code": "temp_error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the op must not kill a socket
            return {"ok": False, "code": "temp_error",
                    "error": "temporary space operation failed: "
                             f"{type(exc).__name__}"}

    def working_set(self, msg: dict[str, Any]) -> dict[str, Any]:
        """The `working_set` op: the session's working set as text, or with
        `since: true` the session-start brief (`ozagia.core.working_set`).
        Without `session`, the session whose working set changed last (of
        `harness`, when given) is used (`meta.resolved: "latest"`)."""
        from ozagia.core import working_set as W
        harness = msg.get("harness") if isinstance(msg.get("harness"), str) and msg.get(
            "harness") else None
        session = msg.get("session") if isinstance(msg.get("session"), str) and msg.get(
            "session") else None
        since = msg.get("since") is True
        # `free_text: false`: only paths, keys, test ids and job ids (no
        # failure messages, command lines or DevSession titles).
        free_text = msg.get("free_text") is not False
        cap = W.BRIEF_MAX_CHARS if since else W.TEXT_MAX_CHARS
        raw = msg.get("max_chars")
        if isinstance(raw, int) and not isinstance(raw, bool):
            cap = max(200, min(raw, cap))
        if since:
            tests, last = self.test_record.view()
            text = W.session_brief(self.root, cap, tests=tests, last_test=last,
                                   jobs=self._running_jobs(),
                                   schedule_reference_sync=True,
                                   goal_lines=self._goal_lines(
                                       harness, session,
                                       cap // W.BRIEF_GOAL_SHARE))
            with self._stats_lock:
                self.working["session_briefs_served"] += 1
            return {"ok": True, "text": text,
                    "meta": {"intent": "session_brief", "keys": [],
                             "tests": len(tests), "chars": len(text)}}
        resolved = "explicit"
        if session is None:
            got = self.sessions.latest(harness)
            resolved = "latest" if got else "none"
            if got:
                harness, session = got
        states = (self.sessions.states(harness, session, msg.get("sub"))
                  if session else [])
        view = W.merge_views([st.work.view() for st in states])
        text = ""
        jobs: list[dict[str, Any]] = []
        if states:
            harness = harness or states[0].harness
            jobs = self._running_jobs()
            plan = W.devsession_line(self.root, free_text=free_text)

            def exists(key: str) -> bool:
                if not key.startswith(("sym:", "test:")):
                    return True
                return self.memory.node_by_key(key) is not None
            # The goal store is read before the graph lock is taken.
            goal_lines = self._goal_lines(harness, session, cap // 4,
                                          titles=free_text)
            with self._graph_lock():
                text = W.working_set_text(
                    view, harness=harness, session=session,
                    compactions=max(st.compactions for st in states),
                    jobs=jobs, plan=plan, key_exists=exists, max_chars=cap,
                    free_text=free_text, goal_lines=goal_lines)
        if text:
            with self._stats_lock:
                self.working["working_sets_served"] += 1
                self.working["working_set_chars"] += len(text)
        return {"ok": True, "text": text,
                "meta": {"intent": "working_set" if text else "working_set_empty",
                         "keys": view.keys[:JOURNAL_KEYS], "harness": harness,
                         "session": session, "resolved": resolved,
                         "jobs": len(jobs), **W.text_meta(view, text)}}

    def _rel(self, path: Any, cwd: Any) -> str | None:
        """`path` (absolute, or relative to `cwd`, else the root) relative
        to the root, or None (outside it, or not a path)."""
        from ozagia.intent.common import resolve
        base = Path(cwd) if isinstance(cwd, str) and os.path.isdir(cwd) else None
        try:
            return resolve(self.root, path, base=base).rel or None
        except Exception:  # noqa: BLE001 - not a path in this root
            return None

    def _owners_op(self, op: str, msg: dict[str, Any]) -> dict[str, Any]:
        """`path_check` {path, cwd?}: does the path exist, and if not, the
        nearest existing paths (`ozagia.intent.paths`; without the graph's
        same-name files, `graph: false`, when the graph lock is busy). `owner_check`
        {paths, cwd?} with the request's harness and session: the ownership
        warnings an edit of those files would get. `who` {path}: the
        sessions that edited it (`ozagia.core.owners.who`)."""
        from ozagia.core import owners as O
        cwd = msg.get("cwd")
        if op == "path_check":
            from ozagia.intent import paths as PA
            path = msg.get("path")
            if not isinstance(path, str) or not path:
                return {"ok": False, "error": "path is required"}
            p = Path(os.path.expanduser(path))
            if not p.is_absolute():
                p = (Path(cwd) if isinstance(cwd, str) and cwd else self.root) / p
            if p.exists():
                return {"ok": True, "exists": True, "line": "", "candidates": [],
                        "meta": {"intent": "path_check", "keys": []}}
            base = cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None
            # Never wait on the graph lock (a reindex can hold it for long):
            # when it is busy, answer from the file system only.
            graph = self.lock.acquire(blocking=False)
            try:
                cands = PA.candidates(self.root, path, base=base,
                                      memory=self.memory if graph else None)
            finally:
                if graph:
                    self.lock.release()
            line = PA.suggestion_line(path, cands)
            return {"ok": True, "exists": False, "line": line,
                    "candidates": [{"path": str(c.path), "rel": c.rel,
                                    "key": c.key, "why": c.why} for c in cands],
                    "graph": graph,
                    "meta": {"intent": "path_check",
                             "keys": [c.key for c in cands if c.key],
                             "path_suggestions": 1 if line else 0}}
        if op == "owner_check":
            paths = msg.get("paths")
            if not isinstance(paths, list):
                return {"ok": False, "error": "paths must be a list"}
            warnings: list[str] = []
            for path in paths[:20]:
                rel = self._rel(path, cwd)
                if rel is None:
                    continue
                try:
                    warnings.extend(self.owners.warnings(
                        self.memory.journal, rel, msg.get("harness"),
                        msg.get("session")))
                except Exception:  # noqa: BLE001 - advisory
                    continue
            return {"ok": True, "warnings": warnings,
                    "meta": {"intent": "owner_check", "keys": [],
                             "ownership_warnings": len(warnings)}}
        rel = self._rel(msg.get("path"), cwd)
        if rel is None:
            return {"ok": False, "error": "path must name a file in the root"}
        report = O.who(self.root, rel, self.memory.journal,
                       live=self.owners.live)
        return {"ok": True, "who": report,
                "meta": {"intent": "who", "keys": []}}

    def _job_meta(self, body: dict[str, Any]) -> dict[str, Any]:
        """`body` with `meta.jobs_running` and `meta.job_seq` while a job
        runs, so a plugin knows to watch for job ends (`job_events`)."""
        n = self.jobs.running_count()
        if not n or not isinstance(body.get("meta"), dict):
            return body
        return {**body, "meta": {**body["meta"], "jobs_running": n,
                                 "job_seq": self.jobs.event_seq}}

    def _capture_ps(self, inp: dict[str, Any], msg: dict[str, Any]
                    ) -> dict[str, Any] | None:
        """The `tool` answer for a `pgrep`/`ps -p` command about a job's
        process (`jobs.capture_ps`), or None."""
        command = inp.get("command")
        if not isinstance(command, str):
            return None
        out = self.jobs.capture_ps(command, count=msg.get("preflight") is not True)
        if out is None:
            return None
        from ozagia import intent
        perm = intent.permission_request(self.memory, "shell", inp)
        meta = {"intent": "shell_ps", "keys": [], "ms": 0.0}
        if msg.get("preflight") is True:
            return {"ok": True, "handled": False, "preflight": True,
                    "reason": "permission required", "permission": perm,
                    "meta": {**meta, "intent": "shell_preflight"}}
        return {"ok": True, **self._job_meta({
            "handled": True, "output": out, "is_error": False,
            "permission": perm, "meta": meta})}

    def begin(self) -> None:
        """A request arrived (for the idle timeout)."""
        with self._stats_lock:
            self.active += 1
            self.last_activity = time.monotonic()

    def end(self) -> None:
        with self._stats_lock:
            self.active -= 1
            self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        """Seconds since the last request ended or the last job exited, or
        0 while a request or a job runs."""
        if self.jobs.running_count():
            return 0.0
        with self._stats_lock:
            if self.active:
                return 0.0
            return time.monotonic() - max(self.last_activity, self.jobs.last_end)

    def record(self, msg: dict[str, Any], resp: dict[str, Any],
               ms: float) -> None:
        op = str(msg.get("op"))
        meta = resp.get("meta") or {}
        tool = msg.get("tool") if op in ("tool", "after") else None
        handled = resp.get("handled") if op == "tool" else None
        expired = bool(resp.get("expired"))
        req_meta = msg.get("meta") if isinstance(msg.get("meta"), dict) else {}
        fallback = req_meta.get("fallback") if op == "after" else None
        with self._stats_lock:
            self.by_op[op] = self.by_op.get(op, 0) + 1
            if tool:
                self.by_tool[str(tool)] = self.by_tool.get(str(tool), 0) + 1
            harness = msg.get("harness")
            if isinstance(harness, str) and harness:
                self.by_harness[harness] = self.by_harness.get(harness, 0) + 1
            if meta.get("intent"):
                it = str(meta["intent"])
                self.by_intent[it] = self.by_intent.get(it, 0) + 1
            rr = meta.get("region_replies")
            if isinstance(rr, int) and not isinstance(rr, bool) and rr > 0:
                self.region_replies += rr
            for name in META_COUNTERS:
                n = meta.get(name)
                if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                    setattr(self, name, getattr(self, name) + n)
            for name in self.lint:
                n = meta.get(name)
                if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                    self.lint[name] += n
            for name in self.replace:
                n = meta.get(name)
                if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                    self.replace[name] += n
            if handled is not None:
                self.handled["true" if handled else "false"] += 1
            if meta.get("repeat") in ("stub", "note"):
                self.reads["repeats_seen"] += 1
                self.reads["stubs_served" if meta["repeat"] == "stub"
                           else "notes_served"] += 1
            saved = meta.get("bytes_saved")
            if isinstance(saved, int) and not isinstance(saved, bool) and saved > 0:
                self.reads["bytes_saved"] += saved
            tests = meta.get("tests")
            if isinstance(tests, dict):
                for k in TEST_COUNTERS:
                    v = tests.get(k)
                    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                        self.tests[k] += v
            if not resp.get("ok"):
                self.errors += 1
            if resp.get("is_error"):
                self.tool_errors += 1
            if op == "tool":
                if not resp.get("ok"):
                    self.tool_failures += 1
                elif expired:
                    self.expired += 1
                elif resp.get("preflight"):
                    self.preflights += 1
                elif not handled:
                    self.passthrough += 1
                elif resp.get("late"):
                    self.late += 1
            if (op == "tool" and tool == "shell" and resp.get("ok")
                    and not expired and not resp.get("preflight")):
                it = str(meta.get("intent") or "")
                if not handled:
                    self.shell["passthrough"] += 1
                    reason = shell_reason_key(
                        str(resp.get("reason") or ""))[:120]
                    if (reason in self.shell_reasons
                            or len(self.shell_reasons) < SHELL_REASONS):
                        self.shell_reasons[reason] = (
                            self.shell_reasons.get(reason, 0) + 1)
                    else:
                        self.shell_reasons["(other)"] = (
                            self.shell_reasons.get("(other)", 0) + 1)
                elif it == "shell_refused":
                    self.shell["refused"] += 1
                else:
                    self.shell["captured"] += 1
                    self.shell_intents[it] = self.shell_intents.get(it, 0) + 1
            if op == "after":
                if expired:
                    self.after_expired += 1
                if fallback:
                    self.plugin_fallbacks += 1
                    if fallback == "unknown_outcome":
                        self.unknown_outcomes += 1
            if op in ("tool", "after"):
                self.ms_total += ms
                self.ms_count += 1
                self.ms_max = max(self.ms_max, ms)
            reason = resp.get("reason") or resp.get("error")
            if isinstance(reason, str):
                reason = self._redact_response_text(reason)
            session = msg.get("session")
            if isinstance(session, str):
                session = self._redact_response_text(session)
            harness = msg.get("harness")
            if isinstance(harness, str):
                harness = self._redact_response_text(harness)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": op,
                   "tool": tool, "intent": meta.get("intent"),
                   "ok": resp.get("ok") if isinstance(resp.get("ok"), bool) else None,
                   "handled": handled, "is_error": resp.get("is_error"),
                   "preflight": resp.get("preflight") or None,
                   "expired": expired or None, "ms": round(ms, 1),
                   "keys": list(meta.get("keys") or [])[:JOURNAL_KEYS],
                   "reason": reason,
                   "fallback": fallback,
                   "session": session,
                   "harness": harness,
                   "files": list(meta.get("edited_files") or []) or None}
            try:
                self.journal.parent.mkdir(parents=True, exist_ok=True)
                with self.journal.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
            except OSError as e:
                print(f"journal write failed: {e}", file=sys.stderr, flush=True)
        try:
            from ozagia.session_profile_cli import store_daemon_event
            store_daemon_event(self.root, rec)
        except (OSError, ValueError, TypeError) as e:
            print(f"session profile write failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)

    @contextlib.contextmanager
    def _unlocked(self):
        """Release the graph lock this thread holds, and take it again
        (recording the wait) when the block ends."""
        self.lock.release()
        try:
            yield
        finally:
            started = time.perf_counter()
            self.lock.acquire()
            wait_ms = (time.perf_counter() - started) * 1000.0
            with self._stats_lock:
                self.lock_wait_total_ms += wait_ms
                self.lock_wait_max_ms = max(self.lock_wait_max_ms, wait_ms)
                self.lock_wait_count += 1

    @contextlib.contextmanager
    def _graph_lock(self):
        """Acquire the graph lock and record how long a request waited."""
        started = time.perf_counter()
        self.lock.acquire()
        wait_ms = (time.perf_counter() - started) * 1000.0
        with self._stats_lock:
            self.lock_wait_total_ms += wait_ms
            self.lock_wait_max_ms = max(self.lock_wait_max_ms, wait_ms)
            self.lock_wait_count += 1
        try:
            yield
        finally:
            self.lock.release()

    def _ops_nodes(self) -> int:
        try:
            from ozagia.core.ops_graph import ops_nodes
            return len(ops_nodes(self.memory))
        except Exception:  # noqa: BLE001 - stats must not fail
            return 0

    def stats(self) -> dict[str, Any]:
        job_stats = self.jobs.stats()
        with self._stats_lock:
            return {
                "root": str(self.root), "pid": os.getpid(),
                "uptime_s": round(time.time() - self.started, 1),
                "by_op": dict(self.by_op), "by_tool": dict(self.by_tool),
                "by_intent": dict(self.by_intent),
                "by_harness": dict(self.by_harness),
                "idle_timeout_s": self.idle_timeout,
                "handled": dict(self.handled),
                "passthrough": self.passthrough,
                "expired": self.expired,
                "after_expired": self.after_expired,
                "late": self.late,
                "plugin_fallbacks": self.plugin_fallbacks,
                "unknown_outcomes": self.unknown_outcomes,
                "fallbacks": self.plugin_fallbacks,
                "errors": self.errors,
                "tool_errors": self.tool_errors,
                "hellos": len(self.hellos),
                "hello_sessions": [h.get("session") for h in self.hellos
                                   if h.get("session")],
                "preflights": self.preflights,
                "shell": {
                    **self.shell,
                    "capture_rate": (
                        round(self.shell["captured"]
                              / (self.shell["captured"] + self.shell["passthrough"]), 4)
                        if self.shell["captured"] + self.shell["passthrough"]
                        else None),
                    "by_intent": dict(self.shell_intents),
                    "passthrough_reasons": dict(sorted(
                        self.shell_reasons.items(),
                        key=lambda kv: (-kv[1], kv[0]))[:SHELL_REASONS_SHOWN]),
                },
                # Structured data reads (`json:`/`yaml:`/`toml:` queries),
                # outlines of large structured files, captured `jq` calls.
                "structured_queries": self.by_intent.get("read_structured", 0),
                "structured_outlines": self.by_intent.get(
                    "read_structured_outline", 0),
                "jq_captures": self.by_intent.get("shell_jq", 0),
                # The read ledger (`ozagia.intent.ledger`): repeats of an
                # unchanged range (stubbed or noted), outlines served for
                # large text files, and the bytes they kept out of context.
                **self.reads,
                "outlines_served": (self.by_intent.get("read_outline", 0)
                                    + self.by_intent.get("shell_read_outline", 0)),
                "ledger": self.sessions.stats(),
                # Patch and edit miss recovery: patches checked before
                # apply_patch ran, those with failing hunks (the hook denied
                # them with region replies), region replies sent (patch
                # hunks and edit misses), key-addressed edits applied.
                "patch_checks": (self.by_intent.get("patch_check_ok", 0)
                                 + self.by_intent.get("patch_check_miss", 0)),
                "patch_misses_caught": self.by_intent.get("patch_check_miss", 0),
                "region_replies": self.region_replies,
                "key_edits": self.by_intent.get("edit_key", 0),
                # Path resolution and ownership (`ozagia.intent.paths`,
                # `ozagia.core.owners`): answers that named existing paths
                # for a missing one, warnings about another live session's
                # uncommitted edits, and the sessions live now.
                "path_suggestions": self.path_suggestions,
                "ownership_warnings": self.ownership_warnings,
                # Operations graph (`ozagia.core.ops_graph`, opt-in with the
                # IaC adapter): ops nodes in the graph, and `[ozagia] ops:`
                # lines added after podman/docker/compose logs, journalctl
                # and systemctl status.
                "ops_nodes": self._ops_nodes(),
                "ops_after_annotations": self.ops_after_annotations,
                "live_sessions": self.owners.live_sessions(),
                # Fetch fallback (`ozagia.research.fallback`): research_fetch
                # hints after failed web fetches, and the research_fetch
                # calls (persisted by the MCP tool) and their cache hits.
                "fetch_fallback_hints": self.fetch_fallback_hints,
                **_research_fetch_counters(self.root),
                # Created-file ledger (`ozagia.core.files`): rows recorded
                # (handled writes, `after` scans, session deltas), scans run,
                # ledger/git failures, and session manifests written or
                # diffed (with manifest failures).
                "files_recorded": self.files_recorded,
                "file_ledger_scans": self.file_ledger_scans,
                "file_ledger_errors": self.file_ledger_errors,
                "file_manifest_sessions": self.file_manifest_sessions,
                "file_manifest_errors": self.file_manifest_errors,
                # Library-first hints (`ozagia.core.library_hints`): shown,
                # edited files with a hint, and suppressed candidates.
                "library_hints": self.library_hints,
                "library_hint_files": self.library_hint_files,
                "library_hints_suppressed": self.library_hints_suppressed,
                # The same, from built-in edit/write/patch `after` answers.
                "library_hints_builtin": self.library_hints_builtin,
                "library_hint_builtin_files": self.library_hint_builtin_files,
                # Goal lines added after edits (`ozagia.core.goals`).
                "goal_hints": self.goal_hints,
                # Ranked repo-map delivery in `hint`: maps delivered,
                # requests skipped, and the estimated tokens and top file
                # paths of the delivered maps.
                "repo_map_delivered": self.repo_map_delivered,
                "repo_map_skipped": self.repo_map_skipped,
                "repo_map_tokens": self.repo_map_tokens,
                "repo_map_files": self.repo_map_files,
                "repo_map_last_skip": self.repo_map_last_skip,
                # Secrets (`ozagia.core.secrets`): session captures, status
                # checks, response redactions and errors. No value is kept in
                # the store, a record, a log or a response.
                "secret_captures": self.secret_captures,
                "secret_checks": self.secret_checks,
                "secret_redactions": self.secret_redactions,
                "secret_errors": self.secret_errors,
                "secret_store": self.secret_store.describe(),
                # Lint inside the edit, and captured `ruff check` calls.
                **self.lint,
                # Multi-file replace: previews, applies, files written.
                **self.replace,
                "ruff_captures": self.by_intent.get("shell_ruff", 0),
                # Background jobs (`ozagia.daemon.jobs`): started, waits and
                # the seconds spent in them, and waits that returned new
                # output or the job's end (each one a poll the agent did not
                # make); `jobs` has the rest of the job counters.
                **{k: job_stats[k] for k in ("jobs_started", "waits",
                                            "wait_seconds", "polls_avoided",
                                            "jobs_resource_killed",
                                            "resource_events",
                                            "resource_peak_mb")},
                "jobs": job_stats,
                # The working set (`ozagia.core.working_set`): working sets
                # handed back after a compaction (or to dev_working_set),
                # their characters, and session-start briefs from the daemon.
                **self.working,
                # Toolchain plans (`ozagia.core.toolchain`): plans served
                # (cache hits included), cache hits, and invalid/failed
                # requests.
                **self.toolchain_counts,
                # Affected-test selection and the test-result cache: subset
                # runs, tests not run because no change reached them, tests
                # answered from the cache, and runs that fell back to the
                # whole suite while selection was on.
                **self.tests,
                "permission_assert": _last(self.hellos, "permission_assert"),
                "codemode_tools": sorted({
                    name for h in self.hellos
                    for name, info in (h.get("tools") or {}).items()
                    if isinstance(info, dict) and info.get("codemode") is not False}),
                "mean_ms": (round(self.ms_total / self.ms_count, 1)
                            if self.ms_count else 0.0),
                "max_ms": round(self.ms_max, 1),
                "lock_wait_ms_mean": (round(self.lock_wait_total_ms
                                            / self.lock_wait_count, 2)
                                      if self.lock_wait_count else 0.0),
                "lock_wait_ms_max": round(self.lock_wait_max_ms, 2),
                "lock_wait_count": self.lock_wait_count,
                "journal": str(self.journal),
            }

    def shutdown_later(self) -> None:
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def _encode(resp: dict[str, Any]) -> bytes:
    data = json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) <= P.MAX_MESSAGE_BYTES:
        return data
    small: dict[str, Any] = {"id": resp.get("id"), "ok": True}
    if "append" in resp:
        small.update(append="", meta=resp.get("meta") or {})
    elif "handled" in resp:
        small.update(handled=False, reason="response exceeds MAX_MESSAGE_BYTES",
                     meta=resp.get("meta") or {})
    else:
        small = {"id": resp.get("id"), "ok": False,
                 "error": "response exceeds MAX_MESSAGE_BYTES"}
    return json.dumps(small, ensure_ascii=False).encode("utf-8") + b"\n"


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        state = self.server.state
        while True:
            try:
                line = self.rfile.readline(P.MAX_MESSAGE_BYTES + 1)
            except (ConnectionError, OSError):
                return
            if not line:
                return
            if len(line) > P.MAX_MESSAGE_BYTES:
                resp = state.redact_response(
                    {"id": None, "ok": False,
                     "error": f"message exceeds {P.MAX_MESSAGE_BYTES} "
                              f"bytes; connection closed"})
                self._send(resp)
                state.record({"op": "oversized"},
                             {"ok": False, "error": resp.get("error")}, 0.0)
                return
            if not line.strip():
                continue
            t0 = time.perf_counter()
            msg: Any = None
            state.begin()
            try:
                msg = json.loads(line)
            except (ValueError, UnicodeDecodeError) as e:
                resp = {"id": None, "ok": False, "error": f"bad JSON: {e}"}
            else:
                if not isinstance(msg, dict):
                    resp = {"id": None, "ok": False,
                            "error": "a request must be a JSON object"}
                else:
                    try:
                        resp = {"id": msg.get("id"), **state.dispatch(msg)}
                        # A handler's own `id` field (the `tmp` preflight
                        # reports `id: null`) must not replace the request id.
                        resp["id"] = msg.get("id")
                    except Exception as e:  # noqa: BLE001 - reported
                        resp = {"id": msg.get("id"), "ok": False,
                                "error": f"{type(e).__name__}: {e}"}
            finally:
                state.end()
            ms = (time.perf_counter() - t0) * 1000
            resp = state.redact_response(resp)
            state.record(msg if isinstance(msg, dict) else {"op": "invalid"},
                         resp, ms)
            if not self._send(resp):
                return
            if isinstance(msg, dict) and msg.get("op") == "shutdown":
                state.shutdown_later()
                return

    def _send(self, resp: dict[str, Any]) -> bool:
        try:
            self.wfile.write(_encode(resp))
            self.wfile.flush()
            return True
        except (ConnectionError, OSError):
            return False


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, state: DaemonState) -> None:
        self.state = state
        super().__init__(path, _Handler)

    def server_bind(self) -> None:
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)


def _clear_stale(sock: Path) -> None:
    """Remove a socket file no daemon answers on; refuse anything else."""
    try:
        st = os.lstat(sock)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise DaemonStartError(f"{sock} exists and is not a socket")
    from ozagia.daemon.client import DaemonClient, DaemonError
    try:
        with DaemonClient(sock, timeout=STALE_PING_TIMEOUT_S) as c:
            c.ping()
    except DaemonError:
        sock.unlink()
        return
    raise DaemonStartError(f"a daemon already answers on {sock}")


def _take_lock(root: Path, sock: Path, lock_fd: int | None) -> int:
    """Hold the daemon lock (`ozagia.daemon.autostart.lock_path`) for this
    process's life and return its descriptor: `lock_fd` when the starter
    handed over the locked file, else a new one. Raise `DaemonStartError`
    when another process holds it (a daemon loading, running or writing its
    snapshot on the way out)."""
    import fcntl

    from ozagia.daemon.autostart import lock_path
    lock_p = lock_path(root, sock)
    if lock_fd is None:
        lock_p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_p, os.O_RDWR | os.O_CREAT, 0o600)
    else:
        fd = lock_fd
    try:
        # On an inherited descriptor this succeeds: the lock is on the
        # shared open file, which the starter already locked.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        except OSError:
            holder = ""
        if lock_fd is None:
            os.close(fd)
        # Only ask; the holder's socket is its own to remove.
        from ozagia.daemon.client import DaemonClient, DaemonError
        try:
            with DaemonClient(sock, timeout=STALE_PING_TIMEOUT_S) as c:
                c.ping()
        except DaemonError:
            pass
        else:
            raise DaemonStartError(f"a daemon already answers on {sock}") from None
        pid = f" (pid {holder})" if holder.isdigit() else ""
        raise DaemonStartError(
            f"another daemon for {root} holds {lock_p}{pid}: it is loading, "
            "running or shutting down; not starting a second one") from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def _parent_gone(parent_pid: int) -> bool:
    if os.getppid() != parent_pid:
        return True
    try:
        os.kill(parent_pid, 0)
    except OSError:
        return True
    return False


def _watch_parent(state: DaemonState, parent_pid: int,
                  stop: threading.Event) -> None:
    while not stop.wait(PARENT_POLL_S):
        if _parent_gone(parent_pid):
            print(f"parent {parent_pid} is gone; shutting down",
                  file=sys.stderr, flush=True)
            state.shutdown_later()
            return


def _watch_idle(state: DaemonState, stop: threading.Event) -> None:
    limit = state.idle_timeout
    assert limit is not None
    poll = min(IDLE_POLL_S, max(limit / 4, 0.05))
    while not stop.wait(poll):
        if state.idle_for() >= limit:
            print(f"idle for {limit:g} s; shutting down", file=sys.stderr, flush=True)
            state.idle_shutdown = True
            state.shutdown_later()
            return


def serve(root: str | Path, *, dump: str | Path | None = None,
          journal: str | Path | None = None, sock: str | Path | None = None,
          build_if_missing: bool = True, ready_stream: Any = None,
          parent_pid: int | None = None,
          idle_timeout: float | None = None, lock_fd: int | None = None) -> int:
    """Run the daemon until shutdown; returns an exit code. With
    `parent_pid`, it also shuts down once that process is no longer its
    parent or no longer exists; with `idle_timeout`, once no request has
    come for that many seconds.

    The daemon holds its lock (`_take_lock`; `lock_fd` is a locked
    descriptor handed over by `ozagia.daemon.autostart.ensure`) from before
    the snapshot load until it exits, and refuses to start when another
    process holds it."""
    root_p = Path(root).resolve()
    from ozagia.config import state_path
    sock_p = Path(sock) if sock else P.socket_path(root_p)
    journal_p = Path(journal) if journal else state_path(root_p, JOURNAL_NAME)
    if len(os.fsencode(str(sock_p))) > SUN_PATH_MAX:
        raise DaemonStartError(f"socket path too long: {sock_p}")
    # Refuse a live or loading daemon before the (slow) snapshot load.
    sock_p.parent.mkdir(parents=True, exist_ok=True)
    lock = _take_lock(root_p, sock_p, lock_fd)
    try:
        return _serve_locked(root_p, sock_p, journal_p, dump=dump,
                             build_if_missing=build_if_missing,
                             ready_stream=ready_stream, parent_pid=parent_pid,
                             idle_timeout=idle_timeout)
    finally:
        os.close(lock)


def _serve_locked(root_p: Path, sock_p: Path, journal_p: Path, *,
                  dump: str | Path | None, build_if_missing: bool,
                  ready_stream: Any, parent_pid: int | None,
                  idle_timeout: float | None) -> int:
    _clear_stale(sock_p)
    memory = load_memory(root_p, dump, build_if_missing=build_if_missing)
    # Rewriting the snapshot after every reingest costs ~1 s on a large repo;
    # the daemon keeps the graph in memory and writes it once on exit.
    memory.defer_writes = True
    state = DaemonState(root_p, memory, journal_p, test_env=captured_test_env(),
                        idle_timeout=idle_timeout)
    _clear_stale(sock_p)
    server = _Server(str(sock_p), state)
    state.server = server
    inode = os.stat(sock_p).st_ino

    def on_signal(signum: int, _frame: Any) -> None:
        state.shutdown_later()

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, on_signal)
    out = ready_stream or sys.stdout
    stop_watch = threading.Event()
    if parent_pid:
        threading.Thread(target=_watch_parent, name="ozagia-parent-watch",
                         args=(state, int(parent_pid), stop_watch),
                         daemon=True).start()
    if state.idle_timeout:
        state.last_activity = time.monotonic()
        threading.Thread(target=_watch_idle, name="ozagia-idle-watch",
                         args=(state, stop_watch), daemon=True).start()
    try:
        print(f"ready {sock_p}", file=out, flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        stop_watch.set()
        server.server_close()
        try:
            state.jobs.shutdown()
        except Exception as e:  # noqa: BLE001 - the snapshot still gets written
            print(f"job shutdown failed: {e}", file=sys.stderr, flush=True)
        if getattr(memory, "snapshot_dirty", False):
            try:
                memory.write_snapshot()
                memory.snapshot_dirty = False
            except OSError as e:
                print(f"snapshot write failed: {e}", file=sys.stderr, flush=True)
        try:
            if os.stat(sock_p).st_ino == inode:
                sock_p.unlink()
        except OSError:
            pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


__all__ = ["DaemonStartError", "DaemonState", "IDLE_POLL_S", "JOURNAL_NAME",
           "PARENT_POLL_S",
           "captured_test_env", "load_memory", "serve"]
