"""Versioned realistic deep-review corpus manifest and offline acquisition.

The manifest is data: a pinned, documented set of public repositories and
review subjects.  This module validates it, computes a deterministic tree
digest, verifies local checkouts, and downloads a pinned GitHub archive only
when the caller passes an explicit network flag.  It never imports or executes
target code, runs target tests, or invokes a model.

Nothing here scores a review.  A candidate repository is allowed to have no
pinned commit: a human acquires it, records the commit and digests, and then
marks it verified.  Refusing to invent those pins is the point.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ozagia.config import repo_root, state_path
from ozagia.eval._safe_git import command as safe_git_command
from ozagia.eval.hf_benchmarks import (
    GITHUB_HOSTS,
    MAX_ARCHIVE_BYTES,
    MAX_EXTRACTED_BYTES,
    _atomic_write_json,
    _download,
    extract_archive,
)

CORPUS_SCHEMA_VERSION = 1
EVALUATION_DIR = Path(__file__).resolve().parents[3] / "evaluation" / "deep_review_corpus"
DEFAULT_MANIFEST = EVALUATION_DIR / "corpus.json"
DEFAULT_LABELS_DIR = EVALUATION_DIR / "labels"
TREE_DIGEST_ALGORITHM = "ozagia-tree-v1"
TREE_DIGEST_EXCLUDES = (".git", ".ozagia")
MARKER_NAME = ".ozagia-deep-review-corpus.json"
CHECKOUT_SUBDIR = "repositories"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TREE_BYTES = 4 * 1024 * 1024 * 1024
MAX_TREE_FILES = 500_000
MIN_REALISTIC_REPOSITORIES = 10
MIN_REALISTIC_RANGE_SLOTS = 50

EVIDENCE_CLASSES = frozenset({"realistic_corpus", "synthetic_regression"})
ACQUISITION_STATUSES = frozenset({"candidate_unacquired", "acquired", "verified"})
RANGE_STATUSES = frozenset({"slot_unassigned", "assigned", "labeled", "excluded"})
REVIEW_THEMES = frozenset({
    "security", "correctness", "resilience", "performance",
    "architecture", "tests", "dependencies", "operations", "docs",
})
_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CorpusError(RuntimeError):
    """A corpus operation failed safely, with a stable reason code."""

    def __init__(self, reason: str, message: str, *,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "status": ("refused" if self.reason == "network_not_allowed" else "error"),
            "reason": self.reason,
            "error": str(self),
            "details": dict(self.details),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    return _text(value, field, required=False)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _string_list(value: Any, field: str, *, required: bool = True) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]")
        assert text is not None
        if text not in result:
            result.append(text)
    return result


def _relative_path(value: Any, field: str) -> str:
    text = _text(value, field)
    assert text is not None
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or text in {".", ""}:
        raise ValueError(f"{field} must be a relative path inside the checkout root: {text}")
    return path.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: str | Path, *, exclude_top: Iterable[str] = TREE_DIGEST_EXCLUDES,
                max_bytes: int = MAX_TREE_BYTES,
                max_files: int = MAX_TREE_FILES) -> dict[str, Any]:
    """Return a deterministic digest of regular files and symlinks under ``root``.

    Paths are relative POSIX strings.  The digest covers ``ozagia-tree-v1``, the
    entry kind, the relative path, and either the file SHA-256 or the symlink
    target.  ``.git`` and ``.ozagia`` are excluded by default so checkout
    metadata and review state do not change the tree identity.  Symlinks are
    never followed.
    """
    base = Path(root).resolve()
    if not base.is_dir():
        raise CorpusError("checkout_missing", f"not a directory: {base}",
                          details={"path": str(base)})
    excluded = {str(item) for item in exclude_top}
    records: list[str] = []
    files = 0
    total_bytes = 0
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if relative.parts and relative.parts[0] in excluded:
            continue
        if path.is_symlink():
            records.append(f"l\0{relative.as_posix()}\0{os.readlink(path)}")
            files += 1
            if files > max_files:
                raise CorpusError("too_large", f"tree exceeds {max_files} entries")
            continue
        if not path.is_file():
            continue
        files += 1
        if files > max_files:
            raise CorpusError("too_large", f"tree exceeds {max_files} entries")
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > max_bytes:
            raise CorpusError("too_large", f"tree exceeds {max_bytes} bytes")
        records.append(f"f\0{relative.as_posix()}\0{_sha256_file(path)}")
    payload = TREE_DIGEST_ALGORITHM + "\n" + "\n".join(records)
    return {
        "algorithm": TREE_DIGEST_ALGORITHM,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "files": files,
        "bytes": total_bytes,
        "excluded_top_level": sorted(excluded),
    }


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items()
            if key not in {"digest", "manifest_path"}}
    return canonical_sha256(body)


def _commit_or_none(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    if not _COMMIT.fullmatch(text):
        raise ValueError(f"{field} must be a 40- or 64-character lowercase Git commit")
    return text


def _sha_or_none(value: Any, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _repositories(raw: Any, evidence_class: str, targets: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("manifest.repositories must be a list")
    minimum = MIN_REALISTIC_REPOSITORIES if evidence_class == "realistic_corpus" else 1
    if len(raw) < minimum:
        raise ValueError(
            f"manifest.repositories needs at least {minimum} entries for {evidence_class}")
    repositories: list[dict[str, Any]] = []
    ids: set[str] = set()
    slugs: set[str] = set()
    for index, item in enumerate(raw):
        field = f"manifest.repositories[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must be an object")
        repo_id = _text(item.get("id"), f"{field}.id")
        assert repo_id is not None
        if repo_id in ids:
            raise ValueError(f"duplicate repository id: {repo_id}")
        ids.add(repo_id)
        slug = _text(item.get("repo"), f"{field}.repo")
        assert slug is not None
        if not _REPO_SLUG.fullmatch(slug):
            raise ValueError(f"{field}.repo must be an owner/name GitHub slug")
        if slug in slugs:
            raise ValueError(f"duplicate repository slug: {slug}")
        slugs.add(slug)
        remote = _text(item.get("remote"), f"{field}.remote")
        expected_remote = f"https://github.com/{slug}"
        if remote.rstrip("/") != expected_remote:
            raise ValueError(f"{field}.remote must be {expected_remote}")
        license_name = _text(item.get("license"), f"{field}.license")
        license_status = _text(item.get("license_status"), f"{field}.license_status")
        status = _text(item.get("acquisition_status"), f"{field}.acquisition_status")
        assert status is not None
        if status not in ACQUISITION_STATUSES:
            raise ValueError(f"{field}.acquisition_status must be one of "
                             f"{', '.join(sorted(ACQUISITION_STATUSES))}")
        commit = _commit_or_none(item.get("pinned_commit"), f"{field}.pinned_commit")
        tree = _sha_or_none(item.get("tree_sha256"), f"{field}.tree_sha256")
        archive = _sha_or_none(item.get("archive_sha256"), f"{field}.archive_sha256")
        verified_at = _optional_text(item.get("verified_at"), f"{field}.verified_at")
        if status == "candidate_unacquired":
            if any(value is not None for value in (commit, tree, archive, verified_at)):
                raise ValueError(f"{field} is unacquired but records a commit, digest or "
                                 "verification time")
        else:
            if commit is None:
                raise ValueError(f"{field}.pinned_commit is required once acquired")
            if tree is None:
                raise ValueError(f"{field}.tree_sha256 is required once acquired")
            if status == "verified" and verified_at is None:
                raise ValueError(f"{field}.verified_at is required for a verified repository")
        languages = _string_list(item.get("languages"), f"{field}.languages")
        if not languages:
            raise ValueError(f"{field}.languages must not be empty")
        size_class = _text(item.get("size_class"), f"{field}.size_class")
        assert size_class is not None
        if size_class not in {"small", "medium", "large", "very-large", "unknown"}:
            raise ValueError(f"{field}.size_class is not a known class")
        size_status = _text(item.get("size_status"), f"{field}.size_status")
        language_status = _text(item.get("language_mix_status"),
                                f"{field}.language_mix_status")
        reason = _text(item.get("reason"), f"{field}.reason")
        local_checkout = _relative_path(item.get("local_checkout"), f"{field}.local_checkout")
        holdout = _bool(item.get("holdout", False), f"{field}.holdout")
        repositories.append({
            "id": repo_id,
            "repo": slug,
            "remote": expected_remote,
            "license": license_name,
            "license_status": license_status,
            "acquisition_status": status,
            "pinned_commit": commit,
            "tree_sha256": tree,
            "archive_sha256": archive,
            "verified_at": verified_at,
            "languages": languages,
            "language_mix_status": language_status,
            "size_class": size_class,
            "size_status": size_status,
            "reason": reason,
            "local_checkout": local_checkout,
            "holdout": holdout,
        })
    holdout_count = sum(1 for item in repositories if item["holdout"])
    wanted = targets.get("holdout_repositories")
    if isinstance(wanted, int) and not isinstance(wanted, bool) and holdout_count < wanted:
        raise ValueError(f"manifest needs at least {wanted} holdout repositories; "
                         f"found {holdout_count}")
    return repositories


def _change_ranges(raw: Any, repositories: list[dict[str, Any]],
                   evidence_class: str, targets: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("manifest.change_ranges must be a list")
    target = targets.get("change_ranges")
    minimum = MIN_REALISTIC_RANGE_SLOTS if evidence_class == "realistic_corpus" else 1
    if isinstance(target, int) and not isinstance(target, bool):
        minimum = max(minimum, target)
    if len(raw) < minimum:
        raise ValueError(f"manifest.change_ranges needs at least {minimum} slots; "
                         f"found {len(raw)}")
    repo_by_id = {item["id"]: item for item in repositories}
    slots: list[dict[str, Any]] = []
    ids: set[str] = set()
    per_repo: dict[str, int] = {}
    for index, item in enumerate(raw):
        field = f"manifest.change_ranges[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must be an object")
        slot_id = _text(item.get("id"), f"{field}.id")
        assert slot_id is not None
        if slot_id in ids:
            raise ValueError(f"duplicate change-range slot id: {slot_id}")
        ids.add(slot_id)
        repo_id = _text(item.get("repository_id"), f"{field}.repository_id")
        assert repo_id is not None
        if repo_id not in repo_by_id:
            raise ValueError(f"{field}.repository_id is unknown: {repo_id}")
        per_repo[repo_id] = per_repo.get(repo_id, 0) + 1
        status = _text(item.get("status"), f"{field}.status")
        assert status is not None
        if status not in RANGE_STATUSES:
            raise ValueError(f"{field}.status must be one of "
                             f"{', '.join(sorted(RANGE_STATUSES))}")
        base = _commit_or_none(item.get("base"), f"{field}.base")
        head = _commit_or_none(item.get("head"), f"{field}.head")
        if status == "slot_unassigned":
            if base is not None or head is not None:
                raise ValueError(f"{field} is unassigned but records a base or head commit")
        elif status in {"assigned", "labeled"}:
            if base is None or head is None:
                raise ValueError(f"{field} needs base and head commits once assigned")
            if base == head:
                raise ValueError(f"{field}.base and head must differ")
        theme = _text(item.get("theme"), f"{field}.theme")
        assert theme is not None
        if theme not in REVIEW_THEMES:
            raise ValueError(f"{field}.theme must be one of {', '.join(sorted(REVIEW_THEMES))}")
        focus = _string_list(item.get("review_focus"), f"{field}.review_focus")
        if not focus:
            raise ValueError(f"{field}.review_focus must not be empty")
        source_hint = _text(item.get("source_hint"), f"{field}.source_hint")
        reason = _text(item.get("reason"), f"{field}.reason")
        holdout = _bool(item.get("holdout", False), f"{field}.holdout")
        if holdout != repo_by_id[repo_id]["holdout"]:
            raise ValueError(f"{field}.holdout must match its repository holdout flag")
        slots.append({
            "id": slot_id,
            "repository_id": repo_id,
            "status": status,
            "base": base,
            "head": head,
            "theme": theme,
            "review_focus": focus,
            "source_hint": source_hint,
            "reason": reason,
            "holdout": holdout,
        })
    missing = sorted(set(repo_by_id) - set(per_repo))
    if missing:
        raise ValueError("repositories without a change-range slot: " + ", ".join(missing))
    return slots


def validate_manifest(raw: Any) -> dict[str, Any]:
    """Validate and normalize a corpus manifest, raising ``ValueError``."""
    if not isinstance(raw, Mapping):
        raise ValueError("corpus manifest must be an object")
    schema = raw.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ValueError("manifest.schema_version must be an integer")
    if schema != CORPUS_SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest.schema_version {schema}; "
                         f"supported: {CORPUS_SCHEMA_VERSION}")
    corpus_id = _text(raw.get("corpus_id"), "manifest.corpus_id")
    assert corpus_id is not None
    description = _text(raw.get("description"), "manifest.description")
    created = _text(raw.get("created"), "manifest.created")
    status = _text(raw.get("status"), "manifest.status")
    evidence_class = _text(raw.get("evidence_class"), "manifest.evidence_class")
    assert evidence_class is not None
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError(f"manifest.evidence_class must be one of "
                         f"{', '.join(sorted(EVIDENCE_CLASSES))}")
    targets = raw.get("targets")
    if not isinstance(targets, Mapping):
        raise ValueError("manifest.targets must be an object")
    normalized_targets: dict[str, Any] = {}
    for key in ("repositories", "change_ranges", "holdout_repositories"):
        value = targets.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"manifest.targets.{key} must be a non-negative integer")
        normalized_targets[key] = value
    complete_target = targets.get("complete_repositories_for_quality")
    if complete_target is not None:
        if (isinstance(complete_target, bool) or not isinstance(complete_target, int)
                or complete_target < 1):
            raise ValueError("manifest.targets.complete_repositories_for_quality "
                             "must be a positive integer")
        normalized_targets["complete_repositories_for_quality"] = complete_target
    if evidence_class == "realistic_corpus":
        if normalized_targets["repositories"] < MIN_REALISTIC_REPOSITORIES:
            raise ValueError("realistic corpus targets at least 10 repositories")
        if normalized_targets["change_ranges"] < MIN_REALISTIC_RANGE_SLOTS:
            raise ValueError("realistic corpus targets at least 50 change ranges")
    review_policy = raw.get("review_policy")
    if not isinstance(review_policy, Mapping):
        raise ValueError("manifest.review_policy must be an object")
    reviewers = review_policy.get("reviewers")
    if reviewers != 2:
        raise ValueError("manifest.review_policy.reviewers must be 2")
    if review_policy.get("adjudicator") is not True:
        raise ValueError("manifest.review_policy.adjudicator must be true")
    if review_policy.get("independent_before_findings") is not True:
        raise ValueError("manifest.review_policy.independent_before_findings must be true")
    repositories = _repositories(raw.get("repositories"), evidence_class,
                                 normalized_targets)
    if len(repositories) < normalized_targets["repositories"]:
        raise ValueError("manifest has fewer repositories than targets.repositories")
    change_ranges = _change_ranges(raw.get("change_ranges"), repositories,
                                   evidence_class, normalized_targets)
    normalized = {
        "schema_version": schema,
        "corpus_id": corpus_id,
        "description": description,
        "created": created,
        "status": status,
        "evidence_class": evidence_class,
        "targets": normalized_targets,
        "review_policy": dict(review_policy),
        "repositories": repositories,
        "change_ranges": change_ranges,
    }
    normalized["digest"] = manifest_digest(normalized)
    return normalized


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Read, validate and normalize a corpus manifest."""
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"corpus manifest missing: {source}")
    if source.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"corpus manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid corpus manifest JSON in {source}: {exc}") from exc
    result = validate_manifest(raw)
    result["manifest_path"] = str(source)
    return result


def _repo_by_id(manifest: Mapping[str, Any], repo_id: str) -> Mapping[str, Any]:
    for item in manifest.get("repositories") or []:
        if item.get("id") == repo_id:
            return item
    raise CorpusError("unknown_repository", f"repository is not in the manifest: {repo_id}",
                      details={"repository_id": repo_id})


def _slot_by_key(manifest: Mapping[str, Any], repository_id: str,
                 subject_key: str) -> Mapping[str, Any] | None:
    if subject_key == "whole-repo":
        return None
    for item in manifest.get("change_ranges") or []:
        if item.get("id") == subject_key:
            if item.get("repository_id") != repository_id:
                raise CorpusError(
                    "subject_repository_mismatch",
                    f"subject {subject_key} belongs to {item.get('repository_id')}, "
                    f"not {repository_id}")
            return item
    raise CorpusError("unknown_subject", f"subject key is not in the manifest: {subject_key}",
                      details={"repository_id": repository_id, "subject_key": subject_key})


def checkout_path(manifest: Mapping[str, Any], repo_id: str, *,
                  checkout_root: str | Path | None = None) -> Path:
    repo = _repo_by_id(manifest, repo_id)
    base = (Path(checkout_root).expanduser().resolve() if checkout_root is not None
            else state_path(repo_root(), "eval", "deep_review_corpus"))
    return base / str(repo["local_checkout"])


def verify_checkout(checkout: str | Path, *,
                    expected_commit: str | None = None,
                    expected_tree_sha256: str | None = None) -> dict[str, Any]:
    """Verify a local checkout without importing or executing it."""
    path = Path(checkout).expanduser().resolve()
    if not path.is_dir():
        return {
            "checkout": str(path),
            "status": "missing",
            "exists": False,
            "commit_status": "not_checked",
            "tree_status": "not_checked",
        }
    head = None
    git_error = None
    if (path / ".git").exists():
        command, env = safe_git_command(path, "rev-parse", "HEAD")
        try:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=20, env=env, check=False)
            if proc.returncode == 0:
                head = proc.stdout.strip() or None
            else:
                git_error = proc.stderr.strip()[:500] or "git rev-parse failed"
        except (OSError, subprocess.SubprocessError) as exc:
            git_error = str(exc)
    if expected_commit is None:
        commit_status = "not_checked"
    elif head is None:
        commit_status = ("not_verifiable_without_git_metadata"
                         if not (path / ".git").exists() else "unverified")
    elif head == expected_commit:
        commit_status = "verified"
    else:
        commit_status = "mismatch"
    tree = tree_digest(path)
    if expected_tree_sha256 is None:
        tree_status = "not_checked"
    elif tree["sha256"] == expected_tree_sha256:
        tree_status = "verified"
    else:
        tree_status = "mismatch"
    checks = {commit_status, tree_status}
    if "mismatch" in checks:
        status = "mismatch"
    elif "verified" in checks and checks <= {"verified", "not_checked",
                                             "not_verifiable_without_git_metadata"}:
        status = "verified"
    else:
        status = "unverified"
    return {
        "checkout": str(path),
        "status": status,
        "exists": True,
        "head_commit": head,
        "expected_commit": expected_commit,
        "commit_status": commit_status,
        "git_error": git_error,
        "tree": tree,
        "expected_tree_sha256": expected_tree_sha256,
        "tree_status": tree_status,
    }


def _remove_tree(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError as exc:
        raise CorpusError("cleanup_failed", f"cannot remove {path}: {exc}") from exc


def _read_marker(path: Path) -> dict[str, Any] | None:
    marker = path / MARKER_NAME
    if not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _download_archive(url: str, *, timeout: float,
                      opener: Callable[..., Any] | None = None) -> tuple[bytes, str]:
    try:
        return _download(url, allowed_hosts=GITHUB_HOSTS, timeout=timeout,
                         max_bytes=MAX_ARCHIVE_BYTES, opener=opener,
                         accept="application/gzip, */*")
    except Exception as exc:  # BenchmarkError carries the stable reason.
        reason = getattr(exc, "reason", None)
        details = getattr(exc, "details", {}) or {}
        if reason:
            raise CorpusError(str(reason), str(exc), details=details) from exc
        raise


def acquire_repository(manifest: Mapping[str, Any], repo_id: str, *,
                       allow_network: bool = False,
                       timeout: float = 60.0,
                       checkout_root: str | Path | None = None,
                       opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Return a verified checkout, downloading a pinned archive only if allowed.

    With ``allow_network=False`` an absent checkout is a refusal, not a silent
    fallback.  A repository without a pinned commit cannot be acquired: a human
    must record the commit and tree digest first.
    """
    repo = _repo_by_id(manifest, repo_id)
    commit = repo.get("pinned_commit")
    if not commit:
        raise CorpusError(
            "unassigned_commit",
            f"{repo_id} has no pinned commit; acquire and pin it manually before download",
            details={"repository_id": repo_id})
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise CorpusError("invalid_argument", "timeout must be a positive number")
    destination = checkout_path(manifest, repo_id, checkout_root=checkout_root)
    expected_tree = repo.get("tree_sha256")
    expected_archive = repo.get("archive_sha256")
    if destination.exists():
        marker = _read_marker(destination)
        verification = verify_checkout(
            destination, expected_commit=commit, expected_tree_sha256=expected_tree)
        if marker is None and verification["status"] != "verified":
            raise CorpusError(
                "checkout_exists_unverified",
                f"existing checkout is not verifiable and has no acquisition marker: "
                f"{destination}",
                details={"repository_id": repo_id, "checkout": str(destination)})
        verification["repository_id"] = repo_id
        verification["archive_sha256"] = (marker or {}).get("archive_sha256")
        verification["acquisition_marker"] = marker
        return verification
    if not allow_network:
        raise CorpusError(
            "network_not_allowed",
            "repository archive download needs explicit --allow-network",
            details={"repository_id": repo_id, "repo": repo["repo"],
                     "pinned_commit": commit})
    url = f"{repo['remote']}/archive/{commit}.tar.gz"
    body, final_url = _download_archive(url, timeout=float(timeout), opener=opener)
    archive_sha = hashlib.sha256(body).hexdigest()
    if expected_archive is not None and archive_sha != expected_archive:
        raise CorpusError(
            "archive_digest_mismatch",
            f"downloaded archive digest does not match the manifest for {repo_id}",
            details={"expected": expected_archive, "actual": archive_sha})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        stats = extract_archive(body, temporary, max_bytes=MAX_EXTRACTED_BYTES)
        tree = tree_digest(temporary)
        if expected_tree is not None and tree["sha256"] != expected_tree:
            raise CorpusError(
                "tree_digest_mismatch",
                f"extracted tree digest does not match the manifest for {repo_id}",
                details={"repository_id": repo_id, "expected": expected_tree,
                         "actual": tree["sha256"]})
        marker = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "corpus_id": manifest.get("corpus_id"),
            "repository_id": repo_id,
            "repo": repo["repo"],
            "pinned_commit": commit,
            "source_url": final_url,
            "fetched_at": _utc_now(),
            "archive_bytes": len(body),
            "archive_sha256": archive_sha,
            "tree": tree,
            "expected_tree_sha256": expected_tree,
            "tree_verified": (tree["sha256"] == expected_tree
                              if expected_tree is not None else None),
            "extracted": stats,
        }
        _atomic_write_json(temporary / MARKER_NAME, marker)
        if destination.exists() or destination.is_symlink():
            _remove_tree(destination)
        os.replace(temporary, destination)
    except BaseException:
        _remove_tree(temporary)
        raise
    verification = verify_checkout(
        destination, expected_commit=commit, expected_tree_sha256=expected_tree)
    verification["repository_id"] = repo_id
    verification["archive_sha256"] = archive_sha
    verification["acquisition_marker"] = marker
    if expected_tree is None:
        verification["status"] = "acquired_pending_manifest_tree_pin"
    return verification


def manifest_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    repositories = list(manifest.get("repositories") or [])
    slots = list(manifest.get("change_ranges") or [])
    by_status: dict[str, int] = {}
    for item in repositories:
        status = str(item.get("acquisition_status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_id": manifest.get("corpus_id"),
        "evidence_class": manifest.get("evidence_class"),
        "manifest_digest": manifest.get("digest") or manifest_digest(manifest),
        "repositories": len(repositories),
        "acquisition_status": dict(sorted(by_status.items())),
        "holdout_repositories": sum(1 for item in repositories if item.get("holdout")),
        "change_range_slots": len(slots),
        "assigned_change_ranges": sum(
            1 for item in slots if item.get("status") in {"assigned", "labeled"}),
        "labeled_change_ranges": sum(1 for item in slots if item.get("status") == "labeled"),
        "targets": dict(manifest.get("targets") or {}),
        "languages": sorted({language for item in repositories
                             for language in item.get("languages") or []}),
    }


def verify_manifest_checkouts(manifest: Mapping[str, Any], *,
                              checkout_root: str | Path | None = None,
                              overrides: Mapping[str, str | Path] | None = None
                              ) -> dict[str, Any]:
    """Verify every manifest checkout that is present, offline."""
    overrides = dict(overrides or {})
    results: list[dict[str, Any]] = []
    counts = {"verified": 0, "mismatch": 0, "unverified": 0, "missing": 0}
    for repo in manifest.get("repositories") or []:
        repo_id = str(repo["id"])
        path = (Path(overrides[repo_id]).expanduser()
                if repo_id in overrides
                else checkout_path(manifest, repo_id, checkout_root=checkout_root))
        if repo_id not in overrides and not path.exists():
            result = {"checkout": str(path), "status": "missing", "exists": False,
                      "commit_status": "not_checked", "tree_status": "not_checked"}
        else:
            result = verify_checkout(
                path, expected_commit=repo.get("pinned_commit"),
                expected_tree_sha256=repo.get("tree_sha256"))
        result["repository_id"] = repo_id
        result["repo"] = repo.get("repo")
        result["acquisition_status"] = repo.get("acquisition_status")
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        results.append(result)
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_id": manifest.get("corpus_id"),
        "manifest_digest": manifest.get("digest") or manifest_digest(manifest),
        "checkout_root": str(checkout_root) if checkout_root is not None else None,
        "counts": dict(sorted(counts.items())),
        "repositories": results,
        "status": "verified" if counts.get("verified") == len(results) else "incomplete",
    }


def _write_json(value: Any, out: str | Path | None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if out is None:
        print(text, end="")
    else:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate the manifest offline")
    validate.add_argument("--out", type=Path)

    status = sub.add_parser("status", help="verify local checkouts offline")
    status.add_argument("--checkout-root", type=Path)
    status.add_argument("--checkout", action="append", default=[],
                        metavar="ID=PATH", help="verify a specific checkout path")
    status.add_argument("--out", type=Path)

    verify = sub.add_parser("verify", help="verify one checkout offline")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--checkout", required=True, type=Path)
    verify.add_argument("--out", type=Path)

    acquire = sub.add_parser("acquire", help="download and verify a pinned archive")
    acquire.add_argument("--repo", required=True)
    acquire.add_argument("--allow-network", action="store_true",
                         help="explicitly permit the GitHub archive download")
    acquire.add_argument("--timeout", type=float, default=60.0)
    acquire.add_argument("--checkout-root", type=Path)
    acquire.add_argument("--out", type=Path)

    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "validate":
            _write_json({**manifest_summary(manifest),
                         "status": "valid",
                         "manifest_path": manifest.get("manifest_path")}, args.out)
        elif args.command == "status":
            overrides: dict[str, str] = {}
            for item in args.checkout:
                if "=" not in item:
                    parser.error("--checkout must be ID=PATH")
                repo_id, path = item.split("=", 1)
                overrides[repo_id] = path
            _write_json(verify_manifest_checkouts(
                manifest, checkout_root=args.checkout_root, overrides=overrides), args.out)
        elif args.command == "verify":
            repo = _repo_by_id(manifest, args.repo)
            _write_json(verify_checkout(
                args.checkout, expected_commit=repo.get("pinned_commit"),
                expected_tree_sha256=repo.get("tree_sha256")), args.out)
        else:
            _write_json(acquire_repository(
                manifest, args.repo, allow_network=args.allow_network,
                timeout=args.timeout, checkout_root=args.checkout_root), args.out)
    except (CorpusError, ValueError, OSError) as exc:
        if isinstance(exc, CorpusError):
            payload = exc.to_dict()
        else:
            payload = {"schema_version": CORPUS_SCHEMA_VERSION, "status": "error",
                       "reason": "invalid_input", "error": str(exc)}
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
