"""The deep-review section of the unified read-only status snapshot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ozagia.core import status as status_module


def _status(root: Path, monkeypatch) -> dict[str, Any]:
    """Compose status with every unrelated component stubbed out."""
    monkeypatch.setattr(
        status_module, "_config_status",
        lambda value: (status_module._section("defaults", {}), None))
    monkeypatch.setattr(
        status_module, "_workspace_status",
        lambda value, degraded: status_module._section("ok", {}))
    monkeypatch.setattr(
        status_module, "_resources_status",
        lambda value, **kwargs: status_module._section("ok", {}))
    monkeypatch.setattr(
        status_module, "_files_status",
        lambda value, degraded: status_module._section("ok", {}))
    monkeypatch.setattr(
        status_module, "_runtime_status",
        lambda value, project, degraded: status_module._section("ok", {}))
    monkeypatch.setattr(
        status_module, "_daemon_status",
        lambda value, **kwargs: status_module._section("absent", {}))
    monkeypatch.setattr(
        status_module, "_doctor_status",
        lambda value, degraded: status_module._section("ok", {}))
    return status_module.status(root)


def _manifest(root: Path, **overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": status_module.REVIEW_MANIFEST_SCHEMA,
        "root": str(root),
        "profile": "full",
        "status": "partial",
        "complete": False,
        "exhaustive": False,
        "truth_assessed": False,
        "findings": 2,
        "subjects": {
            "security": {"status": "complete", "findings": ["sec-1"]},
            "tests-and-coverage": {"status": "unknown", "findings": []},
        },
    }
    manifest.update(overrides)
    return manifest


def _write_manifest(review: Path, root: Path, **overrides: Any) -> Path:
    review.mkdir(parents=True)
    path = review / status_module.REVIEW_MANIFEST_NAME
    path.write_text(json.dumps(_manifest(root, **overrides)), encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root.resolve()


def test_reviews_absent_when_state_or_review_directory_is_missing(
    repo: Path, monkeypatch,
) -> None:
    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "absent"
    assert section["error"] is None
    body = section["report"]
    assert body["root"] == str(repo)
    assert body["reviews_root"] == str(repo / ".ozagia" / "reviews")
    assert body["latest"] is None
    assert body["manifest"] is None
    assert body["age_s"] is None
    assert body["status"] is None
    assert body["subjects"] == {}
    assert report["degraded"] == []
    # Reading absence must not create the state directory.
    assert not (repo / ".ozagia").exists()

    (repo / ".ozagia" / "reviews").mkdir(parents=True)
    section = _status(repo, monkeypatch)["reviews"]
    assert section["state"] == "absent"
    assert section["report"]["latest"] is None


def test_reviews_reads_the_newest_valid_manifest(
    repo: Path, monkeypatch,
) -> None:
    reviews_root = repo / ".ozagia" / "reviews"
    old = reviews_root / "deep-20260101T000000000000Z"
    _write_manifest(old, repo, status="unknown", findings=99)
    latest = reviews_root / "deep-20260202T000000000000Z"
    _write_manifest(latest, repo)

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "ok"
    assert section["error"] is None
    body = section["report"]
    assert body["root"] == str(repo)
    assert body["latest"] == str(latest)
    assert body["manifest"] == str(latest / "manifest.json")
    assert body["manifest_root"] == str(repo)
    assert body["status"] == "partial"
    assert body["complete"] is False
    assert body["exhaustive"] is False
    assert body["truth_assessed"] is False
    assert body["findings"] == 2
    assert body["subject_count"] == 2
    assert body["subjects"] == {
        "security": "complete",
        "tests-and-coverage": "unknown",
    }
    assert body["subjects_truncated"] is False
    assert body["age_s"] is not None
    assert 0 <= body["age_s"] < 60
    # Finding IDs stay in the manifest, not in the status snapshot.
    assert "sec-1" not in json.dumps(section)
    assert report["degraded"] == []
    text = status_module.format_status(report)
    assert "reviews: ok" in text
    assert "status=partial" in text


def test_reviews_bounds_the_subject_status_map(
    repo: Path, monkeypatch,
) -> None:
    subjects = {
        f"subject-{index:02d}": {"status": "complete"}
        for index in range(40)
    }
    review = (
        repo / ".ozagia" / "reviews" / "deep-20260101T000000000000Z")
    _write_manifest(review, repo, subjects=subjects)

    body = _status(repo, monkeypatch)["reviews"]["report"]

    assert body["subject_count"] == 40
    assert body["subjects_truncated"] is True
    assert list(body["subjects"]) == sorted(subjects)[
        :status_module.REVIEW_SUBJECT_LIMIT]
    assert len(body["subjects"]) == status_module.REVIEW_SUBJECT_LIMIT


def test_reviews_oversized_manifest_is_unknown(
    repo: Path, monkeypatch,
) -> None:
    review = (
        repo / ".ozagia" / "reviews" / "deep-20260101T000000000000Z")
    review.mkdir(parents=True)
    manifest = review / "manifest.json"
    manifest.write_bytes(b"{" * (status_module.REVIEW_MANIFEST_MAX_BYTES + 1))

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "unknown"
    assert section["report"]["status"] is None
    assert [item["code"] for item in report["degraded"]] == [
        "reviews.manifest"]
    assert "exceeds" in report["degraded"][0]["message"]


def test_reviews_malformed_manifest_is_unknown_and_degraded(
    repo: Path, monkeypatch,
) -> None:
    review = (
        repo / ".ozagia" / "reviews" / "deep-20260101T000000000000Z")
    review.mkdir(parents=True)
    manifest = review / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "unknown"
    assert section["error"]
    assert section["report"]["latest"] == str(review)
    assert section["report"]["status"] is None
    assert [item["code"] for item in report["degraded"]] == [
        "reviews.manifest"]
    assert report["degraded"][0]["component"] == "reviews"
    assert "not UTF-8 JSON" in report["degraded"][0]["message"]

    manifest.write_text(
        json.dumps(_manifest(repo, schema="not-a-deep-review-manifest")),
        encoding="utf-8")
    section = _status(repo, monkeypatch)["reviews"]
    assert section["state"] == "unknown"
    assert "schema" in section["error"]


def test_reviews_refuses_a_symlinked_reviews_directory(
    repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    outside = tmp_path / "outside"
    _write_manifest(outside / "deep-20260101T000000000000Z", repo)
    (repo / ".ozagia").mkdir()
    (repo / ".ozagia" / "reviews").symlink_to(
        outside, target_is_directory=True)

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "unknown"
    assert "symlink" in section["error"]
    assert section["report"]["latest"] is None
    assert [item["code"] for item in report["degraded"]] == [
        "reviews.escape"]


def test_reviews_refuses_a_symlinked_newest_directory(
    repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    name = "deep-20260101T000000000000Z"
    outside = tmp_path / "outside"
    _write_manifest(outside / name, repo)
    reviews_root = repo / ".ozagia" / "reviews"
    reviews_root.mkdir(parents=True)
    (reviews_root / name).symlink_to(
        outside / name, target_is_directory=True)

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "unknown"
    assert "symlink" in section["error"]
    assert section["report"]["latest"] == str(reviews_root / name)
    assert section["report"]["status"] is None
    assert [item["code"] for item in report["degraded"]] == [
        "reviews.escape"]


def test_reviews_refuses_a_symlinked_manifest(
    repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    review = (
        repo / ".ozagia" / "reviews" / "deep-20260101T000000000000Z")
    review.mkdir(parents=True)
    outside = tmp_path / "manifest.json"
    outside.write_text(json.dumps(_manifest(repo)), encoding="utf-8")
    (review / "manifest.json").symlink_to(outside)

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "unknown"
    assert "manifest" in section["error"]
    assert "symlink" in section["error"]
    assert section["report"]["status"] is None
    assert [item["code"] for item in report["degraded"]] == [
        "reviews.escape"]


def test_reviews_refuses_a_symlinked_state_directory(
    repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    outside = tmp_path / "outside"
    _write_manifest(outside / "reviews" / "deep-20260101T000000000000Z", repo)
    (repo / ".ozagia").symlink_to(outside, target_is_directory=True)

    report = _status(repo, monkeypatch)
    section = report["reviews"]

    assert section["state"] == "unknown"
    assert "symlink" in section["error"]
    assert section["report"]["latest"] is None
    assert [item["code"] for item in report["degraded"]] == [
        "reviews.escape"]
