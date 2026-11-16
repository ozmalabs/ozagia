import { For, Show, createMemo, createSignal } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type { Envelope, ResearchColumn, ResearchView } from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, Card, Empty, FailureView, Loading, Metric } from "./ui"

const VIEWS = ["library", "fetch", "screening", "evidence", "consensus", "citation"] as const
type ResearchViewName = (typeof VIEWS)[number]

const VIEW_LABELS: Record<ResearchViewName, string> = {
  library: "Reference library",
  fetch: "Fetch record",
  screening: "Screening decisions",
  evidence: "Evidence map",
  consensus: "Review consensus",
  citation: "Citation graph",
}

function display(value: unknown): string {
  if (value === null || value === undefined) return "—"
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function missingState(envelope: Envelope): boolean {
  const text = `${envelope.error?.message ?? ""} ${envelope.error?.detail ?? ""}`.toLowerCase()
  return /not found|does not exist|no such file|missing|no research|invalid research/.test(text)
}

export function ResearchTab() {
  const ctx = usePlugin<typeof definition>()
  const [investigation, setInvestigation] = createSignal("")
  const [view, setView] = createSignal<ResearchViewName>("library")
  const [result, setResult] = createSignal<Envelope<ResearchView> | null>(null)
  const [busy, setBusy] = createSignal(false)

  const model = () => {
    const envelope = result()
    if (!envelope?.ok || !envelope.data) return null
    return envelope.data
  }

  const columns = createMemo<ResearchColumn[]>(() => {
    const declared = model()?.columns ?? []
    if (declared.length) return declared
    const first = model()?.rows?.[0]
    if (!first) return []
    return Object.keys(first).map((key) => ({ key, label: key }))
  })

  const rows = createMemo(() => (model()?.rows ?? []).slice(0, 100))
  const summary = createMemo(() =>
    Object.entries(model()?.summary ?? {}).sort(([a], [b]) => a.localeCompare(b)),
  )

  const submit = async () => {
    const id = investigation().trim()
    if (!id || busy()) return
    setBusy(true)
    const envelope = await invoke<ResearchView>(ctx, () =>
      ctx.client.rpc(Ozagia).researchView({ investigation: id, view: view() }),
    )
    setResult(envelope)
    setBusy(false)
  }

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Research</div>
          <div class="ozagia-sub">Read-only projection of a recorded research investigation</div>
        </div>
        <button
          class="ozagia-button"
          type="button"
          disabled={busy() || !investigation().trim()}
          onClick={() => void submit()}
        >
          {busy() ? "Loading…" : "Load view"}
        </button>
      </div>

      <div class="ozagia-card">
        <h3>Investigation</h3>
        <div class="ozagia-row">
          <label class="ozagia-field">
            <span>Investigation id</span>
            <input
              class="ozagia-input"
              value={investigation()}
              onInput={(event) => setInvestigation(event.currentTarget.value)}
              placeholder="research-20261116-120000-abcd1234"
              spellcheck={false}
            />
          </label>
          <label class="ozagia-field">
            <span>View</span>
            <select
              class="ozagia-select"
              value={view()}
              onChange={(event) => setView(event.currentTarget.value as ResearchViewName)}
            >
              <For each={VIEWS}>
                {(item) => <option value={item}>{VIEW_LABELS[item]}</option>}
              </For>
            </select>
          </label>
        </div>
        <div class="ozagia-note" style={{ "margin-top": "6px" }}>
          The id names a recorded investigation under the state directory. This panel does not
          build a graph, fetch an artifact, call a model or assess truth.
        </div>
      </div>

      <Show when={busy()}>
        <Loading label="Loading the research view…" />
      </Show>

      <Show when={result() && !result()!.ok}>
        <FailureView envelope={result()!} onRetry={() => void submit()} />
        <Empty>
          <div>No research view was loaded.</div>
          <Show when={missingState(result()!)}>
            <div class="ozagia-note" style={{ "margin-top": "6px" }}>
              The investigation id or its recorded graph was not found. Check the id and the
              state directory; this panel never creates an investigation.
            </div>
          </Show>
          <div class="ozagia-note" style={{ "margin-top": "4px" }}>
            Read-only command: <code>ozagia.research_ui_cli --investigation ID --view VIEW</code>
          </div>
        </Empty>
      </Show>

      <Show when={model()}>
        {(current) => (
          <>
            <Card title={current().title ?? "Research view"} state={current().view ?? undefined}>
              <div class="ozagia-metrics">
                <Metric label="view" value={current().view ?? "—"} />
                <Metric label="rows" value={(current().rows ?? []).length} />
                <Metric label="columns" value={columns().length} />
                <Metric label="nodes" value={(current().nodes ?? []).length} />
              </div>
              <div class="ozagia-chips">
                <Badge tone="muted">{`investigation: ${current().investigation_id ?? "unknown"}`}</Badge>
                <Badge tone="muted">{`truth assessed: ${current().truth_assessed ? "yes" : "no"}`}</Badge>
                <Show when={(current().omitted?.rows ?? 0) > 0}>
                  <Badge tone="warn">{`${current().omitted?.rows} row(s) omitted`}</Badge>
                </Show>
              </div>
              <Show when={current().note}>
                <div class="ozagia-note" style={{ "margin-top": "6px" }}>{current().note}</div>
              </Show>
            </Card>

            <Card title="Summary">
              <Show
                when={summary().length > 0}
                fallback={<Empty>This view recorded no summary counts.</Empty>}
              >
                <dl class="ozagia-grid">
                  <For each={summary()}>
                    {([key, value]) => (
                      <>
                        <dt>{key}</dt>
                        <dd>{display(value)}</dd>
                      </>
                    )}
                  </For>
                </dl>
              </Show>
            </Card>

            <Card title="Rows">
              <Show
                when={rows().length > 0}
                fallback={<Empty>This view selected no rows.</Empty>}
              >
                <div class="ozagia-scroll">
                  <table class="ozagia-table">
                    <thead>
                      <tr>
                        <For each={columns()}>
                          {(column) => <th>{column.label || column.key}</th>}
                        </For>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={rows()}>
                        {(row) => (
                          <tr>
                            <For each={columns()}>
                              {(column) => <td>{display(row[column.key])}</td>}
                            </For>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
                <Show when={(current().rows ?? []).length > rows().length}>
                  <div class="ozagia-note">
                    {`Showing ${rows().length} of ${(current().rows ?? []).length} returned rows.`}
                  </div>
                </Show>
              </Show>
              <Show when={(current().omitted?.rows ?? 0) > 0}>
                <div class="ozagia-note">
                  {`The view omitted ${current().omitted?.rows} row(s): ${current().omitted?.reason ?? "row cap"}. This is not a complete projection.`}
                </div>
              </Show>
            </Card>

            <Card title="Provenance and omissions">
              <dl class="ozagia-grid">
                <dt>generator</dt>
                <dd>{current().provenance?.generator ?? "—"}</dd>
                <dt>source</dt>
                <dd>{current().provenance?.source ?? "—"}</dd>
                <dt>root</dt>
                <dd>{current().provenance?.root ?? "—"}</dd>
                <dt>graph schema</dt>
                <dd>{display(current().provenance?.graph_schema)}</dd>
                <dt>view schema</dt>
                <dd>{display(current().provenance?.view_schema)}</dd>
                <dt>derived</dt>
                <dd>{current().provenance?.derived ? "yes (projection of recorded records)" : "not recorded"}</dd>
                <dt>row cap</dt>
                <dd>{display(current().omitted?.limit)}</dd>
                <dt>omission reason</dt>
                <dd>{current().omitted?.reason ?? "—"}</dd>
              </dl>
              <details class="ozagia-details">
                <summary>Record counts</summary>
                <pre class="ozagia-pre">{JSON.stringify(current().provenance?.record_counts ?? {}, null, 2)}</pre>
              </details>
              <Show when={current().omitted?.graph}>
                <details class="ozagia-details">
                  <summary>Graph projection omissions</summary>
                  <pre class="ozagia-pre">{JSON.stringify(current().omitted?.graph, null, 2)}</pre>
                </details>
              </Show>
              <div class="ozagia-note">
                Provenance describes the projection. It does not establish freshness, support or
                consensus.
              </div>
            </Card>

            <Card title="What this view did not assess">
              <Show
                when={(current().not_assessed ?? []).length > 0}
                fallback={<div class="ozagia-note">The view did not list its limits.</div>}
              >
                <ul class="ozagia-list">
                  <For each={current().not_assessed ?? []}>{(item) => <li>{item}</li>}</For>
                </ul>
              </Show>
            </Card>
          </>
        )}
      </Show>

      <Show when={!busy() && !result()}>
        <Empty>
          <div>No research view yet.</div>
          <div class="ozagia-note" style={{ "margin-top": "4px" }}>
            Enter an investigation id and choose a view. A missing investigation is reported as an
            error, not as an empty result.
          </div>
        </Empty>
      </Show>
    </div>
  )
}
