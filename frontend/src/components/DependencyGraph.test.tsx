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
