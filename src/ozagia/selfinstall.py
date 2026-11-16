"""`ozagia self ...`: the installed ozagia (see the `install` script).

    ozagia self info [--json]          version, target, runtime, OpenCode
    ozagia self check [--tui] [--json] verify the install (exit 1 on a failure)
    ozagia self upgrade [--version V]  re-run the installer
    ozagia self uninstall [--yes]      remove $OZAGIA_HOME and the PATH line

An installed ozagia runs from `$OZAGIA_HOME/current` (default `~/.ozagia`):
its own Python under `python/`, with `manifest.json` next to it naming the
version, target and pinned OpenCode release. The launcher sets
`OZAGIA_HOME` and `OZAGIA_OPENCODE` (the pinned OpenCode binary). A pip or
source install has no manifest: `info` says so, `check` checks the runtime
and the OpenCode on PATH, and `upgrade`/`uninstall` refuse.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

INSTALL_URL = "https://ozagia.com/install"
# The same script, straight from the repository.
INSTALL_URL_GITHUB = "https://raw.githubusercontent.com/ozmalabs/ozagia/main/install"
INSTALL_URL_ENV = "OZAGIA_INSTALL_URL"
OPENCODE_ENV = "OZAGIA_OPENCODE"
# Seconds `check --tui` waits for the plugin's setup hello.
TUI_HELLO_TIMEOUT_S = 45.0


def home() -> Path:
    return Path(os.environ.get("OZAGIA_HOME") or Path.home() / ".ozagia")


def bundle_dir() -> Path | None:
    """The bundle this interpreter belongs to (`<bundle>/python/bin/python3`),
    or None for a pip or source install."""
    root = Path(sys.prefix).resolve().parent
    return root if (root / "manifest.json").is_file() else None


def manifest() -> dict[str, Any] | None:
    b = bundle_dir()
    if b is None:
        return None
    try:
        return json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def opencode_path() -> str | None:
    """The OpenCode binary `ozagia` runs: $OZAGIA_OPENCODE, else `opencode`
    on PATH."""
    exe = os.environ.get(OPENCODE_ENV) or "opencode"
    return shutil.which(exe)


def _version() -> str:
    from ozagia.cli import _version as v
    return v()


def info() -> dict[str, Any]:
    m = manifest()
    oc = opencode_path()
    out: dict[str, Any] = {
        "version": _version(),
        "installed": m is not None,
        "home": str(home()) if m is not None else None,
        "bundle": str(bundle_dir()) if m is not None else None,
        "target": (m or {}).get("target"),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "opencode": oc,
        "opencode_pinned": ((m or {}).get("opencode") or {}).get("version"),
    }
    return out


def _opencode_version(exe: str) -> str | None:
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (p.stdout or p.stderr).strip()
    return text.split()[-1].lstrip("v") if text else None


def tui_hello(opencode: str, timeout_s: float = TUI_HELLO_TIMEOUT_S) -> dict[str, Any]:
    """Start `opencode --standalone` (the TUI) in a scratch git repository
    with the ozagia plugin installed and a stub daemon socket, under a pty,
    and wait for the plugin's setup `hello`. Makes no model call."""
    from ozagia.integrations.opencode import install as oc
    got: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ozagia-tui-") as tmp:
        work = Path(tmp) / "w"
        work.mkdir()
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        oc.install(work)
        sock = Path(tempfile.mkdtemp(prefix="ozs-")) / "d.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock))
        srv.listen(8)

        def serve() -> None:
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn, conn.makefile("rwb") as fh:
                    for line in fh:
                        try:
                            req = json.loads(line)
                        except ValueError:
                            break
                        got.append(req)
                        fh.write((json.dumps({"id": req.get("id"), "ok": True})
                                  + "\n").encode())
                        fh.flush()

        threading.Thread(target=serve, daemon=True).start()
        env = dict(os.environ, OZAGIA_DAEMON_SOCK=str(sock), PWD=str(work))
        cmd = f"{_sh(opencode)} --standalone {_sh(str(work))}"
        if sys.platform == "darwin":
            argv = ["script", "-q", "/dev/null", "/bin/sh", "-c", cmd]
        else:
            argv = ["script", "-qfec", cmd, "/dev/null"]
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(argv, cwd=str(work), env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as e:
            srv.close()
            return {"ok": False, "error": f"cannot start the TUI under `script`: {e}"}
        try:
            while time.monotonic() - t0 < timeout_s and proc.poll() is None:
                if any(r.get("op") == "hello" for r in got):
                    break
                time.sleep(0.25)
        finally:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            srv.close()
            shutil.rmtree(sock.parent, ignore_errors=True)
        hello = next((r for r in got if r.get("op") == "hello"), None)
        if hello is None:
            return {"ok": False, "seconds": round(time.monotonic() - t0, 1),
                    "error": "no hello from the plugin"}
        return {"ok": True, "seconds": round(time.monotonic() - t0, 1),
                "permission_assert": hello.get("permission_assert")}


def _sh(text: str) -> str:
    import shlex
    return shlex.quote(text)


def check(*, tui: bool = False) -> dict[str, Any]:
    """Each check as {"name", "ok", "detail"}; `ok` overall."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    m = manifest()
    add("runtime", sys.version_info >= (3, 12),
        f"Python {sys.version.split()[0]} at {sys.executable}")
    if m is not None:
        add("bundle", m.get("version") == _version(),
            f"manifest {m.get('version')} ({m.get('target')}), ozagia {_version()}")
    missing = []
    for mod in ("tree_sitter", "tree_sitter_python", "tree_sitter_rust",
                "tree_sitter_typescript"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    add("parsers", not missing or m is None,
        "tree-sitter parsers importable" if not missing
        else f"missing: {', '.join(missing)}")
    oc = opencode_path()
    if oc is None:
        add("opencode", False, f"no OpenCode: set {OPENCODE_ENV} or put `opencode` on PATH")
    else:
        got = _opencode_version(oc)
        pinned = ((m or {}).get("opencode") or {}).get("version")
        ok = got is not None and (pinned is None or got == pinned)
        detail = f"{oc} reports {got or '?'}"
        if pinned:
            detail += f" (pinned {pinned})"
        add("opencode", ok, detail)
    try:
        from ozagia.integrations.opencode.install import plugin_source
        add("plugin", "PLUGIN_ID" in plugin_source(), "plugin source packaged")
    except Exception as e:  # noqa: BLE001 - reported
        add("plugin", False, f"{type(e).__name__}: {e}")
    if tui:
        if oc is None:
            add("tui", False, "no OpenCode to start")
        elif shutil.which("script") is None:
            add("tui", False, "`script` (util-linux / BSD) is needed for the pty")
        else:
            res = tui_hello(oc)
            add("tui", res["ok"], (f"plugin loaded in the TUI in {res['seconds']} s"
                                   if res["ok"] else res.get("error", "failed")))
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def _installer(version: str | None) -> list[str]:
    url = os.environ.get(INSTALL_URL_ENV) or INSTALL_URL
    args = ["--version", version] if version else []
    if url.startswith(("http://", "https://", "file://")):
        script = f"curl -fsSL {_sh(url)} | bash -s -- {' '.join(_sh(a) for a in args)}"
    else:
        script = f"bash {_sh(url)} {' '.join(_sh(a) for a in args)}"
    return ["bash", "-c", script]


def upgrade(version: str | None) -> int:
    if manifest() is None:
        print("error: not an installed ozagia (no manifest); upgrade with the tool "
              "you installed it with", file=sys.stderr)
        return 2
    return subprocess.call(_installer(version))


def _rc_files() -> list[Path]:
    h = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or h / ".config")
    zd = Path(os.environ.get("ZDOTDIR") or h)
    return [h / ".bashrc", h / ".bash_profile", h / ".profile", zd / ".zshrc",
            zd / ".zshenv", xdg / "fish" / "config.fish"]


def _path_in(line: str) -> str | None:
    """The directory an installer PATH line adds (`export PATH=<dir>:$PATH`
    or `fish_add_path <dir>`), or None."""
    line = line.strip()
    if line.startswith("export PATH=") and line.endswith(":$PATH"):
        return line[len("export PATH="):-len(":$PATH")]
    if line.startswith("fish_add_path "):
        return line[len("fish_add_path "):].strip()
    return None


def _same_dir(a: str, b: str) -> bool:
    return os.path.realpath(os.path.normpath(a)) == os.path.realpath(os.path.normpath(b))


def _strip_path_lines(path: Path, bin_dir: str) -> bool:
    """Remove the installer's `# ozagia` + PATH line pair for `bin_dir` from
    `path` (paths compared resolved: `//`, symlinks such as macOS's /var)."""
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return False
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        added = _path_in(lines[i + 1]) if i + 1 < len(lines) else None
        if lines[i] == "# ozagia" and added and _same_dir(added, bin_dir):
            if out and out[-1] == "":
                out.pop()
            i += 2
            changed = True
            continue
        out.append(lines[i])
        i += 1
    if changed:
        path.write_text("\n".join(out), encoding="utf-8")
    return changed


def uninstall(*, yes: bool) -> int:
    if manifest() is None:
        print("error: not an installed ozagia (no manifest)", file=sys.stderr)
        return 2
    h = home()
    if not yes:
        print(f"This removes {h} (ozagia, its Python and its OpenCode) and the "
              "PATH line the installer added. Run again with --yes to do it.")
        return 1
    bin_dir = str(h / "bin")
    for rc in _rc_files():
        if rc.is_file() and _strip_path_lines(rc, bin_dir):
            print(f"removed the PATH line from {rc}")
    shutil.rmtree(h, ignore_errors=True)
    print(f"removed {h}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ozagia self",
                                 description="The installed ozagia.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("info", help="version, target, runtime, OpenCode")
    i.add_argument("--json", action="store_true")
    c = sub.add_parser("check", help="verify the install")
    c.add_argument("--tui", action="store_true",
                   help="also start the TUI and wait for the plugin (no model call)")
    c.add_argument("--json", action="store_true")
    u = sub.add_parser("upgrade", help="install the latest (or --version) release")
    u.add_argument("--version", default=None)
    x = sub.add_parser("uninstall", help="remove the installed ozagia")
    x.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "info":
        res = info()
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            for k, v in res.items():
                print(f"{k}: {'' if v is None else v}")
        return 0
    if args.cmd == "check":
        res = check(tui=args.tui)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            for c_ in res["checks"]:
                print(f"{'ok  ' if c_['ok'] else 'FAIL'} {c_['name']}: {c_['detail']}")
        return 0 if res["ok"] else 1
    if args.cmd == "upgrade":
        return upgrade(args.version)
    return uninstall(yes=args.yes)


__all__ = ["check", "info", "main", "manifest", "opencode_path", "tui_hello",
           "uninstall", "upgrade"]
