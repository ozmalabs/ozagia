"""Operations graph: compose files, Containerfiles, quadlets, systemd units
(`ozagia.core.ops_graph`, emitted by the opt-in IaC adapter), its queries,
and `[ozagia] ops:` lines after container/unit log commands."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import intent
from ozagia.core.infra_discovery import source_kind
from ozagia.core.keys import format_key, parse_key
from ozagia.core.memory import DevMemory, build_index
from ozagia.core.ops_graph import (
    REDACTED,
    clean,
    env_name,
    looks_secret,
    normalize_image,
    ops_source_format,
    parse_compose,
    parse_containerfile,
    parse_quadlet,
    parse_unit,
)
from ozagia.intent.ops_logs import log_requests

FIXTURE = Path(__file__).parent / "fixtures" / "ops_repo"
CANARIES = ("CANARY_COMPOSE_SECRET", "CANARY_ARG_SECRET", "CANARY_ENV_SECRET",
            "CANARY_ENVFILE_SECRET", "CANARY_QUADLET_SECRET",
            "CANARY_UNIT_SECRET", "Zx9Kq2Lm8Pw4Rt6Yv1Nb3Hc5Jd7Fg0SaQe")


def _keys(graph):
    return {n.key: n for n in graph.nodes}


def _edges(graph):
    return {(e.source, e.relation, e.target) for e in graph.edges}


# --------------------------------------------------------------------------- #
# discovery and helpers
# --------------------------------------------------------------------------- #

def test_source_formats_and_discovery_kind():
    assert ops_source_format("compose.yaml") == "compose"
    assert ops_source_format("deploy/docker-compose.override.yml") == "compose"
    assert ops_source_format("podman-compose.yml") == "compose"
    assert ops_source_format("app/Containerfile") == "containerfile"
    assert ops_source_format("Dockerfile.dev") == "containerfile"
    assert ops_source_format("build/api.Dockerfile") == "containerfile"
    assert ops_source_format("deploy/web.service") == "unit"
    assert ops_source_format("deploy/web.timer") == "unit"
    assert ops_source_format("x/web.container") == "quadlet"
    assert ops_source_format("x/containers/systemd/web.network") == "quadlet"
    assert ops_source_format("quadlets/data.volume") == "quadlet"
    # Common names elsewhere are not quadlets.
    assert ops_source_format("docs/web.network") is None
    assert ops_source_format("img/disk.image") is None
    assert ops_source_format("values.yaml") is None
    assert source_kind("app/compose.yaml") == "ops"
    assert source_kind("deploy/k8s.yaml") == "manifest"
    assert source_kind(".env") is None


def test_keys_round_trip():
    key = "ops:compose/app/compose.yaml#service/web"
    assert parse_key(key) == ("ops", "compose/app/compose.yaml#service/web")
    assert format_key(parse_key(key)) == key


def test_secret_redaction_helpers():
    for secret in ("AKIAABCDEFGHIJKLMNOP", "Bearer abcdefghijkl",
                   "https://user:hunter2@example.com/x", "password=hunter2",
                   "token: abc", "Zx9Kq2Lm8Pw4Rt6Yv1Nb3Hc5Jd7Fg0SaQe",
                   "-----BEGIN RSA PRIVATE KEY-----",
                   "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkw"):
        assert looks_secret(secret), secret
        assert clean(secret) == REDACTED
    for fine in ("8080:80", "/usr/bin/python3", "postgres:16",
                 "python@sha256:" + "a" * 64, "*-*-* 03:00:00"):
        assert not looks_secret(fine), fine
        assert clean(fine) == fine
    assert clean("${PORT:-hunter2}:80") == "${PORT}:80"
    assert env_name("DB_PASSWORD=hunter2") == "DB_PASSWORD"
    assert env_name("AKIAABCDEFGHIJKLMNOP") is None
    assert normalize_image("docker.io/library/nginx") == "nginx:latest"
    assert normalize_image("localhost/app:1") == "app:1"
    assert normalize_image("ghcr.io/o/app@sha256:" + "b" * 64).endswith("b" * 64)
    assert normalize_image("${IMAGE}") is None
    assert normalize_image("scratch") is None


# --------------------------------------------------------------------------- #
# per-format parsing
# --------------------------------------------------------------------------- #

def test_compose_with_pyyaml():
    pytest.importorskip("yaml")
    text = (FIXTURE / "app" / "compose.yaml").read_text()
    graph = parse_compose(text, "app/compose.yaml")
    nodes = _keys(graph)
    web = nodes["ops:compose/app/compose.yaml#service/web"]
    assert web.line == 5
    assert web.attrs["image"] == "shop-web:1.2"
    assert web.attrs["ports"] == ["8080:80", "8443:443/tcp"]
    assert web.attrs["expose"] == ["9000"]
    assert web.attrs["env"] == ["DB_PASSWORD", "LOG_LEVEL"]
    assert web.attrs["env_files"] == ["deploy/web.env"]
    assert web.attrs["profiles"] == ["prod"]
    assert web.attrs["project"] == "shop"
    assert nodes["ops:compose/app/compose.yaml#service/db"].attrs["env"] == [
        "POSTGRES_PASSWORD"]
    edges = _edges(graph)
    assert ("ops:compose/app/compose.yaml#service/web", "depends_on",
            "ops:compose/app/compose.yaml#service/db") in edges
    assert ("ops:compose/app/compose.yaml#service/web", "uses_network",
            "ops:compose/app/compose.yaml#network/front") in edges
    assert ("ops:compose/app/compose.yaml#service/db", "mounts_volume",
            "ops:compose/app/compose.yaml#volume/data") in edges
    builds = [r for r in graph.refs if r.relation == "builds_from"]
    assert {(r.target, r.stage) for r in builds} == {
        ("app/Containerfile", "runtime"), ("app/worker", None)}
    dumped = json.dumps(graph.to_dict())
    assert not any(c in dumped for c in CANARIES)


def test_compose_without_pyyaml_reports_and_keeps_service_names(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    text = (FIXTURE / "app" / "compose.yaml").read_text()
    graph = parse_compose(text, "app/compose.yaml")
    services = {n.name: n for n in graph.nodes if n.kind == "compose_service"}
    assert set(services) == {"web", "db", "worker"}
    assert services["web"].confidence == "partial"
    assert services["web"].attrs["image"] == "shop-web:1.2"
    assert services["web"].attrs["container_name"] == "shop-web"
    assert services["db"].line == 31
    assert any("YAML parser unavailable" in msg for _p, msg in graph.diagnostics)
    assert not any(c in json.dumps(graph.to_dict()) for c in CANARIES)


def test_containerfile_stages_copy_from_and_names_only():
    text = (FIXTURE / "app" / "Containerfile").read_text()
    graph = parse_containerfile(text, "app/Containerfile")
    nodes = _keys(graph)
    assert set(nodes) == {"ops:containerfile/app/Containerfile",
                          "ops:containerfile/app/Containerfile#stage/build",
                          "ops:containerfile/app/Containerfile#stage/runtime"}
    build = nodes["ops:containerfile/app/Containerfile#stage/build"]
    runtime = nodes["ops:containerfile/app/Containerfile#stage/runtime"]
    assert build.attrs["base_image"] == "python:3.13-slim"
    assert build.attrs["args"] == ["PIP_TOKEN"]
    assert build.attrs["env"] == ["PIP_NO_CACHE_DIR", "SECRET_KEY"]
    assert runtime.attrs["expose"] == ["80/tcp", "443"]
    assert runtime.attrs["entrypoint"] == "/app/entry.sh"
    assert runtime.attrs["cmd"] == "serve"
    assert runtime.line == 11
    edges = _edges(graph)
    assert (runtime.key, "from_stage", build.key) in edges
    assert (runtime.key, "copies_from", build.key) in edges
    assert {(r.relation, r.target) for r in graph.refs} == {
        ("base_image", "python:3.13-slim"), ("copies_from", "busybox:1.36")}
    root = nodes["ops:containerfile/app/Containerfile"]
    assert root.attrs["args"] == ["PY"]
    assert root.attrs["final_stage"] == "runtime"
    # The heredoc body's `FROM fake:image` is not an instruction.
    assert "fake:image" not in json.dumps(graph.to_dict())
    assert not any(c in json.dumps(graph.to_dict()) for c in CANARIES)


def test_quadlet_container_fields():
    text = (FIXTURE / "deploy/containers/systemd/shop-web.container").read_text()
    rel = "deploy/containers/systemd/shop-web.container"
    graph = parse_quadlet(text, rel)
    (node,) = graph.nodes
    assert node.kind == "quadlet_container"
    assert node.attrs["service_unit"] == "shop-web.service"
    assert node.attrs["container_name"] == "shop-web"
    assert node.attrs["image"] == "shop-web:1.2"
    assert node.attrs["ports"] == ["8080:80"]
    assert node.attrs["env"] == ["TOKEN", "MODE"]
    assert node.attrs["requires"] == ["shop-db.service"]
    refs = {(r.relation, r.target_type, r.target) for r in graph.refs}
    assert ("in_pod", "quadlet", "shop.pod") in refs
    assert ("uses_network", "quadlet", "shop.network") in refs
    assert ("mounts_volume", "quadlet", "shop-data.volume") in refs
    assert ("env_file", "file", "../../web.env") in refs
    assert "CANARY_QUADLET_SECRET" not in json.dumps(graph.to_dict())


def test_unit_and_timer_fields():
    text = (FIXTURE / "deploy/systemd/backup.service").read_text()
    graph = parse_unit(text, "deploy/systemd/backup.service")
    (node,) = graph.nodes
    assert node.kind == "systemd_service"
    assert node.attrs["exec"] == ["/usr/bin/python3"]
    assert node.attrs["exec_other"] == ["/bin/sh"]
    assert node.attrs["env"] == ["AWS_SECRET_ACCESS_KEY", "REGION"]
    assert node.attrs["env_files"] == ["/etc/shop/backup.env"]
    assert node.attrs["wants"] == ["shop-web.service"]
    assert "CANARY_UNIT_SECRET" not in json.dumps(graph.to_dict())
    timer = parse_unit((FIXTURE / "deploy/systemd/backup.timer").read_text(),
                       "deploy/systemd/backup.timer")
    (tnode,) = timer.nodes
    assert tnode.kind == "systemd_timer"
    assert tnode.attrs["on_calendar"] == ["*-*-* 03:00:00"]
    assert tnode.attrs["unit"] == "backup.service"
    # A file with a unit suffix but no unit sections yields nothing.
    assert parse_unit("just text\n", "notes.path").nodes == []


# --------------------------------------------------------------------------- #
# the graph through the IaC adapter
# --------------------------------------------------------------------------- #

def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True)


@pytest.fixture()
def ops_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    unit = root / "deploy/systemd/backup.service"
    unit.write_text(unit.read_text().replace("@ROOT@", str(root.resolve())))
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "add", ".")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "fixture")
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    return root, DevMemory(dump, tmp_path / "journal.jsonl", root=root), dump


def _ops(memory):
    return {n["key"]: n["attrs"] for n in memory.nodes.values()
            if n["type"] == "InfraResource" and n["attrs"].get("family") == "ops"}


def _edge_set(memory, prefix="infra_"):
    return {(memory.key_of(e["src"]), e["type"], memory.key_of(e["tgt"]))
            for bucket in memory._out.values() for e in bucket
            if e["type"].startswith(prefix)}


def test_graph_nodes_cross_links_and_no_values(ops_repo):
    pytest.importorskip("yaml")
    root, memory, dump = ops_repo
    ops = _ops(memory)
    web = "ops:compose/app/compose.yaml#service/web"
    quadlet = "ops:quadlet/deploy/containers/systemd/shop-web.container"
    unit = "ops:unit/deploy/systemd/backup.service"
    assert {web, quadlet, unit, "ops:image/shop-web:1.2", "ops:image/postgres:16",
            "ops:containerfile/app/Containerfile#stage/runtime",
            "ops:containerfile/app/worker/Dockerfile",
            "ops:unit/deploy/systemd/backup.timer"} <= set(ops)
    assert ops[web]["path"] == "app/compose.yaml" and ops[web]["line"] == 5
    edges = _edge_set(memory)
    # compose build -> Containerfile (target stage); the worker's build
    # context resolves to its Dockerfile.
    assert (web, "infra_builds_from",
            "ops:containerfile/app/Containerfile#stage/runtime") in edges
    assert ("ops:compose/app/compose.yaml#service/worker", "infra_builds_from",
            "ops:containerfile/app/worker/Dockerfile") in edges
    # quadlet Image= and compose image: one shared image node.
    assert (web, "infra_uses_image", "ops:image/shop-web:1.2") in edges
    assert (quadlet, "infra_uses_image", "ops:image/shop-web:1.2") in edges
    assert ("ops:image/shop-web:1.2", "infra_built_from",
            "ops:containerfile/app/Containerfile") in edges
    assert ("ops:quadlet/deploy/containers/systemd/shop-db.container",
            "infra_uses_image", "ops:image/postgres:16") in edges
    # Unit dependencies resolve to the quadlet's generated service.
    assert (unit, "infra_wants", quadlet) in edges
    assert ("ops:unit/deploy/systemd/backup.timer", "infra_triggers", unit) in edges
    files = _edge_set(memory, "ops_")
    assert (unit, "ops_runs", "file:scripts/backup.py") in files
    assert (unit, "ops_runs", "file:scripts/notify.sh") in files
    assert (web, "ops_env_file", "file:deploy/web.env") in files
    assert (quadlet, "ops_env_file", "file:deploy/web.env") in files
    # Values never reach the graph or automatic snippets.
    text = dump.read_text()
    assert not any(c in text for c in CANARIES)
    env_id = memory.node_by_key("file:deploy/web.env")
    assert memory.snippet(env_id)["text"] == ""
    cf_id = memory.node_by_key("file:app/Containerfile")
    assert memory.snippet(cf_id)["text"] == ""


def test_queries_and_mcp_tool(ops_repo):
    pytest.importorskip("yaml")
    _root, memory, _dump = ops_repo
    dep = memory.adapter_query("infra.ops", query="dependents",
                               target="ops:image/shop-web:1.2")
    keys = {r["key"] for r in dep["results"]}
    assert {"ops:compose/app/compose.yaml#service/web",
            "ops:quadlet/deploy/containers/systemd/shop-web.container",
            "ops:unit/deploy/systemd/backup.service"} <= keys
    by_name = memory.adapter_query("infra.ops", query="dependents", target="db")
    assert "ops:compose/app/compose.yaml#service/web" in {
        r["key"] for r in by_name["results"]}
    exposes = memory.adapter_query("infra.ops", query="exposes",
                                   target="ops:compose/app/compose.yaml#service/web")
    (item,) = exposes["results"]
    assert item["ports"] == ["8080:80", "8443:443/tcp"]
    assert item["networks"] == ["front"] and item["env"] == ["DB_PASSWORD", "LOG_LEVEL"]
    runs = memory.adapter_query("infra.ops", query="runs", target="scripts/backup.py")
    assert [r["key"] for r in runs["results"]] == ["ops:unit/deploy/systemd/backup.service"]
    env = memory.adapter_query("infra.ops", query="runs", target="file:deploy/web.env")
    assert {r["relation"] for r in env["results"]} == {"env_file"}
    assert len(env["results"]) == 2
    bad = memory.adapter_query("infra.ops", query="nope", target="db")
    assert bad["results"] == [] and "error" in bad
    # The general infra queries cover ops nodes too.
    located = memory.adapter_query("infra.locate", query="shop-web")
    assert any(r["key"].startswith("ops:") for r in located["results"])
    impact = memory.adapter_query("infra.impact", key="ops:image/postgres:16")
    assert impact["edges"]
    from ozagia.integrations.mcp.server import DevMemoryServer
    server = DevMemoryServer(memory)
    assert "infra_ops" in server._handlers
    got = server._handlers["infra_ops"]({"query": "runs",
                                         "target": "scripts/notify.sh"})
    assert got["results"][0]["key"] == "ops:unit/deploy/systemd/backup.service"


def test_after_annotations(ops_repo):
    pytest.importorskip("yaml")
    root, memory, _dump = ops_repo

    def after(command, workdir=None):
        inp = {"command": command}
        if workdir:
            inp["workdir"] = workdir
        return intent.after(memory, "shell", inp, "2026-11-16 log line\n")

    out = after("podman logs -f --tail 50 shop-web")
    assert "[ozagia] ops:quadlet/deploy/containers/systemd/shop-web.container: quadlet container shop-web.container" in out["append"]
    assert "[ozagia] ops:compose/app/compose.yaml#service/web: compose service web, declared at app/compose.yaml:5" in out["append"]
    assert out["meta"]["ops_after_annotations"] == 2
    assert "ops:quadlet/deploy/containers/systemd/shop-web.container" in out["meta"]["keys"]
    # The quadlet's default container name.
    out = after("sudo podman logs systemd-shop-db")
    assert "ops:quadlet/deploy/containers/systemd/shop-db.container" in out["append"]
    # Compose's default container name <project>-<service>-<n>.
    out = after("docker logs shop-db-1")
    assert "ops:compose/app/compose.yaml#service/db" in out["append"]
    out = after("cd app && docker compose logs --tail 20 db")
    assert "ops:compose/app/compose.yaml#service/db" in out["append"]
    assert "#service/web" not in out["append"]
    out = after("docker compose -f app/compose.yaml logs")
    assert "[ozagia] ops:compose/app/compose.yaml: compose project" in out["append"]
    out = after("journalctl --user -xeu backup -n 100")
    assert ("[ozagia] ops:unit/deploy/systemd/backup.service: systemd service "
            "backup.service, declared at deploy/systemd/backup.service:1; runs "
            "file:scripts/backup.py, file:scripts/notify.sh") in out["append"]
    out = after("journalctl -u shop-web.service --since today")
    assert "ops:quadlet/deploy/containers/systemd/shop-web.container" in out["append"]
    out = after("systemctl --user status backup.timer | head -20")
    assert "ops:unit/deploy/systemd/backup.timer" in out["append"]
    out = after("journalctl -u 'backup*'")
    assert "backup.service" in out["append"] and "backup.timer" in out["append"]
    for unrelated in ("podman ps", "systemctl --user restart backup",
                      "journalctl -u nothing-here", "echo podman logs web"):
        assert "ops:" not in after(unrelated)["append"], unrelated
    # Never captured: the built-in runs these commands.
    for command in ("podman logs shop-web", "journalctl -u backup",
                    "systemctl status backup", "docker compose logs web"):
        assert intent.handle(memory, "shell", {"command": command})["handled"] is False


def test_log_request_parsing():
    assert log_requests(["docker", "logs", "-n", "5", "a", "b"]) == [
        ("container", "a"), ("container", "b")]
    # podman's `-n` is `--names`, a boolean.
    assert log_requests(["podman", "--remote", "logs", "-n", "shop-web"]) == [
        ("container", "shop-web")]
    assert log_requests(["podman", "logs", "--tail", "5", "a"]) == [("container", "a")]
    assert log_requests(["podman-compose", "logs", "-n", "web"]) == [
        ("compose", ([], ["web"]))]
    assert log_requests(["docker-compose", "logs", "-n", "5", "web"]) == [
        ("compose", ([], ["web"]))]
    assert log_requests(["docker", "-H", "tcp://h", "container", "logs", "x"]) == [
        ("container", "x")]
    assert log_requests(["docker-compose", "-p", "p", "logs", "-f", "web"]) == [
        ("compose", ([], ["web"]))]
    assert log_requests(["podman", "compose", "--file", "c.yml", "logs"]) == [
        ("compose", (["c.yml"], []))]
    assert log_requests(["journalctl", "-n50", "-fu", "web", "--unit=db"]) == [
        ("unit", "web"), ("unit", "db")]
    assert log_requests(["systemctl", "-H", "host", "status", "a", "b.timer"]) == [
        ("unit", "a"), ("unit", "b.timer")]
    assert log_requests(["systemctl", "start", "a"]) == []
    assert log_requests(["docker", "compose", "up", "web"]) == []


def test_daemon_counts_ops(ops_repo, tmp_path):
    pytest.importorskip("yaml")
    from ozagia.daemon.server import DaemonState
    root, memory, _dump = ops_repo
    st = DaemonState(root, memory, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "shell", "harness": "opencode",
           "input": {"command": "journalctl -u backup"}, "output": "x\n"}
    st.record(msg, st.dispatch(msg), 1.0)
    stats = st.stats()
    assert stats["ops_after_annotations"] == 1
    assert stats["ops_nodes"] == len(_ops(memory))


def test_reingest_on_edit_and_new_files(ops_repo):
    pytest.importorskip("yaml")
    root, memory, _dump = ops_repo
    compose = root / "app/compose.yaml"
    compose.write_text(compose.read_text().replace(
        "  worker:\n", "  cache:\n    image: redis:7\n    ports: [\"6379\"]\n  worker:\n"))
    memory._staleness_memo = None
    memory.refresh()
    ops = _ops(memory)
    assert ops["ops:compose/app/compose.yaml#service/cache"]["ports"] == ["6379"]
    assert "ops:image/redis:7" in ops
    # A unit edit moves the run link.
    unit = root / "deploy/systemd/backup.service"
    unit.write_text(unit.read_text().replace("ExecStartPost=/bin/sh scripts/notify.sh\n", ""))
    memory._staleness_memo = None
    memory.refresh()
    files = _edge_set(memory, "ops_")
    assert ("ops:unit/deploy/systemd/backup.service", "ops_runs",
            "file:scripts/notify.sh") not in files
    assert ("ops:unit/deploy/systemd/backup.service", "ops_runs",
            "file:scripts/backup.py") in files
    # A new quadlet is discovered; a deleted one goes.
    (root / "deploy/containers/systemd/cache.container").write_text(
        "[Container]\nImage=docker.io/library/redis:7\n")
    (root / "deploy/containers/systemd/shop-db.container").unlink()
    memory._staleness_memo = None
    memory.refresh()
    ops = _ops(memory)
    assert "ops:quadlet/deploy/containers/systemd/cache.container" in ops
    assert "ops:quadlet/deploy/containers/systemd/shop-db.container" not in ops
    assert ("ops:quadlet/deploy/containers/systemd/cache.container",
            "infra_uses_image", "ops:image/redis:7") in _edge_set(memory)
    # The same graph as a fresh build.
    fresh_dump = root.parent / "fresh.json"
    build_index(root, fresh_dump, include_docs="none", include_git=False)
    fresh = DevMemory(fresh_dump, root.parent / "fresh.jsonl", root=root)
    assert set(_ops(fresh)) == set(_ops(memory))
    assert _edge_set(fresh) == _edge_set(memory)
    assert _edge_set(fresh, "ops_") == _edge_set(memory, "ops_")


# --------------------------------------------------------------------------- #
# review fixes: isolation, time bounds, values, budgets, matching
# --------------------------------------------------------------------------- #

def _build(tmp_path, monkeypatch, extra=None):
    """The fixture repo plus `extra` (path -> text), built with the IaC
    adapter."""
    from ozagia.integrations.iac.adapter import clear_parse_cache
    clear_parse_cache()
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    unit = root / "deploy/systemd/backup.service"
    unit.write_text(unit.read_text().replace("@ROOT@", str(root.resolve())))
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    for rel, text in (extra or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "add", ".")
    _git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "fixture")
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    return root, DevMemory(dump, tmp_path / "journal.jsonl", root=root), dump


def _diagnostics(memory):
    return [(n["attrs"].get("path"), n["attrs"].get("message"))
            for n in memory.nodes.values() if n["type"] == "InfraDiagnostic"]


def test_malformed_shapes_do_not_raise():
    pytest.importorskip("yaml")
    graph = parse_compose(
        "services:\n  a:\n    image: x:1\n    networks:\n      - {foo: bar}\n"
        "      - [1, 2]\n      - back\n", "c/compose.yaml")
    assert "ops:compose/c/compose.yaml#network/back" in _keys(graph)
    graph = parse_containerfile(
        "FROM alpine AS a\nCOPY --from=\u00b2 /x /y\n"
        "RUN --mount=type=cache,from=\u00b2 true\n", "Containerfile")
    assert "ops:containerfile/Containerfile#stage/a" in _keys(graph)


def test_one_bad_source_is_a_diagnostic(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    from ozagia.core import ops_graph

    real = ops_graph.parse_containerfile

    def boom(text, rel):
        if rel == "app/worker/Dockerfile":
            raise RuntimeError("parser bug")
        return real(text, rel)

    monkeypatch.setattr(ops_graph, "parse_containerfile", boom)
    _root, memory, _dump = _build(tmp_path, monkeypatch, {
        "bad/compose.yaml": "services:\n  a:\n    networks:\n      - {foo: bar}\n",
        "bad/Containerfile": "FROM alpine AS a\nCOPY --from=\u00b2 /x /y\n",
    })
    ops = _ops(memory)
    assert "ops:compose/app/compose.yaml#service/web" in ops
    assert "ops:compose/bad/compose.yaml#service/a" in ops
    assert "ops:containerfile/bad/Containerfile#stage/a" in ops
    assert "ops:containerfile/app/worker/Dockerfile" not in ops
    assert ("app/worker/Dockerfile", "operations source could not be parsed") in \
        _diagnostics(memory)


def test_build_ops_graph_isolates_parse_errors(tmp_path):
    from ozagia.integrations.iac.ops import build_ops_graph

    def parse(rel, text):
        if rel == "b.service":
            raise ValueError("bad")
        return parse_unit(text, rel)

    graph = build_ops_graph(tmp_path, {"a.service": "[Service]\nExecStart=/bin/a\n",
                                       "b.service": "[Service]\n"}, parse=parse)
    assert [n.key for n in graph.nodes] == ["ops:unit/a.service"]
    assert graph.diagnostics == [("b.service", "operations source could not be parsed")]


def test_long_lines_parse_in_linear_time():
    import time
    from ozagia.core.ops_graph import _scan_compose
    start = time.monotonic()
    # Quadratic for the old key regex; now over the line cap, then skipped.
    _scan_compose("services:\n  a:" + " " * 200_000 + "x" + " " * 200_000 + "y\n")
    # Under the cap: still linear.
    services, _ = _scan_compose("services:\n  a:\n    image:" + " " * 2000 + "x"
                                + " " * 2000 + "y\n")
    assert services["a"]["image"] == "x" + " " * 2000 + "y"
    # Under the Containerfile instruction cap; quadratic for the old regex.
    parse_containerfile("FROM alpine AS a\nRUN " + "--mount= " * 7000 + "true\n",
                        "Containerfile")
    graph = parse_containerfile("FROM alpine AS a\nRUN " + "--mount=" * 20_000
                                + "\n", "Containerfile")
    assert any("too long" in msg for _p, msg in graph.diagnostics)
    assert time.monotonic() - start < 3
    graph = parse_containerfile(
        "FROM alpine AS a\nFROM alpine AS b\n"
        "RUN --mount=type=cache,target=/x --mount=type=bind,from=a,target=/y true\n",
        "Containerfile")
    assert ("ops:containerfile/Containerfile#stage/b", "mounts_from",
            "ops:containerfile/Containerfile#stage/a") in _edges(graph)


def test_env_assignment_first_words_are_skipped():
    pytest.importorskip("yaml")
    graph = parse_compose(
        "services:\n  a:\n    entrypoint: \"DB_PASS=hunter2abc ./run --x\"\n"
        "  b:\n    entrypoint: [\"MYKEY=hunter2qqq\", \"/app\"]\n"
        "  c:\n    entrypoint: \"env -u X A=hunter2b /bin/prog\"\n"
        "  d:\n    entrypoint: \"A=hunter2d\"\n", "compose.yaml")
    nodes = _keys(graph)
    assert nodes["ops:compose/compose.yaml#service/a"].attrs["entrypoint"] == "./run"
    assert nodes["ops:compose/compose.yaml#service/b"].attrs["entrypoint"] == "/app"
    assert nodes["ops:compose/compose.yaml#service/c"].attrs["entrypoint"] == "/bin/prog"
    assert "entrypoint" not in nodes["ops:compose/compose.yaml#service/d"].attrs
    quadlet = parse_quadlet("[Container]\nImage=x:1\nExec=MYKEY=hunter2qqq /app\n",
                            "q/a.container")
    assert quadlet.nodes[0].attrs["exec"] == ["/app"]
    cf = parse_containerfile("FROM alpine\nCMD DB_PASS=hunter2xyz psql -c x\n"
                             "ENTRYPOINT [\"K=hunter2e\", \"/entry\"]\n", "Containerfile")
    stage = _keys(cf)["ops:containerfile/Containerfile#stage/0"]
    assert stage.attrs["cmd"] == "psql" and stage.attrs["entrypoint"] == "/entry"
    unit = parse_unit("[Service]\nExecStart=TOKEN_X=hunter2u /usr/bin/app\n", "a.service")
    assert unit.nodes[0].attrs["exec"] == ["/usr/bin/app"]
    dumped = json.dumps([g.to_dict() for g in (graph, quadlet, cf, unit)])
    assert "hunter2" not in dumped


def test_env_files_are_not_hashed(ops_repo):
    import hashlib
    root, memory, dump = ops_repo
    digest = hashlib.sha256((root / "deploy/web.env").read_bytes()).hexdigest()
    assert digest not in dump.read_text()
    stamp = memory._adapter_info["stamps"]["deploy/web.env"]
    assert stamp is not None and stamp[2] is None
    # Size/mtime stamps still notice an edit.
    from ozagia.core.reingest import _stamp_changed
    assert not _stamp_changed(root, "deploy/web.env", list(stamp))
    (root / "deploy/web.env").write_text("API_TOKEN=other-value-here\n")
    assert _stamp_changed(root, "deploy/web.env", list(stamp))


def test_build_files_respect_skip_and_env_files(tmp_path):
    from ozagia.integrations.iac.ops import build_ops_graph
    (tmp_path / "skipme").mkdir()
    (tmp_path / "skipme" / "Build.df").write_text("FROM alpine\n")
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "Build.df").write_text("FROM alpine\n")
    (tmp_path / "ok" / ".env").write_text("FROM CANARY_ENV_READ\n")
    (tmp_path / "skipme" / "x.env").write_text("A=1\n")
    compose = ("services:\n"
               "  a:\n    build: {context: ./skipme, dockerfile: Build.df}\n"
               "    env_file: skipme/x.env\n"
               "  b:\n    build: {context: ./ok, dockerfile: Build.df}\n"
               "  c:\n    build: {context: ./ok, dockerfile: .env}\n")
    pytest.importorskip("yaml")
    graph = build_ops_graph(tmp_path, {"compose.yaml": compose},
                            skipped=lambda rel: rel.startswith("skipme"))
    keys = {n.key for n in graph.nodes}
    assert "ops:containerfile/ok/Build.df" in keys
    assert "ops:containerfile/skipme/Build.df" not in keys
    assert "ops:containerfile/ok/.env" not in keys
    assert not graph.file_links
    assert "CANARY_ENV_READ" not in json.dumps(graph.to_dict())


def test_redaction_keeps_mixed_case_paths_and_images():
    assert not looks_secret("/home/User1/projects/MyService/bin/run")
    assert clean("/home/User1/projects/MyService/bin/run") == \
        "/home/User1/projects/MyService/bin/run"
    assert normalize_image("ghcr.io/AcmeCorp2/PaymentsGatewayService/api:1") == \
        "ghcr.io/AcmeCorp2/PaymentsGatewayService/api:1"
    # Base64 secrets with `/` are still caught.
    for secret in ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                   "dGhpcyBpcyBh/c2VjcmV0+dmFsdWUxMjM0NTY3ODkw",
                   "abc/Zx9Kq2Lm8Pw4Rt6Yv1Nb3Hc5Jd7Fg0SaQe/def"):
        assert looks_secret(secret), secret


def test_yaml_loader_allows_aliases_but_bounds_merges():
    pytest.importorskip("yaml")
    import time
    from ozagia.core.ops_graph import _load_yaml
    value, error = _load_yaml("x: &c {a: 1}\nservices:\n  w:\n    <<: *c\n    b: [*c, *c]\n")
    assert error is None and value["services"]["w"]["a"] == 1
    lines = ["l0: &l0 {" + ", ".join(f"k{i}: 1" for i in range(20)) + "}"]
    for level in range(1, 40):
        lines.append(f"l{level}: &l{level} {{<<: [*l{level - 1}, *l{level - 1}], "
                     f"m{level}: 1}}")
    start = time.monotonic()
    value, error = _load_yaml("\n".join(lines) + "\nservices: {}\n")
    # PyYAML 6.0.4 deduplicates merge keys while flattening, so the same
    # document now loads in linear time instead of charging the node budget.
    # Older versions stop at the budget. Either way the load is bounded.
    if error is None:
        assert value is not None and value["l39"]["m39"] == 1
    else:
        assert value is None and error == "YAML node limit exceeded"
    assert time.monotonic() - start < 5
    graph = parse_compose("\n".join(lines) + "\nservices:\n  a:\n    image: x:1\n",
                          "compose.yaml")
    if error is None:
        assert not any("node limit" in msg for _p, msg in graph.diagnostics)
    else:
        assert any("node limit" in msg for _p, msg in graph.diagnostics)
    assert "ops:compose/compose.yaml#service/a" in _keys(graph)


def test_ops_parse_cache_reuses_unchanged_sources(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    from ozagia.core import ops_graph
    root, memory, dump = _build(tmp_path, monkeypatch)
    first_edges = (_edge_set(memory), _edge_set(memory, "ops_"))
    calls = []
    real = ops_graph.parse_ops_source

    def counted(text, rel, fmt=None):
        calls.append(rel)
        return real(text, rel, fmt)

    monkeypatch.setattr(ops_graph, "parse_ops_source", counted)
    again = tmp_path / "again.json"
    build_index(root, again, include_docs="none", include_git=False)
    assert calls == []
    second = DevMemory(again, tmp_path / "again.jsonl", root=root)
    # Cached per-file graphs are not mutated by resolution.
    assert (_edge_set(second), _edge_set(second, "ops_")) == first_edges
    unit = root / "deploy/systemd/backup.service"
    unit.write_text(unit.read_text() + "\n# edited\n")
    build_index(root, again, include_docs="none", include_git=False)
    assert calls == ["deploy/systemd/backup.service"]


def test_ops_sources_have_their_own_budget(tmp_path, monkeypatch):
    from ozagia.core.infra_discovery import iter_candidate_paths
    for i in range(5):
        (tmp_path / f"a{i}.service").write_text("[Service]\nExecStart=/bin/x\n")
    (tmp_path / "z1.tf").write_text('resource "x" "y" {}')
    (tmp_path / "z2.tf").write_text('resource "x" "z" {}')
    found = list(iter_candidate_paths(tmp_path, max_files=2, max_ops_files=3))
    assert [p for p, k in found if k == "terraform"] == ["z1.tf", "z2.tf"]
    assert len([p for p, k in found if k == "ops"]) == 3
    # The byte budget: ops sources over theirs do not push Terraform out.
    from ozagia.integrations.iac import adapter
    monkeypatch.setattr(adapter, "MAX_OPS_SOURCE_BYTES", 10)
    _root, memory, _dump = _build(tmp_path / "b", monkeypatch,
                                  {"infra/main.tf": 'resource "aws_s3_bucket" "b" {}\n'})
    families = {n["attrs"].get("family") for n in memory.nodes.values()
                if n["type"] == "InfraResource"}
    assert "terraform" in families and "ops" not in families
    assert any("Operations source budget exceeded" in str(m)
               for _p, m in _diagnostics(memory))


def test_compose_logs_match_like_compose(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    _root, memory, _dump = _build(tmp_path, monkeypatch, {
        "svc2/compose.yaml": "services:\n  db:\n    image: postgres:16\n"})

    def after(command, workdir=None):
        inp = {"command": command}
        if workdir:
            inp["workdir"] = workdir
        return intent.after(memory, "shell", inp, "log line\n")["append"]

    # No compose project in the working directory or above it.
    assert "ops:" not in after("docker compose logs db")
    # Outside the repository.
    assert "ops:" not in after("cd /tmp && docker compose logs db")
    assert "ops:" not in after("docker compose -f compose.yaml logs db", "/tmp")
    out = after("cd svc2 && docker compose logs db")
    assert "ops:compose/svc2/compose.yaml#service/db" in out
    assert "ops:compose/app/compose.yaml" not in out
    # A child directory walks up to its parent's project.
    out = after("cd app/static && docker compose logs db")
    assert "ops:compose/app/compose.yaml#service/db" in out
    assert "svc2" not in out
    # No bare-service-name container fallback.
    assert "ops:" not in after("docker logs db")
    out = after("docker logs svc2-db-1")
    assert "ops:compose/svc2/compose.yaml#service/db" in out
    assert "ops:compose/app/compose.yaml" not in out
    # podman's `-n` is `--names`.
    out = after("podman logs -n shop-web")
    assert "ops:quadlet/deploy/containers/systemd/shop-web.container" in out
    assert ", declared at " in out
