import {
  For,
  Show,
  createEffect,
  createMemo,
  createResource,
  createSignal,
} from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type { ApertureSummary, Envelope, ViewModel, ViewNode } from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, Empty, FailureView, Loading, Metric } from "./ui"

function shortHead(value?: string | null): string {
  if (!value) return "not recorded"
  return value.length > 12 ? value.slice(0, 12) : value
}

function GraphCanvas(props: {
  model: ViewModel
  selected: string | null
  onSelect: (id: string) => void
}) {
  const WIDTH = 360
  const HEIGHT = 300
  const CX = WIDTH / 2
  const CY = HEIGHT / 2

  const nodes = createMemo(() => [...props.model.nodes].sort((a, b) => a.id.localeCompare(b.id)))
  const positions = createMemo(() => {
    const list = nodes()
    const map = new Map<string, { x: number; y: number }>()
    const radius = list.length <= 1 ? 0 : 112
    const seed = props.model.layout?.seed ?? 7
    list.forEach((node, index) => {
      const angle = (2 * Math.PI * index) / Math.max(1, list.length) + seed * 0.137
      map.set(node.id, {
        x: CX + radius * Math.cos(angle),
        y: CY + radius * Math.sin(angle),
      })
    })
    return map
  })

  const color = (group: string) => props.model.legend.color?.[group] ?? "#7a869a"
  const width = (weight?: number) => 3 + Math.min(5, Math.max(0, weight ?? 0) * 5)

  return (
    <svg
      class="ozagia-svg"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={`${props.model.aperture} graph, ${props.model.nodes.length} nodes and ${props.model.edges.length} edges`}
    >
      <For each={props.model.edges}>
        {(edge) => {
          const from = () => positions().get(edge.src)
          const to = () => positions().get(edge.tgt)
          return (
            <Show when={from() && to()}>
              <line
                class="ozagia-svg-edge"
                x1={from()!.x}
                y1={from()!.y}
                x2={to()!.x}
                y2={to()!.y}
              >
                <title>{`${edge.type}: ${edge.src} → ${edge.tgt}`}</title>
              </line>
            </Show>
          )
        }}
      </For>
      <For each={nodes()}>
        {(node: ViewNode) => {
          const point = () => positions().get(node.id) ?? { x: CX, y: CY }
          return (
            <g
              tabIndex={0}
              role="button"
              aria-label={`${node.label} (${node.type})`}
              onClick={() => props.onSelect(node.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault()
                  props.onSelect(node.id)
                }
              }}
            >
              <circle
                class="ozagia-svg-node"
                data-selected={node.id === props.selected ? "true" : "false"}
                cx={point().x}
                cy={point().y}
                r={width(node.weight)}
                fill={color(node.group)}
              >
                <title>{`${node.label} · ${node.type} · ${node.group}`}</title>
              </circle>
            </g>
          )
        }}
      </For>
    </svg>
  )
}

export function GraphTab() {
  const ctx = usePlugin<typeof definition>()
  const [aperture, setAperture] = createSignal("architecture")
  const [tick, setTick] = createSignal(0)
  const [selected, setSelected] = createSignal<string | null>(null)

  const [apertures, { refetch: refetchApertures }] = createResource<Envelope<ApertureSummary[]>>(
    () => invoke<ApertureSummary[]>(ctx, () => ctx.client.rpc(Ozagia).apertures({})),
  )

  const [view, { refetch: refetchView }] = createResource(
    () => ({ aperture: aperture(), tick: tick() }),
    (input) =>
      invoke<ViewModel>(ctx, () => ctx.client.rpc(Ozagia).view({ aperture: input.aperture })),
  )

  const apertureData = () => {
    const envelope = apertures()
    if (!envelope?.ok || !Array.isArray(envelope.data)) return [] as ApertureSummary[]
    return envelope.data
  }
  const model = () => {
    const envelope = view()
    if (!envelope?.ok || !envelope.data) return null
    if (!Array.isArray(envelope.data.nodes) || !Array.isArray(envelope.data.edges)) return null
    return envelope.data
  }
  const selectedNode = createMemo(() => model()?.nodes.find((node) => node.id === selected()) ?? null)
  const visibleNodes = createMemo(() =>
    [...(model()?.nodes ?? [])]
      .sort((a, b) => a.id.localeCompare(b.id))
      .slice(0, 120),
  )

  createEffect(() => {
    const list = apertureData()
    if (list.length && !list.some((item) => item.id === aperture())) {
      setAperture(list[0].id)
    }
  })

  const refresh = () => {
    setTick((value) => value + 1)
    void refetchApertures()
  }

  return (
    <div class="ozagia-stack">
      <div class="ozagia-head">
        <div>
          <div class="ozagia-title">Graph</div>
          <div class="ozagia-sub">Aperture projection from the local graph snapshot</div>
        </div>
        <button class="ozagia-button" type="button" onClick={refresh}>
          Refresh
        </button>
      </div>

      <div class="ozagia-row">
        <label class="ozagia-field">
          <span>Aperture</span>
          <select
            class="ozagia-select"
            value={aperture()}
            onChange={(event) => setAperture(event.currentTarget.value)}
          >
            <For each={apertureData()}>
              {(item) => <option value={item.id}>{`${item.title} (${item.layout})`}</option>}
            </For>
            <Show when={!apertureData().some((item) => item.id === aperture())}>
              <option value={aperture()}>{aperture()}</option>
            </Show>
          </select>
        </label>
        <div class="ozagia-field" style={{ "flex": "0 0 auto" }}>
          <span>View</span>
          <Badge tone={model() ? "ok" : "muted"}>
            {model() ? model()!.layout.kind ?? "layout" : "—"}
          </Badge>
        </div>
      </div>

      <Show when={apertures() && !apertures()!.ok}>
        <FailureView
          envelope={apertures()}
          onRetry={() => void refetchApertures()}
        />
      </Show>

      <Show when={view.loading && !model()}>
        <Loading label="Loading graph view…" />
      </Show>
      <Show when={!view.loading && view() && !view()!.ok}>
        <FailureView envelope={view()} onRetry={() => void refetchView()} />
        <Empty>
          <div>No view is available. Build a graph snapshot first, then refresh.</div>
          <div class="ozagia-note" style={{ "margin-top": "6px" }}>
            Example: <code>python -m ozagia.core.memory build</code> (see docs/view.md)
          </div>
        </Empty>
      </Show>

      <Show when={model()}>
        {(current) => (
          <>
            <div class="ozagia-metrics">
              <Metric label="nodes" value={current().nodes.length} />
              <Metric label="edges" value={current().edges.length} />
              <Metric label="groups" value={current().groups.length} />
              <Metric
                label="omitted"
                value={`${current().omitted.nodes} / ${current().omitted.edges}`}
              />
            </div>

            <Show
              when={current().nodes.length > 0}
              fallback={<Empty>This aperture selected no nodes.</Empty>}
            >
              <GraphCanvas model={current()} selected={selected()} onSelect={setSelected} />
              <div class="ozagia-chips">
                <For each={current().groups}>
                  {(group) => (
                    <span class="ozagia-chip">
                      <i
                        class="ozagia-dot"
                        style={{ background: current().legend.color?.[group.id] ?? "#7a869a" }}
                      />
                      {`${group.label} (${group.count}${group.collapsed ? `, ${group.collapsed} collapsed` : ""})`}
                    </span>
                  )}
                </For>
              </div>
            </Show>

            <Show when={selectedNode()}>
              {(node) => (
                <div class="ozagia-card">
                  <h3>Node inspector</h3>
                  <dl class="ozagia-grid">
                    <dt>id</dt>
                    <dd>{node().id}</dd>
                    <dt>label</dt>
                    <dd>{node().label}</dd>
                    <dt>type</dt>
                    <dd>{node().type}{node().raw_type && node().raw_type !== node().type ? ` (${node().raw_type})` : ""}</dd>
                    <dt>group</dt>
                    <dd>{node().group}</dd>
                    <dt>key</dt>
                    <dd>{node().key ?? "—"}</dd>
                    <dt>weight</dt>
                    <dd>{node().weight ?? "—"}</dd>
                  </dl>
                  <details class="ozagia-details">
                    <summary>Recorded attributes</summary>
                    <pre class="ozagia-pre">{JSON.stringify(node().attrs ?? {}, null, 2)}</pre>
                  </details>
                </div>
              )}
            </Show>

            <div class="ozagia-card">
              <h3>Nodes</h3>
              <div class="ozagia-scroll">
                <table class="ozagia-table">
                  <thead>
                    <tr>
                      <th>label</th>
                      <th>type</th>
                      <th>group</th>
                      <th>weight</th>
                    </tr>
                  </thead>
                  <tbody>
                    <For each={visibleNodes()}>
                      {(node) => (
                        <tr
                          tabIndex={0}
                          style={{ cursor: "pointer" }}
                          onClick={() => setSelected(node.id)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault()
                              setSelected(node.id)
                            }
                          }}
                        >
                          <td>
                            <span class="ozagia-truncate" title={node.label}>{node.label}</span>
                          </td>
                          <td>{node.type}</td>
                          <td>{node.group}</td>
                          <td>{node.weight ?? "—"}</td>
                        </tr>
                      )}
                    </For>
                  </tbody>
                </table>
              </div>
              <Show when={current().nodes.length > visibleNodes().length}>
                <div class="ozagia-note">
                  {`Showing ${visibleNodes().length} of ${current().nodes.length} nodes; the SVG shows all of them.`}
                </div>
              </Show>
            </div>

            <div class="ozagia-card">
              <h3>Snapshot and provenance</h3>
              <dl class="ozagia-grid">
                <dt>aperture</dt>
                <dd>{current().aperture}</dd>
                <dt>snapshot root</dt>
                <dd>{current().snapshot.root ?? "—"}</dd>
                <dt>head</dt>
                <dd>{shortHead(current().snapshot.head)}</dd>
                <dt>freshness</dt>
                <dd>{current().snapshot.freshness ?? "not recorded"}</dd>
                <dt>graph</dt>
                <dd>{current().provenance.graph ?? "—"} (schema {current().provenance.graph_schema ?? "—"})</dd>
                <dt>sources</dt>
                <dd>{(current().provenance.sources ?? []).join(", ") || "—"}</dd>
                <dt>omitted</dt>
                <dd>
                  {`${current().omitted.nodes} node(s), ${current().omitted.edges} edge(s); ${current().omitted.reason}`}
                </dd>
              </dl>
              <Show when={Object.keys(current().omitted.by ?? {}).length > 0}>
                <details class="ozagia-details">
                  <summary>Omission breakdown</summary>
                  <pre class="ozagia-pre">{JSON.stringify(current().omitted.by, null, 2)}</pre>
                </details>
              </Show>
              <div class="ozagia-note">
                A view reports the recorded graph through this aperture. It does not establish
                freshness, support or consensus.
              </div>
            </div>
          </>
        )}
      </Show>

      <Show when={!view.loading && !model() && !(view() && !view()!.ok)}>
        <Empty>No graph view was returned.</Empty>
      </Show>
    </div>
  )
}
