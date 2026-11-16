# Operations graph (experimental)

The opt-in IaC adapter (see [iac_context.md](iac_context.md)) also reads the
files that describe how a project runs: docker/podman compose files,
Containerfiles/Dockerfiles, podman quadlets and systemd units. Like the rest
of the adapter this is static, partial source analysis
(`ozagia.core.ops_graph`): nothing runs podman, docker or systemctl, and no
live container or unit state is read.

```toml
[adapters]
modules = ["ozagia.integrations.iac.adapter:register"]
```

Compose files need the `ozagia[iac-yaml]` extra for full extraction. Without
PyYAML, a compose file still gives its service names, lines and plain
`image:`/`container_name:` values (confidence `partial`) and an
`infra_status` diagnostic. The other formats use the standard library.

## Files

| Format | Names |
|---|---|
| compose | `compose.yaml`, `compose.*.yaml`, `docker-compose*.yml`, `podman-compose*.yml` (`.yml`/`.yaml`) |
| Containerfile | `Containerfile`, `Dockerfile`, `Containerfile.*`, `Dockerfile.*`, `*.containerfile`, `*.dockerfile` (any case) |
| quadlet | `*.container` anywhere; `*.pod`, `*.network`, `*.volume`, `*.kube`, `*.image`, `*.build` under a `containers/systemd` or `quadlet(s)` directory |
| systemd unit | `*.service`, `*.timer`, `*.socket`, `*.target`, `*.path` (files without unit sections give no node) |

A compose `build.dockerfile` with another name is parsed when the compose
file names it.

## Node keys

Nodes are `InfraResource` nodes with `family: ops`; `<rel>` is the
repository-relative path.

| Key | Node |
|---|---|
| `ops:compose/<rel>` | compose project (one per file) |
| `ops:compose/<rel>#service/<name>` | compose service |
| `ops:compose/<rel>#network/<name>`, `#volume/<name>` | compose network, named volume |
| `ops:containerfile/<rel>` | Containerfile/Dockerfile |
| `ops:containerfile/<rel>#stage/<name>` | build stage (`AS` name, else its index) |
| `ops:quadlet/<rel>` | quadlet (`kind`: `quadlet_container`, `quadlet_pod`, ...) |
| `ops:unit/<rel>` | systemd unit (`kind`: `systemd_service`, `systemd_timer`, ...) |
| `ops:image/<ref>` | an image reference, shared by every file that names it |

Image references are normalised so formats meet on one node:
`docker.io/`, `index.docker.io/`, `library/` and `localhost/` prefixes are
dropped and a missing tag becomes `:latest` (`localhost/shop-web` built by
compose and `shop-web:latest` run by a quadlet are the same node).

## Attributes and edges

Retained attributes: compose services (`image`, `container_name`, `ports`,
`expose`, `volumes`, `networks`, `depends_on`, `env_files`, `env`,
`profiles`, `entrypoint` program, `build_context`, `project`), stages
(`base_image`, `from_stage`, `expose`, `entrypoint`/`cmd` program, `args`,
`env`, `user`), quadlets (`image`, `pod`, `networks`, `volumes`, `ports`,
`env_files`, `env`, `container_name`, `service_unit`, `secrets` names) and
units (`exec` programs, `env_files`, `env`, `working_directory`,
`requires`/`wants`/`after`/`before`/`part_of`/... names, `on_calendar`,
`listen`, `unit` for timers, sockets and paths).

Edges between ops nodes are `infra_<relation>` (so `infra_dependencies` and
`infra_impact` walk them): `has_service`, `has_network`, `has_volume`,
`has_stage`, `depends_on`, `uses_network`, `mounts_volume`, `uses_image`,
`builds_from` (compose build → Containerfile or its `target` stage),
`built_from` (image → Containerfile), `base_image`, `from_stage`,
`copies_from`, `mounts_from`, `in_pod`, `pulls`, `builds_image`, `triggers`
(timer/socket/path → unit), and the unit dependency names (`requires`,
`wants`, `after`, ...). A unit dependency on `web.service` resolves to the
quadlet that generates it (`web.container` → `web.service`, `web.pod` →
`web-pod.service`).

Edges to repository files are `ops_<relation>`: `runs` (a path in a unit's
`ExecStart*` line that is in the repository: absolute under the root, or
relative to a `WorkingDirectory` in the repository, else to the root or the
unit's directory), `env_file`, `mounts` (compose bind mounts, quadlet
`Volume=./x`) and `kube_yaml`.

## Secrets

Environment and build-argument values are never stored: `ENV`, `ARG`,
`Environment=`, compose `environment:` and env files give names only, and env
files are linked by path and never read. Commands keep only their program
(`ExecStart`'s binary, `ENTRYPOINT`'s first word), never arguments. Every
retained string is bounded to 256 characters and replaced with `[REDACTED]`
when it looks like a credential (token prefixes such as `ghp_`/`sk-`/`xox?-`,
AWS key IDs, `Bearer`, private keys, `user:pass@` URLs, `password=...`,
JWTs, long mixed-case opaque strings); `${VAR:-default}` keeps only `${VAR}`.
Ops sources and `.env`/`*.env` files are left out of automatic snippets.

## Queries

`infra.ops` (graph query) and the `infra_ops` MCP tool:

```json
{"query": "dependents", "target": "ops:image/postgres:16"}
{"query": "exposes", "target": "web"}
{"query": "runs", "target": "scripts/backup.py"}
```

- `dependents`: nodes with a declared edge to the target, up to three hops
  (`has_*` containment edges are left out).
- `exposes`: ports, exposed ports, sockets, volumes, networks, env names,
  image and program of the matching nodes.
- `runs`: units, quadlets and compose services that run, mount or read a
  repository file (`file:` key or path).

`target` is an `ops:` key, a file path, or a name: a service, unit
(`backup` means `backup.service`), quadlet, container or image.
`infra.locate`, `infra.dependencies` and `infra.impact` also cover ops nodes.

## After log and status commands

When one of these ran, its output gets `[ozagia] ops:<key>: ...` lines naming
the nodes it is about, as a pytest summary gets `test:` keys:

| Command | Matches |
|---|---|
| `podman logs N`, `docker logs N`, `podman container logs N` | `container_name` (compose, quadlet `ContainerName=` or the default `systemd-<name>`), compose's `<project>-<service>-<n>`, else a service or quadlet name |
| `docker compose logs [svc...]`, `podman compose`, `docker-compose`, `podman-compose` | services of the compose file named by `-f`, else in the working directory; no service: the compose project |
| `journalctl -u U` (`--unit`, `--user-unit`, `-fu U`, globs) | units named `U` (`U.service` without a suffix, templates `a@b.service` → `a@.service`) and quadlets generating `U` |
| `systemctl status U...` | as `journalctl -u` |

```
[ozagia] ops:unit/deploy/systemd/backup.service: systemd service backup.service (deploy/systemd/backup.service:1); runs file:scripts/backup.py
```

These commands are never captured or answered; the built-in runs them and
only its output is annotated. Lines are capped at eight per command.

## Counters

`DaemonState.stats()` reports `ops_nodes` (ops nodes in the graph) and
`ops_after_annotations` (`[ozagia] ops:` lines added); the agent eval copies
both into run records (`graph_stats_fields`) and sums the second
(`total_ops_after_annotations`).

## Limits

- Compose `extends`, `include`, multiple `-f` merge order and profiles
  activation are not evaluated; variables in image names leave the image
  unresolved.
- Containerfile `ARG` substitution in `FROM` is not evaluated.
- Unit drop-ins (`*.d/*.conf`), specifiers (`%h`, `%i`) and generators other
  than the quadlet service names above are not evaluated.
- Runtime processes ozagia starts itself are in `ozagia.core.runtime`
  ([runtime.md](runtime.md)); the ops graph only describes declared sources.
