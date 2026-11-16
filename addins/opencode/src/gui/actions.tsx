import { For, Show, createEffect, createMemo, createResource, createSignal } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type { ActionPreview, ActionSummary, Envelope } from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, Card, Empty, FailureView, Loading, Metric } from "./ui"

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

  const [actions, { refetch }] = createResource<Envelope<ActionSummary[]>>(() =>
    invoke<ActionSummary[]>(ctx, () => ctx.client.rpc(Ozagia).actionsList({})),
  )

  const actionData = () => {
    const envelope = actions()
    if (!envelope?.ok || !Array.isArray(envelope.data)) return null
    return envelope.data
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

    setBusy(true)
    const envelope = await invoke<ActionPreview>(ctx, () =>
      ctx.client.rpc(Ozagia).actionPreview(input),
    )
    setResult(envelope)
    setBusy(false)
  }

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Actions</div>
          <div class="ozagia-sub">Preview only: this panel never applies an action</div>
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
                    : "Runs the read-only preview and shows its token; it never applies."}
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
                Display only. This add-in has no Apply control and no mutating RPC. Applying needs
                the separate <code>ozagia.actions_cli apply</code> command after reviewing the
                operation, argv and risks above.
              </div>
            </Card>
          </>
        )}
      </Show>

      <Show when={!busy() && !result()}>
        <Empty>
          <div>No preview yet.</div>
          <div class="ozagia-note" style={{ "margin-top": "4px" }}>
            Pick an action and preview it. Nothing is started, stopped, signalled or connected.
          </div>
        </Empty>
      </Show>
    </div>
  )
}
