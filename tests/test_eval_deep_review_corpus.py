import copy
import json
from pathlib import Path

import pytest

from ozagia.eval.deep_review_corpus import (
    DEFAULT_MANIFEST,
    CorpusError,
    acquire_repository,
    load_manifest,
    manifest_digest,
    manifest_summary,
    tree_digest,
    validate_manifest,
    verify_checkout,
    verify_manifest_checkouts,
)

FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "deep_review_corpus"


def _raw_manifest():
    return json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))


def test_realistic_manifest_is_valid_and_candidates_are_explicitly_unacquired():
    manifest = load_manifest(DEFAULT_MANIFEST)
    assert manifest["corpus_id"] == "ozagia-deep-review-realistic-v1"
    assert manifest["evidence_class"] == "realistic_corpus"
    assert len(manifest["repositories"]) >= 10
    assert len(manifest["change_ranges"]) >= 50
    assert sum(1 for item in manifest["repositories"] if item["holdout"]) == 2
    assert all(item["acquisition_status"] == "candidate_unacquired"
               for item in manifest["repositories"])
    assert all(item["pinned_commit"] is None for item in manifest["repositories"])
    assert all(item["tree_sha256"] is None for item in manifest["repositories"])
    assert all(item["base"] is None and item["head"] is None
               for item in manifest["change_ranges"])
    assert all(item["status"] == "slot_unassigned" for item in manifest["change_ranges"])
    summary = manifest_summary(manifest)
    assert summary["repositories"] == len(manifest["repositories"])
    assert summary["change_range_slots"] == len(manifest["change_ranges"])
    assert summary["assigned_change_ranges"] == 0
    assert summary["manifest_digest"] == manifest["digest"]


def test_manifest_digest_is_stable_and_sensitive_to_content():
    manifest = load_manifest(DEFAULT_MANIFEST)
    first = manifest_digest(manifest)
    second = manifest_digest(copy.deepcopy(manifest))
    assert first == second
    changed = copy.deepcopy(manifest)
    changed["description"] += " changed"
    assert manifest_digest(changed) != first


def test_realistic_manifest_rejects_fewer_than_ten_repositories():
    raw = _raw_manifest()
    removed = [raw["repositories"].pop() for _ in range(3)]
    removed_ids = {item["id"] for item in removed}
    raw["change_ranges"] = [slot for slot in raw["change_ranges"]
                            if slot["repository_id"] not in removed_ids]
    with pytest.raises(ValueError, match="at least 10 entries"):
        validate_manifest(raw)


def test_realistic_manifest_rejects_fewer_than_fifty_slots():
    raw = _raw_manifest()
    raw["targets"]["change_ranges"] = 50
    raw["change_ranges"] = raw["change_ranges"][:49]
    with pytest.raises(ValueError, match="at least 50 slots"):
        validate_manifest(raw)


def test_manifest_rejects_duplicate_repository_and_slot_ids():
    raw = _raw_manifest()
    duplicate = copy.deepcopy(raw["repositories"][1])
    duplicate["repo"] = "example/other"
    duplicate["remote"] = "https://github.com/example/other"
    raw["repositories"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate repository id"):
        validate_manifest(raw)

    raw = _raw_manifest()
    raw["change_ranges"][1]["id"] = raw["change_ranges"][0]["id"]
    with pytest.raises(ValueError, match="duplicate change-range slot id"):
        validate_manifest(raw)


def test_unacquired_repository_cannot_record_a_commit_or_digest():
    raw = _raw_manifest()
    raw["repositories"][0]["pinned_commit"] = "a" * 40
    with pytest.raises(ValueError, match="unacquired but records"):
        validate_manifest(raw)


def test_assigned_slot_requires_two_distinct_valid_commits():
    raw = _raw_manifest()
    raw["change_ranges"][0]["status"] = "assigned"
    with pytest.raises(ValueError, match="needs base and head"):
        validate_manifest(raw)

    raw = _raw_manifest()
    raw["change_ranges"][0].update({
        "status": "assigned", "base": "a" * 40, "head": "b" * 40})
    normalized = validate_manifest(raw)
    assert normalized["change_ranges"][0]["base"] == "a" * 40

    raw["change_ranges"][0]["head"] = "not-a-commit"
    with pytest.raises(ValueError, match="40- or 64-character"):
        validate_manifest(raw)

    raw["change_ranges"][0]["head"] = raw["change_ranges"][0]["base"]
    with pytest.raises(ValueError, match="must differ"):
        validate_manifest(raw)


def test_slot_holdout_flag_must_match_repository_holdout_flag():
    raw = _raw_manifest()
    slot = next(item for item in raw["change_ranges"]
                if item["repository_id"] == "flask")
    slot["holdout"] = True
    with pytest.raises(ValueError, match="must match its repository holdout flag"):
        validate_manifest(raw)


def test_every_repository_needs_at_least_one_slot():
    raw = _raw_manifest()
    removed = [slot for slot in raw["change_ranges"]
               if slot["repository_id"] == "fd"]
    raw["change_ranges"] = [slot for slot in raw["change_ranges"]
                            if slot["repository_id"] != "fd"]
    for index, slot in enumerate(removed):
        replacement = copy.deepcopy(slot)
        replacement["id"] = f"flask-extra-{index + 1}"
        replacement["repository_id"] = "flask"
        raw["change_ranges"].append(replacement)
    with pytest.raises(ValueError, match="without a change-range slot: fd"):
        validate_manifest(raw)


def test_tree_digest_is_repeatable_and_content_sensitive(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    first = tree_digest(tmp_path)
    second = tree_digest(tmp_path)
    assert first == second
    assert first["files"] == 2
    assert first["bytes"] == 22
    (tmp_path / "sub" / "b.py").write_text("print('changed')\n", encoding="utf-8")
    assert tree_digest(tmp_path)["sha256"] != first["sha256"]


def test_verify_checkout_reports_missing_verified_and_mismatch(tmp_path):
    missing = verify_checkout(tmp_path / "absent", expected_tree_sha256="a" * 64)
    assert missing["status"] == "missing"

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "a.py").write_text("x = 1\n", encoding="utf-8")
    digest = tree_digest(checkout)["sha256"]
    verified = verify_checkout(checkout, expected_tree_sha256=digest)
    assert verified["status"] == "verified"
    assert verified["tree_status"] == "verified"
    assert verified["commit_status"] == "not_checked"

    mismatch = verify_checkout(checkout, expected_tree_sha256="b" * 64)
    assert mismatch["status"] == "mismatch"
    assert mismatch["tree_status"] == "mismatch"

    unverified = verify_checkout(checkout)
    assert unverified["status"] == "unverified"


def test_acquire_is_offline_by_default_and_refuses_unpinned_candidates(tmp_path):
    manifest = load_manifest(DEFAULT_MANIFEST)
    with pytest.raises(CorpusError, match="no pinned commit") as excinfo:
        acquire_repository(manifest, "flask", checkout_root=tmp_path)
    assert excinfo.value.reason == "unassigned_commit"

    pinned = copy.deepcopy(manifest)
    repo = next(item for item in pinned["repositories"] if item["id"] == "flask")
    repo["pinned_commit"] = "a" * 40
    repo["tree_sha256"] = "b" * 64
    with pytest.raises(CorpusError) as excinfo:
        acquire_repository(pinned, "flask", checkout_root=tmp_path)
    assert excinfo.value.reason == "network_not_allowed"
    assert excinfo.value.to_dict()["status"] == "refused"


def test_acquire_does_not_clobber_an_unverified_existing_checkout(tmp_path):
    manifest = copy.deepcopy(load_manifest(DEFAULT_MANIFEST))
    repo = next(item for item in manifest["repositories"] if item["id"] == "flask")
    repo["pinned_commit"] = "a" * 40
    repo["tree_sha256"] = "b" * 64
    existing = tmp_path / "repositories" / "flask"
    existing.mkdir(parents=True)
    (existing / "local.txt").write_text("keep me\n", encoding="utf-8")
    with pytest.raises(CorpusError) as excinfo:
        acquire_repository(manifest, "flask", checkout_root=tmp_path)
    assert excinfo.value.reason == "checkout_exists_unverified"
    assert (existing / "local.txt").is_file()


def test_verify_manifest_checkouts_is_offline_and_reports_incomplete(tmp_path):
    manifest = load_manifest(DEFAULT_MANIFEST)
    checkout = tmp_path / "flask"
    checkout.mkdir()
    (checkout / "app.py").write_text("app = 1\n", encoding="utf-8")
    result = verify_manifest_checkouts(
        manifest, overrides={"flask": checkout})
    assert result["counts"]["missing"] == len(manifest["repositories"]) - 1
    assert result["counts"]["unverified"] == 1
    assert result["status"] == "incomplete"


def test_corpus_evaluation_directory_has_templates_and_label_root():
    assert (FIXTURE / "templates" / "reviewer-a.template.json").is_file()
    assert (FIXTURE / "templates" / "reviewer-b.template.json").is_file()
    assert (FIXTURE / "templates" / "adjudicator.template.json").is_file()
    assert (FIXTURE / "labels").is_dir()


def test_validate_cli_writes_a_summary(tmp_path):
    from ozagia.eval.deep_review_corpus import main
    out = tmp_path / "summary.json"
    assert main(["validate", "--out", str(out)]) == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["status"] == "valid"
    assert summary["repositories"] >= 10
    assert summary["change_range_slots"] >= 50
    assert summary["acquisition_status"] == {"candidate_unacquired": 12}

