/**
 * Single page, six panels, two polling queries.
 *
 * The cluster id comes from a one-shot `/clusters` fetch rather than being
 * baked in at build time, so the same image works against any registered
 * cluster. `?cluster=<uuid>` overrides it.
 */

import { CollectionsTable } from './components/CollectionsTable'
import { ComponentTable } from './components/ComponentTable'
import { EventsStrip } from './components/EventsStrip'
import { Header } from './components/Header'
import { LogViewer } from './components/LogViewer'
import { MetadataCard } from './components/MetadataCard'
import { MetricsPanel } from './components/MetricsPanel'
import { PanelError, PanelLoading } from './components/Panel'
import { POLL_INTERVAL_MS, useClusters, useOverview } from './hooks/queries'
import { useNow } from './lib/format'

function clusterFromUrl(): string | undefined {
  return new URLSearchParams(window.location.search).get('cluster') ?? undefined
}

export default function App() {
  const now = useNow(1000)
  const clusters = useClusters()
  const clusterId = clusterFromUrl() ?? clusters.data?.items[0]?.id
  const overview = useOverview(clusterId)

  const data = overview.data
  // A poll that fails while previous data is on screen must not blank the
  // page: the panels keep rendering the last good values and the banner says
  // the API is unreachable.
  const isLoading = overview.isLoading || (clusters.isLoading && !clusterId)
  const error = overview.error ?? clusters.error

  if (clusters.isLoading && !clusters.data) {
    return (
      <main className="app">
        <div className="panel">
          <PanelLoading />
        </div>
      </main>
    )
  }

  if (!clusterId && !clusters.isLoading) {
    return (
      <main className="app">
        <div className="panel">
          {clusters.error ? (
            <PanelError error={clusters.error} />
          ) : (
            <div className="state state--empty">
              <span>
                no cluster registered. Start the API so it bootstraps one from .env, or POST
                /api/v1/clusters.
              </span>
            </div>
          )}
        </div>
      </main>
    )
  }

  return (
    <main className="app">
      <Header overview={data} error={error} isFetching={overview.isFetching} now={now} />

      <div className="grid">
        <div className="grid__col grid__col--left">
          <MetadataCard
            cluster={data?.cluster}
            isLoading={isLoading}
            error={error}
            now={now}
          />
          <EventsStrip section={data?.events} isLoading={isLoading} error={error} now={now} />
        </div>

        <div className="grid__col grid__col--right">
          <ComponentTable
            section={data?.components}
            isLoading={isLoading}
            error={error}
            now={now}
          />
          <CollectionsTable section={data?.collections} isLoading={isLoading} error={error} />
          <MetricsPanel section={data?.metrics} isLoading={isLoading} error={error} />
        </div>
      </div>

      <LogViewer clusterId={clusterId} components={data?.components} />

      <footer className="footer">
        polling every {POLL_INTERVAL_MS / 1000}s
        {data && (
          <>
            <span className="sep">·</span>
            <span>fan-out {Math.round(data.duration_ms)}ms of {data.budget_s}s budget</span>
          </>
        )}
        <span className="sep">·</span>
        <a href="/api/v1/../../docs">API docs</a>
      </footer>
    </main>
  )
}
