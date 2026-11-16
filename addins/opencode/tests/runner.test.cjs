"use strict"

const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const { after, test } = require("node:test")
const { loadTs } = require("./load-ts.cjs")

const runner = loadTs(path.join(__dirname, "..", "src", "runner.ts"))
const {
  ACTION_NAMES,
  InputError,
  actionPreviewSpec,
  actionsListSpec,
  aperturesSpec,
  optionalDepth,
  optionalInteger,
  optionalNumber,
  policyCheckSpec,
  redact,
  researchViewSpec,
  resourcesCheckSpec,
  runOzagia,
  safeTail,
  settingsSchemaSpec,
  settingsValuesSpec,
  statusSpec,
  testingPlanSpec,
  text,
  viewSpec,
  worktreeCheckSpec,
} = runner

const ROOT = "/tmp/ozagia repo"
const tempDirs = []

after(() => {
  for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true })
})

function fakePython(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ozagia-addin-test-"))
  tempDirs.push(dir)
  const file = path.join(dir, "fake-python")
  fs.writeFileSync(file, `#!/usr/bin/env node\n${body}\n`, "utf8")
  fs.chmodSync(file, 0o755)
  return { dir, file }
}

test("status and aperture argument builders", () => {
  assert.deepEqual(statusSpec(ROOT), {
    module: "ozagia.status_cli",
    args: ["--root", ROOT, "--json"],
    display: "ozagia.status_cli --json",
  })
  assert.deepEqual(aperturesSpec(ROOT), {
    module: "ozagia.view_cli",
    args: ["--list-apertures", "--format", "json", "--root", ROOT],
    display: "ozagia.view_cli --list-apertures --format json",
  })
  assert.deepEqual(worktreeCheckSpec(ROOT), {
    module: "ozagia.worktree_cli",
    args: ["check", "--root", ROOT, "--json"],
    display: "ozagia.worktree_cli check --json",
  })
})

test("view builder includes only supplied optional flags", () => {
  assert.deepEqual(viewSpec(ROOT, { aperture: "architecture" }).args, [
    "--aperture",
    "architecture",
    "--format",
    "json",
    "--root",
    ROOT,
  ])

  assert.deepEqual(
    viewSpec(ROOT, {
      aperture: "impact",
      depth: 2,
      node: "file:src/app.py",
      graph: "/tmp/graph.json",
    }).args,
    [
      "--aperture",
      "impact",
      "--format",
      "json",
      "--root",
      ROOT,
      "--depth",
      "2",
      "--node",
      "file:src/app.py",
      "--graph",
      "/tmp/graph.json",
    ],
  )

  assert.equal(viewSpec(ROOT, { aperture: "evidence" }).display, "ozagia.view_cli --aperture evidence --format json")
})

test("view builder rejects malformed apertures" , () => {
  for (const aperture of ["", "has space", "../escape", "a".repeat(65), undefined]) {
    assert.throws(() => viewSpec(ROOT, { aperture }), InputError)
  }
  assert.throws(() => viewSpec(ROOT, { aperture: "impact", depth: -1 }), InputError)
  assert.throws(() => viewSpec(ROOT, { aperture: "impact", depth: 33 }), InputError)
  assert.throws(() => viewSpec(ROOT, { aperture: "impact", depth: 1.5 }), InputError)
})

test("policy builder covers commit, merge and push flags", () => {
  assert.deepEqual(
    policyCheckSpec(ROOT, {
      kind: "commit",
      message: "feat: add panel",
      force: true,
      tags: true,
    }).args,
    [
      "policy",
      "--for",
      "commit",
      "--root",
      ROOT,
      "--json",
      "--message",
      "feat: add panel",
      "--force",
      "--tags",
    ],
  )

  assert.deepEqual(policyCheckSpec(ROOT, { kind: "merge", target: "main" }).args, [
    "policy",
    "--for",
    "merge",
    "--root",
    ROOT,
    "--json",
    "--target",
    "main",
  ])
  assert.throws(() => policyCheckSpec(ROOT, { kind: "delete" }), InputError)
  assert.throws(() => policyCheckSpec(ROOT, {}), InputError)
  assert.throws(
    () => policyCheckSpec(ROOT, { kind: "commit", message: "x".repeat(1001) }),
    InputError,
  )
})

test("resources builder defaults to job and bounds numbers", () => {
  assert.deepEqual(resourcesCheckSpec(ROOT, {}).args, [
    "check",
    "--for",
    "job",
    "--root",
    ROOT,
    "--json",
  ])
  assert.deepEqual(
    resourcesCheckSpec(ROOT, {
      kind: "worktree",
      memoryMb: 2048,
      diskMb: 4096.5,
      path: "/tmp/wt",
    }).args,
    [
      "check",
      "--for",
      "worktree",
      "--root",
      ROOT,
      "--json",
      "--memory-mb",
      "2048",
      "--disk-mb",
      "4096.5",
      "--path",
      "/tmp/wt",
    ],
  )
  assert.throws(() => resourcesCheckSpec(ROOT, { kind: "pod" }), InputError)
  assert.throws(() => resourcesCheckSpec(ROOT, { memoryMb: -1 }), InputError)
  assert.throws(() => resourcesCheckSpec(ROOT, { memoryMb: Number.POSITIVE_INFINITY }), InputError)
})

test("testing builder repeats changed paths and keeps defaults", () => {
  assert.deepEqual(testingPlanSpec(ROOT, {}).args, [
    "plan",
    "--root",
    ROOT,
    "--json",
  ])
  assert.deepEqual(
    testingPlanSpec(ROOT, {
      base: "main",
      head: "HEAD",
      task: "code_review",
      changed: ["src/app.py", "tests/test_app.py"],
    }).args,
    [
      "plan",
      "--root",
      ROOT,
      "--json",
      "--base",
      "main",
      "--head",
      "HEAD",
      "--task",
      "code_review",
      "--changed",
      "src/app.py",
      "--changed",
      "tests/test_app.py",
    ],
  )
  assert.deepEqual(testingPlanSpec(ROOT, { changed: "src/app.py" }).args, [
    "plan",
    "--root",
    ROOT,
    "--json",
    "--changed",
    "src/app.py",
  ])
  assert.throws(() => testingPlanSpec(ROOT, { changed: ["x".repeat(4097)] }), InputError)
  assert.throws(() => testingPlanSpec(ROOT, { changed: Array(501).fill("a") }), InputError)
})

test("research builder validates the investigation id and view", () => {
  assert.deepEqual(
    researchViewSpec(ROOT, {
      investigation: "research-20261116-120000-abcd1234",
      view: "consensus",
    }).args,
    [
      "--root",
      ROOT,
      "--investigation",
      "research-20261116-120000-abcd1234",
      "--view",
      "consensus",
      "--format",
      "json",
    ],
  )
  assert.throws(() => researchViewSpec(ROOT, { investigation: "../escape", view: "library" }), InputError)
  assert.throws(() => researchViewSpec(ROOT, { investigation: "research-1", view: "unknown" }), InputError)
})

test("settings and action catalogue builders", () => {
  assert.deepEqual(settingsSchemaSpec(), {
    module: "ozagia.settings_cli",
    args: ["schema", "--json"],
    display: "ozagia.settings_cli schema --json",
  })
  assert.deepEqual(settingsValuesSpec(ROOT), {
    module: "ozagia.settings_cli",
    args: ["values", "--root", ROOT, "--json"],
    display: "ozagia.settings_cli values --json",
  })
  assert.deepEqual(actionsListSpec(), {
    module: "ozagia.actions_cli",
    args: ["list", "--json"],
    display: "ozagia.actions_cli list --json",
  })
})

test("action preview builder covers fixed and optional arguments", () => {
  assert.deepEqual(actionPreviewSpec(ROOT, { action: "daemon.status" }).args, [
    "preview",
    "daemon.status",
    "--root",
    ROOT,
    "--json",
  ])

  assert.deepEqual(
    actionPreviewSpec(ROOT, { action: "service.start", name: "api" }).args,
    ["preview", "service.start", "--root", ROOT, "--name", "api", "--json"],
  )

  const logs = actionPreviewSpec(ROOT, {
    action: "service.logs",
    name: "api",
    maxBytes: 8192,
  })
  assert.deepEqual(logs.args, [
    "preview",
    "service.logs",
    "--root",
    ROOT,
    "--name",
    "api",
    "--max-bytes",
    "8192",
    "--json",
  ])

  const daemon = actionPreviewSpec(ROOT, {
    action: "daemon.restart",
    wait: 30,
    idleTimeout: 0,
  })
  assert.deepEqual(daemon.args, [
    "preview",
    "daemon.restart",
    "--root",
    ROOT,
    "--wait",
    "30",
    "--idle-timeout",
    "0",
    "--json",
  ])

  const tunnel = actionPreviewSpec(ROOT, {
    action: "tunnel.start",
    name: "database",
    live: true,
    network: true,
  })
  assert.deepEqual(tunnel.args, [
    "preview",
    "tunnel.start",
    "--root",
    ROOT,
    "--name",
    "database",
    "--allow-live",
    "--allow-network",
    "--json",
  ])
  assert.equal(tunnel.display, "ozagia.actions_cli preview tunnel.start --json")

  assert.deepEqual(
    actionPreviewSpec(ROOT, {
      action: "tunnel.start",
      name: "database",
      live: false,
      network: false,
    }).args,
    ["preview", "tunnel.start", "--root", ROOT, "--name", "database", "--json"],
  )

  assert.deepEqual(
    actionPreviewSpec(ROOT, {
      action: "integration.status",
      name: "codex",
      scope: "user",
    }).args,
    [
      "preview",
      "integration.status",
      "--root",
      ROOT,
      "--name",
      "codex",
      "--scope",
      "user",
      "--json",
    ],
  )

  assert.deepEqual(
    actionPreviewSpec(ROOT, { action: "integration.status" }).args,
    ["preview", "integration.status", "--root", ROOT, "--json"],
  )
})

test("action preview builder rejects unsupported and unbounded input", () => {
  assert.ok(ACTION_NAMES.includes("tunnel.start"))
  assert.throws(() => actionPreviewSpec(ROOT, {}), InputError)
  assert.throws(() => actionPreviewSpec(ROOT, { action: "service.delete" }), InputError)
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "service.start" }),
    /name is required/,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "service.start", name: "api\nrm -rf /" }),
    /control characters/,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "service.start", name: "x".repeat(129) }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "daemon.status", name: "api" }),
    /not supported/,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "daemon.status", live: true }),
    /not supported/,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "service.logs", name: "api", maxBytes: -1 }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "service.logs", name: "api", maxBytes: 65537 }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "service.logs", name: "api", maxBytes: 1.5 }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "integration.status", scope: "global" }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, {
      action: "integration.status",
      name: "vim",
    }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "daemon.ensure", wait: -1 }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "daemon.ensure", wait: 601 }),
    InputError,
  )
  assert.throws(
    () => actionPreviewSpec(ROOT, { action: "daemon.ensure", idleTimeout: 86401 }),
    InputError,
  )

  for (const action of ACTION_NAMES) {
    const input = { action }
    if (
      [
        "service.status",
        "service.start",
        "service.run",
        "service.stop",
        "service.restart",
        "service.logs",
        "tunnel.status",
        "tunnel.start",
      ].includes(action)
    ) {
      input.name = "api"
    }
    assert.equal(actionPreviewSpec(ROOT, input).args[1], action)
  }
})

test("shared bounds and types", () => {
  assert.equal(text("  /repo  ", "root", 10), "/repo")
  assert.throws(() => text("", "root", 10), InputError)
  assert.throws(() => text("x".repeat(11), "root", 10), InputError)
  assert.throws(() => text("bad\u0000value", "root", 10), InputError)
  assert.equal(optionalDepth(0), 0)
  assert.equal(optionalDepth(32), 32)
  assert.throws(() => optionalDepth(1.1), InputError)
  assert.equal(optionalNumber(0, "memoryMb", 10), 0)
  assert.throws(() => optionalNumber("4", "memoryMb", 10), InputError)
  assert.equal(optionalInteger(0, "maxBytes", 0, 10), 0)
  assert.equal(optionalInteger(10, "maxBytes", 0, 10), 10)
  assert.throws(() => optionalInteger(11, "maxBytes", 0, 10), InputError)
  assert.throws(() => optionalInteger(1.5, "maxBytes", 0, 10), InputError)
  assert.throws(() => optionalInteger("4", "maxBytes", 0, 10), InputError)
})

test("redaction removes obvious tokens and keeps the tail", () => {
  assert.equal(redact("token=supersecret"), "token=[redacted]")
  assert.equal(redact("Bearer abc.def-123"), "Bearer [redacted]")
  assert.equal(redact("ghp_abcdefghijklmnopqrstuvwxyz0123456789"), "[redacted]")
  assert.equal(redact("sk-abcdefghijklmnopqrstuvwxyz"), "[redacted]")
  assert.equal(redact(`npm_${"a".repeat(30)}`), "[redacted]")
  assert.equal(redact(`AIza${"a".repeat(35)}`), "[redacted]")
  assert.equal(redact("https://user:pass@example.test/x"), "https://[redacted]@example.test/x")

  const tail = safeTail("start " + "x".repeat(1000) + " end", 80)
  assert.ok(tail.startsWith("…"))
  assert.ok(tail.endsWith("end"))
  assert.ok(tail.length <= 81)
  assert.equal(safeTail(" a\n\tb  c "), "a b c")
})

test("runOzagia parses JSON and accepts exit status 1", async () => {
  const { dir, file } = fakePython(
    "process.stdout.write(JSON.stringify({ ok: true, value: 1 })); process.exit(1)",
  )
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000 },
    statusSpec(dir),
  )
  assert.equal(result.ok, true)
  assert.deepEqual(result.data, { ok: true, value: 1 })
  assert.equal(result.exitCode, 1)
  assert.equal(result.error, null)
  assert.equal(result.command, `${file} -m ozagia.status_cli --json`)
})

test("runOzagia reports exit errors with a redacted short tail", async () => {
  const { dir, file } = fakePython(
    'process.stderr.write("boom token=supersecret\\n"); process.exit(2)',
  )
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000 },
    worktreeCheckSpec(dir),
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "exit_status")
  assert.match(result.error.detail, /\[redacted\]/)
  assert.doesNotMatch(result.error.detail, /supersecret/)
})

test("runOzagia times out and cancels bounded subprocesses", async () => {
  const { dir, file } = fakePython("setTimeout(() => {}, 5000)")

  const timedOut = await runOzagia(
    { python: file, root: dir, timeoutMs: 100 },
    aperturesSpec(dir),
  )
  assert.equal(timedOut.ok, false)
  assert.equal(timedOut.error.code, "timeout")

  const controller = new AbortController()
  setTimeout(() => controller.abort(), 50)
  const cancelled = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000 },
    aperturesSpec(dir),
    controller.signal,
  )
  assert.equal(cancelled.ok, false)
  assert.equal(cancelled.error.code, "cancelled")
})

test("runOzagia reports a missing root before spawning", async () => {
  const missing = fs.mkdtempSync(path.join(os.tmpdir(), "ozagia-addin-gone-"))
  fs.rmSync(missing, { recursive: true, force: true })
  const result = await runOzagia(
    { python: "python3", root: missing, timeoutMs: 2000 },
    statusSpec(missing),
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "root_missing")
  assert.match(result.error.message, /root does not exist/)
})

test("runOzagia caps stdout", async () => {
  const { dir, file } = fakePython("process.stdout.write('x'.repeat(10000))")
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000, maxBuffer: 200 },
    aperturesSpec(dir),
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "output_too_large")
})
