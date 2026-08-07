/** Overview fixtures, shaped exactly like the live API response. */

import type { Cluster, Overview, Section } from '../api/types'

export const CLUSTER: Cluster = {
  id: '202b9ea6-a927-44ec-98d4-46f7ceff4a08',
  name: 'local-standalone',
  deployment_type: 'docker_standalone',
  deployment_status: 'running',
  milvus_version: 'v2.6.20',
  endpoint_uri: 'http://milvus-standalone:19530',
  metrics_uri: 'http://milvus-standalone:9091',
  object_store_endpoint: 'milvus-minio:9000',
  compose_project: 'milvus-cp',
  namespace: null,
  last_health_status: 'healthy',
  last_health_check_at: '2026-08-07T12:00:00Z',
  labels: { source: 'bootstrap' },
  created_at: '2026-08-07T11:00:00Z',
  updated_at: '2026-08-07T12:00:00Z',
}

export function ok<T>(data: T): Section<T> {
  return {
    data,
    status: 'ok',
    observed_at: '2026-08-07T12:00:00Z',
    stale: false,
    degraded_reason: null,
    duration_ms: 12,
  }
}

export function stale<T>(data: T, code = 'MILVUS_UNREACHABLE'): Section<T> {
  return {
    data,
    status: 'stale',
    observed_at: '2026-08-07T11:58:41Z',
    stale: true,
    degraded_reason: { code, message: 'connection refused', since: null },
    duration_ms: 5010,
  }
}

export function unavailable<T>(code = 'DOCKER_UNAVAILABLE'): Section<T> {
  return {
    data: null,
    status: 'unavailable',
    observed_at: '2026-08-07T12:00:00Z',
    stale: false,
    degraded_reason: { code, message: 'cannot reach the Docker socket', since: null },
    duration_ms: 3,
  }
}

export function healthyOverview(): Overview {
  return {
    cluster: CLUSTER,
    health: ok({
      status: 'healthy',
      rule: 5,
      milvus_reachable: true,
      latency_ms: 8,
      server_version: 'v2.6.20',
      error_code: null,
      error_message: null,
      reasons: [],
      checks: {},
    }),
    components: ok({
      components: [
        {
          component_name: 'milvus-standalone',
          kind: 'container',
          runtime_id: 'abc123',
          image: 'milvusdb/milvus:v2.6.20',
          state: 'running',
          health: 'healthy',
          restart_count: 0,
          started_at: '2026-08-07T11:00:00Z',
          exit_code: null,
        },
        {
          component_name: 'cp-postgres',
          kind: 'container',
          runtime_id: 'def456',
          image: 'postgres:16-alpine',
          state: 'running',
          health: 'healthy',
          restart_count: 0,
          started_at: '2026-08-07T11:00:00Z',
          exit_code: null,
        },
      ],
      total: 2,
      running: 2,
      missing: 0,
    }),
    collections: ok({
      collections: [
        {
          collection_name: 'demo_docs',
          row_count: 5000,
          num_partitions: 1,
          dimension: 384,
          index_type: 'HNSW',
          metric_type: 'COSINE',
          is_loaded: true,
          load_state: 'Loaded',
          source: 'live',
          observed_at: null,
          error_code: null,
          error_message: null,
        },
      ],
      count: 1,
      snapshot_only: 0,
    }),
    metrics: ok({
      metrics: [
        {
          name: 'milvus_num_node',
          label: 'Milvus nodes',
          unit: 'nodes',
          aggregation: 'sum',
          kind: 'gauge',
          value: 4,
          available: true,
          quantiles: null,
          series_count: 4,
          unavailable_reason: null,
          description: 'Number of Milvus nodes by role.',
        },
        {
          name: 'milvus_querynode_entity_num',
          label: 'Loaded entities',
          unit: 'entities',
          aggregation: 'sum',
          kind: 'gauge',
          value: null,
          available: false,
          quantiles: null,
          series_count: 0,
          unavailable_reason: 'not exposed by this Milvus version or not yet active',
          description: 'Entities held by query nodes.',
        },
      ],
      families_scraped: 361,
      available_count: 1,
      allowlisted_count: 2,
    }),
    logs: ok({
      component: 'milvus-standalone',
      lines: [{ timestamp: '2026-08-07T12:00:00Z', stream: 'stdout', message: 'ready' }],
      count: 1,
      truncated: false,
    }),
    events: ok([
      {
        id: 3,
        cluster_id: CLUSTER.id,
        event_type: 'health_transition',
        severity: 'info',
        message: "cluster 'local-standalone' health unknown -> healthy",
        payload: { from: 'unknown', to: 'healthy', rule: 5 },
        created_at: '2026-08-07T11:59:00Z',
      },
    ]),
    generated_at: '2026-08-07T12:00:00Z',
    budget_s: 6,
    duration_ms: 214.8,
    degraded: false,
  }
}

/** Milvus stopped: health unavailable, container exited, live panels stale. */
export function outageOverview(): Overview {
  const base = healthyOverview()
  return {
    ...base,
    health: {
      ...base.health,
      data: {
        status: 'unavailable',
        rule: 1,
        milvus_reachable: false,
        latency_ms: 5012,
        server_version: null,
        error_code: 'MILVUS_UNREACHABLE',
        error_message: 'connection refused',
        reasons: ['milvus_unreachable'],
        checks: {},
      },
    },
    components: ok({
      components: [
        {
          component_name: 'milvus-standalone',
          kind: 'container',
          runtime_id: 'abc123',
          image: 'milvusdb/milvus:v2.6.20',
          state: 'exited',
          health: 'unhealthy',
          restart_count: 0,
          started_at: '2026-08-07T11:00:00Z',
          exit_code: 137,
        },
      ],
      total: 1,
      running: 0,
      missing: 0,
    }),
    collections: stale(base.collections.data!),
    metrics: stale(base.metrics.data!),
    degraded: true,
  }
}
