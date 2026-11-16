from __future__ import annotations

from ozagia.core.infra_query import context
from ozagia.core.proactive_context import build_context


class Memory:
    _index_version = 1
    nodes = {
        1: {"type": "InfraResource", "key": "infra:payments", "attrs": {
            "name": "payments", "kind": "Deployment", "path": "deploy/app.yaml",
            "pointer": "/documents/0"}},
        2: {"type": "InfraResource", "key": "infra:payments-service", "attrs": {
            "name": "payments-service", "kind": "Service", "path": "deploy/svc.yaml",
            "pointer": "/documents/0"}},
    }
    _out = {1: [{"src": 1, "tgt": 2, "type": "selector",
                "attrs": {"provenance": "inferred"}}]}
    _in = {2: [{"src": 1, "tgt": 2, "type": "selector",
               "attrs": {"provenance": "inferred"}}]}

    def staleness(self):
        return {"freshness": "fresh"}

    def code_search(self, *args, **kwargs):
        return {"results": []}

    def nodes_of_type(self, type_name):
        return [nid for nid, node in self.nodes.items()
                if node.get("type") == type_name]

    def node_by_key(self, key):
        return next((nid for nid, node in self.nodes.items()
                     if node.get("key") == key), None)


class CodeMemory(Memory):
    def __init__(self, *, code=True):
        self.code = code

    def code_search(self, query, *, limit, include_snippet):
        if not self.code:
            return {"ok": True, "results": []}
        return {"ok": True, "results": [
            {"path": f"src/model_{i}.py", "snippet": "payment model " + "x" * 80,
             "line": 4, "symbols": [{"name": "Payment", "line": 9}]}
            for i in range(6)]}


class ManyInfraMemory(Memory):
    nodes = {
        i: {"type": "InfraResource", "key": f"infra:payments-{i}", "attrs": {
            "name": f"payments-{i}", "kind": "ConfigMap",
            "path": f"deploy/config-{i}.yaml", "pointer": "/documents/0"}}
        for i in range(1, 13)
    }
    _out: dict = {}

    def code_search(self, *args, **kwargs):
        return {"ok": True, "results": []}


class RankMemory:
    nodes = {
        1: {"type": "InfraResource", "key": "infra:payments-api", "attrs": {
            "name": "payments-api", "kind": "Deployment",
            "path": "deploy/payments.yaml"}},
        2: {"type": "InfraResource", "key": "infra:ledger", "attrs": {
            "name": "ledger", "kind": "StatefulSet", "path": "deploy/ledger.yaml"}},
        3: {"type": "InfraResource", "key": "infra:helper", "attrs": {
            "name": "helper", "kind": "Service", "path": "deploy/workloads/helper.yaml"}},
    }
    _out: dict = {}

    def staleness(self):
        return {"freshness": "fresh"}

    def code_search(self, *args, **kwargs):
        return {"ok": True, "results": []}


class AddressMemory:
    nodes = {
        1: {"type": "InfraResource", "key": "infra:module-net", "attrs": {
            "name": "net", "address": "net", "aliases": "module.net",
            "kind": "module", "family": "terraform", "path": "main.tf",
            "line": 2}},
        2: {"type": "InfraResource", "key": "infra:subnet", "attrs": {
            "name": "aws_subnet.private", "address": "aws_subnet.private",
            "kind": "resource", "family": "terraform", "path": "subnet.tf",
            "line": 1}},
    }
    _out: dict = {}

    def staleness(self):
        return {"freshness": "fresh"}

    def code_search(self, *args, **kwargs):
        return {"ok": True, "results": []}


class FamilyMemory:
    nodes = {
        1: {"type": "InfraResource", "key": "infra:tf", "attrs": {
            "name": "payments", "kind": "resource", "family": "terraform",
            "path": "infra/main.tf", "line": 1}},
        2: {"type": "InfraResource", "key": "infra:k8s", "attrs": {
            "name": "payments", "kind": "Deployment", "family": "kubernetes",
            "path": "k8s/app.yaml", "pointer": "/documents/0"}},
    }
    _out: dict = {}

    def staleness(self):
        return {"freshness": "fresh"}

    def code_search(self, *args, **kwargs):
        return {"ok": True, "results": []}


class AmbiguousMemory:
    nodes = {
        1: {"type": "InfraResource", "key": "infra:a", "attrs": {
            "name": "payments", "kind": "Deployment", "path": "one/app.yaml",
            "pointer": "/documents/0"}},
        2: {"type": "InfraResource", "key": "infra:b", "attrs": {
            "name": "payments", "kind": "Deployment", "path": "two/app.yaml",
            "pointer": "/documents/0"}},
    }
    _out: dict = {}

    def staleness(self):
        return {"freshness": "fresh"}

    def code_search(self, *args, **kwargs):
        return {"ok": True, "results": []}


def test_infrastructure_context_is_attributed_bounded_and_uses_history():
    result = build_context(Memory(), "Check configuration", ["payments"], budget_tokens=80)
    assert "deploy/app.yaml#/documents/0" in result["context"]
    assert "Service payments-service (inferred)" in result["context"]
    assert "infra:payments" in result["keys"]
    assert len(result["context"]) <= 320
    assert result["estimated_tokens"] <= 80


def test_unrelated_request_does_not_emit_infrastructure():
    assert not build_context(Memory(), "Fix invoices")["context"]


def test_shared_budget_keeps_code_and_infrastructure_under_one_cap():
    result = build_context(CodeMemory(), "payments model", budget_tokens=100)
    assert "[infra]" in result["context"]
    assert "[ozagia]" in result["context"]
    assert len(result["context"]) <= 400
    allocation = result["allocation"]
    assert allocation["infra_chars"] > 0
    assert allocation["code_chars"] > 0
    assert allocation["infra_chars"] <= 180
    assert allocation["infra_chars"] + allocation["code_chars"] <= 400
    assert any(key.startswith("file:") for key in result["keys"])
    assert any(key.startswith("infra:") for key in result["keys"])


def test_infrastructure_only_hint_can_use_the_whole_unused_budget():
    result = build_context(ManyInfraMemory(), "payments", budget_tokens=100)
    assert result["allocation"]["code_chars"] == 0
    assert result["allocation"]["infra_chars"] > 180
    assert len(result["context"]) <= 400
    assert result["context"].count("[infra]") > 3


def test_current_query_outranks_history_and_history_alone_is_weak():
    current = context(RankMemory(), "payments", history=["ledger"], max_chars=800)
    rows = current["results"]
    assert rows[0]["label"] == "payments-api"
    assert {row["label"] for row in rows} == {"payments-api", "ledger"}
    history_only = context(RankMemory(), "check configuration", history=["ledger"],
                           max_chars=800)
    assert history_only["results"]
    assert history_only["results"][0]["label"] == "ledger"


def test_bare_path_coincidence_is_not_enough_for_a_hint():
    result = context(RankMemory(), "workloads", max_chars=800)
    assert result["context"] == ""
    assert result["reason"] == "no matching infrastructure declarations"


def test_identifier_components_do_not_match_inside_other_words():
    from ozagia.core.infra_query import locate

    # ``net`` must not match ``subnet`` by raw substring, while the Terraform
    # module address alias remains directly addressable.
    assert context(AddressMemory(), "net", max_chars=800)["results"][0]["label"] == "net"
    assert all(row["label"] != "aws_subnet.private"
               for row in context(AddressMemory(), "net", max_chars=800)["results"])
    located = locate(AddressMemory(), "module.net")
    assert [row["name"] for row in located["results"]] == ["net"]
    qualified = context(AddressMemory(), "aws_subnet.private.id", max_chars=800)
    assert qualified["results"][0]["label"] == "aws_subnet.private"


def test_explicit_technology_terms_bias_equal_matches():
    terraform = context(FamilyMemory(), "terraform payments", max_chars=800)
    kubernetes = context(FamilyMemory(), "k8s payments", max_chars=800)
    assert terraform["results"][0]["family"] == "terraform"
    assert kubernetes["results"][0]["family"] == "kubernetes"


def test_duplicate_declarations_are_reported_as_ambiguous():
    result = context(AmbiguousMemory(), "payments", max_chars=800)
    assert result["ambiguous"] is True
    assert result["ambiguity"][0]["count"] == 2
    assert len(result["results"]) == 2


def test_query_cache_is_bounded_by_graph_version_and_returns_copies(monkeypatch):
    from ozagia.core import infra_query

    memory = Memory()
    calls = []
    original = infra_query._score_node

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(infra_query, "_score_node", counted)
    first = infra_query.context(memory, "payments", max_chars=800)
    count = len(calls)
    second = infra_query.context(memory, "payments", max_chars=800)
    assert len(calls) == count
    assert second["context"] == first["context"]
    first["context"] = "mutated by caller"
    assert second["context"] != "mutated by caller"
    stats = infra_query.cache_stats(memory)
    assert stats["hits"] >= 1 and stats["misses"] >= 1
    memory._index_version += 1
    infra_query.context(memory, "payments", max_chars=800)
    assert len(calls) > count


def test_locate_cache_and_eviction(monkeypatch):
    from ozagia.core import infra_query

    monkeypatch.setattr(infra_query, "QUERY_CACHE_MAX", 2)
    memory = Memory()
    first = infra_query.locate(memory, "payments")
    second = infra_query.locate(memory, "payments")
    assert first == second
    assert infra_query.cache_stats(memory)["hits"] == 1
    infra_query.locate(memory, "service")
    infra_query.locate(memory, "payments-api")
    assert infra_query.cache_stats(memory)["entries"] <= 2


def test_relationship_line_names_the_target_and_avoids_inbound_duplicate():
    result = context(Memory(), "payments", max_chars=800)
    edge_lines = [line for line in result["context"].splitlines()
                  if "selector ->" in line]
    assert len(edge_lines) == 1
    assert "Service payments-service" in edge_lines[0]
    assert "deploy/svc.yaml#/documents/0" not in edge_lines[0]
    assert not any("<- selector" in line for line in result["context"].splitlines())
