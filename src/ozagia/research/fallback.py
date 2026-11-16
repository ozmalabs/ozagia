"""Open-access fallback for a failed web fetch of a paper.

A harness's own web fetch fails on many paper URLs: a publisher landing page
answers 403, an old link answers 404, or the response is a PDF the tool can't
read. Two pieces here:

- `failure_reason`, `paper_like`, `after_lines`: local, offline checks for
  the intent layer's `after` of a web fetch (`webfetch`). When the output shows
  a failure and the URL looks like a paper (or the failure is a PDF), `after`
  appends one hint line naming `research_fetch`. Nothing here touches the
  network; a hook never fetches.
- `fetch_fallback(url)`: the explicit fetch behind the `research_fetch` MCP
  tool. It derives identifiers from the URL (arXiv, DOI, PubMed/PMC, Europe
  PMC, Semantic Scholar, OpenReview, `.pdf` links), lists open-access
  candidates with the documented providers of `ozagia.research.fetch`
  (arXiv, Crossref/OpenAlex, Unpaywall when an email is configured, Europe
  PMC) and the Semantic Scholar Graph API, follows `citation_pdf_url` on a
  landing page once, downloads with a size cap, a timeout and a content-type
  check, extracts text with `ozagia.research.pdf` (or JATS XML with
  `ozagia.research.tei`), caches the artifact and its text by content hash
  under `<state dir>/research/fetch`, and returns the text page by page with
  a stable `source:` key.

Requests go through `fetch.http_get`: its User-Agent, its SSRF guard (no
private hosts, no credentials in URLs), no cookies and no auth headers.
Robots and terms are handled as the rest of `ozagia.research.fetch` handles
them: documented APIs and open-access copies only, no paywall bypass.
`OZAGIA_RESEARCH_OFFLINE=1` turns the network off; cached copies are still
served. Counters (`research_fetches`, `research_fetch_cache_hits`) are kept in
the cache directory; the daemon's `stats` reports them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from ozagia.research import references as refs

ENV_OFFLINE = "OZAGIA_RESEARCH_OFFLINE"
# Unpaywall needs a contact email; without one it is skipped.
ENV_EMAIL = "OZAGIA_RESEARCH_EMAIL"
DEFAULT_MAX_BYTES = 25_000_000
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_CHARS = 20_000
MAX_CHARS_LIMIT = 100_000
# Downloads tried for one URL at most (candidates and landing-page links).
MAX_DOWNLOADS = 8
CACHE_PARTS = ("research", "fetch")
COUNTERS = ("research_fetches", "research_fetch_cache_hits")
HINT_TOOL = "research_fetch"

# How much of a fetch tool's output the failure checks look at: a failure
# message comes first, and a page that merely mentions "404" further down is
# not a failure.
_HEAD_CHARS = 600
_BINARY_SAMPLE = 2000

_STATUS_RE = re.compile(
    r"(?:\bstatus(?:\s+code)?|\bHTTP(?:/\d(?:\.\d)?)?|\berror|\bcode|\bfailed\s+with)"
    r"\s*[:=]?\s*(4\d\d|5\d\d)\b", re.I)
_STATUS_PHRASE_RE = re.compile(
    r"\b(404\s+not\s+found|403\s+forbidden|401\s+unauthorized|410\s+gone"
    r"|429\s+too\s+many\s+requests|451\s+unavailable)\b", re.I)
_UNSUPPORTED_RE = re.compile(
    r"unsupported\s+(?:content|mime|media|file)[\s-]*type"
    r"|content[\s-]*type\b.{0,60}\b(?:not\s+supported|unsupported|cannot\s+be)"
    r"|cannot\s+(?:read|process|parse|display|handle)\s+(?:this\s+)?(?:binary|pdf)"
    r"|binary\s+(?:content|data|file)\b.{0,40}\b(?:not|cannot|unsupported)"
    r"|(?:pdf|binary)\s+(?:files?|content|documents?)\s+(?:is|are)\s+not\s+supported",
    re.I | re.S)
_PDF_TYPE_RE = re.compile(r"application/pdf", re.I)

# Hosts whose pages are papers (or paper landing pages) even when the URL
# carries no identifier.
PAPER_HOSTS = (
    "arxiv.org", "doi.org", "semanticscholar.org", "openreview.net",
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "europepmc.org",
    "biorxiv.org", "medrxiv.org", "ssrn.com", "papers.ssrn.com",
    "sciencedirect.com", "elsevier.com", "springer.com", "nature.com",
    "wiley.com", "tandfonline.com", "sagepub.com", "jstor.org",
    "ieeexplore.ieee.org", "dl.acm.org", "acm.org", "aclanthology.org",
    "proceedings.mlr.press", "neurips.cc", "papers.nips.cc", "plos.org",
    "frontiersin.org", "mdpi.com", "academic.oup.com", "cambridge.org",
    "science.org", "cell.com", "pnas.org", "iop.org", "aps.org",
    "pubs.acs.org", "rsc.org", "researchgate.net", "huggingface.co/papers",
    "ar5iv.labs.arxiv.org", "osf.io", "zenodo.org", "hal.science",
    "hal.archives-ouvertes.fr", "jmlr.org", "ojs.aaai.org", "ijcai.org",
    "thecvf.com", "openaccess.thecvf.com", "royalsocietypublishing.org",
    "bmj.com", "thelancet.com", "nejm.org", "jamanetwork.com",
)

_DOI_IN_PATH = re.compile(r"(10\.\d{4,9}/[^\s?#&\"'<>]+)")
_ARXIV_PATH = re.compile(
    r"^/(?:abs|pdf|html|format|e-print)/((?:\d{4}\.\d{4,5})|(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
    r"(v\d+)?", re.I)
_S2_ID = re.compile(r"/paper/(?:[^/]+/)?([0-9a-f]{40})\b", re.I)


class FetchFallbackError(Exception):
    """The fallback fetch found no readable open-access copy (or is off)."""


# --------------------------------------------------------------------------- #
# local checks (no network)
# --------------------------------------------------------------------------- #
def offline() -> bool:
    """Whether `$OZAGIA_RESEARCH_OFFLINE` turns research network access off."""
    return os.environ.get(ENV_OFFLINE, "").strip().lower() in (
        "1", "true", "yes", "on")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _clean_doi(doi: str) -> str | None:
    doi = unquote(doi)
    doi = re.sub(r"(?:\.full(?:\.pdf)?|\.abstract|\.pdf|/full|/abstract|/pdf"
                 r"|/epdf|/meta|/references)$", "", doi, flags=re.I)
    if doi.lower().startswith("10.1101/"):
        doi = re.sub(r"v\d+$", "", doi)   # bioRxiv/medRxiv version suffix
    return refs.normalize_doi(doi)


def paper_ids(url: str) -> dict[str, Any]:
    """Identifiers a URL names: `arxiv`, `doi`, `pmid`, `pmcid`, `s2`
    (Semantic Scholar paper id), `openreview`, and `pdf: True` for a URL
    that names a PDF itself. Empty for anything else."""
    if not isinstance(url, str):
        return {}
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return {}
    host = (parts.hostname or "").casefold()
    path = unquote(parts.path or "")
    query = parse_qs(parts.query or "")
    ids: dict[str, Any] = {}
    if _host_matches(host, "arxiv.org"):
        m = _ARXIV_PATH.match(path)
        if m:
            ids["arxiv"] = m.group(1)
            if path.lower().startswith("/pdf/"):
                ids["pdf"] = True
    elif _host_matches(host, "huggingface.co") and path.startswith("/papers/"):
        arxiv = refs.normalize_arxiv(path.split("/")[2] if path.count("/") >= 2 else "")
        if arxiv:
            ids["arxiv"] = arxiv
    elif _host_matches(host, "openreview.net"):
        oid = (query.get("id") or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{5,40}", oid):
            ids["openreview"] = oid
            if path.startswith("/pdf"):
                ids["pdf"] = True
    elif _host_matches(host, "semanticscholar.org"):
        m = _S2_ID.search(path)
        if m:
            ids["s2"] = m.group(1).lower()
        elif path.startswith("/arxiv/"):
            arxiv = refs.normalize_arxiv(path[len("/arxiv/"):])
            if arxiv:
                ids["arxiv"] = arxiv
    elif host in ("pubmed.ncbi.nlm.nih.gov",):
        m = re.match(r"^/(\d{1,9})/?$", path)
        if m:
            ids["pmid"] = m.group(1)
    elif _host_matches(host, "ncbi.nlm.nih.gov"):
        m = re.search(r"/(PMC\d{4,9})\b", path, re.I)
        if m:
            ids["pmcid"] = m.group(1).upper()
        else:
            m = re.match(r"^/pubmed/(\d{1,9})/?$", path)
            if m:
                ids["pmid"] = m.group(1)
    elif _host_matches(host, "europepmc.org"):
        m = re.search(r"/(PMC\d{4,9})\b", path, re.I)
        if m:
            ids["pmcid"] = m.group(1).upper()
        else:
            m = re.search(r"/(?:article|abstract)/MED/(\d{1,9})\b", path, re.I)
            if m:
                ids["pmid"] = m.group(1)
    if "arxiv" not in ids:
        m = _DOI_IN_PATH.search(path)
        if m:
            doi = _clean_doi(m.group(1))
            if doi:
                ids["doi"] = doi
        if "doi" not in ids:
            for key in ("doi", "DOI"):
                for value in query.get(key) or []:
                    doi = _clean_doi(value)
                    if doi:
                        ids["doi"] = doi
                        break
    if path.lower().endswith(".pdf"):
        ids["pdf"] = True
    return ids


def paper_like(url: str) -> bool:
    """Whether a URL looks like a paper, a paper's landing page or a PDF."""
    if paper_ids(url):
        return True
    host = _host(url)
    if not host:
        return False
    try:
        path = urlsplit(url).path or ""
    except ValueError:
        path = ""
    for domain in PAPER_HOSTS:
        name, _, prefix = domain.partition("/")
        if _host_matches(host, name) and (not prefix or path.startswith("/" + prefix)):
            return True
    return False


def _binary(text: str) -> bool:
    sample = text[:_BINARY_SAMPLE]
    if len(sample) < 200:
        return False
    odd = sum(1 for ch in sample
              if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\r\t\f"))
    return odd / len(sample) > 0.05


def failure_reason(output: Any) -> str | None:
    """Why a web fetch's output looks failed, or None: `http <code>`,
    `unsupported content type`, `pdf binary`."""
    if isinstance(output, (bytes, bytearray)):
        output = bytes(output[:_BINARY_SAMPLE]).decode("latin-1")
    if not isinstance(output, str) or not output.strip():
        return None
    head = output.lstrip()[:_HEAD_CHARS]
    if "%PDF-" in output[:1024]:
        return "pdf binary"
    m = _STATUS_RE.search(head)
    if m:
        return f"http {m.group(1)}"
    m = _STATUS_PHRASE_RE.search(head)
    if m:
        return f"http {m.group(1)[:3]}"
    if _UNSUPPORTED_RE.search(head):
        return ("pdf binary" if _PDF_TYPE_RE.search(head)
                else "unsupported content type")
    if _binary(output):
        return "pdf binary" if _PDF_TYPE_RE.search(head) else "binary content"
    return None


def hint_line(url: str) -> str:
    """The `[ozagia]` line `after` appends for a failed fetch of `url`."""
    return ("[ozagia] fetch failed; the research fetcher can try: "
            f"{HINT_TOOL} " + json.dumps({"url": url}, ensure_ascii=False))


def after_lines(inp: Mapping[str, Any], output: Any) -> tuple[list[str], str | None]:
    """The hint lines for a web fetch (`webfetch`) that ran, and the failure
    reason: one line when its output shows a failure and the URL is
    paper-like (or the failure is a PDF or binary body). Local only: it
    never fetches."""
    url = inp.get("url") if isinstance(inp, Mapping) else None
    if not isinstance(url, str) or not url.strip():
        return [], None
    url = url.strip()
    try:
        if urlsplit(url).scheme not in ("http", "https"):
            return [], None
    except ValueError:
        return [], None
    reason = failure_reason(output)
    if reason is None:
        return [], None
    if not (paper_like(url) or reason in ("pdf binary", "binary content")):
        return [], reason
    return [hint_line(url)], reason


# --------------------------------------------------------------------------- #
# cache and counters
# --------------------------------------------------------------------------- #
def cache_dir(root: str | Path) -> Path:
    from ozagia.config import state_path
    return state_path(root, *CACHE_PARTS)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_counters(root: str | Path) -> dict[str, int]:
    """The fetch counters of `root` (zeros when none were written)."""
    out = {k: 0 for k in COUNTERS}
    try:
        data = json.loads((cache_dir(root) / "counters.json").read_text("utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for k in COUNTERS:
            v = data.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[k] = v
    return out


def _bump(root: str | Path, **inc: int) -> None:
    """Add to the persisted counters; never raises (a lost increment under
    a race is acceptable for a usage counter)."""
    try:
        path = cache_dir(root) / "counters.json"
        lock = None
        try:
            import fcntl
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = open(path.with_suffix(".lock"), "a")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except (ImportError, OSError):
            lock = None
        try:
            counts = read_counters(root)
            for k, v in inc.items():
                counts[k] = counts.get(k, 0) + int(v)
            _atomic_write(path, json.dumps(counts, sort_keys=True).encode())
        finally:
            if lock is not None:
                lock.close()
    except Exception:  # noqa: BLE001 - counters are advisory
        pass


def _url_digest(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def source_key(digest: str) -> str:
    """The stable key of a fetched artifact: its content hash."""
    return f"source:sha256:{digest[:16]}"


class FetchCache:
    """`<state dir>/research/fetch`: `objects/<aa>/<sha256><ext>` (the
    artifact), `text/<sha256>.json` (its extracted text and metadata) and
    `urls/<sha256(url)>.json` (which artifact a URL resolved to)."""

    def __init__(self, root: str | Path):
        self.dir = cache_dir(root)

    def _url_path(self, url: str) -> Path:
        return self.dir / "urls" / f"{_url_digest(url)}.json"

    def _text_path(self, digest: str) -> Path:
        return self.dir / "text" / f"{digest}.json"

    def lookup(self, url: str) -> dict[str, Any] | None:
        try:
            ref = json.loads(self._url_path(url).read_text("utf-8"))
            digest = ref.get("sha256") if isinstance(ref, dict) else None
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                return None
            return self.text(digest)
        except (OSError, ValueError):
            return None

    def text(self, digest: str) -> dict[str, Any] | None:
        try:
            entry = json.loads(self._text_path(digest).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return entry if isinstance(entry, dict) and isinstance(entry.get("pages"), list) else None

    def store_object(self, content: bytes, digest: str, ext: str) -> Path:
        target = self.dir / "objects" / digest[:2] / f"{digest}{ext}"
        if not target.is_file():
            _atomic_write(target, content)
        return target

    def store_text(self, digest: str, entry: dict[str, Any]) -> None:
        _atomic_write(self._text_path(digest),
                      json.dumps(entry, ensure_ascii=False).encode("utf-8"))

    def index(self, url: str, digest: str) -> None:
        _atomic_write(self._url_path(url), json.dumps(
            {"url": url, "sha256": digest}, ensure_ascii=False).encode("utf-8"))


# --------------------------------------------------------------------------- #
# landing pages
# --------------------------------------------------------------------------- #
class _MetaParser(HTMLParser):
    """`citation_pdf_url`, `citation_doi`, `citation_arxiv_id` and
    `<link rel=alternate type=application/pdf>` of a landing page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pdf: list[str] = []
        self.doi: list[str] = []
        self.arxiv: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "").strip()
            if not content:
                return
            if name in ("citation_pdf_url", "eprints.document_url", "bepress_citation_pdf_url"):
                self.pdf.append(content)
            elif name in ("citation_doi", "dc.identifier", "prism.doi", "bepress_citation_doi"):
                self.doi.append(content)
            elif name == "citation_arxiv_id":
                self.arxiv.append(content)
            elif name == "citation_title" and not self.title:
                self.title = content[:500]
        elif tag == "link":
            if ("alternate" in a.get("rel", "").lower()
                    and "pdf" in a.get("type", "").lower() and a.get("href")):
                self.pdf.append(a["href"])


def landing_links(html: bytes, base_url: str) -> dict[str, Any]:
    """PDF links and identifiers a landing page declares in its meta tags."""
    parser = _MetaParser()
    try:
        parser.feed(html[:2_000_000].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - malformed HTML gives what it gave
        pass
    pdfs = []
    for href in parser.pdf:
        full = urljoin(base_url, href)
        if urlsplit(full).scheme in ("http", "https") and full not in pdfs:
            pdfs.append(full)
    doi = next((d for d in (refs.normalize_doi(x) for x in parser.doi) if d), None)
    arxiv = next((d for d in (refs.normalize_arxiv(x) for x in parser.arxiv) if d), None)
    return {"pdf": pdfs[:3], "doi": doi, "arxiv": arxiv, "title": parser.title}


# --------------------------------------------------------------------------- #
# the fetch
# --------------------------------------------------------------------------- #
def _candidate(url: str, provider: str, *, kind: str = "pdf", depth: int = 0,
               license: Any = None, version: Any = None) -> dict[str, Any]:
    return {"url": url, "provider": provider, "kind": kind, "depth": depth,
            "license": license, "version": version}


def _is_pdf(content: bytes) -> bool:
    return b"%PDF-" in content[:1024]


def _media(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().casefold()


class _Run:
    def __init__(self, url: str, *, fetcher: Callable[..., Any], email: str | None,
                 max_bytes: int, timeout: float,
                 pdf_extractor: Callable[..., Any] | None, cache: FetchCache) -> None:
        self.url = url
        self.cache = cache
        self.fetcher = fetcher
        self.email = email
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.pdf_extractor = pdf_extractor
        self.ids = paper_ids(url)
        self.attempts: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.resolved: set[str] = set()
        self.queue: deque[dict[str, Any]] = deque()
        self.metadata: dict[str, Any] = {"identifiers": {
            k: v for k, v in self.ids.items() if k != "pdf"}}

    # -- candidates ------------------------------------------------------- #
    def _stages(self) -> Iterator[Callable[[], list[dict[str, Any]]]]:
        ids = self.ids
        if ids.get("pdf"):
            yield lambda: [_candidate(self.url, "direct")]
        if ids.get("arxiv"):
            yield lambda: self._arxiv(ids["arxiv"])
        if ids.get("openreview"):
            yield lambda: [_candidate(
                f"https://openreview.net/pdf?id={ids['openreview']}", "openreview")]
        if ids.get("s2"):
            yield lambda: self._semantic_scholar(ids["s2"])
        if ids.get("pmcid"):
            yield lambda: self._pmc(ids["pmcid"])
        if ids.get("pmid"):
            yield lambda: self._pubmed(ids["pmid"])
        if ids.get("doi"):
            yield lambda: self._doi(ids["doi"])
        if not ids.get("pdf"):
            # The URL itself last: a landing page may declare its PDF.
            yield lambda: [_candidate(self.url, "direct", kind="any")]
        # Identifiers learned on the way (Semantic Scholar, Europe PMC, a
        # landing page) are resolved after the URL's own.
        while True:
            stage = self._learned()
            if stage is None:
                return
            yield stage

    def _learned(self) -> Callable[[], list[dict[str, Any]]] | None:
        idents = self.metadata["identifiers"]
        for kind, fn in (("arxiv", self._arxiv), ("pmcid", self._pmc),
                         ("doi", self._doi)):
            value = idents.get(kind)
            if isinstance(value, str) and value and f"{kind}:{value}" not in self.resolved:
                return lambda fn=fn, value=value: fn(value)
        return None

    def _learn(self, **identifiers: Any) -> None:
        idents = self.metadata["identifiers"]
        for k, v in identifiers.items():
            if isinstance(v, str) and v and not idents.get(k):
                idents[k] = v

    def _arxiv(self, arxiv_id: str) -> list[dict[str, Any]]:
        self.resolved.add(f"arxiv:{arxiv_id}")
        from urllib.parse import quote
        return [_candidate(f"https://arxiv.org/pdf/{quote(arxiv_id, safe='/')}",
                           "arxiv", version="pdf")]

    def _pmc(self, pmcid: str) -> list[dict[str, Any]]:
        self.resolved.add(f"pmcid:{pmcid}")
        from urllib.parse import quote
        return [_candidate(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/"
            f"{quote(pmcid)}/fullTextXML", "europepmc", kind="jats", version="jats")]

    def _pubmed(self, pmid: str) -> list[dict[str, Any]]:
        from ozagia.research.fetch import resolve_europepmc
        ref = resolve_europepmc({"pmid": pmid}, fetcher=self.fetcher)
        idents = refs.normalize_identifiers(ref.get("identifiers"))
        self._learn(pmcid=idents.get("pmcid"), doi=idents.get("doi"))
        self.metadata.setdefault("title", ref.get("title"))
        return []

    def _semantic_scholar(self, paper_id: str) -> list[dict[str, Any]]:
        from ozagia.research.fetch import _json_fetch
        payload = _json_fetch(
            self.fetcher,
            f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}"
            "?fields=title,externalIds,openAccessPdf",
            timeout=min(self.timeout, 20.0), max_bytes=2_000_000)
        if not isinstance(payload, Mapping):
            return []
        external = payload.get("externalIds") or {}
        if isinstance(external, Mapping):
            self._learn(arxiv=refs.normalize_arxiv(external.get("ArXiv")),
                        doi=refs.normalize_doi(external.get("DOI")),
                        pmcid=refs.normalize_pmcid(external.get("PubMedCentral")))
        if payload.get("title"):
            self.metadata.setdefault("title", str(payload["title"])[:500])
        oa = payload.get("openAccessPdf")
        if isinstance(oa, Mapping) and isinstance(oa.get("url"), str) and oa["url"]:
            return [_candidate(oa["url"], "semanticscholar",
                               license=oa.get("license"), version=oa.get("status"))]
        return []

    def _doi(self, doi: str) -> list[dict[str, Any]]:
        self.resolved.add(f"doi:{doi}")
        from ozagia.research.fetch import preview_fetch
        preview = preview_fetch(doi, email=self.email, fetcher=self.fetcher)
        ref = preview.get("reference") or {}
        if ref.get("title"):
            self.metadata.setdefault("title", ref["title"])
        idents = preview.get("identifiers") or {}
        self._learn(arxiv=idents.get("arxiv"), pmcid=idents.get("pmcid"))
        for note in preview.get("notes") or []:
            self.attempts.append({"provider": "unpaywall", "note": note})
        out = []
        for c in preview.get("candidates") or []:
            media = _media(c.get("media_type"))
            if "gzip" in media or "tar" in media:
                continue      # arXiv source bundles are not text
            kind = "jats" if "xml" in media else ("any" if "html" in media else "pdf")
            out.append(_candidate(str(c["url"]), str(c.get("provider") or "doi"),
                                  kind=kind, license=c.get("license"),
                                  version=c.get("version")))
        return out

    # -- downloads -------------------------------------------------------- #
    def run(self) -> dict[str, Any]:
        stages = self._stages()
        downloads = 0
        while True:
            if not self.queue:
                stage = next(stages, None)
                if stage is None:
                    break
                try:
                    self.queue.extend(stage())
                except Exception as exc:  # noqa: BLE001 - recorded, next stage
                    self.attempts.append({"error": f"resolve: {exc}"})
                continue
            cand = self.queue.popleft()
            if cand["url"] in self.seen:
                continue
            if downloads >= MAX_DOWNLOADS:
                self.attempts.append({"url": cand["url"], "error": "download limit reached"})
                break
            self.seen.add(cand["url"])
            downloads += 1
            try:
                got = self._download(cand)
            except Exception as exc:  # noqa: BLE001 - recorded, next candidate
                self.attempts.append({"url": cand["url"], "provider": cand["provider"],
                                      "error": str(exc) or type(exc).__name__})
                continue
            if got is not None:
                return got
        detail = "; ".join(
            (f"{a.get('url')}: {a['error']}" if a.get("url") else a.get("error") or a.get("note", ""))
            for a in self.attempts)
        raise FetchFallbackError(
            f"no readable open-access copy found for {self.url}"
            + (f" ({detail})" if detail else ""))

    def _download(self, cand: dict[str, Any]) -> dict[str, Any] | None:
        from ozagia.research.fetch import _content
        response = _content(self.fetcher, cand["url"], timeout=self.timeout,
                            max_bytes=self.max_bytes)
        content = response.get("content")
        if not isinstance(content, bytes):
            raise FetchFallbackError("fetcher returned no byte content")
        if len(content) > self.max_bytes:
            raise FetchFallbackError(f"response exceeds {self.max_bytes} bytes")
        media = _media(response.get("media_type"))
        final_url = str(response.get("final_url") or cand["url"])
        if _is_pdf(content):
            return self._artifact(cand, content, final_url, "application/pdf", ".pdf")
        if "pdf" in media or media in ("application/octet-stream", "binary/octet-stream"):
            raise FetchFallbackError(
                f"content-type mismatch: {media or 'no type'} but the body is not a PDF")
        if "xml" in media and cand["kind"] in ("jats", "any"):
            return self._artifact(cand, content, final_url, "application/xml", ".xml")
        if "html" in media:
            if cand["depth"] >= 1:
                raise FetchFallbackError("content-type mismatch: expected a PDF, got text/html")
            found = landing_links(content, final_url)
            if found.get("title"):
                self.metadata.setdefault("title", found["title"])
            self._learn(doi=found.get("doi"), arxiv=found.get("arxiv"))
            for href in found["pdf"]:
                self.queue.appendleft(_candidate(href, f"{cand['provider']}:landing",
                                                 depth=cand["depth"] + 1,
                                                 license=cand.get("license")))
            if not found["pdf"] and not found.get("doi") and not found.get("arxiv"):
                raise FetchFallbackError(
                    "landing page (text/html) declares no PDF or identifier")
            return None
        raise FetchFallbackError(f"unsupported content type: {media or 'unknown'}")

    def _extract(self, content: bytes, media: str) -> list[dict[str, Any]]:
        if media == "application/xml":
            from ozagia.research import tei
            parsed = tei.parse_tei(content)
            pages = []
            if parsed.get("abstract"):
                pages.append({"number": 1, "text": "Abstract\n\n" + parsed["abstract"]})
            for section in parsed.get("sections") or []:
                text = f"{section.get('heading') or ''}\n\n{section.get('text') or ''}".strip()
                if text:
                    pages.append({"number": len(pages) + 1, "text": text})
            if parsed.get("title"):
                self.metadata.setdefault("title", parsed["title"])
            return pages
        extractor = self.pdf_extractor
        if extractor is None:
            from ozagia.research.pdf import extract_pdf

            def extractor(data: bytes) -> dict[str, Any]:
                return extract_pdf(data, ocr=False, timeout=max(self.timeout, 30.0))
        result = extractor(content)
        if isinstance(result, Mapping) and result.get("title"):
            self.metadata.setdefault("title", result["title"])
        pages = result.get("pages") if isinstance(result, Mapping) else None
        return [{"number": int(p.get("number") or i + 1), "text": str(p.get("text") or "")}
                for i, p in enumerate(pages or []) if isinstance(p, Mapping)]

    def _artifact(self, cand: dict[str, Any], content: bytes, final_url: str,
                  media: str, ext: str) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        cache = self.cache
        cached = cache.text(digest)
        if cached is not None:
            cache.index(self.url, digest)
            return {**cached, "url": self.url, "attempts": self.attempts}
        pages = self._extract(content, media)
        if not any(p["text"].strip() for p in pages):
            raise FetchFallbackError("the document has no extractable text")
        path = cache.store_object(content, digest, ext)
        title = self.metadata.get("title")
        entry = {
            "source": source_key(digest),
            "url": self.url,
            "resolved_url": final_url,
            "provider": cand["provider"],
            "media_type": media,
            "sha256": digest,
            "size": len(content),
            "cache_relative": path.relative_to(cache.dir).as_posix(),
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "license": cand.get("license"),
            "version": cand.get("version"),
            "title": title if isinstance(title, str) else "",
            "identifiers": dict(self.metadata["identifiers"]),
            "pages": pages,
        }
        cache.store_text(digest, entry)
        cache.index(self.url, digest)
        return {**entry, "attempts": self.attempts}


def _window(entry: Mapping[str, Any], offset: int, max_chars: int) -> dict[str, Any]:
    """Page chunks covering characters [offset, offset + max_chars) of the
    document's text (pages joined by a blank line)."""
    pages = [p for p in entry.get("pages") or [] if isinstance(p, Mapping)]
    sep = "\n\n"
    chunks = []
    pos = 0
    end = offset + max_chars
    total = 0
    for i, page in enumerate(pages):
        text = str(page.get("text") or "")
        start = pos
        stop = start + len(text)
        if stop > offset and start < end and text:
            lo = max(offset, start) - start
            hi = min(end, stop) - start
            chunks.append({"page": int(page.get("number") or i + 1),
                           "start": start + lo,
                           "key": f"{entry.get('source')}#p{int(page.get('number') or i + 1)}",
                           "text": text[lo:hi]})
        pos = stop + (len(sep) if i < len(pages) - 1 else 0)
        total = stop
    returned_to = min(end, total)
    return {"chunks": chunks, "offset": offset, "total_chars": total,
            "next_offset": returned_to if returned_to < total else None,
            "pages": len(pages)}


def fetch_fallback(
    url: str,
    *,
    root: str | Path = ".",
    offset: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
    fetcher: Callable[..., Any] | None = None,
    pdf_extractor: Callable[..., Any] | None = None,
    email: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Fetch an open-access copy of the paper `url` names and return its
    text from character `offset`, at most `max_chars` (see the module doc).

    A URL fetched before is answered from the cache with `cache_hit: true`
    and no network access. Raises `FetchFallbackError` when nothing readable
    was found (the message lists each attempt) or when offline without a
    cached copy."""
    if not isinstance(url, str) or not url.strip():
        raise FetchFallbackError("url must be a non-empty string")
    url = url.strip()
    if urlsplit(url).scheme not in ("http", "https"):
        raise FetchFallbackError("url must use http or https")
    offset = max(0, int(offset or 0))
    max_chars = max(1, min(int(max_chars or DEFAULT_MAX_CHARS), MAX_CHARS_LIMIT))
    cache = FetchCache(root)
    cached = cache.lookup(url)
    if cached is not None:
        _bump(root, research_fetches=1, research_fetch_cache_hits=1)
        return _answer(cached, offset, max_chars, cache_hit=True, attempts=[])
    if offline():
        _bump(root, research_fetches=1)
        raise FetchFallbackError(
            f"research network access is off ({ENV_OFFLINE}=1) and {url} is not cached")
    if fetcher is None:
        from ozagia.research.fetch import http_get
        fetcher = http_get
    run = _Run(url, fetcher=fetcher,
               email=email or os.environ.get(ENV_EMAIL) or None,
               max_bytes=int(max_bytes), timeout=float(timeout),
               pdf_extractor=pdf_extractor, cache=cache)
    _bump(root, research_fetches=1)
    entry = run.run()
    attempts = entry.pop("attempts", [])
    return _answer(entry, offset, max_chars, cache_hit=False, attempts=attempts)


def _answer(entry: Mapping[str, Any], offset: int, max_chars: int, *,
            cache_hit: bool, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    meta = {k: entry.get(k) for k in (
        "source", "url", "resolved_url", "provider", "media_type", "sha256", "size",
        "cache_relative",
        "retrieved_at", "license", "version", "title", "identifiers")}
    out = {"ok": True, **meta, "cache_hit": cache_hit,
           **_window(entry, offset, max_chars),
           "truth_assessed": False}
    if attempts:
        out["attempts"] = attempts
    if out["next_offset"] is not None:
        out["more"] = (f"{HINT_TOOL} " + json.dumps(
            {"url": entry.get("url"), "offset": out["next_offset"]}, ensure_ascii=False))
    return out


# --------------------------------------------------------------------------- #
# the MCP tool
# --------------------------------------------------------------------------- #
TOOL_SPEC: dict[str, Any] = {
    "name": HINT_TOOL,
    "description": (
        "Fetch the text of a paper whose web fetch failed (404/403, a PDF the "
        "fetch tool can't read, a publisher landing page). Resolves open-access "
        "copies (arXiv, DOI via Crossref/OpenAlex/Unpaywall, PubMed/PMC via "
        "Europe PMC, Semantic Scholar, OpenReview, citation_pdf_url on a landing "
        "page), downloads with a size cap, extracts PDF or JATS text, caches it "
        "by content hash and returns it page by page with a stable source: key. "
        "Pass offset=next_offset for the next part. Open access only; network "
        "is off with OZAGIA_RESEARCH_OFFLINE=1 (cached copies are still served)."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL that failed."},
            "max_chars": {"type": "integer",
                          "description": f"Characters to return (default {DEFAULT_MAX_CHARS}, "
                                         f"at most {MAX_CHARS_LIMIT})."},
            "offset": {"type": "integer",
                       "description": "Character offset into the document text (default 0)."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}


def research_fetch_tool(args: Mapping[str, Any], *, root: str | Path,
                        **kwargs: Any) -> dict[str, Any]:
    """The `research_fetch` MCP handler."""
    return fetch_fallback(str(args.get("url") or ""), root=root,
                          offset=int(args.get("offset") or 0),
                          max_chars=int(args.get("max_chars") or DEFAULT_MAX_CHARS),
                          **kwargs)


__all__ = ["COUNTERS", "ENV_EMAIL", "ENV_OFFLINE", "FetchCache", "FetchFallbackError",
           "TOOL_SPEC", "after_lines", "cache_dir", "failure_reason", "fetch_fallback",
           "hint_line", "landing_links", "offline", "paper_ids", "paper_like",
           "read_counters", "research_fetch_tool", "source_key"]
