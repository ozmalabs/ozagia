# Python environments

`ozagia.core.environments` finds a project's Python interpreters, finds out
what each provides, and answers which of them can run a set of files. It
reads source with `ast` and imports no project module. It does run
interpreters: a probe starts `python -I -c <probe>`, and that runs the
interpreter's `site` setup, including the `.pth` files and `sitecustomize`
of its site-packages. Interpreters the project ships are therefore only run
when the user trusts them (see [What is run](#what-is-run)).

It exists because of the 17 Nov 2026 Ocelot eval
([results](results/ocelot_live_eval_2026-11-17.md)): the harness ran task
checks with ozagia's own interpreter, which had no `numpy`, while Ocelot's
`.venv` had it and `numpy` was declared only in an optional requirements
file. Nothing knew which interpreter had which imports.

## Usage

```sh
ozagia env [--root R] [--json] [--no-cache]
ozagia env [--root R] --for PATH [PATH ...] [--json] [--no-cache]
```

- Without `--for`: every candidate interpreter (path, where it was found,
  version, venv or base, number of distributions) and the Python dependencies
  the tracked manifests declare, with the file and group of each.
- With `--for`: the interpreters ranked for those files (root-relative files
  or directories; a `path::node` suffix is dropped), the best one, and per
  third-party import whether it is required, which files import it, how many
  interpreters have it and which manifests declare it. Exit 0 when some
  interpreter has every required import, 1 when none does, 2 for a path
  outside the root.
- `--no-cache` probes every interpreter and neither reads nor writes the
  caches.
- Interpreters read statically are marked `read statically, not run`, with
  a note saying how to trust them.

`ozagia env list|show` are the declarative environment profiles of
`.ozagia.toml` and are unrelated ([sre_operations.md](sre_operations.md)).

The MCP tool `dev_environment` returns the same documents: no arguments for
the listing, `paths` for `env_for`, `module` for `who_has`.

## Candidates

In order of evidence; the same interpreter found twice (`python` and
`python3` in one directory, or a doc naming `.venv/bin/python`) is listed
once, with the other evidence in `also`. A venv's `python` and the base
interpreter it links to are different candidates.

1. `$VIRTUAL_ENV`.
2. `.venv`, `venv` and `.venv-*` in the root.
3. When the root has none of those and is a git worktree or a clone of a
   local repository: the same directories in the source repository.
4. The lock file's tool environment: `uv.lock` (`$UV_PROJECT_ENVIRONMENT`
   or `.venv`), `poetry.lock` (`poetry env info --path`, only if `poetry` is
   installed), `pdm.lock` (`.venv`, else `pdm info --python`). Tools run
   with a 10 s timeout.
5. Interpreters named in the command docs (`index.command_docs`, default
   `key_docs`): paths such as `.venv/bin/python` or `~/envs/x/bin/python3`,
   and versioned names such as `python3.12` on `PATH`. Only existing
   executables count.
6. `python` and `python3` on `PATH`.
7. The interpreter ozagia runs under.

## What is run

A candidate is *project-shipped* when its path (with the directory
resolved, the file not), its venv directory (the one holding `pyvenv.cfg`)
or its resolved binary lies inside the root or the source repository. That
covers the root's and the source repository's venvs, in-root paths named in
the command docs, and a lock tool's answer (`pdm info --python`) pointing
inside. The candidate path decides, not only where the binary resolves: a
committed `.venv/bin/python` linking to a system interpreter, next to a
committed `pyvenv.cfg` and a `.pth`, would run that `.pth` through the
system interpreter.

Project-shipped interpreters are run only when trusted:

- `OZAGIA_TRUST_PROJECT_PYTHON=1` in ozagia's environment, or
- `[environments] trust_project_python = true` in the user-level
  `$XDG_CONFIG_HOME/ozagia/config.toml` (default `~/.config/ozagia/`). A
  config file inside the root does not count, and a repository's
  `.ozagia.toml` cannot set it. This mirrors the linter's
  `OZAGIA_LINT_TRUST_VENV` / `[lint] trust_project_venv`.

The eval harness passes trust explicitly: it runs the repository's tests
under the chosen interpreter anyway. `ozagia env`, `dev_environment`,
`review-repo` and `dev_review_repo` use the setting.

An untrusted project-shipped venv gets a *static probe* (`probe: "static"`)
instead: `pyvenv.cfg` (`version`/`version_info`,
`include-system-site-packages`, `home`), the entries of
`lib/python*/site-packages`, and each `*.dist-info` (or `*.egg-info`)
`METADATA` name, `top_level.txt` or else `RECORD`, all read as text. `.pth`
path lines are listed; `.pth` import lines are not run. A static probe
cannot see what `sitecustomize`, `.pth` import lines or editable-install
finders add, and it uses this interpreter's standard-library list. With
`include-system-site-packages = true` it adds the packages of a run
candidate in the venv's `home` directory, when there is one. A
project-shipped interpreter that is not a venv is not probed (the probe
fails with that reason). The interpreter ozagia runs under is run even when
it is inside the root: its `site` setup has already run in this process.

Every interpreter that is run gets a minimal environment: `PATH`, `HOME`,
`LANG`, `LC_*` and `TMPDIR`. Tokens, `OZAGIA_*`, `PYTHON*` and
`VIRTUAL_ENV` are not passed.

## Probes

A probe prints JSON: version, `sys.executable`, `sys.prefix`,
`sys.base_prefix` (a venv when they differ), the standard library module
names, and the top-level import names the interpreter can find: names in
each non-standard-library `sys.path` directory (site-packages, `.pth`
entries such as editable installs, and the user site for a base
interpreter, which `-I` leaves out) and, per installed distribution, the
names in its `top_level.txt` or else the first components of its `RECORD`.

Probes are bounded: 10 s per interpreter and 8 MiB of output, spooled to a
temporary file. A failed probe (missing interpreter, timeout, non-zero
exit, no JSON) is reported with its reason. It is remembered for the rest
of the process, so a hanging interpreter costs its timeout once per run,
and it is not written to the cache file.

Results are cached in `<state dir>/environments/probes.json` by interpreter
path and probe kind. An entry is used only while the mtimes of the
interpreter, of its venv's `pyvenv.cfg` and of every directory the probe
scanned are unchanged; installing or removing a distribution changes its
`site-packages` mtime, and editing `include-system-site-packages` changes
`pyvenv.cfg`. Within one process probes are also memoized.

Parsed imports are cached in `<state dir>/environments/imports.json` by git
blob id, for tracked files whose working-tree content is the index's (`git
diff-files` does not list them). The cache keeps only the blobs of the
current tree.

## Imports

Each import is classified:

| kind | where |
|---|---|
| `module` | runs when the file is imported (module level, class bodies, plain `if`/`for`/`with` blocks) |
| `lazy` | inside a function, or under `if __name__ == "__main__"` |
| `conditional` | under an `if` (or its `else`/`elif`) that tests `sys.version_info`, `sys.platform`, `sys.implementation`, `os.name` or `platform` |
| `guarded` | in a `try` whose handler catches `ImportError`, `ModuleNotFoundError`, `Exception` or everything; under `if TYPE_CHECKING`; after `pytest.importorskip("x")` in the same function (imports of `x`); anywhere in a file whose module level calls `pytest.importorskip("x")` (every import of `x`) |

A top-level name is standard library when it is in this interpreter's
`sys.stdlib_module_names` or `sys.builtin_module_names` (an interpreter with
its own list must have it too). It is first-party when the tree has
`name.py` or a `name/` directory with Python files under the file's own base
directory (the first ancestor without `__init__.py`, which pytest and
`python script.py` put on `sys.path`), the root or an `index.import_roots`
entry. Failing that, a module or package of that name anywhere in the tree
counts as first-party (for scripts that extend `sys.path`), unless an
installed distribution in a probed interpreter provides the name: then it
is third-party. On Ocelot, `domains/chess.py` does not make `import chess`
(python-chess) first-party. Everything else is third-party.

For `env_for(paths)`, the files asked about need their `module` and `lazy`
imports. Their first-party imports are followed transitively (with each
parent package's `__init__.py`, and for a test file the `conftest.py` files
of its directory and its parents); those files need their `module` imports.
The closure is capped at 5000 files. Closure-level `lazy` imports and every
`guarded` import are optional.

A `conditional` import is optional, except where its test can be evaluated
for an interpreter: `sys.version_info` (or `sys.version_info[:n]`) compared
with a tuple of integers, and `sys.platform` compared with a string or
`sys.platform.startswith(...)`, each possibly negated, and the `else` of
such a test. The version is the interpreter's probed version; the platform
is this machine's. Where the branch is taken and the import would be
required without the test, the import is required for that interpreter;
where the branch is not taken, the import is ignored for it. Other tests
(`platform.system()`, `and`/`or` combinations) leave it optional.
First-party modules imported under a condition are followed.

## Ranking

An interpreter is complete when it provides every required module (and, for
the eval, the module the check runs with `-m`). Interpreters are ranked by
the number of missing required modules, then of missing optional ones,
then the candidate order; failed probes come last. `best` is the first
complete interpreter, or null.

`who_has(root, module)` returns `[{"interpreter", "version", "probe"}]` for the
candidates that can import the module's top-level name, in candidate order.

`module_environment(root, module)` is what the failure classifier
(`ozagia.core.failures`, W21) calls for an `environment` failure: one
discovery and probe pass gives `available_in` (`who_has`'s items) and
`declared_in` (the manifest entries that declare the module, `[{"file",
"optional", "group"}]`, `[]` when none does, through the declared
dependencies below and the probes' distribution names), with `trusted`.
Trust is the user's (`trust_project_python`) unless the caller passes it:
an untrusted project venv is read statically, never run. Answers are
memoized in-process for `MODULE_ENV_TTL_S` (300 s) per root, module, trust
and cache mode, so the daemon, which classifies every captured pytest that
did not pass, discovers once per module and not per run.

## Declared dependencies

The manifest readers (`ozagia.core.manifests`, shared with the repo review)
read `pyproject.toml` (`dependencies`, `optional-dependencies`,
`dependency-groups`, Poetry dependencies, groups and dev dependencies),
`requirements*.txt`, `package.json`, `Cargo.toml` and `go.mod`. Each
dependency records its file, whether the declaration is optional and its
group: the extra or group name, or `<group>` of `requirements-<group>.txt`.
A requirements file whose group names the main set (`base`, `main`, `prod`,
`production`, `core`, `runtime`, `common`, `install`) is not optional.
Manifests under `.venv`, `node_modules` and the other generic skip
directories are ignored. A manifest anywhere in the tree counts for every
file; subprojects are not told apart.

Import names are matched to distribution names by normalization (case and
`-`, `_`, `.`), a table of known aliases (`yaml`: PyYAML, `PIL`: Pillow,
`sklearn`: scikit-learn, ...) and what the probed interpreters install the
module from.

## Uses

- The eval harness picks the check interpreter with `env_for` over the
  check files ([eval_agent.md](eval_agent.md)).
- The repo review's dependencies section reports `undeclared_import`,
  `optional_only_import` and `test_env_gap` ([repo_review.md](repo_review.md)).
  It writes both caches, checks its budget before discovery and between
  probes (each probe's timeout is cut to the time left), and records the
  probed set (path, probe kind, version) as the population of the three
  checks: `--compare` does not compare `test_env_gap` across reviews that
  probed different sets, and holds resolved import findings.

## Limits

- Only interpreters on this machine are probed. A conda environment, a
  container image or a CI runner is not found unless it is named in a
  command doc or active in `$VIRTUAL_ENV`.
- Dynamic imports (`importlib.import_module`, `__import__`) and imports
  through `sys.path` changes to directories outside the tree are not seen.
- A probed interpreter "provides" a module when its top-level name is
  installed; versions and import-time failures inside the package are not
  checked.
- A static probe sees only what is on disk in site-packages (see [What is
  run](#what-is-run)).
- Environments are not graph nodes; the probe cache is a JSON file in the
  state directory.
