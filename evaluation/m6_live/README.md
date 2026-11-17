# M6 live map-arm study

This directory is a self-contained, offline-validated harness for one local
Qwen comparison: **plain OpenCode baseline** versus **OpenCode + Ozagia full
MCP maps**, on 20 read-only repository tasks, one repeat each (40 sessions).

No live model call was made when this directory was committed. The default
runner mode is a dry run.

## Files

| File | Purpose |
| --- | --- |
| `tasks.json` | 20 frozen task prompts and short JSON answer contracts. |
| `assertions.json` | Held-out deterministic scoring: required substring groups, forbidden phrases and source evidence. |
| `schedule.json` | Frozen randomized pair/arm order and seed. |
| `run_m6.py` | Gated live runner; dry-run by default. |
| `m6_live_lib.py` | Corpus, schedule, arm-config, scorer and event-parser helpers. |
| `validate_facts.py` | Checks every required/forbidden fact's source literal against the current worktree. |
| `preregistration.md` | Frozen thresholds and decision rules. |
| `../../tests/test_m6_live.py` | Offline tests for scoring, schedule, config, gates and parsing. |

## OpenCode 2.3 config shape

The generated config uses the V2 shapes: the server is under
`mcp.servers.ozagia`, not directly under `mcp`, and permissions are an ordered
rule list (`permissions`, not the V1 `permission` object). Read is allowed;
`edit` (which covers edit, write and patch), `shell`, `webfetch`, `websearch`
and `external_directory` are denied. The maps arm allows the explicit
`ozagia_<tool>` action for each read-only MCP tool and denies each mutating
tool. The MCP server is configured with `codemode: false`, so OpenCode lists
those tools directly; `execute`/Code Mode remains denied, which also keeps its
HTTP `fetch` out of the session. The tests compare the allowed/denied tool
names with the server's `_TOOLS` table and the live preflight repeats that
check. The direct-tool shape was also checked offline against OpenCode 2.3 with
a synthetic OpenAI-compatible endpoint (no live model): the maps config exposed
47 `ozagia_*` read tools and no `execute`, while the baseline exposed only the
built-in read/glob/grep tools.

## Offline checks

```sh
cd <this worktree>
PYTHONPATH=$PWD/src /home/matt/ozmadev/ozagia/.venv/bin/python \
  evaluation/m6_live/validate_facts.py
PYTHONPATH=$PWD/src /home/matt/ozmadev/ozagia/.venv/bin/python \
  evaluation/m6_live/run_m6.py --dry-run --out /tmp/ozagia-m6-dry
PYTHONPATH=$PWD/src /home/matt/ozmadev/ozagia/.venv/bin/python -m pytest -q tests/test_m6_live.py
/home/matt/ozmadev/ozagia/.venv/bin/ruff check src tests evaluation/m6_live
```

The dry run writes `schedule.json` and `manifest.json`, verifies the frozen
corpus and facts, and starts no process, network request, MCP server, daemon or
model session.

## Live gate

Live mode is intentionally not part of the offline checks. It needs **both**
`--live` and `OZAGIA_EVAL_LIVE=1`, a clean checkout at the pinned commit, a
local endpoint serving `vllm/qwen3.8-27b`, at least 3 GiB free, and a person's
approval. See `preregistration.md` for the exact command and thresholds.

## Artifacts

`runs.jsonl` (one content-minimized record per session), `manifest.json`,
`source_manifest.json`, `summary.json` and `report.md` are written under
`--out`. The generated OpenCode config references the local API key as
`{env:M6_API_KEY}`; the value is supplied only in that session's child
environment. The temporary config is deleted when the session ends, and
artifacts carry only a redacted config hash.
