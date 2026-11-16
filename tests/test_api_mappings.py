import json
from pathlib import Path

from ozagia.core.memory import Builder
from ozagia.core.api_mappings import index_api_mappings
from ozagia.core.openapi import index_openapi_document


FIXTURE = Path(__file__).parent / "fixtures" / "api_mappings"


def _builder_with_contract():
    builder = Builder()
    contract_file = builder.node("File", {"path": "openapi.json"}, key=("file", "openapi.json"))
    result = index_openapi_document(builder, contract_file, "openapi.json",
                                    (FIXTURE / "contracts/openapi.json").read_text(),
                                    repo="contracts")
    assert result["valid"]
    return builder


def _roots():
    return {"service": FIXTURE / "service", "client": FIXTURE / "client",
            "contracts": FIXTURE / "contracts"}


def test_indexes_explicit_cross_repository_links_with_provenance():
    builder = _builder_with_contract()
    result = index_api_mappings(builder, (FIXTURE / "registry.json").read_text(),
                                "integration/registry.json", _roots())
    assert result == {"valid": True, "mappings": 3, "errors": []}
    links = [node for node in builder.nodes if node["type"] == "ApiMapping"]
    assert {node["attrs"]["relation"] for node in links} == {
        "produces_api", "consumes_api", "tests_api"}
    assert all(node["attrs"]["inference"] is False for node in links)
    assert all(node["attrs"]["provenance"]["source"] == "integration/registry.json"
               for node in links)
    api_operations = {n["id"]: n for n in builder.nodes if n["type"] == "ApiOperation"}
    assert len(builder.edges) == 7
    operation_edges = [e for e in builder.edges if e["type"] == "refers_to_api_operation"]
    assert len(operation_edges) == 3
    assert all(api_operations[e["tgt"]]["attrs"]["repo"] == "contracts"
               for e in operation_edges)


def test_rejects_traversal_unknown_repo_and_missing_paths():
    records = [
        {"relation": "consumes_api", "repo": "client", "path": "../service/src/server.py", "symbol": "x", "api": "a"},
        {"relation": "consumes_api", "repo": "missing", "path": "x.py", "symbol": "x", "api": "a"},
        {"relation": "tests_api", "repo": "client", "path": "absent.py", "symbol": "x", "api": "a"},
    ]
    builder = _builder_with_contract()
    result = index_api_mappings(builder, json.dumps({"version": 1, "mappings": records}),
                                "registry.json", _roots())
    assert result["mappings"] == 0
    assert len(result["errors"]) == 3
    assert not any(n["type"] == "ApiMapping" for n in builder.nodes)


def test_rejects_absolute_and_noncanonical_paths():
    records = [
        {"relation": "consumes_api", "repo": "client", "path": str(FIXTURE / "client/src/client.py"), "symbol": "x", "api": "a"},
        {"relation": "consumes_api", "repo": "client", "path": "src/./client.py", "symbol": "x", "api": "a"},
        {"relation": "consumes_api", "repo": "client", "path": "src\\client.py", "symbol": "x", "api": "a"},
    ]
    result = index_api_mappings(_builder_with_contract(), json.dumps({"version":  1, "mappings": records}),
                                "registry.json", _roots())
    assert result["mappings"] == 0
    assert len(result["errors"]) == 3


def test_rejects_symlink_escape_and_missing_repository_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    (root / "escape.py").symlink_to(outside)
    records = [
        {"relation": "consumes_api", "repo": "client", "path": "escape.py", "symbol": "x", "api": "a"},
        {"relation": "consumes_api", "repo": "absent", "path": "x.py", "symbol": "x", "api": "a"},
    ]
    result = index_api_mappings(_builder_with_contract(), json.dumps({"version": 1, "mappings": records}),
                                "registry.json", {"client": root, "absent": tmp_path / "missing",
                                                  "contracts": FIXTURE / "contracts"})
    assert result["mappings"] == 0
    assert len(result["errors"]) == 2


def test_repeated_endpoint_keeps_provenance_per_mapping():
    row = {"relation": "consumes_api", "repo": "client", "path": "src/client.py",
           "symbol": "fetch_pets", "api": "contracts:openapi.json#listPets"}
    builder = _builder_with_contract()
    result = index_api_mappings(builder, json.dumps({"version": 1, "mappings": [row, row]}),
                                "registry.json", _roots())
    mappings = [n for n in builder.nodes if n["type"] == "ApiMapping"]
    refs = [n for n in builder.nodes if n["type"] == "ApiCodeReference"]
    assert result["mappings"] == 2
    assert len(mappings) == 2
    assert [n["attrs"]["provenance"]["pointer"] for n in mappings] == [
        "#/mappings/0", "#/mappings/1"]
    assert len(refs) == 1
    assert "provenance" not in refs[0]["attrs"]


def test_invalid_envelope_does_not_modify_graph():
    builder = Builder()
    result = index_api_mappings(builder, '{"version": 2, "mappings": []}',
                                "registry.json", {})
    assert result["valid"] is False
    assert builder.nodes == []


def test_invalid_relation_type_is_reported():
    builder = Builder()
    record = {"relation": [], "repo": "client", "path": "a.py",
              "symbol": "x", "api": "api"}
    builder = _builder_with_contract()
    result = index_api_mappings(builder, json.dumps({"version": 1, "mappings": [record]}),
                                "registry.json", _roots())
    assert result["errors"] == ["#/mappings/0:invalid_fields"]


def test_requires_matching_indexed_openapi_operation():
    builder = _builder_with_contract()
    row = {"relation": "consumes_api", "repo": "client", "path": "src/client.py",
           "symbol": "fetch_pets", "api": "contracts:openapi.json#missing"}
    result = index_api_mappings(builder, json.dumps({"version": 1, "mappings": [row]}),
                                "registry.json", _roots())
    assert result["mappings"] == 0
    assert result["errors"] == ["#/mappings/0:api_operation_not_indexed"]
    assert not any(e["type"] == "refers_to_api_operation" for e in builder.edges)


def test_ambiguous_operation_id_is_rejected():
    builder = _builder_with_contract()
    operation = next(n for n in builder.nodes if n["type"] == "ApiOperation")
    duplicate = builder.node("ApiOperation", dict(operation["attrs"]),
                             key=("api_operation", "duplicate"))
    assert duplicate != operation["id"]
    row = {"relation": "tests_api", "repo": "client", "path": "tests/test_client.py",
           "symbol": "test_fetch_pets", "api": "contracts:openapi.json#listPets"}
    result = index_api_mappings(builder, json.dumps({"version": 1, "mappings": [row]}),
                                "registry.json", _roots())
    assert result["mappings"] == 0
    assert result["errors"] == ["#/mappings/0:api_operation_ambiguous"]


def test_malformed_and_uncontained_api_references_are_rejected():
    rows = [
        {"relation": "consumes_api", "repo": "client", "path": "src/client.py",
         "symbol": "fetch_pets", "api": "contracts:openapi.json"},
        {"relation": "consumes_api", "repo": "client", "path": "src/client.py",
         "symbol": "fetch_pets", "api": "contracts:../client/src/client.py#fetch"},
        {"relation": "consumes_api", "repo": "client", "path": "src/client.py",
         "symbol": "fetch_pets", "api": "missing:openapi.json#listPets"},
    ]
    result = index_api_mappings(_builder_with_contract(),
                                json.dumps({"version": 1, "mappings": rows}),
                                "registry.json", _roots())
    assert result["mappings"] == 0
    assert result["errors"] == ["#/mappings/0:invalid_api_reference",
                                "#/mappings/1:unsafe_or_missing_api_path",
                                "#/mappings/2:unknown_api_repo"]


def test_excessively_nested_registry_reports_invalid_json(monkeypatch):
    def reject_deep_input(_text):
        raise RecursionError("nested input")

    monkeypatch.setattr("ozagia.core.api_mappings.json.loads", reject_deep_input)
    result = index_api_mappings(Builder(), "[]", "registry.json", {})
    assert result == {"valid": False, "mappings": 0, "errors": ["invalid_json"]}
