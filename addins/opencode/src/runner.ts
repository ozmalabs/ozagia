/**
 * Spawn ozagia's read-only CLIs and turn their JSON into the RPC envelope.
 *
 * This module deliberately imports only Node built-ins: the node:test suite
 * transpiles it directly, without loading any OpenCode package or starting a
 * process. There is no shell, stdin is closed, output is capped and every
 * free-form argument is bounded before it reaches argv.
 */
import { spawn } from "node:child_process"
import { existsSync, realpathSync } from "node:fs"
import * as nodePath from "node:path"
import type { Envelope, EnvelopeError } from "./types.js"

export interface RunnerConfig {
  /** Python executable, for example `python3` or an absolute path. */
  python: string
  /** Repository root passed to every CLI invocation. */
  root: string
  /** Wall-clock limit for one read-only subprocess. */
  timeoutMs: number
  /** Wall-clock limit for a bounded task run. Defaults to 15 minutes. */
  taskTimeoutMs?: number
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
  /**
   * Values that must not appear in the display command or an error envelope.
   * The subprocess args still carry them. Used for confirmation tokens and
   * task input values, which may be credentials.
   */
  sensitive?: readonly string[]
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
const MAX_TASK_ID = 64
const MAX_APERTURE = 64
const MAX_MESSAGE = 1000
const MAX_CHANGED = 500
const MAX_DEPTH = 32
const MAX_MB = 10_000_000
const MAX_ACTION_NAME = 128
const MAX_ACTION_LOG_BYTES = 64 * 1024
const MAX_ACTION_WAIT_S = 600
const MAX_ACTION_IDLE_TIMEOUT_S = 86_400
const MAX_CONFIRMATION = 128
const MIN_CONFIRMATION = 20
const MAX_TASK_INPUTS = 16
const MAX_TASK_INPUT_VALUE = 4096
const MAX_TASK_ARGV_BYTES = 64 * 1024
const DEFAULT_MAX_BUFFER = 8 * 1024 * 1024
const STDERR_TAIL = 64 * 1024
const ERROR_TAIL = 500

/**
 * The local executor's test runner is capped at 900 s. The task RPC uses the
 * same 15-minute ceiling instead of the short read-only timeout.
 */
export const MAX_TASK_TIMEOUT_MS = 15 * 60_000
export const DEFAULT_TASK_TIMEOUT_MS = MAX_TASK_TIMEOUT_MS

const APERTURE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const INVESTIGATION_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/
const TASK_ID_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
const TASK_INPUT_NAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
/** The same alphabet and length as `core.actions._TOKEN`. */
const CONFIRMATION_RE = /^[A-Za-z0-9_-]{20,128}$/
const CONTROL_RE = /[\u0000-\u001f\u007f]/

const RESEARCH_VIEWS = ["library", "fetch", "screening", "evidence", "consensus", "citation"] as const
const POLICY_KINDS = ["commit", "merge", "push"] as const
const RESOURCE_KINDS = ["job", "task", "worktree"] as const
const ACTION_SCOPES = ["project", "user"] as const
const INTEGRATION_NAMES = ["opencode", "claude", "codex", "kiro"] as const

/**
 * The action names this add-in publishes. `core.actions` also lists
 * `integration.install`, `integration.uninstall` and `integration.verify`;
 * those installers remain CLI-only. Keep in step with `ACTION_NAMES` in
 * `schemas.ts` and `core/actions.py`.
 */
export const ACTION_NAMES = [
  "daemon.status",
  "daemon.ensure",
  "daemon.stop",
  "daemon.restart",
  "service.list",
  "service.status",
  "service.start",
  "service.run",
  "service.stop",
  "service.restart",
  "service.logs",
  "tunnel.list",
  "tunnel.status",
  "tunnel.start",
  "integration.status",
] as const

export type ActionName = (typeof ACTION_NAMES)[number]

type ActionArgument = "name" | "live" | "network" | "maxBytes" | "scope" | "wait" | "idleTimeout"

/**
 * Which preview arguments each action accepts. Mirrors
 * `ozagia.core.actions._canonical` so an unsupported flag fails here instead of
 * spawning the CLI only to return an error.
 */
const ACTION_ARGUMENTS: Record<ActionName, readonly ActionArgument[]> = {
  "daemon.status": [],
  "daemon.ensure": ["wait", "idleTimeout"],
  "daemon.stop": [],
  "daemon.restart": ["wait", "idleTimeout"],
  "service.list": [],
  "service.status": ["name"],
  "service.start": ["name"],
  "service.run": ["name"],
  "service.stop": ["name"],
  "service.restart": ["name"],
  "service.logs": ["name", "maxBytes"],
  "tunnel.list": [],
  "tunnel.status": ["name"],
  "tunnel.start": ["name", "live", "network"],
  "integration.status": ["name", "scope"],
}

const ACTIONS_REQUIRING_NAME: ReadonlySet<ActionName> = new Set([
  "service.status",
  "service.start",
  "service.run",
  "service.stop",
  "service.restart",
  "service.logs",
  "tunnel.status",
  "tunnel.start",
])

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

export function optionalInteger(
  value: unknown,
  field: string,
  min: number,
  max: number,
): number | undefined {
  if (value === undefined || value === null) return undefined
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new InputError(`${field} must be an integer`)
  }
  if (value < min || value > max) {
    throw new InputError(`${field} must be between ${min} and ${max}`)
  }
  return value
}

/** An action target label: bounded text without control characters. */
export function actionName(value: unknown, field = "name"): string | undefined {
  const name = optionalText(value, field, MAX_ACTION_NAME)
  if (name === undefined) return undefined
  if (/[\u0000-\u001f\u007f]/.test(name)) {
    throw new InputError(`${field} contains control characters`)
  }
  return name
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

export function settingsSchemaSpec(): CommandSpec {
  return {
    module: "ozagia.settings_cli",
    args: ["schema", "--json"],
    display: "ozagia.settings_cli schema --json",
  }
}

export function settingsValuesSpec(root: string): CommandSpec {
  return {
    module: "ozagia.settings_cli",
    args: ["values", "--root", root, "--json"],
    display: "ozagia.settings_cli values --json",
  }
}

export function tasksListSpec(root: string): CommandSpec {
  return {
    module: "ozagia.task_library_cli",
    args: ["list", "--root", root, "--json"],
    display: "ozagia.task_library_cli list --json",
  }
}

export interface TaskShowInput {
  id?: unknown
}

/** Validate a task-class id before it reaches argv. */
export function taskId(value: unknown): string {
  const id = text(value, "id", MAX_TASK_ID)
  if (!TASK_ID_RE.test(id)) {
    throw new InputError(
      "id must start with a letter and contain only letters, digits, dots, underscores and dashes",
    )
  }
  return id
}

/**
 * Validate a preview confirmation token.
 *
 * This mirrors `ozagia.core.actions._TOKEN` exactly: 20-128 characters from
 * `[A-Za-z0-9_-]`. The token is argv-only; it never enters a display string,
 * log line, error tail or GUI command text.
 */
export function confirmationToken(value: unknown): string {
  if (typeof value !== "string" || !value) {
    throw new InputError("confirm must be the confirmation token from a preview")
  }
  if (value.length > MAX_CONFIRMATION) {
    throw new InputError(`confirm must be at most ${MAX_CONFIRMATION} characters`)
  }
  if (!CONFIRMATION_RE.test(value)) {
    throw new InputError(
      `confirm must be ${MIN_CONFIRMATION}-${MAX_CONFIRMATION} letters, digits, underscores or dashes`,
    )
  }
  return value
}

export function tasksShowSpec(root: string, input: TaskShowInput): CommandSpec {
  const id = taskId(input.id)
  return {
    module: "ozagia.task_library_cli",
    args: ["show", id, "--root", root, "--json"],
    display: "ozagia.task_library_cli show --json",
  }
}

export function tasksValidateSpec(root: string): CommandSpec {
  return {
    module: "ozagia.task_library_cli",
    args: ["validate", "--root", root, "--json"],
    display: "ozagia.task_library_cli validate --json",
  }
}

export function tasksCatalogSpec(root: string): CommandSpec {
  return {
    module: "ozagia.task_library_cli",
    args: ["catalog", "--root", root, "--json"],
    display: "ozagia.task_library_cli catalog --json",
  }
}

export function actionsListSpec(): CommandSpec {
  return {
    module: "ozagia.actions_cli",
    args: ["list", "--json"],
    display: "ozagia.actions_cli list --json",
  }
}

export interface ActionPreviewInput {
  action?: unknown
  name?: unknown
  live?: unknown
  network?: unknown
  maxBytes?: unknown
  scope?: unknown
  wait?: unknown
  idleTimeout?: unknown
}

export interface ActionApplyInput extends ActionPreviewInput {
  confirm?: unknown
}

function supplied(value: unknown): boolean {
  if (value === undefined || value === null) return false
  if (typeof value === "string" && !value.trim()) return false
  return true
}

interface ValidatedActionArguments {
  action: ActionName
  /** Optional action flags, without the subcommand, root or `--json`. */
  args: string[]
}

/**
 * Validate one action's arguments against `ACTION_ARGUMENTS`.
 *
 * Both preview and apply use this, so a caller cannot smuggle a flag that the
 * chosen action does not document.
 */
function validatedActionArguments(input: ActionPreviewInput): ValidatedActionArguments {
  const action = oneOf(input.action, "action", ACTION_NAMES)
  const allowed = ACTION_ARGUMENTS[action]

  const fields: Array<[ActionArgument, unknown]> = [
    ["name", input.name],
    ["live", input.live],
    ["network", input.network],
    ["maxBytes", input.maxBytes],
    ["scope", input.scope],
    ["wait", input.wait],
    ["idleTimeout", input.idleTimeout],
  ]
  for (const [field, value] of fields) {
    if (supplied(value) && !allowed.includes(field)) {
      throw new InputError(`${field} is not supported by ${action}`)
    }
  }

  let name: string | undefined
  if (allowed.includes("name")) {
    name = actionName(input.name)
    if (name === undefined && ACTIONS_REQUIRING_NAME.has(action)) {
      throw new InputError(`name is required for ${action}`)
    }
    if (action === "integration.status" && name !== undefined) {
      name = oneOf(name, "name", INTEGRATION_NAMES)
    }
  }

  const live = allowed.includes("live") ? optionalBoolean(input.live, "live") : undefined
  const network = allowed.includes("network")
    ? optionalBoolean(input.network, "network")
    : undefined
  const maxBytes = allowed.includes("maxBytes")
    ? optionalInteger(input.maxBytes, "maxBytes", 0, MAX_ACTION_LOG_BYTES)
    : undefined
  const scope =
    allowed.includes("scope") && supplied(input.scope)
      ? oneOf(input.scope, "scope", ACTION_SCOPES)
      : undefined
  const wait = allowed.includes("wait")
    ? optionalNumber(input.wait, "wait", MAX_ACTION_WAIT_S)
    : undefined
  const idleTimeout = allowed.includes("idleTimeout")
    ? optionalNumber(input.idleTimeout, "idleTimeout", MAX_ACTION_IDLE_TIMEOUT_S)
    : undefined

  const args: string[] = []
  if (name !== undefined) args.push("--name", name)
  if (live) args.push("--allow-live")
  if (network) args.push("--allow-network")
  if (maxBytes !== undefined) args.push("--max-bytes", String(maxBytes))
  if (scope !== undefined) args.push("--scope", scope)
  if (wait !== undefined) args.push("--wait", String(wait))
  if (idleTimeout !== undefined) args.push("--idle-timeout", String(idleTimeout))
  return { action, args }
}

/**
 * Build a read-only `actions_cli preview` invocation.
 *
 * The argument set is checked against the action's documented arguments before
 * anything is spawned. The one-time confirmation token that preview returns is
 * shown as data; applying it needs the explicit `actionApply` RPC.
 */
export function actionPreviewSpec(root: string, input: ActionPreviewInput): CommandSpec {
  const { action, args } = validatedActionArguments(input)
  return {
    module: "ozagia.actions_cli",
    args: ["preview", action, "--root", root, ...args, "--json"],
    display: `ozagia.actions_cli preview ${action} --json`,
  }
}

/**
 * Build an `actions_cli apply` invocation from a preview token.
 *
 * The actual argv carries the token so the CLI can consume it. The display
 * string only ever shows `<redacted>`, and `sensitive` makes the runner scrub
 * the token from any error tail as well.
 */
export function actionApplySpec(root: string, input: ActionApplyInput): CommandSpec {
  const token = confirmationToken(input.confirm)
  const { action, args } = validatedActionArguments(input)
  return {
    module: "ozagia.actions_cli",
    args: ["apply", action, "--confirm", token, "--root", root, ...args, "--json"],
    display: `ozagia.actions_cli apply ${action} --confirm <redacted> --json`,
    sensitive: [token],
  }
}

export interface TaskRunInput {
  id?: unknown
  inputs?: unknown
}

export interface TaskRunValues {
  id: string
  inputs: Record<string, string>
}

/**
 * Bounded task inputs: at most 16 names, the library's name alphabet, string
 * values up to 4096 characters with no control characters.
 */
export function taskInputs(value: unknown): Record<string, string> {
  if (value === undefined || value === null) return {}
  if (!isRecord(value)) {
    throw new InputError("inputs must be an object mapping names to strings")
  }
  const entries = Object.entries(value)
  if (entries.length > MAX_TASK_INPUTS) {
    throw new InputError(`inputs must contain at most ${MAX_TASK_INPUTS} entries`)
  }
  const out: Record<string, string> = {}
  for (const [name, raw] of entries) {
    if (!TASK_INPUT_NAME_RE.test(name)) {
      throw new InputError(
        "input names must start with a letter and contain only letters, digits, dots, underscores and dashes",
      )
    }
    if (typeof raw !== "string") {
      throw new InputError(`inputs.${name} must be a string`)
    }
    if (raw.length > MAX_TASK_INPUT_VALUE) {
      throw new InputError(`inputs.${name} must be at most ${MAX_TASK_INPUT_VALUE} characters`)
    }
    if (CONTROL_RE.test(raw)) {
      throw new InputError(`inputs.${name} contains control characters`)
    }
    out[name] = raw
  }
  return out
}

/** Validate the `taskRun` request before any CLI is spawned. */
export function taskRunValues(value: unknown): TaskRunValues {
  if (!isRecord(value)) throw new InputError("task run input must be an object")
  return { id: taskId(value.id), inputs: taskInputs(value.inputs) }
}

/** A library spec path, checked before it reaches `tasks_cli run`. */
export function taskSpecPath(value: unknown): string {
  if (typeof value !== "string" || !value) {
    throw new InputError("the task library returned no spec path")
  }
  if (value.length > MAX_PATH) {
    throw new InputError(`the task spec path must be at most ${MAX_PATH} characters`)
  }
  if (CONTROL_RE.test(value)) {
    throw new InputError("the task spec path contains control characters")
  }
  if (!nodePath.isAbsolute(value)) {
    throw new InputError("the task spec path must be absolute")
  }
  return nodePath.normalize(value)
}

function within(base: string, target: string): boolean {
  const relative = nodePath.relative(base, target)
  return (
    relative === "" ||
    (!relative.startsWith(".." + nodePath.sep) && relative !== ".." && !nodePath.isAbsolute(relative))
  )
}

/** Resolve symlinks when the path exists; otherwise keep the lexical path. */
function canonicalPath(value: string): string {
  try {
    return realpathSync(value)
  } catch {
    return value
  }
}

export interface TaskPathResolution {
  path?: string
  error?: string
}

/**
 * Take the spec path from `task_library_cli show` and prove it belongs to the
 * library: a repository override must stay under `<root>/ozagia/tasks` and a
 * built-in must stay under the package's `task_specs` directory. A caller's
 * path is never used.
 */
export function resolveTaskSpecPath(
  root: string,
  id: string,
  data: unknown,
): TaskPathResolution {
  if (!isRecord(data) || !isRecord(data.entry)) {
    return { error: "the task library returned no catalogue entry" }
  }
  const entry = data.entry
  if (entry.id !== id) {
    return { error: "the task library returned a different task id" }
  }
  if (isRecord(data.spec) && data.spec.id !== undefined && data.spec.id !== id) {
    return { error: "the task specification id does not match the requested id" }
  }
  let resolved: string
  try {
    resolved = taskSpecPath(entry.path)
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : "the task library returned an invalid spec path",
    }
  }
  if (nodePath.basename(resolved) !== `${id}.toml`) {
    return { error: "the spec path is not the library file for this task id" }
  }
  const canonical = canonicalPath(resolved)
  if (entry.source === "repo") {
    const base = canonicalPath(nodePath.resolve(root, "ozagia", "tasks"))
    if (!within(base, canonical)) {
      return { error: "the repository task spec is outside ozagia/tasks" }
    }
  } else if (entry.source === "builtin") {
    const segments = canonical.split(/[\\/]+/)
    if (!segments.includes("task_specs")) {
      return { error: "the built-in task spec is outside the package library" }
    }
  } else {
    return { error: "the task library returned an unknown spec source" }
  }
  return { path: canonical }
}

/**
 * Build a `tasks_cli run` invocation for a resolved library spec.
 *
 * The local executor is read-only and `--approve` is never passed, so a
 * mutating step stops as blocked. Input values are argv-only; the display
 * string and error tails show placeholders.
 */
export function taskRunSpec(
  root: string,
  input: TaskRunInput,
  specPath: string,
): CommandSpec {
  const id = taskId(input.id)
  const inputs = taskInputs(input.inputs)
  const path = taskSpecPath(specPath)

  const args = ["run", path, "--executor", "local", "--allow-exec", "--root", root]
  const names = Object.keys(inputs).sort()
  for (const name of names) {
    args.push("--input", `${name}=${inputs[name]}`)
  }
  args.push("--json")

  const total = args.reduce((sum, item) => sum + Buffer.byteLength(item, "utf8"), 0)
  if (total > MAX_TASK_ARGV_BYTES) {
    throw new InputError(`task arguments must be at most ${MAX_TASK_ARGV_BYTES} bytes`)
  }

  const placeholders = names.map((name) => `${name}=<redacted>`).join(" ")
  return {
    module: "ozagia.tasks_cli",
    args,
    display:
      `ozagia.tasks_cli run ${path} --executor local --allow-exec --json` +
      (placeholders ? ` (--input ${placeholders})` : ""),
    sensitive: Object.values(inputs),
  }
}

/** The task RPC timeout: at least one second, never above 15 minutes. */
export function taskRunTimeoutMs(config: RunnerConfig): number {
  const value = config.taskTimeoutMs
  if (typeof value !== "number" || !Number.isFinite(value)) return DEFAULT_TASK_TIMEOUT_MS
  return Math.min(MAX_TASK_TIMEOUT_MS, Math.max(1000, Math.trunc(value)))
}

/**
 * Resolve and run one library task.
 *
 * Resolution is a bounded `task_library_cli show`, and the path comes only from
 * that JSON. A missing, mismatched or out-of-library path is a structured
 * error, never a fallback to a caller-supplied spec. The run uses the local
 * read-only executor; no approve flag, model or network call is added.
 */
export async function runTask(
  config: RunnerConfig,
  inputValue: unknown,
  signal?: AbortSignal,
): Promise<Envelope> {
  const input = taskRunValues(inputValue)
  const show = await runOzagia(config, tasksShowSpec(config.root, { id: input.id }), signal)
  if (!show.ok) return show

  const resolution = resolveTaskSpecPath(config.root, input.id, show.data)
  if (!resolution.path) {
    return {
      ok: false,
      command: `${config.python} -m ozagia.tasks_cli run --json`,
      data: null,
      error: {
        code: "task_path_invalid",
        message: safeTail(resolution.error ?? "the task library returned no usable spec path"),
      },
      exitCode: null,
    }
  }

  const spec = taskRunSpec(config.root, input, resolution.path)
  return runOzagia({ ...config, timeoutMs: taskRunTimeoutMs(config) }, spec, signal)
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

/**
 * Remove exact sensitive values (tokens, input values) from display text.
 *
 * Short values are left alone so a one-character input cannot shred an error
 * message; confirmation tokens are at least 20 characters.
 */
export function redactSensitive(input: string, secrets?: readonly string[]): string {
  if (!secrets?.length) return input
  let out = input
  for (const secret of secrets) {
    if (typeof secret !== "string" || secret.length < 8) continue
    out = out.split(secret).join("[redacted]")
  }
  return out
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
  const secrets = spec.sensitive

  if (!existsSync(config.root)) {
    return Promise.resolve({
      ...failure("root_missing", `root does not exist: ${config.root}`),
      command: redactSensitive(command, secrets),
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
      const sealed: Envelope = {
        ...envelope,
        command: redactSensitive(envelope.command || command, secrets),
      }
      if (envelope.error) {
        sealed.error = {
          ...envelope.error,
          message: redactSensitive(envelope.error.message, secrets),
        }
        if (envelope.error.detail) {
          sealed.error.detail = redactSensitive(envelope.error.detail, secrets)
        }
      }
      resolve(sealed)
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
