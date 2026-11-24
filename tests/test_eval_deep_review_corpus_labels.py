import json
import shutil
from pathlib import Path

import pytest

from ozagia.eval.deep_review_corpus import load_manifest
from ozagia.eval.deep_review_corpus_labels import (
    LabelError,
    label_file_digest,
    load_label_file,
    load_label_set,
    load_template_file,
    seal_label_file,
    validate_label_file,
    validate_labels_dir,
)

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "deep_review_corpus_dry_run"
CORPUS = FIXTURE / "corpus.json"
LABELS = FIXTURE / "labels"
TEMPLATES = Path(__file__).resolve().parents[1] / "evaluation" / "deep_review_corpus" / "templates"


@pytest.fixture()
def manifest():
    return load_manifest(CORPUS)


def test_templates_are_valid_only_as_templates(manifest):
    for path in sorted(TEMPLATES.glob("*.json")):
        loaded = load_template_file(path)
        assert loaded["corpus_id"].startswith("<")
        assert loaded["declared_scope"]["status"] == "unknown"
        with pytest.raises(LabelError, match="not in the manifest"):
            load_label_file(path, manifest=manifest)


def test_complete_dry_run_label_file_validates_with_required_fields(manifest):
    for name in ("reviewer-a.json", "adjudication.json"):
        path = LABELS / "fixture-service" / "range-complete" / name
        loaded = load_label_file(path, manifest=manifest, require_complete=True,
                                 require_sealed=True)
        assert loaded["provenance"] == "synthetic_test_fixture"
        assert loaded["declared_scope"]["status"] == "complete"
        entries = loaded.get("labels") or loaded.get("decisions")
        assert entries
        assert all(entry["source_file_digest"] for entry in entries)


def test_seal_is_deterministic_and_tampering_is_detected(manifest, tmp_path):
    path = LABELS / "fixture-service" / "range-partial" / "reviewer-a.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    first = seal_label_file(envelope)
    second = seal_label_file(envelope)
    assert first["label_file_sha256"] == second["label_file_sha256"]
    assert first["label_file_sha256"] == label_file_digest(envelope)

    tampered = dict(envelope)
    tampered["declared_scope"] = dict(envelope["declared_scope"], status="unknown")
    with pytest.raises(LabelError, match="does not match"):
        validate_label_file(tampered, manifest=manifest, require_sealed=True)


def test_duplicate_label_ids_are_rejected(manifest):
    path = LABELS / "fixture-service" / "range-complete" / "reviewer-a.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope.pop("label_file_sha256")
    envelope["labels"].append(json.loads(json.dumps(envelope["labels"][0])))
    with pytest.raises(LabelError, match="duplicate labels id"):
        validate_label_file(envelope, manifest=manifest)


def test_require_human_rejects_synthetic_fixture_labels(manifest):
    path = LABELS / "fixture-service" / "range-complete" / "reviewer-a.json"
    with pytest.raises(LabelError, match="requires human provenance"):
        load_label_file(path, manifest=manifest, require_human=True)


def test_labels_directory_summary_keeps_partial_scope_separate(manifest):
    summary = validate_labels_dir(LABELS, manifest)
    assert summary["status"] == "incomplete"
    assert summary["counts"]["subjects"] == 2
    assert summary["counts"]["complete_subjects"] == 1
    assert summary["counts"]["partial_or_unknown_subjects"] == 1
    assert any("scope is partial" in warning for warning in summary["warnings"])
    assert summary["digest"]

    strict = validate_labels_dir(LABELS, manifest, require_complete=True)
    assert strict["status"] == "invalid"
    assert any("not complete" in error for error in strict["errors"])


def test_load_label_set_returns_reviewers_and_adjudication(manifest):
    loaded = load_label_set(LABELS, manifest)
    assert set(loaded["subjects"]) == {
        "fixture-service/range-complete", "fixture-service/range-partial"}
    complete = loaded["subjects"]["fixture-service/range-complete"]
    assert set(complete["reviewers"]) == {"reviewer-a", "reviewer-b"}
    assert complete["adjudication"] is not None
    assert complete["summary"]["complete"] is True


def test_adjudication_referencing_unknown_reviewer_label_is_invalid(manifest, tmp_path):
    copied = tmp_path / "labels"
    shutil.copytree(LABELS, copied)
    path = copied / "fixture-service" / "range-complete" / "adjudication.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["decisions"][0]["reviewer_label_ids"]["reviewer-a"] = ["MISSING"]
    path.write_text(json.dumps(seal_label_file(envelope), indent=2) + "\n", encoding="utf-8")
    summary = validate_labels_dir(copied, manifest)
    assert summary["status"] == "invalid"
    assert any("unknown labels" in error for error in summary["errors"])


def test_missing_reviewer_file_is_a_warning_until_completion_is_required(manifest, tmp_path):
    copied = tmp_path / "labels"
    shutil.copytree(LABELS, copied)
    (copied / "fixture-service" / "range-partial" / "reviewer-b.json").unlink()
    summary = validate_labels_dir(copied, manifest)
    assert summary["status"] == "invalid"
    assert any("unknown reviewer reviewer-b" in error for error in summary["errors"])
    strict = validate_labels_dir(copied, manifest, require_complete=True)
    assert strict["status"] == "invalid"
    assert any("expected 2 reviewer files" in error for error in strict["errors"])


def test_validate_cli_writes_the_label_summary(tmp_path):
    from ozagia.eval.deep_review_corpus_labels import main
    out = tmp_path / "labels.json"
    assert main(["--manifest", str(CORPUS), "validate", "--labels", str(LABELS),
                 "--out", str(out)]) == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["status"] == "incomplete"
    assert summary["counts"]["subjects"] == 2
    assert summary["counts"]["complete_subjects"] == 1
