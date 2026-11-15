from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ozagia.research.service import ResearchService


def _workspace(tmp_path, files: dict[str, bytes | str]):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    for name, content in files.items():
        path = source_root / name
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)
    return source_root


def _init(svc: ResearchService, source_root: Path, names: list[str]) -> str:
    return svc.call("init", {
        "question": "Deferred integration",
        "roots": [{
            "root_id": "papers",
            "path": str(source_root),
            "files": {name: "selected" for name in names},
        }],
    })["investigation_id"]


def test_pdf_preview_selected_and_cached_source(tmp_path):
    source_root = _workspace(tmp_path, {"paper.pdf": b"%PDF-1.7 fixture"})

    def fake_pdf(data, **_kwargs):
        return {
            "format": "pdf",
            "sha256": hashlib.sha256(data).hexdigest(),
            "backend": "pdftotext",
            "pages": [{"number": 1, "text": "Page text"}],
            "title": "",
            "authors": [],
            "abstract": "",
            "sections": [],
            "references": [{"title": "Cited Work", "citation_key": "cited2020"}],
            "citations": [],
            "warnings": [],
            "degraded_math": True,
            "truth_assessed": False,
        }

    svc = ResearchService(tmp_path, pdf_extractor=fake_pdf)
    sid = _init(svc, source_root, ["paper.pdf"])
    parsed = svc.call("pdf_preview", {
        "session_id": sid, "root_id": "papers", "path": "paper.pdf",
        "record_references": True})
    assert parsed["pages"][0]["text"] == "Page text"
    assert parsed["recorded_references"][0]["citation_key"] == "cited2020"
    assert parsed["truth_assessed"] is False

    raw = b"%PDF-1.7 cached"
    digest = hashlib.sha256(raw).hexdigest()
    relative = f"objects/{digest[:2]}/{digest}.pdf"
    cache_path = tmp_path / ".ozagia" / "research" / "cache" / relative
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(raw)
    source = svc.call("record", {
        "session_id": sid, "kind": "Source",
        "fields": {"locator": {"kind": "cached_fetch", "cache_relative": relative,
                               "sha256": digest, "media_type": "application/pdf"}},
    })
    cached = svc.call("pdf_preview", {
        "session_id": sid, "source_id": source["id"]})
    assert cached["sha256"] == digest


def test_semantic_build_and_hybrid_search(tmp_path):
    source_root = _workspace(tmp_path, {
        "a.md": "# Graph methods\nA graph traversal method with citations.\n",
        "b.md": "# Other\nUnrelated prose.\n",
    })
    svc = ResearchService(tmp_path)
    sid = _init(svc, source_root, ["a.md", "b.md"])
    built = svc.call("semantic_build", {"session_id": sid})
    assert built["engine"] == "hash-bow"
    assert "not a semantic model" in built["note"]
    result = svc.call("semantic_search", {
        "session_id": sid, "query": "graph traversal", "limit": 5})
    assert result["engine"] == "hybrid-rrf"
    assert result["results"]
    assert result["results"][0]["citation"]["path"] == "a.md"
    assert result["truth_assessed"] is False


def test_citation_fetch_neighborhood_and_path(tmp_path):
    source_root = _workspace(tmp_path, {})
    source_payload = {
        "id": "https://openalex.org/W1",
        "referenced_works": ["https://openalex.org/W2"],
    }
    target_payload = {
        "id": "https://openalex.org/W2",
        "title": "Cited Work",
        "doi": "https://doi.org/10.5555/cited.1",
        "publication_year": 2020,
        "authorships": [{"author": {"display_name": "Ada Doe"}}],
        "primary_location": {"source": {"display_name": "Journal"}},
    }

    def fake_fetch(url, **_kwargs):
        if url.endswith("/works/https://doi.org/10.1234/abc.1"):
            payload = source_payload
        elif url.endswith("/works/W2"):
            payload = target_payload
        else:
            raise AssertionError(url)
        return {"content": json.dumps(payload).encode(), "media_type": "application/json",
                "final_url": url, "headers": {}}

    svc = ResearchService(tmp_path, fetcher=fake_fetch)
    sid = _init(svc, source_root, [])
    reference = svc.call("ref_record", {
        "session_id": sid,
        "reference": {"title": "Source Work", "citation_key": "source2024",
                      "identifiers": {"doi": "10.1234/abc.1"}},
    })["reference"]
    fetched = svc.call("ref_citation_fetch", {
        "session_id": sid, "reference_id": reference["id"], "limit": 1})
    assert fetched["edges"]
    cited_id = fetched["recorded"][0]["reference"]["id"]
    neighbourhood = svc.call("ref_citation_neighborhood", {
        "session_id": sid, "reference_id": reference["id"]})
    assert {node["id"] for node in neighbourhood["nodes"]} == {reference["id"], cited_id}
    path = svc.call("ref_citation_path", {
        "session_id": sid, "src": reference["id"], "tgt": cited_id})
    assert path["found"] is True
    assert path["length"] == 1


def test_manuscript_check_and_bundle_with_fake_pandoc(tmp_path):
    source_root = _workspace(tmp_path, {})
    calls = []

    def fake_runner(argv, cwd):
        calls.append((argv, cwd))
        return {"returncode": 0, "stdout": "", "stderr": ""}

    svc = ResearchService(tmp_path, pandoc_runner=fake_runner)
    sid = _init(svc, source_root, [])
    svc.call("ref_record", {
        "session_id": sid,
        "reference": {"title": "A Graph Paper", "citation_key": "smith2024",
                      "identifiers": {"doi": "10.1234/abc.1"}},
    })
    checks = svc.call("manuscript_check", {
        "session_id": sid, "body": "See [@smith2024]."})
    assert checks["ok"] is True
    bundle = svc.call("manuscript_bundle", {
        "session_id": sid, "title": "Test Manuscript",
        "body": "See [@smith2024].",
        "output_dir": str(tmp_path / "exports"),
        "run_pandoc": True,
    })
    assert bundle["pandoc"]["status"] == "success"
    for name in ("manuscript", "references", "claims", "checks"):
        assert Path(bundle["bundle"][name]).is_file()
    assert calls
    assert calls[0][0][:2] == ["pandoc", "manuscript.md"]
    assert "--citeproc" in calls[0][0]
