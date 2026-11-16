import {
  Command,
  Panel,
  Plugin,
  SettingsPage,
  Style,
  type Command as CommandItem,
  type PanelProps,
  type PanelTab,
} from "@opencode/plugin/gui"
import { createSignal } from "solid-js"
import { Ozagia } from "./rpc"
import type { Envelope, TestPlan, WorktreeReport } from "./types"
import { failureText, invoke } from "./gui/client"
import { definition } from "./gui/definition"
import { OzagiaPanel } from "./gui/panel"
import { OzagiaSettings } from "./gui/settings"
import { panelCss } from "./gui/styles"

type TabId = "graph" | "status" | "testing" | "research" | "actions"

export default Plugin.define({
  ...definition,
  setup(ctx) {
    ctx.add(Style, panelCss)

    const [lastTestPlan, setLastTestPlan] = createSignal<Envelope<TestPlan> | null>(null)

    const tab = (id: TabId, titleKey: string, pinned = false): PanelTab => ({
      id,
      get title() {
        return ctx.t(titleKey)
      },
      pinned,
      fallback: pinned,
      closable: pinned ? false : "compact",
    })

    // The graph tab is pinned so the panel is discoverable; the other tabs are
    // opened by their commands or the panel launcher and stay closeable.
    const graphTab = tab("graph", "tab.graph", true)
    const statusTab = tab("status", "tab.status")
    const testingTab = tab("testing", "tab.testing")
    const researchTab = tab("research", "tab.research")
    const actionsTab = tab("actions", "tab.actions")

    ctx.add(Panel, {
      id: "ozagia",
      region: "side",
      width: 430,
      mobile: {
        get title() {
          return ctx.t("panel.title")
        },
        order: 70,
        kind: "menu",
        icon: "outline-worktree",
      },
      list: (input) => [
        graphTab,
        ...(input.open.includes("status") ? [statusTab] : []),
        ...(input.open.includes("testing") ? [testingTab] : []),
        ...(input.open.includes("research") ? [researchTab] : []),
        ...(input.open.includes("actions") ? [actionsTab] : []),
      ],
      render: (props: PanelProps) => (
        <OzagiaPanel
          tab={() => props.tab}
          lastPlan={lastTestPlan}
          setLastPlan={setLastTestPlan}
        />
      ),
    })

    ctx.add(SettingsPage, {
      id: "ozagia-settings",
      icon: "settings-gear",
      get title() {
        return ctx.t("settings.title")
      },
      get entries() {
        return [
          {
            id: "ozagia-settings",
            title: ctx.t("settings.title"),
            description: ctx.t("settings.description"),
            keywords: "ozagia settings registry config source scope read-only",
          },
          {
            id: "ozagia-settings-readonly",
            title: ctx.t("settings.readonly"),
            description: ctx.t("settings.readonly.description"),
          },
        ]
      },
      render: () => <OzagiaSettings />,
    })

    const open = (id: TabId) => {
      const session = ctx.sessions.current()
      if (session) ctx.layout.open(`${ctx.id}:${id}`, session, { background: true })
    }

    const openCommand = (id: TabId, commandId: string, titleKey: string) => (): CommandItem => ({
      id: commandId,
      get title() {
        return ctx.t(titleKey)
      },
      get group() {
        return ctx.t("command.group")
      },
      section: "session",
      enabled: !!ctx.sessions.current(),
      run: () => open(id),
    })

    ctx.add(Command, openCommand("graph", "open-graph", "command.openGraph"))
    ctx.add(Command, openCommand("status", "open-status", "command.openStatus"))
    ctx.add(Command, openCommand("testing", "open-testing", "command.openTesting"))
    ctx.add(Command, openCommand("research", "open-research", "command.openResearch"))
    ctx.add(Command, openCommand("actions", "open-actions", "command.openActions"))

    ctx.add(Command, (): CommandItem => ({
      id: "open-settings",
      get title() {
        return ctx.t("command.openSettings")
      },
      description: ctx.t("command.openSettings.description"),
      keywords: "ozagia settings config registry sources",
      get group() {
        return ctx.t("command.group")
      },
      section: "navigation",
      enabled: true,
      run: () => ctx.layout.settings("ozagia-settings"),
    }))

    ctx.add(Command, (): CommandItem => ({
      id: "check-worktree",
      get title() {
        return ctx.t("command.checkWorktree")
      },
      description: ctx.t("command.checkWorktree.description"),
      keywords: "git worktree branch drift policy ozagia",
      get group() {
        return ctx.t("command.group")
      },
      section: "session",
      enabled: !!ctx.sessions.current(),
      run: async () => {
        const session = ctx.sessions.current()
        if (!session) return
        const envelope = await invoke<WorktreeReport>(ctx, () =>
          ctx.client.rpc(Ozagia).worktreeCheck({}),
        )
        if (!envelope.ok || !envelope.data) {
          ctx.toast.show({
            title: "Worktree check failed",
            description: failureText(envelope),
            variant: "error",
            persistent: true,
          })
          return
        }
        const report = envelope.data
        const worktrees = report.worktrees ?? []
        const findings = [
          ...(report.findings ?? []),
          ...worktrees.flatMap((worktree) => worktree.findings ?? []),
        ]
        const failed =
          report.ok === false || findings.some((finding) => finding.level === "error")
        ctx.toast.show({
          title: failed ? "Worktree check has errors" : "Worktree check passed",
          description: `${worktrees.length} worktree(s), ${findings.length} finding(s).`,
          variant: failed ? "error" : "success",
          persistent: failed,
        })
        ctx.layout.open(`${ctx.id}:status`, session, { background: true })
      },
    }))

    ctx.add(Command, (): CommandItem => ({
      id: "plan-tests",
      get title() {
        return ctx.t("command.planTests")
      },
      description: ctx.t("command.planTests.description"),
      keywords: "tests affected plan pytest ozagia",
      get group() {
        return ctx.t("command.group")
      },
      section: "session",
      enabled: !!ctx.sessions.current(),
      run: async () => {
        const session = ctx.sessions.current()
        if (!session) return
        const envelope = await invoke<TestPlan>(ctx, () =>
          ctx.client.rpc(Ozagia).testingPlan({ base: "HEAD" }),
        )
        setLastTestPlan(envelope)
        if (!envelope.ok || !envelope.data) {
          ctx.toast.show({
            title: "Test plan failed",
            description: failureText(envelope),
            variant: "error",
            persistent: true,
          })
          return
        }
        const plan = envelope.data
        const selected = plan.counts?.selected_files ?? 0
        const changed = plan.counts?.changed_files ?? 0
        ctx.toast.show({
          title: plan.refused ? "Test policy refused" : "Test plan ready",
          description: `${selected} selected of ${changed} changed file(s); graph ${plan.graph?.freshness ?? "unknown"}.`,
          variant: plan.refused ? "error" : "success",
          persistent: !!plan.refused,
        })
        ctx.layout.open(`${ctx.id}:testing`, session, { background: true })
      },
    }))
  },
})
