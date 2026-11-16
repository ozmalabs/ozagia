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
    assert "[ozagia] ops:compose/app/compose.yaml#service/web: compose service web (app/compose.yaml:5)" in out["append"]
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
            "backup.service (deploy/systemd/backup.service:1); runs "
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
    assert log_requests(["podman", "--remote", "logs", "-n", "5", "a", "b"]) == [
        ("container", "a"), ("container", "b")]
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
