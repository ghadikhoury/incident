import type { ServiceHealth } from '../api'
import { describeChaos } from '../logic'

const STATUS_LABEL: Record<ServiceHealth['status'], string> = {
  healthy: 'Healthy',
  unhealthy: 'Unhealthy',
  down: 'Down',
  unknown: 'Checking…',
}

export function ServiceList({ services }: { services: ServiceHealth[] }) {
  return (
    <section className="panel">
      <h2>Service health</h2>
      {services.length === 0 ? (
        <p className="muted">Waiting for the first health check…</p>
      ) : (
        <ul className="services">
          {services.map((service) => {
            const chaos = describeChaos(service.chaos)
            return (
              <li key={service.name} className={`service service--${service.status}`}>
                <div className="service__header">
                  <span className="service__name">{service.display_name}</span>
                  <span className={`pill pill--${service.status}`}>
                    {STATUS_LABEL[service.status]}
                  </span>
                </div>
                <div className="service__meta">
                  {service.response_ms !== null && <span>{service.response_ms} ms</span>}
                  {service.depends_on.length > 0 && (
                    <span>depends on {service.depends_on.join(', ')}</span>
                  )}
                </div>
                {service.error && <div className="service__error">{service.error}</div>}
                {chaos && <div className="service__chaos">Injected failure: {chaos}</div>}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
