"""Deterministic adversarial cases for explicit model JSON boundaries."""

import json

import pytest

from ozagia.harness.structured_output import (
    loads_model_json,
    normalize_json_response,
    recover_fenced_json,
)
from ozagia.harness.native import _parse_args
from ozagia.harness.paper_review import (
    PaperReviewDriverError,
    _parse_json_object as parse_paper_review,
)
from ozagia.harness.review import (
    ReviewDriverError,
    _parse_json_object as parse_review,
)


@pytest.mark.parametrize("marker", ["`", "~"])
@pytest.mark.parametrize("width", range(3, 9))
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("label", ["", "json", "JSON"])
def test_fence_variants_retain_outer_prose_and_exact_json(marker, width,
                                                          newline, label):
    fence = marker * width
    header = fence + (label if label else "")
    body = '{"text":"escaped \\\\n and ```python\\\\nprint(1)\\\\n```"}'
    text = f"before{newline}{header}{newline}{body}{newline}{fence}{newline}after"

    recovered = recover_fenced_json(text)
    assert recovered is not None
    assert recovered.body == body
    assert recovered.prefix == f"before{newline}"
    assert recovered.suffix == f"{newline}after"
    assert loads_model_json(text)["text"] == "escaped \\n and ```python\\nprint(1)\\n```"


@pytest.mark.parametrize("text", [
    '```json\n{"x":NaN}\n```',
    '```json\n{"x":Infinity}\n```',
    '```json\n{"x":-Infinity}\n```',
    '```json\n{"x":1e9999}\n```',
    '```json\n{"x":1,"x":2}\n```',
    '{"outer":{"x":1,"x":2}}',
])
def test_nonstandard_numbers_and_duplicate_keys_are_rejected(text):
    assert recover_fenced_json(text) is None if text.startswith("```") else True
    with pytest.raises(ValueError):
        loads_model_json(text)


def test_excessive_json_nesting_is_reported_as_value_error():
    text = "[" * 10_000 + "0" + "]" * 10_000
    with pytest.raises(ValueError, match="nesting"):
        loads_model_json(text)


@pytest.mark.parametrize("text", [
    '```json\n{"x":1}\n```\n```json\n{"x":2}\n```',
    '```python\nprint(1)\n```\n```json\n{"x":1}\n```',
    'unclosed ```python\n```json\n{"x":1}\n```',
    '```json\n{"x":1}\n````',
    '```json extra\n{"x":1}\n```',
    'prefix ``` marker\n```json\n{"x":1}\n```',
])
def test_ambiguous_prose_and_multiple_fences_are_left_untouched(text):
    assert recover_fenced_json(text) is None
    assert normalize_json_response(text) == text
    with pytest.raises(json.JSONDecodeError):
        loads_model_json(text)


def test_multiple_json_objects_in_prose_are_not_recovered():
    text = 'first {"x":1} and second {"x":2}'
    assert recover_fenced_json(text) is None
    assert normalize_json_response(text) == text
    with pytest.raises(json.JSONDecodeError):
        loads_model_json(text)


@pytest.mark.parametrize("parser, error", [
    (parse_paper_review, PaperReviewDriverError),
    (parse_review, ReviewDriverError),
])
@pytest.mark.parametrize("text", [
    '```json\n{"value":NaN}\n```',
    '```json\n{"role":"user","role":"admin"}\n```',
])
def test_structured_consumers_reject_ambiguous_json(parser, error, text):
    with pytest.raises(error):
        parser(text)


@pytest.mark.parametrize("parser", [parse_paper_review, parse_review])
@pytest.mark.parametrize("text", [
    'Context: the answer is below.\n{"answer":1}\nEnd of response.',
    'Context:\n```json\n{"answer":1}\n```\nEnd of response.',
])
def test_json_object_consumers_preserve_supported_prose_recovery(parser, text):
    assert parser(text) == {"answer": 1}


def test_native_tool_arguments_use_same_strict_fenced_decoder():
    args, error = _parse_args('```json\n{"query":"ok"}\n```')
    assert error is None and args == {"query": "ok"}
    args, error = _parse_args('```json\n{"query":"x","query":"y"}\n```')
    assert args is None and "not valid JSON" in error
    args, error = _parse_args('```json\n{"query":NaN}\n```')
    assert args is None and "not valid JSON" in error
