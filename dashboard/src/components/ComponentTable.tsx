/**
 * Container / pod state.
 *
 * A component the API reports as `missing` gets a row like any other. The API
 * goes to some trouble to reconcile vanished containers into the list rather
 * than omitting them, and dropping them here would throw that away -- an
 * absent row reads as "fine", a red `missing` row reads as "look at me".
 */

import { uptime } from '../lib/format'
import { Panel, StatusPill, statusTone } from './Panel'
import type { Section, ComponentsLive } from '../api/types'

interface Props {
  section: Section<ComponentsLive> | undefined
  isLoading: boolean
  error: unknown
  now: number
}

export function ComponentTable({ section, isLoading, error, now }: Props) {
  const live = section?.data
  const components = live?.components ?? []

  return (
    <Panel
      title="Components"
      section={section}
      isLoading={isLoading}
      error={error}
      isEmpty={components.length === 0}
      emptyText="no labelled containers found — is the stack up? (make up)"
      subtitle={
        live ? (
          <span className="hint">
            {live.running}/{live.total} running
            {live.missing > 0 && <span className="hint--bad"> · {live.missing} missing</span>}
          </span>
        ) : undefined
      }
    >
      <div className="tablewrap">
        <table className="table">
          <thead>
            <tr>
              <th>component</th>
              <th>state</th>
              <th>health</th>
              <th className="num">restarts</th>
              <th>uptime</th>
              <th>image</th>
            </tr>
          </thead>
          <tbody>
            {components.map((component) => (
              <tr
                key={component.component_name}
                className={`row row--${statusTone(component.state)}`}
              >
                <td className="mono">{component.component_name}</td>
                <td>
                  <StatusPill status={component.state} />
                  {component.exit_code !== null && (
                    <span className="hint hint--bad"> exit {component.exit_code}</span>
                  )}
                </td>
                <td>{component.health ?? <span className="hint">no healthcheck</span>}</td>
                <td className="num">{component.restart_count}</td>
                <td>{component.state === 'running' ? uptime(component.started_at, now) : '—'}</td>
                <td className="mono truncate" title={component.image ?? ''}>
                  {component.image ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}
