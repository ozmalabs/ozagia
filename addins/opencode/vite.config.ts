import { defineConfig } from "vite"
import { gui } from "@opencode/plugin/gui/vite"

// The preset compiles Solid, emits one CommonJS file and keeps the modules
// the desktop app provides (solid-js, @opencode/*, effect) external.
export default defineConfig({
  plugins: [gui()],
})
