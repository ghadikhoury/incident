import { describe, expect, it } from 'vitest'
import type { Incident, ServiceHealth } from './api'
import { describeChaos, formatAge, mergeIncidents, systemHealth, upsertIncident } from './logic'

function service(name: string, status: ServiceHealth['status']): ServiceHealth {
  return {
    name,
    display_name: name,
    status,
    depends_on: [],
    error: null,
    response_ms: null,
    chaos: null,
    checked_at: null,
  }
}

function incident(id: string, createdAt: string, status: Incident['status'] = 'OPEN'): Incident {
  return {
    incident_id: id,
    title: id,
    service: 'payment',
    severity: 'SEV-3',
    status,
    trigger: 'MANUAL',
    summary: null,
    assigned_to: null,
    created_at: createdAt,
    updated_at: createdAt,
    resolved_at: null,
  }
}

describe('systemHealth', () => {
  it('is unknown before the first health check', () => {
    expect(systemHealth([])).toBe('UNKNOWN')
    expect(systemHealth([service('gateway', 'unknown')])).toBe('UNKNOWN')
  })

  it('is operational when everything is healthy', () => {
    expect(systemHealth([service('gateway', 'healthy'), service('payment', 'healthy')])).toBe(
      'OPERATIONAL',
    )
  })

  it('is degraded when an internal service fails', () => {
    expect(systemHealth([service('gateway', 'healthy'), service('payment', 'unhealthy')])).toBe(
      'DEGRADED',
    )
  })

  it('is an outage when the gateway fails', () => {
    expect(systemHealth([service('gateway', 'down'), service('payment', 'healthy')])).toBe('OUTAGE')
  })
})

describe('formatAge', () => {
  const start = Date.parse('2026-10-02T12:00:00Z')
  const at = (seconds: number) => start + seconds * 1000

  it.each([
    [0, '0s'],
    [59, '59s'],
    [60, '1m'],
    [3599, '59m'],
    [3600 + 5 * 60, '1h 5m'],
    [3 * 86400, '3d'],
  ])('%i seconds -> %s', (seconds, expected) => {
    expect(formatAge('2026-10-02T12:00:00Z', at(seconds))).toBe(expected)
  })

  it('never goes negative with clock skew', () => {
    expect(formatAge('2026-10-02T12:00:00Z', at(-5))).toBe('0s')
  })
})

describe('upsertIncident', () => {
  it('adds new incidents newest first', () => {
    const list = [incident('INC-1001', '2026-10-02T12:00:00Z')]
    const result = upsertIncident(list, incident('INC-1002', '2026-10-02T12:05:00Z'))
    expect(result.map((i) => i.incident_id)).toEqual(['INC-1002', 'INC-1001'])
  })

  it('replaces an existing incident in place', () => {
    const list = [
      incident('INC-1002', '2026-10-02T12:05:00Z'),
      incident('INC-1001', '2026-10-02T12:00:00Z'),
    ]
    const result = upsertIncident(list, incident('INC-1001', '2026-10-02T12:00:00Z', 'RESOLVED'))
    expect(result.map((i) => [i.incident_id, i.status])).toEqual([
      ['INC-1002', 'OPEN'],
      ['INC-1001', 'RESOLVED'],
    ])
  })

  it('does not replace a successful action with an older delayed socket event', () => {
    const acknowledged = incident('INC-1001', '2026-10-02T12:00:00Z', 'ACKNOWLEDGED')
    acknowledged.updated_at = '2026-10-02T12:01:00Z'
    const stale = incident('INC-1001', '2026-10-02T12:00:00Z')
    expect(upsertIncident([acknowledged], stale)).toEqual([acknowledged])
  })
})

describe('mergeIncidents', () => {
  it('keeps a live update that is newer than the fetched copy', () => {
    const live = { ...incident('INC-1001', '2026-10-02T12:00:00Z', 'ACKNOWLEDGED') }
    live.updated_at = '2026-10-02T12:01:00Z'
    const stale = incident('INC-1001', '2026-10-02T12:00:00Z', 'OPEN')
    const fetchedOnly = incident('INC-1002', '2026-10-02T12:02:00Z')
    const result = mergeIncidents([live], [stale, fetchedOnly])
    expect(result.map((i) => [i.incident_id, i.status])).toEqual([
      ['INC-1002', 'OPEN'],
      ['INC-1001', 'ACKNOWLEDGED'],
    ])
  })
})

describe('describeChaos', () => {
  it('is null when nothing is injected', () => {
    expect(describeChaos(null)).toBeNull()
    expect(describeChaos({ latency_ms: 0, error_rate: 0, db_delay_s: 0 })).toBeNull()
  })

  it('lists active failures', () => {
    expect(describeChaos({ latency_ms: 3000, error_rate: 0.4, db_delay_s: 3 })).toBe(
      'DB +3s, +3000ms, 40% errors',
    )
  })
})
