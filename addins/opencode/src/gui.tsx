import {
  Command,
  ComposerPill,
  Panel,
  Plugin,
  SettingsPage,
  Style,
  type Command as CommandItem,
  type PanelProps,
  type PanelTab,
} from "@opencode/plugin/gui"
import { createEffect, createSignal, onCleanup } from "solid-js"
import { Ozagia } from "./rpc"
import type {
  Envelope,
  SecretFulfilledReport,
  SecretRequestRow,
  SecretRequestsReport,
  TestPlan,
  WorktreeReport,
} from "./types"
import { failureText, invoke } from "./gui/client"
import { definition } from "./gui/definition"
import { OzagiaPanel } from "./gui/panel"
import { OzagiaSettings } from "./gui/settings"
import { panelCss } from "./gui/styles"

type TabId = "graph" | "status" | "profile" | "testing" | "research" | "actions" | "tasks" | "tools"

export default Plugin.define({
  ...definition,
  setup(ctx) {
    ctx.add(Style, panelCss)

    const [lastTestPlan, setLastTestPlan] = createSignal<Envelope<TestPlan> | null>(null)

    // Pending secret requests are metadata only: ids, labels, purposes and
    // expiry. A value is never written to this (or any other) signal.
    const [pendingSecrets, setPendingSecrets] = createSignal<
      Record<string, SecretRequestRow[]>
    >({})
    const SECRET_POLL_MS = 4000

    const secretRows = (sessionId: string) => pendingSecrets()[sessionId] ?? []
    const isPendingSecret = (row: SecretRequestRow) =>
      row.state === "pending" && typeof row.request_id === "string" && !!row.request_id
    const pendingSecretsFor = (sessionId: string) => secretRows(sessionId).filter(isPendingSecret)
    const describeSecret = (row: SecretRequestRow) =>
      row.label || row.purpose || row.request_id

    const formatSecretExpiry = (expires: number | null | undefined): string => {
      if (typeof expires !== "number" || !Number.isFinite(expires)) return ""
      try {
        return new Date(expires * 1000).toLocaleString(ctx.locale.locale())
      } catch {
        return ""
      }
    }

    const refreshSecretRequests = async (
      sessionId: string,
    ): Promise<Envelope<SecretRequestsReport>> => {
      const envelope = await invoke<SecretRequestsReport>(ctx, () =>
        ctx.client.rpc(Ozagia).secretsRequests({ session: sessionId }),
      )
      if (envelope.ok && envelope.data) {
        setPendingSecrets((previous) => ({
          ...previous,
          [sessionId]: envelope.data?.requests ?? [],
        }))
      }
      return envelope
    }

    // Poll only while a session is routed. The effect's cleanup clears the
    // timer on a route change and when the plugin goes away.
    createEffect(() => {
      const session = ctx.sessions.current()
      if (!session) {
        setPendingSecrets({})
        return
      }
      const sessionId = session.id
      let stopped = false
      let busy = false
      const load = async () => {
        if (stopped || busy) return
        busy = true
        try {
          await refreshSecretRequests(sessionId)
        } finally {
          busy = false
        }
      }
      void load()
      const timer = setInterval(() => {
        void load()
      }, SECRET_POLL_MS)
      onCleanup(() => {
        stopped = true
        clearInterval(timer)
      })
    })
    onCleanup(() => setPendingSecrets({}))

    const chooseSecretRequest = async (
      sessionId: string,
      preselected?: SecretRequestRow,
    ): Promise<SecretRequestRow | undefined> => {
      if (preselected) return preselected
      let rows = pendingSecretsFor(sessionId)
      if (!rows.length) {
        const envelope = await refreshSecretRequests(sessionId)
        if (envelope.ok && envelope.data) {
          rows = (envelope.data.requests ?? []).filter(isPendingSecret)
        }
      }
      if (rows.length === 1) return rows[0]
      if (rows.length > 1) {
        const picked = await ctx.dialogs.select<string>({
          title: ctx.t("secrets.select.title"),
          placeholder: ctx.t("secrets.select.placeholder"),
          options: rows.map((row) => ({
            title: describeSecret(row),
            value: row.request_id,
            description: row.purpose ?? undefined,
            group: ctx.t("secrets.select.group"),
          })),
        })
        if (!picked) return undefined
        return rows.find((row) => row.request_id === picked)
      }
      const requestId = await ctx.dialogs.prompt({
        title: ctx.t("secrets.requestId.title"),
        description: ctx.t("secrets.requestId.description"),
        placeholder: "req_…",
      })
      if (!requestId || !requestId.trim()) return undefined
      return { request_id: requestId.trim(), state: "pending", label: null, purpose: null }
    }

    const provideSecret = async (
      sessionId: string,
      preselected?: SecretRequestRow,
    ): Promise<void> => {
      const request = await chooseSecretRequest(sessionId, preselected)
      if (!request) return
      const label = describeSecret(request)
      const description = [
        request.purpose,
        request.target ? ctx.t("secrets.prompt.target", { target: request.target }) : "",
        ctx.t("secrets.prompt.body"),
      ]
        .filter(Boolean)
        .join("\n\n")
      // The host's native secret prompt is the only place the value is read.
      // It stays a local variable and goes straight to the RPC call below.
      const value = await ctx.dialogs.prompt({
        title: ctx.t("secrets.prompt.title", { label }),
        description,
        placeholder: ctx.t("secrets.prompt.placeholder"),
        secret: true,
        submit: ctx.t("secrets.prompt.submit"),
      })
      if (value === undefined) return
      const envelope = await invoke<SecretFulfilledReport>(ctx, () =>
        ctx.client.rpc(Ozagia).secretsFulfill({
          session: sessionId,
          requestId: request.request_id,
          value,
        }),
      )
      await refreshSecretRequests(sessionId)
      if (!envelope.ok || !envelope.data) {
        await ctx.dialogs.alert({
          title: ctx.t("secrets.fail.title"),
          description: failureText(envelope),
        })
        return
      }
      const report = envelope.data
      const lines = [
        report.ref ? ctx.t("secrets.done.ref", { ref: report.ref }) : "",
        report.fingerprint
          ? ctx.t("secrets.done.fingerprint", { fingerprint: report.fingerprint })
          : "",
        report.expires
          ? ctx.t("secrets.done.expires", { expires: formatSecretExpiry(report.expires) })
          : "",
      ].filter(Boolean)
      await ctx.dialogs.alert({
        title: ctx.t("secrets.done.title"),
        description: lines.join("\n"),
      })
      if (report.ref) ctx.screen.current()?.composer.insert(report.ref)
    }

    const declineSecret = async (
      sessionId: string,
      preselected?: SecretRequestRow,
    ): Promise<void> => {
      const request = await chooseSecretRequest(sessionId, preselected)
      if (!request) return
      const label = describeSecret(request)
      const confirmed = await ctx.dialogs.confirm({
        title: ctx.t("secrets.decline.title", { label }),
        description: request.purpose || ctx.t("secrets.decline.description"),
        confirm: ctx.t("secrets.decline.confirm"),
        cancel: ctx.t("secrets.decline.cancel"),
        danger: true,
      })
      if (!confirmed) return
      const envelope = await invoke<SecretRequestsReport>(ctx, () =>
        ctx.client.rpc(Ozagia).secretsDecline({
          session: sessionId,
          requestId: request.request_id,
        }),
      )
      await refreshSecretRequests(sessionId)
      if (!envelope.ok) {
        await ctx.dialogs.alert({
          title: ctx.t("secrets.fail.title"),
          description: failureText(envelope),
        })
        return
      }
      await ctx.dialogs.alert({
        title: ctx.t("secrets.declined.title"),
        description: ctx.t("secrets.declined.body", { label }),
      })
    }

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
    const profileTab = tab("profile", "tab.profile")
    const testingTab = tab("testing", "tab.testing")
    const researchTab = tab("research", "tab.research")
    const actionsTab = tab("actions", "tab.actions")
    const tasksTab = tab("tasks", "tab.tasks")
    const toolsTab = tab("tools", "tab.tools")

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
        ...(input.open.includes("profile") ? [profileTab] : []),
        ...(input.open.includes("testing") ? [testingTab] : []),
        ...(input.open.includes("research") ? [researchTab] : []),
        ...(input.open.includes("actions") ? [actionsTab] : []),
        ...(input.open.includes("tasks") ? [tasksTab] : []),
        ...(input.open.includes("tools") ? [toolsTab] : []),
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

    ctx.add(ComposerPill, {
      id: "secrets",
      order: 40,
      show(input) {
        const pending = pendingSecretsFor(input.session.id)
        if (!pending.length) return undefined
        const first = pending[0]
        const label =
          pending.length === 1
            ? ctx.t("secrets.pill.one", { label: describeSecret(first) })
            : ctx.t("secrets.pill.many", { count: pending.length })
        return {
          label,
          icon: "lock",
          tone: "warning",
          title: ctx.t("secrets.pill.title"),
          rows: pending.slice(0, 8).map((row) => ({
            id: row.request_id,
            label: describeSecret(row),
            detail: row.purpose || undefined,
            tone: "warning",
            run: () => {
              void provideSecret(input.session.id, row)
            },
          })),
        }
      },
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
    ctx.add(Command, openCommand("profile", "open-profile", "command.openProfile"))
    ctx.add(Command, openCommand("testing", "open-testing", "command.openTesting"))
    ctx.add(Command, openCommand("research", "open-research", "command.openResearch"))
    ctx.add(Command, openCommand("actions", "open-actions", "command.openActions"))
    ctx.add(Command, openCommand("tasks", "open-tasks", "command.openTasks"))
    ctx.add(Command, openCommand("tools", "open-tools", "command.openTools"))

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

    ctx.add(Command, (): CommandItem => ({
      id: "provide-secret",
      get title() {
        return ctx.t("command.provideSecret")
      },
      description: ctx.t("command.provideSecret.description"),
      keywords: "secret token credential request secure prompt ozagia",
      get group() {
        return ctx.t("command.group")
      },
      section: "session",
      featured: true,
      enabled: !!ctx.sessions.current(),
      run: async () => {
        const session = ctx.sessions.current()
        if (!session) return
        await provideSecret(session.id)
      },
    }))

    ctx.add(Command, (): CommandItem => ({
      id: "decline-secret",
      get title() {
        return ctx.t("command.declineSecret")
      },
      description: ctx.t("command.declineSecret.description"),
      keywords: "secret token credential request decline deny ozagia",
      get group() {
        return ctx.t("command.group")
      },
      section: "session",
      enabled: !!ctx.sessions.current(),
      run: async () => {
        const session = ctx.sessions.current()
        if (!session) return
        await declineSecret(session.id)
      },
    }))
  },
})
