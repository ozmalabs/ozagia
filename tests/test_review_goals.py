"""The repo review and the goal system together (W19): every finding type
of a real review converts into a goal, `ops:` keys resolve from the tracked
sources, and review -> goal -> fix -> review -> check reaches `met` only
through a later review that ozagia wrote."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from ozagia.cli import main
from ozagia.core import fingerprint as FP
from ozagia.core import goals as G
from ozagia.core import ops_graph as OG
from ozagia.core import repo_review as RR
from ozagia.core import working_set as W
from ozagia.core.memory import DevMemory
from ozagia.intent.shell import command_heads

from review_fixture import _commit, _git, _graph, _rich_repo, _write, make_repo

WEB = "ops:compose/deploy/compose.yaml#service/web"
DB = "ops:compose/deploy/compose.yaml#service/db"
T_FA = "test:tests/test_a.py::test_fa"
PASSED = "[ozagia] pytest passed: exit 0, 0.1 s, 1 passed, 0 failed, 0 error, 0 skipped\n"
FAILED = ("[ozagia] pytest failed: exit 1, 0.1 s, 0 passed, 1 failed, 0 error, 0 skipped\n"
          f"[ozagia] failed {T_FA}: assert 2 == 1\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path).resolve()


def _memory(root: Path) -> DevMemory:
    return DevMemory(root / ".ozagia" / "graph.json",
                     root / ".ozagia" / "journal.jsonl", root=root)


def _pytest(root: Path, text: str) -> None:
    """A pytest run as the daemon captures it (start time, fingerprint)."""
    command = "python -m pytest -q tests"
    start, tree = time.time(), FP.tree_fingerprint(root)
    run = W.pytest_run(command, command_heads(command), text, workdir=root,
                       root=root, source="captured", env={})
    assert run is not None
    run.ts, run.tree = start, tree
    W.TestRecord(root).apply(run)


def _review_save(root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    assert main(["review-repo", "--root", str(root), "--save", "--json"]) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["generator"] == RR.GENERATOR and packet["tree"]
    return packet


def _finding(packet: dict[str, Any], ftype: str, key: str | None = None) -> dict[str, Any]:
    for f in packet["findings"]:
        if f["type"] == ftype and (key is None or key in f["keys"]):
            return f
    raise AssertionError(f"no {ftype} finding for {key}")


def _check_cli(root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, dict]:
    assert main(["goal", "check", "--root", str(root), "--json"]) == 0
    return {g["id"]: g for g in json.loads(capsys.readouterr().out)["goals"]}


def _mcp(root: Path):
    from ozagia.integrations.mcp.server import DevMemoryServer
    server = DevMemoryServer(_memory(root))

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
        return json.loads(resp["result"]["content"][0]["text"])
    return call


# --------------------------------------------------------------------------- #
# every finding type converts
# --------------------------------------------------------------------------- #


def test_every_finding_type_of_a_real_review_becomes_a_goal(repo: Path) -> None:
    _rich_repo(repo)
    packet = RR.review_repo(repo, max_findings_per_section=500)
    RR.save_packet(repo, packet)
    mem = _memory(repo)
    by_type: dict[str, dict[str, Any]] = {}
    for f in packet["findings"]:
        by_type.setdefault(f["type"], f)
    assert set(by_type) == set(RR.FINDING_TYPES)
    goals = {}
    for ftype, finding in sorted(by_type.items()):
        goal = G.from_review(repo, mem, None, finding["id"])
        rr = goal["criteria"][-1]
        assert rr["kind"] == "review_resolved", ftype
        assert rr["args"]["type"] == ftype and rr["args"]["keys"] == finding["keys"]
        assert rr["args"]["anchor"]["generated_at"] == packet["generated_at"]
        goals[goal["id"]] = ftype
    reports = G.check_goals(repo, mem)
    for report in reports:
        ftype = goals[report["id"]]
        results = {r["kind"]: r for r in report["results"]}
        # Nothing is fixed yet: the finding is still in the packet.
        assert results["review_resolved"]["status"] == "fail", (ftype, results)
        assert report["status"] == "open"
        assert not any("evaluation failed" in r["reason"] for r in report["results"])
    assert G.tracking_goals(repo) == {f["id"]: gid for gid, f in (
        (gid, by_type[t]) for gid, t in goals.items())}


def test_ops_keys_resolve_from_tracked_sources(repo: Path) -> None:
    mem = _memory(repo)
    assert mem.node_by_key(WEB) is None       # the default index has no ops nodes
    goal = G.add_goal(repo, mem, {"title": "pin web", "targets": [WEB], "criteria": [
        f"ops_has:{WEB} pinned=true", f"ops_has:{DB} pinned=true",
        f"ops_has:{DB} restart", "ops_has:ops:image/postgres:16",
        {"kind": "ops_has", "args": {"key": WEB, "attr": "image", "pinned": True}}]})
    assert goal["criteria"][0]["args"] == {"key": WEB, "pinned": True}
    report = G.check_goals(repo, mem, [goal["id"]])[0]
    assert [r["status"] for r in report["results"]] == [
        "fail", "pass", "fail", "pass", "fail"]
    assert report["results"][0]["evidence"]["origin"] == "sources"
    _write(repo, "deploy/compose.yaml", (repo / "deploy" / "compose.yaml").read_text()
           .replace("image: nginx\n", "image: nginx@sha256:" + "a" * 64 + "\n"))
    report = G.check_goals(repo, mem, [goal["id"]])[0]
    assert [r["status"] for r in report["results"]][::4] == ["pass", "pass"]
    for bad, message in ((f"ops_has:{WEB} pinned=false", "pinned must be true"),
                         ({"kind": "ops_has", "args": {"key": WEB, "attr": "image",
                                                       "value": "x", "pinned": True}},
                          "exclude each other"),
                         (f"ops_has:{WEB.replace('web', 'nope')}", "unknown key")):
        with pytest.raises(G.GoalError, match=message):
            G.add_goal(repo, mem, {"title": "x", "criteria": [bad]})


def test_image_pinned_is_shared() -> None:
    assert OG.image_pinned("nginx") is False
    assert OG.image_pinned("docker.io/library/nginx:latest") is False
    assert OG.image_pinned("nginx:1.27") is True
    assert OG.image_pinned("registry:5000/team/app") is False
    assert OG.image_pinned("app@sha256:" + "b" * 64) is True
    assert OG.image_pinned("${IMAGE}") is None
    assert RR._unpinned("nginx") and not RR._unpinned("${IMAGE}")


def test_containerfile_stage_goal_uses_base_image(repo: Path) -> None:
    _write(repo, "deploy/Containerfile", "FROM python\nRUN true\n")
    _commit(repo, "containerfile")
    _graph(repo)
    packet = RR.review_repo(repo, sections=["ops"])
    stage = [f for f in packet["findings"] if f["type"] == "unpinned_image"
             and f["keys"][0].startswith("ops:containerfile/")][0]
    assert stage["suggested_goal"]["criteria"][0]["args"]["attr"] == "base_image"
    RR.save_packet(repo, packet)
    mem = _memory(repo)
    goal = G.from_review(repo, mem, None, stage["id"])
    assert [r["status"] for r in G.check_goals(repo, mem, [goal["id"]])[0]["results"]] \
        == ["fail", "fail"]


# --------------------------------------------------------------------------- #
# review -> goal -> fix -> review -> check
# --------------------------------------------------------------------------- #


def test_review_goal_fix_review_check_is_met(repo: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    _pytest(repo, FAILED)
    first = _review_save(repo, capsys)
    env = _finding(first, "committed_env_file", "file:.env")
    image = _finding(first, "unpinned_image", WEB)
    failing = _finding(first, "failing_test", T_FA)

    # Three ways in: the CLI (packet defaults to latest.json), MCP and the API.
    assert main(["goal", "from-review", "--root", str(repo), env["id"], "--json"]) == 0
    g_env = json.loads(capsys.readouterr().out)["goal"]
    call = _mcp(repo)
    got = call("goal_from_review", {"finding_id": image["id"]})
    assert got["ok"] is True, got
    g_image = got["goal"]
    g_test = G.from_review(repo, _memory(repo), None, failing["id"])
    assert [c["kind"] for c in g_env["criteria"]] == ["review_resolved"]
    assert [c["kind"] for c in g_image["criteria"]] == ["ops_has", "review_resolved"]
    assert [c["kind"] for c in g_test["criteria"]] == ["test_passes", "review_resolved"]
    assert call("goal_from_review", {"finding_id": env["id"]})["ok"] is False

    # dev_review_repo names the goal tracking each top finding.
    brief = call("dev_review_repo", {"max_findings": 50})
    tracked = {f["id"]: f["goal"] for f in brief["top_findings"]}
    assert tracked[env["id"]] == g_env["id"] and tracked[image["id"]] == g_image["id"]
    assert any(goal is None for goal in tracked.values())

    before = _check_cli(repo, capsys)
    assert {gid: g["status"] for gid, g in before.items()} == {
        g_env["id"]: "open", g_image["id"]: "open", g_test["id"]: "open"}

    # The fix: stop tracking .env (it stays on disk), pin the image, make
    # the test pass.
    _git(repo, "rm", "-q", "--cached", ".env")
    _write(repo, ".gitignore", (repo / ".gitignore").read_text() + ".env\n")
    _write(repo, "deploy/compose.yaml", (repo / "deploy" / "compose.yaml").read_text()
           .replace("image: nginx\n", "image: nginx:1.27\n"))
    _commit(repo, "fix review findings")
    _graph(repo)
    _pytest(repo, PASSED)

    # Fixed, but no later review yet: the review criterion cannot pass.
    # The compose file changed since that review (stale); the other goals'
    # files did not, so they read the old review (finding still there).
    mid = _check_cli(repo, capsys)
    # .env was only untracked, so its content is unchanged and the old
    # review still names it.
    for gid, want in ((g_env["id"], "fail"), (g_image["id"], "stale"),
                      (g_test["id"], "fail")):
        statuses = {r["kind"]: r["status"] for r in mid[gid]["results"]}
        assert statuses["review_resolved"] == want, mid[gid]
        assert mid[gid]["status"] == "open"

    second = _review_save(repo, capsys)
    assert second["generated_at"] > first["generated_at"]
    after = _check_cli(repo, capsys)
    for gid in (g_env["id"], g_image["id"], g_test["id"]):
        assert after[gid]["status"] == "met", after[gid]
        rr = [r for r in after[gid]["results"] if r["kind"] == "review_resolved"][0]
        assert rr["evidence"]["state"] == "absent"
        assert rr["evidence"]["basis"] == "content"
        assert rr["evidence"]["generated_at"] == second["generated_at"]

    # An edit to a target after the review makes the evidence stale, which
    # does not regress a met goal.
    _write(repo, "deploy/compose.yaml", (repo / "deploy" / "compose.yaml").read_text()
           + "# note\n")
    later = _check_cli(repo, capsys)
    rr = [r for r in later[g_image["id"]]["results"] if r["kind"] == "review_resolved"][0]
    assert rr["status"] == "stale" and later[g_image["id"]]["status"] == "met"


def test_hand_written_packet_does_not_resolve(repo: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    first = _review_save(repo, capsys)
    env = _finding(first, "committed_env_file", "file:.env")
    goal = G.from_review(repo, _memory(repo), None, env["id"])
    _git(repo, "rm", "-q", ".env")
    _commit(repo, "drop env")
    reviews = RR.reviews_dir(repo)
    latest = reviews / RR.LATEST_NAME

    def forge(**changes: Any) -> dict[str, Any]:
        packet = json.loads(json.dumps(first))
        packet["findings"] = [f for f in packet["findings"] if f["id"] != env["id"]]
        for section in packet["sections"].values():
            section["ids"] = [i for i in section["ids"] if i != env["id"]]
            section["ids_by_type"] = {t: [i for i in ids if i != env["id"]]
                                      for t, ids in section["ids_by_type"].items()}
        packet["tree"] = FP.tree_fingerprint(repo)
        for key, value in changes.items():
            if value is None:
                packet.pop(key, None)
            else:
                packet[key] = value
        latest.write_text(json.dumps(packet), encoding="utf-8")
        return packet

    def review_status() -> tuple[str, str, str]:
        (report,) = G.check_goals(repo, _memory(repo), [goal["id"]])
        rr = [r for r in report["results"] if r["kind"] == "review_resolved"][0]
        return report["status"], rr["status"], rr["reason"]

    # No generator field: not ozagia's review.
    forge(generator=None, generated_at="2099-01-01T00:00:00.000000Z")
    status, rr, reason = review_status()
    assert (status, rr) == ("open", "unknown") and "not written by ozagia" in reason
    # The anchor's generated_at: not a later review.
    forge()
    status, rr, reason = review_status()
    assert (status, rr) == ("open", "unknown") and "not a later review" in reason
    # Another root.
    forge(root="/elsewhere", generated_at="2099-01-01T00:00:00.000000Z")
    status, rr, reason = review_status()
    assert (status, rr) == ("open", "unknown") and "/elsewhere" in reason
    # A packet outside the reviews dir is never read; a link to one is refused.
    outside = repo.parent / "forged.json"
    outside.write_text(latest.read_text(), encoding="utf-8")
    latest.unlink()
    for p in reviews.glob(f"{RR.PACKET_PREFIX}*.json"):
        p.unlink()
    latest.symlink_to(outside)
    status, rr, reason = review_status()
    assert (status, rr) == ("open", "unknown") and "not under the reviews" in reason
    latest.unlink()
    # A real later review resolves it.
    _graph(repo)
    _review_save(repo, capsys)
    assert review_status()[:2] == ("met", "pass")


def test_cli_from_review_takes_packet_and_id(repo: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    first = _review_save(repo, capsys)
    env = _finding(first, "committed_env_file", "file:.env")
    saved = sorted(RR.reviews_dir(repo).glob(f"{RR.PACKET_PREFIX}*.json"))[-1]
    assert main(["goal", "from-review", "--root", str(repo), str(saved), env["id"],
                 "--json"]) == 0
    goal = json.loads(capsys.readouterr().out)["goal"]
    assert goal["source"]["review_packet"].endswith(saved.name)
    assert main(["goal", "from-review", "--root", str(repo), "a", "b", "c"]) == 2
    assert "[PACKET] FINDING_ID" in capsys.readouterr().err
    assert main(["goal", "from-review", "--root", str(repo), "RR-nope"]) == 2
    assert "not in" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# review fixes: ignored files, the file: fallback, hashing, packet keys
# --------------------------------------------------------------------------- #


def test_ignored_file_skip_is_only_for_review_freshness(repo: Path) -> None:
    _write(repo, ".gitignore", (repo / ".gitignore").read_text() + "local.cfg\n")
    _commit(repo, "ignore local.cfg")
    _write(repo, "local.cfg", "a = 1\n")
    fp = FP.tree_fingerprint(repo)
    assert fp is not None and "local.cfg" not in fp["dirty"]
    _write(repo, "local.cfg", "a = 2\n")
    view = FP.TreeView(repo)
    # Test freshness stays fail-closed: content never recorded = changed.
    assert FP.changed_since(view, fp, ["local.cfg"])[0] == ["local.cfg"]
    # Review freshness skips it (an untracked, ignored .env after a fix).
    assert FP.changed_since(view, fp, ["local.cfg"], skip_ignored=True)[0] == []


def test_file_fallback_takes_only_exact_tracked_paths(repo: Path) -> None:
    _write(repo, ".gitignore", (repo / ".gitignore").read_text() + "local.cfg\n")
    _commit(repo, "ignore local.cfg")
    _write(repo, "local.cfg", "a = 1\n")
    assert G._working_file(repo, "file:.env") == ".env"           # tracked
    for key in ("file:local.cfg", "file:.git/config", "file:./.env",
                "file:src//pkg/c.py", "file:../x", "file:.ozagia/graph.json"):
        assert G._working_file(repo, key) is None, key
    mem = _memory(repo)
    with pytest.raises(G.GoalError, match="unknown key"):
        G.add_goal(repo, mem, {"title": "x", "targets": ["file:local.cfg"],
                               "criteria": [f"symbol_exists:sym:{'src/pkg/c.py'}::lonely"]})


def test_content_hash_streams_and_caps(repo: Path, monkeypatch) -> None:
    digest, ev = G.content_hash(repo, None, "file:.env")
    assert digest and ev["basis"] == "file bytes"
    monkeypatch.setattr(G, "MAX_HASH_BYTES", 4)
    digest, ev = G.content_hash(repo, None, "file:.env")
    assert digest is None and ev["error"] == "unreadable"


def test_packet_keys_need_canonical_paths() -> None:
    assert G._safe_key_path("file:src/pkg/c.py")
    assert G._safe_key_path("ops:compose/deploy/compose.yaml#service/web")
    assert G._safe_key_path("package:src/pkg/a")
    for key in ("file:../../tmp/x", "file:/etc/passwd", "file:./a.py",
                "file:a//b", "sym:../x.py::f"):
        assert not G._safe_key_path(key), key
