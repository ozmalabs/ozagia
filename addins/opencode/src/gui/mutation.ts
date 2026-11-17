/**
 * Display and confirmation helpers for the two mutating add-in surfaces.
 *
 * This module has no imports on purpose: the offline `node:test` suite loads
 * it directly, and the window process uses it for redacted copy text and
 * button gating. It never holds a token from anywhere except the preview
 * result it is given, and it never builds apply text that includes one.
 */

/** Mirrors `core.actions._TOKEN`. */
export const ACTION_TOKEN_RE = /^[A-Za-z0-9_-]{20,128}$/

export interface ApplyPreview {
  confirmation?: string | null
  applicable?: boolean
}

/**
 * Display-only apply command. The token is deliberately `<redacted>`; the RPC
 * carries it in argv and never in this string.
 */
export function redactedApplyCommand(action: string): string {
  return `python -m ozagia.actions_cli apply ${action} --confirm <redacted> --json`
}

/**
 * Whether the explicit Apply control may run.
 *
 * A preview result with a bounded token is required, the person must have
 * checked the confirmation box, and no other call may be in flight. Preview
 * alone never satisfies this predicate.
 */
export function canApplyAction(
  preview: ApplyPreview | null,
  confirmed: boolean,
  busy: boolean,
): boolean {
  if (busy || !confirmed || !preview) return false
  return typeof preview.confirmation === "string" && ACTION_TOKEN_RE.test(preview.confirmation)
}
