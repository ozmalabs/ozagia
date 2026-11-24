# Integration matrix

This page is the canonical answer to "does ozagia's integration with *host*
work, and on which version?". It records the verification level that was
actually reached. Binary presence, an installer round trip and a hook fixture
are not a live session.

The machine-readable version table is
[`src/ozagia/integrations/versions.py`](../src/ozagia/integrations/versions.py);
it is what `ozagia doctor` and `ozagia-status` compare an installed binary
with. Change this page and that module together.

## Verification levels

| Level | What it proves | What it does not prove |
| --- | --- | --- |
| `protocol fixture` | The integration maps a host-shaped payload to the daemon protocol and handles the reply, against a stub daemon in tests. | That the host sends that payload, loads the hook or shows the output. |
| `installer write/remove` | `install` writes the expected files and `uninstall` restores them; existing user keys survive. | That the host reads or trusts the files. |
| `host-launch without model` | The real host binary was started and the plugin loaded/rendered, with no model request. | That a model session, tool call or hook works end to end. |
| `live language server` | A real language server answered a definition/navigation request on a fixture. | Exhaustive language compatibility, or a version outside the tested one. |
| `live session` | A real host agent session ran tools and hooks, without a stub. | Nothing in this matrix reaches this level yet. |

`verified <version>` below means the row's stated level was reached on that
exact host version. `untested` means the row's level was not reached on the
installed host. `not installed` describes the host, not the code.

## Matrix

| Surface | Last verified host version | Verification level | Evidence | Exact limitations |
| --- | --- | --- | --- | --- |
| <a id="opencode-tui"></a>**OpenCode TUI** | `verified 2.4.1` (also `2.3.0`) | host-launch without model | `tests/test_opencode_plugin.py::test_real_opencode_lists_the_installed_plugin`; `tests/test_opencode_plugin.py::test_tui_brand_renders_under_the_logo_in_a_real_pty`; `docs/harness.md` "Interactive mode" | Plugin is listed and `setup` passes a Node stub; the TUI brand renders under a PTY. No model request, no tool call and no end-to-end agent session. The pty test only proves the home-screen injection, not every panel. |
| **OpenCode Desktop** | `untested` | none | `docs/opencode_addin.md` "API-version notes and gaps" | GUI is built and typechecked against `@opencode/plugin` 2.3.0. No Desktop window was mounted, no Desktop binary was probed, and no host event was delivered. Browser-tab support is host-dependent. |
| <a id="codex-hooks"></a>**Codex hooks** | `untested` on the installed `codex-cli 0.172.0`; MCP table read once on `0.169.0` | protocol fixture + installer write/remove | `tests/test_codex_hooks.py`; `tests/test_codex_integration.py`; `docs/codex.md` | No Codex session ran the hooks. Hooks need Codex's `/hooks` trust review and a trusted project. The `codex mcp get ozagia --json` check was on 0.169.0, not 0.172.0. Hook stdout/context delivery to the model is unverified. |
| <a id="kiro-hooks"></a>**Kiro hooks** | `not installed` (`kiro-cli` is absent from this host); the last offline profile validation used `2.37.0` | protocol fixture + installer write/remove; profile validation was on 2.37.0, not this host | `tests/test_kiro_hooks.py`; `docs/kiro.md` | No Kiro chat session ever ran the hooks. 2.37 drops PostToolUse output in every engine. V1/V2 only load agent-profile hooks; V3 workspace hook files need a trusted workspace. The agent-profile validator is not exercised on this host. |
| <a id="claude-code-hooks"></a>**Claude Code hooks** | `untested` on the installed `2.1.227` | protocol fixture + installer write/remove | `tests/test_claude_hooks.py`; `docs/claude_code.md` | No Claude Code session ran the hooks; Claude is logged out on the development machine. The reference used for the mapping mentioned 2.1.319/2.1.325, which is not a live check of 2.1.227. The built-in result is never replaced and `additionalContext` is capped by the host. |
| <a id="pyright"></a>**pyright** | `verified 1.1.414` | live language server | `OZAGIA_LIVE_LSP=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_lsp.py::test_live_pyright_definition`; `docs/lsp.md` | One definition request on a fixture. `pyright` is not on `PATH` here; doctor finds `.venv/bin/pyright` and the LSP layer uses `.venv/bin/pyright-langserver`. No exhaustive Python compatibility, diagnostics quality or rename check. |
| <a id="rust-analyzer"></a>**rust-analyzer** | `not installed` on this host (the `~/.cargo/bin/rust-analyzer` rustup shim reports `Unknown binary`); a previous host ran the live suite on `1.97.1` | live language server (previous host only) | `tests/test_lsp.py` live Rust tests; `docs/lsp.md` "Verification in this tree" | This host lacks the `rustup component add rust-analyzer` component. Symptom: the shim path exists but `rust-analyzer --version` exits 1, so doctor reports `unknown`, not `verified`. Cold indexing and macro/cfg expansion remain limits even where installed. |

## Current host snapshot (2026-11-24)

`ozagia doctor --root .` on the development host reports:

| Host | Installed | Version | Doctor state |
| --- | --- | --- | --- |
| `opencode` | yes (`~/.opencode/bin/opencode`) | 2.4.1 | `verified` |
| `codex` | yes (`~/.local/bin/codex`) | 0.172.0 | `installed_untested_version` (hooks live-untested) |
| `kiro-cli` | no | — | `not_installed` |
| `claude` | yes (`~/.local/bin/claude`) | 2.1.227 | `installed_untested_version` |
| `pyright` | yes (project `.venv/bin/pyright`) | 1.1.414 | `verified` |
| `rust-analyzer` | shim only; component absent | unknown | `unknown` |

These are observations, not integration results. A `verified` OpenCode TUI row
does not make the Codex or Claude rows verified, and a missing `kiro-cli` does
not mean the Kiro adapter code is broken.

## `ozagia doctor` and `ozagia-status`

`ozagia doctor --json` adds three fields:

- `integrations.hosts`: one row per host with `state`, `installed`, `version`,
  `path`, `tested_versions`, `tested_range`, `verification`, `matrix` and
  `error`. Version states are `verified`, `installed_untested_version`,
  `not_installed` and `unknown` (the binary was found but `--version` timed
  out, failed to parse, or could not be executed).
- `integrations.config`: read-only presence for the OpenCode plugin, Claude
  Code settings, Codex hooks/config/AGENTS.md and Kiro hook/agent/MCP files,
  under project and user scope where the installer supports both. It never
  writes. Configuration presence is not host loading or trust.
- `capabilities`: catalogue version, hash, capability/domain/surface counts and
  a bounded summary of the read-only `capabilities audit` (audit state, counts
  and finding counts by kind/severity). Full findings are not copied.

Warnings are data, not a doctor failure. A version outside the tested range, an
unparsable version, an unreadable settings file, or capability-catalogue drift
is listed under `warnings`; `doctor` exits non-zero only for a missing root or
invalid project configuration, as before.

`ozagia-status` composes the same doctor report and adds bounded
`integrations` and `capabilities` sections (`state`, host/config summary,
catalogue summary and warnings). It does not copy audit findings, evidence or
the full doctor report into those sections. Its absent-versus-unknown rules are
unchanged; see [status.md](status.md).

## How to re-verify

```bash
# OpenCode TUI plugin listing and brand render (no model call)
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_opencode_plugin.py::test_real_opencode_lists_the_installed_plugin \
  tests/test_opencode_plugin.py::test_tui_brand_renders_under_the_logo_in_a_real_pty

# pyright live definition smoke (no model, no network)
OZAGIA_LIVE_LSP=1 PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_lsp.py::test_live_pyright_definition

# rust-analyzer needs the rustup component first
rustup component add rust-analyzer

# Host version probe only
for host in opencode codex claude kiro-cli pyright rust-analyzer; do
  command -v "$host" >/dev/null && "$host" --version
done
```

A live Codex, Kiro or Claude session spends model tokens and needs a person's
approval. When one is run, change that row to `verified <version>` at the
`live session` level and record the exact host version and limitations here.
