/**
 * Curated metric tiles.
 *
 * A metric the API returns with `available: false` is rendered greyed with its
 * reason, never hidden. That is the whole point of the API returning it: metric
 * names drift between Milvus versions, and a dashboard that silently drops what
 * it cannot find goes blank after an upgrade with nobody noticing. A greyed
 * tile is a question someone will eventually ask.
 */

import { formatMetric, formatNumber } from '../lib/format'
import { Panel } from './Panel'
import type { Section, MetricsLive, MetricRow } from '../api/types'

interface Props {
  section: Section<MetricsLive> | undefined
  isLoading: boolean
  error: unknown
}

export function MetricsPanel({ section, isLoading, error }: Props) {
  const live = section?.data
  const metrics = live?.metrics ?? []

  return (
    <Panel
      title="Metrics"
      section={section}
      isLoading={isLoading}
      error={error}
      isEmpty={metrics.length === 0}
      emptyText="no metrics scraped — is Milvus exposing :9091/metrics?"
      subtitle={
        live ? (
          <span className="hint">
            {live.available_count}/{live.allowlisted_count} available
            <span className="hint--dim"> · {live.families_scraped} families scraped</span>
          </span>
        ) : undefined
      }
    >
      <div className="tiles">
        {metrics.map((metric) => (
          <Tile key={metric.name} metric={metric} />
        ))}
      </div>
    </Panel>
  )
}

function Tile({ metric }: { metric: MetricRow }) {
  if (!metric.available) {
    return (
      <div className="tile tile--absent" title={metric.unavailable_reason ?? ''}>
        <div className="tile__label">{metric.label}</div>
        <div className="tile__value">—</div>
        <div className="tile__note">not exposed by this version</div>
      </div>
    )
  }

  const p50 = metric.quantiles?.p50
  const p99 = metric.quantiles?.p99

  return (
    <div className="tile" title={metric.description}>
      <div className="tile__label">{metric.label}</div>
      <div className="tile__value">{formatMetric(metric.value, metric.unit)}</div>
      <div className="tile__note">
        {p99 !== undefined && p99 !== null ? (
          <>
            p50 {formatNumber(p50 ?? null)} · p99 {formatNumber(p99)}
          </>
        ) : (
          <>
            {metric.unit}
            {metric.series_count > 1 && ` · ${metric.aggregation} of ${metric.series_count}`}
          </>
        )}
      </div>
    </div>
  )
}
