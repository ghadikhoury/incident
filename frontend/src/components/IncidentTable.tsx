import { useState } from 'react'
import type { Incident } from '../api'
import { formatAge } from '../logic'

interface Props {
  incidents: Incident[]
  now: number
  busy: boolean
  onAcknowledge: (id: string) => void
  onResolve: (id: string) => void
  onSelect: (id: string) => void
}

export function IncidentTable({ incidents, now, busy, onAcknowledge, onResolve, onSelect }: Props) {
  const [showResolved, setShowResolved] = useState(false)
  const visible = showResolved ? incidents : incidents.filter((i) => i.status !== 'RESOLVED')
  const activeCount = incidents.filter((i) => i.status !== 'RESOLVED').length

  return (
    <section className="panel">
      <div className="panel__header">
        <h2>
          {showResolved ? 'All incidents' : 'Active incidents'}{' '}
          <span className="count">{showResolved ? incidents.length : activeCount}</span>
        </h2>
        <label className="toggle">
          <input
            type="checkbox"
            checked={showResolved}
            onChange={(e) => setShowResolved(e.target.checked)}
          />
          Show resolved
        </label>
      </div>
      {visible.length === 0 ? (
        <p className="muted">No {showResolved ? '' : 'active '}incidents.</p>
      ) : (
        <div className="table-wrap">
          <table className="incidents">
            <thead>
              <tr>
                <th>Sev</th>
                <th>ID</th>
                <th>Title</th>
                <th>Root / service</th>
                <th>Alerts</th>
                <th>Status</th>
                <th>Owner</th>
                <th>Age</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {visible.map((incident) => (
                <tr key={incident.incident_id}>
                  <td>
                    <span className={`sev sev--${incident.severity}`}>{incident.severity}</span>
                    {incident.severity_reason && (
                      <small className="severity-reason">{incident.severity_reason}</small>
                    )}
                  </td>
                  <td className="mono">{incident.incident_id}</td>
                  <td>
                    {incident.title}
                    {incident.correlation_label && (
                      <span className="cascade-label">{incident.correlation_label}</span>
                    )}
                  </td>
                  <td>
                    {incident.probable_root ?? incident.service}
                    {!!incident.downstream_services?.length && (
                      <small className="downstream">Downstream: {incident.downstream_services.join(', ')}</small>
                    )}
                  </td>
                  <td>
                    {incident.alerts?.length ? (
                      <details className="alert-details">
                        <summary>{incident.alerts.length} alerts</summary>
                        <ul>
                          {incident.alerts.map((alert) => (
                            <li key={alert.alarm_name}>
                              {alert.service} {alert.signal}: {alert.state}
                            </li>
                          ))}
                        </ul>
                      </details>
                    ) : (
                      '0'
                    )}
                  </td>
                  <td>
                    <span className={`status status--${incident.status}`}>{incident.status}</span>
                  </td>
                  <td>{incident.assigned_to ?? <span className="muted">unassigned</span>}</td>
                  <td className="mono">{formatAge(incident.created_at, now)}</td>
                  <td className="actions">
                    <button onClick={() => onSelect(incident.incident_id)}>Review</button>
                    {incident.status === 'OPEN' && (
                      <button disabled={busy} onClick={() => onAcknowledge(incident.incident_id)}>
                        Acknowledge
                      </button>
                    )}
                    {incident.status !== 'RESOLVED' && (
                      <button disabled={busy} onClick={() => onResolve(incident.incident_id)}>
                        Resolve
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
