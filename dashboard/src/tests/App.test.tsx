/**
 * The acceptance criteria, asserted rather than eyeballed.
 *
 * The rules that matter: every panel has a visible loading / error / empty
 * state, stale data is visually distinct, and no failure produces a white
 * screen.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../App'
import { CLUSTER, healthyOverview, outageOverview, unavailable } from './fixtures'
import type { Overview } from '../api/types'

const PANELS = ['Cluster metadata', 'Components', 'Collections', 'Metrics', 'Logs', 'Events']

/** Queries retry once, so a failure takes ~1s longer than the default wait. */
const RETRY_TIMEOUT_MS = 5000

function json(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

interface RouteOptions {
  overview?: Overview
  clusters?: unknown
  clustersStatus?: number
  overviewFails?: boolean
  logs?: unknown
}

function mockFetch(options: RouteOptions = {}) {
  return vi.fn(async (url: string) => {
    if (url.includes('/clusters?')) {
      if (options.clustersStatus && options.clustersStatus >= 400) {
        return json(
          { error: { code: 'POSTGRES_UNAVAILABLE', message: 'database unreachable', detail: {} } },
          options.clustersStatus,
        )
      }
      return json(options.clusters ?? { items: [CLUSTER], total: 1, limit: 50, offset: 0 })
    }
    if (url.includes('/overview')) {
      if (options.overviewFails) throw new TypeError('network down')
      return json(options.overview ?? healthyOverview())
    }
    if (url.includes('/logs')) {
      return json(
        options.logs ?? {
          cluster: CLUSTER,
          live: {
            component: 'milvus-standalone',
            lines: [
              { timestamp: '2026-08-07T12:00:00Z', stream: 'stdout', message: 'proxy started' },
              { timestamp: '2026-08-07T12:00:01Z', stream: 'stderr', message: '[ERROR] boom' },
            ],
            count: 2,
            truncated: false,
          },
          live_status: 'ok',
          observed_at: '2026-08-07T12:00:00Z',
          stale: false,
          degraded_reason: null,
        },
      )
    }
    throw new Error(`unexpected fetch: ${url}`)
  })
}

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

afterEach(() => {
  // Explicit, because Testing Library only registers its automatic cleanup
  // when vitest runs with `globals: true`. Without this the previous test's
  // DOM stays mounted and every query finds two of everything.
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('healthy stack', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch())
  })

  it('populates all six panels', async () => {
    renderApp()
    for (const title of PANELS) {
      expect(await screen.findByText(title)).toBeTruthy()
    }
  })

  it('shows the cluster identity and a green status pill', async () => {
    renderApp()
    // Scoped to the heading: the cluster name legitimately appears twice, in
    // the header and again as a row in the metadata card.
    expect(await screen.findByRole('heading', { name: 'local-standalone' })).toBeTruthy()
    const pill = document.querySelector('.statuspill')
    expect(pill?.className).toContain('statuspill--good')
    expect(pill?.textContent).toContain('healthy')
  })

  it('renders real values, not placeholders', async () => {
    renderApp()
    // components, collections, metrics
    expect(await screen.findByText('milvus-standalone')).toBeTruthy()
    expect(await screen.findByText('demo_docs')).toBeTruthy()
    expect(await screen.findByText('5,000')).toBeTruthy()
    expect(await screen.findByText('Milvus nodes')).toBeTruthy()
  })

  it('shows no banner when everything is ok', async () => {
    renderApp()
    await screen.findByText('demo_docs')
    expect(document.querySelector('.banner')).toBeNull()
  })

  it('greys an unavailable metric instead of hiding it', async () => {
    renderApp()
    // The absent metric must still be on the page, with its reason.
    expect(await screen.findByText('Loaded entities')).toBeTruthy()
    expect(screen.getByText('not exposed by this version')).toBeTruthy()
    const absent = document.querySelector('.tile--absent')
    expect(absent).not.toBeNull()
  })

  it('renders the log tail with severity tinting', async () => {
    renderApp()
    expect(await screen.findByText('proxy started')).toBeTruthy()
    expect(document.querySelector('.logline--error')).not.toBeNull()
  })

  it('makes exactly one overview request per render, not one per panel', async () => {
    const fetchMock = mockFetch()
    vi.stubGlobal('fetch', fetchMock)
    renderApp()
    await screen.findByText('demo_docs')
    const overviewCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/overview'),
    )
    expect(overviewCalls.length).toBe(1)
  })
})

describe('milvus outage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch({ overview: outageOverview() }))
  })

  it('turns the status pill red and shows a banner naming the error code', async () => {
    renderApp()
    await waitFor(() => {
      const pill = document.querySelector('.statuspill')
      expect(pill?.className).toContain('statuspill--bad')
    })
    const banner = document.querySelector('.banner')
    expect(banner).not.toBeNull()
    expect(banner?.textContent).toContain('MILVUS_UNREACHABLE')
  })

  it('shows the stopped container with its exit code', async () => {
    renderApp()
    expect(await screen.findByText('exited')).toBeTruthy()
    expect(screen.getByText(/exit 137/)).toBeTruthy()
  })

  it('dims stale panels and stamps them with the original observation time', async () => {
    renderApp()
    await screen.findByText('exited')

    const stalePanels = document.querySelectorAll('.panel--stale')
    expect(stalePanels.length).toBeGreaterThanOrEqual(2)

    const labels = screen.getAllByText(/\(stale\)/)
    expect(labels.length).toBeGreaterThanOrEqual(2)
    // The timestamp shown is when the data was true, not now.
    expect(labels[0]?.textContent).toMatch(/as of \d{2}:\d{2}:\d{2} \(stale\)/)
  })

  it('keeps the metadata panel live while live panels go stale', async () => {
    renderApp()
    await screen.findByText('exited')

    const panels = Array.from(document.querySelectorAll('.panel'))
    const metadata = panels.find((p) => p.textContent?.includes('Cluster metadata'))
    expect(metadata).toBeDefined()
    // Reads from PostgreSQL only, so a Milvus outage must not dim it. This is
    // the degradation contract made visible: stored data stays live while the
    // live panels go stale.
    expect(metadata?.className).not.toContain('panel--stale')
    // Scoped: deployment_type also appears in the header facts line.
    expect(within(metadata as HTMLElement).getByText('docker_standalone')).toBeTruthy()
  })
})

describe('degraded dependencies', () => {
  it('renders an unavailable panel with its code rather than a blank box', async () => {
    const overview = healthyOverview()
    overview.components = unavailable('DOCKER_UNAVAILABLE')
    overview.degraded = true
    vi.stubGlobal('fetch', mockFetch({ overview }))

    renderApp()
    expect(await screen.findAllByText('DOCKER_UNAVAILABLE')).toBeTruthy()
    const panels = Array.from(document.querySelectorAll('.panel'))
    const components = panels.find((p) => p.textContent?.includes('Components'))
    expect(within(components as HTMLElement).getByText(/cannot reach the Docker socket/)).toBeTruthy()
  })

  it('shows the documented empty state when there are no collections', async () => {
    const overview = healthyOverview()
    overview.collections.data = { collections: [], count: 0, snapshot_only: 0 }
    vi.stubGlobal('fetch', mockFetch({ overview }))

    renderApp()
    expect(await screen.findByText('no collections — run make demo')).toBeTruthy()
  })

  it('shows an empty state for events rather than an empty box', async () => {
    const overview = healthyOverview()
    overview.events.data = []
    vi.stubGlobal('fetch', mockFetch({ overview }))

    renderApp()
    expect(await screen.findByText(/no events yet/)).toBeTruthy()
  })
})

describe('failure never yields a white screen', () => {
  it('renders a banner when the API itself is unreachable', async () => {
    vi.stubGlobal('fetch', mockFetch({ overviewFails: true }))
    renderApp()

    // Generous timeout on purpose: the queries retry once before giving up, so
    // the failure surfaces about a second later than the default 1000ms allows.
    // The user sees "loading…" until then, which is the honest thing to show.
    await waitFor(
      () => {
        const banner = document.querySelector('.banner')
        expect(banner).not.toBeNull()
        expect(banner?.textContent).toContain('API_UNREACHABLE')
      },
      { timeout: RETRY_TIMEOUT_MS },
    )
    // The page still has structure, not a blank body.
    expect(document.querySelectorAll('.panel').length).toBeGreaterThan(0)
  })

  it('explains itself when no cluster is registered', async () => {
    vi.stubGlobal('fetch', mockFetch({ clusters: { items: [], total: 0, limit: 50, offset: 0 } }))
    renderApp()
    expect(await screen.findByText(/no cluster registered/)).toBeTruthy()
  })

  it('surfaces a 503 from the clusters endpoint', async () => {
    vi.stubGlobal('fetch', mockFetch({ clustersStatus: 503 }))
    renderApp()
    expect(
      await screen.findByText('POSTGRES_UNAVAILABLE', {}, { timeout: RETRY_TIMEOUT_MS }),
    ).toBeTruthy()
  })
})
