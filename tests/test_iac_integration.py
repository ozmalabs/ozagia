from __future__ import annotations

import json
import subprocess

from ozagia.core.memory import DevMemory, build_index
from ozagia.core.proactive_context import build_context


def test_opt_in_graph_context_reingests_and_excludes_values(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    (root / ".gitignore").write_text("ignored.tf\n")
    terraform = root / "main.tf"
    terraform.write_text('resource "aws_vpc" "payments" {\n  description = "CANARY_SECRET"\n}\n')
    manifest = root / "deployment.json"
    manifest.write_text(json.dumps({"apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "payments", "namespace": "prod"},
        "spec": {"template": {
            "metadata": {"labels": {"app": "payments"}},
            "spec": {"containers": [{"name": "app",
                "env": [{"name": "TOKEN", "value": "CANARY_SECRET"}]}]}}}}))
    (root / "service.json").write_text(json.dumps({
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "payments", "namespace": "prod"},
        "spec": {"selector": {"app": "payments"}}}))
    (root / "stack.json").write_text(json.dumps({"AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {"PaymentsBucket": {"Type": "AWS::S3::Bucket",
            "Properties": {"BucketName": "CANARY_SECRET"}}}}))
    (root / "Pulumi.yaml").write_text("name: sample\nruntime: yaml\nresources:\n"
                                       "  app:\n    type: aws:s3:Bucket\n"
                                       "    properties:\n      secret: CANARY_SECRET\n")
    (root / "terraform.tfstate").write_text('{"secret":"CANARY_SECRET"}')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c",
                    "user.email=t@example.invalid", "commit", "-qm", "fixture"], check=True)
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    attrs = [n["attrs"] for n in memory.nodes.values() if n["type"] == "InfraResource"]
    assert {n["kind"] for n in attrs} >= {"resource", "Deployment"}
    assert any(n["family"] == "cloudformation" and n["name"] == "PaymentsBucket" for n in attrs)
    assert any(n["family"] == "pulumi" and n["name"] == "app" for n in attrs)
    assert "CANARY_SECRET" not in dump.read_text()
    file_id = memory.node_by_key("file:deployment.json")
    assert file_id is not None
    assert "CANARY_SECRET" not in json.dumps(memory.snippet(file_id))
    result = build_context(memory, "payments", budget_tokens=200)
    assert "main.tf" in result["context"] and "deployment.json" in result["context"]
    assert "CANARY_SECRET" not in result["context"]
    assert len(result["context"]) <= 800
    assert result["allocation"]["budget_chars"] == 800
    assert result["allocation"]["infra_chars"] > 0
    assert result["allocation"]["infra_chars"] <= 360
    (root / "ignored.tf").write_text('resource "aws_vpc" "ignored" {}\n')
    memory._staleness_memo = None
    assert memory.staleness()["freshness"] != "fresh"
    memory.refresh()
    located = memory.adapter_query("infra.locate", query="payments")
    assert located["freshness"] == "fresh" and located["ambiguous"]
    key = located["results"][0]["key"]
    assert memory.adapter_query("infra.impact", key=key)["complete"] is False
    from ozagia.integrations.mcp.server import DevMemoryServer
    server = DevMemoryServer(memory)
    assert {"infra_context", "infra_locate", "infra_dependencies", "infra_impact",
            "infra_status", "infra_import_sarif",
            "infra_import_cdk"} <= set(server._handlers)
    assert server._handlers["infra_import_sarif"]({"sarif": "{}"})["diagnostics"]
    cdk = server._handlers["infra_import_cdk"]({
        "manifest": json.dumps({
            "version": "30.0.0",
            "artifacts": {"Stack": {
                "type": "aws:cloudformation:stack",
                "properties": {"templateFile": "Stack.template.json"}}}}),
        "templates": {"Stack.template.json": json.dumps({
            "Resources": {"Bucket": {"Type": "AWS::S3::Bucket"}}})}})
    assert cdk["ok"] is True and cdk["resources"]
    bad_cdk = server._handlers["infra_import_cdk"]({"manifest": "{}"})
    assert bad_cdk["ok"] is False and "error" in bad_cdk
    status = server._handlers["infra_status"]({})
    assert status["resources"] > 0 and status["edges"] > 0
    assert status["families"]["terraform"] > 0
    assert status["complete"] is False
    hint = server._handlers["infra_context"]({
        "query": "payments", "history": ["payments"], "max_chars": 240})
    assert hint["context"] and len(hint["context"]) <= 240
    assert hint["estimated_tokens"] <= 60
    assert "CANARY_SECRET" not in hint["context"]
    (root / "added.tf").write_text('resource "aws_vpc" "added" {}\n')
    memory._staleness_memo = None
    assert memory.staleness()["freshness"] != "fresh"
    assert memory.adapter_query("infra.locate", query="payments")["results"] == []
    memory.refresh()
    assert any(n["type"] == "InfraResource" and n["attrs"]["path"] == "added.tf"
               for n in memory.nodes.values())
    refreshed_hint = build_context(memory, "added", budget_tokens=100)
    assert "added.tf" in refreshed_hint["context"]
    terraform.unlink()
    memory.reingest(["main.tf"])
    assert not any(n["type"] == "InfraResource" and n["attrs"]["path"] == "main.tf"
                   for n in memory.nodes.values())
    assert set(memory.nodes_of_type("InfraResource")) == {
        nid for nid, node in memory.nodes.items()
        if node["type"] == "InfraResource"}
    deleted_hint = build_context(memory, "main", budget_tokens=100)
    assert "main.tf" not in deleted_hint["context"]


def test_renamed_infra_file_replaces_old_nodes_and_edges(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    (root / "main.tf").write_text(
        'resource "aws_vpc" "main" {}\n'
        'resource "aws_subnet" "private" { vpc_id = aws_vpc.main.id }\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c",
                    "user.email=t@example.invalid", "commit", "-qm", "fixture"],
                   check=True)
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    (root / "main.tf").rename(root / "renamed.tf")
    memory._staleness_memo = None
    memory.refresh()
    infra = [(nid, n) for nid, n in memory.nodes.items()
             if n["type"] == "InfraResource"]
    assert {n["attrs"]["path"] for _, n in infra} == {"renamed.tf"}
    assert not any(
        (memory.nodes.get(nid) or {}).get("attrs", {}).get("path") == "main.tf"
        for nid in memory.nodes_of_type("InfraResource"))
    edges = [e for bucket in memory._out.values() for e in bucket
             if e["type"] == "infra_references"]
    assert len(edges) == 1
    assert (memory.nodes[edges[0]["src"]]["attrs"]["path"] == "renamed.tf"
            and memory.nodes[edges[0]["tgt"]]["attrs"]["path"] == "renamed.tf")
    fresh_dump = tmp_path / "fresh.json"
    build_index(root, fresh_dump, include_docs="none", include_git=False)
    fresh = DevMemory(fresh_dump, tmp_path / "fresh-journal.jsonl", root=root)
    refreshed_nodes = {
        (memory.key_of(nid), node["attrs"]["kind"], node["attrs"]["path"])
        for nid in memory.nodes_of_type("InfraResource")
        for node in [memory.nodes[nid]]}
    fresh_nodes = {
        (fresh.key_of(nid), node["attrs"]["kind"], node["attrs"]["path"])
        for nid in fresh.nodes_of_type("InfraResource")
        for node in [fresh.nodes[nid]]}
    assert refreshed_nodes == fresh_nodes
    refreshed_edges = sorted(
        (e["type"], memory.key_of(e["src"]), memory.key_of(e["tgt"]))
        for bucket in memory._out.values() for e in bucket
        if e["type"].startswith("infra_"))
    fresh_edges = sorted(
        (e["type"], fresh.key_of(e["src"]), fresh.key_of(e["tgt"]))
        for bucket in fresh._out.values() for e in bucket
        if e["type"].startswith("infra_"))
    assert refreshed_edges == fresh_edges


def test_new_infra_file_discovered_on_refresh(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    (root / "new.tf").write_text('resource "aws_vpc" "new" {}\n')
    memory.refresh()
    assert any(n["type"] == "InfraResource" and n["attrs"]["path"] == "new.tf"
               for n in memory.nodes.values())


def test_source_budget_reports_incomplete_without_rediscovering_files(tmp_path, monkeypatch):
    from ozagia.integrations.iac import adapter

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    (root / "main.tf").write_text('resource "aws_vpc" "example" {}\n')
    (root / "other.tf").write_text('resource "aws_vpc" "other" {}\n')
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.setattr(adapter, "MAX_TOTAL_SOURCE_BYTES", 1)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    assert any("budget exceeded" in n.get("attrs", {}).get("message", "")
               for n in memory.nodes.values() if n["type"] == "InfraDiagnostic")
    assert {"main.tf", "other.tf"} <= set(memory._adapter_info["files"])
    assert not any(n["type"] == "InfraResource" for n in memory.nodes.values())


def test_terraform_address_aliases_are_queryable(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    (root / "main.tf").write_text(
        'variable "region" { default = "x" }\n'
        'module "child" { source = "./child" }\n')
    (root / "child").mkdir()
    (root / "child" / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c",
                    "user.email=t@example.invalid", "commit", "-qm", "fixture"],
                   check=True)
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    assert memory.adapter_query("infra.locate", query="var.region")["results"]
    assert memory.adapter_query("infra.locate", query="module.child")["results"]
    dependencies = memory.adapter_query(
        "infra.dependencies",
        key=memory.adapter_query("infra.locate", query="module.child")["results"][0]["key"],
        depth=1)
    assert any(edge["relation"] == "infra_module_source"
               and edge.get("evidence") == "child" for edge in dependencies["edges"])


def test_parse_cache_reuses_unchanged_files_and_reparses_changed(tmp_path, monkeypatch):
    from ozagia.core import kubernetes_graph, terraform_graph
    from ozagia.integrations.iac import adapter

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    (root / "main.tf").write_text('resource "aws_vpc" "main" {}\n')
    (root / "service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\n")
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    calls = {"terraform": 0, "kubernetes": 0}
    original_tf = terraform_graph.index_terraform_text
    original_kube = kubernetes_graph.parse_kubernetes_source

    def counted_tf(*args, **kwargs):
        calls["terraform"] += 1
        return original_tf(*args, **kwargs)

    def counted_kube(*args, **kwargs):
        calls["kubernetes"] += 1
        return original_kube(*args, **kwargs)

    monkeypatch.setattr(terraform_graph, "index_terraform_text", counted_tf)
    monkeypatch.setattr(kubernetes_graph, "parse_kubernetes_source", counted_kube)
    monkeypatch.setattr(adapter, "PARSE_CACHE_MAX", 16)
    build_index(root, tmp_path / "one.json", include_docs="none", include_git=False)
    assert calls == {"terraform": 1, "kubernetes": 1}
    build_index(root, tmp_path / "two.json", include_docs="none", include_git=False)
    assert calls == {"terraform": 1, "kubernetes": 1}
    (root / "main.tf").write_text('resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }\n')
    build_index(root, tmp_path / "three.json", include_docs="none", include_git=False)
    assert calls == {"terraform": 2, "kubernetes": 1}


def test_adapter_discovers_candidates_once_per_build(tmp_path, monkeypatch):
    from ozagia.integrations.iac import adapter

    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n')
    (root / "main.tf").write_text('resource "aws_vpc" "example" {}\n')
    (root / "service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: api\n")
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    calls = []
    original = adapter.iter_candidate_paths

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "iter_candidate_paths", counted)
    build_index(root, tmp_path / "graph.json", include_docs="none",
                include_git=False)
    assert len(calls) == 1
