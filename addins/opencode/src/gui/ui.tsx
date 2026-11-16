import { Show, type JSX } from "solid-js"
import type { Envelope, EnvelopeError } from "../types"

export type Tone = "ok" | "warn" | "error" | "muted"

export function Badge(props: { tone?: Tone; children: JSX.Element }) {
  return (
    <span class="ozagia-badge" data-tone={props.tone ?? "muted"}>
      {props.children}
    </span>
  )
}

export function Card(props: {
  title: string
  state?: string
  error?: string | null
  children: JSX.Element
}) {
  return (
    <section class="ozagia-card">
      <h3>
        <span>{props.title}</span>
        <Show when={props.state || props.error}>
          <Badge tone={props.error ? "error" : props.state === "ok" ? "ok" : "warn"}>
            {props.error ? "error" : props.state}
          </Badge>
        </Show>
      </h3>
      {props.children}
    </section>
  )
}

export function Loading(props: { label?: string }) {
  return <div class="ozagia-loading">{props.label ?? "Loading…"}</div>
}

export function Empty(props: { children: JSX.Element }) {
  return <div class="ozagia-empty">{props.children}</div>
}

export function Failure(props: {
  error: EnvelopeError
  command?: string
  title?: string
  onRetry?: () => void
}) {
  return (
    <div class="ozagia-failure">
      <b>{props.title ?? "The ozagia CLI did not return a usable report."}</b>
      <div>{props.error.message}</div>
      <Show when={props.error.detail}>
        <div class="ozagia-sub">{props.error.detail}</div>
      </Show>
      <Show when={props.command}>
        <div class="ozagia-sub">
          <code>{props.command}</code>
        </div>
      </Show>
      <Show when={props.onRetry}>
        <div style={{ "margin-top": "6px" }}>
          <button class="ozagia-button" type="button" onClick={() => props.onRetry?.()}>
            Retry
          </button>
        </div>
      </Show>
    </div>
  )
}

/** Show an unsuccessful envelope or an unexpected transport error. */
export function FailureView(props: {
  envelope?: Envelope
  error?: unknown
  onRetry?: () => void
}) {
  if (props.envelope && !props.envelope.ok) {
    return (
      <Failure
        error={props.envelope.error ?? { code: "unknown", message: "No error detail was returned." }}
        command={props.envelope.command}
        onRetry={props.onRetry}
      />
    )
  }
  if (props.error) {
    const message =
      props.error instanceof Error ? props.error.message : String(props.error)
    return (
      <Failure
        error={{ code: "resource", message: message.slice(0, 500) }}
        onRetry={props.onRetry}
      />
    )
  }
  return null
}

export function Metric(props: { label: string; value: JSX.Element | string }) {
  return (
    <div class="ozagia-metric">
      <b>{props.value}</b>
      <span>{props.label}</span>
    </div>
  )
}

export function Meter(props: { pct?: number | null; label?: string }) {
  const pct = () => Math.max(0, Math.min(100, props.pct ?? 0))
  const tone = () => (pct() >= 92 ? "error" : pct() >= 80 ? "warn" : undefined)
  return (
    <div>
      <div class="ozagia-meter" data-tone={tone()}>
        <i style={{ width: `${pct()}%` }} />
      </div>
      <Show when={props.label}>
        <div class="ozagia-note">{props.label}</div>
      </Show>
    </div>
  )
}
