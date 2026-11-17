"""Model graph profiles, distance, graph edges, advisory matching and CLI.

Every test injects or patches the discovery runner. Nothing here runs the real
``opencode`` CLI, contacts a provider or calls a model.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ozagia import models_cli
from ozagia.core import model_graph as G
from ozagia.core import models as M


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_ROOT", "OZAGIA_CONFIG", "OZAGIA_STATE_DIR",
                 "OZAGIA_USER_CONFIG", "OZAGIA_MODELS_DEFAULT",
                 "OZAGIA_MODELS_LOCAL_ONLY"):
        monkeypatch.delenv(name, raising=False)
    return home


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _config(root: Path, text: str) -> Path:
    path = root / "ozagia" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _runner(stdout: str = "", *, returncode: int = 0, stderr: str = "",
            raises: BaseException | None = None):
    def run(argv, timeout):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)

    return run


def _ids(*models: str) -> str:
    return "\n".join(models) + "\n"


def _node(model_id: str = "test/model", **overrides) -> G.ModelNode:
    provider, _, model = model_id.partition("/")
    values = {
        "id": model_id,
        "provider": provider,
        "model": model,
        "family": "test",
        "variant": "default",
        "type": "chat",
        "size_class": "medium",
        "params_total_b": None,
        "params_active_b": None,
        "context": None,
        "input_cost": None,
        "output_cost": None,
        "modalities_in": ("text",),
        "modalities_out": ("text",),
        "capabilities": (),
        "tool_formats": (),
        "local": None,
        "status": M.STATUS_AVAILABLE,
        "source": "profile",
        "confidence": "curated",
        "notes": "",
        "catalog_source": M.SOURCE_OPENCODE,
        "profile_source": "test",
    }
    values.update(overrides)
    return G.ModelNode(**values)


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("model_id", "family", "variant"), [
    ("anthropic/claude-opus-4-1", "claude", "opus"),
    ("anthropic/claude-sonnet-4-5", "claude", "sonnet"),
    ("anthropic/claude-haiku-4-5", "claude", "haiku"),
    ("openai/gpt-5", "gpt", "gpt-5"),
    ("openai/codex-mini-latest", "gpt", "codex-mini"),
    ("google/gemini-2.5-pro", "gemini", "pro"),
    ("google/gemini-2.5-flash", "gemini", "flash"),
    ("ollama/llama-3.3-70b", "llama", "70b"),
    ("ollama/qwen2.5-coder:32b", "qwen", "coder-32b"),
    ("mistral/mistral-large-latest", "mistral", "large"),
    ("deepseek/deepseek-r1", "deepseek", "r1"),
    ("google/gemma-3-27b-it", "gemma", "27b"),
    ("microsoft/phi-4", "phi", "phi-4"),
    ("xai/grok-4", "grok", "default"),
    ("cohere/command-r-plus", "command", "r-plus"),
    ("nvidia/nemotron-4-340b", "nemotron", "340b"),
])
def test_builtin_profiles_cover_common_families(
        tmp_path: Path, model_id: str, family: str, variant: str) -> None:
    root = _root(tmp_path)
    item = G.graph(root, runner=_runner(_ids(model_id)))
    node = item.get(model_id)
    assert node is not None
    assert node.family == family
    assert node.variant == variant
    assert node.source == "profile"
    assert node.confidence == "curated"


def test_unknown_model_is_inferred_without_inventing_metadata(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    model_id = "acme/fable-9000"
    item = G.graph(root, runner=_runner(_ids(model_id)))
    node = item.get(model_id)
    assert node is not None
    assert node.family == "fable"
    assert node.variant == "default"
    assert node.type == "chat"
    assert node.size_class == "unknown"
    assert node.params_total_b is None
    assert node.params_active_b is None
    assert node.context is None
    assert node.input_cost is None
    assert node.output_cost is None
    assert node.capabilities == ()
    assert node.tool_formats == ()
    assert node.source == "inferred"
    assert node.confidence == "inferred"
    assert node.status == M.STATUS_AVAILABLE


def test_open_weight_sizes_are_numeric_and_closed_sizes_stay_unknown(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = G.graph(root, runner=_runner(_ids(
        "ollama/llama-3.3-70b", "anthropic/claude-opus-4-1",
        "openai/gpt-5")))
    llama = item.get("ollama/llama-3.3-70b")
    opus = item.get("anthropic/claude-opus-4-1")
    gpt = item.get("openai/gpt-5")
    assert llama is not None and opus is not None and gpt is not None
    assert llama.params_total_b == 70.0
    assert llama.size_class == "large"
    assert llama.context == 128000
    assert opus.params_total_b is None
    assert opus.size_class == "frontier"
    assert gpt.params_total_b is None
    assert gpt.size_class == "frontier"
    assert gpt.context is not None


def test_config_overrides_families_tasks_weights_and_reports_unknown_keys(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[model_graph]
mystery_key = true

[model_graph.models."openai/gpt-5"]
family = "custom"
context = 1000
capabilities = ["tools"]
mystery = "reported"

[model_graph.families.fable]
patterns = ["*fable*"]
type = "code"
size_class = "small"
notes = "repository family declaration"
mystery_family = "reported"

[model_graph.tasks.diff_review]
capabilities = ["code"]
local_only = true

[model_graph.weights]
family = 0.5
nope = 0.1
""")
    item = G.graph(root, runner=_runner(_ids(
        "openai/gpt-5", "acme/fable-9000")))
    gpt = item.get("openai/gpt-5")
    fable = item.get("acme/fable-9000")
    assert gpt is not None and fable is not None
    assert gpt.family == "custom"
    assert gpt.context == 1000
    assert gpt.capabilities == ("tools",)
    assert gpt.source == "override"
    assert gpt.confidence == "declared"
    assert fable.family == "fable"
    assert fable.type == "code"
    assert fable.size_class == "small"
    assert fable.confidence == "declared"
    assert item.weights["family"] == 0.5
    assert any("mystery" in key for key in item.config.unknown_keys)
    assert any("nope" in key for key in item.config.unknown_keys)
    assert any("mystery_key" in key for key in item.config.unknown_keys)
    assert any("mystery_family" in key for key in item.config.unknown_keys)
    assert any("unknown model_graph key" in warning
               for warning in item.warnings)
    req = G.requirements(root, "diff_review")
    assert req.capabilities == ("code",)
    assert req.local_only is True
    assert req.source == "config:task"


def test_external_metadata_layers_under_explicit_overrides(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[model_graph.models."openai/gpt-5"]
context = 1000
""")
    external = {
        "openai/gpt-5": {
            "context": 500000,
            "input_cost": 0.5,
            "mystery": "reported",
        },
        "acme/fable-9000": {"context": 64000, "params_total_b": 9.0},
    }
    item = G.graph(root, runner=_runner(_ids(
        "openai/gpt-5", "acme/fable-9000")), external=external)
    gpt = item.get("openai/gpt-5")
    fable = item.get("acme/fable-9000")
    assert gpt is not None and fable is not None
    # Explicit repository override beats external metadata.
    assert gpt.context == 1000
    assert gpt.input_cost == 0.5
    # Curated pattern metadata still fills fields external data omits.
    assert gpt.family == "gpt"
    assert "tools" in gpt.capabilities
    assert gpt.source == "override"
    # External-only metadata fills an inferred node without inventing params.
    assert fable.context == 64000
    assert fable.params_total_b == 9.0
    assert fable.size_class == "small"
    assert fable.source == "external"
    assert fable.confidence == "external"
    assert any("mystery" in key for key in item.config.unknown_keys)


def test_config_override_for_unknown_model_is_reported_not_added(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[model_graph.models."acme/not-listed"]
context = 1000
""")
    item = G.graph(root, runner=_runner(_ids("openai/gpt-5")))
    assert item.get("acme/not-listed") is None
    assert any("is not in the catalog" in warning
               for warning in item.warnings)


def test_invalid_known_config_value_is_rejected(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[model_graph.models."openai/gpt-5"]
context = "lots"
""")
    with pytest.raises(G.ModelGraphError, match="context"):
        G.graph(root, runner=_runner(_ids("openai/gpt-5")))


def test_task_profile_exists_for_every_advertised_task(tmp_path: Path) -> None:
    root = _root(tmp_path)
    for task in (
            "code_review", "diff_review", "paper_triage", "repo_scan",
            "test_triage", "pdf_edit", "document_extract", "image_review",
            "research_summarize", "model_classify", "module_build",
            "log_triage", "security_review"):
        req = G.requirements(root, task)
        assert req.task == task
        assert req.capabilities
        assert req.source == "builtin:task"


# --------------------------------------------------------------------------- #
# distance
# --------------------------------------------------------------------------- #
def test_distance_self_is_zero_and_symmetric_in_range() -> None:
    left = _node(
        "a/one", family="gpt", type="reasoning", size_class="large",
        params_total_b=70.0, context=200000, input_cost=1.0,
        output_cost=5.0, capabilities=("tools", "code"),
        tool_formats=("replace",))
    right = _node(
        "b/two", family="claude", type="chat", size_class="small",
        params_total_b=7.0, context=32000, input_cost=3.0, output_cost=15.0,
        capabilities=("tools", "vision"), tool_formats=("mcp",))
    self_result = G.distance(left, left)
    assert self_result["distance"] == 0.0
    assert self_result["similarity"] == 1.0
    assert self_result["coverage"] == 1.0
    forward = G.distance(left, right)
    reverse = G.distance(right, left)
    assert 0.0 <= forward["distance"] <= 1.0
    assert forward["distance"] == reverse["distance"]
    assert forward["similarity"] == reverse["similarity"]
    assert set(forward["components"]) == set(G.FEATURES)
    assert forward["missing"] == []
    assert forward["coverage"] == 1.0


def test_distance_reports_missing_features_and_coverage() -> None:
    bare = _node("a/bare")
    rich = _node(
        "b/rich", context=128000, input_cost=1.0, output_cost=2.0,
        capabilities=("tools", "code"), tool_formats=("replace",))
    result = G.distance(bare, rich)
    assert "context" in result["missing"]
    assert "cost" in result["missing"]
    assert "capabilities" in result["missing"]
    assert "tool_formats" in result["missing"]
    assert "params" in result["missing"]
    assert 0.0 < result["coverage"] < 1.0
    assert result["weights_used"]
    assert "context" not in result["weights_used"]
    assert 0.0 <= result["distance"] <= 1.0


def test_distance_family_and_feature_differences() -> None:
    base = _node("a/base", family="gpt", capabilities=("tools", "code"),
                 tool_formats=("replace", "mcp"))
    same_family = _node("b/same", family="gpt", capabilities=("tools", "code"),
                        tool_formats=("replace", "mcp"))
    other_family = _node("c/other", family="claude",
                         capabilities=("tools", "code"),
                         tool_formats=("replace", "mcp"))
    other_caps = _node("d/caps", family="gpt", capabilities=("vision",),
                       tool_formats=("replace", "mcp"))
    other_tools = _node("e/tools", family="gpt",
                        capabilities=("tools", "code"),
                        tool_formats=("native_pdf",))
    same = G.distance(base, same_family)["distance"]
    different = G.distance(base, other_family)["distance"]
    caps = G.distance(base, other_caps)["distance"]
    tools = G.distance(base, other_tools)["distance"]
    assert same == 0.0
    assert different > same
    assert caps > same
    assert tools > same
    assert 0.0 < G.distance(base, same_family)["coverage"] <= 1.0


def test_distance_custom_weights_only_use_known_features() -> None:
    left = _node("a/one", family="gpt", capabilities=("tools",))
    right = _node("b/two", family="gpt", capabilities=("vision",))
    default = G.distance(left, right)
    weights = {feature: 0.0 for feature in G.FEATURES}
    weights["family"] = 1.0
    family_only = G.distance(left, right, weights)
    assert default["distance"] > 0.0
    assert family_only["distance"] == 0.0
    assert family_only["weights_used"] == {"family": 1.0}
    assert family_only["coverage"] == 1.0
    with pytest.raises(G.ModelGraphError, match="unknown feature"):
        G.distance(left, right, {"not_a_feature": 1.0})
    with pytest.raises(G.ModelGraphError):
        G.distance(left, right, {"family": -1})


# --------------------------------------------------------------------------- #
# graph edges and neighbours
# --------------------------------------------------------------------------- #
def test_graph_is_bounded_and_deterministic(tmp_path: Path) -> None:
    root = _root(tmp_path)
    ids = [f"p/model-{index}" for index in range(8)]
    runner = _runner(_ids(*ids))
    first = G.graph(root, runner=runner, max_nodes=4, max_edges=2)
    second = G.graph(root, runner=runner, max_nodes=4, max_edges=2)
    assert len(first.nodes) == 4
    assert len(first.edges) <= 2
    assert first.truncated is True
    assert first.omitted_nodes == 4
    assert first.omitted_edges > 0
    assert any("truncated" in warning for warning in first.warnings)
    assert [node.id for node in first.nodes] == [
        node.id for node in second.nodes]
    assert [edge.to_dict() for edge in first.edges] == [
        edge.to_dict() for edge in second.edges]
    edge_kinds = {edge.kind for edge in first.edges}
    assert edge_kinds <= {"family", "provider", "similarity"}


def test_graph_contains_family_provider_and_similarity_edges(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = G.graph(root, runner=_runner(_ids(
        "openai/gpt-5", "openai/gpt-5-mini", "anthropic/claude-sonnet-4-5")))
    kinds = {edge.kind for edge in item.edges}
    assert kinds == {"family", "provider", "similarity"}
    for edge in item.edges:
        assert 0.0 <= edge.similarity <= 1.0
        assert edge.source < edge.target
        assert edge.coverage > 0.0


def test_neighbours_are_sorted_deterministically(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _config(root, """
[model_graph.models."p/aaa"]
family = "same"
variant = "v"
context = 1000
capabilities = ["tools"]
tool_formats = ["replace"]

[model_graph.models."p/bbb"]
family = "same"
variant = "v"
context = 1000
capabilities = ["tools"]
tool_formats = ["replace"]

[model_graph.models."p/ccc"]
family = "same"
variant = "v"
context = 1000
capabilities = ["tools"]
tool_formats = ["replace"]
""")
    item = G.graph(root, runner=_runner(_ids("p/aaa", "p/bbb", "p/ccc")))
    first = item.neighbours("p/aaa", limit=2)
    second = item.neighbours("p/aaa", limit=2)
    assert first == second
    assert [entry["id"] for entry in first] == ["p/bbb", "p/ccc"]
    assert all(entry["similarity"] == 1.0 for entry in first)
    assert G.neighbours(root, "p/aaa", limit=2,
                        runner=_runner(_ids("p/aaa", "p/bbb", "p/ccc"))) == first
    with pytest.raises(G.ModelGraphError, match="not in the model graph"):
        item.neighbours("p/missing")


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def _match_root(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "ghost/model"

[models.local]
providers = ["ollama"]

[model_graph.models."ghost/model"]
family = "ghost"
type = "code"
size_class = "medium"
context = 64000
capabilities = ["tools", "code", "structured_output", "long_context"]
tool_formats = ["apply_patch", "replace", "library_call"]
""")
    return root


def _match_catalog_ids() -> str:
    return _ids(
        "openai/gpt-5",
        "openai/gpt-5-mini",
        "anthropic/claude-sonnet-4-5",
        "ollama/qwen2.5-coder:32b",
        "google/gemini-2.5-pro",
        "google/gemma-3-27b-it",
        "acme/fable-9000",
    )


def test_match_ranks_with_reasons_and_reports_unknown_models(
        tmp_path: Path) -> None:
    root = _match_root(tmp_path)
    req = G.requirements(root, "diff_review")
    result = G.match(root, req, limit=3, runner=_runner(_match_catalog_ids()))
    assert result.source == "advisory"
    assert result.selected is not None
    assert result.selected.score <= 1.0
    scores = [candidate.score for candidate in result.candidates]
    assert scores == sorted(scores, reverse=True)
    assert result.selected.reasons
    assert dict(result.selected.components)
    assert any("capabilities" in reason for reason in result.selected.reasons)
    assert any(entry["id"] == "ghost/model" for entry in result.not_assessed)
    assert all(entry["status"] == M.STATUS_UNKNOWN
               for entry in result.not_assessed)


def test_match_hard_filters_local_context_cost_capability_type_modality(
        tmp_path: Path) -> None:
    root = _match_root(tmp_path)
    runner = _runner(_match_catalog_ids())

    local = G.match(root, G.requirements(root, local_only=True),
                    runner=runner)
    assert [candidate.id for candidate in local.candidates] == [
        "ollama/qwen2.5-coder:32b"]
    assert any(entry["id"] == "openai/gpt-5" for entry in local.excluded)

    context = G.match(root, G.requirements(root, min_context=200000),
                      runner=runner)
    assert all(candidate.id != "acme/fable-9000"
               for candidate in context.candidates)
    assert any("context unknown" in " ".join(entry["reasons"])
               for entry in context.excluded)

    cost = G.match(root, G.requirements(root, max_cost=2.0), runner=runner)
    assert "openai/gpt-5-mini" in [c.id for c in cost.candidates]
    assert "openai/gpt-5" in [entry["id"] for entry in cost.excluded]
    assert any("cost unknown" in " ".join(entry["reasons"])
               for entry in cost.excluded)

    caps = G.match(root, G.requirements(root, capabilities=("pdf_native",)),
                   runner=runner)
    assert caps.candidates
    assert all(dict(candidate.components)["capability"] == 1.0
               for candidate in caps.candidates)
    assert "acme/fable-9000" in [entry["id"] for entry in caps.excluded]

    typed = G.match(root, G.requirements(root, type="multimodal"),
                    runner=runner)
    assert all(candidate.type == "multimodal"
               for candidate in typed.candidates)

    image = G.match(root, G.requirements(root, modalities_in=("image",)),
                    runner=runner)
    assert "acme/fable-9000" in [entry["id"] for entry in image.excluded]
    assert any("missing input modalities" in " ".join(entry["reasons"])
               for entry in image.excluded)


def test_match_task_profiles_tool_formats_and_size(tmp_path: Path) -> None:
    root = _match_root(tmp_path)
    runner = _runner(_match_catalog_ids())
    pdf = G.requirements(root, "pdf_edit")
    assert "pdf" in pdf.modalities_in
    assert "native_pdf" in pdf.tool_formats
    assert pdf.min_context == 128000
    result = G.match(root, pdf, limit=5, runner=runner)
    assert result.selected is not None
    assert dict(result.selected.components)["tools"] == 1.0
    for candidate in result.candidates:
        # Every ranked candidate passed the hard capability filter.
        assert candidate.score >= 0.0
    unavailable = [entry["id"] for entry in result.excluded]
    assert "acme/fable-9000" in unavailable

    tool_req = G.requirements(root, tool_formats=("native_pdf",))
    tool_result = G.match(root, tool_req, limit=10, runner=runner)
    assert tool_result.candidates
    for candidate in tool_result.candidates:
        assert any("tool formats" in reason for reason in candidate.reasons)

    size_req = G.requirements(root, size="small")
    size_result = G.match(root, size_req, limit=10, runner=runner)
    assert size_result.candidates
    for candidate in size_result.candidates:
        assert any("size" in reason for reason in candidate.reasons)


def test_match_reports_unavailable_discovery_as_not_assessed(
        tmp_path: Path) -> None:
    root = _match_root(tmp_path)
    runner = _runner(raises=FileNotFoundError("opencode"))
    result = G.match(root, G.requirements(root), runner=runner)
    assert result.discovery.status == M.DISCOVERY_UNAVAILABLE
    assert result.candidates == ()
    assert result.selected is None
    assert any(entry["id"] == "ghost/model" for entry in result.not_assessed)
    assert any("discovery unavailable" in " ".join(entry["reasons"])
               for entry in result.not_assessed)


def test_graph_and_match_do_not_call_models_resolve(tmp_path: Path) -> None:
    root = _match_root(tmp_path)
    runner = _runner(_match_catalog_ids())
    calls: list[object] = []
    original = M.resolve

    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    before = M.resolve(root, "diff_review", runner=runner).to_dict()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(M, "resolve", spy)
        G.graph(root, runner=runner)
        G.match(root, G.requirements(root, "diff_review"), runner=runner)
    after = M.resolve(root, "diff_review", runner=runner).to_dict()
    assert calls == []
    assert before == after


def test_no_network_or_model_calls_for_graph_match_and_tool_fit(
        tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    runner = _runner(_ids("openai/gpt-5"))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no network/model call expected")

    monkeypatch.setattr(M.subprocess, "run", forbidden)
    item = G.graph(root, runner=runner)
    G.match(root, G.requirements(root, "diff_review"), runner=runner)
    G.tool_fit(root, "openai/gpt-5", [
        {"id": "patch", "format": "apply_patch"}], graph_item=item)


# --------------------------------------------------------------------------- #
# tool fit
# --------------------------------------------------------------------------- #
def test_tool_fit_ranks_formats_capabilities_and_preferences(
        tmp_path: Path) -> None:
    root = _root(tmp_path)
    item = G.graph(root, runner=_runner(_ids("google/gemma-3-27b-it")))
    result = G.tool_fit(root, "google/gemma-3-27b-it", [
        {"id": "patch", "format": "apply_patch", "provides": ["code"],
         "preferred_by": ["google/*"]},
        {"id": "pdf", "format": "native_pdf",
         "provides": ["pdf_native"]},
        {"id": "other", "format": "whole_file"},
    ], graph_item=item)
    fits = {tool["id"]: tool for tool in result["tools"]}
    assert result["best"]["id"] == "patch"
    assert fits["patch"]["fit"] > fits["pdf"]["fit"]
    assert fits["pdf"]["fit"] == 0.2
    assert "format apply_patch has a supported near equivalent" in \
        fits["patch"]["reasons"]
    assert fits["other"]["fit"] == 0.6
    matrix = G.tool_matrix(root, [
        {"id": "patch", "format": "apply_patch"},
        {"id": "pdf", "format": "native_pdf"},
    ], graph_item=item)
    assert matrix["pairs"]
    assert matrix["pairs"][0]["fit"] >= matrix["pairs"][-1]["fit"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli_root(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    _config(root, """
[models]
default = "openai/gpt-5"

[models.local]
providers = ["ollama"]
""")
    return root


def test_cli_show_includes_profile_and_keeps_existing_shape(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_match_catalog_ids()))
    assert models_cli.main(["show", "openai/gpt-5", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "id: openai/gpt-5" in text
    assert "references: default" in text
    assert "family: gpt/gpt-5" in text
    assert "size_class: frontier" in text

    assert models_cli.main([
        "show", "openai/gpt-5", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == M.SCHEMA
    assert doc["model"]["id"] == "openai/gpt-5"
    assert doc["model"]["source"] == M.SOURCE_OPENCODE
    assert doc["profile"]["family"] == "gpt"
    assert doc["profile"]["context"] == 400000


def test_cli_graph_json_and_text(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_match_catalog_ids()))
    assert models_cli.main([
        "graph", "--root", str(root), "--max-nodes", "3",
        "--max-edges", "2", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == G.SCHEMA
    assert doc["counts"]["nodes"] == 3
    assert doc["counts"]["edges"] <= 2
    assert doc["truncated"] is True
    assert doc["nodes"][0]["family"]

    assert models_cli.main([
        "graph", "--root", str(root), "--max-nodes", "3",
        "--max-edges", "2"]) == 0
    text = capsys.readouterr().out
    assert "models: 3" in text
    assert "families:" in text
    assert "family=" in text


def test_cli_similar_json_text_and_unknown(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_match_catalog_ids()))
    assert models_cli.main([
        "similar", "openai/gpt-5", "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["model"] == "openai/gpt-5"
    assert doc["similar"]
    assert doc["similar"][0]["id"] == "openai/gpt-5-mini"
    assert doc["similar"][0]["similarity"] >= doc["similar"][-1]["similarity"]

    assert models_cli.main([
        "similar", "openai/gpt-5", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "model: openai/gpt-5" in text
    assert "similarity=" in text

    assert models_cli.main([
        "similar", "openai/missing", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert "not in the model graph" in captured.err


def test_cli_match_json_text_and_filters(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_match_catalog_ids()))
    assert models_cli.main([
        "match", "--task", "diff_review", "--capability", "pdf_native",
        "--tool-format", "native_pdf", "--min-context", "100000",
        "--max-cost", "20", "--size", "large", "--type", "reasoning",
        "--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["task"] == "diff_review"
    assert doc["source"] == "advisory"
    assert doc["selected"]["id"]
    assert "pdf_native" in doc["requirements"]["capabilities"]
    assert "native_pdf" in doc["requirements"]["tool_formats"]
    assert doc["requirements"]["min_context"] == 100000
    assert doc["requirements"]["max_cost"] == 20

    assert models_cli.main([
        "match", "--task", "diff_review", "--local",
        "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert "task: diff_review" in text
    assert "source: advisory" in text
    assert "selected:" in text
    assert "excluded:" in text
    assert "not_assessed:" in text


def test_cli_graph_clamps_and_reports_invalid_values(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr(M, "default_runner", _runner(_match_catalog_ids()))
    assert models_cli.main([
        "match", "--size", "enormous", "--root", str(root)]) == 2
    assert "size" in capsys.readouterr().err
    assert models_cli.main([
        "graph", "--max-nodes", "0", "--root", str(root)]) == 2
    assert "max_nodes" in capsys.readouterr().err


def test_cli_does_not_run_a_real_subprocess(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root = _cli_root(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no subprocess expected")

    monkeypatch.setattr(M.subprocess, "run", forbidden)
    monkeypatch.setattr(M, "default_runner", _runner(_match_catalog_ids()))
    assert models_cli.main([
        "match", "--task", "diff_review", "--root", str(root),
        "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["selected"]
