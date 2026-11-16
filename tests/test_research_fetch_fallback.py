"""tests/test_research_fetch_fallback.py -- the fetch fallback (W15).

Failed web fetches get a `research_fetch` hint from `after` (no network), and
`research_fetch` resolves an open-access copy. Downloads go to a local HTTP
server: a fetcher rewrites `https://<host>/<path>` to
`http://127.0.0.1:<port>/<host>/<path>` and calls the real `http_get` (with
the private-host guard lifted for the test only). Nothing reaches the network.
"""
from __future__ import annotations

import json
import shutil
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ozagia import intent as I
from ozagia.research import fallback as FB
from ozagia.research import fetch as F

needs_pdftotext = pytest.mark.skipif(shutil.which("pdftotext") is None,
                                     reason="pdftotext not installed")


def make_pdf(*pages: str) -> bytes:
    """A small valid PDF with one line of Helvetica text per page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font = 3 + 2 * n
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> "
                    f"/Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                    + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref}\n%%EOF\n").encode()
    return bytes(out)


def fake_pdf(data, **_kwargs):
    """A PDF extractor for tests that don't need pdftotext."""
    assert data.startswith(b"%PDF-")
    return {"pages": [{"number": 1, "text": "page one text"},
                      {"number": 2, "text": "page two text"}], "title": ""}


class Site:
    """A local HTTP server; `routes["<host>/<path?query>"] = (status, type, body)`."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str, bytes]] = {}
        self.requests: list[dict] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                key = self.path.lstrip("/")
                site.requests.append({"path": key, "headers": dict(self.headers.items())})
                status, ctype, body = site.routes.get(key, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def local(self, url: str) -> str:
        parts = urlsplit(url)
        return (f"http://127.0.0.1:{self.port}/{parts.hostname}{parts.path}"
                + (f"?{parts.query}" if parts.query else ""))

    def fetcher(self, url, **kwargs):
        got = F.http_get(self.local(url), allow_private=True, **kwargs)
        if got["final_url"] == self.local(url):
            got["final_url"] = url   # as the real host would report it
        return got

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (FB.ENV_OFFLINE, FB.ENV_EMAIL, "OZAGIA_STATE_DIR", "OZAGIA_GROBID_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open a connection fails the test."""
    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(F, "urlopen", refuse)
    monkeypatch.setattr(F, "http_get", refuse)


# --------------------------------------------------------------------------- #
# failure detection and the hint (local)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("output, reason", [
    ("Request failed with status code: 404", "http 404"),
    ("Error: HTTP 403 Forbidden", "http 403"),
    ("403 Forbidden", "http 403"),
    ("Unsupported content type: application/pdf", "pdf binary"),
    ("Error: unsupported content type image/png", "unsupported content type"),
    ("%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj", "pdf binary"),
    ("Title: A page\n\nSome article text.", None),
    ("Plenty of text " * 100 + " status code 404 explained", None),
    ("", None),
])
def test_failure_reason(output, reason):
    assert FB.failure_reason(output) == reason


@pytest.mark.parametrize("url, ids", [
    ("https://arxiv.org/abs/2401.12345v2", {"arxiv": "2401.12345"}),
    ("https://arxiv.org/pdf/2401.12345", {"arxiv": "2401.12345", "pdf": True}),
    ("https://doi.org/10.1234/ABC.5", {"doi": "10.1234/abc.5"}),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/xyz.123", {"doi": "10.1002/xyz.123"}),
    ("https://www.biorxiv.org/content/10.1101/2020.01.01.123456v1.full.pdf",
     {"doi": "10.1101/2020.01.01.123456", "pdf": True}),
    ("https://pubmed.ncbi.nlm.nih.gov/12345678/", {"pmid": "12345678"}),
    ("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/", {"pmcid": "PMC1234567"}),
    ("https://www.semanticscholar.org/paper/Some-Title/" + "a" * 40, {"s2": "a" * 40}),
    ("https://openreview.net/forum?id=AbCdE12345", {"openreview": "AbCdE12345"}),
    ("https://example.org/files/paper.pdf", {"pdf": True}),
    ("https://example.org/blog/post", {}),
])
def test_paper_ids(url, ids):
    assert FB.paper_ids(url) == ids


def test_hint_only_for_paper_like_urls_or_pdf_failures():
    lines, reason = FB.after_lines({"url": "https://arxiv.org/abs/2401.12345"},
                                   "Request failed with status code: 404")
    assert reason == "http 404"
    assert lines == ['[ozagia] fetch failed; the research fetcher can try: '
                     'research_fetch {"url": "https://arxiv.org/abs/2401.12345"}']
    # A 404 of a page that is not a paper gets no hint.
    assert FB.after_lines({"url": "https://example.org/docs"}, "status code: 404") == (
        [], "http 404")
    # A PDF the tool can't read gets one wherever it is.
    assert FB.after_lines({"url": "https://example.org/x"}, "%PDF-1.5 ...")[0]
    # A page that loaded gets none.
    assert FB.after_lines({"url": "https://arxiv.org/abs/2401.12345"}, "Abstract: ...") == (
        [], None)


def test_intent_after_appends_the_hint_without_network(no_network):
    res = I.after(object(), "webfetch", {"url": "https://doi.org/10.1234/abc"},
                  "Request failed with status code: 403")
    assert res["append"] == ('[ozagia] fetch failed; the research fetcher can try: '
                             'research_fetch {"url": "https://doi.org/10.1234/abc"}')
    assert res["meta"]["intent"] == "after_fetch_failed"
    assert res["meta"]["fetch_fallback_hints"] == 1
    assert res["meta"]["fetch_failure"] == "http 403"
    ok = I.after(object(), "webfetch", {"url": "https://doi.org/10.1234/abc"}, "Title: x")
    assert ok["append"] == "" and "fetch_fallback_hints" not in ok["meta"]


def test_daemon_counts_hints_and_reads_fetch_counters(tmp_path, no_network):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(tmp_path, object(), tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "webfetch", "harness": "kiro", "session": "k1",
           "input": {"url": "https://arxiv.org/pdf/2401.12345"},
           "output": "Unsupported content type: application/pdf"}
    resp = st.dispatch(msg)
    assert "research_fetch" in resp["append"]
    st.record(msg, resp, 1.0)
    FB._bump(tmp_path, research_fetches=3, research_fetch_cache_hits=1)
    stats = st.stats()
    assert stats["fetch_fallback_hints"] == 1
    assert stats["research_fetches"] == 3 and stats["research_fetch_cache_hits"] == 1


# --------------------------------------------------------------------------- #
# research_fetch against a local server
# --------------------------------------------------------------------------- #
@needs_pdftotext
def test_arxiv_pdf_is_extracted_cached_and_hit(tmp_path, site):
    site.routes["arxiv.org/pdf/2401.12345"] = (
        200, "application/pdf", make_pdf("Hello fallback world", "Second page here"))
    url = "https://arxiv.org/abs/2401.12345"
    got = FB.fetch_fallback(url, root=tmp_path, fetcher=site.fetcher)
    assert got["ok"] is True and got["cache_hit"] is False
    assert got["provider"] == "arxiv" and got["media_type"] == "application/pdf"
    assert got["source"] == FB.source_key(got["sha256"])
    assert got["source"].startswith("source:sha256:")
    text = "\n".join(c["text"] for c in got["chunks"])
    assert "Hello fallback world" in text and "Second page here" in text
    assert [c["page"] for c in got["chunks"]] == [1, 2]
    assert got["chunks"][0]["key"] == got["source"] + "#p1"
    assert got["identifiers"]["arxiv"] == "2401.12345"
    # Cached by content hash under the state dir.
    cache = FB.cache_dir(tmp_path)
    assert (cache / got["cache_relative"]).is_file()
    assert (cache / "text" / f"{got['sha256']}.json").is_file()
    # The request carried the User-Agent and no cookies or credentials.
    (req,) = site.requests
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers["user-agent"] == F.USER_AGENT
    assert "cookie" not in headers and "authorization" not in headers

    again = FB.fetch_fallback(url, root=tmp_path, fetcher=site.fetcher)
    assert again["cache_hit"] is True
    assert again["chunks"] == got["chunks"] and again["source"] == got["source"]
    assert len(site.requests) == 1                     # no second download
    assert FB.read_counters(tmp_path) == {"research_fetches": 2,
                                          "research_fetch_cache_hits": 1}


def test_paging_by_offset_and_max_chars(tmp_path, site):
    site.routes["example.org/p.pdf"] = (200, "application/pdf", make_pdf("x"))
    url = "https://example.org/p.pdf"
    first = FB.fetch_fallback(url, root=tmp_path, fetcher=site.fetcher,
                              pdf_extractor=fake_pdf, max_chars=10)
    assert first["provider"] == "direct"
    assert first["chunks"] == [{"page": 1, "start": 0, "key": first["source"] + "#p1",
                                "text": "page one t"}]
    assert first["total_chars"] == len("page one text\n\npage two text")
    assert first["next_offset"] == 10
    assert first["more"] == 'research_fetch {"url": "https://example.org/p.pdf", "offset": 10}'
    rest = FB.fetch_fallback(url, root=tmp_path, fetcher=site.fetcher, offset=10,
                             max_chars=1000)
    assert rest["cache_hit"] is True and rest["next_offset"] is None
    assert [(c["page"], c["text"]) for c in rest["chunks"]] == [
        (1, "ext"), (2, "page two text")]


def test_size_cap(tmp_path, site):
    site.routes["example.org/big.pdf"] = (200, "application/pdf", make_pdf("y" * 5000))
    with pytest.raises(FB.FetchFallbackError, match="exceeds 1000 bytes"):
        FB.fetch_fallback("https://example.org/big.pdf", root=tmp_path,
                          fetcher=site.fetcher, pdf_extractor=fake_pdf, max_bytes=1000)
    assert not (FB.cache_dir(tmp_path) / "objects").exists()


def test_content_type_mismatch(tmp_path, site):
    site.routes["example.org/fake.pdf"] = (200, "application/pdf", b"<html>login</html>")
    site.routes["example.org/img.pdf"] = (200, "image/png", b"\x89PNG....")
    with pytest.raises(FB.FetchFallbackError, match="content-type mismatch"):
        FB.fetch_fallback("https://example.org/fake.pdf", root=tmp_path,
                          fetcher=site.fetcher, pdf_extractor=fake_pdf)
    with pytest.raises(FB.FetchFallbackError, match="unsupported content type: image/png"):
        FB.fetch_fallback("https://example.org/img.pdf", root=tmp_path,
                          fetcher=site.fetcher, pdf_extractor=fake_pdf)


def test_404_with_no_open_access_copy_lists_the_attempts(tmp_path, site):
    with pytest.raises(FB.FetchFallbackError) as err:
        FB.fetch_fallback("https://arxiv.org/abs/2401.99999", root=tmp_path,
                          fetcher=site.fetcher, pdf_extractor=fake_pdf)
    msg = str(err.value)
    assert "no readable open-access copy" in msg and "404" in msg
    assert "arxiv.org/pdf/2401.99999" in site.paths()


def test_landing_page_citation_pdf_url_is_followed(tmp_path, site):
    html = (b'<html><head><meta name="citation_title" content="A Paper">'
            b'<meta name="citation_pdf_url" content="/content/paper.pdf"></head></html>')
    site.routes["journal.example/article/1"] = (200, "text/html; charset=utf-8", html)
    site.routes["journal.example/content/paper.pdf"] = (200, "application/pdf", make_pdf("z"))
    got = FB.fetch_fallback("https://journal.example/article/1", root=tmp_path,
                            fetcher=site.fetcher, pdf_extractor=fake_pdf)
    assert got["provider"] == "direct:landing"
    assert got["title"] == "A Paper"
    assert site.paths() == ["journal.example/article/1", "journal.example/content/paper.pdf"]


def test_doi_resolves_through_openalex(tmp_path, site):
    doi = "10.1234/abc.1"
    site.routes[f"api.openalex.org/works/https://doi.org/{doi}"] = (
        200, "application/json", json.dumps({
            "id": "https://openalex.org/W1", "title": "Open Work", "doi": f"https://doi.org/{doi}",
            "open_access": {"is_oa": True, "oa_status": "green",
                            "oa_url": "https://repo.example/oa.pdf"}}).encode())
    site.routes["repo.example/oa.pdf"] = (200, "application/pdf", make_pdf("oa"))
    got = FB.fetch_fallback(f"https://doi.org/{doi}", root=tmp_path,
                            fetcher=site.fetcher, pdf_extractor=fake_pdf)
    assert got["provider"] == "openalex" and got["identifiers"]["doi"] == doi
    assert got["title"] == "Open Work"
    # Crossref answered 404 and was skipped; doi.org itself was not needed.
    assert site.paths()[-1] == "repo.example/oa.pdf"


def test_offline_mode(tmp_path, site, monkeypatch):
    site.routes["example.org/p.pdf"] = (200, "application/pdf", make_pdf("x"))
    url = "https://example.org/p.pdf"
    FB.fetch_fallback(url, root=tmp_path, fetcher=site.fetcher, pdf_extractor=fake_pdf)
    monkeypatch.setenv(FB.ENV_OFFLINE, "1")
    count = len(site.requests)
    assert FB.fetch_fallback(url, root=tmp_path, fetcher=site.fetcher)["cache_hit"] is True
    with pytest.raises(FB.FetchFallbackError, match="OZAGIA_RESEARCH_OFFLINE=1"):
        FB.fetch_fallback("https://example.org/other.pdf", root=tmp_path,
                          fetcher=site.fetcher, pdf_extractor=fake_pdf)
    assert len(site.requests) == count


def test_default_fetcher_refuses_private_hosts(tmp_path, site):
    with pytest.raises(FB.FetchFallbackError, match="private address"):
        FB.fetch_fallback(f"http://127.0.0.1:{site.port}/x.pdf", root=tmp_path,
                          pdf_extractor=fake_pdf)
    assert site.requests == []


# --------------------------------------------------------------------------- #
# the MCP tool
# --------------------------------------------------------------------------- #
def _call(server, args):
    return server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "research_fetch", "arguments": args}})


def test_mcp_research_fetch(tmp_path, monkeypatch, site):
    from ozagia.integrations.mcp.server import DevMemoryServer
    server = DevMemoryServer(None, mode="research", workspace=str(tmp_path))
    tools = {t["name"]: t for t in server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    schema = tools["research_fetch"]["inputSchema"]
    assert set(schema["properties"]) == {"url", "max_chars", "offset"}
    assert schema["required"] == ["url"]
    # Offline and not cached: a tool error naming the switch.
    monkeypatch.setenv(FB.ENV_OFFLINE, "1")
    resp = _call(server, {"url": "https://arxiv.org/abs/2401.12345"})
    assert resp["result"]["isError"] is True
    assert "OZAGIA_RESEARCH_OFFLINE" in resp["result"]["content"][0]["text"]
    # Cached (fetched earlier through the local server): served offline.
    monkeypatch.delenv(FB.ENV_OFFLINE)
    site.routes["example.org/p.pdf"] = (200, "application/pdf", make_pdf("x"))
    FB.fetch_fallback("https://example.org/p.pdf", root=tmp_path, fetcher=site.fetcher,
                      pdf_extractor=fake_pdf)
    monkeypatch.setenv(FB.ENV_OFFLINE, "1")
    resp = _call(server, {"url": "https://example.org/p.pdf", "max_chars": 8, "offset": 0})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["cache_hit"] is True and body["chunks"][0]["text"] == "page one"
    assert body["next_offset"] == 8


def test_research_fetch_is_registered_in_every_mode(tmp_path):
    from ozagia.integrations.mcp import server as S
    assert "research_fetch" not in {t["name"] for t in S._TOOLS}
    # Every mode registers it (`_load_research_fetch_tool`).
    assert "_load_research_fetch_tool" in vars(S.DevMemoryServer)
    srv = S.DevMemoryServer(None, mode="research", workspace=str(tmp_path))
    assert "research_fetch" in srv._handlers


def test_module_imports_nothing_heavy():
    """The intent layer imports `fallback` on every webfetch `after`."""
    import subprocess
    import sys
    code = ("import sys, ozagia.research.fallback as f; "
            "print(sorted(m for m in sys.modules if m.startswith('ozagia.research')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=Path(__file__).resolve().parents[1])
    assert "ozagia.research.pdf" not in out.stdout
    assert "ozagia.research.fetch" not in out.stdout
