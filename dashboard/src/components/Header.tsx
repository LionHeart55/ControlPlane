/**
 * Header and the connection banner.
 *
 * The banner is the one element that must never be subtle. It stays visible
 * for as long as anything is degraded, names the failed dependency and shows
 * its stable error code -- the code is what you search for, so it is rendered
 * verbatim rather than translated into prose.
 */

import { ApiError } from '../api/client'
import { relativeTime } from '../lib/format'
import { statusTone } from './Panel'
import type { Overview } from '../api/types'

interface HeaderProps {
  overview: Overview | undefined
  error: unknown
  isFetching: boolean
  now: number
}

export function Header({ overview, error, isFetching, now }: HeaderProps) {
  const cluster = overview?.cluster
  const health = overview?.health
  // Prefer the live probe; fall back to the stored status when the probe
  // itself could not run, so the pill is never blank.
  const status = health?.data?.status ?? cluster?.last_health_status ?? 'unknown'

  return (
    <header className="header">
      <div className="header__row">
        <div className="header__identity">
          <h1 className="header__name">{cluster?.name ?? 'Milvus control plane'}</h1>
          <div className="header__facts">
            <span>{cluster?.deployment_type ?? '—'}</span>
            <span className="sep">·</span>
            <span>Milvus {health?.data?.server_version ?? cluster?.milvus_version ?? '—'}</span>
            <span className="sep">·</span>
            <span title={overview?.generated_at ?? ''}>
              last checked {relativeTime(overview?.generated_at, now)}
            </span>
            {isFetching && <span className="header__pulse" title="refreshing" aria-hidden="true" />}
          </div>
        </div>
        <div className={`statuspill statuspill--${statusTone(status)}`}>
          <span className="statuspill__dot" aria-hidden="true" />
          <span className="statuspill__text">{status}</span>
        </div>
      </div>
      <ConnectionBanner overview={overview} error={error} />
    </header>
  )
}

interface BannerLine {
  tone: 'warn' | 'bad'
  label: string
  code: string
  message: string
}

export function ConnectionBanner({
  overview,
  error,
}: {
  overview: Overview | undefined
  error: unknown
}) {
  // The API being unreachable outranks anything it might have told us.
  if (error) {
    const code = error instanceof ApiError ? error.code : 'UNEXPECTED_ERROR'
    const message = error instanceof Error ? error.message : String(error)
    return (
      <Banner
        lines={[{ tone: 'bad', label: 'control-plane API', code, message }]}
        note="showing the last successful poll"
      />
    )
  }

  if (!overview) return null

  const lines: BannerLine[] = []
  const sections: [string, Overview[keyof Overview]][] = [
    ['health', overview.health],
    ['collections', overview.collections],
    ['metrics', overview.metrics],
    ['components', overview.components],
    ['logs', overview.logs],
    ['events', overview.events],
  ]

  for (const [label, raw] of sections) {
    const section = raw as Overview['health']
    if (section?.status === 'ok') continue
    if (!section) continue
    lines.push({
      tone: section.status === 'unavailable' ? 'bad' : 'warn',
      label,
      code: section.degraded_reason?.code ?? section.status.toUpperCase(),
      message: section.degraded_reason?.message ?? 'serving cached data',
    })
  }

  // A cluster that is up but unhealthy is worth a banner even when every
  // section fetched cleanly — the fetch succeeding is not the same as the
  // cluster being well.
  const health = overview.health?.data
  if (health && health.status !== 'healthy' && health.error_code) {
    lines.unshift({
      tone: health.status === 'unavailable' ? 'bad' : 'warn',
      label: `cluster ${health.status}`,
      code: health.error_code,
      message: health.error_message ?? 'see the health panel',
    })
  }

  if (lines.length === 0) return null
  return <Banner lines={lines} />
}

function Banner({ lines, note }: { lines: BannerLine[]; note?: string }) {
  const tone = lines.some((line) => line.tone === 'bad') ? 'bad' : 'warn'
  return (
    <div className={`banner banner--${tone}`} role="alert">
      <div className="banner__lines">
        {lines.map((line) => (
          <div className="banner__line" key={`${line.label}:${line.code}`}>
            <span className="banner__label">{line.label}</span>
            <code className="banner__code">{line.code}</code>
            <span className="banner__message">{line.message}</span>
          </div>
        ))}
      </div>
      {note && <div className="banner__note">{note}</div>}
    </div>
  )
}
