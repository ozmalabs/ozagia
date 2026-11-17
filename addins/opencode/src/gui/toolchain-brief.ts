/**
 * Pure helpers for the read-only Tools tab.
 *
 * This module mirrors the useful parts of `ozagia.core.toolchain`'s
 * `brief_lines` and the input bounds in `runner.ts`, without importing the GUI
 * host or spawning anything. The `node:test` suite loads it directly.
 */
import type {
  ToolchainCapabilityReport,
  ToolchainEnvironment,
  ToolchainInstall,
  ToolchainPlan,
  ToolchainTool,
} from "../types"

export const MAX_TOOLCHAIN_CAPABILITIES = 16
export const MAX_TOOLCHAIN_PATHS = 32
export const MAX_TOOLCHAIN_PATH = 2000
export const MAX_BRIEF_CAPABILITIES = 8

const CAPABILITY_RE = /^[a-z][a-z0-9_-]{0,31}$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/

/** Model families with registry `native_for`/`preferred_by` metadata. */
export const TOOLCHAIN_MODEL_FAMILIES = [
  { id: "any", label: "No family preference" },
  { id: "claude", label: "Claude Code" },
  { id: "codex", label: "OpenAI Codex / GPT-5" },
  { id: "gemini", label: "Gemini CLI" },
  { id: "cursor", label: "Cursor" },
  { id: "aider", label: "Aider" },
  { id: "openhands", label: "OpenHands" },
  { id: "local", label: "Weaker / local models" },
] as const

const FAMILY_ALIASES: Record<string, string> = {
  "claude-code": "claude",
  anthropic: "claude",
  gpt: "codex",
  "gpt-5": "codex",
  openai: "codex",
  "gemini-cli": "gemini",
  llama: "local",
  qwen: "local",
}

function clip(value: string, max = 40): string {
  return value.length > max ? `${value.slice(0, max)}…` : value
}

/** Split comma/space separated capability ids, lower-case and de-duplicate. */
export function splitToolchainCapabilities(value: string): string[] {
  const parts = value
    .split(/[\s,]+/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
  return [...new Set(parts)]
}

/** Split newline/comma separated paths and de-duplicate; spaces stay part of a path. */
export function splitToolchainPaths(value: string): string[] {
  const parts = value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  return [...new Set(parts)]
}

/**
 * Validate the form's capability/path lists locally.
 *
 * The RPC and `runner.ts` repeat these bounds. This copy only explains a bad
 * form before a call; it is not a security boundary.
 */
export function toolchainInputIssue(
  capabilities: readonly string[],
  paths: readonly string[],
): string | null {
  if (capabilities.length > MAX_TOOLCHAIN_CAPABILITIES) {
    return `At most ${MAX_TOOLCHAIN_CAPABILITIES} capabilities are accepted; ${capabilities.length} were given.`
  }
  for (const id of capabilities) {
    if (!CAPABILITY_RE.test(id)) {
      return `Capability "${clip(id)}" must be lower-case letters, digits, underscores or dashes (at most 32 characters), such as pdf or documents.`
    }
  }
  if (paths.length > MAX_TOOLCHAIN_PATHS) {
    return `At most ${MAX_TOOLCHAIN_PATHS} paths are accepted; ${paths.length} were given.`
  }
  for (const path of paths) {
    if (!path) return "A path is empty."
    if (path.length > MAX_TOOLCHAIN_PATH) {
      return `Path "${clip(path)}" is longer than ${MAX_TOOLCHAIN_PATH} characters.`
    }
    if (CONTROL_RE.test(path)) {
      return `Path "${clip(path)}" contains control characters.`
    }
  }
  return null
}

export type ToolchainCapabilityState =
  | "ready"
  | "missing"
  | "blocked"
  | "remote"
  | "unprobed"

/** The primary state a capability card shows; counts carry the rest. */
export function capabilityState(report: ToolchainCapabilityReport): ToolchainCapabilityState {
  if (report.ready) return "ready"
  if ((report.missing ?? []).length > 0) return "missing"
  if (report.blocked) return "blocked"
  if ((report.remote ?? []).length > 0) return "remote"
  if ((report.unprobed ?? []).length > 0) return "unprobed"
  return "missing"
}

export function capabilityTone(
  state: ToolchainCapabilityState,
): "ok" | "warn" | "error" | "muted" {
  if (state === "ready") return "ok"
  if (state === "missing") return "warn"
  if (state === "blocked") return "error"
  return "muted"
}

/** A short `provides` list with a bounded `+N more` suffix. */
export function formatProvides(tool: ToolchainTool, limit = 4): string {
  const values = (tool.provides ?? []).filter(Boolean)
  const text = values.slice(0, limit).join(", ")
  if (values.length > limit) return `${text}, +${values.length - limit} more`
  return text || "—"
}

export function installKindLabel(item: ToolchainInstall): string {
  const kind = item.install_kind ?? ""
  if (kind === "python-extra") {
    return `Python extra ${item.extra ?? ""}`.trim()
  }
  if (kind === "python-package") return "Python package"
  if (kind === "npm-package") return "npm package"
  if (kind === "system-package") return "system package"
  return "toolchain package"
}

/** The first `limit` install previews in the plan's deterministic order. */
export function installPreviewRows(
  report: ToolchainCapabilityReport,
  limit = 8,
): ToolchainInstall[] {
  return (report.install ?? []).slice(0, Math.max(0, limit))
}

export function toolKindLabel(tool: ToolchainTool): string {
  if (tool.install_kind === "python-extra") {
    return `Python extra ${tool.extra ?? ""}`.trim()
  }
  if (tool.kind === "python") return "Python library"
  if (tool.kind === "node") return "Node library"
  return "system package"
}

export function sudoStateText(environment?: ToolchainEnvironment): string {
  const sudo = environment?.sudo
  if (!sudo?.available) return "sudo missing"
  if (!sudo.probed) return "sudo not probed"
  return sudo.noninteractive ? "sudo usable" : "sudo not usable non-interactively"
}

export function approvalStateText(environment?: ToolchainEnvironment): string {
  const approval = environment?.approval
  return (
    "install " + (approval?.allow_install ? "approved" : "not approved") +
    ", network " + (approval?.allow_network ? "approved" : "not approved") +
    ", sudo " + (approval?.allow_sudo ? "approved" : "not approved")
  )
}

/** The one-line environment summary used by the brief. */
export function toolchainEnvironmentLine(environment?: ToolchainEnvironment): string {
  const platform = environment?.platform ?? {}
  const distro = platform.distro ?? {}
  let label = platform.system ?? "unknown"
  if (platform.arch) label += ` ${platform.arch}`
  const pretty = distro.pretty_name ?? distro.name
  if (distro.id) {
    label += pretty
      ? `, ${pretty} (id ${distro.id})`
      : `, id ${distro.id}`
  }
  const managers = Object.keys(environment?.managers ?? {}).sort()
  let managerText = managers.length ? managers.slice(0, 8).join(", ") : "none detected"
  if (managers.length > 8) managerText += `, +${managers.length - 8} more`
  return (
    `${label}; managers: ${managerText}; ${sudoStateText(environment)}; ` +
    approvalStateText(environment)
  )
}

function normalizeFamily(value?: string): string | undefined {
  if (!value) return undefined
  const trimmed = value.trim().toLowerCase()
  if (!trimmed || trimmed === "any") return undefined
  return FAMILY_ALIASES[trimmed] ?? trimmed
}

function briefTool(
  report: ToolchainCapabilityReport,
  family?: string,
): ToolchainTool | undefined {
  const usable = (report.available ?? []).filter((tool) => tool.usable !== false)
  if (!usable.length) return undefined
  if (family) {
    const match = usable.find((tool) =>
      [...(tool.native_for ?? []), ...(tool.preferred_by ?? [])].some(
        (name) => normalizeFamily(name) === family,
      ),
    )
    if (match) return match
  }
  return usable.find((tool) => tool.recommended) ?? usable[0]
}

/**
 * A compact, model-facing brief rendered from one plan.
 *
 * `family` is a local metadata hint, not a CLI flag: the CLI has no
 * `--model-family` option. Installation is never performed here, and the
 * brief says so.
 */
export function toolchainBrief(
  plan: ToolchainPlan | null | undefined,
  family?: string,
): string {
  if (!plan) return ""
  const environment = plan.environment
  const lines = [`[ozagia] tools: ${toolchainEnvironmentLine(environment)}`]
  const reports = plan.capabilities ?? []
  const footer = [
    "keep the provider count small: extra tools and MCP servers cost context and accuracy",
    "not checked: versions, remote credentials/network and installation; nothing was installed, and every install preview is would_run: false",
  ]

  if (!reports.length) {
    lines.push(
      "no external-tool capability requested; name a capability such as pdf or add a path",
    )
    lines.push(...footer)
    return lines.join("\n")
  }

  const wantedFamily = normalizeFamily(family)
  for (const report of reports.slice(0, MAX_BRIEF_CAPABILITIES)) {
    const available = report.available ?? []
    const remote = report.remote ?? []
    const unprobed = report.unprobed ?? []

    if (report.ready) {
      const chosen = briefTool(report, wantedFamily)
      if (!chosen) continue
      const label = wantedFamily ? `for ${wantedFamily} prefer ` : "prefer "
      const useful = `${chosen.id} (${toolKindLabel(chosen)}: ${formatProvides(chosen, 3)})`
      const others = available
        .filter((tool) => tool !== chosen && tool.usable !== false)
        .slice(0, 3)
        .map((tool) => tool.id)
      let line = `${report.id}: ready; ${label}${useful}`
      if (others.length) line += `; also ${others.join(", ")}`
      lines.push(line)
      continue
    }

    const missingItems = (report.missing ?? []).map((tool) => tool.id)
    let missing = missingItems.slice(0, 5).join(", ")
    if (missingItems.length > 5) missing += `, +${missingItems.length - 5} more`

    const unusable = available.filter((tool) => tool.usable === false)
    const unusableText = unusable.length
      ? "installed but unusable: " +
        unusable
          .slice(0, 2)
          .map(
            (tool) =>
              `${tool.id} (needs ${(tool.missing_requirements ?? []).join(", ") || "a dependency"})`,
          )
          .join("; ") +
        "; "
      : ""

    const remoteText = remote
      .slice(0, 3)
      .map(
        (tool) =>
          `${tool.id} (${tool.credentialed ? "network/credentials" : "network"})`,
      )
      .join(", ")

    if (!missingItems.length && (remote.length || unprobed.length)) {
      const kind = remote.length ? "remote only" : "unprobed"
      const providers = remote.length
        ? remoteText
        : unprobed.slice(0, 3).map((tool) => tool.id).join(", ")
      lines.push(`${report.id}: ${kind}; ${unusableText}${providers}`)
      continue
    }

    const approved = environment?.approval?.allow_install === true
    const missingText = missing || "no local provider"
    if (!approved) {
      const path = report.recommended_install
        ? `; recommended ${installKindLabel(report.recommended_install)}`
        : ""
      lines.push(
        `${report.id}: missing; ${unusableText}${missingText}${path}; ` +
          "installation not approved",
      )
      if (remoteText) lines.push(`  remote providers: ${remoteText}`)
      continue
    }

    const chosen =
      report.recommended_install ??
      (report.install ?? []).find((item) => !(item.blocked_by ?? []).length)
    if (!chosen) {
      lines.push(
        `${report.id}: missing; ${unusableText}${missingText}; no install option`,
      )
      continue
    }
    let line =
      `${report.id}: missing; ${unusableText}${missingText}; install ` +
      `${chosen.tool ?? chosen.package ?? "a provider"} with ` +
      `${chosen.command ?? "<no command>"} (${installKindLabel(chosen)})`
    if ((chosen.blocked_reasons ?? []).length) {
      line += ` (blocked: ${(chosen.blocked_reasons ?? []).join("; ")})`
    }
    lines.push(line)
  }

  if (reports.length > MAX_BRIEF_CAPABILITIES) {
    lines.push(
      `... and ${reports.length - MAX_BRIEF_CAPABILITIES} more capability reports`,
    )
  }
  lines.push(...footer)
  return lines.join("\n")
}
