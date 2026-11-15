"""Adapter indexers' declared inputs (`watch`, `texts`) and the attribute
overlay: after any sequence of changes, `refresh()` gives the graph a fresh
build gives, and `staleness()` says so."""
from __future__ import annotations

import json
import random
import textwrap
from collections import Counter
from pathlib import Path

import pytest

import test_graph_reingest as G
from conftest import write_adapter_config

layout = G.layout
_write, _git, _mem = G._write, G._git, G._mem


def _seed(sid: str, deps: list[str], names: list[str]) -> str:
    return json.dumps({"id": sid, "version": "1", "depends_on": deps,
                       "nodes": [{"local_id": i, "type": "Concept",
                                  "attrs": {"name": n}}
                                 for i, n in enumerate(names)],
                       "rules": []})


@pytest.fixture
def seeded(tmp_path: Path, layout, seed_adapter) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    G._fixture(root, git=False)
    _write(root / "seeds" / "base.json", _seed("base", [], ["root"]))
    _write(root / "seeds" / "demo.json", _seed("demo", ["base"], ["widget"]))
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text() + "\n\ndef test_seed():\n"
                 "    # loads seeds/demo.json\n    assert True\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    dump = tmp_path / "state" / "graph.json"
    G._build(root, dump)
    return root, dump


def _edges(mem) -> set[tuple[str, str, str]]:
    return {(mem.key_of(e["src"]), e["type"], mem.key_of(e["tgt"]))
            for e in mem.edges}


def _settled(mem, root: Path) -> None:
    """refresh() took everything in: nothing pending, and fresh."""
    assert mem.pending_changes() == []
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is False


def test_new_test_file_mentioning_a_seed_gets_its_edges(seeded) -> None:
    root, dump = seeded
    mem = _mem(dump, root)
    _write(root / "tests" / "test_new.py",
           "def test_uses():\n    # reads seeds/base.json\n    assert True\n")
    assert mem.pending_changes() == ["tests/test_new.py"]
    mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 1.0
    assert ("test:tests/test_new.py::test_uses", "mentions_seed",
            "seed:base") in _edges(mem)
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_new_test_in_a_mentioning_file_and_a_removed_mention(seeded) -> None:
    root, dump = seeded
    mem = _mem(dump, root)
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text() + "\n\ndef test_more():\n    assert True\n")
    mem.refresh()
    assert ("test:tests/test_a.py::test_more", "mentions_seed",
            "seed:demo") in _edges(mem)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")
    p.write_text(p.read_text().replace("seeds/demo.json", "the demo seed"))
    mem.refresh()
    assert not any(t == "mentions_seed" and s.startswith("test:tests/test_a.py")
                   for s, t, _g in _edges(mem))
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_edit_without_a_mention_change_skips_the_pass(seeded) -> None:
    root, dump = seeded
    mem = _mem(dump, root)
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text().replace("assert True", "assert 1 == 1"))
    mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 0.0
    p = root / "pkg" / "b.py"
    p.write_text(p.read_text() + "\n# no seeds here\n")
    mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 0.0
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_new_untracked_seed_is_pending_and_stale_until_refreshed(seeded) -> None:
    root, dump = seeded
    mem = _mem(dump, root)
    assert mem.staleness()["stale"] is False
    _write(root / "seeds" / "extra.json", _seed("extra", ["demo"], ["thing"]))
    assert mem.pending_changes() == ["seeds/extra.json"]
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is True       # the digest sees it
    mem.refresh()
    assert mem.node_by_key("seed:extra") is not None
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")
    (root / "seeds" / "extra.json").unlink()
    assert mem.pending_changes() == ["seeds/extra.json"]
    mem.refresh()
    assert mem.node_by_key("seed:extra") is None
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_unparsable_watched_file_is_not_pending_forever(seeded) -> None:
    root, dump = seeded
    mem = _mem(dump, root)
    _write(root / "seeds" / "broken.json", "{not json")
    assert mem.pending_changes() == ["seeds/broken.json"]
    mem.refresh()
    assert mem.pending_changes() == []


# --------------------------------------------------------------------------- #
# the property: random changes, refresh, compare with a fresh build
# --------------------------------------------------------------------------- #
_SEEDS = ["base", "demo", "extra", "more"]


def _mention(rng: random.Random) -> str:
    return rng.choice([f"seeds/{s}.json" for s in _SEEDS] + ["no seed"])


def _random_change(rng: random.Random, root: Path) -> None:
    kind = rng.choice(["seed", "seed", "test", "test", "doc", "py", "rm"])
    if kind == "seed":
        sid = rng.choice(_SEEDS)
        deps = rng.sample([s for s in _SEEDS if s != sid], rng.randint(0, 2))
        names = rng.sample(["a", "b", "c", "d"], rng.randint(0, 3))
        _write(root / "seeds" / f"{sid}.json", _seed(sid, deps, names))
    elif kind == "test":
        rel = rng.choice(["tests/test_a.py", "tests/test_b.py", "tests/test_s.py"])
        names = rng.sample(["test_p", "test_q", "test_r"], rng.randint(1, 3))
        body = "\n\n".join(f"def {n}():\n    # {_mention(rng)}\n    assert True"
                           for n in names)
        _write(root / rel, f'"""Uses {_mention(rng)}."""\n\n\n{body}\n')
    elif kind == "doc":
        _write(root / rng.choice(["README.md", "docs/guide.md"]),
               f"# Doc\n\nSee {_mention(rng)}.\n")
    elif kind == "py":
        _write(root / "pkg" / "c.py", f'"""Reads {_mention(rng)}."""\n\n'
               f"def c{rng.randint(0, 3)}():\n    return 1\n")
    else:
        pool = [p for p in [*(root / "seeds").glob("*.json"),
                            root / "tests" / "test_s.py", root / "pkg" / "c.py"]
                if p.exists() and p.name != "base.json"]
        if pool:
            rng.choice(pool).unlink()


@pytest.mark.parametrize("seed", list(range(4)))
def test_refresh_with_the_seed_adapter_equals_a_fresh_build(seeded, seed) -> None:
    root, dump = seeded
    mem = _mem(dump, root)
    rng = random.Random(seed)
    fresh = root.parent / "fresh.json"
    passes = Counter()
    for _ in range(25):
        _random_change(rng, root)
        mem.refresh()
        passes[mem.last_reingest_timings.get("adapter_pass")] += 1
        _settled(mem, root)
        G._assert_equivalent(mem, root, fresh)
    assert passes[1.0] > 0


# --------------------------------------------------------------------------- #
# the attribute overlay
# --------------------------------------------------------------------------- #
TAGGER = textwrap.dedent('''
    """A test adapter: tags Test nodes from tags.json, and File nodes of the
    tagged tests' files."""
    import json
    from pathlib import Path


    def index_tags(ctx):
        p = Path(ctx.root) / "tags.json"
        if not p.exists():
            return
        tags = json.loads(p.read_text())
        ctx.file_node("tags.json", indexed=False)
        for key, tag in sorted(tags.items()):
            rel, name = key.split(":", 1)[1].split("::", 1)
            nid = ctx.builder.find(("test", rel, name))
            if nid is None:
                continue
            ctx.builder.attrs(nid)["tag"] = tag
            fid = ctx.builder.find(("file", rel))
            if fid is not None:
                a = ctx.builder.attrs(fid)
                a["tagged"] = a.get("tagged", 0) + 1


    def register(registry):
        registry.add_indexer(index_tags, watch=("tags.json",), texts=False)
''')


@pytest.fixture
def tagged(tmp_path: Path, layout, monkeypatch) -> tuple[Path, Path]:
    base = tmp_path / "adapter"
    base.mkdir()
    (base / "ozagia_test_tagger.py").write_text(TAGGER)
    cfg = write_adapter_config(base, ["ozagia_test_tagger:register"])
    monkeypatch.setenv("OZAGIA_CONFIG", str(cfg))
    root = tmp_path / "repo"
    G._fixture(root, git=False)
    _write(root / "tags.json", json.dumps({
        "test:tests/test_a.py::test_one": "fast",
        "test:tests/test_a.py::TestGroup.test_two": "slow"}))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    dump = tmp_path / "state" / "graph.json"
    G._build(root, dump)
    return root, dump


def _attrs(mem, key: str) -> dict:
    return mem.nodes[mem.node_by_key(key)]["attrs"]


def test_adapter_attrs_survive_a_reingest_without_a_pass(tagged) -> None:
    root, dump = tagged
    mem = _mem(dump, root)
    keys = [k for k in (n["key"] for n in mem.nodes.values())
            if k.startswith("test:tests/test_a.py")]
    assert any(_attrs(mem, k).get("tag") == "fast" for k in keys), keys
    assert "file:tests/test_a.py" in mem._adapter_info["attrs"]
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text() + "\n# a change\n")
    mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 0.0
    assert _attrs(mem, "test:tests/test_a.py::test_one")["tag"] == "fast"
    assert _attrs(mem, "file:tests/test_a.py")["tagged"] >= 1
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_adapter_pass_recomputes_and_removes_attrs(tagged) -> None:
    root, dump = tagged
    mem = _mem(dump, root)
    _write(root / "tags.json", json.dumps({"test:tests/test_a.py::test_one": "slow"}))
    mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 1.0
    assert _attrs(mem, "test:tests/test_a.py::test_one")["tag"] == "slow"
    G._assert_equivalent(mem, root, root.parent / "fresh.json")
    _write(root / "tags.json", "{}")
    mem.refresh()
    assert "tag" not in _attrs(mem, "test:tests/test_a.py::test_one")
    assert "tagged" not in _attrs(mem, "file:tests/test_a.py")
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_overlay_round_trips_through_the_snapshot(tagged) -> None:
    root, dump = tagged
    mem = _mem(dump, root)
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text() + "\n# a change\n")
    mem.refresh()
    again = _mem(dump, root)
    assert again._adapter_info["attrs"] == mem._adapter_info["attrs"]
    p.write_text(p.read_text() + "\n# another\n")
    again.refresh()
    G._assert_equivalent(again, root, root.parent / "fresh.json")


def test_removing_or_restoring_a_tagged_test_runs_the_pass(tagged) -> None:
    root, dump = tagged
    mem = _mem(dump, root)
    p = root / "tests" / "test_a.py"
    original = p.read_text()
    p.write_text(original.replace("    def test_two(self):", "    def helper(self):"))
    mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 1.0
    assert _attrs(mem, "file:tests/test_a.py")["tagged"] == 1
    G._assert_equivalent(mem, root, root.parent / "fresh.json")
    p.write_text(original.replace("def test_one():", "def not_a_test():"))
    mem.refresh()
    p.write_text(original)
    mem.refresh()
    assert _attrs(mem, "test:tests/test_a.py::test_one")["tag"] == "fast"
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")


def test_ignored_watched_file_counts_in_the_digest(seeded) -> None:
    root, dump = seeded
    (root / ".gitignore").write_text("seeds/local_*.json\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "ignore")
    G._build(root, dump)
    mem = _mem(dump, root)
    assert mem.staleness()["stale"] is False
    _write(root / "seeds" / "local_x.json", _seed("local_x", [], ["q"]))
    assert mem.pending_changes() == ["seeds/local_x.json"]
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is True
    mem.refresh()
    _settled(mem, root)
    G._assert_equivalent(mem, root, root.parent / "fresh.json")
