import { Plugin } from "@opencode/plugin/gui"

/**
 * Declarations for the GUI part. The host gives the plugin its id from the
 * server part (`ozagia-addin`); the GUI does not declare one.
 */
export const definition = Plugin.define({
  i18n: {
    en: {
      "panel.title": "ozagia",
      "tab.graph": "Graph",
      "tab.status": "Status",
      "tab.testing": "Testing",
      "command.group": "ozagia",
      "command.openGraph": "ozagia: Open graph panel",
      "command.openStatus": "ozagia: Open status panel",
      "command.openTesting": "ozagia: Open testing panel",
      "command.checkWorktree": "ozagia: Check worktree",
      "command.planTests": "ozagia: Plan tests",
      "command.checkWorktree.description": "Check the current worktree, branch and drift (read-only).",
      "command.planTests.description": "Plan affected tests for HEAD against the working tree (read-only).",
    },
  },
})
