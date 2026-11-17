/**
 * JSON schemas for the RPC methods that can change local state.
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
