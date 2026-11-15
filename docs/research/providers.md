# Optional external research providers

Ozagia ships documented open metadata/API providers only: arXiv, Crossref,
OpenAlex, Unpaywall and Europe PMC. It does **not** bundle a Google Scholar
scraper or any other unofficial scraper, and it does not enable one by default.

An external package can add one at arm's length. Registration is not
enablement: `ref_resolve`, `fetch_preview` and `search` use an external provider
only when the caller names it with `provider="..."`. A provider can be marked
`unofficial: true`; results retain that flag on the fetched `Source`.

## Registering a provider

The provider is a mapping, or a zero-argument callable returning one:

```python
# my_scholar_provider.py
def provider():
    return {
        "name": "scholar",
        "unofficial": True,
        "source": "my-scholar-package",
        # Resolve an identifier to a Reference-shaped mapping.
        "metadata": resolve_identifier,
        # Return candidate artifacts for a Reference.
        "fulltext": find_fulltext,
        # Optional: return Reference-shaped search candidates.
        "search": search_scholar,
    }
```

Then opt in explicitly:

```bash
export OZAGIA_RESEARCH_PROVIDERS=my_scholar_provider:provider
```

Installed packages can also declare the `ozagia.research.providers` entry-point
group. Discovery still does not enable a provider; the operation must name it.

Use it:

```text
research_ref_providers
research_external_search   {"query": "graph equalisation", "provider": "scholar"}
research_ref_resolve       {"identifier": "...", "provider": "scholar"}
research_fetch_preview     {"reference_id": "reference-0001", "provider": "scholar"}
```

The operation response includes `external_providers`, and external candidates
carry `unofficial: true`. A provider is responsible for its own authentication,
rate limits and terms. Ozagia still applies the fetch URL guard, size cap,
content hash and graph provenance when a candidate is applied.

## Google Scholar and similar services

Google Scholar has no official public API. Scraping libraries are third-party,
can break without notice, and may conflict with the service's terms or
anti-bot controls. If you choose to use one:

- keep it in a separate package, not in Ozagia;
- check the service's terms and your institution's policy;
- rate-limit and cache aggressively; do not bypass access controls or
  paywalls;
- expect incomplete or unstable metadata, and record the provider and
  retrieval time on every result;
- never treat a search rank or citation count as evidence for a claim.

The same boundary applies to other unofficial sources. An MCP server can also
be used for exploration in OpenCode, but records should be promoted through
Ozagia operations so the graph keeps one provenance chain.
