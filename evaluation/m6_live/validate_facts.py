#!/usr/bin/env python3
"""Validate the held-out fact sources for the M6 live corpus.

Every required group names an evidence file and the exact source literal that
supports it. Every forbidden phrase names a contrary source literal. This
script checks those literals against the current worktree; it makes no model,
network, daemon or subprocess call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import m6_live_lib as L  # noqa: E402


def main() -> int:
    report = L.validate_facts()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
