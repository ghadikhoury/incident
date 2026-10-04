// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { DemoPanel } from './DemoPanel'
import { DiagnosisPanel } from './DiagnosisPanel'
import { IncidentDetailPage } from './IncidentDetailPage'
import type { Environment, Incident, ServiceHealth } from '../api'

const environment: Environment = {
  mode: 'local', incident_source: 'local SQLite',
  detection: { source: 'local health probes', status: 'available', checked_at: null, failure_duration_s: 9 },
  diagnosis: { provider: 'gemini', configuration: 'missing_key' },
  restart_commands: { inventory: 'docker compose start inventory' },
}
const inventory: ServiceHealth = { name: 'inventory', display_name: 'Inventory', status: 'down',
  depends_on: [], error: 'unreachable: ConnectError', response_ms: null, chaos: null, checked_at: null }
const incident: Incident = { incident_id: 'INC-1001', title: 'inventory incident', service: 'inventory',
  severity: 'SEV-2', status: 'OPEN', trigger: 'LOCAL_HEALTH', summary: null, assigned_to: null,
  created_at: '2026-10-04T00:00:00Z', updated_at: '2026-10-04T00:00:10Z', resolved_at: null,
  diagnosis: { status: 'UNAVAILABLE', claimed_at: '2026-10-04T00:00:09Z', unavailable_reason: 'missing_configuration',
    summary: null, likely_root_cause: null, confidence: null, evidence: [], recommended_actions: [] } }

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('offers precise operator crash recovery without sending a backend restart action', async () => {
  const recover = vi.fn()
  const copy = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } })
  render(<DemoPanel services={[inventory]} environment={environment} busy={false}
    onInject={vi.fn()} onRecover={recover} onDeclare={vi.fn()} />)
  fireEvent.change(screen.getAllByRole('combobox', { name: 'Service' })[0], { target: { value: 'inventory' } })
  expect(screen.getByText('docker compose start inventory')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Recover' }))
  expect(recover).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Copy restart command' }))
  expect(await screen.findByText('Copied.')).toBeTruthy()
  expect(copy).toHaveBeenCalledWith('docker compose start inventory')
})

it('shows missing configuration and an explicit audited retry without recommended actions', () => {
  const retry = vi.fn()
  render(<DiagnosisPanel incident={incident} actor="engineer" busy={false} onApprove={vi.fn()} onReject={vi.fn()} onRetry={retry} />)
  expect(screen.getByText(/backend Gemini API key is missing/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve and run' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry analysis' }))
  expect(retry).toHaveBeenCalledWith('INC-1001')
})

it('renders actual local probes and health metrics on Review without CloudWatch labels', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => ({ ok: true, json: async () =>
    path.endsWith('/metrics') ? { start: incident.created_at, end: incident.updated_at, series: [
      { service: 'inventory', metric: 'HealthCheckFailed', points: [{ at: incident.updated_at, value: 1 }] },
    ] } : path.endsWith('/logs') ? { source: 'collected local health probes', rows: Array.from({ length: 210 }, (_, index) => (
      { service: 'inventory', '@timestamp': incident.updated_at,
        message: index === 0 ? 'Observed /health: down' : index === 209 ? 'Latest recovery: healthy' : `Probe ${index}` }
    )) } : { ...incident, timeline: [] },
  })))
  render(<IncidentDetailPage incident={incident} graph={[]} services={[inventory]} environment={environment}
    actor="engineer" busy={false} onBack={vi.fn()} onAcknowledge={vi.fn()} onResolve={vi.fn()}
    onPatch={vi.fn()} onApprove={vi.fn()} onReject={vi.fn()} onRetry={vi.fn()} />)
  expect(await screen.findByText('Observed /health: down')).toBeTruthy()
  expect(screen.getByText('Latest recovery: healthy')).toBeTruthy()
  expect(screen.queryByText('Probe 105')).toBeNull()
  expect(screen.getByRole('img', { name: 'Health-probe failure (1 = failed) over time' })).toBeTruthy()
  expect(screen.getByText('Collected health evidence')).toBeTruthy()
  expect(screen.getByText('docker compose start inventory')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Search CloudWatch' })).toBeNull()
})
