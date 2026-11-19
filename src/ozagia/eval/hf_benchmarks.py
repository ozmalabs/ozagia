"""Hugging Face benchmark ingestion and retrieval verification.

This module turns public benchmark datasets (currently SWE-bench Verified and
SWE-bench Lite) into the explicit task format consumed by
:mod:`ozagia.eval.retrieval`, and verifies which files Ozagia's retrieval arms
surface for each issue.

It is deliberately narrow:

* Network access is opt-in (``allow_network=True``). Without it the module
  refuses before opening a socket. ``fetch`` downloads bounded dataset pages;
  ``verify`` downloads GitHub archive tarballs only for repositories that are
  not already cached.
* Nothing here executes target repository code, runs tests, calls a model, or
  claims that an agent fixed an issue. The measured question is file
  localisation: do the gold patch's files appear inside a token budget?
* Data is cached under ``<state>/eval/hf/<benchmark>/``. Raw rows and their
  metadata are written atomically, with the dataset revision in the path so a
  later dataset update cannot silently mix revisions.

Cache layout::

    <state>/eval/hf/<benchmark>/<dataset_sha>/rows.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/meta.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/tasks.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/sources.json
    <state>/eval/hf/<benchmark>/<dataset_sha>/runs/<owner>/<name>/<commit>/...
    <state>/eval/hf/<benchmark>/repos/<owner>/<name>/<commit>/...

``--cache-dir`` replaces the ``<state>/eval/hf`` prefix. Tests use local rows
and local tarballs, never the network.

CLI::

    python -m ozagia.eval.hf_benchmarks list
    python -m ozagia.eval.hf_benchmarks fetch --benchmark swe-bench-verified \\
        --allow-network --limit 10
    python -m ozagia.eval.hf_benchmarks import --benchmark swe-bench-verified \\
        tests/fixtures/hf_benchmarks/rows_verified.json
    python -m ozagia.eval.hf_benchmarks convert --benchmark swe-bench-verified
    python -m ozagia.eval.hf_benchmarks verify --benchmark swe-bench-verified \\
        --allow-network --budgets 500,900,1500
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ozagia.config import repo_root, state_path
from ozagia.eval import retrieval
from ozagia.eval.retrieval import ARMS as RETRIEVAL_ARMS

ROOT = repo_root()
SCHEMA = 1
ROWS_SCHEMA = "ozagia.hf_benchmark.rows.v1"
META_SCHEMA = "ozagia.hf_benchmark.meta.v1"
FETCH_SCHEMA = "ozagia.hf_benchmark.fetch.v1"
CONVERT_SCHEMA = "ozagia.hf_benchmark.convert.v1"
VERIFY_SCHEMA = "ozagia.hf_benchmark.verify.v1"
USER_AGENT = "ozagia-eval-benchmarks/0.1 (+https://github.com/ozmalabs/ozagia)"
DEFAULT_LIMIT = 10
MAX_FETCH_ROWS = 100
MAX_PAGE_ROWS = 100
MAX_ROWS_BYTES = 32 * 1024 * 1024
MAX_TOTAL_ROWS_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 200_000
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 300.0
MAX_REDIRECTS = 3
MAX_URL_LENGTH = 4096
TASK_TEXT_LIMIT = 8000
DEFAULT_BUDGETS = (500, 900, 1500)
DEFAULT_ARMS = ("baseline", "code_search", "dev_repo_map", "grep")
DATASET_HOSTS = frozenset({"datasets-server.huggingface.co", "huggingface.co"})
GITHUB_HOSTS = frozenset({"github.com", "codeload.github.com"})
CHECKOUT_MARKER = ".ozagia-hf-checkout.json"
_LICENSE_UNKNOWN = "unknown"
_LICENSE_STATUS_UNKNOWN = "not stated on dataset card or tags"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _validate_repo(repo: Any, *, benchmark: str | None = None) -> str:
    text = str(repo or "").strip()
    if not _REPO_SLUG.fullmatch(text):
        raise BenchmarkError(
            "invalid_repo", f"invalid repository {repo!r}", benchmark=benchmark)
    owner, name = text.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise BenchmarkError(
            "invalid_repo", f"invalid repository {repo!r}", benchmark=benchmark)
    return text


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Benchmark:
    """One benchmark dataset and its acquisition policy."""

    id: str
    title: str
    dataset: str
    config: str
    split: str
    description: str
    max_rows: int
    license: str = _LICENSE_UNKNOWN
    license_status: str = _LICENSE_STATUS_UNKNOWN

    @property
    def metadata_url(self) -> str:
        return f"https://huggingface.co/api/datasets/{self.dataset}"

    @property
    def dataset_url(self) -> str:
        return f"https://huggingface.co/datasets/{self.dataset}"

    @property
    def url(self) -> str:
        return self.rows_url()

    def rows_url(self, offset: int = 0, length: int = MAX_PAGE_ROWS) -> str:
        query = urlencode({
            "dataset": self.dataset,
            "config": self.config,
            "split": self.split,
            "offset": int(offset),
            "length": int(length),
        })
        return f"https://datasets-server.huggingface.co/rows?{query}"

    def archive_url(self, repo: str, base_commit: str) -> str:
        return f"https://github.com/{repo}/archive/{base_commit}.tar.gz"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "id": self.id,
            "title": self.title,
            "dataset": self.dataset,
            "config": self.config,
            "split": self.split,
            "url": self.url,
            "metadata_url": self.metadata_url,
            "dataset_url": self.dataset_url,
            "description": self.description,
            "license": self.license,
            "license_status": self.license_status,
            "max_rows": self.max_rows,
        }


BENCHMARK_LIST: tuple[Benchmark, ...] = (
    Benchmark(
        id="swe-bench-verified",
        title="SWE-bench Verified",
        dataset="SWE-bench/SWE-bench_Verified",
        config="default",
        split="test",
        description=(
            "500 human-validated SWE-bench test instances. Gold-file "
            "localisation only; no patch execution or agent-success claim."),
        max_rows=500,
    ),
    Benchmark(
        id="swe-bench-lite",
        title="SWE-bench Lite",
        dataset="SWE-bench/SWE-bench_Lite",
        config="default",
        split="test",
        description=(
            "300-instance curated SWE-bench subset. Gold-file localisation "
            "only; no patch execution or agent-success claim."),
        max_rows=300,
    ),
)
BENCHMARKS: dict[str, Benchmark] = {b.id: b for b in BENCHMARK_LIST}
REGISTRY = BENCHMARKS


class BenchmarkError(RuntimeError):
    """A benchmark operation failed safely.

    ``reason`` is a short stable code. Refusals caused by the offline network
    gate use ``network_not_allowed`` and carry ``status="refused"``.
    """

    def __init__(self, reason: str, message: str, *,
                 benchmark: str | None = None,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.benchmark = benchmark
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "benchmark": self.benchmark,
            "status": ("refused" if self.reason == "network_not_allowed"
                       else "error"),
            "reason": self.reason,
            "error": str(self),
            "details": dict(self.details),
        }


def get_benchmark(benchmark: str | Benchmark) -> Benchmark:
    if isinstance(benchmark, Benchmark):
        return benchmark
    key = str(benchmark or "").strip()
    if key not in BENCHMARKS:
        raise BenchmarkError(
            "unknown_benchmark",
            f"unknown benchmark {key!r}; choose one of "
            f"{', '.join(BENCHMARKS)}")
    return BENCHMARKS[key]


def list_benchmarks() -> list[dict[str, Any]]:
    """The registry as plain dictionaries, in stable registry order."""
    return [benchmark.to_dict() for benchmark in BENCHMARK_LIST]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise BenchmarkError(
            "write_failed", f"cannot write {path}: {exc}",
            details={"path": str(path)}) from exc


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _safe_segment(value: Any, *, what: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_SEGMENT.fullmatch(text):
        raise BenchmarkError(
            "invalid_identifier", f"{what} is not a safe path segment: {text!r}")
    return text


def _cache_root(root: str | Path | None = None,
                cache_dir: str | Path | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    base = Path(root).resolve() if root is not None else ROOT
    return state_path(base, "eval", "hf")


def _benchmark_cache(benchmark: Benchmark, cache_root: Path) -> Path:
    return cache_root / benchmark.id


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError("invalid_argument", "timeout must be a number")
    timeout = float(value)
    if not 0 < timeout <= MAX_TIMEOUT_S:
        raise BenchmarkError(
            "invalid_argument",
            f"timeout must be between 0 and {int(MAX_TIMEOUT_S)} seconds")
    return timeout


def _validate_limit(value: Any, *, default: int | None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchmarkError("invalid_argument", "limit must be an integer")
    if value < 1 or value > MAX_FETCH_ROWS:
        raise BenchmarkError(
            "invalid_argument",
            f"limit must be between 1 and {MAX_FETCH_ROWS}")
    return value


def _validate_offset(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchmarkError("invalid_argument", "offset must be an integer")
    if value < 0:
        raise BenchmarkError("invalid_argument", "offset must be non-negative")
    return value


def _parse_budgets(values: Iterable[int] | str) -> tuple[int, ...]:
    if isinstance(values, str):
        raw = [part.strip() for part in values.split(",") if part.strip()]
        try:
            parsed = [int(part) for part in raw]
        except ValueError as exc:
            raise BenchmarkError(
                "invalid_argument", f"invalid budgets value: {values!r}") from exc
    else:
        parsed = [int(value) for value in values]
    if not parsed:
        raise BenchmarkError("invalid_argument", "at least one budget is required")
    if any(value <= 0 for value in parsed):
        raise BenchmarkError("invalid_argument", "budgets must be positive integers")
    return tuple(sorted(set(parsed)))


def _parse_arms(values: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        raw = [part.strip() for part in values.split(",")]
    else:
        raw = [str(part).strip() for part in values]
    wanted: list[str] = []
    for arm in raw:
        if arm and arm not in wanted:
            wanted.append(arm)
    unknown = sorted(set(wanted) - set(RETRIEVAL_ARMS))
    if unknown:
        raise BenchmarkError(
            "invalid_argument", f"unknown arms: {', '.join(unknown)}")
    if not wanted:
        raise BenchmarkError("invalid_argument", "at least one arm is required")
    return tuple(wanted)


def _ordered_aggregates(rows: Iterable[Mapping[str, Any]],
                        budgets: Sequence[int],
                        arms: Sequence[str]) -> list[dict[str, Any]]:
    by_key = {
        (int(row.get("budget_tokens") or 0), row.get("arm")): dict(row)
        for row in rows
    }
    return [by_key[(budget, arm)] for budget in budgets for arm in arms
            if (budget, arm) in by_key]


# --------------------------------------------------------------------------- #
# Network acquisition (explicit, bounded, host allow-listed)
# --------------------------------------------------------------------------- #
class _NoRedirect(HTTPRedirectHandler):
    """Follow redirects explicitly so every hop is validated and capped."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_url(request: Request, timeout: float):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _validate_url(url: str, allowed_hosts: Iterable[str], *,
                  benchmark: str | None = None) -> str:
    if not isinstance(url, str) or not url.strip():
        raise BenchmarkError("bad_url", "URL must be a non-empty string",
                             benchmark=benchmark)
    raw = url.strip()
    if len(raw) > MAX_URL_LENGTH or any(ord(char) < 32 for char in raw):
        raise BenchmarkError(
            "bad_url", "URL is too long or contains control characters",
            benchmark=benchmark)
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise BenchmarkError("bad_url", f"URL is invalid: {exc}",
                             benchmark=benchmark) from exc
    if parts.scheme.lower() != "https":
        raise BenchmarkError("bad_url", "URL and redirects must use HTTPS",
                             benchmark=benchmark)
    if parts.username is not None or parts.password is not None:
        raise BenchmarkError("bad_url", "URL must not contain credentials",
                             benchmark=benchmark)
    if not parts.hostname:
        raise BenchmarkError("bad_url", "URL has no host", benchmark=benchmark)
    host = parts.hostname.lower()
    if host not in {item.lower() for item in allowed_hosts}:
        raise BenchmarkError(
            "host_not_allowed", f"host {host!r} is not allow-listed",
            benchmark=benchmark, details={"host": host})
    if port not in (None, 443):
        raise BenchmarkError(
            "bad_url", "URL must use the default HTTPS port",
            benchmark=benchmark)
    return parts.geturl()


def _download(url: str, *, allowed_hosts: Iterable[str], timeout: float,
              max_bytes: int, opener: Callable[..., Any] | None = None,
              benchmark: str | None = None,
              accept: str = "application/json") -> tuple[bytes, str]:
    """Download a bounded body, validating every redirect hop."""
    allowed = tuple(allowed_hosts)
    current = _validate_url(url, allowed, benchmark=benchmark)
    open_url = opener or _open_url
    for redirects in range(MAX_REDIRECTS + 1):
        request = Request(current, headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        })
        try:
            response = open_url(request, timeout=timeout)
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            try:
                if exc.code not in {301, 302, 303, 307, 308} or not location:
                    raise BenchmarkError(
                        "network_error", f"HTTP {exc.code} from {current}",
                        benchmark=benchmark,
                        details={"status": exc.code}) from exc
                if redirects >= MAX_REDIRECTS:
                    raise BenchmarkError(
                        "network_error",
                        f"download exceeded {MAX_REDIRECTS} redirects",
                        benchmark=benchmark) from exc
                current = _validate_url(
                    urljoin(current, location), allowed, benchmark=benchmark)
            finally:
                exc.close()
            continue
        except (TimeoutError, URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                raise BenchmarkError(
                    "timeout", f"download timed out after {timeout:g}s",
                    benchmark=benchmark) from exc
            raise BenchmarkError(
                "network_error", f"download failed: {reason}",
                benchmark=benchmark) from exc
        except OSError as exc:
            raise BenchmarkError(
                "network_error", f"download failed: {exc}",
                benchmark=benchmark) from exc

        if isinstance(response, (bytes, bytearray)):
            if len(response) > max_bytes:
                raise BenchmarkError(
                    "too_large",
                    f"download exceeds the {max_bytes}-byte limit",
                    benchmark=benchmark)
            return bytes(response), current

        try:
            final = getattr(response, "geturl", lambda: current)()
            final = _validate_url(final, allowed, benchmark=benchmark)
            code = getattr(response, "status", None)
            if code is None and hasattr(response, "getcode"):
                code = response.getcode()
            if code is not None and not 200 <= int(code) < 300:
                raise BenchmarkError(
                    "network_error", f"HTTP {code} from {current}",
                    benchmark=benchmark, details={"status": int(code)})
            declared = getattr(response, "headers", {}).get("Content-Length")
            try:
                declared_bytes = int(declared) if declared is not None else None
            except (TypeError, ValueError):
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > max_bytes:
                raise BenchmarkError(
                    "too_large",
                    f"download exceeds the {max_bytes}-byte limit",
                    benchmark=benchmark,
                    details={"content_length": declared_bytes})
            chunks = bytearray()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise BenchmarkError(
                        "too_large",
                        f"download exceeds the {max_bytes}-byte limit",
                        benchmark=benchmark)
            return bytes(chunks), final
        except BenchmarkError:
            raise
        except (OSError, ValueError) as exc:
            raise BenchmarkError(
                "network_error", f"download failed: {exc}",
                benchmark=benchmark) from exc
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    raise BenchmarkError(
        "network_error", f"download exceeded {MAX_REDIRECTS} redirects",
        benchmark=benchmark)


def _decode_json(body: bytes, url: str, *,
                 benchmark: str | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(
            "bad_payload", f"response from {url} is not JSON: {exc}",
            benchmark=benchmark) from exc
    if not isinstance(payload, dict):
        raise BenchmarkError(
            "bad_payload", f"response from {url} is not a JSON object",
            benchmark=benchmark)
    return payload


def _fetch_json(url: str, *, allowed_hosts: Iterable[str], timeout: float,
                max_bytes: int, opener: Callable[..., Any] | None = None,
                benchmark: str | None = None) -> dict[str, Any]:
    body, _final = _download(
        url, allowed_hosts=allowed_hosts, timeout=timeout, max_bytes=max_bytes,
        opener=opener, benchmark=benchmark)
    return _decode_json(body, url, benchmark=benchmark)


def _license_from_metadata(metadata: Mapping[str, Any]) -> tuple[str, str]:
    """Return an explicit licence if one is stated; never infer one."""
    card = metadata.get("cardData")
    if isinstance(card, Mapping):
        licence = card.get("license")
        if isinstance(licence, list):
            values = [str(v).strip() for v in licence if str(v).strip()]
            if values:
                return values[0], "stated in dataset cardData"
        elif isinstance(licence, str) and licence.strip():
            return licence.strip(), "stated in dataset cardData"
    tags = metadata.get("tags")
    if isinstance(tags, list):
        licences = [str(tag).split(":", 1)[1].strip() for tag in tags
                    if isinstance(tag, str) and tag.startswith("license:")
                    and tag.split(":", 1)[1].strip()]
        if licences:
            return licences[0], "stated in dataset tags"
    return _LICENSE_UNKNOWN, _LICENSE_STATUS_UNKNOWN


def fetch_dataset_metadata(benchmark: str | Benchmark, *,
                           allow_network: bool = False,
                           timeout: float = DEFAULT_TIMEOUT_S,
                           opener: Callable[..., Any] | None = None
                           ) -> dict[str, Any]:
    """Fetch the dataset revision and the explicitly stated licence."""
    bench = get_benchmark(benchmark)
    if not allow_network:
        raise BenchmarkError(
            "network_not_allowed",
            "dataset metadata fetch needs explicit allow_network=True",
            benchmark=bench.id,
            details={"url": bench.metadata_url})
    timeout = _validate_timeout(timeout)
    metadata = _fetch_json(
        bench.metadata_url, allowed_hosts=DATASET_HOSTS, timeout=timeout,
        max_bytes=MAX_METADATA_BYTES, opener=opener, benchmark=bench.id)
    dataset_sha = str(metadata.get("sha") or "").strip()
    if not dataset_sha:
        raise BenchmarkError(
            "bad_payload", "dataset metadata has no revision sha",
            benchmark=bench.id, details={"url": bench.metadata_url})
    _safe_segment(dataset_sha, what="dataset sha")
    licence, licence_status = _license_from_metadata(metadata)
    tags = metadata.get("tags")
    clean_tags = ([str(tag)[:200] for tag in tags[:100]]
                  if isinstance(tags, list) else [])
    return {
        "dataset": bench.dataset,
        "dataset_sha": dataset_sha,
        "license": licence,
        "license_status": licence_status,
        "last_modified": metadata.get("lastModified"),
        "tags": clean_tags,
        "url": bench.metadata_url,
    }


# --------------------------------------------------------------------------- #
# Row documents and cache validation
# --------------------------------------------------------------------------- #
def _normalise_rows(raw_rows: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise BenchmarkError("bad_payload", f"{where}: rows must be a list")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw_rows):
        if not isinstance(item, dict):
            raise BenchmarkError(
                "bad_payload", f"{where}: rows[{index}] must be an object")
        row = item
        if ("row" in item and isinstance(item["row"], dict)
                and not any(key in item for key in
                            ("instance_id", "problem_statement", "patch"))):
            row = item["row"]
        if not isinstance(row, dict) or not row:
            raise BenchmarkError(
                "bad_payload", f"{where}: rows[{index}] is not a row object")
        rows.append(dict(row))
    return rows


def _normalise_features(raw: Any, where: str) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BenchmarkError("bad_payload", f"{where}: features must be a list")
    return list(raw)


def _read_rows_document(path: str | Path) -> tuple[dict[str, Any],
                                                   list[dict[str, Any]],
                                                   list[Any]]:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise BenchmarkError(
            "read_failed", f"cannot read rows file {source}: {exc}",
            details={"path": str(source)}) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(
            "bad_payload", f"rows file {source} is not valid JSON: {exc}",
            details={"path": str(source)}) from exc
    if isinstance(data, list):
        document: dict[str, Any] = {}
        raw_rows = data
    elif isinstance(data, dict):
        document = dict(data)
        raw_rows = data.get("rows")
    else:
        raise BenchmarkError(
            "bad_payload", f"rows file {source} must be an object or list",
            details={"path": str(source)})
    rows = _normalise_rows(raw_rows, str(source))
    features = _normalise_features(document.get("features"), str(source))
    return document, rows, features


def _rows_document(*, benchmark: Benchmark, dataset_sha: str,
                   rows: Sequence[Mapping[str, Any]],
                   features: Sequence[Any], source_url: str,
                   fetched_at: str, offset: int, limit: int,
                   origin: str) -> dict[str, Any]:
    return {
        "schema": ROWS_SCHEMA,
        "benchmark": benchmark.id,
        "dataset": benchmark.dataset,
        "dataset_sha": dataset_sha,
        "config": benchmark.config,
        "split": benchmark.split,
        "source_url": source_url,
        "fetched_at": fetched_at,
        "offset": int(offset),
        "limit": int(limit),
        "origin": origin,
        "row_count": len(rows),
        "features": list(features),
        "rows": [dict(row) for row in rows],
    }


def _write_cache(*, benchmark: Benchmark, cache_root: Path,
                 dataset_sha: str, rows: Sequence[Mapping[str, Any]],
                 features: Sequence[Any], source_url: str,
                 metadata_url: str, fetched_at: str, offset: int, limit: int,
                 origin: str, license_text: str, license_status: str,
                 meta_details: Mapping[str, Any] | None = None,
                 imported_from: str | None = None,
                 import_sha256: str | None = None) -> dict[str, Any]:
    _safe_segment(dataset_sha, what="dataset sha")
    destination = _benchmark_cache(benchmark, cache_root) / dataset_sha
    document = _rows_document(
        benchmark=benchmark, dataset_sha=dataset_sha, rows=rows,
        features=features, source_url=source_url, fetched_at=fetched_at,
        offset=offset, limit=limit, origin=origin)
    payload = _json_bytes(document)
    digest = hashlib.sha256(payload).hexdigest()
    meta: dict[str, Any] = {
        "schema": META_SCHEMA,
        "benchmark": benchmark.id,
        "title": benchmark.title,
        "dataset": benchmark.dataset,
        "dataset_sha": dataset_sha,
        "config": benchmark.config,
        "split": benchmark.split,
        "source_url": source_url,
        "metadata_url": metadata_url,
        "fetched_at": fetched_at,
        "license": license_text,
        "license_status": license_status,
        "row_count": len(rows),
        "sha256": digest,
        "rows_sha256": digest,
        "bytes": len(payload),
        "origin": origin,
    }
    if meta_details:
        meta["dataset_metadata"] = dict(meta_details)
    if imported_from is not None:
        meta["imported_from"] = imported_from
    if import_sha256 is not None:
        meta["import_sha256"] = import_sha256
    rows_path = destination / "rows.json"
    meta_path = destination / "meta.json"
    _atomic_write(rows_path, payload)
    _atomic_write_json(meta_path, meta)
    return {
        "rows_path": rows_path,
        "meta_path": meta_path,
        "meta": meta,
        "sha256": digest,
    }


def _cache_directories(benchmark: Benchmark, cache_root: Path) -> list[Path]:
    base = _benchmark_cache(benchmark, cache_root)
    try:
        entries = [path for path in base.iterdir()
                   if path.is_dir()
                   and (path / "rows.json").is_file()
                   and (path / "meta.json").is_file()]
    except OSError:
        return []
    return entries


def _latest_cache_directory(benchmark: Benchmark,
                            cache_root: Path) -> Path | None:
    entries = _cache_directories(benchmark, cache_root)

    def sort_key(path: Path) -> tuple[str, float]:
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            stamp = str(meta.get("fetched_at") or "")
        except (OSError, json.JSONDecodeError):
            stamp = ""
        try:
            mtime = (path / "meta.json").stat().st_mtime
        except OSError:
            mtime = 0.0
        return stamp, mtime

    return max(entries, key=sort_key) if entries else None


def load_cached_rows(benchmark: str | Benchmark, *,
                     root: str | Path | None = None,
                     cache_root: str | Path | None = None,
                     cache_dir: str | Path | None = None,
                     dataset_sha: str | None = None
                     ) -> dict[str, Any]:
    """Load and validate cached rows. Never touches the network."""
    bench = get_benchmark(benchmark)
    base = (_cache_root(root, cache_dir) if cache_root is None
            else Path(cache_root).expanduser().resolve())
    if dataset_sha is not None:
        destination = _benchmark_cache(bench, base) / _safe_segment(
            dataset_sha, what="dataset sha")
    else:
        destination = _latest_cache_directory(bench, base)
        if destination is None:
            raise BenchmarkError(
                "cache_missing",
                f"no cached rows for {bench.id} under "
                f"{_benchmark_cache(bench, base)}; fetch or import first",
                benchmark=bench.id)
    rows_path = destination / "rows.json"
    meta_path = destination / "meta.json"
    try:
        meta_bytes = meta_path.read_bytes()
        rows_bytes = rows_path.read_bytes()
    except OSError as exc:
        raise BenchmarkError(
            "cache_missing", f"incomplete cache at {destination}: {exc}",
            benchmark=bench.id, details={"path": str(destination)}) from exc
    try:
        meta = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(
            "invalid_meta", f"invalid meta.json at {meta_path}: {exc}",
            benchmark=bench.id) from exc
    if not isinstance(meta, dict):
        raise BenchmarkError(
            "invalid_meta", f"meta.json at {meta_path} is not an object",
            benchmark=bench.id)
    if meta.get("benchmark") != bench.id or meta.get("dataset") != bench.dataset:
        raise BenchmarkError(
            "invalid_meta",
            f"cache at {destination} was written for another benchmark/dataset",
            benchmark=bench.id,
            details={"benchmark": meta.get("benchmark"),
                     "dataset": meta.get("dataset")})
    if str(meta.get("dataset_sha")) != destination.name:
        raise BenchmarkError(
            "invalid_meta",
            f"cache directory {destination.name!r} does not match "
            f"meta.json dataset_sha {meta.get('dataset_sha')!r}",
            benchmark=bench.id)
    if (meta.get("config") != bench.config
            or meta.get("split") != bench.split):
        raise BenchmarkError(
            "invalid_meta",
            f"cache at {destination} has config/split "
            f"{meta.get('config')!r}/{meta.get('split')!r}",
            benchmark=bench.id)
    digest = hashlib.sha256(rows_bytes).hexdigest()
    if meta.get("sha256") != digest:
        raise BenchmarkError(
            "invalid_meta",
            f"rows.json digest does not match meta.json at {destination}",
            benchmark=bench.id,
            details={"expected": meta.get("sha256"), "actual": digest})
    try:
        document = json.loads(rows_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(
            "bad_payload", f"invalid rows.json at {rows_path}: {exc}",
            benchmark=bench.id) from exc
    if not isinstance(document, dict):
        raise BenchmarkError(
            "bad_payload", f"rows.json at {rows_path} is not an object",
            benchmark=bench.id)
    if document.get("dataset_sha") not in (None, meta.get("dataset_sha")):
        raise BenchmarkError(
            "invalid_meta", "rows.json and meta.json disagree on dataset_sha",
            benchmark=bench.id)
    if document.get("dataset") not in (None, bench.dataset):
        raise BenchmarkError(
            "invalid_meta", "rows.json was written for another dataset",
            benchmark=bench.id)
    rows = _normalise_rows(document.get("rows"), str(rows_path))
    features = _normalise_features(document.get("features"), str(rows_path))
    if int(meta.get("row_count") or 0) != len(rows):
        raise BenchmarkError(
            "invalid_meta",
            f"meta.json row_count={meta.get('row_count')} but rows.json has "
            f"{len(rows)} rows", benchmark=bench.id)
    if not rows:
        raise BenchmarkError(
            "no_rows", f"cache at {destination} has no rows",
            benchmark=bench.id)
    return {
        "benchmark": bench,
        "dataset_sha": meta.get("dataset_sha") or destination.name,
        "rows": rows,
        "features": features,
        "meta": meta,
        "document": document,
        "rows_path": rows_path,
        "meta_path": meta_path,
        "cache_path": destination,
    }


# --------------------------------------------------------------------------- #
# Fetch and import
# --------------------------------------------------------------------------- #
def fetch_rows(benchmark: str | Benchmark, *,
               limit: int = DEFAULT_LIMIT, offset: int = 0,
               allow_network: bool = False,
               timeout: float = DEFAULT_TIMEOUT_S,
               root: str | Path | None = None,
               cache_root: str | Path | None = None,
               cache_dir: str | Path | None = None,
               opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Fetch bounded rows from the HF datasets server and cache them."""
    bench = get_benchmark(benchmark)
    limit = _validate_limit(limit, default=DEFAULT_LIMIT) or DEFAULT_LIMIT
    offset = _validate_offset(offset)
    if not allow_network:
        raise BenchmarkError(
            "network_not_allowed",
            "dataset fetch needs explicit allow_network=True (--allow-network)",
            benchmark=bench.id,
            details={"dataset": bench.dataset, "config": bench.config,
                     "split": bench.split})
    timeout = _validate_timeout(timeout)
    base = (_cache_root(root, cache_dir) if cache_root is None
            else Path(cache_root).expanduser().resolve())
    metadata = fetch_dataset_metadata(
        bench, allow_network=True, timeout=timeout, opener=opener)
    dataset_sha = metadata["dataset_sha"]
    rows: list[dict[str, Any]] = []
    features: list[Any] = []
    first_url = bench.rows_url(offset=offset, length=min(MAX_PAGE_ROWS, limit))
    cursor = offset
    remaining = limit
    total_bytes = 0
    while remaining > 0:
        page_length = min(MAX_PAGE_ROWS, remaining)
        url = bench.rows_url(offset=cursor, length=page_length)
        body, _final = _download(
            url, allowed_hosts=DATASET_HOSTS, timeout=timeout,
            max_bytes=MAX_ROWS_BYTES, opener=opener, benchmark=bench.id)
        total_bytes += len(body)
        if total_bytes > MAX_TOTAL_ROWS_BYTES:
            raise BenchmarkError(
                "too_large",
                f"rows download exceeds the {MAX_TOTAL_ROWS_BYTES}-byte "
                "total limit", benchmark=bench.id)
        payload = _decode_json(body, url, benchmark=bench.id)
        page_rows = payload.get("rows")
        if not isinstance(page_rows, list):
            raise BenchmarkError(
                "bad_payload", f"rows response from {url} has no rows list",
                benchmark=bench.id)
        if not features:
            features = _normalise_features(payload.get("features"), url)
        page = _normalise_rows(page_rows, url)
        if not page:
            break
        rows.extend(page)
        if len(rows) >= limit:
            rows = rows[:limit]
            break
        cursor += len(page)
        remaining = limit - len(rows)
    if not rows:
        raise BenchmarkError(
            "no_rows", "the dataset returned no rows for this request",
            benchmark=bench.id,
            details={"offset": offset, "limit": limit})
    fetched_at = _utc_now()
    written = _write_cache(
        benchmark=bench, cache_root=base, dataset_sha=dataset_sha,
        rows=rows, features=features, source_url=first_url,
        metadata_url=bench.metadata_url, fetched_at=fetched_at,
        offset=offset, limit=limit, origin="network",
        license_text=metadata["license"],
        license_status=metadata["license_status"],
        meta_details={
            "last_modified": metadata.get("last_modified"),
            "tags": metadata.get("tags") or [],
        })
    return {
        "schema": FETCH_SCHEMA,
        "status": "ok",
        "benchmark": bench.id,
        "dataset": bench.dataset,
        "dataset_sha": dataset_sha,
        "config": bench.config,
        "split": bench.split,
        "offset": offset,
        "requested_limit": limit,
        "row_count": len(rows),
        "license": metadata["license"],
        "license_status": metadata["license_status"],
        "rows_path": str(written["rows_path"]),
        "meta_path": str(written["meta_path"]),
        "sha256": written["sha256"],
    }


def import_rows(source: str | Path, benchmark: str | Benchmark, *,
                allow_network: bool = False,
                root: str | Path | None = None,
                cache_root: str | Path | None = None,
                cache_dir: str | Path | None = None,
                dataset_sha: str | None = None,
                limit: int | None = None,
                offset: int = 0) -> dict[str, Any]:
    """Import a local rows JSON (HF response or cache document) offline."""
    bench = get_benchmark(benchmark)
    if allow_network:
        # Accepted for CLI parity, but import never contacts the network.
        pass
    path = Path(source).expanduser().resolve()
    document, rows, features = _read_rows_document(path)
    offset = _validate_offset(offset)
    if limit is not None:
        limit = _validate_limit(limit, default=None)
    rows = rows[offset:offset + limit if limit is not None else None]
    if document.get("dataset") not in (None, bench.dataset):
        raise BenchmarkError(
            "invalid_dataset",
            f"{path} contains dataset {document.get('dataset')!r}, expected "
            f"{bench.dataset!r}", benchmark=bench.id)
    if document.get("config") not in (None, bench.config):
        raise BenchmarkError(
            "invalid_config",
            f"{path} contains config {document.get('config')!r}, expected "
            f"{bench.config!r}", benchmark=bench.id)
    if document.get("split") not in (None, bench.split):
        raise BenchmarkError(
            "invalid_split",
            f"{path} contains split {document.get('split')!r}, expected "
            f"{bench.split!r}", benchmark=bench.id)
    if not rows:
        raise BenchmarkError(
            "no_rows", f"{path} contains no rows in the requested range",
            benchmark=bench.id, details={"offset": offset, "limit": limit})
    try:
        source_bytes = path.read_bytes()
        source_digest = hashlib.sha256(source_bytes).hexdigest()
    except OSError as exc:
        raise BenchmarkError(
            "read_failed", f"cannot read {path}: {exc}",
            benchmark=bench.id) from exc
    digest_hint = (dataset_sha or document.get("dataset_sha")
                   or document.get("sha"))
    if isinstance(digest_hint, str) and digest_hint.strip():
        dataset_sha_value = _safe_segment(digest_hint, what="dataset sha")
    else:
        dataset_sha_value = f"local-{source_digest[:16]}"
    fetched_at = str(document.get("fetched_at") or _utc_now())
    base = (_cache_root(root, cache_dir) if cache_root is None
            else Path(cache_root).expanduser().resolve())
    license_text = str(document.get("license") or bench.license)
    license_status = str(
        document.get("license_status") or bench.license_status)
    written = _write_cache(
        benchmark=bench, cache_root=base, dataset_sha=dataset_sha_value,
        rows=rows, features=features,
        source_url=str(document.get("source_url") or path.as_uri()),
        metadata_url=bench.metadata_url, fetched_at=fetched_at,
        offset=int(document.get("offset") or 0),
        limit=int(document.get("limit") or len(rows)),
        origin="import", license_text=license_text,
        license_status=license_status, imported_from=str(path),
        import_sha256=source_digest)
    return {
        "schema": FETCH_SCHEMA,
        "status": "ok",
        "origin": "import",
        "benchmark": bench.id,
        "dataset": bench.dataset,
        "dataset_sha": dataset_sha_value,
        "config": bench.config,
        "split": bench.split,
        "row_count": len(rows),
        "imported_from": str(path),
        "license": license_text,
        "license_status": license_status,
        "rows_path": str(written["rows_path"]),
        "meta_path": str(written["meta_path"]),
        "sha256": written["sha256"],
    }


# --------------------------------------------------------------------------- #
# Gold patch parsing and task conversion
# --------------------------------------------------------------------------- #
def _safe_repo_path(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    path = raw.strip().strip('"')
    if not path or any(ord(char) < 32 for char in path):
        return None
    if path.startswith(("./",)):
        while path.startswith("./"):
            path = path[2:]
    if path.startswith("/") or "\\" in path:
        return None
    if path in {".", ".."}:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return None
    if any(part in {"", "."} for part in path.split("/")):
        return None
    return pure.as_posix()


def _diff_git_sides(rest: str) -> list[str]:
    try:
        parts = shlex.split(rest)
    except ValueError:
        parts = rest.split()
    if (len(parts) == 2 and parts[0].startswith("a/")
            and parts[1].startswith("b/")):
        return parts
    marker = rest.find(" b/")
    if marker > 0:
        return [rest[:marker].strip(), rest[marker + 1:].strip()]
    return parts


def _path_from_diff_side(value: str, prefix: str) -> str | None:
    text = value.strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = [text[1:-1]]
        text = tokens[0] if tokens else text[1:-1]
    if text.startswith(prefix):
        text = text[len(prefix):]
    return _safe_repo_path(text)


def parse_patch_files(patch: Any) -> list[str]:
    """Parse changed repository paths from a unified gold diff.

    Primary source is ``diff --git a/x b/x``; file headers ``--- a/x`` and
    ``+++ b/x`` are the fallback. ``/dev/null`` sides are skipped. Deletions
    use the old path (the file exists at the base commit); additions use the
    new path. Results are deduplicated and sorted.
    """
    if not isinstance(patch, str) or not patch.strip():
        return []
    found: set[str] = set()
    pending_old: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            pending_old = None
            rest = line[len("diff --git "):].strip()
            sides = _diff_git_sides(rest)
            if len(sides) >= 2:
                old = _path_from_diff_side(sides[0], "a/")
                new = _path_from_diff_side(sides[1], "b/")
            else:
                old = new = None
            chosen = new or old
            if chosen:
                found.add(chosen)
            continue
        if line.startswith("--- "):
            value = line[4:].strip().split("\t", 1)[0].strip()
            pending_old = "" if value == "/dev/null" else (
                _path_from_diff_side(value, "a/") or "")
            continue
        if line.startswith("+++ ") and pending_old is not None:
            value = line[4:].strip().split("\t", 1)[0].strip()
            new = None if value == "/dev/null" else (
                _path_from_diff_side(value, "b/"))
            if new:
                found.add(new)
            elif pending_old:
                found.add(pending_old)
            pending_old = None
            continue
        if line.startswith("@@"):
            pending_old = None
    return sorted(found)


def sanitize_task_id(value: Any) -> str | None:
    """A retrieval-safe id derived from ``instance_id``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"[^A-Za-z0-9._:-]+", "-", value.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-._:")
    return text or None


def convert_rows(rows: Sequence[Mapping[str, Any]], *,
                 benchmark: str | Benchmark,
                 dataset_sha: str = "",
                 offset: int = 0,
                 limit: int | None = None,
                 text_limit: int = TASK_TEXT_LIMIT) -> dict[str, Any]:
    """Convert SWE-bench rows to retrieval tasks plus a sources sidecar."""
    bench = get_benchmark(benchmark)
    if text_limit <= 0:
        raise BenchmarkError("invalid_argument", "text_limit must be positive")
    offset = _validate_offset(offset)
    if limit is not None:
        limit = _validate_limit(limit, default=None)
    selected = list(rows)[offset:offset + limit if limit is not None else None]
    tasks: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    omissions: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    truncated = 0

    def omit(instance_id: Any, reason: str) -> None:
        omissions.append({
            "instance_id": instance_id if isinstance(instance_id, str) else None,
            "reason": reason,
        })
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for raw in selected:
        row: dict[str, Any] = {}
        if isinstance(raw, Mapping):
            wrapped = raw.get("row")
            if (isinstance(wrapped, Mapping)
                    and "instance_id" not in raw):
                row = dict(wrapped)
            else:
                row = dict(raw)
        instance_id = row.get("instance_id")
        problem = row.get("problem_statement")
        if not isinstance(problem, str) or not problem.strip():
            omit(instance_id, "missing_problem_statement")
            continue
        files = parse_patch_files(row.get("patch"))
        if not files:
            omit(instance_id, "no_parseable_files")
            continue
        task_id = sanitize_task_id(instance_id)
        if task_id is None:
            omit(instance_id, "missing_instance_id")
            continue
        repo = row.get("repo")
        base_commit = row.get("base_commit")
        if not isinstance(repo, str) or not _REPO_SLUG.fullmatch(repo.strip()):
            omit(instance_id, "missing_repo")
            continue
        if (not isinstance(base_commit, str) or not base_commit.strip()
                or not _SAFE_SEGMENT.fullmatch(base_commit.strip())):
            omit(instance_id, "missing_base_commit")
            continue
        if task_id in sources:
            omit(instance_id, "duplicate_task_id")
            continue
        text = problem.strip()
        if len(text) > text_limit:
            text = text[:text_limit]
            truncated += 1
        tasks.append({
            "id": task_id,
            "task": text,
            "expected_files": files,
        })
        sources[task_id] = {
            "repo": repo.strip(),
            "base_commit": base_commit.strip(),
            "instance_id": instance_id.strip(),
        }
    document = {
        "version": 1,
        "benchmark": bench.id,
        "dataset": bench.dataset,
        "dataset_sha": dataset_sha,
        "config": bench.config,
        "split": bench.split,
        "task_count": len(tasks),
        "text_limit": int(text_limit),
        "tasks": tasks,
    }
    return {
        "schema": CONVERT_SCHEMA,
        "benchmark": bench.id,
        "dataset": bench.dataset,
        "dataset_sha": dataset_sha,
        "config": bench.config,
        "split": bench.split,
        "input_rows": len(selected),
        "task_count": len(tasks),
        "tasks": tasks,
        "omission_count": len(omissions),
        "omissions": omissions,
        "omissions_by_reason": {
            key: reason_counts[key] for key in sorted(reason_counts)},
        "truncated_tasks": truncated,
        "document": document,
        "sources": sources,
    }


def write_conversion(conversion: Mapping[str, Any], tasks_path: str | Path,
                     sources_path: str | Path | None = None) -> dict[str, str]:
    tasks_target = Path(tasks_path).expanduser().resolve()
    sources_target = (Path(sources_path).expanduser().resolve()
                      if sources_path is not None
                      else tasks_target.with_name("sources.json"))
    _atomic_write_json(tasks_target, conversion["document"])
    _atomic_write_json(sources_target, conversion["sources"])
    return {"tasks_path": str(tasks_target),
            "sources_path": str(sources_target)}


def convert_cached(benchmark: str | Benchmark, *,
                   root: str | Path | None = None,
                   cache_root: str | Path | None = None,
                   cache_dir: str | Path | None = None,
                   dataset_sha: str | None = None,
                   limit: int | None = None,
                   offset: int = 0,
                   tasks_path: str | Path | None = None,
                   sources_path: str | Path | None = None,
                   text_limit: int = TASK_TEXT_LIMIT) -> dict[str, Any]:
    """Load cached rows and write retrieval tasks plus a sources sidecar."""
    bench = get_benchmark(benchmark)
    loaded = load_cached_rows(
        bench, root=root, cache_root=cache_root, cache_dir=cache_dir,
        dataset_sha=dataset_sha)
    conversion = convert_rows(
        loaded["rows"], benchmark=bench,
        dataset_sha=loaded["dataset_sha"], offset=offset, limit=limit,
        text_limit=text_limit)
    default_tasks = loaded["cache_path"] / "tasks.json"
    chosen_tasks = Path(tasks_path) if tasks_path is not None else default_tasks
    chosen_sources = (Path(sources_path) if sources_path is not None
                      else chosen_tasks.with_name("sources.json"))
    written = write_conversion(conversion, chosen_tasks, chosen_sources)
    meta = loaded["meta"]
    return {
        **conversion,
        "status": "ok",
        "tasks": conversion["document"]["tasks"],
        "rows_path": str(loaded["rows_path"]),
        "meta_path": str(loaded["meta_path"]),
        "tasks_path": written["tasks_path"],
        "sources_path": written["sources_path"],
        "provenance": {
            "benchmark": bench.id,
            "dataset": bench.dataset,
            "dataset_sha": loaded["dataset_sha"],
            "config": bench.config,
            "split": bench.split,
            "fetched_at": meta.get("fetched_at"),
            "origin": meta.get("origin"),
            "license": meta.get("license"),
            "license_status": meta.get("license_status"),
            "rows_sha256": meta.get("sha256"),
        },
    }


def render_conversion_markdown(conversion: Mapping[str, Any]) -> str:
    provenance = conversion.get("provenance") or {}
    lines = [
        f"# {conversion.get('benchmark')} task conversion",
        "",
        f"- Dataset: `{conversion.get('dataset')}` "
        f"({conversion.get('config')}/{conversion.get('split')})",
        f"- Dataset revision: `{conversion.get('dataset_sha')}`",
        f"- Rows read: {conversion.get('input_rows')}",
        f"- Retrieval tasks: {conversion.get('task_count')}",
        f"- Omitted rows: {conversion.get('omission_count')}",
        f"- Tasks truncated to {conversion.get('text_limit', TASK_TEXT_LIMIT)} "
        f"chars: {conversion.get('truncated_tasks')}",
        f"- Tasks file: `{conversion.get('tasks_path')}`",
        f"- Sources file: `{conversion.get('sources_path')}`",
        f"- Licence: {provenance.get('license')} "
        f"({provenance.get('license_status')})",
        "",
        "## Omissions",
        "",
    ]
    reasons = conversion.get("omissions_by_reason") or {}
    if reasons:
        lines.append("| Reason | Rows |")
        lines.append("|---|---:|")
        for reason, count in reasons.items():
            lines.append(f"| `{reason}` | {count} |")
    else:
        lines.append("None.")
    lines.extend([
        "",
        "Expected files are the changed files in the gold code patch, not the "
        "test patch. This is file-localisation ground truth only.",
        "",
    ])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Archive extraction and repository checkout
# --------------------------------------------------------------------------- #
def _remove_tree(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError as exc:
        raise BenchmarkError(
            "cache_cleanup_failed", f"cannot remove {path}: {exc}",
            details={"path": str(path)}) from exc


def extract_archive(archive: bytes, destination: str | Path, *,
                    max_bytes: int = MAX_EXTRACTED_BYTES,
                    max_members: int = MAX_ARCHIVE_MEMBERS) -> dict[str, Any]:
    """Safely extract a GitHub-style tar.gz, stripping its top directory.

    Rejects absolute paths, ``..`` traversal, devices, FIFOs, multiple
    top-level entries, and archives beyond the byte/member limits. Symlinks and
    hard links are skipped rather than created: a benchmark checkout only needs
    regular source files, and skipping removes the link-escape surface. All
    members are validated before anything is written. No target code is
    imported or executed.
    """
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve()
    total_bytes = 0
    files = 0
    directories = 0
    skipped_links = 0
    top_levels: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > max_members:
                raise BenchmarkError(
                    "too_large",
                    f"archive has {len(members)} members; limit is {max_members}")
            plans: list[tuple[tarfile.TarInfo, list[str]]] = []
            for member in members:
                name = member.name
                if not name or "\x00" in name:
                    raise BenchmarkError(
                        "bad_archive", "archive member has an empty or invalid name")
                if name.startswith("/") or "\\" in name:
                    raise BenchmarkError(
                        "path_traversal",
                        f"archive member has an unsafe path: {name!r}")
                pure = PurePosixPath(name)
                parts = [part for part in pure.parts if part not in ("", ".")]
                if not parts or any(part == ".." for part in parts):
                    raise BenchmarkError(
                        "path_traversal",
                        f"archive member escapes the destination: {name!r}")
                if member.issym() or member.islnk():
                    # Skip links entirely. They are not needed for retrieval
                    # and creating them would widen the extraction surface.
                    skipped_links += 1
                    continue
                if not (member.isfile() or member.isdir()):
                    raise BenchmarkError(
                        "unsafe_member",
                        f"archive member is not a regular file or directory: "
                        f"{name!r}")
                top_levels.add(parts[0])
                if len(top_levels) > 1:
                    raise BenchmarkError(
                        "bad_archive",
                        "archive has more than one top-level directory")
                if len(parts) == 1:
                    if member.isfile():
                        raise BenchmarkError(
                            "bad_archive",
                            f"archive has a file at its root: {name!r}")
                    # The top directory is stripped; do not recreate it.
                    directories += 1
                    continue
                relative = target.joinpath(*parts[1:])
                try:
                    relative.resolve().relative_to(resolved_target)
                except ValueError as exc:
                    raise BenchmarkError(
                        "path_traversal",
                        f"archive member escapes the destination: {name!r}") from exc
                if member.isdir():
                    directories += 1
                else:
                    if member.size < 0:
                        raise BenchmarkError(
                            "bad_archive",
                            f"archive member has a negative size: {name!r}")
                    total_bytes += member.size
                    if total_bytes > max_bytes:
                        raise BenchmarkError(
                            "too_large",
                            f"archive expands beyond the {max_bytes}-byte limit")
                    files += 1
                plans.append((member, parts[1:]))

            # Only write after every member has passed validation.
            for member, relative_parts in plans:
                relative = target.joinpath(*relative_parts)
                if member.isdir():
                    relative.mkdir(parents=True, exist_ok=True)
                    continue
                relative.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise BenchmarkError(
                        "bad_archive", f"cannot read archive member: {member.name!r}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(relative, flags, 0o644)
                written = 0
                try:
                    with os.fdopen(fd, "wb") as stream:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > member.size:
                                raise BenchmarkError(
                                    "bad_archive",
                                    f"archive member is larger than declared: "
                                    f"{member.name!r}")
                            stream.write(chunk)
                finally:
                    source.close()
                if written != member.size:
                    raise BenchmarkError(
                        "bad_archive",
                        f"archive member is truncated: {member.name!r}")
    except BenchmarkError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BenchmarkError(
            "bad_archive", f"cannot extract tar archive: {exc}") from exc
    if not top_levels:
        raise BenchmarkError("bad_archive", "archive is empty")
    return {
        "top_level": next(iter(top_levels)),
        "files": files,
        "directories": directories,
        "skipped_links": skipped_links,
        "bytes": total_bytes,
    }


def checkout_path(benchmark: str | Benchmark, repo: str, base_commit: str, *,
                  root: str | Path | None = None,
                  cache_root: str | Path | None = None,
                  cache_dir: str | Path | None = None) -> Path:
    bench = get_benchmark(benchmark)
    repo = _validate_repo(repo, benchmark=bench.id)
    commit = _safe_segment(base_commit, what="base commit")
    owner, name = repo.split("/", 1)
    base = (_cache_root(root, cache_dir) if cache_root is None
            else Path(cache_root).expanduser().resolve())
    return _benchmark_cache(bench, base) / "repos" / owner / name / commit


def _checkout_complete(path: Path, repo: str, base_commit: str) -> bool:
    marker = path / CHECKOUT_MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("repo") == repo and data.get("base_commit") == base_commit


def ensure_checkout(benchmark: str | Benchmark, repo: str, base_commit: str, *,
                    allow_network: bool = False,
                    timeout: float = DEFAULT_TIMEOUT_S,
                    max_bytes: int = MAX_ARCHIVE_BYTES,
                    root: str | Path | None = None,
                    cache_root: str | Path | None = None,
                    cache_dir: str | Path | None = None,
                    opener: Callable[..., Any] | None = None) -> Path:
    """Return a cached checkout, downloading and safely extracting if needed."""
    bench = get_benchmark(benchmark)
    repo = _validate_repo(repo, benchmark=bench.id)
    commit = _safe_segment(base_commit, what="base commit")
    destination = checkout_path(
        bench, repo, commit, root=root, cache_root=cache_root,
        cache_dir=cache_dir)
    if _checkout_complete(destination, repo, commit):
        return destination
    if not allow_network:
        raise BenchmarkError(
            "network_not_allowed",
            "repository archive download needs explicit allow_network=True "
            "(--allow-network)", benchmark=bench.id,
            details={"repo": repo, "base_commit": commit})
    timeout = _validate_timeout(timeout)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise BenchmarkError("invalid_argument", "max_bytes must be positive")
    url = bench.archive_url(repo, commit)
    body, final = _download(
        url, allowed_hosts=GITHUB_HOSTS, timeout=timeout, max_bytes=max_bytes,
        opener=opener, benchmark=bench.id, accept="application/gzip, */*")
    digest = hashlib.sha256(body).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent))
    try:
        stats = extract_archive(body, temporary)
        marker = {
            "schema": 1,
            "benchmark": bench.id,
            "repo": repo,
            "base_commit": commit,
            "source_url": final,
            "fetched_at": _utc_now(),
            "archive_bytes": len(body),
            "archive_sha256": digest,
            "extracted": stats,
        }
        _atomic_write_json(temporary / CHECKOUT_MARKER, marker)
        if destination.exists() or destination.is_symlink():
            _remove_tree(destination)
        os.replace(temporary, destination)
    except BaseException:
        _remove_tree(temporary)
        raise
    return destination


# --------------------------------------------------------------------------- #
# Verification and aggregation
# --------------------------------------------------------------------------- #
def _load_sources(path: str | Path) -> dict[str, dict[str, Any]]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError(
            "read_failed", f"cannot read sources file {source}: {exc}",
            details={"path": str(source)}) from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError(
            "bad_payload", f"sources file {source} is not valid JSON: {exc}",
            details={"path": str(source)}) from exc
    if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
        data = data["tasks"]
    if not isinstance(data, dict):
        raise BenchmarkError(
            "bad_payload", f"sources file {source} must be an object",
            details={"path": str(source)})
    sources: dict[str, dict[str, Any]] = {}
    for task_id, value in data.items():
        if (not isinstance(task_id, str) or not isinstance(value, dict)
                or not isinstance(value.get("repo"), str)
                or not isinstance(value.get("base_commit"), str)):
            raise BenchmarkError(
                "bad_payload",
                f"sources file {source} has an invalid entry for {task_id!r}")
        sources[task_id] = {
            "repo": value["repo"],
            "base_commit": value["base_commit"],
            "instance_id": value.get("instance_id"),
        }
    return sources


def _task_document_tasks(path: str | Path) -> tuple[list[dict[str, Any]],
                                                   dict[str, Any]]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError(
            "read_failed", f"cannot read tasks file {source}: {exc}",
            details={"path": str(source)}) from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError(
            "bad_payload", f"tasks file {source} is not valid JSON: {exc}",
            details={"path": str(source)}) from exc
    if isinstance(data, dict):
        raw_tasks = data.get("tasks")
        meta = {key: value for key, value in data.items() if key != "tasks"}
    elif isinstance(data, list):
        raw_tasks = data
        meta = {}
    else:
        raise BenchmarkError(
            "bad_payload", f"tasks file {source} must be an object or list",
            details={"path": str(source)})
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise BenchmarkError(
            "no_tasks", f"tasks file {source} has no tasks",
            details={"path": str(source)})
    try:
        tasks = retrieval.load_tasks(source)
    except (OSError, ValueError) as exc:
        raise BenchmarkError(
            "bad_tasks", f"invalid tasks file {source}: {exc}",
            details={"path": str(source)}) from exc
    return tasks, meta


def _resolve_verify_task_files(
    bench: Benchmark, *, root: str | Path | None,
    cache_root: Path, dataset_sha: str | None,
    tasks_path: str | Path | None, sources_path: str | Path | None,
    text_limit: int) -> dict[str, Any]:
    loaded: dict[str, Any] | None = None
    if tasks_path is not None:
        task_file = Path(tasks_path).expanduser().resolve()
        source_file = (Path(sources_path).expanduser().resolve()
                       if sources_path is not None
                       else task_file.with_name("sources.json"))
        tasks, meta = _task_document_tasks(task_file)
        if meta.get("benchmark") not in (None, bench.id):
            raise BenchmarkError(
                "invalid_tasks",
                f"tasks file {task_file} belongs to benchmark "
                f"{meta.get('benchmark')!r}", benchmark=bench.id)
        if meta.get("dataset") not in (None, bench.dataset):
            raise BenchmarkError(
                "invalid_tasks",
                f"tasks file {task_file} belongs to dataset "
                f"{meta.get('dataset')!r}", benchmark=bench.id)
        sources = _load_sources(source_file)
        return {
            "loaded": loaded,
            "tasks": tasks,
            "sources": sources,
            "task_meta": meta,
            "tasks_path": task_file,
            "sources_path": source_file,
            "cache_dir": task_file.parent,
        }
    loaded = load_cached_rows(
        bench, root=root, cache_root=cache_root, dataset_sha=dataset_sha)
    cache_dir = loaded["cache_path"]
    task_file = cache_dir / "tasks.json"
    source_file = cache_dir / "sources.json"
    if not task_file.is_file() or not source_file.is_file():
        conversion = convert_rows(
            loaded["rows"], benchmark=bench,
            dataset_sha=loaded["dataset_sha"], text_limit=text_limit)
        written = write_conversion(conversion, task_file, source_file)
        task_file = Path(written["tasks_path"])
        source_file = Path(written["sources_path"])
    tasks, meta = _task_document_tasks(task_file)
    sources = _load_sources(source_file)
    return {
        "loaded": loaded,
        "tasks": tasks,
        "sources": sources,
        "task_meta": meta,
        "tasks_path": task_file,
        "sources_path": source_file,
        "cache_dir": cache_dir,
    }


def _weighted_aggregates(entries: Iterable[Mapping[str, Any]],
                         budgets: Sequence[int],
                         arms: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    entry_list = list(entries)
    for budget in budgets:
        for arm in arms:
            group = []
            for entry in entry_list:
                for row in entry.get("aggregates") or []:
                    if (row.get("budget_tokens") == budget
                            and row.get("arm") == arm
                            and int(row.get("tasks") or 0) > 0):
                        group.append(row)
            if not group:
                continue
            total = sum(int(row["tasks"]) for row in group)
            weighted = lambda key: round(
                sum(float(row.get(key) or 0.0) * int(row["tasks"])
                    for row in group) / total, 6)

            def mean(key: str, default: float = 0.0) -> float:
                values = [float(row.get(key, default)) for row in group]
                return round(sum(values) / len(group), 6)

            rows.append({
                "budget_tokens": budget,
                "arm": arm,
                "tasks": total,
                "mean_recall_at_budget": weighted("mean_recall_at_budget"),
                "mean_recall_content_at_budget": weighted(
                    "mean_recall_content_at_budget"),
                "mean_precision": weighted("mean_precision"),
                "mean_evidence_coverage": weighted("mean_evidence_coverage"),
                "mean_evidence_density": weighted("mean_evidence_density"),
                "mean_evidence_terms": weighted("mean_evidence_terms"),
                "mean_evidence_symbols": weighted("mean_evidence_symbols"),
                "mean_evidence_weight": weighted("mean_evidence_weight"),
                "mean_selected_files": weighted("mean_selected_files"),
                "mean_content_files": weighted("mean_content_files"),
                "mean_estimated_tokens": weighted("mean_estimated_tokens"),
                "mean_elapsed_seconds": weighted("mean_elapsed_seconds"),
                "recall_1_tasks": sum(
                    int(row.get("recall_1_tasks") or 0) for row in group),
                "budget_respected_runs": sum(
                    int(row.get("budget_respected_runs") or 0)
                    for row in group),
                "repos": len(group),
                "mean_repo_recall": mean("mean_recall_at_budget"),
                "mean_repo_recall_content": mean(
                    "mean_recall_content_at_budget"),
            })
    return rows


def _repo_entries(repo_entries: Sequence[Mapping[str, Any]],
                  budgets: Sequence[int],
                  arms: Sequence[str]) -> list[dict[str, Any]]:
    by_repo: dict[str, list[Mapping[str, Any]]] = {}
    for entry in repo_entries:
        by_repo.setdefault(str(entry["repo"]), []).append(entry)
    result: list[dict[str, Any]] = []
    for repo in sorted(by_repo):
        group = by_repo[repo]
        assessed = [entry for entry in group if entry.get("status") == "assessed"]
        result.append({
            "repo": repo,
            "checkouts": len(group),
            "task_count": sum(int(entry.get("task_count") or 0) for entry in group),
            "assessed_task_count": sum(
                int(entry.get("assessed_task_count") or 0) for entry in group),
            "not_assessed_task_count": sum(
                int(entry.get("not_assessed_task_count") or 0)
                for entry in group),
            "universe_misses": sorted({
                miss["path"] for entry in group
                for miss in entry.get("universe_misses") or []
                if isinstance(miss, dict) and miss.get("path")}),
            "aggregates": _weighted_aggregates(assessed, budgets, arms),
        })
    return result


def retrieval_evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Indirection so callers can patch this name or ``retrieval.evaluate``."""
    return retrieval.evaluate(*args, **kwargs)


def _not_assessed_entry(repo: str, base_commit: str,
                        task_ids: Sequence[str], reason: str,
                        detail: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "repo": repo,
        "base_commit": base_commit,
        "task_ids": list(task_ids),
        "task_count": len(task_ids),
        "reason": reason,
    }
    if detail:
        entry["detail"] = detail
    return entry


def verify(benchmark: str | Benchmark, *,
           root: str | Path | None = None,
           cache_root: str | Path | None = None,
           cache_dir: str | Path | None = None,
           allow_network: bool = False,
           timeout: float = DEFAULT_TIMEOUT_S,
           budgets: Iterable[int] | str = DEFAULT_BUDGETS,
           arms: Iterable[str] | str = DEFAULT_ARMS,
           limit: int | None = DEFAULT_LIMIT,
           offset: int = 0,
           dataset_sha: str | None = None,
           tasks_path: str | Path | None = None,
           sources_path: str | Path | None = None,
           max_archive_bytes: int = MAX_ARCHIVE_BYTES,
           dry_run: bool = False,
           opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Materialise checkouts and run retrieval evaluation against gold files.

    The network gate is checked before any archive is requested. Existing
    checkouts are reused without network access.
    """
    bench = get_benchmark(benchmark)
    if not dry_run:
        timeout = _validate_timeout(timeout)
    budgets_list = _parse_budgets(budgets)
    arms_list = _parse_arms(arms)
    offset = _validate_offset(offset)
    if limit is not None:
        limit = _validate_limit(limit, default=DEFAULT_LIMIT)
    base = (_cache_root(root, cache_dir) if cache_root is None
            else Path(cache_root).expanduser().resolve())
    resolved = _resolve_verify_task_files(
        bench, root=root, cache_root=base, dataset_sha=dataset_sha,
        tasks_path=tasks_path, sources_path=sources_path,
        text_limit=TASK_TEXT_LIMIT)
    loaded = resolved["loaded"]
    all_tasks = resolved["tasks"]
    selected = all_tasks[offset:offset + limit if limit is not None else None]
    if not selected:
        raise BenchmarkError(
            "no_tasks", "no tasks selected for verification",
            benchmark=bench.id)
    sources = resolved["sources"]
    cache_dir_path: Path = resolved["cache_dir"]
    task_meta = resolved["task_meta"]
    provenance = {
        "benchmark": bench.id,
        "dataset": bench.dataset,
        "config": bench.config,
        "split": bench.split,
        "dataset_sha": (loaded["dataset_sha"] if loaded is not None
                        else task_meta.get("dataset_sha")),
        "license": (loaded["meta"].get("license") if loaded is not None
                    else bench.license),
        "license_status": (loaded["meta"].get("license_status")
                           if loaded is not None else bench.license_status),
        "fetched_at": (loaded["meta"].get("fetched_at")
                       if loaded is not None else None),
        "origin": (loaded["meta"].get("origin")
                   if loaded is not None else None),
        "rows_sha256": (loaded["meta"].get("sha256")
                        if loaded is not None else None),
        "rows_path": (str(loaded["rows_path"]) if loaded is not None else None),
        "meta_path": (str(loaded["meta_path"]) if loaded is not None else None),
        "tasks_path": str(resolved["tasks_path"]),
        "sources_path": str(resolved["sources_path"]),
    }

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unassigned: list[dict[str, Any]] = []
    for task in selected:
        source = sources.get(task["id"])
        if source is None:
            unassigned.append(task)
            continue
        key = (source["repo"], source["base_commit"])
        grouped.setdefault(key, []).append(task)

    missing_checkouts: list[dict[str, Any]] = []
    for (repo, commit), group in sorted(grouped.items()):
        path = checkout_path(
            bench, repo, commit, root=root, cache_root=base)
        if not _checkout_complete(path, repo, commit):
            missing_checkouts.append({
                "repo": repo,
                "base_commit": commit,
                "task_ids": [task["id"] for task in group],
            })
    if missing_checkouts and not allow_network and not dry_run:
        raise BenchmarkError(
            "network_not_allowed",
            "verify needs to download repository archives; pass "
            "--allow-network or pre-populate the checkout cache",
            benchmark=bench.id,
            details={"repositories": missing_checkouts})

    repo_entries: list[dict[str, Any]] = []
    not_assessed: list[dict[str, Any]] = []
    assessed_ids: set[str] = set()
    for (repo, commit), group in sorted(grouped.items()):
        path = checkout_path(
            bench, repo, commit, root=root, cache_root=base)
        entry: dict[str, Any] = {
            "repo": repo,
            "base_commit": commit,
            "task_count": len(group),
            "task_ids": [task["id"] for task in group],
            "checkout": str(path),
            "status": "planned",
            "assessed_task_count": 0,
            "not_assessed_task_count": len(group),
            "missing_expected_files": [],
            "universe_misses": [],
            "aggregates": [],
        }
        if dry_run:
            entry["action"] = ("cached" if _checkout_complete(path, repo, commit)
                               else "would_download")
            repo_entries.append(entry)
            continue
        try:
            checkout = ensure_checkout(
                bench, repo, commit, allow_network=allow_network,
                timeout=timeout, max_bytes=max_archive_bytes, root=root,
                cache_root=base, opener=opener)
        except Exception as exc:
            reason = (exc.reason if isinstance(exc, BenchmarkError)
                      else "checkout_failed")
            entry["status"] = "not_assessed"
            entry["reason"] = reason
            entry["detail"] = str(exc)
            repo_entries.append(entry)
            not_assessed.append(_not_assessed_entry(
                repo, commit, entry["task_ids"], reason, str(exc)))
            continue

        filtered: list[dict[str, Any]] = []
        missing_files: list[dict[str, str]] = []
        for task in group:
            existing = []
            for rel in task["expected_files"]:
                if (checkout / rel).is_file():
                    existing.append(rel)
                else:
                    missing_files.append({
                        "task_id": task["id"],
                        "path": rel,
                    })
            if existing:
                filtered.append({
                    "id": task["id"],
                    "task": task["task"],
                    "expected_files": existing,
                })
            else:
                not_assessed.append(_not_assessed_entry(
                    repo, commit, [task["id"]],
                    "no_expected_files_at_base",
                    "gold code files do not exist in the checkout"))
        entry["missing_expected_files"] = missing_files
        if not filtered:
            entry["status"] = "not_assessed"
            entry["reason"] = "no_expected_files_at_base"
            repo_entries.append(entry)
            continue

        owner, name = repo.split("/", 1)
        run_dir = (cache_dir_path / "runs" / owner / name / commit)
        run_tasks = run_dir / "tasks.json"
        run_doc = {
            "version": 1,
            "benchmark": bench.id,
            "dataset": bench.dataset,
            "dataset_sha": provenance["dataset_sha"],
            "config": bench.config,
            "split": bench.split,
            "task_count": len(filtered),
            "tasks": filtered,
        }
        _atomic_write_json(run_tasks, run_doc)
        entry["run_tasks_path"] = str(run_tasks)
        try:
            report = retrieval_evaluate(
                root=checkout, tasks_path=run_tasks, budgets=budgets_list,
                arms=arms_list, cache_path=run_dir / "eval_tags.json")
        except Exception as exc:
            entry["status"] = "not_assessed"
            entry["reason"] = "evaluation_failed"
            entry["detail"] = str(exc)
            repo_entries.append(entry)
            not_assessed.append(_not_assessed_entry(
                repo, commit, [task["id"] for task in filtered],
                "evaluation_failed", str(exc)))
            continue
        entry["status"] = "assessed"
        entry["assessed_task_count"] = len(filtered)
        entry["not_assessed_task_count"] = len(group) - len(filtered)
        entry["universe_misses"] = [
            {"repo": repo, "base_commit": commit, "path": rel}
            for rel in report.get(
                "expected_files_outside_source_universe") or []]
        entry["aggregates"] = _ordered_aggregates(
            report.get("aggregates") or [], budgets_list, arms_list)
        entry["source_files"] = int(report.get("source_files") or 0)
        entry["fingerprint"] = report.get("deterministic_fingerprint")
        entry["task_set_summary"] = report.get("task_set_summary")
        assessed_ids.update(task["id"] for task in filtered)
        repo_entries.append(entry)

    for task in unassigned:
        not_assessed.append(_not_assessed_entry(
            "", "", [task["id"]], "missing_source_metadata",
            "sources.json has no repo/base_commit for this task"))

    assessed_entries = [entry for entry in repo_entries
                        if entry.get("status") == "assessed"]
    selected_ids = {task["id"] for task in selected}
    not_assessed_ids = selected_ids - assessed_ids
    overall = {
        "task_count": len(selected),
        "assessed_task_count": len(assessed_ids),
        "not_assessed_task_count": len(not_assessed_ids),
        "not_assessed_task_ids": sorted(not_assessed_ids),
        "repo_count": len({entry["repo"] for entry in repo_entries}),
        "checkout_count": len(repo_entries),
        "assessed_checkout_count": len(assessed_entries),
        "universe_misses": sorted(
            {tuple(sorted(miss.items())) for entry in repo_entries
             for miss in entry.get("universe_misses") or []}),
        "aggregates": _weighted_aggregates(
            assessed_entries, budgets_list, arms_list),
    }
    overall["universe_misses"] = [
        dict(items) for items in overall["universe_misses"]]
    report: dict[str, Any] = {
        "schema": VERIFY_SCHEMA,
        "status": "ok",
        "generated_at": _utc_now(),
        "benchmark": bench.to_dict(),
        "provenance": provenance,
        "allow_network": bool(allow_network),
        "dry_run": bool(dry_run),
        "budgets": list(budgets_list),
        "arms": list(arms_list),
        "limit": limit,
        "offset": offset,
        "task_count": len(selected),
        "repos": repo_entries,
        "by_repo": _repo_entries(repo_entries, budgets_list, arms_list),
        "not_assessed": not_assessed,
        "overall": overall,
        "limitations": [
            "File-localisation retrieval only: a hit does not mean a patch "
            "would be correct, complete, or pass the benchmark's tests.",
            "Expected files are the changed files in the gold code patch; "
            "test patches are never used as ground truth.",
            "No target repository code is executed and no model calls are made.",
            "Large repositories take longer to index; token estimates are "
            "the retrieval modules' own heuristics.",
            "Rows are pinned to the cached dataset revision sha; re-fetch to "
            "evaluate a newer dataset revision.",
            "A repository checkout or evaluation failure is recorded as not "
            "assessed, never as a zero score.",
        ],
    }
    report["markdown"] = render_verify_markdown(report)
    return report


def render_verify_markdown(report: Mapping[str, Any]) -> str:
    benchmark = report.get("benchmark") or {}
    provenance = report.get("provenance") or {}
    overall = report.get("overall") or {}
    lines: list[str] = [
        f"# {benchmark.get('title') or benchmark.get('id')} retrieval verification",
        "",
        f"- Dataset: `{benchmark.get('dataset')}` "
        f"({benchmark.get('config')}/{benchmark.get('split')})",
        f"- Dataset revision: `{provenance.get('dataset_sha')}`",
        f"- Rows source: {provenance.get('origin') or 'unknown'} "
        f"(fetched {provenance.get('fetched_at') or 'unknown'})",
        f"- Licence: {provenance.get('license')} "
        f"({provenance.get('license_status')})",
        f"- Tasks: {overall.get('task_count')} selected, "
        f"{overall.get('assessed_task_count')} assessed, "
        f"{overall.get('not_assessed_task_count')} not assessed",
        f"- Repositories/checkouts: {overall.get('repo_count')}/"
        f"{overall.get('checkout_count')}",
        "- Budgets: " + ", ".join(str(b) for b in report.get("budgets") or []),
        "- Arms: " + ", ".join(report.get("arms") or []),
        f"- Network allowed: {report.get('allow_network')}"
        + (" (dry run)" if report.get("dry_run") else ""),
        "",
        "## Overall metrics",
        "",
    ]
    aggregates = overall.get("aggregates") or []
    if aggregates:
        lines.append("| Budget | Arm | Tasks | Mean recall | "
                     "Mean content recall | Mean precision | "
                     "Mean evidence coverage | Mean evidence symbols | "
                     "Mean evidence/1k tokens |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|")
        for row in aggregates:
            lines.append(
                f"| {row['budget_tokens']} | {row['arm']} | {row['tasks']} | "
                f"{row['mean_recall_at_budget']:.3f} | "
                f"{row['mean_recall_content_at_budget']:.3f} | "
                f"{row['mean_precision']:.3f} | "
                f"{row.get('mean_evidence_coverage', 0.0):.3f} | "
                f"{row.get('mean_evidence_symbols', 0.0):.1f} | "
                f"{row.get('mean_evidence_density', 0.0):.2f} |")
    else:
        lines.append("No retrieval runs were assessed.")
    lines.extend(["", "## Per repository", ""])
    if report.get("by_repo"):
        lines.append("| Repo | Checkouts | Tasks | Assessed | "
                     "Universe misses |")
        lines.append("|---|---:|---:|---:|---|")
        for row in report["by_repo"]:
            misses = ", ".join(f"`{path}`" for path in row["universe_misses"])
            lines.append(
                f"| {row['repo']} | {row['checkouts']} | "
                f"{row['task_count']} | {row['assessed_task_count']} | "
                f"{misses or '-'} |")
    else:
        lines.append("No repository groups.")
    per_repo_rows = [
        (repo_row["repo"], aggregate)
        for repo_row in report.get("by_repo") or []
        for aggregate in repo_row.get("aggregates") or []
    ]
    lines.extend(["", "## Per-repository metrics", ""])
    if per_repo_rows:
        lines.append("| Repo | Budget | Arm | Tasks | Mean recall | "
                     "Mean content recall |")
        lines.append("|---|---:|---|---:|---:|---:|")
        for repo, aggregate in per_repo_rows:
            lines.append(
                f"| {repo} | {aggregate['budget_tokens']} | "
                f"{aggregate['arm']} | {aggregate['tasks']} | "
                f"{aggregate['mean_recall_at_budget']:.3f} | "
                f"{aggregate['mean_recall_content_at_budget']:.3f} |")
    else:
        lines.append("No per-repository runs were assessed.")
    lines.extend(["", "## Repository checkouts", ""])
    if report.get("repos"):
        lines.append("| Repo | Commit | Status | Tasks | Assessed | Detail |")
        lines.append("|---|---|---|---:|---:|---|")
        for entry in report["repos"]:
            detail = entry.get("reason") or entry.get("action") or ""
            lines.append(
                f"| {entry['repo']} | `{str(entry['base_commit'])[:12]}` | "
                f"{entry['status']} | {entry['task_count']} | "
                f"{entry.get('assessed_task_count', 0)} | {detail} |")
    else:
        lines.append("None.")
    if report.get("not_assessed"):
        lines.extend(["", "## Not assessed", "",
                      "| Repo | Commit | Tasks | Reason |",
                      "|---|---|---:|---|"])
        for entry in report["not_assessed"]:
            lines.append(
                f"| {entry['repo'] or '-'} | "
                f"`{str(entry['base_commit'])[:12] or '-'}` | "
                f"{entry['task_count']} | {entry['reason']} |")
    lines.extend([
        "",
        "## Reading these numbers",
        "",
        "- This measures whether gold-patch files are surfaced inside a token "
        "budget. It is not a SWE-bench pass rate and makes no claim that an "
        "agent can resolve an issue.",
        "- The expected files come from the gold code patch (`diff --git` "
        "paths), excluding `/dev/null` sides. Test files are not ground truth.",
        "- `baseline` is a sorted-path control; `grep` is a lexical control; "
        "`code_search` and `dev_repo_map` are Ozagia retrieval arms.",
        "- A missing checkout, missing expected file at the base commit, or "
        "evaluation error is recorded as not assessed rather than scored zero.",
        "- No target code is executed and no model calls are made.",
        "",
    ])
    return "\n".join(lines)


# Convenience aliases for callers that use the short operation names.
fetch = fetch_rows
import_dataset = import_rows
convert = convert_cached
verify_benchmark = verify
parse_gold_patch = parse_patch_files
extract_changed_files = parse_patch_files


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--benchmark", default="swe-bench-verified",
                        help="benchmark id (default: swe-bench-verified)")
    parser.add_argument("--root", default=None,
                        help="repository root used to locate the state dir")
    parser.add_argument("--cache-dir", default=None,
                        help="cache root (default: <state>/eval/hf)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="network timeout in seconds")
    parser.add_argument("--allow-network", action="store_true",
                        help="explicitly allow HTTPS dataset/archive downloads")
    parser.add_argument("--json", action="store_true",
                        help="print JSON instead of the default summary")


def _write_outputs(report: Mapping[str, Any], *, json_out: str | None,
                   md_out: str | None, markdown: str) -> None:
    if json_out:
        target = Path(json_out)
        _atomic_write_json(target, report)
    if md_out:
        target = Path(md_out)
        _atomic_write(target, (markdown.rstrip() + "\n").encode("utf-8"))


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ozagia-eval-benchmarks",
        description="Hugging Face benchmark ingestion and retrieval "
                    "verification (offline by default).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="list the benchmark registry")
    list_parser.add_argument("--json", action="store_true",
                             help="print JSON (the default)")

    fetch_parser = subparsers.add_parser(
        "fetch", help="fetch bounded dataset rows and cache them")
    _add_common(fetch_parser)
    fetch_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                              help=f"rows to fetch (default: {DEFAULT_LIMIT}, "
                                   f"max: {MAX_FETCH_ROWS})")
    fetch_parser.add_argument("--offset", type=int, default=0,
                              help="dataset row offset (default: 0)")

    import_parser = subparsers.add_parser(
        "import", help="import a local rows JSON file offline")
    _add_common(import_parser)
    import_parser.add_argument("path", help="local rows JSON file")
    import_parser.add_argument("--dataset-sha", default=None,
                               help="override the dataset revision label")
    import_parser.add_argument("--limit", type=int, default=None,
                               help="import at most N rows (default: all)")
    import_parser.add_argument("--offset", type=int, default=0,
                               help="skip N rows (default: 0)")

    convert_parser = subparsers.add_parser(
        "convert", help="convert cached rows to retrieval tasks")
    _add_common(convert_parser)
    convert_parser.add_argument("--limit", type=int, default=None,
                                help="convert at most N rows (default: all)")
    convert_parser.add_argument("--offset", type=int, default=0,
                                help="skip N rows (default: 0)")
    convert_parser.add_argument("--dataset-sha", default=None,
                                help="select a cached dataset revision")
    convert_parser.add_argument("--out-json", default=None,
                                help="tasks JSON output path")
    convert_parser.add_argument("--out-md", default=None,
                                help="conversion summary markdown output path")

    verify_parser = subparsers.add_parser(
        "verify", help="materialise checkouts and verify retrieval")
    _add_common(verify_parser)
    verify_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                               help=f"tasks to verify (default: {DEFAULT_LIMIT}, "
                                    f"max: {MAX_FETCH_ROWS})")
    verify_parser.add_argument("--offset", type=int, default=0,
                               help="skip N tasks (default: 0)")
    verify_parser.add_argument("--budgets", default=",".join(
        str(value) for value in DEFAULT_BUDGETS),
        help="comma-separated token budgets")
    verify_parser.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                               help="comma-separated retrieval arms")
    verify_parser.add_argument("--dataset-sha", default=None,
                               help="select a cached dataset revision")
    verify_parser.add_argument("--tasks", default=None,
                               help="explicit tasks JSON (default: cached)")
    verify_parser.add_argument("--sources", default=None,
                               help="explicit sources sidecar")
    verify_parser.add_argument("--dry-run", action="store_true",
                               help="plan checkouts without downloading or "
                                    "evaluating")
    verify_parser.add_argument("--out-json", default=None,
                               help="verification JSON output path")
    verify_parser.add_argument("--out-md", default=None,
                               help="verification markdown output path")

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            _print_json({
                "schema": SCHEMA,
                "benchmarks": list_benchmarks(),
            })
            return 0
        bench = get_benchmark(args.benchmark)
        if args.command == "fetch":
            result = fetch_rows(
                bench, limit=args.limit, offset=args.offset,
                allow_network=args.allow_network, timeout=args.timeout,
                root=args.root, cache_dir=args.cache_dir)
            _print_json(result)
            return 0
        if args.command == "import":
            result = import_rows(
                args.path, bench, allow_network=args.allow_network,
                root=args.root, cache_dir=args.cache_dir,
                dataset_sha=args.dataset_sha, limit=args.limit,
                offset=args.offset)
            _print_json(result)
            return 0
        if args.command == "convert":
            result = convert_cached(
                bench, root=args.root, cache_dir=args.cache_dir,
                dataset_sha=args.dataset_sha, limit=args.limit,
                offset=args.offset, tasks_path=args.out_json)
            markdown = render_conversion_markdown(result)
            if args.out_md:
                _atomic_write(
                    Path(args.out_md),
                    (markdown.rstrip() + "\n").encode("utf-8"))
            if args.json or not args.out_md:
                _print_json(result)
            else:
                print(f"ozagia-eval-benchmarks: tasks={result['task_count']} "
                      f"omitted={result['omission_count']} "
                      f"tasks_path={result['tasks_path']}")
            return 0
        if args.command == "verify":
            report = verify(
                bench, root=args.root, cache_dir=args.cache_dir,
                allow_network=args.allow_network, timeout=args.timeout,
                budgets=args.budgets, arms=args.arms, limit=args.limit,
                offset=args.offset, dataset_sha=args.dataset_sha,
                tasks_path=args.tasks, sources_path=args.sources,
                dry_run=args.dry_run)
            markdown = report["markdown"]
            if args.out_json or args.out_md:
                _write_outputs(
                    report, json_out=args.out_json, md_out=args.out_md,
                    markdown=markdown)
                if args.json:
                    _print_json(report)
                else:
                    print(
                        f"ozagia-eval-benchmarks: tasks={report['task_count']} "
                        f"assessed={report['overall']['assessed_task_count']} "
                        f"not_assessed="
                        f"{report['overall']['not_assessed_task_count']}")
            elif args.json:
                _print_json(report)
            else:
                print(markdown)
            return 0
    except BenchmarkError as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True),
              file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "status": "error",
            "reason": "operation_failed",
            "error": str(exc),
        }, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
