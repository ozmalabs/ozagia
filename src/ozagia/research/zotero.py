"""Read-only Zotero local-API import.

Nothing here writes to a Zotero library. A caller may inject ``get_json`` for
tests or for a different local transport.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping
from urllib.parse import quote
from urllib.request import Request, urlopen

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError

DEFAULT_BASE_URL = "http://127.0.0.1:23119"


class ZoteroError(ResearchError):
    """Zotero local API is unavailable or returned an unexpected payload."""


def _default_get_json(url: str, *, timeout: float = 10.0) -> Any:
    request = Request(url, headers={
        "Accept": "application/json",
        "Zotero-API-Version": "3",
        "User-Agent": "ozagia-research/0.1",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(10_000_000).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported as a typed Zotero error
        raise ZoteroError(f"Zotero local API request failed: {exc}") from exc


def _creator_name(creator: Mapping[str, Any]) -> str:
    first = refs._collapse(creator.get("firstName", ""))
    last = refs._collapse(creator.get("lastName", ""))
    name = refs._collapse(creator.get("name", ""))
    if last and first:
        return f"{last}, {first}"
    return last or first or name


def _citation_key(data: Mapping[str, Any]) -> str:
    for key in ("citationKey", "citekey", "citation-key"):
        value = refs._collapse(data.get(key))
        if value:
            return value
    extra = str(data.get("extra") or "")
    match = re.search(r"Citation Key:\s*(\S+)", extra, re.I)
    return match.group(1) if match else ""


def item_to_reference(item: Mapping[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), Mapping) else item
    if not isinstance(data, Mapping):
        raise ZoteroError("Zotero item has no data object")
    key = _citation_key(data)
    identifiers = {
        "doi": data.get("DOI"),
        "pmid": data.get("PMID"),
        "isbn": data.get("ISBN"),
        "arxiv": data.get("arXiv"),
    }
    date = data.get("date", "")
    return {
        "title": refs._collapse(data.get("title", "")),
        "authors": [
            _creator_name(creator) for creator in (data.get("creators") or [])
            if isinstance(creator, Mapping) and _creator_name(creator)
        ],
        "year": refs._year(date),
        "venue": refs._collapse(
            data.get("publicationTitle") or data.get("bookTitle")
            or data.get("conferenceName") or data.get("publisher") or ""),
        "reference_type": _zotero_type(data.get("itemType")),
        "abstract": refs._collapse(data.get("abstractNote", "")),
        "url": refs._collapse(data.get("url", "")),
        "identifiers": refs.normalize_identifiers(identifiers),
        "citation_key": key,
        "citation_keys": [key] if key else [],
        "raw": json.dumps(data, ensure_ascii=False, sort_keys=True),
        "metadata_status": "parsed_zotero",
    }


def _zotero_type(value: Any) -> str:
    return {
        "journalArticle": "article",
        "conferencePaper": "inproceedings",
        "book": "book",
        "bookSection": "chapter",
        "thesis": "thesis",
        "report": "report",
        "preprint": "preprint",
        "dataset": "dataset",
        "computerProgram": "software",
        "webpage": "web",
    }.get(refs._collapse(value), "misc")


def items_to_references(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [item_to_reference(item) for item in items if isinstance(item, Mapping)]


def local_items(
    *,
    base_url: str = DEFAULT_BASE_URL,
    collection: str | None = None,
    limit: int = 100,
    get_json: Callable[..., Any] = _default_get_json,
) -> list[dict[str, Any]]:
    """Read items from the Zotero local API without writing to the library."""
    if not 1 <= int(limit) <= 1000:
        raise ZoteroError("Zotero limit must be between 1 and 1000")
    base = base_url.rstrip("/")
    if collection:
        url = f"{base}/api/users/0/collections/{quote(collection)}/items?limit={int(limit)}"
    else:
        url = f"{base}/api/users/0/items?limit={int(limit)}"
    payload = get_json(url)
    if isinstance(payload, Mapping):
        payload = payload.get("items")
    if not isinstance(payload, list):
        raise ZoteroError("Zotero local API returned no item list")
    return items_to_references(payload)
