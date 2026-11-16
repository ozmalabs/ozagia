# Installing ozagia

```bash
curl -fsSL https://ozagia.com/install | bash
```

`https://ozagia.com/install` serves the `install` script at the root of this
repository, as attached to the latest release. Options go after `bash -s --`:

| option | meaning |
|---|---|
| `--version 0.1.0` | that release instead of the latest |
| `--bundle FILE` | install a downloaded `ozagia-<target>.tar.gz` (no ozagia download) |
| `--no-opencode` | skip OpenCode; ozagia then uses `opencode` from PATH |
| `--no-modify-path` | leave shell startup files alone |
| `--base-url URL` | a mirror of the release downloads (also `$OZAGIA_RELEASES_URL`) |
| `--opencode-url URL` | where OpenCode is fetched, with `{name}` for the asset (also `$OZAGIA_OPENCODE_URL`) |

`$OZAGIA_HOME` (default `~/.ozagia`) moves the whole install.

## What it installs

```
~/.ozagia/
  bin/ozagia                    the launcher (the one directory added to PATH)
  current -> versions/<version>
  versions/<version>/
    python/                     CPython from python-build-standalone, with
                                ozagia and the tree-sitter parsers installed
    manifest.json               version, target, pinned OpenCode release
    LICENSE, THIRD_PARTY.md
  opencode/<version>/opencode   OpenCode, from its own GitHub release
```

The launcher runs `current/python/bin/python3 -I -m ozagia`, so no Python,
pip or virtualenv on the machine is used, and `PYTHONPATH` and user site
packages are ignored. It sets `OZAGIA_OPENCODE` to the pinned OpenCode. The
`opencode` you may have installed yourself (`~/.opencode/bin`) is left
alone; `ozagia --opencode system` (or `OZAGIA_OPENCODE=opencode`) uses it.

Checks: the bundle must match the release's `SHA256SUMS`, and OpenCode must
match the sha256 pinned in the bundle's `manifest.json`. That pin is
`release/pins.json` in this repository at the release's tag. Any mismatch
stops the install before anything is replaced. On x64 machines without AVX2
the installer takes OpenCode's `baseline` build, as OpenCode's own installer
does.

Platforms: `linux-x64`, `linux-arm64` (glibc; Alpine/musl is refused) and
`darwin-arm64`, `darwin-x64`. Windows is not supported: the graph daemon uses
Unix sockets.

## Using it

```bash
cd /path/to/repo
ozagia                 # the TUI on this repository (as `ozagia agent`)
ozagia ../other-repo   # the TUI on another directory
ozagia agent --prompt "make tests/test_x.py pass" -m provider/model
ozagia index           # build or refresh the graph snapshot only
```

Models and providers are OpenCode's: configure them as for OpenCode
(`opencode auth login`, `~/.config/opencode/opencode.json`). The pinned
OpenCode reads the same configuration.

## Managing it

| command | does |
|---|---|
| `ozagia self info` | version, target, runtime, OpenCode path |
| `ozagia self check [--tui]` | runtime, parsers, OpenCode version, plugin; `--tui` starts the TUI under a pty and waits for the plugin to load (no model call) |
| `ozagia self upgrade [--version V]` | runs the installer again (`$OZAGIA_INSTALL_URL`, default `https://ozagia.com/install`) |
| `ozagia self uninstall --yes` | removes `~/.ozagia` and the PATH line the installer added |

An upgrade unpacks the new version, switches `current`, and then removes the
old version. OpenCode is downloaded again only when the pinned version or
checksum changed.

## From source

```bash
uv pip install -e '.[all]'
```

This gives the same `ozagia` command, using the `opencode` on PATH.
