/**
 * The incident trail — the panel you point at during the reliability demo.
 *
 * Rows exist only for transitions, never per poll, so a ten-minute outage
 * produces exactly two entries: one going down, one coming back. That is what
 * makes this readable as a timeline rather than a log.
 */

import { relativeTime } from '../lib/format'
import { Panel } from './Panel'
import type { Section, EventRow } from '../api/types'

interface Props {
  section: Section<EventRow[]> | undefined
  isLoading: boolean
  error: unknown
  now: number
}

export function EventsStrip({ section, isLoading, error, now }: Props) {
  const events = (section?.data ?? []).slice(0, 10)

  return (
    <Panel
      title="Events"
      section={section}
      isLoading={isLoading}
      error={error}
      isEmpty={events.length === 0}
      emptyText="no events yet — nothing has changed state since the control plane started"
      subtitle={<span className="hint">transitions only, newest first</span>}
    >
      <ul className="events">
        {events.map((event) => (
          <li key={event.id} className={`event event--${severityTone(event.severity)}`}>
            <span className="event__dot" aria-hidden="true" />
            <div className="event__body">
              <div className="event__message">{event.message}</div>
              <div className="event__meta">
                <code>{event.event_type}</code>
                <span className="sep">·</span>
                <span title={event.created_at}>{relativeTime(event.created_at, now)}</span>
                {typeof event.payload?.rule === 'number' && (
                  <>
                    <span className="sep">·</span>
                    <span title="which ordered health rule fired">rule {event.payload.rule}</span>
                  </>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function severityTone(severity: string): string {
  switch (severity) {
    case 'error':
      return 'bad'
    case 'warning':
      return 'warn'
    default:
      return 'good'
  }
}
