#!/usr/bin/env python3
"""Guarded local-Qwen runner for the M6 map-arm comparison.

Default mode is an offline dry run: it writes the frozen schedule and a
manifest and starts no process, model, network request, MCP server or daemon.
Live mode needs both ``--live`` and ``OZAGIA_EVAL_LIVE=1``; it still checks the
twelve-GB disk floor, source cleanliness, corpus hashes, the local endpoint and
the MCP tool surface before the first session.

Usage:

    PYTHONPATH=$PWD/src python evaluation/m6_live/run_m6.py --dry-run
    PYTHONPATH=$PWD/src OZAGIA_EVAL_LIVE=1 python evaluation/m6_live/run_m6.py \
        --live --source /path/to/checkout --out /tmp/ozagia-m6-live
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import m6_live_lib as L  # noqa: E402

DEFAULT_OUT = Path("/tmp/ozagia-m6-live")
DISK_FLOOR_BYTES = 3 * 1024 * 1024 * 1024
DAEMON_READY_TIMEOUT_S = 180
SNAPSHOT_TIMEOUT_S = 1200
GIT_TIMEOUT_S = 180
DEFAULT_API_KEY_FILE = Path.home() / ".config/opencode/hyperqwen-api-key"
LIVE_ENV = "OZAGIA_EVAL_LIVE"
SOURCE_MANIFEST = "source_manifest.json"
RUNS_JSONL = "runs.jsonl"
MANIFEST_JSON = "manifest.json"
SCHEDULE_JSON = "schedule.json"
SUMMARY_JSON = "summary.json"
REPORT_MD = "report.md"


class RunRefused(RuntimeError):
    """A preflight gate failed; no model session may start."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=timeout,
                          check=False, stdin=subprocess.DEVNULL)


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


def _git(source: Path, *args: str, home: Path) -> str:
    proc = _run(["git", "-C", str(source), *args], env=_git_env(home))
    if proc.returncode != 0:
        raise RunRefused(f"git {' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def _source_pin(source: Path, base: str, runtime: Path) -> dict[str, Any]:
    home = runtime / "git-home"
    home.mkdir(parents=True, exist_ok=True)
    status = _git(source, "status", "--porcelain", "--untracked-files=normal", home=home)
    if status.strip():
        first = "\n".join(status.splitlines()[:10])
        raise RunRefused(f"source checkout is dirty; refusing to run:\n{first}")
    head = _git(source, "rev-parse", "HEAD", home=home).strip()
    commit = _git(source, "rev-parse", "--verify", f"{base}^{{commit}}", home=home).strip()
    if commit != head:
        raise RunRefused(
            f"--base resolves to {commit}, not the checked-out HEAD {head}; "
            "check out the pinned commit instead of evaluating a different tree")
    tree = _git(source, "rev-parse", f"{commit}^{{tree}}", home=home).strip()
    entries: list[dict[str, Any]] = []
    raw = _git(source, "ls-tree", "-r", "-z", "--full-tree", commit, home=home)
    for item in raw.split("\0"):
        if not item:
            continue
        meta, _tab, path = item.partition("\t")
        fields = meta.split()
        if len(fields) < 3:
            continue
        mode, kind, blob = fields[0], fields[1], fields[2]
        row: dict[str, Any] = {"path": path, "mode": mode, "kind": kind, "blob": blob}
        if kind == "blob":
            target = source / path
            if not target.is_file():
                raise RunRefused(f"tracked file is missing from the checkout: {path}")
            data = target.read_bytes()
            row["sha256"] = L.sha256_bytes(data)
            row["size"] = len(data)
        entries.append(row)
    entries.sort(key=lambda item: item["path"])
    manifest = {
        "schema": "ozagia.m6_live.source_manifest/1",
        "commit": commit,
        "tree": tree,
        "entries": entries,
    }
    digest = L.sha256_text(L.canonical_json(manifest))
    return {"repo": str(source), "commit": commit, "tree": tree,
            "clean": True, "files": len(entries),
            "manifest_sha256": digest, "manifest": manifest}


def _disk_check(out: Path) -> int:
    probe = out
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < DISK_FLOOR_BYTES:
        raise RunRefused(
            f"disk free is {free / (1024 ** 3):.2f} GiB, below the 3 GiB floor at {probe}")
    return free


def _tool_env(source: Path, runtime: Path, *, work: Path | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(runtime / "tool-home"),
        "XDG_CONFIG_HOME": str(runtime / "tool-xdg/config"),
        "XDG_DATA_HOME": str(runtime / "tool-xdg/data"),
        "XDG_CACHE_HOME": str(runtime / "tool-xdg/cache"),
        "XDG_STATE_HOME": str(runtime / "tool-xdg/state"),
        "TMPDIR": str(runtime / "tool-tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONPATH": str(source / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME", "TMPDIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
    if work is not None:
        env["OZAGIA_ROOT"] = str(work)
        env["OZAGIA_STATE_DIR"] = str(work / ".ozagia")
    return env


def _check_tool_python(tool_python: str, source: Path, runtime: Path) -> dict[str, Any]:
    code = (
        "import json, ozagia\n"
        "from pathlib import Path\n"
        "print(json.dumps({'file': str(Path(ozagia.__file__).resolve())}))\n"
    )
    proc = _run([tool_python, "-c", code], env=_tool_env(source, runtime), timeout=60)
    if proc.returncode != 0:
        raise RunRefused(f"tool interpreter cannot import ozagia: {proc.stderr.strip()[:400]}")
    try:
        info = json.loads(proc.stdout)
    except ValueError as exc:
        raise RunRefused(f"tool interpreter probe returned non-JSON: {proc.stdout[:200]}") from exc
    file = Path(str(info.get("file", ""))).resolve()
    if not file.is_relative_to((source / "src").resolve()):
        raise RunRefused(
            f"tool interpreter imports ozagia from {file}, not from the pinned source tree")
    return {"path": str(Path(tool_python).resolve()), "ozagia_file": str(file)}


def _mcp_surface(tool_python: str, source: Path, runtime: Path) -> dict[str, Any]:
    code = (
        "import json\n"
        "from ozagia.integrations.mcp.server import _TOOLS\n"
        "print(json.dumps(sorted(str(t['name']) for t in _TOOLS)))\n"
    )
    proc = _run([tool_python, "-c", code], env=_tool_env(source, runtime), timeout=60)
    if proc.returncode != 0:
        raise RunRefused(f"MCP tool table probe failed: {proc.stderr.strip()[:400]}")
    try:
        tools = json.loads(proc.stdout)
    except ValueError as exc:
        raise RunRefused(f"MCP tool table probe returned non-JSON: {proc.stdout[:200]}") from exc
    names = set(str(item) for item in tools)
    for required in ("dev_maps", "dev_capabilities"):
        if required not in names:
            raise RunRefused(f"the MCP server does not declare {required}")
    unknown = [tool for tool in (*L.MCP_READ_TOOLS, *L.MCP_MUTATING_TOOLS) if tool not in names]
    if unknown:
        raise RunRefused(f"MCP permission list names unknown tools: {', '.join(unknown)}")
    overlap = set(L.MCP_READ_TOOLS) & set(L.MCP_MUTATING_TOOLS)
    if overlap:
        raise RunRefused(f"MCP read/mutating lists overlap: {', '.join(sorted(overlap))}")
    return {"server": L.MCP_SERVER_NAME, "tool_count": len(names),
            "dev_maps": "dev_maps" in names, "dev_capabilities": "dev_capabilities" in names,
            "read_tools": list(L.MCP_READ_TOOLS), "mutating_tools": list(L.MCP_MUTATING_TOOLS),
            "permission_actions": {
                tool: L.mcp_permission_action(tool)
                for tool in (*L.MCP_READ_TOOLS, *L.MCP_MUTATING_TOOLS)}}


def _load_api_key(path: Path) -> str:
    try:
        value = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RunRefused(
            f"cannot read the local API key file ({exc}); pass --api-key-file") from exc
    if not value:
        raise RunRefused("the local API key file is empty")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _health_check(api_key: str) -> dict[str, Any]:
    url = L.ENDPOINT.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read(2 * 1024 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RunRefused(f"local model health check failed for {url}: {exc}") from exc
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise RunRefused(f"local model health check returned non-JSON from {url}") from exc
    rows = doc.get("data", doc.get("models", []))
    ids = [str(item.get("id")) for item in rows if isinstance(item, dict) and item.get("id")]
    if L.MODEL_ID not in ids and L.MODEL not in ids:
        raise RunRefused(
            f"local endpoint does not serve {L.MODEL_ID!r}; served: {', '.join(ids) or 'none'}")
    return {"url": url, "configured_model": L.MODEL, "served_models": sorted(ids)}


def _opencode_version(opencode: str, runtime: Path) -> str:
    env = _tool_env(Path("/"), runtime)
    proc = _run([opencode, "--version"], env=env, timeout=60)
    if proc.returncode != 0:
        raise RunRefused(f"opencode --version failed: {proc.stderr.strip()[:300]}")
    version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else ""
    if not version:
        raise RunRefused("opencode --version returned no version")
    return version


def _checkout(source: Path, commit: str, dest: Path, runtime: Path) -> None:
    if dest.exists():
        raise RunRefused(f"disposable checkout path already exists: {dest.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = _git_env(runtime / "git-home")
    proc = _run(["git", "clone", "--quiet", "--shared", "--no-checkout",
                 str(source), str(dest)], env=env, timeout=600)
    if proc.returncode != 0:
        raise RunRefused(f"git clone failed: {proc.stderr.strip()[:400]}")
    proc = _run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit],
                env=env, timeout=600)
    if proc.returncode != 0:
        raise RunRefused(f"git checkout {commit} failed: {proc.stderr.strip()[:400]}")


def _build_snapshot(work: Path, source: Path, runtime: Path, tool_python: str) -> float:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from ozagia.core.memory import build_index\n"
        "from ozagia.core.repo_map import update_tag_cache\n"
        "root = Path(sys.argv[1]); dump = Path(sys.argv[2])\n"
        "build_index(root, dump, include_docs='features', include_git=True, include_scripts=True)\n"
        "update_tag_cache(root, cache_path=root / '.ozagia' / 'tags.json')\n"
        "print(json.dumps({'ok': True, 'dump': str(dump)}))\n"
    )
    started = time.monotonic()
    env = _tool_env(source, runtime, work=work)
    proc = _run([tool_python, "-c", code, str(work),
                 str(work / ".ozagia" / "graph.json")], cwd=work, env=env,
                timeout=SNAPSHOT_TIMEOUT_S)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-800:]
        raise RunRefused(f"snapshot build failed: {detail}")
    return time.monotonic() - started


def _freshness(work: Path, source: Path, runtime: Path, tool_python: str) -> dict[str, Any]:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from ozagia.core.memory import DevMemory\n"
        "root = Path(sys.argv[1])\n"
        "mem = DevMemory(root / '.ozagia' / 'graph.json', root / '.ozagia' / 'journal.jsonl', root=root)\n"
        "print(json.dumps(mem.staleness(), sort_keys=True))\n"
    )
    env = _tool_env(source, runtime, work=work)
    proc = _run([tool_python, "-c", code, str(work)], cwd=work, env=env, timeout=180)
    if proc.returncode != 0:
        raise RunRefused(f"snapshot freshness check failed: {proc.stderr.strip()[:400]}")
    try:
        doc = json.loads(proc.stdout)
    except ValueError as exc:
        raise RunRefused(f"snapshot freshness check returned non-JSON: {proc.stdout[:200]}") from exc
    if doc.get("freshness") != "fresh":
        raise RunRefused(f"snapshot is not fresh before the session: {doc.get('freshness')}")
    return {key: doc.get(key) for key in
            ("freshness", "stale", "snapshot_head", "head", "root_mismatch",
             "tree_changed", "verification_errors")}


def _start_daemon(work: Path, source: Path, runtime: Path, tool_python: str) -> dict[str, Any]:
    sock = work / ".ozagia" / "daemon.sock"
    journal = work / ".ozagia" / "journal.jsonl"
    stderr_path = runtime / "daemon.stderr.txt"
    env = _tool_env(source, runtime, work=work)
    argv = [tool_python, "-m", "ozagia.daemon", "serve",
            "--root", str(work), "--dump", str(work / ".ozagia" / "graph.json"),
            "--journal", str(journal), "--sock", str(sock), "--no-build",
            "--parent-pid", str(os.getpid()), "--idle-timeout", "1800"]
    with stderr_path.open("wb") as stderr:
        started = time.monotonic()
        proc = subprocess.Popen(argv, cwd=str(work), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=stderr, text=True, encoding="utf-8",
                                errors="replace", start_new_session=True)
        line = ""
        try:
            import select
            ready, _w, _x = select.select([proc.stdout], [], [], DAEMON_READY_TIMEOUT_S)
            if ready and proc.stdout is not None:
                line = proc.stdout.readline().strip()
        finally:
            pass
        elapsed = time.monotonic() - started
        if not line.startswith("ready "):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            detail = ""
            try:
                detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                pass
            raise RunRefused(f"daemon did not become ready: {detail.strip() or line or 'no output'}")
        if not sock.exists():
            raise RunRefused("daemon printed ready but its socket is absent")
        return {"proc": proc, "sock": sock, "ready_s": round(elapsed, 3),
                "stderr_path": stderr_path, "argv": argv}


def _stop_daemon(daemon: dict[str, Any] | None) -> dict[str, Any]:
    if not daemon:
        return {"stopped": False}
    proc = daemon["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    return {"stopped": proc.poll() is not None, "returncode": proc.poll()}


def _driver_env(work: Path, xdg: Path, config_path: Path, *, arm: str,
                api_key: str | None = None) -> dict[str, str]:
    for name in ("home", "config", "data", "cache", "state", "tmp"):
        path = xdg / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(xdg / "home"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "TMPDIR": str(xdg / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PWD": str(work),
        "OPENCODE_CONFIG": str(config_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI": "1",
    }
    if api_key is not None:
        env[L.API_KEY_ENV] = api_key
    if arm == "maps":
        env.update({
            "OZAGIA_ROOT": str(work),
            "OZAGIA_STATE_DIR": str(work / ".ozagia"),
            "OZAGIA_DAEMON_SOCK": str(work / ".ozagia" / "daemon.sock"),
        })
    if any("PROXY" in key.upper() for key in env):
        raise RunRefused("proxy variables must not enter the session environment")
    return env


def _scrub(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "<redacted>")
    return text


def _bounded_answer(text: str) -> str:
    return text[:20000]


def _mcp_failure(stderr: str) -> str | None:
    for line in stderr.splitlines():
        low = line.casefold()
        if "mcp" in low and ("failed" in low or "error" in low):
            return line.strip()[:300]
    return None


def _run_session(row: dict[str, Any], work: Path, setup: dict[str, Any], *,
                 source: Path, runtime: Path, api_key: str, opencode: str,
                 tool_python: str, timeout_s: int) -> dict[str, Any]:
    arm = str(row["arm"])
    task_id = str(row["task_id"])
    temp = Path(tempfile.mkdtemp(prefix=f"{row['ordinal']:02d}-{arm}-", dir=runtime))
    try:
        config = L.build_arm_config(
            arm, api_key=api_key, workdir=work,
            tool_python=tool_python, source_src=source / "src")
        config_path = temp / "opencode.json"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
        config_path.chmod(0o600)
        config_digest = L.config_digest(config)
        xdg = temp / "xdg"
        env = _driver_env(work, xdg, config_path, arm=arm, api_key=api_key)
        prompt = str(setup["prompt"])
        argv = [opencode, "run", "--standalone", "--format", "json", "--auto",
                "--archived", "-m", L.MODEL, prompt]
        started_at = _now()
        started = time.monotonic()
        timed_out = False
        proc = subprocess.Popen(argv, cwd=str(work), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate()
        driver_s = time.monotonic() - started
        stdout = _scrub(stdout or "", api_key)
        stderr = _scrub(stderr or "", api_key)
        parsed = L.parse_opencode_events(stdout, secret=api_key)
        score = L.score_answer(parsed["answer"], setup["assertion"])
        stderr_tail = stderr.strip()[-1200:]
        mcp_failure = _mcp_failure(stderr) if arm == "maps" else None
        driver_error = parsed["driver_error"]
        if timed_out:
            status = "timeout"
        elif driver_error or proc.returncode != 0:
            status = "driver_error"
        else:
            status = "completed"
        invalid_reason = None
        if timed_out:
            invalid_reason = "timeout"
        elif driver_error:
            invalid_reason = "driver_error"
        elif proc.returncode != 0:
            invalid_reason = f"opencode exit {proc.returncode}"
        elif mcp_failure:
            invalid_reason = "mcp_server_failed"
        valid = invalid_reason is None
        verified = bool(valid and score["success"])
        setup_s = float(setup["setup_s"])
        inclusive_s = setup_s + driver_s
        errors = list(parsed["errors"])
        if driver_error and driver_error not in errors:
            errors.insert(0, driver_error)
        if stderr_tail and (not valid or mcp_failure):
            errors.append(stderr_tail)
        if mcp_failure:
            errors.append(mcp_failure)
        return {
            "schema": "ozagia.m6_live.run/1",
            "study": L.STUDY,
            "schedule_hash": L.SCHEDULE_SHA256,
            "tasks_sha256": L.TASKS_SHA256,
            "assertions_sha256": L.ASSERTIONS_SHA256,
            "ordinal": row["ordinal"],
            "pair_id": row["pair_id"],
            "pair_order": row["pair_order"],
            "pair_position": row["pair_position"],
            "task_id": task_id,
            "arm": arm,
            "repeat": row["repeat"],
            "started_at_utc": started_at,
            "status": status,
            "valid": valid,
            "invalid_reason": invalid_reason,
            "verified": verified,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "setup_s": round(setup_s, 3),
            "driver_s": round(driver_s, 3),
            "setup_inclusive_s": round(inclusive_s, 3),
            "setup_inclusive_ms": int(round(inclusive_s * 1000)) if verified else None,
            "time_to_verified_ms": int(round(inclusive_s * 1000)) if verified else None,
            "answer": _bounded_answer(parsed["answer"]),
            "answer_chars": len(parsed["answer"]),
            "answer_sha256": L.sha256_text(parsed["answer"]),
            "score": score,
            "tokens": parsed["tokens"],
            "steps": parsed["steps"],
            "tool_calls": parsed["tool_calls"],
            "tools_by_name": parsed["tools_by_name"],
            "mcp_calls": parsed["mcp_calls"],
            "maps_calls": parsed["maps_calls"],
            "capabilities_calls": parsed["capabilities_calls"],
            "permission_denials": parsed["permission_denials"],
            "errors": errors,
            "model": L.MODEL,
            "endpoint": L.ENDPOINT,
            "source_commit": setup["source_commit"],
            "source_tree": setup["source_tree"],
            "config_redacted_sha256": config_digest,
            "arm_config_shape_sha256": L.arm_shape_digest(arm),
            "snapshot": setup.get("snapshot"),
            "daemon": {"ready_s": setup.get("daemon_ready_s")} if arm == "maps" else None,
            "treatment": {
                "mcp_servers": 1 if arm == "maps" else 0,
                "mcp_calls": parsed["mcp_calls"],
                "maps_calls": parsed["maps_calls"],
                "mcp_failure": mcp_failure,
            },
        }
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _setup_error(row: dict[str, Any], setup: dict[str, Any], message: str) -> dict[str, Any]:
    arm = str(row["arm"])
    return {
        "schema": "ozagia.m6_live.run/1",
        "study": L.STUDY,
        "schedule_hash": L.SCHEDULE_SHA256,
        "tasks_sha256": L.TASKS_SHA256,
        "assertions_sha256": L.ASSERTIONS_SHA256,
        "ordinal": row["ordinal"],
        "pair_id": row["pair_id"],
        "pair_order": row["pair_order"],
        "pair_position": row["pair_position"],
        "task_id": row["task_id"],
        "arm": arm,
        "repeat": row["repeat"],
        "started_at_utc": _now(),
        "status": "setup_error",
        "valid": False,
        "invalid_reason": "setup_error",
        "verified": False,
        "exit_code": None,
        "timed_out": False,
        "setup_s": round(float(setup.get("setup_s", 0.0)), 3),
        "driver_s": 0.0,
        "setup_inclusive_s": round(float(setup.get("setup_s", 0.0)), 3),
        "setup_inclusive_ms": None,
        "time_to_verified_ms": None,
        "answer": "",
        "answer_chars": 0,
        "answer_sha256": L.sha256_text(""),
        "score": L.score_answer("", setup["assertion"]),
        "tokens": {"input": None, "output": None, "cache_read": None,
                   "cache_write": None, "cost_usd": None},
        "steps": 0,
        "tool_calls": 0,
        "tools_by_name": {},
        "mcp_calls": 0,
        "maps_calls": 0,
        "capabilities_calls": 0,
        "permission_denials": [],
        "errors": [message[:800]],
        "model": L.MODEL,
        "endpoint": L.ENDPOINT,
        "source_commit": setup.get("source_commit"),
        "source_tree": setup.get("source_tree"),
        "config_redacted_sha256": L.arm_shape_digest(arm),
        "arm_config_shape_sha256": L.arm_shape_digest(arm),
        "snapshot": setup.get("snapshot"),
        "daemon": None,
        "treatment": {"mcp_servers": 1 if arm == "maps" else 0,
                      "mcp_calls": 0, "maps_calls": 0,
                      "mcp_failure": "setup_failed" if arm == "maps" else None},
    }


def _pair_setup(source: Path, tasks: dict[str, Any], assertions: dict[str, Any],
                pair_id: str, pair_order: int, runtime: Path, work_root: Path,
                commit: str, tree: str, tool_python: str) -> dict[str, dict[str, Any]]:
    task = next(row for row in tasks["tasks"] if row["id"] == pair_id)
    assertion = assertions["tasks"][pair_id]
    out: dict[str, dict[str, Any]] = {}
    for arm in L.ARMS:
        work = work_root / f"{pair_order:02d}-{pair_id}-{arm}"
        record: dict[str, Any] = {
            "work": work, "prompt": task["prompt"], "assertion": assertion,
            "source_commit": commit, "source_tree": tree, "setup_s": 0.0,
            "snapshot": None, "daemon_ready_s": None,
        }
        started = time.monotonic()
        try:
            _checkout(source, commit, work, runtime)
            if arm == "maps":
                build_s = _build_snapshot(work, source, runtime, tool_python)
                record["snapshot"] = _freshness(work, source, runtime, tool_python)
                daemon = _start_daemon(work, source, runtime, tool_python)
                record["daemon"] = daemon
                record["daemon_ready_s"] = daemon["ready_s"]
                record["build_s"] = round(build_s, 3)
            else:
                if (work / ".ozagia").exists():
                    raise RunRefused("baseline checkout unexpectedly contains Ozagia state")
                record["snapshot"] = None
            record["setup_s"] = time.monotonic() - started
            record["ok"] = True
        except (RunRefused, OSError, subprocess.SubprocessError) as exc:
            record["setup_s"] = time.monotonic() - started
            record["ok"] = False
            record["error"] = str(exc)
        out[arm] = record
    return out


def _write_report(path: Path, summary: dict[str, Any], records: list[dict[str, Any]]) -> None:
    lines = [
        "# M6 local-Qwen map-arm comparison",
        "",
        "Status: generated by `evaluation/m6_live/run_m6.py`.",
        "This report is a measurement transcript, not a general capability claim.",
        "",
        "## Design",
        "",
        f"- Tasks: {L.TASK_COUNT}; arms: baseline and maps; repeats: {L.REPEATS}.",
        f"- Sessions: {len(records)}; schedule seed: {L.SEED}.",
        f"- Schedule SHA-256: `{L.SCHEDULE_SHA256}`.",
        f"- Model: `{L.MODEL}` at `{L.ENDPOINT}`.",
        "- One session at a time; paired arm order is frozen in `schedule.json`.",
        "",
        "## Primary",
        "",
        f"- Rule: {summary['primary']['rule']}.",
        f"- Baseline verified successes: {summary['primary']['baseline_verified_successes']}.",
        f"- Maps verified successes: {summary['primary']['maps_verified_successes']}.",
        f"- Non-inferior: **{summary['primary']['non_inferior']}**.",
        "",
        "## Secondary",
        "",
        f"- Rule: {summary['secondary']['rule']}.",
        f"- Pairs with both arms verified and timed: {summary['secondary']['pairs_used']}.",
        f"- Baseline median setup-inclusive ms: {summary['secondary']['baseline_median_ms']}.",
        f"- Maps median setup-inclusive ms: {summary['secondary']['maps_median_ms']}.",
        f"- Improvement per mille: {summary['secondary']['improvement_per_mille']}.",
        f"- At least 10% lower: **{summary['secondary']['at_least_10_percent_lower']}**.",
        f"- Insufficient pairs (fewer than 10): {summary['secondary']['insufficient_pairs']}.",
        "",
        "Every pair not included in the secondary calculation is listed in "
        "`summary.json` under `pairs_not_both_verified`; none is silently dropped.",
        "",
        "## Per task",
        "",
        "| task | baseline | maps |",
        "| --- | --- | --- |",
    ]
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for row in records:
        by_task.setdefault(str(row["task_id"]), {})[str(row["arm"])] = row
    for tid in sorted(by_task):
        cells = []
        for arm in L.ARMS:
            row = by_task[tid].get(arm)
            if row is None:
                cells.append("missing")
            else:
                cells.append(
                    f"{row['status']} / score={row['score']['success']} / "
                    f"verified={row['verified']} / setup={row['setup_s']}s / "
                    f"driver={row['driver_s']}s / total={row['setup_inclusive_s']}s")
        lines.append(f"| {tid} | {cells[0]} | {cells[1]} |")
    lines += [
        "",
        "## Aggregates",
        "",
        "| arm | sessions | verified | timeouts | setup errors | setup ms | driver ms | tokens in/out | tool calls | MCP calls |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in L.ARMS:
        row = summary["arms"][arm]
        lines.append(
            f"| {arm} | {row['sessions']} | {row['verified_successes']} | "
            f"{row['timeouts']} | {row['setup_errors']} | "
            f"{row['setup_ms']['total_ms']} | {row['driver_ms']['total_ms']} | "
            f"{row['tokens']['input']}/{row['tokens']['output']} | "
            f"{row['tool_calls']} | {row['mcp_calls']} |")
    lines += [
        "",
        "## Limits",
        "",
        "- This is 20 tasks with one repeat. It does not estimate general model capability.",
        "- The prompted/context arm and the existing-Ozagia 72-session study are separate "
        "evidence and are not pooled with this comparison.",
        "- Failed, timed-out, invalid and unverified pairs remain visible; they are not "
        "replaced or silently dropped.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _dry_run(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    tasks, assertions = L.load_corpus(HERE)
    L.verify_frozen_corpus(HERE)
    _disk_check(out)
    out.mkdir(parents=True, exist_ok=True)
    schedule = L.schedule_bytes(tasks)
    (out / SCHEDULE_JSON).write_bytes(schedule)
    facts = L.validate_facts()
    if not facts["ok"]:
        raise RunRefused("frozen facts do not validate: " + "; ".join(facts["errors"][:5]))
    manifest = {
        "schema": "ozagia.m6_live.manifest/1",
        "study": L.STUDY,
        "mode": "dry-run",
        "created_at_utc": _now(),
        "corpus": {
            "tasks": len(tasks["tasks"]),
            "required_groups": facts["required_groups"],
            "forbidden_phrases": facts["forbidden_phrases"],
            "tasks_sha256": L.file_sha256(L.TASKS_PATH),
            "assertions_sha256": L.file_sha256(L.ASSERTIONS_PATH),
        },
        "schedule": {
            "seed": L.SEED,
            "sessions": L.SESSION_COUNT,
            "sha256": L.SCHEDULE_SHA256,
            "path": SCHEDULE_JSON,
        },
        "model": {"id": L.MODEL, "endpoint": L.ENDPOINT},
        "runner": {
            "run_m6_sha256": L.file_sha256(Path(__file__)),
            "m6_live_lib_sha256": L.file_sha256(HERE / "m6_live_lib.py"),
        },
        "arms": {
            arm: {
                "config_redacted_sha256": L.arm_shape_digest(arm),
                "mcp_servers": 1 if arm == "maps" else 0,
                "mcp_read_tools": list(L.MCP_READ_TOOLS) if arm == "maps" else [],
                "mcp_mutating_tools_denied": list(L.MCP_MUTATING_TOOLS) if arm == "maps" else [],
                "permissions": L.arm_permissions(arm),
            } for arm in L.ARMS
        },
        "gates": {
            "live_flag_required": True,
            "environment": f"{LIVE_ENV}=1",
            "disk_floor_bytes": DISK_FLOOR_BYTES,
            "source_must_be_clean": True,
        },
        "source": "not inspected in dry-run (no git subprocess started)",
        "process_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "note": ("Dry run only. No model, network, MCP, daemon, checkout or git "
                 "process was started."),
    }
    _write_json(out / MANIFEST_JSON, manifest)
    print(json.dumps({
        "mode": "dry-run",
        "tasks": len(tasks["tasks"]),
        "sessions": L.SESSION_COUNT,
        "schedule_sha256": L.SCHEDULE_SHA256,
        "tasks_sha256": L.TASKS_SHA256,
        "assertions_sha256": L.ASSERTIONS_SHA256,
        "required_groups": facts["required_groups"],
        "forbidden_phrases": facts["forbidden_phrases"],
        "manifest": str(out / MANIFEST_JSON),
        "schedule": str(out / SCHEDULE_JSON),
        "process_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
    }, indent=2, sort_keys=True))
    return 0


def _live_run(args: argparse.Namespace) -> int:
    if os.environ.get(LIVE_ENV) != "1":
        _log(f"refused: live mode needs both --live and {LIVE_ENV}=1")
        return 2
    source = args.source.resolve()
    out = args.out.resolve()
    if out.is_relative_to(source):
        _log("refused: --out must be outside the source checkout")
        return 2
    if not (source / ".git").exists():
        _log(f"refused: --source is not a Git checkout: {source}")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    _disk_check(out)
    tasks, _assertions = L.load_corpus(HERE)
    L.verify_frozen_corpus(HERE)
    runtime = Path(tempfile.mkdtemp(prefix="ozagia-m6-live-runtime-",
                                    dir=tempfile.gettempdir()))
    runtime.chmod(0o700)
    runs_path = out / RUNS_JSONL
    if runs_path.exists():
        _log(f"refused: {runs_path} already exists; use a new --out directory")
        shutil.rmtree(runtime, ignore_errors=True)
        return 2
    try:
        pin = _source_pin(source, args.base, runtime)
        _write_json(out / SOURCE_MANIFEST, pin["manifest"])
        facts = L.validate_facts()
        if not facts["ok"]:
            raise RunRefused(
                "frozen facts do not validate: " + "; ".join(facts["errors"][:5]))
        tool = _check_tool_python(args.tool_python, source, runtime)
        mcp = _mcp_surface(args.tool_python, source, runtime)
        opencode = shutil.which("opencode")
        if not opencode:
            raise RunRefused("opencode is not on PATH")
        version = _opencode_version(opencode, runtime)
        api_key = _load_api_key(args.api_key_file)
        health = _health_check(api_key)
        manifest = {
            "schema": "ozagia.m6_live.manifest/1",
            "study": L.STUDY,
            "mode": "live",
            "created_at_utc": _now(),
            "source": {"repo": pin["repo"], "commit": pin["commit"], "tree": pin["tree"],
                       "clean": pin["clean"], "files": pin["files"],
                       "manifest_sha256": pin["manifest_sha256"],
                       "manifest_path": SOURCE_MANIFEST},
            "corpus": {
                "tasks": len(tasks["tasks"]),
                "tasks_sha256": L.TASKS_SHA256,
                "assertions_sha256": L.ASSERTIONS_SHA256,
                "required_groups": facts["required_groups"],
                "forbidden_phrases": facts["forbidden_phrases"],
            },
            "schedule": {"seed": L.SEED, "sessions": L.SESSION_COUNT,
                         "sha256": L.SCHEDULE_SHA256, "path": SCHEDULE_JSON},
            "model": health | {"id": L.MODEL},
            "opencode": {"path": str(Path(opencode).resolve()), "version": version},
            "tool_python": tool,
            "runner": {
                "run_m6_sha256": L.file_sha256(Path(__file__)),
                "m6_live_lib_sha256": L.file_sha256(HERE / "m6_live_lib.py"),
            },
            "mcp": mcp,
            "arms": {
                arm: {
                    "config_redacted_sha256": L.arm_shape_digest(arm),
                    "mcp_servers": 1 if arm == "maps" else 0,
                    "permissions": L.arm_permissions(arm),
                } for arm in L.ARMS
            },
            "gates": {"live_flag": True, "environment": f"{LIVE_ENV}=1",
                      "disk_floor_bytes": DISK_FLOOR_BYTES,
                      "source_clean": True},
            "environment": {
                "allowlist": sorted(_driver_env(Path("/"), runtime / "probe",
                                                runtime / "probe.json",
                                                arm="baseline", api_key="<redacted>")),
                "no_proxy_variables": True,
                "no_global_config": True,
                "one_session_at_a_time": True,
            },
            "schedule_path": str(out / SCHEDULE_JSON),
        }
        (out / SCHEDULE_JSON).write_bytes(L.schedule_bytes(tasks))
        _write_json(out / MANIFEST_JSON, manifest)
        _log(f"preflight ok: {pin['commit']} model={L.MODEL} endpoint={L.ENDPOINT}")
        records: list[dict[str, Any]] = []
        with runs_path.open("x", encoding="utf-8") as runs_handle:
            runs_path.chmod(0o600)
            current_pair: str | None = None
            pair_setup: dict[str, dict[str, Any]] = {}
            try:
                for row in L.schedule_document(tasks)["schedule"]:
                    tid = str(row["task_id"])
                    if tid != current_pair:
                        if current_pair is not None:
                            for arm in L.ARMS:
                                daemon = pair_setup.get(arm, {}).get("daemon")
                                if daemon:
                                    _stop_daemon(daemon)
                            if not args.keep:
                                for arm in L.ARMS:
                                    shutil.rmtree(pair_setup.get(arm, {}).get("work", Path("/nonexistent")),
                                                  ignore_errors=True)
                        current_pair = tid
                        pair_setup = _pair_setup(
                            source, tasks, _assertions, tid, int(row["pair_order"]), runtime,
                            out / "work", pin["commit"], pin["tree"], args.tool_python)
                    setup = pair_setup[str(row["arm"])]
                    if not setup.get("ok"):
                        record = _setup_error(row, setup, str(setup.get("error", "setup failed")))
                    elif setup.get("ok") is None:
                        record = _setup_error(row, setup, "setup did not complete")
                    else:
                        try:
                            record = _run_session(
                                row, setup["work"], setup,
                                source=source, runtime=runtime, api_key=api_key,
                                opencode=opencode, tool_python=args.tool_python,
                                timeout_s=args.timeout_s)
                        except (OSError, subprocess.SubprocessError, ValueError) as exc:
                            record = _setup_error(
                                row, setup, f"driver setup failed: {exc}")
                    records.append(record)
                    runs_handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
                    runs_handle.flush()
                    _log(f"[{record['ordinal']:02d}/{L.SESSION_COUNT}] {record['task_id']} "
                         f"{record['arm']} status={record['status']} "
                         f"verified={record['verified']} score={record['score']['success']}")
            finally:
                for arm in L.ARMS:
                    daemon = pair_setup.get(arm, {}).get("daemon")
                    if daemon:
                        _stop_daemon(daemon)
                if not args.keep:
                    for arm in L.ARMS:
                        shutil.rmtree(pair_setup.get(arm, {}).get("work", Path("/nonexistent")),
                                      ignore_errors=True)
        summary = L.summarise_runs(records)
        summary["runs_jsonl_sha256"] = L.file_sha256(runs_path)
        _write_json(out / SUMMARY_JSON, summary)
        _write_report(out / REPORT_MD, summary, records)
        manifest["finished_at_utc"] = _now()
        manifest["runs"] = len(records)
        manifest["runs_jsonl_sha256"] = summary["runs_jsonl_sha256"]
        manifest["summary_sha256"] = L.file_sha256(out / SUMMARY_JSON)
        _write_json(out / MANIFEST_JSON, manifest)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except RunRefused as exc:
        _log(f"refused: {exc}")
        return 2
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _log(f"failed: {exc}")
        return 1
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="write schedule/manifest only (the default)")
    mode.add_argument("--live", action="store_true",
                      help="run live local-model sessions (needs OZAGIA_EVAL_LIVE=1)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2],
                        help="clean Git checkout at the pinned commit")
    parser.add_argument("--base", default="HEAD", help="commit to pin (must equal HEAD)")
    parser.add_argument("--tool-python", default=sys.executable,
                        help="interpreter that can import the pinned worktree's ozagia")
    parser.add_argument("--api-key-file", type=Path,
                        default=Path(os.environ.get("M6_API_KEY_FILE", DEFAULT_API_KEY_FILE)),
                        help="local vLLM API key file (never copied to artifacts)")
    parser.add_argument("--timeout-s", type=int, default=300,
                        help="per-session driver timeout in seconds")
    parser.add_argument("--keep", action="store_true",
                        help="keep disposable checkouts after a live run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.live and args.dry_run:
        print("refused: choose one of --live or --dry-run", file=sys.stderr)
        return 2
    if args.live:
        return _live_run(args)
    return _dry_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
