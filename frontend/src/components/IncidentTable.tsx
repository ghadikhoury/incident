import { useState } from 'react'
import type { Incident } from '../api'
import { formatAge } from '../logic'

interface Props {
  incidents: Incident[]
  now: number
  busy: boolean
  onAcknowledge: (id: string) => void
  onResolve: (id: string) => void
}

export function IncidentTable({ incidents, now, busy, onAcknowledge, onResolve }: Props) {
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
                <th>Service</th>
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
                  </td>
                  <td className="mono">{incident.incident_id}</td>
                  <td>{incident.title}</td>
                  <td>{incident.service}</td>
                  <td>
                    <span className={`status status--${incident.status}`}>{incident.status}</span>
                  </td>
                  <td>{incident.assigned_to ?? <span className="muted">unassigned</span>}</td>
                  <td className="mono">{formatAge(incident.created_at, now)}</td>
                  <td className="actions">
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
