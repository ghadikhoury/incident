import type { ServiceHealth, ServiceNode } from '../api'

interface Props {
  nodes: ServiceNode[]
  services: ServiceHealth[]
  alertedServices?: ReadonlySet<string>
}

const WIDTH = 850
const HEIGHT = 280
const NODE_WIDTH = 138
const NODE_HEIGHT = 50

export function DependencyGraph({ nodes, services, alertedServices }: Props) {
  if (!nodes.length) return null

  const byName = new Map(nodes.map((node) => [node.name, node]))
  const dependents = new Map(nodes.map((node) => [node.name, [] as string[]]))
  for (const node of nodes) {
    for (const dependency of node.depends_on) dependents.get(dependency)?.push(node.name)
  }
  const depths = new Map<string, number>()
  const depth = (name: string): number => {
    if (depths.has(name)) return depths.get(name)!
    const parents = dependents.get(name) ?? []
    const result = parents.length ? Math.max(...parents.map(depth)) + 1 : 0
    depths.set(name, result)
    return result
  }
  for (const node of nodes) depth(node.name)
  const maxDepth = Math.max(...depths.values())
  const layers = new Map<number, ServiceNode[]>()
  for (const node of nodes) {
    const layer = depths.get(node.name)!
    layers.set(layer, [...(layers.get(layer) ?? []), node])
  }
  const positions = new Map<string, { x: number; y: number }>()
  for (const [layer, members] of layers) {
    members.forEach((node, index) =>
      positions.set(node.name, {
        x: NODE_WIDTH / 2 + 12 + (layer * (WIDTH - NODE_WIDTH - 24)) / Math.max(maxDepth, 1),
        y: ((index + 1) * HEIGHT) / (members.length + 1),
      }),
    )
  }
  const health = new Map(services.map((service) => [service.name, service.status]))

  return (
    <section className="panel dependency-panel">
      <h2>Service dependencies</h2>
      <p className="muted">Arrows point to services each component depends on.
        {alertedServices && ' Red nodes have active incident alarms.'}</p>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Service dependency graph">
        <defs>
          <marker id="dependency-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 Z" fill="currentColor" />
          </marker>
        </defs>
        {nodes.flatMap((node) =>
          node.depends_on.map((dependency) => {
            const from = positions.get(node.name)
            const to = positions.get(dependency)
            if (!from || !to || !byName.has(dependency)) return null
            return (
              <line
                key={`${node.name}-${dependency}`}
                x1={from.x + NODE_WIDTH / 2}
                y1={from.y}
                x2={to.x - NODE_WIDTH / 2 - 8}
                y2={to.y}
                className="dependency-edge"
                markerEnd="url(#dependency-arrow)"
              />
            )
          }),
        )}
        {nodes.map((node) => {
          const at = positions.get(node.name)!
          const status = health.get(node.name) ?? 'unknown'
          const alarmed = alertedServices?.has(node.name) ?? false
          return (
            <g key={node.name} className={`dependency-node dependency-node--${alarmed ? 'alarm' : status}`}>
              <rect x={at.x - NODE_WIDTH / 2} y={at.y - NODE_HEIGHT / 2} width={NODE_WIDTH} height={NODE_HEIGHT} rx="8" />
              <text x={at.x} y={at.y - 2} textAnchor="middle">{node.display_name}</text>
              <text x={at.x} y={at.y + 15} textAnchor="middle" className="dependency-node__status">
                {node.monitored ? alarmed ? 'alarm active' : status : 'dependency'}
              </text>
            </g>
          )
        })}
      </svg>
    </section>
  )
}
