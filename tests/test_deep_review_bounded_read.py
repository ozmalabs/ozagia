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


def test_bounded_read_rejects_symlinked_ancestor(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret", encoding="utf-8")
    (root / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_text(root / "sub" / "secret.py", 100, root=root)


def test_bounded_read_refuses_ancestor_swapped_before_open(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    sub = root / "sub"
    sub.mkdir()
    (sub / "file.py").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("outside", encoding="utf-8")
    original_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "sub" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            sub.rename(root / "old")
            sub.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    # The capability test uses the original os.open identity.
    monkeypatch.setattr(os, "supports_dir_fd", {racing_open})
    with pytest.raises(OSError):
        read_text(sub / "file.py", 100, root=root)
    assert swapped
