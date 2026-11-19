"""Offline tests for Hugging Face benchmark ingestion and verification.

No test contacts the network: fetch refusal is checked before any opener call,
imports use local fixtures, and verification uses local checkouts or
monkeypatched storefronts.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ozagia.eval import hf_benchmarks as hf
from ozagia.eval.retrieval import load_tasks

FIXTURES = Path(__file__).parent / "fixtures" / "hf_benchmarks"
VERIFIED_ROWS = FIXTURES / "rows_verified.json"
LITE_ROWS = FIXTURES / "rows_lite.json"
DATASET_META = FIXTURES / "dataset_meta.json"


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


def _import_verified(tmp_path: Path, *, dataset_sha: str | None = None) -> Path:
    cache = _cache(tmp_path)
    hf.import_rows(
        VERIFIED_ROWS, "swe-bench-verified", cache_dir=cache,
        dataset_sha=dataset_sha)
    return cache


def _rows_document(rows: list[dict], *, dataset: str = "SWE-bench/SWE-bench_Verified",
                   config: str = "default", split: str = "test") -> dict:
    return {
        "dataset": dataset,
        "config": config,
        "split": split,
        "features": [],
        "rows": [{"row_idx": index, "row": row, "truncated_cells": []}
                 for index, row in enumerate(rows)],
    }


def _write_rows(path: Path, rows: list[dict], **kwargs) -> Path:
    path.write_text(json.dumps(_rows_document(rows, **kwargs)), encoding="utf-8")
    return path


def _tar_bytes(entries: list[tuple[str, bytes | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _tar_with_symlink(name: str, linkname: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        regular = tarfile.TarInfo("repo-sha/README.md")
        regular.size = 6
        archive.addfile(regular, io.BytesIO(b"hello\n"))
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = linkname
        archive.addfile(info)
    return buffer.getvalue()


def _checkout(cache: Path, repo: str, commit: str,
              files: dict[str, str]) -> Path:
    path = hf.checkout_path(
        "swe-bench-verified", repo, commit, cache_dir=cache)
    for relative, text in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (path / hf.CHECKOUT_MARKER).write_text(
        json.dumps({"repo": repo, "base_commit": commit}), encoding="utf-8")
    return path


def _fake_evaluate(recall: float = 1.0, content: float = 0.5):
    calls: list[dict] = []

    def evaluate(*, root, tasks_path, budgets, arms, cache_path):
        document = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
        calls.append({
            "root": Path(root),
            "tasks": document["tasks"],
            "budgets": list(budgets),
            "arms": list(arms),
            "cache_path": Path(cache_path),
        })
        aggregates = []
        for budget in budgets:
            for arm in arms:
                aggregates.append({
                    "budget_tokens": int(budget),
                    "arm": arm,
                    "tasks": len(document["tasks"]),
                    "mean_recall_at_budget": recall,
                    "mean_recall_content_at_budget": content,
                    "mean_precision": 0.25,
                    "mean_selected_files": 4.0,
                    "mean_content_files": 2.0,
                    "mean_estimated_tokens": 100.0,
                    "mean_elapsed_seconds": 0.01,
                    "cache_parsed": 0,
                    "cache_reused": 0,
                    "budget_respected_runs": len(document["tasks"]),
                    "recall_1_tasks": len(document["tasks"]),
                })
        return {
            "ok": True,
            "source_files": 12,
            "expected_files_outside_source_universe": ["pkg/not-indexed.py"],
            "aggregates": aggregates,
            "deterministic_fingerprint": "fixture-fingerprint",
            "task_set_summary": {"tasks": len(document["tasks"])},
        }

    return evaluate, calls


def _custom_rows(commits: tuple[str, str] = (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")) -> list[dict]:
    return [
        {
            "instance_id": "acme__widget-1",
            "repo": "acme/widget",
            "base_commit": commits[0],
            "problem_statement": "Fix the widget renderer for nested layouts.",
            "patch": (
                "diff --git a/pkg/a.py b/pkg/a.py\n"
                "--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-old\n+new\n"),
        },
        {
            "instance_id": "acme__widget-2",
            "repo": "acme/widget",
            "base_commit": commits[1],
            "problem_statement": "Validate widget configuration before saving.",
            "patch": (
                "diff --git a/pkg/b.py b/pkg/b.py\n"
                "--- a/pkg/b.py\n+++ b/pkg/b.py\n@@ -1 +1 @@\n-old\n+new\n"),
        },
    ]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_has_verified_and_lite_with_provenance_fields() -> None:
    assert set(hf.BENCHMARKS) == {"swe-bench-verified", "swe-bench-lite"}
    verified = hf.BENCHMARKS["swe-bench-verified"]
    lite = hf.BENCHMARKS["swe-bench-lite"]
    assert verified.dataset == "SWE-bench/SWE-bench_Verified"
    assert lite.dataset == "SWE-bench/SWE-bench_Lite"
    assert verified.config == lite.config == "default"
    assert verified.split == lite.split == "test"
    assert verified.max_rows == 500
    assert lite.max_rows == 300
    assert verified.license == "unknown"
    assert "not stated" in verified.license_status
    listed = hf.list_benchmarks()
    assert [entry["id"] for entry in listed] == [
        "swe-bench-verified", "swe-bench-lite"]
    assert listed[0]["url"].startswith(
        "https://datasets-server.huggingface.co/rows?")
    assert listed[0]["metadata_url"] == (
        "https://huggingface.co/api/datasets/SWE-bench/SWE-bench_Verified")


def test_list_cli_prints_the_registry(capsys: pytest.CaptureFixture[str]) -> None:
    assert hf.main(["list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in payload["benchmarks"]] == [
        "swe-bench-verified", "swe-bench-lite"]


def test_default_arms_exclude_graph_context_in_a_stable_order() -> None:
    assert hf.DEFAULT_ARMS == (
        "baseline", "code_search", "dev_repo_map", "grep")
    assert hf._parse_arms(",".join(hf.DEFAULT_ARMS)) == hf.DEFAULT_ARMS
    assert "dev_context" not in hf.DEFAULT_ARMS
    assert "both" not in hf.DEFAULT_ARMS


def test_license_is_unknown_without_an_explicit_statement() -> None:
    metadata = json.loads(DATASET_META.read_text(encoding="utf-8"))
    assert hf._license_from_metadata(metadata) == (
        "unknown", "not stated on dataset card or tags")
    assert hf._license_from_metadata({"tags": ["license:mit"]}) == (
        "mit", "stated in dataset tags")


# --------------------------------------------------------------------------- #
# Offline gate, import and cache validation
# --------------------------------------------------------------------------- #
def test_fetch_refuses_without_allow_network_and_opens_no_socket(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network opener must not be called")

    monkeypatch.setattr(hf, "_open_url", fail)
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.fetch_rows(
            "swe-bench-verified", allow_network=False,
            cache_dir=_cache(tmp_path))
    assert caught.value.reason == "network_not_allowed"
    assert caught.value.to_dict()["status"] == "refused"
    assert not _cache(tmp_path).exists()

    assert hf.main([
        "fetch", "--benchmark", "swe-bench-verified",
        "--cache-dir", str(_cache(tmp_path)),
    ]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "refused"
    assert payload["reason"] == "network_not_allowed"
    assert not _cache(tmp_path).exists()


def test_fetch_pages_and_caches_with_an_injected_transport(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads(VERIFIED_ROWS.read_text(encoding="utf-8"))
    metadata = json.loads(DATASET_META.read_text(encoding="utf-8"))
    offsets: list[int] = []

    def fake_open(request, timeout):  # noqa: ANN001, ANN002
        url = request.full_url
        if "api/datasets" in url:
            return json.dumps(metadata).encode("utf-8")
        if "datasets-server.huggingface.co/rows" in url:
            query = parse_qs(urlsplit(url).query)
            offset = int(query["offset"][0])
            offsets.append(offset)
            # Deliberately return one row per page to exercise pagination.
            return json.dumps({
                "features": fixture["features"],
                "rows": fixture["rows"][offset:offset + 1],
            }).encode("utf-8")
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(hf, "_open_url", fake_open)
    cache = _cache(tmp_path)
    result = hf.fetch_rows(
        "swe-bench-verified", allow_network=True, limit=3, cache_dir=cache)
    assert result["row_count"] == 3
    assert offsets == [0, 1, 2]
    assert result["dataset_sha"] == metadata["sha"]
    assert result["license"] == "unknown"
    loaded = hf.load_cached_rows("swe-bench-verified", cache_dir=cache)
    assert len(loaded["rows"]) == 3
    assert loaded["meta"]["origin"] == "network"
    assert loaded["meta"]["source_url"].startswith(
        "https://datasets-server.huggingface.co/rows?")
    assert loaded["meta"]["dataset_metadata"]["tags"]


def test_import_writes_digest_checked_rows_and_meta(tmp_path: Path) -> None:
    cache = _import_verified(tmp_path, dataset_sha="revision-1")
    directory = cache / "swe-bench-verified" / "revision-1"
    rows_path = directory / "rows.json"
    meta_path = directory / "meta.json"
    assert rows_path.is_file() and meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows_bytes = rows_path.read_bytes()
    assert meta["dataset"] == "SWE-bench/SWE-bench_Verified"
    assert meta["dataset_sha"] == "revision-1"
    assert meta["config"] == "default"
    assert meta["split"] == "test"
    assert meta["row_count"] == 4
    assert meta["sha256"] == hashlib.sha256(rows_bytes).hexdigest()
    assert meta["license"] == "unknown"
    assert "not stated" in meta["license_status"]
    assert meta["origin"] == "import"
    assert meta["imported_from"] == str(VERIFIED_ROWS.resolve())

    loaded = hf.load_cached_rows(
        "swe-bench-verified", cache_dir=cache)
    assert len(loaded["rows"]) == 4
    assert loaded["meta"]["row_count"] == 4
    assert loaded["dataset_sha"] == "revision-1"


def test_import_derives_a_local_revision_label(tmp_path: Path) -> None:
    cache = _import_verified(tmp_path)
    directories = list((cache / "swe-bench-verified").iterdir())
    assert len(directories) == 1
    assert directories[0].name.startswith("local-")
    assert (directories[0] / "rows.json").is_file()


def test_import_rejects_a_different_dataset_config_or_split(
        tmp_path: Path) -> None:
    with pytest.raises(hf.BenchmarkError, match="expected"):
        hf.import_rows(
            LITE_ROWS, "swe-bench-verified", cache_dir=_cache(tmp_path))
    bad_split = tmp_path / "bad_split.json"
    document = json.loads(VERIFIED_ROWS.read_text(encoding="utf-8"))
    document["split"] = "train"
    bad_split.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.import_rows(
            bad_split, "swe-bench-verified", cache_dir=_cache(tmp_path))
    assert caught.value.reason == "invalid_split"


def test_cache_rejects_a_corrupt_meta_digest(tmp_path: Path) -> None:
    cache = _import_verified(tmp_path, dataset_sha="revision-1")
    meta_path = cache / "swe-bench-verified" / "revision-1" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.load_cached_rows("swe-bench-verified", cache_dir=cache)
    assert caught.value.reason == "invalid_meta"
    assert "digest" in str(caught.value)


def test_cache_rejects_a_row_count_mismatch(tmp_path: Path) -> None:
    cache = _import_verified(tmp_path, dataset_sha="revision-1")
    meta_path = cache / "swe-bench-verified" / "revision-1" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["row_count"] = 99
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.load_cached_rows("swe-bench-verified", cache_dir=cache)
    assert caught.value.reason == "invalid_meta"
    assert "row_count" in str(caught.value)


# --------------------------------------------------------------------------- #
# Patch parsing and conversion
# --------------------------------------------------------------------------- #
def test_patch_parser_handles_multiple_files_duplicates_and_spaces() -> None:
    patch = (
        'diff --git "a/pkg/with space.py" "b/pkg/with space.py"\n'
        "--- \"a/pkg/with space.py\"\n"
        "+++ \"b/pkg/with space.py\"\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/pkg/second.py b/pkg/second.py\n"
        "--- a/pkg/second.py\n+++ b/pkg/second.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/pkg/second.py b/pkg/second.py\n"
        "--- a/pkg/second.py\n+++ b/pkg/second.py\n"
        "@@ -2 +2 @@\n-old\n+new\n"
    )
    assert hf.parse_patch_files(patch) == [
        "pkg/second.py", "pkg/with space.py"]


def test_patch_parser_skips_dev_null_sides_and_unsafe_paths() -> None:
    patch = (
        "--- a/pkg/removed.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-old\n"
        "--- /dev/null\n"
        "+++ b/pkg/added.py\n"
        "@@ -0,0 +1 @@\n+new\n"
        "--- a/../escape.py\n"
        "+++ b/../escape.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert hf.parse_patch_files(patch) == [
        "pkg/added.py", "pkg/removed.py"]
    assert hf.parse_patch_files("") == []
    assert hf.parse_patch_files("not a diff") == []


def test_patch_parser_uses_the_new_path_for_a_rename() -> None:
    patch = (
        "diff --git a/pkg/old_name.py b/pkg/new_name.py\n"
        "similarity index 100%\n"
        "rename from pkg/old_name.py\n"
        "rename to pkg/new_name.py\n"
    )
    assert hf.parse_patch_files(patch) == ["pkg/new_name.py"]


def test_conversion_writes_tasks_sources_and_reports_omissions(
        tmp_path: Path) -> None:
    cache = _import_verified(tmp_path, dataset_sha="revision-1")
    conversion = hf.convert_cached(
        "swe-bench-verified", cache_dir=cache)
    assert conversion["task_count"] == 2
    assert conversion["omission_count"] == 2
    assert conversion["omissions_by_reason"] == {
        "missing_problem_statement": 1,
        "no_parseable_files": 1,
    }
    tasks = load_tasks(conversion["tasks_path"])
    assert [task["id"] for task in tasks] == [
        "django__django-11099", "sympy__sympy-12345"]
    assert tasks[0]["expected_files"] == [
        "django/db/models.py", "django/db/query.py"]
    assert tasks[1]["expected_files"] == ["sympy/printing/str.py"]
    assert tasks[0]["task"].startswith("Fix the query compiler")

    sources = json.loads(
        Path(conversion["sources_path"]).read_text(encoding="utf-8"))
    assert sources["django__django-11099"] == {
        "repo": "django/django",
        "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "instance_id": "django__django-11099",
    }
    assert sources["sympy__sympy-12345"]["repo"] == "sympy/sympy"
    assert conversion["provenance"]["dataset_sha"] == "revision-1"
    assert conversion["provenance"]["license"] == "unknown"
    assert Path(conversion["tasks_path"]).is_file()
    assert Path(conversion["sources_path"]).is_file()


def test_task_text_is_bounded_and_ids_are_sanitized() -> None:
    row = {
        "instance_id": "owner/repo #42",
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "problem_statement": "x" * 100,
        "patch": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
    }
    conversion = hf.convert_rows(
        [row], benchmark="swe-bench-verified", dataset_sha="r1",
        text_limit=20)
    task = conversion["document"]["tasks"][0]
    assert task["id"] == "owner-repo-42"
    assert len(task["task"]) == 20
    assert conversion["truncated_tasks"] == 1
    assert hf.sanitize_task_id("django__django-11099") == (
        "django__django-11099")
    assert hf.sanitize_task_id("") is None


def test_conversion_omits_a_duplicate_sanitized_id() -> None:
    rows = [
        {
            "instance_id": "same id",
            "repo": "owner/repo",
            "base_commit": "a" * 40,
            "problem_statement": "one",
            "patch": "diff --git a/a.py b/a.py\n",
        },
        {
            "instance_id": "same-id",
            "repo": "owner/repo",
            "base_commit": "a" * 40,
            "problem_statement": "two",
            "patch": "diff --git a/b.py b/b.py\n",
        },
    ]
    conversion = hf.convert_rows(
        rows, benchmark="swe-bench-verified", dataset_sha="r1")
    assert conversion["task_count"] == 1
    assert conversion["omissions_by_reason"] == {"duplicate_task_id": 1}


def test_convert_cli_writes_an_explicit_tasks_file(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cache = _import_verified(tmp_path, dataset_sha="revision-1")
    tasks_path = tmp_path / "tasks.json"
    summary_path = tmp_path / "summary.md"
    assert hf.main([
        "convert", "--benchmark", "swe-bench-verified",
        "--cache-dir", str(cache), "--out-json", str(tasks_path),
        "--out-md", str(summary_path),
    ]) == 0
    assert tasks_path.is_file()
    assert tasks_path.with_name("sources.json").is_file()
    assert summary_path.is_file()
    assert "Omissions" in summary_path.read_text(encoding="utf-8")
    assert hf.main([
        "import", "--benchmark", "swe-bench-verified",
        "--cache-dir", str(cache), str(VERIFIED_ROWS),
    ]) == 0
    assert "row_count" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Archive extraction safety (local tarballs only)
# --------------------------------------------------------------------------- #
def test_extract_archive_strips_the_top_directory(tmp_path: Path) -> None:
    archive = _tar_bytes([
        ("repo-sha/", None),
        ("repo-sha/README.md", b"hello\n"),
        ("repo-sha/pkg/mod.py", b"value = 1\n"),
        ("repo-sha/pkg/", None),
    ])
    target = tmp_path / "checkout"
    stats = hf.extract_archive(archive, target)
    assert stats["top_level"] == "repo-sha"
    assert stats["files"] == 2
    assert (target / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert (target / "pkg" / "mod.py").read_text(encoding="utf-8") == (
        "value = 1\n")
    assert not (target / "repo-sha").exists()


def test_extract_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = _tar_bytes([("repo-sha/../escape.py", b"bad\n")])
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.extract_archive(archive, tmp_path / "checkout")
    assert caught.value.reason == "path_traversal"
    assert not (tmp_path / "escape.py").exists()


def test_extract_archive_skips_symlinks_without_creating_them(
        tmp_path: Path) -> None:
    archive = _tar_with_symlink("repo-sha/link", "/tmp/escape")
    target = tmp_path / "checkout"
    stats = hf.extract_archive(archive, target)
    assert stats["skipped_links"] == 1
    assert (target / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert not (target / "link").exists()
    assert not (tmp_path / "escape").exists()


def test_extract_archive_rejects_absolute_paths_and_multiple_roots(
        tmp_path: Path) -> None:
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.extract_archive(
            _tar_bytes([("/tmp/escape.py", b"bad\n")]),
            tmp_path / "checkout")
    assert caught.value.reason == "path_traversal"
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.extract_archive(
            _tar_bytes([("one/a.py", b"a\n"), ("two/b.py", b"b\n")]),
            tmp_path / "checkout")
    assert caught.value.reason == "bad_archive"


def test_checkout_path_rejects_traversal_components(tmp_path: Path) -> None:
    with pytest.raises(hf.BenchmarkError):
        hf.checkout_path(
            "swe-bench-verified", "../escape", "a" * 40,
            cache_dir=_cache(tmp_path))


# --------------------------------------------------------------------------- #
# Verification aggregation
# --------------------------------------------------------------------------- #
def test_verify_uses_cached_checkouts_and_aggregates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(tmp_path)
    first = "a" * 40
    rows_path = _write_rows(
        tmp_path / "rows.json", _custom_rows((first, first)))
    hf.import_rows(
        rows_path, "swe-bench-verified", cache_dir=cache,
        dataset_sha="revision-1")
    hf.convert_cached("swe-bench-verified", cache_dir=cache)
    checkout = _checkout(
        cache, "acme/widget", first,
        {"pkg/a.py": "a = 1\n", "pkg/b.py": "b = 2\n"})
    evaluate, calls = _fake_evaluate(recall=0.75, content=0.5)
    monkeypatch.setattr(hf, "retrieval_evaluate", evaluate)

    report = hf.verify(
        "swe-bench-verified", cache_dir=cache, allow_network=False,
        budgets=[500], arms=["baseline", "grep"], limit=10)

    assert report["status"] == "ok"
    assert len(calls) == 1
    assert calls[0]["root"] == checkout.resolve()
    assert calls[0]["budgets"] == [500]
    assert calls[0]["arms"] == ["baseline", "grep"]
    assert report["overall"]["task_count"] == 2
    assert report["overall"]["assessed_task_count"] == 2
    assert report["overall"]["not_assessed_task_count"] == 0
    assert report["repos"][0]["status"] == "assessed"
    assert report["repos"][0]["universe_misses"] == [{
        "repo": "acme/widget",
        "base_commit": "a" * 40,
        "path": "pkg/not-indexed.py",
    }]
    assert len(report["overall"]["aggregates"]) == 2
    assert all(row["tasks"] == 2 for row in report["overall"]["aggregates"])
    assert all(
        row["mean_recall_at_budget"] == 0.75
        for row in report["overall"]["aggregates"])
    assert report["by_repo"][0]["repo"] == "acme/widget"
    assert report["by_repo"][0]["assessed_task_count"] == 2
    assert "Overall metrics" in report["markdown"]
    assert "not a SWE-bench pass rate" in report["markdown"]


def test_verify_refuses_missing_checkouts_without_network(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    cache = _cache(tmp_path)
    rows_path = _write_rows(tmp_path / "rows.json", _custom_rows())
    hf.import_rows(
        rows_path, "swe-bench-verified", cache_dir=cache,
        dataset_sha="revision-1")
    hf.convert_cached("swe-bench-verified", cache_dir=cache)

    def fail(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("evaluation must not run without a checkout")

    monkeypatch.setattr(hf, "retrieval_evaluate", fail)
    with pytest.raises(hf.BenchmarkError) as caught:
        hf.verify(
            "swe-bench-verified", cache_dir=cache, allow_network=False,
            budgets=[500], arms=["baseline"])
    assert caught.value.reason == "network_not_allowed"
    assert caught.value.details["repositories"]

    assert hf.main([
        "verify", "--benchmark", "swe-bench-verified",
        "--cache-dir", str(cache), "--budgets", "500",
        "--arms", "baseline",
    ]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["reason"] == "network_not_allowed"


def test_verify_dry_run_plans_without_network_or_evaluation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(tmp_path)
    rows_path = _write_rows(tmp_path / "rows.json", _custom_rows())
    hf.import_rows(
        rows_path, "swe-bench-verified", cache_dir=cache,
        dataset_sha="revision-1")
    hf.convert_cached("swe-bench-verified", cache_dir=cache)
    monkeypatch.setattr(
        hf, "retrieval_evaluate",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("no eval")))
    report = hf.verify(
        "swe-bench-verified", cache_dir=cache, allow_network=False,
        budgets=[500], arms=["baseline"], dry_run=True)
    assert report["dry_run"] is True
    assert report["overall"]["assessed_task_count"] == 0
    assert report["repos"][0]["status"] == "planned"
    assert report["repos"][0]["action"] == "would_download"


def test_verify_records_download_failures_and_continues(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(tmp_path)
    first = "a" * 40
    second = "b" * 40
    rows_path = _write_rows(
        tmp_path / "rows.json", _custom_rows((first, second)))
    hf.import_rows(
        rows_path, "swe-bench-verified", cache_dir=cache,
        dataset_sha="revision-1")
    hf.convert_cached("swe-bench-verified", cache_dir=cache)
    cached_checkout = _checkout(
        cache, "acme/widget", first, {"pkg/a.py": "a = 1\n"})
    evaluate, calls = _fake_evaluate()
    monkeypatch.setattr(hf, "retrieval_evaluate", evaluate)

    def fake_ensure(benchmark, repo, base_commit, **kwargs):  # noqa: ANN001
        if base_commit == first:
            return cached_checkout
        raise hf.BenchmarkError(
            "network_error", "archive unavailable", benchmark="swe-bench-verified")

    monkeypatch.setattr(hf, "ensure_checkout", fake_ensure)
    report = hf.verify(
        "swe-bench-verified", cache_dir=cache, allow_network=True,
        budgets=[500], arms=["baseline"], limit=10)
    assert len(calls) == 1
    assert report["overall"]["assessed_task_count"] == 1
    assert report["overall"]["not_assessed_task_count"] == 1
    statuses = {entry["base_commit"]: entry["status"]
                for entry in report["repos"]}
    assert statuses == {first: "assessed", second: "not_assessed"}
    assert report["not_assessed"][0]["reason"] == "network_error"


def test_verify_omits_tasks_whose_gold_files_are_absent_at_base(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _cache(tmp_path)
    rows_path = _write_rows(tmp_path / "rows.json", _custom_rows()[:1])
    hf.import_rows(
        rows_path, "swe-bench-verified", cache_dir=cache,
        dataset_sha="revision-1")
    hf.convert_cached("swe-bench-verified", cache_dir=cache)
    _checkout(cache, "acme/widget", "a" * 40, {"pkg/other.py": "x = 1\n"})
    monkeypatch.setattr(
        hf, "retrieval_evaluate",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("no eval")))
    report = hf.verify(
        "swe-bench-verified", cache_dir=cache, allow_network=False,
        budgets=[500], arms=["baseline"], limit=10)
    assert report["overall"]["assessed_task_count"] == 0
    assert report["overall"]["not_assessed_task_count"] == 1
    assert report["repos"][0]["status"] == "not_assessed"
    assert report["repos"][0]["reason"] == "no_expected_files_at_base"
    assert report["not_assessed"][0]["reason"] == "no_expected_files_at_base"
