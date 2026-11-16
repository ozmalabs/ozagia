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
