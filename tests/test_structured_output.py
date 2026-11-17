"""Model JSON is normalized only at an explicit structured-output boundary."""

import json

import pytest

from ozagia.harness.native import _parse_args
from ozagia.harness.paper_review import _parse_json_object as parse_paper
from ozagia.harness.review import _parse_json_object as parse_review
from ozagia.harness.structured_output import (
    loads_model_json,
    normalize_json_response,
    recover_fenced_json,
)


@pytest.mark.parametrize("text", [
    '```json\n{"answer": 1}\n```',
    ' \n```JSON\r\n{"answer": 1}\r\n```\n',
    '~~~\n{"answer": 1}\n~~~',
    '````json\n{"answer": 1}\n````',
])
def test_fenced_json_is_normalized_at_structured_boundaries(text):
    assert loads_model_json(text) == {"answer": 1}
    assert _parse_args(text) == ({"answer": 1}, None)
    assert parse_review(text) == {"answer": 1}
    assert parse_paper(text) == {"answer": 1}


@pytest.mark.parametrize("text", [
    '```python\n{"a": 1}\n```',
    '```json\n{"a":\n```',
    '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```',
    '```json\n{"a": 1}\n````',
    '```python\nprint(1)\n```\n```json\n{"a": 1}\n```',
])
def test_nonwrapper_or_invalid_content_is_unchanged(text):
    assert normalize_json_response(text) == text
    with pytest.raises(json.JSONDecodeError):
        loads_model_json(text)


def test_single_fenced_json_with_prose_is_recovered_without_losing_prose():
    text = 'Here is the answer:\n\n```json\n{"a": 1}\n```\n\nMore context.'
    result = recover_fenced_json(text)
    assert result is not None
    assert result.body == '{"a": 1}'
    assert result.prefix == 'Here is the answer:\n\n'
    assert result.suffix == '\n\nMore context.'
    assert loads_model_json(text) == {"a": 1}
    assert _parse_args(text) == ({"a": 1}, None)


def test_fences_inside_json_strings_and_nonobject_tool_args():
    text = '```json\n{"markdown": "```python\\nprint(1)\\n```"}\n```'
    assert loads_model_json(text)["markdown"] == "```python\nprint(1)\n```"
    value, error = _parse_args('```json\n[1, 2]\n```')
    assert value is None
    assert error == "tool arguments must be a JSON object"
