/**
 * Spawn ozagia's read-only CLIs and turn their JSON into the RPC envelope.
 *
 * This module deliberately imports only Node built-ins: the node:test suite
 * transpiles it directly, without loading any OpenCode package or starting a
 * process. There is no shell, stdin is closed, output is capped and every
 * free-form argument is bounded before it reaches argv.
 */
import { spawn } from "node:child_process"
import { existsSync } from "node:fs"
import type { Envelope, EnvelopeError } from "./types.js"

export interface RunnerConfig {
  /** Python executable, for example `python3` or an absolute path. */
  python: string
  /** Repository root passed to every CLI invocation. */
  root: string
  /** Wall-clock limit for one subprocess. */
  timeoutMs: number
  /** Maximum JSON bytes accepted from stdout. Defaults to 8 MiB. */
  maxBuffer?: number
}

export interface CommandSpec {
  /** Python module run as `python -m <module>`. */
  module: string
  /** Arguments after the module name. */
  args: string[]
  /** Short display string; never includes free-form user input. */
  display: string
}

/** A request that did not match the documented shape. */
export class InputError extends Error {
  readonly code = "invalid_input"

  constructor(message: string) {
    super(message)
    this.name = "InputError"
  }
}

const MAX_ROOT = 4096
const MAX_PYTHON = 4096
const MAX_PATH = 4096
const MAX_ID = 128
const MAX_NODE = 256
const MAX_REF = 256
const MAX_TASK = 256
const MAX_APERTURE = 64
const MAX_MESSAGE = 1000
const MAX_CHANGED = 500
const MAX_DEPTH = 32
const MAX_MB = 10_000_000
const DEFAULT_MAX_BUFFER = 8 * 1024 * 1024
const STDERR_TAIL = 64 * 1024
const ERROR_TAIL = 500

const APERTURE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const INVESTIGATION_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/

const RESEARCH_VIEWS = ["library", "fetch", "screening", "evidence", "consensus", "citation"] as const
const POLICY_KINDS = ["commit", "merge", "push"] as const
const RESOURCE_KINDS = ["job", "task", "worktree"] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/** Require a string of 1..max characters after trimming. */
export function text(value: unknown, field: string, max: number): string {
  if (typeof value !== "string") throw new InputError(`${field} must be a string`)
  const trimmed = value.trim()
  if (!trimmed) throw new InputError(`${field} must not be empty`)
  if (trimmed.length > max) throw new InputError(`${field} must be at most ${max} characters`)
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(trimmed)) {
    throw new InputError(`${field} contains control characters`)
  }
  return trimmed
}

/** An optional string; empty and undefined both mean "not supplied". */
export function optionalText(value: unknown, field: string, max: number): string | undefined {
  if (value === undefined || value === null) return undefined
  if (typeof value === "string" && !value.trim()) return undefined
  return text(value, field, max)
}

export function optionalBoolean(value: unknown, field: string): boolean | undefined {
  if (value === undefined || value === null) return undefined
  if (typeof value !== "boolean") throw new InputError(`${field} must be a boolean`)
  return value
}

export function optionalNumber(value: unknown, field: string, max: number): number | undefined {
  if (value === undefined || value === null) return undefined
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new InputError(`${field} must be a finite number`)
  }
  if (value < 0 || value > max) throw new InputError(`${field} must be between 0 and ${max}`)
  return value
}

export function optionalDepth(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new InputError("depth must be an integer")
  }
  if (value < 0 || value > MAX_DEPTH) {
    throw new InputError(`depth must be between 0 and ${MAX_DEPTH}`)
  }
  return value
}

export function oneOf<T extends string>(
  value: unknown,
  field: string,
  choices: readonly T[],
  fallback?: T,
): T {
  if ((value === undefined || value === null) && fallback !== undefined) return fallback
  if (typeof value !== "string" || !choices.includes(value as T)) {
    throw new InputError(`${field} must be one of: ${choices.join(", ")}`)
  }
  return value as T
}

export function stringList(value: unknown, field: string, maxItems: number): string[] | undefined {
  if (value === undefined || value === null) return undefined
  const raw = typeof value === "string" ? [value] : value
  if (!Array.isArray(raw)) throw new InputError(`${field} must be a string or an array of strings`)
  if (raw.length > maxItems) throw new InputError(`${field} must contain at most ${maxItems} entries`)
  return raw.map((item, index) => text(item, `${field}[${index}]`, MAX_PATH))
}

// ---------------------------------------------------------------------------
// Pure argument builders
// ---------------------------------------------------------------------------

export function statusSpec(root: string): CommandSpec {
  return {
    module: "ozagia.status_cli",
    args: ["--root", root, "--json"],
    display: "ozagia.status_cli --json",
  }
}

export function aperturesSpec(root: string): CommandSpec {
  return {
    module: "ozagia.view_cli",
    args: ["--list-apertures", "--format", "json", "--root", root],
    display: "ozagia.view_cli --list-apertures --format json",
  }
}

export interface ViewInput {
  aperture: string
  depth?: number
  node?: string
  graph?: string
}

export function viewSpec(root: string, input: ViewInput): CommandSpec {
  const aperture = text(input.aperture, "aperture", MAX_APERTURE)
  if (!APERTURE_RE.test(aperture)) {
    throw new InputError("aperture must contain only letters, digits, dots, underscores and dashes")
  }
  const depth = optionalDepth(input.depth)
  const node = optionalText(input.node, "node", MAX_NODE)
  const graph = optionalText(input.graph, "graph", MAX_PATH)

  const args = ["--aperture", aperture, "--format", "json", "--root", root]
  if (depth !== undefined) args.push("--depth", String(depth))
  if (node !== undefined) args.push("--node", node)
  if (graph !== undefined) args.push("--graph", graph)

  return {
    module: "ozagia.view_cli",
    args,
    display: `ozagia.view_cli --aperture ${aperture} --format json`,
  }
}

export function worktreeCheckSpec(root: string): CommandSpec {
  return {
    module: "ozagia.worktree_cli",
    args: ["check", "--root", root, "--json"],
    display: "ozagia.worktree_cli check --json",
  }
}

export interface PolicyInput {
  kind?: unknown
  message?: unknown
  target?: unknown
  force?: unknown
  tags?: unknown
}

export function policyCheckSpec(root: string, input: PolicyInput): CommandSpec {
  const kind = oneOf(input.kind, "kind", POLICY_KINDS)
  const message = optionalText(input.message, "message", MAX_MESSAGE)
  const target = optionalText(input.target, "target", MAX_REF)
  const force = optionalBoolean(input.force, "force")
  const tags = optionalBoolean(input.tags, "tags")

  const args = ["policy", "--for", kind, "--root", root, "--json"]
  if (message !== undefined) args.push("--message", message)
  if (target !== undefined) args.push("--target", target)
  if (force) args.push("--force")
  if (tags) args.push("--tags")

  return {
    module: "ozagia.worktree_cli",
    args,
    display: `ozagia.worktree_cli policy --for ${kind} --json`,
  }
}

export interface ResourcesInput {
  kind?: unknown
  memoryMb?: unknown
  diskMb?: unknown
  path?: unknown
}

export function resourcesCheckSpec(root: string, input: ResourcesInput): CommandSpec {
  const kind = oneOf(input.kind, "kind", RESOURCE_KINDS, "job")
  const memoryMb = optionalNumber(input.memoryMb, "memoryMb", MAX_MB)
  const diskMb = optionalNumber(input.diskMb, "diskMb", MAX_MB)
  const path = optionalText(input.path, "path", MAX_PATH)

  const args = ["check", "--for", kind, "--root", root, "--json"]
  if (memoryMb !== undefined) args.push("--memory-mb", String(memoryMb))
  if (diskMb !== undefined) args.push("--disk-mb", String(diskMb))
  if (path !== undefined) args.push("--path", path)

  return {
    module: "ozagia.resources_cli",
    args,
    display: `ozagia.resources_cli check --for ${kind} --json`,
  }
}

export interface TestingInput {
  base?: unknown
  head?: unknown
  task?: unknown
  changed?: unknown
}

export function testingPlanSpec(root: string, input: TestingInput): CommandSpec {
  const base = optionalText(input.base, "base", MAX_REF)
  const head = optionalText(input.head, "head", MAX_REF)
  const task = optionalText(input.task, "task", MAX_TASK)
  const changed = stringList(input.changed, "changed", MAX_CHANGED)

  const args = ["plan", "--root", root, "--json"]
  if (base !== undefined) args.push("--base", base)
  if (head !== undefined) args.push("--head", head)
  if (task !== undefined) args.push("--task", task)
  for (const path of changed ?? []) args.push("--changed", path)

  return {
    module: "ozagia.testing_cli",
    args,
    display: "ozagia.testing_cli plan --json",
  }
}

export interface ResearchInput {
  investigation?: unknown
  view?: unknown
}

export function researchViewSpec(root: string, input: ResearchInput): CommandSpec {
  const investigation = text(input.investigation, "investigation", MAX_ID)
  if (!INVESTIGATION_RE.test(investigation)) {
    throw new InputError("investigation must be a valid research investigation id")
  }
  const view = oneOf(input.view, "view", RESEARCH_VIEWS)

  return {
    module: "ozagia.research_ui_cli",
    args: ["--root", root, "--investigation", investigation, "--view", view, "--format", "json"],
    display: `ozagia.research_ui_cli --investigation ${investigation} --view ${view} --format json`,
  }
}

// ---------------------------------------------------------------------------
// Process execution and error reporting
// ---------------------------------------------------------------------------

/** Replace obvious credential shapes before any text reaches the UI. */
export function redact(input: string): string {
  return input
    .replace(/github_pat_[A-Za-z0-9_]{20,}/g, "[redacted]")
    .replace(/gh[pousr]_[A-Za-z0-9]{20,}/g, "[redacted]")
    .replace(/sk-[A-Za-z0-9_-]{16,}/g, "[redacted]")
    .replace(/xox[baprs]-[A-Za-z0-9-]{10,}/g, "[redacted]")
    .replace(/npm_[A-Za-z0-9]{20,}/g, "[redacted]")
    .replace(/pypi-[A-Za-z0-9_-]{20,}/g, "[redacted]")
    .replace(/AKIA[0-9A-Z]{16}/g, "[redacted]")
    .replace(/AIza[0-9A-Za-z_-]{35}/g, "[redacted]")
    .replace(/(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[redacted]")
    .replace(
      /((?:token|secret|password|passwd|api[_-]?key)\s*[:=]\s*)[^\s,;]+/gi,
      "$1[redacted]",
    )
    .replace(/(\/\/)[^/\s:@]+:[^/\s@]+@/g, "$1[redacted]@")
}

/** Collapse whitespace, redact and cap the tail of a diagnostic string. */
export function safeTail(input: string, max = ERROR_TAIL): string {
  const normal = redact(input).replace(/\s+/g, " ").trim()
  if (normal.length <= max) return normal
  return `…${normal.slice(-max)}`
}

function failure(
  code: string,
  message: string,
  detail?: string,
  exitCode?: number | null,
): Envelope {
  const error: EnvelopeError = { code, message: safeTail(message) }
  if (detail) error.detail = safeTail(detail)
  return { ok: false, command: "", data: null, error, exitCode: exitCode ?? null }
}

/**
 * Run one ozagia command. Exit status 0 and 1 are both successful executions:
 * the CLIs use 1 for a report that contains errors or a refusal while still
 * emitting valid JSON. Anything else is an execution error.
 */
export function runOzagia(
  config: RunnerConfig,
  spec: CommandSpec,
  signal?: AbortSignal,
): Promise<Envelope> {
  const command = `${config.python} -m ${spec.display}`
  const maxBuffer = config.maxBuffer ?? DEFAULT_MAX_BUFFER

  if (!existsSync(config.root)) {
    return Promise.resolve({
      ...failure("root_missing", `root does not exist: ${config.root}`),
      command,
    })
  }

  return new Promise<Envelope>((resolve) => {
    let settled = false
    let stdout = ""
    let stdoutBytes = 0
    let stderr = ""
    let timedOut = false
    let tooLarge = false
    let aborted = false
    let child: ReturnType<typeof spawn>

    const finish = (envelope: Envelope) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener("abort", onAbort)
      resolve({ ...envelope, command: envelope.command || command })
    }

    const stop = (reason: "timeout" | "too_large" | "abort") => {
      if (reason === "timeout") timedOut = true
      if (reason === "too_large") tooLarge = true
      if (reason === "abort") aborted = true
      try {
        child.kill("SIGKILL")
      } catch {
        // The process may already have exited.
      }
    }

    const onAbort = () => stop("abort")
    const timer = setTimeout(() => stop("timeout"), Math.max(1, config.timeoutMs))
    if (signal) {
      if (signal.aborted) {
        // Defer the kill until the child exists.
        timedOut = false
        aborted = true
      } else {
        signal.addEventListener("abort", onAbort, { once: true })
      }
    }

    try {
      child = spawn(config.python, ["-m", spec.module, ...spec.args], {
        cwd: config.root,
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
        env: process.env,
      })
    } catch (error) {
      finish(failure("spawn_failed", error instanceof Error ? error.message : String(error)))
      return
    }

    if (aborted) stop("abort")

    child.stdout?.setEncoding("utf8")
    child.stderr?.setEncoding("utf8")

    child.stdout?.on("data", (chunk: string) => {
      stdoutBytes += Buffer.byteLength(chunk, "utf8")
      if (stdoutBytes > maxBuffer) {
        tooLarge = true
        stop("too_large")
        return
      }
      stdout += chunk
    })

    child.stderr?.on("data", (chunk: string) => {
      stderr += chunk
      if (stderr.length > STDERR_TAIL * 2) stderr = stderr.slice(-STDERR_TAIL)
    })

    child.on("error", (error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") {
        finish(
          failure(
            "python_missing",
            `python executable not found: ${config.python}. Set the plugin option "python".`,
          ),
        )
      } else {
        finish(failure("spawn_failed", error.message))
      }
    })

    child.on("close", (code, signalName) => {
      if (timedOut) {
        finish(failure("timeout", `ozagia CLI exceeded the ${config.timeoutMs} ms timeout`, stderr, code))
        return
      }
      if (tooLarge) {
        finish(failure("output_too_large", `ozagia CLI output exceeded ${maxBuffer} bytes`, stderr, code))
        return
      }
      if (aborted) {
        finish(failure("cancelled", "ozagia CLI call was cancelled", stderr, code))
        return
      }
      if (code !== 0 && code !== 1) {
        const detail = stderr || (stdout ? safeTail(stdout) : "")
        finish(
          failure(
            "exit_status",
            `ozagia CLI exited with status ${code ?? "null"}${signalName ? ` (${signalName})` : ""}`,
            detail,
            code,
          ),
        )
        return
      }

      const text = stdout.trim()
      if (!text) {
        finish(failure("no_output", "ozagia CLI produced no JSON output", stderr, code))
        return
      }

      let data: unknown
      try {
        data = JSON.parse(text)
      } catch (error) {
        finish(
          failure(
            "invalid_json",
            error instanceof Error ? error.message : "ozagia CLI output was not valid JSON",
            stderr || stdout,
            code,
          ),
        )
        return
      }

      finish({ ok: true, command, data, error: null, exitCode: code })
    })
  })
}
