"""
test_edit_protocol.py — Tests for position-addressed edit protocol.
"""

from ozmalib.compress.edit_protocol import (
    parse_edits,
    apply_edits,
    Edit,
    EditSet,
    ApplyResult,
    render_edit_protocol,
)
from ozmalib.compress.code_outline import code_to_outline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SOURCE = '''\
class Config:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        with open(self.path) as f:
            return json.load(f)

    def save(self, data: dict) -> None:
        with open(self.path, "w") as f:
            json.dump(data, f)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    return parser.parse_args()
'''


def _make_index(source=SOURCE):
    _, index = code_to_outline(source, language="python")
    return index


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseEdits:
    def test_single_edit(self):
        text = 'edit [3]\n```\ndef load(self) -> dict:\n    return {}\n```'
        result = parse_edits(text)
        assert len(result.edits) == 1
        e = result.edits[0]
        assert e.operation == "edit"
        assert e.position == 3
        assert "return {}" in e.content

    def test_delete(self):
        text = "delete [5]"
        result = parse_edits(text)
        assert len(result.edits) == 1
        assert result.edits[0].operation == "delete"
        assert result.edits[0].position == 5

    def test_add_after(self):
        text = 'add [3]:after\n```\ndef new_method(self):\n    pass\n```'
        result = parse_edits(text)
        assert len(result.edits) == 1
        e = result.edits[0]
        assert e.operation == "add"
        assert e.placement == "after"
        assert e.position == 3

    def test_add_before(self):
        text = 'add [3]:before\n```\ndef new_method(self):\n    pass\n```'
        result = parse_edits(text)
        assert result.edits[0].placement == "before"

    def test_multi_file_edit(self):
        text = 'edit config.py:[3]\n```\ndef load(self):\n    pass\n```'
        result = parse_edits(text)
        assert len(result.edits) == 1
        assert result.edits[0].file == "config.py"
        assert result.edits[0].position == 3

    def test_multiple_edits(self):
        text = (
            'edit [3]\n```\ndef load(self):\n    pass\n```\n\n'
            'edit [4]\n```\ndef save(self, data):\n    pass\n```'
        )
        result = parse_edits(text)
        assert len(result.edits) == 2
        assert result.edits[0].position == 3
        assert result.edits[1].position == 4

    def test_preamble_preserved(self):
        text = 'Here is my fix:\n\nedit [3]\n```\ncode\n```'
        result = parse_edits(text)
        assert "Here is my fix" in result.preamble

    def test_postamble_preserved(self):
        text = 'edit [3]\n```\ncode\n```\n\nThis should fix the bug.'
        result = parse_edits(text)
        assert "fix the bug" in result.postamble

    def test_empty_input(self):
        result = parse_edits("")
        assert len(result.edits) == 0

    def test_no_edits_just_text(self):
        result = parse_edits("I think the bug is in the load method.")
        assert len(result.edits) == 0

    def test_code_block_with_language(self):
        text = 'edit [3]\n```python\ndef load(self):\n    pass\n```'
        result = parse_edits(text)
        assert len(result.edits) == 1
        assert "def load" in result.edits[0].content

    def test_add_file_no_position(self):
        text = 'add utils.py\n```\ndef helper():\n    pass\n```'
        result = parse_edits(text)
        assert len(result.edits) == 1
        e = result.edits[0]
        assert e.operation == "add"
        assert e.file == "utils.py"
        assert e.position == 0
        assert e.placement == "end"


# ---------------------------------------------------------------------------
# Applying edits
# ---------------------------------------------------------------------------

class TestApplyEdits:
    def test_edit_replaces_body(self):
        index = _make_index()
        edit = Edit(
            operation="edit",
            position=3,  # load method
            content="    def load(self) -> dict:\n        return {}",
        )
        result = apply_edits(SOURCE, index, [edit])
        assert result.success
        assert "return {}" in result.patched_source
        assert "json.load" not in result.patched_source

    def test_delete_removes_node(self):
        index = _make_index()
        edit = Edit(operation="delete", position=3)
        result = apply_edits(SOURCE, index, [edit])
        assert result.success
        # load method should be gone
        assert "def load" not in result.patched_source
        # save should still be there
        assert "def save" in result.patched_source

    def test_add_after(self):
        index = _make_index()
        edit = Edit(
            operation="add",
            position=3,
            placement="after",
            content="    def reload(self):\n        return self.load()",
        )
        result = apply_edits(SOURCE, index, [edit])
        assert result.success
        assert "def reload" in result.patched_source

    def test_add_before(self):
        index = _make_index()
        edit = Edit(
            operation="add",
            position=3,
            placement="before",
            content="    def validate(self):\n        pass",
        )
        result = apply_edits(SOURCE, index, [edit])
        assert result.success
        # validate should appear before load
        src = result.patched_source
        assert src.index("validate") < src.index("def load")

    def test_invalid_position(self):
        index = _make_index()
        edit = Edit(operation="edit", position=99, content="code")
        result = apply_edits(SOURCE, index, [edit])
        assert not result.success
        assert any("99" in e for e in result.errors)

    def test_empty_edit_content(self):
        index = _make_index()
        edit = Edit(operation="edit", position=3, content="")
        result = apply_edits(SOURCE, index, [edit])
        assert not result.success
        assert any("empty" in e for e in result.errors)

    def test_edit_set_input(self):
        index = _make_index()
        edit_set = EditSet(edits=[
            Edit(operation="edit", position=3, content="    def load(self):\n        return {}"),
        ])
        result = apply_edits(SOURCE, index, edit_set)
        assert result.success

    def test_applied_list_populated(self):
        index = _make_index()
        edit = Edit(operation="edit", position=3, content="    def load(self):\n        pass")
        result = apply_edits(SOURCE, index, [edit])
        assert len(result.applied) == 1
        assert result.applied[0].position == 3

    def test_position_zero_appends(self):
        index = _make_index()
        edit = Edit(
            operation="add", position=0,
            placement="end",
            content="def new_func():\n    pass",
        )
        result = apply_edits(SOURCE, index, [edit])
        assert result.success
        assert result.patched_source.rstrip().endswith("pass")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestRenderEdit:
    def test_render_edit(self):
        e = Edit(operation="edit", position=3, content="def load(self):\n    pass")
        rendered = e.render()
        assert "edit [3]" in rendered
        assert "```" in rendered

    def test_render_delete(self):
        e = Edit(operation="delete", position=5)
        assert e.render() == "delete [5]"

    def test_render_add_after(self):
        e = Edit(operation="add", position=3, placement="after", content="code")
        assert "add [3]:after" in e.render()

    def test_render_multi_file(self):
        e = Edit(operation="edit", position=3, file="config.py", content="code")
        assert "config.py:[3]" in e.render()

    def test_render_edit_set(self):
        es = EditSet(edits=[
            Edit(operation="edit", position=3, content="code1"),
            Edit(operation="delete", position=5),
        ])
        rendered = render_edit_protocol(es)
        assert "edit [3]" in rendered
        assert "delete [5]" in rendered

    def test_render_with_preamble(self):
        es = EditSet(
            edits=[Edit(operation="edit", position=3, content="code")],
            preamble="Here is the fix:",
        )
        rendered = render_edit_protocol(es)
        assert rendered.startswith("Here is the fix:")


# ---------------------------------------------------------------------------
# Round-trip: parse → render → parse
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_edit_round_trip(self):
        original = 'edit [3]\n```\ndef load(self):\n    return {}\n```'
        parsed = parse_edits(original)
        rendered = render_edit_protocol(parsed)
        reparsed = parse_edits(rendered)
        assert len(reparsed.edits) == len(parsed.edits)
        assert reparsed.edits[0].position == parsed.edits[0].position
        assert reparsed.edits[0].content == parsed.edits[0].content

    def test_multi_edit_round_trip(self):
        original = (
            'edit [3]\n```\ncode_a\n```\n\n'
            'delete [5]\n\n'
            'add [7]:after\n```\ncode_b\n```'
        )
        parsed = parse_edits(original)
        assert len(parsed.edits) == 3
        rendered = render_edit_protocol(parsed)
        reparsed = parse_edits(rendered)
        assert len(reparsed.edits) == 3
