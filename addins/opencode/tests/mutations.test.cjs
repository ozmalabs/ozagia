"use strict"

/**
 * Offline tests for the two mutating add-in surfaces.
 *
 * Everything here loads dependency-free TypeScript with `load-ts.cjs` and uses
 * a fake `python` executable written to a temp directory. No network, model,
 * real ozagia CLI or OpenCode package is involved.
 */

const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const { after, test } = require("node:test")
const { loadTs } = require("./load-ts.cjs")

const runner = loadTs(path.join(__dirname, "..", "src", "runner.ts"))
const taskCommand = loadTs(path.join(__dirname, "..", "src", "gui", "task-command.ts"))
const mutation = loadTs(path.join(__dirname, "..", "src", "gui", "mutation.ts"))
const schemas = loadTs(path.join(__dirname, "..", "src", "schemas.ts"))

const {
  ACTION_NAMES,
  DEFAULT_TASK_TIMEOUT_MS,
  InputError,
  MAX_TASK_TIMEOUT_MS,
  actionApplySpec,
  actionPreviewSpec,
  confirmationToken,
  redactSensitive,
  resolveTaskSpecPath,
  runOzagia,
  runTask,
  taskInputs,
  taskRunSpec,
  taskRunTimeoutMs,
  taskRunValues,
  taskSpecPath,
} = runner
const {
  AUTOMATIC_TASK_INPUTS,
  MAX_TASK_INPUT_VALUE,
  taskRunExecCommand,
  taskRunInputIssue,
} = taskCommand
const { canApplyAction, redactedApplyCommand } = mutation

const ROOT = "/tmp/ozagia repo"
const TOKEN = "Abcdefghijklmnopqrstuvwxyz_0123456789-ABCD"
const tempDirs = []

after(() => {
  for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true })
})

function tempRoot(prefix = "ozagia-mutations-test-") {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  tempDirs.push(dir)
  return dir
}

function fakePython(body) {
  const dir = tempRoot()
  const file = path.join(dir, "fake-python")
  fs.writeFileSync(file, `#!/usr/bin/env node\n${body}\n`, "utf8")
  fs.chmodSync(file, 0o755)
  return { dir, file }
}

function showEntry(id, source, specPath) {
  return {
    entry: { id, source, path: specPath, domain: "coding", version: 1, title: id },
    spec: { id, inputs: ["diff"] },
  }
}

// ---------------------------------------------------------------------------
// actionApply
// ---------------------------------------------------------------------------

test("actionApply argv carries the token but the display never does", () => {
  const spec = actionApplySpec(ROOT, {
    action: "service.start",
    name: "api",
    confirm: TOKEN,
  })
  assert.deepEqual(spec.args, [
    "apply",
    "service.start",
    "--confirm",
    TOKEN,
    "--root",
    ROOT,
    "--name",
    "api",
    "--json",
  ])
  assert.equal(
    spec.display,
    "ozagia.actions_cli apply service.start --confirm <redacted> --json",
  )
  assert.ok(spec.display.includes("--confirm <redacted>"))
  assert.ok(!spec.display.includes(TOKEN))
  assert.deepEqual(spec.sensitive, [TOKEN])

  const plain = actionApplySpec(ROOT, { action: "daemon.status", confirm: TOKEN })
  assert.deepEqual(plain.args, [
    "apply",
    "daemon.status",
    "--confirm",
    TOKEN,
    "--root",
    ROOT,
    "--json",
  ])
})

test("actionApply confirms only bounded core.actions tokens", () => {
  assert.equal(confirmationToken(TOKEN), TOKEN)
  assert.equal(confirmationToken("a".repeat(20)), "a".repeat(20))
  assert.equal(confirmationToken("a".repeat(128)), "a".repeat(128))

  for (const value of [
    undefined,
    null,
    "",
    "short",
    "a".repeat(19),
    "a".repeat(129),
    `${TOKEN}!`,
    `${TOKEN}.`,
    `${TOKEN}\n`,
    `${TOKEN} `,
    "token with spaces 1234",
  ]) {
    assert.throws(() => confirmationToken(value), InputError)
  }
})

test("actionApply reuses the preview action and argument rules", () => {
  assert.throws(() => actionApplySpec(ROOT, { confirm: TOKEN }), InputError)
  assert.throws(
    () => actionApplySpec(ROOT, { action: "service.delete", confirm: TOKEN }),
    InputError,
  )
  assert.throws(
    () => actionApplySpec(ROOT, { action: "daemon.status", name: "api", confirm: TOKEN }),
    /not supported/,
  )
  assert.throws(
    () => actionApplySpec(ROOT, { action: "service.start", confirm: TOKEN }),
    /name is required/,
  )
  assert.throws(
    () =>
      actionApplySpec(ROOT, {
        action: "service.start",
        name: "api\nrm -rf /",
        confirm: TOKEN,
      }),
    /control characters/,
  )
  assert.throws(
    () =>
      actionApplySpec(ROOT, {
        action: "service.logs",
        name: "api",
        maxBytes: 65537,
        confirm: TOKEN,
      }),
    InputError,
  )

  for (const action of ACTION_NAMES) {
    const input = { action, confirm: TOKEN }
    if (["service.status", "service.start", "service.run", "service.stop", "service.restart", "service.logs", "tunnel.status", "tunnel.start"].includes(action)) {
      input.name = "api"
    }
    assert.equal(actionApplySpec(ROOT, input).args[1], action)
  }
})

test("runner scrubs the token from commands and error tails", async () => {
  const { dir, file } = fakePython(
    [
      "const args = process.argv.slice(2)",
      "const token = args[args.indexOf('--confirm') + 1]",
      "process.stderr.write(`apply refused ${token}\\n`)",
      "process.exit(2)",
    ].join("\n"),
  )
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000 },
    actionApplySpec(dir, { action: "daemon.status", confirm: TOKEN }),
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "exit_status")
  assert.ok(!result.command.includes(TOKEN))
  assert.ok(result.command.includes("--confirm <redacted>"))
  assert.ok(!result.error.detail.includes(TOKEN))
  assert.match(result.error.detail, /\[redacted\]/)
  assert.equal(redactSensitive(`before ${TOKEN} after`, [TOKEN]), "before [redacted] after")
})

test("apply spawns argv without a shell", async () => {
  const marker = path.join(tempRoot(), "shell-marker")
  const name = `api;touch ${marker}`
  const script = [
    "const args = process.argv.slice(2)",
    "const name = args[args.indexOf('--name') + 1]",
    "process.stdout.write(JSON.stringify({ name }))",
  ].join("\n")
  const { dir, file } = fakePython(script)
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000 },
    actionApplySpec(dir, { action: "service.start", name, confirm: TOKEN }),
  )
  assert.equal(result.ok, true)
  assert.deepEqual(result.data, { name })
  assert.equal(fs.existsSync(marker), false)
  assert.ok(result.command.includes("--confirm <redacted>"))
})

// ---------------------------------------------------------------------------
// taskRun
// ---------------------------------------------------------------------------

function taskShowData(root, overrides = {}) {
  const specPath = path.join(root, "ozagia", "tasks", "diff_review.toml")
  return {
    ...showEntry("diff_review", "repo", specPath),
    ...overrides,
  }
}

function fakeTaskPython(options) {
  const show = options.show ?? {}
  const showExit = options.showExit ?? 0
  const runDelay = options.runDelay ?? 0
  const runExit = options.runExit ?? 0
  const body = [
    "const args = process.argv.slice(2)",
    "const moduleName = args[1]",
    "if (moduleName === 'ozagia.task_library_cli') {",
    `  process.stdout.write(${JSON.stringify(JSON.stringify(show))})`,
    `  process.exit(${showExit})`,
    "}",
    "if (moduleName === 'ozagia.tasks_cli') {",
    `  setTimeout(() => { process.stdout.write(JSON.stringify({ argv: args.slice(2) })); process.exit(${runExit}) }, ${runDelay})`,
    "} else { process.exit(3) }",
  ].join("\n")
  return fakePython(body)
}

test("taskRun resolves the id through the library and runs the local executor", async () => {
  const root = tempRoot()
  fs.mkdirSync(path.join(root, "ozagia", "tasks"), { recursive: true })
  const show = taskShowData(root)
  const { file } = fakeTaskPython({ show })
  const result = await runTask(
    { python: file, root, timeoutMs: 2000 },
    {
      id: "diff_review",
      inputs: { zeta: "last value", diff: "a patch", alpha: "first value" },
    },
  )
  assert.equal(result.ok, true)
  assert.deepEqual(result.data.argv, [
    "run",
    show.entry.path,
    "--executor",
    "local",
    "--allow-exec",
    "--root",
    root,
    "--input",
    "alpha=first value",
    "--input",
    "diff=a patch",
    "--input",
    "zeta=last value",
    "--json",
  ])
  assert.ok(!result.command.includes("first value"))
  assert.ok(!result.command.includes("a patch"))
  assert.ok(result.command.includes("alpha=<redacted>"))
  assert.ok(result.command.includes("--executor local --allow-exec"))
  assert.ok(!result.command.includes("--approve"))
  assert.ok(!result.command.includes("--dry-run"))
})

test("taskRun never falls back to a caller-supplied path", async () => {
  const root = tempRoot()
  fs.mkdirSync(path.join(root, "ozagia", "tasks"), { recursive: true })
  const show = taskShowData(root)
  const { file } = fakeTaskPython({ show })
  const result = await runTask(
    { python: file, root, timeoutMs: 2000 },
    {
      id: "diff_review",
      inputs: {},
      path: "/tmp/caller-supplied.toml",
    },
  )
  assert.equal(result.ok, true)
  assert.equal(result.data.argv[1], show.entry.path)
  assert.notEqual(result.data.argv[1], "/tmp/caller-supplied.toml")
})

test("taskRun returns a structured error for an unusable library path", async () => {
  const root = tempRoot()
  fs.mkdirSync(path.join(root, "ozagia", "tasks"), { recursive: true })
  const show = taskShowData(root)
  delete show.entry.path
  const { file } = fakeTaskPython({ show, runExit: 7 })
  const result = await runTask(
    { python: file, root, timeoutMs: 2000 },
    { id: "diff_review", inputs: {} },
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "task_path_invalid")
  assert.match(result.error.message, /spec path/)
  // The run branch would exit 7; the resolver stopped before spawning it.
  assert.equal(result.exitCode, null)

  const outside = taskShowData(root)
  outside.entry.path = path.join(root, "elsewhere", "diff_review.toml")
  assert.match(
    resolveTaskSpecPath(root, "diff_review", outside).error,
    /outside ozagia\/tasks/,
  )
})

test("taskRun returns the show envelope when resolution fails", async () => {
  const root = tempRoot()
  const { file } = fakeTaskPython({ show: {}, showExit: 2 })
  const result = await runTask(
    { python: file, root, timeoutMs: 2000 },
    { id: "diff_review", inputs: {} },
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "exit_status")
  assert.equal(result.exitCode, 2)
})

test("taskRun validates id and inputs before spawning", async () => {
  const root = tempRoot()
  const { file } = fakeTaskPython({ show: taskShowData(root) })

  for (const value of [
    undefined,
    {},
    { id: "../escape" },
    { id: "1bad" },
    { id: "diff_review", inputs: [] },
    { id: "diff_review", inputs: { "1bad": "x" } },
    { id: "diff_review", inputs: { diff: 4 } },
    { id: "diff_review", inputs: { diff: "x".repeat(MAX_TASK_INPUT_VALUE + 1) } },
    { id: "diff_review", inputs: { diff: "bad\u0000value" } },
    { id: "diff_review", inputs: { diff: "bad\nvalue" } },
    { id: "diff_review", inputs: Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`k${i}`, "v"])) },
  ]) {
    assert.throws(() => taskRunValues(value), InputError)
    await assert.rejects(
      runTask({ python: file, root, timeoutMs: 2000 }, value),
      InputError,
    )
  }

  const many = Object.fromEntries(
    Array.from({ length: 16 }, (_, i) => [`k${i}`, "x".repeat(MAX_TASK_INPUT_VALUE)]),
  )
  assert.throws(
    () => taskRunSpec(root, { id: "diff_review", inputs: many }, taskShowData(root).entry.path),
    /at most 65536 bytes/,
  )
})

test("task inputs are bounded strings keyed by the library alphabet", () => {
  assert.deepEqual(taskInputs(undefined), {})
  assert.deepEqual(taskInputs({ diff: "x", "a.b-c_d": "" }), { diff: "x", "a.b-c_d": "" })
  assert.throws(() => taskInputs({ diff: "x".repeat(MAX_TASK_INPUT_VALUE + 1) }), InputError)
  assert.throws(() => taskInputs({ diff: "bad\u0001value" }), InputError)
  assert.throws(() => taskInputs({ diff: true }), InputError)
})

test("task spec paths must belong to the library", () => {
  const root = tempRoot()
  const repoPath = path.join(root, "ozagia", "tasks", "diff_review.toml")
  assert.equal(taskSpecPath(repoPath), repoPath)
  assert.throws(() => taskSpecPath("relative/diff_review.toml"), InputError)
  assert.throws(() => taskSpecPath(`${repoPath}\u0000`), InputError)

  const validRepo = resolveTaskSpecPath(root, "diff_review", taskShowData(root))
  assert.equal(validRepo.path, repoPath)

  const traversal = taskShowData(root)
  traversal.entry.path = path.join(root, "ozagia", "tasks", "..", "..", "diff_review.toml")
  assert.match(
    resolveTaskSpecPath(root, "diff_review", traversal).error,
    /outside ozagia\/tasks/,
  )

  const symlinkRoot = tempRoot()
  const tasksDir = path.join(symlinkRoot, "ozagia", "tasks")
  fs.mkdirSync(tasksDir, { recursive: true })
  const outsideFile = path.join(tempRoot(), "diff_review.toml")
  fs.writeFileSync(outsideFile, "id = 'diff_review'\n")
  const linked = path.join(tasksDir, "diff_review.toml")
  fs.symlinkSync(outsideFile, linked)
  const symlinked = taskShowData(symlinkRoot)
  symlinked.entry.path = linked
  assert.match(
    resolveTaskSpecPath(symlinkRoot, "diff_review", symlinked).error,
    /outside ozagia\/tasks/,
  )

  const wrongName = taskShowData(root)
  wrongName.entry.path = path.join(root, "ozagia", "tasks", "other.toml")
  assert.match(resolveTaskSpecPath(root, "diff_review", wrongName).error, /library file/)

  const otherId = taskShowData(root)
  otherId.entry.id = "test_triage"
  assert.match(resolveTaskSpecPath(root, "diff_review", otherId).error, /different task id/)

  const builtin = taskShowData(root)
  builtin.entry.source = "builtin"
  builtin.entry.path = "/opt/ozagia/task_specs/coding/diff_review.toml"
  assert.equal(
    resolveTaskSpecPath(root, "diff_review", builtin).path,
    "/opt/ozagia/task_specs/coding/diff_review.toml",
  )

  const outsideBuiltin = taskShowData(root)
  outsideBuiltin.entry.source = "builtin"
  outsideBuiltin.entry.path = "/opt/ozagia/other/diff_review.toml"
  assert.match(
    resolveTaskSpecPath(root, "diff_review", outsideBuiltin).error,
    /package library/,
  )

  const unknownSource = taskShowData(root)
  unknownSource.entry.source = "cache"
  assert.match(resolveTaskSpecPath(root, "diff_review", unknownSource).error, /unknown spec source/)
})

test("taskRun uses the long task timeout, not the short read-only timeout", async () => {
  const root = tempRoot()
  fs.mkdirSync(path.join(root, "ozagia", "tasks"), { recursive: true })
  const { file } = fakeTaskPython({ show: taskShowData(root), runDelay: 700 })
  const result = await runTask(
    { python: file, root, timeoutMs: 300, taskTimeoutMs: 2000 },
    { id: "diff_review", inputs: {} },
  )
  assert.equal(result.ok, true)
})

test("task timeouts stay inside the local test-runner ceiling", () => {
  assert.equal(taskRunTimeoutMs({ python: "python3", root: ROOT, timeoutMs: 1000 }), DEFAULT_TASK_TIMEOUT_MS)
  assert.equal(
    taskRunTimeoutMs({ python: "python3", root: ROOT, timeoutMs: 1000, taskTimeoutMs: 5000 }),
    5000,
  )
  assert.equal(
    taskRunTimeoutMs({ python: "python3", root: ROOT, timeoutMs: 1000, taskTimeoutMs: 1 }),
    1000,
  )
  assert.equal(
    taskRunTimeoutMs({ python: "python3", root: ROOT, timeoutMs: 1000, taskTimeoutMs: 10 ** 9 }),
    MAX_TASK_TIMEOUT_MS,
  )
  assert.equal(MAX_TASK_TIMEOUT_MS, 15 * 60_000)
})

// ---------------------------------------------------------------------------
// schemas, GUI display and confirmation state
// ---------------------------------------------------------------------------

test("RPC schemas bound the mutating inputs", () => {
  assert.deepEqual([...schemas.ACTION_NAMES], [...ACTION_NAMES])
  assert.ok(!schemas.ACTION_NAMES.includes("integration.install"))
  assert.deepEqual(schemas.ACTION_APPLY_INPUT.required, ["action", "confirm"])
  assert.equal(schemas.ACTION_APPLY_INPUT.additionalProperties, false)
  assert.equal(schemas.ACTION_APPLY_INPUT.properties.confirm.pattern, "^[A-Za-z0-9_-]{20,128}$")
  assert.equal(schemas.ACTION_APPLY_INPUT.properties.confirm.minLength, 20)
  assert.equal(schemas.ACTION_APPLY_INPUT.properties.confirm.maxLength, 128)

  assert.deepEqual(schemas.TASK_RUN_INPUT.required, ["id"])
  assert.equal(schemas.TASK_RUN_INPUT.additionalProperties, false)
  assert.equal(schemas.TASK_RUN_INPUT.properties.id.pattern, "^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
  assert.equal(schemas.TASK_RUN_INPUT.properties.inputs.maxProperties, 16)
  assert.equal(
    schemas.TASK_RUN_INPUT.properties.inputs.propertyNames.pattern,
    "^[A-Za-z][A-Za-z0-9_.-]{0,63}$",
  )
  assert.equal(schemas.TASK_RUN_INPUT.properties.inputs.additionalProperties.maxLength, 4096)

  const source = fs.readFileSync(path.join(__dirname, "..", "src", "rpc.ts"), "utf8")
  assert.match(source, /actionApply: \{ input: ACTION_APPLY_INPUT, output: ENVELOPE_OUTPUT \}/)
  assert.match(source, /taskRun: \{ input: TASK_RUN_INPUT, output: ENVELOPE_OUTPUT \}/)
})

test("GUI apply text is redacted and needs an explicit confirmation", () => {
  const command = redactedApplyCommand("service.start")
  assert.match(command, /apply service\.start --confirm <redacted>/)
  assert.doesNotMatch(command, /\/[A-Za-z0-9_-]{20,}/)

  const preview = { confirmation: TOKEN }
  assert.equal(canApplyAction(null, true, false), false)
  assert.equal(canApplyAction({}, true, false), false)
  assert.equal(canApplyAction({ confirmation: "" }, true, false), false)
  assert.equal(canApplyAction({ confirmation: "too-short" }, true, false), false)
  assert.equal(canApplyAction(preview, false, false), false)
  assert.equal(canApplyAction(preview, true, true), false)
  assert.equal(canApplyAction(preview, true, false), true)
})

test("GUI task run command is redacted and excludes approval", () => {
  const command = taskRunExecCommand({
    path: "/tmp/a repo/ozagia/tasks/diff_review.toml",
    root: "/tmp/a repo",
    inputs: ["diff", "repo_root"],
  })
  assert.match(command, /run '\/tmp\/a repo\/ozagia\/tasks\/diff_review\.toml'/)
  assert.match(command, /--executor local --allow-exec/)
  assert.match(command, /--input '?diff=<redacted>'?/)
  assert.doesNotMatch(command, /repo_root=/)
  assert.doesNotMatch(command, /--approve/)
  assert.doesNotMatch(command, /--dry-run/)
  assert.ok(AUTOMATIC_TASK_INPUTS.has("repo_root"))
})

test("GUI run form mirrors the input bounds", () => {
  assert.equal(taskRunInputIssue({ diff: "a patch" }, ["diff"]), null)
  assert.match(taskRunInputIssue({}, ["diff"]), /needs a value/)
  assert.match(taskRunInputIssue({ diff: "   " }, ["diff"]), /needs a value/)
  assert.match(taskRunInputIssue({ diff: "x".repeat(MAX_TASK_INPUT_VALUE + 1) }, []), /4096/)
  assert.match(taskRunInputIssue({ diff: "bad\u0000value" }, []), /control characters/)
  assert.match(taskRunInputIssue({ "1bad": "x" }, []), /not valid/)
  const many = Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`k${i}`, "v"]))
  assert.match(taskRunInputIssue(many, []), /At most 16/)
})
