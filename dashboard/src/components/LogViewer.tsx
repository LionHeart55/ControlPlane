/**
 * Log tail with a component selector and an auto-scroll toggle.
 *
 * This is the one panel with its own query: the overview carries a fixed
 * 50-line tail of one component, but the viewer needs 100 lines of whichever
 * component you pick. That is two polling queries in total, not one per panel.
 *
 * Auto-scroll is off-by-choice-able and says "paused" when it is, because a
 * viewer that yanks you back to the bottom while you are reading a stack trace
 * is worse than no viewer.
 */

import { useEffect, useMemo, useRef, useState } from 'react'

import { useLogs } from '../hooks/queries'
import { clockTime } from '../lib/format'
import { Panel } from './Panel'
import type { Envelope, LogsLive, Section, ComponentsLive } from '../api/types'

const FALLBACK_COMPONENTS = ['milvus-standalone', 'milvus-etcd', 'milvus-minio', 'cp-postgres']

interface Props {
  clusterId: string | undefined
  components: Section<ComponentsLive> | undefined
}

export function LogViewer({ clusterId, components }: Props) {
  const [component, setComponent] = useState('milvus-standalone')
  const [autoScroll, setAutoScroll] = useState(true)
  const bodyRef = useRef<HTMLDivElement | null>(null)

  const query = useLogs(clusterId, component, Boolean(clusterId))
  const envelope = query.data as Envelope<LogsLive> | undefined
  const lines = envelope?.live?.lines ?? []

  // Offer whatever the components panel actually found, so the dropdown can
  // never list a container that does not exist. Falls back to the known set
  // while the first poll is in flight.
  const options = useMemo(() => {
    const observed = components?.data?.components?.map((c) => c.component_name) ?? []
    return observed.length > 0 ? observed : FALLBACK_COMPONENTS
  }, [components])

  useEffect(() => {
    if (!autoScroll) return
    const node = bodyRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [lines, autoScroll])

  // The logs envelope is not an overview Section; adapt it so Panel can apply
  // the same staleness and unavailable handling as everywhere else.
  const section: Section<LogsLive> | undefined = envelope
    ? {
        data: envelope.live,
        status: envelope.live_status,
        observed_at: envelope.observed_at,
        stale: envelope.stale,
        degraded_reason: envelope.degraded_reason,
        duration_ms: null,
      }
    : undefined

  return (
    <Panel
      title="Logs"
      section={section}
      isLoading={query.isLoading}
      error={query.error}
      isEmpty={lines.length === 0}
      emptyText={`no log lines for ${component} in the requested window`}
      actions={
        <div className="logctl">
          <select
            className="select"
            value={component}
            onChange={(event) => setComponent(event.target.value)}
            aria-label="component"
          >
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <label className="toggle">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(event) => setAutoScroll(event.target.checked)}
            />
            <span>auto-scroll</span>
          </label>
          {!autoScroll && <span className="badge badge--paused">paused</span>}
        </div>
      }
    >
      <div className="logs" ref={bodyRef}>
        {lines.map((line, index) => (
          <div
            /* Timestamps repeat and continuation lines have none, so the index
               is part of the key -- without it React reuses rows and the view
               tears while scrolling. */
            key={`${line.timestamp ?? 'cont'}-${index}`}
            className={`logline logline--${severity(line.message, line.stream)}`}
          >
            <span className="logline__time">
              {line.timestamp ? clockTime(line.timestamp) : '        '}
            </span>
            <span className="logline__stream">{line.stream === 'stderr' ? 'E' : 'O'}</span>
            <span className="logline__msg">{line.message}</span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

/** Tint by content first, stream second: Milvus writes INFO to stderr. */
function severity(message: string, stream: string): string {
  const text = message.toUpperCase()
  if (text.includes('[ERROR]') || text.includes('ERROR') || text.includes('FATAL')) return 'error'
  if (text.includes('[WARN]') || text.includes('WARNING')) return 'warn'
  if (text.includes('[DEBUG]')) return 'debug'
  if (stream === 'stderr') return 'stderr'
  return 'info'
}
