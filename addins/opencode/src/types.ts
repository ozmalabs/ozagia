/** Shared shapes for the RPC envelope and the read-only panel. */

export interface EnvelopeError {
  code: string
  message: string
  detail?: string
}

export interface Envelope<T = unknown> {
  ok: boolean
  command: string
  data: T | null
  error: EnvelopeError | null
  exitCode?: number | null
}

export interface ApertureSummary {
  id: string
  title: string
  description: string
  grain: string
  layout: string
}

export interface ViewNode {
  id: string
  type: string
  raw_type?: string
  label: string
  group: string
  attrs?: Record<string, unknown>
  weight?: number
  key?: string
}

export interface ViewEdge {
  id: string
  src: string
  tgt: string
  type: string
  raw_type?: string
  attrs?: Record<string, unknown>
}

export interface ViewGroup {
  id: string
  label: string
  count: number
  collapsed: number
}

export interface ViewModel {
  schema: number
  aperture: string
  snapshot: {
    root?: string | null
    head?: string | null
    freshness?: string | null
    generated_at?: string | null
  }
  nodes: ViewNode[]
  edges: ViewEdge[]
  groups: ViewGroup[]
  legend: { color?: Record<string, string>; encoding?: Record<string, string> }
  layout: { kind?: string; seed?: number }
  provenance: {
    sources?: string[]
    derived?: boolean
    graph?: string
    graph_schema?: number | string
  }
  omitted: {
    nodes: number
    edges: number
    reason: string
    by?: Record<string, number>
  }
}

export interface Finding {
  id?: string
  level: string
  message: string
  worktree?: string
  command?: string
  dirty?: Record<string, number>
}

export interface WorktreeInfo {
  path: string
  head?: string | null
  branch?: string | null
  detached?: boolean
  ahead?: number
  behind?: number
  dirty?: Record<string, number>
  expected?: { branch?: string | null; base?: string | null; name?: string | null; source?: string | null; protected?: boolean }
  filesystem?: {
    mount?: string | null
    fstype?: string | null
    free_gb?: number | null
    free_pct?: number | null
    is_tmpfs?: boolean | null
    error?: string | null
  }
  findings?: Finding[]
  operation?: unknown
  stashes?: number
  upstream?: string | null
  error?: string | null
}

export interface Section<T = unknown> {
  state: string
  error: string | null
  report: T | null
}

export interface WorkspaceReport {
  schema?: number
  root?: string
  base?: string
  checked_at?: string
  ok?: boolean
  worktrees?: WorktreeInfo[]
  findings?: Finding[]
}

export interface HostMemory {
  total_mb?: number | null
  available_mb?: number | null
  used_pct?: number | null
  swap_total_mb?: number | null
  swap_free_mb?: number | null
  source?: string | null
}

export interface HostLoad {
  "1m"?: number | null
  "5m"?: number | null
  "15m"?: number | null
}

export interface DiskInfo {
  path?: string
  mount?: string | null
  fstype?: string | null
  total_gb?: number | null
  free_gb?: number | null
  free_pct?: number | null
  inodes_free?: number | null
  is_tmpfs?: boolean | null
  source?: string | null
  error?: string | null
}

export interface ResourcesReport {
  schema?: number
  checked_at?: string
  host?: {
    cpu_count?: number | null
    platform?: string
    pressure?: Record<string, unknown>
    load?: HostLoad
    memory?: HostMemory
  }
  disks?: DiskInfo[]
  policy?: {
    enabled?: boolean
    watchdog?: string
    reserve_memory_pct?: number
    reserve_disk_pct?: number
    reserve_disk_gb?: number
    history?: boolean
    limits?: Record<string, number>
    admission?: Record<string, number>
    worktrees?: Record<string, unknown>
  }
  enforcement?: { available?: boolean; mechanism?: string; cgroups?: boolean; note?: string }
  history?: Record<string, unknown>
  top_groups?: Array<{ command?: string; memory_mb?: number; pgid?: number; process_count?: number }>
}

export interface FilesReport {
  counts?: { records?: number; orphans?: number; new?: number }
  ledger?: { path?: string; present?: boolean }
  new?: { count?: number; session?: string | null; since_hours?: number }
  orphans?: unknown[]
  records?: unknown[]
}

export interface DaemonReport {
  socket?: string
  ping?: Record<string, unknown> | null
  stats?: Record<string, unknown> | null
  stats_error?: string | null
  timeout_s?: number
}

export interface DoctorSnapshot {
  path?: string | null
  state?: string
  [key: string]: unknown
}

export interface DoctorReport {
  snapshot?: DoctorSnapshot
  [key: string]: unknown
}

export interface RuntimeReport {
  live?: boolean
  note?: string
  items?: unknown[]
  services?: unknown[]
  tunnels?: unknown[]
  processes?: unknown[]
  relays?: unknown[]
  [key: string]: unknown
}

export interface ConfigReport {
  path?: string | null
  present?: boolean
  source?: string | null
  adapters?: string[]
  [key: string]: unknown
}

export interface StatusReport {
  schema?: number
  root?: string
  ok?: boolean
  checked_at?: string
  errors?: Array<{ component: string; code: string; message: string }>
  degraded?: Array<{ component: string; code: string; message: string }>
  workspace?: Section<WorkspaceReport>
  resources?: Section<ResourcesReport>
  files?: Section<FilesReport>
  runtime?: Section<RuntimeReport>
  daemon?: Section<DaemonReport>
  doctor?: Section<DoctorReport>
  config?: Section<ConfigReport>
}

export interface WorktreeReport {
  schema?: number
  root?: string
  checked_at?: string
  ok?: boolean
  base?: string
  worktrees?: WorktreeInfo[]
  findings?: Finding[]
}

export interface PolicyReport {
  schema?: number
  ok?: boolean
  kind?: string
  path?: string
  checked_at?: string
  findings?: Finding[]
  [key: string]: unknown
}

export interface ResourcesCheckReport {
  ok?: boolean
  reason?: string | null
  requested?: Record<string, number>
  available?: Record<string, number | null>
  shortfall?: Record<string, number>
  retry_after_s?: number | null
  suggestions?: string[]
  [key: string]: unknown
}

export interface ToolchainDistro {
  available?: boolean
  id?: string | null
  name?: string | null
  pretty_name?: string | null
  version?: string | null
  version_id?: string | null
  id_like?: string[]
  source?: string | null
}

export interface ToolchainPlatform {
  system?: string
  release?: string
  version?: string
  machine?: string
  arch?: string
  family?: string
  distro?: ToolchainDistro
  wsl?: boolean
  container?: boolean
  container_hints?: string[]
}

export interface ToolchainPython {
  executable?: string
  version?: string
  venv?: boolean
  uv?: boolean
}

export interface ToolchainManager {
  executable?: string
  family?: string
  kind?: string
  path?: string
}

export interface ToolchainSudo {
  path?: string | null
  available?: boolean
  noninteractive?: boolean
  probed?: boolean
  note?: string
}

export interface ToolchainApproval {
  allow_install?: boolean
  allow_network?: boolean
  allow_sudo?: boolean
  sources?: Record<string, string>
  environment_overrides?: string[]
}

export interface ToolchainConfigIssue {
  key?: string
  source?: string
  message?: string
}

export interface ToolchainConfig {
  section?: string
  paths?: { legacy?: string | null; repo?: string | null }
  sources?: Record<string, string>
  errors?: ToolchainConfigIssue[]
  unknown?: ToolchainConfigIssue[]
  values?: Record<string, unknown>
}

export interface ToolchainEnvironment {
  schema?: string
  schema_version?: number
  registry_version?: string
  root?: string
  platform?: ToolchainPlatform
  python?: ToolchainPython
  managers?: Record<string, ToolchainManager>
  sudo?: ToolchainSudo
  approval?: ToolchainApproval
  config?: ToolchainConfig
  probe?: { versions?: boolean; sudo?: boolean; sources?: Record<string, string> }
  probe_versions?: boolean
  probe_sudo?: boolean
  preferred?: Record<string, string[]>
}

export interface ToolchainTool {
  id: string
  title?: string
  kind?: string
  format?: string
  path?: string | null
  module?: string | null
  probe?: string | null
  version?: string | null
  provides?: string[]
  writes?: boolean
  distribution?: string | null
  extra?: string | null
  install_kind?: string
  remote?: boolean
  credentialed?: boolean
  probeable?: boolean
  requires?: string[]
  missing_requirements?: string[]
  usable?: boolean
  requires_network?: boolean
  native_for?: string[]
  preferred_by?: string[]
  workflows?: string[]
  recommended?: boolean
  preferred?: boolean
  note?: string
}

export interface ToolchainInstall {
  tool?: string
  manager?: string
  executable?: string
  path?: string | null
  package?: string
  command?: string
  available?: boolean
  requires_sudo?: boolean
  requires_network?: boolean
  blocked_by?: string[]
  blocked_reasons?: string[]
  would_run?: boolean
  recommended?: boolean
  extra?: string | null
  install_kind?: string
  notes?: string
}

export interface ToolchainCapabilityReport {
  id: string
  title?: string
  description?: string
  group?: string | null
  ready?: boolean
  installable?: boolean
  blocked?: boolean
  preferred?: string[]
  recommended_tool?: string | null
  recommended_path?: string | null
  recommended_install?: ToolchainInstall | null
  available?: ToolchainTool[]
  missing?: ToolchainTool[]
  remote?: ToolchainTool[]
  unprobed?: ToolchainTool[]
  install?: ToolchainInstall[]
}

export interface ToolchainRequest {
  capabilities?: string[]
  groups?: string[]
  explicit?: string[]
  inferred?: string[]
  paths?: string[]
  unmatched_paths?: Array<{ path?: string; suffix?: string }>
}

export interface ToolchainSummary {
  capabilities?: number
  ready?: number
  missing?: number
  blocked?: number
  installable?: number
  available_tools?: number
  usable_tools?: number
  missing_tools?: number
  remote_providers?: number
  unprobed_providers?: number
}

export interface ToolchainPlan {
  schema?: string
  schema_version?: number
  registry_version?: string
  root?: string
  request?: ToolchainRequest
  summary?: ToolchainSummary
  capabilities?: ToolchainCapabilityReport[]
  environment?: ToolchainEnvironment
  not_assessed?: string[]
}

export interface ToolchainRegistryCapability {
  id: string
  title?: string
  description?: string
  extensions?: string[]
  tools?: string[]
  note?: string
  group?: string | null
}

export interface ToolchainRegistryGroup {
  id: string
  title?: string
  description?: string
  capabilities?: string[]
  subtypes?: string[]
}

export interface ToolchainRegistryTool extends ToolchainTool {
  capabilities?: string[]
  probes?: string[]
  version_args?: string[]
  installs?: Array<{
    manager?: string
    package?: string
    command?: string
    requires_sudo?: boolean
    requires_network?: boolean
    notes?: string
    extra?: string | null
    kind?: string
  }>
}

export interface ToolchainRegistry {
  schema?: string
  schema_version?: number
  registry_version?: string
  capabilities?: Record<string, ToolchainRegistryCapability>
  groups?: Record<string, ToolchainRegistryGroup>
  tools?: Record<string, ToolchainRegistryTool>
  managers?: Record<string, ToolchainManager & { id?: string; executables?: string[]; command?: string; requires_sudo?: boolean; description?: string }>
}

export interface TestSelection {
  file: string
  node_ids?: string[]
  sources?: string[]
}

export interface OmittedTest {
  file: string
  reason: string
}

export interface TestPlan {
  schema?: string
  mode?: string
  requested_mode?: string
  mode_reason?: string | null
  changed_files?: string[]
  changed_kinds?: Record<string, string>
  selected_files?: string[]
  selected_tests?: TestSelection[]
  counts?: {
    changed_files?: number
    selected_files?: number
    selected_node_ids?: number
    omitted_files?: number
    max_tests?: number | null
    cap_exceeded?: boolean
  }
  omitted?: OmittedTest[]
  omitted_truncated?: boolean
  fallback?: string
  fallback_used?: boolean
  fallback_reason?: string | null
  refused?: boolean
  graph?: { path?: string | null; freshness?: string; usable?: boolean; reason?: string | null }
  test_results?: { available?: boolean; failing?: string[]; flaky?: string[] }
  policy?: Record<string, unknown>
  max_tests?: number | null
  timeout_s?: number | null
  executed?: boolean
  truth_assessed?: boolean
  claim?: string
  not_assessed?: string[]
  warnings?: string[]
}

export interface ResearchColumn {
  key: string
  label: string
}

export interface ResearchNodeRef {
  id: string
  type: string
  label: string
}

export interface ResearchView {
  schema?: number
  schema_name?: string
  view?: string
  title?: string
  investigation_id?: string | null
  columns?: ResearchColumn[]
  nodes?: ResearchNodeRef[]
  rows?: Array<Record<string, unknown>>
  summary?: Record<string, unknown>
  provenance?: {
    derived?: boolean
    generator?: string
    view_schema?: number
    graph_schema?: number | string | null
    investigation?: string | null
    source?: string | null
    root?: string | null
    record_counts?: Record<string, Record<string, number>>
    truth_assessed?: boolean
    [key: string]: unknown
  }
  omitted?: {
    rows?: number
    reason?: string
    limit?: number
    graph?: { nodes?: number; edges?: number; reason?: string; [key: string]: unknown } | null
    [key: string]: unknown
  }
  not_assessed?: string[]
  truth_assessed?: boolean
  note?: string
  graph?: unknown
  [key: string]: unknown
}

export interface SettingSchemaEntry {
  key: string
  group?: string
  type?: string
  default?: unknown
  scope?: string
  description?: string
  validation?: string
  restart_required?: boolean
  section?: string
  name?: string
  aliases?: string[]
  env?: string | null
}

export interface SettingValueEntry {
  key: string
  value?: unknown
  source?: string
  type?: string
  scope?: string
  default?: unknown
  description?: string
  validation?: string
  restart_required?: boolean
  section?: string
}

export interface SettingsSchemaReport {
  schema?: number
  settings?: SettingSchemaEntry[]
}

export interface SettingsValuesReport {
  schema?: number
  root?: string
  locations?: Record<string, unknown>
  settings?: Record<string, SettingValueEntry>
}

export interface TaskWorktreeSpec {
  expected_branch?: string | null
  base?: string
  require_clean?: boolean
  require_up_to_date?: boolean
}

export interface TaskBudget {
  tokens?: number
  model_calls?: number
  cost_usd?: number
  wall_s?: number
}

export interface TaskResources {
  memory_mb?: number
  disk_mb?: number
  pids?: number
  wall_s?: number
}

export interface TaskGates {
  before?: string[]
  after?: string[]
}

export interface TaskApproval {
  mutate?: string
  network?: string
}

export interface TaskStepBudget {
  tokens?: number
  model_calls?: number
  cost_usd?: number
  wall_s?: number
}

export interface TaskStep {
  id: string
  tool: string
  goal: string
  paths?: string[]
  write_paths?: string[]
  budget?: TaskStepBudget
  args?: Record<string, unknown>
}

export interface TaskDefaults {
  worktree?: TaskWorktreeSpec
  resources?: TaskResources
  budget?: TaskBudget
  gates?: TaskGates
  approval?: TaskApproval
  output?: string
  tools?: string[]
  write_scope?: string[]
  requires_graph?: boolean
  steps?: number
}

export interface TaskEntry {
  id: string
  domain: string
  version: number
  title: string
  description: string
  source: string
  path: string
  digest?: string
  overrides?: boolean
  builtin_version?: number | null
  warnings?: string[]
  defaults?: TaskDefaults
}

export interface TaskSpec {
  id: string
  version: number
  title: string
  objective: string
  inputs: string[]
  tools: string[]
  write_scope: string[]
  steps: TaskStep[]
  worktree?: TaskWorktreeSpec
  resources?: TaskResources
  budget?: TaskBudget
  gates?: TaskGates
  stop?: string[]
  output?: string
  output_schema?: unknown
  approval?: TaskApproval
  requires_graph?: boolean
  test_policy?: Record<string, unknown> | null
}

export interface TaskDetail {
  entry: TaskEntry
  spec: TaskSpec
}

export interface TaskListReport {
  schema?: number
  domain?: string | null
  count?: number
  tasks?: TaskEntry[]
}

export interface TaskDomainSummary {
  id: string
  count: number
  task_ids?: string[]
}

export interface TaskCounts {
  builtin?: number
  repo?: number
  total?: number
}

export interface TaskCatalogReport {
  schema?: number
  root?: string | null
  builtin_dir?: string | null
  counts?: TaskCounts
  domains?: TaskDomainSummary[]
  tasks?: TaskEntry[]
}

export interface TaskIssue {
  code: string
  severity: string
  message: string
  task_id?: string | null
  path?: string | null
}

export interface TaskValidateReport extends TaskCatalogReport {
  ok?: boolean
  errors?: string[]
  warnings?: string[]
  issues?: TaskIssue[]
}

export interface ActionPreviewInput {
  action: string
  name?: string
  live?: boolean
  network?: boolean
  maxBytes?: number
  scope?: "project" | "user"
  wait?: number
  idleTimeout?: number
}

/** `actionApply` repeats every preview argument and adds the token. */
export interface ActionApplyInput extends ActionPreviewInput {
  confirm: string
}

export interface TaskRunInput {
  id: string
  inputs?: Record<string, string>
}

/** `toolchainEnv` only controls the bounded sudo usability probe. */
export interface ToolchainEnvInput {
  probeSudo?: boolean
}

/** `toolchainPlan` names capabilities/paths and controls the two probe types. */
export interface ToolchainPlanInput {
  capabilities?: string | string[]
  paths?: string | string[]
  probeVersions?: boolean
  probeSudo?: boolean
}

export interface TaskRunStep {
  id?: string
  tool?: string
  status?: string
  summary?: string
  error?: string | null
  evidence_count?: number
  duration_s?: number
  [key: string]: unknown
}

/** The `tasks_cli run --executor local` report, narrowed for the run result. */
export interface TaskRunReport {
  schema?: number | string
  mode?: string
  executed?: boolean
  ok?: boolean
  run_id?: string
  root?: string
  spec?: {
    id?: string
    version?: number
    title?: string
    [key: string]: unknown
  }
  inputs?: {
    names?: string[]
    digests?: Record<string, string>
    digest?: string
  }
  status?: string
  stop?: string | null
  stop_detail?: string
  preflight?: Record<string, unknown>
  test_plan?: unknown
  gates?: {
    before?: unknown[]
    after?: unknown[]
    after_skipped?: string | null
  }
  steps?: TaskRunStep[]
  artifacts?: unknown[]
  evidence_count?: number
  budget?: Record<string, unknown>
  output?: {
    name?: string
    present?: boolean
    digest?: string | null
    value?: unknown
    [key: string]: unknown
  }
  what_was_not_assessed?: string[]
  warnings?: string[]
  run_dir?: string
  [key: string]: unknown
}

export interface ActionSummary {
  name: string
  description?: string
  mutating?: boolean
  destructive?: boolean
  live_gated?: boolean
}

export interface ActionPreview {
  schema?: number
  action?: string
  mode?: string
  state?: string
  ok?: boolean
  operation?: string
  manager_call?: string | null
  argv?: string[]
  expected_effect?: string
  preconditions?: string[]
  risks?: string[]
  current?: unknown
  current_state?: string
  applicable?: boolean
  confirmation?: string | null
  result?: unknown
  error?: string | null
  live?: boolean
  gated?: boolean
  notes?: string[]
  audit?: string | null
  audit_error?: string | null
}
