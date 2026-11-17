# External toolchain discovery and install previews

`ozagia-toolchain` answers a question a model should not have to guess before
it starts a task: for the work in front of it, which relevant external tools
are already installed, which are missing, and what would installing one
involve on this host? PDF editing is the motivating example, but the registry
covers several capability families and is plain data that is easy to extend.

The command is discovery and planning only:

- it never installs anything and never runs an install command;
- it makes no network calls;
- the only processes it may start are bounded `--version` probes (2 s) and the
  `sudo -n true` usability check (2 s);
- the core stays standard-library only, and a missing tool is a report, not an
  error.

## Why standard library plus optional extras

Detection uses the standard library rather than a third-party detector:

| Question | Standard-library mechanism |
| --- | --- |
| Is a binary installed? | `shutil.which` |
| Is a Python module importable? | `importlib.util.find_spec` (no import) |
| What version is a Python distribution? | `importlib.metadata.version`, with `importlib.metadata.packages_distributions` as a module-to-distribution fallback |
| What OS/distribution is this? | `platform` plus a bounded read of `/etc/os-release` |
| What version does a binary report? | `subprocess.run` with an argv list, a 2 s timeout, a sanitized environment, no shell, and a 4 KiB output cap |
| Is sudo usable without a prompt? | `subprocess.run([sudo, "-n", "true"])`, 2 s, no shell |

PDF, image, OCR, office and media work is often better served by a Python
library than by a command-line tool. Those libraries are optional extras, not
core dependencies:

```toml
[project.optional-dependencies]
pdf = ["pypdf>=5", "pdfplumber>=0.11", "reportlab>=4"]
ocr = ["pytesseract>=0.3"]
image = ["Pillow>=10"]
office = ["python-docx>=1", "openpyxl>=3", "python-pptx>=1"]
media = ["faster-whisper>=1", "opencv-python>=4"]
data = ["pandas>=2"]
```

They are deliberately **not** part of `all` or `dev`, and nothing in the tests
requires them. Install one only when a task needs it:

```bash
uv pip install 'ozagia[pdf]'
python -m pip install 'ozagia[pdf]'
```

External CLIs remain fallbacks. If the `pdf` extra is absent, `pdftotext`,
`qpdf`, Ghostscript and the other CLIs are still discovered and planned. If
the extra is installed, `pypdf`/`pdfplumber`/`reportlab` are preferred in the
plan's recommended path for the capabilities they serve.

The `ocr` extra installs the `pytesseract` wrapper, not the OCR engine.
`pytesseract` is reported as installed but unusable until `tesseract` is
present; the engine remains the readiness signal.

### Prior art and why no detection dependency

The implementation deliberately stays with `shutil.which`,
`importlib.util.find_spec`, `importlib.metadata` and `platform`:

- `whichcraft` and `shutilwhich` are obsolete compatibility shims for
  `shutil.which`; `find-exe` and `pox` only replace `which` and add nothing
  this slice needs.
- `cli-tool-audit` (MIT, PyPI) is the closest published Python library. It is
  config-driven CLI existence/version auditing, but it pulls in about nine
  dependencies (colorama, colorlog, packaging, prettytable, semver, toml,
  tomlkit, tqdm, whichcraft) and has no capability registry, install matrix,
  sudo/approval gates or model-facing plan. It is not a core dependency. A
  future optional backend could use it for pinned version-range audits, which
  this slice does not attempt.
- `zerodep`'s `depdetect` is stdlib-only and offers `check_binary`,
  `check_python_package` and `get_binary_version`, but it is not published on
  PyPI. Vendoring a single file is not worth the maintenance; it is prior art
  only.
- `CLI-Hub`/AgentOS `CLIRegistry` have the capability × provider preflight and
  `--fix-hints` shape this module borrows, but they are TypeScript tools built
  around a network registry. We borrow the shape, not the dependency.

The optional `pdf`, `ocr`, `image`, `office`, `media` and `data` extras are
task libraries, not detection dependencies. The core remains standard-library
only.

## Registry

The registry is versioned data (`registry()`); it is not inferred over the
whole package ecosystem. Umbrella groups expand to child capabilities:

| Group | Capabilities | Notes |
| --- | --- | --- |
| `documents` | `pdf`, `ocr`, `office` | Office covers the `docx`, `xlsx` and `pptx` subtypes. |
| `media` | `image`, `audio`, `video` | ffprobe -> ffmpeg frames/audio -> transcribe -> edit -> visual check. |
| `web` | `browser`, `research` | Browser automation and remote web/research providers. |

`--capability documents` and `--capability media` are equivalent to naming
their children. Each capability also carries its `group` in the plan.

| Capability | Python / optional extra | External CLIs | Remote / unprobed providers |
| --- | --- | --- | --- |
| `pdf` | `pypdf`, `pdfplumber`, `reportlab` (extra `pdf`); `pikepdf`, PyMuPDF (`fitz`) | `qpdf`, `pdftk`, `mutool`, `pdftotext`, `pdftoppm`, `gs` | `pdf-lib` (Node, not probed) |
| `ocr` | `pytesseract` (extra `ocr`, needs Tesseract); `ocrmypdf` (also a CLI) | `tesseract`, `ocrmypdf` | - |
| `image` | Pillow (import `PIL`, extra `image`), OpenCV (`cv2`, extra `media`) | ImageMagick (`magick`/`convert`), FFmpeg | - |
| `office` | `python-docx`, `openpyxl`, `python-pptx` (extra `office`) | LibreOffice (`libreoffice`/`soffice`), Pandoc | - |
| `audio`, `video` | faster-whisper (extra `media`), OpenCV; Whisper CLI | FFmpeg, FFprobe, `whisper` | - |
| `archive` | - | 7-Zip (`7z`/`7zz`/`7za`), `unzip`, `tar` | - |
| `diagram` | - | Graphviz `dot`, Mermaid CLI `mmdc` | - |
| `data` | pandas (extra `data`) | `jq`, `yq`, `sqlite3`, `duckdb`, `jupyter`, `psql` | Postgres MCP Pro (MCP, read-only DSN) |
| `browser` | browser-use (Python) | Chromium/Chrome, Playwright | Chrome DevTools MCP, Puppeteer (Node, not probed) |
| `research` | - | - | Exa, Firecrawl, Fetch, Context7, arXiv, Zotero |
| `ops` | - | Terraform, `kubectl`, `psql` | GitHub MCP, Sentry MCP, Cloudflare MCP |
| `design` | - | - | Figma MCP, Canva MCP, image-generation provider |
| `communication` | - | - | Notion MCP, Linear MCP, Slack MCP, email |

Remote/MCP/API providers are listed per capability but are **not probed** and
are **not installed**. They are reported as requiring network and, where
relevant, credentials. Node libraries (`pdf-lib`, Puppeteer) are local but
unprobeable; the plan lists them as "not probed" rather than guessing.

Each tool entry names:

- a stable id and title;
- its kind (`binary`, `python`, `remote` or `node`) and `format`
  (`library`, `cli`, `mcp`, `remote`, `api` or `app`);
- probe names (binary alternatives such as `magick`/`convert`, or import names
  such as `PIL`/`fitz`);
- bounded `provides` values ("extract text", "merge/split", "fill forms",
  "render pages", "create", "OCR");
- whether it writes files;
- its optional `extra` and Python distribution where applicable;
- `remote`/`credentialed`/`probeable`, `requires_network`, and a `requires`
  list for a wrapper that needs an engine (for example `pytesseract` needs
  `tesseract`);
- short `workflows` phrases and `native_for`/`preferred_by` model-family hints;
- per-package-manager install metadata: package name, command template,
  whether sudo is required, whether the network is required, and notes.

Install metadata covers apt/dnf/pacman/brew for system packages, uv/pip for
Python packages, and npm for `mmdc`, Playwright and Node libraries. A Python
tool that belongs to an extra installs the extra form, not the bare package,
so the plan names the set the project documents:

```text
uv pip install 'ozagia[pdf]'
python -m pip install 'ozagia[pdf]'
sudo apt-get install -y qpdf
npm install -g @mermaid-js/mermaid-cli
```

### Workflow to capability to providers

| Workflow | Capability / group | Preferred local providers | Remote or fallback |
| --- | --- | --- | --- |
| Merge, split or rotate PDFs | `documents` / `pdf` | pypdf, qpdf, mutool | - |
| Extract PDF text or tables | `documents` / `pdf` | pdfplumber, pdftotext | - |
| Create or fill PDFs | `documents` / `pdf` | reportlab, pypdf, pdftk, pdf-lib | - |
| OCR a scan | `documents` / `ocr` | tesseract, ocrmypdf, pytesseract | - |
| Edit DOCX/XLSX/PPTX | `documents` / `office` | python-docx, openpyxl, python-pptx, LibreOffice | OfficeCLI and Paper Office are optional agent-first providers; they are not in this registry |
| Convert office formats | `documents` / `office` | LibreOffice, Pandoc | - |
| Video to transcript | `media` / `video` + `audio` | ffprobe, ffmpeg, whisper/faster-whisper | - |
| Inspect a frame | `media` / `video` + `image` | ffmpeg, OpenCV, ImageMagick, Pillow | - |
| Deterministic browser task | `web` / `browser` | Playwright | Chrome DevTools MCP, browser-use, Puppeteer |
| Web research | `web` / `research` | - | Exa, Firecrawl, Fetch, Context7 |
| Paper or reference lookup | `web` / `research` | - | arXiv, Zotero |
| Read-only database query | `data` | sqlite3, duckdb, psql, pandas | Postgres MCP Pro (read-only DSN) |
| Deployment or incident triage | `ops` | Terraform, kubectl, psql | GitHub MCP, Sentry MCP, Cloudflare MCP |
| Design handoff | `design` | - | Figma MCP, Canva MCP, image generation |
| Team communication | `communication` | - | Notion MCP, Linear MCP, Slack MCP, email |

`pdf-lib`, OfficeCLI/Paper Office and Puppeteer are named for completeness.
They are optional providers, not requirements; `pdf-lib` and Puppeteer are
local unprobed Node libraries in the registry, while OfficeCLI/Paper Office
are left to the project or harness.

### Model preferences

The registry records `native_for`/`preferred_by` where a provider has a clear
model-family fit. `brief_lines(plan, model_family="...")` uses that metadata.
The notes below are guidance, not automatic configuration.

| Model / harness | Preferred tools and behaviour |
| --- | --- |
| Claude Code | Dedicated Read/Write/Edit/Grep/Glob tools over `cat`/`sed`/`grep`/`find`; Bash remains available and hooks often enforce the preference. Native PDF/image input plus the official PDF/DOCX/PPTX/XLSX skills use Python libraries: pypdf, pdfplumber, reportlab, python-docx, openpyxl, python-pptx. |
| OpenAI Codex / GPT-5 | Trained on the exact `apply_patch` format; prefers `apply_patch`, `shell` and `update_plan`, with structured JSON function calls. CLI providers reached through the shell are natural; Playwright is the default deterministic browser path. |
| Gemini CLI | `read_file` handles text, images, audio and PDF natively. `replace` uses a multi-stage edit-correction loop; Aider uses `diff-fenced` for Gemini because of fencing issues. |
| Cursor | Tunes tool instructions per frontier model; semantic `codebase_search` first for concepts, `grep_search` for exact symbols, ranged `read_file`. `edit_file` uses a sketch with `// ... existing code ...`; `reapply` escalates a failed edit. |
| Aider | Model-specific edit formats: whole for GPT-3.5, diff for GPT-4o, udiff for GPT-4 Turbo, diff-fenced for Gemini. Architect mode separates planning from editing. |
| OpenHands | Bash + Python + browser + file editor + task tracker. Benchmark evidence is browser-heavy for GAIA and bash/edit-heavy for SWE-Bench; browser-use/Playwright fit the browser-heavy case. |
| Weaker / local models | Prefer whole-file edits, fewer tools and short descriptions. A dense repo map can hurt; keep the tool count bounded. |

Sources are cited generically: the Anthropic PDF skill, the OpenAI Codex
prompting guide, Gemini CLI tool documentation, Cursor agent documentation,
Aider edit-format documentation and the OpenHands paper. The only behavioural
hook in this slice is the model-family preference in `brief_lines`.

## Detection

`detect_environment(root)` reports:

- OS, release, architecture and family; distribution id, pretty name, version
  and `ID_LIKE` from `/etc/os-release`; WSL and container hints;
- available managers with paths: `uv`, `pip`, `pip3`, `pipx`, `brew`,
  `apt-get`, `dnf`, `yum`, `pacman`, `zypper`, `apk`, `npm`, `cargo`, `go`
  and `gem`;
- the Python executable, version, virtualenv state and `uv` availability;
- whether `sudo` exists and works non-interactively;
- the effective `[tools]` approval settings and their sources;
- configuration errors and unknown `[tools]` keys.

`sudo -n true` is only a usability check. It can update the sudo timestamp
(within the configured timeout), which may make a later interactive `sudo`
prompt less likely; it never reads a password. Pass `--no-sudo` or set
`probe_sudo = false` to skip it.

## Approval

Put the table in the versioned repository configuration
(`<root>/ozagia/config.toml`):

```toml
[tools]
allow_install = false
allow_network = false
allow_sudo = false
probe_versions = true
probe_sudo = true
brief = true
preferred = { pdf = ["pypdf", "pdftotext"] }
```

All three approval values default to false. `allow_install` gates install
previews; `allow_network` gates options that would download packages;
`allow_sudo` gates options that need root. `brief` (default true) lets the
session brief add its bounded `[ozagia] tools:` block for dirty non-code
files; `brief = false` turns that off without disabling the CLI or the
daemon op. Environment variables override a configured or default value when
set to `1`, `true` or `yes`:

```bash
OZAGIA_ALLOW_INSTALL=true
OZAGIA_ALLOW_NETWORK=true
OZAGIA_ALLOW_SUDO=true
```

A false-looking environment value (`0`, `false`, `no`) does not clear a
configured true. The environment override sources are reported. Wrong value
types and unknown `[tools]` keys are reported in `environment.config`, never
raised as a crash; safe defaults remain in effect. `preferred` is a mapping
from capability id to tool ids that should be considered first in that
capability.

`[tools]` is owned by `ozagia.core.toolchain` (`config._OWNED_SECTIONS`), so
`ozagia doctor` and the project-config loader report it as owned rather than
unknown.

## Plan output

`tool_plan(root, capabilities=..., paths=...)` infers capabilities from path
extensions when they are not named explicitly. Unknown extensions are never
guessed.

| Extension | Capability |
| --- | --- |
| `.pdf` | `pdf` |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`, `.tif`, `.tiff` | `image` |
| `.docx`, `.doc`, `.odt`, `.pptx`, `.ppt`, `.xlsx`, `.xls`, `.rtf` | `office` |
| `.mp3`, `.wav`, `.flac`, `.ogg`, `.m4a` | `audio` |
| `.mp4`, `.mov`, `.mkv`, `.avi`, `.webm` | `video` |
| `.zip`, `.7z`, `.tar`, `.tar.gz`, `.tgz`, `.gz` | `archive` |
| `.svg`, `.dot`, `.gv`, `.mmd` | `diagram` |
| `.sqlite`, `.sqlite3`, `.db`, `.csv`, `.tsv`, `.parquet`, `.ipynb` | `data` |
| `.html`, `.htm` | `browser` |
| `.tf`, `.tfvars` | `ops` |
| `.fig` | `design` |
| `.eml` | `communication` |

Only the tools of the requested capabilities are probed. `--capability
documents`, `media` or `web` expands a group to its child capabilities; the
plan records the requested `groups` as well as the expanded capabilities. The
plan contains:

- `request`: explicit, inferred and combined capabilities, requested groups,
  paths, and paths with extensions that matched nothing;
- `environment`: the full detection report;
- `capabilities[]`: `group`, `ready`, `available` (path/version/provides,
  `usable`, `missing_requirements`, and whether it is the recommended tool),
  `missing`, `remote`, `unprobed`, install options and a deterministic
  `recommended_tool`/`recommended_path`;
- `install[]`: one preview per package-manager recipe with `manager`,
  `available`, `command`, `package`, `requires_sudo`, `requires_network`,
  `blocked_by`, `blocked_reasons`, `extra`, `install_kind`, `recommended` and
  `would_run: false`;
- `summary`: ready/missing/blocked counts plus available, usable, missing,
  remote and unprobed provider counts;
- `not_assessed`: what the report does not establish.

`blocked_by` uses `install`, `network`, `sudo` and `manager`. A capability is
`ready` when at least one relevant local tool is `usable`. A wrapper whose
engine is missing (for example `pytesseract` without `tesseract`) is listed as
installed but unusable, and does not make the capability ready. A remote/MCP
provider is never counted as ready: it is listed under `remote` because its
presence and credentials are not checked. A local Node library is listed
under `unprobed` for the same reason. A capability is `installable` when at
least one local option has no blocker, and `blocked` when it is not ready and
no option is feasible.

A recommended option is chosen only when one is feasible. The order prefers
user-level uv/pip/npm over sudo, then the detected distribution manager, then
Homebrew on macOS. The plan's `recommended_path` says whether the preferred
path is a Python extra, a Python package or a system package.

Nothing is installed. Every option is a preview (`would_run: false`) and the
plan still never executes an install command. Remote providers are named for
the workflow and marked as requiring network or credentials; they are never
installed or connected.

## CLI

```bash
ozagia-toolchain env [--json|--text] [--no-sudo]
ozagia-toolchain list [--json|--text]
ozagia-toolchain plan --capability pdf --path report.pdf \
  [--json|--text] [--no-versions] [--no-sudo] [--require-ready]
ozagia-toolchain plan --capability documents --json
ozagia-toolchain explain pypdf [--json|--text] [--no-versions] [--no-sudo]
```

Text is the default. `plan --require-ready` exits 1 when a requested
capability has no available local tool; without it a missing tool is a report
and the command exits 0. `--capability documents`, `media` or `web` expands a
group. `explain` accepts a tool id, a probe alias such as `gs`/`PIL`, or a
distribution name. There is no install or apply subcommand.

A PDF plan:

```bash
ozagia-toolchain plan --path contract.pdf
```

```text
[ozagia] toolchain plan
environment: Linux x86_64, Ubuntu 24.04.2 LTS (id ubuntu); managers: apt-get, pip3, uv; sudo usable; install not approved, network not approved, sudo not approved
request: pdf (paths: contract.pdf)
pdf: missing (recommended path: Python extra pdf)
  missing: fitz, ghostscript, mutool, pdfplumber, pdftk, pdftoppm, pdftotext, pikepdf, pypdf, qpdf, reportlab
  unprobed local providers (presence not checked): pdf-lib
  install previews:
    fitz [Python package]: installation is not approved (install, network, manager)
    ...
    pypdf [Python extra pdf]: installation is not approved (install, network, manager)
...
```

With installation approved, the same plan names the exact preview commands
and any remaining blocker:

```text
pdf: missing (recommended path: Python extra pdf)
  install previews:
    pypdf [Python extra pdf]: uv pip install 'ozagia[pdf]' (recommended)
    qpdf [system package]: sudo apt-get install -y qpdf - blocked: sudo not approved
```

When installation is not approved, no command is suggested at all; the line
says that installation is not approved. `--json` returns the same data for a
caller.

## Model brief

`brief_lines(plan, model_family=None)` returns compact model-facing lines for
a session brief or preflight. The first line names the environment, managers,
sudo state and approval state. Each capability then says `ready` or `missing`,
the useful available tools and what they provide, and, only when installation
is approved, the exact recommended preview command and any blocker. A ready
capability marks a Python extra as the preferred path when one is installed.

When `model_family` is given (`claude`, `codex`, `gemini`, `cursor`, `aider`,
`openhands` or `local`, with aliases such as `claude-code`, `gpt-5` and
`openai`), the brief picks a provider whose `native_for` or `preferred_by`
metadata names that family, for example Playwright for Codex and the Python
document libraries for Claude Code. Remote-only capabilities are reported as
requiring network/credentials, not as installable.

The brief caps the providers per capability (at most a few, with an `... and
N more`/`+N more` marker) and adds one line saying that extra tools and MCP
servers cost context and accuracy.

## Daemon op

The daemon's read-only `toolchain` op plans tools for a request without
touching the graph:

```json
{"id": 1, "op": "toolchain",
 "capabilities": ["pdf"], "paths": ["docs/report.pdf"],
 "model_family": "claude", "probe_versions": false, "probe_sudo": false,
 "refresh": false}
```

`capabilities` and `paths` may each be one string or a bounded list (at most
32 capabilities of 80 characters / 64 paths of 2,000 characters); unknown
capabilities and malformed flags are answered `{ok: false, error}`, never an
exception. `probe_versions` and `probe_sudo` default to false, so an ordinary
request runs only local presence checks (`shutil.which`, `find_spec`), reads
the `[tools]` config and reports the host; no version or sudo command runs.
`text` is `tool_plan_text(plan)` normally, and the compact
`brief_lines(plan, model_family=...)` text when `model_family` is given. The
answer is `{ok: true, plan, text, cached}`; the plan is advisory and nothing
is installed.

Plans are cached in the daemon's memory for 60 seconds, at most 32 entries,
keyed by root, capabilities, paths, model family and the two probe flags.
`refresh: true` bypasses the read (and rebuilds the entry). The cache is
per daemon, so it never serves one repository's plan for another. `stats`
adds:

| Counter | Meaning |
| --- | --- |
| `toolchain_plans` | plans served, including cache hits |
| `toolchain_cache_hits` | answers served from the cache |
| `toolchain_errors` | invalid requests and failed plans |

The op does not take the graph lock: it reads no graph state, and a version
probe (only when explicitly requested) is bounded to 2 seconds per tool.

## Session brief

At session start, `ozagia.core.working_set.session_brief` reads the dirty
files from `git status --porcelain`. When their extensions name an
external-tool capability (`.pdf`, images, office documents, audio, video,
archives, diagrams, data files, HTML, Terraform, ...), it adds a small block
before the goal lines:

```text
[ozagia] tools: pdf in the changed files; advisory, nothing installed
pdf: missing; fitz, ghostscript, ...; installation not approved
```

The block uses `toolchain.brief_lines` filtered to one line per capability,
at most four lines including the header. It is inferred, not requested:
ordinary code changes (and clean trees) add nothing. The brief always passes
`probe_versions=False` and `probe_sudo=False`, even when the `[tools]`
config enables those probes elsewhere: a session start runs no version or
sudo command, makes no network call and installs nothing. Set
`[tools] brief = false` (repository or legacy config;
`toolchain.brief_enabled(root)` reads it) to turn the block off. The block is
part of the brief's `max_chars` budget and is dropped first when the text
would not fit; the mandatory MCP tools line and the goal reserve still fit.

## MCP tool

The full/coding MCP surface includes `dev_tools` (read-only):

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `capabilities` | array of strings | `[]` | capability or group ids (`pdf`, `documents`, `media`, ...) |
| `paths` | array of strings | `[]` | files whose extensions infer a capability |
| `model_family` | string | none | `claude`, `codex`, `gemini`, `cursor`, `aider`, `openhands` or `local` |
| `probe_versions` | boolean | false | run the bounded `--version` probes |
| `probe_sudo` | boolean | false | run the `sudo -n true` usability check |

Its description says: *Use before PDF/document/media/browser work to see
installed/missing tools and approval/install previews.* The handler returns
`{ok, plan, text}` where `text` is the compact `brief_lines` form. It is not
in the graph-only surface (that surface has no `dev_*` tools), and it adds no
other tool's behaviour to the coding mode. No install, network or model call
happens.

## What is not checked

- Version output is only the first non-empty line of an optional bounded
  probe; it is not validated against a minimum version.
- Network reachability, repository availability and package provenance are
  not checked.
- Remote MCP/API providers are not connected or authenticated; their
  credentials, quotas, permissions and network reachability are not checked.
- Local Node libraries (`pdf-lib`, Puppeteer) and agent-first providers such
  as OfficeCLI/Paper Office are not probed; the plan says so instead of
  guessing.
- Version ranges are not audited. `cli-tool-audit` is a possible future
  optional backend for pinned ranges, not part of this slice.
- Nothing is installed, and no install command is executed.
- GUI-only applications and app bundles may be installed without a
  discoverable executable (macOS `.app` bundles, some desktop-only tools).
- Package names differ between distributions; the registry records the common
  names and notes, but a repository may use another name or not carry a
  package.
- Tool behaviour, licences and whether the tool is appropriate for a specific
  file are not assessed.

## Extending the registry

Add a `ToolSpec` to `_TOOLS` and, for a new family, a `CapabilitySpec` to
`_CAPABILITIES` in `src/ozagia/core/toolchain.py`. Give binary entries their
probe alternatives and version arguments; give Python entries their import
name, distribution and, when appropriate, an optional extra. Use `_remote`
for an MCP/API provider and `_node` for a local Node library that cannot be
probed. Add `native_for`/`preferred_by` and `workflows` where there is a clear
model-family or task fit, and add a `GroupSpec` to `_GROUPS` for an umbrella
group. Install recipes are data. Keep ids and install order deterministic,
extend the tests, and add the capability to the documentation.
`[tools] preferred` is an optional per-repository ordering override; no code
change is needed for it.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_toolchain.py \
  tests/test_toolchain_cli.py
ruff check src tests
```
