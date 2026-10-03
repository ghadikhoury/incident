// Pure helpers (no React), so they're easy to unit test.
import type { Chaos, Incident, ServiceHealth } from './api'

export type SystemHealth = 'OPERATIONAL' | 'DEGRADED' | 'OUTAGE' | 'UNKNOWN'

const ENTRY_POINT = 'gateway' // if this is down, customers can't reach anything

export function systemHealth(services: ServiceHealth[]): SystemHealth {
  if (services.length === 0 || services.some((s) => s.status === 'unknown')) return 'UNKNOWN'
  const failing = services.filter((s) => s.status !== 'healthy')
  if (failing.length === 0) return 'OPERATIONAL'
  if (failing.some((s) => s.name === ENTRY_POINT)) return 'OUTAGE'
  return 'DEGRADED'
}

export function formatAge(fromIso: string, now: number): string {
  const seconds = Math.max(0, Math.floor((now - Date.parse(fromIso)) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  return `${Math.floor(hours / 24)}d`
}

/** Insert or replace an incident, keeping the list newest first. */
export function upsertIncident(incidents: Incident[], incident: Incident): Incident[] {
  const existing = incidents.find((i) => i.incident_id === incident.incident_id)
  if (existing && existing.updated_at > incident.updated_at) return incidents
  const others = incidents.filter((i) => i.incident_id !== incident.incident_id)
  return [incident, ...others].sort((a, b) => b.created_at.localeCompare(a.created_at))
}

/**
 * Merge a freshly fetched list into the current one. A live update may have arrived
 * while the fetch was in flight, so for each incident keep the most recently updated copy.
 */
export function mergeIncidents(current: Incident[], fetched: Incident[]): Incident[] {
  const byId = new Map(current.map((i) => [i.incident_id, i]))
  for (const incident of fetched) {
    const existing = byId.get(incident.incident_id)
    if (!existing || incident.updated_at >= existing.updated_at) {
      byId.set(incident.incident_id, incident)
    }
  }
  return [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))
}

/** Human-readable description of injected failures, or null if none are active. */
export function describeChaos(chaos: Chaos | null): string | null {
  if (!chaos) return null
  const parts: string[] = []
  if (chaos.db_delay_s) parts.push(`DB +${chaos.db_delay_s}s`)
  if (chaos.latency_ms) parts.push(`+${chaos.latency_ms}ms`)
  if (chaos.error_rate) parts.push(`${Math.round(chaos.error_rate * 100)}% errors`)
  if (chaos.intermittent_every) parts.push(`every ${chaos.intermittent_every}th request fails`)
  if (chaos.cpu_ms) parts.push(`${chaos.cpu_ms}ms CPU work/request`)
  return parts.length ? parts.join(', ') : null
}
