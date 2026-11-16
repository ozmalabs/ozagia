import type { Context } from "@opencode/plugin/gui"
import type { Envelope } from "../types"

/** Call an RPC and normalise a rejected call into the same envelope shape. */
function describeError(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === "object" && error !== null) {
    const candidate = error as { message?: unknown; type?: unknown; data?: unknown }
    if (typeof candidate.message === "string" && candidate.message) {
      const kind = typeof candidate.type === "string" && candidate.type ? `${candidate.type}: ` : ""
      return `${kind}${candidate.message}`
    }
    if (typeof candidate.data === "string" && candidate.data) return candidate.data
  }
  return String(error)
}

export async function invoke<T>(
  _ctx: Context,
  call: () => Promise<unknown>,
): Promise<Envelope<T>> {
  try {
    return (await call()) as Envelope<T>
  } catch (error) {
    return {
      ok: false,
      command: "",
      data: null,
      error: { code: "rpc", message: describeError(error).slice(0, 500) },
      exitCode: null,
    }
  }
}

export function failureText(envelope: Envelope): string {
  const error = envelope.error
  if (!error) return "The ozagia CLI returned no data."
  return error.detail ? `${error.message} — ${error.detail}` : error.message
}
