import { isAbsolute, resolve } from "node:path"
import { Plugin } from "@opencode/plugin"
import { Ozagia } from "./rpc.js"
import {
  InputError,
  actionPreviewSpec,
  actionsListSpec,
  aperturesSpec,
  policyCheckSpec,
  researchViewSpec,
  resourcesCheckSpec,
  runOzagia,
  settingsSchemaSpec,
  settingsValuesSpec,
  statusSpec,
  tasksCatalogSpec,
  tasksListSpec,
  tasksShowSpec,
  tasksValidateSpec,
  testingPlanSpec,
  viewSpec,
  worktreeCheckSpec,
  type CommandSpec,
  type RunnerConfig,
} from "./runner.js"
import type { Envelope, EnvelopeError } from "./types.js"

const DEFAULT_PYTHON = "python3"
const DEFAULT_TIMEOUT_MS = 20_000
const MIN_TIMEOUT_MS = 1_000
const MAX_TIMEOUT_MS = 120_000
const MAX_OPTION_TEXT = 4096

function optionText(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined
  const trimmed = value.trim()
  if (!trimmed || trimmed.length > MAX_OPTION_TEXT) return undefined
  return trimmed
}

function optionTimeout(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return DEFAULT_TIMEOUT_MS
  return Math.min(MAX_TIMEOUT_MS, Math.max(MIN_TIMEOUT_MS, Math.trunc(value)))
}

function runnerConfig(options: Record<string, unknown>, directory: string): RunnerConfig {
  const python = optionText(options.python) ?? DEFAULT_PYTHON
  const configuredRoot = optionText(options.root)
  const root = configuredRoot
    ? isAbsolute(configuredRoot)
      ? configuredRoot
      : resolve(directory, configuredRoot)
    : directory

  return {
    python,
    root,
    timeoutMs: optionTimeout(options.timeoutMs),
  }
}

function invalid(error: unknown): Envelope {
  if (error instanceof InputError) {
    const detail: EnvelopeError = { code: error.code, message: error.message }
    return { ok: false, command: "", data: null, error: detail, exitCode: null }
  }
  const detail: EnvelopeError = {
    code: "internal",
    message: error instanceof Error ? error.message : String(error),
  }
  return { ok: false, command: "", data: null, error: detail, exitCode: null }
}

export default Plugin.define({
  id: "ozagia-addin",
  async setup(ctx) {
    const config = runnerConfig(ctx.options, ctx.location.directory)

    const run = async (build: () => CommandSpec, signal?: AbortSignal): Promise<Envelope> => {
      try {
        return await runOzagia(config, build(), signal)
      } catch (error) {
        return invalid(error)
      }
    }

    const registration = await ctx.rpc.register(Ozagia, {
      status: async (_input, context) =>
        run(() => statusSpec(config.root), context.signal),
      apertures: async (_input, context) =>
        run(() => aperturesSpec(config.root), context.signal),
      view: async (input, context) =>
        run(() => viewSpec(config.root, input as { aperture: string }), context.signal),
      worktreeCheck: async (_input, context) =>
        run(() => worktreeCheckSpec(config.root), context.signal),
      policyCheck: async (input, context) =>
        run(() => policyCheckSpec(config.root, input as Record<string, unknown>), context.signal),
      resourcesCheck: async (input, context) =>
        run(() => resourcesCheckSpec(config.root, input as Record<string, unknown>), context.signal),
      testingPlan: async (input, context) =>
        run(() => testingPlanSpec(config.root, input as Record<string, unknown>), context.signal),
      researchView: async (input, context) =>
        run(() => researchViewSpec(config.root, input as Record<string, unknown>), context.signal),
      settingsSchema: async (_input, context) =>
        run(() => settingsSchemaSpec(), context.signal),
      settingsValues: async (_input, context) =>
        run(() => settingsValuesSpec(config.root), context.signal),
      tasksList: async (_input, context) =>
        run(() => tasksListSpec(config.root), context.signal),
      tasksShow: async (input, context) =>
        run(() => tasksShowSpec(config.root, input as Record<string, unknown>), context.signal),
      tasksValidate: async (_input, context) =>
        run(() => tasksValidateSpec(config.root), context.signal),
      tasksCatalog: async (_input, context) =>
        run(() => tasksCatalogSpec(config.root), context.signal),
      actionsList: async (_input, context) =>
        run(() => actionsListSpec(), context.signal),
      actionPreview: async (input, context) =>
        run(() => actionPreviewSpec(config.root, input as Record<string, unknown>), context.signal),
    })

    return async () => {
      await registration.dispose()
    }
  },
})
