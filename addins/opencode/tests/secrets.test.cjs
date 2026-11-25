"use strict"

/**
 * Offline tests for the session secret request slice.
 *
 * The runner and spec builders are dependency-free TypeScript loaded with
 * `load-ts.cjs`. A fake `python` executable echoes only the length and digest
 * of what it read from stdin, so the test can prove the value never appeared
 * in argv, the display string or the envelope. No daemon, network, model or
 * OpenCode package is involved.
 */

const assert = require("node:assert/strict")
const crypto = require("node:crypto")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const { after, test } = require("node:test")
const { loadTs } = require("./load-ts.cjs")

const runner = loadTs(path.join(__dirname, "..", "src", "runner.ts"))
const schemas = loadTs(path.join(__dirname, "..", "src", "schemas.ts"))

const {
  InputError,
  MAX_STDIN_BYTES,
  rejectUnknownFields,
  runOzagia,
  secretRequestId,
  secretSession,
  secretValue,
  secretsDeclineSpec,
  secretsFulfillSpec,
  secretsRequestsSpec,
} = runner

const ROOT = "/tmp/ozagia repo"
const SESSION = "ses_abc-123"
const REQUEST = "req_abc-123"
const VALUE = "canary-secret-value-0123456789"
const tempDirs = []

after(() => {
  for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true })
})

function fakePython(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ozagia-secrets-test-"))
  tempDirs.push(dir)
  const file = path.join(dir, "fake-python")
  fs.writeFileSync(file, `#!/usr/bin/env node\n${body}\n`, "utf8")
  fs.chmodSync(file, 0o755)
  return { dir, file }
}

function rawSpec(overrides = {}) {
  return {
    module: "ozagia.secrets_cli",
    args: ["fulfill", REQUEST, "--stdin", "--json"],
    display: "ozagia.secrets_cli fulfill --stdin --json",
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// Spec builders
// ---------------------------------------------------------------------------

test("secret request builders use bounded opaque handles", () => {
  assert.deepEqual(secretsRequestsSpec(ROOT, { session: SESSION }).args, [
    "requests",
    "--root",
    ROOT,
    "--session",
    SESSION,
    "--harness",
    "opencode",
    "--json",
  ])
  assert.equal(
    secretsRequestsSpec(ROOT, { session: SESSION }).display,
    "ozagia.secrets_cli requests --json",
  )

  const spec = secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, ttlS: 60 }, VALUE)
  assert.deepEqual(spec.args, [
    "fulfill",
    REQUEST,
    "--root",
    ROOT,
    "--session",
    SESSION,
    "--harness",
    "opencode",
    "--stdin",
    "--json",
    "--ttl-s",
    "60",
  ])
  assert.equal(spec.stdin, VALUE)
  assert.equal(spec.sensitive, undefined)
  assert.equal(spec.display, "ozagia.secrets_cli fulfill --stdin --json")
  assert.ok(!spec.args.some((arg) => arg.includes(VALUE)))
  assert.ok(!spec.display.includes(VALUE))

  assert.deepEqual(secretsDeclineSpec(ROOT, { session: SESSION, requestId: REQUEST }).args, [
    "decline",
    REQUEST,
    "--root",
    ROOT,
    "--session",
    SESSION,
    "--harness",
    "opencode",
    "--json",
  ])
  assert.equal(
    secretsDeclineSpec(ROOT, { session: SESSION, requestId: REQUEST }).display,
    "ozagia.secrets_cli decline --json",
  )
})

test("secret builders reject unknown fields including harness and inline value", () => {
  assert.throws(
    () => secretsRequestsSpec(ROOT, { session: SESSION, harness: "codex" }),
    /does not accept "harness"/,
  )
  assert.throws(
    () => secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, harness: "codex" }, VALUE),
    /does not accept "harness"/,
  )
  assert.throws(
    () => secretsDeclineSpec(ROOT, { session: SESSION, requestId: REQUEST, harness: "codex" }),
    /does not accept "harness"/,
  )
  assert.throws(
    () => secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, value: VALUE }, VALUE),
    /does not accept "value"/,
  )
  assert.throws(
    () => secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, extra: true }, VALUE),
    /does not accept "extra"/,
  )
  assert.throws(() => rejectUnknownFields([], []), InputError)
})

test("session and request ids are short opaque strings", () => {
  assert.equal(secretSession(`  ${SESSION}  `), SESSION)
  assert.equal(secretRequestId(REQUEST), REQUEST)
  for (const value of [
    undefined,
    null,
    "",
    " ",
    "ses abc",
    "ses/abc",
    "ses\\abc",
    "ses\nabc",
    "x".repeat(129),
  ]) {
    assert.throws(() => secretSession(value), InputError)
    assert.throws(() => secretRequestId(value), InputError)
  }
  assert.throws(() => secretsRequestsSpec(ROOT, {}), InputError)
  assert.throws(
    () => secretsFulfillSpec(ROOT, { session: SESSION, requestId: "has space" }, VALUE),
    InputError,
  )
})

test("ttl and value bounds are enforced before spawn", () => {
  for (const ttlS of [0, -1, 86_401, 1.5, "60"]) {
    assert.throws(
      () => secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, ttlS }, VALUE),
      InputError,
    )
  }
  assert.equal(
    secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, ttlS: 1 }, VALUE).args[11],
    "1",
  )
  assert.equal(
    secretsFulfillSpec(ROOT, { session: SESSION, requestId: REQUEST, ttlS: 86_400 }, VALUE).args[11],
    "86400",
  )

  assert.equal(secretValue("x"), "x")
  assert.equal(secretValue("é".repeat(4096)).length, 4096)
  for (const value of [undefined, null, "", 42]) {
    assert.throws(() => secretValue(value), InputError)
  }
  assert.throws(() => secretValue("x".repeat(8193)), /at most 8192 bytes/)
  assert.throws(() => secretValue("é".repeat(4097)), /at most 8192 bytes/)
  assert.doesNotMatch(
    (() => {
      try {
        secretValue("x".repeat(9000))
      } catch (error) {
        return String(error.message)
      }
      return ""
    })(),
    /xxxxxxxx/,
  )
})

// ---------------------------------------------------------------------------
// Runner stdin transport
// ---------------------------------------------------------------------------

test("runOzagia pipes the value to stdin only and closes it", async () => {
  const { dir, file } = fakePython(`
const crypto = require("node:crypto")
let data = ""
process.stdin.setEncoding("utf8")
process.stdin.on("data", (chunk) => { data += chunk })
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({
    argv: process.argv.slice(2),
    bytes: Buffer.byteLength(data, "utf8"),
    sha256: crypto.createHash("sha256").update(data, "utf8").digest("hex"),
    closed: process.stdin.readableEnded,
  }))
})
`)
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 3000 },
    rawSpec({ stdin: VALUE }),
  )
  assert.equal(result.ok, true)
  assert.equal(result.data.bytes, Buffer.byteLength(VALUE, "utf8"))
  assert.equal(
    result.data.sha256,
    crypto.createHash("sha256").update(VALUE, "utf8").digest("hex"),
  )
  assert.equal(result.data.closed, true)
  assert.ok(!result.data.argv.includes(VALUE))
  assert.ok(!result.command.includes(VALUE))
  assert.ok(!JSON.stringify(result).includes(VALUE))
})

test("runner caps stdin before spawning", async () => {
  const { dir, file } = fakePython(
    "process.stdout.write(JSON.stringify({ spawned: true }))",
  )
  const tooLarge = "x".repeat(MAX_STDIN_BYTES + 1)
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 3000 },
    rawSpec({ stdin: tooLarge }),
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "stdin_too_large")
  assert.ok(!JSON.stringify(result).includes(tooLarge))
})

test("a failing CLI that echoes stdin cannot leak the value", async () => {
  const { dir, file } = fakePython(`
let data = ""
process.stdin.setEncoding("utf8")
process.stdin.on("data", (chunk) => { data += chunk })
process.stdin.on("end", () => {
  process.stderr.write("failed while storing " + data)
  process.exit(2)
})
`)
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 3000 },
    rawSpec({ stdin: VALUE }),
  )
  assert.equal(result.ok, false)
  assert.equal(result.error.code, "exit_status")
  assert.match(result.error.detail, /\[redacted\]/)
  assert.doesNotMatch(result.error.detail, /canary-secret/)
  assert.ok(!JSON.stringify(result).includes(VALUE))
})

test("a successful fulfill envelope carries only metadata, never the value", async () => {
  const { dir, file } = fakePython(`
let data = ""
process.stdin.setEncoding("utf8")
process.stdin.on("data", (chunk) => { data += chunk })
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({
    ok: true,
    request_id: "req_abc-123",
    state: "fulfilled",
    ref: "secret://session/7f3a9c",
    fingerprint: "sha256:1a2b3c4d5e6f7788",
    expires: 1800.5,
    storage: "memory-only",
  }))
})
`)
  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 3000 },
    rawSpec({ stdin: VALUE }),
  )
  assert.equal(result.ok, true)
  assert.deepEqual(Object.keys(result.data).sort(), [
    "expires",
    "fingerprint",
    "ok",
    "ref",
    "request_id",
    "state",
    "storage",
  ])
  assert.equal(result.data.ref, "secret://session/7f3a9c")
  assert.equal(result.data.fingerprint, "sha256:1a2b3c4d5e6f7788")
  assert.ok(!("value" in result.data))
  assert.ok(!JSON.stringify(result).includes(VALUE))
})

// ---------------------------------------------------------------------------
// RPC and GUI shape checks
// ---------------------------------------------------------------------------

test("RPC schemas expose bounded secret methods without a harness field", () => {
  assert.deepEqual(schemas.SECRET_SESSION_INPUT.required, ["session"])
  assert.deepEqual(schemas.SECRET_DECLINE_INPUT.required, ["session", "requestId"])
  assert.deepEqual(schemas.SECRET_FULFILL_INPUT.required, ["session", "requestId", "value"])
  for (const schema of [
    schemas.SECRET_SESSION_INPUT,
    schemas.SECRET_FULFILL_INPUT,
    schemas.SECRET_DECLINE_INPUT,
  ]) {
    assert.equal(schema.additionalProperties, false)
    assert.ok(!("harness" in schema.properties))
  }
  assert.equal(schemas.SECRET_FULFILL_INPUT.properties.value.maxLength, 8192)
  assert.equal(schemas.SECRET_FULFILL_INPUT.properties.ttlS.minimum, 1)
  assert.equal(schemas.SECRET_FULFILL_INPUT.properties.ttlS.maximum, 86400)

  const rpc = fs.readFileSync(path.join(__dirname, "..", "src", "rpc.ts"), "utf8")
  assert.match(rpc, /secretsRequests: \{ input: SECRET_SESSION_INPUT, output: ENVELOPE_OUTPUT \}/)
  assert.match(rpc, /secretsFulfill: \{ input: SECRET_FULFILL_INPUT, output: ENVELOPE_OUTPUT \}/)
  assert.match(rpc, /secretsDecline: \{ input: SECRET_DECLINE_INPUT, output: ENVELOPE_OUTPUT \}/)

  const index = fs.readFileSync(path.join(__dirname, "..", "src", "index.ts"), "utf8")
  assert.match(index, /secretsRequestsSpec\(config\.root/)
  assert.match(index, /secretsFulfillSpec\(config\.root, \{ session, requestId, ttlS \}, value\)/)
  assert.match(index, /secretsDeclineSpec\(config\.root/)
})

test("the GUI uses the native secret prompt and never stores the value", () => {
  const gui = fs.readFileSync(path.join(__dirname, "..", "src", "gui.tsx"), "utf8")
  assert.match(gui, /ctx\.add\(ComposerPill, \{/)
  assert.match(gui, /id: "secrets",/)
  assert.match(gui, /id: "provide-secret"/)
  assert.match(gui, /id: "decline-secret"/)
  assert.match(gui, /ctx\.dialogs\.prompt\(\{[\s\S]*?secret: true/)
  assert.match(gui, /ctx\.dialogs\.confirm\(/)
  assert.match(gui, /secretsFulfill\(\{/)
  assert.match(gui, /secretsDecline\(\{/)
  assert.match(gui, /ctx\.screen\.current\(\)\?\.composer\.insert\(report\.ref\)/)
  assert.match(gui, /setInterval\(/)
  assert.match(gui, /clearInterval\(/)
  assert.match(gui, /createEffect\(/)

  // Only request metadata has a signal; the value is a local variable passed
  // straight to the RPC call and never assigned to a signal or store.
  assert.match(gui, /createSignal<\s*Record<string, SecretRequestRow\[\]>\s*>/)
  assert.doesNotMatch(gui, /createSignal[^\n]*[Ss]ecret[^\n]*[Vv]alue/)
  assert.doesNotMatch(gui, /setPendingSecrets\([^)]*value/)
  assert.doesNotMatch(gui, /ctx\.toast\.show\([\s\S]{0,200}?value/)
})

test("the GUI definition owns the new user-visible strings", () => {
  const definition = fs.readFileSync(
    path.join(__dirname, "..", "src", "gui", "definition.ts"),
    "utf8",
  )
  for (const key of [
    "command.provideSecret",
    "command.declineSecret",
    "secrets.pill.one",
    "secrets.pill.many",
    "secrets.prompt.title",
    "secrets.done.ref",
    "secrets.decline.title",
    "secrets.declined.title",
  ]) {
    assert.match(definition, new RegExp(`"${key.replace(/\./g, "\\.")}"`))
  }
})
