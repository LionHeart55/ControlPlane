/**
 * Types mirroring the control-plane API contract.
 *
 * The important one is the freshness triple carried by every section:
 *
 *   status = "ok"           data is current
 *   status = "stale"        data is real but old — `observed_at` is when it was
 *                           true, and the UI must dim it and say so
 *   status = "unavailable"  data is null and `degraded_reason` says why
 *
 * A dashboard that renders stale numbers as though they were current is worse
 * than one that renders nothing, so `stale` is never ignored downstream.
 */

export type LiveStatus = 'ok' | 'stale' | 'unavailable'

export interface DegradedReason {
  code: string
  message: string
  since: string | null
}

/** One panel's data plus its own freshness. */
export interface Section<T> {
  data: T | null
  status: LiveStatus
  observed_at: string
  stale: boolean
  degraded_reason: DegradedReason | null
  duration_ms: number | null
}

/** The generic envelope used by the standalone (non-overview) routes. */
export interface Envelope<T> {
  cluster: Cluster | null
  live: T | null
  live_status: LiveStatus
  observed_at: string
  stale: boolean
  degraded_reason: DegradedReason | null
}

export type HealthStatus = 'healthy' | 'degraded' | 'unavailable' | 'unknown'

export interface Cluster {
  id: string
  name: string
  deployment_type: string
  deployment_status: string
  milvus_version: string | null
  endpoint_uri: string
  metrics_uri: string | null
  object_store_endpoint: string | null
  compose_project: string | null
  namespace: string | null
  last_health_status: HealthStatus
  last_health_check_at: string | null
  labels: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface LiveHealth {
  status: HealthStatus
  /** Which of the six ordered aggregation rules decided the status. */
  rule: number
  milvus_reachable: boolean
  latency_ms: number | null
  server_version: string | null
  error_code: string | null
  error_message: string | null
  reasons: string[]
  checks: Record<string, unknown>
}

export interface ComponentRow {
  component_name: string
  kind: string
  runtime_id: string | null
  image: string | null
  /** running | exited | paused | restarting | dead | missing */
  state: string
  health: string | null
  restart_count: number
  started_at: string | null
  exit_code: number | null
}

export interface ComponentsLive {
  components: ComponentRow[]
  total: number
  running: number
  missing: number
}

export interface CollectionRow {
  collection_name: string
  row_count: number | null
  num_partitions: number | null
  dimension: number | null
  index_type: string | null
  metric_type: string | null
  is_loaded: boolean | null
  load_state: string | null
  /** "live" or "snapshot" — a snapshot row may already be gone from Milvus. */
  source: string
  observed_at: string | null
  error_code: string | null
  error_message: string | null
}

export interface CollectionsLive {
  collections: CollectionRow[]
  count: number
  snapshot_only: number
}

export interface MetricRow {
  name: string
  label: string
  unit: string
  aggregation: string
  kind: string
  value: number | null
  available: boolean
  quantiles: Record<string, number | null> | null
  series_count: number
  unavailable_reason: string | null
  description: string
}

export interface MetricsLive {
  metrics: MetricRow[]
  families_scraped: number
  available_count: number
  allowlisted_count: number
}

export interface LogLine {
  timestamp: string | null
  stream: 'stdout' | 'stderr' | string
  message: string
}

export interface LogsLive {
  component: string
  lines: LogLine[]
  count: number
  truncated: boolean
}

export interface EventRow {
  id: number
  cluster_id: string | null
  event_type: string
  severity: 'info' | 'warning' | 'error' | string
  message: string
  payload: Record<string, unknown>
  created_at: string
}

export interface Overview {
  cluster: Cluster | null
  health: Section<LiveHealth>
  collections: Section<CollectionsLive>
  metrics: Section<MetricsLive>
  components: Section<ComponentsLive>
  logs: Section<LogsLive>
  events: Section<EventRow[]>
  generated_at: string
  budget_s: number
  duration_ms: number
  /** True when any section is not `ok`. Saves inspecting each one. */
  degraded: boolean
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface ApiErrorBody {
  error: { code: string; message: string; detail: Record<string, unknown> }
}
