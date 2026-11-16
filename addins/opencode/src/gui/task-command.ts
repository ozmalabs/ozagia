/**
 * Build the display-only command a person can copy from the Tasks tab.
 *
 * This module has no imports on purpose: it is pure, bounded and safe to load
 * in the window process. It never runs the command. The copied command is a
 * `tasks_cli run` preview (`--dry-run`), so even a pasted command validates and
 * previews instead of executing steps. Values are shell-quoted for display.
 */

const INPUT_NAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/

/** Inputs the runner fills from its own context; no `--input` is needed. */
export const AUTOMATIC_TASK_INPUTS = new Set(["repo_root", "run_id", "state_dir"])

const SAFE_SHELL_WORD = /^[A-Za-z0-9_@%+=:,./-]+$/

/** Quote one shell word for display. This is not used to spawn anything. */
export function shellQuote(value: string): string {
  if (SAFE_SHELL_WORD.test(value)) return value
  return `'${value.replace(/'/g, `'\\''`)}'`
}

export interface TaskRunCommandInput {
  /** Absolute path to the task specification returned by `show`. */
  path: string
  /** Repository root returned by `validate`. */
  root: string
  /** Declared input names; automatic names are omitted. */
  inputs?: readonly string[]
}

/**
 * A preview-only `tasks_cli run` command with one `--input NAME=<value>` for
 * each declared, non-automatic input. The placeholder is intentionally a
 * literal the user edits; it is never executed by this add-in.
 */
export function taskRunCommand(input: TaskRunCommandInput): string {
  const args = [
    "python",
    "-m",
    "ozagia.tasks_cli",
    "run",
    shellQuote(input.path),
    "--dry-run",
    "--root",
    shellQuote(input.root),
  ]

  for (const name of input.inputs ?? []) {
    if (!INPUT_NAME_RE.test(name) || AUTOMATIC_TASK_INPUTS.has(name)) continue
    args.push("--input", shellQuote(`${name}=<value>`))
  }

  args.push("--json")
  return args.join(" ")
}
