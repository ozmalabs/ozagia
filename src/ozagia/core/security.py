"""Offline security and antipattern scan over the graph plus local files.

This is plan item 15a: a triage layer, not a proof of exploitability. It loads
an *existing* ``.ozagia/graph.json`` read-only, adds bounded filesystem and
manifest reads, and can run a fixed registry of explicitly named external
tools. It never installs a tool, never builds or refreshes the graph, never
edits the repository, and (except for tools whose database refresh needs the
network) never uses the network.

Every finding carries a rule, a location, evidence, a confidence note and a
list of things that were not assessed. ``truth_assessed`` is always false.
Secrets are reported by rule and location; their values are never copied into
the report.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "ozagia.security.report.v1"
SCHEMA_VERSION = 1
ADVISORY = (
    "advisory triage only: findings are evidence for review, not a proof of "
    "exploitability or a statement that the code is secure"
)

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
CONFIDENCES = ("high", "medium", "low")
CONFIDENCE_RANK = {name: rank for rank, name in enumerate(CONFIDENCES)}
FAIL_ON = ("blocking", "high", "medium", "low", "never")
DEFAULT_FAIL_ON = "high"
DEFAULT_MAX_FINDINGS = 200
MAX_FINDINGS = 1000

MAX_SCAN_FILES = 5000
MAX_FILE_BYTES = 512 * 1024
MAX_GRAPH_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 8 * 1024 * 1024
TOOL_TIMEOUT_S = 120
VERSION_TIMEOUT_S = 15
MAX_TOOL_FINDINGS = 500
MAX_GRAPH_PATH = 8
MAX_GRAPH_DEPTH = 3

SECRETS_SCHEMA = "ozagia.security.secrets.v1"
AUDIT_SCHEMA = "ozagia.security.audit.v1"
MAX_SECRET_FILES = 1000
MAX_SECRET_FILE_BYTES = 1024 * 1024
MAX_SECRET_DIFF_BYTES = 16 * 1024 * 1024
SECRET_DIFF_TIMEOUT_S = 30
HOOK_MARKER = "# ozagia security gate"

DEFAULT_EXCLUDES = ("tests/**", "test/**", "docs/**", "vendor/**")

SKIP_PARTS = frozenset({
    ".git", ".ozagia", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", ".tox",
    ".nox", ".next", ".nuxt", ".cache", ".parcel-cache", ".turbo", "dist",
    "build", "target", "coverage", "htmlcov", ".idea", ".vscode",
})

SCAN_SUFFIXES = frozenset({
    ".py", ".pyi", ".rs", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".java", ".kt", ".kts", ".rb", ".php", ".c", ".h", ".cc",
    ".cpp", ".cxx", ".hpp", ".cs", ".swift", ".scala", ".ex", ".exs",
    ".lua", ".sh", ".bash", ".zsh", ".sql", ".html", ".htm", ".jinja",
    ".j2", ".yaml", ".yml", ".toml", ".json", ".xml", ".tf", ".hcl",
    ".dockerfile", ".conf", ".ini", ".cfg", ".properties", ".env",
    ".p12", ".pfx", ".jks", ".keystore", ".key", ".pem",
})

SCAN_NAMES = frozenset({
    "Dockerfile", "Containerfile", "Makefile", "Gemfile", "Procfile",
    "requirements.txt", ".npmrc", ".pypirc", ".env", "credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "config",
})

CI_PREFIXES = (".github/workflows/", ".gitlab-ci", ".circleci/")

OWASP_TITLES = {
    "a01": "A01 broken access control",
    "a02": "A02 cryptographic failures",
    "a03": "A03 injection",
    "a04": "A04 insecure design",
    "a05": "A05 security misconfiguration",
    "a06": "A06 vulnerable and outdated components",
    "a07": "A07 identification and authentication failures",
    "a08": "A08 software and data integrity failures",
    "a09": "A09 security logging and monitoring failures",
    "a10": "A10 server-side request forgery",
}

AUTH_HINTS = re.compile(
    r"(?i)\b(?:login_required|requires?_auth|auth_required|authoriz\w*|"
    r"permission\w*|current_user|jwt_required|verify_jwt|has_role|"
    r"roles_required|IsAuthenticated|permission_classes|Depends\s*\(|"
    r"check_perm\w*|csrf_protect|before_request)\b")
ROUTE_DECORATOR = re.compile(
    r"(?i)^@?(?P<owner>[\w.]+)\s*\.\s*"
    r"(?P<verb>route|get|post|put|patch|delete|api_route)\s*\(\s*"
    r"[frub]?[\"'](?P<path>[^\"']*)[\"'](?P<rest>.*)$",
    re.S,
)
ROUTE_METHODS = re.compile(r"(?i)\bmethods\s*=\s*[\[(]([^\])]*)[\])]")
QUOTED = re.compile(r"[\"']([A-Za-z]+)[\"']")
PRIVATE_ROUTE = re.compile(
    r"(?i)(?:^|/)(?:health|healthz|ready|readyz|live|livez|metrics|static|"
    r"favicon|login|logout|signin|signup|register|public|docs|openapi|"
    r"swagger)(?:/|$)")

@dataclass(frozen=True)
class SecretRule:
    name: str
    pattern: re.Pattern[str]
    kind: str
    severity: str
    confidence: str
    value_group: str = ""
    multiline: bool = False


_SECRET_RULES: tuple[SecretRule, ...] = (
    SecretRule("aws_access_key",
               re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
               "provider-key", "high", "high"),
    SecretRule("github_token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,})\b"),
        "provider-key", "high", "high"),
    SecretRule("anthropic_key", re.compile(
        r"\bsk-ant-(?:api|admin)\d*-[A-Za-z0-9_-]{16,}\b"),
        "provider-key", "high", "high"),
    SecretRule("openai_key", re.compile(
        r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}\b"),
        "provider-key", "high", "high"),
    SecretRule("slack_token", re.compile(
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        "provider-key", "high", "high"),
    SecretRule("slack_webhook", re.compile(
        r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}"),
        "provider-key", "high", "high"),
    SecretRule("google_api_key", re.compile(
        r"\bAIza[0-9A-Za-z_-]{35}\b"), "provider-key", "high", "high"),
    SecretRule("google_oauth_secret", re.compile(
        r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"), "provider-key", "high", "high"),
    SecretRule("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
        "token", "high", "high"),
    SecretRule("url_credentials", re.compile(
        r"://[^/\s:@]+:[^/\s@]+@"), "credential", "high", "medium"),
    SecretRule("generic_high_entropy", re.compile(
        r"(?i)\b(?P<label>api[_-]?key|apikey|secret|client[_-]?secret|"
        r"aws[_-]?secret[_-]?access[_-]?key|secret[_-]?access[_-]?key|"
        r"access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
        r"password|passwd|private[_-]?key)\b\s*[:=]\s*"
        r"(?P<quote>[\"']?)(?P<value>[A-Za-z0-9_./+=:-]{12,})(?P=quote)"),
        "generic-secret", "high", "medium", value_group="value"),
    SecretRule("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
        r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
        "private-key", "high", "high", multiline=True),
)
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(?P<label>api[_-]?key|apikey|secret|client[_-]?secret|"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token|password|passwd|"
    r"private[_-]?key)\b\s*[:=]\s*[\"'](?P<value>[^\"']{8,})[\"']")
_PLACEHOLDER = re.compile(
    r"(?i)(?:example|sample|dummy|placeholder|changeme|change-me|redacted|"
    r"your[-_]|<|>|\$\{|\{\{|os\.environ|process\.env|getenv|env\[|"
    r"x{4,}|\*{3,})")
_CREDENTIAL_FILE_NAMES = frozenset({
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.staging", ".npmrc", ".pypirc", "credentials", "id_rsa",
    "id_dsa", "id_ecdsa", "id_ed25519",
})
_CREDENTIAL_FILE_SUFFIXES = frozenset({
    ".p12", ".pfx", ".jks", ".keystore", ".key",
})
_CREDENTIAL_FILE_PARTS = (
    ("/.aws/credentials", ".aws/credentials"),
    ("/.config/gcloud/application_default_credentials.json",
     ".config/gcloud/application_default_credentials.json"),
    ("/.docker/config.json", ".docker/config.json"),
    ("/.kube/config", ".kube/config"),
)


def secret_fingerprint(value: str) -> str:
    """A stable, non-reversible identifier for a secret value."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")
                                      ).hexdigest()[:16]


@dataclass(frozen=True)
class SecretHit:
    rule: str
    kind: str
    severity: str
    confidence: str
    line: int
    fingerprint: str
    value: str  # internal only; never serialized


def _line_secret_hits(text: str, *, start_line: int = 1) -> list[SecretHit]:
    hits: list[SecretHit] = []
    for number, line in enumerate(text.splitlines(), start=start_line):
        for rule in _SECRET_RULES:
            if rule.multiline:
                continue
            for match in rule.pattern.finditer(line):
                value = (match.group(rule.value_group)
                         if rule.value_group else match.group(0))
                if not value:
                    continue
                if rule.value_group and (_PLACEHOLDER.search(value)
                                         or not _looks_opaque(value)):
                    continue
                if rule.name == "url_credentials" and _PLACEHOLDER.search(value):
                    continue
                hits.append(SecretHit(
                    rule=rule.name, kind=rule.kind, severity=rule.severity,
                    confidence=rule.confidence, line=number,
                    fingerprint=secret_fingerprint(value), value=value))
    return hits


def secret_hits(text: str, *, start_line: int = 1) -> list[SecretHit]:
    """Find secret-like values in text without retaining them in output."""
    hits = _line_secret_hits(text, start_line=start_line)
    for rule in _SECRET_RULES:
        if not rule.multiline:
            continue
        for match in rule.pattern.finditer(text):
            value = match.group(0)
            line = start_line + text.count("\n", 0, match.start())
            hits.append(SecretHit(
                rule=rule.name, kind=rule.kind, severity=rule.severity,
                confidence=rule.confidence, line=line,
                fingerprint=secret_fingerprint(value), value=value))
    unique: dict[tuple[str, int, str], SecretHit] = {}
    for hit in hits:
        unique.setdefault((hit.rule, hit.line, hit.fingerprint), hit)
    return sorted(unique.values(),
                  key=lambda item: (item.line, item.rule, item.fingerprint))


def credential_file_hit(rel: str) -> SecretHit | None:
    """A path-only hit for a file whose name is itself sensitive."""
    name = Path(rel).name
    lowered = "/" + rel.lower().lstrip("/")
    sensitive = (
        name in _CREDENTIAL_FILE_NAMES
        or name.startswith(".env.")
        or Path(name).suffix.lower() in _CREDENTIAL_FILE_SUFFIXES
        or any(part in lowered for _needle, part in _CREDENTIAL_FILE_PARTS))
    if not sensitive:
        return None
    if name.endswith((".example", ".sample", ".template", ".dist")):
        return None
    return SecretHit(
        rule="credential_file", kind="credential-file", severity="high",
        confidence="medium", line=1,
        fingerprint=secret_fingerprint("file:" + rel), value=rel)


def secret_finding(rel: str, hit: SecretHit,
                   *, source_type: str = "filesystem") -> dict[str, Any]:
    location = "file path" if hit.kind == "credential-file" else (
        f"line {hit.line}")
    return _finding(
        rule=hit.rule, kind=hit.kind, category="secret",
        severity=hit.severity, confidence=hit.confidence,
        file=rel, line=hit.line,
        message=(f"{'credential-bearing path' if hit.kind == 'credential-file' else 'secret-like value'} "
                 f"matched rule {hit.rule}; value withheld "
                 f"(fingerprint {hit.fingerprint})"),
        remediation="revoke and rotate the value, then load it from a "
                    "secret manager or environment; remove it from history",
        false_positive_note="the value may be a documented example or an "
                            "inactive credential; the scan does not test it",
        source_type=source_type,
        evidence={"kind": "file" if hit.kind == "credential-file" else "line",
                  "rule": hit.rule, "fingerprint": hit.fingerprint,
                  "detail": f"{location}; value withheld"},
        cwe=["cwe:798"], owasp=["a02"],
        not_assessed=["whether the value is live or revoked",
                      "git history and external secret stores"],
        extra={"secret_rule": hit.rule,
               "secret_fingerprint": hit.fingerprint,
               "value_withheld": True})


def _secret_findings(rel: str, text: str) -> list[dict[str, Any]]:
    return [secret_finding(rel, hit) for hit in secret_hits(text)]



class SecurityError(ValueError):
    """The scan request is invalid or a report cannot be written."""


# --------------------------------------------------------------------------- #
# internal locations and rules
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    kind: str
    category: str
    cwe: tuple[str, ...]
    severity: str
    confidence: str
    message: str
    remediation: str
    false_positive_note: str
    sink: str = ""
    not_assessed: tuple[str, ...] = (
        "runtime reachability", "input validation outside this file")


def _r(name: str, pattern: str, kind: str, category: str, cwe: str,
       severity: str, confidence: str, message: str, remediation: str,
       note: str, *, sink: str = "",
       not_assessed: tuple[str, ...] = ()) -> Rule:
    return Rule(
        name=name, pattern=re.compile(pattern), kind=kind, category=category,
        cwe=(cwe,) if cwe else (), severity=severity, confidence=confidence,
        message=message, remediation=remediation, false_positive_note=note,
        sink=sink, not_assessed=not_assessed or (
            "runtime reachability", "input validation outside this file"))


# Rules match one line at a time. More contextual checks (JWT options, cookie
# flags, SSRF allowlists, routes) run separately below.
_RULES: tuple[Rule, ...] = (
    _r("eval", r"(?<![\w.])eval\s*\(", "code-execution", "owasp:a03",
       "cwe:95", "high", "medium",
       "eval() can execute attacker-controlled input",
       "replace eval with a parser or an explicit dispatch table",
       "eval of a constant or trusted local expression is still flagged",
       sink="eval"),
    _r("exec", r"(?<![\w.])exec\s*\(", "code-execution", "owasp:a03",
       "cwe:95", "high", "medium",
       "exec() can execute attacker-controlled input",
       "remove exec; load configuration as data",
       "a constant expression or a test helper may be benign",
       sink="exec"),
    _r("shell-true", r"(?i)\bshell\s*=\s*True\b", "command-injection",
       "owasp:a03", "cwe:78", "high", "high",
       "subprocess call enables a shell",
       "use an argv list and shell=False; validate every argument",
       "the command may be a fixed literal with no request data",
       sink="shell"),
    _r("os-system", r"\bos\.(?:system|popen)\s*\(", "command-injection",
       "owasp:a03", "cwe:78", "high", "high",
       "os.system/os.popen invokes a shell",
       "use subprocess with an argv list and shell=False",
       "a constant command with no untrusted input is still shell-based",
       sink="shell"),
    _r("sql-fstring", r"(?i)\b(?:execute|executemany|raw)\s*\(\s*f[\"']",
       "sql-injection", "owasp:a03", "cwe:89", "high", "medium",
       "SQL built with an f-string",
       "use driver parameters or a query builder with bound values",
       "the interpolated value may be a fixed identifier, not request data",
       sink="sql"),
    _r("sql-concat", r"(?i)\b(?:execute|executemany|raw)\s*\([^)]*"
       r"(?:\+|\bformat\s*\(|%\s*\()", "sql-injection", "owasp:a03",
       "cwe:89", "high", "medium",
       "SQL built with string concatenation or formatting",
       "bind parameters instead of building a query string",
       "the concatenated fragment may be a static clause",
       sink="sql"),
    _r("sql-template", r"(?i)\btext\s*\(\s*f[\"'][^\"']*"
       r"(?:select|insert|update|delete)\b", "sql-injection", "owasp:a03",
       "cwe:89", "medium", "medium",
       "SQL text template interpolates a value",
       "use bound parameters in the SQLAlchemy text() call",
       "SQLAlchemy text() with no interpolation is safe",
       sink="sql"),
    _r("unsafe-yaml", r"(?<!\bsafe_)\byaml\.(?:unsafe_)?load\s*\(",
       "deserialization", "owasp:a08", "cwe:502", "high", "medium",
       "YAML is loaded without a safe loader",
       "use yaml.safe_load or an explicit SafeLoader",
       "yaml.load with SafeLoader on the same line is filtered out",
       sink="deserialize"),
    _r("pickle", r"\b(?:pickle|dill|cPickle)\.loads?\s*\(", "deserialization",
       "owasp:a08", "cwe:502", "high", "high",
       "pickle/dill deserializes arbitrary objects",
       "do not unpickle untrusted data; use JSON or a signed format",
       "the bytes may be fully trusted local state",
       sink="deserialize"),
    _r("marshal", r"\bmarshal\.loads?\s*\(", "deserialization", "owasp:a08",
       "cwe:502", "high", "medium",
       "marshal deserializes a Python-specific binary format",
       "do not unmarshal untrusted data",
       "marshal is not a secure format even when the producer is local",
       sink="deserialize"),
    _r("template-injection", r"\brender_template_string\s*\(", "template",
       "owasp:a03", "cwe:1336", "high", "medium",
       "a template is rendered from a string",
       "render a fixed template file and pass values as data",
       "the template may be a fixed constant in the repository",
       sink="template"),
    _r("path-traversal-open", r"\bopen\s*\([^)\n]*\brequest\.",
       "path-traversal", "owasp:a03", "cwe:22", "medium", "medium",
       "a request value reaches a file open path",
       "resolve against a fixed base and reject paths outside it",
       "the request value may be checked by a wrapper not visible here",
       sink="path"),
    _r("path-traversal-send", r"\b(?:send_file|send_from_directory|"
       r"FileResponse|StreamingResponse)\s*\([^)\n]*\brequest\.",
       "path-traversal", "owasp:a03", "cwe:22", "medium", "medium",
       "a request value reaches a file response helper",
       "resolve and confine the path to a fixed directory",
       "framework helpers may canonicalise and reject traversal",
       sink="path"),
    _r("archive-extract", r"\b(?:extractall|unpack_archive)\s*\(",
       "archive-extraction", "owasp:a03", "cwe:22", "medium", "medium",
       "archive extraction can write outside the destination",
       "validate member paths and extract with a safe filter",
       "the archive may be fully trusted or members may be validated",
       sink="archive"),
    _r("tls-verify-false", r"\bverify\s*=\s*False\b", "tls-verification",
       "owasp:a02", "cwe:295", "high", "high",
       "TLS certificate verification is disabled",
       "enable certificate verification and configure a local CA if needed",
       "a test-only call site may be intentional"),
    _r("tls-unverified-context", r"_create_unverified_context\s*\(|"
       r"\bCERT_NONE\b", "tls-verification", "owasp:a02", "cwe:295",
       "high", "high",
       "an unverified TLS context is requested",
       "use ssl.create_default_context()",
       "a local test harness may deliberately use an unverified context"),
    _r("tls-reject-unauthorized", r"(?i)\brejectUnauthorized\s*:\s*false\b|"
       r"\bNODE_TLS_REJECT_UNAUTHORIZED\b", "tls-verification", "owasp:a02",
       "cwe:295", "high", "high",
       "TLS host verification is disabled",
       "remove the override and use the platform trust store",
       "a development-only configuration may still be shipped"),
    _r("weak-hash", r"\b(?:hashlib\.(?:md5|sha1)\s*\(|"
       r"hashlib\.new\s*\(\s*[\"'](?:md5|sha1)[\"']|"
       r"createHash\s*\(\s*[\"'](?:md5|sha1)[\"']|"
       r"MessageDigest\.getInstance\s*\(\s*[\"'](?:MD5|SHA-?1)[\"'])",
       "weak-crypto", "owasp:a02", "cwe:327", "medium", "low",
       "MD5 or SHA-1 is used",
       "use SHA-256+ for integrity and a password KDF for passwords",
       "a non-security checksum may be acceptable; confirm the purpose"),
    _r("static-iv", r"(?i)\b(?:iv|nonce)\s*=\s*(?:b)?[\"'][^\"']{8,}[\"']",
       "static-iv", "owasp:a02", "cwe:329", "medium", "medium",
       "a static IV/nonce literal is used",
       "generate a random IV/nonce per message and store it with ciphertext",
       "a test vector or a non-cryptographic value may be intentional"),
    _r("hardcoded-key", r"(?i)\b(?:secret[_-]?key|encryption[_-]?key|"
       r"aes[_-]?key|signing[_-]?key|private[_-]?key|jwt[_-]?secret|"
       r"api[_-]?key)\s*[:=]\s*(?:b)?[\"'][^\"']{8,}[\"']",
       "hardcoded-secret", "owasp:a02", "cwe:321", "high", "medium",
       "a key-like value is hardcoded",
       "load the key from a secret manager or the environment",
       "a placeholder or test fixture may match the shape"),
    _r("debug-true", r"(?i)\bdebug\s*[:=]\s*true\b", "debug-mode",
       "owasp:a05", "cwe:489", "medium", "medium",
       "debug mode is enabled",
       "default debug to false and enable it only for local development",
       "a local development settings file may be intentional"),
    _r("cors-wildcard", r"(?i)(?:allow_origins\s*=\s*\[\s*[\"']\*|"
       r"Access-Control-Allow-Origin\s*:\s*\*|"
       r"\borigin\s*:\s*[\"']\*[\"'])", "cors", "owasp:a05", "cwe:942",
       "medium", "high",
       "CORS allows every origin",
       "list trusted origins and avoid credentials with a wildcard",
       "a public, read-only endpoint may intentionally be open"),
    _r("permissive-mode", r"\b0o?777\b|\bchmod\s*\([^)\n]*777",
       "file-permissions", "owasp:a05", "cwe:732", "medium", "medium",
       "a world-writable file mode is used",
       "use the narrowest mode that works and avoid umask overrides",
       "a temporary test fixture may intentionally use a permissive mode"),
    _r("default-credentials", r"(?i)\b(?:password|passwd|pwd|secret|token)"
       r"\s*[:=]\s*[\"'](?:admin|root|password|changeme|letmein|default|"
       r"123456)[\"']", "default-credentials", "owasp:a05", "cwe:1392",
       "high", "medium",
       "a default or trivial credential is hardcoded",
       "remove the default and require configuration or provisioning",
       "a documentation placeholder may match the shape"),
    _r("jwt-alg-none", r"(?i)\balgorithms?\s*=\s*\[\s*[\"']none[\"']",
       "jwt-none", "owasp:a07", "cwe:347", "critical", "high",
       "JWT verification accepts the 'none' algorithm",
       "pin an asymmetric or HMAC algorithm and reject 'none'",
       "a negative test for the none algorithm may be intentional"),
    _r("weak-password-hash", r"(?i)\b(?:md5|sha1|hashlib\.(?:md5|sha1))\b"
       r"[^\n]*(?:password|passwd|pwd)|(?:password|passwd|pwd)[^\n]*"
       r"\b(?:md5|sha1)\b", "weak-password-hash", "owasp:a07", "cwe:916",
       "high", "medium",
       "a password appears to use MD5/SHA-1",
       "use argon2, scrypt or bcrypt with a per-password salt",
       "the value may be a non-password cache key"),
    _r("insecure-random", r"(?i)(?:\brandom\.(?:random|randint|choice|"
       r"randrange)\s*\(|Math\.random\s*\(\s*\))[^\n]*"
       r"(?:token|secret|password|salt|nonce|reset|session)|"
       r"(?:token|secret|password|salt|nonce|reset|session)[^\n]*"
       r"(?:\brandom\.(?:random|randint|choice|randrange)\s*\(|"
       r"Math\.random\s*\(\s*\))", "insecure-randomness", "owasp:a04",
       "cwe:338", "medium", "low",
       "non-cryptographic randomness may be used for a security value",
       "use secrets.token_bytes/token_urlsafe or the platform CSPRNG",
       "a non-security identifier may use random intentionally",
       not_assessed=("runtime reachability", "whether the value is a secret")),
    _r("tempfile-mktemp", r"\btempfile\.mktemp\s*\(", "race-condition",
       "antipattern", "cwe:377", "low", "medium",
       "tempfile.mktemp is race-prone",
       "use tempfile.mkstemp or TemporaryDirectory",
       "a test helper may use it without a security impact",
       not_assessed=("runtime reachability", "local attacker model")),
    _r("secret-in-log", r"(?i)\b(?:logger?|logging|console|print)\b"
       r"[^\n]*(?:password|passwd|secret|access[_-]?token|"
       r"refresh[_-]?token|api[_-]?key|authorization|credential)",
       "secret-logging", "owasp:a09", "cwe:532", "low", "low",
       "a secret-like value may be written to a log",
       "log an identifier or a hash, never the credential",
       "the logged value may be a redacted field or a parameter name",
       not_assessed=("runtime reachability", "log sinks and retention")),
    _r("curl-pipe-shell", r"(?i)\b(?:curl|wget)\b[^\n|]*\|\s*"
       r"(?:sudo\s+)?(?:ba|z|k)?sh\b", "unverified-download",
       "owasp:a08", "cwe:494", "high", "high",
       "a download is piped directly into a shell",
       "download, verify a checksum/signature, then execute",
       "a pinned, verified installer may be acceptable with review",
       not_assessed=("network trust", "whether the URL is pinned")),
    _r("tls-disabled-install", r"(?i)\b(?:strict-ssl\s+false|"
       r"http\.sslVerify\s+false|pip\s+install\b[^\n]*--trusted-host)",
       "unsigned-update", "owasp:a08", "cwe:494", "medium", "high",
       "package or transport verification is disabled",
       "restore certificate verification; use a local mirror if required",
       "an isolated build environment may intentionally disable it",
       not_assessed=("network trust", "package provenance")),
    _r("no-verify", r"\b--no-verify\b", "hook-bypass", "antipattern",
       "cwe:693", "low", "medium",
       "a verification hook is explicitly bypassed",
       "remove the bypass and fix the failing check",
       "a documented emergency bypass may exist",
       not_assessed=("runtime reachability", "review policy")),
)


def _downgrade(level: str) -> str:
    try:
        return CONFIDENCES[min(CONFIDENCE_RANK[level] + 1,
                               len(CONFIDENCES) - 1)]
    except KeyError:
        return "low"


def _severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("critical", "crit"):
        return "critical"
    if text in ("high", "error"):
        return "high"
    if text in ("medium", "moderate", "warning"):
        return "medium"
    if text in ("low",):
        return "low"
    return "info"


def _finding(*, rule: str, kind: str, category: str, severity: str,
             confidence: str, file: str | None, line: int | None,
             message: str, remediation: str, false_positive_note: str,
             source_type: str, evidence: Mapping[str, Any],
             graph_path: Sequence[str] = (), cwe: Sequence[str] = (),
             owasp: Sequence[str] = (), not_assessed: Sequence[str] = (),
             source: Mapping[str, Any] | None = None,
             extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    src = {"type": source_type, "rule": rule}
    if source:
        src.update(source)
    item: dict[str, Any] = {
        "id": None,
        "category": category,
        "kind": kind,
        "severity": severity if severity in SEVERITY_RANK else "info",
        "confidence": confidence if confidence in CONFIDENCES else "low",
        "file": file,
        "line": line if isinstance(line, int) and line > 0 else None,
        "graph_path": [str(part) for part in graph_path],
        "message": message,
        "evidence": dict(evidence),
        "source": src,
        "remediation": remediation,
        "false_positive_note": false_positive_note,
        "not_assessed": list(dict.fromkeys(
            str(value) for value in (not_assessed or (
                "runtime reachability", "input validation outside this file")))),
        "cwe": list(cwe),
        "owasp": list(owasp),
        "truth_assessed": False,
    }
    if extra:
        item.update({key: value for key, value in extra.items()
                     if key not in item})
    return item


# --------------------------------------------------------------------------- #
# graph loading (read-only)
# --------------------------------------------------------------------------- #
@dataclass
class GraphIndex:
    info: dict[str, Any]
    nodes: dict[int, dict[str, Any]] = field(default_factory=dict)
    key_by_id: dict[int, str] = field(default_factory=dict)
    symbols_by_file: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict)
    symbol_by_id: dict[int, dict[str, Any]] = field(default_factory=dict)
    calls_out: dict[int, list[int]] = field(default_factory=dict)
    calls_in: dict[int, list[int]] = field(default_factory=dict)
    route_symbols: list[dict[str, Any]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.info.get("usable"))


def _git_head(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, errors="replace", timeout=10,
            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _route_labels(symbol: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    for decorator in symbol.get("decorators") or []:
        if not isinstance(decorator, str):
            continue
        match = ROUTE_DECORATOR.match(decorator.strip())
        if not match:
            continue
        path = match.group("path") or "/"
        verb = match.group("verb").lower()
        if verb == "route" or verb == "api_route":
            methods = ROUTE_METHODS.search(match.group("rest") or "")
            names = [item.upper() for item in QUOTED.findall(
                methods.group(1))] if methods else []
            methods_list = names or ["GET"]
        else:
            methods_list = [verb.upper()]
        for method in methods_list:
            label = f"route:{method} {path}"
            if label not in labels:
                labels.append(label)
    return labels


def _load_graph(root: Path, graph_path: str | Path | None) -> GraphIndex:
    path = Path(graph_path).expanduser() if graph_path is not None else (
        root / ".ozagia" / "graph.json")
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        path = Path(str(path))
    info: dict[str, Any] = {
        "path": str(path),
        "available": False,
        "freshness": "missing",
        "usable": False,
        "reason": f"no graph snapshot at {path}",
        "snapshot_root": None,
        "snapshot_head": None,
        "head": None,
        "nodes": 0,
        "edges": 0,
        "call_edges": 0,
        "route_handlers": 0,
    }
    if not path.is_file():
        return GraphIndex(info=info)
    info["available"] = True
    try:
        size = path.stat().st_size
    except OSError as exc:
        info["freshness"] = "error"
        info["reason"] = f"cannot stat graph snapshot: {exc}"
        return GraphIndex(info=info)
    if size > MAX_GRAPH_BYTES:
        info["freshness"] = "error"
        info["reason"] = (f"graph snapshot is {size} bytes, above the "
                          f"{MAX_GRAPH_BYTES}-byte scan limit")
        return GraphIndex(info=info)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        info["freshness"] = "error"
        info["reason"] = f"cannot read graph snapshot: {exc}"
        return GraphIndex(info=info)
    if not isinstance(raw, Mapping):
        info["freshness"] = "error"
        info["reason"] = "graph snapshot is not a JSON object"
        return GraphIndex(info=info)
    raw_nodes = raw.get("nodes")
    raw_edges = raw.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        info["freshness"] = "error"
        info["reason"] = "graph snapshot has no node/edge lists"
        return GraphIndex(info=info)

    index = GraphIndex(info=info)
    repo_attrs: dict[str, Any] = {}
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            continue
        try:
            nid = int(node.get("id"))
        except (TypeError, ValueError):
            continue
        entry = dict(node)
        index.nodes[nid] = entry
        key = entry.get("key")
        if isinstance(key, str) and key:
            index.key_by_id[nid] = key
        attrs = entry.get("attrs") if isinstance(entry.get("attrs"), Mapping) else {}
        typ = entry.get("type")
        if typ == "Repo":
            repo_attrs = dict(attrs)
        elif typ == "Symbol":
            path_value = attrs.get("file")
            if not isinstance(path_value, str) or not path_value:
                continue
            symbol = {
                "id": nid,
                "key": index.key_by_id.get(nid, f"sym:{path_value}"),
                "qualname": str(attrs.get("qualname") or attrs.get("name") or ""),
                "name": str(attrs.get("name") or ""),
                "file": path_value,
                "start_line": _safe_int(attrs.get("start_line")),
                "end_line": _safe_int(attrs.get("end_line")),
                "decorators": [str(item) for item in attrs.get("decorators") or []
                               if isinstance(item, str)],
            }
            index.symbols_by_file.setdefault(path_value, []).append(symbol)
            index.symbol_by_id[nid] = symbol
            if _route_labels(symbol):
                index.route_symbols.append(symbol)
    for symbols in index.symbols_by_file.values():
        symbols.sort(key=lambda item: (
            item["start_line"], item["end_line"], item["qualname"], item["id"]))
    index.route_symbols.sort(key=lambda item: (
        item["file"], item["start_line"], item["qualname"], item["id"]))

    for edge in raw_edges:
        if not isinstance(edge, Mapping):
            continue
        try:
            src, tgt = int(edge.get("src")), int(edge.get("tgt"))
        except (TypeError, ValueError):
            continue
        if str(edge.get("type") or "") != "calls":
            continue
        index.calls_out.setdefault(src, []).append(tgt)
        index.calls_in.setdefault(tgt, []).append(src)
        info["call_edges"] += 1
    for values in index.calls_out.values():
        values.sort()
    for values in index.calls_in.values():
        values.sort()

    info.update({
        "nodes": len(index.nodes),
        "edges": len(raw_edges),
        "route_handlers": len(index.route_symbols),
        "snapshot_root": repo_attrs.get("root"),
        "snapshot_head": repo_attrs.get("head"),
    })
    reasons: list[str] = []
    snapshot_root = repo_attrs.get("root")
    if snapshot_root:
        try:
            if Path(str(snapshot_root)).resolve() != root.resolve():
                reasons.append(f"snapshot root {snapshot_root} is not {root}")
        except (OSError, RuntimeError, ValueError):
            reasons.append(f"snapshot root {snapshot_root} cannot be resolved")
    if not repo_attrs.get("head"):
        reasons.append("snapshot does not record git HEAD")
    if reasons:
        info["freshness"] = "stale" if snapshot_root and (
            f"snapshot root {snapshot_root} is not {root}" in reasons) else "unknown"
        info["reason"] = "; ".join(reasons)
        info["usable"] = not any("is not" in reason for reason in reasons)
        return index

    head = _git_head(root)
    info["head"] = head
    snapshot_head = str(repo_attrs.get("head") or "")
    if head and snapshot_head == head:
        info["freshness"] = "fresh"
        info["reason"] = ("snapshot root and HEAD match the checkout; the "
                          "working tree was not re-digested")
        info["usable"] = True
    elif head:
        info["freshness"] = "stale"
        info["reason"] = (f"snapshot head {snapshot_head[:12]} does not match "
                          f"checkout HEAD {head[:12]}")
        info["usable"] = False
    else:
        info["freshness"] = "unknown"
        info["reason"] = ("snapshot root matches but the checkout HEAD could "
                          "not be read")
        info["usable"] = True
    return index


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _symbol_at(index: GraphIndex, path: str, line: int) -> dict[str, Any] | None:
    for symbol in index.symbols_by_file.get(path, []):
        start = symbol["start_line"]
        end = symbol["end_line"]
        if start and start <= line <= end:
            return symbol
    return None


def _route_ancestor(index: GraphIndex,
                    symbol: Mapping[str, Any]) -> list[dict[str, Any]]:
    direct = _route_labels(symbol)
    if direct:
        return [dict(symbol)]
    start = int(symbol["id"])
    seen = {start}
    queue: list[tuple[int, list[dict[str, Any]]]] = [(start, [])]
    while queue:
        current, chain = queue.pop(0)
        if len(chain) >= MAX_GRAPH_DEPTH:
            continue
        for caller_id in index.calls_in.get(current, []):
            if caller_id in seen:
                continue
            caller = index.symbol_by_id.get(caller_id)
            if caller is None:
                continue
            seen.add(caller_id)
            next_chain = [caller] + chain
            if _route_labels(caller):
                return next_chain + [dict(symbol)]
            queue.append((caller_id, next_chain))
    return []


def _graph_path(index: GraphIndex, symbol: Mapping[str, Any],
                sink: str) -> list[str]:
    chain = _route_ancestor(index, symbol)
    path: list[str] = []
    for item in chain:
        for label in _route_labels(item):
            path.append(label)
        key = str(item.get("key") or f"sym:{item.get('file')}")
        if key not in path:
            path.append(key)
    if not path:
        path.append(str(symbol.get("key") or f"sym:{symbol.get('file')}"))
    sink_part = f"sink:{sink}" if sink else "sink:unknown"
    path.append(sink_part)
    return path[:MAX_GRAPH_PATH]


# --------------------------------------------------------------------------- #
# source walking and scanning
# --------------------------------------------------------------------------- #
def _excluded(rel: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        text = str(pattern or "").strip()
        if not text:
            continue
        clean = text.rstrip("/")
        if fnmatch.fnmatch(rel, clean) or fnmatch.fnmatch(rel, clean + "/*"):
            return True
        if clean.endswith("/**") and rel.startswith(clean[:-3] + "/"):
            return True
        if "/" not in clean and any(part == clean for part in Path(rel).parts):
            return True
    return False


def _scan_candidates(root: Path, exclude: Sequence[str],
                     warnings: list[str]) -> list[str]:
    out: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_PARTS
            and not (rel_dir == ".github" and name == "ISSUE_TEMPLATE")
            and not _excluded((f"{rel_dir}/{name}".strip("/")), exclude))
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}".strip("/")
            if _excluded(rel, exclude):
                continue
            if name.startswith(".env"):
                suffix = ".env"
            else:
                suffix = Path(name).suffix.lower()
            if suffix not in SCAN_SUFFIXES and name not in SCAN_NAMES:
                continue
            if len(out) >= MAX_SCAN_FILES:
                truncated = True
                break
            out.append(rel)
        if truncated:
            break
    if truncated:
        warnings.append(
            f"file scan stopped at {MAX_SCAN_FILES} candidate files; the "
            "report is not a complete inventory")
    return out


def _read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


def _window(lines: Sequence[str], index: int, *, before: int = 2,
            after: int = 6) -> str:
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    return "\n".join(lines[start:end])


def _context_skip(rule_name: str, line: str, window: str) -> bool:
    if rule_name == "unsafe-yaml" and re.search(
            r"(?i)SafeLoader|safe_load", window):
        return True
    if rule_name == "template-injection" and "render_template(" in window:
        return True
    return False


def _extra_findings(rule: Rule, path: str, line_no: int, line: str,
                    window: str, symbol: Mapping[str, Any] | None,
                    index: GraphIndex) -> tuple[str, list[str], list[str]] | None:
    """Return (message_suffix, not_assessed, owasp_extra) or a skip marker."""
    if rule.name == "weak-hash":
        if re.search(r"(?i)password|passwd|pwd", line):
            return None
        if re.search(r"(?i)checksum|integrity|etag|content-md5", window):
            return (" (possibly a non-security checksum)", [], [])
    if rule.name == "hardcoded-key":
        if _PLACEHOLDER.search(line):
            return None
    if rule.name == "debug-true":
        if re.search(r"(?i)(?:test|dev|local)", path):
            return (" (development or test path)", [], [])
    if rule.name == "jwt-alg-none":
        return None
    if rule.name == "secret-in-log":
        if re.search(r"(?i)redact|mask|\*\*\*", line):
            return None
    return ("", [], [])


def _looks_opaque(value: str) -> bool:
    if len(value) < 12 or len(value) > 200:
        return False
    if _PLACEHOLDER.search(value):
        return False
    classes = sum((any(c.islower() for c in value),
                   any(c.isupper() for c in value),
                   any(c.isdigit() for c in value),
                   any(not c.isalnum() for c in value)))
    if classes < 2:
        return False
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    entropy = -sum((count / len(value)) * math.log2(count / len(value))
                   for count in counts.values())
    return entropy >= 2.8


def redact_text(text: str) -> str:
    """Defense-in-depth redaction for reports, CLI output and audit text."""
    value = str(text)
    for rule in _SECRET_RULES:
        if rule.multiline:
            value = rule.pattern.sub(f"<redacted:{rule.name}>", value)
        elif rule.value_group:
            value = rule.pattern.sub(
                lambda match, name=rule.name: (
                    f"{match.group('label')}=<redacted:{name}>"),
                value)
        else:
            value = rule.pattern.sub(f"<redacted:{rule.name}>", value)
    value = _SECRET_ASSIGN.sub(
        lambda match: f"{match.group('label')}=<redacted:secret>", value)
    return value


def _ci_findings(rel: str, text: str) -> list[dict[str, Any]]:
    if not rel.startswith(CI_PREFIXES):
        return []
    out: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        use = re.search(r"^\s*uses:\s*([^\s#]+)", line)
        if use:
            target = use.group(1)
            if target.startswith("./") or target.startswith("docker://"):
                if (target.startswith("docker://")
                        and "@sha256:" not in target):
                    out.append(_finding(
                        rule="unpinned-container-action", kind="supply-chain",
                        category="owasp:a08", severity="medium",
                        confidence="medium", file=rel, line=number,
                        message="container action is not pinned by digest",
                        remediation="pin the image with @sha256:<digest>",
                        false_positive_note="a trusted local registry may "
                                            "allow a tag",
                        source_type="filesystem",
                        evidence={"kind": "line",
                                  "rule": "unpinned-container-action",
                                  "detail": "uses: docker tag without digest"},
                        cwe=["cwe:494"], owasp=["a08"],
                        not_assessed=["image provenance",
                                      "registry trust"]))
                continue
            if "@" in target:
                ref = target.rsplit("@", 1)[1]
                if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                    continue
            out.append(_finding(
                rule="unpinned-action", kind="supply-chain",
                category="owasp:a08", severity="medium", confidence="high",
                file=rel, line=number,
                message="a CI action is not pinned to a full commit SHA",
                remediation="pin third-party actions to a full 40-character "
                            "commit SHA and review updates",
                false_positive_note="a first-party action at a version tag "
                                    "may follow repository policy",
                source_type="filesystem",
                evidence={"kind": "line", "rule": "unpinned-action",
                          "detail": "uses: reference is a tag or branch"},
                cwe=["cwe:494"], owasp=["a08"],
                not_assessed=["action repository trust",
                              "whether the tag is mutable"]))
        if re.search(r"(?i)^\s*permissions:\s*write-all\b", line):
            out.append(_finding(
                rule="ci-write-all", kind="supply-chain", category="owasp:a08",
                severity="medium", confidence="high", file=rel, line=number,
                message="CI workflow requests write-all permissions",
                remediation="grant the narrowest permissions per job",
                false_positive_note="a release workflow may need broad "
                                    "permissions with review",
                source_type="filesystem",
                evidence={"kind": "line", "rule": "ci-write-all",
                          "detail": "permissions: write-all"},
                cwe=["cwe:250"], owasp=["a08"],
                not_assessed=["workflow trigger trust"]))
    if "pull_request_target" in text:
        if re.search(r"(?i)ref\s*:\s*\$\{\{[^}]*pull_request\.head",
                     text):
            out.append(_finding(
                rule="pull-request-target-head", kind="supply-chain",
                category="owasp:a08", severity="high", confidence="high",
                file=rel, line=text[:text.find("pull_request_target")].count(
                    "\n") + 1,
                message="a pull_request_target workflow checks out the pull "
                        "request head",
                remediation="do not check out untrusted head code in a "
                            "privileged workflow; split trusted and untrusted "
                            "work",
                false_positive_note="the workflow may deliberately isolate "
                                    "the checkout from secrets",
                source_type="filesystem",
                evidence={"kind": "line", "rule": "pull-request-target-head",
                          "detail": "pull_request_target plus head ref checkout"},
                cwe=["cwe:829"], owasp=["a08"],
                not_assessed=["repository settings",
                              "secret exposure in the workflow"]))
    return out


def _scan_source(rel: str, text: str, index: GraphIndex,
                 warnings: list[str]) -> list[dict[str, Any]]:
    lines = text.splitlines()
    out: list[dict[str, Any]] = []
    graph_usable = index.usable

    def emit(rule: Rule, line_no: int, line: str,
             window: str | None = None) -> None:
        symbol = _symbol_at(index, rel, line_no) if graph_usable else None
        context = window if window is not None else line
        if _context_skip(rule.name, line, context):
            return
        extra = _extra_findings(rule, rel, line_no, line, context, symbol,
                                index)
        if extra is None:
            return
        suffix, extra_not_assessed, extra_owasp = extra
        path: list[str] = []
        source_type = "filesystem"
        evidence: dict[str, Any] = {
            "kind": "line", "rule": rule.name,
            "detail": f"matched rule {rule.name}",
        }
        confidence = rule.confidence
        if symbol is not None:
            path = _graph_path(index, symbol, rule.sink)
            source_type = "graph"
            evidence = {
                "kind": "graph_path",
                "detail": f"{symbol.get('key')} contains the matched line",
                "path": list(path),
                "snapshot_freshness": index.info.get("freshness"),
            }
            if index.info.get("freshness") != "fresh":
                confidence = _downgrade(confidence)
        owasp = [rule.category.split(":", 1)[1]] if rule.category.startswith(
            "owasp:") else []
        owasp.extend(extra_owasp)
        out.append(_finding(
            rule=rule.name, kind=rule.kind, category=rule.category,
            severity=rule.severity, confidence=confidence,
            file=rel, line=line_no,
            message=rule.message + suffix,
            remediation=rule.remediation,
            false_positive_note=rule.false_positive_note,
            source_type=source_type, evidence=evidence,
            graph_path=path, cwe=list(rule.cwe), owasp=owasp,
            not_assessed=list(rule.not_assessed) + extra_not_assessed,
            extra={"graph_snapshot_freshness": index.info.get("freshness")}
            if path else None))

    for number, line in enumerate(lines, start=1):
        for rule in _RULES:
            if not rule.pattern.search(line):
                continue
            window = _window(lines, number - 1)
            if rule.name in ("unsafe-yaml", "template-injection", "weak-hash",
                             "hardcoded-key", "debug-true", "secret-in-log"):
                emit(rule, number, line, window)
            else:
                emit(rule, number, line)
        if re.search(r"(?i)jwt\.decode\s*\(", line):
            window = _window(lines, number - 1, after=8)
            if (re.search(r"(?i)verify_signature\W+False", window)
                    or not re.search(r"(?i)algorithms\s*=", window)):
                out.append(_finding(
                    rule="jwt-unverified", kind="jwt-verification",
                    category="owasp:a07", severity="high", confidence="medium",
                    file=rel, line=number,
                    message="JWT decode does not show an algorithm or "
                            "signature check",
                    remediation="verify the signature with an explicit "
                                "algorithm allowlist and exp/aud/iss checks",
                    false_positive_note="the options may be supplied by a "
                                        "wrapper or a global configuration",
                    source_type="filesystem",
                    evidence={"kind": "line", "rule": "jwt-unverified",
                              "detail": "jwt.decode without a visible pinned "
                                        "algorithm"},
                    cwe=["cwe:347"], owasp=["a07"],
                    not_assessed=["runtime reachability",
                                  "the library's default verification mode"]))
        if re.search(r"(?i)\.set_cookie\s*\(", line) and not re.search(
                r"(?i)delete_cookie", line):
            window = _window(lines, number - 1, after=8)
            missing = []
            if not re.search(r"(?i)secure\s*=\s*True", window):
                missing.append("Secure")
            if not re.search(r"(?i)httponly\s*=\s*True", window):
                missing.append("HttpOnly")
            if not re.search(r"(?i)samesite\s*=", window):
                missing.append("SameSite")
            if missing:
                out.append(_finding(
                    rule="cookie-flags", kind="cookie-flags",
                    category="owasp:a07", severity="medium", confidence="medium",
                    file=rel, line=number,
                    message="session cookie is set without " +
                            "/".join(missing),
                    remediation="set Secure, HttpOnly and an explicit "
                                "SameSite policy",
                    false_positive_note="a global response hook or proxy may "
                                        "add the attributes",
                    source_type="filesystem",
                    evidence={"kind": "line", "rule": "cookie-flags",
                              "detail": "missing " + "/".join(missing)},
                    cwe=["cwe:614"], owasp=["a07"],
                    not_assessed=["runtime reachability",
                                  "browser and proxy policy"]))
        if _SSRF_REQUEST.search(line) and _SSRF_FETCH.search(line):
            window = _window(lines, number - 1, before=5, after=10)
            if not _SSRF_ALLOWLIST.search(window):
                symbol = _symbol_at(index, rel, number) if graph_usable else None
                path = _graph_path(index, symbol, "ssrf") if symbol else []
                out.append(_finding(
                    rule="ssrf", kind="ssrf", category="owasp:a10",
                    severity="high", confidence="low", file=rel, line=number,
                    message="a request-controlled value reaches an outbound "
                            "HTTP request without a visible allowlist",
                    remediation="validate the URL against a fixed allowlist, "
                                "resolve and reject private addresses, and "
                                "disable redirects where possible",
                    false_positive_note="the URL may be validated by a "
                                        "wrapper, proxy or egress policy",
                    source_type="graph" if path else "filesystem",
                    evidence={"kind": "graph_path" if path else "line",
                              "detail": "request value and HTTP client call "
                                        "on the same line",
                              **({"path": path} if path else {})},
                    graph_path=path, cwe=["cwe:918"], owasp=["a10"],
                    not_assessed=["runtime reachability",
                                  "egress controls and DNS policy"]))

    out.extend(_secret_findings(rel, text))
    # Framework-specific configuration checks.
    if re.search(r"\bFlask\s*\(\s*__name__\s*\)", text):
        if not re.search(r"(?i)Talisman|after_request|secure_headers|"
                         r"SecurityHeaders|helmet", text):
            out.append(_finding(
                rule="flask-missing-security-headers", kind="headers",
                category="owasp:a05", severity="low", confidence="low",
                file=rel, line=None,
                message="Flask app is created without visible security-header "
                        "middleware",
                remediation="add a reviewed security-headers middleware and "
                            "set HSTS, CSP, frame and content-type options",
                false_positive_note="headers may be set by a proxy or a "
                                    "middleware in another module",
                source_type="filesystem",
                evidence={"kind": "file",
                          "detail": "Flask(__name__) present; no header "
                                    "middleware found in this file"},
                cwe=["cwe:693"], owasp=["a05"],
                not_assessed=["deployment proxy configuration",
                              "runtime response headers"]))

    out.extend(_ci_findings(rel, text))
    return out


_SSRF_REQUEST = re.compile(
    r"(?i)\brequest\.(?:args|form|values|json|GET|POST|data)\b|"
    r"\breq\.(?:query|body|params)\b|\bparams\[|\bevent\[")
_SSRF_FETCH = re.compile(
    r"(?i)\brequests\.(?:get|post|put|patch|request)\s*\(|"
    r"\bhttpx\.(?:get|post|put|patch|request|Client)\b|"
    r"\burlopen\s*\(|\bfetch\s*\(|\baxios\.(?:get|post|put|patch)\s*\(|"
    r"\bhttp\.get\s*\(")
_SSRF_ALLOWLIST = re.compile(
    r"(?i)allowlist|allowed[_-]?(?:host|origin|url|domain)|"
    r"is_safe_url|urlparse|startswith\s*\(\s*[\"']https?://|"
    r"private[_-]?ip|ssrf")


def _route_findings(index: GraphIndex, rel: str,
                    lines: Sequence[str]) -> list[dict[str, Any]]:
    if not index.usable:
        return []
    out: list[dict[str, Any]] = []
    for symbol in index.symbols_by_file.get(rel, []):
        routes = _route_labels(symbol)
        if not routes:
            continue
        start = max(0, symbol["start_line"] - 1)
        end = min(len(lines), symbol["end_line"])
        body = "\n".join(lines[start:end])
        if AUTH_HINTS.search(body):
            continue
        for route in routes:
            if PRIVATE_ROUTE.search(route.split(" ", 1)[-1]):
                continue
            confidence = "low" if index.info.get(
                "freshness") == "fresh" else "low"
            out.append(_finding(
                rule="route-without-auth", kind="access-control",
                category="owasp:a01", severity="medium", confidence=confidence,
                file=rel, line=symbol["start_line"],
                message=f"{route} handler has no obvious permission check",
                remediation="apply an explicit authentication/authorization "
                            "dependency and test the negative case",
                false_positive_note="decorators, middleware or gateway policy "
                                    "may enforce access outside this function",
                source_type="graph",
                evidence={"kind": "graph_path",
                          "detail": f"route {route} maps to "
                                    f"{symbol.get('key')} with no visible "
                                    "auth hint",
                          "path": [route, str(symbol.get("key")),
                                   "check:permission-missing"],
                          "snapshot_freshness": index.info.get("freshness")},
                graph_path=[route, str(symbol.get("key")),
                            "check:permission-missing"],
                cwe=["cwe:862"], owasp=["a01"],
                not_assessed=["route registration and middleware",
                              "authentication and authorization policy",
                              "runtime reachability"]))
    return out


# --------------------------------------------------------------------------- #
# dependency inventory
# --------------------------------------------------------------------------- #
def _manifest_entry(path: str, ecosystem: str, components: int,
                    locked: bool, unknown_versions: int = 0,
                    note: str = "") -> dict[str, Any]:
    return {
        "path": path,
        "ecosystem": ecosystem,
        "components": components,
        "locked": locked,
        "unknown_versions": unknown_versions,
        "note": note,
    }


def _pep508_name(value: str) -> tuple[str, str | None]:
    text = value.strip()
    if not text or text.startswith(("#", "-", "git+", "http:", "https:",
                                    "file:")):
        return "", None
    text = text.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", text)
    if not match:
        return "", None
    name = match.group(1)
    version_match = re.search(r"==\s*([A-Za-z0-9_.!+*-]+)", text)
    return name, version_match.group(1) if version_match else None


def _requirements_components(path: Path) -> tuple[int, int, int] | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    names: set[str] = set()
    unknown = 0
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, version = _pep508_name(line)
        if not name:
            continue
        if name.lower() not in names:
            names.add(name.lower())
            if version is None:
                unknown += 1
    return len(names), unknown, 0


def _pyproject_components(path: Path) -> tuple[int, int] | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    values: list[str] = []
    project = data.get("project")
    if isinstance(project, Mapping):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            values.extend(str(item) for item in deps)
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for group in optional.values():
                if isinstance(group, list):
                    values.extend(str(item) for item in group)
    tool = data.get("tool")
    if isinstance(tool, Mapping):
        poetry = tool.get("poetry")
        if isinstance(poetry, Mapping):
            deps = poetry.get("dependencies")
            if isinstance(deps, Mapping):
                values.extend(str(name) for name in deps if name != "python")
    names: set[str] = set()
    unknown = 0
    for value in values:
        name, version = _pep508_name(value)
        if not name:
            continue
        if name.lower() not in names:
            names.add(name.lower())
            if version is None:
                unknown += 1
    return len(names), unknown


def _package_lock_components(path: Path) -> tuple[int, int] | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    components = 0
    unknown = 0
    packages = data.get("packages")
    if isinstance(packages, Mapping) and packages:
        for key, value in packages.items():
            if not key or not isinstance(value, Mapping):
                continue
            components += 1
            if not value.get("version"):
                unknown += 1
    else:
        def walk(deps: Any, depth: int = 0) -> None:
            nonlocal components, unknown
            if not isinstance(deps, Mapping) or depth > 5:
                return
            for name, value in deps.items():
                if not isinstance(value, Mapping):
                    continue
                components += 1
                if not value.get("version"):
                    unknown += 1
                walk(value.get("dependencies"), depth + 1)
        walk(data.get("dependencies"))
    return components, unknown


def _cargo_lock_components(path: Path) -> tuple[int, int] | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    packages = data.get("package")
    if not isinstance(packages, list):
        return None
    return len(packages), 0


def _go_mod_components(path: Path) -> tuple[int, int] | None:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    components = 0
    unknown = 0
    in_block = False
    for line in text.splitlines():
        clean = line.split("//", 1)[0].strip()
        if clean.startswith("require ("):
            in_block = True
            continue
        if in_block and clean == ")":
            in_block = False
            continue
        if clean.startswith("require "):
            parts = clean.split()
            if len(parts) >= 2:
                components += 1
                if len(parts) < 3:
                    unknown += 1
        elif in_block and clean:
            parts = clean.split()
            if len(parts) >= 2:
                components += 1
    return components, unknown


def _dependency_inventory(root: Path, *,
                          scanner_ran: bool = False) -> tuple[dict[str, Any],
                                                             list[dict[str, Any]]]:
    manifests: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    lockfiles: dict[str, bool] = {}

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        values = _pyproject_components(pyproject)
        if values is not None:
            components, unknown = values
            locked = any((root / name).is_file() for name in (
                "uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock"))
            lockfiles["pypi"] = locked
            manifests.append(_manifest_entry(
                "pyproject.toml", "pypi", components, locked, unknown,
                "lockfile not detected" if not locked else ""))

    requirements: list[Path] = []
    for pattern in ("requirements.txt", "requirements-*.txt",
                    "requirements/*.txt"):
        requirements.extend(sorted(root.glob(pattern)))
    for requirement in sorted(set(requirements))[:20]:
        values = _requirements_components(requirement)
        if values is None:
            continue
        components, unknown, _ = values
        if components:
            locked = unknown == 0
            lockfiles["pypi"] = lockfiles.get("pypi", False) or locked
            manifests.append(_manifest_entry(
                requirement.relative_to(root).as_posix(), "pypi", components,
                locked, unknown,
                "all requirements pin a version" if locked else
                "some requirements float"))

    package_json = root / "package.json"
    lock_path = next((root / name for name in (
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml") if (root / name).is_file()), None)
    if lock_path is not None:
        lockfiles["npm"] = True
        if lock_path.name in ("package-lock.json", "npm-shrinkwrap.json"):
            values = _package_lock_components(lock_path)
            if values is not None:
                components, unknown = values
                manifests.append(_manifest_entry(
                    lock_path.name, "npm", components, True, unknown))
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, Mapping):
            names: set[str] = set()
            for field_name in ("dependencies", "devDependencies",
                               "optionalDependencies"):
                deps = data.get(field_name)
                if isinstance(deps, Mapping):
                    names.update(str(name) for name in deps)
            if names and lock_path is None:
                manifests.append(_manifest_entry(
                    "package.json", "npm", len(names), False, len(names),
                    "no npm lockfile found"))
                findings.append(_finding(
                    rule="manifest-without-lockfile", kind="dependency-lock",
                    category="owasp:a06", severity="medium", confidence="medium",
                    file="package.json", line=1,
                    message="package.json declares dependencies but no npm "
                            "lockfile was found",
                    remediation="commit a lockfile and use a frozen install "
                                "in CI",
                    false_positive_note="a library may intentionally not "
                                        "commit a lockfile",
                    source_type="manifest",
                    evidence={"kind": "manifest",
                              "detail": f"{len(names)} declared dependencies; "
                                        "no package-lock.json/yarn.lock/"
                                        "pnpm-lock.yaml"},
                    cwe=["cwe:1104"], owasp=["a06"],
                    not_assessed=["CVE status", "registry resolution"]))
            elif names and lock_path is not None:
                has_components = any(
                    item["path"] == lock_path.name for item in manifests)
                if not has_components:
                    manifests.append(_manifest_entry(
                        "package.json", "npm", len(names), True, 0,
                        f"locked by {lock_path.name}; component versions are "
                        "not parsed from this lock format"))

    cargo_lock = root / "Cargo.lock"
    if cargo_lock.is_file():
        values = _cargo_lock_components(cargo_lock)
        if values is not None:
            components, unknown = values
            lockfiles["cargo"] = True
            manifests.append(_manifest_entry(
                "Cargo.lock", "cargo", components, True, unknown))

    go_mod = root / "go.mod"
    if go_mod.is_file():
        values = _go_mod_components(go_mod)
        if values is not None:
            components, unknown = values
            locked = (root / "go.sum").is_file()
            lockfiles["go"] = locked
            manifests.append(_manifest_entry(
                "go.mod", "go", components, locked, unknown,
                "" if locked else "go.sum not found"))

    manifests.sort(key=lambda item: (item["path"], item["ecosystem"]))
    total_components = sum(item["components"] for item in manifests)
    unknown_versions = sum(item["unknown_versions"] for item in manifests)
    locked = bool(manifests) and all(item["locked"] for item in manifests)
    inventory = {
        "manifests": manifests,
        "components": total_components,
        "unknown_versions": unknown_versions,
        "locked": locked,
        "lockfiles": dict(sorted(lockfiles.items())),
        "cve_status": ("scanner_reported_not_exhaustive" if scanner_ran
                       else "not_assessed"),
        "scanner_ran": scanner_ran,
        "note": ("the inventory is not a vulnerability assessment; scanner "
                 "output is not a clean result"
                 if scanner_ran else
                 "the inventory is not a vulnerability assessment; no "
                 "dependency database was consulted"),
    }
    if total_components:
        cve_status = inventory["cve_status"]
        if scanner_ran:
            message = (
                f"inventoried {total_components} dependency component(s) "
                f"across {len(manifests)} manifest(s); CVE status is limited "
                "to the named scanner output, not a clean result")
            remediation = (
                "review the scanner results and rerun them with an explicit "
                "database refresh; this scan does not claim the components "
                "are safe")
        else:
            message = (
                f"inventoried {total_components} dependency component(s) "
                f"across {len(manifests)} manifest(s); CVE status is not "
                "assessed")
            remediation = (
                "run an explicit dependency scanner (for example pip-audit "
                "or osv-scanner) and review the result; this scan did not")
        findings.append(_finding(
            rule="dependency-inventory", kind="dependency-inventory",
            category="dependency", severity="low", confidence="high",
            file=manifests[0]["path"], line=1,
            message=message,
            remediation=remediation,
            false_positive_note="an inventory is not a vulnerability claim",
            source_type="manifest",
            evidence={"kind": "manifest",
                      "detail": f"{len(manifests)} manifest(s), "
                                f"{unknown_versions} component(s) without an "
                                "exact version",
                      "manifests": [item["path"] for item in manifests],
                      "cve_status": cve_status},
            cwe=["cwe:1104"], owasp=["a06"],
            not_assessed=["CVE and advisory status",
                          "transitive dependency resolution",
                          "licence and package provenance"],
            extra={"cve_status": cve_status}))
    return inventory, findings


# --------------------------------------------------------------------------- #
# external tool registry (fixed commands, no shell, no auto-install)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolSpec:
    name: str
    executable: str
    argv: tuple[str, ...]
    kind: str
    network: bool
    parser: str
    description: str
    version_argv: tuple[str, ...] = ("--version",)
    ecosystem: str = ""


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec for spec in (
        ToolSpec("pip-audit", "pip-audit", ("--format", "json"),
                 "dependency", True, "pip-audit",
                 "Python dependency advisories", ecosystem="pypi"),
        ToolSpec("npm", "npm", ("audit", "--json"), "dependency", True,
                 "npm-audit", "npm dependency advisories", ecosystem="npm"),
        ToolSpec("cargo", "cargo", ("audit", "--json"), "dependency", True,
                 "cargo-audit", "Rust dependency advisories",
                 ecosystem="cargo"),
        ToolSpec("govulncheck", "govulncheck", ("-json", "./..."),
                 "dependency", True, "govulncheck",
                 "Go vulnerability check", ecosystem="go"),
        ToolSpec("osv-scanner", "osv-scanner",
                 ("--format", "json", "-r", "."), "dependency", True,
                 "osv", "OSV scanner across manifests", ecosystem="all"),
        ToolSpec("semgrep", "semgrep",
                 ("scan", "--sarif", "--quiet", "--config", "auto", "."),
                 "sast", True, "sarif",
                 "Semgrep SAST (auto rules require the network)"),
        ToolSpec("bandit", "bandit", ("-f", "json", "-r", "."), "sast", False,
                 "bandit", "Python SAST"),
        ToolSpec("gitleaks", "gitleaks",
                 ("detect", "--no-git", "--redact", "--report-format", "json",
                  "--report-path", "-"), "secret", False, "gitleaks",
                 "Secret detection without git history"),
        ToolSpec("trivy", "trivy",
                 ("fs", "--format", "json", "--quiet", "--scanners",
                  "vuln,secret", "."), "dependency", True, "trivy",
                 "Filesystem dependency and secret scanner", ecosystem="all"),
    )
}


def tools() -> list[dict[str, Any]]:
    """Report the fixed registry and local availability. Nothing is run."""
    out: list[dict[str, Any]] = []
    for spec in TOOL_REGISTRY.values():
        path = shutil.which(spec.executable)
        out.append({
            "name": spec.name,
            "kind": spec.kind,
            "description": spec.description,
            "network_required": spec.network,
            "ecosystem": spec.ecosystem,
            "status": "available" if path else "unavailable",
            "path": path,
        })
    return out


def _tool_command(spec: ToolSpec, root: Path) -> list[str]:
    return [spec.executable, *spec.argv]


def _tool_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("LC_ALL", "C")
    return env


def _invoke(argv: Sequence[str], *, root: Path, timeout: int,
            runner: Callable[..., Any] | None) -> Mapping[str, Any]:
    if runner is not None:
        result = runner(list(argv), timeout=timeout, cwd=str(root))
        if isinstance(result, Mapping):
            return {
                "returncode": int(result.get("returncode",
                                             result.get("exit_code", 1))),
                "stdout": str(result.get("stdout") or ""),
                "stderr": str(result.get("stderr") or ""),
                "version": result.get("version"),
                "database": result.get("database"),
            }
        return {
            "returncode": int(getattr(result, "returncode", 1)),
            "stdout": str(getattr(result, "stdout", "") or ""),
            "stderr": str(getattr(result, "stderr", "") or ""),
        }
    proc = subprocess.run(
        list(argv), cwd=str(root), env=_tool_env(), capture_output=True,
        text=True, errors="replace", timeout=timeout,
        stdin=subprocess.DEVNULL, shell=False, check=False)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def _tool_version(spec: ToolSpec, root: Path,
                  runner: Callable[..., Any] | None,
                  warnings: list[str]) -> str | None:
    if runner is not None:
        # The injected runner owns the command surface; the version comes
        # from the main result if it supplies one.
        return None
    try:
        result = _invoke([spec.executable, *spec.version_argv], root=root,
                         timeout=VERSION_TIMEOUT_S, runner=None)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        warnings.append(f"{spec.name} version could not be read: "
                        f"{type(exc).__name__}")
        return None
    text = str(result.get("stdout") or "").strip()
    if not text:
        text = str(result.get("stderr") or "").strip()
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line[:200] or None


def _rel(root: Path, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.startswith("file://"):
        text = text[7:]
    candidate = Path(text)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    return text.replace("\\", "/")


def _tool_finding(*, tool: str, version: str | None, command: Sequence[str],
                  exit_code: int, database: Any, rule: str, kind: str,
                  category: str, severity: str, confidence: str,
                  file: str | None, line: int | None, message: str,
                  remediation: str, cwe: Sequence[str] = (),
                  owasp: Sequence[str] = (),
                  not_assessed: Sequence[str] = (),
                  extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = {
        "tool": tool,
        "version": version,
        "command": list(command),
        "exit_code": exit_code,
        "database": database if isinstance(database, Mapping) else {
            "status": "unreported"},
    }
    return _finding(
        rule=rule, kind=kind, category=category, severity=severity,
        confidence=confidence, file=file, line=line, message=message,
        remediation=remediation,
        false_positive_note="reported by an external tool; the finding and "
                            "its database may be stale or incomplete",
        source_type="tool", source=source,
        evidence={"kind": "tool", "tool": tool, "rule": rule,
                  "command": list(command), "exit_code": exit_code,
                  "database": source["database"]},
        cwe=cwe, owasp=owasp,
        not_assessed=list(not_assessed) or (
            "scanner database completeness", "runtime reachability"),
        extra=extra)


def _parse_pip_audit(data: Any, *, tool: str, version: str | None,
                     command: Sequence[str], exit_code: int, database: Any,
                     root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    deps = data.get("dependencies")
    if not isinstance(deps, list):
        return out
    for dep in deps:
        if not isinstance(dep, Mapping):
            continue
        name = str(dep.get("name") or "unknown")
        dep_version = str(dep.get("version") or "unknown")
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, Mapping):
                continue
            ident = str(vuln.get("id") or "vulnerability")
            fix = ", ".join(str(value) for value in vuln.get("fix_versions") or [])
            out.append(_tool_finding(
                tool=tool, version=version, command=command,
                exit_code=exit_code, database=database,
                rule=f"pip-audit:{ident}", kind="dependency-vulnerability",
                category="dependency",
                severity=_severity(vuln.get("severity") or "medium"),
                confidence="medium", file=None, line=None,
                message=f"{name} {dep_version}: {ident}" +
                        (f" - {vuln.get('description')}"[:300]
                         if vuln.get("description") else ""),
                remediation=(f"upgrade {name} to a fixed version ({fix})"
                             if fix else f"upgrade or replace {name}"),
                cwe=["cwe:1104"], owasp=["a06"],
                extra={"package": name, "package_version": dep_version,
                       "advisory": ident, "fixed_versions": fix}))
    return out


def _parse_npm(data: Any, *, tool: str, version: str | None,
               command: Sequence[str], exit_code: int, database: Any,
               root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    vulns = data.get("vulnerabilities")
    if not isinstance(vulns, Mapping):
        return out
    for name, item in vulns.items():
        if not isinstance(item, Mapping):
            continue
        severity = _severity(item.get("severity"))
        via = item.get("via")
        advisories = [entry for entry in via or [] if isinstance(entry, Mapping)]
        title = str(advisories[0].get("title")) if advisories else ""
        cwe = []
        for entry in advisories:
            for value in entry.get("cwe") or []:
                match = re.search(r"CWE-(\d+)", str(value))
                if match:
                    cwe.append(f"cwe:{match.group(1)}")
        fix = item.get("fixAvailable")
        out.append(_tool_finding(
            tool=tool, version=version, command=command, exit_code=exit_code,
            database=database, rule=f"npm-audit:{name}", kind="dependency-vulnerability",
            category="dependency", severity=severity, confidence="medium",
            file=None, line=None,
            message=f"{name}: {title or 'advisory'}",
            remediation=(f"update {name} to a fixed version"
                         if fix else f"review and replace {name}"),
            cwe=cwe, owasp=["a06"],
            extra={"package": str(name), "advisory": title,
                   "fixed_versions": fix if isinstance(fix, str) else ""}))
    return out


def _parse_cargo(data: Any, *, tool: str, version: str | None,
                 command: Sequence[str], exit_code: int, database: Any,
                 root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    vulnerabilities = data.get("vulnerabilities")
    entries = vulnerabilities.get("list") if isinstance(
        vulnerabilities, Mapping) else None
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        advisory = entry.get("advisory") if isinstance(
            entry.get("advisory"), Mapping) else {}
        package = entry.get("package") if isinstance(
            entry.get("package"), Mapping) else {}
        name = str(package.get("name") or advisory.get("package") or "unknown")
        version_text = str(package.get("version") or "unknown")
        ident = str(advisory.get("id") or "advisory")
        patched = entry.get("versions") if isinstance(
            entry.get("versions"), Mapping) else {}
        fixed = ", ".join(str(value) for value in patched.get("patched") or [])
        out.append(_tool_finding(
            tool=tool, version=version, command=command, exit_code=exit_code,
            database=database, rule=f"cargo-audit:{ident}",
            kind="dependency-vulnerability", category="dependency",
            severity=_severity(advisory.get("severity")), confidence="medium",
            file=None, line=None,
            message=f"{name} {version_text}: {ident} - "
                    f"{advisory.get('title') or advisory.get('description') or ''}"[:400],
            remediation=(f"upgrade {name} ({fixed})" if fixed
                         else f"upgrade or replace {name}"),
            cwe=["cwe:1104"], owasp=["a06"],
            extra={"package": name, "package_version": version_text,
                   "advisory": ident, "fixed_versions": fixed}))
    return out


def _parse_osv(data: Any, *, tool: str, version: str | None,
               command: Sequence[str], exit_code: int, database: Any,
               root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    results = data.get("results")
    if not isinstance(results, list):
        return out
    for result in results:
        if not isinstance(result, Mapping):
            continue
        source = result.get("source") if isinstance(
            result.get("source"), Mapping) else {}
        file_path = _rel(root, source.get("path"))
        for package in result.get("packages") or []:
            if not isinstance(package, Mapping):
                continue
            meta = package.get("package") if isinstance(
                package.get("package"), Mapping) else {}
            name = str(meta.get("name") or "unknown")
            pkg_version = str(meta.get("version") or "unknown")
            for vuln in package.get("vulnerabilities") or []:
                if not isinstance(vuln, Mapping):
                    continue
                ident = str(vuln.get("id") or "vulnerability")
                severity = _severity(
                    (vuln.get("database_specific") or {}).get("severity")
                    if isinstance(vuln.get("database_specific"), Mapping)
                    else None)
                out.append(_tool_finding(
                    tool=tool, version=version, command=command,
                    exit_code=exit_code, database=database,
                    rule=f"osv:{ident}", kind="dependency-vulnerability",
                    category="dependency", severity=severity,
                    confidence="medium", file=file_path, line=None,
                    message=f"{name} {pkg_version}: {ident} - "
                            f"{vuln.get('summary') or 'advisory'}"[:400],
                    remediation=f"upgrade or replace {name}",
                    cwe=["cwe:1104"], owasp=["a06"],
                    extra={"package": name, "package_version": pkg_version,
                           "advisory": ident}))
    return out


def _parse_govulncheck(data: Any, *, tool: str, version: str | None,
                       command: Sequence[str], exit_code: int, database: Any,
                       root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(data, Mapping):
        data = [data]
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping):
            continue
        finding = item.get("finding")
        if not isinstance(finding, Mapping):
            continue
        osv = str(finding.get("osv") or "vulnerability")
        trace = finding.get("trace") if isinstance(
            finding.get("trace"), Mapping) else {}
        module = str(trace.get("module") or "module")
        function = str(trace.get("function") or "")
        file_path = _rel(root, trace.get("position", {}).get("filename")
                         if isinstance(trace.get("position"), Mapping) else None)
        out.append(_tool_finding(
            tool=tool, version=version, command=command, exit_code=exit_code,
            database=database, rule=f"govulncheck:{osv}",
            kind="dependency-vulnerability", category="dependency",
            severity="high", confidence="medium", file=file_path, line=None,
            message=f"{module} {function}: {osv}",
            remediation="upgrade the Go module to a fixed version",
            cwe=["cwe:1104"], owasp=["a06"],
            extra={"package": module, "advisory": osv}))
    return out


def _parse_sarif(data: Any, *, tool: str, version: str | None,
                 command: Sequence[str], exit_code: int, database: Any,
                 root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    runs = data.get("runs")
    if not isinstance(runs, list):
        return out
    for run in runs:
        if not isinstance(run, Mapping):
            continue
        driver = (run.get("tool") or {}).get("driver") if isinstance(
            run.get("tool"), Mapping) else {}
        tool_name = str((driver or {}).get("name") or tool)
        for result in run.get("results") or []:
            if not isinstance(result, Mapping):
                continue
            rule = str(result.get("ruleId") or "sarif")
            level = _severity(result.get("level"))
            locations = result.get("locations")
            location = locations[0] if isinstance(locations, list) and locations else {}
            physical = location.get("physicalLocation") if isinstance(
                location, Mapping) else {}
            artifact = physical.get("artifactLocation") if isinstance(
                physical, Mapping) else {}
            region = physical.get("region") if isinstance(
                physical, Mapping) else {}
            message = result.get("message") if isinstance(
                result.get("message"), Mapping) else {}
            out.append(_tool_finding(
                tool=tool, version=version, command=command,
                exit_code=exit_code, database=database,
                rule=f"{tool_name}:{rule}", kind="static-analysis",
                category="antipattern", severity=level, confidence="medium",
                file=_rel(root, (artifact or {}).get("uri")),
                line=_safe_int((region or {}).get("startLine")) or None,
                message=str((message or {}).get("text") or rule)[:400],
                remediation="review the rule and the data flow; this scan "
                            "does not prove exploitability",
                extra={"scanner_rule": rule}))
    return out


def _parse_bandit(data: Any, *, tool: str, version: str | None,
                  command: Sequence[str], exit_code: int, database: Any,
                  root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    for item in data.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        rule = str(item.get("test_id") or item.get("test_name") or "bandit")
        severity = _severity(item.get("issue_severity"))
        out.append(_tool_finding(
            tool=tool, version=version, command=command,
            exit_code=exit_code, database=database,
            rule=f"bandit:{rule}", kind="static-analysis",
            category="antipattern", severity=severity,
            confidence="medium", file=_rel(root, item.get("filename")),
            line=_safe_int(item.get("line_number")) or None,
            message=str(item.get("issue_text") or rule)[:400],
            remediation="review the rule and the data flow; this scan does "
                        "not prove exploitability",
            cwe=[f"cwe:{item['issue_cwe']['id']}"]
            if isinstance(item.get("issue_cwe"), Mapping)
            and item["issue_cwe"].get("id") else [],
            extra={"scanner_rule": rule,
                   "tool_confidence": str(item.get("issue_confidence") or "")}))
    return out


def _parse_gitleaks(data: Any, *, tool: str, version: str | None,
                    command: Sequence[str], exit_code: int, database: Any,
                    root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    entries = data if isinstance(data, list) else (
        data.get("findings") if isinstance(data, Mapping) else [])
    for item in entries or []:
        if not isinstance(item, Mapping):
            continue
        rule = str(item.get("RuleID") or item.get("ruleId") or "gitleaks")
        raw_value = item.get("Secret") or item.get("secret") or item.get(
            "Match") or item.get("match")
        fingerprint = (secret_fingerprint(str(raw_value))
                       if isinstance(raw_value, str) and raw_value else None)
        out.append(_tool_finding(
            tool=tool, version=version, command=command,
            exit_code=exit_code, database=database,
            rule=f"gitleaks:{rule}", kind="secret-like-literal",
            category="secret", severity="high", confidence="medium",
            file=_rel(root, item.get("File") or item.get("file")),
            line=_safe_int(item.get("StartLine") or item.get("startLine"))
            or None,
            message=f"gitleaks rule {rule} matched; the value is withheld"
            + (f" (fingerprint {fingerprint})" if fingerprint else ""),
            remediation="revoke and rotate the credential, then load it from "
                        "a secret manager",
            cwe=["cwe:798"], owasp=["a02"],
            not_assessed=["whether the value is live",
                          "scanner rule false positives"],
            extra={"secret_value_withheld": True,
                   "secret_rule": rule,
                   "secret_fingerprint": fingerprint}))
    return out


def _parse_trivy(data: Any, *, tool: str, version: str | None,
                 command: Sequence[str], exit_code: int, database: Any,
                 root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(data, Mapping):
        return out
    for result in data.get("Results") or []:
        if not isinstance(result, Mapping):
            continue
        target = _rel(root, result.get("Target"))
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, Mapping):
                continue
            ident = str(vuln.get("VulnerabilityID") or "vulnerability")
            package = str(vuln.get("PkgName") or "unknown")
            installed = str(vuln.get("InstalledVersion") or "unknown")
            fixed = str(vuln.get("FixedVersion") or "")
            out.append(_tool_finding(
                tool=tool, version=version, command=command,
                exit_code=exit_code, database=database,
                rule=f"trivy:{ident}", kind="dependency-vulnerability",
                category="dependency", severity=_severity(vuln.get("Severity")),
                confidence="medium", file=target, line=None,
                message=f"{package} {installed}: {ident} - "
                        f"{vuln.get('Title') or 'advisory'}"[:400],
                remediation=(f"upgrade {package} ({fixed})" if fixed
                             else f"review {package}"),
                cwe=["cwe:1104"], owasp=["a06"],
                extra={"package": package, "package_version": installed,
                       "advisory": ident, "fixed_versions": fixed}))
        for secret in result.get("Secrets") or []:
            if not isinstance(secret, Mapping):
                continue
            rule = str(secret.get("RuleID") or "secret")
            out.append(_tool_finding(
                tool=tool, version=version, command=command,
                exit_code=exit_code, database=database,
                rule=f"trivy-secret:{rule}", kind="secret-like-literal",
                category="secret", severity=_severity(secret.get("Severity")),
                confidence="medium", file=target,
                line=_safe_int(secret.get("StartLine")) or None,
                message=f"trivy secret rule {rule} matched; the value is "
                        "withheld",
                remediation="revoke and rotate the credential",
                cwe=["cwe:798"], owasp=["a02"],
                extra={"secret_value_withheld": True}))
    return out


_PARSERS = {
    "pip-audit": _parse_pip_audit,
    "npm-audit": _parse_npm,
    "cargo-audit": _parse_cargo,
    "osv": _parse_osv,
    "govulncheck": _parse_govulncheck,
    "sarif": _parse_sarif,
    "bandit": _parse_bandit,
    "gitleaks": _parse_gitleaks,
    "trivy": _parse_trivy,
}


def _database_info(data: Any, stdout: str) -> dict[str, Any]:
    if isinstance(data, Mapping):
        for key in ("database", "metadata", "auditReportVersion"):
            value = data.get(key)
            if isinstance(value, Mapping):
                stamp = (value.get("timestamp") or value.get("generated_at")
                         or value.get("updated_at") or value.get("time"))
                if stamp:
                    return {"status": "reported", "time": str(stamp)[:80]}
        runs = data.get("runs")
        if isinstance(runs, list) and runs and isinstance(runs[0], Mapping):
            invocations = runs[0].get("invocations")
            if isinstance(invocations, list) and invocations:
                stamp = invocations[0].get("startTimeUtc") if isinstance(
                    invocations[0], Mapping) else None
                if stamp:
                    return {"status": "reported", "time": str(stamp)[:80]}
    return {"status": "unreported",
            "note": "tool output did not include a database timestamp"}


def _parse_tool_output(spec: ToolSpec, stdout: str, *, version: str | None,
                       command: Sequence[str], exit_code: int,
                       root: Path) -> tuple[list[dict[str, Any]],
                                             dict[str, Any],
                                             str | None]:
    if not stdout.strip():
        return [], {"status": "unreported"}, "tool produced no output"
    if len(stdout.encode("utf-8", errors="replace")) > MAX_TOOL_OUTPUT_BYTES:
        return [], {"status": "unreported"}, (
            f"tool output exceeded the {MAX_TOOL_OUTPUT_BYTES}-byte cap")
    data: Any = None
    try:
        data = json.loads(stdout)
    except ValueError:
        # govulncheck emits one JSON object per line.
        if spec.parser == "govulncheck":
            entries = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
            data = entries or None
        if data is None:
            return [], {"status": "unreported"}, "tool output was not valid JSON"
    database = _database_info(data, stdout)
    parser = _PARSERS.get(spec.parser)
    if parser is None:
        return [], database, f"no parser for {spec.parser}"
    findings = parser(
        data, tool=spec.name, version=version, command=command,
        exit_code=exit_code, database=database, root=root)
    return findings[:MAX_TOOL_FINDINGS], database, None


def _run_tool(spec: ToolSpec, root: Path,
              runner: Callable[..., Any] | None,
              warnings: list[str]) -> tuple[dict[str, Any],
                                            list[dict[str, Any]]]:
    command = _tool_command(spec, root)
    result: dict[str, Any] = {
        "name": spec.name,
        "kind": spec.kind,
        "command": command,
        "network_required": spec.network,
        "status": "error",
        "version": None,
        "exit_code": None,
        "database": {"status": "unreported"},
        "findings": 0,
        "injected": runner is not None,
        "error": None,
    }
    try:
        result["version"] = _tool_version(spec, root, runner, warnings)
        invoked = _invoke(command, root=root, timeout=TOOL_TIMEOUT_S,
                          runner=runner)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return result, []
    exit_code = int(invoked.get("returncode", 1))
    result["exit_code"] = exit_code
    if invoked.get("version"):
        result["version"] = str(invoked["version"])[:200]
    parsed, database, error = _parse_tool_output(
        spec, str(invoked.get("stdout") or ""),
        version=result["version"], command=command, exit_code=exit_code,
        root=root)
    if invoked.get("database"):
        value = invoked["database"]
        if isinstance(value, Mapping):
            if "time" in value or "status" in value:
                database = dict(value)
            else:
                stamp = (value.get("timestamp") or value.get("generated_at")
                         or value.get("updated_at"))
                database = ({"status": "reported", "time": str(stamp)[:80]}
                            if stamp else
                            {"status": "unreported",
                             "note": "runner did not supply a database time"})
        else:
            database = {"status": "reported", "time": str(value)[:80]}
    result["database"] = database
    if error:
        result["error"] = error
        return result, []
    result["status"] = "ok"
    result["findings"] = len(parsed)
    return result, parsed


def _run_tools(names: Sequence[str], root: Path,
               allow_network: bool, runner: Callable[..., Any] | None,
               warnings: list[str]) -> tuple[list[dict[str, Any]],
                                              list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for name in names:
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            raise SecurityError(
                f"unknown tool {name!r}; known tools: "
                f"{', '.join(TOOL_REGISTRY)}")
        available = runner is not None or shutil.which(spec.executable) is not None
        if not available:
            results.append({
                "name": spec.name, "kind": spec.kind,
                "command": _tool_command(spec, root),
                "network_required": spec.network, "status": "unavailable",
                "version": None, "exit_code": None,
                "database": {"status": "unreported"}, "findings": 0,
                "injected": False,
                "error": f"{spec.executable} is not installed",
            })
            warnings.append(f"{spec.name} is unavailable; no result was "
                            "inferred")
            continue
        if spec.network and not allow_network:
            results.append({
                "name": spec.name, "kind": spec.kind,
                "command": _tool_command(spec, root),
                "network_required": True, "status": "refused",
                "version": None, "exit_code": None,
                "database": {"status": "unreported"}, "findings": 0,
                "injected": runner is not None,
                "error": "requires network access; pass --allow-network to "
                         "run it explicitly",
            })
            warnings.append(f"{spec.name} was refused because network access "
                            "was not allowed")
            continue
        result, parsed = _run_tool(spec, root, runner, warnings)
        results.append(result)
        findings.extend(parsed)
    return results, findings


# --------------------------------------------------------------------------- #
# scan orchestration
# --------------------------------------------------------------------------- #
def _load_security_config(root: Path) -> dict[str, Any]:
    path = root / ".ozagia.toml"
    config: dict[str, Any] = {
        "enabled": True,
        "tools": [],
        "allow_network": False,
        "fail_on": DEFAULT_FAIL_ON,
        "exclude": list(DEFAULT_EXCLUDES),
        "max_findings": DEFAULT_MAX_FINDINGS,
        "block_secrets": True,
        "secret_allowlist": [],
    }
    if not path.is_file():
        return config
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return config
    section = data.get("security")
    if not isinstance(section, Mapping):
        return config
    if isinstance(section.get("enabled"), bool):
        config["enabled"] = section["enabled"]
    tools_value = section.get("tools")
    if isinstance(tools_value, list):
        config["tools"] = [str(item) for item in tools_value
                           if isinstance(item, str)]
    if isinstance(section.get("allow_network"), bool):
        config["allow_network"] = section["allow_network"]
    fail_on = str(section.get("fail_on") or "").lower()
    if fail_on in FAIL_ON:
        config["fail_on"] = fail_on
    exclude = section.get("exclude")
    if isinstance(exclude, list):
        config["exclude"] = [str(item) for item in exclude
                             if isinstance(item, str)]
    cap = _safe_int(section.get("max_findings"), DEFAULT_MAX_FINDINGS)
    config["max_findings"] = max(1, min(cap, MAX_FINDINGS))
    if isinstance(section.get("block_secrets"), bool):
        config["block_secrets"] = section["block_secrets"]
    allowlist = section.get("secret_allowlist")
    if isinstance(allowlist, list):
        config["secret_allowlist"] = [str(item) for item in allowlist
                                      if isinstance(item, str)]
    return config


def load_config(root: str | Path = ".") -> dict[str, Any]:
    """The effective ``[security]`` settings; no scan and no writes."""
    return _load_security_config(Path(root).expanduser().resolve())


def _finalize_findings(findings: Sequence[Mapping[str, Any]],
                       cap: int) -> tuple[list[dict[str, Any]], int, int]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in findings:
        item = dict(raw)
        item["graph_path"] = [str(part) for part in item.get("graph_path") or []]
        item["cwe"] = sorted(set(str(value) for value in item.get("cwe") or []))
        item["owasp"] = sorted(set(
            str(value) for value in item.get("owasp") or []))
        item["not_assessed"] = [redact_text(str(value)) for value in
                                dict.fromkeys(item.get("not_assessed") or [])]
        for key in ("message", "remediation", "false_positive_note"):
            if item.get(key) is not None:
                item[key] = redact_text(str(item[key]))
        evidence = item.get("evidence")
        if isinstance(evidence, Mapping):
            item["evidence"] = {
                str(key): (redact_text(value) if isinstance(value, str)
                           else value)
                for key, value in evidence.items()}
        source = item.get("source") if isinstance(item.get("source"),
                                                  Mapping) else {}
        rule = str(source.get("rule") or "")
        key = (rule, item.get("file"), item.get("line"), item.get("kind"),
               item.get("message"))
        existing = unique.get(key)
        if existing is None:
            unique[key] = item
            continue
        # Prefer graph context, then confidence, then a tool's provenance.
        existing_score = (bool(existing.get("graph_path")),
                          -CONFIDENCE_RANK.get(
                              str(existing.get("confidence")), 2),
                          str(existing.get("source", {}).get("type")) == "tool")
        new_score = (bool(item.get("graph_path")),
                     -CONFIDENCE_RANK.get(str(item.get("confidence")), 2),
                     str(source.get("type")) == "tool")
        if new_score > existing_score:
            unique[key] = item

    def line_key(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted(
        unique.values(),
        key=lambda item: (
            SEVERITY_RANK.get(str(item.get("severity")), 99),
            CONFIDENCE_RANK.get(str(item.get("confidence")), 9),
            str(item.get("file") or ""),
            line_key(item.get("line")),
            str(item.get("source", {}).get("rule") or ""),
            str(item.get("message") or ""),
        ))
    for item in ordered:
        digest = hashlib.sha256(
            "|".join((
                str(item.get("source", {}).get("rule") or ""),
                str(item.get("file") or ""),
                str(item.get("line") or ""),
                str(item.get("kind") or ""),
                str(item.get("message") or ""),
            )).encode("utf-8")).hexdigest()[:12]
        item["id"] = f"sec-{digest}"
    kept = ordered[:cap]
    return kept, len(ordered), max(0, len(ordered) - len(kept))


def _dependency_not_assessed(inventory: Mapping[str, Any],
                             tool_results: Sequence[Mapping[str, Any]]
                             ) -> list[str]:
    if any(result.get("status") == "ok" for result in tool_results):
        return ["scanner database completeness and recency",
                "transitive dependencies the scanner did not resolve",
                "runtime reachability of the vulnerable component"]
    return ["CVE and advisory status: no dependency scanner ran",
            "transitive dependency resolution",
            "runtime reachability and exploitability"]


def scan(root: str | Path = ".", *, graph_path: str | Path | None = None,
         tools: Sequence[str] | None = None,
         allow_network: bool | None = None,
         runner: Callable[..., Any] | None = None,
         exclude: Sequence[str] | None = None,
         max_findings: int | None = None,
         fail_on: str | None = None,
         staged: bool = False,
         diff: str | None = None) -> dict[str, Any]:
    """Scan one checkout offline and return a deterministic report.

    Only tools named in ``tools`` (or ``[security].tools`` in
    ``.ozagia.toml``) are run. A network-requiring tool is refused unless
    ``allow_network`` is true. A missing tool is reported as ``unavailable``,
    never installed and never treated as a failure. ``runner`` injects a
    caller-owned command executor (used by tests and integration harnesses);
    it is not a model and not a shell.
    """
    root_path = Path(root).expanduser()
    try:
        root_path = root_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise SecurityError(f"cannot resolve root {root!r}: {exc}") from exc
    if not root_path.is_dir():
        raise SecurityError(f"root is not a directory: {root_path}")
    if staged and diff:
        raise SecurityError("choose at most one of --staged or --diff")
    config = _load_security_config(root_path)
    requested = (list(tools) if tools is not None else list(config["tools"]))
    requested = list(dict.fromkeys(name for name in requested if name))
    for name in requested:
        if name not in TOOL_REGISTRY:
            raise SecurityError(
                f"unknown tool {name!r}; known tools: "
                f"{', '.join(TOOL_REGISTRY)}")
    allow = config["allow_network"] if allow_network is None else bool(
        allow_network)
    if fail_on is None:
        fail_on = config["fail_on"]
    if fail_on not in FAIL_ON:
        raise SecurityError(f"fail_on must be one of {FAIL_ON}")
    cap = max(1, min(_safe_int(max_findings, config["max_findings"]),
                     MAX_FINDINGS))
    patterns = tuple(exclude) if exclude is not None else tuple(
        config["exclude"])

    warnings: list[str] = []
    index = _load_graph(root_path, graph_path)
    if not index.info.get("available"):
        warnings.append(
            "graph snapshot missing; graph-derived route and sink paths "
            "were not applied")
    elif not index.usable:
        warnings.append(
            "graph snapshot is stale or unverified; graph-derived paths "
            "were not used")
    candidates = _scan_candidates(root_path, patterns, warnings)
    source_findings: list[dict[str, Any]] = []
    files_scanned = 0
    files_skipped = 0
    for rel in candidates:
        credential = credential_file_hit(rel)
        if credential is not None:
            source_findings.append(secret_finding(rel, credential))
        text = _read_text(root_path / rel)
        if text is None:
            files_skipped += 1
            continue
        files_scanned += 1
        source_findings.extend(_scan_source(rel, text, index, warnings))
        source_findings.extend(
            _route_findings(index, rel, text.splitlines()))

    tool_results: list[dict[str, Any]] = []
    tool_findings: list[dict[str, Any]] = []
    if requested:
        tool_results, tool_findings = _run_tools(
            requested, root_path, allow, runner, warnings)
        for result in tool_results:
            if result.get("status") == "error":
                warnings.append(
                    f"{result.get('name')} did not produce a parsable result: "
                    f"{result.get('error')}")
    scanner_ran = any(result.get("status") == "ok" for result in tool_results)
    inventory, dependency_findings = _dependency_inventory(
        root_path, scanner_ran=scanner_ran)
    secret_scope: dict[str, Any] | None = None
    if staged or diff:
        secret_scope = scan_secrets(root_path, staged=staged, diff=diff)
        source_findings_raw = secret_scope.get("findings") or []
        warnings.extend(secret_scope.get("notes") or [])
    else:
        source_findings_raw = []
    findings_raw = (source_findings + dependency_findings + tool_findings
                    + list(source_findings_raw))
    findings, total_findings, omitted = _finalize_findings(findings_raw, cap)

    by_severity = {name: 0 for name in SEVERITIES}
    by_category: dict[str, int] = {}
    by_confidence = {name: 0 for name in CONFIDENCES}
    for item in findings:
        by_severity[str(item.get("severity"))] = (
            by_severity.get(str(item.get("severity")), 0) + 1)
        category = str(item.get("category") or "unknown")
        by_category[category] = by_category.get(category, 0) + 1
        confidence = str(item.get("confidence") or "low")
        by_confidence[confidence] = by_confidence.get(confidence, 0) + 1

    not_assessed = [
        "exploitability and runtime reachability; a finding is not proof "
        "that an input reaches the sink",
        "authentication and authorization policy outside the scanned files",
        "completeness of this rule set and of the graph snapshot",
        "secrets in binary files, generated bundles, git history and "
        "external secret stores",
        "dynamic behaviour, deployment configuration and egress policy",
        "anything requiring the network, a model or a live service unless a "
        "tool is listed as run",
    ]
    if not index.info.get("available"):
        not_assessed.append(
            "graph-derived route-to-sink paths: no graph snapshot was "
            "available")
    elif not index.usable:
        not_assessed.append(
            "graph-derived route-to-sink paths: the snapshot was stale or "
            "unverified, so filesystem rules ran without that context")
    not_assessed.extend(_dependency_not_assessed(inventory, tool_results))
    for result in tool_results:
        if result.get("status") in ("unavailable", "refused", "error"):
            not_assessed.append(
                f"{result.get('name')} results: {result.get('status')}"
                + (f" ({result.get('error')})" if result.get("error") else ""))
    not_assessed = list(dict.fromkeys(not_assessed))

    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "advisory": True,
        "truth_assessed": False,
        "root": str(root_path),
        "graph": dict(index.info),
        "config": {
            "enabled": bool(config["enabled"]),
            "exclude": list(patterns),
            "max_findings": cap,
            "allow_network": bool(allow),
            "fail_on": fail_on,
            "tools": requested,
            "block_secrets": bool(config["block_secrets"]),
            "secret_allowlist": list(config["secret_allowlist"]),
        },
        "inputs": {
            "files_scanned": files_scanned,
            "files_skipped": files_skipped,
            "candidate_files": len(candidates),
            "manifests": len(inventory["manifests"]),
            "limits": {
                "max_scan_files": MAX_SCAN_FILES,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_graph_bytes": MAX_GRAPH_BYTES,
                "max_tool_output_bytes": MAX_TOOL_OUTPUT_BYTES,
                "tool_timeout_s": TOOL_TIMEOUT_S,
                "max_findings": cap,
            },
        },
        "dependency_inventory": inventory,
        "secrets": ({
            "schema": secret_scope.get("schema"),
            "mode": secret_scope.get("mode"),
            "range": secret_scope.get("range"),
            "summary": secret_scope.get("summary"),
            "notes": secret_scope.get("notes") or [],
        } if secret_scope is not None else None),
        "tools": {
            "requested": requested,
            "network_allowed": bool(allow),
            "results": tool_results,
        },
        "findings": findings,
        "summary": {
            "total": len(findings),
            "candidates": total_findings,
            "omitted": omitted,
            "by_severity": by_severity,
            "by_confidence": by_confidence,
            "by_category": dict(sorted(by_category.items())),
            "tools_run": sum(1 for item in tool_results
                             if item.get("status") == "ok"),
            "tools_unavailable": sum(
                1 for item in tool_results
                if item.get("status") == "unavailable"),
            "tools_refused": sum(
                1 for item in tool_results
                if item.get("status") == "refused"),
        },
        "not_assessed": [redact_text(entry) for entry in not_assessed],
        "warnings": [redact_text(item) for item in dict.fromkeys(warnings)],
    }
    return report


def _git_secret(root: Path, args: Sequence[str], *,
                timeout: int = SECRET_DIFF_TIMEOUT_S) -> str:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LC_ALL", "C")
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            errors="replace", timeout=timeout, env=env,
            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecurityError(f"git {' '.join(args)} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SecurityError(
            f"git {' '.join(args)} failed: {detail[0] if detail else 'unknown error'}")
    return proc.stdout or ""


def _secret_changed_files(root: Path, *, staged: bool,
                          range_spec: str | None) -> tuple[
                              list[str], set[str], list[dict[str, Any]], bool]:
    base_args = ["diff", "--name-status", "-z", "--no-renames",
                 "--diff-filter=ACMR"]
    numstat_args = ["diff", "--numstat", "-z", "--no-renames",
                    "--diff-filter=ACMR"]
    if staged:
        base_args.append("--cached")
        numstat_args.append("--cached")
    else:
        assert range_spec
        base, _, head = range_spec.partition("..")
        if not base or not head:
            raise SecurityError(
                f"diff range must be BASE..HEAD, got {range_spec!r}")
        for value in (base, head):
            _git_secret(root, ["rev-parse", "--verify", f"{value}^{{commit}}"])
        base_args.append(range_spec)
        numstat_args.append(range_spec)
    raw = _git_secret(root, base_args)
    fields = raw.split("\0")
    paths: list[str] = []
    position = 0
    while position + 1 < len(fields):
        status = fields[position].strip()
        path = fields[position + 1]
        position += 2
        if not status:
            continue
        if status.startswith(("R", "C")) and position < len(fields):
            path = fields[position]
            position += 1
        if path:
            paths.append(path)
    binary: set[str] = set()
    raw_numstat = _git_secret(root, numstat_args)
    for entry in raw_numstat.split("\0"):
        parts = entry.split("\t")
        if len(parts) == 3 and parts[0] == "-" and parts[1] == "-":
            binary.add(parts[2])
    changed = list(dict.fromkeys(paths))
    truncated = len(changed) > MAX_SECRET_FILES
    if truncated:
        changed = changed[:MAX_SECRET_FILES]
    return changed, binary, [], truncated


def _parse_added_lines(patch: str) -> dict[str, list[tuple[int, str]]]:
    added: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    new_line: int | None = None
    in_hunk = False
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            path = None
            new_line = None
            in_hunk = False
            continue
        if not in_hunk and raw.startswith("+++ "):
            candidate = raw[4:].strip()
            if candidate == "/dev/null":
                path = None
            elif candidate.startswith("b/"):
                path = candidate[2:]
            else:
                path = candidate
            new_line = None
            continue
        if raw.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,\d+)?", raw)
            new_line = int(match.group(1)) if match else None
            in_hunk = True
            continue
        if not in_hunk or path is None or new_line is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            added.setdefault(path, []).append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1
    return added


def _secret_chunks(added: Sequence[tuple[int, str]]
                   ) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    start: int | None = None
    last = 0
    texts: list[str] = []
    for line_no, text in sorted(added):
        if start is None:
            start = line_no
            last = line_no
            texts = [text]
        elif line_no == last + 1:
            texts.append(text)
            last = line_no
        else:
            chunks.append((start, "\n".join(texts)))
            start = line_no
            last = line_no
            texts = [text]
    if start is not None:
        chunks.append((start, "\n".join(texts)))
    return chunks


def _fingerprint_of(finding: Mapping[str, Any]) -> str:
    value = finding.get("secret_fingerprint")
    if isinstance(value, str) and value:
        return value
    evidence = finding.get("evidence")
    if isinstance(evidence, Mapping):
        value = evidence.get("fingerprint")
        if isinstance(value, str) and value:
            return value
    return ""


def _rule_of(finding: Mapping[str, Any]) -> str:
    value = finding.get("secret_rule")
    if isinstance(value, str) and value:
        return value
    source = finding.get("source")
    if isinstance(source, Mapping):
        return str(source.get("rule") or "")
    return ""


def secret_allowlist_match(finding: Mapping[str, Any],
                           entries: Sequence[str]) -> str | None:
    """The first allowlist entry that covers a finding, or None."""
    file_path = str(finding.get("file") or "")
    rule = _rule_of(finding)
    fingerprint = _fingerprint_of(finding)
    for raw in entries:
        entry = str(raw or "").strip()
        if not entry:
            continue
        if entry.startswith("path:"):
            if _secret_path_match(file_path, entry[5:]):
                return entry
        elif entry.startswith("rule:"):
            if rule and rule == entry[5:]:
                return entry
        elif entry.startswith("sha256:"):
            if (len(entry) > len("sha256:") + 4 and fingerprint
                    and (fingerprint == entry
                         or fingerprint.startswith(entry))):
                return entry
        elif "*" in entry or "/" in entry:
            if _secret_path_match(file_path, entry):
                return entry
    return None


def _secret_path_match(path: str, pattern: str) -> bool:
    clean = str(pattern or "").strip().lstrip("/")
    if not clean or not path:
        return False
    if fnmatch.fnmatch(path, clean):
        return True
    if clean.endswith("/**") and path.startswith(clean[:-3] + "/"):
        return True
    return False


def record_secret_decision(root: str | Path, *, kind: str,
                           finding: Mapping[str, Any],
                           decision: str = "allowed_once",
                           mode: str = "staged") -> dict[str, Any]:
    """Append a redacted override decision under the state directory."""
    from ozagia.config import state_path
    root_path = Path(root).expanduser().resolve()
    path = state_path(root_path, "security", "audit.jsonl")
    record = {
        "schema": AUDIT_SCHEMA,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "mode": mode,
        "decision": decision,
        "rule": _rule_of(finding),
        "path": finding.get("file"),
        "line": finding.get("line"),
        "fingerprint": _fingerprint_of(finding),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        raise SecurityError(f"cannot record the secret override: {exc}") from exc
    return record


def scan_secrets(root: str | Path = ".", *, staged: bool = False,
                 diff: str | None = None,
                 allowlist: Sequence[str] | None = None,
                 max_findings: int | None = None,
                 block_secrets: bool | None = None) -> dict[str, Any]:
    """Scan staged content or a diff range for secrets, without printing them.

    Exactly one of ``staged`` and ``diff`` is required. Values are hashed into
    a ``sha256:`` fingerprint; the value itself is never placed in a finding,
    report, warning or audit record.
    """
    root_path = Path(root).expanduser()
    try:
        root_path = root_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise SecurityError(f"cannot resolve root {root!r}: {exc}") from exc
    if not root_path.is_dir():
        raise SecurityError(f"root is not a directory: {root_path}")
    if bool(staged) == bool(diff):
        raise SecurityError("choose exactly one of --staged or --diff BASE..HEAD")
    config = _load_security_config(root_path)
    entries = (list(allowlist) if allowlist is not None
               else list(config["secret_allowlist"]))
    cap = max(1, min(_safe_int(max_findings, config["max_findings"]),
                     MAX_FINDINGS))
    block = config["block_secrets"] if block_secrets is None else bool(
        block_secrets)
    range_spec = diff if diff else None
    changed, binary, notes, truncated = _secret_changed_files(
        root_path, staged=staged, range_spec=range_spec)
    notes = list(notes)
    skipped: list[dict[str, Any]] = []
    if truncated:
        notes.append(
            f"changed file list exceeded {MAX_SECRET_FILES}; only the first "
            f"{MAX_SECRET_FILES} files were checked")
    patch_args = ["diff", "--unified=0", "--no-color", "--no-ext-diff",
                  "--no-renames", "--diff-filter=ACMR"]
    if staged:
        patch_args.append("--cached")
    else:
        assert range_spec
        patch_args.append(range_spec)
    patch = _git_secret(root_path, patch_args)
    patch_truncated = len(patch.encode("utf-8", errors="replace")) > (
        MAX_SECRET_DIFF_BYTES)
    if patch_truncated:
        patch = patch[:MAX_SECRET_DIFF_BYTES]
        notes.append(
            f"diff text exceeded {MAX_SECRET_DIFF_BYTES} bytes; the scan is "
            "incomplete and the gate treats it as not checked")
    added = _parse_added_lines(patch)
    findings: list[dict[str, Any]] = []
    files_scanned = 0
    for rel in changed:
        path_hit = credential_file_hit(rel)
        if path_hit is not None:
            findings.append(secret_finding(rel, path_hit))
        if rel in binary:
            skipped.append({"path": rel, "reason": "binary file"})
            continue
        try:
            size_spec = (f":{rel}" if staged else f"HEAD:{rel}")
            size_text = _git_secret(root_path, ["cat-file", "-s", size_spec])
            size = int(size_text.strip())
        except (SecurityError, ValueError):
            skipped.append({"path": rel, "reason": "staged content unavailable"})
            continue
        if size > MAX_SECRET_FILE_BYTES:
            skipped.append({
                "path": rel,
                "reason": f"larger than {MAX_SECRET_FILE_BYTES} bytes",
            })
            continue
        files_scanned += 1
        for start, text in _secret_chunks(added.get(rel, [])):
            for hit in secret_hits(text, start_line=start):
                findings.append(secret_finding(rel, hit))
    allowed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for finding in findings:
        match = secret_allowlist_match(finding, entries)
        if match:
            allowed.append({
                "rule": _rule_of(finding),
                "path": finding.get("file"),
                "line": finding.get("line"),
                "fingerprint": _fingerprint_of(finding),
                "allowlist": match,
            })
        else:
            kept.append(finding)
    final, total, omitted = _finalize_findings(kept, cap)
    by_rule: dict[str, int] = {}
    for finding in final:
        rule = _rule_of(finding) or "secret"
        by_rule[rule] = by_rule.get(rule, 0) + 1
    if not changed:
        notes.append("no staged content" if staged else
                     f"no changed files in {range_spec}")
    report = {
        "schema": SECRETS_SCHEMA,
        "schema_version": 1,
        "advisory": True,
        "truth_assessed": False,
        "root": str(root_path),
        "mode": "staged" if staged else "diff",
        "range": range_spec,
        "block_secrets": block,
        "gate": {
            "enabled": block,
            "reason": None if block else (
                "[security] block_secrets=false disables the commit/push "
                "secret gate; findings are still reported"),
        },
        "allowlist": list(entries),
        "findings": final,
        "allowed": allowed,
        "summary": {
            "total": len(final),
            "candidates": total,
            "omitted": omitted,
            "allowed": len(allowed),
            "files_changed": len(changed),
            "files_scanned": files_scanned,
            "skipped": len(skipped),
            "truncated": bool(truncated or patch_truncated),
            "by_rule": dict(sorted(by_rule.items())),
        },
        "skipped": skipped,
        "notes": [redact_text(item) for item in dict.fromkeys(notes)],
        "redaction": {
            "value_withheld": True,
            "fingerprint_prefix": "sha256:",
        },
        "not_assessed": [
            "whether a credential is live, revoked or exploitable",
            "secret history older than the staged content or the diff range",
            "binary, large or generated files skipped in the report",
            "secret stores and deployment configuration outside the range",
        ],
    }
    return report


def hook_command(kind: str) -> str:
    if kind in ("commit", "pre-commit"):
        return "python -m ozagia.worktree_cli policy --for commit"
    if kind in ("push", "pre-push"):
        return "python -m ozagia.worktree_cli policy --for push"
    raise SecurityError("hook kind must be commit or push")


def hook_script(kind: str) -> str:
    return ("#!/bin/sh\n"
            f"{HOOK_MARKER}: {kind}\n"
            "set -eu\n"
            f"exec {hook_command(kind)}\n")


def secret_hook_path(root: str | Path, kind: str) -> Path:
    root_path = Path(root).expanduser().resolve()
    hook_name = ("pre-commit" if kind in ("commit", "pre-commit")
                 else "pre-push" if kind in ("push", "pre-push") else "")
    if not hook_name:
        raise SecurityError("hook kind must be commit or push")
    output = _git_secret(root_path, ["rev-parse", "--git-path", "hooks"]).strip()
    path = Path(output)
    if not path.is_absolute():
        path = root_path / path
    return path / hook_name


def install_hook(root: str | Path, kind: str, *,
                 force: bool = False) -> dict[str, Any]:
    """Install an opt-in pre-commit/pre-push hook. Never called implicitly."""
    path = secret_hook_path(root, kind)
    script = hook_script(kind)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SecurityError(f"cannot read existing hook {path}: {exc}") from exc
        if HOOK_MARKER not in existing and not force:
            raise SecurityError(
                f"{path} already exists and was not installed by ozagia; "
                "pass force only after reviewing it")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    except OSError as exc:
        raise SecurityError(f"cannot install hook {path}: {exc}") from exc
    return {"installed": True, "path": str(path), "kind": kind,
            "reversible": True, "command": hook_command(kind)}


def uninstall_hook(root: str | Path, kind: str) -> dict[str, Any]:
    """Remove only a hook that carries the ozagia marker."""
    path = secret_hook_path(root, kind)
    if not path.exists():
        return {"removed": False, "path": str(path),
                "reason": "hook does not exist"}
    try:
        existing = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SecurityError(f"cannot read hook {path}: {exc}") from exc
    if HOOK_MARKER not in existing:
        raise SecurityError(
            f"{path} was not installed by ozagia; refusing to remove it")
    try:
        path.unlink()
    except OSError as exc:
        raise SecurityError(f"cannot remove hook {path}: {exc}") from exc
    return {"removed": True, "path": str(path), "kind": kind}


def secrets_exit_code(report: Mapping[str, Any]) -> int:
    """0 when the gate passes or is disabled, 1 when a secret blocks."""
    if not report.get("block_secrets", True):
        return 0
    return 1 if report.get("findings") else 0


def exit_code(report: Mapping[str, Any], fail_on: str | None = None) -> int:
    """0 unless a finding is at or above ``fail_on``; ``never`` always 0."""
    threshold_name = fail_on or str(
        (report.get("config") or {}).get("fail_on") or DEFAULT_FAIL_ON)
    if threshold_name not in FAIL_ON:
        raise SecurityError(f"fail_on must be one of {FAIL_ON}")
    if threshold_name == "never":
        return 0
    threshold = 0 if threshold_name == "blocking" else SEVERITY_RANK[
        threshold_name]
    for item in report.get("findings") or []:
        rank = SEVERITY_RANK.get(str(item.get("severity")), 99)
        if rank <= threshold:
            return 1
    return 0


def report_json(report: Mapping[str, Any]) -> str:
    return redact_text(json.dumps(report, indent=2, sort_keys=True)) + "\n"


def atomic_write(path: str | Path, text: str) -> None:
    """Write a report atomically; the caller chose the path."""
    import tempfile
    try:
        target = Path(path).expanduser()
    except (OSError, RuntimeError) as exc:
        raise SecurityError(f"cannot resolve output path {path!r}: {exc}") from exc
    parent = target.parent
    if not parent.is_dir():
        raise SecurityError(f"output directory does not exist: {parent}")
    handle: int | None = None
    tmp: str | None = None
    try:
        handle, tmp = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            handle = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        tmp = None
    except OSError as exc:
        raise SecurityError(f"cannot write {target}: {exc}") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def format_tools() -> str:
    lines = ["Security tool registry (fixed commands; nothing is installed):"]
    for item in tools():
        lines.append(
            f"- {item['name']}: {item['status']} "
            f"({item['kind']}, network_required={str(item['network_required']).lower()})")
        lines.append(f"  {item['description']}")
        if item.get("path"):
            lines.append(f"  path: {item['path']}")
    lines.append("A missing tool is reported as unavailable, not as a failure.")
    return "\n".join(lines) + "\n"


def format_report(report: Mapping[str, Any]) -> str:
    graph = report.get("graph") or {}
    summary = report.get("summary") or {}
    config = report.get("config") or {}
    findings = report.get("findings") or []
    lines = [
        "Security scan (advisory)",
        f"root: {report.get('root')}",
        f"graph: {graph.get('freshness')} ({graph.get('path')})"
        + (f" - {graph.get('reason')}" if graph.get("reason") else ""),
        f"files: {((report.get('inputs') or {}).get('files_scanned', 0))} "
        f"scanned, {((report.get('inputs') or {}).get('files_skipped', 0))} "
        "skipped",
        f"findings: {summary.get('total', 0)} shown "
        f"({summary.get('candidates', 0)} candidates, "
        f"{summary.get('omitted', 0)} omitted)",
        "severity: " + ", ".join(
            f"{name}={count}" for name, count in
            (summary.get("by_severity") or {}).items() if count),
    ]
    if config.get("tools"):
        lines.append("tools requested: " + ", ".join(config["tools"]))
    results = (report.get("tools") or {}).get("results") or []
    for result in results:
        lines.append(
            f"  tool {result.get('name')}: {result.get('status')}"
            + (f" (version {result.get('version')})"
               if result.get("version") else "")
            + (f" - {result.get('error')}" if result.get("error") else ""))
    if not findings:
        lines.append("  none")
    for item in findings:
        location = item.get("file") or "(no file)"
        if item.get("line"):
            location += f":{item['line']}"
        lines.append(
            f"  [{str(item.get('severity')).upper()}] {item.get('id')} "
            f"{item.get('category')} {item.get('kind')} {location}: "
            f"{item.get('message')}")
        if item.get("graph_path"):
            lines.append("    graph path: " + " -> ".join(item["graph_path"]))
        evidence = item.get("evidence") or {}
        if evidence.get("detail"):
            lines.append(f"    evidence: {evidence['detail']}")
        source = item.get("source") or {}
        provenance = source.get("type") or "unknown"
        if source.get("tool"):
            provenance += f":{source['tool']}"
        lines.append(f"    source: {provenance} rule={source.get('rule')} "
                     f"confidence={item.get('confidence')}")
        lines.append(f"    fix: {item.get('remediation')}")
        lines.append(f"    false positive note: {item.get('false_positive_note')}")
    inventory = report.get("dependency_inventory") or {}
    if inventory.get("manifests"):
        lines.append(
            f"dependency inventory: {inventory.get('components', 0)} "
            f"component(s), {len(inventory.get('manifests') or [])} "
            f"manifest(s), CVE status={inventory.get('cve_status')}")
    lines.append("not assessed:")
    for entry in report.get("not_assessed") or []:
        lines.append(f"  - {entry}")
    for warning in report.get("warnings") or []:
        lines.append(f"warning: {warning}")
    lines.append("truth_assessed: false")
    lines.append(ADVISORY)
    return redact_text("\n".join(lines) + "\n")


def format_secrets_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "Secret scan (advisory)",
        f"root: {report.get('root')}",
        f"mode: {report.get('mode')}"
        + (f" ({report.get('range')})" if report.get("range") else ""),
        f"gate enabled: {str(bool(report.get('block_secrets'))).lower()}"
        + (f" - {report['gate']['reason']}"
           if isinstance(report.get("gate"), Mapping)
           and report["gate"].get("reason") else ""),
        f"findings: {summary.get('total', 0)} shown "
        f"({summary.get('candidates', 0)} candidates, "
        f"{summary.get('omitted', 0)} omitted, "
        f"{summary.get('allowed', 0)} allowlisted)",
    ]
    for finding in report.get("findings") or []:
        location = finding.get("file") or "(no file)"
        if finding.get("line"):
            location += f":{finding['line']}"
        lines.append(
            f"  [{str(finding.get('severity')).upper()}] "
            f"{finding.get('id')} {finding.get('secret_rule')} "
            f"{location} fingerprint={finding.get('secret_fingerprint')}; "
            "value withheld")
    if not report.get("findings"):
        lines.append("  none")
    for skipped in report.get("skipped") or []:
        lines.append(f"  skipped {skipped.get('path')}: {skipped.get('reason')}")
    for note in report.get("notes") or []:
        lines.append(f"note: {note}")
    lines.append("truth_assessed: false")
    return redact_text("\n".join(lines) + "\n")


def format_finding(finding: Mapping[str, Any]) -> str:
    location = finding.get("file") or "(no file)"
    if finding.get("line"):
        location += f":{finding['line']}"
    source = finding.get("source") or {}
    lines = [
        f"finding {finding.get('id')}",
        f"category: {finding.get('category')} ({finding.get('kind')})",
        f"severity: {finding.get('severity')} confidence: "
        f"{finding.get('confidence')}",
        f"location: {location}",
        f"message: {finding.get('message')}",
        f"source: {source.get('type')} rule={source.get('rule')}",
    ]
    if finding.get("graph_path"):
        lines.append("graph path: " + " -> ".join(finding["graph_path"]))
    evidence = finding.get("evidence") or {}
    for key in ("kind", "detail", "tool", "rule", "command", "exit_code",
                "database"):
        if key in evidence:
            lines.append(f"evidence {key}: {evidence[key]}")
    for key in ("cwe", "owasp"):
        if finding.get(key):
            lines.append(f"{key}: {', '.join(str(value) for value in finding[key])}")
    lines.append(f"remediation: {finding.get('remediation')}")
    lines.append(f"false positive note: {finding.get('false_positive_note')}")
    lines.append("not assessed:")
    for entry in finding.get("not_assessed") or []:
        lines.append(f"  - {entry}")
    lines.append("truth_assessed: false")
    return redact_text("\n".join(lines) + "\n")


def load_report(path: str | Path) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SecurityError(f"cannot read report {candidate}: {exc}") from exc
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
        raise SecurityError(f"{candidate} is not a {SCHEMA} report")
    return dict(data)


__all__ = [
    "ADVISORY",
    "AUDIT_SCHEMA",
    "DEFAULT_EXCLUDES",
    "DEFAULT_FAIL_ON",
    "DEFAULT_MAX_FINDINGS",
    "FAIL_ON",
    "HOOK_MARKER",
    "MAX_FINDINGS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SECRETS_SCHEMA",
    "SEVERITIES",
    "SecurityError",
    "TOOL_REGISTRY",
    "atomic_write",
    "credential_file_hit",
    "exit_code",
    "format_finding",
    "format_report",
    "format_secrets_report",
    "format_tools",
    "hook_command",
    "hook_script",
    "install_hook",
    "load_config",
    "load_report",
    "record_secret_decision",
    "redact_text",
    "report_json",
    "scan",
    "scan_secrets",
    "secret_allowlist_match",
    "secret_finding",
    "secret_fingerprint",
    "secret_hits",
    "secrets_exit_code",
    "tools",
    "uninstall_hook",
]
