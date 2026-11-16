# Ocelot: explicit resolution and installation

Ocelot is a separate MIT-licensed checkout. ozagia does not bundle it, vendor
it or download it on first run. Reconciliation with Ocelot is optional:
without it, ozagia's own reconciliation still works, just without Ocelot's
structural machinery. The plan's distribution item is implemented by
`ozagia.integrations.ocelot`; this module only resolves, installs and verifies
a checkout. It never imports Ocelot and never decides that reconciliation
should use it.

## Resolution order

`resolve(root)` (default `root`: `repo_root()`) tries, in order:

1. `$OZAGIA_OCELOT`, an explicit checkout or managed install directory;
2. a sibling checkout, `<root>/../ocelot` (the development layout);
3. the newest valid install manifest under
   `<data home>/ozagia/ocelot/<version>/`.

`<data home>` is `$XDG_DATA_HOME` when it is absolute, otherwise
`~/.local/share`. `--dest` replaces the install root for any command.

If an explicit `$OZAGIA_OCELOT` path does not exist or is not a directory, that
is an error rather than a silent fall-through to another checkout. If nothing
resolves, the result is the informational state `not_installed`; no download
is attempted.

```bash
python -m ozagia.integrations.ocelot resolve --root .
python -m ozagia.integrations.ocelot status --root . --json
```

`status` reports the resolved path, version, source, install manifest summary
and the licence note. A development checkout or `$OZAGIA_OCELOT` install has
no ozagia manifest, so `installed` is false and `source` is `environment` or
`sibling`.

## Install a pinned version

There is no default version, URL or first-run download. A real install needs
all of:

- `--version`, a bounded version name that also names the install directory;
- `--url`, an HTTPS `.tar.gz`, `.tgz` or `.zip` archive;
- `--sha256`, the 64-hexadecimal archive checksum;
- `--apply` (or `allow_download=True` in the library API).

Without `--apply`, `install` is a dry run: it validates the arguments and
prints the destination, but it does not resolve DNS, download or write. A
checksum is required even for a dry run.

```bash
# Preview. No network, no write.
python -m ozagia.integrations.ocelot install \
  --version 1.2.3 \
  --url https://example.com/ocelot-1.2.3.tar.gz \
  --sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef

# Install the pinned archive.
python -m ozagia.integrations.ocelot install \
  --version 1.2.3 \
  --url https://example.com/ocelot-1.2.3.tar.gz \
  --sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --apply
```

The result lives at `<install root>/<version>/` (default
`~/.local/share/ozagia/ocelot/1.2.3/`) with
`.ozagia-ocelot.json` next to the extracted tree. The manifest records the
format, version, source URL (without query or fragment), archive SHA-256,
archive metadata, install time, licence and every extracted file with its
SHA-256 and size. A single top-level directory in the archive is stripped, so
the version directory is the checkout root.

The archive is checked before extraction. Extraction runs into a temporary
directory on the same filesystem and is renamed into place only after the
manifest is written. A failed download, mismatch, unsafe member or failed
extraction leaves no version directory behind. Existing version directories
are never overwritten, including a directory for the same version; installing
the same version and checksum is idempotent, while different content is
refused. Remove an old install deliberately if it must be replaced.

## Verification

```bash
python -m ozagia.integrations.ocelot verify --version 1.2.3
python -m ozagia.integrations.ocelot verify --dest ~/.local/share/ozagia/ocelot --json
```

`verify` requires a readable ozagia install manifest. It checks every recorded
file's SHA-256 and size, rejects symlinked or unsafe paths, checks the manifest
tree digest, and optionally compares a supplied `--sha256` with the archive
checksum in the manifest. Extra files are reported but do not fail the check;
`__pycache__/` and `*.pyc`/`*.pyo` are ignored. A missing or malformed install
manifest is a validation error (exit 2); files that do not verify return
`ok: false` and exit 1.

## Transport and security rules

The installer accepts only HTTPS URLs. It rejects:

- URLs containing credentials in the authority;
- literal private, loopback, link-local, multicast, reserved or otherwise
  non-global addresses;
- hosts named `localhost` or ending in `.localhost`, `.local`, `.internal` or
  `.home.arpa`;
- fragments, non-archive extensions, and archives over the size cap;
- redirects beyond the cap, redirects without a `Location`, and HTTPS-to-HTTP
  downgrades (every hop is validated again).

Downloaded archives are capped at 100 MiB and extracted content at 512 MiB,
with a 50,000-member cap. Archive members must be regular files or
directories. Absolute paths, `..`, drive letters, backslashes, control
characters, symlinks, hard links and device/FIFO members are rejected. The
address checks are fail-closed: a host with any non-public address is refused.
The download happens only in the applied install path; `resolve`, `status`,
`verify` and dry runs never use the network. Installed resolution is offline.

This is an SSRF and archive-safety guard, not a sandbox. The checksum is the
only integrity anchor; the installer does not verify a signature, audit the
archive, or establish that the code is safe to run. Pin a checksum obtained
through a trusted channel, and review the source if that matters.

## Exit status

| Code | Meaning |
| --- | --- |
| `0` | Resolve/status/dry-run completed, install applied, or verification passed |
| `1` | `verify` found missing, changed or unsafe files, or a checksum mismatch |
| `2` | Invalid arguments, unsafe URL, malformed manifest or I/O error |

## Licence and scope

Ocelot is MIT-licensed. Its licence and attribution stay with the Ocelot
checkout; ozagia does not relicense it or include its source in the ozagia
package or wheel. Reconciliation remains optional, and structural agreement
or analogy from Ocelot is a candidate signal, never a truth verdict.
