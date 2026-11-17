import { For, Show, createResource } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, Card, Empty, FailureView, Loading, Metric } from "./ui"

type Session = {
  harness: string; session_id: string; first_seen?: string; last_seen?: string
  operations: number; tools: number; handled: number; passthrough: number
  tool_calls_observed: number; expired: number; failures: number; unknown: number
  errors: number; plugin_fallbacks: number; tools_by_name: Record<string, number>
  duration_ms: { count: number; sum: number; max: number | null }
  activity_source?: string
  daemon_profile?: { source: string; operations: number; tool_calls_observed: number; handled: number; passthrough: number; expired: number; failures: number; unknown: number; outcome_counts_incomplete?: boolean; errors: number; plugin_fallbacks: number; tools_by_name: Record<string, number>; duration_ms: { count: number; sum: number; max: number | null } } | null
  host_profile?: {
    model_calls: number; models: string[]
    tokens: Record<string, number | null>
    timing_ms: Record<string, number | null>
    tools: Record<string, Record<string, { count: number | null; timed_count: number | null; untimed_count: number | null; duration_ms_sum: number | null; duration_ms_max: number | null }>>
    coverage: Record<string, number | null>
  } | null
}
type Profile = {
  journal: string; observed_at: string; sessions: Session[]; truncated?: boolean
  coverage: Record<string, string>
}
type Status = { daemon?: { state?: string }; doctor?: { report?: { snapshot?: { state?: string } } } }

export function ProfileTab() {
  const ctx = usePlugin<typeof definition>()
  const [profile, { refetch: refreshProfile }] = createResource(() =>
    invoke<Profile>(ctx, () => ctx.client.rpc(Ozagia).sessionProfile({})),
  )
  const [status, { refetch: refreshStatus }] = createResource(() =>
    invoke<Status>(ctx, () => ctx.client.rpc(Ozagia).status({})),
  )
  const refresh = () => { void refreshProfile(); void refreshStatus() }
  const data = () => profile()?.ok ? profile()!.data : null
  const graph = () => status()?.ok ? status()!.data?.doctor?.report?.snapshot?.state ?? "unknown" : "unknown"
  const daemon = () => status()?.ok ? status()!.data?.daemon?.state ?? "unknown" : "unknown"

  return <div class="ozagia-stack">
    <div class="ozagia-head"><div><div class="ozagia-title">Session profile</div>
      <div class="ozagia-sub">Observed Ozagia activity from daemon records</div></div>
      <button class="ozagia-button" type="button" onClick={refresh}>Refresh</button></div>
    <Card title="Graph and daemon" state={daemon()}>
      <Metric label="graph snapshot" value={graph()} />
      <Metric label="daemon" value={daemon()} />
      <Metric label="journal" value={data()?.journal ?? "unknown"} />
      <div class="ozagia-sub">Updated {data()?.observed_at ?? "unknown"}</div>
    </Card>
    <Show when={profile.loading && !data()}><Loading label="Reading observed session activity…" /></Show>
    <Show when={profile() && !profile()!.ok}><FailureView envelope={profile()} onRetry={refresh} /></Show>
    <Show when={profile.error}><FailureView error={profile.error} onRetry={refresh} /></Show>
    <Show when={data()}>
      <Show when={data()!.journal === "absent" && data()!.sessions.length === 0}><Empty>No journal or persisted session counters are available.</Empty></Show>
      <Show when={data()!.sessions.length === 0 && data()!.journal !== "absent"}><Empty>No session-tagged tool activity was found in the bounded journal window.</Empty></Show>
      <For each={data()!.sessions}>{(session) => <Card title={`${session.harness} · ${session.session_id}`}>
        <div class="ozagia-metrics">
          <Metric label="daemon operations" value={String(session.daemon_profile?.operations ?? session.operations)} />
          <Metric label="daemon tool calls observed" value={String(session.daemon_profile?.tool_calls_observed ?? session.tool_calls_observed)} />
          <Metric label="handled" value={String(session.daemon_profile?.handled ?? session.handled)} />
          <Metric label="passthrough" value={String(session.daemon_profile?.passthrough ?? session.passthrough)} />
          <Metric label="expired" value={String(session.daemon_profile?.expired ?? session.expired)} />
          <Metric label="daemon failures" value={String(session.daemon_profile?.failures ?? session.failures)} />
          <Metric label="unknown outcome" value={String(session.daemon_profile?.unknown ?? session.unknown)} />
          <Metric label="errors" value={String(session.daemon_profile?.errors ?? session.errors)} />
          <Metric label="plugin fallbacks" value={String(session.daemon_profile?.plugin_fallbacks ?? session.plugin_fallbacks)} />
          <Metric label="measured duration" value={`${session.daemon_profile?.duration_ms.sum ?? session.duration_ms.sum} ms total / ${session.daemon_profile?.duration_ms.max ?? session.duration_ms.max ?? "unknown"} ms max`} />
        </div>
        <div class="ozagia-sub">Tools: {Object.entries(session.daemon_profile?.tools_by_name ?? session.tools_by_name).map(([name, count]) => `${name} ${count}`).join(" · ") || "none"} · source {session.activity_source ?? "unknown"}</div>
        <Show when={session.daemon_profile?.outcome_counts_incomplete}><div class="ozagia-sub">Older stored outcomes were reclassified as unknown; historical handled/passthrough split is incomplete.</div></Show>
        <Show when={session.host_profile}>
          <div class="ozagia-sub">Host model turns: {session.host_profile!.model_calls}; models: {session.host_profile!.models.join(", ") || "unknown"}</div>
          <div class="ozagia-sub">Tokens input {session.host_profile!.tokens.input ?? "unknown"}; output {session.host_profile!.tokens.output ?? "unknown"}; reasoning {session.host_profile!.tokens.reasoning ?? "unknown"}</div>
          <div class="ozagia-sub">Host model-turn time (summed): {session.host_profile!.timing_ms.model_turn ?? "unknown"} ms; first token (summed): {session.host_profile!.timing_ms.first_token ?? "unknown"} ms</div>
          <div class="ozagia-sub">Host tool outcomes: {Object.entries(session.host_profile!.tools).map(([name, states]) => `${name} ${Object.entries(states).map(([state, stats]) => `${state} ${stats.count ?? "unknown"}`).join("/")}`).join(" · ") || "unknown"}</div>
          <div class="ozagia-sub">Timed host tools: {session.host_profile!.coverage.host_tool_calls_with_timing ?? "unknown"}/{session.host_profile!.coverage.host_tool_calls ?? "unknown"}; missing timing: {session.host_profile!.coverage.host_tool_calls_without_timing ?? "unknown"}</div>
          <div class="ozagia-sub">Timed host tool duration sum: {Object.values(session.host_profile!.tools).flatMap(Object.values).reduce((sum, stats) => sum + (stats.duration_ms_sum ?? 0), 0)} ms (overlaps possible; not critical path)</div>
          <div class="ozagia-sub">Assistant turns with token metadata: {session.host_profile!.coverage.turns_with_tokens ?? "unknown"}/{session.host_profile!.coverage.assistant_turns ?? "unknown"}; turns missing tokens: {session.host_profile!.coverage.turns_without_tokens ?? "unknown"}</div>
        </Show>
        <div class="ozagia-sub">First {session.first_seen ?? "unknown"}; last {session.last_seen ?? "unknown"}</div>
      </Card>}</For>
      <Show when={data()!.truncated}><Badge tone="warn">Journal window truncated; older activity may be missing.</Badge></Show>
      <Card title="Coverage limits">
        <For each={Object.entries(data()!.coverage)}>{([name, explanation]) => <Metric label={name.replaceAll("_", " ")} value={explanation} />}</For>
      </Card>
    </Show>
  </div>
}
