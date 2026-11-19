import os

import pytest

from ozagia.core.deep_review.analyzers import data, operations, static
from ozagia.core.deep_review.analyzers._bounded_read import read_text


def test_bounded_read_rejects_oversized_files_and_symlinks(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("12345", encoding="utf-8")
    with pytest.raises(ValueError, match="read limit"):
        read_text(source, 4)
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("safe no-follow reads are unavailable")
    link = tmp_path / "link.py"
    link.symlink_to(source)
    with pytest.raises(OSError):
        read_text(link, 10)


def test_analyzers_disclose_files_over_read_cap(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("value = 123456\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM example\n", encoding="utf-8")
    monkeypatch.setattr(static, "MAX_SOURCE_BYTES", 4)
    monkeypatch.setattr(data, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(operations, "MAX_FILE_BYTES", 4)

    _, _, skipped = static.analyze(tmp_path)
    assert any(item.get("path") == "source.py" and "limit" in item["reason"]
               for item in skipped)
    inventory = data.analyze(tmp_path)
    assert any(item.get("path") == "source.py" and "limit" in item["reason"]
               for item in inventory["not_checked"])
    operation_report = operations.analyze(tmp_path)
    assert any("Dockerfile: exceeds read size limit" in item["reason"]
               for item in operation_report["not_checked"])
