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
tmpdir="${TMPDIR:-/tmp}"
work="$(cd "$(mktemp -d "${tmpdir%/}/ozagia_smoke.XXXXXX")" && pwd -P)"
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

echo "==> research tools over MCP"
mkdir -p "$work/ws"
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | run ozagia mcp --root "$work/ws" --mode research > "$work/mcp.jsonl"
python3 - "$work/mcp.jsonl" <<'EOF2'
import json, sys
tools = [t["name"] for line in open(sys.argv[1]) for d in [json.loads(line)]
         if d.get("id") == 2 for t in d["result"]["tools"]]
assert "research_init" in tools and "research_paper_review_start" in tools, tools[:10]
print("research tools:", len(tools))
EOF2

echo "==> infrastructure (IaC) adapter"
iac="$work/iac"
mkdir -p "$iac/infra" "$iac/k8s"
printf 'resource "aws_s3_bucket" "logs" {\n  bucket = "app-logs"\n}\n' > "$iac/infra/main.tf"
printf 'apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n  namespace: app\nspec:\n  selector:\n    app: web\n' > "$iac/k8s/svc.yaml"
printf '[adapters]\nmodules = ["ozagia.integrations.iac.adapter:register"]\n' > "$iac/.ozagia.toml"
git -C "$iac" init -q
git -C "$iac" add -A
git -C "$iac" -c user.email=s@s -c user.name=smoke commit -qm init
(cd "$iac" && run ozagia index --json > "$work/iac.json")
python3 - "$iac/.ozagia/graph.json" <<'EOF2'
import json, sys
nodes = json.load(open(sys.argv[1]))["nodes"]
infra = [n for n in nodes if n["type"] == "InfraResource"]
assert len(infra) >= 2, [n["type"] for n in nodes]
print("infra resources:", len(infra))
EOF2

echo "==> reinstall is idempotent (OpenCode kept)"
out="$(fresh bash "$here/install" --base-url "file://$rel" --version "$version")"
case "$out" in *"already installed"*) ;; *) echo "OpenCode downloaded again"; exit 1 ;; esac
[ "$(grep -c "$home/.ozagia/bin" "$home/.bashrc")" = 1 ] || { echo "PATH line added twice"; exit 1; }

echo "==> uninstall"
run ozagia self uninstall --yes
[ ! -e "$home/.ozagia" ] || { echo "~/.ozagia still there"; exit 1; }
! grep -q "$home/.ozagia/bin" "$home/.bashrc" || { echo "PATH line still there"; exit 1; }

echo "==> smoke ok: $name $version"
