/**
 * The panel shell every card renders inside.
 *
 * It exists so the three required render states -- loading, error, empty --
 * are decided in ONE place. Left to individual panels, the empty case is the
 * one that gets forgotten, and a bare blank box reads as "everything is fine,
 * there is simply nothing here" when the truth is usually the opposite.
 *
 * It also owns staleness. When the envelope says `stale: true` the body is
 * dimmed and stamped "as of 12:03:41 (stale)". Rendering an old number as
 * though it were current is the worst thing a control-plane dashboard can do,
 * so it is deliberately not left to each panel to remember.
 */

import type { ReactNode } from 'react'

import { ApiError } from '../api/client'
import { clockTime } from '../lib/format'
import type { Section } from '../api/types'

interface PanelProps {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
  /** The overview section backing this panel, when it has one. */
  section?: Section<unknown> | null
  isLoading: boolean
  error?: unknown
  isEmpty?: boolean
  emptyText?: string
  children: ReactNode
}

export function Panel({
  title,
  subtitle,
  actions,
  section,
  isLoading,
  error,
  isEmpty = false,
  emptyText = 'nothing to show',
  children,
}: PanelProps) {
  const stale = section?.stale === true
  const unavailable = section?.status === 'unavailable'

  return (
    <section className={`panel${stale ? ' panel--stale' : ''}`} aria-busy={isLoading}>
      <header className="panel__head">
        <h2 className="panel__title">{title}</h2>
        <div className="panel__meta">
          {stale && (
            <span className="badge badge--stale" title="the dependency did not answer this poll">
              as of {clockTime(section?.observed_at)} (stale)
            </span>
          )}
          {subtitle}
          {actions}
        </div>
      </header>
      <div className="panel__body">
        {renderBody({ isLoading, error, unavailable, section, isEmpty, emptyText, children })}
      </div>
    </section>
  )
}

function renderBody({
  isLoading,
  error,
  unavailable,
  section,
  isEmpty,
  emptyText,
  children,
}: {
  isLoading: boolean
  error: unknown
  unavailable: boolean
  section?: Section<unknown> | null
  isEmpty: boolean
  emptyText: string
  children: ReactNode
}): ReactNode {
  // Order matters. Loading is only shown when there is genuinely nothing yet;
  // once data has arrived, a refetch keeps the previous render rather than
  // flashing the whole page back to skeletons every five seconds.
  if (isLoading && !section && !error) return <PanelLoading />
  if (error) return <PanelError error={error} />
  if (unavailable) return <PanelUnavailable section={section} />
  if (isEmpty) return <PanelEmpty text={emptyText} />
  return children
}

export function PanelLoading() {
  return (
    <div className="state state--loading" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>loading…</span>
    </div>
  )
}

export function PanelError({ error }: { error: unknown }) {
  const code = error instanceof ApiError ? error.code : 'UNEXPECTED_ERROR'
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="state state--error" role="alert">
      <strong className="state__code">{code}</strong>
      <span>{message}</span>
    </div>
  )
}

/**
 * Distinct from PanelError on purpose: the API answered perfectly well, it
 * just told us a dependency is down. Showing that as a client-side failure
 * would point the operator at the wrong system.
 */
export function PanelUnavailable({ section }: { section?: Section<unknown> | null }) {
  const reason = section?.degraded_reason
  return (
    <div className="state state--unavailable" role="status">
      <strong className="state__code">{reason?.code ?? 'UNAVAILABLE'}</strong>
      <span>{reason?.message ?? 'the dependency behind this panel is unreachable'}</span>
    </div>
  )
}

export function PanelEmpty({ text }: { text: string }) {
  return (
    <div className="state state--empty" role="status">
      <span>{text}</span>
    </div>
  )
}

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill--${statusTone(status)}`}>{status}</span>
}

/** Maps every vocabulary the API uses onto four visual tones. */
export function statusTone(status: string | null | undefined): string {
  switch ((status ?? '').toLowerCase()) {
    case 'healthy':
    case 'running':
    case 'ok':
    case 'loaded':
      return 'good'
    case 'degraded':
    case 'stale':
    case 'restarting':
    case 'paused':
    case 'warning':
      return 'warn'
    case 'unavailable':
    case 'exited':
    case 'missing':
    case 'dead':
    case 'error':
      return 'bad'
    default:
      // `unknown` is genuinely different from bad: it means we could not tell.
      return 'muted'
  }
}
