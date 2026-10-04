// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  window.location.hash = ''
  vi.unstubAllGlobals()
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function switchingBackend(heldPath: string, held: Promise<Response>) {
  class Socket {
    static instances: Socket[] = []
    onopen?: () => void
    onmessage?: (event: { data: string }) => void
    onclose?: () => void
    constructor() { Socket.instances.push(this) }
    close() { this.onclose?.() }
  }
  vi.stubGlobal('WebSocket', Socket)
  let mode: 'aws' | 'local' = 'aws'
  const fetchMock = vi.fn(async (path: string) => {
    if (path === heldPath) return held
    let payload: unknown = []
    if (path === '/api/environment') payload = {
      mode, incident_source: mode === 'aws' ? 'AWS DynamoDB' : 'local SQLite',
      detection: { source: 'health probes', status: 'available', failure_duration_s: 9 },
      diagnosis: { provider: 'gemini', configuration: 'configured' }, restart_commands: {},
    }
    if (path === '/api/incidents') payload = [{ ...original, title: `${mode} record` }]
    if (path === '/api/services') payload = [service]
    return { ok: true, json: async () => payload } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    fetchMock,
    sockets: Socket.instances,
    switchToLocal: () => { mode = 'local'; Socket.instances.at(-1)?.onopen?.() },
  }
}

it.each(['success', 'failure'] as const)('discards an old-environment action %s after a mode switch, even with colliding IDs', async (outcome) => {
  const held = deferred<Response>()
  const backend = switchingBackend('/api/incidents/INC-1001/acknowledge', held.promise)
  render(<App />)
  await screen.findByText('aws record')
  fireEvent.click(screen.getByRole('button', { name: 'Acknowledge' }))
  expect(backend.fetchMock).toHaveBeenCalledWith('/api/incidents/INC-1001/acknowledge', expect.anything())
  backend.switchToLocal()
  await screen.findByText('local record')
  await act(async () => {
    if (outcome === 'success') held.resolve({ ok: true, json: async () => ({ ...original, title: 'aws record', status: 'ACKNOWLEDGED' }) } as Response)
    else held.reject(new Error('old AWS action failed'))
  })
  expect(screen.queryByText('aws record')).toBeNull()
  expect(screen.queryByText('old AWS action failed')).toBeNull()
  expect(screen.getByText('local record')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Acknowledge' }).hasAttribute('disabled')).toBe(false)
})

it('discards delayed incident detail responses from the previous environment', async () => {
  window.location.hash = '/incidents/INC-1999'
  const held = deferred<Response>()
  const backend = switchingBackend('/api/incidents/INC-1999', held.promise)
  render(<App />)
  await waitFor(() => expect(backend.fetchMock).toHaveBeenCalledWith('/api/incidents/INC-1999', expect.anything()))
  backend.switchToLocal()
  await screen.findByText('local record')
  await act(async () => held.resolve({ ok: true, json: async () => ({ ...original, incident_id: 'INC-1999', title: 'old AWS detail', timeline: [] }) } as Response))
  expect(screen.queryByText('old AWS detail')).toBeNull()
})

it('ignores messages and close events from an obsolete socket after switching environments', async () => {
  const backend = switchingBackend('/unused', Promise.resolve({} as Response))
  render(<App />)
  await screen.findByText('aws record')
  const oldSocket = backend.sockets.at(-1)!
  backend.switchToLocal()
  await screen.findByText('local record')
  const currentSocket = backend.sockets.at(-1)!
  expect(currentSocket).not.toBe(oldSocket)
  await act(async () => {
    oldSocket.onmessage?.({ data: JSON.stringify({ type: 'incident', data: { ...original, title: 'obsolete AWS push' } }) })
    oldSocket.onmessage?.({ data: JSON.stringify({ type: 'services', data: [{ ...service, display_name: 'Obsolete AWS service' }] }) })
    oldSocket.onclose?.()
  })
  expect(screen.queryByText('obsolete AWS push')).toBeNull()
  expect(screen.queryByText('Obsolete AWS service')).toBeNull()
  expect(screen.queryByText('Offline, retrying…')).toBeNull()
  await act(async () => currentSocket.onmessage?.({ data: JSON.stringify({ type: 'incident', data: { ...original, title: 'current local push' } }) }))
  expect(screen.getByText('current local push')).toBeTruthy()
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
