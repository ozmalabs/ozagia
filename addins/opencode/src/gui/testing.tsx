import { For, Show, createSignal } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type { Envelope, TestPlan } from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, Card, Empty, Failure, Loading, Metric } from "./ui"

function splitChanged(value: string): string[] {
  const parts = value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  return [...new Set(parts)].slice(0, 500)
}

function freshnessTone(value?: string): "ok" | "warn" | "error" | "muted" {
  if (value === "fresh") return "ok"
  if (value === "missing") return "error"
  if (value === "unknown" || value === "stale") return "warn"
  return "muted"
}

export function TestingTab(props: {
  lastPlan: () => Envelope<TestPlan> | null
  setLastPlan: (value: Envelope<TestPlan> | null) => void
}) {
  const ctx = usePlugin<typeof definition>()
  const [base, setBase] = createSignal("HEAD")
  const [head, setHead] = createSignal("")
  const [task, setTask] = createSignal("")
  const [changed, setChanged] = createSignal("")
  const [busy, setBusy] = createSignal(false)
  const result = () => props.lastPlan()

  const plan = () => {
    const envelope = result()
    if (!envelope?.ok || !envelope.data) return null
    return envelope.data
  }

  const submit = async () => {
    if (busy()) return
    setBusy(true)
    const input: { base?: string; head?: string; task?: string; changed?: string[] } = {}
    const baseValue = base().trim()
    const headValue = head().trim()
    const taskValue = task().trim()
    const paths = splitChanged(changed())
    if (baseValue) input.base = baseValue
    if (headValue) input.head = headValue
    if (taskValue) input.task = taskValue
    if (paths.length) input.changed = paths
    const envelope = await invoke<TestPlan>(ctx, () =>
      ctx.client.rpc(Ozagia).testingPlan(input),
    )
    props.setLastPlan(envelope)
    setBusy(false)
  }

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Testing</div>
          <div class="ozagia-sub">Plan affected tests; nothing is executed</div>
        </div>
      </div>

      <div class="ozagia-card">
        <h3>Plan inputs</h3>
        <div class="ozagia-row">
          <label class="ozagia-field">
            <span>Base</span>
            <input class="ozagia-input" value={base()} onInput={(event) => setBase(event.currentTarget.value)} placeholder="HEAD" />
          </label>
          <label class="ozagia-field">
            <span>Head</span>
            <input class="ozagia-input" value={head()} onInput={(event) => setHead(event.currentTarget.value)} placeholder="working tree" />
          </label>
          <label class="ozagia-field">
            <span>Task class</span>
            <input class="ozagia-input" value={task()} onInput={(event) => setTask(event.currentTarget.value)} placeholder="optional" />
          </label>
        </div>
        <label class="ozagia-field">
          <span>Changed files (comma or newline separated)</span>
          <textarea
            class="ozagia-textarea"
            value={changed()}
            onInput={(event) => setChanged(event.currentTarget.value)}
            placeholder="optional; defaults to git diff base..head"
          />
        </label>
        <div class="ozagia-row" style={{ "margin-top": "8px" }}>
          <button class="ozagia-button" type="button" disabled={busy()} onClick={() => void submit()}>
            {busy() ? "Planning…" : "Plan tests"}
          </button>
          <span class="ozagia-note">
            A plan selects tests. It does not run them, call a model or use the network.
          </span>
        </div>
      </div>

      <Show when={busy()}>
        <Loading label="Planning affected tests…" />
      </Show>

      <Show when={result() && !result()!.ok}>
        <Failure
          error={result()!.error ?? { code: "unknown", message: "No error detail was returned." }}
          command={result()!.command}
          title="The test planner did not return a plan."
          onRetry={() => void submit()}
        />
      </Show>

      <Show when={plan()}>
        {(current) => (
          <>
            <Card title="Plan">
              <div class="ozagia-metrics">
                <Metric label="mode" value={current().mode ?? "—"} />
                <Metric label="requested" value={current().requested_mode ?? "—"} />
                <Metric label="changed" value={current().counts?.changed_files ?? 0} />
                <Metric label="selected" value={current().counts?.selected_files ?? 0} />
                <Metric label="omitted" value={current().counts?.omitted_files ?? 0} />
                <Metric label="max tests" value={current().counts?.max_tests ?? "—"} />
              </div>
              <div class="ozagia-chips">
                <Badge tone={freshnessTone(current().graph?.freshness)}>
                  {`graph: ${current().graph?.freshness ?? "unknown"}`}
                </Badge>
                <Show when={current().graph?.usable === false}>
                  <Badge tone="warn">graph not used</Badge>
                </Show>
                <Show when={current().fallback_used}>
                  <Badge tone="warn">{`fallback: ${current().fallback ?? "yes"}`}</Badge>
                </Show>
                <Show when={current().refused}>
                  <Badge tone="error">refused</Badge>
                </Show>
                <Badge tone="muted">{`executed: ${current().executed ? "yes" : "no"}`}</Badge>
                <Badge tone="muted">{`truth assessed: ${current().truth_assessed ? "yes" : "no"}`}</Badge>
              </div>
              <Show when={current().mode_reason}>
                <div class="ozagia-note">{current().mode_reason}</div>
              </Show>
              <Show when={current().fallback_reason}>
                <div class="ozagia-note">{`Fallback reason: ${current().fallback_reason}`}</div>
              </Show>
              <Show when={current().graph?.reason}>
                <div class="ozagia-note">{`Graph: ${current().graph?.reason}`}</div>
              </Show>
              <Show when={current().graph?.path}>
                <div class="ozagia-note">{`Graph path: ${current().graph?.path}`}</div>
              </Show>
            </Card>

            <Card title="Selected tests">
              <Show
                when={(current().selected_tests ?? []).length > 0}
                fallback={<Empty>{current().refused ? "The policy refused to select tests." : "No tests selected."}</Empty>}
              >
                <div class="ozagia-scroll">
                  <table class="ozagia-table">
                    <thead>
                      <tr>
                        <th>file</th>
                        <th>sources</th>
                        <th>nodes</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={current().selected_tests ?? []}>
                        {(item) => (
                          <tr>
                            <td>
                              <span class="ozagia-truncate" title={item.file}>{item.file}</span>
                            </td>
                            <td>{(item.sources ?? []).join(", ") || "—"}</td>
                            <td>{(item.node_ids ?? []).length}</td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
              </Show>
              <Show when={current().counts?.cap_exceeded}>
                <div class="ozagia-note">The max-tests cap was exceeded by mandatory tests.</div>
              </Show>
            </Card>

            <Card title="Omitted tests">
              <Show
                when={(current().omitted ?? []).length > 0}
                fallback={<Empty>Nothing omitted.</Empty>}
              >
                <ul class="ozagia-list">
                  <For each={current().omitted ?? []}>
                    {(item) => <li>{`${item.file} — ${item.reason}`}</li>}
                  </For>
                </ul>
                <Show when={current().omitted_truncated}>
                  <div class="ozagia-note">The omitted list was truncated by the planner.</div>
                </Show>
              </Show>
            </Card>

            <Card title="What the plan did not assess">
              <Show
                when={(current().not_assessed ?? []).length > 0}
                fallback={<div class="ozagia-note">The planner did not list omissions.</div>}
              >
                <ul class="ozagia-list">
                  <For each={current().not_assessed ?? []}>{(item) => <li>{item}</li>}</For>
                </ul>
              </Show>
              <Show when={(current().warnings ?? []).length > 0}>
                <div class="ozagia-sep" />
                <ul class="ozagia-list">
                  <For each={current().warnings ?? []}>{(item) => <li>{item}</li>}</For>
                </ul>
              </Show>
              <Show when={current().claim}>
                <div class="ozagia-note">{current().claim}</div>
              </Show>
            </Card>
          </>
        )}
      </Show>

      <Show when={!busy() && !result()}>
        <Empty>
          <div>No plan yet.</div>
          <div class="ozagia-note" style={{ "margin-top": "4px" }}>
            Base defaults to HEAD; leaving head empty compares against the working tree.
          </div>
        </Empty>
      </Show>
    </div>
  )
}
