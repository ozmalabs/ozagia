"""Static operations graph: compose files, Containerfiles, quadlets, units.

Source-only and bounded, like the other ``*_graph`` modules: it parses
docker/podman compose files, Containerfiles/Dockerfiles, podman quadlets and
systemd units into identity nodes and declared relationships. It never runs
podman, docker or systemctl and never reads live state.

Node keys (``ops:`` prefix, one key part, so ``ozagia.core.keys`` round-trips
them):

    ops:compose/<rel>                          compose project (one per file)
    ops:compose/<rel>#service/<name>           compose service
    ops:compose/<rel>#network/<name>           compose network
    ops:compose/<rel>#volume/<name>            compose named volume
    ops:containerfile/<rel>                    Containerfile/Dockerfile
    ops:containerfile/<rel>#stage/<name|index> build stage
    ops:quadlet/<rel>                          .container/.pod/.network/...
    ops:unit/<rel>                             .service/.timer/.socket/...
    ops:image/<ref>                            an image reference, shared

``<rel>`` is the repository-relative POSIX path. Image references are
normalised so the same image matches across formats: ``docker.io/``,
``index.docker.io/``, ``library/`` and ``localhost/`` prefixes are dropped and
a missing tag becomes ``:latest``.

Secrets: environment and build-argument *values* are never kept, only names.
Every retained string (paths, binaries, ports, image references) is bounded
and replaced with ``[REDACTED]`` when it matches a common secret shape.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

EXTRACTOR_VERSION = "ops-source-v1"
MAX_ITEMS = 64
MAX_VALUE_CHARS = 256
REDACTED = "[REDACTED]"

_COMPOSE_NAME = re.compile(
    r"(?:docker-|podman-)?compose(?:[.-][A-Za-z0-9_.-]+)?\.ya?ml\Z")
_CONTAINERFILE_NAME = re.compile(
    r"(?:(?:docker|container)file(?:\.[a-z0-9_.-]+)?"
    r"|[a-z0-9_.-]+\.(?:dockerfile|containerfile))\Z")
UNIT_SUFFIXES = (".service", ".timer", ".socket", ".target", ".path")
QUADLET_SUFFIXES = (".container", ".pod", ".network", ".volume", ".kube",
                    ".image", ".build")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_PATH = re.compile(r"[A-Za-z0-9_./@+-]+\Z")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}\Z")
_VAR_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-?+])[^}]*\}")

_SECRET_SHAPES = (
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}"
               r"|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
               r"|glpat-[A-Za-z0-9_-]{20,})"),
    re.compile(r"(?i)\bbearer\s+\S{8,}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key"
               r"|access[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


# --------------------------------------------------------------------------
# Safety helpers


def looks_secret(value: str) -> bool:
    """A string matches a common credential shape (token prefixes, AWS key
    IDs, bearer tokens, private keys, URL credentials, ``password=...``,
    JWTs, or a long mixed-case opaque run)."""
    if any(p.search(value) for p in _SECRET_SHAPES):
        return True
    for run in _OPAQUE.findall(value):
        if (any(c.isupper() for c in run) and any(c.islower() for c in run)
                and any(c.isdigit() for c in run)):
            return True
    return False


def clean(value: Any) -> str | None:
    """A bounded, single-line string safe to retain, ``[REDACTED]`` for a
    secret shape, ``None`` for anything else. ``${VAR:-default}`` keeps only
    the variable name."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_VALUE_CHARS or any(ord(c) < 32 for c in text):
        return None
    text = _VAR_DEFAULT.sub(lambda m: "${" + m.group(1) + "}", text)
    return REDACTED if looks_secret(text) else text


def env_name(value: Any) -> str | None:
    """The name part of ``NAME`` or ``NAME=value``; the value is dropped."""
    if not isinstance(value, str):
        return None
    name = value.strip().strip("\"'").split("=", 1)[0].strip()
    if not _ENV_NAME.fullmatch(name) or looks_secret(name):
        return None
    return name


def _names(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        name = env_name(value)
        if name and name not in out:
            out.append(name)
        if len(out) >= MAX_ITEMS:
            break
    return out


def _cleaned(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = clean(value)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_ITEMS:
            break
    return out


def normalize_image(ref: Any) -> str | None:
    """The shared identity of an image reference, or None (variables,
    unsafe text, secrets)."""
    if not isinstance(ref, str):
        return None
    text = ref.strip()
    if (not text or "$" in text or not _IMAGE.fullmatch(text)
            or looks_secret(text) or text.lower() == "scratch"):
        return None
    for prefix in ("docker.io/", "index.docker.io/", "localhost/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("library/"):
        text = text[len("library/"):]
    last = text.rsplit("/", 1)[-1]
    if "@" not in text and ":" not in last:
        text += ":latest"
    return text


def _safe_rel(rel: str) -> bool:
    return (bool(rel) and len(rel) <= 1024 and _SAFE_PATH.fullmatch(rel) is not None
            and ".." not in PurePosixPath(rel).parts and not rel.startswith("/"))


def _join(base: str, rel: str) -> str | None:
    """`rel` relative to the repository directory `base`, normalised; None
    when it leaves the repository or is not a plain path."""
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "~")) or "$" in rel:
        return None
    parts: list[str] = [] if base in ("", ".") else list(PurePosixPath(base).parts)
    for part in PurePosixPath(rel).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    out = "/".join(parts) or "."
    return out if out == "." or _safe_rel(out) else None


# --------------------------------------------------------------------------
# Graph records


@dataclass
class OpsNode:
    key: str
    kind: str
    name: str
    path: str
    line: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    confidence: str = "high"


@dataclass(frozen=True)
class OpsEdge:
    source: str
    target: str
    relation: str
    evidence: str


@dataclass(frozen=True)
class OpsRef:
    """A reference resolved across files: `target_type` is one of `unit`,
    `quadlet`, `containerfile`, `image`, `file`; `base` is the directory of
    the declaring file (for relative names and same-directory preference)."""
    source: str
    relation: str
    target_type: str
    target: str
    evidence: str
    base: str
    stage: str | None = None


@dataclass
class OpsGraph:
    nodes: list[OpsNode] = field(default_factory=list)
    edges: list[OpsEdge] = field(default_factory=list)
    refs: list[OpsRef] = field(default_factory=list)
    diagnostics: list[tuple[str, str]] = field(default_factory=list)
    # Resolved: (source node key, relation, repo-relative file path).
    file_links: list[tuple[str, str, str]] = field(default_factory=list)

    def extend(self, other: "OpsGraph") -> None:
        self.nodes += other.nodes
        self.edges += other.edges
        self.refs += other.refs
        self.diagnostics += other.diagnostics
        self.file_links += other.file_links

    def to_dict(self) -> dict[str, Any]:
        return {"extractor_version": EXTRACTOR_VERSION,
                "nodes": [{"key": n.key, "kind": n.kind, "name": n.name,
                           "path": n.path, "line": n.line,
                           "confidence": n.confidence, **n.attrs}
                          for n in self.nodes],
                "edges": [asdict(e) for e in self.edges],
                "file_links": [list(x) for x in self.file_links],
                "diagnostics": [list(d) for d in self.diagnostics]}


def ops_source_format(rel: str) -> str | None:
    """`compose`, `containerfile`, `quadlet` or `unit` for a path ozagia
    parses as an operations source, else None. Quadlet suffixes other than
    `.container` count only under a `containers/systemd` or `quadlet(s)`
    directory, since `.network`/`.volume`/`.image` are common elsewhere."""
    path = PurePosixPath(rel)
    name = path.name.lower()
    if _COMPOSE_NAME.fullmatch(name):
        return "compose"
    if _CONTAINERFILE_NAME.fullmatch(name):
        return "containerfile"
    suffix = path.suffix.lower()
    if suffix in UNIT_SUFFIXES and path.stem:
        return "unit"
    if suffix in QUADLET_SUFFIXES and path.stem:
        if suffix == ".container":
            return "quadlet"
        parents = [p.lower() for p in path.parent.parts]
        if any(p in ("quadlet", "quadlets") for p in parents) or any(
                a == "containers" and b == "systemd"
                for a, b in zip(parents, parents[1:])):
            return "quadlet"
    return None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Compose


def _scan_compose(text: str) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Without a YAML parser: service names under a top-level `services:`
    block, their lines and plain `image:`/`container_name:` scalars, plus
    the lines of the other top-level keys. Also used for lines with PyYAML."""
    services: dict[str, dict[str, Any]] = {}
    top: dict[str, int] = {}
    in_services = False
    child_indent: int | None = None
    current: str | None = None
    field_indent: int | None = None
    key_re = re.compile(r"""^(\s*)(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+))\s*:(?:\s+(.*?))?\s*$""")
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue
        m = key_re.match(raw.split(" #", 1)[0].rstrip())
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_services = bool(m) and (m.group(4) or m.group(2) or m.group(3)) == "services"
            if m:
                top.setdefault(m.group(2) or m.group(3) or m.group(4), lineno)
            child_indent = current = field_indent = None
            continue
        if not in_services or not m:
            continue
        name = m.group(2) or m.group(3) or m.group(4)
        if child_indent is None:
            child_indent = indent
        if indent == child_indent:
            current = name
            field_indent = None
            services.setdefault(name, {"line": lineno})
            continue
        if current is None or indent < child_indent:
            continue
        if field_indent is None:
            field_indent = indent
        if indent == field_indent and name in ("image", "container_name"):
            value = (m.group(5) or "").strip().strip("\"'")
            if value:
                services[current].setdefault(name, value)
    return services, top


def _load_yaml(text: str) -> Any:
    """`(value, error)`: PyYAML safe load, or an error message."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None, "YAML parser unavailable; install the iac-yaml extra"
    try:
        return yaml.safe_load(text), None
    except Exception:  # noqa: BLE001 - any parser error is a diagnostic
        return None, "invalid or unsupported YAML"


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value[:MAX_ITEMS]
    if value is None:
        return []
    return [value]


def _port_text(item: Any) -> str | None:
    if isinstance(item, dict):
        target = item.get("target")
        published = item.get("published")
        host = item.get("host_ip")
        proto = item.get("protocol")
        parts = [str(x) for x in (host, published, target) if x not in (None, "")]
        text = ":".join(parts)
        if proto:
            text += f"/{proto}"
        return clean(text)
    return clean(item)


def _volume_entry(item: Any) -> tuple[str | None, str | None, str | None]:
    """(text to retain, named volume, bind source) for one service volume."""
    if isinstance(item, dict):
        source = item.get("source")
        target = item.get("target")
        kind = item.get("type") or "volume"
        source = source if isinstance(source, str) else None
        text = clean(":".join(str(x) for x in (source, target) if x))
        if kind == "volume" and source:
            return text, source, None
        if kind == "bind" and source:
            return text, None, source
        return text, None, None
    if not isinstance(item, str):
        return None, None, None
    source = item.split(":", 1)[0] if ":" in item else ""
    text = clean(item)
    if not source:
        return text, None, None
    if source.startswith((".", "/", "~", "$")):
        return text, None, source
    return text, source, None


def _default_project(rel: str) -> str | None:
    """Compose's default project name: the compose file's directory name,
    lowercased, with characters other than `a-z0-9_-` removed."""
    parent = PurePosixPath(rel).parent.name
    name = re.sub(r"[^a-z0-9_-]", "", parent.lower())
    return name or None


def parse_compose(text: str, rel: str) -> OpsGraph:
    graph = OpsGraph()
    base = PurePosixPath(rel).parent.as_posix()
    digest = _digest(text)
    project_key = f"ops:compose/{rel}"
    scanned, top_lines = _scan_compose(text)
    data, error = _load_yaml(text)
    if error is not None:
        graph.diagnostics.append((rel, f"compose: {error}"))
    project = OpsNode(project_key, "compose_project",
                      PurePosixPath(rel).name, rel, 1,
                      {"source_digest": digest})
    graph.nodes.append(project)
    if not isinstance(data, dict):
        if data is not None:
            graph.diagnostics.append((rel, "compose: top level is not a mapping"))
        # Partial: names from the line scan only.
        project.confidence = "partial"
        for name, info in list(scanned.items())[:MAX_ITEMS * 4]:
            if not _NAME.fullmatch(name):
                continue
            key = f"{project_key}#service/{name}"
            attrs: dict[str, Any] = {"source_digest": digest}
            if _default_project(rel):
                attrs["project"] = _default_project(rel)
            image = normalize_image(info.get("image"))
            if image:
                attrs["image"] = image
                graph.refs.append(OpsRef(key, "uses_image", "image", image,
                                         "services.<name>.image", base))
            container = clean(info.get("container_name"))
            if container:
                attrs["container_name"] = container
            graph.nodes.append(OpsNode(key, "compose_service", name, rel,
                                       info.get("line"), attrs, "partial"))
            graph.edges.append(OpsEdge(project_key, key, "has_service",
                                       "services"))
        return graph
    project_name = clean(data.get("name")) or _default_project(rel)
    if project_name:
        project.attrs["project"] = project_name
    services = data.get("services") if isinstance(data.get("services"), dict) else {}
    networks = data.get("networks") if isinstance(data.get("networks"), dict) else {}
    volumes = data.get("volumes") if isinstance(data.get("volumes"), dict) else {}
    keys_seen: set[str] = set()

    def add_resource(kind: str, name: Any, declared: bool) -> str | None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return None
        key = f"{project_key}#{kind}/{name}"
        if key not in keys_seen:
            keys_seen.add(key)
            graph.nodes.append(OpsNode(
                key, f"compose_{kind}", name, rel, top_lines.get(kind + "s"),
                {"source_digest": digest, "declared": declared}))
            graph.edges.append(OpsEdge(project_key, key, f"has_{kind}",
                                       f"{kind}s"))
        return key

    for name in list(networks)[:MAX_ITEMS]:
        add_resource("network", name, True)
    for name in list(volumes)[:MAX_ITEMS]:
        add_resource("volume", name, True)
    service_keys: dict[str, str] = {}
    for name in list(services)[:MAX_ITEMS * 4]:
        if isinstance(name, str) and _NAME.fullmatch(name):
            service_keys[name] = f"{project_key}#service/{name}"
    for name, key in service_keys.items():
        spec = services.get(name)
        spec = spec if isinstance(spec, dict) else {}
        attrs: dict[str, Any] = {"source_digest": digest}
        if project_name:
            attrs["project"] = project_name
        node = OpsNode(key, "compose_service", name, rel,
                       (scanned.get(name) or {}).get("line"), attrs)
        graph.nodes.append(node)
        graph.edges.append(OpsEdge(project_key, key, "has_service", "services"))
        image = normalize_image(spec.get("image"))
        if spec.get("image") is not None and image is None:
            graph.diagnostics.append((rel, f"compose service {name}: image not resolved"))
        if image:
            attrs["image"] = image
            graph.refs.append(OpsRef(key, "uses_image", "image", image,
                                     "services.<name>.image", base))
        container = clean(spec.get("container_name"))
        if container:
            attrs["container_name"] = container
        build = spec.get("build")
        if isinstance(build, (str, dict)):
            context = build if isinstance(build, str) else build.get("context", ".")
            dockerfile = None if isinstance(build, str) else build.get("dockerfile")
            target = None if isinstance(build, str) else build.get("target")
            ctx_rel = _join(base, context if isinstance(context, str) else ".")
            if ctx_rel is None:
                graph.diagnostics.append((rel, f"compose service {name}: build context outside the repository or not a path"))
            else:
                attrs["build_context"] = ctx_rel
                file_rel = _join(ctx_rel, dockerfile) if isinstance(dockerfile, str) else None
                graph.refs.append(OpsRef(
                    key, "builds_from", "containerfile",
                    file_rel or ctx_rel, "services.<name>.build", base,
                    stage=target if isinstance(target, str) else None))
                if image:
                    graph.refs.append(OpsRef(
                        f"ops:image/{image}", "built_from", "containerfile",
                        file_rel or ctx_rel, "services.<name>.build", base))
        ports = _cleaned(_port_text(p) for p in _list(spec.get("ports")))
        if ports:
            attrs["ports"] = ports
        expose = _cleaned(_list(spec.get("expose")))
        if expose:
            attrs["expose"] = expose
        vol_texts: list[str] = []
        for item in _list(spec.get("volumes")):
            text_v, named, bind = _volume_entry(item)
            if text_v and text_v not in vol_texts:
                vol_texts.append(text_v)
            if named:
                vkey = add_resource("volume", named, named in volumes)
                if vkey:
                    graph.edges.append(OpsEdge(key, vkey, "mounts_volume",
                                               "services.<name>.volumes"))
            elif bind:
                bind_rel = _join(base, bind)
                if bind_rel and bind_rel != ".":
                    graph.refs.append(OpsRef(key, "mounts", "file", bind_rel,
                                             "services.<name>.volumes", "."))
        if vol_texts:
            attrs["volumes"] = vol_texts
        nets = spec.get("networks")
        net_names = list(nets)[:MAX_ITEMS] if isinstance(nets, dict) else _list(nets)
        attached: list[str] = []
        for net in net_names:
            nkey = add_resource("network", net, net in networks)
            if nkey:
                attached.append(net)
                graph.edges.append(OpsEdge(key, nkey, "uses_network",
                                           "services.<name>.networks"))
        if attached:
            attrs["networks"] = attached
        deps = spec.get("depends_on")
        dep_names = list(deps)[:MAX_ITEMS] if isinstance(deps, dict) else _list(deps)
        depends: list[str] = []
        for dep in dep_names:
            if isinstance(dep, str) and dep in service_keys:
                depends.append(dep)
                graph.edges.append(OpsEdge(key, service_keys[dep], "depends_on",
                                           "services.<name>.depends_on"))
            elif isinstance(dep, str):
                graph.diagnostics.append((rel, f"compose service {name}: depends_on target not declared"))
        if depends:
            attrs["depends_on"] = depends
        env_files: list[str] = []
        for item in _list(spec.get("env_file")):
            path = item.get("path") if isinstance(item, dict) else item
            env_rel = _join(base, path) if isinstance(path, str) else None
            if env_rel and env_rel not in env_files:
                env_files.append(env_rel)
                graph.refs.append(OpsRef(key, "env_file", "file", env_rel,
                                         "services.<name>.env_file", "."))
        if env_files:
            attrs["env_files"] = _cleaned(env_files)
        env = spec.get("environment")
        names = _names(env.keys() if isinstance(env, dict) else _list(env))
        if names:
            attrs["env"] = names
        profiles = _cleaned(_list(spec.get("profiles")))
        if profiles:
            attrs["profiles"] = profiles
        entry = _first_word(spec.get("entrypoint"))
        if entry:
            attrs["entrypoint"] = entry
    return graph


def _first_word(value: Any) -> str | None:
    """The program of a command (exec list or shell string), never its
    arguments."""
    if isinstance(value, list):
        return clean(value[0]) if value and isinstance(value[0], str) else None
    if isinstance(value, str):
        try:
            words = shlex.split(value)
        except ValueError:
            words = value.split()
        return clean(words[0]) if words else None
    return None


# --------------------------------------------------------------------------
# Containerfile


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Instructions with continuations joined, comments and heredoc bodies
    dropped: `(first line number, text)`."""
    escape = "\\"
    lines = text.splitlines()
    for raw in lines[:8]:
        m = re.match(r"^\s*#\s*escape\s*=\s*(\S)\s*$", raw, re.I)
        if m:
            escape = m.group(1)
            break
        if not raw.strip().startswith("#"):
            break
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    heredoc: str | None = None
    for lineno, raw in enumerate(lines, 1):
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        stripped = raw.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if buf and stripped.startswith("#"):
            continue
        if not buf:
            start = lineno
        if stripped.endswith(escape):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        joined = " ".join(buf)
        buf = []
        out.append((start, joined))
        m = re.search(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", joined)
        if m and joined.split(None, 1)[0].upper() in ("RUN", "COPY", "ADD"):
            heredoc = m.group(1)
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _words(text: str) -> list[str]:
    try:
        return shlex.split(text, comments=False, posix=True)
    except ValueError:
        return text.split()


def _exec_program(args: str) -> str | None:
    args = args.strip()
    if args.startswith("["):
        try:
            value = json.loads(args)
        except ValueError:
            value = None
        if isinstance(value, list):
            return _first_word(value)
    return _first_word(args)


def parse_containerfile(text: str, rel: str) -> OpsGraph:
    graph = OpsGraph()
    digest = _digest(text)
    file_key = f"ops:containerfile/{rel}"
    root = OpsNode(file_key, "containerfile", PurePosixPath(rel).name, rel, 1,
                   {"source_digest": digest})
    graph.nodes.append(root)
    base = PurePosixPath(rel).parent.as_posix()
    stages: list[OpsNode] = []
    by_name: dict[str, str] = {}
    pre_args: list[str] = []
    current: OpsNode | None = None

    def stage_ref(ref: str) -> str | None:
        if ref.lower() in by_name:
            return by_name[ref.lower()]
        if ref.isdigit() and int(ref) < len(stages):
            return stages[int(ref)].key
        return None

    def add_list(node: OpsNode, field_name: str, values: Iterable[str]) -> None:
        bucket = node.attrs.setdefault(field_name, [])
        for value in values:
            if value and value not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(value)

    for lineno, line in _logical_lines(text)[:4000]:
        head, _, args = line.partition(" ")
        op = head.upper()
        if op == "FROM":
            words = [w for w in _words(args) if not w.startswith("--")]
            if not words:
                continue
            image_ref = words[0]
            alias = words[2] if len(words) >= 3 and words[1].lower() == "as" else None
            index = len(stages)
            label = alias if alias and _NAME.fullmatch(alias) else str(index)
            key = f"{file_key}#stage/{label}"
            current = OpsNode(key, "build_stage", label, rel, lineno,
                              {"source_digest": digest, "index": index})
            stages.append(current)
            graph.nodes.append(current)
            graph.edges.append(OpsEdge(file_key, key, "has_stage", "FROM"))
            parent = stage_ref(image_ref)
            if parent:
                current.attrs["from_stage"] = parent.rsplit("/", 1)[-1]
                graph.edges.append(OpsEdge(key, parent, "from_stage", "FROM"))
            else:
                image = normalize_image(image_ref)
                if image:
                    current.attrs["base_image"] = image
                    graph.refs.append(OpsRef(key, "base_image", "image", image,
                                             "FROM", base))
                elif image_ref.lower() != "scratch":
                    graph.diagnostics.append((rel, f"Containerfile line {lineno}: FROM image not resolved"))
            if alias and _NAME.fullmatch(alias):
                by_name[alias.lower()] = key
            continue
        if op == "ARG" and current is None:
            pre_args.extend(n for n in _names(_words(args)) if n not in pre_args)
            continue
        if current is None:
            continue
        if op in ("COPY", "ADD"):
            for word in _words(args):
                if word.startswith("--from="):
                    ref = word.split("=", 1)[1]
                    target = stage_ref(ref)
                    if target:
                        graph.edges.append(OpsEdge(current.key, target,
                                                   "copies_from", f"{op} --from"))
                    else:
                        image = normalize_image(ref)
                        if image:
                            graph.refs.append(OpsRef(current.key, "copies_from",
                                                     "image", image,
                                                     f"{op} --from", base))
        elif op == "RUN":
            for m in re.finditer(r"--mount=\S*?\bfrom=([^,\s]+)", args):
                target = stage_ref(m.group(1))
                if target:
                    graph.edges.append(OpsEdge(current.key, target,
                                               "mounts_from", "RUN --mount"))
        elif op == "EXPOSE":
            add_list(current, "expose", _cleaned(_words(args)))
        elif op == "ENTRYPOINT":
            program = _exec_program(args)
            if program:
                current.attrs["entrypoint"] = program
        elif op == "CMD":
            program = _exec_program(args)
            if program:
                current.attrs["cmd"] = program
        elif op == "ARG":
            add_list(current, "args", _names(_words(args)))
        elif op == "ENV":
            words = _words(args)
            if words and "=" not in words[0]:
                words = words[:1]       # legacy `ENV NAME value`
            add_list(current, "env", _names(words))
        elif op == "USER":
            user = clean(_words(args)[0] if _words(args) else None)
            if user:
                current.attrs["user"] = user
    if pre_args:
        root.attrs["args"] = pre_args[:MAX_ITEMS]
    if stages:
        final = stages[-1]
        root.attrs["final_stage"] = final.name
        for field_name in ("expose", "entrypoint", "cmd"):
            value = next((s.attrs[field_name] for s in reversed(stages)
                          if field_name in s.attrs), None)
            if value:
                root.attrs[field_name] = value
    else:
        graph.diagnostics.append((rel, "Containerfile has no FROM instruction"))
    return graph


# --------------------------------------------------------------------------
# systemd units and quadlets


def parse_unit_ini(text: str) -> list[tuple[str, str, str, int]]:
    """`(section, key, value, line)` entries of a systemd-style file, with
    repeated keys kept, continuations joined and comments dropped."""
    out: list[tuple[str, str, str, int]] = []
    section = ""
    buf: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines()[:20000], 1):
        line = raw.strip()
        if not buf:
            if not line or line[0] in "#;":
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            start = lineno
        elif line and line[0] in "#;":
            continue
        if line.endswith("\\"):
            buf.append(line[:-1].strip())
            continue
        buf.append(line)
        joined = " ".join(b for b in buf if b)
        buf = []
        key, sep, value = joined.partition("=")
        if sep and section:
            out.append((section, key.strip(), value.strip(), start))
    return out


_DEP_KEYS = {
    "Requires": "requires", "Wants": "wants", "After": "after",
    "Before": "before", "PartOf": "part_of", "BindsTo": "binds_to",
    "Requisite": "requisite", "Upholds": "upholds", "Conflicts": "conflicts",
}
_INSTALL_KEYS = {"WantedBy": "wanted_by", "RequiredBy": "required_by",
                 "UpheldBy": "upheld_by"}
_EXEC_KEYS = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecReload",
              "ExecStop", "ExecStopPost", "ExecCondition")


def quadlet_service_name(rel: str) -> str:
    """The systemd service a quadlet file generates."""
    path = PurePosixPath(rel)
    suffix = path.suffix.lower()
    stem = path.stem
    if suffix in (".container", ".kube"):
        return f"{stem}.service"
    return f"{stem}-{suffix[1:]}.service"


def _env_assign_names(value: str) -> list[str]:
    return _names(_words(value))


def _unit_common(graph: OpsGraph, node: OpsNode, entries: list[tuple[str, str, str, int]],
                 rel: str, base: str) -> None:
    for section, key, value, _line in entries:
        if section == "Unit" and key in _DEP_KEYS:
            relation = _DEP_KEYS[key]
            names = [w for w in value.split() if _NAME.fullmatch(w)]
            bucket = node.attrs.setdefault(relation, [])
            for name in names:
                if name not in bucket and len(bucket) < MAX_ITEMS:
                    bucket.append(name)
                    graph.refs.append(OpsRef(node.key, relation, "unit", name,
                                             f"[Unit] {key}=", base))
        elif section == "Install" and key in _INSTALL_KEYS:
            relation = _INSTALL_KEYS[key]
            bucket = node.attrs.setdefault(relation, [])
            for name in value.split():
                if _NAME.fullmatch(name) and name not in bucket and len(bucket) < MAX_ITEMS:
                    bucket.append(name)
                    graph.refs.append(OpsRef(node.key, relation, "unit", name,
                                             f"[Install] {key}=", base))
        elif key == "EnvironmentFile":
            path = value.lstrip("-").strip()
            bucket = node.attrs.setdefault("env_files", [])
            text = clean(path)
            if text and text not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text)
            graph.refs.append(OpsRef(node.key, "env_file", "file", path,
                                     f"[{section}] EnvironmentFile=", base))
        elif key == "Environment":
            bucket = node.attrs.setdefault("env", [])
            for name in _env_assign_names(value):
                if name not in bucket and len(bucket) < MAX_ITEMS:
                    bucket.append(name)
    for name in list(node.attrs):
        if node.attrs[name] == []:
            del node.attrs[name]


def parse_unit(text: str, rel: str) -> OpsGraph:
    graph = OpsGraph()
    path = PurePosixPath(rel)
    base = path.parent.as_posix()
    entries = parse_unit_ini(text)
    sections = {s for s, *_ in entries}
    if not sections & {"Unit", "Service", "Timer", "Socket", "Path", "Install"}:
        return graph
    suffix = path.suffix.lower()[1:]
    key = f"ops:unit/{rel}"
    node = OpsNode(key, f"systemd_{suffix}", path.name, rel, 1,
                   {"source_digest": _digest(text)})
    graph.nodes.append(node)
    _unit_common(graph, node, entries, rel, base)
    triggers: list[str] = []
    for section, k, value, _line in entries:
        if section == "Service" and k in _EXEC_KEYS:
            command = value.lstrip("-@:+!|")
            words = _words(command)
            if words:
                program = clean(words[0])
                if program:
                    bucket = node.attrs.setdefault(
                        "exec" if k == "ExecStart" else "exec_other", [])
                    if program not in bucket and len(bucket) < MAX_ITEMS:
                        bucket.append(program)
                for word in words[:32]:
                    if "/" in word and not word.startswith("-"):
                        graph.refs.append(OpsRef(key, "runs", "file", word,
                                                 f"[Service] {k}=", base))
        elif section == "Service" and k == "WorkingDirectory":
            wd = clean(value.lstrip("-"))
            if wd:
                node.attrs["working_directory"] = wd
        elif section == "Service" and k == "Type":
            kind = clean(value)
            if kind:
                node.attrs["service_type"] = kind
        elif section == "Timer" and k.startswith("On"):
            bucket = node.attrs.setdefault(
                "on_calendar" if k == "OnCalendar" else "timer_triggers", [])
            text_v = clean(value if k == "OnCalendar" else f"{k}={value}")
            if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text_v)
        elif section == "Socket" and k.startswith("Listen"):
            bucket = node.attrs.setdefault("listen", [])
            text_v = clean(value)
            if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text_v)
        elif section == "Path" and k.startswith(("Path", "Directory")):
            bucket = node.attrs.setdefault("watches", [])
            text_v = clean(value)
            if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text_v)
        elif section in ("Timer", "Path") and k == "Unit":
            triggers.append(value.strip())
        elif section == "Socket" and k == "Service":
            triggers.append(value.strip())
    if suffix in ("timer", "socket", "path") and not triggers:
        triggers.append(f"{path.stem}.service")
    for target in triggers:
        if _NAME.fullmatch(target):
            node.attrs["unit"] = target
            graph.refs.append(OpsRef(key, "triggers", "unit", target,
                                     f"[{suffix.capitalize()}] Unit=", base))
    wd = node.attrs.get("working_directory")
    if isinstance(wd, str):
        node.attrs["_wd"] = wd
    return graph


_QUADLET_SECTION = {"container": "Container", "pod": "Pod",
                    "network": "Network", "volume": "Volume", "kube": "Kube",
                    "image": "Image", "build": "Build"}


def parse_quadlet(text: str, rel: str) -> OpsGraph:
    graph = OpsGraph()
    path = PurePosixPath(rel)
    base = path.parent.as_posix()
    suffix = path.suffix.lower()[1:]
    entries = parse_unit_ini(text)
    key = f"ops:quadlet/{rel}"
    node = OpsNode(key, f"quadlet_{suffix}", path.name, rel, 1,
                   {"source_digest": _digest(text),
                    "service_unit": quadlet_service_name(rel)})
    graph.nodes.append(node)
    _unit_common(graph, node, entries, rel, base)
    main = _QUADLET_SECTION[suffix]
    if suffix == "container":
        node.attrs["container_name"] = f"systemd-{path.stem}"
    for section, k, value, _line in entries:
        if section != main:
            continue
        if k == "Image":
            ref = value.strip()
            if ref.endswith((".image", ".build")):
                node.attrs["image"] = clean(ref) or REDACTED
                graph.refs.append(OpsRef(key, "uses_image", "quadlet", ref,
                                         f"[{main}] Image=", base))
            else:
                image = normalize_image(ref)
                if image:
                    node.attrs["image"] = image
                    graph.refs.append(OpsRef(
                        key, "pulls" if suffix == "image" else "uses_image",
                        "image", image, f"[{main}] Image=", base))
                else:
                    graph.diagnostics.append((rel, f"quadlet {path.name}: Image= not resolved"))
        elif k == "Pod":
            node.attrs["pod"] = clean(value) or REDACTED
            graph.refs.append(OpsRef(key, "in_pod", "quadlet", value.strip(),
                                     f"[{main}] Pod=", base))
        elif k == "Network":
            for net in value.split(","):
                net = net.split(":", 1)[0].strip()
                bucket = node.attrs.setdefault("networks", [])
                text_v = clean(net)
                if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                    bucket.append(text_v)
                if net.endswith(".network"):
                    graph.refs.append(OpsRef(key, "uses_network", "quadlet", net,
                                             f"[{main}] Network=", base))
        elif k == "Volume":
            bucket = node.attrs.setdefault("volumes", [])
            text_v = clean(value)
            if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text_v)
            source = value.split(":", 1)[0].strip()
            if source.endswith(".volume"):
                graph.refs.append(OpsRef(key, "mounts_volume", "quadlet", source,
                                         f"[{main}] Volume=", base))
            elif source.startswith(("./", "../")):
                graph.refs.append(OpsRef(key, "mounts", "file", source,
                                         f"[{main}] Volume=", base))
        elif k == "PublishPort":
            bucket = node.attrs.setdefault("ports", [])
            text_v = clean(value)
            if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text_v)
        elif k == "ExposeHostPort":
            bucket = node.attrs.setdefault("expose", [])
            text_v = clean(value)
            if text_v and text_v not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(text_v)
        elif k in ("ContainerName", "PodName", "NetworkName", "VolumeName"):
            text_v = clean(value)
            if text_v:
                node.attrs[{"ContainerName": "container_name", "PodName": "pod_name",
                            "NetworkName": "network_name",
                            "VolumeName": "volume_name"}[k]] = text_v
        elif k == "ImageTag" and suffix == "build":
            image = normalize_image(value)
            if image:
                node.attrs["image"] = image
                graph.refs.append(OpsRef(key, "builds_image", "image", image,
                                         f"[{main}] ImageTag=", base))
        elif k == "File" and suffix == "build":
            build_rel = _join(base, value.strip())
            if build_rel:
                graph.refs.append(OpsRef(key, "builds_from", "containerfile",
                                         build_rel, f"[{main}] File=", base))
        elif k == "Yaml":
            node.attrs["yaml"] = clean(value) or REDACTED
            graph.refs.append(OpsRef(key, "kube_yaml", "file", value.strip(),
                                     f"[{main}] Yaml=", base))
        elif k == "Exec":
            program = _first_word(value)
            if program:
                node.attrs["exec"] = [program]
        elif k == "Secret":
            bucket = node.attrs.setdefault("secrets", [])
            name = env_name(value.split(",", 1)[0])
            if name and name not in bucket and len(bucket) < MAX_ITEMS:
                bucket.append(name)
    for name in list(node.attrs):
        if node.attrs[name] == []:
            del node.attrs[name]
    return graph


def parse_ops_source(text: str, rel: str, fmt: str | None = None) -> OpsGraph:
    """Parse one operations source by its format (see `ops_source_format`)."""
    fmt = fmt or ops_source_format(rel)
    if not _safe_rel(rel):
        graph = OpsGraph()
        graph.diagnostics.append((rel, "operations source path has unsupported characters"))
        return graph
    if fmt == "compose":
        return parse_compose(text, rel)
    if fmt == "containerfile":
        return parse_containerfile(text, rel)
    if fmt == "quadlet":
        return parse_quadlet(text, rel)
    if fmt == "unit":
        return parse_unit(text, rel)
    return OpsGraph()


# --------------------------------------------------------------------------
# Cross-file resolution


def _unit_template(name: str) -> str | None:
    """`foo@.service` for an instance name `foo@bar.service`."""
    m = re.fullmatch(r"([^@]+)@[^.]*(\.[a-z]+)", name)
    return f"{m.group(1)}@{m.group(2)}" if m else None


def resolve(graph: OpsGraph, *, file_exists: Any, root: str | None = None,
            read_containerfile: Any = None) -> OpsGraph:
    """Resolve references across files in place and return `graph`.

    `file_exists(rel)` says whether a repository file exists (and may be
    linked); `root` is the repository's absolute path, for absolute paths in
    units; `read_containerfile(rel)` returns the text of a build file not
    discovered by name (a compose `dockerfile:` with another name), or None.
    """
    by_key = {n.key: n for n in graph.nodes}
    containerfiles = {n.path: n for n in graph.nodes if n.kind == "containerfile"}
    units: dict[str, list[OpsNode]] = {}
    quadlets: dict[str, list[OpsNode]] = {}
    for n in graph.nodes:
        if n.kind.startswith("systemd_"):
            units.setdefault(n.name, []).append(n)
        elif n.kind.startswith("quadlet_"):
            quadlets.setdefault(n.name, []).append(n)
            units.setdefault(str(n.attrs.get("service_unit")), []).append(n)
    images: dict[str, OpsNode] = {}
    edges = list(graph.edges)
    seen_edges = set(edges)
    links: list[tuple[str, str, str]] = list(graph.file_links)

    def add_edge(edge: OpsEdge) -> None:
        if edge not in seen_edges:
            seen_edges.add(edge)
            edges.append(edge)

    def pick(cands: list[OpsNode], base: str) -> OpsNode | None:
        if len(cands) == 1:
            return cands[0]
        near = [c for c in cands if PurePosixPath(c.path).parent.as_posix() == base]
        return near[0] if len(near) == 1 else None

    def image_node(ref: str, path: str) -> OpsNode:
        node = images.get(ref)
        if node is None:
            node = OpsNode(f"ops:image/{ref}", "image", ref, path, None, {})
            images[ref] = node
        elif path < node.path:
            node.path = path
        return node

    def repo_rel(raw: str, base: str, wd: str | None) -> str | None:
        """The repository path `raw` names: an absolute path under `root`;
        a relative one against the unit's WorkingDirectory when that is in
        the repository, else against the root or the declaring directory
        (the first that exists)."""
        raw = raw.strip()
        prefix = root.rstrip("/") + "/" if root else None
        if raw.startswith("/"):
            if not prefix or not raw.startswith(prefix):
                return None
            rel = raw[len(prefix):]
            return rel if _safe_rel(rel) else None
        if wd and wd.startswith("/"):
            if not prefix:
                return None
            if wd.rstrip("/") == root.rstrip("/"):
                return _join(".", raw)
            if (wd.rstrip("/") + "/").startswith(prefix):
                return _join(wd.rstrip("/")[len(prefix):], raw)
            return None
        options = [_join(base, raw)]
        if base not in ("", "."):
            options.insert(1 if raw.startswith(("./", "../")) else 0, _join(".", raw))
        for option in options:
            if option and option != "." and file_exists(option):
                return option
        return None

    pending = list(graph.refs)
    extra_nodes: list[OpsNode] = []
    while pending:
        ref = pending.pop(0)
        source = by_key.get(ref.source)
        if ref.target_type == "image":
            node = image_node(ref.target, source.path if source else "")
            add_edge(OpsEdge(ref.source, node.key, ref.relation, ref.evidence))
            continue
        if source is None and not ref.source.startswith("ops:image/"):
            continue
        if ref.target_type == "containerfile":
            target = ref.target
            cand = containerfiles.get(target)
            if cand is None:
                for name in ("Containerfile", "Dockerfile"):
                    joined = _join(target, name)
                    if joined and joined in containerfiles:
                        cand = containerfiles[joined]
                        break
            if cand is None and read_containerfile is not None:
                options = [target] + [j for j in (_join(target, "Containerfile"),
                                                  _join(target, "Dockerfile")) if j]
                for option in options:
                    if option in containerfiles or not file_exists(option):
                        continue
                    text = read_containerfile(option)
                    if text is None:
                        continue
                    parsed = parse_containerfile(text, option)
                    for n in parsed.nodes:
                        by_key[n.key] = n
                    extra_nodes.extend(parsed.nodes)
                    for e in parsed.edges:
                        add_edge(e)
                    pending.extend(parsed.refs)
                    graph.diagnostics.extend(parsed.diagnostics)
                    cand = containerfiles[option] = parsed.nodes[0]
                    break
            if cand is None:
                graph.diagnostics.append((source.path if source else "", "build file not found in the repository"))
                continue
            target_key = cand.key
            if ref.stage:
                stage_key = f"{cand.key}#stage/{ref.stage}"
                if stage_key in by_key:
                    target_key = stage_key
            if ref.source.startswith("ops:image/"):
                node = images.get(ref.source[len("ops:image/"):])
                if node is None:
                    continue
            add_edge(OpsEdge(ref.source, target_key, ref.relation, ref.evidence))
            continue
        assert source is not None
        if ref.target_type == "unit":
            name = ref.target
            cands = units.get(name) or units.get(_unit_template(name) or "", [])
            hit = pick(cands, ref.base)
            if hit is not None and hit.key != source.key:
                add_edge(OpsEdge(source.key, hit.key, ref.relation, ref.evidence))
            continue
        if ref.target_type == "quadlet":
            hit = pick(quadlets.get(ref.target, []), ref.base)
            if hit is not None:
                add_edge(OpsEdge(source.key, hit.key, ref.relation, ref.evidence))
            else:
                graph.diagnostics.append((source.path, f"quadlet reference {clean(ref.target) or REDACTED} not found"))
            continue
        if ref.target_type == "file":
            wd = source.attrs.get("_wd") if isinstance(source.attrs.get("_wd"), str) else None
            rel = repo_rel(ref.target, ref.base, wd)
            if rel and rel != "." and file_exists(rel):
                link = (source.key, ref.relation, rel)
                if link not in links:
                    links.append(link)
    for n in graph.nodes + extra_nodes:
        n.attrs.pop("_wd", None)
    graph.nodes = graph.nodes + extra_nodes + sorted(images.values(), key=lambda n: n.key)
    keys = {n.key for n in graph.nodes}
    graph.edges = [e for e in edges if e.source in keys and e.target in keys]
    graph.file_links = links
    graph.refs = []
    return graph


# --------------------------------------------------------------------------
# Queries over the loaded graph (InfraResource nodes, family "ops")


def ops_nodes(memory: Any) -> list[tuple[int, str, dict[str, Any]]]:
    """`(id, key, attrs)` of every ops node in `memory`."""
    nodes = getattr(memory, "nodes", {})
    type_index = getattr(memory, "nodes_of_type", None)
    ids = (type_index("InfraResource") if callable(type_index)
           else [nid for nid, n in nodes.items() if n.get("type") == "InfraResource"])
    out = []
    for nid in ids:
        node = nodes.get(nid) or {}
        attrs = node.get("attrs") or {}
        if attrs.get("family") == "ops":
            out.append((int(nid), str(node.get("key") or ""), attrs))
    return out


def describe(key: str, attrs: dict[str, Any]) -> str:
    """One short line naming an ops node: kind, name and citation."""
    kind = str(attrs.get("kind") or "").replace("_", " ")
    path = str(attrs.get("path") or "")
    line = attrs.get("line")
    cite = f"{path}:{line}" if isinstance(line, int) and path else path
    return f"{key}: {kind} {attrs.get('name', '')}" + (f" ({cite})" if cite else "")


def _resolve_targets(memory: Any, target: str) -> list[tuple[int, str, dict[str, Any]]]:
    target = (target or "").strip()
    if not target:
        return []
    found = ops_nodes(memory)
    if target.startswith("ops:"):
        return [x for x in found if x[1] == target]
    if target.startswith("file:"):
        target = target[5:]
    file_kinds = ("containerfile", "compose_project")
    exact = [x for x in found if x[2].get("path") == target and (
        x[2].get("kind") in file_kinds
        or str(x[2].get("kind", "")).startswith(("systemd_", "quadlet_")))]
    if exact:
        return exact
    names = {target, normalize_image(target) or ""}
    if "." not in target:
        names.add(f"{target}.service")
    return [x for x in found if (x[2].get("name") in names
                                 or x[2].get("aliases") in names
                                 or x[2].get("container_name") == target)]


_EXPOSE_FIELDS = ("ports", "expose", "listen", "volumes", "networks", "env",
                  "env_files", "image", "entrypoint", "cmd", "exec",
                  "working_directory", "on_calendar", "unit", "profiles",
                  "depends_on", "secrets")


def ops_query(memory: Any, query: str, target: str, *, limit: int = 20) -> dict[str, Any]:
    """`dependents` (what depends on `target`), `exposes` (its ports,
    volumes, networks, env names, image, program) or `runs` (units,
    quadlets and compose services that run, mount or read the repository
    file `target`). `target` is an `ops:` key, a `file:` key or path, or a
    name (service, unit, quadlet, container or image)."""
    cap = min(100, max(1, int(limit)))
    nodes = getattr(memory, "nodes", {})
    if query == "runs":
        rel = target[5:] if target.startswith("file:") else target
        fid = memory.node_by_key(f"file:{rel}") if hasattr(memory, "node_by_key") else None
        results = []
        if fid is not None:
            for edge in getattr(memory, "_in", {}).get(fid, []):
                relation = str(edge.get("type", ""))
                if not relation.startswith("ops_"):
                    continue
                src = nodes.get(int(edge["src"])) or {}
                attrs = src.get("attrs") or {}
                results.append({"key": src.get("key"), "relation": relation[4:],
                                "kind": attrs.get("kind"), "name": attrs.get("name"),
                                "path": attrs.get("path"), "line": attrs.get("line")})
        return {"query": query, "target": f"file:{rel}", "results": results[:cap],
                "truncated": len(results) > cap, "complete": False}
    matches = _resolve_targets(memory, target)
    if not matches:
        return {"query": query, "target": target, "results": [],
                "complete": False, "reason": "no ops node matches"}
    results: list[dict[str, Any]] = []
    if query == "exposes":
        for _nid, key, attrs in matches[:cap]:
            item = {"key": key, "kind": attrs.get("kind"), "name": attrs.get("name"),
                    "path": attrs.get("path")}
            item.update({f: attrs[f] for f in _EXPOSE_FIELDS if f in attrs})
            results.append(item)
        return {"query": query, "target": target, "results": results,
                "ambiguous": len(matches) > 1, "complete": False}
    if query != "dependents":
        raise ValueError("query must be dependents, exposes or runs")
    for nid, key, _attrs in matches:
        seen = {nid}
        queue = [(nid, 0)]
        while queue and len(results) < cap:
            cur, level = queue.pop(0)
            if level >= 3:
                continue
            for edge in getattr(memory, "_in", {}).get(cur, []):
                relation = str(edge.get("type", ""))
                if not relation.startswith("infra_") or relation.startswith("infra_has_"):
                    continue
                src = int(edge["src"])
                node = nodes.get(src) or {}
                if node.get("type") != "InfraResource" or src in seen:
                    continue
                seen.add(src)
                attrs = node.get("attrs") or {}
                results.append({"key": node.get("key"), "relation": relation[6:],
                                "of": (nodes.get(cur) or {}).get("key"),
                                "kind": attrs.get("kind"), "name": attrs.get("name"),
                                "path": attrs.get("path"), "depth": level + 1})
                queue.append((src, level + 1))
    return {"query": query, "target": target,
            "matched": [key for _n, key, _a in matches],
            "results": results[:cap], "truncated": len(results) > cap,
            "complete": False}
