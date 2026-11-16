"""A project's Python environments and what each provides (W20).

Finds the interpreters a project may run under, asks each what it has, and
answers which of them can import a set of files (`env_for`) or one module
(`who_has`). Project code is never imported or run: an interpreter is only
asked, with `python -I -c <probe>` from a neutral directory, for its version,
`sys.prefix`, `sys.base_prefix` and the top-level import names its
installed distributions and path entries provide. Imports are read from the
source with `ast`.

Candidates, in order of evidence (`discover`):

1. `$VIRTUAL_ENV`;
2. `.venv`, `venv` and `.venv-*` in the root;
3. the same in the source repository's root, when the root is a git
   worktree or a local clone without its own;
4. the environment of the lock file's tool (`uv.lock`: `.venv` or
   `$UV_PROJECT_ENVIRONMENT`; `poetry.lock`: `poetry env info --path`;
   `pdm.lock`: `pdm info --python`; each tool only when it is installed);
5. interpreters named in the project's command docs (`index.command_docs`);
6. `python` and `python3` on `PATH`;
7. the interpreter ozagia runs under.

Probes are bounded (`PROBE_TIMEOUT_S`, `PROBE_MAX_BYTES`) and cached in
`<state dir>/environments/probes.json`, keyed on the interpreter path and
validated against the mtimes of the interpreter and of every directory the
probe scanned (installing or removing a distribution changes its
`site-packages` mtime). Within one process probes are also memoized.

Import kinds (`file_imports`): `module` (runs when the file is imported),
`lazy` (inside a function or an `if __name__ == "__main__"` block) and
`guarded` (inside `try` with an `ImportError` handler, under
`if TYPE_CHECKING`, or `pytest.importorskip`). For the files asked about,
`module` and `lazy` imports are required. Their first-party imports are
followed (the transitive closure, plus parent packages and, for tests, the
`conftest.py` files pytest loads); there only `module` imports are required.
`guarded` imports and the closure's `lazy` ones are optional: they decide
the order of interpreters that have every required import, not whether one
qualifies.
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
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.core import manifests as MF

SCHEMA = "ozagia.environments.v1"
CACHE_SCHEMA = 1
CACHE_DIR = "environments"
CACHE_NAME = "probes.json"
CACHE_MODES = ("rw", "read", "off")

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

KIND_RANK = {"module": 0, "lazy": 1, "guarded": 2}
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
    return {k: v for k, v in os.environ.items()
            if not k.startswith("PYTHON") and k != "VIRTUAL_ENV"}


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


def _signature(python: str, dirs: Iterable[str]) -> dict[str, Any]:
    real = os.path.realpath(python)
    return {"real": real, "mtime_ns": _stat_ns(real),
            "dirs": {d: _stat_ns(d) for d in dirs}}


def _valid(entry: Mapping[str, Any], python: str) -> bool:
    sig = entry.get("signature")
    if not isinstance(sig, dict) or not isinstance(sig.get("dirs"), dict):
        return False
    return sig == _signature(python, sig["dirs"])


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

    def get(self, python: str) -> dict[str, Any] | None:
        if self.mode == "off":
            return None
        entry = self._load().get(python)
        if isinstance(entry, dict) and _valid(entry, python):
            return entry.get("probe")
        return None

    def put(self, python: str, probe: Mapping[str, Any]) -> None:
        if self.mode != "rw" or not probe.get("ok"):
            return
        self._load()[python] = {"signature": _signature(python, probe.get("scanned") or []),
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


def probe(python: str, *, cache: ProbeCache | None = None,
          timeout: float = PROBE_TIMEOUT_S,
          runner: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """What ``python`` provides: ``ok``, ``version``, ``prefix``,
    ``base_prefix``, ``is_venv``, ``modules`` (top-level import name ->
    distribution names; [] for a name found on a path entry), ``stdlib``,
    ``scanned`` and ``cached`` (``memo``, ``file`` or false). A failed probe
    is not cached."""
    python = os.path.abspath(python)
    use_cache = cache is None or cache.mode != "off"
    memo = _MEMO.get(python) if use_cache else None
    if memo is not None and _valid(memo, python):
        return {**memo["probe"], "cached": "memo"}
    if cache is not None:
        hit = cache.get(python)
        if hit is not None:
            _MEMO[python] = {"signature": _signature(python, hit.get("scanned") or []),
                             "probe": hit}
            return {**hit, "cached": "file"}
    raw = (runner or run_probe)(python, timeout=timeout)
    result = _normalize_probe(python, raw)
    if result["ok"]:
        if use_cache:
            _MEMO[python] = {"signature": _signature(python, result["scanned"]),
                             "probe": result}
        if cache is not None:
            cache.put(python, result)
    return {**result, "cached": False}


def _normalize_probe(python: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if not raw.get("ok"):
        return {"ok": False, "path": python,
                "error": str(raw.get("error") or "probe failed")}
    modules: dict[str, list[str]] = {}
    for name, dists in (raw.get("modules") or {}).items():
        if isinstance(name, str) and name.isidentifier():
            modules[name] = sorted({str(d) for d in (dists or [])
                                    if isinstance(d, str)})
    prefix = str(raw.get("prefix") or "")
    base = str(raw.get("base_prefix") or prefix)
    return {
        "ok": True, "path": python,
        "executable": str(raw.get("executable") or python),
        "version": str(raw.get("version") or ""),
        "prefix": prefix, "base_prefix": base, "is_venv": prefix != base,
        "stdlib": sorted({str(s) for s in raw.get("stdlib") or []}),
        "scanned": [str(s) for s in raw.get("scanned") or []],
        "distributions": int(raw.get("distributions") or 0),
        "modules": dict(sorted(modules.items())),
    }


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


def _lock_candidates(root: Path, *, which: Callable[[str], str | None],
                     tool: Callable[[Sequence[str], Path], str | None]
                     ) -> list[tuple[Path, str, str]]:
    out: list[tuple[Path, str, str]] = []
    if (root / "uv.lock").is_file():
        env = os.environ.get("UV_PROJECT_ENVIRONMENT")
        venv = (Path(env) if env and os.path.isabs(env) else root / (env or ".venv"))
        py = venv_python(venv)
        if py is not None:
            out.append((py, "lock", f"uv.lock: {venv}"))
    if (root / "poetry.lock").is_file() and which("poetry"):
        answer = tool(["poetry", "env", "info", "--path"], root)
        py = venv_python(Path(answer)) if answer else None
        if py is not None:
            out.append((py, "lock", "poetry.lock: poetry env info --path"))
    if (root / "pdm.lock").is_file():
        py = venv_python(root / ".venv")
        if py is None and which("pdm"):
            answer = tool(["pdm", "info", "--python"], root)
            py = Path(answer) if answer and _executable(Path(answer)) else None
        if py is not None:
            out.append((py, "lock", "pdm.lock"))
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


def discover(root: str | Path, *, which: Callable[[str], str | None] = shutil.which,
             tool: Callable[[Sequence[str], Path], str | None] = _run_tool,
             command_docs: Iterable[str] | None = None,
             include_self: bool = True) -> list[dict[str, Any]]:
    """Candidate interpreters for ``root``, in order of evidence. Each is
    {"path", "source", "evidence", "also"} (``also``: other evidence for the
    same interpreter)."""
    root = Path(root).resolve()
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
        src = source_root(root)
        if src is not None:
            for d in _venv_dirs(src):
                py = venv_python(d)
                if py is not None:
                    found.append((py, "source_venv", f"{d.name} of the source "
                                                     f"repository {src}"))
    found += _lock_candidates(root, which=which, tool=tool)
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
            continue
        item = {"path": str(path), "source": source, "evidence": evidence,
                "also": []}
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


def file_imports(text: str) -> list[dict[str, Any]]:
    """The imports of Python source: [{"module", "names", "level", "kind",
    "line"}]. ``module`` is the dotted name after `import`/`from` (empty for
    `from . import x`), ``names`` the imported names of a `from` import and
    ``level`` its leading dots. A file that does not parse falls back to a
    line scan (every import ``module``)."""
    if "import" not in text:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _scan_imports(text)
    out: list[dict[str, Any]] = []

    def skip_call(node: ast.AST | None, kind: str) -> None:
        """`pytest.importorskip("x")` as a statement or an assigned value."""
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

    def block(nodes: Iterable[ast.AST], kind: str) -> None:
        for node in nodes:
            visit(node, kind)

    # Statements only: imports are statements, and expressions are not
    # walked (importorskip is looked for in expression statements and
    # assignments).
    def visit(node: ast.AST, kind: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append({"module": alias.name, "names": [], "level": 0,
                            "kind": kind, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            out.append({"module": node.module or "",
                        "names": [a.name for a in node.names if a.name != "*"],
                        "level": node.level or 0, "kind": kind,
                        "line": node.lineno})
        elif isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
            skip_call(node.value, kind)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            block(node.body, _worse(kind, "lazy"))
        elif isinstance(node, ast.ClassDef):
            block(node.body, kind)
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            guarded = any(_handler_names(h) & _IMPORT_ERRORS
                          for h in node.handlers)
            inner = _worse(kind, "guarded") if guarded else kind
            block(node.body, inner)
            for handler in node.handlers:
                block(handler.body, inner)
            block([*node.orelse, *node.finalbody], kind)
        elif isinstance(node, ast.If):
            if _is_type_checking(node.test):
                body_kind = _worse(kind, "guarded")
            elif _is_main_guard(node.test):
                body_kind = _worse(kind, "lazy")
            else:
                body_kind = kind
            block(node.body, body_kind)
            block(node.orelse, kind)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            block([*node.body, *node.orelse], kind)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            block(node.body, kind)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                block(case.body, kind)

    block(tree.body, "module")
    # A module-level `pytest.importorskip("x")` skips the whole file without
    # x, so every import of x in it is guarded.
    skipped = {str(r["module"]).split(".", 1)[0] for r in out
               if r.pop("skip", None) == "module"}
    for rec in out:
        if not rec["level"] and str(rec["module"]).split(".", 1)[0] in skipped:
            rec["kind"] = "guarded"
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
            if len(files) >= MAX_WALK_FILES:
                return files
    return files


class ImportModel:
    """Imports of a tree's Python files, first-party resolution and the
    closure of a set of files. ``read`` (rel -> text or None) defaults to
    reading under the root; files are parsed once."""

    def __init__(self, root: str | Path, files: Iterable[str] | None = None, *,
                 read: Callable[[str], str | None] | None = None,
                 import_roots: Iterable[str] | None = None) -> None:
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
        self._parsed: dict[str, list[dict[str, Any]]] = {}
        self._edges: dict[str, dict[str, Any]] = {}
        self.unread: set[str] = set()
        self._index_anywhere()

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
            self._index_anywhere()

    def imports(self, rel: str) -> list[dict[str, Any]]:
        if rel not in self._parsed:
            text = self._read(rel)
            if text is None:
                self.unread.add(rel)
            self._parsed[rel] = file_imports(text or "")
        return self._parsed[rel]

    def basedir(self, rel: str) -> str:
        """The first ancestor directory of ``rel`` without `__init__.py`:
        what pytest and `python script.py` put on sys.path."""
        d = PurePosixPath(rel).parent
        while d.as_posix() != "." and (d / "__init__.py").as_posix() in self.py:
            d = d.parent
        return "" if d.as_posix() == "." else d.as_posix()

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
        elsewhere = self.anywhere.get(dotted.split(".", 1)[0]) or set()
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
        import root, or a module or package of that name anywhere in it."""
        for base in self._bases(rel):
            path = self._join(base, top)
            if f"{path}.py" in self.py or path in self.dirs:
                return True
        return top in self.anywhere

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
        [files]}, "third_party": {kind: [tops]}, "stdlib": {kind: [tops]}}."""
        cached = self._edges.get(rel)
        if cached is not None:
            return cached
        fp: dict[str, list[str]] = {k: [] for k in KIND_RANK}
        tp: dict[str, list[str]] = {k: [] for k in KIND_RANK}
        std: dict[str, list[str]] = {k: [] for k in KIND_RANK}
        for rec in self.imports(rel):
            kind = rec["kind"]
            for dotted in self.resolve(rec, rel):
                top = dotted.split(".", 1)[0]
                if self.is_first_party(top, rel):
                    for f in self._first_party_files(dotted, rel):
                        if f != rel and f not in fp[kind]:
                            fp[kind].append(f)
                    continue
                if rec.get("level"):
                    continue
                bucket = std if top in STDLIB else tp
                if top not in bucket[kind]:
                    bucket[kind].append(top)
                break  # `from a import b`: one module, not one per name
        out = {"first_party": fp, "third_party": tp, "stdlib": std}
        self._edges[rel] = out
        return out

    def requirements(self, paths: Iterable[str], *, lazy_required: bool = True,
                     limit: int = MAX_CLOSURE_FILES) -> dict[str, Any]:
        """Third-party and standard-library modules ``paths`` need.

        The files asked about need their ``module`` imports and, with
        ``lazy_required``, their ``lazy`` ones; the first-party modules
        those import (and, for tests, the conftest.py files pytest loads)
        need their ``module`` imports. The rest is optional. Returns
        {"required": {top: [files]}, "optional": {top: [files]}, "stdlib":
        {top: [files]}, "closure": [files], "truncated": bool}.
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
        for top in list(optional):
            if top in required:
                del optional[top]
        return {"required": dict(sorted(required.items())),
                "optional": dict(sorted(optional.items())),
                "stdlib": dict(sorted(stdlib.items())),
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


def _probe_all(candidates: Sequence[Mapping[str, Any]], cache: ProbeCache | None,
               timeout: float,
               runner: Callable[..., dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for cand in candidates:
        result = probe(str(cand["path"]), cache=cache, timeout=timeout,
                       runner=runner)
        out.append({**dict(cand), "probe": result})
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
           "ok": bool(pr.get("ok")), "cached": pr.get("cached", False)}
    if pr.get("ok"):
        out.update(version=pr["version"], prefix=pr["prefix"],
                   is_venv=pr["is_venv"], distributions=pr["distributions"])
    else:
        out["error"] = pr.get("error")
    return out


def probe_candidates(root: str | Path, *, cache_mode: str = "rw",
                     timeout: float = PROBE_TIMEOUT_S,
                     candidates: Sequence[Mapping[str, Any]] | None = None,
                     runner: Callable[..., dict[str, Any]] | None = None
                     ) -> list[dict[str, Any]]:
    """``discover`` then ``probe`` each: candidates with a ``probe`` key."""
    root = Path(root).resolve()
    cands = list(candidates) if candidates is not None else discover(root)
    cache = ProbeCache.for_root(root, cache_mode)
    return _probe_all(cands, cache, timeout, runner)


def env_for(root: str | Path, paths: Iterable[str | Path], *,
            extra_modules: Iterable[str] = (), cache_mode: str = "rw",
            timeout: float = PROBE_TIMEOUT_S,
            candidates: Sequence[Mapping[str, Any]] | None = None,
            runner: Callable[..., dict[str, Any]] | None = None,
            model: ImportModel | None = None) -> dict[str, Any]:
    """The interpreters under which ``paths`` can run, best first.

    ``extra_modules`` are required too (e.g. `pytest` for a pytest check).
    An interpreter qualifies (``complete``) when it provides every required
    module; the ranking is: fewest missing required modules, then fewest
    missing optional ones, then the discovery order. ``best`` is the first
    complete interpreter or None. ``imports`` says, per module, whether it is
    required, which files import it, which interpreters have it and which
    manifests declare it.
    """
    root = Path(root).resolve()
    model = model or ImportModel(root)
    files = expand_paths(root, paths, model.files)
    need = model.requirements(files)
    required = {k: list(v) for k, v in need["required"].items()}
    for module in extra_modules:
        top = str(module).split(".", 1)[0]
        if top and top not in STDLIB:
            required.setdefault(top, []).append("<check command>")
    optional = {k: v for k, v in need["optional"].items() if k not in required}
    stdlib = need["stdlib"]
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner)
    ranked = []
    for order, item in enumerate(probed):
        pr = item["probe"]
        entry = _summary(item)
        if pr.get("ok"):
            entry["missing"] = [m for m in required if not provides(pr, m)]
            entry["missing"] += [m for m in stdlib if not provides(pr, m)
                                 and m not in entry["missing"]]
            entry["missing_optional"] = [m for m in optional
                                         if not provides(pr, m)]
            entry["complete"] = not entry["missing"]
        else:
            entry.update(missing=None, missing_optional=None, complete=False)
        rank = ((0, len(entry["missing"]), len(entry["missing_optional"]), order)
                if pr.get("ok") else (1, 0, 0, order))
        ranked.append((rank, entry))
    ranked.sort(key=lambda pair: pair[0])
    interpreters = [entry for _rank, entry in ranked]
    ok_probes = [item["probe"] for item in probed if item["probe"].get("ok")]
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
    best = next((e for e in interpreters if e.get("complete")), None)
    return {"schema": SCHEMA, "root": str(root), "paths": files,
            "closure_files": len(need["closure"]),
            "closure_truncated": need["truncated"],
            "required": sorted(required), "optional": sorted(optional),
            "best": best, "interpreters": interpreters, "imports": imports,
            "manifests": declared["manifests"],
            "unread": sorted(model.unread)}


def who_has(root: str | Path, module: str, *, cache_mode: str = "rw",
            timeout: float = PROBE_TIMEOUT_S,
            candidates: Sequence[Mapping[str, Any]] | None = None,
            runner: Callable[..., dict[str, Any]] | None = None
            ) -> list[dict[str, Any]]:
    """The found interpreters that can import ``module`` (its top-level
    name), in discovery order: [{"interpreter", "version"}]. The interface
    the failure classifier (W21) calls."""
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner)
    return [{"interpreter": item["path"], "version": item["probe"]["version"]}
            for item in probed if provides(item["probe"], module)]


def environments(root: str | Path, *, cache_mode: str = "rw",
                 timeout: float = PROBE_TIMEOUT_S,
                 candidates: Sequence[Mapping[str, Any]] | None = None,
                 runner: Callable[..., dict[str, Any]] | None = None
                 ) -> dict[str, Any]:
    """Every candidate interpreter with its probe summary, and the Python
    dependencies the manifests declare."""
    root = Path(root).resolve()
    probed = probe_candidates(root, cache_mode=cache_mode, timeout=timeout,
                              candidates=candidates, runner=runner)
    declared = declared_dependencies(root)
    deps = [{"name": entries[0]["name"],
             "declared": [{k: e[k] for k in ("file", "optional", "group")}
                          for e in entries]}
            for _norm, entries in declared["by_name"].items()]
    src = source_root(root)
    return {"schema": SCHEMA, "root": str(root),
            "source_root": str(src) if src is not None else None,
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
    return (f"{shown}  Python {entry.get('version')}  {kind}  "
            f"{entry.get('distributions')} dists  [{entry['source']}: "
            f"{entry['evidence']}]")


def format_environments(doc: Mapping[str, Any]) -> str:
    root = Path(doc["root"])
    lines = [f"root: {root}"]
    if doc.get("source_root"):
        lines.append(f"source repository: {doc['source_root']}")
    lines.append("interpreters (in order of evidence):")
    lines += ["  " + _interp_line(e, root) for e in doc["interpreters"]]
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
    lines.append("imports:")
    for module, info in doc["imports"].items():
        declared = ", ".join(
            d["file"] + (f" [{d['group']}, optional]" if d["optional"] else "")
            for d in info["declared"]) or "declared nowhere"
        lines.append(f"  {module} ({'required' if info['required'] else 'optional'}): "
                     f"{len(info['have'])} interpreter(s) have it; {declared}; "
                     f"imported by {', '.join(info['files'][:3])}")
    return "\n".join(lines) + "\n"
