import { For, Show, createEffect, createMemo, createResource, createSignal } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type {
  Envelope,
  TaskBudget,
  TaskDetail,
  TaskEntry,
  TaskListReport,
  TaskResources,
  TaskValidateReport,
} from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { taskRunCommand } from "./task-command"
import { Badge, Card, Empty, FailureView, Loading, Metric } from "./ui"

const DOMAINS = ["all", "research", "coding", "ops"] as const
type DomainFilter = (typeof DOMAINS)[number]

const DOMAIN_LABELS: Record<DomainFilter, string> = {
  all: "All domains",
  research: "Research",
  coding: "Coding",
  ops: "Ops",
}

interface TasksSnapshot {
  list: Envelope<TaskListReport>
  validation: Envelope<TaskValidateReport>
}

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "—"
  if (typeof value === "string") return value || "—"
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function rootMissing(...envelopes: Array<Envelope | undefined>): boolean {
  return envelopes.some((envelope) => envelope?.error?.code === "root_missing")
}

function budgetRows(budget?: TaskBudget): Array<[string, string]> {
  if (!budget) return []
  return [
    ["tokens", valueText(budget.tokens)],
    ["model calls", valueText(budget.model_calls)],
    ["cost USD", valueText(budget.cost_usd)],
    ["wall s", valueText(budget.wall_s)],
  ]
}

function resourceRows(resources?: TaskResources): Array<[string, string]> {
  if (!resources) return []
  return [
    ["memory MB", valueText(resources.memory_mb)],
    ["disk MB", valueText(resources.disk_mb)],
    ["pids", valueText(resources.pids)],
    ["wall s", valueText(resources.wall_s)],
  ]
}

export function TasksTab() {
  const ctx = usePlugin<typeof definition>()
  const [domain, setDomain] = createSignal<DomainFilter>("all")
  const [selectedId, setSelectedId] = createSignal<string | null>(null)
  const [detail, setDetail] = createSignal<Envelope<TaskDetail> | null>(null)
  const [detailBusy, setDetailBusy] = createSignal(false)
  const [detailNonce, setDetailNonce] = createSignal(0)
  const [copyState, setCopyState] = createSignal<"idle" | "copied" | "failed">("idle")

  const [snapshot, { refetch }] = createResource<TasksSnapshot>(async () => {
    const [list, validation] = await Promise.all([
      invoke<TaskListReport>(ctx, () => ctx.client.rpc(Ozagia).tasksList({})),
      invoke<TaskValidateReport>(ctx, () => ctx.client.rpc(Ozagia).tasksValidate({})),
    ])
    return { list, validation }
  })

  const listEnvelope = () => snapshot()?.list
  const validationEnvelope = () => snapshot()?.validation

  const validation = (): TaskValidateReport | null => {
    const envelope = validationEnvelope()
    if (!envelope?.ok || !envelope.data) return null
    return envelope.data
  }

  const listed = (): TaskEntry[] => {
    const envelope = listEnvelope()
    if (!envelope?.ok) return []
    return envelope.data?.tasks ?? []
  }

  // `list` refuses an invalid library without printing JSON. `validate` still
  // returns the entries that did load, so the catalogue stays browsable and the
  // validation card explains what is wrong.
  const entries = (): TaskEntry[] => {
    const fromList = listed()
    if (fromList.length) return fromList
    return validation()?.tasks ?? []
  }

  const domainCount = (filter: DomainFilter): number =>
    entries().filter((task) => filter === "all" || task.domain === filter).length

  const visible = createMemo(() =>
    entries().filter((task) => domain() === "all" || task.domain === domain()),
  )

  const detailData = (): TaskDetail | null => {
    const envelope = detail()
    if (!envelope?.ok || !envelope.data) return null
    return envelope.data
  }

  const command = (): string => {
    const current = detailData()
    const root = validation()?.root
    if (!current || !root) return ""
    return taskRunCommand({
      path: current.entry.path,
      root,
      inputs: current.spec.inputs ?? [],
    })
  }

  const missingRoot = () => rootMissing(listEnvelope(), validationEnvelope())
  const detailsAvailable = () => validation()?.ok !== false
  const listFailed = () => {
    const envelope = listEnvelope()
    return !!envelope && !envelope.ok
  }
  const validationFailed = () => {
    const envelope = validationEnvelope()
    return !!envelope && !envelope.ok
  }

  createEffect(() => {
    const tasks = visible()
    const current = selectedId()
    if (!detailsAvailable() || !tasks.length) {
      if (current !== null) setSelectedId(null)
      return
    }
    if (!current || !tasks.some((task) => task.id === current)) {
      setSelectedId(tasks[0].id)
    }
  })

  let detailRequest = 0
  createEffect(() => {
    const id = selectedId()
    const catalogue = snapshot()
    const available = detailsAvailable()
    void detailNonce()
    if (!id || !catalogue || !available) {
      setDetail(null)
      setDetailBusy(false)
      return
    }

    const request = ++detailRequest
    setDetail(null)
    setDetailBusy(true)
    void invoke<TaskDetail>(ctx, () => ctx.client.rpc(Ozagia).tasksShow({ id })).then(
      (envelope) => {
        if (request !== detailRequest) return
        setDetail(envelope)
        setDetailBusy(false)
      },
    )
  })

  const selectTask = (id: string) => {
    if (!detailsAvailable()) return
    setSelectedId(id)
    setCopyState("idle")
  }

  const refresh = () => {
    setDetail(null)
    setCopyState("idle")
    void refetch()
  }

  const retryDetail = () => {
    setCopyState("idle")
    setDetailNonce((value) => value + 1)
  }

  const copyCommand = async () => {
    const text = command()
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
          <div class="ozagia-title">Tasks</div>
          <div class="ozagia-sub">Browse versioned task classes; nothing is run or applied</div>
        </div>
        <button class="ozagia-button" type="button" onClick={refresh}>
          Refresh
        </button>
      </div>

      <Show when={snapshot.loading && !snapshot()}>
        <Loading label="Reading the task-class library…" />
      </Show>

      <Show when={listFailed() && !validation() && !missingRoot()}>
        <FailureView envelope={listEnvelope()} onRetry={refresh} />
      </Show>
      <Show when={validationFailed() && !missingRoot()}>
        <FailureView envelope={validationEnvelope()} onRetry={refresh} />
      </Show>

      <Show when={missingRoot()}>
        <Empty>
          <div>The ozagia root could not be found.</div>
          <Show when={validationEnvelope()?.error?.message ?? listEnvelope()?.error?.message}>
            {(message) => (
              <div class="ozagia-note" style={{ "margin-top": "4px" }}>
                {message()}
              </div>
            )}
          </Show>
          <div class="ozagia-note" style={{ "margin-top": "6px" }}>
            Set the plugin option <code>root</code> to the repository, or start OpenCode in
            the repository directory. The Tasks tab never guesses a root.
          </div>
        </Empty>
      </Show>

      <Show when={validation()}>
        {(report) => (
          <Card
            title="Library validation"
            state={report().ok ? "ok" : "invalid"}
            error={report().ok ? null : "validation errors"}
          >
            <div class="ozagia-metrics">
              <Metric label="task classes" value={report().counts?.total ?? entries().length} />
              <Metric label="built-in" value={report().counts?.builtin ?? "—"} />
              <Metric label="repo overrides" value={report().counts?.repo ?? "—"} />
              <Metric label="errors" value={(report().errors ?? []).length} />
              <Metric label="warnings" value={(report().warnings ?? []).length} />
            </div>
            <div class="ozagia-chips">
              <Badge tone={report().ok ? "ok" : "error"}>
                {report().ok ? "library valid" : "library invalid"}
              </Badge>
              <Show when={listFailed() && entries().length > 0}>
                <Badge tone="warn">catalogue read from validate</Badge>
              </Show>
            </div>
            <Show when={report().root}>
              <dl class="ozagia-grid" style={{ "margin-top": "6px" }}>
                <dt>root</dt>
                <dd>{report().root}</dd>
                <dt>built-in dir</dt>
                <dd>{report().builtin_dir ?? "—"}</dd>
              </dl>
            </Show>
            <Show when={(report().errors ?? []).length > 0}>
              <div class="ozagia-sep" />
              <ul class="ozagia-list">
                <For each={report().errors ?? []}>
                  {(message) => (
                    <li>
                      <Badge tone="error">error</Badge> {message}
                    </li>
                  )}
                </For>
              </ul>
            </Show>
            <Show when={(report().warnings ?? []).length > 0}>
              <div class="ozagia-sep" />
              <ul class="ozagia-list">
                <For each={report().warnings ?? []}>
                  {(message) => (
                    <li>
                      <Badge tone="warn">warning</Badge> {message}
                    </li>
                  )}
                </For>
              </ul>
            </Show>
            <Show when={(report().issues ?? []).length > 0}>
              <details class="ozagia-details">
                <summary>{`Issues (${(report().issues ?? []).length})`}</summary>
                <div class="ozagia-scroll">
                  <table class="ozagia-table">
                    <thead>
                      <tr>
                        <th>severity</th>
                        <th>code</th>
                        <th>task</th>
                        <th>message</th>
                      </tr>
                    </thead>
                    <tbody>
                      <For each={(report().issues ?? []).slice(0, 50)}>
                        {(issue) => (
                          <tr>
                            <td>{issue.severity}</td>
                            <td>{issue.code}</td>
                            <td>{issue.task_id ?? "—"}</td>
                            <td>{issue.message}</td>
                          </tr>
                        )}
                      </For>
                    </tbody>
                  </table>
                </div>
                <Show when={(report().issues ?? []).length > 50}>
                  <div class="ozagia-note">
                    {`Showing 50 of ${(report().issues ?? []).length} issues.`}
                  </div>
                </Show>
              </details>
            </Show>
            <Show when={!report().ok}>
              <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                An invalid library is reported, never hidden. Fix the named override or spec;
                this panel does not write a file or run a task.
              </div>
            </Show>
          </Card>
        )}
      </Show>

      <Show when={snapshot()}>
        <Card title="Task catalogue">
          <div class="ozagia-row">
            <label class="ozagia-field">
              <span>Domain</span>
              <select
                class="ozagia-select"
                value={domain()}
                onChange={(event) => setDomain(event.currentTarget.value as DomainFilter)}
              >
                <For each={DOMAINS}>
                  {(item) => (
                    <option value={item}>{`${DOMAIN_LABELS[item]} (${domainCount(item)})`}</option>
                  )}
                </For>
              </select>
            </label>
            <div class="ozagia-note">
              {`${visible().length} of ${entries().length} task class(es) shown`}
            </div>
          </div>

          <Show
            when={visible().length > 0}
            fallback={
              <Empty>
                <Show
                  when={entries().length > 0}
                  fallback={
                    <>
                      <div>No task classes were loaded.</div>
                      <div class="ozagia-note" style={{ "margin-top": "4px" }}>
                        The library returned no entries. Check the validation errors above;
                        built-ins live in the installed ozagia package.
                      </div>
                    </>
                  }
                >
                  <div>No task classes in this domain.</div>
                  <div class="ozagia-note" style={{ "margin-top": "4px" }}>
                    Choose another domain or refresh.
                  </div>
                </Show>
              </Empty>
            }
          >
            <div class="ozagia-scroll" style={{ "margin-top": "6px" }}>
              <table class="ozagia-table">
                <thead>
                  <tr>
                    <th>task class</th>
                    <th>domain</th>
                    <th>version</th>
                    <th>source</th>
                    <th>title</th>
                  </tr>
                </thead>
                <tbody>
                  <For each={visible()}>
                    {(task) => (
                      <tr
                        tabIndex={detailsAvailable() ? 0 : -1}
                        style={{
                          cursor: detailsAvailable() ? "pointer" : "default",
                          background:
                            task.id === selectedId() ? "var(--ozagia-soft)" : undefined,
                        }}
                        onClick={() => selectTask(task.id)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault()
                            selectTask(task.id)
                          }
                        }}
                      >
                        <td>
                          <div>{task.id}</div>
                          <Show when={(task.warnings ?? []).length > 0}>
                            <Badge tone="warn">{`${(task.warnings ?? []).length} warning(s)`}</Badge>
                          </Show>
                        </td>
                        <td>{task.domain}</td>
                        <td>{task.version}</td>
                        <td>
                          <Badge tone={task.source === "repo" ? "ok" : "muted"}>
                            {task.source}
                          </Badge>
                          <Show when={task.overrides}>
                            <div class="ozagia-note">{`overrides v${task.builtin_version ?? "?"}`}</div>
                          </Show>
                        </td>
                        <td>{task.title}</td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </div>
          </Show>
          <Show when={!detailsAvailable()}>
            <div class="ozagia-note" style={{ "margin-top": "6px" }}>
              Task details stay unavailable until the library validates. The list above shows
              the entries validate could load.
            </div>
          </Show>
        </Card>
      </Show>

      <Show when={detailBusy()}>
        <Loading label="Reading the task class…" />
      </Show>

      <Show when={detail() && !detail()!.ok}>
        <FailureView envelope={detail()!} onRetry={retryDetail} />
      </Show>

      <Show when={detailData()}>
        {(current) => {
          const entry = () => current().entry
          const spec = () => current().spec
          const defaults = () => entry().defaults
          const writeScope = () => spec().write_scope ?? defaults()?.write_scope ?? []
          const tools = () => spec().tools ?? defaults()?.tools ?? []
          const gates = () => spec().gates ?? defaults()?.gates
          const budget = () => spec().budget ?? defaults()?.budget
          const resources = () => spec().resources ?? defaults()?.resources
          const approval = () => spec().approval ?? defaults()?.approval
          const worktree = () => spec().worktree ?? defaults()?.worktree

          return (
            <>
              <Card title="Task class">
                <div class="ozagia-head" style={{ "margin-bottom": "6px" }}>
                  <div>
                    <div class="ozagia-title">{entry().title}</div>
                    <div class="ozagia-sub">{`${entry().id} · v${entry().version}`}</div>
                  </div>
                  <Badge tone={entry().source === "repo" ? "ok" : "muted"}>
                    {entry().source}
                  </Badge>
                </div>
                <div class="ozagia-chips">
                  <Badge tone="muted">{`domain: ${entry().domain}`}</Badge>
                  <Badge tone={writeScope().length > 0 ? "warn" : "ok"}>
                    {writeScope().length > 0 ? "declares writes" : "read-only"}
                  </Badge>
                  <Show when={spec().requires_graph ?? defaults()?.requires_graph}>
                    <Badge tone="warn">requires graph</Badge>
                  </Show>
                  <Show when={entry().overrides}>
                    <Badge tone="ok">{`repo override of v${entry().builtin_version ?? "?"}`}</Badge>
                  </Show>
                  <Show when={(entry().warnings ?? []).length > 0}>
                    <Badge tone="warn">{`${(entry().warnings ?? []).length} warning(s)`}</Badge>
                  </Show>
                </div>
                <p class="ozagia-note" style={{ "margin": "6px 0" }}>
                  {spec().objective || entry().description}
                </p>
                <div class="ozagia-metrics">
                  <Metric label="steps" value={spec().steps?.length ?? defaults()?.steps ?? 0} />
                  <Metric label="tools" value={tools().length} />
                  <Metric label="inputs" value={(spec().inputs ?? []).length} />
                  <Metric label="output" value={spec().output ?? defaults()?.output ?? "—"} />
                </div>
                <dl class="ozagia-grid">
                  <dt>path</dt>
                  <dd>{entry().path}</dd>
                  <dt>digest</dt>
                  <dd>{entry().digest ?? "—"}</dd>
                  <dt>built-in version</dt>
                  <dd>{valueText(entry().builtin_version)}</dd>
                </dl>
              </Card>

              <Card title="Scope">
                <dl class="ozagia-grid">
                  <dt>inputs</dt>
                  <dd>{(spec().inputs ?? []).join(", ") || "none"}</dd>
                  <dt>tools</dt>
                  <dd>{tools().join(", ") || "none"}</dd>
                  <dt>write scope</dt>
                  <dd>{writeScope().join(", ") || "none (read-only)"}</dd>
                </dl>
                <Show when={(entry().warnings ?? []).length > 0}>
                  <div class="ozagia-sep" />
                  <ul class="ozagia-list">
                    <For each={entry().warnings ?? []}>
                      {(warning) => <li>{warning}</li>}
                    </For>
                  </ul>
                </Show>
                <Show when={writeScope().length > 0}>
                  <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                    A task with a write scope previews mutations and stops for approval; this
                    panel never applies one.
                  </div>
                </Show>
              </Card>

              <Card title="Budget and resources">
                <Show
                  when={budget() || resources()}
                  fallback={<Empty>The task class declares no budget or resource limits.</Empty>}
                >
                  <div class="ozagia-grid">
                    <For each={budgetRows(budget())}>
                      {([label, value]) => (
                        <>
                          <dt>{`budget ${label}`}</dt>
                          <dd>{value}</dd>
                        </>
                      )}
                    </For>
                    <For each={resourceRows(resources())}>
                      {([label, value]) => (
                        <>
                          <dt>{`resource ${label}`}</dt>
                          <dd>{value}</dd>
                        </>
                      )}
                    </For>
                  </div>
                </Show>
              </Card>

              <Card title="Gates, approval and worktree">
                <dl class="ozagia-grid">
                  <dt>before gates</dt>
                  <dd>{(gates()?.before ?? []).join(", ") || "none"}</dd>
                  <dt>after gates</dt>
                  <dd>{(gates()?.after ?? []).join(", ") || "none"}</dd>
                  <dt>stop conditions</dt>
                  <dd>{(spec().stop ?? []).join(", ") || "mandatory stops only"}</dd>
                  <dt>approval mutate</dt>
                  <dd>{approval()?.mutate ?? "—"}</dd>
                  <dt>approval network</dt>
                  <dd>{approval()?.network ?? "—"}</dd>
                  <dt>expected branch</dt>
                  <dd>{worktree()?.expected_branch ?? "repository mapping"}</dd>
                  <dt>base</dt>
                  <dd>{worktree()?.base ?? "—"}</dd>
                  <dt>require clean</dt>
                  <dd>{valueText(worktree()?.require_clean)}</dd>
                  <dt>require up to date</dt>
                  <dd>{valueText(worktree()?.require_up_to_date)}</dd>
                  <dt>requires graph</dt>
                  <dd>{valueText(spec().requires_graph ?? defaults()?.requires_graph)}</dd>
                </dl>
                <Show when={spec().test_policy}>
                  <details class="ozagia-details">
                    <summary>Test policy</summary>
                    <pre class="ozagia-pre">
                      {JSON.stringify(spec().test_policy, null, 2) ?? "—"}
                    </pre>
                  </details>
                </Show>
              </Card>

              <Card title="Steps">
                <Show
                  when={(spec().steps ?? []).length > 0}
                  fallback={<Empty>The task class declares no steps.</Empty>}
                >
                  <div class="ozagia-scroll">
                    <table class="ozagia-table">
                      <thead>
                        <tr>
                          <th>#</th>
                          <th>id</th>
                          <th>tool</th>
                          <th>goal</th>
                          <th>paths</th>
                        </tr>
                      </thead>
                      <tbody>
                        <For each={spec().steps ?? []}>
                          {(step, index) => (
                            <tr>
                              <td>{index() + 1}</td>
                              <td>{step.id}</td>
                              <td>{step.tool}</td>
                              <td>{step.goal}</td>
                              <td>
                                <div>{(step.paths ?? []).join(", ") || "—"}</div>
                                <Show when={(step.write_paths ?? []).length > 0}>
                                  <div class="ozagia-note">
                                    {`writes: ${(step.write_paths ?? []).join(", ")}`}
                                  </div>
                                </Show>
                              </td>
                            </tr>
                          )}
                        </For>
                      </tbody>
                    </table>
                  </div>
                </Show>
              </Card>

              <Card title="Copy run command">
                <Show
                  when={command()}
                  fallback={
                    <Empty>
                      The repository root was not reported, so no run command can be built.
                    </Empty>
                  }
                >
                  <pre class="ozagia-pre">{command()}</pre>
                  <div class="ozagia-row" style={{ "margin-top": "6px" }}>
                    <button class="ozagia-button" type="button" onClick={() => void copyCommand()}>
                      {copyState() === "copied" ? "Copied" : "Copy command"}
                    </button>
                    <Badge tone="muted">preview only</Badge>
                  </div>
                  <div class="ozagia-note" style={{ "margin-top": "6px" }}>
                    This is a <code>tasks_cli run --dry-run</code> preview with a placeholder
                    for each declared input. It never passes <code>--allow-exec</code>. The
                    add-in does not run it, apply a task or call a model. Substitute the
                    configured interpreter for <code>python</code> if it differs.
                  </div>
                  <Show when={copyState() === "failed"}>
                    <div class="ozagia-note" style={{ "margin-top": "4px" }}>
                      The clipboard was unavailable; select the command text and copy it
                      manually.
                    </div>
                  </Show>
                </Show>
              </Card>
            </>
          )
        }}
      </Show>

      <Show
        when={
          !detailBusy() &&
          !detail() &&
          !snapshot.loading &&
          visible().length > 0 &&
          detailsAvailable()
        }
      >
        <Empty>Select a task class to inspect its scope, budgets, gates and steps.</Empty>
      </Show>
    </div>
  )
}
