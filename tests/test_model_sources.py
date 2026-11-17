"""Cached model metadata sources: adapters, cache, merge and CLI.

Every network test injects a fake opener. Nothing here contacts a provider,
runs ``opencode`` or calls a model.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import time
from pathlib import Path

import pytest

from ozagia import models_cli
from ozagia.core import model_graph as G
from ozagia.core import model_sources as S
from ozagia.core import models as M

FIXTURES = Path(__file__).parent / "fixtures" / "model_sources"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_ROOT", "OZAGIA_CONFIG", "OZAGIA_STATE_DIR",
                 "OZAGIA_USER_CONFIG", "OZAGIA_MODELS_DEFAULT",
                 "OZAGIA_MODELS_LOCAL_ONLY"):
        monkeypatch.delenv(name, raising=False)
    return home


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _parse(source_id: str, name: str) -> S.ParseResult:
    return S.parse_payload(S.get_source(source_id), _fixture(name))


def _runner(stdout: str = "", *, returncode: int = 0):
    def run(argv, timeout):
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "")

    return run


def _ids(*models: str) -> str:
    return "\n".join(models) + "\n"


class _FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "application/json",
                 status: int = 200, url: str | None = None,
                 declared: int | None = None) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self._url = url or "https://models.dev/catalog.json?type=all"
        self.headers = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(
            len(body) if declared is None else declared)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, body: bytes = b"", *, content_type: str = "application/json",
                 error: BaseException | None = None,
                 responses: list[object] | None = None) -> None:
        self.body = body
        self.content_type = content_type
        self.error = error
        self.responses = list(responses or [])
        self.calls: list[tuple[str, float]] = []

    def __call__(self, request, timeout=0):
        self.calls.append((request.full_url, timeout))
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if self.error is not None:
            raise self.error
        return _FakeResponse(
            self.body, content_type=self.content_type, url=request.full_url)


# --------------------------------------------------------------------------- #
# registry and adapters
# --------------------------------------------------------------------------- #
def test_source_registry_metadata_and_order() -> None:
    assert [source.id for source in S.SOURCES] == [
        "models-dev", "litellm", "openrouter", "epoch", "huggingface"]
    assert S.DEFAULT_ORDER == ("models-dev", "litellm", "openrouter", "epoch")
    models_dev = S.get_source("models-dev")
    assert models_dev.url == "https://models.dev/catalog.json?type=all"
    assert models_dev.fallback_urls == (
        "https://models.dev/api.json?type=all",)
    assert models_dev.license == "MIT"
    assert models_dev.primary is True
    assert models_dev.format == "json"
    assert {"family", "context", "input_cost", "capabilities"} <= set(
        models_dev.fields)
    epoch = S.get_source("epoch")
    assert epoch.license == "CC BY 4.0"
    assert "Epoch AI" in epoch.attribution
    assert epoch.format == "csv"
    huggingface = S.get_source("huggingface")
    assert huggingface.fetchable is False
    assert huggingface.role == "future"
    assert S.get_source("openrouter").format == "json"
    with pytest.raises(S.SourceError, match="unknown source") as exc:
        S.get_source("nope")
    assert exc.value.reason == "unknown_source"


def test_models_dev_api_shape_maps_fields_and_costs() -> None:
    result = _parse("models-dev", "models_dev_api.json")
    assert result.shape == "models-dev:provider-map"
    external = result.to_external()
    assert set(external) == {"openai/gpt-5", "openai/embed-1", "acme/widget"}
    gpt = external["openai/gpt-5"]
    assert set(gpt) <= set(S.EXTERNAL_FIELDS)
    assert gpt["family"] == "gpt"
    assert gpt["type"] == "chat"
    assert gpt["context"] == 400000
    assert gpt["input_cost"] == 1.25
    assert gpt["output_cost"] == 10.0
    assert "image" in gpt["modalities_in"]
    assert {"tools", "vision", "reasoning", "structured_output"} <= set(
        gpt["capabilities"])
    assert gpt["source"] == "models-dev"
    assert gpt["confidence"] == "external"
    assert gpt["profile_source"].startswith("models-dev:")
    assert "output_limit" not in gpt
    model = next(item for item in result.models
                 if item.id == "openai/gpt-5")
    assert model.input_limit == 272000
    assert model.output_limit == 128000
    assert model.release_date == "2025-08-07"
    assert model.knowledge == "2024-10-01"
    assert model.open_weights is False

    widget = external["acme/widget"]
    assert widget["type"] == "chat"
    assert "decision" in widget["notes"]
    assert widget["context"] == 64000
    assert widget["capabilities"] == ["reasoning", "tools"]
    assert "open weights" in widget["notes"]

    embed = external["openai/embed-1"]
    assert embed["type"] == "embedding"
    assert embed["modalities_out"] == ["embedding"]


def test_models_dev_catalog_merges_canonical_and_endpoint_data() -> None:
    result = _parse("models-dev", "models_dev_catalog.json")
    external = result.to_external()
    assert "openai/gpt-5" in external
    assert "azure/gpt-5" in external
    assert "openai/gpt-5-2025-08-07" in external
    openai = external["openai/gpt-5"]
    azure = external["azure/gpt-5"]
    canonical = external["openai/gpt-5-2025-08-07"]
    # Endpoint-specific price/limit wins; canonical data fills the rest.
    assert openai["context"] == 400000
    assert openai["input_cost"] == 1.25
    assert openai["capabilities"]
    assert "image" in openai["modalities_in"]
    assert azure["context"] == 200000
    assert azure["input_cost"] == 2.0
    assert azure["output_cost"] == 20.0
    assert "tools" in azure["capabilities"]
    assert canonical["family"] == "gpt"
    models = {model.id: model for model in result.models}
    assert models["azure/gpt-5"].output_limit == 128000
    assert models["azure/gpt-5"].canonical_id == "openai/gpt-5-2025-08-07"


def test_models_dev_flat_models_shape() -> None:
    result = _parse("models-dev", "models_dev_models.json")
    external = result.to_external()
    assert set(external) == {"openai/gpt-5", "acme/box"}
    assert external["openai/gpt-5"]["context"] == 1234
    assert external["openai/gpt-5"]["type"] == "chat"
    assert "decision" in external["openai/gpt-5"]["notes"]
    assert external["acme/box"]["context"] == 8000
    assert external["acme/box"]["family"] == "box"


def test_models_dev_single_provider_wrapper_and_external_contract() -> None:
    payload = json.dumps({
        "provider": {
            "id": "openai",
            "models": {
                "gpt-x": {
                    "family": "gpt",
                    "type": "chat",
                    "limit": {"context": 1000},
                    "cost": {"input": 1.0, "output": 2.0},
                }
            }
        }
    }).encode("utf-8")
    result = S.parse_payload(S.get_source("models-dev"), payload)
    external = result.to_external()
    assert set(external) == {"openai/gpt-x"}
    parsed, unknown = G._parse_external(external)
    assert unknown == ()
    assert parsed["openai/gpt-x"]["context"] == 1000
    assert parsed["openai/gpt-x"]["profile_source"].startswith("models-dev:")


def test_litellm_mapping_costs_modes_and_capabilities() -> None:
    result = _parse("litellm", "litellm.json")
    external = result.to_external()
    assert set(external) == {
        "openai/gpt-5", "openai/embed-1",
        "openrouter/anthropic/claude-test"}
    gpt = external["openai/gpt-5"]
    assert gpt["context"] == 400000
    assert gpt["input_cost"] == pytest.approx(1.25)
    assert gpt["output_cost"] == pytest.approx(10.0)
    assert {"tools", "vision", "reasoning", "structured_output"} <= set(
        gpt["capabilities"])
    assert "image" in gpt["modalities_in"]
    assert "upstream source: https://openai.com/api/pricing/" in gpt["notes"]
    embed = external["openai/embed-1"]
    assert embed["type"] == "embedding"
    assert embed["input_cost"] == pytest.approx(0.02)
    claude = external["openrouter/anthropic/claude-test"]
    assert claude["type"] == "chat"
    assert {"pdf_native", "audio", "computer_use"} <= set(
        claude["capabilities"])
    assert "pdf" in claude["modalities_in"]
    assert "audio" in claude["modalities_in"]
    assert "deprecated 2026-01-01" in claude["notes"]
    assert any("no provider" in warning for warning in result.warnings)


def test_openrouter_mapping_per_token_prices_and_aliases() -> None:
    result = _parse("openrouter", "openrouter.json")
    external = result.to_external()
    assert set(external) == {
        "openai/gpt-5", "openai/gpt-5-2025-08-07", "acme/legacy",
        "acme/legacy-1"}
    gpt = external["openai/gpt-5"]
    assert gpt["context"] == 400000
    assert gpt["input_cost"] == pytest.approx(1.25)
    assert gpt["output_cost"] == pytest.approx(10.0)
    assert gpt["capabilities"] == ["reasoning", "structured_output", "tools"]
    assert gpt["modalities_in"] == ["image", "pdf", "text"]
    assert gpt["notes"].startswith("expires 2027-01-01")
    model = next(item for item in result.models if item.id == "openai/gpt-5")
    assert model.output_limit == 128000
    assert model.release_date == "2025-08-07"
    assert model.knowledge == "2024-10-01"
    legacy = external["acme/legacy"]
    assert "input_cost" not in legacy
    assert legacy["output_cost"] == 0.0


# --------------------------------------------------------------------------- #
# Epoch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("raw", "expected"), [
    ("1.8e12", 1800.0),
    ("175B", 175.0),
    ("2.4T", 2400.0),
    ("70", 70.0),
    ("1,800,000,000", 1.8),
    (180_000_000_000, 180.0),
    ("~7b", 7.0),
    ("unknown", None),
    ("0", None),
    ("-5B", None),
])
def test_parse_parameter_count(raw, expected) -> None:
    assert S.parse_parameter_count_b(raw) == expected


def test_epoch_index_is_conservative_and_apply_epoch_fills_gaps() -> None:
    parsed = _parse("epoch", "epoch.csv")
    assert parsed.epoch is not None
    index = parsed.epoch
    assert index.match("openai/gpt-5").params_total_b == 1800.0
    assert index.match("ollama/llama-3.3-70b").params_total_b == 70.0
    assert index.match("acme/mystery-model").params_total_b == 2400.0
    assert index.match("one/ambiguous-model").params_total_b == 7.0
    assert index.match("other/ambiguous-model") is None
    assert index.match("acme/unrelated-model") is None
    assert index.match("acme/bad-parameters").params_total_b is None

    external = {
        "openai/gpt-5": {
            "context": 1000,
            "source": "models-dev",
            "profile_source": "models-dev: openai/gpt-5",
            "notes": "from models.dev",
        },
        "openai/gpt-5-2025-08-07": {"context": 1000},
        "ollama/llama-3.3-70b": {"family": "llama"},
        "acme/unrelated-model": {"context": 10},
        "other/ambiguous-model": {"context": 10},
        "openai/already-known": {"params_total_b": 5.0},
    }
    enriched = S.apply_epoch(external, index)
    gpt = enriched["openai/gpt-5"]
    assert gpt["params_total_b"] == 1800.0
    assert "training compute: 3.2e25 FLOP" in gpt["notes"]
    assert "from models.dev" in gpt["notes"]
    assert "epoch enrichment" in gpt["profile_source"]
    assert enriched["ollama/llama-3.3-70b"]["params_total_b"] == 70.0
    assert "params_total_b" not in enriched["acme/unrelated-model"]
    assert "params_total_b" not in enriched["other/ambiguous-model"]
    assert enriched["openai/already-known"]["params_total_b"] == 5.0
    # The canonical alias is not guessed from the primary id.
    assert "params_total_b" not in enriched["openai/gpt-5-2025-08-07"]
    # The input is not mutated.
    assert "params_total_b" not in external["openai/gpt-5"]


# --------------------------------------------------------------------------- #
# cache import/load/sync
# --------------------------------------------------------------------------- #
def test_import_caches_atomically_with_sha256_and_meta(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    report = S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    assert report["status"] == "ok"
    assert report["origin"] == "import"
    data_file = S.cache_path(root, "models-dev")
    meta_file = S.meta_path(root, "models-dev")
    assert data_file.is_file() and meta_file.is_file()
    raw = data_file.read_bytes()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["source"] == "models-dev"
    assert meta["url"] == S.get_source("models-dev").url
    assert meta["license"] == "MIT"
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()
    assert meta["bytes"] == len(raw)
    assert meta["format"] == "json"
    assert meta["stale_after_s"] == S.get_source("models-dev").stale_after_s
    assert meta["imported_from"] == str(
        (FIXTURES / "models_dev_api.json").resolve())
    leftovers = [path.name for path in data_file.parent.iterdir()
                 if path.name.startswith(".")]
    assert leftovers == []

    loaded = S.load_cached(root, "models-dev")
    assert loaded.status == "ok"
    assert loaded.stale is False
    assert loaded.not_assessed is False
    assert loaded.external["openai/gpt-5"]["context"] == 400000
    assert loaded.meta is not None
    assert loaded.meta["sha256"] == meta["sha256"]


def test_load_missing_malformed_and_stale_cache_is_reported(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    missing = S.load_cached(root, "models-dev")
    assert missing.status == "missing"
    assert missing.not_assessed is True
    assert missing.external == {}
    assert "no cached" in missing.warnings[0]

    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    data_file = S.cache_path(root, "models-dev")
    data_file.write_text("{}", encoding="utf-8")
    malformed = S.load_cached(root, "models-dev")
    assert malformed.status == "malformed"
    assert malformed.external == {}
    assert "cached data is" in malformed.warnings[0]

    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    future = time.time() + 10 * 24 * 60 * 60
    stale = S.load_cached(root, "models-dev", now=future)
    assert stale.status == "stale"
    assert stale.stale is True
    assert stale.not_assessed is False
    assert stale.external["openai/gpt-5"]["context"] == 400000
    assert any("stale" in warning for warning in stale.warnings)


def test_import_rejects_malformed_oversized_and_future_snapshots(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(S.SourceError) as exc:
        S.import_file(root, "models-dev", bad_json)
    assert exc.value.reason == "malformed"

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text('{"providers": "nope"}', encoding="utf-8")
    with pytest.raises(S.SourceError) as exc:
        S.import_file(root, "models-dev", wrong_shape)
    assert exc.value.reason == "malformed"

    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("name,value\none,1\n", encoding="utf-8")
    with pytest.raises(S.SourceError) as exc:
        S.import_file(root, "epoch", bad_csv)
    assert exc.value.reason == "malformed"

    with pytest.raises(S.SourceError) as exc:
        S.import_file(
            root, "models-dev", FIXTURES / "models_dev_api.json", max_bytes=10)
    assert exc.value.reason == "too_large"
    assert not S.cache_path(root, "models-dev").exists()

    with pytest.raises(S.SourceError) as exc:
        S.import_file(root, "huggingface", FIXTURES / "models_dev_api.json")
    assert exc.value.reason == "unsupported_source"


def test_sync_refuses_network_by_default_without_calling_opener(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    opener = _FakeOpener(_fixture("models_dev_api.json"))
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", opener=opener)
    assert exc.value.reason == "network_not_allowed"
    assert exc.value.to_dict()["status"] == "refused"
    assert opener.calls == []
    assert not S.cache_path(root, "models-dev").exists()


def test_sync_with_injected_opener_caches_and_loads(tmp_path: Path) -> None:
    root = _root(tmp_path)
    opener = _FakeOpener(_fixture("models_dev_api.json"))
    report = S.sync(root, "models-dev", allow_network=True,
                    timeout_s=5, opener=opener)
    assert report["status"] == "ok"
    assert report["origin"] == "network"
    assert report["sha256"] == hashlib.sha256(
        _fixture("models_dev_api.json")).hexdigest()
    assert opener.calls and opener.calls[0][1] == 5
    loaded = S.load_cached(root, "models-dev")
    assert loaded.status == "ok"
    assert loaded.external["acme/widget"]["context"] == 64000


def test_sync_enforces_byte_cap_content_type_timeout_and_redirect_scheme(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    too_big = _FakeOpener(
        _fixture("models_dev_api.json"),
        responses=[_FakeResponse(b"x" * 100, declared=100)])
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", allow_network=True, max_bytes=10,
               opener=too_big)
    assert exc.value.reason == "too_large"

    # The cap also applies while streaming when Content-Length is absent or
    # unreliable, not only to a declared length.
    streamed = _FakeOpener(responses=[
        _FakeResponse(b"x" * 100, declared=0)])
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", allow_network=True, max_bytes=10,
               opener=streamed)
    assert exc.value.reason == "too_large"

    html = _FakeOpener(b"<html></html>", content_type="text/html")
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", allow_network=True, opener=html)
    assert exc.value.reason == "bad_content_type"

    timeout = _FakeOpener(error=TimeoutError("timed out"))
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", allow_network=True, opener=timeout)
    assert exc.value.reason == "timeout"

    from urllib.error import HTTPError

    redirect = _FakeOpener(responses=[
        HTTPError("https://models.dev/catalog.json?type=all", 302, "Found",
                  {"Location": "file:///etc/passwd"}, None),
    ])
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", allow_network=True, opener=redirect)
    assert exc.value.reason == "bad_url"

    malformed = _FakeOpener(b"{not json")
    with pytest.raises(S.SourceError) as exc:
        S.sync(root, "models-dev", allow_network=True, opener=malformed)
    assert exc.value.reason == "malformed"


def test_sync_uses_fallback_url_after_primary_failure(tmp_path: Path) -> None:
    root = _root(tmp_path)
    from urllib.error import HTTPError

    opener = _FakeOpener(responses=[
        HTTPError(S.get_source("models-dev").url, 404, "Not Found", {}, None),
        _FakeResponse(_fixture("models_dev_api.json"),
                      url=S.get_source("models-dev").fallback_urls[0]),
    ])
    report = S.sync(root, "models-dev", allow_network=True, opener=opener)
    assert report["status"] == "ok"
    assert report["fallback_used"] is True
    assert report["url"] == S.get_source("models-dev").fallback_urls[0]
    assert any("HTTP 404" in warning for warning in report["warnings"])


def test_source_status_lists_all_sources_and_not_assessed(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    report = S.source_status(root)
    assert report["schema"] == S.SCHEMA
    assert len(report["sources"]) == len(S.SOURCES)
    by_id = {entry["id"]: entry for entry in report["sources"]}
    assert by_id["models-dev"]["status"] == "ok"
    assert by_id["models-dev"]["stale"] is False
    assert by_id["models-dev"]["age_s"] is not None
    assert by_id["models-dev"]["license"] == "MIT"
    assert by_id["models-dev"]["fields"]
    assert by_id["models-dev"]["cache"]["status"] == "ok"
    assert by_id["litellm"]["status"] == "missing"
    assert by_id["huggingface"]["status"] == "future"
    assert by_id["huggingface"]["not_assessed"] is True
    assert "huggingface" in report["not_assessed"]
    assert "litellm" in report["not_assessed"]
    assert "models-dev" not in report["not_assessed"]


# --------------------------------------------------------------------------- #
# merge into the graph
# --------------------------------------------------------------------------- #
def test_merge_precedence_fill_and_missing_reporting(tmp_path: Path) -> None:
    root = _root(tmp_path)
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    S.import_file(root, "litellm", FIXTURES / "litellm.json")
    S.import_file(root, "openrouter", FIXTURES / "openrouter.json")
    merged = S.merge_cached(
        root, model_ids=["openai/gpt-5", "acme/legacy", "acme/widget"])
    assert merged.order == (
        "models-dev", "litellm", "openrouter", "epoch")
    assert merged.matched_models == 3
    gpt = merged.external["openai/gpt-5"]
    # models.dev wins for the fields it supplies.
    assert gpt["context"] == 400000
    assert gpt["input_cost"] == 1.25
    # LiteLLM fills a note models.dev omits.
    assert "upstream source" in gpt["notes"]
    # OpenRouter contributes a model the other caches do not have.
    assert merged.external["acme/legacy"]["context"] == 8192
    assert "epoch" in merged.not_assessed
    assert any(entry["source"] == "epoch"
               and entry["status"] == "missing"
               for entry in merged.sources)
    assert merged.to_dict()["metadata_models"] == 3


def test_merge_enriches_params_but_does_not_invent_matches(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    S.import_file(root, "epoch", FIXTURES / "epoch.csv")
    merged = S.merge_cached(
        root, model_ids=["openai/gpt-5", "acme/widget", "acme/mystery-model"])
    gpt = merged.external["openai/gpt-5"]
    assert gpt["params_total_b"] == 1800.0
    assert "training compute: 3.2e25 FLOP" in gpt["notes"]
    widget = merged.external["acme/widget"]
    assert "params_total_b" not in widget
    assert "training compute" not in widget.get("notes", "")
    # Epoch can enrich a catalog id known only to the local catalog, but it
    # still cannot add an id that is not in model_ids.
    mystery = merged.external["acme/mystery-model"]
    assert mystery["params_total_b"] == 2400.0
    assert "training compute" not in mystery.get("notes", "")
    assert "acme/not-a-catalog-model" not in merged.external
    assert merged.epoch_matches >= 2

    catalog = M.catalog(
        root, runner=_runner(_ids("acme/mystery-model")))
    node = G.graph(root, catalog=catalog, external=merged.external).get(
        "acme/mystery-model")
    assert node is not None
    assert node.params_total_b == 2400.0
    assert node.source == "epoch"
    assert node.profile_source == "epoch enrichment"


def test_graph_uses_external_but_override_and_curated_profile_win(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "ozagia").mkdir()
    (root / "ozagia" / "config.toml").write_text("""
[model_graph.models."openai/gpt-5"]
context = 1000
""", encoding="utf-8")
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    S.import_file(root, "epoch", FIXTURES / "epoch.csv")
    catalog = M.catalog(root, runner=_runner(_ids("openai/gpt-5")))
    merged = S.merge_cached(root, model_ids=["openai/gpt-5"])
    item = G.graph(root, catalog=catalog, external=merged.external)
    node = item.get("openai/gpt-5")
    assert node is not None
    # Explicit override wins.
    assert node.context == 1000
    assert node.source == "override"
    # Curated profile still fills fields external data omits.
    assert node.family == "gpt"
    assert node.variant == "gpt-5"
    assert "tools" in node.capabilities
    # Epoch supplies parameters but the curated size class remains the
    # graph's own coarse classification.
    assert node.params_total_b == 1800.0
    assert node.size_class == "frontier"
    assert "training compute" in node.notes
    assert node.profile_source == "model_graph.models"
    assert merged.external["openai/gpt-5"]["profile_source"].startswith(
        "models-dev:")


def test_graph_accepts_profile_source_from_external() -> None:
    parsed, unknown = G._parse_external({
        "acme/fable": {"profile_source": "models.dev: acme/fable"}})
    assert parsed["acme/fable"]["profile_source"] == "models.dev: acme/fable"
    assert unknown == ()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli_root(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    (root / "ozagia").mkdir()
    (root / "ozagia" / "config.toml").write_text("""
[models]
default = "openai/gpt-5"
""", encoding="utf-8")
    return root


def _catalog_ids() -> str:
    return _ids("openai/gpt-5", "acme/widget")


def test_cli_sources_json_and_text(tmp_path: Path, capsys,
                                  monkeypatch) -> None:
    root = _cli_root(tmp_path)
    assert models_cli.main(["sources", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == S.SCHEMA
    assert {entry["id"] for entry in doc["sources"]} == set(S.source_ids())
    assert "models-dev" in doc["not_assessed"]
    assert "huggingface" in doc["not_assessed"]

    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    assert models_cli.main(["sources", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "models-dev  status=ok" in text
    assert "license=MIT" in text
    assert "not_assessed: yes" in text


def test_cli_sync_refusal_and_import(tmp_path: Path, capsys) -> None:
    root = _cli_root(tmp_path)
    assert models_cli.main([
        "sync", "models-dev", "--root", str(root), "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "refused"
    assert doc["reason"] == "network_not_allowed"
    assert not S.cache_path(root, "models-dev").exists()

    assert models_cli.main([
        "import", "models-dev", str(FIXTURES / "models_dev_api.json"),
        "--root", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert S.cache_path(root, "models-dev").is_file()


def test_cli_graph_and_match_use_cached_sources_by_default(
        tmp_path: Path, capsys, monkeypatch) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_catalog_ids()))
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")

    assert models_cli.main([
        "graph", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    widget = next(node for node in doc["nodes"]
                  if node["id"] == "acme/widget")
    assert widget["context"] == 64000
    assert widget["source"] == "models-dev"
    assert widget["confidence"] == "external"
    assert doc["sources"]["order"][0] == "models-dev"

    assert models_cli.main([
        "match", "--capability", "tools", "--min-context", "60000",
        "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "acme/widget" in [candidate["id"] for candidate in doc["candidates"]]
    assert doc["sources"]["metadata_models"] == 2

    assert models_cli.main([
        "match", "--capability", "tools", "--min-context", "60000",
        "--root", str(root), "--no-sources", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "acme/widget" not in [
        candidate["id"] for candidate in doc["candidates"]]
    assert doc["sources"]["order"] == []


def test_cli_show_with_metadata_and_unknown_source(
        tmp_path: Path, capsys, monkeypatch) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_catalog_ids()))
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    assert models_cli.main([
        "show", "acme/widget", "--root", str(root), "--with-metadata",
        "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["profile"]["context"] == 64000
    assert doc["profile"]["source"] == "models-dev"
    assert doc["metadata"]["context"] == 64000
    assert doc["sources"]["order"][0] == "models-dev"

    assert models_cli.main([
        "show", "acme/widget", "--root", str(root), "--with-metadata"]) == 0
    text = capsys.readouterr().out
    assert "metadata:" in text
    assert "context: 64000" in text
    assert "sources: models-dev=ok" in text

    assert models_cli.main([
        "graph", "--root", str(root), "--source", "nope", "--json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["reason"] == "unknown_source"


def test_no_network_or_model_calls_in_tests(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no network/model call expected")

    monkeypatch.setattr(S, "build_opener", forbidden)
    monkeypatch.setattr(M.subprocess, "run", forbidden)
    S.import_file(root, "models-dev", FIXTURES / "models_dev_api.json")
    S.source_status(root)
    merged = S.merge_cached(root)
    catalog = M.catalog(root, runner=_runner(_ids("openai/gpt-5")))
    G.graph(root, catalog=catalog, external=merged.external)
