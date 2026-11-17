"use strict"

/**
 * Offline tests for the read-only Tools tab.
 *
 * The suite loads the dependency-free TypeScript modules directly and uses a
 * fake `python` executable for the no-shell check. No network, model, real
 * ozagia CLI or OpenCode package is involved.
 */

const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const { after, test } = require("node:test")
const { loadTs } = require("./load-ts.cjs")

const runner = loadTs(path.join(__dirname, "..", "src", "runner.ts"))
const brief = loadTs(path.join(__dirname, "..", "src", "gui", "toolchain-brief.ts"))
const schemas = loadTs(path.join(__dirname, "..", "src", "schemas.ts"))

const {
  InputError,
  runOzagia,
  toolchainCapabilities,
  toolchainEnvSpec,
  toolchainListSpec,
  toolchainPaths,
  toolchainPlanSpec,
} = runner
const {
  MAX_TOOLCHAIN_CAPABILITIES,
  MAX_TOOLCHAIN_PATHS,
  capabilityState,
  formatProvides,
  installPreviewRows,
  splitToolchainCapabilities,
  splitToolchainPaths,
  toolchainBrief,
  toolchainEnvironmentLine,
  toolchainInputIssue,
} = brief

const ROOT = "/tmp/ozagia repo"
const tempDirs = []

after(() => {
  for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true })
})

function tempRoot(prefix = "ozagia-toolchain-test-") {
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

test("toolchain list and env builders are read-only and bounded", () => {
  assert.deepEqual(toolchainListSpec(ROOT), {
    module: "ozagia.toolchain_cli",
    args: ["list", "--json"],
    display: "ozagia.toolchain_cli list --json",
  })
  // The CLI's `list` has no --root flag; passing one would be an argparse error.
  assert.ok(!toolchainListSpec(ROOT).args.includes("--root"))

  assert.deepEqual(toolchainEnvSpec(ROOT, {}).args, ["env", "--root", ROOT, "--json"])
  assert.deepEqual(toolchainEnvSpec(ROOT, { probeSudo: false }).args, [
    "env",
    "--root",
    ROOT,
    "--json",
    "--no-sudo",
  ])
  assert.equal(
    toolchainEnvSpec(ROOT, { probeSudo: false }).display,
    "ozagia.toolchain_cli env --json --no-sudo",
  )
  assert.deepEqual(toolchainEnvSpec(ROOT, { probeSudo: true }).args, [
    "env",
    "--root",
    ROOT,
    "--json",
  ])
  assert.throws(() => toolchainEnvSpec(ROOT, { probeSudo: "no" }), InputError)
})

test("toolchain plan builder repeats capability and path flags", () => {
  const spec = toolchainPlanSpec(ROOT, {
    capabilities: ["pdf", "documents"],
    paths: ["report.pdf", "/tmp/a repo/budget.xlsx"],
    probeVersions: false,
    probeSudo: false,
  })
  assert.deepEqual(spec.args, [
    "plan",
    "--root",
    ROOT,
    "--capability",
    "pdf",
    "--capability",
    "documents",
    "--path",
    "report.pdf",
    "--path",
    "/tmp/a repo/budget.xlsx",
    "--json",
    "--no-versions",
    "--no-sudo",
  ])
  assert.equal(
    spec.display,
    "ozagia.toolchain_cli plan --capability pdf --capability documents --json --no-versions --no-sudo",
  )
  assert.ok(!spec.display.includes("report.pdf"))
  assert.ok(!spec.display.includes("budget.xlsx"))

  assert.deepEqual(toolchainPlanSpec(ROOT, { capabilities: "media" }).args, [
    "plan",
    "--root",
    ROOT,
    "--capability",
    "media",
    "--json",
  ])
  assert.deepEqual(toolchainPlanSpec(ROOT, { paths: "clip.mp4" }).args, [
    "plan",
    "--root",
    ROOT,
    "--path",
    "clip.mp4",
    "--json",
  ])
  assert.deepEqual(toolchainPlanSpec(ROOT, {}).args, [
    "plan",
    "--root",
    ROOT,
    "--json",
  ])
  assert.deepEqual(
    toolchainPlanSpec(ROOT, { probeVersions: true, probeSudo: true }).args,
    ["plan", "--root", ROOT, "--json"],
  )
})

test("toolchain capability ids and paths are bounded before argv", () => {
  assert.deepEqual(toolchainCapabilities("pdf"), ["pdf"])
  assert.deepEqual(toolchainCapabilities(["pdf", "pdf", "ocr"]), ["pdf", "ocr"])
  assert.deepEqual(toolchainCapabilities(" documents "), ["documents"])
  assert.deepEqual(toolchainPaths(["a.pdf", "a.pdf", "b/c.docx"]), ["a.pdf", "b/c.docx"])
  assert.deepEqual(toolchainPaths("/tmp/a repo/report.pdf"), ["/tmp/a repo/report.pdf"])

  for (const value of [
    "PDF",
    "pdf ocr",
    "pdf;rm",
    "--capability",
    "../pdf",
    "pdf/ocr",
    "pd\nf",
    "",
    "a".repeat(33),
    undefined,
    null,
    42,
  ]) {
    if (value === undefined || value === null) {
      assert.deepEqual(toolchainCapabilities(value), [])
      continue
    }
    assert.throws(() => toolchainCapabilities(value), InputError)
  }
  assert.throws(
    () => toolchainCapabilities(Array(MAX_TOOLCHAIN_CAPABILITIES + 1).fill("pdf")),
    InputError,
  )
  assert.throws(
    () => toolchainPlanSpec(ROOT, { capabilities: Array(17).fill("pdf") }),
    InputError,
  )
  assert.throws(
    () => toolchainPlanSpec(ROOT, { paths: Array(MAX_TOOLCHAIN_PATHS + 1).fill("a.pdf") }),
    InputError,
  )
  assert.throws(
    () => toolchainPlanSpec(ROOT, { paths: ["a".repeat(2001)] }),
    InputError,
  )
  for (const value of ["/tmp/a\nb.pdf", "/tmp/a\tb.pdf", "/tmp/a\u0000b.pdf", "/tmp/a\u007fb.pdf"]) {
    assert.throws(() => toolchainPaths(value), InputError)
    assert.throws(() => toolchainPlanSpec(ROOT, { paths: value }), InputError)
  }
})

test("toolchain plan spawns argv without a shell", async () => {
  const marker = path.join(tempRoot(), "shell-marker")
  const pathValue = `report.pdf;touch ${marker}`
  const script = [
    "const args = process.argv.slice(2)",
    "process.stdout.write(JSON.stringify({ argv: args }))",
  ].join("\n")
  const { dir, file } = fakePython(script)

  const result = await runOzagia(
    { python: file, root: dir, timeoutMs: 2000 },
    toolchainPlanSpec(dir, {
      capabilities: ["pdf"],
      paths: [pathValue],
      probeVersions: false,
      probeSudo: false,
    }),
  )
  assert.equal(result.ok, true)
  assert.ok(result.data.argv.includes("ozagia.toolchain_cli"))
  assert.ok(result.data.argv.includes(pathValue))
  assert.deepEqual(result.data.argv.slice(-2), ["--no-versions", "--no-sudo"])
  assert.equal(fs.existsSync(marker), false)
})

test("toolchain RPC schemas bound the inputs and stay read-only", () => {
  assert.equal(schemas.TOOLCHAIN_ENV_INPUT.additionalProperties, false)
  assert.deepEqual(schemas.TOOLCHAIN_ENV_INPUT.properties, {
    probeSudo: { type: "boolean" },
  })

  const plan = schemas.TOOLCHAIN_PLAN_INPUT
  assert.equal(plan.additionalProperties, false)
  assert.deepEqual(plan.properties.probeVersions, { type: "boolean" })
  assert.deepEqual(plan.properties.probeSudo, { type: "boolean" })
  assert.equal(plan.properties.capabilities.oneOf[1].maxItems, 16)
  assert.equal(plan.properties.capabilities.oneOf[1].items.maxLength, 32)
  assert.equal(
    plan.properties.capabilities.oneOf[1].items.pattern,
    "^[a-z][a-z0-9_-]{0,31}$",
  )
  assert.equal(plan.properties.paths.oneOf[1].maxItems, 32)
  assert.equal(plan.properties.paths.oneOf[1].items.maxLength, 2000)
  assert.equal(
    plan.properties.paths.oneOf[1].items.pattern,
    "^[^\\u0000-\\u001f\\u007f]*$",
  )

  const source = fs.readFileSync(path.join(__dirname, "..", "src", "rpc.ts"), "utf8")
  assert.match(
    source,
    /toolchainEnv: \{ input: TOOLCHAIN_ENV_INPUT, output: ENVELOPE_OUTPUT \}/,
  )
  assert.match(
    source,
    /toolchainList: \{ input: EMPTY_INPUT, output: ENVELOPE_OUTPUT \}/,
  )
  assert.match(
    source,
    /toolchainPlan: \{ input: TOOLCHAIN_PLAN_INPUT, output: ENVELOPE_OUTPUT \}/,
  )
  assert.doesNotMatch(source, /toolchainInstall|toolchainApply/)
})

test("toolchain GUI input helpers split and validate locally", () => {
  assert.deepEqual(splitToolchainCapabilities(" PDF, documents\nmedia "), [
    "pdf",
    "documents",
    "media",
  ])
  assert.deepEqual(splitToolchainPaths("a.pdf, b.pdf\nc.pdf"), [
    "a.pdf",
    "b.pdf",
    "c.pdf",
  ])
  assert.equal(toolchainInputIssue(["pdf"], ["report.pdf"]), null)
  assert.match(
    toolchainInputIssue(Array(MAX_TOOLCHAIN_CAPABILITIES + 1).fill("pdf"), []),
    /At most 16/,
  )
  assert.match(toolchainInputIssue(["PDF"], []), /lower-case/)
  assert.match(
    toolchainInputIssue([], Array(MAX_TOOLCHAIN_PATHS + 1).fill("a.pdf")),
    /At most 32/,
  )
  assert.match(toolchainInputIssue([], ["a".repeat(2001)]), /longer than 2000/)
  assert.match(toolchainInputIssue([], ["a\nb.pdf"]), /control characters/)
})

test("toolchain capability state and install previews are bounded", () => {
  assert.equal(capabilityState({ id: "pdf", ready: true }), "ready")
  assert.equal(capabilityState({ id: "pdf", ready: false, missing: [{ id: "qpdf" }] }), "missing")
  assert.equal(
    capabilityState({ id: "pdf", ready: false, blocked: true, missing: [] }),
    "blocked",
  )
  assert.equal(
    capabilityState({ id: "research", ready: false, remote: [{ id: "exa" }] }),
    "remote",
  )
  assert.equal(
    capabilityState({ id: "browser", ready: false, unprobed: [{ id: "puppeteer" }] }),
    "unprobed",
  )
  assert.equal(formatProvides({ id: "x", provides: ["a", "b", "c", "d", "e"] }, 3), "a, b, c, +2 more")
  assert.equal(formatProvides({ id: "x" }), "—")

  const installs = Array.from({ length: 10 }, (_, index) => ({
    tool: `tool-${index}`,
    command: `echo ${index}`,
  }))
  assert.equal(installPreviewRows({ id: "x", install: installs }, 8).length, 8)
  assert.equal(installPreviewRows({ id: "x", install: installs }, 0).length, 0)
})

function fakeEnvironment(allowInstall = false) {
  return {
    platform: {
      system: "Linux",
      arch: "x86_64",
      distro: { id: "ubuntu", pretty_name: "Ubuntu 24.04", version_id: "24.04" },
    },
    managers: {
      npm: { path: "/usr/bin/npm" },
      uv: { path: "/usr/bin/uv" },
    },
    sudo: { available: true, probed: true, noninteractive: true },
    approval: {
      allow_install: allowInstall,
      allow_network: allowInstall,
      allow_sudo: false,
      sources: {},
    },
  }
}

function fakePlan(allowInstall = false) {
  const install = {
    tool: "tesseract",
    manager: "apt-get",
    command: "sudo apt-get install -y tesseract-ocr",
    install_kind: "system-package",
    requires_sudo: true,
    requires_network: true,
    blocked_by: ["install", "sudo"],
    blocked_reasons: ["installation not approved", "sudo not approved"],
    would_run: false,
    recommended: true,
  }
  return {
    environment: fakeEnvironment(allowInstall),
    capabilities: [
      {
        id: "pdf",
        ready: true,
        available: [
          {
            id: "pypdf",
            kind: "python",
            extra: "pdf",
            install_kind: "python-extra",
            provides: ["extract text", "merge/split", "fill forms"],
            usable: true,
            preferred_by: ["claude"],
          },
          {
            id: "pdftotext",
            kind: "binary",
            install_kind: "system-package",
            provides: ["extract text"],
            usable: true,
            recommended: true,
          },
        ],
        missing: [],
        remote: [],
        unprobed: [],
        install: [],
      },
      {
        id: "research",
        ready: false,
        available: [],
        missing: [],
        remote: [{ id: "exa", credentialed: true, provides: ["web search"] }],
        unprobed: [],
        install: [],
      },
      {
        id: "ocr",
        ready: false,
        available: [
          {
            id: "pytesseract",
            kind: "python",
            provides: ["OCR wrapper"],
            usable: false,
            missing_requirements: ["tesseract"],
          },
        ],
        missing: [{ id: "tesseract", provides: ["OCR"] }],
        remote: [],
        unprobed: [],
        install: [install],
        recommended_install: install,
      },
    ],
  }
}

test("toolchain environment line and model brief mirror the CLI summary", () => {
  const line = toolchainEnvironmentLine(fakeEnvironment())
  assert.match(line, /Linux x86_64/)
  assert.match(line, /Ubuntu 24\.04 \(id ubuntu\)/)
  assert.match(line, /managers: npm, uv/)
  assert.match(line, /sudo usable/)
  assert.match(line, /install not approved, network not approved, sudo not approved/)

  const plan = fakePlan()
  const text = toolchainBrief(plan)
  assert.match(text, /^\[ozagia\] tools: Linux x86_64/)
  assert.match(text, /pdf: ready; prefer pdftotext \(system package: extract text\)/)
  assert.match(text, /research: remote only; exa \(network\/credentials\)/)
  assert.match(
    text,
    /ocr: missing; installed but unusable: pytesseract \(needs tesseract\); tesseract; recommended system package; installation not approved/,
  )
  assert.doesNotMatch(text, /apt-get install/)
  assert.match(text, /nothing was installed, and every install preview is would_run: false/)

  const family = toolchainBrief(plan, "claude-code")
  assert.match(family, /pdf: ready; for claude prefer pypdf \(Python extra pdf: extract text, merge\/split, fill forms\)/)
  assert.match(toolchainBrief(plan, "codex"), /prefer pdftotext/)

  const approved = toolchainBrief(fakePlan(true))
  assert.match(approved, /install tesseract with sudo apt-get install -y tesseract-ocr \(system package\)/)
  assert.match(approved, /blocked: installation not approved; sudo not approved/)
  assert.doesNotMatch(approved, /would_run: true/)
})

test("toolchain model brief is compact and handles empty requests", () => {
  assert.equal(toolchainBrief(null), "")
  const empty = toolchainBrief({ environment: fakeEnvironment(), capabilities: [] })
  assert.match(empty, /no external-tool capability requested/)
  assert.match(empty, /nothing was installed/)

  const reports = Array.from({ length: 10 }, (_, index) => ({
    id: `cap-${index}`,
    ready: true,
    available: [{ id: `tool-${index}`, kind: "binary", provides: ["work"], usable: true }],
  }))
  const text = toolchainBrief({ environment: fakeEnvironment(), capabilities: reports })
  assert.match(text, /cap-7: ready/)
  assert.doesNotMatch(text, /cap-8: ready/)
  assert.match(text, /\.\.\. and 2 more capability reports/)
})

test("the Tools tab is registered with a command and a panel match", () => {
  const gui = fs.readFileSync(path.join(__dirname, "..", "src", "gui.tsx"), "utf8")
  assert.match(gui, /type TabId = .*"tools"/)
  assert.match(gui, /const toolsTab = tab\("tools", "tab.tools"\)/)
  assert.match(gui, /input\.open\.includes\("tools"\) \? \[toolsTab\]/)
  assert.match(gui, /openCommand\("tools", "open-tools", "command\.openTools"\)/)

  const definition = fs.readFileSync(
    path.join(__dirname, "..", "src", "gui", "definition.ts"),
    "utf8",
  )
  assert.match(definition, /"tab\.tools": "Tools"/)
  assert.match(definition, /"command\.openTools": "ozagia: Open tools panel"/)

  const panel = fs.readFileSync(
    path.join(__dirname, "..", "src", "gui", "panel.tsx"),
    "utf8",
  )
  assert.match(panel, /props\.tab\(\)\.id === "tools"/)
  assert.match(panel, /<ToolchainTab \/>/)
})

test("the Tools tab has no install or apply surface", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "src", "gui", "toolchain.tsx"), "utf8")
  const rpcCalls = [...source.matchAll(/rpc\(Ozagia\)\.([A-Za-z0-9_]+)\(/g)].map(
    (match) => match[1],
  )
  assert.deepEqual([...new Set(rpcCalls)].sort(), [
    "toolchainEnv",
    "toolchainList",
    "toolchainPlan",
  ])

  const buttons = [...source.matchAll(/<button[\s\S]*?<\/button>/g)].map((match) => match[0])
  assert.ok(buttons.length > 0)
  for (const button of buttons) {
    assert.doesNotMatch(button, /\bInstall\b|\bApply\b|\bRun task\b/i)
  }
  assert.doesNotMatch(source, /actions_cli|tasks_cli|settings_cli|installTool|applyTool/)

  for (const spec of [
    toolchainListSpec(ROOT),
    toolchainEnvSpec(ROOT, {}),
    toolchainPlanSpec(ROOT, { capabilities: "pdf", paths: "a.pdf" }),
  ]) {
    assert.equal(spec.module, "ozagia.toolchain_cli")
    for (const banned of ["install", "apply", "--require-ready", "--model-family", "--allow-live"]) {
      assert.ok(!spec.args.includes(banned), `unexpected ${banned} in ${spec.display}`)
    }
  }
})
