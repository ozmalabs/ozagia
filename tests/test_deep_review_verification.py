from copy import deepcopy
import hashlib
import os
import socket
import subprocess
import sys

import pytest

from ozagia.core.deep_review.verification import (
    PYTHON_ASSERTION_SCHEMA, verify_model_candidates,
    verify_python_mutable_default,
)


def test_verification_corroborates_only_exact_cited_subject_and_type():
    findings = [{"id": "dr-1", "subject": "security", "type": "unsafe_path",
                 "severity": "minor", "locations": [{"path": "src/a.py"}]}]
    candidates = [{"id": "drm-1", "subject": "security", "type": "unsafe_path",
                   "severity": "note", "truth_assessed": False,
                   "evidence": [{"kind": "model", "citation_ids": ["dr-1"]}]}]

    result = verify_model_candidates(candidates, findings)

    assert result["decisions"] == [{
        "candidate_id": "drm-1", "state": "corroborated",
        "matched_finding_ids": ["dr-1"],
        "promotion": "attach_deterministic_evidence",
        "truth_assessed": False,
        "reason": "exact subject/type match to a unique cited deterministic finding",
    }]
    assert result["truth_assessed"] is False


def test_verification_rejects_uncited_wrong_subject_type_and_ambiguous_ids():
    findings = [
        {"id": "dr-1", "subject": "security", "type": "unsafe_path"},
        {"id": "dr-1", "subject": "security", "type": "unsafe_path"},
        {"id": "dr-2", "subject": "security", "type": "unsafe_path"},
    ]
    candidates = [
        {"id": "no-citation", "subject": "security", "type": "unsafe_path"},
        {"id": "wrong-type", "subject": "security", "type": "shell_injection",
         "evidence": [{"citation_ids": ["dr-2"]}]},
        {"id": "wrong-subject", "subject": "performance", "type": "unsafe_path",
         "evidence": [{"citation_ids": ["dr-2"]}]},
        {"id": "ambiguous", "subject": "security", "type": "unsafe_path",
         "evidence": [{"citation_ids": ["dr-1"]}]},
        {"id": "unknown", "subject": "security", "type": "unsafe_path",
         "evidence": [{"citation_ids": ["missing"]}]},
    ]

    result = verify_model_candidates(candidates, findings)

    assert [row["state"] for row in result["decisions"]] == [
        "unverified", "unverified", "unverified", "unverified", "unverified"]
    assert all(row["promotion"] is None for row in result["decisions"])


def test_verification_is_read_only_and_does_not_promote_severity_or_truth():
    findings = [{"id": "dr-1", "subject": "tests-and-coverage", "type": "missing_test",
                 "severity": "minor"}]
    candidates = [{"id": "drm-1", "subject": "tests-and-coverage",
                   "type": "missing_test", "candidate_severity": "blocking",
                   "truth_assessed": False,
                   "evidence": [{"citation_ids": ["dr-1"]}]}]
    original = deepcopy((findings, candidates))

    result = verify_model_candidates(candidates, findings)

    assert (findings, candidates) == original
    assert result["decisions"][0]["state"] == "corroborated"
    assert "severity" not in result["decisions"][0]
    assert result["decisions"][0]["truth_assessed"] is False


def _assertion(path, symbol, source):
    return {"schema": PYTHON_ASSERTION_SCHEMA,
            "kind": "python.mutable_default", "path": path,
            "symbol": symbol,
            "source_sha256": hashlib.sha256(source).hexdigest()}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs unavailable")
def test_python_assertion_source_rejects_fifo_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "pipe.py")
    script = ("from ozagia.core.deep_review.verification import python_source_sha256; "
              "import sys; assert python_source_sha256(sys.argv[1], 'pipe.py') is None")
    subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                   check=True, timeout=3, capture_output=True)


def test_python_mutable_default_assertion_checks_hash_and_exact_symbol(tmp_path):
    source = b"def safe(x=None):\n    return x\n\ndef suspect(x=[]):\n    return x\n"
    (tmp_path / "mod.py").write_bytes(source)

    result = verify_python_mutable_default(
        tmp_path, _assertion("mod.py", "suspect", source))

    assert result["state"] == "verified_assertion"
    assert result["verified_fact"] == "the exact function has a direct mutable default"
    assert result["truth_assessed"] is False
    assert verify_python_mutable_default(
        tmp_path, _assertion("mod.py", "safe", source))["state"] == "unverified"


def test_python_mutable_default_checker_does_not_trust_shadowable_constructor_names(
        tmp_path):
    source = (b"list = tuple\n"
              b"def f(value=list()):\n"
              b"    return value\n")
    (tmp_path / "mod.py").write_bytes(source)

    result = verify_python_mutable_default(
        tmp_path, _assertion("mod.py", "f", source))

    assert result["state"] == "unverified"


def test_python_mutable_default_rejects_ambiguous_repeated_definitions(tmp_path):
    cases = [
        (b"class C:\n    def f(self, x=[]): pass\nclass C:\n    pass\n", "C.f"),
        (b"class C:\n    def f(self, x=[]): pass\n    def f(self, x=[]): pass\n", "C.f"),
        (b"def f(x=[]): pass\ndef f(x=[]): pass\n", "f"),
        (b"class C:\n    def f(self, x=[]): pass\ndef C(): pass\n", "C.f"),
    ]
    for source, symbol in cases:
        (tmp_path / "mod.py").write_bytes(source)
        result = verify_python_mutable_default(
            tmp_path, _assertion("mod.py", symbol, source))
        assert result["state"] == "unverified"


def test_python_mutable_default_rejects_rebound_names_in_each_scope(tmp_path):
    cases = [
        (b"def f(x=[]): pass\nf = None\n", "f"),
        (b"def f(x=[]): pass\nimport os as f\n", "f"),
        (b"def f(x=[]): pass\nif True:\n    f = None\n", "f"),
        (b"def f(x=[]): pass\nfrom other import *\n", "f"),
        (b"class C:\n    def f(self, x=[]): pass\nC = None\n", "C.f"),
        (b"class C:\n    def f(self, x=[]): pass\n    f = None\n", "C.f"),
        (b"class C:\n    def f(self, x=[]): pass\n    import os as f\n", "C.f"),
    ]
    for source, symbol in cases:
        (tmp_path / "mod.py").write_bytes(source)
        assert verify_python_mutable_default(
            tmp_path, _assertion("mod.py", symbol, source))["state"] == "unverified"


def test_python_source_hash_rejects_nul_path(tmp_path):
    from ozagia.core.deep_review.verification import python_source_sha256

    assert python_source_sha256(tmp_path, "mod\0.py") is None


def test_python_mutable_default_assertion_rejects_changed_source_and_false_claim(tmp_path):
    original = b"def f(x=[]):\n    return x\n"
    changed = b"def f(x=None):\n    return x\n"
    (tmp_path / "mod.py").write_bytes(changed)

    stale = verify_python_mutable_default(
        tmp_path, _assertion("mod.py", "f", original))
    false = verify_python_mutable_default(
        tmp_path, _assertion("mod.py", "f", changed))

    assert stale["state"] == "unverified"
    assert "hash" in stale["reason"]
    assert false["state"] == "unverified"


def test_python_mutable_default_assertion_rejects_symlink(tmp_path):
    source = b"def f(x=[]):\n    return x\n"
    (tmp_path / "real.py").write_bytes(source)
    (tmp_path / "alias.py").symlink_to(tmp_path / "real.py")

    result = verify_python_mutable_default(
        tmp_path, _assertion("alias.py", "f", source))

    assert result["state"] == "unverified"


def test_python_mutable_default_assertion_uses_no_network(tmp_path, monkeypatch):
    source = b"class C:\n    def f(self, x={}):\n        return x\n"
    (tmp_path / "mod.py").write_bytes(source)

    def denied(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", denied)
    result = verify_python_mutable_default(
        tmp_path, _assertion("mod.py", "C.f", source))

    assert result["state"] == "verified_assertion"


def test_python_mutable_default_assertion_fails_closed_on_path_swap_race(
        tmp_path, monkeypatch):
    source = b"def f(x=[]):\n    return x\n"
    outside = tmp_path / "outside.py"
    outside.write_bytes(source)
    project = tmp_path / "project"
    project.mkdir()
    target = project / "mod.py"
    target.write_bytes(source)
    real_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "mod.py" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(os, "supports_dir_fd",
                        os.supports_dir_fd | {racing_open})
    result = verify_python_mutable_default(
        project, _assertion("mod.py", "f", source))

    assert swapped
    assert result["state"] == "unverified"
