import { For, Show, createResource } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type {
  ConfigReport,
  DaemonReport,
  DoctorReport,
  FilesReport,
  Finding,
  ResourcesReport,
  RuntimeReport,
  StatusReport,
  WorktreeInfo,
  WorkspaceReport,
} from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, Card, Empty, FailureView, Loading, Meter, Metric } from "./ui"

function shortPath(value?: string | null): string {
  if (!value) return "—"
  const parts = value.split(/[\\/]/).filter(Boolean)
  return parts.slice(-2).join("/") || value
}

function shortHead(value?: string | null): string {
  if (!value) return "—"
  return value.length > 10 ? value.slice(0, 10) : value
}

function dirtyCount(worktree: WorktreeInfo): number {
  return Object.values(worktree.dirty ?? {}).reduce((total, value) => total + (value || 0), 0)
}

function toneForState(state?: string): "ok" | "warn" | "error" | "muted" {
  if (state === "ok") return "ok"
  if (state === "absent" || state === "defaults") return "muted"
  if (state === "unknown" || state === "degraded") return "warn"
  return "error"
}

function memoryPct(memory: { total_mb?: number | null; available_mb?: number | null; used_pct?: number | null } | undefined): number | null {
  if (!memory) return null
  if (typeof memory.used_pct === "number") return memory.used_pct
  if (typeof memory.total_mb === "number" && typeof memory.available_mb === "number" && memory.total_mb > 0) {
    return ((memory.total_mb - memory.available_mb) / memory.total_mb) * 100
  }
  return null
}

function findText(finding: Finding): string {
  return finding.message || finding.id || "finding"
}

export function StatusTab() {
  const ctx = usePlugin<typeof definition>()
  const [snapshot, { refetch }] = createResource(() =>
    invoke<StatusReport>(ctx, () => ctx.client.rpc(Ozagia).status({})),
  )

  const data = () => {
    const envelope = snapshot()
    if (!envelope?.ok || !envelope.data) return null
    return envelope.data
  }
  const workspace = (): WorkspaceReport | null => data()?.workspace?.report ?? null
  const resources = (): ResourcesReport | null => data()?.resources?.report ?? null
  const files = (): FilesReport | null => data()?.files?.report ?? null
  const runtime = (): RuntimeReport | null => data()?.runtime?.report ?? null
  const daemon = (): DaemonReport | null => data()?.daemon?.report ?? null
  const doctor = (): DoctorReport | null => data()?.doctor?.report ?? null
  const config = (): ConfigReport | null => data()?.config?.report ?? null

  const findings = (): Finding[] => {
    const worktrees = workspace()?.worktrees ?? []
    return [
      ...(workspace()?.findings ?? []),
      ...worktrees.flatMap((worktree) =>
        (worktree.findings ?? []).map((finding) => ({
          ...finding,
          worktree: finding.worktree ?? worktree.path,
        })),
      ),
    ]
  }

  const messages = () => {
    const list = [...(data()?.errors ?? []), ...(data()?.degraded ?? [])]
    const seen = new Set<string>()
    return list.filter((item) => {
      const key = `${item.component}:${item.code}:${item.message}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }

  const refresh = () => void refetch()

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Status</div>
          <div class="ozagia-sub">Read-only workspace, resources and runtime snapshot</div>
        </div>
        <button class="ozagia-button" type="button" onClick={refresh}>
          Refresh
        </button>
      </div>

      <Show when={snapshot.loading && !data()}>
        <Loading label="Reading the workspace…" />
      </Show>
      <Show when={!snapshot.loading && snapshot() && !snapshot()!.ok}>
        <FailureView envelope={snapshot()} onRetry={refresh} />
      </Show>
      <Show when={snapshot.error}>
        <FailureView error={snapshot.error} onRetry={refresh} />
      </Show>

      <Show when={data()}>
        {(report) => (
          <>
            <div class="ozagia-card">
              <h3>
                <span>Snapshot</span>
                <Badge tone={report().ok ? "ok" : "error"}>{report().ok ? "ok" : "hard error"}</Badge>
              </h3>
              <div class="ozagia-metrics">
                <Metric label="root" value={shortPath(report().root)} />
                <Metric label="schema" value={report().schema ?? "—"} />
                <Metric label="checked" value={report().checked_at ?? "—"} />
              </div>
              <Show when={messages().length > 0} fallback={<div class="ozagia-note">No hard errors or degraded components.</div>}>
                <ul class="ozagia-list">
                  <For each={messages()}>
                    {(item) => (
                      <li>
                        <Badge tone={item.component && (report().errors ?? []).some((error) => error.code === item.code) ? "error" : "warn"}>
                          {item.code}
                        </Badge>{" "}
                        {item.message}
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </div>

            <Card title="Workspace" state={report().workspace?.state} error={report().workspace?.error}>
              <Show when={workspace()} fallback={<Empty>Workspace checks are unavailable.</Empty>}>
                {(current) => (
                  <>
                    <div class="ozagia-metrics">
                      <Metric label="worktrees" value={(current().worktrees ?? []).length} />
                      <Metric label="findings" value={findings().length} />
                      <Metric label="base" value={current().base ?? "—"} />
                    </div>
                    <div class="ozagia-scroll">
                      <table class="ozagia-table">
                        <thead>
                          <tr>
                            <th>worktree</th>
                            <th>branch</th>
                            <th>drift</th>
                            <th>dirty</th>
                            <th>disk</th>
                          </tr>
                        </thead>
                        <tbody>
                          <For each={current().worktrees ?? []}>
                            {(worktree) => (
                              <tr>
                                <td>
                                  <span class="ozagia-truncate" title={worktree.path}>
                                    {shortPath(worktree.path)}
                                  </span>
                                </td>
                                <td>
                                  <div>{worktree.detached ? "detached" : worktree.branch ?? "unknown"}</div>
                                  <Show when={worktree.expected?.branch && worktree.expected.branch !== worktree.branch}>
                                    <Badge tone="warn">{`expected ${worktree.expected?.branch}`}</Badge>
                                  </Show>
                                </td>
                                <td>{`↑${worktree.ahead ?? 0} ↓${worktree.behind ?? 0}`}</td>
                                <td>{dirtyCount(worktree)}</td>
                                <td>
                                  <div>{worktree.filesystem?.free_gb != null ? `${worktree.filesystem.free_gb} GB free` : "unknown"}</div>
                                  <Show when={worktree.filesystem?.is_tmpfs}>
                                    <Badge tone="warn">tmpfs</Badge>
                                  </Show>
                                  <div class="ozagia-note">{worktree.filesystem?.fstype ?? ""}</div>
                                </td>
                              </tr>
                            )}
                          </For>
                        </tbody>
                      </table>
                    </div>
                    <Show when={findings().length > 0}>
                      <ul class="ozagia-list">
                        <For each={findings().slice(0, 40)}>
                          {(finding) => (
                            <li>
                              <Badge tone={finding.level === "error" ? "error" : finding.level === "warn" ? "warn" : "muted"}>
                                {finding.level}
                              </Badge>{" "}
                              {findText(finding)}
                              <Show when={finding.worktree}>
                                <span class="ozagia-note">{` · ${shortPath(finding.worktree)}`}</span>
                              </Show>
                            </li>
                          )}
                        </For>
                      </ul>
                      <Show when={findings().length > 40}>
                        <div class="ozagia-note">{`Showing 40 of ${findings().length} findings.`}</div>
                      </Show>
                    </Show>
                  </>
                )}
              </Show>
            </Card>

            <Card title="Resources" state={report().resources?.state} error={report().resources?.error}>
              <Show when={resources()} fallback={<Empty>Host resources are unavailable.</Empty>}>
                {(current) => {
                  const memory = current().host?.memory
                  const disk = (current().disks ?? [])[0]
                  const pct = memoryPct(memory)
                  return (
                    <>
                      <div class="ozagia-metrics">
                        <Metric label="memory used" value={pct != null ? `${pct.toFixed(1)}%` : "unknown"} />
                        <Metric label="available" value={memory?.available_mb != null ? `${memory.available_mb} MB` : "unknown"} />
                        <Metric label="load 1m" value={current().host?.load?.["1m"]?.toFixed(2) ?? "unknown"} />
                        <Metric label="cpus" value={current().host?.cpu_count ?? "unknown"} />
                      </div>
                      <Meter pct={pct} label={`${memory?.total_mb ?? "?"} MB total · swap ${memory?.swap_free_mb ?? "?"}/${memory?.swap_total_mb ?? "?"} MB free`} />
                      <Show when={disk}>
                        <div class="ozagia-sep" />
                        <dl class="ozagia-grid">
                          <dt>disk</dt>
                          <dd>{disk?.mount ?? disk?.path ?? "—"} · {disk?.fstype ?? "unknown"}{disk?.is_tmpfs ? " · tmpfs" : ""}</dd>
                          <dt>free</dt>
                          <dd>{disk?.free_gb != null ? `${disk.free_gb} of ${disk.total_gb ?? "?"} GB (${disk.free_pct ?? "?"}%)` : "unknown"}</dd>
                          <dt>inodes</dt>
                          <dd>{disk?.inodes_free ?? "unknown"}</dd>
                          <dt>pressure</dt>
                          <dd>{current().host?.pressure ? "recorded" : "unknown"}</dd>
                          <dt>policy</dt>
                          <dd>
                            {current().policy
                              ? `reserve memory ${current().policy!.reserve_memory_pct}% · disk ${current().policy!.reserve_disk_pct}% · admission memory ${current().policy!.admission?.min_free_memory_pct ?? "?"}%`
                              : "—"}
                          </dd>
                        </dl>
                      </Show>
                      <Show when={(current().top_groups ?? []).length > 0}>
                        <details class="ozagia-details">
                          <summary>{`Top process groups (${current().top_groups!.length})`}</summary>
                          <table class="ozagia-table">
                            <thead>
                              <tr>
                                <th>command</th>
                                <th>RSS MB</th>
                                <th>procs</th>
                              </tr>
                            </thead>
                            <tbody>
                              <For each={current().top_groups!.slice(0, 8)}>
                                {(group) => (
                                  <tr>
                                    <td>{group.command ?? "unknown"}</td>
                                    <td>{group.memory_mb ?? "—"}</td>
                                    <td>{group.process_count ?? "—"}</td>
                                  </tr>
                                )}
                              </For>
                            </tbody>
                          </table>
                        </details>
                      </Show>
                    </>
                  )
                }}
              </Show>
            </Card>

            <Card title="Files" state={report().files?.state} error={report().files?.error}>
              <Show when={files()} fallback={<Empty>The file ledger is unavailable.</Empty>}>
                {(current) => (
                  <>
                    <div class="ozagia-metrics">
                      <Metric label="records" value={current().counts?.records ?? 0} />
                      <Metric label="orphans" value={current().counts?.orphans ?? 0} />
                      <Metric label="new" value={current().counts?.new ?? 0} />
                    </div>
                    <dl class="ozagia-grid">
                      <dt>ledger</dt>
                      <dd>{current().ledger?.present ? "present" : "absent"}</dd>
                      <dt>path</dt>
                      <dd>{current().ledger?.path ?? "—"}</dd>
                    </dl>
                  </>
                )}
              </Show>
            </Card>

            <Card title="Daemon" state={report().daemon?.state} error={report().daemon?.error}>
              <Show when={daemon()} fallback={<Empty>No daemon state was returned.</Empty>}>
                {(current) => (
                  <>
                    <div class="ozagia-metrics">
                      <Metric label="state" value={report().daemon?.state ?? "unknown"} />
                      <Metric label="ping" value={current().ping ? "answered" : "none"} />
                      <Metric label="stats" value={current().stats ? "available" : "none"} />
                    </div>
                    <dl class="ozagia-grid">
                      <dt>socket</dt>
                      <dd>{current().socket ?? "—"}</dd>
                      <dt>timeout</dt>
                      <dd>{current().timeout_s != null ? `${current().timeout_s}s` : "—"}</dd>
                    </dl>
                    <Show when={current().stats}>
                      <details class="ozagia-details">
                        <summary>Daemon stats</summary>
                        <pre class="ozagia-pre">{JSON.stringify(current().stats, null, 2).slice(0, 5000)}</pre>
                      </details>
                    </Show>
                  </>
                )}
              </Show>
            </Card>

            <Card title="Graph snapshot" state={report().doctor?.state} error={report().doctor?.error}>
              <Show when={doctor()?.snapshot} fallback={<Empty>No snapshot state was returned.</Empty>}>
                {(current) => (
                  <>
                    <div class="ozagia-metrics">
                      <Metric label="state" value={current().state ?? "unknown"} />
                    </div>
                    <dl class="ozagia-grid">
                      <dt>path</dt>
                      <dd>{current().path ?? "—"}</dd>
                    </dl>
                    <Show when={current().state === "missing"}>
                      <div class="ozagia-note">
                        No graph snapshot. Build one before opening the graph panel.
                      </div>
                    </Show>
                    <Show when={(data()?.degraded ?? []).some((item) => item.code.startsWith("snapshot."))}>
                      <ul class="ozagia-list">
                        <For each={(data()?.degraded ?? []).filter((item) => item.code.startsWith("snapshot."))}>
                          {(item) => <li>{item.message}</li>}
                        </For>
                      </ul>
                    </Show>
                  </>
                )}
              </Show>
            </Card>

            <Card title="Runtime" state={report().runtime?.state} error={report().runtime?.error}>
              <Show when={runtime()} fallback={<Empty>Runtime state is unavailable.</Empty>}>
                {(current) => (
                  <div class="ozagia-metrics">
                    <Metric label="items" value={(current().items ?? []).length} />
                    <Metric label="services" value={(current().services ?? []).length} />
                    <Metric label="tunnels" value={(current().tunnels ?? []).length} />
                    <Metric label="live" value={current().live ? "yes" : "no"} />
                  </div>
                )}
              </Show>
            </Card>

            <Card title="Configuration" state={report().config?.state} error={report().config?.error}>
              <Show when={config()} fallback={<Empty>Configuration state is unavailable.</Empty>}>
                {(current) => (
                  <dl class="ozagia-grid">
                    <dt>path</dt>
                    <dd>{current().path ?? "defaults"}</dd>
                    <dt>source</dt>
                    <dd>{current().source ?? "built-in defaults"}</dd>
                    <dt>adapters</dt>
                    <dd>{(current().adapters ?? []).join(", ") || "none"}</dd>
                  </dl>
                )}
              </Show>
              <Show when={!data()?.ok}>
                <div class="ozagia-note">The snapshot reported a hard error; some sections may be missing.</div>
              </Show>
            </Card>
          </>
        )}
      </Show>

      <Show when={!snapshot.loading && snapshot()?.ok && !data()}>
        <Empty>The status command returned no report.</Empty>
      </Show>
    </div>
  )
}
