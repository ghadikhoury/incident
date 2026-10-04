// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from './App'
import type { Incident, ServiceHealth } from './api'

const original: Incident = {
  incident_id: 'INC-1001',
  title: 'Payment latency spike',
  service: 'payment',
  severity: 'SEV-3',
  status: 'OPEN',
  trigger: 'MANUAL',
  summary: null,
  assigned_to: null,
  created_at: '2026-10-02T12:00:00Z',
  updated_at: '2026-10-02T12:00:00Z',
  resolved_at: null,
}

const service: ServiceHealth = {
  name: 'payment',
  display_name: 'Payment',
  status: 'healthy',
  depends_on: [],
  error: null,
  response_ms: 10,
  chaos: null,
  checked_at: '2026-10-02T12:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('discards cached AWS incidents when the same dashboard backend switches to local mode', async () => {
  let openSocket: (() => void) | undefined
  class Socket {
    set onopen(callback: () => void) { openSocket = callback }
    close() {}
  }
  vi.stubGlobal('WebSocket', Socket)
  let mode: 'aws' | 'local' = 'aws'
  vi.stubGlobal('fetch', vi.fn(async (path: string) => ({ ok: true, json: async () => {
    if (path === '/api/environment') return { mode, incident_source: mode === 'aws' ? 'AWS DynamoDB' : 'local SQLite',
      detection: { source: mode === 'aws' ? 'AWS alarm pipeline' : 'local health probes', status: 'available', failure_duration_s: 9 },
      diagnosis: { provider: 'gemini', configuration: 'configured' }, restart_commands: {} }
    if (path === '/api/incidents') return mode === 'aws' ? [{ ...original, title: 'AWS-only record' }] : []
    if (path === '/api/services') return [service]
    return []
  } })))
  render(<App />)
  expect(await screen.findByText('AWS-only record')).toBeTruthy()
  mode = 'local'
  openSocket?.()
  expect(await screen.findByText(/Local demo · local SQLite/)).toBeTruthy()
  await waitFor(() => expect(screen.queryByText('AWS-only record')).toBeNull())
  expect(screen.getByText('No active incidents.')).toBeTruthy()
})

it('loads incidents and applies acknowledge/resolve responses while the socket never opens', async () => {
  class UnavailableSocket {
    close() {}
  }
  vi.stubGlobal('WebSocket', UnavailableSocket)
  const requests: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      requests.push(`${method} ${input}`)
      let payload: unknown
      if (input === '/api/incidents' && method === 'GET') payload = [original]
      else if (input === '/api/services') payload = [service]
      else if (input === '/api/services/graph') payload = []
      else if (input === '/api/environment') payload = { mode: 'local', incident_source: 'local SQLite',
        detection: { source: 'local health probes', status: 'available', failure_duration_s: 9 },
        diagnosis: { provider: 'gemini', configuration: 'missing_key' }, restart_commands: {} }
      else if (input.endsWith('/acknowledge')) {
        payload = { ...original, status: 'ACKNOWLEDGED', updated_at: '2026-10-02T12:01:00Z' }
      } else if (input.endsWith('/resolve')) {
        payload = {
          ...original,
          status: 'RESOLVED',
          updated_at: '2026-10-02T12:02:00Z',
          resolved_at: '2026-10-02T12:02:00Z',
        }
      } else throw new Error(`unexpected request: ${method} ${input}`)
      return { ok: true, json: async () => payload } as Response
    }),
  )

  render(<App />)
  expect(await screen.findByText('Payment latency spike')).toBeTruthy()
  expect(await screen.findByText(/Local demo · local SQLite/)).toBeTruthy()
  expect(screen.getByText(/Local records are isolated from AWS/)).toBeTruthy()
  expect(requests).toContain('GET /api/incidents')
  fireEvent.click(screen.getByRole('button', { name: 'Acknowledge' }))
  await waitFor(() => expect(screen.getAllByText('ACKNOWLEDGED').length).toBeGreaterThan(0))
  expect(screen.queryByRole('button', { name: 'Acknowledge' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Resolve' }))
  await waitFor(() => expect(screen.getByText('No active incidents.')).toBeTruthy())
  expect(requests).toContain('POST /api/incidents/INC-1001/acknowledge')
  expect(requests).toContain('POST /api/incidents/INC-1001/resolve')
})
