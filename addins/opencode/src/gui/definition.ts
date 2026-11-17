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
      "tab.profile": "Profile",
      "tab.testing": "Testing",
      "tab.research": "Research",
      "tab.actions": "Actions",
      "tab.tasks": "Tasks",
      "tab.tools": "Tools",
      "command.group": "ozagia",
      "command.openGraph": "ozagia: Open graph panel",
      "command.openStatus": "ozagia: Open status panel",
      "command.openProfile": "ozagia: Open session profile",
      "command.openTesting": "ozagia: Open testing panel",
      "command.openResearch": "ozagia: Open research panel",
      "command.openActions": "ozagia: Open action previews",
      "command.openTasks": "ozagia: Open task classes",
      "command.openTools": "ozagia: Open tools panel",
      "command.openSettings": "ozagia: Open settings page",
      "command.checkWorktree": "ozagia: Check worktree",
      "command.planTests": "ozagia: Plan tests",
      "command.checkWorktree.description": "Check the current worktree, branch and drift (read-only).",
      "command.planTests.description": "Plan affected tests for HEAD against the working tree (read-only).",
      "command.openResearch.description": "Inspect a recorded research investigation (read-only).",
      "command.openActions.description": "Preview a process or connection action, then apply it only with a second explicit confirmation.",
      "command.openTasks.description": "Browse versioned task classes or run one through the read-only local executor after a confirmation.",
      "command.openTools.description": "Read host tool readiness, install previews and the model brief (read-only; nothing is installed).",
      "command.openSettings.description": "Read the effective settings registry and its sources.",
      "settings.title": "ozagia settings",
      "settings.description": "Read-only view of the effective settings registry. Values show their source, scope and description; this page cannot write a setting.",
      "settings.loading": "Loading settings",
      "settings.loading.description": "Reading the registry schema and effective values.",
      "settings.unavailable": "Settings unavailable",
      "settings.unavailable.description": "The settings registry could not be read. The page stays empty and never writes.",
      "settings.scope": "Scope",
      "settings.readonly": "Read-only",
      "settings.readonly.description": "No control on this page writes a value. Use the ozagia settings CLI after reviewing a preview.",
      "settings.readonly.badge": "no writes",
      "settings.empty": "No settings returned",
      "settings.empty.description": "The registry returned no values.",
    },
  },
})
