"""Deterministic offline pieces of the M6 live map-arm study.

This module has no model, network, daemon or subprocess operation.  The live
runner imports it; the tests and ``validate_facts.py`` import it directly.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import re
import statistics
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
TASKS_PATH = ROOT / "tasks.json"
ASSERTIONS_PATH = ROOT / "assertions.json"
SCHEDULE_PATH = ROOT / "schedule.json"

STUDY = "ozagia-m6-live-maps-local-qwen"
SEED = 20261118
ARMS = ("baseline", "maps")
TASK_COUNT = 20
REPEATS = 1
SESSION_COUNT = TASK_COUNT * len(ARMS) * REPEATS
MODEL = "vllm/qwen3.8-27b"
MODEL_ID = "qwen3.8-27b"
ENDPOINT = "http://127.0.0.1:18020/v1"
LIVE_ENV = "OZAGIA_EVAL_LIVE"
MCP_SERVER_NAME = "ozagia"
MCP_MODULE = "ozagia.integrations.mcp.server"
API_KEY_ENV = "M6_API_KEY"

# Filled from the frozen files before the first live run.  A mismatch is a
# refusal, never a warning.
TASKS_SHA256 = "3656e215ab16a5ecc319ca1bf9799ee61306a96b52e473da732f9a70cdaa8568"
ASSERTIONS_SHA256 = "0f265f150e27dd806538397a3cad4cf1bf9c117e3ea54b3b16fae6c4d13b04d3"
SCHEDULE_SHA256 = "2204d2c41b0ce6c8b6318ff16cbca08d32710d50988696826d8cd54a16ff4ac8"

ORDERING = (
    "shuffle the 20 task ids with random.Random(20261118), then shuffle the "
    "two arms inside each pair"
)

# Read-only MCP tools that the maps arm may call.  The list is intentionally
# explicit: the default permission is deny, and every mutating tool is denied
# by name too.  Tests compare both lists with the server's static _TOOLS table.
MCP_READ_TOOLS = (
    "dev_architecture",
    "dev_ast",
    "dev_brief",
    "dev_callers",
    "dev_callees",
    "dev_capabilities",
    "dev_changed",
    "dev_code_search",
    "dev_context",
    "dev_definition",
    "dev_diagnostics",
    "dev_document_symbols",
    "dev_environment",
    "dev_file",
    "dev_files_explain",
    "dev_files_list",
    "dev_hover",
    "dev_impact",
    "dev_lsp_status",
    "dev_maps",
    "dev_neighbours",
    "dev_node",
    "dev_outline",
    "dev_query",
    "dev_recent",
    "dev_references",
    "dev_rename_preview",
    "dev_repo_map",
    "dev_scip",
    "dev_search",
    "dev_stats",
    "dev_symbol",
    "dev_symbol_callees",
    "dev_symbol_callers",
    "dev_symbol_graph_impact",
    "dev_symbol_impact",
    "dev_symbol_resolve",
    "dev_tests",
    "dev_tools",
    "dev_who",
    "federation_check",
    "federation_node",
    "federation_search",
    "federation_status",
    "goal_list",
    "goal_show",
    "job_list",
    "job_log",
)

MCP_DENIED_TOOLS = (
    "dev_edit",
    "dev_key_edit",
    "dev_record",
    "dev_refresh",
    "dev_review_repo",
    "dev_replace",
    "dev_rule",
    "dev_secrets",
    "dev_seed",
    "dev_session_finish",
    "dev_session_list",
    "dev_session_next",
    "dev_session_record",
    "dev_session_start",
    "dev_session_state",
    "dev_span_edit",
    "dev_working_set",
    "goal_add",
    "goal_check",
    "goal_claim",
    "goal_from_review",
    "job_start",
    "job_stop",
    "job_wait",
)

# OpenCode 2.3 / V2 uses an ordered permission-rule list.  The last matching
# rule wins, so the catch-all deny is first and every allowed action follows.
PERMISSION_BASE: tuple[tuple[str, str], ...] = (
    ("*", "deny"),
    ("read", "allow"),
    ("grep", "allow"),
    ("glob", "allow"),
    # In V2 `edit` is the single action for edit, write and patch, and `shell`
    # replaces V1's `bash` action.
    ("edit", "deny"),
    ("write", "deny"),
    ("patch", "deny"),
    ("shell", "deny"),
    ("bash", "deny"),
    ("webfetch", "deny"),
    ("websearch", "deny"),
    ("external_directory", "deny"),
)

MCP_ENV_ALLOWLIST = (
    "OZAGIA_ROOT",
    "OZAGIA_DUMP",
    "OZAGIA_JOURNAL",
    "OZAGIA_DAEMON_SOCK",
    "OZAGIA_STATE_DIR",
    "OZAGIA_AUTOBUILD",
    "M6_API_KEY",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
)


class CorpusError(ValueError):
    """The frozen task/assertion corpus is malformed or does not match."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_corpus(root: str | Path = ROOT) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(root)
    tasks = read_json(root / "tasks.json")
    assertions = read_json(root / "assertions.json")
    validate_corpus(tasks, assertions)
    return tasks, assertions


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_corpus(tasks: dict[str, Any], assertions: dict[str, Any]) -> None:
    if not isinstance(tasks, dict) or tasks.get("version") != 1:
        raise CorpusError("tasks.json must be an object with version 1")
    if tasks.get("study") != STUDY:
        raise CorpusError(f"tasks.json study must be {STUDY!r}")
    rows = tasks.get("tasks")
    if not isinstance(rows, list) or len(rows) != TASK_COUNT:
        raise CorpusError(f"tasks.json must contain exactly {TASK_COUNT} tasks")
    ids: list[str] = []
    categories: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CorpusError("every task must be an object")
        tid = row.get("id")
        if not _nonempty_str(tid) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", tid):
            raise CorpusError(f"invalid task id: {tid!r}")
        if tid in ids:
            raise CorpusError(f"duplicate task id: {tid}")
        ids.append(tid)
        if not _nonempty_str(row.get("prompt")) or not _nonempty_str(row.get("category")):
            raise CorpusError(f"{tid}: prompt and category are required")
        categories[row["category"]] = categories.get(row["category"], 0) + 1
        contract = row.get("answer_contract")
        if not isinstance(contract, dict) or contract.get("format") != "json":
            raise CorpusError(f"{tid}: answer_contract.format must be json")
        keys = contract.get("keys")
        if (not isinstance(keys, list) or not keys
                or not all(_nonempty_str(key) for key in keys)):
            raise CorpusError(f"{tid}: answer_contract.keys must be a non-empty list")
    if sum(categories.values()) != TASK_COUNT:
        raise CorpusError("task category count is wrong")
    if len([name for name in categories if name in {"self_graph", "model_api", "evidence",
                                                    "preflight", "actions", "prefetch",
                                                    "ocelot"}]) != 7:
        raise CorpusError("the seven named special-task categories must be present")
    if not isinstance(assertions, dict) or assertions.get("version") != 1:
        raise CorpusError("assertions.json must be an object with version 1")
    specs = assertions.get("tasks")
    if not isinstance(specs, dict) or set(specs) != set(ids):
        raise CorpusError("assertions.json must cover exactly the task ids")
    for tid, spec in specs.items():
        if not isinstance(spec, dict):
            raise CorpusError(f"{tid}: assertion entry must be an object")
        required = spec.get("required")
        forbidden = spec.get("forbidden")
        if not isinstance(required, list) or not required:
            raise CorpusError(f"{tid}: required must be a non-empty list")
        if not isinstance(forbidden, list) or not forbidden:
            raise CorpusError(f"{tid}: forbidden must be a non-empty list")
        seen: set[str] = set()
        for group in required:
            if not isinstance(group, dict):
                raise CorpusError(f"{tid}: every required group must be an object")
            gid = group.get("id")
            if not _nonempty_str(gid) or gid in seen:
                raise CorpusError(f"{tid}: bad or duplicate required id {gid!r}")
            seen.add(gid)
            alternatives = group.get("any")
            if (not isinstance(alternatives, list) or not alternatives
                    or not all(_nonempty_str(item) for item in alternatives)):
                raise CorpusError(f"{tid}/{gid}: any must be a non-empty string list")
            if not isinstance(group.get("evidence"), list) or not group["evidence"]:
                raise CorpusError(f"{tid}/{gid}: evidence paths are required")
            if not _nonempty_str(group.get("source")):
                raise CorpusError(f"{tid}/{gid}: source literal is required")
        for item in forbidden:
            if not isinstance(item, dict) or not _nonempty_str(item.get("phrase")):
                raise CorpusError(f"{tid}: every forbidden item needs a phrase")
            contrary = item.get("contrary")
            if not isinstance(contrary, dict) or not _nonempty_str(contrary.get("path")) \
                    or not _nonempty_str(contrary.get("source")):
                raise CorpusError(f"{tid}: every forbidden item needs a contrary")


def task_ids(tasks: dict[str, Any]) -> list[str]:
    return [row["id"] for row in tasks["tasks"]]


def corpus_hashes(root: str | Path = ROOT) -> dict[str, str]:
    root = Path(root)
    return {"tasks_sha256": file_sha256(root / "tasks.json"),
            "assertions_sha256": file_sha256(root / "assertions.json")}


def make_schedule(ids: Iterable[str]) -> list[dict[str, Any]]:
    ordered = list(ids)
    if len(ordered) != TASK_COUNT or len(set(ordered)) != TASK_COUNT:
        raise CorpusError("schedule needs exactly 20 distinct task ids")
    rng = random.Random(SEED)
    rng.shuffle(ordered)
    sessions: list[dict[str, Any]] = []
    for pair_no, tid in enumerate(ordered, 1):
        arms = list(ARMS)
        rng.shuffle(arms)
        for pair_position, arm in enumerate(arms, 1):
            sessions.append({
                "ordinal": len(sessions) + 1,
                "pair_id": tid,
                "pair_order": pair_no,
                "pair_position": pair_position,
                "task_id": tid,
                "arm": arm,
                "repeat": 1,
            })
    return sessions


def schedule_document(tasks: dict[str, Any]) -> dict[str, Any]:
    sessions = make_schedule(task_ids(tasks))
    return {
        "study": STUDY,
        "seed": SEED,
        "pairs": TASK_COUNT,
        "sessions": SESSION_COUNT,
        "repeats": REPEATS,
        "ordering": ORDERING,
        "schedule": sessions,
    }


def schedule_bytes(tasks: dict[str, Any]) -> bytes:
    return (canonical_json(schedule_document(tasks)) + "\n").encode("utf-8")


def schedule_hash(tasks: dict[str, Any]) -> str:
    return sha256_bytes(schedule_bytes(tasks))


def verify_frozen_corpus(root: str | Path = ROOT) -> None:
    """Refuse if the committed corpus or schedule differs from the frozen hashes."""
    root = Path(root)
    actual = corpus_hashes(root)
    if actual["tasks_sha256"] != TASKS_SHA256:
        raise CorpusError(
            f"tasks.json hash mismatch: {actual['tasks_sha256']} != {TASKS_SHA256}")
    if actual["assertions_sha256"] != ASSERTIONS_SHA256:
        raise CorpusError(
            f"assertions.json hash mismatch: {actual['assertions_sha256']} != {ASSERTIONS_SHA256}")
    tasks = read_json(root / "tasks.json")
    generated = schedule_bytes(tasks)
    if sha256_bytes(generated) != SCHEDULE_SHA256:
        raise CorpusError("regenerated schedule does not match the frozen schedule hash")
    frozen = root / "schedule.json"
    if not frozen.is_file():
        raise CorpusError("schedule.json is missing")
    if frozen.read_bytes() != generated:
        raise CorpusError("schedule.json does not match the regenerated schedule")
    errors = validate_schedule_shape(schedule_document(tasks)["schedule"])
    if errors:
        raise CorpusError("; ".join(errors))


def validate_schedule_shape(sessions: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if len(sessions) != SESSION_COUNT:
        errors.append(f"expected {SESSION_COUNT} sessions, got {len(sessions)}")
    if [row.get("ordinal") for row in sessions] != list(range(1, SESSION_COUNT + 1)):
        errors.append("ordinals are not 1..40")
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for row in sessions:
        by_pair.setdefault(str(row.get("pair_id")), []).append(row)
    if len(by_pair) != TASK_COUNT:
        errors.append(f"expected {TASK_COUNT} pairs, got {len(by_pair)}")
    for pair, rows in by_pair.items():
        arms = sorted(str(row.get("arm")) for row in rows)
        if arms != sorted(ARMS):
            errors.append(f"{pair}: arms are {arms}")
        if sorted(int(row.get("pair_position", 0)) for row in rows) != [1, 2]:
            errors.append(f"{pair}: pair positions are not 1,2")
        if len({row.get("pair_order") for row in rows}) != 1:
            errors.append(f"{pair}: rows disagree on pair order")
    if len({row.get("pair_order") for row in sessions}) != TASK_COUNT:
        errors.append("pair order is not unique per pair")
    return errors


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def extract_json(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        return value
    return None


def answer_haystack(answer: str) -> str:
    parts = [answer]
    value = extract_json(answer)
    if value is not None:
        try:
            parts.append(canonical_json(value))
        except (TypeError, ValueError):
            pass
    return _normalise("\n".join(part for part in parts if part))


def score_answer(answer: str, spec: dict[str, Any]) -> dict[str, Any]:
    haystack = answer_haystack(answer)
    matched: list[str] = []
    missing: list[str] = []
    for group in spec["required"]:
        found = any(_normalise(item) in haystack for item in group["any"])
        (matched if found else missing).append(str(group["id"]))
    forbidden_hits = [str(item["phrase"]) for item in spec["forbidden"]
                      if _normalise(str(item["phrase"])) in haystack]
    return {
        "success": not missing and not forbidden_hits,
        "required_total": len(spec["required"]),
        "required_matched": matched,
        "required_missing": missing,
        "forbidden_total": len(spec["forbidden"]),
        "forbidden_hits": forbidden_hits,
    }


def contains_literal(text: str, literal: str) -> bool:
    if literal.casefold() in text.casefold():
        return True
    return _normalise(literal) in _normalise(text)


def validate_facts(root: str | Path = REPO_ROOT,
                   corpus_dir: str | Path = ROOT) -> dict[str, Any]:
    root = Path(root)
    tasks, assertions = load_corpus(corpus_dir)
    errors: list[str] = []
    checked_required = 0
    checked_forbidden = 0
    for tid in task_ids(tasks):
        spec = assertions["tasks"][tid]
        for group in spec["required"]:
            checked_required += 1
            found = False
            for rel in group["evidence"]:
                path = root / rel
                if not path.is_file():
                    errors.append(f"{tid}/{group['id']}: missing evidence {rel}")
                    continue
                if contains_literal(path.read_text(encoding="utf-8"), str(group["source"])):
                    found = True
                    break
            if not found:
                errors.append(
                    f"{tid}/{group['id']}: source literal not found in "
                    f"{', '.join(group['evidence'])}: {group['source']!r}")
        for item in spec["forbidden"]:
            checked_forbidden += 1
            contrary = item["contrary"]
            path = root / str(contrary["path"])
            if not path.is_file():
                errors.append(f"{tid}: missing contrary file {contrary['path']}")
            elif not contains_literal(path.read_text(encoding="utf-8"),
                                      str(contrary["source"])):
                errors.append(
                    f"{tid}: contrary source not found in {contrary['path']}: "
                    f"{contrary['source']!r}")
    return {
        "ok": not errors,
        "tasks": len(tasks["tasks"]),
        "required_groups": checked_required,
        "forbidden_phrases": checked_forbidden,
        "errors": errors,
    }


def mcp_permission_action(tool: str) -> str:
    """OpenCode V2 names an MCP permission action `<server>_<tool>`."""
    return f"{MCP_SERVER_NAME}_{tool}"


def mcp_permission_names() -> list[str]:
    return [mcp_permission_action(tool) for tool in (*MCP_READ_TOOLS, *MCP_DENIED_TOOLS)]


def _permission_rule(action: str, effect: str) -> dict[str, str]:
    return {"action": action, "resource": "*", "effect": effect}


def arm_permissions(arm: str) -> list[dict[str, str]]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    rules = [_permission_rule(action, effect) for action, effect in PERMISSION_BASE]
    if arm == "maps":
        # The MCP server is configured with `codemode: false`, so its tools are
        # native tools. `execute` stays denied: Code Mode would expose an HTTP
        # fetch that the read-only task set must not have.
        for tool in MCP_READ_TOOLS:
            rules.append(_permission_rule(mcp_permission_action(tool), "allow"))
        for tool in MCP_DENIED_TOOLS:
            rules.append(_permission_rule(mcp_permission_action(tool), "deny"))
    return rules


def permission_effect(config: dict[str, Any], action: str) -> str | None:
    """The last matching rule's effect for a single action, or None."""
    effect = None
    for rule in config.get("permissions", []):
        if isinstance(rule, dict) and rule.get("action") in {action, "*"}:
            effect = str(rule.get("effect"))
    return effect


def build_arm_config(arm: str, *, api_key: str, workdir: str | Path,
                     tool_python: str, source_src: str | Path,
                     model: str = MODEL, endpoint: str = ENDPOINT) -> dict[str, Any]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("an API key is required")
    root = str(workdir)
    tool_python_s = str(tool_python)
    source_src_s = str(source_src)
    doc: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "providers": {
            "vllm": {
                "name": "Local Qwen",
                "package": "aisdk:@ai-sdk/openai-compatible",
                # The key itself stays in the child environment; the config
                # file contains only a reference, so a crashed run cannot leave
                # a credential in an artifact.
                "settings": {"baseURL": endpoint,
                             "apiKey": f"{{env:{API_KEY_ENV}}}"},
                "models": {
                    MODEL_ID: {
                        "name": "Qwen3.8-27B local",
                        "limit": {"context": 131072, "output": 16384},
                        "capabilities": {
                            "input": ["text"],
                            "output": ["text"],
                            "tools": True,
                        },
                        "body": {"chat_template_kwargs": {"enable_thinking": False}},
                    },
                },
            },
        },
        "plugins": [],
        "mcp": {},
        "permissions": arm_permissions(arm),
    }
    if arm == "maps":
        env = {
            "OZAGIA_ROOT": root,
            "OZAGIA_DUMP": f"{root}/.ozagia/graph.json",
            "OZAGIA_JOURNAL": f"{root}/.ozagia/journal.jsonl",
            "OZAGIA_DAEMON_SOCK": f"{root}/.ozagia/daemon.sock",
            "OZAGIA_STATE_DIR": f"{root}/.ozagia",
            "OZAGIA_AUTOBUILD": "0",
            # Do not inherit the model credential into the MCP server process.
            "M6_API_KEY": "",
            "PYTHONPATH": source_src_s,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        doc["mcp"] = {"servers": {MCP_SERVER_NAME: {
            "type": "local",
            "command": [tool_python_s, "-m", MCP_MODULE,
                        "--root", root, "--mode", "coding", "--surface", "full"],
            "environment": env,
            # Direct tools, not Code Mode: Code Mode's execute tool also
            # exposes HTTP fetch, which this read-only study denies.
            "codemode": False,
        }}}
    validate_arm_config(doc, arm, endpoint=endpoint)
    return doc


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    clean = copy.deepcopy(config)
    try:
        clean["providers"]["vllm"]["settings"]["apiKey"] = "<redacted>"
    except (KeyError, TypeError) as exc:
        raise ValueError("config has no vllm apiKey to redact") from exc
    return clean


def config_digest(config: dict[str, Any]) -> str:
    return sha256_text(canonical_json(redacted_config(config)))


def arm_shape_config(arm: str, *, model: str = MODEL,
                     endpoint: str = ENDPOINT) -> dict[str, Any]:
    return build_arm_config(
        arm,
        api_key="<redacted>",
        workdir="<WORKDIR>",
        tool_python="<TOOL_PYTHON>",
        source_src="<SOURCE_SRC>",
        model=model,
        endpoint=endpoint,
    )


def arm_shape_digest(arm: str) -> str:
    return config_digest(arm_shape_config(arm))


def validate_arm_config(config: dict[str, Any], arm: str, *,
                        endpoint: str = ENDPOINT) -> None:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    if config.get("model") != MODEL:
        raise ValueError("unexpected model id")
    if config.get("plugins") != []:
        raise ValueError("plugins must be empty")
    if not isinstance(config.get("mcp"), dict):
        raise ValueError("mcp must be an object")
    permissions = config.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        raise ValueError("permissions must be a non-empty rule list")
    for rule in permissions:
        if not isinstance(rule, dict) or set(rule) != {"action", "resource", "effect"}:
            raise ValueError("every permission rule needs action/resource/effect")
        if not all(isinstance(value, str) and value for value in rule.values()):
            raise ValueError("permission rule values must be non-empty strings")
        if rule["effect"] not in {"allow", "deny"}:
            raise ValueError("permission effects must be allow or deny")
        if rule["resource"] != "*":
            raise ValueError("this study uses only the * resource")
    if permissions[0] != {"action": "*", "resource": "*", "effect": "deny"}:
        raise ValueError("the first permission rule must deny by default")
    for action in ("read", "grep", "glob"):
        if permission_effect(config, action) != "allow":
            raise ValueError(f"read action is not allowed: {action}")
    for action in ("edit", "write", "patch", "shell", "bash", "webfetch",
                   "websearch", "external_directory"):
        if permission_effect(config, action) != "deny":
            raise ValueError(f"action is not denied: {action}")
    provider = (config.get("providers") or {}).get("vllm") or {}
    settings = provider.get("settings") or {}
    if settings.get("baseURL") != endpoint or not settings.get("apiKey"):
        raise ValueError("provider endpoint or apiKey is missing")
    if set(provider.get("models") or {}) != {MODEL_ID}:
        raise ValueError("provider model allowlist mismatch")
    if arm == "baseline":
        if config["mcp"]:
            raise ValueError("baseline must not configure MCP")
        if any(rule["action"].startswith(f"{MCP_SERVER_NAME}_") for rule in permissions):
            raise ValueError("baseline must not carry MCP permissions")
        if permission_effect(config, "execute") == "allow":
            raise ValueError("baseline must not enable execute")
    else:
        if set(config["mcp"]) != {"servers"}:
            raise ValueError("maps must use the V2 mcp.servers shape")
        servers = config["mcp"]["servers"]
        if not isinstance(servers, dict) or set(servers) != {MCP_SERVER_NAME}:
            raise ValueError("maps must configure exactly the ozagia MCP server")
        server = servers[MCP_SERVER_NAME]
        if server.get("type") != "local":
            raise ValueError("the ozagia MCP server must be local")
        if server.get("disabled") not in (None, False) or "enabled" in server:
            raise ValueError("the ozagia MCP server must not be disabled")
        command = server.get("command")
        if not isinstance(command, list) or len(command) < 9:
            raise ValueError("MCP command shape is wrong")
        if command[1:4] != ["-m", MCP_MODULE, "--root"]:
            raise ValueError("MCP command must use the ozagia server module and --root")
        if command[5:] != ["--mode", "coding", "--surface", "full"]:
            raise ValueError("MCP mode/surface must be coding/full")
        env = server.get("environment")
        if not isinstance(env, dict) or set(env) - set(MCP_ENV_ALLOWLIST):
            raise ValueError("MCP environment has a non-allowlisted key")
        if env.get("OZAGIA_AUTOBUILD") != "0":
            raise ValueError("MCP must not autobuild the snapshot")
        if env.get("M6_API_KEY") != "":
            raise ValueError("the model credential must not enter the MCP environment")
        if server.get("codemode") is not False:
            raise ValueError("the MCP server must expose direct tools (codemode false)")
        if permission_effect(config, "execute") != "deny":
            raise ValueError("Code Mode execute must stay denied")
        for tool in MCP_READ_TOOLS:
            if permission_effect(config, mcp_permission_action(tool)) != "allow":
                raise ValueError(f"MCP read tool is not allowed: {tool}")
        for tool in MCP_DENIED_TOOLS:
            if permission_effect(config, mcp_permission_action(tool)) != "deny":
                raise ValueError(f"MCP mutating tool is not denied: {tool}")


def assert_secret_absent(value: Any, secret: str) -> None:
    if not secret:
        return
    if secret in canonical_json(value) or secret in json.dumps(value, default=str):
        raise ValueError("a credential value is present in the artifact")


def _bounded(text: Any, limit: int = 500) -> str:
    value = str(text)
    return value[:limit]


def _event_text(event: dict[str, Any]) -> str:
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    for value in (part.get("text"), event.get("text")):
        if isinstance(value, str):
            return value
    return ""


def _error_detail(event: dict[str, Any]) -> str:
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    detail = event.get("error") or part.get("error") or event.get("message") or event
    if isinstance(detail, str):
        return _bounded(detail)
    try:
        return _bounded(canonical_json(detail))
    except (TypeError, ValueError):
        return _bounded(detail)


def _mcp_tool_short(tool: str) -> str | None:
    """The MCP tool name for either V2 (``ozagia_dev_maps``) or the older
    ``mcp__ozagia__dev_maps`` spelling; None for a non-MCP tool."""
    v2 = f"{MCP_SERVER_NAME}_"
    if tool.startswith(v2):
        return tool[len(v2):]
    old = f"mcp__{MCP_SERVER_NAME}__"
    if tool.startswith(old):
        return tool[len(old):]
    return None


_MCP_CODE_RE = re.compile(
    r"""tools\s*(?:\[\s*["']ozagia["']\s*\]|\.\s*ozagia)\s*\.\s*([A-Za-z_]\w*)\s*\("""
)
_DENIAL_WORDS = ("permission", "denied", "not allowed", "blocked", "cannot access")


def parse_opencode_events(stdout: str, *, secret: str | None = None) -> dict[str, Any]:
    """Extract content-free totals and the final assistant message.

    The event shape is the one documented for ``opencode run --format json``:
    ``step_finish`` carries ``part.tokens`` and ``part.cost``, ``tool_use``
    carries ``part.tool`` and ``part.state``, and ``text`` carries
    ``part.text`` and ``part.messageID``.
    """
    if secret:
        stdout = stdout.replace(secret, "<redacted>")
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)

    steps = 0
    cost = 0.0
    tokens_in = tokens_out = tokens_cached = tokens_cache_write = 0
    tokens_seen = tokens_out_seen = cache_seen = cache_write_seen = False
    tool_calls = 0
    tools_by_name: dict[str, int] = {}
    mcp_calls = 0
    maps_calls = 0
    capabilities_calls = 0
    texts_by_message: dict[str, list[str]] = {}
    text_order: list[str] = []
    last_message: str | None = None
    errors: list[str] = []
    denials: list[dict[str, str]] = []

    for event in events:
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        message = part.get("messageID") or event.get("messageID") or event.get("message_id")
        if kind == "step_finish":
            steps += 1
            if isinstance(message, str):
                last_message = message
            if isinstance(part.get("cost"), (int, float)) and not isinstance(part.get("cost"), bool):
                cost += float(part["cost"])
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            if isinstance(tokens.get("input"), int) and not isinstance(tokens.get("input"), bool):
                tokens_in += int(tokens["input"])
                tokens_seen = True
            if isinstance(tokens.get("output"), int) and not isinstance(tokens.get("output"), bool):
                tokens_out += int(tokens["output"])
                tokens_out_seen = True
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            if isinstance(cache.get("read"), int) and not isinstance(cache.get("read"), bool):
                tokens_cached += int(cache["read"])
                cache_seen = True
            if isinstance(cache.get("write"), int) and not isinstance(cache.get("write"), bool):
                tokens_cache_write += int(cache["write"])
                cache_write_seen = True
        elif kind == "tool_use":
            tool_calls += 1
            tool = str(part.get("tool") or part.get("name") or event.get("tool") or "?")
            tools_by_name[tool] = tools_by_name.get(tool, 0) + 1
            short = _mcp_tool_short(tool)
            if short is not None:
                mcp_calls += 1
                if short == "dev_maps":
                    maps_calls += 1
                elif short == "dev_capabilities":
                    capabilities_calls += 1
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inp = state.get("input") if isinstance(state.get("input"), dict) else {}
            code = inp.get("code")
            if isinstance(code, str):
                for match in _MCP_CODE_RE.finditer(code):
                    name = match.group(1)
                    mcp_calls += 1
                    tools_by_name[f"mcp__{MCP_SERVER_NAME}__{name}"] = (
                        tools_by_name.get(f"mcp__{MCP_SERVER_NAME}__{name}", 0) + 1)
                    if name == "dev_maps":
                        maps_calls += 1
                    elif name == "dev_capabilities":
                        capabilities_calls += 1
            status = str(state.get("status") or part.get("status") or "").casefold()
            detail = state.get("error") or state.get("output") or part.get("error") or ""
            if isinstance(detail, dict):
                detail = canonical_json(detail)
            detail_s = str(detail)
            if status in {"error", "denied", "blocked"} or part.get("is_error") is True:
                if any(word in detail_s.casefold() for word in _DENIAL_WORDS):
                    denials.append({"tool": tool, "reason": _bounded(detail_s, 200)})
        elif kind == "text":
            text = _event_text(event)
            if not text:
                continue
            key = str(message) if message else ""
            texts_by_message.setdefault(key, []).append(text)
            text_order.append(key)
        elif kind in {"permission", "permission.denied", "permission_denied"}:
            detail = _error_detail(event)
            tool = str(part.get("tool") or event.get("tool") or "?")
            denials.append({"tool": tool, "reason": detail[:200]})
        elif kind == "error":
            errors.append(_error_detail(event))

    if last_message and texts_by_message.get(last_message):
        answer = "\n".join(texts_by_message[last_message])
    elif texts_by_message:
        last_key = text_order[-1] if text_order else ""
        answer = "\n".join(texts_by_message.get(last_key, []))
    else:
        answer = ""
    driver_error = errors[0] if errors else (None if steps else "opencode produced no finished step")
    errors = errors[:20]
    unique_denials: list[dict[str, str]] = []
    seen_denials: set[tuple[str, str]] = set()
    for item in denials:
        key = (item["tool"], item["reason"])
        if key not in seen_denials and len(unique_denials) < 50:
            seen_denials.add(key)
            unique_denials.append(item)
    return {
        "steps": steps,
        "answer": answer,
        "tokens": {
            "input": tokens_in if tokens_seen else None,
            "output": tokens_out if tokens_out_seen else None,
            "cache_read": tokens_cached if cache_seen else None,
            "cache_write": tokens_cache_write if cache_write_seen else None,
            "cost_usd": round(cost, 6) if steps else None,
        },
        "tool_calls": tool_calls,
        "tools_by_name": dict(sorted(tools_by_name.items())),
        "mcp_calls": mcp_calls,
        "maps_calls": maps_calls,
        "capabilities_calls": capabilities_calls,
        "permission_denials": unique_denials,
        "errors": errors,
        "driver_error": driver_error,
    }


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _timing_totals(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [float(row[field]) for row in rows
              if isinstance(row.get(field), (int, float))
              and not isinstance(row.get(field), bool)]
    return {
        "total_ms": int(round(sum(values) * 1000)) if values else None,
        "median_ms": median_or_none([value * 1000 for value in values]),
        "known_runs": len(values),
        "unknown_runs": len(rows) - len(values),
    }


def summarise_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    per_arm: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        selected = [row for row in runs if row.get("arm") == arm]
        verified = [row for row in selected if row.get("verified") is True]
        known_times = [row["setup_inclusive_ms"] for row in verified
                       if isinstance(row.get("setup_inclusive_ms"), int)]
        per_arm[arm] = {
            "sessions": len(selected),
            "completed": sum(1 for row in selected if row.get("status") == "completed"),
            "valid": sum(1 for row in selected if row.get("valid") is True),
            "verified_successes": len(verified),
            "score_successes": sum(1 for row in selected if (row.get("score") or {}).get("success") is True),
            "timeouts": sum(1 for row in selected if row.get("timed_out") is True),
            "setup_errors": sum(1 for row in selected if row.get("status") == "setup_error"),
            "driver_errors": sum(1 for row in selected if row.get("status") == "driver_error"),
            "tokens": {
                field: (
                    sum(int(row.get("tokens", {}).get(field)) for row in selected
                        if isinstance(row.get("tokens", {}).get(field), int))
                    if any(isinstance(row.get("tokens", {}).get(field), int)
                           for row in selected)
                    else None
                )
                for field in ("input", "output", "cache_read", "cache_write")
            },
            "token_unknown_runs": {
                field: sum(1 for row in selected
                           if not isinstance(row.get("tokens", {}).get(field), int))
                for field in ("input", "output", "cache_read", "cache_write")
            },
            "tool_calls": sum(row.get("tool_calls") or 0 for row in selected),
            "mcp_calls": sum(row.get("mcp_calls") or 0 for row in selected),
            "maps_calls": sum(row.get("maps_calls") or 0 for row in selected),
            "permission_denials": sum(len(row.get("permission_denials") or []) for row in selected),
            "median_verified_setup_inclusive_ms": median_or_none([float(v) for v in known_times]),
            "setup_ms": _timing_totals(selected, "setup_s"),
            "driver_ms": _timing_totals(selected, "driver_s"),
            "setup_inclusive_ms": _timing_totals(selected, "setup_inclusive_s"),
        }
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for row in runs:
        tid = str(row.get("task_id"))
        arm = str(row.get("arm"))
        by_task.setdefault(tid, {})[arm] = row
    pairs: list[dict[str, Any]] = []
    both: list[tuple[int, int]] = []
    for tid in by_task:
        arms = by_task[tid]
        base = arms.get("baseline")
        maps = arms.get("maps")
        order = min((int(row.get("pair_order", 0)) for row in arms.values()
                     if isinstance(row.get("pair_order"), int)), default=0)
        pair = {"task_id": tid, "pair_order": order,
                "baseline": bool(base and base.get("verified") is True),
                "maps": bool(maps and maps.get("verified") is True)}
        if base and maps and pair["baseline"] and pair["maps"]:
            b = _int_or_none(base.get("setup_inclusive_ms"))
            m = _int_or_none(maps.get("setup_inclusive_ms"))
            if b is not None and m is not None:
                pair["baseline_ms"] = b
                pair["maps_ms"] = m
                pair["delta_ms"] = m - b
                both.append((b, m))
            else:
                pair["reason"] = "verified but time unknown"
        else:
            pair["reason"] = "one or both arms failed, timed out or were invalid"
        pairs.append(pair)
    pairs.sort(key=lambda item: (item["pair_order"], item["task_id"]))
    b_median = median_or_none([float(b) for b, _m in both])
    m_median = median_or_none([float(m) for _b, m in both])
    improvement = None
    if b_median and m_median is not None and b_median > 0:
        improvement = round((b_median - m_median) * 1000 / b_median)
    baseline_successes = per_arm["baseline"]["verified_successes"]
    maps_successes = per_arm["maps"]["verified_successes"]
    return {
        "schema": "ozagia.m6_live.summary/1",
        "study": STUDY,
        "arms": per_arm,
        "paired_tasks": len(pairs),
        "pairs": pairs,
        "pairs_both_verified": len(both),
        "pairs_not_both_verified": [pair for pair in pairs if not (
            pair["baseline"] and pair["maps"] and "baseline_ms" in pair)],
        "primary": {
            "rule": "maps verified task successes >= baseline verified task successes",
            "baseline_verified_successes": baseline_successes,
            "maps_verified_successes": maps_successes,
            "non_inferior": maps_successes >= baseline_successes,
            "caveat": "20 tasks, one repeat; no general capability claim",
        },
        "secondary": {
            "rule": "maps median setup-inclusive time-to-verified-outcome at least 10% lower than baseline over pairs where both verified",
            "baseline_median_ms": b_median,
            "maps_median_ms": m_median,
            "improvement_per_mille": improvement,
            "at_least_10_percent_lower": bool(improvement is not None and improvement >= 100),
            "pairs_used": len(both),
            "failed_or_unknown_pairs": [pair for pair in pairs if not (
                pair["baseline"] and pair["maps"] and "baseline_ms" in pair)],
            "insufficient_pairs": len(both) < 10,
        },
    }
