// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { Incident } from '../api'
import { DiagnosisPanel } from './DiagnosisPanel'

afterEach(cleanup)

const incident: Incident = {
  incident_id: 'INC-1008',
  title: 'payment alarm',
  service: 'payment',
  severity: 'SEV-1',
  status: 'OPEN',
  trigger: 'CLOUDWATCH',
  summary: null,
  assigned_to: null,
  created_at: '2026-10-03T10:00:00Z',
  updated_at: '2026-10-03T10:00:00Z',
  resolved_at: null,
  alerts: [{
    alarm_name: 'incident-payment-latency',
    service: 'payment', signal: 'latency', state: 'ALARM',
    first_at: '2026-10-03T10:00:00Z', last_at: '2026-10-03T10:00:00Z',
    observed_value: 3000, threshold: 1000,
  }],
  diagnosis: {
    status: 'READY', claimed_at: '2026-10-03T10:01:00Z',
    summary: 'Requests queue behind occupied connections',
    likely_root_cause: 'Payment connection-pool exhaustion', confidence: 'high',
    evidence: ['payment pool timeout in logs'],
    recommended_actions: [{
      id: 'clear_chaos:payment', action: 'clear_chaos', service: 'payment',
      reason: 'Reset simulated fault', status: 'PENDING', decided_by: null, changed_at: null,
    }],
  },
}

it('separates observed signals from AI inference and requires a named decision', () => {
  const approve = vi.fn()
  const reject = vi.fn()
  const { rerender } = render(
    <DiagnosisPanel incident={incident} actor="" busy={false} onApprove={approve} onReject={reject} onRetry={() => {}} />,
  )
  expect(screen.getByText('Observed facts')).toBeTruthy()
  expect(screen.getByText('AI inference')).toBeTruthy()
  expect(screen.getByText(/observed 3000/)).toBeTruthy()
  expect(screen.getByText('Payment connection-pool exhaustion')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Approve and run' }) as HTMLButtonElement).disabled).toBe(true)
  rerender(
    <DiagnosisPanel incident={incident} actor="alice" busy={false} onApprove={approve} onReject={reject} onRetry={() => {}} />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
  expect(reject).toHaveBeenCalledWith('INC-1008', 'clear_chaos:payment')
  expect(approve).not.toHaveBeenCalled()
})

it('shows unavailable analysis without blocking incident facts', () => {
  const retry = vi.fn()
  render(
    <DiagnosisPanel incident={{ ...incident, diagnosis: { ...incident.diagnosis!, status: 'UNAVAILABLE' } }}
      actor="alice" busy={false} onApprove={() => {}} onReject={() => {}} onRetry={retry} />,
  )
  expect(screen.getByText('AI analysis unavailable.')).toBeTruthy()
  expect(screen.getByText(/observed 3000/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Approve and run' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry analysis' }))
  expect(retry).toHaveBeenCalledWith('INC-1008')
})
