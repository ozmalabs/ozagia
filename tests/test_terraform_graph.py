import json

from ozagia.core.terraform_graph import (
    discover_terraform_files,
    index_terraform_text,
    merge_terraform_graphs,
)


def test_extracts_declarations_and_explicit_reference_without_values():
    source = """variable "region" { default = "CANARY_SECRET" }
resource "aws_vpc" "main" {
  cidr_block = var.region
}
output "id" { value = aws_vpc.main.id }
"""
    graph = index_terraform_text("env/main.tf", source)
    assert {node.address for node in graph.nodes} == {"region", "aws_vpc.main", "id"}
    assert any(edge.evidence == "aws_vpc.main.id" for edge in graph.edges)
    assert any(edge.evidence == "var.region" for edge in graph.unresolved)
    serialized = repr(graph)
    assert "CANARY_SECRET" not in serialized
    assert all(node.confidence == "low" for node in graph.nodes)


def test_dialects_scopes_and_nested_module_keys_do_not_collide():
    tf = index_terraform_text(
        "main.tf", 'module "child" { source = "./child" }', module_scope="root"
    )
    tofu = index_terraform_text(
        "main.tofu", 'module "child" { source = "./child" }', module_scope="root"
    )
    nested = index_terraform_text(
        "main.tf", 'module "child" { source = "./child" }', module_scope="module.a"
    )
    keys = {tf.nodes[0].key, tofu.nodes[0].key, nested.nodes[0].key}
    assert len(keys) == 3
    assert tf.nodes[0].dialect == "terraform"
    assert tofu.nodes[0].dialect == "opentofu"
    assert tf.unresolved[0].evidence == "local module source; target requires repository discovery"


def test_heredoc_masks_body_and_keeps_surrounding_declarations():
    graph = index_terraform_text(
        "main.tf",
        '''resource "before" "safe" {}
resource "x" "y" { user_data = <<EOF
CANARY_SECRET
resource "hidden" "canary" { nested = { braces = true } }
EOF
}
resource "after" "safe" {}
''',
    )
    assert {node.address for node in graph.nodes} == {
        "before.safe",
        "x.y",
        "after.safe",
    }
    assert not graph.errors
    assert "CANARY_SECRET" not in repr(graph)
    assert "hidden" not in repr(graph)


def test_heredoc_inside_list_or_parentheses_does_not_create_declarations():
    for opener in ("values = [<<EOF", "value = (<<EOF"):
        graph = index_terraform_text(
            "main.tf",
            f'''resource "x" "y" {{ {opener}
resource "hidden" "canary" {{}}
EOF
{' ]' if '[' in opener else ' )'} }}
resource "after" "safe" {{}}
''',
        )
        assert {node.address for node in graph.nodes} == {"x.y", "after.safe"}
        assert not graph.errors
        assert "canary" not in repr(graph)


def test_unterminated_heredoc_fails_closed_with_safe_error():
    graph = index_terraform_text(
        "main.tf",
        'resource "x" "y" { user_data = <<EOF\nCANARY_SECRET\nresource "hidden" "x" {}',
    )
    assert not graph.nodes
    assert graph.errors == (("main.tf", "unterminated HCL heredoc"),)
    assert "CANARY_SECRET" not in repr(graph)


def test_quoted_heredoc_marker_does_not_start_a_heredoc():
    graph = index_terraform_text(
        "main.tf",
        '''resource "x" "quoted" {
  note = "literal <<EOF and resource \\"hidden\\" \\"x\\" {}"
}
resource "y" "safe" {}
''',
    )
    assert {node.address for node in graph.nodes} == {"x.quoted", "y.safe"}
    assert not graph.errors
    assert "hidden" not in repr(graph)


def test_indented_heredoc_masks_indented_terminator_and_body():
    graph = index_terraform_text(
        "main.tf",
        '''resource "before" "safe" {}
resource "x" "y" {
  user_data = <<-EOF
    CANARY_SECRET
    resource "hidden" "canary" { braces = { nested = true } }
    EOF
}
resource "after" "safe" {}
''',
    )
    assert {node.address for node in graph.nodes} == {
        "before.safe",
        "x.y",
        "after.safe",
    }
    assert not graph.errors
    assert "CANARY_SECRET" not in repr(graph)


def test_quoted_and_commented_resource_shaped_canaries_are_not_edges():
    source = """resource "aws_vpc" "safe" {
  description = "aws_CANARY.test"
  # aws_COMMENT.test
  cidr_block = var.region
}
"""
    graph = index_terraform_text("main.tf", source)
    assert "aws_CANARY.test" not in repr(graph)
    assert "aws_COMMENT.test" not in repr(graph)
    assert [edge.evidence for edge in graph.unresolved] == ["var.region"]


def test_provider_aliases_are_distinct_and_resource_references_resolve():
    graph = index_terraform_text(
        "main.tf",
        '''provider "aws" { region = "us-east-1" }
provider "aws" {
  alias  = "west"
  region = "us-west-2"
}
resource "aws_vpc" "west" {
  provider = aws.west
}
''',
    )
    providers = [node for node in graph.nodes if node.kind == "provider"]
    assert {node.address for node in providers} == {"aws", "aws.west"}
    edge = next(edge for edge in graph.edges if edge.evidence == "aws.west")
    assert edge.target in {node.key for node in providers}
    assert "us-east-1" not in repr(graph)
    assert "us-west-2" not in repr(graph)


def test_json_provider_alias_is_structural_only():
    graph = index_terraform_text(
        "main.tf.json",
        '{"provider":{"aws":{"alias":"west","region":"CANARY_SECRET"}},'
        '"resource":{"aws_vpc":{"main":{"provider":"aws.west"}}}}',
    )
    providers = [node for node in graph.nodes if node.kind == "provider"]
    assert [node.address for node in providers] == ["aws.west"]
    assert "CANARY_SECRET" not in repr(graph)


def test_json_sources_are_supported_and_diagnostics_are_safe():
    graph = index_terraform_text(
        "main.tf.json", '{"resource":{"aws_s3_bucket":{"b":{"bucket":"CANARY_SECRET"}}}}'
    )
    assert graph.nodes[0].address == "aws_s3_bucket.b"
    assert "CANARY_SECRET" not in repr(graph)


def test_comment_and_nested_resource_text_do_not_create_blocks():
    source = '''# resource "aws_vpc" "commented" { }
locals {
  note = """resource \"aws_vpc\" \"nested\" { }"""
  safe = "resource \\\"aws_vpc\\\" \\\"also_nested\\\" { }"
}
resource "aws_vpc" "real" { cidr_block = var.network.cidr }
'''
    graph = index_terraform_text("main.tf", source)
    assert [node.address for node in graph.nodes if node.kind == "resource"] == ["aws_vpc.real"]
    assert "commented" not in repr(graph)
    assert "nested" not in repr(graph)
    assert any(edge.evidence == "var.network.cidr" for edge in graph.unresolved)


def test_malformed_hcl_fails_closed():
    for source in ('resource "aws_vpc" "broken" {', 'resource "aws_vpc" "broken" { } }'):
        graph = index_terraform_text("main.tf", source)
        assert not graph.nodes
        assert graph.errors == (("main.tf", "malformed HCL block structure"),)


def test_json_declaration_groups_have_fixed_depth():
    source = """{"resource":{"aws_s3_bucket":{"one":{"bucket":"secret"},"nested":{"name":{"unexpected":true}}}},
      "variable":{"region":{"default":"secret"},"nested":{"more":{"unexpected":true}}}}"""
    graph = index_terraform_text("main.tf.json", source)
    assert {node.address for node in graph.nodes} == {
        "aws_s3_bucket.one",
        "aws_s3_bucket.nested",
        "region",
        "nested",
    }


def test_file_discovery_skips_hidden_and_state_files(tmp_path):
    (tmp_path / "main.tf").write_text("resource {}")
    (tmp_path / "terraform.tfstate").write_text("CANARY_SECRET")
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "cache.tf").write_text("x")
    assert discover_terraform_files(tmp_path) == ("main.tf",)


def test_locals_do_not_treat_nested_object_keys_as_declarations():
    graph = index_terraform_text(
        "main.tf",
        '''locals {
  config = {
    nested_key = "CANARY_SECRET"
    deeper = { another = var.region }
  }
  region = var.region
}
''',
    )
    assert [node.address for node in graph.nodes] == ["config", "region"]
    assert "CANARY_SECRET" not in repr(graph)


def test_meta_blocks_ephemeral_resources_and_instance_modes():
    graph = index_terraform_text(
        "main.tf",
        '''moved {
  from = aws_vpc.old
  to   = aws_vpc.new
}
import {
  to = aws_vpc.main
  id = "vpc-123"
}
removed {
  from = aws_vpc.retired
}
check "health" {
  assert {
    condition = var.healthy
  }
}
ephemeral "random_password" "db" {
  length = 16
}
output "password" {
  value = ephemeral.random_password.db.result
}
resource "aws_vpc" "main" {
  count = 2
}
resource "aws_subnet" "private" {
  for_each = var.subnets
  vpc_id   = aws_vpc.main.id
}
''',
    )
    by_kind: dict[str, set[str]] = {}
    for node in graph.nodes:
        by_kind.setdefault(node.kind, set()).add(node.address)
    assert by_kind["moved"] == {"moved@1"}
    assert by_kind["import"] == {"import@5"}
    assert by_kind["removed"] == {"removed@9"}
    assert by_kind["check"] == {"health"}
    assert by_kind["ephemeral"] == {"ephemeral.random_password.db"}
    assert any(edge.relation == "references" and edge.evidence == "aws_vpc.main"
               for edge in graph.edges)
    assert any(edge.evidence == "ephemeral.random_password.db.result"
               for edge in graph.edges)
    modes = {node.address: node.instance_mode for node in graph.nodes}
    assert modes["aws_vpc.main"] == "count"
    assert modes["aws_subnet.private"] == "for_each"
    assert modes["ephemeral.random_password.db"] is None


def test_nested_count_is_not_a_top_level_instance_mode():
    graph = index_terraform_text(
        "main.tf",
        '''resource "aws_vpc" "main" {
  tags = {
    count = 3
  }
}
''',
    )
    assert graph.nodes[0].instance_mode is None


def test_json_expression_references_ephemeral_and_meta_blocks():
    graph = index_terraform_text(
        "main.tf.json",
        json.dumps({
            "resource": {
                "aws_vpc": {"main": {"cidr_block": "10.0.0.0/16"}},
                "aws_subnet": {"private": {
                    "vpc_id": "${aws_vpc.main.id}",
                    "count": 2,
                }},
            },
            "ephemeral": {"random_password": {"db": {"length": 16}}},
            "output": {"id": {"value": "${ephemeral.random_password.db.result}"}},
            "moved": [{"from": "${aws_vpc.old.id}", "to": "${aws_vpc.main.id}"}],
            "import": [{"to": "${aws_vpc.main}", "id": "vpc-123"}],
            "removed": [{"from": "${aws_vpc.removed}"}],
            "check": {"health": {"assert": [{"condition": "${var.healthy}"}]}},
            "variable": {"healthy": {"default": "CANARY_SECRET"}},
        }),
    )
    addresses = {(node.kind, node.address) for node in graph.nodes}
    assert ("ephemeral", "ephemeral.random_password.db") in addresses
    assert ("moved", "moved@0") in addresses
    assert ("import", "import@0") in addresses
    assert ("removed", "removed@0") in addresses
    assert ("check", "health") in addresses
    assert any(edge.evidence == "aws_vpc.main.id" for edge in graph.edges)
    assert any(edge.evidence == "ephemeral.random_password.db.result"
               for edge in graph.edges)
    assert any(node.address == "aws_subnet.private" and node.instance_mode == "count"
               for node in graph.nodes)
    assert "CANARY_SECRET" not in repr(graph)


def test_module_output_reference_links_to_child_output():
    parent = index_terraform_text(
        "main.tf",
        '''module "net" { source = "./modules/net" }
output "vpc" { value = module.net.vpc_id }
''',
        module_scope=".",
    )
    child = index_terraform_text(
        "modules/net/main.tf",
        'output "vpc_id" { value = "vpc-123" }\n',
        module_scope="modules/net",
    )
    merged = merge_terraform_graphs((parent, child))
    child_output = next(node for node in child.nodes if node.kind == "output")
    edge = next(edge for edge in merged.edges if edge.relation == "module_output")
    assert edge.target == child_output.key
    assert edge.evidence == "module.net.vpc_id"
    assert edge.scope == "modules/net"
    module_node = next(node for node in parent.nodes if node.kind == "module")
    assert any(edge.relation == "references" and edge.target == module_node.key
               for edge in merged.edges)


def test_merge_resolves_provider_agnostic_cross_file_references_without_values():
    network = index_terraform_text(
        "infra/network.tf",
        'resource "customcloud_network" "primary" { token = "CANARY_SECRET" }',
    )
    subnet = index_terraform_text(
        "infra/subnet.tf",
        'resource "customcloud_subnet" "private" { network_id = customcloud_network.primary.id }',
    )
    merged = merge_terraform_graphs((network, subnet))
    edge = next(edge for edge in merged.edges if edge.evidence == "customcloud_network.primary.id")
    assert edge.target == network.nodes[0].key
    assert not any(item.evidence == edge.evidence for item in merged.unresolved)
    assert "CANARY_SECRET" not in repr(merged)


def test_merge_keeps_duplicate_addresses_unresolved():
    declaration_a = index_terraform_text(
        "a.tf", 'resource "customcloud_network" "shared" { name = "one" }'
    )
    declaration_b = index_terraform_text(
        "b.tf", 'resource "customcloud_network" "shared" { name = "two" }'
    )
    consumer = index_terraform_text(
        "use.tf",
        'resource "customcloud_subnet" "private" { network_id = customcloud_network.shared.id }',
    )
    merged = merge_terraform_graphs((declaration_a, declaration_b, consumer))
    assert not any(edge.evidence == "customcloud_network.shared.id" for edge in merged.edges)
    assert any(edge.evidence == "customcloud_network.shared.id" for edge in merged.unresolved)


def test_merge_does_not_cross_directory_module_scope_or_dialect():
    target = index_terraform_text(
        "modules/a/network.tf",
        'resource "customcloud_network" "main" {}',
        module_scope="module.a",
    )
    other_scope = index_terraform_text(
        "modules/b/network.tf",
        'resource "customcloud_network" "main" {}',
        module_scope="module.b",
    )
    reference = index_terraform_text(
        "modules/b/use.tf",
        'resource "customcloud_subnet" "main" { x = customcloud_network.main.id }',
        module_scope="module.b",
    )
    tofu = index_terraform_text(
        "modules/b/network.tofu",
        'resource "customcloud_network" "main" {}',
        module_scope="module.b",
    )
    merged = merge_terraform_graphs((target, other_scope, reference, tofu))
    edge = next(edge for edge in merged.edges if edge.evidence == "customcloud_network.main.id")
    assert edge.target == other_scope.nodes[0].key


def test_merge_resolves_local_module_source_to_same_dialect_child_declarations():
    parent = index_terraform_text(
        "main.tf",
        'module "child" { source = "./modules/child" }',
        module_scope=".",
    )
    assert parent.nodes[0].module_source == "./modules/child"
    child = index_terraform_text(
        "modules/child/main.tf",
        'resource "customcloud_network" "main" {}',
        module_scope="modules/child",
    )
    merged = merge_terraform_graphs((parent, child))
    edge = next(edge for edge in merged.edges if edge.relation == "module_source")
    assert edge.source == parent.nodes[0].key
    assert edge.target == child.nodes[0].key
    assert edge.provenance == "declared"
    assert edge.evidence == "modules/child"
    assert not any(item.relation == "module_source" for item in merged.unresolved)


def test_module_output_reference_resolves_to_module_declaration():
    graph = index_terraform_text(
        "main.tf",
        '''module "x" { source = "./child" }
output "value" { value = module.x.output }
''',
        module_scope=".",
    )
    merged = merge_terraform_graphs((graph,))
    module = next(node for node in graph.nodes if node.kind == "module")
    reference = next(edge for edge in merged.edges if edge.evidence == "module.x.output")
    assert reference.target == module.key


def test_local_module_source_escaping_repository_stays_unresolved():
    parent = index_terraform_text(
        "main.tf", 'module "outside" { source = "../../outside" }', module_scope="."
    )
    child = index_terraform_text(
        "outside/main.tf",
        'resource "customcloud_network" "outside" {}',
        module_scope="outside",
    )
    merged = merge_terraform_graphs((parent, child))
    assert not any(edge.relation == "module_source" for edge in merged.edges)
    assert any(
        edge.relation == "module_source"
        and edge.evidence == "local module source escapes repository root"
        for edge in merged.unresolved
    )


def test_remote_git_and_registry_module_sources_stay_unresolved_safely():
    source_text = '''module "git" {
  source = "git::https://example.invalid/private/repo.git?ref=secret-branch"
}
module "registry" { source = "hashicorp/consul/aws" }
'''
    graph = index_terraform_text("main.tf", source_text, module_scope=".")
    merged = merge_terraform_graphs((graph,))
    assert not any(edge.relation == "module_source" for edge in merged.edges)
    assert len([edge for edge in merged.unresolved if edge.relation == "module_source"]) == 2
    assert "remote module source" in repr(merged)
    assert "https://example.invalid" not in repr(merged)
    assert "secret-branch" not in repr(merged)


def test_json_local_module_source_resolves_to_child_declaration():
    parent = index_terraform_text(
        "main.tf.json",
        '{"module":{"child":{"source":"./child","version":"CANARY_SECRET"}}}',
        module_scope=".",
    )
    child = index_terraform_text(
        "child/main.tf.json",
        '{"resource":{"customcloud_network":{"main":{}}}}',
        module_scope="child",
    )
    merged = merge_terraform_graphs((parent, child))
    module = parent.nodes[0]
    assert module.module_source == "./child"
    assert any(
        edge.relation == "module_source"
        and edge.source == module.key
        and edge.target == child.nodes[0].key
        and edge.evidence == "child"
        for edge in merged.edges
    )
    assert "CANARY_SECRET" not in repr(merged)


def test_duplicate_module_declarations_remain_ambiguous_and_unresolved():
    first = index_terraform_text(
        "one.tf", 'module "x" { source = "./child" }', module_scope="."
    )
    second = index_terraform_text(
        "two.tf", 'module "x" { source = "./child" }', module_scope="."
    )
    consumer = index_terraform_text(
        "use.tf",
        'output "value" { value = module.x.output }',
        module_scope=".",
    )
    child = index_terraform_text(
        "child/main.tf", 'resource "customcloud_network" "main" {}', module_scope="child"
    )
    merged = merge_terraform_graphs((first, second, consumer, child))
    assert not any(edge.evidence == "module.x.output" for edge in merged.edges)
    assert any(edge.evidence == "module.x.output" for edge in merged.unresolved)
    assert not any(edge.relation == "module_source" for edge in merged.edges)
    assert sum(edge.relation == "module_source" for edge in merged.unresolved) == 2


def test_local_module_without_discovered_declarations_stays_unresolved():
    parent = index_terraform_text(
        "main.tf", 'module "missing" { source = "./missing" }', module_scope="."
    )
    merged = merge_terraform_graphs((parent,))
    assert not any(edge.relation == "module_source" for edge in merged.edges)
    assert any(
        edge.evidence == "local module source has no statically discovered declarations"
        for edge in merged.unresolved
    )


def test_local_module_source_does_not_cross_dialects():
    parent = index_terraform_text(
        "main.tf", 'module "child" { source = "./child" }', module_scope="."
    )
    child = index_terraform_text(
        "child/main.tofu",
        'resource "customcloud_network" "main" {}',
        module_scope="child",
    )
    merged = merge_terraform_graphs((parent, child))
    assert not any(edge.relation == "module_source" for edge in merged.edges)
    assert any(
        edge.evidence == "local module source has no statically discovered declarations"
        for edge in merged.unresolved
    )
