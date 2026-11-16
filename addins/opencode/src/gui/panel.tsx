import { Match, Switch } from "solid-js"
import type { PanelTab } from "@opencode/plugin/gui"
import type { Envelope, TestPlan } from "../types"
import { ActionsTab } from "./actions"
import { GraphTab } from "./graph"
import { ResearchTab } from "./research"
import { StatusTab } from "./status"
import { TestingTab } from "./testing"

export function OzagiaPanel(props: {
  tab: () => PanelTab
  lastPlan: () => Envelope<TestPlan> | null
  setLastPlan: (value: Envelope<TestPlan> | null) => void
}) {
  return (
    <div class="ozagia-panel">
      <Switch fallback={<StatusTab />}>
        <Match when={props.tab().id === "graph"}>
          <GraphTab />
        </Match>
        <Match when={props.tab().id === "status"}>
          <StatusTab />
        </Match>
        <Match when={props.tab().id === "testing"}>
          <TestingTab lastPlan={props.lastPlan} setLastPlan={props.setLastPlan} />
        </Match>
        <Match when={props.tab().id === "research"}>
          <ResearchTab />
        </Match>
        <Match when={props.tab().id === "actions"}>
          <ActionsTab />
        </Match>
      </Switch>
    </div>
  )
}
