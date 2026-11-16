# Releasing ozagia

A release is a git tag `vX.Y.Z` on `main`. The tag's workflow
(`.github/workflows/release.yml`) builds everything and makes a **draft**
GitHub release; a maintainer checks it and publishes it. Publishing is what
makes `curl -fsSL https://ozagia.com/install | bash` install it (the script
and the bundles are fetched from `releases/latest`).

## What a release contains

| asset | from |
|---|---|
| `ozagia-<version>-py3-none-any.whl`, `ozagia-<version>.tar.gz` | `uv build` |
| `ozagia-linux-x64.tar.gz`, `ozagia-linux-arm64.tar.gz`, `ozagia-darwin-arm64.tar.gz`, `ozagia-darwin-x64.tar.gz` | `scripts/release/build_bundle.py`, one per platform, built on that platform |
| `install` | the installer at that tag |
| `SHA256SUMS` | sha256 of every asset above |

Bundle names carry no version, so `releases/latest/download/<name>` always
names the newest. The version is inside the bundle (`manifest.json`).

A bundle is CPython from python-build-standalone plus the wheel and its
`treesitter` extra, installed with `pip --only-binary :all:`. OpenCode is not
in it: the bundle's manifest pins the OpenCode release (version, asset name
and sha256 per platform variant) and the installer downloads it. Both pins
live in `release/pins.json`.

## Steps

1. **Pins.** To move to a newer OpenCode or CPython:

   ```bash
   python scripts/release/update_pins.py --opencode 2.4.0
   python scripts/release/update_pins.py --runtime 20261201 --python 3.13.19
   ```

   The digests come from GitHub's release API. A new OpenCode can change the
   plugin API (`docs/intent.md`, Permissions; `ctx.tool.transform`), so run
   the plugin tests and one live session (`ozagia agent` on the local model)
   before releasing with it.

2. **Changelog.** Put the release notes under `## Unreleased` in
   `CHANGELOG.md` and commit them. Without that section, `prepare.py` writes
   the commit subjects since the last tag.

3. **Dry run.** Optional, but it catches packaging breakage before a tag
   exists: run the workflow by hand (Actions → release → Run workflow), or
   locally on this machine's platform:

   ```bash
   uv build --wheel -o dist .
   python scripts/release/build_bundle.py --wheel dist/ozagia-*.whl --out bundles
   scripts/release/smoke.sh bundles/ozagia-*.tar.gz   # the one bundle just built
   ```

   `smoke.sh` lays the bundle out as a release under a temp dir, runs
   `install` against it over `file://` into a throwaway `HOME`, then runs
   `ozagia --version`, `self info`, `self check --tui` (the real TUI must load
   the plugin), `ozagia index` on a scratch repository, a reinstall (OpenCode
   kept) and `self uninstall`. It downloads the pinned OpenCode once and makes
   no model call.

4. **Tag.** On a clean, up-to-date `main`:

   ```bash
   python scripts/release/prepare.py 0.1.0 --dry-run   # shows the version and changelog
   python scripts/release/prepare.py 0.1.0 --push
   ```

   It refuses to run on another branch (`--allow-branch` overrides), with
   uncommitted changes, with a tag that exists, or with a version that is not
   newer. It sets `version` in `pyproject.toml`, dates the changelog entry,
   commits `Release v0.1.0`, makes the annotated tag, and with `--push`
   pushes both. Pre-releases use PEP 440 versions: `0.2.0rc1` gives the tag
   `v0.2.0rc1`, and the GitHub release is marked a pre-release.

5. **CI.** The workflow runs:
   - the tests;
   - a check that the tag matches `pyproject.toml`;
   - the wheel and sdist;
   - the four bundles on `ubuntu-24.04`, `ubuntu-24.04-arm`, `macos-15` and
     `macos-15-intel`, each with the smoke test;
   - the draft release, with the notes from `scripts/release/notes.py` (the
     changelog section plus install instructions).

6. **Publish.** Open the draft on GitHub, check the assets and notes, and
   publish. Then, on a clean machine or account:

   ```bash
   curl -fsSL https://ozagia.com/install | bash
   ozagia self check --tui
   ```

7. **After.** Set `pyproject.toml` back to a `.dev0` version for the next one
   (for example `0.1.1.dev0`), so builds from `main` are never mistaken for
   the release.

## Undoing a release

Delete or unpublish the GitHub release (`releases/latest` then falls back to
the previous one) and delete the tag if it was never published. Never reuse
a version number. Users who installed it upgrade with
`ozagia self upgrade --version <previous>`.
