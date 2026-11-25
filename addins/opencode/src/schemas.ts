/**
 * JSON schemas for the RPC methods whose inputs need explicit bounds.
 *
 * Kept dependency-free so the offline `node:test` suite can load this module
 * without the GUI host or a build. `rpc.ts` composes these into `Rpc.define`;
 * the handlers repeat every bound in `runner.ts`, so a caller that bypasses
 * the host validator still cannot build an unbounded argv.
 */

/**
 * Action names the add-in publishes. `core.actions` also lists the three
 * integration installer actions; they remain CLI-only. Keep in step with
 * `ACTION_NAMES` in `runner.ts`.
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

/** The bounded arguments shared by `actionPreview` and `actionApply`. */
export const ACTION_ARGUMENT_PROPERTIES = {
  action: { type: "string", enum: ACTION_NAMES },
  name: { type: "string", minLength: 1, maxLength: 128 },
  live: { type: "boolean" },
  network: { type: "boolean" },
  maxBytes: { type: "integer", minimum: 0, maximum: 65536 },
  scope: { type: "string", enum: ["project", "user"] },
  wait: { type: "number", minimum: 0, maximum: 600 },
  idleTimeout: { type: "number", minimum: 0, maximum: 86400 },
} as const

export const ACTION_PREVIEW_INPUT = {
  type: "object",
  properties: ACTION_ARGUMENT_PROPERTIES,
  required: ["action"],
  additionalProperties: false,
} as const

export const ACTION_APPLY_INPUT = {
  type: "object",
  properties: {
    ...ACTION_ARGUMENT_PROPERTIES,
    // Mirrors `core.actions._TOKEN`: [A-Za-z0-9_-]{20,128}.
    confirm: {
      type: "string",
      minLength: 20,
      maxLength: 128,
      pattern: "^[A-Za-z0-9_-]{20,128}$",
    },
  },
  required: ["action", "confirm"],
  additionalProperties: false,
} as const

export const TASK_RUN_INPUT = {
  type: "object",
  properties: {
    id: {
      type: "string",
      minLength: 1,
      maxLength: 64,
      pattern: "^[A-Za-z][A-Za-z0-9_.-]{0,63}$",
    },
    inputs: {
      type: "object",
      maxProperties: 16,
      propertyNames: { pattern: "^[A-Za-z][A-Za-z0-9_.-]{0,63}$" },
      additionalProperties: {
        type: "string",
        maxLength: 4096,
        pattern: "^[^\\u0000-\\u001f\\u007f]*$",
      },
    },
  },
  required: ["id"],
  additionalProperties: false,
} as const

/**
 * Session secret request inputs.
 *
 * `harness` is deliberately absent: the handler fixes it to `opencode`. The
 * value is a request body field for `secretsFulfill` (the runner moves it to
 * stdin), but no response ever echoes it. `additionalProperties: false`
 * rejects an extra `harness` or an unexpected field before the handler runs.
 */
const SECRET_HANDLE = {
  type: "string",
  minLength: 1,
  maxLength: 128,
  pattern: "^[A-Za-z0-9._:-]+$",
} as const

export const SECRET_SESSION_INPUT = {
  type: "object",
  properties: {
    session: SECRET_HANDLE,
  },
  required: ["session"],
  additionalProperties: false,
} as const

export const SECRET_FULFILL_INPUT = {
  type: "object",
  properties: {
    session: SECRET_HANDLE,
    requestId: SECRET_HANDLE,
    ttlS: { type: "integer", minimum: 1, maximum: 86400 },
    // The runner re-checks UTF-8 bytes (1..8192) before spawning.
    value: { type: "string", minLength: 1, maxLength: 8192 },
  },
  required: ["session", "requestId", "value"],
  additionalProperties: false,
} as const

export const SECRET_DECLINE_INPUT = {
  type: "object",
  properties: {
    session: SECRET_HANDLE,
    requestId: SECRET_HANDLE,
  },
  required: ["session", "requestId"],
  additionalProperties: false,
} as const

/**
 * Toolchain discovery is read-only: `list`, `env` and `plan` only. `plan`
 * bounds the repeated `--capability`/`--path` flags and accepts the CLI's two
 * probe switches. There is no `--model-family` flag and no install method.
 */
export const TOOLCHAIN_ENV_INPUT = {
  type: "object",
  properties: {
    probeSudo: { type: "boolean" },
  },
  additionalProperties: false,
} as const

export const TOOLCHAIN_PLAN_INPUT = {
  type: "object",
  properties: {
    capabilities: {
      oneOf: [
        {
          type: "string",
          minLength: 1,
          maxLength: 32,
          pattern: "^[a-z][a-z0-9_-]{0,31}$",
        },
        {
          type: "array",
          items: {
            type: "string",
            minLength: 1,
            maxLength: 32,
            pattern: "^[a-z][a-z0-9_-]{0,31}$",
          },
          maxItems: 16,
        },
      ],
    },
    paths: {
      oneOf: [
        {
          type: "string",
          minLength: 1,
          maxLength: 2000,
          pattern: "^[^\\u0000-\\u001f\\u007f]*$",
        },
        {
          type: "array",
          items: {
            type: "string",
            minLength: 1,
            maxLength: 2000,
            pattern: "^[^\\u0000-\\u001f\\u007f]*$",
          },
          maxItems: 32,
        },
      ],
    },
    probeVersions: { type: "boolean" },
    probeSudo: { type: "boolean" },
  },
  additionalProperties: false,
} as const
