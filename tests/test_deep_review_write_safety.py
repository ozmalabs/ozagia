import os

from ozagia.core.deep_review import orchestrator


def test_evidence_excerpt_refuses_symlinked_output_directory(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("line\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    packet = {
        "findings": [{"section": "architecture", "type": "finding",
                      "severity": "minor", "keys": ["file:source.py"],
                      "file": "source.py", "line": 1}],
        "sections": {}, "graph": {"freshness": "unknown"},
    }
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    monkeypatch.setattr(orchestrator, "_verified_priority_graph",
                        lambda *a, **k: (None, {"freshness": "unknown"}))
    out = tmp_path / "report"
    out.mkdir()
    (out / "evidence").symlink_to(outside, target_is_directory=True)

    result = orchestrator.build_deep_review(tmp_path, out)
    finding = result["findings"][0]
    assert "symlink" in finding["excerpt_omitted"]
    assert list(outside.iterdir()) == []


def test_persisted_evidence_excerpt_redacts_secret_values(tmp_path, monkeypatch):
    source = tmp_path / "settings.py"
    source.write_text('API_KEY = "sk_live_1234567890abcdef"\n', encoding="utf-8")
    packet = {
        "findings": [{"section": "security", "type": "secret",
                      "severity": "major", "keys": ["file:settings.py"],
                      "file": "settings.py", "line": 1}],
        "sections": {}, "graph": {"freshness": "unknown"},
    }
    monkeypatch.setattr(orchestrator.collect, "collect", lambda *a, **k: packet)
    monkeypatch.setattr(orchestrator, "_verified_priority_graph",
                        lambda *a, **k: (None, {"freshness": "unknown"}))

    result = orchestrator.build_deep_review(tmp_path, tmp_path / "report")
    finding = result["findings"][0]
    assert "secret-like content" in finding["excerpt_omitted"]
    assert not (tmp_path / "report" / "evidence").exists()


def test_excerpt_reads_pinned_file_when_leaf_is_replaced_after_open(
        tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("safe = True\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("API_KEY = 'sk_live_1234567890abcdef'\n",
                       encoding="utf-8")
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        fd = real_open(path, flags, *args, **kwargs)
        if path == "source.py" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return fd

    monkeypatch.setattr(orchestrator.os, "open", swapping_open)
    payload, omission, _meta = orchestrator._excerpt_payload(
        tmp_path, "source.py", 1)

    assert swapped
    assert omission is None
    assert payload == b"safe = True\n"
    assert b"API_KEY" not in payload


def test_excerpt_fails_closed_without_nofollow_support(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("safe = True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator.os, "O_NOFOLLOW", None, raising=False)

    payload, omission, _meta = orchestrator._excerpt_payload(
        tmp_path, "source.py", 1)

    assert payload is None
    assert "unavailable" in omission
