import hashlib

import pytest

from ozagia.edit.code_edit import CodeEditError
from ozagia.edit.dispatch import adapter_for, apply_edit_script
from ozagia.edit.hcl import apply_hcl_edit_script


def _write(root, source, name="main.tf"):
    path = root / name
    path.write_text(source, encoding="utf-8", newline="")
    return path


def test_set_attribute_preserves_comments_and_returns_no_values(tmp_path):
    source = (
        '# header comment\n'
        'resource "aws_vpc" "main" {\n'
        '  # keep this comment\n'
        '  cidr_block = "10.0.0.0/16" # inline comment\n'
        '}\n'
    )
    path = _write(tmp_path, source)
    result = apply_edit_script(tmp_path, {
        "file": "main.tf",
        "ops": [{
            "op": "set_attribute",
            "block": {"kind": "resource", "labels": ["aws_vpc", "main"]},
            "name": "cidr_block",
            "value_raw": '"CANARY_VALUE"',
        }],
    })
    assert not result["applied"]
    assert "CANARY_VALUE" not in repr(result)
    assert path.read_text() == source
    apply_hcl_edit_script(tmp_path, {
        "file": "main.tf",
        "ops": [{
            "op": "set_attribute",
            "block": {"kind": "resource", "labels": ["aws_vpc", "main"]},
            "name": "cidr_block",
            "value_raw": '"10.2.0.0/16"',
        }],
    }, write=True)
    edited = path.read_text()
    assert '  # keep this comment\n' in edited
    assert 'cidr_block = "10.2.0.0/16" # inline comment' in edited
    assert edited.startswith("# header comment\n")


def test_delete_attribute_uses_semantic_path(tmp_path):
    path = _write(tmp_path, 'variable "region" {\n  default = "west"\n}\n')
    result = apply_hcl_edit_script(tmp_path, {
        "file": "main.tf",
        "ops": [{
            "op": "delete_attribute",
            "block": {"kind": "variable", "labels": ["region"]},
            "name": "default",
        }],
    }, write=True)
    assert result["applied"]
    assert path.read_text() == 'variable "region" {\n}\n'


def test_set_replaces_a_balanced_multiline_expression_span(tmp_path):
    path = _write(tmp_path, '''locals {
  config = {
    nested = {
      value = "old"
    }
  }
  keep = true
}
''')
    apply_hcl_edit_script(tmp_path, {
        "file": "main.tf",
        "ops": [{
            "op": "set_attribute",
            "block": {"kind": "locals", "labels": []},
            "name": "config",
            "value_raw": '{ value = "new" }',
        }],
    }, write=True)
    edited = path.read_text()
    assert 'config = { value = "new" }' in edited
    assert 'value = "old"' not in edited
    assert "keep = true" in edited


def test_duplicate_block_is_ambiguous_and_missing_attribute_refused(tmp_path):
    _write(tmp_path, (
        'resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }\n'
        'resource "aws_vpc" "main" { cidr_block = "10.1.0.0/16" }\n'
    ))
    script = {
        "file": "main.tf",
        "ops": [{
            "op": "set_attribute",
            "block": {"kind": "resource", "labels": ["aws_vpc", "main"]},
            "name": "cidr_block",
            "value_raw": '"10.2.0.0/16"',
        }],
    }
    with pytest.raises(CodeEditError, match="ambiguous"):
        apply_hcl_edit_script(tmp_path, script)
    script["ops"][0]["block"]["labels"] = ["aws_vpc", "missing"]
    with pytest.raises(CodeEditError, match="does not exist"):
        apply_hcl_edit_script(tmp_path, script)


def test_missing_attribute_and_heredoc_are_refused(tmp_path):
    _write(tmp_path, 'resource "x" "y" { name = "ok" }\n')
    with pytest.raises(CodeEditError, match="does not exist"):
        apply_hcl_edit_script(tmp_path, {
            "file": "main.tf",
            "ops": [{
                "op": "set_attribute",
                "block": {"kind": "resource", "labels": ["x", "y"]},
                "name": "missing",
                "value_raw": '"new"',
            }],
        })
    _write(tmp_path, 'resource "x" "y" {\n  name = <<EOF\nsecret\nEOF\n}\n')
    with pytest.raises(CodeEditError, match="heredoc"):
        apply_hcl_edit_script(tmp_path, {
            "file": "main.tf",
            "ops": [{
                "op": "set_attribute",
                "block": {"kind": "resource", "labels": ["x", "y"]},
                "name": "name",
                "value_raw": '"new"',
            }],
        })


def test_base_hash_conflict_and_malformed_hcl(tmp_path):
    path = _write(tmp_path, 'resource "x" "y" { name = "old" }\n')
    script = {
        "file": "main.tf",
        "base_sha256": "0" * 64,
        "ops": [{
            "op": "set_attribute",
            "block": {"kind": "resource", "labels": ["x", "y"]},
            "name": "name",
            "value_raw": '"new"',
        }],
    }
    with pytest.raises(CodeEditError, match="stale base"):
        apply_hcl_edit_script(tmp_path, script)
    path.write_text('resource "x" "y" { name = "old"\n', encoding="utf-8")
    with pytest.raises(CodeEditError, match="malformed HCL"):
        apply_hcl_edit_script(tmp_path, {**script, "base_sha256": None})


def test_adapter_dispatch_keeps_json_and_routes_hcl(tmp_path):
    assert adapter_for("main.tf") == "hcl"
    assert adapter_for("main.tofu") == "hcl"
    assert adapter_for("main.pkr.hcl") == "hcl"
    assert adapter_for("main.tf.json") == "json"
    assert adapter_for("ordinary.json") == "json"
    _write(tmp_path, 'resource "x" "y" { name = "old" }\n', "main.tofu")
    result = apply_edit_script(tmp_path, {
        "file": "main.tofu",
        "ops": [{
            "op": "set_attribute",
            "block": {"kind": "resource", "labels": ["x", "y"]},
            "name": "name",
            "value_raw": '"new"',
        }],
    })
    assert result["format"] == "hcl"
    assert hashlib.sha256((tmp_path / "main.tofu").read_bytes()).hexdigest() == result["base_sha256"]
