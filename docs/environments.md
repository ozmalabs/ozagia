# Python environments

`ozagia.core.environments` finds a project's Python interpreters, asks each
what it provides, and answers which of them can run a set of files. It reads
source with `ast` and never imports or runs project code: an interpreter is
only started as `python -I -c <probe>` from a temporary directory.

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
  cache.

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
exit, no JSON) is reported with its reason and not cached.

Results are cached in `<state dir>/environments/probes.json` by interpreter
path. An entry is used only while the interpreter's mtime and the mtime of
every directory the probe scanned are unchanged; installing or removing a
distribution changes its `site-packages` mtime. Within one process probes
are also memoized. The repo review reads the cache but never writes it.

## Imports

Each import is classified:

| kind | where |
|---|---|
| `module` | runs when the file is imported (module level, class bodies, plain `if`/`for`/`with` blocks) |
| `lazy` | inside a function, or under `if __name__ == "__main__"` |
| `guarded` | in a `try` whose handler catches `ImportError`, `ModuleNotFoundError`, `Exception` or everything; under `if TYPE_CHECKING`; `pytest.importorskip("x")` (at module level, every import of `x` in that file) |

A top-level name is standard library when it is in this interpreter's
`sys.stdlib_module_names` or `sys.builtin_module_names` (an interpreter with
its own list must have it too). It is first-party when the tree has
`name.py` or a `name/` directory with Python files under the file's own base
directory (the first ancestor without `__init__.py`, which pytest and
`python script.py` put on `sys.path`), the root or an `index.import_roots`
entry, or a module or package of that name anywhere in the tree (for
scripts that extend `sys.path`). Everything else is third-party.

For `env_for(paths)`, the files asked about need their `module` and `lazy`
imports. Their first-party imports are followed transitively (with each
parent package's `__init__.py`, and for a test file the `conftest.py` files
of its directory and its parents); those files need their `module` imports.
The closure is capped at 5000 files. Closure-level `lazy` imports and every
`guarded` import are optional.

## Ranking

An interpreter is complete when it provides every required module (and, for
the eval, the module the check runs with `-m`). Interpreters are ranked by
the number of missing required modules, then of missing optional ones,
then the candidate order; failed probes come last. `best` is the first
complete interpreter, or null.

`who_has(root, module)` returns `[{"interpreter", "version"}]` for the
candidates that can import the module's top-level name, in candidate order.
The failure classifier calls it to say which interpreter has a module a
check could not import.

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

## Limits

- Only interpreters on this machine are probed. A conda environment, a
  container image or a CI runner is not found unless it is named in a
  command doc or active in `$VIRTUAL_ENV`.
- Dynamic imports (`importlib.import_module`, `__import__`) and imports
  through `sys.path` changes to directories outside the tree are not seen.
- A probed interpreter "provides" a module when its top-level name is
  installed; versions and import-time failures inside the package are not
  checked.
- Environments are not graph nodes; the probe cache is a JSON file in the
  state directory.
