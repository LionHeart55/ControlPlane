/**
 * Exactly three queries, and only two of them poll.
 *
 * `/overview` is a deliberate server-side fan-out so the browser makes one
 * request instead of six. Giving each panel its own query would multiply the
 * request rate by six and, worse, let panels disagree with each other -- the
 * components table showing a container up while the health pill said it was
 * down, because they were fetched two seconds apart.
 *
 * `keepPreviousData` matters as much as the interval: without it every poll
 * would unmount the panels and flash them back to their loading state, and a
 * single failed poll would blank the whole page.
 */

import { keepPreviousData, useQuery } from '@tanstack/react-query'

import { fetchClusters, fetchLogs, fetchOverview } from '../api/client'

export const POLL_INTERVAL_MS = 5000
export const LOG_LINES = 100

/** One-shot: which cluster are we looking at? Not polled. */
export function useClusters() {
  return useQuery({
    queryKey: ['clusters'],
    queryFn: ({ signal }) => fetchClusters(signal),
    staleTime: 60_000,
    retry: 1,
  })
}

export function useOverview(clusterId: string | undefined) {
  return useQuery({
    queryKey: ['overview', clusterId],
    queryFn: ({ signal }) => fetchOverview(clusterId as string, signal),
    enabled: Boolean(clusterId),
    refetchInterval: POLL_INTERVAL_MS,
    // Keep polling when the tab is hidden: the reliability demo involves
    // switching to a terminal to stop a container, and coming back to a
    // dashboard that had frozen would defeat the point.
    refetchIntervalInBackground: true,
    retry: 1,
    placeholderData: keepPreviousData,
  })
}

export function useLogs(clusterId: string | undefined, component: string, enabled: boolean) {
  return useQuery({
    queryKey: ['logs', clusterId, component],
    queryFn: ({ signal }) => fetchLogs(clusterId as string, component, LOG_LINES, signal),
    enabled: Boolean(clusterId) && enabled,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
    placeholderData: keepPreviousData,
  })
}
