#!/usr/bin/env python3
"""Release notes for version X: its CHANGELOG.md section, then how to install
and what the bundles pin.

    python scripts/release/notes.py 0.1.0 > notes.md
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def section(version: str) -> str:
    try:
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def main(argv: list[str]) -> int:
    version = argv[1].lstrip("v")
    pins = json.loads((REPO / "release" / "pins.json").read_text(encoding="utf-8"))
    body = section(version) or "See CHANGELOG.md."
    targets = ", ".join(pins["targets"])
    print(f"""{body}

## Install

```sh
curl -fsSL https://ozagia.com/install | bash
# this version:
curl -fsSL https://ozagia.com/install | bash -s -- --version {version}
```

Then `cd` into a repository and run `ozagia`. `ozagia self check --tui` verifies
the install; `ozagia self upgrade` and `ozagia self uninstall` manage it.

Bundles: {targets}. Each holds its own CPython {pins['runtime']['python']}
(python-build-standalone {pins['runtime']['release']}). The installer downloads
OpenCode {pins['opencode']['version']} from its GitHub release and checks the pinned
sha256. `SHA256SUMS` covers every asset here.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
