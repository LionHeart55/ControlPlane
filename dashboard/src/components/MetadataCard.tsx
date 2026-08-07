/**
 * Every column of the `clusters` row.
 *
 * Reads from PostgreSQL only, so it stays live through a Milvus outage -- which
 * is the point during a drill: the metadata panel keeps working while the live
 * panels dim, and that contrast is the degradation contract made visible.
 */

import { relativeTime } from '../lib/format'
import { Panel, StatusPill } from './Panel'
import type { Cluster } from '../api/types'

interface Props {
  cluster: Cluster | null | undefined
  isLoading: boolean
  error: unknown
  now: number
}

export function MetadataCard({ cluster, isLoading, error, now }: Props) {
  const rows: [string, React.ReactNode][] = cluster
    ? [
        ['id', <code key="id">{cluster.id}</code>],
        ['name', cluster.name],
        ['deployment type', cluster.deployment_type],
        ['deployment status', <StatusPill key="ds" status={cluster.deployment_status} />],
        ['milvus version', cluster.milvus_version ?? '—'],
        ['endpoint', <code key="ep">{cluster.endpoint_uri}</code>],
        ['metrics uri', <code key="mu">{cluster.metrics_uri ?? '—'}</code>],
        ['object store', <code key="os">{cluster.object_store_endpoint ?? '—'}</code>],
        ['compose project', cluster.compose_project ?? '—'],
        ['namespace', cluster.namespace ?? '—'],
        ['last health status', <StatusPill key="lhs" status={cluster.last_health_status} />],
        [
          'last checked',
          cluster.last_health_check_at
            ? relativeTime(cluster.last_health_check_at, now)
            : 'never',
        ],
        ['labels', <code key="lb">{JSON.stringify(cluster.labels ?? {})}</code>],
        ['created', relativeTime(cluster.created_at, now)],
        ['updated', relativeTime(cluster.updated_at, now)],
      ]
    : []

  return (
    <Panel
      title="Cluster metadata"
      subtitle={<span className="hint">from PostgreSQL</span>}
      isLoading={isLoading}
      error={error}
      isEmpty={!cluster}
      emptyText="no cluster registered — POST /api/v1/clusters or restart the API to bootstrap one"
    >
      <dl className="kv">
        {rows.map(([key, value]) => (
          <div className="kv__row" key={key}>
            <dt className="kv__key">{key}</dt>
            <dd className="kv__value">{value}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  )
}
