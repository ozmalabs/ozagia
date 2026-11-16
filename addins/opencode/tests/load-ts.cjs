"use strict"

const fs = require("node:fs")
const path = require("node:path")
const Module = require("node:module")
const ts = require("typescript")

/**
 * Transpile one dependency-free TypeScript module to CommonJS in memory.
 *
 * The node:test suite must not import @opencode/plugin (and therefore must not
 * need the GUI host). runner.ts imports only node: built-ins and type-only
 * modules, so this keeps the tests offline and build-free.
 */
function loadTs(file) {
  const source = fs.readFileSync(file, "utf8")
  const output = ts.transpileModule(source, {
    fileName: file,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true,
      isolatedModules: true,
    },
  })

  const mod = new Module(file, module)
  mod.filename = file
  mod.paths = Module._nodeModulePaths(path.dirname(file))
  mod._compile(output.outputText, file)
  return mod.exports
}

module.exports = { loadTs }
