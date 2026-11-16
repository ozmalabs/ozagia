# Structural HCL and YAML edits

`ozagia.edit.dispatch.apply_edit_script` routes `.tf`, `.tofu` and `.hcl` to
the bounded HCL adapter, keeps `.json` (including `.tf.json`) on the JSON
adapter, and routes `.yaml`/`.yml` to the optional round-trip YAML adapter.
Edits are previews unless the caller explicitly requests `write=True`.
Writes use `edit/txn.py` for the base hash check, atomic replacement and backup.

## HCL

Attribute operations name a literal block kind/labels and an existing direct
attribute:

```python
{
    "file": "network/main.tf",
    "ops": [{
        "op": "set_attribute",
        "block": {"kind": "resource", "labels": ["aws_vpc", "main"]},
        "name": "cidr_block",
        "value_raw": '"10.0.0.0/16"',
    }],
}
```

`remove_attribute` and `delete_attribute` remove an existing attribute.
`blocks` may be used instead of `block` to identify nested block paths. Missing
or duplicate block/attribute matches are refused. Values must be bounded,
single-line HCL expressions; heredoc attributes, malformed delimiters,
ambiguous strings/comments and raw diff/patch keys are refused. The adapter
does not execute Terraform/OpenTofu or validate provider semantics. It preserves
the original source except for the selected value span, but is intentionally
not a complete HCL grammar implementation.

## YAML

Install the round-trip editor extra with `pip install 'ozagia[edit-yaml]'`.
Operations use mapping-key/list-index paths:

```python
{
    "file": "values.yaml",
    "ops": [{
        "op": "set",
        "path": ["service", "image", "tag"],
        "value": "v2",
    }],
}
```

Supported operations are `set`, `delete`, and `merge` (merge targets an
existing mapping path). Comment-preserving serialization is provided by
ruamel.yaml. Duplicate keys, aliases, custom tags, unsafe paths,
malformed documents, excessive depth/size and missing optional parser are
refused. There is no text-patch fallback. Round-trip serialization may adjust
formatting outside a value even though comments and standalone anchors are
retained when supported by ruamel.yaml.

Responses include hashes and operation summaries, not raw op values or source
diffs. Inspect the requested file locally when reviewing a preview.
