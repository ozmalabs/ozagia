# Standalone read-only console

`python -m ozagia.console` serves a small local web console over the reports
that already exist in ozagia. It is a thin shell, not a second agent: it does
not edit files, run tests, apply actions, build the graph, call a model or make
network requests.

```sh
PYTHONPATH=src python -m ozagia.console --root . --port 0 --open
```

The command prints a URL of the form:

```text
http://127.0.0.1:<port>/?token=<random-url-safe-token>
```

Ctrl-C stops the server. With `--open`, the default browser is asked to open
that exact URL. `--port 0` chooses an ephemeral port.

## Options

```text
python -m ozagia.console [--root ROOT] [--host 127.0.0.1] [--port 0]
                         [--token TOKEN] [--open] [--allow-remote]
```

| Option | Meaning |
| --- | --- |
| `--root` | Repository root (default: the current git checkout). It must exist. |
| `--host` | Bind address (default `127.0.0.1`). Loopback is enforced unless overridden. |
| `--port` | Bind port (default `0`, an ephemeral port). |
| `--token` | Explicit token. The default is `secrets.token_urlsafe(32)`. |
| `--open` | Open the token URL in the default browser. |
| `--allow-remote` | Allow a non-loopback bind. It requires an explicit `--token`. |

Only `GET` and `HEAD` are accepted. `POST`, `PUT`, `PATCH`, `DELETE` and
`OPTIONS` get `405`; there is no apply or write endpoint at all.

## Token and cookie authentication

Every `/api/*` request needs either:

- `?token=<token>` in the query string, or
- the cookie set after a successful token use:
  `ozagia_console=<token>; Path=/; HttpOnly; SameSite=Strict`.

The cookie is `HttpOnly` and `SameSite=Strict`, not `Secure` because the
console speaks plain HTTP on loopback. The token is a bearer credential. It is
printed in the start URL and may remain in browser history; do not share it or
paste the URL into another host's browser.

A successful token use on `/` or `/api/*` sets the cookie. A wrong or missing
token on `/api/*` returns `401` with a JSON error envelope. The `/` shell can
be fetched without a token so the page can explain how to authenticate, but it
contains no repository data and every data fetch still requires the token.

The server sends no CORS headers, has no external asset or static-file route,
and answers unknown paths and traversal attempts with `404`. Every response
carries `X-Ozagia-Read-Only: true`. Request logs redact `token=` values.

## Endpoints

All errors use a JSON envelope:

```json
{"ok": false, "error": {"code": "missing_graph", "message": "..."}}
```

Successful endpoints return the source module's report directly (`/api/status`
returns the `core.status` report, for example). The console does not wrap or
rewrite core reports.

| Endpoint | Source | Notes |
| --- | --- | --- |
| `/api/status?top=N` | `core.status.status` | `N` is 0-50. |
| `/api/apertures` | `core.apertures.available_apertures` | Built-ins plus repository overrides. |
| `/api/view?aperture=ID&depth=N&node=ID` | `core.apertures.get_aperture`, `core.view.build_view` | Reads `<state>/graph.json`; a missing graph is `404`, not an empty view. |
| `/api/worktree?base=main` | `core.worktree.check` | Read-only git checks. |
| `/api/resources` | `core.resources.status` | Host memory, disk, load, pressure and history. |
| `/api/testing/plan?base=HEAD&head=REF&task=CLASS` | `core.testing.plan` | Selects tests; never executes them. |
| `/api/research?investigation=ID&view=VIEW` | `core.research_ui.load_state`, `build_research_view` | Views: `library`, `fetch`, `screening`, `evidence`, `consensus`, `citation`. |
| `/api/files?classification=C&status=S&limit=N` | `core.files.list_files`, `orphans`, `ledger_path` | Returns `records` (also `list`), `orphans`, ledger presence and counts. `limit` is 1-1000; orphan rows are capped at 200. |
| `/api/actions` | `core.actions.list_actions` | A JSON list of actions; no apply. |
| `/api/actions?action=NAME&...` | core action preview dispatch | Returns a preview without issuing a confirmation token. |

Action preview arguments are bounded: `name`, `scope` (`project` or `user`),
`max_bytes`, `wait`, `idle_timeout`, `live` and `network`. Core validation
still decides which arguments are valid for a given action. `apply` is not
available.

`/api/actions?action=...` is deliberately not `core.actions.preview`: that function writes
a pending one-time token under the state directory. The console calls the same
canonical argument validation and preview dispatch but skips token issuance. A
preview therefore has `confirmation: null`, `apply_available: false` and a note
explaining that applying is unavailable.

## UI

The one HTML document at `/` is self-contained: inline CSS, inline JavaScript
and no external stylesheet, font, image, script or CDN. It has six tabs:

- **Graph**: aperture, optional depth and node scope; deterministic grid SVG
  (up to 250 nodes) and complete node/edge tables; snapshot freshness,
  provenance, groups, omissions and truncation.
- **Status**: section states, errors, degraded reasons, raw section reports and
  the created-file ledger with orphans.
- **Worktree**: expected branch, drift, dirty counts, operation and findings,
  with filesystem facts in the raw report.
- **Testing**: mode, fallback, selected tests, changed files, graph freshness,
  omissions, cap state and `not_assessed`; it states that no test was executed.
- **Research**: a recorded research view with columns, rows, provenance,
  omissions and `not_assessed`.
- **Actions**: the action list and a preview form with preconditions, risks,
  current state and an explicit "no apply" warning.

Freshness, provenance, omissions and truncation are only ever what the source
reports. The console does not compute freshness, infer missing data or turn a
projection into a truth claim.

## Read-only scope

The console never:

- writes to the repository or its state directory;
- creates `.ozagia/` when it is missing;
- builds, refreshes or edits the graph;
- starts, stops or signals a daemon, service, tunnel or job;
- runs tests or applies an action;
- calls a model, installs packages or makes a network request;
- serves arbitrary files from the repository or filesystem.

It reads existing graph, status, worktree, resource, ledger, research and action
state. The tests assert that representative endpoints do not create
`.ozagia/`.

## Loopback and remote warning

The default and only recommended mode is loopback:

```sh
python -m ozagia.console --root . --port 0
```

A non-loopback host such as `0.0.0.0` is refused unless both
`--allow-remote` and an explicit `--token` are supplied. Remote mode is not a
supported secure deployment: the console has no TLS, no user accounts, no
multi-user authorization and no CSRF-protected mutation surface (there are no
mutations). Do not expose it to an untrusted network. Use an SSH tunnel if a
remote browser is needed.

Even on loopback, the token is a bearer credential. Treat the URL like a
password.

## Limits

| Limit | Value |
| --- | --- |
| Query string | 4 KiB; at most 32 parameters; names 64 characters; values 1 KiB. |
| Request target | 8 KiB. |
| Request body | Rejected with `413`; GET/HEAD only. |
| Rate limit | 240 requests per 10 seconds per client, then `429` with `Retry-After`. |
| View depth | 0-32; aperture caps still apply (typically 250-500 nodes and 900-1500 edges). |
| Status top process groups | 0-50. |
| Ledger records | 1-1000 per request; orphan rows capped at 200. |
| Research rows | Capped by `core.research_ui` and reported in `omitted`. |
| SVG nodes | 250; larger views fall back to the complete table with a warning. |

The server uses `ThreadingHTTPServer` with daemon request threads. A per-request
socket timeout is 20 seconds. `serve_forever` uses a short poll interval and
`shutdown()` closes listening sockets and stops accepting connections.

## Tests

```sh
cd /home/matt/ozmadev/ozagia-wt/standalone-console
PYTHONPATH=src /home/matt/ozmadev/ozagia/.venv/bin/python -m pytest -q tests/test_console.py
/home/matt/ozmadev/ozagia/.venv/bin/ruff check src tests
```

The tests cover token and cookie authentication, unauthorized and traversal
responses, every API happy path with fixtures or monkeypatches, missing graph
and investigation errors, CORS absence, request/query limits, shutdown, the CLI,
and the no-write guarantee.

## Deliberately absent

- Apply buttons or any mutation endpoint. Action preview shows the exact
  operation and risks, then says to use the action CLI when a person decides.
- A bounded task launcher, settings editing, graph building, index refresh,
  test execution, file cleanup and "Open in OpenCode" integration.
- Multi-user accounts, TLS, remote deployment hardening, static-file serving
  and a plugin API.
- A live-reloading or WebSocket transport; tabs fetch on demand.

These are gaps, not hidden behavior. The console is a read-only shell over the
landed reports.
