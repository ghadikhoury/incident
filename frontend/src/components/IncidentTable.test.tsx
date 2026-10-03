// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import type { Incident } from '../api'
import { IncidentTable } from './IncidentTable'

afterEach(cleanup)

it('shows probable root, downstream impact, severity reason, and correlated alerts', () => {
  const incident: Incident = {
    incident_id: 'INC-1007',
    title: 'order errors alarm',
    service: 'payment',
    severity: 'SEV-1',
    severity_reason: '3 services have active alarms',
    status: 'OPEN',
    trigger: 'CLOUDWATCH',
    summary: null,
    assigned_to: null,
    created_at: '2026-10-03T12:00:00Z',
    updated_at: '2026-10-03T12:01:00Z',
    resolved_at: null,
    probable_root: 'payment',
    downstream_services: ['gateway', 'order'],
    correlation_label: 'PROBABLE CASCADING FAILURE',
    alerts: [
      {
        alarm_name: 'incident-payment-latency',
        service: 'payment',
        signal: 'latency',
        state: 'ALARM',
        first_at: '2026-10-03T12:00:00Z',
        last_at: '2026-10-03T12:00:00Z',
        observed_value: 4900,
        threshold: 2000,
      },
    ],
  }
  render(
    <IncidentTable
      incidents={[incident]}
      now={Date.parse('2026-10-03T12:02:00Z')}
      busy={false}
      onAcknowledge={() => {}}
      onResolve={() => {}}
      onSelect={() => {}}
    />,
  )
  expect(screen.getByText('PROBABLE CASCADING FAILURE')).toBeTruthy()
  expect(screen.getByText('Downstream: gateway, order')).toBeTruthy()
  expect(screen.getByText('3 services have active alarms')).toBeTruthy()
  fireEvent.click(screen.getByText('1 alerts'))
  expect(screen.getByText('payment latency: ALARM')).toBeTruthy()
})
