// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { IncidentDetailPage } from './IncidentDetailPage'
import type { Incident } from '../api'

const incident: Incident = {
  incident_id: 'INC-1001', title: 'Payment failure', service: 'payment',
  severity: 'SEV-2', status: 'OPEN', trigger: 'CLOUDWATCH', summary: null,
  assigned_to: null, created_at: '2026-10-03T00:00:00Z',
  updated_at: '2026-10-03T00:01:00Z', resolved_at: null,
  probable_root: 'payment', downstream_services: ['order'],
  alerts: [{ alarm_name: 'incident-payment-errors', service: 'payment', signal: 'errors',
    state: 'ALARM', first_at: '2026-10-03T00:00:00Z', last_at: '2026-10-03T00:00:00Z',
    observed_value: 30, threshold: 20 }],
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('loads detail telemetry, filters saved logs, and offers audited controls', async () => {
  const fetcher = vi.fn(async (path: string) => {
    let result: unknown
    if (path.includes('/log-search')) result = { source: 'CloudWatch Logs search', rows: [
      { '@timestamp': '2026-10-03T00:02:00Z', service: 'payment', error_type: 'PoolTimeout' },
    ] }
    else if (path.endsWith('/metrics')) result = { start: '2026-10-02T23:55:00Z', end: '2026-10-03T00:05:00Z',
      series: [{ service: 'payment', metric: 'Latency', points: [{ at: '2026-10-03T00:00:00Z', value: 2100 }] }] }
    else if (path.endsWith('/logs')) result = { source: 'saved excerpts', rows: [
      { '@timestamp': '2026-10-03T00:00:00Z', service: 'payment', error_type: 'PoolTimeout' },
      { '@timestamp': '2026-10-03T00:01:00Z', service: 'order', error_type: 'DependencyTimeout' },
    ] }
    else result = { ...incident, timeline: [{ at: '2026-10-03T00:00:00Z', kind: 'alarm', message: 'alarm fired', actor: null }] }
    return { ok: true, json: async () => result } as Response
  })
  vi.stubGlobal('fetch', fetcher)
  const onPatch = vi.fn()
  render(<IncidentDetailPage incident={incident} graph={[]} services={[]} actor="alice" busy={false}
    onBack={vi.fn()} onAcknowledge={vi.fn()} onResolve={vi.fn()} onPatch={onPatch}
    onApprove={vi.fn()} onReject={vi.fn()} onRetry={vi.fn()} />)
  expect(await screen.findByText('PoolTimeout')).toBeTruthy()
  expect(screen.getByText('alarm fired')).toBeTruthy()
  expect(screen.getByRole('img', { name: 'Latency p90 (ms) over time' })).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'Search saved logs' }), { target: { value: 'PoolTimeout' } })
  expect(screen.queryByText('DependencyTimeout')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Search CloudWatch' }))
  expect(await screen.findByText(/CloudWatch Logs search:/)).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'Incident owner' }), { target: { value: 'bob' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save owner' }))
  await waitFor(() => expect(onPatch).toHaveBeenCalledWith('INC-1001', { assigned_to: 'bob' }))
  expect(fetcher).toHaveBeenCalledWith('/api/incidents/INC-1001/metrics', expect.objectContaining({ method: 'GET' }))
})
