"""Stable node keys round-trip."""
from __future__ import annotations

import pytest

from ozagia.core.keys import format_key, key_file, parse_key, text_key


@pytest.mark.parametrize("key", [
    ("file", "a/b.py"),
    ("symbol", "a/b.py", "C.m"),
    ("symbol", "src/x.rs", "impl fmt::Display for X"),
    ("test", "t/x.py", "TestA.test_b"),
    ("seed", "gap"),
    ("seed_item", "gap", "Rule", "r1"),
    ("repo",),
])
def test_keys_round_trip(key):
    assert parse_key(format_key(key)) == key


def test_key_file():
    assert key_file("sym:src/x.rs::impl fmt::Display for X") == "src/x.rs"
    assert key_file(text_key("d/a.yaml", 10, 42)) == "d/a.yaml"
    assert key_file("commit:abc") is None
