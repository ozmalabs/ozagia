import { Rpc } from "@opencode/plugin/rpc"
import { ACTION_APPLY_INPUT, ACTION_PREVIEW_INPUT, TASK_RUN_INPUT } from "./schemas.js"

export { ACTION_NAMES } from "./schemas.js"
export type { ActionApplyInput, ActionPreviewInput, TaskRunInput } from "./types.js"

/**
 * The ozagia RPC contract.
 *
 * Every method returns the same envelope:
 *
 *   { ok, command, data, error }
 *
 * `ok` means the ozagia CLI ran and produced a JSON document. It does not mean
 * a worktree, policy or admission check passed: those results live in `data`.
 * `command` is a short, display-only description; free-form arguments such as
 * paths, commit messages and changed-file lists are not echoed back.
 */

const ENVELOPE_OUTPUT = {
  type: "object",
  properties: {
    ok: { type: "boolean" },
    command: { type: "string" },
    data: {},
    error: {},
    exitCode: {},
  },
  required: ["ok", "command", "data", "error"],
  additionalProperties: false,
} as const

const EMPTY_INPUT = {
  type: "object",
  properties: {},
  required: [],
  additionalProperties: false,
} as const

const VIEW_INPUT = {
  type: "object",
  properties: {
    aperture: {
      type: "string",
      minLength: 1,
      maxLength: 64,
      pattern: "^[A-Za-z0-9][A-Za-z0-9._-]*$",
    },
    depth: { type: "integer", minimum: 0, maximum: 32 },
    node: { type: "string", minLength: 1, maxLength: 256 },
    graph: { type: "string", minLength: 1, maxLength: 4096 },
  },
  required: ["aperture"],
  additionalProperties: false,
} as const

const POLICY_INPUT = {
  type: "object",
  properties: {
    kind: { type: "string", enum: ["commit", "merge", "push"] },
    message: { type: "string", maxLength: 1000 },
    target: { type: "string", minLength: 1, maxLength: 256 },
    force: { type: "boolean" },
    tags: { type: "boolean" },
  },
  required: ["kind"],
  additionalProperties: false,
} as const

const RESOURCES_INPUT = {
  type: "object",
  properties: {
    kind: { type: "string", enum: ["job", "task", "worktree"] },
    memoryMb: { type: "number", minimum: 0, maximum: 10000000 },
    diskMb: { type: "number", minimum: 0, maximum: 10000000 },
    path: { type: "string", minLength: 1, maxLength: 4096 },
  },
  additionalProperties: false,
} as const

const TESTING_INPUT = {
  type: "object",
  properties: {
    base: { type: "string", minLength: 1, maxLength: 256 },
    head: { type: "string", minLength: 1, maxLength: 256 },
    task: { type: "string", minLength: 1, maxLength: 256 },
    changed: {
      oneOf: [
        { type: "string", minLength: 1, maxLength: 4096 },
        {
          type: "array",
          items: { type: "string", minLength: 1, maxLength: 4096 },
          maxItems: 500,
        },
      ],
    },
  },
  additionalProperties: false,
} as const

const RESEARCH_INPUT = {
  type: "object",
  properties: {
    investigation: {
      type: "string",
      minLength: 1,
      maxLength: 128,
      pattern: "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    },
    view: {
      type: "string",
      enum: ["library", "fetch", "screening", "evidence", "consensus", "citation"],
    },
  },
  required: ["investigation", "view"],
  additionalProperties: false,
} as const

const TASK_SHOW_INPUT = {
  type: "object",
  properties: {
    id: {
      type: "string",
      minLength: 1,
      maxLength: 64,
      pattern: "^[A-Za-z][A-Za-z0-9_.-]{0,63}$",
    },
  },
  required: ["id"],
  additionalProperties: false,
} as const

export const Ozagia = Rpc.define({
  id: "ozagia",
  methods: {
    status: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    apertures: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    view: { input: VIEW_INPUT, output: ENVELOPE_OUTPUT },
    worktreeCheck: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    policyCheck: { input: POLICY_INPUT, output: ENVELOPE_OUTPUT },
    resourcesCheck: { input: RESOURCES_INPUT, output: ENVELOPE_OUTPUT },
    testingPlan: { input: TESTING_INPUT, output: ENVELOPE_OUTPUT },
    researchView: { input: RESEARCH_INPUT, output: ENVELOPE_OUTPUT },
    settingsSchema: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    settingsValues: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    tasksList: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    tasksShow: { input: TASK_SHOW_INPUT, output: ENVELOPE_OUTPUT },
    tasksValidate: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    tasksCatalog: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    actionsList: { input: EMPTY_INPUT, output: ENVELOPE_OUTPUT },
    actionPreview: { input: ACTION_PREVIEW_INPUT, output: ENVELOPE_OUTPUT },
    actionApply: { input: ACTION_APPLY_INPUT, output: ENVELOPE_OUTPUT },
    taskRun: { input: TASK_RUN_INPUT, output: ENVELOPE_OUTPUT },
  },
  events: {},
})
