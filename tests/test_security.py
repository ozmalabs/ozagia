"""Tests for the offline security scan, secret gate and CLI."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia import security_cli as CLI
from ozagia.config import ConfigError
from ozagia.core import code_review as CR
from ozagia.core import repo_scan as RS
from ozagia.core import security as S
from ozagia.core import worktree as W

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_KEY = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
OPENAI_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
SECRETS = (AWS_KEY, GITHUB_KEY, OPENAI_KEY)


def test_redact_text_redacts_unquoted_secret_assignments():
    value = "unquoted-secret-value-12345"
    redacted = S.redact_text(f"token: {value}")
    assert value not in redacted
    assert "<redacted:" in redacted


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str, check: bool = True
         ) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Security Tester",
        "GIT_AUTHOR_EMAIL": "security@example.com",
        "GIT_COMMITTER_NAME": "Security Tester",
        "GIT_COMMITTER_EMAIL": "security@example.com",
    })
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env,
                          check=False)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(path, "config", "user.name", "Security Tester")
    _git(path, "config", "user.email", "security@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".ozagia/\n", encoding="utf-8")
    (path / "ozagia").mkdir()
    (path / "ozagia" / "resources.toml").write_text(
        "enabled = false\n", encoding="utf-8")
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    return path


def _commit(repo: Path, message: str = "feat: change") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stage(repo: Path, rel: str, text: str) -> None:
    _write(repo / rel, text)
    _git(repo, "add", rel)


def _graph(repo: Path) -> Path:
    path = repo / ".ozagia" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes = [
        {"id": 0, "type": "Repo", "key": "repo:",
         "attrs": {"root": str(repo.resolve())}},
        {"id": 1, "type": "File", "key": "file:src/app.py",
         "attrs": {"path": "src/app.py"}},
        {"id": 2, "type": "Symbol", "key": "sym:src/app.py::run",
         "attrs": {"file": "src/app.py", "qualname": "run", "name": "run",
                   "start_line": 6, "end_line": 10,
                   "decorators": ["app.post('/run')"]}},
        {"id": 3, "type": "Symbol", "key": "sym:src/app.py::helper",
         "attrs": {"file": "src/app.py", "qualname": "helper",
                   "name": "helper", "start_line": 12, "end_line": 18,
                   "decorators": []}},
    ]
    path.write_text(json.dumps({
        "version": 2, "nodes": nodes,
        "edges": [{"src": 1, "type": "defines", "tgt": 2},
                  {"src": 1, "type": "defines", "tgt": 3}],
        "next_id": 4, "files": {},
    }), encoding="utf-8")
    return path


APP = """\
from flask import Flask, request
import subprocess, requests

app = Flask(__name__)

@app.post("/run")
def run():
    cmd = request.form["cmd"]
    subprocess.run(cmd, shell=True)
    return "ok"

def helper():
    requests.get("https://example.com", verify=False)
    SECRET_KEY = "0123456789abcdef"
    AWS = "AKIAIOSFODNN7EXAMPLE"
    GITHUB = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    OPENAI = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
"""


@pytest.fixture
def sec_repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "src" / "app.py", APP)
    _write(repo / "pyproject.toml", """\
[project]
name = "fixture"
version = "0.1"
dependencies = ["flask==2.0.1", "requests>=2"]
""")
    _graph(repo)
    _git(repo, "checkout", "-q", "-b", "feat/security")
    return repo


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel.startswith(".git/"):
                continue
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _finding(report: dict[str, Any], rule: str) -> dict[str, Any]:
    return next(item for item in report["findings"]
                if item["source"].get("rule") == rule)


def _all_text(report: dict[str, Any]) -> str:
    return S.report_json(report) + S.format_report(report)


# --------------------------------------------------------------------------- #
# configuration: legacy and versioned [security]
# --------------------------------------------------------------------------- #
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)


def _security_repo_config(root: Path, text: str) -> Path:
    path = root / "ozagia" / "config.toml"
    _write(path, text)
    return path


def _security_legacy_config(root: Path, text: str) -> Path:
    path = root / ".ozagia.toml"
    _write(path, text)
    return path


def test_security_config_defaults_without_a_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    config = S.load_config(tmp_path)
    assert {
        "enabled": config["enabled"],
        "tools": config["tools"],
        "allow_network": config["allow_network"],
        "fail_on": config["fail_on"],
        "exclude": config["exclude"],
        "max_findings": config["max_findings"],
        "block_secrets": config["block_secrets"],
        "secret_allowlist": config["secret_allowlist"],
        "finding_allowlist": config["finding_allowlist"],
    } == {
        "enabled": True,
        "tools": [],
        "allow_network": False,
        "fail_on": "high",
        "exclude": list(S.DEFAULT_EXCLUDES),
        "max_findings": S.DEFAULT_MAX_FINDINGS,
        "block_secrets": True,
        "secret_allowlist": [],
        "finding_allowlist": [],
    }
    assert set(config["sources"].values()) == {"default"}
    assert config["files"]["legacy"] is None
    assert config["files"]["repo"] == str(
        (tmp_path / "ozagia" / "config.toml").resolve())
    assert config["error"] is None and config["errors"] == []


def test_security_config_reads_the_versioned_file_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    path = _security_repo_config(tmp_path, """
[security]
enabled = false
tools = ["bandit", "gitleaks"]
allow_network = true
fail_on = "medium"
exclude = ["vendor/**", "generated/**"]
max_findings = 5000
block_secrets = false
secret_allowlist = ["path:tests/fixtures/**", "rule:generic_high_entropy"]
finding_allowlist = ["sha256:0123456789abcdef"]
""")
    config = S.load_config(tmp_path)
    assert config["enabled"] is False
    assert config["tools"] == ["bandit", "gitleaks"]
    assert config["allow_network"] is True
    assert config["fail_on"] == "medium"
    assert config["exclude"] == ["vendor/**", "generated/**"]
    # The versioned value is validated then capped like the legacy loader.
    assert config["max_findings"] == S.MAX_FINDINGS
    assert config["block_secrets"] is False
    assert config["secret_allowlist"] == [
        "path:tests/fixtures/**", "rule:generic_high_entropy"]
    assert config["finding_allowlist"] == ["sha256:0123456789abcdef"]
    assert config["sources"] == {key: "repo" for key in S.SECURITY_KEYS}
    assert config["files"] == {"legacy": None, "repo": str(path.resolve())}
    assert config["error"] is None and config["repo_error"] is None
    S.load_config_strict(tmp_path)  # no error


def test_security_config_reads_the_legacy_file_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    path = _security_legacy_config(tmp_path, """
[security]
enabled = false
tools = ["bandit", 3]
allow_network = true
fail_on = "MEDIUM"
exclude = ["vendor/**", 4]
max_findings = 5000
block_secrets = false
secret_allowlist = ["rule:generic_high_entropy", 5]
finding_allowlist = ["sha256:0123456789abcdef", 6]
""")
    config = S.load_config(tmp_path)
    # The legacy loader stays forgiving: bad entries are dropped, not fatal.
    assert config["enabled"] is False
    assert config["tools"] == ["bandit"]
    assert config["allow_network"] is True
    assert config["fail_on"] == "medium"
    assert config["exclude"] == ["vendor/**"]
    assert config["max_findings"] == S.MAX_FINDINGS
    assert config["block_secrets"] is False
    assert config["secret_allowlist"] == ["rule:generic_high_entropy"]
    assert config["finding_allowlist"] == ["sha256:0123456789abcdef"]
    assert config["sources"] == {key: "legacy" for key in S.SECURITY_KEYS}
    assert config["files"] == {"legacy": str(path.resolve()),
                               "repo": str(
                                   (tmp_path / "ozagia" / "config.toml").resolve())}
    assert config["error"] is None


def test_security_config_reads_the_env_and_state_legacy_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    root = tmp_path / "repo"
    root.mkdir()
    env_file = tmp_path / "other" / "cfg.toml"
    _write(env_file, "[security]\nenabled = false\n")
    monkeypatch.setenv("OZAGIA_CONFIG", str(env_file))
    config = S.load_config(root)
    assert config["enabled"] is False
    assert config["sources"]["enabled"] == "legacy"
    assert config["files"]["legacy"] == str(env_file.resolve())

    monkeypatch.delenv("OZAGIA_CONFIG")
    monkeypatch.setenv("OZAGIA_STATE_DIR", "state")
    state_file = root / "state" / "config.toml"
    _write(state_file, "[security]\nblock_secrets = false\n")
    config = S.load_config(root)
    assert config["block_secrets"] is False
    assert config["sources"]["block_secrets"] == "legacy"
    assert config["files"]["legacy"] == str(state_file.resolve())
    assert config["error"] is None


def test_security_config_legacy_wins_per_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    legacy = _security_legacy_config(tmp_path, """
[security]
enabled = true
tools = ["bandit"]
fail_on = "low"
max_findings = 50
""")
    repo = _security_repo_config(tmp_path, """
[security]
enabled = false
tools = ["gitleaks"]
fail_on = "never"
max_findings = 500
exclude = ["vendor/**"]
block_secrets = false
secret_allowlist = ["path:tests/fixtures/**"]
finding_allowlist = ["sha256:fedcba9876543210"]
""")
    config = S.load_config(tmp_path)
    assert config["enabled"] is True
    assert config["tools"] == ["bandit"]
    assert config["fail_on"] == "low"
    assert config["max_findings"] == 50
    assert config["exclude"] == ["vendor/**"]
    assert config["block_secrets"] is False
    assert config["secret_allowlist"] == ["path:tests/fixtures/**"]
    assert config["finding_allowlist"] == ["sha256:fedcba9876543210"]
    assert config["sources"] == {
        "enabled": "legacy", "tools": "legacy", "fail_on": "legacy",
        "max_findings": "legacy", "exclude": "repo",
        "block_secrets": "repo", "secret_allowlist": "repo",
        "finding_allowlist": "repo",
        "allow_network": "default",
    }
    assert config["files"] == {"legacy": str(legacy.resolve()),
                               "repo": str(repo.resolve())}
    assert config["error"] is None and config["errors"] == []


def test_security_config_malformed_legacy_file_is_forgiving(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    legacy = _security_legacy_config(tmp_path, "[security\n")
    repo = _security_repo_config(tmp_path, """
[security]
enabled = false
""")
    config = S.load_config(tmp_path)
    assert config["enabled"] is False
    assert config["legacy_error"] is not None
    assert str(legacy.resolve()) in config["legacy_error"]
    assert config["repo_error"] is None
    assert config["error"] == config["legacy_error"]
    # Strict mode still only rejects a broken versioned config.
    S.load_config_strict(tmp_path)

    repo.unlink()
    config = S.load_config(tmp_path)
    assert config["enabled"] is True
    assert config["legacy_error"] is not None
    assert config["sources"]["enabled"] == "default"


def test_security_config_malformed_versioned_file_is_reported(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    legacy = _security_legacy_config(tmp_path, """
[security]
enabled = false
""")
    repo = _security_repo_config(tmp_path, "[security\n")
    config = S.load_config(tmp_path)
    assert config["enabled"] is False  # the valid legacy value still applies
    assert config["legacy_error"] is None
    assert config["repo_error"] is not None
    assert str(repo.resolve()) in config["repo_error"]
    assert config["error"] == config["repo_error"]
    assert config["errors"]
    with pytest.raises(ConfigError, match="ozagia/config.toml"):
        S.load_config_strict(tmp_path)
    assert legacy.exists()


@pytest.mark.parametrize("key, toml_value, message", [
    ("enabled", '"yes"', "true or false"),
    ("tools", '"bandit"', "list"),
    ("allow_network", "1", "true or false"),
    ("fail_on", '"loud"', "one of"),
    ("exclude", '"tests/**"', "list"),
    ("max_findings", "0", "between"),
    ("block_secrets", '"false"', "true or false"),
    ("secret_allowlist", '"rule:x"', "list"),
    ("finding_allowlist", '"sha256:0123456789abcdef"', "list"),
    ("finding_allowlist", '["sha256:abc"]', "exact"),
])
def test_security_config_invalid_versioned_values_are_reported(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        key: str, toml_value: str, message: str) -> None:
    _isolate_config(monkeypatch)
    _security_repo_config(
        tmp_path, f"[security]\n{key} = {toml_value}\n")
    config = S.load_config(tmp_path)
    assert config["repo_error"] is not None
    assert key in config["repo_error"]
    assert message in config["repo_error"]
    assert config["sources"][key] == "default"
    with pytest.raises(ConfigError):
        S.load_config_strict(tmp_path)


def test_invalid_versioned_value_is_reported_even_when_legacy_wins(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    _security_legacy_config(tmp_path, "[security]\nenabled = true\n")
    _security_repo_config(tmp_path, '[security]\nenabled = "yes"\n')
    config = S.load_config(tmp_path)
    assert config["enabled"] is True
    assert config["sources"]["enabled"] == "legacy"
    assert config["repo_error"] is not None
    with pytest.raises(ConfigError):
        S.load_config_strict(tmp_path)


def test_security_config_ignores_unknown_and_nested_keys(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    _security_repo_config(tmp_path, """
[security]
enabled = false
unknown = true

[security.nested]
key = "value"
""")
    config = S.load_config(tmp_path)
    assert config["enabled"] is False
    assert "unknown" not in config and "nested" not in config
    assert config["error"] is None


# --------------------------------------------------------------------------- #
# graph-derived findings
# --------------------------------------------------------------------------- #
def test_graph_shell_sink_path(sec_repo: Path) -> None:
    report = S.scan(sec_repo, exclude=[], max_findings=100)
    finding = _finding(report, "shell-true")
    assert finding["category"] == "owasp:a03"
    assert finding["cwe"] == ["cwe:78"]
    assert finding["file"] == "src/app.py"
    assert finding["line"] == 9
    assert finding["source"]["type"] == "graph"
    assert finding["graph_path"][0] == "route:POST /run"
    assert finding["graph_path"][-1] == "sink:shell"
    assert finding["evidence"]["kind"] == "graph_path"
    assert finding["evidence"]["path"] == finding["graph_path"]
    assert report["graph"]["available"] is True
    assert report["truth_assessed"] is False

    access = _finding(report, "route-without-auth")
    assert access["category"] == "owasp:a01"
    assert "check:permission-missing" in access["graph_path"]


def test_secret_values_are_never_printed(sec_repo: Path) -> None:
    report = S.scan(sec_repo, exclude=[], max_findings=100)
    text = _all_text(report)
    for value in SECRETS:
        assert value not in text
    secrets = [item for item in report["findings"]
               if item["category"] == "secret"]
    assert secrets
    for item in secrets:
        assert item["value_withheld"] is True
        assert item["secret_fingerprint"].startswith("sha256:")
        assert len(item["secret_fingerprint"]) == len("sha256:") + 16
    assert any(item["source"]["rule"] == "aws_access_key"
               for item in secrets)
    assert any(item["source"]["rule"] == "github_token"
               for item in secrets)
    assert any(item["source"]["rule"] == "openai_key"
               for item in secrets)


def test_crypto_and_tls_rules(sec_repo: Path) -> None:
    report = S.scan(sec_repo, exclude=[], max_findings=100)
    key = _finding(report, "hardcoded-key")
    assert key["category"] == "owasp:a02"
    assert key["cwe"] == ["cwe:321"]
    tls = _finding(report, "tls-verify-false")
    assert tls["category"] == "owasp:a02"
    assert tls["cwe"] == ["cwe:295"]


def test_scanner_ignores_rule_data_and_non_executable_examples() -> None:
    source = '''\
_DANGEROUS_TOKENS = ("eval(", "shell=True", "curl https://x | bash")
_RULES = (
    _r("eval", r"(?<![\\w.])eval\\s*\\("),
    _r("weak-password-hash", r"(?i)\\b(?:md5|sha1)\\b"),
)

def run(user_input):
    """Example only: eval(user_input); curl https://x | bash"""
    # curl https://x | bash
    eval(user_input)
'''
    findings = S._scan_source(
        "src/example.py", source, S.GraphIndex(info={}), [])
    assert [item["source"]["rule"] for item in findings] == ["eval"]
    assert findings[0]["line"] == 10

    workflow_example = "# curl https://example.test/install | bash\non: push\n"
    findings = S._scan_source(
        ".github/workflows/release.yml", workflow_example,
        S.GraphIndex(info={}), [])
    assert not findings

    active_installer = '''\
script = f"curl -fsSL {url} | bash"
subprocess.run(["bash", "-c", script])
'''
    findings = S._scan_source(
        "src/installer.py", active_installer, S.GraphIndex(info={}), [])
    assert any(item["source"]["rule"] == "curl-pipe-shell"
               for item in findings)

    computed_registry = "_DANGEROUS_TOKENS = (eval(user_input),)\n"
    findings = S._scan_source(
        "src/registry.py", computed_registry, S.GraphIndex(info={}), [])
    assert any(item["source"]["rule"] == "eval" for item in findings)

    computed_rule_registry = (
        '_RULES = (_r("computed", eval(user_input)),)\n')
    findings = S._scan_source(
        "src/rule_registry.py", computed_rule_registry,
        S.GraphIndex(info={}), [])
    assert any(item["source"]["rule"] == "eval" for item in findings)


def test_code_execution_rules_ignore_cpp_member_calls() -> None:
    source = '''\
menu->exec(view->viewport()->mapToGlobal(pos));
QMenu::exec(actions, view, nullptr, view);
parent_->m_eventLoop->exec();
d->m_js->execute(builtInScript + script, event);
'''
    findings = S._scan_source(
        "part/example.cpp", source, S.GraphIndex(info={}), [])
    assert not [item for item in findings
                if item["source"]["rule"] in {"exec", "eval", "sql-concat"}]

    declaration = "void exec(const QPoint point = QPoint());\n"
    findings = S._scan_source(
        "part/example.h", declaration, S.GraphIndex(info={}), [])
    assert not [item for item in findings if item["source"]["rule"] == "exec"]

    # `->` and `::` member calls are also not Python's builtin in languages
    # outside the C-family, where these rules still run.
    for rel, text in (("src/example.php", "$menu->exec($pos);\n"),
                      ("src/example.rs", "Type::exec();\n")):
        findings = S._scan_source(rel, text, S.GraphIndex(info={}), [])
        assert not [item for item in findings
                    if item["source"]["rule"] == "exec"], rel

    python_source = '''\
exec(user_input)
cursor.execute("select * from users where id=" + value)
'''
    findings = S._scan_source(
        "src/example.py", python_source, S.GraphIndex(info={}), [])
    rules = {item["source"]["rule"] for item in findings}
    assert {"exec", "sql-concat"} <= rules


def test_code_execution_rules_ignore_python_strings_and_comments() -> None:
    source = '''\
DELEGATES = {"eval": "the agent eval (python -m ozagia.eval.agent)"}
def run(user_input):
    """eval(user_input)"""
    note = "exec(user_input)"  # eval(user_input)
    eval(user_input)
    exec(user_input)
    rendered = f"{eval(user_input)}"
    literal = f"call eval(user_input) later"
'''
    findings = S._scan_source(
        "src/example.py", source, S.GraphIndex(info={}), [])
    assert [(item["source"]["rule"], item["line"]) for item in findings] == [
        ("eval", 5), ("exec", 6), ("eval", 7)]

    # An unparseable file must keep the rule rather than hiding the finding.
    broken = "def run(:\n    eval(user_input)\n"
    findings = S._scan_source(
        "src/broken.py", broken, S.GraphIndex(info={}), [])
    assert any(item["source"]["rule"] == "eval" for item in findings)


def test_generic_secret_rule_ignores_key_loader_calls() -> None:
    text = '''\
api_key = _load_api_key(args.api_key_file)
secret = read_api_key(env_name="X")
API_KEY = "aB3-xY7-zQ2-wP9"
'''
    hits = S.secret_hits(text)
    assert [(hit.rule, hit.line) for hit in hits] == [
        ("generic_high_entropy", 3)]


def test_unsafe_yaml_rule_recognizes_static_safe_loaders() -> None:
    source = '''\
import yaml
class Loader(yaml.SafeLoader):
    pass
class Nested(Loader):
    pass
a = yaml.load(one, Loader=Loader)
b = yaml.load(two, Loader=Nested)
c = yaml.load(three, Loader=yaml.BaseLoader)
d = yaml.load(four)
e = yaml.load(five, Loader=yaml.FullLoader)
f = yaml.load(six, Loader=UnknownLoader)
g = yaml.safe_load(seven)
h = yaml.load(eight)
'''
    findings = S._scan_source(
        "src/example.py", source, S.GraphIndex(info={}), [])
    assert [(item["source"]["rule"], item["line"]) for item in findings] == [
        ("unsafe-yaml", 9),
        ("unsafe-yaml", 10),
        ("unsafe-yaml", 11),
        ("unsafe-yaml", 13),
    ]


def test_secret_scan_ignores_docstrings_but_keeps_live_assignments() -> None:
    source = '''\
"""Examples: secret://keyring/SERVICE/ACCOUNT"""
def load():
    """Example: API_KEY = \"aB3-xY7-zQ2-wP9\"""
    API_KEY = "aB3-xY7-zQ2-wP9"
'''
    ignored = S._python_non_code_lines(source)
    hits = S.secret_hits(source, ignored_lines=ignored)
    assert [(hit.rule, hit.line) for hit in hits] == [
        ("generic_high_entropy", 4)]


def test_generic_secret_rule_rejects_reference_and_expression_shapes() -> None:
    text = '''\
secret = self.resolve
secret: //keyring/SERVICE/ACCOUNT
API_KEY = "aB3-xY7-zQ2-wP9"
'''
    hits = S.secret_hits(text)
    assert [(hit.rule, hit.line) for hit in hits] == [
        ("generic_high_entropy", 3)]


def test_dependency_inventory_without_scanner(sec_repo: Path) -> None:
    report = S.scan(sec_repo, exclude=[], max_findings=100)
    inventory = report["dependency_inventory"]
    assert inventory["components"] == 2
    assert inventory["manifests"][0]["path"] == "pyproject.toml"
    assert inventory["cve_status"] == "not_assessed"
    assert inventory["scanner_ran"] is False
    finding = _finding(report, "dependency-inventory")
    assert finding["category"] == "dependency"
    assert finding["cve_status"] == "not_assessed"
    assert any("CVE" in note for note in finding["not_assessed"])
    assert any("CVE" in note for note in report["not_assessed"])


def test_weak_hash_respects_explicit_non_security_flag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(
        repo / "src" / "hashes.py",
        "import hashlib\n"
        "\n"
        "def cache_key(value):\n"
        "    return hashlib.md5(value, usedforsecurity=False).hexdigest()\n"
        "\n"
        "def password_digest(value):\n"
        "    return hashlib.md5(value).hexdigest()\n"
        "\n"
        "def split_call(value):\n"
        "    return hashlib.sha1(\n"
        "        value,\n"
        "        usedforsecurity=False,\n"
        "    ).hexdigest()\n",
    )
    report = S.scan(repo, exclude=[], max_findings=100)
    weak = [item for item in report["findings"]
            if (item.get("source") or {}).get("rule") == "weak-hash"]
    # The explicit non-security call and its multi-line form are skipped; the
    # ordinary call is still a finding.
    assert [item["line"] for item in weak] == [7]


def test_cookie_flags_ignore_cookie_jar_receivers(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(
        repo / "src" / "cookies.py",
        "def store(jar, cookie):\n"
        "    jar.set_cookie(cookie)\n"
        "    self.jar.set_cookie(cookie)\n"
        "\n"
        "def respond(response, value):\n"
        "    response.set_cookie('session', value)\n",
    )
    report = S.scan(repo, exclude=[], max_findings=100)
    cookies = [item for item in report["findings"]
               if (item.get("source") or {}).get("rule") == "cookie-flags"]
    # A cookie jar is not an HTTP response; only the response call lacks flags.
    assert [(item["file"], item["line"]) for item in cookies] == [
        ("src/cookies.py", 6)]


def test_dedupe_and_cap(sec_repo: Path) -> None:
    full = S.scan(sec_repo, exclude=[], max_findings=100)
    shell = [item for item in full["findings"]
             if item["source"]["rule"] == "shell-true"]
    assert len(shell) == 1
    assert shell[0]["graph_path"][-1] == "sink:shell"

    capped = S.scan(sec_repo, exclude=[], max_findings=1)
    assert len(capped["findings"]) == 1
    assert capped["summary"]["candidates"] > 1
    assert capped["summary"]["omitted"] > 0
    assert S.report_json(S.scan(sec_repo, exclude=[], max_findings=100)) == (
        S.report_json(full))


def test_finding_allowlist_exact_fingerprint_suppresses_only_that_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "src" / "app.py", """\
import subprocess

def run(cmd):
    subprocess.run(cmd, shell=True)
""")
    line = (repo / "src" / "app.py").read_text(encoding="utf-8").splitlines()[3]
    fingerprint = S.finding_fingerprint("shell-true", "src/app.py", line)

    before = S.scan(repo, max_findings=100)
    assert _finding(before, "shell-true")["finding_fingerprint"] == fingerprint
    assert S.exit_code(before) == 1

    _security_repo_config(
        repo, f'[security]\nfinding_allowlist = ["{fingerprint}"]\n')
    after = S.scan(repo, max_findings=100)
    assert after["allowed"] == [{
        "rule": "shell-true", "path": "src/app.py", "line": 4,
        "fingerprint": fingerprint, "allowlist": fingerprint}]
    assert not [item for item in after["findings"]
                if item["source"]["rule"] == "shell-true"]
    assert after["summary"]["allowed"] == 1
    assert after["summary"]["allowed_total"] == 1
    assert S.exit_code(after) == 0
    text = S.format_report(after)
    assert "allowlisted: 1" in text
    assert "[ALLOWED] shell-true src/app.py:4" in text
    payload = json.loads(S.report_json(after))
    assert payload["allowed"] == after["allowed"]

    # Editing the matched line must invalidate the reviewed fingerprint.
    _write(repo / "src" / "app.py", """\
import subprocess

def run(cmd):
    subprocess.run(cmd, shell=True, check=True)
""")
    changed = S.scan(repo, max_findings=100)
    assert changed["allowed"] == []
    assert any(item["source"]["rule"] == "shell-true"
               for item in changed["findings"])
    assert S.exit_code(changed) == 1


def test_finding_allowlist_does_not_suppress_an_unknown_fingerprint(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "src" / "app.py", """\
import subprocess

def run(cmd):
    subprocess.run(cmd, shell=True)
""")
    _security_repo_config(
        repo, '[security]\nfinding_allowlist = ["sha256:0000000000000000"]\n')
    report = S.scan(repo, max_findings=100)
    assert report["allowed"] == []
    assert report["summary"]["allowed"] == 0
    assert any(item["source"]["rule"] == "shell-true"
               for item in report["findings"])


def test_finding_allowlist_report_withholds_secret_values(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    repo = _init_repo(tmp_path / "repo")
    _write(repo / "src" / "leak.py", f'aws = "{AWS_KEY}"\n')
    line = (repo / "src" / "leak.py").read_text(encoding="utf-8").splitlines()[0]
    fingerprint = S.finding_fingerprint("aws_access_key", "src/leak.py", line)
    _security_repo_config(
        repo, f'[security]\nfinding_allowlist = ["{fingerprint}"]\n')

    report = S.scan(repo, max_findings=100)
    assert report["allowed"][0]["rule"] == "aws_access_key"
    assert report["allowed"][0]["path"] == "src/leak.py"
    assert report["allowed"][0]["fingerprint"] == fingerprint
    text = S.report_json(report) + S.format_report(report)
    assert AWS_KEY not in text


def test_finding_allowlist_match_supports_broad_entries_but_not_prefixes() -> None:
    fingerprint = S.finding_fingerprint("shell-true", "./src/app.py",
                                        "    subprocess.run(cmd, shell=True)")
    finding = {
        "file": "src/app.py",
        "line": 4,
        "finding_fingerprint": fingerprint,
        "source": {"rule": "shell-true"},
    }
    assert S.finding_allowlist_match(finding, [fingerprint]) == fingerprint
    # A sha256 entry is exact; a prefix does not silently widen it.
    assert S.finding_allowlist_match(
        finding, [fingerprint[:len("sha256:") + 8]]) is None
    # Broad compatibility entries are still accepted, but discouraged.
    assert S.finding_allowlist_match(finding, ["path:src/**"]) == "path:src/**"
    assert (S.finding_allowlist_match(finding, ["rule:shell-true"])
            == "rule:shell-true")
    assert S.finding_allowlist_match(
        finding, ["sha256:0000000000000000"]) is None


def test_finding_allowlist_precedence_and_versioned_validation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_config(monkeypatch)
    legacy_fingerprint = "sha256:" + "a" * 16
    repo_fingerprint = "sha256:" + "b" * 16
    _security_legacy_config(
        tmp_path,
        f'[security]\nfinding_allowlist = ["{legacy_fingerprint}"]\n')
    _security_repo_config(
        tmp_path,
        f'[security]\nfinding_allowlist = ["{repo_fingerprint}"]\n')
    config = S.load_config(tmp_path)
    assert config["finding_allowlist"] == [legacy_fingerprint]
    assert config["sources"]["finding_allowlist"] == "legacy"
    assert config["error"] is None

    _security_repo_config(
        tmp_path, '[security]\nfinding_allowlist = ["sha256:nothex"]\n')
    config = S.load_config(tmp_path)
    assert config["finding_allowlist"] == [legacy_fingerprint]
    assert config["repo_error"] is not None
    assert "exact" in config["repo_error"]
    with pytest.raises(ConfigError):
        S.load_config_strict(tmp_path)


# --------------------------------------------------------------------------- #
# tool registry and adapters
# --------------------------------------------------------------------------- #
def test_tools_reports_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        S.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "bandit" else None)
    listed = {item["name"]: item for item in S.tools()}
    assert listed["bandit"]["status"] == "available"
    assert listed["bandit"]["network_required"] is False
    assert listed["pip-audit"]["status"] == "unavailable"


def _fake_pip_audit(argv: list[str], *, timeout: int, cwd: str) -> dict[str, Any]:
    assert argv == ["pip-audit", "--format", "json"]
    return {
        "returncode": 1,
        "version": "pip-audit 9.9.9",
        "database": {"timestamp": "2026-01-01T00:00:00Z"},
        "stdout": json.dumps({"dependencies": [{
            "name": "flask", "version": "2.0.1",
            "vulns": [{"id": "CVE-2026-0001", "severity": "high",
                       "description": "fixture advisory",
                       "fix_versions": ["3.0.0"]}],
        }]}),
        "stderr": "",
    }


def test_fake_tool_adapter_records_provenance(sec_repo: Path) -> None:
    report = S.scan(
        sec_repo, exclude=[], tools=["pip-audit"], allow_network=True,
        runner=_fake_pip_audit, max_findings=100)
    result = report["tools"]["results"][0]
    assert result["status"] == "ok"
    assert result["version"] == "pip-audit 9.9.9"
    assert result["command"] == ["pip-audit", "--format", "json"]
    assert result["exit_code"] == 1
    assert result["database"]["time"] == "2026-01-01T00:00:00Z"
    finding = _finding(report, "pip-audit:CVE-2026-0001")
    assert finding["category"] == "dependency"
    assert finding["package"] == "flask"
    assert finding["source"]["tool"] == "pip-audit"
    assert finding["source"]["version"] == "pip-audit 9.9.9"
    assert finding["evidence"]["kind"] == "tool"
    assert report["dependency_inventory"]["cve_status"] == (
        "scanner_reported_not_exhaustive")


def test_missing_tool_is_unavailable_not_failure(
        sec_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    report = S.scan(sec_repo, exclude=[], tools=["bandit"])
    result = report["tools"]["results"][0]
    assert result["status"] == "unavailable"
    assert "not installed" in result["error"]
    assert not [item for item in report["findings"]
                if item["source"].get("type") == "tool"]


def test_network_tool_refused_without_allow_network(
        sec_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S.shutil, "which", lambda name: f"/usr/bin/{name}")
    called: list[list[str]] = []

    def runner(argv: list[str], *, timeout: int, cwd: str) -> dict[str, Any]:
        called.append(argv)
        raise AssertionError("the runner must not run behind the network gate")

    report = S.scan(
        sec_repo, exclude=[], tools=["pip-audit"], allow_network=False,
        runner=runner)
    result = report["tools"]["results"][0]
    assert result["status"] == "refused"
    assert called == []
    assert any("refused" in note for note in report["not_assessed"])


# --------------------------------------------------------------------------- #
# bounded, read-only scan
# --------------------------------------------------------------------------- #
def test_scan_makes_no_network_or_subprocess_calls(
        sec_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Remove the graph so the fallback path is filesystem-only.
    (sec_repo / ".ozagia" / "graph.json").unlink()
    before = _snapshot(sec_repo)

    def _no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the scan must not use the network")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(subprocess, "run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("the scan must not run a process")))
    report = S.scan(sec_repo, exclude=[], max_findings=20)
    assert report["graph"]["freshness"] == "missing"
    assert report["truth_assessed"] is False
    assert _snapshot(sec_repo) == before


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_scan_exit_codes_and_explain(
        sec_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert CLI.main(["scan", "--root", str(sec_repo), "--json",
                     "--fail-on", "high"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == S.SCHEMA
    assert CLI.main(["scan", "--root", str(sec_repo), "--fail-on",
                     "never"]) == 0
    capsys.readouterr()
    assert CLI.main(["tools"]) == 0
    assert "Security tool registry" in capsys.readouterr().out

    finding = payload["findings"][0]
    assert CLI.main(["explain", finding["id"], "--root", str(sec_repo),
                     "--json"]) == 0
    text = capsys.readouterr().out
    assert finding["id"] in text
    for value in SECRETS:
        assert value not in text
    assert CLI.main(["explain", "sec-missing", "--root", str(sec_repo)]) == 2
    assert CLI.main(["scan", "--root", str(sec_repo / "missing")]) == 2


def test_cli_secrets_json_has_no_secret(
        sec_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _stage(sec_repo, "src/leak.py", f'KEY = "{AWS_KEY}"\n')
    code = CLI.main(["secrets", "--root", str(sec_repo), "--staged",
                     "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == S.SECRETS_SCHEMA
    assert payload["findings"]
    for value in SECRETS:
        assert value not in json.dumps(payload)
    assert payload["findings"][0]["secret_fingerprint"].startswith("sha256:")


# --------------------------------------------------------------------------- #
# default commit/push gate
# --------------------------------------------------------------------------- #
def test_commit_gate_blocks_staged_keys_without_values(sec_repo: Path) -> None:
    _stage(sec_repo, "src/keys.py",
           f"AWS = \"{AWS_KEY}\"\nGITHUB = \"{GITHUB_KEY}\"\n"
           f"OPENAI = \"{OPENAI_KEY}\"\n")
    report = W.policy_check(sec_repo, "commit", message="feat: add keys")
    assert report["ok"] is False
    errors = [check for check in report["checks"]
              if check["id"] == "commit.secret"]
    assert len(errors) == 3
    payload = json.dumps(report, sort_keys=True)
    for value in SECRETS:
        assert value not in payload
    for check in errors:
        assert check["fingerprint"].startswith("sha256:")
        assert "value withheld" in check["message"]


def test_commit_gate_allowlist_path_and_fingerprint(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "allowed")
    _git(repo, "checkout", "-q", "-b", "feat/allowed")
    _write(repo / ".ozagia.toml", """\
[security]
secret_allowlist = ["path:tests/fixtures/**", "rule:github_token"]
""")
    _stage(repo, "tests/fixtures/key.txt", f"AWS = \"{AWS_KEY}\"\n")
    _stage(repo, "src/github.py", f'TOKEN = "{GITHUB_KEY}"\n')
    report = W.policy_check(repo, "commit", message="feat: fixture key")
    assert not [check for check in report["checks"]
                if check["id"] == "commit.secret"
                and check["level"] == "error"]
    assert report["security"]["allowed"] >= 2
    assert any(check["id"] == "commit.secret_allowlisted"
               for check in report["checks"])

    fingerprint = S.secret_fingerprint(GITHUB_KEY)
    _write(repo / ".ozagia.toml", f"""\
[security]
secret_allowlist = ["path:tests/fixtures/**", "{fingerprint}"]
""")
    report = W.policy_check(repo, "commit", message="feat: fixture token")
    assert not [check for check in report["checks"]
                if check["id"] == "commit.secret"
                and check["level"] == "error"]
    assert any(check["id"] == "commit.secret_allowlisted"
               for check in report["checks"])


def test_one_time_override_is_recorded(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "override")
    _git(repo, "checkout", "-q", "-b", "feat/override")
    _stage(repo, "src/key.py", f'AWS = "{AWS_KEY}"\n')
    fingerprint = S.secret_fingerprint(AWS_KEY)
    report = W.policy_check(
        repo, "commit", message="feat: override",
        allow_secrets=[fingerprint])
    assert not [check for check in report["checks"]
                if check["id"] == "commit.secret"
                and check["level"] == "error"]
    assert any(check["id"] == "commit.secret_override"
               for check in report["checks"])
    audit_path = repo / ".ozagia" / "security" / "audit.jsonl"
    audit = audit_path.read_text(encoding="utf-8")
    payload = json.loads(audit.strip().splitlines()[-1])
    assert payload["fingerprint"] == fingerprint
    assert payload["decision"] == "allowed_once"
    assert AWS_KEY not in audit


def test_block_secrets_false_is_reported(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "disabled")
    _git(repo, "checkout", "-q", "-b", "feat/disabled")
    _write(repo / ".ozagia.toml", "[security]\nblock_secrets = false\n")
    _stage(repo, "src/key.py", f'AWS = "{AWS_KEY}"\n')
    report = W.policy_check(repo, "commit", message="feat: disabled")
    assert report["security"]["disabled"] is True
    assert not [check for check in report["checks"]
                if check["id"] == "commit.secret"
                and check["level"] == "error"]
    assert any(check["id"] == "commit.secret_gate"
               and check["level"] == "warn" for check in report["checks"])


def test_push_gate_catches_committed_secret(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "push")
    _git(repo, "checkout", "-q", "-b", "feat/push")
    _write(repo / "src" / "key.py", f'AWS = "{AWS_KEY}"\n')
    _commit(repo, "feat: committed key")
    report = W.policy_check(repo, "push", base="main")
    assert report["ok"] is False
    errors = [check for check in report["checks"]
              if check["id"] == "push.secret"]
    assert errors
    assert AWS_KEY not in json.dumps(report, sort_keys=True)
    assert errors[0]["fingerprint"].startswith("sha256:")


def test_secret_scan_skips_binary_and_large(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "skips")
    binary = b"\x00\x01" + AWS_KEY.encode() + b"\x00"
    (repo / "blob.bin").write_bytes(binary)
    _write(repo / "large.txt", ("padding line\n" * 100000) +
           f"AWS = \"{AWS_KEY}\"\n")
    _git(repo, "add", "blob.bin", "large.txt")
    report = S.scan_secrets(repo, staged=True)
    reasons = {item["path"]: item["reason"] for item in report["skipped"]}
    assert "binary" in reasons["blob.bin"]
    assert "larger" in reasons["large.txt"]
    assert AWS_KEY not in S.report_json(report)


def test_missing_staged_content_is_reported(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "clean")
    report = S.scan_secrets(repo, staged=True)
    assert report["findings"] == []
    assert any("no staged content" in note for note in report["notes"])
    policy = W.policy_check(repo, "commit", message="feat: clean")
    assert any(check["id"] == "commit.secret" and check["level"] == "ok"
               for check in policy["checks"])


def test_secret_scan_requires_one_mode(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "mode")
    with pytest.raises(S.SecurityError):
        S.scan_secrets(repo)
    with pytest.raises(S.SecurityError):
        S.scan_secrets(repo, staged=True, diff="main..HEAD")


def test_hooks_are_opt_in_and_reversible(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "hooks")
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert not hook.exists()
    # A policy check never installs a hook.
    W.policy_check(repo, "commit", message="feat: clean")
    assert not hook.exists()
    installed = S.install_hook(repo, "commit")
    assert installed["installed"] is True
    assert S.HOOK_MARKER in hook.read_text(encoding="utf-8")
    assert hook.stat().st_mode & 0o111
    removed = S.uninstall_hook(repo, "commit")
    assert removed["removed"] is True
    assert not hook.exists()
    hook.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
    with pytest.raises(S.SecurityError):
        S.uninstall_hook(repo, "commit")


def test_scan_staged_cli_adds_secret_section(
        sec_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _stage(sec_repo, "src/extra.py", f'AWS = "{AWS_KEY}"\n')
    code = CLI.main(["scan", "--root", str(sec_repo), "--staged", "--json",
                     "--fail-on", "never"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["secrets"]["mode"] == "staged"
    assert payload["secrets"]["summary"]["total"] >= 1
    assert AWS_KEY not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# integration: code review and repo scan
# --------------------------------------------------------------------------- #
def test_code_review_accepts_supplied_security_findings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "review")
    _write(repo / "src" / "app.py", "def f():\n    return 1\n")
    base = _commit(repo, "feat: base")
    _write(repo / "src" / "app.py", "def f():\n    return 2\n")
    head = _commit(repo, "feat: head")
    supplied = {
        "schema": S.SCHEMA,
        "findings": [{
            "id": "sec-abc123",
            "severity": "high",
            "category": "secret",
            "kind": "provider-key",
            "file": "src/app.py",
            "line": 1,
            "message": "supplied finding",
            "evidence": {"kind": "line"},
            "source": {"type": "graph", "rule": "fixture"},
            "not_assessed": ["runtime reachability"],
            "secret_fingerprint": "sha256:0123456789abcdef",
        }],
        "summary": {"total": 1},
        "not_assessed": ["exploitability"],
    }
    packet = CR.review_diff(
        repo, base=base, head=head, memory_mb=0, disk_mb=0,
        security_report=supplied)
    assert packet["security"]["findings"][0]["id"] == "sec-abc123"
    assert packet["security"]["truth_assessed"] is False
    assert CR.exit_code(packet, "major") == 1
    assert any(entry.get("area") == "security"
               for entry in packet["not_assessed"])


def test_repo_scan_injects_security_category(sec_repo: Path) -> None:
    security_report = S.scan(sec_repo, exclude=[], max_findings=100)
    report = RS.scan(sec_repo, top=50, security_report=security_report)
    assert report["security"]["available"] is True
    security = [item for item in report["opportunities"]
                if item["category"] == "security"]
    assert security
    ids = {item["id"] for item in security_report["findings"]}
    assert any(item["id"] in ids for item in security)
    assert "security" in report["inputs"]["categories"]
