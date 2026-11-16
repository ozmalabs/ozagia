import { For, Show, createMemo, createResource } from "solid-js"
import { usePlugin } from "@opencode/plugin/gui"
import { Ozagia } from "../rpc"
import type {
  Envelope,
  SettingSchemaEntry,
  SettingsSchemaReport,
  SettingsValuesReport,
  SettingValueEntry,
} from "../types"
import { invoke } from "./client"
import type { definition } from "./definition"
import { Badge, FailureView, Loading, type Tone } from "./ui"

interface MergedSetting {
  key: string
  group: string
  value: unknown
  source: string
  type: string
  scope: string
  description: string
  default: unknown
  restartRequired: boolean
}

function valueText(value: unknown): string {
  if (value === null || value === undefined) return "—"
  if (typeof value === "string") return value === "" ? '""' : value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function sourceTone(source: string): Tone {
  if (source === "default") return "muted"
  if (source === "user" || source === "repo") return "ok"
  return "warn"
}

export function OzagiaSettings() {
  const ctx = usePlugin<typeof definition>()

  const [schema, { refetch: refetchSchema }] = createResource<Envelope<SettingsSchemaReport>>(
    () => invoke<SettingsSchemaReport>(ctx, () => ctx.client.rpc(Ozagia).settingsSchema({})),
  )
  const [values, { refetch: refetchValues }] = createResource<Envelope<SettingsValuesReport>>(
    () => invoke<SettingsValuesReport>(ctx, () => ctx.client.rpc(Ozagia).settingsValues({})),
  )

  const failure = createMemo<Envelope | null>(() => {
    const schemaEnvelope = schema()
    if (schemaEnvelope && !schemaEnvelope.ok) return schemaEnvelope
    const valuesEnvelope = values()
    if (valuesEnvelope && !valuesEnvelope.ok) return valuesEnvelope
    return null
  })

  const settings = createMemo<MergedSetting[]>(() => {
    const report = values()
    if (!report?.ok || !report.data?.settings) return []
    const schemaByKey = new Map<string, SettingSchemaEntry>()
    for (const item of schema()?.data?.settings ?? []) schemaByKey.set(item.key, item)
    return Object.entries(report.data.settings)
      .map(([key, item]: [string, SettingValueEntry]) => {
        const declared = schemaByKey.get(key)
        return {
          key,
          group: declared?.group ?? key.split(".", 1)[0],
          value: item.value,
          source: item.source ?? "unknown",
          type: item.type ?? declared?.type ?? "unknown",
          scope: item.scope ?? declared?.scope ?? "unknown",
          description: item.description ?? declared?.description ?? "No description recorded.",
          default: item.default ?? declared?.default,
          restartRequired: item.restart_required ?? declared?.restart_required ?? false,
        }
      })
  })

  const groups = createMemo(() => {
    const out: Array<{ name: string; entries: MergedSetting[] }> = []
    for (const entry of settings()) {
      let group = out.find((item) => item.name === entry.group)
      if (!group) {
        group = { name: entry.group, entries: [] }
        out.push(group)
      }
      group.entries.push(entry)
    }
    return out
  })

  const retry = () => {
    void refetchSchema()
    void refetchValues()
  }

  const loading = () => (schema.loading || values.loading) && !settings().length && !failure()

  return (
    <div class="ozagia-settings">
      <ctx.ui.SettingsLayout
        title={ctx.t("settings.title")}
        description={ctx.t("settings.description")}
      >
        <Show when={loading()}>
          <ctx.ui.SettingsSection title={ctx.t("settings.loading")}>
            <ctx.ui.SettingsRow
              id="ozagia-settings-loading"
              title={ctx.t("settings.loading")}
              description={ctx.t("settings.loading.description")}
            >
              <Loading label={ctx.t("settings.loading")} />
            </ctx.ui.SettingsRow>
          </ctx.ui.SettingsSection>
        </Show>

        <Show when={failure()}>
          {(envelope) => (
            <ctx.ui.SettingsSection
              title={ctx.t("settings.unavailable")}
              footer={<FailureView envelope={envelope()} onRetry={retry} />}
            >
              <ctx.ui.SettingsRow
                id="ozagia-settings-unavailable"
                title={ctx.t("settings.unavailable")}
                description={ctx.t("settings.unavailable.description")}
              />
            </ctx.ui.SettingsSection>
          )}
        </Show>

        <Show when={settings().length > 0 && !failure()}>
          <ctx.ui.SettingsSection
            title={ctx.t("settings.scope")}
            footer={
              <div class="ozagia-note">
                {`Root: ${values()?.data?.root ?? "unknown"} · repo config: ${values()?.data?.locations?.repo_config ?? "not recorded"}`}
              </div>
            }
          >
            <ctx.ui.SettingsRow
              id="ozagia-settings-readonly"
              title={ctx.t("settings.readonly")}
              description={ctx.t("settings.readonly.description")}
            >
              <Badge tone="muted">{ctx.t("settings.readonly.badge")}</Badge>
            </ctx.ui.SettingsRow>
          </ctx.ui.SettingsSection>

          <For each={groups()}>
            {(group) => (
              <ctx.ui.SettingsSection title={group.name}>
                <For each={group.entries}>
                  {(entry) => (
                    <ctx.ui.SettingsRow
                      id={`ozagia-${entry.key}`}
                      title={<code>{entry.key}</code>}
                      description={
                        <>
                          <span>{entry.description}</span>
                          <span class="ozagia-note">
                            {` · ${entry.type} · scope ${entry.scope} · default ${valueText(entry.default)}${entry.restartRequired ? " · restart required" : ""}`}
                          </span>
                        </>
                      }
                    >
                      <span class="ozagia-setting-value">
                        <code>{valueText(entry.value)}</code>
                        <Badge tone={sourceTone(entry.source)}>{`source: ${entry.source}`}</Badge>
                      </span>
                    </ctx.ui.SettingsRow>
                  )}
                </For>
              </ctx.ui.SettingsSection>
            )}
          </For>
        </Show>

        <Show when={!loading() && !failure() && settings().length === 0}>
          <ctx.ui.SettingsSection title={ctx.t("settings.empty")}>
            <ctx.ui.SettingsRow
              id="ozagia-settings-empty"
              title={ctx.t("settings.empty")}
              description={ctx.t("settings.empty.description")}
            />
          </ctx.ui.SettingsSection>
        </Show>
      </ctx.ui.SettingsLayout>
    </div>
  )
}
