// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useLiveData } from './useLiveData'

class Socket {
  static instances: Socket[] = []
  onopen?: () => void
  onmessage?: (event: { data: string }) => void
  onclose?: () => void
  constructor() { Socket.instances.push(this) }
  close() { this.onclose?.() }
}

afterEach(() => {
  cleanup()
  Socket.instances = []
  vi.unstubAllGlobals()
})

it.each([
  ['/api/incidents', 'success'], ['/api/incidents', 'failure'],
  ['/api/services', 'success'], ['/api/services', 'failure'],
  ['/api/services/graph', 'success'], ['/api/services/graph', 'failure'],
])('discards delayed %s %s from the previous environment', async (heldPath, outcome) => {
  let resolve!: (response: Response) => void
  let reject!: (error: Error) => void
  const held = new Promise<Response>((yes, no) => { resolve = yes; reject = no })
  let mode: 'aws' | 'local' = 'aws'
  vi.stubGlobal('WebSocket', Socket)
  const fetchMock = vi.fn(async (path: string) => {
    if (path === heldPath && mode === 'aws') return held
    const payload = path === '/api/environment' ? {
      mode, incident_source: mode, detection: {}, diagnosis: {}, restart_commands: {},
    } : []
    return { ok: true, json: async () => payload } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  const { result } = renderHook(useLiveData)
  await waitFor(() => {
    expect(result.current.environment?.mode).toBe('aws')
    expect(fetchMock).toHaveBeenCalledWith(heldPath, expect.anything())
  })
  await act(async () => { mode = 'local'; Socket.instances.at(-1)?.onopen?.() })
  await waitFor(() => expect(result.current.environment?.mode).toBe('local'))
  await act(async () => {
    if (outcome === 'failure') reject(new Error('old AWS request failed'))
    else resolve({ ok: true, json: async () => [{ incident_id: 'INC-1001', title: 'old AWS record', name: 'old AWS service' }] } as Response)
  })
  expect(result.current.incidents).toEqual([])
  expect(result.current.services).toEqual([])
  expect(result.current.graph).toEqual([])
  expect(result.current.loadError).toBeNull()
})

it('discards messages from a closed socket while reconnecting and accepts its replacement', async () => {
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('fetch', vi.fn(async (path: string) => ({
    ok: true,
    json: async () => path === '/api/environment' ? { mode: 'local' } : [],
  })))
  const { result } = renderHook(useLiveData)
  await waitFor(() => expect(result.current.environment?.mode).toBe('local'))
  const closedSocket = Socket.instances.at(-1)!
  await act(async () => {
    closedSocket.onclose?.()
    closedSocket.onmessage?.({ data: JSON.stringify({ type: 'services', data: [{ name: 'obsolete service' }] }) })
  })
  expect(result.current.connection).toBe('offline')
  expect(result.current.services).toEqual([])
  await waitFor(() => expect(Socket.instances.at(-1)).not.toBe(closedSocket), { timeout: 2000 })
  await act(async () => {
    const replacement = Socket.instances.at(-1)!
    replacement.onopen?.()
  })
  await act(async () => Socket.instances.at(-1)?.onmessage?.({ data: JSON.stringify({ type: 'services', data: [{ name: 'current service' }] }) }))
  expect(result.current.connection).toBe('live')
  expect(result.current.services).toEqual([{ name: 'current service' }])
})
