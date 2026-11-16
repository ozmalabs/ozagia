#!/usr/bin/env bash
# Install a built bundle the way users do and check that it works.
#
#   scripts/release/smoke.sh dist/ozagia-<target>.tar.gz [--no-tui]
#
# Lays the bundle out like a GitHub release (download/v<version>/ and
# latest/download/, with SHA256SUMS) under a temp dir, runs ./install against
# it over file:// into a throwaway HOME, then runs the installed ozagia:
# --version, self info, self check (with --tui: the OpenCode TUI must load the
# plugin), and `ozagia index` on a scratch repository. Downloads the pinned
# OpenCode from upstream, once per run. Makes no model call.
set -euo pipefail

bundle="${1:?usage: smoke.sh <bundle.tar.gz> [--no-tui]}"
tui="--tui"
[ "${2:-}" = "--no-tui" ] && tui=""
here="$(cd "$(dirname "$0")/../.." && pwd)"
name="$(basename "$bundle")"
work="$(mktemp -d "${TMPDIR:-/tmp}/ozagia_smoke.XXXXXX")"
trap 'rm -rf "$work"' EXIT

version="$(tar -xzOf "$bundle" "${name%.tar.gz}/manifest.json" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
rel="$work/releases"
mkdir -p "$rel/download/v$version" "$rel/latest"
cp "$bundle" "$rel/download/v$version/"
(cd "$rel/download/v$version" \
    && { sha256sum "$name" 2>/dev/null || shasum -a 256 "$name"; } > SHA256SUMS)
ln -s "../download/v$version" "$rel/latest/download"

home="$work/home"
mkdir -p "$home"
touch "$home/.bashrc" "$home/.profile"
run() { env -i HOME="$home" SHELL=/bin/bash TERM="${TERM:-xterm}" \
            PATH="$home/.ozagia/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
# The installer as a new user runs it: ~/.ozagia/bin not on PATH yet.
fresh() { env -i HOME="$home" SHELL=/bin/bash TERM="${TERM:-xterm}" \
              PATH="/usr/local/bin:/usr/bin:/bin" "$@"; }

echo "==> install $name ($version)"
fresh bash "$here/install" --base-url "file://$rel"
grep -q "$home/.ozagia/bin" "$home/.bashrc" || { echo "PATH line not added"; exit 1; }

echo "==> ozagia --version"
got="$(run ozagia --version)"
[ "$got" = "ozagia $version" ] || { echo "unexpected: $got"; exit 1; }

echo "==> ozagia self info"
run ozagia self info

echo "==> ozagia self check $tui"
run ozagia self check $tui

echo "==> ozagia index on a scratch repository"
repo="$work/repo"
mkdir -p "$repo/pkg" "$repo/tests"
printf 'def add(a, b):\n    return a + b\n' > "$repo/pkg/mod.py"
printf 'from pkg.mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n' \
    > "$repo/tests/test_mod.py"
git -C "$repo" init -q
git -C "$repo" add -A
git -C "$repo" -c user.email=s@s -c user.name=smoke commit -qm init
(cd "$repo" && run ozagia index --json > "$work/index.json")
python3 - "$work/index.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("index:", {k: d[k] for k in d if k in ("action", "stale", "nodes", "edges")})
EOF

echo "==> reinstall is idempotent (OpenCode kept)"
out="$(fresh bash "$here/install" --base-url "file://$rel" --version "$version")"
case "$out" in *"already installed"*) ;; *) echo "OpenCode downloaded again"; exit 1 ;; esac
[ "$(grep -c "$home/.ozagia/bin" "$home/.bashrc")" = 1 ] || { echo "PATH line added twice"; exit 1; }

echo "==> uninstall"
run ozagia self uninstall --yes
[ ! -e "$home/.ozagia" ] || { echo "~/.ozagia still there"; exit 1; }
! grep -q "$home/.ozagia/bin" "$home/.bashrc" || { echo "PATH line still there"; exit 1; }

echo "==> smoke ok: $name $version"
