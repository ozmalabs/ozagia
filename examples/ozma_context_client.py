#!/usr/bin/env python3
"""Ask the Ozma context provider over the loopback transport, via the client.

This starts a read-only engine server for an already-indexed checkout, builds
``GraphEngineClient``, calls ``task_context`` and prints response metadata
only.  It is session-independent: the client never calls ``hello`` for this
request.  No model, external network or target execution is involved, and the
bearer token is never printed.

Setup (the example does not build or fabricate a graph):

    ozagia index --root /path/to/repo
    ozagia engine token --root /path/to/repo

Usage:

    python examples/ozma_context_client.py --root /path/to/repo \
        --task "find the retry loop"

If the snapshot or token is missing, the example prints the command to run and
exits 2.  The server is bound to an ephemeral loopback port and closed again.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from ozagia import engine_http
from ozagia.engine_client import EngineClientError, GraphEngineClient


def _metadata(result: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    """Provider/client metadata, with no snippets, token or raw rows."""
    return {
        "version": result.get("version"),
        "ok": result.get("ok"),
        "freshness": result.get("freshness"),
        "budget": result.get("budget"),
        "items": len(result.get("items") or []),
        "error": result.get("error"),
        "client": {
            "requests": stats.get("requests"),
            "responses": stats.get("responses"),
            "bytes_in": stats.get("bytes_in"),
            "bytes_out": stats.get("bytes_out"),
            "ops": stats.get("ops"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="indexed repository root")
    parser.add_argument("--task", default="find the retry loop",
                        help="context task; must be non-empty")
    parser.add_argument("--budget-tokens", type=int, default=1500)
    parser.add_argument("--max-items", type=int, default=8)
    parser.add_argument("--allow-stale", action="store_true",
                        help="accept a stale or unverifiable snapshot")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()

    try:
        server = engine_http.create_server(root, port=0)
    except engine_http.EngineHTTPError as exc:
        print(f"skipped: {exc}", file=sys.stderr)
        print("Run `ozagia index --root <repo>` and `ozagia engine token "
              "--root <repo>` first.", file=sys.stderr)
        return 2

    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with GraphEngineClient(server.base_url, root=root) as client:
            result = client.task_context(
                args.task,
                budget_tokens=args.budget_tokens,
                max_items=args.max_items,
                allow_stale=args.allow_stale,
            )
            print(json.dumps(_metadata(result, client.stats()),
                             indent=2, sort_keys=True))
            return 0 if result.get("ok") else 1
    except EngineClientError as exc:
        print(f"engine client failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        server.close_engine()


if __name__ == "__main__":
    raise SystemExit(main())
