/**
 * Thin fetch wrapper.
 *
 * Paths are relative (`/api/v1/...`) in both dev and production: nginx proxies
 * them in the container, Vite proxies them in dev. No base-URL switch, so there
 * is no environment-specific code path that only breaks after deployment.
 */

import type { ApiErrorBody, Envelope, LogsLive, Overview, Page, Cluster } from './types'

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(message: string, status: number, code: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      signal,
      headers: { Accept: 'application/json' },
    })
  } catch (cause) {
    // The API itself is unreachable — distinct from the API telling us a
    // dependency is down, which arrives as a normal 200 envelope.
    throw new ApiError(
      cause instanceof Error && cause.name === 'AbortError'
        ? 'request cancelled'
        : 'cannot reach the control-plane API',
      0,
      'API_UNREACHABLE',
    )
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`
    let message = `request failed with ${response.status}`
    try {
      const body = (await response.json()) as ApiErrorBody
      if (body?.error) {
        code = body.error.code ?? code
        message = body.error.message ?? message
      }
    } catch {
      // Not JSON (an nginx error page, say). Keep the generic message.
    }
    throw new ApiError(message, response.status, code)
  }

  return (await response.json()) as T
}

export function fetchClusters(signal?: AbortSignal): Promise<Page<Cluster>> {
  return request<Page<Cluster>>('/clusters?limit=50', signal)
}

export function fetchOverview(clusterId: string, signal?: AbortSignal): Promise<Overview> {
  return request<Overview>(`/clusters/${clusterId}/overview`, signal)
}

export function fetchLogs(
  clusterId: string,
  component: string,
  lines: number,
  signal?: AbortSignal,
): Promise<Envelope<LogsLive>> {
  const query = new URLSearchParams({ component, lines: String(lines) })
  return request<Envelope<LogsLive>>(`/clusters/${clusterId}/logs?${query}`, signal)
}
