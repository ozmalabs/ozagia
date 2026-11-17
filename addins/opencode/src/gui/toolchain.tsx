import { For, Show, createMemo, createResource, createSignal } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type {
  Envelope,
  ToolchainCapabilityReport,
  ToolchainEnvironment,
  ToolchainPlan,
  ToolchainPlanInput,
  ToolchainRegistry,
} from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import {
  MAX_TOOLCHAIN_CAPABILITIES,
  TOOLCHAIN_MODEL_FAMILIES,
  capabilityState,
  capabilityTone,
  formatProvides,
  installKindLabel,
  installPreviewRows,
  splitToolchainCapabilities,
  splitToolchainPaths,
  sudoStateText,
  toolchainBrief,
  toolchainInputIssue,
} from "./toolchain-brief"
import { Badge, Card, Empty, FailureView, Loading, Metric } from "./ui"

const COPY_LIMIT = 8
const MISSING_LIMIT = 12

interface ToolchainSnapshot {
  environment: Envelope<ToolchainEnvironment>
  registry: Envelope<ToolchainRegistry>
}

function platformLabel(environment: ToolchainEnvironment): string {
  const platform = environment.platform
  const distro = platform?.distro
  const parts: string[] = []
  const system = [platform?.system, platform?.arch].filter(Boolean).join(" ")
  if (system) parts.push(system)
  const name = distro?.pretty_name ?? distro?.name ?? distro?.id
  if (name) parts.push(String(name))
  if (distro?.version_id && !String(name ?? "").includes(String(distro.version_id))) {
    parts.push(String(distro.version_id))
  }
  return parts.join(" · ") || "unknown platform"
}

function sudoTone(environment: ToolchainEnvironment): "ok" | "warn" | "muted" {
  const sudo = environment.sudo
  if (!sudo?.available || !sudo.probed) return "muted"
  return sudo.noninteractive ? "ok" : "warn"
}

function gateTone(allowed: boolean): "warn" | "muted" {
  // An approval that is on is a risk to surface; the safe default stays quiet.
  return allowed ? "warn" : "muted"
}

function EnvironmentCard(props: { environment: ToolchainEnvironment }) {
  const environment = () => props.environment
  const managers = () => Object.entries(environment().managers ?? {})
  const approvalRows = () => {
    const approval = environment().approval
    return [
      {
        key: "install",
        allowed: approval?.allow_install === true,
        source: approval?.sources?.allow_install ?? "default",
      },
      {
        key: "network",
        allowed: approval?.allow_network === true,
        source: approval?.sources?.allow_network ?? "default",
      },
      {
        key: "sudo",
        allowed: approval?.allow_sudo === true,
        source: approval?.sources?.allow_sudo ?? "default",
      },
    ]
  }
  const configErrors = () => environment().config?.errors ?? []

  return (
    <Card title="Environment">
      <div class="ozagia-metrics">
        <Metric
          label="managers"
          value={Object.keys(environment().managers ?? {}).length}
        />
        <Metric label="python" value={environment().python?.version ?? "—"} />
        <Metric
          label="venv"
          value={environment().python?.venv ? "yes" : "no"}
        />
        <Metric label="uv" value={environment().python?.uv ? "yes" : "no"} />
      </div>
      <div class="ozagia-chips">
        <Badge tone="muted">{platformLabel(environment())}</Badge>
        <Show when={environment().platform?.wsl}>
          <Badge tone="warn">WSL</Badge>
        </Show>
        <Show when={environment().platform?.container}>
          <Badge tone="warn">container</Badge>
        </Show>
        <Badge tone={sudoTone(environment())}>{sudoStateText(environment())}</Badge>
      </div>
      <dl class="ozagia-grid" style={{ "margin-top": "6px" }}>
        <dt>python</dt>
        <dd>{environment().python?.executable ?? "—"}</dd>
        <dt>managers</dt>
        <dd>
          <Show
            when={managers().length > 0}
            fallback={<span class="ozagia-note">none detected</span>}
          >
            <div class="ozagia-chips">
              <For each={managers()}>
                {([name, info]) => (
                  <span class="ozagia-chip" title={info.path ?? undefined}>
                    <Badge tone="muted">{name}</Badge>
                  </span>
                )}
              </For>
            </div>
          </Show>
        </dd>
        <dt>sudo</dt>
        <dd>
          {environment().sudo?.path ?? "missing"}
          <Show when={environment().sudo?.available && environment().sudo?.probed}>
            {environment().sudo?.noninteractive
              ? " (usable non-interactively)"
              : " (not usable non-interactively)"}
          </Show>
          <Show when={environment().sudo?.available && !environment().sudo?.probed}>
            {" (not probed)"}
          </Show>
        </dd>
        <dt>approval</dt>
        <dd>
          <div class="ozagia-chips">
            <For each={approvalRows()}>
              {(row) => (
                <Badge tone={gateTone(row.allowed)}>
                  {`${row.key}: ${row.allowed ? "approved" : "not approved"} (${row.source})`}
                </Badge>
              )}
            </For>
          </div>
        </dd>
        <dt>config</dt>
        <dd>
          {environment().config?.paths?.repo ?? "—"}
          <Show when={environment().config?.paths?.legacy}>
            {(path) => <div class="ozagia-note">{`legacy: ${path()}`}</div>}
          </Show>
        </dd>
      </dl>
      <Show when={configErrors().length > 0}>
        <div class="ozagia-sep" />
        <div class="ozagia-sub">{`[tools] configuration errors (${configErrors().length})`}</div>
        <ul class="ozagia-list">
          <For each={configErrors().slice(0, 5)}>
            {(issue) => (
              <li>
                <Badge tone="error">config</Badge>{" "}
                {`${issue.key ?? "tools"} (${issue.source ?? "unknown"}): ${issue.message ?? "invalid value"}`}
              </li>
            )}
          </For>
        </ul>
        <Show when={configErrors().length > 5}>
          <div class="ozagia-note">{`Showing 5 of ${configErrors().length} errors.`}</div>
        </Show>
      </Show>
      <div class="ozagia-note" style={{ "margin-top": "6px" }}>
        Approval is read from <code>[tools]</code> and the environment overrides. The panel
        never installs, never changes a gate, and makes no network call. Version and sudo
        probes are bounded CLI calls; <code>sudo -n true</code> is never run unless enabled.
      </div>
    </Card>
  )
}

function RegistryCard(props: { registry: ToolchainRegistry }) {
  const capabilities = () => Object.values(props.registry.capabilities ?? {})
  const groups = () => Object.values(props.registry.groups ?? {})
  const toolCount = () => Object.keys(props.registry.tools ?? {}).length

  return (
    <Card title="Registry">
      <div class="ozagia-metrics">
        <Metric label="version" value={props.registry.registry_version ?? "—"} />
        <Metric label="capabilities" value={capabilities().length} />
        <Metric label="tools" value={toolCount()} />
        <Metric label="groups" value={groups().length} />
      </div>
      <div class="ozagia-chips">
        <For each={groups()}>
          {(group) => (
            <Badge tone="muted">{`${group.id}: ${group.title ?? group.id}`}</Badge>
          )}
        </For>
      </div>
      <div class="ozagia-note" style={{ "margin-top": "6px" }}>
        Static registry data from <code>toolchain_cli list --json</code>. The registry is not
        probed; only <code>env</code> and <code>plan</code> inspect this host.
      </div>
    </Card>
  )
}

function CapabilityCard(props: { report: ToolchainCapabilityReport }) {
  const report = () => props.report
  const state = () => capabilityState(report())
  const available = () => (report().available ?? []).slice(0, MISSING_LIMIT)
  const missing = () => report().missing ?? []
  const remote = () => report().remote ?? []
  const unprobed = () => report().unprobed ?? []
  const install = () => report().install ?? []
  const previews = () => installPreviewRows(report(), COPY_LIMIT)

  return (
    <Card title={`${report().id} — ${report().title ?? report().id}`}>
      <div class="ozagia-chips">
        <Badge tone={capabilityTone(state())}>{state()}</Badge>
        <Show when={report().blocked}>
          <Badge tone="error">blocked</Badge>
        </Show>
        <Show when={report().installable}>
          <Badge tone="warn">installable</Badge>
        </Show>
        <Show when={report().group}>
          <Badge tone="muted">{`group: ${report().group}`}</Badge>
        </Show>
        <Show when={report().recommended_path}>
          <Badge tone="muted">{`path: ${report().recommended_path}`}</Badge>
        </Show>
        <Show when={report().recommended_tool}>
          <Badge tone="ok">{`recommended: ${report().recommended_tool}`}</Badge>
        </Show>
      </div>
      <Show when={report().description}>
        <p class="ozagia-note" style={{ margin: "6px 0" }}>
          {report().description}
        </p>
      </Show>
      <div class="ozagia-metrics">
        <Metric label="available" value={(report().available ?? []).length} />
        <Metric label="missing" value={missing().length} />
        <Metric label="remote" value={remote().length} />
        <Metric label="unprobed" value={unprobed().length} />
        <Metric label="install options" value={install().length} />
      </div>

      <Show when={(report().available ?? []).length > 0}>
        <div class="ozagia-sep" />
        <div class="ozagia-sub">Installed / available</div>
        <div class="ozagia-scroll" style={{ "margin-top": "4px" }}>
          <table class="ozagia-table">
            <thead>
              <tr>
                <th>tool</th>
                <th>kind</th>
                <th>version</th>
                <th>provides</th>
                <th>where</th>
              </tr>
            </thead>
            <tbody>
              <For each={available()}>
                {(tool) => (
                  <tr>
                    <td>
                      <div>{tool.id}</div>
                      <Show when={tool.recommended}>
                        <Badge tone="ok">recommended</Badge>
                      </Show>
                      <Show when={tool.usable === false}>
                        <Badge tone="error">{`needs ${(tool.missing_requirements ?? []).join(", ") || "a dependency"}`}</Badge>
                      </Show>
                    </td>
                    <td>
                      <Badge tone="muted">{tool.kind ?? "tool"}</Badge>
                      <div class="ozagia-note">{tool.format ?? "—"}</div>
                    </td>
                    <td>{tool.version ?? "—"}</td>
                    <td>{formatProvides(tool)}</td>
                    <td>
                      <span class="ozagia-truncate" title={tool.path ?? tool.module ?? undefined}>
                        {tool.path ?? tool.module ?? "—"}
                      </span>
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
        <Show when={(report().available ?? []).length > MISSING_LIMIT}>
          <div class="ozagia-note">{`Showing ${MISSING_LIMIT} of ${(report().available ?? []).length} available tools.`}</div>
        </Show>
      </Show>

      <Show when={missing().length > 0}>
        <div class="ozagia-sep" />
        <details class="ozagia-details">
          <summary>{`Missing (${missing().length})`}</summary>
          <ul class="ozagia-list">
            <For each={missing().slice(0, MISSING_LIMIT)}>
              {(tool) => (
                <li>
                  <b>{tool.id}</b>{" "}
                  <span class="ozagia-note">{formatProvides(tool, 3)}</span>
                </li>
              )}
            </For>
          </ul>
          <Show when={missing().length > MISSING_LIMIT}>
            <div class="ozagia-note">{`Showing ${MISSING_LIMIT} of ${missing().length} missing tools.`}</div>
          </Show>
        </details>
      </Show>

      <Show when={remote().length > 0}>
        <div class="ozagia-sep" />
        <div class="ozagia-sub">Remote providers (not connected)</div>
        <ul class="ozagia-list" style={{ "margin-top": "4px" }}>
          <For each={remote()}>
            {(tool) => (
              <li>
                <b>{tool.id}</b> <Badge tone="muted">remote</Badge>{" "}
                <span class="ozagia-note">
                  {tool.credentialed
                    ? "requires network and credentials; not checked"
                    : "requires network; not checked"}
                </span>
                <div class="ozagia-note">{formatProvides(tool, 3)}</div>
              </li>
            )}
          </For>
        </ul>
      </Show>

      <Show when={unprobed().length > 0}>
        <div class="ozagia-sep" />
        <div class="ozagia-sub">Local providers not probed</div>
        <ul class="ozagia-list" style={{ "margin-top": "4px" }}>
          <For each={unprobed()}>
            {(tool) => (
              <li>
                <b>{tool.id}</b> <Badge tone="muted">presence not checked</Badge>
                <div class="ozagia-note">{formatProvides(tool, 3)}</div>
              </li>
            )}
          </For>
        </ul>
      </Show>

      <Show when={install().length > 0}>
        <div class="ozagia-sep" />
        <div class="ozagia-sub">Install previews (would_run: false)</div>
        <For each={previews()}>
          {(item) => (
            <div class="ozagia-card" style={{ padding: "6px", "margin-top": "4px" }}>
              <div class="ozagia-row" style={{ "justify-content": "space-between" }}>
                <span>
                  <b>{item.tool ?? item.package ?? "option"}</b>{" "}
                  <Badge tone="muted">{installKindLabel(item)}</Badge>
                </span>
                <Badge tone={item.recommended ? "ok" : "muted"}>
                  {item.recommended ? "recommended" : "preview"}
                </Badge>
              </div>
              <pre class="ozagia-pre" style={{ "margin-top": "4px" }}>
                {item.command ?? "—"}
              </pre>
              <div class="ozagia-chips" style={{ "margin-top": "4px" }}>
                <Badge tone="muted">{`manager: ${item.manager ?? "—"}`}</Badge>
                <Show when={item.requires_sudo}>
                  <Badge tone="warn">needs sudo</Badge>
                </Show>
                <Show when={item.requires_network}>
                  <Badge tone="warn">needs network</Badge>
                </Show>
                <Badge tone="muted">would_run: false</Badge>
              </div>
              <Show when={(item.blocked_by ?? []).length > 0}>
                <div class="ozagia-note">
                  {`blocked by: ${(item.blocked_by ?? []).join(", ")}`}
                </div>
                <Show when={(item.blocked_reasons ?? []).length > 0}>
                  <ul class="ozagia-list">
                    <For each={item.blocked_reasons ?? []}>{(reason) => <li>{reason}</li>}</For>
                  </ul>
                </Show>
              </Show>
              <Show when={(item.blocked_by ?? []).length === 0}>
                <div class="ozagia-note">
                  No blocker is recorded, but this is still a preview. The panel cannot run
                  it.
                </div>
              </Show>
            </div>
          )}
        </For>
        <Show when={install().length > previews().length}>
          <div class="ozagia-note">{`Showing ${previews().length} of ${install().length} previews.`}</div>
        </Show>
        <div class="ozagia-note" style={{ "margin-top": "6px" }}>
          These commands are text from the plan. There is no install or apply control in this
          panel, and the CLI never runs them.
        </div>
      </Show>
    </Card>
  )
}

export function ToolchainTab() {
  const ctx = usePlugin<typeof definition>()
  const [capabilityText, setCapabilityText] = createSignal("")
  const [pathText, setPathText] = createSignal("")
  const [probeVersions, setProbeVersions] = createSignal(false)
  const [probeSudo, setProbeSudo] = createSignal(false)
  const [family, setFamily] = createSignal("any")
  const [busy, setBusy] = createSignal(false)
  const [result, setResult] = createSignal<Envelope<ToolchainPlan> | null>(null)
  const [copyState, setCopyState] = createSignal<"idle" | "copied" | "failed">("idle")

  const [snapshot, { refetch }] = createResource<ToolchainSnapshot>(async () => {
    const [environment, registry] = await Promise.all([
      invoke<ToolchainEnvironment>(ctx, () =>
        ctx.client.rpc(Ozagia).toolchainEnv({ probeSudo: probeSudo() }),
      ),
      invoke<ToolchainRegistry>(ctx, () => ctx.client.rpc(Ozagia).toolchainList({})),
    ])
    return { environment, registry }
  })

  const plan = (): ToolchainPlan | null => {
    const envelope = result()
    return envelope?.ok && envelope.data ? envelope.data : null
  }

  const failedPlan = (): Envelope | undefined => {
    const envelope = result()
    return envelope && !envelope.ok ? envelope : undefined
  }

  const environment = (): ToolchainEnvironment | null =>
    plan()?.environment ??
    (snapshot()?.environment.ok ? snapshot()?.environment.data ?? null : null)

  const environmentFailure = (): Envelope | undefined => {
    if (environment()) return undefined
    const envelope = snapshot()?.environment
    return envelope && !envelope.ok ? envelope : undefined
  }

  const registry = (): ToolchainRegistry | null => {
    const envelope = snapshot()?.registry
    return envelope?.ok && envelope.data ? envelope.data : null
  }

  const registryFailure = (): Envelope | undefined => {
    const envelope = snapshot()?.registry
    return envelope && !envelope.ok ? envelope : undefined
  }

  const capabilities = () => splitToolchainCapabilities(capabilityText())
  const paths = () => splitToolchainPaths(pathText())
  const issue = () => toolchainInputIssue(capabilities(), paths())
  const briefText = createMemo(() => toolchainBrief(plan(), family()))

  const refresh = () => {
    // Re-read host facts with the current probe switches. Clearing the plan
    // avoids showing a plan whose environment no longer matches the card.
    setResult(null)
    setCopyState("idle")
    void refetch()
  }

  const submit = async () => {
    if (busy() || issue()) return
    setBusy(true)
    setCopyState("idle")
    const input: ToolchainPlanInput = {
      probeVersions: probeVersions(),
      probeSudo: probeSudo(),
    }
    if (capabilities().length) input.capabilities = capabilities()
    if (paths().length) input.paths = paths()
    const envelope = await invoke<ToolchainPlan>(ctx, () =>
      ctx.client.rpc(Ozagia).toolchainPlan(input),
    )
    setResult(envelope)
    setBusy(false)
  }

  const copyBrief = async () => {
    const text = briefText()
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopyState("copied")
    } catch {
      setCopyState("failed")
    }
  }

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Tools</div>
          <div class="ozagia-sub">
            External-tool readiness and install previews; nothing is installed
          </div>
        </div>
        <button class="ozagia-button" type="button" onClick={refresh}>
          Refresh
        </button>
      </div>

      <div class="ozagia-card">
        <h3>Plan inputs</h3>
        <div class="ozagia-row">
          <label class="ozagia-field">
            <span>Capabilities</span>
            <input
              class="ozagia-input"
              value={capabilityText()}
              onInput={(event) => {
                setCapabilityText(event.currentTarget.value)
                setCopyState("idle")
              }}
              placeholder="pdf, documents, media"
            />
          </label>
        </div>
        <label class="ozagia-field" style={{ "margin-top": "6px" }}>
          <span>Paths (comma or newline separated)</span>
          <textarea
            class="ozagia-textarea"
            value={pathText()}
            onInput={(event) => {
              setPathText(event.currentTarget.value)
              setCopyState("idle")
            }}
            placeholder="report.pdf&#10;budget.xlsx"
          />
        </label>
        <div class="ozagia-chips" style={{ "margin-top": "6px" }}>
          <label class="ozagia-check">
            <input
              type="checkbox"
              checked={probeVersions()}
              onChange={(event) => setProbeVersions(event.currentTarget.checked)}
            />
            Probe versions (bounded per tool)
          </label>
          <label class="ozagia-check">
            <input
              type="checkbox"
              checked={probeSudo()}
              onChange={(event) => setProbeSudo(event.currentTarget.checked)}
            />
            Check sudo usability (runs <code>sudo -n true</code>)
          </label>
        </div>
        <div class="ozagia-row" style={{ "margin-top": "8px" }}>
          <button
            class="ozagia-button"
            type="button"
            disabled={busy() || !!issue()}
            onClick={() => void submit()}
          >
            {busy() ? "Planning…" : "Plan tools"}
          </button>
          <span class="ozagia-note">
            {`${capabilities().length}/${MAX_TOOLCHAIN_CAPABILITIES} capabilities, ` +
              `${paths().length}/32 paths`}
          </span>
        </div>
        <Show when={issue()}>
          {(message) => (
            <div class="ozagia-note" style={{ "margin-top": "4px", color: "var(--ozagia-error)" }}>
              {message()}
            </div>
          )}
        </Show>
        <div class="ozagia-note" style={{ "margin-top": "6px" }}>
          The plan reads installed tools and previews install commands. It never installs,
          runs a package manager, calls a model or uses the network. Every preview reports
          <code> would_run: false</code>.
        </div>
      </div>

      <Show when={snapshot.loading && !snapshot()}>
        <Loading label="Reading the toolchain environment…" />
      </Show>

      <Show when={environmentFailure()}>
        <FailureView envelope={environmentFailure()} onRetry={refresh} />
      </Show>

      <Show when={environment()}>
        {(current) => <EnvironmentCard environment={current()} />}
      </Show>

      <Show when={registryFailure()}>
        <FailureView envelope={registryFailure()} onRetry={refresh} />
      </Show>

      <Show when={registry()}>
        {(current) => <RegistryCard registry={current()} />}
      </Show>

      <Show when={busy()}>
        <Loading label="Probing the requested tools…" />
      </Show>

      <Show when={failedPlan()}>
        <FailureView envelope={failedPlan()} onRetry={() => void submit()} />
      </Show>

      <Show when={plan()}>
        {(current) => (
          <>
            <Card title="Plan">
              <div class="ozagia-metrics">
                <Metric
                  label="requested"
                  value={current().summary?.capabilities ?? (current().capabilities ?? []).length}
                />
                <Metric label="ready" value={current().summary?.ready ?? 0} />
                <Metric label="missing" value={current().summary?.missing ?? 0} />
                <Metric label="blocked" value={current().summary?.blocked ?? 0} />
                <Metric label="installable" value={current().summary?.installable ?? 0} />
              </div>
              <dl class="ozagia-grid">
                <dt>capabilities</dt>
                <dd>{(current().request?.capabilities ?? []).join(", ") || "—"}</dd>
                <dt>groups</dt>
                <dd>{(current().request?.groups ?? []).join(", ") || "—"}</dd>
                <dt>inferred</dt>
                <dd>{(current().request?.inferred ?? []).join(", ") || "—"}</dd>
                <dt>paths</dt>
                <dd>{(current().request?.paths ?? []).length}</dd>
                <dt>registry</dt>
                <dd>{current().registry_version ?? "—"}</dd>
              </dl>
              <Show when={(current().request?.paths ?? []).length > 0}>
                <details class="ozagia-details">
                  <summary>{`Paths (${(current().request?.paths ?? []).length})`}</summary>
                  <ul class="ozagia-list">
                    <For each={(current().request?.paths ?? []).slice(0, 12)}>
                      {(path) => <li>{path}</li>}
                    </For>
                  </ul>
                  <Show when={(current().request?.paths ?? []).length > 12}>
                    <div class="ozagia-note">
                      {`Showing 12 of ${(current().request?.paths ?? []).length} paths.`}
                    </div>
                  </Show>
                </details>
              </Show>
              <Show when={(current().request?.unmatched_paths ?? []).length > 0}>
                <div class="ozagia-sep" />
                <div class="ozagia-sub">Paths whose extension matched nothing</div>
                <ul class="ozagia-list">
                  <For each={current().request?.unmatched_paths ?? []}>
                    {(item) => (
                      <li>{`${item.path ?? "—"} (suffix ${item.suffix || "none"})`}</li>
                    )}
                  </For>
                </ul>
                <div class="ozagia-note">
                  Unknown extensions are reported, never guessed.
                </div>
              </Show>
            </Card>

            <Show
              when={(current().capabilities ?? []).length > 0}
              fallback={
                <Empty>
                  <div>No capability was requested.</div>
                  <div class="ozagia-note" style={{ "margin-top": "4px" }}>
                    Name a capability such as <code>pdf</code> or add a path; the plan never
                    guesses an unknown extension.
                  </div>
                </Empty>
              }
            >
              <For each={current().capabilities ?? []}>
                {(report) => <CapabilityCard report={report} />}
              </For>
            </Show>

            <Card title="What the plan did not assess">
              <Show
                when={(current().not_assessed ?? []).length > 0}
                fallback={<div class="ozagia-note">The plan listed no omissions.</div>}
              >
                <ul class="ozagia-list">
                  <For each={current().not_assessed ?? []}>{(item) => <li>{item}</li>}</For>
                </ul>
              </Show>
            </Card>

            <Card title="Model brief">
              <div class="ozagia-row">
                <label class="ozagia-field">
                  <span>Model family hint</span>
                  <select
                    class="ozagia-select"
                    value={family()}
                    onChange={(event) => {
                      setFamily(event.currentTarget.value)
                      setCopyState("idle")
                    }}
                  >
                    <For each={TOOLCHAIN_MODEL_FAMILIES}>
                      {(item) => <option value={item.id}>{item.label}</option>}
                    </For>
                  </select>
                </label>
                <button
                  class="ozagia-button"
                  type="button"
                  disabled={!briefText()}
                  onClick={() => void copyBrief()}
                >
                  {copyState() === "copied" ? "Copied" : "Copy brief"}
                </button>
              </div>
              <pre class="ozagia-pre" style={{ "margin-top": "6px" }}>
                {briefText()}
              </pre>
              <Show when={copyState() === "failed"}>
                <div class="ozagia-note" style={{ color: "var(--ozagia-error)" }}>
                  The clipboard is unavailable; select the text above and copy it manually.
                </div>
              </Show>
              <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                Rendered locally from the plan's model-family metadata. There is no model
                call, and the CLI has no <code>--model-family</code> flag. Nothing was
                installed.
              </div>
            </Card>
          </>
        )}
      </Show>

      <div class="ozagia-note">
        Read-only: only <code>toolchain_cli list|env|plan</code> are called. No install,
        apply, setting write, model call or network fetch exists in this tab.
      </div>
    </div>
  )
}
