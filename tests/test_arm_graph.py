import json

from ozagia.core.arm_graph import index_arm_text


def test_arm_static_resource_id_dependencies_resolve_without_values():
    source = """{
      "resources": [
        {"type":"Microsoft.Storage/storageAccounts","name":"store1","apiVersion":"2024-01-01","properties":{"accessKey":"CANARY_SECRET"}},
        {"type":"Microsoft.Web/sites","name":"app1","dependsOn":["[resourceId('Microsoft.Storage/storageAccounts', 'store1')]"],"properties":{"password":"ANOTHER_CANARY"}}
      ], "parameters":{"credential":{"defaultValue":"PARAM_CANARY"}}
    }"""
    graph = index_arm_text("infra/template.json", source)
    assert [node.address for node in graph.nodes] == [
        "Microsoft.Storage/storageAccounts/store1",
        "Microsoft.Web/sites/app1",
    ]
    assert len(graph.edges) == 1
    assert graph.edges[0].target == graph.nodes[0].key
    assert graph.edges[0].provenance == "declared"
    serialized = repr(graph)
    for canary in ("CANARY_SECRET", "ANOTHER_CANARY", "PARAM_CANARY"):
        assert canary not in serialized


def test_symbolic_depends_on_and_ambiguity():
    graph = index_arm_text(
        "template.json",
        """{"resources": {
          "storage": {"type":"Contoso/storage","name":"s1"},
          "app": {"type":"Contoso/app","name":"a1","dependsOn":["storage"]},
          "other": {"type":"Contoso/storage","name":"s1"},
          "ambiguous": {"type":"Contoso/app","name":"a2","dependsOn":["[resourceId('Contoso/storage','s1')]" ]}
        }}""",
    )
    assert any(edge.target == graph.nodes[0].key for edge in graph.edges)
    ambiguous_node = next(node for node in graph.nodes if node.address == "Contoso/app/a2")
    assert any(edge.source == ambiguous_node.key for edge in graph.unresolved)


def test_dynamic_dependencies_are_unresolved_without_raw_expression():
    canary = "[resourceId(parameters('sensitive-type'), parameters('credential-canary'))]"
    graph = index_arm_text(
        "template.json",
        '{"resources":[{"type":"Contoso/app","name":"app","dependsOn":['
        + json.dumps(canary)
        + "]}]}",
    )
    assert not graph.edges
    assert graph.unresolved[0].evidence == "dynamic or unsupported dependency expression"
    assert canary not in repr(graph)


def test_bicep_is_reported_unsupported_without_execution():
    graph = index_arm_text("main.bicep", "resource app 'Contoso/app@2024-01-01' = {}")
    assert graph.nodes == ()
    assert "unsupported" in graph.errors[0][1]


def test_duplicate_keys_and_invalid_templates_fail_closed():
    duplicate = index_arm_text("template.json", '{"resources":[],"resources":[]}')
    malformed = index_arm_text("template.json", '{"resources": [')
    assert duplicate.errors[0][1] == "duplicate JSON keys are unsupported"
    assert malformed.errors[0][1] == "invalid ARM JSON template"


def test_non_structural_resource_names_and_malformed_resource_entries_are_skipped():
    graph = index_arm_text(
        "template.json",
        """{"resources":[
          {"type":"Contoso/app","name":"[parameters('name')]"},
          {"type":"not a type","name":"ordinary"},
          "not an object"
        ]}""",
    )
    assert graph.nodes == ()
    assert graph.edges == ()
