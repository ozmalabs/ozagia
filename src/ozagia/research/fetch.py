"""Open-access metadata and artifact acquisition.

The default providers are documented APIs. External providers, including
unofficial scrapers, live in separate packages and are used only when named.
Downloaded artifacts are content-addressed under the state directory and
recorded as Sources; nothing here claims that a paper supports a statement.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit
from urllib.request import (HTTPHandler, HTTPRedirectHandler, HTTPSHandler, Request,
                            build_opener)

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError
from ozagia.research.providers import get_provider, provider_status

USER_AGENT = "ozagia-research/0.1 (+https://github.com/ozmalabs/ozagia)"
MAX_FETCH_BYTES = 50_000_000


class FetchError(ResearchError):
    """Metadata, URL or artifact acquisition failed safely."""


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _host_is_private(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified):
            return True
    return False


def validate_url(url: str, *, allowed_hosts: set[str] | None = None) -> str:
    """Reject non-HTTP, credentialed and private-network URLs (SSRF guard)."""
    if not isinstance(url, str) or not url.strip():
        raise FetchError("fetch URL must be a non-empty string")
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise FetchError("fetch URL must use http or https")
    if parts.username or parts.password:
        raise FetchError("fetch URL must not contain credentials")
    host = parts.hostname
    if not host:
        raise FetchError("fetch URL has no host")
    if allowed_hosts is not None and host.casefold() not in {
        item.casefold() for item in allowed_hosts
    }:
        raise FetchError(f"fetch host {host!r} is not allowlisted")
    if _host_is_private(host):
        raise FetchError(f"fetch host {host!r} resolves to a private address")
    return parts.geturl()


class _GuardedRedirect(HTTPRedirectHandler):
    """Validates each redirect target with `validate_url` before following
    it, so no request reaches a private or non-allowlisted hop."""

    def __init__(self, allowed_hosts: set[str] | None) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        validate_url(newurl, allowed_hosts=self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _peer_checked(conn_class: type) -> Callable[..., Any]:
    """A connection factory for `do_open` whose TCP connect checks the
    connected peer address (`_host_is_private`) before anything is sent,
    which closes the DNS-rebinding window between `validate_url`'s lookup
    and the connection's own. Proxied requests skip the check: their peer
    is the configured proxy."""

    def factory(host: str, **kwargs: Any) -> Any:
        conn = conn_class(host, **kwargs)

        def connect(address: Any, *args: Any, **kw: Any) -> socket.socket:
            sock = socket.create_connection(address, *args, **kw)
            if getattr(conn, "_tunnel_host", None):
                return sock
            try:
                peer = str(sock.getpeername()[0]).split("%", 1)[0]
                private = _host_is_private(peer)
            except BaseException:
                sock.close()
                raise
            if private:
                sock.close()
                raise FetchError(f"fetch host {address[0]!r} connected to a private "
                                 f"address ({peer})")
            return sock

        conn._create_connection = connect
        return conn

    return factory


class _GuardedHTTP(HTTPHandler):
    def http_open(self, req):  # type: ignore[override]
        if req.has_proxy():
            return self.do_open(http.client.HTTPConnection, req)
        return self.do_open(_peer_checked(http.client.HTTPConnection), req)


class _GuardedHTTPS(HTTPSHandler):
    def https_open(self, req):  # type: ignore[override]
        context = getattr(self, "_context", None)
        if req.has_proxy():
            return self.do_open(http.client.HTTPSConnection, req, context=context)
        return self.do_open(_peer_checked(http.client.HTTPSConnection), req,
                            context=context)


def _open(request: Request, *, timeout: float, allowed_hosts: set[str] | None) -> Any:
    opener = build_opener(_GuardedRedirect(allowed_hosts), _GuardedHTTP(), _GuardedHTTPS())
    return opener.open(request, timeout=timeout)


def _read(response: Any, max_bytes: int, end: float | None) -> bytes:
    """The body, at most `max_bytes + 1` bytes, read in pieces so that `end`
    (a `time.monotonic()` deadline) bounds the whole read and a server that
    trickles bytes cannot hold it open."""
    chunks: list[bytes] = []
    size = 0
    sock = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(sock, "_sock", None)
    while size <= max_bytes:
        if end is not None:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise FetchError("download exceeded its time limit")
            if sock is not None:
                try:
                    sock.settimeout(max(0.01, remaining))
                except OSError:
                    pass
        try:
            chunk = response.read1(min(65536, max_bytes + 1 - size))
        except TimeoutError:
            if end is not None and end - time.monotonic() <= 0.05:
                raise FetchError("download exceeded its time limit") from None
            raise
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def http_get(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = MAX_FETCH_BYTES,
    allowed_hosts: set[str] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Fetch a URL with a size cap and the SSRF guard on the URL, every
    redirect hop (before it is requested) and the connected peer address.
    `timeout` applies to each socket operation; `deadline`, in seconds, if
    given, bounds the whole request including the body. The request carries
    `USER_AGENT` and no cookies or credentials."""
    end = time.monotonic() + float(deadline) if deadline is not None else None
    safe_url = validate_url(url, allowed_hosts=allowed_hosts)
    request = Request(safe_url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if end is not None:
        timeout = max(0.01, min(float(timeout), end - time.monotonic()))
    try:
        with _open(request, timeout=timeout, allowed_hosts=allowed_hosts) as response:
            final_url = response.geturl()
            validate_url(final_url, allowed_hosts=allowed_hosts)
            declared = response.headers.get("Content-Length")
            try:
                declared_bytes = int(declared) if declared is not None else None
            except (TypeError, ValueError):
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > max_bytes:
                raise FetchError(f"response exceeds {max_bytes} bytes")
            content = _read(response, max_bytes, end)
            if len(content) > max_bytes:
                raise FetchError(f"response exceeds {max_bytes} bytes")
            return {
                "content": content,
                "media_type": response.headers.get_content_type(),
                "final_url": final_url,
                "headers": dict(response.headers.items()),
            }
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a typed fetch error
        reason = getattr(exc, "reason", None)
        if isinstance(reason, FetchError):
            raise reason from exc
        raise FetchError(f"fetch failed for {safe_url}: {exc}") from exc


def _content(fetcher: Callable[..., Any], url: str, **kwargs: Any) -> dict[str, Any]:
    result = fetcher(url, **kwargs)
    if isinstance(result, dict):
        return result
    if isinstance(result, bytes):
        return {"content": result, "media_type": None, "final_url": url, "headers": {}}
    raise FetchError("fetcher must return bytes or a response mapping")


def _json_fetch(fetcher: Callable[..., Any], url: str, **kwargs: Any) -> Any:
    response = _content(fetcher, url, **kwargs)
    content = response.get("content", b"")
    try:
        return json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FetchError(f"provider returned invalid JSON: {exc}") from exc


def _text_fetch(fetcher: Callable[..., Any], url: str, **kwargs: Any) -> str:
    response = _content(fetcher, url, **kwargs)
    content = response.get("content", b"")
    try:
        return content.decode("utf-8") if isinstance(content, bytes) else str(content)
    except UnicodeError as exc:
        raise FetchError(f"provider returned non-UTF-8 content: {exc}") from exc


def _provenance(provider: str, payload: Any) -> dict[str, Any]:
    encoded = (
        payload.encode("utf-8") if isinstance(payload, str)
        else json.dumps(payload, sort_keys=True).encode("utf-8")
    )
    return {
        "provider": provider,
        "retrieved_at": _utc(),
        "response_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def classify_identifier(value: str) -> dict[str, str]:
    text = str(value).strip()
    doi = refs.normalize_doi(text)
    if doi:
        return {"doi": doi}
    if re.search(r"arxiv|/\d{7}|\d{4}\.\d{4,5}", text, re.I):
        arxiv = refs.normalize_arxiv(text)
        if arxiv:
            return {"arxiv": arxiv}
    pmcid = refs.normalize_pmcid(text)
    if pmcid:
        return {"pmcid": pmcid}
    if re.fullmatch(r"\d{5,9}", text) or re.match(r"pmid\s*:", text, re.I):
        pmid = refs.normalize_pmid(text)
        if pmid:
            return {"pmid": pmid}
    isbn = refs.normalize_isbn(text)
    if isbn and re.search(r"isbn", text, re.I):
        return {"isbn": isbn}
    return {}


def _authors_from_crossref(message: Mapping[str, Any]) -> list[str]:
    authors = []
    for author in message.get("author") or []:
        family = refs._collapse(author.get("family", ""))
        given = refs._collapse(author.get("given", ""))
        if family and given:
            authors.append(f"{family}, {given}")
        elif family or author.get("name"):
            authors.append(family or refs._collapse(author.get("name")))
    return authors


def _year_from_crossref(message: Mapping[str, Any]) -> str:
    for key in ("published-print", "published-online", "issued", "created"):
        part = message.get(key)
        if isinstance(part, Mapping):
            date_parts = part.get("date-parts")
            if isinstance(date_parts, list) and date_parts and date_parts[0]:
                return refs._year(date_parts[0][0])
    return ""


def resolve_crossref(doi: str, *, fetcher: Callable[..., Any] = http_get,
                     email: str | None = None) -> dict[str, Any]:
    suffix = f"?mailto={quote(email)}" if email else ""
    payload = _json_fetch(
        fetcher,
        f"https://api.crossref.org/works/{quote(doi, safe='/')}{suffix}",
        timeout=20,
        max_bytes=20_000_000,
    )
    message = payload.get("message") if isinstance(payload, Mapping) else None
    if not isinstance(message, Mapping):
        raise FetchError("Crossref response has no work message")
    title = message.get("title") or []
    abstract = re.sub(r"<[^>]+>", " ", refs._collapse(message.get("abstract", "")))
    reference = {
        "title": refs._collapse(title[0] if title else ""),
        "authors": _authors_from_crossref(message),
        "year": _year_from_crossref(message),
        "venue": refs._collapse((message.get("container-title") or [""])[0]),
        "reference_type": "article",
        "abstract": refs._collapse(abstract),
        "url": refs._collapse(message.get("URL", "")),
        "identifiers": {"doi": message.get("DOI") or doi},
        "metadata_status": "resolved_crossref",
        "metadata_provenance": [_provenance("crossref", payload)],
    }
    license_info = message.get("license")
    if isinstance(license_info, list) and license_info:
        reference["license"] = refs._collapse(license_info[0].get("URL", ""))
    return reference


def resolve_openalex(identifier: str, *, fetcher: Callable[..., Any] = http_get) -> dict[str, Any]:
    text = str(identifier).strip()
    if refs.normalize_doi(text):
        work_url = f"https://api.openalex.org/works/https://doi.org/{refs.normalize_doi(text)}"
    elif refs.normalize_openalex(text):
        work_url = f"https://api.openalex.org/works/{refs.normalize_openalex(text)}"
    else:
        raise FetchError("OpenAlex resolution needs a DOI or OpenAlex work ID")
    payload = _json_fetch(fetcher, work_url, timeout=20, max_bytes=5_000_000)
    if not isinstance(payload, Mapping):
        raise FetchError("OpenAlex response is not an object")
    authors = [
        refs._collapse((authorship.get("author") or {}).get("display_name", ""))
        for authorship in payload.get("authorships") or []
        if isinstance(authorship, Mapping)
    ]
    primary = payload.get("primary_location") or {}
    source = primary.get("source") if isinstance(primary, Mapping) else None
    reference = {
        "title": refs._collapse(payload.get("title", "")),
        "authors": [author for author in authors if author],
        "year": refs._year(payload.get("publication_year")),
        "venue": refs._collapse((source or {}).get("display_name", ""))
        if isinstance(source, Mapping) else "",
        "reference_type": refs._collapse(payload.get("type", "")) or "misc",
        "abstract": "",
        "url": refs._collapse(payload.get("id", "")),
        "identifiers": {
            "doi": payload.get("doi"),
            "openalex": payload.get("id"),
        },
        "metadata_status": "resolved_openalex",
        "metadata_provenance": [_provenance("openalex", payload)],
    }
    retracted = payload.get("is_retracted")
    if retracted is not None:
        reference["retraction"] = {
            "status": "retracted" if retracted else "none",
            "provider": "openalex",
            "checked_at": _utc(),
            "notices": [],
        }
    open_access = payload.get("open_access")
    if isinstance(open_access, Mapping):
        reference["open_access"] = {
            "is_oa": bool(open_access.get("is_oa")),
            "status": open_access.get("oa_status"),
            "url": open_access.get("oa_url"),
        }
    return reference


def resolve_arxiv(arxiv_id: str, *, fetcher: Callable[..., Any] = http_get) -> dict[str, Any]:
    payload = _text_fetch(
        fetcher,
        f"https://export.arxiv.org/api/query?id_list={quote(arxiv_id, safe='/')}",
        timeout=20,
        max_bytes=5_000_000,
    )
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", payload, re.I):
        raise FetchError("arXiv response contains DTD/entity declarations")
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FetchError(f"invalid arXiv Atom response: {exc}") from exc
    namespace = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entry = root.find("a:entry", namespace)
    if entry is None:
        raise FetchError("arXiv response has no entry")
    title = refs._collapse(entry.findtext("a:title", default="", namespaces=namespace))
    summary = refs._collapse(entry.findtext("a:summary", default="", namespaces=namespace))
    authors = [
        refs._collapse(author.findtext("a:name", default="", namespaces=namespace))
        for author in entry.findall("a:author", namespace)
    ]
    published = entry.findtext("a:published", default="", namespaces=namespace)
    journal = entry.findtext("arxiv:journal_ref", default=None, namespaces=namespace)
    doi = entry.findtext("arxiv:doi", default=None, namespaces=namespace)
    reference = {
        "title": title,
        "authors": [author for author in authors if author],
        "year": refs._year(published),
        "venue": refs._collapse(journal or "arXiv"),
        "reference_type": "preprint",
        "abstract": summary,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "identifiers": {"arxiv": arxiv_id, "doi": doi},
        "metadata_status": "resolved_arxiv",
        "metadata_provenance": [_provenance("arxiv", payload)],
    }
    return reference


def resolve_europepmc(identifier: Mapping[str, str] | str, *,
                      fetcher: Callable[..., Any] = http_get) -> dict[str, Any]:
    classified = classify_identifier(identifier) if isinstance(identifier, str) else dict(identifier)
    if classified.get("pmcid"):
        query = f"PMCID:{classified['pmcid']}"
    elif classified.get("pmid"):
        query = f"EXT_ID:{classified['pmid']} AND SRC:MED"
    else:
        raise FetchError("Europe PMC resolution needs a PMID or PMCID")
    payload = _json_fetch(
        fetcher,
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query={quote(query)}&format=json&resultType=core",
        timeout=20,
        max_bytes=10_000_000,
    )
    results = ((payload.get("resultList") or {}).get("result") or []) if isinstance(payload, Mapping) else []
    if not results:
        raise FetchError("Europe PMC returned no result")
    item = results[0]
    authors = []
    if item.get("authorString"):
        authors = [part.strip() for part in str(item["authorString"]).split(",") if part.strip()]
    return {
        "title": refs._collapse(item.get("title", "")),
        "authors": authors,
        "year": refs._year(item.get("pubYear")),
        "venue": refs._collapse(item.get("journalTitle", "")),
        "reference_type": "article",
        "abstract": refs._collapse(item.get("abstractText", "")),
        "url": f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}",
        "identifiers": {
            "doi": item.get("doi"),
            "pmid": item.get("pmid"),
            "pmcid": item.get("pmcid"),
        },
        "metadata_status": "resolved_europepmc",
        "metadata_provenance": [_provenance("europepmc", payload)],
    }


def _merge_candidates(candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    provenance = []
    for candidate in candidates:
        if not candidate:
            continue
        for field in ("title", "authors", "year", "venue", "reference_type", "abstract",
                      "url", "license", "open_access", "retraction"):
            value = candidate.get(field)
            if value and not merged.get(field):
                merged[field] = value
        identifiers = refs.normalize_identifiers(candidate.get("identifiers"))
        if identifiers:
            merged["identifiers"] = {**(merged.get("identifiers") or {}), **identifiers}
        provenance.extend(candidate.get("metadata_provenance") or [])
    if provenance:
        merged["metadata_provenance"] = provenance
    merged.setdefault("metadata_status", "resolved_metadata")
    return refs.normalize_reference(merged)


def resolve_reference(identifier: str, *, email: str | None = None,
                      fetcher: Callable[..., Any] = http_get,
                      provider: str | None = None) -> dict[str, Any]:
    """Resolve one identifier to a normalized Reference candidate."""
    if provider:
        external = get_provider(provider)
        metadata = external.get("metadata")
        if not callable(metadata):
            raise FetchError(f"external provider {provider!r} has no metadata capability")
        candidate = metadata(identifier)
        return refs.normalize_reference(candidate)
    classified = classify_identifier(identifier)
    if not classified:
        raise FetchError("identifier must be a DOI, arXiv ID, PMID, PMCID or ISBN")
    candidates: list[Mapping[str, Any]] = []
    errors: list[str] = []
    if classified.get("doi"):
        for resolver in (resolve_crossref, resolve_openalex):
            try:
                candidates.append(resolver(classified["doi"], fetcher=fetcher, email=email)
                                  if resolver is resolve_crossref
                                  else resolver(classified["doi"], fetcher=fetcher))
            except (FetchError, ResearchError) as exc:
                errors.append(f"{resolver.__name__}: {exc}")
    elif classified.get("arxiv"):
        try:
            candidates.append(resolve_arxiv(classified["arxiv"], fetcher=fetcher))
        except (FetchError, ResearchError) as exc:
            errors.append(f"resolve_arxiv: {exc}")
    elif classified.get("pmid") or classified.get("pmcid"):
        try:
            candidates.append(resolve_europepmc(classified, fetcher=fetcher))
        except (FetchError, ResearchError) as exc:
            errors.append(f"resolve_europepmc: {exc}")
    else:
        raise FetchError("only DOI, arXiv, PMID and PMCID metadata resolution is implemented")
    if not candidates:
        raise FetchError("; ".join(errors) or "no provider returned metadata")
    reference = _merge_candidates(candidates)
    reference["resolution_errors"] = errors
    return reference


def _unpaywall_locations(doi: str, email: str | None,
                         fetcher: Callable[..., Any]) -> tuple[list[dict[str, Any]], str | None]:
    if not email:
        return [], "Unpaywall requires an email address; pass email to fetch_preview"
    payload = _json_fetch(
        fetcher,
        f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}?email={quote(email)}",
        timeout=20,
        max_bytes=10_000_000,
    )
    locations = []
    for location in payload.get("oa_locations") or []:
        if not isinstance(location, Mapping):
            continue
        url = location.get("url_for_pdf") or location.get("url")
        if not url:
            continue
        locations.append({
            "provider": "unpaywall",
            "url": url,
            "media_type": "application/pdf",
            "license": location.get("license"),
            "version": location.get("version"),
            "oa_status": location.get("host_type"),
            "source_priority": 4,
        })
    return locations, None


def _candidate(provider: str, url: str, *, media_type: str, priority: int,
               license: str | None = None, version: str | None = None,
               unofficial: bool = False) -> dict[str, Any]:
    return {
        "provider": provider,
        "url": url,
        "media_type": media_type,
        "license": license,
        "version": version,
        "source_priority": priority,
        "unofficial": unofficial,
    }


def preview_fetch(
    reference: Mapping[str, Any] | str,
    *,
    email: str | None = None,
    fetcher: Callable[..., Any] = http_get,
    provider: str | None = None,
) -> dict[str, Any]:
    """Resolve metadata and list open-access artifact candidates; record nothing."""
    resolved = (
        resolve_reference(reference, email=email, fetcher=fetcher)
        if isinstance(reference, str)
        else refs.normalize_reference(reference)
    )
    candidates: list[dict[str, Any]] = []
    notes: list[str] = []
    if provider:
        external = get_provider(provider)
        fulltext = external.get("fulltext")
        if not callable(fulltext):
            raise FetchError(f"external provider {provider!r} has no fulltext capability")
        items = fulltext(resolved) or []
        if isinstance(items, Mapping):
            items = [items]
        for item in items:
            if not isinstance(item, Mapping) or not item.get("url"):
                continue
            candidate = _candidate(
                provider,
                item["url"],
                media_type=item.get("media_type", "application/octet-stream"),
                priority=int(item.get("source_priority", 10)),
                license=item.get("license"),
                version=item.get("version"),
                unofficial=bool(external.get("unofficial", True)),
            )
            candidate["external"] = True
            candidates.append(candidate)
    else:
        identifiers = refs.normalize_identifiers(resolved.get("identifiers"))
        if identifiers.get("arxiv"):
            arxiv_id = identifiers["arxiv"]
            candidates.append(_candidate(
                "arxiv", f"https://arxiv.org/e-print/{quote(arxiv_id, safe='/')}",
                media_type="application/gzip", priority=1, version="source"))
            candidates.append(_candidate(
                "arxiv", f"https://arxiv.org/pdf/{quote(arxiv_id, safe='/')}",
                media_type="application/pdf", priority=3, version="pdf"))
        if identifiers.get("pmcid"):
            candidates.append(_candidate(
                "europepmc",
                "https://www.ebi.ac.uk/europepmc/webservices/rest/"
                f"{quote(identifiers['pmcid'])}/fullTextXML",
                media_type="application/xml", priority=2, version="jats"))
        if identifiers.get("doi"):
            try:
                locations, note = _unpaywall_locations(identifiers["doi"], email, fetcher)
                candidates.extend(locations)
                if note:
                    notes.append(note)
            except (FetchError, ResearchError) as exc:
                notes.append(f"unpaywall: {exc}")
        open_access = resolved.get("open_access")
        if isinstance(open_access, Mapping) and open_access.get("url"):
            candidates.append(_candidate(
                "openalex", str(open_access["url"]), media_type="text/html",
                priority=5, version=open_access.get("status")))
    deduped = []
    seen = set()
    for candidate in sorted(candidates, key=lambda item: item["source_priority"]):
        if candidate["url"] in seen:
            continue
        seen.add(candidate["url"])
        deduped.append(candidate)
    return {
        "reference": resolved,
        "identifiers": refs.normalize_identifiers(resolved.get("identifiers")),
        "candidates": deduped,
        "status": "candidates" if deduped else "no_open_access_candidate",
        "notes": notes,
        "external_providers": provider_status()["providers"],
        "truth_assessed": False,
    }


def _extension(media_type: str | None, url: str, version: str | None) -> str:
    media = (media_type or "").casefold()
    if "pdf" in media:
        return ".pdf"
    if "gzip" in media or "tar" in media:
        return ".tar.gz"
    if "xml" in media:
        return ".xml"
    if "html" in media:
        return ".html"
    suffix = Path(urlsplit(url).path).suffix.casefold()
    if suffix in {".pdf", ".xml", ".html", ".htm", ".tar", ".gz", ".tex", ".zip"}:
        return suffix
    return ".bin"


def search_external(query: str, *, provider: str, limit: int = 10) -> dict[str, Any]:
    """Search through an explicitly named external provider; no recording."""
    if not 1 <= int(limit) <= 100:
        raise FetchError("external search limit must be between 1 and 100")
    external = get_provider(provider)
    search = external.get("search")
    if not callable(search):
        raise FetchError(f"external provider {provider!r} has no search capability")
    items = search(query, limit=int(limit)) or []
    if isinstance(items, Mapping):
        items = [items]
    results = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        normalized = refs.normalize_reference(item)
        normalized["provider"] = provider
        normalized["unofficial"] = bool(external.get("unofficial", True))
        results.append(normalized)
    return {
        "query": query,
        "provider": provider,
        "results": results,
        "count": len(results),
        "unofficial": bool(external.get("unofficial", True)),
        "truth_assessed": False,
        "note": "External search results are unverified candidates; record only selected items.",
    }


def apply_fetch(
    graph: Any,
    *,
    reference_id: str,
    candidate: Mapping[str, Any],
    cache_dir: str | Path,
    fetcher: Callable[..., Any] = http_get,
    max_bytes: int = MAX_FETCH_BYTES,
) -> dict[str, Any]:
    """Download one candidate, store it content-addressed, record a Source."""
    state = graph.state()
    reference = next(
        (node for node in state.get("nodes", []) if node.get("id") == reference_id),
        None,
    )
    if reference is None or reference.get("type") != "Reference":
        raise FetchError("fetch_apply needs an existing Reference id")
    url = candidate.get("url")
    if not isinstance(url, str):
        raise FetchError("candidate has no URL")
    response = _content(fetcher, url, timeout=60, max_bytes=max_bytes)
    content = response.get("content")
    if not isinstance(content, bytes):
        raise FetchError("fetcher returned no byte content")
    if len(content) > max_bytes:
        raise FetchError(f"artifact exceeds {max_bytes} bytes")
    digest = hashlib.sha256(content).hexdigest()
    media_type = response.get("media_type") or candidate.get("media_type")
    if media_type in {"application/octet-stream", "binary/octet-stream"}:
        media_type = candidate.get("media_type") or media_type
    extension = _extension(media_type, url, candidate.get("version"))
    cache = Path(cache_dir).resolve()
    relative = Path("objects") / digest[:2] / f"{digest}{extension}"
    target = cache / relative
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    locator = {
        "kind": "cached_fetch",
        "reference_id": reference_id,
        "sha256": digest,
        "cache_relative": relative.as_posix(),
        "url": response.get("final_url") or url,
        "provider": candidate.get("provider"),
        "media_type": media_type,
        "license": candidate.get("license"),
        "version": candidate.get("version"),
        "retrieved_at": _utc(),
        "size": len(content),
        "external": bool(candidate.get("external")),
        "unofficial": bool(candidate.get("unofficial")),
    }
    existing = next(
        (node for node in state.get("nodes", [])
         if node.get("type") == "Source"
         and isinstance(node.get("locator"), Mapping)
         and node["locator"].get("sha256") == digest
         and node["locator"].get("reference_id") == reference_id),
        None,
    )
    source = existing or graph.record(
        "Source",
        name=f"{reference.get('citation_key') or reference_id}{extension}",
        reference_id=reference_id,
        locator=locator,
    )
    linked = any(
        edge.get("type") == "manifestation_of"
        and edge.get("src") == source["id"]
        and edge.get("tgt") == reference_id
        for edge in state.get("edges", [])
    )
    if not linked:
        graph.link(
            source["id"], "manifestation_of", reference_id,
            provenance={"kind": "fetched_artifact", "sha256": digest},
            rationale="This artifact is a manifestation of the bibliographic work; it is not evidence of any claim.",
        )
    return {
        "source": source,
        "reference": reference,
        "artifact": {
            "sha256": digest,
            "cache_relative": relative.as_posix(),
            "path": str(target),
            "size": len(content),
            "media_type": media_type,
            "url": locator["url"],
            "license": candidate.get("license"),
        },
        "truth_assessed": False,
    }
