/** Formatting helpers shared by the panels. */

import { useEffect, useState } from 'react'

/** A clock that ticks, so "N s ago" actually counts up between polls. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}

export function parseTime(value: string | null | undefined): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

/** "3s ago", "4m ago". Coarse on purpose: precision here is false comfort. */
export function relativeTime(value: string | null | undefined, now: number): string {
  const parsed = parseTime(value)
  if (parsed === null) return 'unknown'
  const seconds = Math.max(0, Math.round((now - parsed) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

/** Wall-clock time, for the "as of 12:03:41 (stale)" label. */
export function clockTime(value: string | null | undefined): string {
  const parsed = parseTime(value)
  if (parsed === null) return '--:--:--'
  return new Date(parsed).toLocaleTimeString(undefined, { hour12: false })
}

export function uptime(startedAt: string | null, now: number): string {
  const parsed = parseTime(startedAt)
  if (parsed === null) return '—'
  const seconds = Math.max(0, Math.floor((now - parsed) / 1000))
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`
  return `${seconds}s`
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (!Number.isFinite(value)) return '—'
  if (Number.isInteger(value)) return value.toLocaleString()
  if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString()
  return value.toFixed(Math.abs(value) < 1 ? 4 : 2)
}

/** Bytes and seconds get unit-aware rendering; everything else is a count. */
export function formatMetric(value: number | null, unit: string): string {
  if (value === null) return '—'
  if (unit === 'bytes') return formatBytes(value)
  if (unit === 'seconds') return `${formatNumber(value)}s`
  if (unit === 'ms') return `${formatNumber(value)} ms`
  return formatNumber(value)
}

export function formatBytes(bytes: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let index = 0
  while (Math.abs(value) >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

export function truncate(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 3)}...`
}
