import json

import pytest

from ozagia.core.memory import Builder
from ozagia.core.openapi import index_openapi_document


def test_indexes_json_openapi_operations_and_component_schema_links():
    builder = Builder()
    file_node = builder.node("File", {"path": "api/openapi.json"},
                             key=("file", "api/openapi.json"))
    document = {
        "openapi": "3.1.0",
        "components": {"schemas": {
            "Pet": {"type": "object", "description": "A pet"},
        }},
        "paths": {"/pets": {"get": {
            "operationId": "listPets", "summary": "List pets",
            "responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Pet"},
            }}}},
        }, "parameters": []}},
    }

    result = index_openapi_document(builder, file_node, "api/openapi.json",
                                    json.dumps(document))

    assert result == {"valid": True, "operations": 1, "schemas": 1,
                      "unresolved_references": [], "external_references": [],
                      "unsupported_references": []}
    operation = next(n for n in builder.nodes if n["type"] == "ApiOperation")
    assert operation["attrs"]["operation_id"] == "listPets"
    schema = next(n for n in builder.nodes if n["type"] == "ApiSchema")
    assert schema["attrs"]["name"] == "Pet"
    assert any(e["src"] == operation["id"] and e["tgt"] == schema["id"]
               and e["type"] == "uses_api_schema" for e in builder.edges)


def test_ignores_invalid_and_non_openapi_documents():
    builder = Builder()
    assert index_openapi_document(builder, 0, "x.json", "{")["valid"] is False
    assert index_openapi_document(builder, 0, "x.json", '{"paths":{}}')["valid"] is False
    assert builder.nodes == []


def test_reports_unresolved_and_external_references_without_dangling_edges():
    builder = Builder()
    file_node = builder.node("File", {"path": "api.json"}, key=("file", "api.json"))
    document = {"openapi": "3.0.3", "paths": {"/x": {"get": {
        "responses": {
            "200": {"schema": {"$ref": "#/components/schemas/Missing"}},
            "201": {"schema": {"$ref": "common.json#/components/schemas/Shared"}},
        },
    }}}}

    result = index_openapi_document(builder, file_node, "api.json",
                                    json.dumps(document))

    assert result["unresolved_references"] == ["#/components/schemas/Missing"]
    assert result["external_references"] == [
        "common.json#/components/schemas/Shared"]
    assert result["unsupported_references"] == []
    assert all(edge["tgt"] in {node["id"] for node in builder.nodes}
               for edge in builder.edges)


def test_supports_swagger_20_definition_references():
    builder = Builder()
    file_node = builder.node("File", {"path": "api.json"}, key=("file", "api.json"))
    document = {"swagger": "2.0", "definitions": {"Pet": {"type": "object"}},
                "paths": {"/x": {"get": {"responses": {
                    "200": {"schema": {"$ref": "#/definitions/Pet"}},
                }}}}}
    result = index_openapi_document(builder, file_node, "api.json",
                                    json.dumps(document))
    assert result["valid"] is True
    assert result["unresolved_references"] == []
    assert result["schemas"] == 1
    schema = next(n for n in builder.nodes if n["type"] == "ApiSchema")
    assert schema["attrs"]["source_pointer"] == "#/definitions/Pet"
    assert any(edge["type"] == "uses_api_schema" for edge in builder.edges)


def test_reports_local_parameter_refs_as_unsupported_and_adds_source_pointers():
    builder = Builder()
    file_node = builder.node("File", {"path": "api.json"}, key=("file", "api.json"))
    document = {"openapi": "3.0.3", "paths": {"/pets/{id}": {"get": {
        "parameters": [{"$ref": "#/components/parameters/PetId"}],
        "responses": {"200": {"description": "ok"}},
    }}}}

    result = index_openapi_document(builder, file_node, "api.json",
                                    json.dumps(document))

    operation = next(n for n in builder.nodes if n["type"] == "ApiOperation")
    assert operation["attrs"]["source_pointer"] == "#/paths/~1pets~1{id}/get"
    assert result["unsupported_references"] == [
        "#/components/parameters/PetId"]



def test_schema_name_with_escaped_slash_is_resolved():
    builder = Builder()
    file_node = builder.node("File", {"path": "api.json"}, key=("file", "api.json"))
    document = {"openapi": "3.0.0", "components": {"schemas": {"A/B": {}}},
                "paths": {"/x": {"get": {"responses": {"200": {
                    "schema": {"$ref": "#/components/schemas/A~1B"}}}}}}}
    result = index_openapi_document(builder, file_node, "api.json", json.dumps(document))
    assert not result["unsupported_references"]
    assert any(edge["type"] == "uses_api_schema" for edge in builder.edges)


def test_indexes_yaml_safely_and_keeps_external_refs_unresolved():
    pytest.importorskip("yaml")
    builder = Builder()
    file_node = builder.node("File", {"path": "api.yaml"}, key=("file", "api.yaml"))
    text = """openapi: 3.0.3
components:
  schemas:
    Pet:
      type: object
paths:
  /pets:
    get:
      operationId: listPets
      responses:
        '200':
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Pet'
        '201':
          content:
            application/json:
              schema:
                $ref: https://example.invalid/schema.yaml#/Pet
"""
    result = index_openapi_document(builder, file_node, "api.yaml", text,
                                    repo="pets")
    assert result["valid"] is True
    assert result["operations"] == result["schemas"] == 1
    assert result["external_references"] == [
        "https://example.invalid/schema.yaml#/Pet"]
    operation = next(n for n in builder.nodes if n["type"] == "ApiOperation")
    assert operation["attrs"]["repo"] == "pets"
    assert operation["key"] == "api_operation:pets:api.yaml#get:/pets"
    schema = next(n for n in builder.nodes if n["type"] == "ApiSchema")
    assert schema["attrs"]["repo"] == "pets"
    assert schema["key"] == "api_schema:pets:api.yaml#Pet"


def test_rejects_unsafe_yaml_tags_and_recursive_aliases():
    pytest.importorskip("yaml")
    builder = Builder()
    unsafe = index_openapi_document(builder, 0, "x.yaml",
                                    "!!python/object/apply:os.system ['echo nope']")
    assert unsafe["valid"] is False
    assert "YAML tags" in unsafe["error"]
    recursive = index_openapi_document(builder, 0, "x.yaml", "x: &x [*x]\n")
    assert "aliases" in recursive["error"]
    amplified = index_openapi_document(
        builder, 0, "x.yaml",
        "x: &x [a, a, a]\ny: [*x, *x, *x]\n")
    assert "aliases" in amplified["error"]


def test_yaml_mixed_scalar_mapping_keys_do_not_break_sorting():
    pytest.importorskip("yaml")
    builder = Builder()
    text = """openapi: 3.0.3
components:
  schemas:
    Pet: {type: object}
    12: {type: string}
paths:
  42: {get: {}}
  /pets: {get: {operationId: listPets}}
"""
    result = index_openapi_document(builder, 0, "api.yaml", text)
    assert result["valid"] is True
    assert result["operations"] == 2
    assert result["schemas"] == 2


def test_yaml_timestamps_remain_json_serializable_strings():
    pytest.importorskip("yaml")
    builder = Builder()
    file_node = builder.node("File", {"path": "api.yaml"}, key=("file", "api.yaml"))
    text = """openapi: 3.0.3
info:
  title: Timestamp API
  description: 2026-11-16T12:34:56Z
paths:
  /events:
    get:
      operationId: events
      summary: 2026-11-16
      tags: [2026-11-16T12:34:56Z]
"""
    result = index_openapi_document(builder, file_node, "api.yaml", text)
    assert result["valid"] is True
    operation = next(node for node in builder.nodes if node["type"] == "ApiOperation")
    assert operation["attrs"]["summary"] == "2026-11-16"
    assert operation["attrs"]["tags"] == ["2026-11-16T12:34:56Z"]
    json.dumps({"nodes": builder.nodes, "edges": builder.edges})


def test_repo_namespaces_prevent_schema_node_collisions():
    builder = Builder()
    doc = json.dumps({"openapi": "3.0.3", "components": {
        "schemas": {"Shared": {"type": "object"}}}, "paths": {}})
    for repo in ("service-a", "service-b"):
        file_node = builder.node("File", {"path": "api.json"},
                                 key=("file", f"{repo}:api.json"))
        result = index_openapi_document(builder, file_node, "api.json", doc,
                                        repo=repo)
        assert result["schemas"] == 1
    schemas = [node for node in builder.nodes if node["type"] == "ApiSchema"]
    assert {node["key"] for node in schemas} == {
        "api_schema:service-a:api.json#Shared",
        "api_schema:service-b:api.json#Shared",
    }


def test_yaml_input_limit_is_reported():
    result = index_openapi_document(Builder(), 0, "x.yaml", "a" * (5 * 1024 * 1024 + 1))
    assert result["error"] == "document exceeds the 5 MiB input limit"
