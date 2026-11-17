"""A project's Python environments and what each provides (W20).

Finds the interpreters a project may run under, finds out what each has, and
answers which of them can import a set of files (`env_for`) or one module
(`who_has`). Imports are read from the source with `ast`; no project module
is imported.

What is executed. An interpreter is probed by running it: `python -I -c
<probe>` from a temporary directory with a minimal environment (`PATH`,
`HOME`, `LANG`, `LC_*`, `TMPDIR`). That runs the binary itself and its
`site` initialization, which processes the `.pth` files and
`sitecustomize` of its site-packages, so probing a virtual environment runs
whatever that environment ships. Interpreters the project ships are
therefore not run unless the user trusts them (`trust_project_python`):

- project-shipped: the candidate path, its directory or its venv directory
  (the one holding `pyvenv.cfg`) lies inside the root or the source
  repository, or the binary resolves there. This covers the root's and the
  source repository's venvs, in-root paths named in the command docs and a
  lock tool's answer pointing inside. Judged by the candidate path, not only
  by where the binary resolves: a committed `.venv` whose `bin/python`
  links to a system interpreter still runs the committed `.pth` files;
- trusted: `OZAGIA_TRUST_PROJECT_PYTHON=1` in this process's environment,
  or `[environments] trust_project_python = true` in the user-level
  `$XDG_CONFIG_HOME/ozagia/config.toml` (default `~/.config/ozagia/`; one
  inside the root does not count, and a repository's `.ozagia.toml` cannot
  set it). The eval harness passes trust explicitly: it runs the
  repository's tests anyway.

An untrusted project-shipped venv gets a static probe instead (`probe:
"static"`): `pyvenv.cfg` (version, `include-system-site-packages`,
`home`), the entries of `lib/python*/site-packages`, and each
`*.dist-info`'s `METADATA` name, `top_level.txt` and `RECORD`, read as
text; `.pth` path lines are listed, `.pth` import lines are not run. It
cannot see what `sitecustomize`, `.pth` import lines or editable-install
finders add, nor the base interpreter's standard library (this
interpreter's list stands in). A project-shipped interpreter that is not a
venv is not probed at all. The interpreter ozagia runs under is probed even
when it is inside the root: its `site` has already run in this process.
Other interpreters (`$VIRTUAL_ENV` outside the root, `PATH`, a lock tool's
environment elsewhere) are run.

Candidates, in order of evidence (`discover`):

1. `$VIRTUAL_ENV`;
2. `.venv`, `venv` and `.venv-*` in the root;
3. the same in the source repository's root, when the root is a git
   worktree or a local clone without its own;
4. the environment of the lock file's tool, read from the filesystem
   (`uv.lock`: `.venv` or `$UV_PROJECT_ENVIRONMENT`; `pdm.lock`: `.venv`,
   else the interpreter `.pdm-python` names, read as text). The tools
   themselves (`poetry env info --path`, `pdm info --python`) run only
   when the project is trusted, the cache mode is not `read` and the tool
   is installed: pdm runs the interpreter a committed `.pdm-python` names
   and may create `.venv` and `.pdm-python` in the repository;
5. interpreters named in the project's command docs (`index.command_docs`);
6. `python` and `python3` on `PATH`;
7. the interpreter ozagia runs under.

Probes are bounded (`PROBE_TIMEOUT_S`, `PROBE_MAX_BYTES`) and cached in
`<state dir>/environments/probes.json`, keyed on the interpreter path (and
the probe kind) and validated against the mtimes of the interpreter, of its
venv's `pyvenv.cfg` and of every directory the probe scanned (installing or
removing a distribution changes its `site-packages` mtime). Within one
process probes are also memoized, failed ones included.

Import kinds (`file_imports`): `module` (runs when the file is imported),
`lazy` (inside a function or an `if __name__ == "__main__"` block),
`conditional` (under an `if` that tests `sys.version_info`, `sys.platform`,
`os.name` or `platform`) and `guarded` (inside `try` with an `ImportError`
handler, under `if TYPE_CHECKING`, after `pytest.importorskip` of the same
module in the same function, or anywhere in a file whose module level calls
it). For the files asked about, `module` and `lazy` imports are required.
Their first-party imports are followed (the transitive closure, plus parent
packages and, for tests, the `conftest.py` files pytest loads); there only
`module` imports are required. `guarded` imports and the closure's `lazy`
ones are optional: they decide the order of interpreters that have every
required import, not whether one qualifies. A `conditional` import is
optional too, except that a simple version test (`sys.version_info >= (3,
11)`) or platform test (`sys.platform == "win32"`) is evaluated per
interpreter: required where its branch is taken (and the import would be
required without the test), ignored where it is not.

First-party names: a module under the importing file's base directory or an
import root; else a module or package of that name anywhere in the tree,
unless a probed interpreter has an installed distribution providing that
name (then it is third-party: `domains/chess.py` does not hide
python-chess).

Parsed imports are cached by git blob id in `<state dir>/environments/
imports.json` for files whose working-tree content is the index's.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.core import manifests as MF

SCHEMA = "ozagia.environments.v1"
CACHE_SCHEMA = 2
CACHE_DIR = "environments"
CACHE_NAME = "probes.json"
IMPORTS_CACHE_NAME = "imports.json"
# Bump when `file_imports` output changes.
IMPORTS_SCHEMA = 2
CACHE_MODES = ("rw", "read", "off")
TRUST_ENV = "OZAGIA_TRUST_PROJECT_PYTHON"
# The variables a probe keeps; everything else is dropped.
PROBE_ENV_KEYS = ("PATH", "HOME", "LANG", "TMPDIR")
PROBE_ENV_PREFIXES = ("LC_",)
STATIC_SEES = (
    "pyvenv.cfg and site-packages were read as text, so what sitecustomize, "
    ".pth import lines and editable-install finders add is not seen, and this "
    "interpreter's standard library stands in for its own")
STATIC_LIMIT = ("static probe: the interpreter was not run (project-shipped, "
                "not trusted); " + STATIC_SEES)
MAX_STATIC_ENTRIES = 20_000
MAX_RECORD_BYTES = 2_000_000
MAX_PTH_DIRS = 50

PROBE_TIMEOUT_S = 10.0
PROBE_MAX_BYTES = 8 * 1024 * 1024
TOOL_TIMEOUT_S = 10.0
GIT_TIMEOUT_S = 10.0
MAX_READ_BYTES = 1_000_000
MAX_DOC_BYTES = 500_000
MAX_CLOSURE_FILES = 5000
MAX_WALK_FILES = 200_000
MAX_LISTED_FILES = 10

VENV_DIRS = (".venv", "venv")
VENV_GLOB_PREFIX = ".venv-"
STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | frozenset(
    sys.builtin_module_names) | {"__future__", "__main__"}

# Import names whose distribution name differs from them (beyond case and
# `-`/`_`/`.`), for when no probed interpreter has the distribution.
IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "yaml": ("pyyaml",), "PIL": ("pillow",), "sklearn": ("scikit-learn",),
    "cv2": ("opencv-python", "opencv-python-headless", "opencv-contrib-python"),
    "bs4": ("beautifulsoup4",), "dateutil": ("python-dateutil",),
    "attr": ("attrs",), "dotenv": ("python-dotenv",), "jwt": ("pyjwt",),
    "serial": ("pyserial",), "Crypto": ("pycryptodome",),
    "OpenSSL": ("pyopenssl",), "magic": ("python-magic",),
    "gi": ("pygobject",), "zmq": ("pyzmq",), "docx": ("python-docx",),
    "pkg_resources": ("setuptools",), "_pytest": ("pytest",),
    "google": ("protobuf",), "skimage": ("scikit-image",),
    "Bio": ("biopython",), "usb": ("pyusb",), "win32api": ("pywin32",),
}

KIND_RANK = {"module": 0, "lazy": 1, "conditional": 2, "guarded": 3}
_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError", "Exception",
                            "BaseException"})


class EnvError(RuntimeError):
    """A bad argument (a path outside the root, an unknown cache mode)."""


# --------------------------------------------------------------------------- #
# the probe
# --------------------------------------------------------------------------- #

# Runs under the candidate interpreter with -I (no PYTHON* variables, no
# user site, no cwd on sys.path) from a temporary directory. Python 3.8+.
PROBE_SCRIPT = r'''
import json, os, sys, sysconfig
MAX = 20000
def tops(dist):
    out = set()
    try:
        text = dist.read_text("top_level.txt")
    except Exception:
        text = None
    if text:
        out.update(x.strip().replace("/", ".").split(".")[0]
                   for x in text.splitlines() if x.strip())
        return out
    try:
        files = dist.files or []
    except Exception:
        files = []
    for f in files[:MAX]:
        parts = getattr(f, "parts", str(f).split("/"))
        if not parts:
            continue
        first = parts[0]
        if len(parts) == 1:
            if first.endswith(".py"):
                first = first[:-3]
            elif first.endswith((".so", ".pyd")):
                first = first.split(".")[0]
            else:
                continue
        elif first.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if first.isidentifier() and not first.startswith("__editable__"):
            out.add(first)
    return out
std = set()
for key in ("stdlib", "platstdlib"):
    try:
        std.add(os.path.realpath(sysconfig.get_paths()[key]))
    except Exception:
        pass
def stdlib_dir(p):
    rp = os.path.realpath(p)
    return any(rp == s or rp.startswith(s + os.sep) for s in std)
base = getattr(sys, "base_prefix", sys.prefix)
paths = [p for p in sys.path if p and os.path.isdir(p)]
if sys.prefix == base:
    try:
        import site
        user = site.getusersitepackages()
        if isinstance(user, str) and os.path.isdir(user) and user not in paths:
            paths.append(user)
    except Exception:
        pass
scan = [p for p in paths if not stdlib_dir(p)
        or "site-packages" in p or "dist-packages" in p]
mods = {}
for d in scan:
    try:
        names = sorted(os.listdir(d))[:MAX]
    except OSError:
        continue
    for n in names:
        if n.endswith((".dist-info", ".egg-info", ".pth", ".egg-link")):
            continue
        if n.endswith(".py"):
            n = n[:-3]
        elif n.endswith((".so", ".pyd")):
            n = n.split(".")[0]
        elif not os.path.isdir(os.path.join(d, n)):
            continue
        if n.isidentifier() and n != "__pycache__":
            mods.setdefault(n, [])
dists = 0
try:
    from importlib import metadata
    for dist in metadata.distributions(path=paths):
        dists += 1
        if dists > MAX:
            break
        try:
            name = dist.metadata["Name"] or ""
        except Exception:
            name = ""
        for t in tops(dist):
            lst = mods.setdefault(t, [])
            if name and name not in lst:
                lst.append(name)
except Exception as exc:
    mods.setdefault("__metadata_error__", [repr(exc)[:200]])
print(json.dumps({
    "version": "%d.%d.%d" % tuple(sys.version_info[:3]),
    "executable": sys.executable, "prefix": sys.prefix, "base_prefix": base,
    "stdlib": sorted(getattr(sys, "stdlib_module_names", ())),
    "scanned": scan, "distributions": dists, "modules": mods}))
'''


def _probe_env() -> dict[str, str]:
    """The probe's environment: a minimal allowlist, so no token, `OZAGIA_*`
    or `PYTHON*` variable reaches the interpreter."""
    return {k: v for k, v in os.environ.items()
            if k in PROBE_ENV_KEYS or k.startswith(PROBE_ENV_PREFIXES)}


def _tail(data: bytes, limit: int = 400) -> str:
    return data.decode("utf-8", "replace").strip()[-limit:]


def run_probe(python: str, *, timeout: float = PROBE_TIMEOUT_S,
              max_bytes: int = PROBE_MAX_BYTES) -> dict[str, Any]:
    """Probe one interpreter (no cache). Returns the raw probe dict with
    ``ok``; on failure ``ok`` is false and ``error`` says why. Output is
    spooled to temporary files and refused past ``max_bytes``."""
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.run(
                [python, "-I", "-c", PROBE_SCRIPT], stdout=out, stderr=err,
                stdin=subprocess.DEVNULL, timeout=timeout,
                cwd=tempfile.gettempdir(), env=_probe_env(), check=False)
            size = out.seek(0, os.SEEK_END)
            if size > max_bytes:
                return {"ok": False,
                        "error": f"probe output over {max_bytes} bytes"}
            out.seek(0)
            data = out.read(max_bytes + 1)
            err.seek(0)
            errors = err.read(64 * 1024)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"probe timed out after {timeout:g} s"}
    except OSError as exc:
        return {"ok": False, "error": f"cannot run: {exc.strerror or exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": f"probe exited {proc.returncode}: "
                                      f"{_tail(errors) or _tail(data)}"}
    try:
        doc = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return {"ok": False, "error": "probe printed no JSON: " + _tail(data)}
    if not isinstance(doc, dict) or not isinstance(doc.get("modules"), dict):
        return {"ok": False, "error": "probe printed an unexpected document"}
    return {"ok": True, **doc}


def _stat_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def venv_dir(python: str | Path) -> Path | None:
    """The virtual environment a candidate interpreter belongs to: the
    directory above its `bin/` (or `Scripts/`) when it holds `pyvenv.cfg`.
    Judged by the candidate path, not where the binary resolves."""
    path = Path(os.path.abspath(python))
    if path.parent.name not in ("bin", "Scripts"):
        return None
    venv = path.parent.parent
    try:
        return venv if (venv / "pyvenv.cfg").is_file() else None
    except OSError:
        return None


def read_pyvenv_cfg(venv: Path) -> dict[str, str]:
    """`pyvenv.cfg` as {lowercase key: value} (bounded read)."""
    try:
        with open(venv / "pyvenv.cfg", "rb") as fh:
            text = fh.read(64 * 1024).decode("utf-8", "replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip().lower()] = value.strip()
    return out


def _cfg_path(python: str) -> str | None:
    venv = venv_dir(python)
    return str(venv / "pyvenv.cfg") if venv is not None else None


def _signature(python: str, dirs: Iterable[str]) -> dict[str, Any]:
    real = os.path.realpath(python)
    cfg = _cfg_path(python)
    return {"real": real, "mtime_ns": _stat_ns(real),
            "cfg": _stat_ns(cfg) if cfg else None,
            "dirs": {d: _stat_ns(d) for d in dirs}}


def _valid(entry: Mapping[str, Any], python: str) -> bool:
    sig = entry.get("signature")
    if not isinstance(sig, dict) or not isinstance(sig.get("dirs"), dict):
        return False
    return sig == _signature(python, sig["dirs"])


def interpreter_signature(python: str, dirs: Iterable[str] | None = None
                          ) -> dict[str, Any]:
    """What a probe of ``python`` depends on: {"real": its realpath,
    "mtime_ns": the binary's mtime, "cfg": its venv's `pyvenv.cfg` mtime,
    "dirs": {scanned directory: mtime}} (installing or removing a
    distribution changes its site-packages mtime). Without ``dirs`` the
    interpreter is probed (run, or read from the probe memo) for them."""
    if dirs is None:
        got = probe(python, cache=ProbeCache(None, "off"))
        dirs = got.get("scanned") or [] if got.get("ok") else []
    return _signature(os.path.abspath(python), dirs)


_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def _version_text(text: str) -> str:
    match = _VERSION.match(text.strip())
    if not match:
        return ""
    return ".".join(g for g in match.groups() if g is not None)


def _first_component(path: str) -> str | None:
    """The top-level import name a RECORD path installs, or None."""
    parts = path.replace("\\", "/").split("/")
    first = parts[0]
    if len(parts) == 1:
        if first.endswith(".py"):
            first = first[:-3]
        elif first.endswith((".so", ".pyd")):
            first = first.split(".")[0]
        else:
            return None
    elif first.endswith((".dist-info", ".egg-info", ".data")) or first == "..":
        return None
    if first.isidentifier() and not first.startswith("__editable__") \
            and first != "__pycache__":
        return first
    return None


def _read_bounded(path: Path, limit: int) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _dist_tops(info: Path) -> tuple[str, set[str]]:
    """(distribution name, top-level names) of a `*.dist-info` or
    `*.egg-info` directory, read as text."""
    name = ""
    meta = _read_bounded(info / ("METADATA" if info.suffix == ".dist-info"
                                 else "PKG-INFO"), 64 * 1024)
    for line in (meta or "").splitlines():
        if line.lower().startswith("name:"):
            name = line.split(":", 1)[1].strip()
            break
        if not line.strip():
            break
    if not name:
        name = info.name.rsplit(".", 1)[0].split("-", 1)[0]
    tops: set[str] = set()
    top_level = _read_bounded(info / "top_level.txt", 64 * 1024)
    if top_level:
        tops.update(x.strip().replace("/", ".").split(".")[0]
                    for x in top_level.splitlines() if x.strip())
        return name, {t for t in tops if t.isidentifier()}
    record = _read_bounded(info / ("RECORD" if info.suffix == ".dist-info"
                                   else "installed-files.txt"), MAX_RECORD_BYTES)
    for line in (record or "").splitlines()[:MAX_STATIC_ENTRIES]:
        path = line.split(",", 1)[0].strip().strip('"')
        top = _first_component(path) if path else None
        if top:
            tops.add(top)
    return name, tops


def _list_entries(directory: Path, mods: dict[str, list[str]]) -> None:
    """Import names a path entry provides (plain entries; [] = no
    distribution)."""
    try:
        names = sorted(os.listdir(directory))[:MAX_STATIC_ENTRIES]
    except OSError:
        return
    for n in names:
        if n.endswith((".dist-info", ".egg-info", ".pth", ".egg-link")):
            continue
        if n.endswith(".py"):
            n = n[:-3]
        elif n.endswith((".so", ".pyd")):
            n = n.split(".")[0]
        elif not (directory / n).is_dir():
            continue
        if n.isidentifier() and n != "__pycache__":
            mods.setdefault(n, [])


def static_probe(python: str) -> dict[str, Any]:
    """What a virtual environment provides, read as text without running
    anything (see the module docstring for what this cannot see). Returns
    a raw probe dict like ``run_probe``'s, with ``probe: "static"``."""
    venv = venv_dir(python)
    if venv is None:
        return {"ok": False, "probe": "static",
                "error": "not run: project-shipped and not trusted, and not a "
                         "virtual environment (no pyvenv.cfg) that could be read "
                         f"instead (set {TRUST_ENV}=1 to run it)"}
    cfg = read_pyvenv_cfg(venv)
    sites: list[Path] = []
    seen_real: set[str] = set()
    for site in [*sorted(venv.glob("lib/python*/site-packages")),
                 *sorted(venv.glob("lib64/python*/site-packages")),
                 venv / "Lib" / "site-packages"]:
        real = os.path.realpath(site)
        if site.is_dir() and real not in seen_real:
            seen_real.add(real)
            sites.append(site)
    version = _version_text(cfg.get("version") or cfg.get("version_info") or "")
    if not version:
        for site in sites:
            match = re.fullmatch(r"python(\d+\.\d+)", site.parent.name)
            if match:
                version = match.group(1)
                break
    mods: dict[str, list[str]] = {}
    scanned: list[str] = []
    dists = 0
    pth_dirs: list[Path] = []
    for site in sites:
        scanned.append(str(site))
        _list_entries(site, mods)
        try:
            entries = sorted(os.listdir(site))[:MAX_STATIC_ENTRIES]
        except OSError:
            continue
        for n in entries:
            path = site / n
            if n.endswith((".dist-info", ".egg-info")) and path.is_dir():
                dists += 1
                name, tops = _dist_tops(path)
                for top in tops:
                    lst = mods.setdefault(top, [])
                    if name and name not in lst:
                        lst.append(name)
            elif n.endswith(".pth"):
                # Path lines only; `import` lines are code and are not run.
                for line in (_read_bounded(path, 64 * 1024) or "").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "import ", "import\t")):
                        continue
                    target = Path(line) if os.path.isabs(line) else site / line
                    if target.is_dir() and len(pth_dirs) < MAX_PTH_DIRS:
                        pth_dirs.append(target)
    for target in pth_dirs:
        if str(target) not in scanned:
            scanned.append(str(target))
            _list_entries(target, mods)
    system = cfg.get("include-system-site-packages", "").lower() == "true"
    return {"ok": True, "probe": "static", "limit": STATIC_LIMIT,
            "version": version, "executable": python, "prefix": str(venv),
            "base_prefix": cfg.get("home") or "", "home": cfg.get("home") or "",
            "system_site_packages": system, "stdlib": [], "scanned": scanned,
            "distributions": dists, "modules": mods}


class ProbeCache:
    """Probe results by interpreter path, in ``<state dir>/environments/
    probes.json``. ``mode`` is ``rw`` (read and write), ``read`` (use
    entries, never write) or ``off``."""

    def __init__(self, path: Path | None, mode: str = "rw") -> None:
        if mode not in CACHE_MODES:
            raise EnvError(f"unknown cache mode {mode!r}")
        self.path = path
        self.mode = mode if path is not None else "off"
        self._entries: dict[str, Any] | None = None
        self.dirty = False

    @classmethod
    def for_root(cls, root: str | Path, mode: str = "rw") -> ProbeCache:
        from ozagia.config import state_path
        return cls(state_path(root, CACHE_DIR, CACHE_NAME), mode)

    def _load(self) -> dict[str, Any]:
        if self._entries is None:
            self._entries = {}
            if self.mode != "off" and self.path is not None:
                try:
                    doc = json.loads(self.path.read_text(encoding="utf-8"))
                    if (isinstance(doc, dict) and doc.get("schema") == CACHE_SCHEMA
                            and isinstance(doc.get("probes"), dict)):
                        self._entries = doc["probes"]
                except (OSError, ValueError):
                    pass
        return self._entries

    def get(self, python: str, key: str | None = None) -> dict[str, Any] | None:
        if self.mode == "off":
            return None
        entry = self._load().get(key or python)
        if isinstance(entry, dict) and _valid(entry, python):
            return entry.get("probe")
        return None

    def put(self, python: str, probe: Mapping[str, Any],
            key: str | None = None) -> None:
        if self.mode != "rw" or not probe.get("ok"):
            return
        self._load()[key or python] = {
            "signature": _signature(python, probe.get("scanned") or []),
            "probe": dict(probe)}
        self.dirty = True

    def save(self) -> None:
        """Write the cache atomically (``rw`` mode, when it changed)."""
        if self.mode != "rw" or not self.dirty or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"schema": CACHE_SCHEMA,
                                   "probes": self._load()}, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


# In-process memo: interpreter path -> {"signature", "probe"}.
_MEMO: dict[str, dict[str, Any]] = {}


def clear_memo() -> None:
    _MEMO.clear()
    _MODULE_ENV_MEMO.clear()
    _ROOTS_MEMO.clear()


def probe(python: str, *, cache: ProbeCache | None = None,
          timeout: float = PROBE_TIMEOUT_S,
          runner: Callable[..., dict[str, Any]] | None = None,
          static: bool = False) -> dict[str, Any]:
    """What ``python`` provides: ``ok``, ``probe`` (``exec`` or ``static``),
    ``version``, ``prefix``, ``base_prefix``, ``is_venv``, ``modules``
    (top-level import name -> distribution names; [] for a name found on a
    path entry), ``stdlib``, ``scanned`` and ``cached`` (``memo``, ``file``
    or false). With ``static`` the interpreter is not run (``static_probe``).
    A failed probe is remembered in this process (so a hanging interpreter
    costs its timeout once per run), not in the cache file."""
    python = os.path.abspath(python)
    key = f"static:{python}" if static else python
    use_cache = cache is None or cache.mode != "off"
    memo = _MEMO.get(key) if use_cache else None
    if memo is not None and _valid(memo, python):
        return {**memo["probe"], "cached": "memo"}
    if cache is not None:
        hit = cache.get(python, key)
        if hit is not None:
            _MEMO[key] = {"signature": _signature(python, hit.get("scanned") or []),
                          "probe": hit}
            return {**hit, "cached": "file"}
    raw = static_probe(python) if static else (runner or run_probe)(python, timeout=timeout)
    result = _normalize_probe(python, raw, "static" if static else "exec")
    if use_cache:
        _MEMO[key] = {"signature": _signature(python, result.get("scanned") or []),
                      "probe": result}
    if result["ok"] and cache is not None:
        cache.put(python, result, key)
    return {**result, "cached": False}


def _normalize_probe(python: str, raw: Mapping[str, Any],
                     kind: str = "exec") -> dict[str, Any]:
    if not raw.get("ok"):
        return {"ok": False, "path": python, "probe": kind,
                "error": str(raw.get("error") or "probe failed")}
    modules: dict[str, list[str]] = {}
    for name, dists in (raw.get("modules") or {}).items():
        if isinstance(name, str) and name.isidentifier():
            modules[name] = sorted({str(d) for d in (dists or [])
                                    if isinstance(d, str)})
    prefix = str(raw.get("prefix") or "")
    base = str(raw.get("base_prefix") or prefix)
    out = {
        "ok": True, "path": python, "probe": kind,
        "executable": str(raw.get("executable") or python),
        "version": str(raw.get("version") or ""),
        "prefix": prefix, "base_prefix": base, "is_venv": prefix != base,
        "stdlib": sorted({str(s) for s in raw.get("stdlib") or []}),
        "scanned": [str(s) for s in raw.get("scanned") or []],
        "distributions": int(raw.get("distributions") or 0),
        "modules": dict(sorted(modules.items())),
    }
    if kind == "static":
        out.update(limit=str(raw.get("limit") or STATIC_LIMIT),
                   home=str(raw.get("home") or ""),
                   system_site_packages=bool(raw.get("system_site_packages")))
    return out


def provides(probe_result: Mapping[str, Any], module: str) -> bool:
    """Whether a probed interpreter can import the top-level of ``module``
    (an installed distribution, a path entry or its standard library)."""
    if not probe_result.get("ok"):
        return False
    top = module.split(".", 1)[0]
    if top in (probe_result.get("modules") or {}):
        return True
    stdlib = probe_result.get("stdlib") or []
    if stdlib:
        return top in stdlib or top in sys.builtin_module_names
    # No stdlib list (Python < 3.10): assume ours.
    return top in STDLIB


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #


def _executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def venv_python(venv: Path) -> Path | None:
    """The interpreter of a virtual environment directory, or None."""
    for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
        path = venv / rel
        if _executable(path):
            return path
    return None


def _venv_dirs(root: Path) -> list[Path]:
    out = [root / name for name in VENV_DIRS]
    try:
        out += sorted(p for p in root.iterdir()
                      if p.name.startswith(VENV_GLOB_PREFIX))
    except OSError:
        pass
    return [p for p in out if p.is_dir()]


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def source_root(root: str | Path) -> Path | None:
    """The repository a git worktree or a local clone of ``root`` came
    from, or None (not a worktree or clone, or the same directory)."""
    root = Path(root).resolve()
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        path = Path(common)
        main = path.parent if path.name == ".git" else None
        if main is not None and main.is_dir() and main.resolve() != root:
            return main.resolve()
    url = _git(root, "config", "--get", "remote.origin.url")
    if url:
        if url.startswith("file://"):
            url = url[len("file://"):]
        path = Path(url)
        if path.is_absolute() and path.is_dir():
            path = path.resolve()
            if path.name == ".git":
                path = path.parent
            if path != root:
                return path
    return None


def _run_tool(argv: Sequence[str], cwd: Path) -> str | None:
    """A lock-file tool's one-line answer (bounded), or None."""
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd), capture_output=True,
                              text=True, timeout=TOOL_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.strip().splitlines() if proc.returncode == 0 else []
    return lines[-1].strip() if lines else None


PDM_PYTHON_FILE = ".pdm-python"
MAX_PDM_PYTHON_BYTES = 4096


def pdm_python(root: Path) -> Path | None:
    """The interpreter a `.pdm-python` file names, read as text (bounded;
    a relative path is taken from the root), when it is an executable
    file; else None. Nothing is run."""
    text = _read_bounded(root / PDM_PYTHON_FILE, MAX_PDM_PYTHON_BYTES)
    lines = (text or "").strip().splitlines()
    if not lines or not lines[0].strip():
        return None
    raw = Path(os.path.expanduser(lines[0].strip()))
    path = raw if raw.is_absolute() else root / raw
    return path if _executable(path) else None


def _lock_candidates(root: Path, *, which: Callable[[str], str | None],
                     tool: Callable[[Sequence[str], Path], str | None],
                     run_tools: bool = False
                     ) -> list[tuple[Path, str, str]]:
    """Interpreters named by lock files. uv's venv and pdm's `.venv` and
    `.pdm-python` are read from the filesystem. `poetry env info --path`
    and `pdm info --python` are run only with ``run_tools`` (``discover``:
    a trusted project, not in `read` mode): pdm runs the interpreter a
    committed `.pdm-python` names and may create `.venv` and `.pdm-python`
    in the repository."""
    out: list[tuple[Path, str, str]] = []
    if (root / "uv.lock").is_file():
        env = os.environ.get("UV_PROJECT_ENVIRONMENT")
        venv = (Path(env) if env and os.path.isabs(env) else root / (env or ".venv"))
        py = venv_python(venv)
        if py is not None:
            out.append((py, "lock", f"uv.lock: {venv}"))
    if run_tools and (root / "poetry.lock").is_file() and which("poetry"):
        answer = tool(["poetry", "env", "info", "--path"], root)
        py = venv_python(Path(answer)) if answer else None
        if py is not None:
            out.append((py, "lock", "poetry.lock: poetry env info --path"))
    if (root / "pdm.lock").is_file():
        py = venv_python(root / ".venv")
        evidence = "pdm.lock"
        if py is None:
            py = pdm_python(root)
            evidence = f"pdm.lock: {PDM_PYTHON_FILE} (read as text)"
        if py is None and run_tools and which("pdm"):
            answer = tool(["pdm", "info", "--python"], root)
            py = Path(answer) if answer and _executable(Path(answer)) else None
            evidence = "pdm.lock: pdm info --python"
        if py is not None:
            out.append((py, "lock", evidence))
    return out


_DOC_PYTHON = re.compile(
    r"(?<![\w.$/-])((?:~|\.{1,2})?[\w.~-]*(?:/[\w.-]+)*/"
    r"(?:python|python3(?:\.\d+)?|pypy3?)|python3\.\d+)(?![\w.-])")


def doc_interpreters(root: Path, docs: Iterable[str], *,
                     which: Callable[[str], str | None] = shutil.which
                     ) -> list[tuple[Path, str]]:
    """Existing interpreters named in the command docs: paths
    (`.venv/bin/python`, `~/envs/x/bin/python3`) and versioned names
    (`python3.12`) on PATH, with the doc that names each."""
    out: list[tuple[Path, str]] = []
    for rel in docs:
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_DOC_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for token in dict.fromkeys(_DOC_PYTHON.findall(text)):
            if "/" in token:
                expanded = Path(os.path.expanduser(token))
                cand = expanded if expanded.is_absolute() else root / expanded
            else:
                found = which(token)
                cand = Path(found) if found else None
            if cand is not None and _executable(cand):
                out.append((cand, rel))
    return out


def _key(path: Path) -> tuple[str, str]:
    """Two names of one interpreter in one directory (`python`, `python3`)
    are one candidate; a venv's `python` and the base it links to are not."""
    return (os.path.realpath(path.parent), os.path.realpath(path))


# --------------------------------------------------------------------------- #
# trust
# --------------------------------------------------------------------------- #


def _inside(path: str | Path, roots: Iterable[Path]) -> bool:
    p = Path(path)
    return any(p == r or r in p.parents for r in roots)


# root realpath -> (stamp of `<root>/.git`, project roots). `source_root`
# runs two git subprocesses; the answer only changes when `.git` does.
_ROOTS_MEMO: dict[str, tuple[int | None, list[Path]]] = {}
ROOTS_MEMO_MAX = 256


def project_roots(root: str | Path) -> list[Path]:
    """The directories whose interpreters count as project-shipped: the
    root and, for a worktree or clone, the source repository. Cached per
    root in this process, valid while the mtime of `<root>/.git` (a
    directory, or a worktree's file) is unchanged."""
    root = Path(os.path.realpath(root))
    stamp = _stat_ns(str(root / ".git"))
    hit = _ROOTS_MEMO.get(str(root))
    if hit is not None and hit[0] == stamp:
        return list(hit[1])
    src = source_root(root)
    out = [root] + ([Path(os.path.realpath(src))] if src is not None else [])
    if len(_ROOTS_MEMO) >= ROOTS_MEMO_MAX:
        _ROOTS_MEMO.pop(next(iter(_ROOTS_MEMO)))
    _ROOTS_MEMO[str(root)] = (stamp, out)
    return list(out)


def is_project_shipped(python: str | Path, roots: Sequence[Path]) -> bool:
    """Whether an interpreter is the project's own: its candidate path (with
    the directory resolved, the file not), its venv directory or its
    resolved binary lies inside one of ``roots`` (see ``project_roots``)."""
    path = Path(os.path.abspath(python))
    real_roots = [Path(os.path.realpath(r)) for r in roots]
    places = [path, Path(os.path.realpath(path.parent)) / path.name,
              Path(os.path.realpath(path))]
    venv = venv_dir(path)
    if venv is not None:
        places += [venv, Path(os.path.realpath(venv))]
    return any(_inside(p, real_roots) for p in places)


def is_self(python: str | Path) -> bool:
    """Whether ``python`` is the interpreter ozagia runs under."""
    return bool(sys.executable) and _key(Path(os.path.abspath(python))) == \
        _key(Path(os.path.abspath(sys.executable)))


def trust_project_python(root: str | Path) -> bool:
    """Whether the user allowed running the project's own interpreters:
    `OZAGIA_TRUST_PROJECT_PYTHON=1` in this process's environment, or
    `[environments] trust_project_python = true` in the user-level config
    (`intent.lint.user_config_path`; not one inside the root). A
    repository's own `.ozagia.toml` cannot set it."""
    if os.environ.get(TRUST_ENV, "").strip().lower() in ("1", "true", "yes"):
        return True
    from ozagia.intent import lint as L
    try:
        path = L.user_config_path()
        if not path.is_file() or any(L._within(r, path)
                                     for r in project_roots(root)):
            return False
    except (OSError, RuntimeError):
        return False
    data = L._toml(path) or {}
    table = data.get("environments")
    return isinstance(table, dict) and table.get("trust_project_python") is True


def discover(root: str | Path, *, which: Callable[[str], str | None] = shutil.which,
             tool: Callable[[Sequence[str], Path], str | None] = _run_tool,
             command_docs: Iterable[str] | None = None,
             include_self: bool = True, trust: bool | None = None,
             cache_mode: str = "rw",
             lock_tools: bool | None = None) -> list[dict[str, Any]]:
    """Candidate interpreters for ``root``, in order of evidence. Each is
    {"path", "source", "evidence", "also", "project"} (``also``: other
    evidence for the same interpreter; ``project``: project-shipped, see
    ``is_project_shipped``). Nothing in the root is run: the lock-file
    tools (`poetry`, `pdm`) run only when ``lock_tools`` is true, by
    default when the project is trusted (``trust``, None:
    ``trust_project_python``) and ``cache_mode`` is not `read`."""
    if cache_mode not in CACHE_MODES:
        raise EnvError(f"unknown cache mode {cache_mode!r}")
    root = Path(root).resolve()
    roots = project_roots(root)
    if lock_tools is None:
        lock_tools = cache_mode != "read" and _trusted_flag(root, trust)
    found: list[tuple[Path, str, str]] = []
    env = os.environ.get("VIRTUAL_ENV")
    if env:
        py = venv_python(Path(env))
        if py is not None:
            found.append((py, "virtual_env", f"$VIRTUAL_ENV={env}"))
    own = [(venv_python(d), d) for d in _venv_dirs(root)]
    own = [(py, d) for py, d in own if py is not None]
    found += [(py, "root_venv", d.name) for py, d in own]
    if not own:
        src = roots[1] if len(roots) > 1 else None
        if src is not None:
            for d in _venv_dirs(src):
                py = venv_python(d)
                if py is not None:
                    found.append((py, "source_venv", f"{d.name} of the source "
                                                     f"repository {src}"))
    found += _lock_candidates(root, which=which, tool=tool, run_tools=lock_tools)
    if command_docs is None:
        command_docs = _config(root, "command_docs", ("README.md",))
    found += [(py, "command_docs", f"named in {doc}")
              for py, doc in doc_interpreters(root, command_docs, which=which)]
    for name in ("python", "python3"):
        hit = which(name)
        if hit:
            found.append((Path(hit), "path", f"`{name}` on PATH"))
    if include_self and sys.executable:
        found.append((Path(sys.executable), "ozagia",
                      "the interpreter ozagia runs under"))
    out: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for path, source, evidence in found:
        path = Path(os.path.abspath(path))
        key = _key(path)
        if key in seen:
            seen[key]["also"].append(f"{source}: {evidence}")
            seen[key]["project"] = (seen[key]["project"]
                                    or is_project_shipped(path, roots))
            continue
        item = {"path": str(path), "source": source, "evidence": evidence,
                "also": [], "project": is_project_shipped(path, roots)}
        seen[key] = item
        out.append(item)
    return out


def _config(root: Path, name: str, default: Sequence[str]) -> tuple[str, ...]:
    try:
        from ozagia.config import load_project_config
        return tuple(getattr(load_project_config(root), name))
    except Exception:  # noqa: BLE001 - a bad config is reported elsewhere
        return tuple(default)


# --------------------------------------------------------------------------- #
# imports
# --------------------------------------------------------------------------- #


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    out = set()
    for node in nodes:
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def _is_type_checking(test: ast.expr) -> bool:
    return ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"))


def _is_main_guard(test: ast.expr) -> bool:
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == "__name__" and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def _worse(a: str, b: str) -> str:
    return a if KIND_RANK[a] >= KIND_RANK[b] else b


def _dotted(node: ast.AST) -> str:
    """`sys.version_info` -> "sys.version_info" (names and attributes only)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


_CONDITION_NAMES = ("sys.version_info", "sys.platform", "sys.implementation",
                    "os.name", "platform")
_COMPARE_OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
                ast.Gt: ">", ast.GtE: ">="}


def _is_condition(test: ast.expr) -> bool:
    """Whether an `if` test looks at the interpreter's version or platform."""
    for node in ast.walk(test):
        name = _dotted(node) if isinstance(node, (ast.Attribute, ast.Name)) else ""
        if name and any(name == c or name.startswith(c + ".")
                        for c in _CONDITION_NAMES):
            return True
    return False


def _int_tuple(node: ast.AST) -> list[int] | None:
    if isinstance(node, ast.Tuple) and node.elts and all(
            isinstance(e, ast.Constant) and type(e.value) is int for e in node.elts):
        return [int(e.value) for e in node.elts]  # type: ignore[attr-defined]
    return None


def _condition(test: ast.expr) -> dict[str, Any]:
    """A version or platform test as data ``evaluate_conditions`` can
    evaluate: {"kind": "version", "op", "value", "slice"}, {"kind":
    "platform", "op", "value"} or {"kind": "unknown"}; ``negate`` for
    `not`."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _condition(test.operand)
        return {**inner, "negate": not inner.get("negate", False)}
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = _COMPARE_OPS.get(type(test.ops[0]))
        left, right = test.left, test.comparators[0]
        cut = None
        if (isinstance(left, ast.Subscript) and isinstance(left.slice, ast.Slice)
                and left.slice.lower is None and left.slice.step is None
                and isinstance(left.slice.upper, ast.Constant)
                and type(left.slice.upper.value) is int):
            cut = int(left.slice.upper.value)
            left = left.value
        name = _dotted(left)
        if op and name == "sys.version_info" and _int_tuple(right) is not None:
            return {"kind": "version", "op": op, "value": _int_tuple(right),
                    "slice": cut}
        if (op in ("==", "!=") and name == "sys.platform" and cut is None
                and isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return {"kind": "platform", "op": op, "value": right.value}
    if (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute)
            and test.func.attr == "startswith"
            and _dotted(test.func.value) == "sys.platform" and len(test.args) == 1
            and isinstance(test.args[0], ast.Constant)
            and isinstance(test.args[0].value, str)):
        return {"kind": "platform", "op": "startswith", "value": test.args[0].value}
    return {"kind": "unknown"}


def _negated(cond: Mapping[str, Any]) -> dict[str, Any]:
    return {**cond, "negate": not cond.get("negate", False)}


def version_tuple(text: str | None) -> tuple[int, ...] | None:
    match = _VERSION.match(str(text or "").strip())
    if not match:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


def evaluate_conditions(conds: Iterable[Mapping[str, Any]],
                        version: tuple[int, ...] | None,
                        platform: str = sys.platform) -> bool | None:
    """Whether every condition holds for an interpreter of ``version`` on
    ``platform`` (the probed interpreters all run on this machine): True,
    False, or None when one cannot be evaluated."""
    result: bool | None = True
    for cond in conds:
        value: bool | None
        kind = cond.get("kind")
        if kind == "version" and version is not None:
            have = version[:cond["slice"]] if cond.get("slice") else version
            want = tuple(cond.get("value") or ())
            op = cond.get("op")
            value = {"==": have == want, "!=": have != want, "<": have < want,
                     "<=": have <= want, ">": have > want,
                     ">=": have >= want}.get(str(op))
        elif kind == "platform":
            want_p = str(cond.get("value"))
            value = {"==": platform == want_p, "!=": platform != want_p,
                     "startswith": platform.startswith(want_p)}.get(
                         str(cond.get("op")))
        else:
            value = None
        if value is not None and cond.get("negate"):
            value = not value
        if value is False:
            return False
        if value is None:
            result = None
    return result


def file_imports(text: str) -> list[dict[str, Any]]:
    """The imports of Python source: [{"module", "names", "level", "kind",
    "line"}], plus, for a ``conditional`` import, ``cond`` (the version and
    platform tests it is under, see ``evaluate_conditions``) and ``base``
    (its kind without them). ``module`` is the dotted name after
    `import`/`from` (empty for `from . import x`), ``names`` the imported
    names of a `from` import and ``level`` its leading dots. A file that
    does not parse falls back to a line scan (every import ``module``)."""
    if "import" not in text:
        return []
    try:
        with warnings.catch_warnings():
            # `SyntaxWarning: invalid escape sequence` of the project's
            # source is not ours to print.
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _scan_imports(text)
    out: list[dict[str, Any]] = []

    def top(module: str) -> str:
        return module.split(".", 1)[0]

    def emit(rec: dict[str, Any], kind: str, conds: list[dict[str, Any]],
             skips: set[str]) -> None:
        if not rec["level"] and top(rec["module"]) in skips:
            kind = _worse(kind, "guarded")
        if conds and KIND_RANK[kind] < KIND_RANK["conditional"]:
            rec.update(kind="conditional", base=kind, cond=[dict(c) for c in conds])
        else:
            rec["kind"] = kind
        out.append(rec)

    def skip_call(node: ast.AST | None, kind: str, skips: set[str]) -> None:
        """`pytest.importorskip("x")` as a statement or an assigned value:
        the imports of x after it in the same function (or anywhere in the
        file, at module level) are guarded."""
        if not isinstance(node, ast.Call):
            return
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if (name == "importorskip" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.append({"module": node.args[0].value, "names": [],
                        "level": 0, "kind": "guarded",
                        "line": node.lineno, "skip": kind})
            skips.add(top(node.args[0].value))

    def block(nodes: Iterable[ast.AST], kind: str, conds: list[dict[str, Any]],
              skips: set[str]) -> None:
        for node in nodes:
            visit(node, kind, conds, skips)

    # Statements only: imports are statements, and expressions are not
    # walked (importorskip is looked for in expression statements and
    # assignments).
    def visit(node: ast.AST, kind: str, conds: list[dict[str, Any]],
              skips: set[str]) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                emit({"module": alias.name, "names": [], "level": 0,
                      "line": node.lineno}, kind, conds, skips)
        elif isinstance(node, ast.ImportFrom):
            emit({"module": node.module or "",
                  "names": [a.name for a in node.names if a.name != "*"],
                  "level": node.level or 0, "line": node.lineno},
                 kind, conds, skips)
        elif isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
            skip_call(node.value, kind, skips)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # A function's own importorskip calls guard only its own body.
            block(node.body, _worse(kind, "lazy"), conds, set(skips))
        elif isinstance(node, ast.ClassDef):
            block(node.body, kind, conds, skips)
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            guarded = any(_handler_names(h) & _IMPORT_ERRORS
                          for h in node.handlers)
            inner = _worse(kind, "guarded") if guarded else kind
            block(node.body, inner, conds, skips)
            for handler in node.handlers:
                block(handler.body, inner, conds, skips)
            block([*node.orelse, *node.finalbody], kind, conds, skips)
        elif isinstance(node, ast.If):
            if _is_type_checking(node.test):
                block(node.body, _worse(kind, "guarded"), conds, skips)
                block(node.orelse, kind, conds, skips)
            elif _is_main_guard(node.test):
                block(node.body, _worse(kind, "lazy"), conds, skips)
                block(node.orelse, kind, conds, skips)
            elif _is_condition(node.test):
                cond = _condition(node.test)
                block(node.body, kind, [*conds, cond], skips)
                block(node.orelse, kind, [*conds, _negated(cond)], skips)
            else:
                block(node.body, kind, conds, skips)
                block(node.orelse, kind, conds, skips)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            block([*node.body, *node.orelse], kind, conds, skips)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            block(node.body, kind, conds, skips)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                block(case.body, kind, conds, skips)

    block(tree.body, "module", [], set())
    # A module-level `pytest.importorskip("x")` skips the whole file without
    # x, so every import of x in it is guarded (before the call too).
    skipped = {top(str(r["module"])) for r in out
               if r.pop("skip", None) == "module"}
    for rec in out:
        if not rec["level"] and top(str(rec["module"])) in skipped:
            rec["kind"] = "guarded"
            rec.pop("cond", None)
            rec.pop("base", None)
    return out


_IMPORT_LINE = re.compile(
    r"^[ \t]*(?:from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n#;]+)"
    r"|import[ \t]+([^\n#;]+))", re.M)


def _scan_imports(text: str) -> list[dict[str, Any]]:
    out = []
    for match in _IMPORT_LINE.finditer(text):
        dots, base, names, plain = match.groups()
        line = text.count("\n", 0, match.start()) + 1
        if plain is not None:
            for item in plain.split(","):
                word = item.strip().split(" ", 1)[0]
                if word:
                    out.append({"module": word, "names": [], "level": 0,
                                "kind": "module", "line": line})
            continue
        words = [w.strip().split(" ", 1)[0]
                 for w in names.strip("()").replace("\n", " ").split(",")]
        out.append({"module": base or "", "level": len(dots or ""),
                    "names": [w for w in words if w.isidentifier()],
                    "kind": "module", "line": line})
    return out


def is_test_file(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name.endswith(".py") and (name.startswith("test_")
                                     or name.endswith("_test.py"))


def _read_file(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def tracked_files(root: Path) -> list[str]:
    """`git ls-files`, or a bounded walk without the generic skip dirs."""
    out = _git(root, "ls-files", "-z", "--cached")
    if out is not None:
        return sorted({f for f in out.split("\0") if f})
    from ozagia.config import GENERIC_SKIP_DIRS
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in GENERIC_SKIP_DIRS
                             and not d.startswith(VENV_GLOB_PREFIX))
        base = Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            files.append(name if base == "." else f"{base}/{name}")
    return files


def blob_ids(root: Path) -> dict[str, str]:
    """Tracked path -> git blob id, for files whose working-tree content is
    the index's (`git diff-files` reports none of them). Empty outside git."""
    staged = _git(root, "ls-files", "-s", "-z")
    if staged is None:
        return {}
    dirty = set((_git(root, "diff-files", "--name-only", "-z") or "").split("\0"))
    out: dict[str, str] = {}
    for item in staged.split("\0"):
        head, sep, rel = item.partition("\t")
        parts = head.split()
        if not sep or len(parts) != 3 or parts[2] != "0" or rel in dirty:
            continue
        if parts[0] in ("100644", "100755"):
            out[rel] = parts[1]
    return out


_REC_FIELDS = ("module", "names", "level", "kind", "line", "cond", "base")


def _pack(records: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    return [[r.get(f) for f in _REC_FIELDS] for r in records]


def _unpack(rows: Any) -> list[dict[str, Any]] | None:
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(_REC_FIELDS) \
                or row[3] not in KIND_RANK:
            return None
        rec = dict(zip(_REC_FIELDS, row))
        if rec["cond"] is None:
            del rec["cond"], rec["base"]
        out.append(rec)
    return out


class ImportCache:
    """Parsed imports (`file_imports`) by git blob id, in ``<state dir>/
    environments/imports.json``. ``mode`` as for ``ProbeCache``. ``save``
    keeps only the blobs of the current tree."""

    def __init__(self, path: Path | None, mode: str = "rw") -> None:
        if mode not in CACHE_MODES:
            raise EnvError(f"unknown cache mode {mode!r}")
        self.path = path
        self.mode = mode if path is not None else "off"
        self._entries: dict[str, Any] | None = None
        self.dirty = False
        self.hits = 0
        self.misses = 0

    @classmethod
    def for_root(cls, root: str | Path, mode: str = "rw") -> ImportCache:
        from ozagia.config import state_path
        return cls(state_path(root, CACHE_DIR, IMPORTS_CACHE_NAME), mode)

    def _load(self) -> dict[str, Any]:
        if self._entries is None:
            self._entries = {}
            if self.mode != "off" and self.path is not None:
                try:
                    doc = json.loads(self.path.read_text(encoding="utf-8"))
                    if (isinstance(doc, dict) and doc.get("schema") == IMPORTS_SCHEMA
                            and isinstance(doc.get("blobs"), dict)):
                        self._entries = doc["blobs"]
                except (OSError, ValueError):
                    pass
        return self._entries

    def get(self, blob: str) -> list[dict[str, Any]] | None:
        if self.mode == "off":
            return None
        out = _unpack(self._load().get(blob))
        if out is None:
            self.misses += 1
        else:
            self.hits += 1
        return out

    def put(self, blob: str, records: Sequence[Mapping[str, Any]]) -> None:
        if self.mode != "rw":
            return
        self._load()[blob] = _pack(records)
        self.dirty = True

    def save(self, keep: Iterable[str] | None = None) -> None:
        """Write atomically (``rw``, when changed), dropping blobs not in
        ``keep``."""
        if self.mode != "rw" or self.path is None:
            return
        entries = self._load()
        if keep is not None:
            wanted = set(keep)
            stale = [b for b in entries if b not in wanted]
            for blob in stale:
                del entries[blob]
            self.dirty = self.dirty or bool(stale)
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"schema": IMPORTS_SCHEMA, "blobs": entries},
                                  separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


def provided_names(probes: Iterable[Mapping[str, Any]]) -> set[str]:
    """Top-level names an installed distribution provides in one of the
    probed interpreters (not names found only on a path entry)."""
    out: set[str] = set()
    for pr in probes:
        if pr.get("ok"):
            out.update(n for n, dists in (pr.get("modules") or {}).items() if dists)
    return out


class ImportModel:
    """Imports of a tree's Python files, first-party resolution and the
    closure of a set of files. ``read`` (rel -> text or None) defaults to
    reading under the root; files are parsed once, and with ``cache`` and
    ``blobs`` (``blob_ids``) not at all when their blob was parsed before.
    ``provided`` (``provided_names``) are names an installed distribution
    provides: the "anywhere in the tree" first-party fallback does not
    apply to them."""

    def __init__(self, root: str | Path, files: Iterable[str] | None = None, *,
                 read: Callable[[str], str | None] | None = None,
                 import_roots: Iterable[str] | None = None,
                 blobs: Mapping[str, str] | None = None,
                 cache: ImportCache | None = None,
                 provided: Iterable[str] = ()) -> None:
        self.root = Path(root).resolve()
        self.files = sorted(set(files if files is not None
                                else tracked_files(self.root)))
        self._read = read or (lambda rel: _read_file(self.root, rel))
        if import_roots is None:
            import_roots = _config(self.root, "import_roots", ())
        self.roots = [""] + [r.strip("/") for r in import_roots if r.strip("/")]
        self.py = {f for f in self.files if f.endswith(".py")}
        self.dirs = {p.as_posix() for f in self.py
                     for p in PurePosixPath(f).parents if p.as_posix() != "."}
        self.blobs = dict(blobs or {})
        self.cache = cache
        self.provided = set(provided)
        self._parsed: dict[str, list[dict[str, Any]]] = {}
        self._edges: dict[str, dict[str, Any]] = {}
        self._basedirs: dict[str, str] = {}
        self.unread: set[str] = set()
        self._index_anywhere()

    def set_provided(self, names: Iterable[str]) -> None:
        """Names installed distributions provide (see the class docstring)."""
        names = set(names)
        if names != self.provided:
            self.provided = names
            self._edges.clear()

    def save_cache(self) -> None:
        if self.cache is not None:
            try:
                self.cache.save(keep=self.blobs.values())
            except OSError:
                pass

    def _index_anywhere(self) -> None:
        """Top-level name -> the directories holding `name.py` or
        `name/__init__.py` anywhere in the tree: modules that scripts and
        tests import after putting their directory on sys.path."""
        self.anywhere: dict[str, set[str]] = {}
        for rel in self.py:
            path = PurePosixPath(rel)
            if path.name == "__init__.py":
                if path.parent.as_posix() == ".":
                    continue
                name, base = path.parent.name, path.parent.parent.as_posix()
            else:
                name, base = path.stem, path.parent.as_posix()
            if name.isidentifier():
                self.anywhere.setdefault(name, set()).add("" if base == "." else base)

    def add(self, rel: str) -> None:
        """Include an untracked file asked about."""
        if rel.endswith(".py") and rel not in self.py:
            self.py.add(rel)
            self.dirs.update(p.as_posix() for p in PurePosixPath(rel).parents
                             if p.as_posix() != ".")
            self._edges.clear()
            self._basedirs.clear()
            self._index_anywhere()

    def imports(self, rel: str) -> list[dict[str, Any]]:
        if rel not in self._parsed:
            blob = self.blobs.get(rel)
            hit = self.cache.get(blob) if blob and self.cache is not None else None
            if hit is not None:
                self._parsed[rel] = hit
                return hit
            text = self._read(rel)
            if text is None:
                self.unread.add(rel)
            self._parsed[rel] = file_imports(text or "")
            if blob and text is not None and self.cache is not None:
                self.cache.put(blob, self._parsed[rel])
        return self._parsed[rel]

    def basedir(self, rel: str) -> str:
        """The first ancestor directory of ``rel`` without `__init__.py`:
        what pytest and `python script.py` put on sys.path."""
        hit = self._basedirs.get(rel)
        if hit is None:
            d = rel.rpartition("/")[0]
            while d and f"{d}/__init__.py" in self.py:
                d = d.rpartition("/")[0]
            hit = self._basedirs[rel] = d
        return hit

    def _bases(self, rel: str) -> list[str]:
        own = self.basedir(rel)
        return [own, *(r for r in self.roots if r != own)]

    def module_name(self, rel: str) -> str:
        stem = rel[:-3] if rel.endswith(".py") else rel
        if stem.endswith("/__init__") or stem == "__init__":
            stem = stem[:-len("__init__")].rstrip("/")
        best = ""
        for base in [*self.roots, self.basedir(rel)]:
            if base and stem.startswith(base + "/") and len(base) > len(best):
                best = base
        return (stem[len(best) + 1:] if best else stem).replace("/", ".")

    def _join(self, base: str, dotted: str) -> str:
        path = dotted.replace(".", "/")
        return f"{base}/{path}" if base else path

    def module_file(self, dotted: str, rel: str) -> str | None:
        """The tracked file of a first-party module imported from ``rel``:
        under its own base directory or an import root, else under the one
        directory anywhere in the tree that has the top-level module."""
        bases = self._bases(rel)
        top = dotted.split(".", 1)[0]
        elsewhere = (self.anywhere.get(top) or set()) if top not in self.provided \
            else set()
        if len(elsewhere) == 1:
            bases = [*bases, *elsewhere]
        for base in bases:
            path = self._join(base, dotted)
            if f"{path}.py" in self.py:
                return f"{path}.py"
            if f"{path}/__init__.py" in self.py:
                return f"{path}/__init__.py"
        return None

    def is_first_party(self, top: str, rel: str) -> bool:
        """A module of the tree: under the file's base directory or an
        import root, or a module or package of that name anywhere in it
        that no installed distribution provides (``provided``)."""
        for base in self._bases(rel):
            path = self._join(base, top)
            if f"{path}.py" in self.py or path in self.dirs:
                return True
        return top in self.anywhere and top not in self.provided

    def resolve(self, rec: Mapping[str, Any], rel: str) -> list[str]:
        """Absolute dotted names a record imports (a `from` import gives the
        module and module.name for each name)."""
        module = str(rec.get("module") or "")
        level = int(rec.get("level") or 0)
        if level:
            package = self.module_name(rel)
            if not rel.endswith("__init__.py"):
                package = package.rpartition(".")[0]
            parts = package.split(".") if package else []
            if level - 1 > len(parts):
                return []
            parts = parts[:len(parts) - (level - 1)]
            module = ".".join([*parts, *([module] if module else [])])
            if not module:
                return []
        names = [module] if module else []
        names += [f"{module}.{n}" for n in rec.get("names") or [] if module]
        return names

    def _first_party_files(self, dotted: str, rel: str) -> list[str]:
        """The files importing ``dotted`` executes: its parent packages'
        `__init__.py` and its own file."""
        parts = dotted.split(".")
        out = []
        for n in range(1, len(parts) + 1):
            hit = self.module_file(".".join(parts[:n]), rel)
            if hit and hit not in out:
                out.append(hit)
        return out

    def conftests(self, rel: str) -> list[str]:
        """conftest.py files pytest loads for a test file: in the root and
        each directory down to the test's."""
        parent = PurePosixPath(rel).parent
        dirs = [*reversed([p.as_posix() for p in parent.parents]), parent.as_posix()]
        out = []
        for d in dirs:
            path = "conftest.py" if d in (".", "") else f"{d}/conftest.py"
            if path in self.py and path != rel and path not in out:
                out.append(path)
        return out

    def edges(self, rel: str) -> dict[str, Any]:
        """One file's imports, resolved once: {"first_party": {kind:
        [files]}, "third_party": {kind: [tops]}, "stdlib": {kind: [tops]},
        "conditional": [{"class", "name", "base", "cond"}]} (``class``
        first_party (``name`` a file), third_party or stdlib (a top))."""
        cached = self._edges.get(rel)
        if cached is not None:
            return cached
        fp: dict[str, list[str]] = {k: [] for k in KIND_RANK}
        tp: dict[str, list[str]] = {k: [] for k in KIND_RANK}
        std: dict[str, list[str]] = {k: [] for k in KIND_RANK}
        cond: list[dict[str, Any]] = []
        for rec in self.imports(rel):
            kind = rec["kind"]
            for dotted in self.resolve(rec, rel):
                top = dotted.split(".", 1)[0]
                if self.is_first_party(top, rel):
                    for f in self._first_party_files(dotted, rel):
                        if f != rel and f not in fp[kind]:
                            fp[kind].append(f)
                        if f != rel and kind == "conditional":
                            cond.append({"class": "first_party", "name": f,
                                         "base": rec.get("base"),
                                         "cond": rec.get("cond") or []})
                    continue
                if rec.get("level"):
                    continue
                bucket = std if top in STDLIB else tp
                if top not in bucket[kind]:
                    bucket[kind].append(top)
                if kind == "conditional":
                    cond.append({"class": "stdlib" if top in STDLIB else "third_party",
                                 "name": top, "base": rec.get("base"),
                                 "cond": rec.get("cond") or []})
                break  # `from a import b`: one module, not one per name
        out = {"first_party": fp, "third_party": tp, "stdlib": std,
               "conditional": cond}
        self._edges[rel] = out
        return out

    def requirements(self, paths: Iterable[str], *, lazy_required: bool = True,
                     limit: int = MAX_CLOSURE_FILES) -> dict[str, Any]:
        """Third-party and standard-library modules ``paths`` need.

        The files asked about need their ``module`` imports and, with
        ``lazy_required``, their ``lazy`` ones; the first-party modules
        those import (and, for tests, the conftest.py files pytest loads)
        need their ``module`` imports. The rest is optional. A
        ``conditional`` import is listed under ``optional`` (third-party)
        and in ``conditional``: top -> [{"file", "cond", "required"}]
        (``required``: it would be required without its condition), for
        ``assess`` to evaluate per interpreter. First-party modules imported
        under a condition are followed like unconditional ones. Returns
        {"required", "optional", "stdlib", "conditional"} ({top: [files]}
        except ``conditional``), "closure": [files], "truncated": bool}.
        """
        targets = [p for p in dict.fromkeys(paths) if p.endswith(".py")]
        for rel in targets:
            self.add(rel)
        queue: list[tuple[str, bool]] = [(rel, True) for rel in targets]
        for rel in targets:
            if is_test_file(rel):
                queue += [(c, False) for c in self.conftests(rel)]
        seen: dict[str, bool] = {}
        required: dict[str, list[str]] = {}
        optional: dict[str, list[str]] = {}
        stdlib: dict[str, list[str]] = {}
        conditional: dict[str, list[dict[str, Any]]] = {}
        truncated = False

        def note(table: dict[str, list[str]], top: str, rel: str) -> None:
            files = table.setdefault(top, [])
            if rel not in files:
                files.append(rel)

        head = 0
        while head < len(queue):
            rel, target = queue[head]
            head += 1
            if rel in seen and (seen[rel] or not target):
                continue
            if len(seen) >= limit:
                truncated = True
                break
            seen[rel] = target or seen.get(rel, False)
            need = ("module", "lazy") if target and lazy_required else ("module",)
            edges = self.edges(rel)
            for kind in KIND_RANK:
                for top in edges["third_party"][kind]:
                    note(required if kind in need else optional, top, rel)
                for top in edges["stdlib"][kind]:
                    if kind in need:
                        note(stdlib, top, rel)
            for kind in need:
                queue += [(f, False) for f in edges["first_party"][kind]
                          if f not in seen]
            for item in edges["conditional"]:
                if item["class"] == "first_party":
                    if item["base"] in need and item["name"] not in seen:
                        queue.append((item["name"], False))
                    continue
                uses = conditional.setdefault(item["name"], [])
                use = {"file": rel, "cond": item["cond"],
                       "required": item["base"] in need}
                if use not in uses:
                    uses.append(use)
        for top in list(optional):
            if top in required:
                del optional[top]
        for top in list(conditional):
            if top in required or top in stdlib:
                del conditional[top]
        return {"required": dict(sorted(required.items())),
                "optional": dict(sorted(optional.items())),
                "stdlib": dict(sorted(stdlib.items())),
                "conditional": dict(sorted(conditional.items())),
                "closure": sorted(seen), "truncated": truncated}

    def third_party(self, rel: str) -> dict[str, dict[str, Any]]:
        """Direct third-party imports of one file: top -> {"kind" (the
        strongest), "line"}."""
        out: dict[str, dict[str, Any]] = {}
        for rec in self.imports(rel):
            if rec.get("level"):
                continue
            module = str(rec.get("module") or "")
            top = module.split(".", 1)[0]
            if not top or top in STDLIB or self.is_first_party(top, rel):
                continue
            prev = out.get(top)
            if prev is None or KIND_RANK[rec["kind"]] < KIND_RANK[prev["kind"]]:
                out[top] = {"kind": rec["kind"], "line": rec["line"]}
        return out


# --------------------------------------------------------------------------- #
# declared dependencies
# --------------------------------------------------------------------------- #


def _manifest_files(files: Iterable[str]) -> list[str]:
    from ozagia.config import GENERIC_SKIP_DIRS
    out = []
    for rel in files:
        parts = PurePosixPath(rel).parts
        if any(p in GENERIC_SKIP_DIRS or p.startswith(VENV_GLOB_PREFIX)
               for p in parts[:-1]):
            continue
        if MF.is_manifest(rel):
            out.append(rel)
    return out


def declared_dependencies(root: str | Path, files: Iterable[str] | None = None, *,
                          read: Callable[[str], str | None] | None = None
                          ) -> dict[str, Any]:
    """Python dependencies the tracked manifests declare: {"manifests":
    [files], "unparsed": [files], "by_name": {normalized name: [{"name",
    "file", "optional", "group"}]}}."""
    root = Path(root).resolve()
    files = tracked_files(root) if files is None else list(files)
    reader = read or (lambda rel: _read_file(root, rel))
    by_name: dict[str, list[dict[str, Any]]] = {}
    manifests: list[str] = []
    unparsed: list[str] = []
    for rel in _manifest_files(files):
        text = reader(rel)
        parsed = MF.parse_manifest(rel, text) if text is not None else None
        if parsed is None:
            unparsed.append(rel)
            continue
        if parsed["ecosystem"] != "python":
            continue
        manifests.append(rel)
        for dep in parsed["dependencies"]:
            by_name.setdefault(MF.normalize(dep["name"]), []).append(
                {"name": dep["name"], "file": rel, "optional": dep["optional"],
                 "group": dep["group"]})
    return {"manifests": manifests, "unparsed": unparsed,
            "by_name": dict(sorted(by_name.items()))}


def distribution_names(module: str,
                       probes: Iterable[Mapping[str, Any]] = ()) -> set[str]:
    """Normalized distribution names that may provide ``module``: its own
    name, known aliases and what probed interpreters install it from."""
    top = module.split(".", 1)[0]
    out = {MF.normalize(top)}
    out.update(MF.normalize(a) for a in IMPORT_ALIASES.get(top, ()))
    for item in probes:
        for dist in (item.get("modules") or {}).get(top, []) or []:
            out.add(MF.normalize(dist))
    return out


def declarations(module: str, declared: Mapping[str, Any],
                 probes: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The manifest entries that declare ``module`` (see
    ``declared_dependencies``)."""
    by_name = declared.get("by_name") or {}
    out = []
    for name in sorted(distribution_names(module, probes)):
        out += by_name.get(name, [])
    return out


# --------------------------------------------------------------------------- #
# answers
# --------------------------------------------------------------------------- #


def _rel_path(root: Path, path: str | Path) -> str:
    raw = Path(path)
    full = raw if raw.is_absolute() else root / raw
    try:
        return Path(os.path.abspath(full)).relative_to(root).as_posix()
    except ValueError:
        raise EnvError(f"{path} is outside the root {root}") from None


def expand_paths(root: Path, paths: Iterable[str | Path],
                 files: Sequence[str]) -> list[str]:
    """Root-relative Python files for ``paths`` (a directory gives its
    tracked .py files; a `path::node` suffix is dropped)."""
    out: list[str] = []
    for item in paths:
        text = str(item).split("::", 1)[0]
        rel = _rel_path(root, text)
        if (root / rel).is_dir():
            prefix = "" if rel in ("", ".") else rel + "/"
            out += [f for f in files if f.startswith(prefix) and f.endswith(".py")]
        elif rel.endswith(".py"):
            out.append(rel)
    return list(dict.fromkeys(out))


def probe_kind(item: Mapping[str, Any]) -> str:
    """``exec``, ``static`` or ``failed`` for a probed candidate."""
    pr = item.get("probe") or {}
    return str(pr.get("probe") or "exec") if pr.get("ok") else "failed"


def population(probed: Iterable[Mapping[str, Any]]) -> list[list[str]]:
    """The probed interpreter set, as compared between two reviews: sorted
    [path, probe kind, version]."""
    return sorted([str(item["path"]), probe_kind(item),
                   str((item.get("probe") or {}).get("version") or "")]
                  for item in probed)


def probe_one(cand: Mapping[str, Any], *, roots: Sequence[Path] | None = None,
              root: str | Path | None = None, trusted: bool = False,
              cache: ProbeCache | None = None, timeout: float = PROBE_TIMEOUT_S,
              runner: Callable[..., dict[str, Any]] | None = None
              ) -> dict[str, Any]:
    """One candidate with its ``probe`` and ``project`` flag: run, or read
    statically when it is project-shipped, not trusted and not the
    interpreter ozagia runs under."""
    path = str(cand["path"])
    project = cand.get("project")
    if project is None:
        if roots is None:
            roots = project_roots(root) if root is not None else []
        project = is_project_shipped(path, roots)
    static = bool(project) and not trusted and not is_self(path)
    result = probe(path, cache=cache, timeout=timeout, runner=runner,
                   static=static)
    return {**dict(cand), "project": bool(project), "probe": result}


def merge_system_site(probed: Sequence[dict[str, Any]]) -> None:
    """A statically read venv with `include-system-site-packages = true`
    also sees its base interpreter's packages: take them from a probed
    candidate in the venv's `home` directory, when there is one."""
    for item in probed:
        pr = item["probe"]
        if not (pr.get("ok") and pr.get("probe") == "static"
                and pr.get("system_site_packages") and pr.get("home")):
            continue
        home = os.path.realpath(str(pr["home"]))
        base = next((o["probe"] for o in probed
                     if o is not item and o["probe"].get("ok")
                     and o["probe"].get("probe") == "exec"
                     and (os.path.realpath(os.path.dirname(o["path"])) == home
                          or os.path.dirname(os.path.realpath(o["path"])) == home)),
                    None)
        modules = dict(pr.get("modules") or {})
        if base is None:
            item["probe"] = {**pr, "limit": pr.get("limit", STATIC_LIMIT)
                             + "; the base interpreter's packages "
                               "(include-system-site-packages) are not seen"}
            continue
        for name, dists in (base.get("modules") or {}).items():
            modules.setdefault(name, list(dists))
        item["probe"] = {**pr, "modules": dict(sorted(modules.items())),
                         "system_site_from": base.get("path"),
                         "stdlib": list(base.get("stdlib") or [])}


def _probe_all(candidates: Sequence[Mapping[str, Any]], cache: ProbeCache | None,
               timeout: float,
               runner: Callable[..., dict[str, Any]] | None, *,
               root: Path, trusted: bool) -> list[dict[str, Any]]:
    roots: list[Path] | None = None
    out = []
    for cand in candidates:
        if cand.get("project") is None and roots is None:
            roots = project_roots(root)
        out.append(probe_one(cand, roots=roots, trusted=trusted, cache=cache,
                             timeout=timeout, runner=runner))
    merge_system_site(out)
    if cache is not None:
        try:
            cache.save()
        except OSError:
            pass
    return out


def _summary(item: Mapping[str, Any]) -> dict[str, Any]:
    pr = item["probe"]
    out = {"path": item["path"], "source": item["source"],
           "evidence": item["evidence"], "also": list(item.get("also") or []),
           "project": bool(item.get("project")), "probe": probe_kind(item),
           "ok": bool(pr.get("ok")), "cached": pr.get("cached", False)}
    if pr.get("ok"):
        out.update(version=pr["version"], prefix=pr["prefix"],
                   is_venv=pr["is_venv"], distributions=pr["distributions"])
        if pr.get("probe") == "static":
            out["limit"] = pr.get("limit")
    else:
        out["error"] = pr.get("error")
    return out


def probe_candidates(root: str | Path, *, cache_mode: str = "rw",
                     timeout: float = PROBE_TIMEOUT_S,
                     candidates: Sequence[Mapping[str, Any]] | None = None,
                     runner: Callable[..., dict[str, Any]] | None = None,
                     trust: bool | None = None) -> list[dict[str, Any]]:
    """``discover`` then ``probe`` each: candidates with ``probe`` and
    ``project`` keys. ``trust`` None means ``trust_project_python``."""
    root = Path(root).resolve()
    trusted = trust_project_python(root) if trust is None else bool(trust)
    cands = (list(candidates) if candidates is not None
             else discover(root, trust=trusted, cache_mode=cache_mode))
    cache = ProbeCache.for_root(root, cache_mode)
    return _probe_all(cands, cache, timeout, runner, root=root, trusted=trusted)


def assess(pr: Mapping[str, Any], required: Iterable[str],
           optional: Iterable[str], stdlib: Iterable[str] = (),
           conditional: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
           ) -> dict[str, list[str]]:
    """What one probed interpreter misses: {"missing", "missing_optional"}.
    A conditional import (``ImportModel.requirements``) whose condition
    holds for this interpreter's version is required where it would be
    without the condition; one whose condition does not hold is ignored;
    one that cannot be evaluated stays optional."""
    missing = [m for m in required if not provides(pr, m)]
    missing += [m for m in stdlib if not provides(pr, m) and m not in missing]
    version = version_tuple(pr.get("version"))
    taken: set[str] = set()
    ignored: set[str] = set()
    for top, uses in (conditional or {}).items():
        states = [(evaluate_conditions(u.get("cond") or [], version), u)
                  for u in uses]
        if any(state is True and u.get("required") for state, u in states):
            taken.add(top)
        elif all(state is False for state, _u in states):
            ignored.add(top)
    for top in sorted(taken):
        if top not in missing and not provides(pr, top):
            missing.append(top)
    missing_optional = [m for m in optional if m not in taken and m not in ignored
                        and not provides(pr, m)]
    return {"missing": missing, "missing_optional": missing_optional}


def env_for(root: str | Path, paths: Iterable[str | Path], *,
            extra_modules: Iterable[str] = (), cache_mode: str = "rw",
            timeout: float = PROBE_TIMEOUT_S,
            candidates: Sequence[Mapping[str, Any]] | None = None,
            runner: Callable[..., dict[str, Any]] | None = None,
            model: ImportModel | None = None,
            trust: bool | None = None) -> dict[str, Any]:
    """The interpreters under which ``paths`` can run, best first.

    ``extra_modules`` are required too (e.g. `pytest` for a pytest check).
    An interpreter qualifies (``complete``) when it provides every required
    module; the ranking is: fewest missing required modules, then fewest
    missing optional ones, then the discovery order. ``best`` is the first
    complete interpreter or None. ``imports`` says, per module, whether it is
    required, which files import it, which interpreters have it and which
    manifests declare it. ``trust``: run project-shipped interpreters (None:
    ``trust_project_python``); untrusted ones are read statically.
    """
    root = Path(root).resolve()
    trusted = _trusted_flag(root, trust)
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner, trust=trusted)
    ok_probes = [item["probe"] for item in probed if item["probe"].get("ok")]
    if model is None:
        model = ImportModel(root, blobs=blob_ids(root) if cache_mode != "off" else None,
                            cache=ImportCache.for_root(root, cache_mode))
    model.set_provided(provided_names(ok_probes))
    files = expand_paths(root, paths, model.files)
    need = model.requirements(files)
    model.save_cache()
    required = {k: list(v) for k, v in need["required"].items()}
    for module in extra_modules:
        top = str(module).split(".", 1)[0]
        if top and top not in STDLIB:
            required.setdefault(top, []).append("<check command>")
    optional = {k: v for k, v in need["optional"].items() if k not in required}
    conditional = {k: v for k, v in need["conditional"].items()
                   if k not in required}
    stdlib = need["stdlib"]
    ranked = []
    for order, item in enumerate(probed):
        pr = item["probe"]
        entry = _summary(item)
        if pr.get("ok"):
            entry.update(assess(pr, required, optional, stdlib, conditional))
            entry["complete"] = not entry["missing"]
        else:
            entry.update(missing=None, missing_optional=None, complete=False)
        rank = ((0, len(entry["missing"]), len(entry["missing_optional"]), order)
                if pr.get("ok") else (1, 0, 0, order))
        ranked.append((rank, entry))
    ranked.sort(key=lambda pair: pair[0])
    interpreters = [entry for _rank, entry in ranked]
    declared = declared_dependencies(root, model.files)
    imports: dict[str, Any] = {}
    for kind, table in (("required", required), ("optional", optional)):
        for module, where in sorted(table.items()):
            imports[module] = {
                "required": kind == "required",
                "files": where[:MAX_LISTED_FILES],
                "have": [item["path"] for item in probed
                         if provides(item["probe"], module)],
                "declared": declarations(module, declared, ok_probes),
            }
            if module in conditional:
                imports[module]["conditional"] = True
    best = next((e for e in interpreters if e.get("complete")), None)
    return {"schema": SCHEMA, "root": str(root), "paths": files,
            "closure_files": len(need["closure"]),
            "closure_truncated": need["truncated"],
            "required": sorted(required), "optional": sorted(optional),
            "conditional": sorted(conditional),
            "best": best, "interpreters": interpreters, "imports": imports,
            "manifests": declared["manifests"],
            "unread": sorted(model.unread),
            "trusted": trusted,
            "population": population(probed)}


def _trusted_flag(root: Path, trust: bool | None) -> bool:
    return trust_project_python(root) if trust is None else bool(trust)


def who_has(root: str | Path, module: str, *, cache_mode: str = "rw",
            timeout: float = PROBE_TIMEOUT_S,
            candidates: Sequence[Mapping[str, Any]] | None = None,
            runner: Callable[..., dict[str, Any]] | None = None,
            trust: bool | None = None) -> list[dict[str, Any]]:
    """The found interpreters that can import ``module`` (its top-level
    name), in discovery order: [{"interpreter", "version", "probe"}]. The
    interface the failure classifier (W21) calls."""
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner, trust=trust)
    return [{"interpreter": item["path"], "version": item["probe"]["version"],
             "probe": probe_kind(item)}
            for item in probed if provides(item["probe"], module)]


# In-process memo of `module_environment`: (root, top, trusted, cache_mode)
# -> (monotonic time, stamp spec, stamp, answer). Discovery runs git and
# reads manifests, so a daemon that classifies every captured pytest asks
# at most once per MODULE_ENV_TTL_S for the same module, and only while
# the stamp holds (`_module_env_stamp`: what `pip install` or a manifest
# edit changes).
MODULE_ENV_TTL_S = 300.0
MODULE_ENV_MAX = 64
_MODULE_ENV_MEMO: dict[tuple[str, str, bool, str],
                       tuple[float, dict[str, Any], Any, dict[str, Any]]] = {}


def _module_env_stamp(spec: Mapping[str, Any]) -> list[Any]:
    """Stats a memoized `module_environment` answer depends on: each
    probed interpreter's signature (realpath, mtimes of the binary, its
    `pyvenv.cfg` and the site-packages directories it scanned), the mtimes
    of the manifests read, and of the root directory (a new top-level
    manifest or venv). Stats only, no subprocess."""
    return [[_signature(path, dirs) for path, dirs in spec["interpreters"]],
            [_stat_ns(p) for p in spec["files"]]]


def module_environment(root: str | Path, module: str, *, cache_mode: str = "rw",
                       timeout: float = PROBE_TIMEOUT_S,
                       candidates: Sequence[Mapping[str, Any]] | None = None,
                       runner: Callable[..., dict[str, Any]] | None = None,
                       trust: bool | None = None) -> dict[str, Any]:
    """Where ``module`` (its top-level name) is available and declared, for
    an `environment` failure (W21 calls this): {"module", "available_in":
    ``who_has``'s items, "declared_in": [{"file", "optional", "group"}]
    (the manifests that declare it; [] when none does), "trusted"}.

    Trust follows ``trust_project_python`` unless ``trust`` is given:
    untrusted project-shipped interpreters are read statically, never run.
    Answers are memoized in-process per root, module, trust and cache mode
    (not with ``candidates`` or ``runner``) for at most ``MODULE_ENV_TTL_S``,
    and only while ``_module_env_stamp`` is unchanged: installing or
    removing a distribution, or editing a manifest, recomputes it."""
    root = Path(root).resolve()
    top = str(module).split(".", 1)[0]
    trusted = _trusted_flag(root, trust)
    key = (str(root), top, trusted, cache_mode)
    memo = candidates is None and runner is None and cache_mode != "off"
    now = time.monotonic()
    if memo:
        hit = _MODULE_ENV_MEMO.get(key)
        if hit is not None and now - hit[0] < MODULE_ENV_TTL_S \
                and _module_env_stamp(hit[1]) == hit[2]:
            return {**hit[3], "cached": True}
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner, trust=trusted)
    ok_probes = [item["probe"] for item in probed if item["probe"].get("ok")]
    available = [{"interpreter": item["path"], "version": item["probe"]["version"],
                  "probe": probe_kind(item)}
                 for item in probed if provides(item["probe"], top)]
    try:
        declared = declared_dependencies(root)
    except (OSError, ValueError):
        declared = None
    declared_in = None if declared is None else [
        {k: e[k] for k in ("file", "optional", "group")}
        for e in declarations(top, declared, ok_probes)]
    answer = {"module": top, "available_in": available,
              "declared_in": declared_in, "trusted": trusted}
    if memo:
        read = (declared or {}).get("manifests", []) + (declared or {}).get("unparsed", [])
        spec = {"interpreters": [(str(item["path"]),
                                  list(item["probe"].get("scanned") or []))
                                 for item in probed],
                "files": [str(root)] + [str(root / rel) for rel in read]}
        if len(_MODULE_ENV_MEMO) >= MODULE_ENV_MAX:
            _MODULE_ENV_MEMO.pop(min(_MODULE_ENV_MEMO, key=lambda k: _MODULE_ENV_MEMO[k][0]))
        _MODULE_ENV_MEMO[key] = (now, spec, _module_env_stamp(spec), answer)
    return {**answer, "cached": False}


def environments(root: str | Path, *, cache_mode: str = "rw",
                 timeout: float = PROBE_TIMEOUT_S,
                 candidates: Sequence[Mapping[str, Any]] | None = None,
                 runner: Callable[..., dict[str, Any]] | None = None,
                 trust: bool | None = None) -> dict[str, Any]:
    """Every candidate interpreter with its probe summary, and the Python
    dependencies the manifests declare."""
    root = Path(root).resolve()
    trusted = _trusted_flag(root, trust)
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner, trust=trusted)
    declared = declared_dependencies(root)
    deps = [{"name": entries[0]["name"],
             "declared": [{k: e[k] for k in ("file", "optional", "group")}
                          for e in entries]}
            for _norm, entries in declared["by_name"].items()]
    src = source_root(root)
    return {"schema": SCHEMA, "root": str(root),
            "source_root": str(src) if src is not None else None,
            "trusted": trusted,
            "interpreters": [_summary(item) for item in probed],
            "manifests": declared["manifests"],
            "unparsed_manifests": declared["unparsed"],
            "dependencies": deps}


# --------------------------------------------------------------------------- #
# text output
# --------------------------------------------------------------------------- #


def _interp_line(entry: Mapping[str, Any], root: Path) -> str:
    path = entry["path"]
    try:
        shown = Path(path).relative_to(root).as_posix()
    except ValueError:
        shown = path
    if not entry.get("ok"):
        return f"{shown}  [{entry['source']}]  probe failed: {entry.get('error')}"
    kind = "venv" if entry.get("is_venv") else "base"
    read = "  read statically, not run" if entry.get("probe") == "static" else ""
    return (f"{shown}  Python {entry.get('version') or '?'}  {kind}  "
            f"{entry.get('distributions')} dists{read}  [{entry['source']}: "
            f"{entry['evidence']}]")


def _trust_note(doc: Mapping[str, Any]) -> list[str]:
    if doc.get("trusted") or not any(e.get("project") and not is_self(e["path"])
                                     for e in doc["interpreters"]):
        return []
    return [f"project-shipped interpreters were not run (set {TRUST_ENV}=1 or "
            "[environments] trust_project_python = true in the user config to "
            "run them); for those read statically, " + STATIC_SEES]


def format_environments(doc: Mapping[str, Any]) -> str:
    root = Path(doc["root"])
    lines = [f"root: {root}"]
    if doc.get("source_root"):
        lines.append(f"source repository: {doc['source_root']}")
    lines.append("interpreters (in order of evidence):")
    lines += ["  " + _interp_line(e, root) for e in doc["interpreters"]]
    lines += _trust_note(doc)
    lines.append("declared Python dependencies: "
                 + (", ".join(doc["manifests"]) or "no manifest"))
    for dep in doc["dependencies"]:
        where = ", ".join(e["file"] + (f" [{e['group']}]" if e["group"] else "")
                          for e in dep["declared"])
        lines.append(f"  {dep['name']}: {where}")
    return "\n".join(lines) + "\n"


def format_env_for(doc: Mapping[str, Any]) -> str:
    root = Path(doc["root"])
    lines = [f"root: {root}", "for: " + ", ".join(doc["paths"])]
    lines.append(f"files read: {doc['closure_files']}"
                 + (" (truncated)" if doc["closure_truncated"] else ""))
    best = doc.get("best")
    lines.append("best: " + (_interp_line(best, root) if best else
                             "none: no found interpreter has every required import"))
    lines.append("interpreters (best first):")
    for entry in doc["interpreters"]:
        line = "  " + _interp_line(entry, root)
        if entry.get("ok"):
            line += ("  ok" if entry["complete"] else
                     "  missing: " + ", ".join(entry["missing"]))
            if entry["missing_optional"]:
                line += "  (optional missing: " + ", ".join(entry["missing_optional"]) + ")"
        lines.append(line)
    lines += _trust_note(doc)
    lines.append("imports:")
    for module, info in doc["imports"].items():
        declared = ", ".join(
            d["file"] + (f" [{d['group']}, optional]" if d["optional"] else "")
            for d in info["declared"]) or "declared nowhere"
        need = ("required" if info["required"] else
                "conditional" if info.get("conditional") else "optional")
        lines.append(f"  {module} ({need}): "
                     f"{len(info['have'])} interpreter(s) have it; {declared}; "
                     f"imported by {', '.join(info['files'][:3])}")
    return "\n".join(lines) + "\n"
