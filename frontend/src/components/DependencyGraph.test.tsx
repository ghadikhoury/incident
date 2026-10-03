// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import type { ServiceNode } from '../api'
import { DependencyGraph } from './DependencyGraph'

afterEach(cleanup)

it('draws the configured dependencies and unmonitored database', () => {
  const nodes: ServiceNode[] = [
    { name: 'order', display_name: 'Orders', depends_on: ['payment'], monitored: true },
    { name: 'payment', display_name: 'Payments', depends_on: ['postgres'], monitored: true },
    { name: 'postgres', display_name: 'PostgreSQL', depends_on: [], monitored: false },
  ]
  const { container } = render(<DependencyGraph nodes={nodes} services={[]} />)
  expect(screen.getByRole('img', { name: 'Service dependency graph' })).toBeTruthy()
  expect(screen.getByText('PostgreSQL')).toBeTruthy()
  expect(container.querySelectorAll('line.dependency-edge')).toHaveLength(2)
})

it('marks services with incident alarms even when their health probes pass', () => {
  const nodes: ServiceNode[] = [
    { name: 'inventory', display_name: 'Inventory', depends_on: [], monitored: true },
  ]
  const { container } = render(<DependencyGraph nodes={nodes} services={[
    { name: 'inventory', display_name: 'Inventory', status: 'healthy', depends_on: [],
      error: null, response_ms: 5, chaos: null, checked_at: '2026-10-03T00:00:00Z' },
  ]} alertedServices={new Set(['inventory'])} />)
  expect(screen.getByText('alarm active')).toBeTruthy()
  expect(container.querySelector('.dependency-node--alarm')).toBeTruthy()
})
