import { For, Show, createEffect, createMemo, createResource, createSignal } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { ACTION_NAMES, Ozagia } from "../rpc"
import type { ActionPreview, ActionSummary, Envelope } from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { canApplyAction, redactedApplyCommand } from "./mutation"
import { Badge, Card, Empty, FailureView, Loading, Metric } from "./ui"

const SUPPORTED_ACTIONS = new Set<string>(ACTION_NAMES)

const NAME_ACTIONS = new Set([
  "service.status",
  "service.start",
  "service.run",
  "service.stop",
  "service.restart",
  "service.logs",
  "tunnel.status",
  "tunnel.start",
])
const REQUIRED_NAME_ACTIONS = new Set([
  "service.status",
  "service.start",
  "service.run",
  "service.stop",
  "service.restart",
  "service.logs",
  "tunnel.status",
  "tunnel.start",
])
const INTEGRATIONS = ["", "opencode", "claude", "codex", "kiro"] as const

function numeric(value: string): number | undefined {
  const text = value.trim()
  if (!text) return undefined
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : undefined
}

function flags(action: ActionSummary): string[] {
  const out: string[] = []
  if (action.mutating) out.push("mutating")
  if (action.destructive) out.push("destructive")
  if (action.live_gated) out.push("live-gated")
  if (!out.length) out.push("read-only")
  return out
}

export function ActionsTab() {
  const ctx = usePlugin<typeof definition>()
  const [action, setAction] = createSignal("daemon.status")
  const [name, setName] = createSignal("")
  const [live, setLive] = createSignal(false)
  const [network, setNetwork] = createSignal(false)
  const [maxBytes, setMaxBytes] = createSignal("")
  const [scope, setScope] = createSignal("project")
  const [wait, setWait] = createSignal("")
  const [idleTimeout, setIdleTimeout] = createSignal("")
  const [result, setResult] = createSignal<Envelope<ActionPreview> | null>(null)
  const [busy, setBusy] = createSignal(false)
  const [previewInput, setPreviewInput] = createSignal<Record<string, unknown> | null>(null)
  const [applyConfirmed, setApplyConfirmed] = createSignal(false)
  const [applyBusy, setApplyBusy] = createSignal(false)
  const [applyResult, setApplyResult] = createSignal<Envelope<ActionPreview> | null>(null)

  const [actions, { refetch }] = createResource<Envelope<ActionSummary[]>>(() =>
    invoke<ActionSummary[]>(ctx, () => ctx.client.rpc(Ozagia).actionsList({})),
  )

  const actionData = () => {
    const envelope = actions()
    if (!envelope?.ok || !Array.isArray(envelope.data)) return null
    // `actions_cli list` also publishes installers this add-in does not expose.
    return envelope.data.filter((item) => SUPPORTED_ACTIONS.has(item.name))
  }
  const selected = createMemo(
    () => actionData()?.find((item) => item.name === action()) ?? null,
  )
  const previewData = () => {
    const envelope = result()
    if (!envelope?.ok || !envelope.data) return null
    return envelope.data
  }

  const hasName = () => NAME_ACTIONS.has(action()) || action() === "integration.status"
  const nameRequired = () => REQUIRED_NAME_ACTIONS.has(action())
  const canPreview = () =>
    !busy() && (!nameRequired() || !!name().trim())

  createEffect(() => {
    const list = actionData()
    if (list?.length && !list.some((item) => item.name === action())) {
      chooseAction(list[0].name)
    }
  })

  function chooseAction(next: string) {
    setAction(next)
    setName("")
    setLive(false)
    setNetwork(false)
    setMaxBytes("")
    setScope("project")
    setWait("")
    setIdleTimeout("")
    setResult(null)
    setPreviewInput(null)
    setApplyResult(null)
    setApplyConfirmed(false)
  }

  const resetApply = () => {
    setApplyResult(null)
    setApplyConfirmed(false)
  }

  const submit = async () => {
    if (!canPreview()) return
    const input: Record<string, unknown> = { action: action() }
    if (hasName() && name().trim()) input.name = name().trim()
    if (action() === "tunnel.start") {
      input.live = live()
      input.network = network()
    }
    if (action() === "service.logs") {
      const bytes = numeric(maxBytes())
      if (bytes !== undefined) input.maxBytes = bytes
    }
    if (action() === "integration.status") input.scope = scope()
    if (action() === "daemon.ensure" || action() === "daemon.restart") {
      const waitValue = numeric(wait())
      const idleValue = numeric(idleTimeout())
      if (waitValue !== undefined) input.wait = waitValue
      if (idleValue !== undefined) input.idleTimeout = idleValue
    }

    resetApply()
    setBusy(true)
    const envelope = await invoke<ActionPreview>(ctx, () =>
      ctx.client.rpc(Ozagia).actionPreview(input),
    )
    setResult(envelope)
    setPreviewInput(envelope.ok && envelope.data?.confirmation ? input : null)
    setBusy(false)
  }

  const applyReady = () => canApplyAction(previewData() ?? null, applyConfirmed(), applyBusy())

  const apply = async () => {
    const preview = previewData()
    const input = previewInput()
    const token = preview?.confirmation
    if (!preview || !input || !token || !applyReady()) return
    setApplyBusy(true)
    const envelope = await invoke<ActionPreview>(ctx, () =>
      ctx.client.rpc(Ozagia).actionApply({ ...input, confirm: token }),
    )
    setApplyResult(envelope)
    setApplyBusy(false)
    setApplyConfirmed(false)
  }

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Actions</div>
          <div class="ozagia-sub">Preview first; applying needs a second, explicit confirmation</div>
        </div>
        <button class="ozagia-button" type="button" onClick={() => void refetch()}>
          Refresh
        </button>
      </div>

      <Show when={actions.loading && !actionData()}>
        <Loading label="Reading the action catalogue…" />
      </Show>
      <Show when={actions() && !actions()!.ok}>
        <FailureView envelope={actions()!} onRetry={() => void refetch()} />
      </Show>
      <Show when={actions.error}>
        <FailureView error={actions.error} onRetry={() => void refetch()} />
      </Show>

      <Show when={actionData()}>
        {(list) => (
          <>
            <Card title="Catalogue">
              <div class="ozagia-scroll">
                <table class="ozagia-table">
                  <thead>
                    <tr>
                      <th>action</th>
                      <th>kind</th>
                      <th>effect</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={list()}>
                      {(item) => (
                        <tr
                          tabIndex={0}
                          style={{
                            cursor: "pointer",
                            background:
                              item.name === action() ? "var(--ozagia-soft)" : undefined,
                          }}
                          onClick={() => chooseAction(item.name)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault()
                              chooseAction(item.name)
                            }
                          }}
                        >
                          <td>
                            <code>{item.name}</code>
                          </td>
                          <td>
                            <div class="ozagia-chips">
                              <For each={flags(item)}>
                                {(flag) => (
                                  <Badge
                                    tone={
                                      flag === "destructive"
                                        ? "error"
                                        : flag === "read-only"
                                          ? "ok"
                                          : "warn"
                                    }
                                  >
                                    {flag}
                                  </Badge>
                                )}
                              </For>
                            </div>
                          </td>
                          <td>{item.description ?? "—"}</td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </div>
            </Card>

            <Card title="Preview arguments">
              <label class="ozagia-field">
                <span>Action</span>
                <select
                  class="ozagia-select"
                  value={action()}
                  onChange={(event) => chooseAction(event.currentTarget.value)}
                >
                  <For each={list()}>
                    {(item) => <option value={item.name}>{item.name}</option>}
                  </For>
                </select>
              </label>

              <Show when={hasName()}>
                <Show
                  when={action() === "integration.status"}
                  fallback={
                    <label class="ozagia-field">
                      <span>{nameRequired() ? "Name (required)" : "Name"}</span>
                      <input
                        class="ozagia-input"
                        value={name()}
                        onInput={(event) => setName(event.currentTarget.value)}
                        placeholder="service or tunnel name"
                        spellcheck={false}
                      />
                    </label>
                  }
                >
                  <label class="ozagia-field">
                    <span>Integration</span>
                    <select
                      class="ozagia-select"
                      value={name()}
                      onChange={(event) => setName(event.currentTarget.value)}
                    >
                      <For each={INTEGRATIONS}>
                        {(item) => <option value={item}>{item || "all integrations"}</option>}
                      </For>
                    </select>
                  </label>
                </Show>
              </Show>

              <Show when={action() === "service.logs"}>
                <label class="ozagia-field">
                  <span>Max bytes (0–65536)</span>
                  <input
                    class="ozagia-input"
                    type="number"
                    min="0"
                    max="65536"
                    value={maxBytes()}
                    onInput={(event) => setMaxBytes(event.currentTarget.value)}
                    placeholder="4096"
                  />
                </label>
              </Show>

              <Show when={action() === "integration.status"}>
                <label class="ozagia-field">
                  <span>Scope</span>
                  <select
                    class="ozagia-select"
                    value={scope()}
                    onChange={(event) => setScope(event.currentTarget.value)}
                  >
                    <option value="project">project</option>
                    <option value="user">user</option>
                  </select>
                </label>
              </Show>

              <Show when={action() === "daemon.ensure" || action() === "daemon.restart"}>
                <div class="ozagia-row">
                  <label class="ozagia-field">
                    <span>Wait seconds (0–600)</span>
                    <input
                      class="ozagia-input"
                      type="number"
                      min="0"
                      max="600"
                      value={wait()}
                      onInput={(event) => setWait(event.currentTarget.value)}
                      placeholder="10"
                    />
                  </label>
                  <label class="ozagia-field">
                    <span>Idle timeout seconds (0–86400)</span>
                    <input
                      class="ozagia-input"
                      type="number"
                      min="0"
                      max="86400"
                      value={idleTimeout()}
                      onInput={(event) => setIdleTimeout(event.currentTarget.value)}
                      placeholder="1800"
                    />
                  </label>
                </div>
              </Show>

              <Show when={action() === "tunnel.start"}>
                <div class="ozagia-row" style={{ "margin-top": "6px" }}>
                  <label class="ozagia-check">
                    <input
                      type="checkbox"
                      checked={live()}
                      onChange={(event) => setLive(event.currentTarget.checked)}
                    />
                    allow live
                  </label>
                  <label class="ozagia-check">
                    <input
                      type="checkbox"
                      checked={network()}
                      onChange={(event) => setNetwork(event.currentTarget.checked)}
                    />
                    allow network
                  </label>
                </div>
                <div class="ozagia-note" style={{ "margin-top": "4px" }}>
                  Preview plans the relay command. It does not bind a socket or open a connection.
                </div>
              </Show>

              <div class="ozagia-row" style={{ "margin-top": "8px" }}>
                <button
                  class="ozagia-button"
                  type="button"
                  disabled={!canPreview()}
                  onClick={() => void submit()}
                >
                  {busy() ? "Previewing…" : "Preview action"}
                </button>
                <span class="ozagia-note">
                  {nameRequired() && !name().trim()
                    ? `${action()} needs a name before it can be previewed.`
                    : "Runs the read-only preview and shows its token. Preview alone never applies."}
                </span>
              </div>

              <Show when={selected()}>
                {(item) => (
                  <div class="ozagia-chips" style={{ "margin-top": "6px" }}>
                    <For each={flags(item())}>
                      {(flag) => (
                        <Badge
                          tone={
                            flag === "destructive"
                              ? "error"
                              : flag === "read-only"
                                ? "ok"
                                : "warn"
                          }
                        >
                          {flag}
                        </Badge>
                      )}
                    </For>
                  </div>
                )}
              </Show>
            </Card>
          </>
        )}
      </Show>

      <Show when={busy()}>
        <Loading label="Building the action preview…" />
      </Show>

      <Show when={result() && !result()!.ok}>
        <FailureView envelope={result()!} onRetry={() => void submit()} />
      </Show>

      <Show when={previewData()}>
        {(preview) => (
          <>
            <Card
              title="Preview"
              state={preview().state}
              error={preview().error ?? undefined}
            >
              <div class="ozagia-metrics">
                <Metric label="action" value={preview().action ?? action()} />
                <Metric label="mode" value={preview().mode ?? "—"} />
                <Metric label="applicable" value={preview().applicable ? "yes" : "no"} />
                <Metric label="live" value={preview().live ? "yes" : "no"} />
                <Metric label="gated" value={preview().gated ? "yes" : "no"} />
              </div>
              <dl class="ozagia-grid">
                <dt>operation</dt>
                <dd>{preview().operation ?? "—"}</dd>
                <dt>expected</dt>
                <dd>{preview().expected_effect ?? "—"}</dd>
                <dt>current state</dt>
                <dd>{preview().current_state || "—"}</dd>
                <dt>manager call</dt>
                <dd>{preview().manager_call ?? "—"}</dd>
              </dl>
              <Show when={(preview().argv ?? []).length > 0}>
                <div class="ozagia-sep" />
                <div class="ozagia-sub">Exact argv (JSON array, no shell)</div>
                <pre class="ozagia-pre">{JSON.stringify(preview().argv, null, 2)}</pre>
              </Show>
              <Show when={preview().current !== null && preview().current !== undefined}>
                <details class="ozagia-details">
                  <summary>Observed current state</summary>
                  <pre class="ozagia-pre">{JSON.stringify(preview().current, null, 2)}</pre>
                </details>
              </Show>
            </Card>

            <Show when={(preview().preconditions ?? []).length > 0}>
              <Card title="Preconditions">
                <ul class="ozagia-list">
                  <For each={preview().preconditions ?? []}>{(item) => <li>{item}</li>}</For>
                </ul>
              </Card>
            </Show>

            <Card title="Risks and notes">
              <Show
                when={(preview().risks ?? []).length > 0}
                fallback={<div class="ozagia-note">The preview listed no risks.</div>}
              >
                <ul class="ozagia-list">
                  <For each={preview().risks ?? []}>
                    {(item) => (
                      <li>
                        <Badge tone="warn">risk</Badge> {item}
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
              <Show when={(preview().notes ?? []).length > 0}>
                <div class="ozagia-sep" />
                <ul class="ozagia-list">
                  <For each={preview().notes ?? []}>{(item) => <li>{item}</li>}</For>
                </ul>
              </Show>
              <Show when={preview().error}>
                <div class="ozagia-sep" />
                <div class="ozagia-note">{`Preview error: ${preview().error}`}</div>
              </Show>
            </Card>

            <Card title="Confirmation token">
              <Show
                when={preview().confirmation}
                fallback={<div class="ozagia-note">The preview returned no token.</div>}
              >
                <code class="ozagia-token">{preview().confirmation}</code>
              </Show>
              <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                The token is bound to this action, these arguments and this repository, and is
                consumed once. Applying passes it through argv only: command text, logs and error
                tails show <code>--confirm &lt;redacted&gt;</code>.
              </div>
            </Card>

            <Card
              title="Apply action"
              state={applyResult()?.data?.state}
              error={applyResult()?.data?.error ?? null}
            >
              <Show
                when={preview().confirmation}
                fallback={
                  <Empty>
                    Apply stays unavailable until the preview returns a one-time token.
                  </Empty>
                }
              >
                <div class="ozagia-metrics">
                  <Metric label="action" value={preview().action ?? action()} />
                  <Metric label="applicable" value={preview().applicable ? "yes" : "no"} />
                  <Metric
                    label="kind"
                    value={[
                      selected()?.mutating ? "mutating" : "read-only",
                      selected()?.destructive ? "destructive" : null,
                      selected()?.live_gated ? "live-gated" : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  />
                </div>
                <Show
                  when={(preview().risks ?? []).length > 0}
                  fallback={<div class="ozagia-note">The preview listed no risks.</div>}
                >
                  <ul class="ozagia-list">
                    <For each={preview().risks ?? []}>
                      {(item) => (
                        <li>
                          <Badge tone="warn">risk</Badge> {item}
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
                <div class="ozagia-sep" />
                <label class="ozagia-check">
                  <input
                    type="checkbox"
                    checked={applyConfirmed()}
                    onChange={(event) => setApplyConfirmed(event.currentTarget.checked)}
                  />
                  {`I reviewed ${preview().action ?? action()}, its argv, risks and current state, and I want to apply it.`}
                </label>
                <div class="ozagia-row" style={{ "margin-top": "6px" }}>
                  <button
                    class="ozagia-button"
                    type="button"
                    disabled={!applyReady()}
                    onClick={() => void apply()}
                  >
                    {applyBusy() ? "Applying…" : "Apply action"}
                  </button>
                  <span class="ozagia-note">
                    Runs <code>actions_cli apply</code> through the add-in RPC. This is separate
                    from Preview and cannot be triggered by it.
                  </span>
                </div>
                <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                  Command shape: <code>{redactedApplyCommand(preview().action ?? action())}</code>
                </div>
                <Show when={preview().applicable === false}>
                  <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                    The preview reported that apply would be refused or a no-op; the CLI still
                    decides.
                  </div>
                </Show>
                <Show when={applyBusy()}>
                  <Loading label="Applying the confirmed action…" />
                </Show>
                <Show when={applyResult() && !applyResult()!.ok}>
                  <FailureView envelope={applyResult()!} />
                </Show>
                <Show when={applyResult()?.ok && applyResult()?.data}>
                  {(applied) => (
                    <>
                      <div class="ozagia-sep" />
                      <div class="ozagia-sub">Apply result</div>
                      <div class="ozagia-metrics">
                        <Metric label="state" value={applied().state ?? "—"} />
                        <Metric label="ok" value={applied().ok ? "yes" : "no"} />
                        <Metric label="mode" value={applied().mode ?? "apply"} />
                      </div>
                      <Show when={applied().error}>
                        <div class="ozagia-note">{`Apply error: ${applied().error}`}</div>
                      </Show>
                      <Show when={applied().current_state}>
                        <div class="ozagia-note">{`Current state: ${applied().current_state}`}</div>
                      </Show>
                      <Show when={applied().result !== null && applied().result !== undefined}>
                        <details class="ozagia-details">
                          <summary>Structured result</summary>
                          <pre class="ozagia-pre">
                            {JSON.stringify(applied().result, null, 2)}
                          </pre>
                        </details>
                      </Show>
                      <Show when={applied().audit}>
                        <div class="ozagia-note">{`Audit: ${applied().audit}`}</div>
                      </Show>
                      <Show when={applied().audit_error}>
                        <div class="ozagia-note">{`Audit warning: ${applied().audit_error}`}</div>
                      </Show>
                    </>
                  )}
                </Show>
              </Show>
            </Card>
          </>
        )}
      </Show>

      <Show when={!busy() && !result()}>
        <Empty>
          <div>No preview yet.</div>
          <div class="ozagia-note" style={{ "margin-top": "4px" }}>
            Pick an action and preview it. Nothing is started, stopped, signalled or connected
            until you check the confirmation box and press Apply.
          </div>
        </Empty>
      </Show>
    </div>
  )
}
