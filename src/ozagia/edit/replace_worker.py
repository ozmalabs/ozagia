"""The scan worker of `ozagia.edit.replace`.

`python -m ozagia.edit.replace_worker` reads one scan request (JSON) on
stdin and writes `ozagia.edit.replace.scan`'s result (JSON) on stdout, or
`{"error": ...}`. It runs in its own process so that the caller can
abandon a scan at its deadline: Python's `re` cannot be interrupted from
another thread.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    from ozagia.edit.code_edit import CodeEditError
    from ozagia.edit.replace import scan
    try:
        req = json.loads(sys.stdin.buffer.read())
        res = scan(req)
    except CodeEditError as e:
        res = {"error": str(e)}
    sys.stdout.write(json.dumps(res))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
